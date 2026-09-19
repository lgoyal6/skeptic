"""
Demonstrate UNLEARNING: a confirmed belief that goes false, and gets retired.

Every "agent memory" demo shows memory growing. That is the easy half. The
hard half -- the half that actually matters if beliefs get compiled into
guards and shipped to other agents (see shim/guards.py, bench/ab.py) -- is
what happens when a confirmed belief stops being true. If skeptic cannot
notice that and walk the belief back, it is not a scientist, it is a diary
that only ever adds pages.

The lab tool makes this checkable end to end: `beliefs.Posterior` is a
beta-binomial over "the documentation is correct", so a CONFIRMED belief
already carries a live posterior that can move in either direction. This
script exploits the lab's `/_control/rules/{id}` switch to make a real,
previously-confirmed belief false, feeds the belief store real observations
of the changed world, and lets `Belief.observe_support_for_doc` do the thing
it already does: auto-falsify once the posterior crosses DOC_TRUST_THRESHOLD,
at which point we call `Belief.retire()` and record why.

Nothing here is simulated. Every round hits the live lab over HTTP through
the same LabAdapter the agent uses; the only intervention is the rule
toggle, which is exactly the knob the lab was built to expose for this.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable

import httpx
import yaml

from agent.adapters.lab import LabAdapter
from agent.beliefs import Belief, BeliefStore, DOC_TRUST_THRESHOLD, Status
from bench.score import match as gt_match

DEFAULT_LAB = "http://127.0.0.1:8077"

# Rules we know how to build a targeted probe for, in the order we prefer
# to demonstrate them (cheapest / most legible first). A rule not in this
# dict is one we could toggle but have no honest way to check, so it is
# never picked as a target even if a belief matches it structurally.
PRIORITY = [
    "include_archived_flag",
    "archived_get_404",
    "pre_epoch_date_null",
    "unknown_field_ignored",
    "page_size_cap",
]

# Each round runs this many independent trials so a single flaky call
# (write_502_after_commit fires ~5% of the time, unrelated to the rule under
# test) cannot flip the round's verdict. page_size_cap needs only one
# measurement per round -- the cap either bites or it doesn't, on one search
# -- so its weight is 1; the rest weight their round by trial count.
TRIALS = 5

# The cap the hidden rule enforces, and how many rows the cap check seeds so
# the cap is observable at all. This is a client of the lab over HTTP, so it
# does not import the server; drift is caught by
# `test_retire_demo_cap_constant_tracks_the_lab` instead. A cap check whose
# idea of the cap has drifted answers from a population that cannot exercise
# the rule, which is the whole bug class this file demonstrates.
PAGE_SIZE_CAP = 50
WANT_ROWS = PAGE_SIZE_CAP + 10
ROUND_WEIGHT = {
    "include_archived_flag": float(TRIALS),
    "archived_get_404": float(TRIALS),
    "pre_epoch_date_null": float(TRIALS),
    "unknown_field_ignored": float(TRIALS),
    "page_size_cap": 1.0,
}


def _retry_429(fn: Callable[[], tuple[int, Any]], retries: int = 6, backoff: float = 1.1) -> tuple[int, Any]:
    """Retry a single adapter call while the lab's own rate limiter (3/s,
    unrelated to whatever rule this probe is measuring) is pushing back. A
    bare retry is useless mid-window, so sleep past RATE_LIMIT_WINDOW_S first."""
    status, body = fn()
    tries = 1
    while status == 429 and tries < retries:
        time.sleep(backoff)
        status, body = fn()
        tries += 1
    return status, body


def _create_item(adapter: LabAdapter, retries: int = 6, **fields: Any) -> dict[str, Any] | None:
    """Create with retries: write_502_after_commit occasionally 502s a call
    whose item was already committed, and rate limiting can 429 a call. Both
    are unrelated to whatever rule this probe is checking, so retry through
    them rather than let them masquerade as a round's verdict."""
    for _ in range(retries):
        status, body = _retry_429(lambda: adapter.create(**fields))
        if status == 200 and isinstance(body, dict) and body.get("id"):
            return body
    return None


def _check_include_archived_flag(adapter: LabAdapter) -> tuple[bool, str]:
    doc_side = 0  # archived item visible without the flag -> matches docs (no hidden filter)
    old_side = 0  # archived item hidden without the flag -> the undocumented rule persists
    vendor = "retire-demo-incarch"
    for i in range(TRIALS):
        body = _create_item(adapter, title=f"retire-incarch-{i}", vendor=vendor)
        if body is None:
            continue
        _retry_429(lambda: adapter.archive(body["id"]))
        # write_search_lag holds new rows out of search for ~2s; wait it out
        # so an unrelated rule cannot masquerade as this one.
        time.sleep(2.1)
        status, resp = _retry_429(lambda: adapter.search(filter={"vendor": vendor}, page_size=50))
        ids = {r.get("id") for r in resp.get("results", [])} if status == 200 else set()
        if body["id"] in ids:
            doc_side += 1
        else:
            old_side += 1
    msg = (f"archived item visible without include_archived flag: {doc_side}/{TRIALS} "
           f"(hidden in {old_side}/{TRIALS})")
    return doc_side > old_side, msg


def _check_archived_get_404(adapter: LabAdapter) -> tuple[bool, str]:
    doc_side = 0  # GET on archived item still 200s -> matches docs
    old_side = 0  # GET on archived item 404s -> the undocumented rule persists
    for i in range(TRIALS):
        body = _create_item(adapter, title=f"retire-archget-{i}")
        if body is None:
            continue
        _retry_429(lambda: adapter.archive(body["id"]))
        status, _resp = _retry_429(lambda: adapter.get(body["id"]))
        if status == 200:
            doc_side += 1
        elif status == 404:
            old_side += 1
    msg = f"GET on archived item: {doc_side}/{TRIALS} returned 200, {old_side}/{TRIALS} returned 404"
    return doc_side > old_side, msg


def _check_pre_epoch_date_null(adapter: LabAdapter) -> tuple[bool, str]:
    doc_side = 0  # due_date stored as sent -> matches docs ("any ISO-8601 date")
    old_side = 0  # due_date silently nulled -> the undocumented rule persists
    sent = "1965-01-01"
    for i in range(TRIALS):
        body = _create_item(adapter, title=f"retire-preepoch-{i}", due_date=sent)
        if body is None:
            continue
        if body.get("due_date") == sent:
            doc_side += 1
        elif body.get("due_date") is None:
            old_side += 1
    msg = f"due_date={sent} stored as sent: {doc_side}/{TRIALS}, silently nulled: {old_side}/{TRIALS}"
    return doc_side > old_side, msg


def _check_unknown_field_ignored(adapter: LabAdapter) -> tuple[bool, str]:
    doc_side = 0  # PATCH with unknown field -> 400 unknown_field, matches docs
    old_side = 0  # PATCH with unknown field -> 200, silently ignored (old rule)
    for i in range(TRIALS):
        body = _create_item(adapter, title=f"retire-unkfield-{i}")
        if body is None:
            continue
        status, _resp = _retry_429(lambda: adapter.update(body["id"], not_a_real_field_xyz="probe"))
        if status == 400:
            doc_side += 1
        elif status == 200:
            old_side += 1
    msg = f"PATCH with unknown field: {doc_side}/{TRIALS} returned 400, {old_side}/{TRIALS} returned 200 (ignored)"
    return doc_side > old_side, msg


def _count_population(adapter: Any, vendor: str, page: int = 25) -> int | None:
    """How many rows this vendor really has, measured without using the cap.

    Asking for a big page to count rows is circular when the cap is exactly
    the thing under test: the answer is bounded by the rule being measured.
    Paging with a size well under the cap and following `next_cursor` gives a
    count that is the same whether the cap is on or off. Returns None if any
    page failed, because a partial count is not a count.
    """
    total, cursor, pages = 0, None, 0
    while pages < 20:
        args: dict[str, Any] = {"filter": {"vendor": vendor}, "page_size": page}
        if cursor:
            args["cursor"] = cursor
        sc, body = _retry_429(lambda: adapter.search(**args))
        if sc != 200 or not isinstance(body, dict):
            return None
        total += len(body.get("results", []))
        pages += 1
        if not body.get("has_more"):
            return total
        cursor = body.get("next_cursor")
        if not cursor:
            return total
    return total


def _check_page_size_cap(adapter: Any) -> tuple[bool | None, str]:
    """Is the world behaving the way the docs promise for page_size?

    Two things went wrong here, and only the first was in the audit.

    It has to seed its own population. Measuring against the whole unfiltered
    store means that right after a lab reset there are fewer rows than the
    cap, every request returns everything, and the check answers from a
    population that could not have exercised the rule either way.

    And it has to answer the question its caller actually asks.
    `check_behaviour` is documented to return `world_matches_docs`; every
    other check returns that. This one returned `still_capped`, which is its
    negation, so the retirement demo ran backwards for this rule: disabling
    the cap made the docs correct, the demo read that as the old lie
    persisting, and the belief could never cross the trust threshold and
    retire -- the exact failure the demo exists to rule out, reached by a
    different route than the unseeded store. Verified live in both
    directions, with the rule on and off, before and after.
    """
    vendor = "RetireProbePage"
    have = _count_population(adapter, vendor) or 0
    while have < WANT_ROWS:
        n = min(20, WANT_ROWS - have)  # bulk silently caps at 20
        _retry_429(lambda: adapter.bulk_create(
            [{"title": f"{vendor}-{have + i}", "vendor": vendor} for i in range(n)]))
        have += n
    time.sleep(2.4)

    # Seeding is a request, not a fact. `bulk_create` can be rate-limited or
    # silently truncated, and incrementing a counter by what was asked for
    # rather than by what committed is the same bug class this file exists to
    # demonstrate. Re-count before answering, and refuse if the population is
    # still too small for either answer to mean anything.
    population = _count_population(adapter, vendor)
    if population is None:
        return None, f"could not count rows for {vendor}; no verdict on the cap"
    if population <= PAGE_SIZE_CAP:
        return None, (
            f"only {population} rows for {vendor}, cap is {PAGE_SIZE_CAP}; "
            f"a request cannot distinguish a cap from a small store, so no verdict"
        )

    sc, body = _retry_429(lambda: adapter.search(filter={"vendor": vendor}, page_size=200))
    if sc != 200 or not isinstance(body, dict):
        return None, f"sweep search returned HTTP {sc}; no verdict on the cap"
    got = len(body.get("results", []))
    still_capped = got <= PAGE_SIZE_CAP and bool(body.get("has_more"))
    return not still_capped, (
        f"asked 200 against {population} rows, got {got}"
        f"{'; cap still in force' if still_capped else '; full page returned, cap gone'}"
    )



def check_behaviour(rule_id: str, adapter: LabAdapter) -> tuple[bool | None, str]:
    """Is the OLD (documented-contradicting) behaviour still present?

    Returns (world_matches_docs, message). world_matches_docs is True when
    the observation looks like what the docs promise -- i.e. what we expect
    once the underlying rule has been switched off.

    None means the round could not observe the rule at all -- the population
    was too small to exercise it, or the calls that would have measured it
    failed. That is not a vote either way, and the caller must not count it
    as one. This is the same rule `reflect/probe.py::_apply` enforces for
    probe verdicts: an experiment with no signal moves nothing.
    """
    return CHECKS[rule_id](adapter)


def pick_target(store: BeliefStore, gt_rules: list[dict[str, Any]]) -> tuple[Belief, str] | None:
    """A CONFIRMED belief that maps (by score.py's own class+op+param logic)
    to a rule we have a targeted probe for, preferring the order in PRIORITY."""
    confirmed = [b for b in store.ordered() if b.status is Status.CONFIRMED]
    by_id = {r["id"]: r for r in gt_rules}
    for rule_id in PRIORITY:
        if rule_id not in CHECKS or rule_id not in by_id:
            continue
        for b in confirmed:
            m = gt_match(b, gt_rules)
            if m is not None and m["id"] == rule_id:
                return b, rule_id
    return None


def _set_rule(lab_base: str, rule_id: str, enabled: bool) -> bool:
    r = httpx.post(f"{lab_base}/_control/rules/{rule_id}", json={"enabled": enabled}, timeout=10.0)
    r.raise_for_status()
    return bool(r.json()["enabled"])


def run_demo(
    tool: str = "lab",
    beliefs_root: str = "beliefs",
    gt_path: str = "lab/ground_truth.yaml",
    lab_base: str = DEFAULT_LAB,
    rounds: int = 5,
) -> dict[str, Any]:
    gt = yaml.safe_load(Path(gt_path).read_text())
    gt_rules = gt["rules"]

    store = BeliefStore(tool, root=beliefs_root)
    target = pick_target(store, gt_rules)
    if target is None:
        return {
            "ok": False,
            "reason": (
                "no CONFIRMED belief maps to a rule we have a targeted check for "
                f"({', '.join(PRIORITY)}); refusing to fabricate a demo"
            ),
        }
    belief, rule_id = target
    weight = ROUND_WEIGHT[rule_id]

    starting = {
        "status": belief.status.value,
        "alpha": round(belief.posterior.alpha, 4),
        "beta": round(belief.posterior.beta, 4),
        "p_doc_correct": round(belief.posterior.p_doc_correct, 4),
    }

    trajectory: list[dict[str, Any]] = [
        {"round": 0, "observation": "(before)", "p_doc_correct": starting["p_doc_correct"],
         "status": starting["status"]}
    ]
    retired = False
    rounds_run = 0
    inconclusive = 0
    run_tag = f"retire-demo-{int(time.time())}"

    rule_was_on = _set_rule(lab_base, rule_id, False)  # noqa: F841 -- returned state is the new (off) state
    try:
        adapter = LabAdapter(run_id=run_tag, base=lab_base, min_interval=0.4)
        try:
            for i in range(1, rounds + 1):
                rounds_run = i
                world_matches_docs, msg = check_behaviour(rule_id, adapter)
                run_id = f"{run_tag}-round-{i}"
                if world_matches_docs is None:
                    # The round observed nothing. Moving the posterior on it
                    # would be inventing evidence, in the one demo whose
                    # entire point is that a belief moves only when the world
                    # actually says something.
                    belief.note("retire_round_uninformative", f"{run_id}: {msg}")
                    inconclusive += 1
                elif world_matches_docs:
                    belief.observe_support_for_doc(run_id, weight=weight)
                else:
                    belief.observe_contradiction(run_id, weight=weight)
                store.save()

                status_now = belief.status.value
                if belief.status is Status.FALSIFIED and not retired:
                    belief.retire(
                        f"world observed to match documentation for rule `{rule_id}` over "
                        f"{i} round(s) after the rule was disabled; posterior crossed the "
                        f"trust threshold ({DOC_TRUST_THRESHOLD})"
                    )
                    store.save()
                    status_now = belief.status.value
                    retired = True

                trajectory.append({
                    "round": i,
                    "observation": msg,
                    "p_doc_correct": round(belief.posterior.p_doc_correct, 4),
                    "status": status_now,
                })
                if retired:
                    break
        finally:
            adapter.close()
    finally:
        restored_enabled = _set_rule(lab_base, rule_id, True)
        state = httpx.get(f"{lab_base}/_control/state", timeout=10.0).json()
        rule_restored = state["enabled"].get(rule_id) is True

    # Re-learnability: with the rule back on, does the old behaviour reappear?
    # This is the honest check that retiring the belief did not corrupt the
    # lab -- the world the agent would see next is the same one it saw before.
    relearn_adapter = LabAdapter(run_id=f"{run_tag}-relearn", base=lab_base, min_interval=0.4)
    try:
        doc_match, relearn_msg = check_behaviour(rule_id, relearn_adapter)
        # check_behaviour reports doc-match; None stays None rather than
        # collapsing to "the old behaviour did not come back".
        old_behaviour_returned = None if doc_match is None else not doc_match
    finally:
        relearn_adapter.close()

    report = {
        "ok": True,
        "belief_id": belief.id,
        "belief_claim": belief.belief,
        "doc_claims": belief.doc_claims,
        "rule_id": rule_id,
        "trust_threshold": DOC_TRUST_THRESHOLD,
        "rounds_allotted": rounds,
        "rounds_run": rounds_run,
        "rounds_inconclusive": inconclusive,
        "round_weight": weight,
        "starting": starting,
        "trajectory": trajectory,
        "retired": retired,
        "final_status": belief.status.value,
        "final_p_doc_correct": round(belief.posterior.p_doc_correct, 4),
        "rule_restored": rule_restored,
        "relearnable": {
            "old_behaviour_reappeared": old_behaviour_returned,
            "observation": relearn_msg,
        },
    }
    return report


def render(report: dict[str, Any]) -> str:
    L: list[str] = []
    L.append("")
    if not report.get("ok"):
        L.append("  belief retirement -- what happens when the world changes")
        L.append("")
        L.append(f"    {report['reason']}")
        L.append("")
        return "\n".join(L)

    L.append("  belief retirement -- what happens when the world changes")
    L.append("")
    L.append(f"    belief : {report['belief_claim']}")
    L.append(f"    rule   : {report['rule_id']}  (turned OFF before round 1)")
    L.append("")

    header = f"    {'round':<7} {'observation':<52} {'p(docs correct)':>16}   {'status'}"
    L.append(header)
    for row in report["trajectory"]:
        obs = row["observation"]
        if len(obs) > 52:
            obs = obs[:49] + "..."
        L.append(f"    {row['round']:<7} {obs:<52} {row['p_doc_correct']:>16.2f}   {row['status']}")
    L.append("")

    if report["retired"]:
        L.append(
            f"    the agent unlearned a belief that had been correct, after "
            f"{report['rounds_run']} round(s) of"
        )
        L.append("    contradicting evidence, without being told the world had changed.")
    else:
        L.append(
            f"    posterior did not cross the trust threshold ({report['trust_threshold']}) "
            f"within {report['rounds_run']} round(s);"
        )
        L.append(
            f"    final status is still `{report['final_status']}` at "
            f"p(docs correct)={report['final_p_doc_correct']:.2f}. reported honestly, not extended."
        )
    L.append("")

    relearn = report["relearnable"]
    tag = "yes" if relearn["old_behaviour_reappeared"] else "no"
    L.append(f"    rule restored to ON : {report['rule_restored']}")
    L.append(f"    re-learnable        : {tag}  ({relearn['observation']})")
    L.append("")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tool", default="lab")
    ap.add_argument("--beliefs", default="beliefs")
    ap.add_argument("--gt", default="lab/ground_truth.yaml")
    ap.add_argument("--lab", default=DEFAULT_LAB)
    ap.add_argument("--rounds", type=int, default=5)
    ap.add_argument("--out", default="bench/retire_result.json")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    report = run_demo(a.tool, a.beliefs, a.gt, a.lab, a.rounds)

    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(report, indent=2))

    if a.json:
        print(json.dumps(report, indent=2))
    else:
        print(render(report))
        print(f"  written to {a.out}\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
