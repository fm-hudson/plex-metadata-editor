#!/usr/bin/python3
"""
plex_metadata_reviewer.py

A hybrid UI built on the discovery logic of plex_mkv_metadata_batch.py
and the visual comparison engine of plex_metadata_editor.py.

It allows you to review Plex metadata updates before they are written to
the file, with a 3-second Auto-Mode for rapid processing.
"""

import sys
import os
import json
import time
import threading
import requests
import xml.etree.ElementTree as ET
from urllib.parse import parse_qs
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_VERSION = "v1.0.0"
PROCESS_TITLE = "plex metadata web reviewer (port 8766)"

try:
    import setproctitle
    setproctitle.setproctitle(PROCESS_TITLE)
    if hasattr(setproctitle, "setthreadtitle"):
        setproctitle.setthreadtitle("plex-web-rev")
except ImportError:
    pass

# Add current directory (and ~/myScripts fallback) to path to reuse the tagging logic
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.append(os.path.expanduser('~/myScripts'))
try:
    import media_tag_io as io
    import plex_metadata_lib as lib
    from plex_mkv_metadata_batch import discover_matching_items, SECTION_ITEM_TYPES
except ImportError as e:
    print(f"Error importing required modules: {e}")
    sys.exit(1)

class Plex:
    def __init__(self, url, token):
        self.url = (url or "").rstrip("/")
        self.token = token or ""
    def get(self, path, params=None):
        params = dict(params or {})
        params["X-Plex-Token"] = self.token
        r = requests.get(self.url + path, params=params, timeout=60)
        r.raise_for_status()
        return r
    def xml(self, path, params=None):
        return ET.fromstring(self.get(path, params).content)
    def item(self, rating_key):
        root = self.xml(f"/library/metadata/{rating_key}")
        video = root.find("Video")
        if video is None:
            raise RuntimeError(f"No Video element for ratingKey {rating_key}")
        return video

class _ProgressHooks:
    def __init__(self):
        self.lock = threading.Lock()
        self.progress = None
    
    def begin(self, name):
        with self.lock:
            self.progress = {
                "name": name, 
                "steps": [{"name": s, "state": "pending"} for s in ["Safety copy", "Check original", "Write tags", "Verify", "Restore original", "Clean up"]],
                "pct": None, "bytes_done": 0, "bytes_total": 0
            }
            
    def step(self, name):
        with self.lock:
            if not self.progress: return
            p = self.progress
            for s in p["steps"]:
                if s["state"] == "active": s["state"] = "done"
            match = [s for s in p["steps"] if s["name"] == name]
            if match: match[0]["state"] = "active"
            else:
                pos = next((k for k, s in enumerate(p["steps"]) if s["state"] == "pending"), len(p["steps"]))
                p["steps"].insert(pos, {"name": name, "state": "active"})
            p.update(pct=None, bytes_done=0, bytes_total=0)

    def fail(self):
        with self.lock:
            if not self.progress: return
            for s in self.progress["steps"]:
                if s["state"] == "active": s["state"] = "failed"

    def skip_pending(self, names):
        with self.lock:
            if not self.progress: return
            for s in self.progress["steps"]:
                if s["state"] == "pending" and s["name"] in names:
                    s["state"] = "skipped"

    def pct(self, pct):
        with self.lock:
            if self.progress: self.progress["pct"] = pct

    def bytes(self, done, total):
        with self.lock:
            if self.progress:
                self.progress["bytes_done"] = done
                self.progress["bytes_total"] = total
                self.progress["pct"] = (done/total*100) if total else 0

PROGRESS_HOOKS = _ProgressHooks()

# Global State
STATE = {
    "queue": [],
    "history": [],
    "total_found": 0,
    "current_item": None, # (ratingKey, file_path)
    "settings": {
        "directory": "",
        "plex_url": "http://127.0.0.1:32400",
        "plex_token": "",
        "backup_dir": ""
    },
    "last_heartbeat": time.time()
}

lock = threading.Lock()


def load_settings():
    path = os.path.expanduser('~/scripts_meta_data/plex_metadata_editor_settings.json')
    if os.path.exists(path):
        try:
            with open(path) as f:
                s = json.load(f)
                STATE["settings"].update({k: v for k, v in s.items() if k in STATE["settings"]})
        except Exception as e:
            print(f"Failed to load settings: {e}")

load_settings()

PAGE = r"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Plex Metadata Reviewer</title>
<style>
*{box-sizing:border-box}
body{margin:0;font:14px system-ui,sans-serif;background:#1e1f22;color:#ddd;display:flex;flex-direction:column;height:100vh;}
header{display:flex;gap:12px;align-items:center;padding:12px 16px;background:#2b2d31;border-bottom:1px solid #111;}
header .grow{flex:1;min-width:0}
button{background:#3a3d44;color:#eee;border:1px solid #555;border-radius:4px;padding:8px 16px;cursor:pointer;font-weight:bold;}
button:hover{background:#4a4e57}
button.primary{background:#e5a00d;color:#111;border-color:#e5a00d;}
button.skip{background:#5c626b;color:#fff;border-color:#4a4e57;}
button:disabled{opacity:.5;cursor:default}
input[type=text],input[type=password]{background:#15161a;color:#ddd;border:1px solid #444;border-radius:4px;padding:5px;font:inherit}
textarea{width:100%;resize:vertical;background:#15161a;color:#ddd;border:1px solid #444;border-radius:4px;padding:5px;}
main{display:flex;gap:20px;padding:20px;flex:1;overflow:auto;}
#left{width:320px;flex-shrink:0;display:flex;flex-direction:column;gap:10px;}
#left img{width:100%;border-radius:6px;background:#111;min-height:480px;display:block;object-fit:cover;}
#right{flex:1;min-width:0}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:12px 16px}
label{display:block;font-size:12px;color:#aaa;margin-bottom:2px}
label input,label textarea{width:100%;display:block;margin-top:2px}
.span2{grid-column:span 2}.spanall{grid-column:1/-1}
fieldset{border:1px solid #3a3d44;border-radius:6px;margin:15px 0;padding:12px 16px}
legend{color:#e5a00d;padding:0 6px;font-weight:bold;}
.muted{color:#888;font-size:12px}.warn{color:#f0a040}.ok{color:#5c5}.bad{color:#f55}
#status{font-size:14px;padding:12px 16px;background:#25272c;border:1px solid #3a3d44;border-radius:6px;margin:10px 20px;}
#status:empty{display:none}

.pstep{margin:3px 0;color:#888}.pstep .pi{display:inline-block;width:18px}
.ps-done{color:#5c5}.ps-active{color:#eee;font-weight:600}.ps-failed{color:#f55}.ps-skipped{color:#666}
.pbar{height:10px;background:#15161a;border:1px solid #444;border-radius:5px;overflow:hidden;margin:4px 0 2px 18px;max-width:560px}
.pbar>div{height:100%;background:#e5a00d;transition:width .4s}
.pbar.ind>div{width:30%;animation:pind 1.2s ease-in-out infinite alternate}
@keyframes pind{from{margin-left:0}to{margin-left:70%}}
.ps-active .muted{margin-left:18px;font-weight:400}


.mk{display:inline-block;font-size:10px;font-weight:700;border-radius:8px;padding:0 6px;margin-left:6px;cursor:help;vertical-align:1px}
.mk-new{background:#2e7d32;color:#fff}.mk-chg{background:#b26a00;color:#fff}.mk-rem{background:#8e2b2b;color:#fff}
.is-new{border-color:#3fa046 !important;box-shadow:0 0 0 1px #3fa046}.is-chg{border-color:#d08a1a !important;box-shadow:0 0 0 1px #d08a1a}

.auto-toggle { display:flex; align-items:center; gap:6px; background:#15161a; padding:6px 12px; border-radius:4px; border:1px solid #444; }
.auto-toggle input { width:16px; height:16px; cursor:pointer; }
.progress-bar-container { height: 4px; background: #222; border-radius: 2px; overflow: hidden; margin-top: 4px; width: 100%; display: none; }
.progress-bar { height: 100%; background: #e5a00d; width: 0%; transition: width 0.1s linear; }

.modal{position:fixed;inset:0;background:rgba(0,0,0,.6);display:none;align-items:flex-start;justify-content:center;z-index:10;padding-top:40px;}
.modal.show{display:flex}
#setup-panel { padding: 20px; background: #2b2d31; border-radius: 8px; width: 100%; max-width: 600px; box-shadow: 0 10px 25px rgba(0,0,0,0.5); }
#setup-panel .row { margin-bottom: 15px; }
#setup-panel label { font-size: 14px; margin-bottom: 5px; }

</style></head><body>

<div class="modal show" id="setupModal">
<div id="setup-panel">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:15px;">
        <h2 style="margin:0;color:#e5a00d;">Settings & Scan <span style="color:#888;font-size:14px;font-weight:normal;">(v1.0.0)</span></h2>
        <button onclick="closeSettings()" id="closeSettingsBtn" style="display:none;">✕</button>
    </div>
    <div class="row">
        <label>Folder to Scan</label>
        <input type="text" id="s_dir" style="width:100%" placeholder="/path/to/media/movies">
    </div>
    <div class="row">
        <label>Plex URL</label>
        <input type="text" id="s_url" style="width:100%">
    </div>
    <div class="row">
        <label>Plex Token</label>
        <input type="password" id="s_token" style="width:100%">
    </div>
    <button class="primary" onclick="startDiscovery()" style="width:100%">Start Scan</button>
    <div id="setup-msg" style="margin-top:15px;" class="muted"></div>
</div>
</div>

<div id="app" style="display:none; flex-direction:column; height:100%;">
    <header>
      <div style="display:flex;flex-direction:column;align-items:center;gap:4px;margin-right:15px;flex-shrink:0;">
          <button onclick="openSettings()" title="Settings">⚙️ Settings</button>
          <span style="color:#e5a00d;background:#15161a;border:1px solid #3a3d44;border-radius:4px;font-size:11px;font-weight:bold;padding:1px 8px;">v1.0.0</span>
      </div>
      <div style="display:flex; flex-direction:column; max-width:60%;">
          <div id="queue-counter" style="font-weight:bold;font-size:16px;">Queue: 0 items</div>
          <div id="file-path" class="muted" style="margin-top:4px;font-family:monospace;font-size:14px;word-break:break-all;"></div>
      </div>
      <span class="grow"></span>
      
      <div class="auto-toggle" title="Automatically approve and proceed after 3 seconds" style="flex-shrink:0;">
          <input type="checkbox" id="auto-mode" onchange="toggleAutoMode()">
          <label for="auto-mode" style="margin:0;font-size:14px;color:#eee;cursor:pointer;">Auto-Mode</label>
      </div>

      <div style="display:flex; flex-direction:column; align-items:center; flex-shrink:0;">
          <div style="display:flex; gap:6px;">
              <button class="skip" onclick="prevItem()" id="prevBtn">Prev</button>
              <button class="skip" onclick="skipItem()" id="skipBtn">Skip</button>
              <button class="primary" onclick="saveItem()" id="saveBtn">OK & Next</button>
          </div>
          <div class="progress-bar-container" id="auto-progress-cont"><div class="progress-bar" id="auto-progress"></div></div>
      </div>
    </header>
    <div id="status"></div>
    <main id="main">
      <div id="left">
        <img id="poster" src="" onerror="this.src='data:image/svg+xml,%3Csvg xmlns=\'http://www.w3.org/2000/svg\'/%3E'">
      </div>
      <div id="right">
        <!-- Metadata fields injected here by JS -->
      </div>
    </main>
</div>

<script>
const $ = id => document.getElementById(id);
const esc = s => (s==null?'':String(s)).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');

let currentRecord = null;
let currentMergedTags = null;
let autoTimer = null;
let timerMs = 0;
const AUTO_DELAY_MS = 3000;

async function api(path, payload){
  const r = await fetch(path, {method:payload?'POST':'GET', headers:payload?{'Content-Type':'application/json'}:{}, body:payload?JSON.stringify(payload):null});
  if(!r.ok) throw new Error(r.statusText);
  return r.json();
}

async function loadInitialSettings() {
    const s = await api('/api/settings');
    $('s_dir').value = s.directory;
    $('s_url').value = s.plex_url;
    $('s_token').value = s.plex_token;
}


function openSettings() {
    $('setupModal').classList.add('show');
    $('closeSettingsBtn').style.display = 'block';
    $('setup-msg').textContent = '';
}
function closeSettings() {
    $('setupModal').classList.remove('show');
}
async function startDiscovery() {
    $('setup-msg').innerHTML = '<span class="muted">Querying Plex and scanning folder... this may take a minute.</span>';
    try {
        const payload = {
            directory: $('s_dir').value,
            plex_url: $('s_url').value,
            plex_token: $('s_token').value
        };
        const res = await api('/api/discover', payload);
        if(res.error) throw new Error(res.error);
        $('setupModal').classList.remove('show');
        $('app').style.display = 'flex';
        loadNext();
    } catch(e) {
        $('setup-msg').innerHTML = `<span class="bad">${esc(e.message)}</span>`;
    }
}

function setStatus(html){ $('status').innerHTML = html; }

async function prevItem() {
    clearAutoTimer();
    $('main').style.opacity = '0.5';
    $('saveBtn').disabled = true;
    $('skipBtn').disabled = true;
    $('prevBtn').disabled = true;
    try {
        const res = await api('/api/prev');
        if(res.error) throw new Error(res.error);
        if(res.empty) {
            setStatus(`<span class="muted">Already at the beginning of the queue</span>`);
            $('main').style.opacity = '1';
            $('saveBtn').disabled = false;
            $('skipBtn').disabled = false;
            $('prevBtn').disabled = false;
            return;
        }
        loadNextData(res);
    } catch(e) {
        setStatus(`<span class="bad">Error loading prev: ${esc(e.message)}</span>`);
        $('main').style.opacity = '1';
    }
}

async function loadNext() {
    clearAutoTimer();
    $('main').style.opacity = '0.5';
    $('saveBtn').disabled = true;
    $('skipBtn').disabled = true;
    $('prevBtn').disabled = true;
    try {
        const res = await api('/api/next');
        loadNextData(res);
    } catch(e) {
        setStatus(`<span class="bad">Error loading next: ${esc(e.message)}</span>`);
    }
}

function fmtSize(b){ return b>=1073741824 ? (b/1073741824).toFixed(2)+' GB' : b>=1048576 ? (b/1048576).toFixed(0)+' MB' : Math.ceil(b/1024)+' KB'; }

function loadNextData(res) {
    $('main').style.opacity = '1';
    if(res.done) {
        $('main').innerHTML = `
          <div id="left">
            <img id="poster" src="data:image/svg+xml,%3Csvg xmlns=\\'http://www.w3.org/2000/svg\\'/%3E">
          </div>
          <div id="right">
            <div style="padding:40px;text-align:center;font-size:20px;width:100%">Queue Complete! 🎉</div>
          </div>
        `;
        $('queue-counter').textContent = 'Queue: 0 items left';
        $('file-path').textContent = '';
        $('status').style.display = 'none';
        $('prevBtn').disabled = false;
        $('saveBtn').disabled = true;
        $('skipBtn').disabled = true;
        return;
    }
    
    // Ensure layout is restored if we came back from Queue Complete
    if(!$('right')) {
        $('main').innerHTML = `
          <div id="left">
            <img id="poster" src="" onerror="this.src='data:image/svg+xml,%3Csvg xmlns=\\'http://www.w3.org/2000/svg\\'/%3E'">
          </div>
          <div id="right"></div>
        `;
    }
    
    currentRecord = res;
    $('queue-counter').textContent = `Queue: ${res.remaining} left`;
    $('file-path').innerHTML = `${esc(res.path)} <span style="margin-left:10px;color:#aaa;">(${fmtSize(res.size)})</span>`;
    
    // Merge diffs
    const tags = Object.assign({}, res.file_tags);
    const f = res.file_tags; const p = res.plex_tags;
    const mk = {};
    for(const k in p) {
        if(p[k] !== undefined && p[k] !== null && p[k] !== '') {
            tags[k] = p[k];
            if(f[k] === undefined || f[k] === null || f[k] === '') mk[k] = ['new', 'New from Plex'];
            else if(JSON.stringify(f[k]) !== JSON.stringify(p[k])) mk[k] = ['chg', 'Was: '+JSON.stringify(f[k])];
        }
    }
    for(const k in f) {
        if(f[k] && (!p[k] || p[k].length===0) && tags[k]===undefined) {
            // Not removed in batch mode, we preserve existing if Plex doesn't have it
            tags[k] = f[k];
        }
    }
    
    currentMergedTags = tags;

    renderForm(tags, mk);
    $('poster').src = res.plex_poster_url || '';
    
    $('saveBtn').disabled = false;
    $('skipBtn').disabled = false;
    $('prevBtn').disabled = false;
    
    if($('auto-mode').checked) startAutoTimer();
}

function renderForm(t, mk) {
    const wrap = (k, lbl, inp) => {
        let h = `<label>${lbl}`;
        if(mk[k]) h += ` <span class="mk mk-${mk[k][0]}" title="${esc(mk[k][1])}">${mk[k][0].toUpperCase()}</span>`;
        h += `</label>${inp}`;
        return `<div>${h}</div>`;
    };
    const cls = k => mk[k] ? ` class="is-${mk[k][0]}"` : '';
    const text = (k, l) => wrap(k, l, `<input type=text readonly value="${esc(t[k])}"${cls(k)}>`);
    const date = (k, l) => wrap(k, l, `<input type=date readonly value="${esc(t[k])}"${cls(k)}>`);
    const num = (k, l) => wrap(k, l, `<input type=number readonly value="${esc(t[k])}"${cls(k)}>`);
    const area = (k, l) => wrap(k, l, `<textarea readonly rows=3${cls(k)}>${esc(t[k])}</textarea>`);
    const list = (k, l) => wrap(k, l, `<input type=text readonly value="${esc((t[k]||[]).join(', '))}"${cls(k)}>`);
    const dicts = (k, l) => wrap(k, l, `<input type=text readonly value="${esc((t[k]||[]).map(x=>x.actor?`${x.actor} (${x.character})`:x.name).join(', '))}"${cls(k)}>`);
    const extra = (k, l) => wrap(k, l, `<textarea readonly rows=3${cls(k)}>${esc((t[k]||[]).map(x=>`${x.name}: ${x.value}`).join('\n'))}</textarea>`);

    $('right').innerHTML = `
        <fieldset><legend>Title & Summary</legend>
          <div class="grid">
            ${text('title', 'Title')}
            ${text('sort_title', 'Sort Title')}
            ${text('original_title', 'Original Title')}
            <div class="spanall">${area('summary', 'Summary')}</div>
            <div class="spanall">${text('tagline', 'Tagline')}</div>
          </div>
        </fieldset>
        <fieldset><legend>Release & Studio</legend>
          <div class="grid">
            ${date('release_date', 'Release Date')}
            ${num('year', 'Year')}
            ${text('studio', 'Studio')}
            ${text('content_rating', 'Content Rating')}
            ${text('imdb', 'IMDb ID')}
            ${text('tmdb', 'TMDb ID')}
            ${text('tvdb', 'TVDb ID')}
          </div>
        </fieldset>
        <fieldset><legend>Classification</legend>
          <div class="grid">
            <div class="spanall">${list('genres', 'Genres')}</div>
            <div class="spanall">${list('collections', 'Collections')}</div>
            <div class="spanall">${list('countries', 'Countries')}</div>
          </div>
        </fieldset>
        <fieldset><legend>TV Show</legend>
          <div class="grid">
            ${text('show_title', 'Show Title')}
            ${text('sort_show', 'Sort Show')}
            ${num('season', 'Season')}
            ${num('episode', 'Episode')}
            ${text('episode_id', 'Episode ID (e.g. S01E02)')}
          </div>
        </fieldset>
        <fieldset><legend>People</legend>
          <div class="grid">
            <div class="spanall">${list('directors', 'Directors')}</div>
            <div class="spanall">${list('writers', 'Writers')}</div>
            <div class="spanall">${list('producers', 'Producers')}</div>
            <div class="spanall">${dicts('roles', 'Cast (Actor - Character)')}</div>
          </div>
        </fieldset>
        <fieldset><legend>Other File Tags</legend>
          <div class="grid">
            <div class="spanall">${extra('extra_tags', 'Global MKV Tags')}</div>
          </div>
        </fieldset>
    `;
}

function toggleAutoMode() {
    if($('auto-mode').checked) {
        if(currentRecord && !$('saveBtn').disabled) startAutoTimer();
    } else {
        clearAutoTimer();
    }
}

function startAutoTimer() {
    clearAutoTimer();
    timerMs = 0;
    $('auto-progress-cont').style.display = 'block';
    $('auto-progress').style.width = '0%';
    autoTimer = setInterval(() => {
        timerMs += 100;
        $('auto-progress').style.width = `${(timerMs/AUTO_DELAY_MS)*100}%`;
        if(timerMs >= AUTO_DELAY_MS) {
            clearAutoTimer();
            saveItem();
        }
    }, 100);
}

function clearAutoTimer() {
    if(autoTimer) clearInterval(autoTimer);
    autoTimer = null;
    $('auto-progress-cont').style.display = 'none';
}

async function skipItem() {
    clearAutoTimer();
    setStatus(`<span class="muted">Skipped ${esc(currentRecord.path)}</span>`);
    try {
        await api('/api/skip', { ratingKey: currentRecord.ratingKey, path: currentRecord.path });
    } catch(e) {}
    loadNext();
}


const sleep = ms => new Promise(r => setTimeout(r, ms));
function renderProgress(p){
  if(!p || !p.steps) return;
  const icon = {done:'✔', active:'▶', failed:'✘', skipped:'–', pending:'·'};
  const rows = p.steps.map(s => {
    let extra = '';
    if(s.state === 'active'){
      if(p.pct !== null){
        const det = p.bytes_total ? ` · ${fmtSize(p.bytes_done)} of ${fmtSize(p.bytes_total)}` : '';
        extra = `<div class="pbar"><div style="width:${p.pct.toFixed(1)}%"></div></div><div class="muted">${p.pct.toFixed(0)}%${det}</div>`;
      } else {
        extra = `<div class="pbar ind"><div></div></div>`;
      }
    }
    return `<div class="pstep ps-${s.state}"><span class="pi">${icon[s.state]||''}</span>${esc(s.name)}${extra}</div>`;
  }).join('');
  $('status').innerHTML = `<b>Saving ${esc(p.name)}</b><br>${rows}`;
}

async function saveItem() {
    clearAutoTimer();
    $('saveBtn').disabled = true;
    $('skipBtn').disabled = true;
    $('prevBtn').disabled = true;
    setStatus(`<span class="muted">Starting save...</span>`);
    
    try {
        const payload = {
            ratingKey: currentRecord.ratingKey,
            path: currentRecord.path,
            tags: currentMergedTags
        };
        const start = await api('/api/save', payload);
        if(!start.ok) {
            setStatus(`<span class="bad">Save failed to start: ${esc(start.error)}</span>`);
            $('saveBtn').disabled = false; $('skipBtn').disabled = false; $('prevBtn').disabled = false;
            return;
        }
        
        let result = null;
        while(!result){
            await sleep(500);
            const st = await api('/api/save_status');
            if(st && st.result !== undefined) {
                result = st.result;
            } else {
                renderProgress(st);
            }
        }
        
        if(result.ok) {
            setStatus(`<span class="ok">Saved successfully: ${esc(currentRecord.path)}</span>`);
            loadNext();
        } else {
            setStatus(`<span class="bad">Save failed: ${esc(result.error || result.restored_text || "Unknown error")}</span>`);
            $('saveBtn').disabled = false; $('skipBtn').disabled = false; $('prevBtn').disabled = false;
        }
    } catch(e) {
        setStatus(`<span class="bad">Save exception: ${esc(e.message)}</span>`);
        $('saveBtn').disabled = false; $('skipBtn').disabled = false; $('prevBtn').disabled = false;
    }
}

window.onload = () => {
    loadInitialSettings();
    setInterval(() => fetch('/api/ping').catch(()=>{}), 5000);
};
</script>
</body></html>
"""

class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def send_json(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode("utf-8"))
        
    def do_GET(self):
        if self.path == "/":
            self.send_response(200)
            self.send_header("Content-type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(PAGE.encode("utf-8"))
        elif self.path == "/api/ping":
            STATE["last_heartbeat"] = time.time()
            self.send_json({"ok": True})
        elif self.path == "/api/save_status":
            with PROGRESS_HOOKS.lock:
                self.send_json(PROGRESS_HOOKS.progress or {})
        elif self.path == "/api/settings":
            self.send_json(STATE["settings"])
        elif self.path == "/api/prev":
            with lock:
                if not STATE["history"]:
                    self.send_json({"empty": True})
                    return
                # Put current item back on front of queue
                if STATE["current_item"]:
                    STATE["queue"].insert(0, STATE["current_item"])
                
                # Pop history and make it current
                rk, path = STATE["history"].pop()
                STATE["current_item"] = (rk, path)
                remaining = len(STATE["queue"])

            try:
                # Get plex metadata
                plex_item = Plex(STATE["settings"]["plex_url"], STATE["settings"]["plex_token"]).item(rk)
                plex_tags = io.parse_plex(plex_item)
                poster_url = None
                if plex_item.get("thumb"):
                    poster_url = f"{STATE['settings']['plex_url']}{plex_item.get('thumb')}?X-Plex-Token={STATE['settings']['plex_token']}"

                # Get file metadata
                if path.lower().endswith('.mkv'):
                    file_tags, _ = io.read_mkv(path, "/tmp/plex_rev")
                else:
                    file_tags, _ = io.read_mp4(path)

                self.send_json({
                    "done": False,
                    "remaining": remaining,
                    "ratingKey": rk,
                    "path": path,
                    "size": os.path.getsize(path),
                    "plex_tags": plex_tags,
                    "file_tags": file_tags,
                    "plex_poster_url": poster_url
                })
            except Exception as e:
                import traceback
                traceback.print_exc()
                self.send_json({"error": str(e)}, 500)

        elif self.path == "/api/next":
            with lock:
                if not STATE["queue"]:
                    self.send_json({"done": True})
                    return
                
                # Move current to history (if it wasn't a skip/save which clear it explicitly via POST)
                # Actually, skip/save don't do that yet. Let's just trust POSTs to maintain history.
                # When we load next organically (startup), we just pop. 
                rk, path = STATE["queue"].pop(0)
                STATE["current_item"] = (rk, path)
                remaining = len(STATE["queue"])
            
            try:
                # Get plex metadata
                plex_item = Plex(STATE["settings"]["plex_url"], STATE["settings"]["plex_token"]).item(rk)
                plex_tags = io.parse_plex(plex_item)
                poster_url = None
                if plex_item.get("thumb"):
                    poster_url = f"{STATE['settings']['plex_url']}{plex_item.get('thumb')}?X-Plex-Token={STATE['settings']['plex_token']}"

                # Get file metadata
                if path.lower().endswith('.mkv'):
                    file_tags, _ = io.read_mkv(path, "/tmp/plex_rev")
                else:
                    file_tags, _ = io.read_mp4(path)

                self.send_json({
                    "done": False,
                    "remaining": remaining,
                    "ratingKey": rk,
                    "path": path,
                    "size": os.path.getsize(path),
                    "plex_tags": plex_tags,
                    "file_tags": file_tags,
                    "plex_poster_url": poster_url
                })
            except Exception as e:
                import traceback
                traceback.print_exc()
                self.send_json({"error": str(e)}, 500)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(content_length)) if content_length else {}

        if self.path == "/api/discover":
            # Save settings to memory
            STATE["settings"]["directory"] = body.get("directory", "")
            STATE["settings"]["plex_url"] = body.get("plex_url", "")
            STATE["settings"]["plex_token"] = body.get("plex_token", "")
            
            # Save settings back to JSON file so they persist
            settings_path = os.path.expanduser('~/scripts_meta_data/plex_metadata_editor_settings.json')
            try:
                os.makedirs(os.path.dirname(settings_path), exist_ok=True)
                with lock:
                    # read existing first so we don't overwrite other fields (like backup_dir) if they exist
                    existing = {}
                    if os.path.exists(settings_path):
                        with open(settings_path, 'r') as f:
                            existing = json.load(f)
                    existing.update(STATE["settings"])
                    with open(settings_path, 'w') as f:
                        json.dump(existing, f, indent=2)
            except Exception as e:
                print(f"Warning: could not save settings to disk: {e}")
            
            try:
                matches = discover_matching_items(STATE["settings"]["plex_url"], STATE["settings"]["plex_token"], STATE["settings"]["directory"])
                
                # Sort alphabetically by path so order is deterministic
                matches.sort(key=lambda x: x[1])
                
                # Filter out ones already done by batch or UI
                filtered = []
                for rk, fpath in matches:
                    sent = io.read_sentinel(fpath)
                    if sent not in io.SKIP_VALUES:
                        filtered.append((rk, fpath))
                
                with lock:
                    STATE["queue"] = filtered
                    STATE["history"] = []
                    STATE["current_item"] = None
                    STATE["total_found"] = len(filtered)
                    
                self.send_json({"ok": True, "count": len(filtered)})
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
                
        elif self.path == "/api/skip":
            rk = body.get("ratingKey")
            fpath = body.get("path")
            with lock:
                STATE["history"].append((rk, fpath))
                STATE["current_item"] = None
            self.send_json({"ok": True})
                
        elif self.path == "/api/save":
            rk = body.get("ratingKey")
            fpath = body.get("path")
            merged_tags = body.get("tags")
            
            with lock:
                STATE["history"].append((rk, fpath))
                STATE["current_item"] = None
                
            PROGRESS_HOOKS.begin(os.path.basename(fpath))
            
            def save_thread(state_settings):
                try:
                    plex = Plex(state_settings["plex_url"], state_settings["plex_token"])
                    plex_item = plex.item(rk)
                    poster_bytes = None
                    thumb = plex_item.get("thumb")
                    cover_path = None
                    if thumb:
                        import requests
                        resp = requests.get(f"{state_settings['plex_url']}{thumb}", params={"X-Plex-Token": state_settings['plex_token']}, timeout=30)
                        if resp.status_code == 200:
                            poster_bytes = resp.content
                            cover_path = f"/tmp/plex_rev_cover_{rk}.jpg"
                            with open(cover_path, "wb") as f_out:
                                f_out.write(poster_bytes)

                    res = io.safe_write(
                        path=fpath, 
                        f=merged_tags, 
                        cover=cover_path,
                        sentinel_value=io.SENTINEL_VALUE_BATCH,
                        tmp_prefix="/tmp/plex_rev",
                        backup_dir=state_settings["backup_dir"] or "",
                        progress=PROGRESS_HOOKS
                    )
                    
                    if cover_path and os.path.exists(cover_path):
                        try:
                            os.remove(cover_path)
                        except Exception:
                            pass
                            
                    with PROGRESS_HOOKS.lock:
                        PROGRESS_HOOKS.progress["result"] = res
                        
                except Exception as e:
                    import traceback
                    traceback.print_exc()
                    with PROGRESS_HOOKS.lock:
                        PROGRESS_HOOKS.fail()
                        PROGRESS_HOOKS.progress["result"] = {"ok": False, "error": str(e)}

            threading.Thread(target=save_thread, args=(dict(STATE["settings"]),)).start()
            self.send_json({"ok": True, "job": "started"})


def monitor_heartbeat(server):
    # Give the browser 60 seconds to open and make its first ping
    time.sleep(60)
    while True:
        time.sleep(5)
        if time.time() - STATE.get("last_heartbeat", time.time()) > 15:
            print("No heartbeat from UI for 15 seconds. Shutting down auto-magic...")
            # Cleanup PID file
            pid_file = os.path.expanduser('~/scripts_meta_data/plex_metadata_reviewer_8766.pid')
            if os.path.exists(pid_file):
                try:
                    os.remove(pid_file)
                except Exception:
                    pass
            # Shutdown the server
            threading.Thread(target=server.shutdown, daemon=True).start()
            break

def main():
    port = 8766
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Plex Metadata Reviewer running on http://127.0.0.1:{port}")
    threading.Thread(target=monitor_heartbeat, args=(server,), daemon=True).start()
    server.serve_forever()

if __name__ == "__main__":
    main()
