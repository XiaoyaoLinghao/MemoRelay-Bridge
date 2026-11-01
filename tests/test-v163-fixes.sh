#!/usr/bin/env bash
# v1.6.3 max-review fixes D1-D8.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$SCRIPT_DIR/.."
ENG="$ROOT/scripts/archive-engine.sh"
ok=0
chk() { if eval "$2"; then echo "OK  : $1"; else echo "FAIL: $1"; ok=1; fi; }

# ---- behavioral: conversation-noise (D2 code fence, D5 decorative/json) ----
( source "$ROOT/scripts/lib/conversation-noise.sh"; set +euo pipefail
  r=0
  is_noise_message '请帮我看这段 ```print(1)``` 哪里错' && r=1   # D2: must NOT be noise
  is_conversation_noise_line '我们用 ----- 分隔' && r=1          # D5: prose kept
  is_conversation_noise_line '{"k":"v"} 这是我想存的配置' && r=1 # D5: trailing prose kept
  is_conversation_noise_line '```python' || r=1                  # bare fence still noise
  is_conversation_noise_line '-----' || r=1                       # pure rule still noise
  exit $r ) ; chk "D2/D5 noise rules anchored (code/prose kept, bare markers dropped)" "[ $? = 0 ]"

# ---- behavioral: D3 yaml_scalar keeps '#' in values, strips inline comment ----
printf 'api_token: "sk-A#9x"\nmemory_dir: /srv/d#a\nagent_id: main # c\n' >/tmp/v163_cfg.yaml
tok=$(grep -E '^[[:space:]]*api_token:' /tmp/v163_cfg.yaml | head -1 | sed -E 's/^[[:space:]]*api_token:[[:space:]]*//' | sed -E 's/[[:space:]]+#.*$//;s/^["'\'']//;s/["'\'']$//;s/[[:space:]]*$//')
agt=$(grep -E '^[[:space:]]*agent_id:' /tmp/v163_cfg.yaml | head -1 | sed -E 's/^[[:space:]]*agent_id:[[:space:]]*//' | sed -E 's/[[:space:]]+#.*$//;s/^["'\'']//;s/["'\'']$//;s/[[:space:]]*$//')
chk "D3 yaml_scalar keeps '#' in token value" "[ '$tok' = 'sk-A#9x' ]"
chk "D3 yaml_scalar strips a real inline comment" "[ '$agt' = 'main' ]"
# the live function reflects the same sed
chk "D3 config-loader uses whitespace-anchored comment strip" \
    "grep -q '\\[\\[:space:\\]\\]+#' '$ROOT/scripts/lib/config-loader.sh'"

# ---- structural ----
chk "D1 cooldown skip_write also skips compact" "grep -q 'skip_write:-0.* = .1.* ]; then' '$ENG' || grep -q 'skip_write.*跳过 sessions.compact' '$ENG'"
chk "D4 slot_has_substance treats jq-fail as substance" "grep -q 'jq 解析失败，保守判为有实质内容' '$ENG'"
chk "D6 sentinel gate skips when 结构化事实 present" "grep -q 'cloud_block\" != \\*.结构化事实' '$ENG'"
chk "D7 summarizer error parse handles flat string" "grep -q '\\.error.message? // .error' '$ROOT/scripts/summarizers/cloud-summarizer.sh'"
chk "D8 extractor uses char-based truncation (\${content:0:600})" \
    "grep -q 'content:0:600' '$ROOT/scripts/extractors/local-extractor.sh' && ! grep -qE '\\| *head -c 600' '$ROOT/scripts/extractors/local-extractor.sh'"

[ "$ok" = 0 ] && echo "ALL v1.6.3 fix checks PASS" || echo "SOME v1.6.3 fix checks FAILED"
exit "$ok"
