"""F18 regression: contract provenance is pinned to the contract content commit.

The old check compared the recorded commit with the *current* KW HEAD, so every
unrelated KW code commit made ``--check`` fail while the contract content was
byte-identical. The pinned-release check verifies content identity plus git
provenance and refuses anything that would hide a real contract change.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

DMA_ROOT = Path(__file__).resolve().parents[2]
KW_ROOT = DMA_ROOT.parent / "MemoRelay-Core"
if not KW_ROOT.is_dir():
    KW_ROOT = DMA_ROOT.parent / "core"
SCRIPT = DMA_ROOT / "scripts" / "sync_contracts.py"


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], capture_output=True, text=True
    )


def _kw_head() -> str:
    return subprocess.run(
        ["git", "-C", str(KW_ROOT), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()


def test_contract_check_passes_against_the_workspace_source():
    assert KW_ROOT.is_dir(), "the KW source checkout is required in this workspace"
    result = _run("--source", str(KW_ROOT), "--check")
    assert result.returncode == 0, result.stderr
    assert "content_sha256=" in result.stdout
    info = json.loads((DMA_ROOT / "contracts" / "v1" / "VENDORED.json").read_text())
    assert info["source_contract_commit"]
    assert info["pin_has_recorded_content"] is True
    contract_commit = subprocess.run(
        ["git", "-C", str(KW_ROOT), "log", "-1", "--format=%H", "--", "contracts/v1"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert info["source_contract_commit"] == contract_commit


def _copy_vendored(tmp_path: Path) -> Path:
    target = tmp_path / "vendored"
    shutil.copytree(DMA_ROOT / "contracts" / "v1", target)
    return target


def test_check_rejects_a_pin_that_is_not_the_contract_commit(tmp_path):
    target = _copy_vendored(tmp_path)
    manifest_path = target / "VENDORED.json"
    manifest = json.loads(manifest_path.read_text())
    # Pointing the pin at the current (unrelated) KW HEAD must not pass.
    manifest["source_contract_commit"] = _kw_head()
    manifest["source_commit"] = manifest["source_contract_commit"]
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    result = _run("--source", str(KW_ROOT), "--check", "--target", str(target))
    assert result.returncode == 1
    assert "provenance" in result.stderr.lower() or "drift" in result.stderr.lower()


def test_check_rejects_content_drift_between_vendored_and_source(tmp_path):
    target = _copy_vendored(tmp_path)
    manifest_path = target / "VENDORED.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files"]["error-catalog.json"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    result = _run("--source", str(KW_ROOT), "--check", "--target", str(target))
    assert result.returncode == 1
    assert "hashes differ" in result.stderr or "provenance mismatch" in result.stderr


def test_check_rejects_uncommitted_contract_change_in_source(tmp_path):
    """A dirty contracts/v1 tree must not be vendored/verified as a release."""
    target = _copy_vendored(tmp_path)
    fake_kw = tmp_path / "fake-kw"
    (fake_kw / "contracts" / "v1").mkdir(parents=True)
    for name in (
        "openapi.json",
        "extraction-output.schema.json",
        "semantic-invariants.json",
        "error-catalog.json",
        "valid-examples.json",
        "invalid-examples.json",
        "redaction-policy.json",
        "redaction-examples.json",
        "VERSION.json",
        "README.md",
        "SHA256SUMS",
    ):
        shutil.copyfile(DMA_ROOT / "contracts" / "v1" / name, fake_kw / "contracts" / "v1" / name)
    # No git history at all -> no provenance; a real checkout is required.
    result = _run("--source", str(fake_kw), "--check", "--target", str(target))
    assert result.returncode == 1
    assert "provenance" in result.stderr or "pin" in result.stderr
