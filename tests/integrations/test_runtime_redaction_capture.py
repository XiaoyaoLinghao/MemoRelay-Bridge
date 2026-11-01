import json

def test_native_cli_runtime_reference_masks_a_bare_old_tool_key(tmp_path):
 from .test_dsh_native_capture import FIXTURE,SID
 import subprocess,sys
 from dma.outbox import Outbox
 secret="SYNTHETIC-BARE-MIMO-TOKEN-UNLABELLED"
 rows=[json.loads(x) for x in FIXTURE.read_text().splitlines()];rows[9]["data"]["content"]=[{"type":"text","text":"old command output "+secret}]
 raw="\n".join(json.dumps(x) for x in rows)
 env=tmp_path/"host.env";env.write_text("MIMO_API_KEY="+secret+"\n");cfg=tmp_path/"runtime.json";cfg.write_text(json.dumps({"redaction_secret_refs":[{"file":str(env),"format":"dotenv","names":["MIMO_API_KEY"]}]}))
 box=tmp_path/"outbox.db";a=[sys.executable,"-m","dma.integrations.dsh.capture","--workspace","owner:demo","--instance","instance:pilot-dsh","--actor","actor:dsh","--session",SID,"--outbox",str(box),"--redaction-config",str(cfg)]
 result=subprocess.run(a,input=raw,text=True,capture_output=True,timeout=20)
 assert result.returncode==0,result.stdout+result.stderr
 assert secret not in result.stdout+result.stderr
 with Outbox(box).read() as c:payloads=[x[0] for x in c.execute("SELECT payload_json FROM outbox")]
 assert all(secret not in x for x in payloads) and any("KNOWN_SECRET" in x for x in payloads)
 assert secret not in box.read_bytes().decode(errors="ignore")
