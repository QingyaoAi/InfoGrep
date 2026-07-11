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
from .engine import SearchEngine


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

# InfoGrep logo mark (magnifying glass over documents), inlined so the single-file
# server needs no static asset route. Regenerate from image/webite.png if the brand changes.
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
  .knum { display:flex; align-items:center; gap:6px; color:var(--mut); font-size:13.5px; }
  .knum input { width:60px; }

  /* results */
  .meta { color:var(--mut); font-size:13px; margin:14px 2px 8px; min-height:18px; }
  .meta .err { color:var(--danger); }
  .hit { background:var(--card); border:1px solid var(--bd); border-radius:12px;
    padding:12px 15px; margin:10px 0; transition:border-color .12s, transform .12s; }
  .hit.openable { cursor:pointer; }
  .hit.openable:hover { border-color:var(--acc); }
  .hit .top { display:flex; gap:9px; align-items:baseline; flex-wrap:wrap; }
  .hit .fname { font-weight:650; font-size:14.5px; }
  .hit .score { color:var(--mut); font-size:12px; font-variant-numeric:tabular-nums; }
  .badge { font-size:10.5px; padding:2px 8px; border-radius:20px; border:1px solid var(--bd);
    color:var(--acc); background:color-mix(in srgb, var(--acc) 10%, transparent); }
  .badge.ext { color:var(--mut); background:transparent; }
  .hit .reveal { margin-left:auto; font-size:11px; color:var(--mut); white-space:nowrap; }
  .hit .path { color:var(--mut); font-size:11.5px; margin-top:2px; word-break:break-all; }
  .snip { margin-top:7px; color:color-mix(in srgb, var(--fg) 82%, var(--mut)); white-space:pre-wrap;
    font-size:13.5px; overflow-wrap:anywhere; }
  mark { background:var(--mark); color:inherit; border-radius:3px; padding:0 1px; }
  .empty { text-align:center; color:var(--mut); padding:40px 0 20px; }
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
    </div>
    <div class="knum"><span>results</span><input type="number" id="k" value="10" min="1" max="50"/></div>
    <label class="chk" id="prfwrap" title="RM3 pseudo-relevance feedback: expand the query with terms from top results">
      <input type="checkbox" id="prf"/> query expansion</label>
  </div>

  <div class="meta" id="meta"></div>
  <div id="results"></div>
</main>
<footer>Local-first search — nothing leaves this machine. <a href="https://github.com/QingyaoAi/InfoGrep">GitHub</a></footer>

<script>
const $ = id => document.getElementById(id);
let INDEXES = [];          // /api/indexes payload entries
let POLL = null;           // status poll timer while indexing
function curDir(){ return $('dir').value || ''; }
function entry(dir){ return INDEXES.find(i => i.dir === dir); }
function mode(){ return document.querySelector('#modes input:checked').value; }

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
    INDEXES = d.indexes || [];
    const sel = $('dir'); const prev = keep || sel.value || d.default;
    sel.innerHTML = '';
    for (const i of INDEXES){
      const o = document.createElement('option'); o.value = i.dir;
      o.textContent = i.name + (i.indexing ? '  (indexing…)' : '');
      sel.appendChild(o);
    }
    if ([...sel.options].some(o => o.value === prev)) sel.value = prev;
    renderFolder();
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
  ev.preventDefault();
  const q = $('q').value.trim(); if (!q) return;
  $('go').disabled = true; note('<span class="spin"></span>searching…'); $('results').innerHTML = '';
  try{
    const p = new URLSearchParams({q, mode: mode(), k: $('k').value,
      prf: $('prf').checked ? '1' : '0', dir: curDir()});
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
      top.appendChild(el('span', 'score', '[' + Number(h.score).toFixed(3) + ']'));
      top.appendChild(el('span', 'badge', esc(h.retriever)));
      if (h.ext) top.appendChild(el('span', 'badge ext', esc(h.ext)));
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
function syncPrf(){ $('prfwrap').style.display = (mode() === 'hybrid' || mode() === 'sparse') ? '' : 'none'; }

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
$('dir').addEventListener('change', () => { renderFolder(); $('q').focus(); });
$('modes').addEventListener('change', syncPrf);
document.addEventListener('keydown', e => {
  if (e.key === '/' && document.activeElement !== $('q')){ e.preventDefault(); $('q').focus(); }
});
syncPrf();
loadIndexes();
</script>
</body>
</html>"""

PAGE = PAGE.replace("__ICON_DATA_URI__", _icon_data_uri())


def _make_handler(directory: Path):
    import threading

    from .config import index_home

    default_dir = str(Config.load(directory).target_dir)
    engines: dict[str, SearchEngine] = {}  # dir -> SearchEngine (warm searchers)
    jobs: dict[str, str] = {}  # dir -> "running" | "done" | "error: ..."
    state_lock = threading.Lock()

    def engine_for(d: str | None) -> SearchEngine:
        key = str(Path(d).expanduser().resolve()) if d else default_dir
        with state_lock:
            eng = engines.get(key)
            if eng is None:
                eng = engines[key] = SearchEngine(Config.load(key))
            return eng

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
                info = {
                    "dir": target,
                    "name": Path(target).name,
                    "scheduled": scheduler.is_scheduled(Path(target)),
                    "schedule_at": schedules.get(target),
                }
                try:  # fast: read the manifest, don't walk the filesystem
                    st = Indexer(Config.load(target)).status(check_staleness=False)
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

    class Handler(BaseHTTPRequestHandler):
        # Quiet by default (no per-request stderr logging).
        def log_message(self, *args):  # noqa: D401
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
            elif parsed.path == "/api/status":
                self._json(self._status(qs))
            elif parsed.path == "/api/search":
                self._json(self._search(qs))
            elif parsed.path == "/api/open":
                self._json(self._open(qs))
            elif parsed.path == "/api/indexes":
                indexes = list_indexes()
                self._json(
                    {"indexes": indexes, "default": effective_default(indexes),
                     "version": __version__}
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
            elif parsed.path == "/api/schedule":
                self._json(self._set_schedule(qs))
            elif parsed.path == "/api/forget":
                self._json(forget_index((qs.get("dir", [""])[0]).strip()))
            else:
                self._json({"error": "not found"}, code=404)

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

        def _search(self, qs: dict) -> dict:
            q = (qs.get("q", [""])[0]).strip()
            if not q:
                return {"error": "empty query", "results": []}
            eng = engine_for(qs.get("dir", [None])[0])
            mode = qs.get("mode", ["hybrid"])[0]
            try:
                k = max(1, min(50, int(qs.get("k", ["10"])[0])))
            except ValueError:
                k = 10
            prf = qs.get("prf", ["0"])[0] in ("1", "true", "on")
            try:
                out = eng.search(mode, q, k=k, prf=prf)
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
