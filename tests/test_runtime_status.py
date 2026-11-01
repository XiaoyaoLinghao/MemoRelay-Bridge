"""Unit tests for the DMA runtime status producer.

Every test uses a private temporary directory and invokes the same CLI that
archive-engine.sh uses.  No sessions, credentials, network calls, or deployed
files are involved.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "runtime-status.py"


class RuntimeStatusCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="dma-runtime-status-")
        self.root = Path(self.tempdir.name)
        self.config = self.root / "config"
        self.memory = self.root / "memory"
        self.pending = self.memory / ".pending"
        self.config.mkdir()
        self.pending.mkdir(parents=True)
        self.status = self.config / ".runtime_status.json"
        self.checkpoint = self.config / ".archive_merge_checkpoint.json"
        self.retry = self.config / ".cloud_retry_count"
        self.alert = self.config / ".cloud_fail_alert"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def run_cli(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            cwd=str(SCRIPT.parents[1]),
            text=True,
            capture_output=True,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        if check and result.returncode != 0:
            self.fail(f"CLI failed: {result.args}\nstdout={result.stdout}\nstderr={result.stderr}")
        return result

    def load(self) -> dict:
        return json.loads(self.status.read_text(encoding="utf-8"))

    def begin(self, run_id: str = "run-1") -> None:
        self.run_cli(
            "begin",
            "--path",
            str(self.status),
            "--run-id",
            run_id,
            "--legacy-retry-file",
            str(self.retry),
            "--legacy-alert-file",
            str(self.alert),
        )

    def finish(self, run_id: str = "run-1", *extra: str) -> None:
        self.run_cli(
            "finish",
            "--path",
            str(self.status),
            "--run-id",
            run_id,
            "--outcome",
            "idle",
            "--reason",
            "no_input",
            "--pending-count",
            "0",
            "--reconcile-dir",
            str(self.pending),
            "--checkpoint-path",
            str(self.checkpoint),
            "--checkpoint-before",
            "missing",
            *extra,
        )

    def test_begin_has_complete_running_record_and_legacy_failure_bootstrap(self) -> None:
        self.retry.write_text("3\n", encoding="utf-8")
        self.alert.write_text("legacy alert\n", encoding="utf-8")
        self.begin()
        value = self.load()
        self.assertEqual(value["schema_version"], "dma-runtime-status-v1")
        self.assertEqual(value["outcome"], "running")
        self.assertIsNone(value["finished_at"])
        self.assertEqual(value["failures"]["summary"]["consecutive"], 3)
        self.assertEqual(value["status_errors"], [])
        self.assertEqual(list(self.config.glob(".runtime_status.json.*.tmp")), [])

    def test_legacy_marker_evidence_latches_until_full_recovery(self) -> None:
        self.retry.write_text("not-a-count\n", encoding="utf-8")
        self.begin()
        first = self.load()
        self.assertIn("legacy_retry_invalid", first["status_errors"])

        self.finish()
        self.begin("run-2")
        second = self.load()
        self.assertIn("legacy_retry_invalid", second["status_errors"])

    def test_exit_zero_summary_failure_is_terminal_and_preserves_pending(self) -> None:
        self.checkpoint.write_text('{"cursor": 1}\n', encoding="utf-8")
        before = self.run_cli("fingerprint", "--file", str(self.checkpoint)).stdout.strip()
        (self.pending / "2026-09-09_08-00.json").write_text("[]\n", encoding="utf-8")
        self.begin()
        # This represents a handled cloud failure: archive-engine keeps its
        # historical exit-0 behavior while publishing a failed outcome.
        result = self.run_cli(
            "finish",
            "--path",
            str(self.status),
            "--run-id",
            "run-1",
            "--outcome",
            "failed",
            "--reason",
            "summary",
            "--pending-count",
            "4",
            "--oldest-pending-at",
            "2026-09-09T00:00:00+00:00",
            "--reconcile-dir",
            str(self.pending),
            "--checkpoint-path",
            str(self.checkpoint),
            "--checkpoint-before",
            before,
            "--storage-result",
            "success",
            "--summary-result",
            "failed",
        )
        self.assertEqual(result.returncode, 0)
        value = self.load()
        self.assertEqual(value["outcome"], "failed")
        self.assertEqual(value["reason"], "summary")
        self.assertEqual(value["pending_count"], 4)
        self.assertEqual(value["pending_reconcile_count"], 1)
        self.assertEqual(value["failures"]["summary"]["consecutive"], 1)
        self.assertIsNone(value["checkpoint_progress_at"])

    def test_idle_does_not_clear_summary_failure_but_summary_success_recovers_it(self) -> None:
        self.begin()
        self.finish("run-1", "--summary-result", "failed")
        failed = self.load()
        self.assertEqual(failed["failures"]["summary"]["consecutive"], 1)
        recovered_at_before = failed["failures"]["summary"]["last_recovered_at"]

        self.begin("run-2")
        self.run_cli(
            "finish",
            "--path",
            str(self.status),
            "--run-id",
            "run-2",
            "--outcome",
            "idle",
            "--reason",
            "no_input",
            "--pending-count",
            "0",
            "--reconcile-dir",
            str(self.pending),
            "--summary-result",
            "success",
        )
        recovered = self.load()
        self.assertEqual(recovered["failures"]["summary"]["consecutive"], 0)
        self.assertIsNotNone(recovered["failures"]["summary"]["last_recovered_at"])
        self.assertEqual(recovered_at_before, None)

    def test_storage_recovery_does_not_create_archive_failure(self) -> None:
        self.begin()
        self.run_cli(
            "finish",
            "--path",
            str(self.status),
            "--run-id",
            "run-1",
            "--outcome",
            "failed",
            "--reason",
            "storage",
            "--pending-count",
            "null",
            "--storage-result",
            "failed",
        )
        failed = self.load()
        self.assertEqual(failed["failures"]["storage"]["consecutive"], 1)
        self.assertEqual(failed["failures"]["archive"]["consecutive"], 0)

        self.begin("run-2")
        self.run_cli(
            "finish",
            "--path",
            str(self.status),
            "--run-id",
            "run-2",
            "--outcome",
            "idle",
            "--reason",
            "no_input",
            "--pending-count",
            "0",
            "--storage-result",
            "success",
        )
        recovered = self.load()
        self.assertEqual(recovered["failures"]["storage"]["consecutive"], 0)
        self.assertEqual(recovered["failures"]["archive"]["consecutive"], 0)

    def test_partial_archived_flag_does_not_advance_last_archived_at(self) -> None:
        self.checkpoint.write_text('{"cursor": 1}\n', encoding="utf-8")
        checkpoint_before = self.run_cli(
            "fingerprint", "--file", str(self.checkpoint)
        ).stdout.strip()
        self.begin()
        self.run_cli(
            "finish",
            "--path",
            str(self.status),
            "--run-id",
            "run-1",
            "--outcome",
            "archived",
            "--reason",
            "substantive",
            "--pending-count",
            "0",
            "--reconcile-dir",
            str(self.pending),
            "--checkpoint-path",
            str(self.checkpoint),
            "--checkpoint-before",
            checkpoint_before,
            "--storage-result",
            "success",
            "--summary-result",
            "success",
            "--archive-result",
            "success",
            "--archived",
            "1",
        )
        first = self.load()
        archived_at = first["last_archived_at"]
        self.assertIsNotNone(archived_at)

        self.begin("run-2")
        self.run_cli(
            "finish",
            "--path",
            str(self.status),
            "--run-id",
            "run-2",
            "--outcome",
            "partial",
            "--reason",
            "summary",
            "--pending-count",
            "0",
            "--reconcile-dir",
            str(self.pending),
            "--checkpoint-path",
            str(self.checkpoint),
            "--checkpoint-before",
            checkpoint_before,
            "--storage-result",
            "success",
            "--summary-result",
            "failed",
            "--archive-result",
            "success",
            "--archived",
            "1",
        )
        partial = self.load()
        self.assertEqual(partial["last_archived_at"], archived_at)

    def test_corrupt_previous_status_is_exposed_and_not_reset_silently(self) -> None:
        self.status.write_text("{broken", encoding="utf-8")
        self.begin()
        running = self.load()
        self.assertIn("previous_status_corrupt", running["status_errors"])
        self.finish()
        terminal = self.load()
        self.assertIn("previous_status_corrupt", terminal["status_errors"])

    def test_history_latch_requires_proven_full_archival_success(self) -> None:
        self.status.write_text("{broken", encoding="utf-8")
        self.checkpoint.write_text('{"cursor": 1}\n', encoding="utf-8")
        checkpoint_before = self.run_cli(
            "fingerprint", "--file", str(self.checkpoint)
        ).stdout.strip()
        self.begin()
        self.run_cli(
            "finish",
            "--path",
            str(self.status),
            "--run-id",
            "run-1",
            "--outcome",
            "archived",
            "--reason",
            "substantive",
            "--pending-count",
            "0",
            "--reconcile-dir",
            str(self.pending),
            "--checkpoint-path",
            str(self.checkpoint),
            "--checkpoint-before",
            checkpoint_before,
            "--storage-result",
            "unknown",
            "--summary-result",
            "unknown",
            "--archive-result",
            "unknown",
            "--archived",
            "1",
        )
        retained = self.load()
        self.assertIn("previous_status_corrupt", retained["status_errors"])

        self.begin("run-2")
        self.run_cli(
            "finish",
            "--path",
            str(self.status),
            "--run-id",
            "run-2",
            "--outcome",
            "archived",
            "--reason",
            "substantive",
            "--pending-count",
            "0",
            "--reconcile-dir",
            str(self.pending),
            "--checkpoint-path",
            str(self.checkpoint),
            "--checkpoint-before",
            checkpoint_before,
            "--storage-result",
            "success",
            "--summary-result",
            "success",
            "--archive-result",
            "success",
            "--archived",
            "1",
        )
        recovered = self.load()
        self.assertNotIn("previous_status_corrupt", recovered["status_errors"])

    def test_crash_boundary_leaves_running_record(self) -> None:
        self.begin("crashed-run")
        value = self.load()
        self.assertEqual(value["outcome"], "running")
        self.assertIsNone(value["finished_at"])

        self.begin("recovery-run")
        interrupted = self.load()
        self.assertIn("interrupted_previous_run", interrupted["status_errors"])
        self.run_cli(
            "finish",
            "--path",
            str(self.status),
            "--run-id",
            "recovery-run",
            "--outcome",
            "idle",
            "--reason",
            "no_input",
            "--pending-count",
            "0",
        )
        self.begin("later-run")
        still_interrupted = self.load()
        self.assertIn("interrupted_previous_run", still_interrupted["status_errors"])

    def test_run_id_mismatch_cannot_overwrite_active_owner(self) -> None:
        self.begin("owner-run")
        result = self.run_cli(
            "finish",
            "--path",
            str(self.status),
            "--run-id",
            "other-run",
            "--outcome",
            "failed",
            "--reason",
            "archive",
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.load()["run_id"], "owner-run")
        self.assertEqual(self.load()["outcome"], "running")

    def test_unknown_evidence_stays_null(self) -> None:
        self.begin()
        self.run_cli(
            "finish",
            "--path",
            str(self.status),
            "--run-id",
            "run-1",
            "--outcome",
            "failed",
            "--reason",
            "storage",
            "--pending-count",
            "null",
            "--storage-result",
            "failed",
            "--status-error",
            "pending_snapshot_unknown",
        )
        value = self.load()
        self.assertIsNone(value["pending_count"])
        self.assertIsNone(value["pending_reconcile_count"])
        self.assertIn("reconcile_snapshot_unknown", value["status_errors"])
        self.assertIn("checkpoint_evidence_unknown", value["status_errors"])


if __name__ == "__main__":
    unittest.main()
