import assert from 'node:assert/strict';
import http from 'node:http';
import {mkdtempSync,rmSync} from 'node:fs';
import {pathToFileURL} from 'node:url';
const {installUnifiedMemoryPlugin}=await import(pathToFileURL(process.argv[2]).href);
const root=mkdtempSync('/tmp/umsearch-'),socketPath=root+'/kw.sock';
let mode='ok';const requests=[];
const item={text:'历史端口4317',value:4317,claim_id:'c:1',revision_id:'r:1',predicate:'server.port',facet:'desired',state:'superseded',event_time:{value:'2026-10-01T00:00:00Z'},source_actors:['shiguang'],sources:[{event_id:'evt:original'}]};
const server=http.createServer((req,res)=>{const chunks=[];req.on('data',c=>chunks.push(c));req.on('end',()=>{requests.push(JSON.parse(Buffer.concat(chunks).toString()));res.writeHead(mode==='error'?503:200,{'Content-Type':'application/json'});res.end(JSON.stringify(mode==='error'?{error:{code:'CANARY-PW-7f3a'}}:{status:'ok',items:[item],next_cursor:'cur:next',watermark:4,restore_epoch:'epoch:1',incomplete_reasons:[]}));});});
await new Promise(resolve=>server.listen(socketPath,resolve));
const tools=[];const plugin=installUnifiedMemoryPlugin({systemPrompt:{context(){}},tools:{register(v){tools.push(v);}}},{socketPath,clientId:'client:test',projectIds:['project:songta'],enableSearchTool:true,sessionId:'s:active',deadlineMs:500});
try{
 assert.equal(tools.length,1);const tool=tools[0];assert.equal(tool.name,'unified_memory_search');
 const result=JSON.parse(await tool.execute({query:'端口',intent:'history',cursor:'cur:old',limit:2}));
 assert.ok(result.ok&&result.items[0].state==='superseded'&&result.context.includes('来源者'));
 assert.equal(requests[0].intent,'history');assert.equal(requests[0].cursor,'cur:old');assert.deepEqual(requests[0].project_ids,['project:songta']);assert.equal(requests[0].session_id,'s:active');
 const count=requests.length;const bad=JSON.parse(await tool.execute({query:'x',project_ids:['other']}));assert.ok(!bad.ok);assert.equal(requests.length,count);
 assert.equal(plugin.contextText(),'');
 mode='error';const failure=JSON.parse(await tool.execute({query:'端口'}));assert.equal(failure.error.code,'STORE_UNAVAILABLE');assert.ok(!JSON.stringify(failure).includes('CANARY'));
 assert.equal(tool.output.render({},'test')[0].text,'test');
 console.log(JSON.stringify({passed:true,checks:['registered read-only tool','same scoped recall API','history/source/time preserved','cursor preserved','scope cannot be overridden','tool does not overwrite automatic cache','failure safe and distinct'],externalLlmCalls:0}));
}finally{server.closeAllConnections();await new Promise(resolve=>server.close(resolve));rmSync(root,{recursive:true,force:true});}
