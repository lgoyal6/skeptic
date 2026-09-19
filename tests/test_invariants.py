"""
Regression tests for skeptic's measurement machinery.

skeptic's whole claim is a measured one -- precision and recall of discovered
beliefs against a known answer key -- so the measurement code has to be more
trustworthy than the thing it measures. During the build, the SAME bug class
appeared four separate times: measuring without first establishing that the
thing being measured could have been observed.

  1. A lab smoke test asserted `page_size <= 50` against a 25-row store, so
     the cap was never exercised and the assertion passed vacuously.
  2. A detector test asserted on anomaly *kind*, so `cursor_expiry` "passed"
     because that kind appeared elsewhere in the run while nothing fired for
     the cursor at all.
  3. Recon's page-size check ran against a 20-row vendor -- same vacuous pass.
  4. Worst: a probe swept a vendor with ZERO rows, got 0 results for every
     page size, and the verdict read that as *refuting* a belief that was
     true. A vacuous experiment destroyed a correct belief.

Every test below either guards directly against one of those four, or locks
down another hard-won invariant (class constraint, posterior lifecycle,
scoring honesty, guard derivation) discovered along the way. These are pure
unit tests: no network, no live lab, no model calls, and nothing is ever
written outside a pytest `tmp_path`.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

import bench.score as score_mod
from agent.beliefs import (
    DOC_DOUBT_THRESHOLD,
    DOC_TRUST_THRESHOLD,
    Belief,
    BeliefStore,
    Posterior,
    Status,
)
from agent.contract import Anomaly, Call, ContractLayer
from bench.score import match
from reflect.hypothesis import CLASSES, KIND_ALLOWS, constrain_class
from reflect.probe import ProbeRecord, _apply, _uninformative, indistinguishable
from reflect.templates import Observation
from shim.guards import ClampPageSize, compile_guards


# ---------------------------------------------------------------------------
# The vacuous-measurement family -- the most important group.
# ---------------------------------------------------------------------------


def test_uninformative_all_zero_sweep():
    """Bug #4: a sweep over an empty vendor returned 0 for every page size and refuted a true belief; every-value-zero must be flagged uninformative."""
    obs = Observation(
        template="boundary",
        params={"op": "search", "param": "page_size", "values": [10, 50, 100]},
        calls=3,
        facts={
            "sweep": [
                {"page_size": 10, "returned": 0},
                {"page_size": 50, "returned": 0},
                {"page_size": 100, "returned": 0},
            ]
        },
    )
    assert _uninformative(obs) is True


def test_uninformative_zero_population():
    """A vendor with zero rows can never produce a signal; `population == 0` must short-circuit before any verdict is drawn."""
    obs = Observation(template="boundary", params={}, calls=1, facts={"population": 0})
    assert _uninformative(obs) is True


def test_uninformative_when_facts_carry_error():
    """A probe that errored out has no signal at all and must not be judged as though it observed something."""
    obs = Observation(
        template="boundary", params={}, calls=1,
        facts={"error": "ConnectionError: refused"},
    )
    assert _uninformative(obs) is True


def test_uninformative_false_for_real_variation():
    """Sanity check: a sweep that actually varies must NOT be flagged uninformative, or every real experiment gets thrown away along with the vacuous ones."""
    obs = Observation(
        template="boundary",
        params={"op": "search", "param": "page_size", "values": [10, 50, 100]},
        calls=3,
        facts={
            "sweep": [
                {"page_size": 10, "returned": 10},
                {"page_size": 50, "returned": 50},
                {"page_size": 100, "returned": 50},
            ]
        },
    )
    assert _uninformative(obs) is False


def _confirmed_belief(tmp_path, name: str = "lab.search.cap_belief") -> tuple[BeliefStore, Belief]:
    store = BeliefStore("lab", root=tmp_path / "beliefs")
    b = Belief(
        id=name, tool="lab", operation="search", cls="silent_truncation",
        parameter="page_size", doc_claims="page_size accepts up to 500",
        belief="page_size is silently clamped to 50",
    )
    store.add(b)
    b.confirm("probe0")
    return store, b


def test_run_probe_style_guard_prevents_falsification_from_empty_sweep(tmp_path):
    """Bug #4, guarded: run_probe downgrades every verdict to 'inconclusive' when _uninformative(obs) is True, BEFORE the record ever reaches _apply. This mirrors that exact neutralization (no LLM/lab needed) and confirms a belief survives intact when the guard is honored."""
    store, b = _confirmed_belief(tmp_path)

    obs = Observation(template="boundary", params={}, calls=2, facts={"population": 0})
    assert _uninformative(obs) is True

    # What reflect.probe.run_probe actually does with an uninformative
    # observation: every verdict is replaced with "inconclusive" regardless
    # of what the model said, and only THEN is the record applied.
    neutralised_verdicts = [
        {"hypothesis_id": b.id, "verdict": "inconclusive", "because": "the experiment observed no signal"}
    ]
    rec = ProbeRecord(
        id="probe1", belief_ids=[b.id], template="boundary", params={}, why="",
        predictions=[], observation=obs.to_dict(), verdicts=neutralised_verdicts,
        learned="", calls=2, wall_s=0.01,
    )
    _apply(store, rec)
    assert b.status is Status.CONFIRMED


def test_apply_itself_guards_against_uninformative_observation(tmp_path):
    """The vacuous-measurement guard lives in `_apply`, the point of effect, so a probe record replayed from disk -- never passed through `run_probe` -- still cannot falsify a belief from zero signal."""
    store, b = _confirmed_belief(tmp_path, name="lab.search.cap_belief2")

    obs = Observation(
        template="boundary", params={}, calls=2,
        facts={"sweep": [{"page_size": 10, "returned": 0}, {"page_size": 50, "returned": 0}]},
    )
    assert _uninformative(obs) is True  # the sweep really did see nothing

    # The record carries a "falsified" verdict and is handed straight to
    # `_apply`, exactly as `bench/apply_probes.py` and `bench/settle.py` do
    # with `probes/*.json` -- never passing through `run_probe`, which is
    # where this check used to live and where every other caller bypassed it.
    rec = ProbeRecord(
        id="probe2", belief_ids=[b.id], template="boundary", params={}, why="",
        predictions=[], observation=obs.to_dict(),
        verdicts=[{"hypothesis_id": b.id, "verdict": "falsified", "because": "0 rows at every size"}],
        learned="", calls=2, wall_s=0.01,
    )
    _apply(store, rec)

    assert b.status is Status.CONFIRMED
    assert any(h.get("event") == "probe_uninformative" for h in b.history), \
        "the belief must record WHY the verdict was dropped, not silently ignore it"


def test_indistinguishable_flags_identical_predictions():
    """An experiment whose predictions read the same across rivals cannot actually separate them, and letting it 'confirm' one would be a coin flip dressed up as evidence."""
    preds = [
        {"hypothesis_id": "h1", "predicts": "Capped at 50."},
        {"hypothesis_id": "h2", "predicts": "capped at 50"},
        {"hypothesis_id": "h3", "predicts": "Different outcome entirely."},
    ]
    assert indistinguishable(preds) == ["h1"]


# ---------------------------------------------------------------------------
# Class constraint.
# ---------------------------------------------------------------------------


def test_constrain_class_rejects_evidence_unsupported_class():
    """The exact false belief that cost precision: an update.silent_ignore anomaly labelled idempotency_hazard by the model must be pulled back to what the wire evidence actually supports."""
    assert constrain_class("silent_ignore", "idempotency_hazard") == "silent_coercion"


def test_constrain_class_silent_empty_ambiguity_survives():
    """silent_empty is deliberately two-valued (bad filter field vs. hidden archived rows); the designed ambiguity must not be collapsed to one class."""
    assert constrain_class("silent_empty", "undocumented_flag") == "undocumented_flag"
    assert constrain_class("silent_empty", "silent_coercion") == "silent_coercion"


def test_kind_allows_values_are_all_known_classes():
    """A class typo in KIND_ALLOWS would silently mint beliefs the scorer can never match against ground truth."""
    allowed_classes = {c for tup in KIND_ALLOWS.values() for c in tup}
    assert allowed_classes <= set(CLASSES)


def test_every_contract_anomaly_kind_has_a_class_constraint():
    """Every anomaly kind contract.py can actually produce must have a KIND_ALLOWS entry, or the reflector is free to assign an unconstrained kind any class it likes."""
    src = Path(__file__).resolve().parents[1] / "agent" / "contract.py"
    text = src.read_text()
    kinds = set(re.findall(r'kind="([a-z_]+)"', text))
    assert kinds, "no anomaly kinds found via regex in contract.py -- the pattern may be stale"
    missing = kinds - set(KIND_ALLOWS.keys())
    assert not missing, f"anomaly kind(s) with no KIND_ALLOWS entry: {sorted(missing)}"


# ---------------------------------------------------------------------------
# Posterior and lifecycle.
# ---------------------------------------------------------------------------


def test_fresh_posterior_starts_pro_doc():
    """Trusting the docs by default is the exact failure mode this project measures; if the prior ever flips anti-doc, every doubt/trust threshold means something different."""
    assert Posterior().p_doc_correct > 0.5


def test_repeated_contradiction_drives_posterior_below_doubt_threshold():
    """The doc-doubt mechanism must actually fire on repeated contradicting evidence, or a wrong doc claim is trusted forever."""
    p = Posterior()
    for _ in range(20):
        p.contradicts_doc(1.0)
        if p.p_doc_correct < DOC_DOUBT_THRESHOLD:
            break
    assert p.p_doc_correct < DOC_DOUBT_THRESHOLD


def test_confirmed_belief_auto_falsifies_once_docs_prove_out():
    """The unlearning mechanism: a CONFIRMED belief must revert to FALSIFIED once repeated evidence shows the documentation was right after all."""
    b = Belief(
        id="lab.search.x", tool="lab", operation="search", cls="silent_truncation",
        parameter="page_size", doc_claims="d", belief="b",
    )
    b.confirm("probe0")
    assert b.status is Status.CONFIRMED

    for i in range(20):
        b.observe_support_for_doc(f"run{i}")
        if b.status is Status.FALSIFIED:
            break
    assert b.status is Status.FALSIFIED
    assert b.posterior.p_doc_correct > DOC_TRUST_THRESHOLD


def test_belief_round_trip_via_dict():
    """to_dict/from_dict must round-trip class, posterior, status, replay and history cleanly, or a reloaded belief store silently loses what it learned."""
    b = Belief(
        id="lab.search.cap", tool="lab", operation="search", cls="silent_truncation",
        parameter="page_size", doc_claims="page_size accepts up to 500",
        belief="page_size is clamped to 50", action="clamp requests and paginate",
        posterior=Posterior(alpha=5.0, beta=2.0),
        replay={"seed": "vendorA", "note": "boundary sweep"},
        competing=["lab.search.other"],
    )
    b.note("created", "seed")
    b.confirm("probe0")

    d = b.to_dict()
    assert d["class"] == "silent_truncation"
    assert "cls" not in d

    b2 = Belief.from_dict(d)
    assert b2.cls == b.cls
    assert b2.status == b.status
    assert b2.posterior.alpha == b.posterior.alpha
    assert b2.posterior.beta == b.posterior.beta
    assert b2.replay == b.replay
    assert b2.history == b.history
    assert b2.competing == b.competing
    assert b2.parameter == b.parameter
    assert b2.action == b.action


# ---------------------------------------------------------------------------
# Scoring honesty.
# ---------------------------------------------------------------------------


def test_match_returns_none_for_unmatched_class():
    """A false belief that matches no ground-truth rule must come back as None so score() counts it as false, not silently drops it."""
    rules = [{"id": "r1", "class": "silent_truncation", "operation": "search", "parameter": "page_size"}]
    b = Belief(
        id="x", tool="lab", operation="search", cls="idempotency_hazard",
        doc_claims="", belief="", parameter="page_size",
    )
    assert match(b, rules) is None


def test_match_does_not_match_on_class_alone_when_parameter_disagrees():
    """Class-only matching would let a belief about the wrong parameter count as a hit against a rule it does not actually describe."""
    rules = [{"id": "r1", "class": "silent_truncation", "operation": "search", "parameter": "page_size"}]
    b = Belief(
        id="x", tool="lab", operation="search", cls="silent_truncation",
        doc_claims="", belief="", parameter="title",
    )
    assert match(b, rules) is None


def test_score_precision_with_one_false_belief(tmp_path, monkeypatch):
    """A false belief must cost precision rather than being ignored: one matching + one matching-nothing confirmed belief must score exactly 0.5 through the real score() path, using only a tmp_path store."""
    beliefs_root = tmp_path / "beliefs"
    store = BeliefStore("lab", root=beliefs_root)

    good = Belief(
        id="lab.search.cap", tool="lab", operation="search", cls="silent_truncation",
        parameter="page_size", doc_claims="page_size accepts up to 500",
        belief="page_size is clamped to 50",
    )
    store.add(good)
    good.confirm("probeA")

    bad = Belief(
        id="lab.update.mystery", tool="lab", operation="update", cls="idempotency_hazard",
        parameter="foo", doc_claims="", belief="an invented false belief",
    )
    store.add(bad)
    bad.confirm("probeB")
    store.save()

    gt_path = tmp_path / "ground_truth.yaml"
    gt_path.write_text(
        yaml.safe_dump(
            {
                "rules": [
                    {"id": "page_size_cap", "class": "silent_truncation", "operation": "search", "parameter": "page_size"},
                ]
            }
        )
    )

    # Never let this test touch the network or the real runs/observable.json.
    monkeypatch.setattr(score_mod, "OBSERVED_PATH", tmp_path / "observable.json")

    def _no_network(*_args, **_kwargs):
        raise RuntimeError("no network calls allowed in tests")

    monkeypatch.setattr(score_mod.httpx, "get", _no_network)

    result = score_mod.score(
        tool="lab", beliefs_root=str(beliefs_root), gt_path=str(gt_path),
        lab_base="http://127.0.0.1:1",
    )

    assert result["confirmed_beliefs"] == 2
    assert result["matched"] == 1
    assert result["false_beliefs"] == 1
    assert result["precision"] == 0.5


# ---------------------------------------------------------------------------
# Guards derived from beliefs, not hand-written.
# ---------------------------------------------------------------------------


def test_compile_guards_on_empty_store_returns_no_guards(tmp_path):
    """The shim must never enforce more than what was actually learned; an empty store must compile to zero guards."""
    store = BeliefStore("lab", root=tmp_path / "beliefs")
    assert compile_guards(store) == []


def test_compile_guards_needs_confirmed_not_just_hypothesis(tmp_path):
    """Guards are derived only from CONFIRMED beliefs; a mere hypothesis of the exact same shape must never be enforced at the call boundary."""
    confirmed_store = BeliefStore("lab", root=tmp_path / "beliefs_confirmed")
    confirmed = Belief(
        id="lab.search.cap", tool="lab", operation="search", cls="silent_truncation",
        parameter="page_size", doc_claims="", belief="capped at 50",
    )
    confirmed_store.add(confirmed)
    confirmed.confirm("probe0")
    guards = compile_guards(confirmed_store)
    assert any(isinstance(g, ClampPageSize) for g in guards)

    hypothesis_store = BeliefStore("lab", root=tmp_path / "beliefs_hypothesis")
    hypothesis = Belief(
        id="lab.search.cap2", tool="lab", operation="search", cls="silent_truncation",
        parameter="page_size", doc_claims="", belief="capped at 50",
    )
    hypothesis_store.add(hypothesis)  # deliberately never confirmed
    assert compile_guards(hypothesis_store) == []


def test_clamp_page_size_before_clamps_over_cap_and_leaves_under_cap_alone():
    """The guard must fire only when the requested page_size actually exceeds the real cap, never distorting a request already within it."""
    guard = ClampPageSize(name="clamp_page_size", implements_class="silent_truncation", parameter="page_size")

    over, refusal_over = guard.before("search", {"page_size": 500})
    assert refusal_over is None
    assert over["page_size"] == guard.cap
    assert guard.fired == 1

    under, refusal_under = guard.before("search", {"page_size": 20})
    assert refusal_under is None
    assert under["page_size"] == 20
    assert guard.fired == 1  # unchanged: nothing to clamp


# ---------------------------------------------------------------------------
# Contract layer.
# ---------------------------------------------------------------------------


def test_contract_detects_silent_truncation_of_echoed_field():
    """A field silently truncated on the wire must be flagged, or a caller has no way to know 'stored as sent' was a lie."""
    layer = ContractLayer()
    call = Call(n=1, op="create", request={"title": "x" * 400}, status=200,
                response={"title": "x" * 255}, t=0.0)
    anomalies = layer.record(call)
    kinds = [(a.kind, a.parameter) for a in anomalies]
    assert ("silent_truncation", "title") in kinds


def test_cursor_expiry_requires_exact_error_not_a_substring_match():
    """Bug #2's root cause: an agent's own garbage cursor also gets a 400 mentioning 'cursor', and matching that substring once turned it into a false expiry anomaly. Only the exact cursor_expired error may fire."""
    invalid_cursor_layer = ContractLayer()
    invalid_cursor_call = Call(
        n=1, op="search", request={"cursor": "garbage"}, status=400,
        response={"error": "invalid_cursor"}, t=0.0,
    )
    anomalies = invalid_cursor_layer.record(invalid_cursor_call)
    assert not any(a.kind == "expiry" for a in anomalies)

    expired_cursor_layer = ContractLayer()
    expired_cursor_call = Call(
        n=1, op="search", request={"cursor": "abc"}, status=400,
        response={"error": "cursor_expired"}, t=0.0,
    )
    anomalies2 = expired_cursor_layer.record(expired_cursor_call)
    assert any(a.kind == "expiry" and a.parameter == "cursor" for a in anomalies2)


def test_anomaly_signature_is_stable_and_shaped_operation_parameter_kind():
    """Signature must stay operation.parameter.kind so the same anomaly recurring across runs is recognised as one thing, not counted fresh each time."""
    with_param = Anomaly(kind="silent_truncation", operation="search", summary="s",
                          expected="e", observed="o", parameter="page_size")
    assert with_param.signature() == "search.page_size.silent_truncation"

    without_param = Anomaly(kind="undocumented_status", operation="create", summary="s",
                             expected="e", observed="o", parameter=None)
    assert without_param.signature() == "create._.undocumented_status"


# --- structural identity may not be invented by the verdict step -----------


def test_restatement_may_not_invent_a_parameter():
    """A cursor-expiry finding came back with parameter 'wait_s', the knob the
    experiment turned rather than the thing the API mishandles, and then
    matched no ground-truth rule at all."""
    from agent.beliefs import Belief
    from reflect.probe import _rewrite_from_evidence

    b = Belief(
        id="lab.search.x", tool="lab", operation="search", cls="expiry",
        parameter="cursor", doc_claims="cursors never expire",
        belief="cursors expire", action="reissue",
    )
    b.note("signature", "search.cursor.expiry")
    _rewrite_from_evidence(b, {
        "learned": "cursors expire after 60 seconds",
        "learned_parameter": "wait_s",
    })
    assert b.parameter == "cursor", "the verdict must not rename the parameter"
    assert any(h["event"] == "parameter_restatement_rejected" for h in b.history)


def test_restatement_may_not_invent_a_class():
    """A silent_null anomaly came back restated as silent_truncation, which no
    wire evidence supported, and scored as a false belief."""
    from agent.beliefs import Belief
    from reflect.probe import _rewrite_from_evidence

    b = Belief(
        id="lab.create.y", tool="lab", operation="create", cls="silent_coercion",
        parameter="due_date", doc_claims="any ISO date", belief="stored null",
    )
    b.note("signature", "create.due_date.silent_null")
    _rewrite_from_evidence(b, {
        "learned": "pre-1970 dates are stored as null",
        "learned_class": "silent_truncation",
    })
    assert b.cls == "silent_coercion", "wire evidence admits only silent_coercion"


def test_replay_total_does_not_double_count():
    """Two beliefs can blame the same call; summing per-belief totals inflated
    the headline by nearly 2x (219 attributions over 115 distinct calls)."""
    from replay.counterfactual import replay_all
    from agent.beliefs import BeliefStore

    rep = replay_all(BeliefStore("lab"))
    assert rep["total_wasted_calls_deduped"] <= rep["total_wasted_calls"]


# ---------------------------------------------------------------------------
# Status-blindness in the probe templates -- the same vacuous-measurement bug
# class, one layer in from the four above. An error body is still a dict, so a
# 429 or a 502 reads as "the field came back null" / "the row count changed"
# unless the template checks the status first.
# ---------------------------------------------------------------------------


class _StubAdapter:
    """Returns a scripted (status, body) per operation. No network, no lab."""

    def __init__(self, search=None, create=None):
        self._search = list(search or [])
        self._create = list(create or [])
        self.calls: list[str] = []

    def search(self, **_kw):
        self.calls.append("search")
        return self._search.pop(0)

    def create(self, **_kw):
        self.calls.append("create")
        return self._create.pop(0)


def test_timing_rate_limited_read_is_not_eventual_consistency():
    """A 429 on one of the two reads makes `before != after` true for a store that never changed; a failed read must be uninformative, not evidence of lag."""
    from reflect.templates import _timing

    a = _StubAdapter(search=[
        (200, {"results": [{"id": str(i)} for i in range(12)]}),
        (429, {"error": "rate_limited", "message": "too many requests"}),
    ])
    obs = _timing(a, {"wait_s": 0.0, "base": {"filter": {"vendor": "V"}, "page_size": 50}})

    assert obs.facts["usable"] is False
    assert obs.facts["changed"] is None, "a failed read may not report a change"
    assert obs.facts["after_status"] == 429
    assert _uninformative(obs) is True, "the pipeline guard has to be able to see this"


def test_timing_two_good_reads_still_report_the_change():
    """The status check must not neuter the template: two successful reads that genuinely differ still report lag."""
    from reflect.templates import _timing

    a = _StubAdapter(search=[
        (200, {"results": [{"id": "1"}]}),
        (200, {"results": [{"id": "1"}, {"id": "2"}, {"id": "3"}]}),
    ])
    obs = _timing(a, {"wait_s": 0.0, "base": {"filter": {"vendor": "V"}, "page_size": 50}})

    assert obs.facts["usable"] is True
    assert obs.facts["changed"] is True
    assert obs.facts["delta"] == 2
    assert _uninformative(obs) is False


def test_boundary_create_sweep_with_no_committed_row_observed_nothing():
    """Per-row `usable: False` stops one bad create reading as coercion, but a sweep where EVERY create failed carries no `returned` key for the all-zero check to find, so it needs its own uninformative signal."""
    from reflect.templates import _boundary

    a = _StubAdapter(create=[(429, {"error": "rate_limited"})] * 4)
    obs = _boundary(a, {"op": "create", "param": "title", "values": [1, 10, 50, 200]})

    assert obs.facts["usable_rows"] == 0
    assert all(r["stored"] is None and r["usable"] is False for r in obs.facts["sweep"])
    assert _uninformative(obs) is True, "a sweep that committed nothing measured nothing"


def test_boundary_create_sweep_with_one_good_row_is_still_evidence():
    """One survivor is enough to keep the experiment: the guard must fire on nothing-committed, not on anything-failed."""
    from reflect.templates import _boundary

    a = _StubAdapter(create=[
        (502, {"error": "bad_gateway"}),
        (200, {"title": "x" * 255}),
        (429, {"error": "rate_limited"}),
        (502, {"error": "bad_gateway"}),
    ])
    obs = _boundary(a, {"op": "create", "param": "title",
                        "values": ["x" * 10, "x" * 400, "x" * 500, "x" * 600]})

    assert obs.facts["usable_rows"] == 1
    assert _uninformative(obs) is False
    good = [r for r in obs.facts["sweep"] if r["usable"]]
    assert good[0]["stored"] == 255, "the one successful create still reports truncation"


def test_consistency_502_after_commit_cannot_confirm_silent_coercion():
    """The lab 502s on ~5% of creates by design, and its error body is a dict, so every field sent reads back as `stored: null` -- the exact shape of real silent-coercion evidence. Fixed upstream; this locks it down."""
    from reflect.templates import _consistency

    a = _StubAdapter(create=[(502, {"error": "bad_gateway", "message": "upstream failure"})])
    obs = _consistency(a, {"fields": {"title": "victim", "amount": 999}, "vendor": "V"})

    assert obs.facts["usable"] is False
    assert obs.facts["create_status"] == 502
    assert "mismatches" not in obs.facts, "a failed create may not report field mismatches"
    assert _uninformative(obs) is True
    assert a.calls == ["create"], "a failed create must not be read back"


def test_consistency_successful_create_still_reports_real_coercion():
    """The status check must not blind the template to the coercion it exists to find."""
    from reflect.templates import _consistency

    class _A(_StubAdapter):
        def get(self, _id):
            return 200, {"id": "i1", "title": "kept", "due_date": None}

    a = _A(create=[(200, {"id": "i1", "title": "kept", "due_date": None})])
    obs = _consistency(a, {"fields": {"title": "kept", "due_date": "1969-07-20"}, "vendor": "V"})

    assert obs.facts["echo_matches_sent"] is False
    assert obs.facts["mismatches"]["due_date"] == {"sent": "1969-07-20", "stored": None}
    assert _uninformative(obs) is False


# ---------------------------------------------------------------------------
# The shared request budget. A per-adapter pacer is computed from the tool's
# limit under the assumption the adapter is alone; concurrency makes that
# assumption false, and the limit is global, so the enforcement must be too.
# ---------------------------------------------------------------------------


def test_four_workers_stay_within_one_global_budget():
    """Four adapters each individually paced under a 3 req/s limit summed to ~10 req/s and 72% 429s. Sharing one budget, no 1s window may ever hold more than 3 sends."""
    import threading
    import time as _time

    from agent.adapters.budget import RateBudget

    budget = RateBudget(n=3, window_s=1.0, safety_s=0.0)
    sends: list[float] = []
    lock = threading.Lock()

    def worker():
        for _ in range(3):
            budget.acquire()
            with lock:
                sends.append(_time.monotonic())

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(sends) == 12
    sends.sort()
    # The server evicts entries strictly older than the window, then rejects
    # at >= n. So any 4 sends inside one window is a 429 on the 4th.
    worst = max(
        sum(1 for s in sends if start <= s < start + 1.0) for start in sends
    )
    assert worst <= 3, f"{worst} sends landed in one 1s window; the tool allows 3"


def test_budget_does_not_serialise_below_the_limit():
    """The guard must cost nothing when the fleet is already under budget, or it is just a slower pacer."""
    import time as _time

    from agent.adapters.budget import RateBudget

    budget = RateBudget(n=3, window_s=1.0, safety_s=0.0)
    t0 = _time.monotonic()
    for _ in range(3):
        budget.acquire()
    assert _time.monotonic() - t0 < 0.1, "the first n calls in a window must not block"
    assert budget.waits == 0


def test_bursting_probe_takes_the_budget_instead_of_racing_siblings():
    """header_burst exists to trip the limiter, so it cannot run under a budget that prevents tripping it -- but a burst fired while siblings send makes THEIR 429s, so it holds the budget rather than bypassing it."""
    import threading
    import time as _time

    from agent.adapters.budget import RateBudget

    budget = RateBudget(n=3, window_s=1.0, safety_s=0.0)
    order: list[str] = []
    released = threading.Event()

    def sibling():
        budget.acquire()
        order.append("sibling")

    with budget.exclusive():
        t = threading.Thread(target=sibling)
        t.start()
        _time.sleep(0.2)
        order.append("burst-done")
        released.set()
    t.join(timeout=5.0)

    assert order[0] == "burst-done", "a sibling sent while the burst held the budget"
    assert order == ["burst-done", "sibling"]


# ---------------------------------------------------------------------------
# Guard derivation from evidence. `unknown_field_ignored` is the one rule
# whose parameter is not a fixed field of the documented surface -- it is
# whatever key the caller happened to send -- so matching it on a literal
# means the guard compiles only for beliefs minted by one code path.
# ---------------------------------------------------------------------------


def _unknown_field_belief(tmp_path, parameter: str, signature: str | None):
    from agent.beliefs import Belief, BeliefStore

    store = BeliefStore("lab", root=tmp_path / "beliefs")
    b = Belief(
        id=f"lab.update.{parameter}", tool="lab", operation="update",
        cls="silent_coercion", parameter=parameter,
        doc_claims="PATCH with an unknown field returns 400 unknown_field",
        belief="PATCH accepts unknown fields with 200 and silently discards them",
    )
    store.add(b)
    if signature:
        b.note("signature", signature)
    b.confirm("probe0")
    return store


def _names(store):
    return [g.name for g in compile_guards(store)]


def test_unknown_field_guard_compiles_for_any_field_name(tmp_path):
    """Pinned to the literal `not_a_real_field`, a correctly confirmed and correctly scored belief compiled to zero enforcement whenever the run used a different field name -- which every path except one hardcoded recon probe does."""
    store = _unknown_field_belief(
        tmp_path, "nonexistent_field", "update.nonexistent_field.silent_ignore")
    assert "reject_unknown_update_field" in _names(store)


def test_unknown_field_guard_still_compiles_for_the_original_probe_name(tmp_path):
    """The belief the recon path actually mints must keep working."""
    store = _unknown_field_belief(
        tmp_path, "not_a_real_field", "update.not_a_real_field.silent_ignore")
    assert "reject_unknown_update_field" in _names(store)


def test_unknown_field_guard_is_matched_on_evidence_not_a_field_blacklist(tmp_path):
    """The discriminator is the wire evidence (`silent_ignore` on `update`), not "is this name absent from a list I wrote down" -- so a documented field whose evidence really is silent_ignore still compiles."""
    store = _unknown_field_belief(tmp_path, "assignee", "update.assignee.silent_ignore")
    assert "reject_unknown_update_field" in _names(store)


def test_unknown_field_guard_refuses_a_belief_whose_evidence_is_something_else(tmp_path):
    """Compiling from evidence has to cut both ways: a silent_coercion belief on update whose anomaly was a type coercion is a different defect and must not compile this guard."""
    store = _unknown_field_belief(tmp_path, "amount", "update.amount.type_coercion")
    assert "reject_unknown_update_field" not in _names(store)


def test_unknown_field_guard_does_not_leak_its_sentinel_into_the_manifest(tmp_path):
    """The exported guard manifest is a published artifact; an internal matching sentinel appearing as the guard's parameter would be a lie about what was observed."""
    from shim.guards import UNKNOWN_UPDATE_FIELD, guard_report

    store = _unknown_field_belief(
        tmp_path, "nonexistent_field", "update.nonexistent_field.silent_ignore")
    row = next(r for r in guard_report(compile_guards(store))
               if r["guard"] == "reject_unknown_update_field")
    assert row["parameter"] == "nonexistent_field"
    assert UNKNOWN_UPDATE_FIELD not in str(row)


def test_guard_field_list_is_not_a_second_copy_of_the_schema():
    """The fallback for a belief with no recorded signature reads the documented surface itself. A hand-maintained duplicate drifts, and the failure when it does is silent: the guard stops compiling."""
    from agent.contract import DOC_SPEC
    from shim.guards import DOCUMENTED_FIELDS

    assert DOCUMENTED_FIELDS == frozenset(DOC_SPEC["promises"]["echo_fields"])


# ---------------------------------------------------------------------------
# The retirement demo. It exists to show a belief unlearning when the world
# changes, which makes a check that answers backwards, or answers from a
# population that could not have observed the rule, worse than no check.
# ---------------------------------------------------------------------------


class _PagedStore:
    """A lab-shaped search: honours cursors, and optionally caps page_size."""

    def __init__(self, rows: int, cap: int | None = 50):
        self.rows = rows
        self.cap = cap
        self.bulk_calls = 0

    def search(self, **kw):
        page = kw.get("page_size", 50)
        if self.cap is not None:
            page = min(page, self.cap)
        offset = int(kw.get("cursor") or 0)
        got = max(0, min(page, self.rows - offset))
        has_more = offset + got < self.rows
        return 200, {"results": [{"id": str(offset + i)} for i in range(got)],
                     "has_more": has_more,
                     "next_cursor": str(offset + got) if has_more else None}

    def bulk_create(self, items):
        self.bulk_calls += 1
        self.rows += len(items)
        return 200, {"ids": ["x"] * len(items)}


def test_retire_demo_cap_check_answers_the_question_its_caller_asks(monkeypatch):
    """`check_behaviour` is documented to return world_matches_docs and every other check does. This one returned its negation, so the demo ran backwards: disabling the cap made the docs correct and the demo read that as the old lie persisting."""
    import bench.retire_demo as rd

    monkeypatch.setattr(rd.time, "sleep", lambda *_a, **_k: None)

    capped, _ = rd._check_page_size_cap(_PagedStore(rows=60, cap=50))
    assert capped is False, "cap in force means the world contradicts the docs"

    uncapped, _ = rd._check_page_size_cap(_PagedStore(rows=60, cap=None))
    assert uncapped is True, "cap gone means the world matches the docs"


def test_retire_demo_cap_check_refuses_an_insufficient_population(monkeypatch):
    """The audit's finding: a store holding fewer rows than the cap cannot distinguish a cap from a small store. Seeding is now verified by re-counting, and a population that still cannot exercise the rule gets no verdict rather than a guess."""
    import bench.retire_demo as rd

    monkeypatch.setattr(rd.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(rd, "WANT_ROWS", 0)  # suppress seeding

    verdict, msg = rd._check_page_size_cap(_PagedStore(rows=12, cap=50))
    assert verdict is None, "a 12-row store cannot answer a question about a 50-row cap"
    assert "no verdict" in msg


def test_retire_demo_counts_population_without_using_the_cap(monkeypatch):
    """Counting rows with a big page is circular when the cap is the thing under test; the count must be the same whether the rule is on or off."""
    import bench.retire_demo as rd

    monkeypatch.setattr(rd.time, "sleep", lambda *_a, **_k: None)
    assert rd._count_population(_PagedStore(rows=137, cap=50), "V") == 137
    assert rd._count_population(_PagedStore(rows=137, cap=None), "V") == 137


def test_retire_demo_seeds_enough_rows_to_exercise_the_cap(monkeypatch):
    """Seeding is a request, not a fact, so the check re-counts before answering. It must seed strictly past the cap, or the answer means nothing either way."""
    import bench.retire_demo as rd

    monkeypatch.setattr(rd.time, "sleep", lambda *_a, **_k: None)
    store = _PagedStore(rows=0, cap=50)
    verdict, msg = rd._check_page_size_cap(store)

    assert store.rows > rd.PAGE_SIZE_CAP, "seeded population must exceed the cap"
    assert store.bulk_calls > 0
    assert verdict is False, f"the cap is on in this store; got {verdict} ({msg})"


def test_retire_demo_cap_constant_tracks_the_lab():
    """`bench/retire_demo.py` is an HTTP client of the lab, so it restates the cap rather than importing the server. A restated limit drifts, and when this one drifts the check silently seeds a population that cannot exercise the rule."""
    from bench.retire_demo import PAGE_SIZE_CAP
    from lab.server import PAGE_SIZE_CAP as LAB_CAP

    assert PAGE_SIZE_CAP == LAB_CAP


def test_idempotent_create_guard_does_not_pay_for_a_discarded_search():
    """The guard issued the identical /v1/search twice, discarding the first. A wasted round trip against a 3 req/s tool, invisible in the run's call count because it happens inside .after()."""
    from shim.guards import IdempotentCreate

    class _Client:
        def __init__(self):
            self.posts = 0

        def post(self, _path, json=None):
            self.posts += 1

            class _R:
                status_code = 200

                @staticmethod
                def json():
                    return {"results": [{"title": "already-there"}]}

            return _R()

    class _Adapter:
        def __init__(self):
            self.client = _Client()

    import shim.guards as g
    real_sleep, g.time.sleep = g.time.sleep, lambda *_a, **_k: None
    try:
        a = _Adapter()
        guard = IdempotentCreate(implements_class="idempotency_hazard",
                                 parameter=None, note="", name="idempotent_create")
        out = guard.after("create", {"title": "already-there", "vendor": "V"}, 502, {}, a)
    finally:
        g.time.sleep = real_sleep

    assert a.client.posts == 1, f"the guard made {a.client.posts} searches; one answers the question"
    assert out["committed_despite_error"] is True
