import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync, writeFileSync, mkdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { LeosAgent } from '../../index.js';
import piExtension from '../../pi-extension.js';
import { runPython } from '../../scripts/harness_bridge.js';

const root = resolve(import.meta.dirname, '../..');
process.env.LEOS_AGENT_PRICE_REFRESH = 'off';
process.env.LEOS_AGENT_ROOT = root;
const storage = mkdtempSync(join(tmpdir(), 'leo adapter test '));
process.env.LEOS_AGENT_LOCAL_PATH = storage;
test.after(() => rmSync(storage, { recursive: true, force: true }));

test('OpenCode corrects native profile selection without adding a model field', async () => {
  const hooks = await LeosAgent({ directory: storage, client: { app: { agents: async () => ({ data: [
    { name: 'leo-standard', model: { providerID: 'openai', id: 'gpt-5.6-terra' } },
    { name: 'leo-parent' },
  ] }) } } });
  await hooks.event({ event: { type: 'message.updated', properties: { info: {
    sessionID: 's', model: { providerID: 'openai', id: 'gpt-5.6-sol' },
  } } } });
  const output = { args: { subagent_type: 'general', prompt: 'Investigate the regression', description: 'Investigate' } };
  await hooks['tool.execute.before']({ tool: 'task', sessionID: 's', callID: 'c' }, output);
  assert.equal(output.args.subagent_type, 'leo-parent');
  assert.equal(output.args.prompt, 'Investigate the regression');
  assert.equal('model' in output.args, false);
});

test('OpenCode leaves unrelated tools untouched without querying agents', async () => {
  const hooks = await LeosAgent({ client: { app: { agents: () => { throw new Error('should not query'); } } } });
  const args = { command: 'pwd' };
  await hooks['tool.execute.before']({ tool: 'bash' }, { args });
  assert.deepEqual(args, { command: 'pwd' });
});

test('Pi uses one skill discovery source and caches session payload', async () => {
  const hooks = {};
  piExtension({ on: (name, callback) => { hooks[name] = callback; } });
  assert.equal(hooks.resources_discover, undefined);
  assert.equal(typeof hooks.tool_call, 'function');
  const first = await hooks.before_agent_start({ systemPrompt: 'original' });
  const second = await hooks.before_agent_start({ systemPrompt: 'original' });
  assert.equal(first.systemPrompt, second.systemPrompt);
  assert.ok(first.systemPrompt.startsWith('original\n\n'));
  assert.ok(first.systemPrompt.includes('Cost-aware delegation'));
});

test('bridge handles spaces and terminates a hung script', async () => {
  const location = mkdtempSync(join(tmpdir(), 'leo bridge space '));
  try {
    mkdirSync(join(location, 'scripts'));
    writeFileSync(join(location, 'scripts/slow.py'), 'import time\ntime.sleep(10)\n');
    const start = Date.now();
    assert.equal(await runPython(location, 'pi', 'slow.py', [], {}, 100), null);
    assert.ok(Date.now() - start < 3000);
    writeFileSync(join(location, 'scripts/echo.py'), 'print("ok")\n');
    assert.equal((await runPython(location, 'pi', 'echo.py')).stdout.trim(), 'ok');
  } finally { rmSync(location, { recursive: true, force: true }); }
});
