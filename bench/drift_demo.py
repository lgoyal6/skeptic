"""
Replay a real contract change and show what the detector makes of it.

    ./.venv/bin/python -m bench.drift_demo

Frankfurter's v1 and v2 surfaces were captured on the same day, minutes apart,
and they disagree about three things: the filter parameter is renamed, an
unknown currency goes from silently dropped to rejected with 422, and a date
with no published rate goes from silently substituted to returned as asked.

That makes it a real temporal pair rather than a planted one, which matters:
the planted changes in `tests/test_drift.py` prove the classifier separates
the four kinds, and this proves it does something useful on a change nobody
constructed for it.

The same rule set is evaluated at every window -- the union across both
versions -- because that is what carrying a belief across a version change
means. A v1 belief that stops being asked is not retired, it is forgotten,
and the difference is the entire point of the exercise.

Nothing here touches the network or spends a model call.
"""

from __future__ import annotations

import argparse
import json
import sys

from contracts.drift import build_timeline, union_rules, window_from_fixture

TOOL = "frankfurter"
V1, V2 = "v1-2026-09-19", "v2-2026-09-19"


def demote_report(windows, store) -> list[dict]:
    """Which beliefs held under the first contract did not survive, and how fast."""
    first = store.versions[0]
    stale = [r for r, v in first.semantics.items() if v == "contradicts_doc"]
    out = []
    for rule in stale:
        series = [w.semantics.get(rule) for w in windows]
        demoted_at = next((i for i, v in enumerate(series) if v != "contradicts_doc"), None)
        out.append({
            "belief": rule,
            "held_under": f"v{first.version}",
            "series": series,
            "demoted_at_window": demoted_at,
            "observations_until_demotion": None if demoted_at is None else demoted_at,
            "outcome": None if demoted_at is None else series[demoted_at],
        })
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeats", type=int, default=2,
                    help="windows per captured version; >1 lets a change be confirmed")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    rules = union_rules(TOOL, [V1, V2])
    windows = []
    for ver, tag in ((V1, "v1"), (V2, "v2")):
        for k in range(a.repeats):
            windows.append(window_from_fixture(
                TOOL, ver, label=f"{tag}@w{len(windows)}", rules=rules))

    store, changes = build_timeline(windows)
    demotions = demote_report(windows, store)

    if a.json:
        print(json.dumps({"contracts": store.to_dict(),
                          "changes": [c.to_dict() for c in changes],
                          "demotions": demotions}, indent=2, default=str))
        return 0

    print(f"\n  contract drift -- {TOOL}, {len(windows)} windows, "
          f"{len(rules)} rules carried across all of them\n")

    print("  CONTRACT VERSIONS (append-only; the predecessor stays replayable)")
    for c in store.versions:
        print(f"    {c.summary()}   predecessor={c.predecessor}")
        for k, v in sorted(c.semantics.items()):
            print(f"        {k:<45} {v}")
    print()

    print("  CLASSIFIED CHANGES")
    by_kind: dict[str, list] = {}
    for c in changes:
        by_kind.setdefault(c.kind, []).append(c)
    for kind in ("documentation", "schema", "semantic", "transient"):
        rows = by_kind.get(kind, [])
        print(f"    {kind}: {len(rows)}")
        for c in rows:
            mark = "promoted" if c.promoted else "candidate (unconfirmed)"
            print(f"      - {c.signal}  [{mark}, confirmed x{c.confirmations}]")
            print(f"          {c.detail[:160]}")
            if c.evidence:
                print(f"          smallest evidence set: {c.evidence}")
    print()

    print("  BELIEF DEMOTION (beliefs the first contract held, re-asked at every window)")
    for d in demotions:
        if d["demoted_at_window"] is None:
            print(f"    {d['belief']}: still holds across all {len(d['series'])} windows")
        else:
            print(f"    {d['belief']}")
            print(f"        {' -> '.join(str(s) for s in d['series'])}")
            print(f"        demoted after {d['observations_until_demotion']} window(s), "
                  f"to {d['outcome']!r}")
            if d["outcome"] == "unobservable":
                print(f"        (the newer capture does not ask the question this belief "
                      f"answers, so it is set aside rather than refuted -- abstention, "
                      f"not a verdict)")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
