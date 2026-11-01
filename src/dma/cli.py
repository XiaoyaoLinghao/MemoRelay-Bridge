"""dma-memory CLI: capture/deliver/import/doctor/status (T17).

All commands print JSON. The CLI never installs launchd/cron entries; it prints
a reviewable preview. Paths are explicit; no production Hermes/DSH default path
is read unless the operator passes it.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import contracts, __version__


def _emit(payload, *, code: int = 0) -> int:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return code


def cmd_versions(_args) -> int:
    return _emit(
        {
            "name": "daily-memory-archiver-core",
            "version": __version__,
            "contract_version": contracts.CONTRACT_VERSION,
            "redaction_policy_version": contracts.redaction_policy()["policy_version"],
            "contracts_dir": str(contracts.contracts_dir()),
        }
    )


def cmd_doctor(args) -> int:
    checks = [{"check": "contracts", "passed": not contracts.verify_checksums(), "detail": contracts.verify_checksums()}]
    checks.append({"check": "python", "passed": sys.version_info >= (3, 11), "detail": sys.version.split()[0]})
    if args.outbox:
        path = Path(args.outbox)
        checks.append({"check": "outbox", "passed": path.is_file(), "detail": str(path)})
    if args.socket:
        path = Path(args.socket)
        checks.append({"check": "socket", "passed": path.exists(), "detail": str(path)})
    failed = [check for check in checks if not check["passed"]]
    return _emit({"ok": not failed, "checks": checks, "failed": failed}, code=0 if not failed else 1)


def _outbox(path: str, workspace: str, instance: str):
    from .outbox import Outbox

    return Outbox(path, workspace_id=workspace, instance_id=instance)


def cmd_status(args) -> int:
    outbox = _outbox(args.outbox, args.workspace, args.instance)
    return _emit({"ok": True, "stats": outbox.stats()}, code=0 if Path(args.outbox).is_file() else 1)


def cmd_capture(args) -> int:
    from .adapters.hermes.adapter import HermesAdapter
    from .adapters.hermes.reader import HermesSqliteReader
    from .pipeline import capture
    from .redaction import Redactor
    from .redaction.references import redactor_from_config

    redactor = redactor_from_config(args.redaction_config) if getattr(args,'redaction_config',None) else Redactor()
    reader = HermesSqliteReader(
        args.state_db,
        instance_id=args.instance,
        profile=args.profile,
        settle_seconds=args.settle_seconds,
    )
    adapter = HermesAdapter(redactor, workspace_id=args.workspace, instance_id=args.instance)
    outbox = _outbox(args.outbox, args.workspace, args.instance)
    result = capture(reader, outbox, stream=f"hermes:{args.profile}", convert=adapter.to_events)
    return _emit({"ok": True, "capture": result, "stats": outbox.stats()})


def cmd_deliver(args) -> int:
    from .delivery import DeliveryClient, deliver_once

    outbox = _outbox(args.outbox, args.workspace, args.instance)
    client = DeliveryClient(args.socket, args.client, timeout=args.timeout)
    result = deliver_once(outbox, client)
    return _emit({"ok": True, "delivery": result, "stats": outbox.stats()})


def cmd_import_daily(args) -> int:
    from .importers.daily_memory import DailyMemoryImporter

    outbox = _outbox(args.outbox, args.workspace, args.instance)
    importer = DailyMemoryImporter(
        workspace_id=args.workspace,
        instance_id=args.instance,
        project_map=dict(item.split("=", 1) for item in args.project_map),
        default_project_id=args.project,
    )
    report = importer.import_path(args.input, outbox)
    return _emit({"ok": True, "import": report, "stats": outbox.stats()})


def cmd_import_chat(args) -> int:
    from .importers.chat_history import ChatHistoryImporter

    outbox = _outbox(args.outbox, args.workspace, args.instance)
    importer = ChatHistoryImporter(
        host=args.host,
        workspace_id=args.workspace,
        instance_id=args.instance,
        project_ids=(args.project,) if args.project else (),
    )
    report = importer.import_path(args.input, outbox)
    return _emit({"ok": True, "import": report, "stats": outbox.stats()})


def cmd_reconcile(args) -> int:
    from .delivery import DeliveryClient
    from .outbox import Outbox
    from .reconcile import reconcile_after_restore

    outbox = _outbox(args.outbox, args.workspace, args.instance)
    client = DeliveryClient(args.socket, args.client, timeout=args.timeout)
    report = reconcile_after_restore(outbox, client, deliver=not args.no_deliver)
    # F15 honesty: the command must not claim success while the reconcile report
    # says otherwise (unrecoverable gaps, unverifiable legacy ack epochs, or an
    # unfinished replay delivery). The inner report is emitted unchanged.
    ok = bool(report.get("ok"))
    return _emit(
        {"ok": ok, "reconcile": report, "stats": outbox.stats()},
        code=0 if ok else 1,
    )


def cmd_redaction_recover(args) -> int:
    from .adapters.hermes.adapter import HermesAdapter
    from .adapters.hermes.reader import HermesSqliteReader
    from .delivery import DeliveryClient
    from .redaction import Redactor
    from .redaction.references import redactor_from_config
    from .redaction_recovery import reread_after_redaction

    outbox = _outbox(args.outbox, args.workspace, args.instance)
    reader = HermesSqliteReader(
        args.state_db, instance_id=args.instance, profile=args.profile
    )
    adapter = HermesAdapter(redactor_from_config(args.redaction_config) if getattr(args,'redaction_config',None) else Redactor(), workspace_id=args.workspace, instance_id=args.instance)
    client = (
        DeliveryClient(args.socket, args.client, timeout=7.0) if args.socket and args.client else None
    )
    report = reread_after_redaction(outbox, [(reader, adapter)], client=client, deliver=bool(client))
    return _emit({"ok": True, "recovery": report, "stats": outbox.stats()})


def cmd_install_preview(_args) -> int:
    return _emit(
        {
            "ok": True,
            "note": "Preview only; nothing was written and no service manager was touched. Review before installing.",
            "launchd_example": {
                "label": "local.unified-memory.dma-deliver",
                "program": ["/path/to/python", "-m", "dma.cli", "deliver", "--outbox", "/path/to/outbox.db", "--socket", "/path/to/kw.sock", "--client", "client:REVIEW", "--workspace", "owner:REVIEW", "--instance", "instance:REVIEW"],
                "run_at_load": True,
                "keep_alive": False,
                "start_interval_seconds": 30,
                "review": "operator must verify paths, owner, logs and rollback before enabling",
            },
        }
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dma-memory", description="DMA 2.0 adapter operations CLI")
    sub = parser.add_subparsers(dest="command", required=True)
    common = dict(workspace="owner:local", instance="instance:local")

    p = sub.add_parser("versions")
    p.set_defaults(func=cmd_versions)

    p = sub.add_parser("doctor")
    p.add_argument("--outbox", default=None)
    p.add_argument("--socket", default=None)
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("status")
    p.add_argument("--outbox", required=True)
    p.add_argument("--workspace", default=common["workspace"])
    p.add_argument("--instance", default=common["instance"])
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("capture")
    p.add_argument("--redaction-config", default=None)
    p.add_argument("--state-db", required=True)
    p.add_argument("--outbox", required=True)
    p.add_argument("--workspace", default=common["workspace"])
    p.add_argument("--instance", default=common["instance"])
    p.add_argument("--profile", default="main")
    p.add_argument("--settle-seconds", type=float, default=60.0)
    p.set_defaults(func=cmd_capture)

    p = sub.add_parser("deliver")
    p.add_argument("--outbox", required=True)
    p.add_argument("--socket", required=True)
    p.add_argument("--client", required=True)
    p.add_argument("--workspace", default=common["workspace"])
    p.add_argument("--instance", default=common["instance"])
    p.add_argument("--timeout", type=float, default=7.0)
    p.set_defaults(func=cmd_deliver)

    p = sub.add_parser("import-daily")
    p.add_argument("--input", required=True, help="v1 memory directory or single .md")
    p.add_argument("--outbox", required=True)
    p.add_argument("--workspace", default=common["workspace"])
    p.add_argument("--instance", default="instance:legacy-import")
    p.add_argument("--project", default=None)
    p.add_argument("--project-map", action="append", default=[], help="KEY=project_id (file name or date)")
    p.set_defaults(func=cmd_import_daily)

    p = sub.add_parser("import-chat")
    p.add_argument("--host", choices=["codex", "claude_code"], required=True)
    p.add_argument("--input", required=True)
    p.add_argument("--outbox", required=True)
    p.add_argument("--workspace", default=common["workspace"])
    p.add_argument("--instance", default=None)
    p.add_argument("--project", default=None)
    p.set_defaults(func=cmd_import_chat)

    p = sub.add_parser("reconcile")
    p.add_argument("--outbox", required=True)
    p.add_argument("--socket", required=True)
    p.add_argument("--client", required=True)
    p.add_argument("--workspace", default=common["workspace"])
    p.add_argument("--instance", default=common["instance"])
    p.add_argument("--timeout", type=float, default=7.0)
    p.add_argument("--no-deliver", action="store_true")
    p.set_defaults(func=cmd_reconcile)

    p = sub.add_parser("redaction-recover")
    p.add_argument("--redaction-config", default=None)
    p.add_argument("--outbox", required=True)
    p.add_argument("--state-db", required=True)
    p.add_argument("--workspace", default=common["workspace"])
    p.add_argument("--instance", required=True)
    p.add_argument("--profile", default="main")
    p.add_argument("--socket", default=None)
    p.add_argument("--client", default=None)
    p.set_defaults(func=cmd_redaction_recover)

    p = sub.add_parser("install-preview")
    p.set_defaults(func=cmd_install_preview)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "command", None) == "import-chat" and getattr(args, "instance", None) is None:
        args.instance = f"instance:{args.host}-import"
    try:
        return args.func(args)
    except KeyboardInterrupt:  # pragma: no cover
        return 130
    except Exception as exc:  # noqa: BLE001 - CLI boundary; no secret content in messages
        code = getattr(exc, "code", type(exc).__name__)
        message = getattr(exc, "message", None) or str(exc)
        return _emit({"ok": False, "error": {"code": code, "message": message[:300]}}, code=2)


if __name__ == "__main__":
    raise SystemExit(main())
