"""Entry point: poll loop + Telegram command thread + daily summary scheduler."""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import __version__
from .collectors import collect
from .config import load_config
from .engine import Monitor
from .farmproxy import FarmProxy
from .store import Store
from .poolwatch import PoolWatch
from .summary import build_daily_summary, build_status, daily_slot_due
from .telegram_bot import TelegramBot

log = logging.getLogger("main")


def env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


class App:
    def __init__(self, cfg: dict, data_dir: str, dry_run: bool):
        self.cfg = cfg
        self.store = Store(os.path.join(data_dir, "copilot.sqlite3"))
        self.bot = TelegramBot(os.environ.get("TELEGRAM_BOT_TOKEN"), data_dir,
                               os.environ.get("TELEGRAM_CHAT_ID") or (cfg.get("telegram") or {}).get("owner_chat_id"), dry_run=dry_run)
        self.monitor = Monitor(cfg, self.store, lambda text, kind, miner: self.bot.send(text))
        self.latest: list = []
        self.latest_lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=max(4, min(32, len(cfg["miners"]))))
        self.farm = None
        if cfg["farm_proxy"].get("enabled"):
            self.farm = FarmProxy(cfg, self.store, self.bot.send, lan_snapshot=self.snapshot)
        self.bot.handlers = {
            "/status": lambda: build_status(self.snapshot(), cfg, self.store, farm=self.farm),
            "/summary": lambda: build_daily_summary(cfg, self.store, self.snapshot(), farm=self.farm),
        }
        if self.farm is not None:
            self.bot.handlers["/proxy"] = self.farm.proxy_text
            self.bot.extra_help = "\n/proxy - Braiins Farm Proxy (pool side): connections, hashrate, invalid %"
        self.stop = threading.Event()
        self.poolwatch = None
        if cfg["pool_watch"].get("enabled"):
            self.poolwatch = PoolWatch(cfg, self.store, self.bot)
            self.bot.guest = self.poolwatch

    def snapshot(self):
        with self.latest_lock:
            return list(self.latest)

    def poll_once(self, now: float | None = None):
        timeout = float(self.cfg["request_timeout_seconds"])
        readings = list(self.pool.map(lambda m: collect(m, timeout), self.cfg["miners"]))
        for r in readings:
            if r.ok:
                log.debug("%s ok hr=%s temp=%s", r.name, r.hashrate_ghs, r.temp_c)
            else:
                log.debug("%s FAIL %s", r.name, r.error)
        ok = sum(1 for r in readings if r.ok)
        log.info("poll: %d/%d responding, fleet %.1f GH/s", ok, len(readings),
                 sum(r.hashrate_ghs or 0 for r in readings if r.ok and r.kind != "stratum"))
        with self.latest_lock:
            self.latest = readings
        self.monitor.process(readings, now)
        return readings

    def maybe_daily_summary(self, now: float | None = None) -> bool:
        now = now or time.time()
        if daily_slot_due(self.cfg, self.store, "last_daily_summary", now):
            self.bot.send(build_daily_summary(self.cfg, self.store, self.snapshot(), now, farm=self.farm))
            return True
        return False

    def run(self, once: bool = False):
        self.bot.verify()
        if not once:
            threading.Thread(target=self.bot.poll_forever, args=(self.store,), daemon=True,
                             name="telegram").start()
            if self.farm:
                threading.Thread(target=self.farm.run_forever, args=(self.stop,), daemon=True,
                                 name="farmproxy").start()
            if self.poolwatch:
                threading.Thread(target=self.poolwatch.run_forever, args=(self.stop,), daemon=True,
                                 name="poolwatch").start()
        interval = float(self.cfg["poll_interval_seconds"])
        last_prune = 0.0
        while not self.stop.is_set():
            started = time.time()
            try:
                self.poll_once()
                self.maybe_daily_summary()
                if started - last_prune > 3600:
                    self.store.prune(started - float(self.cfg["retention_days"]) * 86400)
                    last_prune = started
            except Exception:  # noqa: BLE001
                log.exception("poll cycle failed")
            if once:
                break
            self.stop.wait(max(1.0, interval - (time.time() - started)))
        self.bot.stop()


def proxy_check(cfg: dict) -> int:
    """Read-only farm proxy probe (used by update.sh / find_proxy.sh). No Telegram, no DB."""
    cfg["farm_proxy"]["enabled"] = True
    farm = FarmProxy(cfg, Store(":memory:"), lambda text: None)
    found = farm.detect(time.time())
    print("Farm proxy candidates:")
    for line in farm.detect_log:
        print("  " + line)
    if not found:
        print("RESULT: no farm proxy metrics endpoint reachable from the bot.")
        return 2
    farm.poll()
    print("RESULT: OK -> " + farm.source.url)
    print(farm.proxy_text())
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Solo Miner Copilot")
    ap.add_argument("--config", default=os.environ.get("CONFIG_PATH", "config.yaml"))
    ap.add_argument("--data-dir", default=os.environ.get("DATA_DIR", "data"))
    ap.add_argument("--dry-run", action="store_true", help="print Telegram messages instead of sending")
    ap.add_argument("--once", action="store_true", help="poll once, print /status and /summary, exit")
    ap.add_argument("--proxy-check", action="store_true",
                    help="find the Braiins Farm Proxy metrics endpoint, print /proxy, exit (read-only)")
    args = ap.parse_args(argv)

    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stdout)
    dry = args.dry_run or env_flag("DRY_RUN")
    cfg = load_config(args.config)
    if args.proxy_check:
        return proxy_check(cfg)
    if not os.environ.get("TELEGRAM_BOT_TOKEN") and not dry:
        log.warning("TELEGRAM_BOT_TOKEN is not set -> running in DRY-RUN mode (messages only logged).")
    log.info("Solo Miner Copilot %s: %d targets, poll every %ss, summary %s %s", __version__,
             len(cfg["miners"]), cfg["poll_interval_seconds"], cfg["daily_summary_time"], cfg["timezone"])
    log.info("Pool watch (Braiins Solo, phone-only friends): %s",
             "ON" if cfg["pool_watch"].get("enabled") else "off (add pool_watch: enabled: true to config.yaml)")
    log.info("Farm proxy check: %s", "ON" if cfg["farm_proxy"].get("enabled") else "off")
    app = App(cfg, args.data_dir, dry)

    def _sig(*_):
        log.info("shutting down")
        app.stop.set()
    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    if args.once:
        app.bot.verify()
        app.poll_once()
        if app.farm:
            app.farm.poll()
        print(build_status(app.snapshot(), cfg, app.store), flush=True)
        print("", flush=True)
        print(build_daily_summary(cfg, app.store, app.snapshot()), flush=True)
        return 0
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
