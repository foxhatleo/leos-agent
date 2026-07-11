#!/usr/bin/env python3
"""Tests for bin/leos-seats.py + leos-doctor.check_seat_flags — seat-roster validation.

The conformance half builds candidates from the EXACT committed recipes (core/council/
seats.catalog.json and the docs/SETUP.md step-5 shapes) with {MODEL} resolved to dummy slugs and
{EFFORT} retained, and asserts `leos-seats.py validate` accepts them — the documented install flow
must never be refused by our own validator. The rejection half asserts the model rules are
mechanical, not prose: unresolved {MODEL}, a Fable/Mythos Anthropic seat, and an unpinned external
opus seat must all be refused. Run: bin/leos-python tests/seats-tests.py
"""

import copy
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
TEST_TMP = os.path.join(ROOT, "local", "test-work")
os.makedirs(TEST_TMP, exist_ok=True)
tempfile.tempdir = TEST_TMP
SEATS = os.path.join(ROOT, "bin", "leos-seats.py")
CATALOG = json.load(open(os.path.join(ROOT, "core", "council", "seats.catalog.json")))
ROSTER = {entry["role"]: entry for entry in CATALOG["roster"]}

passed = failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"FAIL: {name}")


def resolve(recipe, model, extra=None):
    """A catalog recipe with {MODEL} resolved (setup's job) and {EFFORT} retained (runtime's)."""
    seat = copy.deepcopy(recipe)
    seat["argv"] = [a.replace("{MODEL}", model) for a in seat["argv"]]
    seat.update(extra or {})
    return seat


def validate(candidate, host):
    path = os.path.join(tempfile.mkdtemp(prefix="seatcand."), "candidate.json")
    with open(path, "w") as f:
        json.dump(candidate, f)
    r = subprocess.run([sys.executable, SEATS, "validate", "--host", host, "--input", path],
                       capture_output=True, text=True)
    try:
        out = json.loads(r.stdout)
    except Exception:
        out = {"ok": False, "problems": [r.stdout or r.stderr]}
    return r.returncode, out


def codex_candidate():
    gpt = ROSTER["gpt"]
    return {
        "host": "codex",
        "native": dict(resolve(gpt["asNative"], "gpt-5.6-sol"), mode="exec"),
        "seats": [
            dict(resolve(ROSTER["opus"]["asExternal"], "claude-opus-4-8"),
                 name="opus", timeoutSeconds=300),
            dict(resolve(ROSTER["glm"]["asExternal"], "glm-5"), name="glm", timeoutSeconds=300),
            dict(resolve(ROSTER["gemini"]["asExternal"], "gemini-3.1-pro"),
                 name="gemini", timeoutSeconds=300),
            dict(resolve(ROSTER["grok"]["asExternalCursor"], "grok-4"),
                 name="grok", timeoutSeconds=300, adapter="cursor-json", responsePath="result"),
        ],
    }


def claude_candidate():
    return {
        "host": "claude",
        # docs/SETUP.md step-5 native shape, verbatim semantics.
        "native": {"mode": "subagent", "model": "opus",
                   "efforts": {"default": "high", "max": "xhigh"}},
        "seats": [
            dict(resolve(ROSTER["gpt"]["asExternal"], "gpt-5.6-sol"),
                 name="gpt", timeoutSeconds=300),
            dict(resolve(ROSTER["glm"]["asExternal"], "glm-5"), name="glm", timeoutSeconds=300),
            dict(resolve(ROSTER["gemini"]["asExternal"], "gemini-3.1-pro"),
                 name="gemini", timeoutSeconds=300),
            dict(resolve(ROSTER["grok"]["asExternalCursor"], "grok-4"),
                 name="grok", timeoutSeconds=300, adapter="cursor-json", responsePath="result"),
        ],
    }


def main():
    # 1. Conformance: the documented rosters validate as written. {EFFORT} stays in the stored
    #    argv (the runner substitutes it per tier); only {MODEL} is setup-resolved.
    ec, out = validate(codex_candidate(), "codex")
    check("catalog-conformant codex roster validates", ec == 0 and out.get("ok"))
    ec, out = validate(claude_candidate(), "claude")
    check("catalog-conformant claude roster validates", ec == 0 and out.get("ok"))
    check("conformant argv keeps the {EFFORT} runtime placeholder",
          any("{EFFORT}" in a for a in codex_candidate()["native"]["argv"]))

    # 2. Unresolved {MODEL} anywhere is refused.
    cand = codex_candidate()
    cand["native"]["argv"] = [a.replace("gpt-5.6-sol", "{MODEL}") for a in cand["native"]["argv"]]
    ec, out = validate(cand, "codex")
    check("unresolved {MODEL} in native exec argv is refused",
          ec == 1 and any("unresolved" in p for p in out.get("problems", [])))
    cand = claude_candidate()
    cand["native"]["model"] = "{MODEL}"
    ec, out = validate(cand, "claude")
    check("unresolved {MODEL} native subagent model is refused",
          ec == 1 and any("unresolved" in p for p in out.get("problems", [])))

    # 3. The Anthropic-seat rule is mechanical: Fable/Mythos never pass, on either seat kind.
    cand = claude_candidate()
    cand["native"]["model"] = "claude-fable-5"
    ec, out = validate(cand, "claude")
    check("fable native subagent is refused",
          ec == 1 and any("Opus line" in p for p in out.get("problems", [])))
    cand = codex_candidate()
    cand["seats"][0]["argv"] = [a.replace("claude-opus-4-8", "claude-fable-5")
                                for a in cand["seats"][0]["argv"]]
    ec, out = validate(cand, "codex")
    check("fable external opus seat is refused",
          ec == 1 and any("Opus line" in p for p in out.get("problems", [])))
    cand = codex_candidate()
    cand["seats"][0]["name"] = "anthropic"
    cand["seats"][0]["argv"] = [a.replace("claude-opus-4-8", "claude-fable-5")
                                 for a in cand["seats"][0]["argv"]]
    ec, out = validate(cand, "codex")
    check("renaming an Anthropic seat cannot bypass the Opus rule",
          ec == 1 and any("Opus line" in p for p in out.get("problems", [])))

    for bogus in ("not-opus", "opus-impersonator", "claude-not-opus"):
        cand = claude_candidate()
        cand["native"]["model"] = bogus
        ec, out = validate(cand, "claude")
        check(f"substring-only Opus impostor {bogus!r} is refused",
              ec == 1 and any("Opus line" in p for p in out.get("problems", [])))

    cand = codex_candidate()
    cand["seats"][0].pop("provider")
    ec, out = validate(cand, "codex")
    check("external seat provider identity is required",
          ec == 1 and any("provider" in p for p in out.get("problems", [])))

    # 4. An external opus seat that never pins --model runs the CLI default — refused.
    cand = codex_candidate()
    argv = cand["seats"][0]["argv"]
    i = argv.index("--model")
    cand["seats"][0]["argv"] = argv[:i] + argv[i + 2:]
    ec, out = validate(cand, "codex")
    check("unpinned external opus seat is refused",
          ec == 1 and any("pin --model" in p for p in out.get("problems", [])))

    # 5. Non-claude hosts' native subagents are not held to the Opus rule (they run their own
    #    model), but the unresolved check still applies everywhere.
    cand = {"host": "opencode", "native": {"mode": "subagent", "model": "some-host-model"},
            "seats": []}
    ec, out = validate(cand, "opencode")
    check("non-claude native subagent model is host's own affair", ec == 0 and out.get("ok"))

    total = passed + failed
    print(f"seats-tests: {passed}/{total} PASS" + (" — ALL PASS" if not failed else f" ({failed} FAIL)"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
