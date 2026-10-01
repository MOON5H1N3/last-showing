"""History import and run model, against a small fake archive with the real file format."""
import asyncio
import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import history  # noqa: E402
from app.db import DB  # noqa: E402

START = date(2025, 11, 12)
TAGS = [(START + timedelta(days=3 * i)).strftime("%Y%m%d") + ".060000" for i in range(60)]


def film_runs(site):
    """Fake films: 'big' ones run 8 weeks with lots of showings; 'small' ones 10 days with few."""
    films = []
    for n in range(40):
        big = n % 4 == 0
        opens = START + timedelta(days=7 + 4 * n)
        films.append((f"{site}-HO{n:05d}", opens, opens + timedelta(days=56 if big else 10), 30 if big else 4))
    return films


def snapshot(site, snap_day):
    out = []
    for sid, a, b, per_week in film_runs(site):
        perfs = []
        d = max(a, snap_day)
        while d <= min(b, snap_day + timedelta(days=8)):
            for k in range(max(1, per_week // 7)):
                t = datetime(d.year, d.month, d.day, 12 + k % 10, tzinfo=timezone.utc)
                perfs.append({"time": int(t.timestamp() * 1000), "screen": "1"})
            d += timedelta(days=1)
        if perfs:
            out.append({"category": "movie", "showingId": f"myvue.com-{sid}", "title": sid, "performances": perfs,
                        "themoviedb": {"id": abs(hash(sid)) % 10**6, "releaseDate": a.isoformat()}})
    return out


def handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if url.endswith("service=git-upload-pack"):
        body = "".join(f"003f{'0' * 40} refs/tags/{t}\n" for t in ["20250101.000000"] + TAGS)
        return httpx.Response(200, text=body)
    if "/releases/download/" in url:
        tag, fname = url.rsplit("/", 2)[-2:]
        site = fname.replace("myvue.com-", "")
        snap_day = datetime.strptime(tag[:8], "%Y%m%d").date()
        return httpx.Response(200, json=snapshot(site, snap_day))
    return httpx.Response(404)


class TestHistory(unittest.TestCase):
    def test_import_and_model(self):
        db = DB(":memory:")

        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
                first = await history.import_history(db, ["harrow", "romford"], client=c)
                again = await history.import_history(db, ["harrow", "romford"], client=c)
            return first, again

        first, again = asyncio.run(run())
        self.assertEqual(first["snapshots_added"], 60)
        self.assertEqual(again["snapshots_added"], 0)  # incremental
        self.assertEqual(first["files"], 120)
        states = history.build_states(db, cribbs=False)
        self.assertTrue(states)
        rm = history.RunModel(states, k=20)
        self.assertTrue(rm.ready)
        # a big film one week in should very likely be on in 3 weeks; a small one should not
        big, _ = rm.estimate(date(2026, 3, 1), None, 1.0, 0.35, 1.0)
        small, _ = rm.estimate(date(2026, 3, 1), None, 1.0, 0.05, 1.0)
        self.assertGreater(rm.p_still_showing(big, 21), 0.8)
        self.assertLess(rm.p_still_showing(small, 21), 0.2)
        bt = history.backtest(states, k=20)
        self.assertTrue(bt["ok"])
        self.assertLess(bt["horizons"]["14"]["brier"], bt["horizons"]["14"]["brier_weeks_only"] + 1e-9)

    def test_pick_tags(self):
        tags = ["20251101.000000", "20251112.010000", "20251112.200000", "20251114.000000", "20251115.000000"]
        self.assertEqual(history.pick_tags(tags, 3), ["20251112.010000", "20251115.000000"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
