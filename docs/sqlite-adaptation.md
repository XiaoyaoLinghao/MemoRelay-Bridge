# Native session storage adaptation — 1.7.0

Status: local implementation candidate; no upstream publication or production acceptance.
Accepted upstream base: `5a9c363cdf96b38ba2a8402c2848fc93babbbe0d` (1.6.4).

## Runnable scenarios and scope

1. Archive selected legacy JSONL sessions, then rerun with no duplicate output.
2. Archive selected SQLite sessions without requiring or exporting `sessions.json`/JSONL.
3. Carry the incremental checkpoint across JSONL-to-SQLite migration; handle equal
   timestamps and appended messages without losing text at the storage boundary.
4. Reject an unsupported schema, corrupt selected transcript or failed discovery
   before writing memory, advancing checkpoints or compacting sessions.

Non-goals: modifying OpenClaw databases or its migration process; changing the
Markdown/structured-facts contract, summary/noise policy or cloud API; installing
or deploying to an actual OpenClaw host; publishing a tag or GitHub release.

## Frozen integration contract

`scripts/session-store.py snapshot --agent ID --home PATH --legacy-store PATH
--checkpoint PATH --keys-json JSON` is the single storage adapter. Python 3.9+
and its standard-library `sqlite3` replace direct session-file reads in the
archive entry point. There are no third-party Python dependencies.

It writes one JSON object to stdout: `backend`, `sessions` (key and token usage),
`count`, `data` (sk, ts, role, content), `stripped` (role, content), and a proposed
`checkpoint` map. Diagnostics go to stderr; errors exit nonzero without partial
JSON. Source rows, schema and main-database content are read-only. No compatibility
exports are generated. SQLite `mode=ro` plus `query_only` and a read transaction
provide a consistent live snapshot. SQLite itself may create/update transient
WAL/SHM reader-coordination files, even for a read-only connection; this is not
a directory-byte-immutability guarantee. Do not use `immutable=1` on a live
database or infer immutability from momentary absence of WAL files. A directory
that cannot support SQLite's required coordination fails closed.

The adapter's `list --agent ID --home PATH --legacy-store PATH` command shares
discovery and schema validation, returns `{agent_id,count,sessions}` without
reading transcripts, and powers the public interactive `list-sessions` command.
Interactive `archive` propagates a failed engine exit as `ok:false` and nonzero.

The reader retains the old merged-JSONL broad marker/text filter, and records
filtered identities as consumed in its proposal. The shell retains the existing
second-stage `slot_has_substance` decision; this split preserves the old policy.
The shell engine owns triggers, substance decisions, summaries, memory
output, locking and checkpoint publication. It consumes one immutable snapshot
per run and publishes the proposed checkpoint only after accepted output or an
intentional noise-only discard. A deferred batch must never trigger compaction.
Storage discovery must succeed before reconciliation or empty-day output.

The reader owns discovery, schema checks, transcript normalization and identities.
The checkpoint file remains DMA-owned. Version 2 cursors record consumed event
identities per session key, independent of source backend; this catches late or
equal-timestamp messages. Identity history is deliberately retained, trading
checkpoint size for migration/rotation deduplication. Do not prune it casually.
This version scans all retained history for the selected keys before evaluating
the archive trigger. "Incremental" describes output selection/deduplication,
not incremental database I/O. This intentionally keeps a single consistent
snapshot and early validation; low-usage polls still incur O(selected history)
read cost. Large production-store performance is not yet benchmarked.

Legacy timestamp cursors can only establish what the old archiver recorded:
messages at or before that timestamp are treated as consumed on initial upgrade.
An old timestamp alone cannot prove whether a same-timestamp event arrived late;
this pre-existing ambiguity cannot be reconstructed automatically.

Implementation ownership: main agent owns shell integration and documentation;
session-store owner owns the Python adapter; test owner owns new regression tests.
An independent reviewer reviews the complete base-to-candidate diff.

## Verification boundaries

Use generated fixtures explicitly labelled as such. Cover equivalent stores,
long text, repeat runs, equal timestamps, migration, rollover, selected scope,
unknown schemas, corruption and source preservation. Shell checks run under
Bash with cloud and compaction disabled against isolated data. Actual OpenClaw
runtime, real migrated user data and live cloud calls remain external evidence.

Markdown append and checkpoint replacement retain the upstream two-file commit
boundary: a process/power failure between them can replay a batch. This change
does not claim crash-atomic exactly-once delivery.
