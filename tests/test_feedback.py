"""Feedback loop: thumbs, prediction outcomes, leave-one-out backtest and tuning. Synthetic data only."""
import time
from datetime import datetime, timedelta, timezone

import pytest

from movienight import db, library, letterboxd as lb, recommender as rec
from movienight.util import now_iso

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def mid(client, title):
    """Library id of a seeded film (ids are assigned by the library, not the Plex key)."""
    st = client.app.state.engine.get()
    return next(c.id for c in st.cands if c.title == title)


def mv(client, movie_id):
    return client.get(f"/api/movies/{movie_id}").json()


def genre_aff(client, name):
    rows = client.get("/api/profile").json()["genres"]
    return next((r["affinity"] for r in rows if r["name"] == name), None)


# --------------------------------------------------------------------------- A. thumbs

def test_vote_endpoint_and_my_vote(client):
    assert mv(client, mid(client, 'Crime Two'))["my_vote"] is None
    assert client.post("/api/movies/9999/feedback", json={"vote": 1}).status_code == 404
    for bad in ({"vote": 3}, {"vote": -2}, {"vote": "up"}, {"vote": 1.5}, {}):
        assert client.post(f"/api/movies/{mid(client, 'Crime Two')}/feedback", json=bad).status_code == 422
    assert client.post(f"/api/movies/{mid(client, 'Crime Two')}/feedback", json={"vote": 1}).json() == {"ok": True}
    assert mv(client, mid(client, 'Crime Two'))["my_vote"] == 1
    items = client.get("/api/recommendations").json()["items"]
    assert next(i for i in items if i["id"] == mid(client, 'Crime Two'))["my_vote"] == 1
    assert all("my_vote" in i for i in items)
    client.post(f"/api/movies/{mid(client, 'Crime Two')}/feedback", json={"vote": -1})
    assert mv(client, mid(client, 'Crime Two'))["my_vote"] == -1
    client.post(f"/api/movies/{mid(client, 'Crime Two')}/feedback", json={"vote": 0})
    assert mv(client, mid(client, 'Crime Two'))["my_vote"] is None
    assert client.get("/api/feedback").json()["votes"] == {"up": 0, "down": 0, "love": 0}


def test_vote_effect_on_ranking(client):
    base = {i["id"]: i["match"] for i in client.get("/api/recommendations").json()["items"]}
    client.post(f"/api/movies/{mid(client, 'Rom Com')}/feedback", json={"vote": -1})      # Rom Com
    client.post(f"/api/movies/{mid(client, 'Drama Three')}/feedback", json={"vote": 1})       # Drama Three
    after = {i["id"]: i["match"] for i in client.get("/api/recommendations").json()["items"]}
    assert after[mid(client, 'Rom Com')] < base[mid(client, 'Rom Com')] and after[mid(client, 'Drama Three')] > base[mid(client, 'Drama Three')]
    assert after[mid(client, 'Rom Com')] in range(1, 100)           # penalised, not removed
    assert client.get("/api/feedback").json()["votes"] == {"up": 1, "down": 1, "love": 0}


def test_vote_is_weaker_than_rating_and_ignored_when_rated(client):
    # Gamma (id 14) has a real rating: a thumb must not move the profile
    before = genre_aff(client, "Drama")
    client.post(f"/api/movies/{mid(client, 'Gamma')}/feedback", json={"vote": -1})
    assert genre_aff(client, "Drama") == before
    # Drama Three (id 13) is unrated: a thumbs-down moves the Drama affinity
    client.post(f"/api/movies/{mid(client, 'Drama Three')}/feedback", json={"vote": -1})
    assert genre_aff(client, "Drama") < before
    client.post(f"/api/movies/{mid(client, 'Drama Three')}/feedback", json={"vote": 1})
    assert genre_aff(client, "Drama") > before


def test_vote_signal_weaker_than_a_rating():
    now = NOW
    up = rec.Hist(1, "x", watched=True, vote=1, synthetic=True)
    five = rec.Hist(2, "y", rating=5.0)
    s_up, _ = rec.film_signal(up, 3.0, now)
    s_five, _ = rec.film_signal(five, 3.0, now)
    assert 0 < s_up < s_five
    assert rec.film_signal(rec.Hist(3, "z", watched=True, vote=-1, synthetic=True), 3.0, now)[0] == -s_up


def test_similar_reason_for_thumbs_up(client):
    client.post(f"/api/movies/{mid(client, 'Crime Two')}/feedback", json={"vote": 1})       # Crime Two, director Dir One
    st = client.app.state.engine.get()
    other = next(c for c in st.cands if c.title == 'Crime Two')
    prof = st.profile
    assert prof.vote_hists and prof.vote_hists[0].title == "Crime Two"
    clone = rec.Cand(99, "Clone", 2020, 999, {"genres": ["Crime"], "directors": ["Dir One"]})
    clone.feats = rec.features(clone.meta)
    clone.comps = {}
    texts = [r for r in rec.reasons_for(clone, prof) if r["kind"] == "similar"]
    assert texts and texts[0]["text"] == "Similar to Crime Two, which you gave a \U0001F44D"
    assert other.vote == 1


def test_votes_survive_resync(client):
    client.post(f"/api/movies/{mid(client, 'Drama Three')}/feedback", json={"vote": 1})       # tmdb 113
    with db.session(client.cfg.db_path) as c:
        assert [tuple(r) for r in c.execute("SELECT key, vote FROM votes")] == [("tmdb:113", 1)]
        c.execute("DELETE FROM media_items")
        c.execute("DELETE FROM movies")
        from conftest import plex_item, LIBRARY
        library.store_server_items(c, "m1", [plex_item(k, t, y, tid, views=v) for k, t, y, tid, v in LIBRARY])
        library.assign_movies(c)
        new_id = c.execute("SELECT id FROM movies WHERE tmdb_id=113").fetchone()[0]
    client.app.state.engine.invalidate()
    assert mv(client, new_id)["my_vote"] == 1


# --------------------------------------------------------------------------- B. outcomes

def feedback(client):
    return client.get("/api/feedback").json()


def test_feedback_shape_empty(client):
    client.app.state.engine.get()
    fb = feedback(client)
    assert fb["outcomes"] == []
    assert set(fb["summary"]) == {"count", "rated", "hits", "misses", "mean_rating_of_picks", "user_mean_rating"}
    assert fb["summary"]["count"] == 0 and fb["summary"]["mean_rating_of_picks"] is None
    assert fb["votes"] == {"up": 0, "down": 0, "love": 0}
    assert set(fb["backtest"]) >= {"films", "spearman", "top_quintile_hit_rate", "base_rate", "weights",
                                   "default_weights", "tuned", "computed_at"}


def watch(client, title, year, tmdb, rating, date, liked=False):
    with db.session(client.cfg.db_path) as c:
        lb.apply_rss(c, [{"title": title, "year": year, "rating": rating, "liked": liked,
                          "watched_date": date, "tmdb_id": tmdb}])


def test_outcome_lifecycle(client):
    client.app.state.engine.get()                       # stores predictions for unwatched films
    with db.session(client.cfg.db_path) as c:
        # pre-existing watches (Alpha Heist...) have no prediction: nothing to freeze
        assert c.execute("SELECT COUNT(*) FROM predictions WHERE title='Gamma'").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM predictions WHERE title='Crime Two'").fetchone()[0] == 1
        c.execute("UPDATE predictions SET match=90 WHERE title='Crime Two'")
    assert feedback(client)["outcomes"] == []
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    watch(client, "Crime Two", 2016, 111, None, today)
    fb = feedback(client)
    assert len(fb["outcomes"]) == 1
    o = fb["outcomes"][0]
    assert (o["title"], o["year"], o["predicted"], o["my_rating"], o["verdict"]) == ("Crime Two", 2016, 90, None, "pending")
    assert o["poster_url"] and o["watched_date"] == today and o["liked"] is False
    # rating arrives later: same outcome, updated; user mean = (4.5+2+3.5+4+5)/5 = 3.8 -> 5.0 is a hit
    watch(client, "Crime Two", 2016, 111, 5.0, today, liked=True)
    fb = feedback(client)
    assert len(fb["outcomes"]) == 1
    o = fb["outcomes"][0]
    assert o["my_rating"] == 5.0 and o["liked"] is True and o["verdict"] == "hit"
    assert fb["summary"] == {"count": 1, "rated": 1, "hits": 1, "misses": 0, "mean_rating_of_picks": 5.0,
                             "user_mean_rating": 3.8}
    # a low rating of a highly predicted film is a miss
    with db.session(client.cfg.db_path) as c:
        c.execute("UPDATE predictions SET match=88 WHERE title='Rom Com'")
    watch(client, "Rom Com", 2020, 112, 1.0, today)
    fb = feedback(client)
    assert [o["title"] for o in fb["outcomes"]] == ["Rom Com", "Crime Two"] or len(fb["outcomes"]) == 2
    assert {o["title"]: o["verdict"] for o in fb["outcomes"]} == {"Rom Com": "miss", "Crime Two": "hit"}


def test_no_outcome_for_watch_dated_before_prediction_or_without_history(client):
    client.app.state.engine.get()
    watch(client, "Drama Three", 2010, 113, 3.0, "2001-02-03")      # watched long before it was predicted
    assert feedback(client)["outcomes"] == []


def test_no_predictions_without_history(cfg):
    from conftest import seed
    import httpx
    from fastapi.testclient import TestClient
    from movienight.app import create_app
    app = create_app(cfg, transport=httpx.MockTransport(lambda r: None))
    with TestClient(app) as c:
        seed(cfg.db_path)
        with db.session(cfg.db_path) as conn:
            conn.execute("DELETE FROM lb_films")                       # plex only, no Letterboxd history
        app.state.engine.invalidate()
        assert c.get("/api/recommendations").status_code == 200
        with db.session(cfg.db_path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0] == 0


def test_verdict_rule():
    v = library.verdict
    assert v(90, None, 3.4) == "pending"
    assert v(90, 4.0, 3.4) == "hit" and v(90, 3.4, 3.4) == "ok" and v(90, 2.5, 3.4) == "miss"
    assert v(50, 5.0, 3.4) == "ok" and v(50, 1.0, 3.4) == "ok"


# --------------------------------------------------------------------------- C. backtest

def synth(n=80, taste="genre"):
    hist, cands = [], []
    for i in range(n):
        alpha = i % 2 == 0
        year = 1970 + (i % 2) * 40 if taste == "decade" else 1990 + (i % 25)
        genre = ["G%d" % (i % 3)] if taste == "decade" else (["Alpha"] if alpha else ["Beta"]) + ["X%d" % (i % 5)]
        meta = {"genres": genre, "directors": [f"Dir{i}"], "cast": [f"Act{i % 7}"], "keywords": [f"kw{i % 3}"],
                "year": year, "vote_average": 7.0, "vote_count": 1000, "language": "en", "countries": ["US"]}
        rating = (4.0 + (i % 3) * 0.5) if alpha else (1.0 + (i % 3) * 0.5)
        hist.append(rec.Hist(i, f"Film {i}", year, 1000 + i, rating, False, True, NOW - timedelta(days=i), meta))
    for j in range(20):
        cands.append(rec.Cand(500 + j, f"Cand {j}", 2000, None, {"genres": ["Alpha" if j % 2 else "Beta"]}))
    return hist, cands


def test_spearman_and_quintile_helpers():
    assert rec.spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert rec.spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)
    assert rec.spearman([1, 1, 1], [1, 2, 3]) is None
    preds = list(range(10))
    ratings = [1, 1, 1, 1, 1, 1, 1, 5, 5, 5]                 # 70th percentile = 5
    hit, base = rec.top_quintile(preds, ratings)
    assert (hit, base) == (1.0, 0.3)


def test_backtest_positive_and_leave_one_out():
    hist, cands = synth(80, "genre")
    solo = hist[0]
    solo.meta = {**solo.meta, "directors": ["Only Once"]}     # director appears in exactly one film
    prof = rec.build_profile(hist, NOW)
    assert prof.stats["director"]["Only Once"].aff > 0        # the full profile would credit the film itself
    rows = {h.key: v for h, v in rec.loo_rows(hist, cands, NOW)}
    assert len(rows) == 80
    assert rows[solo.key]["director"] == 0.0                  # ...but leave-one-out gives no credit
    bt = rec.backtest(hist, cands, NOW)
    assert bt["films"] == 80
    assert bt["spearman"] > 0.5
    assert bt["top_quintile_hit_rate"] > bt["base_rate"]
    assert set(bt["weights"]) == set(bt["default_weights"]) == set(rec.TUNABLE)


def test_loo_excludes_self_from_collaborative_evidence():
    hist, cands = synth(12, "genre")
    for h in hist[:6]:
        h.rating, h.recs = 5.0, [hist[7].tmdb_id, h.tmdb_id]     # loved films recommend film 7 (and themselves)
    rows = {h.key: v for h, v in rec.loo_rows(hist, cands, NOW)}
    assert rows[7]["similar"] > 0
    assert rows[0]["similar"] == 0.0                               # recs naming itself do not count


def test_backtest_degrades_without_metadata():
    hist = [rec.Hist(i, f"F{i}", 2000, None, 3.0, False, True, None, {"year": 2000}) for i in range(10)]
    bt = rec.backtest(hist, [], NOW)
    assert bt["films"] == 0 and bt["tuned"] is False and bt["spearman"] is None
    assert bt["weights"] == bt["default_weights"]
    assert rec.backtest([], [], NOW)["films"] == 0


def test_tuning_thresholds(monkeypatch):
    hist, cands = synth(80, "decade")
    rows = rec.loo_rows(hist, cands, NOW)
    t = rec.tune(rows)
    assert t["adopted"] and t["cv_gain"] >= rec.MIN_CV_GAIN
    assert t["weights"] != rec.DEFAULT_WEIGHTS
    for k, w in t["weights"].items():                                # bounded to 0.5x - 2x
        assert 0.5 * rec.DEFAULT_WEIGHTS[k] - 1e-9 <= w <= 2 * rec.DEFAULT_WEIGHTS[k] + 1e-9
    bt = rec.backtest(hist, cands, NOW)
    assert bt["tuned"] and bt["spearman"] >= bt["default_spearman"]
    # too few films: never adopted
    small = rec.tune(rows[:rec.MIN_BACKTEST_FILMS - 1])
    assert not small["adopted"] and small["weights"] == rec.DEFAULT_WEIGHTS
    # improvement below the threshold: never adopted
    monkeypatch.setattr(rec, "MIN_CV_GAIN", 5.0)
    again = rec.tune(rows)
    assert not again["adopted"] and again["weights"] == rec.DEFAULT_WEIGHTS


def test_backtest_speed_300_films():
    hist, cands = synth(300, "decade")
    t0 = time.perf_counter()
    bt = rec.backtest(hist, cands, NOW)
    assert bt["films"] == 300
    assert time.perf_counter() - t0 < 5.0


def test_refresh_model_persists_and_scoring_uses_weights(client):
    with db.session(client.cfg.db_path) as c:
        bt = library.refresh_model(c)
        assert bt["films"] == 4 and bt["tuned"] is False and bt["computed_at"]
        assert library.load_weights(c) is None
    p = client.get("/api/profile").json()["model"]
    assert set(p) == {"films", "spearman", "top_quintile_hit_rate", "base_rate", "tuned"} and p["films"] == 4
    assert client.get("/api/feedback").json()["backtest"]["films"] == 4
    # an adopted weight set is used by the scorer
    zero = {**rec.DEFAULT_WEIGHTS, "genre": 0.0}
    with db.session(client.cfg.db_path) as c:
        import json
        db.set_meta(c, "model_weights", json.dumps(zero))
    client.app.state.engine.invalidate()
    st = client.app.state.engine.get()
    assert all(c.comps["genre"][0] == 0.0 for c in st.cands)
