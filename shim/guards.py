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
    cap: int = 50

    def before(self, op, payload):
        if op == "search" and payload and isinstance(payload.get("page_size"), int):
            if payload["page_size"] > self.cap:
                payload = {**payload, "page_size": self.cap}
                self.fired += 1
        return payload, None


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
]


def compile_guards(store: BeliefStore, only_confirmed: bool = True) -> list[Guard]:
    """Turn what the agent has confirmed into what the shim will enforce."""
    beliefs = store.active() if only_confirmed else store.ordered()
    out: list[Guard] = []
    seen: set[str] = set()
    for b in beliefs:
        for cls, param, klass, kw in REGISTRY:
            if b.cls != cls:
                continue
            if param is not None and b.parameter != param:
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
