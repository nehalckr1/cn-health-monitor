#!/bin/bash
REPO="$HOME/health-monitor"
LOG="$REPO/monitor.log"
mkdir -p "$REPO/results/d1"
hpid() { pgrep -f "mininet:$1\$" | head -1; }
hrun() { local h=$1; shift; sudo mnexec -a "$(hpid $h)" "$@"; }
need_monitor() { pgrep -f "server/monitor" >/dev/null || { echo "FAIL: monitor not running"; exit 1; }; }
new_log() { tail -n +$(($1+1)) "$LOG"; }
