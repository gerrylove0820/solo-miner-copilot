#!/usr/bin/env bash
# Solo Miner Copilot - find the Braiins Farm Proxy metrics on your node (read-only).
#   bash tools/find_proxy.sh (in the solo-miner-copilot folder)
# Prints the farm proxy / Prometheus / Grafana containers, their docker networks, which
# metrics URL answers, the FP_ metric + label names, and whether WATCH_WORKERS names appear in any
# label. Changes nothing. Also saved to find_proxy.txt (Umbrel Files -> Home ->
# solo-miner-copilot) so you can download and share it. BTC addresses are shortened.
set -u
APP=solo-miner-copilot
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$HERE/find_proxy.txt"
[ -w "$HERE" ] || OUT=/tmp/find_proxy.txt

main() {
echo "== Solo Miner Copilot farm proxy finder  $(date '+%Y-%m-%d %H:%M %Z')"
if docker info >/dev/null 2>&1; then D="docker"; elif sudo -n true 2>/dev/null || [ -t 0 ]; then D="sudo docker"; else D=""; fi
CANDS="http://127.0.0.1:9090 http://127.0.0.1:8080/metrics ${EXTRA_PROXY_URLS:-}"
if [ -n "$D" ] && $D info >/dev/null 2>&1; then
  echo; echo "== Containers (farm proxy / prometheus / grafana / braiins)"
  $D ps -a --format '{{.Names}} | {{.Image}} | {{.Status}} | ports: {{.Ports}} | nets: {{.Networks}}' \
    | grep -iE 'farm|prom|grafana|braiins|stratum' || echo "  (none found by name)"
  echo; echo "== Docker networks"
  $D network ls --format '{{.Name}} ({{.Driver}})' | grep -iE 'farm|prom|braiins|proxy' || echo "  (no farm/proxy-named network)"
  echo; echo "== Container addresses"
  for c in $($D ps --format '{{.Names}}' | grep -iE 'farm|prom|braiins' | grep -viE 'gui|config|grafana'); do
    ips="$($D inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}={{$v.IPAddress}} {{end}}' "$c" 2>/dev/null)"
    echo "  $c: $ips"
    for ip in $(echo "$ips" | grep -oE '[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+'); do
      case "$c" in *prom*) CANDS="$CANDS http://$ip:9090" ;; *) CANDS="$CANDS http://$ip:8080/metrics" ;; esac
    done
  done
else
  echo "(docker not usable here - skipping container list; run as your node user)"
fi

echo; echo "== Probing metrics URLs from your node"
python3 - $CANDS <<'PY'
import json, os, re, sys, urllib.parse, urllib.request
watch = os.environ.get("WATCH_WORKERS", "").lower().split()
local_candidates = {"http://127.0.0.1:9090", "http://127.0.0.1:8080/metrics",
                    *os.environ.get("EXTRA_PROXY_URLS", "").split()}
def get(u):
    with urllib.request.urlopen(u, timeout=4) as r:
        return r.read().decode("utf-8", "replace")
def short(s):
    return re.sub(r"\b(bc1[a-z0-9]{4})[a-z0-9]{12,}([a-z0-9]{4})\b", r"\1…\2", s, flags=re.I)
ok = []
for u in dict.fromkeys(sys.argv[1:]):
    try:
        if u.endswith("/metrics"):
            t = get(u)
            names = sorted({l.split("{")[0].split(" ")[0] for l in t.splitlines() if l.startswith(("FP_", "sr_", "subscription", "connections_by"))})
            if not names:
                print(f"  {u}: answers, but no farm proxy metrics"); continue
            labels = sorted({k for l in t.splitlines() if not l.startswith("#") for k in re.findall(r'([A-Za-z_]\w*)="', l)})
            hits = sorted({short(l.split(" ")[0]) for l in t.splitlines() if any(w in l.lower() for w in watch)})
            mode = "metrics"
        else:
            j = json.loads(get(u + "/api/v1/label/__name__/values"))
            names = sorted(n for n in j.get("data", []) if n.startswith(("FP_", "sr_")))
            if not names:
                print(f"  {u}: Prometheus, but no FP_ metrics (not the farm proxy one)"); continue
            labels = sorted(json.loads(get(u + "/api/v1/labels")).get("data", []))
            hits = []
            for lb in labels if watch else []:
                if lb in ("__name__", "job", "instance"): continue
                vals = json.loads(get(u + f"/api/v1/label/{urllib.parse.quote(lb)}/values")).get("data", [])
                hits += [f"{lb}={short(v)}" for v in vals if any(w in v.lower() for w in watch)]
            mode = "prometheus"
        ok.append(u)
        print(f"  {u}: OK - farm proxy {mode}")
        print(f"     metrics ({len(names)}): {', '.join(names[:40])}{' ...' if len(names) > 40 else ''}")
        print(f"     labels: {', '.join(labels)}")
        if watch:
            print(f"     watched workers in labels: {', '.join(hits) if hits else 'NO (workers may be counted in connections only)'}")
    except Exception as e:
        print(f"  {u}: no ({str(e)[:70]})")
print()
if ok:
    print(f"RESULT: farm proxy metrics found at {ok[0]}")
    if not any(x in local_candidates for x in ok):
        print("        (only on a docker-internal address -> run: bash tools/use_proxynet.sh)")
else:
    print("RESULT: no farm proxy metrics found from your node host.")
PY

if [ -n "${D:-}" ] && $D ps --format '{{.Names}}' 2>/dev/null | grep -qx "$APP"; then
  echo; echo "== What the bot itself sees (inside the $APP container)"
  $D exec "$APP" python -m app.main --proxy-check 2>&1 | sed 's/^[0-9-]* [0-9:,]* WARNING farmproxy: .*/  (details below)/' | head -30
fi
}

main 2>&1 | tee "$OUT"
echo; echo "(saved to $OUT)"
