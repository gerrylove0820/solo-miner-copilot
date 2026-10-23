"""Braiins Farm Proxy collector + alerts (pool side of the farm).

Some miners point at a Braiins Farm Proxy on your node. Some devices have no local
API, so the proxy is the only place to see them.

Data sources (autodetected, first that answers with FP_ metrics wins):
  * Prometheus HTTP API  GET <url>/api/v1/query?query=...   (stock stack: port 9090)
  * raw exporter         GET http://farm-proxy:8080/metrics  (only inside the proxy's
                         docker network - not published on the host by the stock compose)
Metric names (braiins/farm-proxy Grafana dashboards, farm-proxy 26.02 binary):
  FP_subscription_size_to_target_total{goal,target}  downstream (miner) connections;
      goal="#!!ii-DevFee" = dev-fee connection
  FP_connections_by_target{goal,target}               upstream (pool) connections
  FP_accepted_shares / FP_rejected_shares{direction="down"|"up",category,target,reason}
      counters of share *difficulty*; hashrate GH/s = rate() * 4.294967296 (2^32/1e9)
  newer names: (FP_)sr_dn_/sr_up_accepted|rejected_shares_total (old ones marked OBSOLETE)
  FP_target_status{target}
Per-worker labels were NOT found in the dashboards; if any label value contains a
watched worker name (or IP) it is used, otherwise only the connection count can tell
that a pool-only miner dropped.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

from . import __version__
from .collectors import num, tcp_port_open
from .format import fmt_dur, fmt_hr

log = logging.getLogger("farmproxy")

H = 4.294967296          # share-difficulty/s -> GH/s
DEVFEE_GOAL = "#!!ii-DevFee"
DEFAULT_URLS = ["http://127.0.0.1:9090", "http://localhost:9090", "http://prometheus:9090",
                "http://farm-proxy:8080/metrics", "http://127.0.0.1:8080/metrics"]
NOT_WORKER_LABELS = {"__name__", "job", "instance", "direction", "category", "target", "goal",
                     "reason", "type", "side", "le", "quantile", "version", "version_full",
                     "status", "operation", "queue", "event", "routing_domain", "contract_name",
                     "license_state", "subscription_type", "failure_type", "stratum_version",
                     "result", "state", "decision", "site_id", "subnet_id", "host_id"}

# metric name regexes (old FP_* names first = Grafana parity, then new sr_* names)
M_CONN_DN = "(FP_)?subscription_size_to_target_total"
M_CONN_UP = "(FP_)?connections_by_target"
M_TARGET = "(FP_)?target_status"
M_ACC = {"down": ("FP_accepted_shares", "(FP_)?sr_dn_accepted_shares_total"),
         "up": ("FP_accepted_shares", "(FP_)?sr_up_accepted_shares_total")}
M_REJ = {"down": ("FP_rejected_shares", "(FP_)?sr_dn_rejected_shares_total"),
         "up": ("FP_rejected_shares", "(FP_)?sr_up_rejected_shares_total")}


def _fetch(url: str, timeout: float) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": f"solo-miner-copilot/{__version__}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(8_000_000)
    except urllib.error.HTTPError as e:
        return e.code, b""


# ----------------------------------------------------------------- prometheus text format
_LINE = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{(.*)\})?\s+(\S+)(\s+\S+)?\s*$')
_LBL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"')


def parse_prom_text(text: str) -> list[tuple[str, dict, float]]:
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _LINE.match(line)
        if not m:
            continue
        v = num(m.group(4))
        if v is None:
            continue
        labels = {k: val.replace('\\"', '"').replace("\\\\", "\\") for k, val in _LBL.findall(m.group(3) or "")}
        out.append((m.group(1), labels, v))
    return out


def _name_match(pattern: str, name: str) -> bool:
    return re.fullmatch(pattern, name) is not None


# ----------------------------------------------------------------- snapshot
@dataclass
class ProxySnapshot:
    ok: bool = False
    ts: float = 0.0
    source: str | None = None
    mode: str | None = None          # prometheus | metrics
    error: str | None = None
    dn_conn: float | None = None     # standard downstream (miner) connections
    dn_conn_devfee: float | None = None
    up_conn: float | None = None
    dn_hr: float | None = None       # GH/s, 5m
    dn_hr_1h: float | None = None
    dn_hr_24h: float | None = None
    up_hr: float | None = None
    up_hr_1h: float | None = None
    up_hr_24h: float | None = None
    dn_cmp: float | None = None      # comparison window (default 15m) for up vs down
    up_cmp: float | None = None
    dn_invalid_pct: float | None = None   # invalid window (default 15m)
    up_invalid_pct: float | None = None
    dn_invalid_24h: float | None = None
    up_invalid_24h: float | None = None
    hr_24h_span_h: float | None = None    # metrics mode: how much history the 24h numbers cover
    targets: dict = field(default_factory=dict)      # target -> status value
    per_worker: bool = False
    workers: dict = field(default_factory=dict)      # watched name -> {"present", "hr", "label"}


def _pct(rej, acc):
    if rej is None or acc is None or acc + rej <= 0:
        return None
    return 100.0 * rej / (acc + rej)


# ----------------------------------------------------------------- sources
class PromSource:
    mode = "prometheus"

    def __init__(self, url: str, timeout: float, fetch=_fetch):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.fetch = fetch
        self.worker_label: dict[str, str] = {}   # watched -> label name
        self._labels_checked = 0.0

    def _get(self, path: str, params: dict | None = None):
        q = ("?" + urllib.parse.urlencode(params)) if params else ""
        code, body = self.fetch(f"{self.url}{path}{q}", self.timeout)
        if code != 200:
            raise RuntimeError(f"HTTP {code}")
        d = json.loads(body.decode("utf-8", errors="replace"))
        if d.get("status") != "success":
            raise RuntimeError(f"prometheus error: {d.get('error')}")
        return d["data"]

    def query(self, expr: str) -> float | None:
        data = self._get("/api/v1/query", {"query": expr})
        res = data.get("result") or []
        if data.get("resultType") == "scalar":
            return num(res[1]) if res else None
        if not res:
            return None
        return sum(v for v in (num(r["value"][1]) for r in res) if v is not None)

    def query_by(self, expr: str, label: str) -> dict:
        data = self._get("/api/v1/query", {"query": expr})
        return {r["metric"].get(label, "?"): num(r["value"][1]) for r in data.get("result") or []}

    def probe(self) -> bool:
        return (self.query('count({__name__=~"FP_.+"})') or 0) > 0

    @staticmethod
    def _rate(names: tuple[str, str], direction: str, win: str, extra: str = "") -> str:
        old, new = names
        return (f'(sum(rate({old}{{direction="{direction}"{extra}}}[{win}])) or '
                f'sum(rate({{__name__=~"{new}"{extra}}}[{win}])))')

    def _discover_worker_labels(self, watch: list[str], now: float) -> None:
        if now - self._labels_checked < 3600 and self._labels_checked:
            return
        self._labels_checked = now
        try:
            names = [n for n in self._get("/api/v1/labels", {"match[]": '{__name__=~"FP_.+"}'})
                     if n not in NOT_WORKER_LABELS][:20]
        except Exception as e:  # noqa: BLE001
            log.debug("label discovery failed: %s", e)
            return
        for name in names:
            try:
                values = self._get(f"/api/v1/label/{name}/values", {"match[]": '{__name__=~"FP_.+"}'})
            except Exception:  # noqa: BLE001
                continue
            for w in watch:
                if any(w.lower() in str(v).lower() for v in values) and w not in self.worker_label:
                    self.worker_label[w] = name
                    log.info("farm proxy: per-worker data for '%s' found in label '%s'", w, name)

    def snapshot(self, cfg: dict, now: float) -> ProxySnapshot:
        s = ProxySnapshot(ok=True, ts=now, source=self.url, mode=self.mode)
        cw, iw = cfg["compare_window"], cfg["invalid_window"]
        dev = f'goal="{DEVFEE_GOAL}"'
        std = f'goal!="{DEVFEE_GOAL}"'
        s.dn_conn = self.query(f'sum({{__name__=~"{M_CONN_DN}",{std}}})')
        s.dn_conn_devfee = self.query(f'sum({{__name__=~"{M_CONN_DN}",{dev}}})')
        s.up_conn = self.query(f'sum({{__name__=~"{M_CONN_UP}",{std}}})')
        for attr, d, w in (("dn_hr", "down", "5m"), ("dn_hr_1h", "down", "1h"), ("dn_hr_24h", "down", "24h"),
                           ("up_hr", "up", "5m"), ("up_hr_1h", "up", "1h"), ("up_hr_24h", "up", "24h"),
                           ("dn_cmp", "down", cw), ("up_cmp", "up", cw)):
            v = self.query(f"{self._rate(M_ACC[d], d, w)} * {H}")
            setattr(s, attr, v)
        for d, attr, w in (("down", "dn_invalid_pct", iw), ("up", "up_invalid_pct", iw),
                           ("down", "dn_invalid_24h", "24h"), ("up", "up_invalid_24h", "24h")):
            acc = self.query(self._rate(M_ACC[d], d, w))
            rej = self.query(self._rate(M_REJ[d], d, w))
            setattr(s, attr, _pct(rej or 0.0, acc) if acc is not None else None)
        try:
            s.targets = self.query_by(f'sum by (target) ({{__name__=~"{M_TARGET}"}})', "target")
        except Exception:  # noqa: BLE001
            s.targets = {}
        watch = list(cfg.get("watch_workers") or [])
        if watch:
            self._discover_worker_labels(watch, now)
            for w in watch:
                lbl = self.worker_label.get(w)
                if not lbl:
                    continue
                s.per_worker = True
                rx = re.sub(r"([.\\+*?\[^\]$(){}=!<>|:-])", r"\\\\\1", w)
                sel = f'{lbl}=~"(?i).*{rx}.*"'
                present = (self.query(f'count({{__name__=~"FP_.+",{sel}}})') or 0) > 0
                hr = self.query(f"{self._rate(M_ACC['down'], 'down', '5m', ',' + sel)} * {H}")
                s.workers[w] = {"present": present, "hr": hr, "label": lbl}
        return s


class MetricsSource:
    """Raw /metrics: gauges read directly; hashrates from our own counter deltas."""
    mode = "metrics"

    def __init__(self, url: str, timeout: float, fetch=_fetch):
        self.url = url
        self.timeout = timeout
        self.fetch = fetch
        self.hist: list[tuple[float, dict]] = []    # (ts, counters)

    def _series(self):
        code, body = self.fetch(self.url, self.timeout)
        if code != 200:
            raise RuntimeError(f"HTTP {code}")
        return parse_prom_text(body.decode("utf-8", errors="replace"))

    def probe(self) -> bool:
        return any(n.startswith("FP_") for n, _, _ in self._series())

    @staticmethod
    def _counter(series, names: tuple[str, str], direction: str, pred=None) -> float | None:
        old, new = names
        vals = [v for n, l, v in series if n == old and l.get("direction") == direction and (pred is None or pred(l))]
        if not vals:
            vals = [v for n, l, v in series if _name_match(new, n) and (pred is None or pred(l))]
        return sum(vals) if vals else None

    def _rate(self, key: str, window: float, now: float) -> tuple[float | None, float]:
        cur = self.hist[-1][1].get(key) if self.hist else None
        if cur is None:
            return None, 0.0
        old = None
        for ts, c in self.hist:
            if ts >= now - window - 1 and c.get(key) is not None:
                old = (ts, c[key])
                break
        if old is None or now - old[0] < min(window, 120) * 0.5:
            return None, 0.0
        dt = now - old[0]
        if dt <= 0 or cur < old[1]:   # counter reset (proxy restart)
            return None, dt
        return (cur - old[1]) / dt, dt

    def snapshot(self, cfg: dict, now: float) -> ProxySnapshot:
        series = self._series()
        s = ProxySnapshot(ok=True, ts=now, source=self.url, mode=self.mode)
        conn = [(l, v) for n, l, v in series if _name_match(M_CONN_DN, n)]
        if conn:
            s.dn_conn = sum(v for l, v in conn if l.get("goal") != DEVFEE_GOAL)
            s.dn_conn_devfee = sum(v for l, v in conn if l.get("goal") == DEVFEE_GOAL)
        up = [(l, v) for n, l, v in series if _name_match(M_CONN_UP, n)]
        if up:
            s.up_conn = sum(v for l, v in up if l.get("goal") != DEVFEE_GOAL)
        for n, l, v in series:
            if _name_match(M_TARGET, n):
                s.targets[l.get("target", "?")] = s.targets.get(l.get("target", "?"), 0) + v
        counters = {"dn_acc": self._counter(series, M_ACC["down"], "down"),
                    "dn_rej": self._counter(series, M_REJ["down"], "down"),
                    "up_acc": self._counter(series, M_ACC["up"], "up"),
                    "up_rej": self._counter(series, M_REJ["up"], "up")}
        watch = list(cfg.get("watch_workers") or [])
        for w in watch:
            lw = w.lower()
            hit = [(n, l) for n, l, _ in series
                   if any(lw in str(val).lower() for k, val in l.items() if k not in NOT_WORKER_LABELS)]
            if hit:
                s.per_worker = True
                lbl = next(k for k, val in hit[0][1].items() if lw in str(val).lower())
                counters[f"w:{w}"] = self._counter(
                    series, M_ACC["down"], "down",
                    pred=lambda l, lw=lw: any(lw in str(val).lower() for val in l.values()))
                s.workers[w] = {"present": True, "hr": None, "label": lbl}
        self.hist.append((now, counters))
        self.hist = [(t, c) for t, c in self.hist if t >= now - 86400 - 600]
        r = lambda k, w: self._rate(k, w, now)[0]  # noqa: E731
        win = {"5m": 300, "15m": 900, "10m": 600, "30m": 1800, "1h": 3600}
        cw, iw = win.get(cfg["compare_window"], 900), win.get(cfg["invalid_window"], 900)
        for attr, k, w in (("dn_hr", "dn_acc", 300), ("dn_hr_1h", "dn_acc", 3600),
                           ("up_hr", "up_acc", 300), ("up_hr_1h", "up_acc", 3600),
                           ("dn_cmp", "dn_acc", cw), ("up_cmp", "up_acc", cw)):
            v = r(k, w)
            setattr(s, attr, v * H if v is not None else None)
        d24, span = self._rate("dn_acc", 86400, now)
        u24, _ = self._rate("up_acc", 86400, now)
        s.dn_hr_24h = d24 * H if d24 is not None else None
        s.up_hr_24h = u24 * H if u24 is not None else None
        s.hr_24h_span_h = span / 3600 if span else None
        for attr, a, b, w in (("dn_invalid_pct", "dn_acc", "dn_rej", iw), ("up_invalid_pct", "up_acc", "up_rej", iw),
                              ("dn_invalid_24h", "dn_acc", "dn_rej", 86400), ("up_invalid_24h", "up_acc", "up_rej", 86400)):
            acc = r(a, w)
            rej = r(b, w)
            setattr(s, attr, _pct(rej or 0.0, acc) if acc is not None else None)
        for w in s.workers:
            v = r(f"w:{w}", 300)
            s.workers[w]["hr"] = v * H if v is not None else None
        return s


def make_source(url: str, timeout: float, fetch=_fetch):
    return MetricsSource(url, timeout, fetch) if url.rstrip("/").endswith("/metrics") else \
        PromSource(url, timeout, fetch)


# ----------------------------------------------------------------- watcher
class FarmProxy:
    def __init__(self, cfg: dict, store, notify: Callable[[str], None],
                 lan_snapshot: Callable[[], list] | None = None, fetch=_fetch,
                 port_check=tcp_port_open):
        self.cfg = cfg
        self.fp = cfg["farm_proxy"]
        self.store = store
        self.notify = notify
        self.lan_snapshot = lan_snapshot or (lambda: [])
        self.fetch = fetch
        self.port_check = port_check
        self.source = None
        self.latest: ProxySnapshot | None = None
        self.min_conn_24h: list[tuple[float, float]] = []
        self.last_detect = 0.0
        self.detect_log: list[str] = []
        self._lock = threading.Lock()
        self._warned_no_worker = False

    # ------------------------------------------------------------ state
    @property
    def st(self) -> dict:
        if not hasattr(self, "_st"):
            self._st = self.store.get("fp_state", {}) or {}
        return self._st

    def _save(self):
        self.store.set("fp_state", self.st)

    def _send(self, now, kind, text):
        log.info("FARM PROXY ALERT [%s] %s", kind, text.replace("\n", " | "))
        self.store.log_alert(now, "farm-proxy", kind, text)
        self.notify(text)

    # ------------------------------------------------------------ detection
    def candidates(self) -> list[str]:
        urls = [u for u in (self.fp.get("urls") or []) if u]
        return list(dict.fromkeys(urls or DEFAULT_URLS))

    def detect(self, now: float) -> bool:
        self.last_detect = now
        timeout = float(self.fp["request_timeout_seconds"])
        tried = []
        for url in self.candidates():
            src = make_source(url, timeout, self.fetch)
            try:
                if src.probe():
                    self.source = src
                    log.info("farm proxy: using %s (%s) - tried: %s", url, src.mode, ", ".join(tried) or "-")
                    self.detect_log = tried + [f"{url}: OK ({src.mode})"]
                    return True
                tried.append(f"{url}: no FP_ metrics")
            except Exception as e:  # noqa: BLE001
                tried.append(f"{url}: {str(e)[:60] or e.__class__.__name__}")
        self.detect_log = tried
        if not self.st.get("detect_failed_logged"):
            log.warning("farm proxy: no metrics endpoint found (%s). Run tools/find_proxy.sh on your node.",
                        "; ".join(tried))
            self.st["detect_failed_logged"] = True
        return False

    # ------------------------------------------------------------ poll
    def poll(self, now: float | None = None) -> ProxySnapshot:
        now = now or time.time()
        snap = None
        if self.source is None and now - self.last_detect >= 120:
            self.detect(now)
        if self.source is not None:
            try:
                snap = self.source.snapshot(self.fp, now)
                if snap.dn_conn is None and snap.dn_hr is None and snap.up_conn is None:
                    raise RuntimeError("no farm proxy metrics in response")
            except Exception as e:  # noqa: BLE001
                log.warning("farm proxy poll via %s failed: %s", self.source.url, e)
                snap = ProxySnapshot(ok=False, ts=now, source=self.source.url, error=str(e)[:120])
                self.source = None if now - float(self.st.get("fail_since") or now) >= 120 else self.source
        if snap is None:
            snap = ProxySnapshot(ok=False, ts=now, error="no metrics endpoint found")
        with self._lock:
            self.latest = snap
        try:
            self.evaluate(snap, now)
        except Exception:  # noqa: BLE001
            log.exception("farm proxy alert evaluation failed")
        self._save()
        return snap

    # ------------------------------------------------------------ alerts
    def evaluate(self, s: ProxySnapshot, now: float) -> None:
        fp, st = self.fp, self.st
        cooldown = float(fp["alert_cooldown_minutes"]) * 60
        # ---------------- reachability
        if not s.ok:
            if not st.get("fail_since"):
                last_ok = float(st.get("last_ok") or 0)
                # down since the last good poll (if it was recent; not after the bot was stopped)
                st["fail_since"] = last_ok if 0 < now - last_ok <= 3 * float(fp["poll_seconds"]) else now
            down = now - float(st["fail_since"])
            if down >= float(fp["unreachable_alert_minutes"]) * 60 and not st.get("unreach_alerted"):
                host, port = fp.get("stratum_host"), int(fp.get("stratum_port") or 0)
                port_s = ""
                if host and port:
                    port_s = (f"\nStratum port {host}:{port} is {'OPEN (proxy probably fine, only metrics missing)' if self.port_check(host, port, 3) else 'CLOSED - miners cannot connect!'}")
                self._send(now, "fp_unreachable",
                           f"🔴 FARM PROXY metrics unreachable for {fmt_dur(down)} ({s.error}).{port_s}\n"
                           "Pool-side checks (connections, pool-only workers, upstream) are paused. If this never "
                           "worked, run tools/find_proxy.sh on your node.")
                st["unreach_alerted"] = True
            return
        if st.get("unreach_alerted"):
            self._send(now, "fp_reachable", f"✅ Farm proxy metrics reachable again ({s.source}).")
        st["fail_since"] = None
        st["last_ok"] = now
        st["unreach_alerted"] = False
        st["detect_failed_logged"] = False
        # ---------------- downstream connections
        exp = int(fp["expected_connections"])
        if s.dn_conn is not None and exp > 0:
            n = int(round(s.dn_conn))
            self.min_conn_24h = [(t, v) for t, v in self.min_conn_24h if t >= now - 86400] + [(now, n)]
            if n < exp:
                st["conn_low"] = int(st.get("conn_low", 0)) + 1
                st.setdefault("conn_since", now)
                st["conn_since"] = st["conn_since"] or now
                worse = st.get("conn_alerted") and n < int(st.get("conn_alerted_n", exp)) and \
                    now - float(st.get("conn_last", 0)) >= 600
                if st["conn_low"] >= int(fp["connection_confirm_polls"]) and (not st.get("conn_alerted") or worse):
                    self._send(now, "fp_conn_low", self._conn_text(n, exp))
                    st.update(conn_alerted=True, conn_alerted_n=n, conn_last=now)
            else:
                if st.get("conn_alerted"):
                    self._send(now, "fp_conn_ok", f"✅ FARM PROXY: all miners connected again ({n}/{exp}, "
                                                  f"was short for {fmt_dur(now - float(st.get('conn_since') or now))}).")
                st.update(conn_low=0, conn_alerted=False, conn_since=None)
        # ---------------- watched (pool-only) workers
        for w in fp.get("watch_workers") or []:
            ws = st.setdefault("workers", {}).setdefault(w, {})
            info = s.workers.get(w)
            if not s.per_worker:
                if not self._warned_no_worker:
                    log.info("farm proxy: no per-worker labels for %s - watched via connection count only",
                             ", ".join(fp.get("watch_workers") or []))
                    self._warned_no_worker = True
                continue
            ok = bool(info and info["present"] and (info["hr"] is None or info["hr"] > 0))
            if ok:
                if ws.get("alerted"):
                    self._send(now, "fp_worker_ok", f"✅ {w} is hashing again at the farm proxy"
                                                    f"{' (' + fmt_hr(info['hr']) + ')' if info['hr'] else ''}.")
                ws.update(bad=0, alerted=False, since=None)
                continue
            ws["bad"] = int(ws.get("bad", 0)) + 1
            ws["since"] = ws.get("since") or now
            if ws["bad"] >= int(fp["worker_confirm_polls"]) and not ws.get("alerted") and \
                    now - float(ws.get("last", 0)) >= cooldown:
                why = "is no longer connected to" if not (info and info["present"]) else "has 0 hashrate at"
                self._send(now, "fp_worker_down", f"🔴 POOL-ONLY MINER DOWN: {w} {why} the farm proxy.\n"
                                                  "It has no local API, so check its power/Wi-Fi in its app.")
                ws.update(alerted=True, last=now)
        # ---------------- upstream vs downstream
        pct = float(fp["upstream_min_pct"]) / 100
        if s.dn_cmp and s.dn_cmp > 0 and s.up_cmp is not None:
            ratio = s.up_cmp / s.dn_cmp
            dead = s.up_conn is not None and s.up_conn <= 0
            if ratio < pct or dead:
                st["up_since"] = st.get("up_since") or now
                if now - float(st["up_since"]) >= float(fp["upstream_alert_minutes"]) * 60 - 1 and \
                        not st.get("up_alerted") and now - float(st.get("up_last", 0)) >= cooldown:
                    tgt = ", ".join(f"{k}={'up' if (v or 0) > 0 else 'DOWN'}" for k, v in s.targets.items()) or "n/a"
                    self._send(now, "fp_upstream",
                               f"⚠️ FARM PROXY UPSTREAM PROBLEM: pool/node side gets {fmt_hr(s.up_cmp)} but miners send "
                               f"{fmt_hr(s.dn_cmp)} ({ratio * 100:.0f}%) for {fmt_dur(now - float(st['up_since']))}.\n"
                               f"Upstream connections: {_n(s.up_conn)} | targets: {tgt}\n"
                               "Check your node / solo pool app.")
                    st.update(up_alerted=True, up_last=now)
            else:
                if st.get("up_alerted") and ratio >= max(pct, 0.8):
                    self._send(now, "fp_upstream_ok", f"✅ Farm proxy upstream OK again ({ratio * 100:.0f}% of downstream).")
                    st["up_alerted"] = False
                if ratio >= pct:
                    st["up_since"] = None
        # ---------------- invalid share ratio
        lim = float(fp["invalid_max_pct"])
        for side, val in (("down", s.dn_invalid_pct), ("up", s.up_invalid_pct)):
            k = f"inv_{side}"
            if val is None:
                continue
            if val > lim:
                st[k + "_n"] = int(st.get(k + "_n", 0)) + 1
                if st[k + "_n"] >= int(fp["invalid_confirm_polls"]) and not st.get(k + "_alerted") and \
                        now - float(st.get(k + "_last", 0)) >= cooldown:
                    who = "from miners (downstream)" if side == "down" else "at the pool/node (upstream)"
                    self._send(now, f"fp_invalid_{side}", f"⚠️ FARM PROXY: {val:.1f}% invalid shares {who} "
                                                          f"over the last {fp['invalid_window']} (limit {lim:.0f}%).")
                    st.update({k + "_alerted": True, k + "_last": now})
            else:
                st[k + "_n"] = 0
                if st.get(k + "_alerted") and val < lim * 0.6:
                    self._send(now, f"fp_invalid_{side}_ok", f"✅ Farm proxy invalid shares ({side}) back to {val:.1f}%.")
                    st[k + "_alerted"] = False

    def _conn_text(self, n: int, exp: int) -> str:
        lan = [r for r in self.lan_snapshot() if getattr(r, "kind", "") != "stratum"]
        off = [r.name for r in lan if not r.ok]
        lines = [f"🔌 FARM PROXY: only {n} of {exp} miners connected ({exp - n} missing)."]
        if lan:
            if not off:
                pool_only = ", ".join(self.fp.get("watch_workers") or []) or "a pool-only miner"
                lines.append(f"All {len(lan)} LAN miners answer locally, so it's probably {pool_only} "
                             "(or a miner pointed at another pool).")
            else:
                lines.append("Offline on the LAN: " + ", ".join(off[:8]))
        return "\n".join(lines)

    # ------------------------------------------------------------ rendering
    def status_lines(self) -> list[str]:
        s = self.latest
        if s is None:
            return ["⏳ Pool (farm proxy): no data yet"]
        if not s.ok:
            return [f"🔴 Pool (farm proxy): metrics unavailable ({s.error})"]
        exp = int(self.fp["expected_connections"])
        n = _n(s.dn_conn) + (f"/{exp}" if exp > 0 else "")
        icon = "🟠" if exp > 0 and s.dn_conn is not None and s.dn_conn < exp else "🟢"
        inv = f"{s.dn_invalid_pct:.1f}%" if s.dn_invalid_pct is not None else "n/a"
        lines = [f"{icon} Pool (farm proxy): {n} connections | down {fmt_hr(s.dn_hr)} | "
                 f"up {fmt_hr(s.up_hr)} | invalid {inv}"]
        lines += self._worker_lines()
        return lines

    def _worker_lines(self) -> list[str]:
        s = self.latest
        out = []
        for w in self.fp.get("watch_workers") or []:
            info = (s.workers.get(w) if s else None)
            if s and s.per_worker and info and info.get("present"):
                hr = info.get("hr")
                out.append(f"{'🟢' if hr is None or hr > 0 else '🔴'} {w} (pool-only): "
                           f"{fmt_hr(hr) if hr is not None else 'connected'}")
            elif s and s.per_worker:
                out.append(f"🔴 {w} (pool-only): not seen at the proxy")
            else:
                out.append(f"• {w} (pool-only): no per-worker data from the proxy - counted in connections")
        return out

    def proxy_text(self) -> str:
        s = self.latest
        if s is None:
            return "Farm proxy: first check still running - try again in a minute."
        if not s.ok:
            tried = "\n".join("  " + t for t in self.detect_log) or "  (nothing tried yet)"
            return (f"🔴 Farm proxy metrics unavailable: {s.error}\nTried:\n{tried}\n\n"
                    "Run on your node:  bash tools/find_proxy.sh (in the solo-miner-copilot folder)")
        exp = int(self.fp["expected_connections"])
        mins = [v for _, v in self.min_conn_24h]

        def pc(v):
            return f"{v:.2f}%" if v is not None else "n/a"
        lines = [f"🛰️ Braiins Farm Proxy ({s.mode}: {s.source})",
                 f"Miner connections: {_n(s.dn_conn)}"
                 + (f"/{exp} expected" if exp > 0 else "")
                 + f" (+{_n(s.dn_conn_devfee)} devfee)"
                 + (f" | lowest seen 24h: {min(mins)}" if mins else ""),
                 f"Upstream connections: {_n(s.up_conn)}",
                 f"Downstream: {fmt_hr(s.dn_hr)} 5m | {fmt_hr(s.dn_hr_1h)} 1h | {fmt_hr(s.dn_hr_24h)} 24h",
                 f"Upstream:   {fmt_hr(s.up_hr)} 5m | {fmt_hr(s.up_hr_1h)} 1h | {fmt_hr(s.up_hr_24h)} 24h",
                 f"Invalid shares ({self.fp['invalid_window']}): down {pc(s.dn_invalid_pct)} | up {pc(s.up_invalid_pct)}"
                 f" | 24h: down {pc(s.dn_invalid_24h)} | up {pc(s.up_invalid_24h)}"]
        if s.mode == "metrics" and s.hr_24h_span_h is not None and s.hr_24h_span_h < 23:
            lines.append(f"(24h numbers cover only {s.hr_24h_span_h:.1f}h since the bot started)")
        if s.targets:
            lines.append("Targets: " + ", ".join(f"{k} {'up' if (v or 0) > 0 else 'DOWN'}" for k, v in s.targets.items()))
        if exp > 0 and s.dn_conn is not None and s.dn_conn > exp:
            lines.append(f"ℹ️ More connections than expected - set farm_proxy expected_connections: "
                         f"{int(s.dn_conn)} in config.yaml.")
        lines += self._worker_lines()
        return "\n".join(lines)

    def daily_lines(self) -> list[str]:
        s = self.latest
        if s is None or not s.ok:
            return ["", "🛰️ Pool (farm proxy): no data" + (f" ({s.error})" if s else "")]
        mins = [v for _, v in self.min_conn_24h]
        inv = f"{s.dn_invalid_24h:.2f}%" if s.dn_invalid_24h is not None else "n/a"
        span = (f" (over {s.hr_24h_span_h:.1f}h)" if s.mode == "metrics" and s.hr_24h_span_h is not None
                and s.hr_24h_span_h < 23 else "")
        exp = int(self.fp["expected_connections"])
        count = _n(s.dn_conn) + (f"/{exp}" if exp > 0 else "")
        return ["", f"🛰️ Pool (farm proxy): {count} connected now"
                    + (f", lowest {min(mins)} in 24h" if mins else ""),
                f"   24h avg: down {fmt_hr(s.dn_hr_24h)} | up {fmt_hr(s.up_hr_24h)}{span} | invalid {inv}"] + \
            ["   " + x for x in self._worker_lines()]

    # ------------------------------------------------------------ thread
    def run_forever(self, stop: threading.Event) -> None:
        interval = max(15.0, float(self.fp["poll_seconds"]))
        log.info("Farm proxy check on: every %.0fs, expecting %s miner connections, candidates: %s",
                 interval, self.fp["expected_connections"], ", ".join(self.candidates()))
        while not stop.is_set():
            started = time.time()
            try:
                self.poll(started)
            except Exception:  # noqa: BLE001
                log.exception("farm proxy cycle failed")
            stop.wait(max(1.0, interval - (time.time() - started)))


def _n(v) -> str:
    return "n/a" if v is None else f"{v:.0f}"
