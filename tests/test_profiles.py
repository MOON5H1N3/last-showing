"""A second profile (someone without Letterboxd): its own tickets, wants and plan; the cinema shared; picks from the
crowd, critics, buzz and the taste quiz until it has 25 ratings."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from starlette.testclient import TestClient  # noqa: E402

import app.jobs as J  # noqa: E402
import app.planner as P  # noqa: E402
from app import quiz  # noqa: E402
from app.db import DB, ProfileDB  # noqa: E402
from app.jobs import Engine  # noqa: E402
from app.taste import StarterModel  # noqa: E402
from app.web import create_app  # noqa: E402
from tests.test_core import NOW, seeded_db, settings  # noqa: E402


def setup():
    s = settings()
    mem = seeded_db()
    path = s.data_dir / "lastshowing.sqlite3"
    mem.backup_to(path)
    db = DB(path)
    eng = Engine(s, db)
    return s, db, eng


class TestProfiles(unittest.TestCase):
    def setUp(self):
        self._mp = J.make_plan
        J.make_plan = lambda db, s, *a, **k: P.make_plan(db, s, NOW)  # fixed clock for every profile

    def tearDown(self):
        J.make_plan = self._mp

    def test_shared_reads_personal_writes(self):
        s, db, _ = setup()
        db.set("plan", {"secret": 1})
        db.set("vue_last_listing", "2026-10-01T06:00:00")
        p = ProfileDB(s.data_dir / "profiles" / "dad.sqlite3", db.path)
        self.assertIsNone(p.get("plan"))                                # someone else's plan never shows through
        self.assertEqual(p.get("vue_last_listing"), "2026-10-01T06:00:00")  # the listings are shared
        self.assertGreater(p.one("SELECT COUNT(*) n FROM vue_films")["n"], 0)
        self.assertEqual(p.one("SELECT COUNT(*) n FROM ratings")["n"], 0)
        p.x("INSERT INTO wants(film_id,tmdb_id,title,created_at) VALUES('HO1',1,'x','x')")
        self.assertIsNone(db.one("SELECT 1 FROM wants WHERE film_id='HO1'"))

    def test_starter_model_follows_the_crowd_and_quiz(self):
        s, db, _ = setup()
        p = ProfileDB(s.data_dir / "profiles" / "dad.sqlite3", db.path)
        self.assertIsInstance(P.build_model(p), StarterModel)
        self.assertNotIsInstance(P.build_model(db), StarterModel)       # the main profile has its ratings
        m = StarterModel().fit([])
        hi, lo = m.raw({"genres": ["Drama"]}, 4.0, 50000)[0], m.raw({"genres": ["Drama"]}, 2.8, 50000)[0]
        self.assertGreater(hi, lo)
        self.assertAlmostEqual(hi, 4.0, delta=0.05)                     # a well-known film: about its average
        q = StarterModel(likes=["Comedy"], avoids=["Horror"]).fit([])
        self.assertGreater(q.raw({"genres": ["Comedy"]}, 3.5, 50000)[0], m.raw({"genres": ["Comedy"]}, 3.5, 50000)[0])
        self.assertLess(q.raw({"genres": ["Horror", "Comedy"]}, 3.5, 50000)[0], 3.5)  # skip wins over like
        pred = q.predict({"genres": ["Horror"]}, 3.5, n=50000)
        self.assertTrue(any("skip horror" in r for r in pred.reasons))

    def test_quiz_answers_are_ratings(self):
        s, db, _ = setup()
        p = ProfileDB(s.data_dir / "profiles" / "dad.sqlite3", db.path)
        quiz.answer(p, 9001, "loved")
        quiz.answer(p, 9002, "unseen")
        self.assertEqual(quiz.answered(p), {9001: 4.5})
        quiz.answer(p, 9001, "no")
        self.assertEqual(quiz.answered(p), {9001: 1.5})
        self.assertIn(9002, quiz.get(p)["unseen"])
        self.assertIsNone(db.one("SELECT 1 FROM ratings WHERE tmdb_id=9001"))

    def test_web_add_switch_and_own_tickets(self):
        s, db, eng = setup()
        eng.replan()
        c = TestClient(create_app(eng))
        self.assertNotIn("Viewing as", c.get("/").text)                  # one profile: no switcher
        r = c.post("/profile/add", data={"name": "Dad", "tickets": "2"}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertIn("/quiz", r.headers["location"])
        self.assertEqual(c.cookies.get("ls_profile"), "dad")
        for url in ["/", "/films", "/film/HO1", "/settings", "/quiz", "/together", "/taste", "/?m=1"]:
            self.assertEqual(c.get(url).status_code, 200, url)
        page = c.get("/settings").text
        self.assertIn("Take the taste quiz", page)
        self.assertNotIn("Letterboxd username", page)
        self.assertIn("Viewing as", c.get("/").text)
        c.post("/quiz/genres", data={"g:Comedy": "like", "g:Horror": "skip"})
        self.assertEqual(engine_db(eng, "dad").get("quiz")["likes"], ["Comedy"])
        c.post("/want", data={"film_id": "HO1", "next": "/"})
        self.assertIsNotNone(engine_db(eng, "dad").one("SELECT 1 FROM wants WHERE film_id='HO1'"))
        self.assertIsNone(db.one("SELECT 1 FROM wants WHERE film_id='HO1'"))  # the main profile's are untouched
        c.post("/settings", data={"profile_name": "Pops", "ticket_source": "monzo", "tickets_per_month": "1",
                                  "theme": "light"})
        self.assertEqual(s.tickets_per_month, 2)                         # main settings unchanged
        self.assertEqual(eng.profiles.settings("dad").tickets_per_month, 1)
        self.assertIn('lang="en-GB" data-theme="light"', c.get("/").text)
        c.post("/rate", data={"film_id": "HO1", "rating": "4"})
        self.assertEqual(engine_db(eng, "dad").one("SELECT rating FROM ratings WHERE source='app'")["rating"], 4.0)
        c.post("/profile", data={"profile": "me", "next": "/"})
        self.assertNotIn('lang="en-GB" data-theme="light"', c.get("/").text)
        self.assertIn("Films you&#39;d both enjoy with Pops", c.get("/").text.replace("'", "&#39;"))
        self.assertEqual(c.get("/together").status_code, 200)
        eng.profiles.replan_all()

    def test_remove_keeps_the_file(self):
        s, db, eng = setup()
        eng.replan()
        c = TestClient(create_app(eng))
        c.post("/profile/add", data={"name": "Dad"})
        c.post("/profile/remove", data={"profile": "dad"})
        self.assertEqual([p["id"] for p in eng.profiles.all()], ["me"])
        self.assertTrue(list((s.data_dir / "profiles").glob("dad-removed-*.sqlite3")))


def engine_db(eng, pid):
    return eng.profiles.engine(pid).db


if __name__ == "__main__":
    unittest.main()
