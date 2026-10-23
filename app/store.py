"""SQLite persistence: per-poll samples, key/value state, alert log."""
from __future__ import annotations

import json
import os
import sqlite3
import statistics
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    ts REAL NOT NULL, miner TEXT NOT NULL, ok INTEGER NOT NULL,
    hashrate REAL, hashrate_now REAL, temp REAL, vr_temp REAL,
    power REAL, accepted INTEGER, best_diff REAL
);
CREATE INDEX IF NOT EXISTS idx_samples_miner_ts ON samples(miner, ts);
CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(ts);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS alerts (
    ts REAL NOT NULL, miner TEXT, kind TEXT, message TEXT
);
-- pool watch (Braiins Solo, multi-user)
CREATE TABLE IF NOT EXISTS pw_subs (
    chat_id TEXT NOT NULL, address TEXT NOT NULL, created REAL NOT NULL,
    PRIMARY KEY (chat_id, address)
);
CREATE TABLE IF NOT EXISTS pw_samples (
    ts REAL NOT NULL, address TEXT NOT NULL, hashrate REAL,
    workers_online INTEGER, workers_total INTEGER, best REAL
);
CREATE INDEX IF NOT EXISTS idx_pw_samples_addr_ts ON pw_samples(address, ts);
"""


class Store:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    # ---------------- kv
    def get(self, key: str, default=None):
        with self._lock:
            row = self.db.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key: str, value) -> None:
        with self._lock:
            self.db.execute("INSERT INTO kv(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                            (key, json.dumps(value)))

    # ---------------- samples
    def add_sample(self, ts: float, r) -> None:
        with self._lock:
            self.db.execute(
                "INSERT INTO samples VALUES (?,?,?,?,?,?,?,?,?,?)",
                (ts, r.name, 1 if r.ok else 0, r.hashrate_ghs, r.hashrate_now_ghs, r.temp_c,
                 r.vr_temp_c, r.power_w, r.accepted, r.best_diff))

    def hashrate_median(self, miner: str, since: float) -> tuple[float | None, int]:
        with self._lock:
            rows = self.db.execute(
                "SELECT hashrate FROM samples WHERE miner=? AND ts>=? AND ok=1 AND hashrate>0",
                (miner, since)).fetchall()
        vals = [r[0] for r in rows]
        return (statistics.median(vals) if vals else None), len(vals)

    def window_stats(self, since: float, until: float | None = None) -> dict:
        """Aggregate per-miner stats over [since, until]."""
        until = until or time.time()
        with self._lock:
            rows = self.db.execute(
                """SELECT miner, COUNT(*), SUM(ok),
                          AVG(CASE WHEN ok=1 THEN hashrate END),
                          AVG(CASE WHEN ok=1 THEN COALESCE(hashrate,0) ELSE 0 END),
                          MAX(temp), MAX(vr_temp),
                          AVG(CASE WHEN ok=1 THEN power END),
                          SUM(CASE WHEN ok=1 AND power IS NOT NULL THEN 1 ELSE 0 END),
                          MAX(best_diff), MIN(ts), MAX(ts)
                   FROM samples WHERE ts>=? AND ts<=? GROUP BY miner""",
                (since, until)).fetchall()
        out = {}
        for (miner, n, n_ok, hr_online, hr_eff, tmax, vrmax, pw, n_pw, best, t0, t1) in rows:
            out[miner] = {
                "samples": n, "ok_samples": n_ok or 0,
                "uptime_pct": 100.0 * (n_ok or 0) / n if n else 0.0,
                "avg_hashrate_online": hr_online, "avg_hashrate_effective": hr_eff,
                "max_temp": tmax, "max_vr_temp": vrmax,
                "avg_power_online": pw, "power_samples": n_pw or 0,
                "best_diff": best, "first_ts": t0, "last_ts": t1,
            }
        return out

    def log_alert(self, ts: float, miner: str | None, kind: str, message: str) -> None:
        with self._lock:
            self.db.execute("INSERT INTO alerts VALUES (?,?,?,?)", (ts, miner, kind, message))

    def prune(self, older_than: float) -> None:
        with self._lock:
            self.db.execute("DELETE FROM samples WHERE ts<?", (older_than,))
            self.db.execute("DELETE FROM alerts WHERE ts<?", (older_than,))
            self.db.execute("DELETE FROM pw_samples WHERE ts<?", (older_than,))

    # ---------------- pool watch subscriptions
    def pw_add_sub(self, chat_id: str, address: str, ts: float) -> bool:
        with self._lock:
            cur = self.db.execute("INSERT OR IGNORE INTO pw_subs VALUES (?,?,?)",
                                  (str(chat_id), address, ts))
        return cur.rowcount > 0

    def pw_remove_sub(self, chat_id: str, address: str | None = None) -> int:
        with self._lock:
            if address is None:
                cur = self.db.execute("DELETE FROM pw_subs WHERE chat_id=?", (str(chat_id),))
            else:
                cur = self.db.execute("DELETE FROM pw_subs WHERE chat_id=? AND address=?",
                                      (str(chat_id), address))
        return cur.rowcount

    def pw_subs_for_chat(self, chat_id: str) -> list[str]:
        with self._lock:
            rows = self.db.execute("SELECT address FROM pw_subs WHERE chat_id=? ORDER BY created",
                                   (str(chat_id),)).fetchall()
        return [r[0] for r in rows]

    def pw_all_subs(self) -> list[tuple[str, str]]:
        """[(chat_id, address)] ordered by subscription time."""
        with self._lock:
            return [(r[0], r[1]) for r in
                    self.db.execute("SELECT chat_id, address FROM pw_subs ORDER BY created").fetchall()]

    def pw_add_sample(self, ts: float, address: str, hashrate, online: int, total: int, best) -> None:
        with self._lock:
            self.db.execute("INSERT INTO pw_samples VALUES (?,?,?,?,?,?)",
                            (ts, address, hashrate, online, total, best))

    def pw_window(self, address: str, since: float, until: float | None = None) -> dict:
        until = until or time.time()
        with self._lock:
            n, avg_hr, up, best = self.db.execute(
                """SELECT COUNT(*), AVG(hashrate),
                          AVG(CASE WHEN workers_total>0 THEN 1.0*workers_online/workers_total END),
                          MAX(best)
                   FROM pw_samples WHERE address=? AND ts>=? AND ts<=?""",
                (address, since, until)).fetchone()
        return {"samples": n or 0, "avg_hashrate": avg_hr,
                "uptime_pct": (up * 100.0) if up is not None else None, "best": best}
