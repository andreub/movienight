import httpx

from movienight import db, library, plex, tmdb
from movienight.sync import SyncManager

from test_seen_push import mark, mid, push_client, pushes, run_sync_push, seen_keys  # noqa: F401
from test_sync_people_stage import _cfg, _no_sleep
from test_completion_people import seen_film


async def test_exact_title_in_adjacent_year_beats_fuzzy_first_year():
    def handler(req):
        year = req.url.params.get("year")
        if year == "1973":      # fuzzy substring only
            return httpx.Response(200, json={"results": [{"id": 2, "title": "The Godfather Part II"}]})
        if year == "1972":
            return httpx.Response(200, json={"results": [{"id": 1, "title": "The Godfather"}]})
        return httpx.Response(200, json={"results": []})

    t = tmdb.Tmdb(httpx.AsyncClient(transport=httpx.MockTransport(handler)), "k")
    assert await t.search("The Godfather", 1973) == 1


def test_pick_search_result_fuzzy_flag():
    data = {"results": [{"id": 2, "title": "The Godfather Part II"}]}
    assert tmdb.pick_search_result(data, "the godfather", fuzzy=False) is None
    assert tmdb.pick_search_result(data, "the godfather") == 2


async def test_slug_non_dict_json_is_transient():
    def handler(req):
        return httpx.Response(200, json=[1, 2])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        assert await plex.resolve_slug(c, "cid", "plex://movie/abc") is None


def test_migrations_apply_and_are_idempotent(tmp_path):
    p = tmp_path / "m.db"
    db.migrate(p)
    db.migrate(p)
    conn = db.connect(p)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)


async def test_non_tmdb_error_in_a_phase_does_not_skip_recs(tmp_path):
    """A 5xx (HTTPStatusError, not TmdbError) from the details phase is logged; recommendations still run."""
    cfg = _cfg(tmp_path)
    with db.session(cfg.db_path) as c:
        for tid in range(1, 4):
            seen_film(c, tid)
        c.execute("UPDATE lb_films SET rating=5, liked=1 WHERE tmdb_id=1")

    def handler(request):
        if request.url.path == "/3/movie/1/recommendations":
            return httpx.Response(200, json={"results": [{"id": 5}]})
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        mgr = SyncManager(cfg, http, library.Engine(lambda: db.connect(cfg.db_path), None))
        mgr.tmdb.sleep = _no_sleep
        mgr._stage("tmdb")
        await mgr.stage_tmdb()
    with db.session(cfg.db_path) as c:
        assert c.execute("SELECT COUNT(*) FROM tmdb_recs WHERE tmdb_id=1").fetchone()[0] == 1


def test_unpush_404_counts_as_done(push_client):
    c = push_client
    mark(c, "Crime Two")
    c.fake.status = 404         # item deleted on the server
    mark(c, "Crime Two", False)
    out = run_sync_push(c)
    assert out["failed"] == 0 and pushes(c) == []
