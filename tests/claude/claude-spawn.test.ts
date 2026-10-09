// Runs under `claude plugin test .` against Claude Code's own event engine:
// the hooks registered on `on` here stand for the engine beneath the mod
// (its environment, the guard process and the spawn itself).
import { test, expect } from 'claude-code/testing'

const MARKER = 'LEOS_AGENT_CLAUDE_SPAWN_MOD'
const CORRECT = { action: 'correct', reason: 'explicit-tier-default', updated_input: { model: 'haiku' } }
const BLOCK = { action: 'block', reason: 'forced-model-over-ceiling', retry: 'Change the force setting.' }
// agent.spawn's input as Claude Code 2.1.296 raised it for a model's Agent call.
const SPAWN = {
  tool_use_id: 'toolu_kit', prompt: 'Rename one symbol', description: 'rename', subagentType: 'leos-agent:leo-cheap',
  provider: { plugin: 'engine', tier: 'core' }, parentModel: 'claude-sonnet-5', permissionMode: 'default',
  background: true, fork: false,
}
const START = { cwd: '/tmp', surface: null, isInteractive: false }

function world(on: any, guard: unknown, { version = '2.1.296', inherited }: { version?: string; inherited?: string } = {}) {
  const env = new Map<string, string>(inherited ? [[MARKER, inherited]] : [])
  const runs: { argv: readonly string[]; stdin?: string }[] = []
  const spawned: { model?: string; tool_use_id: string; fork: boolean }[] = []
  on('env.get', ($: any, e: any) => ({ value: env.get(e.name) }))
  on('env.set', ($: any, e: any) => {
    if (e.value === undefined) env.delete(e.name)
    else env.set(e.name, e.value)
    return { value: undefined }
  })
  on('session.start', ($: any, e: any) => ({ cwd: e.cwd }))
  on('session.version', () => ({ value: { version, base: version } }))
  on('session.id', () => ({ value: 'kit-session' }))
  on('session.cwd', () => ({ value: '/tmp' }))
  on('process.run', ($: any, e: any) => {
    runs.push({ argv: e.argv, stdin: e.init?.stdin })
    return { value: { exitCode: 0, stdout: JSON.stringify(guard), stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
  })
  on('agent.spawn', ($: any, e: any) => {
    spawned.push({ model: e.model, tool_use_id: e.tool_use_id, fork: e.fork })
    return { model: e.model ?? e.parentModel, agentId: 'a1' }
  })
  return { env, runs, spawned }
}

test('session.start marks the spawn hook live with the plugin root', async ($, on) => {
  const w = world(on, CORRECT)
  await $.session.start(START as any)
  expect((w.env.get(MARKER) ?? '').startsWith('/')).toBe(true)
})

test('a build whose agent.spawn misses teammates clears an inherited marker', async ($, on) => {
  const w = world(on, CORRECT, { version: '2.1.288', inherited: '/somewhere/else' })
  await $.session.start(START as any)
  expect(w.env.has(MARKER)).toBe(false)
})

test('the guard correction is the model the subagent starts on', async ($, on) => {
  const w = world(on, CORRECT)
  await $.session.start(START as any)
  const result = await $.agent.spawn(SPAWN as any)
  expect(result.model).toBe('haiku')
  expect(w.spawned).toEqual([{ model: 'haiku', tool_use_id: 'toolu_kit', fork: false }])
  expect(w.runs.length).toBe(1)
  expect(w.runs[0].argv).toEqual(['python3', `${w.env.get(MARKER)}/scripts/dispatch_guard.py`, '--json'])
  const sent = JSON.parse(w.runs[0].stdin ?? '{}')
  expect(sent.tool_input).toEqual({ subagent_type: 'leos-agent:leo-cheap', prompt: 'Rename one symbol', description: 'rename' })
  expect(sent.parent_model).toBe('claude-sonnet-5')
  expect(sent.tool_use_id).toBe('toolu_kit')
  expect(sent.session_id).toBe('kit-session')
})

test('a guard block refuses the spawn and nothing starts', async ($, on) => {
  const w = world(on, BLOCK)
  await $.session.start(START as any)
  const result = await $.agent.spawn({ ...SPAWN, model: 'opus' } as any)
  expect(result.deny).toEqual(expect.stringContaining(BLOCK.reason))
  expect(result.deny).toEqual(expect.stringContaining(BLOCK.retry))
  expect(w.spawned.length).toBe(0)
})

test('without a live marker the spawn is left to the command guard', async ($, on) => {
  const w = world(on, CORRECT, { inherited: '/another/plugin/root' })
  const result = await $.agent.spawn({ ...SPAWN, model: 'opus' } as any)
  expect(result.model).toBe('opus')
  expect(w.runs.length).toBe(0)
})

test('a fork is never routed', async ($, on) => {
  const w = world(on, CORRECT)
  await $.session.start(START as any)
  await $.agent.spawn({ ...SPAWN, subagentType: 'fork', fork: true } as any)
  expect(w.runs.length).toBe(0)
  expect(w.spawned.map((s) => s.fork)).toEqual([true])
})
