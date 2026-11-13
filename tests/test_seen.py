"""Local "Seen it" marks. Synthetic data only."""
import json
from datetime import datetime, timezone

from conftest import det, plex_item, LIBRARY

from movienight import db, library


def mid(client, title):
    return next(c.id for c in client.app.state.engine.get().cands if c.title == title)


def mv(client, movie_id):
    return client.get(f"/api/movies/{movie_id}").json()


def seen(client, title, value=True):
    return client.post(f"/api/movies/{mid(client, title)}/seen", json={"seen": value})


def rec_ids(client, **params):
    return [i["id"] for i in client.get("/api/recommendations", params={"limit": 200, **params}).json()["items"]]


def test_endpoint_and_flags(client):
    cid = mid(client, "Crime Two")
    assert client.post("/api/movies/9999/seen", json={"seen": True}).status_code == 404
    assert client.post(f"/api/movies/{cid}/seen", json={}).status_code == 422
    m = mv(client, cid)
    assert m["watched"] is False and m["seen_here"] is False
    assert seen(client, "Crime Two").json() == {"ok": True}
    m = mv(client, cid)
    assert m["watched"] is True and m["seen_here"] is True
    items = client.get("/api/recommendations", params={"include_watched": True, "limit": 200}).json()["items"]
    assert all("seen_here" in i for i in items)
    assert seen(client, "Crime Two", False).json() == {"ok": True}      # undo
    m = mv(client, cid)
    assert m["watched"] is False and m["seen_here"] is False
    seen(client, "Crime Two", False)                                      # idempotent


def test_excluded_from_recommendations_and_pick(client):
    cid = mid(client, "Crime Two")
    assert cid in rec_ids(client)
    seen(client, "Crime Two")
    assert cid not in rec_ids(client)
    assert cid in rec_ids(client, include_watched=True)
    for _ in range(30):
        assert client.get("/api/pick").json()["pick"]["id"] != cid
    seen(client, "Crime Two", False)
    assert cid in rec_ids(client)


def test_seen_here_false_when_letterboxd_watched(client):
    cid = mid(client, "Gamma")                       # logged on Letterboxd
    assert mv(client, cid)["watched"] is True and mv(client, cid)["seen_here"] is False
    seen(client, "Gamma")
    m = mv(client, cid)
    assert m["watched"] is True and m["seen_here"] is False


def test_survives_library_rebuild(client):
    seen(client, "Drama Three")                      # tmdb 113
    with db.session(client.cfg.db_path) as c:
        assert [r[0] for r in c.execute("SELECT key FROM seen")] == ["tmdb:113"]
        c.execute("DELETE FROM plex_items")
        c.execute("DELETE FROM movies")
        library.store_server_items(c, "m1", [plex_item(k, t, y, tid, views=v) for k, t, y, tid, v in LIBRARY])
        library.assign_movies(c)
        new_id = c.execute("SELECT id FROM movies WHERE tmdb_id=113").fetchone()[0]
    client.app.state.engine.invalidate()
    m = mv(client, new_id)
    assert m["watched"] is True and m["seen_here"] is True


def genre_aff(client, name):
    rows = client.get("/api/profile").json()["genres"]
    return next((r["affinity"] for r in rows if r["name"] == name), None)


def test_seen_alone_is_neutral_and_vote_still_feeds_profile(client):
    base = genre_aff(client, "Drama")
    n = client.get("/api/profile").json()["stats"]["watched"]
    seen(client, "Drama Three")
    assert genre_aff(client, "Drama") == base
    assert client.get("/api/profile").json()["stats"]["watched"] == n
    cid = mid(client, "Drama Three")
    client.post(f"/api/movies/{cid}/feedback", json={"vote": -1})
    assert genre_aff(client, "Drama") < base
    assert mv(client, cid)["my_vote"] == -1 and mv(client, cid)["seen_here"] is True
    client.post(f"/api/movies/{cid}/feedback", json={"vote": 1})
    assert genre_aff(client, "Drama") > base
    assert cid not in rec_ids(client)                # still hidden


def test_real_rating_wins(client):
    before = genre_aff(client, "Drama")
    seen(client, "Gamma")
    client.post(f"/api/movies/{mid(client, 'Gamma')}/feedback", json={"vote": -1})
    assert genre_aff(client, "Drama") == before


def test_no_outcomes_and_no_prediction_rows(client):
    client.app.state.engine.get()
    seen(client, "Crime Two")
    client.app.state.engine.get()
    fb = client.get("/api/feedback").json()
    assert fb["outcomes"] == []
    with db.session(client.cfg.db_path) as c:
        assert c.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0] == 0


def test_profile_stat(client):
    assert client.get("/api/profile").json()["stats"]["seen_here"] == 0
    seen(client, "Crime Two")
    seen(client, "Rom Com")
    seen(client, "Gamma")                            # already watched elsewhere: not counted
    assert client.get("/api/profile").json()["stats"]["seen_here"] == 2


def test_seen_candidates(client):
    this_year = datetime.now(timezone.utc).year
    with db.session(client.cfg.db_path) as c:
        # popularity: Drama Three highest, then Crime Two, Rom Com lowest; a brand-new film is too recent
        for tid, vc in ((113, 90000), (111, 50000), (112, 10)):
            d = json.loads(c.execute("SELECT json FROM tmdb_details WHERE tmdb_id=?", (tid,)).fetchone()[0])
            d["vote_count"] = vc
            c.execute("UPDATE tmdb_details SET json=? WHERE tmdb_id=?", (json.dumps(d), tid))
        newd = det(900, "Brand New", this_year - 1, ["Drama"], vc=999999)
        c.execute("INSERT INTO tmdb_details(tmdb_id,json) VALUES(900,?)", (json.dumps(newd),))
        library.store_server_items(c, "m1", [plex_item(k, t, y, tid, views=v) for k, t, y, tid, v in LIBRARY]
                                   + [plex_item(99, "Brand New", this_year - 1, 900)])
        library.assign_movies(c)
    client.app.state.engine.invalidate()
    r = client.get("/api/seen-candidates").json()
    titles = [i["title"] for i in r["items"]]
    assert "Brand New" not in titles                  # released < 3 years ago
    assert "Gamma" not in titles and "Plex Watched" not in titles          # already watched
    assert titles[:2] == ["Drama Three", "Crime Two"] and titles[-1] == "Rom Com"
    assert r["total"] == len(titles) and all("seen_here" in i for i in r["items"])
    # paging
    p = client.get("/api/seen-candidates", params={"limit": 2, "offset": 1}).json()
    assert [i["title"] for i in p["items"]] == titles[1:3] and p["total"] == r["total"]
    # marked seen and dismissed films leave the list
    seen(client, "Drama Three")
    client.post(f"/api/movies/{mid(client, 'Crime Two')}/dismiss", json={"days": 0})
    t2 = [i["title"] for i in client.get("/api/seen-candidates").json()["items"]]
    assert "Drama Three" not in t2 and "Crime Two" not in t2
    assert client.get("/api/seen-candidates", params={"limit": 0}).status_code == 422
