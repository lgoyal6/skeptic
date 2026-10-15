"""Score frozen, independently versioned tool-contract recordings.

This is a deterministic evaluator only. It does not assert that fixtures are
independent external evidence; provenance labels remain in each input row.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def evaluate(rows: list[dict]) -> dict:
    if not rows:
        raise ValueError("holdout must not be empty")
    ids = [r.get("workflow_id") for r in rows]
    if any(not x for x in ids) or len(set(ids)) != len(ids):
        raise ValueError("workflow_id values must be present and unique")
    if any(r.get("split") != "holdout" for r in rows):
        raise ValueError("evaluator accepts holdout records only")
    versions = [r.get("tool_version") for r in rows]
    if any(not isinstance(v, str) or not v.strip() for v in versions):
        raise ValueError("every recording requires a non-empty tool_version")
    out = {"n_workflows": len(rows), "n_distinct_tool_versions": len(set(versions)),
           "external_provenance_established": False,
           "valid_n": 0, "valid_false_refusals": 0, "invalid_n": 0,
           "invalid_false_accepts": 0, "discovered_defects": 0,
        "provenance_labels": sorted({r.get("provenance", "unspecified") for r in rows})}
    for r in rows:
        if r.get("expected") not in {"allow", "deny"} or r.get("observed") not in {"allow", "deny"}:
            raise ValueError("expected and observed must be allow or deny")
        if r["expected"] == "allow":
            out["valid_n"] += 1
            out["valid_false_refusals"] += r["observed"] == "deny"
        else:
            out["invalid_n"] += 1
            out["invalid_false_accepts"] += r["observed"] == "allow"
        out["discovered_defects"] += bool(r.get("confirmed_defect", False))
    out["valid_false_refusal_rate"] = out["valid_false_refusals"] / out["valid_n"] if out["valid_n"] else None
    out["invalid_false_accept_rate"] = out["invalid_false_accepts"] / out["invalid_n"] if out["invalid_n"] else None
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("recordings", type=Path)
    args = parser.parse_args()
    print(json.dumps(evaluate(json.loads(args.recordings.read_text())), indent=2))


if __name__ == "__main__":
    main()
