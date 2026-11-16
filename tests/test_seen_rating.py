"""Rating films on "Seen these?": the love vote, seen + vote in one action, the already-marked list.
Synthetic data only."""
from datetime import datetime, timezone

from conftest import det, plex_item, LIBRARY

from movienight import db, library, recommender as rec

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def mid(client, title):
    return next(c.id for c in client.app.state.engine.get().cands if c.title == title)


def mv(client, movie_id):
    return client.get(f"/api/movies/{movie_id}").json()


def rate(client, title, seen=True, vote=None):
    body = {"seen": seen} if vote is None else {"seen": seen, "vote": vote}
    return client.post(f"/api/movies/{mid(client, title)}/seen", json=body)


def rows(client):
    with db.session(client.cfg.db_path) as c:
        return ({r[0] for r in c.execute("SELECT key FROM seen")},
                {r[0]: r[1] for r in c.execute("SELECT key, vote FROM votes")})


def genre_aff(client, name):
    return next((r["affinity"] for r in client.get("/api/profile").json()["genres"] if r["name"] == name), None)


# --------------------------------------------------------------------------- love vote

def test_love_vote_accepted_and_stored(client):
    cid = mid(client, "Drama Three")
    assert client.post(f"/api/movies/{cid}/feedback", json={"vote": 2}).json() == {"ok": True}
    assert mv(client, cid)["my_vote"] == 2
    assert rows(client)[1] == {"tmdb:113": 2}
    assert client.get("/api/feedback").json()["votes"] == {"up": 0, "down": 0, "love": 1}
    for bad in (3, -2, "love", None):
        assert client.post(f"/api/movies/{cid}/feedback", json={"vote": bad}).status_code == 422
    assert rows(client)[1] == {"tmdb:113": 2}                  # untouched by the rejected calls


def test_love_signal_is_like_a_liked_five_star_film():
    s_up = rec.film_signal(rec.Hist(1, "a", vote=1, synthetic=True), 3.5, NOW)[0]
    s_love = rec.film_signal(rec.Hist(2, "b", vote=rec.LOVE, synthetic=True), 3.5, NOW)[0]
    s_five = rec.film_signal(rec.Hist(3, "c", rating=5.0, liked=True), 3.5, NOW)[0]
    assert s_up < s_love and 0.9 <= s_love <= 1.0 and abs(s_love - s_five) <= 0.1
    assert rec.vote_boost(rec.LOVE) >= rec.vote_boost(1) > 0 > rec.vote_boost(-1)


def _setup(vote):
    """A small library with two films sharing director/cast/keywords; the first one gets `vote`."""
    hist = [rec.Hist(i, f"H{i}", 2000 + i, 900 + i, r, False, True, NOW,
                     {"genres": [g], "directors": [f"HD{i}"], "year": 2000 + i})
            for i, (r, g) in enumerate([(4.0, "Drama"), (3.0, "Comedy"), (2.5, "Horror"), (3.5, "Drama")])]
    twin = {"genres": ["Western"], "directors": ["Twin Dir"], "cast": ["Twin A", "Twin B"],
            "keywords": ["k1", "k2", "k3"], "year": 1970}
    cands = [rec.Cand(1, "Voted", 1970, 11, dict(twin)), rec.Cand(2, "Sibling", 1971, 12, dict(twin))]
    cands += [rec.Cand(10 + j, f"Other {j}", 1990 + j, 100 + j, {"genres": [g], "directors": [f"OD{j}"], "year": 1990 + j})
              for j, g in enumerate(["Drama", "Comedy", "Horror", "Action", "Drama", "Romance", "Comedy", "Crime"])]
    cands[0].vote = vote
    cands[0].seen_local = True
    prof = rec.score_candidates(hist, cands, now=NOW)
    return {c.title: c for c in cands}, prof


def test_love_lifts_similar_films_more_than_thumb_up():
    none, _ = _setup(None)
    up, _ = _setup(1)
    love, prof = _setup(rec.LOVE)
    assert none["Sibling"].raw < up["Sibling"].raw < love["Sibling"].raw
    assert none["Sibling"].match < up["Sibling"].match < love["Sibling"].match
    assert up["Voted"].comps["vote"][0] <= love["Voted"].comps["vote"][0]
    # explanations name the loved film as loved
    texts = [r["text"] for r in rec.reasons_for(love["Sibling"], prof, limit=10)]
    assert "From Twin Dir, who directed Voted (you loved it)" in texts
    because = rec.because(love["Sibling"], [], prof)
    assert because and because[0]["title"] == "Voted" and because[0]["my_vote"] == rec.LOVE
    assert rec.because(love["Voted"], [], prof) == []          # never explains a film with itself


def test_similar_reason_for_love(client):
    client.post(f"/api/movies/{mid(client, 'Crime Two')}/feedback", json={"vote": 2})
    prof = client.app.state.engine.get().profile
    clone = rec.Cand(99, "Clone", 2020, 999, {"genres": ["Crime"], "directors": ["Dir One"]})
    clone.feats, clone.comps = rec.features(clone.meta), {}
    texts = [r["text"] for r in rec.reasons_for(clone, prof) if r["kind"] == "similar"]
    assert texts == ["Similar to Crime Two, which you loved"]


# --------------------------------------------------------------------------- seen + vote in one action

def test_seen_and_vote_in_one_action_change_and_undo(client):
    cid = mid(client, "Drama Three")
    assert rate(client, "Drama Three", True, 2).json() == {"ok": True}
    m = mv(client, cid)
    assert m["seen_here"] is True and m["my_vote"] == 2
    assert rows(client) == ({"tmdb:113"}, {"tmdb:113": 2})
    rate(client, "Drama Three", True, -1)                      # change of mind: still seen
    assert rows(client) == ({"tmdb:113"}, {"tmdb:113": -1})
    rate(client, "Drama Three", True, 0)                       # seen, no opinion
    assert rows(client) == ({"tmdb:113"}, {})
    rate(client, "Drama Three", True, 1)
    rate(client, "Drama Three", False, 0)                      # full undo
    assert rows(client) == (set(), {})
    m = mv(client, cid)
    assert m["seen_here"] is False and m["watched"] is False and m["my_vote"] is None


def test_seen_without_vote_keeps_the_vote(client):
    client.post(f"/api/movies/{mid(client, 'Rom Com')}/feedback", json={"vote": 1})
    rate(client, "Rom Com")                                    # old clients: {"seen": true} only
    assert rows(client) == ({"tmdb:112"}, {"tmdb:112": 1})
    rate(client, "Rom Com", False)
    assert rows(client) == (set(), {"tmdb:112": 1})


def test_bad_vote_with_seen_stores_nothing(client):
    for bad in (3, "up", 0.5):
        assert rate(client, "Crime Two", True, bad).status_code == 422
    assert rows(client) == (set(), {})
    assert client.post("/api/movies/9999/seen", json={"seen": True, "vote": 2}).status_code == 404


def test_love_moves_the_profile_more_than_thumb_up(client):
    base = genre_aff(client, "Drama")
    rate(client, "Drama Three", True, 1)
    up = genre_aff(client, "Drama")
    rate(client, "Drama Three", True, 2)
    love = genre_aff(client, "Drama")
    assert base < up < love
    assert mid(client, "Drama Three") not in [i["id"] for i in client.get("/api/recommendations").json()["items"]]


def test_letterboxd_rating_wins_over_love(client):
    before = genre_aff(client, "Drama")
    rate(client, "Gamma", True, 2)                             # rated 3.5 on Letterboxd
    assert genre_aff(client, "Drama") == before
    st = client.app.state.engine.get()
    gamma = st.by_id[mid(client, "Gamma")]
    assert gamma.vote == 2 and gamma.comps["vote"][0] == 0.0
    assert all(h.title != "Gamma" for h in st.profile.vote_hists)


# --------------------------------------------------------------------------- already marked

def test_seen_marked_list(client):
    assert client.get("/api/seen-marked").json() == {"items": [], "total": 0}
    rate(client, "Crime Two", True, 1)
    rate(client, "Rom Com")
    rate(client, "Drama Three", True, 2)
    with db.session(client.cfg.db_path) as c:
        for key, at in (("tmdb:111", "2026-01-03T00:00:00Z"), ("tmdb:112", "2026-01-01T00:00:00Z"),
                        ("tmdb:113", "2026-01-02T00:00:00Z")):
            c.execute("UPDATE seen SET created_at=? WHERE key=?", (at, key))
        c.execute("INSERT INTO seen(key, created_at) VALUES('tmdb:777777', '2026-02-01T00:00:00Z')")  # not in the library
    r = client.get("/api/seen-marked").json()
    assert [(i["title"], i["my_vote"], i["seen_here"]) for i in r["items"]] == [
        ("Crime Two", 1, True), ("Drama Three", 2, True), ("Rom Com", None, True)]
    assert r["total"] == 3 and all(i["reasons"] == [] for i in r["items"])
    p = client.get("/api/seen-marked", params={"limit": 1, "offset": 1}).json()
    assert [i["title"] for i in p["items"]] == ["Drama Three"] and p["total"] == 3
    rate(client, "Rom Com", True, -1)                          # rating it later keeps its place
    assert [(i["title"], i["my_vote"]) for i in client.get("/api/seen-marked").json()["items"]][-1] == ("Rom Com", -1)
    rate(client, "Crime Two", False, 0)                        # undo leaves the list
    assert [i["title"] for i in client.get("/api/seen-marked").json()["items"]] == ["Drama Three", "Rom Com"]
    assert client.get("/api/seen-marked", params={"limit": 0}).status_code == 422


def test_seen_marked_survives_library_rebuild(client):
    rate(client, "Drama Three", True, 2)
    with db.session(client.cfg.db_path) as c:
        c.execute("DELETE FROM plex_items")
        c.execute("DELETE FROM movies")
        library.store_server_items(c, "m1", [plex_item(k, t, y, tid, views=v) for k, t, y, tid, v in LIBRARY])
        library.assign_movies(c)
    client.app.state.engine.invalidate()
    [item] = client.get("/api/seen-marked").json()["items"]
    assert item["title"] == "Drama Three" and item["my_vote"] == 2


def test_because_lists_a_loved_library_film(client):
    with db.session(client.cfg.db_path) as c:                  # a second film by the director of Drama Three
        import json
        c.execute("INSERT INTO tmdb_details(tmdb_id,json) VALUES(130,?)",
                  (json.dumps(det(130, "Nine Again", 2012, ["Drama"], ["Dir Nine"])),))
        library.store_server_items(c, "m1", [plex_item(k, t, y, tid, views=v) for k, t, y, tid, v in LIBRARY]
                                   + [plex_item(30, "Nine Again", 2012, 130)])
        library.assign_movies(c)
    client.app.state.engine.invalidate()
    rate(client, "Drama Three", True, 2)
    b = mv(client, mid(client, "Nine Again"))["because"]
    hit = next(x for x in b if x["title"] == "Drama Three")
    assert hit["my_vote"] == 2 and hit["why"] == "Same director (Dir Nine)" and hit["poster_url"]
