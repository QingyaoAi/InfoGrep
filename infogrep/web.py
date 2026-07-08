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

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>InfoGrep</title>
<style>
  :root { --bg:#0f1115; --card:#171a21; --fg:#e6e6e6; --mut:#8b93a7; --acc:#5b9dff; --bd:#262b36; }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--fg); font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
  header { padding:18px 24px; border-bottom:1px solid var(--bd); display:flex; align-items:baseline; gap:14px; }
  header h1 { margin:0; font-size:20px; letter-spacing:.5px; }
  header .status { color:var(--mut); font-size:13px; }
  main { max-width:920px; margin:0 auto; padding:24px; }
  form { display:flex; flex-wrap:wrap; gap:10px; align-items:center; }
  input[type=text]{ flex:1 1 360px; padding:11px 13px; background:var(--card); color:var(--fg);
    border:1px solid var(--bd); border-radius:8px; font-size:15px; }
  select, input[type=number]{ padding:10px; background:var(--card); color:var(--fg); border:1px solid var(--bd); border-radius:8px; }
  label.chk { color:var(--mut); display:flex; align-items:center; gap:6px; }
  button { padding:11px 18px; background:var(--acc); color:#04122b; border:0; border-radius:8px; font-weight:600; cursor:pointer; }
  button:disabled { opacity:.6; cursor:default; }
  .meta { color:var(--mut); font-size:13px; margin:16px 0 8px; min-height:18px; }
  .hit { background:var(--card); border:1px solid var(--bd); border-radius:10px; padding:13px 15px; margin:10px 0; }
  .hit.openable { cursor:pointer; }
  .hit.openable:hover { border-color:var(--acc); background:#1b2330; }
  .hit .top { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
  .hit .path { font-weight:600; color:#cfe0ff; word-break:break-all; }
  .hit.openable .path { text-decoration:underline dotted; }
  .hit .reveal { margin-left:auto; font-size:11px; color:var(--mut); white-space:nowrap; }
  .hit .score { color:var(--mut); font-variant-numeric:tabular-nums; }
  .badge { font-size:11px; padding:2px 8px; border-radius:20px; background:#1e2633; color:var(--acc); border:1px solid var(--bd); }
  .snip { margin-top:8px; color:#c7ccd6; white-space:pre-wrap; font-size:13.5px; }
  .err { color:#ff9a9a; }
</style>
</head>
<body>
<header>
  <h1>InfoGrep</h1>
  <span class="status" id="status">…</span>
</header>
<main>
  <form id="f">
    <input type="text" id="q" placeholder="Search…" autofocus autocomplete="off"/>
    <select id="dir" title="which indexed folder to search"></select>
    <button id="adddir" type="button" title="Index another folder">＋ folder</button>
    <label class="chk" title="Re-index this folder daily at 03:00 (incremental, macOS)">
      <input type="checkbox" id="sched"/> ⏰ daily</label>
    <select id="mode" title="retrieval mode">
      <option value="hybrid">hybrid</option>
      <option value="sparse">sparse</option>
      <option value="dense">dense</option>
      <option value="kb">kb</option>
      <option value="graph">graph</option>
    </select>
    <input type="number" id="k" value="10" min="1" max="50" title="number of results" style="width:64px"/>
    <label class="chk"><input type="checkbox" id="prf"/> PRF</label>
    <button id="go" type="submit">Search</button>
  </form>
  <div class="meta" id="meta"></div>
  <div id="results"></div>
</main>
<script>
const $ = id => document.getElementById(id);
let SCHEDULED = {};  // dir -> daily reindex on/off
function curDir(){ return $('dir').value || ''; }
function syncSched(){ $('sched').checked = !!SCHEDULED[curDir()]; }
async function loadIndexes(){
  try{
    const d = await (await fetch('/api/indexes')).json();
    const sel = $('dir'); const prev = sel.value || d.default;
    sel.innerHTML='';
    SCHEDULED = {};
    for(const i of d.indexes){
      SCHEDULED[i.dir] = !!i.scheduled;
      const o=document.createElement('option'); o.value=i.dir;
      o.textContent=i.name + (i.indexing?' (indexing…)':'') + (i.n_files!=null?'  ·  '+i.n_files+' files':'');
      sel.appendChild(o);
    }
    if([...sel.options].some(o=>o.value===prev)) sel.value=prev;
    syncSched();
  }catch(e){}
}
async function toggleSchedule(){
  const dir = curDir(); if(!dir){ $('sched').checked=false; return; }
  const on = $('sched').checked;
  try{
    const p = new URLSearchParams({dir, on: on?'1':'0'});
    const r = await (await fetch('/api/schedule?'+p, {method:'POST'})).json();
    if(!r.ok){ $('meta').textContent = 'Daily reindex: '+(r.error||'failed'); $('sched').checked = !on; return; }
    SCHEDULED[dir] = !!r.scheduled;
    $('meta').textContent = r.scheduled ? 'Daily reindex ON (03:00) — '+dir : 'Daily reindex off — '+dir;
  }catch(e){ $('meta').textContent = 'Daily reindex: '+e; $('sched').checked = !on; }
}
async function status(){
  try{ const s = await (await fetch('/api/status?dir='+encodeURIComponent(curDir()))).json();
    $('status').textContent = s.indexed ? `${s.dir} — ${s.n_files} files, ${s.n_passages} passages${s.indexing?' · indexing…':s.stale?' · STALE':''}` : `${s.dir} — not indexed`;
  }catch(e){ $('status').textContent = 'status unavailable'; }
}
async function addFolder(){
  const dir = prompt('Absolute path of a folder to index:');
  if(!dir) return;
  $('status').textContent = 'indexing '+dir+' …';
  try{ await fetch('/api/index?dir='+encodeURIComponent(dir), {method:'POST'}); }catch(e){}
  await loadIndexes(); $('dir').value=dir; status();
  // poll until it finishes
  const t=setInterval(async()=>{ await loadIndexes();
    const s=await (await fetch('/api/status?dir='+encodeURIComponent(dir))).json();
    if(s.indexed && !s.indexing){ clearInterval(t); status(); }
  }, 2000);
}
function el(tag, cls, text){ const e=document.createElement(tag); if(cls)e.className=cls; if(text!=null)e.textContent=text; return e; }
async function search(ev){
  ev.preventDefault();
  const q=$('q').value.trim(); if(!q) return;
  $('go').disabled=true; $('meta').textContent='Searching…'; $('results').innerHTML='';
  try{
    const p=new URLSearchParams({q, mode:$('mode').value, k:$('k').value, prf:$('prf').checked?'1':'0', dir:curDir()});
    const r=await (await fetch('/api/search?'+p)).json();
    if(r.error){ $('meta').innerHTML=''; $('meta').appendChild(el('span','err',r.error)); return; }
    const used=(r.used||[]).join(', ')||'—';
    let m=`${r.results.length} result(s) · retrievers: ${used}`;
    const sk=Object.entries(r.skipped||{}); if(sk.length) m+=' · skipped: '+sk.map(([k,v])=>k+' ('+v+')').join(', ');
    $('meta').textContent=m;
    for(const h of r.results){
      const card=el('div','hit'); const top=el('div','top');
      const ref=(h.abs_path||h.path)+(h.page!=null?(' · p.'+h.page):'');
      top.appendChild(el('span','path', ref));
      top.appendChild(el('span','score','['+Number(h.score).toFixed(3)+']'));
      top.appendChild(el('span','badge', h.retriever));
      if(h.ext) top.appendChild(el('span','badge', h.ext));
      if(h.abs_path){
        top.appendChild(el('span','reveal','📂 open folder'));
        card.classList.add('openable');
        card.title='Open this file’s folder in Finder';
        // Click anywhere on the result opens its folder (ignore text selection).
        card.addEventListener('click', ()=>{ if(!String(window.getSelection())) reveal(h.abs_path); });
      }
      card.appendChild(top); card.appendChild(el('div','snip', (h.snippet||'').trim()));
      $('results').appendChild(card);
    }
    if(!r.results.length) $('results').appendChild(el('div','meta','No matches.'));
  }catch(e){ $('meta').innerHTML=''; $('meta').appendChild(el('span','err',String(e))); }
  finally{ $('go').disabled=false; }
}
async function reveal(path){
  try{
    const res=await (await fetch('/api/open?path='+encodeURIComponent(path))).json();
    if(!res.ok){ $('meta').textContent='Could not open: '+(res.error||'error'); }
  }catch(e){ $('meta').textContent='Could not open: '+e; }
}
$('f').addEventListener('submit', search);
$('adddir').addEventListener('click', addFolder);
$('sched').addEventListener('change', toggleSchedule);
$('dir').addEventListener('change', ()=>{ status(); syncSched(); $('q').focus(); });
loadIndexes().then(status);
</script>
</body>
</html>"""


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

    def start_index(d: str) -> dict:
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
                Indexer(Config.load(key)).reindex()
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
                self._json({"indexes": list_indexes(), "default": default_dir})
            elif parsed.path == "/api/index":  # start (re)indexing a folder
                self._json(start_index((qs.get("dir", [""])[0]).strip()))
            elif parsed.path == "/api/schedule":  # toggle daily reindex for a folder
                self._json(self._set_schedule(qs))
            else:
                self._json({"error": "not found"}, code=404)

        # POST also accepts the actions that change state.
        def do_POST(self):
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            if parsed.path == "/api/index":
                self._json(start_index((qs.get("dir", [""])[0]).strip()))
            elif parsed.path == "/api/schedule":
                self._json(self._set_schedule(qs))
            else:
                self._json({"error": "not found"}, code=404)

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
            info = eng.status()
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


def serve(directory: str | Path, host: str = "127.0.0.1", port: int = DEFAULT_PORT) -> None:
    directory = Path(directory).expanduser().resolve()
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
