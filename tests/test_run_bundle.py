"""The Phase 3 run bundle is complete, ordered, and replayable."""

from __future__ import annotations

import json

from bench.run_bundle import EVENT_TYPES, build_bundle, verify_bundle


def test_bundle_contains_every_required_evidence_boundary():
    bundle = build_bundle("frankfurter", "v2-2026-09-19", policy="greedy", seed=1337)

    assert bundle["source"]["docs_sha256"]
    assert bundle["source"]["fixture_sha256"]
    assert bundle["selector"] == {"policy": "greedy", "seed": 1337, "budget_calls": 5}
    assert bundle["belief_store"]["before"]
    assert bundle["belief_store"]["after"]
    assert bundle["probe_log"]
    assert bundle["model_usage_by_role"]["boundary"] == "offline replay uses no model calls"
    assert bundle["generated_guards"]
    assert bundle["corrected_specification"]["version"] == 1
    assert verify_bundle(bundle) == []


def test_events_are_typed_ordered_and_capture_decisions_before_observations():
    bundle = build_bundle("frankfurter", "v2-2026-09-19", policy="eig", seed=7)
    events = bundle["events"]
    names = [e["event"] for e in events]

    assert set(names) <= EVENT_TYPES
    assert [e["sequence"] for e in events] == list(range(1, len(events) + 1))
    assert "hypotheses_minted" in names
    assert "probe_selected" in names
    assert "prediction_committed" in names
    assert "anomaly_detected" in names
    assert "belief_confirmed" in names
    assert "belief_falsified" in names
    assert "guard_compiled" in names
    assert names[-1] == "corrected_specification_published"

    first_selected = names.index("probe_selected")
    first_prediction = names.index("prediction_committed")
    first_verdict = min(names.index("belief_confirmed"), names.index("belief_falsified"))
    assert first_selected < first_prediction < first_verdict


def test_bundle_hash_detects_tampering():
    bundle = build_bundle("lab", "seed1337-2026-09-19")
    tampered = json.loads(json.dumps(bundle))
    tampered["probe_log"][0]["status"] = 599

    assert any("bundle hash mismatch" in p for p in verify_bundle(tampered))


def test_source_replay_detects_tampering_even_after_rehash():
    bundle = build_bundle("lab", "seed1337-2026-09-19")
    tampered = json.loads(json.dumps(bundle))
    tampered["probe_log"][0]["status"] = 599

    body = dict(tampered)
    body.pop("bundle_sha256")
    from bench.run_bundle import _canonical_bytes, _sha256_bytes
    tampered["bundle_sha256"] = _sha256_bytes(_canonical_bytes(body))

    assert "bundle does not reproduce from the committed source fixture" in verify_bundle(tampered)


def test_same_fixture_policy_and_seed_produce_identical_bundle():
    first = build_bundle("lab", "seed1337-2026-09-19", policy="greedy", seed=42)
    second = build_bundle("lab", "seed1337-2026-09-19", policy="greedy", seed=42)

    assert first == second


def test_unobservable_evidence_is_rejected_not_promoted():
    bundle = build_bundle("github", "rest-2026-09-19")
    events = bundle["events"]
    rejected = [e for e in events if e["event"] == "observation_rejected_as_uninformative"]
    after = {b["id"]: b for b in bundle["belief_store"]["after"]}

    assert rejected
    assert any(e["event"] == "belief_contested" for e in events)
    assert after["rate_limit_window_actually_resets"]["status"] == "contested"
    assert after["rate_limit_window_actually_resets"]["verdict"] == "unobservable"
