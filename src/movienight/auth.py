"""Login: with the Plex account the app belongs to (default) and/or an OpenID Connect provider
(e.g. Pocket ID: public client + PKCE).

With a login method on, everything except the health check and the login pages needs a
session. If OIDC is the only method and its client id is missing, the app answers 503: it
never runs open by accident.

The session is an HMAC-signed cookie. Its key lives in the database, so a restart or a
redeploy does not sign everyone out.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import secrets
import time
import urllib.parse
from dataclasses import dataclass

import httpx
import jwt

SESSION_COOKIE = "movienight_session"
SESSION_DAYS = 30
PENDING_SECONDS = 900
MAX_PENDING = 200
PUBLIC_PATHS = {"/healthz", "/login", "/login/oidc", "/login/callback", "/login/plex",
                "/login/plex/callback", "/logout", "/logged-out",
                "/static/favicon.svg"}


class LoginError(Exception):
    """The login could not be completed; the message is safe to show."""


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def safe_next(path: str | None) -> str:
    """Only same-site paths: never an absolute or protocol-relative URL (open redirect)."""
    if not path or not path.startswith("/") or path.startswith("//") or "\\" in path:
        return "/"
    return path


@dataclass
class Session:
    sub: str            # "plex:<account id>" or "oidc:<sub claim>"
    name: str


class Sessions:
    """Signs and checks the session cookie."""

    def __init__(self, key: bytes, secure: bool):
        self.key = key
        self.secure = secure

    def _mac(self, payload: str) -> str:
        return _b64(hmac.new(self.key, payload.encode(), hashlib.sha256).digest())

    def sign(self, sub: str, name: str = "") -> str:
        data = {"sub": sub, "name": name, "exp": int(time.time()) + SESSION_DAYS * 86400}
        payload = _b64(json.dumps(data).encode())
        return f"{payload}.{self._mac(payload)}"

    def check(self, cookie_value: str | None) -> Session | None:
        try:
            payload, mac = (cookie_value or "").split(".")
            if not hmac.compare_digest(mac, self._mac(payload)):
                return None
            data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
            if data["exp"] <= time.time():
                return None
            return Session(str(data["sub"]), data.get("name", ""))
        except Exception:  # noqa: BLE001
            return None

    def cookie_kwargs(self) -> dict:
        return {"httponly": True, "secure": self.secure, "samesite": "lax", "path": "/"}


class Pending:
    """Logins that were started and not finished yet: state -> data, short-lived and bounded."""

    def __init__(self):
        self._items: dict[str, tuple[tuple, float]] = {}

    def add(self, *data) -> str:
        now = time.time()
        live = {k: v for k, v in self._items.items() if v[1] > now - PENDING_SECONDS}
        while len(live) >= MAX_PENDING:                      # drop the oldest
            live.pop(min(live, key=lambda k: live[k][1]))
        state = secrets.token_urlsafe(24)
        live[state] = (data, now)
        self._items = live
        return state

    def pop(self, state: str) -> tuple:
        item = self._items.pop(state, None)
        if not item or item[1] < time.time() - PENDING_SECONDS:
            raise LoginError("This login expired. Try again.")
        return item[0]


class OIDC:
    def __init__(self, issuer: str, client_id: str, public_url: str, allowed_subs: str,
                 http: httpx.AsyncClient):
        self.issuer = issuer.rstrip("/")
        self.client_id = client_id
        self.redirect_uri = public_url.rstrip("/") + "/login/callback"
        self.allowed = {s.strip() for s in allowed_subs.split(",") if s.strip()}
        self.http = http
        self._pending = Pending()                            # verifier, nonce, next
        self._meta: dict | None = None
        self._lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        return bool(self.client_id)

    # --- discovery ---------------------------------------------------------------------
    async def meta(self) -> dict:
        async with self._lock:
            if self._meta is None:
                r = await self.http.get(f"{self.issuer}/.well-known/openid-configuration")
                r.raise_for_status()
                meta = r.json()
                if meta.get("issuer", "").rstrip("/") != self.issuer:
                    raise LoginError("The login provider's issuer does not match the configuration")
                self._meta = meta
            return self._meta

    async def _signing_key(self, id_token: str):
        meta = await self.meta()
        r = await self.http.get(meta["jwks_uri"])
        r.raise_for_status()
        kid = jwt.get_unverified_header(id_token).get("kid")
        for k in jwt.PyJWKSet.from_dict(r.json()).keys:
            if kid is None or k.key_id == kid:
                return k.key
        raise LoginError("The login provider signed with an unknown key")

    # --- login flow --------------------------------------------------------------------
    async def login_url(self, next_path: str | None) -> str:
        verifier = secrets.token_urlsafe(48)
        nonce = secrets.token_urlsafe(24)
        state = self._pending.add(verifier, nonce, safe_next(next_path))
        q = {"response_type": "code", "client_id": self.client_id, "redirect_uri": self.redirect_uri,
             "scope": "openid profile", "state": state, "nonce": nonce,
             "code_challenge": _b64(hashlib.sha256(verifier.encode()).digest()),
             "code_challenge_method": "S256"}
        return (await self.meta())["authorization_endpoint"] + "?" + urllib.parse.urlencode(q)

    async def finish(self, state: str, code: str) -> tuple[Session, str]:
        """Returns (who signed in, next path). Raises LoginError if not allowed."""
        verifier, nonce, next_path = self._pending.pop(state)
        meta = await self.meta()
        r = await self.http.post(meta["token_endpoint"], data={
            "grant_type": "authorization_code", "code": code, "redirect_uri": self.redirect_uri,
            "client_id": self.client_id, "code_verifier": verifier})
        if r.status_code != 200:
            raise LoginError("The login provider rejected the sign-in")
        id_token = r.json().get("id_token")
        if not id_token:
            raise LoginError("The login provider sent no identity")
        try:
            claims = jwt.decode(id_token, await self._signing_key(id_token),
                                algorithms=["RS256", "ES256", "EdDSA"],
                                audience=self.client_id, issuer=self.issuer,
                                options={"require": ["exp", "iat", "sub"]})
        except jwt.PyJWTError as e:
            raise LoginError("The identity sent by the login provider is not valid") from e
        if not hmac.compare_digest(str(claims.get("nonce", "")), nonce):
            raise LoginError("This login does not match the one that was started")
        if not self.allows(claims["sub"]):
            raise LoginError("This account is not allowed to use MovieNight")
        name = str(claims.get("name") or claims.get("preferred_username") or "")[:80]
        return Session("oidc:" + claims["sub"], name), next_path

    def allows(self, sub: str) -> bool:
        return not self.allowed or sub in self.allowed

    async def logout_url(self, fallback: str) -> str:
        """Also end the provider's own session when it has a logout page."""
        try:
            end = (await self.meta()).get("end_session_endpoint")
        except Exception:  # noqa: BLE001
            end = None
        return end + "?" + urllib.parse.urlencode({"client_id": self.client_id}) if end else fallback


class PlexLogin:
    """Sign in with Plex: a PIN that plex.tv sends back to us through `forwardUrl`.

    Only the Plex account this MovieNight belongs to gets in. `owner()` returns its account
    id (or None while nobody owns it yet: the first sign-in then becomes the owner)."""

    def __init__(self, public_url: str, http: httpx.AsyncClient, client_id: str, plex_api):
        self.callback = public_url.rstrip("/") + "/login/plex/callback"
        self.http = http
        self.client_id = client_id
        self.plex = plex_api
        self._pending = Pending()                            # pin id, next

    async def login_url(self, next_path: str | None) -> str:
        pin = await self.plex.create_pin(self.http, self.client_id)
        state = self._pending.add(pin["id"], safe_next(next_path))
        forward = self.callback + "?" + urllib.parse.urlencode({"state": state})
        return self.plex.auth_url(self.client_id, pin["code"], forward)

    async def finish(self, state: str) -> tuple[dict, str, str]:
        """Returns (Plex account {"id", "name", "email"}, its token, next path)."""
        pin_id, next_path = self._pending.pop(state)
        token = await self.plex.check_pin(self.http, self.client_id, pin_id)
        if not token:
            raise LoginError("Plex did not confirm the sign-in. Try again.")
        account = await self.plex.account(self.http, self.client_id, token)
        if not account or not account["id"]:
            raise LoginError("Plex did not say which account signed in. Try again.")
        return account, token, next_path


def is_owner(account: dict, owner: str) -> bool:
    """`owner` is a Plex account id, username or email (case-insensitive)."""
    want = owner.strip().lower()
    return bool(want) and want in {str(account.get(k) or "").lower() for k in ("id", "name", "email")}
