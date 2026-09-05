"""
The contract layer.

The documentation makes checkable promises. This module holds those promises
as data and compares them against what actually came back on the wire. When
reality and the spec disagree and no held belief already explains the
disagreement, that is an anomaly -- the trigger for a hypothesis.

This is deliberately NOT an LLM. Detection is deterministic so the same run
produces the same anomalies, which is what makes the bench reproducible. The
model's job starts afterwards: explaining the anomaly, not spotting it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import Any


# What lab/DOCS.md promises, in machine-readable form.
DOC_SPEC: dict[str, Any] = {
    "tool": "lab",
    "no_rate_limit": True,
    "status_codes": {
        "create": [200],
        "get": [200, 404],
        "update": [200, 400],
        "search": [200, 400],
        "bulk_create": [200],
        "archive": [200, 404],
    },
    "field_types": {"amount": "number", "title": "string", "due_date": "string|null"},
    "limits": {"title_max": 2000, "page_size_max": 500, "bulk_max": 100},
    "promises": {
        "echo_fields": ["title", "amount", "due_date", "status", "assignee", "vendor"],
        "immediately_searchable": True,
        "cursors_never_expire": True,
        "get_returns_archived": True,
        "unknown_update_field_is_400": True,
        "unknown_filter_field_is_400": True,
        "sort_created_is_creation_order": True,
        "502_means_not_processed": True,
    },
}


@dataclass
class Anomaly:
    kind: str                 # short slug, becomes the hypothesis seed
    operation: str
    summary: str              # human sentence
    expected: str
    observed: str
    parameter: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    call_ns: list[int] = field(default_factory=list)

    def signature(self) -> str:
        """Stable key so the same anomaly recurring is recognised as one thing."""
        return f"{self.operation}.{self.parameter or '_'}.{self.kind}"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["signature"] = self.signature()
        return d


@dataclass
class Call:
    n: int
    op: str
    request: dict[str, Any]
    status: int
    response: Any
    t: float


class ContractLayer:
    """Accumulates a run's calls and reports contract violations."""

    def __init__(self, spec: dict[str, Any] | None = None) -> None:
        self.spec = spec or DOC_SPEC
        self.calls: list[Call] = []

    def record(self, call: Call) -> list[Anomaly]:
        self.calls.append(call)
        found: list[Anomaly] = []
        found += self._check_status(call)
        found += self._check_echo(call)
        found += self._check_types(call)
        found += self._check_caps(call)
        found += self._check_cross_call(call)
        return found

    # --- single-call checks ----------------------------------------------

    def _check_status(self, c: Call) -> list[Anomaly]:
        out = []
        allowed = self.spec["status_codes"].get(c.op, [200])
        if c.status == 429 and self.spec.get("no_rate_limit"):
            out.append(
                Anomaly(
                    kind="undocumented_status",
                    operation=c.op,
                    summary="Got 429 from an API documented as having no rate limit.",
                    expected="no rate limit; 429 never returned",
                    observed=f"HTTP 429 on {c.op}",
                    evidence={"status": 429, "retry_after_present": c.response.get("_retry_after_present")
                              if isinstance(c.response, dict) else None},
                    call_ns=[c.n],
                )
            )
        elif (
            c.op == "search"
            and c.status == 400
            and isinstance(c.response, dict)
            and "cursor" in str(c.response.get("error", ""))
        ):
            # 400 is a documented outcome for search, so a plain status check
            # sails past this one. The lie is specifically that cursors are
            # promised to live forever.
            out.append(
                Anomaly(
                    kind="expiry",
                    operation="search",
                    parameter="cursor",
                    summary="A previously issued cursor was rejected as expired.",
                    expected="cursors remain valid indefinitely",
                    observed=f"400 {c.response.get('error')}",
                    evidence={"error": c.response.get("error")},
                    call_ns=[c.n],
                )
            )
        elif c.status not in allowed and c.status != 429:
            out.append(
                Anomaly(
                    kind="undocumented_status",
                    operation=c.op,
                    summary=f"HTTP {c.status} is not a documented outcome for {c.op}.",
                    expected=f"one of {allowed}",
                    observed=f"HTTP {c.status}",
                    evidence={"body": _clip(c.response)},
                    call_ns=[c.n],
                )
            )
        return out

    def _check_echo(self, c: Call) -> list[Anomaly]:
        """A field we sent came back different, with no error."""
        out = []
        if c.op not in ("create", "update") or c.status != 200:
            return out
        if not isinstance(c.response, dict):
            return out
        for f in self.spec["promises"]["echo_fields"]:
            if f not in c.request:
                continue
            sent, got = c.request[f], c.response.get(f)
            if sent == got:
                continue
            if isinstance(sent, str) and isinstance(got, str) and got == sent[: len(got)] and len(got) < len(sent):
                out.append(
                    Anomaly(
                        kind="silent_truncation",
                        operation=c.op,
                        parameter=f,
                        summary=f"`{f}` was silently truncated from {len(sent)} to {len(got)} characters.",
                        expected=f"stored as sent (doc: title_max={self.spec['limits']['title_max']})",
                        observed=f"truncated to {len(got)}",
                        evidence={"sent_len": len(sent), "got_len": len(got)},
                        call_ns=[c.n],
                    )
                )
            elif got is None and sent is not None:
                out.append(
                    Anomaly(
                        kind="silent_null",
                        operation=c.op,
                        parameter=f,
                        summary=f"`{f}` was sent as {sent!r} but stored as null, with no error.",
                        expected="stored as sent",
                        observed="null",
                        evidence={"sent": sent},
                        call_ns=[c.n],
                    )
                )
            elif str(sent) == str(got) and type(sent) is not type(got):
                out.append(
                    Anomaly(
                        kind="type_coercion",
                        operation=c.op,
                        parameter=f,
                        summary=f"`{f}` was sent as {type(sent).__name__} and returned as {type(got).__name__}.",
                        expected=f"{self.spec['field_types'].get(f, 'same type')}",
                        observed=type(got).__name__,
                        evidence={"sent": sent, "got": got},
                        call_ns=[c.n],
                    )
                )
        return out

    def _check_types(self, c: Call) -> list[Anomaly]:
        out = []
        if c.status != 200 or not isinstance(c.response, dict):
            return out
        amount = c.response.get("amount")
        if isinstance(amount, str):
            out.append(
                Anomaly(
                    kind="type_coercion",
                    operation=c.op,
                    parameter="amount",
                    summary="`amount` came back as a string; the docs say it is always a number.",
                    expected="number",
                    observed=f"string {amount!r}",
                    evidence={"value": amount},
                    call_ns=[c.n],
                )
            )
        return out

    def _check_caps(self, c: Call) -> list[Anomaly]:
        out = []
        if c.op != "search" or c.status != 200 or not isinstance(c.response, dict):
            return out
        asked = c.request.get("page_size")
        got = len(c.response.get("results", []))
        if isinstance(asked, int) and got < asked and c.response.get("has_more"):
            out.append(
                Anomaly(
                    kind="silent_truncation",
                    operation="search",
                    parameter="page_size",
                    summary=f"Asked for page_size={asked}, received {got}, and has_more is true.",
                    expected=f"up to {asked} (doc: page_size_max={self.spec['limits']['page_size_max']})",
                    observed=f"{got} results with has_more=true",
                    evidence={"asked": asked, "got": got},
                    call_ns=[c.n],
                )
            )
        if got == 0 and c.request.get("filter"):
            out.append(
                Anomaly(
                    kind="silent_empty",
                    operation="search",
                    parameter="filter",
                    summary="A filtered search returned zero results and no error.",
                    expected="matching rows, or 400 invalid_filter for a bad field",
                    observed="200 with empty results",
                    evidence={"filter": c.request.get("filter")},
                    call_ns=[c.n],
                )
            )
        return out

    # --- cross-call checks ------------------------------------------------

    def _check_cross_call(self, c: Call) -> list[Anomaly]:
        out: list[Anomaly] = []

        # a write we just made is not visible to a later search
        if c.op == "search" and c.status == 200 and isinstance(c.response, dict):
            recent_creates = [
                p for p in self.calls[-12:]
                if p.op == "create" and p.status == 200 and isinstance(p.response, dict)
            ]
            ids_seen = {r.get("id") for r in c.response.get("results", [])}
            for p in recent_creates:
                vendor = (p.request or {}).get("vendor")
                if not vendor or (c.request.get("filter") or {}).get("vendor") != vendor:
                    continue
                if p.response.get("id") not in ids_seen and (c.t - p.t) < 1.5:
                    out.append(
                        Anomaly(
                            kind="write_not_visible",
                            operation="search",
                            summary="An item created moments ago is absent from a search that should match it.",
                            expected="items are immediately searchable",
                            observed=f"absent {c.t - p.t:.2f}s after creation",
                            evidence={"item_id": p.response.get("id"), "gap_s": round(c.t - p.t, 3)},
                            call_ns=[p.n, c.n],
                        )
                    )
                    break

        # bulk reported more than it delivered
        if c.op == "search" and c.status == 200 and isinstance(c.response, dict):
            for p in self.calls:
                if p.op != "bulk_create" or p.status != 200:
                    continue
                reported = (p.response or {}).get("created")
                ids = set((p.response or {}).get("ids") or [])
                if isinstance(reported, int) and reported > len(ids):
                    out.append(
                        Anomaly(
                            kind="silent_truncation",
                            operation="bulk_create",
                            parameter="items",
                            summary=f"bulk_create reported {reported} created but returned only {len(ids)} ids.",
                            expected=f"all {reported} created (doc: bulk_max={self.spec['limits']['bulk_max']})",
                            observed=f"{len(ids)} ids returned",
                            evidence={"reported": reported, "ids_returned": len(ids)},
                            call_ns=[p.n, c.n],
                        )
                    )
                    break

        # a 502 that nevertheless committed
        if c.op == "search" and c.status == 200 and isinstance(c.response, dict):
            titles = {r.get("title") for r in c.response.get("results", [])}
            for p in self.calls:
                if p.op == "create" and p.status == 502:
                    t = (p.request or {}).get("title")
                    if t and t in titles:
                        out.append(
                            Anomaly(
                                kind="idempotency_hazard",
                                operation="create",
                                summary="A create that returned 502 had in fact been committed.",
                                expected="502 means the request was not processed",
                                observed=f"item titled {t!r} exists despite the 502",
                                evidence={"title": t},
                                call_ns=[p.n, c.n],
                            )
                        )
                        break

        # GET 404 for an id we hold
        if c.op == "get" and c.status == 404:
            wanted = (c.request or {}).get("id")
            for p in self.calls:
                if p.op in ("create", "archive") and isinstance(p.response, dict):
                    if p.response.get("id") == wanted:
                        out.append(
                            Anomaly(
                                kind="undocumented_flag",
                                operation="get",
                                summary="GET returned 404 for an item this run created.",
                                expected="GET returns the item regardless of archived state",
                                observed="404 not_found",
                                evidence={"item_id": wanted},
                                call_ns=[p.n, c.n],
                            )
                        )
                        break

        # PATCH with an unknown field was accepted
        if c.op == "update" and c.status == 200 and isinstance(c.response, dict):
            unknown = [k for k in (c.request or {}) if k not in self.spec["promises"]["echo_fields"]]
            if unknown:
                out.append(
                    Anomaly(
                        kind="silent_ignore",
                        operation="update",
                        parameter=unknown[0],
                        summary=f"PATCH with unknown field {unknown[0]!r} returned 200 instead of 400.",
                        expected="400 unknown_field",
                        observed="200, field absent from the stored item",
                        evidence={"unknown_fields": unknown},
                        call_ns=[c.n],
                    )
                )

        # sort=created did not produce creation order
        if (
            c.op == "search"
            and c.status == 200
            and isinstance(c.response, dict)
            and (c.request or {}).get("sort") == "created"
        ):
            rows = c.response.get("results", [])
            created = [r.get("created") for r in rows if r.get("created") is not None]
            if len(created) >= 2 and created != sorted(created):
                out.append(
                    Anomaly(
                        kind="mislabelled_semantics",
                        operation="search",
                        parameter="sort",
                        summary="sort=created did not return rows in creation order.",
                        expected="ordered by created ascending",
                        observed="out of order by created",
                        evidence={"created_sequence": created[:6]},
                        call_ns=[c.n],
                    )
                )
        return out


def _clip(v: Any, n: int = 220) -> str:
    try:
        s = json.dumps(v)
    except Exception:
        s = str(v)
    return s[:n]


def unexplained(anomalies: list[Anomaly], active_signatures: set[str]) -> list[Anomaly]:
    """Anomalies no confirmed belief already accounts for."""
    return [a for a in anomalies if a.signature() not in active_signatures]
