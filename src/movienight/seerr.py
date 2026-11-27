"""Overseerr / Jellyseerr ("Seerr", same API v1): request films missing from the library, as each user.

There is no API key. Every signed-in user connects their own Seerr account through Plex: MovieNight asks plex.tv
for a PIN, the user approves it at app.plex.tv, and the Plex token that comes back is handed straight to Seerr
(`POST /auth/plex`). Only Seerr's session cookie (`connect.sid`) is kept, per user; the Plex token is never
stored. Requests and statuses then go to Seerr with that cookie, so Seerr applies the user's own permissions.

Statuses: one paged read of `GET /media` (films only), cached per user for an hour. Cookie values are never
logged.
"""
from __future__ import annotations

import asyncio
import http.cookiejar
import logging
import secrets
import time
from pathlib import Path

import httpx

from . import db, plex
from .util import now_iso

log = logging.getLogger("movienight.seerr")
PLEX_BASE = "https://plex.tv"   # overridable (tests)
TIMEOUT = httpx.Timeout(8.0, connect=3.0)
PIN_SECONDS = 600.0             # a connect attempt must come back within this long
CACHE_SECONDS = 3600.0          # a user's statuses
PAGE_SIZE = 100
MAX_PAGES = 200                 # 20 000 media items, a safety stop
COOKIE = "connect.sid"

# The app's states, from Seerr's MediaStatus: 1 unknown, 2 pending, 3 processing, 4 partially available,
# 5 available (6 blocklisted / 7 deleted in Jellyseerr count as nothing).
NONE, REQUESTED, AVAILABLE = "none", "requested", "available"
STATES = (NONE, REQUESTED, AVAILABLE)
_MEDIA = {2: REQUESTED, 3: REQUESTED, 4: AVAILABLE, 5: AVAILABLE}


class SeerrError(RuntimeError):
    """A clean, user-facing message (never the URL or the cookie)."""

    def __init__(self, message: str, status: int = 502, code: str = "seerr.failed", **params):
        super().__init__(message)
        self.status = status
        self.code = code            # the web UI words the message itself (catalogue `apiError.<code>`)
        self.params = params


class SessionExpired(SeerrError):
    def __init__(self):
        super().__init__("Your Seerr session expired. Reconnect Seerr in Setup.", 409, "seerr.expired")


class CannotRequest(SeerrError):
    def __init__(self):
        super().__init__("Your Seerr account can't request movies", 403, "seerr.cannotRequest")


class NotConnected(SeerrError):
    def __init__(self):
        super().__init__("Connect your Seerr account first (Setup)", 409, "seerr.notConnected")


class _NoCookies(http.cookiejar.CookieJar):
    """The Seerr client keeps no cookies of its own: each call carries one user's cookie explicitly, so one
    user's session can never leak into another user's call."""

    def extract_cookies(self, response, request):  # noqa: D102
        return None

    def set_cookie(self, cookie):  # noqa: D102
        return None


def new_client(transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=transport, follow_redirects=False, timeout=TIMEOUT,
                             cookies=_NoCookies())


def state_of(status) -> str:
    return _MEDIA.get(status, NONE)


def _name(user: dict) -> str:
    for k in ("displayName", "plexUsername", "username", "jellyfinUsername"):
        v = user.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()[:80]
    return "Seerr user"


def _plex_headers(client_id: str) -> dict:
    return {"X-Plex-Product": "MovieNight", "X-Plex-Client-Identifier": client_id, "Accept": "application/json"}


class Accounts:
    """Per-user Seerr sessions: the connect flow, statuses and requests."""

    def __init__(self, client: httpx.AsyncClient, url: str, db_path: Path | str, clock=time.monotonic,
                 ttl: float = CACHE_SECONDS, pin_ttl: float = PIN_SECONDS):
        self.client = client
        self.url = url.strip().rstrip("/")
        self.db_path = db_path
        self.clock = clock
        self.ttl = ttl
        self.pin_ttl = pin_ttl
        self.pins: dict[int, tuple[str, str, float]] = {}          # pin id -> (user id, client id, created)
        self._cache: dict[str, tuple[dict[int, str], float]] = {}   # user id -> ({tmdb: state}, expires)
        self._locks: dict[str, asyncio.Lock] = {}

    # ---- stored sessions
    def _row(self, user: str):
        with db.session(self.db_path) as c:
            return c.execute("SELECT cookie, seerr_name, expired FROM seerr_sessions WHERE user_id=?",
                             (user,)).fetchone()

    def status(self, user: str) -> dict:
        row = self._row(user)
        return {"enabled": True, "connected": bool(row) and not row["expired"], "expired": bool(row and row["expired"]),
                "name": row["seerr_name"] if row else None}

    def _expire(self, user: str) -> None:
        with db.session(self.db_path) as c:
            c.execute("UPDATE seerr_sessions SET expired=1 WHERE user_id=?", (user,))
        self.invalidate(user)
        log.info("Seerr: a stored session was refused; the user is asked to reconnect")

    def invalidate(self, user: str) -> None:
        self._cache.pop(user, None)

    def _cookie(self, user: str) -> str:
        row = self._row(user)
        if not row:
            raise NotConnected()
        if row["expired"]:
            raise SessionExpired()
        return row["cookie"]

    async def _seerr(self, method: str, path: str, cookie: str | None = None, **kw) -> httpx.Response:
        headers = {"Accept": "application/json"}
        if cookie:
            headers["Cookie"] = f"{COOKIE}={cookie}"
        try:
            return await self.client.request(method, self.url + path, headers=headers, **kw)
        except httpx.TimeoutException:
            raise SeerrError("Seerr did not answer in time", 504, "seerr.timeout") from None
        except httpx.HTTPError:
            raise SeerrError("Could not reach Seerr", 502, "seerr.unreachable") from None

    # ---- connect (Plex PIN -> Seerr session)
    def _prune(self) -> None:
        now = self.clock()
        for pid in [p for p, (_, _, at) in self.pins.items() if now - at > self.pin_ttl]:
            self.pins.pop(pid, None)

    async def start(self, user: str, origin: str) -> str:
        """A new Plex PIN for this user; returns the app.plex.tv URL that sends them back to the callback."""
        self._prune()
        client_id = secrets.token_hex(16)
        try:
            r = await self.client.post(f"{PLEX_BASE}/api/v2/pins", params={"strong": "true"},
                                       headers=_plex_headers(client_id))
            r.raise_for_status()
            pin = r.json()
            pin_id, code = int(pin["id"]), str(pin["code"])
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            raise SeerrError("plex.tv could not be reached. Try again in a moment.", 502, "plex.unreachable") from None
        self.pins[pin_id] = (user, client_id, self.clock())
        forward = f"{origin.rstrip('/')}/api/seerr/callback?pin={pin_id}"
        return plex.auth_url(client_id, code, forward)

    async def finish(self, user: str, pin_id: int) -> str:
        """Completes a connect: Plex token -> Seerr session cookie, stored for this user. Returns the Seerr
        name. Raises SeerrError on any failure."""
        pending = self.pins.get(pin_id)
        if not pending or pending[0] != user:          # unknown, or someone else's: left alone
            raise SeerrError("Unknown or foreign connect attempt", 400, "seerr.badAttempt")
        self.pins.pop(pin_id, None)
        _, client_id, created = pending
        if self.clock() - created > self.pin_ttl:
            raise SeerrError("The connect attempt expired", 400, "seerr.attemptExpired")
        try:
            r = await self.client.get(f"{PLEX_BASE}/api/v2/pins/{int(pin_id)}", headers=_plex_headers(client_id))
            r.raise_for_status()
            token = r.json().get("authToken")
        except (httpx.HTTPError, ValueError, AttributeError):
            raise SeerrError("plex.tv could not be reached", 502, "plex.unreachable") from None
        if not token:
            raise SeerrError("Plex did not confirm the sign-in", 400, "seerr.plexUnconfirmed")
        r = await self._seerr("POST", "/auth/plex", json={"authToken": token})
        del token
        if r.status_code >= 400:
            raise SeerrError(f"Seerr refused the Plex sign-in ({r.status_code})", 502, "seerr.refused", http=r.status_code)
        cookie = r.cookies.get(COOKIE)
        if not cookie:
            raise SeerrError("Seerr did not start a session", 502, "seerr.noSession")
        try:
            data = r.json()
        except ValueError:
            data = {}
        name = _name(data if isinstance(data, dict) else {})
        with db.session(self.db_path) as c:
            c.execute("INSERT INTO seerr_sessions(user_id, cookie, seerr_name, connected_at, expired) VALUES(?,?,?,?,0)"
                      " ON CONFLICT(user_id) DO UPDATE SET cookie=excluded.cookie, seerr_name=excluded.seerr_name,"
                      " connected_at=excluded.connected_at, expired=0", (user, cookie, name, now_iso()))
        self.invalidate(user)
        return name

    async def disconnect(self, user: str) -> None:
        row = self._row(user)
        with db.session(self.db_path) as c:
            c.execute("DELETE FROM seerr_sessions WHERE user_id=?", (user,))
        self.invalidate(user)
        if row and not row["expired"]:
            try:                                        # best effort: end the session on Seerr too
                await self._seerr("POST", "/auth/logout", cookie=row["cookie"])
            except SeerrError:
                pass

    # ---- statuses and requests
    async def statuses(self, user: str) -> dict[int, str]:
        """{tmdb id: requested | available} for every film Seerr knows (others are `none`), cached an hour."""
        hit = self._cache.get(user)
        if hit and hit[1] > self.clock():
            return hit[0]
        lock = self._locks.setdefault(user, asyncio.Lock())
        async with lock:
            hit = self._cache.get(user)
            if hit and hit[1] > self.clock():
                return hit[0]
            cookie = self._cookie(user)
            out: dict[int, str] = {}
            skip = 0
            for _ in range(MAX_PAGES):
                r = await self._seerr("GET", "/media", cookie=cookie,
                                      params={"take": PAGE_SIZE, "skip": skip, "filter": "all"})
                if r.status_code in (401, 403):
                    self._expire(user)
                    raise SessionExpired()
                if r.status_code >= 400:
                    raise SeerrError(f"Seerr answered with an error ({r.status_code})", 502, "seerr.error", http=r.status_code)
                try:
                    data = r.json()
                    results = data.get("results") or []
                    total = int((data.get("pageInfo") or {}).get("results") or 0)
                except (ValueError, AttributeError, TypeError):
                    raise SeerrError("Seerr sent an unreadable answer", 502, "seerr.unreadable") from None
                for m in results:
                    if not isinstance(m, dict) or m.get("mediaType") != "movie":
                        continue
                    try:
                        tid = int(m.get("tmdbId") or 0)
                    except (TypeError, ValueError):
                        continue
                    state = state_of(m.get("status"))
                    if tid > 0 and state != NONE:
                        out[tid] = state
                skip += len(results)
                if not results or skip >= total:
                    break
            self._cache[user] = (out, self.clock() + self.ttl)
            return out

    async def request(self, user: str, tmdb_id: int) -> str:
        """Ask Seerr for the film as this user. Returns the new state (409, already requested, counts as done)."""
        cookie = self._cookie(user)
        r = await self._seerr("POST", "/request", cookie=cookie, json={"mediaType": "movie", "mediaId": int(tmdb_id)})
        if r.status_code == 401:
            self._expire(user)
            raise SessionExpired()
        if r.status_code == 403:
            raise CannotRequest()
        if r.status_code >= 400 and r.status_code != 409:
            raise SeerrError(f"Seerr answered with an error ({r.status_code})", 502, "seerr.error", http=r.status_code)
        self.invalidate(user)
        state = REQUESTED
        if r.status_code != 409:
            try:
                media = (r.json() or {}).get("media") or {}
                if state_of(media.get("status")) == AVAILABLE:
                    state = AVAILABLE
            except (ValueError, AttributeError):
                pass
        return state
