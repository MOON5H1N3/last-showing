"""Break alerts: a Discord DM only when something stops working, and a short note when it's fixed.

* A rejected key (TMDB, OMDb, the Guardian) alerts straight away: it won't fix itself.
* Any other refresh step alerts once it has failed twice in a row, since one failure is often a blip.
* Recommendations never trigger an alert.
"""
from __future__ import annotations

from datetime import datetime

from .db import DB

FAILS_BEFORE_ALERT = 2
KEY_WORDS = ("refused the key",)


def _is_key_problem(message: str) -> bool:
    return any(w in (message or "") for w in KEY_WORDS)


def assess(db: DB, report: dict, now: datetime | None = None) -> list[str]:
    """Update the failure streaks from a refresh report; return the messages to send (new problems and fixes)."""
    now = now or datetime.now()
    streaks: dict = db.get("alert_streaks") or {}
    open_alerts: dict = db.get("alerts_open") or {}
    messages: list[str] = []
    for st in report.get("steps", []):
        name, ok, result = st["step"], st["ok"], str(st.get("result") or "")
        if ok:
            streaks.pop(name, None)
            if name in open_alerts:
                since = open_alerts.pop(name)["since"][:10]
                messages.append(f"Fixed: **{name}** is working again. It had been failing since {since}.")
            continue
        streaks[name] = streaks.get(name, 0) + 1
        if name in open_alerts:
            continue  # already told you
        if _is_key_problem(result):
            open_alerts[name] = {"since": now.isoformat(timespec="minutes"), "error": result[:300]}
            messages.append(f"Key problem: **{name}** stopped working. {result[:300]}\n"
                            "Check the key in your .env, then run `docker compose up -d --force-recreate`.")
        elif streaks[name] >= FAILS_BEFORE_ALERT:
            open_alerts[name] = {"since": now.isoformat(timespec="minutes"), "error": result[:300]}
            messages.append(f"Not working: **{name}** has failed on the last {streaks[name]} refreshes. {result[:300]}\n"
                            "Everything else still works. The logs (`docker logs last-showing`) say more.")
    db.set("alert_streaks", streaks)
    db.set("alerts_open", open_alerts)
    return messages
