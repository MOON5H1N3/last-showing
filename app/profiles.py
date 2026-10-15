"""Profiles: more than one person on one Last Showing (e.g. you and your dad).

The main profile is the one Last Showing has always had, in the main database. Every other profile has its own
small file under data/profiles/ for its own things (ratings from the taste quiz, tickets, Want to see, hidden
films, trips, its plan), and reads everything about the cinema and the films from the main database (see
db.ProfileDB). Its own settings are only the personal ones (name, tickets); everything else is shared.

The web app always works "as" one profile, chosen by a cookie. The proxies below hand each page the database,
settings and planner of whoever is looking, so the pages themselves don't need to know about profiles.
"""
from __future__ import annotations

import contextvars
import copy
import re
from pathlib import Path

from .db import ProfileDB

MAIN = "me"
current: contextvars.ContextVar[str] = contextvars.ContextVar("profile", default=MAIN)
PERSONAL_SETTINGS = ("ticket_source", "tickets_per_month", "ticket_rollover", "ticket_excluded_formats", "theme")


class Profiles:
    def __init__(self, main_engine):
        self.main = main_engine
        self._engines: dict[str, object] = {}

    # ---- the list ----
    def all(self) -> list[dict]:
        return [{"id": MAIN, "name": self.main.db.get("profile_name") or "You"}] + list(self.main.db.get("profiles") or [])

    def exists(self, pid: str) -> bool:
        return any(p["id"] == pid for p in self.all())

    def name(self, pid: str) -> str:
        return next((p["name"] for p in self.all() if p["id"] == pid), "You")

    def add(self, name: str, tickets: int = 2) -> str:
        name = (name or "").strip()[:30] or "Guest"
        base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "guest"
        pid, n = base, 2
        while self.exists(pid) or pid == MAIN:
            pid, n = f"{base}-{n}", n + 1
        self.main.db.set("profiles", list(self.main.db.get("profiles") or []) + [{"id": pid, "name": name}])
        eng = self.engine(pid)
        eng.db.set("profile_settings", {"ticket_source": "monzo", "tickets_per_month": max(0, min(10, tickets))})
        return pid

    def rename(self, pid: str, name: str) -> None:
        name = (name or "").strip()[:30]
        if not name:
            return
        if pid == MAIN:
            self.main.db.set("profile_name", name)
        else:
            self.main.db.set("profiles", [dict(p, name=name) if p["id"] == pid else p
                                          for p in (self.main.db.get("profiles") or [])])

    # ---- each profile's own pieces ----
    def path(self, pid: str) -> Path:
        return Path(self.main.s.data_dir) / "profiles" / f"{pid}.sqlite3"

    def settings(self, pid: str):
        if pid == MAIN:
            return self.main.s
        s = copy.copy(self.main.s)
        for k, v in (self.engine(pid).db.get("profile_settings") or {}).items():
            if k in PERSONAL_SETTINGS:
                setattr(s, k, v)
        s.letterboxd_user = ""  # a profile without Letterboxd
        return s

    def engine(self, pid: str):
        if pid == MAIN or not self.exists(pid):
            return self.main
        eng = self._engines.get(pid)
        if eng is None:
            from .jobs import Engine
            eng = Engine(copy.copy(self.main.s), ProfileDB(self.path(pid), self.main.db.path))
            self._engines[pid] = eng
        return eng

    def remove(self, pid: str) -> None:
        """Takes the profile off the list. Its file is kept (renamed), so nothing is lost by accident."""
        if pid == MAIN:
            return
        self.main.db.set("profiles", [p for p in (self.main.db.get("profiles") or []) if p["id"] != pid])
        eng = self._engines.pop(pid, None)
        if eng is not None:
            eng.db._conn.close()
        f = self.path(pid)
        if f.exists():
            f.rename(f.with_name(f"{f.stem}-removed-{len(list(f.parent.glob(f.stem + '-removed-*'))) + 1}.sqlite3"))

    def replan_all(self) -> None:
        """After the morning refresh: everyone else's plan, from the fresh listings, and a notice on their plan page
        if a film they want stops being listed (Discord messages are the main profile's, so no DM)."""
        from datetime import datetime

        from . import watch
        for p in self.all()[1:]:
            eng = self.engine(p["id"])
            eng.s = self.settings(p["id"])
            try:
                watch.check(eng.db, eng.s.vue_cinema_name or eng.s.vue_cinema_slug, datetime.now(eng.s.tz))
            except Exception:
                pass
            try:
                eng.replan()
            except Exception:  # one profile's plan never stops the rest
                pass

    def active(self):
        pid = current.get()
        eng = self.engine(pid)
        if eng is not self.main:
            eng.s = self.settings(pid)
        return eng


class DBProxy:
    """The database of whoever is looking."""
    def __init__(self, profiles: Profiles):
        object.__setattr__(self, "_p", profiles)

    def __getattr__(self, name):
        return getattr(self._p.active().db, name)


class SettingsProxy:
    """Settings as whoever is looking sees them: shared ones, plus their own tickets."""
    def __init__(self, profiles: Profiles):
        object.__setattr__(self, "_p", profiles)

    def __getattr__(self, name):
        return getattr(self._p.active().s, name)

    def __setattr__(self, name, value):
        setattr(self._p.main.s, name, value)  # only the main profile changes shared settings


class EngineProxy:
    """Planning follows whoever is looking; refreshes, backups and the schedule are the main profile's."""
    PER_PROFILE = {"plan", "replan", "db", "s"}

    def __init__(self, profiles: Profiles):
        object.__setattr__(self, "_p", profiles)

    def __getattr__(self, name):
        if name in self.PER_PROFILE:
            return getattr(self._p.active(), name)
        return getattr(self._p.main, name)
