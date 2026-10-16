import json

import pytest

from bench.guard_candidates import candidate_schema, parse_candidate

EXCERPT = "The catalog response has an items array. It is paginated."
QUOTE = "The catalog response has an items array."
WORKFLOW = {"workflow_id": "candidate-test", "goal_family": "catalog-discovery", "api_family": "test",
            "api_version": "v1", "split": "discovery", "tool": "test", "capture_id": "test-v1",
            "calls": [{"op": "catalog", "path": "/items", "request": {"page": 1}},
                      {"op": "catalog", "path": "/items", "request": {"page": 2}}]}
VALID = {"kind": "type", "pointer": "/items", "value": "array", "doc_claim": QUOTE,
         "request_keys": ["page"], "predictions": [True, True]}


def parse(value):
    return parse_candidate(json.dumps(value), excerpt=EXCERPT, quotes=[QUOTE], workflow=WORKFLOW, rule_id="items-v1")


def test_valid_candidate_uses_existing_rule_and_does_not_confirm_it():
    rule, predictions = parse(VALID)
    assert predictions == [True, True]
    assert rule.check({"response": {"items": []}, "request": {"page": 1}}) is None
    assert rule.check({"response": {"items": "corrupt"}, "request": {"page": 1}})
    assert parse({"abstain": True}) is None
    assert parse({**VALID, "pointer": "/a~1b/~0"})[0].pointer == "/a~1b/~0"


@pytest.mark.parametrize("change", [
    {"kind": "response_check"}, {"kind": []}, {"kind": "required", "value": "array"},
    {"predictions": [{"check": True}, {"check": False}]}, {"predictions": [1, 1]},
    {"predictions": [True]}, {"doc_claim": "The catalog has an items array."},
    {"request_keys": ["secret"]}, {"request_keys": ["page", "page"]},
    {"request_keys": [False]}, {"pointer": "/items/~2"}, {"pointer": "/api/items" * 20},
    {"kind": "equals", "value": {"items": []}}, {"extra": True},
])
def test_bad_candidates_are_rejected_without_repair(change):
    with pytest.raises(ValueError):
        parse({**VALID, **change})


def test_duplicates_nonfinite_and_fake_abstention_are_rejected():
    for text in ['{"abstain":1}', '{"abstain":true,"abstain":true}',
                 json.dumps({**VALID, "value": float("nan")})]:
        with pytest.raises(ValueError):
            parse_candidate(text, excerpt=EXCERPT, quotes=[QUOTE], workflow=WORKFLOW, rule_id="x")


def test_schema_enforces_types_and_quotes_without_accepting_holdout_inputs():
    schema = candidate_schema(EXCERPT, [QUOTE], WORKFLOW)
    variants = schema["anyOf"]
    assert [v["properties"]["kind"]["enum"][0] for v in variants[:3]] == ["required", "type", "equals"]
    assert variants[0]["properties"]["value"] == {"type": "null"}
    assert variants[0]["properties"]["predictions"]["items"] == {"type": "boolean"}
    assert variants[0]["properties"]["doc_claim"]["enum"] == [QUOTE]
    with pytest.raises(ValueError, match="discovery"):
        candidate_schema(EXCERPT, [QUOTE], {**WORKFLOW, "split": "certification"})
    with pytest.raises(ValueError, match="exact"):
        candidate_schema(EXCERPT, ["Invented quote."], WORKFLOW)
