#!/usr/bin/env python3
"""Tests for bin/leos-seats.py + leos-doctor.check_seat_flags — seat-roster validation.

The conformance half builds candidates from the EXACT committed recipes (core/council/
seats.catalog.json) with {MODEL} resolved to dummy slugs and {EFFORT} retained, and asserts
`leos-seats.py validate` accepts them — the documented install flow must never be refused by our
own validator. The rejection half asserts the model rules are mechanical, not prose: an old-shape
file, unresolved {MODEL}, a Fable/Mythos Anthropic seat, an out-of-range minTier, an envFile escape,
and an unpinned exec opus seat must all be refused. Run: bin/leos-python tests/seats-tests.py
"""

import copy
import json
import os
import shutil
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
PRESETS = CATALOG["presets"]["minTier"]

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
    if "argv" in seat:
        seat["argv"] = [a.replace("{MODEL}", model) for a in seat["argv"]]
    if "model" in seat and isinstance(seat["model"], str):
        seat["model"] = seat["model"].replace("{MODEL}", model)
    seat.update(extra or {})
    return seat


def validate(candidate, host):
    d = tempfile.mkdtemp(prefix="seatcand.")
    path = os.path.join(d, "candidate.json")
    with open(path, "w") as f:
        json.dump(candidate, f)
    r = subprocess.run([sys.executable, SEATS, "validate", "--host", host, "--input", path],
                       capture_output=True, text=True)
    shutil.rmtree(d, ignore_errors=True)
    try:
        out = json.loads(r.stdout)
    except Exception:
        out = {"ok": False, "problems": [r.stdout or r.stderr]}
    return r.returncode, out


def exec_seat(role, recipe_key, model, extra=None):
    """An exec seat built from a catalog recipe, with role name + provider + minTier preset."""
    role_entry = ROSTER[role]
    seat = resolve(role_entry[recipe_key], model)
    seat.setdefault("name", role)
    seat.setdefault("provider", role_entry["provider"])
    seat.setdefault("mode", "exec")
    seat.setdefault("minTier", PRESETS.get(role, 4))
    seat.setdefault("timeoutSeconds", 300)
    seat.update(extra or {})
    return seat


def claude_candidate():
    """A 7-role Claude host: opus as an in-process subagent, the rest as exec seats."""
    return {
        "host": "claude",
        "seats": [
            dict(resolve(ROSTER["opus"]["asSubagent"], "claude-opus-4-8"),
                 name="opus", provider="anthropic", mode="subagent", minTier=PRESETS["opus"]),
            exec_seat("gpt", "asExternal", "gpt-5.6-sol"),
            exec_seat("grok", "asExternalCursor", "grok-4.5",
                      {"adapter": "cursor-json", "responsePath": "result"}),
            exec_seat("glm", "asExternalOpencode", "z-ai/glm-5.2"),
            exec_seat("gemini", "asExternalOpencode", "google/gemini-3.1-pro"),
            exec_seat("mimo", "asExternalOpencode", "xiaomi/mimo-v2.5-pro"),
            exec_seat("deepseek", "asExternalOpencode", "deepseek/deepseek-v4-pro"),
        ],
    }


def codex_candidate():
    """A Codex host: gpt as its own-provider exec seat, the rest as exec seats."""
    return {
        "host": "codex",
        "seats": [
            dict(resolve(ROSTER["gpt"]["asNative"], "gpt-5.6-sol"),
                 name="gpt", provider="openai", mode="exec", minTier=PRESETS["gpt"], timeoutSeconds=300),
            exec_seat("opus", "asExternal", "claude-opus-4-8"),
            exec_seat("grok", "asExternalCursor", "grok-4.5",
                      {"adapter": "cursor-json", "responsePath": "result"}),
            exec_seat("glm", "asExternalOpencode", "z-ai/glm-5.2"),
            exec_seat("gemini", "asExternalOpencode", "google/gemini-3.1-pro"),
            exec_seat("mimo", "asExternalOpencode", "xiaomi/mimo-v2.5-pro"),
            exec_seat("deepseek", "asExternalOpencode", "deepseek/deepseek-v4-pro"),
        ],
    }


def main():
    # 1. Conformance: the documented 7-role rosters validate as written. {EFFORT} stays in the
    #    stored argv (the runner substitutes it per tier); only {MODEL} is setup-resolved.
    ec, out = validate(claude_candidate(), "claude")
    check("catalog-conformant claude 7-role roster validates", ec == 0 and out.get("ok"))
    ec, out = validate(codex_candidate(), "codex")
    check("catalog-conformant codex 7-role roster validates", ec == 0 and out.get("ok"))
    check("conformant argv keeps the {EFFORT} runtime placeholder",
          any("{EFFORT}" in a for a in codex_candidate()["seats"][0]["argv"]))

    # 2. The old top-level `native` schema is rejected (must regenerate via SETUP step 5).
    cand = claude_candidate()
    cand["native"] = {"mode": "subagent", "model": "opus"}
    ec, out = validate(cand, "claude")
    check("old-shape top-level native is rejected",
          ec == 1 and any("native" in p and "seats" in p for p in out.get("problems", [])))

    # 3. Unresolved {MODEL} anywhere is refused.
    cand = codex_candidate()
    cand["seats"][0]["argv"] = [a.replace("gpt-5.6-sol", "{MODEL}") for a in cand["seats"][0]["argv"]]
    ec, out = validate(cand, "codex")
    check("unresolved {MODEL} in exec argv is refused",
          ec == 1 and any("unresolved" in p for p in out.get("problems", [])))
    cand = claude_candidate()
    cand["seats"][0]["model"] = "{MODEL}"  # the opus subagent
    ec, out = validate(cand, "claude")
    check("unresolved {MODEL} in subagent model is refused",
          ec == 1 and any("unresolved" in p for p in out.get("problems", [])))

    # 4. The Anthropic-seat rule is mechanical: Fable/Mythos never pass, on either seat kind.
    cand = claude_candidate()
    cand["seats"][0]["model"] = "claude-fable-5"  # opus subagent
    ec, out = validate(cand, "claude")
    check("fable subagent opus seat is refused",
          ec == 1 and any("Opus line" in p for p in out.get("problems", [])))
    cand = codex_candidate()
    cand["seats"][1]["argv"] = [a.replace("claude-opus-4-8", "claude-fable-5")
                                for a in cand["seats"][1]["argv"]]  # opus exec seat
    ec, out = validate(cand, "codex")
    check("fable exec opus seat is refused",
          ec == 1 and any("Opus line" in p for p in out.get("problems", [])))
    cand = codex_candidate()
    cand["seats"][1]["name"] = "anthropic"
    cand["seats"][1]["argv"] = [a.replace("claude-opus-4-8", "claude-fable-5")
                                 for a in cand["seats"][1]["argv"]]
    ec, out = validate(cand, "codex")
    check("renaming an Anthropic seat cannot bypass the Opus rule",
          ec == 1 and any("Opus line" in p for p in out.get("problems", [])))

    for bogus in ("not-opus", "opus-impersonator", "claude-not-opus"):
        cand = claude_candidate()
        cand["seats"][0]["model"] = bogus
        ec, out = validate(cand, "claude")
        check(f"substring-only Opus impostor {bogus!r} is refused",
              ec == 1 and any("Opus line" in p for p in out.get("problems", [])))

    # 5. An exec seat's provider identity is required.
    cand = codex_candidate()
    cand["seats"][1].pop("provider")
    ec, out = validate(cand, "codex")
    check("exec seat provider identity is required",
          ec == 1 and any("provider" in p for p in out.get("problems", [])))

    # 6. An exec opus seat that never pins --model runs the CLI default — refused.
    cand = codex_candidate()
    argv = cand["seats"][1]["argv"]
    i = argv.index("--model")
    cand["seats"][1]["argv"] = argv[:i] + argv[i + 2:]
    ec, out = validate(cand, "codex")
    check("unpinned exec opus seat is refused",
          ec == 1 and any("pin --model" in p for p in out.get("problems", [])))

    # 7. mode: subagent is ONLY valid on a host with an in-process subagent primitive (claude today).
    cand = claude_candidate()
    cand["host"] = "codex"  # move the opus subagent to a non-claude host
    ec, out = validate(cand, "codex")
    check("subagent seat on a non-subagent host is refused",
          ec == 1 and any("subagent" in p and "host" in p for p in out.get("problems", [])))

    # 8. minTier bounds: an out-of-range minTier is refused.
    for bad in (0, 5, "3", True):
        cand = claude_candidate()
        cand["seats"][1]["minTier"] = bad
        ec, out = validate(cand, "claude")
        check(f"out-of-range minTier {bad!r} is refused",
              ec == 1 and any("minTier" in p for p in out.get("problems", [])))

    # 9. envFile must resolve under LEOS_LOCAL (escape attempted).
    cand = claude_candidate()
    cand["seats"][1]["envFile"] = "../../etc/passwd"
    ec, out = validate(cand, "claude")
    check("envFile escaping LEOS_LOCAL is refused",
          ec == 1 and any("envFile" in p for p in out.get("problems", [])))
    cand = claude_candidate()
    cand["seats"][1]["envFile"] = "council/env/gpt.env"
    ec, out = validate(cand, "claude")
    check("envFile under LEOS_LOCAL is accepted", ec == 0 and out.get("ok"))

    # 10. The inline env dict refuses secret-named keys (secrets go in envFile, not env).
    cand = claude_candidate()
    cand["seats"][1]["env"] = {"API_KEY": "x"}
    ec, out = validate(cand, "claude")
    check("secret-named inline env key is refused",
          ec == 1 and any("secret" in p for p in out.get("problems", [])))
    cand = claude_candidate()
    cand["seats"][1]["env"] = {"REGION": "us"}
    ec, out = validate(cand, "claude")
    check("non-secret inline env key is accepted", ec == 0 and out.get("ok"))

    # 11. A codex seat overriding CODEX_HOME is refused (it throws away host auth).
    cand = codex_candidate()
    cand["seats"][0]["env"] = {"CODEX_HOME": "/tmp/isolated"}
    ec, out = validate(cand, "codex")
    check("codex seat overriding CODEX_HOME is refused",
          ec == 1 and any("CODEX_HOME" in p for p in out.get("problems", [])))

    # 12. The two new providers (xiaomi, deepseek) are accepted.
    cand = claude_candidate()
    deepseek = [s for s in cand["seats"] if s["name"] == "deepseek"][0]
    check("deepseek provider is xiaomi's peer (deepseek)", deepseek["provider"] == "deepseek")
    ec, out = validate(cand, "claude")
    check("7-role roster with xiaomi + deepseek providers validates", ec == 0 and out.get("ok"))

    # 13. A config with zero seats is valid (council skips with a reduced-diversity note).
    ec, out = validate({"host": "claude", "seats": []}, "claude")
    check("zero-seat config is valid", ec == 0 and out.get("ok"))

    # 14. A config with no own-provider seat (all foreign) is valid.
    cand = {"host": "claude", "seats": [exec_seat("gpt", "asExternal", "gpt-5.6-sol"),
                                        exec_seat("glm", "asExternalOpencode", "z-ai/glm-5.2")]}
    ec, out = validate(cand, "claude")
    check("all-foreign (no own-provider) config is valid", ec == 0 and out.get("ok"))

    # 15. Plan-specific timeouts are validated at install time.
    cand = codex_candidate()
    cand["seats"][1]["planTimeoutSeconds"] = 901
    ec, out = validate(cand, "codex")
    check("out-of-range plan timeout is refused during seat validation",
          ec == 1 and any("planTimeoutSeconds" in p for p in out.get("problems", [])))

    total = passed + failed
    print(f"seats-tests: {passed}/{total} PASS" + (" — ALL PASS" if not failed else f" ({failed} FAIL)"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
