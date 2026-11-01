#!/usr/bin/env python3
"""Vendor the frozen KW contract v1 into the DMA repository with provenance.

KW's `contracts/v1` is the source of truth (state/DECISIONS.md D005). This
script copies the exact frozen files and records the *contract content commit*
— the last KW commit that touched ``contracts/v1`` — plus per-file SHA-256 in
``contracts/v1/VENDORED.json``. It never edits the contract content and never
pushes anything.

F18 decision (T20 remediation): the contract release is **pinned to the commit
that defines the contract content**, not to an arbitrary current KW HEAD. A
later KW code commit that does not touch ``contracts/v1`` does not invalidate
the vendoring. ``--check`` therefore verifies:

1. every file actually present in the target (FILES + EXTRA) is re-hashed from
   its real bytes and must equal the current KW ``contracts/v1`` content;
2. the ``VENDORED.json`` per-file record must describe those actual bytes, so
   editing the target file or the manifest cannot pass;
3. the recorded pin actually contains those exact file bytes (read with
   ``git show <pin>:contracts/v1/<file>``); ``files_at_pin`` in the manifest is
   never trusted as its own proof;
4. the pin is an ancestor of the current KW HEAD;
5. KW has not committed a newer ``contracts/v1`` change since the pin (which
   requires an explicit re-vendor, never a silent pass);
6. ``SHA256SUMS`` matches the KW source listing and every entry describes the
   actual vendored bytes.

Usage:
  python scripts/sync_contracts.py --source ../MemoRelay-Core
  python scripts/sync_contracts.py --source ../MemoRelay-Core --check
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TARGET = REPO_ROOT / "contracts" / "v1"
CONTRACT_TREE = "contracts/v1"
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


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git_value(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True
    )
    return completed.stdout.strip() if completed.returncode == 0 else ""


def git_bytes(repo: Path, *args: str) -> bytes:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True
    )
    return completed.stdout if completed.returncode == 0 else b""


def contract_content_commit(source_root: Path) -> str:
    """The last KW commit touching contracts/v1 (the release pin)."""
    return git_value(source_root, "log", "-1", "--format=%H", "--", CONTRACT_TREE)


def files_at_commit(source_root: Path, commit: str) -> dict[str, str]:
    """Per-file SHA-256 of the contract files as stored at ``commit``."""
    hashes: dict[str, str] = {}
    if not commit:
        return hashes
    for name in FILES:
        blob = git_bytes(source_root, "show", f"{commit}:{CONTRACT_TREE}/{name}")
        if not blob:
            return {}
        hashes[name] = hashlib.sha256(blob).hexdigest()
    return hashes


def contract_content_digest(files: dict[str, str]) -> str:
    payload = "".join(f"{name}\0{digest}\n" for name, digest in sorted(files.items()))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def read_sha256sums(path: Path) -> dict[str, str] | None:
    """Parse a ``SHA256SUMS`` file into ``{name: digest}`` (``None`` if malformed)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    entries: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        fields = stripped.split()
        if len(fields) != 2:
            return None
        digest, name = fields
        digest = digest.lower()
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            return None
        entries[name.lstrip("*")] = digest
    return entries or None


def pin_blob_hashes(source_root: Path, commit: str, names) -> tuple[dict[str, str], list[str]]:
    """SHA-256 of the *real* git blobs at ``commit`` (never the manifest self-report)."""
    hashes: dict[str, str] = {}
    missing: list[str] = []
    if not commit:
        return hashes, list(names)
    for name in names:
        blob = git_bytes(source_root, "show", f"{commit}:{CONTRACT_TREE}/{name}")
        if not blob:
            missing.append(name)
        else:
            hashes[name] = hashlib.sha256(blob).hexdigest()
    return hashes, missing


def check_actual_target_bytes(target: Path, source: Path) -> tuple[dict[str, str], list[str]]:
    """Re-hash the files that actually sit in the vendored target directory (F18).

    FILES and EXTRA are both part of the frozen release, so both are verified
    against the current KW source content. The returned mapping is authoritative
    for every later comparison; a tampered ``VENDORED.json`` cannot hide a
    modified target file.
    """
    actual: dict[str, str] = {}
    problems: list[str] = []
    for name in FILES + EXTRA:
        target_path = target / name
        if not target_path.is_file():
            problems.append(f"vendored target is missing required contract file: {name}")
            continue
        actual[name] = sha256_file(target_path)
        source_path = source / name
        if not source_path.is_file():
            problems.append(f"KW source contract file is missing: {name}")
        elif actual[name] != sha256_file(source_path):
            problems.append(
                f"vendored file hashes differ from the KW contracts/v1 source: {name} "
                f"(target={actual[name]}, source={sha256_file(source_path)})"
            )
    return actual, problems


def is_ancestor(source_root: Path, ancestor: str, descendant: str) -> bool:
    if not ancestor or not descendant:
        return False
    completed = subprocess.run(
        ["git", "-C", str(source_root), "merge-base", "--is-ancestor", ancestor, descendant],
        capture_output=True,
    )
    return completed.returncode == 0


def build_manifest(source_root: Path) -> dict:
    source = source_root / CONTRACT_TREE
    pin = contract_content_commit(source_root)
    head = git_value(source_root, "rev-parse", "HEAD")
    files = {name: sha256_file(source / name) for name in FILES}
    at_pin = files_at_commit(source_root, pin)
    return {
        "contract_id": "dma-kw-shared-memory",
        "contract_version": json.loads((source / "VERSION.json").read_text())["contract_version"],
        "source_repository": "MemoRelay-Core",
        "source_path": str(source),
        "source_commit": pin or "",
        "source_contract_commit": pin or "",
        "source_head_at_vendor": head,
        "source_worktree_clean": git_value(source_root, "status", "--porcelain") == "",
        "contract_content_sha256": contract_content_digest(files),
        "vendored_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "files": files,
        "files_at_pin": at_pin,
        "pin_has_recorded_content": bool(at_pin) and at_pin == files,
        "sha256sums_sha256": sha256_file(source / "SHA256SUMS"),
        "note": (
            "Vendored release of KW contracts/v1 pinned to the last commit touching "
            "contracts/v1. Later KW code commits do not invalidate the vendoring; a "
            "new contracts/v1 commit requires re-vendoring. DMA validates this copy "
            "and never edits contract content."
        ),
    }


def check(target: Path, source_root: Path) -> int:
    """Verify the vendored release from the *actual target bytes* plus git provenance."""
    current = target / "VENDORED.json"
    if not current.is_file():
        print("vendored contract missing; run sync_contracts.py", file=sys.stderr)
        return 1
    try:
        existing = json.loads(current.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"vendored VENDORED.json is unreadable: {exc}", file=sys.stderr)
        return 1
    if not isinstance(existing, dict):
        print("vendored VENDORED.json must contain a JSON object", file=sys.stderr)
        return 1
    source = source_root / CONTRACT_TREE
    manifest = build_manifest(source_root)
    problems: list[str] = []

    # 1. Hash the files that are actually vendored: the manifest record is only a
    #    self-report and can be edited alongside the target file.
    actual, byte_problems = check_actual_target_bytes(target, source)
    problems.extend(byte_problems)
    recorded_files = existing.get("files")
    if not isinstance(recorded_files, dict) or set(recorded_files) != set(FILES):
        problems.append(
            "vendored manifest does not record exactly the expected contract files "
            "(files field missing or tampered)"
        )
    else:
        for name in FILES:
            if name in actual and recorded_files.get(name) != actual[name]:
                problems.append(
                    f"VENDORED.json recorded hash for {name} does not match the actual "
                    "vendored bytes (the manifest was edited or the target file changed)"
                )
        if recorded_files != manifest["files"]:
            # Kept as a distinct diagnostic: the recorded release must describe the
            # frozen KW source content, even if the docstrings above change wording.
            problems.append("vendored file hashes differ from the KW contracts/v1 source")
            for name in FILES:
                if recorded_files.get(name) != manifest["files"].get(name):
                    problems.append(
                        f"recorded hash drift: {name} is {recorded_files.get(name)} in "
                        f"VENDORED.json but {manifest['files'].get(name)} in the KW source"
                    )

    # 2. Provenance: pin exists, is the last contracts/v1 commit, is an ancestor of
    #    the current KW HEAD (a later code commit must not invalidate the release).
    recorded_pin = existing.get("source_contract_commit") or existing.get("source_commit")
    if not isinstance(recorded_pin, str):
        recorded_pin = ""
    current_pin = manifest["source_contract_commit"]
    head = manifest["source_head_at_vendor"]
    if not recorded_pin:
        problems.append("vendored manifest has no contract content commit pin")
    if recorded_pin != current_pin:
        problems.append(
            f"contract provenance drift: pinned {recorded_pin} but KW contracts/v1 was "
            f"last changed by {current_pin}; re-vendor explicitly"
        )
    if not is_ancestor(source_root, recorded_pin, head):
        problems.append(
            f"pinned contract commit {recorded_pin} is not an ancestor of the current KW HEAD {head}"
        )

    # 3. Read the real pin blobs: the pinned commit must contain exactly the bytes
    #    that are vendored. ``files_at_pin`` in the manifest is never trusted.
    pin_hashes, pin_missing = pin_blob_hashes(source_root, recorded_pin, FILES + EXTRA)
    if pin_missing:
        problems.append(
            f"pinned contract commit {recorded_pin} does not contain the expected "
            f"contract files: {', '.join(pin_missing)}"
        )
    else:
        for name in FILES + EXTRA:
            if name in actual and actual[name] != pin_hashes[name]:
                problems.append(
                    f"vendored target bytes do not match the pinned contract commit "
                    f"{recorded_pin}: {name} (target={actual[name]}, pin={pin_hashes[name]})"
                )
        recorded_at_pin = existing.get("files_at_pin")
        if isinstance(recorded_at_pin, dict):
            for name in FILES:
                if recorded_at_pin.get(name) != pin_hashes[name]:
                    problems.append(
                        f"VENDORED.json files_at_pin entry for {name} does not match the "
                        "actual pinned commit blob (self-reported provenance is tampered)"
                    )

    # 4. SHA256SUMS is the published second check: it must equal the KW source
    #    listing and every listed digest must describe the actual vendored bytes.
    source_sums = read_sha256sums(source / "SHA256SUMS")
    target_sums = read_sha256sums(target / "SHA256SUMS")
    if source_sums is None:
        problems.append("KW contracts/v1 SHA256SUMS is missing or malformed")
    if target_sums is None:
        problems.append("vendored SHA256SUMS is missing or malformed")
    elif source_sums is not None:
        if target_sums != source_sums:
            problems.append("vendored SHA256SUMS does not match the KW contracts/v1 SHA256SUMS")
        for name in FILES:
            expected = target_sums.get(name)
            if expected is None:
                problems.append(f"vendored SHA256SUMS does not list {name}")
            elif name in actual and expected != actual[name]:
                problems.append(
                    f"vendored SHA256SUMS entry for {name} does not match the actual vendored bytes"
                )
    recorded_sums_hash = existing.get("sha256sums_sha256")
    if (
        "SHA256SUMS" in actual
        and recorded_sums_hash is not None
        and recorded_sums_hash != actual["SHA256SUMS"]
    ):
        problems.append(
            "VENDORED.json sha256sums_sha256 does not match the actual vendored SHA256SUMS bytes"
        )
    recorded_content = existing.get("contract_content_sha256")
    if recorded_content is not None and set(FILES) <= set(actual):
        actual_digest = contract_content_digest({name: actual[name] for name in FILES})
        if recorded_content != actual_digest:
            problems.append(
                "VENDORED.json contract_content_sha256 does not match the actual vendored file bytes"
            )

    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1
    print(
        "vendored contract matches KW contracts/v1: "
        f"pin={recorded_pin} kw_head={head} "
        f"content_sha256={manifest['contract_content_sha256']} "
        f"files={len(manifest['files'])}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "knowledge-weaver",
        help="path to the knowledge-weaver checkout",
    )
    parser.add_argument(
        "--target",
        type=Path,
        default=TARGET,
        help="vendored contract directory (default: this repository's contracts/v1)",
    )
    parser.add_argument("--check", action="store_true", help="verify only; do not copy")
    args = parser.parse_args(argv)
    source_root = args.source.resolve()
    source = source_root / CONTRACT_TREE
    target = args.target.resolve()
    for name in FILES + EXTRA:
        if not (source / name).is_file():
            print(f"missing source contract file: {name}", file=sys.stderr)
            return 2
    if args.check:
        return check(target, source_root)
    manifest = build_manifest(source_root)
    if not manifest["source_contract_commit"]:
        print(
            "cannot determine the KW contracts/v1 commit; refusing to vendor without provenance",
            file=sys.stderr,
        )
        return 2
    if not manifest["pin_has_recorded_content"]:
        print(
            "KW contract working tree differs from the pinned contract commit; "
            "commit the contract change first",
            file=sys.stderr,
        )
        return 2
    target.mkdir(parents=True, exist_ok=True)
    for name in FILES + EXTRA:
        shutil.copyfile(source / name, target / name)
    (target / "VENDORED.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "vendored": str(target),
                "contract_version": manifest["contract_version"],
                "source_contract_commit": manifest["source_contract_commit"],
                "kw_head_at_vendor": manifest["source_head_at_vendor"],
                "contract_content_sha256": manifest["contract_content_sha256"],
                "files": len(manifest["files"]),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
