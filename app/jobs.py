"""The refresh pipeline: pull everything, score it, save the plan."""
from __future__ import annotations

import asyncio
import json
import logging
import traceback
from pathlib import Path
from datetime import datetime, timedelta

from . import letterboxd, tickets, vue
from .config import Settings
from .db import DB
from .planner import make_plan
from .tmdb import TMDB

log = logging.getLogger(__name__)
MATCH_RULES_VERSION = 2  # bump when Vue -> TMDB matching changes, so existing matches are redone


class Engine:
    def __init__(self, s: Settings, db: DB):
        self.s = s
        self.db = db
        self.lock = asyncio.Lock()
        self.running_step: str | None = None
        self.settings_version = 0
        self.settings_changed = asyncio.Event()

    def settings_saved(self) -> None:
        self.settings_version += 1
        self.settings_changed.set()

    def backup(self, keep: int = 14) -> Path | None:
        """A copy of the database in data/backups, one per day; the newest `keep` are kept."""
        folder = self.s.data_dir / "backups"
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / f"lastshowing-{datetime.now(self.s.tz):%Y%m%d}.sqlite3"
        try:
            self.db.backup_to(dest)
        except Exception as e:
            log.warning("backup failed: %s", e)
            return None
        for old in sorted(folder.glob("lastshowing-*.sqlite3"))[:-keep]:
            old.unlink(missing_ok=True)
        return dest

    @property
    def busy(self) -> bool:
        return self.lock.locked()

    def plan(self) -> dict:
        from .planner import PLAN_VERSION
        p = self.db.get("plan") or {}
        stale = p and (p.get("version") != PLAN_VERSION
                       or p.get("month") != datetime.now(self.s.tz).strftime("%Y-%m"))
        if stale:  # saved by an older version, or a new month has started since
            try:
                p = self.replan()
            except Exception as e:
                log.warning("couldn't rebuild the plan: %s", e)
        return p

    def replan(self) -> dict:
        p = make_plan(self.db, self.s)
        self.db.set("plan", p)
        return p

    async def refresh(self, vue_listings: bool = True) -> dict:
        async with self.lock:
            return await self._refresh(vue_listings)

    async def _refresh(self, vue_listings: bool) -> dict:
        s, db = self.s, self.db
        now = datetime.now(s.tz)
        report = {"started": now.isoformat(timespec="seconds"), "steps": [], "errors": []}
        tmdb = TMDB(s.tmdb_key, db) if s.tmdb_key else None

        async def step(name, coro_fn):
            self.running_step = name
            try:
                res = await coro_fn()
                report["steps"].append({"step": name, "ok": True, "result": res})
                log.info("%s: %s", name, res)
            except Exception as e:
                log.error("%s failed: %s\n%s", name, e, traceback.format_exc())
                report["steps"].append({"step": name, "ok": False, "result": str(e)})
                report["errors"].append(f"{name}: {e}")

        try:
            # 1. Letterboxd export (only when a new ZIP appears)
            async def do_export():
                z = letterboxd.latest_export(s.letterboxd_dir)
                if not z:
                    return "no export ZIP in data/letterboxd yet"
                prev = db.get("letterboxd_import") or {}
                if (prev.get("file") == z.name and prev.get("mtime") == z.stat().st_mtime
                        and prev.get("version") == letterboxd.IMPORT_VERSION):
                    return f"{z.name} already imported"
                if not tmdb:
                    return "skipped (no TMDB key)"
                r = await letterboxd.import_export(db, tmdb, z)
                return (f"imported {r['ratings']} ratings, {r['diary']} diary entries, {r['watchlist']} watchlist; "
                        f"{r['tv_count']} TV entries skipped; {r['unmatched_count']} unmatched")
            await step("Letterboxd export", do_export)

            # 2. RSS
            if s.letterboxd_user:
                async def do_rss():
                    n = await letterboxd.poll_rss(db, s.letterboxd_user)
                    return f"{n} new diary entries"
                await step("Letterboxd RSS", do_rss)

            # 3. Vue listings
            if vue_listings:
                async def do_vue():
                    cinemas: list = []
                    cid, with_s, all_f = await vue.fetch(s.vue_cinema_slug, s.vue_cinema_id or db.get("vue_cinema_id", ""),
                                                         cinemas)
                    db.set("vue_cinema_id", cid)
                    if cinemas:
                        db.set("vue_cinemas", cinemas)
                        if not s.vue_cinema_name:
                            s.vue_cinema_name = next((c["name"] for c in cinemas if c["id"] == cid), "")
                    films = vue.parse(with_s, all_f)
                    vue.store(db, films, datetime.now(s.tz))
                    showing = sum(1 for f in films if f.sessions)
                    return f"{len(films)} films listed ({showing} with showtimes)"
                await step("Vue listings", do_vue)

            if tmdb:
                # 4. match Vue films to TMDB
                async def do_match():
                    listed = set(db.get("vue_listed_film_ids", []))
                    retry_log = db.get("match_attempts", {})
                    if db.get("match_rules_version") != MATCH_RULES_VERSION:
                        # matching rules changed: retry every automatic match with the new rules
                        db.x("UPDATE vue_films SET tmdb_id=NULL WHERE tmdb_override=0")
                        retry_log = {}
                        db.set("match_rules_version", MATCH_RULES_VERSION)
                    rows = [r for r in db.q("SELECT * FROM vue_films WHERE tmdb_id IS NULL AND tmdb_override=0")
                            if r["film_id"] in listed]
                    cutoff = (datetime.now() - timedelta(days=3)).isoformat()
                    todo = [r for r in rows if retry_log.get(r["film_id"], "") < cutoff]

                    async def one(r):
                        year = int(r["release_date"][:4]) if r["release_date"] else None
                        tid, note = await tmdb.match_vue(r["title"], r["original_title"], year, r["director"], r["kind"])
                        db.x("UPDATE vue_films SET tmdb_id=?, match_note=? WHERE film_id=?", (tid, note, r["film_id"]))
                        retry_log[r["film_id"]] = datetime.now().isoformat()
                        return tid
                    res = await asyncio.gather(*(one(r) for r in todo), return_exceptions=True)
                    db.set("match_attempts", retry_log)
                    ok = sum(1 for x in res if isinstance(x, int))
                    return f"matched {ok}/{len(todo)} new listings"
                await step("Match Vue films to TMDB", do_match)

                # 5. metadata
                async def do_meta():
                    listed = set(db.get("vue_listed_film_ids", []))
                    vue_ids = [r["tmdb_id"] for r in db.q("SELECT film_id, tmdb_id FROM vue_films WHERE tmdb_id IS NOT NULL")
                               if r["film_id"] in listed]
                    rated = [r["tmdb_id"] for r in db.q("SELECT tmdb_id FROM ratings")]
                    wl = [r["tmdb_id"] for r in db.q("SELECT tmdb_id FROM watchlist")]
                    a = await tmdb.details_many(vue_ids, max_age_days=3)
                    b = await tmdb.details_many(rated + wl, max_age_days=90)
                    return f"{len(a)} cinema films, {len(b)} of your films"
                await step("TMDB metadata", do_meta)

                # 5b. held-out model check (weekly, or when your ratings change a lot)
                async def do_check():
                    from .planner import training_rows
                    from .taste import evaluate
                    rows = training_rows(db)
                    prev = db.get("model_check") or {}
                    stale = prev.get("checked_at", "") < (datetime.now() - timedelta(days=7)).isoformat()
                    if not stale and abs(prev.get("films", 0) - len(rows)) < 25 and prev.get("version") == 3:
                        return f"up to date (best: {prev.get('best')})"
                    res = await asyncio.to_thread(evaluate, rows)
                    res.update(checked_at=datetime.now().isoformat(timespec="seconds"), version=3)
                    db.set("model_check", res)
                    if not res.get("ok"):
                        return res.get("why")
                    best = next(v for v in res["variants"] if v["variant"] == res["best"])
                    return (f"best: {res['best']}, off by {best['error']:.2f}★ on films it hadn't seen "
                            f"(guessing your average: {res['guess_average_error']:.2f}★); before release: "
                            f"{res['pre_release']['new']:.2f}★, was {res['pre_release']['old']:.2f}★")
                await step("Model check", do_check)

            # 6. Letterboxd community averages (cinema films first)
            if s.letterboxd_community:
                async def do_comm():
                    listed = set(db.get("vue_listed_film_ids", []))
                    stale = (datetime.now() - timedelta(days=2)).isoformat()  # cinema films: buzz moves quickly
                    rows = db.q("SELECT tmdb_id, fetched_at, rating_count, lists FROM community")
                    have = {r["tmdb_id"]: r["fetched_at"] for r in rows}
                    # read before rating counts and buzz were collected: fetch again, oldest first
                    no_counts = {r["tmdb_id"] for r in rows if r["rating_count"] is None and r["lists"] is None}
                    cinema = sorted((r["tmdb_id"] for r in db.q("SELECT film_id, tmdb_id FROM vue_films WHERE tmdb_id IS NOT NULL")
                                     if r["film_id"] in listed and (have.get(r["tmdb_id"], "") < stale
                                                                    or r["tmdb_id"] in no_counts)),
                                    key=lambda t: have.get(t, ""))
                    rated_ids = [r["tmdb_id"] for r in db.q("SELECT tmdb_id FROM ratings")]
                    rated = [t for t in rated_ids if t not in have] + sorted(
                        (t for t in rated_ids if t in no_counts), key=lambda t: have.get(t, ""))
                    todo = cinema[:s.community_fetch_limit] + rated[:s.community_backfill]
                    n = await letterboxd.fetch_community(db, todo, len(todo))
                    left = max(0, len(rated) - s.community_backfill)
                    return f"{n} averages and buzz fetched" + (f"; {left} of your rated films still to backfill" if left else "")
                await step("Letterboxd averages", do_comm)

            # 6b. Vue listing history (how long films last): first run a couple of minutes, then weekly top-ups
            if s.history_enabled:
                async def do_history():
                    from . import history
                    prev = db.get("history_import") or {}
                    if prev.get("imported_at", "") > (datetime.now() - timedelta(days=7)).isoformat():
                        return f"up to date ({prev.get('films')} runs, {prev.get('first')} to {prev.get('last')})"
                    r = await history.import_history(db, s.history_sites)
                    states = await asyncio.to_thread(history.build_states, db)
                    check = await asyncio.to_thread(history.backtest, states)
                    check["checked_at"] = datetime.now().isoformat(timespec="seconds")
                    db.set("run_check", check)
                    return f"{r['snapshots_added']} new snapshots; {r['films']} film runs from {r['first']} to {r['last']}"
                await step("Vue history", do_history)

            # 7. tickets
            async def do_tickets():
                added = tickets.auto_detect(db, now.date(), s.tickets)
                return f"auto-marked {len(added)}: " + ", ".join(a["title"] for a in added) if added else "no new cinema trips in your diary"
            await step("Ticket auto-detect", do_tickets)

            # 8. plan
            async def do_plan():
                p = self.replan()
                return f"{len(p['picks'])} picks, {len(p['leaving_soon'])} leaving soon"
            await step("Plan", do_plan)
        finally:
            self.running_step = None
            if tmdb:
                await tmdb.close()
        report["finished"] = datetime.now(s.tz).isoformat(timespec="seconds")
        report["ok"] = not report["errors"]
        db.set("last_refresh", report)
        return report

    async def set_override(self, film_id: str, tmdb_id: int | None) -> None:
        self.db.x("UPDATE vue_films SET tmdb_id=?, tmdb_override=1, match_note=? WHERE film_id=?",
                  (tmdb_id, "set by you" if tmdb_id else "marked as not a film", film_id))
        if tmdb_id and self.s.tmdb_key:
            t = TMDB(self.s.tmdb_key, self.db)
            try:
                await t.details(tmdb_id, 0)
            finally:
                await t.close()
        self.replan()
