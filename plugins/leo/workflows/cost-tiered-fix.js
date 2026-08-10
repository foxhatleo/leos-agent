export const meta = {
  name: 'cost-tiered-fix',
  description: 'Fix a batch of independent tasks with tiered models: Opus plans and verifies, Haiku/Sonnet execute, low-confidence items escalate to Opus',
  whenToUse: 'A list of independent, well-scoped fixes (many tickets, many files) — NOT one large stateful change, which belongs in a normal session with subagents',
  phases: [
    { title: 'Plan', detail: 'decompose the goal into tiered work items', model: 'opus' },
    { title: 'Execute', detail: 'cheap executors, one isolated worktree per item' },
    { title: 'Verify', detail: 'Opus reviews each branch diff', model: 'opus' },
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
//
// args.tiers optionally remaps the three rungs, e.g.
//   { tiers: { cheap: { model: 'haiku', effort: 'low' } } }
// Workflow scripts have no filesystem access, so the canonical matrix in
// config/models.json cannot be read here — the caller passes it through when
// this machine's mapping differs from the defaults below.

if (!args || (!args.goal && !Array.isArray(args.tasks))) {
  throw new Error('cost-tiered-fix needs args: { goal: "..." } or { tasks: [...] }')
}

const RUN_ID_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$/
if (args.runId !== undefined && (typeof args.runId !== 'string' || !RUN_ID_RE.test(args.runId))) {
  throw new Error('cost-tiered-fix: runId must match ^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$')
}
const BRANCH_PREFIX = args.runId ? `leos/fix-${args.runId}` : 'leos/fix'

// A rung is a model AND an effort, mirroring config/models.json. Escalation has
// to buy more than a model swap: the rung above gets a wider reasoning budget
// as well, or moving up spends more for exactly the deliberation that already
// failed. Keeping them together as one value is what makes that structural
// rather than a rule someone has to remember.
const TIERS = {
  cheap: { model: 'haiku', effort: 'low' },
  normal: { model: 'sonnet', effort: 'medium' },
  judge: { model: 'opus', effort: 'high' },
  ...(args.tiers || {}),
}

// Bare model aliases only — this is the documented subagent `model:` shape.
// The 1m-extended-context suffix (square-bracket /model syntax) is a
// /model-command and SKILL-frontmatter thing, not a valid subagent model
// value; passing it here reaches the model selector verbatim and kills the
// spawn (see the outage this repo had). A caller-supplied args.tiers is the
// one path that can reintroduce it, so it is checked rather than trusted.
const RUNGS = ['cheap', 'normal', 'judge']
const EFFORTS = new Set(['low', 'medium', 'high', 'xhigh', 'max'])
for (const rung of RUNGS) {
  const row = TIERS[rung]
  if (!row || typeof row.model !== 'string' || !row.model) {
    throw new Error(`cost-tiered-fix: tiers.${rung} must be { model, effort }`)
  }
  if (row.model.includes('[')) {
    throw new Error(
      `cost-tiered-fix: tiers.${rung}.model ${JSON.stringify(row.model)} carries a context ` +
        'suffix; subagent model takes a bare alias or a full id, and a suffix kills the spawn',
    )
  }
  if (!EFFORTS.has(row.effort)) {
    throw new Error(`cost-tiered-fix: tiers.${rung}.effort must be one of ${[...EFFORTS].join(', ')}`)
  }
}

const PLAN_SCHEMA = {
  type: 'object',
  properties: {
    items: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          task: { type: 'string', description: 'self-contained instruction: exact file paths, expected behavior, how to check it' },
          tier: { type: 'string', enum: ['cheap', 'normal'], description: 'cheap for mechanical work, normal for implementation needing local judgment' },
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

function isGeneratedBranch(branch, expected) {
  return typeof branch === 'string' && (branch === expected || new RegExp(`^${expected.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}-[1-9]\\d*$`).test(branch))
}

function boundExecutorOutput(untrusted, expectedBranch) {
  // Agent output is untrusted: keep only a bounded, typed report and never
  // let it point verification at an arbitrary refs/heads/* branch.
  if (!untrusted || typeof untrusted !== 'object') return null
  const result = {
    summary: typeof untrusted.summary === 'string' ? untrusted.summary.slice(0, 2000) : 'untrusted executor output omitted summary',
    checks: typeof untrusted.checks === 'string' ? untrusted.checks.slice(0, 2000) : undefined,
    confidence: ['high', 'medium', 'low'].includes(untrusted.confidence) ? untrusted.confidence : 'low',
  }
  if (untrusted.branch !== undefined) {
    if (!isGeneratedBranch(untrusted.branch, expectedBranch)) {
      result.summary = `${result.summary}\n[untrusted executor output: rejected unexpected branch]`.slice(0, 2000)
      result.confidence = 'low'
    } else {
      result.branch = untrusted.branch
    }
  }
  return result
}

function execPrompt(task, branch) {
  return [
    'You are one executor in a fan-out. Work ONLY on this task; touch nothing else.',
    `Task: ${task}`,
    `You are in an isolated git worktree. Create and switch to branch ${branch} (if that name already exists, use the lowest free numeric suffix, e.g. ${branch}-2), implement the task, run the narrowest relevant check (tests/build for the touched files), and commit.`,
    'Report the branch name you actually used. If the task is ambiguous or you cannot make the check pass, commit only what is safe and report confidence: low with the blocker in summary. If you created no branch, omit the branch field entirely — never invent one.',
  ].join('\n')
}

// Next rung up the escalation ladder. The judge rung is the ceiling: it has
// nowhere left to escalate to, so it maps to itself.
function nextTier(rung) {
  if (rung === 'cheap') return 'normal'
  return 'judge'
}

// Caller-supplied args.tasks bypasses the planning agent (and PLAN_SCHEMA's
// validation with it), so entries need their own gate here. Tiers are rung
// names rather than model ids, which is what keeps an untrusted planner string
// from ever reaching `model:` — an unknown rung fails here instead.
const ALLOWED_TIERS = new Set(['cheap', 'normal'])
function validateTask(t, i) {
  if (typeof t === 'string') return { task: t, tier: 'normal' }
  if (t && typeof t === 'object' && typeof t.task === 'string') {
    if (t.tier !== undefined && !ALLOWED_TIERS.has(t.tier)) {
      throw new Error(`cost-tiered-fix: args.tasks[${i}].tier must be one of ${[...ALLOWED_TIERS].join(', ')}, got ${JSON.stringify(t.tier)}`)
    }
    return { tier: 'normal', ...t }
  }
  throw new Error(`cost-tiered-fix: args.tasks[${i}] must be a string or an object with a string "task", got ${JSON.stringify(t)}`)
}

phase('Plan')
let items
if (Array.isArray(args.tasks)) {
  items = args.tasks.map(validateTask)
  log(`Using ${items.length} caller-provided tasks (planning skipped)`)
} else {
  const plan = await agent(
    `Decompose this goal into independent, well-scoped work items that can each be done in an isolated worktree without touching the same files. For each item write a self-contained instruction (exact file paths, expected behavior, how to check it) and pick a tier: "cheap" for mechanical work, "normal" for implementation needing local judgment. At most 10 items — if the goal needs more, return the 10 highest-value and say so in the last item.\n\nGoal: ` + args.goal,
    { label: 'plan', phase: 'Plan', model: TIERS.judge.model, effort: TIERS.judge.effort, schema: PLAN_SCHEMA },
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

  // Stage 1 — execute at the planned rung; model and effort are the cost levers
  (item, _orig, i) =>
    agent(execPrompt(item.task, `${BRANCH_PREFIX}-${i}`), {
      label: `exec-${i}:${item.tier}`,
      phase: 'Execute',
      model: TIERS[item.tier].model,
      effort: TIERS[item.tier].effort,
      isolation: 'worktree',
      schema: EXEC_SCHEMA,
    }).then(result => boundExecutorOutput(result, `${BRANCH_PREFIX}-${i}`)),

  // Stage 2 — escalation ladder:
  //   - confident result (non-null, confidence !== 'low') -> return as-is, no escalation.
  //   - null result -> ONE retry at the same tier (haiku retries at sonnet, since
  //     haiku already failed cheap); if that retry is also null/low, ONE escalation to
  //     the next tier up.
  //   - low-confidence result -> ONE escalation exactly one rung up.
  //   Stop at the first confident attempt. Every superseded attempt's branch is
  //   collected into supersededBranches so the tail can flag it as an orphan.
  async (run, item, i) => {
    if (run && run.confidence !== 'low') return run

    const supersededBranches = []

    async function attempt(rung, suffix, priorSummary) {
      const branch = `${BRANCH_PREFIX}-${i}-${suffix}`
      return agent(
        execPrompt(item.task, branch) +
          `\n\nA cheaper model already attempted this and reported: "${priorSummary}". Start from the task itself on a fresh branch off the same base as mainline — do NOT build on the earlier attempt's branch.`,
        { label: `escalate-${i}-${suffix}`, phase: 'Execute', model: TIERS[rung].model, effort: TIERS[rung].effort, isolation: 'worktree', schema: EXEC_SCHEMA },
      ).then(result => boundExecutorOutput(result, branch))
    }

    let result
    let finalTier
    if (!run) {
      const retryTier = item.tier === 'cheap' ? 'normal' : item.tier
      finalTier = retryTier
      log(`Item ${i} produced no result — retrying at ${retryTier}`)
      result = await attempt(retryTier, 'r2', 'no result (agent failed)')
      if (!result || result.confidence === 'low') {
        if (result && result.branch) supersededBranches.push(result.branch)
        const escTier = nextTier(retryTier)
        finalTier = escTier
        log(`Item ${i} still ${result ? 'low confidence' : 'no result'} at ${retryTier} — escalating to ${escTier}`)
        result = await attempt(escTier, 'r3', result ? result.summary : 'no result on retry')
      }
    } else {
      if (run.branch) supersededBranches.push(run.branch)
      const escTier = nextTier(item.tier)
      finalTier = escTier
      log(`Item ${i} low confidence — escalating to ${escTier}`)
      result = await attempt(escTier, 'r2', run.summary)
    }

    // "escalated" means the work actually moved up a rung. A same-tier retry
    // (a null result at a tier that is already the ceiling) is not one.
    const escalated = finalTier !== item.tier

    if (!result) {
      // Every attempt failed, but earlier attempts may already have created
      // branches. Returning null here would drop supersededBranches and leave
      // those branches out of the orphan report — invisible litter in the repo.
      return { summary: 'every attempt failed; no usable result', confidence: 'low', supersededBranches, escalated }
    }
    return { ...result, supersededBranches, escalated }
  },

  // Stage 3 — Opus verifies the actual diff, not the executor's self-report.
  // Stage 2 always returns an object (never null — see its final `return`s
  // above), so `run` here is never null; no null-guard needed.
  async (run, item, i) => {
    if (!run.branch) {
      return { task: item.task, ...run, verdict: { approved: false, issues: ['executor reported no branch — nothing to review'] } }
    }
    // The rubric arrives as a skill rather than an agent type. A dedicated
    // reviewer agent is not registered on every harness — Claude Code defers
    // that role to its own review skill — so binding this stage to one would
    // make the verify phase die exactly where the role is absent. Loading
    // leo:review-gate gets the same canonical rubric on all four.
    const verdict = await agent(
      [
        `Invoke the leo:review-gate skill and apply its rubric for this review. Its "What a verdict judges" list is the standard; follow it in order.`,
        `Review branch ${run.branch} against this task: "${item.task}".`,
        `Diff scope: git diff $(git merge-base HEAD refs/heads/${run.branch}) refs/heads/${run.branch}`,
        `First check the branch is reviewable: git rev-parse --verify refs/heads/${run.branch} and git diff --stat $(git merge-base HEAD refs/heads/${run.branch}) refs/heads/${run.branch}. If the branch is missing or the diff is empty, return approved: false with issue "no reviewable diff".`,
        `Executor self-report (do not trust it, verify it): ${run.summary} — checks: ${run.checks || 'none reported'}`,
      ].join('\n'),
      { label: `verify-${i}`, phase: 'Verify', model: TIERS.judge.model, effort: TIERS.judge.effort, schema: VERDICT_SCHEMA },
    )
    return { task: item.task, ...run, verdict }
  },
)

const done = results.filter(Boolean)
const approved = done.filter(r => r.verdict && r.verdict.approved)
const rejected = done.filter(r => !r.verdict || !r.verdict.approved)
log(`${approved.length} approved, ${rejected.length} rejected, ${items.length - done.length} failed to run`)

// Orphan tracking: only superseded retries (an earlier attempt's branch that
// got superseded by a later, kept attempt on the SAME item) are safe to
// delete — that work is duplicated by the branch that replaced it. A
// rejected branch is different: it may be the only copy of that item's
// work, just judged not good enough yet, so it is reported separately and
// never described as safe to delete — deleting it on the note's say-so
// would destroy the only copy.
const orphans = [...new Set(done.flatMap(r => r.supersededBranches || []).filter(Boolean))]
const kept = approved.map(r => r.branch).filter(Boolean)

return {
  approved: approved.map(r => ({ task: r.task, branch: r.branch, escalated: !!r.escalated })),
  rejected: rejected.map(r => ({ task: r.task, branch: r.branch || null, issues: r.verdict ? r.verdict.issues : ['agent failed, no verdict'] })),
  orphans,
  note: `Approved (merge these from the main session): ${kept.join(', ') || 'none'}. Orphaned (superseded retries — safe to delete): ${orphans.join(', ') || 'none'}. Rejected branches hold work that failed review but may still be worth salvaging — do NOT delete them without reviewing first: ${rejected.map(r => r.branch).filter(Boolean).join(', ') || 'none'}. To clean up an orphan: \`git worktree list\` to find its path, then \`git worktree remove <path>\` (prune does not remove live worktrees).`,
}
