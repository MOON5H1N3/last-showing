"""Break alerts: keys alert at once, other steps after two failures in a row, and fixes are reported."""
import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import alerts  # noqa: E402
from app.db import DB  # noqa: E402


def report(**steps):
    return {"steps": [{"step": k.replace("_", " "), "ok": v is True, "result": "fine" if v is True else v}
                      for k, v in steps.items()]}


class TestAlerts(unittest.TestCase):
    def test_step_alerts_after_two_failures_then_fixed(self):
        db = DB(":memory:")
        self.assertEqual(alerts.assess(db, report(Vue_listings="timed out", Plan=True)), [])  # a blip
        msgs = alerts.assess(db, report(Vue_listings="timed out", Plan=True))
        self.assertEqual(len(msgs), 1)
        self.assertIn("Vue listings", msgs[0])
        self.assertIn("last 2 refreshes", msgs[0])
        self.assertEqual(alerts.assess(db, report(Vue_listings="timed out")), [])  # not repeated
        fixed = alerts.assess(db, report(Vue_listings=True))
        self.assertEqual(len(fixed), 1)
        self.assertIn("Fixed", fixed[0])
        self.assertEqual(db.get("alerts_open"), {})

    def test_key_alerts_straight_away(self):
        db = DB(":memory:")
        msgs = alerts.assess(db, report(TMDB_metadata="TMDB refused the key in TMDB_API_KEY"))
        self.assertEqual(len(msgs), 1)
        self.assertIn("Key problem", msgs[0])
        self.assertIn("force-recreate", msgs[0])
        self.assertEqual(alerts.assess(db, report(TMDB_metadata="TMDB refused the key in TMDB_API_KEY")), [])

    def test_one_failure_after_success_resets(self):
        db = DB(":memory:")
        alerts.assess(db, report(Early_reviews="boom"))
        alerts.assess(db, report(Early_reviews=True))
        self.assertEqual(alerts.assess(db, report(Early_reviews="boom")), [])  # streak restarted


class TestEngineSendsAlerts(unittest.TestCase):
    def test_refresh_calls_notify(self):
        import asyncio
        from app.jobs import Engine
        from tests.test_core import seeded_db, settings
        s = settings()
        s.tmdb_key = ""
        s.letterboxd_user = ""
        s.letterboxd_community = False
        s.history_enabled = False
        s.omdb_key = s.guardian_key = ""
        db = seeded_db()
        eng = Engine(s, db)
        sent = []

        async def notify(msgs):
            sent.extend(msgs)
        eng.notify = notify
        db.set("alert_streaks", {"Plan": 1})
        import app.jobs as J
        orig = J.make_plan
        J.make_plan = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("planner broke"))
        try:
            asyncio.run(eng.refresh(vue_listings=False))
        finally:
            J.make_plan = orig
        self.assertTrue(any("Plan" in m and "planner broke" in m for m in sent))


if __name__ == "__main__":
    unittest.main(verbosity=2)
