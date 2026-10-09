"""Every page renders, and the Settings page saves and takes effect."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from starlette.testclient import TestClient  # noqa: E402

import app.planner as P  # noqa: E402
from app.jobs import Engine  # noqa: E402
from app.web import create_app  # noqa: E402
from tests.test_core import NOW, seeded_db, settings  # noqa: E402

FORM = {"ticket_source": "custom", "tickets_per_month": "3", "ticket_expiry": "month",
        "vue_cinema_slug": "bristol-cribbs-causeway", "urgency_weight": "70", "favourite_weight": "50",
        "watchlist_boost": "25", "include_events": "on", "include_rereleases": "on", "include_seen": "on",
        "letterboxd_user": "someone", "letterboxd_community": "on", "monthly_day": "1", "monthly_hour": "9",
        "refresh_hour": "6"}


def client():
    s = settings()
    db = seeded_db()
    db.set("vue_cinemas", [{"id": "10018", "name": "Bristol Cribbs Causeway", "slug": "bristol-cribbs-causeway"},
                           {"id": "10019", "name": "Bristol Longwell Green", "slug": "bristol-longwell-green"}])
    eng = Engine(s, db)
    eng.replan = lambda: (db.set("plan", P.make_plan(db, s, NOW)) or db.get("plan"))  # fixed clock
    eng.plan = lambda: db.get("plan") or eng.replan()
    eng.refresh = lambda *a, **k: _noop()
    eng.replan()
    return TestClient(create_app(eng)), s, db


async def _noop():
    return {}


class TestPages(unittest.TestCase):
    def test_pages(self):
        c, _, _ = client()
        for url in ["/", "/?m=1", "/?m=2", "/films", "/films?sort=leaving&show=now", "/films?show=soon&sort=opening",
                    "/film/HO1", "/taste", "/settings", "/health", "/api/plan"]:
            self.assertEqual(c.get(url).status_code, 200, url)
        self.assertEqual(c.get("/film/NOPE").status_code, 404)
        home = c.get("/").text
        self.assertIn("Last Showing", home)
        self.assertIn("/static/favicon.svg", home)
        self.assertIn('href="/?m=1"', home)  # month switcher
        self.assertEqual(home.count('class="stub"'), 2)

    def test_settings_change_tickets(self):
        c, s, db = client()
        r = c.post("/settings", data=FORM, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual((s.tickets, s.urgency_weight, s.letterboxd_user), (3, 0.7, "someone"))
        self.assertEqual(db.get("settings")["tickets_per_month"], 3)
        self.assertEqual(c.get("/").text.count('class="stub"'), 3)
        c.post("/settings", data={**FORM, "ticket_source": "none"})
        home = c.get("/").text
        self.assertNotIn('class="stub', home)
        self.assertIn("Worth seeing", home)

    def test_switch_cinema(self):
        c, s, db = client()
        r = c.post("/settings", data={**FORM, "vue_cinema_slug": "bristol-longwell-green"}, follow_redirects=False)
        self.assertIn("Longwell", r.headers["location"])
        self.assertEqual((s.vue_cinema_id, s.vue_cinema_name), ("10019", "Bristol Longwell Green"))
        self.assertEqual(db.one("SELECT COUNT(*) n FROM sessions")["n"], 0)

    def test_actions_return_to_page(self):
        c, _, db = client()
        r = c.post("/pin", data={"film_id": "HO9", "month": "2026-11", "next": "/?m=1"}, follow_redirects=False)
        self.assertTrue(r.headers["location"].startswith("/?m=1&msg="))
        r = c.post("/dismiss", data={"film_id": "HO1", "next": "/film/HO1"}, follow_redirects=False)
        self.assertTrue(r.headers["location"].startswith("/films?msg="))  # its page is gone, so back to Films
        r = c.post("/pin", data={"film_id": "HO9", "month": "2026-11", "next": "https://evil.example"},
                   follow_redirects=False)
        self.assertTrue(r.headers["location"].startswith("/?msg="))  # never an outside redirect


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestWantAndPin(unittest.TestCase):
    def test_want_unwant_and_summary(self):
        c, s, db = client()
        r = c.post("/want", data={"film_id": "HO8", "next": "/"}, follow_redirects=False)
        self.assertIn(r.status_code, (302, 303))
        self.assertIsNotNone(db.one("SELECT 1 FROM wants WHERE film_id='HO8'"))
        home = c.get("/").text
        self.assertIn("You want to see 1 film", home)
        c.post("/unwant", data={"film_id": "HO8", "next": "/"})
        self.assertIsNone(db.one("SELECT 1 FROM wants WHERE film_id='HO8'"))

    def test_film_page_offers_the_right_months(self):
        c, s, db = client()
        page = c.get("/film/HO9").text  # Ebenezer opens in November
        self.assertIn("☆ Want to see", page)
        self.assertNotIn("I'm seeing this", page)  # one button now
        self.assertNotIn('class="whenpick"', page)  # the month choice appears once it's wanted
        c.post("/want", data={"film_id": "HO9", "next": "/film/HO9"})
        page = c.get("/film/HO9").text
        self.assertIn("Wanted: next free ticket", page)
        self.assertIn('class="whenpick"', page)
        self.assertNotIn('value="2026-10"', page)
        self.assertIn('value="2026-11"', page)
        c.post("/pin", data={"film_id": "HO9", "month": "2026-11", "next": "/film/HO9"})
        self.assertEqual(db.one("SELECT month FROM pins WHERE film_id='HO9'")["month"], "2026-11")
        self.assertIn("Wanted: November", c.get("/film/HO9").text)
        c.post("/unpin", data={"film_id": "HO9", "next": "/film/HO9"})  # back to the next free ticket, still wanted
        self.assertIsNotNone(db.one("SELECT 1 FROM wants WHERE film_id='HO9'"))
        c.post("/pin", data={"film_id": "HO9", "month": "2026-11", "next": "/film/HO9"})
        c.post("/unwant", data={"film_id": "HO9", "next": "/film/HO9"})  # Remove clears the month too
        self.assertIsNone(db.one("SELECT 1 FROM pins WHERE film_id='HO9'"))


class TestTheme(unittest.TestCase):
    def test_theme_switch(self):
        c, s, db = client()
        self.assertNotIn('data-theme=', c.get("/").text.split("<head>")[0])  # follows the device by default
        c.post("/settings", data={**FORM, "theme": "light"})
        self.assertIn('data-theme="light"', c.get("/").text)
        c.post("/settings", data={**FORM, "theme": "system"})
        self.assertNotIn('data-theme=', c.get("/").text.split("<head>")[0])
        self.assertEqual(c.get("/static/app-icon.png").status_code, 200)


class TestFilmNotices(unittest.TestCase):
    def test_notice_on_plan(self):
        from datetime import datetime as _dt
        c, s, db = client()
        today = _dt.now(s.tz).date().isoformat()
        db.set("film_notices", [{"film_id": "X", "kind": "gone", "on": today,
                                 "text": "**Extra <Geography>** is no longer listed at Vue Test."}])
        r = c.get("/")
        self.assertIn("<strong>Extra &lt;Geography&gt;</strong> is no longer listed", r.text)


class TestTripsPage(unittest.TestCase):
    def test_add_trips(self):
        c, s, db = client()
        s.tmdb_key = ""  # no network: unmatched trips are still kept
        r = c.post("/trips", data={"trips": "date,film\n2023-07-07,Asteroid City\n", "format": "json"})
        self.assertEqual(r.json()["added"], 1)
        page = c.get("/settings").text
        self.assertIn("Cinema trips", page)
        self.assertIn("1 film since July 2023", page)


class TestPickShowing(unittest.TestCase):
    def test_use_then_pick_showing(self):
        c, s, db = client()
        db.set("vue_last_listing", "2099-01-01T06:00:00")  # every listed showing is current
        db.x("UPDATE sessions SET start=datetime('now','+2 days'), last_seen='2099-01-01T07:00:00' WHERE film_id='HO2'")
        r = c.post("/use", data={"film_id": "HO2", "next": "/"}, follow_redirects=False)
        self.assertTrue(r.headers["location"].endswith("#showing"))
        self.assertIn("msg=Ticket+used", r.headers["location"])
        self.assertIn("Which showing did you book", c.get("/").text)
        sid = db.one("SELECT session_id FROM sessions WHERE film_id='HO2'")["session_id"]
        c.post("/showing", data={"session_id": sid, "next": "/"})
        self.assertNotIn("Which showing did you book", c.get("/").text)
        self.assertIsNotNone(db.one("SELECT start FROM cinema_trips WHERE film_id='HO2' AND start IS NOT NULL"))


class TestPaidTrips(unittest.TestCase):
    def test_booked_when_tickets_are_gone_and_fixing_an_extra_ticket(self):
        c, s, db = client()
        month = __import__("datetime").date.today().strftime("%Y-%m")
        for fid in ("HO1", "HO3"):
            c.post("/use", data={"film_id": fid, "next": "/"})
        page = c.get("/film/HO2").text
        self.assertIn("Booked (paid)", page)  # this month's tickets are used
        self.assertNotIn("Booked: use a ticket", page)
        c.post("/use", data={"film_id": "HO2", "paid": "1", "next": "/"})
        self.assertIsNone(db.one("SELECT 1 FROM ticket_uses WHERE film_id='HO2' AND active=1"))
        self.assertEqual(db.one("SELECT source FROM cinema_trips WHERE film_id='HO2'")["source"], "paid")
        # an older mistake: a third trip marked as a ticket
        db.x("INSERT INTO ticket_uses(month,film_id,tmdb_id,title,used_on,source,active,created_at) "
             "VALUES(?,'HO11',NULL,'Digger',date('now'),'dashboard',1,'x')", (month,))
        c.post("/settings", data={})  # any post replans
        page = c.get("/").text
        self.assertIn("3 trips are marked as tickets this month", page)
        use = db.one("SELECT id FROM ticket_uses WHERE film_id='HO11'")["id"]
        c.post("/paid", data={"use_id": use, "next": "/"})
        self.assertNotIn("trips are marked as tickets", c.get("/").text)
        self.assertEqual(db.one("SELECT source FROM cinema_trips WHERE film_id='HO11'")["source"], "paid")


class TestContextAndUndo(unittest.TestCase):
    def test_want_from_a_month_and_undo(self):
        c, s, db = client()
        p = c.get("/?m=1").text  # November
        self.assertIn('/film/HO9?from=2026-11', p)  # film links carry the month
        self.assertNotIn('class="wantstar', p)       # no stars in the lists any more
        page = c.get("/film/HO9?from=2026-11").text
        self.assertIn("Want to see in November", page)
        self.assertIn("or earliest free ticket", page)
        self.assertIn("☆ Want to see<", c.get("/film/HO9").text)  # no month: earliest free ticket
        r = c.post("/pin", data={"film_id": "HO9", "month": "2026-11", "next": "/film/HO9?from=2026-11"},
                   follow_redirects=False)
        loc = r.headers["location"]
        self.assertIn("in+November", loc)
        tok = loc.split("undo=")[1].split("#")[0]
        page = c.get(loc).text
        self.assertIn(">Undo<", page)
        c.post("/undo-last", data={"token": tok, "next": "/film/HO9"})
        self.assertIsNone(db.one("SELECT 1 FROM wants WHERE film_id='HO9'"))
        self.assertIsNone(db.one("SELECT 1 FROM pins WHERE film_id='HO9'"))
        r = c.post("/undo-last", data={"token": tok, "next": "/"}, follow_redirects=False)
        self.assertIn("can%27t+be+undone", r.headers["location"])  # only once

    def test_undo_a_ticket(self):
        c, s, db = client()
        r = c.post("/use", data={"film_id": "HO1", "next": "/"}, follow_redirects=False)
        tok = r.headers["location"].split("undo=")[1].split("#")[0]
        self.assertIsNotNone(db.one("SELECT 1 FROM ticket_uses WHERE film_id='HO1' AND active=1"))
        c.post("/undo-last", data={"token": tok, "next": "/"})
        self.assertIsNone(db.one("SELECT 1 FROM ticket_uses WHERE film_id='HO1'"))
        self.assertIsNone(db.one("SELECT 1 FROM cinema_trips WHERE film_id='HO1'"))
