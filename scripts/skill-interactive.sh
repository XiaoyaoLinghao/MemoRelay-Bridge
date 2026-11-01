#!/usr/bin/env bash
# Daily Memory Archiver - OpenClaw Skill 自然语言交互入口
# 供 OpenClaw 通过对话方式查询、配置、操作归档功能
# 输出格式：JSON 便于 LLM 解析后用自然语言回答用户

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CONFIG_DIR="${DAILY_MEMORY_CONFIG_DIR:-$SKILL_ROOT/config}"
export CONFIG_DIR
CONFIG_FILE="$CONFIG_DIR/config.yaml"
OPENCLAW_HOME="${OPENCLAW_HOME:-$HOME/.openclaw}"

# shellcheck source=lib/credentials-store.sh
source "$SCRIPT_DIR/lib/credentials-store.sh"
# shellcheck source=lib/config-loader.sh
source "$SCRIPT_DIR/lib/config-loader.sh"

# ============================================================================
# 子命令列表
# ============================================================================
case "${1:-help}" in
    # ------------------------------------------------------------------------
    # 状态查询：返回完整的配置状态 JSON
    # 用法：scripts/skill-interactive.sh status
    # ------------------------------------------------------------------------
    status)
        config_get_status_json "$CONFIG_FILE"
        ;;

    # ------------------------------------------------------------------------
    # 列出当前 agent 下的所有 session keys
    # 用法：scripts/skill-interactive.sh list-sessions [agent_id]
    # ------------------------------------------------------------------------
    list-sessions)
        agent_id="${2:-main}"
        sessions_json="${SESSIONS_JSON:-$OPENCLAW_HOME/agents/$agent_id/sessions/sessions.json}"
        if ! python3 "$SCRIPT_DIR/session-store.py" list --agent "$agent_id" \
            --home "$OPENCLAW_HOME" --legacy-store "$sessions_json"; then
            jq -n --arg agent "$agent_id" '{error: "session store read failed", agent_id: $agent}'
            exit 1
        fi
        ;;

    # ------------------------------------------------------------------------
    # 添加 session key 到 merge_jsonl_keys
    # 用法：scripts/skill-interactive.sh add-key <session_key>
    # ------------------------------------------------------------------------
    add-key)
        key="${2:?missing session_key}"
        if ! bash "$SCRIPT_DIR/config-manager.sh" merge-jsonl-keys-add "$key" 2>&1; then
            jq -n --arg k "$key" '{error: "failed to add key", key: $k}'
            exit 1
        fi
        jq -n --arg k "$key" '{ok: true, action: "added", key: $k}'
        ;;

    # ------------------------------------------------------------------------
    # 移除 session key 从 merge_jsonl_keys
    # 用法：scripts/skill-interactive.sh remove-key <session_key>
    # ------------------------------------------------------------------------
    remove-key)
        key="${2:?missing session_key}"
        # 读取现有列表，过滤后重写
        preserved_m=() m=""
        while IFS= read -r line; do
            [ -n "$line" ] && [ "$line" != "$key" ] && preserved_m+=("$line")
        done < <(yaml_merge_keys "$CONFIG_FILE")

        # 调用 config-manager 中的重写逻辑
        # 这里我们直接复用 merge_jsonl_keys_rewrite_block
        if grep -qE '^[[:space:]]+merge_jsonl_keys:' "$CONFIG_FILE" 2>/dev/null; then
            # 直接在本文件中实现重写逻辑（避免 source config-manager.sh 依赖问题）
            tmp=$(mktemp)
            in_session=0
            in_merge=0
            while IFS= read -r line || [ -n "$line" ]; do
                if [[ "$line" == "session:" ]]; then
                    in_session=1
                    in_merge=0
                    printf '%s\n' "$line"
                    continue
                fi
                if [ "$in_session" = 1 ] && [[ "$line" =~ ^[a-zA-Z_][a-zA-Z0-9_]*: ]] && [[ ! "$line" =~ ^[[:space:]] ]]; then
                    in_session=0
                    in_merge=0
                    printf '%s\n' "$line"
                    continue
                fi
                if [ "$in_session" = 1 ] && [[ "$line" =~ ^[[:space:]]+merge_jsonl_keys: ]]; then
                    in_merge=1
                    echo "  merge_jsonl_keys:"
                    if [ ${#preserved_m[@]} -gt 0 ]; then
                        for k in "${preserved_m[@]}"; do
                            printf "    - \"%s\"\n" "$k"
                        done
                    else
                        echo "  merge_jsonl_keys: []"
                    fi
                    continue
                fi
                if [ "$in_merge" = 1 ]; then
                    if [[ "$line" =~ ^[[:space:]]+-[[:space:]] ]]; then
                        continue
                    fi
                    if [[ "$line" =~ ^[a-zA-Z_][a-zA-Z0-9_]*: ]] && [[ ! "$line" =~ ^[[:space:]] ]]; then
                        in_merge=0
                        in_session=0
                        printf '%s\n' "$line"
                        continue
                    fi
                    in_merge=0
                fi
                printf '%s\n' "$line"
            done < "$CONFIG_FILE" > "$tmp"
            mv "$tmp" "$CONFIG_FILE"
            jq -n --arg k "$key" '{ok: true, action: "removed", key: $k}'
        else
            jq -n --arg k "$key" '{error: "merge_jsonl_keys not found in config", key: $k}'
            exit 1
        fi
        ;;

    # ------------------------------------------------------------------------
    # 设置 API 凭证（从 stdin 读取 JSON {"api_url":"...","api_token":"...","model":"..."}）
    # 用法：cat creds.json | scripts/skill-interactive.sh set-credentials
    # ------------------------------------------------------------------------
    set-credentials)
        bash "$SCRIPT_DIR/config-manager.sh" save-json
        jq -n '{ok: true, action: "credentials saved and encrypted"}'
        ;;

    # ------------------------------------------------------------------------
    # 测试云端 API 连接
    # 用法：scripts/skill-interactive.sh test-api
    # ------------------------------------------------------------------------
    test-api)
        if [ ! -f "$CONFIG_DIR/credentials.enc" ]; then
            jq -n '{error: "credentials.enc not found"}'
            exit 1
        fi
        if ! raw=$(credentials_decrypt_raw 2>/dev/null); then
            jq -n '{error: "failed to decrypt credentials"}'
            exit 1
        fi
        api_url=$(echo "$raw" | jq -r '.api_url')
        api_tok=$(echo "$raw" | jq -r '.api_token')

        # 尝试调用 chat completions
        resp=$(curl -sS --max-time 10 -X POST "${api_url%/}/chat/completions" \
            -H "Authorization: Bearer $api_tok" \
            -H "Content-Type: application/json" \
            -d '{"model":"test","messages":[],"max_tokens":1}' 2>&1 || true)

        if echo "$resp" | jq -e '.error' >/dev/null 2>&1; then
            err_msg=$(echo "$resp" | jq -r '.error.message // .error // "unknown error"')
            jq -n --arg e "$err_msg" '{api_test: false, error: $e}'
        else
            jq -n '{api_test: true, note: "connection successful (may return error for invalid model, that is expected)"}'
        fi
        ;;

    # ------------------------------------------------------------------------
    # 运行归档（带可选 force）
    # 用法：scripts/skill-interactive.sh archive [force]
    # ------------------------------------------------------------------------
    archive)
        force_arg=""
        if [ "${2:-}" = "force" ]; then
            force_arg="--force"
        fi
        # 捕获输出但仍显示日志
        log_output=""
        archive_status=0
        log_output=$(bash "$SCRIPT_DIR/archive-engine.sh" archive $force_arg 2>&1) || archive_status=$?
        echo "$log_output" >&2
        # 返回 JSON 结果
        jq -n --arg force "${2:-normal}" --arg log "$log_output" --argjson status "$archive_status" '{
            ok: ($status == 0),
            exit_code: $status,
            mode: $force,
            log_preview: ($log | split("\n") | .[-5:] | join("\n"))
        }'
        exit "$archive_status"
        ;;

    # ------------------------------------------------------------------------
    # 列出配置建议/警告
    # 用法：scripts/skill-interactive.sh suggestions
    # ------------------------------------------------------------------------
    suggestions)
        warnings=$(config_get_warnings "$CONFIG_FILE" | jq -R . | jq -s .)
        tips='[
            "如果对话量较少但仍希望定期归档，可设置 periodic_archive_minutes = 120",
            "建议至少配置 2 个以上 session key 进行多路合并",
            "首次使用建议运行 archive --force 生成初始记忆",
            "cron 建议每 30 分钟运行一次：*/30 * * * * path/to/bin/daily-memory-archiver archive"
        ]'
        jq -n --argjson warnings "$warnings" --argjson tips "$tips" '{
            warnings: $warnings,
            tips: $tips
        }'
        ;;

    # ------------------------------------------------------------------------
    # 列出所有自定义噪声过滤规则
    # 用法：scripts/skill-interactive.sh list-noise-patterns
    # ------------------------------------------------------------------------
    list-noise-patterns)
        patterns=$(yaml_custom_noise_patterns "$CONFIG_FILE" | jq -R . | jq -s .)
        jq -n --argjson patterns "$patterns" '{
            count: ($patterns | length),
            patterns: $patterns
        }'
        ;;

    # ------------------------------------------------------------------------
    # 添加自定义噪声过滤规则
    # 用法：scripts/skill-interactive.sh add-noise-pattern <pattern>
    # ------------------------------------------------------------------------
    add-noise-pattern)
        pattern="${2:?missing noise pattern}"
        # 检查是否已存在
        if yaml_custom_noise_patterns "$CONFIG_FILE" | grep -qxF "$pattern" 2>/dev/null; then
            jq -n --arg p "$pattern" '{error: "pattern already exists", pattern: $p}'
            exit 1
        fi
        # 追加到配置文件
        if grep -qE '^[[:space:]]+custom_patterns:' "$CONFIG_FILE" 2>/dev/null; then
            # custom_patterns 已存在，追加
            tmp=$(mktemp)
            in_noise=0
            in_custom=0
            while IFS= read -r line || [ -n "$line" ]; do
                if [[ "$line" == "noise_filter:" ]]; then
                    in_noise=1
                    in_custom=0
                    printf '%s\n' "$line"
                    continue
                fi
                if [ "$in_noise" = 1 ] && [[ "$line" =~ ^[a-zA-Z_][a-zA-Z0-9_]*: ]] && [[ ! "$line" =~ ^[[:space:]] ]]; then
                    in_noise=0
                    in_custom=0
                    printf '%s\n' "$line"
                    continue
                fi
                if [ "$in_noise" = 1 ] && [[ "$line" =~ ^[[:space:]]+custom_patterns: ]]; then
                    in_custom=1
                    if [[ "$line" == *"[]"* ]]; then
                        # 是空数组，转换为列表格式
                        echo "  custom_patterns:"
                    else
                        printf '%s\n' "$line"
                    fi
                    printf "    - \"%s\"\n" "$pattern"
                    continue
                fi
                if [ "$in_custom" = 1 ]; then
                    if [[ "$line" =~ ^[[:space:]]+-[[:space:]] ]]; then
                        printf '%s\n' "$line"
                        continue
                    fi
                    if [[ "$line" =~ ^[a-zA-Z_][a-zA-Z0-9_]*: ]] && [[ ! "$line" =~ ^[[:space:]] ]]; then
                        in_custom=0
                        in_noise=0
                        printf '%s\n' "$line"
                        continue
                    fi
                    in_custom=0
                fi
                printf '%s\n' "$line"
            done < "$CONFIG_FILE" > "$tmp"
            mv "$tmp" "$CONFIG_FILE"
        else
            # custom_patterns 不存在，创建 noise_filter 段
            if ! grep -qE '^noise_filter:' "$CONFIG_FILE" 2>/dev/null; then
                echo "" >> "$CONFIG_FILE"
                echo "noise_filter:" >> "$CONFIG_FILE"
                echo "  custom_patterns:" >> "$CONFIG_FILE"
                echo "    - \"$pattern\"" >> "$CONFIG_FILE"
            else
                # noise_filter 存在但无 custom_patterns
                tmp=$(mktemp)
                in_noise=0
                while IFS= read -r line || [ -n "$line" ]; do
                    if [[ "$line" == "noise_filter:" ]]; then
                        in_noise=1
                        printf '%s\n' "$line"
                        echo "  custom_patterns:"
                        printf "    - \"%s\"\n" "$pattern"
                        continue
                    fi
                    if [ "$in_noise" = 1 ] && [[ "$line" =~ ^[a-zA-Z_][a-zA-Z0-9_]*: ]] && [[ ! "$line" =~ ^[[:space:]] ]]; then
                        in_noise=0
                        printf '%s\n' "$line"
                        continue
                    fi
                    printf '%s\n' "$line"
                done < "$CONFIG_FILE" > "$tmp"
                mv "$tmp" "$CONFIG_FILE"
            fi
        fi
        jq -n --arg p "$pattern" '{ok: true, action: "added", pattern: $p}'
        ;;

    # ------------------------------------------------------------------------
    # 移除自定义噪声过滤规则
    # 用法：scripts/skill-interactive.sh remove-noise-pattern <pattern>
    # ------------------------------------------------------------------------
    remove-noise-pattern)
        pattern="${2:?missing noise pattern}"
        # 读取现有列表，过滤后重写
        preserved=()
        while IFS= read -r line; do
            [ -n "$line" ] && [ "$line" != "$pattern" ] && preserved+=("$line")
        done < <(yaml_custom_noise_patterns "$CONFIG_FILE")

        tmp=$(mktemp)
        found=0
        in_noise=0
        in_custom=0
        while IFS= read -r line || [ -n "$line" ]; do
            if [[ "$line" == "noise_filter:" ]]; then
                in_noise=1
                in_custom=0
                printf '%s\n' "$line"
                continue
            fi
            if [ "$in_noise" = 1 ] && [[ "$line" =~ ^[a-zA-Z_][a-zA-Z0-9_]*: ]] && [[ ! "$line" =~ ^[[:space:]] ]]; then
                in_noise=0
                in_custom=0
                printf '%s\n' "$line"
                continue
            fi
            if [ "$in_noise" = 1 ] && [[ "$line" =~ ^[[:space:]]+custom_patterns: ]]; then
                in_custom=1
                echo "  custom_patterns:"
                if [ ${#preserved[@]} -gt 0 ]; then
                    for p in "${preserved[@]}"; do
                        printf "    - \"%s\"\n" "$p"
                    done
                else
                    echo "  custom_patterns: []"
                fi
                found=1
                continue
            fi
            if [ "$in_custom" = 1 ]; then
                if [[ "$line" =~ ^[[:space:]]+-[[:space:]] ]]; then
                    continue
                fi
                if [[ "$line" =~ ^[a-zA-Z_][a-zA-Z0-9_]*: ]] && [[ ! "$line" =~ ^[[:space:]] ]]; then
                    in_custom=0
                    in_noise=0
                    printf '%s\n' "$line"
                    continue
                fi
                in_custom=0
            fi
            printf '%s\n' "$line"
        done < "$CONFIG_FILE" > "$tmp"
        mv "$tmp" "$CONFIG_FILE"

        if [ "$found" = 1 ]; then
            jq -n --arg p "$pattern" '{ok: true, action: "removed", pattern: $p}'
        else
            jq -n --arg p "$pattern" '{error: "custom_patterns not found", pattern: $p}'
            exit 1
        fi
        ;;

    # ------------------------------------------------------------------------
    # 帮助：返回所有可用子命令说明
    # ------------------------------------------------------------------------
    help|*)
        cat <<'EOF'
{
    "commands": [
        {"name": "status", "desc": "返回完整的配置状态 JSON", "usage": "status"},
        {"name": "list-sessions", "desc": "列出 agent 下的所有 session keys 及用量", "usage": "list-sessions [agent_id]"},
        {"name": "add-key", "desc": "添加 session key 到 merge_jsonl_keys", "usage": "add-key <session_key>"},
        {"name": "remove-key", "desc": "从 merge_jsonl_keys 移除 session key", "usage": "remove-key <session_key>"},
        {"name": "set-credentials", "desc": "从 stdin 读取 JSON 设置 API 凭证", "usage": "set-credentials (stdin: {api_url, api_token, model})"},
        {"name": "test-api", "desc": "测试云端 API 连接是否正常", "usage": "test-api"},
        {"name": "archive", "desc": "运行归档，可选 force 参数忽略阈值", "usage": "archive [force]"},
        {"name": "suggestions", "desc": "返回配置建议和警告", "usage": "suggestions"},
        {"name": "list-noise-patterns", "desc": "列出所有自定义噪声过滤规则", "usage": "list-noise-patterns"},
        {"name": "add-noise-pattern", "desc": "添加自定义噪声过滤关键词", "usage": "add-noise-pattern <pattern>"},
        {"name": "remove-noise-pattern", "desc": "移除自定义噪声过滤规则", "usage": "remove-noise-pattern <pattern>"}
    ],
    "dream_mode_compatibility": {
        "note": "OpenClaw Dream 模式自动扫描 ~/.openclaw/workspace/memory/*.md 文件，无需调用 CLI",
        "format_version": "2.0",
        "yaml_frontmatter": true,
        "standard_categories": [
            "📌 核心要点", "🎯 决策与结论", "✅ 已完成事项", "📋 待办与计划",
            "🧠 用户偏好与习惯", "🔧 技术/项目要点", "⚠️ 风险与注意事项", "💡 创意与想法"
        ],
        "auto_scan_directory": "~/.openclaw/workspace/memory/*.md"
    }
}
EOF
        ;;
esac
