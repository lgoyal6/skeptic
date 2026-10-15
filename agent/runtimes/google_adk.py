"""Google ADK Runner/session/FunctionTool path with scripted decisions only."""
from __future__ import annotations
import asyncio
import json
from importlib.metadata import version
from dataclasses import asdict
from typing import Any, AsyncGenerator

from google.adk.agents import BaseAgent, InvocationContext
from google.adk.events import Event
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools import FunctionTool, ToolContext
from google.genai.types import Content, Part

from .contract import Action, Executor
from .tracing import runtime_receipt


def _action_tool(executor: Executor) -> FunctionTool:
    def dispatch(action_id: str, tool: str, payload: dict[str, Any],
                 approved: bool, approval_expires_at: float | None, cancelled: bool,
                 run_id: str, session_id: str,
                 tool_context: ToolContext) -> dict:
        del tool_context
        return asdict(executor.execute(Action(action_id, tool, payload, approved,
                                              approval_expires_at, cancelled,
                                              run_id, session_id)))
    return FunctionTool(dispatch)


class _ScriptedAgent(BaseAgent):
    executor: Any
    action: Action

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        result = await _action_tool(self.executor).run_async(
            args={"action_id": self.action.action_id, "tool": self.action.tool,
                  "payload": self.action.payload, "approved": self.action.approved,
                  "approval_expires_at": self.action.approval_expires_at,
                  "cancelled": self.action.cancelled,
                  "run_id": self.action.run_id, "session_id": self.action.session_id},
            tool_context=ToolContext(ctx),
        )
        yield Event(author=self.name, content=Content(
            role="model", parts=[Part(text=json.dumps(result, sort_keys=True))]),
            turnComplete=True)


async def _run_async(executor: Executor, action: Action) -> dict:
    app_name, user_id, session_id = "skeptic-offline", action.run_id, action.session_id
    sessions = InMemorySessionService()
    await sessions.create_session(app_name=app_name, user_id=user_id,
                                  session_id=session_id, state={"action_id": action.action_id,
                                                               "run_id": action.run_id})
    agent = _ScriptedAgent(name="skeptic_contract_agent", executor=executor, action=action)
    runner = Runner(agent=agent, app_name=app_name, session_service=sessions)
    message = Content(role="user", parts=[Part(text=json.dumps(asdict(action), sort_keys=True))])
    events = [event async for event in runner.run_async(user_id=user_id,
                                                        session_id=session_id,
                                                        new_message=message)]
    final = next((event for event in reversed(events) if event.content and event.content.parts), None)
    if final is None:
        raise RuntimeError("ADK completed without a result event")
    return json.loads(final.content.parts[0].text)


def run(executor: Executor, action: Action) -> dict:
    """Run a real ADK session and tool workflow without constructing an LLM."""
    outcome = asyncio.run(_run_async(executor, action))
    return runtime_receipt("google-adk", version("google-adk"), asdict(action), outcome)
