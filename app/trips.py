"""Cinema trips: what you actually chose to see on the big screen.

They come in three ways: when you use a ticket (and pick the showing you booked, so the time, screen and format are
known); from your Letterboxd diary, for a film that was showing at your cinema that day; and, for history, as lines
of "date, film" read from the Vue app's My tickets list. Each film is kept once, at its first visit. They're used to:

* count Monzo tickets for months Last Showing has been running (never past the month's allowance, and never a film
  you already counted or gave back);
* mark the film as seen, so it isn't recommended again;
* learn what you pick for the cinema (the big-screen review in stage 9).
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta

from . import tickets
from .db import DB
from .tmdb import clean_vue_title, norm

SCHEMA = """
CREATE TABLE IF NOT EXISTS cinema_trips (
    key TEXT PRIMARY KEY, title TEXT, tmdb_id INTEGER, film_id TEXT, visited_on TEXT, cinema TEXT,
    kind TEXT, source TEXT, created_at TEXT
);
"""

MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


SHOWING_COLS = {"session_id": "TEXT", "start": "TEXT", "screen": "TEXT", "formats": "TEXT"}


def ensure(db: DB) -> None:
    for stmt in SCHEMA.strip().split(";"):
        if stmt.strip():
            db.x(stmt)
    have = {r["name"] for r in db.q("PRAGMA table_info(cinema_trips)")}
    for col, kind in SHOWING_COLS.items():  # the showing you booked, when you picked it
        if col not in have:
            db.x(f"ALTER TABLE cinema_trips ADD COLUMN {col} {kind}")


def _key(tmdb_id: int | None, title: str) -> str:
    return f"tmdb:{tmdb_id}" if tmdb_id else f"title:{norm(clean_vue_title(title)[0])}"


def record(db: DB, film_id: str | None, tmdb_id: int | None, title: str, visited: date, source: str,
           cinema: str = "") -> bool:
    """A trip, if this film isn't already one (each film once, at its first visit). True if it was added."""
    ensure(db)
    key = _key(tmdb_id, title)
    if db.one("SELECT 1 FROM cinema_trips WHERE key=?", (key,)):
        return False
    kind = "film"
    if film_id:
        row = db.one("SELECT kind FROM vue_films WHERE film_id=?", (film_id,))
        kind = (row["kind"] if row else None) or "film"
    db.x("""INSERT INTO cinema_trips(key,title,tmdb_id,film_id,visited_on,cinema,kind,source,created_at)
            VALUES(?,?,?,?,?,?,?,?,?)""", (key, clean_vue_title(title)[0], tmdb_id, film_id, visited.isoformat(), cinema,
                                           kind, source, datetime.now().isoformat(timespec="seconds")))
    if tmdb_id:
        db.x("INSERT OR IGNORE INTO watched(tmdb_id) VALUES(?)", (tmdb_id,))
    return True


def showings(db: DB, film_id: str, today: date, back: int = 3, ahead: int = 21) -> list[dict]:
    """Showings to choose from: the last few days (if you mark it afterwards) and the next three weeks."""
    lo, hi = (today - timedelta(days=back)).isoformat(), (today + timedelta(days=ahead + 1)).isoformat()
    last = (db.get("vue_last_listing") or "")[:10]
    now = today.isoformat()
    out = []
    # a showing still to come must be in the latest listing (Vue drops cancelled ones); past ones were listed back then
    for r in db.q("""SELECT session_id, start, screen, formats FROM sessions WHERE film_id=? AND start>=? AND start<?
                     AND (start<? OR last_seen>=?) ORDER BY start""", (film_id, lo, hi, now, last)):
        fmts = [f for f in json.loads(r["formats"] or "[]") if f]
        when = datetime.fromisoformat(r["start"])
        label = when.strftime("%a %-d %b, %H:%M") + (" · " + ", ".join(f.upper() if len(f) <= 4 else f.title()
                                                                        for f in fmts) if fmts else "")
        out.append({"session_id": r["session_id"], "start": r["start"], "screen": r["screen"], "formats": fmts,
                    "label": label})
    return out


def set_showing(db: DB, session_id: str) -> dict | None:
    """You picked the showing you booked: the trip gets its date, time, screen and format."""
    ensure(db)
    s = db.one("SELECT session_id, film_id, start, screen, formats FROM sessions WHERE session_id=?", (session_id,))
    if not s:
        return None
    f = db.one("SELECT title, tmdb_id FROM vue_films WHERE film_id=?", (s["film_id"],))
    title, tid = (f["title"], f["tmdb_id"]) if f else (s["film_id"], None)
    when = datetime.fromisoformat(s["start"])
    key = _key(tid, title)
    trip = db.one("SELECT visited_on, start FROM cinema_trips WHERE key=?", (key,))
    if not trip:
        record(db, s["film_id"], tid, title, when.date(), "ticket")
    elif trip["visited_on"][:7] != when.strftime("%Y-%m") and trip["start"]:
        return None  # a film you went to long ago keeps its first visit
    db.x("UPDATE cinema_trips SET visited_on=?, session_id=?, start=?, screen=?, formats=?, film_id=? WHERE key=?",
         (when.date().isoformat(), s["session_id"], s["start"], s["screen"], s["formats"], s["film_id"], key))
    return {"title": clean_vue_title(title)[0], "label": when.strftime("%a %-d %b, %H:%M"), "screen": s["screen"]}


def need_showing(db: DB, today: date) -> list[dict]:
    """Tickets used this month on a film whose showing you haven't picked yet, and whose times are now listed."""
    ensure(db)
    out = []
    for u in db.q("SELECT film_id, tmdb_id, title FROM ticket_uses WHERE month=? AND active=1 AND film_id IS NOT NULL",
                  (today.strftime("%Y-%m"),)):
        trip = db.one("SELECT start FROM cinema_trips WHERE key=?", (_key(u["tmdb_id"], u["title"]),))
        if trip and trip["start"]:
            continue
        opts = showings(db, u["film_id"], today)
        if opts:
            out.append({"film_id": u["film_id"], "title": clean_vue_title(u["title"])[0], "showings": opts})
    return out


def forget_ticket_trip(db: DB, film_id: str | None, tmdb_id: int | None, title: str, month: str) -> None:
    """You gave a ticket back: drop the trip it made, unless it came from somewhere else (diary, your Vue tickets)."""
    ensure(db)
    db.x("DELETE FROM cinema_trips WHERE key=? AND source='ticket' AND substr(visited_on,1,7)=?",
         (_key(tmdb_id, title), month))


def from_diary(db: DB, since: date, cinema: str = "") -> list[str]:
    """Backstop: a diary entry for a film that was showing at your cinema that day (or the day either side) is a trip."""
    ensure(db)
    added = []
    for e in db.q("SELECT tmdb_id, title, watched_date FROM diary WHERE tmdb_id IS NOT NULL AND watched_date>=?",
                  (since.isoformat(),)):
        wd = date.fromisoformat(e["watched_date"])
        lo, hi = (wd - timedelta(days=1)).isoformat(), (wd + timedelta(days=2)).isoformat()
        for f in db.q("SELECT film_id, title FROM vue_films WHERE tmdb_id=?", (e["tmdb_id"],)):
            if db.one("SELECT 1 FROM sessions WHERE film_id=? AND start>=? AND start<?", (f["film_id"], lo, hi)):
                if record(db, f["film_id"], e["tmdb_id"], f["title"], wd, "letterboxd", cinema):
                    added.append(clean_vue_title(f["title"])[0])
                break
    return added


def parse_date(text: str) -> date | None:
    text = text.strip()
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.search(r"(\d{1,2})\s+([A-Za-z]{3})[a-z]*\s+(\d{4})", text)  # "Tue 6 Oct 2026", as the Vue app shows it
    if m and m.group(2).lower() in MONTHS:
        return date(int(m.group(3)), MONTHS[m.group(2).lower()], int(m.group(1)))
    return None


def parse_lines(text: str) -> tuple[list[tuple[str, date]], list[str]]:
    """One ticket per line: '2026-10-06, Sense and Sensibility', 'Sense and Sensibility, Tue 6 Oct 2026', or a CSV
    with a header naming the date and film columns (like the list Claude made from your screenshots)."""
    import csv
    import io
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    rows, bad = [], []
    head = [h.strip().lower() for h in next(csv.reader([lines[0]]))] if lines else []
    if "date" in head and ({"film", "title"} & set(head)):
        col = head.index("film") if "film" in head else head.index("title")
        for rec in csv.reader(io.StringIO("\n".join(lines[1:]))):
            d = parse_date(rec[head.index("date")]) if len(rec) > col else None
            if d and rec[col].strip():
                rows.append((rec[col].strip(), d))
            else:
                bad.append(",".join(rec))
        return rows, bad
    day = r"(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*"
    for line in lines:
        line = line.strip()
        d = parse_date(line)
        title = re.sub(rf"\d{{4}}-\d{{2}}-\d{{2}}|(?:{day}\s+)?\d{{1,2}}\s+[A-Za-z]{{3}}\w*\s+\d{{4}}", "", line, flags=re.I)
        title = re.sub(r"\s*[|;\t]\s*", " ", title)
        title = re.sub(rf"^[\s,-]*(?:{day}\b[\s,]*)?|[\s,-]+$", "", title, flags=re.I).strip().strip('"')
        if d and title:
            rows.append((title, d))
        else:
            bad.append(line)
    return rows, bad


def is_rerelease(title: str, visited: date) -> bool:
    """'Baby Driver (2017)' seen in 2025 is a re-release; 'Barbie (2023)' seen in 2023 isn't."""
    _, year = clean_vue_title(title)
    return bool(year and year < visited.year - 1)


async def match(db: DB, tmdb, title: str, visited: date) -> tuple[int | None, str | None]:
    """A trip's film: first anything Last Showing already knows from your cinema's listings, then TMDB."""
    clean, year = clean_vue_title(title)
    for r in db.q("SELECT film_id, title, tmdb_id FROM vue_films WHERE tmdb_id IS NOT NULL"):
        if norm(clean_vue_title(r["title"])[0]) == norm(clean):
            return r["tmdb_id"], r["film_id"]
    if not tmdb:
        return None, None
    tid, kind = await tmdb.match_title(clean, year or visited.year)
    if not tid and not year:
        tid, kind = await tmdb.match_title(clean, visited.year - 1)  # seen early the next year
    return (tid if kind == "film" else None), None


async def import_trips(db: DB, tmdb, rows: list[tuple[str, date]], cinema: str, tickets_per_month: int,
                       first_month: str, source: str = "vue tickets") -> dict:
    ensure(db)
    stamp = datetime.now().isoformat(timespec="seconds")
    added, known, unmatched, counted = [], 0, [], []
    for title, visited in sorted(rows, key=lambda r: r[1]):  # earliest first, so a film keeps its first visit
        tid, film_id = await match(db, tmdb, title, visited)
        key = f"tmdb:{tid}" if tid else f"title:{norm(clean_vue_title(title)[0])}"
        if db.one("SELECT 1 FROM cinema_trips WHERE key=?", (key,)):
            known += 1
            continue
        kind = "rerelease" if is_rerelease(title, visited) else "film"
        clean = clean_vue_title(title)[0]
        db.x("""INSERT INTO cinema_trips(key,title,tmdb_id,film_id,visited_on,cinema,kind,source,created_at)
                VALUES(?,?,?,?,?,?,?,?,?)""", (key, clean, tid, film_id, visited.isoformat(), cinema, kind, source, stamp))
        added.append(clean)
        if not tid:
            unmatched.append(clean)
        else:
            db.x("INSERT OR IGNORE INTO watched(tmdb_id) VALUES(?)", (tid,))
        month = tickets.month_of(visited)
        if month >= first_month and tickets_per_month:
            already = db.one("""SELECT 1 FROM ticket_uses WHERE month=? AND ((film_id IS NOT NULL AND film_id=?)
                                OR (tmdb_id IS NOT NULL AND tmdb_id=?) OR lower(title)=lower(?))""",
                             (month, film_id, tid, clean))  # includes ones you gave back
            if not already and len(tickets.active_uses(db, month)) < tickets_per_month:
                tickets.add_use(db, film_id, tid, clean, visited, source)
                counted.append(f"{clean} ({visited.strftime('%-d %b')})")
    return {"added": len(added), "already": known, "unmatched": unmatched, "tickets": counted}


def summary(db: DB) -> dict:
    ensure(db)
    rows = [dict(r) for r in db.q("SELECT * FROM cinema_trips ORDER BY visited_on DESC")]
    return {"count": len(rows), "first": rows[-1]["visited_on"] if rows else None,
            "rereleases": sum(1 for r in rows if r["kind"] == "rerelease"),
            "unmatched": [r["title"] for r in rows if not r["tmdb_id"]], "recent": rows[:8]}
