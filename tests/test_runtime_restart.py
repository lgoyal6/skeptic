import json

from agent.runtimes.contract import Action, Executor
from agent.runtimes.tracing import TraceEvent, append_trace
from agent.runtimes.tracing import TRACE_SCHEMA_VERSION
from agent.runtimes.google_adk import run as adk_run


def test_persistent_effect_ledger_prevents_duplicate_after_restart(tmp_path):
    path = tmp_path / "effects.jsonl"
    calls = []
    action = Action("a1", "write", {"value": 2})
    first_executor = Executor({"write": lambda p: calls.append(p) or {"ok": True}},
                              allowed={"write"}, ledger_path=path)
    assert adk_run(first_executor, action)["trace"]["payload"]["outcome"]["status"] == "completed"
    restarted = Executor({"write": lambda p: calls.append(p) or {"ok": True}},
                         allowed={"write"}, ledger_path=path)
    replay = adk_run(restarted, action)["trace"]["payload"]["outcome"]
    assert replay["replayed"] is True
    assert calls == [{"value": 2}]


def test_crash_after_effect_before_result_ack_is_indeterminate_not_replayed(tmp_path):
    path = tmp_path / "effects.jsonl"
    action = Action("a2", "write", {"value": 3})
    fingerprint = Executor._fingerprint(action)
    path.write_text(json.dumps({"action_id": "a2", "tool": "write", "accepted": True,
                                "status": "running", "result": {},
                                "fingerprint": fingerprint}) + "\n")
    calls = []
    executor = Executor({"write": lambda p: calls.append(p) or {"ok": True}},
                        allowed={"write"}, ledger_path=path)
    outcome = adk_run(executor, action)["trace"]["payload"]["outcome"]
    assert outcome["status"] == "indeterminate" and outcome["replayed"]
    assert calls == []


def test_local_trace_redacts_secrets_and_requires_correlated_ids(tmp_path):
    path = tmp_path / "trace.jsonl"
    event = TraceEvent("run", "action", "attempt", "adk", "tool_result",
                       {"authorization": "Bearer abc", "message": "Bearer xyz"})
    append_trace(path, event)
    row = json.loads(path.read_text())
    assert row["schema_version"] == TRACE_SCHEMA_VERSION
    assert row["payload"] == {"authorization": "[REDACTED]", "message": "Bearer [REDACTED]"}


def test_langsmith_run_tree_adapter_is_local_and_redacts_payload():
    from agent.observability.langsmith import to_run_tree
    event = TraceEvent("run-1", "action-1", "try-1", "native", "tool_result",
                       {"api_key": "secret", "outcome": {"status": "completed"}},
                       session_id="session-1")
    serialized = to_run_tree(event)
    assert serialized["name"] == "tool_result"
    assert serialized["inputs"]["session_id"] == "session-1"
    assert serialized["inputs"]["payload"]["api_key"] == "[REDACTED]"
    assert serialized["outputs"]["api_key"] == "[REDACTED]"


def test_schema_drift_and_stale_action_id_are_rejected():
    import pytest
    from agent.runtimes.contract import Action
    with pytest.raises(ValueError, match="schema"):
        TraceEvent("run", "a", "try", "native", "event", {}, schema_version="99").validate()
    executor = Executor({"read": lambda p: {"ok": True}}, allowed={"read", "write"})
    executor.execute(Action("shared-id", "read", {}))
    with pytest.raises(ValueError, match="different immutable inputs"):
        executor.execute(Action("shared-id", "write", {}))


def test_reused_action_id_rejects_changed_immutable_inputs(tmp_path):
    import pytest
    base = Action("same", "write", {"v": 1}, run_id="r", session_id="s")
    executor = Executor({"write": lambda p: {"ok": True}}, allowed={"write"},
                        ledger_path=tmp_path / "effects.jsonl")
    executor.execute(base)
    for changed in (
        Action("same", "write", {"v": 2}, run_id="r", session_id="s"),
        Action("same", "write", {"v": 1}, approved=False, run_id="r", session_id="s"),
        Action("same", "write", {"v": 1}, run_id="r", session_id="other"),
    ):
        with pytest.raises(ValueError, match="different immutable inputs"):
            executor.execute(changed)


def test_call_budget_survives_executor_restart_and_is_per_session(tmp_path):
    import pytest
    path = tmp_path / "effects.jsonl"
    calls = []
    handler = {"write": lambda p: calls.append(p) or {"ok": True}}
    first = Executor(handler, allowed={"write"}, ledger_path=path, max_calls=1)
    first.execute(Action("one", "write", {}, run_id="r", session_id="s"))
    restarted = Executor(handler, allowed={"write"}, ledger_path=path, max_calls=1)
    assert restarted.execute(Action("two", "write", {}, run_id="r", session_id="s")).status == "budget_exceeded"
    assert restarted.execute(Action("three", "write", {}, run_id="r", session_id="other")).status == "completed"
    assert len(calls) == 2
