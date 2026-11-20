"""Push local "seen it" marks to the media server as played (MOVIENIGHT_PUSH_SEEN).

Every item the app marks played is recorded in `seen_pushes`; only those are ever unscrobbled, so
anything the user watched on the server on their own is left alone. A `seen_pushes` row whose `seen`
mark is gone is a pending unscrobble (retried by the sync). Server errors never propagate: the local
mark is what matters, and the sync retries. Tokens never reach the logs. The calls go through the
selected backend (servers.py): Plex scrobble/unscrobble or Jellyfin played/unplayed.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3

import httpx

from . import plex_history, servers
from .util import norm_title, now_iso, safe_error

log = logging.getLogger("movienight.seenpush")
CONCURRENCY = 4

# seen key -> movie: the key is `tmdb:<id>` when the TMDB id is known, else movies.dkey
_ITEMS_OF_SEEN = (
    "SELECT se.key, i.server_id, i.rating_key, i.json, s.name sname, s.reachable, s.base_url, s.access_token"
    " FROM seen se JOIN movies m ON (m.tmdb_id IS NOT NULL AND se.key='tmdb:'||m.tmdb_id)"
    "  OR (m.tmdb_id IS NULL AND se.key=m.dkey)"
    " JOIN media_items i ON i.movie_id=m.id JOIN media_servers s ON s.id=i.server_id"
    " WHERE s.enabled=1"
)


def _set_view_count(conn: sqlite3.Connection, server_id: str, rating_key: str, n: int) -> None:
    row = conn.execute("SELECT json FROM media_items WHERE server_id=? AND rating_key=?",
                       (server_id, rating_key)).fetchone()
    if row is None:
        return
    it = json.loads(row["json"])
    it["view_count"] = n
    conn.execute("UPDATE media_items SET json=? WHERE server_id=? AND rating_key=?",
                 (json.dumps(it), server_id, rating_key))


async def _run(calls: list, fn) -> list:
    sem = asyncio.Semaphore(CONCURRENCY)

    async def one(c):
        async with sem:
            return await fn(c)

    return await asyncio.gather(*(one(c) for c in calls), return_exceptions=True)


async def push(conn: sqlite3.Connection, http: httpx.AsyncClient, client_id: str,
               keys: list[str] | None = None, backend=None, history_token: str = "") -> dict:
    """Mark played every not-yet-pushed item of the `seen` films (all, or only `keys`).

    Only items on enabled servers with a stored view count of 0 are pushed; items already watched in
    Plex are skipped and not recorded. Returns counts: pushed, already_watched, failed, unreachable,
    not_on_server (seen films with no item on an enabled server). With a `history_token` (Plex only) the
    plex.tv history entries the new scrobbles created are then re-dated or removed (plex_history.py).
    """
    sql, params = _ITEMS_OF_SEEN, []
    if keys is not None:
        sql += f" AND se.key IN ({','.join('?' * len(keys))})"
        params = list(keys)
    pushed_items = {(r[0], r[1]) for r in conn.execute("SELECT server_id, rating_key FROM seen_pushes")}
    rows = conn.execute(sql, params).fetchall()
    on_server = {r["key"] for r in rows}
    wanted = set(keys) if keys is not None else {r[0] for r in conn.execute("SELECT key FROM seen")}
    counts = {"pushed": 0, "already_watched": 0, "failed": 0, "unreachable": 0,
              "not_on_server": len(wanted - on_server)}
    todo, fixes = [], {}
    for r in rows:
        if (r["server_id"], r["rating_key"]) in pushed_items:
            continue
        if int(json.loads(r["json"]).get("view_count") or 0) > 0:
            counts["already_watched"] += 1
        elif not r["reachable"] or not r["base_url"]:
            counts["unreachable"] += 1
        else:
            todo.append(r)
    if not todo:
        return counts
    be = backend or servers.PLEX
    user_id = be.user_id(conn)

    async def call(r):
        await be.mark_played(http, r["base_url"], client_id, r["access_token"] or "", user_id, r["rating_key"])

    for r, res in zip(todo, await _run(todo, call)):
        if isinstance(res, asyncio.CancelledError):
            raise res
        if isinstance(res, BaseException):
            counts["failed"] += 1
            log.warning("could not mark %s played on %s server %s: %s", r["key"], be.label, r["sname"], safe_error(res))
            continue
        conn.execute("INSERT OR IGNORE INTO seen_pushes(key,server_id,rating_key,pushed_at) VALUES(?,?,?,?)",
                     (r["key"], r["server_id"], r["rating_key"], now_iso()))
        _set_view_count(conn, r["server_id"], r["rating_key"], 1)
        counts["pushed"] += 1
        if history_token and be.name == "plex" and r["key"] not in fixes:
            fixes[r["key"]] = _history_item(conn, r)
    conn.commit()
    if fixes:
        # after the whole batch, never interleaved; best effort, never changes the push result
        await plex_history.fix_new_activities(conn, http, client_id, history_token, list(fixes.values()))
    return counts


def _history_item(conn: sqlite3.Connection, r) -> dict:
    """Title, year and real watch date (Letterboxd diary) of a film just marked played."""
    it = json.loads(r["json"])
    title, year = it.get("title") or "", it.get("year")
    row = None
    if r["key"].startswith("tmdb:"):
        row = conn.execute("SELECT watched_date FROM lb_films WHERE tmdb_id=? AND watched=1"
                           " AND watched_date IS NOT NULL ORDER BY watched_date DESC LIMIT 1",
                           (r["key"][5:],)).fetchone()
    if row is None:
        row = conn.execute("SELECT watched_date FROM lb_films WHERE norm_title=? AND year IS ? AND watched=1"
                           " AND watched_date IS NOT NULL ORDER BY watched_date DESC LIMIT 1",
                           (norm_title(title), year)).fetchone()
    return {"title": title, "year": year, "date": row["watched_date"] if row else None}


async def unpush(conn: sqlite3.Connection, http: httpx.AsyncClient, client_id: str,
                 keys: list[str] | None = None, backend=None) -> dict:
    """Unscrobble the items the app pushed for films no longer marked seen (all, or only `keys`).

    Records for servers that no longer exist are dropped; unreachable servers and Plex errors leave
    the record in place for the next sync. Returns counts: unpushed, failed, dropped.
    """
    sql = ("SELECT p.key, p.server_id, p.rating_key, s.id sid, s.name sname, s.reachable, s.base_url,"
           " s.access_token FROM seen_pushes p LEFT JOIN media_servers s ON s.id=p.server_id"
           " WHERE p.key NOT IN (SELECT key FROM seen)")
    params: list = []
    if keys is not None:
        sql += f" AND p.key IN ({','.join('?' * len(keys))})"
        params = list(keys)
    counts = {"unpushed": 0, "failed": 0, "dropped": 0}
    todo = []
    for r in conn.execute(sql, params).fetchall():
        if r["sid"] is None:
            conn.execute("DELETE FROM seen_pushes WHERE key=? AND server_id=? AND rating_key=?",
                         (r["key"], r["server_id"], r["rating_key"]))
            counts["dropped"] += 1
        elif not r["reachable"] or not r["base_url"]:
            counts["failed"] += 1
        else:
            todo.append(r)

    be = backend or servers.PLEX
    user_id = be.user_id(conn)

    async def call(r):
        await be.mark_unplayed(http, r["base_url"], client_id, r["access_token"] or "", user_id, r["rating_key"])

    for r, res in zip(todo, await _run(todo, call)):
        if isinstance(res, asyncio.CancelledError):
            raise res
        if isinstance(res, BaseException):
            counts["failed"] += 1
            log.warning("could not mark %s unplayed on %s server %s: %s", r["key"], be.label, r["sname"], safe_error(res))
            continue
        conn.execute("DELETE FROM seen_pushes WHERE key=? AND server_id=? AND rating_key=?",
                     (r["key"], r["server_id"], r["rating_key"]))
        _set_view_count(conn, r["server_id"], r["rating_key"], 0)
        counts["unpushed"] += 1
    conn.commit()
    return counts


def pushed_count(conn: sqlite3.Connection) -> int:
    """Seen films the app has marked played on at least one media server."""
    return conn.execute("SELECT COUNT(DISTINCT key) FROM seen_pushes WHERE key IN (SELECT key FROM seen)").fetchone()[0]
