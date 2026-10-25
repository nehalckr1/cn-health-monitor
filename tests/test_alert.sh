#!/bin/bash
source "$(dirname "$0")/lib.sh"; need_monitor
sudo pkill -f "agent.py --id"; sleep 1
START=$(wc -l < "$LOG")
hrun h2 python3 "$REPO/agent/agent.py" --id h2 --monitor 10.0.0.100 --port 5005 --interval 1 --fake-cpu >/dev/null 2>&1 &
sleep 8
OUT=$(new_log $START)
sudo pkill -f "agent.py --id h2"
echo "$OUT" | grep -Eq "ALERT node=(h2|10.0.0.2) sev=CRITICAL" || { echo "FAIL: no ALERT"; exit 1; }
echo "$OUT" | grep -q "CONTROLLER POST /alert.*-> 200" || { echo "FAIL: alert not accepted by controller"; exit 1; }
echo "PASS"
