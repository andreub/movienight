"""Seerr (Overseerr / Jellyseerr), per user, no API key: connecting through a Plex PIN, statuses from /media
(paging, mapping, cache), requests and their errors, disconnecting, the off switch and migration 15.
plex.tv and Seerr are mock transports: no real instance is ever contacted."""
import json
import sqlite3
from urllib.parse import parse_qs, unquote, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from movienight import db, seerr
from movienight.app import create_app
from movienight.config import Config

from conftest import no_network

SEERR = "https://seerr.example.test/api/v1"
PLEX = "https://plex.example.test"
ORIGIN = "https://movienight.example.test"
COOKIE = "s%3Afake-session.signature"


class Fake:
    """plex.tv and Seerr over one mock transport, with a log of calls and switchable answers."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.pin_headers: list[dict] = []
        self.auth_token = "plex-token-123"          # what plex.tv hands out for an approved PIN (None: not approved)
        self.auth_answer = (200, {"id": 7, "displayName": "Alice", "plexUsername": "alice"}, COOKIE)
        self.auth_bodies: list[dict] = []
        self.media: list[dict] = []
        self.media_fail: int | None = None
        self.request_answer = (201, {"id": 1, "status": 1, "media": {"status": 2}})
        self.request_bodies: list[dict] = []
        self.cookies: list[str | None] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        self.calls.append((request.method, path))
        if host == "plex.example.test":
            self.pin_headers.append(dict(request.headers))
            if request.method == "POST" and path == "/api/v2/pins":
                assert request.url.params.get("strong") == "true"
                return httpx.Response(201, json={"id": 4242, "code": "abcd1234"})
            if request.method == "GET" and path == "/api/v2/pins/4242":
                return httpx.Response(200, json={"id": 4242, "authToken": self.auth_token})
            return httpx.Response(404)
        if host != "seerr.example.test":
            return no_network(request)
        self.cookies.append(request.headers.get("cookie"))
        assert "x-api-key" not in request.headers
        if request.method == "POST" and path == "/api/v1/auth/plex":
            self.auth_bodies.append(json.loads(request.content))
            code, body, cookie = self.auth_answer
            headers = {"set-cookie": f"connect.sid={cookie}; Path=/; HttpOnly"} if cookie else {}
            return httpx.Response(code, json=body, headers=headers)
        if request.method == "POST" and path == "/api/v1/auth/logout":
            return httpx.Response(200, json={"status": "ok"})
        if request.method == "GET" and path == "/api/v1/media":
            if self.media_fail:
                return httpx.Response(self.media_fail, json={"message": "nope"})
            take, skip = int(request.url.params["take"]), int(request.url.params["skip"])
            assert request.url.params["filter"] == "all"
            page = self.media[skip:skip + take]
            return httpx.Response(200, json={"pageInfo": {"pages": -(-len(self.media) // take), "page": skip // take + 1,
                                                          "results": len(self.media)}, "results": page})
        if request.method == "POST" and path == "/api/v1/request":
            self.request_bodies.append(json.loads(request.content))
            code, body = self.request_answer
            return httpx.Response(code, json=body)
        return httpx.Response(404)

    def seerr_calls(self, path):
        return [c for c in self.calls if c[1] == "/api/v1" + path]


@pytest.fixture(autouse=True)
def plex_base(monkeypatch):
    monkeypatch.setattr(seerr, "PLEX_BASE", PLEX)


@pytest.fixture
def fake():
    return Fake()


def make_client(tmp_path, fake, **over):
    cfg = Config(data_dir=tmp_path / "data", letterboxd_dir=tmp_path / "nolb", sync_on_start=False,
                 seerr_url=SEERR, **over)
    return TestClient(create_app(cfg, transport=httpx.MockTransport(fake)), follow_redirects=False)


@pytest.fixture
def sc(tmp_path, fake):
    with make_client(tmp_path, fake) as c:
        yield c


def connect(c, origin=ORIGIN):
    r = c.post("/api/seerr/connect", headers={"Origin": origin} if origin else {})
    assert r.status_code == 200, r.text
    return r.json()["url"]


def connected(c):
    connect(c)
    r = c.get("/api/seerr/callback", params={"pin": 4242})
    assert r.status_code == 303 and r.headers["location"] == "/?seerr=connected"
    return r


def row(c, user="local"):
    with db.session(c.app.state.cfg.db_path) as conn:
        return conn.execute("SELECT * FROM seerr_sessions WHERE user_id=?", (user,)).fetchone()


# ---- connect

def test_status_before_connecting(sc):
    assert sc.get("/api/seerr/status").json() == {"enabled": True, "connected": False, "expired": False, "name": None}
    assert sc.get("/api/status").json()["features"]["seerr"] is True


def test_connect_creates_a_pin_and_the_plex_url(sc, fake):
    url = connect(sc)
    assert url.startswith("https://app.plex.tv/auth#?")
    q = parse_qs(url.split("#?", 1)[1])
    assert q["code"] == ["abcd1234"]
    assert q["context[device][product]"] == ["MovieNight"]
    assert q["forwardUrl"] == [f"{ORIGIN}/api/seerr/callback?pin=4242"]
    client_id = q["clientID"][0]
    assert len(client_id) == 32
    h = fake.pin_headers[0]
    assert h["x-plex-product"] == "MovieNight" and h["x-plex-client-identifier"] == client_id
    assert h["accept"] == "application/json"
    user, cid, _ = sc.app.state.seerr.pins[4242]
    assert (user, cid) == ("local", client_id)
    # each attempt gets a fresh client id
    q2 = parse_qs(connect(sc).split("#?", 1)[1])
    assert q2["clientID"][0] != client_id


def test_connect_without_origin_uses_the_request_url(sc):
    q = parse_qs(connect(sc, origin=None).split("#?", 1)[1])
    assert unquote(q["forwardUrl"][0]) == "http://testserver/api/seerr/callback?pin=4242"


def test_callback_stores_the_cookie_and_never_the_plex_token(sc, fake):
    connected(sc)
    assert fake.auth_bodies == [{"authToken": "plex-token-123"}]
    # the pin check used the same client id as the pin
    assert fake.pin_headers[1]["x-plex-client-identifier"] == fake.pin_headers[0]["x-plex-client-identifier"]
    r = row(sc)
    assert r["cookie"] == COOKIE and r["seerr_name"] == "Alice" and r["expired"] == 0
    assert sc.get("/api/seerr/status").json() == {"enabled": True, "connected": True, "expired": False, "name": "Alice"}
    assert 4242 not in sc.app.state.seerr.pins
    dump = "\n".join(sqlite3.connect(sc.app.state.cfg.db_path).iterdump())
    assert "plex-token-123" not in dump
    # the Seerr client keeps no cookie jar of its own: one user's session never rides along another's call
    assert not sc.app.state.seerr.client.cookies


def failed(c, pin=4242):
    r = c.get("/api/seerr/callback", params={"pin": pin})
    assert r.status_code == 303 and r.headers["location"] == "/?seerr=failed"
    assert row(c) is None


def test_callback_with_another_users_pin_fails(sc, fake):
    sc.app.state.seerr.pins[4242] = ("plex:someone-else", "f" * 32, sc.app.state.seerr.clock())
    failed(sc)
    assert 4242 in sc.app.state.seerr.pins          # theirs is left alone
    assert not fake.seerr_calls("/auth/plex")


def test_callback_with_an_unknown_pin_fails(sc):
    failed(sc, pin=999)


def test_callback_with_an_expired_pin_fails(sc, fake):
    connect(sc)
    accounts = sc.app.state.seerr
    user, cid, at = accounts.pins[4242]
    accounts.pins[4242] = (user, cid, at - seerr.PIN_SECONDS - 1)
    failed(sc)
    assert not fake.seerr_calls("/auth/plex")


def test_callback_without_an_auth_token_fails(sc, fake):
    fake.auth_token = None
    connect(sc)
    failed(sc)
    assert not fake.seerr_calls("/auth/plex")


def test_callback_without_a_seerr_cookie_fails(sc, fake):
    fake.auth_answer = (200, {"id": 7, "displayName": "Alice"}, None)
    connect(sc)
    failed(sc)


def test_callback_refused_by_seerr_fails(sc, fake):
    fake.auth_answer = (403, {"message": "Access denied."}, None)
    connect(sc)
    failed(sc)


def test_pin_is_single_use(sc):
    connected(sc)
    r = sc.get("/api/seerr/callback", params={"pin": 4242})
    assert r.headers["location"] == "/?seerr=failed"
    assert row(sc)["expired"] == 0                  # the stored session is untouched


# ---- statuses

def media(n, start=1):
    return [{"mediaType": "movie", "tmdbId": start + i, "status": 1 + i % 7} for i in range(n)]


def test_statuses_page_map_and_cache(sc, fake):
    connected(sc)
    fake.media = media(230) + [{"mediaType": "tv", "tmdbId": 9000, "status": 5}]
    r = sc.get("/api/seerr/statuses", params={"tmdb": "1,2,3,4,5,6,7,9000,99999"})
    assert r.status_code == 200
    assert r.json()["statuses"] == {"1": "none", "2": "requested", "3": "requested", "4": "available",
                                    "5": "available", "6": "none", "7": "none", "9000": "none", "99999": "none"}
    assert len(fake.seerr_calls("/media")) == 3     # 100 + 100 + 31
    assert all(c == f"connect.sid={COOKIE}" for c in fake.cookies[-3:])
    # cached for the hour: no more Seerr calls
    sc.get("/api/seerr/statuses", params={"tmdb": "230"})
    assert len(fake.seerr_calls("/media")) == 3
    # a request invalidates it
    sc.post("/api/seerr/request/1")
    sc.get("/api/seerr/statuses", params={"tmdb": "1"})
    assert len(fake.seerr_calls("/media")) == 6


def test_statuses_cache_expires(sc, fake):
    connected(sc)
    now = [1000.0]
    sc.app.state.seerr.clock = lambda: now[0]
    fake.media = media(3)
    sc.get("/api/seerr/statuses", params={"tmdb": "2"})
    now[0] += seerr.CACHE_SECONDS - 1
    sc.get("/api/seerr/statuses", params={"tmdb": "2"})
    assert len(fake.seerr_calls("/media")) == 1
    now[0] += 2
    sc.get("/api/seerr/statuses", params={"tmdb": "2"})
    assert len(fake.seerr_calls("/media")) == 2


@pytest.mark.parametrize("code", [401, 403])
def test_statuses_refused_marks_the_session_expired(sc, fake, code):
    connected(sc)
    fake.media_fail = code
    r = sc.get("/api/seerr/statuses", params={"tmdb": "1"})
    assert r.status_code == 409 and "Reconnect" in r.json()["detail"]
    assert row(sc)["expired"] == 1
    assert sc.get("/api/seerr/status").json() == {"enabled": True, "connected": False, "expired": True, "name": "Alice"}
    # reconnecting clears it
    fake.media_fail = None
    connected(sc)
    assert row(sc)["expired"] == 0


def test_statuses_need_a_connection(sc, fake):
    assert sc.get("/api/seerr/statuses", params={"tmdb": "1"}).status_code == 409
    assert not fake.seerr_calls("/media")


def test_statuses_seerr_error(sc, fake):
    connected(sc)
    fake.media_fail = 500
    r = sc.get("/api/seerr/statuses", params={"tmdb": "1"})
    assert r.status_code == 502 and row(sc)["expired"] == 0


# ---- requests

def test_request_success(sc, fake):
    connected(sc)
    r = sc.post("/api/seerr/request/603")
    assert r.status_code == 200 and r.json() == {"status": "requested"}
    assert fake.request_bodies == [{"mediaType": "movie", "mediaId": 603}]
    assert fake.cookies[-1] == f"connect.sid={COOKIE}"


def test_request_409_counts_as_success(sc, fake):
    connected(sc)
    fake.request_answer = (409, {"message": "Request for this media already exists."})
    assert sc.post("/api/seerr/request/603").json() == {"status": "requested"}


def test_request_401_marks_expired(sc, fake):
    connected(sc)
    fake.request_answer = (401, {"message": "Unauthorized"})
    r = sc.post("/api/seerr/request/603")
    assert r.status_code == 409 and "Reconnect" in r.json()["detail"]
    assert row(sc)["expired"] == 1
    # then nothing more goes to Seerr until a reconnect
    n = len(fake.calls)
    assert sc.post("/api/seerr/request/603").status_code == 409
    assert len(fake.calls) == n


def test_request_403_cannot_request(sc, fake):
    connected(sc)
    fake.request_answer = (403, {"message": "Forbidden"})
    r = sc.post("/api/seerr/request/603")
    assert r.status_code == 403 and r.json()["detail"] == "Your Seerr account can't request movies"
    assert row(sc)["expired"] == 0


def test_request_needs_a_connection(sc, fake):
    assert sc.post("/api/seerr/request/603").status_code == 409
    assert not fake.request_bodies


# ---- disconnect, sign-in, off switch, config, migration

def test_disconnect(sc, fake):
    connected(sc)
    fake.media = media(3)
    sc.get("/api/seerr/statuses", params={"tmdb": "2"})
    assert sc.delete("/api/seerr/session").json() == {"ok": True}
    assert row(sc) is None
    assert fake.seerr_calls("/auth/logout")
    assert sc.get("/api/seerr/status").json()["connected"] is False
    assert sc.get("/api/seerr/statuses", params={"tmdb": "2"}).status_code == 409
    assert sc.delete("/api/seerr/session").status_code == 200     # idempotent


def test_routes_need_a_sign_in_when_login_is_on(tmp_path, fake):
    with make_client(tmp_path, fake, login="plex", public_url="https://movienight.example.test") as c:
        for method, path in [("GET", "/api/seerr/status"), ("POST", "/api/seerr/connect"),
                             ("GET", "/api/seerr/callback?pin=1"), ("DELETE", "/api/seerr/session"),
                             ("GET", "/api/seerr/statuses?tmdb=1"), ("POST", "/api/seerr/request/1")]:
            assert c.request(method, path).status_code == 401, path
    assert not fake.calls


def test_feature_disabled_when_url_unset(client):
    assert client.get("/api/seerr/status").json() == {"enabled": False, "connected": False, "expired": False,
                                                      "name": None}
    assert client.get("/api/status").json()["features"]["seerr"] is False
    for method, path in [("POST", "/api/seerr/connect"), ("GET", "/api/seerr/callback?pin=1"),
                         ("DELETE", "/api/seerr/session"), ("GET", "/api/seerr/statuses?tmdb=1"),
                         ("POST", "/api/seerr/request/1")]:
        assert client.request(method, path).status_code == 404, path


def test_config_from_env(monkeypatch):
    monkeypatch.setenv("MOVIENIGHT_SEERR_URL", "https://seerr.example.test/api/v1/")
    monkeypatch.setenv("MOVIENIGHT_SEERR_API_KEY", "ignored")
    cfg = Config.from_env()
    assert cfg.seerr_url == "https://seerr.example.test/api/v1" and cfg.seerr_configured
    assert not hasattr(cfg, "seerr_api_key")
    monkeypatch.delenv("MOVIENIGHT_SEERR_URL")
    assert not Config.from_env().seerr_configured


def test_migration_15(tmp_path):
    path = tmp_path / "m.db"
    db.migrate(path)
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 15
    cols = {r[1]: r for r in conn.execute("PRAGMA table_info(seerr_sessions)")}
    assert set(cols) == {"user_id", "cookie", "seerr_name", "connected_at", "expired"}
    assert cols["user_id"][5] == 1                                  # primary key
    conn.execute("INSERT INTO seerr_sessions(user_id, cookie, connected_at) VALUES('u', 'c', 'now')")
    assert conn.execute("SELECT expired FROM seerr_sessions").fetchone()[0] == 0
