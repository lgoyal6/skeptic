"""
skeptic -- the deterministic command surface.

The AO orchestrator decides WHICH of these to invoke and which hypothesis to
fund. It does not implement the loop itself. That split matters: a
non-deterministic orchestrator making judgement calls is fine, but a
non-deterministic orchestrator that could reorder the measurement would make
`make bench SEED=n` meaningless.

  skeptic run      one task run, detect anomalies, mint rival hypotheses
  skeptic probe    design and run an experiment to settle open hypotheses
  skeptic cycle    run/probe until the budget is spent -- the learning curve
  skeptic status   what is believed, and how sure
  skeptic bench    score beliefs against ground truth
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
from agent.beliefs import BeliefStore, Status
from agent.executor import run_task
from agent.llm import Usage, build
from reflect.hypothesis import mint, propose
from reflect.probe import run_probe

LAB = "http://127.0.0.1:8077"
HISTORY = Path("runs/history.jsonl")


def _reset_lab(seed: int) -> None:
    httpx.post(f"{LAB}/_control/reset", json={"seed": seed}, timeout=30.0)


def _record(entry: dict[str, Any]) -> None:
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY.open("a") as fh:
        fh.write(json.dumps(entry) + "\n")


def _next_run_id(prefix: str = "run") -> str:
    n = 0
    if HISTORY.exists():
        n = sum(1 for _ in HISTORY.open())
    return f"{prefix}-{n + 1:03d}"


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def cmd_run(a: argparse.Namespace) -> int:
    ex, refl, usage = build()
    task = tasks.get(a.task)
    run_id = a.run_id or _next_run_id()

    if a.reset:
        _reset_lab(a.seed)

    res = run_task(
        task, ex, run_id=run_id, cfg={"vendor": a.vendor, "n": a.n, "seed": a.seed},
        memory=not a.no_memory, beliefs_root=a.beliefs, verbose=a.verbose,
    )

    print(f"  {run_id}  {task.id}  {'PASS' if res.success else 'FAIL'}  {res.detail}")
    print(f"    steps={res.steps} calls={res.tool_calls} tokens={res.total_tokens} "
          f"wall={res.wall_s}s beliefs_used={res.beliefs_active}")
    if res.signatures:
        print(f"    anomalies: {', '.join(res.signatures)}")

    minted = 0
    if not a.no_learn and res.anomalies:
        store = BeliefStore("lab", root=a.beliefs)
        known = {s for b in store.ordered() for s in [b.id]}
        seen_sigs = {
            n.get("detail", "") for b in store.ordered() for n in b.history
        }
        calls = _read_run_log(run_id)
        handled: set[str] = set()
        # one hypothesis set per distinct signature, cheapest first
        for an in res.anomalies:
            sig = an["signature"]
            if sig in handled or sig in _covered_signatures(store):
                continue
            handled.add(sig)
            if minted >= a.max_hypotheses:
                break
            from agent.contract import Anomaly

            anomaly = Anomaly(
                kind=an["kind"], operation=an["operation"], summary=an["summary"],
                expected=an["expected"], observed=an["observed"],
                parameter=an.get("parameter"), evidence=an.get("evidence") or {},
            )
            try:
                hyps = propose(refl, anomaly, calls)
            except Exception as e:  # noqa: BLE001
                print(f"    ! hypothesis generation failed for {sig}: {e}")
                continue
            if not hyps:
                continue
            made = mint(store, hyps, anomaly, run_id=run_id)
            _tag_signature(made, sig)
            minted += 1
            print(f"    + {len(made)} rival hypotheses for {sig}")
        store.save()

    _record({
        "run_id": run_id, "task": task.id, "success": res.success, "detail": res.detail,
        "steps": res.steps, "tool_calls": res.tool_calls, "tokens": res.total_tokens,
        "prompt_tokens": res.prompt_tokens, "completion_tokens": res.completion_tokens,
        "wall_s": res.wall_s, "memory": res.memory_enabled,
        "beliefs_active": res.beliefs_active, "belief_ctx_tokens": res.belief_context_tokens,
        "signatures": res.signatures, "hypothesis_sets_minted": minted,
        "expected": res.expected, "got": res.got, "error": res.error,
        "t": time.time(),
    })
    return 0


def _read_run_log(run_id: str) -> list[dict[str, Any]]:
    p = Path("runs") / f"{run_id}.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def _covered_signatures(store: BeliefStore) -> set[str]:
    out = set()
    for b in store.ordered():
        if b.status in (Status.CONFIRMED, Status.RETIRED):
            for h in b.history:
                if h.get("event") == "signature":
                    out.add(h.get("detail", ""))
    return out


def _tag_signature(beliefs: list[Any], sig: str) -> None:
    for b in beliefs:
        if not any(h.get("event") == "signature" for h in b.history):
            b.note("signature", sig)


# ---------------------------------------------------------------------------
# probe
# ---------------------------------------------------------------------------


def cmd_probe(a: argparse.Namespace) -> int:
    ex, refl, usage = build()
    store = BeliefStore("lab", root=a.beliefs)

    open_hyps = store.hypotheses()
    if not open_hyps:
        print("  no open hypotheses to settle")
        return 0

    # group rivals: a belief plus the hypotheses it competes with
    groups: list[list[Any]] = []
    used: set[str] = set()
    for b in open_hyps:
        if b.id in used:
            continue
        group = [b] + [store.get(r) for r in b.competing if store.get(r) and store.get(r).status is Status.HYPOTHESIS]
        group = [g for g in group if g]
        for g in group:
            used.add(g.id)
        groups.append(group)

    if a.belief:
        groups = [g for g in groups if any(x.id == a.belief for x in g)]

    n = 0
    for group in groups[: a.max_probes]:
        pid = f"probe-{int(time.time() * 1000) % 10_000_000}-{n}"
        print(f"  probing {len(group)} rival(s) -> {pid}")
        for g in group:
            print(f"    ? {g.belief[:88]}")
        try:
            rec = run_probe(refl, store, group, probe_id=pid, vendor=a.vendor,
                            probes_dir=a.probes)
        except Exception as e:  # noqa: BLE001
            print(f"    ! probe failed: {e}")
            continue
        n += 1
        print(f"    {rec.template} ({rec.calls} calls, {rec.wall_s}s)")
        for v in rec.verdicts:
            print(f"      {str(v.get('verdict','?')).upper():13} {str(v.get('because',''))[:90]}")
        if rec.learned:
            print(f"      learned: {rec.learned[:110]}")
    return 0


# ---------------------------------------------------------------------------
# cycle
# ---------------------------------------------------------------------------


def cmd_cycle(a: argparse.Namespace) -> int:
    """The learning curve: alternate task runs and experiments."""
    for i in range(a.n):
        print(f"\n=== cycle {i + 1}/{a.n} ===")
        run_args = argparse.Namespace(
            task=a.task, run_id=None, vendor=a.vendor, n=a.rows, seed=a.seed,
            no_memory=False, no_learn=False, beliefs=a.beliefs, verbose=False,
            reset=a.reset_each, max_hypotheses=a.max_hypotheses,
        )
        cmd_run(run_args)
        probe_args = argparse.Namespace(
            beliefs=a.beliefs, probes=a.probes, vendor=a.vendor,
            belief=None, max_probes=a.probes_per_cycle,
        )
        cmd_probe(probe_args)
    return 0


# ---------------------------------------------------------------------------
# status / bench
# ---------------------------------------------------------------------------


def cmd_status(a: argparse.Namespace) -> int:
    store = BeliefStore("lab", root=a.beliefs)
    c = store.counts()
    print(f"\n  beliefs about `lab`: {c}\n")
    for b in store.ordered():
        mark = {"confirmed": "+", "hypothesis": "?", "falsified": "x", "retired": "-"}[b.status.value]
        print(f"  {mark} [{b.cls}] p(doc)={b.posterior.p_doc_correct:.2f} "
              f"for={b.evidence_for} against={b.evidence_against}")
        print(f"      {b.belief[:100]}")
        if b.action and b.status is Status.CONFIRMED:
            print(f"      -> {b.action[:96]}")
    ctx = store.render_for_context()
    print(f"\n  context block: {store.context_tokens()} tokens, {len(store.active())} active beliefs\n")
    if a.context:
        print(ctx)
    return 0


def cmd_bench(a: argparse.Namespace) -> int:
    from bench.score import render, score

    s = score(beliefs_root=a.beliefs)
    if a.json:
        print(json.dumps(s, indent=2))
    else:
        print(render(s))
    return 0


# ---------------------------------------------------------------------------


def main() -> int:
    p = argparse.ArgumentParser(prog="skeptic")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--beliefs", default="beliefs")
        sp.add_argument("--probes", default="probes")
        sp.add_argument("--vendor", default="Northwind")

    r = sub.add_parser("run", help="one task run")
    common(r)
    r.add_argument("--task", default="inventory")
    r.add_argument("--run-id", default=None)
    r.add_argument("--n", type=int, default=73)
    r.add_argument("--seed", type=int, default=1337)
    r.add_argument("--no-memory", action="store_true")
    r.add_argument("--no-learn", action="store_true")
    r.add_argument("--reset", action="store_true")
    r.add_argument("--verbose", action="store_true")
    r.add_argument("--max-hypotheses", type=int, default=2)
    r.set_defaults(func=cmd_run)

    pr = sub.add_parser("probe", help="settle open hypotheses")
    common(pr)
    pr.add_argument("--belief", default=None)
    pr.add_argument("--max-probes", type=int, default=2)
    pr.set_defaults(func=cmd_probe)

    cy = sub.add_parser("cycle", help="run and probe repeatedly")
    common(cy)
    cy.add_argument("--n", type=int, default=5)
    cy.add_argument("--task", default="inventory")
    cy.add_argument("--rows", type=int, default=73)
    cy.add_argument("--seed", type=int, default=1337)
    cy.add_argument("--reset-each", action="store_true")
    cy.add_argument("--max-hypotheses", type=int, default=2)
    cy.add_argument("--probes-per-cycle", type=int, default=2)
    cy.set_defaults(func=cmd_cycle)

    st = sub.add_parser("status", help="what is believed")
    common(st)
    st.add_argument("--context", action="store_true", help="print the context block too")
    st.set_defaults(func=cmd_status)

    bn = sub.add_parser("bench", help="score against ground truth")
    common(bn)
    bn.add_argument("--json", action="store_true")
    bn.set_defaults(func=cmd_bench)

    a = p.parse_args()
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
