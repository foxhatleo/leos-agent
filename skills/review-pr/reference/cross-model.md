# Cross-model review lens — leos-agent

An optional adversarial lens run by a second provider's model, through that
provider's own CLI. It is off by default and costs nothing until used.

## When

Only when the user enabled it: for this review (your brief says
`cross-model: on`) or on this machine (`review_peer.py config --enable`, which
`detect` reports as `"enabled_by": "machine setting"`). With the machine
setting alone, use it only for a consequential PR (auth, security, data
migration, money, or wide blast radius). It never replaces a lens or any
coverage; it is one more set of candidates.

Enabling it is consent to send the pinned PR diff, and any requirements
summary you pass with `--context`, to that provider, billed to that CLI's
account. Say so in the report every time it runs.

## Detect, then run once

```
python3 "<plugin-root>/scripts/review_peer.py" detect [--user-enabled]
```

It sends nothing. It attests the host from its environment (Claude Code sets
`CLAUDECODE=1`, Codex sets `CODEX_THREAD_ID`), picks the other family (a Claude
host uses `codex`, a Codex host uses `claude`), and checks that the CLI is
installed and that its own status command reports a login. Anything else is a
skip with a `reason`: report "cross-model lens: not run — <reason>" and go on.
Pass `--user-enabled` only when the brief says the user enabled it for this
review. A `note` about a Codex sandbox without network means the run needs the
user's approved escalation; ask, never bypass.

When `status` is `ready`, start your own lenses first, then run it once in the
foreground with a 10-minute Bash timeout:

```
python3 "<plugin-root>/scripts/review_peer.py" run -R OWNER/REPO -n N --commit SHA \
  --out /private/scratch/peer.json [--context requirements.txt] [--user-enabled]
```

It re-checks the head, sends only the non-generated patches that fit its input
cap (the rest are listed under `not_sent`), in a fenced untrusted block, to a
read-only peer in an empty directory: `codex exec` with a read-only sandbox and
the user's config and rules ignored, or `claude -p` with no tools, safe mode
and a spending cap. It applies a time limit, never retries, and never switches
route. The artifact's `status` is `done`, `skipped` or `failed`, with a
`reason`; a failure is reported, not retried.

## Fold in

The artifact's `findings` are lens candidates in the usual shape. Each goes
through §3 of the procedure like any other: re-read at SHA, confirm the failure
path, confirm the PR introduced it. The peer saw only the diff, so expect more
false positives. Its paths never count as coverage, and it can never be the
only reason for a seriously-problematic verdict without your own verification.

Agreement adds confidence only when `independence_verified` is true: the host
family was attested and the model the peer's CLI reported is of the other
family. Then a finding both you (or a lens) and the peer raised independently
may gain up to 10 points. Otherwise peer findings are evidence with no bonus.

In the report, give the route, `model_requested` and the reported
`model_actual` (or "unreported"), `independence_verified`, `cost_usd` when the
CLI reported it, `bytes_sent`, any `not_sent` files, and that the PR diff was
sent to that provider.
