#!/usr/bin/env python3
"""Member B - topology for the UDP health-monitoring project.

   h1..hN, bg1 --- s1 --- s2 --- s4 --- mon, bg2, (VM "root" = 10.0.0.254)
                    |             |
                    +----- s3 ----+      (redundant path, on by default)

* The VM is attached to s4 as 10.0.0.254, so hosts inside Mininet can reach
  Ryu's REST API at http://10.0.0.254:8080 .
* The controller never floods (the topology has a loop), so ARP is filled in
  statically, IPv6 is switched off, and every host pings once at start-up so
  the controller learns where each host is.

Run:  sudo python3 topo.py --agents 6 [--no-redundant] [--pingall]
"""
import argparse
import time
from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch, Node
from mininet.link import TCLink
from mininet.cli import CLI
from mininet.log import setLogLevel, info


def build(n_agents, redundant, ctrl_ip, ctrl_port):
    net = Mininet(controller=None, switch=OVSSwitch, link=TCLink,
                  autoSetMacs=True)
    net.addController('c0', controller=RemoteController,
                      ip=ctrl_ip, port=ctrl_port)

    s = {i: net.addSwitch('s%d' % i, protocols='OpenFlow13') for i in (1, 2, 3, 4)}

    # NOTE: "mon" must stay the FIRST host: it gets MAC 00:00:00:00:00:01,
    # which controller.py uses (MON_MAC).
    mon = net.addHost('mon', ip='10.0.0.100/24')
    bg1 = net.addHost('bg1', ip='10.0.0.201/24')
    bg2 = net.addHost('bg2', ip='10.0.0.202/24')
    agents = [net.addHost('h%d' % i, ip='10.0.0.%d/24' % i)
              for i in range(1, n_agents + 1)]

    for h in agents + [bg1]:
        net.addLink(h, s[1], bw=100)
    for h in (mon, bg2):
        net.addLink(h, s[4], bw=100)

    net.addLink(s[1], s[2], bw=10, delay='5ms')
    net.addLink(s[2], s[4], bw=10, delay='5ms')
    net.addLink(s[1], s[3], bw=10, delay='5ms')
    if redundant:
        net.addLink(s[3], s[4], bw=10, delay='5ms')
    return net


def start_with_root(net, switch, ip):
    """Attach the VM (root namespace) to a switch, then start the network."""
    root = Node('root', inNamespace=False)
    intf = net.addLink(root, switch).intf1
    root.setIP(ip, intf=intf)
    net.start()
    intf.updateAddr()          # read the real IP and MAC from the system
    return root, intf


def static_arp_and_no_ipv6(net, root, root_intf):
    """No broadcasts at all: every host (and the VM) knows every MAC."""
    peers = [(h, h.IP(), h.MAC(), h.defaultIntf().name) for h in net.hosts]
    if root is not None:
        peers.append((root, root_intf.IP(), root_intf.MAC(), root_intf.name))
    for node, own_ip, _, nic in peers:
        for _, ip, mac, _ in peers:
            if ip != own_ip:
                node.cmd('ip neigh replace %s lladdr %s dev %s nud permanent'
                         % (ip, mac, nic))
        if node is root:
            node.cmd('sysctl -qw net.ipv6.conf.%s.disable_ipv6=1' % nic)
        else:
            node.cmd('sysctl -qw net.ipv6.conf.all.disable_ipv6=1')


def warm_up(net, root):
    """One ping from every host so the controller learns every location."""
    mon, bg1 = net.get('mon'), net.get('bg1')
    for _ in range(2):
        for h in net.hosts:
            target = bg1.IP() if h is mon else mon.IP()
            h.cmd('ping -c 1 -W 1 %s > /dev/null 2>&1 &' % target)
        if root is not None:
            root.cmd('ping -c 1 -W 1 %s > /dev/null 2>&1 &' % mon.IP())
        time.sleep(1.5)


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--agents', type=int, default=6)
    ap.add_argument('--no-redundant', action='store_true',
                    help='leave out the s3-s4 link (no loop)')
    ap.add_argument('--ctrl-ip', default='127.0.0.1')
    ap.add_argument('--ctrl-port', type=int, default=6653)
    ap.add_argument('--root-ip', default='10.0.0.254/24',
                    help='VM address on the Mininet network ("" to disable)')
    ap.add_argument('--wait', type=float, default=6,
                    help='seconds to wait for link discovery')
    ap.add_argument('--pingall', action='store_true')
    a = ap.parse_args()

    setLogLevel('info')
    net = build(a.agents, not a.no_redundant, a.ctrl_ip, a.ctrl_port)
    root = root_intf = None
    if a.root_ip:
        root, root_intf = start_with_root(net, net.get('s4'), a.root_ip)
    else:
        net.start()
    static_arp_and_no_ipv6(net, root, root_intf)
    info('*** Waiting %ss for link discovery\n' % a.wait)
    time.sleep(a.wait)
    info('*** Warming up (every host pings once)\n')
    warm_up(net, root)
    if a.pingall:
        net.pingAll()
    CLI(net)
    net.stop()
