#!/bin/bash
# usage: tests/scenario_nodedown.sh <label> <run#>
source "$(dirname "$0")/lib.sh"; need_monitor
LABEL=${1:-dynamic}; RUN=${2:-1}
D2="$REPO/results/d2"; mkdir -p "$D2"
OUT="$D2/nodedown.csv"
[ -f "$OUT" ] || echo "label,run,detect_s,controller_ms" > "$OUT"
set_mode "$LABEL"
sudo pkill -f "agent.py --id"; sleep 1
hrun h1 python3 "$REPO/agent/agent.py" --id h1 --monitor 10.0.0.100 --port 5005 --interval 0.5 >/dev/null 2>&1 &
sleep 6
START=$(wc -l < "$LOG")
sudo pkill -f "agent.py --id h1"
sleep 8
W="$D2/${LABEL}_nodedown_run${RUN}.log"; new_log $START > "$W"
DET=$(grep -o "NODE_DOWN node=h1 silent_for=[0-9.]*" "$W" | head -1 | sed 's/.*=//')
CMS=$(grep "CONTROLLER POST /node_down node=h1" "$W" | head -1 | sed 's/.* in //; s/ ms.*//')
echo "$LABEL,$RUN,${DET:-NA},${CMS:-NA}" | tee -a "$OUT"
