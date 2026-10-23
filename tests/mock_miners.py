"""Local mock miners for dry-run testing (no real hardware needed).

Starts on 127.0.0.1:
  AxeOS HTTP   : ports 18001 (healthy), 18002 (overheats), 18003 (goes offline after 8s),
                 18004 (shares stall), 18005 (NerdQaxe, best diff climbs)
  CGMiner TCP  : 14028 (Avalon Nano 3S), 14029 (Avalon Q), 14030 (Braiins)
Run:  python -m tests.mock_miners    (Ctrl-C to stop)
"""
from __future__ import annotations

import json
import os
import random
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

S = os.path.join(os.path.dirname(__file__), "samples")
T0 = time.time()


def sample(name):
    with open(os.path.join(S, name), "r", encoding="utf-8") as fh:
        return fh.read()


AXE = json.loads(sample("axeos_info.json"))
NERD = json.loads(sample("nerdqaxe_info.json"))


def axe_payload(port):
    el = time.time() - T0
    d = dict(NERD if port == 18005 else AXE)
    d["hashRate"] = d["hashRate"] * random.uniform(0.97, 1.03)
    d["uptimeSeconds"] = int(el)
    d["sharesAccepted"] = int(el) if port != 18004 else 42
    if port == 18002:
        d["temp"] = 74.5
        d["vrTemp"] = 88
    if port == 18005:
        d["bestDiff"] = f"{12.3 + (1.0 if el > 6 else 0):.1f}G"
    return d


class AxeHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        port = self.server.server_address[1]
        if port == 18003 and time.time() - T0 > 8:
            self.close_connection = True
            time.sleep(6)  # simulate a hung miner -> client timeout
            return
        if self.path != "/api/system/info":
            self.send_response(404); self.end_headers(); return
        body = json.dumps(axe_payload(port)).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


CG = {
    14028: {"summary": sample("avalon_nano3s_summary.json"), "estats": sample("avalon_nano3s_estats.txt")},
    14029: {"summary": sample("avalon_nano3s_summary.json"), "estats": sample("avalon_q_estats.json")},
    14030: {"summary": sample("braiins_summary.json"), "temps": sample("braiins_temps.json"),
            "fans": sample("braiins_fans.json"), "tunerstatus": sample("braiins_tunerstatus.json")},
}


class CgHandler(socketserver.BaseRequestHandler):
    def handle(self):
        raw = self.request.recv(4096).decode().strip()
        try:
            cmd = json.loads(raw)["command"]
        except Exception:
            cmd = raw
        port = self.server.server_address[1]
        reply = CG[port].get(cmd)
        if reply is None:
            reply = json.dumps({"STATUS": [{"STATUS": "E", "Msg": f"Invalid command {cmd}"}]})
        if cmd == "summary":
            d = json.loads(reply)
            d["SUMMARY"][0]["Accepted"] += int(time.time() - T0)
            reply = json.dumps(d)
        self.request.sendall(reply.encode() + b"\x00")


class TCP(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def start():
    servers = []
    for p in (18001, 18002, 18003, 18004, 18005):
        s = ThreadingHTTPServer(("127.0.0.1", p), AxeHandler)
        s.daemon_threads = True
        servers.append(s)
    for p in CG:
        servers.append(TCP(("127.0.0.1", p), CgHandler))
    for s in servers:
        threading.Thread(target=s.serve_forever, daemon=True).start()
    return servers


if __name__ == "__main__":
    start()
    print("mock miners running; Ctrl-C to stop", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
