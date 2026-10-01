"""Shared test data: a trimmed copy of real Vue API responses (shape captured from Vue Cribbs Causeway)."""
from __future__ import annotations

import random
from datetime import date, datetime, timedelta


def _session(sid, dt: datetime, screen="Screen 5", extra=()):
    attrs = [{"name": "English", "shortName": "", "value": "english", "attributeType": "Language"},
             {"name": "Ultra Lux and Lux", "shortName": "Ultra Lux and Lux", "value": "ultra-lux-and-lux",
              "attributeType": "Session"}]
    attrs += [{"name": e, "shortName": e, "value": e, "attributeType": "Session"} for e in extra]
    return {"sessionId": str(sid), "showTimeWithTimeZone": dt.isoformat(), "startTime": dt.replace(tzinfo=None).isoformat(),
            "screenName": screen, "bookingUrl": f"/book-tickets/summary/10018/X/{sid}", "isSoldOut": False,
            "attributes": attrs}


def _film(fid, title, release, status, distributor, director, days, per_day=4, attrs=(), extra=(), original=None,
          screen="Screen 5"):
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("Europe/London")
    groups = []
    sid = sum(ord(c) * 31 ** i for i, c in enumerate(fid)) % 100000 * 100
    for d in days:
        sess = []
        for k in range(per_day):
            sid += 1
            sess.append(_session(sid, datetime(d.year, d.month, d.day, 12 + k * 3, 0, tzinfo=tz), screen, extra))
        groups.append({"date": d.isoformat() + "T00:00:00", "sessions": sess})
    return {"filmId": fid, "filmTitle": title, "originalTitle": original or title, "releaseDate": release + "T00:00:00",
            "filmStatus": status, "hasSessions": bool(groups), "runningTime": 100, "director": director,
            "distributor": distributor, "genres": [], "filmUrl": f"https://www.myvue.com/film/{fid.lower()}",
            "posterImageSrc": "", "certificate": {"name": "15"},
            "filmAttributes": [{"name": a, "value": a} for a in attrs], "showingGroups": groups}


def rng(start: date, n: int) -> list[date]:
    return [start + timedelta(days=i) for i in range(n)]


def vue_responses():
    o1 = date(2026, 10, 1)
    with_s = [
        _film("HO1", "Resident Evil", "2026-09-18", 1, "Sony Pictures Entertainment (UK + EIRE)", "Zach Cregger", rng(o1, 8)),
        _film("HO2", "Heart of the Beast", "2026-09-25", 1, "Paramount Pictures International (UK + EIRE)", "David Ayer", rng(o1, 8)),
        _film("HO3", "Leviticus", "2026-09-02", 1, "Picturehouse Entertainment", "Adrian Chiarella",
              [date(2026, 10, 2), date(2026, 10, 3), date(2026, 10, 6)], per_day=1),
        _film("HO4", "The Odyssey", "2026-07-17", 1, "Universal Pictures (UK + IE)", "Christopher Nolan",
              [date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 4)], per_day=2),
        _film("HO5", "Casino Royale (20th Anniversary)", "2026-10-05", 1, "Park Circus", "Martin Campbell",
              [date(2026, 10, 3), date(2026, 10, 5)], per_day=1, attrs=["movie-milestones"], original="Casino Royale"),
        _film("HO6", "Hadestown: The Musical", "2026-10-13", 2, "Trafalgar Releasing", "Brett Sullivan",
              rng(date(2026, 10, 13), 3), per_day=2, attrs=["Big-Screen-Musicals"], extra=["event"]),
        _film("HO7", "Avengers: Doomsday", "2026-12-16", 2, "Walt Disney Studios Motion Pictures International (UK + IE)",
              "Anthony Russo, Joe Russo", rng(date(2026, 12, 16), 3), extra=["epic"], screen="EPIC"),
        _film("HO10", "Met Opera 2026-27: Macbeth", "2026-10-17", 2, "CinemaLive", "", [date(2026, 10, 17)], per_day=1,
              extra=["event"]),
        _film("HO11", "Digger", "2026-10-02", 1, "Warner Bros. Entertainment Inc. (UK + IE)", "Alejandro G. Iñárritu",
              rng(date(2026, 10, 2), 7)),
    ]
    all_f = with_s + [
        _film("HO8", "Clayface", "2026-10-23", 2, "Warner Bros. Entertainment Inc. (UK + IE)", "James Watkins", []),
        _film("HO9", "Ebenezer", "2026-11-13", 2, "Sony Pictures Entertainment (UK + EIRE)", "Ti West", []),
    ]
    return {"responseCode": 0, "result": with_s}, {"responseCode": 0, "result": all_f}


def meta(tid, title, year, genres, directors, cast=(), keywords=(), companies=(), lang="en", va=7.0, vc=500):
    return {"id": tid, "title": title, "original_title": title, "year": year, "release_date": f"{year}-01-01",
            "runtime": 100, "language": lang, "genres": list(genres), "directors": list(directors), "writers": [],
            "dop": [], "composer": [], "cast": list(cast), "companies": list(companies), "countries": ["US"],
            "keywords": list(keywords), "vote_average": va, "vote_count": vc, "popularity": 10, "poster": None,
            "overview": f"About {title}."}


# Cinema films -> tmdb ids + metadata
CINEMA_META = {
    "HO1": meta(9001, "Resident Evil", 2026, ["Horror", "Action"], ["Zach Cregger"], ["Actor Horror1", "Actor A"], ["zombie", "survival"]),
    "HO2": meta(9002, "Heart of the Beast", 2026, ["Action", "Drama"], ["David Ayer"], ["Actor B"], ["dog"]),
    "HO3": meta(9003, "Leviticus", 2026, ["Horror", "Drama"], ["Adrian Chiarella"], ["Actor Horror1"], ["religion", "supernatural"]),
    "HO4": meta(9004, "The Odyssey", 2026, ["Adventure", "Fantasy"], ["Christopher Nolan"], ["Actor C"], ["epic"]),
    "HO5": meta(36557, "Casino Royale", 2006, ["Action", "Thriller"], ["Martin Campbell"], ["Daniel Craig"], ["spy"]),
    "HO6": meta(9006, "Hadestown", 2026, ["Music"], ["Brett Sullivan"], ["Actor M"], ["musical"]),
    "HO7": meta(9007, "Avengers: Doomsday", 2026, ["Action", "Science Fiction"], ["Anthony Russo"], ["Actor D"], ["superhero"]),
    "HO8": meta(9008, "Clayface", 2026, ["Horror"], ["James Watkins"], ["Actor Horror1"], ["body horror"]),
    "HO9": meta(9009, "Ebenezer", 2026, ["Horror", "Fantasy"], ["Ti West"], ["Actor E"], ["christmas", "ghost"]),
    "HO11": meta(9011, "Digger", 2026, ["Comedy"], ["Alejandro G. Iñárritu"], ["Actor F"], ["satire"], va=6.4),
}


def training_set(seed=4):
    """60 rated films: loves horror (esp. Zach Cregger / Actor Horror1), lukewarm on superheroes, dislikes musicals."""
    r = random.Random(seed)
    rows = []
    tid = 100
    for i in range(22):
        tid += 1
        d = ["Zach Cregger"] if i < 3 else ["Horror Dir %d" % (i % 5)]
        rows.append((meta(tid, f"Horror {i}", 2010 + i % 15, ["Horror"], d, ["Actor Horror1" if i % 2 else "Actor X"],
                          ["supernatural" if i % 3 else "zombie"]), min(5, 4.0 + r.choice([0, 0.5, 1.0]) * (1 if i < 3 else 0.6))))
    for i in range(18):
        tid += 1
        rows.append((meta(tid, f"Hero {i}", 2012 + i % 12, ["Action", "Science Fiction"], ["Hero Dir %d" % (i % 4)],
                          ["Actor D" if i % 2 else "Actor Y"], ["superhero"]), 2.5 + r.choice([0, 0.5])))
    for i in range(10):
        tid += 1
        rows.append((meta(tid, f"Musical {i}", 2005 + i, ["Music", "Romance"], ["Musical Dir"], ["Actor M"], ["musical"]),
                     1.5 + r.choice([0, 0.5])))
    for i in range(10):
        tid += 1
        rows.append((meta(tid, f"Drama {i}", 2000 + i, ["Drama"], ["Drama Dir %d" % (i % 3)], ["Actor Z"], ["family"]),
                     3.0 + r.choice([0, 0.5])))
    return rows
