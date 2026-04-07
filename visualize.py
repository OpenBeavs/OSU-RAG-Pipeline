#!/usr/bin/env python3
"""
OSU Knowledge Graph Visualizer
================================
Reads discovered_urls.json and opens an interactive D3 force graph in your browser.

Usage:
    python visualize.py                        # uses discovered_urls.json
    python visualize.py path/to/output.json    # custom input file
    python visualize.py --no-open              # generate graph.html without opening
"""
from __future__ import annotations

import json
import sys
import webbrowser
from collections import defaultdict
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
DEFAULT_INPUT = ROOT_DIR / "discovered_urls.json"
OUTPUT_HTML = ROOT_DIR / "graph.html"

# Cap pages embedded per domain so the HTML stays manageable on large crawls
MAX_PAGES_PER_DOMAIN = 200


# ──────────────────────────────────────────────────────────────────────────────
# Data builder
# ──────────────────────────────────────────────────────────────────────────────

def build_graph_data(raw: dict) -> dict:
    urls           = raw.get("urls", {})
    failed         = raw.get("failed", {})
    domain_summary = raw.get("domain_summary", {})
    domain_graph   = raw.get("domain_graph", {})
    meta           = raw.get("crawl_metadata", {})

    # ── Nodes ────────────────────────────────────────────────────────────────
    nodes = []
    for domain, stats in domain_summary.items():
        domain_pages = [v for v in urls.values() if v.get("domain") == domain]
        min_depth    = min((p.get("depth", 99) for p in domain_pages), default=0)
        thin_count   = sum(1 for p in domain_pages if p.get("thin"))
        fail_count   = sum(1 for v in failed.values() if v.get("domain") == domain)

        nodes.append({
            "id":          domain,
            "pages":       stats["pages"],
            "avg_latency": round(stats["avg_latency_ms"]),
            "redirected":  stats["redirected_count"],
            "thin":        thin_count,
            "failed":      fail_count,
            "min_depth":   min_depth,
        })

    # ── Edges ────────────────────────────────────────────────────────────────
    # Use domain_graph from the crawler when available.
    # Gracefully falls back to no edges for older discovered_urls.json files.
    known_domains: set[str] = {n["id"] for n in nodes}
    seen_edges: set[tuple[str, str]] = set()
    links = []

    for from_d, to_list in domain_graph.items():
        if from_d not in known_domains:
            continue
        for to_d in to_list:
            if to_d not in known_domains or to_d == from_d:
                continue
            key = (min(from_d, to_d), max(from_d, to_d))
            if key not in seen_edges:
                seen_edges.add(key)
                links.append({"source": from_d, "target": to_d})

    # ── Pages per domain (capped, sorted by word count) ───────────────────────
    pages_by_domain: dict[str, list] = defaultdict(list)
    for url, m in urls.items():
        d = m.get("domain", "")
        pages_by_domain[d].append({
            "url":     url,
            "title":   m.get("title") or url,
            "words":   m.get("word_count", 0),
            "depth":   m.get("depth", 0),
            "thin":    m.get("thin", False),
            "latency": m.get("latency_ms", 0),
        })

    for d in pages_by_domain:
        pages_by_domain[d].sort(key=lambda x: -x["words"])
        pages_by_domain[d] = pages_by_domain[d][:MAX_PAGES_PER_DOMAIN]

    return {
        "nodes": nodes,
        "links": links,
        "pages": dict(pages_by_domain),
        "meta": {
            "started_at":      meta.get("started_at", ""),
            "ended_at":        meta.get("ended_at", ""),
            "pages_crawled":   meta.get("summary", {}).get("pages_crawled", 0),
            "pages_failed":    meta.get("summary", {}).get("pages_failed", 0),
            "urls_discovered": meta.get("summary", {}).get("urls_discovered", 0),
        },
    }


# ──────────────────────────────────────────────────────────────────────────────
# HTML template  (D3 v7 loaded from CDN — requires internet connection)
# __DATA__ is replaced with the embedded JSON payload at generation time.
# ──────────────────────────────────────────────────────────────────────────────

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>OSU Knowledge Graph</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }

body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  background: #0d1117;
  color: #e6edf3;
  height: 100vh;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

/* ── Header ────────────────────────────────────────────────────────────── */
#header {
  background: #161b22;
  border-bottom: 1px solid #30363d;
  padding: 0 16px;
  height: 52px;
  display: flex;
  align-items: center;
  gap: 12px;
  flex-shrink: 0;
  z-index: 10;
}
#header h1 {
  font-size: 15px;
  font-weight: 600;
  color: #DC4405;
  white-space: nowrap;
  letter-spacing: -0.3px;
}
#search {
  flex: 1;
  max-width: 380px;
  background: #0d1117;
  border: 1px solid #30363d;
  border-radius: 6px;
  padding: 6px 12px;
  color: #e6edf3;
  font-size: 13px;
  outline: none;
}
#search::placeholder { color: #6e7681; }
#search:focus { border-color: #DC4405; }
.stat-pill {
  background: #21262d;
  border: 1px solid #30363d;
  border-radius: 20px;
  padding: 3px 10px;
  font-size: 12px;
  color: #8b949e;
  white-space: nowrap;
}
.stat-pill b { color: #e6edf3; font-weight: 600; }

/* ── Body layout ───────────────────────────────────────────────────────── */
#body { display: flex; flex: 1; overflow: hidden; }

/* ── Left panel ────────────────────────────────────────────────────────── */
#left-panel {
  width: 230px;
  background: #161b22;
  border-right: 1px solid #30363d;
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
  overflow: hidden;
}
#left-panel-header {
  padding: 10px 14px 8px;
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  color: #8b949e;
  border-bottom: 1px solid #30363d;
  flex-shrink: 0;
}
#domain-list {
  overflow-y: auto;
  flex: 1;
}
.domain-item {
  display: flex;
  align-items: center;
  padding: 6px 14px;
  cursor: pointer;
  gap: 8px;
  border-bottom: 1px solid #21262d;
  transition: background 0.1s;
}
.domain-item:hover { background: #21262d; }
.domain-item.active { background: #1c2a1c; }
.domain-item.active .d-name { color: #DC4405; }
.domain-item.dim { opacity: 0.25; }
.d-dot {
  width: 8px; height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
}
.d-name {
  font-size: 12px;
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: #c9d1d9;
}
.d-count {
  font-size: 11px;
  color: #6e7681;
  flex-shrink: 0;
}

/* ── Graph area ────────────────────────────────────────────────────────── */
#graph-wrap {
  flex: 1;
  position: relative;
  overflow: hidden;
  background: #0d1117;
}
#graph { width: 100%; height: 100%; }

.link { stroke: #30363d; stroke-opacity: 0.7; fill: none; }
.link.hi { stroke: #DC4405; stroke-opacity: 1; }

.node { cursor: pointer; }
.node circle {
  stroke: rgba(255,255,255,0.15);
  stroke-width: 1.5;
  transition: stroke 0.15s, stroke-width 0.15s;
}
.node:hover circle { stroke: #fff; stroke-width: 2.5; }
.node.selected circle { stroke: #DC4405; stroke-width: 3; }
.node.dim { opacity: 0.1; pointer-events: none; }

.node-label {
  font-size: 10px;
  fill: #8b949e;
  pointer-events: none;
  dominant-baseline: hanging;
  text-anchor: middle;
  transition: fill 0.15s;
}
.node.selected .node-label,
.node:hover .node-label { fill: #e6edf3; }

/* ── Tooltip ───────────────────────────────────────────────────────────── */
#tt {
  position: absolute;
  background: #1c2128;
  border: 1px solid #30363d;
  border-radius: 8px;
  padding: 10px 13px;
  font-size: 12px;
  pointer-events: none;
  opacity: 0;
  transition: opacity 0.12s;
  z-index: 50;
  min-width: 180px;
  line-height: 1.6;
}
#tt.on { opacity: 1; }
#tt .tt-h { font-weight: 600; color: #e6edf3; margin-bottom: 4px; word-break: break-all; }
#tt .tt-r { color: #8b949e; }
#tt .tt-r b { color: #c9d1d9; }

/* ── Zoom controls ─────────────────────────────────────────────────────── */
#zoom-ctrl {
  position: absolute;
  bottom: 16px;
  right: 16px;
  display: flex;
  flex-direction: column;
  gap: 5px;
}
.zb {
  width: 30px; height: 30px;
  background: #161b22;
  border: 1px solid #30363d;
  border-radius: 6px;
  color: #c9d1d9;
  font-size: 15px;
  cursor: pointer;
  display: flex; align-items: center; justify-content: center;
}
.zb:hover { background: #21262d; }

/* ── Empty-state hint ──────────────────────────────────────────────────── */
#hint {
  position: absolute;
  bottom: 16px;
  left: 50%;
  transform: translateX(-50%);
  background: #161b22;
  border: 1px solid #30363d;
  border-radius: 20px;
  padding: 5px 14px;
  font-size: 12px;
  color: #6e7681;
  pointer-events: none;
  white-space: nowrap;
}

/* ── Right detail panel ────────────────────────────────────────────────── */
#detail {
  width: 320px;
  background: #161b22;
  border-left: 1px solid #30363d;
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
  transform: translateX(100%);
  transition: transform 0.22s ease;
}
#detail.open { transform: translateX(0); }

#detail-head {
  padding: 11px 14px;
  border-bottom: 1px solid #30363d;
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}
#detail-title {
  font-size: 13px;
  font-weight: 600;
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
#detail-close {
  background: none; border: none; color: #6e7681;
  font-size: 18px; cursor: pointer; padding: 0 4px; border-radius: 4px;
  line-height: 1;
}
#detail-close:hover { background: #21262d; color: #e6edf3; }

#detail-stats {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 7px;
  padding: 12px 14px;
  border-bottom: 1px solid #30363d;
  flex-shrink: 0;
}
.sc {
  background: #0d1117;
  border: 1px solid #30363d;
  border-radius: 6px;
  padding: 7px 9px;
}
.sc-label { font-size: 10px; color: #6e7681; text-transform: uppercase; letter-spacing: 0.3px; }
.sc-val { font-size: 19px; font-weight: 600; color: #e6edf3; margin-top: 1px; }

#pages-head {
  padding: 8px 14px 4px;
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  color: #6e7681;
  flex-shrink: 0;
}
#pages-list { flex: 1; overflow-y: auto; }

.pg {
  padding: 8px 14px;
  border-bottom: 1px solid #21262d;
  cursor: default;
}
.pg:hover { background: #21262d; }
.pg.thin-pg { opacity: 0.45; }
.pg-title {
  font-size: 12px;
  color: #c9d1d9;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  margin-bottom: 2px;
}
.pg-meta { font-size: 11px; color: #6e7681; display: flex; gap: 8px; margin-bottom: 2px; }
.pg-url {
  font-size: 10px;
  color: #388bfd;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  display: block;
  text-decoration: none;
}
.pg-url:hover { text-decoration: underline; }
.badge {
  display: inline-block;
  font-size: 10px;
  padding: 1px 5px;
  border-radius: 10px;
  background: #30363d;
  color: #8b949e;
}
.badge.thin-b { background: #3d1a1a; color: #f85149; }

/* ── Scrollbars ────────────────────────────────────────────────────────── */
::-webkit-scrollbar { width: 5px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: #30363d; border-radius: 3px; }
::-webkit-scrollbar-thumb:hover { background: #484f58; }
</style>
</head>
<body>

<div id="header">
  <h1>&#127754; OSU Knowledge Graph</h1>
  <input id="search" type="text" placeholder="Search domains or pages…" autocomplete="off">
  <div class="stat-pill">Pages <b id="s-pages">…</b></div>
  <div class="stat-pill">Domains <b id="s-domains">…</b></div>
  <div class="stat-pill">Links <b id="s-links">…</b></div>
  <div class="stat-pill">Failed <b id="s-failed">…</b></div>
</div>

<div id="body">
  <div id="left-panel">
    <div id="left-panel-header">Domains</div>
    <div id="domain-list"></div>
  </div>

  <div id="graph-wrap">
    <svg id="graph"></svg>
    <div id="tt"></div>
    <div id="hint">Click a node to explore its pages</div>
    <div id="zoom-ctrl">
      <button class="zb" id="zin" title="Zoom in">+</button>
      <button class="zb" id="zout" title="Zoom out">&#8722;</button>
      <button class="zb" id="zfit" title="Fit all" title="Reset view">&#8859;</button>
    </div>
  </div>

  <div id="detail">
    <div id="detail-head">
      <div id="detail-title">Domain</div>
      <button id="detail-close">&#215;</button>
    </div>
    <div id="detail-stats"></div>
    <div id="pages-head">Pages</div>
    <div id="pages-list"></div>
  </div>
</div>

<script src="https://d3js.org/d3.v7.min.js"></script>
<script>
// ── Embedded data ──────────────────────────────────────────────────────────
const DATA = __DATA__;

// ── Color palette (OSU orange first, then distinct hues) ───────────────────
const PALETTE = [
  "#DC4405","#4A9EFF","#2EA043","#D2A808","#A371F7",
  "#E85C0D","#1BB7A6","#FF6B8B","#5CA8FF","#87C95F",
  "#F0883E","#3D9FFF","#56D364","#E3B341","#BC8CFF",
  "#FF8C69","#00BCD4","#FFB347","#9C88FF","#66BB6A",
];
const colorOf = (() => {
  const map = {};
  DATA.nodes.forEach((n, i) => { map[n.id] = PALETTE[i % PALETTE.length]; });
  return d => map[d] || "#888";
})();

// ── Populate header stats ──────────────────────────────────────────────────
document.getElementById("s-pages").textContent   = DATA.meta.pages_crawled.toLocaleString();
document.getElementById("s-domains").textContent = DATA.nodes.length;
document.getElementById("s-links").textContent   = DATA.links.length;
document.getElementById("s-failed").textContent  = DATA.meta.pages_failed.toLocaleString();

// ── State ──────────────────────────────────────────────────────────────────
let selected = null;
let query    = "";

// ── Left panel ─────────────────────────────────────────────────────────────
function renderList() {
  const q = query.toLowerCase();
  const sorted = [...DATA.nodes].sort((a, b) => b.pages - a.pages);
  const html = sorted.map(n => {
    const name = n.id.replace(/\.oregonstate\.edu$/, "");
    const match = !q || n.id.includes(q) || name.includes(q);
    return `<div class="domain-item${selected===n.id?' active':''}${!match&&q?' dim':''}"
                 data-d="${n.id}" onclick="pick('${n.id}')">
      <div class="d-dot" style="background:${colorOf(n.id)}"></div>
      <div class="d-name" title="${n.id}">${name}</div>
      <div class="d-count">${n.pages}</div>
    </div>`;
  }).join("");
  document.getElementById("domain-list").innerHTML = html;
}
renderList();

// ── SVG / D3 setup ─────────────────────────────────────────────────────────
const wrap = document.getElementById("graph-wrap");
let W = wrap.clientWidth, H = wrap.clientHeight;

const svg = d3.select("#graph").attr("viewBox", [0,0,W,H]);
const g   = svg.append("g");

const zoom = d3.zoom().scaleExtent([0.04, 15])
  .on("zoom", e => g.attr("transform", e.transform));
svg.call(zoom);

// ── Node sizing ────────────────────────────────────────────────────────────
const maxP  = d3.max(DATA.nodes, d => d.pages) || 1;
const rScale = d3.scaleSqrt().domain([1, maxP]).range([6, 38]);
const r = d => rScale(Math.max(d.pages, 1));

// ── Clone data for D3 mutation ─────────────────────────────────────────────
const nodes = DATA.nodes.map(d => ({...d}));
const links = DATA.links.map(d => ({...d}));

// ── Force simulation ───────────────────────────────────────────────────────
const sim = d3.forceSimulation(nodes)
  .force("link", d3.forceLink(links).id(d => d.id)
    .distance(d => 60 + r(d.source) + r(d.target))
    .strength(0.25))
  .force("charge", d3.forceManyBody().strength(d => -Math.max(180, r(d) * 18)))
  .force("center", d3.forceCenter(W/2, H/2).strength(0.04))
  .force("collide", d3.forceCollide().radius(d => r(d) + 6).iterations(2));

// ── Draw links ─────────────────────────────────────────────────────────────
const linkSel = g.append("g")
  .selectAll("line").data(links).join("line").attr("class", "link");

// ── Draw node groups ───────────────────────────────────────────────────────
const nodeSel = g.append("g")
  .selectAll(".node").data(nodes).join("g")
  .attr("class", "node")
  .call(d3.drag()
    .on("start", (e, d) => { if (!e.active) sim.alphaTarget(0.3).restart(); d.fx=d.x; d.fy=d.y; })
    .on("drag",  (e, d) => { d.fx=e.x; d.fy=e.y; })
    .on("end",   (e, d) => { if (!e.active) sim.alphaTarget(0); d.fx=null; d.fy=null; }))
  .on("click",     (e, d) => { e.stopPropagation(); pick(d.id); })
  .on("mouseover", (e, d) => showTT(e, d))
  .on("mousemove", moveT)
  .on("mouseout",  hideTT);

nodeSel.append("circle")
  .attr("r", r)
  .attr("fill", d => colorOf(d.id));

nodeSel.append("text")
  .attr("class", "node-label")
  .attr("dy", d => r(d) + 4)
  .text(d => d.id.replace(/\.oregonstate\.edu$/, "").replace("oregonstate.edu", "oregonstate"));

// Click background → deselect
svg.on("click", () => { selected = null; refresh(); closeDetail(); });

// ── Tick ───────────────────────────────────────────────────────────────────
sim.on("tick", () => {
  linkSel.attr("x1", d=>d.source.x).attr("y1", d=>d.source.y)
         .attr("x2", d=>d.target.x).attr("y2", d=>d.target.y);
  nodeSel.attr("transform", d => `translate(${d.x},${d.y})`);
});

// ── Visual refresh after selection/search ──────────────────────────────────
function refresh() {
  if (selected) {
    const nbrs = new Set([selected]);
    links.forEach(l => {
      const s = l.source.id ?? l.source;
      const t = l.target.id ?? l.target;
      if (s === selected) nbrs.add(t);
      if (t === selected) nbrs.add(s);
    });
    nodeSel.classed("selected", d => d.id === selected)
           .classed("dim", d => !nbrs.has(d.id));
    linkSel.classed("hi", l => {
      const s = l.source.id??l.source, t = l.target.id??l.target;
      return s===selected || t===selected;
    }).classed("link", true);
  } else if (query) {
    const q = query.toLowerCase();
    nodeSel.classed("selected", false)
           .classed("dim", d => !d.id.includes(q));
    linkSel.classed("hi", false);
  } else {
    nodeSel.classed("selected", false).classed("dim", false);
    linkSel.classed("hi", false);
  }
  renderList();
}

// ── Domain pick ────────────────────────────────────────────────────────────
function pick(domain) {
  if (selected === domain) { selected = null; refresh(); closeDetail(); return; }
  selected = domain;
  refresh();
  openDetail(domain);

  // Pan/zoom to node
  const nd = nodes.find(n => n.id === domain);
  if (nd && nd.x != null) {
    const scale = 1.8;
    svg.transition().duration(500).call(
      zoom.transform,
      d3.zoomIdentity.translate(W/2 - nd.x*scale, H/2 - nd.y*scale).scale(scale)
    );
  }
}

// ── Tooltip ────────────────────────────────────────────────────────────────
const tt = document.getElementById("tt");

function showTT(event, d) {
  const pct = d.pages > 0 ? Math.round(d.thin / d.pages * 100) : 0;
  tt.innerHTML = `
    <div class="tt-h">${d.id}</div>
    <div class="tt-r">Pages: <b>${d.pages}</b></div>
    <div class="tt-r">Avg latency: <b>${d.avg_latency} ms</b></div>
    <div class="tt-r">Thin: <b>${d.thin} (${pct}%)</b></div>
    <div class="tt-r">Failed: <b>${d.failed}</b></div>
    <div class="tt-r">Min depth: <b>${d.min_depth}</b></div>`;
  tt.classList.add("on");
  moveT(event);
}
function moveT(event) {
  const rect = wrap.getBoundingClientRect();
  let x = event.clientX - rect.left + 14;
  let y = event.clientY - rect.top  - 10;
  if (x + 210 > W) x -= 225;
  tt.style.left = x + "px";
  tt.style.top  = y + "px";
}
function hideTT() { tt.classList.remove("on"); }

// ── Detail panel ───────────────────────────────────────────────────────────
function openDetail(domain) {
  const nd    = DATA.nodes.find(n => n.id === domain);
  const pages = DATA.pages[domain] || [];

  document.getElementById("detail-title").textContent = domain;

  const thin_pct = nd.pages > 0 ? Math.round(nd.thin/nd.pages*100) : 0;
  document.getElementById("detail-stats").innerHTML = `
    <div class="sc">
      <div class="sc-label">Pages</div>
      <div class="sc-val" style="color:${colorOf(domain)}">${nd.pages}</div>
    </div>
    <div class="sc">
      <div class="sc-label">Avg latency</div>
      <div class="sc-val">${nd.avg_latency}<small style="font-size:11px;color:#6e7681"> ms</small></div>
    </div>
    <div class="sc">
      <div class="sc-label">Thin pages</div>
      <div class="sc-val">${nd.thin}<small style="font-size:11px;color:#6e7681"> (${thin_pct}%)</small></div>
    </div>
    <div class="sc">
      <div class="sc-label">Failed</div>
      <div class="sc-val" style="color:${nd.failed>0?'#f85149':'#e6edf3'}">${nd.failed}</div>
    </div>`;

  const listEl = document.getElementById("pages-list");
  if (!pages.length) {
    listEl.innerHTML = `<div style="padding:20px;text-align:center;color:#6e7681;font-size:13px">No cached pages</div>`;
  } else {
    listEl.innerHTML = pages.map(p => `
      <div class="pg${p.thin?' thin-pg':''}">
        <div class="pg-title" title="${p.title}">${p.title || "(no title)"}</div>
        <div class="pg-meta">
          <span>depth ${p.depth}</span>
          <span>${p.words.toLocaleString()} words</span>
          <span>${p.latency} ms</span>
          ${p.thin ? '<span class="badge thin-b">thin</span>' : ''}
        </div>
        <a class="pg-url" href="${p.url}" target="_blank" rel="noopener noreferrer">${p.url}</a>
      </div>`).join("");
  }

  document.getElementById("detail").classList.add("open");
  document.getElementById("hint").style.display = "none";
}

function closeDetail() {
  document.getElementById("detail").classList.remove("open");
  document.getElementById("hint").style.display = "";
}

document.getElementById("detail-close").onclick = () => {
  selected = null;
  refresh();
  closeDetail();
};

// ── Search ─────────────────────────────────────────────────────────────────
document.getElementById("search").addEventListener("input", function() {
  query = this.value.trim();
  refresh();
});

// ── Zoom controls ──────────────────────────────────────────────────────────
document.getElementById("zin").onclick  = () => svg.transition().duration(280).call(zoom.scaleBy, 1.5);
document.getElementById("zout").onclick = () => svg.transition().duration(280).call(zoom.scaleBy, 0.67);
document.getElementById("zfit").onclick = () => {
  svg.transition().duration(450).call(zoom.transform, d3.zoomIdentity.translate(W/2, H/2).scale(0.85));
};

// ── Resize ─────────────────────────────────────────────────────────────────
window.addEventListener("resize", () => {
  W = wrap.clientWidth; H = wrap.clientHeight;
  svg.attr("viewBox", [0,0,W,H]);
  sim.force("center", d3.forceCenter(W/2, H/2).strength(0.04)).alpha(0.1).restart();
});
</script>
</body>
</html>"""


# ──────────────────────────────────────────────────────────────────────────────
# Generator
# ──────────────────────────────────────────────────────────────────────────────

def generate(input_path: Path = DEFAULT_INPUT, open_browser: bool = True) -> None:
    if not input_path.exists():
        print(f"Error: {input_path} not found. Run crawler.py first.")
        sys.exit(1)

    print(f"Reading {input_path}…")
    raw = json.loads(input_path.read_text(encoding="utf-8"))

    graph_data = build_graph_data(raw)
    n_nodes = len(graph_data["nodes"])
    n_links = len(graph_data["links"])
    n_pages = sum(len(v) for v in graph_data["pages"].values())

    print(f"  {n_nodes} domains | {n_links} inter-domain edges | {n_pages} pages embedded")

    if n_links == 0:
        print(
            "  Note: no edges found — re-run crawler.py to populate domain_graph,\n"
            "  or the crawl used an older version of crawler.py."
        )

    html = HTML_TEMPLATE.replace("__DATA__", json.dumps(graph_data, separators=(",", ":")))

    OUTPUT_HTML.write_text(html, encoding="utf-8")
    size_kb = OUTPUT_HTML.stat().st_size // 1024
    print(f"Wrote {OUTPUT_HTML}  ({size_kb} KB)")

    if open_browser:
        webbrowser.open(OUTPUT_HTML.as_uri())
        print("Opened in browser.")


if __name__ == "__main__":
    args = sys.argv[1:]
    no_open = "--no-open" in args
    args = [a for a in args if a != "--no-open"]
    input_file = Path(args[0]) if args else DEFAULT_INPUT
    generate(input_file, open_browser=not no_open)
