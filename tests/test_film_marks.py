"""Films by TMDB id (Complete's detail panel): seen + verdict for films outside the library, unmarking per source,
details fetched on demand, and the MOVIENIGHT_COMPLETIONIST flag. Synthetic data only."""
import json
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import plex_item, seed
from movienight import db, library, recommender as rec
from movienight.app import create_app
from movienight.config import Config
from movienight.sync import SyncManager
import test_completion
import test_jellyfin
import test_seen_push


def group(c, key="collection-900"):
    return c.get(f"/api/completions/{key}").json()


def state_of(g, tid):
    return next(f["state"] for f in g["films"] if f["tmdb_id"] == tid)


# ---------------------------------------------------------------- films outside the library

def test_missing_film_panel_from_the_group(cc):
    f = cc.get("/api/films/9001").json()            # no TMDB details, no library entry: what the group knows
    assert (f["title"], f["year"], f["in_library"], f["movie_id"]) == ("Film 9001", 2018, False, None)
    assert f["seen"] is False and f["seen_via"] == [] and f["can_unmark"] is False and f["my_vote"] is None
    assert f["poster_url"].endswith("/c9001.jpg") and f["letterboxd_url"].endswith("/tmdb/9001")
    assert cc.get("/api/films/424242").status_code == 404
    assert cc.post("/api/films/424242/seen", json={"seen": True}).status_code == 404


def test_mark_outside_library_then_it_arrives(cc):
    r = cc.post("/api/films/9001/seen", json={"seen": True, "vote": 2})
    assert r.status_code == 200 and r.json() == {"ok": True}
    with db.session(cc.cfg.db_path) as c:
        assert c.execute("SELECT 1 FROM seen WHERE key='tmdb:9001'").fetchone()
        assert c.execute("SELECT vote FROM votes WHERE key='tmdb:9001'").fetchone()[0] == 2
    g = group(cc)
    assert state_of(g, 9001) == "seen" and g["seen"] == 2
    f = cc.get("/api/films/9001").json()
    assert f["seen_via"] == ["here"] and f["can_unmark"] is True and f["my_vote"] == 2
    st = cc.app.state.engine.get()
    assert not any(h.tmdb_id == 9001 for h in st.profile.vote_hists)     # outside the library: no taste signal yet

    # the film shows up in the library (a sync stores it): seen at once, and the verdict counts
    with db.session(cc.cfg.db_path) as c:
        rows = [json.loads(r[0]) for r in c.execute("SELECT json FROM media_items WHERE server_id='m1'")]
        library.store_server_items(c, "m1", rows + [plex_item(90, "Film 9001", 2018, 9001)])
        library.assign_movies(c)
    cc.app.state.engine.invalidate()
    st = cc.app.state.engine.get()
    cand = next(c for c in st.cands if c.tmdb_id == 9001)
    assert cand.watched and cand.seen_here and cand.vote == 2
    hist = next(h for h in st.profile.vote_hists if h.tmdb_id == 9001)
    assert hist.vote == rec.LOVE
    m = cc.get(f"/api/movies/{cand.id}").json()
    assert m["watched"] is True and m["my_vote"] == 2
    assert state_of(group(cc), 9001) == "seen"
    f = cc.get("/api/films/9001").json()
    assert f["in_library"] is True and f["movie_id"] == cand.id and f["seen_via"] == ["here"]

    # and it can be unmarked again: seen mark and vote go together
    assert cc.post("/api/films/9001/seen", json={"seen": False, "vote": 0}).status_code == 200
    st = cc.app.state.engine.get()
    cand = next(c for c in st.cands if c.tmdb_id == 9001)
    assert not cand.watched and cand.vote is None and state_of(group(cc), 9001) == "available"


def test_verdict_outside_library_counts_in_taste(cc):
    from movienight import completion
    with db.session(cc.cfg.db_path) as c:
        before, _ = completion.user_ratings(cc.app.state.engine.get(), completion._tmdb_keyed(c, "votes", "vote"))
    assert 9002 not in before
    cc.post("/api/films/9002/seen", json={"seen": True, "vote": -1})
    with db.session(cc.cfg.db_path) as c:
        after, mu = completion.user_ratings(cc.app.state.engine.get(), completion._tmdb_keyed(c, "votes", "vote"))
    assert after[9002] == (mu - 1.5, False)


def test_letterboxd_seen_cannot_be_unmarked(cc):
    f = cc.get("/api/films/101").json()                 # watched on Letterboxd, not in the library
    assert f["seen"] and f["seen_via"] == ["letterboxd"] and f["can_unmark"] is False and f["my_rating"] == 4.5
    r = cc.post("/api/films/101/seen", json={"seen": False})
    assert r.status_code == 409 and "Letterboxd" in r.json()["detail"]
    assert state_of(group(cc), 101) == "seen"


def test_details_fetched_on_demand(cfg):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        assert request.url.path == "/3/movie/9001"
        return httpx.Response(200, json={"id": 9001, "title": "Film Nine", "release_date": "2018-06-01",
                                         "overview": "Fetched.", "runtime": 101, "genres": [{"name": "Drama"}]})

    app = create_app(replace(cfg, tmdb_token="eyJtest"), transport=httpx.MockTransport(handler))
    with TestClient(app) as c:
        seed(cfg.db_path)
        with db.session(cfg.db_path) as conn:
            for x in test_completion.COLLECTIONS:
                conn.execute("INSERT INTO tmdb_collections(collection_id,json,fetched_at) VALUES(?,?,?)",
                             (x["id"], json.dumps(x), "2026-01-01T00:00:00Z"))
        app.state.engine.invalidate()
        f = c.get("/api/films/9001").json()
        assert (f["title"], f["overview"], f["runtime"], f["genres"]) == ("Film Nine", "Fetched.", 101, ["Drama"])
        assert c.get("/api/films/9001").json()["overview"] == "Fetched."
        assert calls == ["/3/movie/9001"]                   # cached: fetched once


# ---------------------------------------------------------------- unmarking: app pushes yes, native plays no

def test_unmark_app_pushed_play_unscrobbles_at_once(cfg):
    app, fake = test_seen_push.make_client(cfg, True)     # MOVIENIGHT_PUSH_SEEN on
    with TestClient(app) as c:
        seed(cfg.db_path)
        app.state.engine.invalidate()
        assert c.post("/api/films/113/seen", json={"seen": True, "vote": 1}).status_code == 200
        assert [r.url.params["key"] for r in fake.scrobbles()] == ["13"]          # pushed: played on Plex
        f = c.get("/api/films/113").json()
        assert f["seen_via"] == ["here"] and f["pushed"] is True and f["can_unmark"] is True
        assert c.post("/api/films/113/seen", json={"seen": False, "vote": 0}).status_code == 200
        assert [r.url.params["key"] for r in fake.scrobbles("/:/unscrobble")] == ["13"]
        f = c.get("/api/films/113").json()                  # unseen now, not after the next sync
        assert f["seen"] is False and f["seen_via"] == [] and f["pushed"] is False
        cand = next(x for x in app.state.engine.get().cands if x.tmdb_id == 113)
        assert not cand.watched and not cand.plex_watched
        with db.session(cfg.db_path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM seen_pushes").fetchone()[0] == 0


def test_native_plex_play_cannot_be_unmarked(cfg):
    app, fake = test_seen_push.make_client(cfg, True)
    with TestClient(app) as c:
        seed(cfg.db_path)
        with db.session(cfg.db_path) as conn:              # Drama Three (113) played in Plex itself
            conn.execute("UPDATE media_items SET json=json_set(json,'$.view_count',2) WHERE rating_key='13'")
        app.state.engine.invalidate()
        f = c.get("/api/films/113").json()
        assert f["seen"] and f["seen_via"] == ["server"] and f["can_unmark"] is False
        r = c.post("/api/films/113/seen", json={"seen": False})
        assert r.status_code == 409 and r.json()["detail"].startswith("Seen on Plex")
        assert fake.scrobbles("/:/unscrobble") == []
        assert c.get("/api/films/113").json()["seen_via"] == ["server"]
        mid = f["movie_id"]                                 # the library route refuses it the same way
        r = c.post(f"/api/movies/{mid}/seen", json={"seen": False})
        assert r.status_code == 409 and r.json()["detail"].startswith("Seen on Plex")
        assert fake.scrobbles("/:/unscrobble") == []
        assert c.post(f"/api/movies/{mid}/seen", json={"seen": True}).status_code == 200


def test_native_jellyfin_play_cannot_be_unmarked(tmp_path):
    fake = test_jellyfin.FakeJellyfin()
    cfg = test_jellyfin.jf_cfg(tmp_path)
    app = create_app(cfg, transport=httpx.MockTransport(fake))
    with TestClient(app) as c:
        test_jellyfin.seed_history(cfg.db_path)
        test_jellyfin.run_sync(c, "jellyfin")
        f = c.get("/api/films/113").json()                  # "Played One", played in Jellyfin
        assert f["seen_via"] == ["server"] and f["can_unmark"] is False
        r = c.post("/api/films/113/seen", json={"seen": False})
        assert r.status_code == 409 and "Jellyfin" in r.json()["detail"]
        assert not [p for m, p in fake.paths() if m == "DELETE"]


# ---------------------------------------------------------------- MOVIENIGHT_COMPLETIONIST

def test_flag_from_env(monkeypatch):
    monkeypatch.delenv("MOVIENIGHT_COMPLETIONIST", raising=False)
    assert Config.from_env().completionist is True
    for off in ("0", "false", "no"):
        monkeypatch.setenv("MOVIENIGHT_COMPLETIONIST", off)
        assert Config.from_env().completionist is False
    monkeypatch.setenv("MOVIENIGHT_COMPLETIONIST", "1")
    assert Config.from_env().completionist is True


def test_flag_on_status_and_routes(cc):
    assert cc.get("/api/status").json()["features"] == {"completionist": True}
    assert cc.get("/api/completions").status_code == 200


def test_flag_off_hides_the_api(cfg):
    app = create_app(replace(cfg, completionist=False), transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    with TestClient(app) as c:
        seed(cfg.db_path)
        app.state.engine.invalidate()
        assert c.get("/api/status").json()["features"] == {"completionist": False}
        assert c.get("/api/completions").status_code == 404
        assert c.get("/api/completions/collection-900").status_code == 404
        assert c.post("/api/completions/collection-900/hide").status_code == 404
        assert c.delete("/api/completions/collection-900/hide").status_code == 404
        assert c.get("/api/movies/1").status_code in (200, 404)     # the rest of the API is unchanged


async def test_flag_off_skips_the_lookups(tmp_path):
    cfg = replace(test_completion._cfg(tmp_path), completionist=False)
    with db.session(cfg.db_path) as c:
        c.execute("INSERT INTO lb_films(name,year,norm_title,tmdb_id,resolve_state,watched) VALUES('F',2000,'f',1,1,1)")
        c.execute("INSERT INTO tmdb_details(tmdb_id,json,fetched_at) VALUES(1,?,'x')", (json.dumps(   # predates them
            {"id": 1, "title": "F", "release_date": "2000-01-01"}),))
        c.execute("INSERT INTO tmdb_details(tmdb_id,json,fetched_at) VALUES(2,?,'x')", (json.dumps(
            {"id": 2, "release_date": "2000-01-01", "collection": {"id": 77, "name": "Web-Slinger Collection"},
             "keyword_ids": [5001], "keywords": ["pint trilogy"]}),))
        c.execute("INSERT INTO lb_films(name,year,norm_title,tmdb_id,resolve_state,watched) VALUES('G',2000,'g',2,1,1)")

    def handler(request):
        raise AssertionError(f"unexpected TMDB call {request.url.path}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        mgr = SyncManager(cfg, http, library.Engine(lambda: db.connect(cfg.db_path), None))
        await mgr.stage_tmdb()          # no backfill, no collection search, no collection, no keyword discover
    assert not mgr._errors
    with db.session(cfg.db_path) as c:
        assert c.execute("SELECT COUNT(*) FROM tmdb_collections").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM tmdb_lookups").fetchone()[0] == 0


@pytest.fixture
def cc(client):
    with db.session(client.cfg.db_path) as c:
        for x in test_completion.COLLECTIONS:
            c.execute("INSERT INTO tmdb_collections(collection_id,json,fetched_at) VALUES(?,?,?)",
                      (x["id"], json.dumps(x), "2026-01-01T00:00:00Z"))
        for kid, k in test_completion.KEYWORDS.items():
            c.execute("INSERT INTO tmdb_lookups(key,json,fetched_at) VALUES(?,?,?)",
                      (f"keyword:{kid}", json.dumps(k), "2026-01-01T00:00:00Z"))
    client.app.state.engine.invalidate()
    return client
