// The eval runner evaluates grader regexes as JavaScript against JSON.stringify
// output. tests/test_evals.py checks the suite in depth with Python's re; this
// file confirms the patterns compile and behave the same way in V8 itself.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readdirSync, readFileSync, existsSync } from 'node:fs';
import { join, resolve } from 'node:path';

const evals = resolve(import.meta.dirname, '../../evals');

const graders = () => {
  const out = {};
  for (const name of readdirSync(evals)) {
    const dir = join(evals, name, 'graders');
    if (!existsSync(dir)) continue;
    for (const file of readdirSync(dir).filter((f) => f.endsWith('.md'))) {
      const text = readFileSync(join(dir, file), 'utf8');
      const field = (key) => {
        const match = text.match(new RegExp(`^${key}: (?:'((?:[^']|'')*)'|(\\S+))$`, 'm'));
        return match ? (match[1] !== undefined ? match[1].replaceAll("''", "'") : match[2]) : undefined;
      };
      out[`${name}/${file.slice(0, -3)}`] = {
        type: field('type'), pattern: field('pattern'), inputMatch: field('input_match'), flags: field('flags') ?? '',
      };
    }
  }
  return out;
};

const all = graders();
const agent = (subagent_type, model) => JSON.stringify(
  { description: 'Scan the fixture files', prompt: 'Read every file and report.', ...(subagent_type && { subagent_type }), ...(model && { model }) });

test('every grader regex compiles as JavaScript with its flags', () => {
  assert.ok(Object.keys(all).length >= 5);
  for (const [name, g] of Object.entries(all)) {
    const source = g.pattern ?? g.inputMatch;
    if (source === undefined) continue;
    assert.doesNotThrow(() => new RegExp(source, g.flags), name);
  }
});

test('tier graders read the effective tier from JSON.stringify input', () => {
  const cheap = new RegExp(all['cheap-retrieval/cheap-delegation'].inputMatch);
  const standard = new RegExp(all['escalation-after-failed-check/one-tier-up'].inputMatch);
  for (const input of [agent('leos-agent:leo-cheap'), agent('leo-runner'), agent('general-purpose', 'haiku'),
    agent('leos-agent:leo-standard', 'haiku')]) {
    assert.ok(cheap.test(input), input);
    assert.ok(!standard.test(input), input);
  }
  for (const input of [agent('leos-agent:leo-standard'), agent('general-purpose', 'sonnet')]) {
    assert.ok(standard.test(input), input);
    assert.ok(!cheap.test(input), input);
  }
  for (const input of [agent('general-purpose'), agent('other-plugin:leo-cheap'), agent('leos-agent:leo-premium')]) {
    assert.ok(!cheap.test(input) && !standard.test(input), input);
  }
});

test('escalation brief must open with the marker', () => {
  const brief = new RegExp(all['escalation-after-failed-check/escalation-brief'].inputMatch);
  const withPrompt = (prompt) => JSON.stringify({ description: 'Escalate', prompt, subagent_type: 'leos-agent:leo-standard' });
  assert.ok(brief.test(withPrompt('Escalation from cheap: the result failed its count check.')));
  assert.ok(brief.test(withPrompt('**Escalation from cheap:** the result failed its count check.')));
  assert.ok(!brief.test(withPrompt('Please redo this. Escalation from cheap: it failed.')));
});

test('worker contract grader reads a JSON-escaped tool result line', () => {
  const g = all['worker-contract/result-and-verified-lines'];
  const contract = new RegExp(g.pattern, g.flags);
  const line = (text) => JSON.stringify({ type: 'user', message: { role: 'user', content: [
    { tool_use_id: 'toolu_01', type: 'tool_result', content: [{ type: 'text', text }] }] } });
  assert.ok(contract.test(line('catalog, media, search\n\nResult: done\nVerified: read all ten manifests')));
  assert.ok(!contract.test(line('End with `Result: done|partial|blocked|escalate` and\n`Verified: <evidence>`')));
  assert.ok(!contract.test(line('catalog, media, search\nResult: done')));
});

test('answer lines anchor per line under the m flag', () => {
  const g = all['cheap-retrieval/correct-answer'];
  const answer = new RegExp(g.pattern, g.flags);
  assert.ok(answer.test('Checked all eighteen.\n\nANSWER: INC-1003, INC-1008, INC-1015\n'));
  assert.ok(!answer.test('Checked all eighteen.\n\nANSWER: INC-1003, INC-1008, INC-1015, INC-1016\n'));
});
