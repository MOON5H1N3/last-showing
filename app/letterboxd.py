"""Letterboxd: your ratings, diary and watchlist.

* Full history comes from the official data export ZIP (Settings > Data > Export) dropped in /data/letterboxd.
* New activity comes from your public RSS feed, which also carries TMDB ids.
* Community averages are read from public film pages (a few per run, politely).
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import re
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime
from pathlib import Path

import httpx

from .db import DB

log = logging.getLogger(__name__)
IMPORT_VERSION = 2  # bump when matching changes, so the export is re-imported with the new rules
NS = {"letterboxd": "https://letterboxd.com", "tmdb": "https://themoviedb.org"}


def _rows(z: zipfile.ZipFile, name: str) -> list[dict]:
    """The export also has orphaned/ and deleted/ copies of some files; use the one nearest the top."""
    hits = [n for n in z.namelist() if n.lower().rsplit("/", 1)[-1] == name
            and not any(bad in n.lower() for bad in ("deleted/", "orphaned/"))]
    if not hits:
        return []
    with z.open(min(hits, key=lambda n: n.count("/"))) as fh:
        return list(csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8-sig")))


def _year(v: str | None) -> int | None:
    try:
        return int(v) if v else None
    except ValueError:
        return None


def _rating(v: str | None) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


def latest_export(folder: Path) -> Path | None:
    zips = sorted(folder.glob("*.zip"), key=lambda p: p.stat().st_mtime) if folder.exists() else []
    return zips[-1] if zips else None


async def import_export(db: DB, tmdb, zip_path: Path) -> dict:
    """Read a Letterboxd export ZIP. Titles are matched to TMDB (cached, so re-imports are quick)."""
    with zipfile.ZipFile(zip_path) as z:
        ratings, diary = _rows(z, "ratings.csv"), _rows(z, "diary.csv")
        watchlist, watched = _rows(z, "watchlist.csv"), _rows(z, "watched.csv")

    titles = {(r.get("Name", ""), _year(r.get("Year"))) for r in ratings + diary + watchlist + watched if r.get("Name")}
    titles = list(titles)
    ids = await asyncio.gather(*(tmdb.match_title(n, y) for n, y in titles), return_exceptions=True)
    tmap, kinds = {}, {}
    for t, res in zip(titles, ids):
        if isinstance(res, tuple):
            tmap[t], kinds[t] = res
        else:  # exceptions, or an older matcher returning just an id
            tmap[t] = res if isinstance(res, int) else None
            kinds[t] = "film" if tmap[t] else "none"

    def tid(r):
        return tmap.get((r.get("Name", ""), _year(r.get("Year"))))

    tv = sorted(f"{n} ({y})" for (n, y), k in kinds.items() if k == "tv")
    unmatched = sorted(f"{n} ({y})" for (n, y), k in kinds.items() if k == "none")
    with db.tx():
        # a re-import replaces what the last export put in (keeps anything that came from the RSS feed)
        db.x("DELETE FROM ratings WHERE source='export'")
        db.x("DELETE FROM diary WHERE source='export'")
        db.x("DELETE FROM watched")
        db.x("INSERT OR IGNORE INTO watched(tmdb_id) SELECT tmdb_id FROM diary")
        for r in ratings:
            t, val = tid(r), _rating(r.get("Rating"))
            if t and val is not None:
                db.x("""INSERT INTO ratings(tmdb_id,rating,rated_on,source) VALUES(?,?,?,'export')
                        ON CONFLICT(tmdb_id) DO UPDATE SET rating=excluded.rating, rated_on=excluded.rated_on, source='export'
                        WHERE excluded.rated_on >= ratings.rated_on""", (t, val, r.get("Date") or ""))
        for r in diary:
            t = tid(r)
            wd = r.get("Watched Date") or r.get("Date")
            if t and wd:
                db.x("""INSERT OR IGNORE INTO diary(entry_key,tmdb_id,title,year,watched_date,rating,rewatch,source)
                        VALUES(?,?,?,?,?,?,?,'export')""",
                     (f"{t}|{wd}", t, r.get("Name"), _year(r.get("Year")), wd, _rating(r.get("Rating")),
                      int((r.get("Rewatch") or "").lower() == "yes")))
        db.x("DELETE FROM watchlist")
        for r in watchlist:
            if tid(r):
                db.x("INSERT OR IGNORE INTO watchlist(tmdb_id,title) VALUES(?,?)", (tid(r), r.get("Name")))
        for r in watched + ratings + diary:
            if tid(r):
                db.x("INSERT OR IGNORE INTO watched(tmdb_id) VALUES(?)", (tid(r),))
    summary = {"file": zip_path.name, "ratings": len(ratings), "diary": len(diary), "watchlist": len(watchlist),
               "watched": len(watched), "unmatched": unmatched[:50], "unmatched_count": len(unmatched),
               "tv": tv[:50], "tv_count": len(tv), "version": IMPORT_VERSION,
               "imported_at": datetime.now().isoformat(timespec="seconds"), "mtime": zip_path.stat().st_mtime}
    db.set("letterboxd_import", summary)
    return summary


def parse_rss(xml_text: str) -> list[dict]:
    root = ET.fromstring(xml_text)
    out = []
    for item in root.iter("item"):
        title = item.findtext("letterboxd:filmTitle", namespaces=NS)
        if not title:
            continue  # lists and other non-diary items
        tid = item.findtext("tmdb:movieId", namespaces=NS)
        out.append({
            "guid": item.findtext("guid"),
            "title": title,
            "year": _year(item.findtext("letterboxd:filmYear", namespaces=NS)),
            "watched_date": item.findtext("letterboxd:watchedDate", namespaces=NS),
            "rating": _rating(item.findtext("letterboxd:memberRating", namespaces=NS)),
            "rewatch": (item.findtext("letterboxd:rewatch", namespaces=NS) or "").lower() == "yes",
            "tmdb_id": int(tid) if tid and tid.isdigit() else None,
            "pub": item.findtext("pubDate"),
        })
    return out


def store_rss(db: DB, entries: list[dict]) -> int:
    new = 0
    with db.tx():
        for e in entries:
            t = e["tmdb_id"]
            if not t:
                continue
            if e["watched_date"]:
                key = f"{t}|{e['watched_date']}"
                if not db.one("SELECT 1 FROM diary WHERE entry_key=?", (key,)):
                    new += 1
                db.x("""INSERT OR IGNORE INTO diary(entry_key,tmdb_id,title,year,watched_date,rating,rewatch,source)
                        VALUES(?,?,?,?,?,?,?,'rss')""",
                     (key, t, e["title"], e["year"], e["watched_date"], e["rating"], int(e["rewatch"])))
            db.x("INSERT OR IGNORE INTO watched(tmdb_id) VALUES(?)", (t,))
            if e["rating"] is not None:
                rated_on = e["watched_date"] or ""
                db.x("""INSERT INTO ratings(tmdb_id,rating,rated_on,source) VALUES(?,?,?,'rss')
                        ON CONFLICT(tmdb_id) DO UPDATE SET rating=excluded.rating, rated_on=excluded.rated_on, source='rss'
                        WHERE excluded.rated_on >= ratings.rated_on""", (t, e["rating"], rated_on))
            # something you've now logged is no longer on your watchlist
            db.x("DELETE FROM watchlist WHERE tmdb_id=?", (t,))
    return new


async def poll_rss(db: DB, username: str) -> int:
    async with httpx.AsyncClient(timeout=20, headers={"User-Agent": "last-showing/1.0 (+self-hosted)"},
                                 follow_redirects=True) as c:
        r = await c.get(f"https://letterboxd.com/{username}/rss/")
        r.raise_for_status()
    entries = parse_rss(r.text)
    n = store_rss(db, entries)
    db.set("letterboxd_rss", {"checked_at": datetime.now().isoformat(timespec="seconds"), "entries": len(entries)})
    return n


AVG_RE = re.compile(r"([0-9.]+) out of 5")
LD_VALUE_RE = re.compile(r'"ratingValue"\s*:\s*([0-9.]+)')
LD_COUNT_RE = re.compile(r'"ratingCount"\s*:\s*([0-9]+)')
STAT_RE = {
    "watched": re.compile(r"Watched by ([0-9,]+)"),
    "lists": re.compile(r"Appears in ([0-9,]+)"),
    "likes": re.compile(r"Liked by ([0-9,]+)"),
}


def parse_rating(html: str, twitter_meta: str | None = None) -> tuple[float | None, int | None]:
    """Average and number of ratings from a film page's structured data (the twitter tag as a fallback)."""
    v, n = LD_VALUE_RE.search(html or ""), LD_COUNT_RE.search(html or "")
    avg = float(v.group(1)) if v else None
    if avg is None and twitter_meta:
        m = AVG_RE.search(twitter_meta)
        avg = float(m.group(1)) if m else None
    return avg, (int(n.group(1)) if n else None)


SLUG_RE = re.compile(r"letterboxd\.com/film/([^/\"'?#]+)/")


def film_slug(url: str, html: str = "") -> str | None:
    """The film's Letterboxd slug, from where the page ended up or, failing that, its canonical link."""
    for text in (url or "", html or ""):
        m = SLUG_RE.search(text) or re.search(r"^/film/([^/]+)/", text)
        if m:
            return m.group(1)
    return None


def parse_stats(html: str) -> dict[str, int | None]:
    """Watched / lists / likes counts from Letterboxd's film stats fragment."""
    text = (html or "").replace("&nbsp;", " ").replace("\xa0", " ")
    out = {}
    for k, rx in STAT_RE.items():
        m = rx.search(text)
        out[k] = int(m.group(1).replace(",", "")) if m else None
    return out


async def fetch_community(db: DB, tmdb_ids: list[int], limit: int, buzz_ids: set[int] | None = None) -> int:
    """Letterboxd average and how many ratings it's based on, from the public film page
    (via letterboxd.com/tmdb/<id>). The lists / watches / likes box isn't read: Letterboxd blocks automated
    requests for it after the first, so buzz uses TMDB popularity and Vue's showings instead."""
    todo = [t for t in dict.fromkeys(tmdb_ids) if t][:limit]
    if not todo:
        return 0
    from .browser import browser_page

    got = 0
    now = datetime.now()
    stamp, day = now.isoformat(timespec="seconds"), now.date().isoformat()
    pops = {r["tmdb_id"]: r["data"] for r in db.q(
        f"SELECT tmdb_id, data FROM movies WHERE tmdb_id IN ({','.join('?' * len(todo))})", tuple(todo))}
    try:
        async with browser_page() as page:
            for t in todo:
                avg = count = None
                stats = {"watched": None, "lists": None, "likes": None}
                try:
                    await page.goto(f"https://letterboxd.com/tmdb/{t}/", wait_until="domcontentloaded", timeout=30000)
                    html = await page.content()
                    meta = await page.get_attribute('meta[name="twitter:data2"]', "content", timeout=3000) \
                        if 'twitter:data2' in html else None
                    avg, count = parse_rating(html, meta)
                    got += 1
                except Exception as e:  # missing rating, page change, or blocked - just skip
                    log.debug("community rating failed for %s: %s", t, e)
                db.x("""INSERT OR REPLACE INTO community(tmdb_id,lb_avg,fetched_at,rating_count,watched,lists,likes)
                        VALUES(?,?,?,?,?,?,?)""", (t, avg, stamp, count, stats["watched"], stats["lists"], stats["likes"]))
                pop = None
                try:
                    pop = json.loads(pops[t]).get("popularity") if t in pops else None
                except Exception:
                    pass
                if any(v is not None for v in (avg, count, *stats.values())):
                    db.x("""INSERT OR REPLACE INTO buzz_log(tmdb_id,day,lb_avg,rating_count,watched,lists,likes,tmdb_popularity)
                            VALUES(?,?,?,?,?,?,?,?)""", (t, day, avg, count, stats["watched"], stats["lists"], stats["likes"], pop))
                await asyncio.sleep(1.5)
    except Exception as e:
        log.warning("Letterboxd community ratings unavailable this run: %s", e)
    return got
