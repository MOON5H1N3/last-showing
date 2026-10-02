"""Turns listings + predictions into a plan for this month's tickets.

The core trade-off: a film you'd love that will still be showing next month can wait for next month's
ticket, while a slightly-less-loved film that leaves this month can't. So each film's priority is

    priority = predicted rating
               + FAVOURITE_WEIGHT × (its chance of being a 4.5★+ favourite, above your usual rate)
               - URGENCY_WEIGHT × (chance it's still showing next month)

and the top films by priority get this month's tickets.
"""
from __future__ import annotations

import json
import math
from calendar import monthrange
from dataclasses import asdict, dataclass, field, replace
from typing import Callable

import numpy as np
from datetime import date, datetime, timedelta

from .config import Settings
from .db import DB
from .reviews import verdict as review_verdict
from .taste import VARIANTS, TasteModel, community_value, size_effect, stars

MAJOR = ("disney", "warner", "universal", "sony", "paramount", "20th century")
MID = ("studiocanal", "lionsgate", "entertainment film", "altitude", "signature", "sky", "black bear",
       "elevation", "focus", "eone", "vertigo", "searchlight", "a24")
MONTHS_AHEAD = 3  # this month and the next two
PLAN_VERSION = 9  # bump when the plan's shape changes, so a saved plan from an older version is rebuilt
CONF_SHRINK = {"high": 1.0, "medium": 0.9, "low": 0.75, "none": 0.5}


def month_bounds(d: date) -> tuple[date, date, date]:
    start = d.replace(day=1)
    end = d.replace(day=monthrange(d.year, d.month)[1])
    nxt = end + timedelta(days=1)
    return start, end, nxt


def fmt_day(d: date | None) -> str:
    return d.strftime("%a %-d %b") if d else "?"


@dataclass
class RunEstimate:
    end: date
    basis: str  # known | listing | estimate | history
    note: str
    pfn: Callable[[date], float] | None = None  # from the history model, when available

    def p_showing(self, on: date) -> float:
        if self.pfn:
            return self.pfn(on)
        scale = {"known": 0.6, "listing": 1.5, "estimate": 6.0, "history": 6.0}[self.basis]
        return 1 / (1 + math.exp(-((self.end - on).days + 0.5) / scale))


def history_run(rm, today: date, release: date | None, first_seen: date | None, snaps: list, day_totals: dict,
                latest_day: str | None, session_days: list[date] | None = None) -> RunEstimate | None:
    """Run estimate from similar past situations at other Vues (and finished runs at yours)."""
    if not rm or not rm.ready:
        return None
    opened = release or first_seen
    week_in = (today - opened).days / 7 if opened and opened <= today else None
    share = ratio = None
    if week_in is not None and snaps and snaps[-1]["day"] == latest_day:
        n7 = snaps[-1]["sessions_next7"]
        share = n7 / max(1, day_totals.get(latest_day, 1))
        seen_from_start = opened and date.fromisoformat(snaps[0]["day"]) <= opened + timedelta(days=3)
        if seen_from_start:
            ratio = n7 / max(1, max(r["sessions_next7"] for r in snaps))
    if week_in is None and opened and session_days and day_totals:
        # not open yet, but advance showings are listed: its opening-week showings against a typical week's total
        opening = sum(1 for d in session_days if opened <= d < opened + timedelta(days=7))
        if opening >= 3:
            share = opening / max(1.0, float(np.median(list(day_totals.values()))))
    idx, ref = rm.estimate(today, opened, week_in, share, ratio)
    if len(idx) == 0:
        return None

    def pfn(on: date, idx=idx, ref=ref) -> float:
        return rm.p_still_showing(idx, max(0, (on - ref).days))

    end = ref + timedelta(days=rm.median_remaining(idx))
    p30 = pfn(max(today, ref) + timedelta(days=30))
    lasted = "almost all" if p30 >= 0.95 else "hardly any" if p30 < 0.05 else f"{p30:.0%}"
    if week_in is None:
        basis = "similar openings" if share is not None else "films opening at other Vues (no showtimes yet)"
        note = f"Opens {fmt_day(ref)}; {lasted} of {basis} lasted a month"
    else:
        weeks = max(0, round((end - today).days / 7))
        left = "under a week left" if weeks == 0 else f"about {weeks} more week{'s' if weeks != 1 else ''}"
        note = f"{week_in:.0f} week{'s' if round(week_in) != 1 else ''} in; similar films usually had {left} ({lasted} lasted another month)"
    return RunEstimate(end, "history", note, pfn)


def published_until(films, sessions: dict, today: date) -> date | None:
    """The last day of the week Vue has published times for: the most common last listed day among films already
    open. (Not the latest showing of all: one advance preview or a Halloween re-release weeks away would make every
    other film look as if it were leaving.)"""
    lasts = []
    for f in films:
        rel = f["release_date"]
        if f["kind"] != "film" or (rel and date.fromisoformat(rel) > today):
            continue
        days = [datetime.fromisoformat(r["start"]).date() for r in sessions.get(f["film_id"], [])]
        if days:
            lasts.append(max(days))
    if not lasts:
        return None
    counts: dict[date, int] = {}
    for d in lasts:
        counts[d] = counts.get(d, 0) + 1
    return max(counts, key=lambda d: (counts[d], d))


def base_weeks(distributor: str) -> float:
    d = (distributor or "").lower()
    if any(m in d for m in MAJOR):
        return 6.0
    if any(m in d for m in MID):
        return 3.5
    return 2.5


def estimate_run(kind: str, distributor: str, release: date | None, session_days: list[date],
                 next7_history: list[int], today: date, horizon: date | None) -> RunEstimate:
    last = max(session_days) if session_days else None
    if kind in ("event", "rerelease"):
        end = last or release or today
        n = len(set(session_days))
        return RunEstimate(end, "known", f"Limited run: {n} day{'s' if n != 1 else ''} listed, last {fmt_day(end)}")
    weeks = base_weeks(distributor)
    if not session_days or (release and release > today):  # not open yet, even if advance showings are on sale
        start = release or today
        return RunEstimate(start + timedelta(days=int(weeks * 7)), "estimate",
                           f"Opens {fmt_day(start)}; films like this usually run ~{weeks:g} weeks")
    opened = release is None or release <= today
    if opened and horizon and last and last < horizon - timedelta(days=1):  # advance showings aren't a leaving sign
        return RunEstimate(last, "listing", f"Nothing listed after {fmt_day(last)}; likely leaving")
    weeks_in = max(0.0, ((today - release).days / 7) if release else 0.0)
    remaining = max(0.5, weeks - weeks_in)
    cur = next7_history[-1] if next7_history else sum(1 for d in session_days if d <= today + timedelta(days=7))
    peak = max(next7_history) if next7_history else cur
    note = f"{weeks_in:.0f} week{'s' if round(weeks_in) != 1 else ''} in; {cur} showings in the next 7 days"
    if (peak and cur / peak < 0.35) or cur <= 7:
        remaining = min(remaining, 1.0)
        note += " (fading)"
    elif peak and cur / peak < 0.6:
        remaining = min(remaining, 2.0)
        note += " (dropping)"
    end = max(last or today, today + timedelta(days=int(round(remaining * 7))))
    return RunEstimate(end, "estimate", note)


@dataclass
class Item:
    film_id: str
    title: str
    kind: str
    tmdb_id: int | None
    year: int | None
    poster: str | None
    vue_url: str
    predicted: float | None
    stars: str
    confidence: str
    reasons: list[str]
    community: float | None
    on_watchlist: bool
    your_rating: float | None
    release_date: str | None
    first_date: str | None
    est_end: str
    end_basis: str
    end_note: str
    p_next_month: float
    priority: float
    watch_by: str | None = None
    sessions: list[dict] = field(default_factory=list)
    overview: str | None = None
    fav_chance: float | None = None
    p_available: float = 1.0          # chance it's showing during the month in question
    pinned: bool = False
    provisional: bool = False         # a future month: no showtimes yet, so it's a forecast
    trailer: str | None = None
    letterboxd_url: str | None = None
    cert: str | None = None
    runtime: int | None = None
    genres: list[str] = field(default_factory=list)
    backdrop: str | None = None
    chances: dict = field(default_factory=dict)   # chance it's still showing in 7 / 14 / 30 days
    buzz: dict = field(default_factory=dict)      # level, score and the numbers behind it
    short_run: bool = False                       # unusually short: likely gone within two weeks of opening here
    big_screen: float = 0.5                       # 0 = just as good at home, 1 = made for the cinema
    big_screen_note: str = ""
    home_from: str | None = None                  # roughly when it's likely to be out to rent at home
    opens_in_month: bool = False                  # for a month's list: opens that month (vs already showing)
    leaving: bool = False                         # likely gone within a week
    why: str = ""                                 # the single most telling reason, for one-line lists
    extra: bool = False                           # a re-release or event: shown on its own, not competing for tickets
    wanted: bool = False                          # you marked it "Want to see"
    paid_trip: bool = False                       # wanted, but no free ticket fits before it's likely to leave
    reviews: dict = field(default_factory=dict)   # critics and audience verdicts, for you to judge (not scored)
    model_rating: float | None = None             # the model's own prediction, before your rating or watchlist boost
    p_two_weeks: float | None = None              # chance it's still on two weeks after its first showing here
    unconfirmed: bool = False                     # no showtimes at your cinema yet
    times_overdue: bool = False                   # ...and Vue has already put out that week's times: no ticket for it


BIG_FORMATS = {"epic", "imax", "biggest-screen", "4dx", "screenx"}
SPECTACLE = {"Science Fiction": 0.35, "Action": 0.3, "Adventure": 0.3, "Fantasy": 0.25, "War": 0.25,
             "Animation": 0.15, "Horror": 0.15, "Music": 0.15, "Thriller": 0.05}
QUIET = {"Drama": -0.1, "Romance": -0.1, "Documentary": -0.1, "Comedy": -0.05}
HOME_WINDOW_DAYS = 45  # most studio films can be rented at home roughly six weeks after release


def times_due(release: date) -> date:
    """The day Vue should have published showtimes for a film opening on `release`: the Tuesday before (Vue puts
    out each Tuesday the times for the week from Friday). Allow that day itself, so overdue means after it."""
    return release - timedelta(days=((release.weekday() - 1) % 7) or 7)


def times_overdue(release: date | None, has_times: bool, today: date) -> bool:
    """A coming-soon film with no showtimes at your cinema after they were due: probably not showing there.
    Re-checked at every refresh, so if times turn up later it's back in the running straight away."""
    return bool(release and not has_times and today > times_due(release))


def big_screen(kind: str, genres: list[str], formats: set[str], runtime: int | None) -> tuple[float, str]:
    """How much a film gains from being seen in a cinema, 0 to 1, with a short reason."""
    if kind == "event":
        return 1.0, "Only in cinemas"
    if kind == "rerelease":
        return 0.8, "A rare chance to see it on a big screen"
    ups = sorted((SPECTACLE[g], g) for g in genres if g in SPECTACLE)[-2:]
    score = 0.35 + sum(v for v, _ in ups) + sum(QUIET.get(g, 0) for g in genres[:2])
    big = sorted(formats & BIG_FORMATS)
    if big:
        score += 0.2
    if runtime and runtime >= 140:
        score += 0.05
    score = max(0.0, min(1.0, score))
    if score >= 0.6:
        bits = [g.lower().replace("science fiction", "sci-fi") for _, g in ups[::-1]]
        if big:
            bits.append("showing on " + ("EPIC" if "epic" in big else "IMAX" if "imax" in big else "the biggest screen"))
        return score, "Suits the big screen: " + ", ".join(bits)
    if score < 0.4:
        return score, "Fine at home"
    return score, ""


def _eligible(formats: list[str], excluded: list[str]) -> bool:
    return not any(f in excluded for f in formats)


def training_rows(db: DB) -> list[tuple]:
    rows = db.q("""SELECT r.tmdb_id, r.rating, m.data, c.lb_avg, c.rating_count FROM ratings r
                   JOIN movies m ON m.tmdb_id=r.tmdb_id LEFT JOIN community c ON c.tmdb_id=r.tmdb_id""")
    data = []
    for r in rows:
        meta = json.loads(r["data"])
        data.append((meta, r["rating"], community_value(r["lb_avg"], meta), r["rating_count"] if r["lb_avg"] else None))
    return data


def _pct(values: dict, key) -> float | None:
    """Where a film's value sits among all the films in the listing (0 = lowest, 1 = highest)."""
    v = values.get(key)
    if v is None or len(values) < 5:
        return None
    others = list(values.values())
    return (sum(1 for o in others if o < v) + 0.5 * sum(1 for o in others if o == v)) / len(others)


def buzz_for(key, lists: dict, pops: dict, opening: dict, raw: dict) -> dict:
    """How much attention a film is getting compared with everything else at your cinema right now.

    Averages whichever of these exist: TMDB popularity (page views, trailers), how many showings your Vue gave
    its opening week (the cinema's own bet), and, for films not out yet, how many Letterboxd members have
    already rated it (early screenings)."""
    parts = [p for p in (_pct(lists, key), _pct(pops, key), _pct(opening, key)) if p is not None]
    if not parts:
        return {}
    score = sum(parts) / len(parts)
    level = "big" if score >= 0.8 else "some" if score >= 0.5 else "quiet"
    return {"score": round(score, 2), "level": level, **{k: v for k, v in raw.items() if v is not None}}


def build_model(db: DB) -> TasteModel:
    """Uses whichever variant won the last held-out model check."""
    check = db.get("model_check") or {}
    cols, lam, scale = VARIANTS.get(check.get("best") or "Everything", VARIANTS["Everything"])
    m = TasteModel(cols, lam, scale).fit(training_rows(db))
    m.check = check or None
    return m


def load_run_model(db: DB):
    from .history import RunModel, build_states
    if not db.get("history_import"):
        states = build_states(db, cribbs=True) if db.one("SELECT 1 FROM snapshots LIMIT 1") else []
    else:
        states = build_states(db, cribbs=True)
    return RunModel(states)


def make_plan(db: DB, s: Settings, now: datetime | None = None, model: TasteModel | None = None,
              run_model=None) -> dict:
    now = now or datetime.now(s.tz)
    today = now.date()
    m_start, m_end, next_start = month_bounds(today)
    month = today.strftime("%Y-%m")
    model = model or build_model(db)
    if run_model is None:
        try:
            run_model = load_run_model(db)
        except Exception:  # no history yet
            run_model = None
    day_totals = {r["day"]: r["t"] for r in db.q("SELECT day, SUM(sessions_next7) t FROM snapshots GROUP BY day")}
    latest_day = max(day_totals) if day_totals else None

    listed = set(db.get("vue_listed_film_ids", []))
    last_listing = db.get("vue_last_listing")
    films = [f for f in db.q("SELECT * FROM vue_films") if f["film_id"] in listed]

    uses = [dict(u) for u in db.q("SELECT * FROM ticket_uses WHERE month=? AND active=1 ORDER BY used_on", (month,))]
    used_films = {u["film_id"] for u in uses if u["film_id"]} | {f"tmdb:{u['tmdb_id']}" for u in uses if u["tmdb_id"]}
    carried = 0
    if s.ticket_rollover and s.tickets:
        prev = (m_start - timedelta(days=1)).strftime("%Y-%m")
        if prev >= (db.get("installed_month") or month):  # only months Last Showing was running for
            used_prev = db.one("SELECT COUNT(*) n FROM ticket_uses WHERE month=? AND active=1", (prev,))["n"]
            carried = max(0, s.tickets - used_prev)
    total = s.tickets + carried
    remaining = max(0, total - len(uses))

    sess_rows = db.q("SELECT * FROM sessions WHERE last_seen >= ? AND start >= ? ORDER BY start",
                     (last_listing or "", now.isoformat()))
    sessions: dict[str, list] = {}
    for r in sess_rows:
        sessions.setdefault(r["film_id"], []).append(r)

    horizon = published_until(films, sessions, today)

    ratings = {r["tmdb_id"]: r["rating"] for r in db.q("SELECT tmdb_id, rating FROM ratings")}
    watchlist = {r["tmdb_id"] for r in db.q("SELECT tmdb_id FROM watchlist")}
    seen = {r["tmdb_id"] for r in db.q("SELECT tmdb_id FROM watched")}
    metas = {r["tmdb_id"]: json.loads(r["data"]) for r in db.q("SELECT tmdb_id, data FROM movies")}
    comm_rows = {r["tmdb_id"]: r for r in db.q("SELECT * FROM community")}
    comm = {t: r["lb_avg"] for t, r in comm_rows.items()}
    review_rows = {r["tmdb_id"]: r for r in db.q("SELECT * FROM reviews")}

    dismissed = {r["film_id"] for r in db.q("SELECT film_id FROM dismissed")}
    pins = {r["film_id"]: r["month"] for r in db.q("SELECT film_id, month FROM pins")}
    wants = {r["film_id"] for r in db.q("SELECT film_id FROM wants")}
    months = [m_start]
    for _ in range(MONTHS_AHEAD - 1):
        months.append(month_bounds(months[-1])[2])
    window_end = month_bounds(months[-1])[1]

    base: list[tuple[Item, RunEstimate, list[date]]] = []  # one entry per scoreable film
    buzz_in: dict[str, dict] = {}
    unscored: list[dict] = []
    for f in films:
        if f["kind"] == "event" and not s.include_events:
            continue
        if f["kind"] == "rerelease" and not s.include_rereleases:
            continue
        if f["film_id"] in dismissed:
            continue
        release = date.fromisoformat(f["release_date"]) if f["release_date"] else None
        fs = sessions.get(f["film_id"], [])
        eligible = [r for r in fs if _eligible(json.loads(r["formats"] or "[]"), s.ticket_excluded_formats)]
        days = [datetime.fromisoformat(r["start"]).date() for r in eligible]
        if not days and fs:
            continue  # showtimes exist, but none your tickets cover
        if not days and not (release and release <= window_end):
            continue
        if f["kind"] != "film" and not days:
            continue  # events and re-releases only count once they have dates

        tid = f["tmdb_id"]
        meta = metas.get(tid) if tid else None
        if not meta:
            if not days or days[0] <= m_end:
                unscored.append({"film_id": f["film_id"], "title": f["title"], "kind": f["kind"], "vue_url": f["url"],
                                 "why": f["match_note"] or "not matched to TMDB yet",
                                 "first_date": days[0].isoformat() if days else (release.isoformat() if release else None)})
            continue

        crow = comm_rows.get(tid)
        pred = model.predict(meta, community_value(comm.get(tid), meta),
                             "Letterboxd average" if comm.get(tid) else "TMDB average (on a 5★ scale)",
                             n=crow["rating_count"] if crow is not None and comm.get(tid) else None)
        reasons = list(pred.reasons)
        rating = pred.rating
        conf = pred.confidence
        yours = ratings.get(tid)
        if not s.include_seen and (yours is not None or tid in seen):
            continue
        if yours is not None:
            rating, conf = yours, "high"
            reasons.insert(0, f"You rated it {stars(yours)} ({yours:g})")
        on_wl = tid in watchlist
        if on_wl:
            rating = min(5.0, rating + s.watchlist_boost)
            reasons.insert(0, "On your watchlist")

        snaps = db.q("SELECT day, sessions_next7 FROM snapshots WHERE film_id=? ORDER BY day", (f["film_id"],))
        hist = [r["sessions_next7"] for r in snaps]
        all_days = [datetime.fromisoformat(r["start"]).date() for r in fs]
        run = estimate_run(f["kind"], f["distributor"], release, all_days, hist, today, horizon)
        if f["kind"] == "film" and run.basis == "estimate":  # the listing itself says nothing definite
            first_seen = min(all_days) if all_days else None
            run = history_run(run_model, today, release, first_seen, snaps, day_totals, latest_day, all_days) or run
        adj = model.mu + (rating - model.mu) * CONF_SHRINK.get(conf, 0.75)
        fav = pred.fav_chance
        if fav is not None and yours is None:
            adj += s.favourite_weight * (fav - model.fav_base)  # could be a favourite: lift it

        item = Item(
            film_id=f["film_id"], title=meta.get("title") if f["kind"] == "film" else f["title"], kind=f["kind"],
            tmdb_id=tid, year=meta.get("year"), poster=meta.get("poster") or f["poster"], vue_url=f["url"],
            predicted=round(rating, 2), stars=stars(rating), confidence=conf, reasons=reasons,
            community=pred.community, on_watchlist=on_wl, your_rating=yours,
            release_date=release.isoformat() if release else None, first_date=None,
            est_end=run.end.isoformat(), end_basis=run.basis, end_note=run.note,
            p_next_month=0.0, priority=adj,  # month-specific values are filled in per month below
            fav_chance=fav if yours is None else None,
            sessions=[{"start": r["start"], "day": r["start"][:10], "screen": r["screen"],
                       "formats": json.loads(r["formats"] or "[]"),
                       "sold_out": bool(r["sold_out"]), "url": r["booking_url"]} for r in eligible],
            overview=meta.get("overview"), trailer=meta.get("trailer"),
            letterboxd_url=f"https://letterboxd.com/tmdb/{tid}/",
            cert=(f["cert"] or "").upper() if (f["cert"] or "").lower() not in ("", "tbc") else None,
            runtime=meta.get("runtime") or f["runtime"] or None, genres=(meta.get("genres") or [])[:3],
            backdrop=meta.get("backdrop"),
            chances={str(d): round(run.p_showing(today + timedelta(days=d)), 2) for d in (7, 14, 30)},
        )
        top = next((r for r in reasons if r.startswith(("For: ", "Against: ", "You rated", "On your watchlist"))), "")
        if top.startswith("For: "):
            top = top[5:]
        elif top.startswith("Against: "):
            top = "Against you: " + top[9:]
        item.why = top[:1].upper() + top[1:]
        fmts = {x for r in fs for x in json.loads(r["formats"] or "[]")}
        item.big_screen, item.big_screen_note = big_screen(f["kind"], meta.get("genres") or [], fmts, item.runtime)
        if f["kind"] == "film" and release:
            item.home_from = (release + timedelta(days=HOME_WINDOW_DAYS)).isoformat()
        # Cinema first: a film that suits the big screen gets a lift, but a quiet film is never marked down for it
        if f["kind"] == "film":
            item.priority = adj + s.big_screen_weight * max(0.0, item.big_screen - 0.5)
        # Re-releases and events sit in their own section rather than taking tickets from new films, unless
        # you'd really love one you haven't seen, or it's on your watchlist (a pin always counts)
        rv = review_rows.get(tid)
        v = review_verdict(rv["rt"] if rv else None, rv["metascore"] if rv else None, rv["guardian_stars"] if rv else None,
                           comm.get(tid), crow["rating_count"] if crow is not None else None)
        recent = release is None or release >= today - timedelta(days=45)  # early reviews: new and upcoming films
        if v["label"] and f["kind"] == "film" and recent:
            item.reviews = {**v, "guardian_url": rv["guardian_url"] if rv else None}
        item.model_rating = round(pred.rating, 2) if meta else None
        item.unconfirmed = f["kind"] == "film" and not fs
        item.times_overdue = item.unconfirmed and times_overdue(release, False, today)
        item.wanted = f["film_id"] in wants
        item.extra = (f["kind"] != "film" and not on_wl and not item.wanted
                      and not (yours is None and rating >= model.mu + 0.75))
        base.append((item, run, days))
        first_listed = min(all_days) if all_days else None
        wk = sum(1 for d in all_days if first_listed <= d < first_listed + timedelta(days=7)) if first_listed else None
        buzz_in[f["film_id"]] = {
            # early Letterboxd ratings only mean buzz before release; afterwards they just measure how big it got
            "early_ratings": (crow["rating_count"] if crow is not None and release and release > today else None),
            "tmdb_popularity": round(meta["popularity"], 1) if meta.get("popularity") else None,
            # a full opening week listed at your Vue (not a one-off preview)
            "opening_showings": wk if (f["kind"] == "film" and wk and wk >= 3 and release and
                                       abs((first_listed - release).days) <= 2) else None,
        }

    # Vue sometimes lists the same film twice (e.g. two language versions): keep the one with more showings
    seen_tmdb: dict[int, int] = {}
    for idx, (it_, _, dys) in enumerate(base):
        if it_.tmdb_id and it_.kind == "film":
            prev = seen_tmdb.get(it_.tmdb_id)
            if prev is None or len(dys) > len(base[prev][2]):
                seen_tmdb[it_.tmdb_id] = idx
    base = [b for i, b in enumerate(base) if not (b[0].tmdb_id and b[0].kind == "film") or seen_tmdb[b[0].tmdb_id] == i]

    # buzz is relative: compared with everything else showing or coming to your cinema
    lists = {k: math.log1p(v["early_ratings"]) for k, v in buzz_in.items() if v["early_ratings"] is not None}
    pops = {k: v["tmdb_popularity"] for k, v in buzz_in.items() if v["tmdb_popularity"] is not None}
    openings = {k: v["opening_showings"] for k, v in buzz_in.items() if v["opening_showings"] is not None}
    for item, run, days in base:
        item.buzz = buzz_for(item.film_id, lists, pops, openings, buzz_in[item.film_id])
        if item.kind == "film":
            opens = date.fromisoformat(item.release_date) if item.release_date else (days[0] if days else today)
            item.p_two_weeks = round(run.p_showing(max(opens, today) + timedelta(days=14)), 2)
            # most films at a multiplex last under three weeks, so only flag the unusually short
            item.short_run = item.p_two_weeks < 0.25 and opens >= today - timedelta(days=7)

    def availability(item: Item, run: RunEstimate, days: list[date], k: int) -> tuple[float, list[date], bool] | None:
        """Chance a film is showing during month k (None if it can't be), its days that month, and whether it
        opens that month."""
        ms = months[k]
        me = month_bounds(ms)[1]
        release = date.fromisoformat(item.release_date) if item.release_date else None
        in_month = [d for d in days if ms <= d <= me and d >= today]
        opens_here = bool(release and ms <= release <= me and release >= today)
        if in_month:
            p_avail = 1.0
        elif k == 0:
            if not (opens_here and not days):
                return None
            p_avail = 1.0
        elif item.kind != "film":
            return None  # events and re-releases: only on their listed dates
        elif release and release > me:
            return None
        else:
            p_avail = 1.0 if opens_here else run.p_showing(today if k == 0 else ms)
        return (p_avail, in_month, opens_here) if p_avail >= 0.3 else None

    def is_used(item: Item) -> bool:
        return item.film_id in used_films or bool(item.tmdb_id and f"tmdb:{item.tmdb_id}" in used_films)

    # Want to see: fit every wanted film into free tickets across the months, soonest deadline first (that fits
    # the most in). Anything that can't fit before it's likely to leave becomes a paid trip.
    month_keys = [m.strftime("%Y-%m") for m in months]
    slots = [remaining] + [s.tickets] * (len(months) - 1)
    overdue = {item.film_id for item, _, _ in base if item.times_overdue}
    for fid, mk in pins.items():
        if mk in month_keys and fid not in dismissed and fid not in overdue:
            slots[month_keys.index(mk)] -= 1
    wanted_free: dict[str, int] = {}
    wanted_paid: dict[str, int] = {}
    wanted_window: dict[str, list[int]] = {}
    for item, run, days in base:
        if item.wanted and not is_used(item) and item.film_id not in pins and not item.times_overdue:
            win = [k for k in range(len(months)) if (a_ := availability(item, run, days, k)) and a_[0] >= 0.5]
            if win:
                wanted_window[item.film_id] = win
    order = {item.film_id: item for item, _, _ in base}
    for k in range(len(months)):
        ready = [fid for fid, win in wanted_window.items()
                 if k in win and fid not in wanted_free and fid not in wanted_paid]
        ready.sort(key=lambda fid: (wanted_window[fid][-1], order[fid].est_end))
        for fid in ready:
            if slots[k] > 0:
                wanted_free[fid] = k
                slots[k] -= 1
            elif wanted_window[fid][-1] == k:  # last chance and no free ticket left
                wanted_paid[fid] = wanted_window[fid][0]  # go in the first month it's on, before it can leave
    wanted_outside = [order[fid].title for fid in wants if fid in order and fid not in wanted_window
                      and not is_used(order[fid]) and fid not in pins and not order[fid].times_overdue]

    # Plan each month in turn. A film picked in an earlier month isn't picked again; pins and wanted films come
    # first, and any free tickets left go to the best recommendations.
    month_plans = []
    taken: set[str] = set(used_films)
    for k, ms in enumerate(months):
        me = month_bounds(ms)[1]
        following = month_bounds(ms)[2]
        current = k == 0
        ref = today if current else ms
        tickets = remaining if current else s.tickets
        key = ms.strftime("%Y-%m")
        cands, opening, paid, no_times = [], [], [], []
        for item, run, days in base:
            if item.film_id in taken or (item.tmdb_id and f"tmdb:{item.tmdb_id}" in taken):
                continue
            here = availability(item, run, days, k)
            mine_free = wanted_free.get(item.film_id) == k
            mine_paid = wanted_paid.get(item.film_id) == k
            if item.wanted and item.film_id not in pins and not (mine_free or mine_paid) and not item.times_overdue:
                continue  # a wanted film belongs to the month it was fitted into
            if here is None:
                continue
            p_avail, in_month, opens_here = here
            release = date.fromisoformat(item.release_date) if item.release_date else None
            p_next = run.p_showing(following + timedelta(days=6))
            it = replace(item)
            it.p_available = round(p_avail, 2)
            it.p_next_month = round(p_next, 2)
            it.provisional = not in_month
            it.sessions = [x for x in item.sessions if ms.isoformat() <= x["start"][:10] <= me.isoformat()][:6]
            it.first_date = (in_month[0] if in_month else max(ref, release) if release else ref).isoformat()
            it.priority = round(item.priority - s.urgency_weight * p_next - 0.8 * (1 - p_avail), 3)
            it.pinned = pins.get(it.film_id) == key or mine_free
            it.paid_trip = mine_paid
            if item.kind == "film":
                start = max(date.fromisoformat(it.first_date), today)
                it.p_two_weeks = round(run.p_showing(start + timedelta(days=14)), 2)
                it.short_run = it.p_two_weeks < 0.25 and (release is None or release >= today - timedelta(days=7))
            it.opens_in_month = opens_here
            it.leaving = current and date.fromisoformat(it.est_end) <= today + timedelta(days=7)
            if it.times_overdue:
                it.pinned = it.paid_trip = False  # no times where they were due: listed, but never holds a ticket
                no_times.append(it)
                continue
            if mine_paid:
                paid.append(it)
                continue
            if opens_here:
                opening.append(it)
            cands.append(it)
        cands.sort(key=lambda i: (not i.pinned, -i.priority))
        extras = [c for c in cands if c.extra and not c.pinned]
        extras.sort(key=lambda i: (i.first_date or "", -(i.predicted or 0)))
        cands = [c for c in cands if c not in extras]
        pinned = [c for c in cands if c.pinned]
        picks = (pinned + [c for c in cands if not c.pinned])[:max(tickets, len(pinned))]
        for p in picks + paid:
            end = date.fromisoformat(p.est_end)
            p.watch_by = min(max(end, date.fromisoformat(p.first_date)), me).isoformat()
            taken.add(p.film_id)
        picks.sort(key=lambda p: p.watch_by)
        paid.sort(key=lambda p: p.watch_by)
        rest = [c for c in cands if c not in picks]
        leaving = []
        if current:
            soon_cut = today + timedelta(days=10)
            leaving = [i for i in rest if date.fromisoformat(i.est_end) <= soon_cut and (i.predicted or 0) >= model.mu]
            leaving.sort(key=lambda i: i.est_end)

        # Worth paying for: the next best films as optional extras. Cinema first: any film you'd clearly enjoy
        # qualifies, big-screen films simply rank a little higher.
        n_worth = 5 if s.tickets else 8  # with no tickets, this is the main list
        worth = [i for i in rest if (i.predicted or 0) >= model.mu + 0.3][:n_worth]  # rest is in priority order
        worth.sort(key=lambda i: i.first_date or "")
        # Catching later: only quiet films that didn't make the cut above, so few new releases end up here
        # (a film above the "worth paying for" bar is never sent home; if the top few are full, it stays in
        # Everything else as a cinema option)
        home = [i for i in rest if i not in worth and i.kind == "film" and i.big_screen < 0.4
                and model.mu <= (i.predicted or 0) < model.mu + 0.3]
        home.sort(key=lambda i: -(i.predicted or 0))
        home = home[:4]
        # Everything else, once each, in date order
        shown = {i.film_id for i in picks + worth + home}
        everything = [i for i in rest if i.film_id not in shown] + no_times
        everything.sort(key=lambda i: (i.first_date or "", -(i.predicted or 0)))
        opening = [o for o in opening if o not in picks]
        opening.sort(key=lambda i: (i.first_date or "", -(i.predicted or 0)))
        month_plans.append({
            "month": key, "month_label": ms.strftime("%B %Y"), "month_name": ms.strftime("%B"),
            "month_start": ms.isoformat(), "month_end": me.isoformat(), "current": current,
            "tickets_left": tickets if current else max(0, tickets - 0),
            "picks": [asdict(p) for p in picks], "paid_trips": [asdict(p) for p in paid],
            "leaving_soon": [asdict(i) for i in leaving[:6]],
            "worth_paying": [asdict(i) for i in worth],
            "at_home": [asdict(i) for i in home], "everything": [asdict(i) for i in everything],
            "extras": [asdict(i) for i in extras],
            "also_good": [asdict(i) for i in worth], "opening": [asdict(i) for i in opening],
            "candidates": [{"film_id": c.film_id, "title": c.title, "tmdb_id": c.tmdb_id} for c in cands],
        })

    wanted_pinned = [fid for fid in wants if fid in pins and fid in order and not is_used(order[fid])]
    want_summary = {
        "total": len(wanted_free) + len(wanted_paid) + len(wanted_pinned) + len(wanted_outside),
        "free": len(wanted_free) + len(wanted_pinned),
        "paid": [{"film_id": fid, "title": order[fid].title, "month_name": months[k].strftime("%B")}
                 for fid, k in sorted(wanted_paid.items(), key=lambda x: x[1])],
        "later": wanted_outside,
        "free_tickets": remaining + s.tickets * (len(months) - 1),
    }

    cur = month_plans[0]
    days_left = (m_end - today).days + 1
    warning = None
    if remaining and days_left <= 7:
        warning = {"days_left": days_left, "tickets_left": remaining,
                   "films": [{"title": p["title"], "sessions": p["sessions"][:3]} for p in cur["picks"]]}
    return {
        "version": PLAN_VERSION,
        "generated_at": now.isoformat(timespec="minutes"),
        "month": month, "month_label": today.strftime("%B %Y"),
        "month_end": m_end.isoformat(),
        "tickets_total": total, "tickets_carried": carried, "tickets_used": len(uses), "tickets_left": remaining,
        "ticket_source": s.ticket_source,
        "uses": uses,
        "picks": cur["picks"], "leaving_soon": cur["leaving_soon"], "also_good": cur["also_good"],
        "next_month": month_plans[1]["opening"] if len(month_plans) > 1 else [],
        "months": month_plans,
        "films": {item.film_id: asdict(item) for item, _, _ in base},
        "warning": warning,
        "unscored": unscored,
        "all_candidates": cur["candidates"],
        "pins": [{"film_id": k2, "month": v} for k2, v in pins.items()],
        "wants": want_summary,
        "dismissed": [dict(r) for r in db.q("SELECT * FROM dismissed ORDER BY created_at DESC")],
        "model": model.summary(),
        "runs": {"films": run_model.n_films if run_model else 0, "ready": bool(run_model and run_model.ready)},
        "listing_checked": last_listing,
    }


def buzz_check(db: DB) -> dict:
    """For the Taste page: how film size relates to your ratings, and how much pre-release buzz is logged so far."""
    rows = db.q("""SELECT r.rating, c.lb_avg, c.rating_count FROM ratings r JOIN community c ON c.tmdb_id=r.tmdb_id
                   WHERE c.lb_avg IS NOT NULL AND c.rating_count IS NOT NULL""")
    out = size_effect([(r["rating"], r["lb_avg"], r["rating_count"]) for r in rows])
    out["waiting"] = db.one("SELECT COUNT(*) n FROM ratings WHERE tmdb_id NOT IN "
                            "(SELECT tmdb_id FROM community WHERE rating_count IS NOT NULL)")["n"]
    # films whose buzz was logged before you rated them: what we'll learn from as they build up
    out["logged"] = db.one("SELECT COUNT(DISTINCT tmdb_id) n FROM buzz_log")["n"]
    out["logged_then_rated"] = db.one("""SELECT COUNT(DISTINCT b.tmdb_id) n FROM buzz_log b JOIN ratings r ON r.tmdb_id=b.tmdb_id
                                         WHERE b.day < COALESCE(NULLIF(r.rated_on,''), '9999')""")["n"]
    return out


def record_predictions(db: DB, plan: dict, today: date) -> int:
    """Keep the first prediction for every film you haven't rated yet, to check against your rating later."""
    rows = [(f["tmdb_id"], f["film_id"], f["title"], f.get("model_rating"), f.get("fav_chance"), f.get("confidence"),
             f.get("community"), today.isoformat())
            for f in (plan.get("films") or {}).values()
            if f.get("tmdb_id") and f.get("your_rating") is None and f.get("model_rating") is not None]
    with db.tx():
        for r in rows:
            db.x("""INSERT OR IGNORE INTO predictions(tmdb_id,film_id,title,predicted,fav_chance,confidence,community,recorded_on)
                    VALUES(?,?,?,?,?,?,?,?)""", r)
    return len(rows)


def accuracy_check(db: DB) -> dict:
    """Films you rated after Last Showing predicted them: predicted against actual, and against the crowd."""
    rows = db.q("""SELECT p.*, r.rating, r.rated_on,
                          EXISTS(SELECT 1 FROM ticket_uses t WHERE t.tmdb_id=p.tmdb_id AND t.active=1) AS ticket
                   FROM predictions p JOIN ratings r ON r.tmdb_id=p.tmdb_id
                   WHERE COALESCE(NULLIF(r.rated_on,''),'9999') >= p.recorded_on
                   ORDER BY r.rated_on DESC""")
    films = [{"title": r["title"], "film_id": r["film_id"], "predicted": r["predicted"], "rating": r["rating"],
              "gap": round(r["rating"] - r["predicted"], 2), "community": r["community"], "ticket": bool(r["ticket"]),
              "rated_on": r["rated_on"]} for r in rows]
    out = {"films": films[:20], "count": len(films),
           "tracking": db.one("SELECT COUNT(*) n FROM predictions")["n"],
           "since": (db.one("SELECT MIN(recorded_on) d FROM predictions") or {"d": None})["d"]}
    if films:
        out["error"] = round(sum(abs(f["gap"]) for f in films) / len(films), 2)
        out["bias"] = round(sum(f["gap"] for f in films) / len(films), 2)  # + = you liked them more than predicted
        crowd = [f for f in films if f["community"] is not None]
        if crowd:
            out["crowd_error"] = round(sum(abs(f["rating"] - f["community"]) for f in crowd) / len(crowd), 2)
            out["crowd_films"] = len(crowd)
    return out
