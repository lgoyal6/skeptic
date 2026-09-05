"""
Scoring: did the agent actually learn what is true?

Matching is STRUCTURAL, not semantic. A belief matches a ground-truth rule
when its class, operation and parameter line up. No LLM judge decides whether
two sentences mean the same thing, because an LLM judge grading its own
sibling is how benchmarks become fiction.

Recall is computed against OBSERVABLE rules only: a rule the lab never
actually triggered during the run is one the agent could not have found, and
counting it would understate the result dishonestly in the other direction.
Both numbers are reported so nothing hides.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import httpx
import yaml

from agent.beliefs import BeliefStore, Status

# Anomaly kind -> ground-truth class. Used both when a hypothesis is minted
# and when a belief is scored, so the two cannot drift.
KIND_TO_CLASS = {
    "silent_truncation": "silent_truncation",
    "silent_null": "silent_coercion",
    "type_coercion": "silent_coercion",
    "silent_ignore": "silent_coercion",
    "write_not_visible": "eventual_consistency",
    "silent_empty": "silent_coercion",       # or undocumented_flag; the probe decides
    "undocumented_status": "rate_limit",
    "undocumented_flag": "undocumented_flag",
    "mislabelled_semantics": "mislabelled_semantics",
    "idempotency_hazard": "idempotency_hazard",
    "expiry": "expiry",
}


def _param_matches(belief_param: str | None, gt_param: str | None) -> bool:
    if gt_param in (None, "*"):
        return True
    if belief_param in (None, "*"):
        return False
    return belief_param == gt_param


def _op_matches(belief_op: str, gt_op: str) -> bool:
    return gt_op == "*" or belief_op == gt_op


def match(belief, rules: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Best ground-truth rule for a belief, or None."""
    candidates = [
        r for r in rules
        if r["class"] == belief.cls
        and _op_matches(belief.operation, r["operation"])
        and _param_matches(belief.parameter, r.get("parameter"))
    ]
    if not candidates:
        return None
    # prefer an exact parameter match over a wildcard one
    exact = [r for r in candidates if r.get("parameter") == belief.parameter]
    return (exact or candidates)[0]


def observable_rules(lab_base: str) -> set[str] | None:
    try:
        r = httpx.get(f"{lab_base}/_control/rules_fired", timeout=5.0)
        return set(r.json().get("observable", []))
    except Exception:
        return None


def score(
    tool: str = "lab",
    beliefs_root: str = "beliefs",
    gt_path: str = "lab/ground_truth.yaml",
    lab_base: str = "http://127.0.0.1:8077",
) -> dict[str, Any]:
    gt = yaml.safe_load(Path(gt_path).read_text())
    rules = gt["rules"]
    by_id = {r["id"]: r for r in rules}

    store = BeliefStore(tool, root=beliefs_root)
    confirmed = [b for b in store.ordered() if b.status is Status.CONFIRMED]

    matched: dict[str, str] = {}       # rule_id -> belief_id
    false_beliefs: list[dict[str, Any]] = []
    duplicates: list[str] = []

    for b in confirmed:
        m = match(b, rules)
        if m is None:
            false_beliefs.append(
                {"belief": b.id, "class": b.cls, "operation": b.operation,
                 "parameter": b.parameter, "text": b.belief}
            )
            continue
        if m["id"] in matched:
            duplicates.append(b.id)
            continue
        matched[m["id"]] = b.id

    obs = observable_rules(lab_base)
    all_ids = {r["id"] for r in rules}
    denom_ids = obs if obs else all_ids

    found_observable = {rid for rid in matched if rid in denom_ids}

    precision = len(matched) / len(confirmed) if confirmed else 0.0
    recall_obs = len(found_observable) / len(denom_ids) if denom_ids else 0.0
    recall_all = len(matched) / len(all_ids)

    return {
        "tool": tool,
        "confirmed_beliefs": len(confirmed),
        "matched": len(matched),
        "false_beliefs": len(false_beliefs),
        "duplicates": len(duplicates),
        "precision": round(precision, 4),
        "recall_observable": round(recall_obs, 4),
        "recall_all_rules": round(recall_all, 4),
        "observable_rules": sorted(denom_ids),
        "found": sorted(matched.keys()),
        "missed": sorted(denom_ids - set(matched.keys())),
        "never_triggered": sorted(all_ids - denom_ids),
        "false_belief_detail": false_beliefs,
        "duplicate_detail": duplicates,
        "counts": store.counts(),
    }


def render(s: dict[str, Any]) -> str:
    L: list[str] = []
    L.append("")
    L.append(f"  skeptic bench -- beliefs about `{s['tool']}` vs ground truth")
    L.append("")
    L.append(f"    confirmed beliefs   {s['confirmed_beliefs']}")
    L.append(f"    matched a real rule {s['matched']}")
    L.append(f"    FALSE beliefs       {s['false_beliefs']}")
    L.append(f"    duplicates          {s['duplicates']}")
    L.append("")
    L.append(f"    precision           {s['precision']:.2f}")
    L.append(f"    recall (observable) {s['recall_observable']:.2f}   "
             f"{len(s['found'])}/{len(s['observable_rules'])}")
    L.append(f"    recall (all 14)     {s['recall_all_rules']:.2f}")
    L.append("")
    if s["found"]:
        L.append("    found:")
        for r in s["found"]:
            L.append(f"      + {r}")
    if s["missed"]:
        L.append("    missed (observable but not learned):")
        for r in s["missed"]:
            L.append(f"      - {r}")
    if s["never_triggered"]:
        L.append("    never triggered by this run (excluded from recall):")
        for r in s["never_triggered"]:
            L.append(f"      . {r}")
    if s["false_belief_detail"]:
        L.append("    FALSE beliefs (matched no rule):")
        for f in s["false_belief_detail"]:
            L.append(f"      ! {f['belief']}: {f['text'][:70]}")
    L.append("")
    L.append(f"    lifecycle: {s['counts']}")
    L.append("")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tool", default="lab")
    ap.add_argument("--beliefs", default="beliefs")
    ap.add_argument("--gt", default="lab/ground_truth.yaml")
    ap.add_argument("--lab", default="http://127.0.0.1:8077")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    s = score(a.tool, a.beliefs, a.gt, a.lab)
    if a.json:
        print(json.dumps(s, indent=2))
    else:
        print(render(s))
    return 0


if __name__ == "__main__":
    sys.exit(main())
