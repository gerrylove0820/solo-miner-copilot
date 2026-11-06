"""Optional phone alerts through the free hosted ntfy.sh service.

Off unless NTFY_TOPIC is set. Telegram is independent: a ntfy failure never
blocks it. Messages are a short sanitized line (miner display name and the
numbers for that alert). They do not carry LAN addresses, wallets, tokens,
or the status-page link.

This is not iOS web push. The LAN status page cannot wake an iPhone.
Delivery needs the ntfy app, subscribed to the same secret topic on https://ntfy.sh.

Accepted-block detection is not implemented. Best-share text says it is a
share record, not a confirmed block.
"""
from __future__ import annotations

import logging
import os
import queue
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from . import __version__
from .format import fmt_diff, fmt_dur, fmt_hr, fmt_temp

log = logging.getLogger("ntfy")

DEFAULT_URL = "https://ntfy.sh"
_TOPIC_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_IPV4 = re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d?\d)\b")
_IPV6 = re.compile(r"\b(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}\b")
_URL = re.compile(r"\bhttps?://[^\s<>]+", re.IGNORECASE)
_BC1 = re.compile(r"\bbc1[02-9ac-hj-np-z]{8,}\b", re.IGNORECASE)
_B58 = re.compile(r"\b[13][a-km-zA-HJ-NP-Z1-9]{25,34}\b")
_STATUS_PATH = re.compile(r"/s/(?:[A-Za-z0-9_-]*)")
_TG_TOKEN = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b")
_USERINFO = re.compile(r"\b[^/\s:]{1,64}:[^/\s@]{1,128}@")

_TITLES = {
    "offline": "Miner offline",
    "recovered": "Miner back online",
    "overheat_asic": "High temperature",
    "overheat_vr": "High temperature",
    "cooled_asic": "Temperature normal",
    "cooled_vr": "Temperature normal",
    "hashrate_drop": "Hashrate drop",
    "hashrate_ok": "Hashrate recovered",
    "shares_stalled": "Shares stalled",
    "shares_ok": "Shares flowing",
    "best_diff": "New best share",
}
_PRIORITY = {
    "offline": 4,
    "overheat_asic": 4,
    "overheat_vr": 4,
    "hashrate_drop": 4,
    "shares_stalled": 4,
    "best_diff": 3,
    "recovered": 3,
    "cooled_asic": 3,
    "cooled_vr": 3,
    "hashrate_ok": 3,
    "shares_ok": 3,
}
_RETRY_CODES = {408, 429, 500, 502, 503, 504}


@dataclass(frozen=True)
class NtfySettings:
    enabled: bool
    topic: str
    base_url: str
    error: str | None = None


def settings_from_env(env: Mapping[str, str] | None = None) -> NtfySettings:
    """Read NTFY_TOPIC and optional NTFY_URL. No topic means phone push stays off."""
    src = os.environ if env is None else env
    topic = str(src.get("NTFY_TOPIC") or "").strip()
    base = str(src.get("NTFY_URL") or DEFAULT_URL).strip() or DEFAULT_URL
    if not topic:
        return NtfySettings(False, "", DEFAULT_URL, None)
    error = _settings_error(topic, base)
    if error:
        return NtfySettings(False, "", DEFAULT_URL, error)
    return NtfySettings(True, topic, base.rstrip("/"), None)


def _settings_error(topic: str, base: str) -> str | None:
    if not _TOPIC_RE.fullmatch(topic):
        return "NTFY_TOPIC must be 16 to 64 characters of letters, numbers, _ or -"
    parsed = urllib.parse.urlparse(base)
    host = parsed.hostname or ""
    if parsed.scheme != "https" or not host or parsed.username or parsed.password:
        return "NTFY_URL must be an https address with no username or password"
    return None


def sanitize(text: str, *secrets: str) -> str:
    """Drop addresses, wallets, URLs, tokens, and any configured secret."""
    out = text or ""
    for secret in secrets:
        if secret:
            out = out.replace(secret, "")
    out = _URL.sub("", out)
    out = _USERINFO.sub("", out)
    out = _TG_TOKEN.sub("", out)
    out = _STATUS_PATH.sub("", out)
    out = _BC1.sub("", out)
    out = _B58.sub("", out)
    out = _IPV4.sub("", out)
    out = _IPV6.sub("", out)
    out = " ".join(out.replace("\n", " ").split())
    return out.strip(" .:-")


def safe_name(name: str | None) -> str:
    """Miner display name, or 'miner' when the name is an address or empty."""
    raw = (name or "").strip()
    if not raw or _IPV4.search(raw) or _IPV6.search(raw) or _BC1.search(raw) or _B58.search(raw):
        return "miner"
    cleaned = sanitize(raw)
    if not cleaned or _IPV4.search(cleaned) or _IPV6.search(cleaned):
        return "miner"
    return cleaned[:64]


def msg_offline(name: str | None, polls: int, pool_port: bool) -> str:
    what = "Pool port is not accepting connections" if pool_port else "Not responding"
    return sanitize(f"OFFLINE: {safe_name(name)}. {what} for {int(polls)} polls in a row.")


def msg_recovered(name: str | None, down_seconds: float) -> str:
    return sanitize(f"BACK ONLINE: {safe_name(name)}. Down for about {fmt_dur(down_seconds)}.")


def msg_overheat(name: str | None, what: str, reading: str, limit: float, still_hot: bool) -> str:
    again = " Still hot." if still_hot else ""
    return sanitize(f"OVERHEAT: {safe_name(name)}. {what} {reading}, limit {fmt_temp(limit)}.{again}")


def msg_cooled(name: str | None, what: str, reading: str) -> str:
    return sanitize(f"TEMPERATURE NORMAL: {safe_name(name)}. {what} {reading}.")


def msg_hashrate_drop(name: str | None, now: float, pct: float, median: float, seconds: float) -> str:
    return sanitize(
        f"HASHRATE DROP: {safe_name(name)}. Now {fmt_hr(now)}, {pct:.0f}% of its "
        f"recent median {fmt_hr(median)}, for {fmt_dur(seconds)}.")


def msg_hashrate_ok(name: str | None, now: float, median: float) -> str:
    return sanitize(f"HASHRATE RECOVERED: {safe_name(name)}. Now {fmt_hr(now)}, median {fmt_hr(median)}.")


def msg_shares_stalled(name: str | None, hashrate: float | None) -> str:
    rate = fmt_hr(hashrate) if hashrate else "an unknown rate"
    return sanitize(f"SHARES STALLED: {safe_name(name)}. Still hashing at {rate}, but no new accepted shares.")


def msg_shares_ok(name: str | None) -> str:
    return sanitize(f"SHARES FLOWING: {safe_name(name)}.")


def msg_best_share(name: str | None, best: float, previous: float) -> str:
    return sanitize(
        f"NEW BEST SHARE: {safe_name(name)} hit {fmt_diff(best)} "
        f"(previous {fmt_diff(previous)}). This is a share record, not a confirmed block.")


class NtfyClient:
    """HTTPS publisher. start() is a no-op when the topic is unset."""

    def __init__(self, settings: NtfySettings | None = None, *,
                 timeout: float = 8.0, attempts: int = 3,
                 opener: Callable | None = None,
                 sleeper: Callable[[float], None] | None = None):
        self.settings = settings if settings is not None else settings_from_env()
        self.timeout = float(timeout)
        self.attempts = max(1, int(attempts))
        self._opener = opener or urllib.request.urlopen
        self._sleeper = sleeper or time.sleep
        self._lock = threading.Lock()
        self._queue: queue.Queue = queue.Queue(maxsize=50)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.settings.enabled and self.settings.topic)

    def start(self) -> bool:
        if self.settings.error:
            log.error("Phone push: off (%s). Telegram is unchanged.", self.settings.error)
            return False
        if not self.enabled:
            log.info("Phone push: off (set NTFY_TOPIC in .env to use ntfy.sh). Telegram is unchanged.")
            return False
        self._ensure_worker()
        log.info("Phone push: ON over HTTPS. The topic is not logged. Telegram still sends every alert.")
        return True

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)
        self._thread = None

    def send_alert(self, text: str, kind: str, miner: str | None = None) -> None:
        """Queue one already-sanitized alert. Failures stay in this thread."""
        del miner  # the display name is already inside text; never append a host
        if not self.enabled:
            return
        title = _TITLES.get(kind, "Miner alert")
        priority = _PRIORITY.get(kind, 3)
        self._ensure_worker()
        try:
            self._queue.put_nowait((text, title, priority, kind))
        except queue.Full:
            log.warning("Phone push queue is full; dropped kind=%s. Telegram was not affected.", kind)

    def send_test(self) -> str:
        """Synchronous test used by Telegram /testpush. No miner data in the body."""
        if self.settings.error:
            return "Phone push is off. Fix NTFY_TOPIC or NTFY_URL in .env, then restart. Telegram is unaffected."
        if not self.enabled:
            return ("Phone push is off. Add NTFY_TOPIC to .env (a long random topic from "
                    "openssl rand -hex 16), then restart. Telegram is unaffected.")
        ok = self.publish(
            "Test alert. Phone push is on. This message has no miner data.",
            "Solo Miner Copilot",
            3,
        )
        if ok:
            return "Test alert sent to your phone. If nothing arrives, open the ntfy app and check the subscription."
        return "Test alert could not be sent. Phone push failed; Telegram is unaffected."

    def publish(self, message: str, title: str, priority: int = 3) -> bool:
        if not self.enabled:
            return False
        with self._lock:
            return self._publish(message, title, priority)

    def _publish(self, message: str, title: str, priority: int) -> bool:
        topic = self.settings.topic
        body = sanitize(message, topic) or "Miner alert."
        safe_title = sanitize(title, topic) or "Miner alert"
        url = self.settings.base_url.rstrip("/") + "/" + topic
        data = body.encode("utf-8")
        headers = {
            "Title": safe_title[:200],
            "Priority": str(int(priority)),
            "User-Agent": f"solo-miner-copilot/{__version__}",
        }
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        for attempt in range(1, self.attempts + 1):
            try:
                with self._opener(req, timeout=self.timeout) as resp:
                    resp.read()
                    code = getattr(resp, "status", 200)
                if code < 400:
                    return True
                retry = code in _RETRY_CODES
                log.warning("Phone push failed (attempt %s/%s): HTTP %s", attempt, self.attempts, code)
            except urllib.error.HTTPError as exc:
                retry = exc.code in _RETRY_CODES
                log.warning("Phone push failed (attempt %s/%s): HTTP %s", attempt, self.attempts, exc.code)
            except Exception as exc:  # noqa: BLE001
                retry = True
                log.warning("Phone push failed (attempt %s/%s): %s", attempt, self.attempts, type(exc).__name__)
            if not retry or attempt == self.attempts:
                return False
            self._sleeper(min(2.0, 0.4 * attempt))
        return False

    def _ensure_worker(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="ntfy", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=0.3)
            except queue.Empty:
                continue
            try:
                message, title, priority, kind = item
                if not self.publish(message, title, priority):
                    log.warning("Phone push did not deliver kind=%s. Telegram was not affected.", kind)
            except Exception:  # noqa: BLE001
                log.warning("Phone push worker failed. Telegram was not affected.")
            finally:
                self._queue.task_done()
