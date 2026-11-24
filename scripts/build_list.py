#!/usr/bin/env python3
"""Build a bundled Completionist list (src/movienight/data/lists/<key>.json) from a TSV of films. Dev only.

    MOVIENIGHT_TMDB_TOKEN=... python scripts/build_list.py scripts/lists/oscars-best-picture.tsv \\
        --key oscars-best-picture --name "Oscars · Best Picture"

The TSV has a header and the columns `title`, `year`, and optionally `rank` (a ranked poll, ties allowed) and
`tmdb_id` (pins the TMDB film when a title search is ambiguous). Each row is resolved with TMDB's /search/movie
(the year, then year ±1, then any year) and must match the title (or original title) and the year ±1; anything
less exact is printed to stderr for review. Standard library only, so it also runs where the token lives
(`python - <args> < scripts/build_list.py` cannot read the TSV from stdin then: use --rows-json).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://api.themoviedb.org/3"
OUT_DIR = Path(__file__).resolve().parent.parent / "src" / "movienight" / "data" / "lists"


def norm(s: str | None) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower().replace("&", "and")
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    return re.sub(r"^(the|a|an|la|le|l|les) ", "", s)


def get(path: str, params: dict, token: str) -> dict:
    params = dict(params)
    headers = {"Accept": "application/json"}
    if token.startswith("eyJ"):
        headers["Authorization"] = f"Bearer {token}"
    else:
        params["api_key"] = token
    url = f"{API}{path}?{urllib.parse.urlencode(params)}"
    for attempt in range(5):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=20) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                time.sleep(1 + attempt)
                continue
            raise
    raise RuntimeError(f"TMDB kept failing for {path}")


def year_of(m: dict) -> int | None:
    d = m.get("release_date") or ""
    return int(d[:4]) if d[:4].isdigit() else None


def resolve(title: str, year: int, token: str) -> tuple[dict | None, str]:
    """The TMDB film for (title, year) and how sure: 'exact' (title and year ±1), or 'loose' (best guess)."""
    want = norm(title)
    loose = None
    for y in (year, year - 1, year + 1, None):
        params = {"query": title, "include_adult": "false"}
        if y:
            params["primary_release_year"] = y
        results = get("/search/movie", params, token).get("results") or []
        for m in results:
            close = year_of(m) is not None and abs(year_of(m) - year) <= 1
            if close and want in (norm(m.get("title")), norm(m.get("original_title"))):
                return m, "exact"
        if loose is None:
            loose = next((m for m in results if year_of(m) and abs(year_of(m) - year) <= 1), None)
    return loose, "loose"


def entry(m: dict, rank: int | None) -> dict:
    out = {"tmdb_id": m["id"], "title": m.get("title") or "", "year": year_of(m),
           "poster_path": m.get("poster_path"), "backdrop_path": m.get("backdrop_path"),
           "release_date": m.get("release_date") or "", "vote_count": m.get("vote_count") or 0,
           "vote_average": m.get("vote_average"),
           "genre_ids": m.get("genre_ids") or [g["id"] for g in m.get("genres") or []]}
    if rank is not None:
        out["rank"] = rank
    return out


def build(rows: list[dict], key: str, name: str, ordered: bool, token: str) -> dict:
    films, ids = [], set()
    for row in rows:
        title, year = row["title"].strip(), int(row["year"])
        rank = int(row["rank"]) if str(row.get("rank") or "").strip() else None
        pinned = str(row.get("tmdb_id") or "").strip()
        if pinned:
            m, how = get(f"/movie/{int(pinned)}", {}, token), "pinned"
        else:
            m, how = resolve(title, year, token)
        if m is None:
            print(f"NOT FOUND  {title} ({year})", file=sys.stderr)
            continue
        if how != "exact":
            print(f"{how.upper():10} {title} ({year}) -> {m.get('title')} ({year_of(m)}) tmdb {m['id']}", file=sys.stderr)
        if m["id"] in ids:
            print(f"DUPLICATE  {title} ({year}) -> tmdb {m['id']}", file=sys.stderr)
            continue
        ids.add(m["id"])
        films.append(entry(m, rank))
    return {"key": key, "name": name, "ordered": ordered, "films": films}


def read_rows(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f, delimiter="\t" if path.suffix == ".tsv" else ","))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("input", nargs="?", type=Path, help="TSV/CSV with title, year[, rank][, tmdb_id]")
    ap.add_argument("--rows-json", help="the rows as a JSON list instead of a file")
    ap.add_argument("--key", required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--ordered", action="store_true", help="the list has an order of its own")
    ap.add_argument("--out", help="output path, '-' for stdout (default: the package's data/lists/<key>.json)")
    a = ap.parse_args(argv)
    token = (os.environ.get("MOVIENIGHT_TMDB_TOKEN") or "").strip()
    if not token:
        ap.error("set MOVIENIGHT_TMDB_TOKEN")
    rows = json.loads(a.rows_json) if a.rows_json else read_rows(a.input)
    data = build(rows, a.key, a.name, a.ordered, token)
    text = json.dumps(data, ensure_ascii=False, indent=1) + "\n"
    if a.out == "-":
        sys.stdout.write(text)
    else:
        out = Path(a.out) if a.out else OUT_DIR / f"{a.key}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"{len(data['films'])} films -> {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
