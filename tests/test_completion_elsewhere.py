"""Completionist elsewhere in the app: the chat's list_completions tool, the groups a film is part of on its detail
("Part of X · n of N") and Tonight's "Complete a saga" filter (/api/pick?complete=1). Synthetic data only."""
import json
from dataclasses import replace
from datetime import date

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import seed
from movienight import chat, completion, db
from movienight.app import create_app
from test_completion import LISTS, cc  # noqa: F401 - fixture
from test_chat import parse_sse, script

# seeded (conftest + test_completion.cc): sagas in progress collection-960 (long saga, nothing in the library),
# collection-900 "Alpha" (next in the library: Crime Two, tmdb 111), series-5001 "Pint Trilogy" (next: Drama Three,
# 113) and franchise-web-slinger (next: Rom Com, 112); collection-970 is complete. 101 (seen) is in 900 and 970.


@pytest.fixture
def today(monkeypatch):
    monkeypatch.setattr(completion, "_today", lambda: date(2026, 11, 22))


def mid_of(client, tmdb_id):
    with db.session(client.cfg.db_path) as c:
        return c.execute("SELECT id FROM movies WHERE tmdb_id=?", (tmdb_id,)).fetchone()[0]


def add_orphan(client):
    """Sci Epic (tmdb 116, in the library, unwatched) joins the long saga as an orphan ("Long Saga: ...")."""
    films = [{"id": 116, "title": "Long Saga: Sci Epic", "year": 2021, "release_date": "2021-05-01",
              "poster_path": None, "backdrop_path": None, "vote_count": 5000, "vote_average": 7.0,
              "genre_ids": [878], "via": "title", "collection_id": None, "runtime": 160}]
    with db.session(client.cfg.db_path) as c:
        c.execute("INSERT INTO tmdb_lookups(key,json,fetched_at) VALUES(?,?,'2026-11-01T00:00:00Z')",
                  (completion.orphan_key("Long Saga"), json.dumps({"phrase": "Long Saga", "films": films})))
    client.app.state.engine.invalidate()


def add_director(client):
    """A director of three seen films (101, 202, 303) with one more film, outside every saga (9850)."""
    with db.session(client.cfg.db_path) as c:
        for tid in (101, 202, 303):
            c.execute("UPDATE tmdb_details SET json=json_set(json,'$.director_ids',json('[525]')) WHERE tmdb_id=?", (tid,))
        p = {"id": 525, "name": "Dee Rector", "profile_path": None, "acted": [], "directed": [
            {"id": t, "title": f"P{t}", "release_date": f"{y}-05-01", "vote_count": 1000, "vote_average": 7.0,
             "genre_ids": [18]} for t, y in ((101, 2015), (202, 2019), (303, 2001), (9850, 2024))]}
        c.execute("INSERT INTO tmdb_person_credits(person_id,name,profile_path,json,fetched_at) VALUES(525,?,NULL,?,?)",
                  (p["name"], json.dumps(p), "2026-11-01T00:00:00Z"))
    client.app.state.engine.invalidate()


def flag_off_client(cfg, **kw):
    app = create_app(replace(cfg, completionist=False), transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    return app, TestClient(app, **kw)


# ---------------------------------------------------------------- chat: list_completions

def run(client, args, connect=True):
    st = client.app.state.engine.get()
    conn = (lambda: db.connect(client.cfg.db_path)) if connect else None
    return chat.run_tool("list_completions", args, st, {}, conn)


def test_list_completions_shape_and_ranking(cc, today):  # noqa: F811
    o = run(cc, {})
    assert not o.is_error
    out = json.loads(o.content)
    # the same ranking as /api/completions, in progress only
    assert [g["name"] for g in out["groups"]] == [
        g["name"] for g in cc.get("/api/completions").json()["groups"]]
    assert (out["in_progress"], out["completed"]) == (4, 1)
    alpha = next(g for g in out["groups"] if g["name"] == "Alpha")
    assert {k: alpha[k] for k in ("kind", "seen", "total", "in_library", "reason")} == {
        "kind": "collection", "seen": 1, "total": 4, "in_library": 1, "reason": "You rated ★4.5"}
    # next films: the one in the library first (with its movie id), then the missing ones in release order
    assert alpha["next"] == [{"title": "Film 111", "year": 2016, "movie_id": mid_of(cc, 111)},
                             {"title": "Film 9001", "year": 2018, "in_library": False},
                             {"title": "Film 9002", "year": 2020, "in_library": False}]
    assert all(len(g["next"]) <= completion.CHAT_NEXT for g in out["groups"])
    assert "films" not in alpha and "key" not in alpha and "poster_url" not in o.content     # compact
    assert len(json.loads(run(cc, {"limit": 2}).content)["groups"]) == 2
    assert len(json.loads(run(cc, {"limit": "lots"}).content)["groups"]) == 4          # bad limit: the default


def test_list_completions_kind_filter(cc, today):  # noqa: F811
    sagas = json.loads(run(cc, {"kind": "collection"}).content)["groups"]
    assert {g["kind"] for g in sagas} == {"collection", "series"}       # the Sagas chip holds the keyword series
    fr = json.loads(run(cc, {"kind": "franchise"}).content)["groups"]
    assert [g["name"] for g in fr] == ["Web-Slinger"]
    none = json.loads(run(cc, {"kind": "director"}).content)
    assert none["groups"] == [] and none["note"]
    bad = run(cc, {"kind": "curated"})
    assert bad.is_error and "collection" in bad.content


def test_list_completions_unknown_without_connection(cc):  # noqa: F811
    o = run(cc, {}, connect=False)
    assert o.is_error and "Unknown tool" in o.content


def test_list_completions_in_the_chat_loop(cc, monkeypatch):  # noqa: F811
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    for k in ("MOVIENIGHT_AI_MODEL", "MOVIENIGHT_AI_PROVIDER", "MOVIENIGHT_AI_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    if not any(getattr(r, "path", "") == "/api/chat" for r in cc.app.routes):
        cc.app.include_router(chat.router)
    fake = script(monkeypatch, [{"tools": [("list_completions", {"kind": "franchise"})]}, {"text": ["Go on."]}])
    r = cc.post("/api/chat", json={"messages": [{"role": "user", "content": "which saga should I continue?"}]})
    ev = parse_sse(r.text)
    assert ev[0] == ("tool", {"name": "list_completions", "label": chat.TOOL_LABELS["list_completions"]})
    res = fake.calls[1]["messages"][-1]["content"][0]
    assert not res.get("is_error") and json.loads(res["content"])["groups"][0]["name"] == "Web-Slinger"


def test_list_completions_absent_when_flag_off(cfg, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    for k in ("MOVIENIGHT_AI_MODEL", "MOVIENIGHT_AI_PROVIDER", "MOVIENIGHT_AI_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    app, client = flag_off_client(cfg)
    app.include_router(chat.router)
    with client as c:
        seed(cfg.db_path)
        app.state.engine.invalidate()
        fake = script(monkeypatch, [{"tools": [("list_completions", {})]}, {"text": ["ok"]}])
        c.post("/api/chat", json={"messages": [{"role": "user", "content": "sagas?"}]})
    assert "list_completions" not in [t["name"] for t in fake.calls[0]["tools"]]
    res = fake.calls[1]["messages"][-1]["content"][0]
    assert res["is_error"] is True and "Unknown tool" in res["content"]
    assert chat.tools_for(False) is chat.TOOLS and "list_completions" not in [t["name"] for t in chat.TOOLS]


# ---------------------------------------------------------------- detail: groups a film is part of

def test_movie_detail_groups_best_first(cc, today):  # noqa: F811
    g = cc.get(f"/api/movies/{mid_of(cc, 111)}").json()["groups"]
    assert g == [{"key": "collection-900", "name": "Alpha", "kind": "collection", "seen": 1, "total": 4}]
    assert cc.get(f"/api/movies/{mid_of(cc, 113)}").json()["groups"][0]["key"] == "series-5001"
    assert cc.get(f"/api/movies/{mid_of(cc, 112)}").json()["groups"][0]["key"] == "franchise-web-slinger"
    assert cc.get(f"/api/movies/{mid_of(cc, 115)}").json()["groups"] == []        # only in an unstarted saga
    # a film seen on Letterboxd only (light detail by TMDB id): its saga in progress, then the finished one
    assert [x["key"] for x in cc.get("/api/films/101").json()["groups"]] == ["collection-900", "collection-970"]
    assert cc.get("/api/films/9001").json()["groups"][0]["key"] == "collection-900"     # missing from the library


def test_movie_detail_groups_orphan_and_hidden(cc, today):  # noqa: F811
    assert cc.get(f"/api/movies/{mid_of(cc, 116)}").json()["groups"] == []
    add_orphan(cc)
    g = cc.get(f"/api/movies/{mid_of(cc, 116)}").json()["groups"]
    assert [(x["key"], x["total"]) for x in g] == [("collection-960", 31)]
    assert cc.post("/api/completions/collection-960/hide").status_code == 200
    assert cc.get(f"/api/movies/{mid_of(cc, 116)}").json()["groups"] == []          # hidden groups stay out


def test_movie_detail_groups_cap_lists_and_people(cc, today, monkeypatch):  # noqa: F811
    monkeypatch.setattr(completion, "bundled_lists", lambda: LISTS)
    add_director(cc)
    # 101: two sagas, two lists and a director: the best two (sagas first), no person
    g = cc.get("/api/films/101").json()["groups"]
    assert [x["key"] for x in g] == ["collection-900", "collection-970"]
    # 111: one saga and one list
    assert [x["key"] for x in cc.get(f"/api/movies/{mid_of(cc, 111)}").json()["groups"]] == [
        "collection-900", "list-best-ever"]
    assert cc.get("/api/films/9701").json()["groups"][0]["kind"] == "list"
    # 9850: in no saga or list, so the director's filmography
    assert cc.get("/api/films/9850").json()["groups"] == [
        {"key": "director-525", "name": "Dee Rector", "kind": "director", "seen": 3, "total": 4}]


def test_movie_detail_no_groups_when_flag_off(cfg):
    app, client = flag_off_client(cfg)
    with client as c:
        seed(cfg.db_path)
        app.state.engine.invalidate()
        mid = c.get("/api/recommendations", params={"limit": 50}).json()["items"][0]["id"]
        assert "groups" not in c.get(f"/api/movies/{mid}").json()


# ---------------------------------------------------------------- Tonight: Complete a saga

def test_pick_complete_a_saga(cc, today):  # noqa: F811
    nexts = {mid_of(cc, t) for t in (111, 112, 113)}
    plain = cc.get("/api/pick").json()
    assert plain["saga_count"] == 3 and not any(r["kind"] == "trophy" for r in plain["pick"]["reasons"])
    for _ in range(5):
        r = cc.get("/api/pick", params={"complete": "true"}).json()
        shown = [r["pick"]] + r["alternatives"]
        assert {m["id"] for m in shown} <= nexts and len(shown) == 3 and r["saga_count"] == 3
        for m in shown:
            assert m["reasons"][0]["kind"] == "trophy" and m["reasons"][0]["text"].startswith(f"Next in {m['saga']['name']} · ")
    alpha = next(m for m in shown if m["id"] == mid_of(cc, 111))
    assert alpha["reasons"][0]["text"] == "Next in Alpha · 1 of 4 seen" and alpha["saga"]["key"] == "collection-900"
    # the other filters still apply, and count what is left
    assert cc.get("/api/pick", params={"complete": "true", "mood": "light"}).json()["saga_count"] == 1
    # a hidden saga is not suggested; neither is a film once seen
    cc.post("/api/completions/franchise-web-slinger/hide")
    cc.post(f"/api/movies/{mid_of(cc, 111)}/seen", json={"seen": True})
    r = cc.get("/api/pick", params={"complete": "true"}).json()
    assert {m["id"] for m in [r["pick"]] + r["alternatives"]} == {mid_of(cc, 113)} and r["saga_count"] == 1
    rec = cc.get("/api/recommendations", params={"complete": "true"}).json()
    assert [m["id"] for m in rec["items"]] == [mid_of(cc, 113)] and rec["items"][0]["saga"]["key"] == "series-5001"
    assert cc.get("/api/eras", params={"complete": "true"}).json()


def test_pick_complete_nothing_left(client):
    r = client.get("/api/pick").json()
    assert r["saga_count"] == 0
    assert client.get("/api/pick", params={"complete": "true"}).status_code == 404


def test_pick_complete_flag_off(cfg):
    app, client = flag_off_client(cfg)
    with client as c:
        seed(cfg.db_path)
        app.state.engine.invalidate()
        assert "saga_count" not in c.get("/api/pick").json()
        assert c.get("/api/pick", params={"complete": "true"}).status_code == 404
