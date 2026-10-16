"""Build the public Frankfurter date-check walkthrough from saved fixtures.
The real deterministic check produces all three verdicts before export.
Only dates, HTTP status, evidence keys, and source hashes enter the bundle.
"""

import argparse
import hashlib
import json
from pathlib import Path
import sys


RULE_ID = "non_publication_date_silently_substituted"
SUNDAY = "2026-09-13"
FRIDAY = "2026-09-11"
INPUTS = (
    "fixtures/checks.py",
    "fixtures/frankfurter/v1-2026-09-19/docs.md",
    "fixtures/frankfurter/v1-2026-09-19/traffic.jsonl",
    "fixtures/frankfurter/v2-2026-09-19/docs.md",
    "fixtures/frankfurter/v2-2026-09-19/traffic.jsonl",
    "shim/corpus_guards.py",
    "docs/ONE_MISMATCH.md",
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_evidence(root: Path, source_commit: str) -> dict:
    sys.path.insert(0, str(root))
    from fixtures.checks import evaluate, load_traffic

    v1 = load_traffic("frankfurter", "v1-2026-09-19", root / "fixtures")
    v2 = load_traffic("frankfurter", "v2-2026-09-19", root / "fixtures")
    cases = []
    selections = (
        ("v1-sunday-mismatch", "v1", v1, lambda row: row["path"].endswith(SUNDAY)),
        ("v1-friday-control", "v1", v1, lambda row: row["path"].endswith(FRIDAY)),
        ("v2-sunday-correction", "v2", v2, lambda row: row["request"].get("date") == SUNDAY),
    )
    expected = {
        "v1-sunday-mismatch": "contradicts_doc",
        "v1-friday-control": "unobservable",
        "v2-sunday-correction": "supports_doc",
    }
    for case_id, version, rows, select in selections:
        row = next((item for item in rows if select(item)), None)
        if row is None:
            raise ValueError(f"Missing fixture case: {case_id}")
        verdict = evaluate(RULE_ID, [row])
        if verdict.outcome != expected[case_id]:
            raise ValueError(f"{case_id} changed from {expected[case_id]} to {verdict.outcome}")
        request_date = row["request"].get("date") or row["path"].rstrip("/").split("/")[-1]
        response = row["response"]
        response_date = response.get("date") if isinstance(response, dict) else response[0].get("date")
        cases.append({
            "id": case_id,
            "version": version,
            "path": row["path"],
            "requestDate": request_date,
            "responseDate": response_date,
            "status": row["status"],
            "evidenceKey": row["key"],
            "outcome": verdict.outcome,
            "detail": verdict.detail,
        })

    return {
        "sourceCommit": source_commit,
        "ruleId": RULE_ID,
        "cases": cases,
        "sourceHashes": {name: sha256(root / name) for name in INPUTS},
        "scope": "Saved public API recordings only. The page does not make live requests or call a model.",
        "limits": [
            "Three saved requests explain one date-fidelity check; they do not describe every API behavior.",
            "The v1 and v2 records are historical captures, not a live service check.",
            "The Friday match is unobservable for substitution risk. It is not evidence that the promise holds.",
            "The guard checks echoed dates. It does not verify that the returned exchange rate is economically correct.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Export saved date-fidelity evidence")
    parser.add_argument("--output", type=Path, default=Path("docs/demo/evidence.json"))
    parser.add_argument("--source-commit", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    data = build_evidence(root, args.source_commit)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"cases": len(data["cases"]), "outcomes": [row["outcome"] for row in data["cases"]]}))


if __name__ == "__main__":
    main()
