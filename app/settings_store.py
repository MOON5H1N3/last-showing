"""Saving and loading the Settings page."""
from __future__ import annotations

import re

from .config import Settings
from .db import DB

KEY = "settings"


def load(db: DB, s: Settings) -> None:
    """Apply saved settings over the .env defaults."""
    s.apply(db.get(KEY) or {})


def _coerce(kind: str, raw, current):
    name = kind.split(":")[0]
    if name == "bool":
        return raw in (True, "on", "true", "1", "yes")
    if name == "int":
        lo, hi = (int(x) for x in kind.split(":")[1:])
        return min(hi, max(lo, int(float(raw))))
    if name == "float":
        lo, hi = (float(x) for x in kind.split(":")[1:])
        return round(min(hi, max(lo, float(raw))), 3)
    if name == "choice":
        options = kind.split(":", 1)[1].split(",")
        return raw if raw in options else current
    if name == "slug":
        raw = str(raw).strip().lower()
        return raw if re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,80}", raw) else current
    if name == "list":
        items = raw if isinstance(raw, list) else str(raw).split(",")
        return sorted({str(x).strip().lower() for x in items if str(x).strip()})
    return str(raw).strip()[:200]


def save(db: DB, s: Settings, values: dict) -> list[str]:
    """Validates and saves the given settings. Returns the names that changed."""
    saved = dict(db.get(KEY) or {})
    changed = []
    for k, raw in values.items():
        kind = s.EDITABLE.get(k)
        if not kind:
            continue
        try:
            v = _coerce(kind, raw, getattr(s, k))
        except (TypeError, ValueError):
            continue
        if v != getattr(s, k):
            changed.append(k)
        saved[k] = v
        setattr(s, k, v)
    if s.ticket_source == "monzo" and s.tickets_per_month < 1:
        s.tickets_per_month = saved["tickets_per_month"] = 1
    db.set(KEY, saved)
    return changed


def from_form(form, cinemas: list[dict]) -> dict:
    """The Settings page's form fields -> setting values. Sliders arrive as 0-100."""
    v: dict = {}
    for k in ("ticket_source", "ticket_chain", "letterboxd_user", "theme"):
        if form.get(k) is not None:
            v[k] = form.get(k)
    for k in ("tickets_per_month", "monthly_day", "monthly_hour", "refresh_hour"):
        if form.get(k) not in (None, ""):
            v[k] = form.get(k)
    v["ticket_rollover"] = form.get("ticket_expiry") == "rollover"
    v["ticket_excluded_formats"] = form.getlist("excluded_formats") if hasattr(form, "getlist") else []
    for k in ("include_events", "include_rereleases", "include_seen", "letterboxd_community"):
        v[k] = form.get(k) == "on"
    scale = {"urgency_weight": 100, "favourite_weight": 50, "watchlist_boost": 100, "big_screen_weight": 100}
    for k, div in scale.items():
        if form.get(k) not in (None, ""):
            v[k] = float(form.get(k)) / div
    if form.get("nearby_present"):
        v["nearby_cinemas"] = form.getlist("nearby_cinemas") if hasattr(form, "getlist") else []
    slug = form.get("vue_cinema_slug")
    if slug:
        match = next((c for c in cinemas if c["slug"] == slug), None)
        v["vue_cinema_slug"] = slug
        if match:
            v["vue_cinema_id"], v["vue_cinema_name"] = match["id"], match["name"]
    return v
