#!/usr/bin/env python3
"""bench: a paired, significance-tested cost benchmark on the eval suite.

`claude plugin eval --ablation with-without` runs every case with the plugin
and without it. This pairs run i of one arm with run i of the other, reads each
run's own usage record (the CLI's list-price cost and per-model tokens), and
tests the paired differences with an exact Wilcoxon signed-rank test. The
report never states a direction without its p-value and n.

    bench.py run --runs 3 --max-cost-usd 8 [--case GLOB | --tag TAG] [--ledger FILE --budget-usd N]
    bench.py report <result.json>... [--json]

`run` is a paid model call per agent run. `report` and the statistics are free.
Results go under evals/results/, which git ignores.
"""
import argparse
import datetime
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "evals" / "results"
ARMS = ("with", "without")
# Settings a run inherits from the shell that change which model answers.
MODEL_OVERRIDES = ("CLAUDE_CODE_SUBAGENT_MODEL", "ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
                   "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_SMALL_FAST_MODEL")
# Per-run metrics, in report order. Token metrics need the run's usage record.
METRICS = ("cost_usd", "turns", "cache_read", "new_input", "output")
TOKEN_FIELDS = (("input", "inputTokens"), ("cache_write", "cacheCreationInputTokens"),
                ("cache_read", "cacheReadInputTokens"), ("output", "outputTokens"))
# A directory the eval CLI kept with --keep-temp: <tmp>/e-XXXXXX/out/trace.jsonl.
KEPT_DIR = re.compile(r"e-[A-Za-z0-9]{6}")


# ---- statistics (stdlib, exact) ----------------------------------------------

def median(values):
    values = sorted(values)
    if not values:
        return None
    mid = len(values) // 2
    return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2


def average_ranks(values):
    """1-based ranks of values, ties sharing their average rank."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def signed_rank_distribution(doubled_ranks):
    """counts[s]: how many of the 2^n sign assignments give doubled W+ == s."""
    counts = [1] + [0] * sum(doubled_ranks)
    top = 0
    for rank in doubled_ranks:
        top += rank
        for s in range(top, rank - 1, -1):
            counts[s] += counts[s - rank]
    return counts


def wilcoxon(deltas):
    """Exact two-sided Wilcoxon signed-rank test of median(deltas) == 0.

    Zero differences are dropped (Wilcoxon's convention); tied magnitudes take
    average ranks, and the null distribution is enumerated for those ranks, so
    the p-value is exact for any n and any ties.
    """
    nonzero = [d for d in deltas if d != 0]
    n = len(nonzero)
    result = {"n": len(deltas), "n_nonzero": n, "w_plus": 0.0, "w_minus": 0.0, "p": 1.0, "rank_biserial": None}
    if not n:
        return result
    ranks = average_ranks([abs(d) for d in nonzero])
    w_plus = sum(r for r, d in zip(ranks, nonzero) if d > 0)
    w_minus = sum(ranks) - w_plus
    doubled = [int(round(2 * r)) for r in ranks]
    counts = signed_rank_distribution(doubled)
    total = 2 ** n
    observed = int(round(2 * w_plus))
    lower = sum(counts[:observed + 1]) / total
    upper = sum(counts[observed:]) / total
    result.update(w_plus=w_plus, w_minus=w_minus, p=min(1.0, 2 * min(lower, upper)),
                  rank_biserial=(w_plus - w_minus) / (w_plus + w_minus))
    return result


def hodges_lehmann(deltas, confidence=0.95):
    """Hodges-Lehmann estimate of the median difference with its exact
    distribution-free confidence interval (Walsh averages), or interval None
    when n is too small to reach the confidence at all."""
    n = len(deltas)
    if not n:
        return {"estimate": None, "low": None, "high": None, "confidence": None}
    walsh = sorted((deltas[i] + deltas[j]) / 2 for i in range(n) for j in range(i, n))
    counts = signed_rank_distribution(list(range(2, 2 * n + 1, 2)))  # untied ranks, doubled
    total = 2 ** n
    alpha = 1 - confidence
    # k: the largest count with P(W+ <= k - 1) <= alpha / 2.
    k, tail = 0, 0.0
    while True:
        nxt = tail + counts[2 * k] / total
        if nxt > alpha / 2:
            break
        tail, k = nxt, k + 1
    out = {"estimate": median(walsh), "low": None, "high": None, "confidence": None}
    if k >= 1:
        out.update(low=walsh[k - 1], high=walsh[len(walsh) - k], confidence=1 - 2 * tail)
    return out


def sign_test(wins, losses):
    """Exact two-sided binomial test on discordant pairs."""
    n = wins + losses
    if not n:
        return 1.0
    low = min(wins, losses)
    tail = sum(math.comb(n, i) for i in range(low + 1)) / 2 ** n
    return min(1.0, 2 * tail)


# ---- reading the eval CLI's output ------------------------------------------

def usage_from_trace(path):
    """The run's final `result` record, reduced to counts: per-model tokens
    and list-price cost, turns, and children started. None if absent."""
    record = None
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if isinstance(item, dict) and item.get("type") == "result":
                    record = item
    except OSError:
        return None
    if record is None:
        return None
    models = {}
    for model, usage in (record.get("modelUsage") or {}).items():
        if not isinstance(usage, dict):
            continue
        entry = {key: number(usage.get(field)) for key, field in TOKEN_FIELDS}
        entry["cost_usd"] = number(usage.get("costUSD"), float)
        models[str(model)] = entry
    children = (record.get("subagent_stats") or {}).get("spawned")
    return {"models": models, "num_turns": number(record.get("num_turns")),
            "children": number(children), "total_cost_usd": number(record.get("total_cost_usd"), float)}


def number(value, kind=int):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    return kind(value)


def run_record(raw, usage):
    """One arm's run as the report uses it."""
    scored = [g for g in raw.get("graders", []) if g.get("scored")]
    record = {
        "cost_usd": number(raw.get("costUsd"), float),
        "turns": number(raw.get("turns")),
        "correct": bool(scored) and all(g.get("passed") is True for g in scored),
        "error": raw.get("error") is not None,
        "indicators": {g["name"]: g.get("passed") is True for g in raw.get("graders", [])
                       if g.get("withOnly") and isinstance(g.get("name"), str)},
        "cache_read": None, "new_input": None, "output": None, "models": {}, "children": None,
    }
    if usage:
        models = usage["models"]
        if models and all(m[k] is not None for m in models.values() for k, _ in TOKEN_FIELDS):
            record["cache_read"] = sum(m["cache_read"] for m in models.values())
            record["new_input"] = sum(m["input"] + m["cache_write"] for m in models.values())
            record["output"] = sum(m["output"] for m in models.values())
        record["models"] = {name: m["cost_usd"] for name, m in models.items()}
        record["children"] = usage["children"]
    return record


def load_pairs(results, usage=None):
    """Pairs (case, index, with_run, without_run) from eval --json results.

    Run i of one arm pairs with run i of the other in the same case and result
    file; a run without its partner (a cost ceiling hit mid-case) is dropped
    and counted.
    """
    usage = usage or {}
    pairs, unpaired = [], 0
    for result in results:
        for case in result.get("cases", []):
            arms = case.get("arms") or {}
            runs = [arms.get(arm) or [] for arm in ARMS]
            unpaired += abs(len(runs[0]) - len(runs[1]))
            for index, (with_raw, without_raw) in enumerate(zip(*runs)):
                pairs.append({
                    "case": case.get("name"), "index": index,
                    "with": run_record(with_raw, usage.get(with_raw.get("tracePath"))),
                    "without": run_record(without_raw, usage.get(without_raw.get("tracePath"))),
                })
    return pairs, unpaired


# ---- the report ---------------------------------------------------------------

def compare(pairs, metric, confidence):
    usable = [p for p in pairs if not (p["with"]["error"] or p["without"]["error"])
              and p["with"][metric] is not None and p["without"][metric] is not None]
    deltas = [p["with"][metric] - p["without"][metric] for p in usable]
    out = {"metric": metric, "n": len(usable),
           "with_median": median([p["with"][metric] for p in usable]),
           "without_median": median([p["without"][metric] for p in usable]),
           "median_delta": median(deltas)}
    out["wilcoxon"] = wilcoxon(deltas)
    out["hodges_lehmann"] = hodges_lehmann(deltas, confidence)
    return out


def summarize(pairs, unpaired=0, alpha=0.05):
    confidence = 1 - alpha
    errored = sum(1 for p in pairs if p["with"]["error"] or p["without"]["error"])
    with_ok = sum(p["with"]["correct"] for p in pairs)
    without_ok = sum(p["without"]["correct"] for p in pairs)
    wins = sum(p["with"]["correct"] and not p["without"]["correct"] for p in pairs)
    losses = sum(p["without"]["correct"] and not p["with"]["correct"] for p in pairs)
    cases = {}
    for p in pairs:
        cases.setdefault(p["case"], []).append(p)
    return {
        "pairs": len(pairs), "unpaired_runs": unpaired, "errored_pairs": errored, "alpha": alpha,
        "metrics": [compare(pairs, metric, confidence) for metric in METRICS],
        "correctness": {"with": with_ok, "without": without_ok, "with_only_correct": wins,
                        "without_only_correct": losses, "p": sign_test(wins, losses)},
        "cases": [{"case": name, "n": len(items),
                   "with_cost_median": median([i["with"]["cost_usd"] for i in items if i["with"]["cost_usd"] is not None]),
                   "without_cost_median": median([i["without"]["cost_usd"] for i in items
                                                  if i["without"]["cost_usd"] is not None]),
                   "with_correct": sum(i["with"]["correct"] for i in items),
                   "without_correct": sum(i["without"]["correct"] for i in items),
                   "with_children": sum(i["with"]["children"] or 0 for i in items)}
                  for name, items in cases.items()],
    }


def smallest_p(n):
    """The smallest two-sided p an exact signed-rank test can give at n."""
    return min(1.0, 2 / 2 ** n) if n else 1.0


def verdict(summary):
    """Plain sentences; every directional claim carries its p and n."""
    alpha = summary["alpha"]
    cost = summary["metrics"][0]
    test, hl = cost["wilcoxon"], cost["hodges_lehmann"]
    n = cost["n"]
    lines = []
    if n == 0:
        lines.append("No usable pairs: no cost comparison.")
    elif smallest_p(test["n_nonzero"]) >= alpha:
        lines.append(f"Too few pairs to test: n={n} cannot reach p<{alpha:g}; no cost claim.")
    elif test["p"] < alpha and hl["estimate"] is not None and hl["estimate"] != 0:
        direction = "lower" if hl["estimate"] < 0 else "higher"
        lines.append(f"Cost per run was {direction} with the plugin on these cases: "
                     f"Hodges-Lehmann {fmt_usd(hl['estimate'])} per run{fmt_ci(hl, fmt_usd)} "
                     f"(Wilcoxon p={test['p']:.3g}, n={n}).")
    else:
        lines.append(f"No significant cost difference at alpha={alpha:g} "
                     f"(Wilcoxon p={test['p']:.3g}, n={n}); this does not show the costs are equal.")
    c = summary["correctness"]
    total = summary["pairs"]
    if c["p"] < alpha and c["without_only_correct"] > c["with_only_correct"]:
        lines.append(f"Correctness regressed with the plugin: {c['with']}/{total} vs {c['without']}/{total} "
                     f"(sign test p={c['p']:.3g}, n={total}).")
    else:
        lines.append(f"Correct answers: with {c['with']}/{total}, without {c['without']}/{total} "
                     f"(sign test on {c['with_only_correct'] + c['without_only_correct']} discordant pairs "
                     f"p={c['p']:.3g}).")
    if summary["errored_pairs"] or summary["unpaired_runs"]:
        lines.append(f"Excluded from metric tests: {summary['errored_pairs']} pair(s) with an errored run; "
                     f"{summary['unpaired_runs']} run(s) without a partner.")
    lines.append("Costs are the CLI's list-price estimates for these cases and pinned models, not bills; "
                 "they do not establish savings in general.")
    return lines


def fmt_usd(value):
    return "n/a" if value is None else f"{value:+.4f} USD" if value else "0 USD"


def fmt_num(value):
    if value is None:
        return "n/a"
    if isinstance(value, float) and not value.is_integer() and abs(value) < 10:
        return f"{value:.4f}"
    return f"{value:,.0f}"


def fmt_ci(hl, fmt):
    if hl["low"] is None:
        return " [no interval at this n]"
    return f" [{hl['confidence']:.1%} CI {fmt(hl['low'])} to {fmt(hl['high'])}]"


def render(summary):
    out = [f"Paired benchmark: {summary['pairs']} pairs (with plugin vs without), exact Wilcoxon signed-rank",
           ""]
    head = f"{'metric':<10} {'n':>3} {'with med':>12} {'w/out med':>12} {'HL delta':>12} {'CI':>27} {'p':>7} {'r':>6}"
    out.append(head)
    for m in summary["metrics"]:
        hl, test = m["hodges_lehmann"], m["wilcoxon"]
        ci = "n/a" if hl["low"] is None else f"{fmt_num(hl['low'])} to {fmt_num(hl['high'])}"
        r = "n/a" if test["rank_biserial"] is None else f"{test['rank_biserial']:+.2f}"
        out.append(f"{m['metric']:<10} {m['n']:>3} {fmt_num(m['with_median']):>12} {fmt_num(m['without_median']):>12} "
                   f"{fmt_num(hl['estimate']):>12} {ci:>27} {test['p']:>7.3g} {r:>6}")
    out.append("")
    out.append(f"{'case':<32} {'n':>3} {'with cost':>10} {'w/out cost':>10} {'correct':>9} {'children':>8}")
    for c in summary["cases"]:
        out.append(f"{c['case']:<32} {c['n']:>3} {fmt_num(c['with_cost_median']):>10} "
                   f"{fmt_num(c['without_cost_median']):>10} {c['with_correct']:>4}/{c['without_correct']:<4} "
                   f"{c['with_children']:>8}")
    out.append("")
    out.extend(verdict(summary))
    return "\n".join(out)


def report(results, usage=None, alpha=0.05):
    pairs, unpaired = load_pairs(results, usage)
    summary = summarize(pairs, unpaired, alpha)
    summary["verdict"] = verdict(summary)
    return summary


# ---- running it ---------------------------------------------------------------

def eval_command(args, json_path):
    cmd = ["claude", "plugin", "eval", str(ROOT), "--scaffold", "--no-publish", "--trust-plugin",
           "--ablation", "with-without", "--runs", str(args.runs), "--json", str(json_path),
           "--output-dir", str(json_path.parent), "--max-cost-usd", f"{args.max_cost_usd:g}",
           "--threshold", "0", "--keep-temp"]
    if args.case:
        cmd += ["--case", args.case]
    for tag in args.tag or []:
        cmd += ["--tag", tag]
    if args.concurrency:
        cmd += ["--concurrency", str(args.concurrency)]
    return cmd


def ledger_total(path):
    """Sum of the cost column of a ledger: `time | command | cost | ...` lines."""
    total = 0.0
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return 0.0
    for line in lines:
        parts = [p.strip() for p in line.split("|")]
        if line.startswith("#") or len(parts) < 3:
            continue
        try:
            total += float(parts[2])
        except ValueError:
            continue
    return total


def budget_refusal(args):
    """Why a run must not start, or None. The ceiling is checked before each
    agent run launches, so a run can overshoot by the runs already in flight."""
    if args.budget_usd is None:
        return None
    if not args.ledger:
        return "--budget-usd needs --ledger to know what was already spent"
    spent = ledger_total(args.ledger)
    in_flight = (args.concurrency or 1) * args.in_flight_usd
    if spent + args.max_cost_usd + in_flight > args.budget_usd:
        return (f"refusing: spent {spent:.4f} + ceiling {args.max_cost_usd:g} + in-flight allowance "
                f"{in_flight:g} exceeds the budget {args.budget_usd:g} USD")
    return None


def remove_kept(trace_path):
    """Delete one directory --keep-temp left behind, and nothing else."""
    trace = Path(trace_path)
    kept = trace.parent.parent
    if trace.name != "trace.jsonl" or trace.parent.name != "out" or not KEPT_DIR.fullmatch(kept.name) \
            or kept.is_symlink() or not kept.is_dir():
        return False
    # The CLI seals what the plugin wrote (mode 000) and makes the rest
    # read-only; open each directory before descending into it.
    os.chmod(kept, stat.S_IRWXU)
    for dirpath, dirnames, _ in os.walk(kept):
        for name in dirnames:
            path = os.path.join(dirpath, name)
            if not os.path.islink(path):
                os.chmod(path, stat.S_IRWXU)
    shutil.rmtree(kept)
    return True


def cmd_run(args):
    refusal = budget_refusal(args)
    if refusal:
        print(refusal, file=sys.stderr)
        return 2
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    out_dir = Path(args.out) if args.out else RESULTS / f"bench-{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "result.json"
    cmd = eval_command(args, json_path)
    env = {k: v for k, v in os.environ.items() if k not in MODEL_OVERRIDES}
    print(" ".join(cmd), file=sys.stderr)
    code = subprocess.run(cmd, cwd=str(ROOT), env=env).returncode
    if not json_path.is_file():
        print(f"no result written (exit {code})", file=sys.stderr)
        return code or 1
    result = json.loads(json_path.read_text(encoding="utf-8"))
    usage = {}
    for case in result.get("cases", []):
        for arm in ARMS:
            for run in (case.get("arms") or {}).get(arm) or []:
                trace = run.get("tracePath")
                if isinstance(trace, str):
                    usage[trace] = usage_from_trace(trace)
                    if not args.keep_traces:
                        remove_kept(trace)
    (out_dir / "usage.json").write_text(json.dumps(usage, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    cost = result.get("costUsd")
    if args.ledger:
        with open(args.ledger, "a", encoding="utf-8") as handle:
            handle.write(f"{stamp} | {' '.join(sys.argv)} | {cost if cost is not None else 'unknown'} | "
                         f"{out_dir}\n")
    summary = report([result], usage, args.alpha)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=1) + "\n", encoding="utf-8")
    print(render(summary))
    return 0 if code in (0, 1) else code


def cmd_report(args):
    results, usage = [], {}
    for name in args.results:
        path = Path(name)
        if path.is_dir():
            path = path / "result.json"
        results.append(json.loads(path.read_text(encoding="utf-8")))
        sidecar = path.parent / "usage.json"
        if sidecar.is_file():
            usage.update(json.loads(sidecar.read_text(encoding="utf-8")))
    summary = report(results, usage, args.alpha)
    print(json.dumps(summary, indent=1) if args.json else render(summary))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="paid: run the suite in both arms and report")
    run.add_argument("--runs", type=int, default=3)
    run.add_argument("--case", help="case name glob, passed to --case")
    run.add_argument("--tag", action="append", help="case tag, passed to --tag")
    run.add_argument("--max-cost-usd", type=float, required=True, help="the CLI's own ceiling for this run")
    run.add_argument("--concurrency", type=int)
    run.add_argument("--out", help="output directory (default evals/results/bench-<time>)")
    run.add_argument("--ledger", help="append one line per run: time | command | cost | output")
    run.add_argument("--budget-usd", type=float, help="refuse to start past this total, ledger included")
    run.add_argument("--in-flight-usd", type=float, default=0.5,
                     help="allowance per concurrent run for overshooting the ceiling (default 0.5)")
    run.add_argument("--keep-traces", action="store_true", help="keep the run directories --keep-temp left")
    run.add_argument("--alpha", type=float, default=0.05)
    rep = sub.add_parser("report", help="free: report on existing results")
    rep.add_argument("results", nargs="+", help="result.json files or bench output directories")
    rep.add_argument("--json", action="store_true")
    rep.add_argument("--alpha", type=float, default=0.05)
    args = parser.parse_args(argv)
    if args.command == "run" and (args.runs < 1 or args.max_cost_usd <= 0):
        parser.error("--runs must be at least 1 and --max-cost-usd positive")
    return cmd_run(args) if args.command == "run" else cmd_report(args)


if __name__ == "__main__":
    sys.exit(main())
