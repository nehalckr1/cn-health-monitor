#!/usr/bin/env python3
"""Member B - topology for the UDP health-monitoring project.

   h1..hN, bg1 --- s1 --- s2 --- s4 --- mon, bg2
                    |
                    s3     (linked to s4 only with --redundant)

Run:  sudo python3 topo.py --agents 6 [--redundant] [--pingall]
"""
import argparse
from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.link import TCLink
from mininet.cli import CLI
from mininet.log import setLogLevel


def build(n_agents, redundant, ctrl_ip, ctrl_port):
    net = Mininet(controller=None, switch=OVSSwitch, link=TCLink,
                  autoSetMacs=True)
    net.addController('c0', controller=RemoteController,
                      ip=ctrl_ip, port=ctrl_port)

    s = {i: net.addSwitch('s%d' % i, protocols='OpenFlow13') for i in (1, 2, 3, 4)}

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
        net.addLink(s[3], s[4], bw=10, delay='5ms')   # creates a loop!
    return net


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--agents', type=int, default=6)
    ap.add_argument('--redundant', action='store_true')
    ap.add_argument('--ctrl-ip', default='127.0.0.1')
    ap.add_argument('--ctrl-port', type=int, default=6653)
    ap.add_argument('--pingall', action='store_true')
    a = ap.parse_args()

    setLogLevel('info')
    net = build(a.agents, a.redundant, a.ctrl_ip, a.ctrl_port)
    net.start()
    if a.pingall:
        net.pingAll()
    CLI(net)
    net.stop()

