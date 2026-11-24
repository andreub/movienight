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

Then filmographies, which have no order of their own:
- **director**: someone who directed DIRECTOR_MIN_SEEN films the user has seen; **actor**: someone billed in the
  top 5 of ACTOR_MIN_SEEN seen films. Their credits (cached /person/{id}?append_to_response=movie_credits) skip
  documentaries, TV films, unreleased and little-voted films, unless seen. Someone eligible as both is a director.

Then canonical lists, bundled with the app (data/lists/*.json, built by scripts/build_list.py; no TMDB call at
runtime): **list**, e.g. every Best Picture winner or the Sight and Sound 2022 poll, once one of their films is seen.
They have no order of their own either.

Groups are built from the engine state plus the cached TMDB tables (tmdb_collections, tmdb_lookups,
tmdb_details, tmdb_person_credits), memoised on the state like tags.py. Keys are path-safe: collection-556,
franchise-spider-man, series-357318, director-525, actor-6193, list-oscars-best-picture.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
from datetime import date, datetime, timezone
from functools import cache
from pathlib import Path

from . import library, tmdb

SAGA_KINDS = ("collection", "franchise", "series")
LIST_KINDS = ("list",)
PEOPLE_KINDS = ("director", "actor")
KINDS = SAGA_KINDS + LIST_KINDS + PEOPLE_KINDS
LISTS_DIR = Path(__file__).parent / "data" / "lists"
COLLECTION_TTL_DAYS = 30
LOOKUP_TTL_DAYS = 30
PERSON_TTL_DAYS = 30

# filmographies: a director with this many films seen, an actor with this many seen in the top 5 billing (cast_ids)
DIRECTOR_MIN_SEEN = 3
ACTOR_MIN_SEEN = 4
PEOPLE_MAX = 40            # of each kind, most films seen first: at most 2 x 40 credit lookups per sync
PERSON_MIN_VOTES = 100     # obscure credits (shorts, festival films, cameos) stay out, unless seen
PERSON_SKIP_GENRES = {99, 10770}    # documentaries and TV films, unless seen
ACTOR_MAX_FILMS = 30       # an actor's group: every seen film, then the most relevant unseen ones up to this
ACTOR_MIN_UNSEEN = 5       # ...but never fewer unseen than this when the seen films alone reach the cap
PEOPLE_TASTE = 0.25        # genre affinity's weight on a missing film's relevance (x (1 + w * affinity))

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
# cached parts stored before they carried backdrop_path (refetched once; the refetch stores the key, even as null)
STALE_PARTS_SQL = ("(COALESCE(json_array_length(json,'$.parts'),0) > 0"
                   " AND json_type(json,'$.parts[0].backdrop_path') IS NULL)")

# ranking: score = W_RECENT*recency + W_TASTE*taste + W_LIBRARY*library + W_PROGRESS*progress, each part in [0, 1]
# (taste in [-1, 1])
W_RECENT, W_TASTE, W_LIBRARY, W_PROGRESS = 3.0, 1.5, 0.75, 0.5
RECENT_FULL_DAYS = 548       # ~18 months: an unseen film released since then earns the full recency bonus
RECENT_ZERO_DAYS = 1826      # ~5 years: the bonus has faded linearly to 0
TASTE_PRIOR = 2.0            # shrinkage: the user's overall mean counts as this many extra rated films
TASTE_SPAN = 1.0             # a shrunk mean this many stars above the overall mean is the full taste bonus
VOTE_AS_RATING = {-1: -1.5, 1: 0.5, 2: 1.0}     # an app vote (no rating), as stars relative to the overall mean


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


def wanted_people(conn: sqlite3.Connection, seen: set[int]) -> dict[str, list[tuple[int, int]]]:
    """{"director": [(person id, films seen)], "actor": [...]}: directors with DIRECTOR_MIN_SEEN films seen and actors
    with ACTOR_MIN_SEEN seen in the top 5 billing, most seen first, PEOPLE_MAX of each. Counted from the stored
    details of seen films (director_ids, cast_ids)."""
    counts: dict[str, dict[int, int]] = {"director": {}, "actor": {}}
    for tid, dirs, cast in conn.execute(
            "SELECT tmdb_id, json_extract(json,'$.director_ids'), json_extract(json,'$.cast_ids') FROM tmdb_details"
            " WHERE json_type(json,'$.director_ids')='array' OR json_type(json,'$.cast_ids')='array'"):
        if tid not in seen:
            continue
        for kind, ids in (("director", dirs), ("actor", cast)):
            for pid in set(json.loads(ids or "[]")):
                if isinstance(pid, int) and pid > 0:
                    counts[kind][pid] = counts[kind].get(pid, 0) + 1
    out = {}
    for kind, least in (("director", DIRECTOR_MIN_SEEN), ("actor", ACTOR_MIN_SEEN)):
        ranked = sorted(((pid, n) for pid, n in counts[kind].items() if n >= least), key=lambda x: (-x[1], x[0]))
        out[kind] = ranked[:PEOPLE_MAX]
    return out


def wanted_person_ids(conn: sqlite3.Connection, seen: set[int]) -> list[int]:
    """Every eligible director and actor (one lookup per person, even when both)."""
    w = wanted_people(conn, seen)
    return sorted({pid for kind in PEOPLE_KINDS for pid, _ in w[kind]})


# --------------------------------------------------------------------------- building groups

def _seen(st) -> set[int]:
    return ({h.tmdb_id for h in st.history if h.watched and h.tmdb_id}
            | {c.tmdb_id for c in st.cands if c.watched and c.tmdb_id})


def _tmdb_keyed(conn: sqlite3.Connection, table: str, col: str = "1") -> dict[int, int]:
    """tmdb id -> `col` of the `tmdb:<id>` rows of the seen or votes table (marks on films outside the library)."""
    out = {}
    for key, val in conn.execute(f"SELECT key, {col} FROM {table} WHERE key LIKE 'tmdb:%'"):
        tid = key[5:]
        if tid.isdigit():
            out[int(tid)] = val
    return out


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
            "backdrop_path": p.get("backdrop_path"),
            "vote_count": p.get("vote_count") or 0, "vote_average": p.get("vote_average"),
            "genre_ids": p.get("genre_ids") or []}


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
                           "films": [_film(p) for p in c["parts"]], "backdrop_path": c.get("backdrop_path")})

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


def load_lists(folder: Path = LISTS_DIR) -> tuple[dict, ...]:
    """The canonical lists in `folder` (one JSON file each: key, name, ordered, films), by file name."""
    out = []
    for path in sorted(folder.glob("*.json")):
        d = json.loads(path.read_text(encoding="utf-8"))
        if d.get("key") and d.get("films"):
            out.append(d)
    return tuple(out)


@cache
def bundled_lists() -> tuple[dict, ...]:
    """The lists shipped with the app, read once per process."""
    return load_lists()


def _list_groups(seen: set[int]) -> list[dict]:
    """Canonical lists with at least one film seen. Unordered: presented like filmographies (unseen first)."""
    groups = []
    for lst in bundled_lists():
        films = [_film({**f, "id": f["tmdb_id"]}) | ({"rank": f["rank"]} if f.get("rank") is not None else {})
                 for f in lst["films"] if f.get("tmdb_id")]
        if seen.intersection(f["tmdb_id"] for f in films):
            groups.append({"key": f"list-{slug(lst['key'])}", "kind": "list", "name": lst.get("name") or lst["key"],
                           "films": films, "ordered": bool(lst.get("ordered"))})
    return groups


def keep_credit(p: dict, seen: set[int], today: str) -> bool:
    """A filmography credit worth listing: released, not a documentary or a TV film, voted on by PERSON_MIN_VOTES
    people. A film the user has seen always stays (it counts for progress)."""
    if p.get("id") in seen:
        return True
    when = p.get("release_date") or ""
    if not when or when > today:
        return False
    if PERSON_SKIP_GENRES.intersection(p.get("genre_ids") or []):
        return False
    return (p.get("vote_count") or 0) >= PERSON_MIN_VOTES


def _relevance(f: dict) -> float:
    """Popularity x quality for films outside the library (vote count damped by log)."""
    return math.log1p(f.get("vote_count") or 0) * (f.get("vote_average") or 0)


def _appeal(f: dict, aff=None) -> float:
    """Relevance, nudged by the user's taste for the film's genres (`aff`: genre id list -> affinity in [-1, 1])."""
    genres = f.get("genre_ids") or f.get("_genres") or []
    return _relevance(f) * (1 + PEOPLE_TASTE * (aff(genres) if aff else 0.0))


def ranked_unseen(films: list[dict], aff=None) -> list[dict]:
    """For groups without an order: films in the library first (best match first), then missing ones by relevance
    plus taste."""
    return (sorted((f for f in films if f["state"] == "available"), key=lambda f: -(f.get("match") or 0))
            + sorted((f for f in films if f["state"] == "missing"), key=lambda f: -_appeal(f, aff)))


def cap_films(films: list[dict], seen: set[int], avail: set[int], aff=None, cap: int = ACTOR_MAX_FILMS) -> list[dict]:
    """At most `cap` films: every seen one (they count for progress), then the most relevant unseen ones, those in
    the library first. When the seen films alone fill the cap, ACTOR_MIN_UNSEEN unseen ones still come along."""
    seen_films = [f for f in films if f["tmdb_id"] in seen]
    unseen = sorted((f for f in films if f["tmdb_id"] not in seen),
                    key=lambda f: (f["tmdb_id"] not in avail, -_appeal(f, aff), f["tmdb_id"]))
    room = cap - len(seen_films) if len(seen_films) < cap else ACTOR_MIN_UNSEEN
    return seen_films + unseen[:room]


def _people_groups(conn: sqlite3.Connection, seen: set[int], avail: set[int] = frozenset(), aff=None,
                   today: str | None = None) -> list[dict]:
    """Director and actor filmographies, from the cached credits of the eligible people (wanted_people). Someone
    eligible as both only gets the director group. Kept when at least one film is seen and one is not."""
    today = today or _today().isoformat()
    want = wanted_people(conn, seen)
    directors = {pid for pid, _ in want["director"]}
    ids = directors | {pid for pid, _ in want["actor"]}
    rows = {}
    if ids:
        marks = ",".join("?" * len(ids))
        rows = {r[0]: r for r in conn.execute(
            f"SELECT person_id, name, profile_path, json FROM tmdb_person_credits WHERE person_id IN ({marks})",
            sorted(ids))}
    groups = []
    for kind, field in (("director", "directed"), ("actor", "acted")):
        for pid, _ in want[kind]:
            if (kind == "actor" and pid in directors) or pid not in rows:
                continue
            _, name, profile, js = rows[pid]
            p = json.loads(js or "{}")
            films: dict[int, dict] = {}
            for c in p.get(field) or []:
                if c.get("id") and keep_credit(c, seen, today):
                    films.setdefault(c["id"], _film(c))
            films = list(films.values())
            if kind == "actor":
                films = cap_films(films, seen, avail, aff)
            n_seen = sum(f["tmdb_id"] in seen for f in films)
            if not n_seen or n_seen == len(films):
                continue
            groups.append({"key": f"{kind}-{pid}", "kind": kind, "name": name or p.get("name") or "",
                           "films": _by_date(films), "ordered": False, "person_id": pid,
                           "profile_path": profile or p.get("profile_path")})
    return groups


def _decorate(g: dict, seen: set[int], avail: dict, backdrops: dict[int, str] | None = None, aff=None) -> dict:
    backdrops = backdrops or {}
    ordered = g.get("ordered", True)
    films = []
    for f in g["films"]:
        c = avail.get(f["tmdb_id"])
        state = "seen" if f["tmdb_id"] in seen else "available" if c is not None else "missing"
        out = {k: f[k] for k in ("tmdb_id", "title", "year", "vote_count", "vote_average")}
        out["release_date"] = f.get("release_date") or ""
        out["state"] = state
        out["poster_url"] = tmdb.poster_url(f.get("poster_path"))
        out["backdrop_url"] = tmdb.backdrop_url(f.get("backdrop_path") or backdrops.get(f["tmdb_id"]))
        if c is not None:
            out["movie_id"] = c.id
            out["match"] = c.match
            out["poster_url"] = out["poster_url"] or library.poster_of(c)
            out["backdrop_url"] = out["backdrop_url"] or library.backdrop_of(c)
        if f.get("rank") is not None:
            out["rank"] = f["rank"]          # a ranked poll's place (ties share it)
        out["_genres"] = f.get("genre_ids") or []
        films.append(out)
    if not ordered:      # filmographies and lists: unseen first (library, then missing by relevance), seen ones last, newest first
        films = ranked_unseen(films, aff) + sorted((f for f in films if f["state"] == "seen"),
                                                   key=lambda f: (f["release_date"] or "", f["tmdb_id"]), reverse=True)
    for f in films:
        f.pop("_genres", None)
    n_seen = sum(f["state"] == "seen" for f in films)
    avail_films = [f for f in films if f["state"] == "available"]
    votes = [f["vote_average"] for f in films if f.get("vote_average")]
    nxt = avail_films[0] if avail_films else None
    # the film to watch next: the earliest unseen one (in the library first); its backdrop is the TV hero's
    unseen = [f for f in films if f["state"] != "seen"]
    up = nxt or (unseen[0] if unseen else None)
    hero = next((f["backdrop_url"] for f in ([up] if up else []) + unseen + films if f["backdrop_url"]),
                None) or tmdb.backdrop_url(g.get("backdrop_path"))
    out = {
        "key": g["key"], "kind": g["kind"], "name": g["name"],
        "seen": n_seen, "total": len(films), "available": len(avail_films),
        "missing": sum(f["state"] == "missing" for f in films),
        "complete": n_seen == len(films),
        # sagas, franchises and series are ordered: `films` is the whole group in release order, seen films
        # included, so the client can show where the gaps fall. Filmographies (directors, actors) are not: unseen
        # films first, ranked (library, then missing by relevance and taste), then the seen ones, newest first.
        "ordered": ordered,
        "next": {k: nxt[k] for k in ("movie_id", "title", "year")} if nxt else None,
        "backdrop_url": hero,
        "films": films,
        "_mean": sum(votes) / len(votes) if votes else 0.0,
    }
    if g.get("person_id"):
        out["person_id"] = g["person_id"]
        out["profile_url"] = profile_url(g.get("profile_path"))
    return out


def profile_url(path: str | None) -> str | None:
    return tmdb.profile_url(path)


# --------------------------------------------------------------------------- ranking

def _today() -> date:
    return datetime.now(timezone.utc).date()


def _date(s: str | None) -> date | None:
    try:
        return date.fromisoformat((s or "")[:10])
    except ValueError:
        return None


def user_ratings(st, votes: dict[int, int] | None = None) -> tuple[dict[int, tuple[float, bool]], float]:
    """tmdb id -> (stars, is a real rating), and the user's overall mean rating. A Letterboxd rating wins; a film
    with only an app vote gets the overall mean shifted by VOTE_AS_RATING. `votes` (tmdb id -> vote) adds the
    verdicts on films outside the library."""
    mu = st.profile.mean_rating or 3.0
    out: dict[int, tuple[float, bool]] = {}
    for h in st.history:
        if h.tmdb_id and h.rating is not None and not h.synthetic:
            out[h.tmdb_id] = (float(h.rating), True)
    for c in st.cands:
        if not c.tmdb_id or c.tmdb_id in out:
            continue
        if c.my_rating is not None:
            out[c.tmdb_id] = (float(c.my_rating), True)
        elif c.vote in VOTE_AS_RATING:
            out[c.tmdb_id] = (min(5.0, max(0.5, mu + VOTE_AS_RATING[c.vote])), False)
    for tid, v in (votes or {}).items():
        if tid not in out and v in VOTE_AS_RATING:
            out[tid] = (min(5.0, max(0.5, mu + VOTE_AS_RATING[v])), False)
    return out, mu


def recency(films: list[dict], today: date) -> tuple[float, dict | None]:
    """1 when an unseen, released film came out in the last ~18 months, fading linearly to 0 at ~5 years; and
    that film (the newest one)."""
    best, newest, film = 0.0, None, None
    for f in films:
        d = _date(f.get("release_date"))
        if f["state"] == "seen" or d is None or d > today:
            continue
        age = (today - d).days
        r = 1.0 if age <= RECENT_FULL_DAYS else max(0.0, (RECENT_ZERO_DAYS - age) / (RECENT_ZERO_DAYS - RECENT_FULL_DAYS))
        if r > 0 and (newest is None or d > newest):
            best, newest, film = r, d, f
    return best, film


def taste(stars: list[float], mu: float) -> float:
    """The user's ratings of a group's seen films against their overall mean `mu`, in [-1, 1]. The mean is shrunk
    towards `mu` as if TASTE_PRIOR more films had been rated at `mu`, so one rated film says little."""
    if not stars:
        return 0.0
    shrunk = (sum(stars) + TASTE_PRIOR * mu) / (len(stars) + TASTE_PRIOR)
    return max(-1.0, min(1.0, (shrunk - mu) / TASTE_SPAN))


def _stars(x: float) -> str:
    return "★" + f"{x:.1f}".rstrip("0").rstrip(".")


def score(g: dict, ratings: dict[int, tuple[float, bool]], mu: float, today: date) -> tuple[float, str | None]:
    """The group's rank score and a short "why" for the card. Recency weighs most, then taste, then a film in the
    library, then the share seen."""
    rec, recent = recency(g["films"], today)
    rated = [ratings[f["tmdb_id"]] for f in g["films"] if f["state"] == "seen" and f["tmdb_id"] in ratings]
    t = taste([r for r, _ in rated], mu)
    lib = 1.0 if g["available"] else 0.0
    progress = g["seen"] / g["total"] if g["total"] else 0.0
    total = W_RECENT * rec + W_TASTE * t + W_LIBRARY * lib + W_PROGRESS * progress
    real = [r for r, is_real in rated if is_real]
    if recent is not None:
        fresh = (today - _date(recent["release_date"])).days <= RECENT_FULL_DAYS
        reason = f"{'New' if fresh else 'Recent'} · {recent['year'] or recent['release_date'][:4]}"
    elif t >= 0.25:
        reason = (f"You rated {_stars(sum(real) / len(real))}" if real
                  else "You liked their films" if g.get("kind") in PEOPLE_KINDS else "You liked these")
    elif lib:
        reason = "In your library"
    elif progress >= 0.5:
        reason = "Almost done"
    else:
        reason = None
    return round(total, 3), reason


def genre_affinity(st):
    """genre ids -> the user's mean affinity for those genres (recommender profile), clamped to [-1, 1]."""
    stats = (getattr(st.profile, "stats", None) or {}).get("genre") or {}

    def aff(ids) -> float:
        vals = [max(-1.0, min(1.0, stats[n].aff)) for n in (tmdb.GENRES.get(i) for i in ids) if n in stats]
        return sum(vals) / len(vals) if vals else 0.0
    return aff


def build(st, conn: sqlite3.Connection) -> list[dict]:
    """Every started group, ranked by score (best first); cached on the state (rebuilt whenever the state is).
    Sagas (collections, franchises, series) come first, then canonical lists, then filmographies (directors,
    actors), each block by score."""
    if "completion" in st.cache:
        return st.cache["completion"]
    seen = _seen(st) | set(_tmdb_keyed(conn, "seen"))      # + films marked seen here that are not in the library
    avail = _available(st)
    aff = genre_affinity(st)
    raw = _raw_groups(conn, seen) + _list_groups(seen) + _people_groups(conn, seen, set(avail), aff)
    need = {f["tmdb_id"] for g in raw for f in g["films"] if not f.get("backdrop_path")}
    backdrops = {r[0]: r[1] for r in conn.execute(
        "SELECT tmdb_id, json_extract(json,'$.backdrop_path') FROM tmdb_details"
        " WHERE json_extract(json,'$.backdrop_path') IS NOT NULL") if r[0] in need} if need else {}
    ratings, mu = user_ratings(st, _tmdb_keyed(conn, "votes", "vote"))
    today = _today()
    out = []
    for g in raw:
        d = _decorate(g, seen, avail, backdrops, aff)
        d["score"], d["reason"] = score(d, ratings, mu, today)
        out.append(d)
    out.sort(key=lambda g: (block(g["kind"]), -g["score"], g["name"].lower()))
    st.cache["completion"] = out
    return out


def block(kind: str) -> int:
    """Where a kind's groups go in the "All" view: sagas, then lists, then people."""
    return 2 if kind in PEOPLE_KINDS else 1 if kind in LIST_KINDS else 0


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
