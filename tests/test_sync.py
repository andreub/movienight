import json

import httpx
from fastapi.testclient import TestClient

from conftest import FIX
from movienight.app import create_app
from movienight.config import Config


def handler(request: httpx.Request) -> httpx.Response:
    h, path = request.url.host, request.url.path
    if h == "plex.tv" and path == "/api/v2/resources":
        return httpx.Response(200, json=json.loads((FIX / "plex_resources.json").read_text()))
    if h == "plex.tv" and path == "/api/v2/user":
        return httpx.Response(200, json={"username": "tester"})
    if h == "plex.tv" and path == "/api/v2/pins":
        return httpx.Response(200, json={"id": 77, "code": "abcd"})
    if h == "plex.tv" and path == "/api/v2/pins/77":
        return httpx.Response(200, json={"authToken": "PLEX-USER-TOKEN"})
    if h == "remote.example":
        if path == "/identity":
            return httpx.Response(200, json={})
        if path == "/library/sections":
            return httpx.Response(200, json={"MediaContainer": {"Directory": [{"key": "1", "type": "movie"}, {"key": "2", "type": "show"}]}})
        if path == "/library/sections/1/all":
            return httpx.Response(200, content=(FIX / "plex_all.json").read_bytes())
    if h == "letterboxd.com":
        return httpx.Response(200, content=(FIX / "rss.xml").read_bytes())
    if h == "api.themoviedb.org":
        if path == "/3/search/movie":
            return httpx.Response(200, json={"results": []})
        if path.endswith("/recommendations"):
            return httpx.Response(200, json={"results": [{"id": 101}]})
        tid = int(path.split("/")[3])
        return httpx.Response(200, json={"id": tid, "title": f"T{tid}", "release_date": "2015-01-01",
                                         "genres": [{"name": "Crime"}], "vote_average": 7, "vote_count": 900})
    return httpx.Response(404)


def test_full_sync_end_to_end(tmp_path):
    cfg = Config(data_dir=tmp_path / "d", letterboxd_dir=FIX / "export", tmdb_token="eyJtest", sync_on_start=False,
                 plex_token="ENV-PLEX-TOKEN")
    app = create_app(cfg, transport=httpx.MockTransport(handler))
    with TestClient(app) as c:
        assert c.get("/api/status").json()["letterboxd"]["watched"] == 4
        assert c.post("/api/sync", json={"what": "all"}).status_code == 202
        for _ in range(200):
            s = c.get("/api/status").json()
            if not s["sync"]["running"]:
                break
            import time; time.sleep(0.05)
        assert s["sync"]["error"] is None, s["sync"]
        assert s["plex"]["connected"] and s["plex"]["movies"] == 3 and s["ready"]
        servers = {x["id"]: x for x in s["plex"]["servers"]}
        assert servers["mach1"]["reachable"] and not servers["mach2"]["reachable"]
        assert s["tmdb"]["enriched"] > 0 and s["tmdb"]["pending"] == 0
        assert s["letterboxd"]["watched"] == 6            # RSS added Epsilon and Brand New
        r = c.get("/api/recommendations", params={"include_watched": "true"}).json()
        assert r["total"] == 3
        by = {m["title"]: m for m in r["items"]}
        assert by["Alpha Heist"]["watched"] is True                          # Plex viewCount > 0
        assert by["No Ids Movie"]["poster_url"] is None                       # degraded: no thumb, no TMDB
        assert by["Zeta Noir"]["poster_url"].startswith("/api/image/")
        assert "PLEX-USER-TOKEN" not in c.get("/api/status").text + json.dumps(r)
        assert c.post("/api/sync", json={"what": "plex"}).status_code == 202


def test_sync_errors_are_recorded_not_raised(tmp_path):
    def boom(request):
        return httpx.Response(500)
    cfg = Config(data_dir=tmp_path / "d", letterboxd_dir=tmp_path / "x", plex_token="T", sync_on_start=False)
    app = create_app(cfg, transport=httpx.MockTransport(boom))
    with TestClient(app) as c:
        c.post("/api/sync", json={"what": "plex"})
        import time
        for _ in range(100):
            s = c.get("/api/status").json()
            if not s["sync"]["running"]:
                break
            time.sleep(0.05)
        assert s["sync"]["error"] and "T" != s["sync"]["error"] and "plex:" in s["sync"]["error"]
        assert c.get("/api/recommendations").status_code == 200


def test_pin_flow(tmp_path):
    cfg = Config(data_dir=tmp_path / "d", letterboxd_dir=tmp_path / "x", sync_on_start=False)
    app = create_app(cfg, transport=httpx.MockTransport(handler))
    with TestClient(app) as c:
        pin = c.post("/api/plex/pin").json()
        assert pin["id"] == 77 and pin["auth_url"].startswith("https://app.plex.tv/auth#?clientID=")
        assert "code=abcd" in pin["auth_url"] and "MovieNight" in pin["auth_url"]
        assert c.get("/api/plex/pin/77").json() == {"authorized": True}
        s = c.get("/api/status").json()
        assert s["plex"]["connected"] and s["plex"]["account"] == "tester"
        assert "PLEX-USER-TOKEN" not in json.dumps(s)
