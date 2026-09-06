"""
From anomaly to competing hypotheses.

An anomaly says "the docs and reality disagree here". It does not say why.
Usually there are several explanations that fit the same evidence, and the
whole point of this project is that you do not get to pick one by vibes --
you design an experiment that separates them.

So this module produces a SET of rival hypotheses, each with the observable
consequence that would distinguish it. The probe designer consumes those
consequences. If the model returns only one hypothesis for an anomaly that
genuinely has rivals, the probe has nothing to discriminate and the belief
stays unconfirmed, which is the correct outcome.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from agent.beliefs import Belief, BeliefStore
from agent.contract import Anomaly
from agent.llm import LLM

# Anomaly kind -> the ground-truth class vocabulary. The reflector must pick
# from this list, so a belief's class is always scoreable.
CLASSES = [
    "silent_truncation",
    "silent_coercion",
    "eventual_consistency",
    "expiry",
    "undocumented_flag",
    "mislabelled_semantics",
    "idempotency_hazard",
    "rate_limit",
]

PROMPT = """An API's documentation and its real behaviour disagree. Explain why.

ANOMALY
  operation : {op}
  parameter : {param}
  what happened: {summary}
  documentation promises: {expected}
  actually observed: {observed}
  evidence: {evidence}

RECENT CALLS (most recent last)
{calls}

Give 2 or 3 COMPETING explanations that all fit this evidence. They must be
genuinely rival: if the same experiment would confirm two of them, they are
one hypothesis, not two.

For each, state the observable consequence that would distinguish it from the
others -- something you could check with one or two cheap API calls.

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
      "prior": 0.0
    }}
  ]
}}
`prior` is your belief that this explanation is the right one, and the priors
should sum to about 1.0."""


@dataclass
class Hypothesis:
    belief: str
    cls: str
    parameter: str | None
    distinguishing_test: str
    action: str
    prior: float
    anomaly_signature: str
    operation: str

    def belief_id(self, tool: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "_", self.belief.lower())[:44].strip("_")
        return f"{tool}.{self.operation}.{slug}"


def _fmt_calls(calls: list[dict[str, Any]], n: int = 8) -> str:
    out = []
    for c in calls[-n:]:
        req = json.dumps(c.get("request"))[:160]
        resp = json.dumps(c.get("response"))[:200]
        out.append(f"  #{c.get('n')} {c.get('op')} {req} -> {c.get('status')} {resp}")
    return "\n".join(out) or "  (none)"


def propose(
    reflector: LLM,
    anomaly: Anomaly,
    calls: list[dict[str, Any]],
    tool: str = "lab",
) -> list[Hypothesis]:
    msg = PROMPT.format(
        op=anomaly.operation,
        param=anomaly.parameter or "none",
        summary=anomaly.summary,
        expected=anomaly.expected,
        observed=anomaly.observed,
        evidence=json.dumps(anomaly.evidence)[:400],
        calls=_fmt_calls(calls),
        classes=", ".join(CLASSES),
    )
    data = reflector.json_chat(
        [
            {"role": "system", "content": "You are a careful API reverse-engineer. You never assert what you have not tested."},
            {"role": "user", "content": msg},
        ],
        max_tokens=6000,
    )

    out: list[Hypothesis] = []
    for h in data.get("hypotheses", [])[:3]:
        cls = constrain_class(anomaly.kind, str(h.get("class", "")).strip())
        param = h.get("parameter")
        if isinstance(param, str) and param.lower() in ("null", "none", ""):
            param = None
        out.append(
            Hypothesis(
                belief=str(h.get("belief", "")).strip(),
                cls=cls,
                parameter=param if param is not None else anomaly.parameter,
                distinguishing_test=str(h.get("distinguishing_test", "")).strip(),
                action=str(h.get("action", "")).strip(),
                prior=float(h.get("prior", 0.0) or 0.0),
                anomaly_signature=anomaly.signature(),
                operation=anomaly.operation,
            )
        )
    return [h for h in out if h.belief]


# What the WIRE evidence permits for each anomaly kind. The reflector picks a
# class from prose and sometimes picks one the evidence cannot support: an
# `update.not_a_real_field.silent_ignore` anomaly came back labelled
# `idempotency_hazard`, which is a different defect entirely, and it scored as
# a false belief. The model may choose only among classes its own evidence
# admits.
#
# silent_empty is deliberately two-valued: "the filter field is wrong" and
# "archived rows are hidden" present identically, and only a probe separates
# them. That ambiguity is designed, so both stay allowed.
KIND_ALLOWS: dict[str, tuple[str, ...]] = {
    "silent_truncation": ("silent_truncation",),
    "silent_null": ("silent_coercion",),
    "type_coercion": ("silent_coercion",),
    "silent_ignore": ("silent_coercion",),
    "write_not_visible": ("eventual_consistency",),
    "silent_empty": ("silent_coercion", "undocumented_flag"),
    "undocumented_status": ("rate_limit",),
    "undocumented_flag": ("undocumented_flag",),
    "mislabelled_semantics": ("mislabelled_semantics",),
    "idempotency_hazard": ("idempotency_hazard",),
    "expiry": ("expiry",),
}


def constrain_class(kind: str, proposed: str) -> str:
    """Keep the model's class only if the wire evidence admits it."""
    allowed = KIND_ALLOWS.get(kind)
    if not allowed:
        return proposed if proposed in CLASSES else "silent_coercion"
    return proposed if proposed in allowed else allowed[0]


def _guess_class(kind: str) -> str:
    from bench.score import KIND_TO_CLASS

    return KIND_TO_CLASS.get(kind, "silent_coercion")


def mint(
    store: BeliefStore,
    hyps: list[Hypothesis],
    anomaly: Anomaly,
    run_id: str,
    tool: str = "lab",
) -> list[Belief]:
    """Record rival hypotheses as linked, unconfirmed beliefs."""
    made: list[Belief] = []
    ids = [h.belief_id(tool) for h in hyps]
    for h, bid in zip(hyps, ids):
        b = store.get(bid)
        if b is None:
            b = Belief(
                id=bid,
                tool=tool,
                operation=h.operation,
                cls=h.cls,
                parameter=h.parameter,
                doc_claims=anomaly.expected,
                belief=h.belief,
                action=h.action,
                competing=[i for i in ids if i != bid],
            )
            b.posterior.beta += max(0.0, min(1.0, h.prior))  # doubt the docs in proportion
            store.add(b)
        b.observe_contradiction(run_id)
        b.note("hypothesis", h.distinguishing_test[:160])
        made.append(b)
    return made
