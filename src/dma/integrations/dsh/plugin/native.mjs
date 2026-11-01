/** DSH 0.2.1-rc.2 Cordis loader: agent-scoped recall + native durable capture. */
import {spawn} from 'node:child_process';
import {pathToFileURL} from 'node:url';
import {installUnifiedMemoryPlugin} from './unified-memory.mjs';
export const name='unified-memory-native';
export const inject=['systemPrompt','sessions','sessionPersistence'];

function capture(config,id,log){
 const args=['-m','dma.integrations.dsh.capture','--workspace',config.workspaceId,'--instance',config.instanceId,'--actor',config.actorId,'--session',id,'--outbox',config.outboxPath,'--with-lineage'];
 if(config.redactionConfigPath)args.push('--redaction-config',config.redactionConfigPath);
 for(const project of config.captureProjectIds??config.projectIds??[])args.push('--project',project);
 return new Promise((resolve,reject)=>{
  const child=spawn(config.pythonPath,args,{stdio:['pipe','pipe','pipe']});let output='';let settled=false;
  const finish=(error,result)=>{if(settled)return;settled=true;clearTimeout(timer);error?reject(error):resolve(result);};
  const timer=setTimeout(()=>{child.kill('SIGKILL');finish(new Error('unified-memory native capture timed out; native log retained'));},config.captureTimeoutMs??15000);
  child.stdout.on('data',chunk=>{output+=chunk.toString();if(output.length>65536){child.kill('SIGKILL');finish(new Error('unified-memory capture returned oversized status'));}});
  // Never forward raw subprocess diagnostics or native stdin to the model/log.
  child.stderr.resume();child.stdin.on('error',()=>{});child.on('error',()=>finish(new Error('unified-memory capture process unavailable')));
  child.on('close',code=>{if(code!==0)return finish(new Error('unified-memory native capture failed; native log retained'));try{const result=JSON.parse(output);if(!result.ok)throw new Error();finish(null,result);}catch{finish(new Error('unified-memory capture returned invalid status'));}});
  child.stdin.end(JSON.stringify(log));
 });
}

export function apply(ctx,config){
 for(const key of ['socketPath','clientId','workspaceId','instanceId','actorId','outboxPath','pythonPath','sessionExportModule'])if(!config[key])throw new Error('unified-memory native missing '+key);
 const exportsReady=import(pathToFileURL(config.sessionExportModule).href);
 ctx.on('agent/created',({agent})=>{
  const memory=installUnifiedMemoryPlugin(agent.ctx,{...config,sessionId:agent.id});
  // A terminal model error skips turn-stopping. Save the committed prefix
  // before the next model request; overlapping native IDs remain idempotent.
  const checkpoint=async()=>{
    const {flushLiveSessionLog,readSessionLogText}=await exportsReady;
    await flushLiveSessionLog({sessions:ctx.sessions},agent.id);
    const log=await readSessionLogText(ctx.sessionPersistence,agent.id);
    if(typeof log!=='string')throw new Error('unified-memory native session not persisted');
    // Resolve copied history through the host's own logical export, in memory.
    // No raw handoff files, guessed new IDs, or silent inherited-message loss.
    const ancestors={};const visited=new Set([agent.id]);let cursor=log;
    for(let depth=0;;depth++){
     const first=cursor.split('\n').find(line=>line.trim());const header=JSON.parse(first||'null');
     if(!header||header.type!=='session'||header.version!==4)throw new Error('unified-memory native session header unavailable');
     if(!header.isSeeded)break;
     const parent=header.parentSession;
     if(typeof parent!=='string'||visited.has(parent)||depth>=64)throw new Error('unified-memory native fork lineage unavailable');
     visited.add(parent);const parentLog=await readSessionLogText(ctx.sessionPersistence,parent);
     if(typeof parentLog!=='string')throw new Error('unified-memory native ancestor not persisted');
     const parentHeader=JSON.parse(parentLog.split('\n').find(line=>line.trim())||'null');
     if(parentHeader?.id!==parent)throw new Error('unified-memory native ancestor identity mismatch');
     ancestors[parent]=parentLog;cursor=parentLog;
    }
    await capture(config,agent.id,{log,ancestors});
  };
  agent.ctx.on('agent/pre-step',async(payload,next)=>{await checkpoint();await memory.onPreStep(payload);return next();});
  agent.ctx.on('agent/turn-stopping',async()=>{
   try{await checkpoint();}finally{memory.onTurnStopping();}
  });
 });
}
