#!/usr/bin/env python3
"""Make the D2 graphs from results/d2/*.csv  ->  results/graphs/*.png
Usage (from the repo root):  python3 results/make_graphs.py
"""
import csv
import os
import statistics as st

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = os.path.dirname(os.path.abspath(__file__))
D2 = os.path.join(HERE, "d2")
OUT = os.path.join(HERE, "graphs")
os.makedirs(OUT, exist_ok=True)

BLUE, ORANGE = "#2a78d6", "#eb6834"
INK, INK2, GRID, SURF = "#0b0b0b", "#52514e", "#e3e2de", "#fcfcfb"
MODE_COLOR = {"dynamic": BLUE, "baseline": ORANGE}
MODE_LABEL = {"dynamic": "Dynamic (priority queue + rerouting)",
              "baseline": "Baseline (static, no priority)"}

plt.rcParams.update({
    "font.size": 10, "axes.edgecolor": GRID, "axes.labelcolor": INK2,
    "xtick.color": INK2, "ytick.color": INK2, "text.color": INK,
    "figure.facecolor": SURF, "axes.facecolor": SURF, "savefig.facecolor": SURF,
})


def rows(name):
    with open(os.path.join(D2, name)) as f:
        return list(csv.DictReader(f))


def style(ax):
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def title(ax, text):
    ax.set_title(text, loc="left", color=INK, fontsize=11, pad=12)


def congestion():
    data = rows("congestion.csv")
    metrics = [("avg_ms", "Average"), ("p95_ms", "95th percentile"), ("max_ms", "Maximum")]
    fig, ax = plt.subplots(figsize=(7.2, 4.4))
    w, gap = 0.34, 0.04
    for j, mode in enumerate(("dynamic", "baseline")):
        vals = [st.mean(float(r[m]) for r in data if r["label"] == mode) for m, _ in metrics]
        xs = [i + (j - 0.5) * (w + gap) for i in range(len(metrics))]
        bars = ax.bar(xs, vals, w, color=MODE_COLOR[mode], label=MODE_LABEL[mode])
        ax.bar_label(bars, labels=[f"{v:.2f}" if v < 10 else f"{v:.0f}" for v in vals],
                     padding=2, fontsize=9, color=INK)
    ax.set_yscale("log")
    ax.set_ylim(0.05, 2000)
    ax.set_xticks(range(len(metrics)))
    ax.set_xticklabels([l for _, l in metrics])
    ax.set_ylabel("Alert latency (ms, log scale)")
    title(ax, "Alert latency under congestion (mean of 3 runs)")
    style(ax)
    ax.legend(frameon=False, loc="upper left", fontsize=9)
    fig.tight_layout()
    fig.savefig(os.path.join(OUT, "congestion_latency.png"), dpi=170)
    plt.close(fig)


def linkdown():
    data = rows("linkdown.csv")
    modes = ["dynamic", "baseline"]
    vals = [st.mean(float(r["outage_s"]) for r in data if r["label"] == m) for m in modes]
    fig, ax = plt.subplots(figsize=(5.4, 4.2))
    ax.bar([0, 1], [max(v, 0.04) for v in vals], 0.42, color=[MODE_COLOR[m] for m in modes])
    ax.set_xticks([0, 1])
    ax.set_xticklabels(["Dynamic\n(reroutes)", "Baseline\n(static path)"])
    ax.set_ylim(0, 12.5)
    ax.set_ylabel("Alert outage after link failure (s)")
    ax.text(0, 0.35, "under 1 s", ha="center", va="bottom", fontsize=10, color=INK)
    ax.text(1, vals[1] + 0.3, f"{vals[1]:.0f} s*", ha="center", va="bottom", fontsize=10, color=INK)
    title(ax, "Outage when the primary link goes down (3 runs each)")
    style(ax)
    fig.text(0.01, 0.01, "*Baseline recovers only when the test restores the link after 10 s.",
             fontsize=8, color=INK2)
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    fig.savefig(os.path.join(OUT, "linkdown_outage.png"), dpi=170)
    plt.close(fig)


def scalability():
    latest = {}
    for r in rows("scalability.csv"):
        latest[(int(r["agents"]), int(r["run"]))] = r
    data = list(latest.values())
    ns = sorted({int(r["agents"]) for r in data})
    fig, ax = plt.subplots(figsize=(6.6, 4.2))
    series = (("avg_ms", "Average", "-", "o"), ("p95_ms", "95th percentile", "--", "s"))
    color = BLUE
    for key, label, ls, mk in series:
        meds = []
        for n in ns:
            vals = [float(r[key]) for r in data if int(r["agents"]) == n]
            ax.scatter([n] * len(vals), vals, s=16, color=color, alpha=0.35, linewidths=0)
            meds.append(st.median(vals))
        ax.plot(ns, meds, color=color, linewidth=2, linestyle=ls, marker=mk, markersize=7,
                markeredgecolor=SURF, markeredgewidth=2, label=label)
    ax.set_xticks(ns)
    ax.set_xlabel("Number of agents")
    ax.set_ylabel("Alert latency (ms)")
    ax.set_ylim(bottom=0)
    title(ax, "Alert latency vs. number of agents (median of 3 runs; dots = single runs)")
    style(ax)
    ax.legend(frameon=False, loc="upper right", fontsize=9)
    fig.tight_layout()
    fig.savefig(os.path.join(OUT, "scalability_latency.png"), dpi=170)
    plt.close(fig)
    lost = sum(int(r["lost_packets"]) for r in data)
    downs = sum(int(r["down_events"]) for r in data)
    print(f"scalability: total lost packets = {lost}, DOWN events = {downs} across all runs")


if __name__ == "__main__":
    congestion()
    linkdown()
    scalability()
    print("graphs written to", OUT)
