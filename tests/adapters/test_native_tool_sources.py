import json
import sqlite3
from pathlib import Path
from dma.adapters.dsh.reader import DshSessionReader
from dma.adapters.hermes.reader import HermesSqliteReader
from tests.adapters.conftest import make_hermes_db


def dsh_records(name):
 rows=[{"type":"tool/call","seq":0,"time":1000,"data":{"callId":"call:1","name":name,"arguments":{}}},
       {"type":"tool/result","seq":1,"time":1001,"data":{"message":{"toolCallId":"call:1","content":[{"type":"text","text":"端口4317"}]}},"surfaceOp":"append","sourceEventSeqs":[0]}]
 reader=DshSessionReader(None,session_log="\n".join(json.dumps(x) for x in rows),instance_id="i",session_id="s")
 return reader.read_since("s",None)[0]


def test_actual_native_tool_result_call_reference_is_not_maintenance():
 rows=dsh_records("read_fixture")
 assert len(rows)==1 and rows[0][0].origin_kind=="tool_execution"
 assert rows[0][0].role=="tool" and rows[0][0].aliases==("call:call:1","source:0")


def test_memory_search_tool_result_is_injection_not_new_execution_evidence():
 rows=dsh_records("unified_memory_search")
 assert rows[0][0].origin_kind=="memory_injection"


def test_hermes_memory_search_result_is_injection_while_real_tool_stays_evidence(tmp_path):
 db=make_hermes_db(tmp_path/"state.db",[("s",None,None)],[{"id":1,"text":"召回的端口4317","timestamp":1,"role":"tool","session_id":"s"},{"id":2,"text":"实测端口4317","timestamp":2,"role":"tool","session_id":"s"}])
 with sqlite3.connect(db) as c:
  c.execute("ALTER TABLE messages ADD COLUMN tool_name TEXT")
  c.execute("UPDATE messages SET tool_name='unified_memory_search' WHERE id=1")
  c.execute("UPDATE messages SET tool_name='read_fixture' WHERE id=2")
 rows,_=HermesSqliteReader(db,instance_id="i").read_since("s",None)
 assert [r.origin_kind for r,_ in rows]==["memory_injection","tool_execution"]
