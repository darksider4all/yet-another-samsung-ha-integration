#!/usr/bin/env python3
"""
reader.py - Samsung Tizen TV reader + local control proxy.

Local-only, no Samsung account/cloud. Talks directly to the TV's own
WebSocket control API (8001/8002). Serves a dashboard + a JSON API that a
custom Home Assistant integration (custom_components/tv_reader) consumes.

Endpoints:
  GET  /            - dashboard (power, running app, installed apps)
  GET  /v1/state    - JSON: {power, running_app, installed_apps, timestamp}
  POST /v1/control  - JSON body: {action: launch_app|close_app|send_key|power_on|power_off, ...}

Protocol notes (reverse-engineered against UE55DU8572UXXH / Tizen 7,
firmware T-NKLDDEUC-0090-2123.9 - see FINISH_READER_DOCKET.md):
  - wss://<tv>:8002/api/v2?name=<b64>  (NO token needed) is the "app
    control" channel. {"id":<appId>,"method":"ms.application.get",
    "params":{"id":<appId>}} -> {"id":...,"result":{"id","name","running",
    "visible","version"}} for installed apps, or {"error":{"code":404}} for
    unknown ids. "visible":true = foreground app.
  - wss://<tv>:8002/api/v2/channels/samsung.remote.control?name=<b64>&token=<tok>
    is the paired "remote" channel used for key presses (ms.remote.control).
  - The token-less ed.installedApp.get "list all apps" event is DEAD on this
    firmware (silently dropped), so the installed-app list is a *candidate
    probe* over CANDIDATE_APP_IDS; names/versions always come live from the TV.
  - REST http://<tv>:8001/api/v2/ -> device info (PowerState); REST
    applications/<id> with POST/DELETE -> launch / close an app.
"""

import base64
import json
import os
import socket
import ssl
import subprocess
import threading
import time
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import websocket

TV_PORT = 8002
REST_PORT = 8001
CLIENT_NAME = "yast-bridge"
HTTP_PORT = 8777
POLL_INTERVAL = 4  # seconds between full probe cycles
# Persistent storage. The add-on's /data volume survives updates and rebuilds;
# a path inside the image (/app/...) is wiped every time the container is
# recreated, which silently loses the wizard config and the TV pairing token.
DATA_DIR = os.environ.get("YAST_DATA_DIR") or os.environ.get("DATA_DIR") or "/data"
TOKEN_FILE = os.environ.get("TV_TOKEN_FILE", os.path.join(DATA_DIR, "tv_token"))
CONFIG_FILE = os.environ.get("CONFIG_FILE", os.path.join(DATA_DIR, "config.json"))
IMG_DIR = os.environ.get("IMG_DIR", os.path.join(DATA_DIR, "img"))
APP_WGT = os.environ.get("APP_WGT", "/app/app.wgt")  # bundled signed TV app, served at /app.wgt


def _seed_img():
    """Copy bundled default artwork into IMG_DIR on first start (if empty)."""
    import shutil
    src = "/app/img"
    try:
        if not os.path.isdir(src):
            return
        os.makedirs(IMG_DIR, exist_ok=True)
        have = any(f.endswith(".png") for f in os.listdir(IMG_DIR))
        if not have:
            for f in os.listdir(src):
                if f.endswith(".png"):
                    shutil.copy2(os.path.join(src, f), os.path.join(IMG_DIR, f))
    except Exception as e:  # noqa: BLE001
        print("img seed skipped:", e, flush=True)


_seed_img()

# ---- runtime configuration (config.json; editable live via /config) ----
_CONFIG = {}
_LAST_SAVE_ERROR = None


def _load_config():
    global _CONFIG
    try:
        with open(CONFIG_FILE) as f:
            _CONFIG = json.load(f) or {}
    except Exception:
        _CONFIG = {}


def _save_config():
    """Persist _CONFIG to CONFIG_FILE.

    Returns True on success, False on failure (never raises): callers must not
    report success to the wizard unless the bytes actually reached disk.
    """
    global _LAST_SAVE_ERROR
    try:
        parent = os.path.dirname(CONFIG_FILE)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(CONFIG_FILE, "w") as f:
            json.dump(_CONFIG, f, indent=2)
        _LAST_SAVE_ERROR = None
        return True
    except Exception as e:  # noqa: BLE001
        _LAST_SAVE_ERROR = str(e)
        print(f"config save FAILED ({CONFIG_FILE}): {e}", flush=True)
        return False


def _cfg(key, default=None):
    """config.json value -> matching UPPER env var -> default. Read live each call so
    changes via POST /v1/config take effect without a restart."""
    v = _CONFIG.get(key)
    if v is not None and v != "":
        return v
    env = os.environ.get(key.upper())
    if env:
        return env
    return default


_load_config()

print(
    f"config: {CONFIG_FILE} ({len(_CONFIG)} keys) | token: {TOKEN_FILE} | img: {IMG_DIR}",
    flush=True,
)
if CONFIG_FILE.startswith("/app/"):
    print(
        "WARNING: config lives inside the container image and is lost on every "
        "add-on update; point CONFIG_FILE at a persistent volume",
        flush=True,
    )


CONFIG_HTML = r"""<!doctype html><html><head><meta charset="utf-8">
<title>Yet Another Samsung Thing · setup</title><meta name="viewport" content="width=device-width, initial-scale=1">
<style>
 :root{--g:#e9c27a;--bg:#0f0f14;--card:#17171f;--line:#2a2a35;--mut:#1c1c24}
 *{box-sizing:border-box}
 body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:#eee;max-width:720px;margin:0 auto;padding:1rem 1rem 6rem;line-height:1.45}
 h1{font-size:1.5rem;margin:.4rem 0 1.1rem;display:flex;align-items:center;gap:.5rem}
 .step{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:1rem 1.1rem;margin-bottom:1rem}
 .step h2{font-size:1.02rem;margin:0 0 .6rem;color:var(--g)}
 label.fld{font-weight:600;display:block;margin-top:.7rem;font-size:.9rem}
 input.fld{width:100%;padding:.6rem;margin-top:.3rem;background:var(--mut);color:#eee;border:1px solid #333;border-radius:7px}
 input.fld:focus{outline:none;border-color:var(--g)}
 button{padding:.55rem 1.1rem;background:var(--g);color:#111;border:0;border-radius:8px;font-weight:700;cursor:pointer}
 button.ghost{background:#2a2a35;color:#eee}
 button.sm{padding:.35rem .7rem;font-size:.82rem}
 button.link{background:none;border:0;color:var(--g);padding:0;font-weight:600;font-size:.82rem;cursor:pointer}
 .muted{opacity:.6;font-size:.84rem;font-weight:400}
 .note{background:#2a2410;border:1px solid #574a1e;border-radius:8px;padding:.6rem .8rem;font-size:.85rem;margin-top:.6rem}
 .stat{margin-left:.6rem;font-size:.9rem}
 .ok{color:#6fd36f}.bad{color:#ff6b6b}
 .pill{background:#22222c;border:1px solid #333;border-radius:999px;padding:.45rem .8rem;margin:.4rem .35rem 0 0;display:inline-block;cursor:pointer;font-size:.88rem}
 .pill:hover{border-color:var(--g)}
 /* lights */
 .ltools{display:flex;align-items:center;gap:.6rem;margin:.7rem 0 .5rem;flex-wrap:wrap}
 .ltools input.fld{flex:1 1 180px;margin-top:0}
 .list{max-height:46vh;overflow-y:auto;overflow-x:hidden;border:1px solid var(--line);border-radius:8px;background:#121219}
 .grp{position:sticky;top:0;background:#1b1b24;border-bottom:1px solid var(--line);padding:.45rem .7rem;display:flex;justify-content:space-between;align-items:center;font-size:.8rem;font-weight:700;letter-spacing:.03em;text-transform:uppercase;color:#bdbdc7;z-index:1}
 .row{display:grid;grid-template-columns:22px 1fr auto;align-items:center;gap:.7rem;padding:.5rem .7rem;border-bottom:1px solid #20202a;cursor:pointer}
 .row:last-child{border-bottom:0}
 .row:hover{background:#1a1a22}
 .row:has(.chk:checked){background:#1e2130}
 .chk{width:20px;height:20px;accent-color:var(--g);cursor:pointer;margin:0}
 .row>span{min-width:0}
 .row .nm{display:block;font-weight:600;font-size:.92rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
 .row .eid{display:block;font-size:.76rem;opacity:.5;font-weight:400;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
 .badge{font-size:.72rem;padding:.12rem .5rem;border-radius:999px;background:#2a2a35;color:#9a9aa6;white-space:nowrap}
 .badge.on{background:#1d3a24;color:#7bdc8c}
 .empty{padding:1rem;text-align:center;opacity:.5;font-size:.86rem}
 /* sticky save bar */
 .savebar{position:fixed;left:0;right:0;bottom:0;background:rgba(15,15,20,.95);border-top:1px solid var(--line);backdrop-filter:blur(6px);padding:.7rem 1rem;display:flex;justify-content:center}
 .savebar .inner{width:100%;max-width:720px;display:flex;align-items:center;gap:.8rem}
 .savebar button{padding:.7rem 1.6rem;font-size:1rem}
 .cmd{background:#0c0c11;border:1px solid var(--line);border-radius:8px;padding:.7rem .8rem;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:.78rem;line-height:1.5;white-space:pre;overflow-x:auto;margin:.4rem 0}
 a.dl{color:var(--g);font-size:.85rem;margin-left:.8rem}
 .switch{position:relative;display:inline-block;width:46px;height:26px;flex:none}
 .switch input{opacity:0;width:0;height:0}
 .switch .slider{position:absolute;inset:0;background:#2a2a35;border-radius:999px;transition:.2s;cursor:pointer}
 .switch .slider:before{content:"";position:absolute;height:20px;width:20px;left:3px;top:3px;background:#eee;border-radius:50%;transition:.2s}
 .switch input:checked + .slider{background:var(--g)}
 .switch input:checked + .slider:before{transform:translateX(20px);background:#111}
 .info{cursor:help;color:var(--g);opacity:.85}
</style></head><body>
<h1>&#128062; Yet Another Samsung Thing</h1>
<div class="muted" style="margin:-.7rem 0 1.1rem;font-size:.9rem">Bridge setup</div>

<div class="step">
  <h2>1 &middot; TV</h2>
  <button class="ghost sm" onclick="discover()">Discover TVs</button><span id="dstat" class="stat"></span>
  <div id="tvlist"></div>
  <div id="dnote" class="note" style="display:none">
    Discovery uses SSDP multicast, which only works when the bridge runs with <b>host networking</b>
    (HA add-on: <code>host_network: true</code>; Docker: <code>network_mode: host</code>). If nothing shows up,
    enter the TV's IP manually below.
  </div>
  <label class="fld">TV IP</label><input id="tv_ip" class="fld" placeholder="192.168.1.50">
  <label class="fld">TV MAC <span class="muted">(optional; only used for Wake-on-LAN)</span></label><input id="tv_mac" class="fld" placeholder="AA:BB:CC:DD:EE:FF">
</div>

<div class="step">
  <h2>2 &middot; Install the TV app</h2>
  <div class="muted">Sideload the app onto the TV. Needs the TV in <b>Developer Mode</b> and the <code>sdb</code> tool on the computer you run the command from.</div>
  <button class="ghost sm" onclick="checkTV()" style="margin-top:.7rem">Check TV</button><span id="ckstat" class="stat"></span>
  <div id="devguide"></div>
  <div id="installbox" style="display:none">
    <label class="fld">Run this on your computer <span class="muted">(macOS/Linux, or Windows WSL/PowerShell)</span></label>
    <pre id="installcmd" class="cmd"></pre>
    <button class="ghost sm" onclick="copyCmd()">Copy</button><span id="cpstat" class="stat"></span>
    <a class="dl" id="wgtdl" href="#">Download the .wgt instead</a>
    <div class="muted" style="margin-top:.5rem">On first connect the TV shows an <b>Allow</b> prompt (~15s) — accept it. No <code>sdb</code>? It ships with Tizen Studio’s CLI tools (x86_64).</div>
  </div>
</div>

<div class="step">
  <h2>3 &middot; Home Assistant</h2>
  <form autocomplete="off" onsubmit="return false">
  <label class="fld">HA URL</label><input id="ha_url" class="fld" placeholder="http://homeassistant.local:8123">
  <label class="fld">Long-lived access token <span class="muted">(leave blank to keep the saved one)</span></label><input id="ha_token" class="fld" type="password" autocomplete="new-password">
  <button onclick="testHA()" style="margin-top:.8rem">Test &amp; load entities</button><span id="hstat" class="stat"></span>
  </form>
</div>

<div class="step">
  <h2>4 &middot; Dashboard tiles</h2>
  <div class="muted">Pick the entities to show on the TV — any category. Each tile gets the control its entity supports (dim, colour, position, temperature…), so you are not limited to lights and switches.</div>
  <div class="ltools">
    <select id="lcat" class="fld" onchange="filterLights()" style="flex:0 1 190px;margin-top:0"></select>
    <input id="lsearch" class="fld" type="search" placeholder="Search entities…" oninput="filterLights()">
    <span id="lcount" class="muted">0 selected</span>
    <button class="ghost sm" onclick="clearLights()">Clear</button>
  </div>
  <div id="lights" class="list"><div class="empty">Connect Home Assistant above to load your entities.</div></div>
</div>

<div class="step">
  <h2>5 &middot; App behavior</h2>
  <label class="fld">Splash time <span class="muted">(how long a notification stays on the TV)</span></label>
  <div style="display:flex;align-items:center;gap:.5rem;margin-top:.3rem">
    <input id="notify_seconds" class="fld" type="number" min="1" max="120" step="1" placeholder="8" style="max-width:110px;margin:0">
    <span class="muted">seconds</span>
  </div>
  <div class="muted" style="margin-top:.45rem">After this many seconds the TV returns to what it was doing. Home Assistant can still override it per message.</div>
  <div style="display:flex;align-items:flex-start;gap:.7rem;margin-top:1.1rem">
    <label class="switch"><input type="checkbox" id="auto_load"><span class="slider"></span></label>
    <div>
      <div style="font-weight:600;font-size:.9rem">Auto-load the app when the TV turns on <span class="info" title="A small watchdog service relaunches the app in the background whenever the TV powers on, so telemetry and controls come back automatically. It fires once per power-on, so it will not keep popping over whatever you are watching. Requires the watchdog running on a host with sdb (see server/watchdog). Turn off to never auto-launch.">&#9432;</span></div>
      <div class="muted" style="margin-top:.2rem">A background watchdog relaunches the app once each time the TV powers on. Turn off to launch it yourself instead.</div>
    </div>
  </div>
</div>

<div class="step">
  <h2>6 &middot; Pair the remote</h2>
  <div class="muted">Needed for local power on/off and key presses. Click, then press <b>Allow</b> on the TV.</div>
  <button class="ghost sm" onclick="pair()" style="margin-top:.7rem">Pair now</button><span id="pstat" class="stat"></span>
</div>

<div class="savebar"><div class="inner">
  <button onclick="saveAll()">Save configuration</button><span id="sstat" class="stat"></span>
</div></div>

<script>
var ENTITIES=[], CHECKED={}, LAN_IP='';
function $(id){return document.getElementById(id);}
function esc(s){return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;');}
async function init(){
  try{
    var c=await (await fetch('v1/config',{cache:'no-store'})).json();
    $('tv_ip').value=c.tv_ip||''; $('tv_mac').value=c.tv_mac||''; $('ha_url').value=c.ha_url||'';
    if(c.ha_token_set) $('ha_token').placeholder='(token saved)';
    if(c.notify_seconds) $('notify_seconds').value=c.notify_seconds;
    LAN_IP=c.lan_ip||'';
    $('auto_load').checked = (c.auto_load !== false);
    (c.main_entities||c.main_lights||[]).forEach(function(l){ CHECKED[l.entity_id]={name:l.name}; });
    updateCount();
    // Populate the category picker straight away; the saved token is already
    // good, so making the user press "Test & load entities" again is a wart.
    if(c.ha_token_set) loadEntities(true);
  }catch(e){}
}
async function discover(){
  $('dstat').textContent='scanning…'; $('tvlist').innerHTML=''; $('dnote').style.display='none';
  try{
    var r=await (await fetch('v1/discover_tv',{cache:'no-store'})).json();
    var tvs=r.tvs||[];
    if(!tvs.length){ $('dstat').textContent=''; $('dnote').style.display='block'; return; }
    $('dstat').innerHTML='<span class="ok">found '+tvs.length+'</span>';
    $('tvlist').innerHTML=tvs.map(function(t){
      return '<span class="pill" onclick="pickTV(\''+t.ip+'\',\''+(t.mac||'')+'\')">&#128250; '+esc(t.name||t.ip)+' &middot; '+t.ip+'</span>';
    }).join('');
  }catch(e){ $('dstat').innerHTML='<span class="bad">scan failed</span>'; $('dnote').style.display='block'; }
}
function pickTV(ip,mac){ $('tv_ip').value=ip; if(mac) $('tv_mac').value=mac; $('dstat').innerHTML='<span class="ok">selected '+ip+'</span>'; }
async function testHA(){
  var url=$('ha_url').value.trim(), tok=$('ha_token').value;
  $('hstat').textContent='connecting…';
  var body={ha_url:url}; if(tok) body.ha_token=tok;
  await fetch('v1/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  await loadEntities(false);
}
async function loadEntities(silent){
  try{
    var r=await fetch('v1/ha_entities',{cache:'no-store'});
    if(!r.ok){ if(!silent) $('hstat').innerHTML='<span class="bad">&#10007; '+esc((await r.json()).error||'failed')+'</span>'; return false; }
    ENTITIES=(await r.json()).entities||[];
    if(!silent) $('hstat').innerHTML='<span class="ok">&#10003; connected &middot; '+ENTITIES.length+' entities</span>';
    renderCatalog();
    return true;
  }catch(e){ if(!silent) $('hstat').innerHTML='<span class="bad">&#10007; '+e+'</span>'; return false; }
}
var CAT_TITLE={light:'Lights',switch:'Switches',outlet:'Outlets',cover:'Covers',garage_door:'Garage doors',gate:'Gates',door:'Doors',window:'Windows',lock:'Locks',climate:'Climate',fan:'Fans',media_player:'Media',camera:'Cameras',vacuum:'Vacuums',scene:'Scenes',script:'Scripts',button:'Buttons',number:'Numbers',select:'Selects',alarm:'Alarm',contact:'Contacts',motion:'Motion',moisture:'Moisture',smoke:'Smoke',temperature:'Temperature',humidity:'Humidity',illuminance:'Illuminance',power:'Power',energy:'Energy',battery:'Battery',weather:'Weather',sensor:'Sensors'};
function catTitle(c){ return CAT_TITLE[c]||(c?(c.charAt(0).toUpperCase()+c.slice(1)):'Other'); }
function renderCatalog(){
  if(!ENTITIES.length){ $('lights').innerHTML='<div class="empty">No entities found.</div>'; return; }
  var groups={}; ENTITIES.forEach(function(e){ var c=e.category||'other'; (groups[c]=groups[c]||[]).push(e); });
  var cats=Object.keys(groups).sort(function(a,b){ return catTitle(a).localeCompare(catTitle(b)); });
  var html='';
  cats.forEach(function(cat){
    var arr=groups[cat].slice().sort(function(a,b){return (a.name||'').localeCompare(b.name||'');});
    html+='<div class="grp" data-grp="'+esc(cat)+'"><span>'+esc(catTitle(cat))+' ('+arr.length+')</span>'
        +'<button class="link" data-all="'+esc(cat)+'">select all</button></div>';
    html+=arr.map(function(e){
      var on=CHECKED[e.entity_id]?'checked':'';
      var txt=((e.name||'')+' '+e.entity_id+' '+catTitle(cat)).toLowerCase();
      return '<label class="row" data-cat="'+esc(cat)+'" data-text="'+esc(txt)+'">'
        +'<input type="checkbox" class="chk" data-e="'+esc(e.entity_id)+'" data-n="'+esc(e.name||e.entity_id)+'" '+on+'>'
        +'<span><span class="nm">'+esc(e.name||e.entity_id)+'</span><span class="eid">'+esc(e.entity_id)+'</span></span>'
        +'<span class="badge'+(e.state==='on'?' on':'')+'">'+esc(e.state||'')+'</span>'
        +'</label>';
    }).join('');
  });
  $('lights').innerHTML=html;
  buildCatSelect(cats,groups);
  $('lights').querySelectorAll('input.chk').forEach(function(cb){
    cb.onchange=function(){ if(cb.checked) CHECKED[cb.dataset.e]={name:cb.dataset.n}; else delete CHECKED[cb.dataset.e]; updateCount(); };
  });
  $('lights').querySelectorAll('button.link[data-all]').forEach(function(b){
    b.onclick=function(ev){ ev.preventDefault();
      var boxes=[]; $('lights').querySelectorAll('.grp[data-grp="'+b.dataset.all+'"] ~ label').forEach(function(){});
      // gather rows that belong to this group (until next grp)
      var hdr=b.closest('.grp'), n=hdr.nextElementSibling;
      while(n && !n.classList.contains('grp')){ var c=n.querySelector('input.chk'); if(c && n.style.display!=='none') boxes.push(c); n=n.nextElementSibling; }
      var anyUnchecked=boxes.some(function(c){return !c.checked;});
      boxes.forEach(function(c){ c.checked=anyUnchecked; if(anyUnchecked) CHECKED[c.dataset.e]={name:c.dataset.n}; else delete CHECKED[c.dataset.e]; });
      b.textContent=anyUnchecked?'clear all':'select all'; updateCount();
    };
  });
  updateCount();
  filterLights();
}
function buildCatSelect(cats,groups){
  var sel=$('lcat'); if(!sel) return;
  var cur=sel.value||'';
  var opts='<option value="">All categories ('+ENTITIES.length+')</option>';
  cats.forEach(function(c){ opts+='<option value="'+esc(c)+'">'+esc(catTitle(c))+' ('+groups[c].length+')</option>'; });
  sel.innerHTML=opts;
  if(cur && cats.indexOf(cur)>=0) sel.value=cur;
}
function filterLights(){
  var q=$('lsearch').value.trim().toLowerCase();
  var cat=($('lcat')||{}).value||'';
  $('lights').querySelectorAll('label.row').forEach(function(r){
    var ok=(!cat || r.dataset.cat===cat) && (!q || r.dataset.text.indexOf(q)>=0);
    r.style.display=ok?'':'none';
  });
  // hide group headers with no visible rows
  $('lights').querySelectorAll('.grp').forEach(function(h){
    var n=h.nextElementSibling, vis=false;
    while(n && !n.classList.contains('grp')){ if(n.style.display!=='none'){vis=true;break;} n=n.nextElementSibling; }
    h.style.display=vis?'':'none';
  });
}
function clearLights(){ CHECKED={}; $('lights').querySelectorAll('input.chk').forEach(function(c){c.checked=false;}); updateCount(); }
function updateCount(){ var n=Object.keys(CHECKED).length; $('lcount').textContent=n+' selected'; }
function checkTV(){
  var ip=$('tv_ip').value.trim();
  if(!ip){ $('ckstat').innerHTML='<span class="bad">enter the TV IP in step 1 first</span>'; return; }
  $('ckstat').textContent='checking\u2026'; $('devguide').innerHTML='';
  fetch('v1/tv_devmode?ip='+encodeURIComponent(ip),{cache:'no-store'}).then(function(r){return r.json();})
    .then(function(d){ renderDev(d, ip); })
    .catch(function(e){ $('ckstat').innerHTML='<span class="bad">check failed</span>'; });
}
function renderDev(d, ip){
  var thisPC=d.requester_ip||'this computer';
  if(!d.reachable){
    $('ckstat').innerHTML='<span class="bad">\u2717 not reachable</span>';
    $('devguide').innerHTML='<div class="note">Can\u2019t reach the TV at '+esc(ip)+':8001. Make sure it\u2019s on and the IP is right.</div>';
    $('installbox').style.display='none'; return;
  }
  var nm=d.name?(' ('+esc(d.name)+')'):'';
  if(d.developer_mode===true){
    $('ckstat').innerHTML='<span class="ok">\u2713 Developer Mode on</span>';
    var ipok=d.developer_ip && d.developer_ip===thisPC;
    var g='<div class="note">Developer Mode is <b>on</b>'+nm+'. Host PC IP on the TV: <b>'+esc(d.developer_ip||'?')+'</b>.';
    if(!ipok) g+=' If you\u2019ll run the install from <b>this computer ('+esc(thisPC)+')</b>, set the TV\u2019s Host PC IP to that and reboot.';
    g+='</div>'; $('devguide').innerHTML=g;
  } else {
    $('ckstat').innerHTML='<span class="bad">Developer Mode off</span>';
    $('devguide').innerHTML='<div class="note"><b>Turn on Developer Mode:</b> on the TV open <b>Apps</b>, press <b>1 2 3 4 5</b>, toggle <b>Developer mode ON</b>, set <b>Host PC IP</b> to <b>'+esc(thisPC)+'</b> (the computer you\u2019ll run the command on), then <b>reboot the TV</b>. Re-check after it restarts.</div>';
  }
  showInstall(ip);
}
function instCmd(ip){
  var base='http://'+(LAN_IP||location.hostname)+':8777';
  return '# 1) download the signed app from the bridge\n'
    +'curl -L -o samsung-tv-app.wgt "'+base+'/app.wgt"\n'
    +'# 2) connect to the TV (accept the Allow prompt the first time)\n'
    +'sdb connect '+ip+'\n'
    +'# 3) install and launch\n'
    +'sdb -s '+ip+':26101 push samsung-tv-app.wgt /home/owner/share/tmp/sdk_tools/sideload.wgt\n'
    +'sdb -s '+ip+':26101 shell 0 vd_appinstall ChanProbe0 /home/owner/share/tmp/sdk_tools/sideload.wgt\n'
    +'sdb -s '+ip+':26101 shell 0 was_execute ChanProbe0.ChannelProbe';
}
function showInstall(ip){
  $('installcmd').textContent=instCmd(ip);
  $('wgtdl').href='app.wgt';
  $('installbox').style.display='';
}
function copyCmd(){
  try{ navigator.clipboard.writeText($('installcmd').textContent).then(function(){ $('cpstat').innerHTML='<span class="ok">copied</span>'; }); }
  catch(e){ $('cpstat').innerHTML='<span class="bad">select & copy manually</span>'; }
}
async function pair(){
  $('pstat').textContent='pairing… press Allow on the TV';
  try{
    var r=await fetch('v1/control',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'pair'})});
    var j=await r.json();
    $('pstat').innerHTML=(r.ok&&j.token)?'<span class="ok">&#10003; paired</span>':'<span class="bad">&#10007; not paired ('+esc(j.error||'denied/timeout')+')</span>';
  }catch(e){ $('pstat').innerHTML='<span class="bad">&#10007; '+e+'</span>'; }
}
async function saveAll(){
  var ml=Object.keys(CHECKED).map(function(id){ return {entity_id:id, name:CHECKED[id].name}; });
  var body={tv_ip:$('tv_ip').value.trim(), tv_mac:$('tv_mac').value.trim(), ha_url:$('ha_url').value.trim(), main_entities:ml};
  if($('ha_token').value) body.ha_token=$('ha_token').value;
  var ns=parseInt($('notify_seconds').value,10); if(ns>0) body.notify_seconds=ns;
  body.auto_load=$('auto_load').checked;
  $('sstat').textContent='saving…';
  var r=await fetch('v1/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  $('sstat').innerHTML=r.ok?'<span class="ok">&#10003; saved &middot; '+ml.length+' tiles</span>':'<span class="bad">&#10007; save failed</span>';
  if(r.ok) $('ha_token').value='';
}
init();
</script></body></html>
"""




def _load_token():
    t = os.environ.get("TV_TOKEN", "")
    if t:
        return t
    try:
        with open(TOKEN_FILE) as f:
            return f.read().strip()
    except Exception:
        return ""


_TOKEN_LOCK = threading.RLock()


def _save_token(tok):
    with _TOKEN_LOCK:
        try:
            os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
            with open(TOKEN_FILE, "w") as f:
                f.write(tok or "")
        except Exception as e:
            print("token save failed:", e, flush=True)


TV_TOKEN = _load_token()  # remote-channel pairing token (env or persisted file)

# On-TV agent (hidden Tizen app that reports telemetry via POST /v1/report).
# Liveness only; relaunching is handled by the watchdog systemd service on .114.
AGENT_STALE_SECONDS = int(os.environ.get("AGENT_STALE_SECONDS", "75"))   # offline if silent this long

# Candidate Tizen appIds to probe for presence/running-state.
# Verified against this TV: 111299001912 (YouTube), 3201910019365 (Prime Video),
# 3201512006963 (Plex), 3201606009684 (Spotify), 3201901017640 (Disney+).
CANDIDATE_APP_IDS = [
    "org.tizen.browser",       # Internet
    "111299001912",            # YouTube (verified on this TV)
    "3201907018807",           # Netflix (2020+ id; 11101200001 is pre-2020)
    "3201910019365",           # Prime Video (verified on this TV)
    "3201512006963",           # Plex (verified on this TV)
    "3201606009684",           # Spotify (verified on this TV)
    "3201901017640",           # Disney+ (verified on this TV)
    "3201807016597",           # Apple TV
    "3201601007625",           # Hulu
    "3201601007230",           # HBO Max
    "3202301029760",           # Max (2023+)
    "3202203026841",           # Twitch
    "3202405035480",           # Samsung TV Plus (preinstalled)
    "3202004020626",           # Amazon Alexa
]


def _b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode()


def _lan_ip():
    """Best-effort LAN IP of this host (for the wizard's install command)."""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80)); ip = s.getsockname()[0]; s.close(); return ip
    except Exception:
        return ""


def _rest_request(path: str, method: str = "GET") -> str:
    url = f"http://{_cfg('tv_ip','')}:{REST_PORT}/api/v2/{path}"
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=6) as r:
        return r.read().decode()


def tv_devmode(ip=""):
    """Query the TV's local REST info (:8001, no auth) for Developer Mode state.
    Lets the setup wizard tell the user to enable it / fix the Host PC IP without sdb."""
    ip = (ip or _cfg("tv_ip", "")).strip()
    out = {"ip": ip, "reachable": False, "developer_mode": None, "developer_ip": None, "name": None, "model": None}
    if not ip:
        return out
    url = f"http://{ip}:{REST_PORT}/api/v2/"
    # The TV's REST endpoint is intermittently unresponsive; retry a few times so a
    # single bad moment doesn't make the wizard wrongly say "not reachable".
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=4) as r:
                d = json.loads(r.read().decode())
            dev = d.get("device") or {}
            out["reachable"] = True
            dm = dev.get("developerMode")
            out["developer_mode"] = (str(dm) == "1") if dm is not None else None
            out["developer_ip"] = dev.get("developerIP")
            out["name"] = dev.get("name")
            out["model"] = dev.get("modelName")
            break
        except Exception:
            if attempt < 2:
                time.sleep(0.6)
    if not out["reachable"]:
        c = _DEVICE_CACHE
        if c.get("device") and c.get("ip") == ip and (time.time() - c.get("ts", 0.0)) < 90:
            dev = c["device"]
            out["reachable"] = True
            out["cached"] = True
            dm = dev.get("developerMode")
            out["developer_mode"] = (str(dm) == "1") if dm is not None else None
            out["developer_ip"] = dev.get("developerIP")
            out["name"] = dev.get("name")
            out["model"] = dev.get("modelName")
    return out


_POWER_CACHE = None  # last known-good PowerState ("on"/"standby"); power reads are flaky
_DEVICE_CACHE = {"ip": None, "device": None, "ts": 0.0}  # last good /api/v2 device block from the poller


_POWER_INTENT_TS = 0.0          # when a *deliberate* power command set the cache
POWER_INTENT_GRACE = 30.0       # seconds during which pollers may not overwrite it


def _note_power(p, deliberate=False):
    """One writer for the power cache.

    KEY_POWER is a blind toggle, so the cache must reflect what we last *did*, not
    what a stale REST field remembers. A deliberate power command therefore locks the
    value for POWER_INTENT_GRACE seconds against poller writes; before this, the next
    poll cycle snapped 'standby' back to 'on' and a second turn_off flipped the TV on.
    """
    global _POWER_CACHE, _POWER_INTENT_TS
    if not p:
        return
    if not deliberate and (time.time() - _POWER_INTENT_TS) < POWER_INTENT_GRACE:
        return  # a real command is more trustworthy than an inference
    _POWER_CACHE = p
    if deliberate:
        _POWER_INTENT_TS = time.time()


def _effective_power(snap):
    """Robust on/standby from all signals, since this TV's REST PowerState is often
    blank. Priority: REST value -> a visibly-running app or a real agent foreground
    app -> agent alive but nothing foreground means screen off (standby). Feeds the
    toggle cache so KEY_POWER goes the right way."""
    p = snap.get("power")
    if p in ("on", "standby", "off"):
        eff = p
    else:
        ra = snap.get("running_app") or {}
        ag = snap.get("agent") or {}
        fg = ag.get("foregroundApp")
        fg_on = isinstance(fg, str) and fg and not fg.startswith("err")
        if ra.get("visible") or fg_on:
            eff = "on"
        elif ag.get("online") or ra or snap.get("installed_apps"):
            eff = "standby"   # reachable but nothing on screen -> screen off
        else:
            eff = "off"       # nothing reachable -> fully powered off
    # read path: never writes the cache. Letting it do so is what undid a deliberate
    # power-off within one poll cycle.
    return eff


def get_power_state() -> str | None:
    """Read PowerState from the REST device info (local, no auth). Caches the last
    non-empty value; returns None on failure/empty so callers keep the last good one."""
    try:
        dev = json.loads(_rest_request(""))["device"]
        _DEVICE_CACHE.update(ip=_cfg("tv_ip", ""), device=dev, ts=time.time())
        return dev.get("PowerState") or None
    except Exception:
        return None


class TVState:
    """Thread-safe holder for the latest poll results."""

    def __init__(self):
        self._lock = threading.Lock()
        self._installed_apps = []
        self._running_app = None
        self._power = None
        self._timestamp = None
        self._last_error = None

    def update(self, installed_apps, running_app, power=None, error=None):
        with self._lock:
            self._installed_apps = installed_apps
            self._running_app = running_app
            if power is not None:
                self._power = power
            self._timestamp = datetime.now(timezone.utc).isoformat()
            self._last_error = error

    def snapshot(self):
        with self._lock:
            return {
                "power": self._power,
                "running_app": self._running_app,
                "installed_apps": self._installed_apps,
                "timestamp": self._timestamp,
                "last_error": self._last_error,
            }


def poller_loop(state: TVState, stop_event: threading.Event):
    """Background thread: keep a WS connection to the app-control channel
    and repeatedly cycle through CANDIDATE_APP_IDS with ms.application.get.
    """
    while not stop_event.is_set():
        ws = None
        # ponytail: while the on-TV agent is reporting, it already delivers app and
        # foreground telemetry over /v1/report — this WS adds nothing, and every
        # reconnect makes the TV demand a fresh "Allow this device?". That retry loop
        # is what turned one command into a stack of dialogs. Poll only as a fallback.
        # ponytail: also stay off the TV while it is asleep. A standby set refuses the
        # handshake and raises an Allow dialog per attempt, so polling it costs the
        # user a screenful of prompts (9 observed) across one wake cycle.
        if (
            (_AGENT.snapshot() or {}).get("online")
            or not TV_TOKEN
            or _POWER_CACHE in ("standby", "off")
        ):
            # ponytail: no token means every reconnect raises an Allow dialog, so wait
            # quietly instead of polling. Pairing (POST /v1/control {action:pair}) is
            # the only thing that should ask the TV for permission.
            # Trust the on-TV agent over the TV's REST PowerState (which lies on this
            # model): a real foreground app means on; agent alive with nothing
            # foreground means the screen is off. REST must never decide the cache.
            _ag = (_AGENT.snapshot() or {})
            _fg = _ag.get("foregroundApp")
            _hint = (
                "on" if (isinstance(_fg, str) and _fg and not _fg.startswith("err"))
                else ("standby" if _ag.get("online") else None)
            )
            _note_power(_hint)
            _snap = state.snapshot()
            state.update(_snap["installed_apps"], _snap["running_app"], power=_hint)
            for _ in range(POLL_INTERVAL * 2):
                if stop_event.is_set():
                    break
                time.sleep(0.5)
            continue
        # The app-control channel takes no token (only the remote-control channel on
        # the SSL port does). Sending one here was wrong: it made the TV treat the
        # poller as a different client than the paired one.
        _q = f"name={_b64(CLIENT_NAME)}"
        url = f"wss://{_cfg('tv_ip','')}:{TV_PORT}/api/v2?{_q}"
        try:
            ws = websocket.create_connection(
                url, timeout=8, sslopt={"cert_reqs": ssl.CERT_NONE}
            )
            ws.settimeout(8)
            first = json.loads(ws.recv())
            if first.get("event") != "ms.channel.connect":
                raise RuntimeError(f"unexpected handshake reply: {first}")
            # No token adoption here: the app-control channel has its own identity and
            # its token is not the remote-control token. Mixing them was a race and a
            # correctness bug (review item 2).

            while not stop_event.is_set():
                installed = []
                running_app = None
                for app_id in CANDIDATE_APP_IDS:
                    ws.send(
                        json.dumps(
                            {
                                "id": app_id,
                                "method": "ms.application.get",
                                "params": {"id": app_id},
                            }
                        )
                    )
                    try:
                        reply = json.loads(ws.recv())
                    except Exception as e:
                        raise RuntimeError(f"recv failed for {app_id}: {e}") from e

                    if reply.get("error"):
                        continue  # not installed / unknown id - skip silently
                    result = reply.get("result")
                    if not result:
                        continue
                    app_info = {
                        "appId": result.get("id", app_id),
                        "name": result.get("name"),
                        "running": bool(result.get("running")),
                        "visible": bool(result.get("visible")),
                        "version": result.get("version"),
                    }
                    installed.append(app_info)
                    if app_info["visible"]:
                        running_app = app_info

                if running_app is None:
                    for app_info in installed:
                        if app_info["running"]:
                            running_app = app_info
                            break

                power = get_power_state()
                if not power and running_app and running_app.get("visible"):
                    power = "on"  # REST blanked PowerState but an app is visibly running
                _note_power(power)
                state.update(installed, running_app, power=power)

                for _ in range(POLL_INTERVAL * 2):
                    if stop_event.is_set():
                        break
                    time.sleep(0.5)
        except Exception as e:
            state.update(
                state.snapshot()["installed_apps"],
                state.snapshot()["running_app"],
                power=get_power_state(),
                error=str(e),
            )
            # ponytail: back off 60s instead of 3s. Every reconnect attempt makes the
            # TV raise an "Allow this device?" dialog, so a 3s retry loop turns a
            # standby set into a prompt storm. 60s keeps recovery usable while the
            # set is asleep; lower it only if telemetry needs to be fresher on wake.
            for _ in range(120):
                if stop_event.is_set():
                    break
                time.sleep(0.5)
        finally:
            if ws is not None:
                try:
                    ws.close()
                except Exception:
                    pass


# ---- control actions -------------------------------------------------------

def control_launch_app(app_id: str) -> bool:
    _rest_request(f"applications/{app_id}", "POST")
    return True


DEFAULT_AGENT_APP = "ChanProbe0.ChannelProbe"
CLOSE_WAIT = 3.0  # seconds to wait for the TV to actually stop the app


def app_state(app_id: str) -> dict:
    """Installed-app record from the TV's REST API ({'running': bool, ...})."""
    try:
        return json.loads(_rest_request(f"applications/{app_id}") or "{}")
    except Exception:  # noqa: BLE001
        return {}


def launch_agent() -> tuple:
    """Bring the on-TV agent to the foreground. Returns (ok, error).

    Tizen suspends the timers of a hidden web app, so a backgrounded agent can
    never poll the command queue: only an external launch can wake it, and its
    boot path then fetches /v1/toast and paints the pending message.

    Two TV quirks, both verified on the device:
      * POST is a no-op while the app is already running (returns true, window
        never surfaces, no boot), so we close it first;
      * DELETE is applied asynchronously — launching immediately gets the fresh
        instance killed when the close finally lands ~3 s later, which shows the
        toast for two seconds and looks like "notifications don't work".
    Hence: close, wait for running:false, then launch.
    """
    app_id = _cfg("agent_app_id", "") or DEFAULT_AGENT_APP
    try:
        try:
            control_close_app(app_id)
            deadline = time.time() + CLOSE_WAIT
            while time.time() < deadline:
                if not app_state(app_id).get("running", False):
                    break
                time.sleep(0.25)
        except Exception:  # noqa: BLE001 - not running is fine
            pass
        control_launch_app(app_id)
        return True, None
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


def control_close_app(app_id: str) -> bool:
    _rest_request(f"applications/{app_id}", "DELETE")
    return True


def control_wol() -> bool:
    """Wake-on-LAN magic packet. Does NOT wake this Wi-Fi TV (kept for reference)."""
    mac = _cfg("tv_mac", "").replace(":", "").replace("-", "").lower()
    if len(mac) != 12:
        raise RuntimeError(f"bad/missing TV MAC {_cfg('tv_mac','')!r}")
    magic = bytes.fromhex("ff" * 6 + mac * 16)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(magic, ("255.255.255.255", 9))
    finally:
        sock.close()
    return True


def control_power_on() -> bool:
    """Local power-on: in network-standby the TV's remote-control WS still answers,
    and KEY_POWER toggles it back on (verified). Idempotent - no-op if already on. A
    *fully* powered-off TV won't answer; the HA layer falls back to SmartThings then."""
    if _POWER_CACHE == "on":
        return True
    # KEY_POWER is a blind toggle: if we are not *sure* the set is off, sending it
    # would switch it ON-to-OFF or OFF-to-ON with equal confidence. Refuse instead.
    if _POWER_CACHE not in ("standby", "off"):
        return False
    ok = _send_remote_key("KEY_POWER")
    if ok:
        _note_power("on", deliberate=True)
    return ok


def _remote_url(token=""):
    q = f"name={_b64(CLIENT_NAME)}"
    if token:
        q += f"&token={token}"
    return f"wss://{_cfg('tv_ip','')}:{TV_PORT}/api/v2/channels/samsung.remote.control?{q}"


def control_pair(timeout=45) -> str:
    """Pair (or re-pair) from scratch: drop any stored token first, then connect with
    none so the TV shows an Allow dialog. On approval the connect frame carries a
    token, which is persisted. Clearing first matters — a stale token is refused
    outright (1005) and the TV never gets as far as asking."""
    global TV_TOKEN
    _clear_token()
    ws = _open_remote_channel(timeout)
    try:
        tok = TV_TOKEN
        if not tok:
            raise RuntimeError("connected but no token")
        return tok
    finally:
        try:
            ws.close()
        except Exception:
            pass


def _clear_token():
    """Forget the stored pairing token: the TV refuses it, so stop presenting it."""
    global TV_TOKEN
    with _TOKEN_LOCK:
        TV_TOKEN = ""
        try:
            os.remove(TOKEN_FILE)
        except Exception:
            pass


_CONNECT_TIMES = []            # timestamps of remote-channel connections
CONNECT_BUDGET = 12            # max such connections per CONNECT_WINDOW
CONNECT_WINDOW = 3600.0        # seconds


def _connect_allowed():
    """Hard circuit breaker: a burst of connections is what makes the TV ask again.

    Whatever the cause of a retry loop, the bridge can now add at most CONNECT_BUDGET
    dialogs an hour — a guardrail, not a fix. Reset by restarting the add-on.
    """
    now = time.time()
    _CONNECT_TIMES[:] = [t for t in _CONNECT_TIMES if now - t < CONNECT_WINDOW]
    if len(_CONNECT_TIMES) >= CONNECT_BUDGET:
        raise RuntimeError(
            f"refusing to connect: {len(_CONNECT_TIMES)} remote-channel connections in "
            f"the last {int(CONNECT_WINDOW/60)} min (budget {CONNECT_BUDGET}). "
            "Check for a retry loop before restarting the bridge."
        )
    _CONNECT_TIMES.append(now)


def _open_remote_channel(wait_s: int):
    """Connect and wait for ms.channel.connect, adopting any token the TV hands back.

    wait_s must cover a human answering an on-screen Allowed dialog on the first
    pairing — 6s was why a token was never captured: the socket died before Allow.
    """
    global TV_TOKEN
    _connect_allowed()          # circuit breaker: never storm the TV with dialogs
    ws = websocket.create_connection(
        _remote_url(TV_TOKEN), timeout=wait_s, sslopt={"cert_reqs": ssl.CERT_NONE}
    )
    ws.settimeout(wait_s)
    # The remote channel broadcasts connect/disconnect events for ALL clients
    # (e.g. samsungtv_smart). Read until OUR ms.channel.connect, ignoring others.
    for _ in range(12):
        msg = json.loads(ws.recv())
        if msg.get("event") == "ms.channel.connect":
            tok = (msg.get("data") or {}).get("token")
            if tok:
                with _TOKEN_LOCK:            # read-modify-write under one lock
                    if str(tok) != TV_TOKEN:
                        TV_TOKEN = str(tok)
                        _save_token(TV_TOKEN)
            return ws
    raise RuntimeError("no ms.channel.connect on remote channel")


def _send_remote_key(key: str) -> bool:
    """Send one remote key, self-healing a stale token.

    If the stored token is refused, forget it and reconnect without one: the TV then
    asks once, and the token it returns is saved. That converts 'rejected token →
    prompt forever' into 'rejected token → one prompt → works'.
    """
    global TV_TOKEN
    had_token = bool(TV_TOKEN)
    try:
        ws = _open_remote_channel(8 if had_token else 45)
    except Exception:
        if not had_token:
            raise
        _clear_token()
        ws = _open_remote_channel(45)
    try:
        ws.send(json.dumps({
            "method": "ms.remote.control",
            "params": {"Cmd": "Click", "DataOfCmd": key,
                       "Option": "false", "TypeOfRemote": "SendRemoteKey"},
        }))
        time.sleep(0.4)
        return True
    finally:
        try:
            ws.close()
        except Exception:
            pass


def control_power_off() -> dict:
    """Local power-off (to standby) via KEY_POWER over the remote-control WS.

    Returns what actually happened instead of a bare True. A cache miss used to look
    identical to a successful send, which is how a dead power-off stayed invisible for
    the caller. Still idempotent: KEY_POWER is a toggle, so standby/off is never sent
    again, and an unknown power state is refused rather than guessed - guessing could
    switch the TV ON."""
    power = _POWER_CACHE
    if power in ("standby", "off"):
        return {"sent": False, "power": power, "reason": "already_off"}
    if power != "on":
        return {"sent": False, "power": power, "reason": "power_unknown"}
    # No token guard here on purpose: the first unpaired press is how the TV is asked
    # for permission, and _send_remote_key adopts the token it returns.
    _send_remote_key("KEY_POWER")
    # deliberate: locks the cache so the poller cannot snap it back to "on" and
    # let a second turn_off flip the set back on
    _note_power("standby", deliberate=True)
    return {"sent": True, "power": power, "reason": None}


# Server-side control actions. Everything the on-TV agent handles (volume, mute,
# channel, tune, power_off, notify, hide) lives in AGENT_ACTIONS and is routed to
# the agent; only these few run here. (The TV's remote-control WebSocket does not
# register keypresses on this model, so that path was removed.)
# ---- Home Assistant proxy (for the on-TV lights dashboard) -----------------
# The HA token lives here (server-side), never in the TV app. Read from env or
# /app/data/ha.env (KEY=VALUE lines).
def _ha_conf():
    url = _cfg("ha_url", "") or os.environ.get("HA_URL", "")
    tok = _cfg("ha_token", "") or os.environ.get("HA_TOKEN", "")
    if not (url and tok):  # legacy fallback
        try:
            for line in open("/app/data/ha.env"):
                line = line.strip()
                if line.startswith("HA_URL="): url = url or line[7:]
                elif line.startswith("HA_TOKEN="): tok = tok or line[9:]
        except Exception:
            pass
    return url.rstrip("/"), tok


def _ha_request(path, method="GET", body=None):
    import urllib.request
    url, tok = _ha_conf()
    if not (url and tok):
        raise RuntimeError("HA not configured (set /app/data/ha.env)")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url + path, data=data, method=method,
        headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json",
                 "User-Agent": "Mozilla/5.0 (yast-bridge)"})
    with urllib.request.urlopen(req, timeout=8) as r:
        txt = r.read().decode()
        return json.loads(txt) if txt else None


# ---- entity categories ------------------------------------------------------
# A tile's control type is derived from the entity itself, the way HomeKit
# picks an accessory type: domain first, then device_class, then
# supported_features. Nothing is hardcoded per device, so any entity the user
# selects gets the right kind of control automatically.
CATEGORY_BY_DOMAIN = {
    "light": "light",
    "switch": "switch",
    "input_boolean": "switch",
    "fan": "fan",
    "cover": "cover",
    "lock": "lock",
    "climate": "climate",
    "water_heater": "climate",
    "humidifier": "climate",
    "media_player": "media_player",
    "camera": "camera",
    "vacuum": "vacuum",
    "sensor": "sensor",
    "binary_sensor": "sensor",
    "scene": "scene",
    "script": "script",
    "button": "button",
    "number": "number",
    "input_number": "number",
    "select": "select",
    "input_select": "select",
    "alarm_control_panel": "alarm",
    "person": "person",
    "device_tracker": "tracker",
    "weather": "weather",
    "update": "update",
    "sun": "sensor",
}

# Where a device_class deserves its own accessory type rather than the domain's.
DEVICE_CLASS_CATEGORY = {
    ("switch", "outlet"): "outlet",
    ("cover", "garage"): "garage_door",
    ("cover", "gate"): "gate",
    ("cover", "door"): "door",
    ("cover", "window"): "window",
    ("binary_sensor", "door"): "contact",
    ("binary_sensor", "window"): "contact",
    ("binary_sensor", "garage_door"): "contact",
    ("binary_sensor", "motion"): "motion",
    ("binary_sensor", "occupancy"): "motion",
    ("binary_sensor", "presence"): "motion",
    ("binary_sensor", "moisture"): "moisture",
    ("binary_sensor", "smoke"): "smoke",
    ("sensor", "temperature"): "temperature",
    ("sensor", "humidity"): "humidity",
    ("sensor", "illuminance"): "illuminance",
    ("sensor", "power"): "power",
    ("sensor", "energy"): "energy",
    ("sensor", "battery"): "battery",
}

TOGGLEABLE_DOMAINS = {"light", "switch", "input_boolean", "fan", "cover", "lock",
                      "media_player", "scene", "script", "vacuum", "humidifier"}

# States that count as "active" for the tile highlight, per domain family.
ACTIVE_STATES = {"on", "open", "opening", "playing", "cleaning", "unlocked", "heat",
                 "cool", "heat_cool", "auto", "dry", "fan_only"}


def _attrs_of(state):
    return (state or {}).get("attributes") or {}


def entity_category(state):
    dom = ((state or {}).get("entity_id") or "").split(".", 1)[0]
    dc = _attrs_of(state).get("device_class")
    return DEVICE_CLASS_CATEGORY.get((dom, dc)) or CATEGORY_BY_DOMAIN.get(dom, dom or "unknown")


def entity_capabilities(state):
    """Which controls the tile should offer — mirrors HomeKit's characteristic set."""
    dom = ((state or {}).get("entity_id") or "").split(".", 1)[0]
    attrs = _attrs_of(state)
    feats = int(attrs.get("supported_features") or 0)
    caps = []
    if dom in TOGGLEABLE_DOMAINS:
        caps.append("toggle")
    if dom == "light":
        modes = set(attrs.get("supported_color_modes") or [])
        if not modes or "brightness" in modes or "brightness" in attrs or feats & 1:
            caps.append("dim")
        if modes & {"color_temp", "hs", "xy", "rgb", "rgbw", "rgbww"} or feats & 2:
            caps.append("color_temp")
        if modes & {"hs", "xy", "rgb", "rgbw", "rgbww"}:
            caps.append("color")
    if dom == "cover":
        if feats & 4:
            caps.append("position")
        if feats & 3:
            caps.append("open_close")
    if dom == "climate":
        if feats & 1:
            caps.append("temperature")
        if feats & 2:
            caps.append("target_temp")
    if dom == "media_player":
        if feats & 4:
            caps.append("volume")
        if feats & 128:
            caps.append("play_pause")
    return caps


def entity_record(state, name=None):
    """One tile: identity, category, live value and the controls it supports."""
    state = state or {}
    eid = state.get("entity_id") or ""
    dom = eid.split(".", 1)[0]
    attrs = _attrs_of(state)
    st = state.get("state")
    br = attrs.get("brightness")
    unit = attrs.get("unit_of_measurement")
    try:
        value = float(st) if st not in (None, "unknown", "unavailable") else None
    except (TypeError, ValueError):
        value = None
    return {
        "entity_id": eid,
        "name": name or attrs.get("friendly_name") or eid,
        "domain": dom,
        "category": entity_category(state),
        "device_class": attrs.get("device_class"),
        "state": st,
        "on": st in ACTIVE_STATES,
        "available": st not in ("unavailable", "unknown", None),
        "reachable": st not in ("unavailable", "unknown", None),  # legacy key
        "value": value,
        "unit": unit,
        "brightness": (round(br / 255 * 100) if isinstance(br, (int, float)) else None),
        "capabilities": entity_capabilities(state),
    }


def ha_entity_view(category=None):
    """The configured tiles, classified. `main_entities` wins over `main_lights`."""
    states = _ha_request("/api/states")
    by_id = {s.get("entity_id"): s for s in states}
    wanted = _cfg("main_entities", None)
    if not wanted:
        wanted = [i if isinstance(i, str) else i.get("entity_id") for i in (_cfg("main_lights", []) or [])]
    out = []
    for item in wanted:
        if isinstance(item, str):
            item = {"entity_id": item}
        eid = (item or {}).get("entity_id")
        if not eid:
            continue
        rec = entity_record(by_id.get(eid) or {"entity_id": eid, "state": "unavailable"}, item.get("name"))
        out.append(rec)
    if category:
        out = [r for r in out if r["category"] == category]
    return out


def ha_lights():
    """Legacy view: the togglable tiles only, in the pre-category shape.

    The app shipped on the TV paints every row as an on/off tile and toggles it
    on Enter, so read-only entities are dropped here — otherwise a selected
    sensor would render as a dead switch. /v1/entities carries the full selection.
    """
    return [r for r in ha_entity_view() if "toggle" in r["capabilities"]]


# The setup wizard offers every entity that can render as a tile. Domains that
# are pure bookkeeping (or that HA exposes but nothing belongs on a TV) are
# dropped, so the picker lists what a dashboard can actually use — grouped by
# the same category the app renders, never by domain.
WIZARD_EXCLUDED_DOMAINS = {
    "automation", "zone", "group", "tag", "persistent_notification", "todo",
    "calendar", "event", "notify", "conversation", "tts", "stt", "remote",
    "text", "input_text", "input_button", "input_datetime", "time", "date",
    "datetime", "image", "counter", "timer", "schedule", "update", "person",
    "device_tracker", "sun", "valve", "assist_satellite",
}


def ha_entities():
    """Every tileable entity, classified, for the setup wizard's picker.

    Returns the same `category` the app renders with, so the wizard and the TV
    agree on what a tile is — the picker no longer assumes light/switch.
    """
    states = _ha_request("/api/states") or []
    out = []
    for s in states:
        eid = (s or {}).get("entity_id") or ""
        dom = eid.split(".", 1)[0]
        if not dom or dom in WIZARD_EXCLUDED_DOMAINS:
            continue
        rec = entity_record(s)
        out.append({
            "entity_id": rec["entity_id"],
            "name": rec["name"],
            "domain": dom,
            "category": rec["category"],
            "device_class": rec["device_class"],
            "state": rec["state"],
        })
    out.sort(key=lambda x: (x["category"] or "", x["name"].lower()))
    return out


def _mac_from_arp(ip):
    try:
        subprocess.run(["ping", "-c", "1", "-W", "1", ip], capture_output=True, timeout=3)
    except Exception:
        pass
    try:
        for line in open("/proc/net/arp"):
            parts = line.split()
            if len(parts) >= 4 and parts[0] == ip and parts[3] != "00:00:00:00:00:00":
                return parts[3]
    except Exception:
        pass
    return ""


def discover_tvs(timeout=3):
    """SSDP-discover Samsung TVs on the LAN (needs host networking). Returns ip/name/mac.
    Multicast does not traverse Docker's default bridge - run the add-on/container with
    host networking for this to find anything."""
    import socket, re as _re, urllib.request as _ur, time as _t
    msg = "\r\n".join(["M-SEARCH * HTTP/1.1", "HOST:239.255.255.250:1900",
                         'MAN:"ssdp:discover"', "MX:2", "ST:ssdp:all", "", ""])
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.settimeout(timeout)
    try:
        sock.sendto(msg.encode(), ("239.255.255.250", 1900))
    except Exception:
        pass
    locs = {}
    t0 = _t.time()
    while _t.time() - t0 < timeout:
        try:
            data, addr = sock.recvfrom(65535)
        except Exception:
            break
        m = _re.search(r"LOCATION:\s*(\S+)", data.decode(errors="ignore"), _re.I)
        if m:
            locs.setdefault(addr[0], set()).add(m.group(1))
    sock.close()
    out = []
    for ip, urls in locs.items():
        name = None; samsung = False
        for u in urls:
            try:
                xml = _ur.urlopen(_ur.Request(u, headers={"User-Agent": "UPnP/1.0"}), timeout=4).read().decode(errors="ignore")
                fn = _re.search(r"<friendlyName>([^<]+)", xml, _re.I)
                mn = _re.search(r"<manufacturer>([^<]+)", xml, _re.I)
                if fn and not name:
                    name = fn.group(1)
                if mn and "samsung" in mn.group(1).lower():
                    samsung = True
            except Exception:
                pass
        if samsung or (name and "tv" in name.lower()):
            out.append({"ip": ip, "name": name or ip, "mac": _mac_from_arp(ip)})
    return out


def ha_light_toggle(entity_id):
    _ha_request("/api/services/homeassistant/toggle", "POST", {"entity_id": entity_id})
    return True


CONTROL_ACTIONS = {
    "launch_app": lambda b: control_launch_app(b["app_id"]),
    "close_app": lambda b: control_close_app(b["app_id"]),
    "power_on": lambda b: control_power_on(),
    "power_off": lambda b: control_power_off(),
    "pair": lambda b: control_pair(),
    # wipe the stored token so the next pair starts clean (a stale token is refused
    # with 1005 and the TV never gets as far as showing its Allow dialog)
    "forget_pairing": lambda b: (_clear_token(), {"ok": True, "token": ""})[1],
    "pairing_status": lambda b: {
        "has_token": bool(TV_TOKEN),
        "token_len": len(TV_TOKEN or ""),
        "token_file": TOKEN_FILE,
        "client_name": CLIENT_NAME,
    },
    "send_key": lambda b: _send_remote_key(b["key"]),
}


# (dev diagnostic dashboard removed - production UI is Home Assistant)


# Latest telemetry report POSTed by the ON-TV Tizen app (tizen.tvchannel etc.)
_LATEST_REPORT = {}
_report_lock = threading.Lock()
# Ring buffer of the last N reports (for diagnostics / stream inspection)
from collections import deque  # noqa: E402
_REPORT_LOG = deque(maxlen=80)


class AgentState:
    """Latest structured telemetry from the on-TV agent, with liveness."""

    def __init__(self):
        self._lock = threading.Lock()
        self._d = {
            "channel": None, "program": None, "audio": None, "input": None,
            "foregroundApp": None, "system": None, "network": None, "sensors": None,
            "apps": None,
            "seq": None, "event": None, "hidden": None,
            "agent_ts": None, "last_seen": None,
        }

    def update_from_report(self, report):
        """report = raw JSON POSTed by the agent (source == 'tv-agent')."""
        with self._lock:
            for k in ("channel", "program", "audio", "input", "foregroundApp",
                      "system", "network", "sensors", "apps", "seq", "event", "hidden"):
                if k in report and report[k] is not None:
                    self._d[k] = report[k]
            self._d["agent_ts"] = report.get("ts")
            self._d["last_seen"] = datetime.now(timezone.utc).isoformat()

    def snapshot(self):
        with self._lock:
            d = dict(self._d)
        d["online"] = self._is_online(d.get("last_seen"))
        # The on-TV agent can't read the MAC (privileged); fill the known value.
        net = d.get("network")
        if isinstance(net, dict):
            mac = net.get("mac")
            if not mac or (isinstance(mac, str) and mac.lower().startswith("err")):
                net = dict(net)
                net["mac"] = _cfg("tv_mac", "")
                d["network"] = net
        return d

    def last_seen_epoch(self):
        with self._lock:
            ls = self._d.get("last_seen")
        if not ls:
            return 0.0
        try:
            return datetime.fromisoformat(ls).timestamp()
        except Exception:
            return 0.0

    @staticmethod
    def _is_online(last_seen_iso):
        if not last_seen_iso:
            return False
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(last_seen_iso)).total_seconds()
            return age < AGENT_STALE_SECONDS
        except Exception:
            return False


_AGENT = AgentState()

# Command channel: the bridge queues commands, the on-TV agent long-polls
# GET /v1/commands and executes them via Tizen APIs (reliable, unlike the
# TV's remote-control WebSocket which does not register keys on this model).
_cmd_cond = threading.Condition()
_cmd_queue = deque()  # (queued_at, cmd) — see take_commands()
COMMAND_TTL = 30.0  # a command nobody polled for this long is dropped, never delivered
# Consumed by GET /v1/launchmode: "foreground" = open the dashboard, "background" =
# stay hidden (the power-on watchdog sets this). Must exist at module level: the
# handler's `global _LAUNCH_MODE` does not create it, and the first GET used to
# raise NameError and drop the connection.
_LAUNCH_MODE = "foreground"

# --- request counters (GET /v1/diag) ---------------------------------------
# The access log is deliberately silent, which makes "is the TV app actually
# polling /v1/lights?" unanswerable from HA. These counters answer it.
_STARTED_AT = time.time()
_diag_lock = threading.Lock()
_diag = {}


def _note_request(path):
    p = path.split("?")[0]
    with _diag_lock:
        e = _diag.setdefault(p, {"count": 0, "last": 0.0})
        e["count"] += 1
        e["last"] = time.time()
# Power on/off is NOT available locally on this TV (remotepower is NotSupported,
# WoL doesn't wake Wi-Fi, remote-WS keys don't register) - the HA integration does
# power via the samsungtv_smart/SmartThings delegate instead.
AGENT_ACTIONS = {"volume_up", "volume_down", "set_volume", "mute",
                 "channel_up", "channel_down", "tune_channel", "notify", "hide"}
# launch_app is deliberately NOT an agent action: a hidden Tizen app has its
# timers suspended, so handing a "launch yourself" command to it can never work.
# It must run bridge-side through CONTROL_ACTIONS, which talks to the TV's REST API.


def enqueue_command(cmd):
    with _cmd_cond:
        _cmd_queue.append((time.time(), cmd))
        _cmd_cond.notify_all()


def take_commands():
    """Pop all queued commands, discarding any that went stale while the agent was away.

    Without this, a command queued while the TV app was offline (e.g. a notify
    toast) would still fire minutes later and could stomp a fresh one.
    """
    now = time.time()
    out = []
    dropped = 0
    with _cmd_cond:
        while _cmd_queue:
            queued_at, cmd = _cmd_queue.popleft()
            if now - queued_at <= COMMAND_TTL:
                out.append(cmd)
            else:
                dropped += 1
    if dropped:
        print(f"dropped {dropped} stale command(s) older than {COMMAND_TTL:.0f}s", flush=True)
    return out


# Pending toast: set by POST /v1/notify, consumed once by the agent on (re)launch
# via GET /v1/toast. launch()-ing the agent reloads its page, so the message can't
# be carried in-process; the freshly loaded agent fetches it here instead.
_toast_lock = threading.Lock()
_toast = {"data": None, "ts": 0.0}
TOAST_TTL = 60.0


def set_toast(payload):
    with _toast_lock:
        _toast["data"] = payload
        _toast["ts"] = time.time()


def take_toast():
    with _toast_lock:
        if _toast["data"] and (time.time() - _toast["ts"]) < TOAST_TTL:
            d = _toast["data"]
            _toast["data"] = None
            return d
        return None


def make_handler(state: TVState):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # noqa: A003
            pass

        def _send(self, status, body_bytes, content_type):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body_bytes)))
            # API/config responses reflect live state; never let the browser cache them.
            if self.path.startswith("/v1/") or self.path in ("/config", "/", "/index.html"):
                self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body_bytes)

        def do_GET(self):  # noqa: N802
            _note_request(self.path)
            if self.path == "/v1/state":
                snap = state.snapshot()
                snap["agent"] = _AGENT.snapshot()
                snap["power"] = _effective_power(snap)
                self._send(200, json.dumps(snap).encode("utf-8"), "application/json")
            elif self.path.startswith("/img/"):
                import re as _re  # os is imported at module level; a local import here
                # would shadow it for the whole function (UnboundLocalError above)
                name = self.path.split("/img/", 1)[1].split("?")[0]
                if not _re.match(r"^[A-Za-z0-9_.\-]+$", name):
                    name = "cat.png"
                path = os.path.join(IMG_DIR, name)
                if not os.path.exists(path):
                    path = os.path.join(IMG_DIR, "cat.png")
                try:
                    with open(path, "rb") as f:
                        data = f.read()
                    ct = "image/png" if path.endswith(".png") else "image/jpeg" if path.endswith((".jpg", ".jpeg")) else "application/octet-stream"
                    self._send(200, data, ct)
                except Exception:
                    self._send(404, b"no image", "text/plain")
            elif self.path == "/v1/report":
                with _report_lock:
                    r = dict(_LATEST_REPORT)
                self._send(200, json.dumps(r).encode("utf-8"), "application/json")
            elif self.path == "/v1/reportlog":
                with _report_lock:
                    items = list(_REPORT_LOG)
                self._send(200, json.dumps(items).encode("utf-8"), "application/json")
            elif self.path == "/v1/toast":
                t = take_toast()
                self._send(200, json.dumps(t or {}).encode("utf-8"), "application/json")
            elif self.path == "/v1/launchmode":
                global _LAUNCH_MODE
                mode = _LAUNCH_MODE
                _LAUNCH_MODE = "foreground"
                self._send(200, json.dumps({"mode": mode}).encode("utf-8"), "application/json")
            elif self.path == "/v1/config":
                red = {k: v for k, v in _CONFIG.items() if k != "ha_token"}
                red["ha_token_set"] = bool(_CONFIG.get("ha_token"))
                red["lan_ip"] = _lan_ip()
                # Where the config actually lives, and whether it is on disk.
                red["config_file"] = CONFIG_FILE
                red["persisted"] = os.path.exists(CONFIG_FILE)
                if _LAST_SAVE_ERROR:
                    red["save_error"] = _LAST_SAVE_ERROR
                self._send(200, json.dumps(red).encode("utf-8"), "application/json")
            elif self.path == "/config":
                self._send(200, CONFIG_HTML.encode("utf-8"), "text/html; charset=utf-8")
            elif self.path == "/v1/discover_tv":
                try:
                    self._send(200, json.dumps({"tvs": discover_tvs()}).encode("utf-8"), "application/json")
                except Exception as e:  # noqa: BLE001
                    self._send(502, json.dumps({"error": str(e)}).encode("utf-8"), "application/json")
            elif self.path.startswith("/v1/tv_devmode"):
                from urllib.parse import urlparse, parse_qs
                ip = (parse_qs(urlparse(self.path).query).get("ip", [""])[0] or "").strip()
                res = tv_devmode(ip)
                try:
                    res["requester_ip"] = self.client_address[0]
                except Exception:
                    res["requester_ip"] = None
                self._send(200, json.dumps(res).encode("utf-8"), "application/json")
            elif self.path == "/app.wgt":
                try:
                    with open(APP_WGT, "rb") as f:
                        data = f.read()
                    self._send(200, data, "application/octet-stream")
                except Exception:
                    self._send(404, b"app.wgt not bundled in this build", "text/plain")
            elif self.path == "/v1/ha_entities":
                try:
                    self._send(200, json.dumps({"entities": ha_entities()}).encode("utf-8"), "application/json")
                except Exception as e:  # noqa: BLE001
                    self._send(502, json.dumps({"error": str(e)}).encode("utf-8"), "application/json")
            elif self.path == "/v1/lights":
                try:
                    self._send(200, json.dumps({"lights": ha_lights()}).encode("utf-8"), "application/json")
                except Exception as e:  # noqa: BLE001
                    self._send(502, json.dumps({"error": str(e)}).encode("utf-8"), "application/json")
            elif self.path.startswith("/v1/entities"):
                # Classified tiles: any domain, category derived from the entity.
                # ?category=light filters; the app groups by `category`.
                try:
                    from urllib.parse import urlparse, parse_qs
                    cat = (parse_qs(urlparse(self.path).query).get("category", [""])[0] or "").strip() or None
                    self._send(200, json.dumps({"entities": ha_entity_view(cat)}).encode("utf-8"), "application/json")
                except Exception as e:  # noqa: BLE001
                    self._send(502, json.dumps({"error": str(e)}).encode("utf-8"), "application/json")
            elif self.path.startswith("/v1/commands"):
                # long-poll: block up to 25s until a command is queued
                deadline = time.time() + 25
                with _cmd_cond:
                    while not _cmd_queue and time.time() < deadline:
                        _cmd_cond.wait(timeout=max(0.0, deadline - time.time()))
                cmds = take_commands()
                self._send(200, json.dumps({"commands": cmds}).encode("utf-8"), "application/json")
            elif self.path == "/" or self.path == "/index.html":
                self._send(200, CONFIG_HTML.encode("utf-8"), "text/html; charset=utf-8")
            elif self.path == "/v1/ping":
                self._send(200, json.dumps({"service": "yast-bridge", "ok": True}).encode("utf-8"), "application/json")
            elif self.path == "/v1/diag":
                now = time.time()
                with _diag_lock:
                    reqs = {
                        p: {"count": v["count"], "seconds_since_last": round(now - v["last"], 1)}
                        for p, v in sorted(_diag.items())
                    }
                payload = {
                    "uptime_s": round(now - _STARTED_AT, 1),
                    "agent_online": bool(_AGENT.snapshot().get("online")),
                    "queued_commands": len(_cmd_queue),
                    "toast_pending": bool(_toast["data"]) and (now - _toast["ts"]) < TOAST_TTL,
                    "launch_mode": _LAUNCH_MODE,
                    "requests": reqs,
                    # pairing truth, so a prompt storm can be diagnosed without ssh
                    "pairing": {
                        "has_token": bool(TV_TOKEN),
                        "token_len": len(TV_TOKEN or ""),
                        "token_file": TOKEN_FILE,
                        "client_name": CLIENT_NAME,
                    },
                }
                self._send(200, json.dumps(payload).encode("utf-8"), "application/json")
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self):  # noqa: N802
            _note_request(self.path)
            if self.path == "/v1/launchmode":
                try:
                    n = int(self.headers.get("Content-Length", 0))
                    body = json.loads(self.rfile.read(n) or b"{}")
                    global _LAUNCH_MODE
                    _LAUNCH_MODE = str(body.get("mode", "foreground"))
                    self._send(200, json.dumps({"ok": True, "mode": _LAUNCH_MODE}).encode("utf-8"), "application/json")
                except Exception as e:  # noqa: BLE001
                    self._send(400, json.dumps({"error": str(e)}).encode("utf-8"), "application/json")
                return
            if self.path == "/v1/config":
                try:
                    n = int(self.headers.get("Content-Length", 0))
                    body = json.loads(self.rfile.read(n) or b"{}")
                    for k in ("tv_ip", "tv_mac", "ha_url", "main_lights", "main_entities"):
                        if k in body:
                            _CONFIG[k] = body[k]
                    if "notify_seconds" in body:
                        try:
                            _CONFIG["notify_seconds"] = max(1, int(body["notify_seconds"]))
                        except (TypeError, ValueError):
                            pass
                    if "auto_load" in body:
                        _CONFIG["auto_load"] = bool(body["auto_load"])
                    if body.get("ha_token"):
                        _CONFIG["ha_token"] = body["ha_token"]
                    if not _save_config():
                        self._send(
                            500,
                            json.dumps(
                                {
                                    "error": "config could not be written to disk",
                                    "config_file": CONFIG_FILE,
                                    "detail": _LAST_SAVE_ERROR,
                                }
                            ).encode("utf-8"),
                            "application/json",
                        )
                        return
                    self._send(
                        200,
                        json.dumps({"ok": True, "config_file": CONFIG_FILE, "persisted": True}).encode("utf-8"),
                        "application/json",
                    )
                except Exception as e:  # noqa: BLE001
                    self._send(400, json.dumps({"error": str(e)}).encode("utf-8"), "application/json")
                return
            if self.path == "/v1/lights/toggle":
                try:
                    n = int(self.headers.get("Content-Length", 0))
                    body = json.loads(self.rfile.read(n) or b"{}")
                    eid = body.get("entity_id")
                    if not eid:
                        raise ValueError("missing 'entity_id'")
                    ha_light_toggle(str(eid))
                    self._send(200, json.dumps({"ok": True}).encode("utf-8"), "application/json")
                except Exception as e:  # noqa: BLE001
                    self._send(502, json.dumps({"error": str(e)}).encode("utf-8"), "application/json")
                return
            if self.path == "/v1/cast":
                # Display media on the TV via the agent: {"url": "...", "kind": image|video|web, "duration": N}
                try:
                    n = int(self.headers.get("Content-Length", 0))
                    body = json.loads(self.rfile.read(n) or b"{}")
                    url = body.get("url", "")
                    if not url:
                        raise ValueError("missing 'url'")
                    payload = {"url": str(url), "duration": int(body.get("duration", 30) or 30)}
                    if body.get("kind"):
                        payload["kind"] = str(body["kind"])
                    set_toast(payload)
                    cmd = {"action": "notify"}
                    cmd.update(payload)
                    enqueue_command(cmd)
                    self._send(200, json.dumps({"ok": True, "queued": True}).encode("utf-8"), "application/json")
                except Exception as e:
                    self._send(400, json.dumps({"error": str(e)}).encode("utf-8"), "application/json")
                return
            if self.path == "/v1/notify":
                # Simple toast API (e.g. for Home Assistant): {"message": "..."}
                try:
                    n = int(self.headers.get("Content-Length", 0))
                    body = json.loads(self.rfile.read(n) or b"{}")
                    msg = body.get("message", body.get("text", ""))
                    if not msg:
                        raise ValueError("missing 'message'")
                    duration = int(body.get("duration") or _cfg("notify_seconds", 8) or 8)
                    set_toast({"message": str(msg), "duration": duration})
                    # A hidden Tizen app has its timers suspended, so it can never
                    # poll the command queue by itself: launch it. Its boot path
                    # fetches /v1/toast and paints the message. Only fall back to the
                    # queue when the launch fails, otherwise the queued `notify` would
                    # cause a second relaunch that wipes the toast it just showed.
                    launched, launch_error = launch_agent()
                    if not launched:
                        enqueue_command({"action": "notify", "message": str(msg), "duration": duration})
                    online = _AGENT.snapshot().get("online")
                    self._send(
                        200,
                        json.dumps(
                            {
                                "ok": True,
                                "launched": launched,
                                "queued": not launched,
                                "launch_error": launch_error,
                                "agent_online": bool(online),
                            }
                        ).encode("utf-8"),
                        "application/json",
                    )
                except Exception as e:
                    self._send(400, json.dumps({"error": str(e)}).encode("utf-8"), "application/json")
                return
            if self.path == "/v1/report":
                try:
                    n = int(self.headers.get("Content-Length", 0))
                    data = json.loads(self.rfile.read(n) or b"{}")
                    with _report_lock:
                        _LATEST_REPORT.clear()
                        _LATEST_REPORT.update(data)
                        _LATEST_REPORT["received_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ")
                        stored = dict(_LATEST_REPORT)
                        _REPORT_LOG.append(stored)
                    if isinstance(data, dict) and data.get("source") == "tv-agent":
                        _AGENT.update_from_report(data)
                    self._send(200, json.dumps({"ok": True, "received_at": stored["received_at"]}).encode("utf-8"), "application/json")
                except Exception as e:
                    self._send(400, json.dumps({"error": str(e)}).encode("utf-8"), "application/json")
                return
            if self.path != "/v1/control":
                self._send(404, b"not found", "text/plain")
                return
            try:
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"{}")
                action = body.get("action")
                if action in AGENT_ACTIONS:
                    # Reliable path: hand to the on-TV agent (executes via Tizen APIs)
                    enqueue_command(body)
                    online = _AGENT.snapshot().get("online")
                    self._send(200, json.dumps({"ok": True, "queued": True, "agent_online": bool(online)}).encode("utf-8"), "application/json")
                elif action in CONTROL_ACTIONS:
                    res = CONTROL_ACTIONS[action](body)
                    if isinstance(res, str):
                        self._send(200, json.dumps({"ok": True, "token": res}).encode("utf-8"), "application/json")
                    elif isinstance(res, dict):
                        reason = res.get("reason")
                        if reason in (None, "already_off"):
                            status = 200
                        else:
                            status = 409   # power_unknown: refused rather than guessed
                        self._send(status, json.dumps({"ok": bool(res.get("sent")), **res}).encode("utf-8"), "application/json")
                    else:
                        self._send(200, json.dumps({"ok": bool(res)}).encode("utf-8"), "application/json")
                else:
                    raise ValueError(f"unknown action: {action!r}")
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}).encode("utf-8"), "application/json")

    return Handler


def main():
    state = TVState()
    stop_event = threading.Event()

    poller = threading.Thread(target=poller_loop, args=(state, stop_event), daemon=True)
    poller.start()

    server = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), make_handler(state))
    print(f"reader.py listening on http://0.0.0.0:{HTTP_PORT}/  (dashboard /, JSON /v1/state, POST /v1/control)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        server.shutdown()


if __name__ == "__main__":
    main()