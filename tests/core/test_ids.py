"""T03: canonical identity, digest and chunking helpers."""
from __future__ import annotations

from dma import contracts
from dma.ids import EMPTY_BODY_SHA256, body_digest, canonical_event_id, split_text


def test_event_id_matches_frozen_baseline_sample():
    assert (
        canonical_event_id("owner:demo", "instance:hermes-test", "message:17")
        == "evt:4ccc6ba8b5f8693f4cdb5421453404b7da8c44f94c2a99c6de2638506abb3005"
    )


def test_body_digest_matches_frozen_baseline_sample():
    assert body_digest("松塔项目端口改为4200。") == (
        "6219e53be98e0cc6873f5c99a8f988fb5efd7f3fc3ccf8110925525a89df7776"
    )
    assert EMPTY_BODY_SHA256 == body_digest("")


def test_same_native_id_on_two_instances_differs_globally():
    a = canonical_event_id("owner:demo", "instance:a", "message:17")
    b = canonical_event_id("owner:demo", "instance:b", "message:17")
    assert a != b and a.startswith("evt:")


def test_split_text_never_loses_or_reorders_content():
    text = "".join(f"第{i}行：端口与备份记录。\n" for i in range(500))
    parts = split_text(text, max_chars=100, max_bytes=400)
    assert "".join(parts) == text
    assert all(len(part.encode("utf-8")) <= 400 for part in parts)
    assert len(parts) > 1


def test_split_text_default_limits_are_exact():
    text = "a" * 250_001
    parts = split_text(text)
    assert len(parts) == 2
    assert parts[0] == "a" * 250_000
    assert "".join(parts) == text


def test_split_text_cjk_respects_byte_ceiling():
    text = "中" * 10
    parts = split_text(text, max_bytes=7)  # 3 bytes per char -> at most 2 chars
    assert "".join(parts) == text
    assert all(len(part.encode("utf-8")) <= 7 for part in parts)


def test_split_text_empty_yields_single_empty_part():
    assert split_text("") == [""]


def test_vendored_contract_policy_version_is_supported():
    assert contracts.redaction_policy()["policy_version"] in contracts.REDACTION_POLICY_VERSIONS
