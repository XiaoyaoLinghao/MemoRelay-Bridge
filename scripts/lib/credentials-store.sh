#!/usr/bin/env bash
# 加密凭证：credentials.enc（AES-256-CBC + PBKDF2），密钥 config/.master_key
set -euo pipefail
set -euo pipefail

CRED_DIR="${CONFIG_DIR:?CONFIG_DIR must be set}"
MASTER_KEY_FILE="$CRED_DIR/.master_key"
CREDENTIALS_ENC="$CRED_DIR/credentials.enc"

ensure_master_key() {
    mkdir -p "$CRED_DIR"
    chmod 700 "$CRED_DIR"
    if [ ! -f "$MASTER_KEY_FILE" ]; then
        openssl rand -hex 32 >"$MASTER_KEY_FILE"
        chmod 600 "$MASTER_KEY_FILE"
    fi
}

credentials_encrypt_to_file() {
    local plain="$1"
    local out="${2:-$CREDENTIALS_ENC}"
    ensure_master_key
    printf '%s' "$plain" | openssl enc -aes-256-cbc -pbkdf2 -salt \
        -pass "file:$MASTER_KEY_FILE" -base64 -out "$out"
    chmod 600 "$out"
}

credentials_decrypt_raw() {
    [ -f "$CREDENTIALS_ENC" ] || return 1
    [ -f "$MASTER_KEY_FILE" ] || return 1
    openssl enc -aes-256-cbc -pbkdf2 -d -salt \
        -pass "file:$MASTER_KEY_FILE" -base64 -in "$CREDENTIALS_ENC" 2>/dev/null || return 1
}

credentials_save_cloud_triple() {
    local api_url="$1" api_token="$2" model="$3"
    local json
    json=$(jq -n --arg u "$api_url" --arg t "$api_token" --arg m "$model" \
        '{api_url:$u, api_token:$t, model:$m}')
    credentials_encrypt_to_file "$json"
}

credentials_load_into_env() {
    local raw
    if ! raw=$(credentials_decrypt_raw 2>/dev/null); then
        echo "credentials: 解密 credentials.enc 失败（.master_key 不匹配、文件损坏或 openssl 异常）。请重新执行 save-json。" >&2
        return 1
    fi
    export DAILY_MEMORY_API_URL
    export DAILY_MEMORY_API_TOKEN
    export DAILY_MEMORY_MODEL
    DAILY_MEMORY_API_URL=$(echo "$raw" | jq -r '.api_url // empty')
    DAILY_MEMORY_API_TOKEN=$(echo "$raw" | jq -r '.api_token // empty')
    DAILY_MEMORY_MODEL=$(echo "$raw" | jq -r '.model // empty')
}
