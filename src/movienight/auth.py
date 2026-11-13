"""Optional login with an OpenID Connect provider (e.g. Pocket ID): public client + PKCE.

Off while MOVIENIGHT_OIDC_ISSUER is empty (local use). With an issuer set, everything except
the health check and the login pages needs a session, and the app answers 503 until the
client id is set too: it never runs open by accident.

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
PUBLIC_PATHS = {"/healthz", "/login", "/login/callback", "/logout", "/logged-out",
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
    sub: str
    name: str


class OIDC:
    def __init__(self, issuer: str, client_id: str, public_url: str, allowed_subs: str,
                 key: bytes, http: httpx.AsyncClient):
        self.issuer = issuer.rstrip("/")
        self.client_id = client_id
        self.redirect_uri = public_url.rstrip("/") + "/login/callback"
        self.secure = self.redirect_uri.startswith("https://")
        self.allowed = {s.strip() for s in allowed_subs.split(",") if s.strip()}
        self.key = key
        self.http = http
        self._pending: dict[str, tuple[str, str, str, float]] = {}  # state -> verifier, nonce, next, created
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
        state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
        now = time.time()
        live = {k: v for k, v in self._pending.items() if v[3] > now - PENDING_SECONDS}
        while len(live) >= MAX_PENDING:                      # bounded: drop the oldest
            live.pop(min(live, key=lambda k: live[k][3]))
        live[state] = (verifier, nonce, safe_next(next_path), now)
        self._pending = live
        q = {"response_type": "code", "client_id": self.client_id, "redirect_uri": self.redirect_uri,
             "scope": "openid profile", "state": state, "nonce": nonce,
             "code_challenge": _b64(hashlib.sha256(verifier.encode()).digest()),
             "code_challenge_method": "S256"}
        return (await self.meta())["authorization_endpoint"] + "?" + urllib.parse.urlencode(q)

    async def finish(self, state: str, code: str) -> tuple[str, str]:
        """Returns (session cookie value, next path). Raises LoginError if not allowed."""
        pending = self._pending.pop(state, None)
        if not pending or pending[3] < time.time() - PENDING_SECONDS:
            raise LoginError("This login expired. Try again.")
        verifier, nonce, next_path, _ = pending
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
        if self.allowed and claims["sub"] not in self.allowed:
            raise LoginError("This account is not allowed to use MovieNight")
        name = str(claims.get("name") or claims.get("preferred_username") or "")[:80]
        return self.sign(claims["sub"], name), next_path

    async def logout_url(self, fallback: str) -> str:
        """Also end the provider's own session when it has a logout page."""
        try:
            end = (await self.meta()).get("end_session_endpoint")
        except Exception:  # noqa: BLE001
            end = None
        return end + "?" + urllib.parse.urlencode({"client_id": self.client_id}) if end else fallback

    # --- session cookie ----------------------------------------------------------------
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
            if data["exp"] <= time.time() or (self.allowed and data["sub"] not in self.allowed):
                return None
            return Session(data["sub"], data.get("name", ""))
        except Exception:  # noqa: BLE001
            return None

    def cookie_kwargs(self) -> dict:
        return {"httponly": True, "secure": self.secure, "samesite": "lax", "path": "/"}
