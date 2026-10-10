"""The read-only prediction feed other apps use (POST /api/predict)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.test_core import CINEMA_META, seeded_db  # noqa: E402
from tests.test_web import client  # noqa: E402


class TestPredictFeed(unittest.TestCase):
    def test_scores_and_seen(self):
        c, _, db = client()
        ids = [m["id"] for m in CINEMA_META.values()][:3]
        rated = db.one("SELECT tmdb_id, rating FROM ratings")
        r = c.post("/api/predict", json={"tmdb_ids": ids + [rated["tmdb_id"], 999999999, "junk"]})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        got = {f["tmdb_id"]: f for f in body["films"]}
        self.assertEqual(set(got), set(ids) | {rated["tmdb_id"]})
        self.assertEqual(body["missing"], [999999999])  # no TMDB key in tests: only cached films can be scored
        for f in got.values():
            self.assertTrue(0.5 <= f["predicted"] <= 5.0)
            self.assertIn("genres", f)
            self.assertIn("keywords", f)
        self.assertTrue(got[rated["tmdb_id"]]["seen"])
        self.assertEqual(got[rated["tmdb_id"]]["your_rating"], rated["rating"])
        self.assertFalse(got[ids[0]]["seen"])
        self.assertGreater(body["model"]["ratings"], 25)
        # remembered for the Letterboxd backfill
        self.assertIn(ids[0], db.get("outside_ids"))

    def test_bad_request(self):
        c, _, _ = client()
        self.assertEqual(c.post("/api/predict", json={}).status_code, 400)
        self.assertEqual(c.post("/api/predict", content=b"nope").status_code, 400)
        self.assertEqual(c.get("/api/predict").status_code, 405)

    def test_model_cached(self):
        from app import predict
        db = seeded_db()
        cache = predict.ModelCache()
        m1 = cache.get(db)
        self.assertIs(cache.get(db), m1)
        db.x("INSERT INTO ratings(tmdb_id,rating,rated_on,source) VALUES(1,4,'2026-10-09','rss')")
        self.assertIsNot(cache.get(db), m1)  # a new rating: refitted


if __name__ == "__main__":
    unittest.main()
