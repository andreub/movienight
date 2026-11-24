"""Completionist: TMDB data, the discovery sync (collections, franchise searches, series keywords), group building
(franchise merge, series keywords, dedupe), film states, ranking and hide/unhide. Synthetic data only."""
import json
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest

from movienight import completion, db, library, tmdb
from movienight.config import Config
from movienight.sync import SyncManager


def part(tid, year, va=7.0, vc=1000, title=None):
    return {"id": tid, "title": title or f"Film {tid}", "year": year, "release_date": f"{year}-06-01",
            "poster_path": f"/c{tid}.jpg", "vote_count": vc, "vote_average": va}


def coll(cid, name, parts):
    return {"id": cid, "name": name, "poster_path": None, "backdrop_path": None, "parts": parts}


def ws(tid, year, title, vc=10000, va=7.0):
    return part(tid, year, va, vc, title)


# seeded (conftest): seen 101, 202, 303, 404; unwatched library films 111, 112, 113, 115, 116, 505.
# 101 and 404 share a director, an actor and a keyword: an ordinary filmography, never a group by itself.
COLLECTIONS = [
    coll(900, "Alpha Collection", [part(101, 2015, 8), part(111, 2016, 8), part(9001, 2018, 8, 50),
                                   part(9002, 2020, 8, 90000)]),
    # a franchise across collections, like Spider-Man: Raimi, Amazing, MCU (the stem names each one and its films)
    coll(910, "Web-Slinger Collection", [ws(202, 2002, "Web-Slinger"), ws(9101, 2004, "Web-Slinger 2")]),
    coll(920, "The Amazing Web-Slinger Collection", [ws(112, 2012, "The Amazing Web-Slinger", 8000),
                                                     ws(9201, 2014, "The Amazing Web-Slinger 2", 7000)]),
    coll(925, "Web-Slinger (Shared Universe) Collection", [ws(9251, 2017, "Web-Slinger: Homecoming"),
                                                           ws(9252, 2019, "Web-Slinger: Far From Home")]),
    # not merged: a far less popular TV offshoot, a villain spin-off whose name lacks the stem, a generic stem
    coll(921, "Web-Slinger (TV) Collection", [ws(9211, 1977, "Web-Slinger", 90), ws(9212, 1978, "Web-Slinger Strikes Back", 40)]),
    coll(922, "Venomous Collection", [ws(9221, 2018, "Venomous"), ws(9222, 2021, "Venomous: Web-Slinger Cameo")]),
    coll(930, "Pint Collection", [part(404, 2012), part(113, 2010)]),             # inside the "pint trilogy" series
    coll(940, "Unstarted Collection", [part(115, 1975), part(9401, 1980)]),
    coll(950, "Single Collection", [part(101, 2015)]),
    coll(960, "Long Saga Collection", [part(303, 2001)] + [part(9600 + i, 2002 + i, 6, 100 + i) for i in range(29)]),
    coll(970, "Done Collection", [part(101, 2015), part(202, 2019)]),
]
KEYWORDS = {
    # a series keyword like "cornetto trilogy": three films plus a fan-made short (dropped by its vote count)
    5001: {"id": 5001, "name": "pint trilogy", "total": 4, "parts": [
        part(113, 2010, 6.0), part(404, 2012), part(9301, 2014, 7.2, 500, "Pint Three"), part(9302, 2026, 9, 2, "Pint Short")]},
    5002: {"id": 5002, "name": "theme saga", "total": 300, "parts": [part(101, 2015), part(9501, 2016)]},  # a theme
    5003: {"id": 5003, "name": "alpha duology", "total": 2, "parts": [part(101, 2015), part(111, 2016)]},  # in 900
}


@pytest.fixture
def cc(client):
    with db.session(client.cfg.db_path) as c:
        for x in COLLECTIONS:
            c.execute("INSERT INTO tmdb_collections(collection_id,json,fetched_at) VALUES(?,?,?)",
                      (x["id"], json.dumps(x), "2026-01-01T00:00:00Z"))
        c.execute("INSERT INTO tmdb_collections(collection_id,json,fetched_at) VALUES(999,'{}','2026-01-01T00:00:00Z')")
        for kid, k in KEYWORDS.items():
            c.execute("INSERT INTO tmdb_lookups(key,json,fetched_at) VALUES(?,?,?)",
                      (f"keyword:{kid}", json.dumps(k), "2026-01-01T00:00:00Z"))
    client.app.state.engine.invalidate()
    return client


# ---------------------------------------------------------------- TMDB data

def test_compact_details_collection_people_and_keyword_ids():
    raw = {"id": 1, "title": "T", "release_date": "2002-05-01",
           "belongs_to_collection": {"id": 556, "name": "Spider-Man Collection", "poster_path": "/x"},
           "keywords": {"keywords": [{"id": 357318, "name": "cornetto trilogy"}, {"id": 9748, "name": "revenge"}]},
           "credits": {"crew": [{"id": 7, "name": "Dir", "job": "Director"}, {"id": 8, "name": "W", "job": "Writer"}],
                       "cast": [{"id": 100 + i, "name": f"A{i}", "order": 9 - i} for i in range(10)]}}
    d = tmdb.compact_details(raw)
    assert d["collection"] == {"id": 556, "name": "Spider-Man Collection"}
    assert d["director_ids"] == [7]
    assert d["cast_ids"] == [109, 108, 107, 106, 105]        # top 5 by billing order
    assert d["keywords"] == ["cornetto trilogy", "revenge"] and d["keyword_ids"] == [357318, 9748]
    plain = tmdb.compact_details({"id": 2, "title": "N"})
    assert "collection" in plain and plain["collection"] is None
    assert plain["director_ids"] == [] and plain["cast_ids"] == [] and plain["keyword_ids"] == []


def test_compact_collection_sorts_and_drops_unreleased():
    raw = {"id": 5, "name": "Saga Collection", "parts": [
        {"id": 3, "title": "Three", "release_date": "2030-01-01"}, {"id": 2, "title": "Two", "release_date": "2010-01-01"},
        {"id": 4, "title": "Four", "release_date": ""}, {"id": 1, "title": "One", "release_date": "2005-01-01",
                                                          "vote_count": 10, "vote_average": 7.5}]}
    c = tmdb.compact_collection(raw, today="2026-11-22")
    assert [p["id"] for p in c["parts"]] == [1, 2]
    assert c["parts"][0] == {"id": 1, "title": "One", "year": 2005, "release_date": "2005-01-01", "poster_path": None,
                             "backdrop_path": None, "vote_count": 10, "vote_average": 7.5}


def test_migrations_12_13_tables(cfg):
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    db.migrate(cfg.db_path)
    with db.session(cfg.db_path) as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS) >= 13
        assert {r[1] for r in c.execute("PRAGMA table_info(tmdb_collections)")} == {"collection_id", "json", "fetched_at"}
        assert {r[1] for r in c.execute("PRAGMA table_info(completion_hidden)")} == {"group_key", "created_at"}
        assert {r[1] for r in c.execute("PRAGMA table_info(tmdb_lookups)")} == {"key", "json", "fetched_at"}


# ---------------------------------------------------------------- names and signals

@pytest.mark.parametrize("name, want", [
    ("Spider-Man Collection", "Spider-Man"), ("Spider-Man (MCU) Collection", "Spider-Man"),
    ("Spider-Man: Spider-Verse Collection", "Spider-Man"), ("The Amazing Spider-Man Collection", "Amazing Spider-Man"),
    ("The Dark Knight Collection", "Dark Knight"), ("Before... Collection", "Before"),
])
def test_stem(name, want):
    assert completion.stem(name) == want


def test_distinctive_stems():
    assert all(map(completion.distinctive, ["Spider-Man", "Godzilla", "Toy Story", "Dark Knight"]))
    assert not any(map(completion.distinctive, ["Before", "Matrix", "Alien", ""]))


def test_series_keywords():
    yes = ["cornetto trilogy", "three colours trilogy", "john ford's cavalry trilogy", "twilight saga", "dollars trilogy"]
    no = ["trilogy", "family saga", "space saga", "sequel", "revenge", "superhero", "vengeance", "based on comic",
          "trilogy of terror", "menstrual cycle", "duringcreditsstinger"]
    assert all(map(completion.is_series_keyword, yes))
    assert not any(map(completion.is_series_keyword, no))
    assert completion.series_name("cornetto trilogy") == "Cornetto Trilogy"


def test_franchise_merge_rejects_unrelated():
    colls = {c["id"]: c for c in COLLECTIONS}
    fr = completion.franchises(colls)
    assert fr == [{"name": "Web-Slinger", "collections": [910, 920, 925]}]      # not 921 (TV), 922 (spin-off)
    # a generic stem never merges ("Before" vs "The Land Before Time")
    before = {1: coll(1, "Before... Collection", [ws(1, 1995, "Before Sunrise"), ws(2, 2004, "Before Sunset")]),
              2: coll(2, "The Land Before Time Collection", [ws(3, 1988, "The Land Before Time"),
                                                             ws(4, 1994, "The Land Before Time II")])}
    assert completion.franchises(before) == []
    # a far less popular offshoot whose name and titles carry the stem stays out
    dk = {1: coll(1, "The Dark Knight Collection", [ws(1, 2005, "Batman Begins", 21000), ws(2, 2008, "The Dark Knight", 33000),
                                                    ws(3, 2012, "The Dark Knight Rises", 23000)]),
          2: coll(2, "Batman: The Dark Knight Returns Collection", [ws(4, 2012, "Batman: The Dark Knight Returns, Part 1", 2200),
                                                                    ws(5, 2013, "Batman: The Dark Knight Returns, Part 2", 2000)])}
    assert completion.franchises(dk) == []


# ---------------------------------------------------------------- discovery sync

def _cfg(tmp_path):
    cfg = Config(data_dir=tmp_path / "d", letterboxd_dir=tmp_path / "nolb", tmdb_token="eyJtest", sync_on_start=False)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    db.migrate(cfg.db_path)
    return cfg


def _runner(cfg, handler):
    calls: list[str] = []

    def record(request):
        calls.append(request.url.path)
        return handler(request)

    async def run():
        calls.clear()
        async with httpx.AsyncClient(transport=httpx.MockTransport(record)) as http:
            mgr = SyncManager(cfg, http, library.Engine(lambda: db.connect(cfg.db_path), None))
            mgr._stage("tmdb")
            with db.session(cfg.db_path) as c:
                await mgr._details(c)
                await mgr._collections(c)
        return sorted(calls)
    return run


async def test_discovery_sync_fetches_once_ttl_and_no_loop(tmp_path):
    cfg = _cfg(tmp_path)
    with db.session(cfg.db_path) as c:
        for tid in (1, 2, 3):
            c.execute("INSERT INTO lb_films(name,year,norm_title,tmdb_id,resolve_state,watched) VALUES(?,2000,?,?,1,1)",
                      (f"F{tid}", f"f{tid}", tid))
        c.execute("INSERT INTO tmdb_details(tmdb_id,json,fetched_at) VALUES(1,?,'x')", (json.dumps(
            {"id": 1, "release_date": "2000-01-01", "collection": {"id": 77, "name": "Web-Slinger Collection"},
             "keywords": ["pint trilogy", "trilogy", "revenge"], "keyword_ids": [5001, 384197, 9748]}),))
        c.execute("INSERT INTO tmdb_details(tmdb_id,json,fetched_at) VALUES(2,?,'x')",       # no saga, no series
                  (json.dumps({"id": 2, "release_date": "2000-01-01", "collection": None, "keyword_ids": []}),))
        c.execute("INSERT INTO tmdb_details(tmdb_id,json,fetched_at) VALUES(3,?,'x')",       # predates keyword_ids
                  (json.dumps({"id": 3, "release_date": "2000-01-01", "collection": None}),))

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/3/search/collection":
            assert request.url.params["query"] == "Web-Slinger"
            return httpx.Response(200, json={"results": [{"id": 79, "name": "The Amazing Web-Slinger Collection"},
                                                         {"id": 80, "name": "Unrelated Collection"}]})
        if path == "/3/discover/movie":
            assert request.url.params["with_keywords"] == "5001"
            return httpx.Response(200, json={"total_results": 2, "results": [
                {"id": 1, "title": "A", "release_date": "2000-01-01", "vote_count": 50},
                {"id": 9, "title": "Z", "release_date": "2004-01-01", "vote_count": 60}]})
        if path.startswith("/3/collection/"):
            cid = int(path.rsplit("/", 1)[1])
            if cid == 78:
                return httpx.Response(404)
            return httpx.Response(200, json={"id": cid, "name": f"C{cid}", "parts": [
                {"id": 1, "title": "A", "release_date": "2000-01-01"}, {"id": 9, "title": "Z", "release_date": "2004-01-01"}]})
        tid = int(path.split("/")[3])
        body = {"id": tid, "title": f"T{tid}", "release_date": "2000-01-01"}
        if tid == 3:
            body["belongs_to_collection"] = {"id": 78, "name": "Gone"}        # a stem too short to search
        return httpx.Response(200, json=body)

    run = _runner(cfg, handler)
    # details: only 3 (backfill); search: the distinctive stem; collections: 77 (seen), 79 (search hit whose
    # name carries the stem; not 80), 78 (backfilled film 3, a 404); discover: the series keyword only
    assert await run() == ["/3/collection/77", "/3/collection/78", "/3/collection/79", "/3/discover/movie",
                           "/3/movie/3", "/3/search/collection"]
    with db.session(cfg.db_path) as c:
        rows = {r[0]: json.loads(r[1]) for r in c.execute("SELECT collection_id, json FROM tmdb_collections")}
        lookups = {r[0]: json.loads(r[1]) for r in c.execute("SELECT key, json FROM tmdb_lookups")}
    assert rows[78] == {} and [p["id"] for p in rows[77]["parts"]] == [1, 9]
    assert set(lookups) == {"collsearch:web-slinger", "keyword:5001"}
    assert lookups["keyword:5001"]["name"] == "pint trilogy" and lookups["keyword:5001"]["total"] == 2
    assert [p["id"] for p in lookups["keyword:5001"]["parts"]] == [1, 9]
    assert await run() == []                      # nothing pending: no refetch loop
    old = (datetime.now(timezone.utc) - timedelta(days=31)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with db.session(cfg.db_path) as c:
        c.execute("UPDATE tmdb_collections SET fetched_at=? WHERE collection_id IN (77, 78)", (old,))
        c.execute("UPDATE tmdb_lookups SET fetched_at=? WHERE key='keyword:5001'", (old,))
    assert await run() == ["/3/collection/77", "/3/collection/78", "/3/discover/movie"]     # past the TTL


async def test_no_lookups_for_an_ordinary_filmography(tmp_path):
    """A director's films with recurring cast and themes, none in a collection: no search, no discover, no group."""
    cfg = _cfg(tmp_path)
    films = [(11, ["nonlinear timeline", "magician", "trilogy"]), (12, ["dream", "heist", "family saga"]),
             (13, ["space travel", "black hole", "sequel"]), (14, ["world war ii", "survival"]),
             (15, ["time inversion", "espionage"])]
    with db.session(cfg.db_path) as c:
        for tid, kws in films:
            c.execute("INSERT INTO lb_films(name,year,norm_title,tmdb_id,resolve_state,watched) VALUES(?,2010,?,?,1,1)",
                      (f"N{tid}", f"n{tid}", tid))
            c.execute("INSERT INTO tmdb_details(tmdb_id,json,fetched_at) VALUES(?,?,'x')", (tid, json.dumps(
                {"id": tid, "release_date": "2010-01-01", "collection": None, "director_ids": [525],
                 "cast_ids": [3894, 1892, 2037], "keywords": kws, "keyword_ids": [tid * 10 + i for i in range(len(kws))]})))

    def handler(request):
        raise AssertionError(f"unexpected TMDB call {request.url.path}")

    assert await _runner(cfg, handler)() == []
    with db.session(cfg.db_path) as c:
        assert completion._raw_groups(c, {11, 12, 13, 14, 15}) == []


# ---------------------------------------------------------------- groups

def test_groups_discovered_dedupe_and_ranking(cc, monkeypatch):
    monkeypatch.setattr(completion, "_today", lambda: date(2026, 11, 22))
    r = cc.get("/api/completions").json()
    keys = [g["key"] for g in r["groups"]]
    # score: the long saga has unseen films from 2025-26 (recency wins); Alpha's seen film is rated 4.5 against a
    # 3.5 mean; the series' 4.0; the franchise's seen film is rated 2.0, so it drops below the rest
    assert keys == ["collection-960", "collection-900", "series-5001", "franchise-web-slinger"]
    by = {g["key"]: g for g in r["groups"]}
    assert [by[k]["reason"] for k in keys] == ["New: Film 9624 · 2026", "You rated ★4.5",
                                               "In your library", "In your library"]
    assert [by[k]["reason_film"] for k in keys] == [9624, None, None, None]     # only a New/Recent reason names a film
    assert by["collection-960"]["ordered"] and by["collection-960"]["films"] == sorted(
        by["collection-960"]["films"], key=lambda f: f["release_date"] or "")   # sagas keep their release order
    assert by["collection-960"]["score"] > by["collection-900"]["score"] > by["series-5001"]["score"] > \
        by["franchise-web-slinger"]["score"]
    assert [g["key"] for g in r["completed"]] == ["collection-970"]
    gone = {"collection-910", "collection-920", "collection-925",        # absorbed by the franchise
            "collection-921", "collection-922",                          # not started (and never merged)
            "collection-930",                                            # inside the series keyword
            "series-5002", "series-5003",                                # a theme; inside collection 900
            "collection-940", "collection-950", "collection-999"}        # not started, too small, 404
    assert not gone & set(keys + [g["key"] for g in r["completed"]])
    assert r["counts"] == {"collection": 3, "franchise": 1, "series": 1, "director": 0, "actor": 0}
    assert r["hidden"] == [] and r["hidden_count"] == 0

    fr = next(g for g in r["groups"] if g["key"] == "franchise-web-slinger")
    assert (fr["name"], fr["seen"], fr["total"], fr["available"], fr["missing"]) == ("Web-Slinger", 1, 6, 1, 4)
    assert [f["year"] for f in fr["films"]] == [2002, 2004, 2012, 2014, 2017, 2019]     # release order
    assert "Venomous" not in {f["title"] for f in fr["films"]}

    pint = next(g for g in r["groups"] if g["key"] == "series-5001")
    assert pint["name"] == "Pint Trilogy" and pint["ordered"] is True
    assert [(f["tmdb_id"], f["state"]) for f in pint["films"]] == [(113, "available"), (404, "seen"), (9301, "missing")]


def test_films_whole_group_in_order_with_states(cc):
    g = next(g for g in cc.get("/api/completions").json()["groups"] if g["key"] == "collection-900")
    assert g["name"] == "Alpha"                                   # " Collection" dropped
    assert [(f["tmdb_id"], f["state"]) for f in g["films"]] == [
        (101, "seen"), (111, "available"), (9001, "missing"), (9002, "missing")]
    assert g["films"][1]["movie_id"] and "movie_id" not in g["films"][2]
    assert g["next"]["title"] == "Film 111" and g["next"]["movie_id"] == g["films"][1]["movie_id"]
    assert "films_preview" not in g and "more" not in g
    # unordered groups (later phases) rank unseen films: library first, then missing by votes x rating
    assert [f["tmdb_id"] for f in completion.ranked_unseen(g["films"])] == [111, 9002, 9001]


def test_long_saga_returns_every_film(cc):
    g = next(g for g in cc.get("/api/completions").json()["groups"] if g["key"] == "collection-960")
    assert len(g["films"]) == 30 and g["available"] == 0 and g["next"] is None
    full = cc.get("/api/completions/collection-960").json()
    assert full["films"] == g["films"] and full["films"][0]["tmdb_id"] == 303 and full["films"][0]["state"] == "seen"
    assert [f["year"] for f in full["films"]] == sorted(f["year"] for f in full["films"])
    assert cc.get("/api/completions/collection-12345").status_code == 404


def test_kind_filter(cc):
    r = cc.get("/api/completions", params={"kind": "series"}).json()
    assert [g["key"] for g in r["groups"]] == ["series-5001"] and r["completed"] == []
    assert cc.get("/api/completions", params={"kind": "curated"}).status_code == 422


def test_hide_and_unhide(cc):
    assert cc.post("/api/completions/collection-900/hide").json() == {"ok": True}
    r = cc.get("/api/completions").json()
    assert "collection-900" not in [g["key"] for g in r["groups"]] and r["hidden_count"] == 1
    assert r["counts"]["collection"] == 2
    h = cc.get("/api/completions", params={"hidden": "true"}).json()["hidden"]
    assert [g["key"] for g in h] == ["collection-900"] and h[0]["hidden"] is True
    assert cc.get("/api/completions/collection-900").json()["hidden"] is True
    assert cc.delete("/api/completions/collection-900/hide").json() == {"ok": True}
    assert "collection-900" in [g["key"] for g in cc.get("/api/completions").json()["groups"]]
    assert cc.post("/api/completions/collection-12345/hide").status_code == 404


def test_seen_mark_moves_a_film_to_seen(cc):
    g = cc.get("/api/completions/collection-900").json()
    mid = next(f["movie_id"] for f in g["films"] if f["tmdb_id"] == 111)
    assert cc.post(f"/api/movies/{mid}/seen", json={"seen": True}).status_code == 200
    g = cc.get("/api/completions/collection-900").json()
    assert next(f["state"] for f in g["films"] if f["tmdb_id"] == 111) == "seen" and g["seen"] == 2


def test_no_groups_for_a_filmography_without_sagas(client):
    """Seeded films share a director, an actor and keywords, but no collection or series keyword: no groups."""
    r = client.get("/api/completions").json()
    assert [g["key"] for g in r["groups"] + r["completed"]] == []


# ---------------------------------------------------------------- ranking

def _g(films, available=0):
    return {"films": films, "seen": sum(f["state"] == "seen" for f in films), "total": len(films),
            "available": available}


def _f(tid, state, when):
    return {"tmdb_id": tid, "title": f"F{tid}", "state": state, "release_date": when, "year": int(when[:4])}


TODAY = date(2026, 11, 22)


def test_recency_full_then_fading():
    def rec(when, state="missing"):
        return completion.recency([_f(1, state, when)], TODAY)[0]
    assert rec("2026-03-01") == rec("2025-06-01") == 1.0            # within ~18 months
    assert 0 < rec("2022-06-01") < rec("2023-06-01") < 1             # fading
    assert rec("2021-06-01") == 0.0                                  # past ~5 years
    assert rec("2026-03-01", "seen") == 0.0                          # only unseen films count
    assert rec("2027-01-01") == 0.0                                  # unreleased


def test_taste_shrinks_few_ratings_towards_the_mean():
    mu = 3.5
    assert completion.taste([], mu) == 0.0
    assert completion.taste([3.5, 3.5, 3.5], mu) == 0.0
    one_five, three = completion.taste([5.0], mu), completion.taste([4.6, 4.6, 4.6], mu)
    assert 0 < one_five < three                                      # one 5-star film says less than three at 4.6
    assert completion.taste([5.0] * 50, mu) == 1.0                   # capped
    assert completion.taste([1.0], mu) < 0                           # disliked


def test_score_ordering_and_reasons():
    mu = 3.5
    ratings = {1: (4.5, True), 2: (4.5, True), 3: (2.0, True), 4: (4.0, False)}
    groups = {
        "new": _g([_f(3, "seen", "2015-01-01"), _f(10, "missing", "2026-05-01")]),          # disliked, but new
        "loved": _g([_f(1, "seen", "2010-01-01"), _f(2, "seen", "2012-01-01"), _f(11, "missing", "2014-01-01")]),
        "library": _g([_f(5, "seen", "2010-01-01"), _f(12, "available", "2012-01-01")], available=1),
        "voted": _g([_f(4, "seen", "2010-01-01"), _f(13, "missing", "2012-01-01")]),
        "plain": _g([_f(6, "seen", "2010-01-01"), _f(14, "missing", "2012-01-01"), _f(15, "missing", "2013-01-01")]),
    }
    scored = {k: completion.score(g, ratings, mu, TODAY) for k, g in groups.items()}
    assert sorted(scored, key=lambda k: -scored[k][0]) == ["new", "loved", "library", "voted", "plain"]
    assert scored["new"][1] == "New: F10 · 2026"
    assert scored["loved"][1] == "You rated ★4.5"
    assert scored["library"][1] == "In your library"
    assert scored["plain"][1] is None
    # an app vote stands in for a missing rating, but never shows as stars
    votes_only = _g([_f(4, "seen", "2010-01-01"), _f(7, "seen", "2011-01-01"), _f(16, "missing", "2012-01-01")])
    assert completion.score(votes_only, {4: (5.0, False), 7: (5.0, False)}, mu, TODAY)[1] == "You liked these"


def test_user_ratings_prefers_letterboxd_over_votes(client):
    st = client.app.state.engine.get()
    ratings, mu = completion.user_ratings(st)
    assert ratings[101] == (4.5, True) and ratings[202] == (2.0, True)
    assert mu == pytest.approx(3.5)
    c = next(c for c in st.cands if c.tmdb_id == 116)
    c.vote = 2
    assert completion.user_ratings(st)[0][116] == (4.5, False)


def test_cards_carry_a_hero_backdrop(cc):
    g = next(g for g in cc.get("/api/completions").json()["groups"] if g["key"] == "collection-900")
    # parts carry no backdrop here: the next film (111, in the library) falls back to its cached TMDB details
    assert g["backdrop_url"].endswith("/b111.jpg")
    assert g["films"][1]["backdrop_url"].endswith("/b111.jpg") and g["films"][2]["backdrop_url"] is None


async def test_collection_parts_without_backdrop_refetched_once(tmp_path):
    cfg = _cfg(tmp_path)
    fresh = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with db.session(cfg.db_path) as c:
        c.execute("INSERT INTO lb_films(name,year,norm_title,tmdb_id,resolve_state,watched) VALUES('F1',2000,'f1',1,1,1)")
        c.execute("INSERT INTO tmdb_details(tmdb_id,json,fetched_at) VALUES(1,?,'x')", (json.dumps(
            {"id": 1, "release_date": "2000-01-01", "collection": {"id": 77, "name": "Gone Collection"},
             "keyword_ids": []}),))
        c.execute("INSERT INTO tmdb_collections(collection_id,json,fetched_at) VALUES(77,?,?)", (json.dumps(
            {"id": 77, "name": "C", "parts": [{"id": 1, "title": "A", "release_date": "2000-01-01"}]}), fresh))

    def handler(request):
        assert request.url.path == "/3/collection/77"
        return httpx.Response(200, json={"id": 77, "name": "C", "parts": [
            {"id": 1, "title": "A", "release_date": "2000-01-01", "backdrop_path": None}]})

    run = _runner(cfg, handler)
    assert await run() == ["/3/collection/77"]           # stored before parts carried backdrop_path
    assert await run() == []                             # a null backdrop is stored: no refetch loop
