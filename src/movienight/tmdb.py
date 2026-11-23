"""TMDB client: search/resolve, details, recommendations. Credentials never logged."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import httpx

from .util import norm_title

log = logging.getLogger("movienight.tmdb")
API = "https://api.themoviedb.org/3"
IMG = "https://image.tmdb.org/t/p"
TIMEOUT = httpx.Timeout(15.0, connect=5.0)


def poster_url(path: str | None) -> str | None:
    return f"{IMG}/w500{path}" if path else None


def backdrop_url(path: str | None) -> str | None:
    return f"{IMG}/w1280{path}" if path else None


class TmdbError(RuntimeError):
    pass


class Tmdb:
    def __init__(self, client: httpx.AsyncClient, token: str, concurrency: int = 8, sleep=asyncio.sleep):
        self.client = client
        self.token = token.strip()
        self.sem = asyncio.Semaphore(concurrency)
        self.sleep = sleep

    @property
    def configured(self) -> bool:
        return bool(self.token)

    def _auth(self, params: dict | None) -> tuple[dict, dict]:
        headers = {"Accept": "application/json"}
        params = dict(params or {})
        if self.token.startswith("eyJ"):
            headers["Authorization"] = f"Bearer {self.token}"
        else:
            params["api_key"] = self.token
        return headers, params

    async def get(self, path: str, params: dict | None = None) -> dict | None:
        """GET with bounded concurrency and retries. Returns None on 404."""
        headers, params = self._auth(params)
        async with self.sem:
            for attempt in range(5):
                try:
                    r = await self.client.get(API + path, params=params, headers=headers, timeout=TIMEOUT)
                except httpx.TransportError:
                    if attempt >= 2:
                        raise
                    await self.sleep(1 + attempt)
                    continue
                if r.status_code == 429:
                    try:
                        wait = float(r.headers.get("Retry-After", "2"))
                    except ValueError:
                        wait = 2.0
                    await self.sleep(min(max(wait, 0.5), 30.0))
                    continue
                if r.status_code >= 500 and attempt < 2:
                    await self.sleep(1 + attempt)
                    continue
                if r.status_code == 404:
                    return None
                if r.status_code in (401, 403):
                    raise TmdbError("TMDB credentials were rejected")
                r.raise_for_status()
                return r.json()
        raise TmdbError("TMDB rate limit: giving up for now")

    async def search(self, title: str, year: int | None) -> int | None:
        """Resolve (title, year) to a TMDB id; tries year, year-1, year+1."""
        want = norm_title(title)
        if not want:
            return None
        years: list[int | None] = [year, year - 1, year + 1] if year else [None]
        for y in years:
            params = {"query": title, "include_adult": "false"}
            if y:
                params["year"] = y
            data = await self.get("/search/movie", params)
            hit = pick_search_result(data, want)
            if hit:
                return hit
        return None

    async def details(self, tmdb_id: int) -> dict | None:
        raw = await self.get(f"/movie/{int(tmdb_id)}", {"append_to_response": "credits,keywords"})
        return compact_details(raw) if raw else None

    async def recommendations(self, tmdb_id: int) -> list[int]:
        data = await self.get(f"/movie/{int(tmdb_id)}/recommendations")
        return [r["id"] for r in (data or {}).get("results", []) if r.get("id")]

    async def collection(self, cid: int, today: str | None = None) -> dict | None:
        """A TMDB collection (saga): name and released parts, oldest first. None when TMDB has no such id."""
        raw = await self.get(f"/collection/{int(cid)}")
        return compact_collection(raw, today) if raw else None

    async def search_collections(self, query: str) -> list[dict]:
        """Collections whose name matches `query` (first page): [{id, name}]."""
        data = await self.get("/search/collection", {"query": query, "include_adult": "false"})
        return [{"id": r["id"], "name": r.get("name") or ""} for r in (data or {}).get("results") or [] if r.get("id")]

    async def keyword_movies(self, kid: int, today: str | None = None) -> dict:
        """Films tagged with a keyword (first page, oldest first): {total, parts} with parts shaped like a
        collection's. `total` is TMDB's raw count, so a caller can tell a short series from a theme keyword."""
        data = await self.get("/discover/movie", {"with_keywords": str(int(kid)), "include_adult": "false",
                                                  "include_video": "false", "sort_by": "primary_release_date.asc"})
        data = data or {}
        return {"total": int(data.get("total_results") or 0),
                "parts": compact_collection({"parts": data.get("results") or []}, today)["parts"]}


def pick_search_result(data: dict | None, want_norm: str) -> int | None:
    results = (data or {}).get("results") or []
    for r in results:
        if want_norm in (norm_title(r.get("title")), norm_title(r.get("original_title"))):
            return r["id"]
    if len(want_norm) >= 6:
        for r in results:
            t = norm_title(r.get("title"))
            if t and (want_norm in t or t in want_norm) and min(len(t), len(want_norm)) >= 6:
                return r["id"]
    return None


def compact_collection(raw: dict, today: str | None = None) -> dict:
    """Parts with a release date up to `today` (YYYY-MM-DD, default now), sorted by date: unreleased ones are dropped."""
    today = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    parts = []
    for p in raw.get("parts") or []:
        date = p.get("release_date") or ""
        if not p.get("id") or not date or date > today:
            continue
        parts.append({"id": p["id"], "title": p.get("title") or p.get("original_title") or "",
                      "year": int(date[:4]) if date[:4].isdigit() else None, "release_date": date,
                      "poster_path": p.get("poster_path"), "backdrop_path": p.get("backdrop_path"),
                      "vote_count": p.get("vote_count") or 0, "vote_average": p.get("vote_average")})
    parts.sort(key=lambda p: (p["release_date"], p["id"]))
    return {"id": raw.get("id"), "name": raw.get("name") or "", "poster_path": raw.get("poster_path"),
            "backdrop_path": raw.get("backdrop_path"), "parts": parts}


def compact_details(raw: dict) -> dict:
    credits = raw.get("credits") or {}
    crew = credits.get("crew") or []
    cast = sorted(credits.get("cast") or [], key=lambda c: c.get("order", 999))
    kws = (raw.get("keywords") or {}).get("keywords") or []
    date = raw.get("release_date") or ""
    coll = raw.get("belongs_to_collection") or None
    return {
        "id": raw.get("id"),
        "title": raw.get("title") or "",
        "original_title": raw.get("original_title") or raw.get("title") or "",
        "year": int(date[:4]) if date[:4].isdigit() else None,
        "release_date": date,
        "runtime": raw.get("runtime") or None,
        "tagline": raw.get("tagline") or "",
        "overview": raw.get("overview") or "",
        "genres": [g["name"] for g in raw.get("genres") or [] if g.get("name")],
        "directors": [c["name"] for c in crew if c.get("job") == "Director" and c.get("name")],
        "director_ids": [c["id"] for c in crew if c.get("job") == "Director" and c.get("id")],
        "cast": [c["name"] for c in cast[:12] if c.get("name")],
        "cast_ids": [c["id"] for c in cast[:5] if c.get("id")],
        # always present (null = no saga), so the sync can tell "no collection" from "fetched before this existed"
        "collection": {"id": coll["id"], "name": coll.get("name") or ""} if coll and coll.get("id") else None,
        "keywords": [k["name"] for k in kws if k.get("name")],
        # parallel to `keywords`; always present, so the sync can backfill details stored before it existed
        "keyword_ids": [k.get("id") for k in kws if k.get("name")],
        "language": raw.get("original_language") or None,
        "countries": [c["iso_3166_1"] for c in raw.get("production_countries") or [] if c.get("iso_3166_1")],
        "vote_average": raw.get("vote_average"),
        "vote_count": raw.get("vote_count"),
        "poster_path": raw.get("poster_path"),
        "backdrop_path": raw.get("backdrop_path"),
        "imdb_id": raw.get("imdb_id"),
    }
