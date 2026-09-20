"""
Replay the whole fixture corpus offline, in one command, with no credentials.

    ./.venv/bin/python -m bench.replay_corpus

This is the command the README points an independent checkout at, and the one
CI runs on every change. It needs no network, no API keys, and no paid model
calls: every response it reads was captured once and committed.

What it checks, per fixture:

- the fixture verifies against its own manifest, including that the normalized
  `traffic.jsonl` can still be re-derived from the immutable raw capture;
- every recorded exchange replays, and the body that comes back hashes to the
  value recorded at capture time;
- the same `ContractLayer` the live adapters use sees the recording, so the
  anomaly signatures a fixture produces are produced by the real detector
  rather than by a fixture-specific shortcut;
- the mismatch classes the corpus claims to cover are actually present.

It deliberately does not judge whether the right beliefs were formed. That is
scoring, and it lives in `bench.score`. This command answers the narrower
question an outside reader needs answered first: does the evidence this
project publishes still exist, and does it still say what it said?
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

from agent.adapters.fixture import FixtureAdapter
from fixtures.format import body_hash, fixture_dir, read_manifest, tools, verify


def replay_one(tool: str, version: str, root: str = "fixtures",
               runs_dir: str | None = None) -> dict[str, Any]:
    problems = verify(tool, version, root)
    manifest = read_manifest(tool, version, root)
    expected = yaml.safe_load(
        (fixture_dir(tool, version, root) / "expected.yaml").read_text()) or {}
    rules = expected.get("rules") or []

    with tempfile.TemporaryDirectory() as td:
        a = FixtureAdapter(run_id=f"replay-{tool}-{version}", tool=tool, version=version,
                           runs_dir=runs_dir or td, fixtures_root=root)
        played = a.replay_all()

    bad_hash = [p["key"] for p in played
                if p["expected_body_sha256"] and body_hash(p["body"]) != p["expected_body_sha256"]]
    if bad_hash:
        problems.append(f"{len(bad_hash)} response(s) did not hash to the captured value")
    if a.misses:
        problems.append(f"{len(a.misses)} recorded exchange(s) did not replay")

    observable = [r for r in rules if r.get("observable", True)]
    unobservable = [r for r in rules if not r.get("observable", True)]
    controls = [r for r in rules if r.get("mismatch_class") == "no-mismatch-control"]

    return {
        "tool": tool, "version": version,
        "exchanges": len(played),
        "anomalies": a.anomaly_signatures(),
        "classes": sorted({str(r.get("mismatch_class", "")) for r in rules} - {""}),
        "rules": len(rules),
        "observable": len(observable),
        "unobservable": len(unobservable),
        "controls": len(controls),
        "qualitative": bool(manifest.get("qualitative")),
        "problems": problems,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="fixtures")
    ap.add_argument("--tool", default=None)
    ap.add_argument("--version", default=None)
    a = ap.parse_args()

    corpus = [(t, v) for t, v in tools(a.root)
              if a.tool in (None, t) and a.version in (None, v)]
    if not corpus:
        print(f"  no fixtures matched tool={a.tool} version={a.version}")
        return 2

    print("\n  offline corpus replay -- no network, no credentials, no model calls\n")
    print(f"  {'tool@version':<38} {'calls':>5} {'rules':>5} {'ctrl':>4} {'unobs':>5}  classes")
    print(f"  {'-'*38} {'-'*5} {'-'*5} {'-'*4} {'-'*5}  {'-'*40}")

    rows = [replay_one(t, v, a.root) for t, v in corpus]
    class_counts: Counter[str] = Counter()
    failed = 0
    for r in rows:
        for c in r["classes"]:
            class_counts[c] += 1
        if r["problems"]:
            failed += 1
        flag = "" if not r["problems"] else "  <-- PROBLEM"
        print(f"  {r['tool']+'@'+r['version']:<38} {r['exchanges']:>5} {r['rules']:>5} "
              f"{r['controls']:>4} {r['unobservable']:>5}  {','.join(r['classes'])}{flag}")

    print(f"\n  {len(rows)} fixtures, {sum(r['exchanges'] for r in rows)} exchanges replayed")
    print(f"  distinct tools: {len({r['tool'] for r in rows})}")
    print(f"\n  mismatch-class coverage:")
    for c, n in sorted(class_counts.items()):
        print(f"    {c:<24} {n} fixture(s)")

    missing = {"status-code-mismatch", "field-omission", "renamed-field",
               "pagination-cursor", "semantic-type-correct",
               "no-mismatch-control", "unobservable"} - set(class_counts)
    if missing:
        print(f"\n  classes the corpus does NOT cover: {sorted(missing)}")

    if failed:
        print(f"\n  {failed} fixture(s) had problems:")
        for r in rows:
            for p in r["problems"]:
                print(f"    {r['tool']}@{r['version']}: {p}")
        return 1
    print(f"\n  every fixture verifies, replays, and hashes to its captured value\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
