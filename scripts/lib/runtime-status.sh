#!/usr/bin/env bash
# DMA runtime status lifecycle hooks.
#
# The archive engine sources this file and calls runtime_status_begin at the
# point where it owns the archive lock.  The hooks only publish observations;
# they never make archive decisions or alter the existing archive policy.

RUNTIME_STATUS_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_STATUS_PY="${DAILY_MEMORY_RUNTIME_STATUS_PY:-$RUNTIME_STATUS_SCRIPT_DIR/runtime-status.py}"
RUNTIME_STATUS_PYTHON="${DAILY_MEMORY_PYTHON:-python3}"
RUNTIME_STATUS_ACTIVE=0
RUNTIME_STATUS_FINISHED=0
RUNTIME_STATUS_RUN_ID=""
RUNTIME_STATUS_PATH=""
RUNTIME_STATUS_CHECKPOINT_PATH=""
RUNTIME_STATUS_CHECKPOINT_BEFORE=""
RUNTIME_STATUS_OUTCOME=""
RUNTIME_STATUS_REASON=""
RUNTIME_STATUS_PENDING_COUNT="null"
RUNTIME_STATUS_OLDEST_PENDING_AT=""
RUNTIME_STATUS_ARCHIVED=0
RUNTIME_STATUS_STORAGE_RESULT=unknown
RUNTIME_STATUS_SUMMARY_RESULT=unknown
RUNTIME_STATUS_ARCHIVE_RESULT=unknown
RUNTIME_STATUS_ERRORS=()

runtime_status_log() {
    if declare -F log >/dev/null 2>&1; then
        log "$*"
    else
        printf '%s\n' "[WARN] $*" >&2
    fi
}

runtime_status_add_error() {
    local code="${1:-}"
    [ -n "$code" ] || return 0
    local existing
    for existing in "${RUNTIME_STATUS_ERRORS[@]}"; do
        [ "$existing" = "$code" ] && return 0
    done
    RUNTIME_STATUS_ERRORS+=("$code")
}

runtime_status_begin() {
    [ "$RUNTIME_STATUS_ACTIVE" = "0" ] || return 0
    if [ "${RUNTIME_STATUS_LOCK_AVAILABLE:-1}" != "1" ]; then
        runtime_status_log "flock 不可用，跳过 runtime status 发布（归档继续按原策略执行）"
        return 0
    fi
    local config_dir="${CONFIG_DIR:-${DAILY_MEMORY_CONFIG_DIR:-}}"
    if [ -z "$config_dir" ]; then
        runtime_status_log "runtime status 未配置 CONFIG_DIR，跳过状态发布"
        RUNTIME_STATUS_ACTIVE=1
        runtime_status_add_error status_path_unknown
        return 0
    fi
    RUNTIME_STATUS_PATH="${DAILY_MEMORY_STATUS_PATH:-$config_dir/.runtime_status.json}"
    RUNTIME_STATUS_CHECKPOINT_PATH="${MERGE_CHECKPOINT_FILE:-$config_dir/.archive_merge_checkpoint.json}"
    RUNTIME_STATUS_RUN_ID="${DMA_RUNTIME_RUN_ID:-dma-$(date -u '+%Y%m%dT%H%M%S.%NZ')-$$-${RANDOM:-0}}"
    RUNTIME_STATUS_ACTIVE=1

    if [ -f "$RUNTIME_STATUS_CHECKPOINT_PATH" ]; then
        if RUNTIME_STATUS_CHECKPOINT_BEFORE=$("$RUNTIME_STATUS_PYTHON" "$RUNTIME_STATUS_PY" fingerprint --file "$RUNTIME_STATUS_CHECKPOINT_PATH" 2>/dev/null); then
            :
        else
            RUNTIME_STATUS_CHECKPOINT_BEFORE=""
            runtime_status_add_error checkpoint_evidence_unknown
        fi
    else
        # The helper returns the stable value `missing` for a not-yet-created
        # checkpoint.  This lets a first successful run prove cursor progress.
        if RUNTIME_STATUS_CHECKPOINT_BEFORE=$("$RUNTIME_STATUS_PYTHON" "$RUNTIME_STATUS_PY" fingerprint --file "$RUNTIME_STATUS_CHECKPOINT_PATH" 2>/dev/null); then
            :
        else
            RUNTIME_STATUS_CHECKPOINT_BEFORE=""
            runtime_status_add_error checkpoint_evidence_unknown
        fi
    fi

    local -a begin_args
    begin_args=(
        begin
        --path "$RUNTIME_STATUS_PATH"
        --run-id "$RUNTIME_STATUS_RUN_ID"
        --legacy-retry-file "$config_dir/.cloud_retry_count"
        --legacy-alert-file "$config_dir/.cloud_fail_alert"
    )
    if ! "$RUNTIME_STATUS_PYTHON" "$RUNTIME_STATUS_PY" "${begin_args[@]}"; then
        runtime_status_log "runtime status begin 发布失败（不改变归档策略）"
    fi
}

runtime_status_mark() {
    RUNTIME_STATUS_OUTCOME="${1:-failed}"
    RUNTIME_STATUS_REASON="${2:-archive}"
    if [ $# -ge 3 ] && [ -n "${3:-}" ]; then
        RUNTIME_STATUS_PENDING_COUNT="$3"
    else
        RUNTIME_STATUS_PENDING_COUNT="null"
    fi
    if [ $# -ge 4 ]; then
        RUNTIME_STATUS_OLDEST_PENDING_AT="${4:-}"
    else
        RUNTIME_STATUS_OLDEST_PENDING_AT=""
    fi
    if [ $# -ge 5 ]; then
        RUNTIME_STATUS_ARCHIVED="${5:-0}"
    else
        RUNTIME_STATUS_ARCHIVED=0
    fi
}

runtime_status_note_storage() {
    RUNTIME_STATUS_STORAGE_RESULT="${1:-unknown}"
}

runtime_status_note_summary() {
    RUNTIME_STATUS_SUMMARY_RESULT="${1:-unknown}"
}

runtime_status_note_archive() {
    RUNTIME_STATUS_ARCHIVE_RESULT="${1:-unknown}"
}

runtime_status_mark_pending_from_snapshot() {
    local snapshot="${1:-}"
    local count oldest
    if ! count=$(printf '%s\n' "$snapshot" | jq -r '.count // empty' 2>/dev/null) || [ -z "$count" ]; then
        runtime_status_add_error pending_snapshot_unknown
        RUNTIME_STATUS_PENDING_COUNT="null"
        RUNTIME_STATUS_OLDEST_PENDING_AT=""
        return 0
    fi
    RUNTIME_STATUS_PENDING_COUNT="$count"
    # session-store returns data in chronological order.  Preserve that order
    # instead of lexically sorting offset-bearing timestamps (which can put a
    # later UTC instant before an earlier one).
    oldest=$(printf '%s\n' "$snapshot" | jq -r '[.data[]?.ts | select(type == "string" and length > 0)][0] // empty' 2>/dev/null || true)
    RUNTIME_STATUS_OLDEST_PENDING_AT="$oldest"
}

runtime_status_finish() {
    [ "$RUNTIME_STATUS_ACTIVE" = "1" ] || return 0
    [ "$RUNTIME_STATUS_FINISHED" = "0" ] || return 0
    RUNTIME_STATUS_FINISHED=1
    local outcome="${RUNTIME_STATUS_OUTCOME:-failed}"
    local reason="${RUNTIME_STATUS_REASON:-unhandled_exit}"
    [ -n "${RUNTIME_STATUS_OUTCOME:-}" ] || runtime_status_add_error unhandled_exit
    [ -n "${RUNTIME_STATUS_CHECKPOINT_BEFORE:-}" ] || runtime_status_add_error checkpoint_evidence_unknown
    local reconcile_dir=""
    if [ -n "${MEMORY_DIR:-}" ]; then
        reconcile_dir="$MEMORY_DIR/.pending"
    else
        runtime_status_add_error reconcile_snapshot_unknown
    fi
    local -a args
    args=(
        finish
        --path "$RUNTIME_STATUS_PATH"
        --run-id "$RUNTIME_STATUS_RUN_ID"
        --outcome "$outcome"
        --reason "$reason"
        --pending-count "${RUNTIME_STATUS_PENDING_COUNT:-null}"
        --storage-result "${RUNTIME_STATUS_STORAGE_RESULT:-unknown}"
        --summary-result "${RUNTIME_STATUS_SUMMARY_RESULT:-unknown}"
        --archive-result "${RUNTIME_STATUS_ARCHIVE_RESULT:-unknown}"
        --archived "${RUNTIME_STATUS_ARCHIVED:-0}"
    )
    [ -n "$RUNTIME_STATUS_OLDEST_PENDING_AT" ] && args+=(--oldest-pending-at "$RUNTIME_STATUS_OLDEST_PENDING_AT")
    [ -n "$reconcile_dir" ] && args+=(--reconcile-dir "$reconcile_dir")
    [ -n "$RUNTIME_STATUS_CHECKPOINT_PATH" ] && args+=(--checkpoint-path "$RUNTIME_STATUS_CHECKPOINT_PATH")
    [ -n "$RUNTIME_STATUS_CHECKPOINT_BEFORE" ] && args+=(--checkpoint-before "$RUNTIME_STATUS_CHECKPOINT_BEFORE")
    local code
    for code in "${RUNTIME_STATUS_ERRORS[@]}"; do
        args+=(--status-error "$code")
    done
    if ! "$RUNTIME_STATUS_PYTHON" "$RUNTIME_STATUS_PY" "${args[@]}"; then
        runtime_status_log "runtime status finish 发布失败（不改变归档策略）"
    fi
}

runtime_status_on_exit() {
    local rc="${1:-0}"
    if [ "$RUNTIME_STATUS_ACTIVE" = "1" ] && [ "$RUNTIME_STATUS_FINISHED" = "0" ]; then
        if [ -z "$RUNTIME_STATUS_OUTCOME" ]; then
            RUNTIME_STATUS_OUTCOME=failed
            RUNTIME_STATUS_REASON=archive
            RUNTIME_STATUS_ARCHIVE_RESULT=failed
            runtime_status_add_error unhandled_exit
        elif [ "$rc" -ne 0 ] && [ "$RUNTIME_STATUS_OUTCOME" != "failed" ] && [ "$RUNTIME_STATUS_OUTCOME" != "partial" ]; then
            # A shell error after a terminal marker is still an incomplete
            # observation; retain the nonzero command result and expose it.
            RUNTIME_STATUS_OUTCOME=failed
            RUNTIME_STATUS_REASON=archive
            RUNTIME_STATUS_ARCHIVE_RESULT=failed
            runtime_status_add_error unhandled_exit
        fi
        runtime_status_finish
    fi
}
