import random
from datetime import datetime, timedelta, timezone

from movienight import recommender as rec, queries

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def meta(genres, directors=(), cast=(), keywords=(), year=2010, runtime=100, va=7.0, vc=2000):
    return {"genres": list(genres), "directors": list(directors), "cast": list(cast), "keywords": list(keywords),
            "year": year, "runtime": runtime, "vote_average": va, "vote_count": vc, "language": "en", "countries": ["US"]}


def world():
    hist = [
        rec.Hist(1, "Drive", 2011, 1, 4.5, True, True, NOW - timedelta(days=100),
                 meta(["Crime", "Drama"], ["Nicolas Refn"], ["Ryan G"], ["heist"]), recs=[21]),
        rec.Hist(2, "Bronson", 2008, 2, 4.0, False, True, NOW - timedelta(days=200),
                 meta(["Crime", "Drama"], ["Nicolas Refn"], ["Tom H"], ["prison"])),
        rec.Hist(3, "Notting", 1999, 3, 2.0, False, True, NOW - timedelta(days=300),
                 meta(["Romance", "Comedy"], ["R Michell"], ["Hugh G"], [], 1999)),
        rec.Hist(4, "Plain Drama", 2000, 4, 3.5, False, True, NOW - timedelta(days=300), meta(["Drama"], year=2000)),
        rec.Hist(5, "Rom Two", 2003, 5, 1.5, False, True, NOW - timedelta(days=300), meta(["Romance"], year=2003)),
    ]
    cands = [
        rec.Cand(21, "Neon Demon", 2016, 21, meta(["Crime", "Drama"], ["Nicolas Refn"], ["Elle F"])),
        rec.Cand(22, "Sweet Rom", 2018, 22, meta(["Romance", "Comedy"], ["R Michell"], ["Hugh G"], year=2018)),
        rec.Cand(23, "Generic Drama", 2012, 23, meta(["Drama"], year=2012, va=6.0)),
        rec.Cand(24, "Watchlisted Horror", 2019, 24, meta(["Horror"], year=2019), on_watchlist=True),
        rec.Cand(25, "Seen it", 2011, 25, meta(["Crime"]), lb_watched=True),
        rec.Cand(26, "Fresh Crime", 2024, 26, meta(["Crime"], year=2024), added_at=NOW - timedelta(days=3)),
    ]
    for i in range(30):   # filler so percentiles are meaningful
        cands.append(rec.Cand(100 + i, f"Filler {i}", 1990 + i % 30, None,
                              meta(["Drama" if i % 2 else "Action"], year=1990 + i % 30, va=5.5 + (i % 5) / 4)))
    return hist, cands


def test_affinities_shrink_and_signs():
    hist, cands = world()
    p = rec.build_profile(hist, NOW)
    assert p.stats["genre"]["Crime"].aff > 0 > p.stats["genre"]["Romance"].aff
    # one liked 4.5 film must not beat two good ones by the same director
    assert p.stats["director"]["Nicolas Refn"].aff > p.stats["director"]["R Michell"].aff
    assert abs(p.stats["cast"]["Ryan G"].aff) < 0.5            # single film: shrunk
    assert p.stats["cast"]["Ryan G"].aff < p.stats["director"]["Nicolas Refn"].aff


def test_liked_unrated_positive_and_watched_unrated_neutral():
    h1 = rec.Hist(1, "a", liked=True)
    h2 = rec.Hist(2, "b")
    assert rec.film_signal(h1, 3.4, NOW)[0] > 0.5
    assert 0 <= rec.film_signal(h2, 3.4, NOW)[0] < 0.1
    old = rec.Hist(3, "c", rating=5, date=NOW - timedelta(days=365 * 20))
    new = rec.Hist(3, "c", rating=5, date=NOW)
    assert rec.film_signal(new, 3, NOW)[1] > rec.film_signal(old, 3, NOW)[1]


def test_ranking_match_spread_and_reasons():
    hist, cands = world()
    p = rec.score_candidates(hist, cands, now=NOW)
    by = {c.id: c for c in cands}
    assert by[21].match > by[22].match and by[21].match > by[23].match
    assert by[24].match > by[23].match                      # watchlist boost
    unwatched = sorted(c.match for c in cands if not c.watched)
    assert unwatched[-1] >= 85 and unwatched[-1] <= 99
    assert 35 <= unwatched[len(unwatched) // 2] <= 65
    assert all(1 <= c.match <= 99 for c in cands)
    texts = [r["text"] for r in rec.reasons_for(by[21], p)]
    assert any("Fans of Drive (you gave it ★★★★½) also love this" == t for t in texts)
    assert any(t == "You rated 2 films by Nicolas Refn ★★★★ on average" for t in texts)
    kinds = [r["kind"] for r in rec.reasons_for(by[24], p)]
    assert kinds[0] == "watchlist"
    assert 1 <= len(rec.reasons_for(by[100], p)) <= 4
    assert any(r["kind"] == "fresh" for r in rec.reasons_for(by[26], p))
    assert [r["text"] for r in rec.reasons_for(by[21], p)][0]


def test_idf_downweights_common_genre():
    hist, cands = world()
    for c in cands:
        c.feats = rec.features(c.meta)
    idf = rec.build_idf(hist, cands)
    assert idf["genre"]["Drama"] < idf["genre"]["Action"]


def test_because():
    hist, cands = world()
    p = rec.score_candidates(hist, cands, now=NOW)
    b = rec.because(cands[0], hist, p)
    assert b and b[0]["title"] == "Drive" and b[0]["my_rating"] == 4.5


def test_filters_moods_sort_facets():
    hist, cands = world()
    rec.score_candidates(hist, cands, now=NOW)
    f = queries.Filters()
    shown = queries.apply_filters(cands, f, {})
    assert 25 not in {c.id for c in shown}                        # watched excluded
    assert 25 in {c.id for c in queries.apply_filters(cands, queries.Filters(include_watched=True), {})}
    assert 21 not in {c.id for c in queries.apply_filters(cands, f, {21: "x"})}
    assert [c.id for c in queries.apply_filters(cands, queries.Filters(watchlist_only=True), {})] == [24]
    assert {c.id for c in queries.apply_filters(cands, queries.Filters(mood="scary"), {})} == {24}
    light = {c.id for c in queries.apply_filters(cands, queries.Filters(mood="light"), {})}
    assert 22 in light and 21 not in light
    assert [c.id for c in queries.apply_filters(cands, queries.Filters(q="neon"), {})] == [21]
    assert all(c.year >= 2018 for c in queries.apply_filters(cands, queries.Filters(min_year=2018), {}))
    srt = queries.sort_cands(shown, "year")
    assert srt[0].year == 2024
    fac = queries.facets(cands, queries.Filters(genres=["Horror"]), {})
    assert {"name": "Crime", "count": 2} in fac["genres"] and fac["max_year"] == 2024
    assert len(queries.MOODS) >= 6


def test_pick_weighted_exclude_and_diverse_alternatives():
    hist, cands = world()
    rec.score_candidates(hist, cands, now=NOW)
    shown = queries.apply_filters(cands, queries.Filters(), {})
    rng = random.Random(1)
    top = queries.sort_cands(shown, "match")[0].id
    wins = sum(queries.pick(shown, set(), rng)[0].id == top for _ in range(200))
    assert wins > 30                                  # strongly favoured, not deterministic
    ids = {queries.pick(shown, set(), rng)[0].id for _ in range(100)}
    assert len(ids) > 1
    chosen, alts = queries.pick(shown, {top}, rng)
    assert chosen.id != top and top not in {a.id for a in alts}
    assert len(alts) == 3 and chosen.id not in {a.id for a in alts}
    assert queries.pick([], set()) is None


def test_recent_mood_window_and_year_fallback(monkeypatch):
    from datetime import date
    assert queries.RECENT_MONTHS == 18
    assert queries.MOOD_BY_ID["recent"]["description"] == "Released in the last 18 months"
    ids = [m["id"] for m in queries.MOODS]
    assert ids.index("recent") == ids.index("classic") + 1
    hist, cands = world()
    rec.score_candidates(hist, cands, now=NOW)
    mood = queries.MOOD_BY_ID["recent"]
    by_id = {c.id: c for c in cands}

    def recent(today):
        monkeypatch.setattr(queries, "today", lambda: today)
        return {c.id for c in queries.apply_filters(cands, queries.Filters(mood="recent"), {})}

    # cutoff 2025-05-16 -> year >= 2025: 2024 film dropped
    assert 26 not in recent(date(2026, 11, 16))
    # cutoff 2023-12-31 (today 2025-06-30) -> 2024 kept
    assert 26 in recent(date(2025, 6, 30))
    # cutoff 2023-12-31 vs 2024-01-01: boundary on both sides
    assert queries.recent_cutoff() == date(2023, 12, 30)
    monkeypatch.setattr(queries, "today", lambda: date(2025, 7, 1))
    assert queries.recent_cutoff() == date(2024, 1, 1)
    assert 26 in recent(date(2025, 7, 1))
    monkeypatch.setattr(queries, "today", lambda: date(2026, 1, 1))
    assert queries.recent_cutoff() == date(2024, 7, 1)
    assert 26 in recent(date(2026, 1, 1))
    assert 26 not in recent(date(2027, 1, 1))     # cutoff 2025-07-01
    # year only: falls back on year, no year -> out
    by_id[26].year = None
    by_id[26].meta["year"] = 2026
    assert 26 in recent(date(2027, 1, 1))
    by_id[26].meta["year"] = None
    assert 26 not in recent(date(2027, 1, 1))
    by_id[26].year = 2026
    # combined with another filter
    monkeypatch.setattr(queries, "today", lambda: date(2026, 11, 16))
    f = queries.Filters(mood="recent", genres=["Crime"])
    assert {c.id for c in queries.apply_filters(cands, f, {})} == {26}
    f = queries.Filters(mood="recent", genres=["Horror"])
    assert queries.apply_filters(cands, f, {}) == []
    assert queries.recent_cutoff() == date(2025, 5, 16)
    monkeypatch.setattr(queries, "today", lambda: date(2026, 8, 31))
    assert queries.recent_cutoff() == date(2025, 2, 28)
