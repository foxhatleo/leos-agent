/**
 * OpenCode plugin entry point for leos-agent.
 *
 * One hook: refuse a subagent dispatch that names no model, so a fan-out cannot
 * silently inherit the parent's expensive model. The decision itself lives in
 * scripts/dispatch_guard.py -- five harnesses share exactly one policy, and only
 * the event shape differs per harness.
 *
 * OpenCode has no per-tool matcher, so the prefilter is here in JS: without it
 * every Read, Grep and Bash would pay a python3 spawn, which would be a worse
 * regression than the one being fixed. The keys below are duplicated from
 * dispatch_guard.py and pinned equal by tests/test_dispatch_guard.py.
 *
 * Everything else in this plugin is installed to disk on demand by
 * scripts/leo-install.py, which the installed `install` skill runs.
 */
import { spawn } from 'node:child_process';
import { appendFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';

const AGENT_KEYS = ['subagent_type', 'subagentType', 'agent_type', 'agentType', 'agent', 'subagent', 'profile'];
const PROMPT_KEYS = ['prompt', 'brief', 'instructions', 'task', 'message', 'input'];
const GUARD_TIMEOUT_MS = 10_000;

const dataRoot = () => process.env.LEOS_AGENT_LOCAL_PATH || join(homedir(), '.leos-agent-local');

/** A breadcrumb that cannot be written must not itself break the session. */
function breadcrumb(message) {
  try {
    appendFileSync(join(dataRoot(), 'dispatch-guard.log'), `${new Date().toISOString()} ${message}\n`, { mode: 0o600 });
  } catch {
    /* nothing left to do; the guard has already failed open */
  }
}

const hasKey = (args, keys) => keys.some((k) => typeof args[k] === 'string' && args[k].trim());

/**
 * Run the shared guard. Resolves to a block reason, or null to allow.
 * Every infrastructure failure -- missing python3, spawn error, timeout, a
 * wedged interpreter -- resolves null: the harm guarded against here is money,
 * and a guard that fails closed would wedge every dispatch in every session.
 */
function runGuard(root, event) {
  return new Promise((resolve) => {
    let child;
    try {
      child = spawn('python3', [join(root, 'scripts', 'dispatch_guard.py')], {
        stdio: ['pipe', 'ignore', 'pipe'],
        env: { ...process.env, LEOS_AGENT_HARNESS: 'opencode' },
      });
    } catch (err) {
      breadcrumb(`spawn failed: ${err && err.message}`);
      return resolve(null);
    }

    let stderr = '';
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(value);
    };
    const timer = setTimeout(() => {
      breadcrumb('guard timed out; allowing');
      try { child.kill('SIGKILL'); } catch { /* already gone */ }
      finish(null);
    }, GUARD_TIMEOUT_MS);

    child.stderr.on('data', (chunk) => { stderr += chunk; });
    // EPIPE on either pipe means the child exited early. That is an allow, not
    // an unhandled error event that would take the session down with it.
    child.stderr.on('error', () => {});
    child.stdin.on('error', () => {});
    child.on('error', (err) => {
      breadcrumb(`guard error: ${err && err.message}`);
      finish(null);
    });
    child.on('close', (code) => finish(code === 2 ? (stderr.trim() || '[leo routing] blocked') : null));

    try {
      child.stdin.end(JSON.stringify(event));
    } catch (err) {
      breadcrumb(`stdin write failed: ${err && err.message}`);
      finish(null);
    }
  });
}

export const LeosAgent = async (ctx) => {
  // The factory's ctx is the only place the session directory is available;
  // tool.execute.before receives just {tool, sessionID, callID}.
  const directory = (ctx && (ctx.directory || ctx.worktree)) || process.cwd();
  const root = process.env.LEOS_AGENT_ROOT || process.env.PLUGIN_ROOT || new URL('.', import.meta.url).pathname;

  return {
    'tool.execute.before': async (input, output) => {
      const args = output && output.args;
      if (!args || typeof args !== 'object') return;
      const tool = String((input && input.tool) || '');
      if (tool.startsWith('mcp__')) return;
      if (!hasKey(args, AGENT_KEYS) || !hasKey(args, PROMPT_KEYS)) return;

      const reason = await runGuard(root, {
        tool_name: tool,
        tool_input: args,
        session_id: (input && input.sessionID) || '',
        cwd: directory,
      });
      // OpenCode has no deny return value; throwing is how a hook refuses.
      if (reason) throw new Error(reason);
    },
  };
};

export default LeosAgent;
