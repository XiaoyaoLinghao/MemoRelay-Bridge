"""T17: DMA operations CLI (capture/import/deliver/doctor/status/preview)."""
from __future__ import annotations

import json

from dma.cli import main
from dma.outbox import Outbox
from tests.adapters.conftest import make_hermes_db
from tests.core.conftest import FakeKwServer, accept_all
from tests.importers.conftest import write_daily


def _run(capsys, argv):
    code = main(argv)
    captured = capsys.readouterr()
    lines = [line for line in captured.out.splitlines() if line.strip()]
    return code, json.loads("\n".join(lines)) if lines else {}


def test_versions_doctor_and_preview(tmp_path, capsys):
    code, payload = _run(capsys, ["versions"])
    assert code == 0 and payload["contract_version"] == "1.0"
    code, payload = _run(capsys, ["doctor"])
    assert code == 0 and payload["ok"] is True
    code, payload = _run(capsys, ["install-preview"])
    assert code == 0 and payload["ok"] is True
    assert "nothing was written" in payload["note"]
    preview = payload["launchd_example"]
    assert preview["program"][2:4] == ["dma.cli", "deliver"]
    assert preview["keep_alive"] is False and preview["start_interval_seconds"] == 30


def test_capture_status_and_deliver(tmp_path, capsys, clock):
    now = clock.now.timestamp()
    db = make_hermes_db(
        tmp_path / "state.db",
        [("session:root", None, None)],
        [{"id": 1, "text": "松塔项目端口改为4200", "timestamp": now - 600}],
    )
    outbox = tmp_path / "outbox.db"
    code, payload = _run(
        capsys,
        [
            "capture", "--state-db", str(db), "--outbox", str(outbox),
            "--profile", "main", "--settle-seconds", "0", "--instance", "instance:hermes-test",
        ],
    )
    assert code == 0 and payload["capture"]["inserted"] == 1
    code, payload = _run(capsys, ["status", "--outbox", str(outbox), "--instance", "instance:hermes-test"])
    assert code == 0 and payload["stats"]["by_state"]["pending"] == 1
    with FakeKwServer(accept_all) as kw:
        code, payload = _run(
            capsys,
            [
                "deliver", "--outbox", str(outbox), "--socket", str(kw.socket_path),
                "--client", "client:hermes-main", "--instance", "instance:hermes-test",
            ],
        )
        assert code == 0 and payload["delivery"]["accepted"] == 1
    reopened = Outbox(outbox, workspace_id="owner:local", instance_id="instance:hermes-test")
    assert reopened.stats()["by_state"]["delivered"] == 1


def test_import_daily_cli(tmp_path, capsys):
    memory = write_daily(tmp_path)
    outbox = tmp_path / "outbox.db"
    args = [
        "import-daily", "--input", str(memory), "--outbox", str(outbox),
        "--workspace", "owner:demo", "--instance", "instance:legacy-import",
        "--project", "project:songta",
    ]
    code, payload = _run(capsys, args)
    assert code == 0 and payload["import"]["imported_events"] == 4
    code, again = _run(capsys, args)
    assert code == 0 and again["import"]["duplicates"] == 4


def test_import_chat_cli(tmp_path, capsys):
    from tests.importers.conftest import write_chat_jsonl

    path = write_chat_jsonl(tmp_path, host="codex")
    code, payload = _run(
        capsys,
        [
            "import-chat", "--host", "codex", "--input", str(path),
            "--outbox", str(tmp_path / "outbox.db"), "--workspace", "owner:demo",
        ],
    )
    assert code == 0 and payload["import"]["host"] == "codex"
    assert payload["stats"]["total"] == 4


def test_cli_error_is_json_and_nonzero(tmp_path, capsys):
    code, payload = _run(
        capsys,
        ["capture", "--state-db", str(tmp_path / "missing.db"), "--outbox", str(tmp_path / "o.db")],
    )
    assert code == 2 and payload["ok"] is False
