#!/bin/bash
source "$(dirname "$0")/lib.sh"; need_monitor
sudo pkill -f "agent.py --id"; sleep 1
START=$(wc -l < "$LOG")
for i in 1 2 3 4 5 6; do
  hrun h$i python3 "$REPO/agent/agent.py" --id h$i --monitor 10.0.0.100 --port 5005 --interval 1 --fake-cpu >/dev/null 2>&1 &
done
sleep 8; ok=1
for i in 1 2 3 4 5 6; do
  if new_log $START | grep -Eq "node=(h$i|10.0.0.$i) "; then echo "h$i reached monitor"; else echo "h$i MISSING"; ok=0; fi
done
sudo pkill -f "agent.py --id"
[ $ok = 1 ] && echo "PASS" || echo "FAIL"
