"""Buzz, early averages, taste-only scoring before release, and the full opening list."""
import json
import random
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import letterboxd  # noqa: E402
from app.db import DB  # noqa: E402
from app.planner import buzz_check, make_plan  # noqa: E402
from app.taste import TasteModel, credibility, evaluate, size_effect  # noqa: E402
from tests.sample_data import CINEMA_META, training_set  # noqa: E402
from tests.test_core import NOW, seeded_db, settings  # noqa: E402

STATS = ('<div class="production-statistic -watches" aria-label="Watched by 776&nbsp;members"></div>'
         '<div aria-label="Appears in 17,515&nbsp;lists"></div><div aria-label="Liked by 277&nbsp;members"></div>')
LD = '<script type="application/ld+json">{"aggregateRating":{"ratingValue":3.78,"ratingCount":1082990}}</script>'


class TestLetterboxdParsing(unittest.TestCase):
    def test_stats(self):
        self.assertEqual(letterboxd.parse_stats(STATS), {"watched": 776, "lists": 17515, "likes": 277})
        self.assertEqual(letterboxd.parse_stats(""), {"watched": None, "lists": None, "likes": None})

    def test_film_slug(self):
        self.assertEqual(letterboxd.film_slug("https://letterboxd.com/film/the-social-reckoning/"), "the-social-reckoning")
        self.assertEqual(letterboxd.film_slug("https://letterboxd.com/tmdb/1/",
                                              '<link rel="canonical" href="https://letterboxd.com/film/digger-2026/">'),
                         "digger-2026")
        self.assertIsNone(letterboxd.film_slug("https://letterboxd.com/tmdb/1/", "<html></html>"))

    def test_rating(self):
        self.assertEqual(letterboxd.parse_rating(LD), (3.78, 1082990))
        self.assertEqual(letterboxd.parse_rating("<html></html>", "3.12 out of 5"), (3.12, None))
        self.assertEqual(letterboxd.parse_rating("<html></html>"), (None, None))


class TestEarlyAverages(unittest.TestCase):
    def test_credibility(self):
        self.assertEqual(credibility(None), 1.0)
        self.assertLess(credibility(20), 0.3)
        self.assertGreater(credibility(100000), 0.99)

    def test_few_ratings_count_for_less(self):
        rows = [(meta, r, r - 0.3) for meta, r in training_set()]
        m = TasteModel().fit(rows)
        film = CINEMA_META["HO2"]
        settled = m.predict(film, 2.0, n=50000).rating
        early = m.predict(film, 2.0, n=15)
        self.assertGreater(early.rating, settled)  # a harsh early average drags it down less
        self.assertIn("from only 15 ratings", early.reasons[0])


class TestNoAverageYet(unittest.TestCase):
    def test_taste_only_fallback(self):
        rows = [(meta, r, r - 0.3) for meta, r in training_set()]
        m = TasteModel().fit(rows)
        self.assertIsNotNone(m.no_comm)
        horror = m.predict(CINEMA_META["HO1"], None)
        self.assertIn("taste alone", horror.reasons[0])
        # scored by the taste-only model, not the full one with the average treated as "average"
        self.assertAlmostEqual(horror.rating, round(m.no_comm.raw(CINEMA_META["HO1"], None)[0], 2))
        self.assertNotAlmostEqual(horror.rating, round(m.raw(CINEMA_META["HO1"], None, fallback=False)[0], 2))

    def test_check_reports_pre_release(self):
        rows = [(meta, r, r - 0.3) for meta, r in training_set()]
        res = evaluate(rows)
        self.assertIn("pre_release", res)
        self.assertLessEqual(res["pre_release"]["new"], res["pre_release"]["old"] + 0.05)


class TestSizeEffect(unittest.TestCase):
    def test_finds_a_real_lean(self):
        rnd = random.Random(1)
        rows = []
        for _ in range(300):
            n = int(10 ** rnd.uniform(2.5, 6))
            crowd = rnd.uniform(2.5, 4.2)
            lean = -0.3 * (len(str(n)) - 4)  # rates big films below the crowd, small ones above
            rows.append((crowd + lean + rnd.gauss(0, 0.3), crowd, n))
        res = size_effect(rows)
        self.assertTrue(res["ok"] and res["lean_is_clear"])
        self.assertLess(res["slope_per_10x"], -0.15)
        self.assertGreater(res["bands"][0]["gap"], res["bands"][2]["gap"])

    def test_no_lean(self):
        rnd = random.Random(2)
        rows = [(c + rnd.gauss(0, 0.4), c, int(10 ** rnd.uniform(2.5, 6))) for c in (rnd.uniform(2.5, 4) for _ in range(300))]
        self.assertFalse(size_effect(rows)["lean_is_clear"])

    def test_not_enough(self):
        self.assertFalse(size_effect([(3.0, 3.0, 500)] * 10)["ok"])


class TestPlanBuzz(unittest.TestCase):
    def test_opening_uncapped_and_buzz(self):
        db = seeded_db()
        for i, (fid, m) in enumerate(CINEMA_META.items()):
            db.x("INSERT OR REPLACE INTO community(tmdb_id,lb_avg,fetched_at,rating_count,watched,lists,likes) VALUES(?,?,?,?,?,?,?)",
                 (m["id"], None if i % 3 == 0 else 3.4, NOW.isoformat(), None if i % 3 == 0 else 5000, 100 * i, 1000 * (i + 1), 10 * i))
        p = make_plan(db, settings(), NOW)
        opening = [o for m in p["months"] for o in m["opening"]]
        dates = [o["first_date"] for o in p["months"][0]["opening"]]
        self.assertEqual(dates, sorted(dates))  # date order
        films = list(p["films"].values())
        self.assertTrue(all(f["buzz"] for f in films))
        most = max(films, key=lambda f: f["buzz"].get("lists") or 0)
        least = min(films, key=lambda f: f["buzz"].get("lists") or 10 ** 9)
        self.assertGreater(most["buzz"]["score"], least["buzz"]["score"])
        for m in p["months"]:
            ids = [x["film_id"] for k in ("picks", "worth_paying", "at_home", "everything") for x in m[k]]
            self.assertEqual(len(ids), len(set(ids)))  # each film shown once
        self.assertTrue(opening is not None)

    def test_buzz_check_and_log(self):
        db = seeded_db()
        res = buzz_check(db)
        self.assertFalse(res["ok"])
        self.assertEqual(res["logged"], 0)
        self.assertGreater(res["waiting"], 0)


class ShortRuns:
    """A stand-in run model: every film is gone about 9 days after its reference date."""
    ready = True
    n_films = 1

    def estimate(self, today, opened, week_in, share, ratio):
        return [0], (opened or today)

    def p_still_showing(self, idx, days):
        return 1.0 if days < 9 else 0.1

    def median_remaining(self, idx):
        return 9


class TestShortRuns(unittest.TestCase):
    def test_short_runs_flagged(self):
        db = seeded_db()
        p = make_plan(db, settings(), NOW, run_model=ShortRuns())
        flagged = [f for f in p["films"].values() if f["short_run"]]
        self.assertTrue(flagged)
        self.assertTrue(all(f["p_two_weeks"] < 0.25 for f in flagged))  # only the unusually short


class TestMigration(unittest.TestCase):
    def test_adds_columns_to_old_database(self):
        path = Path(tempfile.mkdtemp()) / "old.sqlite3"
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE community (tmdb_id INTEGER PRIMARY KEY, lb_avg REAL, fetched_at TEXT)")
        con.execute("INSERT INTO community VALUES (1, 3.5, 'x')")
        con.commit()
        con.close()
        db = DB(path)
        row = db.one("SELECT * FROM community WHERE tmdb_id=1")
        self.assertEqual(row["lb_avg"], 3.5)
        self.assertIsNone(row["lists"])
        db.x("INSERT INTO buzz_log(tmdb_id,day,lists) VALUES(1,'2026-10-01',5)")
        DB(path)  # opening again is harmless


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestBigScreen(unittest.TestCase):
    def test_scores(self):
        from app.planner import big_screen
        self.assertEqual(big_screen("event", [], set(), 90)[0], 1.0)
        epic = big_screen("film", ["Science Fiction", "Action"], {"epic"}, 150)
        drama = big_screen("film", ["Drama", "Romance"], set(), 100)
        self.assertGreater(epic[0], 0.8)
        self.assertIn("EPIC", epic[1])
        self.assertLess(drama[0], 0.4)
        self.assertEqual(drama[1], "Fine at home")

    def test_layout(self):
        db = seeded_db()
        m0 = make_plan(db, settings(), NOW)["months"][0]
        self.assertLessEqual(len(m0["worth_paying"]), 5)
        self.assertTrue(all(x["kind"] != "film" or x["big_screen"] < 0.4 for x in m0["at_home"]))
        self.assertTrue(all(x["home_from"] for x in m0["at_home"]))

    def test_big_screen_weight_moves_priority(self):
        db = seeded_db()
        flat = make_plan(db, settings(big_screen_weight=0.0), NOW)["films"]
        big = make_plan(db, settings(big_screen_weight=1.0), NOW)["films"]
        for fid, f in big.items():
            if f["kind"] == "film" and f["big_screen"] > 0.5:
                self.assertGreater(f["priority"], flat[fid]["priority"])
            else:  # quiet films, events and re-releases are never marked down or lifted
                self.assertAlmostEqual(f["priority"], flat[fid]["priority"])

    def test_rereleases_and_events_dont_take_tickets(self):
        db = seeded_db()
        p = make_plan(db, settings(), NOW)
        for m in p["months"]:
            main = [x for k in ("picks", "worth_paying", "at_home", "everything") for x in m[k]]
            self.assertFalse([x for x in main if x["extra"]])
            self.assertTrue(all(x["kind"] != "film" for x in m["extras"]))
        # a pinned event still takes its ticket
        db.x("INSERT INTO pins VALUES('HO5','2026-10','x')")
        p = make_plan(db, settings(), NOW)
        self.assertIn("HO5", [x["film_id"] for x in p["months"][0]["picks"]])


class TestWantToSee(unittest.TestCase):
    def want(self, db, *ids):
        for fid in ids:
            db.x("INSERT OR REPLACE INTO wants(film_id,title,created_at) VALUES(?,?,?)", (fid, fid, "x"))

    def placed(self, p):
        free = {x["film_id"]: m["month"] for m in p["months"] for x in m["picks"] if x.get("wanted")}
        paid = {x["film_id"]: m["month"] for m in p["months"] for x in m["paid_trips"]}
        return free, paid

    def test_every_wanted_film_placed_once(self):
        db = seeded_db()
        ids = ["HO1", "HO2", "HO3", "HO4", "HO8", "HO9"]
        self.want(db, *ids)
        s = settings(tickets_per_month=1, ticket_source="custom")
        p = make_plan(db, s, NOW)
        free, paid = self.placed(p)
        self.assertFalse(set(free) & set(paid))
        for fid in ids:
            self.assertTrue(fid in free or fid in paid or p["films"][fid]["title"] in p["wants"]["later"], fid)
        for m in p["months"]:
            self.assertLessEqual(len([x for x in m["picks"] if x.get("wanted")]), 1)  # one free ticket a month
        self.assertEqual(p["wants"]["free"], len(free))
        self.assertEqual(len(p["wants"]["paid"]), len(paid))
        self.assertGreater(len(paid), 0)  # six wanted films can't fit in three free tickets

    def test_enough_tickets_means_no_paid_trips(self):
        db = seeded_db()
        self.want(db, "HO1", "HO8")
        p = make_plan(db, settings(), NOW)  # two free tickets a month
        free, paid = self.placed(p)
        self.assertEqual(paid, {})
        self.assertEqual(set(free), {"HO1", "HO8"})

    def test_wanted_beats_recommendations(self):
        db = seeded_db()
        base = make_plan(db, settings(), NOW)
        top = {x["film_id"] for x in base["months"][0]["picks"]}
        other = next(f for f in ["HO2", "HO4", "HO11"] if f not in top and f in base["films"])
        self.want(db, other)
        p = make_plan(db, settings(), NOW)
        free, _ = self.placed(p)
        self.assertIn(other, free)

    def test_wanted_event_isnt_an_extra(self):
        db = seeded_db()
        self.want(db, "HO5")  # a re-release
        p = make_plan(db, settings(), NOW)
        self.assertFalse(p["films"]["HO5"]["extra"])
        free, paid = self.placed(p)
        self.assertTrue("HO5" in free or "HO5" in paid)
