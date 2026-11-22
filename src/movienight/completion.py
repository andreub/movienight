"""Completionist: the series the user has started, discovered from the database (nothing hand-written), with
progress (X of N seen) and every film in release order: seen, available now in the library, or missing.

Three signals, in this order:
- **collection**: a TMDB collection (saga) one of the seen films belongs to.
- **franchise**: collections merged because one's stem ("Spider-Man", from "Spider-Man (MCU) Collection") names
  the others and most of their films, and they are comparably popular. Siblings are found with a cached
  /search/collection on the stem. Venom, cameo films and the 1970s TV films stay out.
- **series**: a TMDB keyword on a seen film whose name ends in a series word ("cornetto trilogy"), expanded
  through a cached /discover/movie?with_keywords. Generic ones ("trilogy", "family saga") and keywords on many
  films (themes, not series) are skipped; obscure entries (shorts, fan films) are dropped by vote count.

Groups are built from the engine state plus the cached TMDB tables (tmdb_collections, tmdb_lookups,
tmdb_details), memoised on the state like tags.py. Keys are path-safe: collection-556, franchise-spider-man,
series-357318.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3

from . import library, tmdb

KINDS = ("collection", "franchise", "series")
COLLECTION_TTL_DAYS = 30
LOOKUP_TTL_DAYS = 30

# series keywords: the last word names a series; at least one other word must be specific
SERIES_WORDS = {"trilogy", "duology", "dilogy", "tetralogy", "quadrilogy", "pentalogy", "hexalogy", "saga"}
GENERIC_WORDS = {"the", "a", "an", "of", "film", "films", "movie", "movies", "book", "novel", "tv", "family", "epic",
                 "space", "fantasy", "crime", "sequel", "prequel", "anthology", "generational", "vampire"}
SERIES_MAX_TOTAL = 20      # a keyword on more films than this is a theme, not a series
SERIES_MIN_VOTES = 20      # drops shorts and fan films tagged with a series keyword (unless seen)
# franchises: the stem must be on this share of each collection's titles, and the smaller collection's most
# voted film must reach this share of the bigger one's (keeps animated spin-offs and TV films out)
STEM_TITLE_SHARE = 2 / 3
FRANCHISE_MIN_POPULARITY = 0.15


def kind_of(key: str) -> str | None:
    kind = key.split("-", 1)[0]
    return kind if kind in KINDS else None


# --------------------------------------------------------------------------- names

def _clean_name(name: str) -> str:
    name = (name or "").strip()
    return name[:-len(" Collection")] if name.endswith(" Collection") else name


def stem(name: str) -> str:
    """The franchise stem of a collection name: "Spider-Man (MCU) Collection" -> "Spider-Man",
    "Spider-Man: Spider-Verse Collection" -> "Spider-Man", "The Dark Knight Collection" -> "Dark Knight"."""
    n = _clean_name(name)
    n = re.sub(r"\s*\([^)]*\)\s*$", "", n).split(":")[0].strip()
    n = re.sub(r"^(the|a|an)\s+", "", n, flags=re.I)
    return n.strip(" .\u2026-")


def distinctive(s: str) -> bool:
    """Specific enough to search and merge on: two words or more, or one long, hyphenated or numbered word.
    "Before" (the Before trilogy) and "Matrix" are not; "Spider-Man", "Godzilla" and "Toy Story" are."""
    words = s.split()
    if len(words) >= 2:
        return True
    return len(words) == 1 and ("-" in s or len(s) >= 7 or any(ch.isdigit() for ch in s))


def _has(text: str, phrase: str) -> bool:
    return bool(re.search(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", text or "", re.I))


def is_series_keyword(name: str) -> bool:
    words = re.findall(r"[\w'&.-]+", (name or "").lower())
    return len(words) >= 2 and words[-1] in SERIES_WORDS and any(w not in GENERIC_WORDS for w in words[:-1])


def series_name(name: str) -> str:
    return " ".join(w[:1].upper() + w[1:] for w in (name or "").split())


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-") or "group"


# --------------------------------------------------------------------------- sync helpers (SQL only)

def seen_tmdb_ids(conn: sqlite3.Connection) -> set[int]:
    """TMDB ids the user has seen: Letterboxd, played on the media server, or marked seen here."""
    return {r[0] for r in conn.execute(
        "SELECT tmdb_id FROM lb_films WHERE tmdb_id IS NOT NULL AND watched=1"
        " UNION SELECT m.tmdb_id FROM movies m JOIN media_items i ON i.movie_id=m.id"
        "  WHERE m.tmdb_id IS NOT NULL AND COALESCE(json_extract(i.json,'$.view_count'),0) > 0"
        " UNION SELECT CAST(substr(key,6) AS INTEGER) FROM seen WHERE key LIKE 'tmdb:%'") if r[0]}


def _seen_collections(conn: sqlite3.Connection, seen: set[int]) -> dict[int, str]:
    """collection id -> name, for the collections of seen films."""
    out: dict[int, str] = {}
    for tid, cid, name in conn.execute(
            "SELECT tmdb_id, json_extract(json,'$.collection.id'), json_extract(json,'$.collection.name')"
            " FROM tmdb_details WHERE json_type(json,'$.collection')='object'"):
        if tid in seen and cid:
            out[int(cid)] = name or ""
    return out


def wanted_searches(conn: sqlite3.Connection, seen: set[int]) -> list[str]:
    """Distinctive stems of the seen films' collections, each searched once for franchise siblings. A hyphenated
    word inside a longer stem is searched too ("Amazing Spider-Man" -> "Spider-Man" finds Raimi's trilogy)."""
    out: set[str] = set()
    for s in map(stem, _seen_collections(conn, seen).values()):
        if distinctive(s):
            out.add(s)
        words = s.split()
        if len(words) > 1:
            out.update(w for w in words if "-" in w.strip("-") and len(w) >= 5)
    return sorted(out, key=str.lower)


def search_key(s: str) -> str:
    return f"collsearch:{s.lower()}"


def wanted_collections(conn: sqlite3.Connection, seen: set[int]) -> list[int]:
    """Collections of seen films, plus the search hits whose name carries a seen collection's stem."""
    cids = set(_seen_collections(conn, seen))
    for s in wanted_searches(conn, seen):
        row = conn.execute("SELECT json FROM tmdb_lookups WHERE key=?", (search_key(s),)).fetchone()
        for hit in json.loads(row[0]) if row else []:
            if hit.get("id") and _has(hit.get("name") or "", s):
                cids.add(int(hit["id"]))
    return sorted(cids)


def wanted_keywords(conn: sqlite3.Connection, seen: set[int]) -> list[tuple[int, str]]:
    """(keyword id, name) of the series keywords on seen films."""
    out: dict[int, str] = {}
    for tid, js in conn.execute("SELECT tmdb_id, json FROM tmdb_details WHERE json_type(json,'$.keyword_ids')='array'"):
        if tid not in seen:
            continue
        d = json.loads(js or "{}")
        for kid, name in zip(d.get("keyword_ids") or [], d.get("keywords") or []):
            if kid and is_series_keyword(name):
                out[int(kid)] = name
    return sorted(out.items())


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


def _film(p: dict) -> dict:
    return {"tmdb_id": p["id"], "title": p.get("title") or "", "year": p.get("year"),
            "release_date": p.get("release_date") or "", "poster_path": p.get("poster_path"),
            "vote_count": p.get("vote_count") or 0, "vote_average": p.get("vote_average")}


def _by_date(films) -> list[dict]:
    return sorted(films, key=lambda f: (f["release_date"] or "9999", f["tmdb_id"]))


def _title_share(c: dict, phrase: str) -> float:
    return sum(_has(p.get("title") or "", phrase) for p in c["parts"]) / len(c["parts"])


def _max_votes(c: dict) -> int:
    return max((p.get("vote_count") or 0) for p in c["parts"])


def _link(a: dict, b: dict) -> str | None:
    """The stem that ties collection `a` to `b`, if any: a's distinctive stem names b and most films of both,
    and neither is a far less popular offshoot of the other."""
    s = stem(a.get("name") or "")
    if not distinctive(s) or not _has(b.get("name") or "", s):
        return None
    if _title_share(a, s) < STEM_TITLE_SHARE or _title_share(b, s) < STEM_TITLE_SHARE:
        return None
    va, vb = _max_votes(a), _max_votes(b)
    if min(va, vb) < FRANCHISE_MIN_POPULARITY * max(va, vb, 1):
        return None
    return s


def franchises(colls: dict[int, dict]) -> list[dict]:
    """Linked collections: [{name, collections}] with two collections or more, named by their shortest stem."""
    parent = {cid: cid for cid in colls}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    stems: dict[int, list[str]] = {}
    ids = sorted(colls)
    for a in ids:
        for b in ids:
            if a != b and (s := _link(colls[a], colls[b])):
                ra, rb = find(a), find(b)
                if ra != rb:
                    parent[max(ra, rb)] = min(ra, rb)
                stems.setdefault(a, []).append(s)
    comps: dict[int, list[int]] = {}
    for cid in ids:
        comps.setdefault(find(cid), []).append(cid)
    out = []
    for members in comps.values():
        if len(members) >= 2:
            names = [s for m in members for s in stems.get(m, [])]
            out.append({"name": min(names, key=lambda s: (len(s), s)), "collections": members})
    return out


def _raw_groups(conn: sqlite3.Connection, seen: set[int]) -> list[dict]:
    """{key, kind, name, films} in release order, before the per-user state; only groups with a seen film."""
    colls = {}
    for r in conn.execute("SELECT collection_id, json FROM tmdb_collections"):
        c = json.loads(r["json"] or "{}")
        if c.get("parts"):
            colls[r["collection_id"]] = c

    groups: list[dict] = []
    absorbed: set[int] = set()
    used_keys: set[str] = set()
    for f in franchises(colls):
        films: dict[int, dict] = {}
        for cid in f["collections"]:
            absorbed.add(cid)
            for p in colls[cid]["parts"]:
                films.setdefault(p["id"], _film(p))
        key = f"franchise-{slug(f['name'])}"
        if key in used_keys:
            key = f"{key}-{min(f['collections'])}"
        used_keys.add(key)
        groups.append({"key": key, "kind": "franchise", "name": f["name"], "films": _by_date(films.values())})
    for cid, c in colls.items():
        if cid not in absorbed:
            groups.append({"key": f"collection-{cid}", "kind": "collection", "name": _clean_name(c.get("name")),
                           "films": [_film(p) for p in c["parts"]]})

    # series keywords: skipped when a saga already holds those films; a saga they contain gives way to them
    series: list[dict] = []
    for key, js in conn.execute("SELECT key, json FROM tmdb_lookups WHERE key LIKE 'keyword:%' ORDER BY key"):
        k = json.loads(js or "{}")
        if not k.get("parts") or (k.get("total") or 0) > SERIES_MAX_TOTAL:
            continue
        films = [_film(p) for p in k["parts"] if (p.get("vote_count") or 0) >= SERIES_MIN_VOTES or p["id"] in seen]
        if len(films) >= 2:
            series.append({"key": f"series-{key.split(':', 1)[1]}", "kind": "series",
                           "name": series_name(k.get("name") or ""), "films": _by_date(films)})
    sets = [{f["tmdb_id"] for f in g["films"]} for g in groups]
    kept: list[set[int]] = []
    for g in series:
        ids = {f["tmdb_id"] for f in g["films"]}
        if any(ids <= other for other in sets + kept):
            continue
        kept.append(ids)
        groups.append(g)
    groups = [g for g in groups if g["kind"] != "collection"
              or not any({f["tmdb_id"] for f in g["films"]} < k for k in kept)]
    return [g for g in groups if len(g["films"]) >= 2 and seen.intersection(f["tmdb_id"] for f in g["films"])]


def _relevance(f: dict) -> float:
    """Popularity x quality for films outside the library (vote count damped by log)."""
    return math.log1p(f.get("vote_count") or 0) * (f.get("vote_average") or 0)


def ranked_unseen(films: list[dict]) -> list[dict]:
    """For groups without an order: films in the library first (group order), then missing ones by relevance."""
    return ([f for f in films if f["state"] == "available"]
            + sorted((f for f in films if f["state"] == "missing"), key=lambda f: -_relevance(f)))


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
    votes = [f["vote_average"] for f in films if f.get("vote_average")]
    nxt = avail_films[0] if avail_films else None
    return {
        "key": g["key"], "kind": g["kind"], "name": g["name"],
        "seen": n_seen, "total": len(films), "available": len(avail_films),
        "missing": sum(f["state"] == "missing" for f in films),
        "complete": n_seen == len(films),
        # sagas, franchises and series are ordered: `films` is the whole group in release order, seen films
        # included, so the client can show where the gaps fall. Unordered groups list unseen films first, ranked.
        "ordered": True,
        "next": {k: nxt[k] for k in ("movie_id", "title", "year")} if nxt else None,
        "films": films,
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


def public(g: dict, hidden: set[str]) -> dict:
    out = {k: v for k, v in g.items() if not k.startswith("_")}
    out["hidden"] = g["key"] in hidden
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
    """One group with every film (release order) and its state."""
    g = next((g for g in build(st, conn) if g["key"] == key), None)
    return public(g, hidden_keys(conn)) if g else None
