"""plex.tv watch-history fix-up after a seen push. plex.tv is faked; synthetic data only."""
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import GUIDS, seed
from movienight import db, plex_history
from movienight.app import create_app
from movienight.util import norm_title
from test_seen_push import mark as _mark, mid, pushes

UUID = "00000000-0000-0000-0000-000000000000"


def drain(c):
    """The history fix-up runs in the background after the push; wait for it."""
    c.portal.call(c.app.state.sync.drain_history)


def mark(c, *a, **kw):
    r = _mark(c, *a, **kw)
    drain(c)
    return r


def lb_push(c, backfill=False):
    """One full sync push including the Letterboxd-watched films."""
    sync = c.app.state.sync

    async def go():
        conn = db.connect(c.cfg.db_path)
        try:
            return await sync.push_seen(conn, db.client_id(conn), lb=True)
        finally:
            conn.close()

    out = c.portal.call(go)
    drain(c)
    return out


def scrobbled(fake):
    return sorted(r.url.params["key"] for r in fake.calls if r.url.path == "/:/scrobble")


def ago(minutes):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class FakeServices:
    """Media server scrobbles, plex.tv account and the community GraphQL API."""

    def __init__(self, nodes=None):
        self.nodes = nodes if nodes is not None else []
        self.gql: list[dict] = []
        self.errors: dict[str, list] = {}       # operationName -> errors answered with HTTP 200
        self.reject_year = False
        self.history_calls = 0
        self.calls: list[httpx.Request] = []
        self.fail_discover = False
        self.view_counts: dict[str, int] = {}     # plex.tv userState per discover id
        self.discover: list[tuple[str, str]] = []   # (scrobble|unscrobble, discover id)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if request.url.path in ("/:/scrobble", "/:/unscrobble"):
            return httpx.Response(200)
        if request.url.host == "plex.local" and request.url.path.startswith("/library/metadata/"):
            guid = GUIDS.get(int(request.url.path.rsplit("/", 1)[-1]))
            return httpx.Response(200, json={"MediaContainer": {"Metadata": [{"guid": guid}] if guid else [{}]}})
        if request.url.host == "discover.provider.plex.tv":
            assert request.headers["X-Plex-Token"] == "TOK" and "TOK" not in str(request.url)
            parts = request.url.path.strip("/").split("/")
            if request.method == "GET" and parts[-1] == "userState":
                n = self.view_counts.get(parts[-2], 0)
                return httpx.Response(200, json={"MediaContainer": {"UserState": [{"viewCount": n}]}})
            assert request.method == "PUT" and parts[0] == "actions"
            if self.fail_discover:
                return httpx.Response(500)
            assert request.url.params["identifier"] == "tv.plex.provider.discover"
            did = request.url.params["key"]
            self.discover.append((parts[1], did))
            self.view_counts[did] = 1 if parts[1] == "scrobble" else 0
            return httpx.Response(200, json={})
        if request.url.host == "plex.tv" and request.url.path == "/api/v2/user":
            return httpx.Response(200, json={"id": 1, "uuid": UUID, "username": "someone"})
        if request.url.host == "community.plex.tv":
            assert request.headers["X-Plex-Token"] == "TOK" and "TOK" not in str(request.url)
            body = json.loads(request.content)
            self.gql.append(body)
            op = body["operationName"]
            if op in self.errors:
                return httpx.Response(200, json={"errors": self.errors[op]})
            if op == "GetWatchHistoryHub":
                self.history_calls += 1
                assert body["variables"]["uuid"] == UUID
                nodes = self.nodes
                if self.reject_year:
                    if "year" in body["query"]:
                        return httpx.Response(200, json={"errors": [{"message": 'Cannot query field "year"'}]})
                    nodes = [{**n, "metadataItem": {k: v for k, v in n["metadataItem"].items() if k != "year"}}
                             for n in nodes]
                return httpx.Response(200, json={"data": {"user": {"watchHistory": {"nodes": nodes}}}})
            return httpx.Response(200, json={"data": {"ok": True}})
        raise AssertionError(f"unexpected network call: {request.url.host}{request.url.path}")

    def mutations(self):
        return [b for b in self.gql if b["operationName"] != "GetWatchHistoryHub"]


DISCOVER_IDS = {"Crime Two": "aaa", "Rom Com": "bbb"}     # conftest GUIDS: plex://movie/aaa, plex://movie/bbb


def node(nid, title, year, when, type_="MOVIE", did=None):
    did = did or DISCOVER_IDS.get(title) or "x" + nid
    return {"id": nid, "date": when, "metadataItem": {"id": did, "title": title, "type": type_, "year": year}}


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(plex_history, "LOOKUP_DELAY", 0)
    monkeypatch.setattr(plex_history, "MUTATION_DELAY", 0)


def make(cfg, fake, **over):
    app = create_app(replace(cfg, push_seen=True, plex_token="TOK", **over), transport=httpx.MockTransport(fake))
    c = TestClient(app)
    c.cfg = cfg
    return app, c


def start(app, c, cfg):
    seed(cfg.db_path)
    app.state.engine.invalidate()


def add_diary(cfg, tmdb, name, year, day):
    with db.session(cfg.db_path) as c:
        c.execute("INSERT INTO lb_films(name,year,norm_title,tmdb_id,resolve_state,watched,watched_date)"
                  " VALUES(?,?,?,?,1,1,?)", (name, year, norm_title(name), tmdb, day))


def run(cfg, fake, body, diary=None, **over):
    app, c = make(cfg, fake, **over)
    with c:
        start(app, c, cfg)
        if diary:
            add_diary(cfg, *diary)
        body(c)


def test_known_date_edits_activity(cfg):
    fake = FakeServices([node("a1", "Crime Two", 2016, ago(0)), node("old", "Crime Two", 2016, ago(60 * 24))])

    def body(c):
        mark(c, "Crime Two")
        [m] = fake.mutations()
        assert m["operationName"] == "updateActivityDate"
        assert m["variables"] == {"id": "a1", "input": {"date": "2024-03-05T20:00:00.000Z"}}
        assert pushes(c) == [("tmdb:111", "m1", "11")]

    run(cfg, fake, body, diary=(111, "Crime Two", 2016, "2024-03-05"))


def test_unknown_date_removes_activity(cfg):
    fake = FakeServices([node("a1", "Crime Two", 2016, ago(1))])

    def body(c):
        mark(c, "Crime Two")
        [m] = fake.mutations()
        assert m["operationName"] == "removeActivity"
        assert m["variables"] == {"input": {"id": "a1", "type": "WATCH_HISTORY"}}

    run(cfg, fake, body)


def test_no_match_sends_no_mutation(cfg):
    fake = FakeServices([node("a1", "Other Film", 2016, ago(0)), node("a2", "Crime Two", 1999, ago(0), did="zzz")])

    def body(c):
        mark(c, "Crime Two")
        assert fake.mutations() == []
        assert fake.history_calls == plex_history.LOOKUP_TRIES      # retried, bounded
        assert pushes(c) == [("tmdb:111", "m1", "11")]

    run(cfg, fake, body)


def test_old_entries_never_touched(cfg):
    fake = FakeServices([node("old", "Crime Two", 2016, ago(30))])

    def body(c):
        mark(c, "Crime Two")
        assert fake.mutations() == []

    run(cfg, fake, body)


def test_rate_limit_is_retried_once_and_push_survives(cfg, monkeypatch):
    slept = []

    async def nap(s):
        slept.append(s)

    monkeypatch.setattr(plex_history, "_sleep", nap)
    fake = FakeServices([node("a1", "Crime Two", 2016, ago(0))])
    fake.errors["removeActivity"] = [{"message": "slow", "extensions": {"code": "RATE_LIMITED", "retryAfter": 2}}]

    def body(c):
        mark(c, "Crime Two")
        assert [m["operationName"] for m in fake.mutations()] == ["removeActivity"] * 2
        assert 2.0 in slept
        assert pushes(c) == [("tmdb:111", "m1", "11")]

    run(cfg, fake, body)


def test_graphql_error_on_history_is_swallowed(cfg):
    fake = FakeServices([node("a1", "Crime Two", 2016, ago(0))])
    fake.errors["GetWatchHistoryHub"] = [{"message": "boom"}]

    def body(c):
        mark(c, "Crime Two")
        assert fake.mutations() == [] and pushes(c) == [("tmdb:111", "m1", "11")]

    run(cfg, fake, body)


def test_falls_back_when_year_field_rejected(cfg):
    fake = FakeServices([node("a1", "Crime Two", 2016, ago(0))])
    fake.reject_year = True

    def body(c):
        mark(c, "Crime Two")
        assert [m["operationName"] for m in fake.mutations()] == ["removeActivity"]

    run(cfg, fake, body)


def test_disabled_makes_no_plex_tv_call(cfg):
    fake = FakeServices([node("a1", "Crime Two", 2016, ago(0))])

    def body(c):
        mark(c, "Crime Two")
        assert fake.gql == [] and pushes(c) == [("tmdb:111", "m1", "11")]

    run(cfg, fake, body, plex_history_fix=False)


def test_uuid_is_cached(cfg):
    fake = FakeServices([node("a1", "Crime Two", 2016, ago(0)), node("a2", "Rom Com", 2020, ago(0))])

    def body(c):
        mark(c, "Crime Two")
        mark(c, "Rom Com")
        with db.session(cfg.db_path) as d:
            assert d.execute("SELECT value FROM meta WHERE key='plex_uuid'").fetchone()[0] == UUID

    run(cfg, fake, body)


def test_env_flag(monkeypatch):
    from movienight.config import Config
    monkeypatch.delenv("MOVIENIGHT_PLEX_HISTORY_FIX", raising=False)
    assert Config.from_env().plex_history_fix is True
    monkeypatch.setenv("MOVIENIGHT_PLEX_HISTORY_FIX", "0")
    assert Config.from_env().plex_history_fix is False


def test_letterboxd_watched_without_diary_date_removes(cfg):
    fake = FakeServices([node("a1", "Crime Two", 2016, ago(0))])

    def body(c):
        mark(c, "Crime Two")        # also stored locally with a "marked at" timestamp: never used as a watch date
        [m] = fake.mutations()
        assert m["operationName"] == "removeActivity"

    run(cfg, fake, body, diary=(111, "Crime Two", 2016, None))


def test_baseline_skips_existing_letterboxd_history(cfg):
    fake = FakeServices([node("g1", "Gamma", 2001, ago(0))])

    def body(c):
        out = lb_push(c)                 # Gamma is watched on Letterboxd (seeded) but predates the baseline
        assert out["pushed"] == 0 and scrobbled(fake) == [] and fake.gql == []
        with db.session(cfg.db_path) as d:
            assert d.execute("SELECT value FROM meta WHERE key='lb_push_baseline'").fetchone()
            assert d.execute("SELECT COUNT(*) FROM lb_baseline").fetchone()[0] == 4
        lb_push(c)
        assert scrobbled(fake) == []

    run(cfg, fake, body)


def test_new_letterboxd_film_with_diary_date_is_marked_and_redated(cfg):
    fake = FakeServices([node("a1", "Crime Two", 2016, ago(0))])

    def body(c):
        lb_push(c)                                         # baseline
        add_diary(cfg, 111, "Crime Two", 2016, "2024-03-05")
        out = lb_push(c)
        assert out["pushed"] == 1 and scrobbled(fake) == ["11"]
        [m] = fake.mutations()
        assert m["operationName"] == "updateActivityDate"
        assert m["variables"] == {"id": "a1", "input": {"date": "2024-03-05T20:00:00.000Z"}}
        assert pushes(c) == [("tmdb:111", "m1", "11")]
        again = lb_push(c)                                 # recorded: not pushed twice, and never unscrobbled
        assert again["pushed"] == 0 and again["unpushed"] == 0 and again["unpush_failed"] == 0
        assert scrobbled(fake) == ["11"]
        with db.session(cfg.db_path) as d:
            assert d.execute("SELECT source FROM seen_pushes").fetchone()[0] == "lb"

    run(cfg, fake, body)


def test_new_letterboxd_film_without_diary_date_is_marked_and_removed(cfg):
    fake = FakeServices([node("a1", "Crime Two", 2016, ago(0))])

    def body(c):
        lb_push(c)
        add_diary(cfg, 111, "Crime Two", 2016, None)
        assert lb_push(c)["pushed"] == 1 and scrobbled(fake) == ["11"]
        [m] = fake.mutations()
        assert m["operationName"] == "removeActivity"
        assert m["variables"] == {"input": {"id": "a1", "type": "WATCH_HISTORY"}}

    run(cfg, fake, body)


def test_backfill_is_opt_in_and_uses_diary_dates(cfg):
    fake = FakeServices([node("g1", "Gamma", 2001, ago(0))])

    def body(c):
        out = lb_push(c)
        assert out["pushed"] == 1 and scrobbled(fake) == ["14"]       # Gamma, from the pre-existing history
        [m] = fake.mutations()
        assert m["variables"]["input"]["date"] == "2024-01-01T20:00:00.000Z"

    run(cfg, fake, body, push_lb_backfill=True)


def test_manual_mark_is_still_removed_even_when_letterboxd_has_other_films(cfg):
    fake = FakeServices([node("a1", "Rom Com", 2020, ago(0))])

    def body(c):
        mark(c, "Rom Com")
        [m] = fake.mutations()
        assert m["operationName"] == "removeActivity"

    run(cfg, fake, body)


def test_manual_click_does_not_wait_for_history_fix(cfg):
    fake = FakeServices([node("a1", "Crime Two", 2016, ago(0))])

    def body(c):
        _mark(c, "Crime Two")                              # returns without draining the background fix-up
        assert pushes(c) == [("tmdb:111", "m1", "11")]
        drain(c)
        assert len(fake.mutations()) == 1

    run(cfg, fake, body)


def test_discover_scrobble_marks_watched_on_plex_tv_then_fixes_history(cfg):
    fake = FakeServices([node("a1", "Crime Two", 2016, ago(0))])

    def body(c):
        mark(c, "Crime Two")
        assert fake.discover == [("scrobble", "aaa")]
        assert fake.view_counts["aaa"] == 1
        order = [r.url.host for r in fake.calls if r.url.path in ("/:/scrobble", "/actions/scrobble", "/api")]
        assert order[:2] == ["plex.local", "discover.provider.plex.tv"] and set(order[2:]) == {"community.plex.tv"}
        assert [m["operationName"] for m in fake.mutations()] == ["removeActivity"]
        with db.session(cfg.db_path) as d:
            assert d.execute("SELECT discover_at FROM seen_pushes").fetchone()[0]

    run(cfg, fake, body)


def test_already_watched_on_plex_tv_is_left_alone(cfg):
    fake = FakeServices([node("a1", "Crime Two", 2016, ago(0))])
    fake.view_counts["aaa"] = 1

    def body(c):
        mark(c, "Crime Two")
        assert fake.discover == [] and fake.mutations() == [] and fake.history_calls == 0

    run(cfg, fake, body)


def test_discover_failure_does_not_break_push_and_is_retried_by_catchup(cfg):
    fake = FakeServices([node("a1", "Crime Two", 2016, ago(0))])

    def body(c):
        fake.view_counts["aaa"] = 0
        fake.fail_discover = True
        mark(c, "Crime Two")
        assert pushes(c) == [("tmdb:111", "m1", "11")] and fake.mutations() == []
        with db.session(cfg.db_path) as d:
            assert d.execute("SELECT discover_at FROM seen_pushes").fetchone()[0] is None
        fake.fail_discover = False
        sync_push(c)
        assert fake.discover == [("scrobble", "aaa")]
        assert [m["operationName"] for m in fake.mutations()] == ["removeActivity"]

    run(cfg, fake, body)


def sync_push(c, lb=False):
    sync = c.app.state.sync

    async def go():
        conn = db.connect(c.cfg.db_path)
        try:
            return await sync.push_seen(conn, db.client_id(conn), lb=lb)
        finally:
            conn.close()

    out = c.portal.call(go)
    drain(c)
    return out


def old_push(cfg, key, server, rk, source="seen"):
    with db.session(cfg.db_path) as d:
        d.execute("INSERT INTO seen_pushes(key,server_id,rating_key,pushed_at,source) VALUES(?,?,?,?,?)",
                  (key, server, rk, "2020-01-01T00:00:00Z", source))


def test_catchup_settles_old_manual_push_once(cfg):
    fake = FakeServices([node("a1", "Crime Two", 2016, ago(0))])

    def body(c):
        old_push(cfg, "tmdb:111", "m1", "11")
        with db.session(cfg.db_path) as d:
            d.execute("INSERT INTO seen(key,created_at) VALUES('tmdb:111','2020-01-01T00:00:00Z')")
        sync_push(c)
        assert fake.discover == [("scrobble", "aaa")]
        assert [m["operationName"] for m in fake.mutations()] == ["removeActivity"]
        sync_push(c)                                         # once per item
        assert fake.discover == [("scrobble", "aaa")] and len(fake.mutations()) == 1

    run(cfg, fake, body)


def test_catchup_redates_letterboxd_push_and_skips_when_plex_tv_has_it(cfg):
    fake = FakeServices([node("a1", "Crime Two", 2016, ago(0))])

    def body(c):
        add_diary(cfg, 111, "Crime Two", 2016, "2023-07-01")
        old_push(cfg, "tmdb:111", "m1", "11", source="lb")
        sync_push(c)
        [m] = fake.mutations()
        assert m["operationName"] == "updateActivityDate"
        assert m["variables"]["input"]["date"] == "2023-07-01T20:00:00.000Z"

    run(cfg, fake, body)


def test_catchup_skips_item_plex_tv_already_has_watched(cfg):
    fake = FakeServices([node("a1", "Crime Two", 2016, ago(0))])
    fake.view_counts["aaa"] = 1

    def body(c):
        old_push(cfg, "tmdb:111", "m1", "11", source="lb")
        sync_push(c)
        assert fake.discover == [] and fake.mutations() == []
        with db.session(cfg.db_path) as d:
            assert d.execute("SELECT discover_at FROM seen_pushes").fetchone()[0]

    run(cfg, fake, body)


def test_unmark_also_unscrobbles_on_plex_tv(cfg):
    fake = FakeServices([node("a1", "Crime Two", 2016, ago(0))])

    def body(c):
        mark(c, "Crime Two")
        r = c.post(f"/api/movies/{mid(c, 'Crime Two')}/seen", json={"seen": False})
        assert r.status_code == 200
        assert fake.discover == [("scrobble", "aaa"), ("unscrobble", "aaa")]

    run(cfg, fake, body)


def test_history_outcomes_are_logged_at_info(cfg, caplog):
    import logging
    caplog.set_level(logging.INFO, logger="movienight")
    fake = FakeServices([node("a1", "Crime Two", 2016, ago(0)), node("a2", "Rom Com", 2020, ago(0))])

    def body(c):
        add_diary(cfg, 112, "Rom Com", 2020, "2024-02-02")
        mark(c, "Crime Two")
        mark(c, "Rom Com")

    run(cfg, fake, body)
    text = caplog.text
    assert "plex history: removed" in text and "plex history: re-dated" in text and "2024-02-02" in text
    assert "TOK" not in text

    caplog.clear()
    fake2 = FakeServices([])
    run(replace(cfg, data_dir=cfg.data_dir / "b"), fake2, lambda c: mark(c, "Crime Two"))
    assert "no new entry found" in caplog.text


def test_app_loggers_print_info_to_stdout(monkeypatch, capsys):
    import logging
    import sys
    from movienight import app as app_mod
    pkg, root = logging.getLogger("movienight"), logging.getLogger()
    saved = (pkg.handlers[:], pkg.level, pkg.propagate, root.handlers[:])
    pkg.handlers, root.handlers = [], []
    try:
        app_mod.configure_logging()
        assert pkg.level == logging.INFO and any(isinstance(h, logging.StreamHandler) and h.stream is sys.stdout
                                                 for h in pkg.handlers)
        logging.getLogger("movienight.sync").info("hello from the app")
        assert "hello from the app" in capsys.readouterr().out
    finally:
        pkg.handlers, pkg.level, pkg.propagate = saved[0], saved[1], saved[2]
        root.handlers = saved[3]
