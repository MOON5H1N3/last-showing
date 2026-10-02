"""How long films stay on: learned from past Vue listings.

Source: Clusterflick (https://github.com/clusterflick), which has saved the listings of 13 London Vue
cinemas several times a day since November 2025, published under CC BY 4.0. We download a snapshot every
few days for a handful of suburban multiplexes (the closest match to Cribbs Causeway), and keep only small
summary tables. Last Showing's own Cribbs Causeway records are added to the same pool as they build up.

The question it answers: "a film N weeks into its run, taking X% of this cinema's showings this week, at
Y% of its opening week's showings - how likely is it to still be on in D days?" It looks up the most
similar situations in the history and counts how many of those films were still showing D days later.
"""
from __future__ import annotations

import asyncio
import logging
import math
import re
import zlib
from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import numpy as np

from .db import DB

log = logging.getLogger(__name__)
REPO = "https://github.com/clusterflick/data-transformed"
DEFAULT_SITES = ["harrow", "romford", "croydon-purley-way", "dagenham", "eltham"]
LONDON = ZoneInfo("Europe/London")
FIRST_VUE_TAG = "20251110"  # nothing for Vue before this

SCHEMA = """
CREATE TABLE IF NOT EXISTS hist_films (site TEXT, film_id TEXT, tmdb_id INTEGER, title TEXT, category TEXT,
                                       release_date TEXT, PRIMARY KEY (site, film_id));
CREATE TABLE IF NOT EXISTS hist_dates (site TEXT, film_id TEXT, show_date TEXT, PRIMARY KEY (site, film_id, show_date));
CREATE TABLE IF NOT EXISTS hist_snaps (site TEXT, day TEXT, film_id TEXT, next7 INTEGER, total7 INTEGER,
                                       PRIMARY KEY (site, day, film_id));
CREATE TABLE IF NOT EXISTS hist_tags (tag TEXT PRIMARY KEY, sites TEXT);
"""


def ensure_schema(db: DB) -> None:
    for stmt in SCHEMA.strip().split(";"):
        if stmt.strip():
            db.x(stmt)


# ---------------------------------------------------------------- import

async def list_tags(client: httpx.AsyncClient) -> list[str]:
    """Release tags via git's public ref list (no API key or rate limit needed)."""
    r = await client.get(f"{REPO}.git/info/refs", params={"service": "git-upload-pack"})
    r.raise_for_status()
    return sorted(set(re.findall(r"refs/tags/(\d{8}\.\d{6})", r.text)))


def pick_tags(tags: list[str], every_days: int = 3, after: str = FIRST_VUE_TAG) -> list[str]:
    chosen, last = [], None
    for t in sorted(tags):
        if t < after:
            continue
        d = datetime.strptime(t[:8], "%Y%m%d").date()
        if last is None or (d - last).days >= every_days:
            chosen.append(t)
            last = d
    return chosen


def summarise_snapshot(movies: list[dict], snap_day: date) -> tuple[dict, dict]:
    """-> ({film_id: info}, {film_id: next-7-day showing count}) for one cinema's snapshot."""
    info, next7 = {}, {}
    week_end = snap_day + timedelta(days=7)
    for m in movies:
        sid = m.get("showingId") or ""
        film_id = sid.rsplit("-", 1)[-1]
        if not film_id:
            continue
        tm = m.get("themoviedb") or {}
        dates = set()
        n7 = 0
        for p in m.get("performances") or []:
            t = p.get("time")
            if not t:
                continue
            d = datetime.fromtimestamp(t / 1000, timezone.utc).astimezone(LONDON).date()
            dates.add(d)
            if snap_day <= d < week_end:
                n7 += 1
        info[film_id] = {"tmdb_id": tm.get("id"), "title": m.get("title") or tm.get("title"),
                         "category": m.get("category"), "release_date": tm.get("releaseDate"), "dates": dates}
        next7[film_id] = n7
    return info, next7


def store_snapshot(db: DB, site: str, snap_day: date, info: dict, next7: dict) -> None:
    total = sum(next7.values())
    with db.tx():
        for fid, i in info.items():
            db.x("""INSERT INTO hist_films(site,film_id,tmdb_id,title,category,release_date) VALUES(?,?,?,?,?,?)
                    ON CONFLICT(site,film_id) DO UPDATE SET tmdb_id=COALESCE(excluded.tmdb_id, tmdb_id),
                    title=excluded.title, category=excluded.category,
                    release_date=COALESCE(excluded.release_date, release_date)""",
                 (site, fid, i["tmdb_id"], i["title"], i["category"], i["release_date"]))
            db.many("INSERT OR IGNORE INTO hist_dates(site,film_id,show_date) VALUES(?,?,?)",
                    [(site, fid, d.isoformat()) for d in i["dates"]])
            if next7.get(fid):
                db.x("INSERT OR REPLACE INTO hist_snaps(site,day,film_id,next7,total7) VALUES(?,?,?,?,?)",
                     (site, snap_day.isoformat(), fid, next7[fid], total))


LOCAL_PREFIX = "near:"  # sites read live from Vue each morning (nearby cinemas), vs the London archive


def store_vue_listing(db: DB, slug: str, snap_day: date, films) -> int:
    """One morning's listing at a nearby Vue (parsed VueFilms), in the same summary tables as the London archive."""
    ensure_schema(db)
    info, next7 = {}, {}
    week_end = snap_day + timedelta(days=7)
    for f in films:
        if f.kind != "film" or not f.sessions:
            continue
        dates = {s.start.date() for s in f.sessions}
        info[f.film_id] = {"tmdb_id": None, "title": f.title, "category": "movie",
                           "release_date": f.release_date.isoformat() if f.release_date else None, "dates": dates}
        next7[f.film_id] = sum(1 for s in f.sessions if snap_day <= s.start.date() < week_end)
    store_snapshot(db, LOCAL_PREFIX + slug, snap_day, info, next7)
    return len(info)


def is_local(key: str) -> bool:
    return key.startswith(LOCAL_PREFIX) or key.startswith("cribbs/")


def source_summary(db: DB) -> dict:
    """How much each source has contributed: London archive, each nearby Vue, and your own cinema."""
    ensure_schema(db)
    names = db.get("nearby_names") or {}
    sites = []
    for r in db.q("""SELECT site, COUNT(DISTINCT film_id) films, MIN(day) a, MAX(day) b FROM hist_snaps
                     WHERE site LIKE ? GROUP BY site ORDER BY site""", (LOCAL_PREFIX + "%",)):
        slug = r["site"][len(LOCAL_PREFIX):]
        finished = sum(1 for s in _site_finished(db, r["site"]))
        sites.append({"slug": slug, "name": names.get(slug, slug.replace("-", " ").title()), "films": r["films"],
                      "finished": finished, "since": r["a"]})
    return {"sites": sites}


def _site_finished(db: DB, site: str):
    last = db.one("SELECT MAX(day) d FROM hist_snaps WHERE site=?", (site,))["d"]
    first = db.one("SELECT MIN(day) d FROM hist_snaps WHERE site=?", (site,))["d"]
    if not last:
        return []
    last_d, first_d = date.fromisoformat(last), date.fromisoformat(first)
    return [r for r in db.q("SELECT film_id, MIN(show_date) a, MAX(show_date) b FROM hist_dates WHERE site=? GROUP BY 1",
                            (site,))
            if date.fromisoformat(r["b"]) < last_d - timedelta(days=1) and date.fromisoformat(r["a"]) > first_d + timedelta(days=3)]


async def import_history(db: DB, sites: list[str] | None = None, every_days: int = 3,
                         client: httpx.AsyncClient | None = None, progress=None) -> dict:
    """Downloads snapshots not yet imported. The first run takes a few minutes; later runs only fetch new ones."""
    ensure_schema(db)
    sites = sites or DEFAULT_SITES
    own = client is None
    client = client or httpx.AsyncClient(timeout=60, follow_redirects=True,
                                         headers={"User-Agent": "last-showing/1.0 (+self-hosted)"})
    done = {r["tag"] for r in db.q("SELECT tag FROM hist_tags")}
    try:
        tags = [t for t in pick_tags(await list_tags(client), every_days) if t not in done]
        sem = asyncio.Semaphore(4)
        files = 0

        async def one(tag: str, site: str):
            nonlocal files
            async with sem:
                r = await client.get(f"{REPO}/releases/download/{tag}/myvue.com-{site}")
            if r.status_code != 200:
                return
            try:
                movies = r.json()
            except ValueError:
                return
            snap_day = datetime.strptime(tag[:8], "%Y%m%d").date()
            info, next7 = summarise_snapshot(movies if isinstance(movies, list) else [], snap_day)
            store_snapshot(db, site, snap_day, info, next7)
            files += 1

        for i, tag in enumerate(tags):
            await asyncio.gather(*(one(tag, s) for s in sites), return_exceptions=True)
            db.x("INSERT OR REPLACE INTO hist_tags(tag,sites) VALUES(?,?)", (tag, ",".join(sites)))
            if progress:
                progress(i + 1, len(tags))
    finally:
        if own:
            await client.aclose()
    n_films = db.one("SELECT COUNT(*) n FROM hist_films")["n"]
    summary = {"snapshots_added": len(tags), "files": files, "films": n_films,
               "imported_at": datetime.now().isoformat(timespec="seconds"),
               "first": db.one("SELECT MIN(day) d FROM hist_snaps")["d"],
               "last": db.one("SELECT MAX(day) d FROM hist_snaps")["d"], "sites": sites}
    db.set("history_import", summary)
    return summary


# ---------------------------------------------------------------- states

@dataclass
class State:
    key: str          # site/film
    week_in: float    # weeks since the film opened at that cinema
    share: float      # share of the cinema's showings in the next 7 days
    ratio: float      # this week's showings / the most it has had in any week so far
    remaining: int    # days from this snapshot to its last showing
    censored: bool    # still showing when the records end, so `remaining` is a lower bound


def build_states(db: DB, cribbs: bool = True) -> list[State]:
    ensure_schema(db)
    states: list[State] = []
    # each source's own first and last record: the London archive and the nearby Vues started and stop at different times
    bounds = {r["site"]: (date.fromisoformat(r["a"]), date.fromisoformat(r["b"]))
              for r in db.q("SELECT site, MIN(day) a, MAX(day) b FROM hist_snaps GROUP BY site")}
    if bounds:
        films = {(r["site"], r["film_id"]): r for r in db.q("SELECT * FROM hist_films WHERE category='movie'")}
        spans = {(r["site"], r["film_id"]): (date.fromisoformat(r["a"]), date.fromisoformat(r["b"]))
                 for r in db.q("SELECT site, film_id, MIN(show_date) a, MAX(show_date) b FROM hist_dates GROUP BY 1,2")}
        snaps = defaultdict(list)
        for r in db.q("SELECT * FROM hist_snaps ORDER BY day"):
            snaps[(r["site"], r["film_id"])].append(r)
        for k, rows in snaps.items():
            if k not in films or k not in spans:
                continue
            first, last = spans[k]
            first_d, last_d = bounds[k[0]]
            if first <= first_d + timedelta(days=3):
                continue  # already running when records began: its start is unknown
            censored = last >= last_d - timedelta(days=1)  # still listed when the records end
            peak = 0
            for r in rows:
                day = date.fromisoformat(r["day"])
                peak = max(peak, r["next7"])
                states.append(State(f"{k[0]}/{k[1]}", max(0.0, (day - first).days / 7), r["next7"] / max(1, r["total7"]),
                                    r["next7"] / max(1, peak), (last - day).days, censored))
    if cribbs:
        states += cribbs_states(db)
    return states


def cribbs_states(db: DB) -> list[State]:
    """Last Showing's own records of films that have finished their run at your cinema."""
    listed = set(db.get("vue_listed_film_ids", []))
    today = date.today()
    totals = {r["day"]: r["t"] for r in db.q("SELECT day, SUM(sessions_next7) t FROM snapshots GROUP BY day")}
    out = []
    for f in db.q("""SELECT v.film_id, v.release_date, MIN(s.start) a, MAX(s.start) b FROM vue_films v
                     JOIN sessions s ON s.film_id=v.film_id WHERE v.kind='film' GROUP BY v.film_id"""):
        last = datetime.fromisoformat(f["b"]).date()
        if f["film_id"] in listed or last > today - timedelta(days=3):
            continue  # still running
        first = datetime.fromisoformat(f["a"]).date()
        if f["release_date"] and date.fromisoformat(f["release_date"]) < first - timedelta(days=3):
            continue  # we only saw the end of its run
        peak = 0
        for r in db.q("SELECT day, sessions_next7 FROM snapshots WHERE film_id=? ORDER BY day", (f["film_id"],)):
            day = date.fromisoformat(r["day"])
            peak = max(peak, r["sessions_next7"])
            out.append(State(f"cribbs/{f['film_id']}", max(0.0, (day - first).days / 7),
                             r["sessions_next7"] / max(1, totals.get(r["day"], 1)), r["sessions_next7"] / max(1, peak),
                             (last - day).days, False))
    return out


# ---------------------------------------------------------------- model

class RunModel:
    """Nearest-neighbour look-up over past situations."""

    LOCAL_FULL = 150  # finished local runs at which London counts for only a quarter

    def __init__(self, states: list[State], k: int = 60):
        self.states = states
        self.k = k
        self.n_films = len({s.key for s in states})
        self.n_local = len({s.key for s in states if is_local(s.key) and not s.censored})
        self.n_london = len({s.key for s in states if not is_local(s.key)})
        # Lean on local runs as they build up: London counts fully at first, down to a quarter
        self.london_weight = max(0.25, 1 - self.n_local / self.LOCAL_FULL)
        self.local = np.array([is_local(s.key) for s in states], dtype=bool)
        self.w = np.where(self.local, 1.0, self.london_weight) if states else np.zeros(0)
        if states:
            self.X = np.array([[s.week_in, math.log(max(s.share, 1e-3)), s.ratio] for s in states])
            self.scale = np.array([1.5, 0.6, 0.25])  # how far apart counts as "different" for each feature
            self.rem = np.array([s.remaining for s in states])
            self.cens = np.array([s.censored for s in states])
            self.open_mask = self.X[:, 0] < 0.5
        else:
            self.X = np.zeros((0, 3))

    @property
    def ready(self) -> bool:
        return self.n_films >= 40

    def _neighbours(self, week_in: float, share: float | None, ratio: float | None) -> np.ndarray:
        q = np.array([week_in, math.log(max(share or 1e-3, 1e-3)), ratio if ratio is not None else 1.0])
        use = np.array([True, share is not None, ratio is not None])
        d = np.sqrt((((self.X - q) / self.scale) ** 2)[:, use].sum(axis=1))
        return np.argsort(d)[: self.k]

    def p_still_showing(self, idx: np.ndarray, days_ahead: int) -> float:
        rem, cens, w = self.rem[idx], self.cens[idx], self.w[idx]
        known = ~cens | (rem >= days_ahead)  # a censored run only counts if it already lasted long enough
        if known.sum() < 5:
            known = np.ones(len(rem), dtype=bool)
            if not len(rem):
                return 0.5
        return float(np.average(rem[known] >= days_ahead, weights=w[known]))

    def median_remaining(self, idx: np.ndarray) -> int:
        rem, w = self.rem[idx], self.w[idx]
        order = np.argsort(rem)
        cum = np.cumsum(w[order])
        return int(rem[order][np.searchsorted(cum, cum[-1] / 2)])

    def local_share(self, idx: np.ndarray) -> float:
        """How much of an estimate came from Vues near you (and your own), rather than London."""
        w = self.w[idx]
        return float(w[self.local[idx]].sum() / w.sum()) if len(idx) and w.sum() else 0.0

    def estimate(self, today: date, release: date | None, week_in: float | None, share: float | None,
                 ratio: float | None):
        """-> (idx of similar past situations, reference date they're measured from)."""
        if week_in is None:  # not open yet: everything that has just opened
            opening = np.where(self.open_mask)[0]
            if share is not None and len(opening):
                sub = self.X[opening]
                q = math.log(max(share, 1e-3))
                opening = opening[np.argsort(np.abs(sub[:, 1] - q))[: self.k]]
            return opening, (release or today)
        return self._neighbours(week_in, share, ratio), today


# ---------------------------------------------------------------- backtest

def backtest(states: list[State], horizons=(14, 30), k: int = 60) -> dict:
    """Holds out 1 in 5 films, predicts from the rest, and checks the chances came true."""
    if len({s.key for s in states}) < 60:
        return {"ok": False, "why": "not enough history yet"}
    test_keys = {s.key for s in states if zlib.crc32(s.key.encode()) % 5 == 0}
    train = RunModel([s for s in states if s.key not in test_keys], k)
    out = {"ok": True, "films_train": train.n_films, "films_test": len(test_keys), "horizons": {}}
    for h in horizons:
        preds, actual, base = [], [], []
        wk = np.array([s.week_in for s in train.states])
        for s in states:
            if s.key not in test_keys or (s.censored and s.remaining < h):
                continue
            idx = train._neighbours(s.week_in, s.share, s.ratio)
            preds.append(train.p_still_showing(idx, h))
            actual.append(float(s.remaining >= h))
            same_week = np.where(np.abs(wk - s.week_in) < 0.5)[0]  # baseline: weeks into its run, nothing else
            base.append(train.p_still_showing(same_week, h) if len(same_week) else 0.5)
        preds, actual, base = map(np.array, (preds, actual, base))
        bins = []
        for lo, hi in [(0, .2), (.2, .4), (.4, .6), (.6, .8), (.8, 1.01)]:
            m = (preds >= lo) & (preds < hi)
            if m.sum() >= 5:
                bins.append({"predicted": f"{int(lo * 100)}–{min(100, int(hi * 100))}%", "cases": int(m.sum()),
                             "came_true": round(float(actual[m].mean()), 3)})
        out["horizons"][str(h)] = {
            "cases": int(len(preds)),
            "brier": round(float(np.mean((preds - actual) ** 2)), 4),
            "brier_weeks_only": round(float(np.mean((base - actual) ** 2)), 4),
            "calibration": bins,
        }
    return out
