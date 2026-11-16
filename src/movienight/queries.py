"""Filters, moods, sorting, facets and the weighted-random pick over scored candidates."""
from __future__ import annotations

import math
import random
from datetime import date
from dataclasses import dataclass, field

from .recommender import Cand
from .util import norm_title

# "Recent" mood window, counted back from today at request time.
RECENT_MONTHS = 18


def today() -> date:  # separate function so tests can patch the clock
    return date.today()


def recent_cutoff() -> date:
    t = today()
    m = t.year * 12 + t.month - 1 - RECENT_MONTHS
    y, mo = divmod(m, 12)
    mo += 1
    last = [31, 29 if y % 4 == 0 and (y % 100 or y % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][mo - 1]
    return date(y, mo, min(t.day, last))


# Moods are data: every key is optional. A film matches when
#   (it has one of `genres` OR one of `keywords` OR contains all genres of one `genre_sets` entry)
#   AND none of `not_genres` AND runtime/year bounds hold.
MOODS: list[dict] = [
    {"id": "light", "label": "Something light", "emoji": "🍿", "description": "Comedy, romance, feel-good",
     "genres": ["Comedy", "Romance", "Family", "Animation", "Music"],
     "keywords": ["feel-good", "feel good", "heartwarming"],
     "not_genres": ["Horror", "War", "Thriller", "Crime"]},
    {"id": "tense", "label": "On the edge of my seat", "emoji": "😬", "description": "Thrillers, crime, mystery",
     "genres": ["Thriller", "Crime", "Mystery"], "keywords": ["suspense", "heist", "manhunt"],
     "not_genres": ["Comedy", "Family", "Animation"]},
    {"id": "scary", "label": "Scare me", "emoji": "👻", "description": "Horror and things that go bump",
     "genres": ["Horror"], "keywords": ["slasher", "haunted house", "supernatural horror", "possession"]},
    {"id": "mind-bending", "label": "Mind-bending", "emoji": "🌀", "description": "Sci-fi puzzles and twisty stories",
     "genre_sets": [["Science Fiction", "Mystery"], ["Science Fiction", "Thriller"], ["Fantasy", "Mystery"]],
     "keywords": ["time travel", "dream", "twist ending", "parallel universe", "simulated reality",
                  "alternate reality", "psychological thriller", "mindfuck", "memory loss", "time loop"]},
    {"id": "epic", "label": "Go epic", "emoji": "⚔️", "description": "Big stories, war, adventure, history",
     "genres": ["War", "History", "Adventure", "Fantasy", "Western"], "min_runtime": 115},
    {"id": "heartfelt", "label": "Something heartfelt", "emoji": "💛", "description": "Moving dramas and human stories",
     "genre_sets": [["Drama", "Romance"], ["Drama", "Family"], ["Drama", "Music"], ["Drama", "Comedy"]],
     "keywords": ["coming of age", "friendship", "family relationships", "grief", "love"],
     "not_genres": ["Horror", "Thriller", "War"]},
    {"id": "short", "label": "Short (under 95 min)", "emoji": "⏱️", "description": "Done before you know it",
     "max_runtime": 94},
    {"id": "classic", "label": "A classic (before 1980)", "emoji": "🎞️", "description": "From the golden decades",
     "max_year": 1979},
    {"id": "recent", "label": "Something recent", "emoji": "🆕", "description": "Released in the last 18 months",
     "recent": True},
]
MOOD_BY_ID = {m["id"]: m for m in MOODS}


def moods_public() -> list[dict]:
    return [{k: m[k] for k in ("id", "label", "emoji", "description")} for m in MOODS]


def mood_matches(mood: dict, c: Cand) -> bool:
    genres = set(c.meta.get("genres") or [])
    kws = {k.lower() for k in c.meta.get("keywords") or []}
    runtime, year = c.meta.get("runtime"), c.year or c.meta.get("year")
    if mood.get("not_genres") and genres & set(mood["not_genres"]):
        return False
    if "max_runtime" in mood and not (runtime and runtime <= mood["max_runtime"]):
        return False
    if "min_runtime" in mood and not (runtime and runtime >= mood["min_runtime"]):
        return False
    if "max_year" in mood and not (year and year <= mood["max_year"]):
        return False
    if mood.get("recent") and not (year and year >= recent_cutoff().year):
        return False   # only the release year is stored: year >= year of the cutoff date
    if "min_year" in mood and not (year and year >= mood["min_year"]):
        return False
    content = ("genres" in mood) or ("keywords" in mood) or ("genre_sets" in mood)
    if not content:
        return True
    if genres & set(mood.get("genres", [])):
        return True
    if kws & {k.lower() for k in mood.get("keywords", [])}:
        return True
    return any(set(s) <= genres for s in mood.get("genre_sets", []))


@dataclass
class Filters:
    mood: str | None = None
    genres: list = field(default_factory=list)
    max_runtime: int | None = None
    min_year: int | None = None
    max_year: int | None = None
    watchlist_only: bool = False
    include_watched: bool = False
    include_dismissed: bool = False
    server: str | None = None
    q: str | None = None


def apply_filters(cands: list[Cand], f: Filters, dismissed: dict, skip_genre: bool = False) -> list[Cand]:
    mood = MOOD_BY_ID.get(f.mood) if f.mood else None
    q = norm_title(f.q) if f.q else ""
    genres = set(f.genres)
    out = []
    for c in cands:
        if not f.include_watched and c.watched:
            continue
        if not f.include_dismissed and c.id in dismissed:
            continue
        if f.watchlist_only and not c.on_watchlist:
            continue
        if f.server and not any(p["server_id"] == f.server for p in c.plex):
            continue
        year = c.year or c.meta.get("year")
        if f.min_year and not (year and year >= f.min_year):
            continue
        if f.max_year and not (year and year <= f.max_year):
            continue
        rt = c.meta.get("runtime")
        if f.max_runtime and not (rt and rt <= f.max_runtime):
            continue
        if genres and not skip_genre and not (genres & set(c.meta.get("genres") or [])):
            continue
        if q:
            if not c.nt:
                c.nt = norm_title(c.title) + " " + norm_title(c.meta.get("original_title"))
            if q not in c.nt:
                continue
        if mood and not mood_matches(mood, c):
            continue
        out.append(c)
    return out


def _rating_key(c: Cand) -> float:
    r = c.meta.get("vote_average")
    if r:
        return r
    return c.meta.get("plex_rating") or 0.0


SORTS = {
    "match": lambda c: (-c.match, -c.raw),
    "rating": lambda c: (-_rating_key(c), -c.match),
    "recent": lambda c: (-(c.added_at.timestamp() if c.added_at else 0), -c.match),
    "year": lambda c: (-(c.year or 0), -c.match),
    "runtime": lambda c: (c.meta.get("runtime") or 10**6, -c.match),
}


def sort_cands(cands: list[Cand], sort: str) -> list[Cand]:
    return sorted(cands, key=SORTS.get(sort, SORTS["match"]))


def facets(cands: list[Cand], f: Filters, dismissed: dict) -> dict:
    base = apply_filters(cands, f, dismissed, skip_genre=True)
    counts: dict[str, int] = {}
    years = []
    for c in base:
        for g in c.meta.get("genres") or []:
            counts[g] = counts.get(g, 0) + 1
        y = c.year or c.meta.get("year")
        if y:
            years.append(y)
    return {
        "genres": [{"name": n, "count": k} for n, k in sorted(counts.items(), key=lambda x: (-x[1], x[0]))],
        "min_year": min(years) if years else None,
        "max_year": max(years) if years else None,
    }


# --------------------------------------------------------------------------- pick

def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a | b else 0.0


def pick(cands: list[Cand], exclude: set[int], rng: random.Random | None = None,
         pool_size: int = 40, n_alt: int = 3) -> tuple[Cand, list[Cand]] | None:
    rng = rng or random
    pool = [c for c in sort_cands(cands, "match") if c.id not in exclude][:pool_size]
    if not pool:
        return None
    top = pool[0].match
    weights = [math.exp((c.match - top) / 5.0) for c in pool]
    chosen = rng.choices(pool, weights=weights, k=1)[0]
    rest = [c for c in pool if c.id != chosen.id]
    picked: list[Cand] = []
    cg = set(chosen.meta.get("genres") or [])
    taken_genres = [cg]
    while rest and len(picked) < n_alt:
        def utility(c: Cand) -> float:
            g = set(c.meta.get("genres") or [])
            overlap = max(_jaccard(g, t) for t in taken_genres)
            return c.match / 100.0 - 0.45 * overlap + rng.random() * 0.08
        best = max(rest, key=utility)
        picked.append(best)
        rest.remove(best)
        taken_genres.append(set(best.meta.get("genres") or []))
    return chosen, picked
