"""
The belief store: what the agent thinks is true about a tool, why it thinks
so, and how sure it is.

Three things make this more than a notes file:

1. Confidence is a beta-binomial posterior over "the documentation is
   correct", seeded from the doc claim as the prior. It moves per
   observation and can move *back*, which is what makes retirement a
   measurement rather than a heuristic.

2. A belief is only `confirmed` after a designed probe AND surviving a
   falsification attempt. Observation alone is never enough.

3. Every belief carries a `class`. Under a context budget, instances
   compact into their class line, which is the whole "belief graph": one
   field and a fold.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

import yaml

# Rough chars-per-token for budgeting. Deliberately conservative.
CHARS_PER_TOKEN = 3.6

# Posterior thresholds.
DOC_DOUBT_THRESHOLD = 0.30   # below this, the doc is suspect -> spawn hypothesis
DOC_TRUST_THRESHOLD = 0.70   # above this, the doc looks right again -> retire belief


class Status(str, Enum):
    HYPOTHESIS = "hypothesis"
    CONFIRMED = "confirmed"
    FALSIFIED = "falsified"
    RETIRED = "retired"


@dataclass
class Posterior:
    """Beta-binomial over P(the documentation is correct).

    alpha counts observations consistent with the docs, beta counts
    observations that contradict them. The prior is weakly pro-doc: we start
    out believing the documentation, which is exactly the failure mode this
    project is about.
    """

    alpha: float = 2.0
    beta: float = 1.0

    @property
    def p_doc_correct(self) -> float:
        return self.alpha / (self.alpha + self.beta)

    @property
    def n(self) -> int:
        return int(self.alpha + self.beta - 3.0)

    def supports_doc(self, weight: float = 1.0) -> None:
        self.alpha += weight

    def contradicts_doc(self, weight: float = 1.0) -> None:
        self.beta += weight


@dataclass
class Belief:
    id: str
    tool: str
    operation: str
    cls: str                      # silent_truncation, idempotency_hazard, ...
    doc_claims: str
    belief: str
    action: str = ""
    parameter: str | None = None
    status: Status = Status.HYPOTHESIS
    posterior: Posterior = field(default_factory=Posterior)
    evidence_for: int = 0
    evidence_against: int = 0
    first_seen: str = ""
    last_confirmed: str = ""
    probes: list[str] = field(default_factory=list)
    survived_falsification: str | None = None
    falsified_by: str | None = None
    replay: dict[str, Any] = field(default_factory=dict)
    competing: list[str] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)

    # --- lifecycle --------------------------------------------------------

    def note(self, event: str, detail: str = "") -> None:
        self.history.append(
            {
                "t": round(time.time(), 3),
                "event": event,
                "detail": detail,
                "p_doc_correct": round(self.posterior.p_doc_correct, 4),
                "status": self.status.value,
            }
        )

    def observe_contradiction(self, run_id: str, weight: float = 1.0) -> None:
        self.posterior.contradicts_doc(weight)
        self.evidence_for += 1
        self.first_seen = self.first_seen or run_id
        self.note("contradiction", run_id)

    def observe_support_for_doc(self, run_id: str, weight: float = 1.0) -> None:
        """The world behaved as documented -- evidence AGAINST our belief."""
        self.posterior.supports_doc(weight)
        self.evidence_against += 1
        self.note("doc_behaved_as_written", run_id)
        if (
            self.status is Status.CONFIRMED
            and self.posterior.p_doc_correct > DOC_TRUST_THRESHOLD
        ):
            self.status = Status.FALSIFIED
            self.note("auto_falsified", "posterior reverted above trust threshold")

    def confirm(self, probe_id: str, run_id: str = "") -> None:
        self.probes.append(probe_id)
        self.status = Status.CONFIRMED
        self.last_confirmed = run_id
        self.note("confirmed_by_probe", probe_id)

    def falsify(self, probe_id: str, why: str = "") -> None:
        self.probes.append(probe_id)
        self.status = Status.FALSIFIED
        self.falsified_by = probe_id
        self.note("falsified", why or probe_id)

    def retire(self, why: str) -> None:
        self.status = Status.RETIRED
        self.note("retired", why)

    @property
    def is_active(self) -> bool:
        return self.status is Status.CONFIRMED

    # --- serialisation ----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        d["posterior"] = {
            "alpha": round(self.posterior.alpha, 3),
            "beta": round(self.posterior.beta, 3),
        }
        d["p_doc_correct"] = round(self.posterior.p_doc_correct, 4)
        d["class"] = d.pop("cls")
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Belief":
        d = dict(d)
        d.pop("p_doc_correct", None)
        post = d.pop("posterior", {}) or {}
        klass = d.pop("class", None) or d.pop("cls", "unknown")
        status = Status(d.pop("status", "hypothesis"))
        return cls(
            cls=klass,
            status=status,
            posterior=Posterior(
                alpha=float(post.get("alpha", 2.0)), beta=float(post.get("beta", 1.0))
            ),
            **d,
        )


class BeliefStore:
    """One YAML file per tool. Every change is meant to land as a PR."""

    def __init__(self, tool: str, root: str | Path = "beliefs") -> None:
        self.tool = tool
        self.path = Path(root) / f"{tool}.yaml"
        self.beliefs: dict[str, Belief] = {}
        self.load()

    # --- persistence ------------------------------------------------------

    def load(self) -> None:
        if not self.path.exists():
            return
        data = yaml.safe_load(self.path.read_text()) or {}
        for rec in data.get("beliefs", []) or []:
            b = Belief.from_dict(rec)
            self.beliefs[b.id] = b

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "tool": self.tool,
            "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "counts": self.counts(),
            "beliefs": [b.to_dict() for b in self.ordered()],
        }
        self.path.write_text(yaml.safe_dump(payload, sort_keys=False, width=100))

    # --- access -----------------------------------------------------------

    def ordered(self) -> list[Belief]:
        rank = {
            Status.CONFIRMED: 0,
            Status.HYPOTHESIS: 1,
            Status.FALSIFIED: 2,
            Status.RETIRED: 3,
        }
        return sorted(self.beliefs.values(), key=lambda b: (rank[b.status], b.id))

    def active(self) -> list[Belief]:
        return [b for b in self.ordered() if b.is_active]

    def hypotheses(self) -> list[Belief]:
        return [b for b in self.ordered() if b.status is Status.HYPOTHESIS]

    def counts(self) -> dict[str, int]:
        out = {s.value: 0 for s in Status}
        for b in self.beliefs.values():
            out[b.status.value] += 1
        return out

    def add(self, b: Belief) -> Belief:
        existing = self.beliefs.get(b.id)
        if existing:
            return existing
        b.note("created", b.doc_claims[:60])
        self.beliefs[b.id] = b
        return b

    def get(self, belief_id: str) -> Belief | None:
        return self.beliefs.get(belief_id)

    # --- context rendering ------------------------------------------------

    def render_for_context(self, budget_tokens: int = 2500) -> str:
        """What the executor actually sees before a run.

        Only confirmed beliefs, cheapest-useful-first. When the budget is
        tight, instances collapse into one line per class -- the compaction
        the 32K window forces.
        """
        active = self.active()
        if not active:
            return "(no confirmed beliefs about this tool yet)"

        def full_line(b: Belief) -> str:
            return f"- [{b.cls}] {b.belief}\n  -> {b.action}"

        lines = [full_line(b) for b in active]
        header = f"What you have learned about `{self.tool}` (docs are unreliable):\n"
        body = header + "\n".join(lines)
        if self._tokens(body) <= budget_tokens:
            return body

        # over budget: contract instances into class lines
        by_class: dict[str, list[Belief]] = {}
        for b in active:
            by_class.setdefault(b.cls, []).append(b)

        compact: list[str] = []
        for cls_name, members in sorted(by_class.items(), key=lambda kv: -len(kv[1])):
            if len(members) == 1:
                compact.append(full_line(members[0]))
            else:
                ops = ", ".join(sorted({m.operation for m in members}))
                acts = "; ".join(dict.fromkeys(m.action for m in members if m.action))
                compact.append(
                    f"- [{cls_name}] affects {ops} ({len(members)} known cases)\n  -> {acts}"
                )
        body = header + "\n".join(compact)

        # still over: drop the least-supported classes
        while self._tokens(body) > budget_tokens and len(compact) > 1:
            compact.pop()
            body = header + "\n".join(compact) + "\n- (older beliefs elided for context budget)"
        return body

    @staticmethod
    def _tokens(s: str) -> int:
        return int(len(s) / CHARS_PER_TOKEN) + 1

    def context_tokens(self, budget_tokens: int = 2500) -> int:
        return self._tokens(self.render_for_context(budget_tokens))
