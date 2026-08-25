#!/usr/bin/env python3
"""Trailer Catcher — the local half of the browser-driven trailer grabber.

Tony browses YouTube normally, finds a trailer, clicks a button in HIS OWN
unpacked Chrome extension. The extension posts the signed media URLs here; this
service downloads them, muxes, files the result into the library and marks the
item done in the Trailer Grabber's state file.

⚠️ **WHY A BROWSER AT ALL.** yt-dlp cannot run YouTube's BotGuard JavaScript, so
its InnerTube player request gets challenged once an IP's volume is noticed
(~25 items/day, proven 2026-08-23). Chrome runs that JS natively and is served.
So Chrome does every bit of the cryptography — signatures, the `n` transform,
attestation — and the extension just reads the resulting URL off the wire. We
compute nothing, which is also why this does not rot each time YouTube changes
their player.

⚠️ **AND WHY MANUAL.** Driving a browser at volume would still be volume. Doing
it by hand while genuinely browsing has no bot-check and no rate limit to dodge,
because there is nothing automated to detect. Slower, and actually finishes.

⚠️ Extension is UNPACKED and self-written on purpose. Third-party YouTube
downloader extensions have a long history of being acquired and turned into
adware in a silent update, and an extension sees every page you visit.

    Run:      python3 trailer_catcher.py        →  http://localhost:5961
    Chrome:   load the extension unpacked from ./trailer_catcher_extension/
"""
from flask import Flask, request, jsonify, render_template_string
from pathlib import Path
from datetime import datetime
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading

PORT = 5961          # cams are on 5959 / 5960

# ⚠️ Kept in step with trailer_refresh.py BY HAND — importing it would execute a
# script built around argparse. If MIN_HEIGHT or STATE move there, move them here.
STATE = os.path.expanduser("~/.local/share/docflix/trailer_refresh.json")
MIN_HEIGHT = 720

app = Flask(__name__)
_lock = threading.Lock()
_armed = {"path": None}          # which library item the next capture belongs to
_recent = []                     # last few results, newest first


# ── state file ──────────────────────────────────────────────────────────────

def load_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"items": {}, "updated": None}


def save_state(st):
    """Atomic write — a half-written state file would lose 1,900 done markers."""
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    st["updated"] = datetime.now().isoformat(timespec="seconds")
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=2)
    os.replace(tmp, STATE)


def worklist(limit=400, query=""):
    """Items still wanting a better trailer, worst first."""
    st = load_state()
    rows = []
    for path, info in st.get("items", {}).items():
        if info.get("status") not in ("pending", "source_limited"):
            continue
        title = info.get("title") or ""
        if query and query.lower() not in title.lower():
            continue
        rows.append({
            "path": path,
            "title": title,
            "year": info.get("year"),
            "kind": info.get("kind"),
            "height": info.get("height") or 0,
            "status": info.get("status"),
        })
    rows.sort(key=lambda r: (r["height"], r["title"]))
    return rows[:limit]


# ── download ────────────────────────────────────────────────────────────────

def strip_range(url: str) -> str:
    """Drop the byte-range params so we fetch the whole stream, not one segment.

    ⚠️ The player requests DASH segments, so every captured URL carries
    &range=A-B (and often &rn=/&rbuf=). Fetch one of those verbatim and you get
    a few seconds of video and no error to tell you why.
    """
    url = re.sub(r"&(range|rn|rbuf)=[^&]*", "", url)
    return url


def probe_height(path):
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=height", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=30).stdout.strip()
        return int(out.split(",")[0])
    except Exception:
        return 0


def mux_local(files, dest: Path):
    """Mux files Chrome already downloaded into *dest*.

    ⚠️ The service does NOT fetch from googlevideo. Verified 2026-08-25: those
    URLs return 403 to any process other than the browser that requested them —
    with and without the range params, with Referer/Origin set, from the same
    machine seconds later. The `spc`/`svpuc`/`vprv` parameters are Google
    binding the URL to the browser SESSION. Chrome fetches; we only mux.
    """
    vids = [f for f in files if f.get("kind") == "video"]
    auds = [f for f in files if f.get("kind") == "audio"]
    if not vids:
        return None, "no video file from the browser"
    for f in files:
        if not Path(f["path"]).exists():
            return None, f"browser reported {f['path']} but it is not there"

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="trailercatch-"))
    try:
        out = tmp / f"out{dest.suffix or '.mkv'}"
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
               "-i", vids[0]["path"]]
        if auds:
            cmd += ["-i", auds[0]["path"], "-map", "0:v:0", "-map", "1:a:0"]
        cmd += ["-c", "copy", str(out)]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        if r.returncode != 0 or not out.exists() or out.stat().st_size < 100_000:
            return None, (r.stderr or "ffmpeg produced nothing").strip()[-400:]
        h = probe_height(out)
        shutil.move(str(out), str(dest))
        # Tidy up Chrome's staging copies once they're safely muxed.
        for f in files:
            Path(f["path"]).unlink(missing_ok=True)
        return h, None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def fetch_and_mux(video_url, audio_url, dest: Path, ua: str):
    """Pull the streams with ffmpeg and mux to *dest*.

    ⚠️ YouTube serves DASH: video and audio are SEPARATE URLs. Capturing only
    one gives a silent clip — the classic first-version bug. Both are required.
    ⚠️ googlevideo URLs are bound to the requesting IP and expire in a few hours,
    so this must run on the same machine as the browser, promptly.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="trailercatch-"))
    try:
        # ⚠️ MUX INTO THE CONTAINER THE DESTINATION NAME PROMISES. This used to
        # always write Matroska and then move it onto whatever name the state
        # file held — and 7 of the 1,385 pending items are .mp4, so those would
        # have become MP4-named files containing Matroska. That is the exact
        # container/extension mismatch that already put ~44% of this library
        # into a wrong wrapper once, running the other way.
        # If a codec genuinely cannot go in that container ffmpeg fails, the
        # backup is restored, and the error is reported — an honest failure
        # beats a file that lies about what it is.
        out = tmp / f"out{dest.suffix or '.mkv'}"
        # ⚠️ googlevideo will refuse a bare GET even with a valid signed URL —
        # it checks Origin/Referer. Without these ffmpeg reports the unhelpful
        # "Error opening input files: Input/output error" and nothing else.
        hdrs = ("Referer: https://www.youtube.com/\r\n"
                "Origin: https://www.youtube.com\r\n")
        net = ["-user_agent", ua, "-headers", hdrs,
               "-reconnect", "1", "-reconnect_streamed", "1",
               "-reconnect_delay_max", "5"]
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
               *net, "-i", strip_range(video_url)]
        if audio_url:
            cmd += [*net, "-i", strip_range(audio_url)]
            cmd += ["-map", "0:v:0", "-map", "1:a:0"]
        else:
            cmd += ["-map", "0"]
        cmd += ["-c", "copy", str(out)]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        if r.returncode != 0 or not out.exists() or out.stat().st_size < 100_000:
            return None, (r.stderr or "ffmpeg produced nothing").strip()[-400:]
        h = probe_height(out)
        shutil.move(str(out), str(dest))
        return h, None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── routes ──────────────────────────────────────────────────────────────────

# ── MSE capture ─────────────────────────────────────────────────────────────
# The page-context hook posts the raw DASH segments it saw go into the player.
# ⚠️ We never fetch from googlevideo — those URLs 403 to anything but the
# browser session that requested them (verified 2026-08-25, including against
# Chrome's own download manager). These bytes already arrived; we just mux them.

_MSE_DIR = Path(tempfile.gettempdir()) / "trailer_catcher_mse"


@app.after_request
def _cors(resp):
    # ⚠️ The POST comes from a page on https://www.youtube.com. http://127.0.0.1
    # counts as a trustworthy origin so mixed-content blocking does not apply,
    # but CORS still does — without these the browser drops the upload silently.
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "POST, GET, OPTIONS"
    return resp


@app.route("/api/mse", methods=["POST", "OPTIONS"])
def api_mse():
    """Receive one concatenated stream (video or audio) as raw bytes."""
    if request.method == "OPTIONS":
        return ("", 204)
    kind = request.args.get("kind", "video")
    vid = re.sub(r"[^A-Za-z0-9_-]", "", request.args.get("vid", "x"))[:24]
    _MSE_DIR.mkdir(parents=True, exist_ok=True)
    p = _MSE_DIR / f"{vid}.{kind}"
    p.write_bytes(request.get_data())
    return jsonify(ok=True, bytes=p.stat().st_size)


@app.route("/api/mse_done", methods=["POST", "OPTIONS"])
def api_mse_done():
    """Mux what was uploaded and file it against the armed title."""
    if request.method == "OPTIONS":
        return ("", 204)
    data = request.json or {}
    vid = re.sub(r"[^A-Za-z0-9_-]", "", data.get("vid", "x"))[:24]
    v = _MSE_DIR / f"{vid}.video"
    a = _MSE_DIR / f"{vid}.audio"
    if not v.exists():
        return jsonify(ok=False, error="no video bytes arrived"), 400
    files = [{"kind": "video", "path": str(v)}]
    if a.exists():
        files.append({"kind": "audio", "path": str(a)})
    return _file_capture(files)


@app.route("/api/arm", methods=["POST"])
def api_arm():
    """Point the next capture at a library item."""
    _armed["path"] = (request.json or {}).get("path") or None
    info = load_state().get("items", {}).get(_armed["path"] or "", {})
    return jsonify(ok=True, path=_armed["path"], title=info.get("title"))


@app.route("/api/armed")
def api_armed():
    p = _armed["path"]
    info = load_state().get("items", {}).get(p or "", {})
    return jsonify(path=p, title=info.get("title"), height=info.get("height"),
                   year=info.get("year"))


@app.route("/api/capture", methods=["POST"])
def api_capture():
    """The extension posts here. {video_url, audio_url, page_title, page_url}"""
    data = request.json or {}
    video_url = data.get("video_url")
    audio_url = data.get("audio_url")
    ua = data.get("ua") or "Mozilla/5.0"
    # Debug drop of the last payload — googlevideo URLs are opaque and
    # short-lived, so having the exact one is the only way to test a failure.
    try:
        with open("/tmp/trailer_catcher_last.json", "w") as _f:
            json.dump(data, _f, indent=2)
    except Exception:
        pass
    if not video_url:
        return jsonify(ok=False, error="no video_url — is the player running?"), 400

    return _file_capture(data.get("files") or [],
                         video_url=video_url, audio_url=audio_url, ua=ua)


def _file_capture(files, video_url=None, audio_url=None, ua="Mozilla/5.0"):
    """Mux a capture into the armed library slot and update the state file.

    Shared by both paths: the MSE upload (which works) and the retired URL fetch.
    """
    with _lock:
        target = _armed["path"]
        if not target:
            return jsonify(ok=False, error="nothing armed — pick a title first"), 409
        st = load_state()
        info = st.get("items", {}).get(target)
        if not info:
            return jsonify(ok=False, error="armed item is gone from the state file"), 409

        dest = Path(target)
        backup = None
        if dest.exists():                       # keep the old one until we win
            backup = dest.with_suffix(dest.suffix + ".prev")
            shutil.move(str(dest), str(backup))

        if files:
            height, err = mux_local(files, dest)
        else:
            height, err = fetch_and_mux(video_url, audio_url, dest, ua)
        if err:
            if backup:
                shutil.move(str(backup), str(dest))   # put it back, lose nothing
            _recent.insert(0, {"title": info.get("title"), "ok": False, "msg": err})
            return jsonify(ok=False, error=err), 500

        # ⚠️ Only claim "done" if it actually beat what was there. A 360p grab
        # that overwrites a 360p file and marks it done is worse than not trying.
        was = info.get("height") or 0
        if height >= MIN_HEIGHT:
            info.update(status="done", was=was, height=height,
                        done_at=datetime.now().isoformat(timespec="seconds"))
        elif height > was:
            info.update(status="source_limited", best_available=height,
                        height=height, was=was)
        else:
            if backup:                       # worse than what we had — revert
                dest.unlink(missing_ok=True)
                shutil.move(str(backup), str(dest))
            _recent.insert(0, {"title": info.get("title"), "ok": False,
                               "msg": f"got {height}p, kept existing {was}p"})
            return jsonify(ok=False, error=f"{height}p is not better than {was}p"), 200

        save_state(st)
        if backup:
            Path(backup).unlink(missing_ok=True)
        _recent.insert(0, {"title": info.get("title"), "ok": True,
                           "msg": f"{was}p → {height}p"})
        del _recent[12:]
        _armed["path"] = None                # disarm so the next click can't misfile
        return jsonify(ok=True, title=info.get("title"), height=height, was=was)


PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>Trailer Catcher</title>
<style>
*{box-sizing:border-box}body{background:#16161e;color:#ddd;font-family:system-ui,sans-serif;margin:0;padding:18px}
h1{font-size:1.2em;margin:0 0 4px}.sub{color:#888;font-size:.85em;margin-bottom:14px}
.armed{background:#1f2b1f;border:1px solid #3a5a3a;border-radius:8px;padding:10px 12px;margin-bottom:14px}
.armed.none{background:#2b1f1f;border-color:#5a3a3a}
input{background:#22222c;border:1px solid #383848;color:#eee;padding:7px 9px;border-radius:6px;width:280px}
table{width:100%;border-collapse:collapse;font-size:.9em}
th{text-align:left;color:#888;font-weight:normal;padding:6px 8px;border-bottom:1px solid #333}
td{padding:5px 8px;border-bottom:1px solid #232330}tr:hover{background:#1e1e28}
.h{color:#e08a5a;font-variant-numeric:tabular-nums}
button{background:#2a3a5a;border:1px solid #3a5a8a;color:#cde;padding:4px 10px;border-radius:5px;cursor:pointer}
button:hover{background:#35507a}a{color:#7aa8e0}
.r{font-size:.85em;padding:3px 0}.ok{color:#7ec87e}.bad{color:#e07a7a}
</style></head><body>
<h1>Trailer Catcher</h1>
<div class="sub">Arm a title, find its trailer on YouTube, click the extension button.</div>
<div id="armed" class="armed none">nothing armed</div>
<input id="q" placeholder="filter titles…" oninput="load()">
<span class="sub" id="count"></span>
<div id="recent"></div>
<table><thead><tr><th>now</th><th>title</th><th>year</th><th></th><th></th></tr></thead>
<tbody id="rows"></tbody></table>
<script>
async function refreshArmed(){
  const a = await (await fetch('api/armed')).json();
  const el = document.getElementById('armed');
  if(a.path){ el.className='armed'; el.innerHTML='<b>armed:</b> '+a.title+' ('+(a.height||'?')+'p) — now go find it'; }
  else { el.className='armed none'; el.textContent='nothing armed — pick a title below'; }
}
async function arm(p){ await fetch('api/arm',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:p})}); refreshArmed(); }
async function load(){
  const q = document.getElementById('q').value;
  const rows = await (await fetch('api/worklist?q='+encodeURIComponent(q))).json();
  document.getElementById('count').textContent = '  '+rows.length+' shown';
  document.getElementById('rows').innerHTML = rows.map(r=>{
    const s = encodeURIComponent(r.title+' '+(r.year||'')+' trailer');
    return '<tr><td class="h">'+(r.height||'?')+'p</td><td>'+r.title+'</td><td>'+(r.year||'')+
      '</td><td><a href="https://www.youtube.com/results?search_query='+s+'" target="_blank">search ▸</a></td>'+
      '<td><button onclick="arm(\\''+r.path.replace(/'/g,"\\\\'")+'\\')">arm</button></td></tr>';
  }).join('');
}
async function recent(){
  const rs = await (await fetch('api/recent')).json();
  document.getElementById('recent').innerHTML = rs.map(r=>
    '<div class="r '+(r.ok?'ok':'bad')+'">'+(r.ok?'✓':'✗')+' '+r.title+' — '+r.msg+'</div>').join('');
}
load(); refreshArmed(); recent(); setInterval(()=>{refreshArmed();recent();}, 3000);
</script></body></html>"""


@app.route("/")
def index():
    return render_template_string(PAGE)


@app.route("/api/worklist")
def api_worklist():
    return jsonify(worklist(query=request.args.get("q", "")))


@app.route("/api/recent")
def api_recent():
    return jsonify(_recent)


if __name__ == "__main__":
    print(f"Trailer Catcher  →  http://localhost:{PORT}")
    app.run(host="127.0.0.1", port=PORT, threaded=True)
