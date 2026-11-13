"""Native Cursor envelopes, including resolved child models and a plain explicit allow."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import dispatch_log  # noqa: E402


class CursorHooks(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("cursor_hook_test", ROOT / "scripts/cursor_hook.py")
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {"LEOS_AGENT_LOCAL_PATH": self.tmp.name, "LEOS_AGENT_PRICE_REFRESH": "off", "LEOS_AGENT_DISPATCH_GUARD": "on"})
        env.start(); self.addCleanup(env.stop)

    def event(self, child, parent):
        return {"hook_event_name": "subagentStart", "subagent_type": "generalPurpose", "task": "Inspect a change",
                "subagent_model": child, "model_id": parent, "parent_conversation_id": "c", "subagent_id": "t"}

    def test_resolved_expensive_child_is_denied(self):
        result = self.module.handle(self.event("claude-opus-5", "claude-haiku-4-5"))
        self.assertEqual(result["permission"], "deny")
        self.assertIn("parent", result["user_message"])
        self.assertNotIn("agent_message", result)

    def test_an_expensive_lens_is_told_to_review_the_area_itself_not_to_use_leo_parent(self):
        # Cursor's leo-parent has no readonly flag, so the lens must not be sent there.
        event = dict(self.event("claude-opus-5", "claude-haiku-4-5"), subagent_type="leo-lens")
        result = self.module.handle(event)
        self.assertEqual(result["permission"], "deny")
        self.assertIn("review that area yourself", result["user_message"].lower())
        self.assertNotIn("parent-level", result["user_message"])
        worker = self.module.handle(dict(event, subagent_type="leo-standard"))
        self.assertEqual(worker["permission"], "deny")
        self.assertNotIn("review that area yourself", worker["user_message"].lower())

    def test_cheap_and_unknown_children_get_a_plain_allow(self):
        # Cursor blocks a permission hook that gives no valid response, so an
        # allowed start answers allow and adds nothing else.
        self.assertEqual(self.module.handle(self.event("claude-haiku-4-5", "claude-opus-5")), {"permission": "allow"})
        self.assertEqual(self.module.handle(self.event("custom-model", "claude-opus-5")), {"permission": "allow"})

    def test_lifecycle_never_injects_policy_or_claims_executed_model(self):
        self.assertEqual(self.module.handle({"hook_event_name": "sessionStart", "conversation_id": "c", "model_id": "claude-opus-5"}).keys(), {"additional_context"})
        self.assertIsNone(self.module.handle({"hook_event_name": "subagentStop", "conversation_id": "c", "status": "completed"}))
        rows = dispatch_log.read()
        self.assertEqual(rows[-1]["decision"], "completed")
        self.assertIsNone(rows[-1]["effective_model"])
        # A finished status is not a done outcome: Cursor never shows child text.
        self.assertEqual((rows[-1]["outcome"], rows[-1]["outcome_source"], rows[-1]["status"]), ("unknown", "status-only", "completed"))
        self.assertIsNone(rows[-1]["verified"])

    def test_parallel_children_join_by_call_id_and_carry_their_summary(self):
        # subagentStop as current Cursor sends it: the Task call id, the child
        # conversation id, a status token, and the child's summary.
        for call in ("t1", "t2"):
            event = self.event("claude-haiku-4-5", "claude-opus-5")
            event.update(subagent_type="leo-cheap", subagent_id=call)
            self.module.handle(event)
        for call, result in (("t2", "blocked"), ("t1", "done")):
            self.module.handle({"hook_event_name": "subagentStop", "subagent_type": "leo-cheap", "parent_conversation_id": "c",
                                "subagent_id": call, "status": "completed",
                                "description": "Inspect", "summary": "PRIVATE_SUMMARY\nResult: %s\nVerified: none" % result,
                                "modified_files": [], "agent_transcript_path": None})
        rows = dispatch_log.read()
        stops = [r for r in rows if r["decision"] == "completed"]
        self.assertEqual([(r["call_id"], r["agent_id"], r["outcome"], r["outcome_source"]) for r in stops],
                         [("t2", "t2", "blocked", "message"), ("t1", "t1", "done", "message")])
        summary = dispatch_log.summarise(rows)
        self.assertEqual(summary["joins"], {"call_id": 2})
        self.assertEqual(summary["outcomes"], {"cheap": {"blocked": 1, "done": 1}})
        self.assertNotIn("PRIVATE_SUMMARY", (Path(self.tmp.name) / "dispatch.jsonl").read_text())

    def test_unknown_resolved_model_does_not_use_config_as_observation(self):
        event = self.event(None, "claude-haiku-4-5")
        event["subagent_type"] = "leo-standard"
        self.assertEqual(self.module.handle(event), {"permission": "allow"})
        self.assertEqual(dispatch_log.read()[-1]["reason"], "per-dispatch-routing-unavailable")


class CursorProcess(unittest.TestCase):
    """The script as Cursor runs it: a process, cwd = the workspace root."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cwd = tempfile.TemporaryDirectory()
        self.addCleanup(self.cwd.cleanup)

    def run_hook(self, name, event, extra_env=None, script=None):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("LEOS_AGENT", "CLAUDE", "CODEX", "CURSOR", "OPENCODE", "HERMES"))}
        env.update(HOME=self.tmp.name, LEOS_AGENT_LOCAL_PATH=self.tmp.name, LEOS_AGENT_PRICE_REFRESH="off",
                   LEOS_AGENT_DISPATCH_GUARD="on", PYTHONDONTWRITEBYTECODE="1")
        env.update(extra_env or {})
        raw = event if isinstance(event, bytes) else json.dumps(event).encode()
        done = subprocess.run([sys.executable, script or str(ROOT / "scripts/cursor_hook.py"), name], input=raw,
                              capture_output=True, cwd=self.cwd.name, env=env, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout.decode()

    def test_manifest_uses_plugin_root_and_names_each_event(self):
        hooks = json.loads((ROOT / "hooks/hooks-cursor.json").read_text())["hooks"]
        self.assertEqual(set(hooks), {"sessionStart", "subagentStart", "subagentStop", "preCompact"})
        for name, (entry,) in hooks.items():
            self.assertIn('"$CURSOR_PLUGIN_ROOT/scripts/cursor_hook.py"', entry["command"])
            self.assertEqual(entry["command"].split()[-1], name)

    def test_session_start_policy_is_deterministic_and_works_from_any_cwd(self):
        first = self.run_hook("sessionStart", {"hook_event_name": "sessionStart", "session_id": "s"})
        second = self.run_hook("sessionStart", {"hook_event_name": "sessionStart", "session_id": "s"})
        self.assertEqual(first, second)
        out = json.loads(first)
        self.assertEqual(list(out), ["additional_context"])
        self.assertTrue(out["additional_context"].strip())

    def test_session_start_payload_off_emits_empty_object(self):
        self.assertEqual(json.loads(self.run_hook("sessionStart", {}, {"LEOS_AGENT_PAYLOAD": "off"})), {})

    def test_pre_compact_nudge(self):
        import compact_nudge
        out = json.loads(self.run_hook("preCompact", {"hook_event_name": "preCompact", "trigger": "auto"}))
        self.assertEqual(out, {"user_message": compact_nudge.MESSAGE})

    def stop(self, summary, **extra):
        return dict({"hook_event_name": "subagentStop", "subagent_type": "leo-cheap", "status": "completed",
                     "description": "d", "task": "t", "summary": summary, "parent_conversation_id": "c"}, **extra)

    def test_stop_asks_once_for_missing_contract_lines(self):
        out = json.loads(self.run_hook("subagentStop", self.stop("did things")))
        self.assertEqual(list(out), ["followup_message"])
        self.assertNotIn("\u2014", out["followup_message"])
        self.assertEqual(self.run_hook("subagentStop", self.stop("did things")).strip(), "{}")

    def test_stop_with_contract_or_other_status_or_other_agent_is_quiet(self):
        ok = self.stop("x\nResult: done\nVerified: none")
        self.assertEqual(self.run_hook("subagentStop", ok).strip(), "{}")
        self.assertEqual(self.run_hook("subagentStop", dict(self.stop("x"), status="aborted")).strip(), "{}")
        self.assertEqual(self.run_hook("subagentStop", dict(self.stop("x"), subagent_type="explore")).strip(), "{}")

    def test_stop_guard_off_never_prompts(self):
        self.assertEqual(self.run_hook("subagentStop", self.stop("x"), {"LEOS_AGENT_DISPATCH_GUARD": "off"}).strip(), "{}")

    def test_import_failure_still_allows_subagent_start(self):
        stub = Path(self.tmp.name) / "stub"
        (stub / "scripts").mkdir(parents=True)
        (stub / "scripts/cursor_hook.py").write_text((ROOT / "scripts/cursor_hook.py").read_text())
        (stub / "scripts/dispatch_guard.py").write_text("raise ImportError('broken')\n")
        script = str(stub / "scripts/cursor_hook.py")
        event = {"hook_event_name": "subagentStart", "subagent_type": "x"}
        self.assertEqual(json.loads(self.run_hook("subagentStart", event, script=script)), {"permission": "allow"})
        self.assertEqual(json.loads(self.run_hook("subagentStart", b"garbage", script=script)), {"permission": "allow"})
        self.assertEqual(json.loads(self.run_hook("preCompact", {"hook_event_name": "preCompact"}, script=script)), {})

    def test_start_deny_shape_uses_documented_fields_only(self):
        event = {"hook_event_name": "subagentStart", "subagent_type": "generalPurpose", "task": "x",
                 "subagent_model": "claude-opus-5", "model_id": "claude-haiku-4-5", "parent_conversation_id": "c", "subagent_id": "a"}
        out = json.loads(self.run_hook("subagentStart", event))
        self.assertEqual(set(out), {"permission", "user_message"})
        self.assertEqual(out["permission"], "deny")


if __name__ == "__main__":
    unittest.main()
