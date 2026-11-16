import json

import httpx
import pytest

from conftest import FIX
from movienight import db, library, plex, tmdb


def test_parse_items_and_guids():
    payload = json.loads((FIX / "plex_all.json").read_text())
    items = [plex.parse_item(m) for m in payload["MediaContainer"]["Metadata"]]
    a = items[0]
    assert (a["tmdb_id"], a["imdb_id"], a["view_count"], a["duration_min"]) == (101, "tt1", 2, 100)
    assert a["genres"] == ["Crime", "Thriller"] and a["directors"] == ["Dir One"] and a["resolution"] == "1080"
    assert a["added_at"].startswith("2023-11-14")
    assert items[1]["imdb_id"] == "tt9" and items[1]["tmdb_id"] is None
    assert items[2]["resolution"] == "sd" and items[2]["thumb"] is None


def test_parse_resources_and_connection_order():
    servers = plex.parse_resources(json.loads((FIX / "plex_resources.json").read_text()))
    assert [s["id"] for s in servers] == ["mach1", "mach2"]
    assert [c["uri"] for c in plex.connection_order(servers[0]["connections"])][-1].startswith("https://relay")
    assert plex.connection_order(servers[0]["connections"])[0]["local"]


async def test_pick_connection_prefers_non_relay_and_skips_dead():
    servers = plex.parse_resources(json.loads((FIX / "plex_resources.json").read_text()))

    def handler(req):
        if req.url.host in ("relay.example", "remote.example"):
            return httpx.Response(200, json={})
        raise httpx.ConnectTimeout("dead")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        assert await plex.pick_connection(c, servers[0], "cid") == "https://remote.example:32400"
        assert await plex.pick_connection(c, servers[1], "cid") is None


def test_dedupe_by_tmdb_imdb_title(tmp_path):
    p = tmp_path / "d.db"
    db.migrate(p)
    with db.session(p) as c:
        for sid in ("a", "b"):
            c.execute("INSERT INTO media_servers(id,name,owned) VALUES(?,?,1)", (sid, sid))
        base = {"genres": [], "title": "", "year": 2000}
        mk = lambda key, title, tm, im: {**plex.parse_item({"ratingKey": key, "title": title, "year": 2000}),
                                          "tmdb_id": tm, "imdb_id": im}
        library.store_server_items(c, "a", [mk(1, "Same", 5, "tt5"), mk(2, "Imdb Only", None, "tt7"), mk(3, "Plain Title", None, None)])
        library.store_server_items(c, "b", [mk(9, "Same!", 5, None), mk(8, "Imdb Only", None, "tt7"),
                                            mk(7, "Plain: Title", None, None), mk(6, "Imdb Fills", None, "tt5")])
        n = library.assign_movies(c)
        assert n == 3
        counts = [r[0] for r in c.execute("SELECT COUNT(*) FROM media_items GROUP BY movie_id ORDER BY 1")]
        assert sorted(counts) == [2, 2, 3]
        same = c.execute("SELECT DISTINCT movie_id FROM media_items WHERE tmdb_id=5 OR imdb_id='tt5'").fetchall()
        assert len(same) == 1


def test_tmdb_compact_and_search_pick():
    raw = {"id": 1, "title": "T", "release_date": "2011-09-16", "runtime": 100, "genres": [{"name": "Crime"}],
           "credits": {"cast": [{"name": "B", "order": 1}, {"name": "A", "order": 0}],
                       "crew": [{"name": "D", "job": "Director"}, {"name": "X", "job": "Editor"}]},
           "keywords": {"keywords": [{"name": "k"}]}, "production_countries": [{"iso_3166_1": "US"}],
           "original_language": "en", "vote_average": 7.6, "vote_count": 10}
    d = tmdb.compact_details(raw)
    assert d["year"] == 2011 and d["cast"] == ["A", "B"] and d["directors"] == ["D"] and d["keywords"] == ["k"]
    assert tmdb.pick_search_result({"results": [{"id": 1, "title": "Other"}, {"id": 2, "title": "Drive!"}]}, "drive") == 2


async def test_tmdb_auth_modes_and_429_retry():
    seen = []

    def handler(req):
        seen.append(req)
        if len(seen) == 1:
            return httpx.Response(429, headers={"Retry-After": "1"})
        return httpx.Response(200, json={"results": [{"id": 7, "title": "Drive"}]})

    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        t = tmdb.Tmdb(c, "eyJabc", sleep=fake_sleep)
        assert await t.search("Drive", 2011) == 7
        assert sleeps == [1.0]
        assert seen[-1].headers["authorization"] == "Bearer eyJabc" and "api_key" not in str(seen[-1].url)
        t3 = tmdb.Tmdb(c, "abc123", sleep=fake_sleep)
        await t3.get("/x")
        assert seen[-1].url.params["api_key"] == "abc123" and "authorization" not in seen[-1].headers


async def test_tmdb_rejects_bad_credentials():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(401))) as c:
        with pytest.raises(tmdb.TmdbError):
            await tmdb.Tmdb(c, "bad").get("/x")
