# Solo Miner Copilot

A self-hosted Telegram alert bot for home and solo Bitcoin miners. It watches your Bitaxe, NerdQAxe, Braiins and Avalon devices on your LAN and messages you when one of them needs attention.

Solo Miner Copilot runs as a single Docker container on your own node (such as Umbrel) or on any Linux box with Docker. Every 60 seconds it polls your miners' local APIs and stores their history in SQLite. It sends alerts and on-demand summaries to **your own** Telegram bot.

> **Status:** beta (v1.3.0). See [Project status](#project-status).

---

## Why self-hosted?

Your miners, IP addresses and stats stay on your own hardware. You create your own Telegram bot with @BotFather, so nothing passes through a server or bot run by someone else. The code is small and readable, so you can check what it does before you run it.

---

## Features

- **Offline alerts:** a miner is marked OFFLINE after 3 missed polls, and you get a message when it recovers.
- **Overheat alerts:**
  - Bitaxe / NerdQAxe: ASIC 70 °C, VR 85 °C
  - Avalon: 95 °C
  - Braiins: 90 °C
  - You can override the limit per miner with `temp_max_c`.
  - Alerts have a 30-minute cooldown. Recovery is reported once the temperature is 5 °C below the limit.
- **Hashrate drop:** alerts when a miner stays below 75% of its own 6-hour median hashrate for 10 minutes.
- **Stalled shares:** alerts when a miner has had no new accepted shares for 20 minutes.
- **New best difficulty:** tells you when a miner sets a new best difficulty.
- **Daily summary** at `daily_summary_time` (default 08:00 in your configured timezone). It includes:
  - total fleet hashrate
  - power use and an electricity cost estimate (`electricity.rate_usd_per_kwh`)
  - your odds of finding a solo block, based on network difficulty from mempool.space. You can point this at your own node's mempool app instead.
- **On-demand commands:** `/status` and `/summary` from Telegram.
- **Optional extras:** Braiins Farm Proxy monitoring, a local stratum port check, and "pool watch" alerts for Braiins Solo users (see [Optional features](#optional-features)).

---

## Supported miners

| `type` | Devices | How it's read |
|---|---|---|
| `axeos` | Bitaxe | AxeOS HTTP API, `GET /api/system/info` (port 80) |
| `nerdqaxe` | NerdQAxe / NerdAxe | AxeOS-style HTTP API |
| `braiins` | Braiins OS / BMM devices | CGMiner-compatible TCP API, port 4028 (`summary`, `temps`, `fans`, `tunerstatus`) |
| `avalon` | Canaan Avalon Nano 3S / Avalon Q | CGMiner API, port 4028 (`estats` / `stats`) |
| `cgminer` | Generic CGMiner-API devices | CGMiner API |

These are optional:

- **Braiins Farm Proxy:** reads its Prometheus metrics, for example `http://127.0.0.1:9090` or `http://farm-proxy:8080/metrics`. The URL is autodetected.
- **Local stratum port check:** set up in the `pool:` section of `config.yaml`.

---

## Privacy & security

- **Runs on your hardware.** The project runs no third-party server, and there is no telemetry or analytics.
- **No open ports.** The container publishes no ports and nothing listens. It uses `network_mode: host` only so it can reach the miners on your LAN.
- **No webhook and no port forwarding.** The bot talks to Telegram through outbound HTTPS long-polling (`getUpdates`) to `api.telegram.org`.
- **Your bot token stays local.** It lives only in your local `.env`, which is in `.gitignore`.
  - The token is never logged.
  - The code never logs the request URL, because that URL contains the token.
- **Your miner IPs stay local.** `config.yaml` is also gitignored.
- **Only you can use the bot.**
  - Only the configured owner chat can run commands. Messages from other chats are ignored.
  - The bot does **not** auto-claim an owner, so the first stranger to message your bot cannot take it over.
- **Other outbound connections:**
  - **mempool.space**, for network difficulty. To avoid it, set `network.enabled: false` or point `mempool_base_url` at your own node.
  - **solo.braiins.com**, only if you enable `pool_watch`.
- **Telegram sees your messages.** Telegram can read the contents of the messages your bot sends you. That's how all Telegram bots work.
- **Read the code.** It's short. A good place to start is `app/telegram_bot.py`.

---

## Requirements

- Docker and Docker Compose v2 (the `docker compose` command). You can run it on:
  - **Umbrel** (umbrelOS already includes Docker), or
  - **any Linux machine**, x86_64 or ARM64 (for example a Raspberry Pi).
- The machine must be on the **same LAN as your miners**.
- `git`, or you can download the repo as a zip instead.
- A Telegram account.

---

## Quick start

### 1. Create your Telegram bot

1. In Telegram, open **@BotFather**.
2. Send `/newbot`.
3. Choose a display name, then a username that ends in `bot`.
4. Copy the token BotFather gives you. It looks like `123456789:AA...`.

**Treat the token like a password.** If it ever leaks, revoke it in BotFather (`/mybots` → your bot → API Token → Revoke) and put the new token in `.env`.

### 2. Install on Umbrel

Solo Miner Copilot is installed from the terminal. It is **not** in the Umbrel App Store.

**Open a terminal on Umbrel** in one of these ways:

- In the dashboard (`http://umbrel.local`), go to **Settings → Advanced settings → Terminal → umbrelOS**, or
- Over SSH: `ssh umbrel@umbrel.local`. The password is your Umbrel login password.

**Then run:**

```bash
cd ~/umbrel/home
git clone https://github.com/gerrylove0820/solo-miner-copilot.git
cd solo-miner-copilot
cp config.example.yaml config.yaml
cp .env.example .env
chmod 600 .env
```

Replace `https://github.com/gerrylove0820/solo-miner-copilot.git` with the real repository URL.

The `~/umbrel/home` folder also shows up in the umbrelOS **Files** app under **Home**.

**Edit your settings:**

```bash
nano config.yaml   # add your miners (see Configuration)
nano .env          # paste your TELEGRAM_BOT_TOKEN
```

In `nano`, press **Ctrl+O** then **Enter** to save, and **Ctrl+X** to exit.

**Start it and watch the logs:**

```bash
sudo docker compose up -d --build
sudo docker logs -f solo-miner-copilot
```

**No git?**

1. On a computer, download the repository as a zip.
2. Upload it to Umbrel with the Files app.
3. In the terminal, unpack it from inside `~/umbrel/home`:

   ```bash
   python3 -m zipfile -e solo-miner-copilot-main.zip .
   ```

**Will it survive reboots?** The container uses `restart: unless-stopped`, so Docker starts it again automatically after a reboot. Because it isn't an App Store app, umbrelOS doesn't manage it: a factory reset or OS reinstall won't keep it, so keep a copy of your `config.yaml` and `.env`.

### 3. Install on other Linux

The steps are the same as on Umbrel, in any folder you like:

```bash
git clone https://github.com/gerrylove0820/solo-miner-copilot.git
cd solo-miner-copilot
cp config.example.yaml config.yaml
cp .env.example .env
chmod 600 .env
nano config.yaml
nano .env
docker compose up -d --build
```

Use `sudo` with `docker` if your user isn't in the `docker` group.

### 4. Get your chat ID

The bot only answers its owner, so you need to tell it your Telegram chat ID.

**The easiest way:**

1. Leave `TELEGRAM_CHAT_ID` empty in `.env` and start the bot.
2. Open a private chat with your bot and send `/start`. Because no owner is set yet, it replies with:
   > Solo Miner Copilot is not set up yet. Your chat ID is 123456789. Put TELEGRAM_CHAT_ID=123456789 in your .env file and restart the container.

   The same ID also appears in the logs as `Telegram setup: chat ID ...`. Until you set it, the bot sends no miner data to anyone.
3. Put that number in `.env`:

   ```
   TELEGRAM_CHAT_ID=123456789
   ```

4. Apply the change. `up -d` re-reads `.env`:

   ```bash
   sudo docker compose up -d
   ```

5. Send `/start` again. You should now get the welcome message and the list of commands.

**Other ways to get the ID:**

- Message **@userinfobot** in Telegram.
- **Group chats:** you can also use a group as the owner chat. Group IDs are negative numbers like `-100...`; put that number in `TELEGRAM_CHAT_ID` and add your bot to the group. (The "your chat ID is ..." setup reply only works in private chats, so get a group's ID another way, e.g. with a chat-ID helper bot.)

`TELEGRAM_CHAT_ID` in `.env` takes priority over `telegram.owner_chat_id` in `config.yaml`.

### 5. Test

**Without Telegram:** set `DRY_RUN=1` in `.env`. Messages are printed to the log instead of being sent.

**One-shot test:** this prints what `/status` and `/summary` would show, then exits.

```bash
sudo docker compose run --rm solo-miner-copilot python -m app.main --once --dry-run
```

---

## Configuration

There are two settings files:

- **`.env`** (copied from `.env.example`): `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `TZ`, `DRY_RUN`, `LOG_LEVEL`
- **`config.yaml`** (copied from `config.example.yaml`): your miners and all other settings

**Example miners block:**

```yaml
miners:
  - {name: Bitaxe-1,      type: axeos,    host: 192.168.1.50}
  - {name: NerdQAxe-1,    type: nerdqaxe, host: 192.168.1.51}
  - {name: Braiins-BMM,   type: braiins,  host: 192.168.1.52, port: 4028}
  - {name: Avalon-Nano3S, type: avalon,   host: 192.168.1.53, port: 4028, temp_max_c: 95}
```

**Main keys:**

| Key | Default | Notes |
|---|---|---|
| `poll_interval_seconds` | `60` | How often miners are polled |
| `timezone` | `"UTC"` | IANA name, e.g. `Europe/Berlin` |
| `daily_summary_time` | `"08:00"` | In the configured timezone |
| `request_timeout_seconds` | `5` | Per-request timeout |
| `telegram.owner_chat_id` | — | `TELEGRAM_CHAT_ID` in `.env` wins over this |
| `electricity.rate_usd_per_kwh` | `0.15` | Used for cost estimates (see also `rate_is_default`) |
| `thresholds.offline_after_failed_polls` | `3` | |
| `thresholds.asic_temp_max_c` | `70` | Bitaxe / NerdQAxe |
| `thresholds.vr_temp_max_c` | `85` | Bitaxe / NerdQAxe |
| `thresholds.avalon_temp_max_c` | `95` | |
| `thresholds.braiins_temp_max_c` | `90` | |
| `thresholds.temp_recovery_margin_c` | `5` | |
| `thresholds.overheat_cooldown_minutes` | `30` | |
| `thresholds.hashrate_drop_pct` | `75` | Percent of the miner's own median |
| `thresholds.hashrate_drop_minutes` | `10` | |
| `thresholds.hashrate_baseline_hours` | `6` | |
| `thresholds.shares_stall_minutes` | `20` | |
| `network.enabled` / `mempool_base_url` / `refresh_minutes` | — | Network difficulty source. Can point at your own node's mempool app, e.g. `http://192.168.1.10:3006` |
| `pool.enabled` / `host` / `port` | — | Optional local stratum port check |
| `retention_days` | `8` | How much history is kept in SQLite |
| `miners[]` | — | `name`, `type`, `host`, optional `port`, `temp_max_c`, `vr_temp_max_c`, `enabled: false`, `ps_power_index` (avalon), `fallback_http_port` |
| `pool_watch.*` | off | See [Pool watch](#pool-watch-braiins-solo) |
| `farm_proxy.*` | off | `enabled`, `urls`, `expected_connections`, `watch_workers`, … |

Everything else is explained in the comments in `config.example.yaml`.

**Applying changes:**

- After editing `config.yaml`: `sudo docker compose restart`
- After editing `.env`: `sudo docker compose up -d`

---

## Telegram commands

These commands only work from the configured owner chat. Messages from any other chat are ignored.

| Command | What it does |
|---|---|
| `/start` | Welcome message. If no owner is set yet, it replies with your chat ID instead. |
| `/status` | Snapshot of your fleet right now |
| `/summary` | Summary of the last 24 hours (same as the daily report) |
| `/proxy` | Farm proxy status. Only available when `farm_proxy` is enabled. |
| `/help` | List of commands |

If pool watch is enabled, the owner also has `/users`, `/watch`, `/unwatch` and `/mystatus`.

---

## Optional features

### Pool watch (Braiins Solo)

Pool watch is **off by default** (`pool_watch.enabled`). Leave it off unless you specifically want it.

**What it does:** any Telegram user who finds your bot can send `/watch <their BTC address>` to get alerts about their own Braiins Solo (solo.braiins.com) workers:

- worker offline
- hashrate drop
- new best difficulty
- daily summary

They can also use `/mystatus`, `/unwatch` and `/stop`.

**What they can't see:** your miners, your config or your LAN.

**What it costs you:** your node makes requests to solo.braiins.com on their behalf.

- Requests are spaced at least 2 seconds apart.
- At most 25 users are allowed.
- Each user can watch up to 3 addresses.

### Braiins Farm Proxy

If you run Braiins Farm Proxy, enable `farm_proxy` in `config.yaml`. The bot reads the proxy's Prometheus metrics and alerts you when:

- there are fewer miner connections than `expected_connections` (set it to `0` to skip this check)
- the metrics have been unreachable for more than 10 minutes
- upstream hashrate has been below 50% of downstream for 10 minutes
- invalid shares are above 5%
- pool-only watched workers (`watch_workers`) are gone

**Helper scripts:**

- `bash tools/find_proxy.sh`: read-only diagnostics that help you find the proxy. You can also set the optional `EXTRA_PROXY_URLS` and `WATCH_WORKERS` environment variables.
- `bash tools/use_proxynet.sh`: joins the farm proxy's Docker network using `docker-compose.proxynet.yml`. Undo it with `bash tools/use_proxynet.sh off`.

---

## Updating, stopping, uninstalling

**Update.** Your `config.yaml`, `.env` and `data/` are kept.

```bash
git pull && sudo docker compose up -d --build
```

**Stop:**

```bash
sudo docker compose down
```

**Uninstall:** run `sudo docker compose down`, then delete the `solo-miner-copilot` folder.

---

## Troubleshooting

Start by checking the logs:

```bash
sudo docker logs --tail 50 solo-miner-copilot
```

| Symptom | What to check |
|---|---|
| `Telegram check failed ... Unauthorized` | The bot token is wrong. Fix `TELEGRAM_BOT_TOKEN` in `.env`, then run `sudo docker compose up -d`. |
| Bot never answers | 1. Check the token. 2. Make sure only **one** copy of the bot is running: two instances polling with the same token conflict (HTTP 409). 3. Make sure `TELEGRAM_CHAT_ID` matches your chat. |
| `No owner configured` | Follow [Get your chat ID](#4-get-your-chat-id). |
| A miner is always OFFLINE | 1. Check that the IP is right and the miner is on the same LAN. 2. Give your miners DHCP reservations so their IPs don't change. 3. For AxeOS devices, test with `curl http://192.168.1.50/api/system/info`. |
| Braiins/Avalon shows "up, no stats" | The miner's web page answers, but its port 4028 API is closed or disabled. |
| Daily summary arrives at the wrong time | Set `timezone` in `config.yaml`. |
| Farm proxy shows "unavailable" | Run `bash tools/find_proxy.sh`. If needed, then run `bash tools/use_proxynet.sh`. |
| Permission errors | Use `sudo` with `docker` commands. |

---

## Development

```bash
pip install -r requirements.txt

# Run the tests
python -m unittest discover -s tests -t .

# Dry run with mock miners
python -m tests.run_dry 25

# One-shot run against your config
DRY_RUN=1 python -m app.main --once --config config.yaml
```

The project is written for Python 3.12 and uses only the standard library plus PyYAML.

---

## Project status

**Beta (v1.3.0).** The author has tested it on a small home fleet of Bitaxe, NerdQAxe, Braiins BMM and Avalon devices. Other firmware versions may report fields differently, so some values may be missing or wrong on your hardware.

Bug reports and pull requests are welcome. **Before sharing logs, remove your bot token, IP addresses and BTC addresses.** Never share your `.env`.

This project is not affiliated with Braiins, Canaan, Bitaxe, Umbrel or Telegram.

---

## License

MIT, provided without warranty. See [LICENSE](LICENSE).
