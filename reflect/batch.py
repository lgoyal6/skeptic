"""
Batch hypothesis generation.

The first version called the reflector once per anomaly. Recon surfaces
thirteen at a stroke, so that was thirteen slow round trips to explain a set
of findings the model could reason about together -- and reasoning about them
together is actually better, because a model that sees "titles truncate" and
"bulk silently drops rows" in one view can notice they are the same class of
defect.

One call, all anomalies, rival hypotheses for each. Roughly an order of
magnitude fewer reflector calls for the same output, which is most of the
answer to "cost-effectiveness and speed".
"""

from __future__ import annotations

import json
from typing import Any

from agent.beliefs import Belief, BeliefStore
from agent.contract import Anomaly
from agent.llm import LLM
from reflect.hypothesis import CLASSES, Hypothesis

BATCH_PROMPT = """An API's documentation disagrees with its real behaviour in several places.
Explain each one.

ANOMALIES
{anomalies}

For EACH anomaly give 2 or 3 COMPETING explanations that fit its evidence.
They must be genuinely rival: if one experiment would confirm two of them,
they are one hypothesis, not two. For each, state the observable consequence
that would distinguish it -- something checkable in one or two cheap calls.

Several anomalies may share a root cause. Say so in `related_to` when you
think two anomalies are the same defect seen from different angles.

Class must be one of: {classes}

Reply with a single JSON object:
{{
  "findings": [
    {{
      "signature": "<the anomaly signature this explains>",
      "related_to": ["<other signature>"],
      "hypotheses": [
        {{
          "belief": "one precise sentence about how the API really behaves",
          "class": "<one of the classes>",
          "parameter": "<parameter involved, or null>",
          "distinguishing_test": "what to do and what each outcome would mean",
          "action": "what an agent should do about it in future runs",
          "prior": 0.0
        }}
      ]
    }}
  ]
}}"""


def _fmt(anomalies: list[Anomaly]) -> str:
    out = []
    for a in anomalies:
        out.append(
            f"- signature: {a.signature()}\n"
            f"    operation: {a.operation}  parameter: {a.parameter or 'none'}\n"
            f"    happened : {a.summary}\n"
            f"    docs say : {a.expected}\n"
            f"    observed : {a.observed}\n"
            f"    evidence : {json.dumps(a.evidence)[:220]}"
        )
    return "\n".join(out)


def propose_batch(
    reflector: LLM,
    anomalies: list[Anomaly],
    max_anomalies: int = 14,
    max_tokens: int = 16000,
) -> dict[str, list[Hypothesis]]:
    """signature -> rival hypotheses, in a single reflector call."""
    picked: list[Anomaly] = []
    seen: set[str] = set()
    for a in anomalies:
        if a.signature() in seen:
            continue
        seen.add(a.signature())
        picked.append(a)
        if len(picked) >= max_anomalies:
            break

    data = reflector.json_chat(
        [
            {"role": "system", "content": "You are a careful API reverse-engineer. You never assert what you have not tested."},
            {"role": "user", "content": BATCH_PROMPT.format(
                anomalies=_fmt(picked), classes=", ".join(CLASSES)
            )},
        ],
        max_tokens=max_tokens,
    )

    by_sig = {a.signature(): a for a in picked}
    out: dict[str, list[Hypothesis]] = {}
    for f in data.get("findings", []) or []:
        sig = str(f.get("signature", "")).strip()
        anomaly = by_sig.get(sig)
        if anomaly is None:
            continue
        hyps: list[Hypothesis] = []
        for h in (f.get("hypotheses") or [])[:3]:
            cls = str(h.get("class", "")).strip()
            if cls not in CLASSES:
                continue
            param = h.get("parameter")
            if isinstance(param, str) and param.lower() in ("null", "none", ""):
                param = None
            belief = str(h.get("belief", "")).strip()
            if not belief:
                continue
            hyps.append(Hypothesis(
                belief=belief,
                cls=cls,
                parameter=param if param is not None else anomaly.parameter,
                distinguishing_test=str(h.get("distinguishing_test", "")).strip(),
                action=str(h.get("action", "")).strip(),
                prior=float(h.get("prior", 0.0) or 0.0),
                anomaly_signature=sig,
                operation=anomaly.operation,
            ))
        if hyps:
            out[sig] = hyps
    return out


def mint_batch(
    store: BeliefStore,
    grouped: dict[str, list[Hypothesis]],
    anomalies: list[Anomaly],
    run_id: str,
    tool: str = "lab",
) -> int:
    from reflect.hypothesis import mint

    by_sig = {a.signature(): a for a in anomalies}
    n = 0
    for sig, hyps in grouped.items():
        anomaly = by_sig.get(sig)
        if anomaly is None:
            continue
        made = mint(store, hyps, anomaly, run_id=run_id, tool=tool)
        for b in made:
            if not any(h.get("event") == "signature" for h in b.history):
                b.note("signature", sig)
        n += 1
    store.save()
    return n
