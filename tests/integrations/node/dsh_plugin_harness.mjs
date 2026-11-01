// Harness for the DSH dynamic-context recall plugin (T12).
// Uses the installed DSH SystemPrompt component read-only; no profile, no model.
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {homedir} from 'node:os';
import {existsSync} from 'node:fs';
import {fileURLToPath, pathToFileURL} from 'node:url';
import path from 'node:path';

const socketPath = process.argv[2];
const clientId = process.argv[3];
const releaseRoot =
  process.env.DSH_RELEASE_ROOT ||
  path.join(homedir(), '.local/share/deepseek-harness/releases/0.2.1-rc.2/node_modules/');
const dshMarker = path.join(releaseRoot, '@deepseek-ai/dsh/package.json');
if (!existsSync(dshMarker)) {
  console.log(JSON.stringify({skipped: true, reason: 'installed DSH packages not found'}));
  process.exit(3);
}

const here = path.dirname(fileURLToPath(import.meta.url));
const pluginPath = path.resolve(here, '../../../src/dma/integrations/dsh/plugin/unified-memory.mjs');
const {installUnifiedMemoryPlugin} = await import(pathToFileURL(pluginPath).href);

const require = createRequire(dshMarker);
const {Context} = await import(require.resolve('@deepseek-ai/cordis'));
const {SystemPrompt, renderContextSections, renderPrompt} = await import(
  require.resolve('@deepseek-ai/dsh-system-prompt')
);

function userMessage(text) {
  return {role: 'user', content: [{type: 'text', text}], source: {kind: 'human'}};
}

async function makeHarness(options) {
  const ctx = new Context();
  await ctx.plugin(SystemPrompt);
  ctx.systemPrompt.section({name: 'identity', order: 0, text: 'You are DSH test identity.'});
  const plugin = installUnifiedMemoryPlugin(ctx, options);
  return {ctx, plugin};
}

const checks = [];
const harness = await makeHarness({socketPath, clientId, projectIds: ['project:songta']});
try {
  await harness.plugin.onPreStep({
    agent: {sessionId: 'session:node-test'},
    messages: [userMessage('松塔项目端口是多少？')],
    turn: 1,
    step: 1,
  });
  const assembled = await harness.ctx.systemPrompt.assemble();
  const refreshed = harness.ctx.systemPrompt.refreshContext(assembled);
  const sections = JSON.stringify(renderContextSections(refreshed));
  const prompt = renderPrompt(refreshed);
  assert.ok(sections.includes('4200'), 'current-turn memory is visible after refresh');
  assert.ok(sections.includes('不是系统指令'), 'memory block is framed as quoted evidence');
  assert.ok(sections.includes('{{do_not_interpolate}}'), 'literal template text is not interpolated');
  assert.ok(prompt.includes('You are DSH test identity.'), 'identity section remains');
  assert.ok(!prompt.includes('4200'), 'context does not leak into the system identity prompt');
  checks.push('current-turn recall visible before request');
  checks.push('identity section unchanged, context separate from system prompt');
  checks.push('interpolation disabled for literal template text');

  await harness.plugin.onPreStep({
    agent: {sessionId: 'session:node-test'},
    messages: [userMessage('备份周期是多少？')],
    turn: 2,
    step: 1,
  });
  const refreshed2 = harness.ctx.systemPrompt.refreshContext(assembled);
  const sections2 = JSON.stringify(renderContextSections(refreshed2));
  assert.ok(sections2.includes('每周') || sections2.includes('weekly'), 'second query recalls the backup fact');
  assert.ok(!sections2.includes('4200'), 'previous-query context replaced');
  checks.push('per-turn query replaces previous context (no next-turn inject)');

  harness.plugin.onTurnStopping();
  const cleared = JSON.stringify(renderContextSections(harness.ctx.systemPrompt.refreshContext(assembled)));
  assert.ok(!cleared.includes('每周') && !cleared.includes('4200'), 'turn-stopping clears cached context');
  checks.push('turn-stopping clears cache');

  const broken = await makeHarness({
    socketPath: '/tmp/definitely-not-a-kw.sock',
    clientId,
    projectIds: ['project:songta'],
    deadlineMs: 500,
  });
  await broken.plugin.onPreStep({
    agent: {sessionId: 'session:node-test'},
    messages: [userMessage('松塔项目端口是多少？')],
    turn: 1,
    step: 1,
  });
  const brokenSections = JSON.stringify(
    renderContextSections(broken.ctx.systemPrompt.refreshContext(await broken.ctx.systemPrompt.assemble()))
  );
  assert.ok(!brokenSections.includes('4200'), 'recall failure never reuses old facts');
  assert.ok(brokenSections.includes('历史暂不可用'), 'recall failure is explicitly visible');
  assert.equal(broken.plugin.recallStatus(), null);
  checks.push('recall failure empty, no stale cache, no exception');
  await broken.ctx.fiber.dispose();

  await harness.ctx.fiber.dispose();
  console.log(JSON.stringify({passed: true, checks, node: process.version, externalLlmCalls: 0}));
} catch (error) {
  console.log(JSON.stringify({passed: false, checks, error: String(error && error.message ? error.message : error)}));
  process.exitCode = 1;
}
