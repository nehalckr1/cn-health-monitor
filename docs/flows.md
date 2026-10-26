# OpenFlow rules installed by the controller (Member B)

All switches use OpenFlow 1.3, table 0. Every rule the controller installs carries a
cookie so that groups of rules can be deleted without touching the others.

| Cookie | Meaning |
|---|---|
| 0x1 | route flows (normal forwarding) |
| 0x2 | quarantine flows |
| 0x3 | monitoring flows (UDP 5005 stats, UDP 5006 alerts) |

## Priorities

| Priority | Match | Action | Idle timeout |
|---|---|---|---|
| 0 | anything (table miss) | send to controller | none |
| 1 | eth_src, eth_dst | output to next hop on the shortest path | 60 s |
| 5 | eth_src, eth_dst, UDP dst 5005 | output on the (congestion-aware) path | 60 s |
| 10 | eth_src, eth_dst, UDP dst 5006 | set_queue:1, output on the (congestion-aware) path | 60 s |
| 100 | IPv4 src = quarantined node | drop | none |
| 110 | IPv4 src = quarantined node, UDP dst 5005 or 5006 | (set_queue:1 for alerts), output toward the monitor | none |

Route flows match source and destination MAC, so every new sender still reaches the
controller once and is learned.

## Behaviour

- **Learning:** hosts are learned from packets arriving on edge (host-facing) ports.
  Broadcasts and unknown destinations are dropped, never flooded, because the diamond
  topology has loops. topo.py installs static ARP entries instead.
- **Routing:** hop-by-hop shortest path (Dijkstra) over the links found with LLDP
  (--observe-links).
- **Alert priority:** UDP 5006 goes into queue 1 (created by setup_qos.sh, 4 Mbit/s
  guaranteed, highest priority) so alerts survive congestion.
- **Link failure:** when a link goes down or comes up, route and monitoring flows are
  flushed and the next packets are routed over the new shortest path.
- **Congestion response:** port counters are polled every 2 s. A link above 80 % of
  10 Mbit/s is "congested" and clears below 50 % (hysteresis, so it does not flap).
  Only monitoring traffic (5005/5006) is moved to another path; other traffic keeps its
  route. Monitoring flows are replaced in place, so there is no gap.
- **Quarantine:** /node_down installs a priority-100 drop for the node plus
  priority-110 exceptions so its stats and alerts still reach the monitor and the
  monitor can see it return. /node_up removes only that node's quarantine flows.
- **Baseline mode:** POST /mode {"mode":"baseline"} deletes all controller flows and
  turns the smart features off: no set_queue, no rerouting (topology frozen), no
  quarantine, no congestion reaction. {"mode":"dynamic"} turns them back on.

## REST API (port 8080, VM address 10.0.0.254)

| Method and path | Body | Effect |
|---|---|---|
| POST /alert | alert JSON from the monitor | logged, shown in /status |
| POST /node_down | {"node_id":"h3"} or {"ip":...} | quarantine the node |
| POST /node_up | same | lift the quarantine |
| GET /mode, POST /mode | {"mode":"baseline" or "dynamic"} | read or change the mode |
| GET /status | none | mode, switches, links, hosts, quarantined, congested, utilization, recent alerts |
