"""Login with an OIDC provider: a fake issuer served through httpx.MockTransport."""
import json
import time
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from movienight import auth
from movienight.app import create_app
from movienight.config import Config

ISSUER = "https://id.example.test"
CLIENT = "client-123"
KEY = ec.generate_private_key(ec.SECP256R1())
OTHER_KEY = ec.generate_private_key(ec.SECP256R1())


class Issuer:
    """Answers discovery, JWKS and the token endpoint; remembers what the app sent."""

    def __init__(self):
        self.sub, self.nonce, self.key, self.aud = "user-1", None, KEY, CLIENT
        self.token_form = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/.well-known/openid-configuration":
            return httpx.Response(200, json={
                "issuer": ISSUER, "authorization_endpoint": ISSUER + "/authorize",
                "token_endpoint": ISSUER + "/token", "jwks_uri": ISSUER + "/jwks",
                "end_session_endpoint": ISSUER + "/end"})
        if path == "/jwks":
            jwk = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(KEY.public_key()))
            return httpx.Response(200, json={"keys": [{**jwk, "kid": "k1", "use": "sig", "alg": "ES256"}]})
        if path == "/token":
            self.token_form = parse_qs(request.content.decode())
            now = int(time.time())
            claims = {"iss": ISSUER, "aud": self.aud, "sub": self.sub, "nonce": self.nonce,
                      "iat": now, "exp": now + 300, "name": "Test User"}
            return httpx.Response(200, json={
                "id_token": jwt.encode(claims, self.key, algorithm="ES256", headers={"kid": "k1"})})
        return httpx.Response(404)


def make(tmp_path, **over):
    issuer = Issuer()
    cfg = Config(data_dir=tmp_path, letterboxd_dir=tmp_path / "none", sync_on_start=False,
                 **{"oidc_issuer": ISSUER, "oidc_client_id": CLIENT, "oidc_allowed_subs": "user-1",
                    "public_url": "https://app.example.test", **over})
    client = TestClient(create_app(cfg, transport=httpx.MockTransport(issuer)),
                        base_url="https://app.example.test", follow_redirects=False)
    return client, issuer


def start_login(client, issuer, next_path="/"):
    r = client.get("/login/oidc", params={"next": next_path})
    assert r.status_code == 303
    q = parse_qs(urlparse(r.headers["location"]).query)
    issuer.nonce = q["nonce"][0]
    return q


def test_everything_needs_a_session_except_health(tmp_path):
    client, _ = make(tmp_path)
    with client:
        assert client.get("/healthz").status_code == 200
        r = client.get("/api/status")
        assert r.status_code == 401 and r.json() == {"detail": "Not signed in", "code": "auth.signedOut", "params": {}}
        r = client.get("/?x=1")
        assert r.status_code == 303 and r.headers["location"] == "/login?next=%2F%3Fx%3D1"
        assert client.get("/static/js/main.js").status_code == 303
        assert client.post("/api/sync", json={"what": "all"}).status_code == 401


def test_full_login_sets_cookie_and_opens_the_app(tmp_path):
    client, issuer = make(tmp_path)
    with client:
        q = start_login(client, issuer, "/#/browse")
        assert q["client_id"] == [CLIENT] and q["code_challenge_method"] == ["S256"]
        assert q["redirect_uri"] == ["https://app.example.test/login/callback"]
        r = client.get("/login/callback", params={"state": q["state"][0], "code": "abc"})
        assert r.status_code == 303 and r.headers["location"] == "/#/browse"
        cookie = r.headers["set-cookie"]
        assert "HttpOnly" in cookie and "Secure" in cookie and "samesite=lax" in cookie.lower()
        assert "code_verifier" in issuer.token_form and "client_secret" not in issuer.token_form
        assert client.get("/api/status").status_code == 200
        assert client.get("/api/me").json() == {"auth": True, "name": "Test User"}
        # the state is single use
        assert client.get("/login/callback", params={"state": q["state"][0], "code": "abc"}).status_code == 403
        r = client.get("/logout")
        assert r.status_code == 303 and r.headers["location"].startswith(ISSUER + "/end")
        client.cookies.clear()
        assert client.get("/api/status").status_code == 401


@pytest.mark.parametrize("change", ["sub", "nonce", "key", "aud", "state"])
def test_login_refused(tmp_path, change):
    client, issuer = make(tmp_path)
    with client:
        q = start_login(client, issuer)
        state = q["state"][0]
        if change == "sub":
            issuer.sub = "someone-else"
        elif change == "nonce":
            issuer.nonce = "forged"
        elif change == "key":
            issuer.key = OTHER_KEY
        elif change == "aud":
            issuer.aud = "another-client"
        else:
            state = "made-up"
        r = client.get("/login/callback", params={"state": state, "code": "abc"})
        assert r.status_code == 403 and "set-cookie" not in r.headers
        assert client.get("/api/status").status_code == 401


def test_cookie_tampering_and_expiry(tmp_path):
    client, issuer = make(tmp_path)
    with client:
        sessions = client.app.state.sessions
        good = sessions.sign("oidc:user-1", "N")
        assert sessions.check(good).sub == "oidc:user-1"
        payload, mac = good.split(".")
        assert sessions.check(payload + "." + mac[:-2] + "AA") is None
        assert sessions.check("") is None and sessions.check("a.b.c") is None
        assert sessions.check(auth.Sessions(b"\0" * 32, True).sign("oidc:user-1")) is None
        # signed by us but no longer on the allow-list, or of an unknown kind: no access
        for sub in ("oidc:user-2", "plex:1", "user-1"):
            client.cookies.set(auth.SESSION_COOKIE, sessions.sign(sub))
            assert client.get("/api/status").status_code == 401
        client.cookies.set(auth.SESSION_COOKIE, good)
        assert client.get("/api/status").status_code == 200
        real = time.time
        try:
            time.time = lambda: real() + (auth.SESSION_DAYS + 1) * 86400
            assert sessions.check(good) is None
        finally:
            time.time = real


def test_session_key_survives_a_restart(tmp_path):
    client, _ = make(tmp_path)
    with client:
        cookie = client.app.state.sessions.sign("oidc:user-1")
    client2, _ = make(tmp_path)
    with client2:
        assert client2.app.state.sessions.check(cookie) is not None


def test_login_page_lists_the_methods(tmp_path):
    client, _ = make(tmp_path, login="plex", oidc_name="Pocket ID")
    with client:
        r = client.get("/login", params={"next": "/#/taste"})
        assert r.status_code == 200
        assert "Sign in with Plex" in r.text and "/login/plex?next=%2F%23%2Ftaste" in r.text
        assert "Sign in with Pocket ID" in r.text and "/login/oidc?next=" in r.text
    client, _ = make(tmp_path, login="plex", oidc_client_id="")       # OIDC half-configured: Plex only
    with client:
        r = client.get("/login")
        assert "Sign in with Plex" in r.text and "/login/oidc" not in r.text
        assert client.get("/login/oidc").headers["location"] == "/login"


@pytest.mark.parametrize("bad", ["https://evil.test/", "//evil.test", "/\\evil.test", "evil", ""])
def test_next_cannot_leave_the_site(bad):
    assert auth.safe_next(bad) == "/"
    assert auth.safe_next("/#/taste") == "/#/taste"


def test_locked_until_the_client_id_is_set(tmp_path):
    client, _ = make(tmp_path, oidc_client_id="")
    with client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/api/status").status_code == 503
        assert client.get("/").status_code == 503


def test_no_issuer_means_no_login(tmp_path):
    client, _ = make(tmp_path, oidc_issuer="")
    with client:
        assert client.get("/api/status").status_code == 200
        assert client.get("/api/me").json() == {"auth": False, "name": None}
        assert client.get("/login").status_code == 303
        assert client.get("/login/plex").status_code == 303


def test_login_none_overrides_the_issuer(tmp_path):
    client, _ = make(tmp_path, login="none")
    with client:
        assert client.get("/api/status").status_code == 200
