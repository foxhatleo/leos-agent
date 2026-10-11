# Fresh-context validation — leos-agent

## Reviewer: before staging a blocking finding

A `blocking` finding, and with it a seriously-problematic verdict, gets one
independent check by a reader who did not raise it and has not seen the
argument for it. Non-blocking findings are not sent here; you validate them
yourself (§3).

- Batch: one `leo-lens` dispatch for all blocking findings of this review,
  never one per finding. Standard tier by default, premium only when the
  reviewer is premium and the finding needs it; never cheap. It is a lens, so
  delegation stays one level deep.
- Brief: SHA, BASE, PR number, OWNER/REPO, absolute plugin root, the path to
  this file, and for each finding an id, `path:line` (side) and the claimed
  failure in one sentence. Leave out the lens's reasoning, your confidence and
  other findings, so the check is independent.
- `CONFIRMED`: stage it as blocking. `REFUTED`: drop it, or stage it at a
  lower severity when the evidence shows a smaller real problem; the verdict
  follows what remains. `UNCERTAIN`: stage it as `major`, not blocking, and say
  why in the report.
- If no lens can run (no `leo-lens`, the guard refuses it, or nesting is
  unavailable), re-read each cited line yourself as a fresh pass and say in
  the report that the validation was not independent.
- Record each result in the stage input's optional `validation` list, which
  stage keeps with the verdict, and report it with its evidence line:
  `{"path": "src/a.ts", "line": 42, "result": "CONFIRMED", "independent": true,
  "evidence": "…"}`. `independent` is false when you checked it yourself.

A large review with many non-blocking candidates may still hand them to one
validator lens per batch (cheap tier for factual checks, standard otherwise),
never the lens that raised them.

## Validator lens

Follow `reference/lenses.md` for reading rules: code at SHA only, through
`ghreview.py show` and `extract`, never a working tree; no mutation, no
delegation. For each finding, read the cited lines and enough surrounding
code, callers included, to decide whether the claimed failure happens at SHA
and whether this PR introduced or worsened it. Judge the claim as stated; do
not hunt for new issues.

Reply with this JSON object, then the `Result:` and `Verified:` lines:

```json
{"status":"done", "validations":[{"id":"F1","verdict":"CONFIRMED",
 "evidence":"src/a.ts:42 reads user.id after the null branch returns"}]}
```

`verdict` is `CONFIRMED`, `REFUTED` or `UNCERTAIN`; `evidence` cites
`path:line` at SHA and says what that code does.
