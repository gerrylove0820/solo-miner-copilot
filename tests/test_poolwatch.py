"""Pool watch (Braiins Solo multi-user) - parsing, validation, alert logic, commands.
All HTTP is mocked; no network, no Telegram token."""
import copy
import json
import os
import tempfile
import unittest

from app.braiins_solo import (ApiBusy, SoloClient, hr_ghs, looks_like_address, normalize_address,
                              notable_addresses, parse_pool_status, parse_user, trunc_addr)
from app.config import load_config
from app.poolwatch import PoolWatch
from app.store import Store
from app.telegram_bot import TelegramBot

HERE = os.path.dirname(__file__)
ADDR = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"          # BIP173 test vector (valid)
ADDR2 = "3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy"                  # public example P2SH address
ADDR3 = "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq"
T0 = 1792318910.0                                            # lastshare in the sample


def sample():
    with open(os.path.join(HERE, "samples", "braiins_solo_user.json")) as fh:
        return json.load(fh)


def pool_status_text(now, runtime=383077):
    return (json.dumps({"runtime": runtime, "lastupdate": int(now), "Users": 3213}) + "\n" +
            json.dumps({"hashrate1m": "301P"}) + "\n" +
            json.dumps({"diff": 26.4, "network_diff": 140577680051919}) + "\n" +
            json.dumps({"notable_shares": [{"worker": ADDR + ".bitaxe", "username": ADDR},
                                           {"worker": ADDR2 + ".s21", "username": ADDR2}]}) + "\n")


class FakeApi:
    """Mock Braiins Solo: users[addr] = dict | 404 | 'error'; pool = 'ok' | 'error' | 'stale'."""

    def __init__(self):
        self.users = {}
        self.pool = "ok"
        self.now = T0
        self.calls = []

    def fetch(self, url, timeout):
        self.calls.append(url)
        if url.endswith("/pool.status"):
            if self.pool == "error":
                raise OSError("connection refused")
            t = self.now - 3600 if self.pool == "stale" else self.now
            return 200, pool_status_text(t).encode(), {}
        addr = url.rsplit("/", 1)[1]
        v = self.users.get(addr, 404)
        if v == 404:
            return 404, b"<html>404</html>", {}
        if v == "error":
            return 502, b"", {}
        return 200, json.dumps(v).encode(), {}


def user_payload(workers, best=1e12):
    """workers: {name: (hr_5m_str, hr_1hr_str, hr_1d_str, lastshare)}"""
    return {"hashrate1m": "1T", "hashrate5m": "1T", "hashrate1hr": "1T", "hashrate1d": "1T",
            "hashrate7d": "1T", "lastshare": max([w[3] for w in workers.values()] or [0]),
            "workers": len(workers), "bestshare": best / 10, "bestever": best,
            "worker": [{"workername": f"{ADDR}.{n}", "hashrate1m": w[0], "hashrate5m": w[0],
                        "hashrate1hr": w[1], "hashrate1d": w[2], "hashrate7d": w[2],
                        "lastshare": w[3], "bestshare": best / 10, "bestever": best}
                       for n, w in workers.items()]}


# ===================================================================== parsing
class ParseTests(unittest.TestCase):
    def test_hashrate_units(self):
        self.assertAlmostEqual(hr_ghs("697T"), 697_000)
        self.assertAlmostEqual(hr_ghs("28.4P"), 28.4e6)
        self.assertAlmostEqual(hr_ghs("359G"), 359)
        self.assertAlmostEqual(hr_ghs("1.2E"), 1.2e9)
        self.assertEqual(hr_ghs("0"), 0.0)
        self.assertAlmostEqual(hr_ghs(1.5e12), 1500)
        self.assertIsNone(hr_ghs(None))
        self.assertIsNone(hr_ghs("garbage"))

    def test_parse_real_sample(self):
        st = parse_user(ADDR, sample(), T0)
        self.assertTrue(st.ok)
        self.assertEqual(len(st.workers), 4)
        self.assertEqual(st.workers[0].name, "worker1")
        self.assertAlmostEqual(st.hr_1d, 27.5e6)
        self.assertEqual(st.best_ever, 215106177034944)
        self.assertGreater(st.best_share, 1e12)
        idle = st.workers[3]
        self.assertEqual(idle.hr_5m, 0.0)
        self.assertGreater(idle.hr_1d, 0)
        self.assertLess(idle.lastshare, T0 - 86400)

    def test_worker_without_suffix_and_garbage(self):
        d = {"hashrate5m": "x", "worker": [{"workername": ADDR}, "junk", {"workername": ADDR + ".a"},
                                           {"workername": ADDR + ".a"}]}
        st = parse_user(ADDR, d)
        self.assertEqual([w.name for w in st.workers], ["(default)", "a", "a#2"])
        self.assertIsNone(st.hr_5m)
        with self.assertRaises(ValueError):
            parse_user(ADDR, ["not", "a", "dict"])

    def test_pool_status_multiline(self):
        d = parse_pool_status(pool_status_text(T0) + "not json\n")
        self.assertEqual(d["network_diff"], 140577680051919)
        self.assertEqual(d["runtime"], 383077)
        self.assertEqual(notable_addresses(d), [ADDR, ADDR2])
        with self.assertRaises(ValueError):
            parse_pool_status("<html>")


# ===================================================================== validation
class AddressTests(unittest.TestCase):
    def test_valid(self):
        for a in (ADDR, ADDR2, ADDR3, "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa",
                  "bc1p5d7rjq7g6rdk2yhzks9smlaqtedr4dekq08ge8ztwac72sfr9rusxg3297",
                  "bc1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qccfmv3"):
            self.assertEqual(normalize_address(a), a, a)

    def test_normalisation(self):
        self.assertEqual(normalize_address(ADDR.upper()), ADDR)
        self.assertEqual(normalize_address(f"  {ADDR}.bitaxe1 "), ADDR)
        self.assertEqual(normalize_address(f"bitcoin:{ADDR}?amount=1"), ADDR)

    def test_invalid(self):
        for a in ("", "hello", ADDR[:-1] + "5", "Bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",
                  "tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx", ADDR2[:-1] + "x", "1" * 30,
                  "bc1q" + "q" * 100, "2N2JD6wb56AfK4tfmM6PwdVmoYk2dCKf4Br", "3" + "x" * 50,
                  "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kemeawh"):
            self.assertIsNone(normalize_address(a), a)

    def test_looks_like(self):
        self.assertTrue(looks_like_address(ADDR))
        self.assertTrue(looks_like_address(ADDR2 + ".worker"))
        self.assertFalse(looks_like_address("hello there"))
        self.assertFalse(looks_like_address("100"))

    def test_trunc(self):
        self.assertEqual(trunc_addr(ADDR), "bc1q…f3t4")
        self.assertNotIn(ADDR[8:30], trunc_addr(ADDR))


# ===================================================================== client
class ClientTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeApi()
        self.clock = [1000.0]
        self.slept = []

        def sleep(s):
            self.slept.append(s)
            self.clock[0] += s
        self.c = SoloClient("https://solo.example", 5, 2.0, 900, fetch=self.api.fetch,
                            clock=lambda: self.clock[0], sleep=sleep)

    def test_rate_limit_spacing(self):
        self.api.users[ADDR] = sample()
        for _ in range(3):
            self.assertTrue(self.c.user(ADDR).ok)
        self.assertEqual(self.slept, [2.0, 2.0])

    def test_404_and_errors(self):
        self.assertEqual(self.c.user(ADDR).status, "not_found")
        self.api.users[ADDR] = "error"
        st = self.c.user(ADDR)
        self.assertEqual(st.status, "error")
        self.assertGreaterEqual(self.c.wait_seconds(), 4)   # backing off

    def test_backoff_grows_and_resets(self):
        self.api.pool = "error"
        waits = []
        for _ in range(4):
            with self.assertRaises(RuntimeError):
                self.c.pool_status()
            waits.append(self.c.backoff)
        self.assertEqual(waits, sorted(waits))
        self.assertGreater(waits[-1], waits[0])
        self.api.pool = "ok"
        self.c.pool_status()
        self.assertEqual(self.c.backoff, 0)

    def test_busy_when_caller_cant_wait(self):
        self.api.pool = "error"
        for _ in range(5):
            with self.assertRaises(RuntimeError):
                self.c.pool_status()
        with self.assertRaises(ApiBusy):
            self.c.user(ADDR, max_wait=10)

    def test_retry_after_429(self):
        def f(url, t):
            return 429, b"", {"Retry-After": "120"}
        c = SoloClient("x", 5, 2, fetch=f, clock=lambda: 0.0, sleep=lambda s: None)
        self.assertEqual(c.user(ADDR).status, "error")
        self.assertGreaterEqual(c.wait_seconds(), 120)


# ===================================================================== engine + commands
class PoolWatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        p = os.path.join(self.tmp, "c.yaml")
        with open(p, "w") as fh:
            fh.write("timezone: America/New_York\nnetwork: {enabled: false}\nminers: []\npool_watch: {enabled: true, max_users: 2}\n")
        self.cfg = load_config(p)
        self.store = Store(os.path.join(self.tmp, "db.sqlite3"))
        self.bot = TelegramBot(None, self.tmp, fixed_chat_id="1", dry_run=True)
        self.api = FakeApi()
        self.client = SoloClient("https://solo.example", 5, 2.0, fetch=self.api.fetch,
                                 sleep=lambda s: None)
        self.pw = PoolWatch(self.cfg, self.store, self.bot, self.client)
        self.bot.guest = self.pw
        self.now = T0
        self.pw.clock = lambda: self.now

    # -- helpers
    def say(self, chat, text):
        n = len(self.bot.sent)
        self.bot.handle_update({"update_id": 1, "message": {"chat": {"id": chat, "type": "private"},
                                                            "text": text}})
        return self.bot.sent[n:]

    def set_workers(self, **workers):
        self.api.users[ADDR] = user_payload(workers)

    def poll(self, dt=300):
        self.now += dt
        self.api.now = self.now
        n = len(self.bot.sent)
        self.pw.poll_cycle(self.now)
        return self.bot.sent[n:]

    def subscribe(self, chat="100"):
        self.set_workers(rig=("1T", "1T", "1T", self.now - 10))
        out = self.say(chat, f"/watch {ADDR}")
        self.assertIn("Now watching", [t for c, t in out if c == chat][0])
        return out

    def texts(self, msgs):
        return [t for _, t in msgs]

    # -- config
    def test_backward_compatible_config(self):
        p = os.path.join(self.tmp, "old.yaml")
        with open(p, "w") as fh:
            fh.write("miners: []\n")
        cfg = load_config(p)
        self.assertFalse(cfg["pool_watch"]["enabled"])
        self.assertEqual(cfg["pool_watch"]["poll_seconds"], 300)
        with open(p, "w") as fh:
            fh.write("miners: []\npool_watch:\n")
        self.assertFalse(load_config(p)["pool_watch"]["enabled"])
        with open(p, "w") as fh:
            fh.write("miners: []\npool_watch: {enabled: true, request_spacing_seconds: 0.1}\n")
        self.assertEqual(load_config(p)["pool_watch"]["request_spacing_seconds"], 2.0)

    def test_shipped_config_has_pool_watch(self):
        cfg = load_config(os.path.join(HERE, "..", "config.example.yaml"))
        self.assertFalse(cfg["pool_watch"]["enabled"])
        self.assertEqual(cfg["pool_watch"]["max_users"], 25)
        self.assertEqual(len(cfg["miners"]), 4)

    # -- telegram routing
    def test_owner_unchanged_and_guest_welcome(self):
        self.bot.handlers = {"/status": lambda: "LAN STATUS"}
        out = self.say("1", "/start")
        self.assertIn("owner", out[0][1])
        self.assertIn("/users", out[0][1])
        out = self.say("200", "/start")
        self.assertEqual(out[0][0], "200")
        self.assertIn("/watch", out[0][1])
        self.assertNotIn("owner", out[0][1])
        # guests never get LAN data
        out = self.say("200", "/status")
        self.assertNotIn("LAN STATUS", out[0][1])
        out = self.say("200", "/users")
        self.assertNotIn("Pool-watch users", out[0][1])
        self.assertEqual(self.texts(self.say("1", "/status")), ["LAN STATUS"])

    def test_watch_validation_and_404(self):
        self.assertIn("valid Bitcoin address", self.say("100", "/watch hello")[0][1])
        self.assertIn("doesn't know", self.say("100", f"/watch {ADDR}")[0][1])
        self.assertEqual(self.store.pw_subs_for_chat("100"), [])
        self.assertIn("Send it like this", self.say("100", "/watch")[0][1])

    def test_bare_address_subscribes_and_owner_notified(self):
        self.set_workers(rig=("1T", "1T", "1T", self.now - 10))
        out = self.say("100", ADDR + ".rig")
        self.assertEqual(self.store.pw_subs_for_chat("100"), [ADDR])
        owner_msgs = [t for c, t in out if c == "1"]
        self.assertEqual(len(owner_msgs), 1)
        self.assertIn("bc1q…f3t4", owner_msgs[0])
        self.assertNotIn(ADDR, owner_msgs[0])
        user_msgs = [t for c, t in out if c == "100"]
        self.assertIn("1/1 workers online", user_msgs[0])
        self.assertIn("Already watching", self.say("100", ADDR)[0][1])
        self.assertEqual(self.say("100", "hello"), [])   # random chatter ignored

    def test_limits(self):
        self.api.users[ADDR] = self.api.users[ADDR2] = self.api.users[ADDR3] = sample()
        extra = "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"
        self.api.users[extra] = sample()
        for a in (ADDR, ADDR2, ADDR3):
            self.say("100", f"/watch {a}")
        self.assertEqual(len(self.store.pw_subs_for_chat("100")), 3)
        self.assertIn("max 3", self.say("100", f"/watch {extra}")[0][1])
        self.say("101", f"/watch {extra}")                       # 2nd user (max_users: 2)
        self.assertIn("full", self.say("102", f"/watch {extra}")[0][1])
        self.assertEqual(self.store.pw_subs_for_chat("102"), [])
        # owner is not counted against the cap
        self.assertTrue(any("Now watching" in t for _, t in self.say("1", f"/watch {extra}")))
        users = self.say("1", "/users")[0][1]
        self.assertIn("2/2", users)
        self.assertNotIn(ADDR, users)
        self.assertIn("bc1q…f3t4", users)

    def test_unwatch_and_stop(self):
        self.subscribe()
        self.assertIn("Stopped watching bc1q…f3t4", self.say("100", "/unwatch")[0][1])
        self.subscribe()
        self.assertIn("Stopped", self.say("100", "/unwatch bc1q…f3t4")[0][1])
        self.subscribe()
        self.assertIn("Stopped watching 1", self.say("100", "/stop")[0][1])
        self.assertEqual(self.store.pw_subs_for_chat("100"), [])

    def test_mystatus(self):
        self.subscribe()
        self.set_workers(rig=("1.2T", "1.1T", "1T", self.now - 30), old=("0", "0", "5G", self.now - 90000))
        self.poll()
        txt = self.say("100", "/mystatus")[0][1]
        self.assertIn("rig", txt)
        self.assertIn("1.20 TH/s", txt)
        self.assertIn("last share", txt)
        self.assertIn("Best difficulty", txt)
        self.assertIn("network diff", txt)      # odds from pool.status network_diff fallback

    # -- alerts
    def test_offline_needs_two_polls_then_recovery(self):
        self.subscribe()
        self.set_workers(rig=("1T", "1T", "1T", self.now))
        self.assertEqual(self.poll(), [])
        # stops sharing: lastshare frozen
        last = self.now
        self.set_workers(rig=("300G", "900G", "1T", last))
        self.assertEqual(self.poll(), [])                      # 5 min old - fine
        self.assertEqual(self.poll(), [])                      # 10 min
        self.assertEqual(self.poll(), [])                      # 15 min
        self.assertEqual(self.poll(), [])                      # 20 min (not > 20)
        self.assertEqual(self.poll(), [])                      # 25 min -> 1st bad poll
        out = self.poll()                                      # 2nd bad poll -> alert
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0][0], "100")
        self.assertIn("WORKER OFFLINE", out[0][1])
        self.assertIn("rig (bc1q…f3t4)", out[0][1])
        self.assertEqual(self.poll(), [])                      # no repeat
        self.set_workers(rig=("1T", "500G", "900G", self.now + 300))
        out = self.poll()
        self.assertIn("BACK ONLINE", out[0][1])

    def test_single_bad_poll_no_alert(self):
        self.subscribe()
        self.set_workers(rig=("0", "1T", "1T", self.now))
        self.assertEqual(self.poll(), [])
        self.set_workers(rig=("1T", "1T", "1T", self.now + 300))
        self.assertEqual(self.poll(), [])

    def test_missing_worker_is_offline(self):
        self.subscribe()
        self.set_workers(rig=("1T", "1T", "1T", self.now), other=("1T", "1T", "1T", self.now))
        self.poll()
        self.poll(0)
        self.set_workers(other=("1T", "1T", "1T", self.now + 300))
        self.assertEqual(self.poll(), [])
        self.set_workers(other=("1T", "1T", "1T", self.now + 300))
        out = self.poll()
        self.assertIn("missing from the pool", out[0][1].lower())

    def test_dormant_workers_stay_quiet(self):
        self.api.users[ADDR] = sample()                        # worker4 idle for days
        self.say("100", f"/watch {ADDR}")
        d = sample()
        for _ in range(4):
            self.now += 300
            for w in d["worker"][:3]:
                w["lastshare"] = self.now
            self.api.users[ADDR] = copy.deepcopy(d)
            self.api.now = self.now
            n = len(self.bot.sent)
            self.pw.poll_cycle(self.now)
            self.assertEqual(self.bot.sent[n:], [])

    def test_api_down_no_offline_alerts_owner_told_after_1h(self):
        self.subscribe()
        self.set_workers(rig=("1T", "1T", "1T", self.now))
        self.poll()
        self.api.pool = "error"
        for _ in range(12):                                    # 0..55 min
            self.assertEqual(self.poll(), [])
        out = self.poll()                                      # 60 min
        self.assertEqual([c for c, _ in out], ["1"])
        self.assertIn("Braiins Solo API has been failing", out[0][1])
        self.assertEqual(self.poll(), [])                      # told once
        # back; miner kept hashing -> no offline alerts, owner gets "back"
        self.api.pool = "ok"
        self.set_workers(rig=("1T", "1T", "1T", self.now + 300))
        out = self.poll()
        self.assertEqual([c for c, _ in out], ["1"])
        self.assertIn("answering again", out[0][1])

    def test_stale_pool_stats_and_address_errors_skip(self):
        self.subscribe()
        self.set_workers(rig=("1T", "1T", "1T", self.now - 3000))   # would look offline
        self.api.pool = "stale"
        self.assertEqual(self.poll(), [])
        self.assertEqual(self.poll(), [])
        self.api.pool = "ok"
        self.api.users[ADDR] = "error"
        self.assertEqual(self.poll(), [])
        self.assertEqual(self.poll(), [])
        self.assertEqual(self.pw._state(ADDR)["workers"]["rig"]["bad"], 0)

    def test_pool_restart_404_not_offline(self):
        self.subscribe()
        self.poll()
        del self.api.users[ADDR]
        real = self.api.fetch

        def restarted(url, t):
            if url.endswith("/pool.status"):
                return 200, pool_status_text(self.now, runtime=300).encode(), {}
            return real(url, t)
        self.client._fetch = restarted
        self.assertEqual(self.poll(), [])
        self.assertEqual(self.poll(), [])
        self.assertEqual(self.poll(), [])

    def test_hashrate_drop_two_polls_grace_and_recovery(self):
        self.subscribe()
        t = lambda: self.now + 300   # noqa: E731 - lastshare fresh on next poll
        # within the 2h grace after first sight: no drop alerts even if low
        self.set_workers(rig=("300G", "300G", "1T", t()))
        for _ in range(3):
            self.assertEqual(self.poll(), [])
        self.set_workers(rig=("1T", "1T", "1T", t()))
        for _ in range(24):                                     # 2 h healthy
            self.set_workers(rig=("1T", "1T", "1T", t()))
            self.assertEqual(self.poll(), [])
        self.set_workers(rig=("500G", "550G", "1T", t()))
        self.assertEqual(self.poll(), [])                       # 1st low poll
        self.set_workers(rig=("500G", "550G", "1T", t()))
        out = self.poll()                                       # 2nd -> alert
        self.assertIn("HASHRATE DROP", out[0][1])
        self.assertIn("55%", out[0][1])
        self.set_workers(rig=("500G", "550G", "1T", t()))
        self.assertEqual(self.poll(), [])                       # no spam
        self.set_workers(rig=("700G", "700G", "1T", t()))
        self.assertEqual(self.poll(), [])                       # 70%: between drop & recovery
        self.set_workers(rig=("1T", "950G", "1T", t()))
        self.assertIn("recovered", self.poll()[0][1])

    def test_drop_cooldown(self):
        self.pw.pw["drop_grace_minutes"] = 0
        self.subscribe()
        lo = lambda: self.set_workers(rig=("500G", "500G", "1T", self.now + 300))   # noqa: E731
        hi = lambda: self.set_workers(rig=("1T", "1T", "1T", self.now + 300))       # noqa: E731
        lo(); self.poll(); lo()
        self.assertEqual(len(self.poll()), 1)
        hi(); self.assertEqual(len(self.poll()), 1)             # recovered
        lo(); self.poll(); lo()
        self.assertEqual(self.poll(), [])                       # within 60 min cooldown
        for _ in range(12):
            lo(); out = self.poll()
            if out:
                break
        self.assertIn("HASHRATE DROP", out[0][1])

    def test_new_best_diff_and_cooldown(self):
        self.subscribe()                                        # baseline best 1e12
        self.api.users[ADDR] = user_payload({"rig": ("1T", "1T", "1T", self.now + 300)}, best=2e12)
        out = self.poll()
        self.assertIn("NEW BEST DIFFICULTY", out[0][1])
        self.assertIn("2.00T", out[0][1])
        self.assertIn("by rig", out[0][1])
        self.api.users[ADDR] = user_payload({"rig": ("1T", "1T", "1T", self.now + 300)}, best=3e12)
        self.assertEqual(self.poll(), [])                       # cooldown
        for _ in range(12):
            self.api.users[ADDR] = user_payload({"rig": ("1T", "1T", "1T", self.now + 300)}, best=3e12)
            out = self.poll()
            if out:
                break
        self.assertIn("3.00T", out[0][1])
        self.assertIn("previous 2.00T", out[0][1])

    def test_alerts_fan_out_to_all_watchers_one_request(self):
        self.subscribe("100")
        self.say("101", f"/watch {ADDR}")
        self.api.calls.clear()
        self.set_workers(rig=("1T", "1T", "1T", self.now - 3000))
        self.poll()
        self.assertEqual(len([u for u in self.api.calls if "/users/" in u]), 1)
        out = self.poll()
        self.assertEqual(sorted(c for c, _ in out), ["100", "101"])

    def test_farm_outage_is_batched(self):
        self.subscribe()
        rigs = {f"rig{i}": ("1T", "1T", "1T", self.now + 300) for i in range(8)}
        self.set_workers(**rigs)
        self.poll()
        dead = {k: ("0", "0", "1T", self.now - 3000) for k in rigs}
        self.set_workers(**dead)
        self.poll()
        out = self.poll()
        self.assertEqual(len(out), 1)
        self.assertIn("8 WORKERS OFFLINE", out[0][1])
        self.assertIn("rig0", out[0][1])

    def test_blocked_user_removed(self):
        self.subscribe("100")
        self.bot.blocked.add("100")
        self.bot.send_to = lambda c, t: False
        self.pw._deliver("100", "x")
        self.assertEqual(self.store.pw_subs_for_chat("100"), [])

    def test_daily_summary(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/New_York")
        self.subscribe()
        for _ in range(3):
            self.set_workers(rig=("1T", "1T", "900G", self.now + 300))
            self.poll()
        t7 = datetime(2026, 10, 18, 7, 0, tzinfo=tz).timestamp()
        self.assertEqual(self.pw.maybe_daily_summary(t7), 0)    # first run: arm only
        t8 = datetime(2026, 10, 18, 8, 0, tzinfo=tz).timestamp()
        n = len(self.bot.sent)
        self.assertEqual(self.pw.maybe_daily_summary(t8), 1)
        chat, txt = self.bot.sent[n]
        self.assertEqual(chat, "100")
        self.assertIn("Daily Braiins Solo summary", txt)
        self.assertIn("Avg hashrate (24h, pool): 1.00 TH/s", txt)
        self.assertIn("Uptime: ~100% of checks", txt)
        self.assertIn("offline: rig", txt)
        self.assertIn("Best difficulty", txt)
        self.assertIn("per year", txt)
        self.assertEqual(self.pw.maybe_daily_summary(t8 + 60), 0)

    def test_guest_rate_limit(self):
        replies = [self.say("300", "/help") for _ in range(20)]
        self.assertEqual(sum(1 for r in replies if r), 12)

    def test_disabled_keeps_old_behaviour(self):
        b = TelegramBot(None, self.tmp, fixed_chat_id="1", dry_run=True)
        b.handle_update({"update_id": 1, "message": {"chat": {"id": 200}, "text": "/start"}})
        b.handle_update({"update_id": 1, "message": {"chat": {"id": 200}, "text": ADDR}})
        self.assertEqual(b.sent, [])


if __name__ == "__main__":
    unittest.main()
