"""plex.tv watch-history fix-up after a seen push. plex.tv is faked; synthetic data only."""
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import seed
from movienight import db, plex_history
from movienight.app import create_app
from movienight.util import norm_title
from test_seen_push import mark as _mark, pushes

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

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if request.url.path == "/:/scrobble":
            return httpx.Response(200)
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


def node(nid, title, year, when, type_="MOVIE"):
    return {"id": nid, "date": when, "metadataItem": {"id": "x" + nid, "title": title, "type": type_, "year": year}}


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
    fake = FakeServices([node("a1", "Other Film", 2016, ago(0)), node("a2", "Crime Two", 1999, ago(0))])

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
