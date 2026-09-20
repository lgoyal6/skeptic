"""
Contracts as immutable versions, and the observation windows they are inferred from.

A belief about a tool is a claim about a moment. Tools change; the claim does
not stop being true, it stops being current. Overwriting it destroys the only
record that could show the change happened, which is why a detected change
here creates a *new* contract version whose predecessor stays inspectable and
replayable rather than mutating the one that was there.

Three things make a contract version:

- **the documentation hash**, so a docs edit is visible even when behaviour is
  identical;
- **the observed schema**, a structural fingerprint of each operation's
  response with every value stripped out, so `0.87 -> 0.88` is not a schema
  change and `{"rates": {...}} -> [{...}]` is;
- **the semantic invariants**, the verdicts the deterministic checks return
  for each documented claim.

Those three are separated on purpose, because a change in each means something
different to whoever has to act on it. Docs moved and behaviour did not: read
the changelog. Schema moved: your parser breaks. Semantics moved: your parser
is fine and your logic is now wrong, which is the expensive one.

Transport failures are deliberately *not* part of a contract. A 502 is an
event, not a promise.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import Any

# How many consecutive windows must agree before a change is promoted from a
# candidate to a contract. One differing window is an event; a change that
# persists is a contract. See `drift.STRONG_KINDS` for the exception.
CONFIRMATIONS_REQUIRED = 2


def schema_of(value: Any, depth: int = 0) -> Any:
    """A structural fingerprint: types and keys, no values.

    Lists collapse to the union of their element schemas rather than keeping
    one entry per element, so a page of 100 rows and a page of 5 rows have the
    same schema. That matters: otherwise every pagination difference would
    read as a schema change and the class would be useless.
    """
    if depth > 8:
        return "..."
    if isinstance(value, dict):
        return {k: schema_of(v, depth + 1) for k, v in sorted(value.items())}
    if isinstance(value, list):
        if not value:
            return ["<empty>"]
        seen: list[Any] = []
        for item in value:
            s = schema_of(item, depth + 1)
            if s not in seen:
                seen.append(s)
        return sorted(seen, key=json.dumps) if len(seen) > 1 else [seen[0]]
    if value is None:
        return "null"
    return type(value).__name__


@dataclass
class Window:
    """One observation window: everything seen of a tool at one point in time.

    A window is the unit the change-point method compares. It is derived from
    a fixture, so it is reproducible, and it carries the exchange keys behind
    every signal so attribution can narrow to the calls that actually moved.
    """

    tool: str
    label: str                                  # the fixture version, or a planted variant
    observed_at: float                          # ordering only; see note in drift.py
    docs_sha256: str
    schema: dict[str, Any] = field(default_factory=dict)      # op -> schema
    semantics: dict[str, str] = field(default_factory=dict)   # rule id -> verdict
    transport: dict[str, list[int]] = field(default_factory=dict)  # op -> statuses
    # How many successful calls backed the schema for each operation. The
    # observed schema is a union over those calls, so it is a LOWER BOUND on
    # the real shape: a field absent from three calls may simply not have been
    # in those three. Without this count, a transient failure that removes one
    # call also removes whatever fields only that call carried, and the
    # narrowing reads as a field being deleted from the API.
    observed_counts: dict[str, int] = field(default_factory=dict)
    evidence: dict[str, list[str]] = field(default_factory=dict)   # signal -> exchange keys

    def signals(self) -> dict[str, Any]:
        """Everything a contract is built from, flattened for comparison."""
        out: dict[str, Any] = {"docs": self.docs_sha256}
        for op, s in self.schema.items():
            out[f"schema:{op}"] = s
        for rule, v in self.semantics.items():
            out[f"semantic:{rule}"] = v
        return out


@dataclass
class Contract:
    """An immutable statement of what a tool did, over a stated span."""

    tool: str
    version: int
    effective_from: str                         # the window label it began at
    docs_sha256: str
    schema: dict[str, Any]
    semantics: dict[str, str]
    predecessor: int | None = None
    windows: list[str] = field(default_factory=list)
    established_by: list[dict[str, Any]] = field(default_factory=list)  # the changes

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def summary(self) -> str:
        mism = sum(1 for v in self.semantics.values() if v == "contradicts_doc")
        abst = sum(1 for v in self.semantics.values() if v == "unobservable")
        return (f"v{self.version} from {self.effective_from} "
                f"({len(self.windows)} window(s), {mism} mismatch(es), {abst} abstention(s))")


@dataclass
class Change:
    """One classified difference between two windows, with its evidence."""

    kind: str                  # documentation | schema | semantic | transient
    signal: str                # which signal moved
    before: Any
    after: Any
    detail: str
    evidence: list[str] = field(default_factory=list)   # exchange keys, smallest set
    confirmations: int = 1
    promoted: bool = False     # did this establish a new contract version?

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ContractStore:
    """Append-only. A new version never edits its predecessor."""

    def __init__(self, tool: str) -> None:
        self.tool = tool
        self.versions: list[Contract] = []

    def open(self, window: Window, changes: list[Change] | None = None) -> Contract:
        c = Contract(
            tool=self.tool,
            version=len(self.versions) + 1,
            effective_from=window.label,
            docs_sha256=window.docs_sha256,
            schema=dict(window.schema),
            semantics=dict(window.semantics),
            predecessor=self.versions[-1].version if self.versions else None,
            windows=[window.label],
            established_by=[c.to_dict() for c in (changes or [])],
        )
        self.versions.append(c)
        return c

    def extend(self, window: Window) -> None:
        """Record that the current contract still holds for another window."""
        if not self.versions:
            raise RuntimeError("no contract open")
        if window.label not in self.versions[-1].windows:
            self.versions[-1].windows.append(window.label)

    @property
    def current(self) -> Contract | None:
        return self.versions[-1] if self.versions else None

    def to_dict(self) -> dict[str, Any]:
        return {"tool": self.tool, "versions": [v.to_dict() for v in self.versions]}
