import json
from dataclasses import replace
from pathlib import Path

import pytest

from agent.adapters.fixture import FixtureAdapter
from agent.beliefs import BeliefStore, Status
from bench.guard_study import Rule, confirm, learned_rules, main, run_trace, score, validate_workflows
from fixtures.format import call_key


def workflow(split="certification", version="v1"):
    return {"workflow_id": "goal-a", "goal_family": "catalog", "api_family": "external",
            "api_version": version, "split": split, "tool": "external", "capture_id": "recording",
            "calls": [{"op": "catalog", "path": "/items", "request": {"page": 1}},
                      {"op": "catalog", "path": "/items", "request": {"page": 2}}]}


@pytest.fixture
def replay(tmp_path):
    directory = tmp_path / "fixtures/external/recording"
    directory.mkdir(parents=True)
    (directory / "manifest.json").write_text(json.dumps({"allowed_operations": ["catalog"],
        "docs_sha256": "sha256:docs", "traffic_sha256": "sha256:traffic"}))
    (directory / "docs.md").write_text("The catalog response has an items array.")
    # Poison answer key proves the adapter/runner does not use it.
    (directory / "expected.yaml").write_text("not: [valid: yaml")
    rows = [{"op": c["op"], "path": c["path"], "request": c["request"],
             "key": call_key(c["op"], c["request"], c["path"]), "status": 200,
             "response": {"items": [{"id": c["request"]["page"]}]}, "headers": {}}
            for c in workflow()["calls"]]
    (directory / "traffic.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    return FixtureAdapter("control", "external", "recording", fixtures_root=tmp_path / "fixtures",
                          runs_dir=tmp_path / "runs")


def rule():
    return Rule.parse({"rule_id": "items-array-v1", "api_family": "external", "api_version": "v1",
        "op": "catalog", "kind": "type", "pointer": "/items", "value": "array",
        "doc_claim": "The catalog response has an items array."})


def test_honest_and_mutant_controls_with_real_replay(replay):
    assert run_trace(workflow(), "static", replay, [rule()])["observed"] == "allow"
    for response in replay.exchanges.values():
        response["response"]["items"] = "silently corrupted"
    assert run_trace(workflow(), "no", replay, [rule()])["observed"] == "allow"
    assert run_trace(workflow(), "static", replay, [rule()])["observed"] == "deny"


def test_predictions_precede_replay_and_falsification_confirms(replay, tmp_path):
    ledger = tmp_path / "predictions.jsonl"
    original = replay.call
    seen = []
    def call(*args, **kwargs):
        rows = [json.loads(s) for s in ledger.read_text().splitlines()]
        assert rows[-1]["event"] == "prediction"
        seen.append(rows[-1]["index"])
        return original(*args, **kwargs)
    replay.call = call
    store = BeliefStore("guard-study", tmp_path / "beliefs")
    belief = confirm(rule(), workflow("discovery"), [True, True], replay, store, ledger, 2)
    assert belief.status is Status.CONFIRMED and belief.survived_falsification
    assert seen == [0, 1]
    assert learned_rules(BeliefStore("guard-study", tmp_path / "beliefs")) == [rule()]
    replay.call = original
    assert run_trace(workflow(), "learned", replay, learned_rules(store))["observed"] == "allow"
    with pytest.raises(ValueError, match="overwrite"):
        confirm(rule(), workflow("discovery"), [True, True], replay, store, ledger, 2)


@pytest.mark.parametrize("mode", ["miss", "timeout", "empty", "counterexample"])
def test_failed_or_unobservable_probe_cannot_activate_guard(replay, tmp_path, mode):
    row = workflow("discovery")
    if mode == "miss":
        row["calls"][0]["request"] = {"page": 999}
    if mode == "timeout":
        def timeout(*args, **kwargs):
            raise TimeoutError("recorded timeout control")
        replay.call = timeout
    if mode == "empty":
        replay.call = lambda *args, **kwargs: (200, {})
    if mode == "counterexample":
        replay.call = lambda *args, **kwargs: (200, {"items": 17})
    store = BeliefStore("guard-study", tmp_path / "beliefs")
    belief = confirm(rule(), row, [True, True], replay, store, tmp_path / "ledger", 2)
    assert belief.status is not Status.CONFIRMED
    assert learned_rules(store) == []


def test_holdout_and_duplicate_probe_and_budget_refused(replay, tmp_path):
    store = BeliefStore("guard-study", tmp_path)
    for row, budget in [(workflow(), 2), (workflow("discovery"), 1)]:
        with pytest.raises(ValueError):
            confirm(rule(), row, [True, True], replay, store, tmp_path / "ledger", budget)
    row = workflow("discovery"); row["calls"][1] = row["calls"][0]
    with pytest.raises(ValueError, match="distinct"):
        confirm(rule(), row, [True, True], replay, store, tmp_path / "ledger", 2)


@pytest.mark.parametrize("key", ["expected", "observed", "confirmed_defect", "ground_truth"])
def test_answer_key_in_workflow_is_rejected(key):
    row = workflow(); row[key] = "allow"
    with pytest.raises(ValueError, match="label-free"):
        validate_workflows([row])


def test_lineage_cannot_bridge_discovery_into_certification():
    rows = [workflow("version_transfer", "v2"), workflow("discovery"), workflow()]
    for i, row in enumerate(rows):
        row["workflow_id"] = str(i)
    with pytest.raises(ValueError, match="lineage"):
        validate_workflows(rows)


def test_new_version_does_not_silently_inherit_old_guard(replay):
    for response in replay.exchanges.values():
        response["response"]["items"] = {"new": "valid version-two schema"}
    assert run_trace(workflow(version="v2"), "learned", replay, [rule()])["observed"] == "allow"
    assert run_trace(workflow(), "learned", replay, [rule()])["observed"] == "deny"


def test_invented_rule_and_boolean_number_refused():
    with pytest.raises(ValueError, match="unknown check"):
        Rule.parse({**rule().__dict__, "kind": "invented_class"})
    numeric = replace(rule(), value="number")
    assert numeric.check({"response": {"items": True}}) is not None
    assert numeric.check({"response": {"items": 3}}) is None


def test_invented_documentation_cannot_be_confirmed(replay, tmp_path):
    store = BeliefStore("guard-study", tmp_path / "beliefs")
    with pytest.raises(ValueError, match="quote discovery"):
        confirm(replace(rule(), doc_claim="Invented API promise"), workflow("discovery"),
                [True, True], replay, store, tmp_path / "ledger", 2)


def test_scorer_separate_join_keeps_inconclusive_denominator(replay):
    observed = run_trace(workflow(), "static", replay, [rule()])
    labels = [{"workflow_id": "goal-a", "expected": "allow", "tool_version": "v1", "provenance": "control"}]
    assert score([observed], labels)["binary_summary"]["valid_false_refusals"] == 0
    observed["observed"] = "inconclusive"
    result = score([observed], labels)
    assert result["attempted_valid_n"] == result["inconclusive_valid_n"] == 1
    assert result["binary_summary"] is None
    with pytest.raises(ValueError, match="populations"):
        score([observed], [])
    with pytest.raises(ValueError, match="version"):
        score([observed], [{**labels[0], "tool_version": "v2"}])


@pytest.mark.parametrize("arm", ["no", "static", "learned"])
def test_cli_emits_decisions_before_any_scorer_join(replay, tmp_path, monkeypatch, arm):
    manifest = tmp_path / "workflows.json"
    manifest.write_text(json.dumps([workflow()]))
    args = ["guard-study", str(manifest), "--fixtures", str(tmp_path / "fixtures"),
            "--output", str(tmp_path / "output"), "--arm", arm]
    if arm == "static":
        rules = tmp_path / "rules.json"; rules.write_text(json.dumps([rule().__dict__]))
        args += ["--rules", str(rules)]
    if arm == "learned":
        args += ["--beliefs", str(tmp_path / "empty-beliefs")]
    monkeypatch.setattr("sys.argv", args)
    main()
    decision = json.loads((tmp_path / "output/decisions.jsonl").read_text())
    assert decision["observed"] == "allow" and "expected" not in decision
    with pytest.raises(ValueError, match="overwrite"):
        main()


def test_workflow_id_cannot_escape_output_directory():
    row = workflow(); row["workflow_id"] = "../../outside"
    with pytest.raises(ValueError, match="safe path"):
        validate_workflows([row])


def test_documented_request_condition_accepts_current_only_response():
    hourly = replace(rule(), pointer="/hourly", value="object", request_keys=("hourly",))
    assert hourly.check({"request": {"current": "temperature_2m"}, "response": {"current": {}}}) is None
    assert hourly.check({"request": {"hourly": "temperature_2m"}, "response": {"current": {}}})
    assert hourly.check({"request": {"hourly": "temperature_2m"}, "response": {"hourly": {}}}) is None
    assert Rule.parse({**hourly.__dict__, "request_keys": ["hourly"]}) == hourly


@pytest.mark.parametrize("mode", ["inactive-condition", "empty-wildcard"])
def test_unexercised_predicate_cannot_confirm_a_belief(replay, tmp_path, mode):
    candidate = replace(rule(), request_keys=("unobserved_parameter",))
    if mode == "empty-wildcard":
        candidate = replace(rule(), pointer="/items/*/id", value="number")
        replay.call = lambda *args, **kwargs: (200, {"items": []})
    store = BeliefStore("guard-study", tmp_path / "beliefs")
    belief = confirm(candidate, workflow("discovery"), [True, True], replay,
                     store, tmp_path / "ledger", 2)
    assert belief.status is Status.HYPOTHESIS and learned_rules(store) == []
