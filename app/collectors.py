"""Miner collectors and payload parsers.

Supported types (config.yaml `type:`):
  axeos     - Bitaxe / AxeOS (ESP-Miner) HTTP GET /api/system/info
  nerdqaxe  - NerdQaxe++ / NerdOS (AxeOS-compatible fork, same endpoint)
  braiins   - Braiins OS / BOSminer CGMiner-compatible TCP API on port 4028
              (summary + temps + fans + tunerstatus), best effort
  avalon    - Canaan Avalon (Nano 3S, Q, ...) CGMiner TCP API on port 4028
              (summary + estats "MM ID" string parsing)
  cgminer   - any generic CGMiner-compatible miner (summary only)
  stratum   - just checks that a TCP port (e.g. the local pool) accepts connections

All hashrates are normalised to GH/s.

AxeOS `hashRate` is the instantaneous register and is the field that spikes
(counter overflow / unlocked hashrate monitor on some firmware and multi-chip
boards). The daily average uses the smoothed windows instead, and a reading
that is a clean power-of-ten above `expectedHashrate` is scaled back to GH/s.
"""
from __future__ import annotations

import json
import logging
import math
import re
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass, field

log = logging.getLogger("collectors")

USER_AGENT = "solo-miner-copilot/1.0"


@dataclass
class Reading:
    name: str
    kind: str
    host: str
    ok: bool = False
    error: str | None = None
    stats_available: bool = True      # False = device answers but no stats API
    hashrate_ghs: float | None = None  # "smoothed" hashrate used for averages
    hashrate_now_ghs: float | None = None  # short-window hashrate for drop detection
    expected_ghs: float | None = None
    temp_c: float | None = None        # hottest ASIC / chip temperature
    asic_temps: list[float] = field(default_factory=list)  # per-chip, sensor order
    vr_temp_c: float | None = None     # voltage regulator temperature (AxeOS)
    board_temp_c: float | None = None  # board temperature (Braiins)
    ambient_c: float | None = None     # inlet / ambient (Avalon)
    fan_rpm: float | None = None
    power_w: float | None = None
    accepted: int | None = None
    rejected: int | None = None
    best_diff: float | None = None
    uptime_s: float | None = None
    model: str | None = None
    note: str | None = None
    extra: dict = field(default_factory=dict)


# ----------------------------------------------------------------- helpers
def num(v) -> float | None:
    """Tolerant float conversion. Returns None for missing / garbage / NaN."""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(str(v).strip().rstrip("%"))
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def pos(v) -> float | None:
    """Like num() but treats <= 0 as 'no sensor / unknown'."""
    f = num(v)
    return f if f is not None and f > 0 else None


_SUFFIX = {"": 1, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "P": 1e15, "E": 1e18}


def parse_diff(v) -> float | None:
    """Parse a difficulty that may be a number or a string like '4.29G' / '1.2 M'."""
    if v is None:
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v) if v >= 0 else None
    m = re.match(r"^\s*([0-9]*\.?[0-9]+(?:[eE][+-]?\d+)?)\s*([kKmMgGtTpPeE]?)", str(v))
    if not m:
        return None
    return float(m.group(1)) * _SUFFIX[m.group(2).upper()]


def first(d: dict, *keys):
    for k in keys:
        if k in d and d[k] is not None and d[k] != "":
            return d[k]
    return None


# ----------------------------------------------------------------- transport
def http_get_json(url: str, timeout: float):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def parse_cgminer_json(text: str) -> dict:
    """Parse a CGMiner API reply. Handles trailing NULs and known malformed JSON."""
    text = text.replace("\x00", "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    a, b = text.find("{"), text.rfind("}")
    if a == -1 or b == -1:
        raise ValueError("no JSON object in reply")
    body = text[a:b + 1]
    body = re.sub(r"}\s*{", "},{", body)        # cgminer stats bug: '}{' without comma
    body = re.sub(r"[\x01-\x1f]", " ", body)     # stray control chars
    return json.loads(body, strict=False)


def _cgminer_once(host: str, port: int, payload: str, timeout: float) -> dict:
    with socket.create_connection((host, port), timeout=timeout) as s:
        s.settimeout(timeout)
        s.sendall(payload.encode())
        chunks = []
        while True:
            try:
                c = s.recv(65536)
            except socket.timeout:
                break
            if not c:
                break
            chunks.append(c)
    text = b"".join(chunks).decode("utf-8", errors="replace")
    if not text.strip("\x00 \r\n"):
        raise ValueError("empty reply")
    data = parse_cgminer_json(text)
    st = data.get("STATUS") or [{}]
    st0 = st[0] if isinstance(st, list) and st else (st if isinstance(st, dict) else {})
    if isinstance(st0, dict) and st0.get("STATUS") in ("E", "F"):
        raise ValueError(f"API error: {st0.get('Msg')}")
    return data


def cgminer_command(host: str, port: int, command: str, timeout: float) -> dict:
    """Send one command to a CGMiner-compatible API.

    Tries the JSON form first ({"command": "summary"}); if the reply is empty or
    unparseable, retries with the plain-text form ("summary"). Network errors
    (refused / timeout / unreachable) are raised immediately.
    """
    try:
        return _cgminer_once(host, port, json.dumps({"command": command}), timeout)
    except ValueError:
        return _cgminer_once(host, port, command, timeout)


def http_alive(host: str, port: int, timeout: float) -> bool:
    """True if something speaks HTTP on host:port (any status code counts)."""
    url = f"http://{host}:{port}/"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status > 0
    except urllib.error.HTTPError:
        return True
    except Exception:  # noqa: BLE001
        return False


def tcp_port_open(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# A sustained Bitaxe / NerdQAxe does not hash at several times the hashrate the
# firmware itself expects at the current clock. Anything above this, after a
# power-of-ten unit correction, is a bad sample and must not enter averages.
HASHRATE_EXPECTED_FACTOR = 3.0
# Isolated spikes of ~100x show up against the miner's own recent median.
# 20x still allows a real recovery from a deep underclock once the guard is armed.
HASHRATE_MEDIAN_FACTOR = 20.0
HASHRATE_MEDIAN_MIN_SAMPLES = 20
# Summary-time trim. Tighter than the ingest guard so one stored spike cannot
# move the mean, while a real overclock (well under 5x) still counts.
HASHRATE_SUMMARY_FACTOR = 5.0

# GH/s times these factors are the wrong-unit readings we know how to undo:
# 10 and 100 cover the register-scale bug seen on some higher-hashrate Bitaxes,
# 1e3 is MH/s stored as GH/s, 1e6 is kH/s, 1e9 is raw H/s.
_HASHRATE_SCALE_FACTORS = (10, 100, 1_000, 1_000_000, 1_000_000_000)


def axeos_expected_ghs(d: dict) -> float | None:
    """Expected hashrate in GH/s from an AxeOS / NerdOS system-info payload.

    Official AxeOS sends `expectedHashrate` in GH/s. NerdOS often omits it;
    the same quantity is frequency(MHz) * smallCoreCount * asicCount / 1000.
    `smallCoreCount` is per chip, so `asicCount` has to be present too —
    assuming 1 ASIC on a multi-chip board would invent a false anchor.
    """
    reported = pos(first(d, "expectedHashrate", "expectedHashRate"))
    if reported:
        return reported
    freq = pos(first(d, "frequency", "actualFrequency", "freq"))
    cores = pos(first(d, "smallCoreCount", "small_core_count"))
    asics = pos(first(d, "asicCount", "asic_count", "chipCount"))
    if freq and cores and asics:
        return freq * cores * asics / 1000.0
    return None


def _pos_num(v) -> float | None:
    f = num(v)
    return f if f is not None and f > 0 else None


def unit_divisor_if_high(value: float, expected: float) -> float:
    """Divisor that brings an inflated reading back near `expected`, or 1.

    Only scales readings that are already above the expected ceiling. A low
    reading is a real stall or underclock and must be kept as-is — multiplying
    it up to "expected" would hide a hashrate drop.
    """
    if value <= 0 or expected <= 0 or value <= expected * HASHRATE_EXPECTED_FACTOR:
        return 1.0
    best = 1.0
    best_err = None
    for factor in _HASHRATE_SCALE_FACTORS:
        ratio = (value / factor) / expected
        if 0.6 <= ratio <= 1.6:
            err = abs(math.log(ratio))
            if best_err is None or err < best_err:
                best_err = err
                best = float(factor)
    return best


def normalize_high_hashrate(value: float | None, expected: float | None) -> float | None:
    """GH/s sample, or None when it is still impossibly above `expected`.

    `expected` is GH/s (AxeOS expectedHashrate, or the NerdOS clock formula).
    A value of 0 or less is returned unchanged. With no anchor the value is
    returned unchanged so legacy payloads that only have `hashRate` keep working.
    """
    if value is None:
        return None
    if value <= 0 or not expected or expected <= 0:
        return value
    scaled = value / unit_divisor_if_high(value, expected)
    if scaled > expected * HASHRATE_EXPECTED_FACTOR:
        return None
    return scaled


def _axeos_asic_temps(d: dict) -> list[float]:
    """Per-ASIC temperatures in sensor order.

    ESP-Miner SystemInfo reports `temp` and, on a second sensor, `temp2`.
    `temp3`..`temp8` are included when present. An `asicTemps` list of live
    per-chip readings wins over those scalars so an average is not counted
    twice. Missing, blank, null, or <= 0 means that sensor is absent.
    """
    listed = d.get("asicTemps")
    if isinstance(listed, (list, tuple)):
        temps = [t for t in (pos(v) for v in listed) if t is not None]
        if temps:
            return temps
    primary = pos(first(d, "temp", "asicTemp", "chipTemp"))
    temps = [primary] if primary is not None else []
    for n in range(2, 9):
        key = f"temp{n}"
        if key not in d:
            continue
        t = pos(d.get(key))
        if t is not None:
            temps.append(t)
    return temps


# ----------------------------------------------------------------- AxeOS / NerdQaxe
def _axeos_window(d: dict, *keys) -> float | None:
    """First positive hashrate window. Zero means 'not filled in yet'."""
    return _pos_num(first(d, *keys))


def parse_axeos(d: dict, r: Reading) -> Reading:
    """Field names from ESP-Miner openapi.yaml (SystemInfo). All optional.

    `hashrate_ghs` is the smoothed value used for daily averages. Prefer the
    firmware's own 10-minute / 1-hour windows over instantaneous `hashRate`,
    which is the field that spikes to ~100x on some AxeOS builds while the
    UI's 1m/10m/1h averages stay correct. Each window is scaled down when it
    is a clean power-of-ten above the expected GH/s rate (MH/s, raw H/s, or
    the 100x register scale), and dropped when it is still impossible.
    """
    r.ok = True
    r.expected_ghs = axeos_expected_ghs(d)
    windows = (
        _axeos_window(d, "hashRate_10m", "hashRate10m", "hashrate10m"),
        _axeos_window(d, "hashRate_1h", "hashRate1h", "hashrate1h"),
        _axeos_window(d, "hashRate_1d", "hashRate1d", "hashrate1d"),
        _axeos_window(d, "hashRate_1m", "hashRate1m", "hashrate1m"),
        _axeos_window(d, "hashRate", "hashrate"),
    )
    smoothed = None
    for raw in windows:
        got = normalize_high_hashrate(raw, r.expected_ghs)
        if got is not None:
            smoothed = got
            break
    if smoothed is None and any(num(d.get(k)) == 0 for k in
                                ("hashRate", "hashrate", "hashRate_10m", "hashRate_1m")):
        smoothed = 0.0
    # 0 is a real "not hashing" reading for drop detection. Only a missing
    # short window falls through to the smoothed value.
    now_raw = num(first(d, "hashRate_1m", "hashRate1m", "hashrate1m"))
    if now_raw is None:
        now_raw = num(first(d, "hashRate", "hashrate"))
    if now_raw is not None and now_raw <= 0:
        now = 0.0
    else:
        now = normalize_high_hashrate(now_raw, r.expected_ghs)
        if now is None:
            now = smoothed
    reported = _axeos_window(d, "hashRate", "hashrate", "hashRate_10m", "hashRate_1m")
    if reported and smoothed is None:
        log.warning("%s: dropped impossible hashrate %.4g (expected %.4g GH/s)",
                    r.name, reported, r.expected_ghs)
    elif reported and smoothed and smoothed < reported / HASHRATE_EXPECTED_FACTOR:
        log.debug("%s: rescaled hashrate %.4g -> %.4g GH/s (expected %.4g)",
                  r.name, reported, smoothed, r.expected_ghs)
    r.hashrate_ghs = smoothed
    r.hashrate_now_ghs = now
    r.asic_temps = _axeos_asic_temps(d)
    r.temp_c = max(r.asic_temps) if r.asic_temps else None
    r.vr_temp_c = pos(first(d, "vrTemp", "vrtemp"))
    r.fan_rpm = num(first(d, "fanrpm", "fanRpm"))
    r.power_w = pos(d.get("power"))
    acc = num(d.get("sharesAccepted"))
    rej = num(d.get("sharesRejected"))
    r.accepted = int(acc) if acc is not None else None
    r.rejected = int(rej) if rej is not None else None
    r.best_diff = parse_diff(first(d, "bestDiff", "bestDifficulty"))
    r.uptime_s = num(first(d, "uptimeSeconds", "uptime"))
    model = first(d, "deviceModel", "ASICModel", "boardVersion")
    r.model = str(model) if model is not None else None
    nd = num(d.get("networkDifficulty"))
    if nd:
        r.extra["network_difficulty"] = nd
    if d.get("hostname"):
        r.extra["hostname"] = d.get("hostname")
    for k in ("overheat_mode", "power_fault", "hardware_fault"):
        if d.get(k):
            r.extra[k] = d.get(k)
    if d.get("miningPaused") is True:
        r.note = "mining paused"
    return r


def collect_axeos(m: dict, timeout: float) -> Reading:
    r = Reading(m["name"], m["type"], m["host"])
    port = m.get("port", 80)
    url = f"http://{m['host']}:{port}/api/system/info" if port != 80 else f"http://{m['host']}/api/system/info"
    try:
        return parse_axeos(http_get_json(url, timeout), r)
    except Exception as e:  # noqa: BLE001
        r.error = f"{type(e).__name__}: {e}"
        return r


# ----------------------------------------------------------------- CGMiner summary
def parse_cgminer_summary(resp: dict, r: Reading) -> Reading:
    s = (resp.get("SUMMARY") or [{}])[0]
    def mhs(*keys):
        v = num(first(s, *keys))
        return v / 1000.0 if v is not None else None
    def ghs(*keys):
        return num(first(s, *keys))
    r.hashrate_ghs = mhs("MHS 1m", "MHS 5m", "MHS 5s", "MHS av")
    if r.hashrate_ghs is None:
        r.hashrate_ghs = ghs("GHS 5s", "GHS av")
    r.hashrate_now_ghs = mhs("MHS 1m", "MHS 5s") or ghs("GHS 5s") or r.hashrate_ghs
    acc = num(s.get("Accepted"))
    rej = num(s.get("Rejected"))
    r.accepted = int(acc) if acc is not None else None
    r.rejected = int(rej) if rej is not None else None
    r.best_diff = parse_diff(s.get("Best Share"))
    r.uptime_s = num(s.get("Elapsed"))
    return r


# ----------------------------------------------------------------- Braiins OS
def parse_braiins(summary: dict | None, temps: dict | None, fans: dict | None,
                  tuner: dict | None, r: Reading) -> Reading:
    if summary:
        parse_cgminer_summary(summary, r)
    if temps:
        chips = [pos(t.get("Chip")) for t in temps.get("TEMPS") or []]
        boards = [pos(t.get("Board")) for t in temps.get("TEMPS") or []]
        chips = [c for c in chips if c is not None]
        boards = [b for b in boards if b is not None]
        r.temp_c = max(chips) if chips else (max(boards) if boards else None)
        r.board_temp_c = max(boards) if boards else None
    if fans:
        rpms = [num(f.get("RPM")) for f in fans.get("FANS") or []]
        rpms = [x for x in rpms if x is not None]
        r.fan_rpm = max(rpms) if rpms else None
    if tuner:
        t0 = (tuner.get("TUNERSTATUS") or [{}])[0]
        r.power_w = pos(first(t0, "ApproximateMinerPowerConsumption", "ApproximateChainPowerConsumption"))
    r.ok = True
    return r


# ----------------------------------------------------------------- Avalon
_MM_RE = re.compile(r"([A-Za-z][A-Za-z0-9_]*)\[([^\[\]]*)\]")


def parse_mm_string(s: str) -> dict:
    """Parse Canaan 'MM ID0' strings like 'Temp[44] TAvg[90] PS[0 0 0 4 2775 125 340]'."""
    return {k: v.strip() for k, v in _MM_RE.findall(s or "")}


def _avalon_mm(estats: dict) -> dict:
    merged: dict = {}
    for item in estats.get("STATS") or []:
        if not isinstance(item, dict):
            continue
        for k, v in item.items():
            if str(k).startswith("MM ID") and isinstance(v, str):
                merged.update(parse_mm_string(v))
    return merged


def parse_avalon(summary: dict | None, estats: dict | None, r: Reading,
                 ps_power_index: int = -1) -> Reading:
    if summary:
        parse_cgminer_summary(summary, r)
    mm = _avalon_mm(estats) if estats else {}
    if mm:
        spd = num(mm.get("GHSspd"))
        if spd is not None:
            r.hashrate_now_ghs = spd
            if r.hashrate_ghs is None:
                r.hashrate_ghs = spd
        if r.hashrate_ghs is None:
            r.hashrate_ghs = num(mm.get("GHSavg"))
        r.expected_ghs = pos(mm.get("GHSmm"))
        r.temp_c = pos(mm.get("TMax")) or pos(mm.get("MTmax")) or pos(mm.get("TAvg"))
        r.ambient_c = pos(first(mm, "ITemp", "Temp"))
        fans = [num(mm.get(f"Fan{i}")) for i in range(1, 5)]
        fans = [f for f in fans if f is not None]
        r.fan_rpm = max(fans) if fans else None
        ps = [num(x) for x in (mm.get("PS") or "").split()]
        if ps:
            try:
                r.power_w = pos(ps[ps_power_index])
            except IndexError:
                r.power_w = None
        if mm.get("SYSTEMSTATU"):
            r.extra["system_status"] = mm["SYSTEMSTATU"]
            if "Idle" in mm["SYSTEMSTATU"]:
                r.note = "idle (soft-off / schedule?)"
        if mm.get("Ver"):
            r.model = mm["Ver"].split("-")[0]
    r.ok = bool(summary or mm)
    return r


# ----------------------------------------------------------------- dispatch
def _web_fallback(m: dict, r: Reading, timeout: float, err: Exception) -> Reading:
    """API unreachable: if the web UI port answers, treat device as up (no stats)."""
    port = int(m.get("fallback_http_port", 80))
    if port and http_alive(m["host"], port, timeout):
        r.ok = True
        r.stats_available = False
        r.note = f"web UI up but stats API unavailable ({type(err).__name__})"
    else:
        r.error = f"{type(err).__name__}: {err}"
    return r


def collect_braiins(m: dict, timeout: float) -> Reading:
    r = Reading(m["name"], m["type"], m["host"])
    port = int(m.get("port", 4028))
    try:
        summary = cgminer_command(m["host"], port, "summary", timeout)
    except Exception as e:  # noqa: BLE001
        return _web_fallback(m, r, timeout, e)
    extras = {}
    for cmd in ("temps", "fans", "tunerstatus"):
        try:
            extras[cmd] = cgminer_command(m["host"], port, cmd, timeout)
        except Exception as e:  # noqa: BLE001
            log.debug("%s: braiins %s failed: %s", m["name"], cmd, e)
            extras[cmd] = None
    return parse_braiins(summary, extras["temps"], extras["fans"], extras["tunerstatus"], r)


def collect_avalon(m: dict, timeout: float) -> Reading:
    r = Reading(m["name"], m["type"], m["host"])
    port = int(m.get("port", 4028))
    summary = estats = None
    err: Exception | None = None
    try:
        summary = cgminer_command(m["host"], port, "summary", timeout)
    except Exception as e:  # noqa: BLE001
        err = e
    if summary is not None or not isinstance(err, OSError):
        for cmd in ("estats", "stats"):
            try:
                estats = cgminer_command(m["host"], port, cmd, timeout)
                if _avalon_mm(estats):
                    break
            except Exception as e:  # noqa: BLE001
                err = err or e
    if summary is None and estats is None:
        return _web_fallback(m, r, timeout, err or ValueError("no data"))
    return parse_avalon(summary, estats, r, int(m.get("ps_power_index", -1)))


def collect_cgminer(m: dict, timeout: float) -> Reading:
    r = Reading(m["name"], m["type"], m["host"])
    try:
        parse_cgminer_summary(cgminer_command(m["host"], int(m.get("port", 4028)), "summary", timeout), r)
        r.ok = True
        return r
    except Exception as e:  # noqa: BLE001
        return _web_fallback(m, r, timeout, e)


def collect_stratum(m: dict, timeout: float) -> Reading:
    r = Reading(m["name"], "stratum", m["host"], stats_available=False)
    r.ok = tcp_port_open(m["host"], int(m.get("port", 3333)), timeout)
    if not r.ok:
        r.error = f"TCP {m['host']}:{m.get('port', 3333)} not accepting connections"
    if r.ok and m.get("api_url"):
        try:
            d = http_get_json(m["api_url"], timeout)
            r.extra["workers"] = d.get("workersCount", len(d.get("workers") or []))
            r.extra["pool_best_diff"] = parse_diff(d.get("bestDifficulty"))
        except Exception as e:  # noqa: BLE001
            r.extra["api_error"] = str(e)
    return r


COLLECTORS = {
    "axeos": collect_axeos,
    "bitaxe": collect_axeos,
    "nerdqaxe": collect_axeos,
    "braiins": collect_braiins,
    "avalon": collect_avalon,
    "cgminer": collect_cgminer,
    "stratum": collect_stratum,
}


def collect(m: dict, timeout: float) -> Reading:
    fn = COLLECTORS.get(m["type"])
    if fn is None:
        r = Reading(m["name"], m["type"], m["host"])
        r.error = f"unknown miner type '{m['type']}'"
        return r
    try:
        r = fn(m, timeout)
    except Exception as e:  # noqa: BLE001 - never let one miner kill the loop
        r = Reading(m["name"], m["type"], m["host"])
        r.error = f"{type(e).__name__}: {e}"
    return r
