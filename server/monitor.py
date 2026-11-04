#!/usr/bin/env python3
"""UDP monitor: receives STATS/HEARTBEAT on 5005 and ALERT on 5006.

On a CRITICAL ALERT or a node going DOWN/UP it also calls the SDN controller's
REST API (/alert, /node_down, /node_up). The calls run in a background thread
so the UDP receive loop is never blocked.
"""
import json
import os
import queue
import csv
import select
import signal
import socket
import sys
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "config.json")
with open(CONFIG_PATH) as f:
    CFG = json.load(f)

# controller settings (defaults match Member B's topology: root node 10.0.0.254)
CFG.setdefault("controller_enabled", True)
CFG.setdefault("controller_url", "http://10.0.0.254:8080")
CFG.setdefault("controller_timeout", 2.0)
CFG.setdefault("alert_post_interval", 5)  # seconds between /alert calls per node
# files for Member C (results) and the dashboard
CFG.setdefault("metrics_csv", "metrics_events.csv")   # one row per event
CFG.setdefault("summary_csv", "metrics_summary.csv")  # one row per node
CFG.setdefault("status_file", "/tmp/monitor_status.json")  # read by dashboard.py

REQUIRED = {"type": str, "node_id": str, "seq": int, "timestamp": (int, float)}
STATS_FIELDS = ("cpu", "mem", "procs", "net_rx", "net_tx")

nodes = {}
lock = threading.Lock()
post_q = queue.Queue(maxsize=1000)
log_f = open(CFG.get("log_file", "monitor.log"), "a", buffering=1)

# ------------------------------------------------------------- metrics (CSV)
csv_lock = threading.Lock()
_new_csv = not os.path.exists(CFG["metrics_csv"])
csv_f = open(CFG["metrics_csv"], "a", newline="", buffering=1)
csv_w = csv.writer(csv_f)
if _new_csv:
    csv_w.writerow(["epoch_time", "event", "node_id", "value", "detail"])


def metric(event, node_id, value="", detail=""):
    """event: ALERT (value=latency_ms), NODE_DOWN (value=silent_seconds),
    NODE_UP, LOSS (value=missing packets), DUP, MALFORMED."""
    with csv_lock:
        csv_w.writerow([f"{time.time():.3f}", event, node_id, value, detail])


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
            "dups": 0, "received": 0, "status": "UP", "values": {}, "ip": "",
            "last_threshold": 0.0, "last_alert_post": 0.0,
            "alerts": 0, "lat_sum": 0.0, "lat_n": 0, "lat_max": 0.0}


# ---------------------------------------------------------------- controller
def post_controller(path, body):
    """Queue a POST to the controller (never blocks)."""
    if not CFG["controller_enabled"]:
        return
    try:
        post_q.put_nowait((path, body, time.time()))
    except queue.Full:
        log(f"CONTROLLER queue full, dropped {path} node={body.get('node_id')}")


def controller_worker():
    base = CFG["controller_url"].rstrip("/")
    while True:
        path, body, queued_at = post_q.get()
        data = json.dumps(body).encode()
        req = urllib.request.Request(
            base + path, data=data, method="POST",
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=CFG["controller_timeout"]) as r:
                code = r.status
                r.read()
            ms = (time.time() - queued_at) * 1000
            log(f"CONTROLLER POST {path} node={body.get('node_id')} "
                f"-> {code} in {ms:.1f} ms")
        except Exception as e:  # network down, controller off, HTTP error...
            log(f"CONTROLLER POST {path} node={body.get('node_id')} FAILED: {e}")


# ------------------------------------------------------------------- packets
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
        metric("MALFORMED", addr[0], "", "not valid JSON")
        return
    err = validate(msg)
    if err:
        log(f"MALFORMED from {addr[0]}: {err}")
        metric("MALFORMED", addr[0], "", err)
        return

    nid, typ, seq = msg["node_id"], msg["type"], msg["seq"]
    with lock:
        n = nodes.setdefault(nid, new_node(nid))
        n["ip"] = addr[0]
        seq_key = "last_alert_seq" if typ == "ALERT" else "last_seq"
        restarted = n["status"] == "DOWN" or seq == 1

        if seq <= n[seq_key] and not restarted:
            n["dups"] += 1
            log(f"DUP/OUT-OF-ORDER node={nid} type={typ} seq={seq} last={n[seq_key]}")
            metric("DUP", nid, 1, typ)
            return
        if typ != "ALERT" and n[seq_key] and seq > n[seq_key] + 1 and not restarted:
            gap = seq - n[seq_key] - 1
            n["lost"] += gap
            log(f"LOSS node={nid} missing={gap} (seq {n[seq_key] + 1}..{seq - 1})")
            metric("LOSS", nid, gap, f"seq {n[seq_key] + 1}..{seq - 1}")
        n[seq_key] = seq

        if n["status"] == "DOWN":
            n["status"] = "UP"
            log(f"NODE_UP node={nid}")
            metric("NODE_UP", nid)
            post_controller("/node_up", {"node_id": nid, "ip": n["ip"]})

        n["last_seen"] = recv_time
        if typ != "ALERT":
            n["received"] += 1
        if typ in ("STATS", "ALERT"):
            n["values"] = {k: msg[k] for k in STATS_FIELDS}

        latency_ms = (recv_time - msg["timestamp"]) * 1000
        if typ == "STATS":
            n["lat_sum"] += latency_ms
            n["lat_n"] += 1
            n["lat_max"] = max(n["lat_max"], latency_ms)

        if typ == "ALERT":
            n["alerts"] += 1
            metric("ALERT", nid, f"{latency_ms:.3f}", msg.get("reason", ""))
            sev = msg.get("severity", "?")
            log(f"ALERT node={nid} sev={sev} "
                f"reason={msg.get('reason', '?')} latency_ms={latency_ms:.2f}")
            if sev == "CRITICAL" and \
                    recv_time - n["last_alert_post"] >= CFG["alert_post_interval"]:
                n["last_alert_post"] = recv_time
                post_controller("/alert", {
                    "node_id": nid, "ip": n["ip"], "severity": sev,
                    "reason": msg.get("reason", ""), "cpu": msg["cpu"],
                    "mem": msg["mem"], "latency_ms": round(latency_ms, 2)})
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
                    metric("NODE_DOWN", nid, f"{now - n['last_seen']:.2f}",
                           f"last_seen={n['last_seen']:.3f}")
                    post_controller("/node_down", {"node_id": nid, "ip": n["ip"]})


def snapshot():
    """Per-node numbers, used for the dashboard and the summary CSV."""
    now = time.time()
    rows = {}
    with lock:
        for nid, n in nodes.items():
            tot = n["received"] + n["lost"]
            rows[nid] = {
                "status": n["status"], "ip": n["ip"],
                "cpu": n["values"].get("cpu", 0), "mem": n["values"].get("mem", 0),
                "received": n["received"], "lost": n["lost"], "dups": n["dups"],
                "loss_pct": round(100.0 * n["lost"] / tot, 2) if tot else 0.0,
                "alerts": n["alerts"],
                "avg_lat_ms": round(n["lat_sum"] / n["lat_n"], 3) if n["lat_n"] else 0.0,
                "max_lat_ms": round(n["lat_max"], 3),
                "age": round(now - n["last_seen"], 1) if n["last_seen"] else -1,
            }
    return now, rows


def write_status():
    now, rows = snapshot()
    tmp = CFG["status_file"] + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"updated": now, "nodes": rows}, f)
    os.replace(tmp, CFG["status_file"])
    return rows


def write_summary(rows):
    tmp = CFG["summary_csv"] + ".tmp"
    with open(tmp, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["node_id", "status", "received", "lost", "loss_pct", "dups",
                    "alerts", "avg_stats_latency_ms", "max_stats_latency_ms"])
        for nid in sorted(rows):
            r = rows[nid]
            w.writerow([nid, r["status"], r["received"], r["lost"], r["loss_pct"],
                        r["dups"], r["alerts"], r["avg_lat_ms"], r["max_lat_ms"]])
    os.replace(tmp, CFG["summary_csv"])


def status_writer():
    i = 0
    while True:
        try:
            rows = write_status()
            if i % 5 == 0:
                write_summary(rows)
        except Exception as e:
            log(f"STATUS WRITE FAILED: {e}")
        i += 1
        time.sleep(1)


def shutdown(*_):
    try:
        write_summary(write_status())
    finally:
        log("monitor stopped, summary written")
        os._exit(0)


def main():
    signal.signal(signal.SIGTERM, shutdown)
    threading.Thread(target=status_writer, daemon=True).start()
    s_stats = make_sock(CFG["stats_port"])
    s_alert = make_sock(CFG["alert_port"])
    threading.Thread(target=down_checker, daemon=True).start()
    threading.Thread(target=controller_worker, daemon=True).start()
    log(f"monitor started on ports {CFG['stats_port']} (stats) and {CFG['alert_port']} (alerts)")
    if CFG["controller_enabled"]:
        log(f"controller REST: {CFG['controller_url']}")
    while True:
        try:
            ready, _, _ = select.select([s_stats, s_alert], [], [], 1.0)
        except KeyboardInterrupt:
            shutdown()
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
        shutdown()
