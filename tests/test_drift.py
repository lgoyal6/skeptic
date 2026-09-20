"""
Tests for temporal contracts: change-point detection, classification, attribution.

The real transition in this corpus (Frankfurter v1 -> v2) is one change, and
one change cannot demonstrate that four kinds are told apart. So the four
kinds are planted here -- a docs edit with identical behaviour, a response
shape change, a meaning change under an unchanged shape, and a transient
failure -- and each must land in its own bucket.

The planted transient is the one that matters most. A detector that cannot
tell a 503 from a contract change will promote every blip into permanent
belief, and this project's own lab fires a 502 on 5% of writes by design.

No network, no credentials, no model calls.
"""

from __future__ import annotations

import copy
import json

import pytest

from contracts.drift import (
    DOCUMENTATION,
    SCHEMA,
    SEMANTIC,
    TRANSIENT,
    build_timeline,
    classify,
    find_change_points,
    union_rules,
    window_from_fixture,
)
from contracts.model import CONFIRMATIONS_REQUIRED, schema_of

TOOL = "frankfurter"
V1, V2 = "v1-2026-09-19", "v2-2026-09-19"
RULES = union_rules(TOOL, [V1, V2])


def W(version, label, **kw):
    return window_from_fixture(TOOL, version, label=label, rules=RULES, **kw)


# --- perturbations, used to plant one kind of change at a time --------------


def plant_schema_change(rows):
    """Add a field to every successful body: shape moves, meaning does not."""
    out = []
    for r in copy.deepcopy(rows):
        if r["status"] < 400 and isinstance(r["response"], dict):
            r["response"]["server_region"] = "eu-west-1"
        out.append(r)
    return out


def plant_semantic_change(rows):
    """Make the substituted date match the request: shape identical, meaning flips."""
    out = []
    for r in copy.deepcopy(rows):
        body = r["response"]
        if r["status"] == 200 and isinstance(body, dict) and "date" in body:
            want = r["request"].get("date") or r["path"].rstrip("/").split("/")[-1]
            if want.count("-") == 2:
                body["date"] = want
        out.append(r)
    return out


def plant_transient(rows, n=2):
    """Turn the first n successful calls into 503s, as a blip would."""
    out, hit = [], 0
    for r in copy.deepcopy(rows):
        if r["status"] == 200 and hit < n:
            r["status"] = 503
            r["response"] = {"error": "service_unavailable"}
            hit += 1
        out.append(r)
    return out


# ---------------------------------------------------------------------------
# The four kinds are classified separately
# ---------------------------------------------------------------------------


def test_documentation_only_drift_is_not_a_behaviour_change():
    """The docs moved and nothing else did. That is a changelog entry, and calling it a break would cry wolf on every typo fix."""
    a = W(V1, "t0")
    b = W(V1, "t1", docs_override="# Rewritten prose, identical behaviour\n")
    changes = classify(a, b)

    kinds = {c.kind for c in changes}
    assert kinds == {DOCUMENTATION}, f"expected documentation only, got {kinds}"
    assert "documentation-only drift" in changes[0].detail
    assert a.schema == b.schema and a.semantics == b.semantics


def test_schema_drift_is_reported_as_schema_not_semantic():
    """A new field appears. Parsers care; meaning did not move."""
    a = W(V1, "t0")
    b = W(V1, "t1", perturb=plant_schema_change)
    changes = classify(a, b)

    assert {c.kind for c in changes} == {SCHEMA}, [c.kind for c in changes]
    c = changes[0]
    assert "added ['server_region']" in c.detail
    assert c.evidence, "a schema change must cite the exchanges whose shape moved"


def test_semantic_drift_is_reported_under_an_unchanged_schema():
    """The expensive kind: every field is where it was, and the answer is different.

    A parser keeps working and the logic that consumes it is now wrong. That
    is why this is classified apart from a schema change rather than folded in
    with it.
    """
    a = W(V1, "t0")
    b = W(V1, "t1", perturb=plant_semantic_change)
    changes = classify(a, b)

    kinds = {c.kind for c in changes}
    assert SEMANTIC in kinds
    assert SCHEMA not in kinds, "the response shape did not change; only the values did"
    moved = [c for c in changes if c.kind == SEMANTIC]
    assert any(c.signal.endswith("non_publication_date_silently_substituted") for c in moved)
    assert a.schema == b.schema


def test_transient_failure_is_never_a_contract_change():
    """Negative control: a 503 in one window must not open a contract version.

    This is the failure that matters. The lab fires a 502 on 5% of writes by
    design, so a detector that promotes a blip would manufacture a contract
    version on an ordinary run.
    """
    windows = [W(V1, "t0"), W(V1, "t1"),
               W(V1, "t2", perturb=plant_transient),   # the blip
               W(V1, "t3"), W(V1, "t4")]
    store, changes = build_timeline(windows)

    assert len(store.versions) == 1, (
        f"a transient 503 opened {len(store.versions)} contract versions; "
        f"it must open none")
    assert any(c.kind == TRANSIENT for c in changes), "the blip should still be reported"
    promoted = [c for c in changes if c.promoted and c.kind != DOCUMENTATION]
    assert not promoted, f"nothing should have been promoted, got {[c.signal for c in promoted]}"


def test_a_persistent_failure_is_not_dismissed_as_transient():
    """The control on the control: if the 503s never stop, that is the new reality and must not be waved away as noise."""
    windows = [W(V1, "t0"), W(V1, "t1"),
               W(V1, "t2", perturb=plant_transient),
               W(V1, "t3", perturb=plant_transient),
               W(V1, "t4", perturb=plant_transient)]
    store, _changes = build_timeline(windows)
    assert len(store.versions) == 2, (
        "a failure present in every window from t2 onward is a change in what the "
        "tool does, not a blip, and must open a new contract version")
    assert store.versions[1].effective_from == "t2"


# ---------------------------------------------------------------------------
# Change-point method
# ---------------------------------------------------------------------------


def test_change_point_needs_confirmation_not_one_differing_window():
    """A single differing window is an event. The change point is where a value takes hold."""
    windows = [W(V1, "t0"), W(V1, "t1"),
               W(V2, "t2"), W(V2, "t3"), W(V2, "t4")]
    points = find_change_points(windows, confirmations=CONFIRMATIONS_REQUIRED)
    assert points, "the v1 -> v2 transition should be detected"
    for signal, idxs in points.items():
        assert idxs == [2], f"{signal} changed at {idxs}, expected the real boundary at index 2"


def test_a_flicker_is_not_a_change_point():
    """One window differs and the next reverts. Nothing changed."""
    windows = [W(V1, "t0"), W(V1, "t1"),
               W(V1, "t2", perturb=plant_schema_change),   # flicker
               W(V1, "t3"), W(V1, "t4")]
    points = find_change_points(windows, confirmations=2)
    assert points == {}, f"a one-window flicker was treated as a change point: {points}"


def test_change_point_is_deterministic_across_repeated_runs():
    """The same windows in the same order must always produce the same answer."""
    def run():
        ws = [W(V1, "t0"), W(V1, "t1"), W(V2, "t2"), W(V2, "t3")]
        store, changes = build_timeline(ws)
        return (json.dumps(store.to_dict(), sort_keys=True, default=str),
                sorted((c.kind, c.signal, c.promoted) for c in changes))

    first, second, third = run(), run(), run()
    assert first == second == third


# ---------------------------------------------------------------------------
# Negative control: clock skew and ordering
# ---------------------------------------------------------------------------


def test_clock_skew_cannot_move_the_change_point():
    """`observed_at` is reporting metadata, not the ordering key.

    Capture timestamps come from whichever machine made them. If ordering were
    recovered from that field, a clock a few seconds off would reorder a
    timeline and move the inferred change point -- an inference about a tool
    decided by a laptop's system clock.
    """
    windows = [W(V1, "t0"), W(V1, "t1"), W(V2, "t2"), W(V2, "t3")]
    baseline = find_change_points(windows)

    skewed = [copy.deepcopy(w) for w in windows]
    for i, w in enumerate(skewed):
        w.observed_at = [500.0, -3000.0, 12.0, -99999.0][i]   # nonsense clocks
    assert find_change_points(skewed) == baseline

    store_a, _ = build_timeline(windows)
    store_b, _ = build_timeline(skewed)
    assert [c.effective_from for c in store_a.versions] == \
           [c.effective_from for c in store_b.versions]


def test_reordering_the_timeline_changes_the_answer_and_is_supposed_to():
    """The control on the previous test.

    Order carries real information, so it must matter. The point of ignoring
    `observed_at` is that order becomes an explicit property of the timeline
    rather than something guessed from an untrusted field -- not that order is
    irrelevant.
    """
    forward = [W(V1, "t0"), W(V1, "t1"), W(V2, "t2"), W(V2, "t3")]
    backward = [W(V2, "t0"), W(V2, "t1"), W(V1, "t2"), W(V1, "t3")]
    fs, _ = build_timeline(forward)
    bs, _ = build_timeline(backward)
    assert fs.versions[0].semantics != bs.versions[0].semantics


def test_fixture_order_within_a_window_does_not_change_the_schema():
    """Two calls to one operation are merged by union, so capture order cannot decide the observed shape."""
    a = W(V1, "t0")
    b = W(V1, "t1", perturb=lambda rows: list(reversed(rows)))
    assert a.schema == b.schema
    assert classify(a, b) == []


# ---------------------------------------------------------------------------
# Contracts are immutable and the predecessor stays inspectable
# ---------------------------------------------------------------------------


def test_a_new_version_does_not_mutate_its_predecessor():
    """The old contract is the only record that the change happened. Overwriting it destroys the evidence for the thing being reported."""
    windows = [W(V1, "t0"), W(V1, "t1"), W(V2, "t2"), W(V2, "t3")]
    store, _ = build_timeline(windows)

    assert len(store.versions) == 2
    old, new = store.versions
    assert old.version == 1 and new.version == 2
    assert new.predecessor == 1 and old.predecessor is None
    assert old.semantics["unknown_symbol_silently_dropped"] == "contradicts_doc"
    assert new.semantics["unknown_symbol_silently_dropped"] == "unobservable"
    assert old.docs_sha256 != new.docs_sha256
    assert old.windows == ["t0", "t1"] and new.windows == ["t2", "t3"]


def test_a_new_version_records_what_established_it():
    """A contract version with no attribution is an assertion. The changes that opened it are stored on it."""
    windows = [W(V1, "t0"), W(V1, "t1"), W(V2, "t2"), W(V2, "t3")]
    store, _ = build_timeline(windows)
    est = store.versions[1].established_by
    assert est, "the new version cites nothing"
    kinds = {c["kind"] for c in est}
    assert {DOCUMENTATION, SCHEMA, SEMANTIC} <= kinds, kinds
    assert any(c["evidence"] for c in est), "no change narrowed to specific exchanges"


def test_attribution_names_specific_exchanges_not_whole_windows():
    """'Something in these five calls changed' is not attribution."""
    a, b = W(V1, "t0"), W(V2, "t1")
    changes = classify(a, b)
    cited = [c for c in changes if c.evidence]
    assert cited, "no change cited any exchange"
    for c in cited:
        assert len(c.evidence) <= 3, (
            f"{c.signal} cited {len(c.evidence)} exchanges; attribution should narrow "
            f"to the smallest set whose signal moved")


# ---------------------------------------------------------------------------
# The real transition, and the demotion it causes
# ---------------------------------------------------------------------------


def test_the_real_version_change_demotes_a_stale_belief():
    """Frankfurter v1 silently substituted a date; v2 returns the date asked for.

    A belief minted under v1 is false under v2, and the point of carrying the
    rule set across versions is that this is *measured* -- the verdict flips on
    recorded evidence -- rather than assumed from the version number.
    """
    windows = [W(V1, "t0"), W(V1, "t1"), W(V2, "t2"), W(V2, "t3")]
    series = [w.semantics["non_publication_date_silently_substituted"] for w in windows]
    assert series == ["contradicts_doc", "contradicts_doc", "supports_doc", "supports_doc"]


def test_a_belief_the_new_capture_cannot_test_is_set_aside_not_refuted():
    """v2 renamed `symbols` to `quotes`, so the v2 capture never asks the question the v1 belief answers.

    The honest outcome is abstention. Recording it as a refutation would be
    inventing evidence from an absence -- the same error as reading an empty
    sweep as a disproof, which is the bug this project has found five times.
    """
    windows = [W(V1, "t0"), W(V1, "t1"), W(V2, "t2"), W(V2, "t3")]
    series = [w.semantics["unknown_symbol_silently_dropped"] for w in windows]
    assert series == ["contradicts_doc", "contradicts_doc", "unobservable", "unobservable"]
    assert "supports_doc" not in series, (
        "the v2 capture contains no request using `symbols`, so it cannot support "
        "the documented claim either -- only abstain")


# ---------------------------------------------------------------------------
# schema_of
# ---------------------------------------------------------------------------


def test_schema_ignores_values_and_list_length():
    """A page of 100 and a page of 5 have one schema, or every pagination difference reads as a schema change."""
    assert schema_of({"a": 1}) == schema_of({"a": 2})
    assert schema_of([{"x": 1}] * 100) == schema_of([{"x": 9}])
    assert schema_of({"a": 1}) != schema_of({"a": "1"})
    assert schema_of({"a": None}) != schema_of({"a": 1})


def test_schema_is_insensitive_to_key_order():
    assert schema_of({"a": 1, "b": 2}) == schema_of({"b": 2, "a": 1})


def test_an_error_body_is_not_part_of_the_response_schema():
    """A 503 has a shape of its own, and folding it into the schema would make every blip read as a structural break.

    This is the guard that makes the transient class possible at all. Without
    it, `{"error": "service_unavailable"}` merges into the shape of a
    successful response and the detector reports a break every time a server
    hiccups -- which, for this project's own lab, is 5% of writes by design.
    """
    clean = W(V1, "t0")
    blipped = W(V1, "t1", perturb=plant_transient)

    assert "error" not in json.dumps(blipped.schema), (
        "the 503 body leaked into the observed response schema")
    assert 503 in blipped.transport["rates"], "the failure is still recorded in transport"
    assert not [c for c in classify(clean, blipped) if c.kind == SCHEMA]


def test_a_narrower_schema_from_a_smaller_sample_is_not_a_removed_field():
    """Absence of evidence is not evidence of absence, in the drift detector itself.

    The observed schema is a union over the window's successful calls, so it
    is a lower bound on the real shape. When a blip removes the only call that
    carried `GBP`, the union narrows -- and calling that a removed field would
    promote a transient failure into "the API deleted something". It is
    reported as a sampling event instead, with the counts that justify the
    decision.
    """
    clean = W(V1, "t0")
    blipped = W(V1, "t1", perturb=plant_transient)

    assert clean.schema != blipped.schema, (
        "this test is about the case where the union genuinely narrows; if the "
        "schemas match, the fixture no longer exercises it")
    assert blipped.observed_counts["rates"] < clean.observed_counts["rates"]

    changes = classify(clean, blipped)
    assert not [c for c in changes if c.kind == SCHEMA]
    sampling = [c for c in changes if c.signal == "sample:rates"]
    assert sampling and sampling[0].kind == TRANSIENT
    assert "lower bound" in sampling[0].detail


def test_a_genuine_field_removal_is_still_reported():
    """The control on the control: when the sample did NOT shrink, a field going missing is a real schema change and must not be excused as sampling."""
    def drop_a_field(rows):
        out = []
        for r in copy.deepcopy(rows):
            if r["status"] == 200 and isinstance(r["response"], dict):
                r["response"].pop("amount", None)
            out.append(r)
        return out

    a = W(V1, "t0")
    b = W(V1, "t1", perturb=drop_a_field)
    assert a.observed_counts == b.observed_counts, "the sample size is unchanged here"
    changes = classify(a, b)
    schema_changes = [c for c in changes if c.kind == SCHEMA]
    assert schema_changes, "a removal under an equal sample must be reported"
    assert "removed ['amount']" in schema_changes[0].detail


def test_a_window_of_only_failures_reports_no_schema_rather_than_an_error_schema():
    """If every call failed, the window observed nothing about the response shape, and must say so instead of describing the error envelope."""
    all_bad = W(V1, "t0", perturb=lambda rows: plant_transient(rows, n=99))
    assert all_bad.schema == {}, f"expected no observed schema, got {list(all_bad.schema)}"
    assert all_bad.observed_counts.get("rates", 0) == 0
    assert all_bad.transport["rates"], "the statuses are still recorded"
