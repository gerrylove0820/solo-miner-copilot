"""Pool watch: phone-only, multi-user alerts for miners on Braiins Solo.

Anyone (not just the owner) can message the bot:
  /start                  welcome + how-to
  /watch <btc_address>    (or just send the address) - subscribe, verified on Braiins Solo
  /unwatch [addr|all]     unsubscribe        /stop  = unwatch all
  /mystatus               per-worker hashrate, last share age, best difficulty
  /help
Owner extra: /users (watcher count, truncated addresses) + an alert on each new subscriber.

Every `poll_seconds` each watched address is fetched once (deduplicated across chats)
through the rate-limited SoloClient. Alerts per worker: offline (no share for
offline_minutes / hashrate 0 / missing, confirmed over offline_confirm_polls), hashrate
drop vs its own 1d average, recovery; per address: new best difficulty. If the API is
down/stale nothing is evaluated (no false "offline"); the owner is told after
api_down_owner_alert_minutes.

Privacy: addresses are only logged truncated (bc1q…abcd); no names/usernames stored.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from datetime import datetime
from zoneinfo import ZoneInfo

from .braiins_solo import (AddressStat, ApiBusy, SoloClient, looks_like_address,
                           normalize_address, trunc_addr)
from .format import fmt_diff, fmt_dur, fmt_hr
from .summary import _odds_lines, daily_slot_due, get_network_difficulty

log = logging.getLogger("poolwatch")

GUEST_COMMANDS = ("/watch", "/unwatch", "/stop", "/mystatus", "/help", "/users")
WORKER_FORGET_S = 7 * 86400


def welcome_text(pw: dict) -> str:
    return ("👋 Hi! This bot can watch your Bitcoin miners on the Braiins Solo pool "
            "(solo.braiins.com) and message you if something goes wrong.\n\n"
            "To start, send:\n/watch <your BTC payout address>\n"
            "(or just paste the address). It's the same address you typed as the pool "
            "username in your miner. Only public pool stats are read - nothing is installed, "
            "no login needed.\n\n" + guest_help(pw))


def guest_help(pw: dict) -> str:
    mins = float(pw["poll_seconds"]) / 60
    return ("Commands:\n"
            "/watch <address> - start watching (max "
            f"{int(pw['max_addresses_per_chat'])} addresses)\n"
            "/mystatus - your workers right now\n"
            "/unwatch <address> - stop watching one (/stop = stop all)\n"
            "/help - this message\n\n"
            f"Checked every {mins:.0f} min. You'll get: 🔴 worker offline (no share for "
            f"{float(pw['offline_minutes']):.0f} min) / ✅ back online, 📉 hashrate drop "
            f"(below {float(pw['drop_pct']):.0f}% of its 24h average), 🏆 new best difficulty, "
            "and a ☀️ daily summary at 8 AM (New York time).")


OWNER_HELP = ("\n\nPool watch (friends on Braiins Solo):\n"
              "/users - who is watching (addresses truncated)\n"
              "/watch, /unwatch, /mystatus - you can use them too")


_BATCH_TITLES = {"pw_offline": "🔴 {n} WORKERS OFFLINE on {a}", "pw_online": "✅ {n} workers back online on {a}",
                 "pw_drop": "📉 HASHRATE DROP on {n} workers of {a}", "pw_drop_ok": "📈 {n} workers recovered on {a}"}


def batch_alerts(alerts: list[tuple[str, str]], a: str, max_single: int = 3) -> list[tuple[str, str]]:
    """Collapse many same-kind worker alerts (farm outage) into one message per kind."""
    by_kind: dict[str, list[str]] = {}
    for kind, text in alerts:
        by_kind.setdefault(kind, []).append(text)
    out = []
    for kind, texts in by_kind.items():
        if kind not in _BATCH_TITLES or len(texts) <= max_single:
            out += [(kind, t) for t in texts]
            continue
        names = [t.split(": ", 1)[1].split(" (", 1)[0] if ": " in t else "?" for t in texts]
        shown = ", ".join(names[:15]) + (f" … +{len(names) - 15} more" if len(names) > 15 else "")
        out.append((kind, _BATCH_TITLES[kind].format(n=len(texts), a=a) + f"\n{shown}\n"
                    "Send /mystatus for details."))
    return out


class PoolWatch:
    def __init__(self, cfg: dict, store, bot, client: SoloClient | None = None):
        self.cfg = cfg
        self.pw = cfg["pool_watch"]
        self.store = store
        self.bot = bot
        self.client = client or SoloClient(
            self.pw["base_url"], float(self.pw["request_timeout_seconds"]),
            float(self.pw["request_spacing_seconds"]))
        self.clock = time.time
        self.latest: dict[str, AddressStat] = {}
        self.pool_info: dict = store.get("pw_pool_status") or {}
        self._lock = threading.RLock()
        self._rate: dict[str, deque] = {}
        self._lookups: dict[str, deque] = {}

    # ================================================================ helpers
    @property
    def owner(self) -> str | None:
        return self.bot.owner_chat_id

    def _state(self, addr: str) -> dict:
        return self.store.get(f"pw:state:{addr}", {}) or {}

    def _save(self, addr: str, st: dict) -> None:
        self.store.set(f"pw:state:{addr}", st)

    def watcher_chats(self) -> list[str]:
        """Distinct non-owner chats with at least one subscription."""
        out = []
        for chat, _ in self.store.pw_all_subs():
            if chat != self.owner and chat not in out:
                out.append(chat)
        return out

    def _deliver(self, chat_id: str, text: str) -> bool:
        ok = self.bot.send_to(chat_id, text)
        blocked = getattr(self.bot, "blocked", set())
        if not ok and chat_id in blocked and chat_id != self.owner:
            n = self.store.pw_remove_sub(chat_id)
            blocked.discard(chat_id)
            log.info("pool watch: a watcher blocked the bot - removed %d subscription(s)", n)
        return ok

    def _allow(self, bucket: dict, chat_id: str, limit: int, window: float, now: float) -> bool:
        q = bucket.setdefault(chat_id, deque())
        while q and now - q[0] > window:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True

    # ================================================================ telegram
    def wants_text(self, text: str) -> bool:
        return looks_like_address(text)

    def welcome(self) -> str:
        return welcome_text(self.pw)

    def owner_help(self) -> str:
        return OWNER_HELP

    def command(self, chat_id: str, cmd: str, args: list[str], is_owner: bool,
                now: float | None = None) -> str | None:
        """Handle a pool-watch command. Returns reply text (None = stay silent)."""
        now = now or self.clock()
        chat_id = str(chat_id)
        if not is_owner and not self._allow(self._rate, chat_id, 12, 60, now):
            return None
        if cmd == "/help":
            return guest_help(self.pw) + (OWNER_HELP if is_owner else "")
        if cmd == "/watch":
            return self._cmd_watch(chat_id, args, is_owner, now)
        if cmd == "/unwatch":
            return self._cmd_unwatch(chat_id, args)
        if cmd == "/stop":
            n = self.store.pw_remove_sub(chat_id)
            return (f"🛑 Stopped watching {n} address(es). Send /watch <address> any time to restart."
                    if n else "You weren't watching anything. Send /watch <address> to start.")
        if cmd == "/mystatus":
            return self._cmd_mystatus(chat_id, now)
        if cmd == "/users" and is_owner:
            return self._cmd_users()
        if cmd == "/start":
            return self.welcome()
        return "Sorry, I don't know that command.\n\n" + guest_help(self.pw)

    def _cmd_watch(self, chat_id: str, args: list[str], is_owner: bool, now: float) -> str:
        if not args:
            return "Send it like this:\n/watch bc1q...your-address\n(or just paste the address)."
        raw = args[0]
        addr = normalize_address(raw)
        if not addr:
            return ("❌ That doesn't look like a valid Bitcoin address. It should start with "
                    "bc1, 1 or 3 - copy it exactly from your miner's pool settings "
                    "(the part before any \".workername\").")
        mine = self.store.pw_subs_for_chat(chat_id)
        if addr in mine:
            return f"👍 Already watching {trunc_addr(addr)}. Send /mystatus to see it."
        cap_chat = int(self.pw["max_addresses_per_chat"])
        if len(mine) >= cap_chat:
            return (f"You're already watching {len(mine)} addresses (max {cap_chat}). "
                    "Remove one with /unwatch <address> first.")
        if not is_owner and not mine and len(self.watcher_chats()) >= int(self.pw["max_users"]):
            log.info("pool watch: global cap reached (%s users), new signup refused", self.pw["max_users"])
            return "😕 Sorry, this bot is full right now. Ask the person who runs it to make room."
        if not is_owner and not self._allow(self._lookups, chat_id, 6, 600, now):
            return "Too many lookups - please wait a few minutes and try again."
        try:
            stat = self.client.user(addr, max_wait=20, now=now)
        except ApiBusy:
            return "⏳ Braiins Solo isn't answering right now. Please try again in a few minutes."
        if stat.status == "not_found":
            return (f"🤔 Braiins Solo doesn't know {trunc_addr(addr)} yet.\n"
                    "Check that your miner is pointed at solo.braiins.com with this address as the "
                    "username and has been hashing for a few minutes, then try again.")
        if not stat.ok:
            return "⏳ Couldn't reach Braiins Solo right now. Please try again in a few minutes."
        with self._lock:
            self.latest[addr] = stat
            self.store.pw_add_sub(chat_id, addr, now)
            st = self._state(addr)
            if not st.get("init"):   # baseline only: first sight never alerts
                self.evaluate(addr, stat, st, now)
                self._save(addr, st)
        log.info("pool watch: new subscription %s (%d watcher chats)", trunc_addr(addr),
                 len(self.watcher_chats()))
        if not is_owner and self.owner and self.pw.get("notify_owner_new_user", True):
            self._deliver(self.owner, f"👤 New pool-watch subscription: {trunc_addr(addr)}\n"
                                      f"Watchers now: {len(self.watcher_chats())}/{int(self.pw['max_users'])}")
        return (f"✅ Now watching {trunc_addr(addr)} on Braiins Solo.\n\n"
                + self.render_address(stat, now) + "\n\nI'll message you if a worker goes offline, "
                "its hashrate drops, or you hit a new best difficulty. /help for commands.")

    def _cmd_unwatch(self, chat_id: str, args: list[str]) -> str:
        mine = self.store.pw_subs_for_chat(chat_id)
        if not mine:
            return "You aren't watching any address. Send /watch <address> to start."
        if args and args[0].lower() == "all":
            n = self.store.pw_remove_sub(chat_id)
            return f"🛑 Stopped watching {n} address(es)."
        if not args:
            if len(mine) == 1:
                self.store.pw_remove_sub(chat_id, mine[0])
                return f"🛑 Stopped watching {trunc_addr(mine[0])}."
            return ("Which one? Send /unwatch <address> or /unwatch all.\nYou watch: "
                    + ", ".join(trunc_addr(a) for a in mine))
        addr = normalize_address(args[0])
        if not addr:   # allow the truncated form, e.g. "bc1q…abcd" or just the last 4 chars
            key = args[0].replace("…", "").replace("...", "")
            hits = [a for a in mine if a.endswith(key[-4:]) and a.startswith(key[:4])] if len(key) >= 4 else []
            addr = hits[0] if len(hits) == 1 else None
        if not addr or addr not in mine:
            return "You're not watching that address. You watch: " + ", ".join(trunc_addr(a) for a in mine)
        self.store.pw_remove_sub(chat_id, addr)
        return f"🛑 Stopped watching {trunc_addr(addr)}."

    def _cmd_mystatus(self, chat_id: str, now: float) -> str:
        mine = self.store.pw_subs_for_chat(chat_id)
        if not mine:
            return "You aren't watching any address yet. Send /watch <your BTC address>."
        tz = ZoneInfo(self.cfg["timezone"])
        lines = [f"📊 Braiins Solo - {datetime.fromtimestamp(now, tz):%a %b %d %I:%M %p %Z}"]
        total_1d = 0.0
        for addr in mine:
            stat = self.latest.get(addr)
            if stat is None or now - stat.fetched_at > float(self.pw["poll_seconds"]) + 60:
                try:
                    fresh = self.client.user(addr, max_wait=10, now=now)
                    if fresh.status != "error":
                        stat = self.latest[addr] = fresh
                except ApiBusy:
                    pass
            lines.append("")
            if stat is None:
                lines.append(f"{trunc_addr(addr)}: no data yet (pool not answering) - try again soon.")
                continue
            lines.append(self.render_address(stat, now))
            total_1d += stat.hr_1d or 0
        diff, src = self._network_diff(now)
        lines += [""] + _odds_lines(total_1d, diff, src)
        return "\n".join(lines)

    def _cmd_users(self) -> str:
        subs = self.store.pw_all_subs()
        by_chat: dict[str, list[str]] = {}
        for chat, addr in subs:
            by_chat.setdefault(chat, []).append(addr)
        owner = self.owner
        others = [c for c in by_chat if c != owner]
        lines = [f"👥 Pool-watch users: {len(others)}/{int(self.pw['max_users'])} "
                 f"({sum(len(by_chat[c]) for c in others)} addresses)"]
        for i, c in enumerate(others, 1):
            lines.append(f"{i}. " + ", ".join(trunc_addr(a) for a in by_chat[c]))
        if owner in by_chat:
            lines.append("You: " + ", ".join(trunc_addr(a) for a in by_chat[owner]))
        down = self.store.get("pw_api_down_since")
        lines.append("Braiins Solo API: " + (f"⚠️ failing for {fmt_dur(self.clock() - down)}" if down else "OK"))
        return "\n".join(lines)

    # ================================================================ rendering
    def _worker_offline(self, w, now: float) -> bool:
        age = (now - w.lastshare) if w.lastshare else None
        if age is None or age > float(self.pw["offline_minutes"]) * 60:
            return True
        return w.hr_5m is not None and w.hr_5m <= 0

    def render_address(self, stat: AddressStat, now: float) -> str:
        a = trunc_addr(stat.address)
        if stat.status == "not_found":
            return f"{a}: not found on Braiins Solo right now (no recent hashing?)"
        if not stat.ok:
            return f"{a}: pool data unavailable ({stat.error})"
        on = [w for w in stat.workers if not self._worker_offline(w, now)]
        lines = [f"{a}: {len(on)}/{len(stat.workers)} workers online | {fmt_hr(stat.hr_5m)} now (5m) | "
                 f"24h avg {fmt_hr(stat.hr_1d)}"]
        for w in sorted(stat.workers, key=lambda x: (self._worker_offline(x, now), x.name))[:20]:
            age = fmt_dur(now - w.lastshare) + " ago" if w.lastshare else "never"
            icon = "🔴" if self._worker_offline(w, now) else "🟢"
            lines.append(f"  {icon} {w.name}: {fmt_hr(w.hr_5m)} (24h {fmt_hr(w.hr_1d)}) | "
                         f"last share {age} | best {fmt_diff(w.best_ever)}")
        if len(stat.workers) > 20:
            lines.append(f"  … and {len(stat.workers) - 20} more workers")
        lines.append(f"  🏆 Best difficulty: {fmt_diff(stat.best_ever)} all-time | "
                     f"{fmt_diff(stat.best_share)} this round")
        return "\n".join(lines)

    def _network_diff(self, now: float):
        diff, src = get_network_difficulty(self.cfg, self.store, None, now)
        if not diff and self.pool_info.get("network_diff"):
            return float(self.pool_info["network_diff"]), "Braiins Solo"
        return diff, src

    # ================================================================ alert logic
    def evaluate(self, addr: str, stat: AddressStat, st: dict, now: float,
                 pool_fresh_restart: bool = False) -> list[tuple[str, str]]:
        """Update per-address state with one successful (ok / not_found) result.
        Returns [(kind, text)] alerts for everyone watching addr."""
        pw = self.pw
        out: list[tuple[str, str]] = []
        a = trunc_addr(addr)
        need_off = int(pw["offline_confirm_polls"])
        need_low = int(pw["drop_confirm_polls"])
        cooldown = float(pw["alert_cooldown_minutes"]) * 60
        grace = float(pw["drop_grace_minutes"]) * 60
        drop_lim = float(pw["drop_pct"]) / 100
        rec_lim = max(drop_lim, float(pw["recovery_pct"]) / 100)
        window = {"1m": "hr_1m", "5m": "hr_5m"}.get(str(pw["drop_compare_window"]), "hr_1hr")
        first = not st.get("init")
        st["init"] = True
        known: dict = st.setdefault("workers", {})
        current = {w.name: w for w in (stat.workers if stat.ok else [])}

        for name in set(known) | set(current):
            w = current.get(name)
            ws = known.setdefault(name, {})
            new = "seen" not in ws
            if w is not None:
                offline = self._worker_offline(w, now)
                if not w.lastshare:
                    reason = "no shares yet"
                elif now - w.lastshare > float(pw["offline_minutes"]) * 60:
                    reason = f"no share for {fmt_dur(now - w.lastshare)}"
                else:
                    reason = "hashrate dropped to 0"
                ws["seen"] = now
            else:
                if pool_fresh_restart:   # pool just restarted: missing data isn't evidence
                    continue
                offline, reason = True, "missing from the pool"
                if now - float(ws.get("seen", now)) > WORKER_FORGET_S:
                    known.pop(name, None)
                    continue
            if new or first:
                ws["dormant"] = offline     # already dead when we started watching: stay quiet
                ws.setdefault("seen", now)
                ws["online_since"] = None if offline else now
                ws["bad"] = ws["low"] = 0
                continue
            label = f"{name} ({a})"
            # ---------------- offline / recovery
            if offline:
                ws["bad"] = int(ws.get("bad", 0)) + 1
                ws["low"] = 0
                ws["online_since"] = None
                if ws.get("off_since") is None:
                    ws["off_since"] = (w.lastshare if w is not None and w.lastshare else now)
                if (not ws.get("dormant") and not ws.get("off_alerted") and ws["bad"] >= need_off
                        and now - float(ws.get("off_last", 0)) >= cooldown):
                    out.append(("pw_offline", f"🔴 WORKER OFFLINE: {label}\n{reason[0].upper() + reason[1:]}. "
                                              f"Check power, Wi-Fi/cable and the pool settings."))
                    ws["off_alerted"], ws["off_last"], ws["drop_alerted"] = True, now, False
                continue
            if ws.get("off_alerted"):
                down = now - float(ws.get("off_since") or now)
                out.append(("pw_online", f"✅ BACK ONLINE: {label} is sending shares again "
                                         f"(down ~{fmt_dur(down)}) at {fmt_hr(w.hr_5m)}."))
            if ws.get("online_since") is None:
                ws["online_since"] = now
            ws.update(bad=0, off_alerted=False, off_since=None, dormant=False)
            # ---------------- hashrate drop vs own 1d average
            cur, avg = getattr(w, window), w.hr_1d
            if not avg or avg <= 0 or cur is None or now - float(ws["online_since"]) < grace:
                ws["low"] = 0
                continue
            ratio = cur / avg
            if ratio < drop_lim:
                ws["low"] = int(ws.get("low", 0)) + 1
                if (ws["low"] >= need_low and not ws.get("drop_alerted")
                        and now - float(ws.get("drop_last", 0)) >= cooldown):
                    out.append(("pw_drop", f"📉 HASHRATE DROP: {label}\nNow {fmt_hr(cur)} = {ratio * 100:.0f}% "
                                           f"of its 24h average {fmt_hr(avg)}. A hashboard/chip, heat or "
                                           f"power problem?"))
                    ws["drop_alerted"], ws["drop_last"] = True, now
            else:
                ws["low"] = 0
                if ratio >= rec_lim and ws.get("drop_alerted"):
                    out.append(("pw_drop_ok", f"📈 Hashrate recovered: {label} at {fmt_hr(cur)} "
                                              f"({ratio * 100:.0f}% of 24h avg)."))
                    ws["drop_alerted"] = False

        # ---------------- best difficulty (address-wide, all-time)
        best = stat.best if stat.ok else None
        if best:
            rec = st.get("best")
            if rec is None or first:
                st["best"] = max(best, rec or 0)
            elif best > float(rec) * 1.000001 and \
                    now - float(st.get("best_last", 0)) >= float(pw["best_diff_cooldown_minutes"]) * 60:
                who = max(stat.workers, key=lambda x: x.best_ever or 0, default=None)
                by = f" by {who.name}" if who and who.best_ever and who.best_ever >= best * 0.999 else ""
                nd = self.pool_info.get("network_diff")
                pct = f"\nThat's {100 * best / float(nd):.4g}% of what a block needs." if nd else ""
                out.append(("pw_best", f"🏆 NEW BEST DIFFICULTY: {a} hit {fmt_diff(best)}{by} "
                                       f"(previous {fmt_diff(rec)}).{pct} Keep swinging!"))
                st["best"], st["best_last"] = best, now
        return out

    # ================================================================ polling
    def poll_cycle(self, now: float | None = None, sleep=None) -> dict:
        """One pass over all watched addresses. Returns a small report dict."""
        now = now or time.time()
        subs = self.store.pw_all_subs()
        addrs = list(dict.fromkeys(a for _, a in subs))
        report = {"addresses": len(addrs), "ok": 0, "not_found": 0, "errors": 0, "api_down": False}
        if not addrs:
            return report
        # 1) pool health / freshness (one cheap request)
        fresh_restart = False
        try:
            info = self.client.pool_status()
            self.pool_info = {k: info.get(k) for k in ("runtime", "lastupdate", "network_diff", "Users")}
            self.store.set("pw_pool_status", self.pool_info)
            lastupdate = float(info.get("lastupdate") or 0)
            if lastupdate and now - lastupdate > 15 * 60:
                raise RuntimeError(f"pool stats stale ({fmt_dur(now - lastupdate)} old)")
            fresh_restart = float(info.get("runtime") or 1e9) < 1800
        except Exception as e:  # noqa: BLE001
            log.warning("Braiins Solo pool.status failed: %s - skipping this check (no alerts)", e)
            report["api_down"] = True
            self._api_health(False, now, str(e))
            return report
        # 2) each address once
        results: dict[str, AddressStat] = {}
        for addr in addrs:
            if sleep and self.client.wait_seconds() > 0:
                sleep(self.client.wait_seconds())
            stat = self.client.user(addr, now=now)
            results[addr] = stat
            report[stat.status if stat.status in ("ok", "not_found") else "errors"] += 1
        if report["errors"] == len(addrs):
            report["api_down"] = True
            self._api_health(False, now, "all address lookups failed")
            return report
        # mass "not found" (e.g. pool lost its user table) = API problem, not offline miners
        prev_ok = [a for a in addrs if results[a].status == "not_found" and self.latest.get(a)
                   and self.latest[a].ok]
        if len(prev_ok) >= 2 and len(prev_ok) * 2 >= len(addrs):
            fresh_restart = True
        self._api_health(True, now)
        # 3) evaluate + notify
        for addr, stat in results.items():
            if stat.status == "error":
                continue
            with self._lock:
                st = self._state(addr)
                try:
                    alerts = self.evaluate(addr, stat, st, now, pool_fresh_restart=fresh_restart)
                except Exception:  # noqa: BLE001
                    log.exception("pool watch evaluation failed for %s", trunc_addr(addr))
                    alerts = []
                self._save(addr, st)
                if stat.ok:
                    self.latest[addr] = stat
                    active = [n for n, ws in st.get("workers", {}).items() if not ws.get("dormant")]
                    online = sum(1 for w in stat.workers if w.name in active and not self._worker_offline(w, now))
                    self.store.pw_add_sample(now, addr, stat.hr_5m, online, len(active), stat.best)
                elif stat.status == "not_found" and not fresh_restart:
                    self.store.pw_add_sample(now, addr, 0.0, 0, len(st.get("workers", {})), None)
            chats = [c for c, a in subs if a == addr]
            for kind, text in batch_alerts(alerts, trunc_addr(addr)):
                log.info("POOL ALERT [%s] %s -> %d chat(s)", kind, trunc_addr(addr), len(chats))
                self.store.log_alert(now, f"pool:{trunc_addr(addr)}", kind, text)
                for c in chats:
                    self._deliver(c, text)
        log.info("pool watch: %d addresses, %d ok, %d not found, %d errors", len(addrs),
                 report["ok"], report["not_found"], report["errors"])
        return report

    def _api_health(self, ok: bool, now: float, why: str = "") -> None:
        since = self.store.get("pw_api_down_since")
        if ok:
            if since is not None:
                if self.store.get("pw_api_down_alerted"):
                    self.bot.send(f"✅ Braiins Solo API is answering again (was down ~{fmt_dur(now - since)}). "
                                  "Pool-watch alerts resumed.")
                log.info("Braiins Solo API recovered after %s", fmt_dur(now - since))
                self.store.set("pw_api_down_since", None)
                self.store.set("pw_api_down_alerted", False)
            return
        if since is None:
            self.store.set("pw_api_down_since", now)
            return
        limit = float(self.pw["api_down_owner_alert_minutes"]) * 60
        if now - since >= limit and not self.store.get("pw_api_down_alerted"):
            self.bot.send(f"⚠️ Braiins Solo API has been failing for {fmt_dur(now - since)} ({why}).\n"
                          "Pool-watch users are NOT being told their miners are offline; checks resume "
                          "automatically.")
            self.store.set("pw_api_down_alerted", True)

    # ================================================================ daily summary
    def build_daily(self, chat_id: str, now: float) -> str | None:
        mine = self.store.pw_subs_for_chat(chat_id)
        if not mine:
            return None
        tz = ZoneInfo(self.cfg["timezone"])
        lines = [f"☀️ Daily Braiins Solo summary - {datetime.fromtimestamp(now, tz):%a %b %d, %Y}"]
        total = 0.0
        for addr in mine:
            stat = self.latest.get(addr)
            win = self.store.pw_window(addr, now - 86400, now)
            lines.append("")
            if stat is None or not stat.ok:
                lines.append(f"{trunc_addr(addr)}: no fresh pool data right now.")
                continue
            on = [w for w in stat.workers if not self._worker_offline(w, now)]
            off = [w.name for w in stat.workers if self._worker_offline(w, now)]
            total += stat.hr_1d or 0
            up = (f"~{win['uptime_pct']:.0f}% of checks had all workers online ({win['samples']} checks)"
                  if win["uptime_pct"] is not None else "not enough history yet")
            lines += [f"{trunc_addr(addr)}",
                      f"  Workers: {len(on)}/{len(stat.workers)} online now"
                      + (f" (offline: {', '.join(off[:5])}{'…' if len(off) > 5 else ''})" if off else ""),
                      f"  Avg hashrate (24h, pool): {fmt_hr(stat.hr_1d)} | 7d {fmt_hr(stat.hr_7d)}",
                      f"  Uptime: {up}",
                      f"  Best difficulty: {fmt_diff(stat.best_ever)} all-time | {fmt_diff(stat.best_share)} this round"]
        diff, src = self._network_diff(now)
        lines += [""] + _odds_lines(total, diff, src)
        return "\n".join(lines)

    def maybe_daily_summary(self, now: float | None = None) -> int:
        now = now or time.time()
        if not self.pw.get("daily_summary", True) or not daily_slot_due(self.cfg, self.store,
                                                                         "pw_last_daily_summary", now):
            return 0
        sent = 0
        for chat in dict.fromkeys(c for c, _ in self.store.pw_all_subs()):
            text = self.build_daily(chat, now)
            if text and self._deliver(chat, text):
                sent += 1
        log.info("pool watch: daily summary sent to %d chat(s)", sent)
        return sent

    # ================================================================ thread
    def run_forever(self, stop: threading.Event) -> None:
        interval = float(self.pw["poll_seconds"])
        log.info("Pool watch on: Braiins Solo every %.0fs, max %s users x %s addresses",
                 interval, self.pw["max_users"], self.pw["max_addresses_per_chat"])
        next_poll = 0.0
        while not stop.is_set():
            now = time.time()
            if now >= next_poll:
                try:
                    self.poll_cycle(now, sleep=stop.wait)
                except Exception:  # noqa: BLE001
                    log.exception("pool watch cycle failed")
                next_poll = now + interval
            try:
                self.maybe_daily_summary()
            except Exception:  # noqa: BLE001
                log.exception("pool watch daily summary failed")
            stop.wait(min(30.0, max(1.0, next_poll - time.time())))
