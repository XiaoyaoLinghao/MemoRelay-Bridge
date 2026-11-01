import json,time,tempfile,shutil,threading,http.server,socketserver
from pathlib import Path
import pytest
from dma.integrations.search import ReadOnlySearch
from dma.integrations.recall_client import RecallClient,RecallUnavailable
from dma.integrations.source_details import attach_source_details
ID='evt:'+'a'*64

def raw_result():return {'status':'ok','used_tokens':20,'items':[{'text':'端口是6421','value':6421,'project_id':'project:imac','state':'current','sources':[{'event_id':ID,'event_revision':1,'part_index':0}]}]}
class Client:
 def __init__(self,parts=2,text='原始业务资料：端口6421',availability='full'):self.parts=parts;self.text=text;self.availability=availability;self.reads=[]
 def recall(self,**kwargs):return raw_result()
 def fetch_source(self,event,revision,part,timeout=None):
  self.reads.append((event,revision,part,timeout));return {'event_id':event,'event_revision':revision,'part_index':part,'part_count':self.parts,'availability':self.availability,'text':self.text if self.availability=='full' else None,'source':{'actor_id':'actor:original'},'event_time':{'value':'2026-10-01','precision':'day','timezone':'UTC'}}

def test_optional_source_detail_reads_exact_original_revisions_and_all_parts():
 c=Client();tool=ReadOnlySearch(c,project_ids=('project:imac',));ordinary=tool.call({'query':'端口'},session_id='s:1');assert not c.reads and 'source_details' not in ordinary
 detailed=tool.call({'query':'端口','include_source_details':True},session_id='s:1')
 assert detailed['source_details_complete'] and [x['part_index'] for x in detailed['source_details']]==[0,1]
 assert all(v['text']==c.text and v['event_time']['value']=='2026-10-01' and v['source']['actor_id']=='actor:original' for v in detailed['source_details'])
 assert all(v[0:2]==(ID,1) and 0<v[3]<=6.5 for v in c.reads)
 assert detailed['items']==ordinary['items']


def test_expired_source_stays_unavailable_while_fact_is_still_returned():
 result=ReadOnlySearch(Client(availability='expired'),project_ids=('project:imac',)).call({'query':'端口','include_source_details':True},session_id='s:1')
 assert result['items'][0]['value']==6421 and not result['source_details_complete']
 assert all(v['text'] is None and v['detail_status']=='expired' for v in result['source_details'])
 assert 'source_not_available' in result['source_details_incomplete_reasons']


def test_detail_budget_utf8_truncation_and_max_parts_are_explicit():
 c=Client(parts=100,text='资料'*20000);result=ReadOnlySearch(c,project_ids=('project:imac',)).call({'query':'端口','include_source_details':True,'budget_tokens':256},session_id='s:1')
 assert not result['source_details_complete'] and 'budget' in result['source_details_incomplete_reasons']
 text=result['source_details'][0]['text'];assert text and c.text.startswith(text) and '\ufffd' not in text and len(text.encode())<=256*3
 c=Client(parts=100,text='短资料');result=ReadOnlySearch(c,project_ids=('project:imac',)).call({'query':'端口','include_source_details':True,'budget_tokens':8000},session_id='s:1')
 assert len(c.reads)==8 and not result['source_details_complete'] and 'more_parts' in result['source_details_incomplete_reasons']


def test_source_errors_are_safe_and_no_lookup_starts_after_deadline():
 class Offline(Client):
  def fetch_source(self,*args,**kw):raise RecallUnavailable('CANARY-PW-7f3a')
 output=ReadOnlySearch(Offline(),project_ids=('project:imac',)).call({'query':'端口','include_source_details':True},session_id='s:1')
 assert 'CANARY' not in json.dumps(output) and output['source_details'][0]['error_code']=='STORE_UNAVAILABLE'
 c=Client();output={'items':raw_result()['items']};attach_source_details(output,raw_result(),c,budget_tokens=8000,deadline=time.monotonic()-1)
 assert not c.reads and 'deadline' in output['source_details_incomplete_reasons']


def test_actual_uds_source_wait_shares_the_total_query_deadline():
 root=Path(tempfile.mkdtemp(prefix='umdetail-',dir='/tmp'));requests=[]
 class Handler(http.server.BaseHTTPRequestHandler):
  def log_message(self,*args):pass
  def send(self,payload):
   body=json.dumps(payload).encode()
   try:self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
   except (BrokenPipeError,ConnectionResetError):pass
  def do_POST(self):self.rfile.read(int(self.headers['Content-Length']));requests.append(self.path);self.send(raw_result())
  def do_GET(self):requests.append(self.path);time.sleep(.4);self.send({'text':'late'})
 class Server(socketserver.UnixStreamServer):pass
 server=Server(str(root/'kw.sock'),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
 try:
  tool=ReadOnlySearch(RecallClient(root/'kw.sock','client:scope'),project_ids=('project:imac',),deadline_ms=120);start=time.monotonic();result=tool.call({'query':'端口','include_source_details':True},session_id='s:1');elapsed=time.monotonic()-start
  assert elapsed<.7 and not result['source_details_complete'] and result['items'][0]['value']==6421
  assert len(requests)==2 and requests[1]==f'/v1/sources/{ID}?revision=1&part=0'
 finally:server.shutdown();server.server_close();thread.join();shutil.rmtree(root)


def test_node_source_detail_uds_parity():
 import os,subprocess
 node=os.environ.get('NODE_BIN') or shutil.which('node')
 if not node:pytest.skip('Node unavailable')
 root=Path(__file__).parents[2];argv=[node,str(root/'tests/integrations/node/source_details.mjs'),str(root/'src/dma/integrations/dsh/plugin/unified-memory.mjs')]
 v=subprocess.run(argv,capture_output=True,text=True,timeout=15);assert v.returncode==0,v.stdout+v.stderr
 assert json.loads(v.stdout)['passed']


@pytest.mark.parametrize('operation',['source','recall'])
def test_interrupted_body_is_reported_unavailable_without_partial_content(monkeypatch,operation):
 import http.client
 import dma.integrations.recall_client as module
 closed=[]
 class Response:
  status=200
  def read(self):raise http.client.IncompleteRead(b'partial-private-payload')
 class Connection:
  def request(self,*args,**kwargs):pass
  def getresponse(self):return Response()
  def close(self):closed.append(True)
 monkeypatch.setattr(module,'_UnixHTTPConnection',lambda *a,**kw:Connection())
 client=RecallClient('/tmp/fixture-only.sock','client:source')
 with pytest.raises(RecallUnavailable) as exc:
  if operation=='source':client.fetch_source(ID,1)
  else:client.recall(query='端口',session_id='s:1',turn_id='t:1',request_id='req:test')
 assert exc.value.code=='STORE_UNAVAILABLE' and 'partial-private' not in str(exc.value) and closed
