"""Watch the films you've pinned or marked Want to see, and tell you when one stops (or starts) holding a ticket.

A film stops holding a ticket when Vue no longer lists it at your cinema, or when its showtimes were due (the Tuesday
before it opens) and none came. Kept quiet on purpose:

* only films you pinned or want, never recommendations;
* only when something changes, never a daily reminder;
* it has to be missing on two refreshes in a row before it counts, so a one-morning blip at Vue sends nothing;
* everything from one refresh goes out in one DM, alongside any break alerts.

Your pin and Want to see are kept, so a film that comes back picks up where it left off.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from .db import DB
from .planner import times_overdue

MISSES_BEFORE_NOTICE = 2
NOTICE_DAYS = 7  # how long a notice stays on the plan page


def _followed(db: DB, today: date) -> dict[str, str]:
    """film_id -> what you did ("your October pin" / "your Want to see"), for films that should be holding a ticket."""
    used = {r["film_id"] for r in db.q("SELECT film_id FROM ticket_uses WHERE active=1 AND film_id IS NOT NULL")}
    used_tmdb = {r["tmdb_id"] for r in db.q("SELECT tmdb_id FROM ticket_uses WHERE active=1 AND tmdb_id IS NOT NULL")}
    seen = {r["tmdb_id"] for r in db.q("SELECT tmdb_id FROM watched")} | used_tmdb
    month = today.strftime("%Y-%m")
    out: dict[str, str] = {}
    for r in db.q("SELECT film_id, tmdb_id FROM wants"):
        if r["film_id"] not in used and r["tmdb_id"] not in seen:
            out[r["film_id"]] = "your Want to see"
    for r in db.q("SELECT film_id, month FROM pins"):
        if r["film_id"] not in used and (r["month"] or "") >= month:
            label = datetime.strptime(r["month"], "%Y-%m").strftime("%B")
            out[r["film_id"]] = f"your {label} pin"
    return out


def check(db: DB, cinema: str, now: datetime) -> list[str]:
    """Call after a successful Vue listing. Returns the DM lines for anything that changed."""
    today = now.date()
    listed = set(db.get("vue_listed_film_ids", []))
    last_listing = db.get("vue_last_listing") or ""
    followed = _followed(db, today)
    misses: dict = db.get("watch_misses") or {}
    lost: dict = db.get("watch_lost") or {}
    notices: list = [n for n in (db.get("film_notices") or [])
                     if n.get("on", "") >= (today - timedelta(days=NOTICE_DAYS)).isoformat()]
    where = cinema or "your cinema"
    lines: list[str] = []

    for fid in list(misses) + list(lost):  # unpinned or seen since: forget it quietly
        if fid not in followed:
            misses.pop(fid, None)
            lost.pop(fid, None)

    for fid, what in followed.items():
        row = db.one("SELECT title, kind, release_date FROM vue_films WHERE film_id=?", (fid,))
        if row is None:
            continue
        title = row["title"]
        if fid not in listed:
            why = f"is no longer listed at {where}"
        else:
            has_times = bool(db.one("SELECT 1 FROM sessions WHERE film_id=? AND last_seen>=? LIMIT 1", (fid, last_listing)))
            release = date.fromisoformat(row["release_date"]) if row["release_date"] else None
            why = (f"has no showtimes at {where}, and Vue has put out its times for the week it opens"
                   if row["kind"] == "film" and times_overdue(release, has_times, today) else "")
        if why:
            misses[fid] = misses.get(fid, 0) + 1
            if misses[fid] >= MISSES_BEFORE_NOTICE and fid not in lost:
                lost[fid] = {"title": title, "since": today.isoformat()}
                text = (f"**{title}** {why}, so {what} isn't holding a ticket now. It's kept: if the film comes back, "
                        "it'll be in your plan again.")
                lines.append(text)
                notices.append({"film_id": fid, "kind": "gone", "on": today.isoformat(), "text": text})
        else:
            misses.pop(fid, None)
            if fid in lost:
                lost.pop(fid)
                text = f"**{title}** is back at {where}, so {what} is active again."
                lines.append(text)
                notices = [n for n in notices if n.get("film_id") != fid]
                notices.append({"film_id": fid, "kind": "back", "on": today.isoformat(), "text": text})

    db.set("watch_misses", misses)
    db.set("watch_lost", lost)
    db.set("film_notices", notices)
    return lines


def current_notices(db: DB, today: date) -> list[dict]:
    cut = (today - timedelta(days=NOTICE_DAYS)).isoformat()
    return [n for n in (db.get("film_notices") or []) if n.get("on", "") >= cut]
