"""Predictions shown as the half-star you'd give on Letterboxd."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.stars import half, label, other  # noqa: E402


class TestHalfStars(unittest.TestCase):
    def test_nearest_half_star(self):
        self.assertEqual([label(v) for v in (3.9, 3.6, 3.25, 2.75, 0.6, 5.3, 4.0)],
                         ["4★", "3½★", "3½★", "3★", "½★", "5★", "4★"])
        self.assertEqual(half(3.24), 3.0)

    def test_close_calls_offer_the_other(self):
        self.assertEqual(other(3.74), "4★")
        self.assertEqual(other(3.76), "3½★")
        self.assertEqual(other(3.6), "")   # comfortably 3½★
        self.assertEqual(other(None), "")


class TestScoreInHalfStars(unittest.TestCase):
    def test_accuracy_counts_spot_on_and_within_half(self):
        from app.db import DB
        from app.planner import accuracy_check
        db = DB(":memory:")
        for tid, pred, rating in ((1, 3.8, 4.0), (2, 3.3, 3.5), (3, 4.1, 3.5), (4, 2.2, 4.0)):
            db.x("INSERT INTO predictions(tmdb_id,film_id,title,predicted,recorded_on) VALUES(?,?,?,?,'2026-10-01')",
                 (tid, f"F{tid}", f"Film {tid}", pred))
            db.x("INSERT INTO ratings(tmdb_id,rating) VALUES(?,?)", (tid, rating))
        acc = accuracy_check(db)
        self.assertEqual(acc["count"], 4)
        self.assertEqual(acc["spot_on"], 0.5)       # 3.8 -> 4★ and 3.3 -> 3½★ exact
        self.assertEqual(acc["within_half"], 0.75)  # 4.1 -> 4★ vs 3½★ is one step; 2.2 -> 2★ vs 4★ isn't


class TestCrowdDifference(unittest.TestCase):
    def test_affinity_is_how_you_differ_from_the_crowd(self):
        """A director everyone loves only counts if you love them more than everyone does."""
        from app.taste import TasteModel

        def film(i, director, crowd, you):
            meta = {"id": i, "title": f"F{i}", "year": 2015 + i % 8, "runtime": 100 + i % 40, "genres": ["Drama"],
                    "directors": [director], "cast": [f"Actor {i % 13}"], "keywords": [], "companies": [],
                    "countries": ["GB"], "collection": None, "popularity": 10 + i % 7, "vote_count": 500}
            return (meta, you, crowd, 5000)
        rows = [film(i, "Crowd Favourite", 4.3, 4.0 + (i % 2) * 0.5) for i in range(12)]    # you agree with the crowd
        rows += [film(100 + i, "Your Favourite", 3.2, 4.5) for i in range(12)]            # you love them more
        rows += [film(200 + i, f"Other {i}", c, c) for i, c in enumerate([2.5, 3.0, 3.5, 4.0, 4.5] * 8)]  # you track the crowd
        m = TasteModel().fit(rows)
        yours, crowds = m._rdev("director", "Your Favourite")[0], m._rdev("director", "Crowd Favourite")[0]
        self.assertGreater(yours, 0.3)
        self.assertLess(abs(crowds), 0.3)
        self.assertGreater(yours - crowds, 0.3)
        # what you see is still against your own average, so a crowd favourite you rate highly shows as one
        self.assertGreater(m._dev("director", "Crowd Favourite")[0], 0.3)
        self.assertIn("m_lb_count", m.w)
