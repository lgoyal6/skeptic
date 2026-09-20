"""
Tests for the probe-selection comparison, and the result it produced.

The comparison exists to answer one question honestly: is the selector doing
work, or would any order have done as well? That only means something if every
arm gets identical inputs and no arm can see the answers.

The headline result is that **expected information gain loses to the simpler
greedy policy** on this corpus. That is asserted here rather than described,
so the claim in the README cannot drift away from what the code does.

No network, no credentials, no model calls.
"""

from __future__ import annotations

import dataclasses
import inspect
import random

import pytest
import yaml

from bench import policies as policy_mod
from bench.policies import POLICIES, Claim, Probe, PolicyView, greedy
from bench.policy_eval import _answer_key, evaluate_all, run_one, settleable_claims, summarise
from fixtures.format import fixture_dir, tools

CORPUS = tools()

# Computed on first use rather than at import. Module-level work makes a
# collection error out of what should be a test failure, which is exactly how
# a mutation run can report "survived" for a fix that is genuinely covered.
_CACHE: dict[str, object] = {}


def summary():
    if "summary" not in _CACHE:
        _CACHE["summary"] = summarise(evaluate_all(seeds=20, corpus=CORPUS))
    return _CACHE["summary"]


# ---------------------------------------------------------------------------
# No arm may see the answer key
# ---------------------------------------------------------------------------


def test_a_policy_cannot_reach_the_answer_key():
    """An EIG policy fitted on the evaluation labels would win by construction and would have measured nothing except its own access to the answers.

    Enforced structurally: the objects a policy receives have no field holding
    a verdict, a truth, or a mismatch class. "Be careful not to look" is not a
    mechanism.
    """
    leaky = {"truth", "mismatch_class", "answer", "label", "outcome", "expected"}
    for cls in (Probe, Claim, PolicyView):
        fields = {f.name for f in dataclasses.fields(cls)}
        assert not (fields & leaky), f"{cls.__name__} exposes {fields & leaky}"

    # Claim carries only what the documentation says, plus what has been paid for.
    assert {f.name for f in dataclasses.fields(Claim)} == {
        "rule_id", "operation", "parameter", "settled", "verdict"}


def test_no_policy_reads_the_expected_yaml_or_the_checks_module():
    """The source of every policy is inspected for a route to the labels."""
    src = inspect.getsource(policy_mod)
    for forbidden in ("expected.yaml", "answer_key", "settleable_claims",
                      "load_traffic", "evaluate("):
        assert forbidden not in src, f"bench/policies.py references {forbidden!r}"


def test_a_claim_handed_to_a_policy_carries_no_verdict_before_it_is_paid_for():
    rows_key = _answer_key(*CORPUS[0])
    rid, spec = next(iter(rows_key.items()))
    c = Claim(rule_id=rid, operation=str(spec.get("operation") or ""),
              parameter=spec.get("parameter"))
    assert c.verdict is None and c.settled is False


# ---------------------------------------------------------------------------
# Every arm gets identical inputs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool,version", CORPUS, ids=[f"{t}@{v}" for t, v in CORPUS])
def test_every_arm_faces_the_same_claims_and_budget(tool, version):
    runs = {p: run_one(p, tool, version, seed=0) for p in POLICIES}
    budgets = {r.budget for r in runs.values()}
    settleable = {r.settleable for r in runs.values()}
    assert len(budgets) == 1, f"arms ran under different budgets: {budgets}"
    assert len(settleable) == 1, f"arms faced different targets: {settleable}"


def test_unobservable_claims_are_presented_to_every_arm():
    """A policy that could tell in advance which questions are unanswerable would be reading the labels. The honest task includes questions with no answer."""
    tool, version = "github", "rest-2026-09-19"
    key = _answer_key(tool, version)
    ceiling = settleable_claims(tool, version)
    assert any(v == "unobservable" for v in ceiling.values())
    r = run_one("greedy", tool, version, seed=0)
    assert r.settleable < len(key), "the unobservable claim should not count toward the target"
    assert r.abstentions >= 1, "the unobservable claim should be reported as an abstention"


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["fixed", "greedy", "eig"])
def test_deterministic_arms_are_deterministic(name):
    """A deterministic policy must ignore the seed entirely, or its 'distribution' across seeds would be an artefact."""
    tool, version = CORPUS[0]
    orders = {tuple(run_one(name, tool, version, seed=s).order) for s in range(5)}
    assert len(orders) == 1, f"{name} produced {len(orders)} different orders across seeds"


def test_the_random_arm_actually_varies():
    """The control on the control: if the random arm never varied, comparing against it would prove nothing."""
    tool, version = "lab", "seed1337-2026-09-19"
    orders = {tuple(run_one("random", tool, version, seed=s).order) for s in range(20)}
    assert len(orders) > 1


def test_eig_falls_back_deterministically_when_underdetermined():
    """With nothing open, outcome probabilities are undefined; the policy hands back to greedy rather than picking arbitrarily."""
    probes = [Probe(key="a", op="search", request={"page_size": 1}),
              Probe(key="b", op="search", request={"page_size": 500})]
    view = PolicyView(unobserved=list(probes), observed=[], claims=[])
    rng = random.Random(0)
    assert POLICIES["eig"](view, rng).key == greedy(view, rng).key

    # And with an open claim no candidate bears on, the same fallback applies.
    view2 = PolicyView(unobserved=list(probes), observed=[],
                       claims=[Claim(rule_id="x", operation="create", parameter="due_date")])
    assert POLICIES["eig"](view2, rng).key == greedy(view2, rng).key


# ---------------------------------------------------------------------------
# The result, asserted so it cannot drift from the README
# ---------------------------------------------------------------------------


def test_no_arm_forms_a_false_belief():
    """The one result that would disqualify a policy regardless of its call count.

    A selector that settles claims faster by concluding things the evidence
    does not support has not improved anything. This was not free: an earlier
    version of the corpus checks produced 52 false beliefs across these arms,
    all from concluding `supports_doc` on evidence that could not have shown
    otherwise.
    """
    for name in POLICIES:
        assert summary()[name]["false_beliefs"] == 0, (
            f"{name} formed {summary()[name]['false_beliefs']} false belief(s)")


def test_every_arm_succeeds_on_every_fixture():
    for name in POLICIES:
        assert summary()[name]["success_rate"] == 1.0, (
            f"{name} succeeded on only {summary()[name]['success_rate']:.0%} of runs")


def test_expected_information_gain_does_not_beat_greedy_on_this_corpus():
    """The published negative result.

    EIG spends more calls than the greedy heuristic it was meant to improve
    on. The plan for this work said to keep the simpler policy and publish the
    result if it lost, so this asserts the loss: if a future change makes EIG
    win, this test fails and the claim in the README has to be rewritten
    rather than quietly becoming true.
    """
    assert summary()["eig"]["calls_mean"] >= summary()["greedy"]["calls_mean"], (
        f"eig now uses {SUMMARY['eig']['calls_mean']} calls/run against greedy's "
        f"{SUMMARY['greedy']['calls_mean']}. If that is real, update the README, "
        f"the evaluation record, and this test together.")


def test_greedy_beats_both_naive_baselines():
    """The positive half: the selector is doing work, not decorating an arbitrary order."""
    assert summary()["greedy"]["calls_mean"] < summary()["fixed"]["calls_mean"]
    assert summary()["greedy"]["calls_mean"] < summary()["random"]["calls_mean"]


def test_the_comparison_reports_zero_for_costs_it_did_not_measure():
    """Replay spends no tokens and no model wall time. Printing a plausible number for either would be inventing evidence."""
    for name in POLICIES:
        assert summary()[name]["tokens"] == 0
        assert summary()[name]["wall_s"] == 0.0
