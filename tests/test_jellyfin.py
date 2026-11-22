"""Jellyfin as the media server (MOVIENIGHT_MEDIA_SERVER=jellyfin). A fake server behind
httpx.MockTransport, payloads shaped like the Jellyfin API docs; synthetic data only, no network."""
import json
import sqlite3
import time
from dataclasses import replace
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import seed
from movienight import db, jellyfin, library
from movienight.app import create_app
from movienight.config import Config

BASE = "https://jf.example.test"
PASSWORD = "pw-SECRET-123"
SERVER_ID = "f00dcafe"
USER_ID = "u-42"


def jf_item(item_id, name, year, tmdb=None, imdb=None, played=False, width=1920, height=1080, **kw):
    it = {"Id": item_id, "Name": name, "ServerId": SERVER_ID, "Type": "Movie", "ProductionYear": year,
          "ProviderIds": {k: v for k, v in (("Tmdb", str(tmdb) if tmdb else None), ("Imdb", imdb)) if v},
          "RunTimeTicks": 6_000_000_000 * 10 + 300_000_000, "CommunityRating": 7.4,
          "Overview": "o", "Taglines": ["t"], "Genres": kw.get("genres", ["Crime"]),
          "People": [{"Name": "Dir One", "Type": "Director"}, {"Name": "Actor A", "Type": "Actor"},
                     {"Name": "Writer W", "Type": "Writer"}],
          "ProductionLocations": ["United States of America"], "DateCreated": "2024-03-05T10:11:12.1234567Z",
          "ImageTags": {"Primary": "abc"}, "BackdropImageTags": ["def"],
          "MediaStreams": [{"Type": "Audio"}, {"Type": "Video", "Width": width, "Height": height}],
          "UserData": {"PlayCount": 2 if played else 0, "Played": played, "IsFavorite": False}}
    if kw.get("no_ids"):
        it.pop("ProviderIds")
    return it


LIBRARY = [jf_item("a1", "Crime Two", 2016, 111), jf_item("a2", "Rom Com", 2020, 112, genres=["Romance"]),
           jf_item("a3", "Played One", 2005, 113, played=True), jf_item("a4", "Gamma", 2001, 303),
           jf_item("a5", "No Ids Movie", 1999, no_ids=True)]


class FakeJellyfin:
    def __init__(self, items=None):
        self.items = list(items if items is not None else LIBRARY)
        self.calls: list[httpx.Request] = []
        self.tokens = set()
        self.logins = 0
        self.legacy_played = False      # True: /UserPlayedItems is missing (Jellyfin 10.8)
        self.played_status = 200

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        h, path = request.url.host, request.url.path
        if h != "jf.example.test":
            raise AssertionError(f"unexpected network call: {h}{path}")
        if path == "/System/Info/Public":
            return httpx.Response(200, json={"Id": SERVER_ID, "ServerName": "Den", "Version": "12.4.0",
                                             "ProductName": "Jellyfin Server", "StartupWizardCompleted": True})
        if path == "/Users/AuthenticateByName" and request.method == "POST":
            body = json.loads(request.content)
            if body != {"Username": "viewer", "Pw": PASSWORD}:
                return httpx.Response(401)
            self.logins += 1
            token = f"TOKEN-{self.logins}"
            self.tokens.add(token)
            return httpx.Response(200, json={"AccessToken": token, "ServerId": SERVER_ID,
                                             "User": {"Id": USER_ID, "Name": "Viewer", "ServerId": SERVER_ID}})
        token = self.token_of(request)
        if token not in self.tokens:
            return httpx.Response(401)
        if path == "/Items" and request.method == "GET":
            q = parse_qs(request.url.query.decode())
            start, limit = int(q["startIndex"][0]), int(q["limit"][0])
            assert q["userId"] == [USER_ID] and q["includeItemTypes"] == ["Movie"] and q["recursive"] == ["true"]
            assert "ProviderIds" in q["fields"][0]
            return httpx.Response(200, json={"Items": self.items[start:start + limit],
                                             "TotalRecordCount": len(self.items), "StartIndex": start})
        if path.startswith("/UserPlayedItems/"):
            if self.legacy_played:
                return httpx.Response(404)
            return httpx.Response(self.played_status, json={"Played": request.method == "POST"})
        if path.startswith(f"/Users/{USER_ID}/PlayedItems/"):
            return httpx.Response(self.played_status, json={})
        if path.startswith("/Items/") and "/Images/" in path:
            return httpx.Response(200, content=b"\xff\xd8\xff\xe0JPEGDATA", headers={"content-type": "image/jpeg"})
        return httpx.Response(404)

    @staticmethod
    def token_of(request) -> str:
        auth = request.headers.get("Authorization", "")
        for part in auth.removeprefix("MediaBrowser ").split(", "):
            k, _, v = part.partition("=")
            if k == "Token":
                return v.strip('"')
        return ""

    def paths(self, prefix=""):
        return [(r.method, r.url.path) for r in self.calls if r.url.path.startswith(prefix)]


def jf_cfg(tmp_path, **over):
    base = dict(data_dir=tmp_path / "d", letterboxd_dir=tmp_path / "nolb", sync_on_start=False,
                media_server="jellyfin", login="none", jellyfin_url=BASE, jellyfin_user="viewer",
                jellyfin_password=PASSWORD)
    return Config(**{**base, **over})


def seed_history(db_path):
    """Letterboxd history only (conftest.seed also adds a Plex library, which a Jellyfin run must not have)."""
    from conftest import DETAILS, HISTORY
    from movienight.util import norm_title
    with db.session(db_path) as c:
        for name, year, tid, rating, liked, wl in HISTORY:
            c.execute("INSERT INTO lb_films(name,year,norm_title,tmdb_id,resolve_state,watched,rating,liked,watchlist,"
                      "watched_date) VALUES(?,?,?,?,1,?,?,?,?,?)",
                      (name, year, norm_title(name), tid, 0 if wl else 1, rating, liked, wl, "2024-01-01"))
        for d in DETAILS:
            c.execute("INSERT INTO tmdb_details(tmdb_id,json) VALUES(?,?)", (d["id"], json.dumps(d)))


def wait_sync(c):
    for _ in range(200):
        s = c.get("/api/status").json()
        if not s["sync"]["running"]:
            return s
        time.sleep(0.02)
    raise AssertionError("sync did not finish")


def run_sync(c, what="all"):
    assert c.post("/api/sync", json={"what": what}).status_code == 202
    return wait_sync(c)


@pytest.fixture
def jf(tmp_path):
    fake = FakeJellyfin()
    cfg = jf_cfg(tmp_path)
    app = create_app(cfg, transport=httpx.MockTransport(fake))
    with TestClient(app) as c:
        seed_history(cfg.db_path)
        c.cfg, c.fake = cfg, fake
        yield c


def items_by_title(c, **params):
    r = c.get("/api/recommendations", params={"limit": 200, **params}).json()
    return {m["title"]: m for m in r["items"]}


# --------------------------------------------------------------------------- client

def test_auth_header_format():
    h = jellyfin.auth_header("dev1", "TOK")["Authorization"]
    assert h == 'MediaBrowser Client="MovieNight", Device="MovieNight", DeviceId="dev1", Version="1.0", Token="TOK"'
    assert "Token" not in jellyfin.auth_header("dev1")["Authorization"]


def test_parse_item():
    it = jellyfin.parse_item(jf_item("x9", "Some Film", 2011, 777, "tt0000001", played=True, width=3840, height=1600))
    assert it["rating_key"] == "x9" and it["tmdb_id"] == 777 and it["imdb_id"] == "tt0000001"
    assert it["view_count"] == 2 and it["duration_min"] == 100 and it["rating"] == 7.4
    assert it["resolution"] == "4k" and it["added_at"] == "2024-03-05T10:11:12Z"
    assert it["thumb"] == "/Items/x9/Images/Primary" and it["art"] == "/Items/x9/Images/Backdrop"
    assert it["directors"] == ["Dir One"] and it["roles"] == ["Actor A"] and it["tagline"] == "t"
    assert set(it) == set(library_item_keys())
    bare = jellyfin.parse_item({"Id": "y", "Name": "Bare", "UserData": {"Played": True, "PlayCount": 0},
                                "Width": 1280, "Height": 536})
    assert bare["tmdb_id"] is None and bare["imdb_id"] is None and bare["view_count"] == 1
    assert bare["thumb"] is None and bare["art"] is None and bare["resolution"] == "720"
    assert bare["duration_min"] is None and bare["added_at"] is None
    assert jellyfin.parse_item(jf_item("z", "Odd", 2000, no_ids=True))["tmdb_id"] is None
    assert jellyfin.parse_item(jf_item("s", "Small", 2000, width=720, height=480))["resolution"] == "sd"


def library_item_keys():
    from movienight import plex
    return plex.parse_item({"ratingKey": 1}).keys()


def test_fetch_movies_pages(monkeypatch):
    monkeypatch.setattr(jellyfin, "PAGE_SIZE", 2)
    fake = FakeJellyfin()
    fake.tokens.add("T")
    seen = []

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(fake)) as http:
            return await jellyfin.fetch_movies(http, BASE, "dev", "T", USER_ID, lambda d, t: seen.append((d, t)))

    import asyncio
    items = asyncio.run(go())
    assert [i["rating_key"] for i in items] == ["a1", "a2", "a3", "a4", "a5"]
    assert len(fake.paths("/Items")) == 3 and seen[-1] == (5, 5)


def test_web_url():
    assert jellyfin.web_url(BASE + "/", "jf:f00dcafe", "a1") == f"{BASE}/web/#/details?id=a1&serverId=f00dcafe"


# --------------------------------------------------------------------------- sync

def test_full_sync(jf, caplog):
    c = jf
    with caplog.at_level("DEBUG"):
        s = run_sync(c)
    assert s["sync"]["error"] is None, s["sync"]
    assert s["media_server"] == "jellyfin"
    j = s["jellyfin"]
    assert j == {"configured": True, "connected": True, "account": "Viewer", "movies": 5,
                 "last_sync": j["last_sync"], "seen_pushed": None, "problem": None} and j["last_sync"]
    assert s["plex"]["connected"] is False and s["plex"]["servers"] == [] and s["ready"]
    assert all(r.url.host == "jf.example.test" for r in c.fake.calls)        # nothing to plex.tv
    assert c.fake.logins == 1
    for r in c.fake.calls:                                                    # token only in the header
        assert "TOKEN" not in str(r.url) and PASSWORD not in str(r.url)
    by = items_by_title(c)
    assert "Played One" not in by                                             # played in Jellyfin = watched
    assert by["Crime Two"]["plex"] == [{"server": "Den", "server_id": "jf:f00dcafe", "resolution": "1080",
                                        "url": f"{BASE}/web/#/details?id=a1&serverId=f00dcafe", "rating_key": "a1"}]
    assert "app_url" not in by["Crime Two"]["plex"][0]                        # Plex-only deep link
    assert by["Crime Two"]["poster_url"]                                      # TMDB poster
    assert by["No Ids Movie"]["poster_url"] == f"/api/image/{by['No Ids Movie']['id']}/poster"
    watched = items_by_title(c, include_watched=True)["Played One"]
    assert watched["watched"] is True
    texts = c.get("/api/status").text + json.dumps(items_by_title(c, include_watched=True))
    assert "TOKEN-" not in texts and PASSWORD not in texts
    assert "TOKEN-" not in caplog.text and PASSWORD not in caplog.text
    run_sync(c, "jellyfin")                                                   # second sync reuses the token
    assert c.fake.logins == 1


def test_reauth_after_401(jf):
    c = jf
    run_sync(c)
    c.fake.tokens.clear()                                                     # token revoked on the server
    s = run_sync(c)
    assert s["sync"]["error"] is None and c.fake.logins == 2
    assert s["jellyfin"]["connected"] and s["jellyfin"]["movies"] == 5
    with db.session(c.cfg.db_path) as conn:
        assert conn.execute("SELECT access_token FROM media_servers").fetchone()[0] == "TOKEN-2"


def test_wrong_password_records_problem(tmp_path, caplog):
    fake = FakeJellyfin()
    cfg = jf_cfg(tmp_path, jellyfin_password="wrong-SECRET-pw")
    with TestClient(create_app(cfg, transport=httpx.MockTransport(fake))) as c, caplog.at_level("DEBUG"):
        s = run_sync(c)
    assert "rejected the user name or password" in s["jellyfin"]["problem"]
    assert "jellyfin:" in s["sync"]["error"] and not s["jellyfin"]["connected"]
    assert "wrong-SECRET-pw" not in json.dumps(s) and "wrong-SECRET-pw" not in caplog.text


def test_missing_variables(tmp_path):
    fake = FakeJellyfin()
    cfg = jf_cfg(tmp_path, jellyfin_password="")
    with TestClient(create_app(cfg, transport=httpx.MockTransport(fake))) as c:
        s = c.get("/api/status").json()
        assert s["jellyfin"]["configured"] is False and "MOVIENIGHT_JELLYFIN_PASSWORD" in s["jellyfin"]["problem"]
        s = run_sync(c)
        assert "Jellyfin is selected but" in s["sync"]["error"]
    assert fake.calls == []


def test_jellyfin_watched_off_keeps_played_films(tmp_path):
    fake = FakeJellyfin()
    cfg = jf_cfg(tmp_path, jellyfin_watched=False)
    with TestClient(create_app(cfg, transport=httpx.MockTransport(fake))) as c:
        seed_history(cfg.db_path)
        run_sync(c)
        by = items_by_title(c)
        assert by["Played One"]["watched"] is False


def test_unreachable_server(tmp_path):
    def down(request):
        raise httpx.ConnectError("down", request=request)
    cfg = jf_cfg(tmp_path)
    with TestClient(create_app(cfg, transport=httpx.MockTransport(down))) as c:
        s = run_sync(c)
    assert "not reachable" in s["jellyfin"]["problem"] and not s["jellyfin"]["connected"]


# --------------------------------------------------------------------------- seen push

@pytest.fixture
def push_jf(tmp_path):
    fake = FakeJellyfin()
    cfg = jf_cfg(tmp_path, push_seen=True)
    app = create_app(cfg, transport=httpx.MockTransport(fake))
    with TestClient(app) as c:
        seed_history(cfg.db_path)
        run_sync(c)
        fake.calls.clear()
        c.cfg, c.fake = cfg, fake
        yield c


def mid(c, title):
    return next(x.id for x in c.app.state.engine.get().cands if x.title == title)


def test_push_and_unpush_call_jellyfin(push_jf):
    c = push_jf
    assert c.post(f"/api/movies/{mid(c, 'Crime Two')}/seen", json={"seen": True}).status_code == 200
    [req] = c.fake.calls
    assert (req.method, req.url.path, dict(req.url.params)) == ("POST", "/UserPlayedItems/a1", {"userId": USER_ID})
    assert FakeJellyfin.token_of(req) == "TOKEN-1"
    s = c.get("/api/status").json()
    assert s["jellyfin"]["seen_pushed"] == 1 and "TOKEN-" not in json.dumps(s)
    c.post(f"/api/movies/{mid(c, 'Crime Two')}/seen", json={"seen": False})
    assert c.fake.paths("/UserPlayedItems") == [("POST", "/UserPlayedItems/a1"), ("DELETE", "/UserPlayedItems/a1")]
    c.post(f"/api/movies/{mid(c, 'Played One')}/seen", json={"seen": True})   # already played there: left alone
    assert len(c.fake.calls) == 2


def test_push_falls_back_to_legacy_path(push_jf):
    c = push_jf
    c.fake.legacy_played = True
    c.post(f"/api/movies/{mid(c, 'Rom Com')}/seen", json={"seen": True})
    assert c.fake.paths() == [("POST", "/UserPlayedItems/a2"), ("POST", f"/Users/{USER_ID}/PlayedItems/a2")]
    with db.session(c.cfg.db_path) as conn:
        assert [tuple(r) for r in conn.execute("SELECT server_id, rating_key FROM seen_pushes")] == [("jf:f00dcafe", "a2")]


def test_push_error_is_retried_by_sync(push_jf, caplog):
    c = push_jf
    c.fake.played_status = 500
    with caplog.at_level("WARNING"):
        c.post(f"/api/movies/{mid(c, 'Gamma')}/seen", json={"seen": True})
    assert "played on Jellyfin server Den" in caplog.text and "TOKEN-" not in caplog.text
    c.fake.played_status = 200
    s = run_sync(c, "jellyfin")
    assert s["sync"]["error"] is None and s["jellyfin"]["seen_pushed"] == 1


def test_push_seen_env_and_alias(monkeypatch):
    for k in ("MOVIENIGHT_PUSH_SEEN", "MOVIENIGHT_PLEX_PUSH_SEEN", "MOVIENIGHT_MEDIA_SERVER", "MOVIENIGHT_LOGIN",
              "MOVIENIGHT_JELLYFIN_WATCHED"):
        monkeypatch.delenv(k, raising=False)
    assert Config.from_env().push_seen is False
    monkeypatch.setenv("MOVIENIGHT_PLEX_PUSH_SEEN", "1")
    assert Config.from_env().push_seen is True                 # older name still works
    monkeypatch.setenv("MOVIENIGHT_PUSH_SEEN", "0")
    assert Config.from_env().push_seen is False                # the new name wins when set
    monkeypatch.delenv("MOVIENIGHT_PLEX_PUSH_SEEN")
    monkeypatch.setenv("MOVIENIGHT_PUSH_SEEN", "yes")
    assert Config.from_env().push_seen is True
    assert Config.from_env().jellyfin_watched is True
    monkeypatch.setenv("MOVIENIGHT_JELLYFIN_WATCHED", "0")
    assert Config.from_env().jellyfin_watched is False


# --------------------------------------------------------------------------- images

def test_image_through_backend(jf):
    c = jf
    run_sync(c)
    r = c.get(f"/api/image/{mid(c, 'Crime Two')}/poster")
    assert r.status_code == 200 and r.content.startswith(b"\xff\xd8")
    [req] = [x for x in c.fake.calls if "/Images/" in x.url.path]
    assert req.url.path == "/Items/a1/Images/Primary" and req.url.params["maxWidth"] == "500"
    assert "TOKEN" not in str(req.url)


# --------------------------------------------------------------------------- switching servers

def test_switch_plex_to_jellyfin_wipes_library_once(tmp_path, caplog):
    cfg = Config(data_dir=tmp_path / "d", letterboxd_dir=tmp_path / "nolb", sync_on_start=False)
    db.migrate(cfg.db_path)
    seed(cfg.db_path)                                           # a Plex library (no meta.media_server yet)
    with db.session(cfg.db_path) as c:
        movie_id = c.execute("SELECT id FROM movies LIMIT 1").fetchone()[0]
        c.execute("INSERT INTO seen(key, created_at) VALUES('tmdb:111', 'x')")
        c.execute("INSERT INTO votes(key, vote, updated_at) VALUES('tmdb:112', 1, 'x')")
        c.execute("INSERT INTO dismissals(movie_id, until, created_at) VALUES(?, NULL, 'x')", (movie_id,))
        c.execute("INSERT INTO seen_pushes(key, server_id, rating_key, pushed_at) VALUES('tmdb:111','m1','11','x')")
        db.set_meta(c, "plex_token", "OLD-PLEX-TOKEN")
        db.set_meta(c, "plex_account", "someone")
    cfg.image_dir.mkdir(parents=True)
    (cfg.image_dir / f"{movie_id}_poster.img").write_bytes(b"old")

    fake = FakeJellyfin()
    jcfg = jf_cfg(tmp_path)
    with caplog.at_level("WARNING"), TestClient(create_app(jcfg, transport=httpx.MockTransport(fake))) as c:
        with db.session(jcfg.db_path) as conn:
            counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                      for t in ("media_items", "media_servers", "movies", "dismissals", "seen_pushes", "seen", "votes",
                                "lb_films", "tmdb_details")}
            assert db.get_meta(conn, "plex_token") is None and db.get_meta(conn, "plex_account") is None
            assert db.get_meta(conn, "media_server") == "jellyfin"
        assert counts == {"media_items": 0, "media_servers": 0, "movies": 0, "dismissals": 0, "seen_pushes": 0,
                          "seen": 1, "votes": 1, "lb_films": 5, "tmdb_details": 10}
        assert not list(cfg.image_dir.glob("*.img"))
        assert "media server changed from plex to jellyfin" in caplog.text
        run_sync(c)
        by = items_by_title(c)
        assert "Crime Two" not in by                                       # seen mark survived the switch
        assert by["Rom Com"]["my_vote"] == 1                               # and the vote too
    caplog.clear()
    with caplog.at_level("WARNING"), TestClient(create_app(jcfg, transport=httpx.MockTransport(fake))) as c:
        assert c.get("/api/status").json()["jellyfin"]["movies"] == 5      # second start: nothing wiped
    assert "media server changed" not in caplog.text


def test_plex_database_stays_plex(tmp_path):
    """A Plex database started again with Plex selected is never wiped."""
    cfg = Config(data_dir=tmp_path / "d", letterboxd_dir=tmp_path / "nolb", sync_on_start=False)
    db.migrate(cfg.db_path)
    seed(cfg.db_path)
    for _ in range(2):
        with TestClient(create_app(cfg, transport=httpx.MockTransport(lambda r: httpx.Response(404)))) as c:
            assert c.get("/api/status").json()["plex"]["movies"] == 8
    with db.session(cfg.db_path) as conn:
        assert db.get_meta(conn, "media_server") == "plex"


# --------------------------------------------------------------------------- login

def test_login_matrix(tmp_path):
    with pytest.raises(ValueError, match="MOVIENIGHT_LOGIN=plex needs"):
        jf_cfg(tmp_path, login="plex")
    # nothing configured: fail closed
    with TestClient(create_app(jf_cfg(tmp_path / "a", login=""), transport=httpx.MockTransport(FakeJellyfin())),
                    follow_redirects=False) as c:
        r = c.get("/api/status")
        assert r.status_code == 503 and "MOVIENIGHT_LOGIN=none" in r.json()["detail"]
        assert c.get("/healthz").status_code == 200
        assert c.get("/login").status_code == 503
    # none: open
    with TestClient(create_app(jf_cfg(tmp_path / "b", login="none"), transport=httpx.MockTransport(FakeJellyfin()))) as c:
        assert c.get("/api/status").status_code == 200 and c.get("/api/me").json()["auth"] is False
    # OIDC: sign-in required, only the OIDC button, no Plex routes
    cfg = jf_cfg(tmp_path / "c", login="", oidc_issuer="https://id.example.test", oidc_client_id="mn",
                 public_url="https://mn.example.test")
    assert cfg.login_methods == {"oidc"}
    with TestClient(create_app(cfg, transport=httpx.MockTransport(FakeJellyfin())), follow_redirects=False) as c:
        assert c.get("/api/status").status_code == 401
        page = c.get("/login").text
        assert "Sign in with Single sign-on" in page and "Plex" not in page
        assert c.app.state.plex_login is None
        assert c.get("/login/plex").headers["location"] == "/login"


def test_plex_routes_not_mounted_with_jellyfin(jf):
    assert jf.post("/api/plex/pin").status_code == 404
    assert jf.post("/api/plex/logout").status_code == 404


def test_unknown_media_server(monkeypatch):
    monkeypatch.setenv("MOVIENIGHT_MEDIA_SERVER", "emby")
    with pytest.raises(ValueError, match="MOVIENIGHT_MEDIA_SERVER must be one of plex, jellyfin"):
        Config.from_env()
    monkeypatch.setenv("MOVIENIGHT_MEDIA_SERVER", "Jellyfin")
    monkeypatch.delenv("MOVIENIGHT_LOGIN", raising=False)
    cfg = Config.from_env()
    assert cfg.media_server == "jellyfin" and cfg.login == "" and cfg.login_required


def test_password_not_in_config_repr():
    assert PASSWORD not in repr(Config(media_server="jellyfin", login="none", jellyfin_password=PASSWORD))


# --------------------------------------------------------------------------- migration 7

def test_migration_7_on_version_6_database(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    for i, script in enumerate(db.MIGRATIONS[:6], start=1):
        conn.executescript(script)
        conn.execute(f"PRAGMA user_version={i}")
    conn.execute("INSERT INTO plex_servers(id,name,owned,reachable,base_url,access_token) VALUES"
                 "('m1','Home',1,1,'http://plex.local:32400','SECRET')")
    conn.execute("INSERT INTO movies(id,dkey,tmdb_id) VALUES(7,'tmdb:111',111)")
    conn.executemany("INSERT INTO plex_items(server_id,rating_key,movie_id,title,year,tmdb_id,json) VALUES(?,?,?,?,?,?,?)",
                     [("m1", str(k), 7, f"T{k}", 2000, 111, "{}") for k in range(3)])
    conn.execute("INSERT INTO seen_pushes VALUES('tmdb:111','m1','0','x')")
    conn.commit()
    conn.close()

    db.migrate(path)
    c = db.connect(path)
    assert c.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)
    names = {r[0] for r in c.execute("SELECT name FROM sqlite_master")}
    assert {"media_servers", "media_items", "media_items_movie"} <= names
    assert not names & {"plex_servers", "plex_items", "plex_items_movie"}
    assert [tuple(r) for r in c.execute("SELECT id, name, access_token FROM media_servers")] == [("m1", "Home", "SECRET")]
    assert [tuple(r) for r in c.execute("SELECT rating_key, movie_id FROM media_items ORDER BY rating_key")] == \
        [("0", 7), ("1", 7), ("2", 7)]
    assert c.execute("SELECT COUNT(*) FROM seen_pushes").fetchone()[0] == 1
    c.close()
    db.migrate(path)                                            # idempotent


def test_status_plex_block_is_empty_with_jellyfin(jf):
    run_sync(jf)
    s = jf.get("/api/status").json()
    assert s["jellyfin"]["movies"] > 0
    assert s["plex"]["movies"] == 0 and s["plex"]["connected"] is False and s["plex"]["servers"] == []


# --------------------------------------------------------------------------- Plex mode unchanged

def test_plex_mode_status_payload_only_adds_media_server(client):
    s = client.get("/api/status").json()
    assert s["media_server"] == "plex" and "jellyfin" not in s
    assert set(s) == {"media_server", "letterboxd", "plex", "tmdb", "sync", "ready", "features"}
    assert set(s["plex"]) == {"connected", "account", "movies", "last_sync", "servers", "seen_pushed"}
    assert [x["id"] for x in s["plex"]["servers"]] == ["m1"]


def test_plex_mode_defaults(monkeypatch):
    for k in ("MOVIENIGHT_MEDIA_SERVER", "MOVIENIGHT_LOGIN", "MOVIENIGHT_OIDC_ISSUER"):
        monkeypatch.delenv(k, raising=False)
    cfg = Config.from_env()
    assert cfg.media_server == "plex" and cfg.login == "plex" and cfg.login_methods == {"plex"}
    assert Config(login="").login_required is False             # plex: no login asked = open, as before


def test_plex_mode_links_and_reasons(client):
    by = {m["title"]: m for m in client.get("/api/recommendations", params={"limit": 200}).json()["items"]}
    url = by["Crime Two"]["plex"][0]["url"]
    assert url == "https://app.plex.tv/desktop/#!/server/m1/details?key=%2Flibrary%2Fmetadata%2F11"
    texts = [r["text"] for m in by.values() for r in m["reasons"]]
    assert not any("Jellyfin" in t for t in texts)
    assert client.app.state.engine.get().server_label == "Plex"


def test_reasons_use_server_label(jf):
    run_sync(jf)
    st = jf.app.state.engine.get()
    assert st.server_label == "Jellyfin"
    from movienight import recommender as rec
    c = next(x for x in st.cands if x.title == "No Ids Movie")
    texts = [r["text"] for r in rec.reasons_for(c, st.profile, server=st.server_label)]
    assert not any("Plex" in t for t in texts)
