"""
Counterfactual replay: what would a confirmed belief have saved, if we had
held it from the start?

A belief's `action` is a claim about the future ("clamp page_size to 50").
Nobody has to take that claim on faith: every call the agent has ever made
against `lab` is sitting in runs/*.jsonl, in order, with its request and
response. Replaying a confirmed belief against that history turns "clamp
page_size to 50 saves tokens" into "this would have avoided N calls across M
of your last K runs" -- a measurement, not a projection.

This module deliberately does not reuse ContractLayer's anomaly detection
verbatim. A belief's `.cls` is a coarse narrative label chosen when the
hypothesis was formed (see beliefs.py) and does not always equal the raw
Anomaly.kind that triggered it -- e.g. a belief classed `silent_coercion` can
have been minted off a `silent_null` anomaly, because "the field is coerced
away" was the operator's story, not the wire-level vocabulary. The mint path
always records the true originating signature as a `history` event named
"signature" (see cli.py / bench/session.py), so that is the ground truth this
module dispatches on, with `.cls` kept only as a last-resort fallback for
beliefs minted without that bookkeeping.

Being wrong in the generous direction is worse than being wrong in the
stingy one: a saving this module cannot point at a specific call for is not
reported. See each detector below for the conservative call it makes, and
the `notes` a report carries when a class has no detector at all.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from agent.beliefs import Belief, BeliefStore, Status

# Filter fields the API actually recognises. Mirrors
# shim.guards.RejectUnknownFilter.known -- kept as a separate constant here
# because replay must work even when no guard for this belief exists yet.
KNOWN_FILTER_FIELDS = ("status", "assignee", "vendor", "archived")

# "~2s" per the brief. contract.py's own live cross-call check uses 1.5s;
# replay is allowed to be a little more generous since it is scoring history,
# not gating a live call.
EVENTUAL_CONSISTENCY_WINDOW_S = 2.0


@dataclass
class Call:
    """One logged call, trimmed to what replay needs."""

    n: int
    t: float
    op: str
    request: dict[str, Any]
    status: int
    response: Any


@dataclass
class Hit:
    """One concrete instance of a belief's domain showing up in a run.

    `wasted` is a call count, not a boolean -- a single hit can span several
    calls (a truncated page plus the follow-up pages it forced), and some
    hits (a field silently gone null) touch a call without wasting it: the
    write still landed, only one field of it was lost.
    """

    call_ns: list[int]
    wasted: int
    reason: str


Detector = Callable[[list[Call]], list[Hit]]


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------


def _parse_call(raw: dict[str, Any]) -> Call:
    return Call(
        n=int(raw["n"]),
        t=float(raw["t"]),
        op=str(raw["op"]),
        request=raw.get("request") or {},
        status=int(raw["status"]),
        response=raw.get("response"),
    )


def _load_run(path: Path) -> tuple[list[Call], int]:
    calls: list[Call] = []
    skipped = 0
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            calls.append(_parse_call(json.loads(line)))
        except Exception:
            skipped += 1
    calls.sort(key=lambda c: c.n)
    return calls, skipped


def _load_all_runs(runs_dir: str) -> tuple[dict[str, list[Call]], int]:
    """run_id -> its ordered calls. run_id is the filename stem, which is
    also what runs/history.jsonl uses to key its own records (see
    agent/adapters/lab.py and bench/session.py -- both write to
    runs/<run_id>.jsonl and key history the same way)."""
    root = Path(runs_dir)
    runs: dict[str, list[Call]] = {}
    skipped_total = 0
    if not root.exists():
        return runs, 0
    for path in sorted(root.glob("*.jsonl")):
        if path.name == "history.jsonl":
            continue
        calls, skipped = _load_run(path)
        skipped_total += skipped
        if calls:
            runs[path.stem] = calls
    return runs, skipped_total


def _load_history(runs_dir: str) -> tuple[dict[str, dict[str, Any]], int]:
    path = Path(runs_dir) / "history.jsonl"
    out: dict[str, dict[str, Any]] = {}
    skipped = 0
    if not path.exists():
        return out, 0
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            skipped += 1
            continue
        rid = rec.get("run_id")
        if rid:
            out[rid] = rec
    return out, skipped


# --------------------------------------------------------------------------
# detectors -- one per raw anomaly kind (+ parameter/operation where the
# kind alone is ambiguous), each returning concrete, cite-able hits
# --------------------------------------------------------------------------


def _detect_page_size_cap(calls: list[Call]) -> list[Hit]:
    """Asked for more than the tool actually hands back in one page, with
    has_more=true. The wasted unit is the surprising call itself plus every
    subsequent call that only exists to keep paging the same query via its
    cursor -- calls that a belief-informed agent would still have needed to
    make eventually, except it would not have been surprised into them one
    at a time. We count the whole chain because the brief's own example
    ("a search asking for more... plus any follow-up paging") describes it
    as one unit; the alternative (excluding the trigger call) would still
    require inventing where the chain "really" starts."""
    hits: list[Hit] = []
    ordered = [c for c in calls if c.op == "search"]
    consumed: set[int] = set()
    for c in ordered:
        if c.n in consumed:
            continue
        asked = c.request.get("page_size")
        resp = c.response if isinstance(c.response, dict) else {}
        got = len(resp.get("results") or [])
        if not (isinstance(asked, int) and got < asked and resp.get("has_more")):
            continue
        chain = [c.n]
        consumed.add(c.n)
        cursor = resp.get("next_cursor")
        while cursor:
            nxt = next(
                (x for x in ordered if x.n > chain[-1] and x.request.get("cursor") == cursor),
                None,
            )
            if nxt is None:
                break
            chain.append(nxt.n)
            consumed.add(nxt.n)
            nresp = nxt.response if isinstance(nxt.response, dict) else {}
            cursor = nresp.get("next_cursor") if nresp.get("has_more") else None
        hits.append(
            Hit(
                call_ns=chain,
                wasted=len(chain),
                reason=(
                    f"asked page_size={asked}, got {got} with has_more=true on call {c.n}"
                    + (f"; {len(chain) - 1} follow-up page(s) chased the same query" if len(chain) > 1 else "")
                ),
            )
        )
    return hits


def _detect_unknown_filter_zero_result(calls: list[Call]) -> list[Hit]:
    """A filter key the API doesn't recognise silently yields zero rows
    instead of a 400. The whole call was wasted: nothing came back, and
    nothing about the 200 status said why."""
    hits: list[Hit] = []
    for c in calls:
        if c.op != "search" or c.status != 200 or not isinstance(c.response, dict):
            continue
        filt = c.request.get("filter")
        if not isinstance(filt, dict):
            continue
        unknown = [k for k in filt if k not in KNOWN_FILTER_FIELDS]
        if not unknown:
            continue
        if len(c.response.get("results") or []) == 0:
            hits.append(
                Hit(call_ns=[c.n], wasted=1, reason=f"unknown filter field(s) {unknown} on call {c.n} returned zero rows")
            )
    return hits


def _detect_idempotent_retry(calls: list[Call]) -> list[Hit]:
    """A create that 5xx'd, followed later by a create of the *same title*
    that succeeded. That second call is the duplicate the agent made
    because it did not know the first one may already have landed."""
    hits: list[Hit] = []
    fails = [c for c in calls if c.op == "create" and 500 <= c.status < 600]
    for f in fails:
        title = (f.request or {}).get("title")
        if not title:
            continue
        for c in calls:
            if c.n <= f.n or c.op != "create" or c.status != 200:
                continue
            if (c.request or {}).get("title") == title:
                hits.append(
                    Hit(
                        call_ns=[c.n],
                        wasted=1,
                        reason=f"call {c.n} retried the {f.status} on call {f.n} with the same title {title!r}",
                    )
                )
                break
    return hits


def _detect_eventual_consistency(calls: list[Call]) -> list[Hit]:
    """A search filtered on the vendor of a just-created item, issued
    within EVENTUAL_CONSISTENCY_WINDOW_S of that create, that doesn't
    contain the new row. The search call was wasted: it ran too soon to
    ever have found what it was looking for."""
    hits: list[Hit] = []
    creates = [c for c in calls if c.op == "create" and c.status == 200 and isinstance(c.response, dict)]
    for c in calls:
        if c.op != "search" or c.status != 200 or not isinstance(c.response, dict):
            continue
        vendor = (c.request.get("filter") or {}).get("vendor")
        if not vendor:
            continue
        ids_seen = {r.get("id") for r in c.response.get("results", [])}
        for p in creates:
            if p.n >= c.n or (p.request or {}).get("vendor") != vendor:
                continue
            gap = c.t - p.t
            if 0 <= gap < EVENTUAL_CONSISTENCY_WINDOW_S and p.response.get("id") not in ids_seen:
                hits.append(
                    Hit(
                        call_ns=[c.n],
                        wasted=1,
                        reason=f"call {c.n} searched {gap:.2f}s after create {p.n} (vendor={vendor!r}) and missed the new row",
                    )
                )
                break
    return hits


def _detect_archived_invisible_get(calls: list[Call]) -> list[Hit]:
    """GET 404 for an id this same run created (and, typically, archived).
    The whole call was wasted: it asked for something it had proof exists."""
    hits: list[Hit] = []
    known_ids = {
        c.response.get("id")
        for c in calls
        if c.op in ("create", "archive") and isinstance(c.response, dict) and c.response.get("id")
    }
    for c in calls:
        if c.op == "get" and c.status == 404:
            wanted = (c.request or {}).get("id")
            if wanted in known_ids:
                hits.append(Hit(call_ns=[c.n], wasted=1, reason=f"call {c.n} got 404 for id {wanted}, created earlier in this run"))
    return hits


def _detect_archived_invisible_search(calls: list[Call]) -> list[Hit]:
    """A search (without include_archived) for the vendor of an item this
    run itself archived, that excludes that item. The call still returned
    other rows, if any -- it's the missing row that is the belief's domain,
    not the call as a whole, so we count it as touched but not wasted."""
    hits: list[Hit] = []
    vendor_by_id: dict[str, str] = {}
    archived_at: dict[str, int] = {}
    for c in calls:
        if c.op == "create" and isinstance(c.response, dict) and c.response.get("id"):
            v = (c.request or {}).get("vendor")
            if v:
                vendor_by_id[c.response["id"]] = v
        if c.op == "archive" and isinstance(c.response, dict) and c.response.get("id"):
            archived_at[c.response["id"]] = c.n
    for c in calls:
        if c.op != "search" or c.status != 200 or not isinstance(c.response, dict):
            continue
        if c.request.get("include_archived"):
            continue
        vendor = (c.request.get("filter") or {}).get("vendor")
        if not vendor:
            continue
        ids_seen = {r.get("id") for r in c.response.get("results", [])}
        for item_id, arch_n in archived_at.items():
            if arch_n >= c.n or vendor_by_id.get(item_id) != vendor:
                continue
            if item_id not in ids_seen:
                hits.append(
                    Hit(call_ns=[c.n], wasted=0, reason=f"call {c.n} excluded archived id {item_id} with no include_archived set")
                )
    return hits


def _detect_bad_status_burn(calls: list[Call]) -> list[Hit]:
    """429 from a tool documented as having no rate limit. The call
    returned nothing; if it had been paced instead it would not exist."""
    hits: list[Hit] = []
    for c in calls:
        if c.status == 429:
            hits.append(Hit(call_ns=[c.n], wasted=1, reason=f"call {c.n} hit 429 despite the no-rate-limit doc"))
    return hits


def _detect_cursor_expiry(calls: list[Call]) -> list[Hit]:
    """A cursor rejected as expired. The call returned an error instead of
    the next page it was promised would always be there."""
    hits: list[Hit] = []
    for c in calls:
        if c.op == "search" and c.status == 400 and isinstance(c.response, dict) and c.response.get("error") == "cursor_expired":
            hits.append(Hit(call_ns=[c.n], wasted=1, reason=f"call {c.n} had its cursor rejected as expired"))
    return hits


def _detect_sort_mislabelled(calls: list[Call]) -> list[Hit]:
    """sort=created did not return creation order. The call still returned
    real rows -- just in the wrong order -- so it is touched, not wasted;
    the tax this belief avoids is a client-side re-sort, not a re-fetch."""
    hits: list[Hit] = []
    for c in calls:
        if c.op != "search" or c.status != 200 or not isinstance(c.response, dict):
            continue
        if (c.request or {}).get("sort") != "created":
            continue
        rows = c.response.get("results") or []
        created = [r.get("created") for r in rows if isinstance(r, dict) and r.get("created") is not None]
        if len(created) >= 2 and created != sorted(created):
            hits.append(Hit(call_ns=[c.n], wasted=0, reason=f"call {c.n} sort=created was not in creation order"))
    return hits


def _detect_field_coerced(calls: list[Call], field: str) -> list[Hit]:
    """A field sent on create/update came back changed (null, or a
    different type) with no error. The write itself still succeeded -- an
    item exists -- so the call is touched, not wasted; what's lost is that
    one field's fidelity, not a round trip."""
    hits: list[Hit] = []
    for c in calls:
        if c.op not in ("create", "update") or c.status != 200 or not isinstance(c.response, dict):
            continue
        req = c.request or {}
        if field not in req:
            continue
        sent, got = req[field], c.response.get(field)
        if sent == got:
            continue
        if got is None:
            hits.append(Hit(call_ns=[c.n], wasted=0, reason=f"call {c.n}: {field}={sent!r} sent, stored as null"))
        elif str(sent) == str(got) and type(sent) is not type(got):
            hits.append(
                Hit(call_ns=[c.n], wasted=0, reason=f"call {c.n}: {field} round-tripped as {type(got).__name__} not {type(sent).__name__}")
            )
    return hits


def _detect_silent_ignore(calls: list[Call], field: str | None) -> list[Hit]:
    """A PATCH with an unknown field returned 200 instead of 400. The write
    still succeeded for the fields that were real, so the call is touched,
    not wasted."""
    hits: list[Hit] = []
    for c in calls:
        if c.op != "update" or c.status != 200:
            continue
        req = c.request or {}
        if field and field in req:
            hits.append(Hit(call_ns=[c.n], wasted=0, reason=f"call {c.n}: unknown field {field!r} accepted with 200 instead of 400"))
    return hits


def _detectors_for_kind(kind: str, param: str | None, operation: str) -> list[Detector]:
    """Map a raw Anomaly.kind (+ parameter/operation, where the kind alone
    is ambiguous across more than one story) to the detector(s) that can
    turn it into cite-able wasted-call evidence. A kind with no entry here
    gets reported as zero, explicitly, rather than guessed at."""
    if kind == "silent_truncation" and param == "page_size":
        return [_detect_page_size_cap]
    if kind == "silent_empty" and param == "filter":
        return [_detect_unknown_filter_zero_result]
    if kind in ("silent_null", "type_coercion") and param:
        return [lambda calls: _detect_field_coerced(calls, param)]
    if kind == "idempotency_hazard":
        return [_detect_idempotent_retry]
    if kind == "write_not_visible":
        return [_detect_eventual_consistency]
    if kind == "undocumented_flag" and operation == "get":
        return [_detect_archived_invisible_get]
    if kind == "undocumented_flag" and operation == "search":
        return [_detect_archived_invisible_search]
    if kind == "undocumented_status":
        return [_detect_bad_status_burn]
    if kind == "expiry":
        return [_detect_cursor_expiry]
    if kind == "mislabelled_semantics" and param == "sort":
        return [_detect_sort_mislabelled]
    if kind == "silent_ignore":
        return [lambda calls: _detect_silent_ignore(calls, param)]
    return []


# --------------------------------------------------------------------------
# belief -> ground-truth signature(s)
# --------------------------------------------------------------------------


def _belief_signatures(belief: Belief) -> set[str]:
    """The real `operation.parameter-or-_.kind` signature(s) that actually
    minted this belief (recorded as a "signature" history event -- see
    cli.py and bench/session.py). Falls back to treating `.cls` as the kind
    for beliefs minted without that bookkeeping; that fallback is
    best-effort, not authoritative, since `.cls` is a narrative label (see
    module docstring)."""
    sigs = {h["detail"] for h in belief.history if h.get("event") == "signature" and h.get("detail")}
    if sigs:
        return sigs
    return {f"{belief.operation}.{belief.parameter or '_'}.{belief.cls}"}


def _split_signature(sig: str) -> tuple[str, str | None, str] | None:
    parts = sig.split(".")
    if len(parts) != 3:
        return None
    op, param, kind = parts
    return op, (None if param == "_" else param), kind


# --------------------------------------------------------------------------
# tokens + rescue
# --------------------------------------------------------------------------


def _tokens_per_call(hist_record: dict[str, Any]) -> float:
    """Flat per-call share of a run's tokens. This is the honest floor of
    precision history.jsonl supports: it has no per-call token breakdown,
    only a run total and a call count, so every call in a run is assumed to
    cost the same."""
    calls = hist_record.get("tool_calls")
    tokens = hist_record.get("tokens")
    if not calls or not tokens:
        return 0.0
    try:
        return float(tokens) / float(calls)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


def _run_plausibly_rescued(hist_record: dict[str, Any], belief_sigs: set[str]) -> bool:
    """A FAILED run whose recorded anomaly signatures include one that
    minted this belief. Deliberately exact-match only: no keyword or
    substring guessing against `detail`/`expected`/`got`, because those are
    free text and a fuzzy match here would be exactly the kind of
    unevidenced saving this module exists to refuse to claim."""
    if hist_record.get("success", True):
        return False
    sigs = set(hist_record.get("signatures") or [])
    return bool(sigs & belief_sigs)


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------


def replay_belief(belief: Belief, runs_dir: str = "runs") -> dict[str, Any]:
    runs, skipped_run_lines = _load_all_runs(runs_dir)
    history, skipped_history_lines = _load_history(runs_dir)

    belief_sigs = _belief_signatures(belief)
    triples = [t for t in (_split_signature(s) for s in belief_sigs) if t is not None]

    per_run_wasted: dict[str, int] = {}
    per_run_calls: dict[str, set[int]] = {}
    evidence: list[dict[str, Any]] = []
    ran_any_detector = False

    for run_id, calls in runs.items():
        touched: set[int] = set()
        wasted_here = 0
        for _op, param, kind in triples:
            for detector in _detectors_for_kind(kind, param, belief.operation):
                ran_any_detector = True
                for hit in detector(calls):
                    touched.update(hit.call_ns)
                    wasted_here += hit.wasted
                    evidence.append({"run": run_id, "call_ns": hit.call_ns, "wasted": hit.wasted, "reason": hit.reason})
        if touched:
            per_run_calls[run_id] = touched
            per_run_wasted[run_id] = wasted_here

    runs_affected = sorted(
        per_run_calls,
        key=lambda rid: min(c.t for c in runs[rid]) if runs.get(rid) else 0.0,
    )
    calls_affected = sum(len(v) for v in per_run_calls.values())
    wasted_calls = sum(per_run_wasted.values())

    tokens_est = 0.0
    for run_id, wasted in per_run_wasted.items():
        hist = history.get(run_id)
        if hist:
            tokens_est += wasted * _tokens_per_call(hist)

    rescued = sorted(rid for rid, rec in history.items() if _run_plausibly_rescued(rec, belief_sigs))

    notes: list[str] = []
    if not runs:
        notes.append(f"no run logs found under {runs_dir!r}")
    if skipped_run_lines:
        notes.append(f"skipped {skipped_run_lines} malformed/truncated run log line(s)")
    if not triples:
        notes.append(f"belief signature(s) {sorted(belief_sigs)} did not parse as operation.parameter.kind")
    elif not ran_any_detector:
        notes.append(
            "no replay rule implemented for signature(s) "
            f"{sorted(belief_sigs)} (belief classed {belief.cls!r}); reporting zero rather than guessing"
        )
    if not history:
        notes.append(f"no {runs_dir}/history.jsonl found; tokens_saved_estimate and runs_rescued are necessarily zero")
    elif skipped_history_lines:
        notes.append(f"skipped {skipped_history_lines} malformed history.jsonl line(s)")

    return {
        "belief_id": belief.id,
        "cls": belief.cls,
        "operation": belief.operation,
        "parameter": belief.parameter,
        "signatures": sorted(belief_sigs),
        "runs_affected": len(runs_affected),
        "calls_affected": calls_affected,
        "wasted_calls": wasted_calls,
        "tokens_saved_estimate": int(round(tokens_est)),
        "runs_rescued": len(rescued),
        "rescued_run_ids": rescued,
        "first_seen_run": runs_affected[0] if runs_affected else None,
        "last_seen_run": runs_affected[-1] if runs_affected else None,
        "method": (
            "tokens_saved_estimate = sum over affected runs of "
            "(wasted_calls_in_run * run.tokens / run.tool_calls) from history.jsonl "
            "(flat per-call share; runs with no history record contribute 0). "
            "runs_rescued = FAILED historical runs whose recorded signatures "
            "(operation.parameter-or-_.kind) exactly match this belief's own "
            "minting signature(s)."
        ),
        "notes": notes,
        "evidence": evidence,
    }


def replay_all(store: BeliefStore, runs_dir: str = "runs") -> dict[str, Any]:
    confirmed = [b for b in store.ordered() if b.status is Status.CONFIRMED]
    runs, _ = _load_all_runs(runs_dir)

    reports = []
    total_wasted = 0
    total_tokens = 0
    total_rescued_runs: set[str] = set()
    for b in confirmed:
        rep = replay_belief(b, runs_dir=runs_dir)
        b.replay = rep
        reports.append(rep)
        total_wasted += rep["wasted_calls"]
        total_tokens += rep["tokens_saved_estimate"]
        total_rescued_runs.update(rep["rescued_run_ids"])

    store.save()

    note = None
    if not confirmed:
        note = "no confirmed beliefs yet -- nothing to replay"
    elif not runs:
        note = f"no run logs found under {runs_dir!r}"

    return {
        "beliefs": reports,
        "total_runs": len(runs),
        "total_wasted_calls": total_wasted,
        "total_tokens": total_tokens,
        "total_rescued_runs": len(total_rescued_runs),
        "method": (
            "each belief's wasted_calls comes from deterministic, cite-able "
            "detectors run offline against runs/*.jsonl (see each belief's own "
            "'method' and 'evidence'); a belief with no detector for its "
            "signature reports zero and says so in 'notes' rather than "
            "estimating."
        ),
        "note": note,
    }


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------


def _fmt_tokens_k(n: int) -> str:
    if n <= 0:
        return "0"
    if n < 1000:
        return str(n)
    return f"{n / 1000:.0f}k"


def render(report: dict[str, Any]) -> str:
    lines = ["", "  counterfactual replay -- what would these beliefs have saved?", ""]

    beliefs = report.get("beliefs") or []
    if not beliefs:
        lines.append(f"    {report.get('note') or 'no confirmed beliefs to replay yet'}")
        lines.append("")
        return "\n".join(lines)

    total_runs = report["total_runs"]
    lines.append(f"    {'belief':<44} {'runs':>7} {'wasted calls':>13} {'est tokens':>11}")
    for b in beliefs:
        runs_col = f"{b['runs_affected']}/{total_runs}"
        tokens_col = f"{b['tokens_saved_estimate']:,}"
        lines.append(f"    {b['belief_id']:<44} {runs_col:>7} {b['wasted_calls']:>13} {tokens_col:>11}")
        if b["runs_rescued"]:
            ids = ", ".join(b["rescued_run_ids"][:6])
            more = "" if len(b["rescued_run_ids"]) <= 6 else f" (+{len(b['rescued_run_ids']) - 6} more)"
            lines.append(f"      rescued {b['runs_rescued']}/{total_runs} previously failed run(s): {ids}{more}")
        for n in b["notes"]:
            lines.append(f"      note: {n}")

    lines.append("")
    lines.append(
        f"    total: {report['total_wasted_calls']} wasted calls across {total_runs} runs, "
        f"~{_fmt_tokens_k(report['total_tokens'])} tokens"
    )
    lines.append(f"    method: {report['method']}")
    if report.get("note"):
        lines.append(f"    note: {report['note']}")
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    store = BeliefStore("lab")
    report = replay_all(store)
    print(render(report))
