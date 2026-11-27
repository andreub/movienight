"""Background sync orchestration: the media server (Plex or Jellyfin), Letterboxd, TMDB enrichment,
profile warm-up."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

import httpx

from . import completion, jellyfin, letterboxd as lb, library, plex, plex_history, seenpush, servers
from .config import Config
from .db import client_id, connect, get_meta, set_meta
from .tmdb import Tmdb, TmdbError
from .util import norm_title, now_iso, safe_error

log = logging.getLogger("movienight.sync")
CHUNK = 32
FOLLOWUP_DELAY = 30 * 60      # seconds until the next plex.tv catch-up batch while rows remain


def plex_token(conn, cfg: Config) -> str:
    return cfg.plex_token or get_meta(conn, "plex_token") or ""


def history_token(conn, cfg: Config) -> str:
    """The account token for the plex.tv history fix-up; empty = feature off (or not Plex)."""
    return plex_token(conn, cfg) if cfg.plex_history_fix and cfg.media_server == "plex" else ""


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
        self.history_lock = asyncio.Lock()     # plex.tv history fix-ups: one at a time, outside push_lock
        self._history_tasks: set[asyncio.Task] = set()
        self._followup: asyncio.Task | None = None
        self.followup_delay = FOLLOWUP_DELAY
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
            stored = conn.execute("SELECT id FROM media_servers").fetchall()
            if not servers and stored:
                # an empty list on a 200 is more likely a plex.tv hiccup than every server unshared at once
                log.warning("plex.tv returned no servers; keeping the %d stored ones", len(stored))
                stored = []
            for r in stored:
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
                library.assign_movies(conn)     # per server: no window where its films have no movie_id
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
                    await self.push_seen(conn, cid, lb=True)
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
        pending = [r["g"] for r in rows]
        sem = asyncio.Semaphore(4)          # Discover throttles bursts (answers 401/429): stay gentle, retry misses

        async def one(g):
            async with sem:
                return g, await plex.resolve_slug(self.http, cid, g, token)

        for attempt in range(3):
            failed: list[str] = []
            for start in range(0, len(pending), 100):
                for g, slug in await asyncio.gather(*(one(g) for g in pending[start:start + 100])):
                    if slug is None:
                        failed.append(g)
                    else:
                        conn.execute("INSERT OR REPLACE INTO plex_slugs(guid,slug,checked_at) VALUES(?,?,?)",
                                     (g, slug, now_iso()))
                conn.commit()
            pending = failed
            if not pending:
                break
            await asyncio.sleep(5)

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

    def schedule_history(self, conn, cid: str, items: list[dict], catchup: bool = False) -> None:
        """Run the plex.tv history fix-up in the background (best effort), so the caller can release its lock."""
        token = history_token(conn, self.cfg)
        if not items or not token:
            return
        task = asyncio.create_task(self._history(cid, token, items, catchup))
        self._history_tasks.add(task)
        task.add_done_callback(self._history_tasks.discard)

    async def _history(self, cid: str, token: str, items: list[dict], catchup: bool = False) -> None:
        try:
            async with self.history_lock:
                conn = connect(self.cfg.db_path)
                try:
                    fn = plex_history.catch_up if catchup else plex_history.fix_new_activities
                    result = await fn(conn, self.http, cid, token, items, pace=self.cfg.plex_history_pace)
                finally:
                    conn.close()
            if catchup:
                self._plan_followup(result)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - best effort
            log.warning("plex history fix failed: %s", safe_error(e))

    def _plan_followup(self, result: dict | None) -> None:
        """While unsettled pushes remain, run the next catch-up batch FOLLOWUP_DELAY later (single flight; the
        history lock still serialises it with everything else). Stops when a batch made no progress and was not
        rate limited, so a row that keeps failing does not loop; the next regular sync resumes it."""
        if self._followup is not None and not self._followup.done():
            return
        conn = connect(self.cfg.db_path)
        try:
            if not seenpush.unsettled(conn):
                return
            progressed = result is None or result.get("rate_limited") or (result.get("fixed", 0) + result.get("settled", 0)) > 0
            if not progressed:
                return
        finally:
            conn.close()
        self._followup = asyncio.create_task(self._followup_run())

    async def _followup_run(self) -> None:
        try:
            await asyncio.sleep(self.followup_delay)
            conn = connect(self.cfg.db_path)
            try:
                items = seenpush.catchup_items(conn)
                self._followup = None
                if items:
                    self.schedule_history(conn, client_id(conn), items, catchup=True)   # plans the next one itself
                else:
                    self._plan_followup(None)           # only re-checks still waiting for their 2 hours
            finally:
                conn.close()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - best effort
            self._followup = None
            log.warning("plex history follow-up failed: %s", safe_error(e))

    async def drain_history(self, followups: bool = False) -> None:
        """Wait for the running history work (and, with `followups`, for the scheduled follow-up batches too)."""
        for _ in range(10):
            pending = [*self._history_tasks, *([self._followup] if followups and self._followup else [])]
            if not pending:
                return
            await asyncio.gather(*pending, return_exceptions=True)

    def cancel_history(self) -> None:
        """Shutdown: drop the background plex.tv work (unsettled rows are picked up again on the next start)."""
        for t in [*self._history_tasks, *([self._followup] if self._followup else [])]:
            t.cancel()

    async def push_seen(self, conn, cid: str, lb: bool = False) -> dict:
        """Retry pending unscrobbles, then mark played every seen film not yet pushed (also the backfill).
        With `lb`, Letterboxd-watched films too (new ones, or the paced backfill when opted in)."""
        token = history_token(conn, self.cfg)
        sink = (lambda items: self.schedule_history(conn, cid, items)) if token else None
        async with self.push_lock:
            undone = await seenpush.unpush(conn, self.http, cid, backend=self.media, history_token=token)
            if token and self.media.name == "plex" and not self._history_tasks:
                # pushes from before the plex.tv settling existed: once each, paced in the background
                self.schedule_history(conn, cid, seenpush.catchup_items(conn), catchup=True)
            counts = await seenpush.push(conn, self.http, cid, backend=self.media, history_sink=sink,
                                         history_token=token, lb=lb, backfill=self.cfg.push_lb_backfill)
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
            if self.cfg.push_seen and self.cfg.media_server == "plex":
                try:
                    library.assign_movies(conn)
                    await self.push_seen(conn, client_id(conn), lb=True)
                except Exception as e:  # noqa: BLE001 - the import itself is stored
                    self._fail("letterboxd films on plex", e)
                conn.commit()
        finally:
            conn.close()
        self.engine.invalidate()

    async def stage_tmdb(self) -> None:
        if not self.tmdb.configured:
            return
        conn = connect(self.cfg.db_path)
        try:
            # any failure in one phase (TMDB error, 5xx HTTPStatusError, ...) must not skip the later ones, _recs
            # included: progress is saved per item, so the next sync resumes
            phases = [self._resolve, self._details]
            if self.cfg.completionist:          # MOVIENIGHT_COMPLETIONIST=0: no collection/search/keyword/person lookups
                phases += [self._collections, self._people]
            phases.append(self._recs)
            for stage in phases:
                try:
                    await stage(conn)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.warning("tmdb %s stage stopped early, will resume next sync: %s", stage.__name__.strip("_"),
                                safe_error(e))
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
        have = set(ids)
        # completionist: backfill details stored before `collection` / `keyword_ids` existed. json_type is NULL
        # only when the key is absent: a film with no saga stores JSON null and is never refetched.
        if self.cfg.completionist:
            wanted = {r[0] for r in conn.execute(
                "SELECT tmdb_id FROM lb_films WHERE tmdb_id IS NOT NULL AND (watched=1 OR liked=1)"
                " UNION SELECT tmdb_id FROM movies WHERE tmdb_id IS NOT NULL")}
            ids += [r[0] for r in conn.execute(
                "SELECT tmdb_id FROM tmdb_details WHERE json != '{}' AND (json_type(json,'$.collection') IS NULL"
                " OR json_type(json,'$.keyword_ids') IS NULL) ORDER BY tmdb_id") if r[0] in wanted and r[0] not in have]
        if not ids:
            return

        def done(tid, det):
            conn.execute("INSERT OR REPLACE INTO tmdb_details(tmdb_id,json,fetched_at) VALUES(?,?,?)",
                         (tid, json.dumps(det or {}), now_iso()))
            conn.commit()

        await self._batches(ids, self.tmdb.details, done)
        self.engine.invalidate()

    async def _collections(self, conn) -> None:
        """Completionist data, each item refreshed after its TTL (30 days): collection searches on the stems of
        the seen films' sagas (franchise siblings), those collections, the films of series keywords, and the orphan
        candidates of each started saga."""
        def cutoff(days):
            return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")

        # parts stored before they carried backdrop_path count as stale (once: a refetch stores the key, even null)
        fresh_lookups = {r[0] for r in conn.execute(
            "SELECT key FROM tmdb_lookups WHERE fetched_at IS NOT NULL AND fetched_at >= ?"
            " AND NOT (key LIKE 'keyword:%' AND " + completion.STALE_PARTS_SQL + ")",
            (cutoff(completion.LOOKUP_TTL_DAYS),))}

        def store(key, value):
            conn.execute("INSERT OR REPLACE INTO tmdb_lookups(key,json,fetched_at) VALUES(?,?,?)",
                         (key, json.dumps(value), now_iso()))
            conn.commit()

        seen = completion.seen_tmdb_ids(conn, self.media.count_watched)
        changed = False
        stems = [s for s in completion.wanted_searches(conn, seen) if completion.search_key(s) not in fresh_lookups]
        if stems:
            await self._batches(stems, self.tmdb.search_collections,
                                lambda s, hits: store(completion.search_key(s), hits or []))

        fresh = {r[0] for r in conn.execute(
            "SELECT collection_id FROM tmdb_collections WHERE fetched_at IS NOT NULL AND fetched_at >= ?"
            " AND NOT " + completion.STALE_PARTS_SQL, (cutoff(completion.COLLECTION_TTL_DAYS),))}
        ids = [c for c in completion.wanted_collections(conn, seen) if c not in fresh]

        def done(cid, coll):
            conn.execute("INSERT OR REPLACE INTO tmdb_collections(collection_id,json,fetched_at) VALUES(?,?,?)",
                         (cid, json.dumps(coll or {}), now_iso()))
            conn.commit()

        if ids:
            await self._batches(ids, self.tmdb.collection, done)
            changed = True

        kws = [(kid, name) for kid, name in completion.wanted_keywords(conn, seen)
               if f"keyword:{kid}" not in fresh_lookups]
        if kws:
            await self._batches(kws, lambda kw: self.tmdb.keyword_movies(kw[0]),
                                lambda kw, res: store(f"keyword:{kw[0]}", {"id": kw[0], "name": kw[1], **(res or {})}))
            changed = True

        # orphans: films in no collection that carry a started saga's name (after the collections, which name them)
        targets = completion.wanted_orphans(conn, seen)
        phrases = [p for p in sorted(targets, key=str.lower) if completion.orphan_key(p) not in fresh_lookups]
        if phrases:
            await self._batches(phrases, lambda p: self._orphans(conn, p, targets[p]),
                                lambda p, res: store(completion.orphan_key(p), res))
            changed = True
        if changed:
            self.engine.invalidate()

    async def _orphans(self, conn, phrase: str, targets: list[dict]) -> dict:
        """One saga name's orphan candidates: a keyword named exactly like it (if it is on few films) and the title
        search, filtered as far as the hits allow, then the details (own collection, runtime) of at most
        ORPHAN_MAX_DETAILS of them. At most 3 + ORPHAN_MAX_DETAILS calls."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        want = completion.norm_words(phrase)
        kw = next((k for k in await self.tmdb.search_keywords(phrase) if completion.norm_words(k["name"]) == want), None)
        kw_parts: list[dict] = []
        if kw:
            res = await self.tmdb.keyword_movies(kw["id"], today)
            if res["total"] <= completion.ORPHAN_KEYWORD_MAX_TOTAL:
                kw_parts = res["parts"]
        hits = await self.tmdb.search_movies(phrase, today)
        cands = completion.orphan_candidates(phrase, hits, kw_parts, targets, today)

        async def detail(p):
            row = conn.execute("SELECT json FROM tmdb_details WHERE tmdb_id=?", (p["id"],)).fetchone()
            d = json.loads(row[0] or "{}") if row else {}
            if "collection" not in d or not d.get("runtime"):
                d = await self.tmdb.details(p["id"]) or {}
            return {**p, "collection_id": (d.get("collection") or {}).get("id"), "runtime": d.get("runtime")}

        films = await asyncio.gather(*(detail(p) for p in cands))
        return {"phrase": phrase, "keyword_id": kw["id"] if kw else None, "films": list(films)}

    async def _people(self, conn) -> None:
        """Completionist filmographies: the film credits of the directors and actors the user has seen several films
        of (at most 40 of each, so at most 80 lookups), each refreshed after its TTL (30 days, 7 for
        people with a film from the last six months or upcoming)."""
        ids = await asyncio.to_thread(self._stale_people)
        if not ids:
            return

        def done(pid, person):
            # committed in one go afterwards (and on failure), not once per person
            person = person or {}
            conn.execute("INSERT OR REPLACE INTO tmdb_person_credits(person_id,name,profile_path,json,fetched_at)"
                         " VALUES(?,?,?,?,?)", (pid, person.get("name"), person.get("profile_path"),
                                                json.dumps(person), now_iso()))

        try:
            await self._batches(ids, self.tmdb.person_credits, done)
        finally:
            conn.commit()
        self.engine.invalidate()

    def _stale_people(self) -> list[int]:
        """Eligible people whose stored filmography is missing or past its TTL (runs in a worker thread: the
        eligibility scan parses the JSON of every seen film)."""
        conn = connect(self.cfg.db_path)
        try:
            def cutoff(days):
                return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
            recent = (datetime.now(timezone.utc) - timedelta(days=completion.PERSON_ACTIVE_DAYS)).strftime("%Y-%m-%d")
            long_c, short_c = cutoff(completion.PERSON_TTL_DAYS), cutoff(completion.PERSON_ACTIVE_TTL_DAYS)
            fresh = {r[0] for r in conn.execute(
                "SELECT person_id FROM tmdb_person_credits WHERE fetched_at IS NOT NULL AND fetched_at >= ?",
                (short_c,))}
            # between the two cutoffs a person stays fresh unless a film of theirs is recent or upcoming
            for pid, js in conn.execute(
                    "SELECT person_id, json FROM tmdb_person_credits WHERE fetched_at IS NOT NULL"
                    " AND fetched_at >= ? AND fetched_at < ?", (long_c, short_c)):
                p = json.loads(js or "{}")
                if not any((c.get("release_date") or "") >= recent for c in (p.get("directed") or []) + (p.get("acted") or [])):
                    fresh.add(pid)
            return [p for p in completion.wanted_person_ids(conn, completion.seen_tmdb_ids(conn, self.media.count_watched))
                    if p not in fresh]
        finally:
            conn.close()

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
