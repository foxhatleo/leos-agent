import test from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { register } from '../../hooks/claude-spawn.js';

const root = resolve(import.meta.dirname, '../..');
const sandbox = mkdtempSync(join(tmpdir(), 'leo claude spawn '));
test.after(() => rmSync(sandbox, { recursive: true, force: true }));

// The Claude Code process environment the mod and its command hooks share.
// Every harness config root points into the sandbox; inherited routing
// overrides are dropped so they cannot leak into a decision.
const baseEnv = { ...process.env };
for (const name of ['CLAUDE_CODE_SUBAGENT_MODEL', 'CLAUDE_CODE_SUBAGENT_MODEL_FORCE', 'LEOS_AGENT_DISPATCH_GUARD',
  'LEOS_AGENT_CLAUDE_SPAWN_MOD', 'LEOS_AGENT_HARNESS', 'CLAUDE_PLUGIN_ROOT']) delete baseEnv[name];
Object.assign(baseEnv, {
  HOME: join(sandbox, 'home'), CLAUDE_CONFIG_DIR: join(sandbox, 'home/.claude'), CODEX_HOME: join(sandbox, 'home/.codex'),
  HERMES_HOME: join(sandbox, 'home/.hermes'), PI_CODING_AGENT_DIR: join(sandbox, 'home/.pi'),
  OPENCODE_CONFIG_DIR: join(sandbox, 'home/.opencode'), OPENCODE_CONFIG: join(sandbox, 'home/.opencode/opencode.json'),
  XDG_CONFIG_HOME: join(sandbox, 'home/.config'), LEOS_AGENT_PRICE_REFRESH: 'off', PYTHONDONTWRITEBYTECODE: '1',
});

const ALIASES = ['sonnet', 'opus', 'haiku', 'fable'];
const PINNED = ['tool_use_id', 'name', 'fork', 'isTeammate', 'workflow', 'parentModel', 'permissionMode',
  'parentAgentId', 'provider'];
const deepFreeze = (value) => {
  if (value && typeof value === 'object') Object.values(value).forEach(deepFreeze);
  return Object.freeze(value);
};
const preToolUseCommands = () => JSON.parse(readFileSync(join(root, 'hooks/hooks.json'), 'utf8')).hooks.PreToolUse
  .filter((entry) => new RegExp(`^(?:${entry.matcher})$`).test('Agent'))
  .flatMap((entry) => entry.hooks.map((hook) => hook.command));

// Each agent definition's `model` as Claude Code 2.1.296 has it: Explore and
// Plan are built in as `inherit`, general-purpose and claude name none, and the
// leo tiers carry theirs in frontmatter.
const DEFINED = { Explore: 'inherit', Plan: 'inherit' };
for (const file of readdirSync(join(root, 'agents')).filter((name) => name.endsWith('.md'))) {
  const model = /^model:\s*(\S+)/m.exec(readFileSync(join(root, 'agents', file), 'utf8'))?.[1];
  if (model) DEFINED[`leos-agent:${file.slice(0, -3)}`] = model;
}

/**
 * The model a spawn settles on, in Claude Code 2.1.296's order: the spawn's own
 * model, then the definition's, then CLAUDE_CODE_SUBAGENT_MODEL (trimmed; empty
 * or `inherit` is unset), then the parent. With CLAUDE_CODE_SUBAGENT_MODEL_FORCE
 * on, the setting, or else the parent, replaces the first two.
 */
function childModel(input, env) {
  const setting = env.CLAUDE_CODE_SUBAGENT_MODEL?.trim();
  const fallback = setting && setting !== 'inherit' ? setting : input.parentModel;
  if (['1', 'true', 'yes', 'on'].includes(env.CLAUDE_CODE_SUBAGENT_MODEL_FORCE?.trim().toLowerCase())) return fallback;
  const chosen = input.model ?? DEFINED[input.subagentType];
  return chosen === 'inherit' ? input.parentModel : chosen ?? fallback;
}

let counter = 0;

/**
 * Claude Code as observed on 2.1.296: session.start runs before the first
 * prompt; an Agent call runs the plugin's PreToolUse command hooks (whose
 * updatedInput must satisfy the Agent schema's model enum, and whose exit 2
 * refuses the call), then raises agent.spawn with the deeply frozen input
 * below, and the child runs on whatever model agent.spawn settled.
 */
function claudeCode({ env = {}, loadMod = true, root: pluginRoot = root, run, version = '2.1.296' } = {}) {
  const storage = join(sandbox, `data-${++counter}`);
  const work = join(sandbox, `work-${counter}`);
  mkdirSync(work, { recursive: true });
  const processEnv = { ...baseEnv, LEOS_AGENT_LOCAL_PATH: storage, ...env };
  const hooks = {};
  const debug = [];
  const runs = [];
  if (loadMod) {
    register((event, matcher, hook) => { (hooks[event] ??= []).push(hook ?? matcher); return { catch() {} }; }, {});
  }
  const $ = {
    plugin: { name: 'leos-agent', root: pluginRoot },
    session: {
      id: async () => 'session-1',
      cwd: async () => work,
      version: async () => {
        if (version === null) throw new Error('no such method on this build');
        return { version, base: version.replace(/-dev.*/, '-dev') };
      },
    },
    env: {
      get: async (name) => processEnv[name],
      set: async (name, value) => { if (value === undefined) delete processEnv[name]; else processEnv[name] = value; },
    },
    process: {
      run: run ?? (async (argv, init = {}) => {
        runs.push(argv);
        const child = spawnSync(argv[0], argv.slice(1), { input: init.stdin, cwd: init.cwd ?? work,
          env: { ...processEnv, ...init.env }, timeout: init.timeoutMs ?? 30000, encoding: 'utf8' });
        if (child.error) throw child.error;
        return { exitCode: child.status ?? 1, stdout: child.stdout, stderr: child.stderr,
          isStdoutTruncated: false, isStderrTruncated: false };
      }),
    },
    ui: { log: (text, options) => debug.push({ text, options }) },
  };
  const chain = async (event, e, bottom, origin) => {
    const list = hooks[event] ?? [];
    const step = (index) => async (input) => {
      if (index === list.length) return bottom(input);
      const next = step(index + 1);
      next.origin = origin;
      return list[index]($, deepFreeze(structuredClone(input)), next);
    };
    return step(0)(e);
  };
  const engine = { plugin: 'engine', tier: 'core' };
  // The bottom of agent.spawn: pinned fields must arrive as raised.
  const spawnWith = (raised, origin = engine) => chain('agent.spawn', raised, async (input) => {
    for (const key of PINNED) assert.deepEqual(input[key], raised[key], `pinned ${key} was rewritten`);
    return { model: childModel(input, processEnv), agentId: `a${counter}` };
  }, origin).then((result) => (result.deny ? { refused: result.deny, by: 'agent.spawn' } : { child: result.model }));

  return {
    processEnv, debug, runs,
    rows: () => {
      const path = join(storage, 'dispatch.jsonl');
      return existsSync(path) ? readFileSync(path, 'utf8').trim().split('\n').map((line) => JSON.parse(line)) : [];
    },
    start: () => chain('session.start', { cwd: work, surface: null, isInteractive: false }, async () => undefined, engine),
    /** One Agent call from the model. Resolves `{ child }` (the model it ran on) or `{ refused }`. */
    agentCall: async (toolInput, { parentModel = 'claude-sonnet-5', toolUseId = `toolu_${counter}` } = {}) => {
      const transcript = join(work, 'transcript.jsonl');
      writeFileSync(transcript, `${JSON.stringify({ type: 'assistant', message: { model: parentModel } })}\n`);
      let input = { description: 'probe', ...toolInput };
      for (const command of preToolUseCommands()) {
        const hook = spawnSync('sh', ['-c', command], { cwd: work, encoding: 'utf8', timeout: 10000,
          env: { ...processEnv, CLAUDE_PLUGIN_ROOT: root },
          input: JSON.stringify({ hook_event_name: 'PreToolUse', session_id: 'session-1', transcript_path: transcript,
            cwd: work, permission_mode: 'default', tool_name: 'Agent', tool_input: input, tool_use_id: toolUseId }) });
        if (hook.status === 2) return { refused: hook.stderr.trim(), by: 'PreToolUse' };
        const updated = hook.status === 0 && hook.stdout.trim()
          ? JSON.parse(hook.stdout).hookSpecificOutput?.updatedInput : undefined;
        if (updated) {
          if (updated.model !== undefined && !ALIASES.includes(updated.model)) {
            return { refused: 'PreToolUse hook for Agent returned updatedInput that failed schema validation', by: 'schema' };
          }
          input = updated;
        }
      }
      const spawn = { tool_use_id: toolUseId, prompt: input.prompt, description: input.description,
        subagentType: input.subagent_type ?? 'general-purpose', provider: { plugin: 'engine', tier: 'core' },
        parentModel, permissionMode: 'default', background: true, fork: false };
      if (input.model) spawn.model = input.model;
      return spawnWith(spawn);
    },
    /** agent.spawn alone, as raised for a teammate, a fork, a workflow agent or a plugin's $.agent.spawn. */
    spawn: spawnWith,
  };
}

test('the module is registered beside the classic hooks and ships with them', () => {
  const manifest = JSON.parse(readFileSync(join(root, 'hooks/hooks.json'), 'utf8'));
  assert.deepEqual(manifest.modules, ['./claude-spawn.js']);
  assert.ok(existsSync(join(root, 'hooks', manifest.modules[0])));
  assert.ok(manifest.hooks.PreToolUse.length > 0);
  const files = JSON.parse(readFileSync(join(root, 'package.json'), 'utf8')).files;
  assert.ok(files.includes('hooks/') && files.includes('scripts/'));
});

test('with the mod live, agent.spawn makes the one decision and the command guard stays out', async () => {
  const cc = claudeCode();
  await cc.start();
  assert.equal(cc.processEnv.LEOS_AGENT_CLAUDE_SPAWN_MOD, root);
  const outcome = await cc.agentCall({ subagent_type: 'leos-agent:leo-cheap', prompt: 'Rename one symbol in src/a.py' },
    { toolUseId: 'toolu_one' });
  assert.deepEqual(outcome, { child: 'haiku' });
  const rows = cc.rows();
  assert.equal(rows.length, 1);
  assert.equal(rows[0].decision, 'correct');
  assert.equal(rows[0].reason, 'explicit-tier-default');
  assert.equal(rows[0].harness, 'claude');
  assert.equal(rows[0].call_id, 'toolu_one');
  assert.equal(cc.runs.length, 1);
});

test('each dispatch ends on the same model with one row whichever side decides', async () => {
  const cases = [
    [{ subagent_type: 'leos-agent:leo-cheap', prompt: 'Rename a symbol' }, 'claude-sonnet-5'],
    [{ subagent_type: 'general-purpose', model: 'opus', prompt: 'Investigate the flaky test' }, 'claude-sonnet-5'],
    [{ subagent_type: 'general-purpose', model: 'haiku', prompt: 'List the files' }, 'claude-sonnet-5'],
    [{ subagent_type: 'leos-agent:leo-premium', prompt: 'Design the migration' }, 'claude-sonnet-5'],
    [{ subagent_type: 'general-purpose', prompt: 'Review the diff' }, 'claude-opus-5-5'],
  ];
  for (const [input, parentModel] of cases) {
    const modded = claudeCode();
    await modded.start();
    const viaSpawn = await modded.agentCall(input, { parentModel });
    for (const without of [claudeCode({ loadMod: false }), claudeCode()]) {
      // claudeCode() without start(): the module loaded but session.start never ran.
      const viaPreToolUse = await without.agentCall(input, { parentModel });
      assert.deepEqual(viaSpawn, viaPreToolUse, JSON.stringify(input));
      const [a, b] = [modded.rows(), without.rows()];
      assert.equal(a.length, 1);
      assert.equal(b.length, 1);
      for (const key of ['decision', 'reason', 'effective_model', 'requested_model', 'agent']) {
        assert.deepEqual(a[0][key], b[0][key], `${key} for ${JSON.stringify(input)}`);
      }
      assert.equal(without.runs.length, 0, 'agent.spawn must not decide while the variable is unset');
    }
  }
});

test('before agent.spawn covers teammates the command guard keeps deciding alone', async () => {
  const input = { subagent_type: 'general-purpose', model: 'opus', prompt: 'Investigate' };
  for (const version of ['2.1.288', '2.1.280-dev.20260920.t101500.sha1a2b3c4', null]) {
    // A value inherited from a parent Claude Code process is cleared, not trusted.
    const cc = claudeCode({ version, env: { LEOS_AGENT_CLAUDE_SPAWN_MOD: root } });
    await cc.start();
    assert.equal(cc.processEnv.LEOS_AGENT_CLAUDE_SPAWN_MOD, undefined, String(version));
    assert.deepEqual(await cc.agentCall(input), { child: 'sonnet' });
    assert.equal(cc.runs.length, 0);
    assert.equal(cc.rows().length, 1);
  }
  for (const version of ['2.1.289', '2.1.300', '2.2.0', '3.0.0']) {
    const cc = claudeCode({ version });
    await cc.start();
    assert.equal(cc.processEnv.LEOS_AGENT_CLAUDE_SPAWN_MOD, root, version);
  }
});

test('a process the session\'s Bash tool starts still gets the guard\'s own decision', async () => {
  // Gates and manual runs inside a session inherit the marker but not
  // CLAUDE_PLUGIN_ROOT, which Claude Code gives only to the plugin's own hooks.
  const cc = claudeCode();
  await cc.start();
  const run = spawnSync('python3', [join(root, 'scripts/dispatch_guard.py')], { encoding: 'utf8',
    env: { ...cc.processEnv, LEOS_AGENT_HARNESS: 'claude' },
    input: JSON.stringify({ hook_event_name: 'PreToolUse', tool_name: 'Agent', tool_use_id: 'toolu_bash',
      tool_input: { subagent_type: 'leos-agent:leo-cheap', prompt: 'x' } }) });
  assert.equal(run.status, 0);
  assert.ok(JSON.parse(run.stdout).hookSpecificOutput.updatedInput.model);
  assert.equal(cc.rows().length, 1);
});

test('a refusal reads exactly as the command guard words it', async () => {
  const env = { CLAUDE_CODE_SUBAGENT_MODEL_FORCE: '1', CLAUDE_CODE_SUBAGENT_MODEL: 'opus' };
  const cc = claudeCode({ env });
  await cc.start();
  const outcome = await cc.agentCall({ subagent_type: 'general-purpose', prompt: 'Refactor the parser' });
  assert.equal(outcome.by, 'agent.spawn');
  assert.deepEqual(cc.rows().map((row) => row.decision), ['block']);
  const legacy = await claudeCode({ env, loadMod: false }).agentCall({ subagent_type: 'general-purpose', prompt: 'Refactor the parser' });
  assert.equal(legacy.by, 'PreToolUse');
  assert.equal(outcome.refused, legacy.refused);
});

test('forks, workflow agents and other plugins\' spawns pass through without running the guard', async () => {
  const cc = claudeCode();
  await cc.start();
  const base = { tool_use_id: 'toolu_x', prompt: 'x', description: 'x', subagentType: 'general-purpose',
    provider: { plugin: 'engine', tier: 'core' }, parentModel: 'claude-sonnet-5', permissionMode: 'default',
    background: true, fork: false, model: 'opus' };
  assert.deepEqual(await cc.spawn({ ...base, subagentType: 'fork', fork: true }), { child: 'opus' });
  assert.deepEqual(await cc.spawn({ ...base, workflow: { runId: 'wf_1', agentIndex: 1 } }), { child: 'opus' });
  assert.deepEqual(await cc.spawn(base, { plugin: 'lab', tier: 'user' }), { child: 'opus' });
  assert.equal(cc.runs.length, 0);
  assert.equal(cc.rows().length, 0);
});

test('a teammate is routed like any other Agent call', async () => {
  const cc = claudeCode();
  await cc.start();
  const outcome = await cc.spawn({ tool_use_id: 'toolu_team', prompt: 'Own the docs', description: 'docs',
    subagentType: 'teammate', provider: { plugin: 'engine', tier: 'core' }, parentModel: 'claude-sonnet-5',
    permissionMode: 'default', background: true, fork: false, isTeammate: true, name: 'scout', model: 'opus' });
  assert.deepEqual(outcome, { child: 'sonnet' });
  assert.equal(cc.rows()[0].call_id, 'toolu_team');
});

test('an unavailable or broken guard fails open with a debug line and no row', async () => {
  const empty = join(sandbox, 'empty-plugin');
  mkdirSync(empty, { recursive: true });
  const spawn = { tool_use_id: 'toolu_f', prompt: 'x', description: 'x', subagentType: 'general-purpose',
    provider: { plugin: 'engine', tier: 'core' }, parentModel: 'claude-sonnet-5', permissionMode: 'default',
    background: true, fork: false, model: 'opus' };
  const failures = [
    claudeCode({ root: empty }),
    claudeCode({ run: async () => { throw new Error('spawn python3 ENOENT'); } }),
    claudeCode({ run: async () => ({ exitCode: 0, stdout: 'not json', stderr: '' }) }),
  ];
  for (const cc of failures) {
    await cc.start();
    assert.deepEqual(await cc.spawn(spawn), { child: 'opus' });
    assert.equal(cc.debug.length, 1);
    assert.equal(cc.debug[0].options?.to, 'debug');
    assert.equal(cc.rows().length, 0);
  }
});

test('an unforced CLAUDE_CODE_SUBAGENT_MODEL runs agents that name no model, capped at the parent', async () => {
  const cases = [
    // The setting, the model's Agent call, the parent, the model the child runs on, the row's reason.
    ['haiku', { subagent_type: 'general-purpose', prompt: 'List the files' }, 'claude-opus-5-5', 'haiku', 'subagent-model-setting'],
    ['haiku', { prompt: 'List the files' }, 'claude-opus-5-5', 'haiku', 'subagent-model-setting'],
    ['opus', { subagent_type: 'general-purpose', prompt: 'Review the diff' }, 'claude-sonnet-5', 'sonnet', 'over-ceiling'],
    ['opus', { subagent_type: 'claude', prompt: 'Review the diff' }, 'us.anthropic.claude-sonnet-4-5-20250929-v1:0',
      'sonnet', 'over-ceiling'],
    // A definition outranks the setting: Explore inherits, so it gets the standard tier.
    ['haiku', { subagent_type: 'Explore', prompt: 'Find the parser' }, 'claude-opus-5-5', 'sonnet', 'explicit-tier-default'],
    ['opus', { subagent_type: 'leos-agent:leo-cheap', prompt: 'Rename a symbol' }, 'claude-sonnet-5', 'haiku',
      'explicit-tier-default'],
  ];
  for (const [setting, input, parentModel, child, reason] of cases) {
    // A settings file's env is in the Claude Code process environment, which
    // both the command hooks and the mod's process.run inherit.
    const env = { CLAUDE_CODE_SUBAGENT_MODEL: setting };
    const modded = claudeCode({ env });
    await modded.start();
    for (const cc of [modded, claudeCode({ env, loadMod: false })]) {
      const label = `${setting}: ${JSON.stringify(input)} under ${parentModel}${cc === modded ? ' via agent.spawn' : ''}`;
      assert.deepEqual(await cc.agentCall(input, { parentModel }), { child }, label);
      assert.deepEqual(cc.rows().map((row) => row.reason), [reason], label);
    }
    assert.equal(modded.runs.length, 1);
  }
});

test('LEOS_AGENT_DISPATCH_GUARD=off turns the spawn path off too', async () => {
  const cc = claudeCode({ env: { LEOS_AGENT_DISPATCH_GUARD: 'off' } });
  await cc.start();
  assert.deepEqual(await cc.agentCall({ subagent_type: 'general-purpose', model: 'opus', prompt: 'x' }), { child: 'opus' });
  assert.equal(cc.rows().length, 0);
});
