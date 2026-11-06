"""Private LAN status page: auth, routes, and the same snapshot as /status."""
import http.client
import os
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import quote

from app import __version__
from app.config import load_config
from app.main import App
from app.status_page import (
    StatusPage,
    StatusPageConfig,
    config_from_env,
    redact,
    snapshot_text,
    tokens_match,
)
from app.summary import build_status

TOKEN = "ab12" * 8  # 32 hex chars, the shape `openssl rand -hex 16` produces
FLEET = "Online 2/3 | 1.50 TH/s | 120 W\n🟢 Bitaxe-1: 500 GH/s | 61°C | 15 W | best 1.20G"


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def fetch(port: int, path: str, method: str = "GET", headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request(method, path, headers=headers or {})
        resp = conn.getresponse()
        body = resp.read()
        header_map = {k.lower(): v for k, v in resp.getheaders()}
        return resp.status, header_map, body
    finally:
        conn.close()


class ConfigTests(unittest.TestCase):
    def test_off_when_token_missing(self):
        cfg = config_from_env({})
        self.assertFalse(cfg.enabled)
        self.assertEqual(cfg.token, "")
        self.assertEqual(cfg.port, 8787)
        self.assertEqual(cfg.bind, "0.0.0.0")
        self.assertIsNone(cfg.error)

    def test_whitespace_token_is_off(self):
        self.assertFalse(config_from_env({"STATUS_PAGE_TOKEN": "   "}).enabled)

    def test_token_enables_lan_defaults(self):
        cfg = config_from_env({"STATUS_PAGE_TOKEN": f"  {TOKEN}  "})
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.token, TOKEN)
        self.assertEqual(cfg.port, 8787)
        self.assertEqual(cfg.bind, "0.0.0.0")

    def test_custom_bind_and_port(self):
        cfg = config_from_env({
            "STATUS_PAGE_TOKEN": TOKEN,
            "STATUS_PAGE_PORT": "8790",
            "STATUS_PAGE_BIND": "127.0.0.1",
        })
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.port, 8790)
        self.assertEqual(cfg.bind, "127.0.0.1")

    def test_bad_port_does_not_enable(self):
        for raw in ("nope", "0", "65536", "-1"):
            cfg = config_from_env({"STATUS_PAGE_TOKEN": TOKEN, "STATUS_PAGE_PORT": raw})
            self.assertFalse(cfg.enabled, raw)
            self.assertIn("STATUS_PAGE_PORT", cfg.error)

    def test_bad_bind_does_not_enable(self):
        cfg = config_from_env({"STATUS_PAGE_TOKEN": TOKEN, "STATUS_PAGE_BIND": "http://0.0.0.0"})
        self.assertFalse(cfg.enabled)
        self.assertIn("STATUS_PAGE_BIND", cfg.error)

    def test_reads_process_environment(self):
        with patch.dict(os.environ, {"STATUS_PAGE_TOKEN": TOKEN, "STATUS_PAGE_PORT": "8788"}, clear=False):
            cfg = config_from_env()
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.port, 8788)
        self.assertEqual(cfg.token, TOKEN)


class TokenTests(unittest.TestCase):
    def test_match_and_mismatch(self):
        self.assertTrue(tokens_match(TOKEN, TOKEN))
        self.assertFalse(tokens_match(TOKEN, TOKEN + "x"))
        self.assertFalse(tokens_match("wrong-token-value", TOKEN))
        self.assertFalse(tokens_match("", TOKEN))
        self.assertFalse(tokens_match(TOKEN, ""))

    def test_redact_hides_raw_and_quoted_token(self):
        self.assertNotIn(TOKEN, redact(f"GET /s/{TOKEN} HTTP/1.1", TOKEN))
        secret = "a/b c"
        quoted = quote(secret, safe="")
        hidden = redact(f"GET /s/{secret} ?token={quoted}", secret)
        self.assertNotIn(secret, hidden)
        self.assertNotIn(quoted, hidden)
        self.assertIn("<token>", hidden)


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.text = FLEET
        self.page = StatusPage(lambda: self.text)
        cfg = StatusPageConfig(True, TOKEN, "127.0.0.1", 0)
        self.assertTrue(self.page.start(cfg))
        self.port = self.page.httpd.server_address[1]
        self.addCleanup(self.page.stop)

    def test_missing_and_wrong_token_are_unauthorized(self):
        for path in ("/", "/s/not-the-secret", "/s/not-the-secret/plain", "/anything"):
            status, headers, body = fetch(self.port, path)
            self.assertEqual(status, 401, path)
            self.assertEqual(body, b"Unauthorized\n")
            self.assertNotIn(b"Online", body)
            self.assertNotIn(b"Bitaxe", body)
            self.assertNotIn(TOKEN.encode(), body)
            self.assertNotIn("www-authenticate", headers)
            self.assertEqual(headers.get("cache-control"), "no-store")

    def test_path_token_shows_same_snapshot_as_html(self):
        status, headers, body = fetch(self.port, f"/s/{TOKEN}")
        self.assertEqual(status, 200)
        self.assertTrue(headers["content-type"].startswith("text/html"))
        page = body.decode()
        self.assertIn(FLEET, page)
        self.assertIn(__version__, page)
        self.assertIn("Telegram /status", page)
        self.assertNotIn(TOKEN, page)
        self.assertEqual(headers.get("referrer-policy"), "no-referrer")
        self.assertEqual(headers.get("cache-control"), "no-store")
        self.assertIn("default-src 'none'", headers.get("content-security-policy", ""))
        self.assertEqual(headers.get("server"), "SoloMinerCopilot")

    def test_plain_route_is_exact_status_text(self):
        status, headers, body = fetch(self.port, f"/s/{TOKEN}/plain")
        self.assertEqual(status, 200)
        self.assertTrue(headers["content-type"].startswith("text/plain"))
        self.assertEqual(body.decode(), FLEET)

    def test_query_and_bearer_token(self):
        status, _, body = fetch(self.port, f"/?token={TOKEN}")
        self.assertEqual(status, 200)
        self.assertIn("Online 2/3", body.decode())
        status, _, body = fetch(self.port, "/", headers={"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(status, 200)
        self.assertIn("Bitaxe-1", body.decode())
        status, _, body = fetch(self.port, "/", headers={"Authorization": "Bearer nope-nope-nope"})
        self.assertEqual(status, 401)
        self.assertNotIn(b"Online", body)

    def test_braces_in_status_text_stay_literal(self):
        self.text = "Online 1/1 {hashrate} {body}"
        status, _, body = fetch(self.port, f"/s/{TOKEN}")
        self.assertEqual(status, 200)
        self.assertIn("Online 1/1 {hashrate} {body}", body.decode())
        self.assertIn("refreshes every minute", body.decode())

    def test_html_escapes_miner_names(self):
        self.text = 'Online 1/1\n🟢 <script>alert("x")</script>: 10 GH/s'
        _, _, body = fetch(self.port, f"/s/{TOKEN}")
        page = body.decode()
        self.assertNotIn("<script>", page)
        self.assertIn("&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;", page)

    def test_head_has_no_body(self):
        status, headers, body = fetch(self.port, f"/s/{TOKEN}", method="HEAD")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self.assertGreater(int(headers["content-length"]), 0)

    def test_post_is_rejected(self):
        status, _, body = fetch(self.port, f"/s/{TOKEN}", method="POST")
        self.assertEqual(status, 405)
        self.assertNotIn(b"Online", body)

    def test_unknown_path_with_valid_token_is_not_the_snapshot(self):
        status, _, body = fetch(self.port, f"/s/{TOKEN}/other")
        self.assertEqual(status, 404)
        self.assertNotIn(b"Online", body)

    def test_snapshot_error_is_generic(self):
        self.page.stop()
        page = StatusPage(lambda: (_ for _ in ()).throw(RuntimeError(f"boom {TOKEN}")))
        self.addCleanup(page.stop)
        self.assertTrue(page.start(StatusPageConfig(True, TOKEN, "127.0.0.1", 0)))
        port = page.httpd.server_address[1]
        with self.assertLogs("status_page", level="ERROR") as logs:
            status, _, body = fetch(port, f"/s/{TOKEN}")
        self.assertEqual(status, 500)
        self.assertIn(b"Status unavailable", body)
        self.assertNotIn(b"boom", body)
        self.assertNotIn(TOKEN.encode(), body)
        self.assertTrue(any("snapshot failed" in line for line in logs.output))

    def test_request_log_does_not_contain_token(self):
        with self.assertLogs("status_page", level="INFO") as logs:
            status, _, _ = fetch(self.port, f"/s/{TOKEN}/plain?token={TOKEN}")
        self.assertEqual(status, 200)
        blob = "\n".join(logs.output)
        self.assertNotIn(TOKEN, blob)
        self.assertIn("<token>", blob)
        self.assertIn("200", blob)

    def test_off_when_unconfigured_and_short_token_warns(self):
        page = StatusPage(lambda: FLEET)
        with self.assertLogs("status_page", level="INFO") as logs:
            self.assertFalse(page.start(StatusPageConfig(False, "", "127.0.0.1", 8787)))
        self.assertIsNone(page.httpd)
        self.assertTrue(any("Status page: off" in line for line in logs.output))

        short = StatusPage(lambda: FLEET)
        self.addCleanup(short.stop)
        with self.assertLogs("status_page", level="WARNING") as logs:
            self.assertTrue(short.start(StatusPageConfig(True, "short-token", "127.0.0.1", 0)))
        self.assertTrue(any("very short" in line for line in logs.output))

    def test_bad_port_logs_and_does_not_bind(self):
        page = StatusPage(lambda: FLEET)
        cfg = config_from_env({"STATUS_PAGE_TOKEN": TOKEN, "STATUS_PAGE_PORT": "nope"})
        with self.assertLogs("status_page", level="ERROR") as logs:
            self.assertFalse(page.start(cfg))
        self.assertIsNone(page.httpd)
        self.assertTrue(any("not started" in line for line in logs.output))


class AppWiringTests(unittest.TestCase):
    def _app(self, folder: str) -> App:
        path = os.path.join(folder, "c.yaml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(
                "timezone: UTC\n"
                "poll_interval_seconds: 30\n"
                "network: {enabled: false}\n"
                "telegram: {owner_chat_id: 4242}\n"
                "miners: []\n"
            )
        return App(load_config(path), folder, dry_run=True)

    def test_page_callable_matches_telegram_status(self):
        folder = tempfile.mkdtemp()
        app = self._app(folder)
        self.addCleanup(app.store.db.close)
        self.addCleanup(app.pool.shutdown, wait=False)
        self.assertEqual(
            app.status_page.snapshot_text(),
            build_status(app.snapshot(), app.cfg, app.store, farm=app.farm),
        )
        self.assertIn("No data yet", app.bot.handlers["/status"]())
        self.assertEqual(app.bot.handlers["/status"](), app.status_page.snapshot_text())

    def test_run_serves_page_on_loopback_and_keeps_telegram(self):
        folder = tempfile.mkdtemp()
        port = _free_port()
        env = {
            "STATUS_PAGE_TOKEN": TOKEN,
            "STATUS_PAGE_BIND": "127.0.0.1",
            "STATUS_PAGE_PORT": str(port),
        }
        app = self._app(folder)
        worker_started = threading.Event()

        def runner():
            with patch.dict(os.environ, env, clear=False):
                worker_started.set()
                app.run()

        worker = threading.Thread(target=runner, daemon=True)

        def shutdown():
            app.stop.set()
            worker.join(5)
            app.status_page.stop()
            app.pool.shutdown(wait=False)
            app.store.db.close()

        worker.start()
        self.addCleanup(shutdown)
        self.assertTrue(worker_started.wait(2))
        deadline = time.monotonic() + 5
        status = None
        body = b""
        while time.monotonic() < deadline:
            try:
                status, _, body = fetch(port, f"/s/{TOKEN}/plain")
                break
            except OSError:
                time.sleep(0.05)
        self.assertEqual(status, 200)
        self.assertEqual(body.decode(), app.bot.handlers["/status"]())
        self.assertIn("No data yet", body.decode())
        self.assertEqual(app.status_page.httpd.server_address[0], "127.0.0.1")
        # A path that only looks like the Telegram command reveals nothing.
        denied, _, denied_body = fetch(port, "/status")
        self.assertEqual(denied, 401)
        self.assertNotIn(b"No data yet", denied_body)
        app.stop.set()
        worker.join(5)
        self.assertFalse(worker.is_alive())

    def test_snapshot_text_hook_uses_the_same_callable(self):
        self.assertEqual(snapshot_text(lambda: "Fleet OK"), "Fleet OK")


class ComposeTests(unittest.TestCase):
    def test_shipped_compose_files_publish_no_ports(self):
        root = os.path.dirname(os.path.dirname(__file__))
        for name in ("docker-compose.yml", "docker-compose.proxynet.yml"):
            with open(os.path.join(root, name), encoding="utf-8") as fh:
                active = "\n".join(
                    line for line in fh if line.strip() and not line.lstrip().startswith("#")
                )
            self.assertNotIn("ports:", active, name)


if __name__ == "__main__":
    unittest.main()
