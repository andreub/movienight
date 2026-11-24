"""Completionist filmographies (directors and actors): TMDB person credits, the credit filters, eligibility, the
person-credits sync (cap, call count, TTL, flag), dedupe, the actor cap, film order and "All" ordering. Synthetic
data only."""
import json
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest

from movienight import completion, db, library, tmdb
from movienight.config import Config
from movienight.sync import SyncManager

TODAY = "2026-11-22"


def credit(tid, year=2010, vc=1000, va=7.0, genres=(18,), date_=None, **kw):
    when = date_ if date_ is not None else f"{year}-05-01"
    return {"id": tid, "title": kw.get("title", f"P{tid}"), "release_date": when, "poster_path": f"/p{tid}.jpg",
            "backdrop_path": f"/b{tid}.jpg", "vote_count": vc, "vote_average": va, "genre_ids": list(genres)}


def person(pid, name, directed=(), acted=()):
    return {"id": pid, "name": name, "profile_path": f"/face{pid}.jpg", "directed": list(directed), "acted": list(acted)}


def store_person(c, p, fetched="2026-11-01T00:00:00Z"):
    c.execute("INSERT OR REPLACE INTO tmdb_person_credits(person_id,name,profile_path,json,fetched_at) VALUES(?,?,?,?,?)",
              (p["id"], p["name"], p["profile_path"], json.dumps(p), fetched))


def _cfg(tmp_path, **kw):
    cfg = Config(data_dir=tmp_path / "d", letterboxd_dir=tmp_path / "nolb", tmdb_token="eyJtest", sync_on_start=False, **kw)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    db.migrate(cfg.db_path)
    return cfg


def seen_film(c, tid, directors=(), cast=()):
    """A film watched on Letterboxd, with details naming its director(s) and top-billed cast."""
    c.execute("INSERT INTO lb_films(name,year,norm_title,tmdb_id,resolve_state,watched) VALUES(?,2010,?,?,1,1)",
              (f"F{tid}", f"f{tid}", tid))
    c.execute("INSERT INTO tmdb_details(tmdb_id,json,fetched_at) VALUES(?,?,'x')", (tid, json.dumps(
        {"id": tid, "release_date": "2010-01-01", "collection": None, "keyword_ids": [],
         "director_ids": list(directors), "cast_ids": list(cast)})))


# ---------------------------------------------------------------- TMDB data

def test_migration_14_person_credits_table(cfg):
    db.migrate(cfg.db_path)
    with db.session(cfg.db_path) as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS) >= 14
        assert {r[1] for r in c.execute("PRAGMA table_info(tmdb_person_credits)")} == {
            "person_id", "name", "profile_path", "json", "fetched_at"}


def test_compact_person_directed_and_top_billed():
    raw = {"id": 525, "name": "Some Director", "profile_path": "/x.jpg", "movie_credits": {
        "crew": [{"id": 1, "job": "Director", "title": "A", "release_date": "2000-01-01", "genre_ids": [18]},
                 {"id": 1, "job": "Director", "title": "A"},                       # listed twice
                 {"id": 2, "job": "Writer", "title": "B"}],
        "cast": [{"id": 3, "order": 0, "title": "C"}, {"id": 4, "order": 4, "title": "D"},
                 {"id": 5, "order": 5, "title": "E"}, {"id": 6, "title": "No order"}]}}
    p = tmdb.compact_person(raw)
    assert (p["name"], p["profile_path"]) == ("Some Director", "/x.jpg")
    assert [c["id"] for c in p["directed"]] == [1] and p["directed"][0]["genre_ids"] == [18]
    assert [c["id"] for c in p["acted"]] == [3, 4]
    assert tmdb.profile_url("/x.jpg").endswith("/w185/x.jpg")


@pytest.mark.parametrize("c, seen, keep", [
    (credit(1), set(), True),
    (credit(1, genres=(99,)), set(), False),                  # documentary
    (credit(1, genres=(18, 10770)), set(), False),            # TV film
    (credit(1, date_=""), set(), False),                      # no release date
    (credit(1, date_="2027-03-01"), set(), False),            # not out yet
    (credit(1, vc=99), set(), False),                         # too few votes
    (credit(1, vc=100), set(), True),
    (credit(1, vc=3, date_="2026-06-01"), set(), True),       # recent release: bypasses the vote floor
    (credit(1, vc=3, date_="2025-05-01"), set(), False),      # older than the window: floor applies
    (credit(1, vc=3, date_="2026-12-01"), set(), False),      # recent-looking but not out yet
    (credit(1, vc=3, date_="2026-06-01", genres=(99,)), set(), False),  # documentaries stay out
    (credit(1, genres=(99,), vc=3, date_=""), {1}, True),     # seen: always kept
])
def test_credit_filters(c, seen, keep):
    assert completion.keep_credit(c, seen, TODAY) is keep


def test_recent_low_vote_film_in_group_fires_recency_reason(tmp_path):
    cfg = _cfg(tmp_path)
    with db.session(cfg.db_path) as c:
        for tid in range(1, 5):
            seen_film(c, tid, directors=[7])
        store_person(c, person(7, "New Director", directed=[credit(t) for t in range(1, 5)]
                               + [credit(60, vc=2, date_="2026-09-01"), credit(61, vc=2, date_="2015-09-01")]))
        g = completion._people_groups(c, {1, 2, 3, 4}, today=TODAY)[0]
    ids = {f["tmdb_id"] for f in g["films"]}
    assert 60 in ids and 61 not in ids
    d = completion._decorate(g, {1, 2, 3, 4}, {})
    assert completion.score(d, {}, 3.0, date.fromisoformat(TODAY))[1] == "New 2026: P60"


# ---------------------------------------------------------------- eligibility

def test_eligibility_thresholds(tmp_path):
    cfg = _cfg(tmp_path)
    with db.session(cfg.db_path) as c:
        for tid in (1, 2, 3):                       # director 10: 3 seen; director 11: 2 seen
            seen_film(c, tid, directors=[10] + ([11] if tid < 3 else []), cast=[20, 21])
        seen_film(c, 4, cast=[20])                  # actor 20: 4 seen; actor 21: 3 seen
        # an unseen film counts for nobody
        c.execute("INSERT INTO tmdb_details(tmdb_id,json) VALUES(5,?)", (json.dumps({"id": 5, "director_ids": [11], "cast_ids": [21]}),))
        w = completion.wanted_people(c, completion.seen_tmdb_ids(c))
    assert w == {"director": [(10, 3)], "actor": [(20, 4)]}


# ---------------------------------------------------------------- sync

def _runner(cfg, handler, full=False):
    calls: list[str] = []

    def record(request):
        calls.append(request.url.path)
        return handler(request)

    async def run():
        calls.clear()
        async with httpx.AsyncClient(transport=httpx.MockTransport(record)) as http:
            mgr = SyncManager(cfg, http, library.Engine(lambda: db.connect(cfg.db_path), None))
            mgr._stage("tmdb")
            if full:
                await mgr.stage_tmdb()
            else:
                with db.session(cfg.db_path) as c:
                    await mgr._people(c)
        return list(calls)
    return run


def _person_handler(request):
    path = request.url.path
    assert path.startswith("/3/person/"), path
    assert request.url.params["append_to_response"] == "movie_credits"
    pid = int(path.rsplit("/", 1)[1])
    if pid == 999:
        return httpx.Response(404)
    return httpx.Response(200, json={"id": pid, "name": f"Person {pid}", "profile_path": f"/f{pid}.jpg", "movie_credits": {
        "crew": [{"id": 1, "job": "Director", "title": "A", "release_date": "2001-01-01", "vote_count": 500}],
        "cast": [{"id": 2, "order": 1, "title": "B", "release_date": "2002-01-01", "vote_count": 500}]}})


async def test_people_sync_caps_at_40_each_ttl_and_no_refetch(tmp_path):
    cfg = _cfg(tmp_path)
    with db.session(cfg.db_path) as c:
        # 45 directors with 3 seen films each (the first ones with more), 45 actors with 4: 40 + 40 are fetched
        tid = 1000
        for d in range(45):
            for _ in range(3 + (d < 5)):
                seen_film(c, tid, directors=[100 + d])
                tid += 1
        for a in range(45):
            for _ in range(4):
                seen_film(c, tid, cast=[500 + a])
                tid += 1
        w = completion.wanted_people(c, completion.seen_tmdb_ids(c))
    assert len(w["director"]) == 40 and len(w["actor"]) == 40
    assert [p for p, _ in w["director"][:5]] == [100, 101, 102, 103, 104]      # most seen first

    run = _runner(cfg, _person_handler)
    calls = await run()
    assert len(calls) == 80 and len(set(calls)) == 80
    with db.session(cfg.db_path) as c:
        row = c.execute("SELECT name, profile_path, json FROM tmdb_person_credits WHERE person_id=100").fetchone()
        assert (row[0], row[1]) == ("Person 100", "/f100.jpg")
        assert [x["id"] for x in json.loads(row[2])["directed"]] == [1]
    assert await run() == []                                    # nothing stale: 0 calls
    old = (datetime.now(timezone.utc) - timedelta(days=31)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with db.session(cfg.db_path) as c:
        c.execute("UPDATE tmdb_person_credits SET fetched_at=? WHERE person_id IN (100, 500)", (old,))
    assert sorted(await run()) == ["/3/person/100", "/3/person/500"]      # past the TTL only


async def test_people_sync_one_lookup_per_person_and_404(tmp_path):
    cfg = _cfg(tmp_path)
    with db.session(cfg.db_path) as c:
        for tid in range(1, 5):          # 7 directs and stars in 4 seen films; 999 is unknown to TMDB
            seen_film(c, tid, directors=[7, 999], cast=[7])
    run = _runner(cfg, _person_handler)
    assert sorted(await run()) == ["/3/person/7", "/3/person/999"]
    with db.session(cfg.db_path) as c:
        assert c.execute("SELECT json FROM tmdb_person_credits WHERE person_id=999").fetchone()[0] == "{}"
    assert await run() == []


@pytest.mark.parametrize("flag", [False, True])
async def test_flag_off_means_no_person_lookups(tmp_path, flag):
    cfg = _cfg(tmp_path, completionist=flag)
    with db.session(cfg.db_path) as c:
        for tid in range(1, 4):
            seen_film(c, tid, directors=[7])

    def handler(request):
        if request.url.path.startswith("/3/person/"):
            return _person_handler(request)
        raise AssertionError(f"unexpected TMDB call {request.url.path}")

    calls = await _runner(cfg, handler, full=True)()
    assert calls == (["/3/person/7"] if flag else [])


# ---------------------------------------------------------------- groups

def test_director_wins_over_actor_and_needs_seen_and_unseen(tmp_path):
    cfg = _cfg(tmp_path)
    with db.session(cfg.db_path) as c:
        for tid in range(1, 5):
            seen_film(c, tid, directors=[7, 8], cast=[7, 9])
        # 7 directs and acts (eligible as both): only the director group. 8 has nothing left to see: no group.
        store_person(c, person(7, "Both Ways", directed=[credit(t) for t in range(1, 5)] + [credit(50)],
                               acted=[credit(t) for t in range(1, 5)] + [credit(51)]))
        store_person(c, person(8, "All Seen", directed=[credit(t) for t in range(1, 5)]))
        store_person(c, person(9, "Actor Only", acted=[credit(t) for t in range(1, 5)] + [credit(52)]))
        groups = completion._people_groups(c, {1, 2, 3, 4}, today=TODAY)
    assert [(g["key"], g["kind"], g["name"]) for g in groups] == [
        ("director-7", "director", "Both Ways"), ("actor-9", "actor", "Actor Only")]
    assert all(g["ordered"] is False for g in groups)


def test_actor_group_kept_when_director_group_is_dropped(tmp_path):
    cfg = _cfg(tmp_path)
    with db.session(cfg.db_path) as c:
        for tid in range(1, 5):
            seen_film(c, tid, directors=[7], cast=[7])
        # 7 is eligible as both, but every film they directed is seen: no director group, so the actor one stays.
        store_person(c, person(7, "Both Ways", directed=[credit(t) for t in range(1, 5)],
                               acted=[credit(t) for t in range(1, 5)] + [credit(51)]))
        groups = completion._people_groups(c, {1, 2, 3, 4}, today=TODAY)
    assert [(g["key"], g["kind"]) for g in groups] == [("actor-7", "actor")]


def test_actor_capped_at_30_keeping_seen_films(tmp_path):
    cfg = _cfg(tmp_path)
    seen = set(range(1, 6))
    with db.session(cfg.db_path) as c:
        for tid in seen:
            seen_film(c, tid, cast=[9])
        acted = [credit(t, vc=50) for t in seen]                       # seen, but little-voted: kept anyway
        acted += [credit(100 + i, vc=200 + i * 100) for i in range(40)]
        store_person(c, person(9, "Prolific", acted=acted))
        g = completion._people_groups(c, seen, avail={100}, today=TODAY)[0]
    ids = [f["tmdb_id"] for f in g["films"]]
    assert len(ids) == 30 and seen <= set(ids)
    assert 100 in ids                                   # in the library: kept although the least voted
    assert 139 in ids and 101 not in ids                # then the most relevant missing ones

    many = set(range(1, 34))                            # the seen films alone pass the cap: 5 unseen still come
    films = [{"tmdb_id": t, "vote_count": 1000, "vote_average": 7} for t in range(1, 50)]
    out = completion.cap_films(films, many, set())
    assert len(out) == 33 + completion.ACTOR_MIN_UNSEEN and many <= {f["tmdb_id"] for f in out}


@pytest.fixture
def people(client):
    """Seeded (conftest): seen 101, 202, 303, 404 (Alpha rated 4.5); 111 and 113 unwatched in the library.
    Director 525 made 101, 202, 303, 111, plus 9800 (popular, missing), 9801 (a documentary) and 9802 (TV film).
    Saga 900 holds 404 and an unseen film."""
    with db.session(client.cfg.db_path) as c:
        for tid in (101, 202, 303):
            c.execute("UPDATE tmdb_details SET json=json_set(json,'$.director_ids',json('[525]')) WHERE tmdb_id=?", (tid,))
        store_person(c, person(525, "Dee Rector", directed=[
            credit(101, 2015), credit(202, 2019), credit(303, 2001), credit(111, 2016, vc=300),
            credit(9800, 2026, vc=9000, va=8.0), credit(9803, 2008, vc=400, va=6.0),
            credit(9801, 2018, genres=(99,)), credit(9802, 2012, genres=(10770,))]))
        c.execute("INSERT INTO tmdb_collections(collection_id,json,fetched_at) VALUES(900,?,'2026-01-01T00:00:00Z')",
                  (json.dumps({"id": 900, "name": "Old Saga Collection", "parts": [
                      {"id": 404, "title": "Delta Noir", "year": 2012, "release_date": "2012-06-01", "vote_count": 100},
                      {"id": 9900, "title": "Delta Two", "year": 1990, "release_date": "1990-06-01", "vote_count": 100}]}),))
    client.app.state.engine.invalidate()
    return client


def test_people_group_shape_and_film_order(people, monkeypatch):
    monkeypatch.setattr(completion, "_today", lambda: date(2026, 11, 22))
    g = people.get("/api/completions/director-525").json()
    assert (g["kind"], g["name"], g["ordered"], g["person_id"]) == ("director", "Dee Rector", False, 525)
    assert g["profile_url"].endswith("/w185/face525.jpg")
    # the New film first (even though missing), then unseen (library, then missing by relevance), then seen, newest
    # first; documentary and TV film dropped
    assert [(f["tmdb_id"], f["state"]) for f in g["films"]] == [
        (9800, "missing"), (111, "available"), (9803, "missing"), (202, "seen"), (101, "seen"), (303, "seen")]
    assert (g["seen"], g["total"], g["available"]) == (3, 6, 1)
    assert g["backdrop_url"].endswith("/b111.jpg")                     # the next film to watch
    assert g["reason"] == "New 2026: P9800" and g["reason_film"] == 9800
    assert "_genres" not in g["films"][0] and "genre_ids" not in g["films"][0]


def test_person_group_without_recency_keeps_order_and_has_no_reason_film(people, monkeypatch):
    monkeypatch.setattr(completion, "_today", lambda: date(2035, 1, 1))     # nothing is recent any more
    g = people.get("/api/completions/director-525").json()
    assert g["reason_film"] is None and not g["reason"].startswith(("New", "Recent"))
    assert g["films"][0]["tmdb_id"] == 111                                  # library film first again


def test_all_lists_sagas_before_people(people, monkeypatch):
    monkeypatch.setattr(completion, "_today", lambda: date(2026, 11, 22))
    r = people.get("/api/completions").json()
    by = {g["key"]: g for g in r["groups"]}
    assert by["director-525"]["score"] > by["collection-900"]["score"]          # a better score...
    assert [g["key"] for g in r["groups"]] == ["collection-900", "director-525"]  # ...still after the sagas
    assert r["counts"] == {"collection": 1, "franchise": 0, "series": 0, "director": 1, "actor": 0}
    d = people.get("/api/completions", params={"kind": "director"}).json()
    assert [g["key"] for g in d["groups"]] == ["director-525"]
    assert people.post("/api/completions/director-525/hide").json() == {"ok": True}
    assert "director-525" not in [g["key"] for g in people.get("/api/completions").json()["groups"]]
