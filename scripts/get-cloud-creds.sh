#!/usr/bin/env bash
# 输出 credentials.enc 解密后的 JSON，或 --export 打印 export 语句
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CONFIG_DIR="${DAILY_MEMORY_CONFIG_DIR:-$SKILL_ROOT/config}"
export CONFIG_DIR
# shellcheck source=lib/credentials-store.sh
source "$SCRIPT_DIR/lib/credentials-store.sh"

if [ "${1:-}" = "--export" ]; then
    raw=$(credentials_decrypt_raw) || exit 1
    u=$(echo "$raw" | jq -r .api_url)
    t=$(echo "$raw" | jq -r .api_token)
    m=$(echo "$raw" | jq -r .model)
    printf 'export DAILY_MEMORY_API_URL=%q\n' "$u"
    printf 'export DAILY_MEMORY_API_TOKEN=%q\n' "$t"
    printf 'export DAILY_MEMORY_MODEL=%q\n' "$m"
    exit 0
fi

credentials_decrypt_raw || exit 1
