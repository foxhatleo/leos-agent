// OpenCode plugin: catastrophic-deletion guard.
//
// OpenCode has no exit-2 PreToolUse hook, so the shared bash-guard.py is invoked from a
// `tool.execute.before` hook that THROWS to block. The plugin file is symlinked into
// ~/.config/opencode/plugins/ from the leos-agent clone; Node resolves the symlink, so
// import.meta.url points at the real file in the clone and locates core/hooks/bash-guard.py
// relatively — the guard stays single-source.
//
// Schema note: OpenCode's plugin API surface has shifted across versions. Verify against the
// installed opencode with the smoke test in tools/opencode/SETUP-DELTA.md before relying on it.

import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import * as path from "node:path";

const HERE = path.dirname(fileURLToPath(import.meta.url));
// tools/opencode/plugin -> clone root -> core/hooks/bash-guard.py
const GUARD = path.resolve(HERE, "..", "..", "..", "core", "hooks", "bash-guard.py");
const PYTHON = path.resolve(HERE, "..", "..", "..", "bin", "leos-python");

function blocks(command: string, cwd?: string): string | null {
  const payload = JSON.stringify({
    tool_name: "Bash",
    tool_input: { command },
    cwd: cwd ?? process.cwd(),
  });
  const r = spawnSync(PYTHON, [GUARD], { input: payload, encoding: "utf8", timeout: 10000 });
  // Exit 43 = policy block. A missing/broken private runtime must also block: allowing a
  // catastrophic command merely because the guard failed to start defeats the only hook surface.
  if (r.status === 43) return (r.stderr || "blocked by bash-guard").trim();
  if (r.status !== 0) return "Leo's Agents guard is unavailable; refusing shell execution";
  return null;
}

export const LeosGuard = async () => ({
  "tool.execute.before": async (input: { tool: string }, output: { args: any }) => {
    if (input.tool !== "bash") return;
    const command: string = output?.args?.command ?? "";
    if (!command) return;
    const reason = blocks(command, output?.args?.cwd);
    if (reason) throw new Error(reason);
  },
});
