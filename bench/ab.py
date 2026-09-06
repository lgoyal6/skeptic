"""
The A/B: does what was learned actually help someone else?

Two fresh agents, neither with any memory:

  NAIVE    the documentation, and nothing else
  SHIELDED the same documentation, plus the exported guards enforcing what
           skeptic learned

Both get identical tasks against an identical world. Neither has ever seen a
belief file. The only difference is whether the learned knowledge is enforced
at the call boundary.

This is the claim that matters. A success curve shows one agent improving,
which could be the model warming to a task. This shows the knowledge
TRANSFERRING to an agent that did none of the learning -- which is what
"publishes the spec so the next agent doesn't have to" has to mean.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from agent import tasks
from agent.beliefs import BeliefStore
from agent.executor import run_task
from agent.llm import build
from shim.guards import compile_guards, guard_report

LAB = "http://127.0.0.1:8077"

SUITE = [
    ("inventory", {"vendor": "AbNorth", "n": 73}),
    ("oldest_three", {"vendor": "AbVertex"}),
    ("exactly_once", {"vendor": "AbAcme", "n": 10}),
]


def _reset_world() -> None:
    httpx.post(f"{LAB}/_control/reset", json={"seed": 1337}, timeout=30.0)


def run_arm(
    label: str,
    guards_on: bool,
    beliefs_root: str,
    repeats: int = 1,
    verbose: bool = True,
) -> dict[str, Any]:
    ex, _refl, usage = build()
    store = BeliefStore("lab", root=beliefs_root)
    rows: list[dict[str, Any]] = []
    p0 = usage.prompt_tokens
    c0 = usage.completion_tokens
    t0 = time.time()

    for rep in range(repeats):
        for task_id, cfg in SUITE:
            guards = compile_guards(store) if guards_on else []
            _reset_world()
            task = tasks.get(task_id)
            run_id = f"ab-{label}-{task_id}-{rep}"
            try:
                res = run_task(
                    task, ex, run_id=run_id, cfg=cfg,
                    memory=False,               # neither arm gets memory
                    beliefs_root=beliefs_root,
                    guards=guards,
                    runs_dir="runs",
                )
            except Exception as e:  # noqa: BLE001
                rows.append({"task": task_id, "ok": False, "detail": f"crashed: {e}",
                             "calls": 0, "tokens": 0, "wall_s": 0.0})
                continue
            fired = sum(g.fired for g in guards)
            rows.append({
                "task": task_id, "ok": res.success, "detail": res.detail,
                "calls": res.tool_calls, "tokens": res.total_tokens,
                "wall_s": res.wall_s, "guards_fired": fired,
                "expected": res.expected, "got": res.got,
            })
            if verbose:
                mark = "PASS" if res.success else "FAIL"
                extra = f" guards_fired={fired}" if guards_on else ""
                print(f"    [{label:8}] {task_id:14} {mark}  calls={res.tool_calls:>3} "
                      f"tokens={res.total_tokens:>7}{extra}  {res.detail[:44]}", flush=True)

    passed = sum(1 for r in rows if r["ok"])
    return {
        "arm": label,
        "guards": guards_on,
        "passed": passed,
        "total": len(rows),
        "calls": sum(r["calls"] for r in rows),
        "tokens": usage.prompt_tokens + usage.completion_tokens - p0 - c0,
        "wall_s": round(time.time() - t0, 1),
        "guards_fired": sum(r.get("guards_fired", 0) for r in rows),
        "rows": rows,
    }


def render(naive: dict[str, Any], shielded: dict[str, Any], guards: list[Any]) -> str:
    L = ["", "  before / after -- two fresh agents, neither with memory", ""]
    L.append(f"  {'':16} {'NAIVE':>14}   {'SHIELDED':>14}")
    L.append(f"  {'':16} {'(docs only)':>14}   {'(docs + guards)':>14}")
    L.append("  " + "-" * 50)

    by_task_n = {r["task"]: r for r in naive["rows"]}
    by_task_s = {r["task"]: r for r in shielded["rows"]}
    for t in [x[0] for x in SUITE]:
        n, s = by_task_n.get(t, {}), by_task_s.get(t, {})
        L.append(f"  {t:16} {'PASS' if n.get('ok') else 'FAIL':>14}   "
                 f"{'PASS' if s.get('ok') else 'FAIL':>14}")
    L.append("  " + "-" * 50)
    L.append(f"  {'passed':16} {naive['passed']}/{naive['total']:<13} "
             f"{shielded['passed']}/{shielded['total']}")
    L.append(f"  {'tool calls':16} {naive['calls']:>14}   {shielded['calls']:>14}")
    L.append(f"  {'tokens':16} {naive['tokens']:>14}   {shielded['tokens']:>14}")
    L.append(f"  {'wall seconds':16} {naive['wall_s']:>14}   {shielded['wall_s']:>14}")
    L.append("")
    if guards:
        L.append("  guards enforced:")
        for g in guard_report(guards):
            L.append(f"    {g['guard']:22} fired {g['fired']:>3}x   ({g['implements']})")
    L.append("")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--beliefs", default="beliefs")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--out", default="bench/ab_result.json")
    a = ap.parse_args()

    store = BeliefStore("lab", root=a.beliefs)
    guards = compile_guards(store)
    if not guards:
        print("  no confirmed beliefs compile to guards yet; run a session first")
        return 1

    print(f"\n  {len(store.active())} confirmed beliefs -> {len(guards)} guards\n")
    naive = run_arm("naive", False, a.beliefs, a.repeats)
    shielded = run_arm("shielded", True, a.beliefs, a.repeats)

    out = render(naive, shielded, compile_guards(store))
    print(out)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(
        {"naive": naive, "shielded": shielded,
         "guards": guard_report(guards)}, indent=2))
    print(f"  written to {a.out}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
