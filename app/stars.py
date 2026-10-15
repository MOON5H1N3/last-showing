"""Ratings as you'd give them on Letterboxd: half-stars from ½★ to 5★.

The model predicts on a smooth scale (it learns better that way), but a prediction is shown as the half-star you'd
most likely give, e.g. 3.9 -> "4★" and 3.6 -> "3½★". When it sits close to the line between two half-stars, the
other one is offered too ("could be 3½★").
"""
from __future__ import annotations

NEAR = 0.1  # within this of the line between two half-stars, either is about as likely


def _clamp(v: float) -> float:
    return min(5.0, max(0.5, v))


def half(v: float | None) -> float | None:
    """The nearest half-star (0.5 to 5)."""
    import math
    return None if v is None else _clamp(math.floor(float(v) * 2 + 0.5) / 2)  # 3.25 -> 3.5, never "round half to even"


def label(v: float | None) -> str:
    """3.9 -> '4★', 3.6 -> '3½★', 0.6 -> '½★'."""
    h = half(v)
    if h is None:
        return ""
    whole = int(h)
    if h - whole:
        return f"{whole}½★" if whole else "½★"
    return f"{whole}★"


def other(v: float | None) -> str:
    """The neighbouring half-star when the prediction is near the line between two, else ''."""
    if v is None:
        return ""
    x = _clamp(float(v)) * 2
    frac = x - int(x)                     # position between two half-stars, 0..1
    if abs(frac - 0.5) > NEAR * 2:        # comfortably inside one half-star
        return ""
    h = half(v)
    alt = h - 0.5 if float(v) < h else h + 0.5
    return label(alt) if 0.5 <= alt <= 5 and alt != h else ""
