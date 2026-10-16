import pytest

from api.date_guard import evaluate_exchange


def test_demo_endpoint_runs_the_source_date_guard():
    mismatch = evaluate_exchange({
        "requestDate": "2026-09-13",
        "responseDate": "2026-09-11",
        "status": 200,
    })
    assert mismatch["finding"] == (
        "requested date 2026-09-13 and the response carries 2026-09-11: "
        "a different day's data returned under a 200, with every field type-correct"
    )
    assert mismatch["checked"] is True
    assert evaluate_exchange({
        "requestDate": "2026-09-13",
        "responseDate": "2026-09-13",
        "status": 200,
    })["finding"] is None


def test_non_success_exchange_is_not_checked_by_the_date_guard():
    result = evaluate_exchange({
        "requestDate": "2026-09-13",
        "responseDate": "2026-09-11",
        "status": 503,
    })
    assert result["finding"] is None
    assert result["checked"] is False


@pytest.mark.parametrize("payload", [
    {"requestDate": "2026-02-30", "responseDate": "2026-09-11", "status": 200},
    {"requestDate": "2026-09-13", "responseDate": "2026-09-11", "status": True},
    {"requestDate": "2026-09-13", "responseDate": "2026-09-11", "status": 200, "extra": 1},
])
def test_demo_endpoint_rejects_invalid_payloads(payload):
    with pytest.raises(ValueError):
        evaluate_exchange(payload)
