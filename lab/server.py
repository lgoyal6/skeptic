"""
The lab: an instrumented task-tracker API whose documentation lies in 14
specific, known ways.

This exists so that "the agent discovered a hidden behaviour" is a scored
claim rather than a vibe. `lab/ground_truth.yaml` is the answer key. The
agent sees only `lab/DOCS.md` (which is wrong) and the wire behaviour.

Every rule is individually toggleable at runtime through /_control, which is
how the belief-retirement demo works: turn a rule off, and a previously
correct belief becomes false.

Determinism: all stochastic behaviour is driven by a seeded RNG plus a
monotonic call counter, so `make bench SEED=n` reproduces byte-for-byte.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import time
import uuid
from collections import deque
from dataclasses import dataclass, field, asdict
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

# ---------------------------------------------------------------------------
# knobs
# ---------------------------------------------------------------------------

SEED = int(os.environ.get("LAB_SEED", "1337"))
# Scales every wall-clock behaviour (search lag, cursor TTL, rate window).
# 1.0 is realistic. Lower it only for local iteration, never for a scored run.
TIME_SCALE = float(os.environ.get("LAB_TIME_SCALE", "1.0"))

PAGE_SIZE_CAP = 50
BULK_CAP = 20
TITLE_CAP = 255
CURSOR_TTL_S = 60.0 * TIME_SCALE
SEARCH_LAG_S = 2.0 * TIME_SCALE
RATE_LIMIT_N = 3
RATE_LIMIT_WINDOW_S = 1.0 * TIME_SCALE
RETRY_AFTER_PRESENT_RATE = 0.6
WRITE_502_RATE = 0.05
LARGE_NUMBER_THRESHOLD = 1_000_000
EPOCH_CUTOFF = "1970-01-01"

ALL_RULES = [
    "page_size_cap",
    "rate_limit_flaky_header",
    "write_search_lag",
    "unknown_filter_field",
    "pre_epoch_date_null",
    "large_number_to_string",
    "cursor_expiry",
    "include_archived_flag",
    "unknown_field_ignored",
    "archived_get_404",
    "bulk_cap",
    "sort_created_is_edited",
    "title_truncation",
    "write_502_after_commit",
]

KNOWN_FILTER_FIELDS = {"status", "assignee", "vendor", "archived"}
KNOWN_ITEM_FIELDS = {"title", "amount", "due_date", "status", "assignee", "vendor"}


# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------


@dataclass
class Item:
    id: str
    title: str
    amount: float | None = None
    due_date: str | None = None
    status: str = "open"
    assignee: str | None = None
    vendor: str | None = None
    archived: bool = False
    created: float = 0.0
    last_edited: float = 0.0
    searchable_at: float = 0.0


@dataclass
class LabState:
    seed: int = SEED
    items: dict[str, Item] = field(default_factory=dict)
    cursors: dict[str, dict[str, Any]] = field(default_factory=dict)
    enabled: dict[str, bool] = field(default_factory=lambda: {r: True for r in ALL_RULES})
    call_log: deque = field(default_factory=lambda: deque(maxlen=5000))
    req_times: deque = field(default_factory=lambda: deque(maxlen=200))
    call_n: int = 0
    rng: random.Random = field(default_factory=lambda: random.Random(SEED))


S = LabState()
app = FastAPI(title="lab", docs_url=None, redoc_url=None)


def reset(seed: int | None = None) -> None:
    global S
    s = SEED if seed is None else seed
    S = LabState(seed=s, rng=random.Random(s))


def _det(tag: str) -> float:
    """Deterministic pseudo-random in [0,1) from seed + tag + call index.

    Using a hash rather than the RNG stream means a rule's behaviour does not
    shift when an unrelated rule is toggled off.
    """
    h = hashlib.sha256(f"{S.seed}:{tag}:{S.call_n}".encode()).hexdigest()
    return int(h[:8], 16) / 0xFFFFFFFF


def on(rule: str) -> bool:
    return S.enabled.get(rule, False)


def _log(op: str, req: Any, status: int, resp: Any, rules: list[str]) -> None:
    S.call_log.append(
        {
            "n": S.call_n,
            "t": time.time(),
            "op": op,
            "request": req,
            "status": status,
            "response_summary": _summarise(resp),
            "rules_fired": rules,
        }
    )


def _summarise(resp: Any) -> Any:
    if isinstance(resp, dict):
        out = {}
        for k, v in resp.items():
            if isinstance(v, list):
                out[k] = f"<list len={len(v)}>"
            else:
                out[k] = v
        return out
    return resp


def _serialise(it: Item) -> dict[str, Any]:
    d = {
        "id": it.id,
        "title": it.title,
        "amount": it.amount,
        "due_date": it.due_date,
        "status": it.status,
        "assignee": it.assignee,
        "vendor": it.vendor,
        "archived": it.archived,
        "created": it.created,
        "last_edited": it.last_edited,
    }
    # rule: large_number_to_string
    if on("large_number_to_string") and isinstance(d["amount"], (int, float)):
        if d["amount"] is not None and d["amount"] > LARGE_NUMBER_THRESHOLD:
            d["amount"] = str(d["amount"])
    return d


# ---------------------------------------------------------------------------
# middleware: rate limit (rule 2)
# ---------------------------------------------------------------------------


@app.middleware("http")
async def rate_limit(request: Request, call_next):
    if request.url.path.startswith("/_control"):
        return await call_next(request)

    S.call_n += 1
    now = time.time()

    if on("rate_limit_flaky_header"):
        while S.req_times and now - S.req_times[0] > RATE_LIMIT_WINDOW_S:
            S.req_times.popleft()
        if len(S.req_times) >= RATE_LIMIT_N:
            headers = {}
            # rule: Retry-After present only ~60% of the time
            if _det("retry_after") < RETRY_AFTER_PRESENT_RATE:
                headers["Retry-After"] = "1"
            _log(request.url.path, None, 429, {"error": "rate_limited"}, ["rate_limit_flaky_header"])
            return JSONResponse(
                {"error": "rate_limited", "message": "too many requests"},
                status_code=429,
                headers=headers,
            )
        S.req_times.append(now)

    return await call_next(request)


# ---------------------------------------------------------------------------
# documented endpoints
# ---------------------------------------------------------------------------


@app.post("/v1/items")
async def create_item(body: dict[str, Any]):
    fired: list[str] = []
    now = time.time()

    title = str(body.get("title", ""))
    # rule: title_truncation
    if on("title_truncation") and len(title) > TITLE_CAP:
        title = title[:TITLE_CAP]
        fired.append("title_truncation")

    due = body.get("due_date")
    # rule: pre_epoch_date_null
    if on("pre_epoch_date_null") and isinstance(due, str) and due < EPOCH_CUTOFF:
        due = None
        fired.append("pre_epoch_date_null")

    it = Item(
        id=f"itm_{uuid.UUID(int=int(hashlib.sha256(f'{S.seed}:{S.call_n}'.encode()).hexdigest()[:32], 16)).hex[:12]}",
        title=title,
        amount=body.get("amount"),
        due_date=due,
        status=body.get("status", "open"),
        assignee=body.get("assignee"),
        vendor=body.get("vendor"),
        created=now,
        last_edited=now,
        # rule: write_search_lag
        searchable_at=now + (SEARCH_LAG_S if on("write_search_lag") else 0.0),
    )
    S.items[it.id] = it
    if on("write_search_lag"):
        fired.append("write_search_lag")

    # rule: write_502_after_commit -- item IS committed, then we 502
    if on("write_502_after_commit") and _det("w502") < WRITE_502_RATE:
        fired.append("write_502_after_commit")
        _log("create", body, 502, {"error": "bad_gateway"}, fired)
        return JSONResponse({"error": "bad_gateway", "message": "upstream failure"}, status_code=502)

    out = _serialise(it)
    _log("create", body, 200, out, fired)
    return out


@app.get("/v1/items/{item_id}")
async def get_item(item_id: str):
    fired: list[str] = []
    it = S.items.get(item_id)
    if it is None:
        _log("get", {"id": item_id}, 404, {"error": "not_found"}, fired)
        return JSONResponse({"error": "not_found"}, status_code=404)

    # rule: archived_get_404
    if on("archived_get_404") and it.archived:
        fired.append("archived_get_404")
        _log("get", {"id": item_id}, 404, {"error": "not_found"}, fired)
        return JSONResponse({"error": "not_found"}, status_code=404)

    out = _serialise(it)
    _log("get", {"id": item_id}, 200, out, fired)
    return out


@app.patch("/v1/items/{item_id}")
async def update_item(item_id: str, body: dict[str, Any]):
    fired: list[str] = []
    it = S.items.get(item_id)
    if it is None:
        _log("update", {"id": item_id, **body}, 404, {"error": "not_found"}, fired)
        return JSONResponse({"error": "not_found"}, status_code=404)

    unknown = [k for k in body if k not in KNOWN_ITEM_FIELDS]
    if unknown:
        # rule: unknown_field_ignored -- 200 and silently drop
        if on("unknown_field_ignored"):
            fired.append("unknown_field_ignored")
        else:
            _log("update", body, 400, {"error": "unknown_field"}, fired)
            return JSONResponse(
                {"error": "unknown_field", "fields": unknown}, status_code=400
            )

    for k, v in body.items():
        if k in KNOWN_ITEM_FIELDS:
            if k == "title" and on("title_truncation") and isinstance(v, str) and len(v) > TITLE_CAP:
                v = v[:TITLE_CAP]
                fired.append("title_truncation")
            setattr(it, k, v)
    it.last_edited = time.time()

    out = _serialise(it)
    _log("update", body, 200, out, fired)
    return out


@app.post("/v1/items/{item_id}/archive")
async def archive_item(item_id: str):
    it = S.items.get(item_id)
    if it is None:
        return JSONResponse({"error": "not_found"}, status_code=404)
    it.archived = True
    it.last_edited = time.time()
    _log("archive", {"id": item_id}, 200, {"ok": True}, [])
    return {"ok": True, "id": item_id, "archived": True}


@app.post("/v1/bulk_create")
async def bulk_create(body: dict[str, Any]):
    fired: list[str] = []
    items = body.get("items", []) or []
    accepted = items

    # rule: bulk_cap -- process 20, report success for all
    if on("bulk_cap") and len(items) > BULK_CAP:
        accepted = items[:BULK_CAP]
        fired.append("bulk_cap")

    ids = []
    now = time.time()
    for spec in accepted:
        S.call_n += 1
        title = str(spec.get("title", ""))
        if on("title_truncation") and len(title) > TITLE_CAP:
            title = title[:TITLE_CAP]
        it = Item(
            id=f"itm_{hashlib.sha256(f'{S.seed}:bulk:{S.call_n}'.encode()).hexdigest()[:12]}",
            title=title,
            amount=spec.get("amount"),
            due_date=spec.get("due_date"),
            status=spec.get("status", "open"),
            assignee=spec.get("assignee"),
            vendor=spec.get("vendor"),
            created=now,
            last_edited=now,
            searchable_at=now + (SEARCH_LAG_S if on("write_search_lag") else 0.0),
        )
        S.items[it.id] = it
        ids.append(it.id)

    # the lie: reports the full submitted count
    out = {"created": len(items), "ids": ids}
    _log("bulk_create", {"n_submitted": len(items)}, 200, out, fired)
    return out


@app.post("/v1/search")
async def search(body: dict[str, Any]):
    fired: list[str] = []
    now = time.time()

    filt = body.get("filter") or {}
    # rule: unknown_filter_field -- 200 + empty, no error
    unknown = [k for k in filt if k not in KNOWN_FILTER_FIELDS]
    if unknown:
        if on("unknown_filter_field"):
            fired.append("unknown_filter_field")
            out = {"results": [], "has_more": False, "next_cursor": None}
            _log("search", body, 200, out, fired)
            return out
        _log("search", body, 400, {"error": "invalid_filter"}, fired)
        return JSONResponse({"error": "invalid_filter", "fields": unknown}, status_code=400)

    # cursor handling
    cursor = body.get("cursor")
    offset = 0
    if cursor:
        c = S.cursors.get(cursor)
        if c is None:
            _log("search", body, 400, {"error": "invalid_cursor"}, fired)
            return JSONResponse({"error": "invalid_cursor"}, status_code=400)
        # rule: cursor_expiry
        if on("cursor_expiry") and now - c["issued"] > CURSOR_TTL_S:
            fired.append("cursor_expiry")
            _log("search", body, 400, {"error": "cursor_expired"}, fired)
            return JSONResponse({"error": "cursor_expired"}, status_code=400)
        offset = c["offset"]

    rows = list(S.items.values())

    # rule: write_search_lag
    visible = [r for r in rows if r.searchable_at <= now]
    if on("write_search_lag") and len(visible) != len(rows):
        fired.append("write_search_lag")
    rows = visible

    # rule: include_archived_flag (undocumented)
    include_archived = bool(body.get("include_archived", False))
    if on("include_archived_flag"):
        if not include_archived:
            before = len(rows)
            rows = [r for r in rows if not r.archived]
            if len(rows) != before:
                fired.append("include_archived_flag")
    # when the rule is OFF, archived rows are simply included

    for k, v in filt.items():
        rows = [r for r in rows if getattr(r, k, None) == v]

    # rule: sort_created_is_edited
    sort = body.get("sort")
    if sort == "created":
        if on("sort_created_is_edited"):
            fired.append("sort_created_is_edited")
            rows.sort(key=lambda r: r.last_edited)
        else:
            rows.sort(key=lambda r: r.created)
    elif sort == "last_edited":
        rows.sort(key=lambda r: r.last_edited)

    # rule: page_size_cap
    requested = int(body.get("page_size", 25) or 25)
    effective = requested
    if on("page_size_cap") and requested > PAGE_SIZE_CAP:
        effective = PAGE_SIZE_CAP
        fired.append("page_size_cap")

    page = rows[offset : offset + effective]
    has_more = (offset + effective) < len(rows)
    next_cursor = None
    if has_more:
        next_cursor = f"cur_{hashlib.sha256(f'{S.seed}:{S.call_n}:{offset}'.encode()).hexdigest()[:16]}"
        S.cursors[next_cursor] = {"offset": offset + effective, "issued": now}

    out = {
        "results": [_serialise(r) for r in page],
        "has_more": has_more,
        "next_cursor": next_cursor,
    }
    _log("search", body, 200, out, fired)
    return out


# ---------------------------------------------------------------------------
# control plane -- the agent must never call these; bench and the demo do
# ---------------------------------------------------------------------------


@app.get("/_control/state")
async def control_state():
    return {
        "seed": S.seed,
        "enabled": S.enabled,
        "items": len(S.items),
        "calls": S.call_n,
        "time_scale": TIME_SCALE,
    }


@app.post("/_control/rules/{rule_id}")
async def control_rule(rule_id: str, body: dict[str, Any]):
    if rule_id not in S.enabled:
        return JSONResponse({"error": "unknown_rule", "known": ALL_RULES}, status_code=404)
    S.enabled[rule_id] = bool(body.get("enabled", True))
    return {"rule": rule_id, "enabled": S.enabled[rule_id]}


@app.post("/_control/seed")
async def control_seed(body: dict[str, Any]):
    """Insert items directly, bypassing every rule.

    Task ground truth has to be exact, so setup must not go through the
    lying surface. The agent still has to *discover* that truth through the
    lying surface, which is the point.
    """
    n = int(body.get("n", 10))
    vendor = body.get("vendor", "SeedCo")
    archived_every = int(body.get("archived_every", 0))
    amount_base = float(body.get("amount_base", 1000.0))
    big_every = int(body.get("big_every", 0))

    now = time.time()
    made = []
    for i in range(n):
        amt = amount_base + i
        if big_every and i % big_every == 0:
            amt = LARGE_NUMBER_THRESHOLD + 1000 + i
        it = Item(
            id=f"itm_{hashlib.sha256(f'{S.seed}:seed:{vendor}:{i}'.encode()).hexdigest()[:12]}",
            title=f"{vendor}-invoice-{i:04d}",
            amount=amt,
            due_date="2026-10-01",
            status="open",
            vendor=vendor,
            archived=bool(archived_every and i % archived_every == 0),
            created=now - (n - i),
            last_edited=now - (n - i),
            searchable_at=0.0,
        )
        S.items[it.id] = it
        made.append(it)

    total = sum(float(x.amount or 0) for x in made)
    return {
        "ok": True,
        "n": len(made),
        "vendor": vendor,
        "archived": sum(1 for x in made if x.archived),
        "active": sum(1 for x in made if not x.archived),
        "total_amount": total,
        "active_amount": sum(float(x.amount or 0) for x in made if not x.archived),
    }


@app.post("/_control/age_cursor/{cursor}")
async def control_age_cursor(cursor: str, body: dict[str, Any] | None = None):
    """Backdate a cursor so expiry can be tested without waiting 60s."""
    secs = float((body or {}).get("seconds", CURSOR_TTL_S + 5))
    c = S.cursors.get(cursor)
    if c is None:
        return JSONResponse({"error": "unknown_cursor"}, status_code=404)
    c["issued"] -= secs
    return {"ok": True, "cursor": cursor, "aged_seconds": secs}


@app.post("/_control/reset")
async def control_reset(body: dict[str, Any] | None = None):
    seed = (body or {}).get("seed")
    reset(int(seed) if seed is not None else None)
    return {"ok": True, "seed": S.seed}


@app.get("/_control/calls")
async def control_calls(limit: int = 200):
    return {"calls": list(S.call_log)[-limit:]}


@app.get("/_control/rules_fired")
async def control_rules_fired():
    """Which hidden rules have actually been triggered at least once.

    A rule the agent could not possibly have observed should not count
    against recall, so bench uses this to compute an honest denominator.
    """
    seen: dict[str, int] = {r: 0 for r in ALL_RULES}
    for c in S.call_log:
        for r in c.get("rules_fired", []):
            seen[r] = seen.get(r, 0) + 1
    return {"fired": seen, "observable": [r for r, n in seen.items() if n > 0]}


@app.get("/healthz")
async def healthz():
    return {"ok": True}
