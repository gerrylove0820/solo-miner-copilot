#!/usr/bin/env bash
# Let the bot join the Braiins Farm Proxy docker network (only if find_proxy.sh says so).
#   bash tools/use_proxynet.sh            # auto-pick the farm proxy network
#   bash tools/use_proxynet.sh NETWORK    # use this docker network name
#   bash tools/use_proxynet.sh off        # back to the normal (host network) setup
set -eu
cd "$(dirname "${BASH_SOURCE[0]}")/.."
[ -f .env ] || { echo "run inside the solo-miner-copilot folder (.env missing)"; exit 1; }
if docker info >/dev/null 2>&1; then D="docker"; else D="sudo docker"; fi
cp -a .env ".env.bak-proxynet"
sed -i '/^COMPOSE_FILE=/d;/^FARM_PROXY_NETWORK=/d' .env
if [ "${1:-}" = off ]; then
  echo "Back to the normal host-network setup."
else
  NET="${1:-}"
  if [ -z "$NET" ]; then
    C="$($D ps --format '{{.Names}}' | grep -iE 'farm.?proxy|prometheus' | grep -viE 'gui|config' | head -1)"
    [ -n "$C" ] && NET="$($D inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$C" | tr ' ' '\n' | grep -v '^$' | head -1)"
  fi
  [ -n "$NET" ] && $D network inspect "$NET" >/dev/null 2>&1 || {
    mv -f .env.bak-proxynet .env; echo "farm proxy docker network not found (${NET:-none}). Nothing changed."; exit 1; }
  [ -n "$(tail -c1 .env)" ] && echo >> .env
  printf 'COMPOSE_FILE=docker-compose.proxynet.yml\nFARM_PROXY_NETWORK=%s\n' "$NET" >> .env
  echo "Bot will join docker network: $NET"
fi
$D compose up -d --force-recreate
sleep 8
$D logs --tail 15 solo-miner-copilot 2>&1 | grep -iE 'farm proxy|error' || true
echo "Done. In Telegram send /proxy to check."
