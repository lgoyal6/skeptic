"""
The regression gate: replay, compile, enforce, and fail on anything that slid back.

    ./.venv/bin/python -m bench.ci

Runs offline, with no credentials and no model calls, and is the single
command CI executes on every change. Five stages, each of which can fail the
build:

1. **Replay** every fixture and check it against its manifest and hashes.
2. **Compile** a guard for every confirmed mismatch, and report any confirmed
   mismatch with no guard behind it.
3. **Positive cases**: run every guard over the recordings where the tool
   behaved. A guard that fires here is worse than no guard -- it makes the
   honest case look broken, and the next person turns it off.
4. **Recorded violations**: run every guard over the exchange that established
   its mismatch. A guard that does not fire on the evidence that created it
   is not enforcing anything.
5. **Planted violations**: run every guard over a synthetic response built to
   break it, so a guard whose recorded case happens to be easy is still
   exercised.

Coverage is reported by mismatch class rather than by line, because 90% line
coverage of a module that never checks pagination tells you nothing about
whether pagination regressions get caught.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from contracts.drift import build_timeline, union_rules, window_from_fixture
from fixtures.checks import CONTRADICTS, evaluate, load_traffic
from fixtures.format import tools, verify
from shim.corpus_guards import compile_from_fixture, uncompiled_mismatches

# Synthetic responses that each guard must reject. Written by hand so a guard
# is exercised even when its recorded evidence happens to be an easy case.
PLANTED: dict[str, dict[str, Any]] = {
    "assert_status_agrees_with_body": {
        "request": {}, "status": 200, "headers": {},
        "response": {"success": False, "data": None,
                     "errors": [{"message": "planted failure under a 200"}]}},
    "assert_requested_currencies_returned": {
        "request": {"base": "USD", "symbols": "EUR,GBP,CHF"}, "status": 200, "headers": {},
        "response": {"base": "USD", "date": "2026-09-18", "rates": {"EUR": 0.87}}},
    "assert_echoed_date_matches_request": {
        "request": {"date": "2026-09-13", "base": "USD"}, "status": 200, "headers": {},
        "response": {"base": "USD", "date": "2026-09-11", "rates": {"EUR": 0.86}}},
    "assert_error_names_the_value_sent": {
        "request": {"forecast_days": 42}, "status": 400, "headers": {},
        "response": {"error": True, "reason": "Allowed range 0 to 16. Given 16."}},
    "assert_page_size_honoured_or_announced": {
        "request": {"page_size": 200}, "status": 200, "headers": {},
        "response": {"results": [{"id": str(i)} for i in range(50)], "has_more": True}},
    "assert_zero_page_is_not_an_empty_result": {
        "request": {"q": "dune", "limit": 0}, "status": 200, "headers": {},
        "response": {"numFound": 48168, "docs": []}},
    "assert_retry_after_present_on_429": {
        "request": {}, "status": 429, "headers": {},
        "response": {"error": "rate_limited"}},
    "assert_known_parameter_spelling": {
        "request": {"base": "USD", "symbols": "EUR"}, "status": 200, "headers": {},
        "response": []},
    "assert_stored_value_matches_sent": {
        "request": {"title": "x", "due_date": "1969-07-20"}, "status": 200, "headers": {},
        "response": {"id": "i1", "title": "x", "due_date": None}},
}

# Recordings where the tool behaved. Every guard must stay silent over all of
# them: these are the fixtures that make a guard's silence meaningful.
HONEST = [("wikipedia", "actionapi-2026-09-19"),
          ("pokeapi", "v2-2026-09-19"),
          ("frankfurter", "v2-2026-09-19")]


def _exchanges(tool: str, version: str) -> list[dict[str, Any]]:
    return [{"request": r["request"], "status": r["status"],
             "headers": r.get("headers") or {}, "response": r["response"],
             "path": r.get("path", ""), "key": r["key"]}
            for r in load_traffic(tool, version)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    failures: list[str] = []
    corpus = tools()

    # 1. replay ------------------------------------------------------------
    for t, v in corpus:
        problems = verify(t, v)
        failures += [f"fixture {t}@{v}: {p}" for p in problems]

    # 2. compile -----------------------------------------------------------
    guards = []
    for t, v in corpus:
        guards += compile_from_fixture(t, v)
        for rid in uncompiled_mismatches(t, v):
            failures.append(
                f"{t}@{v}: rule {rid!r} is a confirmed mismatch with no guard compiled; "
                f"'if the agent learned it, the shim enforces it' is false for it")

    by_class: dict[str, list] = {}
    for g in guards:
        by_class.setdefault(g.mismatch_class, []).append(g)

    # 3. positive cases ----------------------------------------------------
    false_alarms = []
    for t, v in HONEST:
        for ex in _exchanges(t, v):
            for g in guards:
                reason = g(ex)
                if reason:
                    false_alarms.append(f"{g.name} fired on honest {t}@{v} {ex['key']}: {reason}")
    failures += false_alarms

    # 4. recorded violations ----------------------------------------------
    silent = []
    for g in guards:
        # The violating exchange is usually in the guard's own cited evidence.
        # A version guard is the exception: `assert_known_parameter_spelling`
        # was established by the v2 contract but the request it refuses is a
        # v1-shaped one, which lives in the predecessor's recording. So the
        # search widens to every version of the same tool before giving up.
        candidates = [ex for ex in _exchanges(g.tool, g.fixture_version)
                      if not g.evidence or ex["key"] in g.evidence]
        fired = any(g(ex) for ex in candidates)
        if not fired:
            for t2, v2 in corpus:
                if t2 != g.tool:
                    continue
                if any(g(ex) for ex in _exchanges(t2, v2)):
                    fired = True
                    break
        if not fired:
            silent.append(f"{g.name} ({g.rule_id}) did not fire on any recorded "
                          f"{g.tool} exchange")
    failures += silent

    # 5. planted violations ------------------------------------------------
    unplanted, missed = [], []
    for g in {g.name: g for g in guards}.values():
        p = PLANTED.get(g.name)
        if p is None:
            unplanted.append(g.name)
            continue
        if not g(p):
            missed.append(f"{g.name} passed a planted violation")
    failures += missed
    failures += [f"{n} has no planted violation to test it against" for n in unplanted]

    # report ---------------------------------------------------------------
    report = {
        "fixtures": len(corpus),
        "guards": len(guards),
        "distinct_guards": len({g.name for g in guards}),
        "coverage_by_mismatch_class": {k: len(v) for k, v in sorted(by_class.items())},
        "false_alarms": len(false_alarms),
        "silent_on_own_evidence": len(silent),
        "planted_missed": len(missed),
        "failures": failures,
    }
    if a.json:
        print(json.dumps(report, indent=2))
        return 1 if failures else 0

    print(f"\n  regression gate -- offline, no credentials, no model calls\n")
    print(f"  1. replay        {len(corpus)} fixtures verified")
    print(f"  2. compile       {len(guards)} guards "
          f"({report['distinct_guards']} distinct) from confirmed mismatches")
    for cls, n in report["coverage_by_mismatch_class"].items():
        print(f"                     {cls:<24} {n} guard(s)")
    print(f"  3. positive      {len(HONEST)} honest fixtures, "
          f"{len(false_alarms)} false alarm(s)")
    print(f"  4. recorded      {len(guards) - len(silent)}/{len(guards)} guards fired on "
          f"the evidence that created them")
    print(f"  5. planted       {report['distinct_guards'] - len(missed) - len(unplanted)}"
          f"/{report['distinct_guards']} guards rejected a synthetic violation")

    print(f"\n  guard provenance (every guard names what justified it):")
    for g in sorted(guards, key=lambda x: (x.mismatch_class, x.name))[:20]:
        print(f"    {g.name:<42} {g.mismatch_class:<22} {g.tool}@{g.fixture_version}")
        print(f"        rule={g.rule_id}  evidence={g.evidence[:2]}  traffic={g.traffic_sha256[:19]}...")

    if failures:
        print(f"\n  {len(failures)} FAILURE(S):")
        for f in failures:
            print(f"    - {f}")
        return 1
    print(f"\n  every confirmed mismatch compiles to a guard; every guard is silent on "
          f"honest traffic,\n  fires on the evidence that created it, and rejects a planted "
          f"violation.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
