"""Recall client for the KW service over its Unix socket (no TCP, no model)."""

from __future__ import annotations

import http.client
import json
import socket
from pathlib import Path


class RecallUnavailable(Exception):
    """The recall backend could not answer; the host must say this turn has no history."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self._socket_path = socket_path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._socket_path)


class RecallClient:
    def __init__(
        self,
        socket_path: str | Path,
        client_id: str,
        *,
        timeout: float = 6.5,
    ) -> None:
        self.socket_path = str(socket_path)
        self.client_id = client_id
        self.timeout = timeout

    def recall(
        self,
        *,
        query: str,
        session_id: str,
        turn_id: str,
        request_id: str,
        project_ids=(),
        intent: str = "current",
        budget_tokens: int = 2000,
        limit: int = 8,
        deadline_ms: int = 6500,
        cursor: str | None = None,
    ) -> dict:
        request = {
            "schema_version": "1.0",
            "request_id": request_id,
            "session_id": session_id,
            "turn_id": turn_id,
            "query": query,
            "project_ids": list(project_ids),
            "intent": intent,
            "budget_tokens": budget_tokens,
            "limit": limit,
            "deadline_ms": deadline_ms,
            "cursor": cursor,
        }
        body = json.dumps(request, ensure_ascii=False).encode("utf-8")
        connection = _UnixHTTPConnection(self.socket_path, min(self.timeout, deadline_ms / 1000.0))
        try:
            connection.request(
                "POST",
                "/v1/recall",
                body=body,
                headers={
                    "Content-Type": "application/json",
                    "X-DMA-Client": self.client_id,
                    "Accept": "application/json",
                },
            )
            response = connection.getresponse()
            raw = response.read()
            try:
                payload = json.loads(raw.decode("utf-8")) if raw else {}
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise RecallUnavailable("STORE_UNAVAILABLE", "invalid recall response") from None
        except (OSError, TimeoutError, socket.timeout, http.client.HTTPException) as exc:
            raise RecallUnavailable("STORE_UNAVAILABLE", "recall backend unreachable") from exc
        finally:
            connection.close()
        if response.status != 200:
            error = payload.get("error") or {}
            raise RecallUnavailable(
                str(error.get("code") or "STORE_UNAVAILABLE"),
                str(error.get("message") or "recall failed"),
            )
        return payload

    def fetch_source(self, event_id: str, revision: int, part: int = 0, *, timeout: float | None = None) -> dict:
        """Read one exact sanitized source revision through KW's bound API."""
        import re
        if not isinstance(event_id,str) or not re.fullmatch(r"evt:[0-9a-f]{64}",event_id) or type(revision) is not int or revision<1 or type(part) is not int or part<0:
            raise RecallUnavailable("INVALID_REQUEST")
        connection=_UnixHTTPConnection(self.socket_path,min(self.timeout,timeout if timeout is not None else self.timeout))
        try:
            connection.request("GET",f"/v1/sources/{event_id}?revision={revision}&part={part}",headers={"X-DMA-Client":self.client_id,"Accept":"application/json"})
            response=connection.getresponse();raw=response.read()
            try:payload=json.loads(raw.decode("utf-8")) if raw else {}
            except (UnicodeDecodeError,json.JSONDecodeError):raise RecallUnavailable("STORE_UNAVAILABLE") from None
            if not isinstance(payload,dict):raise RecallUnavailable("STORE_UNAVAILABLE")
            if response.status!=200:
                error=payload.get("error") or {}
                code=error.get("code")
                raise RecallUnavailable(code if code in {"SOURCE_NOT_FOUND","SCOPE_DENIED","UNREGISTERED_CLIENT","INVALID_REQUEST"} else "STORE_UNAVAILABLE")
            return payload
        except (OSError,TimeoutError,socket.timeout,http.client.HTTPException):
            raise RecallUnavailable("STORE_UNAVAILABLE") from None
        finally:connection.close()
