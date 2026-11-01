"""T04: shared redact:1 policy cases must pass on the DMA redaction side."""
from __future__ import annotations

import pytest

from dma import contracts
from dma.redaction import Redactor, contains_secret, secret_kinds

CASES = contracts.redaction_examples()["cases"]


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_shared_redaction_case(case):
    redactor = Redactor()
    result = redactor.redact(case["raw"])
    assert result.status == case["expected_status"], case["id"]
    assert set(case["expect_kinds"]) <= set(result.kinds), case["id"]
    if case["expected_status"] == "passed":
        assert result.text == case["raw"]
        assert result.replacements == 0
        assert result.credential_refs == []
    else:
        assert result.replacements >= 1
        for secret in case["must_not_contain"]:
            assert secret not in result.text, case["id"]
        assert result.credential_refs
        assert all("CANARY" not in ref for ref in result.credential_refs)
    if case["expected_status"] == "withheld":
        assert result.text == ""


def test_redacted_output_is_clean_and_idempotent():
    redactor = Redactor()
    result = redactor.redact("数据库密码: CANARY-PW-7f3a")
    assert "[REDACTED:PASSWORD_ASSIGNMENT]" in result.text
    second = redactor.redact(result.text)
    assert second.status == "passed"
    assert second.replacements == 0


def test_known_secret_redaction(monkeypatch):
    redactor = Redactor(known_secrets=["CANARY-INTERNAL-SECRET-42"])
    result = redactor.redact("连接备注 CANARY-INTERNAL-SECRET-42 结束")
    assert result.status == "redacted"
    assert "CANARY-INTERNAL-SECRET-42" not in result.text
    assert "[REDACTED:KNOWN_SECRET]" in result.text
    assert result.credential_refs


def test_detection_helpers_match_policy():
    assert contains_secret("令牌 eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJl")
    assert "jwt" in secret_kinds("令牌 eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJl")
    assert not contains_secret("松塔项目端口改为4200")


def test_multiple_secrets_in_one_message_all_removed():
    raw = "密码: CANARY-PW-1；API sk-CANARY-API-91b2c3d4e5f6g7h8；普通内容保留"
    result = Redactor().redact(raw)
    assert "CANARY-PW-1" not in result.text
    assert "CANARY-API-91b2c3d4e5f6g7h8" not in result.text
    assert "普通内容保留" in result.text
    assert result.replacements == 2


def test_private_key_message_is_withheld():
    raw = (
        "-----BEGIN PRIVATE KEY-----\n"
        "MIIBCgKCAQEA CANARY-PRIVATE-KEY-SYNTHETIC\n"
        "-----END PRIVATE KEY-----"
    )
    result = Redactor().redact(raw)
    assert result.status == "withheld"
    assert result.text == ""
