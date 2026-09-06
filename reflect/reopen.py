"""
When every explanation is refuted, propose better ones.

A probe can refute all the rivals it was given. That does not mean the anomaly
was imaginary -- it means the true explanation was not among the candidates.
The first version of this loop treated that as the end of the road, and five
of the twelve anomalies died that way, including the pagination cap and the
bulk-write cap, which are the two most useful things this tool has to teach.

So an exhausted signature gets another round, and this time the proposer is
shown what it already got wrong: the refuted claims, the experiments that
refuted them, and the facts observed. That is strictly more information than
the first round had, which is why the second round is usually better rather
than merely different.

This is the loop closing on itself. Being wrong is supposed to be
informative.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent.beliefs import Belief, BeliefStore, Status
from agent.contract import Anomaly
from agent.llm import LLM
from reflect.hypothesis import CLASSES, Hypothesis, constrain_class, mint

REOPEN_PROMPT = """An API behaves differently from its documentation. Earlier
explanations for this were tested by experiment and ALL of them were refuted.
Propose better ones.

THE ANOMALY
  signature : {signature}
  operation : {op}   parameter: {param}
  happened  : {summary}
  docs claim: {expected}
  observed  : {observed}
  evidence  : {evidence}

WHAT WAS ALREADY TRIED AND REFUTED
{refuted}

WHAT THE EXPERIMENTS ACTUALLY SAW
{facts}

The anomaly is real -- it was detected on the wire. Something explains it.
Your earlier explanations did not survive contact with the evidence above, so
do not restate them in different words.

Give 2 or 3 NEW competing explanations that are consistent with everything
observed so far, including the results that killed the previous round. For
each, state the observable consequence that would distinguish it.

Class must be one of: {classes}

Reply with a single JSON object:
{{
  "hypotheses": [
    {{
      "belief": "one precise sentence about how the API really behaves",
      "class": "<one of the classes>",
      "parameter": "<the parameter involved, or null>",
      "distinguishing_test": "what to do and what each outcome would mean",
      "action": "what an agent should do about it in future runs",
      "prior": 0.0,
      "differs_from_previous": "<why this is not a rewording of a refuted claim>"
    }}
  ]
}}"""


def signature_of(b: Belief) -> str | None:
    return next((h.get("detail") for h in b.history if h.get("event") == "signature"), None)


def exhausted(store: BeliefStore) -> dict[str, list[Belief]]:
    """Signatures where every hypothesis was refuted and none confirmed."""
    groups: dict[str, list[Belief]] = {}
    for b in store.ordered():
        sig = signature_of(b)
        if sig:
            groups.setdefault(sig, []).append(b)
    return {
        sig: bs for sig, bs in groups.items()
        if bs
        and not any(b.status in (Status.CONFIRMED, Status.HYPOTHESIS) for b in bs)
        and any(b.status is Status.FALSIFIED for b in bs)
    }


def _probe_evidence(beliefs: list[Belief], probes_dir: str = "probes") -> tuple[str, str]:
    """What was refuted, and what the experiments saw."""
    refuted_lines, fact_lines = [], []
    seen_probes: set[str] = set()
    for b in beliefs:
        why = next(
            (h.get("detail") for h in reversed(b.history)
             if h.get("event") in ("falsified", "probe_inconclusive")),
            "",
        )
        refuted_lines.append(f"  - {b.belief}\n      refuted because: {why}")
        for pid in b.probes:
            if pid in seen_probes:
                continue
            seen_probes.add(pid)
            p = Path(probes_dir) / f"{pid}.json"
            if not p.exists():
                continue
            try:
                d = json.loads(p.read_text())
            except Exception:  # noqa: BLE001
                continue
            facts = json.dumps((d.get("observation") or {}).get("facts", {}))[:600]
            fact_lines.append(f"  {pid} ({d.get('template')}): {facts}")
    return "\n".join(refuted_lines), "\n".join(fact_lines) or "  (no probe records on disk)"


def rebuild_anomaly(sig: str, beliefs: list[Belief]) -> Anomaly:
    """Reconstruct enough of the original anomaly to re-propose against it."""
    op, param, kind = (sig.split(".", 2) + ["_", ""])[:3]
    b = beliefs[0]
    return Anomaly(
        kind=kind,
        operation=op,
        parameter=None if param == "_" else param,
        summary=f"{op} contradicted the documentation ({kind})",
        expected=b.doc_claims or "(documented behaviour)",
        observed="see the experiment facts",
        evidence={},
    )


def repropose(
    reflector: LLM,
    store: BeliefStore,
    sig: str,
    beliefs: list[Belief],
    probes_dir: str = "probes",
    tool: str = "lab",
) -> list[Belief]:
    anomaly = rebuild_anomaly(sig, beliefs)
    refuted, facts = _probe_evidence(beliefs, probes_dir)

    data = reflector.json_chat(
        [
            {"role": "system", "content": "You are a careful API reverse-engineer. Being refuted is information; use it."},
            {"role": "user", "content": REOPEN_PROMPT.format(
                signature=sig, op=anomaly.operation, param=anomaly.parameter or "none",
                summary=anomaly.summary, expected=anomaly.expected,
                observed=anomaly.observed, evidence="{}",
                refuted=refuted, facts=facts, classes=", ".join(CLASSES),
            )},
        ],
        max_tokens=14000,
    )

    hyps: list[Hypothesis] = []
    previous = {b.belief.strip().lower() for b in beliefs}
    for h in (data.get("hypotheses") or [])[:3]:
        belief = str(h.get("belief", "")).strip()
        if not belief or belief.lower() in previous:
            continue  # a reworded corpse is not a new hypothesis
        param = h.get("parameter")
        if isinstance(param, str) and param.lower() in ("null", "none", ""):
            param = None
        hyps.append(Hypothesis(
            belief=belief,
            cls=constrain_class(anomaly.kind, str(h.get("class", "")).strip()),
            parameter=param if param is not None else anomaly.parameter,
            distinguishing_test=str(h.get("distinguishing_test", "")).strip(),
            action=str(h.get("action", "")).strip(),
            prior=float(h.get("prior", 0.0) or 0.0),
            anomaly_signature=sig,
            operation=anomaly.operation,
        ))
    if not hyps:
        return []

    made = mint(store, hyps, anomaly, run_id=f"reopen-{sig}", tool=tool)
    for b in made:
        if not any(x.get("event") == "signature" for x in b.history):
            b.note("signature", sig)
        b.note("second_round", "proposed after every earlier explanation was refuted")
    return made


def main() -> int:
    import sys

    from agent.llm import build

    _ex, refl, _usage = build()
    store = BeliefStore("lab")
    groups = exhausted(store)
    if not groups:
        print("  no exhausted signatures")
        return 0

    print(f"  {len(groups)} exhausted signatures, re-proposing with the refuting evidence\n")
    total = 0
    for sig, bs in groups.items():
        try:
            made = repropose(refl, store, sig, bs)
        except Exception as e:  # noqa: BLE001
            print(f"    ! {sig}: {type(e).__name__}: {e}")
            continue
        total += len(made)
        print(f"    {sig}: {len(made)} new hypotheses")
        for b in made:
            print(f"        {b.belief[:88]}")
    store.save()
    print(f"\n  {total} new hypotheses; {store.counts()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
