"""Fixtures for host recall-plugin tests (synthetic hosts, isolated runtime)."""
from __future__ import annotations

import pytest

from dma.integrations.hermes import DmaMemoryProvider
from dma.integrations.recall_client import RecallUnavailable

from tests.adapters.conftest import (  # noqa: F401 - re-exported fixtures
    clock,
    hermes_adapter,
    hermes_reader,
    make_hermes_db,
    outbox,
    redactor,
)


def memory_item(text="松塔项目端口为4200", **overrides):
    item = {
        "claim_id": "claim:test",
        "revision_id": "rev:test",
        "project_id": "project:songta",
        "subject_id": "subject:songta-server",
        "projection_generation": 1,
        "value": 4200,
        "qualifiers": {},
        "predicate": "server.port",
        "facet": "desired",
        "text": text,
        "state": "current",
        "evidence_grade": "user_confirmed",
        "event_time": {
            "value": "2026-10-30T01:00:00Z",
            "precision": "instant",
            "timezone": "UTC",
        },
        "sources": [
            {
                "event_id": "evt:" + "a" * 64,
                "event_revision": 1,
                "part_index": 0,
                "start_byte": 0,
                "end_byte": 12,
                "body_sha256": "b" * 64,
                "availability": "full",
            }
        ],
        "conflict_revision_ids": [],
        "source_actors": ["jun-test"],
        "relevance_score": 0.5,
        "source_detail_expired": False,
    }
    item.update(overrides)
    return item


def recall_result(items):
    return {
        "schema_version": "1.0",
        "request_id": "req:test",
        "status": "ok" if items else "empty",
        "scope_candidates": [],
        "watermark": 1,
        "restore_epoch": "epoch:1",
        "items": items,
        "next_cursor": None,
        "total_hits": len(items),
        "incomplete_reasons": [],
        "used_tokens": 10,
        "truncated": False,
    }


class FakeRecallClient:
    def __init__(self, result=None, error: str | None = None):
        self.result = result or recall_result([])
        self.error = error
        self.calls: list[dict] = []

    def recall(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise RecallUnavailable(self.error, "synthetic failure")
        return self.result


@pytest.fixture
def provider_factory():
    created = []

    def make(*, client, reader=None, adapter=None, outbox=None, **kwargs):
        provider = DmaMemoryProvider(
            recall_client=client,
            client_id="client:hermes-main",
            reader=reader,
            adapter=adapter,
            outbox=outbox,
            **kwargs,
        )
        created.append(provider)
        return provider

    return make
