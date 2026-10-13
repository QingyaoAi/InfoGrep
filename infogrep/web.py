"""Local web UI for testing InfoGrep search in a browser.

A tiny stdlib HTTP server (no extra deps) that serves a one-page search interface plus a
JSON API backed by :class:`infogrep.engine.SearchEngine`. Bound to localhost by default —
this is a local test/debug surface, not a public service.

    infogrep serve --dir <indexed-dir> --port 7421
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__
from .config import Config
from .engine import FILE_TYPES, SearchEngine, search_across


def _reveal_in_file_manager(path: str) -> None:
    """Open the file's containing folder in the OS file manager, selecting the file."""
    if sys.platform == "darwin":
        subprocess.run(["open", "-R", path], check=False)
    elif sys.platform.startswith("win"):
        subprocess.run(["explorer", "/select,", path], check=False)
    else:  # Linux / other: open the folder (selecting a file isn't portable)
        subprocess.run(["xdg-open", os.path.dirname(path)], check=False)

# Uncommon, easy-to-type default port (in the dynamic range, unlikely to collide).
DEFAULT_PORT = 7421

# InfoGrep's logo (the app icon: a compass star on a rounded square) at 48 px, inlined so
# the single-file server needs no static asset route. It is the shape of
# macos/assets/AppIcon.icns without its margin and shadow; docs/assets has larger copies.
_LOGO_PNG_B64 = (
    "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"
)


def _icon_data_uri() -> str:
    return "data:image/png;base64," + _LOGO_PNG_B64


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<link rel="icon" type="image/png" href="__ICON_DATA_URI__"/>
<title>InfoGrep</title>
<style>
  :root {
    --bg:#0e1116; --panel:#151a22; --card:#1a212c; --fg:#e8eaef; --mut:#8b93a7;
    --acc:#4f9cf9; --acc-fg:#08192e; --bd:#242c3a; --bd-soft:#1e2632;
    --ok:#3ecf8e; --warn:#f5a623; --danger:#ef6a6a; --mark:#3b5f96;
    --shadow:0 1px 2px rgba(0,0,0,.4), 0 8px 24px rgba(0,0,0,.25);
  }
  @media (prefers-color-scheme: light) {
    :root {
      --bg:#f5f6f8; --panel:#ffffff; --card:#ffffff; --fg:#1c2430; --mut:#69748c;
      --acc:#2f7de1; --acc-fg:#ffffff; --bd:#dde3ec; --bd-soft:#e8edf4;
      --ok:#1d9e6f; --warn:#c07d0a; --danger:#cc4444; --mark:#cfe3ff;
      --shadow:0 1px 2px rgba(20,30,50,.06), 0 8px 24px rgba(20,30,50,.06);
    }
  }
  * { box-sizing:border-box; }
  html, body { margin:0; }
  body { background:var(--bg); color:var(--fg);
    font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",sans-serif; }
  header { position:sticky; top:0; z-index:5; backdrop-filter:blur(10px);
    background:color-mix(in srgb, var(--bg) 82%, transparent);
    border-bottom:1px solid var(--bd-soft); }
  .bar { max-width:880px; margin:0 auto; padding:12px 20px; display:flex; align-items:center; gap:12px; }
  .brand { font-size:17px; font-weight:700; letter-spacing:.2px; display:flex; align-items:center; gap:8px; }
  .brand .logo { width:20px; height:20px; display:block; }
  .brand .ver { font-size:11px; font-weight:500; color:var(--mut); border:1px solid var(--bd);
    border-radius:20px; padding:1px 8px; }
  .bar .spacer { flex:1; }
  main { max-width:880px; margin:0 auto; padding:20px; }

  /* folder card */
  .folder { background:var(--panel); border:1px solid var(--bd); border-radius:14px;
    padding:14px 16px; box-shadow:var(--shadow); margin-bottom:18px; }
  .folder .row1 { display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
  select, input[type=number], input[type=time] { padding:8px 10px; background:var(--card);
    color:var(--fg); border:1px solid var(--bd); border-radius:9px; font-size:14px; }
  select#dir { font-weight:600; max-width:320px; }
  .fpath { color:var(--mut); font-size:12px; margin-top:6px; word-break:break-all; }
  .fstats { display:flex; gap:14px; flex-wrap:wrap; align-items:center; margin-top:10px;
    color:var(--mut); font-size:13px; }
  .fstats b { color:var(--fg); font-weight:600; }
  .pill { font-size:11.5px; padding:2px 9px; border-radius:20px; border:1px solid var(--bd); }
  .pill.ok { color:var(--ok); border-color:color-mix(in srgb, var(--ok) 40%, var(--bd)); }
  .pill.warn { color:var(--warn); border-color:color-mix(in srgb, var(--warn) 40%, var(--bd)); }
  .pill.busy { color:var(--acc); border-color:color-mix(in srgb, var(--acc) 40%, var(--bd)); }
  .factions { display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin-top:12px;
    padding-top:12px; border-top:1px solid var(--bd-soft); }
  button { font:inherit; cursor:pointer; border-radius:9px; border:1px solid var(--bd);
    background:var(--card); color:var(--fg); padding:7px 13px; font-size:13.5px; }
  button:hover { border-color:var(--acc); }
  button.primary { background:var(--acc); border-color:var(--acc); color:var(--acc-fg); font-weight:600; }
  button.danger:hover { border-color:var(--danger); color:var(--danger); }
  button:disabled { opacity:.5; cursor:default; }
  .sched { display:flex; align-items:center; gap:8px; margin-left:auto; color:var(--mut); font-size:13.5px; }
  .switch { position:relative; width:36px; height:21px; }
  .switch input { opacity:0; width:0; height:0; }
  .knob { position:absolute; inset:0; background:var(--bd); border-radius:20px; transition:.15s; }
  .knob:before { content:""; position:absolute; width:15px; height:15px; border-radius:50%;
    background:#fff; top:3px; left:3px; transition:.15s; }
  .switch input:checked + .knob { background:var(--acc); }
  .switch input:checked + .knob:before { transform:translateX(15px); }

  /* search */
  .searchbar { display:flex; gap:10px; }
  .searchbar input[type=text] { flex:1; padding:13px 16px; font-size:16px; background:var(--panel);
    color:var(--fg); border:1px solid var(--bd); border-radius:12px; outline:none; }
  .searchbar input[type=text]:focus { border-color:var(--acc);
    box-shadow:0 0 0 3px color-mix(in srgb, var(--acc) 22%, transparent); }
  .searchbar button { padding:0 22px; border-radius:12px; font-size:15px; }
  .controls { display:flex; gap:14px; align-items:center; flex-wrap:wrap; margin:12px 0 4px; }
  .seg { display:flex; background:var(--panel); border:1px solid var(--bd); border-radius:10px;
    padding:3px; gap:2px; }
  .seg label { padding:5px 12px; border-radius:8px; font-size:13px; color:var(--mut); cursor:pointer; }
  .seg input { display:none; }
  .seg input:checked + span { }
  .seg label:has(input:checked) { background:var(--acc); color:var(--acc-fg); font-weight:600; }
  .chk { color:var(--mut); display:flex; align-items:center; gap:6px; font-size:13.5px; }
  #types { width:fit-content; flex-wrap:wrap; margin:8px 0 4px; }
  .knum { display:flex; align-items:center; gap:6px; color:var(--mut); font-size:13.5px; }
  .knum input { width:60px; }

  /* results */
  .meta { color:var(--mut); font-size:13px; margin:14px 2px 8px; min-height:18px; }
  .meta .err { color:var(--danger); }
  .hit { background:var(--card); border:1px solid var(--bd); border-radius:12px;
    padding:12px 15px; margin:10px 0; transition:border-color .12s, transform .12s; }
  /* file-type tag: the extension, tinted by kind (the launcher uses the same colours) */
  .tag { font-size:10.5px; font-weight:650; letter-spacing:.3px; text-transform:uppercase;
    padding:2px 8px; border-radius:20px; color:var(--tc, var(--mut));
    background:color-mix(in srgb, var(--tc, var(--mut)) 18%, transparent); }
  .tag.t-pdf { --tc:#ff453a; } .tag.t-doc { --tc:#0a84ff; } .tag.t-slides { --tc:#ff9f0a; }
  .tag.t-sheet { --tc:#30d158; } .tag.t-image { --tc:#bf5af2; } .tag.t-data { --tc:#40c8e0; }
  @media (prefers-color-scheme: light) {
    .tag.t-pdf { --tc:#d70015; } .tag.t-doc { --tc:#0040dd; } .tag.t-slides { --tc:#c93400; }
    .tag.t-sheet { --tc:#248a3d; } .tag.t-image { --tc:#8944ab; } .tag.t-data { --tc:#0071a4; }
  }
  .hit.openable { cursor:pointer; }
  .hit.openable:hover { border-color:var(--acc); }
  .hit .top { display:flex; gap:9px; align-items:baseline; flex-wrap:wrap; }
  .hit .fname { font-weight:650; font-size:14.5px; }
  .hit .score { color:var(--mut); font-size:12px; font-variant-numeric:tabular-nums; }
  .badge { font-size:10.5px; padding:2px 8px; border-radius:20px; border:1px solid var(--bd);
    color:var(--acc); background:color-mix(in srgb, var(--acc) 10%, transparent); }
  .hit .reveal { margin-left:auto; font-size:11px; color:var(--mut); white-space:nowrap; }
  .hit .path { color:var(--mut); font-size:11.5px; margin-top:2px; word-break:break-all; }
  .snip { margin-top:7px; color:color-mix(in srgb, var(--fg) 82%, var(--mut)); white-space:pre-wrap;
    font-size:13.5px; overflow-wrap:anywhere; }
  mark { background:var(--mark); color:inherit; border-radius:3px; padding:0 1px; }
  .empty { text-align:center; color:var(--mut); padding:40px 0 20px; }
  /* an answer (Ask mode): what the agent did, what it concluded, the files it used */
  .answer { background:var(--card); border:1px solid var(--bd); border-radius:12px;
    padding:14px 16px; margin:10px 0; }
  .answer .steps { color:var(--mut); font-size:12.5px; }
  .answer .steps div:before { content:"› "; }
  .answer .text { white-space:pre-wrap; overflow-wrap:anywhere; font-size:14.5px; margin-top:10px; }
  .answer .srcs { margin-top:12px; padding-top:10px; border-top:1px solid var(--bd-soft);
    color:var(--mut); font-size:12.5px; }
  .answer .src { color:var(--acc); cursor:pointer; word-break:break-all; margin-top:3px; }
  .spin { display:inline-block; width:14px; height:14px; border:2px solid var(--mut);
    border-top-color:transparent; border-radius:50%; animation:sp .7s linear infinite;
    vertical-align:-2px; margin-right:6px; }
  @keyframes sp { to { transform:rotate(360deg); } }
  footer { max-width:880px; margin:0 auto; padding:10px 20px 26px; color:var(--mut); font-size:12px; }
  footer a { color:var(--mut); }
</style>
</head>
<body>
<header><div class="bar">
  <div class="brand"><img class="logo" src="__ICON_DATA_URI__" alt=""/> InfoGrep <span class="ver" id="ver"></span></div>
  <div class="spacer"></div>
  <button id="adddir" title="Choose a new folder to index">＋ Index a folder</button>
</div></header>

<main>
  <section class="folder">
    <div class="row1">
      <select id="dir" title="Folder to search"></select>
      <span id="fstate"></span>
    </div>
    <div class="fpath" id="fpath"></div>
    <div class="fstats" id="fstats"></div>
    <div class="factions">
      <button id="reindex" title="Incremental update: only changed files are re-read">↻ Update index</button>
      <button id="rebuild" title="Re-extract and re-index everything from scratch">Full rebuild</button>
      <button id="forget" class="danger" title="Delete this folder's index (the folder itself is untouched)">Remove index</button>
      <div class="sched" title="Re-index this folder automatically every day (macOS)">
        <label class="switch"><input type="checkbox" id="sched"/><span class="knob"></span></label>
        <span>daily at</span>
        <input type="time" id="schedat" value="03:00"/>
      </div>
    </div>
  </section>

  <form class="searchbar" id="f">
    <input type="text" id="q" placeholder="Search file contents…  ( / to focus )" autofocus autocomplete="off"/>
    <button id="go" class="primary" type="submit">Search</button>
  </form>

  <div class="controls">
    <div class="seg" id="modes" title="Retrieval mode">
      <label title="All retrievers fused with reciprocal rank fusion"><input type="radio" name="mode" value="hybrid" checked/><span>Hybrid</span></label>
      <label title="BM25 keyword search (exact terms, names, symbols)"><input type="radio" name="mode" value="sparse"/><span>Keyword</span></label>
      <label title="Embedding search (meaning and paraphrase)"><input type="radio" name="mode" value="dense"/><span>Semantic</span></label>
      <label title="Obsidian knowledge-base graph"><input type="radio" name="mode" value="kb"/><span>Notes</span></label>
      <label title="Folder/filename metadata graph"><input type="radio" name="mode" value="graph"/><span>Folders</span></label>
      <label id="askmode" style="display:none" title="A DeepSeek Harness agent searches and reads your files to answer a question"><input type="radio" name="mode" value="ask"/><span>Ask</span></label>
    </div>
    <div class="knum" id="kwrap"><span>results</span><input type="number" id="k" value="10" min="1" max="50"/></div>
    <label class="chk" id="prfwrap" title="RM3 pseudo-relevance feedback: expand the query with terms from top results">
      <input type="checkbox" id="prf"/> query expansion</label>
    <label class="chk" id="noteswrap" title="Let the agent search the Obsidian knowledge base too (slower)">
      <input type="checkbox" id="notes"/> include notes</label>
    <label class="chk" id="followwrap" title="Continue the conversation the last answer belongs to">
      <input type="checkbox" id="follow"/> follow-up on the last answer</label>
  </div>

  <div class="seg" id="types" title="Only show results of one file type"></div>
  <div class="meta" id="meta"></div>
  <div id="results"></div>
</main>
<footer><span id="local">Local-first search — nothing leaves this machine.</span> <a href="https://github.com/QingyaoAi/InfoGrep">GitHub</a></footer>

<script>
const $ = id => document.getElementById(id);
let INDEXES = [];          // /api/indexes payload entries
let TYPES = [];            // file-type groups: {id, label, exts}
let POLL = null;           // status poll timer while indexing
function curDir(){ return $('dir').value || ''; }
function entry(dir){ return INDEXES.find(i => i.dir === dir); }
function mode(){ return document.querySelector('#modes input:checked').value; }
function ftype(){ const e = document.querySelector('#types input:checked'); return e ? e.value : ''; }
function typeOf(ext){ const t = TYPES.find(t => t.exts.includes(ext)); return t ? t.id : ''; }
function renderTypes(types){
  if (TYPES.length === types.length) return;   // already drawn (keep the current choice)
  TYPES = types;
  $('types').innerHTML = [{id:'', label:'All types'}].concat(types).map((t, i) =>
    '<label><input type="radio" name="ftype" value="' + esc(t.id) + '"' + (i ? '' : ' checked') +
    '/><span>' + esc(t.label) + '</span></label>').join('');
}

function esc(s){ const d = document.createElement('div'); d.textContent = s == null ? '' : s; return d.innerHTML; }
function highlight(text, q){
  let h = esc(text);
  const terms = (q || '').split(/\s+/).filter(t => t.length > 1)
    .map(t => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
  if (terms.length) h = h.replace(new RegExp('(' + terms.join('|') + ')', 'gi'), '<mark>$1</mark>');
  return h;
}
function ago(ts){
  const s = Date.now()/1000 - ts;
  if (s < 90) return 'just now';
  if (s < 5400) return Math.round(s/60) + ' min ago';
  if (s < 129600) return Math.round(s/3600) + ' h ago';
  return Math.round(s/86400) + ' d ago';
}
function el(tag, cls, html){ const e = document.createElement(tag); if (cls) e.className = cls;
  if (html != null) e.innerHTML = html; return e; }
function note(html, isErr){ $('meta').innerHTML = isErr ? '<span class="err">'+html+'</span>' : html; }

// ---- folder card -----------------------------------------------------------
async function loadIndexes(keep){
  try{
    const d = await (await fetch('/api/indexes')).json();
    $('ver').textContent = d.version ? 'v' + d.version : '';
    $('askmode').style.display = d.ask ? '' : 'none';   // answering questions needs dsh
    INDEXES = d.indexes || [];
    renderTypes(d.types || []);
    const sel = $('dir'); const prev = keep || sel.value || d.default;
    sel.innerHTML = '';
    for (const i of INDEXES){
      const o = document.createElement('option'); o.value = i.dir;
      o.textContent = i.name + (i.indexing ? '  (indexing…)' : '');
      sel.appendChild(o);
    }
    if ([...sel.options].some(o => o.value === prev)) sel.value = prev;
    renderFolder();
    syncMode();
  }catch(e){}
}
async function renderFolder(){
  const dir = curDir(); const i = entry(dir);
  $('fpath').textContent = dir;
  $('sched').checked = !!(i && i.scheduled);
  if (i && i.schedule_at) $('schedat').value = i.schedule_at;
  for (const b of ['reindex','rebuild','forget']) $(b).disabled = !dir || !!(i && i.indexing);
  if (!dir){ $('fstate').innerHTML = ''; $('fstats').innerHTML = ''; return; }
  if (i && i.indexing){
    $('fstate').innerHTML = '<span class="pill busy"><span class="spin"></span>indexing…</span>';
    startPoll(dir);
  }
  try{
    const s = await (await fetch('/api/status?dir=' + encodeURIComponent(dir))).json();
    if (!s.indexed){
      $('fstate').innerHTML = '<span class="pill warn">not indexed</span>';
      $('fstats').innerHTML = 'Click <b>↻ Update index</b> to build the index for this folder.';
      return;
    }
    const bits = ['<span><b>' + Number(s.n_files).toLocaleString() + '</b> files</span>',
                  '<span><b>' + Number(s.n_passages).toLocaleString() + '</b> passages</span>'];
    if (s.last_indexed_at) bits.push('<span>indexed <b>' + ago(Number(s.last_indexed_at)) + '</b></span>');
    $('fstats').innerHTML = bits.join(' · ');
    if (!(i && i.indexing)){
      $('fstate').innerHTML = '<span class="pill">indexed</span>';
      checkStaleness(dir);  // slow folder walk; fills in stale/up-to-date when done
    }
  }catch(e){ $('fstate').innerHTML = ''; $('fstats').textContent = 'status unavailable'; }
}
let STALE_PENDING = null;  // folder whose staleness walk is in flight (dedupe)
async function checkStaleness(dir){
  if (STALE_PENDING === dir) return;
  STALE_PENDING = dir;
  try{
    const s = await (await fetch('/api/status?dir=' + encodeURIComponent(dir) + '&stale=1')).json();
    if (dir !== curDir() || !s.indexed) return;
    const i = entry(dir);
    if (i && i.indexing) return;
    $('fstate').innerHTML = s.stale
      ? '<span class="pill warn">stale · ' + s.pending + ' pending</span>'
      : '<span class="pill ok">up to date</span>';
    if (s.stale) $('fstats').innerHTML +=
      ' · <span>+' + s.pending_added + ' ~' + s.pending_modified + ' −' + s.pending_deleted + '</span>';
  }catch(e){}
  finally{ if (STALE_PENDING === dir) STALE_PENDING = null; }
}
function startPoll(dir){
  if (POLL) return;
  POLL = setInterval(async () => {
    await loadIndexes(dir);
    const i = entry(dir);
    if (!i || !i.indexing){ clearInterval(POLL); POLL = null; renderFolder(); }
  }, 2000);
}

// ---- actions ---------------------------------------------------------------
async function runIndex(dir, full){
  note('<span class="spin"></span>' + (full ? 'rebuilding' : 'updating') + ' index for ' + esc(dir) + ' …');
  try{
    const p = new URLSearchParams({dir, full: full ? '1' : '0'});
    const r = await (await fetch('/api/index?' + p, {method:'POST'})).json();
    if (r.ok === false){ note('Index: ' + esc(r.error || 'failed'), true); return; }
    await loadIndexes(dir); startPoll(dir);
  }catch(e){ note('Index: ' + esc(String(e)), true); }
}
async function addFolder(){
  const dir = prompt('Absolute path of a folder to index:');
  if (dir) runIndex(dir.trim(), false);
}
async function forgetIndex(){
  const dir = curDir(); if (!dir) return;
  if (!confirm('Remove the index for\n' + dir + ' ?\n\n(The folder itself is not touched.)')) return;
  try{
    const r = await (await fetch('/api/forget?dir=' + encodeURIComponent(dir), {method:'POST'})).json();
    if (!r.ok){ note('Remove: ' + esc(r.error || 'failed'), true); return; }
    note('Index removed for ' + esc(dir));
    await loadIndexes();
  }catch(e){ note('Remove: ' + esc(String(e)), true); }
}
async function applySchedule(){
  const dir = curDir(); if (!dir){ $('sched').checked = false; return; }
  const on = $('sched').checked, at = $('schedat').value || '03:00';
  try{
    const p = new URLSearchParams({dir, on: on ? '1' : '0', at});
    const r = await (await fetch('/api/schedule?' + p, {method:'POST'})).json();
    if (!r.ok){ note('Daily reindex: ' + esc(r.error || 'failed'), true); $('sched').checked = !on; return; }
    note(r.scheduled ? 'Daily reindex ON at ' + at + ' — ' + esc(dir) : 'Daily reindex off — ' + esc(dir));
    await loadIndexes(dir);
  }catch(e){ note('Daily reindex: ' + esc(String(e)), true); $('sched').checked = !on; }
}

// ---- search ----------------------------------------------------------------
async function search(ev){
  if (ev) ev.preventDefault();
  const q = $('q').value.trim(); if (!q) return;
  if (mode() === 'ask') return ask(q);
  $('go').disabled = true; note('<span class="spin"></span>searching…'); $('results').innerHTML = '';
  try{
    const p = new URLSearchParams({q, mode: mode(), k: $('k').value,
      prf: $('prf').checked ? '1' : '0', dir: curDir()});
    if (ftype()) p.set('type', ftype());
    const r = await (await fetch('/api/search?' + p)).json();
    if (r.error){ note(esc(r.error), true); return; }
    const used = (r.used || []).join(', ') || '—';
    let m = r.results.length + ' result(s) · retrievers: ' + esc(used);
    const sk = Object.entries(r.skipped || {});
    if (sk.length) m += ' · skipped: ' + esc(sk.map(([k,v]) => k + ' (' + v + ')').join(', '));
    note(m);
    for (const h of r.results){
      const card = el('div', 'hit');
      const top = el('div', 'top');
      top.appendChild(el('span', 'fname', esc(h.filename || h.path) + (h.page != null ? '  · p.' + h.page : '')));
      if (h.ext) top.appendChild(el('span', 'tag t-' + typeOf(h.ext), esc(h.ext)));
      top.appendChild(el('span', 'score', '[' + Number(h.score).toFixed(3) + ']'));
      top.appendChild(el('span', 'badge', esc(h.retriever)));
      if (h.abs_path){
        top.appendChild(el('span', 'reveal', '📂 open folder'));
        card.classList.add('openable');
        card.title = 'Open this file’s folder';
        card.addEventListener('click', () => { if (!String(window.getSelection())) reveal(h.abs_path); });
      }
      card.appendChild(top);
      card.appendChild(el('div', 'path', esc(h.abs_path || h.path)));
      card.appendChild(el('div', 'snip', highlight((h.snippet || '').trim(), q)));
      $('results').appendChild(card);
    }
    if (!r.results.length) $('results').appendChild(el('div', 'empty', 'No matches.'));
  }catch(e){ note(esc(String(e)), true); }
  finally{ $('go').disabled = false; }
}
async function reveal(path){
  try{
    const res = await (await fetch('/api/open?path=' + encodeURIComponent(path))).json();
    if (!res.ok) note('Could not open: ' + esc(res.error || 'error'), true);
  }catch(e){ note('Could not open: ' + esc(String(e)), true); }
}

// ---- ask -------------------------------------------------------------------
let ASKED = 0;      // the question on screen; polls for an earlier one are dropped
let SESSION = '';   // the conversation the last answer belongs to, for a follow-up
async function ask(q){
  $('go').disabled = true; note('<span class="spin"></span>thinking…'); $('results').innerHTML = '';
  try{
    const p = new URLSearchParams({q, dir: curDir()});
    if ($('follow').checked && SESSION) p.set('session', SESSION);
    if ($('notes').checked && (entry(curDir()) || {}).kb) p.set('notes', '1');
    const r = await (await fetch('/api/ask?' + p, {method:'POST'})).json();
    if (!r.ok){ note(esc(r.error || 'failed'), true); $('go').disabled = false; return; }
    ASKED = r.id; pollAsk(r.id);
  }catch(e){ note(esc(String(e)), true); $('go').disabled = false; }
}
async function pollAsk(id){
  if (id !== ASKED) return;
  let s;
  try{ s = await (await fetch('/api/ask?id=' + id)).json(); }
  catch(e){ note(esc(String(e)), true); $('go').disabled = false; return; }
  if (id !== ASKED) return;
  const steps = s.steps || [], sources = s.sources || [];
  $('results').innerHTML = '';
  if (steps.length || s.answer){
    const card = el('div', 'answer');
    if (steps.length) card.appendChild(el('div', 'steps', steps.map(t => '<div>' + esc(t) + '</div>').join('')));
    if (s.answer) card.appendChild(el('div', 'text', esc(s.answer)));
    if (sources.length){
      const box = el('div', 'srcs', 'Sources');
      for (const path of sources){
        const row = el('div', 'src', '📂 ' + esc(path));
        row.title = 'Open this file’s folder';
        row.addEventListener('click', () => reveal(path));
        box.appendChild(row);
      }
      card.appendChild(box);
    }
    $('results').appendChild(card);
  }
  if (s.status === 'running'){ setTimeout(() => pollAsk(id), 700); return; }
  $('go').disabled = false;
  if (s.status === 'done'){ SESSION = s.session || ''; note('answered from ' + sources.length + ' file(s)'); syncMode(); }
  else note(esc(s.error || s.status), true);
}
// Show the controls that mean something in the current mode.
function syncMode(){
  const m = mode(), asking = m === 'ask';
  $('prfwrap').style.display = (m === 'hybrid' || m === 'sparse') ? '' : 'none';
  $('kwrap').style.display = asking ? 'none' : '';
  $('types').style.display = asking ? 'none' : '';
  $('followwrap').style.display = (asking && SESSION) ? '' : 'none';
  $('noteswrap').style.display = (asking && (entry(curDir()) || {}).kb) ? '' : 'none';
  $('q').placeholder = asking ? 'Ask a question about your files…' : 'Search file contents…  ( / to focus )';
  $('go').textContent = asking ? 'Ask' : 'Search';
  $('local').textContent = asking
    ? 'An answer is written by the model dsh uses: the passages the agent reads are sent to it.'
    : 'Local-first search — nothing leaves this machine.';
}
// Have the server open this folder's index now, so the first search isn't the slow one.
function warm(){ const d = curDir(); if (d) fetch('/api/warm?dir=' + encodeURIComponent(d)).catch(() => {}); }

// ---- wiring ----------------------------------------------------------------
$('f').addEventListener('submit', search);
$('adddir').addEventListener('click', addFolder);
$('reindex').addEventListener('click', () => runIndex(curDir(), false));
$('rebuild').addEventListener('click', () => {
  if (confirm('Fully rebuild the index for\n' + curDir() + ' ?\n\nEvery file is re-extracted (can take a while).'))
    runIndex(curDir(), true);
});
$('forget').addEventListener('click', forgetIndex);
$('sched').addEventListener('change', applySchedule);
$('schedat').addEventListener('change', () => { if ($('sched').checked) applySchedule(); });
$('dir').addEventListener('change', () => { renderFolder(); syncMode(); warm(); $('q').focus(); });
$('modes').addEventListener('change', syncMode);
$('types').addEventListener('change', () => search());   // re-run the query for that type
document.addEventListener('keydown', e => {
  if (e.key === '/' && document.activeElement !== $('q')){ e.preventDefault(); $('q').focus(); }
});
syncMode();
loadIndexes().then(warm);
</script>
</body>
</html>"""

PAGE = PAGE.replace("__ICON_DATA_URI__", _icon_data_uri())


def _make_handler(directory: Path):
    import threading

    from .config import index_home, indexed_dirs

    default_dir = str(Config.load(directory).target_dir)
    engines: dict[str, SearchEngine] = {}  # dir -> SearchEngine (warm searchers)
    jobs: dict[str, str] = {}  # dir -> "running" | "done" | "error: ..."
    # The one question being answered, or the last one that was (see start_ask).
    asked: dict = {"job": None, "run": None}
    state_lock = threading.Lock()

    def engine_for(d: str | None) -> SearchEngine:
        key = str(Path(d).expanduser().resolve()) if d else default_dir
        with state_lock:
            eng = engines.get(key)
            if eng is None:
                eng = engines[key] = SearchEngine(Config.load(key))
            return eng

    def all_engines() -> list[SearchEngine]:
        """An engine per folder that has an index (``scope=all`` searches them together)."""
        return [engine_for(str(d)) for d in indexed_dirs()]

    def list_indexes() -> list[dict]:
        from . import scheduler
        from .indexer import Indexer

        schedules = {
            a["directory"]: f"{a['hour']:02d}:{a['minute']:02d}" for a in scheduler.list_agents()
        }
        out: list[dict] = []
        root = index_home() / "indexes"
        if root.is_dir():
            for d in sorted(root.iterdir()):
                src = d / "source.txt"
                if not src.is_file():
                    continue
                target = src.read_text().strip()
                cfg = Config.load(target)
                info = {
                    "dir": target,
                    "name": Path(target).name,
                    "scheduled": scheduler.is_scheduled(Path(target)),
                    "schedule_at": schedules.get(target),
                    "kb": cfg.kb.enabled,  # lets clients offer (or hide) a notes toggle
                }
                try:  # fast: read the manifest, don't walk the filesystem
                    st = Indexer(cfg).status(check_staleness=False)
                    info.update(indexed=st.get("indexed", False),
                                n_files=st.get("n_files"), n_passages=st.get("n_passages"))
                except Exception:
                    info["indexed"] = False
                with state_lock:
                    info["indexing"] = jobs.get(str(Path(target).resolve())) == "running"
                out.append(info)
        return out

    def effective_default(indexes: list[dict]) -> str:
        """The folder clients should search by default: the server's configured
        default if it has an index, otherwise the first indexed folder (searching
        an unindexed default silently returns nothing)."""
        if any(i["dir"] == default_dir and i.get("indexed") for i in indexes):
            return default_dir
        return next((i["dir"] for i in indexes if i.get("indexed")), default_dir)

    def start_index(d: str, full: bool = False) -> dict:
        target = Path(d).expanduser()
        if not target.is_dir():
            return {"ok": False, "error": "not a directory"}
        key = str(target.resolve())
        with state_lock:
            if jobs.get(key) == "running":
                return {"ok": True, "status": "already running", "dir": key}
            jobs[key] = "running"

        def run():
            from .indexer import Indexer

            try:
                Indexer(Config.load(key)).reindex(full=full)
                result = "done"
            except Exception as exc:
                result = f"error: {exc}"
            with state_lock:
                jobs[key] = result
                engines.pop(key, None)  # drop cached engine so search reopens fresh index

        threading.Thread(target=run, daemon=True).start()
        return {"ok": True, "status": "started", "dir": key}

    def set_schedule(d: str, on: bool, at: str) -> dict:
        """Enable/disable the daily incremental reindex agent for a folder (macOS)."""
        from . import scheduler

        if not d:
            return {"ok": False, "error": "no directory"}
        target = Path(d).expanduser().resolve()
        try:
            if on:
                hour, minute = (int(x) for x in at.split(":", 1))
                scheduler.install(target, hour=hour, minute=minute)
            else:
                scheduler.uninstall(target)
        except ValueError:
            return {"ok": False, "error": f"invalid time: {at!r} (use HH:MM)"}
        except RuntimeError as exc:  # e.g. not macOS
            return {"ok": False, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {"ok": True, "scheduled": scheduler.is_scheduled(target), "dir": str(target)}

    def forget_index(d: str) -> dict:
        """Delete a folder's side-car index (and its schedule). Never touches the folder."""
        import shutil

        from . import scheduler
        from .config import index_dir_for

        if not d:
            return {"ok": False, "error": "no directory"}
        target = Path(d).expanduser().resolve()
        idx = index_dir_for(target)
        # Only ever delete inside $INFOGREP_HOME/indexes (the side-car location).
        root = (index_home() / "indexes").resolve()
        if root not in idx.resolve().parents:
            return {"ok": False, "error": "not an InfoGrep index location"}
        key = str(target)
        with state_lock:
            if jobs.get(key) == "running":
                return {"ok": False, "error": "indexing is running; try again when it finishes"}
            jobs.pop(key, None)
            engines.pop(key, None)
        try:
            scheduler.uninstall(target)
        except Exception:
            pass  # best-effort; the index removal below is what matters
        if idx.is_dir():
            shutil.rmtree(idx)
        return {"ok": True, "dir": key}

    def start_ask(
        question: str, d: str | None, everywhere: bool, session: str, server: str, notes: bool
    ) -> dict:
        """Hand a question to a dsh agent (see :mod:`infogrep.ask`); poll it with ask_state.

        One question runs at a time: asking another stops the one in flight.
        """
        from .ask import Ask, AskError

        with state_lock:
            running, previous = asked["run"], asked["job"]
        if running is not None:
            running.cancel()
        target = None if everywhere else engine_for(d).config.target_dir
        try:
            run = Ask(question, directory=target, session=session or None, server=server,
                      notes=notes)
        except AskError as exc:
            return {"ok": False, "error": str(exc)}
        job = {"id": (previous["id"] if previous else 0) + 1, "status": "running",
               "question": question, "steps": [], "answer": "", "sources": [],
               "session": session, "error": ""}
        with state_lock:
            asked.update(job=job, run=run)

        def work():
            try:
                for event in run.events():
                    with state_lock:
                        if event["type"] == "session":
                            job["session"] = event["id"]
                        elif event["type"] == "step":
                            job["steps"].append(event["text"])
                        elif event["type"] == "answer":
                            job.update(status="done", answer=event["text"],
                                       sources=event["sources"])
            except AskError as exc:
                with state_lock:
                    job.update(status="cancelled" if run.cancelled else "error", error=str(exc))
            except Exception as exc:  # report it to the client rather than hang its poll
                with state_lock:
                    job.update(status="error", error=f"{type(exc).__name__}: {exc}")
            finally:
                with state_lock:
                    if asked["run"] is run:
                        asked["run"] = None

        threading.Thread(target=work, daemon=True).start()
        return {"ok": True, "id": job["id"]}

    def ask_state(job_id: str) -> dict:
        """Where a question stands: its steps so far, and the answer once there is one."""
        with state_lock:
            job = asked["job"]
            if job is None or (job_id and job_id != str(job["id"])):
                return {"status": "gone"}
            return {**job, "steps": list(job["steps"])}

    def cancel_ask() -> dict:
        with state_lock:
            running = asked["run"]
        if running is not None:
            running.cancel()
        return {"ok": True}

    class Handler(BaseHTTPRequestHandler):
        # Quiet by default (no per-request stderr logging).
        def log_message(self, *args):
            pass

        def _send(self, code: int, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, payload: dict, code: int = 200):
            self._send(code, json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8")

        def do_GET(self):
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            if parsed.path in ("/", "/index.html"):
                self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            elif parsed.path == "/api/ping":  # who is listening here, and on which indexes
                self._json({"version": __version__, "home": str(index_home().resolve())})
            elif parsed.path == "/api/status":
                self._json(self._status(qs))
            elif parsed.path == "/api/ask":  # poll a question started with POST
                self._json(ask_state(qs.get("id", [""])[0]))
            elif parsed.path == "/api/search":
                self._json(self._search(qs))
            elif parsed.path == "/api/warm":  # open a folder's backends before the first query
                for eng in self._engines(qs):
                    eng.warm()
                self._json({"ok": True})
            elif parsed.path == "/api/open":
                self._json(self._open(qs))
            elif parsed.path == "/api/indexes":
                indexes = list_indexes()
                types = [{"id": tid, "label": label, "exts": list(exts)}
                         for tid, (label, exts) in FILE_TYPES.items()]
                from .ask import dsh_command

                self._json(
                    {"indexes": indexes, "default": effective_default(indexes),
                     "types": types, "version": __version__,
                     "ask": dsh_command() is not None}  # question answering needs dsh
                )
            elif parsed.path == "/api/index":  # start (re)indexing a folder
                self._json(self._start_index(qs))
            elif parsed.path == "/api/schedule":  # toggle daily reindex for a folder
                self._json(self._set_schedule(qs))
            elif parsed.path == "/api/forget":  # remove a folder's side-car index
                self._json(forget_index((qs.get("dir", [""])[0]).strip()))
            else:
                self._json({"error": "not found"}, code=404)

        # POST also accepts the actions that change state.
        def do_POST(self):
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            if parsed.path == "/api/index":
                self._json(self._start_index(qs))
            elif parsed.path == "/api/ask":
                self._json(self._start_ask(qs))
            elif parsed.path == "/api/ask/cancel":
                self._json(cancel_ask())
            elif parsed.path == "/api/schedule":
                self._json(self._set_schedule(qs))
            elif parsed.path == "/api/forget":
                self._json(forget_index((qs.get("dir", [""])[0]).strip()))
            else:
                self._json({"error": "not found"}, code=404)

        def _start_ask(self, qs: dict) -> dict:
            question = (qs.get("q", [""])[0]).strip()
            if not question:
                return {"ok": False, "error": "empty question"}
            # The agent's searches come back to this server, which has the indexes open.
            server = f"http://127.0.0.1:{self.server.server_address[1]}"
            return start_ask(
                question,
                qs.get("dir", [None])[0],
                qs.get("scope", [""])[0] == "all",
                qs.get("session", [""])[0],  # continue that conversation
                server,
                # The knowledge base is slow to search, so it is only used on request.
                qs.get("notes", ["0"])[0] in ("1", "true", "on"),
            )

        def _start_index(self, qs: dict) -> dict:
            full = (qs.get("full", ["0"])[0]).lower() in ("1", "true", "on")
            return start_index((qs.get("dir", [""])[0]).strip(), full=full)

        def _set_schedule(self, qs: dict) -> dict:
            on = (qs.get("on", ["1"])[0]).lower() in ("1", "true", "on")
            at = (qs.get("at", ["03:00"])[0]).strip() or "03:00"
            return set_schedule((qs.get("dir", [""])[0]).strip(), on, at)

        def _open(self, qs: dict) -> dict:
            path = (qs.get("path", [""])[0]).strip()
            if not path:
                return {"ok": False, "error": "no path"}
            # Reveal only files inside a *known* indexed directory (guard against ../).
            real = os.path.realpath(path)
            roots = [os.path.realpath(str(e.config.target_dir)) for e in engines.values()]
            roots.append(os.path.realpath(default_dir))
            if not any(real == r or real.startswith(r + os.sep) for r in roots):
                return {"ok": False, "error": "path is outside an indexed directory"}
            if not os.path.exists(real):
                return {"ok": False, "error": "file not found"}
            try:
                _reveal_in_file_manager(real)
                return {"ok": True}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

        def _status(self, qs: dict) -> dict:
            d = (qs.get("dir", [None])[0])
            eng = engine_for(d)
            # The staleness check walks the whole folder (tens of seconds on a big
            # tree), so it's opt-in: the UI fetches it separately in the background.
            stale = (qs.get("stale", ["0"])[0]).lower() in ("1", "true", "on")
            info = eng.status(check_staleness=stale)
            info["dir"] = str(eng.config.target_dir)
            with state_lock:
                info["indexing"] = jobs.get(str(eng.config.target_dir)) == "running"
            return info

        def _engines(self, qs: dict) -> list[SearchEngine]:
            """The folder(s) a request targets: ``scope=all`` is every indexed folder,
            otherwise ``dir`` (or the server's default)."""
            if qs.get("scope", [""])[0] == "all":
                engines = all_engines()
                if engines:
                    return engines
            return [engine_for(qs.get("dir", [None])[0])]

        def _search(self, qs: dict) -> dict:
            q = (qs.get("q", [""])[0]).strip()
            if not q:
                return {"error": "empty query", "results": []}
            engines = self._engines(qs)
            mode = qs.get("mode", ["hybrid"])[0]
            try:
                k = max(1, min(50, int(qs.get("k", ["10"])[0])))
            except ValueError:
                k = 10
            prf = qs.get("prf", ["0"])[0] in ("1", "true", "on")
            # exclude=kb,dense: retrievers to leave out of a hybrid search.
            exclude = tuple(x for x in qs.get("exclude", [""])[0].split(",") if x)
            # type=pdf: only files of that kind (an id from FILE_TYPES; see /api/indexes).
            ftype = qs.get("type", [""])[0]
            if ftype and ftype not in FILE_TYPES:
                return {"error": f"unknown file type: {ftype}", "results": []}
            exts = FILE_TYPES[ftype][1] if ftype else ()
            try:
                if len(engines) == 1:
                    out = engines[0].search(
                        mode, q, k=k, prf=prf, exclude=exclude, exts=exts
                    )
                elif prf:
                    return {"error": "query expansion works on one folder at a time",
                            "results": []}
                else:
                    out = search_across(engines, mode, q, k=k, exclude=exclude, exts=exts)
                return {
                    "results": [r.to_dict() for r in out.results],
                    "used": out.used,
                    "skipped": out.skipped,
                }
            except ValueError:
                return {"error": f"unknown mode: {mode}", "results": []}
            except FileNotFoundError as exc:
                return {"error": str(exc), "results": []}
            except Exception as exc:  # surface backend errors to the page, don't crash
                return {"error": f"{type(exc).__name__}: {exc}", "results": []}

    return Handler


def _exit_when_parent_dies() -> None:
    """Terminate this server when the process that spawned it is gone.

    The macOS app bundle launches the server as a child and can't guarantee a kill on
    its way out (crash, SIGKILL, plain SIGTERM never reach the app's quit handler), so
    the server watches its parent instead: once reparented (getppid changes), exit.
    """
    import threading
    import time

    ppid = os.getppid()

    def poll() -> None:
        while True:
            if os.getppid() != ppid:
                os._exit(0)
            time.sleep(2.0)

    threading.Thread(target=poll, daemon=True).start()


def serve(
    directory: str | Path,
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    exit_with_parent: bool = False,
) -> None:
    directory = Path(directory).expanduser().resolve()
    if exit_with_parent:
        _exit_when_parent_dies()
    handler = _make_handler(directory)
    httpd = ThreadingHTTPServer((host, port), handler)
    url = f"http://{host}:{port}"
    print(f"[infogrep] web UI for {directory}", flush=True)
    print(f"[infogrep] open {url}  (Ctrl-C to stop)", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[infogrep] stopped.")
    finally:
        httpd.server_close()
