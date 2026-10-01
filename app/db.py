"""SQLite storage. One small file in the data folder holds everything."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);

-- Films Vue lists at the cinema (now showing + coming soon)
CREATE TABLE IF NOT EXISTS vue_films (
    film_id TEXT PRIMARY KEY,
    title TEXT, original_title TEXT, release_date TEXT, status INTEGER,
    distributor TEXT, director TEXT, runtime INTEGER, cert TEXT,
    url TEXT, poster TEXT, attrs TEXT, kind TEXT,
    first_seen TEXT, last_seen TEXT,
    tmdb_id INTEGER, tmdb_override INTEGER DEFAULT 0, match_note TEXT
);

-- Every individual showing we have ever seen (used for trends and ticket auto-detect)
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY, film_id TEXT, start TEXT, screen TEXT,
    formats TEXT, sold_out INTEGER, booking_url TEXT, first_seen TEXT, last_seen TEXT
);
CREATE INDEX IF NOT EXISTS sessions_film ON sessions(film_id, start);

-- One row per film per day: how many showings are listed. The trend tells us when a film is fading.
CREATE TABLE IF NOT EXISTS snapshots (
    day TEXT, film_id TEXT, sessions_next7 INTEGER, sessions_total INTEGER, last_session TEXT,
    PRIMARY KEY (day, film_id)
);

CREATE TABLE IF NOT EXISTS movies (tmdb_id INTEGER PRIMARY KEY, data TEXT, fetched_at TEXT);
CREATE TABLE IF NOT EXISTS community (tmdb_id INTEGER PRIMARY KEY, lb_avg REAL, fetched_at TEXT);
-- What Last Showing predicted for each film before you saw it (the first prediction is kept), so it can be
-- checked against your rating later
CREATE TABLE IF NOT EXISTS predictions (
    tmdb_id INTEGER PRIMARY KEY, film_id TEXT, title TEXT, predicted REAL, fav_chance REAL, confidence TEXT,
    community REAL, recorded_on TEXT
);
-- Early reviews (critics) for films at your cinema
CREATE TABLE IF NOT EXISTS reviews (
    tmdb_id INTEGER PRIMARY KEY, imdb_id TEXT, rt INTEGER, metascore INTEGER, imdb_rating REAL,
    guardian_stars INTEGER, guardian_url TEXT, fetched_at TEXT
);
-- Buzz over time: one row per film per day it was checked (lets us learn later what pre-release buzz means)
CREATE TABLE IF NOT EXISTS buzz_log (
    tmdb_id INTEGER, day TEXT, lb_avg REAL, rating_count INTEGER, watched INTEGER, lists INTEGER, likes INTEGER,
    tmdb_popularity REAL, PRIMARY KEY (tmdb_id, day)
);
CREATE TABLE IF NOT EXISTS title_map (key TEXT PRIMARY KEY, tmdb_id INTEGER);

-- Letterboxd data, keyed on TMDB id
CREATE TABLE IF NOT EXISTS ratings (tmdb_id INTEGER PRIMARY KEY, rating REAL, rated_on TEXT, source TEXT);
CREATE TABLE IF NOT EXISTS diary (
    entry_key TEXT PRIMARY KEY, tmdb_id INTEGER, title TEXT, year INTEGER,
    watched_date TEXT, rating REAL, rewatch INTEGER, source TEXT
);
CREATE TABLE IF NOT EXISTS watchlist (tmdb_id INTEGER PRIMARY KEY, title TEXT);
CREATE TABLE IF NOT EXISTS watched (tmdb_id INTEGER PRIMARY KEY);

-- "I'm seeing this": a film you've committed a ticket to in a given month
CREATE TABLE IF NOT EXISTS pins (film_id TEXT PRIMARY KEY, month TEXT, created_at TEXT);
-- "Want to see": films you're set on seeing; they get your free tickets first, then paid trips if needed
CREATE TABLE IF NOT EXISTS wants (film_id TEXT PRIMARY KEY, tmdb_id INTEGER, title TEXT, created_at TEXT);
-- "Not for me": films you never want suggested
CREATE TABLE IF NOT EXISTS dismissed (film_id TEXT PRIMARY KEY, tmdb_id INTEGER, title TEXT, created_at TEXT);

CREATE TABLE IF NOT EXISTS ticket_uses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    month TEXT, film_id TEXT, tmdb_id INTEGER, title TEXT, used_on TEXT,
    source TEXT, active INTEGER DEFAULT 1, created_at TEXT
);
"""


class DB:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.RLock()
        self._conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Add columns introduced after a database was first created."""
        added = {"community": [("rating_count", "INTEGER"), ("watched", "INTEGER"), ("lists", "INTEGER"),
                               ("likes", "INTEGER")]}
        for table, cols in added.items():
            have = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})").fetchall()}
            for name, kind in cols:
                if name not in have:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")

    def q(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        rows = self.q(sql, params)
        return rows[0] if rows else None

    def x(self, sql: str, params: tuple | dict = ()) -> int:
        with self._lock:
            cur = self._conn.execute(sql, params)
            return cur.lastrowid

    def many(self, sql: str, seq) -> None:
        with self._lock:
            self._conn.executemany(sql, seq)

    @contextmanager
    def tx(self) -> Iterator[None]:
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                yield
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def backup_to(self, dest) -> None:
        """A consistent copy of the whole database, safe while it's in use."""
        target = sqlite3.connect(str(dest))
        try:
            with self._lock:
                self._conn.backup(target)
        finally:
            target.close()

    # key/value helpers
    def get(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(row["value"]) if row else default

    def set(self, key: str, value: Any) -> None:
        self.x("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (key, json.dumps(value, default=str)))
