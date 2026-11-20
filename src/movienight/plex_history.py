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
import json
import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone

import httpx

from . import plex
from .db import get_meta, set_meta
from .util import norm_title, now_iso, parse_iso, safe_error

log = logging.getLogger("movienight.plexhistory")

COMMUNITY_API = "https://community.plex.tv/api"
RECENT = timedelta(minutes=10)      # only entries this fresh can be ours; older real plays are never touched
SCAN = 20                           # newest history nodes inspected
LOOKUP_TRIES = 3                    # the entry can show up a moment after the scrobble
LOOKUP_DELAY = 2.0
RECHECK_AFTER = timedelta(hours=2)    # second pass: plex.tv can sync the server scrobble to the history later
CATCHUP_WINDOW = timedelta(minutes=15)   # catch-up: entries this close to a push time are ours
PAGE = 50                           # history page size
MAX_PAGES = 100
PAGE_DELAY = 0.5
MUTATION_DELAY = 6.0                # default pacing between plex.tv writes (configurable: MOVIENIGHT_PLEX_HISTORY_PACE)
RATE_WAITS = (60.0, 120.0, 240.0, 480.0, 600.0)   # backoff when rate limited and the server gives no retryAfter
MAX_BACKOFF = 600.0
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
    def __init__(self, errors: list, retry_after: float | None = None, limited: bool = False):
        self.errors, self.retry_after, self.limited = errors, retry_after, limited
        super().__init__("; ".join(str((e or {}).get("message") or "error")[:80] for e in errors))

    @property
    def schema_error(self) -> bool:
        return any("Cannot query field" in str((e or {}).get("message")) or
                   ((e or {}).get("extensions") or {}).get("code") == "GRAPHQL_VALIDATION_FAILED"
                   for e in self.errors)


class RateLimitedOut(GraphQLError):
    """Still rate limited after the longest backoff: the caller stops its batch and resumes later."""


_AFTER_RE = re.compile(r"(?:retry|try again)[^0-9]{0,20}(\d+(?:\.\d+)?)\s*(?:s\b|sec|seconds)?", re.I)
_shape_logged = False


def _rate_limit(errors: list) -> tuple[bool, float | None]:
    """(is it a rate limit?, seconds to wait if the server said). The real error shape is not documented: the
    code RATE_LIMITED, a retryAfter in the extensions, or the message ("Rate limit exceeded!") are all accepted."""
    global _shape_logged
    limited, retry = False, None
    for e in errors:
        e = e or {}
        ext = e.get("extensions") or {}
        msg = str(e.get("message") or "")
        if ext.get("code") == "RATE_LIMITED" or "rate limit" in msg.lower():
            limited = True
            for k in ("retryAfter", "retry_after", "retryAfterSeconds"):
                try:
                    if ext.get(k) is not None:
                        retry = float(ext[k])
                        break
                except (TypeError, ValueError):
                    pass
            if retry is None and (m := _AFTER_RE.search(msg)):
                retry = float(m.group(1))
            if not _shape_logged:
                _shape_logged = True
                log.info("plex.tv rate limit shape: code=%r extensions=%s message=%r",
                         ext.get("code"), json.dumps(ext, default=str)[:300], msg[:100])
    return limited, retry


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
        limited, retry = _rate_limit(errors)
        raise GraphQLError(errors, retry, limited)
    return body.get("data") or {}


async def _call(http, client_id, token, query, variables, op) -> dict:
    """One GraphQL call. When rate limited it waits (the server's retryAfter, else RATE_WAITS: 60 s doubling up
    to 10 min) and retries the SAME call; RateLimitedOut once the waits are used up."""
    tries = 0
    while True:
        try:
            return await _gql(http, client_id, token, query, variables, op)
        except GraphQLError as e:
            if not e.limited:
                raise
            if tries >= len(RATE_WAITS):
                raise RateLimitedOut(e.errors, e.retry_after, True) from None
            wait = min(e.retry_after if e.retry_after is not None else RATE_WAITS[tries], MAX_BACKOFF)
            log.info("plex.tv rate limited: waiting %.0f s (attempt %d of %d)", wait, tries + 1, len(RATE_WAITS))
            await _sleep(wait)
            tries += 1


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


def find_entry(nodes: list[dict], item: dict, taken: set, now: datetime) -> dict | None:
    """The newest fresh, not yet claimed entry for this film.

    Matched by the Discover id (`metadataItem.id`) when the item has one; otherwise by title (accent and
    case-insensitive), and year when both sides have it.
    """
    did = item.get("discover_id")
    want = norm_title(item.get("title"))
    year = item.get("year")
    for n in nodes[:SCAN]:
        mi = n.get("metadataItem") or {}
        when = parse_iso(n.get("date"))
        if not n.get("id") or n["id"] in taken or when is None or abs(now - when) > RECENT:
            continue
        if did:
            if str(mi.get("id") or "") != did:
                continue
            return n
        if str(mi.get("type") or "movie").lower() != "movie" or norm_title(mi.get("title")) != want:
            continue
        y = mi.get("year")
        if year and y and int(y) != int(year):
            continue
        return n
    return None


def _first_pass_done(conn: sqlite3.Connection, item: dict) -> None:
    """The first plex.tv fix is done: the row now waits for the re-check (`fixed_at`), which catches history
    entries plex.tv creates later from the server scrobble."""
    when = item.get("scrobbled_at")
    when = when.strftime("%Y-%m-%dT%H:%M:%SZ") if isinstance(when, datetime) else None
    for pk in item.get("pks") or []:
        conn.execute("UPDATE seen_pushes SET fixed_at=?, scrobbled_at=COALESCE(?, scrobbled_at)"
                     " WHERE key=? AND server_id=? AND rating_key=?", (now_iso(), when, *pk))
    conn.commit()


def _note_scrobble(conn: sqlite3.Connection, item: dict) -> None:
    """Remember our Discover scrobble time right away (its history entry is dated then)."""
    when = item["scrobbled_at"].strftime("%Y-%m-%dT%H:%M:%SZ")
    for pk in item.get("pks") or []:
        conn.execute("UPDATE seen_pushes SET scrobbled_at=? WHERE key=? AND server_id=? AND rating_key=?", (when, *pk))
    conn.commit()


def _settle(conn: sqlite3.Connection, item: dict) -> None:
    """The re-check is done: nothing more to do for this item (`seen_pushes.discover_at`)."""
    for pk in item.get("pks") or []:
        conn.execute("UPDATE seen_pushes SET discover_at=? WHERE key=? AND server_id=? AND rating_key=?",
                     (now_iso(), *pk))
    conn.commit()


async def _scrobble_discover(conn, http, client_id, token, items: list[dict], out: dict,
                             pace: float | None = None) -> list[dict]:
    """Mark the items watched on plex.tv unless plex.tv already counts them watched; returns those that now
    have a fresh history entry to fix. Items without a Discover id pass through (nothing to scrobble)."""
    fresh, first = [], True
    for it in items:
        did = it.get("discover_id")
        if not did:
            fresh.append(it)
            continue
        if not first:
            await _sleep(MUTATION_DELAY if pace is None else pace)
        first = False
        try:
            seen = await plex.discover_view_count(http, client_id, token, did)
            if seen is None:
                raise httpx.HTTPError("could not read the plex.tv watched state")
            if seen > 0:
                out["already"] += 1
                _first_pass_done(conn, it)           # plex.tv already has it watched: its history stays as is
                continue
            await plex.discover_scrobble(http, client_id, token, did)
            out["scrobbled"] += 1
            it["scrobbled_at"] = datetime.now(timezone.utc)
            if it.get("pks"):
                _note_scrobble(conn, it)
            fresh.append(it)
        except httpx.HTTPError as e:
            out["failed"] += 1
            log.warning("could not mark a film watched on plex.tv: %s", safe_error(e))
    return fresh


async def fix_new_activities(conn: sqlite3.Connection, http: httpx.AsyncClient, client_id: str, token: str,
                             items: list[dict], pace: float | None = None) -> dict:
    """items: [{"title", "year", "date", "discover_id"?, "pks"?}] just marked played on the server.

    Items with a `discover_id` are first marked watched on plex.tv (Discover scrobble; the server scrobble
    alone does not do that), then every item's new history entry is re-dated to `date` ('YYYY-MM-DD') or
    removed when there is none. Returns counts {"scrobbled", "already", "dated", "removed", "missing",
    "failed"}. Never raises (except cancellation).
    """
    out = {"scrobbled": 0, "already": 0, "dated": 0, "removed": 0, "missing": 0, "failed": 0,
           "rate_limited": False}
    delay = MUTATION_DELAY if pace is None else pace
    if not items or not token:
        return out
    found: list[tuple[dict, dict]] = []
    try:
        todo = await _scrobble_discover(conn, http, client_id, token, items, out, pace)
        if not todo:
            return out
        uuid = await user_uuid(conn, http, client_id, token)
        if not uuid:
            log.warning("plex history fix skipped: could not read the plex.tv user id")
            return out
        taken: set = set()
        for attempt in range(LOOKUP_TRIES):
            nodes = await recent_history(http, client_id, token, uuid)
            now = datetime.now(timezone.utc)
            rest = []
            for it in todo:
                n = find_entry(nodes, it, taken, now)
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
        for it in todo:
            log.info("plex history: no new entry found for %r (left as is)", it.get("title"))
            _first_pass_done(conn, it)
        for i, (it, node) in enumerate(found):
            if i:
                await _sleep(delay)
            try:
                iso = iso_date(it.get("date"))
                if iso:
                    await _call(http, client_id, token, M_DATE,
                                {"id": node["id"], "input": {"date": iso}}, "updateActivityDate")
                    out["dated"] += 1
                    log.info("plex history: re-dated %r to %s", it.get("title"), iso[:10])
                    _first_pass_done(conn, it)
                else:
                    await _call(http, client_id, token, M_REMOVE,
                                {"input": {"id": node["id"], "type": "WATCH_HISTORY"}}, "removeActivity")
                    out["removed"] += 1
                    log.info("plex history: removed the entry for %r (no real watch date known)", it.get("title"))
                    _first_pass_done(conn, it)
            except RateLimitedOut:
                out["rate_limited"] = True
                break                                   # the rest is picked up by the catch-up later
            except (httpx.HTTPError, GraphQLError, ValueError) as e:
                out["failed"] += 1
                log.warning("plex history fix failed for one film: %s", _why(e))
    except RateLimitedOut:
        out["rate_limited"] = True
    except (httpx.HTTPError, GraphQLError, ValueError) as e:
        log.warning("plex history fix skipped: %s", _why(e))
    if out["rate_limited"]:
        log.info("plex history fix: still rate limited after the longest backoff; the rest waits for the catch-up")
    log.info("plex history fix: %s", out)
    return out


async def history_since(http, client_id, token, uuid: str, since: datetime) -> list[dict]:
    """The watch history, newest first, paginated back to `since` (bounded by MAX_PAGES)."""
    nodes: list[dict] = []
    after, query = None, Q_FULL
    for _ in range(MAX_PAGES):
        variables = {"uuid": uuid, "first": PAGE, "after": after}
        try:
            data = await _call(http, client_id, token, query, variables, "GetWatchHistoryHub")
        except GraphQLError as e:
            if not e.schema_error or query is Q_MIN:
                raise
            query = Q_MIN
            data = await _call(http, client_id, token, query, variables, "GetWatchHistoryHub")
        wh = ((data.get("user") or {}).get("watchHistory")) or {}
        page = wh.get("nodes") or []
        nodes.extend(page)
        info = wh.get("pageInfo") or {}
        oldest = parse_iso(page[-1].get("date")) if page else None
        if not page or not info.get("hasNextPage") or not info.get("endCursor") or (oldest and oldest < since):
            break
        after = info["endCursor"]
        await _sleep(PAGE_DELAY)
    return nodes


def _index(nodes: list[dict], index: dict) -> None:
    """discoverId -> its MOVIE entries (episodes and anything else are never indexed)."""
    for n in nodes:
        mi = n.get("metadataItem") or {}
        if n.get("id") and mi.get("id") and str(mi.get("type") or "").upper() == "MOVIE":
            index.setdefault(str(mi["id"]), {})[n["id"]] = n


def _ours(item: dict, entries: dict) -> list[dict]:
    """Entries within CATCHUP_WINDOW of one of the item's push times (or of its fresh scrobble): the only ones
    Movienight can have created. Anything else may be a real play and is never touched."""
    times = [t for t in (parse_iso(p) for p in item.get("pushed_ats") or []) if t]
    if item.get("scrobbled_at"):
        sc = item["scrobbled_at"]
        times.append(sc if isinstance(sc, datetime) else parse_iso(sc))
    out = []
    for n in entries.values():
        when = parse_iso(n.get("date"))
        if when is not None and any(abs(when - t) <= CATCHUP_WINDOW for t in times):
            out.append(n)
    return sorted(out, key=lambda n: n.get("date") or "")


async def catch_up(conn: sqlite3.Connection, http: httpx.AsyncClient, client_id: str, token: str,
                   items: list[dict], pace: float | None = None) -> dict:
    """Settle old pushes (made before plex.tv was handled). Per film:
    1. plex.tv viewCount 0 -> Discover scrobble; 2. find the history entries Movienight created (within
    CATCHUP_WINDOW of the push time, matched by Discover id); 3. manual -> remove them, Letterboxd with a
    diary date -> one re-dated, the rest removed. The row is settled only if everything succeeded.

    items: [{"title", "date", "discover_id", "pks", "pushed_ats"}]. The history is fetched once (paginated).
    """
    out = {"films": 0, "scrobbled": 0, "removed": 0, "dated": 0, "fixed": 0, "settled": 0, "failed": 0,
           "rate_limited": False}
    delay = MUTATION_DELAY if pace is None else pace
    if not items or not token:
        return out
    out["films"] = len(items)
    try:
        uuid = await user_uuid(conn, http, client_id, token)
        if not uuid:
            log.warning("plex history catch-up skipped: could not read the plex.tv user id")
            return out
        ok: list[dict] = []
        for i, it in enumerate(items):                      # 1. plex.tv watched state
            if i:
                await _sleep(delay)
            did = it["discover_id"]
            try:
                seen = 1 if it.get("recheck") else await plex.discover_view_count(http, client_id, token, did)
                if seen is None:
                    raise httpx.HTTPError("could not read the plex.tv watched state")
                if seen == 0:
                    await plex.discover_scrobble(http, client_id, token, did)
                    it["scrobbled_at"] = datetime.now(timezone.utc)
                    _note_scrobble(conn, it)            # kept, so a batch cut short still finds its entry later
                    out["scrobbled"] += 1
                ok.append(it)
            except httpx.HTTPError as e:
                out["failed"] += 1
                log.warning("plex history catch-up: %r left for the next sync: %s", it.get("title"), safe_error(e))
        if not ok:
            return out
        # 2. the history, once; entries of a fresh scrobble can lag, so those are looked up again
        olds = [parse_iso(p) for it in ok for p in it.get("pushed_ats") or []]
        since = min([t for t in olds if t] or [datetime.now(timezone.utc)]) - timedelta(days=1)
        index: dict = {}
        _index(await history_since(http, client_id, token, uuid, since), index)
        for _attempt in range(LOOKUP_TRIES - 1):
            if all(_ours(it, index.get(it["discover_id"], {})) for it in ok if it.get("scrobbled_at") and not it.get("recheck")):
                break
            await _sleep(LOOKUP_DELAY)
            recent = datetime.now(timezone.utc) - timedelta(hours=1)
            _index(await history_since(http, client_id, token, uuid, recent), index)
        first = True
        for it in ok:                                       # 3. fix the entries
            if out["rate_limited"]:
                break
            entries = _ours(it, index.get(it["discover_id"], {}))
            iso = iso_date(it.get("date"))
            removed = dated = 0
            good = True
            for n in entries:
                if not first:
                    await _sleep(delay)
                first = False
                try:
                    if iso and not dated:
                        await _call(http, client_id, token, M_DATE, {"id": n["id"], "input": {"date": iso}},
                                    "updateActivityDate")
                        dated += 1
                    else:
                        await _call(http, client_id, token, M_REMOVE,
                                    {"input": {"id": n["id"], "type": "WATCH_HISTORY"}}, "removeActivity")
                        removed += 1
                except RateLimitedOut:
                    out["rate_limited"] = True          # stop the batch, no failure noise: resumed later
                    break
                except (httpx.HTTPError, GraphQLError, ValueError) as e:
                    good = False
                    log.warning("plex history catch-up: a fix failed for %r: %s", it.get("title"), _why(e))
            out["removed"] += removed
            out["dated"] += dated
            if out["rate_limited"]:
                break
            log.info("plex history %s: %r scrobbled=%s, entries removed=%d, re-dated=%d%s",
                     "re-check" if it.get("recheck") else "catch-up", it.get("title"),
                     bool(it.get("scrobbled_at")) and not it.get("recheck"), removed, dated,
                     "" if good else " (will retry)")
            if good:
                if it.get("recheck"):
                    _settle(conn, it)
                    out["settled"] += 1
                else:
                    _first_pass_done(conn, it)
                    out["fixed"] += 1
            else:
                out["failed"] += 1
    except RateLimitedOut:
        out["rate_limited"] = True
    except (httpx.HTTPError, GraphQLError, ValueError) as e:
        log.warning("plex history catch-up stopped: %s", _why(e))
    if out["rate_limited"]:
        log.info("plex history catch-up: still rate limited after the longest backoff; stopping, the rest waits")
    log.info("plex history catch-up: %s", out)
    return out


def _why(e: BaseException) -> str:
    return str(e)[:200] if isinstance(e, GraphQLError) else safe_error(e)
