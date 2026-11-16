"""Letterboxd: CSV export (folder or ZIP) import and RSS sync."""
from __future__ import annotations

import csv
import hashlib
import io
import re
import sqlite3
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import httpx
from defusedxml import ElementTree as ET

from .db import get_meta, set_meta
from .util import norm_title, now_iso, to_float, to_int

EXPORT_FILES = ("watched.csv", "ratings.csv", "likes/films.csv", "watchlist.csv", "diary.csv", "reviews.csv",
                "profile.csv")
# Bumped when the importer starts reading something new from files it already had (v2: diary tags), so an
# unchanged export folder is imported once more after the upgrade.
IMPORT_VERSION = "2"
MAX_ZIP_BYTES = 20 * 1024 * 1024          # uploaded archive
MAX_MEMBER_BYTES = 8 * 1024 * 1024        # per extracted CSV
MAX_TOTAL_BYTES = 24 * 1024 * 1024        # all extracted CSVs
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)
NS = {"lb": "https://letterboxd.com", "tmdb": "https://themoviedb.org"}


class ExportError(ValueError):
    pass


@dataclass
class ExportFilm:
    name: str
    year: int | None
    uri: str | None = None
    watched: bool = False
    rating: float | None = None
    liked: bool = False
    watchlist: bool = False
    date: str | None = None


@dataclass
class Export:
    films: dict = field(default_factory=dict)   # (norm_title, year) -> ExportFilm
    username: str = ""
    # diary tags: {(film key, tag, watched date or ''): rating or None}; None = no diary in this export
    tags: dict | None = None


# --------------------------------------------------------------------------- tags

_EDITION = re.compile(r"^(.*?\S)[\s_-]*((?:19|20)\d{2})$")


def parse_tags(raw: str | None) -> list[str]:
    """Letterboxd exports tags comma-separated ("a, b"). Lower case, inner spaces collapsed, deduped."""
    out = []
    for t in (raw or "").split(","):
        t = " ".join(t.split()).lower()
        if t and t not in out:
            out.append(t)
    return out


def tag_family(tag: str) -> tuple[str, int | None]:
    """'sitges2025' -> ('sitges', 2025); a tag without a trailing 4-digit year is its own family."""
    m = _EDITION.match(tag)
    if m:
        return m.group(1), int(m.group(2))
    return tag, None


# --------------------------------------------------------------------------- parsing

def _rows(data: bytes):
    text = data.decode("utf-8-sig", errors="replace")
    return csv.DictReader(io.StringIO(text))


def parse_export(files: dict[str, bytes]) -> Export:
    """`files` maps lower-case relative names (e.g. 'likes/films.csv') to raw bytes."""
    exp = Export()

    def film(row) -> ExportFilm | None:
        name = (row.get("Name") or "").strip()
        if not name:
            return None
        year = to_int(row.get("Year"))
        key = (norm_title(name), year)
        f = exp.films.get(key)
        if f is None:
            f = exp.films[key] = ExportFilm(name=name, year=year)
        return f

    def upd_date(f: ExportFilm, d: str | None):
        d = (d or "").strip() or None
        if d and (f.date is None or d > f.date):
            f.date = d

    for row in _rows(files["watched.csv"]) if "watched.csv" in files else []:
        if f := film(row):
            f.watched = True
            f.uri = f.uri or (row.get("Letterboxd URI") or "").strip() or None
            upd_date(f, row.get("Date"))
    for row in _rows(files["ratings.csv"]) if "ratings.csv" in files else []:
        if f := film(row):
            f.watched = True
            r = to_float(row.get("Rating"))
            if r is not None:
                f.rating = r
            f.uri = f.uri or (row.get("Letterboxd URI") or "").strip() or None
            upd_date(f, row.get("Date"))
    for row in _rows(files["likes/films.csv"]) if "likes/films.csv" in files else []:
        if f := film(row):
            f.liked = True
            f.uri = f.uri or (row.get("Letterboxd URI") or "").strip() or None
    for row in _rows(files["watchlist.csv"]) if "watchlist.csv" in files else []:
        if f := film(row):
            f.watchlist = True
            f.uri = f.uri or (row.get("Letterboxd URI") or "").strip() or None
    # diary: its URIs are entry URIs, so only use it for dates / fallback ratings (and tags)
    if "diary.csv" in files:
        exp.tags = {}
    for name in ("diary.csv", "reviews.csv"):
        for row in _rows(files[name]) if name in files else []:
            if f := film(row):
                f.watched = True
                if f.rating is None:
                    f.rating = to_float(row.get("Rating"))
                when = (row.get("Watched Date") or row.get("Date") or "").strip()
                upd_date(f, when)
                if exp.tags is not None:
                    key = (norm_title(f.name), f.year)
                    for t in parse_tags(row.get("Tags")):
                        r = to_float(row.get("Rating"))
                        if exp.tags.get((key, t, when)) is None:
                            exp.tags[(key, t, when)] = r
    if "profile.csv" in files:
        for row in _rows(files["profile.csv"]):
            exp.username = (row.get("Username") or "").strip()
            break
    for f in exp.films.values():
        if f.watched:
            f.watchlist = False
    return exp


# --------------------------------------------------------------------------- DB upserts

def _find_row(conn: sqlite3.Connection, uri: str | None, nt: str, year: int | None):
    if uri:
        r = conn.execute("SELECT * FROM lb_films WHERE uri=?", (uri,)).fetchone()
        if r:
            return r
    if year is None:
        return conn.execute(
            "SELECT * FROM lb_films WHERE norm_title=? AND year IS NULL", (nt,)
        ).fetchone()
    return conn.execute("SELECT * FROM lb_films WHERE norm_title=? AND year=?", (nt, year)).fetchone()


def apply_export(conn: sqlite3.Connection, exp: Export) -> dict:
    conn.execute("UPDATE lb_films SET watchlist=0")
    now = now_iso()
    for (nt, year), f in exp.films.items():
        row = _find_row(conn, f.uri, nt, year)
        if row is None:
            conn.execute(
                "INSERT INTO lb_films(name,year,norm_title,uri,watched,rating,liked,watchlist,watched_date,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (f.name, year, nt, f.uri, int(f.watched), f.rating, int(f.liked), int(f.watchlist), f.date, now),
            )
            continue
        rating = f.rating if f.rating is not None else row["rating"]
        date = max([d for d in (f.date, row["watched_date"]) if d], default=None)
        watched = int(f.watched or row["watched"])
        conn.execute(
            "UPDATE lb_films SET name=?, uri=COALESCE(uri,?), watched=?, rating=?, liked=?, watchlist=?,"
            " watched_date=?, updated_at=? WHERE id=?",
            (f.name, f.uri, watched, rating, int(f.liked or row["liked"]),
             0 if watched else int(f.watchlist), date, now, row["id"]),
        )
    if exp.tags is not None:
        store_tags(conn, exp)
    if exp.username:
        set_meta(conn, "lb_export_user", exp.username)
    set_meta(conn, "lb_last_sync", now)
    counts = lb_counts(conn)
    return {k: counts[k] for k in ("watched", "rated", "liked", "watchlist")}


def store_tags(conn: sqlite3.Connection, exp: Export) -> int:
    """Replace the export's diary tags (rows from other sources stay). Idempotent."""
    conn.execute("DELETE FROM lb_tags WHERE source='export'")
    rows = []
    for ((nt, year), tag, when), rating in exp.tags.items():
        f = exp.films[(nt, year)]
        row = _find_row(conn, f.uri, nt, year)
        if row is None:
            continue
        family, edition = tag_family(tag)
        rows.append((row["id"], tag, family, edition, when, rating))
    conn.executemany("INSERT OR IGNORE INTO lb_tags(lb_id,tag,family,edition,watched_date,rating,source)"
                     " VALUES(?,?,?,?,?,?,'export')", rows)
    return len(rows)


def lb_counts(conn: sqlite3.Connection) -> dict:
    r = conn.execute(
        "SELECT COALESCE(SUM(watched),0) w, COALESCE(SUM(rating IS NOT NULL),0) r,"
        " COALESCE(SUM(liked),0) l, COALESCE(SUM(watchlist),0) wl FROM lb_films"
    ).fetchone()
    return {"watched": r["w"], "rated": r["r"], "liked": r["l"], "watchlist": r["wl"]}


def username(conn: sqlite3.Connection, env_user: str = "") -> str:
    return get_meta(conn, "lb_user") or env_user or get_meta(conn, "lb_export_user") or ""


# --------------------------------------------------------------------------- folder / zip

def _fingerprint(root: Path) -> tuple[str, dict[str, Path]]:
    found: dict[str, Path] = {}
    h = hashlib.sha256(f"import:{IMPORT_VERSION}\n".encode())
    for rel in EXPORT_FILES:
        p = root / rel
        if p.is_file():
            st = p.stat()
            found[rel] = p
            h.update(f"{rel}:{st.st_size}:{st.st_mtime_ns}\n".encode())
    return h.hexdigest(), found


def import_dir(conn: sqlite3.Connection, root: Path, force: bool = False) -> dict | None:
    """Import an unpacked export folder. Returns counts, or None when absent/unchanged."""
    try:
        if not root.is_dir():
            return None
        fp, found = _fingerprint(root)
    except OSError:
        return None
    if not found or not ({"watched.csv", "ratings.csv"} & set(found)):
        return None
    if not force and get_meta(conn, "lb_dir_fingerprint") == fp:
        return None
    files = {rel: p.read_bytes() for rel, p in found.items() if p.stat().st_size <= MAX_MEMBER_BYTES}
    counts = apply_export(conn, parse_export(files))
    set_meta(conn, "lb_dir_fingerprint", fp)
    return counts


def read_zip(data: bytes) -> dict[str, bytes]:
    """Safely read the known CSVs out of an export ZIP, entirely in memory."""
    if len(data) > MAX_ZIP_BYTES:
        raise ExportError("File too large")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise ExportError("Not a valid ZIP file") from e
    infos = zf.infolist()
    if len(infos) > 500:
        raise ExportError("Too many entries in ZIP")
    wanted: dict[str, zipfile.ZipInfo] = {}
    for info in infos:
        name = info.filename.replace("\\", "/")
        pp = PurePosixPath(name)
        if pp.is_absolute() or ".." in pp.parts:
            raise ExportError("Unsafe path in ZIP")
        if info.is_dir():
            continue
        parts = [p.lower() for p in pp.parts]
        # accept an optional single wrapper folder
        for rel in EXPORT_FILES:
            rp = rel.split("/")
            if parts[-len(rp):] == rp and len(parts) - len(rp) <= 1:
                wanted.setdefault(rel, info)
    files: dict[str, bytes] = {}
    total = 0
    for rel, info in wanted.items():
        if info.file_size > MAX_MEMBER_BYTES:
            raise ExportError("ZIP member too large")
        with zf.open(info) as fh:
            chunk = fh.read(MAX_MEMBER_BYTES + 1)
        if len(chunk) > MAX_MEMBER_BYTES:
            raise ExportError("ZIP member too large")
        total += len(chunk)
        if total > MAX_TOTAL_BYTES:
            raise ExportError("ZIP content too large")
        files[rel] = chunk
    if not ({"watched.csv", "ratings.csv"} & set(files)):
        raise ExportError("This does not look like a Letterboxd export (watched.csv / ratings.csv missing)")
    return files


def import_zip(conn: sqlite3.Connection, data: bytes) -> dict:
    return apply_export(conn, parse_export(read_zip(data)))


# --------------------------------------------------------------------------- RSS

def parse_rss(xml: bytes) -> list[dict]:
    try:
        root = ET.fromstring(xml)
    except Exception as e:  # defusedxml raises several types
        raise ExportError("Invalid RSS feed") from e
    out = []
    for item in root.iter("item"):
        def t(tag):
            el = item.find(tag, NS)
            return el.text.strip() if el is not None and el.text else None

        title = t("lb:filmTitle")
        if not title:
            continue
        rating = to_float(t("lb:memberRating"))
        out.append({
            "title": title,
            "year": to_int(t("lb:filmYear")),
            "rating": rating,
            "liked": (t("lb:memberLike") or "").lower() == "yes",
            "watched_date": t("lb:watchedDate"),
            "tmdb_id": to_int(t("tmdb:movieId")),
        })
    return out


def apply_rss(conn: sqlite3.Connection, entries: list[dict]) -> int:
    changed = 0
    now = now_iso()
    for e in entries:
        nt = norm_title(e["title"])
        row = None
        if e["tmdb_id"]:
            row = conn.execute("SELECT * FROM lb_films WHERE tmdb_id=?", (e["tmdb_id"],)).fetchone()
        if row is None:
            row = _find_row(conn, None, nt, e["year"])
        if row is None:
            conn.execute(
                "INSERT INTO lb_films(name,year,norm_title,tmdb_id,resolve_state,watched,rating,liked,watchlist,"
                "watched_date,updated_at) VALUES(?,?,?,?,?,1,?,?,0,?,?)",
                (e["title"], e["year"], nt, e["tmdb_id"], 1 if e["tmdb_id"] else 0,
                 e["rating"], int(e["liked"]), e["watched_date"], now),
            )
            changed += 1
            continue
        old_date = row["watched_date"]
        newer = not old_date or not e["watched_date"] or e["watched_date"] >= old_date
        rating = e["rating"] if (e["rating"] is not None and newer) else row["rating"]
        date = max([d for d in (old_date, e["watched_date"]) if d], default=None)
        tmdb_id = row["tmdb_id"] or e["tmdb_id"]
        liked = int(bool(row["liked"]) or e["liked"])
        if (rating, date, liked, tmdb_id) != (row["rating"], old_date, row["liked"], row["tmdb_id"]) \
                or not row["watched"] or row["watchlist"]:
            changed += 1
        conn.execute(
            "UPDATE lb_films SET watched=1, watchlist=0, rating=?, liked=?, watched_date=?, tmdb_id=?,"
            " resolve_state=CASE WHEN ? IS NOT NULL THEN 1 ELSE resolve_state END, updated_at=? WHERE id=?",
            (rating, liked, date, tmdb_id, tmdb_id, now, row["id"]),
        )
    set_meta(conn, "lb_last_sync", now)
    return changed


async def fetch_rss(client: httpx.AsyncClient, user: str) -> bytes:
    if not user or not all(c.isalnum() or c in "_-" for c in user):
        raise ExportError("Invalid Letterboxd username")
    r = await client.get(
        f"https://letterboxd.com/{user}/rss/",
        headers={"User-Agent": BROWSER_UA, "Accept": "application/rss+xml,text/xml,*/*"},
        timeout=httpx.Timeout(15.0),
        follow_redirects=True,
    )
    r.raise_for_status()
    return r.content[: 5 * 1024 * 1024]
