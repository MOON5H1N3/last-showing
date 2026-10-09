"""Undo for the last button you pressed.

Before any action on a film (Booked, Want to see, a month, Remove, Hide, Give it back, the showing, "was paid"), the
film's rows are copied: wants, pins, hidden, ticket uses and cinema trips. Undo puts exactly those rows back, so it
works the same for every button without each one needing its own reverse. Only the most recent action can be undone.
"""
from __future__ import annotations

import secrets
from datetime import datetime

from .db import DB

TABLES = ("wants", "pins", "dismissed", "ticket_uses", "cinema_trips")


def _trip_key(db: DB, film_id: str) -> str | None:
    from . import trips
    row = db.one("SELECT title, tmdb_id FROM vue_films WHERE film_id=?", (film_id,))
    return trips._key(row["tmdb_id"], row["title"]) if row else None


def _rows(db: DB, table: str, film_id: str, key: str | None) -> list[dict]:
    if table == "cinema_trips":
        return [dict(r) for r in db.q("SELECT * FROM cinema_trips WHERE film_id=? OR key=?", (film_id, key or ""))]
    return [dict(r) for r in db.q(f"SELECT * FROM {table} WHERE film_id=?", (film_id,))]


def save(db: DB, film_id: str | None) -> str | None:
    """Copy the film's rows before an action; returns the token the Undo button carries."""
    if not film_id:
        return None
    from . import trips
    trips.ensure(db)
    key = _trip_key(db, film_id)
    token = secrets.token_urlsafe(8)
    db.set("undo", {"token": token, "film_id": film_id, "key": key, "at": datetime.now().isoformat(timespec="seconds"),
                    "rows": {t: _rows(db, t, film_id, key) for t in TABLES}})
    return token


def current(db: DB, token: str | None) -> bool:
    return bool(token) and (db.get("undo") or {}).get("token") == token


def restore(db: DB, token: str) -> str | None:
    """Put the film's rows back as they were. Returns the film_id, or None if that action can't be undone any more."""
    snap = db.get("undo") or {}
    if not token or snap.get("token") != token:
        return None
    fid, key = snap["film_id"], snap.get("key")
    with db.tx():
        for t in TABLES:
            if t == "cinema_trips":
                db.x("DELETE FROM cinema_trips WHERE film_id=? OR key=?", (fid, key or ""))
            else:
                db.x(f"DELETE FROM {t} WHERE film_id=?", (fid,))
            for r in snap["rows"].get(t, []):
                cols = list(r)
                db.x(f"INSERT OR REPLACE INTO {t}({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                     tuple(r[c] for c in cols))
    db.set("undo", {})
    return fid
