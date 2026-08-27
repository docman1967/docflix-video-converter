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
from flask import Flask, request, jsonify, render_template_string, send_file
from pathlib import Path
from datetime import datetime
import json
import os
import re
import shutil
import subprocess
import tempfile
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# ⚠️ Import the encode preset rather than restating it. One definition can't drift.
from modules.trailer_downloader import VIDEO_CODECS, safe_filename  # noqa: E402

PORT = 5961          # cams are on 5959 / 5960

# ⚠️ Kept in step with trailer_refresh.py BY HAND — importing it would execute a
# script built around argparse. If MIN_HEIGHT or STATE move there, move them here.
STATE = os.path.expanduser("~/.local/share/docflix/trailer_refresh.json")
# Unarmed catches land here — for content still in the encode folder that has no
# library entry to arm against yet. Move them in when the title lands.
STAGING = Path(os.path.expanduser("~/downloads/dst/Process/Trailers"))
# ⚠️ Unarmed catches land here RAW and un-encoded, and wait. Tony catches four or
# five on YouTube without breaking flow, then comes to the page once, names them
# and encodes the batch. Naming at catch time meant tab-switching per trailer and
# a name that persisted into the NEXT catch if you forgot to change it.
PENDING = STAGING / ".pending"
PENDING_IX = PENDING / "index.json"
MIN_HEIGHT = 720

app = Flask(__name__)
_lock = threading.Lock()
_armed = {"path": None}          # which library item the next capture belongs to
_recent = []                     # last few results, newest first
_staging_name = {"name": ""}     # legacy: optional name for the next UNARMED catch


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


def mux_local(files, dest: Path, encode=True):
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
        else:
            # ⚠️ Explicit maps, never "-map 0". That also picks up ATTACHMENT
            # streams (embedded cover art), and muxing one into matroska fails
            # with "incorrect codec parameters" because the mimetype tag does
            # not survive. Inherited from trailer_downloader, which learned it
            # the hard way.
            cmd += ["-map", "0:v:0", "-map", "0:a?"]

        # ⚠️ ENCODE TO THE LIBRARY STANDARD, do not stream-copy.
        #
        # YouTube serves h264 or AV1 and NEVER HEVC, so this is always a real
        # transcode of an already-lossy clip. That buys nothing on quality — it
        # is done purely for LIBRARY UNIFORMITY, which is a trade Tony makes
        # deliberately everywhere else (see the 384k AC-3 decision). NVENC makes
        # it cost seconds.
        #
        # ⚠️ The preset is IMPORTED, not copied. trailer_downloader's comment on
        # it: "CQ 32 + p4 + 10-bit + lookahead 32 is not a guess — it is
        # byte-for-byte the Media Suite's library standard. Do not 'improve' it."
        # Two copies of an encode standard would drift; one cannot.
        if encode:
            cmd += VIDEO_CODECS["h265"] + ["-c:a", "aac", "-b:a", "192k"]
        else:
            # Pending catches stay RAW — fast, and Tony can preview them before
            # deciding what they are. The encode happens once he names it.
            cmd += ["-c", "copy"]
        cmd += ["-map_metadata", "-1", "-map_chapters", "-1", str(out)]
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
    return _file_capture(files, page_title=data.get("title", ""))


@app.route("/api/pending")
def api_pending():
    """Caught but not yet named or encoded."""
    rows = [r for r in load_pending() if Path(r["file"]).exists()]
    return jsonify(rows)


@app.route("/api/pending/<ident>/preview")
def api_pending_preview(ident):
    """Serve the raw catch so it can be played in the browser before naming.

    ⚠️ The point of the pending step: look at what you caught before deciding
    what it is. Teasers, fan edits and the wrong cut all look identical in a
    filename.
    """
    for r in load_pending():
        if r["id"] == ident and Path(r["file"]).exists():
            return send_file(r["file"], mimetype="video/x-matroska")
    return ("not found", 404)


@app.route("/api/pending/discard", methods=["POST"])
def api_pending_discard():
    ident = (request.json or {}).get("id")
    rows = load_pending()
    keep = []
    for r in rows:
        if r["id"] == ident:
            Path(r["file"]).unlink(missing_ok=True)
        else:
            keep.append(r)
    save_pending(keep)
    return jsonify(ok=True)


@app.route("/api/pending/encode", methods=["POST"])
def api_pending_encode():
    """Name it, encode to the library standard, file it, drop it from pending.

    Accepts either a `name` (stages as <name>-trailer.mkv) or a `path` (files
    into that library item and updates the state file, same as an armed catch).
    """
    data = request.json or {}
    ident = data.get("id")
    row = next((r for r in load_pending() if r["id"] == ident), None)
    if not row or not Path(row["file"]).exists():
        return jsonify(ok=False, error="that catch is gone"), 404

    files = [{"kind": "video", "path": row["file"]}]
    target = data.get("path")
    if target:
        # Same path as an armed catch — reuse it wholesale rather than
        # reimplementing the state-file bookkeeping and the do-not-overwrite guard.
        _armed["path"] = target
        resp = _file_capture(files)
        body = resp[0].get_json() if isinstance(resp, tuple) else resp.get_json()
        if body.get("ok"):
            _drop_pending(ident)
        return resp

    name = safe_filename((data.get("name") or "").strip())
    if not name:
        return jsonify(ok=False, error="give it a name first"), 400
    if not name.lower().endswith("-trailer"):
        name += "-trailer"
    dest = STAGING / f"{name}.mkv"
    n = 2
    while dest.exists():
        dest = STAGING / f"{name} ({n}).mkv"
        n += 1
    height, err = mux_local(files, dest, encode=True)
    if err:
        _recent.insert(0, {"title": name, "ok": False, "msg": err})
        return jsonify(ok=False, error=err), 500
    _drop_pending(ident)
    _recent.insert(0, {"title": name, "ok": True, "msg": f"{height}p → staged"})
    del _recent[12:]
    return jsonify(ok=True, title=name, height=height, staged=str(dest))


def _drop_pending(ident):
    """Remove from the index. mux_local already deleted the raw source file."""
    rows = [r for r in load_pending() if r["id"] != ident]
    save_pending(rows)


@app.route("/api/staging_name", methods=["POST"])
def api_staging_name():
    """Name the next unarmed catch, so it lands matching the library convention."""
    _staging_name["name"] = ((request.json or {}).get("name") or "").strip()
    return jsonify(ok=True, name=_staging_name["name"])


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
                   year=info.get("year"), staging=str(STAGING),
                   staging_name=_staging_name["name"])


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
                         video_url=video_url, audio_url=audio_url, ua=ua,
                         page_title=data.get("page_title", ""))


def load_pending():
    try:
        return json.loads(PENDING_IX.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_pending(rows):
    PENDING.mkdir(parents=True, exist_ok=True)
    tmp = PENDING_IX.with_suffix(".tmp")
    tmp.write_text(json.dumps(rows, indent=2))
    os.replace(tmp, PENDING_IX)


def probe_meta(path):
    """height + duration, for showing the user what they actually caught."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=height", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=30).stdout.split()
        return int(float(out[0])), int(float(out[1]))
    except Exception:
        return 0, 0


def _free_catch(files, page_title):
    """Nothing armed — park the catch in the pending list, raw and unnamed.

    ⚠️ Deliberately does NOT encode or name here. Tony catches four or five on
    YouTube without breaking flow, then comes to the page once and does the
    naming and encoding as a batch. Naming at catch time meant switching tabs
    per trailer, and a name that silently persisted into the next catch.
    """
    PENDING.mkdir(parents=True, exist_ok=True)
    ident = datetime.now().strftime("%Y%m%d-%H%M%S")
    raw = PENDING / f"{ident}.mkv"
    height, err = mux_local(files, raw, encode=False)
    if err:
        _recent.insert(0, {"title": page_title[:60], "ok": False, "msg": err})
        return jsonify(ok=False, error=err), 500

    h, dur = probe_meta(raw)
    yt = re.sub(r"\s*-\s*YouTube\s*$", "", page_title or "").strip()
    yt = re.sub(r"^\(\d+\)\s*", "", yt)     # Chrome's unread-count prefix
    rows = load_pending()
    rows.insert(0, {"id": ident, "file": str(raw), "yt_title": yt,
                    "height": h, "duration": dur,
                    "caught_at": datetime.now().isoformat(timespec="seconds")})
    save_pending(rows)
    _recent.insert(0, {"title": yt[:60] or ident, "ok": True,
                       "msg": f"{h}p → pending"})
    del _recent[12:]
    return jsonify(ok=True, title=yt, height=h, pending=True)


def _file_capture(files, video_url=None, audio_url=None, ua="Mozilla/5.0",
                  page_title=""):
    """Mux a capture into the armed library slot and update the state file.

    Shared by both paths: the MSE upload (which works) and the retired URL fetch.
    """
    with _lock:
        target = _armed["path"]
        if not target:
            # Nothing armed is a valid mode, not an error — stage it.
            return _free_catch(files, page_title)
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
h1{font-size:1.2em;margin:0 0 10px}
.tabs{display:flex;gap:4px;border-bottom:2px solid #2a2a38;margin-bottom:14px}
.tab{padding:8px 18px;cursor:pointer;color:#888;border-bottom:2px solid transparent;margin-bottom:-2px}
.tab.on{color:#cde;border-bottom-color:#4a7ac0;background:#1c1c26}
.tab .n{background:#3a5a8a;color:#dfe;border-radius:9px;padding:1px 7px;font-size:.8em;margin-left:6px}
.pane{display:none}.pane.on{display:block}
.sub{color:#888;font-size:.85em;margin-bottom:12px}
.armed{background:#1f2b1f;border:1px solid #3a5a3a;border-radius:8px;padding:10px 12px;margin-bottom:14px}
.armed.none{background:#22222c;border-color:#33333f;color:#999}
input{background:#22222c;border:1px solid #383848;color:#eee;padding:7px 9px;border-radius:6px;width:280px}
table{width:100%;border-collapse:collapse;font-size:.9em}
th{text-align:left;color:#888;font-weight:normal;padding:6px 8px;border-bottom:1px solid #333}
td{padding:5px 8px;border-bottom:1px solid #232330}tr:hover{background:#1e1e28}
.h{color:#e08a5a;font-variant-numeric:tabular-nums}
button{background:#2a3a5a;border:1px solid #3a5a8a;color:#cde;padding:4px 10px;border-radius:5px;cursor:pointer}
button:hover{background:#35507a}a{color:#7aa8e0}
.r{font-size:.85em;padding:3px 0}.ok{color:#7ec87e}.bad{color:#e07a7a}
.prow{display:flex;gap:8px;align-items:center;padding:8px 0;border-bottom:1px solid #253040}
.prow:last-child{border-bottom:none}
.pyt{flex:1;color:#bbb;font-size:.85em;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pmeta{color:#e08a5a;font-size:.8em;white-space:nowrap;font-variant-numeric:tabular-nums;min-width:78px}
.prow input{width:230px}
.gh{background:#2e5a2e;border-color:#3a7a3a;color:#dfd}
.dz{background:#5a2a2a;border-color:#8a3a3a;color:#fdd}
.empty{color:#666;padding:24px 0;text-align:center}
code{color:#e08a5a}
</style></head><body>
<h1>Trailer Catcher</h1>
<div class="tabs">
  <div class="tab on" id="tab-armed">Armed</div>
  <div class="tab" id="tab-manual">Manual<span class="n" id="pcount">0</span></div>
</div>

<div class="pane on" id="pane-armed">
  <div class="sub">Arm a title, find its trailer on YouTube, click the extension button. It files itself.</div>
  <div id="armed" class="armed none">nothing armed</div>
  <input id="q" placeholder="filter titles..." oninput="load()">
  <span class="sub" id="count"></span>
  <table><thead><tr><th>now</th><th>title</th><th>year</th><th></th><th></th></tr></thead>
  <tbody id="rows"></tbody></table>
</div>

<div class="pane" id="pane-manual">
  <div class="sub">For content not in the library yet. Catch several on YouTube without stopping,
  then name and encode them here. <span id="stagingpath"></span></div>
  <div id="pendwrap"></div>
  <div id="recent"></div>
</div>

<script>
function show(which){
  for(const t of ['armed','manual']){
    document.getElementById('tab-'+t).classList.toggle('on', t===which);
    document.getElementById('pane-'+t).classList.toggle('on', t===which);
  }
  localStorage.setItem('tc_tab', which);
}
document.getElementById('tab-armed').onclick  = () => show('armed');
document.getElementById('tab-manual').onclick = () => show('manual');

async function refreshArmed(){
  const a = await (await fetch('api/armed')).json();
  const el = document.getElementById('armed');
  if(a.path){
    el.className='armed';
    el.innerHTML='<b>armed:</b> '+a.title+' ('+(a.height||'?')+'p) \u2014 now go find it';
  } else {
    el.className='armed none';
    el.textContent='nothing armed \u2014 pick a title below. A catch with nothing armed goes to Manual.';
  }
  document.getElementById('stagingpath').innerHTML = a.staging ? '<code>'+a.staging+'</code>' : '';
}
async function arm(p){
  await fetch('api/arm',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({path:p})});
  refreshArmed();
}
async function load(){
  const q = document.getElementById('q').value;
  const rows = await (await fetch('api/worklist?q='+encodeURIComponent(q))).json();
  document.getElementById('count').textContent = '  '+rows.length+' shown';
  const tb = document.getElementById('rows');
  tb.innerHTML = '';
  const esc = t => String(t==null?'':t).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;');
  for(const r of rows){
    const s = encodeURIComponent(r.title+' '+(r.year||'')+' trailer');
    const tr = document.createElement('tr');
    tr.innerHTML = '<td class="h">'+(r.height||'?')+'p</td><td>'+esc(r.title)+'</td>'
      + '<td>'+esc(r.year)+'</td>'
      + '<td><a href="https://www.youtube.com/results?search_query='+s+'" target="_blank">search</a></td>'
      + '<td><button class="act-arm">arm</button></td>';
    tr.querySelector('.act-arm').onclick = () => arm(r.path);
    tb.appendChild(tr);
  }
}
async function pending(){
  const rows = await (await fetch('api/pending')).json();
  document.getElementById('pcount').textContent = rows.length;
  const w = document.getElementById('pendwrap');
  if(!rows.length){ w.innerHTML = '<div class="empty">Nothing caught yet.</div>'; return; }
  if(document.querySelector('.prow input:focus')) return;
  const esc = t => String(t==null?'':t).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;');
  w.innerHTML = '';
  for(const r of rows){
    const m = Math.floor(r.duration/60)+':'+String(r.duration%60).padStart(2,'0');
    const d = document.createElement('div');
    d.className = 'prow';
    d.innerHTML = '<span class="pmeta">'+r.height+'p '+m+'</span>'
      + '<a href="api/pending/'+encodeURIComponent(r.id)+'/preview" target="_blank">play</a>'
      + '<span class="pyt" title="'+esc(r.yt_title)+'">'+esc(r.yt_title)+'</span>'
      + '<input class="pname" placeholder="name, e.g. Shameless (UK)">'
      + '<button class="gh act-enc">encode</button>'
      + '<button class="dz act-dis">x</button>';
    const inp = d.querySelector('.pname');
    d.querySelector('.act-enc').onclick = () => enc(r.id, inp.value);
    d.querySelector('.act-dis').onclick = () => dis(r.id);
    inp.onkeydown = e => { if(e.key === 'Enter') enc(r.id, inp.value); };
    w.appendChild(d);
  }
}
async function enc(id, name){
  const v = (name||'').trim();
  if(!v){ alert('Give it a name first'); return; }
  const r = await (await fetch('api/pending/encode',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({id:id,name:v})})).json();
  if(!r.ok) alert(r.error||'failed');
  pending(); recent();
}
async function dis(id){
  if(!confirm('Discard this catch?')) return;
  await fetch('api/pending/discard',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({id:id})});
  pending();
}
async function recent(){
  const rs = await (await fetch('api/recent')).json();
  const esc = t => String(t==null?'':t).replace(/&/g,'&amp;').replace(/</g,'&lt;');
  document.getElementById('recent').innerHTML = rs.map(r =>
    '<div class="r '+(r.ok?'ok':'bad')+'">'+(r.ok?'\u2713':'\u2717')+' '
    + esc(r.title)+' \u2014 '+esc(r.msg)+'</div>').join('');
}
show(localStorage.getItem('tc_tab') || 'armed');
load(); refreshArmed(); recent(); pending();
setInterval(() => { refreshArmed(); recent(); pending(); }, 3000);
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
