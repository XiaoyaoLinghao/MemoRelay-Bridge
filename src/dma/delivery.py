"""Deliver the durable outbox to the KW service over its Unix socket.

Delivery is idempotent: a batch that was committed but whose response was lost
is re-sent with the same deterministic batch id and event part keys, so KW
returns the stored receipts as duplicates instead of storing twice. A network
failure never removes the local payload; it only schedules a bounded retry.
"""

from __future__ import annotations

import http.client
import json
import socket
from pathlib import Path

from . import contracts
from .errors import DeliveryError
from .outbox import Outbox, utc_now

_STATUS_FALLBACK = {
    400: "INVALID_REQUEST",
    403: "SCOPE_DENIED",
    404: "SOURCE_NOT_FOUND",
    409: "EVENT_CONFLICT",
    413: "PAYLOAD_TOO_LARGE",
    500: "INTERNAL_ERROR",
    503: "STORE_UNAVAILABLE",
}


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self._socket_path = str(socket_path)

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._socket_path)


class DeliveryClient:
    """Minimal KW client used by the DMA delivery worker (no TCP, no secrets)."""

    def __init__(self, socket_path: str | Path, client_id: str, *, timeout: float = 7.0) -> None:
        self.socket_path = str(socket_path)
        self.client_id = client_id
        self.timeout = timeout

    def _request(self, method: str, path: str, body: bytes | None = None) -> tuple[int, dict]:
        connection = _UnixHTTPConnection(self.socket_path, self.timeout)
        headers = {"Accept": "application/json", "X-DMA-Client": self.client_id}
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            payload = response.read()
            try:
                decoded = json.loads(payload.decode("utf-8")) if payload else {}
            except (UnicodeDecodeError, json.JSONDecodeError):
                decoded = {}
            return response.status, decoded
        finally:
            connection.close()

    def post_batch(self, batch: dict) -> tuple[int, dict]:
        raw = json.dumps(batch, ensure_ascii=False).encode("utf-8")
        return self._request("POST", "/v1/events:batch", raw)

    def health(self) -> dict:
        status, payload = self._request("GET", "/v1/health")
        if status != 200:
            error = payload.get("error") or {}
            raise DeliveryError(
                str(error.get("code") or "STORE_UNAVAILABLE"),
                str(error.get("message") or "health check failed"),
                retryable=True,
            )
        return payload


def deliver_once(outbox: Outbox, client: DeliveryClient, *, now: str | None = None) -> dict:
    """Attempt delivery of all currently ready outbox rows once."""
    counters = {
        "batches": 0,
        "accepted": 0,
        "duplicate": 0,
        "withheld": 0,
        "excluded": 0,
        "rejected": 0,
        "redaction_required": 0,
        "retried": 0,
        "failed": 0,
    }
    rows = outbox.pending_rows(now=now)
    if not rows:
        return counters
    for batch, keys in outbox.build_batches(rows):
        counters["batches"] += 1
        outbox.mark_delivering(keys, lease_seconds=max(30, int(getattr(client, "timeout", 7.0)) + 5), now=now)
        try:
            status, payload = client.post_batch(batch)
        except (OSError, TimeoutError) as exc:  # socket missing/busy/timeout
            outbox.record_failure(keys, "STORE_UNAVAILABLE", retryable=True, now=now)
            counters["retried"] += len(keys)
            continue
        if status == 200:
            receipts = payload.get("receipts") or []
            covered = set()
            for receipt in receipts:
                receipt_key = (
                    receipt.get("event_id"),
                    int(receipt.get("event_revision") or 0),
                    int(receipt.get("part_index") or 0),
                )
                if receipt_key not in keys:
                    continue
                covered.add(receipt_key)
                outcome = outbox.apply_receipt(
                    receipt,
                    now=now,
                    watermark=payload.get("watermark"),
                    restore_epoch=payload.get("restore_epoch"),
                )
                disposition = receipt.get("disposition")
                if outcome.get("state") == "redaction_required":
                    counters["redaction_required"] += 1
                elif outcome.get("state") == "failed":
                    # A rejected part is durable but NOT accepted; never fold it
                    # into the accepted count (F03 reporting honesty).
                    counters["rejected" if disposition == "rejected" else "failed"] += 1
                elif outcome.get("state") == "retry_wait":
                    counters["retried"] += 1
                else:
                    counters[disposition if disposition in counters else "accepted"] += 1
            missing = [key for key in keys if key not in covered]
            if missing:
                outbox.record_failure(missing, "STORE_UNAVAILABLE", retryable=True, now=now)
                counters["retried"] += len(missing)
            outbox.set_remote_meta(
                backend_instance_id=payload.get("backend_instance_id"),
                restore_epoch=payload.get("restore_epoch"),
                watermark=payload.get("watermark"),
            )
            continue
        error = payload.get("error") or {}
        code = error.get("code") or _STATUS_FALLBACK.get(status, "STORE_UNAVAILABLE")
        retryable = error.get("retryable")
        if retryable is None:
            entry = contracts.error_entry(code)
            retryable = bool(entry and entry.get("retryable"))
        outcome = outbox.record_failure(keys, code, retryable=bool(retryable), now=now)
        counters["retried"] += outcome["retry_wait"]
        counters["failed"] += outcome["failed"]
    return counters


def delivery_summary(outbox: Outbox) -> dict:
    return {
        "stats": outbox.stats(),
        "checked_at": utc_now(outbox.clock),
    }
