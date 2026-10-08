"""Cinema trips from Vue tickets: parsed, each film once, tickets counted only where they should be."""
import asyncio
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import trips  # noqa: E402
from app.db import DB  # noqa: E402

TEXT = """date,day,film,note
2026-10-06,Tue,Sense and Sensibility,
2026-09-24,Thu,Heart of the Beast,
2026-09-14,Mon,The Death of Robin Hood,
2026-09-06,Sun,Coyote vs. Acme,
2026-09-05,Sat,The Dog Stars,
2025-10-17,Fri,Baby Driver (2017),re-release; listed twice
2025-10-17,Fri,Baby Driver (2017),re-release; listed twice
2023-07-21,Fri,Barbie (2023),seen twice
2023-09-19,Tue,Barbie (2023),seen twice
,,no date here,
"""
FREE = """Heart of the Beast, Thu 24 Sep 2026
2026-09-14 | The Death of Robin Hood
Good Luck, Have Fun, Don't Die, Fri 20 Feb 2026
SENSE AND SENSIBILITY
Tue 6 Oct 2026
"""


class FakeTMDB:
    IDS = {"heart of the beast": 2, "the death of robin hood": 3, "coyote vs acme": 4, "the dog stars": 5,
           "baby driver": 339403, "barbie": 346698}

    async def match_title(self, title, year):
        from app.tmdb import norm
        tid = self.IDS.get(norm(title))
        return (tid, "film") if tid else (None, "none")


class TestTrips(unittest.TestCase):
    def test_parse(self):
        rows, bad = trips.parse_lines(TEXT)
        self.assertEqual(len(rows), 9)
        self.assertIn(("Coyote vs. Acme", date(2026, 9, 6)), rows)
        self.assertEqual(len(bad), 1)
        rows, bad = trips.parse_lines(FREE)
        self.assertEqual(rows[:3], [("Heart of the Beast", date(2026, 9, 24)), ("The Death of Robin Hood", date(2026, 9, 14)),
                                    ("Good Luck, Have Fun, Don't Die", date(2026, 2, 20))])
        self.assertEqual(len(bad), 2)  # a title and a date on separate lines aren't guessed at

    def test_rerelease(self):
        self.assertTrue(trips.is_rerelease("Baby Driver (2017)", date(2025, 10, 17)))
        self.assertFalse(trips.is_rerelease("Barbie (2023)", date(2023, 7, 21)))

    def test_import(self):
        db = DB(":memory:")
        db.x("""INSERT INTO vue_films(film_id,title,release_date,status,kind,tmdb_id,first_seen,last_seen)
                VALUES('HO1','Sense and Sensibility','2026-10-02',1,'film',1,'2026-10-01','2026-10-08')""")
        db.x("INSERT INTO ticket_uses(month,film_id,tmdb_id,title,used_on,source,active) "
             "VALUES('2026-10','HO1',1,'Sense and Sensibility','2026-10-02','dashboard',1)")
        rows, _ = trips.parse_lines(TEXT)
        r = asyncio.run(trips.import_trips(db, FakeTMDB(), rows, "Cribbs", 2, "2026-09"))
        self.assertEqual(r["added"], 7)       # Baby Driver and Barbie once each
        self.assertEqual(r["already"], 2)
        # September: two tickets, the first two trips; October: Sense and Sensibility was already counted
        self.assertEqual(r["tickets"], ["The Dog Stars (5 Sep)", "Coyote vs. Acme (6 Sep)"])
        self.assertEqual(db.one("SELECT visited_on FROM cinema_trips WHERE tmdb_id=346698")["visited_on"], "2023-07-21")
        self.assertEqual(db.one("SELECT kind FROM cinema_trips WHERE tmdb_id=339403")["kind"], "rerelease")
        self.assertEqual(db.one("SELECT film_id FROM cinema_trips WHERE tmdb_id=1")["film_id"], "HO1")
        self.assertIsNotNone(db.one("SELECT 1 FROM watched WHERE tmdb_id=5"))
        self.assertEqual(db.one("SELECT COUNT(*) n FROM ticket_uses WHERE month='2023-07'")["n"], 0)  # history only
        again = asyncio.run(trips.import_trips(db, FakeTMDB(), rows, "Cribbs", 2, "2026-09"))
        self.assertEqual((again["added"], again["tickets"]), (0, []))
        self.assertEqual(trips.summary(db)["count"], 7)


if __name__ == "__main__":
    unittest.main()


def _cinema(db):
    db.set("vue_last_listing", "2026-10-08T06:00:00")
    db.x("""INSERT INTO vue_films(film_id,title,release_date,status,kind,tmdb_id,first_seen,last_seen)
            VALUES('HO7','The Social Reckoning','2026-10-09',1,'film',77,'2026-10-01','2026-10-08')""")
    for sid, start, fmts in (("S1", "2026-10-09T19:45:00", '["epic"]'), ("S2", "2026-10-10T17:00:00", "[]"),
                             ("S0", "2026-10-06T19:00:00", "[]")):
        db.x("""INSERT INTO sessions(session_id,film_id,start,screen,formats,sold_out,booking_url,first_seen,last_seen)
                VALUES(?,?,?,?,?,0,'',?,?)""", (sid, "HO7", start, "Screen 1", fmts, "2026-10-01T06:00:00",
                                                "2026-10-06T06:00:00" if sid == "S0" else "2026-10-08T06:00:00"))


class TestShowings(unittest.TestCase):
    def test_use_then_pick(self):
        from app import tickets
        db = DB(":memory:")
        _cinema(db)
        today = date(2026, 10, 8)
        use = tickets.add_use(db, "HO7", 77, "The Social Reckoning", today, "dashboard")
        self.assertEqual(db.one("SELECT source FROM cinema_trips WHERE key='tmdb:77'")["source"], "ticket")
        opts = trips.showings(db, "HO7", today)
        self.assertEqual([o["session_id"] for o in opts], ["S0", "S1", "S2"])  # a past one counts: marked afterwards
        self.assertEqual(opts[1]["label"], "Fri 9 Oct, 19:45 · EPIC")
        self.assertEqual([n["film_id"] for n in trips.need_showing(db, today)], ["HO7"])
        got = trips.set_showing(db, "S1")
        self.assertEqual(got["label"], "Fri 9 Oct, 19:45")
        t = db.one("SELECT * FROM cinema_trips WHERE key='tmdb:77'")
        self.assertEqual((t["visited_on"], t["formats"], t["screen"]), ("2026-10-09", '["epic"]', "Screen 1"))
        self.assertEqual(trips.need_showing(db, today), [])
        tickets.undo_use(db, use)  # giving the ticket back drops the trip it made
        self.assertIsNone(db.one("SELECT 1 FROM cinema_trips WHERE key='tmdb:77'"))

    def test_no_times_yet(self):
        from app import tickets
        db = DB(":memory:")
        _cinema(db)
        db.x("DELETE FROM sessions")
        tickets.add_use(db, "HO7", 77, "The Social Reckoning", date(2026, 10, 8), "dashboard")
        self.assertEqual(trips.need_showing(db, date(2026, 10, 8)), [])  # nothing to pick yet: asked later

    def test_diary_backstop(self):
        db = DB(":memory:")
        _cinema(db)
        db.x("INSERT INTO diary(entry_key,tmdb_id,title,year,watched_date,rating,rewatch,source) "
             "VALUES('d1',77,'The Social Reckoning',2026,'2026-10-10',4.0,0,'rss')")
        db.x("INSERT INTO diary(entry_key,tmdb_id,title,year,watched_date,rating,rewatch,source) "
             "VALUES('d2',99,'Something at home',2020,'2026-10-10',3.0,0,'rss')")
        self.assertEqual(trips.from_diary(db, date(2026, 9, 1), "Cribbs"), ["The Social Reckoning"])
        self.assertEqual(trips.from_diary(db, date(2026, 9, 1), "Cribbs"), [])  # once
