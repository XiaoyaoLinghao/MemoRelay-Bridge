"""Historical importer fixtures: synthetic v1 daily files and transcripts."""
from __future__ import annotations

import json

import pytest

from tests.adapters.conftest import clock, outbox, redactor  # noqa: F401

DAILY_FILE = """---
title: "2026年05月22日 - 会话记忆"
date: "2026-05-22"
---

## 核心要点
- 04:00 - 启动松塔项目，确认采用模块化架构设计
- 这条没有时间，应保留日期精度
## 决策与结论
- 05:00 - 决定端口改为4200

## 风险与注意事项
- 12:00 - 规则引擎边界情况需补测试
"""


def write_daily(tmp_path, *, extra: str = "", files: int = 1):
    directory = tmp_path / "memory"
    directory.mkdir()
    for index in range(files):
        date = f"2026-05-{22 + index:02d}"
        content = DAILY_FILE.replace("2026-05-22", date)
        (directory / f"{date}.md").write_text(content + extra, encoding="utf-8")
    return directory


def write_chat_jsonl(tmp_path, host="codex"):
    path = tmp_path / f"{host}-history.jsonl"
    records = [
        {"role": "user", "content": "松塔项目端口改为4200", "timestamp": "2026-05-22T04:00:00Z", "session_id": "s1", "id": "m1"},
        {"role": "assistant", "content": [{"type": "text", "text": "已记录"}], "timestamp": 1770000000, "session_id": "s1", "id": "m2"},
        {"role": "system", "content": "You are Codex.", "timestamp": "2026-05-22T04:00:00Z", "session_id": "s1"},
        {"role": "tool", "content": "exit 0", "session_id": "s1", "id": "m3"},
        {"role": "user", "content": "无时间戳的消息", "session_id": "s1", "id": "m4"},
    ]
    path.write_text("\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def daily_dir(tmp_path):
    return write_daily(tmp_path)
