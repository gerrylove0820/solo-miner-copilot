"""Private LAN status page for the same snapshot as Telegram /status.

Turn it on with STATUS_PAGE_TOKEN in .env. Leave that unset and nothing
listens, so existing installs keep working with Telegram only.

The page is for a phone on the same home network. It is not published in
docker-compose, and it should not be port-forwarded to the internet.

This page is not phone push. iOS will not deliver web push from a LAN HTTP
page. Phone alerts, when enabled, go out through ntfy.sh (see ntfy.py) and
use their own short text, not this page and not the Telegram message.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import logging
import os
import socket
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse

from . import __version__

log = logging.getLogger("status_page")

DEFAULT_PORT = 8787
DEFAULT_BIND = "0.0.0.0"
REFRESH_SECONDS = 60
_MAX_TOKEN_LEN = 512
_SHORT_TOKEN = 16

# Static page chrome. The fleet text is dropped in already escaped.
_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="referrer" content="no-referrer">
<meta name="theme-color" content="#111111">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Miners">
<meta http-equiv="refresh" content="{refresh}">
<title>Solo Miner Copilot</title>
<style>
  :root { color-scheme: dark; }
  body {
    margin: 0;
    background: #111;
    color: #f2f2f2;
    font: 18px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    padding: max(16px, env(safe-area-inset-top)) max(16px, env(safe-area-inset-right))
             max(24px, env(safe-area-inset-bottom)) max(16px, env(safe-area-inset-left));
  }
  h1 { font-size: 1.15rem; font-weight: 650; margin: 0 0 6px; }
  p.note { color: #9a9a9a; font-size: 0.85rem; margin: 0 0 14px; }
  pre {
    white-space: pre-wrap;
    word-wrap: break-word;
    margin: 0;
    font: inherit;
  }
</style>
</head>
<body>
<h1>Solo Miner Copilot</h1>
<p class="note">v{version} · refreshes every minute · same snapshot as Telegram /status</p>
<pre>{body}</pre>
</body>
</html>
"""


@dataclass(frozen=True)
class StatusPageConfig:
    enabled: bool
    token: str
    bind: str
    port: int
    error: str | None = None


def config_from_env(env: Mapping[str, str] | None = None) -> StatusPageConfig:
    """Read STATUS_PAGE_TOKEN / PORT / BIND. No token means the page stays off."""
    src = os.environ if env is None else env
    token = str(src.get("STATUS_PAGE_TOKEN") or "").strip()
    bind = str(src.get("STATUS_PAGE_BIND") or DEFAULT_BIND).strip() or DEFAULT_BIND
    port, port_error = _parse_port(src.get("STATUS_PAGE_PORT"))
    error = None
    if any(c.isspace() for c in bind) or "/" in bind:
        error = "STATUS_PAGE_BIND must be an address like 0.0.0.0 or 127.0.0.1"
    elif port_error:
        error = port_error
    enabled = bool(token) and error is None
    return StatusPageConfig(enabled, token, bind, port, error)


def snapshot_text(status_text: Callable[[], str]) -> str:
    """Fleet snapshot. The page and any future phone-push channel should call this.

    `status_text` is the same builder Telegram /status uses. Phone push is not
    implemented here.
    """
    text = status_text()
    return text if isinstance(text, str) else str(text)


def tokens_match(provided: str, expected: str) -> bool:
    """True when the two secrets are equal. Length is not compared directly."""
    if not provided or not expected:
        return False
    if len(provided) > _MAX_TOKEN_LEN or len(expected) > _MAX_TOKEN_LEN:
        return False
    try:
        left = hashlib.sha256(provided.encode("utf-8")).digest()
        right = hashlib.sha256(expected.encode("utf-8")).digest()
    except UnicodeError:
        return False
    return hmac.compare_digest(left, right)


def redact(text: str, token: str) -> str:
    """Hide the secret if a request line would otherwise land in the logs."""
    if not text or not token:
        return text
    out = text.replace(token, "<token>")
    quoted = quote(token, safe="")
    if quoted and quoted != token:
        out = out.replace(quoted, "<token>")
    return out


def _parse_port(raw) -> tuple[int, str | None]:
    if raw is None or str(raw).strip() == "":
        return DEFAULT_PORT, None
    try:
        port = int(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_PORT, "STATUS_PAGE_PORT must be a number from 1 to 65535"
    if 1 <= port <= 65535:
        return port, None
    return DEFAULT_PORT, "STATUS_PAGE_PORT must be a number from 1 to 65535"


def _bearer(header: str | None) -> str | None:
    if not header:
        return None
    scheme, _, rest = header.strip().partition(" ")
    if scheme.lower() == "bearer" and rest.strip():
        return rest.strip()
    return None


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, status_text: Callable[[], str], token: str, refresh_seconds: int):
        self.status_text = status_text
        self.token = token
        self.refresh_seconds = refresh_seconds
        super().__init__(addr, _Handler)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def version_string(self) -> str:
        return "SoloMinerCopilot"

    def do_GET(self):
        self._respond(head_only=False)

    def do_HEAD(self):
        self._respond(head_only=True)

    def do_POST(self):
        self._send(405, "text/plain; charset=utf-8", b"Method not allowed\n", head_only=False)

    do_PUT = do_POST
    do_DELETE = do_POST

    def log_message(self, fmt, *args):
        try:
            msg = fmt % args
        except Exception:  # noqa: BLE001
            msg = " ".join(str(a) for a in args)
        log.info("%s", redact(msg, getattr(self.server, "token", "")))

    def _respond(self, head_only: bool):
        if not self._authorized():
            self._send(401, "text/plain; charset=utf-8", b"Unauthorized\n", head_only)
            return
        view = self._view()
        if view is None:
            self._send(404, "text/plain; charset=utf-8", b"Not found\n", head_only)
            return
        try:
            text = snapshot_text(self.server.status_text)
        except Exception:  # noqa: BLE001
            log.exception("status page snapshot failed")
            self._send(500, "text/plain; charset=utf-8",
                       b"Status unavailable right now. Try again in a minute.\n", head_only)
            return
        if view == "plain":
            body = text.encode("utf-8")
            self._send(200, "text/plain; charset=utf-8", body, head_only)
            return
        # replace(), not str.format(): miner names can contain braces.
        page = (_PAGE
                .replace("{refresh}", str(int(self.server.refresh_seconds)))
                .replace("{version}", html.escape(__version__))
                .replace("{body}", html.escape(text)))
        self._send(200, "text/html; charset=utf-8", page.encode("utf-8"), head_only)

    def _authorized(self) -> bool:
        expected = self.server.token
        ok = False
        for candidate in self._candidates():
            ok = tokens_match(candidate, expected) or ok
        return ok

    def _candidates(self) -> list[str]:
        parsed = urlparse(self.path)
        found: list[str] = []
        parts = [unquote(p) for p in parsed.path.split("/") if p]
        if len(parts) >= 2 and parts[0] == "s" and parts[1]:
            found.append(parts[1])
        for value in parse_qs(parsed.query).get("token", [])[:1]:
            if value:
                found.append(value)
        bearer = _bearer(self.headers.get("Authorization"))
        if bearer:
            found.append(bearer)
        return found

    def _view(self) -> str | None:
        parts = [unquote(p) for p in urlparse(self.path).path.split("/") if p]
        if not parts:
            return "html"
        if parts[0] != "s":
            return None
        if len(parts) == 2:
            return "html"
        if len(parts) == 3 and parts[2] == "plain":
            return "plain"
        return None

    def _send(self, code: int, content_type: str, body: bytes, head_only: bool):
        self.close_connection = True
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Robots-Tag", "noindex, nofollow")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; style-src 'unsafe-inline'; img-src 'none'; script-src 'none'")
        self.send_header("Connection", "close")
        self.end_headers()
        if not head_only:
            self.wfile.write(body)


class StatusPage:
    """Optional HTTP server. start() does nothing useful when the token is unset."""

    def __init__(self, status_text: Callable[[], str]):
        self._status_text = status_text
        self.httpd: _Server | None = None
        self._thread: threading.Thread | None = None

    def snapshot_text(self) -> str:
        return snapshot_text(self._status_text)

    def start(self, cfg: StatusPageConfig | None = None) -> bool:
        if self.httpd is not None:
            return True
        cfg = config_from_env() if cfg is None else cfg
        if cfg.error:
            log.error("Status page: not started (%s). Telegram keeps running.", cfg.error)
            return False
        if not cfg.enabled or not cfg.token:
            log.info("Status page: off (set STATUS_PAGE_TOKEN in .env to enable the private LAN page)")
            return False
        if len(cfg.token) < _SHORT_TOKEN:
            log.warning("STATUS_PAGE_TOKEN is very short. Use at least %s random characters.", _SHORT_TOKEN)
        try:
            self.httpd = _Server((cfg.bind, cfg.port), self._status_text, cfg.token, REFRESH_SECONDS)
        except OSError as e:
            log.error("Status page: not started (could not listen on %s:%s: %s). Telegram keeps running.",
                      cfg.bind, cfg.port, e)
            self.httpd = None
            return False
        self._thread = threading.Thread(target=self.httpd.serve_forever, name="status-page", daemon=True)
        self._thread.start()
        host, port = self.httpd.server_address[:2]
        if not _wait_until_accepting(host, port):
            log.error("Status page: listener on %s:%s did not come up. Telegram keeps running.", host, port)
            self.stop()
            return False
        where = "all interfaces of this machine" if cfg.bind in ("0.0.0.0", "::") else cfg.bind
        log.info("Status page: ON port %s (%s). From a phone on the same Wi-Fi open "
                 "http://<lan-ip>:%s/s/<token> — do not port-forward %s.",
                 port, where, port, port)
        return True

    def stop(self) -> None:
        httpd = self.httpd
        if httpd is None:
            return
        self.httpd = None
        httpd.shutdown()
        httpd.server_close()
        thread = self._thread
        self._thread = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)


def _wait_until_accepting(host: str, port: int, timeout: float = 3.0) -> bool:
    probe = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((probe, port), timeout=0.2):
                return True
        except OSError:
            time.sleep(0.02)
    return False
