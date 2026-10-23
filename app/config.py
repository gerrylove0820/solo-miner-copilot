"""Configuration loading with safe defaults."""
from __future__ import annotations

import copy
import os

import yaml

DEFAULTS = {
    "poll_interval_seconds": 60,
    "timezone": "UTC",
    "daily_summary_time": "08:00",
    "request_timeout_seconds": 5,
    "telegram": {"owner_chat_id": None},
    "electricity": {"rate_usd_per_kwh": 0.15, "rate_is_default": True},
    "thresholds": {
        "offline_after_failed_polls": 3,
        "asic_temp_max_c": 70,
        "vr_temp_max_c": 85,
        "avalon_temp_max_c": 95,
        "braiins_temp_max_c": 90,
        "temp_recovery_margin_c": 5,
        "overheat_cooldown_minutes": 30,
        "hashrate_drop_pct": 75,
        "hashrate_drop_minutes": 10,
        "hashrate_baseline_hours": 6,
        "hashrate_baseline_min_minutes": 60,
        "hashrate_alert_cooldown_minutes": 60,
        "shares_stall_minutes": 20,
    },
    "network": {
        "enabled": True,
        "mempool_base_url": "https://mempool.space",
        "refresh_minutes": 60,
    },
    "pool": {
        "enabled": False,
        "name": "Local pool",
        "host": "",
        "port": 3333,
        "api_url": "",
    },
    "retention_days": 8,
    "miners": [],
    # Phone-only "pool watch" for friends (Braiins Solo, looked up by BTC address).
    # Off when the section is missing from config.yaml (backward compatible).
    "pool_watch": {
        "enabled": False,
        "base_url": "https://solo.braiins.com",
        "poll_seconds": 300,
        "request_spacing_seconds": 2,      # min gap between API requests (floor: 2 s)
        "request_timeout_seconds": 15,
        "max_users": 25,                   # global cap on watcher chats (owner not counted)
        "max_addresses_per_chat": 3,
        "offline_minutes": 20,             # no share for this long -> worker offline
        "offline_confirm_polls": 2,
        "drop_pct": 60,                    # below 60% of the worker's own 1d average...
        "drop_confirm_polls": 2,           # ...for 2 polls in a row
        "drop_compare_window": "1hr",      # pool hashrate window compared to 1d: 5m | 1hr
        "drop_grace_minutes": 120,         # no drop alerts this soon after (re)connect
        "recovery_pct": 80,                # "recovered" once back above this % of 1d avg
        "alert_cooldown_minutes": 60,      # same alert for the same worker at most hourly
        "best_diff_cooldown_minutes": 60,
        "api_down_owner_alert_minutes": 60,
        "daily_summary": True,             # 8 AM (daily_summary_time) report to each watcher
        "notify_owner_new_user": True,
    },
    # Braiins Farm Proxy (pool side). Off when the section is missing (backward compatible).
    "farm_proxy": {
        "enabled": False,
        "urls": [],                        # empty = autodetect (127.0.0.1:9090, prometheus:9090, ...)
        "poll_seconds": 60,
        "request_timeout_seconds": 4,
        "expected_connections": 0,         # 0 = do not check connection count
        "connection_confirm_polls": 2,
        "watch_workers": [],               # pool-only miners, e.g. [my-pool-only-miner] (name or IP)
        "worker_confirm_polls": 2,
        "unreachable_alert_minutes": 10,
        "upstream_min_pct": 50,            # upstream < 50% of downstream...
        "upstream_alert_minutes": 10,      # ...for 10 min -> node/pool problem
        "compare_window": "15m",
        "invalid_max_pct": 5,
        "invalid_window": "15m",
        "invalid_confirm_polls": 2,
        "alert_cooldown_minutes": 60,
        "stratum_host": None,              # default: pool.host (e.g. 192.168.1.10)
        "stratum_port": 3333,
    },
}

# type -> default port for the cgminer-style TCP API
CGMINER_PORT = 4028


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str | None = None) -> dict:
    path = path or os.environ.get("CONFIG_PATH", "config.yaml")
    with open(path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    cfg = _deep_merge(DEFAULTS, raw)
    miners = []
    seen = set()
    for i, m in enumerate(cfg.get("miners") or []):
        if not isinstance(m, dict) or not m.get("host"):
            continue
        m = dict(m)
        m["type"] = str(m.get("type", "axeos")).lower()
        m.setdefault("name", f"{m['type']}-{m['host']}")
        if m["name"] in seen:
            m["name"] = f"{m['name']}-{i}"
        seen.add(m["name"])
        if m.get("enabled", True) is False:
            continue
        miners.append(m)
    cfg["miners"] = miners
    pw = cfg.get("pool_watch")
    cfg["pool_watch"] = _deep_merge(DEFAULTS["pool_watch"], pw if isinstance(pw, dict) else {})
    pw = cfg["pool_watch"]
    pw["request_spacing_seconds"] = max(2.0, float(pw["request_spacing_seconds"]))
    pw["poll_seconds"] = max(60.0, float(pw["poll_seconds"]))
    if str(pw.get("drop_compare_window")) not in ("1m", "5m", "1hr"):
        pw["drop_compare_window"] = "1hr"
    fp = cfg.get("farm_proxy")
    cfg["farm_proxy"] = _deep_merge(DEFAULTS["farm_proxy"], fp if isinstance(fp, dict) else {})
    fp = cfg["farm_proxy"]
    if isinstance(fp.get("urls"), str):
        fp["urls"] = [fp["urls"]]
    fp["urls"] = [str(u).strip() for u in (fp.get("urls") or []) if str(u).strip()]
    if isinstance(fp.get("watch_workers"), str):
        fp["watch_workers"] = [fp["watch_workers"]]
    fp["watch_workers"] = [str(w).strip() for w in (fp.get("watch_workers") or []) if str(w).strip()]
    for k in ("compare_window", "invalid_window"):
        if str(fp.get(k)) not in ("5m", "10m", "15m", "30m", "1h"):
            fp[k] = "15m"
    if not fp.get("stratum_host"):
        fp["stratum_host"] = cfg["pool"].get("host")
    if cfg["pool"].get("enabled"):
        p = cfg["pool"]
        cfg["miners"].append({"name": p.get("name", "Local pool"), "type": "stratum",
                              "host": p["host"], "port": int(p.get("port", 3333)),
                              "api_url": p.get("api_url") or ""})
    return cfg


def miner_thresholds(cfg: dict, miner: dict) -> dict:
    """Return effective thresholds for one miner (global + per-type + per-miner overrides)."""
    t = dict(cfg["thresholds"])
    typ = miner.get("type")
    if typ == "avalon":
        temp_max = t["avalon_temp_max_c"]
    elif typ == "braiins":
        temp_max = t["braiins_temp_max_c"]
    else:
        temp_max = t["asic_temp_max_c"]
    t["temp_max_c"] = float(miner.get("temp_max_c", temp_max))
    t["vr_temp_max_c"] = float(miner.get("vr_temp_max_c", t["vr_temp_max_c"]))
    return t
