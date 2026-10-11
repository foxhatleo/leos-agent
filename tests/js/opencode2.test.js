// OpenCode 2's plugin API as 2.0.26 runs it: setup(ctx) registers hooks that
// each get one mutable event; a replaced `input` is what executes, and a throw
// fails the call.
import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync, readFileSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import plugin from '../../index.js';

const root = resolve(import.meta.dirname, '../..');
const sandbox = mkdtempSync(join(tmpdir(), 'leo opencode2 '));
const storage = join(sandbox, 'local');
Object.assign(process.env, {
  LEOS_AGENT_PRICE_REFRESH: 'off', LEOS_AGENT_ROOT: root, LEOS_AGENT_LOCAL_PATH: storage, LEOS_AGENT_DISPATCH_GUARD: 'on',
  HOME: sandbox, CLAUDE_CONFIG_DIR: join(sandbox, 'claude'), CODEX_HOME: join(sandbox, 'codex'),
  HERMES_HOME: join(sandbox, 'hermes'), PI_CODING_AGENT_DIR: join(sandbox, 'pi'),
  OPENCODE_CONFIG_DIR: join(sandbox, 'opencode'), OPENCODE_CONFIG: join(sandbox, 'opencode.json'),
  XDG_CONFIG_HOME: join(sandbox, 'xdg'),
});
delete process.env.LEOS_AGENT_HARNESS;
test.after(() => rmSync(sandbox, { recursive: true, force: true }));

const rows = () => {
  const path = join(storage, 'dispatch.jsonl');
  return existsSync(path) ? readFileSync(path, 'utf8').trim().split('\n').map((l) => JSON.parse(l)) : [];
};

const SOL = { providerID: 'openai', id: 'gpt-5.6-sol' };
const LUNA = { providerID: 'openai', id: 'gpt-5.6-luna' };

// A context shaped like the one OpenCode 2 passes to setup().
const load = async (agents = []) => {
  const hooks = {};
  let listed = 0;
  const register = (domain) => async (name, callback) => {
    hooks[`${domain}.${name}`] = callback;
    return { dispose: async () => {} };
  };
  const ctx = {
    location: { directory: sandbox },
    agent: { list: async () => { listed += 1; return { location: { directory: sandbox }, data: agents }; } },
    session: { hook: register('session') },
    tool: { hook: register('tool') },
    event: { subscribe: () => { throw new Error('price refresh is off'); } },
  };
  const cleanup = await plugin.setup(ctx);
  return { hooks, cleanup, listed: () => listed };
};

const request = (sessionID, model = SOL) => ({
  sessionID, model, agent: 'build', system: [{ type: 'text', text: 'You are an AI agent running in OpenCode.' }],
  messages: [], options: {}, tools: {},
});

test('one default export serves both plugin APIs', () => {
  assert.equal(plugin.id, 'leos-agent');
  assert.equal(typeof plugin.setup, 'function');
  assert.equal(typeof plugin.server, 'function');
});

test('the context hook adds the rendered policy once per request', async () => {
  const { hooks } = await load();
  const first = request('s1');
  await hooks['session.context'](first);
  assert.equal(first.system.length, 2);
  assert.match(first.system[1].text, /^# Cost-aware delegation/);
  const again = request('s1');
  await hooks['session.context'](again);
  assert.deepEqual(again.system, first.system);
  // A system prompt that already carries it, as a loaded instructions file would.
  const carried = request('s1');
  carried.system.push({ type: 'text', text: `<!-- Managed by leos-agent. -->\n${first.system[1].text}\n` });
  await hooks['session.context'](carried);
  assert.equal(carried.system.length, 2);
});

test('a subagent over the parent runs on the parent model and is logged', async () => {
  const { hooks } = await load([{ id: 'leo-cheap', name: 'leo-cheap', model: { providerID: 'openai', id: 'gpt-5.6-terra' } }]);
  await hooks['session.context'](request('parent'));
  const event = { tool: 'subagent', sessionID: 'parent', agent: 'build', messageID: 'm', id: 'call-1',
    input: { agent: 'leo-cheap', description: 'd', prompt: 'Investigate' } };
  await hooks['tool.execute.before'](event);
  assert.deepEqual(event.input, { agent: 'leo-cheap', description: 'd', prompt: 'Investigate', model: 'openai/gpt-5.6-sol' });
  const row = rows().at(-1);
  assert.equal(row.harness, 'opencode');
  assert.equal(row.decision, 'correct');
  assert.equal(row.reason, 'over-ceiling');
  assert.equal(row.call_id, 'call-1');
  assert.equal(row.effective_model, 'openai/gpt-5.6-sol');
});

test('other tools pass untouched without listing agents', async () => {
  const { hooks, listed } = await load();
  const event = { tool: 'shell', sessionID: 'parent', id: 'c', input: { command: 'pwd' } };
  await hooks['tool.execute.before'](event);
  assert.deepEqual(event.input, { command: 'pwd' });
  assert.equal(listed(), 0);
});

test('a completed subagent is recorded by call id with the model its session ran on', async () => {
  const { hooks } = await load();
  await hooks['session.context'](request('ses_child', LUNA));
  const before = rows().length;
  const result = { sessionID: 'ses_child', status: 'completed', output: 'PRIVATE_OUTPUT\nResult: done\nVerified: node --test' };
  await hooks['tool.execute.after']({ tool: 'subagent', sessionID: 'parent', id: 'call-2', status: 'completed',
    input: { agent: 'leo-cheap', description: 'd', prompt: 'PRIVATE_BRIEF' },
    result: { output: result, content: [], metadata: { sessionID: 'ses_child', status: 'completed' } } });
  assert.equal(rows().length, before + 1);
  const row = rows().at(-1);
  assert.equal(row.call_id, 'call-2');
  assert.equal(row.agent, 'leo-cheap');
  assert.equal(row.agent_id, 'ses_child');
  assert.equal(row.effective_model, 'openai/gpt-5.6-luna');
  assert.equal(row.outcome, 'done');
  assert.equal(row.verified, true);
  const raw = readFileSync(join(storage, 'dispatch.jsonl'), 'utf8');
  assert.equal(raw.includes('PRIVATE_OUTPUT') || raw.includes('PRIVATE_BRIEF'), false);
});

test('a background launch and a failed call record nothing', async () => {
  const { hooks } = await load();
  const before = rows().length;
  await hooks['tool.execute.after']({ tool: 'subagent', sessionID: 'parent', id: 'call-3', status: 'completed',
    input: { agent: 'leo-cheap' }, result: { output: { sessionID: 'ses_bg', status: 'running', output: 'working' } } });
  await hooks['tool.execute.after']({ tool: 'subagent', sessionID: 'parent', id: 'call-4', status: 'error',
    input: { agent: 'leo-cheap' }, error: new Error('Unknown agent') });
  assert.equal(rows().length, before);
});
