#!/usr/bin/env bash
# Isolated lifecycle-hook checks.  RUNTIME_STATUS_LOCK_AVAILABLE=1 is an
# explicit test seam: the test models a caller that already owns the archive
# lock; it does not claim to exercise the host's flock implementation.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
RUN_DIR="$(mktemp -d -t dma-runtime-status-hooks.XXXXXX)"
trap 'rm -rf -- "$RUN_DIR"' EXIT

PYTHON_BIN="${DAILY_MEMORY_PYTHON:-python3}"

run_case() {
    local name="$1" expected_outcome="$2" expected_reason="$3"
    local pending="$4" archived="$5" summary_result="$6"
    local case_dir="$RUN_DIR/$name"
    mkdir -p "$case_dir/config" "$case_dir/memory/.pending"
    : >"$case_dir/config/.archive_merge_checkpoint.json"

    (
        export CONFIG_DIR="$case_dir/config"
        export MEMORY_DIR="$case_dir/memory"
        export MERGE_CHECKPOINT_FILE="$CONFIG_DIR/.archive_merge_checkpoint.json"
        export DAILY_MEMORY_RUNTIME_STATUS_PY="$ROOT/scripts/runtime-status.py"
        export DAILY_MEMORY_PYTHON="$PYTHON_BIN"
        export RUNTIME_STATUS_LOCK_AVAILABLE=1
        # Source in the child so every case starts with clean hook state.
        # shellcheck source=../scripts/lib/runtime-status.sh
        source "$ROOT/scripts/lib/runtime-status.sh"

        runtime_status_begin
        runtime_status_note_storage success
        runtime_status_note_summary "$summary_result"
        runtime_status_mark "$expected_outcome" "$expected_reason" "$pending" "" "$archived"
        runtime_status_finish

        "$PYTHON_BIN" - "$CONFIG_DIR/.runtime_status.json" "$expected_outcome" "$expected_reason" "$pending" "$summary_result" <<'PY'
import json
import sys
from pathlib import Path

status = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
assert status["outcome"] == sys.argv[2], status
assert status["reason"] == sys.argv[3], status
expected_pending = None if sys.argv[4] == "null" else int(sys.argv[4])
assert status["pending_count"] == expected_pending, status
assert status["failures"]["summary"]["consecutive"] == (1 if sys.argv[5] == "failed" else 0), status
assert status["pending_reconcile_count"] == 0, status
PY
    )
    printf 'OK  : %s\n' "$name"
}

run_case idle idle no_input 0 0 success
run_case noise noise_only noise_only 0 0 unknown
run_case summary-failure failed summary 4 0 failed
run_case partial partial summary 0 0 failed
printf 'ALL runtime-status hook checks PASS\n'
