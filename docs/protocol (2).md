# Protocol Specification (Project 11: Remote System Health Monitoring, UDP)

All messages are one JSON object sent in one UDP datagram.

## Ports
| Port | Used for | Marking |
|------|----------|---------|
| 5005 | Routine STATS / HEARTBEAT | none (best effort) |
| 5006 | ALERT messages | DSCP EF (46), IP_TOS = 0xB8 (184) |

The monitor listens on both ports on 0.0.0.0.

## Message types
- `STATS`: sent by every agent every `interval` seconds (default 1 s). It also acts as the heartbeat.
- `HEARTBEAT`: minimal packet (type, node_id, seq, timestamp only). The monitor accepts it, but the default agent sends STATS.
- `ALERT`: sent by an agent on port 5006 when its CPU or memory crosses a threshold.

## Fields
| Field | Type | Meaning |
|-------|------|---------|
| type | string | STATS, HEARTBEAT or ALERT |
| node_id | string | unique agent name, e.g. "h3" |
| seq | int | starts at 1, +1 for every packet of that type from this agent |
| timestamp | float | send time, seconds since epoch (time.time()). Used for alert latency |
| cpu | float | CPU percent (0-100) |
| mem | float | memory percent (0-100) |
| procs | int | number of processes |
| net_rx | int | bytes received since boot |
| net_tx | int | bytes sent since boot |

ALERT packets also carry:
| Field | Type | Meaning |
|-------|------|---------|
| severity | string | "WARNING" or "CRITICAL" |
| reason | string | e.g. "cpu 95.0 > 85" |

## Examples
```
{"type":"STATS","node_id":"h3","seq":42,"timestamp":1729750000.123,"cpu":37.5,"mem":61.2,"procs":148,"net_rx":120394,"net_tx":88211}
{"type":"ALERT","node_id":"h3","seq":1,"timestamp":1729750010.5,"cpu":95.0,"mem":61.2,"procs":148,"net_rx":120394,"net_tx":88211,"severity":"CRITICAL","reason":"cpu 95.0 > 85"}
```

## Monitor rules
- Packet that is not valid JSON, or is missing a required field: logged as MALFORMED and dropped.
- seq lower than or equal to the last seq of that type: counted as duplicate / out of order and ignored (exception: seq 1 or a node currently DOWN means the agent restarted).
- seq jumps ahead: gap is counted as lost packets.
- Node is DOWN when no packet arrives for `down_after * interval` seconds (default 3 * 1 = 3 s).
- Node is UP again when any valid packet arrives from a DOWN node.
- Thresholds (cpu 85, mem 90) and `down_after` are in `server/config.json`.

## Controller hooks (D2, agreed with Member B)
On a CRITICAL ALERT or node DOWN the monitor will POST JSON to the controller:
`/alert`, `/node_down`, `/node_up` with body like `{"node_id":"h3"}`.
