#!/usr/bin/env python3
"""Write the large-corpus-audit fixture: 120 incident write-ups and an index.

Deterministic and offline: a fixed seed, the standard library only. Every
company, product and person is made up. Usage: generate.py OUTDIR
"""
import hashlib
import random
import sys
from pathlib import Path

SEED = 20260713
COUNT = 120
LABEL = "Customer-facing outages caused by an expired TLS certificate"

SERVICES = ["Quillfern ledger", "Brindlewick search", "Oakhollow relay", "Tamsin Ridge scheduler",
            "Marrowgate billing portal", "Pellucid Harbor uploads", "Dunmore Fold notifications",
            "Sablewood identity", "Kestrel Lane exports", "Hobbledown analytics"]
TEAMS = ["Platform Reliability", "Edge Networking", "Data Services", "Customer Surface", "Core Runtime"]
PEOPLE = ["Ines Varga", "Tobias Renk", "Mara Oyelaran", "Pieter Lind", "Soledad Wray", "Hamid Corvo",
          "Noor Falkenrath", "Dag Ellison", "Yuki Tamarind", "Bram Oduya"]

# The one sentence per fact. A write-up states each fact in exactly one of these.
CAUSE = {
    "cert": ("The confirmed root cause was an expired TLS certificate on the public endpoint.",
             "The confirmed root cause was a TLS certificate that lapsed without being renewed.",
             "The confirmed root cause was the expiry of the TLS certificate fronting the service."),
    "config": ("The confirmed root cause was a configuration push that lowered the connection-pool limit.",
               "The confirmed root cause was a feature flag enabled in every region at once.",
               "The confirmed root cause was a malformed routing rule shipped with a routine release."),
    "capacity": ("The confirmed root cause was a disk volume that filled during a log burst.",
                 "The confirmed root cause was a worker pool that ran out of memory under load.",
                 "The confirmed root cause was a queue that grew faster than its consumers could drain it."),
    "dependency": ("The confirmed root cause was an upstream vendor API returning errors for two hours.",
                   "The confirmed root cause was a DNS provider fault outside our control.",
                   "The confirmed root cause was a dependency upgrade that changed a default timeout."),
}
# Traps: the certificate is mentioned, but it was not the confirmed cause.
THEORY = (
    "An early theory blamed an expired TLS certificate, but the certificate was valid throughout and the theory was dropped.",
    "Someone suspected the TLS certificate had expired; inspection showed it had been renewed weeks earlier.",
    "The on-call engineer first checked certificate expiry dates and found every certificate in date.",
)
IMPACT = {
    True: ("Customers could not reach the service and support received reports during the outage.",
           "End users saw failed requests for the duration and several enterprise accounts raised tickets.",
           "The outage was visible to customers, who saw errors and timeouts until recovery."),
    False: ("No customers were affected; the fault was caught on an internal environment before release.",
            "Customer traffic was unaffected because the failure was contained to a staging system.",
            "There was no customer impact; the issue surfaced in a pre-production check."),
}
NEAR_MISS = (
    "A certificate renewal reminder fired during the same week and was handled as routine maintenance.",
    "The change calendar noted a planned certificate rotation for a different service that month.",
    "An unrelated audit item about certificate inventory was filed separately and left open.",
)
FILLER = [
    "The on-call engineer acknowledged the page within a few minutes and opened a shared channel.",
    "Dashboards for request rate and error rate were pinned to the incident room for the duration.",
    "A second engineer joined to take notes so the first could keep working the problem.",
    "The status page was updated twice while the team narrowed down where the fault sat.",
    "Several unrelated alerts fired in the same window and were set aside after a quick check.",
    "The team compared the current graphs with the same hour the week before to look for a shape change.",
    "A rollback was considered, weighed against the risk of losing in-flight work, and handled in a follow-up.",
    "Handover notes were written for the next shift so nobody had to reconstruct the order of events.",
    "The incident commander asked for hypotheses to be written down before anyone acted on them.",
    "Logs from the affected hosts were copied to the shared drive for later review.",
    "A short call with the vendor account manager confirmed there was nothing further to report on their side.",
    "The team agreed to review alert thresholds for this service at the next planning session.",
    "Runbook steps were followed in order, and two of them were found to be out of date.",
    "A reviewer asked for the timeline to be rewritten in plain language for the wider company.",
    "The follow-up list was triaged on the Monday after, with owners and rough dates assigned.",
    "Capacity headroom for the next quarter was discussed but no decision was taken in this review.",
    "The support team prepared a short customer note and kept it in reserve until the facts were settled.",
    "Both the primary and the secondary on-call were reachable throughout, which kept coordination simple.",
]


def facts():
    """The ground truth per incident, in ID order: id, cause, customer_facing, theory, near_miss."""
    rng = random.Random(SEED)
    # A fixed share of incidents: confirmed cert cause with and without impact,
    # a cert theory that was dropped, and a near-miss mention. The rest vary.
    kinds = (["cert-hit"] * 11 + ["cert-quiet"] * 7 + ["theory"] * 14 + ["near"] * 12)
    kinds += ["plain"] * (COUNT - len(kinds))
    order = list(range(COUNT))
    for i in range(COUNT - 1, 0, -1):
        j = rng.randrange(i + 1)
        order[i], order[j] = order[j], order[i]
    out = []
    for slot in range(COUNT):
        kind = kinds[order[slot]]
        cause = "cert" if kind.startswith("cert") else ["config", "capacity", "dependency"][rng.randrange(3)]
        if kind == "cert-hit":
            facing = True
        elif kind == "cert-quiet":
            facing = False
        else:
            facing = rng.randrange(3) != 0
        out.append({"id": "INC-%04d" % (4001 + slot), "cause": cause, "customer_facing": facing,
                    "theory": kind == "theory", "near_miss": kind == "near", "pick": rng.randrange(3)})
    return out


def answer_ids():
    return [f["id"] for f in facts() if f["cause"] == "cert" and f["customer_facing"]]


def paragraphs(rng, count):
    out = []
    for _ in range(count):
        out.append(" ".join(FILLER[rng.randrange(len(FILLER))] for _ in range(3)))
    return out


def write_up(fact, rng):
    service = SERVICES[rng.randrange(len(SERVICES))]
    team = TEAMS[rng.randrange(len(TEAMS))]
    owner = PEOPLE[rng.randrange(len(PEOPLE))]
    pick = fact["pick"]
    day = 1 + rng.randrange(28)
    month = 1 + rng.randrange(12)
    minutes = 8 + rng.randrange(170)
    lines = ["# %s: %s incident" % (fact["id"], service), "",
             "Service: %s" % service, "Owning team: %s" % team, "Incident commander: %s" % owner,
             "Date: 2025-%02d-%02d" % (month, day), "Duration: %d minutes" % minutes, ""]
    lines += ["## Summary", "", paragraphs(rng, 2)[0], ""]
    timeline = []
    for step in range(8):
        timeline.append("- T+%03d: %s" % (step * 9 + rng.randrange(8), FILLER[rng.randrange(len(FILLER))]))
    lines += ["## Timeline", ""] + timeline + [""]
    investigation = paragraphs(rng, 2)
    if fact["theory"]:
        investigation.insert(1 + rng.randrange(2), THEORY[pick])
    if fact["near_miss"]:
        investigation.insert(1 + rng.randrange(2), NEAR_MISS[pick])
    lines += ["## Investigation", ""]
    for text in investigation:
        lines += [text, ""]
    lines += ["## Root cause", "", CAUSE[fact["cause"]][pick], paragraphs(rng, 1)[0], ""]
    lines += ["## Customer impact", "", IMPACT[fact["customer_facing"]][pick], paragraphs(rng, 1)[0], ""]
    lines += ["## Follow-ups", ""]
    for _ in range(4):
        lines.append("- %s (owner: %s)" % (FILLER[rng.randrange(len(FILLER))], PEOPLE[rng.randrange(len(PEOPLE))]))
    lines += ["", "## Lessons", ""] + paragraphs(rng, 2)
    return "\n".join(lines).rstrip() + "\n"


def generate(outdir):
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(SEED + 1)
    items = facts()
    for fact in items:
        (out / (fact["id"] + ".md")).write_text(write_up(fact, rng), encoding="utf-8")
    index = ["# Incident write-ups, 2025", "", "Write-ups: %d" % COUNT,
             "%s: %d" % (LABEL, len(answer_ids())), "",
             "Each file is named by its incident ID.", ""]
    (out / "INDEX.md").write_text("\n".join(index), encoding="utf-8")


def digest(outdir):
    h = hashlib.sha256()
    for p in sorted(Path(outdir).rglob("*")):
        if p.is_file():
            h.update(p.name.encode() + b"\0" + p.read_bytes())
    return h.hexdigest()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: generate.py OUTDIR")
    generate(sys.argv[1])
