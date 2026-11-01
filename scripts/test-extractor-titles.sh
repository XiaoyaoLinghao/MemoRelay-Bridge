#!/usr/bin/env bash
# 验证 local-extractor 输出的分类标题与 KW SPEC v1.0 §4 一致
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCAL_EXTRACTOR="$SCRIPT_DIR/extractors/local-extractor.sh"

EXPECTED=(
    "核心要点"
    "决策与结论"
    "已完成事项"
    "待办与计划"
    "用户偏好与习惯"
    "技术/项目要点"
    "风险与注意事项"
    "关键讨论"
)

# 构造最小输入触发提取（任意 1 条消息即可，因为 emit_section 总会被调用）
input='[{"role":"user","content":"test"}]'
output=$(echo "$input" | bash "$LOCAL_EXTRACTOR" 5 2>/dev/null)

missing=0
for title in "${EXPECTED[@]}"; do
    if ! echo "$output" | grep -qF "**${title}**"; then
        echo "FAIL: missing emit_section title: $title" >&2
        missing=$((missing + 1))
    fi
done

if [ "$missing" -gt 0 ]; then
    echo "FAIL: $missing titles missing from local-extractor output" >&2
    exit 1
fi
echo "OK: all 8 expected section titles found"
