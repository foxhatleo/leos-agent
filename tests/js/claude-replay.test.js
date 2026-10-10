// Replays hook and agent.spawn payloads captured from live Claude Code 2.1.296
// sessions (tests/fixtures/claude-2.1.296/<scenario>/) through this plugin's own
// code: agent.spawn through the mod's registered handler, every other event
// through the exact commands hooks/hooks.json runs for it. The live dispatch
// rows each scenario produced are in its meta.json.
//
// Captured with `claude -p --permission-mode auto` and a recording plugin
// beside the released plugin. Transcripts keep only the fields the hooks read;
// paths are placeholders. The hooks ran while Claude Code was still writing
// those transcripts, so a replay against the finished files can see a child
// model the live hook could not yet; assertions below hold either way.
import test from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { cpSync, existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { register } from '../../hooks/claude-spawn.js';

const root = resolve(import.meta.dirname, '../..');
const fixtures = join(root, 'tests/fixtures/claude-2.1.296');
const sandbox = mkdtempSync(join(tmpdir(), 'leo claude replay '));
test.after(() => rmSync(sandbox, { recursive: true, force: true }));

const baseEnv = { ...process.env };
for (const name of ['CLAUDE_CODE_SUBAGENT_MODEL', 'CLAUDE_CODE_SUBAGENT_MODEL_FORCE', 'LEOS_AGENT_DISPATCH_GUARD',
  'LEOS_AGENT_CLAUDE_SPAWN_MOD', 'LEOS_AGENT_HARNESS', 'CLAUDE_PLUGIN_ROOT']) delete baseEnv[name];
Object.assign(baseEnv, {
  HOME: join(sandbox, 'home'), CLAUDE_CONFIG_DIR: join(sandbox, 'home/.claude'), CODEX_HOME: join(sandbox, 'home/.codex'),
  HERMES_HOME: join(sandbox, 'home/.hermes'), PI_CODING_AGENT_DIR: join(sandbox, 'home/.pi'),
  OPENCODE_CONFIG_DIR: join(sandbox, 'home/.opencode'), OPENCODE_CONFIG: join(sandbox, 'home/.opencode/opencode.json'),
  XDG_CONFIG_HOME: join(sandbox, 'home/.config'), LEOS_AGENT_PRICE_REFRESH: 'off', PYTHONDONTWRITEBYTECODE: '1',
});

const COMMAND_EVENTS = JSON.parse(readFileSync(join(root, 'hooks/hooks.json'), 'utf8')).hooks;
const matches = (entry, name) => entry.matcher === undefined || new RegExp(`^(?:${entry.matcher})$`).test(name);
const commandsFor = (input) => (COMMAND_EVENTS[input.hook_event_name] ?? [])
  .filter((entry) => entry.matcher === undefined || matches(entry, input.tool_name ?? input.source ?? ''))
  .flatMap((entry) => entry.hooks.map((hook) => hook.command));

let counter = 0;

/** One captured session replayed in a fresh data directory. */
function session(name, { env = {}, clearSpawnMod = false, transcriptCut = null } = {}) {
  const dir = join(fixtures, name);
  const work = join(sandbox, `${name}-${++counter}`);
  const transcripts = join(work, 'transcripts');
  const cwd = join(work, 'cwd');
  mkdirSync(cwd, { recursive: true });
  cpSync(join(dir, 'transcripts'), transcripts, { recursive: true });
  if (transcriptCut) transcriptCut(transcripts);
  const storage = join(work, 'local');
  const processEnv = { ...baseEnv, LEOS_AGENT_LOCAL_PATH: storage, ...env };
  const fill = (value) => JSON.parse(JSON.stringify(value)
    .replaceAll('{transcripts}', transcripts).replaceAll('{cwd}', cwd));
  const events = readFileSync(join(dir, 'events.jsonl'), 'utf8').trim().split('\n').map((line) => fill(JSON.parse(line)));

  const hooks = {};
  register((event, hook) => { (hooks[event] ??= []).push(hook); return { catch() {} }; }, {});
  const $ = {
    plugin: { name: 'leos-agent', root },
    session: {
      id: async () => events.find((e) => e.input?.session_id)?.input.session_id,
      cwd: async () => cwd,
      version: async () => ({ version: '2.1.296', base: '2.1.296' }),
    },
    env: {
      get: async (key) => processEnv[key],
      set: async (key, value) => { if (value === undefined) delete processEnv[key]; else processEnv[key] = value; },
    },
    process: {
      run: async (argv, init = {}) => {
        const child = spawnSync(argv[0], argv.slice(1), { input: init.stdin, cwd: init.cwd ?? cwd,
          env: { ...processEnv, ...init.env }, timeout: init.timeoutMs ?? 30000, encoding: 'utf8' });
        if (child.error) throw child.error;
        return { exitCode: child.status ?? 1, stdout: child.stdout, stderr: child.stderr };
      },
    },
    ui: { log: () => {} },
  };
  const chain = async (event, e, origin, bottom) => {
    const list = hooks[event] ?? [];
    const step = (index) => async (input) => {
      if (index === list.length) return bottom(input);
      const next = step(index + 1);
      next.origin = origin;
      return list[index]($, input, next);
    };
    return step(0)(e);
  };

  const replies = [];
  return {
    events,
    rows: () => {
      const path = join(storage, 'dispatch.jsonl');
      return existsSync(path) ? readFileSync(path, 'utf8').trim().split('\n').map((line) => JSON.parse(line)) : [];
    },
    report: () => {
      const run = spawnSync('python3', [join(root, 'scripts/dispatch_log.py'), 'report', '--json'],
        { env: processEnv, encoding: 'utf8', timeout: 30000 });
      assert.equal(run.status, 0, run.stderr);
      return JSON.parse(run.stdout);
    },
    /** Every captured event in order; returns one reply per event. */
    replay: async () => {
      for (const event of events) {
        if (event.kind === 'agent.spawn') {
          const settled = await chain('agent.spawn', event.event, event.origin, async (input) => ({ model: input.model }));
          replies.push({ kind: 'agent.spawn', event: event.event, settled });
          continue;
        }
        if (event.kind === 'SessionStart') {
          await chain('session.start', { cwd, surface: null, isInteractive: false }, { plugin: 'engine' }, async () => {});
          // The recording plugin cleared the variable here in the fallback capture.
          if (clearSpawnMod) delete processEnv.LEOS_AGENT_CLAUDE_SPAWN_MOD;
        }
        const outputs = commandsFor(event.input).map((command) => {
          const hook = spawnSync('sh', ['-c', command], { cwd, encoding: 'utf8', timeout: 30000,
            env: { ...processEnv, CLAUDE_PLUGIN_ROOT: root }, input: JSON.stringify(event.input) });
          assert.ok(hook.status === 0 || hook.status === 2, `${command}: exit ${hook.status} ${hook.stderr}`);
          return { status: hook.status, stdout: hook.stdout.trim(), stderr: hook.stderr.trim() };
        });
        replies.push({ kind: event.kind, input: event.input, outputs });
      }
      return replies;
    },
  };
}

const spawnReplies = (replies) => replies.filter((reply) => reply.kind === 'agent.spawn');
const handbackReplies = (replies) => replies.filter((reply) => reply.input?.tool_name === 'SubagentHandback');
const agentPreToolUse = (replies) => replies.filter((reply) => reply.kind === 'PreToolUse' && reply.input.tool_name === 'Agent');
const decision = (output) => (output.stdout ? JSON.parse(output.stdout).hookSpecificOutput : undefined);
const denied = (output) => decision(output)?.permissionDecision === 'deny';
const dispatchRows = (rows) => rows.filter((row) => ['allow', 'correct', 'block'].includes(row.decision));
const completions = (rows) => rows.filter((row) => ['executed', 'completed'].includes(row.decision));

test('fixtures name the build they were captured from and hold no machine paths', () => {
  for (const name of readdirSync(fixtures)) {
    const meta = JSON.parse(readFileSync(join(fixtures, name, 'meta.json'), 'utf8'));
    assert.equal(meta.claude_code, '2.1.296', name);
    const text = spawnSync('grep', ['-rlE', '/(tmp|home|root|Users)/', join(fixtures, name)], { encoding: 'utf8' }).stdout;
    assert.equal(text, '', `${name} holds a machine path`);
  }
});

test('with the mod live, the command guard passes Agent calls through and agent.spawn decides once', async () => {
  for (const name of ['general-purpose', 'lens-tiers', 'ceiling-mod']) {
    const run = session(name);
    const replies = await run.replay();
    for (const reply of agentPreToolUse(replies)) {
      assert.deepEqual(reply.outputs.map((o) => [o.status, o.stdout]), [[0, '']], name);
    }
    const live = JSON.parse(readFileSync(join(fixtures, name, 'meta.json'), 'utf8')).live_rows;
    const pick = (row) => [row.decision, row.agent, row.tier ?? null, row.reason, row.effective_model];
    assert.deepEqual(dispatchRows(run.rows()).map(pick), dispatchRows(live).map(pick), name);
  }
});

test('a haiku parent asking for opus is capped at agent.spawn and the child runs on haiku', async () => {
  const run = session('ceiling-mod');
  const [spawn] = spawnReplies(await run.replay());
  assert.equal(spawn.event.model, 'opus');
  assert.equal(spawn.event.parentModel, 'claude-haiku-5-5');
  // Capped to the parent's alias; the live child transcript showed claude-haiku-5-5.
  assert.equal(spawn.settled.model, 'haiku');
  assert.deepEqual(dispatchRows(run.rows()).map((r) => [r.decision, r.reason]), [['correct', 'over-ceiling']]);
});

test('without the mod, the command guard caps the same call once the parent model is on disk', async () => {
  const run = session('ceiling-fallback', { clearSpawnMod: true });
  const replies = await run.replay();
  const [pre] = agentPreToolUse(replies);
  const corrected = decision(pre.outputs[0]);
  assert.equal(corrected.updatedInput.model, 'haiku');
  // The mod saw its variable cleared and left the spawn alone.
  assert.equal(spawnReplies(replies)[0].settled.model, 'opus');
  assert.deepEqual(dispatchRows(run.rows()).map((r) => [r.decision, r.reason]), [['correct', 'over-ceiling']]);
});

test('without the mod, a first dispatch made before the parent turn is on disk is allowed with a diagnostic', async () => {
  // As captured: Claude Code 2.1.296 had not flushed the dispatching turn when
  // PreToolUse ran, and its SessionStart input names no model.
  const cut = (transcripts) => {
    for (const file of readdirSync(transcripts).filter((f) => f.endsWith('.jsonl'))) {
      const path = join(transcripts, file);
      const lines = readFileSync(path, 'utf8').trim().split('\n');
      const first = lines.findIndex((line) => JSON.parse(line).type === 'assistant');
      writeFileSync(path, `${lines.slice(0, first).join('\n')}\n`);
    }
  };
  const run = session('ceiling-fallback', { clearSpawnMod: true, transcriptCut: cut });
  const [pre] = agentPreToolUse(await run.replay());
  assert.deepEqual(pre.outputs.map((o) => o.stdout), ['']);
  const live = JSON.parse(readFileSync(join(fixtures, 'ceiling-fallback', 'meta.json'), 'utf8')).live_rows;
  const pick = (r) => [r.decision, r.reason];
  assert.deepEqual(dispatchRows(run.rows()).map(pick), dispatchRows(live).map(pick));
  assert.deepEqual(pick(dispatchRows(live)[0]), ['allow', 'parent-model-unavailable']);
});

test('a bare hand-back is refused once, the closed one is delivered, and the report counts the refusal', async () => {
  const run = session('handback-refused');
  const replies = handbackReplies(await run.replay());
  assert.deepEqual(replies.map((r) => r.input.tool_input.message.includes('Result:')), [false, true]);
  assert.deepEqual(replies.map((r) => r.outputs.map(denied)), [[true], [false]]);
  const [done] = completions(run.rows());
  assert.equal(done.contract_refused, true);
  assert.equal(done.outcome, 'done');
  assert.deepEqual(run.report().contract_refused, { cheap: 1 });
});

test('a general-purpose completion has no contract_refused key and its two stops count once', async () => {
  const run = session('general-purpose');
  const replies = await run.replay();
  const stops = replies.filter((r) => r.kind === 'SubagentStop' && r.input.agent_type === 'general-purpose');
  assert.deepEqual(stops.map((r) => r.input.stop_hook_active), [false, true]);
  for (const reply of handbackReplies(replies)) assert.deepEqual(reply.outputs.map(denied), [false]);
  const rows = completions(run.rows());
  for (const row of rows.filter((r) => r.agent === 'general-purpose')) assert.ok(!('contract_refused' in row));
  for (const row of rows.filter((r) => r.agent === 'leos-agent:leo-cheap')) assert.equal(row.contract_refused, false);
  const report = run.report();
  assert.equal(report.dispatch_attempts, 2);
  assert.deepEqual(report.outcomes.cheap, { done: 1 });
  assert.deepEqual(report.contract_refused ?? {}, {});
});

test('a haiku lens runs and is logged at the cheap tier; a lens with no model gets the standard fill', async () => {
  const run = session('lens-tiers');
  const spawns = spawnReplies(await run.replay());
  assert.deepEqual(spawns.map((s) => [s.event.model ?? null, s.settled.model ?? null]), [['haiku', 'haiku'], [null, 'sonnet']]);
  const tiers = (rows) => rows.filter((r) => r.agent === 'leos-agent:leo-lens').map((r) => [r.decision, r.tier]);
  assert.deepEqual(tiers(dispatchRows(run.rows())), [['allow', 'cheap'], ['correct', 'standard']]);
  assert.deepEqual(new Set(tiers(completions(run.rows())).map(([, tier]) => tier)), new Set(['cheap', 'standard']));
  const report = run.report();
  assert.deepEqual(report.outcomes, { cheap: { done: 1 }, standard: { done: 1 } });
});

test('CLAUDE_CODE_SUBAGENT_MODEL is honoured for general-purpose while Explore gets the standard fill', async () => {
  const run = session('subagent-model-setting', { env: { CLAUDE_CODE_SUBAGENT_MODEL: 'haiku' } });
  const spawns = spawnReplies(await run.replay());
  assert.deepEqual(spawns.map((s) => [s.event.subagentType, s.settled.model ?? null]),
    [['general-purpose', null], ['Explore', 'sonnet']]);
  assert.deepEqual(dispatchRows(run.rows()).map((r) => [r.agent, r.decision, r.reason, r.effective_model]),
    [['general-purpose', 'allow', 'subagent-model-setting', 'haiku'], ['Explore', 'correct', 'explicit-tier-default', 'sonnet']]);
});

test('a nested lens is held to its haiku caller, not the sonnet root, on both decision paths', async () => {
  // Captured with the guard off: the lens inherited the reviewer's haiku.
  const off = session('nested-lens', { env: { LEOS_AGENT_DISPATCH_GUARD: 'off' } });
  const offSpawns = spawnReplies(await off.replay());
  assert.deepEqual(offSpawns.map((s) => [s.event.subagentType, s.event.parentModel]),
    [['leos-agent:leo-reviewer', 'claude-sonnet-5-5'], ['leos-agent:leo-lens', 'claude-haiku-5-5']]);
  assert.deepEqual(dispatchRows(off.rows()), []);

  // Guard on, mod live: the standard fill is capped to the caller.
  const on = session('nested-lens');
  const lens = spawnReplies(await on.replay())[1];
  assert.equal(lens.settled.model, 'haiku');
  // Guard on, mod absent: the PreToolUse inside the reviewer carries its agent_id,
  // and the guard reads the caller's model from the reviewer's own transcript.
  const fallback = session('nested-lens', { clearSpawnMod: true });
  const inner = agentPreToolUse(await fallback.replay()).find((r) => r.input.agent_id);
  assert.equal(inner.input.agent_type, 'leos-agent:leo-reviewer');
  assert.equal(decision(inner.outputs[0]).updatedInput.model, 'haiku');
});
