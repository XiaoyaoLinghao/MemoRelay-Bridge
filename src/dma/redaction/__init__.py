"""redact:1 sanitizer for the DMA adapter layer (SEC-01).

Sanitization happens on the **full logical message**, before splitting into
parts and before any outbox, cache, log, export or model write. The rule data
is the shared frozen contract file ``contracts/v1/redaction-policy.json`` so the
KW entry re-check detects exactly what the adapter was required to remove.

No secret value is returned, logged or stored: replacement labels only carry
the rule kind, and credential refs are ordinal non-secret tokens.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .. import contracts

_KNOWN_SECRET_KIND = "known_secret"


@dataclass
class RedactionResult:
    text: str
    status: str  # passed | redacted | withheld
    replacements: int
    credential_refs: list[str] = field(default_factory=list)
    kinds: list[str] = field(default_factory=list)

    def contract(self) -> dict:
        return {
            "policy_version": contracts.redaction_policy()["policy_version"],
            "status": self.status,
            "replacements": self.replacements,
            "credential_refs": list(self.credential_refs),
        }


@dataclass
class _Finding:
    start: int
    end: int
    kind: str


def _compile_rules(policy: dict) -> list[tuple[str, re.Pattern]]:
    return [(rule["kind"], re.compile(rule["pattern"])) for rule in policy["rules"]]


class Redactor:
    def __init__(self, *, known_secrets=(), policy: dict | None = None) -> None:
        self.policy = policy or contracts.redaction_policy()
        self._rules = _compile_rules(self.policy)
        self._withhold = frozenset(self.policy.get("withhold_kinds") or [])
        self._template = self.policy["replacement_template"]
        # Known secrets are runtime references (e.g. canaries for tests); they are
        # never written to the policy file or to logs.
        self._known_secrets = [secret for secret in known_secrets if secret]

    def scan(self, text: str) -> list[_Finding]:
        if not isinstance(text, str) or not text:
            return []
        findings: list[_Finding] = []
        for kind, pattern in self._rules:
            for match in pattern.finditer(text):
                findings.append(_Finding(match.start(), match.end(), kind))
        for secret in self._known_secrets:
            start = 0
            while True:
                index = text.find(secret, start)
                if index < 0:
                    break
                findings.append(_Finding(index, index + len(secret), _KNOWN_SECRET_KIND))
                start = index + len(secret)
        if not findings:
            return []
        findings.sort(key=lambda finding: (finding.start, -finding.end))
        merged: list[_Finding] = []
        for finding in findings:
            if merged and finding.start < merged[-1].end:
                if finding.end > merged[-1].end:
                    merged[-1] = _Finding(merged[-1].start, finding.end, merged[-1].kind)
            else:
                merged.append(finding)
        return merged

    def redact(self, text: str) -> RedactionResult:
        if not isinstance(text, str):
            raise TypeError("redaction input must be text")
        findings = self.scan(text)
        if not findings:
            return RedactionResult(text=text, status="passed", replacements=0)
        kinds: list[str] = []
        for finding in findings:
            if finding.kind not in kinds:
                kinds.append(finding.kind)
        refs = [f"credref:{kind}:{index + 1}" for index, kind in enumerate(kinds)]
        if self._withhold & set(kinds):
            # A withheld message never reaches the outbox with a body; the caller
            # stores a receipt-only event and re-reads the native source later.
            return RedactionResult(
                text="",
                status="withheld",
                replacements=len(findings),
                credential_refs=refs,
                kinds=kinds,
            )
        parts: list[str] = []
        cursor = 0
        for finding in findings:
            parts.append(text[cursor : finding.start])
            label = finding.kind.upper()
            parts.append(self._template.replace("{kind_upper}", label))
            cursor = finding.end
        parts.append(text[cursor:])
        return RedactionResult(
            text="".join(parts),
            status="redacted",
            replacements=len(findings),
            credential_refs=refs,
            kinds=kinds,
        )


def secret_kinds(text: str) -> list[str]:
    """Contract-policy detection only (no replacement); used by self-checks."""
    if not isinstance(text, str) or not text:
        return []
    kinds: list[str] = []
    for kind, pattern in _compile_rules(contracts.redaction_policy()):
        if pattern.search(text) and kind not in kinds:
            kinds.append(kind)
    return kinds


def contains_secret(text: str) -> bool:
    return bool(secret_kinds(text))
