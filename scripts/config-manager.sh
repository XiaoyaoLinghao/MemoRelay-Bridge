#!/usr/bin/env bash
# 配置管理：凭证加密 + YAML；save-json 保留 session.key / merge_jsonl_keys
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CONFIG_DIR="${DAILY_MEMORY_CONFIG_DIR:-$SKILL_ROOT/config}"
CONFIG_FILE="${CONFIG_DIR}/config.yaml"
CREDENTIALS_FILE="${CONFIG_DIR}/credentials.enc"
export CONFIG_DIR
# shellcheck source=lib/credentials-store.sh
source "$SCRIPT_DIR/lib/credentials-store.sh"
# shellcheck source=lib/config-loader.sh
source "$SCRIPT_DIR/lib/config-loader.sh"

mkdir -p "$CONFIG_DIR"
chmod 700 "$CONFIG_DIR"

log() {
    echo "[$(date '+%H:%M:%S')] $1"
}

legacy_yaml_cloud_value() { yaml_scalar "$1"; }

migrate_legacy_plaintext_yaml() {
    [ -f "$CREDENTIALS_FILE" ] && return 0
    [ -f "$CONFIG_FILE" ] || return 0
    local u t m
    u=$(legacy_yaml_cloud_value api_url || true)
    t=$(legacy_yaml_cloud_value api_token || true)
    m=$(legacy_yaml_cloud_value model || true)
    [ -n "$u" ] && [ -n "$t" ] && [ -n "$m" ] || return 0
    log "检测到明文 API 配置，正在写入 credentials.enc …"
    credentials_save_cloud_triple "$u" "$t" "$m"
    save_config_yaml
    log "✅ 已迁移：密钥已脱离 config.yaml"
}

read_preserved_session_key() {
    [ -f "$CONFIG_FILE" ] || return
    awk '
        /^session:/ { ins=1; next }
        ins && /^[a-z][a-z0-9_]*:/ && !/^session/ { exit }
        ins && /^[[:space:]]+key:/ {
            line=$0
            sub(/^[[:space:]]+key:[[:space:]]+/, "", line)
            gsub(/^["'\'']|["'\'']$/, "", line)
            print line
            exit
        }
    ' "$CONFIG_FILE"
}

read_preserved_merge_jsonl_keys() { yaml_merge_keys "$CONFIG_FILE"; }

save_config_yaml() {
    local sk preserved_m m
    sk=""
    preserved_m=()
    if [ -f "$CONFIG_FILE" ]; then
        sk=$(read_preserved_session_key | head -1 | tr -d '\r')
        while IFS= read -r line; do
            [ -n "$line" ] && preserved_m+=("$line")
        done < <(read_preserved_merge_jsonl_keys)
    fi
    {
        echo "# Daily Memory Archiver 配置"
        echo "# 更新时间: $(date -Iseconds)"
        echo "# api_url / api_token / model → credentials.enc（JSON 加密）"
        echo ""
        echo "openclaw:"
        echo "  agent_id: main"
        echo ""
        echo "session:"
        printf '  key: "%s"\n' "${sk//\"/\\\"}"
        echo "  # 多通道：merge_jsonl_keys 按 timestamp 合并后再分析"
        if [ ${#preserved_m[@]} -eq 0 ]; then
            echo "  merge_jsonl_keys: []"
        else
            echo "  merge_jsonl_keys:"
            for m in "${preserved_m[@]}"; do
                printf '    - "%s"\n' "${m//\"/\\\"}"
            done
        fi
        echo ""
        echo "archive:"
        echo "  trigger_mode: \"${ARCHIVE_MODE:-threshold}\""
        echo "  threshold:"
        echo "    max_input_tokens: ${THRESHOLD_INPUT_TOKENS:-200000}"
        echo "    check_interval_minutes: ${CHECK_INTERVAL:-5}"
        echo "    cooldown_minutes: 45"
        echo "  min_new_messages: 1"
        echo "  # 距上次成功写入 memory ≥ 该分钟数时，即使未达 token 阈值也进入归档流程（有 ≥1 条检查点后的新消息则写）；0=关闭"
        echo "  periodic_archive_minutes: ${PERIODIC_ARCHIVE_MINUTES:-120}"
        echo "  compact_only_over_threshold: true"
        echo "  compact:"
        echo "    max_lines: ${COMPACT_MAX_LINES:-400}"
        echo ""
        echo "analyzer:"
        echo "  messages_to_analyze: ${MESSAGES_TO_ANALYZE:-50}"
        echo "  chunk_cloud_summary: true"
        echo "  max_cloud_summary_chunks: 20"
        echo "  cloud_summarizer:"
        echo "    enabled: true"
        echo ""
        echo "logging:"
        echo "  log_max_bytes: ${LOG_MAX_BYTES:-10485760}"
        echo "  log_keep_rotations: ${LOG_KEEP_ROTATIONS:-5}"
        echo "  log_max_age_days: ${LOG_MAX_AGE_DAYS:-30}"
        echo ""
        echo "output:"
        echo "  memory_dir: \"${MEMORY_DIR:-~/.openclaw/workspace/memory}\""
        echo ""
        echo "skill_version: \"1.7.1\""
        echo "config_version: \"8\""
    } > "$CONFIG_FILE"
    chmod 644 "$CONFIG_FILE"
    log "✅ 配置已保存: $CONFIG_FILE"
}

merge_jsonl_keys_add() {
    local new="$1"
    [ -n "$new" ] || {
        echo "用法: $0 merge-jsonl-keys-add <session_key>" >&2
        exit 2
    }
    [ -f "$CONFIG_FILE" ] || {
        echo "缺少 $CONFIG_FILE，请先 init-defaults" >&2
        exit 1
    }
    local preserved_m=() m
    while IFS= read -r line; do
        [ -n "$line" ] && preserved_m+=("$line")
    done < <(read_preserved_merge_jsonl_keys)
    for m in "${preserved_m[@]}"; do
        if [ "$m" = "$new" ]; then
            log "merge_jsonl_keys 已包含: $new"
            exit 0
        fi
    done
    preserved_m+=("$new")
    if grep -qE '^[[:space:]]+merge_jsonl_keys:' "$CONFIG_FILE" 2>/dev/null; then
        merge_jsonl_keys_rewrite_block "${preserved_m[@]}"
    else
        merge_jsonl_keys_insert_after_key "${preserved_m[@]}"
    fi
}

merge_jsonl_keys_insert_after_key() {
    local keys=("$@")
    local tmp in_session inserted
    tmp=$(mktemp)
    in_session=0
    inserted=0
    while IFS= read -r line || [ -n "$line" ]; do
        printf '%s\n' "$line"
        if [[ "$line" == "session:" ]]; then
            in_session=1
            continue
        fi
        if [ "$in_session" = 1 ] && [[ "$line" =~ ^[a-zA-Z_][a-zA-Z0-9_]*: ]] && [[ ! "$line" =~ ^[[:space:]] ]]; then
            in_session=0
        fi
        if [ "$in_session" = 1 ] && [[ "$line" =~ ^[[:space:]]+key: ]] && [ "$inserted" = 0 ]; then
            echo "  # 多通道：merge_jsonl_keys 按 timestamp 合并后再分析"
            if [ ${#keys[@]} -eq 0 ]; then
                echo "  merge_jsonl_keys: []"
            else
                echo "  merge_jsonl_keys:"
                local k
                for k in "${keys[@]}"; do
                    printf '    - "%s"\n' "${k//\"/\\\"}"
                done
            fi
            inserted=1
        fi
    done < "$CONFIG_FILE" > "$tmp"
    mv "$tmp" "$CONFIG_FILE"
    chmod 644 "$CONFIG_FILE"
    log "✅ 已插入 merge_jsonl_keys（${#keys[@]} 项）"
}

merge_jsonl_keys_rewrite_block() {
    local keys=("$@")
    local tmp in_session in_merge
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
            if [ ${#keys[@]} -eq 0 ]; then
                echo "  merge_jsonl_keys: []"
            else
                echo "  merge_jsonl_keys:"
                local k
                for k in "${keys[@]}"; do
                    printf '    - "%s"\n' "${k//\"/\\\"}"
                done
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
    chmod 644 "$CONFIG_FILE"
    log "✅ 已更新 merge_jsonl_keys（${#keys[@]} 项）"
}

save_cloud_and_yaml() {
    local api_url="$1" api_token="$2" model="$3"
    credentials_save_cloud_triple "$api_url" "$api_token" "$model"
    log "✅ 已加密保存: $CREDENTIALS_FILE"
    save_config_yaml
}

load_credentials() {
    credentials_decrypt_raw
}

show_credentials_masked() {
    local raw
    raw=$(credentials_decrypt_raw) || {
        echo "无法读取 credentials.enc" >&2
        return 1
    }
    if echo "$raw" | jq -e '.api_url' >/dev/null 2>&1; then
        echo "$raw" | jq '{api_url, model, api_token: (.api_token|tostring|if length > 10 then .[0:6] + "…" + .[length-4:] else "…" end)}'
    else
        echo '{"format":"legacy_token_only","api_token":"(已隐藏)"}'
    fi
}

save_from_json_stdin() {
    local json u t m
    json=$(cat)
    echo "$json" | jq -e '
        (.api_url | type == "string" and (length > 0))
        and (.api_token | type == "string" and (length > 0))
        and (.model | type == "string" and (length > 0))
    ' >/dev/null 2>&1 || {
        echo "[错误] stdin 须为 JSON：api_url、api_token、model 非空字符串" >&2
        exit 1
    }
    u=$(echo "$json" | jq -r .api_url)
    t=$(echo "$json" | jq -r .api_token)
    m=$(echo "$json" | jq -r .model)
    save_cloud_and_yaml "$u" "$t" "$m"
    touch "${CONFIG_DIR}/.initialized"
    log "✅ save-json 完成"
}

skill_status() {
    echo "skill: daily-memory-archiver"
    echo "skill_root: ${SKILL_ROOT}"
    echo "config_dir: ${CONFIG_DIR}"
    if [ -f "${CONFIG_DIR}/.initialized" ]; then
        echo "initialized: yes"
    else
        echo "initialized: no"
    fi
    if [ -f "$CREDENTIALS_FILE" ]; then
        echo "credentials_enc: present"
    else
        echo "credentials_enc: missing"
    fi
    if [ -f "$CONFIG_FILE" ]; then
        echo "config_yaml: present"
        grep -E '^skill_version:|^config_version:' "$CONFIG_FILE" 2>/dev/null || true
    else
        echo "config_yaml: missing"
    fi
}

main_wizard() {
    migrate_legacy_plaintext_yaml
    echo ""
    echo "Daily Memory Archiver — 配置向导（API 将写入 credentials.enc）"
    local api_url api_token model
    read -rp "API Base URL > " api_url
    read -rsp "API Token > " api_token
    echo ""
    read -rp "Model > " model
    save_cloud_and_yaml "$api_url" "$api_token" "$model"
    touch "${CONFIG_DIR}/.initialized"
    echo "完成: $CONFIG_FILE"
}

case "${1:-}" in
    init-defaults)
        if [ -f "${CONFIG_DIR}/.initialized" ] && [ -f "$CONFIG_FILE" ]; then
            echo "✅ 已存在初始化与 config.yaml，跳过（避免覆盖）。"
            exit 0
        fi
        mkdir -p "$CONFIG_DIR"
        chmod 700 "$CONFIG_DIR"
        save_config_yaml
        touch "${CONFIG_DIR}/.initialized"
        echo "✅ 已写入默认 config.yaml（无 API；请 save-json）。"
        ;;
    load_credentials)
        load_credentials
        ;;
    show|credentials_show)
        show_credentials_masked
        ;;
    migrate)
        migrate_legacy_plaintext_yaml
        ;;
    save-json)
        save_from_json_stdin
        ;;
    status)
        skill_status
        ;;
    merge-jsonl-keys-add)
        merge_jsonl_keys_add "${2:?用法: $0 merge-jsonl-keys-add <session_key>}"
        ;;
    merge-jsonl-keys-list)
        read_preserved_merge_jsonl_keys
        ;;
    "")
        main_wizard
        ;;
    *)
        echo "用法: $0 [init-defaults|save-json|status|show|migrate|merge-jsonl-keys-list|merge-jsonl-keys-add <key>]" >&2
        exit 2
        ;;
esac
