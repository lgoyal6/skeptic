"""
Probe templates: the experiments the agent knows how to run.

A probe is not "call the API again and hope". It is a parameterised
experiment with a known shape, chosen because its outcome differs depending
on which rival hypothesis is true.

Each template declares which hypothesis classes it can separate and roughly
what it costs. Selection is a greedy score -- hypotheses split per estimated
call -- not a formal information-gain calculation. The README says so, because
a greedy discriminator that works is worth more than an entropy formula that
is hard to verify from a three-minute video.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from agent.adapters.lab import LabAdapter


@dataclass
class Observation:
    template: str
    params: dict[str, Any]
    calls: int
    facts: dict[str, Any]          # compact, comparable outcome
    narrative: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "template": self.template,
            "params": self.params,
            "calls": self.calls,
            "facts": self.facts,
            "narrative": self.narrative,
        }


@dataclass
class ProbeTemplate:
    name: str
    描述: str = ""                 # placeholder guard, unused
    description: str = ""
    splits: list[str] = field(default_factory=list)
    est_calls: int = 2
    run: Callable[[LabAdapter, dict[str, Any]], Observation] | None = None


# ---------------------------------------------------------------------------
# implementations
# ---------------------------------------------------------------------------


def _ensure_population(a: LabAdapter, base: dict[str, Any], want: int = 60) -> int:
    """Make sure enough rows exist for a search sweep to mean anything.

    A page-size sweep against an empty vendor returns 0 for every value. That
    is not evidence of a cap or of its absence -- it is no evidence at all.
    It happened: a sweep over `vendor=Probe02` (zero rows) returned 0/0 and
    the verdict read it as refuting a belief that was true. An experiment has
    to establish the conditions under which its variable is observable.
    """
    vendor = ((base or {}).get("filter") or {}).get("vendor")
    if not vendor:
        return 0
    sc, body = a.search(filter={"vendor": vendor}, page_size=50)
    have = len((body or {}).get("results", [])) if sc == 200 else 0
    if (body or {}).get("has_more") or have >= want:
        return have
    made = 0
    while have + made < want:
        n = min(20, want - have - made)  # bulk silently caps at 20
        a.bulk_create([{"title": f"{vendor}-pop-{made + i}", "vendor": vendor} for i in range(n)])
        made += n
    time.sleep(2.4)  # outlast the write/search lag before measuring
    return have + made


def _boundary(a: LabAdapter, p: dict[str, Any]) -> Observation:
    """Sweep one parameter across values and record what comes back.

    Separates "hard cap" from "value ignored" from "coercion", because each
    produces a different shape across the sweep.
    """
    op = p.get("op", "search")
    param = p.get("param", "page_size")
    values = p.get("values") or [1, 10, 50, 200]
    base = dict(p.get("base") or {})
    rows: list[dict[str, Any]] = []
    narrative: list[str] = []

    populated = None
    if op == "search":
        populated = _ensure_population(a, base, want=max(60, max(
            [v for v in values if isinstance(v, int)] or [60]) // 2))
        narrative.append(f"population for sweep: {populated} rows")

    for v in values:
        if op == "search":
            args = dict(base)
            args[param] = v
            sc, body = a.search(**args)
            n = len((body or {}).get("results", [])) if sc == 200 else None
            rows.append({"sent": v, "status": sc, "returned": n,
                         "has_more": (body or {}).get("has_more")})
            narrative.append(f"{param}={v} -> {n} rows, has_more={(body or {}).get('has_more')}")
        else:  # create-style: send a value, read back what was stored
            args = dict(base)
            args[param] = v
            sc, body = a.create(**args)
            got = (body or {}).get(param) if isinstance(body, dict) else None
            stored = len(got) if isinstance(got, str) else got
            rows.append({"sent": v if not isinstance(v, str) else f"<len {len(v)}>",
                         "status": sc, "stored": stored,
                         "stored_type": type(got).__name__})
            narrative.append(f"{param} sent {rows[-1]['sent']} -> stored {stored} ({type(got).__name__})")

    returned = [r.get("returned") for r in rows if r.get("returned") is not None]
    ceiling = max(returned) if returned else None
    saturates = len(set(returned[-2:])) == 1 if len(returned) >= 2 else False

    return Observation(
        template="boundary",
        params=p,
        calls=len(values),
        facts={
            "population": populated,
            "sweep": rows,
            "ceiling": ceiling,
            "saturates_at_ceiling": saturates,
            "tracks_request": returned == sorted(returned) and not saturates,
        },
        narrative=narrative,
    )


def _timing(a: LabAdapter, p: dict[str, Any]) -> Observation:
    """Do a thing, observe, wait, observe again.

    Separates a transient (indexing lag) from a permanent property (hard cap).
    """
    vendor = p.get("vendor", "ProbeTiming")
    wait_s = float(p.get("wait_s", 3.0))
    base = dict(p.get("base") or {"filter": {"vendor": vendor}, "page_size": 50})

    sc0, b0 = a.search(**base)
    n0 = len((b0 or {}).get("results", [])) if sc0 == 200 else None
    time.sleep(wait_s)
    sc1, b1 = a.search(**base)
    n1 = len((b1 or {}).get("results", [])) if sc1 == 200 else None

    return Observation(
        template="timing",
        params=p,
        calls=2,
        facts={"before": n0, "after": n1, "waited_s": wait_s, "changed": n0 != n1,
               "delta": (n1 - n0) if (n0 is not None and n1 is not None) else None},
        narrative=[f"{n0} rows, waited {wait_s}s, then {n1} rows"],
    )


def _idempotency(a: LabAdapter, p: dict[str, Any]) -> Observation:
    """Create until a failure status appears, then check whether it committed."""
    vendor = p.get("vendor", "ProbeIdem")
    attempts = int(p.get("attempts", 40))
    failed_title = None
    statuses: list[int] = []

    for i in range(attempts):
        title = f"{vendor}-probe-{i:03d}"
        sc, _ = a.create(title=title, vendor=vendor)
        statuses.append(sc)
        if sc >= 500:
            failed_title = title
            break

    time.sleep(2.4)
    sc, body = a.search(filter={"vendor": vendor}, page_size=50, include_archived=True)
    titles = [r["title"] for r in (body or {}).get("results", [])]
    committed = failed_title in titles if failed_title else None

    return Observation(
        template="idempotency",
        params=p,
        calls=len(statuses) + 1,
        facts={
            "failure_status": statuses[-1] if statuses and statuses[-1] >= 500 else None,
            "failed_title": failed_title,
            "committed_despite_failure": committed,
            "attempts_to_first_failure": len(statuses) if failed_title else None,
        },
        narrative=[
            f"create returned {statuses[-1] if statuses else '?'} on attempt {len(statuses)}",
            f"item {'EXISTS' if committed else 'absent'} afterwards",
        ] if failed_title else ["no failure status observed in this many attempts"],
    )


def _consistency(a: LabAdapter, p: dict[str, Any]) -> Observation:
    """Write, then read back, and compare what was stored to what was sent."""
    fields = dict(p.get("fields") or {"title": "consistency-probe"})
    vendor = p.get("vendor", "ProbeConsist")
    fields.setdefault("vendor", vendor)

    sc, created = a.create(**fields)
    item_id = (created or {}).get("id") if isinstance(created, dict) else None
    echo = {k: (created or {}).get(k) for k in fields} if isinstance(created, dict) else {}

    readback = {}
    get_status = None
    if item_id:
        get_status, got = a.get(item_id)
        if isinstance(got, dict):
            readback = {k: got.get(k) for k in fields}

    mismatches = {
        k: {"sent": v, "stored": echo.get(k)}
        for k, v in fields.items()
        if echo.get(k) != v
    }
    return Observation(
        template="consistency",
        params=p,
        calls=2,
        facts={
            "create_status": sc,
            "get_status": get_status,
            "echo_matches_sent": not mismatches,
            "mismatches": mismatches,
            "readback_matches_echo": readback == echo if readback else None,
        },
        narrative=[f"sent {list(fields)}, mismatched {list(mismatches) or 'nothing'}"],
    )


def _ordering(a: LabAdapter, p: dict[str, Any]) -> Observation:
    """Create in a known order, edit one early row, then sort and compare."""
    vendor = p.get("vendor", "ProbeOrder")
    ids: list[tuple[str, str]] = []
    for i in range(3):
        sc, body = a.create(title=f"{vendor}-o{i}", vendor=vendor)
        if isinstance(body, dict) and body.get("id"):
            ids.append((body["title"], body["id"]))
        time.sleep(0.15)
    time.sleep(2.4)
    if ids:
        a.update(ids[0][1], status="touched")  # oldest by creation, newest by edit

    sc, body = a.search(filter={"vendor": vendor}, page_size=50, sort=p.get("sort", "created"))
    order = [r["title"] for r in (body or {}).get("results", [])]
    creation_order = [t for t, _ in ids]

    return Observation(
        template="ordering",
        params=p,
        calls=len(ids) + 3,
        facts={
            "creation_order": creation_order,
            "returned_order": order,
            "matches_creation": order[: len(creation_order)] == creation_order,
            "edited_row_moved_last": bool(order) and order[-1] == creation_order[0],
        },
        narrative=[f"created {creation_order}, sort={p.get('sort','created')} returned {order}"],
    )


def _header_burst(a: LabAdapter, p: dict[str, Any]) -> Observation:
    """Fire fast and watch statuses and headers."""
    n = int(p.get("n", 10))
    statuses: list[int] = []
    retry_after: list[bool] = []
    for _ in range(n):
        sc, body = a.search(page_size=1)
        statuses.append(sc)
        if sc == 429 and isinstance(body, dict):
            retry_after.append(bool(body.get("_retry_after_present")))
    n429 = statuses.count(429)
    return Observation(
        template="header_burst",
        params=p,
        calls=n,
        facts={
            "n": n,
            "throttled": n429,
            "first_throttle_at": statuses.index(429) + 1 if 429 in statuses else None,
            "retry_after_present": retry_after.count(True),
            "retry_after_absent": retry_after.count(False),
            "header_reliable": (len(set(retry_after)) <= 1) if retry_after else None,
        },
        narrative=[f"{n429}/{n} throttled; Retry-After present on {retry_after.count(True)}/{len(retry_after)}"],
    )


def _flag_discovery(a: LabAdapter, p: dict[str, Any]) -> Observation:
    """Send an undocumented parameter and see whether the result set changes."""
    flag = p.get("flag", "include_archived")
    base = dict(p.get("base") or {"filter": {"vendor": "ProbeFlag"}, "page_size": 50})

    sc0, b0 = a.search(**base)
    n0 = len((b0 or {}).get("results", [])) if sc0 == 200 else None

    with_flag = dict(base)
    with_flag[flag] = p.get("value", True)
    sc1, b1 = a.search(**with_flag)
    n1 = len((b1 or {}).get("results", [])) if sc1 == 200 else None

    return Observation(
        template="flag_discovery",
        params=p,
        calls=2,
        facts={
            "flag": flag,
            "without": n0,
            "with": n1,
            "flag_changes_result": n0 != n1,
            "flag_rejected": sc1 >= 400,
            "extra_rows": (n1 - n0) if (n0 is not None and n1 is not None) else None,
        },
        narrative=[f"without {flag}: {n0} rows; with {flag}: {n1} rows (status {sc1})"],
    )


TEMPLATES: dict[str, ProbeTemplate] = {
    "boundary": ProbeTemplate(
        name="boundary",
        description="Sweep a parameter across values; a cap saturates, a coercion bends, an ignored value does nothing.",
        splits=["silent_truncation", "silent_coercion", "mislabelled_semantics"],
        est_calls=4,
        run=_boundary,
    ),
    "timing": ProbeTemplate(
        name="timing",
        description="Observe, wait, observe again; a transient changes and a permanent property does not.",
        splits=["eventual_consistency", "silent_truncation", "expiry"],
        est_calls=2,
        run=_timing,
    ),
    "idempotency": ProbeTemplate(
        name="idempotency",
        description="Provoke a failure status, then check whether the write landed anyway.",
        splits=["idempotency_hazard"],
        est_calls=12,
        run=_idempotency,
    ),
    "consistency": ProbeTemplate(
        name="consistency",
        description="Write then read back; compare stored against sent.",
        splits=["silent_coercion", "silent_truncation", "undocumented_flag"],
        est_calls=2,
        run=_consistency,
    ),
    "ordering": ProbeTemplate(
        name="ordering",
        description="Create in a known order, edit one row, then sort and see which order comes back.",
        splits=["mislabelled_semantics"],
        est_calls=6,
        run=_ordering,
    ),
    "header_burst": ProbeTemplate(
        name="header_burst",
        description="Fire rapidly; watch for throttling and whether the advisory header is reliable.",
        splits=["rate_limit"],
        est_calls=10,
        run=_header_burst,
    ),
    "flag_discovery": ProbeTemplate(
        name="flag_discovery",
        description="Send an undocumented parameter and see whether the result set changes.",
        splits=["undocumented_flag", "silent_coercion"],
        est_calls=2,
        run=_flag_discovery,
    ),
}


def catalogue() -> str:
    lines = []
    for t in TEMPLATES.values():
        lines.append(f"  {t.name} (~{t.est_calls} calls): {t.description}")
        lines.append(f"      separates: {', '.join(t.splits)}")
    return "\n".join(lines)


def score_template(t: ProbeTemplate, classes: list[str]) -> float:
    """Greedy discrimination score: rival classes separated per call."""
    hit = len({c for c in classes if c in t.splits})
    if hit < 1:
        return 0.0
    return hit / max(1, t.est_calls)


def choose(classes: list[str]) -> list[tuple[str, float]]:
    ranked = [(n, score_template(t, classes)) for n, t in TEMPLATES.items()]
    return sorted([r for r in ranked if r[1] > 0], key=lambda r: -r[1])
