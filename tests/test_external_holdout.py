import pytest

from bench.external_holdout import evaluate


def test_reports_valid_false_refusals_and_invalid_false_accepts_separately():
    out = evaluate([
        {"workflow_id": "a", "split": "holdout", "tool_version": "1", "expected": "allow", "observed": "deny"},
        {"workflow_id": "b", "split": "holdout", "tool_version": "2", "expected": "deny", "observed": "allow", "confirmed_defect": True},
    ])
    assert out["valid_false_refusal_rate"] == 1
    assert out["invalid_false_accept_rate"] == 1
    assert out["n_distinct_tool_versions"] == 2
    assert out["external_provenance_established"] is False


def test_discovery_rows_cannot_leak_into_holdout_scoring():
    with pytest.raises(ValueError):
        evaluate([{"workflow_id": "a", "split": "discovery", "tool_version": "1", "expected": "allow", "observed": "allow"}])


def test_missing_tool_version_is_not_counted_as_an_independent_version():
    with pytest.raises(ValueError):
        evaluate([{"workflow_id": "a", "split": "holdout", "expected": "allow", "observed": "allow"}])
