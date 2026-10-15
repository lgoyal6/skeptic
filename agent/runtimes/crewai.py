"""CrewAI Flow and BaseTool adapter with no implicit model/provider creation."""
from __future__ import annotations
from typing import Any, Type
from importlib.metadata import version

from pydantic import BaseModel, Field
from crewai.flow.flow import Flow, start
from crewai.tools import BaseTool

from .contract import Action, Executor
from .tracing import runtime_receipt


class ActionInput(BaseModel):
    action_id: str
    tool: str
    payload: dict[str, Any] = Field(default_factory=dict)
    approved: bool = True
    approval_expires_at: float | None = None
    cancelled: bool = False
    run_id: str = "default"
    session_id: str = "default"


class GuardedTool(BaseTool):
    name: str = "guarded_action"
    description: str = "Execute one policy-checked tool action with stable identity."
    args_schema: Type[BaseModel] = ActionInput
    executor: Any

    def _run(self, action_id: str, tool: str, payload: dict[str, Any],
             approved: bool = True, approval_expires_at: float | None = None,
             cancelled: bool = False, run_id: str = "default",
             session_id: str = "default") -> dict:
        from dataclasses import asdict
        return asdict(self.executor.execute(Action(action_id, tool, payload, approved,
                                                   approval_expires_at, cancelled,
                                                   run_id, session_id)))


def run(executor: Executor, action: Action) -> dict:
    """Execute one scripted action through CrewAI's Flow kickoff and BaseTool."""
    guarded = GuardedTool(executor=executor)

    class ActionFlow(Flow):
        @start()
        def dispatch(self):
            current = self.state["action"]
            return guarded.run(action_id=current.action_id, tool=current.tool,
                               payload=current.payload, approved=current.approved,
                               approval_expires_at=current.approval_expires_at,
                               cancelled=current.cancelled, run_id=current.run_id,
                               session_id=current.session_id)

    flow = ActionFlow(tracing=False, suppress_flow_events=True)
    outcome = flow.kickoff(inputs={"action": action})
    from dataclasses import asdict
    return runtime_receipt("crewai", version("crewai"), asdict(action), outcome)
