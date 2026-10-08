"""Entry point: dashboard, Discord bot and scheduler in one process."""
from __future__ import annotations

import asyncio
import logging
import signal
from datetime import datetime, timedelta

import uvicorn

from . import settings_store
from .config import settings
from .db import DB
from .jobs import Engine
from .web import create_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("lastshowing")
# httpx logs every request URL, and TMDB v3 keys travel in the URL, so keep it quiet
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("discord.client").setLevel(logging.ERROR)  # "voice will NOT be supported" noise


def next_run(now: datetime, hour: int, day: int | None = None) -> datetime:
    """Next time it's `hour`:00, on day-of-month `day` if given (every day otherwise)."""
    cand = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if day is None:
        return cand if cand > now else cand + timedelta(days=1)
    for months_ahead in range(0, 3):
        y, m = now.year + (now.month - 1 + months_ahead) // 12, (now.month - 1 + months_ahead) % 12 + 1
        try:
            c = cand.replace(year=y, month=m, day=day)
        except ValueError:  # e.g. day 31 in a short month
            continue
        if c > now:
            return c
    raise RuntimeError("no next run")


async def scheduler(engine: Engine, bot) -> None:
    s = engine.s
    while True:
        now = datetime.now(s.tz)
        daily = next_run(now, s.refresh_hour)
        monthly = next_run(now, s.monthly_hour, s.monthly_day)
        target, kind = (monthly, "monthly") if monthly <= daily else (daily, "daily")
        log.info("next %s run at %s", kind, target.isoformat(timespec="minutes"))
        version = engine.settings_version
        while (wait := (target - datetime.now(s.tz)).total_seconds()) > 0 and engine.settings_version == version:
            try:  # wake when settings change (new refresh time etc.), or every 10 minutes regardless
                await asyncio.wait_for(engine.settings_changed.wait(), timeout=min(wait, 600))
            except asyncio.TimeoutError:
                pass
            engine.settings_changed.clear()
        if engine.settings_version != version:
            continue  # re-plan the schedule with the new times
        try:
            await engine.refresh()
            if kind == "daily":
                engine.backup()
            if kind == "monthly" and bot:
                sent = await bot.send_monthly_plan(engine.plan())
                log.info("monthly plan %s", "sent" if sent else "NOT sent")
            engine.db.set(f"last_{kind}_run", datetime.now(s.tz).isoformat(timespec="seconds"))
        except Exception:
            log.exception("%s run failed", kind)
        await asyncio.sleep(61)


async def main() -> None:
    s = settings
    db = DB(s.db_path)
    settings_store.load(db, s)
    for p in s.problems():
        log.warning(p)
    if not db.get("installed_month"):
        db.set("installed_month", datetime.now(s.tz).strftime("%Y-%m"))
    # "I'm seeing this" and "Want to see" are one button now: a pinned film is a wanted film with its month chosen
    db.x("""INSERT OR IGNORE INTO wants(film_id,tmdb_id,title,created_at)
            SELECT p.film_id, v.tmdb_id, COALESCE(v.title, p.film_id), p.created_at
            FROM pins p LEFT JOIN vue_films v ON v.film_id=p.film_id""")
    engine = Engine(s, db)

    bot = None
    if s.discord_token and s.discord_user_id:
        from .bot import VueBot
        bot = VueBot(engine)
        engine.notify = bot.send_alerts

    server = uvicorn.Server(uvicorn.Config(create_app(engine, bot), host="0.0.0.0", port=s.port,
                                           log_level="warning", lifespan="off"))
    server.install_signal_handlers = lambda: None
    tasks = [asyncio.create_task(server.serve(), name="web"),
             asyncio.create_task(scheduler(engine, bot), name="scheduler")]
    if bot:
        async def run_bot():
            try:
                await bot.start(s.discord_token)
            except Exception as e:
                log.error("Discord bot stopped: %s (the dashboard keeps working)", e)
        tasks.append(asyncio.create_task(run_bot(), name="discord"))
    log.info("dashboard on port %s", s.port)

    # First start: fetch everything straight away rather than waiting for the morning
    if not db.get("last_refresh"):
        tasks.append(asyncio.create_task(engine.refresh(), name="first-refresh"))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    await stop.wait()
    log.info("shutting down")
    server.should_exit = True
    if bot:
        await bot.close()
    for t in tasks:
        t.cancel()


if __name__ == "__main__":
    asyncio.run(main())
