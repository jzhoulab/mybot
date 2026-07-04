from __future__ import annotations

import glob
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sources.access import default_config_path

if TYPE_CHECKING:  # pragma: no cover
    from app.server import AppState


GUI_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>mybot</title>
  <style>
    :root {
      --bg: #f4f6f9;
      --panel: #ffffff;
      --panel-2: #fbfcfe;
      --text: #172033;
      --muted: #64748b;
      --line: #e2e8f0;
      --line-strong: #cbd5e1;
      --good: #157347;
      --good-bg: #e7f5ec;
      --warn: #9a5b00;
      --warn-bg: #fdf2e0;
      --bad: #b42318;
      --bad-bg: #fdeceb;
      --accent: #3257d1;
      --accent-bg: #eaf0ff;
      --chip: #eef2f7;
      --shadow: 0 1px 2px rgba(15,23,42,.06), 0 1px 3px rgba(15,23,42,.04);
    }
    :root[data-theme="dark"] {
      --bg: #0e131c;
      --panel: #161d2b;
      --panel-2: #1b2434;
      --text: #e6ecf5;
      --muted: #93a1b5;
      --line: #26303f;
      --line-strong: #33415a;
      --good: #4ade80;
      --good-bg: #123122;
      --warn: #f0b360;
      --warn-bg: #33260f;
      --bad: #f87171;
      --bad-bg: #3a1a1a;
      --accent: #7aa2ff;
      --accent-bg: #1a2540;
      --chip: #212c3d;
      --shadow: 0 1px 2px rgba(0,0,0,.3);
    }
    @media (prefers-color-scheme: dark) {
      :root:not([data-theme="light"]) {
        --bg: #0e131c; --panel: #161d2b; --panel-2: #1b2434; --text: #e6ecf5;
        --muted: #93a1b5; --line: #26303f; --line-strong: #33415a;
        --good: #4ade80; --good-bg: #123122; --warn: #f0b360; --warn-bg: #33260f;
        --bad: #f87171; --bad-bg: #3a1a1a; --accent: #7aa2ff; --accent-bg: #1a2540;
        --chip: #212c3d; --shadow: 0 1px 2px rgba(0,0,0,.3);
      }
    }
    * { box-sizing: border-box; }
    body {
      margin: 0; background: var(--bg); color: var(--text);
      font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      -webkit-font-smoothing: antialiased;
    }
    header {
      border-bottom: 1px solid var(--line); background: var(--panel);
      padding: 14px 22px; position: sticky; top: 0; z-index: 5;
      display: flex; align-items: center; justify-content: space-between; gap: 16px;
    }
    .brand { display: flex; align-items: baseline; gap: 10px; }
    .brand h1 { margin: 0; font-size: 17px; font-weight: 700; letter-spacing: -0.2px; }
    .brand .sub { color: var(--muted); font-size: 12px; }
    .head-right { display: flex; align-items: center; gap: 12px; }
    .meta { color: var(--muted); font-size: 12px; white-space: nowrap; }
    .icon-btn {
      border: 1px solid var(--line-strong); background: var(--panel-2); color: var(--text);
      border-radius: 8px; padding: 6px 10px; cursor: pointer; font-size: 13px;
    }
    .icon-btn:hover { border-color: var(--accent); }
    main { max-width: 1220px; margin: 0 auto; padding: 18px 20px 60px; }
    .grid { display: grid; grid-template-columns: repeat(12, 1fr); gap: 14px; }
    section {
      background: var(--panel); border: 1px solid var(--line); border-radius: 12px;
      padding: 16px; min-width: 0; box-shadow: var(--shadow);
    }
    section h2 {
      margin: 0 0 12px; font-size: 12px; font-weight: 700; letter-spacing: .04em;
      text-transform: uppercase; color: var(--muted);
    }
    .span-3 { grid-column: span 3; }
    .span-4 { grid-column: span 4; }
    .span-5 { grid-column: span 5; }
    .span-6 { grid-column: span 6; }
    .span-7 { grid-column: span 7; }
    .span-8 { grid-column: span 8; }
    .span-12 { grid-column: span 12; }

    /* Health banner */
    .banner { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
    .health {
      display: flex; align-items: center; gap: 14px; padding: 16px; border-radius: 12px;
      border: 1px solid var(--line); background: var(--panel); box-shadow: var(--shadow);
    }
    .health .dot { width: 12px; height: 12px; border-radius: 50%; flex: none; }
    .health .body { min-width: 0; }
    .health .title { font-weight: 650; font-size: 14px; }
    .health .desc { color: var(--muted); font-size: 12px; margin-top: 2px; }
    .health.good { background: var(--good-bg); border-color: transparent; }
    .health.good .dot { background: var(--good); }
    .health.good .title { color: var(--good); }
    .health.warn { background: var(--warn-bg); border-color: transparent; }
    .health.warn .dot { background: var(--warn); }
    .health.warn .title { color: var(--warn); }
    .health.bad { background: var(--bad-bg); border-color: transparent; }
    .health.bad .dot { background: var(--bad); }
    .health.bad .title { color: var(--bad); }

    /* stat tiles */
    .stats { display: grid; grid-template-columns: repeat(3, minmax(0,1fr)); gap: 10px; }
    .stat { border: 1px solid var(--line); border-radius: 10px; padding: 12px; background: var(--panel-2); }
    .stat .value { font-size: 24px; font-weight: 700; letter-spacing: -0.5px; }
    .stat .label { color: var(--muted); font-size: 12px; margin-top: 2px; }
    .bar { height: 6px; border-radius: 999px; background: var(--line); overflow: hidden; margin-top: 8px; }
    .bar > span { display: block; height: 100%; background: var(--accent); }

    /* toolbar */
    .toolbar { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
    .btn {
      border: 1px solid var(--line-strong); background: var(--panel-2); color: var(--text);
      border-radius: 8px; padding: 8px 14px; cursor: pointer; font-size: 13px; font-weight: 550;
    }
    .btn:hover:not(:disabled) { border-color: var(--accent); color: var(--accent); }
    .btn.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
    .btn.primary:hover:not(:disabled) { filter: brightness(1.05); color: #fff; }
    .btn:disabled { opacity: .5; cursor: not-allowed; }
    .job-status { color: var(--muted); font-size: 12px; }
    .job-status.running { color: var(--accent); }
    .job-status.err { color: var(--bad); }

    /* query */
    .query-row { display: flex; gap: 8px; }
    .query-row input[type=text] {
      flex: 1; border: 1px solid var(--line-strong); background: var(--panel-2); color: var(--text);
      border-radius: 8px; padding: 9px 12px; font-size: 14px;
    }
    .query-row input[type=text]:focus { outline: none; border-color: var(--accent); }
    .query-row select {
      border: 1px solid var(--line-strong); background: var(--panel-2); color: var(--text);
      border-radius: 8px; padding: 0 10px; font-size: 13px;
    }
    .results { display: grid; gap: 10px; margin-top: 12px; }
    .result {
      border: 1px solid var(--line); border-radius: 10px; padding: 12px; background: var(--panel-2);
    }
    .result .r-top { display: flex; align-items: center; gap: 8px; justify-content: space-between; }
    .result .r-title { font-weight: 600; font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .result .r-score {
      flex: none; font-family: ui-monospace, Menlo, monospace; font-size: 12px; font-weight: 700;
      color: var(--accent); background: var(--accent-bg); border-radius: 6px; padding: 2px 8px;
    }
    .result .r-meta { color: var(--muted); font-size: 11.5px; margin-top: 4px; display: flex; gap: 12px; flex-wrap: wrap; }
    .result .r-snippet {
      margin-top: 8px; font-size: 12.5px; color: var(--text); background: var(--panel);
      border: 1px solid var(--line); border-radius: 8px; padding: 8px 10px; max-height: 120px; overflow: auto;
      white-space: pre-wrap; word-break: break-word;
    }
    .pill { border-radius: 999px; padding: 1px 8px; font-size: 11px; font-weight: 600; background: var(--chip); }

    table { width: 100%; border-collapse: collapse; }
    th, td { border-top: 1px solid var(--line); padding: 8px 6px; text-align: left; vertical-align: top; }
    thead th { border-top: 0; color: var(--muted); font-size: 11px; font-weight: 700; text-transform: uppercase; }
    .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; }
    .truncate { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 360px; }
    .num { text-align: right; font-variant-numeric: tabular-nums; }
    .chips { display: flex; gap: 6px; flex-wrap: wrap; }
    .chip { background: var(--chip); border-radius: 999px; padding: 3px 9px; color: var(--muted); font-size: 12px; }
    .list { display: grid; gap: 8px; }
    .row { border-top: 1px solid var(--line); padding-top: 8px; }
    .row:first-child { border-top: 0; padding-top: 0; }
    .kv { display: flex; justify-content: space-between; gap: 12px; }
    .kv .label { color: var(--muted); font-size: 12px; }
    .empty { color: var(--muted); border: 1px dashed var(--line-strong); border-radius: 8px; padding: 14px; text-align: center; }
    .scroll { overflow-x: auto; }
    @media (max-width: 900px) {
      .banner { grid-template-columns: 1fr; }
      .span-3,.span-4,.span-5,.span-6,.span-7,.span-8 { grid-column: span 12; }
      .stats { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <header>
    <div class="brand">
      <h1>mybot</h1>
      <span class="sub">memory control room — scope · freshness · footprint</span>
    </div>
    <div class="head-right">
      <span class="meta" id="generated">Loading...</span>
      <button class="icon-btn" id="theme-toggle" title="Toggle theme">Theme</button>
    </div>
  </header>
  <main>
    <div class="grid">
      <section class="span-12" style="padding:0;border:0;background:transparent;box-shadow:none">
        <div class="banner">
          <div class="health" id="health-index">
            <div class="dot"></div>
            <div class="body"><div class="title">Index</div><div class="desc">-</div></div>
          </div>
          <div class="health" id="health-semantic">
            <div class="dot"></div>
            <div class="body"><div class="title">Semantic search</div><div class="desc">-</div></div>
          </div>
        </div>
      </section>

      <section class="span-12" id="scope-section">
        <h2>Scope — what the assistant can see</h2>
        <div class="job-status" id="scope-toast" style="margin-bottom:10px">manage inclusion &amp; exclusion; excluding purges from the index</div>
        <div id="scope-new"></div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:18px" class="scope-cols">
          <div style="min-width:0">
            <div style="font-weight:600;font-size:13px;margin-bottom:8px">Included projects <span class="muted" id="scope-included-count"></span></div>
            <div class="scroll" id="scope-included" style="max-height:340px;overflow:auto"></div>
          </div>
          <div style="min-width:0">
            <div style="font-weight:600;font-size:13px;margin-bottom:8px">Exclusions</div>
            <div id="scope-excluded"></div>
          </div>
        </div>
      </section>

      <section class="span-4">
        <h2>Pool &amp; maintenance</h2>
        <div class="stats">
          <div class="stat"><div class="value" id="sessions">-</div><div class="label">sessions</div></div>
          <div class="stat"><div class="value" id="chunks">-</div><div class="label">chunks</div></div>
          <div class="stat">
            <div class="value" id="embedded">-</div><div class="label">embedded</div>
            <div class="bar"><span id="embedded-bar" style="width:0%"></span></div>
          </div>
        </div>
        <div class="toolbar" style="margin-top:14px">
          <button class="btn" data-action="refresh">Refresh changed</button>
          <button class="btn" data-action="embed">Backfill embeddings</button>
          <button class="btn" data-action="rebuild">Rebuild index</button>
          <button class="btn" data-action="compact_embeddings">Shrink embeddings</button>
          <button class="btn" data-action="compact">Reclaim space</button>
        </div>
        <div class="job-status" id="job-status" style="margin-top:10px">idle</div>
      </section>

      <section class="span-4">
        <h2>Footprint</h2>
        <div id="footprint"></div>
      </section>

      <section class="span-4">
        <h2>Warnings</h2>
        <div id="warnings"></div>
      </section>

      <section class="span-6">
        <h2>Largest sessions</h2>
        <div class="scroll" id="largest"></div>
      </section>
      <section class="span-6">
        <h2>Recently added</h2>
        <div class="scroll" id="recent"></div>
      </section>

      <details class="span-12" style="border:1px solid var(--line);border-radius:12px;background:var(--panel);box-shadow:var(--shadow)">
        <summary style="cursor:pointer;padding:14px 16px;font-weight:700;font-size:12px;letter-spacing:.04em;text-transform:uppercase;color:var(--muted)">Diagnostics</summary>
        <div style="padding:0 16px 16px">
          <h2 style="margin-top:6px">Retrieval probe</h2>
          <div class="query-row">
            <input type="text" id="q" placeholder="e.g. current cluster SU allocation status" autocomplete="off">
            <select id="q-mode">
              <option value="hybrid">hybrid</option>
              <option value="exact">exact / lexical</option>
              <option value="vector">vector / semantic</option>
            </select>
            <button class="btn primary" id="q-run">Search</button>
          </div>
          <div class="results" id="results"></div>
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:16px" class="scope-cols">
            <div style="min-width:0"><h2>Recent retrievals</h2><div class="scroll" id="retrievals"></div></div>
            <div style="min-width:0"><h2>Runtime</h2><div class="list" id="runtime"></div></div>
          </div>
          <h2 style="margin-top:16px">Visibility policy (raw)</h2>
          <div class="scroll" id="policy"></div>
        </div>
      </details>
    </div>
  </main>
  <script>
    const text = (v) => v === null || v === undefined || v === "" ? "-" : String(v);
    const number = (v) => Number(v || 0).toLocaleString();
    const el = (id) => document.getElementById(id);
    const set = (id, v) => { el(id).textContent = v; };

    function ago(iso) {
      if (!iso) return "unknown";
      const t = Date.parse(iso);
      if (isNaN(t)) return "unknown";
      let s = Math.max(0, (Date.now() - t) / 1000);
      if (s < 90) return Math.round(s) + "s ago";
      if (s < 5400) return Math.round(s / 60) + "m ago";
      if (s < 129600) return Math.round(s / 3600) + "h ago";
      return Math.round(s / 86400) + "d ago";
    }

    /* theme */
    function applyTheme(mode) {
      if (mode === "system") document.documentElement.removeAttribute("data-theme");
      else document.documentElement.setAttribute("data-theme", mode);
    }
    let themeMode = localStorage.getItem("mybot-theme") || "system";
    applyTheme(themeMode);
    el("theme-toggle").onclick = () => {
      themeMode = themeMode === "dark" ? "light" : themeMode === "light" ? "system" : "dark";
      localStorage.setItem("mybot-theme", themeMode);
      applyTheme(themeMode);
      el("theme-toggle").textContent = "Theme: " + themeMode;
    };
    el("theme-toggle").textContent = "Theme: " + themeMode;

    function healthCard(id, cls, title, desc) {
      const node = el(id);
      node.className = "health " + cls;
      node.querySelector(".title").textContent = title;
      node.querySelector(".desc").textContent = desc;
    }

    function renderHealth(data) {
      const fresh = data.trajectory_index_freshness || {};
      const stale = !!fresh.stale;
      const indexedAge = fresh.indexed_at ? ago(fresh.indexed_at) : "never";
      const srcAge = fresh.latest_source_file_mtime ? ago(fresh.latest_source_file_mtime) : "n/a";
      const rh = data.refresh_health || {};
      let idxDesc = `indexed ${indexedAge} · newest ${srcAge} · auto-refresh every ${rh.cadence_seconds || "?"}s`;
      let idxCls = stale ? "warn" : "good", idxTitle = stale ? "Index stale" : "Index fresh";
      if (rh.error) { idxCls = "bad"; idxTitle = "Refresh failing"; idxDesc += ` · ERROR: ${(rh.error.error || "").slice(0, 80)}`; }
      healthCard("health-index", idxCls, idxTitle, idxDesc);
      const sem = data.semantic_search || {};
      const pct = Math.round((sem.coverage || 0) * 100);
      let cls = "good", title = "Semantic search on";
      if (!sem.total_chunks) { cls = "warn"; title = "No index"; }
      else if (sem.embedded_chunks === 0) { cls = "bad"; title = "Semantic search OFF"; }
      else if (!sem.enabled) { cls = "warn"; title = "Semantic search partial"; }
      healthCard(
        "health-semantic",
        cls,
        title,
        `${number(sem.embedded_chunks)} / ${number(sem.total_chunks)} chunks embedded (${pct}%)` +
        (sem.embedded_chunks === 0 ? " · hybrid queries are lexical-only" : "")
      );
    }

    function fmtBytes(n) {
      n = Number(n || 0);
      if (n < 1024) return n + " B";
      const units = ["KB", "MB", "GB", "TB"]; let i = -1;
      do { n /= 1024; i++; } while (n >= 1024 && i < units.length - 1);
      return n.toFixed(1) + " " + units[i];
    }

    function renderFootprint(data) {
      const f = data.footprint || {};
      const node = el("footprint");
      const bySource = (data.trajectory_index || {}).by_source || {};
      const rows = [
        ["index database", fmtBytes(f.index_bytes)],
        ["· embeddings (est)", fmtBytes(f.embedding_bytes_estimate)],
        ["· text (est)", fmtBytes(f.text_bytes_estimate)],
        ["semantic memory", fmtBytes(f.memory_bytes)],
        ["indexed chunks", number(f.total_chunks)],
        ["file cap / tool", number(f.max_files_per_tool)],
      ];
      const list = document.createElement("div"); list.className = "list";
      const addRow = (label, value) => {
        const r = document.createElement("div"); r.className = "row kv";
        r.innerHTML = '<div class="label"></div><div class="mono"></div>';
        r.children[0].textContent = label; r.children[1].textContent = value;
        list.appendChild(r);
      };
      for (const [k, v] of rows) addRow(k, v);
      const avail = f.available_source_files || {};
      for (const src of Object.keys(avail)) {
        const indexed = (bySource[src] || {}).sessions || 0;
        addRow(`coverage: ${src}`, `${number(indexed)} / ${number(avail[src])} files`);
      }
      node.innerHTML = ""; node.appendChild(list);
    }

    function renderStats(data) {
      const bySource = data.trajectory_index.by_source || {};
      const sessions = Object.values(bySource).reduce((s, i) => s + Number(i.sessions || 0), 0);
      set("sessions", number(sessions));
      set("chunks", number(data.trajectory_index.total_chunks));
      const sem = data.semantic_search || {};
      set("embedded", number(sem.embedded_chunks));
      el("embedded-bar").style.width = Math.round((sem.coverage || 0) * 100) + "%";
    }

    function renderJob(data) {
      const m = data.maintenance || {};
      const node = el("job-status");
      const buttons = document.querySelectorAll(".toolbar .btn");
      if (m.running) {
        node.className = "job-status running";
        node.textContent = `running: ${m.action}… (started ${ago(m.started_at)})`;
        buttons.forEach(b => b.disabled = true);
      } else {
        buttons.forEach(b => b.disabled = false);
        if (m.error) { node.className = "job-status err"; node.textContent = `${m.action} failed: ${m.error}`; }
        else if (m.finished_at) {
          node.className = "job-status";
          let extra = "";
          const r = m.result || {};
          if (typeof r.updated_chunks_total === "number") extra = ` · ${number(r.updated_chunks_total)} embedded`;
          else if (typeof r.chunks === "number") extra = ` · ${number(r.chunks)} chunks`;
          else if (typeof r.chunks_inserted === "number") extra = ` · ${number(r.chunks_inserted)} chunks`;
          node.textContent = `${m.action} done ${ago(m.finished_at)}${extra}`;
        } else { node.className = "job-status"; node.textContent = "idle"; }
      }
    }

    function renderWarnings(data) {
      const node = el("warnings");
      const warnings = data.warnings || [];
      if (!warnings.length) { node.innerHTML = '<span class="pill" style="color:var(--good)">all clear</span>'; return; }
      const list = document.createElement("div");
      list.className = "list";
      for (const w of warnings) {
        const row = document.createElement("div");
        row.className = "row";
        row.innerHTML = '<div style="font-weight:600;font-size:13px;color:var(--warn)"></div><div class="label" style="color:var(--muted);font-size:12px"></div>';
        row.children[0].textContent = w.title;
        row.children[1].textContent = w.detail;
        list.appendChild(row);
      }
      node.innerHTML = ""; node.appendChild(list);
    }

    function renderLargest(data) {
      const rows = data.largest_sessions || [];
      const node = el("largest");
      if (!rows.length) { node.innerHTML = '<div class="empty">No sessions indexed.</div>'; return; }
      const table = document.createElement("table");
      table.innerHTML = "<thead><tr><th class='num'>Chunks</th><th>Source</th><th>Title</th><th>Updated</th><th></th></tr></thead><tbody></tbody>";
      const tb = table.querySelector("tbody");
      for (const r of rows) {
        const tr = document.createElement("tr");
        tr.innerHTML = "<td class='num mono'></td><td></td><td class='truncate'></td><td class='mono'></td><td></td>";
        tr.children[0].textContent = number(r.chunks);
        tr.children[1].textContent = text(r.source_name);
        tr.children[2].textContent = text(r.title);
        tr.children[3].textContent = ago(r.updated_at);
        tr.children[4].appendChild(excludeSessionButton(r.source_ref, r.source_name, r.title));
        tb.appendChild(tr);
      }
      node.innerHTML = ""; node.appendChild(table);
    }

    function excludeSessionButton(source_ref, source_name, title) {
      const b = document.createElement("button");
      b.className = "btn"; b.textContent = "exclude"; b.style.padding = "2px 8px"; b.style.fontSize = "11px";
      b.onclick = () => {
        if (!source_ref) return;
        if (confirm(`Exclude this session?\n${title || source_ref}\nIt will be purged from the index.`))
          scopeAction("exclude", "session", source_ref, source_name);
      };
      return b;
    }

    function renderRecent(data) {
      const rows = data.recent_sessions || [];
      const node = el("recent");
      if (!rows.length) { node.innerHTML = '<div class="empty">No indexed trajectories.</div>'; return; }
      const table = document.createElement("table");
      table.innerHTML = "<thead><tr><th>Updated</th><th>Source</th><th>Title</th><th>Working directory</th><th></th></tr></thead><tbody></tbody>";
      const tb = table.querySelector("tbody");
      for (const item of rows) {
        const tr = document.createElement("tr");
        tr.innerHTML = "<td class='mono'></td><td></td><td class='truncate'></td><td class='mono truncate'></td><td></td>";
        tr.children[0].textContent = ago(item.updated_at);
        tr.children[1].textContent = text(item.source_name);
        tr.children[2].textContent = text(item.title);
        tr.children[3].textContent = text(item.cwd);
        tr.children[4].appendChild(excludeSessionButton(item.source_ref, item.source_name, item.title));
        tb.appendChild(tr);
      }
      node.innerHTML = ""; node.appendChild(table);
    }

    function renderRetrievals(data) {
      const rows = data.retrievals || [];
      const node = el("retrievals");
      if (!rows.length) { node.innerHTML = '<div class="empty">No retrievals recorded yet.</div>'; return; }
      const table = document.createElement("table");
      table.innerHTML = "<thead><tr><th>When</th><th>Query</th><th class='num'>Budget</th></tr></thead><tbody></tbody>";
      const tb = table.querySelector("tbody");
      for (const item of rows) {
        const tr = document.createElement("tr");
        tr.innerHTML = "<td class='mono'></td><td class='truncate'></td><td class='num mono'></td>";
        tr.children[0].textContent = ago(item.timestamp);
        tr.children[1].textContent = text(item.query);
        tr.children[2].textContent = `${number(item.tool_calls)} calls · ${Number(item.seconds || 0).toFixed(1)}s`;
        tb.appendChild(tr);
      }
      node.innerHTML = ""; node.appendChild(table);
    }

    function renderRuntime(data) {
      const node = el("runtime");
      node.innerHTML = "";
      const rows = [
        ["server", `${data.server.host}:${data.server.port}`],
        ["backend", data.server.provider_backend],
        ["profile", data.server.codex_permission_profile || "none"],
        ["runtime cwd", data.server.codex_cwd],
      ];
      for (const [label, value] of rows) {
        const row = document.createElement("div");
        row.className = "row kv";
        row.innerHTML = '<div class="label"></div><div class="mono truncate" style="max-width:60%"></div>';
        row.children[0].textContent = label;
        row.children[1].textContent = text(value);
        node.appendChild(row);
      }
    }

    function renderPolicy(data) {
      const accounts = (data.access || {}).accounts || [];
      const node = el("policy");
      if (!accounts.length) { node.innerHTML = '<div class="empty">No access config loaded.</div>'; return; }
      const table = document.createElement("table");
      table.innerHTML = "<thead><tr><th>Source</th><th>Root</th><th>Mode</th><th>Rules</th></tr></thead><tbody></tbody>";
      const tb = table.querySelector("tbody");
      for (const a of accounts) {
        const rules = document.createElement("div"); rules.className = "chips";
        const add = (v) => rules.appendChild(Object.assign(document.createElement("span"), {className:"chip", textContent:v}));
        for (const v of a.excluded_workdir_classes || []) add(`exclude class: ${v}`);
        for (const v of a.excluded_workdirs || []) add(`exclude: ${v}`);
        for (const v of a.included_workdir_classes || []) add(`include class: ${v}`);
        for (const v of a.included_workdirs || []) add(`include: ${v}`);
        for (const v of a.excluded_entrypoints || []) add(`exclude entrypoint: ${v}`);
        if (!rules.children.length) add("no exclusions");
        const tr = document.createElement("tr");
        tr.innerHTML = "<td></td><td class='mono truncate'></td><td></td><td></td>";
        tr.children[0].textContent = `${a.source_name}:${a.name}`;
        tr.children[1].textContent = a.base_dir;
        tr.children[2].textContent = a.visibility_mode;
        tr.children[3].appendChild(rules);
        tb.appendChild(tr);
      }
      node.innerHTML = ""; node.appendChild(table);
    }

    async function scopeAction(action, kind, value, source_name) {
      const toast = el("scope-toast");
      toast.className = "job-status running";
      toast.textContent = `${action} ${kind || ""}…`;
      try {
        const res = await fetch("/gui/scope", {
          method: "POST", headers: {"Content-Type": "application/json"},
          body: JSON.stringify({action, kind, value, source_name}),
        });
        const data = await res.json();
        if (!data.ok) { toast.className = "job-status err"; toast.textContent = data.error || "failed"; return; }
        const p = data.purge || {};
        toast.className = "job-status";
        toast.textContent = `${action} applied — purged ${number(p.purged_sessions || 0)} session(s), ` +
          `${number(p.purged_chunks || 0)} chunks` + (data.reindexing ? " · re-indexing in background" : "");
      } catch (e) { toast.className = "job-status err"; toast.textContent = String(e); }
      load();
    }

    async function reviewProject(decision, cwd, source_name) {
      const toast = el("scope-toast");
      toast.className = "job-status running"; toast.textContent = `${decision} ${cwd}…`;
      try {
        const res = await fetch("/gui/scope", {
          method: "POST", headers: {"Content-Type": "application/json"},
          body: JSON.stringify({action: "review", decision, value: cwd, source_name}),
        });
        const data = await res.json();
        toast.className = data.ok ? "job-status" : "job-status err";
        toast.textContent = data.ok ? `${decision}: ${cwd}` : (data.error || "failed");
      } catch (e) { toast.className = "job-status err"; toast.textContent = String(e); }
      load();
    }

    function renderNewProjects(scope) {
      const node = el("scope-new");
      const pending = scope.new_projects || [];
      if (!pending.length) { node.innerHTML = ""; return; }
      const wrap = document.createElement("div");
      wrap.className = "health warn";
      wrap.style.cssText = "display:block;margin-bottom:12px";
      const head = document.createElement("div");
      head.className = "title";
      head.textContent = `${pending.length} new project${pending.length > 1 ? "s" : ""} auto-included — review`;
      wrap.appendChild(head);
      for (const p of pending.slice(0, 8)) {
        const row = document.createElement("div");
        row.className = "kv"; row.style.cssText = "margin-top:8px;align-items:center";
        const label = document.createElement("div");
        label.className = "mono truncate"; label.style.maxWidth = "60%";
        label.textContent = `${p.cwd}  (${p.source_name}, ${number(p.sessions)} sess)`;
        label.title = p.cwd;
        const btns = document.createElement("div");
        const keep = document.createElement("button"); keep.className = "btn"; keep.textContent = "keep";
        keep.style.cssText = "padding:2px 10px;font-size:11px;margin-right:6px";
        keep.onclick = () => reviewProject("keep", p.cwd, p.source_name);
        const excl = document.createElement("button"); excl.className = "btn"; excl.textContent = "exclude";
        excl.style.cssText = "padding:2px 10px;font-size:11px";
        excl.onclick = () => reviewProject("exclude", p.cwd, p.source_name);
        btns.appendChild(keep); btns.appendChild(excl);
        row.appendChild(label); row.appendChild(btns);
        wrap.appendChild(row);
      }
      node.innerHTML = ""; node.appendChild(wrap);
    }

    function renderScope(data) {
      const scope = data.scope || {};
      const accounts = scope.accounts || [];
      const projects = scope.indexed_projects || [];
      renderNewProjects(scope);
      const inc = el("scope-included");
      el("scope-included-count").textContent = projects.length ? `(${projects.length})` : "";
      if (!projects.length) {
        inc.innerHTML = '<div class="empty">No indexed projects.</div>';
      } else {
        const t = document.createElement("table");
        t.innerHTML = "<thead><tr><th>Project</th><th>Src</th><th class='num'>Sess</th><th></th></tr></thead><tbody></tbody>";
        const tb = t.querySelector("tbody");
        for (const p of projects) {
          const tr = document.createElement("tr");
          tr.innerHTML = "<td class='mono truncate'></td><td></td><td class='num mono'></td><td></td>";
          tr.children[0].textContent = p.cwd || "(unknown)";
          tr.children[0].title = p.cwd || "";
          tr.children[1].textContent = p.source_name;
          tr.children[2].textContent = number(p.sessions);
          if (p.cwd) {
            const b = document.createElement("button");
            b.className = "btn"; b.textContent = "exclude"; b.style.padding = "2px 8px"; b.style.fontSize = "11px";
            b.onclick = () => { if (confirm(`Exclude project ${p.cwd}?\nIts indexed sessions will be purged from the index.`)) scopeAction("exclude", "workdir", p.cwd, p.source_name); };
            tr.children[3].appendChild(b);
          }
          tb.appendChild(tr);
        }
        inc.innerHTML = ""; inc.appendChild(t);
      }
      const exc = el("scope-excluded"); exc.innerHTML = "";
      for (const a of accounts) {
        const box = document.createElement("div"); box.className = "row";
        const head = document.createElement("div"); head.className = "kv";
        const name = document.createElement("div"); name.style.fontWeight = "600";
        name.textContent = `${a.source_name}:${a.name}`;
        const sel = document.createElement("select"); sel.style.fontSize = "12px";
        for (const m of ["blacklist", "whitelist"]) {
          const o = document.createElement("option"); o.value = m; o.textContent = m;
          if (a.visibility_mode === m) o.selected = true; sel.appendChild(o);
        }
        sel.onchange = () => scopeAction("set_visibility", "", sel.value, a.source_name);
        head.appendChild(name); head.appendChild(sel); box.appendChild(head);
        const chips = document.createElement("div"); chips.className = "chips"; chips.style.marginTop = "6px";
        const addChip = (kind, val, label) => {
          const c = document.createElement("span"); c.className = "chip"; c.style.cursor = "pointer";
          c.textContent = label + "  ✕"; c.title = "click to remove exclusion";
          c.onclick = () => scopeAction("unexclude", kind, val, a.source_name);
          chips.appendChild(c);
        };
        for (const v of a.excluded_workdirs || []) addChip("workdir", v, "dir: " + v);
        for (const v of a.excluded_workdir_classes || []) addChip("workdir_class", v, "class: " + v);
        for (const v of a.excluded_entrypoints || []) addChip("entrypoint", v, "entry: " + v);
        for (const v of a.excluded_session_ids || []) addChip("session", v, "session: " + String(v).slice(0, 8));
        if (!chips.children.length) {
          const s = document.createElement("span"); s.className = "muted"; s.style.fontSize = "12px"; s.textContent = "no exclusions";
          chips.appendChild(s);
        }
        box.appendChild(chips); exc.appendChild(box);
      }
    }

    async function load() {
      const res = await fetch("/gui/state", {cache: "no-store"});
      const data = await res.json();
      set("generated", `updated ${ago(data.generated_at)}`);
      renderHealth(data);
      renderStats(data);
      renderFootprint(data);
      renderJob(data);
      renderWarnings(data);
      renderLargest(data);
      renderRecent(data);
      renderRetrievals(data);
      renderRuntime(data);
      renderPolicy(data);
      renderScope(data);
    }

    function escapeText(s) { const d = document.createElement("div"); d.textContent = s || ""; return d.innerHTML; }

    async function runQuery() {
      const q = el("q").value.trim();
      const node = el("results");
      if (!q) { node.innerHTML = '<div class="empty">Type a query to probe the retriever.</div>'; return; }
      node.innerHTML = '<div class="empty">Searching…</div>';
      try {
        const res = await fetch("/gui/query", {
          method: "POST", headers: {"Content-Type": "application/json"},
          body: JSON.stringify({query: q, mode: el("q-mode").value, limit: 10}),
        });
        const data = await res.json();
        if (!data.ok) { node.innerHTML = `<div class="empty">Error: ${escapeText(data.error)}</div>`; return; }
        if (!data.results.length) { node.innerHTML = '<div class="empty">No matches.</div>'; return; }
        node.innerHTML = "";
        for (const r of data.results) {
          const md = r.metadata || {};
          const div = document.createElement("div");
          div.className = "result";
          div.innerHTML =
            '<div class="r-top"><div class="r-title"></div><div class="r-score"></div></div>' +
            '<div class="r-meta"></div><div class="r-snippet"></div>';
          div.querySelector(".r-title").textContent = r.title || "(untitled)";
          div.querySelector(".r-score").textContent = Number(r.score || 0).toFixed(1);
          const meta = div.querySelector(".r-meta");
          meta.innerHTML =
            `<span class="pill">${escapeText(r.source_name)}</span>` +
            `<span>chunk ${text(md.chunk_index)}</span>` +
            `<span>match: ${text(r.match_kind)}</span>` +
            `<span>exact ${text(md.exact_score)} · vec ${text(md.vector_score)}</span>` +
            `<span>${ago(r.updated_at)}</span>`;
          div.querySelector(".r-snippet").textContent = r.text_preview || r.summary_short || "";
          node.appendChild(div);
        }
      } catch (e) {
        node.innerHTML = `<div class="empty">Request failed: ${escapeText(String(e))}</div>`;
      }
    }
    el("q-run").onclick = runQuery;
    el("q").addEventListener("keydown", (e) => { if (e.key === "Enter") runQuery(); });

    document.querySelectorAll(".toolbar .btn").forEach((btn) => {
      btn.onclick = async () => {
        const action = btn.getAttribute("data-action");
        el("job-status").className = "job-status running";
        el("job-status").textContent = `starting ${action}…`;
        try {
          const res = await fetch("/gui/maintenance", {
            method: "POST", headers: {"Content-Type": "application/json"},
            body: JSON.stringify({action}),
          });
          const data = await res.json();
          if (!data.ok) { el("job-status").className = "job-status err"; el("job-status").textContent = data.error || "failed to start"; }
        } catch (e) {
          el("job-status").className = "job-status err"; el("job-status").textContent = String(e);
        }
        load();
      };
    });

    load().catch((err) => set("generated", `load failed: ${err}`));
    setInterval(load, 15000);
  </script>
</body>
</html>
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_under(candidate: str, root: str) -> bool:
    try:
        candidate_path = Path(candidate).expanduser().resolve()
        root_path = Path(root).expanduser().resolve()
        return os.path.commonpath([str(candidate_path), str(root_path)]) == str(root_path)
    except (OSError, ValueError):
        return False


def _recent_indexed_sessions(db_path: str, limit: int = 12) -> list[dict[str, Any]]:
    if not os.path.exists(db_path):
        return []
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT source_name, source_ref, title, cwd, MAX(updated_at) AS updated_at
            FROM trajectory_chunks
            GROUP BY source_ref
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def _file_group_size(path: str) -> int:
    total = 0
    for suffix in ("", "-wal", "-shm"):
        try:
            total += os.path.getsize(path + suffix)
        except OSError:
            continue
    return total


def _footprint(state: "AppState") -> dict[str, Any]:
    from sources.access import account_patterns

    db = state.config.trajectory_index_db_path
    index_bytes = _file_group_size(db)
    memory_bytes = _file_group_size(state.config.memory_db_path)
    total = embedded = 0
    text_estimate = embed_estimate = 0
    try:
        with sqlite3.connect(db) as conn:
            conn.row_factory = sqlite3.Row
            total = conn.execute("SELECT COUNT(*) FROM trajectory_chunks").fetchone()[0]
            embedded = conn.execute(
                "SELECT COUNT(*) FROM trajectory_chunks "
                "WHERE embedding_blob IS NOT NULL OR embedding_json != '[]'"
            ).fetchone()[0]
            sample = conn.execute(
                "SELECT length(text) t, length(embedding_json) ej, length(embedding_blob) eb "
                "FROM trajectory_chunks ORDER BY id DESC LIMIT 300"
            ).fetchall()
        if sample and total:
            n = len(sample)
            avg_text = sum((row["t"] or 0) for row in sample) / n
            avg_embed = sum(((row["ej"] or 0) + (row["eb"] or 0)) for row in sample) / n
            text_estimate = int(avg_text * total)
            embed_estimate = int(avg_embed * total)
    except sqlite3.Error:
        pass

    available: dict[str, int] = {}
    try:
        for source_name in state.trajectory_chunk_index.lookup.source_names:
            count = 0
            for account in state.access_config.accounts_for_source(source_name):
                for pattern in account_patterns(account):
                    count += len(glob.glob(os.path.expanduser(pattern), recursive=True))
            available[source_name] = count
    except Exception:  # pragma: no cover - coverage estimate is best-effort
        available = {}

    return {
        "index_bytes": index_bytes,
        "memory_bytes": memory_bytes,
        "total_chunks": total,
        "embedded_chunks": embedded,
        "embedding_bytes_estimate": embed_estimate,
        "text_bytes_estimate": text_estimate,
        "max_files_per_tool": state.config.trajectory_max_files_per_tool,
        "available_source_files": available,
    }


def _largest_sessions(db_path: str, limit: int = 8) -> list[dict[str, Any]]:
    if not os.path.exists(db_path):
        return []
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT source_name, source_ref, title, cwd,
                   COUNT(*) AS chunks, MAX(updated_at) AS updated_at
            FROM trajectory_chunks
            GROUP BY source_ref
            ORDER BY chunks DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def _session_files(state_dir: str, limit: int = 80) -> list[str]:
    pattern = os.path.join(state_dir, "sessions", "*.jsonl")
    files = [path for path in glob.glob(pattern) if os.path.isfile(path)]
    files.sort(key=lambda path: os.path.getmtime(path), reverse=True)
    return files[:limit]


def _recent_retrievals(state_dir: str, limit: int = 12) -> list[dict[str, Any]]:
    retrievals: list[dict[str, Any]] = []
    for path in _session_files(state_dir):
        last_user = ""
        try:
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if entry.get("type") != "message":
                        continue
                    role = entry.get("role")
                    if role == "user":
                        last_user = str(entry.get("content") or "")
                        continue
                    if role != "assistant":
                        continue
                    meta = entry.get("meta") if isinstance(entry.get("meta"), dict) else {}
                    budgets = meta.get("retrieval_budget") if isinstance(meta.get("retrieval_budget"), list) else []
                    if not budgets:
                        continue
                    retrievals.append(
                        {
                            "timestamp": entry.get("timestamp") or "",
                            "actor_id": meta.get("actor_id") or "",
                            "session": os.path.basename(path).removesuffix(".jsonl"),
                            "query": " ".join(last_user.split())[:180],
                            "tool_calls": sum(int(item.get("tool_calls") or 1) for item in budgets if isinstance(item, dict)),
                            "seconds": round(sum(float(item.get("seconds") or 0) for item in budgets if isinstance(item, dict)), 3),
                            "tokens_estimate": sum(int(item.get("tokens_estimate") or 0) for item in budgets if isinstance(item, dict)),
                        }
                    )
        except OSError:
            continue
    retrievals.sort(key=lambda item: str(item.get("timestamp") or ""), reverse=True)
    return retrievals[:limit]


def _access_accounts(state: "AppState") -> list[dict[str, Any]]:
    accounts: list[dict[str, Any]] = []
    for source_name, source_accounts in sorted(state.access_config.sources.items()):
        for account in source_accounts:
            payload = account.to_json()
            payload["source_name"] = source_name
            accounts.append(payload)
    return accounts


def _warnings(state: "AppState") -> list[dict[str, str]]:
    config = state.config
    project_root = Path(__file__).resolve().parent.parent
    warnings: list[dict[str, str]] = []
    if config.host not in {"127.0.0.1", "localhost"}:
        warnings.append({"title": "network", "detail": f"Server is bound to {config.host}; prefer 127.0.0.1 for local mybot."})
    if not config.codex_permission_profile:
        warnings.append({"title": "sandbox", "detail": "CODEX_PERMISSION_PROFILE is not set."})
    if _is_under(config.codex_cwd, str(project_root)):
        warnings.append({"title": "runtime", "detail": "Codex runtime cwd is inside the mybot repo."})
    if _is_under(config.mybot_tool_path, str(project_root)):
        warnings.append({"title": "tool", "detail": "Model-visible mybot tool path is inside the repo."})
    if not Path(config.mybot_tool_path).expanduser().exists():
        warnings.append({"title": "tool", "detail": "Generated mybot runtime tool is missing."})
    if not state.sync_auth.configured():
        warnings.append({"title": "sync", "detail": "Sync token config is not present."})
    if not default_config_path().exists():
        warnings.append({"title": "policy", "detail": "Trajectory access config is not present; defaults are permissive."})
    return warnings


def build_gui_state(state: "AppState") -> dict[str, Any]:
    try:
        trajectory_stats = state.trajectory_chunk_index.stats()
    except Exception as exc:  # pragma: no cover - status endpoint should stay available
        trajectory_stats = {"error": str(exc), "by_source": {}}
    try:
        trajectory_freshness = state.trajectory_chunk_index.freshness()
    except Exception as exc:  # pragma: no cover - status endpoint should stay available
        trajectory_freshness = {"error": str(exc), "stale": False}
    memory_index = state.get_memory_index() or {}
    warnings = _warnings(state)
    if trajectory_freshness.get("stale"):
        warnings.append(
            {
                "title": "index",
                "detail": "Raw trajectory files are newer than the searchable trajectory index.",
            }
        )

    total_chunks = int(trajectory_stats.get("total_chunks") or 0)
    embedded_chunks = int(trajectory_stats.get("embedded_chunks") or 0)
    coverage = (embedded_chunks / total_chunks) if total_chunks else 0.0
    semantic_enabled = total_chunks > 0 and coverage >= 0.5
    semantic_search = {
        "enabled": semantic_enabled,
        "total_chunks": total_chunks,
        "embedded_chunks": embedded_chunks,
        "missing_embeddings": int(trajectory_stats.get("missing_embeddings") or 0),
        "coverage": round(coverage, 4),
        "autobuild_vectors": bool(state.config.trajectory_index_autobuild_vectors),
    }
    if total_chunks and embedded_chunks == 0:
        warnings.append(
            {
                "title": "semantic search",
                "detail": "No embeddings present — hybrid retrieval is running lexical-only. "
                "Run 'Backfill embeddings' to restore semantic recall.",
            }
        )
    elif total_chunks and coverage < 0.5:
        warnings.append(
            {
                "title": "semantic search",
                "detail": f"Only {embedded_chunks:,}/{total_chunks:,} chunks embedded; "
                "semantic recall is partial until backfill completes.",
            }
        )

    with state.maintenance_lock:
        maintenance = dict(state.maintenance_status)

    try:
        scope = state.scope_summary()
    except Exception as exc:  # pragma: no cover - status surface stays available
        scope = {"error": str(exc), "accounts": [], "indexed_projects": []}

    try:
        footprint = _footprint(state)
    except Exception as exc:  # pragma: no cover - status surface stays available
        footprint = {"error": str(exc)}

    refresh_health = {
        "cadence_seconds": state.config.trajectory_index_background_refresh_seconds,
        "last_ok": getattr(state, "background_refresh_last_ok", ""),
        "error": getattr(state, "background_refresh_error", None),
    }

    return {
        "ok": True,
        "generated_at": _utc_now(),
        "scope": scope,
        "footprint": footprint,
        "refresh_health": refresh_health,
        "server": {
            "host": state.config.host,
            "port": state.config.port,
            "provider_backend": state.config.provider_backend,
            "codex_permission_profile": state.config.codex_permission_profile,
            "codex_cwd": state.config.codex_cwd,
            "mybot_tool_path": state.config.mybot_tool_path,
        },
        "trajectory_index": trajectory_stats,
        "trajectory_index_freshness": trajectory_freshness,
        "semantic_search": semantic_search,
        "maintenance": maintenance,
        "semantic_memory": state.memory_store.get_stats(),
        "memory_index_counts": memory_index.get("counts", {}) if isinstance(memory_index, dict) else {},
        "access": {
            "config_path": str(default_config_path()),
            "accounts": _access_accounts(state),
        },
        "warnings": warnings,
        "recent_sessions": _recent_indexed_sessions(state.config.trajectory_index_db_path),
        "largest_sessions": _largest_sessions(state.config.trajectory_index_db_path),
        "retrievals": _recent_retrievals(state.config.state_dir),
    }
