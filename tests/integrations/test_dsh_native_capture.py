"""Native export stdin is sanitized before durable capture; no raw handoff file."""
import json,subprocess,sys
from pathlib import Path
from dma.outbox import Outbox
FIXTURE=Path(__file__).parents[1]/"fixtures/dsh/native-v4-pilot.jsonl"
SID="session-01cb61b1-aa7f-49ca-a3e6-0f4979342de2"
def invoke(tmp_path,text):
 db=tmp_path/"outbox.db";cmd=[sys.executable,"-m","dma.integrations.dsh.capture","--workspace","owner:demo","--instance","instance:pilot-dsh","--actor","actor:dsh","--session",SID,"--outbox",str(db),"--project","project:songta"]
 result=subprocess.run(cmd,input=text,text=True,capture_output=True,timeout=20)
 return result,db

def test_native_stdin_captures_and_redacts_without_raw_disk_copy(tmp_path):
 rows=[json.loads(x) for x in FIXTURE.read_text().splitlines()];rows[9]["data"]["content"]=[{"type":"text","text":"数据库密码: CANARY-PW-7f3a"}]
 raw="\n".join(json.dumps(x,ensure_ascii=False) for x in rows);result,db=invoke(tmp_path,raw)
 assert result.returncode==0,(result.stdout,result.stderr)
 assert "CANARY-PW-7f3a" not in result.stdout+result.stderr
 assert b"CANARY-PW-7f3a" not in db.read_bytes()
 box=Outbox(db)
 assert box.stats()["total"]==3
 assert all(x.suffix in (".db",".db-wal",".db-shm") or x.name.endswith(("-wal","-shm")) for x in tmp_path.iterdir())

def test_native_parse_failure_does_not_echo_source_or_advance_cursor(tmp_path):
 canary="CANARY-PW-7f3a";result,db=invoke(tmp_path,canary)
 assert result.returncode==1 and canary not in result.stdout+result.stderr
 assert json.loads(result.stdout)["error"]=="NATIVE_CAPTURE_FAILED"
 assert Outbox(db).stats()["total"]==0


def test_native_plugin_exports_fork_ancestors_before_capture(tmp_path):
 import os,shutil
 import pytest
 node=os.environ.get("NODE_BIN") or shutil.which("node")
 if not node:pytest.skip("Node runtime unavailable")
 root=Path(__file__).parents[2]
 exports=tmp_path/"exports.mjs"
 exports.write_text("export async function flushLiveSessionLog(ctx,id){ctx.sessions.flushed.push(id);}; export async function readSessionLogText(p,id){p.reads.push(id);return p.logs[id];}")
 db=tmp_path/"outbox.db"
 cmd=[node,str(root/"tests/integrations/node/native_fork_capture.mjs"),str(root/"src/dma/integrations/dsh/plugin/native.mjs"),str(exports),str(root/"tests/fixtures/dsh/native-fork-pilot.json"),sys.executable,str(db)]
 result=subprocess.run(cmd,capture_output=True,text=True,timeout=20)
 assert result.returncode==0,result.stdout+result.stderr
 assert json.loads(result.stdout)["passed"] is True
 assert Outbox(db).stats()["total"]==3
 with Outbox(db).read() as conn:
  payloads=[json.loads(row["payload_json"]) for row in conn.execute("SELECT payload_json FROM outbox")]
 assert {p["source"]["native_event_id"] for p in payloads}=={"session-preprod-root:seq:0","session-preprod-child:seq:2","session-preprod-grandchild:seq:4"}

 assert all(p["project_ids"]==["project:songta"] for p in payloads)  # capture scope is independent of automatic query scope
