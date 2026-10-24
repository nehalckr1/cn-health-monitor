#!/bin/bash
# Member B - create a normal queue (0) and a priority queue (1) on every switch port.
# Run AFTER the topology is up:  sudo bash topology/setup_qos.sh
RATE=10000000
for sw in s1 s2 s3 s4; do
  for p in $(ovs-vsctl list-ports $sw); do
    ovs-vsctl clear port $p qos
  done
done
ovs-vsctl --all destroy qos
ovs-vsctl --all destroy queue
for sw in s1 s2 s3 s4; do
  for p in $(ovs-vsctl list-ports $sw); do
    ovs-vsctl -- set port $p qos=@q \
      -- --id=@q create qos type=linux-htb other-config:max-rate=$RATE queues:0=@q0 queues:1=@q1 \
      -- --id=@q0 create queue other-config:min-rate=1000000 other-config:max-rate=$RATE other-config:priority=1 \
      -- --id=@q1 create queue other-config:min-rate=4000000 other-config:max-rate=$RATE other-config:priority=0
    echo "QoS set on $p"
  done
done
