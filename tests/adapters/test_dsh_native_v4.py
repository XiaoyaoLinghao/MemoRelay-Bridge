"""Regression from an actual isolated DSH 0.2.1-rc.2 session, not a hand-written host log."""
import json
from pathlib import Path
import pytest
from dma.adapters.dsh.reader import DshSessionReader, SourceReadError
from .conftest import dsh_adapter

FIXTURE=Path(__file__).parents[1]/"fixtures/dsh/native-v4-pilot.jsonl"
SID="session-01cb61b1-aa7f-49ca-a3e6-0f4979342de2"
def reader(path):return DshSessionReader(path,instance_id="instance:pilot-dsh",session_id=SID)
def test_actual_native_header_and_metadata_keep_all_surface_records(tmp_path,redactor,outbox):
 from dma.pipeline import capture
 pairs,position=reader(FIXTURE).read_since("dsh:"+SID,None)
 assert position=="15"
 assert [(x.native_event_id,x.origin_kind) for x,_ in pairs]==[(SID+":seq:8","user_input"),(SID+":seq:9","memory_injection"),(SID+":seq:13","assistant_reply")]
 assert "每周二" in pairs[0][0].text
 assert pairs[0][0].event_time["precision"]=="instant"
 first=capture(reader(FIXTURE),outbox,stream="dsh:"+SID,convert=dsh_adapter(redactor).to_events)
 second=capture(reader(FIXTURE),outbox,stream="dsh:"+SID,convert=dsh_adapter(redactor).to_events)
 assert first["inserted"]==3 and second["inserted"]==0
 assert reader(FIXTURE).read_since("dsh:"+SID,position)[0]==[]
@pytest.mark.parametrize("field,value",[("version",5),("id","session:wrong")])
def test_incompatible_native_header_is_not_silently_skipped(tmp_path,field,value):
 rows=[json.loads(x) for x in FIXTURE.read_text().splitlines()];rows[0][field]=value
 path=tmp_path/"bad.jsonl";path.write_text("\n".join(json.dumps(x) for x in rows))
 with pytest.raises(SourceReadError):reader(path).read_since("dsh:"+SID,None)
def test_new_required_type_still_blocks_cursor(tmp_path):
 rows=[json.loads(x) for x in FIXTURE.read_text().splitlines()];rows[1]["type"]="future/required"
 path=tmp_path/"bad.jsonl";path.write_text("\n".join(json.dumps(x) for x in rows))
 with pytest.raises(SourceReadError):reader(path).read_since("dsh:"+SID,None)

def test_native_export_in_memory_matches_file_without_writing_raw_copy():
 reader=DshSessionReader(None,session_log=FIXTURE.read_text(),instance_id="instance:pilot-dsh",session_id=SID)
 pairs,end=reader.read_since("dsh:"+SID,None)
 assert end=="15" and len(pairs)==3
 assert [x.origin_kind for x,_ in pairs]==["user_input","memory_injection","assistant_reply"]
