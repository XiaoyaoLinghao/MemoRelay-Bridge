import assert from 'node:assert/strict';
import {pathToFileURL} from 'node:url';
import {readFileSync} from 'node:fs';
const [pluginPath,exportsPath,fixturePath,pythonPath,outboxPath]=process.argv.slice(2);
const {apply}=await import(pathToFileURL(pluginPath).href);
const fixture=JSON.parse(readFileSync(fixturePath,'utf8'));
const logs=Object.fromEntries(Object.entries(fixture).map(([id,v])=>[id,[{type:'session',...v.header},...v.events].map(x=>JSON.stringify(x)).join('\n')]));
const listeners={},agentListeners={};
const ctx={sessions:{flushed:[]},sessionPersistence:{logs,reads:[]},on(name,fn){listeners[name]=fn;}};
const id='session-preprod-grandchild';
apply(ctx,{socketPath:'/tmp/no-preprod-recall.sock',clientId:'client:fork',workspaceId:'owner:demo',instanceId:'instance:fork',actorId:'dsh-test',outboxPath,pythonPath,sessionExportModule:exportsPath,projectIds:[],captureProjectIds:['project:songta']});
listeners['agent/created']({agent:{id,ctx:{systemPrompt:{context(){}},on(name,fn){agentListeners[name]=fn;}}}});
// Emulate a terminal model failure: pre-step ran, turn-stopping never runs.
await agentListeners['agent/pre-step']({messages:[]},async()=>{});
assert.deepEqual(ctx.sessions.flushed,[id]);
// Normal completion repeats the same prefix safely.
await agentListeners['agent/turn-stopping']();
assert.deepEqual(ctx.sessions.flushed,[id,id]);
assert.deepEqual(ctx.sessionPersistence.reads,[id,'session-preprod-child','session-preprod-root',id,'session-preprod-child','session-preprod-root']);
console.log(JSON.stringify({passed:true,reads:ctx.sessionPersistence.reads,flushed:ctx.sessions.flushed,externalLlmCalls:0}));
