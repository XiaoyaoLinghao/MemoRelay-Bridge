#!/usr/bin/env python3
"""Publish the DMA runtime status contract.

This module deliberately owns only the small runtime-status state machine.  It
does not read sessions, change archive decisions, or inspect Memory content.
The archive engine calls ``begin`` and ``finish`` while it owns the archive
lock.  Publication is an atomic replace in the status file's directory.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = "dma-runtime-status-v1"
OUTCOMES = {
    "running",
    "archived",
    "idle",
    "noise_only",
    "deferred",
    "failed",
    "partial",
}
OPERATIONS = ("storage", "summary", "archive")
RESULTS = {"unknown", "success", "failed"}
CODE_RE = re.compile(r"^[a-z][a-z0-9_.-]*$")
RUN_ID_RE = re.compile(r"^\S+$")
LATCHED_HISTORY_ERRORS = {
    "legacy_failure_marker_unreadable",
    "legacy_failure_time_unknown",
    "legacy_retry_invalid",
    "legacy_retry_unreadable",
    "previous_status_corrupt",
    "previous_status_unreadable",
    "status_missing_during_run",
    "status_corrupt_during_run",
    "status_unreadable_during_run",
    "interrupted_previous_run",
}


class StatusError(Exception):
    """A status input or publication error."""


def _now() -> str:
    # Keep sub-second precision so filesystem mtimes and back-to-back status
    # transitions remain ordered when consumers enforce timestamp bounds.
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _parse_timestamp(value: Any, field: str, *, allow_none: bool = True) -> None:
    if value is None and allow_none:
        return
    if not isinstance(value, str) or not value.strip():
        raise StatusError(f"{field} must be a timezone-aware timestamp or null")
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise StatusError(f"{field} is not ISO8601: {error}") from error
    if parsed.tzinfo is None:
        raise StatusError(f"{field} must include a timezone offset")


def _is_nonnegative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _validate_failure(value: Any, operation: str) -> None:
    if not isinstance(value, dict):
        raise StatusError(f"failures.{operation} must be an object")
    expected = {"consecutive", "last_failed_at", "last_recovered_at"}
    if set(value) != expected:
        raise StatusError(f"failures.{operation} fields are incomplete")
    if not _is_nonnegative_int(value["consecutive"]):
        raise StatusError(f"failures.{operation}.consecutive must be nonnegative")
    _parse_timestamp(value["last_failed_at"], f"failures.{operation}.last_failed_at")
    _parse_timestamp(
        value["last_recovered_at"], f"failures.{operation}.last_recovered_at"
    )


def validate_status(value: Any) -> dict[str, Any]:
    """Validate one complete status object and return it as a dictionary."""

    if not isinstance(value, dict):
        raise StatusError("status root must be an object")
    required = {
        "schema_version",
        "run_id",
        "started_at",
        "observed_at",
        "finished_at",
        "outcome",
        "reason",
        "last_completed_at",
        "last_archived_at",
        "pending_count",
        "oldest_pending_at",
        "pending_reconcile_count",
        "oldest_reconcile_at",
        "checkpoint_progress_at",
        "failures",
        "status_errors",
    }
    if set(value) != required:
        missing = sorted(required - set(value))
        extra = sorted(set(value) - required)
        detail = []
        if missing:
            detail.append("missing=" + ",".join(missing))
        if extra:
            detail.append("extra=" + ",".join(extra))
        raise StatusError("status fields are not exact: " + " ".join(detail))
    if value["schema_version"] != SCHEMA_VERSION:
        raise StatusError("unsupported schema_version")
    if not isinstance(value["run_id"], str) or not RUN_ID_RE.match(value["run_id"]):
        raise StatusError("run_id must be a nonempty string")
    for field in ("started_at", "observed_at", "finished_at"):
        _parse_timestamp(value[field], field, allow_none=field == "finished_at")
    if not isinstance(value["outcome"], str) or value["outcome"] not in OUTCOMES:
        raise StatusError("unsupported outcome")
    if value["outcome"] == "running" and value["finished_at"] is not None:
        raise StatusError("running status must have finished_at=null")
    if value["outcome"] != "running" and value["finished_at"] is None:
        raise StatusError("terminal status must have finished_at")
    for field in (
        "last_completed_at",
        "last_archived_at",
        "oldest_pending_at",
        "oldest_reconcile_at",
        "checkpoint_progress_at",
    ):
        _parse_timestamp(value[field], field)
    if not isinstance(value["reason"], str) or not CODE_RE.match(value["reason"]):
        raise StatusError("reason must be a stable code")
    if value["pending_count"] is not None and not _is_nonnegative_int(value["pending_count"]):
        raise StatusError("pending_count must be a nonnegative integer or null")
    if value["pending_reconcile_count"] is not None and not _is_nonnegative_int(
        value["pending_reconcile_count"]
    ):
        raise StatusError("pending_reconcile_count must be a nonnegative integer or null")
    if not isinstance(value["failures"], dict) or set(value["failures"]) != set(OPERATIONS):
        raise StatusError("failures must contain exactly storage, summary and archive")
    for operation in OPERATIONS:
        _validate_failure(value["failures"][operation], operation)
    if not isinstance(value["status_errors"], list):
        raise StatusError("status_errors must be a list")
    for code in value["status_errors"]:
        if not isinstance(code, str) or not CODE_RE.match(code):
            raise StatusError("status_errors must contain stable codes")
    if len(set(value["status_errors"])) != len(value["status_errors"]):
        raise StatusError("status_errors must not contain duplicates")
    return value


def _default_failures() -> dict[str, dict[str, Any]]:
    return {
        operation: {
            "consecutive": 0,
            "last_failed_at": None,
            "last_recovered_at": None,
        }
        for operation in OPERATIONS
    }


def _default_status(run_id: str, started_at: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "started_at": started_at,
        "observed_at": started_at,
        "finished_at": None,
        "outcome": "running",
        "reason": "started",
        "last_completed_at": None,
        "last_archived_at": None,
        "pending_count": None,
        "oldest_pending_at": None,
        "pending_reconcile_count": None,
        "oldest_reconcile_at": None,
        "checkpoint_progress_at": None,
        "failures": _default_failures(),
        "status_errors": [],
    }


def _read_status(path: Path) -> tuple[dict[str, Any] | None, list[str]]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None, []
    except (OSError, UnicodeError) as error:
        return None, ["previous_status_unreadable"]
    try:
        value = json.loads(raw.decode("utf-8"))
        return validate_status(value), []
    except (UnicodeError, json.JSONDecodeError, StatusError, TypeError, KeyError, AttributeError):
        return None, ["previous_status_corrupt"]


def _append_codes(target: list[str], codes: Iterable[str]) -> None:
    for code in codes:
        if not isinstance(code, str) or not CODE_RE.match(code):
            raise StatusError(f"invalid diagnostic code: {code!r}")
        if code not in target:
            target.append(code)


def _safe_status(path: Path, run_id: str, started_at: str, codes: Iterable[str]) -> dict[str, Any]:
    value = _default_status(run_id, started_at)
    _append_codes(value["status_errors"], codes)
    return value


def _atomic_write(path: Path, value: dict[str, Any]) -> None:
    path = path.absolute()
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(value, ensure_ascii=False, sort_keys=False, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(parent))
    temp_path = Path(temp_name)
    try:
        mode = stat.S_IRUSR | stat.S_IWUSR
        if hasattr(os, "fchmod"):
            os.fchmod(fd, mode)
        else:
            # CPython on Windows does not expose fchmod; mkstemp already
            # creates a private file there, and chmod is the available best
            # effort equivalent.
            os.chmod(temp_path, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        try:
            directory_fd = os.open(parent, os.O_RDONLY)
        except OSError:
            directory_fd = None
        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            except OSError:
                pass
            finally:
                os.close(directory_fd)
    except Exception:
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise


def _path_from_args(args: argparse.Namespace) -> Path:
    raw = args.path or os.environ.get("DAILY_MEMORY_STATUS_PATH", "")
    if not raw:
        config_dir = os.environ.get("DAILY_MEMORY_CONFIG_DIR", "")
        if not config_dir:
            raise StatusError("status path is not configured")
        raw = str(Path(config_dir) / ".runtime_status.json")
    return Path(os.path.expanduser(raw))


def _read_hash(path: Path) -> str:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except FileNotFoundError:
        return "missing"
    except (OSError, UnicodeError) as error:
        raise StatusError(f"cannot fingerprint checkpoint: {error}") from error


def _parse_nullable_nonnegative(raw: str | None, field: str) -> int | None:
    if raw is None or raw.lower() == "null":
        return None
    try:
        result = int(raw, 10)
    except (TypeError, ValueError) as error:
        raise StatusError(f"{field} must be a nonnegative integer or null") from error
    if result < 0:
        raise StatusError(f"{field} must be a nonnegative integer or null")
    return result


def _list_pending(reconcile_dir: Path) -> tuple[int | None, str | None, list[str]]:
    try:
        if not reconcile_dir.exists():
            return 0, None, []
        if not reconcile_dir.is_dir():
            return None, None, ["reconcile_snapshot_unreadable"]
        sidecars = [
            item
            for item in reconcile_dir.iterdir()
            if item.is_file() and item.suffix == ".json"
        ]
        if not sidecars:
            return 0, None, []
        oldest = min(item.stat().st_mtime for item in sidecars)
        oldest_at = datetime.fromtimestamp(oldest, timezone.utc).isoformat(timespec="microseconds")
        return len(sidecars), oldest_at, []
    except (OSError, ValueError) as error:
        return None, None, ["reconcile_snapshot_unreadable"]


def _legacy_mtime(path: Path) -> tuple[str | None, list[str]]:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="microseconds"), []
    except (OSError, ValueError):
        return None, ["legacy_failure_time_unknown"]


def _bootstrap_legacy_summary_failure(
    value: dict[str, Any], retry_path: Path | None, alert_path: Path | None
) -> None:
    """Carry the pre-v1 cloud retry markers into the first status record.

    These markers are only consulted when no valid v1 status exists.  An empty
    retry file is the normal success state of the legacy writer and means zero.
    """

    count = 0
    failed_at: str | None = None
    for marker in (retry_path, alert_path):
        if marker is None:
            continue
        try:
            exists = marker.exists()
        except OSError:
            _append_codes(value["status_errors"], ["legacy_failure_marker_unreadable"])
            continue
        if not exists:
            continue
        if marker == retry_path:
            try:
                raw = marker.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeError):
                _append_codes(value["status_errors"], ["legacy_retry_unreadable"])
                continue
            if raw == "":
                marker_count = 0
            elif re.fullmatch(r"[0-9]+", raw):
                marker_count = int(raw, 10)
            else:
                _append_codes(value["status_errors"], ["legacy_retry_invalid"])
                marker_count = 0
            if marker_count > count:
                count = marker_count
                failed_at, time_errors = _legacy_mtime(marker)
                _append_codes(value["status_errors"], time_errors)
        else:
            if count < 1:
                count = 1
            if failed_at is None:
                failed_at, time_errors = _legacy_mtime(marker)
                _append_codes(value["status_errors"], time_errors)
    if count > 0:
        entry = value["failures"]["summary"]
        entry["consecutive"] = count
        entry["last_failed_at"] = failed_at


def _operation_result(value: str | None, field: str) -> str:
    result = value or "unknown"
    if result not in RESULTS:
        raise StatusError(f"{field} must be unknown, success or failed")
    return result


def _update_failure(entry: dict[str, Any], result: str, observed_at: str) -> None:
    if result == "failed":
        entry["consecutive"] += 1
        entry["last_failed_at"] = observed_at
    elif result == "success":
        if entry["consecutive"] > 0:
            entry["last_recovered_at"] = observed_at
        entry["consecutive"] = 0


def command_fingerprint(args: argparse.Namespace) -> int:
    print(_read_hash(Path(os.path.expanduser(args.file))))
    return 0


def command_begin(args: argparse.Namespace) -> int:
    path = _path_from_args(args)
    run_id = args.run_id or f"dma-{uuid.uuid4().hex}"
    if not isinstance(run_id, str) or not RUN_ID_RE.match(run_id):
        raise StatusError("run_id must be a nonempty string")
    started_at = _now()
    previous, read_errors = _read_status(path)
    value = _default_status(run_id, started_at)
    if previous is not None:
        for field in (
            "last_completed_at",
            "last_archived_at",
            "checkpoint_progress_at",
        ):
            value[field] = previous[field]
        value["failures"] = copy.deepcopy(previous["failures"])
        # Evidence errors caused by a malformed or interrupted prior status
        # are history, not transient source-read results.  Carry them through
        # subsequent idle runs until a complete archival run proves the
        # status path healthy again.
        _append_codes(
            value["status_errors"],
            (code for code in previous["status_errors"] if code in LATCHED_HISTORY_ERRORS),
        )
        if previous["outcome"] == "running":
            _append_codes(value["status_errors"], ["interrupted_previous_run"])
    else:
        _bootstrap_legacy_summary_failure(
            value,
            Path(os.path.expanduser(args.legacy_retry_file)) if args.legacy_retry_file else None,
            Path(os.path.expanduser(args.legacy_alert_file)) if args.legacy_alert_file else None,
        )
    _append_codes(value["status_errors"], read_errors)
    try:
        _atomic_write(path, value)
    except (OSError, StatusError) as error:
        raise StatusError(f"status begin publication failed: {error}") from error
    return 0


def command_finish(args: argparse.Namespace) -> int:
    path = _path_from_args(args)
    if not args.run_id or not RUN_ID_RE.match(args.run_id):
        raise StatusError("run_id must be a nonempty string")
    current, read_errors = _read_status(path)
    if current is None:
        # A status file removed/corrupted while the run was active cannot be
        # silently replaced with a healthy terminal record.  Publish an
        # explicitly incomplete terminal record when possible.
        value = _safe_status(
            path,
            args.run_id,
            _now(),
            [
                "status_missing_during_run"
                if not read_errors
                else (
                    "status_unreadable_during_run"
                    if "previous_status_unreadable" in read_errors
                    else "status_corrupt_during_run"
                )
            ],
        )
    else:
        if current["run_id"] != args.run_id:
            raise StatusError("status run_id changed during run")
        value = copy.deepcopy(current)
    finish_at = _now()
    outcome = args.outcome
    if outcome not in OUTCOMES or outcome == "running":
        raise StatusError("finish outcome must be a terminal DMA outcome")
    reason = args.reason
    if not isinstance(reason, str) or not CODE_RE.match(reason):
        raise StatusError("reason must be a stable code")
    value["observed_at"] = finish_at
    value["finished_at"] = finish_at
    value["outcome"] = outcome
    value["reason"] = reason
    value["last_completed_at"] = finish_at
    value["pending_count"] = _parse_nullable_nonnegative(args.pending_count, "pending_count")
    value["oldest_pending_at"] = args.oldest_pending_at or None
    if value["oldest_pending_at"] is not None:
        _parse_timestamp(value["oldest_pending_at"], "oldest_pending_at")
    if args.reconcile_dir:
        count, oldest, reconcile_errors = _list_pending(Path(os.path.expanduser(args.reconcile_dir)))
        value["pending_reconcile_count"] = count
        value["oldest_reconcile_at"] = oldest
        _append_codes(value["status_errors"], reconcile_errors)
    else:
        value["pending_reconcile_count"] = None
        value["oldest_reconcile_at"] = None
        _append_codes(value["status_errors"], ["reconcile_snapshot_unknown"])
    _append_codes(value["status_errors"], args.status_error)
    if args.checkpoint_before is None or not args.checkpoint_path:
        _append_codes(value["status_errors"], ["checkpoint_evidence_unknown"])
    else:
        try:
            current_hash = _read_hash(Path(os.path.expanduser(args.checkpoint_path)))
        except StatusError:
            current_hash = None
            _append_codes(value["status_errors"], ["checkpoint_evidence_unknown"])
        if current_hash is not None and current_hash != args.checkpoint_before:
            value["checkpoint_progress_at"] = finish_at
    if args.archived == "1" and outcome == "archived":
        value["last_archived_at"] = finish_at
    for operation, result_arg in (
        ("storage", args.storage_result),
        ("summary", args.summary_result),
        ("archive", args.archive_result),
    ):
        _update_failure(value["failures"][operation], _operation_result(result_arg, operation), finish_at)
    _append_codes(value["status_errors"], read_errors)
    if (
        outcome == "archived"
        and args.storage_result == "success"
        and args.summary_result == "success"
        and args.archive_result == "success"
        and args.archived == "1"
        and value["pending_count"] == 0
        and value["pending_reconcile_count"] == 0
        and not any(code not in LATCHED_HISTORY_ERRORS for code in value["status_errors"])
    ):
        value["status_errors"] = [
            code for code in value["status_errors"] if code not in LATCHED_HISTORY_ERRORS
        ]
    try:
        validate_status(value)
        _atomic_write(path, value)
    except (OSError, StatusError) as error:
        raise StatusError(f"status finish publication failed: {error}") from error
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Publish DMA runtime status v1")
    subparsers = parser.add_subparsers(dest="command", required=True)

    fingerprint = subparsers.add_parser("fingerprint")
    fingerprint.add_argument("--file", required=True)

    begin = subparsers.add_parser("begin")
    begin.add_argument("--path")
    begin.add_argument("--run-id")
    begin.add_argument("--legacy-retry-file")
    begin.add_argument("--legacy-alert-file")

    finish = subparsers.add_parser("finish")
    finish.add_argument("--path")
    finish.add_argument("--run-id", required=True)
    finish.add_argument("--outcome", required=True)
    finish.add_argument("--reason", required=True)
    finish.add_argument("--pending-count")
    finish.add_argument("--oldest-pending-at")
    finish.add_argument("--reconcile-dir")
    finish.add_argument("--checkpoint-path")
    finish.add_argument("--checkpoint-before")
    finish.add_argument("--archived", choices=("0", "1"), default="0")
    finish.add_argument("--storage-result", choices=tuple(RESULTS), default="unknown")
    finish.add_argument("--summary-result", choices=tuple(RESULTS), default="unknown")
    finish.add_argument("--archive-result", choices=tuple(RESULTS), default="unknown")
    finish.add_argument("--status-error", action="append", default=[])
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "fingerprint":
            return command_fingerprint(args)
        if args.command == "begin":
            return command_begin(args)
        if args.command == "finish":
            return command_finish(args)
        raise StatusError("unknown command")
    except (OSError, StatusError, ValueError) as error:
        print(f"runtime-status: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
