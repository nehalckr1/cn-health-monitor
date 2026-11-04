#!/bin/bash
# usage: tests/scenario_linkdown.sh <label> <run#>
source "$(dirname "$0")/lib.sh"; need_monitor
LABEL=${1:-dynamic}; RUN=${2:-1}
D2="$REPO/results/d2"; mkdir -p "$D2"
OUT="$D2/linkdown.csv"
[ -f "$OUT" ] || echo "label,run,lost_packets,outage_s,node_down_events,recovered_before_restore" > "$OUT"
set_mode "$LABEL"
INT=0.02
sudo pkill -f "agent.py --id"; sleep 1
hrun h2 python3 "$REPO/agent/agent.py" --id h2 --monitor 10.0.0.100 --port 5005 --interval $INT --fake-cpu >/dev/null 2>&1 &
sleep 6
START=$(wc -l < "$LOG")
sleep 2
sudo ip link set s1-eth8 down; sudo ip link set s2-eth1 down
sleep 10
sudo ip link set s1-eth8 up; sudo ip link set s2-eth1 up
sleep 6
new_log $START > "$D2/${LABEL}_linkdown_run${RUN}.log"
sudo pkill -f "agent.py --id"
W="$D2/${LABEL}_linkdown_run${RUN}.log"
LOST=$(awk -F'missing=' '/LOSS node=h2/ {split($2,a," "); s+=a[1]} END{print s+0}' "$W")
GAP=$(grep "ALERT node=h2" "$W" | awk '{split($1,t,":"); s=t[1]*3600+t[2]*60+t[3]; if(NR>1 && s-p>m) m=s-p; p=s} END{print m+0}')
OUTAGE=$(awk -v n="$LOST" -v i="$INT" -v g="$GAP" 'BEGIN{o=n*i; if(g>=3) o=g; printf "%.1f", o}')
DOWNS=$(grep -c "NODE_DOWN" "$W")
REC=$(awk -v o="$OUTAGE" 'BEGIN{print (o<9)?"yes":"no"}')
echo "$LABEL,$RUN,$LOST,$OUTAGE,$DOWNS,$REC" | tee -a "$OUT"
