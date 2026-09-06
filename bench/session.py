"""
A full learning session: alternate task runs and experiments across several
tasks, so beliefs accumulate over a curve rather than one lucky probe.

Run this unbuffered and in the background; TensorMux latency swings by 4x
depending on load, so wall-clock here says more about their queue than about
the loop.

    ./.venv/bin/python -u -m bench.session --cycles 8 2>&1 | tee runs/session.log
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
import time
from pathlib import Path

import httpx

from agent.beliefs import BeliefStore
from agent.executor import run_task
from agent.llm import build
from agent import tasks
from reflect.hypothesis import mint, propose
from reflect.probe import run_probe

LAB = "http://127.0.0.1:8077"

SCHEDULE = [
    ("inventory", {"vendor": "Northwind", "n": 73}),
    ("oldest_three", {"vendor": "Vertex"}),
    ("inventory", {"vendor": "Northwind", "n": 73}),
    ("exactly_once", {"vendor": "Acme", "n": 12}),
]


def seed_world() -> None:
    httpx.post(f"{LAB}/_control/seed",
               json={"n": 73, "vendor": "Northwind", "archived_every": 7, "big_every": 11},
               timeout=30.0)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycles", type=int, default=8)
    ap.add_argument("--beliefs", default="beliefs")
    ap.add_argument("--probes-per-cycle", type=int, default=1)
    ap.add_argument("--max-hypotheses", type=int, default=1)
    ap.add_argument("--no-recon", action="store_true")
    ap.add_argument("--recon-calls", type=int, default=80)
    ap.add_argument("--recon-hypotheses", type=int, default=8)
    a = ap.parse_args()

    ex, refl, usage = build()
    seed_world()
    t0 = time.time()

    # Recon first. Learning must not depend on a task run being unlucky in a
    # productive way: one earlier session did three calls, tripped nothing and
    # learned nothing. A bounded deliberate sweep of the documented promises
    # surfaces the whole lie surface in ~30 calls, and the probe designer then
    # settles what it found.
    if not a.no_recon:
        from reflect.recon import sweep
        r = sweep(run_id="recon-000", budget_calls=a.recon_calls)
        print(f"recon: {r.calls} calls, {r.wall_s:.1f}s, "
              f"{len(r.promises_broken)}/{len(r.promises_tested)} promises broken, "
              f"{len(set(x.signature() for x in r.anomalies))} signatures", flush=True)
        for pb in r.promises_broken:
            print(f"   x {pb}", flush=True)
        store = BeliefStore("lab", root=a.beliefs)
        covered = set()
        minted = 0
        calls_log = _read_log("recon-000")
        for an in r.anomalies:
            if minted >= a.recon_hypotheses:
                break
            if an.signature() in covered:
                continue
            covered.add(an.signature())
            try:
                hyps = propose(refl, an, calls_log)
            except Exception as e:
                print(f"   ! hypothesis failed for {an.signature()}: {e}", flush=True)
                continue
            if not hyps:
                continue
            made = mint(store, hyps, an, run_id="recon-000")
            for b in made:
                if not any(h.get("event") == "signature" for h in b.history):
                    b.note("signature", an.signature())
            minted += 1
            print(f"   + {len(made)} rivals for {an.signature()}", flush=True)
        store.save()
        print(f"recon minted {minted} hypothesis sets; {store.counts()}", flush=True)

    for i in range(a.cycles):
        task_id, cfg = SCHEDULE[i % len(SCHEDULE)]
        task = tasks.get(task_id)
        run_id = f"s{i + 1:03d}-{task_id}"
        print(f"\n=== cycle {i + 1}/{a.cycles}  {task_id}  ({time.time() - t0:.0f}s elapsed) ===", flush=True)

        store = BeliefStore("lab", root=a.beliefs)
        n_before = len(store.active())

        try:
            res = run_task(task, ex, run_id=run_id, cfg=cfg, memory=True, beliefs_root=a.beliefs)
        except Exception as e:  # noqa: BLE001
            print(f"  run failed: {type(e).__name__}: {e}", flush=True)
            continue

        print(f"  {'PASS' if res.success else 'FAIL'}  {res.detail[:70]}", flush=True)
        print(f"  steps={res.steps} calls={res.tool_calls} tokens={res.total_tokens} "
              f"wall={res.wall_s}s beliefs_in_context={n_before}", flush=True)
        if res.signatures:
            print(f"  anomalies: {', '.join(res.signatures)}", flush=True)

        with Path("runs/history.jsonl").open("a") as fh:
            fh.write(json.dumps({
                "run_id": run_id, "task": task_id, "success": res.success,
                "detail": res.detail, "steps": res.steps, "tool_calls": res.tool_calls,
                "tokens": res.total_tokens, "prompt_tokens": res.prompt_tokens,
                "completion_tokens": res.completion_tokens, "wall_s": res.wall_s,
                "memory": True, "beliefs_active": n_before,
                "belief_ctx_tokens": res.belief_context_tokens,
                "signatures": res.signatures, "expected": res.expected, "got": res.got,
                "t": time.time(),
            }) + "\n")

        # mint hypotheses for the cheapest unexplained anomaly
        store = BeliefStore("lab", root=a.beliefs)
        covered = {h.get("detail") for b in store.ordered() for h in b.history
                   if h.get("event") == "signature"}
        calls = _read_log(run_id)
        minted = 0
        from agent.contract import Anomaly
        for an in res.anomalies:
            if minted >= a.max_hypotheses:
                break
            if an["signature"] in covered:
                continue
            covered.add(an["signature"])
            anomaly = Anomaly(
                kind=an["kind"], operation=an["operation"], summary=an["summary"],
                expected=an["expected"], observed=an["observed"],
                parameter=an.get("parameter"), evidence=an.get("evidence") or {},
            )
            try:
                hyps = propose(refl, anomaly, calls)
            except Exception as e:  # noqa: BLE001
                print(f"  ! hypothesis failed: {e}", flush=True)
                continue
            if not hyps:
                continue
            made = mint(store, hyps, anomaly, run_id=run_id)
            for b in made:
                if not any(h.get("event") == "signature" for h in b.history):
                    b.note("signature", an["signature"])
            minted += 1
            print(f"  + {len(made)} rivals for {an['signature']}", flush=True)
        store.save()

        # settle one open group
        for _ in range(a.probes_per_cycle):
            store = BeliefStore("lab", root=a.beliefs)
            open_h = store.hypotheses()
            if not open_h:
                break
            first = open_h[0]
            group = [first] + [store.get(r) for r in first.competing
                               if store.get(r) and store.get(r).status.value == "hypothesis"]
            group = [g for g in group if g]
            pid = f"p{i + 1:03d}"
            try:
                rec = run_probe(refl, store, group, probe_id=pid,
                                vendor=cfg.get("vendor", "Northwind"))
            except Exception as e:  # noqa: BLE001
                print(f"  ! probe failed: {e}", flush=True)
                break
            print(f"  probe {rec.template} {rec.calls} calls {rec.wall_s}s -> "
                  f"{[v.get('verdict') for v in rec.verdicts]}", flush=True)
            if rec.learned:
                print(f"    learned: {rec.learned[:100]}", flush=True)

        store = BeliefStore("lab", root=a.beliefs)
        print(f"  beliefs: {store.counts()}  usage={usage.total_tokens} tokens", flush=True)

    print(f"\ndone in {time.time() - t0:.0f}s; {usage.total_tokens} tokens", flush=True)
    print(json.dumps(usage.to_dict(), indent=2), flush=True)
    return 0


def _read_log(run_id: str) -> list[dict]:
    p = Path("runs") / f"{run_id}.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


if __name__ == "__main__":
    sys.exit(main())
