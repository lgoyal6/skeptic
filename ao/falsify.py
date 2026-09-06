"""
The falsification worker: an adversary that outguns the proposer.

Every belief in the store was produced by the same reflector that wanted it
to be true. A confirmation from the model that proposed the hypothesis is
worth very little -- it is the author marking their own homework, and it has
already gone wrong once here: a hypothesis predicted correctly while claiming
the cap depended on the API key's subscription tier, a causal story nobody
tested.

So the adversary runs on a different, stronger model, spawned as an AO worker
on Claude Code while the proposer stays on GLM. That asymmetry is the point.
An adversary no stronger than the proposer shares its blind spots and
rubber-stamps.

Its brief is inverted: not "is this belief supported" but "design the
experiment most likely to BREAK it". A belief that survives a genuine attempt
to refute it has earned `survived_falsification`. One that does not is
downgraded, whatever the proposer would prefer.

    python -m ao.falsify --apply
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from agent.beliefs import BeliefStore, Status

PROJECT = "skeptic"
WORKTREES = Path.home() / ".ao/data/worktrees" / PROJECT
OUTFILE = "falsification.json"

BRIEF = """You are the adversary in a system that reverse-engineers APIs.

Another model observed an API, formed hypotheses about how it really behaves,
ran experiments, and recorded the beliefs below as CONFIRMED. It marked its
own homework. Your job is to find where it is wrong.

For each belief, ask specifically:

  1. Does the stated claim actually follow from the evidence cited, or does it
     assert a CAUSE that was never tested? "Capped at 50" follows from a
     sweep. "Capped at 50 because of your subscription tier" does not, unless
     tiers were varied.
  2. Is the claim over-general? A cap observed for one vendor is not a global
     cap until another vendor was tried.
  3. Is there a cheaper, more boring explanation that fits the same evidence?
  4. What single experiment would most likely BREAK this belief if it is
     wrong? Be concrete: which call, which parameters, which outcome would
     refute it.

BELIEFS UNDER REVIEW
{beliefs}

THE EVIDENCE EACH ONE RESTS ON
{evidence}

Write your answer to `{outfile}` in the repository root as a single JSON
object, then stop. Do not modify any other file.

{{
  "reviews": [
    {{
      "belief_id": "<id>",
      "verdict": "sound" | "overgeneralised" | "unsupported_cause" | "wrong",
      "attack": "<the one experiment most likely to break it, concretely>",
      "because": "<one sentence, citing the specific evidence gap>"
    }}
  ]
}}

Be harsh. A belief you cannot fault is fine, but say why you could not fault
it rather than agreeing by default."""


def _fmt_beliefs(store: BeliefStore) -> tuple[str, str]:
    bl, ev = [], []
    for b in store.active():
        bl.append(
            f"  id: {b.id}\n"
            f"    class: {b.cls}  operation: {b.operation}  parameter: {b.parameter}\n"
            f"    docs claim: {b.doc_claims}\n"
            f"    belief    : {b.belief}\n"
            f"    action    : {b.action}"
        )
        probes = []
        for pid in b.probes:
            p = Path("probes") / f"{pid}.json"
            if not p.exists():
                continue
            try:
                d = json.loads(p.read_text())
            except Exception:  # noqa: BLE001
                continue
            probes.append(
                f"    probe {pid}: template={d.get('template')} calls={d.get('calls')}\n"
                f"      facts: {json.dumps(d.get('observation', {}).get('facts', {}))[:600]}\n"
                f"      learned: {d.get('learned', '')[:200]}"
            )
        ev.append(f"  {b.id}\n" + ("\n".join(probes) or "    (no probe record on disk)"))
    return "\n".join(bl), "\n".join(ev)


BRIEF_FILE = "ADVERSARY_BRIEF.md"

SHORT_PROMPT = """You are the adversary. Read `{brief}` in this repository root and
follow it exactly. Write your answer to `{out}` as described there, then stop.
Do not modify any other file."""


def spawn(prompt: str, name: str = "falsifier", harness: str = "claude-code") -> str:
    out = subprocess.run(
        ["ao", "spawn", "--project", PROJECT, "--kind", "worker",
         "--harness", harness, "--name", name, "--prompt", prompt],
        capture_output=True, text=True, timeout=120,
    )
    text = (out.stdout or "") + (out.stderr or "")
    for tok in text.split():
        if tok.startswith(f"{PROJECT}-"):
            return tok
    raise RuntimeError(f"could not parse session id from: {text[:300]}")


def await_output(session: str, timeout_s: float = 600.0, poll_s: float = 10.0) -> dict[str, Any]:
    target = WORKTREES / session / OUTFILE
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if target.exists():
            try:
                return json.loads(target.read_text())
            except json.JSONDecodeError:
                pass  # still being written
        time.sleep(poll_s)
    raise TimeoutError(f"{session} produced no {OUTFILE} within {timeout_s:.0f}s")


def apply_reviews(store: BeliefStore, reviews: list[dict[str, Any]]) -> dict[str, int]:
    tally = {"sound": 0, "downgraded": 0, "unknown": 0}
    for r in reviews:
        b = store.get(str(r.get("belief_id", "")))
        if b is None:
            tally["unknown"] += 1
            continue
        verdict = str(r.get("verdict", "")).lower()
        because = str(r.get("because", ""))[:220]
        attack = str(r.get("attack", ""))[:220]
        if verdict == "sound":
            b.survived_falsification = "adversary"
            b.note("survived_falsification", because)
            tally["sound"] += 1
        else:
            # An adversary's objection is not proof, so this does not falsify
            # outright -- it withdraws the confirmation and sends the belief
            # back to being a hypothesis with the attack recorded as the next
            # experiment to run.
            b.status = Status.HYPOTHESIS
            b.posterior.supports_doc(1.0)
            b.note("adversary_objection", f"{verdict}: {because}")
            b.note("suggested_attack", attack)
            tally["downgraded"] += 1
    store.save()
    return tally


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--beliefs", default="beliefs")
    ap.add_argument("--harness", default="claude-code")
    ap.add_argument("--timeout", type=float, default=600.0)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    store = BeliefStore("lab", root=a.beliefs)
    active = store.active()
    if not active:
        print("  no confirmed beliefs to attack")
        return 0

    bl, ev = _fmt_beliefs(store)
    brief = BRIEF.format(beliefs=bl, evidence=ev, outfile=OUTFILE)

    # AO rejects a long --prompt (PROMPT_TOO_LONG), and a worker reads from its
    # own worktree anyway, so the brief travels as a committed file and the
    # prompt is a pointer to it. The worktree is created from the branch at
    # spawn time, so the brief has to be committed before spawning.
    Path(BRIEF_FILE).write_text(brief)
    subprocess.run(["git", "add", BRIEF_FILE], check=False, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=Laksh Goyal", "-c", "user.email=laksh.g@gmicloud.ai",
         "commit", "-q", "-m", "ao: adversary brief for the current confirmed set"],
        check=False, capture_output=True,
    )

    print(f"  attacking {len(active)} confirmed beliefs")
    print(f"  proposer: glm-4-7-flash   adversary: {a.harness}")
    print(f"  brief written to {BRIEF_FILE} ({len(brief)} chars) and committed")
    sid = spawn(SHORT_PROMPT.format(brief=BRIEF_FILE, out=OUTFILE), harness=a.harness)
    print(f"  spawned {sid}, waiting for {OUTFILE}")

    try:
        data = await_output(sid, timeout_s=a.timeout)
    except TimeoutError as e:
        print(f"  ! {e}")
        return 1

    reviews = data.get("reviews", [])
    print(f"\n  {len(reviews)} reviews returned\n")
    for r in reviews:
        v = str(r.get("verdict", "?")).upper()
        print(f"    {v:18} {str(r.get('belief_id',''))[:52]}")
        print(f"    {'':18} {str(r.get('because',''))[:100]}")

    Path("probes/falsification.json").write_text(json.dumps(data, indent=2))

    if a.apply:
        tally = apply_reviews(store, reviews)
        print(f"\n  applied: {tally}")
        print(f"  beliefs: {store.counts()}")
    else:
        print("\n  (dry run; pass --apply to update the store)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
