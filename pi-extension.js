/**
 * Pi extension for leos-agent.
 *
 * Pi has no session-start hook that freezes into a single rendered prefix the
 * way Hermes's register_system_prompt_section does (see __init__.py's
 * _payload_section) -- before_agent_start is the closest surface, and per Pi's
 * own docs it fires once per USER TURN, not once per session. Spawning python3
 * on every turn to re-read a file that cannot have changed would be absurd, so
 * the render is memoised for the life of the process: the payload is
 * deterministic by contract (see scripts/emit_payload.py), which is exactly
 * what makes caching it safe. Reusing that script rather than standing up a
 * second renderer for Pi is the thing scripts/payload.py exists to prevent.
 *
 * Skills ship from this package's skills/ directory via resources_discover
 * below, not by an installer copying them to disk -- the same "read live,
 * don't render to disk" move the payload itself made.
 */
import { spawn } from 'node:child_process';
import { join } from 'node:path';

const PAYLOAD_TIMEOUT_MS = 10_000;

/**
 * Run scripts/emit_payload.py and resolve to its stdout, or '' on any failure.
 * A spawn error, a timeout, a non-zero exit, or empty output must all resolve
 * to '' rather than reject: the caller appends this to the existing system
 * prompt, and a broken render must cost the user only the section this
 * extension would have added, never the rest of the prompt.
 */
function runEmitPayload(root) {
  return new Promise((resolve) => {
    let child;
    try {
      child = spawn('python3', [join(root, 'scripts', 'emit_payload.py')], {
        stdio: ['ignore', 'pipe', 'ignore'],
        env: { ...process.env, LEOS_AGENT_HARNESS: 'pi' },
      });
    } catch {
      return resolve('');
    }

    let stdout = '';
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(value);
    };
    const timer = setTimeout(() => {
      try { child.kill('SIGKILL'); } catch { /* already gone */ }
      finish('');
    }, PAYLOAD_TIMEOUT_MS);

    child.stdout.on('data', (chunk) => { stdout += chunk; });
    // EPIPE here means the child exited early -- an empty result, not an
    // unhandled error event that would take the turn down with it.
    child.stdout.on('error', () => {});
    child.on('error', () => finish(''));
    child.on('close', (code) => finish(code === 0 ? stdout : ''));
  });
}

export default function (pi) {
  const root = process.env.LEOS_AGENT_ROOT || process.env.PLUGIN_ROOT || new URL('.', import.meta.url).pathname;

  // Rendered at most once per process. A failed render is NOT cached: it is
  // usually something transient (a python3 that was not on PATH yet), and
  // retrying next turn costs one spawn where caching the failure would cost
  // the session its policy for good.
  let cached = null;
  const payloadOnce = async () => {
    if (cached === null) {
      const text = (await runEmitPayload(root)).trim();
      if (!text) return '';
      cached = text;
    }
    return cached;
  };

  pi.on('before_agent_start', async (event) => {
    const payload = await payloadOnce();
    if (!payload) return;
    return { systemPrompt: event.systemPrompt + '\n\n' + payload };
  });

  pi.on('resources_discover', async () => {
    return { skillPaths: [join(root, 'skills')] };
  });
}
