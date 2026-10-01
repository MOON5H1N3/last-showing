"""TMDB: matching titles to films, and the metadata the taste model learns from."""
from __future__ import annotations

import asyncio
import difflib
import json
import logging
import re
import unicodedata
from datetime import datetime, timedelta

import httpx

from .db import DB

log = logging.getLogger(__name__)
API = "https://api.themoviedb.org/3"


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = s.replace("&", "and")
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


LANGUAGES = ("hindi|tamil|telugu|malayalam|punjabi|kannada|arabic|nepali|bengali|urdu|korean|japanese|polish|cantonese|"
             "mandarin|chinese|french|spanish|german|italian|turkish|persian|farsi|gujarati|marathi|sinhala|thai|"
             "vietnamese|portuguese|russian|ukrainian|romanian|somali|kurdish|tagalog|filipino|greek|hebrew|dutch|"
             "swedish|danish|norwegian|irish|welsh|dubbed|subtitled|subbed|english subtitles|original version")
LANG_SUFFIX = re.compile(rf"\((?:{LANGUAGES})\)", re.I)


def clean_vue_title(title: str) -> tuple[str, int | None]:
    """'Casino Royale (20th Anniversary)' -> ('Casino Royale', None); 'Coraline (2009)' -> ('Coraline', 2009)."""
    year = None
    m = re.search(r"\(((?:19|20)\d{2})\)", title)
    if m:
        year = int(m.group(1))
    t = LANG_SUFFIX.sub("", title)
    t = re.sub(r"\((?:\d+(?:st|nd|rd|th) anniversary|re-?release|encore|[^)]*\d{4}[^)]*)\)", "", t, flags=re.I)
    t = re.sub(r"\b(encore|4k|remastered)\b", "", t, flags=re.I)
    return re.sub(r"\s+", " ", t).strip(" -:"), year


class TMDB:
    def __init__(self, key: str, db: DB):
        self.db = db
        self.key = key
        headers, self.params = {"accept": "application/json"}, {}
        if len(key) > 40:  # v4 "read access token"
            headers["Authorization"] = f"Bearer {key}"
        else:
            self.params = {"api_key": key}
        self.client = httpx.AsyncClient(base_url=API, headers=headers, timeout=20)
        self.sem = asyncio.Semaphore(8)

    async def close(self):
        await self.client.aclose()

    async def get(self, path: str, **params) -> dict:
        async with self.sem:
            for attempt in range(4):
                r = await self.client.get(path, params={**self.params, **params})
                if r.status_code == 429:
                    await asyncio.sleep(1 + attempt)
                    continue
                if r.status_code == 404:
                    return {}
                if r.status_code == 401:
                    raise PermissionError("TMDB refused the key in TMDB_API_KEY")
                r.raise_for_status()
                return r.json()
        return {}

    # ---------- search ----------
    async def search(self, title: str, year: int | None = None) -> list[dict]:
        params = {"query": title, "include_adult": "false", "region": "GB"}
        if year:
            params["year"] = year
        return (await self.get("/search/movie", **params)).get("results", [])

    async def search_tv(self, title: str, year: int | None = None) -> list[dict]:
        params = {"query": title, "include_adult": "false"}
        if year:
            params["first_air_date_year"] = year
        return (await self.get("/search/tv", **params)).get("results", [])

    async def match_title(self, title: str, year: int | None) -> tuple[int | None, str]:
        """Letterboxd title + year -> (TMDB film id, kind), cached. kind is 'film', 'tv' or 'none'.

        Letterboxd also lists some TV (miniseries, specials, single episodes). Those are detected and skipped
        rather than being matched to an unrelated film with a similar name."""
        key = f"v2|{norm(title)}|{year or ''}"
        row = self.db.one("SELECT tmdb_id FROM title_map WHERE key=?", (key,))
        if row:
            tid = row["tmdb_id"]
            return (None, "tv") if tid == -1 else (tid, "film") if tid else (None, "none")
        best, sim = None, 0.0
        for y in ([year, None] if year else [None]):
            results = await self.search(title, y)
            cand = self._best(title, year, results, min_sim=0.6, max_year_gap=1 if year else None)
            if cand:
                best = cand
                sim = self._sim(title, cand)
                break
        kind = "film" if best else "none"
        if not best or sim < 0.95:
            # a close TV match beats a loose film match
            for tv in (await self.search_tv(title, year))[:5] or (await self.search_tv(title))[:5]:
                tv_sim = max(difflib.SequenceMatcher(None, norm(title), norm(tv.get(k, ""))).ratio()
                             for k in ("name", "original_name"))
                ty = int((tv.get("first_air_date") or "0")[:4] or 0)
                if tv_sim >= 0.9 and tv_sim > sim and (not year or not ty or abs(ty - year) <= 1):
                    best, kind = None, "tv"
                    break
        tid = best["id"] if best else (-1 if kind == "tv" else None)
        self.db.x("INSERT OR REPLACE INTO title_map(key,tmdb_id) VALUES(?,?)", (key, tid))
        return (best["id"] if best else None), kind

    @staticmethod
    def _sim(title: str, r: dict) -> float:
        nt = norm(title)
        return max(difflib.SequenceMatcher(None, nt, norm(r.get(k, ""))).ratio() for k in ("title", "original_title"))

    @staticmethod
    def _best(title: str, year: int | None, results: list[dict], min_sim: float = 0.55,
              max_year_gap: int | None = None) -> dict | None:
        nt = norm(title)
        scored = []
        for i, r in enumerate(results[:10]):
            names = {norm(r.get("title", "")), norm(r.get("original_title", ""))}
            sim = max(difflib.SequenceMatcher(None, nt, n).ratio() for n in names)
            ry = int((r.get("release_date") or "0")[:4] or 0)
            ydiff = abs(ry - year) if (year and ry) else 3
            if max_year_gap is not None and year and (not ry or ydiff > max_year_gap):
                continue
            score = sim * 2 - min(ydiff, 3) * 0.25 - i * 0.05 + min((r.get("vote_count") or 0), 2000) / 8000
            scored.append((score, sim, r))
        scored.sort(key=lambda x: -x[0])
        if scored and scored[0][1] >= min_sim:
            return scored[0][2]
        return None

    async def match_vue(self, title: str, original_title: str, release_year: int | None, director: str,
                        kind: str) -> tuple[int | None, str]:
        """Match a Vue listing. Uses the director (when Vue gives one) to settle ambiguous titles."""
        # Vue's "original title" is sometimes in another script or just wrong, so try its display title too
        names = [n for n in dict.fromkeys(clean_vue_title(t)[0] for t in (title, original_title) if t) if n]
        if not names:
            return None, "empty title"
        clean, paren_year = clean_vue_title(title)
        paren_year = paren_year or clean_vue_title(original_title or "")[1]
        years = [paren_year] if paren_year else ([release_year, release_year - 1 if release_year else None]
                                                  if kind == "film" else [None])
        candidates: list[dict] = []
        for name in names:
            for y in dict.fromkeys(years + [None]):
                res = await self.search(name, y)
                candidates += [r for r in res if r["id"] not in {c["id"] for c in candidates}]
                if res and y is not None:
                    break
        if not candidates:
            return None, f"no TMDB results for '{clean}'"
        want = {norm(d) for d in re.split(r",|&| and ", director or "") if d.strip()}
        if want:
            for r in candidates[:5]:
                credits = await self.get(f"/movie/{r['id']}/credits")
                dirs = {norm(c["name"]) for c in credits.get("crew", []) if c.get("job") == "Director"}
                if dirs & want:
                    return r["id"], "matched on title + director"
        year_hint = paren_year or (release_year if kind == "film" else None)
        # a new release must be a recent film: no matching "My Wife & the Dog" (2026) to a 1971 namesake
        gap = 2 if (kind == "film" and year_hint) else None
        found = [b for b in (self._best(n, year_hint, candidates, min_sim=0.75 if kind == "event" else 0.6,
                                        max_year_gap=gap) for n in names) if b]
        if not found:
            return None, f"no confident match for '{clean}'"
        best = max(found, key=lambda b: max(self._sim(n, b) for n in names))
        return best["id"], "matched on title + year"

    # ---------- details ----------
    async def details(self, tmdb_id: int, max_age_days: int = 14) -> dict:
        row = self.db.one("SELECT data, fetched_at FROM movies WHERE tmdb_id=?", (tmdb_id,))
        if row and datetime.fromisoformat(row["fetched_at"]) > datetime.now() - timedelta(days=max_age_days):
            return json.loads(row["data"])
        d = await self.get(f"/movie/{tmdb_id}", append_to_response="credits,keywords,videos")
        if not d:
            return json.loads(row["data"]) if row else {}
        slim = slim_movie(d)
        self.db.x("INSERT OR REPLACE INTO movies(tmdb_id,data,fetched_at) VALUES(?,?,?)",
                  (tmdb_id, json.dumps(slim), datetime.now().isoformat(timespec="seconds")))
        return slim

    async def details_many(self, ids, max_age_days: int = 60) -> dict[int, dict]:
        ids = [i for i in dict.fromkeys(ids) if i]
        results = await asyncio.gather(*(self.details(i, max_age_days) for i in ids), return_exceptions=True)
        out = {}
        for i, r in zip(ids, results):
            if isinstance(r, PermissionError):
                raise r  # a rejected key isn't a one-film problem
            if isinstance(r, Exception):
                log.warning("TMDB details failed for %s: %s", i, r)
            elif r:
                out[i] = r
        return out


def slim_movie(d: dict) -> dict:
    """Keep only what we use, so the cache stays small."""
    credits = d.get("credits") or {}
    crew = credits.get("crew") or []
    return {
        "id": d.get("id"),
        "imdb_id": d.get("imdb_id"),
        "title": d.get("title"),
        "original_title": d.get("original_title"),
        "year": int((d.get("release_date") or "0")[:4] or 0) or None,
        "release_date": d.get("release_date"),
        "runtime": d.get("runtime"),
        "language": d.get("original_language"),
        "genres": [g["name"] for g in d.get("genres") or []],
        "directors": [c["name"] for c in crew if c.get("job") == "Director"],
        "writers": list(dict.fromkeys(c["name"] for c in crew if c.get("job") in ("Screenplay", "Writer", "Story"))),
        "dop": [c["name"] for c in crew if c.get("job") == "Director of Photography"][:1],
        "composer": [c["name"] for c in crew if c.get("job") == "Original Music Composer"][:1],
        "cast": [c["name"] for c in sorted(credits.get("cast") or [], key=lambda c: c.get("order", 99))[:8]],
        "companies": [c["name"] for c in (d.get("production_companies") or [])[:3]],
        "countries": [c["iso_3166_1"] for c in (d.get("production_countries") or [])[:2]],
        "keywords": [k["name"] for k in ((d.get("keywords") or {}).get("keywords") or [])[:25]],
        "vote_average": d.get("vote_average"),
        "vote_count": d.get("vote_count"),
        "popularity": d.get("popularity"),
        "poster": ("https://image.tmdb.org/t/p/w342" + d["poster_path"]) if d.get("poster_path") else None,
        "backdrop": ("https://image.tmdb.org/t/p/w1280" + d["backdrop_path"]) if d.get("backdrop_path") else None,
        "overview": d.get("overview"),
        "trailer": _trailer(d),
    }


def _trailer(d: dict) -> str | None:
    """Best YouTube trailer: official trailers first, English first, newest first."""
    vids = [v for v in ((d.get("videos") or {}).get("results") or []) if v.get("site") == "YouTube" and v.get("key")]
    if not vids:
        return None
    vids.sort(key=lambda v: (v.get("type") != "Trailer", not v.get("official"), v.get("iso_639_1") not in (None, "en"),
                             "" if not v.get("published_at") else "~" + v["published_at"][::-1]))
    return f"https://www.youtube.com/watch?v={vids[0]['key']}"
