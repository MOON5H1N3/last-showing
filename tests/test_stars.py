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
