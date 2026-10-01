import asyncio
import csv
import io
import json
import sys
import tempfile
import unittest
import zipfile
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import letterboxd, tickets, vue  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db import DB  # noqa: E402
from app.planner import estimate_run, make_plan  # noqa: E402
from app.taste import TasteModel  # noqa: E402
from app.tmdb import TMDB, clean_vue_title  # noqa: E402
from tests.sample_data import CINEMA_META, training_set, vue_responses  # noqa: E402

TZ = ZoneInfo("Europe/London")
NOW = datetime(2026, 10, 1, 10, 0, tzinfo=TZ)
FIX = Path(__file__).parent / "fixtures"


def settings(**kw):
    s = Settings()
    s.data_dir = Path(tempfile.mkdtemp())
    s.timezone = "Europe/London"
    s.tickets_per_month = 2
    s.urgency_weight = 0.5
    s.ticket_excluded_formats = []
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def seeded_db(with_training=True) -> DB:
    db = DB(":memory:")
    films = vue.parse(*vue_responses())
    vue.store(db, films, NOW)
    for fid, m in CINEMA_META.items():
        db.x("UPDATE vue_films SET tmdb_id=? WHERE film_id=?", (m["id"], fid))
        db.x("INSERT OR REPLACE INTO movies(tmdb_id,data,fetched_at) VALUES(?,?,?)", (m["id"], json.dumps(m), NOW.isoformat()))
    if with_training:
        for m, r in training_set():
            db.x("INSERT OR REPLACE INTO movies(tmdb_id,data,fetched_at) VALUES(?,?,?)", (m["id"], json.dumps(m), NOW.isoformat()))
            db.x("INSERT INTO ratings(tmdb_id,rating,rated_on,source) VALUES(?,?,?,?)", (m["id"], r, "2025-01-01", "export"))
    return db


class TestVue(unittest.TestCase):
    def test_parse_and_classify(self):
        films = {f.film_id: f for f in vue.parse(*vue_responses())}
        self.assertEqual(len(films), 11)
        self.assertEqual(films["HO1"].kind, "film")
        self.assertEqual(films["HO5"].kind, "rerelease")
        self.assertEqual(films["HO6"].kind, "event")
        self.assertEqual(films["HO10"].kind, "event")
        self.assertEqual(films["HO7"].status, 2)
        self.assertEqual(films["HO8"].sessions, [])  # coming soon, no showtimes yet
        self.assertIn("epic", films["HO7"].sessions[0].formats)
        self.assertEqual(films["HO1"].sessions[0].start.tzinfo is not None, True)

    def test_store_snapshots(self):
        db = seeded_db(False)
        snap = db.one("SELECT * FROM snapshots WHERE film_id='HO1'")
        self.assertEqual(snap["sessions_next7"], 4 * 7 - 0)  # Oct 1..7 (8th is outside the 7-day window) minus past
        self.assertEqual(db.one("SELECT COUNT(*) n FROM sessions")["n"] > 50, True)


class TestTitles(unittest.TestCase):
    def test_clean(self):
        self.assertEqual(clean_vue_title("Casino Royale (20th Anniversary)"), ("Casino Royale", None))
        self.assertEqual(clean_vue_title("Coraline (2009)"), ("Coraline", 2009))
        self.assertEqual(clean_vue_title("Drishyam: The Conclusion (Hindi)"), ("Drishyam: The Conclusion", None))
        self.assertEqual(clean_vue_title("Avengers: Endgame Encore")[0], "Avengers: Endgame")

    def test_best(self):
        res = [{"id": 1, "title": "Resident Evil", "release_date": "2002-03-15", "vote_count": 5000},
               {"id": 2, "title": "Resident Evil", "release_date": "2026-09-18", "vote_count": 10}]
        self.assertEqual(TMDB._best("Resident Evil", 2026, res)["id"], 2)
        self.assertIsNone(TMDB._best("Met Opera Macbeth", None, [{"id": 3, "title": "Totally Different", "release_date": ""}]))


class TestLetterboxd(unittest.TestCase):
    def test_rss(self):
        entries = letterboxd.parse_rss((FIX / "rss.xml").read_text())
        self.assertEqual(len(entries), 2)  # the list item is skipped
        self.assertEqual(entries[0]["tmdb_id"], 1032863)
        self.assertEqual(entries[0]["rating"], 4.0)
        self.assertIsNone(entries[1]["rating"])
        db = DB(":memory:")
        db.x("INSERT INTO watchlist(tmdb_id,title) VALUES(1032863,'x')")
        self.assertEqual(letterboxd.store_rss(db, entries), 2)
        self.assertEqual(letterboxd.store_rss(db, entries), 0)  # idempotent
        self.assertEqual(db.one("SELECT rating FROM ratings WHERE tmdb_id=1032863")["rating"], 4.0)
        self.assertIsNone(db.one("SELECT 1 FROM watchlist"))  # logged -> off watchlist

    def test_export(self):
        class FakeTMDB:
            async def match_title(self, name, year):
                return {"Heat": 949, "Alien": 348, "Nope": 762504}.get(name)

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            def w(name, header, rows):
                s = io.StringIO()
                wr = csv.writer(s)
                wr.writerow(header)
                wr.writerows(rows)
                z.writestr(name, s.getvalue())
            w("ratings.csv", ["Date", "Name", "Year", "Letterboxd URI", "Rating"],
              [["2024-01-01", "Heat", "1995", "u", "4.5"], ["2024-02-01", "Alien", "1979", "u", "5"],
               ["2024-02-01", "Unknown Film", "1990", "u", "3"]])
            w("diary.csv", ["Date", "Name", "Year", "Letterboxd URI", "Rating", "Rewatch", "Tags", "Watched Date"],
              [["2024-01-01", "Heat", "1995", "u", "4.5", "", "", "2023-12-31"]])
            w("watchlist.csv", ["Date", "Name", "Year", "Letterboxd URI"], [["2024-01-01", "Nope", "2022", "u"]])
            w("watched.csv", ["Date", "Name", "Year", "Letterboxd URI"], [["2024-01-01", "Heat", "1995", "u"]])
        p = Path(tempfile.mkdtemp()) / "letterboxd-export.zip"
        p.write_bytes(buf.getvalue())
        db = DB(":memory:")
        r = asyncio.run(letterboxd.import_export(db, FakeTMDB(), p))
        self.assertEqual(r["unmatched_count"], 1)
        self.assertEqual({x["tmdb_id"] for x in db.q("SELECT tmdb_id FROM ratings")}, {949, 348})
        self.assertEqual(db.one("SELECT tmdb_id FROM watchlist")["tmdb_id"], 762504)
        self.assertEqual(db.one("SELECT watched_date FROM diary")["watched_date"], "2023-12-31")


class TestTaste(unittest.TestCase):
    def test_learns_preferences(self):
        m = TasteModel().fit([(meta, r, None) for meta, r in training_set()])
        self.assertLess(m.mae, m.mae_baseline)
        horror = m.predict(CINEMA_META["HO1"], None)
        hero = m.predict(CINEMA_META["HO7"], None)
        musical = m.predict(CINEMA_META["HO6"], None)
        self.assertGreater(horror.rating, hero.rating)
        self.assertGreater(hero.rating, musical.rating)
        self.assertTrue(any("Zach Cregger" in r for r in horror.reasons), horror.reasons)

    def test_community_helps(self):
        rows = [(meta, r, r - 0.3) for meta, r in training_set()]
        m = TasteModel().fit(rows)
        hi = m.predict(CINEMA_META["HO2"], 4.2).rating
        lo = m.predict(CINEMA_META["HO2"], 2.4).rating
        self.assertGreater(hi, lo)

    def test_small_history(self):
        m = TasteModel().fit([(meta, r, None) for meta, r in training_set()[:5]])
        self.assertEqual(m.predict(CINEMA_META["HO1"], None).confidence, "low")


class TestRun(unittest.TestCase):
    today = date(2026, 10, 1)

    def test_event_known(self):
        r = estimate_run("event", "Trafalgar", date(2026, 10, 13), [date(2026, 10, 13), date(2026, 10, 15)], [], self.today, date(2026, 10, 8))
        self.assertEqual((r.end, r.basis), (date(2026, 10, 15), "known"))
        self.assertLess(r.p_showing(date(2026, 11, 7)), 0.01)

    def test_fading_indie(self):
        r = estimate_run("film", "Picturehouse", date(2026, 9, 2), [date(2026, 10, 2), date(2026, 10, 3), date(2026, 10, 6)],
                         [20, 12, 3], self.today, date(2026, 10, 8))
        self.assertEqual(r.basis, "listing")  # not listed through to the end of Vue's published week
        self.assertLess(r.p_showing(date(2026, 11, 7)), 0.05)

    def test_new_blockbuster(self):
        days = [date(2026, 10, d) for d in range(2, 9)]
        r = estimate_run("film", "Warner Bros.", date(2026, 10, 2), days, [40], self.today, date(2026, 10, 8))
        self.assertGreater(r.p_showing(date(2026, 11, 7)), 0.5)

    def test_coming_soon(self):
        r = estimate_run("film", "Sony", date(2026, 11, 13), [], [], self.today, date(2026, 10, 8))
        self.assertEqual(r.end, date(2026, 12, 25))


class TestPlan(unittest.TestCase):
    def test_plan(self):
        s = settings()
        db = seeded_db()
        p = make_plan(db, s, NOW)
        titles = [x["title"] for x in p["picks"]]
        self.assertEqual(p["tickets_left"], 2)
        self.assertEqual(len(titles), 2)
        self.assertIn("Resident Evil", titles)  # loved director + genre, leaving within weeks
        self.assertNotIn("Avengers: Doomsday", [x["title"] for x in p["picks"] + p["also_good"]])  # December
        nov = p["months"][1]
        self.assertIn("Ebenezer", [x["title"] for x in nov["picks"] + nov["opening"]])
        self.assertIn("Met Opera 2026-27: Macbeth", [u["title"] for u in p["unscored"]])
        for x in p["picks"]:
            self.assertTrue(x["watch_by"] <= "2026-10-31")
            self.assertTrue(x["sessions"])
        # picks are ordered by deadline
        self.assertEqual([x["watch_by"] for x in p["picks"]], sorted(x["watch_by"] for x in p["picks"]))
        json.dumps(p)  # serialisable

    def test_urgency_changes_ranking(self):
        db = seeded_db()
        calm = make_plan(db, settings(urgency_weight=0.0), NOW)
        urgent = make_plan(db, settings(urgency_weight=2.0), NOW)
        order = lambda p: [x["title"] for x in p["picks"] + p["leaving_soon"] + p["also_good"]]
        # with high urgency, the fading Leviticus (horror, gone within the week) should rank higher
        self.assertLessEqual(order(urgent).index("Leviticus"), order(calm).index("Leviticus"))

    def test_excluded_formats(self):
        db = seeded_db()
        p = make_plan(db, settings(ticket_excluded_formats=["ultra-lux-and-lux"]), NOW)
        # every fixture session is in a Lux screen, so only films with no showtimes yet can be suggested
        self.assertTrue(all(not x["sessions"] for x in p["picks"] + p["also_good"]))
        self.assertNotIn("Resident Evil", [x["title"] for x in p["picks"] + p["also_good"]])

    def test_tickets(self):
        s = settings()
        db = seeded_db()
        letterboxd.store_rss(db, letterboxd.parse_rss((FIX / "rss.xml").read_text()))
        added = tickets.auto_detect(db, date(2026, 10, 3), 2)
        self.assertEqual([a["title"] for a in added], ["Resident Evil"])  # logged 2 Oct, showing that day
        self.assertEqual(tickets.auto_detect(db, date(2026, 10, 3), 2), [])  # not double counted
        p = make_plan(db, s, NOW)
        self.assertEqual(p["tickets_left"], 1)
        self.assertNotIn("Resident Evil", [x["title"] for x in p["picks"]])
        undone = tickets.undo_latest(db, "2026-10")
        self.assertEqual(undone["title"], "Resident Evil")
        self.assertEqual(tickets.auto_detect(db, date(2026, 10, 3), 2), [])  # respects your undo
        self.assertEqual(make_plan(db, s, NOW)["tickets_left"], 2)



class TestMonths(unittest.TestCase):
    def test_three_months(self):
        db = seeded_db()
        p = make_plan(db, settings(), NOW)
        self.assertEqual([m["month"] for m in p["months"]], ["2026-10", "2026-11", "2026-12"])
        seen = [x["film_id"] for m in p["months"] for x in m["picks"]]
        self.assertEqual(len(seen), len(set(seen)))  # nothing picked twice
        dec = p["months"][2]
        self.assertIn("Avengers: Doomsday", [x["title"] for x in dec["picks"] + dec["also_good"]])
        self.assertTrue(all(x["provisional"] for x in p["months"][1]["picks"]))
        self.assertEqual(p["picks"], p["months"][0]["picks"])  # the top level is still this month

    def test_pin_and_dismiss(self):
        db = seeded_db()
        db.x("INSERT INTO pins VALUES('HO9','2026-11','x')")
        db.x("INSERT INTO dismissed VALUES('HO1',9001,'Resident Evil','x')")
        p = make_plan(db, settings(), NOW)
        nov = p["months"][1]["picks"]
        self.assertTrue(any(x["film_id"] == "HO9" and x["pinned"] for x in nov))
        everything = [x["film_id"] for m in p["months"] for k in ("picks", "also_good", "opening") for x in m[k]]
        self.assertNotIn("HO1", everything)
        self.assertEqual([d["title"] for d in p["dismissed"]], ["Resident Evil"])

    def test_expiry_warning(self):
        db = seeded_db()
        late = datetime(2026, 10, 27, 10, 0, tzinfo=TZ)
        self.assertIsNotNone(make_plan(db, settings(), late)["warning"])
        self.assertIsNone(make_plan(db, settings(), NOW)["warning"])

if __name__ == "__main__":
    unittest.main(verbosity=2)
