"""Predicts how you'd rate a film, learned from your own Letterboxd ratings.

How it works, in plain terms:
1. For every director, writer, actor, genre, keyword, studio, language and decade in films you've rated,
   work out how much above or below your average you tend to rate them. Things you've only seen once
   are pulled towards zero, so one great film doesn't make an actor a "favourite".
2. A film's "affinity" for each of those groups is the average of its people/genres/etc.
3. A small regression learns how much each group matters *for you* (maybe directors predict your
   ratings far better than cast), plus how much the Letterboxd community average helps.
   While learning, each film's own rating is left out of its affinities, so the model can't cheat.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field

import numpy as np

# (feature type, how many films before we trust it, human label)
TYPES = [
    ("genre", 8.0, "Genre"),
    ("director", 1.5, "Director"),
    ("writer", 2.0, "Writer"),
    ("cast", 3.0, "Actor"),
    ("crew", 2.0, "Crew"),
    ("keyword", 6.0, "Theme"),
    ("company", 4.0, "Studio"),
    ("lang", 6.0, "Language"),
    ("decade", 8.0, "Decade"),
]
SHRINK = {t: k for t, k, _ in TYPES}
LABEL = {t: l for t, _, l in TYPES}
COLS = ["community"] + [t for t, _, _ in TYPES]
DEFAULT_W = {"community": 0.7, **{t: 0.6 for t, _, _ in TYPES}}


def features(m: dict) -> dict[str, list[str]]:
    y = m.get("year")
    return {
        "genre": m.get("genres") or [],
        "director": m.get("directors") or [],
        "writer": [w for w in (m.get("writers") or [])[:4] if w not in (m.get("directors") or [])],
        "cast": (m.get("cast") or [])[:8],
        "crew": (m.get("dop") or []) + (m.get("composer") or []),
        "keyword": (m.get("keywords") or [])[:20],
        "company": (m.get("companies") or [])[:3],
        "lang": [m["language"]] if m.get("language") else [],
        "decade": [f"{(y // 10) * 10}s"] if y else [],
    }


FAV = 4.5  # a rating at or above this counts as a favourite
EARLY_K = 60  # a Letterboxd average from this many ratings counts half as much as a settled one


def credibility(n: int | None) -> float:
    """How much to trust a community average built from n ratings (1 = fully)."""
    return 1.0 if n is None else n / (n + EARLY_K)


@dataclass
class Prediction:
    rating: float
    confidence: str
    reasons: list[str] = field(default_factory=list)
    community: float | None = None
    fav_chance: float | None = None  # chance you'd rate it 4.5★ or more


def _n(row: tuple) -> int | None:
    return row[3] if len(row) > 3 else None


def _sigmoid(z):
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))


class TasteModel:
    """cols: which inputs the model may use (all by default). lam: how strongly weights are held back."""

    def __init__(self, cols: list[str] | None = None, lam: float = 3.0, shrink_scale: float = 1.0):
        self.cols = list(cols or COLS)
        self.lam = lam
        self._scale = shrink_scale
        self.shrink = {t: k * shrink_scale for t, k in SHRINK.items()}
        self.no_comm: "TasteModel | None" = None
        self.mu = 3.25
        self.stats: dict[str, dict[str, list[float]]] = {}  # type -> name -> [sum_dev, n]
        self.fav_counts: dict[str, list[int]] = {}           # genre -> [favourites, all]
        self.w = {c: (DEFAULT_W[c] if c in self.cols else 0.0) for c in COLS}
        self.b = 0.0
        self.c_mean = 3.3
        self.fav_coef: np.ndarray | None = None
        self.fav_base = 0.1
        self.n = 0
        self.mae = None
        self.mae_baseline = None
        self.check: dict | None = None  # held-out results, filled in by evaluate()

    # ---------- training ----------
    def fit(self, rows: list[tuple]) -> "TasteModel":
        """rows: (tmdb metadata, your rating, community average or None[, how many ratings that average is from])"""
        rows = [r for r in rows if r[0] and r[1] is not None]
        # films with no community average (not out yet, or too few ratings) are scored by a model that never
        # relied on one, rather than by this model with the average treated as "exactly average"
        self.no_comm = None
        if "community" in self.cols and len(self.cols) > 1 and len(rows) >= 25:
            self.no_comm = TasteModel([c for c in self.cols if c != "community"], self.lam, self._scale).fit(rows)
        self.n = len(rows)
        if not rows:
            return self
        ys = np.array([r[1] for r in rows], dtype=float)
        self.mu = float(ys.mean())
        cs = [r[2] for r in rows if r[2] is not None]
        self.c_mean = float(np.mean(cs)) if cs else 3.3

        stats: dict[str, dict[str, list[float]]] = {t: defaultdict(lambda: [0.0, 0]) for t in SHRINK}
        fav: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        feats = [features(r[0]) for r in rows]
        for f, y in zip(feats, ys):
            for t, names in f.items():
                for nm in set(names):
                    s = stats[t][nm]
                    s[0] += y - self.mu
                    s[1] += 1
            for g in set(f["genre"]):
                fav[g][1] += 1
                fav[g][0] += int(y >= FAV)
        self.stats = {t: dict(v) for t, v in stats.items()}
        self.fav_counts = dict(fav)

        # each film's own rating is left out of its affinities, so the weights are learned honestly
        X = np.array([self._row(f, r[2], leave_out=y - self.mu, n=_n(r)) for f, r, y in zip(feats, rows, ys)])
        mask = np.array([c in self.cols for c in COLS], dtype=float)
        X = X * mask
        if self.n >= 25:
            A = np.c_[np.ones(len(X)), X]
            reg = self.lam * np.eye(A.shape[1])
            reg[0, 0] = 0
            coef = np.linalg.solve(A.T @ A + reg, A.T @ (ys - self.mu))
            self.b = float(coef[0])
            self.w = {c: (float(np.clip(v, 0.0, 1.6)) if c in self.cols else 0.0) for c, v in zip(COLS, coef[1:])}
            self._fit_fav(A, (ys >= FAV).astype(float))
        pred = np.clip(self.mu + self.b + X @ np.array([self.w[c] for c in COLS]), 0.5, 5.0)
        self.mae = float(np.mean(np.abs(pred - ys)))
        self.mae_baseline = float(np.mean(np.abs(ys - self.mu)))
        return self

    def _fit_fav(self, A: np.ndarray, y: np.ndarray, lam: float = 2.0) -> None:
        """Logistic regression (Newton's method) for the chance of a 4.5★+ rating."""
        self.fav_base = float(y.mean())
        if y.sum() < 5:
            self.fav_coef = None
            return
        beta = np.zeros(A.shape[1])
        beta[0] = math.log(self.fav_base / (1 - self.fav_base))
        reg = lam * np.eye(A.shape[1])
        reg[0, 0] = 0
        for _ in range(25):
            p = _sigmoid(A @ beta)
            g = A.T @ (p - y) + reg @ beta
            H = (A * (p * (1 - p))[:, None]).T @ A + reg
            step = np.linalg.solve(H, g)
            beta -= step
            if np.abs(step).max() < 1e-6:
                break
        self.fav_coef = beta

    def _dev(self, t: str, name: str, leave_out: float | None = None) -> tuple[float, int]:
        s = self.stats.get(t, {}).get(name)
        if not s:
            return 0.0, 0
        total, n = s
        if leave_out is not None:
            total, n = total - leave_out, n - 1
        if n <= 0:
            return 0.0, 0
        return total / (n + self.shrink[t]), n

    def _row(self, f: dict[str, list[str]], community: float | None, leave_out: float | None = None,
             n: int | None = None) -> list[float]:
        row = [(community - self.c_mean) * credibility(n) if community is not None else 0.0]
        for t, _, _ in TYPES:
            devs = [d for d, n in (self._dev(t, nm, leave_out) for nm in set(f.get(t, []))) if n > 0]
            row.append(float(np.mean(devs)) if devs else 0.0)
        return row

    # ---------- prediction ----------
    def raw(self, meta: dict, community: float | None, n: int | None = None,
            fallback: bool = True) -> tuple[float, float | None]:
        if community is None and fallback and self.no_comm is not None:
            return self.no_comm.raw(meta, None)
        x = np.array(self._row(features(meta or {}), community, n=n)) * np.array([c in self.cols for c in COLS], dtype=float)
        rating = float(min(5.0, max(0.5, self.mu + self.b + sum(self.w[c] * v for c, v in zip(COLS, x)))))
        fav = float(_sigmoid(self.fav_coef @ np.r_[1.0, x])) if self.fav_coef is not None else None
        return rating, fav

    def predict(self, meta: dict, community: float | None, community_label: str = "Letterboxd average",
                n: int | None = None) -> Prediction:
        f = features(meta or {})
        comm_n = n
        rating, fav = self.raw(meta, community, comm_n)

        reasons: list[tuple[float, str]] = []
        for t, _, _ in TYPES:
            if t in ("decade", "lang"):
                continue
            for nm in set(f.get(t, [])):
                d, n = self._dev(t, nm)
                if n >= 2 and abs(d) >= 0.12:
                    avg_gap = d * (n + self.shrink[t]) / n  # the plain average gap, before shrinking
                    word = "For" if d > 0 else "Against"
                    sign = "+" if d > 0 else "−"
                    reasons.append((abs(d) * (self.w[t] + 0.2), d > 0,
                                    f"{word}: {LABEL[t].lower()} {nm}, {sign}{abs(avg_gap):.1f}★ across {n} of your films"))
        reasons.sort(key=lambda r: -r[0])
        top = reasons[:4]
        top.sort(key=lambda r: (not r[1], -r[0]))  # the "for" reasons first
        out = [r[2] for r in top]
        if community is not None:
            label = f"{community_label} {community:.2f}★"
            if credibility(comm_n) < 0.8:
                label += f" from only {comm_n:,} rating{'s' if comm_n != 1 else ''}, so it counts for less"
            out.insert(0, label)
        elif self.no_comm is not None and meta:
            out.insert(0, "No Letterboxd average yet, so this is from your taste alone")

        evidence = sum(1 for t in ("director", "writer", "cast", "keyword", "company")
                       for nm in f.get(t, []) if self._dev(t, nm)[1] > 0)
        if not meta:
            conf = "none"
        elif self.n < 25:
            conf = "low"
        elif community is not None and credibility(comm_n) >= 0.8 and evidence >= 4:
            conf = "high"
        elif community is not None or evidence >= 3:
            conf = "medium"
        else:
            conf = "low"
        return Prediction(round(rating, 2), conf, out, community, round(fav, 3) if fav is not None else None)

    # ---------- what it's learned ----------
    def summary(self) -> dict:
        top = {}
        for t in ("director", "cast", "genre", "keyword"):
            items = []
            for nm, (total, n) in self.stats.get(t, {}).items():
                if n >= 2:
                    items.append((nm, total / (n + self.shrink[t]), total / n, n))
            items.sort(key=lambda x: -x[1])
            # a "lean" only when it's big enough to mean something: 0.3★ or more, over 10+ films for genres/themes
            min_n = 10 if t in ("genre", "keyword") else 3
            top[t] = {"loved": [(nm, round(raw, 2), n) for nm, d, raw, n in items if raw >= 0.3 and n >= min_n][:6],
                      "not_for_you": [(nm, round(raw, 2), n) for nm, d, raw, n in items[::-1]
                                      if raw <= -0.3 and n >= min_n][:4]}
        # which genres your favourites come from, against how often you watch them
        n_all = self.n or 1
        fav_total = self.fav_base * n_all if self.n else 0
        favs = []
        for g, (nf, na) in self.fav_counts.items():
            if nf >= 3:
                share_fav = nf / fav_total if fav_total else 0
                share_all = na / n_all
                favs.append((g, nf, round(share_fav, 3), round(share_all, 3)))
        favs.sort(key=lambda x: -x[1])
        return {
            "ratings": self.n, "average": round(self.mu, 2),
            "favourites": int(round(fav_total)), "favourite_rate": round(self.fav_base, 3),
            "favourite_genres": favs[:8],
            "weights": {k: round(v, 2) for k, v in self.w.items()},
            "error": round(self.mae, 2) if self.mae is not None else None,
            "error_baseline": round(self.mae_baseline, 2) if self.mae_baseline is not None else None,
            "check": self.check,
            "top": top,
        }


# name -> (inputs used, how strongly weights are held back, how many films before a person/genre is trusted)
VARIANTS = {
    "Everything": (COLS, 3.0, 1.0),
    "Everything, trusts people and genres sooner": (COLS, 3.0, 0.5),
    "Everything, trusts them later": (COLS, 3.0, 2.0),
    "Community average only": (["community"], 3.0, 1.0),
    "Your taste only, no community average": ([c for c in COLS if c != "community"], 3.0, 1.0),
}


def evaluate(rows: list[tuple], folds: int = 5, seed: int = 7) -> dict:
    """Held-out check: train on 4/5 of your ratings, predict the other 1/5, five times over.

    Reports, for each variant: the average error, and how many of the films it thought most likely to be
    favourites (its top 10%) really were 4.5★+."""
    rows = [r for r in rows if r[0] and r[1] is not None]
    if len(rows) < 50:
        return {"ok": False, "why": f"needs at least 50 rated films with details, has {len(rows)}"}
    rng = np.random.default_rng(seed)
    order = rng.permutation(len(rows))
    fold_of = np.empty(len(rows), dtype=int)
    fold_of[order] = np.arange(len(rows)) % folds
    ys = np.array([r[1] for r in rows])
    pre_old, pre_new = np.zeros(len(rows)), np.zeros(len(rows))  # as if no community average existed yet
    results = []
    for name, (cols, lam, scale) in VARIANTS.items():
        preds = np.zeros(len(rows))
        favs = np.full(len(rows), np.nan)
        for k in range(folds):
            train = [rows[i] for i in range(len(rows)) if fold_of[i] != k]
            m = TasteModel(cols, lam, scale).fit(train)
            for i in np.where(fold_of == k)[0]:
                preds[i], f = m.raw(rows[i][0], rows[i][2], _n(rows[i]))
                if name == "Everything":
                    pre_old[i] = m.raw(rows[i][0], None, fallback=False)[0]
                    pre_new[i] = m.raw(rows[i][0], None)[0]
                if f is not None:
                    favs[i] = f
        mae = float(np.mean(np.abs(preds - ys)))
        top_hit = None
        if not np.isnan(favs).all():
            cut = np.nanquantile(favs, 0.9)
            chosen = favs >= cut
            top_hit = float((ys[chosen] >= FAV).mean())
        results.append({"variant": name, "error": round(mae, 3),
                        "top10_favourite_rate": round(top_hit, 3) if top_hit is not None else None})
    base_mae = float(np.mean(np.abs(ys - ys.mean())))
    best = min(results, key=lambda r: r["error"])
    return {"ok": True, "films": len(rows), "folds": folds, "guess_average_error": round(base_mae, 3),
            "favourite_rate": round(float((ys >= FAV).mean()), 3), "variants": results, "best": best["variant"],
            # films before release: treating the missing average as "average" vs scoring on your taste alone
            "pre_release": {"old": round(float(np.mean(np.abs(pre_old - ys))), 3),
                            "new": round(float(np.mean(np.abs(pre_new - ys))), 3)}}


def community_value(lb_avg: float | None, tmdb_meta: dict | None) -> float | None:
    """Prefer the Letterboxd average; fall back to TMDB's (roughly rescaled) once enough people have voted."""
    if lb_avg:
        return float(lb_avg)
    if tmdb_meta and (tmdb_meta.get("vote_count") or 0) >= 40 and tmdb_meta.get("vote_average"):
        return round(tmdb_meta["vote_average"] / 2 - 0.25, 2)
    return None


def stars(r: float | None) -> str:
    if r is None or (isinstance(r, float) and math.isnan(r)):
        return "?"
    half = round(r * 2) / 2
    return "★" * int(half) + ("½" if half % 1 else "")


def size_effect(rows: list[tuple[float, float, int]], min_films: int = 60) -> dict:
    """Do you rate big, much-talked-about films differently from the crowd?

    rows: (your rating, Letterboxd average, how many ratings it has). Films are split into thirds by how many
    people rated them; for each third, the average gap between your rating and the crowd's. The slope says how
    that gap moves for every 10x more ratings, with a rough 95% range."""
    rows = [r for r in rows if r[1] is not None and r[2] and r[2] >= 100]
    if len(rows) < min_films:
        return {"ok": False, "films": len(rows), "needed": min_films}
    yours = np.array([r[0] for r in rows], dtype=float)
    crowd = np.array([r[1] for r in rows], dtype=float)
    size = np.log10(np.array([r[2] for r in rows], dtype=float))
    gap = yours - crowd
    order = np.argsort(size)
    bands = []
    for name, idx in zip(("Smaller films", "Middle", "Biggest films"), np.array_split(order, 3)):
        g = gap[idx]
        counts = 10 ** size[idx]
        bands.append({"band": name, "films": int(len(idx)), "gap": round(float(g.mean()), 2),
                      "range": round(float(1.96 * g.std(ddof=1) / math.sqrt(len(idx))), 2),
                      "from": int(counts.min()), "to": int(counts.max())})
    x = size - size.mean()
    slope = float((x * (gap - gap.mean())).sum() / (x ** 2).sum())
    resid = gap - gap.mean() - slope * x
    se = float(math.sqrt((resid ** 2).sum() / (len(x) - 2) / (x ** 2).sum()))
    clear = abs(slope) > 1.96 * se
    return {"ok": True, "films": len(rows), "overall_gap": round(float(gap.mean()), 2), "bands": bands,
            "slope_per_10x": round(slope, 2), "slope_range": round(1.96 * se, 2), "lean_is_clear": bool(clear),
            "correlation": round(float(np.corrcoef(size, gap)[0, 1]), 2)}
