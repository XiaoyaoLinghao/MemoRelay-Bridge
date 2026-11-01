#!/usr/bin/env bash
# v1.6.2 code-review fixes #7-#10. Behavioral test for consolidate_structured_facts
# (sources the engine — main-guarded) + structural assertions.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENG="$SCRIPT_DIR/../scripts/archive-engine.sh"
SUM="$SCRIPT_DIR/../scripts/summarizers/cloud-summarizer.sh"
ok=0
chk() { if eval "$2"; then echo "OK  : $1"; else echo "FAIL: $1"; ok=1; fi; }

# ---- behavioral: consolidate_structured_facts (#7 merge, #8 strip-invalid) ----
source "$ENG"; set +euo pipefail 2>/dev/null   # only defines functions (main-guarded)

facts_of() { awk '/```json/{f=1;next}/```/{f=0;next}f'; }

# #7: two fences (chunk mode) -> one merged+deduped fence
two='### 结构化事实
```json
[{"type":"project","name":"KW","summary":"a"}]
```
### 结构化事实
```json
[{"type":"project","name":"KW","summary":"a"},{"type":"tech","name":"jq","summary":"b"}]
```'
m=$(consolidate_structured_facts "$two")
chk "#7 chunk fences merged into ONE block" "[ \"\$(grep -c '### 结构化事实' <<<\"\$m\")\" = 1 ]"
chk "#7 facts merged + deduped to 2" "[ \"\$(facts_of <<<\"\$m\" | jq 'length')\" = 2 ]"

# #8: invalid JSON fence stripped (not written)
bad='叙事
### 结构化事实
```json
[{"type": BROKEN
```'
mb=$(consolidate_structured_facts "$bad")
chk "#8 invalid facts fence stripped" "[ \"\$(grep -c '结构化事实' <<<\"\$mb\")\" = 0 ]"

# no fence -> unchanged
chk "#7/#8 no-fence block unchanged" "[ \"\$(consolidate_structured_facts '本时段无实质内容')\" = '本时段无实质内容' ]"

# ---- structural ----
chk "#9 render_raw_detail helper exists"          "grep -q '^render_raw_detail()' '$ENG'"
chk "#9 finalize_archive_bookkeeping helper exists" "grep -q '^finalize_archive_bookkeeping()' '$ENG'"
chk "#9 >=3 finalize_archive_bookkeeping call sites" \
    "[ \"\$(grep -c '^[[:space:]]*finalize_archive_bookkeeping\$' '$ENG')\" -ge 3 ]"
chk "#7/#8 consolidate_structured_facts called in success path" \
    "grep -q 'cloud_block=\$(consolidate_structured_facts' '$ENG'"
chk "#10 gate uses stable [空时段] token"           "grep -q \"cloud_block.* == .*\\[空时段\\]\" '$ENG'"
chk "#10 gate recognizes all tag types (not just 关键)" \
    "grep -q 'grep -qE .\\^\\\\\\[(关键|已完成|待办|创意)' '$ENG'"
chk "#10 summarizer instructs [空时段] token"        "grep -q '\\[空时段\\]' '$SUM'"

[ "$ok" = 0 ] && echo "ALL v1.6.2 fix checks PASS" || echo "SOME v1.6.2 fix checks FAILED"
exit "$ok"
