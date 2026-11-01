# DSH dynamic-context recall plugin (T12)

`unified-memory.mjs` is a Node-standard-library module for DSH 0.2.1-rc.2. It
registers one dynamic `SystemPrompt.context` section (`interpolate: false`) and
updates its cache during the `agent/pre-step` hook, so the host's
`refreshContext` before the current model request reads this turn's recall.
It never edits messages, never uses `agent.inject` for next-turn delivery, and
clears the block at `agent/turn-stopping`. Recall is fetched from the KW service
over its owner-only Unix socket with the same scope binding as DMA.

Integration notes for the host-side loader (T17/T19):

* construct the plugin with `{socketPath, clientId, projectIds, deadlineMs,
  budgetTokens, limit}`;
* call `plugin.onPreStep(payload)` in the `agent/pre-step` listener (await it
  before continuing the waterfall; it is bounded by `deadlineMs`);
* call `plugin.onTurnStopping()` on `agent/turn-stopping`;
* optional indicator: `plugin.recallStatus()` returns
  `{provider_label, count}` for the last prefetch only.

The recalled block is quoted historical evidence with provenance labels. It is
not identity and not a system instruction; instruction-like/identity-like items
are dropped. The host wrapper for other providers adds an "authoritative
reference" note — that packaging risk is tracked for T19/review.


## Native Cordis loader (real-host pilot, 2026-11-01)

`native.mjs` exports the real Cordis `name`, `inject`, `apply` surface. It creates a memory context through each `agent.ctx` on the native `agent/created` event, preserving per-agent scope. The pre-step waterfall awaits recall and calls `next()`; it never returns the payload in place of a decision. Actual DSH Agent identity is `agent.id` (the legacy `sessionId` spelling remains supported in the lower-level helper).

Required non-secret config: `socketPath`, `clientId`, `workspaceId`, `instanceId`, `actorId`, `outboxPath`, `pythonPath`, `sessionExportModule`, plus optional `projectIds`, `deadlineMs`, `captureTimeoutMs`. `sessionExportModule` points to the installed host's `@deepseek-ai/dsh-session-log-export/lib/index.js`; its absolute path is deployment-specific. Core Python and both `.mjs` files are packaged in the wheel.

At turn-stopping, the loader uses the host's `flushLiveSessionLog` and `readSessionLogText` APIs, including the host's default multi-frame zstd storage. Do not directly decode the storage file with a one-frame convenience API. Logical JSONL goes through stdin to `python -m dma.integrations.dsh.capture`; no extra raw handoff file is written. The reader validates the native v4 file header/session id and all contiguous event seqs, skips known configuration/title/inbox bookkeeping, captures surfaced user/assistant/tool records and marks memory injection for exclusion. Unknown required events still fail visibly. Capture must complete durably before that hook returns. Delivery/extraction remain separate reliable workers; the hook does not call a model.

This loader was validated in an isolated actual DSH profile. Nested fork inheritance was validated through the installed SessionStore and native plugin handoff; resumed headless auto-compaction was also exercised through the installed compaction engine. The supported headless CLI rejects adopting fork/subagent sessions; interactive GUI fork continuation is not claimed. Keep native source logs through historical migration and rollback.

## Sending-process recovery

An outbox sending state has a bounded lease (at least 30 seconds and longer than the HTTP timeout). Expired sending rows, including legacy rows without a lease, become retryable without resetting an active sender on another process's startup. After a remote commit with lost acknowledgement, resend uses the same event/batch identity and the same durable receipt. Six real SIGKILL windows, including inside capture transaction and before/after remote acknowledgement, are recorded in the pilot artifacts.

## Current-turn retrieval status

Automatic recall distinguishes three outcomes in the model-visible context:
service unreachable (history not obtained), successful empty retrieval (no
matching records), and incomplete retrieval (use only returned evidence).
Unavailable notices contain no raw backend diagnostics and never reuse old
facts. Trivial turns and turn-stopping still clear the session cache. Context
notices are operational status, not source evidence or user assertions. This
change requires no host source/profile modification.

## Fork lineage

Native capture exports the current session and its fork ancestors in memory
through the configured host logical-export API. Copied events keep their exact
original session/seq/time/source, including nested forks and a child arriving
before its parent has been captured. A missing or inconsistent ancestor refuses
capture without advancing the cursor; native history remains available to retry.
The handoff accepts `--with-lineage` over stdin and never writes raw copies.
Exports are bounded to 64 ancestor levels. Deploy the matching Python package
and native plugin together; an older capture CLI cannot accept the new flag.

## Optional active search

Enable `enableSearchTool:true` to expose `unified_memory_search` on the same scoped API. See `docs/UNIFIED_MEMORY_HOST_INTEGRATION.md` for tool parameters, pagination, provenance and matching-package deployment requirements. The automatic context remains enabled and independent of explicit tool results.

`captureProjectIds` optionally supplies candidate source projects separately from `projectIds` used by recall. It defaults to `projectIds` for compatibility. Use a deliberate registered source-project set while allowing unscoped recall to use KW project resolution; do not force every query into the synthetic pilot project or suppress global user preferences. Unknown project hints remain unresolved, not silently reassigned.

The same tool optionally accepts `include_source_details:true` for exact, scoped source revisions over KW GET, capped at8 parts and the shared total deadline/budget. `source_details_complete`/`detail_status` explicitly distinguish full, expired, partial and unavailable originals. It never reads native host files or revives expired bodies. See the v2 host integration document for behavior and limits.

### Failed model requests
The native loader checkpoints the durable session prefix before each model step as well as at normal turn stopping. A completed tool result is therefore in the sanitized local outbox before the following model request can fail. These checkpoints overlap and preserve native identities; replay does not create new events. A process killed before the next checkpoint may still have an uncaptured native-log tail; retain the native log and resume/replay it, rather than claiming synchronous per-event capture.
