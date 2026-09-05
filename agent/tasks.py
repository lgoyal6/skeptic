"""
Tasks the agent is asked to do, with deterministic success checks.

A task is not "did the agent seem helpful". It is a number the agent must
report correctly, where reporting it correctly is only possible if the agent
has worked around the tool's undocumented behaviour. That is what makes the
success curve mean something.

Each task names the hidden rules it is designed to expose. Nothing in the
task text hints at them.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

CTL = "http://127.0.0.1:8077"


@dataclass
class TaskResult:
    ok: bool
    detail: str
    expected: Any = None
    got: Any = None


@dataclass
class Task:
    id: str
    goal: str                                   # what the agent is told
    setup: Callable[[dict[str, Any]], dict[str, Any]]
    check: Callable[[dict[str, Any], dict[str, Any]], TaskResult]
    exposes: list[str] = field(default_factory=list)
    answer_schema: str = ""


def _ctl(path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return httpx.post(f"{CTL}/_control{path}", json=payload or {}, timeout=30.0).json()


# ---------------------------------------------------------------------------
# inventory: count and total every invoice for a vendor
# ---------------------------------------------------------------------------


def _setup_inventory(cfg: dict[str, Any]) -> dict[str, Any]:
    vendor = cfg.get("vendor", "Northwind")
    seeded = _ctl(
        "/seed",
        {"n": cfg.get("n", 73), "vendor": vendor, "archived_every": 7, "big_every": 11},
    )
    return {"vendor": vendor, **seeded}


def _check_inventory(state: dict[str, Any], answer: dict[str, Any]) -> TaskResult:
    want_n = state["n"]          # every invoice, archived included
    want_total = round(float(state["total_amount"]), 2)
    got_n = answer.get("count")
    got_total = answer.get("total_amount")
    try:
        got_n = int(got_n)
        got_total = round(float(got_total), 2)
    except (TypeError, ValueError):
        return TaskResult(False, "answer missing or unparseable count/total_amount",
                          {"count": want_n, "total_amount": want_total},
                          {"count": answer.get("count"), "total_amount": answer.get("total_amount")})
    ok_n = got_n == want_n
    ok_t = abs(got_total - want_total) < 0.01
    bits = []
    if not ok_n:
        bits.append(f"count {got_n} != {want_n}")
    if not ok_t:
        bits.append(f"total {got_total} != {want_total}")
    return TaskResult(
        ok_n and ok_t,
        "exact" if (ok_n and ok_t) else "; ".join(bits),
        {"count": want_n, "total_amount": want_total},
        {"count": got_n, "total_amount": got_total},
    )


INVENTORY = Task(
    id="inventory",
    goal=(
        "Report the total number of invoices for vendor {vendor} and the sum of their "
        "amounts. Include every invoice on record, whatever its state. "
        "Answer with JSON: {{\"count\": <int>, \"total_amount\": <number>}}"
    ),
    setup=_setup_inventory,
    check=_check_inventory,
    exposes=[
        "page_size_cap",          # 73 rows, must paginate
        "include_archived_flag",  # ~11 archived and invisible by default
        "large_number_to_string", # ~7 amounts arrive as strings and break naive sums
        "rate_limit_flaky_header",
        "cursor_expiry",
    ],
    answer_schema='{"count": int, "total_amount": number}',
)


# ---------------------------------------------------------------------------
# exactly_once: create N invoices, no duplicates, despite a lying 502
# ---------------------------------------------------------------------------


def _setup_exactly_once(cfg: dict[str, Any]) -> dict[str, Any]:
    vendor = cfg.get("vendor", "Acme")
    _ctl("/reset", {"seed": cfg.get("seed", 1337)})
    return {"vendor": vendor, "n": cfg.get("n", 12)}


def _check_exactly_once(state: dict[str, Any], answer: dict[str, Any]) -> TaskResult:
    vendor, n = state["vendor"], state["n"]
    time.sleep(2.4)  # outlast the search lag before auditing
    rows: list[dict[str, Any]] = []
    cursor = None
    for _ in range(12):
        body: dict[str, Any] = {
            "filter": {"vendor": vendor},
            "page_size": 50,
            "include_archived": True,
        }
        if cursor:
            body["cursor"] = cursor
        r = httpx.post(f"{CTL}/v1/search", json=body, timeout=30.0).json()
        rows.extend(r.get("results", []))
        cursor = r.get("next_cursor")
        if not cursor:
            break
    titles = [x["title"] for x in rows]
    uniq = set(titles)
    dupes = len(titles) - len(uniq)
    ok = len(uniq) == n and dupes == 0
    return TaskResult(
        ok,
        "exactly once" if ok else f"{len(uniq)} distinct titles, {dupes} duplicates (wanted {n}, 0)",
        {"distinct": n, "duplicates": 0},
        {"distinct": len(uniq), "duplicates": dupes},
    )


EXACTLY_ONCE = Task(
    id="exactly_once",
    goal=(
        "Create exactly {n} invoices for vendor {vendor}, titled {vendor}-po-0001 through "
        "{vendor}-po-{n:04d}. Every title must exist exactly once when you are done, with no "
        "duplicates. Verify your own work before answering. "
        "Answer with JSON: {{\"created\": <int>}}"
    ),
    setup=_setup_exactly_once,
    check=_check_exactly_once,
    exposes=["write_502_after_commit", "write_search_lag", "title_truncation"],
    answer_schema='{"created": int}',
)


# ---------------------------------------------------------------------------
# oldest_three: the ordering lie
# ---------------------------------------------------------------------------


def _setup_oldest(cfg: dict[str, Any]) -> dict[str, Any]:
    vendor = cfg.get("vendor", "Vertex")
    seeded = _ctl("/seed", {"n": 12, "vendor": vendor})
    # touch two early rows so edit-order and creation-order disagree
    rows = httpx.post(
        f"{CTL}/v1/search",
        json={"filter": {"vendor": vendor}, "page_size": 50, "sort": "last_edited"},
        timeout=30.0,
    ).json()["results"]
    by_title = {r["title"]: r for r in rows}
    for t in (f"{vendor}-invoice-0000", f"{vendor}-invoice-0001"):
        if t in by_title:
            httpx.patch(f"{CTL}/v1/items/{by_title[t]['id']}", json={"status": "reviewed"}, timeout=30.0)
    return {"vendor": vendor, "expected": [f"{vendor}-invoice-{i:04d}" for i in range(3)]}


def _check_oldest(state: dict[str, Any], answer: dict[str, Any]) -> TaskResult:
    want = state["expected"]
    got = answer.get("titles")
    ok = isinstance(got, list) and [str(x) for x in got[:3]] == want
    return TaskResult(ok, "correct order" if ok else "wrong titles or order", want, got)


OLDEST_THREE = Task(
    id="oldest_three",
    goal=(
        "List the titles of the three OLDEST invoices for vendor {vendor}, oldest first, "
        "by when they were created. "
        "Answer with JSON: {{\"titles\": [<string>, <string>, <string>]}}"
    ),
    setup=_setup_oldest,
    check=_check_oldest,
    exposes=["sort_created_is_edited", "page_size_cap"],
    answer_schema='{"titles": [string, string, string]}',
)


ALL_TASKS = {t.id: t for t in (INVENTORY, EXACTLY_ONCE, OLDEST_THREE)}


def get(task_id: str) -> Task:
    if task_id not in ALL_TASKS:
        raise KeyError(f"unknown task {task_id}; have {sorted(ALL_TASKS)}")
    return ALL_TASKS[task_id]
