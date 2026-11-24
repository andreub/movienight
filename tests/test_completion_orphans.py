"""Completionist orphans: films in no TMDB collection that join the started saga whose name they carry (title or
keyword). Synthetic sagas and titles only: the rule is derived from data, never from names in the code."""
import json
from dataclasses import replace
from datetime import date
from pathlib import Path

import httpx
import pytest

from movienight import completion, db, library
from movienight.sync import SyncManager

from test_completion import _cfg, _runner

TODAY = date(2026, 11, 22)


def film(tid, year, title, vc=10000, genres=(28,), via="title", coll=None, runtime=120):
    return {"id": tid, "title": title, "year": year, "release_date": f"{year}-05-01", "poster_path": f"/o{tid}.jpg",
            "backdrop_path": None, "vote_count": vc, "vote_average": 7.0, "genre_ids": list(genres), "via": via,
            "collection_id": coll, "runtime": runtime}


def part(tid, year, title, vc=10000, genres=(28,)):
    return {"id": tid, "title": title, "year": year, "release_date": f"{year}-05-01", "poster_path": None,
            "backdrop_path": None, "vote_count": vc, "vote_average": 7.0, "genre_ids": list(genres)}


SKY = {"id": 500, "name": "Sky Raiders Collection", "parts": [
    part(1, 1977, "Sky Raiders"), part(2, 1980, "Sky Raiders: The Return", 8000), part(3, 2019, "Sky Raiders: Rise", 12000)]}
PALS = {"id": 600, "name": "Paper Pals Collection", "parts": [
    part(11, 1995, "Paper Pals", 9000, (16, 10751)), part(12, 1999, "Paper Pals 2", 7000, (16, 10751))]}
SKY_ORPHANS = [
    film(101, 2016, "Lone Pilot: A Sky Raiders Story", 9000),            # joins between 1980 and 2019
    film(102, 2018, "Hornet", 6000, via="keyword"),                     # keyword only, popular enough: joins
    film(103, 2008, "Sky Raiders: The Clone Squadron", 2300, (16, 878)),   # animated spin-off of a live-action saga
    film(104, 1978, "The Sky Raiders Holiday Special", 500, (10770,)),  # TV film (and the name is not at the start)
    film(105, 1979, "Sky Raiders: Holiday Special", 9000, (10770, 35)),  # TV film
    film(106, 2020, "Sky Raiders: Fan Edit", 900),                      # too few votes (median 10000)
    film(107, 2009, "Sky Raiders Origins: Wing", 9000, coll=77),        # in another collection
    film(108, 2021, "Sky Raiders: Making Of", 9000, (99,)),             # documentary
    film(109, 2022, "Sky Raiders: Short Hop", 9000, runtime=22),        # a short
    film(110, 2001, "Sky Raiders", 9000),                               # a same-name remake: another take
    film(111, 2017, "Sting", 3000, via="keyword"),                      # keyword only, below its higher bar
    film(112, 2030, "Sky Raiders: Future", 9000),                       # unreleased
]
PALS_ORPHANS = [film(201, 2003, "Paper Pals: Snow Day", 3000, (16, 10751), runtime=75),   # an animated saga's own
                film(202, 2004, "Paper Pals: Tiny Tale", 3000, (16,), runtime=8)]          # a short


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(completion, "_today", lambda: TODAY)
    cfg = _cfg(tmp_path)
    with db.session(cfg.db_path) as c:
        for x in (SKY, PALS):
            c.execute("INSERT INTO tmdb_collections(collection_id,json,fetched_at) VALUES(?,?,'2026-11-01T00:00:00Z')",
                      (x["id"], json.dumps(x)))
        for phrase, films in (("Sky Raiders", SKY_ORPHANS), ("Paper Pals", PALS_ORPHANS)):
            c.execute("INSERT INTO tmdb_lookups(key,json,fetched_at) VALUES(?,?,'2026-11-01T00:00:00Z')",
                      (completion.orphan_key(phrase), json.dumps({"phrase": phrase, "films": films})))
        yield c


def test_names_saga():
    yes = ["Sky Raiders: The Return", "Lone Pilot: A Sky Raiders Story", "Sky Raiders 2", "SKY-RAIDERS: Rise",
           "Sky Raiders Origins: Wing"]
    no = ["Sky Raiders", "sky raiders!", "The Sky Raiders Holiday Special", "Return of the Sky Raiders",
          "Sky Raidersmith", "Blue Sky: Raiders", ""]
    assert all(completion.names_saga(t, "Sky Raiders") for t in yes)
    assert not any(completion.names_saga(t, "Sky Raiders") for t in no)
    assert completion.names_saga("Mission: Unlikely - Fallout", "Mission: Unlikely")      # a colon in the name


def test_saga_phrase():
    assert completion.saga_phrase("Sky Raiders Collection") == "Sky Raiders"
    assert completion.saga_phrase("Web-Slinger (Shared Universe) Collection") == "Web-Slinger"
    assert completion.saga_phrase("Web-Slinger") == "Web-Slinger"                           # a franchise's stem
    assert completion.saga_phrase("Alien Collection") is None                               # too generic to search


def test_orphans_join_in_release_order_and_filters_reject(conn):
    groups = {g["key"]: g for g in completion._raw_groups(conn, {1, 11})}
    sky = groups["collection-500"]
    assert [(f["tmdb_id"], f["year"]) for f in sky["films"]] == [
        (1, 1977), (2, 1980), (101, 2016), (102, 2018), (3, 2019)]
    assert sky["films"][2]["genre_ids"] == [28] and sky["films"][2]["poster_path"] == "/o101.jpg"


def test_animated_saga_takes_animated_orphans(conn):
    pals = next(g for g in completion._raw_groups(conn, {11}) if g["key"] == "collection-600")
    assert [f["tmdb_id"] for f in pals["films"]] == [11, 12, 201]        # not the short


def test_unstarted_saga_takes_nothing(conn):
    assert [g["key"] for g in completion._raw_groups(conn, {11})] == ["collection-600"]
    assert completion.wanted_orphans(conn, {11}) == {
        "Paper Pals": [{"ids": {11, 12}, "median": 8000, "animated": True}]}


def test_orphans_join_a_franchise(tmp_path, monkeypatch):
    monkeypatch.setattr(completion, "_today", lambda: TODAY)
    ws = [{"id": 910, "name": "Web-Slinger Collection", "parts": [part(1, 2002, "Web-Slinger"),
                                                                  part(2, 2004, "Web-Slinger 2")]},
          {"id": 920, "name": "The Amazing Web-Slinger Collection", "parts": [
              part(3, 2012, "The Amazing Web-Slinger", 8000), part(4, 2014, "The Amazing Web-Slinger 2", 7000)]}]
    cfg = _cfg(tmp_path)
    with db.session(cfg.db_path) as c:
        for x in ws:
            c.execute("INSERT INTO tmdb_collections(collection_id,json,fetched_at) VALUES(?,?,'2026-11-01T00:00:00Z')",
                      (x["id"], json.dumps(x)))
        c.execute("INSERT INTO tmdb_lookups(key,json,fetched_at) VALUES(?,?,'2026-11-01T00:00:00Z')",
                  (completion.orphan_key("Web-Slinger"),
                   json.dumps({"films": [film(5, 2018, "Web-Slinger: Night Shift", 9000),
                                         film(6, 2019, "Web-Slinger: Into the Webs", 9000, (16,))]})))
        fr = completion._raw_groups(c, {1})
    assert [(g["key"], [f["tmdb_id"] for f in g["films"]]) for g in fr] == [("franchise-web-slinger", [1, 2, 3, 4, 5])]


def test_no_hardcoded_names():
    src = "".join((Path(completion.__file__).parent / f).read_text().lower() for f in ("completion.py", "sync.py"))
    assert not any(name in src for name in ("star wars", "rogue one", "transformers", "bumblebee"))


# ---------------------------------------------------------------- sync

def _seed_sync(cfg):
    with db.session(cfg.db_path) as c:
        c.execute("INSERT INTO lb_films(name,year,norm_title,tmdb_id,resolve_state,watched) VALUES('S',1977,'s',1,1,1)")
        c.execute("INSERT INTO tmdb_details(tmdb_id,json,fetched_at) VALUES(1,?,'x')", (json.dumps(
            {"id": 1, "release_date": "1977-05-01", "collection": {"id": 500, "name": "Sky Raiders Collection"},
             "keyword_ids": [], "runtime": 121}),))
        # a library film already detailed: no /movie call for it
        c.execute("INSERT INTO tmdb_details(tmdb_id,json,fetched_at) VALUES(102,?,'x')", (json.dumps(
            {"id": 102, "release_date": "2018-05-01", "collection": None, "keyword_ids": [], "runtime": 114}),))


def _hit(tid, year, title, vc, genres=(28,)):
    return {"id": tid, "title": title, "release_date": f"{year}-05-01", "vote_count": vc, "genre_ids": list(genres)}


def _sync_handler(request):
    path, q = request.url.path, request.url.params
    if path == "/3/search/collection":
        return httpx.Response(200, json={"results": []})
    if path == "/3/collection/500":
        return httpx.Response(200, json={**SKY, "parts": [{**p, "genre_ids": p["genre_ids"]} for p in SKY["parts"]]})
    if path == "/3/search/keyword":
        assert q["query"] == "Sky Raiders"
        return httpx.Response(200, json={"results": [{"id": 43, "name": "sky raiders fan film"},
                                                     {"id": 42, "name": "Sky-Raiders"}]})
    if path == "/3/discover/movie":
        assert q["with_keywords"] == "42"
        return httpx.Response(200, json={"total_results": 3, "results": [
            _hit(1, 1977, "Sky Raiders", 10000), _hit(102, 2018, "Hornet", 6000), _hit(111, 2017, "Sting", 3000)]})
    if path == "/3/search/movie":
        assert q["query"] == "Sky Raiders"
        return httpx.Response(200, json={"results": [
            _hit(1, 1977, "Sky Raiders", 10000), _hit(101, 2016, "Lone Pilot: A Sky Raiders Story", 9000),
            _hit(103, 2008, "Sky Raiders: The Clone Squadron", 2300, (16,)),
            _hit(107, 2009, "Sky Raiders Origins: Wing", 9000), _hit(110, 2001, "Sky Raiders", 9000),
            _hit(120, 1990, "Blue Sky: Raiders of Nowhere", 9000), _hit(112, 2099, "Sky Raiders: Future", 9000)]})
    if path.startswith("/3/movie/"):
        tid = int(path.split("/")[3])
        body = {"id": tid, "title": f"T{tid}", "release_date": "2010-01-01", "runtime": 130}
        if tid == 107:
            body["belongs_to_collection"] = {"id": 77, "name": "Sky Raiders Origins Collection"}
        return httpx.Response(200, json=body)
    raise AssertionError(f"unexpected TMDB call {path}")


async def test_sync_finds_orphans_once_with_bounded_calls(tmp_path, monkeypatch):
    monkeypatch.setattr(completion, "_today", lambda: TODAY)
    cfg = _cfg(tmp_path)
    _seed_sync(cfg)
    run = _runner(cfg, _sync_handler)
    # details only for the candidates that can still join (101 by title, 107 to learn its collection; 102 is cached);
    # not the animated spin-off, the remake, the unreleased film, the weak keyword match or the unrelated title
    assert await run() == ["/3/collection/500", "/3/discover/movie", "/3/movie/101", "/3/movie/107",
                           "/3/search/collection", "/3/search/keyword", "/3/search/movie"]
    with db.session(cfg.db_path) as c:
        found = json.loads(c.execute("SELECT json FROM tmdb_lookups WHERE key=?",
                                     (completion.orphan_key("Sky Raiders"),)).fetchone()[0])
        assert found["keyword_id"] == 42
        assert {f["id"]: (f["via"], f["collection_id"], f["runtime"]) for f in found["films"]} == {
            101: ("title", None, 130), 102: ("keyword", None, 114), 107: ("title", 77, 130)}
        sky = completion._raw_groups(c, {1})[0]
    assert [f["tmdb_id"] for f in sky["films"]] == [1, 2, 101, 102, 3]
    assert await run() == []                        # cached for its TTL: no refetch loop


async def test_theme_keyword_is_ignored(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _seed_sync(cfg)

    def handler(request):
        if request.url.path == "/3/discover/movie":
            return httpx.Response(200, json={"total_results": 400, "results": [_hit(102, 2018, "Hornet", 60000)]})
        return _sync_handler(request)

    await _runner(cfg, handler)()
    with db.session(cfg.db_path) as c:
        found = json.loads(c.execute("SELECT json FROM tmdb_lookups WHERE key LIKE 'orphans:%'").fetchone()[0])
    assert [f["id"] for f in found["films"]] == [101, 107]


async def test_flag_off_no_orphan_lookups(tmp_path):
    cfg = replace(_cfg(tmp_path), completionist=False)
    _seed_sync(cfg)
    with db.session(cfg.db_path) as c:
        c.execute("INSERT INTO tmdb_collections(collection_id,json,fetched_at) VALUES(500,?,'2099-01-01T00:00:00Z')",
                  (json.dumps(SKY),))

    def handler(request):
        if request.url.path.startswith(("/3/search/", "/3/discover/", "/3/collection/")):
            raise AssertionError(f"unexpected TMDB call {request.url.path}")
        return httpx.Response(200, json={"results": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        mgr = SyncManager(cfg, http, library.Engine(lambda: db.connect(cfg.db_path), None))
        await mgr.stage_tmdb()
    with db.session(cfg.db_path) as c:
        assert c.execute("SELECT COUNT(*) FROM tmdb_lookups").fetchone()[0] == 0
