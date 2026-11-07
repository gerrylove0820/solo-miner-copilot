"""Optional alerts when a configured payout is in a block on the active chain.

The source is the user's own node (Bitcoin Core or Bitcoin Cash Node RPC).
The coinbase of a new mainnet block is compared locally with payout addresses
from the environment. The address is not sent to a pool or an explorer.

A match means that block's coinbase pays the configured address. It does not
say which miner found it. Farm-proxy metrics and miner best-share / blockFound
counters are not used.

Off unless BLOCK_WATCH is set and a chain has a node URL plus a public address.
The first synced tip is a baseline: older blocks are not announced.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass

log = logging.getLogger("blockwatch")

# Public Bitcoin mainnet block. Fetched from blockstream.info/api/block-height/500000.
# Bitcoin Cash forked before this height, so a BCH node returns a different hash.
BTC_CHECK_HEIGHT = 500000
BTC_BLOCK_500000 = "00000000000000000024fb37364cbf81fd49cc2d51c09c75c35433c3a1945d04"

_WIF = re.compile(r"^[5KL][1-9A-HJ-NP-Za-km-z]{50,51}$")
_XPRV = re.compile(r"^(xprv|yprv|zprv|tprv)", re.IGNORECASE)
_BECH32 = re.compile(r"^bc1[02-9ac-hj-np-z]{11,}$", re.IGNORECASE)
_CASH = re.compile(r"^(?:bitcoincash:)?[qp][0-9a-z]{41,}$", re.IGNORECASE)
_LEGACY = re.compile(r"^[13][a-km-zA-HJ-NP-Z1-9]{25,34}$")


class RpcError(Exception):
    """Safe to log: the message is a type name or an RPC code, never a secret."""


@dataclass(frozen=True)
class ChainSettings:
    name: str            # BTC or BCH
    url: str
    user: str
    password: str
    addresses: tuple[str, ...]
    family: str          # btc or bch


def chains_from_env(env: Mapping[str, str] | None = None) -> tuple[ChainSettings, ...]:
    """Enabled chains. Empty when BLOCK_WATCH is unset. Secrets stay out of errors."""
    src = os.environ if env is None else env
    if str(src.get("BLOCK_WATCH") or "").strip().lower() not in ("1", "true", "yes", "on"):
        return ()
    out = []
    for name, family, prefix in (("BTC", "btc", "BTC"), ("BCH", "bch", "BCH")):
        chain = _chain_from_env(src, name, family, prefix)
        if chain is not None:
            out.append(chain)
    return tuple(out)


def _chain_from_env(src: Mapping[str, str], name: str, family: str, prefix: str) -> ChainSettings | None:
    url = str(src.get(f"{prefix}_NODE_RPC_URL") or "").strip()
    raw_addresses = str(src.get(f"{prefix}_PAYOUT_ADDRESSES") or "")
    if not url and not raw_addresses.strip():
        return None
    url_error = _url_error(url)
    if url_error:
        log.error("Block watch %s: off (%s).", name, url_error)
        return None
    user = str(src.get(f"{prefix}_NODE_RPC_USER") or "")
    password = str(src.get(f"{prefix}_NODE_RPC_PASSWORD") or "")
    cookie = str(src.get(f"{prefix}_NODE_RPC_COOKIE") or "").strip()
    if cookie:
        loaded = _read_cookie(cookie)
        if loaded is None:
            log.error("Block watch %s: off (RPC cookie file is not readable).", name)
            return None
        user, password = loaded
    addresses, rejected_key = _addresses(raw_addresses)
    if rejected_key:
        log.error("Block watch %s: off (a payout value looks like a private key, not an address).", name)
        return None
    if not addresses:
        log.error("Block watch %s: off (set %s_PAYOUT_ADDRESSES to a public payout address).", name, prefix)
        return None
    if not user and not password:
        log.error("Block watch %s: off (set RPC user/password or a cookie file).", name)
        return None
    return ChainSettings(name, url, user, password, addresses, family)


def _url_error(url: str) -> str | None:
    if not url:
        return "node RPC URL is empty"
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return "node RPC URL must be http or https with a host"
    if parsed.username or parsed.password:
        return "node RPC URL must not contain a username or password"
    return None


def _read_cookie(path: str) -> tuple[str, str] | None:
    try:
        with open(path, encoding="utf-8") as fh:
            raw = fh.read().strip()
    except OSError:
        return None
    user, sep, password = raw.partition(":")
    if not sep or not user or not password:
        return None
    return user, password


def _addresses(raw: str) -> tuple[tuple[str, ...], bool]:
    found = []
    for part in raw.replace("\n", ",").split(","):
        item = part.strip()
        if not item:
            continue
        if _WIF.fullmatch(item) or _XPRV.match(item):
            return (), True
        if _BECH32.fullmatch(item) or _CASH.fullmatch(item) or _LEGACY.fullmatch(item):
            found.append(item)
    return tuple(found), False


def norm_address(addr: str) -> str:
    text = addr.strip()
    lower = text.lower()
    if lower.startswith("bitcoincash:"):
        text = text.split(":", 1)[1]
        lower = text.lower()
    if lower.startswith("bc1") or lower.startswith("q") or lower.startswith("p"):
        return lower
    return text


def coinbase_addresses(block: dict) -> set[str]:
    """Addresses paid by the coinbase. Later transactions in the block are ignored."""
    txs = block.get("tx") or []
    if not txs or not isinstance(txs[0], dict):
        return set()
    vin = (txs[0].get("vin") or [{}])[0]
    if not isinstance(vin, dict) or "coinbase" not in vin:
        return set()
    found: set[str] = set()
    for out in txs[0].get("vout") or []:
        if not isinstance(out, dict):
            continue
        spk = out.get("scriptPubKey") or {}
        if not isinstance(spk, dict):
            continue
        one = spk.get("address")
        if isinstance(one, str) and one.strip():
            found.add(one.strip())
        for item in spk.get("addresses") or []:
            if isinstance(item, str) and item.strip():
                found.add(item.strip())
    return found


def payout_matches(block: dict, addresses: tuple[str, ...]) -> bool:
    wanted = {norm_address(a) for a in addresses}
    return any(norm_address(got) in wanted for got in coinbase_addresses(block))


def message_found(chain: str, height: int, block_hash: str, confs: int) -> tuple[str, str]:
    telegram = (
        f"{chain} payout block is in the active chain.\n"
        f"Height {height}\n"
        f"Hash {block_hash}\n"
        f"Confirmations: {confs}\n"
        f"Not final: a reorg can still replace a recent block.\n"
        f"Source: your {chain} node. The coinbase pays your configured payout.\n"
        f"This does not identify which miner found it."
    )
    phone = (
        f"{chain} payout block in the active chain. Height {height}. "
        f"Hash {block_hash}. {confs} confirmation(s), not final. "
        f"Does not identify which miner."
    )
    return telegram, phone


def message_reorg(chain: str, height: int, block_hash: str) -> tuple[str, str]:
    telegram = (
        f"{chain} payout block left the active chain.\n"
        f"Height {height}\n"
        f"Hash {block_hash}\n"
        f"The node no longer has this hash at that height. This is not a new payout."
    )
    phone = (
        f"{chain} payout block left the active chain. Height {height}. "
        f"Hash {block_hash}. Not a new payout."
    )
    return telegram, phone


def message_settled(chain: str, height: int, block_hash: str, confs: int) -> tuple[str, str]:
    telegram = (
        f"{chain} payout block is still in the active chain after {confs} confirmations.\n"
        f"Height {height}\n"
        f"Hash {block_hash}\n"
        f"A deeper reorg is uncommon. This is not a promise the block can never be replaced."
    )
    phone = (
        f"{chain} payout block still in the active chain after {confs} confirmations. "
        f"Height {height}. Hash {block_hash}. Not a promise it can never be replaced."
    )
    return telegram, phone


class JsonRpc:
    """Minimal Bitcoin-style JSON-RPC. Errors omit the URL and the password."""

    def __init__(self, url: str, user: str, password: str, timeout: float = 8.0, opener=None):
        self.url = url
        self.user = user
        self.password = password
        self.timeout = timeout
        self._opener = opener or urllib.request.urlopen

    def getblockchaininfo(self) -> dict:
        return self.call("getblockchaininfo", [])

    def getblockhash(self, height: int) -> str:
        return self.call("getblockhash", [int(height)])

    def getblock(self, block_hash: str, verbosity: int = 2) -> dict:
        return self.call("getblock", [block_hash, int(verbosity)])

    def call(self, method: str, params: list):
        body = json.dumps({"jsonrpc": "1.0", "id": "smc", "method": method, "params": params}).encode()
        req = urllib.request.Request(self.url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        token = base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
        req.add_header("Authorization", "Basic " + token)
        try:
            with self._opener(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode())
        except Exception as exc:  # noqa: BLE001
            raise RpcError(type(exc).__name__) from None
        if not isinstance(payload, dict):
            raise RpcError("bad-response")
        if payload.get("error"):
            err = payload["error"]
            code = err.get("code") if isinstance(err, dict) else "error"
            raise RpcError(f"rpc {code}")
        return payload.get("result")


class BlockWatch:
    def __init__(self, store, telegram: Callable[[str], object],
                 phone: Callable[[str, str, str | None], None] | None,
                 chains: tuple[ChainSettings, ...] | None = None, *,
                 rpc_factory=None,
                 min_confirmations: int = 1,
                 settled_confirmations: int = 6,
                 max_blocks_per_poll: int = 10,
                 poll_seconds: float = 60.0,
                 error_log_seconds: float = 600.0):
        self.store = store
        self.telegram = telegram
        self.phone = phone
        self.chains = chains if chains is not None else chains_from_env()
        self.rpc_factory = rpc_factory or (lambda chain: JsonRpc(chain.url, chain.user, chain.password))
        self.min_confirmations = max(1, int(min_confirmations))
        self.settled_confirmations = max(self.min_confirmations, int(settled_confirmations))
        self.max_blocks_per_poll = max(1, int(max_blocks_per_poll))
        self.poll_seconds = float(poll_seconds)
        self.error_log_seconds = float(error_log_seconds)
        self._rpcs = {}

    @property
    def enabled(self) -> bool:
        return bool(self.chains)

    def start_log(self) -> None:
        if not self.enabled:
            log.info("Block watch: off (set BLOCK_WATCH=1 plus a node and a public payout address).")
            return
        names = ", ".join(chain.name for chain in self.chains)
        log.info("Block watch: ON for %s. Baseline is the current tip. Addresses are not logged.", names)

    def run_forever(self, stop: threading.Event) -> None:
        self.start_log()
        while not stop.is_set():
            try:
                self.poll()
            except Exception:  # noqa: BLE001
                log.warning("block watch poll failed. Telegram miner alerts were not affected.")
            stop.wait(self.poll_seconds)

    def poll(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        for chain in self.chains:
            try:
                self._poll_chain(chain, now)
            except RpcError as exc:
                self._log_once(chain, now, f"node unavailable ({exc}). No block alert sent.")
            except Exception:  # noqa: BLE001
                self._log_once(chain, now, "node unavailable (error). No block alert sent.")

    def _rpc(self, chain: ChainSettings):
        rpc = self._rpcs.get(chain.name)
        if rpc is None:
            rpc = self.rpc_factory(chain)
            self._rpcs[chain.name] = rpc
        return rpc

    def _state(self, chain: ChainSettings) -> dict:
        state = self.store.get(f"blockwatch:{chain.name}") or {}
        state.setdefault("matches", {})
        return state

    def _save(self, chain: ChainSettings, state: dict) -> None:
        self.store.set(f"blockwatch:{chain.name}", state)

    def _log_once(self, chain: ChainSettings, now: float, message: str) -> None:
        state = self._state(chain)
        last = state.get("last_error_at")
        if last is not None and now - float(last) < self.error_log_seconds:
            return
        state["last_error_at"] = now
        self._save(chain, state)
        log.warning("Block watch %s: %s", chain.name, message)

    def _poll_chain(self, chain: ChainSettings, now: float) -> None:
        rpc = self._rpc(chain)
        info = rpc.getblockchaininfo()
        if not isinstance(info, dict):
            raise RpcError("bad-info")
        if str(info.get("chain") or "") != "main":
            self._log_once(chain, now, "node is not on mainnet. No block alert sent.")
            return
        if not self._identity_ok(chain, rpc):
            self._log_once(chain, now, f"node is not a {chain.name} mainnet node. No block alert sent.")
            return
        blocks = int(info.get("blocks") or 0)
        headers = int(info.get("headers") or blocks)
        if info.get("initialblockdownload") or headers > blocks + 1:
            self._log_once(chain, now, "node is still syncing. No block alert sent.")
            return
        state = self._state(chain)
        if state.get("cursor") is None:
            state["cursor"] = blocks
            state["scanned"] = {str(blocks): str(rpc.getblockhash(blocks))}
            self._save(chain, state)
            log.info("Block watch %s: baseline height %s. Older blocks are not announced.", chain.name, blocks)
            return
        cursor = self._rewind_reorg(rpc, state, blocks)
        end = min(blocks, cursor + self.max_blocks_per_poll)
        scanned = state.setdefault("scanned", {})
        for height in range(cursor + 1, end + 1):
            block_hash = str(rpc.getblockhash(height))
            block = rpc.getblock(block_hash, 2)
            if not isinstance(block, dict):
                raise RpcError("bad-block")
            if payout_matches(block, chain.addresses):
                state["matches"].setdefault(block_hash, {
                    "height": height, "found_sent": False, "reorg_sent": False, "settled_sent": False,
                })
            state["cursor"] = height
            scanned[str(height)] = block_hash
        self._trim_scanned(state, blocks)
        self._update_matches(chain, rpc, state, blocks)
        self._save(chain, state)

    def _rewind_reorg(self, rpc, state: dict, tip: int) -> int:
        """Step back while a scanned height no longer has the hash we saw."""
        cursor = min(int(state["cursor"]), tip)
        scanned = state.setdefault("scanned", {})
        checked = 0
        while cursor > 0 and checked <= self.max_blocks_per_poll:
            previous = scanned.get(str(cursor))
            if not previous or str(rpc.getblockhash(cursor)) == previous:
                break
            del scanned[str(cursor)]
            cursor -= 1
            checked += 1
        state["cursor"] = cursor
        return cursor

    def _trim_scanned(self, state: dict, tip: int) -> None:
        scanned = state.get("scanned") or {}
        keep = self.max_blocks_per_poll + self.settled_confirmations + 2
        for height in list(scanned):
            if int(height) < tip - keep:
                del scanned[height]

    def _identity_ok(self, chain: ChainSettings, rpc) -> bool:
        got = str(rpc.getblockhash(BTC_CHECK_HEIGHT)).strip().lower()
        if chain.family == "btc":
            return got == BTC_BLOCK_500000
        return len(got) == 64 and got != BTC_BLOCK_500000 and all(c in "0123456789abcdef" for c in got)

    def _update_matches(self, chain: ChainSettings, rpc, state: dict, tip: int) -> None:
        for block_hash, rec in list(state["matches"].items()):
            height = int(rec["height"])
            try:
                active = str(rpc.getblockhash(height))
            except RpcError:
                active = ""
            if active != block_hash:
                confs = -1
            else:
                header = rpc.getblock(block_hash, 1)
                confs = int(header.get("confirmations") or (tip - height + 1))
            if confs < 0 or active != block_hash:
                if rec.get("found_sent") and not rec.get("reorg_sent"):
                    if self._emit(chain, "block_reorg", *message_reorg(chain.name, height, block_hash)):
                        rec["reorg_sent"] = True
                continue
            if confs >= self.min_confirmations and not rec.get("found_sent"):
                if self._emit(chain, "block_active", *message_found(chain.name, height, block_hash, confs)):
                    rec["found_sent"] = True
            if (rec.get("found_sent") and not rec.get("reorg_sent") and not rec.get("settled_sent")
                    and confs >= self.settled_confirmations):
                if self._emit(chain, "block_settled", *message_settled(chain.name, height, block_hash, confs)):
                    rec["settled_sent"] = True

    def _emit(self, chain: ChainSettings, kind: str, telegram: str, phone: str) -> bool:
        try:
            self.telegram(telegram)
        except Exception:  # noqa: BLE001
            log.warning("block watch telegram send failed kind=%s chain=%s", kind, chain.name)
            return False
        if self.phone is not None:
            try:
                self.phone(phone, kind, None)
            except Exception:  # noqa: BLE001
                log.warning("block watch phone send failed kind=%s chain=%s", kind, chain.name)
        return True
