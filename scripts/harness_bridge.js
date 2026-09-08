/** Bounded subprocess bridge; no shell interpolation or model requests. */
import { spawn } from 'node:child_process';
import { join } from 'node:path';

export function runPython(root, harness, script, args = [], event = {}, timeout = 10000) {
  return new Promise((resolve) => {
    let child;
    let timer;
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(value);
    };
    try {
      child = spawn('python3', [join(root, 'scripts', script), ...args], {
        stdio: ['pipe', 'pipe', 'pipe'],
        env: { ...process.env, LEOS_AGENT_HARNESS: harness },
      });
    } catch { return finish(null); }
    let stdout = '';
    let stderr = '';
    timer = setTimeout(() => { child.kill('SIGKILL'); finish(null); }, timeout);
    child.stdout.on('data', (chunk) => {
      stdout += chunk;
      if (stdout.length > 1024 * 1024) { child.kill('SIGKILL'); finish(null); }
    });
    child.stderr.on('data', (chunk) => { stderr = (stderr + chunk).slice(-4096); });
    for (const stream of [child.stdin, child.stdout, child.stderr]) stream.on('error', () => {});
    child.on('error', () => finish(null));
    child.on('close', (code) => finish({ code, stdout, stderr }));
    try { child.stdin.end(JSON.stringify(event)); }
    catch { child.kill('SIGKILL'); finish(null); }
  });
}

export async function guard(root, harness, event) {
  const result = await runPython(root, harness, 'dispatch_guard.py', ['--json'], event);
  const failed = () => {
    console.error(`[leos-agent] ${harness} routing bridge failed; dispatch allowed without a price check`);
    return { action: 'allow', reason: 'bridge-error' };
  };
  if (!result || result.code !== 0) return failed();
  try { return JSON.parse(result.stdout); }
  catch { return failed(); }
}

export async function bounded(promise, timeout = 2000) {
  let timer;
  try {
    return await Promise.race([promise, new Promise((resolve) => { timer = setTimeout(() => resolve(null), timeout); })]);
  } catch { return null; }
  finally { clearTimeout(timer); }
}
