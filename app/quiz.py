"""The taste quiz: a quick start for someone with no Letterboxd (or anything else) to learn from.

Two parts, both optional and both changeable later:
  * genres: Like / Fine / Skip. Liked genres nudge a guess up a little, skipped ones down more (see taste.StarterModel).
  * well-known films: Loved / Liked / Meh / Not for me / Haven't seen. Answers are stored as ratings (source "quiz"),
    so they teach the model exactly as Letterboxd ratings do, and count towards the 25 it needs to move from the
    starter model to the full one.
"""
from __future__ import annotations

import json
from datetime import date

from .db import DB

GENRES = ["Action", "Adventure", "Animation", "Comedy", "Crime", "Documentary", "Drama", "Family", "Fantasy",
          "History", "Horror", "Music", "Mystery", "Romance", "Science Fiction", "Thriller", "War", "Western"]
ANSWERS = {"loved": 4.5, "liked": 3.5, "meh": 2.5, "no": 1.5}
LABELS = [("loved", "Loved it"), ("liked", "Liked it"), ("meh", "Meh"), ("no", "Not for me"), ("unseen", "Haven't seen")]


def get(db: DB) -> dict:
    q = db.get("quiz") or {}
    return {"likes": list(q.get("likes") or []), "avoids": list(q.get("avoids") or []),
            "unseen": list(q.get("unseen") or [])}


def save_genres(db: DB, form) -> tuple[list[str], list[str]]:
    q = get(db)
    q["likes"] = [g for g in GENRES if form.get(f"g:{g}") == "like"]
    q["avoids"] = [g for g in GENRES if form.get(f"g:{g}") == "skip"]
    db.set("quiz", q)
    return q["likes"], q["avoids"]


def answered(db: DB) -> dict[int, float]:
    return {r["tmdb_id"]: r["rating"] for r in db.q("SELECT tmdb_id, rating FROM ratings WHERE source='quiz'")}


def answer(db: DB, tmdb_id: int, choice: str) -> None:
    q = get(db)
    q["unseen"] = [t for t in q["unseen"] if t != tmdb_id]
    db.x("DELETE FROM ratings WHERE tmdb_id=? AND source='quiz'", (tmdb_id,))
    if choice in ANSWERS:
        db.x("INSERT OR REPLACE INTO ratings(tmdb_id, rating, rated_on, source) VALUES(?,?,?,'quiz')",
             (tmdb_id, ANSWERS[choice], date.today().isoformat()))
    elif choice == "unseen":
        q["unseen"].append(tmdb_id)
    db.set("quiz", q)


def films(db: DB, n: int = 12) -> list[dict]:
    """Well-known films to ask about: the most-voted on TMDB, a mix of genres and decades, none asked before."""
    done = set(answered(db)) | set(get(db)["unseen"])
    done |= {r["tmdb_id"] for r in db.q("SELECT tmdb_id FROM ratings")}
    pool = []
    for r in db.q("SELECT tmdb_id, data FROM movies"):
        if r["tmdb_id"] in done:
            continue
        m = json.loads(r["data"])
        if (m.get("vote_count") or 0) < 3000 or not m.get("genres") or (m.get("year") or 0) < 1970:
            continue
        pool.append(m | {"tmdb_id": r["tmdb_id"]})
    pool.sort(key=lambda m: -(m.get("vote_count") or 0))
    out, used_genre = [], {}
    for m in pool:  # round the genres so it isn't twelve superhero films
        g = m["genres"][0]
        if used_genre.get(g, 0) >= max(1, n // 6):
            continue
        used_genre[g] = used_genre.get(g, 0) + 1
        out.append({"tmdb_id": m["tmdb_id"], "title": m.get("title"), "year": m.get("year"),
                    "poster": m.get("poster"), "genres": m["genres"][:2]})
        if len(out) >= n:
            break
    return out


def progress(db: DB) -> dict:
    n = db.one("SELECT COUNT(*) n FROM ratings")["n"]
    return {"ratings": n, "needed": 25, "quiz": len(answered(db))}
