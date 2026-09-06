"""
Guards: learned beliefs, made executable.

A belief that only lives in a prompt is advice. A guard is the same belief
enforced at the call boundary, so a fresh agent with no memory at all gets the
benefit without having to learn anything.

Guards are DERIVED from confirmed beliefs, never hand-written per tool. Each
one declares the belief class and parameter it implements, and `compile_guards`
activates only those the agent has actually confirmed. That is what stops the
shim and the memory from drifting apart: if the agent never learned it, the
shim does not enforce it.

Interface, matching agent.adapters.lab.LabAdapter:
    before(op, payload) -> (payload, refusal_or_None)
    after(op, payload, status, body, adapter) -> body
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from agent.beliefs import Belief, BeliefStore, Status


@dataclass
class Guard:
    name: str
    implements_class: str
    parameter: str | None = None
    operation: str = "*"
    note: str = ""
    fired: int = 0

    def before(self, op: str, payload: dict[str, Any] | None) -> tuple[dict[str, Any] | None, str | None]:
        return payload, None

    def after(self, op: str, payload: dict[str, Any] | None, status: int, body: Any, adapter: Any) -> Any:
        return body


# ---------------------------------------------------------------------------


@dataclass
class ClampPageSize(Guard):
    """Clamp to the real cap AND follow the cursor.

    Clamping alone just moves the problem: the agent asks for 500, silently
    receives 50, and reports 50 as the total. The first A/B showed exactly
    that -- the shielded agent still answered 23 against a true 73. A guard
    that knows about the cap should do what the caller believed it was
    already doing, which is return the whole page set.
    """

    cap: int = 50
    max_pages: int = 20

    def before(self, op, payload):
        if op == "search" and payload and isinstance(payload.get("page_size"), int):
            if payload["page_size"] > self.cap:
                payload = {**payload, "page_size": self.cap}
                self.fired += 1
        return payload, None

    def after(self, op, payload, status, body, adapter):
        if op != "search" or status != 200 or not isinstance(body, dict):
            return body
        if not body.get("has_more") or not body.get("next_cursor"):
            return body

        rows = list(body.get("results") or [])
        cursor = body.get("next_cursor")
        pages = 0
        while cursor and pages < self.max_pages:
            pages += 1
            nxt = {**(payload or {}), "cursor": cursor}
            try:
                r = adapter.client.post("/v1/search", json=nxt)
                d = r.json()
            except Exception:  # noqa: BLE001
                break
            if r.status_code != 200 or not isinstance(d, dict):
                break
            rows.extend(d.get("results") or [])
            cursor = d.get("next_cursor") if d.get("has_more") else None
        if pages:
            self.fired += 1
        return {**body, "results": rows, "has_more": False, "next_cursor": None,
                "_guard_paged": pages}


@dataclass
class RejectUnknownFilter(Guard):
    known: tuple[str, ...] = ("status", "assignee", "vendor", "archived")

    def before(self, op, payload):
        if op == "search" and payload:
            bad = [k for k in (payload.get("filter") or {}) if k not in self.known]
            if bad:
                self.fired += 1
                return payload, (
                    f"filter field(s) {bad} are not real; this tool returns an empty "
                    f"result set instead of an error. Known fields: {list(self.known)}"
                )
        return payload, None


@dataclass
class IncludeArchived(Guard):
    def before(self, op, payload):
        if op == "search" and payload is not None and "include_archived" not in payload:
            payload = {**payload, "include_archived": True}
            self.fired += 1
        return payload, None


@dataclass
class CoerceAmount(Guard):
    """Large amounts arrive as strings; a naive sum silently concatenates or fails."""

    def after(self, op, payload, status, body, adapter):
        if status != 200 or not isinstance(body, dict):
            return body
        changed = False

        def fix(row: dict[str, Any]) -> dict[str, Any]:
            nonlocal changed
            v = row.get("amount")
            if isinstance(v, str):
                try:
                    row = {**row, "amount": float(v)}
                    changed = True
                except ValueError:
                    pass
            return row

        if "results" in body and isinstance(body["results"], list):
            body = {**body, "results": [fix(r) if isinstance(r, dict) else r for r in body["results"]]}
        elif "amount" in body:
            body = fix(body)
        if changed:
            self.fired += 1
        return body


@dataclass
class WaitForWrite(Guard):
    delay_s: float = 2.2
    _last_write: float = 0.0

    def before(self, op, payload):
        if op == "search" and self._last_write:
            gap = time.time() - self._last_write
            if gap < self.delay_s:
                time.sleep(self.delay_s - gap)
                self.fired += 1
        return payload, None

    def after(self, op, payload, status, body, adapter):
        if op in ("create", "bulk_create") and status in (200, 502):
            self._last_write = time.time()
        return body


@dataclass
class IdempotentCreate(Guard):
    """A 5xx on create may still have committed. Verify before anyone retries."""

    def after(self, op, payload, status, body, adapter):
        if op != "create" or status < 500:
            return body
        title = (payload or {}).get("title")
        vendor = (payload or {}).get("vendor")
        if not title:
            return body
        self.fired += 1
        time.sleep(2.3)
        try:
            sc, found = adapter.client.post(
                "/v1/search",
                json={"filter": {"vendor": vendor} if vendor else {}, "page_size": 50,
                      "include_archived": True},
            ).status_code, None
        except Exception:
            return body
        try:
            r = adapter.client.post(
                "/v1/search",
                json={"filter": {"vendor": vendor} if vendor else {}, "page_size": 50,
                      "include_archived": True},
            ).json()
            titles = {x.get("title") for x in r.get("results", [])}
        except Exception:
            return body
        if title in titles:
            return {
                "_guard": "idempotent_create",
                "committed_despite_error": True,
                "title": title,
                "advice": "the write landed; do NOT retry or you will create a duplicate",
                "original_status": status,
            }
        return body


@dataclass
class Throttle(Guard):
    """Pace requests, and back off on a 429 whether or not it tells you to.

    The tool throttles at roughly 3 requests per second and only sends
    Retry-After about 60% of the time, so an agent that waits for the header
    waits forever on the other 40%. Counterfactual replay found 15 calls
    burned on 429s across the run history, every one of them avoidable.
    """

    min_interval: float = 0.4
    backoff_s: float = 1.2
    _last: float = 0.0

    def before(self, op, payload):
        gap = time.time() - self._last
        if self._last and gap < self.min_interval:
            time.sleep(self.min_interval - gap)
            self.fired += 1
        self._last = time.time()
        return payload, None

    def after(self, op, payload, status, body, adapter):
        if status == 429:
            # Honour the header when present; fall back to a fixed backoff
            # when it is not, which is the case roughly two times in five.
            wait = self.backoff_s
            if isinstance(body, dict) and body.get("_retry_after_present"):
                wait = max(wait, 1.0)
            self.fired += 1
            time.sleep(wait)
            self._last = time.time()
        return body


@dataclass
class NormaliseDate(Guard):
    """Pre-1970 dates are silently stored as null, so refuse rather than lose data."""

    cutoff: str = "1970-01-01"

    def before(self, op, payload):
        if op in ("create", "update") and payload:
            d = payload.get("due_date")
            if isinstance(d, str) and d < self.cutoff:
                self.fired += 1
                return payload, (
                    f"due_date {d!r} is before {self.cutoff}; this tool stores such dates "
                    f"as null without reporting an error, so the value would be lost silently"
                )
        return payload, None


@dataclass
class RejectUnknownUpdateField(Guard):
    known: tuple[str, ...] = ("title", "amount", "due_date", "status", "assignee", "vendor")

    def before(self, op, payload):
        if op == "update" and payload:
            bad = [k for k in payload if k not in self.known]
            if bad:
                self.fired += 1
                return payload, (
                    f"field(s) {bad} are not in this tool's schema; it returns 200 and "
                    f"silently discards them rather than erroring. Known: {list(self.known)}"
                )
        return payload, None


@dataclass
class TruncateTitle(Guard):
    cap: int = 255

    def before(self, op, payload):
        if op in ("create", "update") and payload:
            t = payload.get("title")
            if isinstance(t, str) and len(t) > self.cap:
                self.fired += 1
                return payload, (
                    f"title is {len(t)} characters; this tool silently truncates at "
                    f"{self.cap} and reports success, so the tail would be lost"
                )
        return payload, None


@dataclass
class BulkChunk(Guard):
    cap: int = 20

    def before(self, op, payload):
        if op == "bulk_create" and payload and isinstance(payload.get("items"), list):
            if len(payload["items"]) > self.cap:
                self.fired += 1
                return payload, (
                    f"bulk_create silently processes only the first {self.cap} items while "
                    f"reporting success for all of them. Send at most {self.cap} per call."
                )
        return payload, None


@dataclass
class SortByEdited(Guard):
    """`sort=created` actually orders by last_edited, so creation order needs client sorting."""

    def after(self, op, payload, status, body, adapter):
        if op != "search" or status != 200 or not isinstance(body, dict):
            return body
        if (payload or {}).get("sort") != "created":
            return body
        rows = body.get("results")
        if isinstance(rows, list) and all(isinstance(r, dict) and "created" in r for r in rows):
            self.fired += 1
            return {**body, "results": sorted(rows, key=lambda r: r.get("created", 0))}
        return body


# class -> the guard that implements it, plus the parameter it applies to
REGISTRY: list[tuple[str, str | None, type[Guard], dict[str, Any]]] = [
    ("silent_truncation", "page_size", ClampPageSize, {"name": "clamp_page_size"}),
    ("silent_truncation", "items", BulkChunk, {"name": "bulk_chunk"}),
    ("silent_coercion", "filter", RejectUnknownFilter, {"name": "reject_unknown_filter"}),
    ("silent_coercion", "amount", CoerceAmount, {"name": "coerce_amount"}),
    ("undocumented_flag", None, IncludeArchived, {"name": "include_archived"}),
    ("eventual_consistency", None, WaitForWrite, {"name": "wait_for_write"}),
    ("idempotency_hazard", None, IdempotentCreate, {"name": "idempotent_create"}),
    ("mislabelled_semantics", "sort", SortByEdited, {"name": "sort_by_created"}),
    ("rate_limit", None, Throttle, {"name": "throttle"}),
    ("silent_coercion", "due_date", NormaliseDate, {"name": "reject_pre_epoch_date"}),
    # Matched on operation rather than a literal field name. It used to be
    # pinned to "not_a_real_field", the name one hardcoded recon probe happens
    # to use, so the same rule discovered by any other path compiled to no
    # guard at all despite being correctly confirmed and correctly scored.
    ("silent_coercion", "__update_any__", RejectUnknownUpdateField,
     {"name": "reject_unknown_update_field"}),
    ("silent_truncation", "title", TruncateTitle, {"name": "warn_title_truncation"}),
]


def compile_guards(store: BeliefStore, only_confirmed: bool = True) -> list[Guard]:
    """Turn what the agent has confirmed into what the shim will enforce."""
    beliefs = store.active() if only_confirmed else store.ordered()
    out: list[Guard] = []
    seen: set[str] = set()
    known_fields = {"title", "amount", "due_date", "status", "assignee", "vendor"}
    for b in beliefs:
        for cls, param, klass, kw in REGISTRY:
            if b.cls != cls:
                continue
            if param == "__update_any__":
                # any belief about a field the schema does not define
                if b.operation != "update" or b.parameter in known_fields or not b.parameter:
                    continue
            elif param is not None and b.parameter != param:
                continue
            if kw["name"] in seen:
                continue
            g = klass(implements_class=cls, parameter=param, note=b.belief[:120], **kw)
            out.append(g)
            seen.add(kw["name"])
    return out


def guard_report(guards: list[Guard]) -> list[dict[str, Any]]:
    return [
        {"guard": g.name, "implements": g.implements_class, "parameter": g.parameter,
         "fired": g.fired, "because": g.note}
        for g in guards
    ]
