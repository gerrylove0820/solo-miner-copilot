"""Phone push: sanitized ntfy payloads, failures, and Telegram left intact."""
import os
import queue
import tempfile
import threading
import unittest
import urllib.error
from email.message import Message
from unittest.mock import patch

from app.collectors import Reading
from app.config import load_config
from app.engine import Monitor
from app.main import App
from app.ntfy import (
    NtfyClient,
    NtfySettings,
    msg_best_share,
    msg_offline,
    safe_name,
    sanitize,
    settings_from_env,
)
from app.store import Store

TOPIC = "a1b2c3d4e5f67890"  # 16 chars, the minimum accepted length
HOST = "203.0.113.9"        # documentation address, not a real LAN host


class Resp:
    def __init__(self, status=200):
        self.status = status

    def read(self):
        return b"ok"

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class Opener:
    def __init__(self, errors=None):
        self.errors = list(errors or [])
        self.calls = []

    def __call__(self, req, timeout=None):
        self.calls.append(req)
        self.timeout = timeout
        if self.errors:
            raise self.errors.pop(0)
        return Resp()


def http_error(code):
    return urllib.error.HTTPError("https://ntfy.sh/hidden", code, "no", Message(), None)


class SanitizeTests(unittest.TestCase):
    def test_strips_addresses_wallets_urls_and_secrets(self):
        dirty = (
            f"OFFLINE: Bitaxe-1 ({HOST}) error http://{HOST}/s/{TOPIC} "
            f"user:secretpass@pool bc1qexampleexampleexampleexampleexamp "
            f"1BoatSLRHtKNngkdXEeobR76b53LETtpyT 123456789:AAHAAAAAAAAAAAAAAAAAAAA "
            f"topic {TOPIC}"
        )
        clean = sanitize(dirty, TOPIC)
        for secret in (HOST, TOPIC, "bc1q", "1Boat", "AAHA", "secretpass", "http://", "/s/"):
            self.assertNotIn(secret, clean)
        self.assertIn("Bitaxe-1", clean)
        self.assertIn("OFFLINE", clean)

    def test_ipv6_and_ip_shaped_names(self):
        self.assertNotIn("2001:db8::10", sanitize("seen at 2001:db8::10 today"))
        self.assertEqual(safe_name(HOST), "miner")
        self.assertEqual(safe_name("2001:db8::10"), "miner")
        self.assertEqual(safe_name("bc1qexampleexampleexampleexampleexamp"), "miner")
        self.assertEqual(safe_name("Bitaxe-1"), "Bitaxe-1")

    def test_best_share_is_not_called_a_block(self):
        text = msg_best_share("Bitaxe-1", 5e6, 1e6)
        self.assertIn("NEW BEST SHARE", text)
        self.assertIn("not a confirmed block", text)
        self.assertNotIn("FOUND BLOCK", text)
        self.assertNotIn("accepted", text.lower())

    def test_offline_message_drops_an_address_in_the_name(self):
        text = msg_offline(HOST, 3, False)
        self.assertNotIn(HOST, text)
        self.assertIn("OFFLINE: miner", text)


class SettingsTests(unittest.TestCase):
    def test_off_when_unset(self):
        cfg = settings_from_env({})
        self.assertFalse(cfg.enabled)
        self.assertEqual(cfg.base_url, "https://ntfy.sh")
        self.assertIsNone(cfg.error)

    def test_https_default_and_topic_kept(self):
        cfg = settings_from_env({"NTFY_TOPIC": f"  {TOPIC}  "})
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.topic, TOPIC)
        self.assertEqual(cfg.base_url, "https://ntfy.sh")

    def test_rejects_short_topic_http_and_passwords_without_echoing_them(self):
        short = settings_from_env({"NTFY_TOPIC": "short-topic"})
        self.assertFalse(short.enabled)
        self.assertNotIn("short-topic", short.error)
        http = settings_from_env({"NTFY_TOPIC": TOPIC, "NTFY_URL": "http://ntfy.sh"})
        self.assertFalse(http.enabled)
        self.assertIn("https", http.error)
        secret = "s3cret-password"
        leaked = settings_from_env({"NTFY_TOPIC": TOPIC, "NTFY_URL": f"https://user:{secret}@ntfy.sh"})
        self.assertFalse(leaked.enabled)
        self.assertNotIn(secret, leaked.error)


class PublishTests(unittest.TestCase):
    def client(self, opener, **kwargs):
        settings = NtfySettings(True, TOPIC, "https://ntfy.sh")
        return NtfyClient(settings, timeout=1.5, attempts=3, opener=opener,
                          sleeper=kwargs.get("sleeper") or (lambda _s: None))

    def test_post_is_https_and_body_is_sanitized(self):
        opener = Opener()
        client = self.client(opener)
        ok = client.publish(f"OFFLINE: Bitaxe-1 at {HOST} /s/{TOPIC}", "Miner offline", 4)
        self.assertTrue(ok)
        req = opener.calls[0]
        self.assertEqual(req.full_url, f"https://ntfy.sh/{TOPIC}")
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(opener.timeout, 1.5)
        body = req.data.decode()
        self.assertIn("OFFLINE: Bitaxe-1", body)
        self.assertNotIn(HOST, body)
        self.assertNotIn(TOPIC, body)
        self.assertNotIn("/s/", body)
        self.assertEqual(req.get_header("Priority"), "4")
        self.assertNotIn(TOPIC, req.get_header("User-agent"))

    def test_retries_timeouts_then_succeeds(self):
        sleeps = []
        opener = Opener(errors=[urllib.error.URLError("timed out"), urllib.error.URLError("timed out")])
        client = self.client(opener, sleeper=sleeps.append)
        self.assertTrue(client.publish("BACK ONLINE: Bitaxe-1.", "Miner back online", 3))
        self.assertEqual(len(opener.calls), 3)
        self.assertEqual(len(sleeps), 2)
        self.assertLessEqual(max(sleeps), 2.0)

    def test_does_not_retry_a_rejected_request(self):
        opener = Opener(errors=[http_error(401)])
        client = self.client(opener)
        self.assertFalse(client.publish("HASHRATE DROP: Bitaxe-1.", "Hashrate drop", 4))
        self.assertEqual(len(opener.calls), 1)

    def test_gives_up_after_three_server_errors(self):
        opener = Opener(errors=[http_error(503), http_error(503), http_error(503)])
        client = self.client(opener)
        self.assertFalse(client.publish("SHARES STALLED: Bitaxe-1.", "Shares stalled", 4))
        self.assertEqual(len(opener.calls), 3)

    def test_logs_omit_the_topic_even_when_the_error_contains_it(self):
        class Loud(Exception):
            def __str__(self):
                return "boom " + TOPIC

        opener = Opener(errors=[Loud(), Loud(), Loud()])
        client = self.client(opener)
        with self.assertLogs("ntfy", level="WARNING") as logs:
            self.assertFalse(client.publish("TEMPERATURE NORMAL: Bitaxe-1.", "Temperature normal"))
        blob = "\n".join(logs.output)
        self.assertNotIn(TOPIC, blob)
        self.assertIn("Loud", blob)

    def test_disabled_client_does_not_call_the_network(self):
        opener = Opener()
        client = NtfyClient(NtfySettings(False, "", "https://ntfy.sh"), opener=opener)
        self.assertFalse(client.publish("hi", "title"))
        self.assertEqual(opener.calls, [])
        self.assertIn("Phone push is off", client.send_test())

    def test_queue_full_drops_without_raising(self):
        opener = Opener()
        client = self.client(opener)
        client._queue = queue.Queue(maxsize=1)
        client._thread = threading.current_thread()
        client.send_alert("OFFLINE: Bitaxe-1. Not responding for 3 polls in a row.", "offline")
        with self.assertLogs("ntfy", level="WARNING") as logs:
            client.send_alert("OFFLINE: Bitaxe-1. Not responding for 4 polls in a row.", "offline")
        self.assertIn("queue is full", "\n".join(logs.output))
        self.assertNotIn(TOPIC, "\n".join(logs.output))

    def test_start_log_hides_the_topic(self):
        client = self.client(Opener())
        with self.assertLogs("ntfy", level="INFO") as logs:
            self.assertTrue(client.start())
        self.addCleanup(client.stop)
        self.assertNotIn(TOPIC, "\n".join(logs.output))
        self.assertIn("HTTPS", "\n".join(logs.output))


class EnginePhoneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        path = os.path.join(self.tmp, "c.yaml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(
                "poll_interval_seconds: 60\n"
                "network: {enabled: false}\n"
                "thresholds:\n"
                "  offline_after_failed_polls: 3\n"
                "  hashrate_baseline_min_minutes: 1\n"
                "  hashrate_drop_minutes: 1\n"
                "  shares_stall_minutes: 1\n"
                "miners:\n"
                "  - {name: Bitaxe-1, type: axeos, host: 203.0.113.9}\n"
            )
        self.cfg = load_config(path)
        self.store = Store(os.path.join(self.tmp, "db.sqlite3"))
        self.telegram = []
        self.phone = []
        self.mon = Monitor(self.cfg, self.store,
                           lambda text, kind, miner: self.telegram.append((kind, text)),
                           phone=lambda text, kind, miner: self.phone.append((kind, text, miner)))
        self.t = 1_700_000_000.0

    def step(self, reading, dt=60):
        self.t += dt
        self.mon.process([reading], self.t)

    def reading(self, **kwargs):
        ok = kwargs.get("ok", True)
        r = Reading("Bitaxe-1", "axeos", HOST, ok=ok)
        if ok:
            r.hashrate_ghs = r.hashrate_now_ghs = kwargs.get("hr", 1000.0)
            r.temp_c = kwargs.get("temp", 60)
            r.vr_temp_c = kwargs.get("vr", 50)
            r.accepted = kwargs.get("acc")
            r.best_diff = kwargs.get("best")
        else:
            r.error = kwargs.get("error", f"timed out talking to {HOST}")
        return r

    def test_alerts_keep_telegram_text_and_strip_the_phone_copy(self):
        self.step(self.reading())
        self.step(self.reading(ok=False))
        self.step(self.reading(ok=False))
        self.step(self.reading(ok=False))
        self.assertEqual(self.phone[0][0], "offline")
        self.assertIn(HOST, self.telegram[0][1])
        self.assertNotIn(HOST, self.phone[0][1])
        self.assertIn("OFFLINE: Bitaxe-1", self.phone[0][1])
        self.assertNotIn("timed out", self.phone[0][1])
        self.step(self.reading(ok=False))
        self.assertEqual([k for k, _, _ in self.phone], ["offline"])
        self.step(self.reading())
        self.assertEqual(self.phone[-1][0], "recovered")
        self.assertIn("BACK ONLINE: Bitaxe-1", self.phone[-1][1])
        self.assertNotIn(HOST, self.phone[-1][1])

        self.step(self.reading(temp=80))
        self.assertEqual(self.phone[-1][0], "overheat_asic")
        self.assertIn("OVERHEAT: Bitaxe-1", self.phone[-1][1])
        self.assertIn("80°C", self.phone[-1][1])
        self.assertNotIn(HOST, self.phone[-1][1])
        self.step(self.reading(temp=60))
        self.assertEqual(self.phone[-1][0], "cooled_asic")
        self.assertIn("TEMPERATURE NORMAL", self.phone[-1][1])

        for _ in range(3):
            self.step(self.reading(hr=1000))
        self.step(self.reading(hr=100))
        self.step(self.reading(hr=100))
        self.assertIn("hashrate_drop", [k for k, _, _ in self.phone])
        drop = [text for kind, text, _ in self.phone if kind == "hashrate_drop"][-1]
        self.assertIn("HASHRATE DROP: Bitaxe-1", drop)
        self.assertNotIn(HOST, drop)
        self.step(self.reading(hr=1000))
        self.assertEqual(self.phone[-1][0], "hashrate_ok")

        self.step(self.reading(acc=5, hr=1000))
        self.step(self.reading(acc=5, hr=1000))
        self.assertEqual(self.phone[-1][0], "shares_stalled")
        self.assertIn("SHARES STALLED: Bitaxe-1", self.phone[-1][1])
        self.assertNotIn(HOST, self.phone[-1][1])
        self.step(self.reading(acc=6, hr=1000))
        self.assertEqual(self.phone[-1][0], "shares_ok")

        self.step(self.reading(best=1e6))
        self.assertNotIn("best_diff", [k for k, _, _ in self.phone])
        self.step(self.reading(best=5e6))
        self.assertEqual(self.phone[-1][0], "best_diff")
        self.assertIn("NEW BEST SHARE", self.phone[-1][1])
        self.assertIn("not a confirmed block", self.phone[-1][1])
        self.assertIn("NEW BEST DIFFICULTY", self.telegram[-1][1])
        self.assertNotIn(HOST, self.phone[-1][1])

    def test_phone_hook_failure_still_sends_telegram(self):
        def boom(text, kind, miner):
            raise RuntimeError("do not log " + TOPIC)

        self.mon.phone = boom
        self.step(self.reading(temp=80))
        self.assertEqual(self.telegram[0][0], "overheat_asic")
        self.assertIn(HOST, self.telegram[0][1])


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        path = os.path.join(self.tmp, "c.yaml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("network: {enabled: false}\nminers: []\n")
        self.path = path

    def app(self, env):
        with patch.dict(os.environ, env, clear=False):
            app = App(load_config(self.path), self.tmp, dry_run=True)
        self.addCleanup(app.ntfy.stop)
        self.addCleanup(app.pool.shutdown, wait=False)
        self.addCleanup(app.store.db.close)
        return app

    def say(self, app, chat, text):
        app.bot.handle_update({"update_id": 1, "message": {"chat": {"id": chat}, "text": text}})

    def test_testpush_off_and_non_owner_ignored(self):
        app = self.app({"TELEGRAM_CHAT_ID": "4242", "NTFY_TOPIC": ""})
        opener = Opener()
        app.ntfy._opener = opener
        self.say(app, 999, "/testpush")
        self.assertEqual(app.bot.sent, [])
        self.say(app, 4242, "/testpush")
        self.assertIn("Phone push is off", app.bot.sent[-1][1])
        self.assertEqual(opener.calls, [])
        self.say(app, 4242, "/status")
        self.assertIn("No data yet", app.bot.sent[-1][1])

    def test_testpush_delivers_without_naming_the_topic(self):
        app = self.app({"TELEGRAM_CHAT_ID": "4242", "NTFY_TOPIC": TOPIC})
        opener = Opener()
        app.ntfy._opener = opener
        app.ntfy._sleeper = lambda _s: None
        self.say(app, 4242, "/testpush")
        reply = app.bot.sent[-1][1]
        self.assertIn("Test alert sent", reply)
        self.assertNotIn(TOPIC, reply)
        self.assertEqual(opener.calls[0].full_url, f"https://ntfy.sh/{TOPIC}")
        body = opener.calls[0].data.decode()
        self.assertIn("no miner data", body)
        self.assertNotIn(TOPIC, body)
        self.say(app, 4242, "/status")
        self.assertIn("No data yet", app.bot.sent[-1][1])

    def test_testpush_failure_does_not_stop_telegram(self):
        app = self.app({"TELEGRAM_CHAT_ID": "4242", "NTFY_TOPIC": TOPIC})
        app.ntfy._opener = Opener(errors=[urllib.error.URLError("timed out")] * 3)
        app.ntfy._sleeper = lambda _s: None
        with self.assertLogs("ntfy", level="WARNING") as logs:
            self.say(app, 4242, "/testpush")
        self.assertIn("could not be sent", app.bot.sent[-1][1])
        self.assertNotIn(TOPIC, app.bot.sent[-1][1])
        self.assertNotIn(TOPIC, "\n".join(logs.output))
        self.say(app, 4242, "/status")
        self.assertIn("No data yet", app.bot.sent[-1][1])


if __name__ == "__main__":
    unittest.main()
