#!/bin/bash
source "$(dirname "$0")/lib.sh"; need_monitor
sudo pkill -f "agent.py --id"; sleep 1
START=$(wc -l < "$LOG")
hrun h1 python3 "$REPO/agent/agent.py" --id h1 --monitor 10.0.0.100 --port 5005 --interval 1 >/dev/null 2>&1 &
sleep 4
sudo pkill -f "agent.py --id h1"
sleep 7
OUT=$(new_log $START)
echo "$OUT" | grep -Eq "NODE_DOWN node=(h1|10.0.0.1)" || { echo "FAIL: node not marked DOWN"; exit 1; }
echo "$OUT" | grep -q "CONTROLLER POST /node_down.*-> 200" || { echo "FAIL: controller not told"; exit 1; }
echo "PASS"
