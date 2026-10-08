"""The read-only feed other apps use (GET /api/seen)."""
import unittest

from app import tickets
from app.db import DB


class TestSeenFeed(unittest.TestCase):
    def test_tickets_and_watch_counts(self):
        db = DB(":memory:")
        db.x("INSERT INTO ticket_uses(month,film_id,tmdb_id,title,used_on,source,active,created_at) VALUES('2026-09','HO1',11,'Film A','2026-09-04','manual',1,'x')")
        db.x("INSERT INTO ticket_uses(month,film_id,tmdb_id,title,used_on,source,active,created_at) VALUES('2026-09','HO2',12,'Undone','2026-09-05','manual',0,'x')")
        for k, d in (("a", "2024-01-01"), ("b", "2025-02-02")):
            db.x("INSERT INTO diary(entry_key,tmdb_id,title,year,watched_date,rating,rewatch,source) VALUES(?,?,?,?,?,?,?,?)",
                 (k, 11, "Film A", 2024, d, 4.5, 0, "export"))
        seen = tickets.seen_feed(db)
        self.assertEqual([t["tmdb_id"] for t in seen["tickets"]], [11])  # undone tickets don't count
        self.assertEqual([(d["tmdb_id"], d["watches"], d["last"]) for d in seen["diary"]], [(11, 2, "2025-02-02")])


if __name__ == "__main__":
    unittest.main()
