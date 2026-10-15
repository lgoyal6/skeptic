from agent.runtimes.contract import Action, Executor
from agent.runtimes.crewai import run as crewai_run
from agent.runtimes.google_adk import run as adk_run
from agent.runtimes.native import run as native_run


def _result(runtime, executor, action):
    receipt = runtime(executor, action)
    assert receipt["trace"]["schema_version"] == "1"
    assert receipt["trace"]["action_id"] == action.action_id
    return receipt["trace"]["payload"]["outcome"], receipt


def test_native_crewai_and_adk_keep_the_same_valid_and_denied_contract():
    for runtime in (native_run, crewai_run, adk_run):
        effects = []
        executor = Executor({"read": lambda payload: effects.append(payload) or {"value": payload["key"]}},
                            allowed={"read"})
        ok, receipt = _result(runtime, executor, Action("valid-1", "read", {"key": "v"}))
        denied, _ = _result(runtime, executor, Action("denied-1", "delete", {}))
        assert ok["accepted"] and ok["status"] == "completed"
        assert ok["result"] == {"value": "v"}
        assert not denied["accepted"] and denied["status"] == "denied"
        if runtime is not native_run:
            assert receipt["sdk_version"]
        assert effects == [{"key": "v"}]


def test_sdk_callbacks_share_idempotency_ledger():
    effects = []
    executor = Executor({"write": lambda payload: effects.append(payload) or {"written": True}},
                        allowed={"write"})
    action = Action("same-action", "write", {"value": 1})
    first, _ = _result(crewai_run, executor, action)
    replay, _ = _result(adk_run, executor, action)
    assert first["status"] == replay["status"] == "completed"
    assert first["replayed"] is False and replay["replayed"] is True
    assert effects == [{"value": 1}]


def test_valid_control_expired_cancelled_and_budget_cases_agree_across_runtimes():
    for runtime in (native_run, crewai_run, adk_run):
        effects = []
        executor = Executor({"read": lambda p: effects.append(p) or {"ok": True}},
                            allowed={"read"}, max_calls=1, clock=lambda: 10.0)
        cases = [
            (Action("good", "read", {}), "completed"),
            (Action("expired", "read", {}, approval_expires_at=10.0), "expired_approval"),
            (Action("cancel", "read", {}, cancelled=True), "cancelled"),
            (Action("overflow", "read", {}), "budget_exceeded"),
            (Action("denied", "erase", {}), "denied"),
        ]
        for action, expected in cases:
            receipt = runtime(executor, action)
            outcome = (receipt["trace"]["payload"]["outcome"]
                       if "trace" in receipt else receipt)
            assert outcome["status"] == expected
        assert effects == [{}]
