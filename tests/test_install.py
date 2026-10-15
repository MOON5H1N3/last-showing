"""The dashboard can be installed on a phone (from a secure address): its manifest and icons load, even behind a password."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from starlette.testclient import TestClient  # noqa: E402

from app.jobs import Engine  # noqa: E402
from app.web import create_app  # noqa: E402
from tests.test_core import seeded_db, settings  # noqa: E402


class TestInstall(unittest.TestCase):
    def test_manifest(self):
        s = settings()
        c = TestClient(create_app(Engine(s, seeded_db())))
        m = c.get("/manifest.webmanifest").json()
        self.assertEqual((m["name"], m["display"], m["start_url"]), ("Last Showing", "standalone", "/"))
        for icon in m["icons"]:
            self.assertEqual(c.get(icon["src"]).status_code, 200, icon["src"])
        self.assertTrue(any(i["purpose"] == "maskable" for i in m["icons"]))

    def test_install_files_open_behind_a_password(self):
        s = settings()
        s.dashboard_password = "pw"
        c = TestClient(create_app(Engine(s, seeded_db())))
        self.assertEqual(c.get("/settings").status_code, 401)
        for path in ("/manifest.webmanifest", "/static/icon-192.png", "/static/icon-maskable-512.png", "/health"):
            self.assertEqual(c.get(path).status_code, 200, path)


if __name__ == "__main__":
    unittest.main()
