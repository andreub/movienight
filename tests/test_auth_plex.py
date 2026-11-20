"""Sign in with Plex: plex.tv faked through httpx.MockTransport."""
from urllib.parse import parse_qs, urlparse

import httpx
from fastapi.testclient import TestClient

from movienight import auth, db, library
from movienight.app import create_app
from movienight.config import Config

USERS = {"tok-a": {"id": 1001, "username": "Alice", "email": "alice@example.test"},
         "tok-b": {"id": 2002, "username": "bob", "email": "bob@example.test"}}


class PlexTv:
    def __init__(self):
        self.next_token = "tok-a"      # what the next approved PIN yields; None = not approved
        self.pins = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/api/v2/pins":
            self.pins += 1
            self.last_strong = request.url.params.get("strong") == "true"
            return httpx.Response(201, json={"id": 500 + self.pins, "code": f"code{self.pins}", "expiresIn": 900})
        if path.startswith("/api/v2/pins/"):
            return httpx.Response(200, json={"authToken": self.next_token})
        if path == "/api/v2/user":
            user = USERS.get(request.headers.get("X-Plex-Token", ""))
            return httpx.Response(200, json=user) if user else httpx.Response(401)
        if path == "/api/v2/resources":
            return httpx.Response(200, json=[])
        return httpx.Response(404)


def make(tmp_path, **over):
    plex_tv = PlexTv()
    cfg = Config(data_dir=tmp_path, letterboxd_dir=tmp_path / "none", sync_on_start=False,
                 **{"login": "plex", "public_url": "https://app.example.test", **over})
    client = TestClient(create_app(cfg, transport=httpx.MockTransport(plex_tv)),
                        base_url="https://app.example.test", follow_redirects=False)
    return client, plex_tv, cfg


def sign_in(client, next_path="/"):
    r = client.get("/login/plex", params={"next": next_path})
    assert r.status_code == 303 and r.headers["location"].startswith("https://app.plex.tv/auth#?")
    fragment = parse_qs(r.headers["location"].split("#?", 1)[1])
    forward = urlparse(fragment["forwardUrl"][0])
    assert forward.geturl().startswith("https://app.example.test/login/plex/callback?state=")
    return client.get("/login/plex/callback", params={"state": parse_qs(forward.query)["state"][0]})


def test_first_sign_in_owns_the_app_and_connects_plex(tmp_path):
    client, plex_tv, _ = make(tmp_path)
    with client:
        assert client.get("/api/status").status_code == 401
        assert client.get("/").headers["location"] == "/login?next=%2F"
        assert "Sign in with Plex" in client.get("/login").text

        r = sign_in(client, "/#/browse")
        assert r.status_code == 303 and r.headers["location"] == "/#/browse"
        assert "HttpOnly" in r.headers["set-cookie"] and "Secure" in r.headers["set-cookie"]
        assert client.get("/api/me").json() == {"auth": True, "name": "Alice"}
        status = client.get("/api/status").json()
        assert status["plex"]["connected"] and status["plex"]["account"] == "Alice"

        # someone else's Plex account is refused, and the owner's session is untouched
        other = TestClient(client.app, base_url="https://app.example.test", follow_redirects=False)
        plex_tv.next_token = "tok-b"
        r = sign_in(other)
        assert r.status_code == 403 and "set-cookie" not in r.headers
        assert other.get("/api/status").status_code == 401
        assert client.get("/api/status").status_code == 200

        assert client.get("/logout").headers["location"] == "/logged-out"


def test_an_already_connected_library_decides_the_owner(tmp_path):
    client, plex_tv, cfg = make(tmp_path)
    with client:
        with db.session(cfg.db_path) as c:
            library.set_plex_token(c, "tok-a", "Alice")
        plex_tv.next_token = "tok-b"
        assert sign_in(client).status_code == 403
        plex_tv.next_token = "tok-a"
        assert sign_in(client).status_code == 303
        with db.session(cfg.db_path) as c:
            assert db.get_meta(c, "plex_token") == "tok-a" and db.get_meta(c, "plex_owner_id") == "1001"


def test_configured_owner_by_username_or_email(tmp_path):
    client, plex_tv, _ = make(tmp_path, plex_owner="ALICE")
    with client:
        plex_tv.next_token = "tok-b"
        assert sign_in(client).status_code == 403          # first to arrive, but not the owner
        plex_tv.next_token = "tok-a"
        assert sign_in(client).status_code == 303
    assert auth.is_owner({"id": "1", "name": "x", "email": "Bob@Example.test"}, "bob@example.test")
    assert not auth.is_owner({"id": "1", "name": "", "email": ""}, "")


def test_unapproved_pin_reused_state_and_stale_session(tmp_path):
    client, plex_tv, cfg = make(tmp_path)
    with client:
        plex_tv.next_token = None
        assert sign_in(client).status_code == 403
        assert client.get("/login/plex/callback", params={"state": "made-up"}).status_code == 403
        plex_tv.next_token = "tok-a"
        r = client.get("/login/plex")
        state = parse_qs(urlparse(parse_qs(r.headers["location"].split("#?", 1)[1])["forwardUrl"][0]).query)["state"][0]
        assert client.get("/login/plex/callback", params={"state": state}).status_code == 303
        assert client.get("/login/plex/callback", params={"state": state}).status_code == 403   # single use
        assert client.get("/api/status").status_code == 200
        with db.session(cfg.db_path) as c:                  # the app now belongs to someone else
            db.set_meta(c, "plex_owner_id", "2002")
        assert client.get("/api/status").status_code == 401


def test_open_redirect_is_not_possible_through_next(tmp_path):
    client, _, _ = make(tmp_path)
    with client:
        assert sign_in(client, "https://evil.test/").headers["location"] == "/"


# --------------------------------------------------------------------------- TV code sign-in

def start_code(client):
    r = client.post("/login/plex/code", params={"next": "/#/browse"})
    assert r.status_code == 200 and auth.CODE_COOKIE in r.headers["set-cookie"]
    return r.json()


def test_tv_code_pending_then_ok_sets_the_session(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "CODE_POLL_SECONDS", 0)
    client, plex_tv, _ = make(tmp_path)
    with client:
        assert "Sign in with a code (TV)" in client.get("/login").text
        assert client.get("/login/plex/tv").status_code == 200
        d = start_code(client)
        assert plex_tv.last_strong is False                  # plex.tv/link only takes the short codes
        assert d["code"] == "code1" and d["expires_at"] > 0 and "500" not in d["id"]   # the PIN id stays server-side
        plex_tv.next_token = None
        r = client.get(f"/login/plex/code/{d['id']}")
        assert r.status_code == 200 and r.json() == {"status": "pending"} and "set-cookie" not in r.headers
        plex_tv.next_token = "tok-a"
        r = client.get(f"/login/plex/code/{d['id']}")
        assert r.status_code == 200 and r.json() == {"status": "ok", "next": "/#/browse"}
        assert auth.SESSION_COOKIE in r.headers["set-cookie"]
        assert client.get("/api/me").json() == {"auth": True, "name": "Alice"}
        assert client.get(f"/login/plex/code/{d['id']}").status_code == 410             # single use


def test_tv_code_wrong_account_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "CODE_POLL_SECONDS", 0)
    client, plex_tv, _ = make(tmp_path, plex_owner="alice")
    with client:
        plex_tv.next_token = "tok-b"
        d = start_code(client)
        r = client.get(f"/login/plex/code/{d['id']}")
        assert r.status_code == 403 and r.json()["status"] == "denied"
        assert auth.SESSION_COOKIE not in r.headers.get("set-cookie", "")
        assert client.get("/api/status").status_code == 401


def test_tv_code_expires(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "CODE_POLL_SECONDS", 0)
    client, plex_tv, _ = make(tmp_path)
    with client:
        d = start_code(client)
        client.app.state.plex_login._codes[d["id"]]["expires"] = 0
        r = client.get(f"/login/plex/code/{d['id']}")
        assert r.status_code == 410 and r.json()["status"] == "expired"
        assert client.get("/login/plex/code/made-up").status_code == 410


def test_tv_code_cannot_be_polled_by_another_client(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "CODE_POLL_SECONDS", 0)
    client, plex_tv, _ = make(tmp_path)
    with client:
        d = start_code(client)
        other = TestClient(client.app, base_url="https://app.example.test", follow_redirects=False)
        r = other.get(f"/login/plex/code/{d['id']}")
        assert r.status_code == 403 and auth.SESSION_COOKIE not in r.headers.get("set-cookie", "")
        start_code(other)                                    # its own code does not unlock the first one
        assert other.get(f"/login/plex/code/{d['id']}").status_code == 403
        assert client.get(f"/login/plex/code/{d['id']}").json()["status"] == "ok"      # the owner still can


def test_tv_codes_are_rate_limited(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "CODE_STARTS_PER_MINUTE", 2)
    client, _, _ = make(tmp_path)
    with client:
        start_code(client)
        start_code(client)
        assert client.post("/login/plex/code").status_code == 429
