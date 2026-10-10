"""Your predicted rating for any film, for other apps (Home Showing scores your Plex library with it).

POST /api/predict with {"tmdb_ids": [...]} (up to 500 at a time). Read-only: nothing in your plan changes. The ids
are remembered so the daily refresh slowly backfills their Letterboxd averages, which sharpens later predictions."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime

from .db import DB
from .taste import TasteModel, community_value

log = logging.getLogger(__name__)
MAX_IDS = 500
KEEP_IDS = 5000  # how many outside ids are remembered for the Letterboxd backfill


class ModelCache:
    """The taste model takes a second or two to fit, so it's kept until your ratings or the model check change."""

    def __init__(self):
        self.key = None
        self.model: TasteModel | None = None

    def get(self, db: DB) -> TasteModel:
        from .planner import build_model
        r = db.one("SELECT COUNT(*) AS n, MAX(rated_on) AS last FROM ratings")
        c = db.one("SELECT COUNT(*) AS n FROM community WHERE lb_avg IS NOT NULL")
        key = (r["n"], r["last"], c["n"], (db.get("model_check") or {}).get("checked_at"),
               datetime.now().date().isoformat())
        if self.model is None or key != self.key:
            self.model, self.key = build_model(db), key
        return self.model


def seen_ids(db: DB) -> dict[int, dict]:
    """Everything you've seen, by TMDB id: your rating if any, and when you last watched it."""
    out: dict[int, dict] = {}
    for r in db.q("SELECT tmdb_id FROM watched WHERE tmdb_id IS NOT NULL"):
        out.setdefault(r["tmdb_id"], {})
    for r in db.q("SELECT tmdb_id, rating FROM ratings"):
        out.setdefault(r["tmdb_id"], {})["rating"] = r["rating"]
    for r in db.q("SELECT tmdb_id, MAX(watched_date) AS last FROM diary WHERE tmdb_id IS NOT NULL GROUP BY tmdb_id"):
        out.setdefault(r["tmdb_id"], {})["last_watched"] = r["last"]
    for r in db.q("SELECT tmdb_id FROM ticket_uses WHERE active=1 AND tmdb_id IS NOT NULL"):
        out.setdefault(r["tmdb_id"], {})
    try:
        for r in db.q("SELECT tmdb_id FROM cinema_trips WHERE tmdb_id IS NOT NULL"):
            out.setdefault(r["tmdb_id"], {})
    except Exception:  # no trips table yet
        pass
    return out


def remember(db: DB, ids: list[int]) -> None:
    """Keep the ids for the Letterboxd-average backfill (newest asked-for first)."""
    old = db.get("outside_ids") or []
    merged = list(dict.fromkeys(list(ids) + old))[:KEEP_IDS]
    if merged != old:
        db.set("outside_ids", merged)


def parse_ids(raw) -> list[int]:
    out = []
    for v in raw or []:
        try:
            i = int(v)
        except (TypeError, ValueError):
            continue
        if i > 0:
            out.append(i)
    return list(dict.fromkeys(out))


def score(db: DB, model: TasteModel, metas: dict[int, dict], ids: list[int]) -> dict:
    comm = {r["tmdb_id"]: r for r in db.q("SELECT tmdb_id, lb_avg, rating_count FROM community")}
    seen = seen_ids(db)
    watchlist = {r["tmdb_id"] for r in db.q("SELECT tmdb_id FROM watchlist")}
    films, missing = [], []
    for tid in ids:
        meta = metas.get(tid)
        if not meta:
            missing.append(tid)
            continue
        crow = comm.get(tid)
        lb = crow["lb_avg"] if crow is not None else None
        value = community_value(lb, meta)
        pred = model.predict(meta, value, "Letterboxd average" if lb else "TMDB average (on a 5★ scale)",
                             n=crow["rating_count"] if lb else None)
        s = seen.get(tid)
        films.append({
            "tmdb_id": tid, "title": meta.get("title"), "year": meta.get("year"),
            "release_date": meta.get("release_date"), "runtime": meta.get("runtime"),
            "language": meta.get("language"), "genres": meta.get("genres") or [],
            "keywords": meta.get("keywords") or [], "directors": meta.get("directors") or [],
            "cast": (meta.get("cast") or [])[:4], "poster": meta.get("poster"), "backdrop": meta.get("backdrop"),
            "overview": meta.get("overview"), "trailer": meta.get("trailer"),
            "predicted": pred.rating, "fav_chance": pred.fav_chance, "confidence": pred.confidence,
            "reasons": pred.reasons, "community": value,
            "community_source": "letterboxd" if lb else ("tmdb" if value is not None else None),
            "seen": s is not None, "your_rating": (s or {}).get("rating"),
            "last_watched": (s or {}).get("last_watched"), "on_watchlist": tid in watchlist,
        })
    return {"films": films, "missing": missing}


async def predict(db: DB, cache: ModelCache, tmdb, ids: list[int]) -> dict:
    ids = ids[:MAX_IDS]
    model = await asyncio.to_thread(cache.get, db)
    if tmdb:
        metas = await tmdb.details_many(ids, max_age_days=90)
    else:  # no TMDB key: only films already in the cache can be scored
        marks = ",".join("?" * len(ids)) or "NULL"
        metas = {r["tmdb_id"]: json.loads(r["data"])
                 for r in db.q(f"SELECT tmdb_id, data FROM movies WHERE tmdb_id IN ({marks})", tuple(ids))}
    remember(db, ids)
    out = score(db, model, metas, ids)
    out["model"] = {"ratings": model.n, "average": round(model.mu, 2), "favourite_rate": round(model.fav_base, 3),
                    "error": round(model.mae, 2) if model.mae is not None else None}
    return out
