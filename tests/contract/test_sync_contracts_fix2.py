"""F18 round-3 regression: the vendor check must verify *actual target bytes*.

Codex round-2 finding: ``sync_contracts.py --check`` compared the recorded
``VENDORED.json`` file hashes with the KW source instead of re-hashing the files
that actually sit in the target directory. A newline appended to a target
contract file (or a modified ``SHA256SUMS``/``README.md``) still exited 0.

These tests run the real script against the real frozen KW ``contracts/v1``
source and a temporary copy of DMA's vendored release; every mutation is
synthetic and limited to ``tmp_path``.
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
VENDORED = DMA_ROOT / "contracts" / "v1"
FILES = (
    "openapi.json",
    "extraction-output.schema.json",
    "semantic-invariants.json",
    "error-catalog.json",
    "valid-examples.json",
    "invalid-examples.json",
    "redaction-policy.json",
    "redaction-examples.json",
    "VERSION.json",
)
EXTRA = ("README.md", "SHA256SUMS")

pytestmark = pytest.mark.skipif(
    not KW_ROOT.is_dir(), reason="the KW source checkout is required in this workspace"
)


def _run(target: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--source",
            str(KW_ROOT),
            "--target",
            str(target),
            "--check",
        ],
        capture_output=True,
        text=True,
    )


@pytest.fixture
def vendored(tmp_path: Path) -> Path:
    target = tmp_path / "vendored"
    shutil.copytree(VENDORED, target)
    return target


def _manifest(target: Path) -> dict:
    return json.loads((target / "VENDORED.json").read_text())


def _write_manifest(target: Path, manifest: dict) -> None:
    (target / "VENDORED.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def test_clean_vendored_target_passes(vendored):
    result = _run(vendored)
    assert result.returncode == 0, result.stderr
    assert "matches KW contracts/v1" in result.stdout
    assert "content_sha256=" in result.stdout
    assert "pin=" in result.stdout


def test_check_rejects_appended_bytes_in_actual_target(vendored):
    """Codex counterexample: hash the file that is actually vendored, not the record."""
    path = vendored / "openapi.json"
    path.write_text(path.read_text() + "\n", encoding="utf-8")
    result = _run(vendored)
    assert result.returncode != 0, {"stdout": result.stdout, "stderr": result.stderr}
    assert "openapi.json" in result.stderr
    # The manifest still reports the old (correct) hash; only the real bytes changed.
    assert _manifest(vendored)["files"]["openapi.json"] != "0" * 64


def test_check_rejects_missing_actual_target_file(vendored):
    (vendored / "semantic-invariants.json").unlink()
    result = _run(vendored)
    assert result.returncode != 0, {"stdout": result.stdout, "stderr": result.stderr}
    assert "semantic-invariants.json" in result.stderr


@pytest.mark.parametrize("name", EXTRA)
def test_check_rejects_missing_target_extra_file(vendored, name):
    """README.md/SHA256SUMS are not in VENDORED.json files; the tree must still be complete."""
    (vendored / name).unlink()
    result = _run(vendored)
    assert result.returncode != 0, {"stdout": result.stdout, "stderr": result.stderr}
    assert name in result.stderr


def test_check_rehashes_every_contract_file(vendored):
    """Every FILES entry must be re-hashed from its actual bytes (not just the record)."""
    for name in FILES:
        path = vendored / name
        path.write_text(path.read_text(encoding="utf-8") + " ", encoding="utf-8")
    result = _run(vendored)
    assert result.returncode != 0, {"stdout": result.stdout, "stderr": result.stderr}
    for name in FILES:
        assert name in result.stderr, {"missing_from_stderr": name, "stderr": result.stderr}


def test_check_rejects_tampered_vendored_manifest_files(vendored):
    manifest = _manifest(vendored)
    manifest["files"]["openapi.json"] = "0" * 64
    _write_manifest(vendored, manifest)
    result = _run(vendored)
    assert result.returncode != 0, {"stdout": result.stdout, "stderr": result.stderr}
    stderr = result.stderr.lower()
    assert "openapi.json" in stderr
    assert "manifest" in stderr or "recorded" in stderr or "hashes differ" in stderr


def test_check_rejects_tampered_sha256sums(vendored):
    path = vendored / "SHA256SUMS"
    path.write_text(path.read_text() + "\n", encoding="utf-8")
    result = _run(vendored)
    assert result.returncode != 0, {"stdout": result.stdout, "stderr": result.stderr}
    assert "SHA256SUMS" in result.stderr


def test_check_rejects_tampered_sha256sums_entry(vendored):
    path = vendored / "SHA256SUMS"
    lines = path.read_text().splitlines()
    lines[0] = "0" * 64 + "  openapi.json"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = _run(vendored)
    assert result.returncode != 0, {"stdout": result.stdout, "stderr": result.stderr}
    assert "SHA256SUMS" in result.stderr


def test_check_rejects_tampered_extra_contract_file(vendored):
    path = vendored / "README.md"
    path.write_text(path.read_text() + "\n", encoding="utf-8")
    result = _run(vendored)
    assert result.returncode != 0, {"stdout": result.stdout, "stderr": result.stderr}
    assert "README.md" in result.stderr


def test_check_reads_real_pin_blobs_not_the_manifest_self_report(vendored):
    """A tampered ``files_at_pin`` self-report is detected from the real git blobs."""
    manifest = _manifest(vendored)
    manifest["files_at_pin"]["openapi.json"] = "0" * 64
    _write_manifest(vendored, manifest)
    result = _run(vendored)
    assert result.returncode != 0, {"stdout": result.stdout, "stderr": result.stderr}
    assert "files_at_pin" in result.stderr


def test_check_rejects_tampered_contract_content_digest(vendored):
    manifest = _manifest(vendored)
    manifest["contract_content_sha256"] = "0" * 64
    _write_manifest(vendored, manifest)
    result = _run(vendored)
    assert result.returncode != 0, {"stdout": result.stdout, "stderr": result.stderr}
    assert "contract_content_sha256" in result.stderr
