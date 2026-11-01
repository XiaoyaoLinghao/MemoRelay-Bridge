"""Vendored frozen DMA/KW contract v1 (source of truth: KW contracts/v1).

DMA copies a checksummed release so it can validate what it stores and sends
without importing the KW package. ``scripts/sync_contracts.py`` refreshes the
copy and records the source commit/version in ``VENDORED.json``.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

CONTRACT_VERSION = "1.0"
SUPPORTED_SCHEMA_VERSIONS = ("1.0",)
REDACTION_POLICY_VERSIONS = ("redact:1",)
TOKEN_RE = re.compile(r"^[A-Za-z0-9._:-]+$")
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")

CONTRACT_FILES = (
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

_cache: dict[str, Any] = {}


class ContractError(RuntimeError):
    pass


def contracts_dir() -> Path:
    here = Path(__file__).resolve()
    candidates = [
        Path(os.environ["DMA_CONTRACTS_DIR"]) if os.environ.get("DMA_CONTRACTS_DIR") else None,
        here.parents[2] / "contracts" / "v1" if len(here.parents) > 2 else None,
        Path.cwd() / "contracts" / "v1",
        here.parent / "contracts_v1",
    ]
    for candidate in candidates:
        if candidate is None:
            continue
        if (candidate / "VERSION.json").is_file() and (candidate / "openapi.json").is_file():
            return candidate
    raise ContractError(
        "vendored contract directory not found; run scripts/sync_contracts.py or install a packaged wheel"
    )


def load(name: str) -> Any:
    if name in _cache:
        return _cache[name]
    if name not in CONTRACT_FILES:
        raise ContractError(f"{name!r} is not a frozen contract file")
    packaged = Path(__file__).resolve().parent / "contracts_v1"
    candidates = []
    try:
        candidates.append(contracts_dir())
    except ContractError:
        pass
    candidates.append(packaged)
    for base in candidates:
        path = base / name
        if path.is_file():
            value = json.loads(path.read_text(encoding="utf-8"))
            _cache[name] = value
            return value
    raise ContractError(f"missing vendored contract file: {name}")


def vendored_info() -> dict:
    path = contracts_dir() / "VENDORED.json"
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def redaction_policy() -> dict:
    return load("redaction-policy.json")


def redaction_examples() -> dict:
    return load("redaction-examples.json")


def valid_examples() -> dict:
    return load("valid-examples.json")


def invalid_examples() -> list:
    return load("invalid-examples.json")


def version_info() -> dict:
    return load("VERSION.json")


def error_catalog() -> dict:
    return load("error-catalog.json")


def error_entry(code: str | None) -> dict | None:
    if not code:
        return None
    for entry in error_catalog().get("entries", []):
        if entry.get("code") == code:
            return entry
    return None


def is_retryable(code: str | None) -> bool:
    entry = error_entry(code)
    return bool(entry and entry.get("retryable"))


def verify_checksums() -> list[str]:
    directory = contracts_dir()
    issues: list[str] = []
    sums = directory / "SHA256SUMS"
    if not sums.is_file():
        return ["SHA256SUMS missing"]
    declared: dict[str, str] = {}
    for line in sums.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) != 2 or not SHA256_RE.match(parts[0]):
            issues.append(f"malformed SHA256SUMS entry: {line!r}")
            continue
        declared[parts[1]] = parts[0]
    import hashlib

    for name in CONTRACT_FILES:
        expected = declared.get(name)
        path = directory / name
        if expected is None:
            issues.append(f"{name} not covered by SHA256SUMS")
            continue
        if not path.is_file():
            issues.append(f"{name} missing")
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != expected:
            issues.append(f"{name} checksum mismatch")
    extra = set(declared) - set(CONTRACT_FILES)
    if extra:
        issues.append(f"undeclared contract files: {sorted(extra)}")
    return issues
