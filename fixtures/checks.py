"""
Deterministic checks that turn a recording into a verdict about one claim.

`expected.yaml` is an answer key: it states what the documentation promised
and what the tool actually does. It is prose, and prose cannot be executed.
This module is the executable half -- one function per rule, each reading only
the recorded exchanges and returning one of three verdicts:

    CONTRADICTS  the recording shows the documented claim is false here
    SUPPORTS     the recording shows the documented claim holding
    UNOBSERVABLE the recording cannot distinguish the cases; abstain

The third verdict is the one that matters most and the one a checker is most
tempted to omit. Every vacuous-measurement bug this project has found came
from a check that had only two answers available to it and therefore had to
pick one. A sweep over an empty vendor is not a refutation. A uniform failure
across every input is not evidence about field handling. `UNOBSERVABLE` is
what those must return.

Each check reports the keys of the exchanges it used, so a verdict can be
traced to the specific recorded calls that produced it rather than taken on
trust. That evidence set is also what Phase 2's drift attribution narrows: the
smallest set of exchanges whose change would flip the verdict.

Checks are registered by rule id, not embedded in `expected.yaml`, so the
answer key stays declarative and a check can be corrected without rewriting
(and re-hashing) a fixture.
"""

from __future__ import annotations

import datetime
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

CONTRADICTS = "contradicts_doc"
SUPPORTS = "supports_doc"
UNOBSERVABLE = "unobservable"


@dataclass
class Verdict:
    outcome: str
    detail: str
    evidence: list[str] = field(default_factory=list)   # exchange keys used

    def to_dict(self) -> dict[str, Any]:
        return {"outcome": self.outcome, "detail": self.detail, "evidence": self.evidence}


Check = Callable[[list[dict[str, Any]]], Verdict]
CHECKS: dict[str, Check] = {}


def check(rule_id: str) -> Callable[[Check], Check]:
    def deco(fn: Check) -> Check:
        CHECKS[rule_id] = fn
        return fn
    return deco


def load_traffic(tool: str, version: str, root: str | Path = "fixtures") -> list[dict[str, Any]]:
    p = Path(root) / tool / version / "traffic.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def _by(rows: list[dict[str, Any]], **where: Any) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        if all(r["request"].get(k) == v for k, v in where.items()):
            out.append(r)
    return out


def _n_results(body: Any, path: tuple[str, ...]) -> int | None:
    cur = body
    for p in path:
        if not isinstance(cur, dict) or p not in cur:
            return None
        cur = cur[p]
    return len(cur) if isinstance(cur, list) else None


# ---------------------------------------------------------------------------
# open-meteo
# ---------------------------------------------------------------------------


@check("error_reports_clamped_input")
def _om_error_echo(rows):
    """Does the out-of-range diagnostic report the value the caller sent?"""
    bad = [r for r in rows if r["status"] != 200]
    if len(bad) < 2:
        return Verdict(UNOBSERVABLE,
                       "fewer than two rejected requests; one diagnostic cannot be "
                       "compared against another", [r["key"] for r in bad])
    reasons = {r["key"]: str((r["response"] or {}).get("reason", "")) for r in bad}
    sent = {r["key"]: r["request"].get("forecast_days") for r in bad}
    # The claim is that the diagnostic describes THIS request. It fails if two
    # different inputs produce the same diagnostic, and that diagnostic names a
    # value neither of them sent.
    if len(set(reasons.values())) == 1:
        text = next(iter(reasons.values()))
        named = [v for v in sent.values() if str(v) in text]
        if not named:
            return Verdict(CONTRADICTS,
                           f"inputs {sorted(sent.values())} all produced {text!r}, which "
                           f"names none of them", list(bad and reasons))
    return Verdict(SUPPORTS, "each rejected request produced a diagnostic naming its own value",
                   list(reasons))


@check("forecast_days_cap_is_enforced")
def _om_cap(rows):
    ok = [r for r in rows if r["status"] == 200]
    at_cap = [r for r in ok if r["request"].get("forecast_days") == 16]
    over = [r for r in rows if (r["request"].get("forecast_days") or 0) > 16]
    if not at_cap or not over:
        return Verdict(UNOBSERVABLE, "the capture does not span the documented ceiling",
                       [r["key"] for r in at_cap + over])
    days = _n_results(at_cap[0]["response"], ("daily", "time"))
    rejected = all(r["status"] != 200 for r in over)
    if days == 16 and rejected:
        return Verdict(SUPPORTS,
                       f"forecast_days=16 returned {days} days and over-range values were "
                       f"rejected rather than clamped",
                       [at_cap[0]["key"]] + [r["key"] for r in over])
    return Verdict(CONTRADICTS, f"forecast_days=16 returned {days}; over-range rejected={rejected}",
                   [at_cap[0]["key"]] + [r["key"] for r in over])


# ---------------------------------------------------------------------------
# openlibrary
# ---------------------------------------------------------------------------


@check("zero_limit_returns_empty_page_with_200")
def _ol_zero(rows):
    zero = _by(rows, limit=0)
    neg = _by(rows, limit=-5)
    if not zero or not neg:
        return Verdict(UNOBSERVABLE, "the capture does not contain both limit=0 and a negative limit",
                       [r["key"] for r in zero + neg])
    z, n = zero[0], neg[0]
    docs = _n_results(z["response"], ("docs",))
    found = (z["response"] or {}).get("numFound")
    if z["status"] == 200 and docs == 0 and (found or 0) > 0 and n["status"] != 200:
        return Verdict(CONTRADICTS,
                       f"limit=0 -> HTTP 200 with 0 docs but numFound={found}; limit=-5 -> "
                       f"HTTP {n['status']}. The parameter is validated, but not at the one "
                       f"value that makes an empty page look like an empty result set",
                       [z["key"], n["key"]])
    return Verdict(SUPPORTS, f"limit=0 -> HTTP {z['status']} with {docs} docs", [z["key"], n["key"]])


@check("limit_is_not_capped_at_100")
def _ol_cap(rows):
    big = _by(rows, limit=1000)
    if not big:
        return Verdict(UNOBSERVABLE, "no large-limit request in the capture", [])
    docs = _n_results(big[0]["response"], ("docs",))
    if docs == 1000:
        return Verdict(SUPPORTS, f"limit=1000 returned {docs} docs: no hidden cap", [big[0]["key"]])
    return Verdict(CONTRADICTS, f"limit=1000 returned {docs} docs", [big[0]["key"]])


# ---------------------------------------------------------------------------
# frankfurter
# ---------------------------------------------------------------------------


def _rate_keys(body: Any) -> set[str]:
    if isinstance(body, dict) and isinstance(body.get("rates"), dict):
        return set(body["rates"])
    if isinstance(body, list):
        return {r.get("quote") for r in body if isinstance(r, dict)}
    return set()


@check("unknown_symbol_silently_dropped")
def _fr_symbol(rows):
    asked = [r for r in rows if "ZZZ" in str(r["request"].get("symbols", ""))]
    base_bad = [r for r in rows if r["request"].get("base") == "ZZZ"]
    if not asked:
        return Verdict(UNOBSERVABLE, "no request asked for an unknown symbol", [])
    a = asked[0]
    got = _rate_keys(a["response"])
    if a["status"] == 200 and "ZZZ" not in got:
        detail = (f"symbols=EUR,ZZZ -> HTTP 200 returning {sorted(got)}: the unknown code is "
                  f"absent with no error")
        if base_bad:
            detail += f"; the same API rejects base=ZZZ with HTTP {base_bad[0]['status']}"
        return Verdict(CONTRADICTS, detail, [a["key"]] + [r["key"] for r in base_bad])
    if a["status"] != 200:
        return Verdict(SUPPORTS, f"an unknown symbol was rejected with HTTP {a['status']}", [a["key"]])
    return Verdict(UNOBSERVABLE, "inconclusive", [a["key"]])


def _requested_date(r: dict[str, Any]) -> str | None:
    want = r["request"].get("date") or r["path"].rstrip("/").split("/")[-1]
    return want if str(want).count("-") == 2 else None


def _could_have_been_substituted(want: str) -> bool:
    """Is this a date a rates API plausibly has no publication for?

    Weekends, computed from the date itself. This uses calendar arithmetic on
    the request, never the answer key -- the point is to know whether the
    question asked was capable of producing a negative answer, which is a
    property of the question.
    """
    try:
        return datetime.date.fromisoformat(want).weekday() >= 5
    except ValueError:
        return False


@check("non_publication_date_silently_substituted")
def _fr_date(rows):
    """Does a dated request come back carrying the date that was asked for?

    The subtle half of this check is when it may answer SUPPORTS. Observing a
    Friday come back as that Friday establishes nothing: a publication day
    could not have been substituted, so the evidence was incapable of showing
    the defect. Concluding "the docs are honest" from it is the vacuous pass
    this project has now found six times -- asserting a cap against a store
    too small to reach it, asserting a sweep against an empty vendor, and now
    asserting date fidelity against a date that was never at risk.

    So: a mismatch refutes; a match refutes nothing unless the date probed was
    one that could have been substituted; otherwise abstain and say why.
    """
    dated = [r for r in rows if _requested_date(r)]
    if not dated:
        return Verdict(UNOBSERVABLE, "no dated request in the evidence", [])

    for r in dated:
        want = _requested_date(r)
        body = r["response"]
        got = body.get("date") if isinstance(body, dict) else (
            body[0].get("date") if isinstance(body, list) and body else None)
        if got and want and got != want:
            return Verdict(CONTRADICTS,
                           f"requested {want}, response carries date {got}, HTTP {r['status']}: "
                           f"a different day's rates returned as if they were the requested day",
                           [r["key"]])

    at_risk = [r for r in dated if _could_have_been_substituted(_requested_date(r))]
    if not at_risk:
        return Verdict(UNOBSERVABLE,
                       f"every dated request observed ({[_requested_date(r) for r in dated]}) "
                       f"was for a publication day, which could not have been substituted. "
                       f"The evidence matches the documentation but was never capable of "
                       f"contradicting it",
                       [r["key"] for r in dated])
    return Verdict(SUPPORTS,
                   f"a date that could have been substituted "
                   f"({_requested_date(at_risk[0])}, a weekend) came back as requested",
                   [r["key"] for r in at_risk])


@check("filter_parameter_renamed")
def _fr_rename(rows):
    uses_quotes = any("quotes" in r["request"] for r in rows)
    uses_symbols = any("symbols" in r["request"] for r in rows)
    if uses_quotes and not uses_symbols:
        return Verdict(CONTRADICTS,
                       "this version's filter parameter is `quotes`; the predecessor's was "
                       "`symbols`. A client carrying the old spelling sends an unrecognised "
                       "parameter rather than failing loudly",
                       [r["key"] for r in rows if "quotes" in r["request"]][:2])
    return Verdict(UNOBSERVABLE, "the capture does not span both spellings", [])


@check("unknown_quote_rejected")
def _fr_quote_reject(rows):
    asked = [r for r in rows if "ZZZ" in str(r["request"].get("quotes", ""))]
    if not asked:
        return Verdict(UNOBSERVABLE, "no request asked for an unknown quote", [])
    a = asked[0]
    if a["status"] >= 400:
        return Verdict(SUPPORTS,
                       f"an unknown quote was rejected with HTTP {a['status']}: "
                       f"{(a['response'] or {}).get('message')!r}", [a["key"]])
    return Verdict(CONTRADICTS, f"an unknown quote returned HTTP {a['status']}", [a["key"]])


@check("requested_date_returned")
def _fr_date_ok(rows):
    v = _fr_date(rows)
    if v.outcome == CONTRADICTS:
        return Verdict(CONTRADICTS, v.detail, v.evidence)
    if v.outcome == SUPPORTS:
        return Verdict(SUPPORTS, v.detail, v.evidence)
    return v


# ---------------------------------------------------------------------------
# github
# ---------------------------------------------------------------------------


@check("per_page_silently_clamped")
def _gh_clamp(rows):
    over = [r for r in rows if (r["request"].get("per_page") or 0) > 100]
    if not over:
        return Verdict(UNOBSERVABLE, "no over-maximum page request in the capture", [])
    r = over[0]
    n = len(r["response"]) if isinstance(r["response"], list) else None
    warned = bool(r["headers"].get("warning")) or (
        isinstance(r["response"], dict) and "warnings" in r["response"])
    if r["status"] == 200 and n == 100 and not warned:
        return Verdict(CONTRADICTS,
                       f"per_page={r['request']['per_page']} returned {n} items with HTTP 200 and "
                       f"no warning in body or headers: the request was silently reinterpreted",
                       [r["key"]])
    if warned:
        return Verdict(SUPPORTS, "the clamp was announced", [r["key"]])
    return Verdict(UNOBSERVABLE, f"per_page over-max returned HTTP {r['status']} with {n} items",
                   [r["key"]])


@check("rate_limit_headers_are_honest")
def _gh_headers(rows):
    with_hdr = [r for r in rows if r["headers"].get("x-ratelimit-limit")]
    if not with_hdr:
        return Verdict(UNOBSERVABLE, "no response carried rate-limit headers", [])
    r = with_hdr[0]
    lim = r["headers"].get("x-ratelimit-limit")
    rem = r["headers"].get("x-ratelimit-remaining")
    used = r["headers"].get("x-ratelimit-used")
    consistent = None
    try:
        consistent = int(lim) - int(used) == int(rem)
    except (TypeError, ValueError):
        consistent = None
    if lim == "60" and consistent:
        return Verdict(SUPPORTS,
                       f"x-ratelimit-limit={lim} matches the documented unauthenticated limit, "
                       f"and remaining({rem}) = limit - used({used})", [r["key"]])
    return Verdict(CONTRADICTS,
                   f"x-ratelimit-limit={lim}, remaining={rem}, used={used}; "
                   f"internally consistent={consistent}", [r["key"]])


@check("rate_limit_window_actually_resets")
def _gh_reset(rows):
    """The documented reset cannot be witnessed by a recording. Abstain."""
    with_hdr = [r for r in rows if r["headers"].get("x-ratelimit-reset")]
    return Verdict(UNOBSERVABLE,
                   "x-ratelimit-reset names a time roughly an hour after capture. A recording "
                   "made in one second contains no observation of that moment, so the promise "
                   "can be neither confirmed nor refuted from this evidence",
                   [r["key"] for r in with_hdr][:1])


# ---------------------------------------------------------------------------
# wikipedia -- the honest control for the clamp class
# ---------------------------------------------------------------------------


@check("clamp_is_announced_in_band")
def _wiki_clamp(rows):
    over = [r for r in rows if (r["request"].get("srlimit") or 0) > 500]
    if not over:
        return Verdict(UNOBSERVABLE, "no over-maximum request in the capture", [])
    r = over[0]
    n = _n_results(r["response"], ("query", "search"))
    warn = ((r["response"] or {}).get("warnings") or {}).get("search") or {}
    text = " ".join(str(v) for v in warn.values())
    sent = str(r["request"].get("srlimit"))
    if n == 500 and text and sent in text:
        return Verdict(SUPPORTS,
                       f"srlimit={sent} returned {n} results AND a warning naming the parameter, "
                       f"the offending value {sent}, and the permitted range: the clamp is "
                       f"identical to the lab's and GitHub's, the disclosure is not",
                       [r["key"]])
    if n == 500 and not text:
        return Verdict(CONTRADICTS, f"srlimit={sent} clamped to {n} with no warning", [r["key"]])
    return Verdict(UNOBSERVABLE, f"srlimit={sent} returned {n} results, warning={text!r}", [r["key"]])


# ---------------------------------------------------------------------------
# pokeapi -- the honest control for pagination
# ---------------------------------------------------------------------------


@check("pagination_reports_its_own_end")
def _poke(rows):
    big = [r for r in rows if (r["request"].get("limit") or 0) > 10000]
    if not big:
        return Verdict(UNOBSERVABLE, "no over-collection request in the capture", [])
    r = big[0]
    b = r["response"] or {}
    n, count, nxt = _n_results(b, ("results",)), b.get("count"), b.get("next")
    if n == count and nxt is None:
        return Verdict(SUPPORTS,
                       f"asking for more than exists returned every row (results={n}), count "
                       f"agrees ({count}), and next is null because there is genuinely no next page",
                       [r["key"]])
    return Verdict(CONTRADICTS, f"results={n}, count={count}, next={nxt!r}", [r["key"]])


# ---------------------------------------------------------------------------
# restcountries -- status-code mismatch, and an unobservable surface
# ---------------------------------------------------------------------------


@check("http_200_carries_hard_failure")
def _rc_status(rows):
    if not rows:
        return Verdict(UNOBSERVABLE, "no exchanges observed", [])
    lying = [r for r in rows
             if r["status"] == 200 and isinstance(r["response"], dict)
             and r["response"].get("success") is False]
    if not lying:
        return Verdict(SUPPORTS, "no response carried a failure body under a success status",
                       [r["key"] for r in rows][:1])
    r = lying[0]
    msg = (r["response"].get("errors") or [{}])[0].get("message", "")
    return Verdict(CONTRADICTS,
                   f"HTTP 200 with success=false, data=null and error {msg[:80]!r}. "
                   f"{len(lying)}/{len(rows)} exchanges do this: a client branching on the "
                   f"status code treats a retired API as a working one",
                   [r["key"] for r in lying])


@check("deprecation_notice_points_at_itself")
def _rc_circular(rows):
    def msg(r):
        return (((r["response"] or {}).get("errors") or [{}])[0] or {}).get("message", "")
    recommending = [r for r in rows if "v5" in msg(r)]
    on_v5 = [r for r in rows if "/v5" in r["path"]]
    if not recommending or not on_v5:
        return Verdict(UNOBSERVABLE,
                       "the capture does not contain both the recommendation and a request to "
                       "the recommended version", [])
    if "v5" in msg(on_v5[0]):
        return Verdict(CONTRADICTS,
                       f"the error tells the caller to migrate to v5, and {on_v5[0]['path']} "
                       f"returns the identical instruction: the remediation is well-formed, "
                       f"type-correct and circular",
                       [recommending[0]["key"], on_v5[0]["key"]])
    return Verdict(SUPPORTS, "the recommended version behaves differently", [on_v5[0]["key"]])


@check("field_selection_semantics")
def _rc_fields(rows):
    """Every input produces the same bytes, so nothing about fields is observable."""
    with_fields = [r for r in rows if "fields" in r["request"]]
    if len(with_fields) < 2:
        return Verdict(UNOBSERVABLE, "fewer than two field-selection requests", [])
    hashes = {r["body_sha256"] for r in with_fields}
    if len(hashes) == 1:
        return Verdict(UNOBSERVABLE,
                       f"all {len(with_fields)} field-selection requests -- a valid list, a "
                       f"partly invalid one, and a wholly invalid one -- returned byte-identical "
                       f"bodies. Nothing distinguishes the inputs, so nothing can be concluded "
                       f"about field handling",
                       [r["key"] for r in with_fields])
    return Verdict(SUPPORTS, "different field lists produced different responses",
                   [r["key"] for r in with_fields])


# ---------------------------------------------------------------------------
# lab
# ---------------------------------------------------------------------------


@check("page_size_cap")
def _lab_cap(rows):
    over = [r for r in rows if r["op"] == "search" and (r["request"].get("page_size") or 0) > 50]
    if not over:
        return Verdict(UNOBSERVABLE, "no over-cap search in the capture", [])
    r = over[0]
    n = _n_results(r["response"], ("results",))
    warned = bool(r["headers"].get("warning")) or (
        isinstance(r["response"], dict) and "warnings" in r["response"])
    if n == 50 and not warned:
        return Verdict(CONTRADICTS,
                       f"page_size={r['request']['page_size']} returned {n} rows with "
                       f"has_more={(r['response'] or {}).get('has_more')} and no warning: "
                       f"documented as accepting up to 500",
                       [r["key"]])
    return Verdict(SUPPORTS, f"page_size={r['request']['page_size']} returned {n}", [r["key"]])


@check("pre_epoch_date_null")
def _lab_date(rows):
    creates = [r for r in rows if r["op"] == "create" and r["request"].get("due_date")]
    if not creates:
        return Verdict(UNOBSERVABLE, "no dated create in the evidence", [])
    # The claim is specifically about dates before the epoch. A 1990 date
    # stored correctly is not evidence either way, and treating it as support
    # would be the same vacuous pass as asserting a cap against a small store.
    pre_epoch = [r for r in creates if str(r["request"]["due_date"]) < "1970-01-01"]
    if not pre_epoch:
        return Verdict(UNOBSERVABLE,
                       f"no create used a pre-epoch due_date "
                       f"({[r['request']['due_date'] for r in creates]}); the claim is about "
                       f"dates before 1970 and this evidence could not have tested it",
                       [r["key"] for r in creates])
    r = pre_epoch[0]
    sent, stored = r["request"]["due_date"], (r["response"] or {}).get("due_date")
    if r["status"] == 200 and sent and stored is None:
        return Verdict(CONTRADICTS,
                       f"created with due_date={sent!r}, HTTP 200, stored value is null: "
                       f"documented as echoed as sent", [r["key"]])
    return Verdict(SUPPORTS, f"due_date {sent!r} stored as {stored!r}", [r["key"]])


@check("rate_limit_flaky_header")
def _lab_rate(rows):
    limited = [r for r in rows if r["status"] == 429]
    if not limited:
        return Verdict(UNOBSERVABLE,
                       "no request was rate limited in this capture, so an undocumented limit "
                       "was not observed either way", [])
    with_ra = [r for r in limited if r["headers"].get("retry-after")]
    return Verdict(CONTRADICTS,
                   f"{len(limited)}/{len(rows)} calls returned 429 against documentation that "
                   f"promises no rate limit, and Retry-After was present on only "
                   f"{len(with_ra)}/{len(limited)} of them: the rejection behaviour is "
                   f"inconsistent, so one observation cannot characterise it",
                   [r["key"] for r in limited])


def evaluate(rule_id: str, rows: list[dict[str, Any]]) -> Verdict:
    fn = CHECKS.get(rule_id)
    if fn is None:
        return Verdict(UNOBSERVABLE, f"no check registered for rule {rule_id!r}", [])
    return fn(rows)
