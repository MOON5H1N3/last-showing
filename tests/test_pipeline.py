"""End-to-end refresh with the network faked out: Vue, Letterboxd RSS and TMDB."""
import asyncio
import csv
import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import jobs, letterboxd, tmdb, vue  # noqa: E402
from app.db import DB  # noqa: E402
from tests.sample_data import CINEMA_META, training_set, vue_responses  # noqa: E402
from tests.test_core import FIX, settings  # noqa: E402

TRAIN = training_set()
BY_TITLE = {m["title"]: m for m in list(CINEMA_META.values()) + [m for m, _ in TRAIN]}
BY_ID = {m["id"]: m for m in BY_TITLE.values()}


def fake_tmdb(request: httpx.Request) -> httpx.Response:
    path = request.url.path.replace("/3", "", 1)
    q = parse_qs(urlparse(str(request.url)).query)
    if path == "/search/movie":
        title = q["query"][0]
        hits = [m for t, m in BY_TITLE.items() if t.lower() == title.lower()]
        return httpx.Response(200, json={"results": [{"id": m["id"], "title": m["title"], "original_title": m["title"],
                                                      "release_date": f"{m['year']}-01-01", "vote_count": 100} for m in hits]})
    if path.endswith("/credits"):
        m = BY_ID.get(int(path.split("/")[2]))
        return httpx.Response(200, json={"crew": [{"job": "Director", "name": d} for d in (m or {}).get("directors", [])]})
    if path.startswith("/movie/"):
        m = BY_ID.get(int(path.split("/")[2]))
        if not m:
            return httpx.Response(404, json={})
        return httpx.Response(200, json={
            "id": m["id"], "title": m["title"], "original_title": m["title"], "release_date": f"{m['year']}-01-01",
            "runtime": 100, "original_language": "en", "genres": [{"name": g} for g in m["genres"]],
            "credits": {"crew": [{"job": "Director", "name": d} for d in m["directors"]],
                        "cast": [{"name": c, "order": i} for i, c in enumerate(m["cast"])]},
            "keywords": {"keywords": [{"name": k} for k in m["keywords"]]},
            "production_companies": [], "production_countries": [], "vote_average": 7.0, "vote_count": 500})
    return httpx.Response(404, json={})


class TestPipeline(unittest.TestCase):
    def test_refresh(self):
        s = settings(tmdb_key="k" * 32, letterboxd_user="someone", letterboxd_community=False, history_enabled=False)
        s.letterboxd_dir.mkdir(parents=True)
        # export zip with the training ratings
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["Date", "Name", "Year", "Letterboxd URI", "Rating"])
        for m, r in TRAIN:
            w.writerow(["2025-01-01", m["title"], m["year"], "u", r])
        with zipfile.ZipFile(s.letterboxd_dir / "letterboxd-someone.zip", "w") as z:
            z.writestr("ratings.csv", buf.getvalue())

        real_client = httpx.AsyncClient

        def client(*a, **kw):
            kw["transport"] = httpx.MockTransport(fake_tmdb)
            return real_client(*a, **kw)

        async def fake_fetch(slug, cid="", cinemas_out=None, nearby=None, nearby_out=None):
            if cinemas_out is not None:
                cinemas_out.append({"id": "10018", "name": "Bristol Cribbs Causeway", "slug": "bristol-cribbs-causeway"})
            if nearby_out is not None and nearby:  # a nearby Vue: same listings shape, kept only as history
                nearby_out[nearby[0]] = {"name": "Swindon", "response": vue_responses()[0]}
            return "10018", *vue_responses()

        async def fake_rss(db, user):
            return letterboxd.store_rss(db, letterboxd.parse_rss((FIX / "rss.xml").read_text()))

        tmdb.httpx.AsyncClient = client
        vue.fetch, letterboxd.poll_rss = fake_fetch, fake_rss
        try:
            db = DB(s.db_path)
            eng = jobs.Engine(s, db)
            report = asyncio.run(eng.refresh())
        finally:
            tmdb.httpx.AsyncClient = real_client
        for st in report["steps"]:
            print(f"  {st['step']}: {st['ok']} {st['result']}")
        self.assertTrue(report["ok"], report["errors"])
        plan = eng.plan()
        self.assertEqual(plan["model"]["ratings"], len(TRAIN))
        matched = {r["film_id"]: r["tmdb_id"] for r in db.q("SELECT film_id, tmdb_id FROM vue_films")}
        self.assertEqual(matched["HO1"], 9001)
        self.assertEqual(matched["HO5"], 36557)  # 'Casino Royale (20th Anniversary)' -> Casino Royale
        self.assertIsNone(matched["HO10"])       # opera: no confident match
        from app.planner import make_plan
        from tests.test_core import NOW
        self.assertTrue(make_plan(db, s, NOW)["picks"])
        if s.nearby_cinemas:  # nearby Vues land in the run-length history, not in your listings
            self.assertIn(s.nearby_cinemas[0], db.get("nearby_names") or {})  # read and kept as history (the
            # sample showtimes are from early October 2026, so whether any are still ahead depends on today's date)
            self.assertIn("nearby Vue", next(st["result"] for st in report["steps"] if st["step"] == "Vue listings"))
        print("  unmatched:", [(r["title"], r["match_note"]) for r in db.q("SELECT * FROM vue_films WHERE tmdb_id IS NULL")])
        # second run: export not re-imported
        tmdb.httpx.AsyncClient = client
        try:
            r2 = asyncio.run(eng.refresh(vue_listings=False))
        finally:
            tmdb.httpx.AsyncClient = real_client
        self.assertIn("already imported", r2["steps"][0]["result"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
