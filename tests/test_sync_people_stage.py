"""People stage of the TMDB sync: a TMDB failure there does not skip recommendations, the eligibility scan runs off
the event loop with a shorter TTL for active people, and the seen set honours the backend's count_watched flag."""
import json
from datetime import datetime, timedelta, timezone

import httpx

from movienight import completion, db, library
from movienight.config import Config
from movienight.sync import SyncManager

from test_completion_people import credit, person, seen_film, store_person


def _cfg(tmp_path):
    cfg = Config(data_dir=tmp_path / "d", letterboxd_dir=tmp_path / "nolb", tmdb_token="eyJtest", sync_on_start=False)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    db.migrate(cfg.db_path)
    return cfg


async def _no_sleep(_):
    return None


async def test_people_failure_does_not_skip_recs(tmp_path):
    """An exhausted 429 retry in the people stage is logged; the recommendations stage still runs."""
    cfg = _cfg(tmp_path)
    with db.session(cfg.db_path) as c:
        for tid in range(1, 4):
            seen_film(c, tid, directors=[7])
        c.execute("UPDATE lb_films SET rating=5, liked=1 WHERE tmdb_id=1")

    def handler(request):
        path = request.url.path
        if path.startswith("/3/person/"):
            return httpx.Response(429, headers={"Retry-After": "0"})
        assert path == "/3/movie/1/recommendations", path
        return httpx.Response(200, json={"results": [{"id": 5}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        mgr = SyncManager(cfg, http, library.Engine(lambda: db.connect(cfg.db_path), None))
        mgr.tmdb.sleep = _no_sleep
        mgr._stage("tmdb")
        await mgr.stage_tmdb()
    with db.session(cfg.db_path) as c:
        assert c.execute("SELECT COUNT(*) FROM tmdb_recs WHERE tmdb_id=1").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM tmdb_person_credits").fetchone()[0] == 0


def test_person_ttl_shorter_for_recent_or_upcoming_films(tmp_path):
    cfg = _cfg(tmp_path)
    now = datetime.now(timezone.utc)
    stamp = (now - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    soon = (now + timedelta(days=30)).strftime("%Y-%m-%d")
    with db.session(cfg.db_path) as c:
        for tid in range(1, 4):
            seen_film(c, tid, directors=[7, 8])
        store_person(c, person(7, "Active", directed=[credit(1, date_=soon)]), fetched=stamp)
        store_person(c, person(8, "Quiet", directed=[credit(2, year=1999)]), fetched=stamp)
    mgr = SyncManager(cfg, None, library.Engine(lambda: db.connect(cfg.db_path), None))
    assert mgr._stale_people() == [7]


def test_seen_ids_ignore_server_plays_when_not_counted(tmp_path):
    cfg = _cfg(tmp_path)
    with db.session(cfg.db_path) as c:
        c.execute("INSERT INTO movies(dkey,tmdb_id) VALUES('k',50)")
        mid = c.execute("SELECT id FROM movies").fetchone()[0]
        c.execute("INSERT INTO media_items(server_id,rating_key,movie_id,json) VALUES('s','k',?,?)",
                  (mid, json.dumps({"view_count": 2})))
        assert completion.seen_tmdb_ids(c) == {50}
        assert completion.seen_tmdb_ids(c, count_watched=False) == set()
