import json
from types import SimpleNamespace as NS

import pytest

from movienight import chat


# ------------------------------------------------------------------ fake Anthropic client

class FakeStream:
    def __init__(self, turn):
        self.turn = turn

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def __aiter__(self):
        async def gen():
            for t in self.turn.get("text", []):
                yield NS(type="text", text=t)
        return gen()

    async def get_final_message(self):
        t = self.turn
        content = []
        if t.get("text"):
            content.append(NS(type="text", text="".join(t["text"])))
        for i, (name, inp) in enumerate(t.get("tools", [])):
            content.append(NS(type="tool_use", id=f"tu_{id(t)}_{i}", name=name, input=inp))
        stop = t.get("stop") or ("tool_use" if t.get("tools") else "end_turn")
        return NS(stop_reason=stop, content=content)


class FakeClient:
    def __init__(self, turns):
        self.turns, self.calls = list(turns), []
        self.beta = NS(messages=NS(stream=self._stream))

    def _stream(self, **kw):
        self.calls.append(json.loads(json.dumps({k: v for k, v in kw.items() if k != "messages"}, default=str))
                          | {"messages": list(kw["messages"])})
        return FakeStream(self.turns.pop(0) if len(self.turns) > 1 else self.turns[0])

    async def close(self):
        pass


@pytest.fixture
def chat_client(client, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.delenv("MOVIENIGHT_AI_MODEL", raising=False)
    if not any(getattr(r, "path", "") == "/api/chat" for r in client.app.routes):
        client.app.include_router(chat.router)
    return client


def mid(c, title):
    items = c.get("/api/recommendations", params={"include_watched": "true", "limit": 50}).json()["items"]
    return next(m["id"] for m in items if m["title"] == title)


def script(monkeypatch, turns):
    fake = FakeClient(turns)
    monkeypatch.setattr(chat, "make_client", lambda: fake)
    return fake


def post(c, messages):
    r = c.post("/api/chat", json={"messages": messages})
    return r, parse_sse(r.text) if r.status_code == 200 else []


def parse_sse(text):
    out = []
    for block in text.strip().split("\n\n"):
        ev, data = None, None
        for line in block.split("\n"):
            if line.startswith("event: "):
                ev = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        out.append((ev, data))
    return out


def names(events):
    return [e for e, _ in events]


U = [{"role": "user", "content": "something tense"}]


# ------------------------------------------------------------------ status / validation

def test_status_enabled_and_disabled(client, monkeypatch):
    client.app.include_router(chat.router) if not any(
        getattr(r, "path", "") == "/api/chat/status" for r in client.app.routes) else None
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    assert client.get("/api/chat/status").json() == {"enabled": False, "model": "claude-opus-5-5"}
    r = client.post("/api/chat", json={"messages": U})
    assert r.status_code == 503 and "detail" in r.json()
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok")
    monkeypatch.setenv("MOVIENIGHT_AI_MODEL", "claude-sonnet-5-5")
    assert client.get("/api/chat/status").json() == {"enabled": True, "model": "claude-sonnet-5-5"}


@pytest.mark.parametrize("body", [
    {}, {"messages": []}, {"messages": "x"},
    {"messages": [{"role": "assistant", "content": "hi"}]},
    {"messages": [{"role": "system", "content": "hi"}]},
    {"messages": [{"role": "user", "content": "  "}]},
    {"messages": [{"role": "user", "content": "x" * 4001}]},
    {"messages": [{"role": "user", "content": 5}]},
    {"messages": [{"role": "user", "content": "a"}] * 41},
])
def test_validation_400(chat_client, monkeypatch, body):
    script(monkeypatch, [{"text": ["x"]}])
    r = chat_client.post("/api/chat", json=body)
    assert r.status_code == 400 and "detail" in r.json()


def test_invalid_json_400(chat_client):
    r = chat_client.post("/api/chat", content=b"{nope", headers={"content-type": "application/json"})
    assert r.status_code == 400


# ------------------------------------------------------------------ happy path

def test_tool_then_recommend_then_text(chat_client, monkeypatch):
    fake = script(monkeypatch, [
        {"text": ["Let me look. "], "tools": [("search_library", {"genres": ["thriller"], "limit": 5})]},
        {"tools": [("recommend", {"picks": [{"movie_id": mid(chat_client, "Crime Two"), "pitch": "Heist like Alpha."}]})]},
        {"text": ["Enjoy ", "the film."]},
    ])
    r, ev = post(chat_client, U)
    assert r.headers["content-type"].startswith("text/event-stream")
    assert names(ev) == ["text", "tool", "tool", "movies", "text", "text", "done"]
    assert ev[1][1] == {"name": "search_library", "label": "Searching your library…"}
    items = ev[3][1]["items"]
    assert len(items) == 1 and items[0]["title"] == "Crime Two" and items[0]["pitch"] == "Heist like Alpha."
    assert "poster_url" in items[0] and "reasons" in items[0]
    assert ev[-1] == ("done", {})
    # tool result for the search went back in one user message with results
    second = fake.calls[1]["messages"]
    results = second[-1]["content"]
    assert second[-1]["role"] == "user" and results[0]["type"] == "tool_result"
    rows = json.loads(results[0]["content"])["results"]
    assert rows and all(row["title"] != "Gamma" for row in rows) and "Crime Two" in {x["title"] for x in rows}
    # request shape: stable system/tools, model default, no credentials/private data
    c0 = fake.calls[0]
    assert c0["model"] == "claude-opus-5-5" and c0["cache_control"] == {"type": "ephemeral"}
    assert c0["system"] == chat.SYSTEM_PROMPT and [t["name"] for t in c0["tools"]] == [
        "search_library", "get_movie", "get_taste_profile", "recommend"]
    assert "SECRET" not in json.dumps(fake.calls, default=str)


def test_parallel_tools_single_user_message(chat_client, monkeypatch):
    fake = script(monkeypatch, [
        {"tools": [("get_taste_profile", {}), ("get_movie", {"id": mid(chat_client, "Crime Two")}), ("search_library", {"query": "rom"})]},
        {"text": ["done"]},
    ])
    r, ev = post(chat_client, U)
    assert names(ev) == ["tool", "tool", "tool", "text", "done"]
    res = fake.calls[1]["messages"][-1]["content"]
    assert len(res) == 3 and not any(x.get("is_error") for x in res)
    profile = json.loads(res[0]["content"])
    assert {"genres", "directors", "highest_rated", "recently_watched"} <= set(profile)
    assert json.loads(res[1]["content"])["title"] == "Crime Two"
    assert [x["title"] for x in json.loads(res[2]["content"])["results"]] == ["Rom Com"]


def test_unknown_movie_id_is_error_result(chat_client, monkeypatch):
    fake = script(monkeypatch, [
        {"tools": [("recommend", {"picks": [{"movie_id": 99999, "pitch": "x"}]})]},
        {"tools": [("recommend", {"picks": [{"movie_id": mid(chat_client, "Rom Com"), "pitch": "Fun."}]})]},
        {"text": ["ok"]},
    ])
    r, ev = post(chat_client, U)
    assert names(ev).count("movies") == 1 and ev[names(ev).index("movies")][1]["items"][0]["title"] == "Rom Com"
    err = fake.calls[1]["messages"][-1]["content"][0]
    assert err["is_error"] is True and "99999" in err["content"]


def test_bad_tool_input_and_unknown_tool(chat_client, monkeypatch):
    fake = script(monkeypatch, [
        {"tools": [("get_movie", {"id": "zzz"}), ("nope", {}), ("search_library", {"limit": "many", "genres": ["Nope"]})]},
        {"text": ["sorry"]},
    ])
    post(chat_client, U)
    res = fake.calls[1]["messages"][-1]["content"]
    assert res[0]["is_error"] and res[1]["is_error"] and "is_error" not in res[2]
    assert json.loads(res[2]["content"])["total"] == 0


# ------------------------------------------------------------------ limits and stop reasons

def test_loop_cap(chat_client, monkeypatch):
    fake = script(monkeypatch, [{"tools": [("get_taste_profile", {})]}])
    r, ev = post(chat_client, U)
    assert len(fake.calls) == chat.MAX_TURNS
    assert names(ev)[-2:] == ["error", "done"] and "detail" in ev[-2][1]


def test_timeout(chat_client, monkeypatch):
    import asyncio

    class Slow(FakeStream):
        async def get_final_message(self):
            await asyncio.sleep(5)

    fake = FakeClient([{"text": []}])
    fake._stream = lambda **kw: Slow({})
    fake.beta = NS(messages=NS(stream=fake._stream))
    monkeypatch.setattr(chat, "make_client", lambda: fake)
    monkeypatch.setattr(chat, "CHAT_TIMEOUT_S", 0.2)
    r, ev = post(chat_client, U)
    assert names(ev) == ["error", "done"]


@pytest.mark.parametrize("stop", ["refusal", "max_tokens"])
def test_stop_reasons(chat_client, monkeypatch, stop):
    script(monkeypatch, [{"text": ["part"], "stop": stop}])
    r, ev = post(chat_client, U)
    assert names(ev) == ["text", "error", "done"]


def test_api_errors_are_friendly(chat_client, monkeypatch):
    import anthropic
    import httpx

    class Boom(FakeStream):
        async def __aenter__(self):
            req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
            raise anthropic.AuthenticationError("bad key sk-secret", response=httpx.Response(401, request=req), body=None)

    fake = FakeClient([{}])
    fake.beta = NS(messages=NS(stream=lambda **kw: Boom({})))
    monkeypatch.setattr(chat, "make_client", lambda: fake)
    r, ev = post(chat_client, U)
    assert names(ev) == ["error", "done"] and "sk-secret" not in r.text and "credentials" in ev[0][1]["detail"]


def test_leading_assistant_dropped_and_non_modern_model(chat_client, monkeypatch):
    monkeypatch.setenv("MOVIENIGHT_AI_MODEL", "claude-haiku-4-5")
    fake = script(monkeypatch, [{"text": ["hi"]}])
    post(chat_client, [{"role": "assistant", "content": "Hello!"}, {"role": "user", "content": "hey"}])
    assert fake.calls[0]["messages"] == [{"role": "user", "content": "hey"}]
    assert "fallbacks" not in fake.calls[0] and "thinking" not in fake.calls[0]
