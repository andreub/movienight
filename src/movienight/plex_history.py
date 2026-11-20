"""Fix the plex.tv watch-history entry a scrobble creates (MOVIENIGHT_PLEX_HISTORY_FIX).

Scrobbling a film on a (shared) server with the user's own token makes plex.tv add a WATCH_HISTORY
activity dated "now" to the user's profile. After a push we find that new entry and either move it to
the real watch date (Letterboxd diary) or, when no date is known, remove it (the film stays played on
the server). Best effort: nothing here raises; failures are logged and the seen push is unaffected.

The community.plex.tv GraphQL API is undocumented (reverse-engineered from the public Plex web app),
so field names may change; every step degrades to "do nothing". Tokens never reach the logs.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

import httpx

from . import plex
from .db import get_meta, set_meta
from .util import norm_title, parse_iso, safe_error

log = logging.getLogger("movienight.plexhistory")

COMMUNITY_API = "https://community.plex.tv/api"
RECENT = timedelta(minutes=10)      # only entries this fresh can be ours; older real plays are never touched
SCAN = 20                           # newest history nodes inspected
LOOKUP_TRIES = 3                    # the entry can show up a moment after the scrobble
LOOKUP_DELAY = 2.0
MUTATION_DELAY = 1.5                # pacing between mutations (the API rate-limits)
MAX_RETRY_AFTER = 10.0
_sleep = asyncio.sleep
TIMEOUT = httpx.Timeout(15.0, connect=5.0)

Q_FULL = ("query GetWatchHistoryHub($uuid: ID = \"\", $first: PaginationInt!, $after: String) "
          "{ user(id: $uuid) { watchHistory(first: $first, after: $after) { nodes { id date "
          "metadataItem { id title type year } } pageInfo { hasNextPage endCursor } } } }")
Q_MIN = Q_FULL.replace("metadataItem { id title type year }", "metadataItem { id title type }")
M_DATE = ("mutation updateActivityDate($id: ID!, $input: UpdateActivityInput!) "
          "{ updateActivity(id: $id, input: $input) { id } }")
M_REMOVE = "mutation removeActivity($input: RemoveActivityInput!) { removeActivity(input: $input) }"


class GraphQLError(Exception):
    def __init__(self, errors: list, retry_after: float | None = None):
        self.errors, self.retry_after = errors, retry_after
        super().__init__("; ".join(str((e or {}).get("message") or "error")[:80] for e in errors))

    @property
    def schema_error(self) -> bool:
        return any("Cannot query field" in str((e or {}).get("message")) or
                   ((e or {}).get("extensions") or {}).get("code") == "GRAPHQL_VALIDATION_FAILED"
                   for e in self.errors)


def iso_date(day: str | None) -> str | None:
    """A Letterboxd 'YYYY-MM-DD' watch date as the ISO instant plex.tv expects (evening, UTC)."""
    try:
        d = datetime.strptime((day or "")[:10], "%Y-%m-%d")
    except ValueError:
        return None
    return d.strftime("%Y-%m-%dT20:00:00.000Z")


async def _gql(http: httpx.AsyncClient, client_id: str, token: str, query: str, variables: dict, op: str) -> dict:
    h = plex.base_headers(client_id, token)
    h.update({"Content-Type": "application/json", "X-Plex-Platform": "Web"})
    r = await http.post(COMMUNITY_API, json={"query": query, "variables": variables, "operationName": op},
                        headers=h, timeout=TIMEOUT)
    r.raise_for_status()
    body = r.json()
    errors = body.get("errors") or []
    if errors:
        retry = None
        for e in errors:
            ext = (e or {}).get("extensions") or {}
            if ext.get("code") == "RATE_LIMITED":
                try:
                    retry = float(ext.get("retryAfter") or 1)
                except (TypeError, ValueError):
                    retry = 1.0
        raise GraphQLError(errors, retry)
    return body.get("data") or {}


async def _call(http, client_id, token, query, variables, op) -> dict:
    """One GraphQL call; a rate limit is waited out once (bounded)."""
    try:
        return await _gql(http, client_id, token, query, variables, op)
    except GraphQLError as e:
        if e.retry_after is None:
            raise
        await _sleep(min(e.retry_after, MAX_RETRY_AFTER))
        return await _gql(http, client_id, token, query, variables, op)


async def user_uuid(conn: sqlite3.Connection, http: httpx.AsyncClient, client_id: str, token: str) -> str:
    """The plex.tv user uuid of the token's account, cached in meta."""
    cached = get_meta(conn, "plex_uuid")
    if cached:
        return cached
    acc = await plex.account(http, client_id, token)
    uuid = (acc or {}).get("uuid") or ""
    if uuid:
        set_meta(conn, "plex_uuid", uuid)
        conn.commit()
    return uuid


async def recent_history(http, client_id, token, uuid: str) -> list[dict]:
    variables = {"uuid": uuid, "first": SCAN}
    try:
        data = await _call(http, client_id, token, Q_FULL, variables, "GetWatchHistoryHub")
    except GraphQLError as e:
        if not e.schema_error:
            raise
        data = await _call(http, client_id, token, Q_MIN, variables, "GetWatchHistoryHub")
    return (((data.get("user") or {}).get("watchHistory") or {}).get("nodes")) or []


def find_entry(nodes: list[dict], title: str, year: int | None, taken: set, now: datetime) -> dict | None:
    """The newest fresh, not yet claimed entry for this film (title, and year when both sides have it)."""
    want = norm_title(title)
    for n in nodes[:SCAN]:
        mi = n.get("metadataItem") or {}
        when = parse_iso(n.get("date"))
        if not n.get("id") or n["id"] in taken or when is None or abs(now - when) > RECENT:
            continue
        if str(mi.get("type") or "movie").lower() != "movie" or norm_title(mi.get("title")) != want:
            continue
        y = mi.get("year")
        if year and y and int(y) != int(year):
            continue
        return n
    return None


async def fix_new_activities(conn: sqlite3.Connection, http: httpx.AsyncClient, client_id: str, token: str,
                             items: list[dict]) -> dict:
    """items: [{"title", "year", "date"}] just marked played; date is 'YYYY-MM-DD' or None.

    Returns counts {"dated", "removed", "missing", "failed"}. Never raises (except cancellation).
    """
    out = {"dated": 0, "removed": 0, "missing": 0, "failed": 0}
    if not items or not token:
        return out
    found: list[tuple[dict, dict]] = []
    try:
        uuid = await user_uuid(conn, http, client_id, token)
        if not uuid:
            log.warning("plex history fix skipped: could not read the plex.tv user id")
            return out
        todo, taken = list(items), set()
        for attempt in range(LOOKUP_TRIES):
            nodes = await recent_history(http, client_id, token, uuid)
            now = datetime.now(timezone.utc)
            rest = []
            for it in todo:
                n = find_entry(nodes, it["title"], it.get("year"), taken, now)
                if n is None:
                    rest.append(it)
                else:
                    taken.add(n["id"])
                    found.append((it, n))
            todo = rest
            if not todo:
                break
            if attempt < LOOKUP_TRIES - 1:
                await _sleep(LOOKUP_DELAY)
        out["missing"] = len(todo)
        for i, (it, node) in enumerate(found):
            if i:
                await _sleep(MUTATION_DELAY)
            try:
                iso = iso_date(it.get("date"))
                if iso:
                    await _call(http, client_id, token, M_DATE,
                                {"id": node["id"], "input": {"date": iso}}, "updateActivityDate")
                    out["dated"] += 1
                else:
                    await _call(http, client_id, token, M_REMOVE,
                                {"input": {"id": node["id"], "type": "WATCH_HISTORY"}}, "removeActivity")
                    out["removed"] += 1
            except (httpx.HTTPError, GraphQLError, ValueError) as e:
                out["failed"] += 1
                log.warning("plex history fix failed for one film: %s", _why(e))
    except (httpx.HTTPError, GraphQLError, ValueError) as e:
        log.warning("plex history fix skipped: %s", _why(e))
    log.info("plex history fix: %s", out)
    return out


def _why(e: BaseException) -> str:
    return str(e)[:200] if isinstance(e, GraphQLError) else safe_error(e)
