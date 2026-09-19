"""
Settle every open hypothesis, several experiments at a time.

A probe spends a couple of seconds calling the tool and several minutes
waiting on the model. Running them one after another wastes almost all of the
wall clock on I/O, so this fans out across threads and applies the verdicts
serially afterwards -- the belief store is one YAML file, and concurrent
writers would race.

Each worker gets its own vendor namespace so experiments cannot disturb each
other's data.

    ./.venv/bin/python -u -m bench.settle --workers 4
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from agent.adapters.budget import RateBudget
from agent.beliefs import BeliefStore, Status
from agent.llm import build
from reflect.probe import _apply, run_probe

PRINT_LOCK = threading.Lock()


def say(msg: str) -> None:
    with PRINT_LOCK:
        print(msg, flush=True)


def groups(store: BeliefStore) -> list[list[Any]]:
    """Rival sets: a hypothesis plus the hypotheses it competes with."""
    out: list[list[Any]] = []
    used: set[str] = set()
    for b in store.hypotheses():
        if b.id in used:
            continue
        g = [b] + [
            store.get(r) for r in b.competing
            if store.get(r) and store.get(r).status is Status.HYPOTHESIS
        ]
        g = [x for x in g if x]
        for x in g:
            used.add(x.id)
        out.append(g)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    # The tool throttles at 3 req/s, globally, across every client hitting the
    # process. Each adapter used to pace itself at 0.4s as if it were the only
    # one, so four workers put ~10 req/s on a budget of 3 and the audit
    # measured 23 of 32 calls coming back 429: the probes were largely
    # measuring the rate limiter rather than their own variable. Dropping the
    # worker count shrank the overshoot without removing it -- two workers at
    # 0.4s is still ~5 req/s -- because a per-adapter pacer cannot see its
    # siblings. One `RateBudget`, shared by reference, can, so the worker
    # count is now a concurrency choice rather than a rate-limit workaround.
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--max-groups", type=int, default=20)
    ap.add_argument("--beliefs", default="beliefs")
    ap.add_argument("--rate-limit", type=int, default=3,
                    help="requests per second the whole fleet may spend (the tool's global limit)")
    a = ap.parse_args()

    budget = RateBudget(n=a.rate_limit, window_s=1.0)

    ex, refl, usage = build()
    store = BeliefStore("lab", root=a.beliefs)
    gs = groups(store)[: a.max_groups]
    if not gs:
        say("  nothing open to settle")
        return 0

    say(f"  {len(gs)} rival groups, {a.workers} at a time, {a.rate_limit} req/s shared")
    t0 = time.time()
    done: list[tuple[Any, list[Any]]] = []

    def work(i: int, group: list[Any]):
        pid = f"probe-{i:03d}"
        vendor = f"Probe{i:02d}"
        say(f"    [{pid}] {len(group)} rivals -> {group[0].cls}")
        rec = run_probe(refl, store, group, probe_id=pid, vendor=vendor,
                        apply=False, budget=budget)
        verdicts = [v.get("verdict") for v in rec.verdicts]
        say(f"    [{pid}] {rec.template} {rec.calls} calls {rec.wall_s:.0f}s -> {verdicts}")
        return rec, group

    with ThreadPoolExecutor(max_workers=a.workers) as pool:
        futs = {pool.submit(work, i, g): i for i, g in enumerate(gs)}
        for f in as_completed(futs):
            try:
                done.append(f.result())
            except Exception as e:  # noqa: BLE001
                say(f"    ! probe failed: {type(e).__name__}: {e}")

    # apply serially: one writer, no races
    fresh = BeliefStore("lab", root=a.beliefs)
    for rec, _group in done:
        learned = getattr(rec, "_learned_payload", {}) or {}
        _apply(fresh, rec, adversarial=False, learned=learned)
    fresh.save()

    say(f"\n  settled {len(done)} groups in {time.time() - t0:.0f}s")
    say(f"  beliefs: {fresh.counts()}")
    say(f"  reflector: {usage.by_role.get('reflector')}")
    say(f"  request budget: {budget.stats()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
