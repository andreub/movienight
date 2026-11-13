"""SQLite schema, connection helper and small key/value helpers."""
from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

MIGRATIONS = [
    # 1: initial schema
    """
    CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE IF NOT EXISTS lb_films (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        year INTEGER,
        norm_title TEXT NOT NULL,
        uri TEXT,
        tmdb_id INTEGER,
        resolve_state INTEGER NOT NULL DEFAULT 0,   -- 0 pending, 1 resolved, 2 not found
        watched INTEGER NOT NULL DEFAULT 0,
        rating REAL,
        liked INTEGER NOT NULL DEFAULT 0,
        watchlist INTEGER NOT NULL DEFAULT 0,
        watched_date TEXT,
        updated_at TEXT
    );
    CREATE UNIQUE INDEX IF NOT EXISTS lb_films_uri ON lb_films(uri) WHERE uri IS NOT NULL;
    CREATE INDEX IF NOT EXISTS lb_films_ny ON lb_films(norm_title, year);
    CREATE INDEX IF NOT EXISTS lb_films_tmdb ON lb_films(tmdb_id);
    CREATE TABLE IF NOT EXISTS tmdb_details (
        tmdb_id INTEGER PRIMARY KEY, json TEXT NOT NULL, fetched_at TEXT);
    CREATE TABLE IF NOT EXISTS tmdb_recs (
        tmdb_id INTEGER PRIMARY KEY, json TEXT NOT NULL, fetched_at TEXT);
    CREATE TABLE IF NOT EXISTS plex_servers (
        id TEXT PRIMARY KEY,
        name TEXT,
        owned INTEGER NOT NULL DEFAULT 0,
        enabled INTEGER NOT NULL DEFAULT 1,
        reachable INTEGER NOT NULL DEFAULT 0,
        access_token TEXT,
        base_url TEXT,
        movies INTEGER NOT NULL DEFAULT 0,
        last_sync TEXT
    );
    CREATE TABLE IF NOT EXISTS movies (
        id INTEGER PRIMARY KEY,
        dkey TEXT NOT NULL UNIQUE,
        tmdb_id INTEGER
    );
    CREATE TABLE IF NOT EXISTS plex_items (
        server_id TEXT NOT NULL,
        rating_key TEXT NOT NULL,
        movie_id INTEGER,
        title TEXT,
        year INTEGER,
        tmdb_id INTEGER,
        imdb_id TEXT,
        json TEXT NOT NULL,
        PRIMARY KEY (server_id, rating_key)
    );
    CREATE INDEX IF NOT EXISTS plex_items_movie ON plex_items(movie_id);
    CREATE TABLE IF NOT EXISTS dismissals (
        movie_id INTEGER PRIMARY KEY,
        until TEXT,            -- NULL = forever
        created_at TEXT
    );
    """,
    # 2: feedback loop (thumbs, prediction tracking, outcomes). Keys are `tmdb:<id>` when the TMDB id
    # is known, else the library's dedupe key (movies.dkey), so they survive library re-syncs.
    """
    CREATE TABLE IF NOT EXISTS votes (
        key TEXT PRIMARY KEY,
        vote INTEGER NOT NULL,          -- 1 or -1 (0 deletes the row)
        updated_at TEXT
    );
    CREATE TABLE IF NOT EXISTS predictions (
        key TEXT PRIMARY KEY,
        tmdb_id INTEGER,
        norm_title TEXT NOT NULL,
        year INTEGER,
        title TEXT NOT NULL,
        match INTEGER NOT NULL,
        first_at TEXT NOT NULL,         -- when the film was first predicted
        updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS predictions_tmdb ON predictions(tmdb_id);
    CREATE INDEX IF NOT EXISTS predictions_ny ON predictions(norm_title, year);
    CREATE TABLE IF NOT EXISTS outcomes (
        id INTEGER PRIMARY KEY,
        key TEXT NOT NULL UNIQUE,
        lb_id INTEGER,
        tmdb_id INTEGER,
        title TEXT NOT NULL,
        year INTEGER,
        predicted INTEGER NOT NULL,
        rating REAL,
        liked INTEGER NOT NULL DEFAULT 0,
        watched_date TEXT,
        recorded_at TEXT NOT NULL
    );
    """,
]


def connect(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=8000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def session(path: Path | str):
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def migrate(path: Path | str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        for i, script in enumerate(MIGRATIONS, start=1):
            if i > version:
                conn.executescript(script)
                conn.execute(f"PRAGMA user_version={i}")
        conn.commit()
    finally:
        conn.close()


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str | None) -> None:
    if value is None:
        conn.execute("DELETE FROM meta WHERE key=?", (key,))
    else:
        conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def client_id(conn: sqlite3.Connection) -> str:
    cid = get_meta(conn, "plex_client_id")
    if not cid:
        cid = uuid.uuid4().hex
        set_meta(conn, "plex_client_id", cid)
        conn.commit()
    return cid
