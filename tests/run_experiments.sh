#!/bin/bash
cd "$(dirname "$0")/.."
for mode in dynamic baseline; do
  for run in 1 2 3; do
    ./tests/scenario_congestion.sh $mode $run
    ./tests/scenario_linkdown.sh $mode $run
  done
done
curl -s -X POST http://127.0.0.1:8080/mode -H "Content-Type: application/json" -d '{"mode":"dynamic"}'; echo
echo "DONE"
