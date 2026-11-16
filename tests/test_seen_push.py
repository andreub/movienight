"""Pushing "seen it" marks to Plex as played (MOVIENIGHT_PLEX_PUSH_SEEN). Synthetic data only."""
import json
import time
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import FIX, seed
from movienight import db, library
from movienight.app import create_app
import test_sync


class FakePlex:
    """Records every request; scrobble/unscrobble answer `status` (or raise when `down`)."""

    def __init__(self):
        self.calls: list[httpx.Request] = []
        self.status = 200
        self.down = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if request.url.path in ("/:/scrobble", "/:/unscrobble"):
            if self.down:
                raise httpx.ConnectError("unreachable", request=request)
            return httpx.Response(self.status)
        raise AssertionError(f"unexpected network call: {request.url.host}{request.url.path}")

    def scrobbles(self, path="/:/scrobble"):
        return [r for r in self.calls if r.url.path == path]


def make_client(cfg, push: bool):
    fake = FakePlex()
    app = create_app(replace(cfg, plex_push_seen=push), transport=httpx.MockTransport(fake))
    return app, fake


@pytest.fixture
def push_client(cfg):
    app, fake = make_client(cfg, True)
    with TestClient(app) as c:
        seed(cfg.db_path)
        app.state.engine.invalidate()
        c.cfg, c.fake = cfg, fake
        yield c


def mid(client, title):
    return next(c.id for c in client.app.state.engine.get().cands if c.title == title)


def mark(client, title, value=True):
    r = client.post(f"/api/movies/{mid(client, title)}/seen", json={"seen": value})
    assert r.status_code == 200 and r.json() == {"ok": True}
    return r


def pushes(client):
    with db.session(client.cfg.db_path) as c:
        return [tuple(r) for r in c.execute("SELECT key, server_id, rating_key FROM seen_pushes ORDER BY key")]


def seen_keys(client):
    with db.session(client.cfg.db_path) as c:
        return {r[0] for r in c.execute("SELECT key FROM seen")}


def view_count(client, server_id, rating_key):
    with db.session(client.cfg.db_path) as c:
        row = c.execute("SELECT json FROM plex_items WHERE server_id=? AND rating_key=?", (server_id, rating_key)).fetchone()
        return json.loads(row[0])["view_count"]


def run_sync_push(client):
    sync = client.app.state.sync

    async def go():
        c = db.connect(client.cfg.db_path)
        try:
            return await sync.push_seen(c, db.client_id(c))
        finally:
            c.close()

    out = client.portal.call(go)
    client.app.state.engine.invalidate()
    return out


def test_flag_off_makes_no_plex_call(cfg):
    app, fake = make_client(cfg, False)
    with TestClient(app) as c:
        seed(cfg.db_path)
        app.state.engine.invalidate()
        c.cfg = cfg
        mark(c, "Crime Two")
        mark(c, "Crime Two", False)
        mark(c, "Rom Com")
        assert fake.calls == []
        assert seen_keys(c) == {"tmdb:112"} and pushes(c) == []
        assert c.get("/api/status").json()["plex"]["seen_pushed"] is None


def test_mark_scrobbles_and_records(push_client):
    c = push_client
    mark(c, "Crime Two")                                       # rating key 11, tmdb 111
    [req] = c.fake.scrobbles()
    assert req.method == "GET" and req.url.host == "plex.local" and req.url.port == 32400
    assert dict(req.url.params) == {"key": "11", "identifier": "com.plexapp.plugins.library"}
    assert req.headers["X-Plex-Token"] == "SECRET"
    assert req.headers["X-Plex-Product"] == "MovieNight" and req.headers["X-Plex-Client-Identifier"]
    assert "SECRET" not in str(req.url)                        # token only in a header
    assert pushes(c) == [("tmdb:111", "m1", "11")]
    assert view_count(c, "m1", "11") == 1
    m = c.get(f"/api/movies/{mid(c, 'Crime Two')}").json()
    assert m["watched"] is True and m["seen_here"] is True      # still offers the undo
    assert c.get("/api/status").json()["plex"]["seen_pushed"] == 1
    mark(c, "Crime Two")                                       # marking again: nothing new to push
    assert len(c.fake.scrobbles()) == 1


def test_plex_error_keeps_mark_and_sync_retries(push_client, caplog):
    c = push_client
    c.fake.status = 500
    with caplog.at_level("WARNING"):
        mark(c, "Crime Two")
        c.fake.down = True
        mark(c, "Rom Com")
    assert "could not mark tmdb:111 played" in caplog.text and "SECRET" not in caplog.text
    assert seen_keys(c) == {"tmdb:111", "tmdb:112"} and pushes(c) == []
    assert view_count(c, "m1", "11") == 0
    c.fake.status, c.fake.down = 200, False
    counts = run_sync_push(c)
    assert counts["pushed"] == 2 and counts["failed"] == 0
    assert pushes(c) == [("tmdb:111", "m1", "11"), ("tmdb:112", "m1", "12")]
    n = len(c.fake.scrobbles())
    assert run_sync_push(c)["pushed"] == 0 and len(c.fake.scrobbles()) == n   # nothing left to do


def test_unreachable_server_is_skipped_until_reachable(push_client):
    c = push_client
    with db.session(c.cfg.db_path) as conn:
        conn.execute("UPDATE plex_servers SET reachable=0")
    mark(c, "Crime Two")
    assert c.fake.calls == [] and pushes(c) == []
    with db.session(c.cfg.db_path) as conn:
        conn.execute("UPDATE plex_servers SET reachable=1")
    assert run_sync_push(c)["pushed"] == 1


def test_already_watched_in_plex_is_left_alone(push_client):
    c = push_client
    mark(c, "Plex Watched")                                    # view_count 3, no TMDB id
    assert c.fake.calls == [] and pushes(c) == []
    counts = run_sync_push(c)
    assert counts["already_watched"] == 1 and counts["pushed"] == 0 and c.fake.calls == []
    mark(c, "Plex Watched", False)                             # never unscrobbled
    assert c.fake.calls == []
    m = c.get(f"/api/movies/{mid(c, 'Plex Watched')}").json()
    assert m["watched"] is True and m["seen_here"] is False


def test_unmark_unscrobbles_only_pushed_items(push_client):
    c = push_client
    # the same film on a second (shared) server where the user already watched it in Plex
    with db.session(c.cfg.db_path) as conn:
        conn.execute("INSERT INTO plex_servers(id,name,owned,reachable,base_url,access_token) VALUES"
                     "('s2','Friend',0,1,'http://friend.local:32400','SECRET2')")
        from conftest import plex_item
        library.store_server_items(conn, "s2", [plex_item(91, "Rom Com", 2020, 112, views=1)])
        library.assign_movies(conn)
    c.app.state.engine.invalidate()
    mark(c, "Rom Com")
    assert [r.url.host for r in c.fake.scrobbles()] == ["plex.local"]
    assert pushes(c) == [("tmdb:112", "m1", "12")]
    m = c.get(f"/api/movies/{mid(c, 'Rom Com')}").json()
    assert m["seen_here"] is False                             # genuinely watched on the shared server
    mark(c, "Rom Com", False)
    [un] = c.fake.scrobbles("/:/unscrobble")
    assert un.url.host == "plex.local" and dict(un.url.params) == {"key": "12", "identifier": "com.plexapp.plugins.library"}
    assert pushes(c) == [] and view_count(c, "m1", "12") == 0 and view_count(c, "s2", "91") == 1
    mark(c, "Drama Three", False)                              # never marked: no call
    assert len(c.fake.scrobbles("/:/unscrobble")) == 1


def test_failed_unscrobble_is_retried_by_sync(push_client):
    c = push_client
    mark(c, "Crime Two")
    c.fake.status = 503
    mark(c, "Crime Two", False)
    assert seen_keys(c) == set() and pushes(c) == [("tmdb:111", "m1", "11")]
    m = c.get(f"/api/movies/{mid(c, 'Crime Two')}").json()
    assert m["watched"] is False                               # our own push does not count as watched
    c.fake.status = 200
    out = run_sync_push(c)
    assert out["unpushed"] == 1 and pushes(c) == []
    assert len(c.fake.scrobbles("/:/unscrobble")) == 2


def test_pushed_film_keeps_seen_here_after_library_sync(push_client):
    c = push_client
    mark(c, "Crime Two")
    from conftest import LIBRARY, plex_item
    with db.session(c.cfg.db_path) as conn:                    # next library read: Plex now says watched
        library.store_server_items(conn, "m1", [plex_item(k, t, y, tid, views=1 if k == 11 else v)
                                                for k, t, y, tid, v in LIBRARY])
        library.assign_movies(conn)
    c.app.state.engine.invalidate()
    cid = mid(c, "Crime Two")
    m = c.get(f"/api/movies/{cid}").json()
    assert m["watched"] is True and m["seen_here"] is True
    assert run_sync_push(c)["pushed"] == 0                     # recorded: not pushed twice
    mark(c, "Crime Two", False)
    assert len(c.fake.scrobbles("/:/unscrobble")) == 1
    m = c.get(f"/api/movies/{cid}").json()
    assert m["watched"] is False and m["seen_here"] is False


def test_token_never_in_api_responses(push_client):
    c = push_client
    mark(c, "Crime Two")
    texts = [c.get("/api/status").text, c.get(f"/api/movies/{mid(c, 'Crime Two')}").text,
             c.get("/api/recommendations", params={"include_watched": True, "limit": 200}).text,
             c.post(f"/api/movies/{mid(c, 'Rom Com')}/seen", json={"seen": True}).text]
    assert not any("SECRET" in t for t in texts)


def test_full_sync_backfills_existing_marks(tmp_path, caplog):
    """End to end: a seen mark made before the flag existed is pushed after the Plex library stage."""
    scrobbled = []

    def handler(request):
        if request.url.path == "/:/scrobble":
            scrobbled.append((request.url.host, dict(request.url.params), request.headers["X-Plex-Token"]))
            return httpx.Response(200)
        return test_sync.handler(request)

    from movienight.config import Config
    cfg = Config(data_dir=tmp_path / "d", letterboxd_dir=FIX / "export", sync_on_start=False,
                 plex_token="ENV-PLEX-TOKEN", plex_push_seen=True)
    db.migrate(cfg.db_path)
    with db.session(cfg.db_path) as conn:
        for key in ("imdb:tt9", "tmdb:101", "tmdb:999999"):    # Zeta Noir (unwatched), Alpha Heist (watched), absent
            conn.execute("INSERT INTO seen(key, created_at) VALUES(?, '2024-01-01T00:00:00Z')", (key,))
    app = create_app(cfg, transport=httpx.MockTransport(handler))
    with TestClient(app) as c, caplog.at_level("INFO"):
        assert c.post("/api/sync", json={"what": "plex"}).status_code == 202
        for _ in range(200):
            s = c.get("/api/status").json()
            if not s["sync"]["running"]:
                break
            time.sleep(0.05)
        assert s["sync"]["error"] is None, s["sync"]
        assert scrobbled == [("remote.example", {"key": "2", "identifier": "com.plexapp.plugins.library"},
                              "SERVER-SECRET-1")]
        last = app.state.sync.last_push
        assert last["pushed"] == 1 and last["already_watched"] == 1 and last["not_on_server"] == 1
        assert s["plex"]["seen_pushed"] == 1
        by = {m["title"]: m for m in c.get("/api/recommendations", params={"include_watched": True}).json()["items"]}
        assert by["Zeta Noir"]["seen_here"] is True and by["Alpha Heist"]["seen_here"] is False
    assert "SERVER-SECRET" not in caplog.text and "ENV-PLEX-TOKEN" not in caplog.text


def votes(client):
    with db.session(client.cfg.db_path) as c:
        return {r[0]: r[1] for r in c.execute("SELECT key, vote FROM votes")}


def test_seen_with_vote_pushes_and_undo_unscrobbles(push_client):
    c = push_client
    cid = mid(c, "Crime Two")
    assert c.post(f"/api/movies/{cid}/seen", json={"seen": True, "vote": 2}).json() == {"ok": True}
    [req] = c.fake.scrobbles()
    assert dict(req.url.params)["key"] == "11"
    assert pushes(c) == [("tmdb:111", "m1", "11")] and votes(c) == {"tmdb:111": 2}
    c.post(f"/api/movies/{cid}/seen", json={"seen": True, "vote": -1})     # re-rating: nothing new to push
    assert len(c.fake.scrobbles()) == 1 and votes(c) == {"tmdb:111": -1}
    c.post(f"/api/movies/{cid}/seen", json={"seen": False, "vote": 0})
    assert len(c.fake.scrobbles("/:/unscrobble")) == 1
    assert pushes(c) == [] and seen_keys(c) == set() and votes(c) == {}


def test_seen_with_vote_survives_plex_error(push_client):
    c = push_client
    c.fake.down = True
    r = c.post(f"/api/movies/{mid(c, 'Rom Com')}/seen", json={"seen": True, "vote": 1})
    assert r.status_code == 200
    assert seen_keys(c) == {"tmdb:112"} and votes(c) == {"tmdb:112": 1} and pushes(c) == []


def test_seen_with_vote_flag_off_makes_no_plex_call(cfg):
    app, fake = make_client(cfg, False)
    with TestClient(app) as c:
        seed(cfg.db_path)
        app.state.engine.invalidate()
        c.cfg = cfg
        cid = mid(c, "Crime Two")
        c.post(f"/api/movies/{cid}/seen", json={"seen": True, "vote": 2})
        c.post(f"/api/movies/{cid}/seen", json={"seen": False, "vote": 0})
        c.post(f"/api/movies/{mid(c, 'Rom Com')}/seen", json={"seen": True, "vote": -1})
        assert fake.calls == [] and pushes(c) == []
        assert seen_keys(c) == {"tmdb:112"} and votes(c) == {"tmdb:112": -1}
