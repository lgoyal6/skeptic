"""
Two-tier model access.

EXECUTOR does the volume: every task run, every tool call. Cheap and fast.
REFLECTOR is called rarely: forming hypotheses, designing probes, distilling
beliefs. It gets the better model because it does the thinking that matters.

Reporting the split is the honest answer to "cost-effectiveness and speed":
the expensive model is invoked a handful of times per run, not per step.

GLM-4.7-Flash is a reasoning model. Its visible answer arrives in `content`
while `reasoning` carries the chain, and a small max_tokens starves the
answer entirely -- the model spends the whole budget thinking and returns
content=null with finish_reason=length. Anything under ~300 tokens is a trap.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

MIN_SAFE_TOKENS = 512  # below this a reasoning model can starve its own answer


def load_env(path: str | Path = ".env") -> None:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


@dataclass
class Usage:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    wall_s: float = 0.0
    by_role: dict[str, dict[str, int]] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def add(self, role: str, prompt: int, completion: int, secs: float) -> None:
        self.calls += 1
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        self.wall_s += secs
        r = self.by_role.setdefault(role, {"calls": 0, "prompt": 0, "completion": 0})
        r["calls"] += 1
        r["prompt"] += prompt
        r["completion"] += completion

    def to_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "wall_s": round(self.wall_s, 2),
            "by_role": self.by_role,
        }


@dataclass
class Reply:
    content: str | None
    reasoning: str | None
    tool_calls: list[dict[str, Any]]
    finish_reason: str
    prompt_tokens: int
    completion_tokens: int

    def first_tool(self) -> tuple[str, dict[str, Any]] | None:
        if not self.tool_calls:
            return None
        fn = self.tool_calls[0]["function"]
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}
        return fn["name"], args


class LLM:
    """One OpenAI-compatible endpoint, one model, one role label."""

    def __init__(
        self,
        role: str,
        model: str,
        base_url: str,
        api_key: str,
        usage: Usage | None = None,
        # Measured completions on this endpoint run to 370s under load while
        # the same call takes 34s idle. A 120s ceiling was cutting off work
        # that was still coming, and reporting it as a transport failure.
        timeout: float = 480.0,
    ) -> None:
        self.role = role
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.usage = usage if usage is not None else Usage()
        self.client = httpx.Client(timeout=timeout)

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 1200,
        temperature: float = 0.2,
        response_json: bool = False,
        retries: int = 4,
    ) -> Reply:
        if max_tokens < MIN_SAFE_TOKENS:
            max_tokens = MIN_SAFE_TOKENS

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if response_json:
            payload["response_format"] = {"type": "json_object"}

        last_err: Exception | None = None
        for attempt in range(retries):
            t0 = time.time()
            try:
                r = self.client.post(
                    f"{self.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "content-type": "application/json",
                    },
                    json=payload,
                )
            except Exception as e:  # transport
                last_err = e
                time.sleep(1.5 * (attempt + 1))
                continue

            if r.status_code in (429, 500, 502, 503, 504):
                last_err = RuntimeError(f"{r.status_code}: {r.text[:200]}")
                time.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code != 200:
                raise RuntimeError(f"{self.role} llm {r.status_code}: {r.text[:400]}")

            d = r.json()
            ch = d["choices"][0]
            m = ch["message"]
            u = d.get("usage") or {}
            pt = int(u.get("prompt_tokens") or 0)
            ct = int(u.get("completion_tokens") or 0)
            self.usage.add(self.role, pt, ct, time.time() - t0)

            return Reply(
                content=m.get("content"),
                reasoning=m.get("reasoning"),
                tool_calls=m.get("tool_calls") or [],
                finish_reason=ch.get("finish_reason", ""),
                prompt_tokens=pt,
                completion_tokens=ct,
            )

        raise RuntimeError(f"{self.role} llm failed after {retries} attempts: {last_err}")

    def json_chat(
        self, messages: list[dict[str, Any]], max_tokens: int = 4000, retries: int = 3
    ) -> dict[str, Any]:
        """Chat that must return a JSON object.

        A reasoning model can spend its entire completion budget thinking and
        return content=null with finish_reason=length -- the answer starved
        before it was written. That is a budget problem, not a formatting
        problem, so escalate the budget rather than scolding the model.
        """
        budget = max_tokens
        convo = list(messages)
        for attempt in range(retries):
            rep = self.chat(convo, max_tokens=budget, response_json=True)
            text = (rep.content or "").strip()
            parsed = _extract_json(text)
            if parsed is not None:
                return parsed

            if rep.finish_reason == "length" or not text:
                budget = min(budget * 2, 16000)
                continue

            convo = convo + [
                {"role": "assistant", "content": text[:500]},
                {"role": "user", "content": "That was not valid JSON. Reply with a single JSON object and nothing else."},
            ]
        raise ValueError(
            f"{self.role} did not return JSON after {retries} attempts (last budget {budget})"
        )


def _extract_json(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    try:
        v = json.loads(text)
        return v if isinstance(v, dict) else None
    except json.JSONDecodeError:
        pass
    if "```" in text:
        for block in text.split("```")[1::2]:
            block = block.removeprefix("json").strip()
            try:
                v = json.loads(block)
                if isinstance(v, dict):
                    return v
            except json.JSONDecodeError:
                continue
    start, depth = text.find("{"), 0
    if start == -1:
        return None
    for i, c in enumerate(text[start:], start):
        depth += (c == "{") - (c == "}")
        if depth == 0:
            try:
                v = json.loads(text[start : i + 1])
                return v if isinstance(v, dict) else None
            except json.JSONDecodeError:
                return None
    return None


def build(usage: Usage | None = None) -> tuple[LLM, LLM, Usage]:
    """(executor, reflector, shared usage) from the environment.

    Reflector prefers GPT-5 Nano when AI Grants access has landed, then
    Anthropic, then falls back to the executor model so the loop still runs.
    """
    load_env()
    u = usage if usage is not None else Usage()

    tmx = os.environ.get("TENSORMUX_API_KEY")
    if not tmx:
        raise RuntimeError("TENSORMUX_API_KEY missing; put it in .env")

    executor = LLM(
        role="executor",
        model=os.environ.get("EXECUTOR_MODEL", "glm-4-7-flash"),
        base_url=os.environ.get("TENSORMUX_BASE_URL", "https://api.tensormux.com/v1"),
        api_key=tmx,
        usage=u,
    )

    nano = os.environ.get("AIGRANTS_API_KEY")
    if nano:
        reflector = LLM(
            role="reflector",
            model=os.environ.get("REFLECTOR_MODEL", "gpt-5-nano"),
            base_url=os.environ.get("AIGRANTS_BASE_URL", "https://api.openai.com/v1"),
            api_key=nano,
            usage=u,
        )
    else:
        reflector = LLM(
            role="reflector",
            model=os.environ.get("REFLECTOR_FALLBACK_MODEL", "glm-4-7-flash"),
            base_url=os.environ.get("TENSORMUX_BASE_URL", "https://api.tensormux.com/v1"),
            api_key=tmx,
            usage=u,
        )
    return executor, reflector, u
