"""Static (and, when node is available, behavioral) checks for the
cost-tiered-fix workflow script.

Run: python3 -m unittest tests.test_workflow_static -v

The workflow has no JS test runner and the repo deliberately ships no
package.json (see plugins/leo/workflows in general), so these are plain
text-level invariants plus one node-backed behavioral smoke test that is
skipped when node is unavailable.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import unittest


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOW = os.path.join(REPO, "plugins", "leo", "workflows", "cost-tiered-fix.js")


def _read():
    with open(WORKFLOW, encoding="utf-8") as fh:
        return fh.read()


class TestOrphanVsRejectedReporting(unittest.TestCase):
    """A branch rejected for a fixable reason may hold the only copy of that
    work — the report must never call it safe to delete."""

    def test_report_returns_separate_orphans_and_rejected(self):
        text = _read()
        self.assertIn("rejected:", text)
        self.assertIn("orphans", text)
        self.assertIn("orphans,", text)

    def test_safe_to_delete_phrase_is_scoped_to_orphans_only(self):
        text = _read()
        # The phrase must appear (describing orphans) but never inside a
        # sentence about rejected branches.
        self.assertIn("safe to delete", text)
        # Look at the sentence containing the phrase, not the whole line —
        # the note is one long template-literal line that also separately
        # discusses rejected branches later on.
        for sentence in re.split(r"(?<=[.:])\s+", text):
            if "safe to delete" in sentence:
                with self.subTest(sentence=sentence):
                    self.assertNotIn("rejected", sentence.lower())

    def test_orphans_sourced_only_from_superseded_branches(self):
        text = _read()
        # The bug: orphans used to be `created` (done branches) minus `kept`,
        # which folded rejected branches in. It must now be built solely
        # from supersededBranches.
        match = re.search(r"const orphans = (.+)", text)
        self.assertIsNotNone(match, "expected a single `const orphans = ...` assignment")
        self.assertIn("supersededBranches", match.group(1))
        self.assertNotIn("created", match.group(1))


class TestTierSchemaConsistency(unittest.TestCase):
    def test_plan_schema_tier_enum_matches_tiers_constant(self):
        text = _read()
        enum_match = re.search(r"enum:\s*\[([^\]]+)\]", text)
        self.assertIsNotNone(enum_match, "expected PLAN_SCHEMA tier enum")
        enum_body = enum_match.group(1)
        self.assertIn("TIERS.cheap", enum_body)
        self.assertIn("TIERS.normal", enum_body)
        # The judge tier must never be offered to the planning agent — the
        # judge is reserved for verification, not something the planner
        # assigns to a work item.
        self.assertNotIn("TIERS.judge", enum_body)

    def test_next_tier_ceiling_is_judge(self):
        text = _read()
        match = re.search(r"function nextTier\(tier\)\s*\{(.*?)\n\}", text, re.DOTALL)
        self.assertIsNotNone(match, "expected a nextTier function")
        body = match.group(1)
        self.assertIn("TIERS.judge", body)


class TestNoBareModelLiterals(unittest.TestCase):
    """`[1m]` is /model command / SKILL frontmatter syntax, NOT a valid model
    value for a subagent spawn — passing it as `model:` reaches the model
    selector verbatim and kills the spawn (the outage this repo already had).
    Bare aliases (`haiku`, `sonnet`, `opus`) are the documented subagent
    model shape, so this test enforces the OPPOSITE of what its name used to:
    no `[1m]` suffix anywhere, and every model literal must be a bare alias.
    Do NOT "fix" this by adding `[1m]` back — that is the regression."""

    ALLOWED_MODELS = {"haiku", "sonnet", "opus"}

    def test_no_1m_suffix_anywhere(self):
        text = _read()
        self.assertNotIn("[1m]", text)

    def test_all_model_literals_are_bare_aliases(self):
        text = _read()
        literals = re.findall(r"model:\s*'([^']+)'", text)
        self.assertTrue(literals, "expected at least one model: '...' literal")
        for literal in literals:
            with self.subTest(literal=literal):
                self.assertIn(literal, self.ALLOWED_MODELS)

    def test_tiers_constant_values_are_bare_aliases(self):
        text = _read()
        match = re.search(r"const TIERS = \{(.*?)\n\}", text, re.DOTALL)
        self.assertIsNotNone(match, "expected a `const TIERS = {...}` block")
        body = match.group(1)
        for literal in re.findall(r":\s*'([^']+)'", body):
            with self.subTest(literal=literal):
                self.assertIn(literal, self.ALLOWED_MODELS)


class TestEscalationBudget(unittest.TestCase):
    def test_effort_for_normal_tier_is_not_low(self):
        text = _read()
        match = re.search(r"function effortFor\(tier\)\s*\{(.*?)\n\}", text, re.DOTALL)
        self.assertIsNotNone(match, "expected an effortFor function")
        body = match.group(1)
        # A cheap->normal escalation must raise the reasoning budget, not
        # just swap models — 'low' for the normal branch would be the
        # original bug.
        self.assertNotRegex(body, r"TIERS\.normal\s*\?\s*'low'")


class TestCallerTaskValidation(unittest.TestCase):
    def test_validates_caller_supplied_tasks(self):
        text = _read()
        self.assertIn("function validateTask", text)
        self.assertIn("must be a string or an object", text)
        # Behavioral coverage for the actual reject/accept behavior lives in
        # TestWorkflowBehavioral.test_invalid_tier_throws below (node-gated).


class TestNoUnreachableStage3NullGuard(unittest.TestCase):
    def test_stage3_no_longer_null_guards_run(self):
        text = _read()
        # Stage 2 always returns a non-null object, so stage 3's leading
        # `if (!run) return null` was dead code.
        self.assertNotIn("if (!run) return null", text)


@unittest.skipUnless(shutil.which("node"), "node not on PATH")
class TestWorkflowBehavioral(unittest.TestCase):
    """Runs the workflow script under node with stubbed agent/pipeline/phase/log
    globals, exercising four scenarios end to end."""

    @classmethod
    def setUpClass(cls):
        harness = os.path.join(os.path.dirname(__file__), "_workflow_static_harness.mjs")
        cls._harness_path = harness
        with open(harness, "w", encoding="utf-8") as fh:
            fh.write(_HARNESS_SOURCE)
        result = subprocess.run(
            ["node", harness, WORKFLOW],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=30,
        )
        cls._proc = result
        if result.returncode != 0:
            raise AssertionError(f"harness failed: {result.stdout}\n{result.stderr}")
        cls.output = json.loads(result.stdout)

    @classmethod
    def tearDownClass(cls):
        if os.path.exists(cls._harness_path):
            os.remove(cls._harness_path)

    def test_all_high_confidence(self):
        r = self.output["allHighConfidence"]
        self.assertEqual(len(r["approved"]), 1)
        self.assertEqual(len(r["rejected"]), 0)
        self.assertFalse(r["approved"][0]["escalated"])

    def test_low_confidence_escalation(self):
        r = self.output["lowConfidenceEscalation"]
        self.assertEqual(len(r["approved"]), 1)
        self.assertTrue(r["approved"][0]["escalated"])

    def test_all_attempts_failed(self):
        r = self.output["allAttemptsFailed"]
        self.assertEqual(len(r["approved"]), 0)
        self.assertEqual(len(r["rejected"]), 1)
        self.assertIsNone(r["rejected"][0]["branch"])

    def test_zero_item_plan(self):
        r = self.output["zeroItemPlan"]
        self.assertEqual(r["approved"], [])
        self.assertEqual(r["rejected"], [])

    def test_low_confidence_escalation_reports_superseded_branch_as_orphan(self):
        # Behavioral replacement for the old text-grep checks (assertIn
        # "orphans," / "supersededBranches" in source): actually run the
        # escalation path and check the superseded first-attempt branch
        # shows up in `orphans`, not silently dropped.
        r = self.output["lowConfidenceEscalation"]
        self.assertIn("leos/fix-0", r["orphans"])
        self.assertIn("safe to delete", r["note"])

    def test_invalid_tier_is_rejected_before_any_agent_call(self):
        # Behavioral replacement for the old text-grep check (assertIn
        # "function validateTask" / "must be a string or an object" in
        # source): actually feed a bad tier through args.tasks and confirm
        # it throws instead of silently flowing into `model:`.
        r = self.output["invalidTierThrows"]
        self.assertTrue(r["threw"], "expected an invalid tier to throw")
        self.assertIn("must be one of", r["message"])


_HARNESS_SOURCE = r"""
import fs from 'node:fs'

const workflowPath = process.argv[2]
const rawCode = fs.readFileSync(workflowPath, 'utf8')
// The real host strips/parses the leading `export const meta = {...}` block
// separately and runs the rest as a function body (top-level `return` and
// `await` prove that — neither is legal in a plain ES module or script).
// Reproduce that here: drop the export statement, keep everything else.
const code = rawCode.replace(/^export const meta = \{[\s\S]*?\n\}\n/, '')
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor
const run = new AsyncFunction('args', 'agent', 'pipeline', 'phase', 'log', code)

function stubPipeline() {
  const stages = Array.prototype.slice.call(arguments, 1)
  const items = arguments[0]
  return (async () => {
    const results = []
    for (let i = 0; i < items.length; i++) {
      let val = items[i]
      for (const stage of stages) {
        val = await stage(val, items[i], i)
      }
      results.push(val)
    }
    return results
  })()
}

const noopPhase = () => {}
const noopLog = () => {}

async function scenario(agentImpl, tasks) {
  return run({ tasks }, agentImpl, stubPipeline, noopPhase, noopLog)
}

async function main() {
  const out = {}

  // Scenario A: one item, high confidence first try, approved on verify.
  out.allHighConfidence = await scenario(async (prompt, opts) => {
    if (opts.phase === 'Verify') return { approved: true, issues: [] }
    return { branch: 'leos/fix-0', summary: 'done', checks: 'ran tests', confidence: 'high' }
  }, ['do the thing'])

  // Scenario B: low confidence first try, escalates once, then confident.
  out.lowConfidenceEscalation = await scenario(async (prompt, opts) => {
    if (opts.phase === 'Verify') return { approved: true, issues: [] }
    if (opts.label.startsWith('exec-')) {
      return { branch: 'leos/fix-0', summary: 'unsure', confidence: 'low' }
    }
    return { branch: 'leos/fix-0-r2', summary: 'fixed on escalation', confidence: 'high' }
  }, ['do the thing'])

  // Scenario C: every attempt returns null -> no branch -> rejected, not approved.
  out.allAttemptsFailed = await scenario(async (prompt, opts) => {
    if (opts.phase === 'Verify') return { approved: false, issues: ['should not be called'] }
    return null
  }, ['do the thing'])

  // Scenario D: zero items.
  out.zeroItemPlan = await scenario(async () => null, [])

  // Scenario E: caller-supplied task with an invalid tier must throw before
  // any agent call happens (validateTask's gate).
  out.invalidTierThrows = await (async () => {
    try {
      await scenario(async () => {
        throw new Error('agent should never be called for an invalid tier')
      }, [{ task: 'do the thing', tier: 'nonsense' }])
      return { threw: false }
    } catch (err) {
      return { threw: true, message: String((err && err.message) || err) }
    }
  })()

  process.stdout.write(JSON.stringify(out))
}

main().catch(err => {
  process.stderr.write(String(err && err.stack || err) + ' ')
  process.exit(1)
})
"""


if __name__ == "__main__":
    unittest.main()
