"""Pinned and wanted films that stop (or start) holding a ticket: quiet, once, and undone if the film comes back."""
import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import watch  # noqa: E402
from app.db import DB  # noqa: E402
from app.planner import times_due, times_overdue  # noqa: E402

CINEMA = "Vue Bristol Cribbs Causeway"


def setup(release="2026-10-02"):
    db = DB(":memory:")
    db.x("""INSERT INTO vue_films(film_id,title,release_date,status,kind,first_seen,last_seen)
            VALUES('F1','Extra Geography',?,2,'film','2026-09-20','2026-10-01')""", (release,))
    db.x("INSERT INTO pins(film_id,month,created_at) VALUES('F1','2026-10','2026-09-25')")
    return db


def listing(db, day, films=("F1",), times=False):
    stamp = f"{day}T06:00:00"
    db.set("vue_last_listing", stamp)
    db.set("vue_listed_film_ids", list(films))
    if times:
        db.x("""INSERT OR REPLACE INTO sessions(session_id,film_id,start,screen,formats,sold_out,booking_url,first_seen,last_seen)
                VALUES('S1','F1',?,'1','[]',0,'',?,?)""", (f"{day}T19:00:00", stamp, stamp))
    return datetime.fromisoformat(stamp)


class TestTimesDue(unittest.TestCase):
    def test_tuesday_before(self):
        self.assertEqual(times_due(date(2026, 10, 2)), date(2026, 9, 29))   # Friday -> Tuesday
        self.assertEqual(times_due(date(2026, 10, 6)), date(2026, 9, 29))   # Tuesday -> the Tuesday before
        self.assertFalse(times_overdue(date(2026, 10, 2), False, date(2026, 9, 29)))  # due day itself is fine
        self.assertTrue(times_overdue(date(2026, 10, 2), False, date(2026, 9, 30)))
        self.assertFalse(times_overdue(date(2026, 10, 2), True, date(2026, 10, 1)))
        self.assertFalse(times_overdue(None, False, date(2026, 10, 1)))


class TestWatch(unittest.TestCase):
    def test_gone_needs_two_misses_once_then_back(self):
        db = setup()
        self.assertEqual(watch.check(db, CINEMA, listing(db, "2026-10-02", films=())), [])  # a blip: nothing
        msgs = watch.check(db, CINEMA, listing(db, "2026-10-03", films=()))
        self.assertEqual(len(msgs), 1)
        self.assertIn("no longer listed", msgs[0])
        self.assertIn("October pin", msgs[0])
        self.assertEqual(watch.check(db, CINEMA, listing(db, "2026-10-04", films=())), [])  # never repeated
        self.assertEqual(len(watch.current_notices(db, date(2026, 10, 4))), 1)
        back = watch.check(db, CINEMA, listing(db, "2026-10-05", times=True))
        self.assertEqual(len(back), 1)
        self.assertIn("back at", back[0])
        notes = watch.current_notices(db, date(2026, 10, 5))
        self.assertEqual([n["kind"] for n in notes], ["back"])
        self.assertIsNotNone(db.one("SELECT 1 FROM pins WHERE film_id='F1'"))  # the pin was kept throughout

    def test_listed_but_no_times_after_tuesday(self):
        db = setup()
        self.assertEqual(watch.check(db, CINEMA, listing(db, "2026-09-29")), [])  # times due today: fine
        self.assertEqual(watch.check(db, CINEMA, listing(db, "2026-09-30")), [])  # first miss
        msgs = watch.check(db, CINEMA, listing(db, "2026-10-01"))
        self.assertEqual(len(msgs), 1)
        self.assertIn("no showtimes", msgs[0])

    def test_blip_then_fine_sends_nothing(self):
        db = setup()
        watch.check(db, CINEMA, listing(db, "2026-10-02", films=()))
        self.assertEqual(watch.check(db, CINEMA, listing(db, "2026-10-03", times=True)), [])
        self.assertEqual(watch.check(db, CINEMA, listing(db, "2026-10-04", films=())), [])  # streak restarted

    def test_used_or_unpinned_films_are_ignored(self):
        db = setup()
        db.x("INSERT INTO ticket_uses(month,film_id,title,used_on,source,active) VALUES('2026-10','F1','x','2026-10-02','you',1)")
        for d in ("2026-10-03", "2026-10-04", "2026-10-05"):
            self.assertEqual(watch.check(db, CINEMA, listing(db, d, films=())), [])
        db2 = setup()
        watch.check(db2, CINEMA, listing(db2, "2026-10-02", films=()))
        db2.x("DELETE FROM pins")
        self.assertEqual(watch.check(db2, CINEMA, listing(db2, "2026-10-03", films=())), [])
        self.assertEqual(db2.get("watch_misses"), {})

    def test_wanted_film(self):
        db = setup()
        db.x("DELETE FROM pins")
        db.x("INSERT INTO wants(film_id,tmdb_id,title,created_at) VALUES('F1',1,'Extra Geography','2026-09-25')")
        watch.check(db, CINEMA, listing(db, "2026-10-02", films=()))
        msgs = watch.check(db, CINEMA, listing(db, "2026-10-03", films=()))
        self.assertIn("Want to see", msgs[0])


class TestPlanUnconfirmed(unittest.TestCase):
    def test_pin_without_times(self):
        from app.planner import make_plan
        from tests.test_core import NOW, seeded_db, settings
        db = seeded_db()
        db.x("INSERT INTO pins(film_id,month,created_at) VALUES('HO8','2026-10','2026-09-25')")
        p = make_plan(db, settings(), NOW)  # Clayface opens 23 Oct: times not due yet, so it holds a ticket
        pick = next(i for i in p["months"][0]["picks"] if i["film_id"] == "HO8")
        self.assertTrue(pick["unconfirmed"])
        self.assertFalse(pick["times_overdue"])
        db.x("UPDATE vue_films SET release_date='2026-10-02' WHERE film_id='HO8'")  # times were due Tue 29 Sep
        m = make_plan(db, settings(), NOW)["months"][0]
        self.assertNotIn("HO8", [i["film_id"] for i in m["picks"]])
        late = next(i for i in m["everything"] if i["film_id"] == "HO8")
        self.assertTrue(late["times_overdue"])
        self.assertFalse(late["pinned"])


if __name__ == "__main__":
    unittest.main()
