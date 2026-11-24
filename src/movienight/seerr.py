"""Overseerr / Jellyseerr ("Seerr", same API v1): request films missing from the library.

Only two calls: the request state of a film (`GET /api/v1/movie/{tmdbId}`) and a new request
(`POST /api/v1/request`). States are cached in memory for a few minutes, so a Complete view with many
missing films asks Seerr once per film, a few at a time. The API key is never logged.
"""
from __future__ import annotations

import asyncio
import logging
import time

import httpx

log = logging.getLogger("movienight.seerr")
TIMEOUT = httpx.Timeout(5.0, connect=3.0)
CACHE_SECONDS = 300.0
BATCH_CAP = 40          # ids looked up per batch call; the rest are ignored
CONCURRENCY = 4         # Seerr calls in flight at once, over every caller

# What the app shows, from Seerr's MediaStatus (mediaInfo.status):
#   1 unknown, 2 pending, 3 processing, 4 partially available, 5 available, 6 blocklisted, 7 deleted (Jellyseerr)
# and, when the media status says nothing yet, its requests' MediaRequestStatus:
#   1 pending approval, 2 approved, 3 declined, 4 failed, 5 completed
NONE, PENDING, PROCESSING, AVAILABLE, BLOCKED = "none", "pending", "processing", "available", "blocked"
STATES = (NONE, PENDING, PROCESSING, AVAILABLE, BLOCKED)
_MEDIA = {2: PENDING, 3: PROCESSING, 4: AVAILABLE, 5: AVAILABLE, 6: BLOCKED}
_REQUEST = {1: PENDING, 2: PROCESSING, 5: PROCESSING}


class SeerrError(RuntimeError):
    """A clean, user-facing message (never the URL or the key)."""

    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


def status_of(data: dict | None) -> str:
    """The app's state for Seerr's movie payload (the 4K status is ignored)."""
    info = (data or {}).get("mediaInfo") or {}
    state = _MEDIA.get(info.get("status"))
    if state:
        return state
    reqs = [r for r in info.get("requests") or [] if isinstance(r, dict) and not r.get("is4k")]
    found = {_REQUEST.get(r.get("status")) for r in reqs} - {None}
    for s in (PROCESSING, PENDING):
        if s in found:
            return s
    return NONE


def request_state(data: dict | None) -> str:
    """The state right after a request, from the MediaRequest Seerr returns (auto-approved: processing)."""
    data = data or {}
    if data.get("status") == 2:
        return PROCESSING
    media = _MEDIA.get((data.get("media") or {}).get("status"))
    return media if media in (PROCESSING, AVAILABLE) else PENDING


class Seerr:
    def __init__(self, client: httpx.AsyncClient, url: str, api_key: str, clock=time.monotonic,
                 ttl: float = CACHE_SECONDS, concurrency: int = CONCURRENCY):
        self.client = client
        self.url = url.strip().rstrip("/")
        self.key = api_key.strip()
        self.clock = clock
        self.ttl = ttl
        self.sem = asyncio.Semaphore(concurrency)
        self._cache: dict[int, tuple[str, float]] = {}

    @property
    def configured(self) -> bool:
        return bool(self.url and self.key)

    def _headers(self) -> dict:
        return {"X-Api-Key": self.key, "Accept": "application/json"}

    def cached(self, tmdb_id: int) -> str | None:
        hit = self._cache.get(tmdb_id)
        if hit and hit[1] > self.clock():
            return hit[0]
        self._cache.pop(tmdb_id, None)
        return None

    def remember(self, tmdb_id: int, state: str) -> str:
        self._cache[tmdb_id] = (state, self.clock() + self.ttl)
        return state

    async def _call(self, method: str, path: str, json: dict | None = None) -> httpx.Response:
        async with self.sem:
            try:
                return await self.client.request(method, self.url + "/api/v1" + path, json=json,
                                                 headers=self._headers(), timeout=TIMEOUT)
            except httpx.TimeoutException:
                raise SeerrError("Seerr did not answer in time", 504) from None
            except httpx.HTTPError:
                raise SeerrError("Could not reach Seerr", 502) from None

    @staticmethod
    def _check(r: httpx.Response) -> None:
        if r.status_code == 401:
            raise SeerrError("Seerr rejected the API key (MOVIENIGHT_SEERR_API_KEY)", 502)
        if r.status_code == 403:
            raise SeerrError("Seerr refused: the API key's user may not request films", 502)
        if r.status_code >= 400:
            raise SeerrError(f"Seerr answered with an error ({r.status_code})", 502)

    async def movie_status(self, tmdb_id: int, fresh: bool = False) -> str:
        """none | pending | processing | available | blocked. A film Seerr does not know is `none`."""
        tmdb_id = int(tmdb_id)
        if not fresh and (hit := self.cached(tmdb_id)):
            return hit
        r = await self._call("GET", f"/movie/{tmdb_id}")
        if r.status_code == 404:
            return self.remember(tmdb_id, NONE)
        self._check(r)
        try:
            data = r.json()
        except ValueError:
            raise SeerrError("Seerr sent an unreadable answer", 502) from None
        return self.remember(tmdb_id, status_of(data))

    async def statuses(self, tmdb_ids: list[int]) -> dict[int, str]:
        """States of up to BATCH_CAP films (cached ones first, no Seerr call). A film whose lookup fails is left
        out; if every lookup failed, the first error is raised."""
        ids = list(dict.fromkeys(int(i) for i in tmdb_ids if int(i) > 0))[:BATCH_CAP]
        out: dict[int, str] = {}
        todo = []
        for i in ids:
            hit = self.cached(i)
            if hit:
                out[i] = hit
            else:
                todo.append(i)
        results = await asyncio.gather(*(self.movie_status(i) for i in todo), return_exceptions=True)
        errors = []
        for i, res in zip(todo, results):
            if isinstance(res, BaseException):
                errors.append(res)
            else:
                out[i] = res
        if errors and not out:
            first = errors[0]
            raise first if isinstance(first, SeerrError) else SeerrError("Could not reach Seerr", 502)
        return out

    async def request(self, tmdb_id: int) -> str:
        """Ask Seerr for the film. Returns the new state; one already requested (409) counts as requested."""
        tmdb_id = int(tmdb_id)
        r = await self._call("POST", "/request", json={"mediaType": "movie", "mediaId": tmdb_id})
        if r.status_code == 409:
            self._cache.pop(tmdb_id, None)
            try:
                state = await self.movie_status(tmdb_id, fresh=True)
            except SeerrError:
                state = NONE
            return self.remember(tmdb_id, state if state != NONE else PENDING)
        self._check(r)
        try:
            data = r.json()
        except ValueError:
            data = {}
        return self.remember(tmdb_id, request_state(data))
