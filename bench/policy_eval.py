"""
Four-arm comparison of probe-selection policies, on identical inputs.

    ./.venv/bin/python -m bench.policy_eval
    ./.venv/bin/python -m bench.policy_eval --seeds 50 --json

Every arm gets the same fixtures, the same claims, the same budget, the same
success definition, and the same seeds. The only thing that differs is which
probe is bought next.

## The task

A fixture is a set of recorded exchanges and a set of documented claims. The
agent starts having observed nothing and buys one exchange at a time, up to a
call budget. After each purchase every claim is re-evaluated against only the
exchanges bought so far. A claim is *settled* when its check returns a verdict
other than `unobservable`.

Success is settling every claim that is settleable from the full recording,
within budget, with no verdict that disagrees with the answer key. The answer
key is used here -- at scoring time, after every decision has been made -- and
nowhere else. No policy can see it.

## What is measured

Calls spent, claims settled, false beliefs, abstentions, and whether the run
succeeded. Wall time and tokens are reported as zero and stated as such:
replay spends neither, and printing a fabricated number for them would be
worse than printing nothing.

## A stated limitation

On this corpus every recorded exchange costs exactly one call, so the cost
term in the EIG score is constant across candidates and the comparison
measures information only. A corpus with genuinely uneven probe costs would
test more of the policy than this one does. That is a real gap, and it is
reported rather than hidden behind a cost model invented to fill it.
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
from dataclasses import dataclass, field
from typing import Any

import yaml

from bench.policies import POLICIES, Claim, Probe, PolicyView
from fixtures.checks import UNOBSERVABLE, evaluate, load_traffic
from fixtures.format import fixture_dir, tools


def _answer_key(tool: str, version: str) -> dict[str, Any]:
    p = fixture_dir(tool, version) / "expected.yaml"
    exp = yaml.safe_load(p.read_text()) or {}
    return {r["id"]: r for r in exp.get("rules") or []}


@dataclass
class RunResult:
    policy: str
    tool: str
    version: str
    seed: int
    calls: int
    budget: int
    settled: int
    settleable: int
    false_beliefs: int
    abstentions: int
    success: bool
    order: list[str] = field(default_factory=list)


def settleable_claims(tool: str, version: str) -> dict[str, str]:
    """The verdict each claim reaches when the whole recording is observed.

    The ceiling any policy could reach, computed once from full evidence. It
    is the scoring target, not an input to any decision.
    """
    rows = load_traffic(tool, version)
    return {rid: evaluate(rid, rows).outcome for rid in _answer_key(tool, version)}


def run_one(policy_name: str, tool: str, version: str, seed: int,
            budget: int | None = None) -> RunResult:
    rows = load_traffic(tool, version)
    key = _answer_key(tool, version)
    ceiling = settleable_claims(tool, version)
    target = {r: v for r, v in ceiling.items() if v != UNOBSERVABLE}

    probes = [Probe(key=r["key"], op=r["op"], request=r["request"]) for r in rows]
    by_key = {r["key"]: r for r in rows}
    budget = budget if budget is not None else len(probes)

    # Unobservable claims are presented to every policy alongside the rest. A
    # policy that could tell in advance which questions are unanswerable would
    # be reading the labels; the honest task includes questions with no answer.
    claims = [Claim(rule_id=rid, operation=str(spec.get("operation") or ""),
                    parameter=spec.get("parameter"))
              for rid, spec in key.items()]

    rng = random.Random(seed)
    view = PolicyView(unobserved=list(probes), observed=[], claims=claims)
    picked: list[str] = []
    verdicts: dict[str, str] = {}

    while view.unobserved and len(picked) < budget:
        probe = POLICIES[policy_name](view, rng)
        view.unobserved.remove(probe)
        view.observed.append(probe)
        picked.append(probe.key)

        seen_rows = [by_key[k] for k in picked]
        for c in claims:
            v = evaluate(c.rule_id, seen_rows)
            verdicts[c.rule_id] = v.outcome
            c.verdict = v.outcome
            c.settled = v.outcome != UNOBSERVABLE

        if all(verdicts.get(r) == v for r, v in target.items()):
            break

    settled = sum(1 for r in target if verdicts.get(r, UNOBSERVABLE) != UNOBSERVABLE)
    false_beliefs = sum(1 for r, want in ceiling.items()
                        if verdicts.get(r, UNOBSERVABLE) != UNOBSERVABLE
                        and verdicts.get(r) != want)
    abstentions = sum(1 for r in ceiling if verdicts.get(r, UNOBSERVABLE) == UNOBSERVABLE)
    success = settled == len(target) and false_beliefs == 0

    return RunResult(policy=policy_name, tool=tool, version=version, seed=seed,
                     calls=len(picked), budget=budget, settled=settled,
                     settleable=len(target), false_beliefs=false_beliefs,
                     abstentions=abstentions, success=success, order=picked)


def evaluate_all(seeds: int = 20, corpus: list[tuple[str, str]] | None = None
                 ) -> list[RunResult]:
    corpus = corpus or tools()
    out: list[RunResult] = []
    for name in POLICIES:
        for tool, version in corpus:
            # A deterministic policy returns the same answer for every seed, so
            # running it many times would manufacture a distribution out of one
            # observation. Run it once; its distribution is a point mass.
            n = seeds if name == "random" else 1
            for s in range(n):
                out.append(run_one(name, tool, version, seed=s))
    return out


def summarise(results: list[RunResult]) -> dict[str, dict[str, Any]]:
    by: dict[str, list[RunResult]] = {}
    for r in results:
        by.setdefault(r.policy, []).append(r)

    out: dict[str, dict[str, Any]] = {}
    for name, rows in by.items():
        calls = [r.calls for r in rows]
        out[name] = {
            "runs": len(rows),
            "success_rate": round(sum(r.success for r in rows) / len(rows), 3),
            "calls_mean": round(statistics.fmean(calls), 2),
            "calls_median": statistics.median(calls),
            "calls_min": min(calls),
            "calls_max": max(calls),
            "calls_stdev": round(statistics.stdev(calls), 2) if len(calls) > 1 else 0.0,
            "false_beliefs": sum(r.false_beliefs for r in rows),
            "abstentions": sum(r.abstentions for r in rows),
            "tokens": 0,
            "wall_s": 0.0,
        }
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=20)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    corpus = tools()
    results = evaluate_all(seeds=a.seeds, corpus=corpus)
    summary = summarise(results)

    if a.json:
        print(json.dumps({"summary": summary,
                          "runs": [r.__dict__ for r in results]}, indent=2))
        return 0

    print(f"\n  probe-selection policy comparison")
    print(f"  {len(corpus)} fixtures, identical claims/budget/success for every arm, "
          f"{a.seeds} seeds for the stochastic arm\n")
    print(f"  {'policy':<9} {'runs':>5} {'success':>8} {'calls/run':>10} {'median':>7} "
          f"{'min':>4} {'max':>4} {'sd':>6} {'false':>6} {'abstain':>8}")
    print(f"  {'-'*9} {'-'*5} {'-'*8} {'-'*10} {'-'*7} {'-'*4} {'-'*4} {'-'*6} {'-'*6} {'-'*8}")
    for name in ("fixed", "random", "greedy", "eig"):
        s = summary[name]
        print(f"  {name:<9} {s['runs']:>5} {s['success_rate']:>8.3f} {s['calls_mean']:>10.2f} "
              f"{s['calls_median']:>7} {s['calls_min']:>4} {s['calls_max']:>4} "
              f"{s['calls_stdev']:>6} {s['false_beliefs']:>6} {s['abstentions']:>8}")

    print(f"\n  per fixture, calls to settle everything settleable:")
    print(f"  {'fixture':<34} " + " ".join(f"{p:>8}" for p in POLICIES))
    for tool, version in corpus:
        cells = []
        for p in POLICIES:
            rows = [r for r in results
                    if r.policy == p and r.tool == tool and r.version == version]
            cells.append(f"{statistics.fmean([r.calls for r in rows]):>8.1f}")
        ok = all(r.success for r in results if r.tool == tool and r.version == version)
        print(f"  {tool + '@' + version:<34} " + " ".join(cells)
              + ("" if ok else "   <-- an arm failed"))

    base, eig = summary["greedy"]["calls_mean"], summary["eig"]["calls_mean"]
    print(f"\n  greedy {base:.2f} calls/run, eig {eig:.2f} calls/run: ", end="")
    if eig < base:
        print(f"eig uses {100 * (base - eig) / base:.1f}% fewer")
    elif eig > base:
        print(f"eig uses {100 * (eig - base) / base:.1f}% MORE -- the simpler policy wins")
    else:
        print("a tie, so the simpler policy is preferred")
    print(f"  false beliefs by arm: "
          f"{ {p: summary[p]['false_beliefs'] for p in POLICIES} }")
    print(f"  tokens and wall time are 0 for every arm: replay spends neither.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
