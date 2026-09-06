"""
The memory-wipe ablation: the control that makes "memory helps" falsifiable.

The project's headline claim is that the agent improves because its belief
memory grows. The obvious objection is that the model just got luckier, or
warmed to the task -- nothing to do with what it supposedly learned. This
file is the control that answers that: run the identical agent on the
identical tasks against the identical world, with the belief memory erased
or corrupted, and see whether performance actually depends on it.

Two controls, not one, because one alone is confounded:

  wiped     run_task(memory=False) -- no belief context at all. Removing
            memory also removes ~N tokens of prompt, so a gap between
            `memory` and `wiped` could just be an artefact of prompt
            length, not of the knowledge the prompt carries.

  shuffled  run_task(memory=True) against a CORRUPTED belief store: the same
            number of confirmed beliefs, rendered to roughly the same token
            cost, but with each belief's claim and remedy swapped onto
            another belief's operation -- fluent, plausible, and wrong.
            This holds prompt length roughly constant while destroying the
            information. If `memory` beats `shuffled`, the knowledge is
            doing the work, not the token count.

Same task suite, same world reset, same executor as bench/ab.py -- this file
only adds the third arm and the belief-corruption step.
"""

from __future__ import annotations

import argparse
import copy
import json
import random
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from agent import tasks
from agent.beliefs import BeliefStore
from agent.executor import run_task
from agent.llm import build

from bench.ab import SUITE, _reset_world


def _make_shuffled_store(real_root: str, tmp_root: str) -> BeliefStore:
    """Corrupt a copy of the real belief store into `tmp_root`.

    Deep-copies every confirmed belief, then derives a permutation of the
    list with no fixed points and swaps `belief`/`action` text along it, so
    belief i ends up carrying belief perm(i)'s claim and remedy. Everything
    else -- id, class, operation, status, posterior -- is untouched, so the
    corrupted store has the same count of confirmed beliefs, referencing the
    same operations, and renders to close to the same token budget as the
    real one. Only the information content is destroyed.
    """
    real = BeliefStore("lab", root=real_root)
    active = [copy.deepcopy(b) for b in real.active()]

    shuffled = BeliefStore("lab", root=tmp_root)
    if len(active) <= 1:
        # nothing to scramble against; ship the (trivial) store unchanged
        # rather than loop forever looking for a derangement that can't exist
        shuffled.beliefs = {b.id: b for b in active}
        shuffled.save()
        return shuffled

    n = len(active)
    rng = random.Random(1337)
    perm = list(range(n))
    while True:  # derangement: no belief keeps its own text
        rng.shuffle(perm)
        if all(perm[i] != i for i in range(n)):
            break

    # snapshot original text before mutating -- assigning in place while
    # reading donors from the same list would let an already-overwritten
    # belief get picked up as a later donor, silently un-deranging it
    originals = [(b.belief, b.action) for b in active]
    for i, b in enumerate(active):
        b.belief, b.action = originals[perm[i]]

    shuffled.beliefs = {b.id: b for b in active}
    shuffled.save()
    return shuffled


def run_arm(
    label: str,
    memory: bool,
    beliefs_root: str,
    repeats: int = 1,
    verbose: bool = True,
) -> dict[str, Any]:
    ex, _refl, usage = build()
    store = BeliefStore("lab", root=beliefs_root)
    ctx_tokens = store.context_tokens() if memory else 0
    n_active = len(store.active()) if memory else 0

    rows: list[dict[str, Any]] = []
    p0, c0 = usage.prompt_tokens, usage.completion_tokens
    t0 = time.time()

    for rep in range(repeats):
        for task_id, cfg in SUITE:
            _reset_world()
            task = tasks.get(task_id)
            run_id = f"ablate-{label}-{task_id}-{rep}"
            try:
                res = run_task(
                    task, ex, run_id=run_id, cfg=cfg,
                    memory=memory,
                    beliefs_root=beliefs_root,
                    runs_dir="runs",
                )
                row = {
                    "task": task_id, "rep": rep, "ok": res.success, "detail": res.detail,
                    "calls": res.tool_calls, "tokens": res.total_tokens,
                    "wall_s": res.wall_s, "expected": res.expected, "got": res.got,
                    "belief_context_tokens": res.belief_context_tokens,
                    "beliefs_active": res.beliefs_active,
                }
            except Exception as e:  # noqa: BLE001 - a crashed run is a failure, not a stop
                row = {
                    "task": task_id, "rep": rep, "ok": False, "detail": f"crashed: {type(e).__name__}: {e}",
                    "calls": 0, "tokens": 0, "wall_s": 0.0, "expected": None, "got": None,
                    "belief_context_tokens": 0, "beliefs_active": 0,
                }
            rows.append(row)
            if verbose:
                mark = "PASS" if row["ok"] else "FAIL"
                print(f"    [{label:8}] {task_id:14} rep={rep} {mark}  "
                      f"calls={row['calls']:>3} tokens={row['tokens']:>7}  {str(row['detail'])[:44]}",
                      flush=True)

    passed = sum(1 for r in rows if r["ok"])
    return {
        "arm": label,
        "memory": memory,
        "beliefs_root": beliefs_root,
        "context_tokens": ctx_tokens,
        "beliefs_active": n_active,
        "passed": passed,
        "total": len(rows),
        "calls": sum(r["calls"] for r in rows),
        "tokens": usage.prompt_tokens + usage.completion_tokens - p0 - c0,
        "wall_s": round(time.time() - t0, 1),
        "rows": rows,
    }


def render(results: dict[str, dict[str, Any]], repeats: int) -> str:
    arms = ["memory", "wiped", "shuffled"]
    label_w, col_w = 16, 11

    L = ["", "  memory-wipe ablation -- is it the knowledge, or just the tokens?", ""]
    L.append("  " + " " * label_w + "".join(f"{a:>{col_w}}" for a in arms))
    rule = "  " + "-" * (label_w + col_w * len(arms))
    L.append(rule)

    for task_id, _cfg in SUITE:
        cells = []
        for a in arms:
            task_rows = [r for r in results[a]["rows"] if r["task"] == task_id]
            passed = sum(1 for r in task_rows if r["ok"])
            cells.append(f"{passed}/{len(task_rows)}")
        L.append(f"  {task_id:<{label_w}}" + "".join(f"{c:>{col_w}}" for c in cells))
    L.append(rule)

    def row(label: str, values: list[str]) -> str:
        return f"  {label:<{label_w}}" + "".join(f"{v:>{col_w}}" for v in values)

    L.append(row("passed", [f"{results[a]['passed']}/{results[a]['total']}" for a in arms]))
    L.append(row("tool calls", [str(results[a]["calls"]) for a in arms]))
    L.append(row("tokens", [f"{results[a]['tokens']:,}" for a in arms]))
    L.append(row("context tokens", [str(results[a]["context_tokens"]) for a in arms]))
    L.append("")

    mem, wiped, shuf = results["memory"]["passed"], results["wiped"]["passed"], results["shuffled"]["passed"]
    total = results["memory"]["total"]
    n_note = f" (n={repeats} repeat{'s' if repeats != 1 else ''} per task; treat as noisy below --repeats 3)" if repeats < 3 else ""

    if mem > wiped and mem > shuf:
        reading = (
            f"reading: memory ({mem}/{total}) beat both wiped ({wiped}/{total}) and the "
            f"token-matched shuffled arm ({shuf}/{total}); the gain looks like the knowledge "
            f"itself, not just longer prompts or extra tokens.{n_note}"
        )
    elif mem > wiped and mem <= shuf:
        reading = (
            f"reading: memory ({mem}/{total}) beat wiped ({wiped}/{total}) but did not clearly "
            f"beat shuffled ({shuf}/{total}), which costs the same tokens but carries false "
            f"claims -- this run does NOT separate 'more tokens' from 'true knowledge'; the "
            f"headline memory claim is unconfirmed by this ablation.{n_note}"
        )
    else:
        reading = (
            f"reading: memory ({mem}/{total}) did not clearly beat wiped ({wiped}/{total}) here, "
            f"so this run gives no evidence that belief memory is driving performance -- it may "
            f"be small-sample noise, but it is not a confirmation either.{n_note}"
        )
    L.append("  " + reading)
    L.append("")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--beliefs", default="beliefs")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--out", default="bench/ablate_result.json")
    a = ap.parse_args()

    real_store = BeliefStore("lab", root=a.beliefs)
    if not real_store.active():
        print("  no confirmed beliefs in the real store yet; run a session first")
        return 1

    tmp_root = tempfile.mkdtemp(prefix="skeptic-ablate-")
    try:
        _make_shuffled_store(a.beliefs, tmp_root)

        print(f"\n  {len(real_store.active())} confirmed beliefs; running {len(SUITE)} tasks "
              f"x {a.repeats} repeat(s) x 3 arms (memory, wiped, shuffled)\n")

        results = {
            "memory": run_arm("memory", True, a.beliefs, a.repeats),
            "wiped": run_arm("wiped", False, a.beliefs, a.repeats),
            "shuffled": run_arm("shuffled", True, tmp_root, a.repeats),
        }
    finally:
        # never leave the corrupted copy behind; the real beliefs/ dir is
        # never touched by this file in the first place
        shutil.rmtree(tmp_root, ignore_errors=True)

    out = render(results, a.repeats)
    print(out)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(results, indent=2, default=str))
    print(f"  written to {a.out}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
