"""Alert engine: turns successive readings into deduplicated alerts.

Per-miner state is persisted in SQLite (kv 'state:<miner>') so restarts don't
re-send alerts that were already delivered.
"""
from __future__ import annotations

import logging
import time
from typing import Callable

from .config import miner_thresholds
from .format import fmt_dur, fmt_hr, fmt_diff, fmt_temp

log = logging.getLogger("engine")


class Monitor:
    def __init__(self, cfg: dict, store, notify: Callable[[str, str, str | None], None]):
        self.cfg = cfg
        self.store = store
        self.notify = notify
        self.miners = {m["name"]: m for m in cfg["miners"]}
        self.states: dict[str, dict] = {}

    # ------------------------------------------------------------ state
    def state(self, name: str) -> dict:
        if name not in self.states:
            self.states[name] = self.store.get(f"state:{name}", {}) or {}
        return self.states[name]

    def save(self, name: str) -> None:
        self.store.set(f"state:{name}", self.states[name])

    def _send(self, now: float, miner: str | None, kind: str, text: str) -> None:
        log.info("ALERT [%s] %s: %s", kind, miner, text.replace("\n", " | "))
        self.store.log_alert(now, miner, kind, text)
        self.notify(text, kind, miner)

    # ------------------------------------------------------------ main
    def process(self, readings, now: float | None = None) -> None:
        now = time.time() if now is None else now
        for r in readings:
            self.store.add_sample(now, r)
        for r in readings:
            m = self.miners.get(r.name, {"name": r.name, "type": r.kind, "host": r.host})
            st = self.state(r.name)
            try:
                self._offline(r, m, st, now)
                if r.ok and r.stats_available:
                    self._temps(r, m, st, now)
                    self._hashrate(r, m, st, now)
                    self._shares(r, m, st, now)
                    self._best(r, m, st, now)
                elif not r.ok:
                    st.pop("drop_since", None)
            except Exception:  # noqa: BLE001
                log.exception("alert evaluation failed for %s", r.name)
            self.save(r.name)

    # ------------------------------------------------------------ offline
    def _offline(self, r, m, st, now):
        need = int(self.cfg["thresholds"]["offline_after_failed_polls"])
        label = f"{r.name} ({r.host})"
        if r.ok:
            if st.get("offline_alerted"):
                down = now - st.get("offline_since", now)
                self._send(now, r.name, "recovered",
                           f"✅ BACK ONLINE: {label} is responding again (down ~{fmt_dur(down)}).")
            st["fail_count"] = 0
            st["offline_alerted"] = False
            st.pop("offline_since", None)
            st["last_ok"] = now
            return
        st["fail_count"] = int(st.get("fail_count", 0)) + 1
        if st["fail_count"] == 1:
            st["offline_since"] = now
        if st["fail_count"] >= need and not st.get("offline_alerted"):
            last = st.get("last_ok")
            seen = f" Last seen {fmt_dur(now - last)} ago." if last else ""
            what = "Pool port is not accepting connections" if r.kind == "stratum" else "Not responding"
            self._send(now, r.name, "offline",
                       f"🔴 OFFLINE: {label}\n{what} for {st['fail_count']} polls in a row.{seen}\n"
                       f"Error: {r.error or 'unknown'}")
            st["offline_alerted"] = True

    # ------------------------------------------------------------ temps
    def _temps(self, r, m, st, now):
        th = miner_thresholds(self.cfg, m)
        cooldown = float(th["overheat_cooldown_minutes"]) * 60
        margin = float(th["temp_recovery_margin_c"])
        checks = [("asic", r.temp_c, th["temp_max_c"], "ASIC/chip temp")]
        if r.vr_temp_c is not None:
            checks.append(("vr", r.vr_temp_c, th["vr_temp_max_c"], "VR (voltage regulator) temp"))
        for key, val, limit, label in checks:
            if val is None:
                continue
            k_last, k_hot = f"hot_{key}_last_alert", f"hot_{key}_active"
            if val > limit:
                if now - float(st.get(k_last, 0)) >= cooldown:
                    again = " (still hot)" if st.get(k_hot) else ""
                    self._send(now, r.name, f"overheat_{key}",
                               f"🔥 OVERHEAT{again}: {r.name} ({r.host})\n{label} {fmt_temp(val)} "
                               f"> limit {fmt_temp(limit)}. Check airflow / fan ({_fan(r)}).")
                    st[k_last] = now
                st[k_hot] = True
            elif st.get(k_hot) and val <= limit - margin:
                self._send(now, r.name, f"cooled_{key}",
                           f"🧊 Temp OK again: {r.name} {label} {fmt_temp(val)}.")
                st[k_hot] = False

    # ------------------------------------------------------------ hashrate drop
    def _hashrate(self, r, m, st, now):
        th = self.cfg["thresholds"]
        cur = r.hashrate_now_ghs if r.hashrate_now_ghs is not None else r.hashrate_ghs
        if cur is None:
            return
        poll = float(self.cfg["poll_interval_seconds"])
        since = now - float(th["hashrate_baseline_hours"]) * 3600
        median, n = self.store.hashrate_median(r.name, since)
        min_n = max(3, int(float(th["hashrate_baseline_min_minutes"]) * 60 / poll * 0.8))
        if not median or n < min_n:
            return
        limit = median * float(th["hashrate_drop_pct"]) / 100.0
        hold = float(th["hashrate_drop_minutes"]) * 60
        cooldown = float(th["hashrate_alert_cooldown_minutes"]) * 60
        if cur < limit:
            st.setdefault("drop_since", now)
            if (not st.get("drop_alerted") and now - st["drop_since"] >= hold - poll / 2
                    and now - float(st.get("drop_last_alert", 0)) >= cooldown):
                pct = 100.0 * cur / median if median else 0
                self._send(now, r.name, "hashrate_drop",
                           f"📉 HASHRATE DROP: {r.name} ({r.host})\nNow {fmt_hr(cur)} = {pct:.0f}% of its "
                           f"6h median {fmt_hr(median)} for {fmt_dur(now - st['drop_since'])}.")
                st["drop_alerted"] = True
                st["drop_last_alert"] = now
        else:
            if st.get("drop_alerted"):
                self._send(now, r.name, "hashrate_ok",
                           f"📈 Hashrate recovered: {r.name} at {fmt_hr(cur)} (median {fmt_hr(median)}).")
            st.pop("drop_since", None)
            st["drop_alerted"] = False

    # ------------------------------------------------------------ shares stalled
    def _shares(self, r, m, st, now):
        if r.accepted is None:
            return
        last = st.get("last_accepted")
        if last is None or r.accepted != last:
            if last is not None and r.accepted > last and st.get("stall_alerted"):
                self._send(now, r.name, "shares_ok",
                           f"✅ Shares flowing again: {r.name} (accepted {r.accepted}).")
            st["last_accepted"] = r.accepted
            st["accepted_changed_at"] = now
            st["stall_alerted"] = False
            return
        stall = float(self.cfg["thresholds"]["shares_stall_minutes"]) * 60
        hashing = (r.hashrate_ghs or 0) > 0 or (r.hashrate_now_ghs or 0) > 0
        if hashing and not st.get("stall_alerted") and now - float(st.get("accepted_changed_at", now)) >= stall:
            self._send(now, r.name, "shares_stalled",
                       f"⚠️ HASHING BUT NOT SUBMITTING: {r.name} ({r.host})\nAccepted shares stuck at "
                       f"{r.accepted} for {fmt_dur(now - st['accepted_changed_at'])} while hashing at "
                       f"{fmt_hr(r.hashrate_ghs)}. Check pool connection / stratum settings.")
            st["stall_alerted"] = True

    # ------------------------------------------------------------ best diff
    def _best(self, r, m, st, now):
        if not r.best_diff:
            return
        rec = st.get("best_diff_record")
        if rec is None:
            st["best_diff_record"] = r.best_diff
            return
        if r.best_diff > rec * 1.000001:
            self._send(now, r.name, "best_diff",
                       f"🏆 NEW BEST DIFFICULTY: {r.name} hit {fmt_diff(r.best_diff)} "
                       f"(previous record {fmt_diff(rec)}). Keep swinging!")
            st["best_diff_record"] = r.best_diff


def _fan(r) -> str:
    return f"{r.fan_rpm:.0f} rpm" if r.fan_rpm is not None else "fan n/a"
