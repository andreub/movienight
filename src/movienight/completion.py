"""Completionist: sagas (TMDB collections), franchises (several collections merged) and curated trilogies the
user has started, with progress (X of N seen) and what is left: available now in the library, or missing.

Groups are built from the engine state plus the cached TMDB tables (tmdb_collections, tmdb_details), memoised on
the state like tags.py. Keys are path-safe: collection-556, franchise-spider-man, curated-cornetto.
"""
from __future__ import annotations

import functools
import json
import math
import sqlite3
from pathlib import Path

from . import library, tmdb

GROUPS_FILE = Path(__file__).parent / "data" / "completion_groups.json"
KINDS = ("collection", "franchise", "curated")
PREVIEW = 5             # unseen films drawn on a card; the rest sit behind "+N more"
COLLECTION_TTL_DAYS = 30


@functools.cache
def config() -> dict:
    """The bundled franchise and curated group definitions."""
    data = json.loads(GROUPS_FILE.read_text(encoding="utf-8"))
    return {"franchises": data.get("franchises") or [], "curated": data.get("curated") or []}


def franchise_of() -> dict[int, dict]:
    """collection id -> the franchise that absorbs it."""
    return {cid: f for f in config()["franchises"] for cid in f["collections"]}


def curated_groups() -> list[dict]:
    return config()["curated"]


def kind_of(key: str) -> str | None:
    kind = key.split("-", 1)[0]
    return kind if kind in KINDS else None


# --------------------------------------------------------------------------- sync helpers (SQL only)

def seen_tmdb_ids(conn: sqlite3.Connection) -> set[int]:
    """TMDB ids the user has seen: Letterboxd, played on the media server, or marked seen here."""
    return {r[0] for r in conn.execute(
        "SELECT tmdb_id FROM lb_films WHERE tmdb_id IS NOT NULL AND watched=1"
        " UNION SELECT m.tmdb_id FROM movies m JOIN media_items i ON i.movie_id=m.id"
        "  WHERE m.tmdb_id IS NOT NULL AND COALESCE(json_extract(i.json,'$.view_count'),0) > 0"
        " UNION SELECT CAST(substr(key,6) AS INTEGER) FROM seen WHERE key LIKE 'tmdb:%'") if r[0]}


def started_curated_ids(seen: set[int]) -> list[int]:
    """Films of the curated groups the user has started (only those need TMDB details)."""
    out: list[int] = []
    for g in curated_groups():
        ids = [f["id"] for f in g["films"]]
        if seen.intersection(ids):
            out += [i for i in ids if i not in out]
    return out


def wanted_collections(conn: sqlite3.Connection, seen: set[int]) -> list[int]:
    """Collections of seen films, plus every sibling collection of a franchise one of them belongs to."""
    cids: set[int] = set()
    for tid, cid in conn.execute("SELECT tmdb_id, json_extract(json,'$.collection.id') FROM tmdb_details"
                                 " WHERE json_type(json,'$.collection')='object'"):
        if tid in seen and cid:
            cids.add(int(cid))
    fr = franchise_of()
    for cid in list(cids):
        if cid in fr:
            cids.update(fr[cid]["collections"])
    return sorted(cids)


# --------------------------------------------------------------------------- building groups

def _seen(st) -> set[int]:
    return ({h.tmdb_id for h in st.history if h.watched and h.tmdb_id}
            | {c.tmdb_id for c in st.cands if c.watched and c.tmdb_id})


def _available(st) -> dict:
    """Unwatched library films by TMDB id (the best match when a film is there twice)."""
    out: dict = {}
    for c in st.cands:
        if c.tmdb_id and not c.watched and (c.tmdb_id not in out or c.match > out[c.tmdb_id].match):
            out[c.tmdb_id] = c
    return out


def _base_film(tid: int, title: str, year, date: str | None, poster_path, votes, rating) -> dict:
    return {"tmdb_id": tid, "title": title or "", "year": year, "release_date": date or "",
            "poster_path": poster_path, "vote_count": votes or 0, "vote_average": rating}


def _clean_name(name: str) -> str:
    name = (name or "").strip()
    return name[:-len(" Collection")] if name.endswith(" Collection") else name


def _raw_groups(conn: sqlite3.Connection, seen: set[int]) -> list[dict]:
    """{key, kind, name, films} in saga order, before the per-user state; only groups with a seen film."""
    colls = {}
    for r in conn.execute("SELECT collection_id, json FROM tmdb_collections"):
        c = json.loads(r["json"] or "{}")
        if c.get("parts"):
            colls[r["collection_id"]] = c

    def parts(c):
        return [_base_film(p["id"], p.get("title"), p.get("year"), p.get("release_date"), p.get("poster_path"),
                           p.get("vote_count"), p.get("vote_average")) for p in c["parts"]]

    groups: list[dict] = []
    absorbed: set[int] = set()
    for f in config()["franchises"]:
        films: dict[int, dict] = {}
        for cid in f["collections"]:
            if cid in colls:
                absorbed.add(cid)
                for p in parts(colls[cid]):
                    films.setdefault(p["tmdb_id"], p)
        ordered = sorted(films.values(), key=lambda p: (p["release_date"] or "9999", p["tmdb_id"]))
        groups.append({"key": f"franchise-{f['key']}", "kind": "franchise", "name": f["name"], "films": ordered})

    curated = curated_groups()
    cur_ids = [{f["id"] for f in g["films"]} for g in curated]
    if curated:
        ids = sorted(set().union(*cur_ids))
        marks = ",".join("?" * len(ids))
        det = {r[0]: json.loads(r[1] or "{}") for r in conn.execute(
            f"SELECT tmdb_id, json FROM tmdb_details WHERE tmdb_id IN ({marks})", ids)}
        for g in curated:
            films = []
            for f in g["films"]:
                d = det.get(f["id"]) or {}
                films.append(_base_film(f["id"], d.get("title") or f.get("title"), d.get("year") or f.get("year"),
                                        d.get("release_date"), d.get("poster_path"), d.get("vote_count"),
                                        d.get("vote_average")))
            groups.append({"key": f"curated-{g['key']}", "kind": "curated", "name": g["name"], "films": films})

    for cid, c in colls.items():
        if cid in absorbed:
            continue
        ids = {p["id"] for p in c["parts"]}
        if len(ids) < 2 or any(ids <= cur for cur in cur_ids):       # a curated group that matches replaces it
            continue
        groups.append({"key": f"collection-{cid}", "kind": "collection", "name": _clean_name(c.get("name")),
                       "films": parts(c)})
    return [g for g in groups if len(g["films"]) >= 2 and seen.intersection(f["tmdb_id"] for f in g["films"])]


def _relevance(f: dict) -> float:
    """Popularity x quality for films outside the library (vote count damped by log)."""
    return math.log1p(f.get("vote_count") or 0) * (f.get("vote_average") or 0)


def _decorate(g: dict, seen: set[int], avail: dict) -> dict:
    films = []
    for f in g["films"]:
        c = avail.get(f["tmdb_id"])
        state = "seen" if f["tmdb_id"] in seen else "available" if c is not None else "missing"
        out = {k: f[k] for k in ("tmdb_id", "title", "year", "vote_count", "vote_average")}
        out["state"] = state
        out["poster_url"] = tmdb.poster_url(f.get("poster_path"))
        if c is not None:
            out["movie_id"] = c.id
            out["match"] = c.match
            out["poster_url"] = out["poster_url"] or library.poster_of(c)
        films.append(out)
    n_seen = sum(f["state"] == "seen" for f in films)
    avail_films = [f for f in films if f["state"] == "available"]
    missing = [f for f in films if f["state"] == "missing"]
    # available first, in saga order (ties broken by taste); then missing by relevance
    unseen = avail_films + sorted(missing, key=lambda f: -_relevance(f))
    votes = [f["vote_average"] for f in films if f.get("vote_average")]
    nxt = avail_films[0] if avail_films else None
    return {
        "key": g["key"], "kind": g["kind"], "name": g["name"],
        "seen": n_seen, "total": len(films), "available": len(avail_films), "missing": len(missing),
        "complete": n_seen == len(films),
        "next": {k: nxt[k] for k in ("movie_id", "title", "year")} if nxt else None,
        "films_preview": unseen[:PREVIEW],
        "more": max(0, len(unseen) - PREVIEW),
        "_films": films,
        "_mean": sum(votes) / len(votes) if votes else 0.0,
    }


def _rank(g: dict) -> tuple:
    return (0 if g["available"] else 1, -g["seen"] / g["total"], g["missing"], -g["_mean"], g["name"].lower())


def build(st, conn: sqlite3.Connection) -> list[dict]:
    """Every started group, ranked; cached on the state (rebuilt whenever the state is)."""
    if "completion" in st.cache:
        return st.cache["completion"]
    seen = _seen(st)
    avail = _available(st)
    out = sorted((_decorate(g, seen, avail) for g in _raw_groups(conn, seen)), key=_rank)
    st.cache["completion"] = out
    return out


def hidden_keys(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT group_key FROM completion_hidden")}


def public(g: dict, hidden: set[str], full: bool = False) -> dict:
    out = {k: v for k, v in g.items() if not k.startswith("_")}
    out["hidden"] = g["key"] in hidden
    if full:
        out["films"] = g["_films"]
    return out


def overview(st, conn: sqlite3.Connection, kind: str | None = None, show_hidden: bool = False) -> dict:
    """Groups in progress, finished ones (shown collapsed) and, with `show_hidden`, the hidden ones."""
    groups = build(st, conn)
    hidden = hidden_keys(conn)
    visible = [g for g in groups if g["key"] not in hidden]
    counts = {k: sum(g["kind"] == k for g in visible) for k in KINDS}
    pick = (lambda g: g["kind"] == kind) if kind else (lambda g: True)
    return {
        "groups": [public(g, hidden) for g in visible if pick(g) and not g["complete"]],
        "completed": [public(g, hidden) for g in visible if pick(g) and g["complete"]],
        "hidden": [public(g, hidden) for g in groups if g["key"] in hidden and pick(g)] if show_hidden else [],
        "hidden_count": sum(g["key"] in hidden for g in groups),
        "counts": counts,
    }


def group(st, conn: sqlite3.Connection, key: str) -> dict | None:
    """One group with every film (saga order) and its state."""
    g = next((g for g in build(st, conn) if g["key"] == key), None)
    return public(g, hidden_keys(conn), full=True) if g else None
