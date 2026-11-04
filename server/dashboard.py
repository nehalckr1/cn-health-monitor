#!/usr/bin/env python3
"""Live terminal dashboard. Run in a normal terminal while monitor.py runs:
    python3 server/dashboard.py [status_file]
Reads the status file that monitor.py rewrites every second."""
import json
import sys
import time

PATH = sys.argv[1] if len(sys.argv) > 1 else "/tmp/monitor_status.json"
CLEAR = "\033[2J\033[H"
RED, GREEN, YELLOW, RESET = "\033[31m", "\033[32m", "\033[33m", "\033[0m"


def natural(k):
    digits = "".join(c for c in k if c.isdigit())
    return (int(digits) if digits else 0, k)


def render(data):
    nodes = data["nodes"]
    age = time.time() - data["updated"]
    up = sum(1 for n in nodes.values() if n["status"] == "UP")
    out = [f"HEALTH MONITOR   nodes: {len(nodes)}   up: {up}   down: {len(nodes) - up}"
           f"   alerts: {sum(n['alerts'] for n in nodes.values())}"
           f"   (status {age:.0f}s old)", ""]
    out.append(f"{'NODE':<8}{'STATUS':<8}{'CPU%':>7}{'MEM%':>7}{'SEEN(s)':>9}"
               f"{'RECV':>8}{'LOST':>6}{'LOSS%':>7}{'ALERTS':>8}{'LAT(ms)':>9}")
    for nid in sorted(nodes, key=natural):
        n = nodes[nid]
        color = GREEN if n["status"] == "UP" else RED
        if n["status"] == "UP" and (n["alerts"] and n["age"] < 5):
            color = YELLOW
        out.append(f"{color}{nid:<8}{n['status']:<8}{n['cpu']:>7.1f}{n['mem']:>7.1f}"
                   f"{n['age']:>9.1f}{n['received']:>8}{n['lost']:>6}{n['loss_pct']:>7.1f}"
                   f"{n['alerts']:>8}{n['avg_lat_ms']:>9.2f}{RESET}")
    out.append("")
    out.append("Ctrl+C to quit.   Yellow = recent alert, red = node DOWN")
    return "\n".join(out)


def main():
    while True:
        try:
            with open(PATH) as f:
                text = render(json.load(f))
        except FileNotFoundError:
            text = f"waiting for {PATH} (is monitor.py running?)"
        except (json.JSONDecodeError, KeyError):
            text = "reading status..."
        sys.stdout.write(CLEAR + text + "\n")
        sys.stdout.flush()
        time.sleep(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
