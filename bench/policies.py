"""
Probe-selection policies, and the rule that keeps the comparison honest.

An agent with a call budget has to decide what to ask next. This module holds
four ways of deciding, so the claim "the selector is doing work" can be tested
against the null hypothesis that any order would have done as well.

    fixed    capture order: the order a human wrote the plan in
    random   uniform over unobserved probes, seeded
    greedy   the current policy: prefer probes that look discriminative
    eig      expected reduction in posterior entropy per unit cost

## What a policy is allowed to see

This is the part that decides whether the experiment means anything.

A policy may read the **request** of a candidate probe (what question it
asks), the **documentation** (`operation`, `parameter`, `doc_claims`), and
every **response it has already paid for**. It may not read the response of a
probe it has not chosen -- that is the thing it is deciding whether to buy --
and it may not read `truth` or `mismatch_class` from the answer key.

That last one is not a formality. An EIG policy whose outcome probabilities
were fitted on the evaluation labels would win every comparison by
construction and would have measured nothing except its own access to the
answers. `PolicyView` enforces it by holding the answer key out of reach
rather than by asking each policy to be careful, because "be careful" is not
a mechanism.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Any, Callable

VERDICTS = ("contradicts_doc", "supports_doc")


@dataclass
class Probe:
    """One candidate observation, described only by the question it asks."""

    key: str
    op: str
    request: dict[str, Any]
    cost: int = 1                 # calls; one recorded exchange is one call

    def mentions(self, parameter: str | None) -> bool:
        if parameter is None:
            return True           # a rule about the operation as a whole
        return parameter in self.request or any(
            parameter in str(k) for k in self.request)


@dataclass
class Claim:
    """A documented claim under test. Carries no answer, by construction."""

    rule_id: str
    operation: str
    parameter: str | None
    settled: bool = False
    verdict: str | None = None


@dataclass
class PolicyView:
    """Everything a policy is permitted to know, and nothing else.

    Constructed by the evaluator from the fixture's documentation and the
    probes already paid for. The answer key never enters it.
    """

    unobserved: list[Probe]
    observed: list[Probe] = field(default_factory=list)
    claims: list[Claim] = field(default_factory=list)

    @property
    def open_claims(self) -> list[Claim]:
        return [c for c in self.claims if not c.settled]


Policy = Callable[[PolicyView, random.Random], Probe]


# ---------------------------------------------------------------------------
# baselines
# ---------------------------------------------------------------------------


def fixed_order(view: PolicyView, _rng: random.Random) -> Probe:
    """Capture order. The order somebody happened to write the plan in.

    Worth measuring because it is what a project gets for free, and a policy
    that cannot beat it is not earning its complexity.
    """
    return view.unobserved[0]


def random_valid(view: PolicyView, rng: random.Random) -> Probe:
    """Uniform over unobserved probes. The null hypothesis."""
    return view.unobserved[rng.randrange(len(view.unobserved))]


def greedy(view: PolicyView, _rng: random.Random) -> Probe:
    """The current policy: prefer probes that touch an open claim, then extremes.

    Two heuristics, both derived from the documentation rather than from any
    response. A probe that mentions the parameter an open claim is about is
    more likely to say something about it. Among those, a probe whose numeric
    argument is furthest from what has already been observed is more likely to
    cross a boundary, because a cap or a coercion shows up at the edges and
    three samples from the middle of a range all look alike.
    """
    def score(p: Probe) -> tuple[int, float]:
        touching = sum(1 for c in view.open_claims
                       if c.operation == p.op and p.mentions(c.parameter))
        seen = [v for q in view.observed if q.op == p.op
                for v in q.request.values() if isinstance(v, (int, float))]
        mine = [v for v in p.request.values() if isinstance(v, (int, float))]
        distance = 0.0
        if mine:
            distance = max(min((abs(v - s) for s in seen), default=float(abs(v)))
                           for v in mine)
        return (touching, distance)

    return max(view.unobserved, key=lambda p: (score(p), -view.unobserved.index(p)))


# ---------------------------------------------------------------------------
# expected information gain
# ---------------------------------------------------------------------------


def _entropy(p: float) -> float:
    if p <= 0.0 or p >= 1.0:
        return 0.0
    return -(p * math.log2(p) + (1 - p) * math.log2(1 - p))


def expected_information_gain(view: PolicyView, _rng: random.Random) -> Probe:
    """Expected posterior-entropy reduction per unit cost.

    For each open claim the posterior is over two hypotheses -- the
    documentation holds, or it does not -- and starts at maximum entropy,
    because nothing in the documentation tells you whether it is true. One bit
    each.

    A probe cannot be scored by its outcome, which is unknown until it is
    bought. What can be estimated from the request alone is the probability
    that the probe is *discriminative* for a claim: that its answer will
    settle the claim either way. That estimate comes from three
    documentation-derived features, none of which touch the answer key:

      - the probe addresses the claim's operation;
      - the probe mentions the claim's parameter;
      - the probe's argument is unlike the arguments already observed, since a
        repeat of a question already asked cannot move a posterior.

    The score is the sum over open claims of `P(discriminative) * H(posterior)`
    divided by the probe's cost, and the maximum is taken. When no claim is
    open, or every candidate scores zero -- which happens when the remaining
    probes address nothing still in question -- the outcome probabilities are
    underdetermined, and the policy falls back to `greedy` deterministically
    rather than picking arbitrarily.
    """
    if not view.open_claims:
        return greedy(view, _rng)

    def p_discriminative(p: Probe, c: Claim) -> float:
        if c.operation != p.op:
            return 0.0
        prob = 0.5                                   # right operation
        if p.mentions(c.parameter):
            prob += 0.3                              # and the parameter in question
        seen_same = [q for q in view.observed
                     if q.op == p.op and q.request == p.request]
        if seen_same:
            return 0.0                               # the same question, already answered
        novel = [v for v in p.request.values() if isinstance(v, (int, float))]
        prior = [v for q in view.observed if q.op == p.op
                 for v in q.request.values() if isinstance(v, (int, float))]
        if novel and all(v not in prior for v in novel):
            prob += 0.2                              # an argument not yet tried
        return min(prob, 1.0)

    def score(p: Probe) -> float:
        gain = sum(p_discriminative(p, c) * _entropy(0.5) for c in view.open_claims)
        return gain / max(p.cost, 1)

    scored = [(score(p), -i, p) for i, p in enumerate(view.unobserved)]
    best = max(scored)
    if best[0] <= 0.0:
        # Underdetermined: no candidate is estimated to bear on anything still
        # open. Guessing here would be noise wearing a formula, so hand back to
        # the simpler policy and say so.
        return greedy(view, _rng)
    return best[2]


POLICIES: dict[str, Policy] = {
    "fixed": fixed_order,
    "random": random_valid,
    "greedy": greedy,
    "eig": expected_information_gain,
}
