"""T01/T03: DMA must consume the exact frozen KW contract release."""
from __future__ import annotations

import json

from dma import contracts
from dma.ids import EMPTY_BODY_SHA256, body_digest, canonical_event_id


def test_vendored_contract_checksums_pass():
    assert contracts.verify_checksums() == []


def test_vendored_manifest_records_source_and_version():
    info = contracts.vendored_info()
    assert info, "run scripts/sync_contracts.py"
    version = contracts.version_info()
    assert info["contract_version"] == version["contract_version"] == contracts.CONTRACT_VERSION
    assert len(info["source_commit"]) == 40
    assert info["source_worktree_clean"] is True
    assert set(info["files"]) == set(contracts.CONTRACT_FILES)


def test_shared_event_example_matches_dma_identity_helpers():
    event = contracts.valid_examples()["Event"]
    source = event["source"]
    assert canonical_event_id(event["workspace_id"], source["instance_id"], source["native_event_id"]) == event["event_id"]
    assert body_digest(event["body"]["text"]) == event["body"]["sha256"]


def test_retract_empty_body_digest_matches_contract():
    assert EMPTY_BODY_SHA256 == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_error_catalog_codes_are_available_to_dma():
    required = {
        "REDACTION_REQUIRED",
        "UNSUPPORTED_SCHEMA",
        "INVALID_REQUEST",
        "UNREGISTERED_CLIENT",
        "SCOPE_DENIED",
        "PAYLOAD_TOO_LARGE",
        "EVENT_CONFLICT",
        "PARENT_PENDING",
        "SOURCE_EXPIRED",
        "SOURCE_NOT_FOUND",
        "MODEL_UNAVAILABLE",
        "EMBEDDING_MISMATCH",
        "STORE_UNAVAILABLE",
        "RESTORE_IN_PROGRESS",
        "STALE_CURSOR",
    }
    codes = {entry["code"] for entry in contracts.error_catalog()["entries"]}
    assert required <= codes
    assert contracts.is_retryable("PARENT_PENDING") is True
    assert contracts.is_retryable("EVENT_CONFLICT") is False
    assert contracts.error_entry("STORE_UNAVAILABLE")["http_status"] == 503
