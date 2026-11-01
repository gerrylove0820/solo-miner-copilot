import json
import os
import unittest

from app.collectors import (Reading, parse_avalon, parse_axeos, parse_braiins, parse_cgminer_json,
                            parse_diff, parse_mm_string)
from app.format import fmt_asic_temp

S = os.path.join(os.path.dirname(__file__), "samples")


def load(name):
    with open(os.path.join(S, name), "r", encoding="utf-8") as fh:
        txt = fh.read()
    return parse_cgminer_json(txt) if name.endswith(".txt") else json.loads(txt)


class ParserTests(unittest.TestCase):
    def test_parse_diff(self):
        self.assertEqual(parse_diff(1234), 1234.0)
        self.assertAlmostEqual(parse_diff("4.29G"), 4.29e9)
        self.assertAlmostEqual(parse_diff("1.2 M"), 1.2e6)
        self.assertAlmostEqual(parse_diff("12k"), 12e3)
        self.assertIsNone(parse_diff("n/a"))
        self.assertIsNone(parse_diff(None))

    def test_axeos_full(self):
        r = parse_axeos(load("axeos_info.json"), Reading("b", "axeos", "x"))
        self.assertTrue(r.ok)
        self.assertAlmostEqual(r.hashrate_ghs, 1162.4)
        self.assertAlmostEqual(r.hashrate_now_ghs, 1150.1)
        self.assertEqual(r.expected_ghs, 1176.0)
        self.assertEqual(r.temp_c, 61.25)
        self.assertEqual(r.asic_temps, [61.25])  # temp2: 0 is not a second chip
        self.assertEqual(r.vr_temp_c, 52)
        self.assertEqual(r.fan_rpm, 4210)
        self.assertEqual(r.power_w, 17.8)
        self.assertEqual(r.accepted, 10234)
        self.assertEqual(r.rejected, 12)
        self.assertEqual(r.best_diff, 4291234567)
        self.assertEqual(r.uptime_s, 86400)
        self.assertEqual(r.model, "Gamma")
        self.assertEqual(r.extra["network_difficulty"], 136039872848261)

    def test_axeos_legacy_missing_fields(self):
        r = parse_axeos(load("axeos_legacy.json"), Reading("b", "axeos", "x"))
        self.assertTrue(r.ok)
        self.assertEqual(r.temp_c, 58)          # no temp2 key on old firmware
        self.assertEqual(r.asic_temps, [58.0])
        self.assertIsNone(r.vr_temp_c)          # 0 = no sensor
        self.assertIsNone(r.expected_ghs)       # absent
        self.assertAlmostEqual(r.best_diff, 4.29e9)  # string with suffix
        self.assertEqual(r.accepted, 88)        # numeric string

    def test_axeos_empty(self):
        r = parse_axeos({}, Reading("b", "axeos", "x"))
        self.assertTrue(r.ok)
        self.assertIsNone(r.hashrate_ghs)
        self.assertIsNone(r.temp_c)
        self.assertEqual(r.asic_temps, [])

    def test_nerdqaxe(self):
        r = parse_axeos(load("nerdqaxe_info.json"), Reading("n", "nerdqaxe", "x"))
        self.assertAlmostEqual(r.hashrate_ghs, 4812.7)
        self.assertAlmostEqual(r.best_diff, 12.3e9)
        self.assertEqual(r.temp_c, 63.5)
        self.assertEqual(r.asic_temps, [63.5])  # no second ASIC temp reported
        self.assertEqual(r.vr_temp_c, 58)

    def test_axeos_dual_asic_hottest(self):
        r = parse_axeos({"temp": 49.9, "temp2": 59.8, "vrTemp": 75}, Reading("b", "axeos", "x"))
        self.assertEqual(r.asic_temps, [49.9, 59.8])
        self.assertEqual(r.temp_c, 59.8)
        self.assertEqual(r.vr_temp_c, 75)
        hotter_first = parse_axeos({"temp": 68.2, "temp2": 51.0}, Reading("b", "axeos", "x"))
        self.assertEqual(hotter_first.asic_temps, [68.2, 51.0])
        self.assertEqual(hotter_first.temp_c, 68.2)

    def test_axeos_second_temp_absent(self):
        for extra in ({}, {"temp2": None}, {"temp2": 0}, {"temp2": -1}, {"temp2": ""}):
            r = parse_axeos({"temp": 61.25, "vrTemp": 52, **extra}, Reading("b", "axeos", "x"))
            self.assertEqual(r.temp_c, 61.25, extra)
            self.assertEqual(r.asic_temps, [61.25], extra)

    def test_axeos_temp_only_on_second_sensor(self):
        r = parse_axeos({"temp": 0, "temp2": 59.8}, Reading("b", "axeos", "x"))
        self.assertEqual(r.asic_temps, [59.8])
        self.assertEqual(r.temp_c, 59.8)

    def test_axeos_asic_temps_list(self):
        # Per-chip list wins over the scalar average in `temp`.
        r = parse_axeos({"temp": 60, "temp2": 61, "asicTemps": [49.9, 59.8, 70.2]},
                        Reading("b", "axeos", "x"))
        self.assertEqual(r.asic_temps, [49.9, 59.8, 70.2])
        self.assertEqual(r.temp_c, 70.2)
        fallback = parse_axeos({"temp": 61.25, "temp2": 0, "asicTemps": [-1, 0, None]},
                               Reading("b", "axeos", "x"))
        self.assertEqual(fallback.temp_c, 61.25)
        self.assertEqual(fallback.asic_temps, [61.25])
        triple = parse_axeos({"temp": 40, "temp2": 0, "temp3": 80}, Reading("b", "axeos", "x"))
        self.assertEqual(triple.asic_temps, [40.0, 80.0])
        self.assertEqual(triple.temp_c, 80.0)

    def test_fmt_asic_temp(self):
        self.assertEqual(fmt_asic_temp(61.25, [61.25]), "61°C")
        self.assertEqual(fmt_asic_temp(61.25, []), "61°C")
        self.assertEqual(fmt_asic_temp(None, []), "n/a")
        self.assertEqual(fmt_asic_temp(59.8, [49.9, 59.8]), "ASIC 50/60°C")
        self.assertEqual(fmt_asic_temp(70.2, [49.9, 59.8, 70.2]), "ASIC 50/60/70°C")

    def test_mm_string(self):
        d = parse_mm_string("Temp[44] TAvg[90] PS[0 0 0 4 2775 125 340] SYSTEMSTATU[Work: In Work, Hash Board: 1]")
        self.assertEqual(d["Temp"], "44")
        self.assertEqual(d["PS"], "0 0 0 4 2775 125 340")
        self.assertEqual(d["SYSTEMSTATU"], "Work: In Work, Hash Board: 1")

    def test_malformed_cgminer_json(self):
        d = load("avalon_nano3s_estats.txt")   # '}{' bug + trailing NUL
        self.assertEqual(len(d["STATS"]), 2)

    def test_avalon_nano3s(self):
        r = parse_avalon(load("avalon_nano3s_summary.json"), load("avalon_nano3s_estats.txt"),
                         Reading("a", "avalon", "x"))
        self.assertTrue(r.ok)
        self.assertAlmostEqual(r.hashrate_ghs, 4144.11499, places=3)   # MHS 1m / 1000
        self.assertAlmostEqual(r.hashrate_now_ghs, 4218.70)            # GHSspd
        self.assertAlmostEqual(r.expected_ghs, 4268.88)
        self.assertEqual(r.temp_c, 94)       # TMax
        self.assertEqual(r.asic_temps, [])   # Avalon has no AxeOS temp2
        self.assertEqual(r.ambient_c, 44)    # Temp
        self.assertEqual(r.fan_rpm, 5130)
        self.assertEqual(r.power_w, 340)     # PS[-1] (pyasic uses PS[6]) - UNVERIFIED meaning
        self.assertEqual(r.accepted, 785)
        self.assertEqual(r.best_diff, 6753407)
        self.assertEqual(r.uptime_s, 23241)

    def test_avalon_q_estats_only(self):
        r = parse_avalon(None, load("avalon_q_estats.json"), Reading("q", "avalon", "x"))
        self.assertTrue(r.ok)
        self.assertAlmostEqual(r.hashrate_ghs, 42841.72)
        self.assertEqual(r.temp_c, 71)
        self.assertEqual(r.asic_temps, [])
        self.assertEqual(r.ambient_c, 28)    # ITemp
        self.assertEqual(r.fan_rpm, 1253)
        self.assertEqual(r.power_w, 847)
        self.assertIn("In Work", r.extra["system_status"])
        self.assertIsNone(r.accepted)

    def test_braiins(self):
        r = parse_braiins(load("braiins_summary.json"), load("braiins_temps.json"),
                          load("braiins_fans.json"), load("braiins_tunerstatus.json"),
                          Reading("bmm", "braiins", "x"))
        self.assertTrue(r.ok)
        self.assertAlmostEqual(r.hashrate_ghs, 1010.0)
        self.assertEqual(r.temp_c, 108.9375)
        self.assertEqual(r.asic_temps, [])
        self.assertEqual(r.board_temp_c, 85.875)
        self.assertEqual(r.fan_rpm, 5340)
        self.assertEqual(r.power_w, 1419)
        self.assertEqual(r.accepted, 4321)

    def test_braiins_summary_only(self):
        r = parse_braiins(load("braiins_summary.json"), None, None, None, Reading("bmm", "braiins", "x"))
        self.assertTrue(r.ok)
        self.assertIsNone(r.temp_c)
        self.assertIsNone(r.power_w)


if __name__ == "__main__":
    unittest.main()
