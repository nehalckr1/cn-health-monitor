#!/bin/bash
# Make a Mininet command file that starts N agents (h1..hN) and one monitor.
# Usage:   bash agent/gen_agents.sh 40 [fake_cpu_host_number]
# Then, at the mininet> prompt:   source /tmp/start_agents.cmd
# (the topology must have been started with:  topo.py --agents N)
N=${1:-6}
FAKE=${2:-0}
BASE=${BASE:-$(cd "$(dirname "$0")/.." && pwd)}
OUT=/tmp/start_agents.cmd
echo "mon python3 $BASE/server/monitor.py > /tmp/mon.out 2>&1 &" > $OUT
echo "py import time; time.sleep(1)" >> $OUT
for i in $(seq 1 $N); do
  EXTRA=""
  [ "$i" = "$FAKE" ] && EXTRA="--fake-cpu"
  echo "h$i python3 $BASE/agent/agent.py --id=h$i --monitor 10.0.0.100 $EXTRA > /tmp/h$i.out 2>&1 &" >> $OUT
done
echo "wrote $OUT ($N agents). At mininet> run:  source $OUT"
