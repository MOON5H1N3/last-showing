"""How long films have left: the end of Vue's published week isn't moved by previews or far-off one-offs."""
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.planner import SURE_GONE, RunEstimate, estimate_run, published_until, range_text  # noqa: E402
from datetime import timedelta  # noqa: E402

TODAY = date(2026, 10, 2)


def film(fid, release, kind="film"):
    return {"film_id": fid, "release_date": release, "kind": kind}


def sess(*days):
    return [{"start": f"{d}T19:00:00"} for d in days]


class TestPublishedWeek(unittest.TestCase):
    def test_previews_and_one_offs_ignored(self):
        films = [film("A", "2026-10-02"), film("B", "2026-09-18"), film("C", "2026-09-04"),
                 film("HOCUS", "1993-07-16"),          # a re-release listed as a film, with a Halloween showing
                 film("PREVIEW", "2026-10-09")]        # opens next week, advance showings on sale
        sessions = {"A": sess("2026-10-02", "2026-10-08"), "B": sess("2026-10-03", "2026-10-08"),
                    "C": sess("2026-10-03", "2026-10-05"), "HOCUS": sess("2026-10-03", "2026-10-31"),
                    "PREVIEW": sess("2026-10-09", "2026-10-11")}
        self.assertEqual(published_until(films, sessions, TODAY), date(2026, 10, 8))

    def test_full_week_not_leaving_short_week_is(self):
        until = date(2026, 10, 8)
        full = estimate_run("film", "Universal", date(2026, 10, 2), [date(2026, 10, 2), date(2026, 10, 8)], [], TODAY, until)
        self.assertNotEqual(full.basis, "listing")
        short = estimate_run("film", "Universal", date(2026, 9, 4), [date(2026, 10, 3), date(2026, 10, 5)], [], TODAY, until)
        self.assertEqual(short.basis, "listing")
        self.assertIn("likely leaving", short.note)


class TestHonestRanges(unittest.TestCase):
    def curve(self, gone_from, spread):
        """A run model's answer: on for sure until `gone_from` days ahead, then tailing off over `spread` days."""
        return RunEstimate(TODAY + timedelta(days=gone_from), "history", "",
                           lambda on: max(0.0, min(1.0, 1 - ((on - TODAY).days - gone_from) / spread)))

    def test_range_wording(self):
        self.assertEqual(range_text(self.curve(10, 14), TODAY, None), "Probably 2 to 3 more weeks")
        self.assertEqual(range_text(self.curve(0, 4), TODAY, None), "Probably gone within a week")
        self.assertEqual(range_text(RunEstimate(TODAY, "known", ""), TODAY, None), "")

    def test_only_warn_when_sure(self):
        unsure = self.curve(3, 20)   # might go next week, might stay a month
        self.assertGreater(unsure.p_showing(TODAY + timedelta(days=7)), SURE_GONE)  # so no "Leaving soon"
        sure = self.curve(2, 4)
        self.assertLessEqual(sure.p_showing(TODAY + timedelta(days=7)), SURE_GONE)


if __name__ == "__main__":
    unittest.main()
