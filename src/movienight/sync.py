"""Background sync orchestration: the media server (Plex or Jellyfin), Letterboxd, TMDB enrichment,
profile warm-up."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

import httpx

from . import jellyfin, letterboxd as lb, library, plex, seenpush, servers
from .config import Config
from .db import client_id, connect, get_meta, set_meta
from .tmdb import Tmdb, TmdbError
from .util import norm_title, now_iso, safe_error

log = logging.getLogger("movienight.sync")
CHUNK = 32


def plex_token(conn, cfg: Config) -> str:
    return cfg.plex_token or get_meta(conn, "plex_token") or ""


class SyncManager:
    def __init__(self, cfg: Config, http: httpx.AsyncClient, engine: library.Engine):
        self.cfg = cfg
        self.http = http
        self.engine = engine
        self.tmdb = Tmdb(http, cfg.tmdb_token)
        self.state = {"running": False, "stage": "idle", "done": 0, "total": 0, "error": None}
        self._task: asyncio.Task | None = None
        self._errors: list[str] = []
        self.media = servers.backend(cfg)
        self.push_lock = asyncio.Lock()        # seen-mark pushes to the media server: one writer at a time
        self.last_push: dict | None = None
        self.jellyfin_problem: str | None = None

    # ---- status / control
    @property
    def running(self) -> bool:
        return self.state["running"]

    def start(self, what: str = "all") -> bool:
        if self.running:
            return False
        self.state.update(running=True, stage="idle", done=0, total=0, error=None)
        self._task = asyncio.create_task(self._run(what))
        return True

    async def wait(self) -> None:
        if self._task:
            await self._task

    def _progress(self, done: int, total: int) -> None:
        self.state["done"], self.state["total"] = done, total

    def _stage(self, name: str) -> None:
        self.state.update(stage=name, done=0, total=0)

    def _fail(self, stage: str, exc: BaseException) -> None:
        msg = f"{stage}: {safe_error(exc)}"
        log.warning("sync stage failed: %s", msg)
        self._errors.append(msg)

    async def _run(self, what: str) -> None:
        self._errors = []
        media = self.cfg.media_server          # only the selected server type's stage ever runs
        first = {"plex": [media], "jellyfin": [media], "letterboxd": ["letterboxd"], "tmdb": [],
                 "all": [media, "letterboxd"]}.get(what, [media, "letterboxd"])
        try:
            for name in first + ["tmdb", "profile"]:
                self._stage(name)
                try:
                    await getattr(self, f"stage_{name}")()
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001 - never crash the app
                    self._fail(name, e)
        finally:
            self.state.update(running=False, stage="idle", done=0, total=0,
                              error="; ".join(self._errors) or None)

    async def scheduler(self) -> None:
        """Initial sync, then one every `sync_hours`."""
        if self.cfg.sync_on_start:
            self.start("all")
        interval = max(self.cfg.sync_hours, 0.05) * 3600
        while True:
            await asyncio.sleep(interval)
            self.start("all")

    # ---- stages
    async def stage_plex(self) -> None:
        if self.cfg.media_server != "plex":
            return
        conn = connect(self.cfg.db_path)
        try:
            token = plex_token(conn, self.cfg)
            if not token:
                return
            cid = client_id(conn)
            try:
                servers = await plex.get_servers(self.http, cid, token)
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 401:
                    raise RuntimeError("Plex token was rejected; log in again") from None
                raise
            if not get_meta(conn, "plex_account"):
                set_meta(conn, "plex_account", await plex.account_name(self.http, cid, token) or "")
            bases = await asyncio.gather(*(plex.pick_connection(self.http, s, cid) for s in servers))
            for s in servers:
                conn.execute(
                    "INSERT INTO media_servers(id,name,owned) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET"
                    " name=excluded.name, owned=excluded.owned", (s["id"], s["name"], int(s["owned"])))
            conn.commit()
            seen = {s["id"] for s in servers}
            for r in conn.execute("SELECT id FROM media_servers").fetchall():
                if r["id"] not in seen:        # server no longer shared with us
                    conn.execute("DELETE FROM media_items WHERE server_id=?", (r["id"],))
                    conn.execute("DELETE FROM media_servers WHERE id=?", (r["id"],))
            for s, base in zip(servers, bases):
                conn.execute("UPDATE media_servers SET reachable=?, base_url=?, access_token=? WHERE id=?",
                             (int(bool(base)), base, s["access_token"], s["id"]))
            conn.commit()
            for s, base in zip(servers, bases):
                enabled = conn.execute("SELECT enabled FROM media_servers WHERE id=?", (s["id"],)).fetchone()["enabled"]
                if not base or not enabled:
                    continue
                try:
                    items = await plex.fetch_movies(self.http, base, cid, s["access_token"], self._progress)
                except Exception as e:  # noqa: BLE001
                    self._fail(f"plex ({s['name']})", e)
                    continue
                library.store_server_items(conn, s["id"], items)
                conn.execute("UPDATE media_servers SET last_sync=? WHERE id=?", (now_iso(), s["id"]))
                conn.commit()
            library.assign_movies(conn)
            set_meta(conn, "plex_last_sync", now_iso())
            conn.commit()
            try:
                await self._slugs(conn, cid, token)
            except Exception as e:  # noqa: BLE001 - play links just fall back to the web URL
                self._fail("plex slugs", e)
            if self.cfg.push_seen:
                try:
                    await self.push_seen(conn, cid)
                except Exception as e:  # noqa: BLE001 - the library itself is already stored
                    self._fail("plex seen marks", e)
        finally:
            conn.close()
        self.engine.invalidate()

    async def _slugs(self, conn, cid: str, token: str) -> None:
        """Resolve watch.plex.tv slugs for guids not yet looked up (misses are retried after a week)."""
        week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = conn.execute(
            "SELECT DISTINCT json_extract(i.json, '$.guid') g FROM media_items i"
            " LEFT JOIN plex_slugs p ON p.guid = json_extract(i.json, '$.guid')"
            " WHERE g IS NOT NULL AND (p.guid IS NULL OR (p.slug = '' AND p.checked_at < ?))", (week_ago,)).fetchall()
        guids = [r["g"] for r in rows]
        sem = asyncio.Semaphore(8)

        async def one(g):
            async with sem:
                return g, await plex.resolve_slug(self.http, cid, g, token)

        for start in range(0, len(guids), 100):
            for g, slug in await asyncio.gather(*(one(g) for g in guids[start:start + 100])):
                if slug is not None:
                    conn.execute("INSERT OR REPLACE INTO plex_slugs(guid,slug,checked_at) VALUES(?,?,?)",
                                 (g, slug, now_iso()))
            conn.commit()

    async def stage_jellyfin(self) -> None:
        """Sign in (or reuse the stored token), read the movie library, push seen marks if enabled."""
        if self.cfg.media_server != "jellyfin":
            return
        cfg = self.cfg
        self.jellyfin_problem = None
        if not cfg.jellyfin_configured:
            self.jellyfin_problem = ("Jellyfin is selected but MOVIENIGHT_JELLYFIN_URL, MOVIENIGHT_JELLYFIN_USER"
                                     " or MOVIENIGHT_JELLYFIN_PASSWORD is not set")
            raise RuntimeError(self.jellyfin_problem)
        base = cfg.jellyfin_url.rstrip("/")
        conn = connect(cfg.db_path)
        try:
            cid = client_id(conn)
            row = conn.execute("SELECT * FROM media_servers WHERE id LIKE 'jf:%' ORDER BY id LIMIT 1").fetchone()
            same_login = (row is not None and row["base_url"] == base
                          and (get_meta(conn, "jellyfin_login") or "") == cfg.jellyfin_user.lower())
            token = (row["access_token"] or "") if same_login else ""
            user_id = get_meta(conn, "jellyfin_user_id") or ""
            server_id = row["id"] if same_login else ""
            try:
                info = await jellyfin.server_info(self.http, base)
            except httpx.HTTPError as e:
                if row is not None:
                    conn.execute("UPDATE media_servers SET reachable=0 WHERE id=?", (row["id"],))
                    conn.commit()
                self.jellyfin_problem = f"Jellyfin server not reachable ({safe_error(e)})"
                raise

            async def sign_in() -> None:
                nonlocal token, user_id, server_id
                try:
                    a = await jellyfin.authenticate(self.http, base, cid, cfg.jellyfin_user, cfg.jellyfin_password)
                except jellyfin.AuthError as e:
                    self.jellyfin_problem = str(e)
                    raise
                token, user_id = a["token"], a["user_id"]
                server_id = jellyfin.server_key(a["server_id"] or info["id"])
                set_meta(conn, "jellyfin_user_id", user_id)
                set_meta(conn, "jellyfin_account", a["name"])
                set_meta(conn, "jellyfin_login", cfg.jellyfin_user.lower())
                self._store_jellyfin_server(conn, server_id, info["name"], base, token)

            if not token or not user_id:
                await sign_in()
            try:
                items = await jellyfin.fetch_movies(self.http, base, cid, token, user_id, self._progress)
            except httpx.HTTPStatusError as e:
                if e.response.status_code != 401:
                    raise
                await sign_in()                    # token revoked or expired: sign in again, once
                items = await jellyfin.fetch_movies(self.http, base, cid, token, user_id, self._progress)
            self._store_jellyfin_server(conn, server_id, info["name"], base, token)
            library.store_server_items(conn, server_id, items)
            conn.execute("UPDATE media_servers SET last_sync=? WHERE id=?", (now_iso(), server_id))
            conn.commit()
            library.assign_movies(conn)
            set_meta(conn, "jellyfin_last_sync", now_iso())
            conn.commit()
            self.jellyfin_problem = None
            if cfg.push_seen:
                try:
                    await self.push_seen(conn, cid)
                except Exception as e:  # noqa: BLE001 - the library itself is already stored
                    self._fail("jellyfin seen marks", e)
        except Exception as e:
            self.jellyfin_problem = self.jellyfin_problem or f"Jellyfin sync failed ({safe_error(e)})"
            raise
        finally:
            conn.close()
        self.engine.invalidate()

    @staticmethod
    def _store_jellyfin_server(conn, server_id: str, name: str, base: str, token: str) -> None:
        """One row for the Jellyfin server; rows of any other server (another URL or ServerId) go."""
        for r in conn.execute("SELECT id FROM media_servers WHERE id != ?", (server_id,)).fetchall():
            conn.execute("DELETE FROM media_items WHERE server_id=?", (r["id"],))
            conn.execute("DELETE FROM media_servers WHERE id=?", (r["id"],))
        conn.execute(
            "INSERT INTO media_servers(id,name,owned,reachable,base_url,access_token) VALUES(?,?,0,1,?,?)"
            " ON CONFLICT(id) DO UPDATE SET name=excluded.name, reachable=1, base_url=excluded.base_url,"
            " access_token=excluded.access_token", (server_id, name, base, token))
        conn.commit()

    async def push_seen(self, conn, cid: str) -> dict:
        """Retry pending unscrobbles, then mark played every seen film not yet pushed (also the backfill)."""
        async with self.push_lock:
            undone = await seenpush.unpush(conn, self.http, cid, backend=self.media)
            counts = await seenpush.push(conn, self.http, cid, backend=self.media)
        self.last_push = {**counts, "unpushed": undone["unpushed"], "unpush_failed": undone["failed"]}
        log.info("seen marks on %s: %s", self.media.label, self.last_push)
        return self.last_push

    async def stage_letterboxd(self) -> None:
        conn = connect(self.cfg.db_path)
        try:
            lb.import_dir(conn, self.cfg.letterboxd_dir)
            conn.commit()
            user = lb.username(conn, self.cfg.letterboxd_user)
            if user:
                try:
                    entries = lb.parse_rss(await lb.fetch_rss(self.http, user))
                    lb.apply_rss(conn, entries)
                except Exception as e:  # noqa: BLE001
                    self._fail("letterboxd rss", e)
            conn.commit()
            library.record_outcomes(conn)
        finally:
            conn.close()
        self.engine.invalidate()

    async def stage_tmdb(self) -> None:
        if not self.tmdb.configured:
            return
        conn = connect(self.cfg.db_path)
        try:
            await self._resolve(conn)
            await self._details(conn)
            await self._recs(conn)
        finally:
            conn.close()
        self.engine.invalidate()

    async def stage_profile(self) -> None:
        await asyncio.to_thread(self._refresh_model)
        self.engine.invalidate()
        await asyncio.to_thread(self.engine.get)

    def _refresh_model(self) -> None:
        """Backtest + weight tuning, then freeze outcomes (runs in a worker thread)."""
        conn = connect(self.cfg.db_path)
        try:
            library.record_outcomes(conn)
            library.refresh_model(conn, self.media)
        finally:
            conn.close()

    # ---- TMDB work, resumable: only what is missing
    async def _batches(self, items: list, fn, on_done) -> None:
        total, done, bad_chunks = len(items), 0, 0
        # The TMDB stage runs several sub-phases (resolve, details, recs) under one progress bar: done
        # carries over from the previous sub-phase, so total must grow by this one's work, not be replaced.
        base = self.state["done"]
        self.state["total"] = base + total
        for i in range(0, total, CHUNK):
            chunk = items[i:i + CHUNK]
            results = await asyncio.gather(*(fn(x) for x in chunk), return_exceptions=True)
            fails = 0
            for x, res in zip(chunk, results):
                if isinstance(res, TmdbError):
                    raise res
                if isinstance(res, asyncio.CancelledError):
                    raise res
                if isinstance(res, BaseException):
                    fails += 1
                    last = res
                else:
                    on_done(x, res)
            bad_chunks = bad_chunks + 1 if fails == len(chunk) else 0
            done += len(chunk)
            self.state["done"] = base + done
            if bad_chunks >= 3:
                raise last  # type: ignore[possibly-undefined]

    async def _resolve(self, conn) -> None:
        rows = conn.execute(
            "SELECT id, name, year, norm_title FROM lb_films WHERE tmdb_id IS NULL AND resolve_state=0"
        ).fetchall()
        if not rows:
            return
        plex_ids = {}
        for r in conn.execute("SELECT title, year, tmdb_id FROM media_items WHERE tmdb_id IS NOT NULL"):
            plex_ids.setdefault((norm_title(r["title"]), r["year"]), r["tmdb_id"])
        todo = []
        for r in rows:
            hit = plex_ids.get((r["norm_title"], r["year"]))
            if hit:
                conn.execute("UPDATE lb_films SET tmdb_id=?, resolve_state=1 WHERE id=?", (hit, r["id"]))
            else:
                todo.append(r)
        conn.commit()

        def done(row, tid):
            conn.execute("UPDATE lb_films SET tmdb_id=?, resolve_state=? WHERE id=?",
                         (tid, 1 if tid else 2, row["id"]))
            conn.commit()

        await self._batches(todo, lambda r: self.tmdb.search(r["name"], r["year"]), done)

    async def _details(self, conn) -> None:
        ids = [r[0] for r in conn.execute(
            "SELECT tmdb_id FROM lb_films WHERE tmdb_id IS NOT NULL AND (watched=1 OR liked=1)"
            " UNION SELECT tmdb_id FROM movies WHERE tmdb_id IS NOT NULL"
        ) if not conn.execute("SELECT 1 FROM tmdb_details WHERE tmdb_id=?", (r[0],)).fetchone()]
        # backfill: library films whose stored details predate release_date
        have = set(ids)
        ids += [r[0] for r in conn.execute(
            "SELECT m.tmdb_id FROM movies m JOIN tmdb_details d ON d.tmdb_id=m.tmdb_id"
            " WHERE d.json != '{}' AND json_extract(d.json,'$.release_date') IS NULL"
        ) if r[0] not in have]
        if not ids:
            return

        def done(tid, det):
            conn.execute("INSERT OR REPLACE INTO tmdb_details(tmdb_id,json,fetched_at) VALUES(?,?,?)",
                         (tid, json.dumps(det or {}), now_iso()))
            conn.commit()

        await self._batches(ids, self.tmdb.details, done)
        self.engine.invalidate()

    async def _recs(self, conn) -> None:
        ids = [r[0] for r in conn.execute(
            "SELECT tmdb_id FROM lb_films WHERE tmdb_id IS NOT NULL AND (rating>=4 OR liked=1)"
        ) if not conn.execute("SELECT 1 FROM tmdb_recs WHERE tmdb_id=?", (r[0],)).fetchone()]
        if not ids:
            return

        def done(tid, recs):
            conn.execute("INSERT OR REPLACE INTO tmdb_recs(tmdb_id,json,fetched_at) VALUES(?,?,?)",
                         (tid, json.dumps(recs), now_iso()))
            conn.commit()

        await self._batches(ids, self.tmdb.recommendations, done)


def tmdb_counts(conn, configured: bool) -> dict:
    enriched = conn.execute("SELECT COUNT(*) FROM tmdb_details WHERE json != '{}'").fetchone()[0]
    if not configured:
        return {"configured": False, "enriched": enriched, "pending": 0}
    unresolved = conn.execute("SELECT COUNT(*) FROM lb_films WHERE tmdb_id IS NULL AND resolve_state=0").fetchone()[0]
    missing = conn.execute(
        "SELECT COUNT(*) FROM (SELECT tmdb_id FROM lb_films WHERE tmdb_id IS NOT NULL AND (watched=1 OR liked=1)"
        " UNION SELECT tmdb_id FROM movies WHERE tmdb_id IS NOT NULL) WHERE tmdb_id NOT IN"
        " (SELECT tmdb_id FROM tmdb_details)"
    ).fetchone()[0]
    return {"configured": True, "enriched": enriched, "pending": unresolved + missing}
