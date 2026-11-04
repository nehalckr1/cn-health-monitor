#!/bin/bash
# usage: tests/scenario_congestion.sh <label> <run#>   e.g.  dynamic 1
source "$(dirname "$0")/lib.sh"; need_monitor
LABEL=${1:-dynamic}; RUN=${2:-1}
D2="$REPO/results/d2"; mkdir -p "$D2"
OUT="$D2/congestion.csv"
[ -f "$OUT" ] || echo "label,run,alerts,avg_ms,p95_ms,max_ms,down_events" > "$OUT"
set_mode "$LABEL"
sudo pkill -f "agent.py --id"; sudo pkill -f iperf; sleep 1
hrun bg2 iperf -s -u >/dev/null 2>&1 &
sleep 1
hrun h2 python3 "$REPO/agent/agent.py" --id h2 --monitor 10.0.0.100 --port 5005 --interval 1 --fake-cpu >/dev/null 2>&1 &
sleep 5
START=$(wc -l < "$LOG")
hrun h3 iperf -c 10.0.0.202 -u -b 9M -t 30 >/dev/null 2>&1 &
hrun h4 iperf -c 10.0.0.202 -u -b 9M -t 30 >/dev/null 2>&1 &
hrun bg1 iperf -c 10.0.0.202 -u -b 9M -t 30 >/dev/null 2>&1
sleep 2
new_log $START > "$D2/${LABEL}_congestion_run${RUN}.log"
sudo pkill -f "agent.py --id"; sudo pkill -f iperf
STATS=$(grep -o "latency_ms=[0-9.]*" "$D2/${LABEL}_congestion_run${RUN}.log" | cut -d= -f2 | sort -n | awk '{a[NR]=$1; s+=$1} END{ if(NR==0){print "0,0,0,0"; exit} i=int(NR*0.95); if(i<1)i=1; printf "%d,%.2f,%.2f,%.2f", NR, s/NR, a[i], a[NR] }')
DOWNS=$(grep -c "NODE_DOWN" "$D2/${LABEL}_congestion_run${RUN}.log")
echo "$LABEL,$RUN,$STATS,$DOWNS" | tee -a "$OUT"
