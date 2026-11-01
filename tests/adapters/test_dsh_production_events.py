import json
import pytest
from dma.adapters.dsh.reader import DshSessionReader
from dma.errors import SourceReadError

def read(types):
 rows=[{"type":"session","version":4,"id":"session-prod-compat","createdAt":1793491200000,"isSeeded":False}]
 for seq,kind in enumerate(types):rows.append({"type":kind,"seq":seq,"time":1793491200000+seq,"data":{"content":[{"type":"text","text":"background test"}],"status":"done","jobId":"job-demo","detail":"narrow exit only","todos":[{"text":"review report","status":"pending"}],"task":{"id":"task-demo","status":"open"},"files":[{"path":"/tmp/report.txt"}],"goal":{"objective":"review","status":"active"}}})
 r=DshSessionReader(None,session_log="\n".join(json.dumps(x) for x in rows)+"\n",instance_id="instance:compat",session_id="session-prod-compat")
 return r.read_since("dsh:test",None)

def test_observed_native_bookkeeping_does_not_block_user_or_reenter_memory():
 kinds=["session/title-llm-request","job/registered","workspace/changes","llm/retry","llm/retry-started","command/run","command/done","approval/asked","approval/decided","subagent/descriptor","agent-preset/selected","model/selection","session-log-deepseek/delivery-accepted","web/deepseek-search-llm-request","team/member","subagent/catalog","working-directory/change","user/message"]
 records,position=read(kinds);assert len(records)==1 and records[0][0].role=="user" and position==str(len(kinds)-1)

@pytest.mark.parametrize("kind",["job/settled","todo/write","team/task","deliverables/presented","goal/change"])
def test_actual_work_state_preserved_as_narrow_native_tool_evidence(kind):
 records,_=read([kind]);assert len(records)==1
 record=records[0][0];assert record.role=="tool" and record.origin_kind==("maintenance" if kind=="job/settled" else "tool_execution")
 text=json.loads(record.text);assert text["native_event"]==kind and text["verification_scope"]=="native state update only, not verification of the whole task"
 assert record.native_event_id.endswith(":seq:0")
 from dma.adapters.dsh.adapter import DshAdapter
 from dma.redaction import Redactor
 events=DshAdapter(Redactor(),workspace_id="owner:demo",instance_id="instance:compat").to_events(record)
 assert events and events[0]["source"]["origin_kind"]==record.origin_kind
 assert all("/" not in alias for alias in events[0]["source"]["aliases"])


def test_unrecognized_required_native_event_still_fails_closed():
 with pytest.raises(SourceReadError):read(["future/required"])


@pytest.mark.parametrize("kind",["agent-instructions","skill-catalog","time-context","compact-checkpoint","tool-jobs","repeat-tool-reminder","user-approval","agent-message","model-selection","subagent-settled","tool-goal"])
def test_observed_generated_user_roles_are_not_human_confirmations(kind):
 rows=[{"type":"session","version":4,"id":"s-generated","isSeeded":False,"createdAt":1},
       {"type":"user/message","seq":0,"time":1,"data":{"source":{"kind":kind},"content":[{"type":"text","text":"generated report, not new human confirmation"}]}}]
 reader=DshSessionReader(None,session_log="\n".join(json.dumps(x) for x in rows)+"\n",instance_id="instance:compat",session_id="s-generated")
 records,_=reader.read_since("s",None)
 assert len(records)==1 and records[0][0].origin_kind=="memory_injection"
 assert records[0][0].role=="user"

def test_unknown_user_message_source_does_not_silently_become_human():
 rows=[{"type":"session","version":4,"id":"s-generated","isSeeded":False,"createdAt":1},
       {"type":"user/message","seq":0,"time":1,"data":{"source":{"kind":"future-automation"},"content":[{"type":"text","text":"not verified"}]}}]
 reader=DshSessionReader(None,session_log="\n".join(json.dumps(x) for x in rows)+"\n",instance_id="instance:compat",session_id="s-generated")
 with pytest.raises(SourceReadError):reader.read_since("s",None)
