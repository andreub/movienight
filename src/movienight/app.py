"""FastAPI application: JSON API + static frontend."""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import random
import secrets
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import quote

import httpx
from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import auth, cast, chat, completion, db, letterboxd as lb, library, plex, queries, seenpush, seerr, servers, tags as lbtags, version
from .config import Config
from .sync import SyncManager, history_token, plex_token, tmdb_counts
from .util import now_iso, safe_error

log = logging.getLogger("movienight")
STATIC_DIR = Path(__file__).parent / "static"
_PKG_DIR = Path(__file__).parent
_APP_VERSION: str | None = None


def _app_version() -> str:
    global _APP_VERSION
    if _APP_VERSION is None:  # read the code once per process
        _APP_VERSION = version.compute_version(_PKG_DIR)
    return _APP_VERSION
MAX_UPLOAD = lb.MAX_ZIP_BYTES


class SyncBody(BaseModel):
    what: Literal["all", "plex", "jellyfin", "letterboxd", "tmdb"] = "all"   # plex/jellyfin = the selected server


class PlayBody(BaseModel):
    player_id: str = Field(min_length=1, max_length=200)
    server_id: str = Field(min_length=1, max_length=200)
    rating_key: str = Field(min_length=1, max_length=40)


class DismissBody(BaseModel):
    days: int = Field(default=30, ge=0, le=3650)


Vote = Literal[2, 1, -1, 0]      # 2 love (a favourite), 1 thumb up, -1 thumb down, 0 clears the vote


class VoteBody(BaseModel):
    vote: Vote


class SeenBody(BaseModel):
    seen: bool
    vote: Vote | None = None    # also set (or with 0, clear) the vote in the same transaction


class ServerBody(BaseModel):
    enabled: bool


class UserBody(BaseModel):
    username: str = Field(min_length=1, max_length=60, pattern=r"^[A-Za-z0-9_-]+$")


def configure_logging() -> None:
    """Let the app's own loggers (movienight.*) print INFO to stdout: uvicorn only sets up its own loggers,
    so without this `docker logs` shows no sync/push lines. MOVIENIGHT_LOG_LEVEL overrides (default INFO)."""
    level = getattr(logging, (os.environ.get("MOVIENIGHT_LOG_LEVEL") or "INFO").strip().upper(), logging.INFO)
    pkg = logging.getLogger("movienight")
    pkg.setLevel(level)
    if not logging.getLogger().handlers and not pkg.handlers:
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        pkg.addHandler(h)
        pkg.propagate = False


def create_app(cfg: Config | None = None, transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    configure_logging()
    cfg = cfg or Config.from_env()
    media = servers.backend(cfg)
    for noisy in ("httpx", "httpcore"):          # request URLs may contain credentials
        logging.getLogger(noisy).setLevel(logging.WARNING)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        cfg.image_dir.mkdir(parents=True, exist_ok=True)
        db.migrate(cfg.db_path)
        with db.session(cfg.db_path) as c:
            wiped = db.switch_media_server(c, cfg.media_server)
        if wiped:
            log.warning("media server changed from %s to %s: library, dismissals and %s settings deleted;"
                        " seen marks, votes and Letterboxd data kept", wiped, cfg.media_server, wiped)
            for f in cfg.image_dir.glob("*.img"):     # cached by movie id, and the ids start over
                f.unlink(missing_ok=True)
        http = httpx.AsyncClient(transport=transport, follow_redirects=False,
                                 timeout=httpx.Timeout(20.0, connect=5.0))
        engine = library.Engine(lambda: db.connect(cfg.db_path), media)
        sync = SyncManager(cfg, http, engine)
        app.state.http, app.state.engine, app.state.sync = http, engine, sync
        seerr_http = seerr.new_client(transport) if cfg.seerr_configured else None   # cookie-less, see seerr.py
        app.state.seerr = seerr.Accounts(seerr_http, cfg.seerr_url, cfg.db_path) if seerr_http else None
        methods = cfg.login_methods
        app.state.sessions = app.state.oidc = app.state.plex_login = None
        if cfg.login_required:       # with Jellyfin and no OIDC this fails closed (503), never open
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
            sync.cancel_history()
            await asyncio.to_thread(cast.close_all)
            if sync._task:
                sync._task.cancel()
            await http.aclose()
            if seerr_http:
                await seerr_http.aclose()

    app = FastAPI(title="MovieNight", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.cfg = cfg      # read by routers mounted from other modules (the chat's list_completions tool)

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
    def _page(title: str, body: str, status: int = 200, links: list[tuple[str, str]] | None = None,
              script: str = "") -> HTMLResponse:
        buttons = "".join(
            f"<p><a style='display:inline-block;min-width:240px;padding:14px 22px;border-radius:12px;"
            f"background:#f5b942;color:#14122b;font-weight:700;text-decoration:none' href='{html.escape(href)}'>"
            f"{html.escape(label)}</a></p>" for label, href in (links if links is not None else [("Sign in", "/login")]))
        return HTMLResponse(
            "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title)} · MovieNight</title><link rel=icon href='/static/favicon.svg'>"
            "<body style='font:18px system-ui;background:#14122b;color:#f3efe6;display:grid;place-items:center;"
            "min-height:90vh;text-align:center;padding:24px;margin:0'>"
            "<style>a:focus-visible,button:focus-visible{outline:4px solid #f3efe6;outline-offset:4px}</style><main>"
            f"<h1>{html.escape(title)}</h1><p>{html.escape(body)}</p>{buttons}</main>"
            + (f"<script>{script}</script>" if script else ""),
            status_code=status, headers={"Cache-Control": "no-store"})

    # In the Android TV app (its WebView's user agent says MovieNightTV) the code sign-in comes first, focused.
    _TV_FIRST = ("if(/MovieNightTV/.test(navigator.userAgent)){const a=document.querySelector('a[href^=\"/login/plex/tv\"]');"
                 "const first=document.querySelector('main p a');"
                 "if(a&&first){first.parentElement.before(a.parentElement);a.focus();}}")

    def _code_page(next_path: str) -> HTMLResponse:
        """Sign in with a 4-character code typed at plex.tv/link (no typing on the TV itself)."""
        nxt = json.dumps(next_path).replace("<", "\\u003c")
        script = (
            f"const NEXT={nxt};const codeEl=document.getElementById('code'),msg=document.getElementById('msg'),"
            "again=document.getElementById('again');let timer=null;"
            "function fail(t){msg.textContent=t;again.hidden=false;again.focus();}"
            "async function start(){clearTimeout(timer);again.hidden=true;codeEl.textContent='····';"
            "msg.textContent='Getting a code…';"
            "try{const r=await fetch('/login/plex/code?next='+encodeURIComponent(NEXT),{method:'POST',credentials:'same-origin'});"
            "const d=await r.json();if(!r.ok)return fail(d.detail||'Could not get a code.');"
            "codeEl.textContent=d.code;msg.textContent='Waiting for the code to be entered…';poll(d.id);}"
            "catch(e){fail('Could not reach MovieNight.');}}"
            "function poll(id){timer=setTimeout(async()=>{try{"
            "const r=await fetch('/login/plex/code/'+encodeURIComponent(id),{credentials:'same-origin'});const d=await r.json();"
            "if(d.status==='ok'){msg.textContent='Signed in.';location.replace(d.next||'/');return;}"
            "if(d.status==='pending'){poll(id);return;}"
            "codeEl.textContent='····';"
            "fail(d.status==='expired'?'This code expired.':(d.detail||'Could not sign you in.'));"
            "}catch(e){poll(id);}},2000);}"
            "again.onclick=start;start();")
        return HTMLResponse(
            "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            "<title>Sign in with a code · MovieNight</title><link rel=icon href='/static/favicon.svg'>"
            "<body style='font:20px system-ui;background:#14122b;color:#f3efe6;display:grid;place-items:center;"
            "min-height:90vh;text-align:center;padding:24px;margin:0'>"
            "<style>a{color:#f5b942}a:focus-visible,button:focus-visible{outline:4px solid #f3efe6;outline-offset:4px}"
            "#code{font:700 clamp(64px,16vw,168px)/1.1 ui-monospace,Menlo,monospace;letter-spacing:.18em;"
            "color:#f5b942;margin:16px 0}</style><main>"
            "<h1>Sign in with a code</h1>"
            "<p>Go to <a href='https://plex.tv/link' target=_blank rel=noopener>plex.tv/link</a>"
            " on your phone and enter this code</p>"
            "<p id=code aria-live=polite>····</p><p id=msg role=status></p>"
            "<p><button id=again type=button hidden style='min-width:240px;padding:14px 22px;border:0;border-radius:12px;"
            "background:#f5b942;color:#14122b;font:700 18px system-ui;cursor:pointer'>Get a new code</button></p>"
            f"<p><a href='/login?next={html.escape(quote(next_path, safe=''))}'>Other ways to sign in</a></p></main>"
            f"<script>{script}</script>",
            headers={"Cache-Control": "no-store"})

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
        if sessions is None or path in auth.PUBLIC_PATHS or path.startswith(auth.PUBLIC_PREFIXES):
            return await call_next(request)
        if st.plex_login is None and not _oidc_ready(st):
            if cfg.media_server != "plex" and not cfg.oidc_issuer:
                return JSONResponse({"detail": "Login is not configured (set MOVIENIGHT_OIDC_ISSUER and"
                                               " MOVIENIGHT_OIDC_CLIENT_ID, or MOVIENIGHT_LOGIN=none)"}, status_code=503)
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
            links.append(("Sign in with a code (TV)", "/login/plex/tv" + q))
        if _oidc_ready(st):
            links.append((f"Sign in with {cfg.oidc_name}", "/login/oidc" + q))
        if not links:
            if cfg.media_server != "plex" and not cfg.oidc_issuer:
                return _page("Login not configured", "Set MOVIENIGHT_OIDC_ISSUER and MOVIENIGHT_OIDC_CLIENT_ID"
                             " (or MOVIENIGHT_LOGIN=none) and restart.", 503, [])
            return _page("Login not configured", "Set MOVIENIGHT_OIDC_CLIENT_ID and restart.", 503, [])
        return _page("MovieNight", "Sign in to pick tonight's film.", links=links, script=_TV_FIRST)

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
            await _plex_admit(st, account, token)
        except auth.LoginError as e:
            return _page("Could not sign you in", str(e), 403)
        except Exception as e:  # noqa: BLE001
            log.error("plex login failed: %s", safe_error(e))
            return _page("Could not sign you in", "Something went wrong talking to plex.tv.", 502)
        return _signed_in(st, auth.Session("plex:" + account["id"], account["name"]), next_path)

    async def _plex_admit(st, account: dict, token: str) -> None:
        """Only the Plex account this MovieNight belongs to gets in (the first one owns it). Raises LoginError."""
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

    @app.get("/login/plex/tv", include_in_schema=False)
    def login_plex_tv(request: Request, next: str = "/"):
        if request.app.state.plex_login is None:
            return RedirectResponse("/login", status_code=303)
        return _code_page(auth.safe_next(next))

    @app.post("/login/plex/code", include_in_schema=False)
    async def login_plex_code(request: Request, next: str = "/"):
        """TV-style sign-in: a short code to type at plex.tv/link on a phone that is signed in to Plex."""
        st = request.app.state
        if st.plex_login is None:
            return JSONResponse({"detail": "Plex sign-in is off"}, status_code=404)
        try:
            started = await st.plex_login.start_code(next)
        except auth.TooManyCodes as e:
            return JSONResponse({"detail": str(e)}, status_code=429)
        except Exception as e:  # noqa: BLE001
            log.error("plex code start failed: %s", safe_error(e))
            return JSONResponse({"detail": "plex.tv could not be reached. Try again in a moment."}, status_code=502)
        nonce = started.pop("nonce")
        res = JSONResponse(started, headers={"Cache-Control": "no-store"})
        res.set_cookie(auth.CODE_COOKIE, nonce, max_age=auth.CODE_SECONDS,
                       **{**st.sessions.cookie_kwargs(), "path": "/login/plex/code"})
        return res

    @app.get("/login/plex/code/{handle}", include_in_schema=False)
    async def login_plex_code_poll(request: Request, handle: str):
        """{"status": "pending" | "ok" | "expired"}; "ok" also sets the session cookie, like the callback."""
        st = request.app.state
        if st.plex_login is None:
            return JSONResponse({"detail": "Plex sign-in is off"}, status_code=404)
        headers = {"Cache-Control": "no-store"}
        try:
            done = await st.plex_login.poll_code(handle, request.cookies.get(auth.CODE_COOKIE))
            if done is None:
                return JSONResponse({"status": "pending"}, headers=headers)
            account, token, next_path = done
            await _plex_admit(st, account, token)
        except auth.CodeExpired as e:
            return JSONResponse({"status": "expired", "detail": str(e)}, status_code=410, headers=headers)
        except auth.LoginError as e:
            return JSONResponse({"status": "denied", "detail": str(e)}, status_code=403, headers=headers)
        except Exception as e:  # noqa: BLE001
            log.error("plex code login failed: %s", safe_error(e))
            return JSONResponse({"detail": "Something went wrong talking to plex.tv."}, status_code=502, headers=headers)
        res = JSONResponse({"status": "ok", "next": next_path}, headers=headers)
        res.set_cookie(auth.SESSION_COOKIE, st.sessions.sign("plex:" + account["id"], account["name"]),
                       max_age=auth.SESSION_DAYS * 86400, **st.sessions.cookie_kwargs())
        res.delete_cookie(auth.CODE_COOKIE, path="/login/plex/code")
        return res


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
            is_plex = cfg.media_server == "plex"
            token = plex_token(c, cfg) if is_plex else ""
            server_rows = [
                {"id": r["id"], "name": r["name"], "owned": bool(r["owned"]), "movies": r["movies"],
                 "enabled": bool(r["enabled"]), "reachable": bool(r["reachable"])}
                for r in c.execute("SELECT * FROM media_servers ORDER BY owned DESC, name")
            ] if is_plex else []
            movies = c.execute("SELECT COUNT(*) FROM movies").fetchone()[0]
            pushed = seenpush.pushed_count(c) if cfg.push_seen else None
            res = {
                "media_server": cfg.media_server,
                "letterboxd": {"username": lb.username(c, cfg.letterboxd_user), **counts,
                               "last_sync": db.get_meta(c, "lb_last_sync")},
                "plex": {"connected": bool(token), "account": (db.get_meta(c, "plex_account") or None) if is_plex else None,
                         "movies": movies if is_plex else 0,   # the shared tables hold the selected server's library only
                         "last_sync": db.get_meta(c, "plex_last_sync") if is_plex else None,
                         "servers": server_rows, "seen_pushed": pushed if is_plex else None},
                "tmdb": tmdb_counts(c, sync_mgr().tmdb.configured),
                "sync": dict(sync_mgr().state),
                "ready": bool(movies) and (counts["watched"] + counts["watchlist"]) > 0,
                "features": {"completionist": cfg.completionist, "seerr": cfg.seerr_configured},
            }
            if not is_plex:
                jf = c.execute("SELECT reachable, access_token FROM media_servers WHERE id LIKE 'jf:%'"
                               " ORDER BY id LIMIT 1").fetchone()
                problem = sync_mgr().jellyfin_problem
                if not cfg.jellyfin_configured:
                    problem = ("Jellyfin is selected but MOVIENIGHT_JELLYFIN_URL, MOVIENIGHT_JELLYFIN_USER"
                               " or MOVIENIGHT_JELLYFIN_PASSWORD is not set")
                res["jellyfin"] = {
                    "configured": cfg.jellyfin_configured,
                    "connected": bool(cfg.jellyfin_configured and jf and jf["access_token"] and jf["reachable"]),
                    "account": db.get_meta(c, "jellyfin_account") or None,
                    "movies": movies, "last_sync": db.get_meta(c, "jellyfin_last_sync"),
                    "seen_pushed": pushed, "problem": problem,
                }
            return res
        finally:
            c.close()

    # ---- Plex (only mounted when Plex is the selected media server)
    if cfg.media_server == "plex":
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
                for t in ("media_items", "movies", "media_servers", "dismissals"):
                    c.execute(f"DELETE FROM {t}")           # fixed table names
                db.set_meta(c, "plex_last_sync", None)
            engine().invalidate()
            return {"ok": True}

        @app.put("/api/plex/servers/{server_id}")
        def plex_server(server_id: str, body: ServerBody):
            with db.session(cfg.db_path) as c:
                cur = c.execute("UPDATE media_servers SET enabled=? WHERE id=?", (int(body.enabled), server_id))
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
                include_dismissed, server, q, era=None) -> queries.Filters:
        if mood in ("recent", "classic"):    # legacy moods, now eras
            mood, era = None, era or mood
        if mood and mood not in queries.MOOD_BY_ID:
            raise HTTPException(400, "Unknown mood")
        if era and queries.parse_era(era) is None:
            raise HTTPException(400, "Unknown era")
        return queries.Filters(mood=mood or None, era=era or None, genres=genre or [], max_runtime=max_runtime, min_year=min_year,
                               max_year=max_year, watchlist_only=watchlist_only, include_watched=include_watched,
                               include_dismissed=include_dismissed, server=server or None, q=(q or "").strip() or None)

    def dismissed_now() -> dict:
        c = conn()
        try:
            return library.active_dismissals(c)
        finally:
            c.close()

    def base_cands(st, tag: str | None, tag_family: str | None) -> list:
        """The library, or with a tag filter the films most like the ones the user tagged (see tags.py)."""
        tag, tag_family = (tag or "").strip() or None, (tag_family or "").strip() or None
        if not tag and not tag_family:
            return st.cands
        if tag and tag_family:
            raise HTTPException(400, "Use tag or tag_family, not both")
        try:
            kind, name, grp = lbtags.resolve(st, tag=tag, family=tag_family)
        except lbtags.UnknownTag as e:
            raise HTTPException(400, str(e)) from None
        return lbtags.group_cands(st, kind, name, grp)

    # ---- play on a TV: Google Cast (Chromecast / Android TV) running the Plex receiver; Plex only
    players_cache: dict = {"at": 0.0, "items": []}

    async def cast_players() -> list[dict]:
        if cfg.media_server != "plex":
            return []
        now = time.monotonic()
        if players_cache["at"] and now - players_cache["at"] < 60:
            return players_cache["items"]
        c = conn()
        try:
            token, cid = plex_token(c, cfg), db.client_id(c)
        finally:
            c.close()
        if not token:
            return []
        hints: list[dict] = []
        try:
            hints = await plex.get_players(app.state.http, cid, token)   # plex.tv knows the TVs' LAN IPs
        except (httpx.HTTPError, ValueError) as e:
            log.warning("plex players lookup failed: %s", safe_error(e))
        items = await cast.discover(cast.candidate_hosts(cfg.cast_hosts, hints))
        players_cache.update(at=now, items=items)
        return items

    @app.get("/api/players")
    async def players():
        return [{"id": p["id"], "name": p["name"], "product": p["product"]} for p in await cast_players()]

    @app.post("/api/play", status_code=204)
    async def play(body: PlayBody):
        if cfg.media_server != "plex":
            raise HTTPException(400, "Playing on a player is only available with Plex")
        player = next((p for p in await cast_players() if p["id"] == body.player_id), None)
        if player is None:
            raise HTTPException(404, "Player not found")
        c = conn()
        try:
            row = c.execute(
                "SELECT s.base_url, s.access_token FROM media_items i JOIN media_servers s ON s.id=i.server_id"
                " WHERE i.server_id=? AND i.rating_key=? AND s.base_url IS NOT NULL",
                (body.server_id, body.rating_key)).fetchone()
            token, cid = plex_token(c, cfg), db.client_id(c)
        finally:
            c.close()
        if row is None:
            raise HTTPException(404, "Movie not found on that server")
        server_token = row["access_token"] or token
        # Send the TV a connection that actually answers from here (local first, then remote, relay last):
        # a shared server's "local" address belongs to its own network and may be unreachable.
        server_url = row["base_url"]
        try:
            srv = next((s for s in await plex.get_servers(app.state.http, cid, token)
                        if s["id"] == body.server_id), None)
        except (httpx.HTTPError, ValueError) as e:
            log.warning("plex servers lookup failed: %s", safe_error(e))
            srv = None
        if srv:
            server_url = await plex.pick_connection(app.state.http, srv, cid) or server_url
            server_token = srv["access_token"] or server_token
        ok = await cast.play(player, server_url, server_token, body.rating_key)
        if not ok:
            raise HTTPException(502, f"Could not reach {player['name']}. Is it on and on the same network?")
        return Response(status_code=204)

    @app.get("/api/moods")
    def moods():
        return queries.moods_public()

    @app.get("/api/eras")
    def eras(
        mood: str | None = None, genre: list[str] = Query(default=[]),
        max_runtime: int | None = Query(None, ge=1), min_year: int | None = None, max_year: int | None = None,
        watchlist_only: bool = False, include_watched: bool = False, include_dismissed: bool = False,
        server: str | None = None, q: str | None = None, tag: str | None = None, tag_family: str | None = None,
        complete: bool = False,
    ):
        """Eras that still have a film to suggest under the other filters (era itself is ignored)."""
        f = filters(mood, genre, max_runtime, min_year, max_year, watchlist_only, include_watched,
                    include_dismissed, server, q)
        if f.era:     # legacy mood=recent|classic means an era, which this endpoint ignores
            f.era = None
        st = engine().get()
        cands = base_cands(st, tag, tag_family)
        if complete:
            nexts = saga_next(st)
            cands = [c for c in cands if c.id in nexts]
        return queries.eras_public(queries.apply_filters(cands, f, dismissed_now()))

    @app.get("/api/tags")
    def tags():
        """Letterboxd diary tags that can steer picks (tag / tag_family on /api/recommendations and /api/pick)."""
        return {"min_films": lbtags.MIN_GROUP_FILMS, "families": lbtags.usable(engine().get())}

    def saga_next(st) -> dict[int, dict]:
        """movie id -> the started saga it continues (Tonight's "Complete a saga"); {} with the completionist off."""
        if not cfg.completionist:
            return {}
        c = conn()
        try:
            return completion.saga_next(st, c)
        finally:
            c.close()

    def film_groups(st, tmdb_id: int | None) -> list[dict]:
        """The groups a film is part of, for its detail; [] with the completionist off."""
        if not cfg.completionist or not tmdb_id:
            return []
        c = conn()
        try:
            return completion.groups_of(st, c, tmdb_id)
        finally:
            c.close()

    def saga_items(cands: list, st, dismissed: dict, nexts: dict[int, dict] | None) -> list[dict]:
        """movie_json of each film; with `nexts` (the Complete a saga filter), its saga named first in the reasons.
        With the completionist on, each also carries `groups` ("Part of X · n of N", from the completion cache)."""
        out = []
        c = conn() if cfg.completionist else None
        try:
            for cand in cands:
                m = library.movie_json(cand, st, dismissed)
                if nexts and cand.id in nexts:
                    m["reasons"] = [completion.saga_reason(nexts[cand.id])] + m["reasons"][:3]
                    m["saga"] = nexts[cand.id]
                if c is not None:
                    m["groups"] = completion.groups_of(st, c, cand.tmdb_id)
                out.append(m)
        finally:
            if c is not None:
                c.close()
        return out

    # ---- completionist: sagas, franchises, series, canonical lists and director/actor filmographies
    # (MOVIENIGHT_COMPLETIONIST=0: not mounted, so every /api/completions* answers 404)
    if cfg.completionist:
        @app.get("/api/completions")
        def completions(kind: Literal["collection", "franchise", "series", "list", "director", "actor"] | None = None, hidden: bool = False):
            st = engine().get()
            c = conn()
            try:
                return completion.overview(st, c, kind=kind, show_hidden=hidden)
            finally:
                c.close()

        @app.get("/api/completions/{key}")
        def completion_group(key: str):
            st = engine().get()
            c = conn()
            try:
                g = completion.group(st, c, key)
            finally:
                c.close()
            if g is None:
                raise HTTPException(404, "Group not found")
            return g

        @app.post("/api/completions/{key}/hide")
        def completion_hide(key: str):
            st = engine().get()
            with db.session(cfg.db_path) as c:
                if not any(g["key"] == key for g in completion.build(st, c)):
                    raise HTTPException(404, "Group not found")
                c.execute("INSERT OR REPLACE INTO completion_hidden(group_key, created_at) VALUES(?,?)", (key, now_iso()))
            return {"ok": True}

        @app.delete("/api/completions/{key}/hide")
        def completion_unhide(key: str):
            with db.session(cfg.db_path) as c:
                c.execute("DELETE FROM completion_hidden WHERE group_key=?", (key,))
            return {"ok": True}

    # ---- Seerr (Overseerr / Jellyseerr): request films missing from the library, as each user's own Seerr account
    # (connected through Plex; only Seerr's session cookie is stored). With MOVIENIGHT_SEERR_URL unset only
    # /api/seerr/status answers ({"enabled": false}); every other /api/seerr/* route is 404.
    def _seerr_user(request: Request) -> str:
        """The signed-in user (session subject); with login off there is a single, local user."""
        session = getattr(request.state, "session", None)
        if session is not None:
            return session.sub
        if request.app.state.sessions is not None:
            raise HTTPException(401, "Not signed in")
        return "local"

    def _seerr_failed(e: seerr.SeerrError) -> HTTPException:
        if e.status >= 500:
            log.warning("Seerr: %s", e)
        return HTTPException(e.status, str(e))

    @app.get("/api/seerr/status")
    def seerr_account(request: Request):
        """This user's Seerr link: {"enabled", "connected", "expired", "name"}."""
        if not cfg.seerr_configured:
            return {"enabled": False, "connected": False, "expired": False, "name": None}
        return app.state.seerr.status(_seerr_user(request))

    if cfg.seerr_configured:
        @app.post("/api/seerr/connect")
        async def seerr_connect(request: Request):
            """Starts connecting this user's Seerr account: {"url": the app.plex.tv page to approve it on}."""
            user = _seerr_user(request)
            origin = (request.headers.get("origin") or "").strip()
            if not origin.startswith(("http://", "https://")):
                origin = str(request.base_url)
            try:
                url = await app.state.seerr.start(user, origin)
            except seerr.SeerrError as e:
                raise _seerr_failed(e) from None
            return JSONResponse({"url": url}, headers={"Cache-Control": "no-store"})

        @app.get("/api/seerr/callback", include_in_schema=False)
        async def seerr_callback(request: Request, pin: int = 0):
            """Where app.plex.tv sends the user back: the Plex token becomes a Seerr session (never stored)."""
            user = _seerr_user(request)
            try:
                await app.state.seerr.finish(user, pin)
            except seerr.SeerrError as e:
                log.warning("Seerr connect failed: %s", e)
                return RedirectResponse("/?seerr=failed", status_code=303)
            except Exception as e:  # noqa: BLE001
                log.error("Seerr connect failed: %s", safe_error(e))
                return RedirectResponse("/?seerr=failed", status_code=303)
            return RedirectResponse("/?seerr=connected", status_code=303)

        @app.delete("/api/seerr/session")
        async def seerr_disconnect(request: Request):
            """Disconnects this user's Seerr account (and ends the session on Seerr, best effort)."""
            await app.state.seerr.disconnect(_seerr_user(request))
            return {"ok": True}

        @app.get("/api/seerr/statuses")
        async def seerr_statuses(request: Request, tmdb: str = Query("", max_length=20000)):
            """Request states of films by TMDB id (`?tmdb=1,2,3`): {"statuses": {"1": "requested"}}, each
            none | requested | available. 409 when the user is not connected or the session expired."""
            try:
                ids = [int(x) for x in tmdb.split(",") if x.strip()]
            except ValueError:
                raise HTTPException(422, "Invalid tmdb: comma-separated TMDB ids") from None
            try:
                got = await app.state.seerr.statuses(_seerr_user(request))
            except seerr.SeerrError as e:
                raise _seerr_failed(e) from None
            return {"statuses": {str(i): got.get(i, seerr.NONE) for i in dict.fromkeys(ids) if i > 0}}

        @app.post("/api/seerr/request/{tmdb_id}")
        async def seerr_request(request: Request, tmdb_id: int):
            """Request a film as this user's Seerr account: {"status": "requested" | "available"}. Already
            requested counts as requested; 403 when the account may not request, 409 when it must reconnect."""
            if tmdb_id < 1:
                raise HTTPException(404, "Film not found")
            try:
                state = await app.state.seerr.request(_seerr_user(request), tmdb_id)
            except seerr.SeerrError as e:
                raise _seerr_failed(e) from None
            return {"status": state}

    @app.get("/api/recommendations")
    def recommendations(
        limit: int = Query(24, ge=1, le=200), offset: int = Query(0, ge=0),
        sort: Literal["match", "rating", "recent", "year", "runtime"] = "match",
        mood: str | None = None, era: str | None = None, genre: list[str] = Query(default=[]),
        max_runtime: int | None = Query(None, ge=1), min_year: int | None = None, max_year: int | None = None,
        watchlist_only: bool = False, include_watched: bool = False, include_dismissed: bool = False,
        server: str | None = None, q: str | None = None, tag: str | None = None, tag_family: str | None = None,
        complete: bool = False,
    ):
        f = filters(mood, genre, max_runtime, min_year, max_year, watchlist_only, include_watched,
                    include_dismissed, server, q, era)
        st, dismissed = engine().get(), dismissed_now()
        cands = base_cands(st, tag, tag_family)
        nexts = saga_next(st) if complete else None
        if nexts is not None:           # "Complete a saga": the next film of each started saga, in the library
            cands = [c for c in cands if c.id in nexts]
        matched = queries.sort_cands(queries.apply_filters(cands, f, dismissed), sort)
        return {
            "items": saga_items(matched[offset:offset + limit], st, dismissed, nexts),
            "total": len(matched),
            "facets": queries.facets(cands, f, dismissed),
        }

    @app.get("/api/pick")
    def pick(
        exclude: list[int] = Query(default=[]),
        mood: str | None = None, era: str | None = None, genre: list[str] = Query(default=[]),
        max_runtime: int | None = Query(None, ge=1), min_year: int | None = None, max_year: int | None = None,
        watchlist_only: bool = False, include_watched: bool = False, include_dismissed: bool = False,
        server: str | None = None, q: str | None = None, tag: str | None = None, tag_family: str | None = None,
        complete: bool = False,
    ):
        """A film for tonight and a few alternatives. `complete`: only the next film of each started saga that is in
        the library, its saga named first in the reasons. `saga_count` (with the completionist on): how many such
        films the other filters leave, so Tonight shows its "Complete a saga" chip only when there is one."""
        f = filters(mood, genre, max_runtime, min_year, max_year, watchlist_only, include_watched,
                    include_dismissed, server, q, era)
        st, dismissed = engine().get(), dismissed_now()
        matched = queries.apply_filters(base_cands(st, tag, tag_family), f, dismissed)
        nexts = saga_next(st)
        if complete:
            matched = [c for c in matched if c.id in nexts]
        result = queries.pick(matched, set(exclude), random.Random())
        if result is None:
            raise HTTPException(404, "Nothing matches these filters")
        chosen, alts = result
        shown = nexts if complete else None
        out = {"pick": saga_items([chosen], st, dismissed, shown)[0],
               "alternatives": saga_items(alts, st, dismissed, shown)}
        if cfg.completionist:
            out["saga_count"] = len(matched) if complete else sum(c.id in nexts for c in matched)
        return out

    @app.get("/api/movies/{movie_id}")
    def movie(movie_id: int):
        st, dismissed = engine().get(), dismissed_now()
        c = st.by_id.get(movie_id)
        if c is None:
            raise HTTPException(404, "Movie not found")
        out = library.movie_detail_json(c, st, dismissed)
        if cfg.completionist:
            out["groups"] = film_groups(st, c.tmdb_id)    # "Part of X · n of N", best first
        return out

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
            _store_vote(c, library.feedback_key(row["tmdb_id"], row["dkey"]), body.vote)
        engine().invalidate()
        return {"ok": True}

    def _store_vote(c, key: str, vote: int) -> None:
        if vote == 0:
            c.execute("DELETE FROM votes WHERE key=?", (key,))
        else:
            c.execute("INSERT INTO votes(key, vote, updated_at) VALUES(?,?,?) ON CONFLICT(key)"
                      " DO UPDATE SET vote=excluded.vote, updated_at=excluded.updated_at", (key, vote, now_iso()))

    @app.post("/api/movies/{movie_id}/seen")
    async def mark_seen(movie_id: int, body: SeenBody):
        with db.session(cfg.db_path) as c:
            row = c.execute("SELECT tmdb_id, dkey FROM movies WHERE id=?", (movie_id,)).fetchone()
            if row is None:
                raise HTTPException(404, "Movie not found")
            key = library.feedback_key(row["tmdb_id"], row["dkey"])
        return await _set_seen(key, body)

    async def _set_seen(key: str, body: SeenBody) -> dict:
        """Store (or remove) the "seen it" mark of `key` and, when given, its vote in one transaction, then push
        it to the media server when MOVIENIGHT_PUSH_SEEN is on (only items the app marked itself are undone)."""
        with db.session(cfg.db_path) as c:
            if body.seen:
                c.execute("INSERT OR IGNORE INTO seen(key, created_at) VALUES(?,?)", (key, now_iso()))
            else:
                c.execute("DELETE FROM seen WHERE key=?", (key,))
            if body.vote is not None:          # same transaction: seen + vote land together or not at all
                _store_vote(c, key, body.vote)
        if cfg.push_seen:
            # best effort: the local mark is saved; whatever fails here is retried by the sync
            c = conn()
            try:
                async with sync_mgr().push_lock:
                    cid = db.client_id(c)
                    token = history_token(c, cfg)
                    if body.seen:
                        await seenpush.push(c, app.state.http, cid, [key], backend=media,
                                            history_token=token,
                                            history_sink=((lambda items: sync_mgr().schedule_history(c, cid, items))
                                                          if token else None))
                    else:
                        await seenpush.unpush(c, app.state.http, cid, [key], backend=media, history_token=token)
            except Exception as e:  # noqa: BLE001
                log.warning("seen mark not synced to %s: %s", media.label, safe_error(e))
            finally:
                c.close()
        engine().invalidate()
        return {"ok": True}

    # ---- any film by TMDB id, in the library or not (Complete: seen and missing saga films)
    def _listed_film(st, c, tmdb_id: int) -> dict | None:
        """The film as a completionist group lists it (title, year, poster), for films without TMDB details."""
        if not cfg.completionist:
            return None
        return next((f for g in completion.build(st, c) for f in g["films"] if f["tmdb_id"] == tmdb_id), None)

    @app.get("/api/films/{tmdb_id}")
    async def film(tmdb_id: int):
        """A light detail view: library info, else the cached TMDB details (fetched once on demand), else what a
        group knows; plus how the film was seen and whether that mark can be undone here."""
        if tmdb_id < 1:
            raise HTTPException(404, "Film not found")
        st = engine().get()
        c = conn()
        try:
            base = _listed_film(st, c, tmdb_id)
            out = library.film_json(c, st, tmdb_id, base)
            if (out is None or not out["details"]) and sync_mgr().tmdb.configured:
                try:
                    det = await sync_mgr().tmdb.details(tmdb_id)
                except Exception as e:  # noqa: BLE001 - what is cached is still shown
                    log.warning("TMDB details for a film outside the library failed: %s", safe_error(e))
                    det = None
                if det:
                    c.execute("INSERT OR REPLACE INTO tmdb_details(tmdb_id,json,fetched_at) VALUES(?,?,?)",
                              (tmdb_id, json.dumps(det), now_iso()))
                    c.commit()
                    out = library.film_json(c, st, tmdb_id, base)
        finally:
            c.close()
        if out is None:
            raise HTTPException(404, "Film not found")
        out.pop("details", None)
        if cfg.completionist:
            out["groups"] = film_groups(st, tmdb_id)
        return out

    @app.post("/api/films/{tmdb_id}/seen")
    async def film_seen(tmdb_id: int, body: SeenBody):
        """Mark a film seen (with an optional verdict) by TMDB id, whether it is in the library or not. The mark is
        stored as `tmdb:<id>`, so a library film with that TMDB id added later is seen, and the vote counts, from
        then on. Unmarking undoes the app's own push to the media server (MOVIENIGHT_PUSH_SEEN), at once. A film
        seen on Letterboxd, or played on the media server itself, cannot be unmarked here (409)."""
        st = engine().get()
        c = conn()
        try:
            known = (c.execute("SELECT 1 FROM movies WHERE tmdb_id=? UNION SELECT 1 FROM tmdb_details WHERE tmdb_id=?"
                               " AND json != '{}'", (tmdb_id, tmdb_id)).fetchone() is not None
                     or _listed_film(st, c, tmdb_id) is not None)
            if not known:
                raise HTTPException(404, "Film not found")
            if not body.seen:
                f = library.film_json(c, st, tmdb_id, {"title": "?"})
                if f and ("letterboxd" in f["seen_via"] or "server" in f["seen_via"]):
                    where = "Letterboxd" if "letterboxd" in f["seen_via"] else media.label
                    raise HTTPException(409, f"Seen on {where}: it can only be unmarked there")
        finally:
            c.close()
        return await _set_seen(f"tmdb:{tmdb_id}", body)

    @app.get("/api/seen-candidates")
    def seen_candidates(limit: int = Query(60, ge=1, le=200), offset: int = Query(0, ge=0)):
        st, dismissed = engine().get(), dismissed_now()
        items, total = library.seen_candidates(st, dismissed, limit, offset)
        return {"items": [library.movie_json(c, st, dismissed, with_reasons=False) for c in items], "total": total}

    @app.get("/api/seen-marked")
    def seen_marked(limit: int = Query(60, ge=1, le=200), offset: int = Query(0, ge=0)):
        """Films marked "seen it" here, newest mark first, with their current vote (`my_vote`)."""
        st, dismissed = engine().get(), dismissed_now()
        c = conn()
        try:
            items, total = library.seen_marked(c, st, limit, offset)
        finally:
            c.close()
        return {"items": [library.movie_json(m, st, dismissed, with_reasons=False) for m in items], "total": total}

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
                "SELECT i.json, s.base_url, s.access_token, db_cid.value cid FROM media_items i"
                " JOIN media_servers s ON s.id=i.server_id LEFT JOIN meta db_cid ON db_cid.key='plex_client_id'"
                " WHERE i.movie_id=? AND s.enabled=1 AND s.base_url IS NOT NULL ORDER BY s.owned DESC", (movie_id,)
            ).fetchall()
        import json as _json
        for r in row:
            path = _json.loads(r["json"]).get(field)
            if not path:
                continue
            w, h = (500, 750) if kind == "poster" else (1280, 720)
            got = await media.fetch_image(app.state.http, r["base_url"], r["access_token"] or "", r["cid"] or "",
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
    app_version = _app_version()
    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

        @app.middleware("http")
        async def revalidate_static_code(request, call_next):
            # JS and CSS have no version strings in their URLs, so make the browser revalidate (ETag) on every
            # load instead of reusing a heuristically cached copy after a deploy. Images keep default caching.
            resp = await call_next(request)
            if request.url.path.startswith(("/static/js/", "/static/css/")) and resp.status_code < 400:
                resp.headers["Cache-Control"] = "no-cache"
            return resp

    @app.get("/", include_in_schema=False)
    def index():
        f = STATIC_DIR / "index.html"
        if f.is_file():
            # The page learns which version it was loaded as; the client compares it with the API header.
            html = f.read_text(encoding="utf-8").replace(version.PLACEHOLDER, app_version)
            return HTMLResponse(html, headers={"Cache-Control": "no-cache"})
        return HTMLResponse("<!doctype html><title>MovieNight</title><h1>MovieNight</h1>"
                            "<p>Frontend not installed yet. The API is available under /api.</p>")

    @app.middleware("http")
    async def _version_header(request, call_next):
        # Added last, so it is outermost: also stamps 401s from the auth middleware.
        resp = await call_next(request)
        if request.url.path.startswith("/api/"):
            resp.headers[version.HEADER] = app_version
        return resp

    return app


def _sniff(data: bytes) -> str:
    if data[:8].startswith(b"\x89PNG"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


app = create_app()
