export const meta = {
  name: 'cost-tiered-fix',
  description: 'Fix a batch of independent tasks with tiered models: Opus plans and verifies, Haiku/Sonnet execute, low-confidence items escalate to Opus',
  whenToUse: 'A list of independent, well-scoped fixes (many tickets, many files) — NOT one large stateful change, which belongs in a normal session with subagents',
  phases: [
    { title: 'Plan', detail: 'decompose the goal into tiered work items', model: 'opus[1m]' },
    { title: 'Execute', detail: 'cheap executors, one isolated worktree per item' },
    { title: 'Verify', detail: 'Opus reviews each branch diff', model: 'opus[1m]' },
  ],
}

// Invoke with either:
//   args: { goal: "...", runId?: "..." }                     -> Opus plans the decomposition
//   args: { tasks: ["...", { task, tier }], runId?: "..." }  -> skip planning, run your list
// runId (e.g. a ticket id or date string) namespaces branch names across runs;
// Date.now()/Math.random() are unavailable in workflow scripts, so it must come
// from the caller. Without it, executors resolve collisions by numeric suffix.
// Each work item ends up as a committed branch plus an Opus verdict.
// Merging approved branches is left to the main session.

if (!args || (!args.goal && !Array.isArray(args.tasks))) {
  throw new Error('cost-tiered-fix needs args: { goal: "..." } or { tasks: [...] }')
}

const BRANCH_PREFIX = args.runId ? `leos/fix-${args.runId}` : 'leos/fix'

const PLAN_SCHEMA = {
  type: 'object',
  properties: {
    items: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          task: { type: 'string', description: 'self-contained instruction: exact file paths, expected behavior, how to check it' },
          tier: { type: 'string', enum: ['haiku', 'sonnet[1m]'], description: 'haiku for mechanical work, sonnet[1m] for normal implementation' },
        },
        required: ['task', 'tier'],
      },
    },
  },
  required: ['items'],
}

const EXEC_SCHEMA = {
  type: 'object',
  properties: {
    branch: { type: 'string', description: 'the branch actually created and committed to; omit if no branch was created' },
    summary: { type: 'string' },
    checks: { type: 'string', description: 'what was run to verify, and the result' },
    confidence: { type: 'string', enum: ['high', 'medium', 'low'] },
  },
  required: ['summary', 'confidence'],
}

const VERDICT_SCHEMA = {
  type: 'object',
  properties: {
    approved: { type: 'boolean' },
    issues: { type: 'array', items: { type: 'string' } },
  },
  required: ['approved', 'issues'],
}

function execPrompt(task, branch) {
  return [
    'You are one executor in a fan-out. Work ONLY on this task; touch nothing else.',
    `Task: ${task}`,
    `You are in an isolated git worktree. Create and switch to branch ${branch} (if that name already exists, use the lowest free numeric suffix, e.g. ${branch}-2), implement the task, run the narrowest relevant check (tests/build for the touched files), and commit.`,
    'Report the branch name you actually used. If the task is ambiguous or you cannot make the check pass, commit only what is safe and report confidence: low with the blocker in summary. If you created no branch, omit the branch field entirely — never invent one.',
  ].join('\n')
}

// Next tier up the escalation ladder. Opus is the ceiling: it has nowhere
// left to escalate to, so it maps to itself.
function nextTier(tier) {
  if (tier === 'haiku') return 'sonnet[1m]'
  if (tier === 'sonnet[1m]') return 'opus[1m]'
  return 'opus[1m]'
}

function effortFor(tier) {
  return tier === 'opus[1m]' ? 'high' : 'low'
}

phase('Plan')
let items
if (Array.isArray(args.tasks)) {
  items = args.tasks.map(t => (typeof t === 'string' ? { task: t, tier: 'sonnet[1m]' } : { tier: 'sonnet[1m]', ...t }))
  log(`Using ${items.length} caller-provided tasks (planning skipped)`)
} else {
  const plan = await agent(
    'Decompose this goal into independent, well-scoped work items that can each be done in an isolated worktree without touching the same files. For each item write a self-contained instruction (exact file paths, expected behavior, how to check it) and pick a tier: haiku for mechanical work, sonnet[1m] for normal implementation. At most 10 items — if the goal needs more, return the 10 highest-value and say so in the last item.\n\nGoal: ' + args.goal,
    { label: 'plan', phase: 'Plan', model: 'opus[1m]', effort: 'high', schema: PLAN_SCHEMA },
  )
  if (!plan || !Array.isArray(plan.items) || plan.items.length === 0) {
    log('Planning agent failed or returned no items — aborting cleanly')
    return { approved: [], rejected: [], note: 'planning agent died or produced no work items; nothing was run' }
  }
  items = plan.items
  log(`Planned ${items.length} work items`)
}
if (items.length > 10) {
  log(`Capping fan-out: running the first 10 of ${items.length} items`)
  items = items.slice(0, 10)
}

// pipeline(): no barrier between stages — item 0 can be verifying while item 3
// is still executing. Wall-clock is the slowest single item, not the sum.
const results = await pipeline(
  items,

  // Stage 1 — execute cheap (haiku/sonnet[1m], effort low: the cost levers)
  (item, _orig, i) =>
    agent(execPrompt(item.task, `${BRANCH_PREFIX}-${i}`), {
      label: `exec-${i}:${item.tier}`,
      phase: 'Execute',
      model: item.tier,
      effort: 'low',
      isolation: 'worktree',
      schema: EXEC_SCHEMA,
    }),

  // Stage 2 — escalation ladder:
  //   - confident result (non-null, confidence !== 'low') -> return as-is, no escalation.
  //   - null result -> ONE retry at the same tier (haiku retries at sonnet[1m], since
  //     haiku already failed cheap); if that retry is also null/low, ONE escalation to
  //     the next tier up.
  //   - low-confidence result -> ONE escalation exactly one rung up.
  //   Stop at the first confident attempt. Every superseded attempt's branch is
  //   collected into supersededBranches so the tail can flag it as an orphan.
  async (run, item, i) => {
    if (run && run.confidence !== 'low') return run

    const supersededBranches = []

    async function attempt(tier, suffix, priorSummary) {
      const branch = `${BRANCH_PREFIX}-${i}-${suffix}`
      return agent(
        execPrompt(item.task, branch) +
          `\n\nA cheaper model already attempted this and reported: "${priorSummary}". Start from the task itself on a fresh branch off the same base as mainline — do NOT build on the earlier attempt's branch.`,
        { label: `escalate-${i}-${suffix}`, phase: 'Execute', model: tier, effort: effortFor(tier), isolation: 'worktree', schema: EXEC_SCHEMA },
      )
    }

    let result
    if (!run) {
      const retryTier = item.tier === 'haiku' ? 'sonnet[1m]' : item.tier
      log(`Item ${i} produced no result — retrying at ${retryTier}`)
      result = await attempt(retryTier, 'r2', 'no result (agent failed)')
      if (!result || result.confidence === 'low') {
        if (result && result.branch) supersededBranches.push(result.branch)
        const escTier = nextTier(retryTier)
        log(`Item ${i} still ${result ? 'low confidence' : 'no result'} at ${retryTier} — escalating to ${escTier}`)
        result = await attempt(escTier, 'r3', result ? result.summary : 'no result on retry')
      }
    } else {
      if (run.branch) supersededBranches.push(run.branch)
      const escTier = nextTier(item.tier)
      log(`Item ${i} low confidence — escalating to ${escTier}`)
      result = await attempt(escTier, 'r2', run.summary)
    }

    if (!result) return null
    return { ...result, supersededBranches, escalated: true }
  },

  // Stage 3 — Opus verifies the actual diff, not the executor's self-report
  async (run, item, i) => {
    if (!run) return null
    if (!run.branch) {
      return { task: item.task, ...run, verdict: { approved: false, issues: ['executor reported no branch — nothing to review'] } }
    }
    const verdict = await agent(
      [
        `Review branch ${run.branch} against this task: "${item.task}".`,
        'You are read-only: inspect, never edit files or touch git state.',
        `First check the branch is reviewable: git rev-parse --verify ${run.branch} and git diff --stat $(git merge-base HEAD ${run.branch}) ${run.branch}. If the branch is missing or the diff is empty, return approved: false with issue "no reviewable diff".`,
        `Then inspect the real diff: git diff $(git merge-base HEAD ${run.branch}) ${run.branch}`,
        'Judge correctness and completeness only: is the task actually done, does anything break, was scope respected?',
        `Executor self-report (do not trust it, verify it): ${run.summary} — checks: ${run.checks || 'none reported'}`,
      ].join('\n'),
      { label: `verify-${i}`, phase: 'Verify', model: 'opus[1m]', effort: 'medium', schema: VERDICT_SCHEMA },
    )
    return { task: item.task, ...run, verdict }
  },
)

const done = results.filter(Boolean)
const approved = done.filter(r => r.verdict && r.verdict.approved)
const rejected = done.filter(r => !r.verdict || !r.verdict.approved)
log(`${approved.length} approved, ${rejected.length} rejected, ${items.length - done.length} failed to run`)

// Orphan tracking: every branch that got created (including superseded retries
// and rejected attempts) but wasn't kept as an approved fix is safe to delete.
const created = [...new Set(done.flatMap(r => [r.branch, ...(r.supersededBranches || [])].filter(Boolean)))]
const kept = approved.map(r => r.branch).filter(Boolean)
const orphans = created.filter(b => !kept.includes(b))

return {
  approved: approved.map(r => ({ task: r.task, branch: r.branch, escalated: !!r.escalated })),
  rejected: rejected.map(r => ({ task: r.task, branch: r.branch || null, issues: r.verdict ? r.verdict.issues : ['agent failed, no verdict'] })),
  note: `Approved (merge these from the main session): ${kept.join(', ') || 'none'}. Orphaned (superseded retries or rejected attempts — safe to delete): ${orphans.join(', ') || 'none'}. To clean up an orphan: \`git worktree list\` to find its path, then \`git worktree remove <path>\` (prune does not remove live worktrees).`,
}
