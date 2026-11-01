import json
import pytest
from dma.adapters.dsh.reader import DshSessionReader
from dma.errors import SourceReadError

def logs(tamper=False):
 root=[{"type":"session","version":4,"id":"root","createdAt":1,"isSeeded":False},
       {"type":"user/message","seq":0,"time":1,"data":{"content":[{"type":"text","text":"original human port4200"}],"source":{"kind":"user"}}},
       {"type":"turn/start","seq":1,"time":2,"data":{"turn":"parent-turn"}}]
 child=[{"type":"session","version":4,"id":"child","createdAt":2,"parentSession":"root","isSeeded":True,"origin":{"kind":"subagent"},"delegationDepth":1},
        root[1],
        {"type":"working-directory/change","seq":1,"time":3,"data":{"cwd":"/tmp/child"}},
        {"type":"user/message","seq":2,"time":3,"data":{"content":[{"type":"text","text":"delegated instruction, not a new human confirmation"}],"source":{"kind":"agent-instructions"}}},
        {"type":"session/end-seed","seq":3,"time":3,"data":{"inherited":True}},
        {"type":"assistant/message","seq":4,"time":4,"data":{"content":[{"type":"text","text":"child reports its new work"}]}}]
 if tamper:child[1]=dict(child[1],data={"content":[{"type":"text","text":"changed human fact"}],"source":{"kind":"user"}})
 dump=lambda rows:"\n".join(json.dumps(x) for x in rows)+"\n"
 return dump(root),dump(child)

def test_subagent_seed_scaffolding_is_own_excluded_context_not_parent_business():
 root,child=logs();r=DshSessionReader(None,session_log=child,ancestor_logs={"root":root},instance_id="instance:compat",session_id="child")
 records,_=r.read_since("s",None);rows=[x for x,_ in records]
 assert rows[0].native_event_id=="root:seq:0" and rows[0].origin_kind=="user_input"
 assert rows[1].native_event_id=="child:seq:2" and rows[1].origin_kind=="memory_injection"
 assert rows[2].native_event_id=="child:seq:4" and rows[2].origin_kind=="assistant_reply"

def test_mismatching_real_inherited_business_still_refused():
 root,child=logs(tamper=True);r=DshSessionReader(None,session_log=child,ancestor_logs={"root":root},instance_id="instance:compat",session_id="child")
 with pytest.raises(SourceReadError,match="prefix does not match"):r.read_since("s",None)
