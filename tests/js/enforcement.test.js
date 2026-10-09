// Host-faithful simulations of the OpenCode and Pi dispatch hooks.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdtempSync, rmSync, readFileSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { LeosAgent } from '../../index.js';
import piExtension from '../../pi-extension.js';

const root = resolve(import.meta.dirname, '../..');
const sandbox = mkdtempSync(join(tmpdir(), 'leo enforcement '));
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
const digest = (text) => createHash('sha256').update(text).digest('hex').slice(0, 12);

const opencode = async () => {
  const hooks = await LeosAgent({ directory: sandbox, client: { app: { agents: async () => ({ data: [
    { name: 'leo-standard', model: { providerID: 'openai', id: 'gpt-5.6-terra' } },
    { name: 'leo-parent' },
  ] }) } } });
  await hooks.event({ event: { type: 'message.updated', properties: { info: {
    sessionID: 's', model: { providerID: 'openai', id: 'gpt-5.6-sol' },
  } } } });
  return hooks;
};

// session/tools.ts: trigger('tool.execute.before', input, { args }) then
// item.execute(args, ctx). The slash-command subtask path in session/prompt.ts
// does the same with its own taskArgs. Only `args` itself reaches the tool.
const hostRunsTask = async (hooks, args) => {
  await hooks['tool.execute.before']({ tool: 'task', sessionID: 's', callID: 'c' }, { args });
  return args;
};

test('OpenCode applies a correction to the object the host executes', async () => {
  const hooks = await opencode();
  const args = { subagent_type: 'general', prompt: 'Investigate', description: 'd', command: 'review' };
  const executed = await hostRunsTask(hooks, args);
  assert.equal(executed, args);
  assert.deepEqual(executed, { subagent_type: 'leo-parent', prompt: 'Investigate', description: 'd', command: 'review' });
  // The dispatch row names the agent that runs, not the one requested.
  const row = rows().at(-1);
  assert.equal(row.decision, 'correct');
  assert.equal(row.agent, 'leo-parent');
  assert.equal(row.tier, 'parent');
});

test('OpenCode blocks a correction it cannot apply', async () => {
  const hooks = await opencode();
  const args = Object.freeze({ subagent_type: 'general', prompt: 'Investigate', description: 'd' });
  await assert.rejects(hostRunsTask(hooks, args), /correction-not-applied.*subagent_type="leo-parent"/);
  assert.equal(args.subagent_type, 'general');
});

test('OpenCode leaves a compliant task untouched', async () => {
  const hooks = await opencode();
  const args = { subagent_type: 'explore', prompt: 'Look around', description: 'd' };
  const before = { ...args };
  await hostRunsTask(hooks, args);
  assert.deepEqual(args, before);
});

test('Pi dispatch and completion rows carry the session and still join by call id', async () => {
  const hooks = {};
  piExtension({ on: (name, callback) => { hooks[name] = callback; } });
  const ctx = (id) => ({ cwd: sandbox, model: { id: 'claude-opus-5' }, sessionManager: { getSessionId: () => id } });
  for (const [id, call] of [['pi-a', 'a1'], ['pi-b', 'b1']]) {
    const blocked = await hooks.tool_call({ toolName: 'subagent', toolCallId: call, input: { agent: 'scout', task: 'Look' } }, ctx(id));
    assert.equal(blocked, undefined);
  }
  const dispatches = rows().filter((r) => r.harness === 'pi' && r.decision !== 'completed');
  assert.deepEqual(dispatches.slice(-2).map((r) => r.session), [digest('pi-a'), digest('pi-b')]);
  assert.notEqual(dispatches.at(-2).burst, dispatches.at(-1).burst);
  await hooks.tool_result({ toolName: 'subagent', toolCallId: 'a1', input: { agent: 'scout' },
    content: [{ type: 'text', text: 'Result: done' }] }, ctx('pi-a'));
  const completion = rows().at(-1);
  assert.equal(completion.session, digest('pi-a'));
  assert.equal(completion.call_id, 'a1');
  // A context without a session manager still records, just without a session.
  await hooks.tool_call({ toolName: 'subagent', toolCallId: 'x', input: { agent: 'scout', task: 'Look' } },
    { sessionManager: { getSessionId: () => { throw new Error('closed'); } } });
  assert.equal(rows().at(-1).call_id, 'x');
});
