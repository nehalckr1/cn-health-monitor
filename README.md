# Remote System Health Monitoring (UDP) with SDN

UDP agents on emulated hosts report CPU, memory, process count and network counters to a
central monitor. The monitor raises threshold alerts, detects node failures from missed
heartbeats, and tells an SDN controller (Ryu, OpenFlow 1.3) over REST, so critical alert
traffic is prioritised and rerouted around congestion and link failures.

## Architecture

    agents (h1..h6) --UDP 5005 stats / 5006 alerts--> monitor (mon, 10.0.0.100)
                                                        |  REST (port 8080)
                                                        v
                     Mininet diamond topology  <--  Ryu controller (OpenFlow 1.3)
                     s1 -- s2 -- s4  and  s1 -- s3 -- s4   (10 Mbit/s links)

- Alerts use UDP port 5006 and are put in a high-priority OVS queue (queue 1, 4 Mbit/s
  guaranteed, priority 0); stats use port 5005.
- The controller has two modes, switchable at runtime: `dynamic` (priority, rerouting,
  quarantine, congestion response) and `baseline` (static paths, no priority).

## Repository layout

    agent/       agent.py        UDP health agent (psutil)
    server/      monitor.py      UDP monitor, thresholds, DOWN/UP detection, REST client
                 config.json     thresholds, ports, log file
    controller/  controller.py   Ryu controller + REST API (/alert /node_down /node_up /status /mode)
    topology/    topo.py         Mininet diamond topology
                 setup_qos.sh    creates the OVS priority queues (run after topology start)
    tests/       D1 tests, scenario scripts, run_experiments.sh
    results/     d1/ test output, d2/ experiment CSVs, graphs/ figures, make_graphs.py
    docs/        protocol.md (packet format), flows.md (OpenFlow rules)

## Install (Ubuntu VM)

    sudo apt update
    sudo apt install -y mininet openvswitch-switch iperf curl git \
        python3-psutil python3-requests python3-matplotlib
    sudo systemctl enable --now openvswitch-switch

Ryu needs an old Python and old build tools (it does not build on Python 3.12+):

    curl -LsSf https://astral.sh/uv/install.sh | sh
    source ~/.local/bin/env
    uv python install 3.9
    uv venv --python 3.9 ~/ryu-env
    source ~/ryu-env/bin/activate
    uv pip install "setuptools==58.2.0" wheel eventlet==0.30.2
    uv pip install "ryu==4.34" --no-build-isolation
    ryu-manager --version        # ryu-manager 4.34

## Run

    ./run_all.sh

This starts Ryu, the topology, the QoS queues and the monitor, and leaves you at the
`mininet>` prompt. Wait for "[run_all] QoS applied and monitor started", then run
`pingall` twice (the controller learns hosts from the first packets). Use a second
terminal for tests and experiments.

Manual start (three terminals): (1) `source ~/ryu-env/bin/activate && ryu-manager
controller/controller.py --observe-links`; (2) `sudo python3 topology/topo.py`;
(3) `sudo bash topology/setup_qos.sh`, then start the monitor inside host `mon`.

Switch controller mode: `curl -X POST http://127.0.0.1:8080/mode -H "Content-Type:
application/json" -d '{"mode":"baseline"}'` (or `"dynamic"`); `GET /mode` shows it.

## Tests and experiments

    ./tests/test_all_agents.sh   # every agent reaches the monitor
    ./tests/test_alert.sh        # threshold alert fires and the controller accepts it
    ./tests/test_node_down.sh    # killed agent marked DOWN and reported
    ./tests/run_experiments.sh   # congestion + link-down, 3 runs per mode
    ./tests/scenario_nodedown.sh <mode> <run>
    ./tests/scenario_scale.sh <mode> <agents> <run>
    python3 results/make_graphs.py

Each scenario script appends a row to a CSV in `results/d2/` and keeps the raw monitor log
window next to it.

## Results (3 runs per mode)

| Scenario | Dynamic | Baseline |
|---|---|---|
| Alert latency under congestion (avg / p95 / max) | 0.16 / 0.34 / 1.2 ms | 189 / 261 / 310 ms |
| Link failure on the primary path | failover under 1 s, no DOWN event | no alerts for the whole 10 s outage |
| Node-failure detection (monitor) | 3.3-3.6 s (`down_after` = 3 s) | same |
| Controller notified after DOWN | 1-6 ms | 1-1.3 ms (call accepted, ignored) |
| 5 / 10 / 20 agents | avg 0.1-0.2 ms, p95 under 1 ms, 0 lost, 0 DOWN | not run |

## Known limitations

- Applying the OVS QoS replaces Mininet's `netem` queues, so the 5 ms per-hop link delay is
  not active during experiments; absolute latencies are lower than the topology specifies.
  Comparisons between modes are unaffected.
- The baseline link-down outage is capped by the test (the link is restored after 10 s).
- Node-failure detection time is set by the `down_after` threshold, not by the network.
- Scalability was tested up to 20 agents on a 2-vCPU VM; no limit was reached.
- The monitor resets its sequence tracking when a node returns from DOWN, so packet loss over
  long outages is measured from gaps between alerts, not from sequence numbers.

## Team

Member A: agent and monitor (sockets). Member B: Mininet topology, controller, QoS.
Member C: integration, tests, experiments, results.
