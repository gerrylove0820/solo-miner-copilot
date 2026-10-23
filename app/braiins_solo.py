"""Braiins Solo (solo.braiins.com, ckpool-based) public stats: address validation,
rate-limited HTTP client and JSON parsing.

Endpoints (no auth, read-only):
  GET /users/<btc_address>  -> pretty-printed JSON, HTTP 404 if the pool doesn't know it
  GET /pool.status          -> several JSON objects, one per line (runtime/lastupdate,
                               pool hashrates, network_diff, notable_shares, ...)

Real /users/<addr> shape (checked live 2026-10-18):
  {"hashrate1m": "28.4P", "hashrate5m": ..., "hashrate1hr": ..., "hashrate1d": ...,
   "hashrate7d": ..., "lastshare": <unix>, "workers": <int, connected count - unreliable>,
   "shares": .., "rejected": .., "bestshare": <float, current round>,
   "bestever": <int, all-time>, "authorised": <unix>, "max_diff", "avg_diff",
   "worker": [{"workername": "<addr>.<name>", "hashrate1m".."hashrate7d", "lastshare",
               "shares", "rejected", "bestshare", "bestever", "conn_num", "useragent", ...}],
   "notable_shares": [...]}
Hashrates are strings in H/s with an SI suffix ("697T", "359G", "0").
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

from . import __version__
from .collectors import num, parse_diff

log = logging.getLogger("braiins_solo")

USER_AGENT = f"solo-miner-copilot/{__version__} (pool-watch)"


# ----------------------------------------------------------------- privacy helper
def trunc_addr(addr: str | None) -> str:
    """bc1qxyz...abcd -> 'bc1q…abcd' (safe to log / show to the owner)."""
    if not addr:
        return "?"
    return addr if len(addr) <= 10 else f"{addr[:4]}…{addr[-4:]}"


# ----------------------------------------------------------------- address validation
_BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_BECH32_CONST, _BECH32M_CONST = 1, 0x2BC830A3


def _bech32_polymod(values) -> int:
    gen = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    chk = 1
    for v in values:
        b = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ v
        for i in range(5):
            chk ^= gen[i] if ((b >> i) & 1) else 0
    return chk


def _valid_bech32_segwit(addr: str) -> bool:
    if len(addr) < 14 or len(addr) > 74 or not addr.startswith("bc1"):
        return False
    data = addr[3:]
    if any(c not in _BECH32_CHARSET for c in data) or len(data) < 7:
        return False
    vals = [_BECH32_CHARSET.index(c) for c in data]
    hrp = [ord(c) >> 5 for c in "bc"] + [0] + [ord(c) & 31 for c in "bc"]
    const = _bech32_polymod(hrp + vals)
    witver = vals[0]
    if witver > 16:
        return False
    if (witver == 0 and const != _BECH32_CONST) or (witver > 0 and const != _BECH32M_CONST):
        return False
    # convert 5-bit program to bytes
    acc, bits, prog = 0, 0, []
    for v in vals[1:-6]:
        acc = (acc << 5) | v
        bits += 5
        while bits >= 8:
            bits -= 8
            prog.append((acc >> bits) & 0xFF)
    if bits >= 5 or (acc << (8 - bits)) & 0xFF and bits:
        return False
    if not 2 <= len(prog) <= 40:
        return False
    return not (witver == 0 and len(prog) not in (20, 32))


def _valid_base58check(addr: str) -> bool:
    if not 26 <= len(addr) <= 35 or any(c not in _B58 for c in addr):
        return False
    n = 0
    for c in addr:
        n = n * 58 + _B58.index(c)
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    raw = b"\x00" * (len(addr) - len(addr.lstrip("1"))) + raw
    if len(raw) != 25:
        return False
    payload, chk = raw[:-4], raw[-4:]
    if hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4] != chk:
        return False
    return payload[0] == (0x00 if addr[0] == "1" else 0x05)


def normalize_address(text: str | None) -> str | None:
    """Return a canonical mainnet BTC address, or None if it isn't one.

    Accepts 'bitcoin:' URIs, a '.workername' suffix (as typed in miner pool settings)
    and all-uppercase bech32 (QR codes)."""
    if not text:
        return None
    s = text.strip().strip("<>\"'`")
    if s.lower().startswith("bitcoin:"):
        s = s[8:]
    s = s.split("?")[0].split(".")[0].strip()
    if not s:
        return None
    if s[:3].lower() == "bc1":
        if s != s.lower() and s != s.upper():
            return None  # mixed case is invalid bech32
        s = s.lower()
        return s if _valid_bech32_segwit(s) else None
    if s[0] in "13":
        return s if _valid_base58check(s) else None
    return None


ADDRESS_LIKE = re.compile(r"^(bitcoin:)?(bc1[0-9a-zA-Z]{8,90}|[13][1-9A-HJ-NP-Za-km-z]{20,40})(\.\S*)?$")


def looks_like_address(text: str) -> bool:
    """Cheap pre-check used to treat a bare message as /watch."""
    return bool(ADDRESS_LIKE.match((text or "").strip()))


# ----------------------------------------------------------------- data
def hr_ghs(v) -> float | None:
    """'697T' (H/s with SI suffix) -> GH/s. '0' -> 0.0. Garbage -> None."""
    if v is None:
        return None
    if isinstance(v, str) and v.strip().upper().endswith("Z"):
        f = num(v.strip()[:-1])
        return f * 1e21 / 1e9 if f is not None else None
    h = parse_diff(v)
    return h / 1e9 if h is not None else None


@dataclass
class WorkerStat:
    name: str
    hr_1m: float | None = None    # GH/s
    hr_5m: float | None = None
    hr_1hr: float | None = None
    hr_1d: float | None = None
    hr_7d: float | None = None
    lastshare: float | None = None
    best_share: float | None = None  # current round
    best_ever: float | None = None


@dataclass
class AddressStat:
    address: str
    status: str = "error"           # ok | not_found | error
    error: str | None = None
    fetched_at: float = 0.0
    hr_1m: float | None = None
    hr_5m: float | None = None
    hr_1hr: float | None = None
    hr_1d: float | None = None
    hr_7d: float | None = None
    lastshare: float | None = None
    best_share: float | None = None
    best_ever: float | None = None
    workers: list[WorkerStat] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def best(self) -> float | None:
        vals = [v for v in (self.best_ever, self.best_share) if v]
        return max(vals) if vals else None


def worker_short_name(address: str, workername: str) -> str:
    w = str(workername or "")
    if w.startswith(address):
        w = w[len(address):].lstrip("._")
    return w or "(default)"


def parse_user(address: str, d: dict, now: float | None = None) -> AddressStat:
    if not isinstance(d, dict):
        raise ValueError("unexpected /users payload")
    st = AddressStat(address=address, status="ok", fetched_at=now or time.time())
    st.hr_1m, st.hr_5m = hr_ghs(d.get("hashrate1m")), hr_ghs(d.get("hashrate5m"))
    st.hr_1hr, st.hr_1d = hr_ghs(d.get("hashrate1hr")), hr_ghs(d.get("hashrate1d"))
    st.hr_7d = hr_ghs(d.get("hashrate7d"))
    st.lastshare = num(d.get("lastshare")) or None
    st.best_share, st.best_ever = num(d.get("bestshare")), num(d.get("bestever"))
    names = set()
    for w in d.get("worker") or []:
        if not isinstance(w, dict):
            continue
        name = worker_short_name(address, w.get("workername"))
        base, i = name, 2
        while name in names:   # defensive: duplicate worker names
            name = f"{base}#{i}"
            i += 1
        names.add(name)
        st.workers.append(WorkerStat(
            name=name, hr_1m=hr_ghs(w.get("hashrate1m")), hr_5m=hr_ghs(w.get("hashrate5m")),
            hr_1hr=hr_ghs(w.get("hashrate1hr")), hr_1d=hr_ghs(w.get("hashrate1d")),
            hr_7d=hr_ghs(w.get("hashrate7d")), lastshare=num(w.get("lastshare")) or None,
            best_share=num(w.get("bestshare")), best_ever=num(w.get("bestever"))))
    return st


def parse_pool_status(text: str) -> dict:
    """pool.status is several JSON objects, one per line -> merged dict."""
    out: dict = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.update(obj)
    if not out:
        raise ValueError("empty/invalid pool.status")
    return out


def notable_addresses(pool_status: dict) -> list[str]:
    """Addresses seen in pool.status notable_shares (worker field minus '.worker')."""
    out = []
    for s in pool_status.get("notable_shares") or []:
        a = normalize_address(str(s.get("worker") or s.get("username") or ""))
        if a and a not in out:
            out.append(a)
    return out


# ----------------------------------------------------------------- HTTP client
class ApiBusy(RuntimeError):
    """Raised when a caller can't wait for the rate limiter / back-off."""


def _urllib_fetch(url: str, timeout: float) -> tuple[int, bytes, dict]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "application/json, text/plain"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(2_000_000), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, b"", dict(e.headers or {})


class SoloClient:
    """Thread-safe, rate-limited client. One request at a time, >= min_interval
    seconds apart; exponential back-off after errors (and honours 429 Retry-After)."""

    def __init__(self, base_url: str = "https://solo.braiins.com", timeout: float = 15,
                 min_interval: float = 2.0, max_backoff: float = 900,
                 fetch: Callable[[str, float], tuple[int, bytes, dict]] | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.base = base_url.rstrip("/")
        self.timeout = float(timeout)
        self.min_interval = max(0.0, float(min_interval))
        self.max_backoff = float(max_backoff)
        self._fetch = fetch or _urllib_fetch
        self._clock, self._sleep = clock, sleep
        self._lock = threading.Lock()
        self._next_ok = 0.0
        self.backoff = 0.0
        self.consecutive_errors = 0
        self.requests = 0

    def wait_seconds(self) -> float:
        return max(0.0, self._next_ok - self._clock())

    def _fail(self, retry_after: float | None = None) -> None:
        self.consecutive_errors += 1
        self.backoff = min(self.max_backoff, max(self.min_interval * 2, self.backoff * 2 or 5.0))
        delay = max(self.backoff, retry_after or 0)
        self._next_ok = self._clock() + delay

    def _get(self, path: str, max_wait: float | None = None) -> tuple[int, bytes]:
        lock_timeout = -1 if max_wait is None else max_wait + self.timeout + self.min_interval
        if not self._lock.acquire(timeout=lock_timeout):
            raise ApiBusy("Braiins Solo client busy")
        try:
            wait = self.wait_seconds()
            if max_wait is not None and wait > max_wait:
                raise ApiBusy(f"Braiins Solo API backing off ({wait:.0f}s)")
            if wait > 0:
                self._sleep(wait)
            self.requests += 1
            try:
                code, body, headers = self._fetch(self.base + path, self.timeout)
            except Exception as e:  # noqa: BLE001 - network errors
                self._fail()
                raise RuntimeError(f"network error: {e.__class__.__name__}: {e}") from None
            if code == 429 or code >= 500:
                ra = num((headers or {}).get("Retry-After"))
                self._fail(ra)
                raise RuntimeError(f"HTTP {code}")
            self.consecutive_errors = 0
            self.backoff = 0.0
            self._next_ok = self._clock() + self.min_interval
            return code, body
        finally:
            self._lock.release()

    def user(self, address: str, max_wait: float | None = None, now: float | None = None) -> AddressStat:
        """Never raises for API problems (except ApiBusy): returns status error/not_found."""
        try:
            code, body = self._get(f"/users/{address}", max_wait)
        except ApiBusy:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("Braiins Solo %s: %s", trunc_addr(address), e)
            return AddressStat(address, "error", str(e), now or time.time())
        if code == 404:
            return AddressStat(address, "not_found", "unknown to Braiins Solo", now or time.time())
        if code != 200:
            return AddressStat(address, "error", f"HTTP {code}", now or time.time())
        try:
            return parse_user(address, json.loads(body.decode("utf-8", errors="replace")), now)
        except Exception as e:  # noqa: BLE001
            log.warning("Braiins Solo %s: bad JSON (%s)", trunc_addr(address), e)
            return AddressStat(address, "error", "bad JSON", now or time.time())

    def pool_status(self, max_wait: float | None = None) -> dict:
        code, body = self._get("/pool.status", max_wait)
        if code != 200:
            raise RuntimeError(f"pool.status HTTP {code}")
        return parse_pool_status(body.decode("utf-8", errors="replace"))
