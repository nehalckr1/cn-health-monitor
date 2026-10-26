"""Member B - Ryu controller: loop-safe path routing + alert priority + REST.

Run:  ryu-manager --observe-links controller/controller.py --verbose

* Switch-to-switch links are discovered with LLDP (--observe-links).
* Hosts are learned from packets that arrive on host-facing (edge) ports.
* Traffic is forwarded hop by hop along the shortest path.  Broadcasts and
  unknown destinations are DROPPED, never flooded, so a redundant topology
  with loops cannot cause a broadcast storm (topo.py uses static ARP).
* When a link goes down or comes up, all route flows are flushed and the
  next packets are routed over the new shortest path.
* UDP port 5006 (alerts) is sent through priority queue 1.
* Congestion response: port counters are read every POLL_INTERVAL seconds.
  A switch-to-switch link above UTIL_HIGH of its capacity is "congested";
  MONITORING traffic (UDP 5005/5006) is then routed around it, while all
  other traffic keeps its normal path (so the two paths do not flip-flop).
* REST API (port 8080): /alert, /node_down, /node_up, /status, /mode.
"""
import heapq
import json
import re
import time

from webob import Response
from ryu.app.wsgi import ControllerBase, WSGIApplication, route
from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib import hub
from ryu.lib.packet import packet, ethernet, ether_types, udp
from ryu.topology import event as topo_event
from ryu.topology.api import get_link

STATS_PORT = 5005      # UDP port for routine stats / heartbeats
ALERT_PORT = 5006      # UDP port for ALERT packets
ALERT_QUEUE = 1        # priority queue created by setup_qos.sh
MON_MAC = '00:00:00:00:00:01'   # MAC of host "mon" (first host in topo.py)
REST_NAME = 'health_api'

POLL_INTERVAL = 2          # seconds between port-statistics requests
LINK_CAPACITY_BPS = 10e6   # inter-switch links are 10 Mbit/s (topo.py)
UTIL_HIGH = 0.8            # link becomes "congested" at 80 % utilisation
UTIL_LOW = 0.5             # ... and clears again below 50 %
CONGESTED_COST = 10        # path cost of a congested link (normal = 1)

PRIO_ROUTE = 1
PRIO_STATS = 5
PRIO_ALERT = 10
PRIO_QUARANTINE_DROP = 100
PRIO_QUARANTINE_ALLOW = 110

COOKIE_ROUTE = 1       # tags route flows so they can be flushed together
COOKIE_QUAR = 2        # tags quarantine flows
COOKIE_MON = 3         # tags monitoring (UDP 5005/5006) route flows
ALL_BITS = 0xFFFFFFFFFFFFFFFF


class L2Switch(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]
    _CONTEXTS = {'wsgi': WSGIApplication}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._init_state()
        kwargs['wsgi'].register(HealthApi, {REST_NAME: self})
        self.poller = hub.spawn(self._poll_loop)

    def _init_state(self):
        self.mode = 'dynamic'      # 'dynamic' (smart) or 'baseline' (static)
        self.datapaths = {}        # dpid -> datapath
        self.adj = {}              # dpid -> {neighbour dpid: out port}
        self.sw_ports = {}         # dpid -> set of switch-to-switch ports
        self.hosts = {}            # mac -> (dpid, port)
        self.link_cost = {}        # (dpid, dpid) -> cost for MONITORING flows
        self.congested = set()     # {(dpid, dpid)} links above UTIL_HIGH
        self.link_util = {}        # (dpid, dpid) -> last utilisation 0..1
        self.port_prev = {}        # (dpid, port) -> (tx_bytes, time)
        self.mon_flows = set()     # (dpid, src mac, dst mac) with monitoring flows
        self.quarantined = {}      # ip -> node_id
        self.alerts = []           # recent alerts from the monitor

    # ---------- flow helpers ----------
    def add_flow(self, dp, priority, match, actions, idle=0, cookie=0):
        ofp, parser = dp.ofproto, dp.ofproto_parser
        inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        dp.send_msg(parser.OFPFlowMod(datapath=dp, cookie=cookie,
                                      priority=priority, match=match,
                                      instructions=inst, idle_timeout=idle))

    def delete_flows(self, dp, cookie, match=None):
        ofp, parser = dp.ofproto, dp.ofproto_parser
        dp.send_msg(parser.OFPFlowMod(
            datapath=dp, command=ofp.OFPFC_DELETE, table_id=ofp.OFPTT_ALL,
            out_port=ofp.OFPP_ANY, out_group=ofp.OFPG_ANY,
            cookie=cookie, cookie_mask=ALL_BITS,
            match=match or parser.OFPMatch()))

    def flush_routes(self, monitoring_only=False):
        for dp in self.datapaths.values():
            if not monitoring_only:
                self.delete_flows(dp, COOKIE_ROUTE)
            self.delete_flows(dp, COOKIE_MON)

    # ---------- topology ----------
    def fetch_links(self):
        """[(src_dpid, src_port, dst_dpid, dst_port), ...] from Ryu."""
        return [(l.src.dpid, l.src.port_no, l.dst.dpid, l.dst.port_no)
                for l in get_link(self, None)]

    def refresh_topology(self):
        self.adj, self.sw_ports = {}, {}
        for sd, sp, dd, dp_ in self.fetch_links():
            self.adj.setdefault(sd, {})[dd] = sp
            self.sw_ports.setdefault(sd, set()).add(sp)
            self.sw_ports.setdefault(dd, set()).add(dp_)
        # forget "hosts" that were wrongly learned on a switch-to-switch port
        for mac, (dpid, port) in list(self.hosts.items()):
            if port in self.sw_ports.get(dpid, ()):
                del self.hosts[mac]
        self.congested = {l for l in self.congested
                          if l[1] in self.adj.get(l[0], {})}
        self.link_cost = {l: CONGESTED_COST for l in self.congested}
        self.flush_routes()
        self.mon_flows.clear()
        for ip, node in list(self.quarantined.items()):
            self.quarantine(node, ip)           # re-install with new paths
        self.logger.info("topology: %s",
                         {d: sorted(n) for d, n in self.adj.items()})

    def cost(self, a, b, monitoring=False):
        return self.link_cost.get((a, b), 1) if monitoring else 1

    def next_switch(self, src, dst, monitoring=False):
        """Next-hop switch on the cheapest path src -> dst (Dijkstra).
        monitoring=True makes congested links expensive."""
        dist, first, pq = {src: 0}, {}, [(0, src)]
        while pq:
            d, u = heapq.heappop(pq)
            if d > dist.get(u, float('inf')):
                continue
            for v in self.adj.get(u, {}):
                nd = d + self.cost(u, v, monitoring)
                if nd < dist.get(v, float('inf')):
                    dist[v] = nd
                    first[v] = v if u == src else first[u]
                    heapq.heappush(pq, (nd, v))
        return first.get(dst)

    def out_port_toward(self, dpid, mac, monitoring=False):
        loc = self.hosts.get(mac)
        if loc is None:
            return None
        dst_dpid, dst_port = loc
        if dpid == dst_dpid:
            return dst_port
        nxt = self.next_switch(dpid, dst_dpid, monitoring)
        return None if nxt is None else self.adj[dpid][nxt]

    # ---------- congestion response ----------
    def _poll_once(self):
        for dp in list(self.datapaths.values()):
            ofp, parser = dp.ofproto, dp.ofproto_parser
            dp.send_msg(parser.OFPPortStatsRequest(dp, 0, ofp.OFPP_ANY))

    def _poll_loop(self):
        while True:
            self._poll_once()
            hub.sleep(POLL_INTERVAL)

    def install_monitoring_flows(self, dp, src, dst, port):
        """(Re)install the UDP 5005/5006 flows of one src->dst pair.  An ADD
        with the same match REPLACES the old flow, so there is no gap in which
        the plain route flow could catch the packets."""
        parser = dp.ofproto_parser
        for udp_port, prio, pre in (
                (STATS_PORT, PRIO_STATS, []),
                (ALERT_PORT, PRIO_ALERT,
                 [parser.OFPActionSetQueue(ALERT_QUEUE)])):
            m = parser.OFPMatch(eth_src=src, eth_dst=dst, eth_type=0x0800,
                                ip_proto=17, udp_dst=udp_port)
            self.add_flow(dp, prio, m, pre + [parser.OFPActionOutput(port)],
                          idle=60, cookie=COOKIE_MON)
        self.mon_flows.add((dp.id, src, dst))

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def port_stats_reply(self, ev):
        stats = [(s.port_no, s.tx_bytes) for s in ev.msg.body]
        self.handle_port_stats(ev.msg.datapath.id, stats, time.time())

    def neighbor_on_port(self, dpid, port):
        for nb, p in self.adj.get(dpid, {}).items():
            if p == port:
                return nb
        return None

    def set_mode(self, mode):
        """Switch between 'dynamic' and 'baseline'.  All flows installed by
        this app are deleted so the new mode takes effect immediately."""
        if mode == self.mode:
            return
        for dp in self.datapaths.values():
            for cookie in (COOKIE_ROUTE, COOKIE_QUAR, COOKIE_MON):
                self.delete_flows(dp, cookie)
        self.quarantined.clear()
        self.congested.clear()
        self.link_cost = {}
        self.mon_flows.clear()
        self.mode = 'dynamic'              # let refresh_topology take a fresh
        self.refresh_topology()            # snapshot of the live topology
        self.mode = mode                   # baseline: topology is frozen now
        self.logger.info("MODE -> %s", mode)

    def handle_port_stats(self, dpid, stats, now):
        """stats = [(port_no, tx_bytes), ...]; updates utilisation and the
        congested set, and re-routes monitoring traffic when it changes."""
        changed = False
        for port, tx_bytes in stats:
            nb = self.neighbor_on_port(dpid, port)
            if nb is None:                       # host-facing port: ignore
                continue
            prev = self.port_prev.get((dpid, port))
            self.port_prev[(dpid, port)] = (tx_bytes, now)
            if prev is None or now <= prev[1]:
                continue
            bps = max(tx_bytes - prev[0], 0) * 8 / (now - prev[1])
            util = bps / LINK_CAPACITY_BPS
            link = (dpid, nb)
            self.link_util[link] = util
            if self.mode == 'baseline':          # measure only, never react
                continue
            if link not in self.congested and util >= UTIL_HIGH:
                self.congested.add(link)
                changed = True
            elif link in self.congested and util < UTIL_LOW:
                self.congested.discard(link)
                changed = True
        if changed:
            self.apply_congestion()

    def apply_congestion(self):
        self.link_cost = {l: CONGESTED_COST for l in self.congested}
        for dpid, src, dst in list(self.mon_flows):
            dp = self.datapaths.get(dpid)
            port = self.out_port_toward(dpid, dst, monitoring=True)
            if dp is not None and port is not None:
                self.install_monitoring_flows(dp, src, dst, port)
        for ip, node in list(self.quarantined.items()):
            self.quarantine(node, ip)
        self.logger.info("CONGESTED links: %s", sorted(self.congested))

    @set_ev_cls([topo_event.EventSwitchEnter, topo_event.EventSwitchLeave,
                 topo_event.EventLinkAdd, topo_event.EventLinkDelete])
    def topology_changed(self, ev):
        if self.mode == 'baseline':          # static: ignore link changes
            return
        self.refresh_topology()

    # ---------- OpenFlow events ----------
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features(self, ev):
        dp = ev.msg.datapath
        ofp, parser = dp.ofproto, dp.ofproto_parser
        self.datapaths[dp.id] = dp
        actions = [parser.OFPActionOutput(ofp.OFPP_CONTROLLER,
                                          ofp.OFPCML_NO_BUFFER)]
        self.add_flow(dp, 0, parser.OFPMatch(), actions)
        self.logger.info("switch %016x connected", dp.id)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in(self, ev):
        msg = ev.msg
        dp = msg.datapath
        ofp, parser = dp.ofproto, dp.ofproto_parser
        in_port = msg.match['in_port']

        pkt = packet.Packet(msg.data)
        eths = pkt.get_protocols(ethernet.ethernet)
        if not eths or eths[0].ethertype == ether_types.ETH_TYPE_LLDP:
            return
        src, dst = eths[0].src, eths[0].dst

        if in_port not in self.sw_ports.get(dp.id, ()):     # edge port
            self.hosts[src] = (dp.id, in_port)              # learn host

        if int(dst.split(':')[0], 16) & 1:   # broadcast / multicast
            return                           # never flood (loops!)
        out_port = self.out_port_toward(dp.id, dst)
        if out_port is None:                 # unknown destination
            return
        baseline = self.mode == 'baseline'
        mon_port = out_port if baseline else (
            self.out_port_toward(dp.id, dst, monitoring=True) or out_port)

        # match source AND destination so that every new sender still reaches
        # the controller once (that is how hosts are learned)
        self.add_flow(dp, PRIO_ROUTE, parser.OFPMatch(eth_src=src, eth_dst=dst),
                      [parser.OFPActionOutput(out_port)], idle=60,
                      cookie=COOKIE_ROUTE)
        # monitoring traffic: congestion-aware path; alerts use queue 1
        if not baseline:
            self.install_monitoring_flows(dp, src, dst, mon_port)

        # forward the packet that triggered this, the same way its flow will
        u = pkt.get_protocol(udp.udp)
        if baseline:
            out = [parser.OFPActionOutput(out_port)]
        elif u is not None and u.dst_port == STATS_PORT:
            out = [parser.OFPActionOutput(mon_port)]
        elif u is not None and u.dst_port == ALERT_PORT:
            out = [parser.OFPActionSetQueue(ALERT_QUEUE),
                   parser.OFPActionOutput(mon_port)]
        else:
            out = [parser.OFPActionOutput(out_port)]
        data = msg.data if msg.buffer_id == ofp.OFP_NO_BUFFER else None
        dp.send_msg(parser.OFPPacketOut(datapath=dp, buffer_id=msg.buffer_id,
                                        in_port=in_port, actions=out,
                                        data=data))

    # ---------- actions used by the REST API ----------
    def record_alert(self, info):
        info = dict(info)
        info['received'] = time.time()
        self.alerts.append(info)
        del self.alerts[:-50]                  # keep the last 50
        self.logger.info("ALERT from monitor: %s", info)

    def quarantine(self, node_id, ip):
        """Drop everything from this node, except its stats/alert packets to
        the monitor (so the monitor can still see it come back)."""
        if self.mode == 'baseline':
            self.logger.info("baseline: NODE_DOWN %s ignored", node_id)
            return
        self.quarantined[ip] = node_id
        for dpid, dp in self.datapaths.items():
            ofp, parser = dp.ofproto, dp.ofproto_parser
            drop = parser.OFPMatch(eth_type=0x0800, ipv4_src=ip)
            self.add_flow(dp, PRIO_QUARANTINE_DROP, drop, [],
                          cookie=COOKIE_QUAR)
            mon_port = self.out_port_toward(dpid, MON_MAC, monitoring=True)
            if mon_port is None:
                continue
            for udp_port in (STATS_PORT, ALERT_PORT):
                allow = parser.OFPMatch(eth_type=0x0800, ip_proto=17,
                                        ipv4_src=ip, udp_dst=udp_port)
                actions = []
                if udp_port == ALERT_PORT:
                    actions.append(parser.OFPActionSetQueue(ALERT_QUEUE))
                actions.append(parser.OFPActionOutput(mon_port))
                self.add_flow(dp, PRIO_QUARANTINE_ALLOW, allow, actions,
                              cookie=COOKIE_QUAR)
        self.logger.info("QUARANTINED %s (%s)", node_id, ip)

    def restore(self, node_id, ip):
        """Remove every quarantine flow that matches this node's IP."""
        if self.mode == 'baseline':
            self.logger.info("baseline: NODE_UP %s ignored", node_id)
            return
        self.quarantined.pop(ip, None)
        for dp in self.datapaths.values():
            match = dp.ofproto_parser.OFPMatch(eth_type=0x0800, ipv4_src=ip)
            self.delete_flows(dp, COOKIE_QUAR, match)
        self.logger.info("RESTORED %s (%s)", node_id, ip)


class HealthApi(ControllerBase):
    def __init__(self, req, link, data, **config):
        super().__init__(req, link, data, **config)
        self.app = data[REST_NAME]

    @staticmethod
    def _reply(status, obj):
        return Response(status=status, content_type='application/json',
                        body=json.dumps(obj).encode('utf-8'))

    @staticmethod
    def _body(req):
        raw = req.body.decode('utf-8') if req.body else '{}'
        data = json.loads(raw or '{}')
        if not isinstance(data, dict):
            raise ValueError('body must be a JSON object')
        return data

    @staticmethod
    def _node(body):
        """Return (node_id, ip) from {"node_id":"h3"} or {"ip":"10.0.0.3"}."""
        node_id = str(body.get('node_id', ''))
        ip = body.get('ip')
        if not ip:
            m = re.fullmatch(r'h(\d+)', node_id)
            if not m:
                raise ValueError('need "node_id" like "h3" or an "ip"')
            ip = '10.0.0.%d' % int(m.group(1))
        return node_id or ip, str(ip)

    @route('health', '/alert', methods=['POST'])
    def alert(self, req, **kwargs):
        try:
            body = self._body(req)
        except ValueError as e:
            return self._reply(400, {'error': str(e)})
        self.app.record_alert(body)
        return self._reply(200, {'status': 'ok'})

    @route('health', '/node_down', methods=['POST'])
    def node_down(self, req, **kwargs):
        try:
            node_id, ip = self._node(self._body(req))
        except ValueError as e:
            return self._reply(400, {'error': str(e)})
        self.app.quarantine(node_id, ip)
        return self._reply(200, {'status': 'quarantined', 'node': node_id,
                                 'ip': ip})

    @route('health', '/node_up', methods=['POST'])
    def node_up(self, req, **kwargs):
        try:
            node_id, ip = self._node(self._body(req))
        except ValueError as e:
            return self._reply(400, {'error': str(e)})
        self.app.restore(node_id, ip)
        return self._reply(200, {'status': 'restored', 'node': node_id,
                                 'ip': ip})

    @route('health', '/mode', methods=['GET'])
    def get_mode(self, req, **kwargs):
        return self._reply(200, {'mode': self.app.mode})

    @route('health', '/mode', methods=['POST'])
    def post_mode(self, req, **kwargs):
        try:
            mode = str(self._body(req).get('mode', ''))
        except ValueError as e:
            return self._reply(400, {'error': str(e)})
        if mode not in ('dynamic', 'baseline'):
            return self._reply(400, {'error': 'mode must be "dynamic" or '
                                              '"baseline"'})
        self.app.set_mode(mode)
        return self._reply(200, {'mode': self.app.mode})

    @route('health', '/status', methods=['GET'])
    def status(self, req, **kwargs):
        a = self.app
        return self._reply(200, {
            'mode': a.mode,
            'switches': sorted(a.datapaths),
            'links': {str(d): sorted(n) for d, n in a.adj.items()},
            'hosts': {m: list(loc) for m, loc in a.hosts.items()},
            'quarantined': a.quarantined,
            'congested': ['%s-%s' % l for l in sorted(a.congested)],
            'utilization': {'%s-%s' % l: round(u, 2)
                            for l, u in sorted(a.link_util.items())},
            'recent_alerts': a.alerts[-10:]})
