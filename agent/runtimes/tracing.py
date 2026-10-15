"""Versioned local runtime trace contract and conservative redaction."""
from __future__ import annotations
from dataclasses import asdict, dataclass
import json
import re
from pathlib import Path
from typing import Any

TRACE_SCHEMA_VERSION = "1"
_SENSITIVE_KEY = re.compile(r"(authorization|api[_-]?key|secret|password|token|cookie)", re.I)
_BEARER = re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/-]+=*")


def redact(value: Any, key: str = "") -> Any:
    if _SENSITIVE_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(item, key) for item in value]
    if isinstance(value, str):
        return _BEARER.sub("Bearer [REDACTED]", value)
    return value


@dataclass(frozen=True)
class TraceEvent:
    run_id: str
    action_id: str
    attempt_id: str
    runtime: str
    event_type: str
    payload: dict
    schema_version: str = TRACE_SCHEMA_VERSION
    session_id: str = "default"

    def validate(self):
        if not all((self.run_id, self.session_id, self.action_id, self.attempt_id,
                    self.runtime, self.event_type)):
            raise ValueError("trace correlation fields are required")
        if self.schema_version != TRACE_SCHEMA_VERSION:
            raise ValueError("unsupported trace schema version")


def append_trace(path: str | Path, event: TraceEvent) -> None:
    event.validate()
    row = asdict(event)
    row["payload"] = redact(row["payload"])
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as stream:
        stream.write(json.dumps(row, sort_keys=True) + "\n")


def runtime_receipt(runtime: str, sdk_version: str, action: dict,
                    outcome: dict) -> dict:
    event = TraceEvent(
        run_id=str(action.get("run_id", "default")),
        session_id=str(action.get("session_id", "default")),
        action_id=str(action["action_id"]),
        attempt_id=f"{action['action_id']}:0", runtime=runtime,
        event_type="verified_tool_outcome",
        payload={"action": action, "outcome": outcome},
    )
    event.validate()
    return {"runtime": runtime, "sdk_version": sdk_version,
            "trace": redact(asdict(event))}
