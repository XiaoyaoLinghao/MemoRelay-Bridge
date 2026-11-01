import assert from 'node:assert/strict';
import http from 'node:http';
import {mkdtempSync, rmSync} from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';

const {installUnifiedMemoryPlugin} = await import(pathToFileURL(process.argv[2]).href);
const dir = mkdtempSync('/tmp/um-notice-');
const socketPath = path.join(dir, 'kw.sock');
let mode = 'ok';
const item = {text:'银杏验收服务端口为4317', predicate:'server.port', facet:'desired',
              state:'current', source_actors:['shiguang-test'], sources:[{}]};
const server = http.createServer((req, res) => {
  req.resume();
  if (mode === 'timeout') return;
  res.writeHead(mode === 'error' ? 503 : 200, {'Content-Type':'application/json'});
  if (mode === 'error') return res.end(JSON.stringify({error:{code:'CANARY-PW-7f3a'}}));
  res.end(JSON.stringify({status:mode === 'degraded' ? 'degraded' : mode === 'empty' ? 'empty' : 'ok',
    items: mode === 'empty' ? [] : [item], incomplete_reasons: mode === 'degraded' ? ['VECTOR_UNAVAILABLE'] : []}));
});
await new Promise(resolve => server.listen(socketPath, resolve));
const make = id => installUnifiedMemoryPlugin({systemPrompt:{context(){}}},
  {socketPath,clientId:'client:test',sessionId:id,deadlineMs:100});
const turn = (id, text='银杏验收端口') => ({agent:{id}, messages:[{role:'user',content:text}],turn:1});
const a=make('s:a'), b=make('s:b');
const checks=[];
try {
  await a.onPreStep(turn('s:a'));
  assert.ok(a.contextText().includes('4317')); checks.push('current fact before failure');
  mode='error'; await a.onPreStep(turn('s:a','另一个项目端口'));
  assert.ok(a.contextText().includes('历史暂不可用'));
  assert.ok(!a.contextText().includes('4317') && !a.contextText().includes('CANARY'));
  assert.equal(a.state.lastErrorCode,'STORE_UNAVAILABLE'); checks.push('error visible, no stale fact or raw diagnostic');
  mode='empty'; await a.onPreStep(turn('s:a'));
  assert.ok(a.contextText().includes('未找到') && !a.contextText().includes('历史暂不可用')); assert.equal(a.state.lastErrorCode,null);
  assert.equal(a.recallStatus().count,0); checks.push('successful empty differs from unavailable');
  mode='degraded'; await a.onPreStep(turn('s:a'));
  assert.ok(a.contextText().includes('检索不完整') && a.contextText().includes('4317'));
  assert.equal(a.recallStatus().count,1); checks.push('partial evidence retained with incomplete notice');
  await b.onPreStep(turn('s:b')); b.onTurnStopping();
  assert.equal(b.contextText(),''); assert.ok(a.contextText().includes('4317'));
  checks.push('session state independent');
  mode='timeout'; const started=Date.now();await a.onPreStep(turn('s:a'));
  assert.ok(a.contextText().includes('历史暂不可用'));assert.ok(Date.now()-started<2000);
  checks.push('bounded timeout exposes current status');
  await a.onPreStep(turn('s:a','ok'));assert.equal(a.contextText(),'');
  assert.equal(a.state.lastErrorCode,null);checks.push('trivial turn clears status');
  console.log(JSON.stringify({passed:true,checks,externalLlmCalls:0}));
} finally {
  server.closeAllConnections();await new Promise(resolve => server.close(resolve));
  rmSync(dir,{recursive:true,force:true});
}
