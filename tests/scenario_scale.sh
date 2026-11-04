#!/bin/bash
# usage: tests/scenario_scale.sh <label> <N agents> <run#>
source "$(dirname "$0")/lib.sh"; need_monitor
LABEL=${1:-dynamic}; N=${2:-5}; RUN=${3:-1}
D2="$REPO/results/d2"; mkdir -p "$D2"
OUT="$D2/scalability.csv"
[ -f "$OUT" ] || echo "label,agents,run,alerts,avg_ms,p95_ms,max_ms,lost_packets,down_events" > "$OUT"
set_mode "$LABEL"
sudo pkill -f "agent.py --id"; sleep 1
START=$(wc -l < "$LOG")
for i in $(seq 1 $N); do
  hrun h$(( (i-1) % 6 + 1 )) python3 "$REPO/agent/agent.py" --id s$i --monitor 10.0.0.100 --port 5005 --interval 1 --fake-cpu >/dev/null 2>&1 &
done
sleep 25
W="$D2/${LABEL}_scale${N}_run${RUN}.log"; new_log $START > "$W"
sudo pkill -f "agent.py --id"
STATS=$(grep -o "latency_ms=[0-9.]*" "$W" | cut -d= -f2 | sort -n | awk '{a[NR]=$1; s+=$1} END{ if(NR==0){print "0,0,0,0"; exit} i=int(NR*0.95); if(i<1)i=1; printf "%d,%.2f,%.2f,%.2f", NR, s/NR, a[i], a[NR] }')
LOST=$(awk -F'missing=' '/LOSS node=/ {split($2,a," "); s+=a[1]} END{print s+0}' "$W")
DOWNS=$(grep -c "NODE_DOWN" "$W")
echo "$LABEL,$N,$RUN,$STATS,$LOST,$DOWNS" | tee -a "$OUT"
