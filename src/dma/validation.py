"""Adapter-side contract self-check for outgoing events.

This is defence in depth for the DMA side: an event must be structurally valid,
carry the ID-01 digest, never exceed part limits and never contain raw content
the shared `redact:1` policy still classifies as secret. The KW service repeats
the checks authoritatively; a rejection here means an adapter bug, not a product
decision.
"""

from __future__ import annotations

import re

from . import contracts
from .errors import InvalidEventError
from .ids import MAX_BATCH_EVENTS, MAX_PART_CHARS, MAX_PART_BYTES, body_digest, canonical_event_id

EVENT_KEYS = {
    "schema_version",
    "event_id",
    "event_revision",
    "workspace_id",
    "operation",
    "source",
    "event_time",
    "body",
    "redaction",
    "project_ids",
    "parent_revision",
    "derived_from",
}
SOURCE_REQUIRED = {
    "instance_id",
    "host",
    "actor_id",
    "session_id",
    "native_event_id",
    "native_schema",
    "role",
    "origin_kind",
    "locator",
}
SOURCE_OPTIONAL = {"turn_id", "parent_event_id", "aliases"}
BODY_KEYS = {"text", "sha256", "part_index", "part_count", "content_revision", "format"}
TIME_KEYS = {"value", "precision", "timezone"}
REDACTION_KEYS = {"policy_version", "status", "replacements", "credential_refs"}

OPERATIONS = {"create", "edit", "retract", "redact"}
HOSTS = {"hermes", "dsh", "openclaw", "codex", "claude_code", "legacy"}
ROLES = {"user", "assistant", "tool", "system"}
ORIGINS = {
    "user_input",
    "assistant_reply",
    "tool_execution",
    "host_system",
    "memory_injection",
    "maintenance",
    "derived_import",
}
REDACTION_STATUSES = {"passed", "redacted", "withheld"}
PRECISIONS = {"instant", "day", "unknown"}

_INSTANT_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_EVENT_ID_RE = re.compile(r"^evt:[a-f0-9]{64}$")
_SHA_RE = re.compile(r"^[a-f0-9]{64}$")


def _token(value) -> bool:
    return isinstance(value, str) and bool(contracts.TOKEN_RE.match(value)) and len(value) <= 180


def _token_list(value, path: str, errors: list[str], *, max_items: int, allow_empty=True) -> None:
    if not isinstance(value, list):
        errors.append(f"{path} must be a list")
        return
    if len(value) > max_items:
        errors.append(f"{path} exceeds {max_items} items")
    if not value and not allow_empty:
        errors.append(f"{path} must not be empty")
    seen = set()
    for index, item in enumerate(value):
        if not _token(item):
            errors.append(f"{path}[{index}] must be a short token")
        elif item in seen:
            errors.append(f"{path}[{index}] duplicates {item!r}")
        seen.add(item)


def _check_time(event_time, errors: list[str]) -> None:
    if not isinstance(event_time, dict) or set(event_time) != TIME_KEYS:
        errors.append("event_time must have exactly value/precision/timezone")
        return
    precision = event_time.get("precision")
    if precision not in PRECISIONS:
        errors.append("event_time.precision must be instant/day/unknown")
        return
    timezone = event_time.get("timezone")
    if not isinstance(timezone, str) or not 1 <= len(timezone) <= 64:
        errors.append("event_time.timezone must be a non-empty string")
    value = event_time.get("value")
    if precision == "instant":
        if not isinstance(value, str) or not _INSTANT_RE.match(value):
            errors.append("event_time.value must be a timezone-qualified instant")
    elif precision == "day":
        if not isinstance(value, str) or not _DATE_RE.match(value):
            errors.append("event_time.value must be a YYYY-MM-DD date")
    elif precision == "unknown" and value is not None:
        errors.append("event_time.value must be null when precision is unknown")


def _check_source(source, errors: list[str]) -> None:
    if not isinstance(source, dict):
        errors.append("source must be an object")
        return
    keys = set(source)
    missing = SOURCE_REQUIRED - keys
    extra = keys - SOURCE_REQUIRED - SOURCE_OPTIONAL
    if missing:
        errors.append(f"source is missing {sorted(missing)}")
    if extra:
        errors.append(f"source has unknown fields {sorted(extra)}")
    for field in ("instance_id", "actor_id", "session_id", "native_event_id"):
        if field in source and not _token(source[field]):
            errors.append(f"source.{field} must be a short token")
    if source.get("host") not in HOSTS:
        errors.append("source.host is not a supported host")
    if source.get("role") not in ROLES:
        errors.append("source.role is not a supported role")
    if source.get("origin_kind") not in ORIGINS:
        errors.append("source.origin_kind is not a supported origin")
    if not isinstance(source.get("native_schema"), str) or not 1 <= len(source["native_schema"]) <= 120:
        errors.append("source.native_schema must be a short string")
    locator = source.get("locator")
    if not isinstance(locator, str) or len(locator) > 2048:
        errors.append("source.locator must be a string of at most 2048 characters")
    if "turn_id" in source and not _token(source["turn_id"]):
        errors.append("source.turn_id must be a short token")
    if "parent_event_id" in source and not (
        isinstance(source["parent_event_id"], str) and _EVENT_ID_RE.match(source["parent_event_id"])
    ):
        errors.append("source.parent_event_id must be a namespaced event id")
    if "aliases" in source:
        _token_list(source["aliases"], "source.aliases", errors, max_items=100)


def _check_body(body, errors: list[str]) -> None:
    if not isinstance(body, dict) or set(body) != BODY_KEYS:
        errors.append("body must have exactly text/sha256/part_index/part_count/content_revision/format")
        return
    text = body.get("text")
    if not isinstance(text, str):
        errors.append("body.text must be a string")
        return
    if len(text) > MAX_PART_CHARS:
        errors.append(f"body.text exceeds {MAX_PART_CHARS} characters")
    if len(text.encode("utf-8")) > MAX_PART_BYTES:
        errors.append(f"body.text exceeds {MAX_PART_BYTES} UTF-8 bytes")
    digest = body.get("sha256")
    if not isinstance(digest, str) or not _SHA_RE.match(digest):
        errors.append("body.sha256 must be a hex digest")
    elif digest != body_digest(text):
        errors.append("body.sha256 must be the digest of the redacted text")
    part_index = body.get("part_index")
    part_count = body.get("part_count")
    if not isinstance(part_index, int) or isinstance(part_index, bool) or part_index < 0:
        errors.append("body.part_index must be a non-negative integer")
    if not isinstance(part_count, int) or isinstance(part_count, bool) or part_count < 1:
        errors.append("body.part_count must be a positive integer")
    elif isinstance(part_index, int) and part_index >= part_count:
        errors.append("body.part_index must be below part_count")
    elif part_count > MAX_BATCH_EVENTS:
        errors.append(f"body.part_count exceeds {MAX_BATCH_EVENTS}")
    if not _token(body.get("content_revision")):
        errors.append("body.content_revision must be a short token")
    if body.get("format") != "text/plain":
        errors.append("body.format must be text/plain")


def _check_redaction(redaction, body, errors: list[str]) -> None:
    if not isinstance(redaction, dict) or set(redaction) != REDACTION_KEYS:
        errors.append("redaction must have exactly policy_version/status/replacements/credential_refs")
        return
    if redaction.get("policy_version") not in contracts.REDACTION_POLICY_VERSIONS:
        errors.append("redaction.policy_version is not supported")
    if redaction.get("status") not in REDACTION_STATUSES:
        errors.append("redaction.status is not supported")
    replacements = redaction.get("replacements")
    if not isinstance(replacements, int) or isinstance(replacements, bool) or replacements < 0:
        errors.append("redaction.replacements must be a non-negative integer")
    refs = redaction.get("credential_refs")
    if not isinstance(refs, list):
        errors.append("redaction.credential_refs must be a list")
    else:
        _token_list(refs, "redaction.credential_refs", errors, max_items=100)
    if isinstance(body, dict) and redaction.get("status") == "withheld" and body.get("text") != "":
        errors.append("withheld content must not carry a body")


def validate_event(event: dict) -> list[str]:
    """Return a list of contract violations for one outgoing event (empty = valid)."""
    errors: list[str] = []
    if not isinstance(event, dict):
        return ["event must be an object"]
    keys = set(event)
    missing = EVENT_KEYS - keys
    extra = keys - EVENT_KEYS
    if missing:
        errors.append(f"event is missing {sorted(missing)}")
    if extra:
        errors.append(f"event has unknown fields {sorted(extra)}")
    if event.get("schema_version") not in contracts.SUPPORTED_SCHEMA_VERSIONS:
        errors.append("schema_version is not supported")
    if not _token(event.get("workspace_id")):
        errors.append("workspace_id must be a short token")
    if event.get("operation") not in OPERATIONS:
        errors.append("operation is not supported")
    _check_source(event.get("source"), errors)
    _check_time(event.get("event_time"), errors)
    _check_body(event.get("body"), errors)
    _check_redaction(event.get("redaction"), event.get("body"), errors)
    if not isinstance(event.get("project_ids"), list):
        errors.append("project_ids must be a list")
    else:
        _token_list(event["project_ids"], "project_ids", errors, max_items=20)
    derived = event.get("derived_from")
    if not isinstance(derived, list):
        errors.append("derived_from must be a list")
    elif len(derived) > 1000:
        errors.append("derived_from exceeds 1000 items")
    else:
        for index, reference in enumerate(derived):
            if (
                not isinstance(reference, dict)
                or set(reference) != {"event_id", "event_revision"}
                or not (isinstance(reference.get("event_id"), str) and _EVENT_ID_RE.match(reference["event_id"]))
                or not isinstance(reference.get("event_revision"), int)
            ):
                errors.append(f"derived_from[{index}] must be event_id/event_revision")
    revision = event.get("event_revision")
    parent = event.get("parent_revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        errors.append("event_revision must be a positive integer")
    if event.get("operation") == "create":
        if revision != 1:
            errors.append("create must be revision 1")
        if parent is not None:
            errors.append("create cannot supersede another revision")
    elif event.get("operation") in ("edit", "retract", "redact"):
        if not isinstance(parent, int) or isinstance(parent, bool) or parent < 1:
            errors.append(f"{event.get('operation')} requires parent_revision")
        elif isinstance(revision, int) and revision != parent + 1:
            errors.append("revision must be parent_revision + 1")
    if event.get("operation") == "retract":
        body = event.get("body") or {}
        if body.get("text") != "":
            errors.append("retract body must be one empty part")
        if body.get("part_index") != 0 or body.get("part_count") != 1:
            errors.append("retract must be one empty part (0 of 1)")
    source = event.get("source")
    if (
        isinstance(source, dict)
        and _token(event.get("workspace_id"))
        and _token(source.get("instance_id"))
        and _token(source.get("native_event_id"))
    ):
        expected_id = canonical_event_id(
            event["workspace_id"], source["instance_id"], source["native_event_id"]
        )
        if event.get("event_id") != expected_id:
            errors.append("event_id must follow the frozen ID-01 derivation")
    # Defence in depth: the redactor must have removed shared-policy secrets.
    body = event.get("body")
    text = body.get("text") if isinstance(body, dict) else None
    if isinstance(text, str) and text:
        kinds = contracts.load("redaction-policy.json")  # noqa: F841 - cached load
        from .redaction import secret_kinds

        detected = secret_kinds(text)
        if detected:
            errors.append(f"body still contains shared-policy secrets: {sorted(detected)}")
    return errors


def assert_valid_event(event: dict) -> None:
    errors = validate_event(event)
    if errors:
        raise InvalidEventError("; ".join(errors))


def validate_batch(batch: dict) -> list[str]:
    errors: list[str] = []
    if not isinstance(batch, dict):
        return ["batch must be an object"]
    keys = set(batch)
    expected = {"schema_version", "batch_id", "events"}
    if keys != expected:
        errors.append("batch must have exactly schema_version/batch_id/events")
    if batch.get("schema_version") not in contracts.SUPPORTED_SCHEMA_VERSIONS:
        errors.append("batch schema_version is not supported")
    if not _token(batch.get("batch_id")):
        errors.append("batch_id must be a short token")
    events = batch.get("events")
    if not isinstance(events, list) or not events:
        errors.append("events must be a non-empty list")
    elif len(events) > MAX_BATCH_EVENTS:
        errors.append("events exceeds the batch limit")
    else:
        seen = set()
        for index, event in enumerate(events):
            for error in validate_event(event):
                errors.append(f"events[{index}]: {error}")
            body = event.get("body") or {}
            key = (
                event.get("event_id"),
                event.get("event_revision"),
                body.get("part_index"),
            )
            if key in seen:
                errors.append(f"events[{index}]: duplicate part key in batch")
            seen.add(key)
    return errors
