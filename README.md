# Solo Miner Copilot

A self-hosted Telegram alert bot for home and solo Bitcoin miners. It watches your Bitaxe, NerdQAxe, Braiins and Avalon devices on your LAN and messages you when one of them needs attention.

Solo Miner Copilot runs as a single Docker container on your own node (such as Umbrel) or on any Linux box with Docker. Every 60 seconds it polls your miners' local APIs and stores their history in SQLite. It sends alerts and on-demand summaries to **your own** Telegram bot.

> **Status:** beta (v1.3.5). See [Project status](#project-status).

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
  - Dual-ASIC Bitaxes use the hotter chip (`temp` and `temp2` from AxeOS). `/status` shows both, for example `ASIC 50/60°C`. The daily summary max is that hotter chip. A missing, zero, or null second temperature is ignored, so single-chip miners are unchanged.
- **Hashrate drop:** alerts when a miner stays below 75% of its own 6-hour median hashrate for 10 minutes.
- **Stalled shares:** alerts when a miner has had no new accepted shares for 20 minutes.
- **New best share:** tells you when a miner reports a higher share difficulty than before. That is a share record, not a confirmed block.
- **Daily summary** at `daily_summary_time` (default 08:00 in your configured timezone). It includes:
  - total fleet hashrate
  - power use and an electricity cost estimate (`electricity.rate_usd_per_kwh`)
  - your odds of finding a solo block, based on network difficulty from mempool.space. You can point this at your own node's mempool app instead.
- **On-demand commands:** `/status`, `/summary`, and `/testpush` from Telegram.
- **Private LAN status page (optional):** the same snapshot as `/status`, in a phone browser on your home Wi-Fi. Off until you set `STATUS_PAGE_TOKEN`. This page cannot push to an iPhone.
- **Phone push (optional):** the same miner alerts, through the free ntfy app. Off until you set `NTFY_TOPIC`. The phone text leaves out LAN addresses and wallets. See [Phone push](#phone-push-ntfy).
- **Confirmed payout blocks (optional):** a block on the active chain whose coinbase pays an address you configure. Off until you set `BLOCK_WATCH=1`. This is not a best-share alert and it does not name which miner found it. See [Confirmed payout blocks](#confirmed-payout-blocks).
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
- **Nothing listens by default.** The container publishes no ports. It uses `network_mode: host` so it can reach the miners on your LAN. The optional status page listens on that machine only after you set `STATUS_PAGE_TOKEN`, and only for your home network. Do not port-forward it.
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
  - **ntfy.sh**, only if you set `NTFY_TOPIC`. The phone message is a short line (miner name and the numbers for that alert). It is sent over HTTPS. It does not include miner LAN addresses, wallet addresses, your bot token, or the status-page link. The topic name is a password: anyone who knows it can read those alerts. ntfy.sh is run by someone else. Leave `NTFY_TOPIC` empty and nothing is sent.
  - **Your own node RPC**, only if you set `BLOCK_WATCH=1`. The app asks that node for new blocks and compares the coinbase with your payout address on the machine. The address is not sent to a pool or an explorer. The RPC password stays in `.env` and is not logged.
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

- **`.env`** (copied from `.env.example`): `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `TZ`, `DRY_RUN`, `LOG_LEVEL`, and optionally `STATUS_PAGE_TOKEN` (private status page), `NTFY_TOPIC` (phone push), and `BLOCK_WATCH` (payout-block alerts). Leave the optional ones empty to keep those extras off.
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
| `/testpush` | Send one test alert to your phone. Works only after `NTFY_TOPIC` is set. |
| `/help` | List of commands |

If pool watch is enabled, the owner also has `/users`, `/watch`, `/unwatch` and `/mystatus`.

---

## Private status page (phone on your Wi-Fi)

Telegram stays the way you get alerts. This page is an extra way to **look** at the same fleet snapshot as `/status`: miners online, hashrate, temperatures, power, and the farm-proxy lines (connection count and the rest) when that check is on.

The page is **off** until you set a secret. If you skip this section, nothing changes and nothing new listens.

It is for your home network. Do not forward the port on your router. Opening this page does not send a push to an iPhone. Phone alerts use the ntfy app, below.

**Turn it on** from the install folder (`~/umbrel/home/solo-miner-copilot`):

1. Generate a secret (or type a long random password of your own, at least 16 characters):

   ```bash
   openssl rand -hex 16
   ```

2. Open `.env` and add one line, using that secret. This is the password in the link.

   ```bash
   nano .env
   ```

   ```
   STATUS_PAGE_TOKEN=paste-the-secret-here
   ```

   In `nano`, press **Ctrl+O**, then **Enter**, then **Ctrl+X**.

3. Rebuild:

   ```bash
   sudo docker compose up -d --build
   ```

4. Find this machine's address on your home network:

   ```bash
   hostname -I
   ```

   Use the first number. That number is `YOUR-UMBREL-IP` in the link below.

5. On the iPhone, join the **same Wi-Fi** (not cellular). Open Safari and go to:

   ```
   http://YOUR-UMBREL-IP:8787/s/paste-the-secret-here
   ```

   Replace the address and the secret with yours. The page reloads about once a minute. In Safari you can use **Share → Add to Home Screen**.

The usual Umbrel install uses host networking, so those steps are enough. You do not open a port in Docker.

**Check from the Umbrel terminal** (same secret):

```bash
curl -fsS "http://127.0.0.1:8787/s/paste-the-secret-here/plain"
```

You should see the same text Telegram would send for `/status`. The logs should contain `Status page: ON` and must not contain the secret.

**Optional settings.** Leave these out to keep the defaults.

| Variable | Default | Meaning |
|---|---|---|
| `STATUS_PAGE_TOKEN` | empty (page off) | Secret in the link. Treat it like a password. |
| `STATUS_PAGE_PORT` | `8787` | Port on the Umbrel machine. |
| `STATUS_PAGE_BIND` | `0.0.0.0` | Lets a phone on the home network connect. Set `127.0.0.1` if only the Umbrel machine itself should open the page. |

Anyone on your Wi-Fi who has the link can open the page. Don't share the link.

**Farm-proxy Docker network.** If you switched on `tools/use_proxynet.sh`, the phone cannot see the page until you publish the port. In `docker-compose.proxynet.yml`, uncomment the `ports` block (`8787:8787`), then run:

```bash
sudo docker compose up -d --build
```

Do not port-forward 8787. To go back to the normal setup, `bash tools/use_proxynet.sh off`.

---

## Phone push (ntfy)

Telegram stays the alert channel. Phone push is an extra copy of the **local miner** alerts: offline, back online, high temperature, temperature normal, hashrate drop, hashrate recovered, shares stalled, shares flowing, and a new best share.

It is **off** until you set a topic. If you skip this section, nothing is sent to ntfy and Telegram is unchanged.

The phone message is a short line: the miner **name** and the numbers for that alert (temperature, hashrate, share difficulty). It does not include the miner’s LAN address, a wallet, your bot token, or the status-page link. It is not the same text Telegram sends.

A new best share is a share record. This app does **not** send “block found” or “the network accepted your block.” AxeOS can count a local candidate when a share meets network difficulty. That count is not used here, because the network may not have accepted it. Confirming an accepted block would need a chain or pool source this app does not read yet.

Pool-watch messages and farm-proxy messages stay on Telegram only.

**What you install on the phone.** No account and no payment.

- **iPhone:** install **ntfy** from the App Store. It is free. The current app needs iOS 15 or later. Allow notifications. In the app, subscribe to your topic on the server `ntfy.sh` (that is the default server). Instant delivery works because ntfy.sh is connected to Apple’s push service. The LAN status page cannot do this.
- **Android:** install **ntfy** from Google Play or from F-Droid. Both are free. Subscribe to the same topic on `ntfy.sh`. The Play app uses Firebase for ntfy.sh. The F-Droid app does not use Firebase; it keeps its own connection.

**Privacy.** Those short alerts are sent to `https://ntfy.sh`, which is run by someone else. The topic is the password. Anyone who knows it can read the alerts. Use a long random topic and do not share it. Paid ntfy plans exist; you do not need one for this.

**Turn it on** from the install folder (`~/umbrel/home/solo-miner-copilot`, which is `/home/umbrel/umbrel/home/solo-miner-copilot` for the `umbrel` user). One command at a time.

If this folder is not a git checkout, copy the v1.3.4 app files first:

```bash
cd ~/umbrel/home/solo-miner-copilot
```

```bash
curl -fsSL -o app/__init__.py https://raw.githubusercontent.com/gerrylove0820/solo-miner-copilot/main/app/__init__.py
```

```bash
curl -fsSL -o app/main.py https://raw.githubusercontent.com/gerrylove0820/solo-miner-copilot/main/app/main.py
```

```bash
curl -fsSL -o app/engine.py https://raw.githubusercontent.com/gerrylove0820/solo-miner-copilot/main/app/engine.py
```

```bash
curl -fsSL -o app/ntfy.py https://raw.githubusercontent.com/gerrylove0820/solo-miner-copilot/main/app/ntfy.py
```

If you use git instead, update the code with:

```bash
git pull
```

Then, either way:

1. Make a topic. Copy the line it prints. That is `YOUR-NTFY-TOPIC`.

   ```bash
   openssl rand -hex 16
   ```

2. Open `.env`.

   ```bash
   nano .env
   ```

   Add one line, using what the previous command printed:

   ```
   NTFY_TOPIC=YOUR-NTFY-TOPIC
   ```

   Press **Ctrl+O**, then **Enter**, then **Ctrl+X**.

3. Rebuild:

   ```bash
   sudo docker compose up -d --build
   ```

4. On the phone, subscribe to `YOUR-NTFY-TOPIC` on server `ntfy.sh`.

5. In Telegram, as the owner, send:

   ```
   /testpush
   ```

   You should get a test alert with no miner data. Logs may say `Phone push: ON over HTTPS`. They must not contain the topic.

No new Python packages. The app still uses the standard library plus PyYAML.

Leave `NTFY_URL` unset. It defaults to `https://ntfy.sh`. A non-HTTPS address is ignored and push stays off.

---

## Confirmed payout blocks

This is separate from a best share. A best share is only a difficulty record from the miner. AxeOS `blockFound` means the miner built a share that met network difficulty. Neither one means the network kept the block.

A confirmed alert here means: **your own node** has this block on its **active mainnet chain**, and the **coinbase** (the block reward transaction, not a later payment in the same block) pays a public address you configured.

That is a fleet payout. It does **not** say which miner, Avalon, or Antminer found it. Braiins Farm Proxy metrics in this app do not include a per-worker accepted block. The proxy is not the source.

**BTC and BCH are separate.** Point `BTC_NODE_RPC_URL` at your Bitcoin node (the Umbrel node behind the farm proxy). Point `BCH_NODE_RPC_URL` at a Bitcoin Cash node you run. The app checks a known post-fork block hash so a Bitcoin node is not treated as Bitcoin Cash, and the other way around.

BCH miners pointed at solopool.org are **not** queried on solopool.org. That site would receive your address and still would not name the ASIC. A block they find still appears on the Bitcoin Cash chain. You need a Bitcoin Cash node’s RPC for that alert. If you do not run one, BCH block alerts stay off. That is the missing piece for BCH.

**What “confirmed” means.** The first alert is “in the active chain” with the node’s confirmation count, usually 1 when the block is new. The text says it is **not final**. A reorg can replace a recent block. If the hash leaves the active chain, you get one “left the active chain” alert. After 6 confirmations you get one “still in the active chain” note. Six confirmations are not a promise the block can never be replaced.

The first time the node is synced, the current tip is a baseline. Older blocks, including ones you found before turning this on, are not announced. While the node is still downloading the chain, nothing is announced.

Alerts go to Telegram and, if phone push is on, to ntfy. The phone text has the chain, height, and block hash. It does not include the payout address, the RPC password, or a miner LAN address. The block hash is public.

**Turn it on** from `~/umbrel/home/solo-miner-copilot` (`/home/umbrel/umbrel/home/solo-miner-copilot`). One command at a time. Skip this if you do not want it. Miner alerts, the status page, and ntfy keep working either way.

If this folder is not a git checkout, copy the v1.3.5 files first:

```bash
cd ~/umbrel/home/solo-miner-copilot
```

```bash
curl -fsSL -o app/__init__.py https://raw.githubusercontent.com/gerrylove0820/solo-miner-copilot/main/app/__init__.py
```

```bash
curl -fsSL -o app/main.py https://raw.githubusercontent.com/gerrylove0820/solo-miner-copilot/main/app/main.py
```

```bash
curl -fsSL -o app/blockwatch.py https://raw.githubusercontent.com/gerrylove0820/solo-miner-copilot/main/app/blockwatch.py
```

```bash
curl -fsSL -o app/ntfy.py https://raw.githubusercontent.com/gerrylove0820/solo-miner-copilot/main/app/ntfy.py
```

If you use git:

```bash
git pull
```

Then edit `.env`. Use the public payout address, never a seed or private key.

```bash
nano .env
```

Add lines like these, with your own values. `http://127.0.0.1:8332` is the usual Bitcoin Core port when the node is on this same machine. If the log says the node is unreachable, that port is not open to the copilot container. Do not guess another address.

```
BLOCK_WATCH=1
BTC_NODE_RPC_URL=http://127.0.0.1:8332
BTC_NODE_RPC_USER=YOUR-RPC-USER
BTC_NODE_RPC_PASSWORD=YOUR-RPC-PASSWORD
BTC_PAYOUT_ADDRESSES=YOUR-BTC-PAYOUT-ADDRESS
```

For Bitcoin Cash, add the same kind of lines with the `BCH_` prefix only if you run a Bitcoin Cash node. Press **Ctrl+O**, then **Enter**, then **Ctrl+X**.

```bash
sudo docker compose up -d --build
```

No new Python packages. Logs say `Block watch: ON for BTC` and a baseline height. They do not print the payout address or the RPC password.

There is no test command that pretends a block was found.

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

**Already installed without git** (the Umbrel folder is not a checkout). For v1.3.5, refresh these app files from `main`, then rebuild. `config.yaml`, `.env` and `data/` stay put. There are no new Python packages.

```bash
cd ~/umbrel/home/solo-miner-copilot
```

```bash
curl -fsSL -o app/__init__.py https://raw.githubusercontent.com/gerrylove0820/solo-miner-copilot/main/app/__init__.py
```

```bash
curl -fsSL -o app/main.py https://raw.githubusercontent.com/gerrylove0820/solo-miner-copilot/main/app/main.py
```

```bash
curl -fsSL -o app/blockwatch.py https://raw.githubusercontent.com/gerrylove0820/solo-miner-copilot/main/app/blockwatch.py
```

```bash
curl -fsSL -o app/ntfy.py https://raw.githubusercontent.com/gerrylove0820/solo-miner-copilot/main/app/ntfy.py
```

```bash
sudo docker compose up -d --build
```

Block alerts stay off until you set `BLOCK_WATCH`. See [Confirmed payout blocks](#confirmed-payout-blocks). Phone push and the status page are unchanged if you already set them.

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
| Phone can't open the status page | 1. The iPhone must be on the same Wi-Fi, not cellular. 2. `STATUS_PAGE_TOKEN` is one line in `.env`, then `sudo docker compose up -d`. 3. Use the number from `hostname -I` in `http://YOUR-UMBREL-IP:8787/s/your-secret`, not `https`. 4. Logs should say `Status page: ON`. 5. Do not port-forward. If you use the farm-proxy compose file, uncomment its `ports` block. |
| No phone push alert | 1. `NTFY_TOPIC` is set, then `sudo docker compose up -d`. 2. The ntfy app is subscribed to that topic on `ntfy.sh`. 3. Send `/testpush` from your Telegram chat. 4. The status page does not deliver iPhone notifications. |
| No payout-block alert | 1. `BLOCK_WATCH=1`, a public payout address, and the node RPC user and password are in `.env`. 2. The node is synced (`initialblockdownload` is false). 3. Blocks found before the baseline height are not announced. 4. BCH needs your own Bitcoin Cash node. solopool.org is not queried. |

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

**Beta (v1.3.5).** The author has tested it on a small home fleet of Bitaxe, NerdQAxe, Braiins BMM and Avalon devices. Other firmware versions may report fields differently, so some values may be missing or wrong on your hardware.

**v1.3.5.** Optional confirmed payout-block alerts from your own Bitcoin or Bitcoin Cash node. A match is a coinbase on the active chain paying your configured address. It does not name a miner. The first synced tip is a baseline. Reorgs get a follow-up. Off unless `BLOCK_WATCH=1`.

**v1.3.4.** Optional phone push through the free ntfy app (`NTFY_TOPIC`). The phone gets a short alert without miner LAN addresses. Telegram is unchanged and still works if push is off or ntfy fails. A best share is labeled as a share, not a confirmed block.

**v1.3.3.** Optional private status page on your home network. It shows the same snapshot as Telegram `/status` and stays off until you set `STATUS_PAGE_TOKEN`. Telegram alerts are unchanged. The page is not phone push.

**v1.3.2.** Daily hashrate averages no longer follow a single wild AxeOS `hashRate` sample. The smoothed 10-minute rate is what gets stored. A clean power-of-ten scale error (about 100x on some higher-hashrate Bitaxes, or MH/s or H/s reported where GH/s is expected) is corrected against that miner's expected hashrate. Samples that are still far above the expected rate, or far above the miner's own recent median, are left out of the average. A real stall is still recorded.

Bug reports and pull requests are welcome. **Before sharing logs, remove your bot token, IP addresses and BTC addresses.** Never share your `.env`.

This project is not affiliated with Braiins, Canaan, Bitaxe, Umbrel or Telegram.

---

## Need help setting it up?

Solo Miner Copilot is free, open-source, and self-hosted, and it will stay that way.

If you'd like help getting it running, G&B Services Corp (Florida) offers **optional paid remote setup help** for about **$49–99** (Phase 1). It's available in English or Spanish.

If the free project helped you, tips in BTC are welcome but never expected.

**Coming soon: Copilot Cloud.** This is a hosted pool watch, planned at $3/mo, $30/yr, or $25 lifetime (early-bird). Nothing is promised yet. If you're interested, join the waitlist.

**How to pay:** in BTC (Lightning or on-chain) or in USD by bank transfer (ACH or wire) to G&B Services Corp. Email us to book a session or send a tip, and we'll reply with a fresh invoice or the bank details.

- Contact: info@gandbservicescorp.com

### ¿Necesitas ayuda?

Solo Miner Copilot sigue siendo gratis, de código abierto y autoalojado.

G&B Services Corp (Florida) ofrece **ayuda remota opcional de instalación** por unos **$49–99** (Fase 1), en español o inglés. Si el proyecto te sirvió, las propinas en BTC son bienvenidas.

**Próximamente: Copilot Cloud.** Será un monitoreo de pool alojado por $3/mes, $30/año o $25 de por vida (precio de lanzamiento). Todavía no hay promesas. Únete a la lista de espera.

**Cómo pagar:** en BTC (Lightning u on-chain) o en dólares por transferencia bancaria (ACH o wire) a G&B Services Corp. Escríbenos para reservar una sesión o enviar una propina, y te respondemos con una factura nueva o los datos bancarios.

- Contacto: info@gandbservicescorp.com

---

## License

MIT, provided without warranty. See [LICENSE](LICENSE).
