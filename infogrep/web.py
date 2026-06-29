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
  .hit .top { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
  .hit .path { font-weight:600; color:#cfe0ff; word-break:break-all; }
  .hit .path.clickable { cursor:pointer; text-decoration:underline dotted; }
  .hit .path.clickable:hover { color:var(--acc); }
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
    <input type="text" id="q" placeholder="Search the indexed directory…" autofocus autocomplete="off"/>
    <select id="mode" title="retrieval mode">
      <option value="hybrid">hybrid</option>
      <option value="sparse">sparse</option>
      <option value="dense">dense</option>
      <option value="kb">kb</option>
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
async function status(){
  try{ const s = await (await fetch('/api/status')).json();
    $('status').textContent = s.indexed ? `${s.dir} — ${s.n_files} files, ${s.n_passages} passages${s.stale?' · STALE':''}` : `${s.dir} — not indexed`;
  }catch(e){ $('status').textContent = 'status unavailable'; }
}
function el(tag, cls, text){ const e=document.createElement(tag); if(cls)e.className=cls; if(text!=null)e.textContent=text; return e; }
async function search(ev){
  ev.preventDefault();
  const q=$('q').value.trim(); if(!q) return;
  $('go').disabled=true; $('meta').textContent='Searching…'; $('results').innerHTML='';
  try{
    const p=new URLSearchParams({q, mode:$('mode').value, k:$('k').value, prf:$('prf').checked?'1':'0'});
    const r=await (await fetch('/api/search?'+p)).json();
    if(r.error){ $('meta').innerHTML=''; $('meta').appendChild(el('span','err',r.error)); return; }
    const used=(r.used||[]).join(', ')||'—';
    let m=`${r.results.length} result(s) · retrievers: ${used}`;
    const sk=Object.entries(r.skipped||{}); if(sk.length) m+=' · skipped: '+sk.map(([k,v])=>k+' ('+v+')').join(', ');
    $('meta').textContent=m;
    for(const h of r.results){
      const card=el('div','hit'); const top=el('div','top');
      const ref=(h.abs_path||h.path)+(h.page!=null?(' · p.'+h.page):'');
      const pathEl=el('span','path', ref);
      if(h.abs_path){
        pathEl.classList.add('clickable');
        pathEl.title='Click to reveal in the file manager';
        pathEl.addEventListener('click', ()=>reveal(h.abs_path));
      }
      top.appendChild(pathEl);
      top.appendChild(el('span','score','['+Number(h.score).toFixed(3)+']'));
      top.appendChild(el('span','badge', h.retriever));
      if(h.ext) top.appendChild(el('span','badge', h.ext));
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
status();
</script>
</body>
</html>"""


def _make_handler(directory: Path):
    engine = SearchEngine(Config.load(directory))  # one engine -> warm searchers

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
            if parsed.path in ("/", "/index.html"):
                self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            elif parsed.path == "/api/status":
                self._json(self._status())
            elif parsed.path == "/api/search":
                self._json(self._search(parse_qs(parsed.query)))
            elif parsed.path == "/api/open":
                self._json(self._open(parse_qs(parsed.query)))
            else:
                self._json({"error": "not found"}, code=404)

        def _open(self, qs: dict) -> dict:
            path = (qs.get("path", [""])[0]).strip()
            if not path:
                return {"ok": False, "error": "no path"}
            # Only reveal files inside the indexed directory (guards against ../ escapes).
            root = os.path.realpath(str(engine.config.target_dir))
            real = os.path.realpath(path)
            if real != root and not real.startswith(root + os.sep):
                return {"ok": False, "error": "path is outside the indexed directory"}
            if not os.path.exists(real):
                return {"ok": False, "error": "file not found"}
            try:
                _reveal_in_file_manager(real)
                return {"ok": True}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

        def _status(self) -> dict:
            info = engine.status()
            info["dir"] = str(engine.config.target_dir)
            return info

        def _search(self, qs: dict) -> dict:
            q = (qs.get("q", [""])[0]).strip()
            if not q:
                return {"error": "empty query", "results": []}
            mode = qs.get("mode", ["hybrid"])[0]
            try:
                k = max(1, min(50, int(qs.get("k", ["10"])[0])))
            except ValueError:
                k = 10
            prf = qs.get("prf", ["0"])[0] in ("1", "true", "on")
            try:
                if mode == "hybrid":
                    out = engine.search_hybrid(q, k=k, prf=prf)
                    return {
                        "results": [r.to_dict() for r in out.results],
                        "used": out.used,
                        "skipped": out.skipped,
                    }
                if mode == "sparse":
                    hits = engine.search_sparse(q, k=k, prf=prf)
                elif mode == "dense":
                    hits = engine.search_dense(q, k=k)
                elif mode == "kb":
                    hits = engine.search_kb(q, k=k)
                else:
                    return {"error": f"unknown mode: {mode}", "results": []}
                return {"results": [r.to_dict() for r in hits], "used": [mode], "skipped": {}}
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
