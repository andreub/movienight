"""API and auth hardening: no blocking work on the event loop, path id and body validation, session revocation,
the per-client TV-code quota, and on-demand TMDB fetches only for films the app knows of. Synthetic data only."""
import asyncio
import io
import json
import math
import zipfile
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import seed
from movienight import auth, db, letterboxd as lb, library
from movienight.app import create_app
from movienight.config import Config
import test_auth
import test_auth_plex
import test_completion


def _on_loop() -> bool:
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


@pytest.fixture
def cc(client):
    with db.session(client.cfg.db_path) as c:
        for x in test_completion.COLLECTIONS:
            c.execute("INSERT INTO tmdb_collections(collection_id,json,fetched_at) VALUES(?,?,?)",
                      (x["id"], json.dumps(x), "2026-01-01T00:00:00Z"))
    client.app.state.engine.invalidate()
    return client


# ---------------------------------------------------------------- #56: heavy work off the event loop

def test_heavy_work_runs_off_the_event_loop(cc, monkeypatch):
    calls = []
    real_get, real_import = library.Engine.get, lb.import_zip

    def get(self):
        calls.append(("engine", _on_loop()))
        return real_get(self)

    def import_zip(conn, data):
        calls.append(("import", _on_loop()))
        return real_import(conn, data)

    monkeypatch.setattr(library.Engine, "get", get)
    monkeypatch.setattr(lb, "import_zip", import_zip)
    mid = next(m["id"] for m in cc.get("/api/recommendations", params={"limit": 50}).json()["items"])
    assert cc.get("/api/films/9001").status_code == 200
    assert cc.post("/api/films/9001/seen", json={"seen": True}).status_code == 200
    assert cc.post(f"/api/movies/{mid}/seen", json={"seen": True}).status_code == 200
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("watched.csv", "Date,Name,Year,Letterboxd URI\n")
    assert cc.post("/api/letterboxd/import", files={"file": ("e.zip", buf.getvalue())}).status_code == 200
    assert ("import", False) in calls and ("engine", False) in calls
    assert ("engine", True) not in calls and ("import", True) not in calls


def test_image_cache_read_off_the_event_loop(cc, monkeypatch):
    cfg = cc.cfg
    (cfg.image_dir / "11_poster.img").write_bytes(b"\x89PNG\r\n\x1a\nxx")
    seen = []
    real = type(cfg.image_dir).read_bytes

    def read_bytes(self):
        seen.append(_on_loop())
        return real(self)

    monkeypatch.setattr(type(cfg.image_dir), "read_bytes", read_bytes)
    r = cc.get("/api/image/11/poster")
    assert r.status_code == 200 and seen == [False]


# ---------------------------------------------------------------- #64: validation

BIG = "99999999999999999999"


@pytest.mark.parametrize("method,path,body", [
    ("POST", f"/api/movies/{BIG}/dismiss", None), ("DELETE", f"/api/movies/{BIG}/dismiss", None),
    ("POST", f"/api/movies/{BIG}/feedback", {"vote": 1}), ("POST", f"/api/movies/{BIG}/seen", {"seen": True}),
    ("GET", f"/api/image/{BIG}/poster", None), ("GET", f"/api/films/{BIG}", None),
    ("POST", f"/api/films/{BIG}/seen", {"seen": True}), ("GET", f"/api/movies/{BIG}", None),
    ("GET", "/api/films/0", None),
])
def test_out_of_range_ids_are_422_not_500(client, method, path, body):
    r = client.request(method, path, json=body)
    assert r.status_code == 422 and r.json()["detail"].startswith("Invalid")


def test_votes_and_seen_are_strict(client):
    mid = next(m["id"] for m in client.get("/api/recommendations", params={"limit": 50}).json()["items"])
    for bad in (True, 1.0, "1"):
        assert client.post(f"/api/movies/{mid}/feedback", json={"vote": bad}).status_code == 422
        assert client.post(f"/api/movies/{mid}/seen", json={"seen": True, "vote": bad}).status_code == 422
    assert client.post(f"/api/movies/{mid}/seen", json={"seen": 1}).status_code == 422
    assert client.post(f"/api/movies/{mid}/feedback", json={"vote": 1}).status_code == 200
    assert client.post(f"/api/movies/{mid}/seen", json={"seen": True, "vote": -1}).status_code == 200


def test_legacy_mood_and_era_together_are_refused(client):
    r = client.get("/api/recommendations", params={"mood": "recent", "era": "1980s"})
    assert r.status_code == 400 and "era" in r.json()["detail"]
    assert client.get("/api/recommendations", params={"mood": "recent"}).status_code == 200
    assert client.get("/api/recommendations", params={"era": "1980s"}).status_code == 200


@pytest.mark.parametrize("raw", ["nan", "inf", "-inf"])
def test_non_finite_sync_hours_fall_back(monkeypatch, raw):
    monkeypatch.setenv("MOVIENIGHT_SYNC_HOURS", raw)
    hours = Config.from_env().sync_hours
    assert math.isfinite(hours) and hours == 6.0


# ---------------------------------------------------------------- #66: on-demand TMDB fetch only for known films

def test_unknown_tmdb_id_is_not_fetched_nor_markable(cfg):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"id": 1, "title": "Anything", "release_date": "2018-06-01"})

    app = create_app(replace(cfg, tmdb_token="eyJtest"), transport=httpx.MockTransport(handler))
    with TestClient(app) as c:
        seed(cfg.db_path)
        with db.session(cfg.db_path) as conn:     # cached details alone (an earlier fetch) do not make it known
            conn.execute("INSERT INTO tmdb_details(tmdb_id,json) VALUES(8888,?)", (json.dumps({"title": "Cached"}),))
        app.state.engine.invalidate()
        assert c.get("/api/films/777777").status_code == 404
        assert c.post("/api/films/777777/seen", json={"seen": True}).status_code == 404
        assert c.post("/api/films/8888/seen", json={"seen": True}).status_code == 404
        assert calls == []
        assert c.get("/api/films/101").status_code == 200              # on Letterboxd: known
        assert c.post("/api/films/101/seen", json={"seen": True}).status_code == 200


# ---------------------------------------------------------------- #66: session revocation

def test_logout_revokes_a_copied_cookie_even_after_a_restart(tmp_path):
    client, issuer = test_auth.make(tmp_path)
    with client:
        q = test_auth.start_login(client, issuer)
        client.get("/login/callback", params={"state": q["state"][0], "code": "abc"})
        copied = client.cookies.get(auth.SESSION_COOKIE)
        assert client.get("/api/status").status_code == 200
        other = client.app.state.sessions.sign("oidc:user-1")   # another device, still signed in
        client.get("/logout")
        client.cookies.clear()
        client.cookies.set(auth.SESSION_COOKIE, copied)
        assert client.get("/api/status").status_code == 401
        client.cookies.set(auth.SESSION_COOKIE, other)
        assert client.get("/api/status").status_code == 200
    client2, _ = test_auth.make(tmp_path)
    with client2:
        assert client2.app.state.sessions.check(copied) is None
        assert client2.app.state.sessions.check(other) is not None


def test_revoke_all_voids_every_session_and_old_cookies_stay_valid():
    s = auth.Sessions(b"k" * 32, True)
    a = s.sign("oidc:x")
    # a cookie signed before session ids and epochs existed is still good at epoch 0
    payload = auth._b64(json.dumps({"sub": "oidc:x", "name": "", "exp": 2**40}).encode())
    legacy = f"{payload}.{s._mac(payload)}"
    assert s.check(a) and s.check(legacy)
    saved = []
    s._persist = lambda ep, rev: saved.append((ep, dict(rev)))
    s.revoke_all()
    assert s.check(a) is None and s.check(legacy) is None and s.check(s.sign("oidc:x"))
    assert saved == [(1, {})]


def test_new_plex_owner_voids_old_sessions(tmp_path):
    client, plex_tv, cfg = test_auth_plex.make(tmp_path, plex_owner="Alice")
    with client:
        assert test_auth_plex.sign_in(client).status_code == 303
        before = client.app.state.sessions.epoch
    client2, plex_tv2, _ = test_auth_plex.make(tmp_path, plex_owner="bob")
    with client2:
        old = client2.app.state.sessions.sign("plex:1001")
        plex_tv2.next_token = "tok-b"
        assert test_auth_plex.sign_in(client2).status_code == 303
        assert client2.app.state.sessions.epoch == before + 1
        assert client2.app.state.sessions.check(old) is None
        assert client2.get("/api/status").status_code == 200        # the new owner's own session


# ---------------------------------------------------------------- #66: TV-code quota per client

class _FakePlex:
    def __init__(self):
        self.n = 0

    async def create_pin(self, http, client_id, strong=True):
        self.n += 1
        return {"id": self.n, "code": f"c{self.n}", "expires_in": 600}


def test_tv_code_quota_is_per_client_too(monkeypatch):
    monkeypatch.setattr(auth, "CODE_STARTS_PER_CLIENT", 2)
    login = auth.PlexLogin("https://app.example.test", None, "cid", _FakePlex())

    async def run():
        await login.start_code("/", "10.0.0.1")
        await login.start_code("/", "10.0.0.1")
        with pytest.raises(auth.TooManyCodes):
            await login.start_code("/", "10.0.0.1")
        await login.start_code("/", "10.0.0.2")       # another client still gets a code

    asyncio.run(run())
