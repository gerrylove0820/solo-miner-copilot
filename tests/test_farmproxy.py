"""Braiins Farm Proxy collector - parsing, autodetect, alert logic. All HTTP mocked."""
import json
import os
import re
import tempfile
import unittest
import urllib.parse

from app.collectors import Reading
from app.config import load_config
from app.farmproxy import H, FarmProxy, MetricsSource, PromSource, parse_prom_text
from app.main import App
from app.store import Store
from app.summary import build_daily_summary, build_status

HERE = os.path.dirname(__file__)
T0 = 1_792_400_000.0


class FakeProm:
    """Answers the PromQL our collector sends, from a simple state dict."""

    def __init__(self):
        self.s = {"dn_conn": 14, "devfee": 1, "up_conn": 1, "dn_hr": 15000.0, "up_hr": 14900.0,
                  "dn_rej": 0.003, "up_rej": 0.0, "targets": {"Umbrel node": 1}}
        self.labels = ["__name__", "category", "direction", "goal", "target", "reason"]
        self.label_values = {}
        self.workers = {}          # value -> hr GH/s (present)
        self.down = False
        self.queries = []

    def fetch(self, url, timeout):
        if self.down:
            raise OSError("connection refused")
        u = urllib.parse.urlparse(url)
        if not u.netloc.startswith("prom"):
            raise OSError("connection refused")
        q = urllib.parse.parse_qs(u.query)
        if u.path == "/api/v1/labels":
            return self._ok(self.labels)
        if u.path.startswith("/api/v1/label/"):
            return self._ok(self.label_values.get(u.path.split("/")[4], []))
        expr = q["query"][0]
        self.queries.append(expr)
        return self._ok({"resultType": "vector", "result": self._eval(expr)})

    @staticmethod
    def _ok(data):
        return 200, json.dumps({"status": "success", "data": data}).encode()

    @staticmethod
    def _v(x, metric=None):
        return [] if x is None else [{"metric": metric or {}, "value": [T0, str(x)]}]

    def _eval(self, e):
        s = self.s
        wk = None
        m = re.search(r'(\w+)=~"\(\?i\)\.\*(.+?)\.\*"', e)
        if m:
            wk = m.group(2)
            hits = [v for k, v in self.workers.items() if wk.lower() in k.lower()]
        if e == 'count({__name__=~"FP_.+"})':
            return self._v(250)
        if e.startswith("count(") and wk is not None:
            return self._v(len(hits)) if hits else []
        if "subscription_size_to_target_total" in e:
            return self._v(s["dn_conn"] if "goal!=" in e else s["devfee"])
        if "connections_by_target" in e:
            return self._v(s["up_conn"])
        if "target_status" in e:
            return [{"metric": {"target": k}, "value": [T0, str(v)]} for k, v in s["targets"].items()]
        up = 'direction="up"' in e
        if wk is not None:
            hr = sum(hits) if hits else None
        else:
            hr = s["up_hr"] if up else s["dn_hr"]
        if hr is None:
            return []
        if "accepted_shares" in e:
            return self._v(hr if str(H) in e else hr / H)
        if "rejected_shares" in e:
            frac = s["up_rej"] if up else s["dn_rej"]
            return self._v((hr / H) * frac / (1 - frac))
        return []


def write_cfg(tmp, body):
    p = os.path.join(tmp, "c.yaml")
    with open(p, "w") as fh:
        fh.write(body)
    return load_config(p)


BASE = """telegram: {owner_chat_id: "5"}
network: {enabled: false}
miners:
  - {name: B1, type: axeos, host: 192.168.1.53}
pool: {host: 192.168.1.10}
farm_proxy:
  enabled: true
  expected_connections: 14
  urls: [http://127.0.0.1:9090, http://prometheus:9090]
  watch_workers: [heater1]
"""


class ParseTests(unittest.TestCase):
    def test_parse_sample_text(self):
        with open(os.path.join(HERE, "samples", "farm_proxy_metrics.txt")) as fh:
            txt = fh.read().replace("{DN}", "123").replace("{UP}", "120").replace("{DNREJ}", "1")
        rows = parse_prom_text(txt + 'x{a="q\\"uote",b="c,d"} 1e3\nbad line here\nnan_metric NaN\n')
        names = {n for n, _, _ in rows}
        self.assertIn("FP_subscription_size_to_target_total", names)
        self.assertNotIn("nan_metric", names)
        dev = [v for n, l, v in rows if n == "FP_subscription_size_to_target_total" and l["goal"] == "#!!ii-DevFee"]
        self.assertEqual(dev, [1.0])
        x = [l for n, l, v in rows if n == "x"][0]
        self.assertEqual(x, {"a": 'q"uote', "b": "c,d"})


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = write_cfg(self.tmp, BASE)

    def test_prometheus_snapshot(self):
        fake = FakeProm()
        s = PromSource("http://prometheus:9090", 2, fake.fetch).snapshot(self.cfg["farm_proxy"], T0)
        self.assertEqual((s.dn_conn, s.dn_conn_devfee, s.up_conn), (14, 1, 1))
        self.assertAlmostEqual(s.dn_hr, 15000)
        self.assertAlmostEqual(s.up_hr_24h, 14900)
        self.assertAlmostEqual(s.dn_invalid_pct, 0.3, places=3)
        self.assertEqual(s.targets, {"Umbrel node": 1.0})
        self.assertFalse(s.per_worker)
        # grafana-parity expressions
        self.assertTrue(any('FP_subscription_size_to_target_total' in q or 'subscription_size_to_target_total' in q
                            for q in fake.queries))
        self.assertTrue(any("FP_accepted_shares{direction=\"down\"}[5m]" in q for q in fake.queries))

    def test_metrics_counters_to_hashrate(self):
        with open(os.path.join(HERE, "samples", "farm_proxy_metrics.txt")) as fh:
            tpl = fh.read()
        state = {"dn": 1e6, "up": 1e6, "rej": 0.0}

        def fetch(url, t):
            body = tpl.replace("{DN}", str(state["dn"])).replace("{UP}", str(state["up"])).replace("{DNREJ}", str(state["rej"]))
            return 200, body.encode()
        src = MetricsSource("http://farm-proxy:8080/metrics", 2, fetch)
        self.assertTrue(src.probe())
        t = T0
        s = src.snapshot(self.cfg["farm_proxy"], t)
        self.assertEqual(s.dn_conn, 14)
        self.assertIsNone(s.dn_hr)                   # needs history first
        rate = 15000 / H                              # 15 TH/s in share-difficulty per second
        for _ in range(6):
            t += 60
            state["dn"] += rate * 60
            state["up"] += rate * 60 * 0.5
            state["rej"] += rate * 60 * 0.1
            s = src.snapshot(self.cfg["farm_proxy"], t)
        self.assertAlmostEqual(s.dn_hr, 15000, delta=1)
        self.assertAlmostEqual(s.up_hr, 7500, delta=1)
        self.assertAlmostEqual(s.dn_invalid_pct, 100 * 0.1 / 1.1, delta=0.01)
        self.assertLess(s.hr_24h_span_h, 1)
        state["dn"] = 10.0                            # proxy restarted: counter reset
        t += 60
        s = src.snapshot(self.cfg["farm_proxy"], t)
        self.assertIsNone(s.dn_hr)


class FarmTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = write_cfg(self.tmp, BASE)
        self.store = Store(os.path.join(self.tmp, "db.sqlite3"))
        self.fake = FakeProm()
        self.msgs = []
        self.lan = [Reading("B1", "axeos", "192.168.1.53", ok=True)]
        self.port_open = True
        self.fp = self.make()
        self.t = T0

    def make(self):
        return FarmProxy(self.cfg, self.store, self.msgs.append, lambda: self.lan, fetch=self.fake.fetch,
                         port_check=lambda h, p, t: self.port_open)

    def step(self, dt=60):
        self.t += dt
        n = len(self.msgs)
        self.fp.poll(self.t)
        return self.msgs[n:]

    def test_zero_expected_connections(self):
        self.fp.fp["expected_connections"] = 0
        self.fake.s["dn_conn"] = 2
        self.assertEqual(self.step(), [])
        self.assertIn("🟢 Pool (farm proxy): 2 connections", self.fp.status_lines()[0])
        self.assertIn("Miner connections: 2 (+", self.fp.proxy_text())
        self.assertNotIn("expected", self.fp.proxy_text())
        self.assertIn("2 connected now", "\n".join(self.fp.daily_lines()))
        self.assertNotIn("/0", "\n".join(self.fp.status_lines() + self.fp.daily_lines()))

    def test_autodetect_logs_and_picks_working(self):
        self.step()
        self.assertEqual(self.fp.source.url, "http://prometheus:9090")
        self.assertIn("127.0.0.1:9090", self.fp.detect_log[0])
        self.assertIn("OK (prometheus)", self.fp.detect_log[-1])
        self.assertIn("14/14", self.fp.status_lines()[0])

    def test_connection_drop_confirm_worse_and_recovery(self):
        self.assertEqual(self.step(), [])
        self.fake.s["dn_conn"] = 13
        self.assertEqual(self.step(), [])                    # 1st low poll
        out = self.step()                                    # 2nd -> alert
        self.assertEqual(len(out), 1)
        self.assertIn("only 13 of 14", out[0])
        self.assertIn("heater1", out[0])                     # all LAN miners answer -> pool-only suspect
        self.assertEqual(self.step(), [])                    # no repeat
        self.fake.s["dn_conn"] = 12
        self.assertEqual(self.step(), [])                    # worse, but < 10 min since alert
        for _ in range(10):
            out = self.step()
            if out:
                break
        self.assertIn("only 12 of 14", out[0])
        self.fake.s["dn_conn"] = 14
        out = self.step()
        self.assertIn("all miners connected again", out[0])

    def test_connection_drop_names_offline_lan_miner(self):
        self.lan = [Reading("B1", "axeos", "192.168.1.53", ok=False)]
        self.fake.s["dn_conn"] = 13
        self.step()
        out = self.step()
        self.assertIn("Offline on the LAN: B1", out[0])

    def test_no_per_worker_data_is_quiet(self):
        for _ in range(5):
            self.assertEqual(self.step(), [])
        txt = self.fp.proxy_text()
        self.assertIn("no per-worker data", txt)
        self.assertIn("Upstream connections: 1", txt)

    def test_per_worker_label_found_then_lost(self):
        self.fake.labels.append("worker_name")
        self.fake.label_values["worker_name"] = ["bc1qxyz.bitaxe1", "bc1qxyz.heater1"]
        self.fake.workers = {"bc1qxyz.heater1": 1100.0}
        self.assertEqual(self.step(), [])
        self.assertIn("1.10 TH/s", "\n".join(self.fp.status_lines()))
        self.fake.workers = {}
        self.assertEqual(self.step(), [])
        out = self.step()
        self.assertIn("POOL-ONLY MINER DOWN: heater1", out[0])
        self.assertEqual(self.step(), [])
        self.fake.workers = {"bc1qxyz.heater1": 0.0}          # back, but 0 hashrate: still down
        self.assertEqual(self.step(), [])
        self.fake.workers = {"bc1qxyz.heater1": 1000.0}
        self.assertIn("heater1 is hashing again", self.step()[0])

    def test_unreachable_after_10_min_once_and_recovery(self):
        self.step()
        self.fake.down = True
        for _ in range(9):
            self.assertEqual(self.step(), [])
        out = self.step()
        self.assertEqual(len(out), 1)
        self.assertIn("unreachable for 10m", out[0])
        self.assertIn("192.168.1.10:3333 is OPEN", out[0])
        for _ in range(30):
            self.assertEqual(self.step(), [])
        # restart while still down: persisted state -> no repeat
        self.fp = self.make()
        for _ in range(15):
            self.assertEqual(self.step(), [])
        self.fake.down = False
        out = []
        for _ in range(3):
            out += self.step()
        self.assertTrue(any("reachable again" in m for m in out))

    def test_never_detected_alerts_once(self):
        self.fake.down = True
        self.port_open = False
        out = []
        for _ in range(60):
            out += self.step()
        self.assertEqual(len(out), 1)
        self.assertIn("CLOSED", out[0])
        self.assertIn("find_proxy.sh", out[0])

    def test_upstream_collapse(self):
        self.step()
        self.fake.s["up_hr"] = 6000.0                       # 40%
        out = []
        for _ in range(10):
            out += self.step()
        self.assertEqual(out, [])                           # 9 min
        out = self.step()
        self.assertIn("UPSTREAM PROBLEM", out[0])
        self.assertIn("40%", out[0])
        self.fake.s["up_hr"] = 9000.0                       # 60%: not recovered yet (< 80%)
        self.assertEqual(self.step(), [])
        self.fake.s["up_hr"] = 14500.0
        self.assertIn("upstream OK again", self.step()[0])

    def test_upstream_connections_zero(self):
        self.step()
        self.fake.s["up_conn"] = 0
        out = []
        for _ in range(12):
            out += self.step()
        self.assertEqual(len(out), 1)
        self.assertIn("Upstream connections: 0", out[0])

    def test_invalid_ratio(self):
        self.step()
        self.fake.s["dn_rej"] = 0.08
        self.assertEqual(self.step(), [])
        out = self.step()
        self.assertIn("8.0% invalid shares from miners", out[0])
        self.assertEqual(self.step(), [])
        self.fake.s["dn_rej"] = 0.01
        self.assertIn("back to 1.0%", self.step()[0])

    def test_garbage_response_never_crashes(self):
        self.step()
        self.fp.source.fetch = lambda url, t: (200, b"<html>not json")
        self.assertEqual(self.step(), [])
        self.assertFalse(self.fp.latest.ok)
        self.fp.fetch = self.fp.source.fetch
        for _ in range(5):
            self.assertEqual(self.step(), [])                # re-detect fails quietly, no crash
        self.assertFalse(self.fp.latest.ok)
        self.fp.fetch = self.fake.fetch
        for _ in range(3):
            self.step()
        self.assertTrue(self.fp.latest.ok)                   # re-detected after ~2 min


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ.pop("TELEGRAM_CHAT_ID", None)

    def test_status_summary_proxy_command(self):
        cfg = write_cfg(self.tmp, BASE)
        app = App(cfg, self.tmp, dry_run=True)
        fake = FakeProm()
        app.farm.fetch = fake.fetch
        app.farm.poll(T0)
        st = build_status([Reading("B1", "axeos", "192.168.1.53", ok=True, hashrate_ghs=500)], cfg, app.store,
                          T0, farm=app.farm)
        self.assertIn("Pool (farm proxy): 14/14 connections | down 15.00 TH/s", st)
        self.assertIn("heater1 (pool-only)", st)
        d = build_daily_summary(cfg, app.store, [], T0, farm=app.farm)
        self.assertIn("🛰️ Pool (farm proxy): 14/14 connected now", d)
        app.bot.handle_update({"update_id": 1, "message": {"chat": {"id": 5}, "text": "/start"}})
        self.assertIn("/proxy", app.bot.sent[-1][1])
        app.bot.handle_update({"update_id": 2, "message": {"chat": {"id": 5}, "text": "/proxy"}})
        self.assertIn("Braiins Farm Proxy (prometheus: http://prometheus:9090)", app.bot.sent[-1][1])
        n = len(app.bot.sent)
        app.bot.handle_update({"update_id": 3, "message": {"chat": {"id": 77}, "text": "/proxy"}})
        self.assertFalse(any("Farm Proxy" in t for c, t in app.bot.sent[n:]))   # guests never see it

    def test_backward_compatible(self):
        cfg = write_cfg(self.tmp, "miners: []\n")
        self.assertFalse(cfg["farm_proxy"]["enabled"])
        app = App(cfg, self.tmp, dry_run=True)
        self.assertIsNone(app.farm)
        self.assertNotIn("/proxy", app.bot.handlers)
        self.assertNotIn("Pool (farm proxy)", build_status([Reading("B1", "axeos", "h", ok=True)], cfg, app.store))
        cfg = write_cfg(self.tmp, "miners: []\nfarm_proxy:\n")
        self.assertFalse(cfg["farm_proxy"]["enabled"])
        cfg = write_cfg(self.tmp, "miners: []\nfarm_proxy: {enabled: true, urls: http://x:9090, watch_workers: heater1}\n")
        self.assertEqual(cfg["farm_proxy"]["urls"], ["http://x:9090"])
        self.assertEqual(cfg["farm_proxy"]["watch_workers"], ["heater1"])

    def test_shipped_config(self):
        cfg = load_config(os.path.join(HERE, "..", "config.example.yaml"))
        fp = cfg["farm_proxy"]
        self.assertFalse(fp["enabled"])
        self.assertEqual(fp["expected_connections"], 0)
        self.assertEqual(fp["watch_workers"], [])
        self.assertEqual(fp["stratum_host"], "192.168.1.10")


if __name__ == "__main__":
    unittest.main()
