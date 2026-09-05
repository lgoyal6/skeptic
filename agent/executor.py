"""
The executor: run one task against the tool, using the documentation and
whatever beliefs have been learned so far.

This is the part that gets better over time, and the only thing that changes
between run 1 and run 40 is the belief block in the system prompt. Same
model, same task, same tool. That isolation is what makes the memory-wipe
ablation a real control rather than a story.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent.adapters.lab import LabAdapter
from agent.beliefs import BeliefStore
from agent.llm import LLM, Usage
from agent.tasks import Task, TaskResult

MAX_STEPS = 28

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search",
            "description": "Search items.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filter": {"type": "object", "description": "Exact-match fields, e.g. {\"vendor\": \"Acme\"}"},
                    "page_size": {"type": "integer"},
                    "cursor": {"type": "string"},
                    "sort": {"type": "string", "enum": ["created", "last_edited"]},
                    "include_archived": {"type": "boolean"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_item",
            "description": "Create one item.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "amount": {"type": "number"},
                    "due_date": {"type": "string"},
                    "status": {"type": "string"},
                    "assignee": {"type": "string"},
                    "vendor": {"type": "string"},
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_item",
            "description": "Fetch one item by id.",
            "parameters": {
                "type": "object",
                "properties": {"item_id": {"type": "string"}},
                "required": ["item_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_item",
            "description": "Update fields on an item.",
            "parameters": {
                "type": "object",
                "properties": {"item_id": {"type": "string"}, "fields": {"type": "object"}},
                "required": ["item_id", "fields"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bulk_create",
            "description": "Create many items at once.",
            "parameters": {
                "type": "object",
                "properties": {"items": {"type": "array", "items": {"type": "object"}}},
                "required": ["items"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": "Give the final answer. Call this exactly once, when you are confident.",
            "parameters": {
                "type": "object",
                "properties": {"answer": {"type": "object", "description": "The JSON answer the task asked for."}},
                "required": ["answer"],
            },
        },
    },
]

SYSTEM = """You are an operations agent working against the Tasker API.

You have its documentation below. The documentation is written confidently \
but it is not always accurate about how the API really behaves. Trust what \
you actually observe on the wire over what the documentation promises.

Work carefully. Verify counts and totals rather than assuming a single call \
returned everything. When a response looks surprising, investigate before \
you rely on it.

--- DOCUMENTATION ---
{docs}
--- END DOCUMENTATION ---

{beliefs}

Call `finish` exactly once with the JSON answer the task asks for."""


@dataclass
class RunResult:
    run_id: str
    task_id: str
    success: bool
    detail: str
    expected: Any = None
    got: Any = None
    steps: int = 0
    tool_calls: int = 0
    anomalies: list[dict[str, Any]] = field(default_factory=list)
    signatures: list[str] = field(default_factory=list)
    wall_s: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    belief_context_tokens: int = 0
    beliefs_active: int = 0
    memory_enabled: bool = True
    answer: Any = None
    error: str | None = None

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def to_dict(self) -> dict[str, Any]:
        d = self.__dict__.copy()
        d["total_tokens"] = self.total_tokens
        return d


def run_task(
    task: Task,
    executor: LLM,
    run_id: str,
    cfg: dict[str, Any] | None = None,
    memory: bool = True,
    beliefs_root: str = "beliefs",
    runs_dir: str = "runs",
    guards: list[Any] | None = None,
    docs_path: str = "lab/DOCS.md",
    verbose: bool = False,
) -> RunResult:
    cfg = cfg or {}
    t0 = time.time()

    state = task.setup(cfg)
    goal = task.goal.format(**state)

    store = BeliefStore("lab", root=beliefs_root)
    if memory:
        belief_block = store.render_for_context(budget_tokens=2500)
        n_active = len(store.active())
        ctx_tokens = store.context_tokens()
    else:
        belief_block = "(memory disabled for this run)"
        n_active = 0
        ctx_tokens = 0

    docs = Path(docs_path).read_text()
    adapter = LabAdapter(run_id=run_id, guards=guards, runs_dir=runs_dir)

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM.format(docs=docs, beliefs=belief_block)},
        {"role": "user", "content": goal},
    ]

    p0, c0 = executor.usage.prompt_tokens, executor.usage.completion_tokens
    answer: Any = None
    steps = 0
    tool_calls = 0
    err: str | None = None

    try:
        while steps < MAX_STEPS:
            steps += 1
            rep = executor.chat(messages, tools=TOOLS, max_tokens=1800)

            if not rep.tool_calls:
                # nudge once toward the tool protocol, then give up on this run
                messages.append({"role": "assistant", "content": rep.content or ""})
                messages.append(
                    {"role": "user", "content": "Use a tool, or call `finish` with your JSON answer."}
                )
                if steps >= 3 and not any(m.get("role") == "tool" for m in messages):
                    err = "model never called a tool"
                    break
                continue

            messages.append(
                {"role": "assistant", "content": rep.content or "", "tool_calls": rep.tool_calls}
            )

            done = False
            for tc in rep.tool_calls:
                fn = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}

                if fn == "finish":
                    answer = args.get("answer", args)
                    done = True
                    result: Any = {"ok": True}
                else:
                    tool_calls += 1
                    result = _dispatch(adapter, fn, args)

                if verbose:
                    print(f"    [{steps}] {fn}({_clip(args)}) -> {_clip(result)}")

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": json.dumps(result)[:4000],
                    }
                )
            if done:
                break
    except Exception as e:  # noqa: BLE001 - a failed run is data, not a crash
        err = f"{type(e).__name__}: {e}"

    if answer is None and err is None:
        err = err or "no answer (step limit reached)"

    checked: TaskResult = (
        task.check(state, answer if isinstance(answer, dict) else {})
        if answer is not None
        else TaskResult(False, err or "no answer")
    )

    res = RunResult(
        run_id=run_id,
        task_id=task.id,
        success=bool(checked.ok),
        detail=checked.detail,
        expected=checked.expected,
        got=checked.got,
        steps=steps,
        tool_calls=tool_calls,
        anomalies=[a.to_dict() for a in adapter.anomalies],
        signatures=sorted({a.signature() for a in adapter.anomalies}),
        wall_s=round(time.time() - t0, 2),
        prompt_tokens=executor.usage.prompt_tokens - p0,
        completion_tokens=executor.usage.completion_tokens - c0,
        belief_context_tokens=ctx_tokens,
        beliefs_active=n_active,
        memory_enabled=memory,
        answer=answer,
        error=err,
    )
    adapter.close()
    return res


def _dispatch(a: LabAdapter, fn: str, args: dict[str, Any]) -> Any:
    try:
        if fn == "search":
            sc, body = a.search(**args)
        elif fn == "create_item":
            sc, body = a.create(**args)
        elif fn == "get_item":
            sc, body = a.get(args.get("item_id", ""))
        elif fn == "update_item":
            sc, body = a.update(args.get("item_id", ""), **(args.get("fields") or {}))
        elif fn == "bulk_create":
            sc, body = a.bulk_create(args.get("items") or [])
        else:
            return {"error": f"unknown tool {fn}"}
        return {"status": sc, "body": body}
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


def _clip(v: Any, n: int = 110) -> str:
    try:
        s = json.dumps(v)
    except Exception:
        s = str(v)
    return s if len(s) <= n else s[:n] + "…"
