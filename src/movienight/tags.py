"""Letterboxd diary tags as reference groups: listing, statistics and "films like the ones I tagged X".

A tag (`sitges2025`) or a whole family (`sitges` = every edition) names a group of films the user has
already watched. The group is never a filter on those films; it steers recommendations towards unwatched
library films that resemble it (see `recommender.group_similarity`).
"""
from __future__ import annotations

import dataclasses
import math
import sqlite3
from collections import Counter

from . import recommender as rec
from .letterboxd import tag_family

MIN_GROUP_FILMS = 3     # a group needs this many films with metadata before it is offered
TAG_WEIGHT = 1.5        # weight of the group similarity next to the user's own taste score
TAG_TOP_SHARE = 0.25    # a group filter keeps the quarter of the library most similar to the group...
TAG_MIN_POOL = 40       # ...but never fewer films than this (small libraries)


class UnknownTag(ValueError):
    pass


def load(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT lb_id, tag, family, edition, watched_date, rating FROM lb_tags ORDER BY family, edition, tag")]


def _has_features(h: rec.Hist) -> bool:
    m = h.meta or {}
    return bool(m.get("genres") or m.get("directors") or m.get("cast") or m.get("keywords"))


def _hists(st) -> dict:
    if "hist_by_key" not in st.cache:
        st.cache["hist_by_key"] = {h.key: h for h in st.history}
    return st.cache["hist_by_key"]


def _group(st, rows: list[dict]) -> dict:
    by_key = _hists(st)
    keys = list(dict.fromkeys(r["lb_id"] for r in rows))
    films = [by_key[k] for k in keys if k in by_key]
    matched = [h for h in films if _has_features(h)]
    ratings = [r["rating"] for r in rows if r["rating"] is not None]
    genres = Counter(g for h in matched for g in dict.fromkeys((h.meta or {}).get("genres") or []))
    return {
        "films": len(keys), "matched": len(matched),
        "mean_rating": round(sum(ratings) / len(ratings), 2) if ratings else None,
        "top_genres": [g for g, _ in sorted(genres.items(), key=lambda x: (-x[1], x[0]))[:3]],
        "_hists": matched,
    }


def label(family: str) -> str:
    return family[:1].upper() + family[1:]


def overview(st) -> list[dict]:
    """Every family with its tags (editions newest first), counts, mean rating and top genres."""
    if "tag_overview" in st.cache:
        return st.cache["tag_overview"]
    fams: dict[str, list[dict]] = {}
    for r in st.tags:
        fams.setdefault(r["family"], []).append(r)
    out = []
    for family, rows in fams.items():
        by_tag: dict[str, list[dict]] = {}
        for r in rows:
            by_tag.setdefault(r["tag"], []).append(r)
        editions = []
        for tag, trs in by_tag.items():
            editions.append({"tag": tag, "edition": trs[0]["edition"], **_group(st, trs)})
        editions.sort(key=lambda e: (-(e["edition"] or 0), e["tag"]))
        out.append({"family": family, "label": label(family), **_group(st, rows), "editions": editions})
    out.sort(key=lambda f: (-f["films"], f["family"]))
    st.cache["tag_overview"] = out
    return out


def _public(g: dict) -> dict:
    return {k: v for k, v in g.items() if not k.startswith("_")}


def usable(st) -> list[dict]:
    """Families (and their tags) with at least MIN_GROUP_FILMS films that have metadata: what can steer picks."""
    out = []
    for f in overview(st):
        if f["matched"] < MIN_GROUP_FILMS:
            continue
        eds = [{k: e[k] for k in ("tag", "edition", "films", "matched")} for e in f["editions"]
               if e["matched"] >= MIN_GROUP_FILMS]
        out.append({"family": f["family"], "label": f["label"], "films": f["films"], "matched": f["matched"],
                    "editions": eds})
    return out


def stats(st) -> list[dict]:
    """Profile view: every family and tag, usable or not."""
    return [{**_public(f), "editions": [_public(e) for e in f["editions"]]} for f in overview(st)]


def _fold(s: str) -> str:
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def resolve(st, tag: str | None = None, family: str | None = None, loose: str | None = None) -> tuple[str, str, dict]:
    """(kind, name, group) for an exact tag, a family, or (`loose`, for the chat) whichever matches first,
    ignoring case, spaces and punctuation ("Sitges 2025" -> sitges2025, "sitges" -> the family).
    Raises UnknownTag when nothing matches or the group is too small."""
    fams = overview(st)
    if loose is not None:
        want = _fold(loose)
        for f in fams:
            for e in f["editions"]:
                if _fold(e["tag"]) == want:
                    tag = e["tag"]
        if tag is None:
            family = next((f["family"] for f in fams if _fold(f["family"]) == want), None)
        if tag is None and family is None:
            raise UnknownTag(f"Unknown tag {loose!r}")
    if tag is not None:
        t = " ".join(tag.split()).lower()
        fam = next((f for f in fams if f["family"] == tag_family(t)[0]), None)
        grp = next((e for e in (fam or {}).get("editions", []) if e["tag"] == t), None)
        kind, name = "tag", t
    else:
        name = " ".join((family or "").split()).lower()
        grp = next((f for f in fams if f["family"] == name), None)
        kind = "family"
    if grp is None:
        raise UnknownTag(f"Unknown {kind} {name!r}")
    if grp["matched"] < MIN_GROUP_FILMS:
        raise UnknownTag(f"The {kind} {name!r} has fewer than {MIN_GROUP_FILMS} films with details")
    return kind, name, grp


def group_cands(st, kind: str, name: str, grp: dict) -> list:
    """Copies of the library candidates for a tag filter: only the TAG_TOP_SHARE most similar to the
    group (among unwatched films), ranked by taste + TAG_WEIGHT x group similarity and recalibrated, so
    `match` and the pick follow the blend. Cached on the state (rebuilt whenever the state is)."""
    key = ("tag_cands", kind, name)
    if key in st.cache:
        return st.cache[key]
    films = grp["_hists"]
    if "idf" not in st.cache:
        st.cache["idf"] = rec.build_idf(st.history, st.cands)
    sim = rec.group_similarity(films, st.cands, st.cache["idf"], st.profile.mean_rating, st.weights)
    pool = sorted((sim[c.id] for c in st.cands if not c.watched), reverse=True)
    keep = max(math.ceil(len(pool) * TAG_TOP_SHARE), TAG_MIN_POOL)
    threshold = max(pool[min(keep, len(pool)) - 1] if pool else 0.0, 1e-9)
    tags_of = {r["lb_id"]: r["tag"] for r in st.tags if (r["family"] if kind == "family" else r["tag"]) == name}
    detail = {"label": name, "films": films, "tags": tags_of}
    copies = []
    for c in st.cands:
        g = sim[c.id]
        cc = dataclasses.replace(c, comps={**c.comps, "tag": (TAG_WEIGHT * g, detail)})
        cc.raw = c.raw + TAG_WEIGHT * g
        copies.append(cc)
    rec.calibrate(copies)
    out = [cc for cc in copies if sim[cc.id] >= threshold]
    st.cache[key] = out
    return out
