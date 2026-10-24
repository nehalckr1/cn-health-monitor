#!/usr/bin/env python3
"""UDP health agent: sends STATS every interval, and ALERT (EF-marked) on threshold."""
import argparse
import json
import random
import socket
import time
import zlib

import psutil

EF_TOS = 46 << 2  # DSCP EF = 46 -> IP_TOS 184 (0xB8)


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--id", required=True, help="node id, e.g. h1")
    p.add_argument("--monitor", default="127.0.0.1", help="monitor IP")
    p.add_argument("--port", type=int, default=5005, help="stats port")
    p.add_argument("--alert-port", type=int, default=5006, help="alert port")
    p.add_argument("--interval", type=float, default=1.0)
    p.add_argument("--cpu-th", type=float, default=85.0)
    p.add_argument("--mem-th", type=float, default=90.0)
    p.add_argument("--fake-cpu", action="store_true", help="force CPU to 95%%")
    p.add_argument("--fake-mem", action="store_true", help="force memory to 95%%")
    p.add_argument("--real", action="store_true",
                   help="use real psutil cpu/mem (same on all Mininet hosts)")
    return p.parse_args()


def main():
    a = parse_args()
    rng = random.Random(zlib.crc32(a.id.encode()))  # different base per node
    base_cpu = rng.uniform(15, 40)
    base_mem = rng.uniform(30, 60)

    stats_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    alert_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    alert_sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, EF_TOS)

    stats_seq = 0
    alert_seq = 0
    psutil.cpu_percent()  # first call primes the counter

    print(f"[agent {a.id}] sending to {a.monitor}:{a.port} (alerts {a.alert_port})", flush=True)
    while True:
        if a.real:
            cpu = psutil.cpu_percent()
            mem = psutil.virtual_memory().percent
        else:
            cpu = min(100.0, max(0.0, base_cpu + rng.uniform(-5, 5)))
            mem = min(100.0, max(0.0, base_mem + rng.uniform(-3, 3)))
        if a.fake_cpu:
            cpu = 95.0
        if a.fake_mem:
            mem = 95.0

        net = psutil.net_io_counters()
        stats_seq += 1
        msg = {
            "type": "STATS",
            "node_id": a.id,
            "seq": stats_seq,
            "timestamp": time.time(),
            "cpu": round(cpu, 1),
            "mem": round(mem, 1),
            "procs": len(psutil.pids()),
            "net_rx": net.bytes_recv,
            "net_tx": net.bytes_sent,
        }
        try:
            stats_sock.sendto(json.dumps(msg).encode(), (a.monitor, a.port))
        except OSError as e:
            print(f"[agent {a.id}] send error: {e}", flush=True)

        reason = None
        if cpu > a.cpu_th:
            reason = f"cpu {cpu:.1f} > {a.cpu_th:g}"
        elif mem > a.mem_th:
            reason = f"mem {mem:.1f} > {a.mem_th:g}"
        if reason:
            alert_seq += 1
            alert = dict(msg)
            alert.update({
                "type": "ALERT",
                "seq": alert_seq,
                "timestamp": time.time(),
                "severity": "CRITICAL",
                "reason": reason,
            })
            try:
                alert_sock.sendto(json.dumps(alert).encode(), (a.monitor, a.alert_port))
            except OSError as e:
                print(f"[agent {a.id}] alert send error: {e}", flush=True)

        time.sleep(a.interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
