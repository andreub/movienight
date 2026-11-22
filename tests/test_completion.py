"""Completionist: TMDB collection data, the collection sync, group building (franchises, curated, dedupe),
film states, ranking and hide/unhide. Synthetic data only."""
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from movienight import completion, db, library, tmdb
from movienight.config import Config
from movienight.sync import SyncManager

CONFIG = {
    "franchises": [{"key": "fr", "name": "Franchise", "collections": [910, 920]}],
    "curated": [{"key": "trio", "name": "Trio", "films": [
        {"id": 404, "title": "Delta Noir", "year": 2012}, {"id": 113, "title": "Drama Three", "year": 2010},
        {"id": 9301, "title": "Curated Missing", "year": 2014}]}],
}


@pytest.fixture
def groups(monkeypatch):
    monkeypatch.setattr(completion, "config", lambda: CONFIG)
    return CONFIG


def part(tid, year, va=7.0, vc=1000, title=None):
    return {"id": tid, "title": title or f"Film {tid}", "year": year, "release_date": f"{year}-06-01",
            "poster_path": f"/c{tid}.jpg", "vote_count": vc, "vote_average": va}


def coll(cid, name, parts):
    return {"id": cid, "name": name, "poster_path": None, "backdrop_path": None, "parts": parts}


# seeded (conftest): seen 101, 202, 303, 404; unwatched library films 111, 112, 113, 115, 116, 505
COLLECTIONS = [
    coll(900, "Alpha Collection", [part(101, 2015, 8), part(111, 2016, 8), part(9001, 2018, 8, 50),
                                   part(9002, 2020, 8, 90000)]),
    coll(910, "Fr One Collection", [part(202, 2019, 6), part(9101, 2021, 6)]),
    coll(920, "Fr Two Collection", [part(112, 2020, 6), part(9201, 2022, 6)]),       # nothing seen: still merged
    coll(930, "Trio Collection", [part(404, 2012), part(113, 2010)]),                  # subset of curated "trio"
    coll(940, "Unstarted Collection", [part(115, 1975), part(9401, 1980)]),
    coll(950, "Single Collection", [part(101, 2015)]),
    coll(960, "Long Saga Collection", [part(303, 2001)] + [part(9600 + i, 2002 + i, 6, 100 + i) for i in range(29)]),
    coll(970, "Done Collection", [part(101, 2015), part(202, 2019)]),
]


@pytest.fixture
def cc(client, groups):
    with db.session(client.cfg.db_path) as c:
        for x in COLLECTIONS:
            c.execute("INSERT INTO tmdb_collections(collection_id,json,fetched_at) VALUES(?,?,?)",
                      (x["id"], json.dumps(x), "2026-01-01T00:00:00Z"))
        c.execute("INSERT INTO tmdb_collections(collection_id,json,fetched_at) VALUES(999,'{}','2026-01-01T00:00:00Z')")
    client.app.state.engine.invalidate()
    return client


# ---------------------------------------------------------------- TMDB data

def test_compact_details_collection_and_people_ids():
    raw = {"id": 1, "title": "T", "release_date": "2002-05-01",
           "belongs_to_collection": {"id": 556, "name": "Spider-Man Collection", "poster_path": "/x"},
           "credits": {"crew": [{"id": 7, "name": "Dir", "job": "Director"}, {"id": 8, "name": "W", "job": "Writer"}],
                       "cast": [{"id": 100 + i, "name": f"A{i}", "order": 9 - i} for i in range(10)]}}
    d = tmdb.compact_details(raw)
    assert d["collection"] == {"id": 556, "name": "Spider-Man Collection"}
    assert d["director_ids"] == [7]
    assert d["cast_ids"] == [109, 108, 107, 106, 105]        # top 5 by billing order
    plain = tmdb.compact_details({"id": 2, "title": "N"})
    assert "collection" in plain and plain["collection"] is None
    assert plain["director_ids"] == [] and plain["cast_ids"] == []


def test_compact_collection_sorts_and_drops_unreleased():
    raw = {"id": 5, "name": "Saga Collection", "parts": [
        {"id": 3, "title": "Three", "release_date": "2030-01-01"}, {"id": 2, "title": "Two", "release_date": "2010-01-01"},
        {"id": 4, "title": "Four", "release_date": ""}, {"id": 1, "title": "One", "release_date": "2005-01-01",
                                                          "vote_count": 10, "vote_average": 7.5}]}
    c = tmdb.compact_collection(raw, today="2026-11-22")
    assert [p["id"] for p in c["parts"]] == [1, 2]
    assert c["parts"][0] == {"id": 1, "title": "One", "year": 2005, "release_date": "2005-01-01", "poster_path": None,
                             "vote_count": 10, "vote_average": 7.5}


def test_migration_12_tables(cfg):
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    db.migrate(cfg.db_path)
    with db.session(cfg.db_path) as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS) >= 12
        assert {r[1] for r in c.execute("PRAGMA table_info(tmdb_collections)")} == {"collection_id", "json", "fetched_at"}
        assert {r[1] for r in c.execute("PRAGMA table_info(completion_hidden)")} == {"group_key", "created_at"}


def test_bundled_groups_file_is_valid():
    cfg = completion.config()
    keys = [f["key"] for f in cfg["franchises"]] + [g["key"] for g in cfg["curated"]]
    assert {"spider-man", "cornetto", "vengeance"} <= set(keys) and len(keys) == len(set(keys))
    assert all(k.replace("-", "").isalnum() for k in keys)          # path-safe
    spidey = next(f for f in cfg["franchises"] if f["key"] == "spider-man")
    assert len(spidey["collections"]) == 4
    for g in cfg["curated"]:
        assert len(g["films"]) == 3 and all(isinstance(f["id"], int) and f["title"] for f in g["films"])


# ---------------------------------------------------------------- collection sync

async def test_collection_sync_fetches_once_ttl_and_no_loop(tmp_path, monkeypatch):
    monkeypatch.setattr(completion, "config", lambda: {
        "franchises": [{"key": "f", "name": "F", "collections": [77, 79]}],
        "curated": [{"key": "c", "name": "C", "films": [{"id": 2, "title": "B", "year": 2001},
                                                         {"id": 50, "title": "X", "year": 2002}]},
                    {"key": "u", "name": "U", "films": [{"id": 60, "title": "Y", "year": 2003}]}]})
    cfg = Config(data_dir=tmp_path / "d", letterboxd_dir=tmp_path / "nolb", tmdb_token="eyJtest", sync_on_start=False)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    db.migrate(cfg.db_path)
    with db.session(cfg.db_path) as c:
        for tid in (1, 2, 3):
            c.execute("INSERT INTO lb_films(name,year,norm_title,tmdb_id,resolve_state,watched) VALUES(?,2000,?,?,1,1)",
                      (f"F{tid}", f"f{tid}", tid))
        c.execute("INSERT INTO tmdb_details(tmdb_id,json,fetched_at) VALUES(1,?,'x')",
                  (json.dumps({"id": 1, "release_date": "2000-01-01", "collection": {"id": 77, "name": "S"}}),))
        c.execute("INSERT INTO tmdb_details(tmdb_id,json,fetched_at) VALUES(2,?,'x')",
                  (json.dumps({"id": 2, "release_date": "2000-01-01", "collection": None}),))      # no saga
        c.execute("INSERT INTO tmdb_details(tmdb_id,json,fetched_at) VALUES(3,?,'x')",
                  (json.dumps({"id": 3, "release_date": "2000-01-01"}),))                          # predates the field
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append(path)
        if path.startswith("/3/collection/"):
            cid = int(path.rsplit("/", 1)[1])
            if cid == 78:
                return httpx.Response(404)
            return httpx.Response(200, json={"id": cid, "name": f"C{cid}", "parts": [
                {"id": 1, "title": "A", "release_date": "2000-01-01"}, {"id": 9, "title": "Z", "release_date": "2004-01-01"}]})
        tid = int(path.split("/")[3])
        body = {"id": tid, "title": f"T{tid}", "release_date": "2000-01-01"}
        if tid == 3:
            body["belongs_to_collection"] = {"id": 78, "name": "Gone"}
        return httpx.Response(200, json=body)

    async def run():
        calls.clear()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            mgr = SyncManager(cfg, http, library.Engine(lambda: db.connect(cfg.db_path), None))
            mgr._stage("tmdb")
            with db.session(cfg.db_path) as c:
                await mgr._details(c)
                await mgr._collections(c)
        return sorted(calls)

    # details: 3 (backfill) and 50 (started curated group); never 2 (JSON null) nor 60 (group not started)
    # collections: 77 (seen film 1), 79 (franchise sibling), 78 (from the backfilled film 3, a 404)
    assert await run() == ["/3/collection/77", "/3/collection/78", "/3/collection/79", "/3/movie/3", "/3/movie/50"]
    with db.session(cfg.db_path) as c:
        rows = {r[0]: json.loads(r[1]) for r in c.execute("SELECT collection_id, json FROM tmdb_collections")}
    assert rows[78] == {} and [p["id"] for p in rows[77]["parts"]] == [1, 9]
    assert await run() == []                      # nothing pending: no refetch loop
    old = (datetime.now(timezone.utc) - timedelta(days=31)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with db.session(cfg.db_path) as c:
        c.execute("UPDATE tmdb_collections SET fetched_at=? WHERE collection_id IN (77, 78)", (old,))
    assert await run() == ["/3/collection/77", "/3/collection/78"]     # past the TTL


# ---------------------------------------------------------------- groups

def test_groups_franchise_curated_dedupe_and_ranking(cc):
    r = cc.get("/api/completions").json()
    keys = [g["key"] for g in r["groups"]]
    # with available films first; then % seen (trio 1/3), then fewest missing, then mean TMDB rating (900 > fr)
    assert keys == ["curated-trio", "collection-900", "franchise-fr", "collection-960"]
    assert [g["key"] for g in r["completed"]] == ["collection-970"]
    # absorbed by the franchise (910, 920), replaced by curated (930), not started (940), too small (950), 404 (999)
    all_keys = keys + [g["key"] for g in r["completed"]]
    assert not {"collection-910", "collection-920", "collection-930", "collection-940", "collection-950",
                "collection-999"} & set(all_keys)
    assert r["counts"] == {"collection": 3, "franchise": 1, "curated": 1}
    assert r["hidden"] == [] and r["hidden_count"] == 0

    fr = next(g for g in r["groups"] if g["key"] == "franchise-fr")
    assert (fr["name"], fr["seen"], fr["total"], fr["available"], fr["missing"]) == ("Franchise", 1, 4, 1, 2)
    trio = r["groups"][0]
    assert (trio["seen"], trio["total"], trio["available"]) == (1, 3, 1)
    assert [f["title"] for f in trio["films_preview"]] == ["Drama Three", "Curated Missing"]   # title from the file


def test_film_states_preview_and_next(cc):
    g = next(g for g in cc.get("/api/completions").json()["groups"] if g["key"] == "collection-900")
    assert g["name"] == "Alpha"                                   # " Collection" dropped
    prev = g["films_preview"]
    # available first, then missing by relevance (votes x rating): 9002 (90k votes) before 9001 (50 votes)
    assert [(f["tmdb_id"], f["state"]) for f in prev] == [(111, "available"), (9002, "missing"), (9001, "missing")]
    assert prev[0]["movie_id"] and "movie_id" not in prev[1]
    assert g["next"]["title"] == "Film 111" and g["next"]["movie_id"] == prev[0]["movie_id"]
    assert g["more"] == 0 and not any(f["state"] == "seen" for f in prev)


def test_long_saga_caps_preview_and_full_list(cc):
    g = next(g for g in cc.get("/api/completions").json()["groups"] if g["key"] == "collection-960")
    assert len(g["films_preview"]) == 5 and g["more"] == 24 and g["available"] == 0 and g["next"] is None
    assert [f["vote_count"] for f in g["films_preview"]] == [128, 127, 126, 125, 124]
    full = cc.get("/api/completions/collection-960").json()
    assert len(full["films"]) == 30 and full["films"][0] == {**full["films"][0], "tmdb_id": 303, "state": "seen"}
    assert [f["year"] for f in full["films"]] == sorted(f["year"] for f in full["films"])
    assert cc.get("/api/completions/collection-12345").status_code == 404


def test_kind_filter(cc):
    r = cc.get("/api/completions", params={"kind": "curated"}).json()
    assert [g["key"] for g in r["groups"]] == ["curated-trio"] and r["completed"] == []
    assert cc.get("/api/completions", params={"kind": "nope"}).status_code == 422


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


def test_no_groups_without_collections(client):
    r = client.get("/api/completions").json()
    assert [g["key"] for g in r["groups"] + r["completed"]] == []      # bundled curated films are not seen here
