import json,os,subprocess,shutil
from pathlib import Path
import pytest
from dma.integrations.scopes import QueryScopes
from dma.integrations.search import ReadOnlySearch
from dma.integrations.hermes import DmaMemoryProvider
from dma.integrations.formatting import format_recall_notice

ALIASES={"iMac":"project:imac","fnOS":"project:fnos","小米智能家居":"project:home","mi.home":"project:home"}
ALLOWED=tuple(set(ALIASES.values()))
@pytest.mark.parametrize("query,expected",[("iMac录像整理",["project:imac"]),("查询 FNOS 状态",["project:fnos"]),("查询小米智能家居",["project:home"]),("miniMac并非项目别名",[]),("iMac和fnOS状态",[]),("mi.home设备",["project:home"]),("miXhome设备",[]),("本轮没有项目名",[])])
def test_known_aliases_and_ambiguity(query,expected):
 assert list(QueryScopes(aliases=ALIASES,allowed=ALLOWED).resolve(query))==expected

class Client:
 def __init__(self):self.requests=[]
 def recall(self,**kw):
  self.requests.append(kw)
  if not kw['project_ids'] and kw.get('intent','current')!='all_projects':return {"status":"degraded","items":[],"scope_candidates":[{"project_id":"project:imac"},{"project_id":"project:fnos"}],"incomplete_reasons":["scope_unresolved"]}
  return {"status":"empty","items":[]}

def test_candidate_selection_and_global_search_are_explicit_and_bounded():
 client=Client();scopes=QueryScopes(aliases=ALIASES,allowed=ALLOWED,allow_workspace=True);tool=ReadOnlySearch(client,scopes=scopes)
 first=tool.call({"query":"以前的项目进度"},session_id='s:1')
 assert first['ok'] and '尚未确定项目范围' in first['context'] and 'project:imac' in first['context'] and '未找到' not in first['context']
 selected=tool.call({"query":"以前的项目进度","project_ids":["project:fnos"]},session_id='s:1')
 assert selected['ok'] and client.requests[-1]['project_ids']==('project:fnos',)
 n=len(client.requests);assert not tool.call({"query":"项目进度","project_ids":["project:private"]},session_id='s:1')['ok'];assert len(client.requests)==n
 assert tool.call({"query":"所有项目中的用户汇报偏好","intent":"all_projects"},session_id='s:1')['ok'];assert client.requests[-1]['intent']=='all_projects' and not client.requests[-1]['project_ids']
 assert not ReadOnlySearch(client).call({"query":"所有项目","intent":"all_projects"},session_id='s:1')['ok']


def test_provider_switches_scope_per_current_question_without_stale_binding():
 client=Client();provider=DmaMemoryProvider(recall_client=client,client_id='client:test',enable_search_tool=True,query_project_aliases=ALIASES,allowed_project_ids=ALLOWED);provider.initialize('s:test')
 provider.prefetch('iMac录像整理');provider.prefetch('fnOS项目状态');text=provider.prefetch('iMac和fnOS的设备端口');provider.prefetch('iMac录像整理')
 assert [v['project_ids'] for v in client.requests]==[('project:imac',),('project:fnos',),(),('project:imac',)]
 assert '尚未确定项目范围' in text
 value=json.loads(provider.handle_tool_call('unified_memory_search',{'query':'项目状态','project_ids':['project:fnos']}));assert value['ok']

def test_scope_aliases_cannot_expand_operator_allowlist():
 with pytest.raises(ValueError):QueryScopes(aliases=ALIASES,allowed=['project:imac'])


def test_node_project_alias_and_candidate_notice_parity(tmp_path):
 node=os.environ.get('NODE_BIN') or shutil.which('node')
 if not node:pytest.skip('Node runtime unavailable')
 root=Path(__file__).parents[2];module=root/'src/dma/integrations/dsh/plugin/unified-memory.mjs'
 script=tmp_path/'check.mjs';script.write_text("import assert from 'node:assert/strict';import {pathToFileURL} from 'node:url';const {resolveQueryProjects}=await import(pathToFileURL(process.argv[2]));const o=JSON.parse(process.argv[3]);for(const [q,w] of o.cases)assert.deepEqual(resolveQueryProjects(q,o),w);assert.deepEqual(resolveQueryProjects('unknown',o,['project:fnos']),['project:fnos']);assert.throws(()=>resolveQueryProjects('x',o,['project:private']));console.log('passed');")
 cases=[["iMac录像整理",['project:imac']],["miniMac",[]],["fnOS项目",['project:fnos']],["iMac和fnOS",[]],["mi.home",['project:home']],["miXhome",[]]]
 v=subprocess.run([node,str(script),str(module),json.dumps({'projectAliases':ALIASES,'allowedProjectIds':ALLOWED,'cases':cases})],capture_output=True,text=True,timeout=15)
 assert v.returncode==0,v.stdout+v.stderr


def test_node_real_uds_project_switch_and_candidate_selection():
 node=os.environ.get('NODE_BIN') or shutil.which('node')
 if not node:pytest.skip('Node runtime unavailable')
 root=Path(__file__).parents[2]
 v=subprocess.run([node,str(root/'tests/integrations/node/project_scope.mjs'),str(root/'src/dma/integrations/dsh/plugin/unified-memory.mjs')],capture_output=True,text=True,timeout=15)
 assert v.returncode==0,v.stdout+v.stderr
 assert json.loads(v.stdout)['passed']


def test_native_tool_schema_exposes_exact_project_tokens_without_mutating_other_providers():
 a=DmaMemoryProvider(recall_client=Client(),client_id='client:a',project_ids=('project:imac',),enable_search_tool=True)
 b=DmaMemoryProvider(recall_client=Client(),client_id='client:b',project_ids=('project:fnos',),enable_search_tool=True,allow_workspace_search=True)
 sa=a.get_tool_schemas()[0]['parameters']['properties'];sb=b.get_tool_schemas()[0]['parameters']['properties']
 assert sa['project_ids']['items']['enum']==['project:imac'] and sb['project_ids']['items']['enum']==['project:fnos']
 assert 'all_projects' not in sa['intent']['enum'] and 'all_projects' in sb['intent']['enum']
 assert a.get_tool_schemas()[0]['parameters']['properties']==sa
