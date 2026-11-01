#!/usr/bin/env bash
# Wave 10 测试：纯噪声跳过 + 静默日标记 + 回归
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

ok=0
fail() { echo "FAIL: $*"; ok=1; }
pass() { echo "OK  : $*"; }

# 1. 语法
bash -n "$SCRIPT_DIR/archive-engine.sh" || fail "archive-engine syntax"
pass "syntax OK"

# 2. 结构断言 — Part A: noise 分支内有 checkpoint + compact
grep -q 'is_noise_message' "$SCRIPT_DIR/archive-engine.sh" || fail "conversation-noise not sourced"
pass "conversation-noise sourced"

awk '/if ! slot_has_substance/,/exit 0/' "$SCRIPT_DIR/archive-engine.sh" | \
    grep -q 'merge_checkpoint_bump_from_messages' || fail "noise branch missing checkpoint bump"
pass "checkpoint bump in noise branch"

awk '/if ! slot_has_substance/,/exit 0/' "$SCRIPT_DIR/archive-engine.sh" | \
    grep -q 'run_compact' || fail "noise branch missing run_compact"
pass "run_compact in noise branch"

# 3. 结构断言 — Part A 在 MIN_NEW_MESSAGES 之前
n_noise=$(grep -n 'slot_has_substance "$messages_all"' "$SCRIPT_DIR/archive-engine.sh" | head -1 | cut -d: -f1)
n_min=$(grep -n 'MIN_NEW_MESSAGES' "$SCRIPT_DIR/archive-engine.sh" | head -1 | cut -d: -f1)
if [ "$n_noise" -lt "$n_min" ]; then
    pass "noise check before MIN_NEW_MESSAGES (L$n_noise < L$n_min)"
else
    fail "noise check NOT before MIN_NEW_MESSAGES (L$n_noise >= L$n_min)"
fi

# 4. Dead code 已删
if grep -q '保底机制/阈值/force' "$SCRIPT_DIR/archive-engine.sh"; then
    fail "dead code still present"
else
    pass "dead code removed"
fi

# 5. Part B: finalize_empty_previous_day 在 do_archive 中被调用
grep -q 'finalize_empty_previous_day' "$SCRIPT_DIR/archive-engine.sh" || fail "finalize_empty_previous_day not found"
pass "finalize_empty_previous_day present"

# 6. Part B 行为测试：模拟跨日场景
WAVE_TEST_DIR=$(mktemp -d)
export DAILY_MEMORY_CONFIG_DIR="$WAVE_TEST_DIR/cfg"
export DAILY_MEMORY_MEMORY_DIR="$WAVE_TEST_DIR/mem"
mkdir -p "$DAILY_MEMORY_CONFIG_DIR" "$DAILY_MEMORY_MEMORY_DIR"

# 6a. source archive-engine 函数（不便完整跑 archive，load_config 会因缺 sessions.json 失败）
#     修复 W10.1-B 后 source-safe：BASH_SOURCE 守卫阻止了 main_cli 提前退出
source "$SCRIPT_DIR/archive-engine.sh" 2>/dev/null || true

# 模拟：昨天为活跃日，无文件 → 应生成标记
yesterday=$(date -d "yesterday" +%Y-%m-%d 2>/dev/null || date -r $(( $(date +%s) - 86400 )) +%Y-%m-%d)
echo "$yesterday" > "$DAILY_MEMORY_CONFIG_DIR/.last_active_day"
MEMORY_DIR="$DAILY_MEMORY_MEMORY_DIR" CONFIG_DIR="$DAILY_MEMORY_CONFIG_DIR" LOG_FILE=/dev/null \
    finalize_empty_previous_day
marker="$DAILY_MEMORY_MEMORY_DIR/${yesterday}.md"
if [ -f "$marker" ]; then
    slots=$(grep -c '^## ' "$marker" 2>/dev/null | tr -d '[:space:]' || echo 0)
    [ -z "$slots" ] && slots=0
    has_comment=$(grep -c '<!--' "$marker" 2>/dev/null | tr -d '[:space:]' || echo 0)
    [ -z "$has_comment" ] && has_comment=0
    if [ "$slots" -eq 0 ] && [ "$has_comment" -gt 0 ]; then
        pass "empty-day marker: 0 slots, HTML comment present"
    else
        fail "marker file format wrong (slots=$slots, comment=$has_comment)"
    fi
else
    fail "empty-day marker file not created"
fi

# 6b. 幂等：再次调，标记已存在不应重复
marker_mtime=$(stat -c %Y "$marker" 2>/dev/null || stat -f %m "$marker")
finalize_empty_previous_day 2>/dev/null || true
marker_mtime2=$(stat -c %Y "$marker" 2>/dev/null || stat -f %m "$marker")
if [ "$marker_mtime" = "$marker_mtime2" ]; then
    pass "idempotent: marker not re-created"
else
    fail "marker was re-created (not idempotent)"
fi

# 6c. .last_active_day 已更新为今天
today=$(date +%Y-%m-%d)
active_day=$(cat "$DAILY_MEMORY_CONFIG_DIR/.last_active_day")
if [ "$active_day" = "$today" ]; then
    pass ".last_active_day updated to today ($today)"
else
    fail ".last_active_day is $active_day, expected $today"
fi

rm -rf "$WAVE_TEST_DIR"

# 6d. W10.1-D 心跳噪声断言：slot_has_substance + is_noise_message
hb='[{"role":"user","content":"[OpenClaw heartbeat poll]"},{"role":"assistant","content":"Disk 37%，无 pending 子会话。\n\nHEARTBEAT_OK"}]'
real='[{"role":"user","content":"[OpenClaw heartbeat poll]"},{"role":"user","content":"帮我评审 extractor.py 的抽取逻辑"}]'
if slot_has_substance "$hb" 2>/dev/null; then fail "纯心跳槽被误判为有实质内容（W10.1-A 回归）"; else pass "纯心跳槽 -> 无实质（正确跳过）"; fi
if slot_has_substance "$real" 2>/dev/null; then pass "含真实 user 输入 -> 有实质（正确写入）"; else fail "真实内容被漏判"; fi
if is_noise_message "[OpenClaw heartbeat poll]" 2>/dev/null; then pass "[OpenClaw heartbeat poll] -> 噪声"; else fail "心跳标记未被识别为噪声"; fi

# 7. 回归测试（直接调用各回归脚本，不回调 self-check.sh 避免递归）
if [ -x "$SCRIPT_DIR/test-fail-guard.sh" ]; then
    bash "$SCRIPT_DIR/test-fail-guard.sh" --structural-only >/dev/null 2>&1 || fail "fail-guard regression"
    pass "fail-guard regression OK"
fi

if [ -x "$SCRIPT_DIR/test-output-format.sh" ]; then
    bash "$SCRIPT_DIR/test-output-format.sh" >/dev/null 2>&1 || fail "output-format regression"
    pass "output-format regression OK"
fi

if [ -x "$SCRIPT_DIR/test-extractor-titles.sh" ]; then
    bash "$SCRIPT_DIR/test-extractor-titles.sh" >/dev/null 2>&1 || fail "extractor-titles regression"
    pass "extractor-titles regression OK"
fi

exit "$ok"
