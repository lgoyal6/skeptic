"""Build an offline LangSmith RunTree from a redacted local trace event.

This adapter deliberately never calls post/create_run or reads credentials.
It validates local schema mapping only; hosted ingestion is a separate step.
"""
from __future__ import annotations

from uuid import NAMESPACE_URL, uuid5

from langsmith.run_trees import RunTree

from agent.runtimes.tracing import TraceEvent, redact


def to_run_tree(event: TraceEvent) -> dict:
    """Return LangSmith's native serialized run shape without network access."""
    event.validate()
    payload = redact(event.payload)
    run = RunTree(
        id=uuid5(NAMESPACE_URL, f"{event.run_id}:{event.action_id}:{event.attempt_id}"),
        name=event.event_type,
        run_type="tool",
        inputs={"action_id": event.action_id, "attempt_id": event.attempt_id,
                "session_id": event.session_id, "payload": payload},
        project_name="skeptic-local",
        extra={"metadata": {"runtime": event.runtime,
                             "schema_version": event.schema_version,
                             "run_id": event.run_id}},
    )
    run.end(outputs=payload)
    return run.dict()
