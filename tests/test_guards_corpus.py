"""
Tests for guards compiled from the corpus, and for the regression gate itself.

A guard is only worth having if it is silent on honest traffic, loud on the
evidence that created it, and traceable to that evidence. All three are
checked here, and the gate that checks them in CI is checked too -- a gate
that cannot fail is the same problem as a test that cannot fail, one level up.

No network, no credentials, no model calls.
"""

from __future__ import annotations

import pytest

from bench.ci import HONEST, PLANTED, _exchanges
from fixtures.checks import CONTRADICTS, evaluate, load_traffic
from fixtures.format import tools
from shim.corpus_guards import (
    GUARD_BODIES,
    compile_from_fixture,
    uncompiled_mismatches,
)

CORPUS = tools()
GUARDS = [g for t, v in CORPUS for g in compile_from_fixture(t, v)]


def test_every_confirmed_mismatch_compiles_to_a_guard():
    """`shim/guards.py` claims that what the agent learns, the shim enforces. That is only true if nothing established is left unenforced."""
    missing = [f"{t}@{v}:{rid}" for t, v in CORPUS for rid in uncompiled_mismatches(t, v)]
    assert not missing, f"confirmed mismatches with no guard: {missing}"


def test_no_guard_is_compiled_from_an_abstention_or_an_honest_result():
    """Enforcing a rule nobody established is how an abstention quietly becomes a belief."""
    for g in GUARDS:
        rows = load_traffic(g.tool, g.fixture_version)
        assert evaluate(g.rule_id, rows).outcome == CONTRADICTS, (
            f"{g.name} was compiled from rule {g.rule_id} whose verdict is not a mismatch")


@pytest.mark.parametrize("g", GUARDS, ids=[f"{g.tool}:{g.rule_id}" for g in GUARDS])
def test_every_guard_carries_its_provenance(g):
    """A guard with no origin is an assertion somebody will eventually delete because nobody can say why it exists."""
    assert g.rule_id and g.mismatch_class and g.why
    assert g.evidence, f"{g.name} cites no exchanges"
    assert g.docs_sha256.startswith("sha256:")
    assert g.traffic_sha256.startswith("sha256:")
    keys = {r["key"] for r in load_traffic(g.tool, g.fixture_version)}
    assert set(g.evidence) <= keys


@pytest.mark.parametrize("tool,version", HONEST, ids=[f"{t}@{v}" for t, v in HONEST])
def test_no_guard_fires_on_a_tool_that_behaved(tool, version):
    """The test that makes a guard's silence mean something.

    MediaWiki clamps and says so; PokeAPI paginates honestly; Frankfurter v2
    rejects an unknown currency instead of dropping it. A guard that fired
    here would be crying wolf on the best-behaved services in the corpus, and
    the next person would switch it off.
    """
    for ex in _exchanges(tool, version):
        for g in GUARDS:
            assert g(ex) is None, f"{g.name} fired on honest {tool}@{version}: {g(ex)}"


@pytest.mark.parametrize("g", GUARDS, ids=[f"{g.tool}:{g.rule_id}" for g in GUARDS])
def test_every_guard_fires_somewhere_in_its_tools_recordings(g):
    """A guard that never fires is not enforcing anything.

    The version guard is why this searches every recording of the tool rather
    than only the guard's own: `assert_known_parameter_spelling` was
    established by the v2 contract, but the request it refuses is a v1-shaped
    one, which lives in the predecessor's traffic.
    """
    fired = any(g(ex) for t, v in CORPUS if t == g.tool for ex in _exchanges(t, v))
    assert fired, f"{g.name} fired on no recorded {g.tool} exchange"


@pytest.mark.parametrize("name", sorted({g.name for g in GUARDS}))
def test_every_guard_rejects_a_planted_violation(name):
    """A guard whose recorded case happens to be easy is still exercised against one built to break it."""
    assert name in PLANTED, f"{name} has no planted violation"
    g = next(g for g in GUARDS if g.name == name)
    assert g(PLANTED[name]), f"{name} passed a planted violation"


@pytest.mark.parametrize("name", sorted({g.name for g in GUARDS}))
def test_a_planted_violation_is_actually_a_violation(name):
    """The control on the planted cases: a planted response must differ from a clean one, or the previous test passes on a technicality."""
    g = next(g for g in GUARDS if g.name == name)
    clean = {"request": {}, "status": 200, "headers": {}, "response": {}}
    assert g(clean) is None, f"{name} fires on an empty clean response; it is too broad"


def test_guard_coverage_spans_the_mismatch_classes():
    """Coverage is reported by mismatch class, not by line: 90% line coverage of a module that never checks pagination says nothing about pagination regressions."""
    covered = {g.mismatch_class for g in GUARDS}
    required = {"status-code-mismatch", "field-omission", "renamed-field",
                "rate-limit", "pagination-cursor", "semantic-type-correct"}
    assert required <= covered, f"no guard for: {sorted(required - covered)}"


def test_the_pagination_guard_tells_a_clamp_from_the_end_of_the_data():
    """The sharpest distinction any guard in this corpus has to draw.

    PokeAPI returning 1351 rows for `limit=100000` is not a clamp; there are
    only 1351. GitHub returning 100 for `per_page=200` with a next link is.
    MediaWiki returning 500 for `srlimit=5000` is a clamp that announced
    itself. Only the middle one may fire.
    """
    from shim.corpus_guards import _page_size_honoured_or_announced as guard

    end_of_data = {"request": {"limit": 100000}, "status": 200, "headers": {},
                   "response": {"count": 1351, "next": None,
                                "results": [{"n": i} for i in range(1351)]}}
    clamped = {"request": {"per_page": 200}, "status": 200,
               "headers": {"link": '<https://api.github.com/x?page=2>; rel="next"'},
               "response": [{"sha": str(i)} for i in range(100)]}
    # The announced case must ALSO signal that more rows existed, or the
    # disclosure branch is never reached and the guard's silence would be an
    # accident. This is exactly how the real Wikipedia fixture passed at
    # first: not excused for disclosing, just unable to see 648 hits behind a
    # 500-row page.
    announced = {"request": {"srlimit": 5000}, "status": 200, "headers": {},
                 "response": {"query": {"search": [{"t": i} for i in range(500)],
                                        "searchinfo": {"totalhits": 648}},
                              "continue": {"sroffset": 500},
                              "warnings": {"search": {"*": "must be between 1 and 500"}}}}

    from shim.corpus_guards import _more_exists
    assert _more_exists(announced, 500), (
        "the announced case must look like a clamp before disclosure can excuse it")
    assert guard(end_of_data) is None, "fired on the end of the data"
    assert guard(clamped), "missed a silent clamp"
    assert guard(announced) is None, "fired on a clamp that disclosed itself"


def test_the_real_wikipedia_clamp_is_excused_for_disclosing_not_by_accident():
    """The guard must reach the disclosure branch on the real fixture.

    MediaWiki clamps `srlimit=5000` to 500 and reports 648 total hits plus a
    `continue` block, so the response does say more rows existed. If the
    detector cannot see that, the guard stays silent because it thinks the
    page was complete -- the right verdict for the wrong reason, and it would
    go on being right until a tool disclosed a clamp in a shape it did read.
    """
    from shim.corpus_guards import _more_exists
    from shim.corpus_guards import _page_size_honoured_or_announced as guard

    ex = next(e for e in _exchanges("wikipedia", "actionapi-2026-09-19")
              if e["request"].get("srlimit") == 5000)
    n = len(ex["response"]["query"]["search"])
    assert n < ex["request"]["srlimit"], "the fixture should show a clamp"
    assert _more_exists(ex, n), "the response does report more hits; the guard must see it"
    assert ex["response"].get("warnings"), "and it discloses the clamp"
    assert guard(ex) is None, "so the guard is excused by the disclosure, not by blindness"


def test_the_regression_gate_returns_nonzero_when_something_regresses(monkeypatch):
    """The gate has to be able to fail, or running it in CI is theatre."""
    import bench.ci as ci

    monkeypatch.setitem(ci.PLANTED, "assert_retry_after_present_on_429",
                        {"request": {}, "status": 200, "headers": {}, "response": {}})
    monkeypatch.setattr("sys.argv", ["ci"])
    assert ci.main() == 1


def test_the_regression_gate_passes_on_the_current_tree(monkeypatch):
    import bench.ci as ci

    monkeypatch.setattr("sys.argv", ["ci"])
    assert ci.main() == 0


def test_a_supported_claim_compiles_no_guard_even_when_a_body_exists(monkeypatch):
    """The filter is tested directly, because on this corpus it is otherwise inert.

    Every rule that has a guard body registered also happens to be a
    confirmed mismatch, so removing the `contradicts` filter changes nothing
    and a mutation of it survives. That is a property of the corpus, not
    evidence the filter works. Registering a body for a rule the tool
    *honours* exercises it: `limit_is_not_capped_at_100` is a supported claim
    on Open Library, and enforcing it would mean guarding against behaviour
    that never happened.
    """
    from shim import corpus_guards as cg

    rows = load_traffic("openlibrary", "search-2026-09-19")
    assert evaluate("limit_is_not_capped_at_100", rows).outcome == "supports_doc"

    monkeypatch.setitem(cg.GUARD_BODIES, "limit_is_not_capped_at_100",
                        ("assert_never_compiled", lambda _ex: "should not exist"))
    names = {g.name for g in cg.compile_from_fixture("openlibrary", "search-2026-09-19")}
    assert "assert_never_compiled" not in names, (
        "a claim the tool honours compiled to enforcement")


def test_an_abstention_compiles_no_guard(monkeypatch):
    """The same filter, from the other side: an unobservable claim is not a belief."""
    from shim import corpus_guards as cg

    rows = load_traffic("github", "rest-2026-09-19")
    assert evaluate("rate_limit_window_actually_resets", rows).outcome == "unobservable"

    monkeypatch.setitem(cg.GUARD_BODIES, "rate_limit_window_actually_resets",
                        ("assert_never_compiled", lambda _ex: "should not exist"))
    names = {g.name for g in cg.compile_from_fixture("github", "rest-2026-09-19")}
    assert "assert_never_compiled" not in names, (
        "an abstention compiled to enforcement; that is how a 'we could not tell' "
        "becomes a 'we know'")
