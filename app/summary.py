"""Status snapshot, daily summary and solo-block odds."""
from __future__ import annotations

import logging
import math
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .collectors import http_get_json
from .format import fmt_diff, fmt_dur, fmt_hr, fmt_odds, fmt_power, fmt_temp

log = logging.getLogger("summary")

TWO32 = 2 ** 32


# ------------------------------------------------------------------ scheduling
def daily_slot_due(cfg: dict, store, key: str, now: float) -> bool:
    """True once per local day at/after cfg daily_summary_time (state in kv `key`).
    First run ever never fires immediately if today's slot already passed."""
    tz = ZoneInfo(cfg["timezone"])
    local = datetime.fromtimestamp(now, tz)
    hh, mm = (int(x) for x in str(cfg["daily_summary_time"]).split(":"))
    target = local.replace(hour=hh, minute=mm, second=0, microsecond=0)
    today = local.date().isoformat()
    last = store.get(key)
    if last is None:
        store.set(key, today if local >= target else (local.date() - timedelta(days=1)).isoformat())
        return False
    if local >= target and last != today:
        store.set(key, today)
        return True
    return False


# ------------------------------------------------------------------ odds
def expected_blocks_per_day(hashrate_ghs: float, difficulty: float) -> float:
    """Expected blocks/day = H * 86400 / (D * 2^32)."""
    if not hashrate_ghs or not difficulty:
        return 0.0
    return hashrate_ghs * 1e9 * 86400 / (difficulty * TWO32)


def solo_odds(hashrate_ghs: float, difficulty: float) -> dict:
    lam = expected_blocks_per_day(hashrate_ghs, difficulty)
    return {
        "per_day": 1 - math.exp(-lam),
        "per_year": 1 - math.exp(-lam * 365.25),
        "expected_days": (1 / lam) if lam > 0 else None,
    }


# ------------------------------------------------------------------ network difficulty
def get_network_difficulty(cfg: dict, store, readings=None, now: float | None = None) -> tuple[float | None, str]:
    """Return (difficulty, source). Order: fresh cache -> mempool.space -> AxeOS-reported -> stale cache."""
    now = now or time.time()
    net = cfg["network"]
    cache = store.get("network_difficulty") or {}
    if cache.get("value") and now - cache.get("ts", 0) < float(net["refresh_minutes"]) * 60:
        return cache["value"], cache.get("source", "cache")
    if net.get("enabled", True):
        base = net["mempool_base_url"].rstrip("/")
        try:
            d = http_get_json(f"{base}/api/v1/mining/hashrate/3d", 10)
            val = float(d.get("currentDifficulty") or 0) or None
            if not val and d.get("difficulty"):
                val = float(d["difficulty"][-1]["difficulty"])
            if val:
                store.set("network_difficulty", {"value": val, "ts": now, "source": "mempool.space"})
                return val, "mempool.space"
        except Exception as e:  # noqa: BLE001
            log.warning("mempool.space difficulty fetch failed: %s", e)
    for r in readings or []:
        nd = (r.extra or {}).get("network_difficulty")
        if nd:
            store.set("network_difficulty", {"value": nd, "ts": now, "source": f"reported by {r.name}"})
            return nd, f"reported by {r.name}"
    if cache.get("value"):
        age = fmt_dur(now - cache.get("ts", now))
        return cache["value"], f"cached {age} old"
    return None, "unavailable"


def _odds_lines(hr_ghs: float, diff: float | None, src: str) -> list[str]:
    if not diff:
        return ["🎲 Solo odds: network difficulty unavailable right now."]
    o = solo_odds(hr_ghs, diff)
    exp_days = o["expected_days"]
    exp = f"{exp_days / 365.25:,.0f} years" if exp_days and exp_days > 365 else (
        f"{exp_days:,.0f} days" if exp_days else "n/a")
    return [
        f"🎲 Solo block odds at {fmt_hr(hr_ghs)} (network diff {fmt_diff(diff)}, {src}):",
        f"   per day:  {fmt_odds(o['per_day'])}",
        f"   per year: {fmt_odds(o['per_year'])}",
        f"   expected time to a block: ~{exp}",
    ]


# ------------------------------------------------------------------ status
def build_status(readings, cfg: dict, store, now: float | None = None, farm=None) -> str:
    now = now or time.time()
    tz = ZoneInfo(cfg["timezone"])
    if not readings:
        return "No data yet - first poll still running. Try again in a minute."
    miners = [r for r in readings if r.kind != "stratum"]
    pools = [r for r in readings if r.kind == "stratum"]
    online = [r for r in miners if r.ok]
    total_hr = sum(r.hashrate_ghs or 0 for r in online)
    total_pw = sum(r.power_w or 0 for r in online)
    pw_known = sum(1 for r in online if r.power_w)
    lines = [f"⛏️ Fleet status - {datetime.fromtimestamp(now, tz):%a %b %d %I:%M %p %Z}",
             f"Online {len(online)}/{len(miners)} | {fmt_hr(total_hr)} | "
             f"{fmt_power(total_pw)} ({pw_known} miners report power)", ""]
    for r in sorted(miners, key=lambda x: (not x.ok, x.name)):
        if not r.ok:
            lines.append(f"🔴 {r.name}: OFFLINE")
            continue
        if not r.stats_available:
            lines.append(f"🟡 {r.name}: up, no stats ({r.note})")
            continue
        parts = [fmt_hr(r.hashrate_ghs)]
        if r.expected_ghs and r.hashrate_ghs is not None:
            parts[0] += f" ({100 * r.hashrate_ghs / r.expected_ghs:.0f}%)"
        t = fmt_temp(r.temp_c)
        if r.vr_temp_c is not None:
            t += f"/VR {fmt_temp(r.vr_temp_c)}"
        parts.append(t)
        if r.power_w:
            parts.append(fmt_power(r.power_w))
        if r.best_diff:
            parts.append(f"best {fmt_diff(r.best_diff)}")
        if r.uptime_s:
            parts.append(f"up {fmt_dur(r.uptime_s)}")
        note = f" [{r.note}]" if r.note else ""
        lines.append(f"🟢 {r.name}: " + " | ".join(parts) + note)
    for p in pools:
        extra = ""
        if p.extra.get("workers") is not None:
            extra = f" | workers {p.extra['workers']}"
        lines.append(f"{'🟢' if p.ok else '🔴'} {p.name} ({p.host}): {'up' if p.ok else 'DOWN'}{extra}")
    if farm is not None:
        lines += farm.status_lines()
    diff, src = get_network_difficulty(cfg, store, readings, now)
    lines += [""] + _odds_lines(total_hr, diff, src)
    return "\n".join(lines)


# ------------------------------------------------------------------ daily summary
def build_daily_summary(cfg: dict, store, readings=None, now: float | None = None,
                        hours: float = 24, farm=None) -> str:
    now = now or time.time()
    tz = ZoneInfo(cfg["timezone"])
    since = now - hours * 3600
    stats = store.window_stats(since, now)
    names = [m["name"] for m in cfg["miners"] if m["type"] != "stratum"]
    rate = float(cfg["electricity"]["rate_usd_per_kwh"])
    rate_lbl = " (DEFAULT rate - set yours in config.yaml)" if cfg["electricity"].get("rate_is_default", True) else ""
    lines = [f"☀️ Daily mining summary - {datetime.fromtimestamp(now, tz):%a %b %d, %Y}",
             f"Window: last {hours:.0f}h", ""]
    fleet_hr = 0.0
    fleet_kwh = 0.0
    best_overall = None
    missing_power = []
    covered_hours = 0.0
    for name in names:
        s = stats.get(name)
        if not s:
            lines.append(f"• {name}: no data")
            continue
        span_h = max(0.0, min(hours, ((s["last_ts"] - s["first_ts"]) / 3600) + cfg["poll_interval_seconds"] / 3600))
        covered_hours = max(covered_hours, span_h)
        fleet_hr += s["avg_hashrate_effective"] or 0
        kwh = None
        if s["avg_power_online"] and s["power_samples"]:
            # energy = avg power while online * online time
            kwh = s["avg_power_online"] * span_h * (s["uptime_pct"] / 100) / 1000
            fleet_kwh += kwh
        else:
            missing_power.append(name)
        if s["best_diff"] and (best_overall is None or s["best_diff"] > best_overall[1]):
            best_overall = (name, s["best_diff"])
        temp = fmt_temp(s["max_temp"])
        if s["max_vr_temp"] is not None:
            temp += f"/VR {fmt_temp(s['max_vr_temp'])}"
        kwh_s = f"{kwh:.2f} kWh" if kwh is not None else "power n/a"
        lines.append(f"• {name}: avg {fmt_hr(s['avg_hashrate_online'])} | uptime {s['uptime_pct']:.0f}% | "
                     f"max {temp} | {kwh_s} | best {fmt_diff(s['best_diff'])}")
    cost = fleet_kwh * rate
    lines += ["",
              f"Fleet avg hashrate: {fmt_hr(fleet_hr)}",
              f"Energy: {fleet_kwh:.2f} kWh over {covered_hours:.1f}h ≈ ${cost:.2f} at ${rate:.3f}/kWh{rate_lbl}"]
    if covered_hours:
        lines.append(f"Avg fleet power (known): {fmt_power(fleet_kwh / covered_hours * 1000)}")
    if missing_power:
        lines.append(f"(no power data for: {', '.join(missing_power)} - not included in kWh)")
    if best_overall:
        lines.append(f"Best difficulty (all-time as reported): {fmt_diff(best_overall[1])} by {best_overall[0]}")
    if farm is not None:
        lines += farm.daily_lines()
    diff, src = get_network_difficulty(cfg, store, readings, now)
    lines += [""] + _odds_lines(fleet_hr, diff, src)
    return "\n".join(lines)
