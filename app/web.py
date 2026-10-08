"""Web dashboard (Starlette): Plan, Films, Film, Taste and Settings pages."""
from __future__ import annotations

import asyncio
import base64
import logging
import re
import secrets
from datetime import date, datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup, escape
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import (FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse,
                                 Response)
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import settings_store, tickets, watch
from .jobs import Engine

log = logging.getLogger(__name__)
TEMPLATES = Environment(loader=FileSystemLoader(Path(__file__).parent / "templates"),
                        autoescape=select_autoescape(["html"]))
_bg: set[asyncio.Task] = set()
APP_NAME = "Last Showing"


def _spawn(coro):
    t = asyncio.create_task(coro)
    _bg.add(t)
    t.add_done_callback(_bg.discard)


def _fmt_dt(v: str | None, fmt: str) -> str:
    if not v:
        return ""
    try:
        d = datetime.fromisoformat(v) if "T" in v else datetime.combine(date.fromisoformat(v[:10]), datetime.min.time())
        return d.strftime(fmt).replace(" 0", " ")
    except ValueError:
        return v


def _cert_colour(cert: str | None) -> str:
    c = (cert or "").upper()
    return {"U": "u", "PG": "pg", "12": "c12", "12A": "c12", "15": "c15", "18": "c18"}.get(c, "cx")


def _int(v, default: int) -> int:
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return default


TEMPLATES.filters["dt"] = _fmt_dt
TEMPLATES.filters["certclass"] = _cert_colour
TEMPLATES.globals["app_name"] = APP_NAME
try:
    TEMPLATES.globals["version"] = (Path(__file__).resolve().parents[1] / "VERSION").read_text().strip()
except OSError:
    TEMPLATES.globals["version"] = "dev"


def _back(form, msg: str, default: str = "/") -> RedirectResponse:
    """Back to the page (and month) the person was on."""
    nxt = form.get("next") or default
    if not nxt.startswith("/") or nxt.startswith("//"):
        nxt = default
    sep = "&" if "?" in nxt else "?"
    return RedirectResponse(f"{nxt}{sep}msg=" + msg.replace(" ", "+"), 303)


class BasicAuth(BaseHTTPMiddleware):
    def __init__(self, app, password: str):
        super().__init__(app)
        self.password = password

    async def dispatch(self, request, call_next):
        if not self.password or request.url.path == "/health":
            return await call_next(request)
        header = request.headers.get("authorization", "")
        if header.startswith("Basic "):
            try:
                _, pw = base64.b64decode(header[6:]).decode().split(":", 1)
                if secrets.compare_digest(pw, self.password):
                    return await call_next(request)
            except Exception:
                pass
        return Response("Password required", status_code=401, headers={"WWW-Authenticate": 'Basic realm="Last Showing"'})


def create_app(engine: Engine, bot=None) -> Starlette:
    s, db = engine.s, engine.db

    def plan() -> dict:
        p = engine.plan()
        if not p:
            try:
                p = engine.replan()
            except Exception as e:  # first run, before any data
                log.info("no plan yet: %s", e)
                p = {}
        return p

    def render(name: str, request: Request, page: str, **extra) -> HTMLResponse:
        status = extra.pop("status_code", 200)
        p = extra.pop("plan", None) or plan()
        today = datetime.now(s.tz).date()
        ctx = dict(
            plan=p, s=s, today=today, page=page, busy=engine.busy, step=engine.running_step,
            msg=request.query_params.get("msg", ""), problems=s.problems(), open_alerts=db.get("alerts_open") or {},
            film_notices=[{**n, "html": Markup(re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", str(escape(n["text"]))))}
                          for n in watch.current_notices(db, today)],
            cinema_name=s.vue_cinema_name or s.vue_cinema_slug.replace("-", " ").title(),
            path=request.url.path + (f"?{request.url.query}" if request.url.query else ""),
            days_until=lambda v: (date.fromisoformat(v[:10]) - today).days if v else None,
        )
        ctx.update(extra)
        ctx["path"] = re.sub(r"[?&]msg=[^&]*", "", ctx["path"])
        return HTMLResponse(TEMPLATES.get_template(name).render(**ctx), status_code=status)

    # ---------------- pages ----------------
    async def plan_page(request: Request):
        p = plan()
        months = p.get("months") or []
        mi = min(_int(request.query_params.get("m"), 0), max(0, len(months) - 1))
        return render("plan.html", request, "plan", plan=p, mi=mi, mp=months[mi] if months else p,
                      month_used=list(p.get("uses", [])))

    async def films_page(request: Request):
        p = plan()
        films = list((p.get("films") or {}).values())
        sort = request.query_params.get("sort", "best")
        show = request.query_params.get("show", "all")
        today = datetime.now(s.tz).date().isoformat()
        if show == "now":
            films = [f for f in films if f["sessions"]]
        elif show == "soon":
            films = [f for f in films if not f["sessions"] or (f.get("release_date") or "") > today]
        key = {"best": lambda f: -(f["predicted"] or 0), "leaving": lambda f: f["est_end"],
               "opening": lambda f: (f.get("release_date") or "9999")}.get(sort, lambda f: -(f["predicted"] or 0))
        films.sort(key=key)
        return render("films.html", request, "films", films=films, sort=sort, show=show)

    async def film_page(request: Request):
        p = plan()
        fid = request.path_params["film_id"]
        film = (p.get("films") or {}).get(fid)
        if not film:
            return render("missing.html", request, "films", status_code=404)
        pinned = next((x["month"] for x in p.get("pins", []) if x["film_id"] == fid), None)
        # the months it could be pinned to: those where the plan expects it to be showing
        sections = ("picks", "paid_trips", "worth_paying", "at_home", "everything", "extras")
        pin_months = [(m["month"], m["month_name"]) for m in p.get("months", [])
                      if any(x["film_id"] == fid for sec in sections for x in m.get(sec, []))]
        if not pin_months and p.get("month"):
            pin_months = [(p["month"], datetime.now(s.tz).strftime("%B"))]
        return render("film.html", request, "films", f=film, pinned_month=pinned, pin_months=pin_months,
                      current_month=p.get("month"))

    async def taste_page(request: Request):
        from .history import source_summary
        from .planner import accuracy_check, buzz_check, run_accuracy
        return render("taste.html", request, "taste", run_check=db.get("run_check") or {},
                      hist=db.get("history_import") or {}, lb_import=db.get("letterboxd_import") or {},
                      bc=buzz_check(db), acc=accuracy_check(db), ra=run_accuracy(db, datetime.now(s.tz).date()),
                      run_sources=source_summary(db))

    async def settings_page(request: Request):
        cinemas = db.get("vue_cinemas") or []
        if not any(c["slug"] == s.vue_cinema_slug for c in cinemas):
            cinemas = [{"id": s.vue_cinema_id, "name": s.vue_cinema_name or s.vue_cinema_slug, "slug": s.vue_cinema_slug}] + cinemas
        from .history import source_summary
        return render("settings.html", request, "settings", cinemas=cinemas, run_sources=source_summary(db),
                      discord=db.get("discord_status") or {}, bot_ok=bool(bot and bot.is_ready()),
                      last=db.get("last_refresh") or {}, lb_import=db.get("letterboxd_import") or {},
                      rss=db.get("letterboxd_rss") or {}, hist=db.get("history_import") or {},
                      backups=sorted((s.data_dir / "backups").glob("lastshowing-*.sqlite3"))[-1:]
                      if (s.data_dir / "backups").exists() else [])

    # ---------------- settings ----------------
    async def settings_save(request: Request):
        form = await request.form()
        cinemas = db.get("vue_cinemas") or []
        old_slug = s.vue_cinema_slug
        changed = settings_store.save(db, s, settings_store.from_form(form, cinemas))
        engine.settings_saved()
        if s.vue_cinema_slug != old_slug:
            # a different cinema: its listings replace the old one's
            db.x("DELETE FROM sessions")
            db.x("DELETE FROM snapshots")
            db.set("vue_listed_film_ids", [])
            db.set("vue_cinema_id", s.vue_cinema_id)
            if not engine.busy:
                _spawn(engine.refresh())
            return _back(form, f"Switched to {s.vue_cinema_name or s.vue_cinema_slug}. Reading its listings now.",
                         "/settings")
        engine.replan()
        return _back(form, "Settings saved" if changed else "Nothing changed", "/settings")

    # ---------------- actions ----------------
    def _title(film_id: str) -> tuple[str, int | None]:
        row = db.one("SELECT title, tmdb_id FROM vue_films WHERE film_id=?", (film_id,))
        return (row["title"], row["tmdb_id"]) if row else (film_id, None)

    async def use(request: Request):
        form = await request.form()
        film_id = form.get("film_id") or None
        title = (form.get("title") or "").strip()
        tmdb_id = None
        if film_id:
            title, tmdb_id = _title(film_id)
        if not title:
            return _back(form, "Pick a film first")
        used_on = form.get("used_on") or datetime.now(s.tz).date().isoformat()
        tickets.add_use(db, film_id, tmdb_id, title, date.fromisoformat(used_on), "dashboard")
        engine.replan()
        return _back(form, f"Ticket used on {title}")

    async def undo(request: Request):
        form = await request.form()
        tickets.undo_use(db, int(form["use_id"]))
        engine.replan()
        return _back(form, "Ticket given back")

    async def pin(request: Request):
        form = await request.form()
        fid, month = form.get("film_id"), form.get("month")
        if not fid or not re.fullmatch(r"\d{4}-\d{2}", month or ""):
            return _back(form, "Couldn't pin that")
        db.x("INSERT OR REPLACE INTO pins(film_id,month,created_at) VALUES(?,?,?)",
             (fid, month, datetime.now().isoformat(timespec="seconds")))
        engine.replan()
        return _back(form, f"Pinned {_title(fid)[0]}. A ticket is kept for it.")

    async def want(request: Request):
        form = await request.form()
        fid = form.get("film_id")
        title, tid = _title(fid)
        if not fid:
            return _back(form, "Couldn't add that")
        db.x("INSERT OR REPLACE INTO wants(film_id,tmdb_id,title,created_at) VALUES(?,?,?,?)",
             (fid, tid, title, datetime.now().isoformat(timespec="seconds")))
        engine.replan()
        return _back(form, f"{title} is on your Want to see list. It gets a free ticket first, or a paid trip if none fits.")

    async def unwant(request: Request):
        form = await request.form()
        db.x("DELETE FROM wants WHERE film_id=?", (form.get("film_id"),))
        engine.replan()
        return _back(form, f"{_title(form.get('film_id'))[0]} is off your Want to see list")

    async def unpin(request: Request):
        form = await request.form()
        db.x("DELETE FROM pins WHERE film_id=?", (form.get("film_id"),))
        engine.replan()
        return _back(form, f"Unpinned {_title(form.get('film_id'))[0]}")

    async def dismiss(request: Request):
        form = await request.form()
        fid = form.get("film_id")
        title, tid = _title(fid)
        db.x("INSERT OR REPLACE INTO dismissed(film_id,tmdb_id,title,created_at) VALUES(?,?,?,?)",
             (fid, tid, title, datetime.now().isoformat(timespec="seconds")))
        db.x("DELETE FROM pins WHERE film_id=?", (fid,))
        engine.replan()
        nxt = form.get("next") or "/"
        if nxt.startswith("/film/"):
            nxt = "/films"
        return _back({"next": nxt}, f"{title} won't be suggested again. Bring it back under Settings.")

    async def undismiss(request: Request):
        form = await request.form()
        db.x("DELETE FROM dismissed WHERE film_id=?", (form.get("film_id"),))
        engine.replan()
        return _back(form, f"{_title(form.get('film_id'))[0]} is back in the running", "/settings")

    async def refresh(request: Request):
        form = await request.form()
        if not engine.busy:
            _spawn(engine.refresh())
        return _back(form, "Refreshing now. This takes a minute or two.")

    async def history_update(request: Request):
        form = await request.form()
        db.set("history_import", {**(db.get("history_import") or {}), "imported_at": ""})  # due now
        if not engine.busy:
            _spawn(engine.refresh(vue_listings=False))
        return _back(form, "Updating the run-length history", "/settings")

    async def upload(request: Request):
        form = await request.form()
        f = form.get("export")
        if not f or not getattr(f, "filename", "").lower().endswith(".zip"):
            return _back(form, "Choose the Letterboxd export .zip file", "/settings")
        s.letterboxd_dir.mkdir(parents=True, exist_ok=True)
        name = re.sub(r"[^A-Za-z0-9._-]", "_", f.filename)
        (s.letterboxd_dir / name).write_bytes(await f.read())
        if not engine.busy:
            _spawn(engine.refresh(vue_listings=False))
        return _back(form, "Export uploaded. Importing your ratings now.", "/settings")

    async def match(request: Request):
        form = await request.form()
        raw = (form.get("tmdb") or "").strip().lower()
        if raw in ("none", "skip", "0"):
            tid = None
        else:
            m = re.search(r"(\d+)", raw)
            if not m:
                return _back(form, "Paste a TMDB link or number", "/films")
            tid = int(m.group(1))
        await engine.set_override(form["film_id"], tid)
        return _back(form, "Match saved", "/films")

    async def send_now(request: Request):
        form = await request.form()
        if not bot:
            return _back(form, "Discord isn't set up", "/settings")
        ok = await bot.send_monthly_plan(plan())
        return _back(form, "Sent to Discord" if ok else "Couldn't send the DM. Check the Discord box.", "/settings")

    async def backup(request: Request):
        dest = engine.backup()
        if not dest:
            return PlainTextResponse("Backup failed; see the logs.", status_code=500)
        return FileResponse(dest, filename=dest.name, media_type="application/octet-stream")

    async def api_plan(request: Request):
        return JSONResponse(plan())

    async def api_seen(request: Request):
        return JSONResponse(tickets.seen_feed(db))

    async def health(request: Request):
        return PlainTextResponse("ok")

    routes = [
        Mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static"),
        Route("/", plan_page),
        Route("/films", films_page),
        Route("/film/{film_id}", film_page),
        Route("/taste", taste_page),
        Route("/settings", settings_page),
        Route("/settings", settings_save, methods=["POST"]),
        Route("/use", use, methods=["POST"]),
        Route("/undo", undo, methods=["POST"]),
        Route("/pin", pin, methods=["POST"]),
        Route("/unpin", unpin, methods=["POST"]),
        Route("/want", want, methods=["POST"]),
        Route("/unwant", unwant, methods=["POST"]),
        Route("/dismiss", dismiss, methods=["POST"]),
        Route("/undismiss", undismiss, methods=["POST"]),
        Route("/refresh", refresh, methods=["POST"]),
        Route("/history", history_update, methods=["POST"]),
        Route("/upload", upload, methods=["POST"]),
        Route("/match", match, methods=["POST"]),
        Route("/send", send_now, methods=["POST"]),
        Route("/backup", backup),
        Route("/api/plan", api_plan),
        Route("/api/seen", api_seen),
        Route("/health", health),
    ]
    return Starlette(routes=routes, middleware=[Middleware(BasicAuth, password=s.dashboard_password)])
