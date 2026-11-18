"""Glue between SQLite and the pure recommender: storing media-server items, loading state, JSON views."""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import plex, recommender as rec, servers, tags as lbtags, tmdb
from .db import get_meta, set_meta
from .util import norm_title, now_iso, parse_iso

log = logging.getLogger("movienight.library")
FOREVER = "9999-12-31T00:00:00Z"


# --------------------------------------------------------------------------- storing library items
# Plex and Jellyfin items share one normalised dict (plex.parse_item / jellyfin.parse_item).

def store_server_items(conn: sqlite3.Connection, server_id: str, items: list[dict]) -> None:
    conn.execute("DELETE FROM media_items WHERE server_id=?", (server_id,))
    conn.executemany(
        "INSERT OR REPLACE INTO media_items(server_id,rating_key,title,year,tmdb_id,imdb_id,json) VALUES(?,?,?,?,?,?,?)",
        [(server_id, it["rating_key"], it["title"], it["year"], it["tmdb_id"], it["imdb_id"], json.dumps(it))
         for it in items],
    )


def assign_movies(conn: sqlite3.Connection) -> int:
    """Dedupe library items into logical movies (TMDB id, then IMDb id, then title+year)."""
    rows = conn.execute("SELECT server_id, rating_key, title, year, tmdb_id, imdb_id FROM media_items").fetchall()
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
        "UPDATE media_items SET movie_id=? WHERE server_id=? AND rating_key=?",
        [(existing[it["dkey"]], it["server_id"], it["rating_key"]) for it in items],
    )
    conn.execute("DELETE FROM movies WHERE id NOT IN (SELECT DISTINCT movie_id FROM media_items WHERE movie_id IS NOT NULL)")
    conn.execute("DELETE FROM dismissals WHERE movie_id NOT IN (SELECT id FROM movies)")
    conn.execute(
        "UPDATE media_servers SET movies=(SELECT COUNT(*) FROM media_items WHERE server_id=media_servers.id)"
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
    tags: list = field(default_factory=list)            # lb_tags rows (dicts)
    weights: dict | None = None                         # adopted (tuned) component weights, None = defaults
    cache: dict = field(default_factory=dict)           # derived data computed on demand (tags.py)
    server_label: str = "Plex"                          # the media server's name in user-facing text


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
        "release_date": det.get("release_date") or None,
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


def feedback_key(tmdb_id: int | None, dkey: str) -> str:
    """Key of a film in the votes/predictions/outcomes tables: stable across library re-syncs."""
    return f"tmdb:{tmdb_id}" if tmdb_id else dkey


def load_inputs(conn: sqlite3.Connection, media=None) -> tuple[list, list]:
    """Candidates (the media server's library) and Letterboxd history, with user state but not yet scored.

    `media` is the selected backend (servers.backend(cfg)); default Plex."""
    media = media or servers.PLEX
    details = {r["tmdb_id"]: json.loads(r["json"]) for r in conn.execute("SELECT tmdb_id, json FROM tmdb_details")}
    recs = {r["tmdb_id"]: json.loads(r["json"]) for r in conn.execute("SELECT tmdb_id, json FROM tmdb_recs")}

    slugs = {r["guid"]: r["slug"] for r in conn.execute("SELECT guid, slug FROM plex_slugs WHERE slug != ''")}

    # ---- candidates
    rows = conn.execute(
        "SELECT m.id, m.dkey, m.tmdb_id, i.server_id, i.rating_key, i.json, s.name sname, s.owned, s.base_url"
        " FROM movies m JOIN media_items i ON i.movie_id=m.id JOIN media_servers s ON s.id=i.server_id"
        " WHERE s.enabled=1 ORDER BY m.id, s.owned DESC, s.name"
    ).fetchall()
    grouped: dict[int, list] = {}
    for r in rows:
        grouped.setdefault(r["id"], []).append(r)
    cands: list[rec.Cand] = []
    plex_meta_by_tmdb: dict[int, dict] = {}
    # items this app marked played itself (seen marks pushed to the server): not the user watching there
    pushed = {(r[0], r[1]) for r in conn.execute("SELECT server_id, rating_key FROM seen_pushes")}
    for mid, rs in grouped.items():
        items = [json.loads(r["json"]) for r in rs]
        tmdb_id = rs[0]["tmdb_id"]
        meta = _merge_meta(items, details.get(tmdb_id) if tmdb_id else None)
        added = [parse_iso(it.get("added_at")) for it in items if it.get("added_at")]
        c = rec.Cand(
            id=mid, title=meta["title"], year=meta["year"], tmdb_id=tmdb_id, meta=meta, dkey=rs[0]["dkey"],
            added_at=max(added) if added else None,
            # Shared servers count too: each server reports the watched state of the account
            # whose token asked, so this is always the user's own (Jellyfin: off with
            # MOVIENIGHT_JELLYFIN_WATCHED=0, e.g. for an account shared with other people).
            plex_watched=media.count_watched and any(
                it.get("view_count", 0) > 0 and (r["server_id"], it["rating_key"]) not in pushed
                for r, it in zip(rs, items)),
            # `plex` holds the active media server's entries (Plex or Jellyfin); the name stays for the API
            plex=[{"server": r["sname"], "server_id": r["server_id"], "resolution": it.get("resolution"),
                   "url": media.web_url(r["server_id"], r["base_url"], it["rating_key"]),
                   "rating_key": it["rating_key"],
                   **({"mobile_url": u} if (s := slugs.get(it.get("guid"))) and (u := media.mobile_url(s)) else {})}
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

    votes = {r["key"]: r["vote"] for r in conn.execute("SELECT key, vote FROM votes")}
    for c in cands:
        c.vote = votes.get(feedback_key(c.tmdb_id, c.dkey))
    seen = {r["key"] for r in conn.execute("SELECT key FROM seen")}
    for c in cands:
        c.seen_local = feedback_key(c.tmdb_id, c.dkey) in seen
    return cands, history


def load_state(conn: sqlite3.Connection, media=None) -> State:
    media = media or servers.PLEX
    now = datetime.now(timezone.utc)
    cands, history = load_inputs(conn, media)
    weights = load_weights(conn)
    profile = rec.score_candidates(history, cands, now=now, weights=weights)
    store_predictions(conn, cands, profile)
    by_id = {c.id: c for c in cands}
    for h in profile.vote_hists:            # synthetic entries of voted library films ("because" lists them)
        if -h.key in by_id:
            h.poster = poster_of(by_id[-h.key])
    return State(cands=cands, by_id=by_id, history=history, profile=profile, tags=lbtags.load(conn),
                 weights=weights, server_label=media.label)


# --------------------------------------------------------------------------- feedback loop

def load_weights(conn: sqlite3.Connection) -> dict | None:
    """The adopted (tuned) component weights, or None for the defaults."""
    try:
        w = json.loads(get_meta(conn, "model_weights") or "null")
    except ValueError:
        return None
    if isinstance(w, dict) and all(isinstance(w.get(k), (int, float)) for k in rec.TUNABLE):
        return {k: float(w[k]) for k in rec.TUNABLE}
    return None


def empty_backtest() -> dict:
    d = {k: round(v, 4) for k, v in rec.DEFAULT_WEIGHTS.items()}
    return {"films": 0, "spearman": None, "top_quintile_hit_rate": None, "base_rate": None, "weights": dict(d),
            "default_weights": d, "tuned": False, "computed_at": None}


def load_backtest(conn: sqlite3.Connection) -> dict:
    try:
        bt = json.loads(get_meta(conn, "model_backtest") or "null")
    except ValueError:
        bt = None
    return bt if isinstance(bt, dict) else empty_backtest()


def refresh_model(conn: sqlite3.Connection, media=None) -> dict:
    """Run the leave-one-out backtest and tuning, and persist the result (weights only if adopted).

    CPU-bound (well under a few seconds for hundreds of films): call from a thread, never from a
    request handler.
    """
    cands, history = load_inputs(conn, media)
    bt = rec.backtest(history, cands)
    bt["computed_at"] = now_iso()
    if not bt["tuned"]:
        bt["weights"] = dict(bt["default_weights"])
    set_meta(conn, "model_weights", json.dumps(bt["weights"]) if bt["tuned"] else None)
    set_meta(conn, "model_backtest", json.dumps(bt))
    conn.commit()
    return bt


def model_summary(conn: sqlite3.Connection) -> dict:
    bt = load_backtest(conn)
    return {k: bt.get(k) for k in ("films", "spearman", "top_quintile_hit_rate", "base_rate", "tuned")}


def store_predictions(conn: sqlite3.Connection, cands: list, profile: rec.Profile) -> None:
    """Upsert the latest predicted match of every not-yet-watched library film.

    Skipped while the user has no Letterboxd history: such predictions are not about taste and
    must not later turn into outcomes when the first export is imported.
    """
    if not profile.n_watched:
        return
    now = now_iso()
    rows = [(feedback_key(c.tmdb_id, c.dkey), c.tmdb_id, norm_title(c.title), c.year, c.title, c.match, now, now)
            for c in cands if not c.watched]
    try:
        conn.executemany(
            "INSERT INTO predictions(key,tmdb_id,norm_title,year,title,match,first_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET tmdb_id=excluded.tmdb_id,"
            " norm_title=excluded.norm_title, year=excluded.year, title=excluded.title,"
            " match=excluded.match, updated_at=excluded.updated_at", rows)
        conn.commit()
    except sqlite3.Error as e:      # never break recommendations over bookkeeping
        log.warning("could not store predictions: %s", e)


def record_outcomes(conn: sqlite3.Connection) -> int:
    """Freeze outcomes for films that were predicted and have since been watched on Letterboxd.

    A watched Letterboxd film matches a stored prediction by TMDB id, else by normalised
    title + year. An outcome is created once per film, only if the film was first predicted
    no later than its watch date (so films already watched when first predicted, e.g. the
    first import, never count); later calls only refresh rating / liked / watched date, since
    the rating may arrive after the watch. Returns the number of new outcomes.
    """
    preds = conn.execute("SELECT * FROM predictions").fetchall()
    if not preds:
        return 0
    by_tmdb = {p["tmdb_id"]: p for p in preds if p["tmdb_id"]}
    by_ny = {(p["norm_title"], p["year"]): p for p in preds}
    have = {r["key"]: r for r in conn.execute("SELECT * FROM outcomes")}
    created = 0
    for row in conn.execute("SELECT * FROM lb_films WHERE watched=1").fetchall():
        p = by_tmdb.get(row["tmdb_id"]) if row["tmdb_id"] else None
        if p is None:
            p = by_ny.get((row["norm_title"], row["year"]))
        if p is None:
            continue
        old = have.get(p["key"])
        if old is not None:
            if (old["rating"], old["liked"], old["watched_date"]) != (row["rating"], row["liked"], row["watched_date"]):
                conn.execute("UPDATE outcomes SET rating=?, liked=?, watched_date=? WHERE id=?",
                             (row["rating"], row["liked"], row["watched_date"], old["id"]))
            continue
        if row["watched_date"] and row["watched_date"][:10] < p["first_at"][:10]:
            continue
        conn.execute(
            "INSERT INTO outcomes(key,lb_id,tmdb_id,title,year,predicted,rating,liked,watched_date,recorded_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (p["key"], row["id"], p["tmdb_id"] or row["tmdb_id"], p["title"], p["year"], p["match"],
             row["rating"], row["liked"], row["watched_date"], now_iso()))
        created += 1
    conn.commit()
    return created


HIT_MATCH = 70          # a prediction at or above this counts as "high"
VERDICT_MARGIN = 0.5    # "clearly" above/below the user's mean rating = half a star


def verdict(predicted: int, rating: float | None, user_mean: float | None) -> str:
    """Was the app right about this film?

    * pending: no rating yet (or no mean to compare with);
    * hit: predicted >= 70 and rated at least 0.5 above the user's mean rating;
    * miss: predicted >= 70 and rated at least 0.5 below it;
    * ok: everything else (including low predictions, which were never a recommendation).
    """
    if rating is None or user_mean is None:
        return "pending"
    if predicted >= HIT_MATCH:
        if rating >= user_mean + VERDICT_MARGIN:
            return "hit"
        if rating <= user_mean - VERDICT_MARGIN:
            return "miss"
    return "ok"


def feedback_json(conn: sqlite3.Connection) -> dict:
    mean_row = conn.execute("SELECT AVG(rating) FROM lb_films WHERE watched=1 AND rating IS NOT NULL").fetchone()[0]
    user_mean = float(mean_row) if mean_row is not None else None
    posters = {}
    outcomes = []
    for r in conn.execute("SELECT * FROM outcomes ORDER BY COALESCE(watched_date,'') DESC, id DESC"):
        poster = None
        if r["tmdb_id"]:
            if r["tmdb_id"] not in posters:
                d = conn.execute("SELECT json FROM tmdb_details WHERE tmdb_id=?", (r["tmdb_id"],)).fetchone()
                posters[r["tmdb_id"]] = tmdb.poster_url((json.loads(d[0]) if d else {}).get("poster_path"))
            poster = posters[r["tmdb_id"]]
        outcomes.append({"title": r["title"], "year": r["year"], "poster_url": poster, "predicted": r["predicted"],
                         "my_rating": r["rating"], "liked": bool(r["liked"]), "watched_date": r["watched_date"],
                         "verdict": verdict(r["predicted"], r["rating"], user_mean)})
    rated = [o["my_rating"] for o in outcomes if o["my_rating"] is not None]
    votes = {r[0]: r[1] for r in conn.execute("SELECT vote, COUNT(*) FROM votes GROUP BY vote")}
    return {
        "outcomes": outcomes,
        "summary": {
            "count": len(outcomes), "rated": len(rated),
            "hits": sum(1 for o in outcomes if o["verdict"] == "hit"),
            "misses": sum(1 for o in outcomes if o["verdict"] == "miss"),
            "mean_rating_of_picks": round(sum(rated) / len(rated), 2) if rated else None,
            "user_mean_rating": round(user_mean, 2) if user_mean is not None else None,
        },
        "votes": {"up": votes.get(1, 0), "down": votes.get(-1, 0), "love": votes.get(rec.LOVE, 0)},
        "backtest": load_backtest(conn),
    }


class Engine:
    """Caches the computed state in memory; invalidate after any import/sync."""

    def __init__(self, connect, media=None):
        self._connect = connect
        self._media = media
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
                    self._state = load_state(conn, self._media)
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
        "reasons": rec.reasons_for(c, st.profile, server=st.server_label) if with_reasons else [],
        "on_watchlist": c.on_watchlist,
        "watched": c.watched,
        "seen_here": c.seen_here,
        "my_rating": c.my_rating,
        "liked": c.liked,
        "my_vote": c.vote,
        "dismissed_until": dismissed.get(c.id),
        "added_at": c.added_at.strftime("%Y-%m-%dT%H:%M:%SZ") if c.added_at else None,
        "plex": [{k: p[k] for k in ("server", "server_id", "resolution", "url", "rating_key", "mobile_url") if k in p} for p in c.plex],
    }


def movie_detail_json(c: rec.Cand, st: State, dismissed: dict) -> dict:
    d = movie_json(c, st, dismissed)
    d["keywords"] = c.meta.get("keywords") or []
    d["because"] = rec.because(c, st.history, st.profile)
    return d


def seen_candidates(st: State, dismissed: dict, limit: int, offset: int, min_age_years: int = 3) -> tuple[list, int]:
    """Unwatched, not dismissed library films most likely to have been seen already.

    Ordered by TMDB vote count (popularity proxy), descending; only films released at least
    `min_age_years` ago (films without a known year are left out)."""
    cutoff = datetime.now(timezone.utc).year - min_age_years
    pool = [c for c in st.cands
            if not c.watched and c.id not in dismissed and c.year and c.year <= cutoff]
    pool.sort(key=lambda c: (-(c.meta.get("vote_count") or 0), c.id))
    return pool[offset:offset + limit], len(pool)


def seen_marked(conn: sqlite3.Connection, st: State, limit: int, offset: int) -> tuple[list, int]:
    """Library films with a local "seen it" mark, newest mark first (marks of films no longer in the
    library are left out). Returns (page of candidates, total)."""
    by_key = {feedback_key(c.tmdb_id, c.dkey): c for c in st.cands}
    pool = [by_key[r["key"]] for r in conn.execute("SELECT key FROM seen ORDER BY created_at DESC, rowid DESC")
            if r["key"] in by_key]
    return pool[offset:offset + limit], len(pool)


def profile_json(st: State, model: dict | None = None) -> dict:
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
            "hours": round(hours), "seen_here": sum(1 for c in st.cands if c.seen_here),
            "library_unwatched": sum(1 for c in st.cands if not c.watched),
            "watchlist_on_plex": sum(1 for c in st.cands if c.on_watchlist and not c.watched),
        },
        "rating_histogram": [{"rating": r / 2, "count": hist_counts.get(r / 2, 0)} for r in range(1, 11)],
        "genres": table("genre", 1, 20),
        "directors": table("director", 2, 20),  # one film says little about a director
        "cast": table("cast", 2, 20),
        "decades": sorted(table("decade", 1, 20, key="decade"), key=lambda r: r["decade"]),
        "keywords": table("keyword", 2, 24, with_mean=False),
        "model": model if model is not None else {k: v for k, v in empty_backtest().items()
                                                  if k in ("films", "spearman", "top_quintile_hit_rate", "base_rate", "tuned")},
        "top_rated": [{"title": h.title, "year": h.year, "my_rating": h.rating, "liked": h.liked,
                       "poster_url": h.poster} for h, _, _ in top],
        "tags": lbtags.stats(st),
    }


def set_plex_token(conn: sqlite3.Connection, token: str | None, account: str | None = None) -> None:
    set_meta(conn, "plex_token", token)
    set_meta(conn, "plex_account", account)
