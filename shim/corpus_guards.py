"""
Guards compiled from confirmed mismatches, one per mismatch class.

A belief that stays in a YAML file is a note. The point of establishing that a
tool's documentation is wrong is to stop the next caller being caught by it, so
every confirmed mismatch compiles to a runtime check that inspects a live
response and refuses it.

## Provenance is part of the guard

Each guard carries the rule it came from, the exchange keys that established
it, the contract version it was compiled under, and the hashes of the
documentation and traffic it was derived from. Without that, a guard is an
assertion of unknown origin that somebody will eventually delete because
nobody can say why it exists. With it, a guard that fires can be traced back
to the specific recorded calls that justified it.

## What a guard may not do

A guard is compiled only for a rule whose verdict is `contradicts_doc`. A
rule that abstained produces no guard -- enforcing a rule nobody established
is how an abstention quietly becomes a belief. A rule whose verdict is
`supports_doc` produces no guard either: the documentation was right, and
there is nothing to defend against.

## The three tests every guard owes

Each guard is exercised three ways, and a guard missing any of them is
reported as uncovered rather than counted:

    positive   a response that is fine must pass
    recorded   the actual recorded response that established the mismatch must fire
    planted    a synthetic violation must fire

The positive case is the one that matters most. A guard that fires on
everything catches every regression and is worthless.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from fixtures.checks import CONTRADICTS, evaluate, load_traffic
from fixtures.format import read_manifest

# A guard sees exactly what a client sees at runtime.
Response = dict  # {"request", "status", "headers", "response"}
GuardFn = Callable[[Response], str | None]   # returns a refusal reason, or None


@dataclass
class CompiledGuard:
    name: str
    mismatch_class: str
    tool: str
    fixture_version: str
    rule_id: str
    why: str
    check: GuardFn
    # provenance
    evidence: list[str] = field(default_factory=list)
    contract_version: int | None = None
    docs_sha256: str = ""
    traffic_sha256: str = ""

    def __call__(self, exchange: Response) -> str | None:
        return self.check(exchange)

    def to_dict(self) -> dict[str, Any]:
        return {
            "guard": self.name,
            "implements": self.mismatch_class,
            "tool": self.tool,
            "fixture_version": self.fixture_version,
            "rule": self.rule_id,
            "why": self.why,
            "evidence": self.evidence,
            "contract_version": self.contract_version,
            "docs_sha256": self.docs_sha256,
            "traffic_sha256": self.traffic_sha256,
        }


# ---------------------------------------------------------------------------
# guard bodies, one per mismatch class
# ---------------------------------------------------------------------------


def _status_agrees_with_body(ex: Response) -> str | None:
    """status-code-mismatch: a success status carrying a failure body."""
    body = ex.get("response")
    if ex.get("status", 0) < 400 and isinstance(body, dict):
        if body.get("success") is False or (body.get("errors") and body.get("data") is None):
            msg = ""
            if isinstance(body.get("errors"), list) and body["errors"]:
                msg = str(body["errors"][0].get("message", ""))[:80]
            return (f"HTTP {ex['status']} carries a failure body (success=false): {msg!r}. "
                    f"Branching on the status code alone would treat this as data.")
    return None


def _requested_currencies_returned(ex: Response) -> str | None:
    """field-omission: something asked for came back missing, with no error."""
    req = ex.get("request") or {}
    asked = str(req.get("symbols") or req.get("quotes") or "")
    if not asked or ex.get("status") != 200:
        return None
    want = {s.strip() for s in asked.split(",") if s.strip()}
    body = ex.get("response")
    got: set[str] = set()
    if isinstance(body, dict) and isinstance(body.get("rates"), dict):
        got = set(body["rates"])
    elif isinstance(body, list):
        got = {r.get("quote") for r in body if isinstance(r, dict)}
    missing = want - got
    if missing:
        return (f"asked for {sorted(want)} and received {sorted(got)}; {sorted(missing)} "
                f"absent from a 200 response with no error field")
    return None


def _echoed_date_matches_request(ex: Response) -> str | None:
    """semantic-type-correct: a well-formed value that answers a different question."""
    req = ex.get("request") or {}
    want = req.get("date")
    if not want:
        path = str(ex.get("path", ""))
        tail = path.rstrip("/").split("/")[-1]
        want = tail if tail.count("-") == 2 else None
    if not want or ex.get("status") != 200:
        return None
    body = ex.get("response")
    got = body.get("date") if isinstance(body, dict) else (
        body[0].get("date") if isinstance(body, list) and body else None)
    if got and got != want:
        return (f"requested date {want} and the response carries {got}: a different day's "
                f"data returned under a 200, with every field type-correct")
    return None


def _error_names_the_value_sent(ex: Response) -> str | None:
    """semantic-type-correct: a diagnostic that reports a value the caller did not send."""
    if ex.get("status", 0) < 400:
        return None
    body = ex.get("response")
    reason = str(body.get("reason", "")) if isinstance(body, dict) else ""
    if not reason:
        return None
    sent = [v for v in (ex.get("request") or {}).values() if isinstance(v, (int, float))]
    if not sent:
        return None
    if not any(str(v) in reason for v in sent):
        return (f"the diagnostic {reason!r} names none of the values actually sent "
                f"({sent}); it describes a request that was not made")
    return None


def _more_exists(ex: Response, n: int) -> bool:
    """Does the response itself say more rows were available?

    Needed to tell a clamp from an honest short page. PokeAPI returning 1351
    rows for `limit=100000` is not a clamp -- there are only 1351 -- and a
    guard that could not tell the difference would fire on the most honest
    fixture in the corpus.
    """
    body = ex.get("response")
    if isinstance(body, dict):
        if body.get("has_more") or body.get("next"):
            return True
        # MediaWiki says it in two other places, and missing both is how this
        # guard came to be silent on the Wikipedia fixture for the wrong
        # reason: it was not excusing a disclosed clamp, it simply could not
        # see that 648 hits existed behind a 500-row page. Right answer,
        # wrong mechanism, which is the failure this project keeps finding.
        if body.get("continue"):
            return True
        q = body.get("query")
        if isinstance(q, dict):
            info = q.get("searchinfo")
            if isinstance(info, dict) and isinstance(info.get("totalhits"), int) \
                    and info["totalhits"] > n:
                return True
        total = body.get("numFound") or body.get("count")
        if isinstance(total, int) and total > n:
            return True
    link = str((ex.get("headers") or {}).get("link", ""))
    return 'rel="next"' in link


def _page_size_honoured_or_announced(ex: Response) -> str | None:
    """pagination-cursor: a page smaller than asked for, with nothing saying so.

    Three conditions, all required. Fewer rows than requested; the response
    itself indicating more were available, so this is a clamp rather than the
    end of the data; and no disclosure in the body or headers. MediaWiki
    performs the identical clamp and states it in a `warnings` object, which
    is why disclosure is checked rather than clamping.
    """
    req = ex.get("request") or {}
    asked = req.get("page_size") or req.get("per_page") or req.get("limit") or req.get("srlimit")
    if not isinstance(asked, int) or asked <= 0 or ex.get("status") != 200:
        return None
    body = ex.get("response")
    if isinstance(body, list):
        n = len(body)
    elif isinstance(body, dict):
        seq = body.get("results") or body.get("docs") or (
            (body.get("query") or {}).get("search") if isinstance(body.get("query"), dict) else None)
        n = len(seq) if isinstance(seq, list) else None
    else:
        n = None
    if n is None or n >= asked:
        return None
    announced = bool((ex.get("headers") or {}).get("warning")) or (
        isinstance(body, dict) and bool(body.get("warnings")))
    if announced:
        return None
    if _more_exists(ex, n):
        return (f"asked for {asked} rows and received {n} with more available, and nothing "
                f"in the body or headers says the request was reinterpreted")
    return None


def _zero_page_is_not_an_empty_result(ex: Response) -> str | None:
    """pagination-cursor: an empty page that looks like an empty result set."""
    req = ex.get("request") or {}
    asked = req.get("limit")
    body = ex.get("response")
    if asked == 0 and ex.get("status") == 200 and isinstance(body, dict):
        found = body.get("numFound") or body.get("count") or 0
        if found and not (body.get("docs") or body.get("results")):
            return (f"limit=0 returned an empty page under a 200 while {found} records match; "
                    f"a caller reading only the page sees 'no results'")
    return None


def _retry_after_present_on_429(ex: Response) -> str | None:
    """rate-limit: a rejection a client cannot pace against."""
    if ex.get("status") != 429:
        return None
    if not (ex.get("headers") or {}).get("retry-after"):
        return ("429 without a Retry-After header: the rejection carries no instruction for "
                "when to try again, and the same endpoint supplies one on other rejections")
    return None


def _known_parameter_spelling(ex: Response) -> str | None:
    """renamed-field: a parameter this contract version does not define."""
    req = ex.get("request") or {}
    if "symbols" in req:
        return ("`symbols` is the predecessor contract's spelling of this filter; this "
                "version defines `quotes`, and an unrecognised parameter is ignored rather "
                "than rejected")
    return None


GUARD_BODIES: dict[str, tuple[str, GuardFn]] = {
    "http_200_carries_hard_failure": ("assert_status_agrees_with_body", _status_agrees_with_body),
    "unknown_symbol_silently_dropped": ("assert_requested_currencies_returned",
                                        _requested_currencies_returned),
    "non_publication_date_silently_substituted": ("assert_echoed_date_matches_request",
                                                  _echoed_date_matches_request),
    "error_reports_clamped_input": ("assert_error_names_the_value_sent",
                                    _error_names_the_value_sent),
    "per_page_silently_clamped": ("assert_page_size_honoured_or_announced",
                                  _page_size_honoured_or_announced),
    "page_size_cap": ("assert_page_size_honoured_or_announced",
                      _page_size_honoured_or_announced),
    "zero_limit_returns_empty_page_with_200": ("assert_zero_page_is_not_an_empty_result",
                                               _zero_page_is_not_an_empty_result),
    "rate_limit_flaky_header": ("assert_retry_after_present_on_429",
                                _retry_after_present_on_429),
    "filter_parameter_renamed": ("assert_known_parameter_spelling", _known_parameter_spelling),
    "deprecation_notice_points_at_itself": ("assert_status_agrees_with_body",
                                            _status_agrees_with_body),
    "pre_epoch_date_null": ("assert_stored_value_matches_sent", None),  # see below
}


def _stored_value_matches_sent(ex: Response) -> str | None:
    """field-omission: a field accepted on write and absent on read-back."""
    req, body = ex.get("request") or {}, ex.get("response")
    if ex.get("status") != 200 or not isinstance(body, dict):
        return None
    for k, v in req.items():
        if k in ("items",) or v is None:
            continue
        if k in body and body[k] is None:
            return (f"sent {k}={v!r} and the stored value came back null under a 200: "
                    f"the write was accepted and the value was not")
    return None


GUARD_BODIES["pre_epoch_date_null"] = ("assert_stored_value_matches_sent",
                                       _stored_value_matches_sent)


# ---------------------------------------------------------------------------
# compilation
# ---------------------------------------------------------------------------


def compile_from_fixture(tool: str, version: str, root: str = "fixtures",
                         contract_version: int | None = None) -> list[CompiledGuard]:
    """Compile a guard for every rule this recording actually contradicts.

    An abstention compiles nothing. A rule the tool honours compiles nothing.
    Only an established mismatch becomes enforcement.
    """
    import yaml

    from fixtures.format import fixture_dir

    rows = load_traffic(tool, version, root)
    manifest = read_manifest(tool, version, root)
    exp = yaml.safe_load((fixture_dir(tool, version, root) / "expected.yaml").read_text()) or {}

    out: list[CompiledGuard] = []
    for rule in exp.get("rules") or []:
        rid = rule["id"]
        verdict = evaluate(rid, rows)
        if verdict.outcome != CONTRADICTS:
            continue
        spec = GUARD_BODIES.get(rid)
        if spec is None:
            continue
        name, fn = spec
        out.append(CompiledGuard(
            name=name,
            mismatch_class=str(rule.get("mismatch_class", "")),
            tool=tool,
            fixture_version=version,
            rule_id=rid,
            why=verdict.detail,
            check=fn,
            evidence=list(verdict.evidence),
            contract_version=contract_version,
            docs_sha256=manifest["docs_sha256"],
            traffic_sha256=manifest["traffic_sha256"],
        ))
    return out


def uncompiled_mismatches(tool: str, version: str, root: str = "fixtures") -> list[str]:
    """Confirmed mismatches with no guard behind them.

    Reported rather than tolerated: "the agent learned it" and "the shim
    enforces it" have to be the same set, or the module's own claim is false.
    """
    import yaml

    from fixtures.format import fixture_dir

    rows = load_traffic(tool, version, root)
    exp = yaml.safe_load((fixture_dir(tool, version, root) / "expected.yaml").read_text()) or {}
    out = []
    for rule in exp.get("rules") or []:
        if evaluate(rule["id"], rows).outcome != CONTRADICTS:
            continue
        if rule["id"] not in GUARD_BODIES:
            out.append(rule["id"])
    return out
