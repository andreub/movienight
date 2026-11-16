"""Model backends for the chat loop in `chat.py`: Anthropic and any OpenAI-compatible endpoint.

A backend owns its native message list and hides the provider's wire format from the loop:

    backend.start(system, tools, history)      tools in the Anthropic shape of chat.TOOLS
    async for item in backend.turn():          str = text delta; the last item is a Turn
    backend.add_tool_results([(call_id, content, is_error), ...])
    await backend.close()

Turn.stop is one of "end", "tools", "length", "refusal", "pause" (re-issue the turn as is).
Nothing here logs or returns credentials or raw provider bodies.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

FALLBACK_BETA = "server-side-fallback-2026-07-01"


@dataclass
class Usage:
    input: int = 0
    output: int = 0
    cache_write: int = 0
    cache_read: int = 0


@dataclass
class ToolCall:
    id: str
    name: str
    args: Any                     # dict (or whatever the model sent); None = arguments were not valid JSON
    raw: Any = None               # provider-native block / arguments string


@dataclass
class Turn:
    stop: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage | None = None
    model: str | None = None


def _n(v) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else 0


# =========================================================================== Anthropic

def _supports_modern(model: str) -> bool:
    return model.startswith(("claude-opus-5", "claude-fable-5", "claude-mythos-5", "claude-sonnet-5-5"))


def _request_kwargs(model: str, max_tokens: int, system: str, tools: list, messages: list) -> dict:
    kw: dict = dict(model=model, max_tokens=max_tokens, system=system, tools=tools, messages=messages,
                    cache_control={"type": "ephemeral"})
    if _supports_modern(model):
        kw.update(thinking={"type": "adaptive"}, output_config={"effort": "medium"},
                  betas=[FALLBACK_BETA], fallbacks="default")
    return kw


def _echo_content(content: list) -> list:
    """Assistant content to send back. After a mid-output fallback, drop thinking/tool_use before the boundary."""
    last = max((i for i, b in enumerate(content) if getattr(b, "type", None) == "fallback"), default=None)
    if last is None:
        return list(content)
    return [b for i, b in enumerate(content)
            if i >= last or getattr(b, "type", None) not in ("thinking", "redacted_thinking", "tool_use")]


_ANTHROPIC_STOPS = {"tool_use": "tools", "max_tokens": "length", "refusal": "refusal", "pause_turn": "pause"}


class AnthropicBackend:
    """The Messages API with prompt caching, adaptive thinking and server-side fallbacks (modern models)."""

    def __init__(self, client, model: str, max_tokens: int):
        self.client, self.model, self.max_tokens = client, model, max_tokens
        self.system, self.tools, self.messages = "", [], []
        self._content: list = []

    def start(self, system: str, tools: list, history: list[dict]) -> None:
        self.system, self.tools, self.messages = system, tools, list(history)

    async def turn(self) -> AsyncIterator[str | Turn]:
        # A ValueError here (tool input JSON the SDK could not parse) propagates: the loop re-issues the turn.
        kw = _request_kwargs(self.model, self.max_tokens, self.system, self.tools, self.messages)
        async with self.client.beta.messages.stream(**kw) as stream:
            async for event in stream:
                if event.type == "text" and event.text:
                    yield event.text
            msg = await stream.get_final_message()
        content = _echo_content(msg.content)
        stop = _ANTHROPIC_STOPS.get(msg.stop_reason, "end")
        if stop == "pause":
            self.messages.append({"role": "assistant", "content": content})
        self._content = content
        calls = [ToolCall(b.id, b.name, b.input, b) for b in content if b.type == "tool_use"]
        if stop == "tools" and not calls:
            stop = "end"
        u = getattr(msg, "usage", None)
        usage = None if u is None else Usage(
            _n(getattr(u, "input_tokens", 0)), _n(getattr(u, "output_tokens", 0)),
            _n(getattr(u, "cache_creation_input_tokens", 0)), _n(getattr(u, "cache_read_input_tokens", 0)))
        served = getattr(msg, "model", None)
        yield Turn(stop, calls, usage, served if isinstance(served, str) and served else None)

    def add_tool_results(self, results: list[tuple[str, str, bool]]) -> None:
        out = []
        for call_id, content, is_error in results:
            r = {"type": "tool_result", "tool_use_id": call_id, "content": content}
            if is_error:
                r["is_error"] = True
            out.append(r)
        self.messages.append({"role": "assistant", "content": self._content})
        self.messages.append({"role": "user", "content": out})

    async def close(self) -> None:
        await self.client.close()


# =========================================================================== OpenAI-compatible

def openai_tools(tools: list[dict]) -> list[dict]:
    """chat.TOOLS (Anthropic shape) -> chat-completions function tools. Drops Anthropic-only keys."""
    return [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                              "parameters": t["input_schema"]}} for t in tools]


def _parse_args(raw: str) -> Any:
    """Arguments string -> dict. Empty means no arguments ({}). Invalid JSON -> None (never raises)."""
    if not (raw or "").strip():
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        return None


_OPENAI_STOPS = {"stop": "end", "tool_calls": "tools", "function_call": "tools", "length": "length",
                 "content_filter": "refusal"}


class OpenAIBackend:
    """Any endpoint that speaks OpenAI chat completions with streaming and function tools (Groq, OpenAI, …).

    Sends only widely supported fields: no parallel_tool_calls, no logprobs, no `name` on messages."""

    def __init__(self, client, model: str, max_tokens: int):
        self.client, self.model, self.max_tokens = client, model, max_tokens
        self.tools: list = []
        self.messages: list = []
        self._calls: list[ToolCall] = []

    def start(self, system: str, tools: list, history: list[dict]) -> None:
        self.tools = openai_tools(tools)
        self.messages = [{"role": "system", "content": system}, *history]

    async def turn(self) -> AsyncIterator[str | Turn]:
        stream = await self.client.chat.completions.create(
            model=self.model, messages=self.messages, tools=self.tools, tool_choice="auto",
            max_completion_tokens=self.max_tokens, stream=True, stream_options={"include_usage": True})
        text: list[str] = []
        refusal = False
        slots: dict[Any, dict] = {}          # tool calls by stream index: {id, name, args: [fragments]}
        order: list = []
        finish = None
        usage = None
        served = None
        async for chunk in stream:
            served = getattr(chunk, "model", None) or served
            if getattr(chunk, "usage", None) is not None:
                usage = chunk.usage           # standard: a final chunk with empty choices; any chunk is accepted
            for choice in getattr(chunk, "choices", None) or []:
                if choice.finish_reason:
                    finish = choice.finish_reason
                delta = choice.delta
                if delta is None:
                    continue
                if getattr(delta, "refusal", None):
                    refusal = True
                if delta.content:
                    text.append(delta.content)
                    yield delta.content
                for tc in delta.tool_calls or []:
                    key = tc.index if tc.index is not None else (tc.id or (order[-1] if order else 0))
                    slot = slots.get(key)
                    if slot is None:
                        slot = slots[key] = {"id": None, "name": "", "args": []}
                        order.append(key)
                    if tc.id:
                        slot["id"] = tc.id
                    fn = tc.function
                    if fn is not None:
                        if fn.name:
                            slot["name"] += fn.name if not slot["name"].endswith(fn.name) else ""
                        if fn.arguments:
                            slot["args"].append(fn.arguments)

        calls: list[ToolCall] = []
        for i, key in enumerate(order):
            s = slots[key]
            raw = "".join(s["args"])
            calls.append(ToolCall(s["id"] or f"call_{i}", s["name"], _parse_args(raw), raw))
        self._calls = calls

        if refusal or finish == "content_filter":
            stop = "refusal"
        elif finish == "length":
            stop = "length"
        elif calls:
            stop = "tools"                    # some servers report "stop" alongside tool calls
        else:
            stop = _OPENAI_STOPS.get(finish or "stop", "end")
            if stop == "tools":
                stop = "end"
        if stop == "tools":
            self.messages.append({
                "role": "assistant", "content": "".join(text) or None,
                "tool_calls": [{"id": c.id, "type": "function", "function": {
                    # echo invalid arguments as {} so a strict server does not reject the whole history
                    "name": c.name, "arguments": c.raw if isinstance(c.args, dict) else "{}"}} for c in calls],
            })
        yield Turn(stop, calls, _openai_usage(usage), served if isinstance(served, str) and served else None)

    def add_tool_results(self, results: list[tuple[str, str, bool]]) -> None:
        for call_id, content, is_error in results:
            # chat completions has no is_error flag: the text itself says what went wrong
            self.messages.append({"role": "tool", "tool_call_id": call_id,
                                  "content": f"Error: {content}" if is_error else content})

    async def close(self) -> None:
        await self.client.close()


def _openai_usage(u) -> Usage | None:
    if u is None:
        return None
    details = getattr(u, "prompt_tokens_details", None)
    cached = _n(getattr(details, "cached_tokens", 0)) if details is not None else 0
    prompt = _n(getattr(u, "prompt_tokens", 0))
    return Usage(input=max(0, prompt - cached), output=_n(getattr(u, "completion_tokens", 0)), cache_read=cached)
