"""Minimal Telegram Bot API client (stdlib only) with an explicit owner chat.

Owner is configured via TELEGRAM_CHAT_ID or telegram.owner_chat_id. Commands from
other chats are ignored unless pool-watch guest mode is enabled; guests never see
LAN data. In dry-run mode outgoing messages are printed without network calls.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Callable

log = logging.getLogger("telegram")

MAX_LEN = 4000  # Telegram limit is 4096 chars


def split_message(text: str, limit: int = MAX_LEN) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) + 1 > limit:
            parts.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        parts.append(cur)
    return parts


class TelegramBot:
    def __init__(self, token: str | None, data_dir: str, fixed_chat_id: str | None = None,
                 dry_run: bool = False):
        self.token = (token or "").strip()
        self.dry_run = dry_run or not self.token
        self.data_dir = data_dir
        self.owner_file = os.path.join(data_dir, "owner_chat.json")
        self.fixed_chat_id = str(fixed_chat_id).strip() if fixed_chat_id else None
        self.handlers: dict[str, Callable[[], str]] = {}
        self._stop = threading.Event()
        self._legacy_warned = False
        self.sent: list[tuple[str, str]] = []  # (chat_id, text) - for tests / dry-run
        self.extra_help = ""       # extra owner help lines (e.g. /proxy)
        self.guest = None          # optional PoolWatch: commands for non-owner chats
        self.blocked: set[str] = set()  # chats that blocked the bot / don't exist (HTTP 403/400)

    # ---------------------------------------------------------- owner
    @property
    def owner_chat_id(self) -> str | None:
        if self.fixed_chat_id:
            return self.fixed_chat_id
        try:
            with open(self.owner_file, "r", encoding="utf-8") as fh:
                cid = json.load(fh).get("chat_id")
                if cid is not None:
                    if not self._legacy_warned:
                        log.warning("Using legacy owner_chat.json; set TELEGRAM_CHAT_ID in .env instead.")
                        self._legacy_warned = True
                    return str(cid)
                return None
        except (FileNotFoundError, ValueError, OSError):
            return None

    # ---------------------------------------------------------- API
    def _api(self, method: str, payload: dict | None = None, timeout: float = 15):
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        data = json.dumps(payload or {}).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            # never log the URL (it contains the token)
            detail = e.read().decode(errors="replace")[:300]
            raise RuntimeError(f"Telegram {method} HTTP {e.code}: {detail}") from None
        except urllib.error.URLError as e:
            raise RuntimeError(f"Telegram {method} network error: {e.reason}") from None
        if not body.get("ok"):
            raise RuntimeError(f"Telegram {method} failed: {body.get('description')}")
        return body.get("result")

    def send_to(self, chat_id: str, text: str) -> bool:
        ok = True
        for part in split_message(text):
            self.sent.append((str(chat_id), part))
            if self.dry_run:
                print(f"\n----- [DRY-RUN TELEGRAM -> chat {chat_id}] -----\n{part}\n-----", flush=True)
                continue
            for attempt in range(3):
                try:
                    self._api("sendMessage", {"chat_id": chat_id, "text": part,
                                              "disable_web_page_preview": True})
                    break
                except Exception as e:  # noqa: BLE001
                    if "HTTP 403" in str(e) or ("HTTP 400" in str(e) and "chat not found" in str(e)):
                        # user blocked the bot / chat gone: retrying won't help
                        log.warning("send failed permanently: %s", str(e)[:120])
                        self.blocked.add(str(chat_id))
                        return False
                    log.warning("send failed (attempt %d): %s", attempt + 1, e)
                    time.sleep(2 * (attempt + 1))
            else:
                ok = False
        return ok

    def send(self, text: str) -> bool:
        cid = self.owner_chat_id
        if cid is None:
            if self.dry_run:
                return self.send_to("<no-owner-yet>", text)
            log.warning("No owner configured (send /start for your chat ID, then set TELEGRAM_CHAT_ID). Dropping message: %s",
                        text.split("\n")[0])
            return False
        return self.send_to(cid, text)

    # ---------------------------------------------------------- commands
    def handle_update(self, update: dict) -> None:
        msg = update.get("message") or update.get("edited_message")
        if not msg or "text" not in msg:
            return
        chat = msg.get("chat") or {}
        chat_id = str(chat.get("id"))
        text = msg["text"].strip()
        if not text.startswith("/"):
            # pool watch: a bare BTC address counts as "/watch <address>"
            if self.guest is not None and chat.get("type", "private") == "private" \
                    and self.guest.wants_text(text):
                text = "/watch " + text
            else:
                return
        words = text.split()
        cmd = words[0].split("@")[0].lower()
        args = words[1:]
        owner = self.owner_chat_id
        if owner is None and (self.guest is None or cmd == "/start"):
            if chat.get("type", "private") == "private":
                log.info("Telegram setup: chat ID %s", chat_id)
                self.send_to(chat_id, "Solo Miner Copilot is not set up yet. "
                             f"Your chat ID is {chat_id}. Put TELEGRAM_CHAT_ID={chat_id} "
                             "in your .env file and restart the container.")
            return
        if cmd == "/start":
            is_owner = chat_id == owner
            if not is_owner:
                if self.guest is not None:
                    self.send_to(chat_id, self.guest.welcome())
                    return
                log.info("Ignoring /start from non-owner chat %s", chat_id)
                return
            self.send_to(chat_id, "👋 Solo Miner Copilot is watching your miners.\n"
                                  "This chat is the owner and will receive alerts.\n\n" + self._owner_help())
            return
        if chat_id != owner:
            if self.guest is not None:
                self._run(chat_id, cmd, lambda: self.guest.command(chat_id, cmd, args, is_owner=False),
                          guest=True)
                return
            log.info("Ignoring %s from non-owner chat %s", cmd, chat_id)
            return
        if cmd == "/help":
            self.send_to(chat_id, self._owner_help())
            return
        handler = self.handlers.get(cmd)
        if handler is None and self.guest is not None and cmd in ("/watch", "/unwatch", "/stop",
                                                                  "/mystatus", "/users"):
            self._run(chat_id, cmd, lambda: self.guest.command(chat_id, cmd, args, is_owner=True))
            return
        if handler is None:
            self.send_to(chat_id, "Unknown command.\n\n" + self._owner_help())
            return
        self._run(chat_id, cmd, handler)

    def _owner_help(self) -> str:
        return HELP.replace("\n/help - this message", self.extra_help + "\n/help - this message") + \
            (self.guest.owner_help() if self.guest is not None else "")

    def _run(self, chat_id: str, cmd: str, fn, guest: bool = False) -> None:
        try:
            reply = fn()
        except Exception as e:  # noqa: BLE001
            log.exception("command %s failed", cmd)
            reply = f"Sorry, {cmd} failed" + (" - please try again later." if guest else f": {e}")
        if reply:
            self.send_to(chat_id, reply)

    def verify(self) -> None:
        if self.owner_chat_id is None:
            log.info("No owner configured. Send /start to the bot; it replies with your chat ID. "
                     "Set TELEGRAM_CHAT_ID to that ID in .env and restart the container.")
        if self.dry_run:
            log.info("Telegram DRY-RUN: messages will be printed, not sent.")
            return
        try:
            me = self._api("getMe")
        except Exception as e:  # noqa: BLE001 - keep monitoring even if Telegram is down
            log.error("Telegram check failed (%s). Is TELEGRAM_BOT_TOKEN correct? "
                      "Monitoring continues; messages will be retried.", e)
            return
        log.info("Telegram bot OK: @%s", me.get("username"))

    def poll_forever(self, offset_store=None) -> None:
        """Long-poll getUpdates. offset_store: object with get/set (persists update offset)."""
        if self.dry_run:
            return
        offset = offset_store.get("tg_offset") if offset_store else None
        while not self._stop.is_set():
            try:
                payload = {"timeout": 30, "allowed_updates": ["message"]}
                if offset is not None:
                    payload["offset"] = offset
                updates = self._api("getUpdates", payload, timeout=45) or []
                for u in updates:
                    offset = u["update_id"] + 1
                    try:
                        self.handle_update(u)
                    except Exception:  # noqa: BLE001
                        log.exception("update handling failed")
                if updates and offset_store:
                    offset_store.set("tg_offset", offset)
            except Exception as e:  # noqa: BLE001
                log.warning("getUpdates error: %s", e)
                self._stop.wait(10)

    def stop(self) -> None:
        self._stop.set()


HELP = ("Commands:\n"
        "/status - fleet snapshot right now\n"
        "/summary - last-24h summary (same as the 8 AM report)\n"
        "/help - this message\n\n"
        "Alerts: offline (3 missed polls) + recovery, overheat, hashrate drop, "
        "shares stalled, new best difficulty, daily summary at 8:00 AM.")
