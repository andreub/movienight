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
        self.fail_ids: set[str] = set()
        self.limits: dict[str, list] = {}           # op -> errors answered once each, then success
        self.limit_forever: dict[str, dict] = {}    # op -> error answered every time
        self.pages: list = []
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
            if op in ("removeActivity", "updateActivityDate"):
                v = body["variables"]
                if (v.get("id") or (v.get("input") or {}).get("id")) in self.fail_ids:
                    return httpx.Response(200, json={"errors": [{"message": "nope"}]})
            if op in self.limit_forever:
                return httpx.Response(200, json={"errors": [self.limit_forever[op]]})
            if self.limits.get(op):
                return httpx.Response(200, json={"errors": [self.limits[op].pop(0)]})
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
                self.pages.append(body["variables"].get("after"))
                start = int(body["variables"].get("after") or 0)
                size = body["variables"]["first"]
                page = nodes[start:start + size]
                info = {"hasNextPage": start + size < len(nodes), "endCursor": str(start + size)}
                return httpx.Response(200, json={"data": {"user": {"watchHistory": {"nodes": page,
                                                                                    "pageInfo": info}}}})
            v = body["variables"]                         # keep the fake history in step with the mutations
            if op == "removeActivity":
                self.nodes = [n for n in self.nodes if n["id"] != v["input"]["id"]]
            elif op == "updateActivityDate":
                for n in self.nodes:
                    if n["id"] == v["id"]:
                        n["date"] = v["input"]["date"]
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
    monkeypatch.setattr(plex_history, "PAGE_DELAY", 0)


def make(cfg, fake, **over):
    over.setdefault("plex_history_pace", 0)
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
        app.state.sync.followup_delay = 3600        # follow-up batches are tested explicitly
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


def nap_recorder(monkeypatch):
    slept = []

    async def nap(sec):
        slept.append(sec)

    monkeypatch.setattr(plex_history, "_sleep", nap)
    return slept


def test_retry_after_is_honored_and_the_same_film_is_retried(cfg, monkeypatch):
    slept = nap_recorder(monkeypatch)
    fake = FakeServices([node("a1", "Crime Two", 2016, ago(0))])
    fake.limits["removeActivity"] = [{"message": "slow", "extensions": {"code": "RATE_LIMITED", "retryAfter": 7}}]

    def body(c):
        mark(c, "Crime Two")
        assert [m["variables"]["input"]["id"] for m in fake.mutations()] == ["a1", "a1"]   # same film, again
        assert 7.0 in slept and not any(x >= 60 for x in slept)
        assert pushes(c) == [("tmdb:111", "m1", "11")] and row_state(cfg)[0]                # fixed in the end

    run(cfg, fake, body)


def test_rate_limit_without_retry_after_backs_off_exponentially(cfg, monkeypatch):
    slept = nap_recorder(monkeypatch)
    fake = FakeServices([node("a1", "Crime Two", 2016, ago(0))])
    msg = {"message": "Rate limit exceeded!"}                  # the shape seen live: no code, no retryAfter
    fake.limits["removeActivity"] = [msg, msg, msg]

    def body(c):
        mark(c, "Crime Two")
        assert [x for x in slept if x >= 60] == [60.0, 120.0, 240.0]
        assert len(fake.mutations()) == 4 and row_state(cfg)[0]

    run(cfg, fake, body)


def test_batch_stops_after_the_longest_backoff_without_failure_noise(cfg, monkeypatch, caplog):
    import logging
    caplog.set_level(logging.INFO, logger="movienight")
    slept = nap_recorder(monkeypatch)
    pushed = 60 * 48
    fake = FakeServices([node("a1", "Crime Two", 2016, ago(pushed - 3)), node("b1", "Rom Com", 2020, ago(pushed - 3))])
    fake.view_counts.update(aaa=1, bbb=1)
    fake.limit_forever["removeActivity"] = {"message": "Rate limit exceeded!"}

    def body(c):
        old_push(cfg, "tmdb:111", "m1", "11", minutes_ago=pushed)
        old_push(cfg, "tmdb:112", "m1", "12", minutes_ago=pushed)
        sync_push(c)
        assert [x for x in slept if x >= 60] == [60.0, 120.0, 240.0, 480.0, 600.0]
        assert len(fake.mutations()) == 1 + len(plex_history.RATE_WAITS)        # only the first film was tried
        assert all(v is None for v in discover_at(cfg).values())               # both left unprocessed
        assert "will retry" not in caplog.text and "WARNING" not in " ".join(r.levelname for r in caplog.records)
        assert "still rate limited" in caplog.text

    run(cfg, fake, body)


def test_unprocessed_rows_resume_when_the_limit_lifts(cfg, monkeypatch):
    nap_recorder(monkeypatch)
    pushed = 60 * 48
    fake = FakeServices([node("a1", "Crime Two", 2016, ago(pushed - 3)), node("b1", "Rom Com", 2020, ago(pushed - 3))])
    fake.view_counts.update(aaa=1, bbb=1)
    fake.limit_forever["removeActivity"] = {"message": "Rate limit exceeded!"}

    def body(c):
        old_push(cfg, "tmdb:111", "m1", "11", minutes_ago=pushed)
        old_push(cfg, "tmdb:112", "m1", "12", minutes_ago=pushed)
        sync_push(c)
        fake.limit_forever.clear()
        sync_push(c)
        assert sorted(n["id"] for n in fake.nodes) == []
        assert all(discover_at(cfg).values())

    run(cfg, fake, body)


def test_pace_setting_is_read_from_env(monkeypatch):
    from movienight.config import Config
    monkeypatch.delenv("MOVIENIGHT_PLEX_HISTORY_PACE", raising=False)
    assert Config.from_env().plex_history_pace == 6.0
    monkeypatch.setenv("MOVIENIGHT_PLEX_HISTORY_PACE", "2.5")
    assert Config.from_env().plex_history_pace == 2.5
    monkeypatch.setenv("MOVIENIGHT_PLEX_HISTORY_PACE", "junk")
    assert Config.from_env().plex_history_pace == 6.0


def test_pace_is_applied_between_mutations(cfg, monkeypatch):
    slept = nap_recorder(monkeypatch)
    pushed = 60 * 48
    fake = FakeServices([node("a1", "Crime Two", 2016, ago(pushed - 3)), node("b1", "Rom Com", 2020, ago(pushed - 3))])
    fake.view_counts.update(aaa=1, bbb=1)

    def body(c):
        old_push(cfg, "tmdb:111", "m1", "11", minutes_ago=pushed)
        old_push(cfg, "tmdb:112", "m1", "12", minutes_ago=pushed)
        sync_push(c)
        assert slept.count(4.5) >= 2                          # between the status checks and between the removals

    run(cfg, fake, body, plex_history_pace=4.5)


def test_followup_batch_is_scheduled_while_rows_remain(cfg, monkeypatch):
    monkeypatch.setattr("movienight.seenpush.LB_BATCH", 1)
    pushed = 60 * 48
    fake = FakeServices([node("a1", "Crime Two", 2016, ago(pushed - 3)), node("b1", "Rom Com", 2020, ago(pushed - 3))])
    fake.view_counts.update(aaa=1, bbb=1)

    def body(c):
        sync = c.app.state.sync
        old_push(cfg, "tmdb:111", "m1", "11", minutes_ago=pushed)
        old_push(cfg, "tmdb:112", "m1", "12", minutes_ago=pushed)
        sync_push(c)                                          # batch of 1: one film fixed, one row still unprocessed
        assert sync._followup is not None and not sync._followup.done()
        assert [m["variables"]["input"]["id"] for m in fake.mutations()] == ["a1"]
        sync.followup_delay = 0
        sync._followup.cancel()
        sync._followup = None
        c.portal.call(lambda: _replan(sync))
        c.portal.call(lambda: sync.drain_history(followups=True))
        assert sorted(m["variables"]["input"]["id"] for m in fake.mutations()) == ["a1", "b1"]
        assert all(v for v in discover_at(cfg).values())

    run(cfg, fake, body)


async def _replan(sync):
    sync._plan_followup(None)


def test_no_followup_when_nothing_remains(cfg):
    pushed = 60 * 48
    fake = FakeServices([node("a1", "Crime Two", 2016, ago(pushed - 3))])
    fake.view_counts["aaa"] = 1

    def body(c):
        sync = c.app.state.sync
        old_push(cfg, "tmdb:111", "m1", "11", minutes_ago=pushed)
        sync_push(c)                                         # first pass done; the re-check is planned
        assert sync._followup is not None
        sync._followup.cancel()
        sync._followup = None
        sync.followup_delay = 0
        c.portal.call(lambda: _replan(sync))
        c.portal.call(lambda: sync.drain_history(followups=True))   # the re-check settles it: nothing is planned
        assert all(v for v in discover_at(cfg).values())
        with db.session(cfg.db_path) as d:
            assert d.execute("SELECT discover_at FROM seen_pushes").fetchone()[0]
        assert sync._followup is None or sync._followup.done()

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
            assert d.execute("SELECT fixed_at FROM seen_pushes").fetchone()[0]    # waits for the re-check

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


def old_push(cfg, key, server, rk, source="seen", minutes_ago=60 * 48):
    pushed = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with db.session(cfg.db_path) as d:
        d.execute("INSERT INTO seen_pushes(key,server_id,rating_key,pushed_at,source) VALUES(?,?,?,?,?)",
                  (key, server, rk, pushed, source))
        if source == "seen":          # a manual mark still in place (else the sync would unscrobble the push)
            d.execute("INSERT OR IGNORE INTO seen(key,created_at) VALUES(?,?)", (key, pushed))


def discover_at(cfg):
    with db.session(cfg.db_path) as d:
        return {r[0]: r[1] for r in d.execute("SELECT rating_key, COALESCE(discover_at, fixed_at) FROM seen_pushes")}


def test_catchup_settles_old_manual_push_once(cfg):
    fake = FakeServices([node("a1", "Crime Two", 2016, ago(0))])

    def body(c):
        old_push(cfg, "tmdb:111", "m1", "11")
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


def test_catchup_already_watched_film_still_gets_its_old_entry_removed(cfg):
    pushed = 60 * 48
    fake = FakeServices([
        node("real", "Crime Two", 2016, ago(60 * 24)),                    # a day after the push: a real play
        node("mine", "Crime Two", 2016, ago(pushed - 5)),                 # 5 min after the push
        node("ep", "Show", 2020, ago(pushed), type_="EPISODE", did="aaa"),  # never indexed, never touched
        node("other", "Other Film", 2016, ago(pushed - 1)),
    ])
    fake.view_counts["aaa"] = 1

    def body(c):
        old_push(cfg, "tmdb:111", "m1", "11", minutes_ago=pushed)
        sync_push(c)
        assert fake.discover == []
        assert [(m["operationName"], m["variables"]["input"]["id"]) for m in fake.mutations()] == \
            [("removeActivity", "mine")]
        assert discover_at(cfg)["11"]
        sync_push(c)                                                       # settled: nothing more
        assert len(fake.mutations()) == 1

    run(cfg, fake, body)


def test_catchup_entry_outside_window_is_untouched_and_row_settles(cfg):
    pushed = 60 * 48
    fake = FakeServices([node("early", "Crime Two", 2016, ago(pushed + 16)),
                         node("late", "Crime Two", 2016, ago(pushed - 16))])
    fake.view_counts["aaa"] = 1

    def body(c):
        old_push(cfg, "tmdb:111", "m1", "11", minutes_ago=pushed)
        sync_push(c)
        assert fake.mutations() == [] and discover_at(cfg)["11"]

    run(cfg, fake, body)


def test_catchup_letterboxd_redates_one_and_removes_the_rest(cfg):
    pushed = 60 * 48
    fake = FakeServices([node("e2", "Crime Two", 2016, ago(pushed - 8)), node("e1", "Crime Two", 2016, ago(pushed - 2))])
    fake.view_counts["aaa"] = 1

    def body(c):
        add_diary(cfg, 111, "Crime Two", 2016, "2023-07-01")
        old_push(cfg, "tmdb:111", "m1", "11", source="lb", minutes_ago=pushed)
        sync_push(c)
        ops = [(m["operationName"], (m["variables"].get("id") or m["variables"]["input"]["id"])) for m in fake.mutations()]
        assert sorted(ops) == [("removeActivity", "e1"), ("updateActivityDate", "e2")] or \
            sorted(ops) == [("removeActivity", "e2"), ("updateActivityDate", "e1")]
        assert len([o for o in ops if o[0] == "updateActivityDate"]) == 1
        assert discover_at(cfg)["11"]

    run(cfg, fake, body)


def test_catchup_fetches_history_once_with_pagination(cfg):
    pushed = 60 * 48
    nodes = [node(f"n{i}", "Filler", 2000, ago(60 * 24 + i), did=f"f{i}") for i in range(110)]   # newer than the push
    nodes.append(node("mine", "Crime Two", 2016, ago(pushed - 3)))                               # index 110
    nodes += [node(f"o{i}", "Old", 1990, ago(60 * 24 * 10 + i), did=f"x{i}") for i in range(300)]  # beyond since
    fake = FakeServices(nodes)
    fake.view_counts["aaa"] = 1
    fake.view_counts["bbb"] = 1

    def body(c):
        old_push(cfg, "tmdb:111", "m1", "11", minutes_ago=pushed)
        old_push(cfg, "tmdb:112", "m1", "12", minutes_ago=pushed)
        sync_push(c)
        assert [m["variables"]["input"]["id"] for m in fake.mutations()] == ["mine"]
        assert fake.pages == [None, "50", "100"]                 # once for both films; stops once past push time - 1 day
        assert all(discover_at(cfg).values())

    run(cfg, fake, body)


def test_catchup_partial_failure_leaves_only_that_row_unsettled(cfg):
    pushed = 60 * 48
    fake = FakeServices([node("a1", "Crime Two", 2016, ago(pushed - 3)), node("b1", "Rom Com", 2020, ago(pushed - 3))])
    fake.view_counts.update(aaa=1, bbb=1)
    fake.fail_ids = {"a1"}

    def body(c):
        old_push(cfg, "tmdb:111", "m1", "11", minutes_ago=pushed)
        old_push(cfg, "tmdb:112", "m1", "12", minutes_ago=pushed)
        sync_push(c)
        done = discover_at(cfg)
        assert done["11"] is None and done["12"]
        fake.fail_ids = set()
        sync_push(c)                                             # retried on the next sync
        assert discover_at(cfg)["11"]
        assert [m["variables"]["input"]["id"] for m in fake.mutations()][-1] == "a1"

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


def stamp(minutes_ago):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def row_state(cfg):
    with db.session(cfg.db_path) as d:
        return tuple(d.execute("SELECT fixed_at, discover_at FROM seen_pushes").fetchone())


def age_push(cfg, minutes):
    with db.session(cfg.db_path) as d:
        d.execute("UPDATE seen_pushes SET pushed_at=?, scrobbled_at=?", (stamp(minutes), stamp(minutes)))


def test_recheck_removes_late_entry_only_after_two_hours_and_only_once(cfg):
    fake = FakeServices([node("a1", "Crime Two", 2016, ago(0))])

    def body(c):
        mark(c, "Crime Two")                                   # first pass: entry removed, row waits
        fixed, settled = row_state(cfg)
        assert fixed and settled is None and [m["operationName"] for m in fake.mutations()] == ["removeActivity"]
        # plex.tv syncs the server scrobble to the history later: a second entry, dated at the push time
        fake.nodes.append(node("late", "Crime Two", 2016, ago(1)))
        calls = fake.history_calls
        sync_push(c)                                           # less than 2 h after the push: not re-checked
        assert fake.history_calls == calls and len(fake.mutations()) == 1 and row_state(cfg)[1] is None
        age_push(cfg, 3 * 60)
        fake.nodes[-1]["date"] = ago(3 * 60 - 1)               # ... as the history would have dated it
        fake.nodes.append(node("real", "Crime Two", 2016, ago(60)))   # a real play an hour ago: not ours
        sync_push(c)
        assert [m["variables"]["input"]["id"] for m in fake.mutations()] == ["a1", "late"]
        assert row_state(cfg)[1]                               # settled
        calls, muts = fake.history_calls, len(fake.mutations())
        sync_push(c)
        sync_push(c)                                           # settled rows are never re-processed
        assert fake.history_calls == calls and len(fake.mutations()) == muts
        assert any(n["id"] == "real" for n in fake.nodes)

    run(cfg, fake, body)


def test_recheck_applies_to_letterboxd_pushes_and_redates(cfg):
    fake = FakeServices([node("a1", "Crime Two", 2016, ago(0))])

    def body(c):
        lb_push(c)                                             # baseline
        add_diary(cfg, 111, "Crime Two", 2016, "2024-03-05")
        lb_push(c)
        assert row_state(cfg)[0] and row_state(cfg)[1] is None
        age_push(cfg, 3 * 60)
        fake.nodes.append(node("late", "Crime Two", 2016, ago(3 * 60 - 2)))
        sync_push(c)
        ops = [(m["operationName"], m["variables"].get("id")) for m in fake.mutations()]
        assert ops == [("updateActivityDate", "a1"), ("updateActivityDate", "late")] or \
            [o[0] for o in ops] == ["updateActivityDate", "removeActivity"]
        assert row_state(cfg)[1]

    run(cfg, fake, body)
