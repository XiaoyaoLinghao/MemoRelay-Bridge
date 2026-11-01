#!/usr/bin/env bash
# v1.6.1 code-review fix regressions. Structural assertions over archive-engine.sh
# (same idiom as the other DMA tests: grep for required logic; non-zero exit on fail).
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENG="$SCRIPT_DIR/../scripts/archive-engine.sh"
ok=0
chk() { if eval "$2"; then echo "OK  : $1"; else echo "FAIL: $1"; ok=1; fi; }

# #1 corrupt sidecar must be skipped, and reconcile must not abort archiving
chk "#1 sidecar JSON validated before use" \
   "grep -q 'jq -e .type==.array.. \"\$sc\"' '$ENG' || grep -q \"jq -e 'type==\\\"array\\\"' \\\"\\\$sc\\\"\" '$ENG'"
chk "#1 do_reconcile guarded in do_archive (|| log)" \
   "grep -q 'do_reconcile || log' '$ENG'"

# #2 reconcile in-loop marker matcher is prefix (matches the awk pre-check)
chk "#2 loop matches '### 原始细节(待补)'* (prefix, not exact)" \
   "grep -q '原始细节(待补)\"\\*' '$ENG'"

# #3 only a real '## HH:MM' header ends a block (awk + loop), not bare '## '
chk "#3 awk pre-check uses time-slot regex" \
   "grep -q 'inb && /\\^## \\[0-9\\]\\[0-9\\]:\\[0-9\\]\\[0-9\\]/' '$ENG'"
chk "#3 loop boundary uses time-slot regex" \
   "grep -q '\\[\\[ \"\\\$line\" =~ \\^##.*\\[0-9\\]\\[0-9\\]:\\[0-9\\]\\[0-9\\] \\]\\]' '$ENG'"

# #4 disabled-cloud writes FINAL '### 原始细节' (no 待补) and no sidecar in that branch
chk "#4 disabled-cloud branch writes final 原始细节 (no 待补)" \
   "awk '/cloud_summarizer 禁用 .* 写.最终.原始细节/{f=1} f&&/原始细节\\(待补\\)/{bad=1} END{exit bad}' '$ENG'"

# #5 sentinel gate only fires on short blocks (length guard)
chk "#5 sentinel gate has cloud_block length guard" \
   "grep -q '\\\${#cloud_block}\" -lt' '$ENG'"

# #6 both do_archive and do_reconcile inject the lexicon
chk "#6 inject_lexicon helper exists" "grep -q '^inject_lexicon()' '$ENG'"
chk "#6 do_reconcile calls inject_lexicon" \
   "awk '/^do_reconcile\\(\\)/{f=1} f&&/inject_lexicon/{c=1} /^do_archive\\(\\)/{f=0} END{exit !c}' '$ENG'"

[ "$ok" = 0 ] && echo "ALL v1.6.1 fix checks PASS" || echo "SOME v1.6.1 fix checks FAILED"
exit "$ok"
