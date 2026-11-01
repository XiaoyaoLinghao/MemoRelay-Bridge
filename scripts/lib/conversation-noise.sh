#!/usr/bin/env bash
# 判断单行对话是否更像噪声，供提取器和摘要器过滤
# 支持：内置规则 + 用户自定义规则 + 上下文感知（区分系统消息和用户讨论
set -euo pipefail

# 用户自定义噪声模式（全局，外部加载
_CUSTOM_NOISE_PATTERNS=()

# 加载自定义噪声模式（从 config.yaml 或直接传入
load_custom_noise_patterns() {
    local config_dir="$1"
    local config_file="$config_dir/config.yaml"
    if [ -f "$config_file" ]; then
        # 临时 source config-loader 以读取 yaml
        local SCRIPT_DIR
        SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
        local patterns
        patterns=$(awk '
          /^noise_filter:/ { ins=1; next }
          ins && /^[a-z][a-z0-9_]*:/ && !/^noise_filter/ { ins=0 }
          ins && /^[[:space:]]+custom_patterns:/ { inlist=1; next }
          inlist && /^[[:space:]]+-[[:space:]]/ {
              line=$0
              sub(/^[[:space:]]+-[[:space:]]+/, "", line)
              gsub(/^["'\'']|["'\'']$/, "", line)
              if (line != "" && line !~ /^#/ && line !~ /^[[:space:]]*$/) {
                  sub(/[[:space:]]+#.*$/, "", line)
                  print line
              }
              next
          }
          inlist && /^[[:space:]]+[a-zA-Z_][a-zA-Z0-9_.-]*:/ && !/^([[:space:]]+-)/ { inlist=0 }
        ' "$config_file")
        while IFS= read -r p; do
            if [ -n "$p" ]; then
                _CUSTOM_NOISE_PATTERNS+=("$p")
            fi
        done <<<"$patterns"
    fi
}

# ========== 上下文感知：判断是否是真正的系统 heartbeat 而非用户讨论 ==========
# 系统 heartbeat 特征：
# 1. 极短（< 80 字符）
# 2. 格式固定（[heartbeat] xxx / heartbeat check）
# 3. 没有实际讨论内容（不含"讨论"、"问题"、"解决"、"配置"等人类词语
_is_system_heartbeat() {
    local s="$1"

    # 先确保包含 heartbeat
    [[ "$s" != *'heartbeat'* && "$s" != *'HEARTBEAT'* ]] && return 1

    # 长度检查：系统 heartbeat 通常很短
    if [ ${#s} -gt 80 ]; then
        # 较长 → 可能是用户在讨论 heartbeat，不视为噪声
        return 1
    fi

    # 检查是否包含"人类讨论"的关键词
    local discussion_words="讨论 问题 解决 配置 设置 太频繁 调整 关闭 打开
                            为什么 怎么 什么 能否 建议 优化 bug 修复 取消 启用
                            用户 助手 对话 记忆 归档 过滤 规则 模式"

    local lc
    lc=$(echo "$s" | tr '[:upper:]' '[:lower:]')
    for w in $discussion_words; do
        if [[ "$lc" == *"$w"* ]]; then
            # 包含讨论关键词 → 用户在谈 heartbeat，不视为噪声
            return 1
        fi
    done

    # 检查系统 heartbeat 典型格式
    if [[ "$s" == *'[heartbeat'* ]] || \
       [[ "$s" == '[heartbeat]'* ]] || \
       [[ "$s" == *'heartbeat check'* ]] || \
       [[ "$s" == *'heartbeat poll'* ]] || \
       [[ "$s" == *'[OpenClaw heartbeat'* ]] || \
       [[ "$s" == *'HEARTBEAT'* ]]; then
        return 0  # 是系统 heartbeat → 噪声
    fi

    return 1
}

# ========== 上下文感知：判断是否是系统工具调用而非用户讨论工具 ==========
_is_system_tool_call() {
    local s="$1"

    # 长度检查：系统工具消息通常较短
    if [ ${#s} -gt 120 ]; then
        return 1  # 较长 → 用户在讨论工具
    fi

    # 检查是否包含讨论关键词
    local lc
    lc=$(echo "$s" | tr '[:upper:]' '[:lower:]')
    local discussion_words="讨论 问题 解决 配置 设置 怎么 为什么 什么 能否 建议
                            优化 bug 修复 调用 函数 方法 失败 成功 权限 路径 目录"
    for w in $discussion_words; do
        [[ "$lc" == *"$w"* ]] && return 1
    done

    # 系统工具调用典型格式
    [[ "$s" == *'[tool'* ]] && return 0
    [[ "$s" == *'"toolCall"'* ]] && return 0
    [[ "$s" == *'tool_call_id'* ]] && return 0
    [[ "$s" == *'[调用工具'* ]] && return 0
    [[ "$s" == *'使用工具'* ]] && return 0

    return 1
}

# ========== 单行噪声检测（包含自定义模式 ==========
is_conversation_noise_line() {
    local s="$1"
    local lc
    lc=$(echo "$s" | tr '[:upper:]' '[:lower:]')

    # ===== Fix 1a: 廉价前闸 — 早段精确匹配 =====
    # OpenClaw heartbeat poll 精确识别
    [[ "$s" == *'[OpenClaw heartbeat poll]'* ]] && return 0

    # 网关重启续接通知
    [[ "$lc" == *'previous turn was interrupted by a gateway restart'* ]] && return 0

    # 代码块【裸围栏行】视为噪声（``` 或 ```lang 单独成行）；含正文的行保留
    # (D2: 单行的代码提问如 "看这段 ```print(1)```" 不再被整条丢弃)
    case "${s//[[:space:]]/}" in '```'*) [ "${#s}" -le 16 ] && return 0 ;; esac

    # ⚡ 上下文感知 heartbeat（优先于通用规则
    _is_system_heartbeat "$s" && return 0

    # ⚡ 上下文感知工具调用
    _is_system_tool_call "$s" && return 0

    # 系统消息 / Sender 相关（Fix 1a: 大小写不敏感，覆盖 (System)/(SYSTEM)/(system)）
    [[ "$s" == *'Sender (untrusted'* ]] && return 0
    [[ "$lc" == *'[system'* ]] && return 0
    [[ "$lc" == *'(system'* ]] && return 0
    [[ "$lc" == 'system prompt'* ]] && return 0

    # MCP / 服务相关
    [[ "$s" == *'[MCP'* ]] && return 0
    [[ "$s" == *'mcp_'* ]] && return 0
    [[ "$s" == *'MCP server'* ]] && return 0

    # Claude Code / 助手自身输出
    [[ "$s" == *'[Spinner'* ]] && return 0
    [[ "$s" == *'[spin'* ]] && return 0
    [[ "$s" == *'Running...'* && ${#s} -lt 20 ]] && return 0

    # 函数调用标记
    [[ "$s" == *'<<<'* ]] && return 0  # <<< 函数调用开始
    [[ "$s" == *'>>>'* ]] && return 0  # >>> 函数调用结束
    [[ "$s" == *'Function invocation'* ]] && return 0

    # 整行就是一个 JSON 对象（工具参数/返回值）才算噪声；D5: 含正文的行如
    # '{"k":"v"} 这是我想存的配置' 以正文结尾、不算噪声
    [[ "$s" =~ ^[[:space:]]*\{\".*\}[[:space:]]*$ ]] && return 0

    # 空行或几乎空行
    local trimmed
    trimmed=$(echo "$s" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    [[ -z "$trimmed" || ${#trimmed} -lt 4 ]] && return 0

    # 整行【只是】装饰分隔线才算噪声；D5: 正文中提到 '-----' 等不再误判
    [[ "$s" =~ ^[[:space:]]*[-=_]{4,}[[:space:]]*$ ]] && return 0

    # 无意义的系统状态行
    [[ "$s" == *'[INFO]'* && ${#s} -lt 50 ]] && return 0
    [[ "$s" == *'[DEBUG]'* && ${#s} -lt 50 ]] && return 0

    # 短装饰行检查首字符
    if [ ${#s} -lt 10 ]; then
        local first=${s:0:1}
        [[ "$first" == '#' || "$first" == '*' || "$first" == '-' ]] && return 0
    fi

    # ========== 用户自定义噪声模式（最后检查 ==========
    local p
    for p in "${_CUSTOM_NOISE_PATTERNS[@]}"; do
        [[ "$s" == *"$p"* ]] && return 0
    done

    # 不是噪声
    return 1
}

# ========== 整条消息是否应该被完全跳过 ==========
is_noise_message() {
    local content="$1"
    local line_count=$(echo "$content" | wc -l)

    # 单行消息：严格检查
    if [ "$line_count" -eq 1 ]; then
        is_conversation_noise_line "$content" && return 0
    fi

    # 统计噪声行占比超过 60% 以上判定为噪声消息
    local noise_count=0 total_count=0
    while IFS= read -r line; do
        total_count=$((total_count + 1))
        if is_conversation_noise_line "$line"; then
            noise_count=$((noise_count + 1))
        fi
    done <<<"$content"

    if [ "$total_count" -gt 0 ] && [ $(( noise_count * 10 / total_count )) -ge 6 ]; then
        return 0  # 是噪声
    fi

    return 1  # 不是噪声
}
