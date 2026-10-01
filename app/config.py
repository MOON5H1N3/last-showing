"""Settings. Environment variables (.env) give the first-run defaults; anything changed on the
Settings page is saved in the database and wins over them. Secrets (tokens, API keys) only ever
come from .env, so they never appear on a web page."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _bool(name: str, default: bool) -> bool:
    v = _env(name, str(default)).lower()
    return v in ("1", "true", "yes", "on")


def _list(name: str, default: str = "") -> list[str]:
    return [x.strip().lower() for x in _env(name, default).split(",") if x.strip()]


@dataclass
class Settings:
    # Cinema
    vue_cinema_slug: str = field(default_factory=lambda: _env("VUE_CINEMA_SLUG", "bristol-cribbs-causeway"))
    vue_cinema_id: str = field(default_factory=lambda: _env("VUE_CINEMA_ID", ""))  # auto-discovered if blank
    vue_cinema_name: str = field(default_factory=lambda: _env("VUE_CINEMA_NAME", ""))

    # Tickets. Monzo Perks gives one Vue ticket a month.
    ticket_source: str = field(default_factory=lambda: _env("TICKET_SOURCE", "monzo"))  # monzo | custom | none
    tickets_per_month: int = field(default_factory=lambda: int(_env("TICKETS_PER_MONTH", "1")))
    ticket_chain: str = field(default_factory=lambda: _env("TICKET_CHAIN", "vue"))
    ticket_rollover: bool = field(default_factory=lambda: _bool("TICKET_ROLLOVER", False))
    # Session attributes a ticket can't be used for, e.g. "epic,3d,ultra-lux". Blank = anything goes.
    ticket_excluded_formats: list[str] = field(default_factory=lambda: _list("TICKET_EXCLUDED_FORMATS"))
    # 0 = always pick the best film, 1 = aggressively prioritise films about to leave
    urgency_weight: float = field(default_factory=lambda: float(_env("URGENCY_WEIGHT", "0.5")))
    # How much a film's chance of being a 4.5★+ favourite lifts it, on top of its predicted rating
    favourite_weight: float = field(default_factory=lambda: float(_env("FAVOURITE_WEIGHT", "1.0")))
    # how much more a film that suits the big screen is worth a cinema trip (0 = rank on enjoyment alone)
    big_screen_weight: float = field(default_factory=lambda: float(_env("BIG_SCREEN_WEIGHT", "0.5")))
    watchlist_boost: float = field(default_factory=lambda: float(_env("WATCHLIST_BOOST", "0.25")))
    include_events: bool = field(default_factory=lambda: _bool("INCLUDE_EVENTS", True))
    include_rereleases: bool = field(default_factory=lambda: _bool("INCLUDE_RERELEASES", True))
    include_seen: bool = field(default_factory=lambda: _bool("INCLUDE_SEEN", True))

    # Letterboxd
    letterboxd_user: str = field(default_factory=lambda: _env("LETTERBOXD_USERNAME", ""))
    letterboxd_community: bool = field(default_factory=lambda: _bool("LETTERBOXD_COMMUNITY_RATINGS", True))
    community_fetch_limit: int = field(default_factory=lambda: int(_env("LETTERBOXD_COMMUNITY_PER_RUN", "40")))
    # Your already-rated films, backfilled a batch per run (each takes ~4s; 150 is ~10 minutes)
    community_backfill: int = field(default_factory=lambda: int(_env("LETTERBOXD_BACKFILL_PER_RUN", "150")))

    # Past Vue listings (Clusterflick archive, CC BY 4.0) used to learn how long films stay on
    history_enabled: bool = field(default_factory=lambda: _bool("VUE_HISTORY", True))
    history_sites: list[str] = field(default_factory=lambda: _list(
        "VUE_HISTORY_SITES", "harrow,romford,croydon-purley-way,dagenham,eltham"))

    # TMDB
    tmdb_key: str = field(default_factory=lambda: _env("TMDB_API_KEY", ""))

    # Discord
    discord_token: str = field(default_factory=lambda: _env("DISCORD_BOT_TOKEN", ""))
    discord_user_id: int = field(default_factory=lambda: int(_env("DISCORD_USER_ID", "0") or 0))

    # Web
    port: int = field(default_factory=lambda: int(_env("PORT", "8095")))
    dashboard_password: str = field(default_factory=lambda: _env("DASHBOARD_PASSWORD", ""))
    public_url: str = field(default_factory=lambda: _env("PUBLIC_URL", "http://localhost:8095"))

    # Schedule
    timezone: str = field(default_factory=lambda: _env("TZ", "Europe/London"))
    monthly_day: int = field(default_factory=lambda: int(_env("MONTHLY_DAY", "1")))
    monthly_hour: int = field(default_factory=lambda: int(_env("MONTHLY_HOUR", "9")))
    refresh_hour: int = field(default_factory=lambda: int(_env("DAILY_REFRESH_HOUR", "6")))

    data_dir: Path = field(default_factory=lambda: Path(_env("DATA_DIR", "/data")))

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def db_path(self) -> Path:
        new, old = self.data_dir / "lastshowing.sqlite3", self.data_dir / "vuearr.sqlite3"
        if not new.exists() and old.exists():  # renamed from Vuearr: bring the database (and its WAL files) along
            for suffix in ("", "-wal", "-shm"):
                src = Path(str(old) + suffix)
                if src.exists():
                    src.rename(Path(str(new) + suffix))
        return new

    @property
    def tickets(self) -> int:
        """Tickets a month after applying the source preset."""
        if self.ticket_source == "none":
            return 0
        if self.ticket_source == "monzo":
            return max(1, self.tickets_per_month)
        return max(0, self.tickets_per_month)

    # ---- the Settings page ----
    # name -> kind. Only these can be changed from the web page; secrets are never listed.
    EDITABLE = {
        "ticket_source": "choice:monzo,custom,none", "tickets_per_month": "int:0:10", "ticket_chain": "choice:vue",
        "ticket_rollover": "bool", "ticket_excluded_formats": "list",
        "vue_cinema_slug": "slug", "vue_cinema_id": "text", "vue_cinema_name": "text",
        "urgency_weight": "float:0:1", "favourite_weight": "float:0:2", "watchlist_boost": "float:0:1",
        "big_screen_weight": "float:0:1",
        "include_events": "bool", "include_rereleases": "bool", "include_seen": "bool",
        "letterboxd_user": "slug", "letterboxd_community": "bool",
        "monthly_day": "int:1:28", "monthly_hour": "int:0:23", "refresh_hour": "int:0:23",
    }

    def apply(self, values: dict) -> None:
        for k, v in (values or {}).items():
            if k in self.EDITABLE:
                setattr(self, k, v)

    def editable_values(self) -> dict:
        return {k: getattr(self, k) for k in self.EDITABLE}

    @property
    def letterboxd_dir(self) -> Path:
        return self.data_dir / "letterboxd"

    def problems(self) -> list[str]:
        out = []
        if not self.tmdb_key:
            out.append("TMDB_API_KEY is not set - films can't be matched or scored.")
        if not self.letterboxd_user:
            out.append("LETTERBOXD_USERNAME is not set.")
        if not self.discord_token:
            out.append("DISCORD_BOT_TOKEN is not set - Discord DMs are off.")
        elif not self.discord_user_id:
            out.append("DISCORD_USER_ID is not set - the bot doesn't know who to DM.")
        return out


settings = Settings()
