import os
import tempfile
import unittest

from app.collectors import Reading, parse_axeos
from app.config import load_config
from app.engine import Monitor
from app.store import Store
from app.summary import build_daily_summary, build_status, expected_blocks_per_day, solo_odds

CFG_YAML = """
poll_interval_seconds: 60
network: {enabled: false}
miners:
  - {name: B1, type: axeos, host: 192.168.1.53}
  - {name: AQ, type: avalon, host: 192.168.1.54, temp_max_c: 85}
"""


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        p = os.path.join(self.tmp, "c.yaml")
        with open(p, "w") as fh:
            fh.write(CFG_YAML)
        self.cfg = load_config(p)
        self.store = Store(os.path.join(self.tmp, "db.sqlite3"))
        self.msgs = []
        self.mon = Monitor(self.cfg, self.store, lambda t, k, m: self.msgs.append((k, m, t)))
        self.t = 1_700_000_000.0

    def kinds(self):
        return [k for k, _, _ in self.msgs]

    def rd(self, ok=True, hr=1000.0, temp=60, vr=50, acc=None, best=None, name="B1"):
        r = Reading(name, "axeos", "192.168.1.53", ok=ok)
        if ok:
            r.hashrate_ghs = r.hashrate_now_ghs = hr
            r.temp_c, r.vr_temp_c, r.accepted, r.best_diff, r.power_w = temp, vr, acc, best, 15.0
        else:
            r.error = "timeout"
        return r

    def step(self, r, dt=60):
        self.t += dt
        self.mon.process([r], self.t)

    def test_offline_after_3_and_recovery(self):
        self.step(self.rd())
        self.step(self.rd(ok=False)); self.step(self.rd(ok=False))
        self.assertEqual(self.kinds(), [])
        self.step(self.rd(ok=False))
        self.assertEqual(self.kinds(), ["offline"])
        self.step(self.rd(ok=False))  # no repeat
        self.assertEqual(self.kinds(), ["offline"])
        self.step(self.rd())
        self.assertEqual(self.kinds(), ["offline", "recovered"])

    def test_single_blip_no_alert(self):
        self.step(self.rd()); self.step(self.rd(ok=False)); self.step(self.rd())
        self.assertEqual(self.kinds(), [])

    def test_overheat_cooldown(self):
        self.step(self.rd(temp=72))
        self.assertEqual(self.kinds(), ["overheat_asic"])
        self.assertIn("ASIC/chip temp 72°C", self.msgs[0][2])
        self.assertNotIn("chips", self.msgs[0][2])
        for _ in range(29):
            self.step(self.rd(temp=73))
        self.assertEqual(self.kinds(), ["overheat_asic"])   # cooldown 30 min
        self.step(self.rd(temp=73))
        self.assertEqual(self.kinds(), ["overheat_asic", "overheat_asic"])
        self.step(self.rd(temp=64))
        self.assertEqual(self.kinds()[-1], "cooled_asic")

    def test_vr_overheat(self):
        self.step(self.rd(vr=90))
        self.assertEqual(self.kinds(), ["overheat_vr"])

    def test_avalon_threshold_override(self):
        r = Reading("AQ", "avalon", "192.168.1.54", ok=True)
        r.temp_c = 80
        self.mon.process([r], self.t)
        self.assertEqual(self.kinds(), [])
        r.temp_c = 86
        self.mon.process([r], self.t + 60)
        self.assertEqual(self.kinds(), ["overheat_asic"])

    def test_hashrate_drop_needs_10_min(self):
        for _ in range(90):  # 1.5 h baseline at 1000 GH/s
            self.step(self.rd(hr=1000))
        for i in range(10):
            self.step(self.rd(hr=600))
        self.assertNotIn("hashrate_drop", self.kinds())  # 9 min below so far
        self.step(self.rd(hr=600))
        self.assertIn("hashrate_drop", self.kinds())
        self.step(self.rd(hr=600))
        self.assertEqual(self.kinds().count("hashrate_drop"), 1)
        self.step(self.rd(hr=990))
        self.assertEqual(self.kinds()[-1], "hashrate_ok")

    def test_no_drop_alert_without_baseline(self):
        for _ in range(5):
            self.step(self.rd(hr=1000))
        for _ in range(15):
            self.step(self.rd(hr=100))
        self.assertNotIn("hashrate_drop", self.kinds())

    def test_shares_stalled(self):
        self.step(self.rd(acc=100))
        for i in range(19):
            self.step(self.rd(acc=100))
        self.assertNotIn("shares_stalled", self.kinds())
        self.step(self.rd(acc=100))
        self.assertIn("shares_stalled", self.kinds())
        self.step(self.rd(acc=100))
        self.assertEqual(self.kinds().count("shares_stalled"), 1)
        self.step(self.rd(acc=101))
        self.assertEqual(self.kinds()[-1], "shares_ok")

    def test_shares_counter_reset_is_not_stall(self):
        self.step(self.rd(acc=500))
        self.step(self.rd(acc=3))     # reboot
        for _ in range(10):
            self.step(self.rd(acc=3 + _ + 1))
        self.assertNotIn("shares_stalled", self.kinds())

    def test_best_diff(self):
        self.step(self.rd(best=1e6))
        self.assertEqual(self.kinds(), [])   # first observation is silent
        self.step(self.rd(best=1e6))
        self.step(self.rd(best=5e6))
        self.assertEqual(self.kinds(), ["best_diff"])
        self.step(self.rd(best=2e6))         # lower (e.g. cgminer restart) -> silent
        self.assertEqual(self.kinds(), ["best_diff"])

    def test_state_survives_restart(self):
        for _ in range(4):
            self.step(self.rd(ok=False))
        mon2 = Monitor(self.cfg, self.store, lambda t, k, m: self.msgs.append((k, m, t)))
        self.t += 60
        mon2.process([self.rd(ok=False)], self.t)
        self.assertEqual(self.kinds().count("offline"), 1)

    def test_odds_math(self):
        # 1 TH/s vs difficulty 1e14: 1e12*86400/(1e14*2^32)
        lam = expected_blocks_per_day(1000, 1e14)
        self.assertAlmostEqual(lam, 1e12 * 86400 / (1e14 * 2 ** 32))
        o = solo_odds(1000, 1e14)
        self.assertAlmostEqual(o["per_day"], lam, places=10)
        self.assertGreater(o["per_year"], o["per_day"])

    def test_summary_and_status_render(self):
        for _ in range(30):
            self.step(self.rd(hr=1000, temp=62, best=2e9, acc=_))
        self.store.set("network_difficulty", {"value": 1.36e14, "ts": self.t, "source": "test"})
        txt = build_daily_summary(self.cfg, self.store, None, self.t)
        self.assertIn("B1", txt)
        self.assertIn("DEFAULT rate", txt)
        self.assertIn("per year", txt)
        self.assertIn("AQ: no data", txt)
        st = build_status([self.rd(hr=1000)], self.cfg, self.store, self.t)
        self.assertIn("Online 1/1", st)
        self.assertIn("max 62°C/VR 50°C", txt)
        self.assertNotIn("ASIC", txt)

    def _axe(self, payload):
        return parse_axeos(payload, Reading("B1", "axeos", "192.168.1.53"))

    def test_dual_asic_second_chip_overheat_and_recovery(self):
        hot = self._axe({"temp": 50, "temp2": 75, "hashRate": 1000, "power": 18})
        self.step(hot)
        self.assertEqual(self.kinds(), ["overheat_asic"])
        self.assertIn("ASIC/chip temp 75°C (chips 50/75°C)", self.msgs[-1][2])
        self.assertIn("> limit 70°C", self.msgs[-1][2])
        cool = self._axe({"temp": 48, "temp2": 60, "hashRate": 1000, "power": 18})
        self.step(cool)
        self.assertEqual(self.kinds()[-1], "cooled_asic")
        self.assertIn("60°C (chips 48/60°C)", self.msgs[-1][2])

    def test_dual_asic_under_limit_does_not_alert(self):
        self.step(self._axe({"temp": 49.9, "temp2": 59.8, "vrTemp": 75, "hashRate": 4400}))
        self.assertEqual(self.kinds(), [])

    def test_missing_temp2_does_not_change_single_chip_alert(self):
        self.step(self._axe({"temp": 72, "temp2": 0, "hashRate": 1000}))
        self.assertEqual(self.kinds(), ["overheat_asic"])
        self.assertIn("ASIC/chip temp 72°C", self.msgs[-1][2])
        self.assertNotIn("chips", self.msgs[-1][2])

    def test_status_shows_both_chips_only_when_present(self):
        dual = self._axe({"temp": 49.9, "temp2": 59.8, "vrTemp": 75, "hashRate": 4400})
        st = build_status([dual], self.cfg, self.store, self.t)
        self.assertIn("ASIC 50/60°C/VR 75°C", st)
        for payload in (
            {"temp": 61.25, "temp2": 0, "vrTemp": 52, "hashRate": 1000},
            {"temp": 61.25, "vrTemp": 52, "hashRate": 1000},
            {"temp": 61.25, "temp2": None, "vrTemp": 52, "hashRate": 1000},
        ):
            st = build_status([self._axe(payload)], self.cfg, self.store, self.t)
            self.assertIn("61°C/VR 52°C", st)
            self.assertNotIn("ASIC", st)
        avalon = Reading("AQ", "avalon", "192.168.1.54", ok=True, hashrate_ghs=4000)
        avalon.temp_c = 94
        st = build_status([avalon], self.cfg, self.store, self.t)
        self.assertIn("94°C", st)
        self.assertNotIn("ASIC", st)
        nerd = parse_axeos({"temp": 63.5, "vrTemp": 58, "hashRate": 4812.7},
                           Reading("N", "nerdqaxe", "192.168.1.55"))
        st = build_status([nerd], self.cfg, self.store, self.t)
        self.assertIn("64°C/VR 58°C", st)
        self.assertNotIn("ASIC", st)

    def test_daily_max_uses_hotter_asic(self):
        self.step(self._axe({"temp": 49.9, "temp2": 59.8, "vrTemp": 76, "hashRate": 4400, "power": 20}))
        self.step(self._axe({"temp": 55, "temp2": 52, "vrTemp": 70, "hashRate": 4400, "power": 20}))
        txt = build_daily_summary(self.cfg, self.store, None, self.t)
        self.assertIn("max 60°C/VR 76°C", txt)
        self.assertNotIn("ASIC", txt)


if __name__ == "__main__":
    unittest.main()
