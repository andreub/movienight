"""The OpenAI-compatible chat backend (MOVIENIGHT_AI_PROVIDER=openai), with a fake client. No network."""
import json
from types import SimpleNamespace as NS

import httpx
import openai
import pytest

from movienight import chat, chat_backends
from test_chat import U, clock, mid, names, parse_sse, rows  # noqa: F401 - clock is a fixture

BASE = "https://llm.example.test/openai/v1"
KEY = "sk-test-SECRET-123"
MODEL = "vendor/test-model-1"


# ------------------------------------------------------------------ fake OpenAI-compatible client

def chunk(content=None, tools=None, finish=None, usage=None, model=MODEL, refusal=None, choices=True):
    tcs = [NS(index=i, id=cid, function=NS(name=name, arguments=args)) for i, cid, name, args in (tools or [])]
    ch = [NS(delta=NS(content=content, tool_calls=tcs or None, refusal=refusal), finish_reason=finish)] if choices else []
    return NS(choices=ch, usage=usage, model=model)


def usage(p, c, cached=None):
    return NS(prompt_tokens=p, completion_tokens=c, total_tokens=p + c,
              prompt_tokens_details=None if cached is None else NS(cached_tokens=cached))


def text_turn(*parts, finish="stop", u=None):
    return [chunk(content=t) for t in parts] + [chunk(finish=finish)] + ([chunk(usage=u, choices=False)] if u else [])


def tool_turn(*calls, u=None, split=True, finish="tool_calls"):
    """calls: (name, args dict | raw str). Arguments are streamed in two fragments, id/name only on the first."""
    out = []
    for i, (name, args) in enumerate(calls):
        raw = args if isinstance(args, str) else json.dumps(args)
        cut = len(raw) // 2 if split else len(raw)
        out.append(chunk(tools=[(i, f"call_{i}_{name}", name, raw[:cut])]))
        if raw[cut:]:
            out.append(chunk(tools=[(i, None, None, raw[cut:])]))
    out.append(chunk(finish=finish))
    if u:
        out.append(chunk(usage=u, choices=False))
    return out


class FakeStream:
    def __init__(self, chunks):
        self.chunks = chunks

    def __aiter__(self):
        async def gen():
            for c in self.chunks:
                yield c
        return gen()


class FakeOpenAI:
    def __init__(self, turns):
        self.turns, self.calls, self.closed = list(turns), [], False
        self.chat = NS(completions=NS(create=self._create))

    async def _create(self, **kw):
        self.calls.append(json.loads(json.dumps(kw)))
        t = self.turns.pop(0) if len(self.turns) > 1 else self.turns[0]
        if isinstance(t, Exception):
            raise t
        return FakeStream(t)

    async def close(self):
        self.closed = True


@pytest.fixture
def oa(client, monkeypatch):
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "MOVIENIGHT_AI_PRICE_INPUT", "MOVIENIGHT_AI_PRICE_OUTPUT",
              "MOVIENIGHT_AI_MAX_TOKENS", "MOVIENIGHT_AI_MONTHLY_BUDGET_USD"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MOVIENIGHT_AI_PROVIDER", "openai")
    monkeypatch.setenv("MOVIENIGHT_AI_BASE_URL", BASE)
    monkeypatch.setenv("MOVIENIGHT_AI_API_KEY", KEY)
    monkeypatch.setenv("MOVIENIGHT_AI_MODEL", MODEL)
    if not any(getattr(r, "path", "") == "/api/chat" for r in client.app.routes):
        client.app.include_router(chat.router)
    return client


def script(monkeypatch, turns):
    fake = FakeOpenAI(turns)
    monkeypatch.setattr(chat, "make_openai_client", lambda: fake)

    def no_anthropic():
        raise AssertionError("the anthropic client must not be built for the openai provider")
    monkeypatch.setattr(chat, "make_client", no_anthropic)
    return fake


def post(c, messages=U):
    r = c.post("/api/chat", json={"messages": messages})
    return r, parse_sse(r.text) if r.status_code == 200 else []


# ------------------------------------------------------------------ conversations

def test_text_only_answer_and_request_shape(oa, monkeypatch):
    fake = script(monkeypatch, [text_turn("Hello ", "there.")])
    r, ev = post(oa, [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "Hey!"},
                      {"role": "user", "content": "anything?"}])
    assert names(ev) == ["text", "text", "done"] and ev[0][1]["delta"] == "Hello "
    c0 = fake.calls[0]
    assert c0["model"] == MODEL and c0["stream"] is True and c0["stream_options"] == {"include_usage": True}
    assert c0["tool_choice"] == "auto" and c0["max_completion_tokens"] == chat.OPENAI_MAX_TOKENS
    assert c0["messages"][0] == {"role": "system", "content": chat.SYSTEM_PROMPT}
    assert c0["messages"][1:] == [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "Hey!"},
                                  {"role": "user", "content": "anything?"}]
    assert [t["function"]["name"] for t in c0["tools"]] == ["search_library", "get_movie", "get_taste_profile", "recommend"]
    t0 = c0["tools"][1]
    assert t0["type"] == "function" and set(t0["function"]) == {"name", "description", "parameters"}
    assert t0["function"]["parameters"] == chat.TOOLS[1]["input_schema"]
    sent = json.dumps(fake.calls)
    assert "eager_input_streaming" not in sent and "cache_control" not in sent and "parallel_tool_calls" not in sent
    assert KEY not in sent and fake.closed


def test_split_tool_call_then_answer(oa, monkeypatch):
    crime = mid(oa, "Crime Two")
    fake = script(monkeypatch, [
        tool_turn(("search_library", {"genres": ["thriller"], "limit": 5})),
        [chunk(content="Try this. ")] + tool_turn(("recommend", {"picks": [{"movie_id": crime, "pitch": "Heist."}]})),
        text_turn("Enjoy."),
    ])
    r, ev = post(oa)
    assert names(ev) == ["tool", "text", "tool", "movies", "text", "done"]
    assert ev[0][1] == {"name": "search_library", "label": "Searching your library…"}
    assert ev[3][1]["items"][0]["title"] == "Crime Two" and ev[3][1]["items"][0]["pitch"] == "Heist."
    m1 = fake.calls[1]["messages"]
    assistant, result = m1[-2], m1[-1]
    assert assistant["role"] == "assistant" and assistant["content"] is None
    assert assistant["tool_calls"] == [{"id": "call_0_search_library", "type": "function", "function": {
        "name": "search_library", "arguments": json.dumps({"genres": ["thriller"], "limit": 5})}}]
    assert result["role"] == "tool" and result["tool_call_id"] == "call_0_search_library"
    assert "Crime Two" in {x["title"] for x in json.loads(result["content"])["results"]}
    # text that came with the tool call is echoed back
    assert fake.calls[2]["messages"][-2]["content"] == "Try this. "


def test_two_parallel_tool_calls(oa, monkeypatch):
    fake = script(monkeypatch, [
        tool_turn(("get_taste_profile", ""), ("get_movie", {"id": mid(oa, "Rom Com")})),
        text_turn("ok"),
    ])
    r, ev = post(oa)
    assert names(ev) == ["tool", "tool", "text", "done"]
    msgs = fake.calls[1]["messages"]
    assert len(msgs[-3]["tool_calls"]) == 2
    assert [m["role"] for m in msgs[-2:]] == ["tool", "tool"]
    assert "genres" in json.loads(msgs[-2]["content"])            # empty arguments = {}
    assert json.loads(msgs[-1]["content"])["title"] == "Rom Com"


def test_invalid_json_arguments_get_error_result_and_model_retries(oa, monkeypatch):
    fake = script(monkeypatch, [
        tool_turn(("get_movie", '{"id": 12'), split=False),
        tool_turn(("get_movie", {"id": mid(oa, "Rom Com")})),
        text_turn("fine"),
    ])
    r, ev = post(oa)
    assert names(ev) == ["tool", "tool", "text", "done"]
    first = fake.calls[1]["messages"]
    assert first[-2]["tool_calls"][0]["function"]["arguments"] == "{}"       # never echo broken JSON
    assert first[-1]["content"].startswith("Error: ") and "not valid JSON" in first[-1]["content"]
    assert json.loads(fake.calls[2]["messages"][-1]["content"])["title"] == "Rom Com"


def test_tool_error_is_marked_in_text(oa, monkeypatch):
    fake = script(monkeypatch, [tool_turn(("recommend", {"picks": [{"movie_id": 99999, "pitch": "x"}]})), text_turn("ok")])
    post(oa)
    res = fake.calls[1]["messages"][-1]
    assert res["role"] == "tool" and res["content"].startswith("Error: ") and "99999" in res["content"]


def test_tool_calls_with_stop_finish_reason_still_run(oa, monkeypatch):
    fake = script(monkeypatch, [tool_turn(("get_taste_profile", "{}"), finish="stop"), text_turn("ok")])
    r, ev = post(oa)
    assert names(ev) == ["tool", "text", "done"] and len(fake.calls) == 2


@pytest.mark.parametrize("finish", ["length", "content_filter"])
def test_length_and_content_filter_stops(oa, monkeypatch, finish):
    script(monkeypatch, [text_turn("part", finish=finish)])
    r, ev = post(oa)
    assert names(ev) == ["text", "error", "done"]
    assert ("cut off" if finish == "length" else "can't help") in ev[1][1]["detail"]


def test_refusal_delta_is_refusal(oa, monkeypatch):
    script(monkeypatch, [[chunk(refusal="No."), chunk(finish="stop")]])
    r, ev = post(oa)
    assert names(ev) == ["error", "done"] and "can't help" in ev[0][1]["detail"]


def test_loop_cap(oa, monkeypatch):
    fake = script(monkeypatch, [tool_turn(("get_taste_profile", {}))])
    r, ev = post(oa)
    assert len(fake.calls) == chat.MAX_TURNS and names(ev)[-2:] == ["error", "done"]


# ------------------------------------------------------------------ usage and cost

def test_usage_mapping_with_cached_tokens(oa, monkeypatch, clock):
    script(monkeypatch, [
        tool_turn(("get_taste_profile", {}), u=usage(1200, 80, cached=1000)),
        text_turn("ok", u=usage(1500, 40)),
    ])
    post(oa)
    (row,) = rows(oa)
    assert (row["model"], row["input"], row["output"], row["cache_write"], row["cache_read"], row["requests"],
            row["questions"]) == (MODEL, 200 + 1500, 120, 0, 1000, 2, 1)
    u = oa.get("/api/chat/status").json()["usage"]
    assert u["spent_usd"] == 0 and u["tokens"] == 2820 and u["questions"] == 1 and not u["exhausted"]


def test_missing_usage_counts_request_only(oa, monkeypatch, clock):
    script(monkeypatch, [text_turn("ok")])
    post(oa)
    (row,) = rows(oa)
    assert (row["input"], row["output"], row["requests"], row["questions"]) == (0, 0, 1, 1)


def test_usage_on_a_chunk_with_choices_is_accepted(oa, monkeypatch, clock):
    script(monkeypatch, [[chunk(content="hi"), chunk(finish="stop", usage=usage(10, 5))]])
    post(oa)
    assert (rows(oa)[0]["input"], rows(oa)[0]["output"]) == (10, 5)


def test_served_model_is_recorded(oa, monkeypatch, clock):
    script(monkeypatch, [[chunk(content="hi", model="vendor/served-2"), chunk(finish="stop", model="vendor/served-2")]])
    post(oa)
    assert rows(oa)[0]["model"] == "vendor/served-2"


def test_price_zero_never_exhausts_budget(oa, monkeypatch, clock):
    monkeypatch.setenv("MOVIENIGHT_AI_MONTHLY_BUDGET_USD", "0.01")
    fake = script(monkeypatch, [text_turn("hi", u=usage(5_000_000, 5_000_000))])
    post(oa)
    r, ev = post(oa)
    assert len(fake.calls) == 2 and ev[0][1]["delta"] == "hi"
    u = oa.get("/api/chat/status").json()["usage"]
    assert u["spent_usd"] == 0 and u["exhausted"] is False


def test_custom_prices_and_budget(oa, monkeypatch, clock):
    monkeypatch.setenv("MOVIENIGHT_AI_PRICE_INPUT", "0.5")
    monkeypatch.setenv("MOVIENIGHT_AI_PRICE_OUTPUT", "2")
    monkeypatch.setenv("MOVIENIGHT_AI_MONTHLY_BUDGET_USD", "1")
    fake = script(monkeypatch, [text_turn("hi", u=usage(1_000_000, 300_000, cached=200_000))])
    post(oa)
    u = oa.get("/api/chat/status").json()["usage"]
    assert u["spent_usd"] == pytest.approx(0.5 + 0.6) and u["exhausted"] is True    # cached at full input price
    r, ev = post(oa)
    assert len(fake.calls) == 1 and "budget for this month" in ev[0][1]["delta"]


def test_cost_by_provider(monkeypatch):
    monkeypatch.setenv("MOVIENIGHT_AI_PROVIDER", "openai")
    monkeypatch.setenv("MOVIENIGHT_AI_PRICE_INPUT", "1")
    monkeypatch.setenv("MOVIENIGHT_AI_PRICE_OUTPUT", "bad")
    assert chat.row_cost("vendor/x", 1_000_000, 1_000_000) == pytest.approx(1.0)     # invalid price = 0
    assert chat.row_cost("claude-opus-5-5", 1_000_000, 1_000_000) == pytest.approx(24.0)  # old Claude rows keep PRICES
    monkeypatch.setenv("MOVIENIGHT_AI_PROVIDER", "anthropic")
    assert chat.row_cost("vendor/x", 1_000_000, 1_000_000) == pytest.approx(48.0)    # unchanged anthropic rule


# ------------------------------------------------------------------ configuration and status

@pytest.mark.parametrize("env, enabled, provider, problem", [
    ({}, False, "anthropic", "ANTHROPIC_API_KEY"),
    ({"ANTHROPIC_API_KEY": "k"}, True, "anthropic", None),
    ({"MOVIENIGHT_AI_BASE_URL": BASE, "MOVIENIGHT_AI_API_KEY": KEY, "MOVIENIGHT_AI_MODEL": MODEL}, True, "openai", None),
    ({"MOVIENIGHT_AI_PROVIDER": "openai", "MOVIENIGHT_AI_API_KEY": KEY, "MOVIENIGHT_AI_MODEL": MODEL},
     False, "openai", "MOVIENIGHT_AI_BASE_URL"),
    ({"MOVIENIGHT_AI_PROVIDER": "openai", "MOVIENIGHT_AI_BASE_URL": BASE, "MOVIENIGHT_AI_MODEL": MODEL,
      "ANTHROPIC_API_KEY": "k"}, False, "openai", "MOVIENIGHT_AI_API_KEY"),
    ({"MOVIENIGHT_AI_PROVIDER": "OpenAI", "MOVIENIGHT_AI_BASE_URL": BASE, "MOVIENIGHT_AI_API_KEY": KEY},
     False, "openai", "MOVIENIGHT_AI_MODEL"),
    ({"MOVIENIGHT_AI_PROVIDER": "anthropic", "MOVIENIGHT_AI_BASE_URL": BASE, "ANTHROPIC_API_KEY": "k"},
     True, "anthropic", None),
    ({"MOVIENIGHT_AI_PROVIDER": "gemini", "ANTHROPIC_API_KEY": "k", "MOVIENIGHT_AI_API_KEY": KEY},
     False, "gemini", "MOVIENIGHT_AI_PROVIDER"),
])
def test_enabled_matrix_and_status(client, monkeypatch, env, enabled, provider, problem):
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "MOVIENIGHT_AI_PROVIDER", "MOVIENIGHT_AI_BASE_URL",
              "MOVIENIGHT_AI_API_KEY", "MOVIENIGHT_AI_MODEL"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    if not any(getattr(r, "path", "") == "/api/chat/status" for r in client.app.routes):
        client.app.include_router(chat.router)
    assert chat.chat_enabled() is enabled
    st = client.get("/api/chat/status").json()
    assert st["enabled"] is enabled and st["provider"] == provider
    assert (st["problem"] is None) if problem is None else (problem in st["problem"])
    assert KEY not in json.dumps(st)
    if not enabled:
        r = client.post("/api/chat", json={"messages": U})
        assert r.status_code == 503 and "not configured" in r.json()["detail"] and KEY not in r.text


def test_openai_model_has_no_default(monkeypatch):
    monkeypatch.setenv("MOVIENIGHT_AI_PROVIDER", "openai")
    monkeypatch.delenv("MOVIENIGHT_AI_MODEL", raising=False)
    assert chat.model_name() == ""
    monkeypatch.setenv("MOVIENIGHT_AI_PROVIDER", "anthropic")
    assert chat.model_name() == chat.DEFAULT_MODEL


def test_max_tokens_override(oa, monkeypatch):
    for raw, want in (("4096", 4096), ("12", chat.OPENAI_MAX_TOKENS), ("x", chat.OPENAI_MAX_TOKENS)):
        monkeypatch.setenv("MOVIENIGHT_AI_MAX_TOKENS", raw)
        fake = script(monkeypatch, [text_turn("hi")])
        post(oa)
        assert fake.calls[0]["max_completion_tokens"] == want


def test_real_client_factory_settings(monkeypatch):
    monkeypatch.setenv("MOVIENIGHT_AI_BASE_URL", BASE)
    monkeypatch.setenv("MOVIENIGHT_AI_API_KEY", KEY)
    c = chat.make_openai_client()          # no request is made
    assert str(c.base_url).rstrip("/") == BASE and c.max_retries == 2 and c.timeout == chat.API_TIMEOUT_S
    assert c.default_headers["User-Agent"] == chat.USER_AGENT and "MovieNight" in chat.USER_AGENT


# ------------------------------------------------------------------ errors

def _status_error(cls, code):
    req = httpx.Request("POST", BASE + "/chat/completions")
    return cls(f"provider body with {KEY}", response=httpx.Response(code, request=req), body={"error": KEY})


@pytest.mark.parametrize("exc, needle", [
    (lambda: _status_error(openai.AuthenticationError, 401), "MOVIENIGHT_AI_API_KEY"),
    (lambda: _status_error(openai.PermissionDeniedError, 403), "MOVIENIGHT_AI_MODEL"),
    (lambda: _status_error(openai.NotFoundError, 404), "MOVIENIGHT_AI_BASE_URL"),
    (lambda: _status_error(openai.RateLimitError, 429), "limit was reached"),
    (lambda: _status_error(openai.BadRequestError, 400), "rejected the request"),
    (lambda: _status_error(openai.APIStatusError, 413), "Start a new chat"),
    (lambda: _status_error(openai.InternalServerError, 500), "had a problem"),
    (lambda: openai.APITimeoutError(request=httpx.Request("POST", BASE)), "too long"),
    (lambda: openai.APIConnectionError(request=httpx.Request("POST", BASE)), "Could not reach"),
])
def test_openai_errors_are_friendly(oa, monkeypatch, exc, needle):
    script(monkeypatch, [exc()])
    r, ev = post(oa)
    assert names(ev) == ["error", "done"] and needle in ev[0][1]["detail"]
    assert KEY not in r.text and "provider body" not in r.text


def test_error_mid_stream_after_text(oa, monkeypatch):
    class Broken(FakeStream):
        def __aiter__(self):
            async def gen():
                yield chunk(content="Let me ")
                raise openai.APIConnectionError(request=httpx.Request("POST", BASE))
            return gen()

    fake = FakeOpenAI([[]])

    async def create(**kw):
        return Broken([])
    fake.chat = NS(completions=NS(create=create))
    monkeypatch.setattr(chat, "make_openai_client", lambda: fake)
    r, ev = post(oa)
    assert names(ev) == ["text", "error", "done"] and fake.closed


# ------------------------------------------------------------------ backend units

def test_parse_args():
    assert chat_backends._parse_args("") == {} and chat_backends._parse_args("  ") == {}
    assert chat_backends._parse_args('{"a": 1}') == {"a": 1}
    assert chat_backends._parse_args("{nope") is None
    assert chat_backends._parse_args("[1]") == [1]         # run_tool answers "must be a JSON object"


def test_openai_tools_conversion():
    out = chat_backends.openai_tools(chat.TOOLS)
    assert [t["function"]["name"] for t in out] == [t["name"] for t in chat.TOOLS]
    assert all(set(t) == {"type", "function"} for t in out)
