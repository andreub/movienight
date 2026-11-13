import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from movienight import db, library
from movienight.app import create_app
from movienight.config import Config

FIX = Path(__file__).parent / "fixtures"


def det(tid, title, year, genres, directors=(), cast=(), keywords=(), runtime=100, va=7.0, vc=2000, lang="en"):
    return {"id": tid, "title": title, "original_title": title, "year": year, "runtime": runtime, "tagline": "",
            "overview": "o", "genres": list(genres), "directors": list(directors), "cast": list(cast),
            "keywords": list(keywords), "language": lang, "countries": ["US"], "vote_average": va,
            "vote_count": vc, "poster_path": f"/p{tid}.jpg", "backdrop_path": f"/b{tid}.jpg"}


def plex_item(key, title, year, tmdb, views=0, thumb=True, **kw):
    return {"rating_key": str(key), "title": title, "original_title": title, "year": year, "tmdb_id": tmdb,
            "imdb_id": None, "summary": "", "tagline": "", "duration_min": 100, "rating": 7.0,
            "view_count": views, "added_at": "2020-01-01T00:00:00Z", "thumb": "/t" if thumb else None,
            "art": "/a" if thumb else None, "genres": kw.get("genres", []), "directors": [], "roles": [],
            "countries": [], "resolution": "1080"}


HISTORY = [  # name, year, tmdb, rating, liked, watchlist
    ("Alpha Heist", 2015, 101, 4.5, 1, 0), ("Beta Romance", 2019, 202, 2.0, 0, 0),
    ("Gamma", 2001, 303, 3.5, 0, 0), ("Delta Noir", 2012, 404, 4.0, 0, 0),
    ("Epsilon Future", 2022, 505, None, 0, 1),
]
DETAILS = [
    det(101, "Alpha Heist", 2015, ["Crime", "Thriller"], ["Dir One"], ["Actor A", "Actor B"], ["heist", "caper"]),
    det(202, "Beta Romance", 2019, ["Romance", "Comedy"], ["Dir Two"], ["Actor C"], ["wedding"]),
    det(303, "Gamma", 2001, ["Drama"], ["Dir Three"], ["Actor D"], []),
    det(404, "Delta Noir", 2012, ["Crime", "Drama"], ["Dir One"], ["Actor A"], ["heist"]),
    det(111, "Crime Two", 2016, ["Crime", "Thriller"], ["Dir One"], ["Actor A"], ["heist"], va=7.5),
    det(112, "Rom Com", 2020, ["Romance", "Comedy"], ["Dir Two"], ["Actor C"], ["wedding"]),
    det(113, "Drama Three", 2010, ["Drama"], ["Dir Nine"], [], [], va=6.0),
    det(115, "Horror Short", 1975, ["Horror"], [], [], [], runtime=80),
    det(116, "Sci Epic", 2021, ["Science Fiction", "Mystery"], ["Dir Seven"], [], ["time travel"], runtime=160),
    det(505, "Epsilon Future", 2022, ["Science Fiction"], [], [], []),
]
LIBRARY = [(11, "Crime Two", 2016, 111, 0), (12, "Rom Com", 2020, 112, 0), (13, "Drama Three", 2010, 113, 0),
           (14, "Gamma", 2001, 303, 0), (15, "Horror Short", 1975, 115, 0), (16, "Epsilon Future", 2022, 505, 0),
           (17, "Sci Epic", 2021, 116, 0), (18, "Plex Watched", 2005, None, 3)]


def seed(db_path):
    with db.session(db_path) as c:
        for name, year, tid, rating, liked, wl in HISTORY:
            from movienight.util import norm_title
            c.execute("INSERT INTO lb_films(name,year,norm_title,tmdb_id,resolve_state,watched,rating,liked,watchlist,"
                      "watched_date) VALUES(?,?,?,?,1,?,?,?,?,?)",
                      (name, year, norm_title(name), tid, 0 if wl else 1, rating, liked, wl, "2024-01-01"))
        for d in DETAILS:
            c.execute("INSERT INTO tmdb_details(tmdb_id,json) VALUES(?,?)", (d["id"], json.dumps(d)))
        c.execute("INSERT INTO tmdb_recs(tmdb_id,json) VALUES(101,?)", (json.dumps([111, 113]),))
        c.execute("INSERT INTO plex_servers(id,name,owned,reachable,base_url,access_token) VALUES"
                  "('m1','Home',1,1,'http://plex.local:32400','SECRET')")
        library.store_server_items(c, "m1", [plex_item(k, t, y, tid, views=v) for k, t, y, tid, v in LIBRARY])
        library.assign_movies(c)


@pytest.fixture
def cfg(tmp_path):
    return Config(data_dir=tmp_path / "data", letterboxd_dir=tmp_path / "nolb", sync_on_start=False)


def no_network(request):
    raise AssertionError(f"unexpected network call: {request.url.host}")


@pytest.fixture
def client(cfg):
    app = create_app(cfg, transport=httpx.MockTransport(no_network))
    with TestClient(app) as c:
        seed(cfg.db_path)
        app.state.engine.invalidate()
        c.cfg = cfg
        yield c
