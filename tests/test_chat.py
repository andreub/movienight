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
        usage = NS(**t["usage"]) if t.get("usage") else None
        return NS(stop_reason=stop, content=content, usage=usage, model=t.get("model"))


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
    for k in ("MOVIENIGHT_AI_MODEL", "MOVIENIGHT_AI_PROVIDER", "MOVIENIGHT_AI_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
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
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "MOVIENIGHT_AI_PROVIDER", "MOVIENIGHT_AI_BASE_URL",
              "MOVIENIGHT_AI_MODEL"):
        monkeypatch.delenv(k, raising=False)
    st = client.get("/api/chat/status").json()
    assert (st["enabled"], st["model"]) == (False, "claude-opus-5-5")
    r = client.post("/api/chat", json={"messages": U})
    assert r.status_code == 503 and "detail" in r.json()
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok")
    monkeypatch.setenv("MOVIENIGHT_AI_MODEL", "claude-sonnet-5-5")
    st = client.get("/api/chat/status").json()
    assert (st["enabled"], st["model"]) == (True, "claude-sonnet-5-5")


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
        "search_library", "get_movie", "get_taste_profile", "list_tags", "list_completions", "recommend"]
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


# ------------------------------------------------------------------ spending quota

import sqlite3
from datetime import datetime, timezone


def U_(i=0, o=0, w=0, r=0):
    return {"input_tokens": i, "output_tokens": o, "cache_creation_input_tokens": w, "cache_read_input_tokens": r}


def rows(c):
    db = sqlite3.connect(c.cfg.db_path)
    db.row_factory = sqlite3.Row
    try:
        return [dict(x) for x in db.execute("SELECT * FROM ai_usage ORDER BY month, model")]
    finally:
        db.close()


@pytest.fixture
def clock(monkeypatch):
    box = {"now": datetime(2026, 11, 16, 12, tzinfo=timezone.utc)}
    monkeypatch.setattr(chat, "_now", lambda: box["now"])
    return box


def test_usage_recorded_per_turn_and_summed(chat_client, monkeypatch, clock):
    script(monkeypatch, [
        {"tools": [("get_taste_profile", {})], "usage": U_(100, 10, 1000, 0), "model": "claude-haiku-5-5"},
        {"text": ["ok"], "usage": U_(50, 20, 0, 1000)},
    ])
    post(chat_client, U)
    post(chat_client, U)
    got = {r["model"]: r for r in rows(chat_client)}
    assert set(got) == {"claude-haiku-5-5", "claude-opus-5-5"}      # served model, else the requested one
    h = got["claude-haiku-5-5"]
    assert (h["month"], h["input"], h["output"], h["cache_write"], h["cache_read"], h["requests"], h["questions"]) == \
        ("2026-11", 100, 10, 1000, 0, 1, 1)
    o = got["claude-opus-5-5"]
    assert (o["input"], o["output"], o["cache_read"], o["requests"], o["questions"]) == (100, 40, 2000, 2, 1)
    u = chat_client.get("/api/chat/status").json()["usage"]
    assert u["month"] == "2026-11" and u["questions"] == 2 and u["budget_usd"] == 2.0 and not u["exhausted"]


def test_usage_recorded_on_error_stop(chat_client, monkeypatch, clock):
    script(monkeypatch, [{"text": ["cut"], "stop": "max_tokens", "usage": U_(10, 8000)}])
    _, ev = post(chat_client, U)
    assert "error" in names(ev)
    assert rows(chat_client)[0]["output"] == 8000


def test_cost_math():
    assert chat.estimate_cost("claude-opus-5-5", 1_000_000, 1_000_000) == pytest.approx(24.0)
    assert chat.estimate_cost("claude-opus-5-5", 0, 0, 1_000_000, 0) == pytest.approx(5.0)     # 1.25 x 4
    assert chat.estimate_cost("claude-opus-5-5", 0, 0, 0, 1_000_000) == pytest.approx(0.4)     # 0.1 x 4
    assert chat.estimate_cost("claude-opus-5-5-20270101", 1_000_000) == pytest.approx(4.0)    # longest prefix
    assert chat.estimate_cost("claude-opus-5", 1_000_000) == pytest.approx(5.0)
    assert chat.estimate_cost("claude-haiku-5-5", 1_000_000, 1_000_000) == pytest.approx(0.6)
    assert chat.estimate_cost("claude-mystery-9", 1_000_000, 1_000_000) == pytest.approx(48.0)  # priciest row


def test_budget_env_parsing(monkeypatch):
    def b(v):
        if v is None:
            monkeypatch.delenv("MOVIENIGHT_AI_MONTHLY_BUDGET_USD", raising=False)
        else:
            monkeypatch.setenv("MOVIENIGHT_AI_MONTHLY_BUDGET_USD", v)
        return chat.monthly_budget()

    assert b(None) == 2.0 and b("") == 2.0 and b("abc") == 2.0 and b("-1") == 2.0 and b("nan") == 2.0
    assert b("5") == 5.0 and b("0.5") == 0.5 and b("0") == 0.0


def test_quota_blocks_new_question_without_api_call(chat_client, monkeypatch, clock):
    monkeypatch.setenv("MOVIENIGHT_AI_MONTHLY_BUDGET_USD", "1")
    fake = script(monkeypatch, [{"text": ["hi"], "usage": U_(0, 100_000), "model": "claude-opus-5-5"}])  # $2
    post(chat_client, U)
    assert len(fake.calls) == 1
    r, ev = post(chat_client, U)
    assert r.status_code == 200 and names(ev) == ["text", "done"]
    assert "budget for this month ($1.00)" in ev[0][1]["delta"] and "1st" in ev[0][1]["delta"]
    assert len(fake.calls) == 1
    assert chat_client.get("/api/chat/status").json()["usage"]["exhausted"] is True


def test_budget_rechecked_before_every_turn(chat_client, monkeypatch, clock):
    # a question that spends the budget stops at its next turn (parallel questions cannot each run 8 more turns)
    monkeypatch.setenv("MOVIENIGHT_AI_MONTHLY_BUDGET_USD", "0.01")
    fake = script(monkeypatch, [
        {"tools": [("get_taste_profile", {})], "usage": U_(0, 100_000)},
        {"text": ["done"], "usage": U_(0, 1)},
    ])
    _, ev = post(chat_client, U)
    assert len(fake.calls) == 1
    assert names(ev)[-2:] == ["error", "done"] and "budget for this month" in ev[-2][1]["detail"]


def test_budget_allows_further_turns_while_under(chat_client, monkeypatch, clock):
    monkeypatch.setenv("MOVIENIGHT_AI_MONTHLY_BUDGET_USD", "100")
    fake = script(monkeypatch, [
        {"tools": [("get_taste_profile", {})], "usage": U_(0, 1000)},
        {"text": ["done"], "usage": U_(0, 1)},
    ])
    _, ev = post(chat_client, U)
    assert len(fake.calls) == 2 and "error" not in names(ev)


def test_failed_turn_still_counted(chat_client, monkeypatch, clock):
    import anthropic
    import httpx

    class Boom(FakeStream):
        async def __aenter__(self):
            req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
            raise anthropic.InternalServerError("boom", response=httpx.Response(500, request=req), body=None)

    fake = FakeClient([{}])
    fake.beta = NS(messages=NS(stream=lambda **kw: Boom({})))
    monkeypatch.setattr(chat, "make_client", lambda: fake)
    _, ev = post(chat_client, U)
    assert names(ev) == ["error", "done"]
    assert [(r["requests"], r["questions"]) for r in rows(chat_client)] == [(1, 1)]


def test_budget_zero_disables_limit(chat_client, monkeypatch, clock):
    monkeypatch.setenv("MOVIENIGHT_AI_MONTHLY_BUDGET_USD", "0")
    fake = script(monkeypatch, [{"text": ["hi"], "usage": U_(0, 1_000_000)}])
    post(chat_client, U)
    _, ev = post(chat_client, U)
    assert len(fake.calls) == 2 and ev[0][1]["delta"] == "hi"
    u = chat_client.get("/api/chat/status").json()["usage"]
    assert u["budget_usd"] is None and u["exhausted"] is False


def test_new_month_starts_from_zero(chat_client, monkeypatch, clock):
    monkeypatch.setenv("MOVIENIGHT_AI_MONTHLY_BUDGET_USD", "1")
    fake = script(monkeypatch, [{"text": ["hi"], "usage": U_(0, 100_000)}])
    post(chat_client, U)
    assert chat_client.get("/api/chat/status").json()["usage"]["exhausted"] is True
    clock["now"] = datetime(2026, 12, 1, 0, 0, tzinfo=timezone.utc)
    u = chat_client.get("/api/chat/status").json()["usage"]
    assert u["month"] == "2026-12" and u["spent_usd"] == 0 and u["questions"] == 0 and not u["exhausted"]
    _, ev = post(chat_client, U)
    assert ev[0][1]["delta"] == "hi" and len(fake.calls) == 2


def test_status_payload_shape(chat_client, monkeypatch, clock):
    script(monkeypatch, [{"text": ["hi"], "usage": U_(1000, 500), "model": "claude-haiku-5-5"}])
    post(chat_client, U)
    u = chat_client.get("/api/chat/status").json()["usage"]
    assert u == {"month": "2026-11", "spent_usd": 0.0003, "budget_usd": 2.0, "questions": 1, "tokens": 1500,
                 "model": "claude-opus-5-5", "exhausted": False}


def test_migration_5_on_existing_v4_db(tmp_path):
    from movienight import db
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    for i, sql in enumerate(db.MIGRATIONS[:4], start=1):
        conn.executescript(sql)
        conn.execute(f"PRAGMA user_version={i}")
    conn.commit()
    conn.close()
    db.migrate(path)
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS) >= 5
    assert [r[1] for r in conn.execute("PRAGMA table_info(ai_usage)")][:3] == ["month", "model", "input"]


def test_shown_films_note_reaches_anthropic_backend_unchanged(chat_client, monkeypatch):
    note = "[Films shown to the user: 1. Heat (1995) #412; 2. Thief (1981) #88]"
    hist = [{"role": "user", "content": "tonight?"}, {"role": "assistant", "content": f"Try Heat or Thief.\n{note}"},
            {"role": "user", "content": "tell me more about the second one"}]
    fake = script(monkeypatch, [{"text": ["ok"]}])
    r, ev = post(chat_client, hist)
    assert r.status_code == 200 and fake.calls[0]["messages"] == hist
    assert "Films shown to the user" in fake.calls[0]["system"] and "by title" in fake.calls[0]["system"]
