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
* REST API (port 8080): /alert, /node_down, /node_up, /status.
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
from ryu.lib.packet import packet, ethernet, ether_types
from ryu.topology import event as topo_event
from ryu.topology.api import get_link

STATS_PORT = 5005      # UDP port for routine stats / heartbeats
ALERT_PORT = 5006      # UDP port for ALERT packets
ALERT_QUEUE = 1        # priority queue created by setup_qos.sh
MON_MAC = '00:00:00:00:00:01'   # MAC of host "mon" (first host in topo.py)
REST_NAME = 'health_api'

PRIO_ROUTE = 1
PRIO_ALERT = 10
PRIO_QUARANTINE_DROP = 100
PRIO_QUARANTINE_ALLOW = 110

COOKIE_ROUTE = 1       # tags route flows so they can be flushed together
COOKIE_QUAR = 2        # tags quarantine flows
ALL_BITS = 0xFFFFFFFFFFFFFFFF


class L2Switch(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]
    _CONTEXTS = {'wsgi': WSGIApplication}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._init_state()
        kwargs['wsgi'].register(HealthApi, {REST_NAME: self})

    def _init_state(self):
        self.datapaths = {}        # dpid -> datapath
        self.adj = {}              # dpid -> {neighbour dpid: out port}
        self.sw_ports = {}         # dpid -> set of switch-to-switch ports
        self.hosts = {}            # mac -> (dpid, port)
        self.link_cost = {}        # (dpid, dpid) -> cost (default 1)
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

    def flush_routes(self):
        for dp in self.datapaths.values():
            self.delete_flows(dp, COOKIE_ROUTE)

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
        self.flush_routes()
        for ip, node in list(self.quarantined.items()):
            self.quarantine(node, ip)           # re-install with new paths
        self.logger.info("topology: %s",
                         {d: sorted(n) for d, n in self.adj.items()})

    def cost(self, a, b):
        return self.link_cost.get((a, b), 1)

    def next_switch(self, src, dst):
        """Next-hop switch on the cheapest path src -> dst (Dijkstra)."""
        dist, first, pq = {src: 0}, {}, [(0, src)]
        while pq:
            d, u = heapq.heappop(pq)
            if d > dist.get(u, float('inf')):
                continue
            for v in self.adj.get(u, {}):
                nd = d + self.cost(u, v)
                if nd < dist.get(v, float('inf')):
                    dist[v] = nd
                    first[v] = v if u == src else first[u]
                    heapq.heappush(pq, (nd, v))
        return first.get(dst)

    def out_port_toward(self, dpid, mac):
        loc = self.hosts.get(mac)
        if loc is None:
            return None
        dst_dpid, dst_port = loc
        if dpid == dst_dpid:
            return dst_port
        nxt = self.next_switch(dpid, dst_dpid)
        return None if nxt is None else self.adj[dpid][nxt]

    @set_ev_cls([topo_event.EventSwitchEnter, topo_event.EventSwitchLeave,
                 topo_event.EventLinkAdd, topo_event.EventLinkDelete])
    def topology_changed(self, ev):
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

        eths = packet.Packet(msg.data).get_protocols(ethernet.ethernet)
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

        out = [parser.OFPActionOutput(out_port)]
        # match source AND destination so that every new sender still reaches
        # the controller once (that is how hosts are learned)
        self.add_flow(dp, PRIO_ROUTE, parser.OFPMatch(eth_src=src, eth_dst=dst),
                      out, idle=60, cookie=COOKIE_ROUTE)
        amatch = parser.OFPMatch(eth_src=src, eth_dst=dst, eth_type=0x0800,
                                 ip_proto=17, udp_dst=ALERT_PORT)
        aout = [parser.OFPActionSetQueue(ALERT_QUEUE),
                parser.OFPActionOutput(out_port)]
        self.add_flow(dp, PRIO_ALERT, amatch, aout, idle=60,
                      cookie=COOKIE_ROUTE)

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
        self.quarantined[ip] = node_id
        for dpid, dp in self.datapaths.items():
            ofp, parser = dp.ofproto, dp.ofproto_parser
            drop = parser.OFPMatch(eth_type=0x0800, ipv4_src=ip)
            self.add_flow(dp, PRIO_QUARANTINE_DROP, drop, [],
                          cookie=COOKIE_QUAR)
            mon_port = self.out_port_toward(dpid, MON_MAC)
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

    @route('health', '/status', methods=['GET'])
    def status(self, req, **kwargs):
        a = self.app
        return self._reply(200, {
            'switches': sorted(a.datapaths),
            'links': {str(d): sorted(n) for d, n in a.adj.items()},
            'hosts': {m: list(loc) for m, loc in a.hosts.items()},
            'quarantined': a.quarantined,
            'recent_alerts': a.alerts[-10:]})

