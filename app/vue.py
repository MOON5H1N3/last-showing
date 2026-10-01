"""Vue listings: what's showing and coming soon at one cinema."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .db import DB

log = logging.getLogger(__name__)

BASE = "https://www.myvue.com"
API = BASE + "/api/microservice/showings/cinemas/{cid}/films?minEmbargoLevel=1&includesSession={s}&includeSessionAttributes=true"

EVENT_DISTRIBUTORS = ("trafalgar", "cinemalive", "piece of magic", "more2screen", "national theatre",
                      "royal opera", "royal ballet", "met opera", "cinema live", "hybe", "sony music")
EVENT_TITLE = re.compile(r"^(met opera|rbo|national theatre live|ntlive|royal ballet|bolshoi)|live viewing|world tour|in concert|concert\b",
                         re.I)
RERELEASE_TITLE = re.compile(r"anniversary|encore|\((19|20)\d{2}\)|re-?release", re.I)
RERELEASE_DISTRIBUTORS = ("park circus", "fathom", "vue lumi")

# Attribute values that describe the screen/format (useful for ticket rules and display)
FORMAT_VALUES = {"epic", "imax", "3d", "4dx", "screenx", "atmos", "hdr", "laser", "infinity-vision",
                 "ultra-lux-and-lux", "biggest-screen", "eighteen", "subtitled", "open-captioned",
                 "mighty-mornings", "big-shorts", "atf"}


@dataclass
class Session:
    session_id: str
    start: datetime
    screen: str
    formats: list[str]
    sold_out: bool
    booking_url: str


@dataclass
class VueFilm:
    film_id: str
    title: str
    original_title: str
    release_date: date | None
    status: int  # 1 = now showing, 2 = coming soon
    distributor: str
    director: str
    runtime: int
    cert: str
    url: str
    poster: str
    attrs: list[str]
    sessions: list[Session] = field(default_factory=list)
    kind: str = "film"  # film | rerelease | event


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def classify(f: VueFilm) -> str:
    attrs = " ".join(f.attrs).lower()
    dist = (f.distributor or "").lower()
    if (any(d in dist for d in EVENT_DISTRIBUTORS) or "big-screen" in attrs or " event" in f" {attrs}"
            or " live" in f" {attrs}" or EVENT_TITLE.search(f.title)):
        return "event"
    if (RERELEASE_TITLE.search(f.title) or "movie-milestones" in attrs or "back on the big screen" in attrs
            or any(d in dist for d in RERELEASE_DISTRIBUTORS)):
        return "rerelease"
    return "film"


def parse(with_sessions: dict, all_films: dict | None = None) -> list[VueFilm]:
    """Merge the two API responses: one has showtimes, the other also lists coming-soon films with no showtimes yet."""
    films: dict[str, VueFilm] = {}
    sources = [(with_sessions or {}).get("result") or []]
    if all_films:
        sources.append(all_films.get("result") or [])
    for rows in sources:
        for r in rows:
            fid = r.get("filmId")
            if not fid or fid in films:
                continue
            attrs = [str(a.get("value") or a.get("name") or "") for a in (r.get("filmAttributes") or [])]
            attrs += [str(a.get("name") or "") for a in (r.get("filmAttributes") or [])]
            sessions = []
            for g in r.get("showingGroups") or []:
                for s in g.get("sessions") or []:
                    start = s.get("showTimeWithTimeZone") or s.get("startTime")
                    try:
                        dt = datetime.fromisoformat(start)
                    except (TypeError, ValueError):
                        continue
                    fmts = sorted({(a.get("value") or "").lower() for a in (s.get("attributes") or [])
                                   if (a.get("value") or "").lower() in FORMAT_VALUES or a.get("attributeType") == "Format"})
                    sessions.append(Session(
                        session_id=str(s.get("sessionId")), start=dt, screen=s.get("screenName") or "",
                        formats=fmts, sold_out=bool(s.get("isSoldOut")),
                        booking_url=BASE + (s.get("bookingUrl") or ""),
                    ))
                    attrs += [str(a.get("value") or "") for a in (s.get("attributes") or [])
                              if (a.get("value") or "").lower() in ("event", "live") or "big-screen" in (a.get("value") or "").lower()]
            f = VueFilm(
                film_id=fid, title=(r.get("filmTitle") or "").strip(),
                original_title=(r.get("originalTitle") or r.get("filmTitle") or "").strip(),
                release_date=_parse_date(r.get("releaseDate")), status=int(r.get("filmStatus") or 0),
                distributor=r.get("distributor") or "", director=r.get("director") or "",
                runtime=int(r.get("runningTime") or 0), cert=((r.get("certificate") or {}).get("name") or ""),
                url=r.get("filmUrl") or "", poster=r.get("posterImageSrc") or "",
                attrs=sorted({a for a in attrs if a}), sessions=sorted(sessions, key=lambda s: s.start),
            )
            f.kind = classify(f)
            films[fid] = f
    return list(films.values())


def parse_cinemas(resp: dict) -> list[dict]:
    out = []
    for group in (resp or {}).get("result") or []:
        for c in group.get("cinemas") or []:
            m = re.search(r"/cinema/([^/]+)/", c.get("whatsOnUrl") or "")
            if c.get("cinemaId") and m:
                out.append({"id": str(c["cinemaId"]), "name": c.get("cinemaName") or c.get("fullName"), "slug": m.group(1)})
    return sorted(out, key=lambda c: c["name"] or "")


async def fetch(slug: str, cinema_id: str = "", cinemas_out: list | None = None) -> tuple[str, dict, dict]:
    """Returns (cinema_id, response_with_sessions, response_all_films). Fills `cinemas_out` with every Vue cinema."""
    from .browser import browser_page

    async with browser_page() as page:
        await page.goto(f"{BASE}/cinema/{slug}/whats-on", wait_until="domcontentloaded", timeout=60000)
        if not cinema_id:
            cinema_id = await page.evaluate(
                """() => { try { const d = JSON.parse(document.getElementById('__NEXT_DATA__').textContent);
                   const s = JSON.stringify(d); const m = s.match(/"cinemaId":\\{"value":"(\\d+)"/); return m ? m[1] : ''; }
                   catch (e) { return ''; } }""")
            if not cinema_id:
                raise RuntimeError(f"Couldn't find the Vue cinema ID for '{slug}'. Set VUE_CINEMA_ID in your .env.")
        js = "u => fetch(u, {credentials: 'include'}).then(r => r.json())"
        with_s = await page.evaluate(js, API.format(cid=cinema_id, s="true"))
        all_f = await page.evaluate(js, API.format(cid=cinema_id, s="false"))
        if cinemas_out is not None:
            try:
                cinemas_out.extend(parse_cinemas(await page.evaluate(js, BASE + "/api/microservice/showings/cinemas")))
            except Exception as e:  # the list is a nice-to-have for the Settings page
                log.warning("couldn't read Vue's cinema list: %s", e)
    if (with_s or {}).get("responseCode") not in (0, None) or not (with_s or {}).get("result"):
        raise RuntimeError(f"Vue returned no listings (code {with_s.get('responseCode') if with_s else '?'})")
    return cinema_id, with_s, all_f


def store(db: DB, films: list[VueFilm], now: datetime) -> None:
    today = now.date().isoformat()
    stamp = now.isoformat(timespec="seconds")
    week = now + timedelta(days=7)
    with db.tx():
        for f in films:
            db.x("""INSERT INTO vue_films(film_id,title,original_title,release_date,status,distributor,director,runtime,
                    cert,url,poster,attrs,kind,first_seen,last_seen)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(film_id) DO UPDATE SET title=excluded.title, original_title=excluded.original_title,
                    release_date=excluded.release_date, status=excluded.status, distributor=excluded.distributor,
                    director=excluded.director, runtime=excluded.runtime, cert=excluded.cert, url=excluded.url,
                    poster=excluded.poster, attrs=excluded.attrs, kind=excluded.kind, last_seen=excluded.last_seen""",
                 (f.film_id, f.title, f.original_title, f.release_date.isoformat() if f.release_date else None,
                  f.status, f.distributor, f.director, f.runtime, f.cert, f.url, f.poster, json.dumps(f.attrs),
                  f.kind, stamp, stamp))
            for s in f.sessions:
                db.x("""INSERT INTO sessions(session_id,film_id,start,screen,formats,sold_out,booking_url,first_seen,last_seen)
                        VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET start=excluded.start,
                        screen=excluded.screen, formats=excluded.formats, sold_out=excluded.sold_out, last_seen=excluded.last_seen""",
                     (s.session_id, f.film_id, s.start.isoformat(), s.screen, json.dumps(s.formats), int(s.sold_out),
                      s.booking_url, stamp, stamp))
            future = [s for s in f.sessions if s.start >= now]
            if future:
                db.x("""INSERT OR REPLACE INTO snapshots(day,film_id,sessions_next7,sessions_total,last_session)
                        VALUES(?,?,?,?,?)""",
                     (today, f.film_id, sum(1 for s in future if s.start <= week), len(future),
                      future[-1].start.date().isoformat()))
    db.set("vue_last_listing", stamp)
    db.set("vue_listed_film_ids", [f.film_id for f in films])
