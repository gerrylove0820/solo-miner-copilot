"""Formatting helpers (pure functions)."""
from __future__ import annotations


def fmt_hr(ghs: float | None) -> str:
    if ghs is None:
        return "n/a"
    v = float(ghs)
    for unit, div in (("PH/s", 1e6), ("TH/s", 1e3)):
        if abs(v) >= div:
            return f"{v / div:.2f} {unit}"
    return f"{v:.0f} GH/s" if abs(v) >= 100 else f"{v:.1f} GH/s"


def fmt_diff(d: float | None) -> str:
    if d is None:
        return "n/a"
    d = float(d)
    for unit, div in (("E", 1e18), ("P", 1e15), ("T", 1e12), ("G", 1e9), ("M", 1e6), ("K", 1e3)):
        if d >= div:
            return f"{d / div:.2f}{unit}"
    return f"{d:.0f}"


def fmt_temp(t: float | None) -> str:
    return "n/a" if t is None else f"{t:.0f}°C"


def fmt_dur(seconds: float | None) -> str:
    if seconds is None:
        return "n/a"
    s = int(max(0, seconds))
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m"
    return f"{s}s"


def fmt_power(w: float | None) -> str:
    return "n/a" if w is None else (f"{w / 1000:.2f} kW" if w >= 1000 else f"{w:.0f} W")


def fmt_odds(p: float) -> str:
    """Probability -> '0.0123% (1 in 8,130)'."""
    if p <= 0:
        return "0%"
    one_in = 1.0 / p
    pct = p * 100
    pct_s = f"{pct:.4f}%" if pct < 0.01 else (f"{pct:.3f}%" if pct < 1 else f"{pct:.1f}%")
    return f"{pct_s} (≈1 in {one_in:,.0f})"
