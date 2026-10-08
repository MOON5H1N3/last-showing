"""Ticket usage: automatic (from your Letterboxd diary) plus manual marking."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from .db import DB


def month_of(d: date) -> str:
    return d.strftime("%Y-%m")


def add_use(db: DB, film_id: str | None, tmdb_id: int | None, title: str, used_on: date, source: str) -> int:
    return db.x("""INSERT INTO ticket_uses(month,film_id,tmdb_id,title,used_on,source,active,created_at)
                   VALUES(?,?,?,?,?,?,1,?)""",
                (month_of(used_on), film_id, tmdb_id, title, used_on.isoformat(), source,
                 datetime.now().isoformat(timespec="seconds")))


def undo_use(db: DB, use_id: int) -> None:
    # kept (inactive) rather than deleted, so auto-detect won't re-add something you removed
    db.x("UPDATE ticket_uses SET active=0 WHERE id=?", (use_id,))


def undo_latest(db: DB, month: str) -> dict | None:
    row = db.one("SELECT * FROM ticket_uses WHERE month=? AND active=1 ORDER BY id DESC LIMIT 1", (month,))
    if row:
        undo_use(db, row["id"])
        return dict(row)
    return None


def active_uses(db: DB, month: str) -> list[dict]:
    return [dict(r) for r in db.q("SELECT * FROM ticket_uses WHERE month=? AND active=1 ORDER BY used_on", (month,))]


def auto_detect(db: DB, today: date, tickets_per_month: int = 2) -> list[dict]:
    """A diary entry this month for a film that was showing at your Vue that day (±1 day) counts as a ticket.
    Once all tickets are used, further cinema trips are assumed to be paid for and aren't counted."""
    month = month_of(today)
    start = today.replace(day=1).isoformat()
    added = []
    entries = db.q("SELECT * FROM diary WHERE watched_date >= ? AND watched_date <= ? ORDER BY watched_date",
                   (start, today.isoformat()))
    for e in entries:
        if not e["tmdb_id"] or len(active_uses(db, month)) >= tickets_per_month:
            continue
        films = db.q("SELECT film_id, title FROM vue_films WHERE tmdb_id=?", (e["tmdb_id"],))
        wd = date.fromisoformat(e["watched_date"])
        for f in films:
            if db.one("SELECT 1 FROM ticket_uses WHERE month=? AND (film_id=? OR tmdb_id=?)",
                      (month, f["film_id"], e["tmdb_id"])):
                break  # already counted, or you removed it before
            lo, hi = (wd - timedelta(days=1)).isoformat(), (wd + timedelta(days=2)).isoformat()
            if db.one("SELECT 1 FROM sessions WHERE film_id=? AND start >= ? AND start < ?", (f["film_id"], lo, hi)):
                add_use(db, f["film_id"], e["tmdb_id"], f["title"], wd, "letterboxd")
                added.append({"title": f["title"], "used_on": wd.isoformat()})
                break
    return added


def seen_feed(db: DB) -> dict:
    """What you've seen, for other apps (Worth Keeping uses it to rank films worth owning):
    Monzo tickets used, every cinema trip from your Vue tickets, and how often you've watched each film from your
    Letterboxd diary."""
    tickets = [dict(r) for r in db.q(
        "SELECT tmdb_id, title, used_on FROM ticket_uses WHERE active=1 AND tmdb_id IS NOT NULL ORDER BY used_on")]
    diary = [dict(r) for r in db.q(
        "SELECT tmdb_id, MAX(title) AS title, MAX(year) AS year, COUNT(*) AS watches, MAX(rating) AS rating, "
        "MAX(watched_date) AS last FROM diary WHERE tmdb_id IS NOT NULL GROUP BY tmdb_id")]
    from .trips import ensure
    ensure(db)
    trips = [dict(r) for r in db.q(  # every film you went to the cinema for, from your Vue tickets (each once)
        "SELECT tmdb_id, title, visited_on, kind FROM cinema_trips ORDER BY visited_on")]
    return {"tickets": tickets, "diary": diary, "trips": trips}
