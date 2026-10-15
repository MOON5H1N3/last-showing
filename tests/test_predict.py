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

    def test_few_tmdb_votes_count_for_less(self):
        """A new release's TMDB average from 50 votes moves the prediction less than the same average from 5,000."""
        import json
        from app import predict
        from app.planner import build_model
        db = seeded_db()
        model = build_model(db)
        model.no_comm = None
        model.w["community"] = 0.7  # a model that does lean on the crowd
        base = json.loads(db.one("SELECT data FROM movies WHERE tmdb_id=?", (CINEMA_META["HO1"]["id"],))["data"])
        metas = {tid: {**base, "id": tid, "vote_count": votes, "vote_average": avg, "collection": {"id": 7, "name": "A Series"}}
                 for tid, votes, avg in ((1, 50, 9.0), (2, 5000, 9.0), (3, 5000, 5.0))}
        got = {f["tmdb_id"]: f for f in predict.score(db, model, metas, [1, 2, 3])["films"]}
        few, many, low = got[1]["predicted"], got[2]["predicted"], got[3]["predicted"]
        self.assertGreater(many, few + 0.3)  # the glowing average counts fully only when it's from many votes
        self.assertGreater(few, low)
        self.assertEqual((got[1]["community_source"], got[1]["community_votes"]), ("tmdb", 50))
        self.assertIn("counts for less", got[1]["reasons"][0])
        self.assertEqual(got[1]["collection"], {"id": 7, "name": "A Series"})

    def test_ruled_out_shared(self):
        """Home Showing's Not for me hides the film here too, by TMDB id; removing it brings it back."""
        c, _, db = client()
        tid = CINEMA_META["HO1"]["id"]
        ids = lambda: [f["film_id"] for f in c.get("/api/plan").json()["all_candidates"]]  # noqa: E731
        self.assertIn("HO1", ids())
        r = c.post("/api/ruled-out", json={"add": [{"tmdb_id": tid, "title": "Resident Evil"}, {"tmdb_id": "junk"}]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual([(d["film_id"], d["title"]) for d in r.json()["dismissed"]], [(f"tmdb:{tid}", "Resident Evil")])
        self.assertNotIn("HO1", ids())  # the Vue listing is caught by its TMDB id
        self.assertIn("ruled out in Home Showing", c.get("/settings").text)
        c.post("/api/ruled-out", json={"add": [{"tmdb_id": tid, "title": "Again"}]})  # already hidden: left alone
        self.assertEqual(db.one("SELECT COUNT(*) n FROM dismissed")["n"], 1)
        c.post("/api/ruled-out", json={"remove": [tid]})
        self.assertIn("HO1", ids())
        self.assertEqual(c.post("/api/ruled-out", content=b"nope").status_code, 400)

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
