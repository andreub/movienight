import io
import zipfile

from conftest import FIX


def test_health_status_moods(client):
    assert client.get("/healthz").json() == {"ok": True}
    s = client.get("/api/status").json()
    assert s["plex"]["movies"] == 8 and s["plex"]["servers"][0]["id"] == "m1"
    assert s["letterboxd"]["watched"] == 4 and s["letterboxd"]["rated"] == 4 and s["ready"] is True
    assert set(s["sync"]) == {"running", "stage", "done", "total", "error"}
    assert s["tmdb"]["configured"] is False
    assert "SECRET" not in client.get("/api/status").text
    moods = client.get("/api/moods").json()
    assert {"light", "short", "classic", "recent"} <= {m["id"] for m in moods}
    assert next(m for m in moods if m["id"] == "recent") == {
        "id": "recent", "label": "Something recent", "emoji": "🆕", "description": "Released in the last 18 months"}
    assert client.get("/api/recommendations", params={"mood": "recent"}).status_code == 200


def test_recommendations_shape_and_ranking(client):
    r = client.get("/api/recommendations").json()
    ids = [m["title"] for m in r["items"]]
    assert "Gamma" not in ids and "Plex Watched" not in ids          # watched (LB / Plex viewCount)
    assert ids[0] in ("Crime Two", "Epsilon Future")
    assert r["total"] == len(ids) == 6
    m = next(x for x in r["items"] if x["title"] == "Crime Two")
    for k in ("id", "title", "original_title", "year", "runtime", "tagline", "overview", "genres", "directors", "cast",
              "language", "countries", "poster_url", "backdrop_url", "tmdb_rating", "tmdb_votes", "tmdb_id",
              "letterboxd_url", "match", "reasons", "on_watchlist", "watched", "my_rating", "liked",
              "dismissed_until", "added_at", "plex"):
        assert k in m, k
    assert m["poster_url"].startswith("https://image.tmdb.org/t/p/w500/")
    assert m["plex"][0]["url"].startswith("https://app.plex.tv/desktop/#!/server/m1/details?key=")
    by = {x["title"]: x for x in r["items"]}
    assert by["Crime Two"]["plex"][0]["mobile_url"] == "https://watch.plex.tv/movie/crime-two-2016"
    assert "mobile_url" not in by["Rom Com"]["plex"][0]           # slug lookup found nothing
    assert "mobile_url" not in by["Drama Three"]["plex"][0]       # no Plex guid
    assert any("Fans of Alpha Heist" in x["text"] for x in m["reasons"])
    assert r["facets"]["genres"] and r["facets"]["min_year"] == 1975


def test_filters_and_sorts(client):
    g = lambda **p: client.get("/api/recommendations", params=p).json()
    assert [m["title"] for m in g(watchlist_only="true")["items"]] == ["Epsilon Future"]
    assert [m["title"] for m in g(mood="short")["items"]] == ["Horror Short"]
    assert [m["title"] for m in g(mood="classic")["items"]] == ["Horror Short"]
    assert {m["title"] for m in g(include_watched="true")["items"]} >= {"Gamma", "Plex Watched"}
    assert [m["title"] for m in g(genre="Horror")["items"]] == ["Horror Short"]
    assert g(max_runtime=90)["total"] == 1
    assert g(q="rom")["items"][0]["title"] == "Rom Com"
    assert [m["year"] for m in g(sort="year")["items"]] == sorted((m["year"] for m in g(sort="year")["items"]), reverse=True)
    assert g(limit=2, offset=1)["items"].__len__() == 2
    assert g(server="nope")["total"] == 0
    assert client.get("/api/recommendations", params={"mood": "zzz"}).status_code == 400
    assert "detail" in client.get("/api/recommendations", params={"limit": "x"}).json()


def test_pick(client):
    r = client.get("/api/pick").json()
    assert r["pick"]["id"] and len(r["alternatives"]) == 3
    assert r["pick"]["id"] not in {a["id"] for a in r["alternatives"]}
    ex = client.get("/api/pick", params=[("exclude", i) for i in range(1, 6)] + [("mood", "scary")])
    assert ex.status_code in (200, 404)
    assert client.get("/api/pick", params={"mood": "scary", "genre": "Nope"}).status_code == 404


def test_detail_dismiss_roundtrip(client):
    items = client.get("/api/recommendations").json()["items"]
    mid = next(m["id"] for m in items if m["title"] == "Crime Two")
    d = client.get(f"/api/movies/{mid}").json()
    assert d["keywords"] == ["heist"] and d["because"] and d["because"][0]["title"] == "Alpha Heist"
    assert client.post(f"/api/movies/{mid}/dismiss", json={"days": 30}).json() == {"ok": True}
    assert mid not in [m["id"] for m in client.get("/api/recommendations").json()["items"]]
    shown = client.get("/api/recommendations", params={"include_dismissed": "true"}).json()["items"]
    assert next(m for m in shown if m["id"] == mid)["dismissed_until"]
    assert client.delete(f"/api/movies/{mid}/dismiss").json() == {"ok": True}
    assert mid in [m["id"] for m in client.get("/api/recommendations").json()["items"]]
    client.post(f"/api/movies/{mid}/dismiss", json={"days": 0})
    assert mid not in [m["id"] for m in client.get("/api/recommendations").json()["items"]]
    assert client.get("/api/movies/99999").status_code == 404
    assert client.post("/api/movies/99999/dismiss", json={"days": 1}).status_code == 404


def test_profile(client):
    p = client.get("/api/profile").json()
    assert p["stats"]["watched"] == 4 and p["stats"]["rated"] == 4 and p["stats"]["liked"] == 1
    assert len(p["rating_histogram"]) == 10
    assert p["genres"][0]["name"] == "Crime" and p["directors"][0]["name"] == "Dir One"
    assert p["top_rated"][0]["title"] == "Alpha Heist"
    assert p["stats"]["library_unwatched"] == 6 and p["stats"]["watchlist_on_plex"] == 1


def test_server_toggle_and_logout(client):
    assert client.put("/api/plex/servers/m1", json={"enabled": False}).json() == {"ok": True}
    assert client.get("/api/recommendations").json()["total"] == 0
    assert client.put("/api/plex/servers/zzz", json={"enabled": True}).status_code == 404
    client.put("/api/plex/servers/m1", json={"enabled": True})
    assert client.post("/api/plex/logout").json() == {"ok": True}
    s = client.get("/api/status").json()
    assert s["plex"]["movies"] == 0 and not s["ready"]


def test_import_zip_endpoint(client):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for p in (FIX / "export").rglob("*.csv"):
            z.writestr(str(p.relative_to(FIX / "export")), p.read_bytes())
    r = client.post("/api/letterboxd/import", files={"file": ("e.zip", buf.getvalue())})
    assert r.status_code == 200 and set(r.json()) == {"watched", "rated", "liked", "watchlist"}
    bad = client.post("/api/letterboxd/import", files={"file": ("e.zip", b"junk")})
    assert bad.status_code == 400 and "detail" in bad.json()


def test_user_sync_and_errors(client):
    assert client.put("/api/letterboxd/user", json={"username": "someone"}).json() == {"ok": True}
    assert client.put("/api/letterboxd/user", json={"username": "bad name!"}).status_code == 422
    assert client.post("/api/sync", json={"what": "bogus"}).status_code == 422


def test_image_proxy_validates_and_404s(client):
    assert client.get("/api/image/1/evil").status_code == 422
    assert client.get("/api/image/99999/poster").status_code == 404


def test_root_serves_something(client):
    assert client.get("/").status_code == 200


def test_static_code_revalidates_but_images_do_not(client):
    for path in ("/static/js/main.js", "/static/css/app.css"):
        r = client.get(path)
        assert r.status_code == 200 and r.headers["cache-control"] == "no-cache" and r.headers.get("etag")
    assert client.get("/static/favicon.svg").headers.get("cache-control") != "no-cache"
    assert client.get("/").headers["cache-control"] == "no-cache"


def test_tv_client_assets_are_served(client):
    # The Android TV app's WebView adds "MovieNightTV" to its user agent; the UI keys D-pad navigation off it.
    assert "MovieNightTV" in client.get("/static/js/tvnav.js").text
    assert "MovieNightTV" in client.get("/static/js/play.js").text
    assert "initTvNav" in client.get("/static/js/main.js").text

