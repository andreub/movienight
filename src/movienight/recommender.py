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
* Thumbs (a vote per film, see `Cand.vote`) are weaker evidence than a Letterboxd rating: a voted
  film that has no real rating enters the profile as a synthetic history entry with a fixed
  signal of +/-`VOTE_SIGNAL`, and gets a direct boost / strong penalty. Once the film has a
  rating, the vote is ignored by the model.
* `backtest()` validates and tunes the component weights with a leave-one-out evaluation over
  the rated films (see its docstring).
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
# effective default weight of every component that can be backtested and tuned
DEFAULT_WEIGHTS = {**{t: WEIGHT[t] * GAIN[t] for t in FEATURE_TYPES}, "similar": W_COLLAB, "acclaimed": W_QUALITY}
VOTE_SIGNAL = 0.6        # |signal| of a thumb for the profile (a 5-star film is ~0.8, a liked unrated one 0.65)
VOTE_UP_BOOST = 0.25     # direct raw-score boost of a thumbs-up film
VOTE_DOWN_PENALTY = 0.7  # direct raw-score penalty of a thumbs-down film
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
    vote: int = 0                       # thumb (+1/-1); only meaningful on synthetic entries
    synthetic: bool = False             # built from a thumb, not from Letterboxd


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
    vote: int | None = None             # thumb: 1, -1 or None
    seen_local: bool = False            # marked "seen it" in this app (neutral: not a taste signal)
    dkey: str = ""                      # stable library key (movies.dkey)
    # computed
    feats: dict = field(default_factory=dict)
    raw: float = 0.0
    match: int = 0
    comps: dict = field(default_factory=dict)

    @property
    def watched(self) -> bool:
        return self.plex_watched or self.lb_watched or self.seen_local

    @property
    def seen_here(self) -> bool:
        """Watched only because of the local mark (not Letterboxd / Plex)."""
        return self.seen_local and not (self.plex_watched or self.lb_watched)


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
    vote_hists: list = field(default_factory=list)      # synthetic entries built from thumbs


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
    if h.synthetic:
        s = VOTE_SIGNAL * (1 if h.vote > 0 else -1)
    elif h.rating is not None:
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
    watched = [h for h in history if h.watched and not h.synthetic]
    rated = [h.rating for h in watched if h.rating is not None]
    p.n_watched, p.n_rated = len(watched), len(rated)
    p.n_liked = sum(1 for h in watched if h.liked)
    if rated:
        p.mean_rating = sum(rated) / len(rated)
    for h in [h for h in history if h.watched]:
        s, w = film_signal(h, p.mean_rating, now)
        if h.synthetic:
            p.vote_hists.append(h)
        else:
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


def _feature_value(t: str, names: list[str], table, idf: dict):
    """Aggregate the candidate's feature affinities of one type.

    `table` is anything with `.get(name) -> FeatStat | None` (the profile's table, or a
    leave-one-out view of it). Returns (value, detail) where detail names the most influential
    positive features.
    """
    weights = idf.get(t, {})
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


def vote_hists(history: list[Hist], cands: list[Cand]) -> list[Hist]:
    """Synthetic history entries for thumbed candidates that have no real Letterboxd rating.

    A film with a rating (on the candidate or in the history) is skipped: the rating wins.
    """
    rated = {h.tmdb_id for h in history if h.rating is not None and h.tmdb_id}
    out = []
    for c in cands:
        if not c.vote or c.my_rating is not None or (c.tmdb_id and c.tmdb_id in rated):
            continue
        out.append(Hist(key=-c.id, title=c.title, year=c.year, tmdb_id=c.tmdb_id, watched=True,
                        meta=c.meta, vote=1 if c.vote > 0 else -1, synthetic=True))
    return out


def component_values(feats: dict, meta: dict, tmdb_id: int | None, table_of, idf: dict, loved_by: list):
    """Unweighted value (and detail for reasons) of every tunable component of one film.

    `table_of(t)` returns the affinity table of feature type `t`; `loved_by` is the list of
    (signal, Hist) of loved films recommending this one.
    """
    out: dict[str, tuple[float, object]] = {}
    for t in FEATURE_TYPES:
        out[t] = _feature_value(t, feats.get(t, []), table_of(t), idf)
    if loved_by:
        loved_by = sorted(loved_by, key=lambda x: -x[0])
        out["similar"] = (1 - math.exp(-sum(s for s, _ in loved_by)), loved_by[0][1])
    else:
        out["similar"] = (0.0, None)
    out["acclaimed"] = (quality(meta), None)
    return out


def score_candidates(history: list[Hist], cands: list[Cand], prof: Profile | None = None,
                     now: datetime | None = None, weights: dict | None = None) -> Profile:
    """Fill `raw`, `comps` and `match` on every candidate. Returns the profile used.

    `weights` overrides `DEFAULT_WEIGHTS` (missing keys fall back to the defaults).
    """
    now = now or datetime.now(timezone.utc)
    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    prof = prof or build_profile(history + vote_hists(history, cands), now)
    for c in cands:
        c.feats = features(c.meta)
    idf = build_idf(history, cands)
    cidx = collab_index(history, prof)
    for c in cands:
        vals = component_values(c.feats, c.meta, c.tmdb_id, lambda t: prof.stats[t], idf,
                                cidx.get(c.tmdb_id, []) if c.tmdb_id else [])
        comps = {k: (w[k] * v, d) for k, (v, d) in vals.items()}
        comps["watchlist"] = (W_WATCHLIST if c.on_watchlist else 0.0, None)
        fresh = 0.0
        if c.added_at:
            days = (now - c.added_at).days
            if 0 <= days < FRESH_DAYS:
                fresh = W_FRESH * (1 - days / FRESH_DAYS)
        comps["fresh"] = (fresh, None)
        vote = 0.0
        if c.vote and c.my_rating is None:
            vote = VOTE_UP_BOOST if c.vote > 0 else -VOTE_DOWN_PENALTY
        comps["vote"] = (vote, None)
        c.comps = comps
        c.raw = sum(v for v, _ in comps.values())
    calibrate(cands)
    return prof


def calibrate(cands: list[Cand]) -> None:
    """Map raw scores to 0-100: blend of a logistic around the median and the percentile rank.

    The watchlist is left out of that scale and applied afterwards as a lift of half the
    distance to the top. Inside the raw score it pushed every watchlist film to the same
    ceiling, so they could not be told apart; this way they stay ordered by taste and a
    weak watchlist film does not outrank a great match."""
    def taste(c: Cand) -> float:
        return c.raw - (W_WATCHLIST if c.on_watchlist else 0.0)

    ref_src = [c for c in cands if not c.watched] or cands
    if not cands:
        return
    ref = sorted(taste(c) for c in ref_src)
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
            z = max(-8.0, min(8.0, (taste(c) - med) / scale))
            lo, hi = bisect_left(ref, taste(c)), bisect_right(ref, taste(c))
            p = ((lo + hi - 1) / 2) / (n - 1)
            p = max(0.0, min(1.0, p))
        logistic = 1.0 / (1.0 + math.exp(-1.2 * z))
        m = 0.5 * logistic + 0.5 * p
        match = 100 * (0.06 + 0.9 * m)
        if c.on_watchlist:
            match += 0.5 * (99 - match)
        c.match = max(1, min(99, round(match)))


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

    if c.vote is None:
        liked_vote = _similar_vote(c, prof)
        if liked_vote:
            add(0.04, "similar", f"Similar to {liked_vote.title}, which you gave a \U0001F44D")

    items.sort(key=lambda x: -x[0])
    out = [r for _, r in items[:limit]]
    if not out:
        genres = c.meta.get("genres") or []
        if genres:
            out = [{"kind": "genre", "text": f"A {genres[0]} pick from your library"}]
        else:
            out = [{"kind": "fresh", "text": "From your Plex library"}]
    return out


def _similar_vote(c: Cand, prof: Profile) -> Hist | None:
    """A thumbs-up film that shares a director, two cast members or three keywords with `c`."""
    cf = c.feats or features(c.meta)
    for h in prof.vote_hists:
        if h.vote <= 0 or (c.tmdb_id and h.tmdb_id == c.tmdb_id):
            continue
        hf = features(h.meta)
        if (set(hf["director"]) & set(cf["director"]) or len(set(hf["cast"]) & set(cf["cast"])) >= 2
                or len(set(hf["keyword"]) & set(cf["keyword"])) >= 3):
            return h
    return None


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


# --------------------------------------------------------------------------- backtest and tuning

TUNABLE = tuple(DEFAULT_WEIGHTS)
MULTIPLIERS = (0.5, 0.7, 1.0, 1.4, 2.0)      # search grid, bounded to 0.5x-2x of each default
MIN_BACKTEST_FILMS = 60                       # below this, defaults are always kept
MIN_METRIC_FILMS = 8                          # below this, metrics are not reported
CV_FOLDS = 5
MIN_CV_GAIN = 0.02                            # mean held-out Spearman gain needed to adopt tuned weights
MIN_FOLDS_IMPROVED = 3                        # ...and at least this many folds must improve
SWEEPS = 3


class _LeaveOut:
    """Read-only view of a feature table in which one film's contribution is subtracted."""

    def __init__(self, base: dict, adjusted: dict):
        self.base, self.adjusted = base, adjusted      # adjusted: name -> FeatStat | None

    def get(self, name):
        if name in self.adjusted:
            return self.adjusted[name]
        return self.base.get(name)


def _without(st: FeatStat | None, t: str, s: float, w: float, h: Hist) -> FeatStat | None:
    """`st` minus the contribution of one film (`best` is kept only as display data)."""
    if st is None or st.n <= 1:
        return None
    out = FeatStat(w=st.w - w, ssum=st.ssum - w * s, n=st.n - 1, rn=st.rn - (h.rating is not None),
                   rsum=st.rsum - (h.rating or 0.0), best=st.best)
    out.aff = out.ssum / (out.w + SHRINK[t])
    return out


def backtestable(h: Hist) -> bool:
    m = h.meta or {}
    return bool(h.watched and h.rating is not None and
                (m.get("genres") or m.get("directors") or m.get("cast") or m.get("keywords")))


def loo_rows(history: list[Hist], cands: list[Cand] | None = None, now: datetime | None = None) -> list[tuple]:
    """Leave-one-out component values for every backtestable film.

    Returns [(Hist, {component: unweighted value})]. For each rated film the profile is the one
    built from the whole history minus that film's own contribution: its (signal, weight) is
    subtracted from the accumulated per-feature statistics (a feature seen only in this film
    disappears), and the film is excluded from the loved-films collaborative evidence. Watchlist,
    freshness and thumbs are not components of these rows. Two small leaks remain by design: the
    user's mean rating (which centres every signal) still includes this film's rating, and the
    idf table counts the film itself in its feature frequencies; both are 1/N effects.
    """
    now = now or datetime.now(timezone.utc)
    cands = cands or []
    for c in cands:
        c.feats = features(c.meta)
    prof = build_profile(history, now)
    idf = build_idf(history, cands)
    cidx = collab_index(history, prof)
    out = []
    for h, s, w in prof.signals:
        if not backtestable(h):
            continue
        adjusted = {t: {} for t in FEATURE_TYPES}
        feats = features(h.meta)
        for t, names in feats.items():
            for name in dict.fromkeys(names):
                adjusted[t][name] = _without(prof.stats[t].get(name), t, s, w, h)
        views = {t: _LeaveOut(prof.stats[t], adjusted[t]) for t in FEATURE_TYPES}
        loved_by = [(x, y) for x, y in (cidx.get(h.tmdb_id, []) if h.tmdb_id else []) if y.key != h.key]
        vals = component_values(feats, h.meta, h.tmdb_id, lambda t: views[t], idf, loved_by)
        out.append((h, {k: v for k, (v, _) in vals.items()}))
    out.sort(key=lambda x: x[0].key)
    return out


def _ranks(xs: list[float]) -> list[float]:
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2.0 + 1
        i = j + 1
    return ranks


def spearman(xs: list[float], ys: list[float]) -> float | None:
    """Spearman rank correlation (average ranks for ties); None when undefined."""
    n = len(xs)
    if n < 2:
        return None
    rx, ry = _ranks(xs), _ranks(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    sxy = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    sxx = sum((a - mx) ** 2 for a in rx)
    syy = sum((b - my) ** 2 for b in ry)
    if sxx <= 0 or syy <= 0:
        return None
    return sxy / math.sqrt(sxx * syy)


def top_quintile(preds: list[float], ratings: list[float]) -> tuple[float | None, float | None]:
    """(hit rate of the top 20% predicted, base rate). A hit is one of the user's favourites:
    a rating >= the lowest rating value that at most 40% of their films reach. Ratings come
    in half stars, so a plain 70th percentile can land on a value most films share."""
    n = len(preds)
    if n < MIN_METRIC_FILMS:
        return None, None
    thr = max(ratings)
    for value in sorted(set(ratings), reverse=True):
        if sum(1 for r in ratings if r >= value) / n > 0.4:
            break
        thr = value
    base = sum(1 for r in ratings if r >= thr) / n
    k = max(1, round(0.2 * n))
    top = sorted(range(n), key=lambda i: -preds[i])[:k]
    return sum(1 for i in top if ratings[i] >= thr) / k, base


def _raw(vals: list[dict], weights: dict) -> list[float]:
    return [sum(weights[k] * v[k] for k in TUNABLE) for v in vals]


def _fit(vals: list[dict], ratings: list[float]) -> dict:
    """Coordinate ascent over per-component multipliers (grid, 0.5x-2x), maximising Spearman."""
    mult = {k: 1.0 for k in TUNABLE}

    def score(m):
        r = spearman(_raw(vals, {k: DEFAULT_WEIGHTS[k] * m[k] for k in TUNABLE}), ratings)
        return -2.0 if r is None else r

    best = score(mult)
    for _ in range(SWEEPS):
        improved = False
        for k in TUNABLE:
            keep = mult[k]
            for g in MULTIPLIERS:
                if g == keep:
                    continue
                mult[k] = g
                sc = score(mult)
                if sc > best + 1e-9:
                    best, keep, improved = sc, g, True
            mult[k] = keep
        if not improved:
            break
    return {k: DEFAULT_WEIGHTS[k] * mult[k] for k in TUNABLE}


def tune(rows: list[tuple]) -> dict:
    """Cross-validated tuning of the component weights.

    Folds are assigned deterministically (film order, i % 5). For each fold the weights are fitted
    on the other folds and compared with the defaults on the held-out fold. The tuned weights
    (fitted on all films) are adopted only with at least `MIN_BACKTEST_FILMS` films, a mean
    held-out Spearman gain >= `MIN_CV_GAIN` and an improvement in >= `MIN_FOLDS_IMPROVED` folds.
    """
    vals = [v for _, v in rows]
    ratings = [h.rating for h, _ in rows]
    n = len(rows)
    result = {"adopted": False, "weights": dict(DEFAULT_WEIGHTS), "cv_gain": None}
    if n < MIN_BACKTEST_FILMS:
        return result
    gains = []
    for f in range(CV_FOLDS):
        tr = [i for i in range(n) if i % CV_FOLDS != f]
        te = [i for i in range(n) if i % CV_FOLDS == f]
        w = _fit([vals[i] for i in tr], [ratings[i] for i in tr])
        tv, tr_ = [vals[i] for i in te], [ratings[i] for i in te]
        a, b = spearman(_raw(tv, w), tr_), spearman(_raw(tv, DEFAULT_WEIGHTS), tr_)
        if a is not None and b is not None:
            gains.append(a - b)
    if not gains:
        return result
    result["cv_gain"] = sum(gains) / len(gains)
    if result["cv_gain"] >= MIN_CV_GAIN and sum(1 for g in gains if g > 0) >= MIN_FOLDS_IMPROVED:
        result["adopted"] = True
        result["weights"] = _fit(vals, ratings)
    return result


def backtest(history: list[Hist], cands: list[Cand] | None = None, now: datetime | None = None,
             do_tune: bool = True) -> dict:
    """Leave-one-out backtest (and tuning) of the scorer over the user's rated films.

    Returns {"films", "spearman", "top_quintile_hit_rate", "base_rate", "weights",
    "default_weights", "tuned", "default_spearman", "cv_gain"}; metrics are for the adopted
    weights and are None when there are fewer than `MIN_METRIC_FILMS` backtestable films.
    """
    rows = loo_rows(history, cands, now)
    tuned = tune(rows) if do_tune else {"adopted": False, "weights": dict(DEFAULT_WEIGHTS), "cv_gain": None}
    weights = tuned["weights"]
    ratings = [h.rating for h, _ in rows]
    preds = _raw([v for _, v in rows], weights)
    hit, base = top_quintile(preds, ratings)
    r = spearman(preds, ratings) if len(rows) >= MIN_METRIC_FILMS else None
    d = spearman(_raw([v for _, v in rows], DEFAULT_WEIGHTS), ratings) if len(rows) >= MIN_METRIC_FILMS else None
    rd = lambda x: None if x is None else round(x, 4)  # noqa: E731
    return {"films": len(rows), "spearman": rd(r), "top_quintile_hit_rate": rd(hit), "base_rate": rd(base),
            "weights": {k: round(v, 4) for k, v in weights.items()},
            "default_weights": {k: round(v, 4) for k, v in DEFAULT_WEIGHTS.items()},
            "tuned": tuned["adopted"], "default_spearman": rd(d), "cv_gain": rd(tuned["cv_gain"])}
