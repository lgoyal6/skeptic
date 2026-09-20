"""
The evaluation package: one command, one frozen record, every number in it.

    ./.venv/bin/python -m bench.evaluate                 # print
    ./.venv/bin/python -m bench.evaluate --write         # also write evaluation.json

Runs the whole offline pipeline and emits a single record containing the
environment, the corpus identity, and every measurement -- including the ones
that went the wrong way. A summary that reports only what worked is not a
measurement, it is an advertisement.

The manifest is frozen before the run: the corpus hashes, the commit, the
Python version and the lockfile hash are captured first, so a number in this
record can always be traced to the exact inputs that produced it. If the
corpus changes, the hashes change, and an old record is visibly about
something else rather than silently comparable.

Everything here is offline. No network, no credentials, no model calls, so
the record is reproducible by anyone with the repository.
"""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from bench.policy_eval import evaluate_all, summarise
from contracts.drift import build_timeline, union_rules, window_from_fixture
from fixtures.checks import CONTRADICTS, SUPPORTS, UNOBSERVABLE, evaluate, load_traffic
from fixtures.format import fixture_dir, read_manifest, tools, verify
from shim.corpus_guards import compile_from_fixture, uncompiled_mismatches

SEEDS = 50


def _git(*args: str) -> str:
    try:
        return subprocess.check_output(["git", *args], text=True,
                                       stderr=subprocess.DEVNULL).strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def frozen_manifest() -> dict[str, Any]:
    """Captured before anything is measured."""
    lock = Path("uv.lock")
    import hashlib
    return {
        "captured_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "commit": _git("rev-parse", "HEAD"),
        "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
        "dirty": bool(_git("status", "--porcelain")),
        "python": platform.python_version(),
        "platform": f"{platform.system()} {platform.machine()}",
        "lockfile_sha256": ("sha256:" + hashlib.sha256(lock.read_bytes()).hexdigest()
                            if lock.is_file() else None),
        "seeds": SEEDS,
        "corpus": [
            {"tool": t, "version": v,
             "traffic_sha256": read_manifest(t, v)["traffic_sha256"],
             "docs_sha256": read_manifest(t, v)["docs_sha256"],
             "raw_capture_sha256": read_manifest(t, v)["raw_capture_sha256"]}
            for t, v in tools()
        ],
        "commands": {
            "suite": "./.venv/bin/python -m pytest -q",
            "clean_install": ("UV_PROJECT_ENVIRONMENT=<env> uv sync --frozen --group dev "
                              "&& <env>/bin/python -m pytest -q"),
            "replay": "./.venv/bin/python -m bench.replay_corpus",
            "gate": "./.venv/bin/python -m bench.ci",
            "mutations": "./.venv/bin/python -m bench.mutations",
            "policies": f"./.venv/bin/python -m bench.policy_eval --seeds {SEEDS}",
            "drift": "./.venv/bin/python -m bench.drift_demo",
            "evaluation": "./.venv/bin/python -m bench.evaluate",
        },
    }


def corpus_results() -> dict[str, Any]:
    per_tool, verdict_counts = [], {CONTRADICTS: 0, SUPPORTS: 0, UNOBSERVABLE: 0}
    by_class: dict[str, dict[str, int]] = {}
    for t, v in tools():
        rows = load_traffic(t, v)
        exp = yaml.safe_load((fixture_dir(t, v) / "expected.yaml").read_text()) or {}
        rules = exp.get("rules") or []
        outcomes = {}
        for r in rules:
            o = evaluate(r["id"], rows).outcome
            outcomes[r["id"]] = o
            verdict_counts[o] += 1
            cls = str(r.get("mismatch_class", ""))
            by_class.setdefault(cls, {CONTRADICTS: 0, SUPPORTS: 0, UNOBSERVABLE: 0})[o] += 1
        per_tool.append({
            "tool": t, "version": v, "exchanges": len(rows),
            "rules": len(rules), "verdicts": outcomes,
            "verifies": verify(t, v) == [],
        })
    return {"per_fixture": per_tool, "verdicts": verdict_counts, "by_mismatch_class": by_class}


def guard_results() -> dict[str, Any]:
    guards = [g for t, v in tools() for g in compile_from_fixture(t, v)]
    unguarded = [f"{t}@{v}:{r}" for t, v in tools() for r in uncompiled_mismatches(t, v)]
    cov: dict[str, int] = {}
    for g in guards:
        cov[g.mismatch_class] = cov.get(g.mismatch_class, 0) + 1
    return {"compiled": len(guards), "distinct": len({g.name for g in guards}),
            "coverage_by_mismatch_class": cov, "unguarded_mismatches": unguarded,
            "provenance": [g.to_dict() for g in guards]}


def drift_results() -> dict[str, Any]:
    tool, v1, v2 = "frankfurter", "v1-2026-09-19", "v2-2026-09-19"
    rules = union_rules(tool, [v1, v2])
    windows = [window_from_fixture(tool, ver, label=f"{tag}@w{i}", rules=rules)
               for tag, ver in (("v1", v1), ("v2", v2)) for i in range(2)]
    store, changes = build_timeline(windows)
    stale = {r: [w.semantics[r] for w in windows]
             for r, o in store.versions[0].semantics.items() if o == CONTRADICTS}
    demotions = {}
    for r, series in stale.items():
        at = next((i for i, s in enumerate(series) if s != CONTRADICTS), None)
        demotions[r] = {"series": series, "windows_until_demotion": at,
                        "demoted_to": None if at is None else series[at]}
    kinds: dict[str, int] = {}
    for c in changes:
        kinds[c.kind] = kinds.get(c.kind, 0) + 1
    return {"windows": len(windows), "contract_versions": len(store.versions),
            "changes_by_kind": kinds,
            "promoted": sum(1 for c in changes if c.promoted),
            "candidates_not_promoted": sum(1 for c in changes if not c.promoted),
            "belief_demotion": demotions}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--seeds", type=int, default=SEEDS)
    a = ap.parse_args()

    manifest = frozen_manifest()
    corpus = corpus_results()
    guards = guard_results()
    drift = drift_results()
    policy = summarise(evaluate_all(seeds=a.seeds, corpus=tools()))

    record = {"manifest": manifest, "corpus": corpus, "guards": guards,
              "drift": drift, "policies": policy,
              "null_and_losing_results": [
                  {"claim": "expected information gain beats the greedy selector",
                   "result": "false on this corpus",
                   "detail": (f"eig spends {policy['eig']['calls_mean']} calls/run against "
                              f"greedy's {policy['greedy']['calls_mean']}; the simpler policy "
                              f"is retained"),
                   "kept": "greedy"},
                  {"claim": "probe cost differentiates the arms",
                   "result": "untested",
                   "detail": ("every recorded exchange costs exactly one call, so the cost "
                              "term in the eig score is constant across candidates. A corpus "
                              "with uneven probe costs would test more of the policy than "
                              "this one does"),
                   "kept": None},
                  {"claim": "tokens and wall time distinguish the arms",
                   "result": "not measured",
                   "detail": "replay spends neither; both are reported as 0 rather than estimated",
                   "kept": None},
              ]}

    print(f"\n  skeptic evaluation record")
    print(f"  commit {manifest['commit'][:12]}"
          f"{' (dirty)' if manifest['dirty'] else ''}  "
          f"python {manifest['python']}  {manifest['platform']}\n")

    print(f"  corpus: {len(manifest['corpus'])} fixtures, "
          f"{sum(f['exchanges'] for f in corpus['per_fixture'])} exchanges, "
          f"{len({c['tool'] for c in manifest['corpus']})} tools")
    print(f"    verdicts: {corpus['verdicts'][CONTRADICTS]} mismatches, "
          f"{corpus['verdicts'][SUPPORTS]} honest, "
          f"{corpus['verdicts'][UNOBSERVABLE]} abstentions")
    print(f"    every fixture verifies: "
          f"{all(f['verifies'] for f in corpus['per_fixture'])}")

    print(f"\n  guards: {guards['compiled']} compiled "
          f"({guards['distinct']} distinct), "
          f"{len(guards['unguarded_mismatches'])} confirmed mismatch(es) unguarded")
    for k, n in sorted(guards["coverage_by_mismatch_class"].items()):
        print(f"    {k:<24} {n}")

    print(f"\n  drift: {drift['windows']} windows -> "
          f"{drift['contract_versions']} contract versions, "
          f"changes {drift['changes_by_kind']}")
    for r, d in drift["belief_demotion"].items():
        print(f"    {r}")
        print(f"        {' -> '.join(d['series'])}  "
              f"(demoted after {d['windows_until_demotion']} windows "
              f"to {d['demoted_to']!r})")

    print(f"\n  probe policies ({a.seeds} seeds for the stochastic arm):")
    print(f"    {'arm':<9} {'success':>8} {'calls/run':>10} {'sd':>6} {'false':>6} {'abstain':>8}")
    for name in ("fixed", "random", "greedy", "eig"):
        s = policy[name]
        print(f"    {name:<9} {s['success_rate']:>8.3f} {s['calls_mean']:>10.2f} "
              f"{s['calls_stdev']:>6} {s['false_beliefs']:>6} {s['abstentions']:>8}")

    print(f"\n  null and losing results (kept, not dropped):")
    for n in record["null_and_losing_results"]:
        print(f"    - {n['claim']}: {n['result']}")
        print(f"        {n['detail']}")

    if a.write:
        Path("evaluation.json").write_text(json.dumps(record, indent=2, default=str) + "\n")
        print(f"\n  wrote evaluation.json")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
