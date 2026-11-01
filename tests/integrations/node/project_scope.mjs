import assert from 'node:assert/strict';
import http from 'node:http';import {mkdtempSync,rmSync} from 'node:fs';import {pathToFileURL} from 'node:url';
const {installUnifiedMemoryPlugin}=await import(pathToFileURL(process.argv[2]));
const root=mkdtempSync('/tmp/umscope-'),socketPath=root+'/kw.sock',requests=[];
const server=http.createServer((req,res)=>{const chunks=[];req.on('data',c=>chunks.push(c));req.on('end',()=>{const v=JSON.parse(Buffer.concat(chunks));requests.push(v);res.writeHead(200,{'Content-Type':'application/json'});res.end(JSON.stringify(v.project_ids.length||v.intent==='all_projects'?{status:'empty',items:[]}:{status:'degraded',items:[],incomplete_reasons:['scope_unresolved'],scope_candidates:[{project_id:'project:imac'},{project_id:'project:fnos'}]}));});});
await new Promise(r=>server.listen(socketPath,r));
const tools=[];const plugin=installUnifiedMemoryPlugin({systemPrompt:{context(){}},tools:{register(t){tools.push(t);}}},{socketPath,clientId:'client:scope',projectIds:[],allowedProjectIds:['project:imac','project:fnos'],projectAliases:{iMac:'project:imac',fnOS:'project:fnos'},allowWorkspaceSearch:true,enableSearchTool:true});
const step=q=>plugin.onPreStep({agent:{id:'s:scope'},messages:[{role:'user',content:q}]});
try{
 await step('iMac端口');await step('fnOS端口');await step('iMac和fnOS端口');assert.ok(plugin.contextText().includes('尚未确定项目范围')&&plugin.contextText().includes('project:imac'));await step('iMac端口');
 assert.deepEqual(requests.map(v=>v.project_ids),[['project:imac'],['project:fnos'],[],['project:imac']]);
 const selected=JSON.parse(await tools[0].execute({query:'项目端口',project_ids:['project:fnos']}));assert.ok(selected.ok);assert.deepEqual(requests.at(-1).project_ids,['project:fnos']);
 assert.deepEqual(tools[0].parameters.properties.project_ids.items.enum,['project:imac','project:fnos']);
 const n=requests.length;assert.ok(!JSON.parse(await tools[0].execute({query:'项目端口',project_ids:['project:private']})).ok);assert.equal(requests.length,n);
 assert.ok(JSON.parse(await tools[0].execute({query:'所有项目里的汇报偏好',intent:'all_projects'})).ok);assert.equal(requests.at(-1).intent,'all_projects');assert.deepEqual(requests.at(-1).project_ids,[]);
 console.log(JSON.stringify({passed:true,requests:requests.map(v=>({query:v.query,projects:v.project_ids,intent:v.intent})),externalModelCalls:0}));
}finally{server.closeAllConnections();await new Promise(r=>server.close(r));rmSync(root,{recursive:true,force:true});}
