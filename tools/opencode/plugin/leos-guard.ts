// OpenCode plugin: catastrophic-deletion guard.
//
// OpenCode has no exit-2 PreToolUse hook, so the shared bash-guard.py is invoked from a
// `tool.execute.before` hook that THROWS to block. The plugin file is symlinked into
// ~/.config/opencode/plugin/ from the leos-agent clone; Node resolves the symlink, so
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

function blocks(command: string, cwd?: string): string | null {
  const payload = JSON.stringify({
    tool_name: "Bash",
    tool_input: { command },
    cwd: cwd ?? process.cwd(),
  });
  const r = spawnSync("python3", [GUARD], { input: payload, encoding: "utf8", timeout: 10000 });
  // Exit 43 = block (see bash-guard.py). Any other code = allow (fail-open on errors).
  if (r.status === 43) return (r.stderr || "blocked by bash-guard").trim();
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
