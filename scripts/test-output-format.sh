#!/usr/bin/env bash
# 验证 archive-engine.sh 写出的 memory 文件符合 KW SPEC v1.1
# 用法：bash scripts/test-output-format.sh [path/to/memory.md]
#      无路径参数时：从 stdin 读取管道输入；无管道/输入为空时回退到内置样例。
set -euo pipefail

if [ $# -ge 1 ] && [ -f "$1" ]; then
    output=$(cat "$1")
else
    # 优先读取管道输入（stdin 非终端时）；为空则回退到内置样例段，
    # 保证 self-check / wave10 等无参调用在 cron/重定向(/dev/null) 下仍走样例。
    output=""
    if [ ! -t 0 ]; then
        output=$(cat)
    fi
    if [ -z "$output" ]; then
        # 模拟一段 archive-engine.sh 写入产生的 markdown 段
        output=$(cat <<'EOF'

## 14:30

### 原始细节

**核心要点**
- 09:30 - 用户：决定采用 Python

### 摘要

今天用户决定采用 Python 后端。

[关键决策] 后端：Python FastAPI

EOF
)
    fi
fi

fail=0

# 验证 1: H3 原始细节 子分区存在
echo "$output" | grep -qE '^### 原始细节$' || { echo "FAIL: missing ### 原始细节"; fail=$((fail+1)); }

# 验证 2: H3 摘要 子分区存在
echo "$output" | grep -qE '^### 摘要$' || { echo "FAIL: missing ### 摘要"; fail=$((fail+1)); }

# 验证 3: 至少一个 tag 行格式
echo "$output" | grep -qE '^\[(关键决策|关键偏好|关键事实|关键风险|关键技术|已完成|待办|创意|关键讨论)\] ' \
    || { echo "FAIL: no valid tag line found"; fail=$((fail+1)); }

# 验证 4: tag 行前面没有 - 前缀
bad_tags=$(echo "$output" | grep -cE '^- \[(关键决策|关键偏好|关键事实|关键风险|关键技术|已完成|待办|创意|关键讨论)\]' || true)
[ "$bad_tags" -eq 0 ] || { echo "FAIL: $bad_tags tag lines with - prefix"; fail=$((fail+1)); }

# 验证 5: 摘要段不应有 ** 标记
summary_block=$(echo "$output" | awk '/^### 摘要$/,/^### |^## /' | head -n -1)
if echo "$summary_block" | grep -qE '\*\*'; then
    echo "FAIL: ### 摘要 contains ** markers (should be tag-only)"
    fail=$((fail+1))
fi

# 验证 6: 没有 "- 无" 占位（v1.1 cloud 段不允许）
in_summary_no_count=$(echo "$summary_block" | grep -cE '^- 无$' || true)
[ "$in_summary_no_count" -eq 0 ] || { echo "FAIL: ### 摘要 contains - 无 placeholder"; fail=$((fail+1)); }

if [ "$fail" -eq 0 ]; then
    echo "OK: output format matches KW SPEC v1.1"
    exit 0
else
    echo "FAIL: $fail format violations"
    exit 1
fi
