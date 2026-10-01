"""Early reviews, shown next to each film so you can judge for yourself (they never change predictions).

* Critics: the Rotten Tomatoes and Metacritic scores (through OMDb, free key) and the Guardian's star rating
  (Guardian Open Platform, free key).
* Audience: the Letterboxd average, once enough people have rated the film (collected elsewhere).
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime

import httpx

from .db import DB
from .tmdb import norm

log = logging.getLogger(__name__)
OMDB = "https://www.omdbapi.com/"
GUARDIAN = "https://content.guardianapis.com/search"

# where "the critics like it" and "the audience likes it" start, and where they turn against it
CRITICS_GOOD, CRITICS_POOR = 65, 45          # 0-100 (Rotten Tomatoes %, Metacritic, Guardian stars x 20)
AUDIENCE_GOOD, AUDIENCE_POOR = 3.5, 3.0      # Letterboxd average (a typical film sits around 3.2)
AUDIENCE_MIN_RATINGS = 50


def parse_omdb(d: dict) -> dict:
    """Rotten Tomatoes %, Metascore and IMDb rating from an OMDb response (None where missing)."""
    if not d or d.get("Response") == "False":
        return {"rt": None, "metascore": None, "imdb": None, "imdb_id": None}
    rt = next((r.get("Value") for r in d.get("Ratings") or [] if r.get("Source") == "Rotten Tomatoes"), None)

    def num(v, cast=int):
        try:
            return cast(str(v).rstrip("%")) if v not in (None, "", "N/A") else None
        except ValueError:
            return None
    return {"rt": num(rt), "metascore": num(d.get("Metascore")), "imdb": num(d.get("imdbRating"), float),
            "imdb_id": d.get("imdbID")}


def pick_guardian(results: list[dict], title: str) -> tuple[int | None, str | None]:
    """The Guardian's review of this film: its headlines read '<Title> review – <tagline>'."""
    want = norm(title)
    for r in results or []:
        stars = (r.get("fields") or {}).get("starRating")
        head = norm(r.get("webTitle", ""))
        if stars and (head.startswith(want + " review") or head == want + " review"):
            try:
                return int(stars), r.get("webUrl")
            except ValueError:
                continue
    return None, None


def verdict(rt: int | None, metascore: int | None, guardian: int | None,
            lb_avg: float | None, lb_n: int | None) -> dict:
    """Critics and audience, each 'good', 'mixed' or 'poor' (or None if there's nothing yet), with a label."""
    critic_vals = [v for v in (rt, metascore, guardian * 20 if guardian else None) if v is not None]
    critics = None
    if critic_vals:
        c = sum(critic_vals) / len(critic_vals)
        critics = "good" if c >= CRITICS_GOOD else "poor" if c < CRITICS_POOR else "mixed"
    audience = None
    if lb_avg and (lb_n or 0) >= AUDIENCE_MIN_RATINGS:
        audience = "good" if lb_avg >= AUDIENCE_GOOD else "poor" if lb_avg < AUDIENCE_POOR else "mixed"
    word = {"good": "✔", "mixed": "split", "poor": "✘"}
    parts = []
    if critics:
        parts.append(f"Critics {word[critics]}")
    if audience:
        parts.append(f"Audiences {word[audience]}")
    detail = []
    if rt is not None:
        detail.append(f"Rotten Tomatoes {rt}%")
    if metascore is not None:
        detail.append(f"Metacritic {metascore}")
    if guardian:
        detail.append("Guardian " + "★" * guardian + "☆" * (5 - guardian))
    if lb_avg and (lb_n or 0) >= AUDIENCE_MIN_RATINGS:
        detail.append(f"Letterboxd {lb_avg:.1f}★ from {lb_n:,} ratings")
    return {"critics": critics, "audience": audience, "label": " ".join(parts), "detail": ", ".join(detail),
            "rt": rt, "metascore": metascore, "guardian": guardian}


async def fetch_reviews(db: DB, omdb_key: str, guardian_key: str, films: list[dict]) -> tuple[int, int]:
    """films: dicts with tmdb_id, title, year, imdb_id (optional). Returns (films with critic scores, Guardian reviews)."""
    if not films or not (omdb_key or guardian_key):
        return 0, 0
    now = datetime.now().isoformat(timespec="seconds")
    got_scores = got_guardian = 0
    async with httpx.AsyncClient(timeout=20, headers={"User-Agent": "last-showing (self-hosted)"}) as c:
        for f in films:
            old = db.one("SELECT * FROM reviews WHERE tmdb_id=?", (f["tmdb_id"],))
            rec = dict(old) if old else {"tmdb_id": f["tmdb_id"]}
            if omdb_key:
                try:
                    params = {"apikey": omdb_key}
                    if f.get("imdb_id") or rec.get("imdb_id"):
                        params["i"] = f.get("imdb_id") or rec.get("imdb_id")
                    else:
                        params.update(t=f["title"], type="movie")
                        if f.get("year"):
                            params["y"] = f["year"]
                    r = await c.get(OMDB, params=params)
                    if r.status_code == 401:
                        raise PermissionError("OMDb refused the key in OMDB_API_KEY")
                    o = parse_omdb(r.json())
                    rec.update(rt=o["rt"], metascore=o["metascore"], imdb_rating=o["imdb"],
                               imdb_id=o["imdb_id"] or rec.get("imdb_id"))
                    if o["rt"] is not None or o["metascore"] is not None:
                        got_scores += 1
                except PermissionError:
                    raise
                except Exception as e:
                    log.debug("OMDb failed for %s: %s", f["title"], e)
            if guardian_key:
                try:
                    params = {"api-key": guardian_key, "q": f'"{f["title"]}"', "section": "film",
                              "tag": "tone/reviews", "show-fields": "starRating", "page-size": 10}
                    if f.get("year"):
                        params["from-date"] = f"{int(f['year']) - 1}-01-01"
                    r = await c.get(GUARDIAN, params=params)
                    if r.status_code in (401, 403):
                        raise PermissionError("The Guardian refused the key in GUARDIAN_API_KEY")
                    stars, url = pick_guardian(((r.json().get("response") or {}).get("results")) or [], f["title"])
                    if stars:
                        rec.update(guardian_stars=stars, guardian_url=url)
                        got_guardian += 1
                except PermissionError:
                    raise
                except Exception as e:
                    log.debug("Guardian failed for %s: %s", f["title"], e)
            db.x("""INSERT OR REPLACE INTO reviews(tmdb_id,imdb_id,rt,metascore,imdb_rating,guardian_stars,guardian_url,fetched_at)
                    VALUES(?,?,?,?,?,?,?,?)""",
                 (rec["tmdb_id"], rec.get("imdb_id"), rec.get("rt"), rec.get("metascore"), rec.get("imdb_rating"),
                  rec.get("guardian_stars"), rec.get("guardian_url"), now))
            await asyncio.sleep(0.3)
    return got_scores, got_guardian
