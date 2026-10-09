/**
 * Claude Code mod: applies the shared routing decision at agent.spawn.
 *
 * Thin adapter. scripts/dispatch_guard.py --json makes the decision
 * (routing_engine.route()) and writes the one dispatch row; this file maps
 * the event, applies the answer, and fails open. No network, no model call,
 * and it never grants tool permission: it only sets the child's model or
 * refuses the spawn.
 *
 * Precedence with the PreToolUse command guard: session.start sets
 * LEOS_AGENT_CLAUDE_SPAWN_MOD to this plugin's root in the Claude Code process.
 * Claude Code starts this plugin's command hooks with CLAUDE_PLUGIN_ROOT set to
 * that same root, and the command guard passes Claude dispatches through
 * without correcting or logging them when the two agree. A Bash-tool process
 * inherits the variable but not CLAUDE_PLUGIN_ROOT, so tests and manual runs
 * inside a session still get decisions. This hook decides only while the
 * variable holds its root, so a failed session.start leaves the command guard
 * in charge and a dispatch is never decided twice. Builds before 2.1.289 raise
 * no agent.spawn for teammates; there the variable is cleared instead.
 */

const DEFAULT_RETRY = 'Retry with an explicit model within the parent price ceiling.';
const GUARD_TIMEOUT_MS = 10000;
const SPAWN_FLOOR = [2, 1, 289];

/** Whether this Claude Code build raises agent.spawn for every spawn the command guard sees. */
export function coversEverySpawn(version) {
  const parts = String(version ?? '').split(/[.-]/, 3).map(Number);
  if (parts.length < 3 || !parts.every(Number.isInteger)) return false;
  for (let i = 0; i < 3; i += 1) if (parts[i] !== SPAWN_FLOOR[i]) return parts[i] > SPAWN_FLOOR[i];
  return true;
}

/** The PreToolUse-shaped event dispatch_guard.py --json reads. */
export function guardEvent(e, session) {
  const toolInput = { subagent_type: e.subagentType, prompt: e.prompt, description: e.description };
  if (typeof e.model === 'string' && e.model) toolInput.model = e.model;
  return {
    hook_event_name: 'PreToolUse',
    tool_name: 'Agent',
    tool_input: toolInput,
    tool_use_id: e.tool_use_id,
    // The engine's own word for the caller's effective model: the ceiling for
    // nested dispatches too, with no transcript lookup.
    parent_model: e.parentModel,
    session_id: session.id,
    cwd: session.cwd,
  };
}

/** The Agent tool error the model reads; the same text dispatch_guard.render_block prints. */
export function blockText(result) {
  return `[leo routing] BLOCKED: ${result?.reason ?? 'model choice required'}. ${result?.retry ?? DEFAULT_RETRY}`;
}

/** One guard decision as the engine takes it: `{ deny }`, `{ model }`, or `{}` to leave the spawn alone. */
export function answerFor(e, result) {
  if (result?.action === 'block') return { deny: blockText(result) };
  const model = result?.action === 'correct' ? result.updated_input?.model : undefined;
  if (typeof model === 'string' && model && model !== e.model) return { model };
  return {};
}

/** Spawns the routing policy covers: the model's own Agent calls and teammates. */
export function routable(e, origin) {
  // A fork inherits the parent and ignores `model`; a workflow agent's content
  // cannot be rewritten; another plugin's own $.agent.spawn was never routed.
  return !e.fork && !e.workflow && origin?.plugin === 'engine';
}

async function decide($, e) {
  const [id, cwd] = await Promise.all([$.session.id(), $.session.cwd()]);
  const run = await $.process.run(['python3', `${$.plugin.root}/scripts/dispatch_guard.py`, '--json'], {
    env: { LEOS_AGENT_HARNESS: 'claude' },
    stdin: JSON.stringify(guardEvent(e, { id, cwd })),
    timeoutMs: GUARD_TIMEOUT_MS,
  });
  if (run.exitCode !== 0) throw new Error(`dispatch guard exited ${run.exitCode}`);
  return JSON.parse(run.stdout);
}

/** @type {import('claude-code').Register} */
export const register = (on) => {
  on('session.start', async ($, e, next) => {
    let live = false;
    try {
      const { version, base } = await $.session.version();
      live = coversEverySpawn(base ?? version);
    } catch {
      // An unknown build keeps the PreToolUse command guard in charge.
    }
    try {
      // Cleared rather than left alone, so a value inherited from a parent
      // Claude Code process cannot silence this process's command guard.
      await $.env.set('LEOS_AGENT_CLAUDE_SPAWN_MOD', live ? $.plugin.root : undefined);
    } catch {
      // The spawn hook below checks the variable before deciding anything.
    }
    return next(e);
  });

  on('agent.spawn', async ($, e, next) => {
    if (!routable(e, next.origin)) return next(e);
    if ((await $.env.get('LEOS_AGENT_CLAUDE_SPAWN_MOD')) !== $.plugin.root) return next(e);
    let answer = {};
    try {
      answer = answerFor(e, await decide($, e));
    } catch (error) {
      $.ui.log(`routing bridge failed; spawn allowed without a price check: ${error?.message ?? error}`, { to: 'debug' });
    }
    if (answer.deny) return { deny: answer.deny };
    return next(answer.model ? { ...e, model: answer.model } : e);
  });
};
