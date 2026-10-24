#!/usr/bin/env python3
"""UDP monitor: receives STATS/HEARTBEAT on 5005 and ALERT on 5006."""
import json
import os
import select
import socket
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "config.json")
with open(CONFIG_PATH) as f:
    CFG = json.load(f)

REQUIRED = {"type": str, "node_id": str, "seq": int, "timestamp": (int, float)}
STATS_FIELDS = ("cpu", "mem", "procs", "net_rx", "net_tx")

nodes = {}
lock = threading.Lock()
log_f = open(CFG.get("log_file", "monitor.log"), "a", buffering=1)


def log(line):
    text = time.strftime("%H:%M:%S ") + line
    print(text, flush=True)
    log_f.write(text + "\n")


def make_sock(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
    s.bind(("0.0.0.0", port))
    s.settimeout(1.0)
    return s


def new_node(node_id):
    return {"last_seen": 0.0, "last_seq": 0, "last_alert_seq": 0, "lost": 0,
            "dups": 0, "received": 0, "status": "UP", "values": {}, "last_threshold": 0.0}


def validate(msg):
    if not isinstance(msg, dict):
        return "not an object"
    for k, t in REQUIRED.items():
        if k not in msg or isinstance(msg[k], bool) or not isinstance(msg[k], t):
            return f"bad or missing field '{k}'"
    if msg["type"] not in ("STATS", "HEARTBEAT", "ALERT"):
        return "unknown type"
    if msg["type"] in ("STATS", "ALERT"):
        for k in STATS_FIELDS:
            if k not in msg or not isinstance(msg[k], (int, float)):
                return f"bad or missing field '{k}'"
    return None


def handle(data, addr, recv_time):
    try:
        msg = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        log(f"MALFORMED from {addr[0]}: not valid JSON")
        return
    err = validate(msg)
    if err:
        log(f"MALFORMED from {addr[0]}: {err}")
        return

    nid, typ, seq = msg["node_id"], msg["type"], msg["seq"]
    with lock:
        n = nodes.setdefault(nid, new_node(nid))
        seq_key = "last_alert_seq" if typ == "ALERT" else "last_seq"
        restarted = n["status"] == "DOWN" or seq == 1

        if seq <= n[seq_key] and not restarted:
            n["dups"] += 1
            log(f"DUP/OUT-OF-ORDER node={nid} type={typ} seq={seq} last={n[seq_key]}")
            return
        if typ != "ALERT" and n[seq_key] and seq > n[seq_key] + 1 and not restarted:
            gap = seq - n[seq_key] - 1
            n["lost"] += gap
            log(f"LOSS node={nid} missing={gap} (seq {n[seq_key] + 1}..{seq - 1})")
        n[seq_key] = seq

        if n["status"] == "DOWN":
            n["status"] = "UP"
            log(f"NODE_UP node={nid}")

        n["last_seen"] = recv_time
        if typ != "ALERT":
            n["received"] += 1
        if typ in ("STATS", "ALERT"):
            n["values"] = {k: msg[k] for k in STATS_FIELDS}

        if typ == "ALERT":
            latency_ms = (recv_time - msg["timestamp"]) * 1000
            log(f"ALERT node={nid} sev={msg.get('severity', '?')} "
                f"reason={msg.get('reason', '?')} latency_ms={latency_ms:.2f}")
        elif typ == "STATS":
            if msg["cpu"] > CFG["cpu"] or msg["mem"] > CFG["mem"]:
                if recv_time - n["last_threshold"] > 5:  # avoid log spam
                    n["last_threshold"] = recv_time
                    log(f"THRESHOLD node={nid} cpu={msg['cpu']} mem={msg['mem']}")


def down_checker():
    limit = CFG["down_after"] * CFG["interval"]
    while True:
        time.sleep(1)
        now = time.time()
        with lock:
            for nid, n in nodes.items():
                if n["status"] == "UP" and now - n["last_seen"] > limit:
                    n["status"] = "DOWN"
                    log(f"NODE_DOWN node={nid} silent_for={now - n['last_seen']:.1f}s")


def main():
    s_stats = make_sock(CFG["stats_port"])
    s_alert = make_sock(CFG["alert_port"])
    threading.Thread(target=down_checker, daemon=True).start()
    log(f"monitor started on ports {CFG['stats_port']} (stats) and {CFG['alert_port']} (alerts)")
    while True:
        try:
            ready, _, _ = select.select([s_stats, s_alert], [], [], 1.0)
        except KeyboardInterrupt:
            break
        for s in ready:
            try:
                data, addr = s.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError as e:
                log(f"SOCKET ERROR {e}")
                continue
            handle(data, addr, time.time())


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
