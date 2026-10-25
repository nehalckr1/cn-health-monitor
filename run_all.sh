#!/bin/bash
cd "$(dirname "$0")"
source ~/ryu-env/bin/activate
sudo mn -c >/dev/null 2>&1
mkdir -p results
sudo pkill -f "server/monitor" 2>/dev/null
ryu-manager controller/controller.py --observe-links > results/ryu.log 2>&1 &
RYU=$!
sleep 3
( sleep 20
  sudo bash topology/setup_qos.sh >/dev/null 2>&1
  sudo setsid nohup mnexec -a "$(pgrep -f 'mininet:mon$' | head -1)" python3 server/monitor.py >> monitor.out 2>&1 < /dev/null &
  echo "[run_all] QoS applied and monitor started" ) &
sudo python3 topology/topo.py
kill $RYU 2>/dev/null
sudo pkill -f "server/monitor" 2>/dev/null
sudo mn -c
