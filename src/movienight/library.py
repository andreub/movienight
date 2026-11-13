"""Glue between SQLite and the pure recommender: storing Plex items, loading state, JSON views."""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import plex, recommender as rec, tmdb
from .db import set_meta
from .util import norm_title, now_iso, parse_iso

FOREVER = "9999-12-31T00:00:00Z"


# --------------------------------------------------------------------------- storing Plex items

def store_server_items(conn: sqlite3.Connection, server_id: str, items: list[dict]) -> None:
    conn.execute("DELETE FROM plex_items WHERE server_id=?", (server_id,))
    conn.executemany(
        "INSERT OR REPLACE INTO plex_items(server_id,rating_key,title,year,tmdb_id,imdb_id,json) VALUES(?,?,?,?,?,?,?)",
        [(server_id, it["rating_key"], it["title"], it["year"], it["tmdb_id"], it["imdb_id"], json.dumps(it))
         for it in items],
    )


def assign_movies(conn: sqlite3.Connection) -> int:
    """Dedupe Plex items into logical movies (TMDB id, then IMDb id, then title+year)."""
    rows = conn.execute("SELECT server_id, rating_key, title, year, tmdb_id, imdb_id FROM plex_items").fetchall()
    items = [dict(r) for r in rows]
    imdb_map, title_map = plex.key_maps(items)
    existing = {r["dkey"]: r["id"] for r in conn.execute("SELECT id, dkey FROM movies")}
    keys = set()
    for it in items:
        it["dkey"] = plex.movie_key(it, imdb_map, title_map)
        keys.add(it["dkey"])
    for dkey in keys:
        if dkey not in existing:
            tmdb_id = int(dkey[5:]) if dkey.startswith("tmdb:") else None
            cur = conn.execute("INSERT INTO movies(dkey, tmdb_id) VALUES(?,?)", (dkey, tmdb_id))
            existing[dkey] = cur.lastrowid
    conn.executemany(
        "UPDATE plex_items SET movie_id=? WHERE server_id=? AND rating_key=?",
        [(existing[it["dkey"]], it["server_id"], it["rating_key"]) for it in items],
    )
    conn.execute("DELETE FROM movies WHERE id NOT IN (SELECT DISTINCT movie_id FROM plex_items WHERE movie_id IS NOT NULL)")
    conn.execute("DELETE FROM dismissals WHERE movie_id NOT IN (SELECT id FROM movies)")
    conn.execute(
        "UPDATE plex_servers SET movies=(SELECT COUNT(*) FROM plex_items WHERE server_id=plex_servers.id)"
    )
    return len(keys)


def active_dismissals(conn: sqlite3.Connection) -> dict[int, str]:
    now = now_iso()
    out = {}
    for r in conn.execute("SELECT movie_id, until FROM dismissals"):
        if r["until"] is None:
            out[r["movie_id"]] = FOREVER
        elif r["until"] > now:
            out[r["movie_id"]] = r["until"]
    return out


# --------------------------------------------------------------------------- state

@dataclass
class State:
    cands: list = field(default_factory=list)
    by_id: dict = field(default_factory=dict)
    history: list = field(default_factory=list)
    profile: rec.Profile = field(default_factory=rec.Profile)
    keywords_by_id: dict = field(default_factory=dict)


def _merge_meta(items: list[dict], det: dict | None) -> dict:
    first = items[0]

    def pick(key):
        for it in items:
            if it.get(key):
                return it[key]
        return None

    det = det or {}
    rating = next((it["rating"] for it in items if it.get("rating") is not None), None)
    return {
        "title": first["title"],
        "original_title": det.get("original_title") or first.get("original_title") or first["title"],
        "year": first.get("year") or det.get("year"),
        "runtime": det.get("runtime") or pick("duration_min"),
        "tagline": det.get("tagline") or pick("tagline") or "",
        "overview": det.get("overview") or pick("summary") or "",
        "genres": det.get("genres") or pick("genres") or [],
        "directors": det.get("directors") or pick("directors") or [],
        "cast": det.get("cast") or pick("roles") or [],
        "keywords": det.get("keywords") or [],
        "language": det.get("language"),
        "countries": det.get("countries") or pick("countries") or [],
        "vote_average": det.get("vote_average"),
        "vote_count": det.get("vote_count"),
        "poster_path": det.get("poster_path"),
        "backdrop_path": det.get("backdrop_path"),
        "plex_rating": rating,
        "has_thumb": bool(pick("thumb")),
        "has_art": bool(pick("art")),
    }


def load_state(conn: sqlite3.Connection) -> State:
    now = datetime.now(timezone.utc)
    details = {r["tmdb_id"]: json.loads(r["json"]) for r in conn.execute("SELECT tmdb_id, json FROM tmdb_details")}
    recs = {r["tmdb_id"]: json.loads(r["json"]) for r in conn.execute("SELECT tmdb_id, json FROM tmdb_recs")}

    # ---- candidates
    rows = conn.execute(
        "SELECT m.id, m.tmdb_id, i.server_id, i.rating_key, i.json, s.name sname, s.owned"
        " FROM movies m JOIN plex_items i ON i.movie_id=m.id JOIN plex_servers s ON s.id=i.server_id"
        " WHERE s.enabled=1 ORDER BY m.id, s.owned DESC, s.name"
    ).fetchall()
    grouped: dict[int, list] = {}
    for r in rows:
        grouped.setdefault(r["id"], []).append(r)
    cands: list[rec.Cand] = []
    plex_meta_by_tmdb: dict[int, dict] = {}
    for mid, rs in grouped.items():
        items = [json.loads(r["json"]) for r in rs]
        tmdb_id = rs[0]["tmdb_id"]
        meta = _merge_meta(items, details.get(tmdb_id) if tmdb_id else None)
        added = [parse_iso(it.get("added_at")) for it in items if it.get("added_at")]
        c = rec.Cand(
            id=mid, title=meta["title"], year=meta["year"], tmdb_id=tmdb_id, meta=meta,
            added_at=max(added) if added else None,
            plex_watched=any(r["owned"] and it.get("view_count", 0) > 0 for r, it in zip(rs, items)),
            plex=[{"server": r["sname"], "server_id": r["server_id"], "resolution": it.get("resolution"),
                   "url": plex.web_url(r["server_id"], it["rating_key"]), "rating_key": it["rating_key"]}
                  for r, it in zip(rs, items)],
        )
        cands.append(c)
        if tmdb_id:
            plex_meta_by_tmdb[tmdb_id] = meta

    # ---- history
    lb = conn.execute("SELECT * FROM lb_films").fetchall()
    history: list[rec.Hist] = []
    by_tmdb, by_ny = {}, {}
    for row in lb:
        meta = details.get(row["tmdb_id"]) if row["tmdb_id"] else None
        if meta is None and row["tmdb_id"]:
            meta = plex_meta_by_tmdb.get(row["tmdb_id"])
        if meta is not None and not meta.get("year"):
            meta = {**meta, "year": row["year"]}
        if meta is None and row["year"]:
            meta = {"year": row["year"]}
        h = rec.Hist(
            key=row["id"], title=row["name"], year=row["year"], tmdb_id=row["tmdb_id"], rating=row["rating"],
            liked=bool(row["liked"]), watched=bool(row["watched"]), date=parse_iso(row["watched_date"]),
            meta=meta, recs=recs.get(row["tmdb_id"], []) if row["tmdb_id"] else [],
            poster=tmdb.poster_url((details.get(row["tmdb_id"]) or {}).get("poster_path")) if row["tmdb_id"] else None,
            runtime=(meta or {}).get("runtime"),
        )
        history.append(h)
        if row["tmdb_id"]:
            by_tmdb[row["tmdb_id"]] = row
        by_ny.setdefault((row["norm_title"], row["year"]), []).append(row)

    for c in cands:
        row = by_tmdb.get(c.tmdb_id) if c.tmdb_id else None
        if row is None:
            names = {norm_title(c.title), norm_title(c.meta.get("original_title"))} - {""}
            y = c.year
            for nm in names:
                for dy in (0, -1, 1):
                    for r in by_ny.get((nm, (y + dy) if y else None), []):
                        # a row already resolved to another TMDB id is a different film
                        if r["tmdb_id"] and c.tmdb_id and r["tmdb_id"] != c.tmdb_id:
                            continue
                        row = row or r
        if row is not None:
            c.lb_watched = bool(row["watched"])
            c.my_rating = row["rating"]
            c.liked = bool(row["liked"])
            c.on_watchlist = bool(row["watchlist"]) and not row["watched"]

    profile = rec.score_candidates(history, cands, now=now)
    st = State(cands=cands, by_id={c.id: c for c in cands}, history=history, profile=profile)
    return st


class Engine:
    """Caches the computed state in memory; invalidate after any import/sync."""

    def __init__(self, connect):
        self._connect = connect
        self._lock = threading.Lock()
        self._state: State | None = None

    def invalidate(self) -> None:
        with self._lock:
            self._state = None

    def get(self) -> State:
        with self._lock:
            if self._state is None:
                conn = self._connect()
                try:
                    self._state = load_state(conn)
                finally:
                    conn.close()
            return self._state


# --------------------------------------------------------------------------- JSON views

def poster_of(c: rec.Cand) -> str | None:
    return tmdb.poster_url(c.meta.get("poster_path")) or (f"/api/image/{c.id}/poster" if c.meta.get("has_thumb") else None)


def backdrop_of(c: rec.Cand) -> str | None:
    if c.meta.get("backdrop_path"):
        return tmdb.backdrop_url(c.meta["backdrop_path"])
    return f"/api/image/{c.id}/art" if c.meta.get("has_art") else None


def movie_json(c: rec.Cand, st: State, dismissed: dict, with_reasons: bool = True) -> dict:
    m = c.meta
    return {
        "id": c.id,
        "title": c.title,
        "original_title": m.get("original_title") or c.title,
        "year": c.year,
        "runtime": m.get("runtime"),
        "tagline": m.get("tagline") or "",
        "overview": m.get("overview") or "",
        "genres": m.get("genres") or [],
        "directors": m.get("directors") or [],
        "cast": (m.get("cast") or [])[:12],
        "language": m.get("language"),
        "countries": m.get("countries") or [],
        "poster_url": poster_of(c),
        "backdrop_url": backdrop_of(c),
        "tmdb_rating": m.get("vote_average"),
        "tmdb_votes": m.get("vote_count"),
        "tmdb_id": c.tmdb_id,
        "letterboxd_url": f"https://letterboxd.com/tmdb/{c.tmdb_id}" if c.tmdb_id else None,
        "match": c.match,
        "reasons": rec.reasons_for(c, st.profile) if with_reasons else [],
        "on_watchlist": c.on_watchlist,
        "watched": c.watched,
        "my_rating": c.my_rating,
        "liked": c.liked,
        "dismissed_until": dismissed.get(c.id),
        "added_at": c.added_at.strftime("%Y-%m-%dT%H:%M:%SZ") if c.added_at else None,
        "plex": [{k: p[k] for k in ("server", "server_id", "resolution", "url")} for p in c.plex],
    }


def movie_detail_json(c: rec.Cand, st: State, dismissed: dict) -> dict:
    d = movie_json(c, st, dismissed)
    d["keywords"] = c.meta.get("keywords") or []
    d["because"] = rec.because(c, st.history, st.profile)
    return d


def profile_json(st: State) -> dict:
    p = st.profile
    watched = [h for h, _, _ in p.signals]
    hours = sum((h.runtime or 0) for h in watched) / 60.0
    hist_counts = {}
    for h in watched:
        if h.rating is not None:
            hist_counts[h.rating] = hist_counts.get(h.rating, 0) + 1

    def table(t, min_count=1, limit=20, with_mean=True, key="name"):
        rows = []
        for name, s in p.stats[t].items():
            if s.n < min_count:
                continue
            row = {key: int(name) if key == "decade" else name, "count": s.n}
            if with_mean:
                mr = rec.mean_rating_of(s)
                row["mean_rating"] = round(mr, 2) if mr is not None else None
            row["affinity"] = round(max(-1.0, min(1.0, s.aff)), 3)
            rows.append(row)
        rows.sort(key=lambda r: (-r["affinity"], -r["count"]))
        return rows[:limit]

    top = sorted(
        (x for x in p.signals if x[0].rating is not None or x[0].liked),
        key=lambda x: (x[0].rating or 0, x[0].liked, x[1]), reverse=True,
    )[:12]
    mean = round(p.mean_rating, 2) if p.n_rated else None
    return {
        "stats": {
            "watched": p.n_watched, "rated": p.n_rated, "mean_rating": mean, "liked": p.n_liked,
            "hours": round(hours), "library_unwatched": sum(1 for c in st.cands if not c.watched),
            "watchlist_on_plex": sum(1 for c in st.cands if c.on_watchlist and not c.watched),
        },
        "rating_histogram": [{"rating": r / 2, "count": hist_counts.get(r / 2, 0)} for r in range(1, 11)],
        "genres": table("genre", 1, 20),
        "directors": table("director", 1, 20),
        "cast": table("cast", 2, 20),
        "decades": sorted(table("decade", 1, 20, key="decade"), key=lambda r: r["decade"]),
        "keywords": table("keyword", 2, 24, with_mean=False),
        "top_rated": [{"title": h.title, "year": h.year, "my_rating": h.rating, "liked": h.liked,
                       "poster_url": h.poster} for h, _, _ in top],
    }


def set_plex_token(conn: sqlite3.Connection, token: str | None, account: str | None = None) -> None:
    set_meta(conn, "plex_token", token)
    set_meta(conn, "plex_account", account)
