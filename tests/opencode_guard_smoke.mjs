#!/usr/bin/env node
// Smoke test (not a unit test): imports the OpenCode leo plugin and drives
// its tool.execute.before hook directly against the bash-guard catastrophic-
// deletion tripwire.
//
// Run: node tests/opencode_guard_smoke.mjs

import { fileURLToPath, pathToFileURL } from "node:url";
import path from "node:path";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.dirname(HERE);
const LEO_JS = path.join(REPO, ".opencode", "plugin", "leo.js");

let failed = false;

function pass(label) {
  console.log(`PASS: ${label}`);
}

function fail(label, detail) {
  failed = true;
  console.log(`FAIL: ${label}${detail ? " — " + detail : ""}`);
}

async function resolveHooks(mod) {
  // The plugin may be exported as a ready-made hooks object, or as a
  // (possibly async) factory function that returns one when called with
  // a minimal OpenCode plugin context.
  const candidate = mod.default ?? mod.LeoPlugin ?? mod.leo ?? Object.values(mod)[0];
  if (candidate === undefined) {
    throw new Error("no usable export found in leo.js");
  }
  if (typeof candidate === "function") {
    return await candidate({});
  }
  return candidate;
}

async function main() {
  let mod;
  try {
    mod = await import(pathToFileURL(LEO_JS).href);
  } catch (err) {
    fail("import leo.js", err.message);
    console.log(failed ? "SMOKE RESULT: FAIL" : "SMOKE RESULT: PASS");
    process.exit(1);
  }

  let hooks;
  try {
    hooks = await resolveHooks(mod);
  } catch (err) {
    fail("resolve plugin hooks", err.message);
    console.log("SMOKE RESULT: FAIL");
    process.exit(1);
  }

  const before = hooks && hooks["tool.execute.before"];
  if (typeof before !== "function") {
    fail("tool.execute.before is a function", `got ${typeof before}`);
    console.log("SMOKE RESULT: FAIL");
    process.exit(1);
  }

  // Catastrophic command: expected to throw (block execution).
  try {
    await before({ tool: "bash" }, { args: { command: "rm -rf ~" } });
    fail("rm -rf ~ is blocked", "hook did not throw");
  } catch (err) {
    pass("rm -rf ~ is blocked");
  }

  // Routine command: expected NOT to throw.
  try {
    await before({ tool: "bash" }, { args: { command: "echo ok" } });
    pass("echo ok is allowed");
  } catch (err) {
    fail("echo ok is allowed", `hook threw: ${err.message}`);
  }

  console.log(failed ? "SMOKE RESULT: FAIL" : "SMOKE RESULT: PASS");
  process.exit(failed ? 1 : 0);
}

main();
