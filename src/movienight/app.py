"""FastAPI application: JSON API + static frontend."""
from __future__ import annotations

import asyncio
import html
import logging
import random
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import quote

import httpx
from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import auth, chat, db, letterboxd as lb, library, plex, queries
from .config import Config
from .sync import SyncManager, plex_token, tmdb_counts
from .util import now_iso, safe_error

log = logging.getLogger("movienight")
STATIC_DIR = Path(__file__).parent / "static"
MAX_UPLOAD = lb.MAX_ZIP_BYTES


class SyncBody(BaseModel):
    what: Literal["all", "plex", "letterboxd", "tmdb"] = "all"


class DismissBody(BaseModel):
    days: int = Field(default=30, ge=0, le=3650)


class VoteBody(BaseModel):
    vote: Literal[1, -1, 0]


class SeenBody(BaseModel):
    seen: bool


class ServerBody(BaseModel):
    enabled: bool


class UserBody(BaseModel):
    username: str = Field(min_length=1, max_length=60, pattern=r"^[A-Za-z0-9_-]+$")


def create_app(cfg: Config | None = None, transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    cfg = cfg or Config.from_env()
    for noisy in ("httpx", "httpcore"):          # request URLs may contain credentials
        logging.getLogger(noisy).setLevel(logging.WARNING)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        cfg.image_dir.mkdir(parents=True, exist_ok=True)
        db.migrate(cfg.db_path)
        http = httpx.AsyncClient(transport=transport, follow_redirects=False,
                                 timeout=httpx.Timeout(20.0, connect=5.0))
        engine = library.Engine(lambda: db.connect(cfg.db_path))
        sync = SyncManager(cfg, http, engine)
        app.state.http, app.state.engine, app.state.sync = http, engine, sync
        methods = cfg.login_methods
        app.state.sessions = app.state.oidc = app.state.plex_login = None
        if methods:
            with db.session(cfg.db_path) as c:
                key = db.get_meta(c, "session_key")
                if not key:
                    key = secrets.token_hex(32)
                    db.set_meta(c, "session_key", key)
                cid = db.client_id(c)
            app.state.sessions = auth.Sessions(bytes.fromhex(key), cfg.public_url.startswith("https://"))
            if "oidc" in methods:
                app.state.oidc = auth.OIDC(cfg.oidc_issuer, cfg.oidc_client_id, cfg.public_url,
                                           cfg.oidc_allowed_subs, http)
            if "plex" in methods:
                app.state.plex_login = auth.PlexLogin(cfg.public_url, http, cid, plex)
        try:
            with db.session(cfg.db_path) as conn:
                lb.import_dir(conn, cfg.letterboxd_dir)
        except Exception as e:  # noqa: BLE001
            log.warning("Letterboxd folder import failed: %s", safe_error(e))
        task = asyncio.create_task(sync.scheduler())
        try:
            yield
        finally:
            task.cancel()
            if sync._task:
                sync._task.cancel()
            await http.aclose()

    app = FastAPI(title="MovieNight", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    # ---- helpers
    def conn():
        return db.connect(cfg.db_path)

    def engine() -> library.Engine:
        return app.state.engine

    def sync_mgr() -> SyncManager:
        return app.state.sync

    # ---- error shape: always {"detail": "..."}
    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError):
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(x) for x in first.get("loc", []) if x != "body" and x != "query")
        return JSONResponse({"detail": f"Invalid {loc or 'request'}: {first.get('msg', 'bad value')}"}, status_code=422)

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception):
        log.error("unhandled error: %s", safe_error(exc))
        return JSONResponse({"detail": "Internal server error"}, status_code=500)

    # ---- login (see auth.py): Plex account and/or OIDC; nothing here runs when login is off
    def _page(title: str, body: str, status: int = 200, links: list[tuple[str, str]] | None = None) -> HTMLResponse:
        buttons = "".join(
            f"<p><a style='display:inline-block;min-width:240px;padding:14px 22px;border-radius:12px;"
            f"background:#f5b942;color:#14122b;font-weight:700;text-decoration:none' href='{html.escape(href)}'>"
            f"{html.escape(label)}</a></p>" for label, href in (links if links is not None else [("Sign in", "/login")]))
        return HTMLResponse(
            "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title)} · MovieNight</title><link rel=icon href='/static/favicon.svg'>"
            "<body style='font:18px system-ui;background:#14122b;color:#f3efe6;display:grid;place-items:center;"
            "min-height:90vh;text-align:center;padding:24px;margin:0'><main>"
            f"<h1>{html.escape(title)}</h1><p>{html.escape(body)}</p>{buttons}</main>",
            status_code=status, headers={"Cache-Control": "no-store"})

    def _oidc_ready(st) -> bool:
        return st.oidc is not None and st.oidc.configured

    def _plex_owner_id() -> str:
        """The Plex account id that signed in as the owner (sessions of any other id are void)."""
        with db.session(cfg.db_path) as c:
            return db.get_meta(c, "plex_owner_id") or ""

    def _session_ok(st, session: auth.Session | None) -> bool:
        if session is None:
            return False
        kind, _, sub = session.sub.partition(":")
        if kind == "oidc":
            return _oidc_ready(st) and st.oidc.allows(sub)
        if kind == "plex":
            return st.plex_login is not None and auth.is_owner({"id": sub}, _plex_owner_id())
        return False

    @app.middleware("http")
    async def _require_session(request: Request, call_next):
        st = request.app.state
        sessions = getattr(st, "sessions", None)
        path = request.url.path
        if sessions is None or path in auth.PUBLIC_PATHS:
            return await call_next(request)
        if st.plex_login is None and not _oidc_ready(st):
            return JSONResponse({"detail": "Login is not configured yet (MOVIENIGHT_OIDC_CLIENT_ID)"}, status_code=503)
        session = sessions.check(request.cookies.get(auth.SESSION_COOKIE))
        if not _session_ok(st, session):
            if path.startswith("/api/"):
                return JSONResponse({"detail": "Not signed in"}, status_code=401)
            target = path + ("?" + request.url.query if request.url.query else "")
            return RedirectResponse("/login?next=" + quote(target, safe=""), status_code=303)
        request.state.session = session
        return await call_next(request)

    def _signed_in(st, session: auth.Session, next_path: str) -> RedirectResponse:
        res = RedirectResponse(next_path, status_code=303)
        res.set_cookie(auth.SESSION_COOKIE, st.sessions.sign(session.sub, session.name),
                       max_age=auth.SESSION_DAYS * 86400, **st.sessions.cookie_kwargs())
        return res

    @app.get("/login", include_in_schema=False)
    def login(request: Request, next: str = "/"):
        st = request.app.state
        if st.sessions is None:
            return RedirectResponse("/", status_code=303)
        q = "?next=" + quote(auth.safe_next(next), safe="")
        links = []
        if st.plex_login is not None:
            links.append(("Sign in with Plex", "/login/plex" + q))
        if _oidc_ready(st):
            links.append((f"Sign in with {cfg.oidc_name}", "/login/oidc" + q))
        if not links:
            return _page("Login not configured", "Set MOVIENIGHT_OIDC_CLIENT_ID and restart.", 503, [])
        return _page("MovieNight", "Sign in to pick tonight's film.", links=links)

    @app.get("/login/oidc", include_in_schema=False)
    async def login_oidc(request: Request, next: str = "/"):
        st = request.app.state
        if not _oidc_ready(st):
            return RedirectResponse("/login", status_code=303)
        try:
            return RedirectResponse(await st.oidc.login_url(next), status_code=303)
        except Exception as e:  # noqa: BLE001
            log.error("login start failed: %s", safe_error(e))
            return _page("Sign-in unavailable", "The login provider could not be reached. Try again in a moment.", 502)

    @app.get("/login/callback", include_in_schema=False)
    async def login_callback(request: Request, state: str = "", code: str = ""):
        st = request.app.state
        if not _oidc_ready(st):
            return RedirectResponse("/", status_code=303)
        try:
            session, next_path = await st.oidc.finish(state, code)
        except auth.LoginError as e:
            return _page("Could not sign you in", str(e), 403)
        except Exception as e:  # noqa: BLE001
            log.error("login failed: %s", safe_error(e))
            return _page("Could not sign you in", "Something went wrong talking to the login provider.", 502)
        return _signed_in(st, session, next_path)

    @app.get("/login/plex", include_in_schema=False)
    async def login_plex(request: Request, next: str = "/"):
        st = request.app.state
        if st.plex_login is None:
            return RedirectResponse("/login", status_code=303)
        try:
            return RedirectResponse(await st.plex_login.login_url(next), status_code=303)
        except Exception as e:  # noqa: BLE001
            log.error("plex login start failed: %s", safe_error(e))
            return _page("Sign-in unavailable", "plex.tv could not be reached. Try again in a moment.", 502)

    @app.get("/login/plex/callback", include_in_schema=False)
    async def login_plex_callback(request: Request, state: str = ""):
        st = request.app.state
        if st.plex_login is None:
            return RedirectResponse("/", status_code=303)
        try:
            account, token, next_path = await st.plex_login.finish(state)
            with db.session(cfg.db_path) as c:
                stored = plex_token(c, cfg)
                owner_id = db.get_meta(c, "plex_owner_id") or ""
            if not owner_id and stored and not cfg.plex_owner:
                # a library is already connected: the app belongs to that Plex account
                known = await plex.account(st.http, st.plex_login.client_id, stored)
                owner_id = (known or {}).get("id") or ""
            if cfg.plex_owner:
                allowed = auth.is_owner(account, cfg.plex_owner)
            elif owner_id:
                allowed = account["id"] == owner_id
            else:
                allowed = not stored          # nobody yet: the first sign-in becomes the owner
            if not allowed:
                raise auth.LoginError("This MovieNight belongs to another Plex account")
            with db.session(cfg.db_path) as c:
                db.set_meta(c, "plex_owner_id", account["id"])
                if not stored:
                    library.set_plex_token(c, token, account["name"] or None)
            if not stored:
                sync_mgr().start("plex")
        except auth.LoginError as e:
            return _page("Could not sign you in", str(e), 403)
        except Exception as e:  # noqa: BLE001
            log.error("plex login failed: %s", safe_error(e))
            return _page("Could not sign you in", "Something went wrong talking to plex.tv.", 502)
        return _signed_in(st, auth.Session("plex:" + account["id"], account["name"]), next_path)

    @app.get("/logout", include_in_schema=False)
    async def logout(request: Request):
        st = request.app.state
        target = "/logged-out"
        sessions = getattr(st, "sessions", None)
        session = sessions.check(request.cookies.get(auth.SESSION_COOKIE)) if sessions else None
        if session and session.sub.startswith("oidc:") and _oidc_ready(st):
            target = await st.oidc.logout_url("/logged-out")
        res = RedirectResponse(target, status_code=303)
        res.delete_cookie(auth.SESSION_COOKIE, path="/")
        return res

    @app.get("/logged-out", include_in_schema=False)
    def logged_out():
        return _page("Signed out", "See you at the next movie night.")

    @app.get("/api/me")
    def me(request: Request):
        """{"auth": false} when login is off; otherwise the signed-in user's display name."""
        session = getattr(request.state, "session", None)
        return {"auth": request.app.state.sessions is not None, "name": (session.name or None) if session else None}

    # ---- health / status
    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/api/status")
    def status():
        c = conn()
        try:
            counts = lb.lb_counts(c)
            token = plex_token(c, cfg)
            servers = [
                {"id": r["id"], "name": r["name"], "owned": bool(r["owned"]), "movies": r["movies"],
                 "enabled": bool(r["enabled"]), "reachable": bool(r["reachable"])}
                for r in c.execute("SELECT * FROM plex_servers ORDER BY owned DESC, name")
            ]
            movies = c.execute("SELECT COUNT(*) FROM movies").fetchone()[0]
            res = {
                "letterboxd": {"username": lb.username(c, cfg.letterboxd_user), **counts,
                               "last_sync": db.get_meta(c, "lb_last_sync")},
                "plex": {"connected": bool(token), "account": db.get_meta(c, "plex_account") or None,
                         "movies": movies, "last_sync": db.get_meta(c, "plex_last_sync"), "servers": servers},
                "tmdb": tmdb_counts(c, sync_mgr().tmdb.configured),
                "sync": dict(sync_mgr().state),
                "ready": bool(movies) and (counts["watched"] + counts["watchlist"]) > 0,
            }
            return res
        finally:
            c.close()

    # ---- Plex
    @app.post("/api/plex/pin")
    async def plex_pin():
        with db.session(cfg.db_path) as c:
            cid = db.client_id(c)
        try:
            return await plex.create_pin(app.state.http, cid)
        except (httpx.HTTPError, KeyError, ValueError) as e:
            raise HTTPException(502, f"Could not reach plex.tv ({safe_error(e)})") from None

    @app.get("/api/plex/pin/{pin_id}")
    async def plex_pin_check(pin_id: int):
        with db.session(cfg.db_path) as c:
            cid = db.client_id(c)
        try:
            token = await plex.check_pin(app.state.http, cid, pin_id)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                raise HTTPException(404, "PIN expired, start again") from None
            raise HTTPException(502, f"Could not reach plex.tv ({safe_error(e)})") from None
        except (httpx.HTTPError, KeyError, ValueError) as e:
            raise HTTPException(502, f"Could not reach plex.tv ({safe_error(e)})") from None
        if not token:
            return {"authorized": False}
        account = await plex.account_name(app.state.http, cid, token)
        with db.session(cfg.db_path) as c:
            library.set_plex_token(c, token, account)
        sync_mgr().start("plex")
        return {"authorized": True}

    @app.post("/api/plex/logout")
    def plex_logout():
        with db.session(cfg.db_path) as c:
            library.set_plex_token(c, None, None)
            for t in ("plex_items", "movies", "plex_servers", "dismissals"):
                c.execute(f"DELETE FROM {t}")           # fixed table names
            db.set_meta(c, "plex_last_sync", None)
        engine().invalidate()
        return {"ok": True}

    @app.put("/api/plex/servers/{server_id}")
    def plex_server(server_id: str, body: ServerBody):
        with db.session(cfg.db_path) as c:
            cur = c.execute("UPDATE plex_servers SET enabled=? WHERE id=?", (int(body.enabled), server_id))
            if cur.rowcount == 0:
                raise HTTPException(404, "Unknown server")
        engine().invalidate()
        return {"ok": True}

    # ---- sync / import
    @app.post("/api/sync", status_code=202)
    async def sync_now(body: SyncBody | None = None):
        what = body.what if body else "all"
        if not sync_mgr().start(what):
            raise HTTPException(409, "A sync is already running")
        return {"started": True}

    @app.post("/api/letterboxd/import")
    async def lb_import(file: UploadFile = File(...)):
        data = await file.read(MAX_UPLOAD + 1)
        if len(data) > MAX_UPLOAD:
            raise HTTPException(413, "File too large")
        try:
            with db.session(cfg.db_path) as c:
                counts = lb.import_zip(c, data)
                library.record_outcomes(c)
        except lb.ExportError as e:
            raise HTTPException(400, str(e)) from None
        engine().invalidate()
        sync_mgr().start("tmdb")
        return counts

    @app.put("/api/letterboxd/user")
    async def lb_user(body: UserBody):
        with db.session(cfg.db_path) as c:
            db.set_meta(c, "lb_user", body.username)
        sync_mgr().start("letterboxd")
        return {"ok": True}

    # ---- recommendations
    def filters(mood, genre, max_runtime, min_year, max_year, watchlist_only, include_watched,
                include_dismissed, server, q) -> queries.Filters:
        if mood and mood not in queries.MOOD_BY_ID:
            raise HTTPException(400, "Unknown mood")
        return queries.Filters(mood=mood or None, genres=genre or [], max_runtime=max_runtime, min_year=min_year,
                               max_year=max_year, watchlist_only=watchlist_only, include_watched=include_watched,
                               include_dismissed=include_dismissed, server=server or None, q=(q or "").strip() or None)

    def dismissed_now() -> dict:
        c = conn()
        try:
            return library.active_dismissals(c)
        finally:
            c.close()

    @app.get("/api/moods")
    def moods():
        return queries.moods_public()

    @app.get("/api/recommendations")
    def recommendations(
        limit: int = Query(24, ge=1, le=200), offset: int = Query(0, ge=0),
        sort: Literal["match", "rating", "recent", "year", "runtime"] = "match",
        mood: str | None = None, genre: list[str] = Query(default=[]),
        max_runtime: int | None = Query(None, ge=1), min_year: int | None = None, max_year: int | None = None,
        watchlist_only: bool = False, include_watched: bool = False, include_dismissed: bool = False,
        server: str | None = None, q: str | None = None,
    ):
        f = filters(mood, genre, max_runtime, min_year, max_year, watchlist_only, include_watched,
                    include_dismissed, server, q)
        st, dismissed = engine().get(), dismissed_now()
        matched = queries.sort_cands(queries.apply_filters(st.cands, f, dismissed), sort)
        return {
            "items": [library.movie_json(c, st, dismissed) for c in matched[offset:offset + limit]],
            "total": len(matched),
            "facets": queries.facets(st.cands, f, dismissed),
        }

    @app.get("/api/pick")
    def pick(
        exclude: list[int] = Query(default=[]),
        mood: str | None = None, genre: list[str] = Query(default=[]),
        max_runtime: int | None = Query(None, ge=1), min_year: int | None = None, max_year: int | None = None,
        watchlist_only: bool = False, include_watched: bool = False, include_dismissed: bool = False,
        server: str | None = None, q: str | None = None,
    ):
        f = filters(mood, genre, max_runtime, min_year, max_year, watchlist_only, include_watched,
                    include_dismissed, server, q)
        st, dismissed = engine().get(), dismissed_now()
        result = queries.pick(queries.apply_filters(st.cands, f, dismissed), set(exclude), random.Random())
        if result is None:
            raise HTTPException(404, "Nothing matches these filters")
        chosen, alts = result
        return {"pick": library.movie_json(chosen, st, dismissed),
                "alternatives": [library.movie_json(a, st, dismissed) for a in alts]}

    @app.get("/api/movies/{movie_id}")
    def movie(movie_id: int):
        st, dismissed = engine().get(), dismissed_now()
        c = st.by_id.get(movie_id)
        if c is None:
            raise HTTPException(404, "Movie not found")
        return library.movie_detail_json(c, st, dismissed)

    @app.post("/api/movies/{movie_id}/dismiss")
    def dismiss(movie_id: int, body: DismissBody | None = None):
        days = body.days if body else 30
        until = None if days == 0 else (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        with db.session(cfg.db_path) as c:
            if not c.execute("SELECT 1 FROM movies WHERE id=?", (movie_id,)).fetchone():
                raise HTTPException(404, "Movie not found")
            c.execute("INSERT OR REPLACE INTO dismissals(movie_id, until, created_at) VALUES(?,?,?)",
                      (movie_id, until, now_iso()))
        return {"ok": True}

    @app.delete("/api/movies/{movie_id}/dismiss")
    def undismiss(movie_id: int):
        with db.session(cfg.db_path) as c:
            c.execute("DELETE FROM dismissals WHERE movie_id=?", (movie_id,))
        return {"ok": True}

    @app.post("/api/movies/{movie_id}/feedback")
    def feedback(movie_id: int, body: VoteBody):
        with db.session(cfg.db_path) as c:
            row = c.execute("SELECT tmdb_id, dkey FROM movies WHERE id=?", (movie_id,)).fetchone()
            if row is None:
                raise HTTPException(404, "Movie not found")
            key = library.feedback_key(row["tmdb_id"], row["dkey"])
            if body.vote == 0:
                c.execute("DELETE FROM votes WHERE key=?", (key,))
            else:
                c.execute("INSERT INTO votes(key, vote, updated_at) VALUES(?,?,?) ON CONFLICT(key)"
                          " DO UPDATE SET vote=excluded.vote, updated_at=excluded.updated_at",
                          (key, body.vote, now_iso()))
        engine().invalidate()
        return {"ok": True}

    @app.post("/api/movies/{movie_id}/seen")
    def mark_seen(movie_id: int, body: SeenBody):
        with db.session(cfg.db_path) as c:
            row = c.execute("SELECT tmdb_id, dkey FROM movies WHERE id=?", (movie_id,)).fetchone()
            if row is None:
                raise HTTPException(404, "Movie not found")
            key = library.feedback_key(row["tmdb_id"], row["dkey"])
            if body.seen:
                c.execute("INSERT OR IGNORE INTO seen(key, created_at) VALUES(?,?)", (key, now_iso()))
            else:
                c.execute("DELETE FROM seen WHERE key=?", (key,))
        engine().invalidate()
        return {"ok": True}

    @app.get("/api/seen-candidates")
    def seen_candidates(limit: int = Query(60, ge=1, le=200), offset: int = Query(0, ge=0)):
        st, dismissed = engine().get(), dismissed_now()
        items, total = library.seen_candidates(st, dismissed, limit, offset)
        return {"items": [library.movie_json(c, st, dismissed, with_reasons=False) for c in items], "total": total}

    @app.get("/api/feedback")
    def feedback_overview():
        c = conn()
        try:
            library.record_outcomes(c)
            return library.feedback_json(c)
        finally:
            c.close()

    @app.get("/api/profile")
    def profile():
        st = engine().get()
        c = conn()
        try:
            model = library.model_summary(c)
        finally:
            c.close()
        return library.profile_json(st, model)

    # ---- images
    @app.get("/api/image/{movie_id}/{kind}")
    async def image(movie_id: int, kind: Literal["poster", "art"]):
        cache = cfg.image_dir / f"{movie_id}_{kind}.img"
        headers = {"Cache-Control": "public, max-age=604800"}
        if cache.is_file():
            data = cache.read_bytes()
            return Response(data, media_type=_sniff(data), headers=headers)
        field = "thumb" if kind == "poster" else "art"
        with db.session(cfg.db_path) as c:
            row = c.execute(
                "SELECT i.json, s.base_url, s.access_token, db_cid.value cid FROM plex_items i"
                " JOIN plex_servers s ON s.id=i.server_id LEFT JOIN meta db_cid ON db_cid.key='plex_client_id'"
                " WHERE i.movie_id=? AND s.enabled=1 AND s.base_url IS NOT NULL ORDER BY s.owned DESC", (movie_id,)
            ).fetchall()
        import json as _json
        for r in row:
            path = _json.loads(r["json"]).get(field)
            if not path:
                continue
            w, h = (500, 750) if kind == "poster" else (1280, 720)
            got = await plex.fetch_image(app.state.http, r["base_url"], r["access_token"] or "", r["cid"] or "",
                                         path, w, h)
            if got:
                data, _ = got
                try:
                    cfg.image_dir.mkdir(parents=True, exist_ok=True)
                    tmp = cache.with_suffix(".tmp")
                    tmp.write_bytes(data)
                    tmp.replace(cache)
                except OSError:
                    pass
                return Response(data, media_type=_sniff(data), headers=headers)
        raise HTTPException(404, "No image available")

    # ---- optional AI chat (off without Anthropic credentials)
    app.include_router(chat.router)

    # ---- frontend
    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        f = STATIC_DIR / "index.html"
        if f.is_file():
            return FileResponse(f, headers={"Cache-Control": "no-cache"})
        return HTMLResponse("<!doctype html><title>MovieNight</title><h1>MovieNight</h1>"
                            "<p>Frontend not installed yet. The API is available under /api.</p>")

    return app


def _sniff(data: bytes) -> str:
    if data[:8].startswith(b"\x89PNG"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


app = create_app()
