"""Taste profile, candidate scoring and human-readable reasons.

Everything here is a pure function over plain data (see `Hist` and `Cand`), so it is easy to
test and does not touch the database or the network.

Model in short
--------------
* Every film the user logged yields a signal in [-1, 1]: the rating centred on the user's own
  mean (scaled), plus a bonus for likes, with mild recency weighting.
* Each feature (genre, director, cast member, keyword, decade, language, country) gets an
  affinity: the weighted mean signal of the films that have it, shrunk towards 0 with a prior
  so that a feature seen once cannot dominate.
* A candidate's score is a weighted sum of: feature affinities (idf-weighted so that "Drama"
  does not swamp rarer features), a collaborative signal (TMDB recommendations of loved
  films), a quality prior (Bayesian average), a watchlist boost and a small freshness boost.
* Raw scores are mapped to a 0-100 `match` by blending a logistic and the percentile rank.
"""
from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .util import stars

FEATURE_TYPES = ("genre", "director", "cast", "keyword", "decade", "language", "country")

# Bayesian shrinkage prior per feature type (higher = needs more evidence)
SHRINK = {"genre": 3.0, "director": 1.5, "cast": 2.0, "keyword": 3.0, "decade": 4.0, "language": 4.0, "country": 4.0}
# weight and gain of each feature type inside the candidate score
WEIGHT = {"genre": 0.30, "director": 0.22, "cast": 0.10, "keyword": 0.18, "decade": 0.05, "language": 0.03, "country": 0.03}
GAIN = {"genre": 2.0, "director": 1.0, "cast": 1.0, "keyword": 2.0, "decade": 2.0, "language": 1.5, "country": 1.5}
W_COLLAB = 0.32
W_QUALITY = 0.12
W_WATCHLIST = 0.30
W_FRESH = 0.06
FRESH_DAYS = 45
CAST_TOP = 8
LOVED = 0.25            # signal above which a film counts as "loved" for collaborative reasoning
MIN_REASON = 0.012


@dataclass
class Hist:
    """A film from the user's Letterboxd history."""
    key: int
    title: str
    year: int | None = None
    tmdb_id: int | None = None
    rating: float | None = None
    liked: bool = False
    watched: bool = True
    date: datetime | None = None
    meta: dict | None = None            # genres/directors/cast/keywords/... (TMDB or Plex)
    recs: list = field(default_factory=list)
    poster: str | None = None
    runtime: int | None = None


@dataclass
class Cand:
    """A movie in the Plex library."""
    id: int
    title: str
    year: int | None = None
    tmdb_id: int | None = None
    meta: dict = field(default_factory=dict)
    added_at: datetime | None = None
    plex_watched: bool = False
    plex: list = field(default_factory=list)      # [{"server_id", "server", ...}]
    nt: str = ""                                    # normalised title (search)
    # user state, filled by the loader
    lb_watched: bool = False
    my_rating: float | None = None
    liked: bool = False
    on_watchlist: bool = False
    # computed
    feats: dict = field(default_factory=dict)
    raw: float = 0.0
    match: int = 0
    comps: dict = field(default_factory=dict)

    @property
    def watched(self) -> bool:
        return self.plex_watched or self.lb_watched


@dataclass
class FeatStat:
    w: float = 0.0          # sum of recency weights
    ssum: float = 0.0       # sum of weight * signal
    n: int = 0
    rn: int = 0             # rated films
    rsum: float = 0.0
    best: tuple | None = None   # (signal, title, rating, liked)
    aff: float = 0.0


@dataclass
class Profile:
    mean_rating: float = 3.0
    n_watched: int = 0
    n_rated: int = 0
    n_liked: int = 0
    signals: list = field(default_factory=list)        # (Hist, signal, weight)
    stats: dict = field(default_factory=lambda: {t: {} for t in FEATURE_TYPES})
    rank: dict = field(default_factory=dict)            # type -> feature -> rank by affinity (0 = best)


# --------------------------------------------------------------------------- features

def features(meta: dict | None) -> dict:
    meta = meta or {}
    year = meta.get("year")
    return {
        "genre": list(meta.get("genres") or []),
        "director": list(meta.get("directors") or [])[:3],
        "cast": list(meta.get("cast") or [])[:CAST_TOP],
        "keyword": list(meta.get("keywords") or [])[:30],
        "decade": [str(int(year) // 10 * 10)] if year else [],
        "language": [meta["language"]] if meta.get("language") else [],
        "country": list(meta.get("countries") or [])[:3],
    }


# --------------------------------------------------------------------------- user signal

def film_signal(h: Hist, mean: float, now: datetime) -> tuple[float, float]:
    """Return (signal in [-1, 1], recency weight in [0.7, 1])."""
    if h.rating is not None:
        s = max(-1.0, min(1.0, (h.rating - mean) / 2.0))
        if h.liked:
            s = min(1.0, s + 0.4)
    elif h.liked:
        s = 0.65
    else:
        s = 0.05          # watched, unrated: neutral / slightly positive
    w = 1.0
    if h.date:
        age_years = max(0.0, (now - h.date).days / 365.25)
        w = 0.7 + 0.3 * math.exp(-age_years / 6.0)
    return s, w


def build_profile(history: list[Hist], now: datetime | None = None) -> Profile:
    now = now or datetime.now(timezone.utc)
    p = Profile()
    watched = [h for h in history if h.watched]
    rated = [h.rating for h in watched if h.rating is not None]
    p.n_watched, p.n_rated = len(watched), len(rated)
    p.n_liked = sum(1 for h in watched if h.liked)
    if rated:
        p.mean_rating = sum(rated) / len(rated)
    for h in watched:
        s, w = film_signal(h, p.mean_rating, now)
        p.signals.append((h, s, w))
        for t, names in features(h.meta).items():
            for name in dict.fromkeys(names):
                st = p.stats[t].setdefault(name, FeatStat())
                st.w += w
                st.ssum += w * s
                st.n += 1
                if h.rating is not None:
                    st.rn += 1
                    st.rsum += h.rating
                if st.best is None or s > st.best[0]:
                    st.best = (s, h.title, h.rating, h.liked)
    for t, table in p.stats.items():
        for st in table.values():
            st.aff = st.ssum / (st.w + SHRINK[t])
        ordered = sorted(table, key=lambda k: -table[k].aff)
        p.rank[t] = {k: i for i, k in enumerate(ordered)}
    return p


def mean_rating_of(st: FeatStat) -> float | None:
    return st.rsum / st.rn if st.rn else None


# --------------------------------------------------------------------------- scoring

def build_idf(history: list[Hist], cands: list[Cand]) -> dict:
    """idf-style weight in [0.3, 1] per feature: ubiquitous features (Drama) count less."""
    df = {t: {} for t in FEATURE_TYPES}
    n = 0
    for feats in [features(h.meta) for h in history if h.meta] + [c.feats for c in cands]:
        n += 1
        for t, names in feats.items():
            for name in set(names):
                df[t][name] = df[t].get(name, 0) + 1
    denom = math.log(n + 1) if n > 1 else 1.0
    return {t: {k: 0.3 + 0.7 * math.log((n + 1) / (v + 1)) / denom for k, v in table.items()}
            for t, table in df.items()}


def quality(meta: dict) -> float:
    """Bayesian average of TMDB rating (fallback: Plex rating) mapped to [-1, 1]."""
    r, v = meta.get("vote_average"), meta.get("vote_count") or 0
    if r and v:
        prior_n, prior_mean = 300.0, 6.3
        avg = (v * r + prior_n * prior_mean) / (v + prior_n)
    elif meta.get("plex_rating"):
        avg = (150.0 * meta["plex_rating"] + 150.0 * 6.3) / 300.0
    else:
        return 0.0
    return max(-1.0, min(1.0, (avg - 6.3) / 1.5))


def _feature_value(t: str, names: list[str], prof: Profile, idf: dict):
    """Aggregate the candidate's feature affinities of one type.

    Returns (value, detail) where detail names the most influential positive features.
    """
    table, weights = prof.stats[t], idf.get(t, {})
    scored = []
    for name in dict.fromkeys(names):
        st = table.get(name)
        if st is None:
            continue
        scored.append((name, st.aff, weights.get(name, 0.5)))
    if not scored:
        return 0.0, []
    if t == "director":
        value = max(a for _, a, _ in scored)
        best = sorted(scored, key=lambda x: -x[1])
    elif t == "cast":
        top = sorted(scored, key=lambda x: -x[1])[:2]
        value = sum(a for _, a, _ in top) / 2.0       # one famous face alone counts half
        best = top
    elif t == "keyword":
        top = sorted(scored, key=lambda x: -(x[1] * x[2]))[:4]
        value = sum(a * w for _, a, w in top) / 4.0
        best = top
    else:
        num = sum(a * w for _, a, w in scored)
        den = sum(w for _, _, w in scored)
        value = num / den if den else 0.0
        best = sorted(scored, key=lambda x: -(x[1] * x[2]))
    return value, [(n, a) for n, a, _ in best]


def collab_index(history: list[Hist], prof: Profile) -> dict:
    """tmdb id -> list of (signal, Hist) of loved films whose TMDB recommendations include it."""
    idx: dict[int, list] = {}
    for h, s, w in prof.signals:
        if s >= LOVED and h.recs:
            for rid in h.recs:
                idx.setdefault(rid, []).append((s * w, h))
    return idx


def score_candidates(history: list[Hist], cands: list[Cand], prof: Profile | None = None,
                     now: datetime | None = None) -> Profile:
    """Fill `raw`, `comps` and `match` on every candidate. Returns the profile used."""
    now = now or datetime.now(timezone.utc)
    prof = prof or build_profile(history, now)
    for c in cands:
        c.feats = features(c.meta)
    idf = build_idf(history, cands)
    cidx = collab_index(history, prof)
    for c in cands:
        comps: dict[str, tuple[float, object]] = {}
        for t in FEATURE_TYPES:
            value, detail = _feature_value(t, c.feats.get(t, []), prof, idf)
            comps[t] = (WEIGHT[t] * GAIN[t] * value, detail)
        loved_by = sorted(cidx.get(c.tmdb_id, []), key=lambda x: -x[0]) if c.tmdb_id else []
        if loved_by:
            total = sum(s for s, _ in loved_by)
            comps["similar"] = (W_COLLAB * (1 - math.exp(-total)), loved_by[0][1])
        else:
            comps["similar"] = (0.0, None)
        comps["acclaimed"] = (W_QUALITY * quality(c.meta), None)
        comps["watchlist"] = (W_WATCHLIST if c.on_watchlist else 0.0, None)
        fresh = 0.0
        if c.added_at:
            days = (now - c.added_at).days
            if 0 <= days < FRESH_DAYS:
                fresh = W_FRESH * (1 - days / FRESH_DAYS)
        comps["fresh"] = (fresh, None)
        c.comps = comps
        c.raw = sum(v for v, _ in comps.values())
    calibrate(cands)
    return prof


def calibrate(cands: list[Cand]) -> None:
    """Map raw scores to 0-100: blend of a logistic around the median and the percentile rank."""
    ref_src = [c for c in cands if not c.watched] or cands
    if not cands:
        return
    ref = sorted(c.raw for c in ref_src)
    n = len(ref)
    med = ref[n // 2]
    q1, q3 = ref[n // 4], ref[min(n - 1, (3 * n) // 4)]
    scale = (q3 - q1) / 1.35
    if scale < 1e-9:
        mean = sum(ref) / n
        scale = math.sqrt(sum((x - mean) ** 2 for x in ref) / n)
    for c in cands:
        if scale < 1e-9 or n == 1:
            z, p = 0.0, 0.5
        else:
            z = max(-8.0, min(8.0, (c.raw - med) / scale))
            lo, hi = bisect_left(ref, c.raw), bisect_right(ref, c.raw)
            p = ((lo + hi - 1) / 2) / (n - 1)
            p = max(0.0, min(1.0, p))
        logistic = 1.0 / (1.0 + math.exp(-1.2 * z))
        m = 0.5 * logistic + 0.5 * p
        c.match = max(1, min(99, round(100 * (0.06 + 0.9 * m))))


# --------------------------------------------------------------------------- reasons

def half_stars(x: float) -> float:
    return max(0.5, min(5.0, round(x * 2) / 2))


def _rating_phrase(h: Hist) -> str:
    if h.rating is not None:
        return f" (you gave it {stars(h.rating)})"
    if h.liked:
        return " (you liked it)"
    return ""


def reasons_for(c: Cand, prof: Profile, limit: int = 4) -> list[dict]:
    items: list[tuple[float, dict]] = []

    def add(value, kind, text):
        items.append((value, {"kind": kind, "text": text}))

    comps = c.comps
    if comps.get("watchlist", (0, 0))[0] > 0:
        add(comps["watchlist"][0] + 0.2, "watchlist", "On your Letterboxd watchlist")

    v, detail = comps.get("similar", (0, None))
    if v > MIN_REASON and detail is not None:
        add(v, "similar", f"Fans of {detail.title} {_rating_phrase(detail).strip()} also love this".replace("  ", " "))

    v, detail = comps.get("director", (0, []))
    if v > MIN_REASON and detail:
        name = detail[0][0]
        st = prof.stats["director"][name]
        mean = mean_rating_of(st)
        if st.n >= 2 and mean is not None:
            add(v, "director", f"You rated {st.n} films by {name} {stars(half_stars(mean))} on average")
        elif st.n >= 2:
            add(v, "director", f"You've watched {st.n} films by {name}")
        elif st.best:
            _, title, rating, liked = st.best
            tail = f" (you gave it {stars(rating)})" if rating is not None else " (you liked it)" if liked else ""
            add(v, "director", f"From {name}, who directed {title}{tail}")

    v, detail = comps.get("cast", (0, []))
    if v > MIN_REASON and detail:
        name = detail[0][0]
        st = prof.stats["cast"][name]
        if st.n >= 2:
            add(v, "cast", f"You've enjoyed {st.n} films with {name}")
        elif st.best:
            add(v, "cast", f"Stars {name}, who you liked in {st.best[1]}")

    v, detail = comps.get("genre", (0, []))
    if v > MIN_REASON and detail:
        liked_genres = [(n, a) for n, a in detail if a > 0]
        if liked_genres:
            name = max(liked_genres, key=lambda x: x[1])[0]
            if prof.rank["genre"].get(name, 99) < 3:
                add(v, "genre", f"{name} is one of your top genres")
            else:
                add(v, "genre", f"You tend to enjoy {name} films")

    v, detail = comps.get("keyword", (0, []))
    if v > MIN_REASON and detail:
        names = [n for n, a in detail if a > 0][:3]
        if names:
            add(v, "keyword", "Themes you like: " + ", ".join(names))

    v, detail = comps.get("decade", (0, []))
    if v > MIN_REASON and detail and detail[0][1] > 0:
        add(v, "decade", f"You tend to enjoy films from the {detail[0][0]}s")

    v, _ = comps.get("acclaimed", (0, None))
    if v > 0.03:
        r = c.meta.get("vote_average")
        if r and (c.meta.get("vote_count") or 0) >= 200:
            add(v, "acclaimed", f"Acclaimed: {r:.1f} on TMDB ({_short_count(c.meta['vote_count'])} votes)")
        elif c.meta.get("plex_rating"):
            add(v, "acclaimed", f"Well rated ({c.meta['plex_rating']:.1f}/10)")

    v, _ = comps.get("fresh", (0, None))
    if v > 0.01:
        add(v, "fresh", "Recently added to Plex")

    items.sort(key=lambda x: -x[0])
    out = [r for _, r in items[:limit]]
    if not out:
        genres = c.meta.get("genres") or []
        if genres:
            out = [{"kind": "genre", "text": f"A {genres[0]} pick from your library"}]
        else:
            out = [{"kind": "fresh", "text": "From your Plex library"}]
    return out


def _short_count(n: int) -> str:
    return f"{n / 1000:.1f}k".replace(".0k", "k") if n >= 1000 else str(n)


# --------------------------------------------------------------------------- "because"

def because(c: Cand, history: list[Hist], prof: Profile, limit: int = 6) -> list[dict]:
    """Films from the user's history that explain this recommendation."""
    out = []
    cfeat = c.feats or features(c.meta)
    cdirs, ccast, ckw = set(cfeat["director"]), set(cfeat["cast"]), set(cfeat["keyword"])
    sig = {h.key: s * w for h, s, w in prof.signals}
    for h in history:
        if not h.watched or h.key not in sig or sig[h.key] <= 0.1:
            continue
        hf = features(h.meta)
        why, strength = None, 0.0
        if c.tmdb_id and c.tmdb_id in h.recs:
            why, strength = "Fans of this also love it", 1.0
        if set(hf["director"]) & cdirs:
            d = sorted(set(hf["director"]) & cdirs)[0]
            why, strength = (f"Same director ({d})", max(strength, 0.9)) if strength < 0.9 else (why, strength)
        shared_cast = sorted(set(hf["cast"]) & ccast)
        if shared_cast and strength < 0.7:
            why, strength = f"Also stars {shared_cast[0]}", 0.7
        shared_kw = sorted(set(hf["keyword"]) & ckw)
        if len(shared_kw) >= 3 and strength < 0.5:
            why, strength = "Similar themes: " + ", ".join(shared_kw[:3]), 0.5
        if why:
            out.append((strength * sig[h.key], {
                "title": h.title, "year": h.year, "my_rating": h.rating, "liked": h.liked,
                "poster_url": h.poster, "why": why,
            }))
    out.sort(key=lambda x: -x[0])
    return [o for _, o in out[:limit]]
