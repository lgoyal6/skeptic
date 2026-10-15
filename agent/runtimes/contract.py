"""Share policy, call budgets, and effect receipts across runtime adapters.

Used by native.py, crewai.py, and google_adk.py at the action boundary.
One executor serializes admission and effects; shared multi-process use is unsupported.
"""
from __future__ import annotations
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import time
import threading
from typing import Callable


@dataclass(frozen=True)
class Action:
    action_id: str
    tool: str
    payload: dict
    approved: bool = True
    approval_expires_at: float | None = None
    cancelled: bool = False
    run_id: str = "default"
    session_id: str = "default"

    def validate(self) -> None:
        if not self.action_id or not self.tool or not self.run_id or not self.session_id:
            raise ValueError("action_id, tool, run_id, and session_id are required")
        if not isinstance(self.payload, dict):
            raise ValueError("payload must be an object")


@dataclass(frozen=True)
class Outcome:
    action_id: str
    tool: str
    accepted: bool
    status: str
    result: dict = field(default_factory=dict)
    replayed: bool = False


class Executor:
    """Deterministic policy/effect boundary; SDKs only transport actions."""
    def __init__(self, handlers: dict[str, Callable[[dict], dict]], *, allowed: set[str],
                 ledger_path: str | Path | None = None, max_calls: int = 8,
                 clock: Callable[[], float] = time.time):
        self._lock = threading.RLock()
        self.handlers = dict(handlers)
        self.allowed = frozenset(allowed)
        if max_calls < 1:
            raise ValueError("max_calls must be positive")
        self.max_calls = max_calls
        self.clock = clock
        self.calls: dict[tuple[str, str], int] = {}
        self.ledger_path = Path(ledger_path) if ledger_path else None
        self.effects: dict[str, Outcome] = {}
        self.fingerprints: dict[str, str] = {}
        if self.ledger_path and self.ledger_path.exists():
            for line in self.ledger_path.read_text().splitlines():
                if line.strip():
                    row = json.loads(line)
                    if row.get("event") == "call_count":
                        key = (row["run_id"], row["session_id"])
                        self.calls[key] = max(self.calls.get(key, 0), row["count"])
                        continue
                    self.effects[row["action_id"]] = Outcome(
                        row["action_id"], row["tool"], row["accepted"],
                        row["status"], row.get("result", {}),
                    )
                    if row.get("fingerprint"):
                        self.fingerprints[row["action_id"]] = row["fingerprint"]

    @staticmethod
    def _fingerprint(action: Action) -> str:
        data = {"run_id": action.run_id, "session_id": action.session_id,
                "tool": action.tool, "payload": action.payload,
                "approved": action.approved,
                "approval_expires_at": action.approval_expires_at,
                "cancelled": action.cancelled}
        encoded = json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(encoded.encode()).hexdigest()

    def _record_call_count(self, action: Action) -> None:
        key = (action.run_id, action.session_id)
        self.calls[key] = self.calls.get(key, 0) + 1
        if self.ledger_path:
            self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
            row = {"event": "call_count", "run_id": action.run_id,
                   "session_id": action.session_id, "count": self.calls[key]}
            with self.ledger_path.open("a") as stream:
                stream.write(json.dumps(row, sort_keys=True) + "\n")
                stream.flush()
                os.fsync(stream.fileno())

    def _record(self, outcome: Outcome) -> None:
        self.effects[outcome.action_id] = outcome
        if self.ledger_path:
            self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
            row = {**outcome.__dict__, "recorded_at": time.time(),
                   "fingerprint": self.fingerprints.get(outcome.action_id)}
            with self.ledger_path.open("a") as stream:
                stream.write(json.dumps(row, sort_keys=True) + "\n")
                stream.flush()
                os.fsync(stream.fileno())

    def execute(self, action: Action) -> Outcome:
        # Admission, the running receipt, and the effect form one critical section.
        # Concurrent SDK callbacks cannot both accept an unseen action or budget slot.
        with self._lock:
            return self._execute_locked(action)

    def _execute_locked(self, action: Action) -> Outcome:
        action.validate()
        fingerprint = self._fingerprint(action)
        if action.action_id in self.effects:
            prior = self.effects[action.action_id]
            if self.fingerprints.get(action.action_id) != fingerprint:
                raise ValueError("action_id reused with different immutable inputs")
            status = "indeterminate" if prior.status == "running" else prior.status
            return Outcome(prior.action_id, prior.tool, prior.accepted, status,
                           prior.result, replayed=True)
        self.fingerprints[action.action_id] = fingerprint
        if action.cancelled:
            outcome = Outcome(action.action_id, action.tool, False, "cancelled")
        elif not action.approved:
            outcome = Outcome(action.action_id, action.tool, False, "approval_required")
        elif action.approval_expires_at is not None and self.clock() >= action.approval_expires_at:
            outcome = Outcome(action.action_id, action.tool, False, "expired_approval")
        elif action.tool not in self.allowed:
            outcome = Outcome(action.action_id, action.tool, False, "denied", {"reason": "tool not allowed"})
        elif action.tool not in self.handlers:
            outcome = Outcome(action.action_id, action.tool, False, "denied", {"reason": "no registered handler"})
        elif self.calls.get((action.run_id, action.session_id), 0) >= self.max_calls:
            outcome = Outcome(action.action_id, action.tool, False, "budget_exceeded")
        else:
            self._record_call_count(action)
            self._record(Outcome(action.action_id, action.tool, True, "running"))
            try:
                result = self.handlers[action.tool](dict(action.payload))
                if not isinstance(result, dict):
                    raise TypeError("tool handler must return a dict")
                outcome = Outcome(action.action_id, action.tool, True, "completed", result)
            except Exception as exc:  # deterministic failure value, no retry here
                outcome = Outcome(action.action_id, action.tool, True, "error", {"error": type(exc).__name__})
        self._record(outcome)
        return outcome
