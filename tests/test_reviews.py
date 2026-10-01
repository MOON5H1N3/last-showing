"""Early reviews: parsing OMDb and the Guardian, the critics/audience verdict, and the refresh step."""
import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import reviews  # noqa: E402
from app.planner import make_plan  # noqa: E402
from tests.sample_data import CINEMA_META  # noqa: E402
from tests.test_core import NOW, seeded_db, settings  # noqa: E402

OMDB = {"Response": "True", "imdbID": "tt123", "Metascore": "81", "imdbRating": "7.9",
        "Ratings": [{"Source": "Internet Movie Database", "Value": "7.9/10"}, {"Source": "Rotten Tomatoes", "Value": "92%"}]}
GUARDIAN = {"response": {"results": [
    {"webTitle": "Clayface and the art of horror – interview", "fields": {}},
    {"webTitle": "Clayface review – a shape-shifting delight", "webUrl": "https://theguardian.com/x", "fields": {"starRating": "4"}},
]}}


class TestParsing(unittest.TestCase):
    def test_omdb(self):
        self.assertEqual(reviews.parse_omdb(OMDB), {"rt": 92, "metascore": 81, "imdb": 7.9, "imdb_id": "tt123"})
        self.assertEqual(reviews.parse_omdb({"Response": "False"})["rt"], None)
        self.assertIsNone(reviews.parse_omdb({"Response": "True", "Metascore": "N/A"})["metascore"])

    def test_guardian_picks_the_review_not_the_interview(self):
        self.assertEqual(reviews.pick_guardian(GUARDIAN["response"]["results"], "Clayface"), (4, "https://theguardian.com/x"))
        self.assertEqual(reviews.pick_guardian([], "Clayface"), (None, None))

    def test_verdict(self):
        v = reviews.verdict(92, 81, 4, 3.9, 2000)
        self.assertEqual((v["critics"], v["audience"]), ("good", "good"))
        self.assertEqual(v["label"], "Critics ✔ Audiences ✔")
        self.assertIn("Rotten Tomatoes 92%", v["detail"])
        split = reviews.verdict(85, None, None, 3.1, 500)
        self.assertEqual(split["label"], "Critics ✔ Audiences split")
        self.assertEqual(reviews.verdict(None, None, None, 3.9, 10)["label"], "")  # too few ratings to say
        self.assertEqual(reviews.verdict(30, 35, 1, None, None)["critics"], "poor")


class TestRefresh(unittest.TestCase):
    def test_fetch_and_badge(self):
        db = seeded_db()

        def handler(req: httpx.Request):
            if "omdbapi" in req.url.host:
                return httpx.Response(200, json=OMDB)
            return httpx.Response(200, json=GUARDIAN)

        real = httpx.AsyncClient
        with mock.patch.object(reviews.httpx, "AsyncClient",
                               lambda **kw: real(transport=httpx.MockTransport(handler), **kw)):
            film = CINEMA_META["HO8"]  # Clayface
            n_scores, n_guardian = asyncio.run(reviews.fetch_reviews(
                db, "omdb-key", "guardian-key", [{"tmdb_id": film["id"], "title": "Clayface", "year": 2026}]))
        self.assertEqual((n_scores, n_guardian), (1, 1))
        row = db.one("SELECT * FROM reviews WHERE tmdb_id=?", (film["id"],))
        self.assertEqual((row["rt"], row["metascore"], row["guardian_stars"]), (92, 81, 4))
        p = make_plan(db, settings(), NOW)
        self.assertEqual(p["films"]["HO8"]["reviews"]["critics"], "good")

    def test_bad_key_is_reported(self):
        db = seeded_db()
        real = httpx.AsyncClient
        with mock.patch.object(reviews.httpx, "AsyncClient",
                               lambda **kw: real(transport=httpx.MockTransport(lambda r: httpx.Response(401)), **kw)):
            with self.assertRaises(PermissionError):
                asyncio.run(reviews.fetch_reviews(db, "bad", "", [{"tmdb_id": 1, "title": "X", "year": 2026}]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
