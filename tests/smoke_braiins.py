"""LIVE read-only smoke test against the real Braiins Solo API (no Telegram).

    python -m tests.smoke_braiins

Picks an address from pool.status notable_shares, fetches /users/<addr>, parses it,
renders /mystatus and runs two real poll cycles in DRY-RUN (messages printed only).
Addresses are printed truncated."""
import json
import os
import sys
import tempfile
import time

from app.braiins_solo import SoloClient, notable_addresses, trunc_addr
from app.config import load_config
from app.poolwatch import PoolWatch
from app.store import Store
from app.telegram_bot import TelegramBot


def main():
    cfg = load_config(os.path.join(os.path.dirname(__file__), "..", "config.yaml"))
    cfg["network"]["enabled"] = False
    c = SoloClient(cfg["pool_watch"]["base_url"], 15, 2.0)
    ps = c.pool_status()
    print("pool.status keys:", sorted(ps.keys()))
    print(f"pool: users={ps.get('Users')} workers={ps.get('Workers')} runtime={ps.get('runtime')}s "
          f"lastupdate age={time.time() - float(ps.get('lastupdate', 0)):.0f}s network_diff={ps.get('network_diff')}")
    addrs = notable_addresses(ps)
    if not addrs:
        print("no notable_shares right now"); return 1
    addr = addrs[-1]
    t0 = time.monotonic()
    code, body = c._get(f"/users/{addr}")
    print(f"\n/users/{trunc_addr(addr)} -> HTTP {code} (waited {time.monotonic() - t0:.1f}s for rate limit)")
    d = json.loads(body)
    print("top-level keys:", sorted(d.keys()))
    if d.get("worker"):
        print("worker keys:   ", sorted(d["worker"][0].keys()))
    print("sample values:  hashrate5m=%r hashrate1d=%r lastshare=%r bestshare=%r bestever=%r workers=%r" % (
        d.get("hashrate5m"), d.get("hashrate1d"), d.get("lastshare"), d.get("bestshare"),
        d.get("bestever"), d.get("workers")))
    st = c.user(addr)
    print(f"parsed: status={st.status} workers={len(st.workers)} hr5m={st.hr_5m:.1f} GH/s "
          f"hr1d={st.hr_1d:.1f} GH/s best={st.best}")
    bad = c.user("bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4")
    print("unknown address ->", bad.status)

    tmp = tempfile.mkdtemp(prefix="smc-smoke-")
    store = Store(os.path.join(tmp, "db.sqlite3"))
    bot = TelegramBot(None, tmp, fixed_chat_id="1", dry_run=True)
    pw = PoolWatch(cfg, store, bot, c)
    bot.guest = pw
    bot.handle_update({"update_id": 1, "message": {"chat": {"id": 555, "type": "private"},
                                                   "text": f"/watch {addr}"}})
    for i in range(2):
        rep = pw.poll_cycle(time.time())
        print(f"poll cycle {i + 1}: {rep}")
    bot.handle_update({"update_id": 2, "message": {"chat": {"id": 555}, "text": "/mystatus"}})
    print(f"\nrequests made: {c.requests}; messages (dry-run): {len(bot.sent)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
