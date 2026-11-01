"""Native DSH logical-log handoff: stdin -> redaction -> durable DMA outbox.

No model/network call. Raw native JSONL stays in memory, never in arguments,
temporary files, stdout or exception text. The host owns native persistence.
"""
import argparse,json,sys
from dma.adapters.dsh.reader import DshSessionReader
from dma.adapters.dsh.adapter import DshAdapter
from dma.outbox import Outbox
from dma.pipeline import capture
from dma.redaction import Redactor
from dma.redaction.references import redactor_from_config

def main():
 p=argparse.ArgumentParser()
 for key in ('workspace','instance','actor','session','outbox'):p.add_argument('--'+key,required=True)
 p.add_argument('--redaction-config',default=None);p.add_argument('--project',action='append',default=[]);p.add_argument('--with-lineage',action='store_true');args=p.parse_args()
 try:
  raw=sys.stdin.read();ancestors={}
  if args.with_lineage:
   envelope=json.loads(raw)
   if not isinstance(envelope,dict) or not isinstance(envelope.get('log'),str) or not isinstance(envelope.get('ancestors'),dict):raise ValueError('invalid lineage handoff')
   raw=envelope['log'];ancestors=envelope['ancestors']
   if len(ancestors)>64 or any(not isinstance(k,str) or not isinstance(v,str) for k,v in ancestors.items()):raise ValueError('invalid ancestor handoff')
  reader=DshSessionReader(None,session_log=raw,ancestor_logs=ancestors,instance_id=args.instance,session_id=args.session,actor_id=args.actor,project_ids=tuple(args.project))
  box=Outbox(args.outbox,workspace_id=args.workspace,instance_id=args.instance)
  result=capture(reader,box,stream='dsh:'+args.session,convert=DshAdapter(redactor_from_config(args.redaction_config) if args.redaction_config else Redactor(),workspace_id=args.workspace,instance_id=args.instance).to_events)
  print(json.dumps({'ok':True,'capture':result}));return 0
 except Exception as exc:
  # SourceReadError may contain file names or schema text. Do not expose the
  # native body in a host prompt/error; the type and a retryable native log remain.
  print(json.dumps({'ok':False,'error':'NATIVE_CAPTURE_FAILED','error_type':type(exc).__name__}));return 1
if __name__=='__main__':raise SystemExit(main())
