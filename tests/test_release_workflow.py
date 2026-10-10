"""The release workflow's concurrency, replayed under GitHub's queueing rule.

GitHub runs at most one workflow run per concurrency group and keeps at most
one more pending behind it. A run that joins a group with a run already pending
cancels the pending one and takes its place, whatever cancel-in-progress says;
that setting reaches only the running run. With `queue: max` up to 100 runs
wait instead. (GitHub Docs, "Control the concurrency of workflows and jobs".)

So the group expression decides which runs can displace which. These tests
evaluate the workflow's own expression for each kind of push and replay pushes
through that rule, rather than pinning the expression's text.
"""

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"


def concurrency_settings(text):
    block = re.search(r"(?m)^concurrency:\n((?:[ \t]+.*\n?)+)", text)
    if block is None:
        raise AssertionError("release.yml has no top-level concurrency block")
    body = block.group(1)

    def field(name):
        match = re.search(r"(?m)^\s+%s:\s*(.+?)\s*$" % re.escape(name), body)
        return match.group(1) if match else None

    return field("group"), field("cancel-in-progress"), field("queue")


class Expression:
    """The subset of GitHub's expression language a group key needs.

    Literals, context properties, ==, !=, !, &&, || and parentheses, with
    GitHub's semantics: && and || return an operand rather than a boolean, and
    string comparison ignores case. Anything else raises, so an expression this
    cannot read fails the test instead of evaluating to something plausible.
    """

    TOKEN = re.compile(r"\s*(?:('(?:[^']|'')*')|(==|!=|&&|\|\||!|\(|\))|([A-Za-z_][\w-]*(?:\.[\w-]+)*))")

    def __init__(self, source, contexts):
        self.tokens = []
        position = 0
        source = source.strip()
        while position < len(source):
            match = self.TOKEN.match(source, position)
            if match is None or match.end() == position:
                raise AssertionError(f"unsupported expression syntax at {source[position:]!r}")
            self.tokens.append(match.groups())
            position = match.end()
        self.contexts = contexts
        self.index = 0

    def evaluate(self):
        value = self.either()
        if self.index != len(self.tokens):
            raise AssertionError(f"trailing tokens in expression: {self.tokens[self.index:]}")
        return value

    def peek(self, operator):
        return self.index < len(self.tokens) and self.tokens[self.index][1] == operator

    def take(self, operator):
        if not self.peek(operator):
            raise AssertionError(f"expected {operator!r}")
        self.index += 1

    def either(self):
        value = self.both()
        while self.peek("||"):
            self.take("||")
            right = self.both()
            value = value if truthy(value) else right
        return value

    def both(self):
        value = self.comparison()
        while self.peek("&&"):
            self.take("&&")
            right = self.comparison()
            value = right if truthy(value) else value
        return value

    def comparison(self):
        value = self.unary()
        while self.peek("==") or self.peek("!="):
            negate = self.peek("!=")
            self.index += 1
            same = equal(value, self.unary())
            value = not same if negate else same
        return value

    def unary(self):
        if self.peek("!"):
            self.take("!")
            return not truthy(self.unary())
        if self.peek("("):
            self.take("(")
            value = self.either()
            self.take(")")
            return value
        if self.index >= len(self.tokens):
            raise AssertionError("expression ended early")
        literal, operator, name = self.tokens[self.index]
        self.index += 1
        if literal is not None:
            return literal[1:-1].replace("''", "'")
        if name in ("true", "false"):
            return name == "true"
        if name == "null":
            return None
        if name is None:
            raise AssertionError(f"unexpected {operator!r}")
        value = self.contexts
        for part in name.split("."):
            if not isinstance(value, dict) or part not in value:
                raise AssertionError(f"the replay has no value for {name}")
            value = value[part]
        return value


def truthy(value):
    return value not in (None, False, 0, "")


def equal(left, right):
    if isinstance(left, str) and isinstance(right, str):
        return left.lower() == right.lower()
    return left == right


def interpolate(template, contexts):
    return re.sub(r"\$\{\{(.*?)\}\}", lambda m: str(Expression(m.group(1), contexts).evaluate()), template)


def push(ref):
    kind, _, name = ref[len("refs/"):].partition("/")
    return {"github": {
        "ref": ref, "ref_name": name, "ref_type": "tag" if kind == "tags" else "branch",
        "event_name": "push", "workflow": "release", "repository": "foxhatleo/leos-agent",
    }}


def replay(refs, group, cancel_in_progress, queue):
    """Start one run per push, none finishing; return (running, cancelled) runs."""
    running, pending, cancelled = {}, {}, []
    for number, ref in enumerate(refs):
        run = (number, ref)
        key = interpolate(group, push(ref))
        if key not in running:
            running[key] = run
            continue
        if cancel_in_progress == "true":
            cancelled.append(running[key])
            running[key] = run
            continue
        waiting = pending.setdefault(key, [])
        if queue == "max":
            # Up to 100 wait; once the queue is full, the newcomer is cancelled.
            if len(waiting) == 100:
                cancelled.append(run)
            else:
                waiting.append(run)
        else:
            cancelled.extend(waiting)
            waiting[:] = [run]
    return sorted(running.values()), cancelled


MAIN = "refs/heads/main"


class TestReleaseConcurrency(unittest.TestCase):
    def setUp(self):
        self.group, self.cancel_in_progress, self.queue = concurrency_settings(WORKFLOW.read_text(encoding="utf-8"))
        self.assertIsNotNone(self.group)

    def replay(self, *refs):
        return replay(refs, self.group, self.cancel_in_progress, self.queue)

    def cancelled(self, *refs):
        return [ref for _, ref in self.replay(*refs)[1]]

    def test_a_running_release_is_never_cancelled(self):
        # A run that has pushed its tag still has to publish it.
        running, cancelled = self.replay(MAIN, MAIN, MAIN)
        self.assertEqual(running, [(0, MAIN)])
        self.assertNotIn((0, MAIN), cancelled)

    def test_main_pushes_prepare_releases_one_at_a_time(self):
        # Two runs preparing a release at once would derive the same version.
        running, _ = self.replay(MAIN, MAIN, MAIN, "refs/tags/v12.2026100901.0", MAIN)
        self.assertEqual([run for run in running if run[1] == MAIN], [(0, MAIN)])

    def test_a_hand_pushed_tag_waiting_behind_a_release_survives_the_next_main_push(self):
        # A superseded main run is fine to drop: the newer tip contains it. A
        # tag has no successor, so cancelling its run means it never publishes.
        tag = "refs/tags/v12.2026100901.0"
        self.assertNotIn(tag, self.cancelled(MAIN, tag, MAIN))
        self.assertNotIn(tag, self.cancelled(tag, MAIN, MAIN))

    def test_hand_pushed_tags_do_not_displace_each_other(self):
        first, second = "refs/tags/v12.2026100900.0", "refs/tags/v12.2026100901.0"
        cancelled = self.cancelled(MAIN, first, second, MAIN)
        self.assertNotIn(first, cancelled)
        self.assertNotIn(second, cancelled)


class TestTheReplayItself(unittest.TestCase):
    """The rule the tests above lean on, checked against its documented cases."""

    def test_a_shared_group_cancels_the_waiting_run(self):
        running, cancelled = replay([MAIN, "refs/tags/v1.0.0", MAIN], "release", "false", None)
        self.assertEqual((running, cancelled), ([(0, MAIN)], [(1, "refs/tags/v1.0.0")]))

    def test_cancel_in_progress_reaches_the_running_run(self):
        running, cancelled = replay([MAIN, MAIN], "release", "true", None)
        self.assertEqual((running, cancelled), ([(1, MAIN)], [(0, MAIN)]))

    def test_queue_max_keeps_waiting_runs(self):
        self.assertEqual(replay([MAIN, "refs/tags/v1.0.0", MAIN], "release", "false", "max")[1], [])

    def test_operators_return_operands_and_compare_without_case(self):
        tag = push("refs/tags/v1.0.0")
        self.assertEqual(interpolate("r-${{ github.ref_type == 'TAG' && github.ref || 'main' }}", tag), "r-refs/tags/v1.0.0")
        self.assertEqual(interpolate("r-${{ github.ref_type == 'tag' && github.ref || 'main' }}", push(MAIN)), "r-main")

    def test_an_unknown_context_fails_instead_of_guessing(self):
        with self.assertRaises(AssertionError):
            interpolate("${{ github.head_ref }}", push(MAIN))


if __name__ == "__main__":
    unittest.main()
