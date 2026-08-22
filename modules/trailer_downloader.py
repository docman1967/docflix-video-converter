#!/usr/bin/env python3
"""
Docflix Trailer Grabber — modules/trailer_downloader.py

Fetch a title's *official* trailer straight from TMDB (guaranteed the right one,
matched by ID), download it with the user's OWN yt-dlp, remux to MKV, and strip
the YouTube tags — all inside the Suite. No more bouncing to a second app.

BYO yt-dlp: this tool ships NOTHING and links NOWHERE. It auto-detects yt-dlp on
the system PATH; if it isn't there, the user installs it themselves and points the
tool at the binary. That keeps a shippable Suite clean of any YouTube-downloader
distribution — the app only knows how to *drive* a tool the user already chose.

Standalone:  docflix-trailer     In-app:  Tools -> Docflix Trailer Grabber...
"""

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
import threading
import urllib.parse
import urllib.request

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

from .constants import (PREFS_DIR, PREFS_FILENAME,
                        BETA_DEFAULT_TMDB_KEY, BETA_DEFAULT_TVDB_KEY)
from .utils import scaled_geometry, scaled_minsize

TMDB_BASE = "https://api.themoviedb.org/3"
TVDB_BASE = "https://api4.thetvdb.com/v4"
_YT_WATCH = "https://www.youtube.com/watch?v={}"


# ══════════════════════════════════════════════════════════════════════
#  Core pipeline  (headless / testable — no Tk in here)
# ══════════════════════════════════════════════════════════════════════

def find_ytdlp(saved_path=None):
    """Locate yt-dlp WITHOUT ever downloading it (BYO). Order: saved path -> PATH.
    Returns an absolute path, or None if the user hasn't provided one."""
    if saved_path and os.path.isfile(saved_path) and os.access(saved_path, os.X_OK):
        return saved_path
    return shutil.which("yt-dlp")


def _tmdb_get(api_key, path, **params):
    params["api_key"] = api_key
    url = f"{TMDB_BASE}{path}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def tmdb_search(api_key, query, kind):
    """kind: 'movie' or 'tv'. Returns list of {id, title, year}."""
    if not api_key:
        raise ValueError("No TMDB API key set (add it in the TV Renamer settings).")
    data = _tmdb_get(api_key, f"/search/{kind}", query=query, include_adult="false")
    out = []
    for r in data.get("results", [])[:20]:
        name = r.get("title") or r.get("name") or "?"
        date = r.get("release_date") or r.get("first_air_date") or ""
        out.append({"id": r.get("id"), "title": name, "year": date[:4] if date else ""})
    return out


def tmdb_trailer_candidates(api_key, kind, tmdb_id, limit=4):
    """Ordered YouTube trailer URLs for a TMDB id, best first.

    ⚠️ Ordering considers TMDB's `size` field (the upload's vertical resolution)
    as well as type/official. The original scored only on type and official and
    ignored size entirely — which is how Downton Abbey resolved to a 480p entry
    and, worse, why a failed download was simply given up on. Better Call Saul
    has FOUR official 1080p trailers on TMDB; the 2026-08-17 bulk run picked one,
    hit the intermittent 403 four times, and abandoned the title with three
    perfectly good alternatives untried.

    Returns a list so the caller can fall through on failure.
    """
    data = _tmdb_get(api_key, f"/{kind}/{tmdb_id}/videos")
    vids = [v for v in data.get("results", []) if v.get("site") == "YouTube" and v.get("key")]
    if not vids:
        return []

    def score(v):
        t = (v.get("type") or "").lower()
        s = (10 if t == "trailer" else 5 if t == "teaser" else 0)
        if v.get("official"):
            s += 3
        if "official" in (v.get("name") or "").lower():
            s += 1
        # size is 360/480/720/1080/2160 — worth ~1 point per tier, enough to
        # break ties between equal-type entries without ever letting a
        # high-res featurette outrank an actual trailer.
        try:
            sz = int(v.get("size") or 0)
        except (TypeError, ValueError):
            sz = 0
        s += {2160: 4, 1080: 3, 720: 2, 480: 1}.get(sz, 0)
        return s

    ranked = sorted(vids, key=score, reverse=True)
    return [_YT_WATCH.format(v["key"]) for v in ranked[:limit]]


def tmdb_trailer_url(api_key, kind, tmdb_id):
    """Best single YouTube trailer URL, or None. (Kept for the GUI.)"""
    c = tmdb_trailer_candidates(api_key, kind, tmdb_id, limit=1)
    return c[0] if c else None


# ── TVDB (v4: login → bearer token; trailer coverage is thinner than TMDB) ──
def _tvdb_req(path, token=None, body=None, method="GET"):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(TVDB_BASE + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def tvdb_login(apikey):
    """Exchange the TVDB v4 apikey for a bearer token."""
    if not apikey:
        raise ValueError("No TVDB API key set.")
    r = _tvdb_req("/login", body={"apikey": apikey}, method="POST")
    if r.get("status") == "success":
        return r["data"]["token"]
    raise ValueError("TVDB login failed: " + str(r.get("message", "")))


def tvdb_search(token, query, kind):
    """kind: 'movie' or 'tv'. Returns list of {id, title, year} (numeric tvdb_id)."""
    want = "movie" if kind == "movie" else "series"
    r = _tvdb_req("/search?query=" + urllib.parse.quote(query), token=token)
    out = []
    for it in r.get("data", []):
        if it.get("type") != want:
            continue
        out.append({"id": it.get("tvdb_id") or it.get("id"),
                    "title": it.get("name") or "?",
                    "year": str(it.get("year") or "")})
        if len(out) >= 20:
            break
    return out


def tvdb_trailer_url(token, kind, tvdb_id):
    """Best YouTube trailer URL from a TVDB series/movie extended record, or None."""
    seg = "movies" if kind == "movie" else "series"
    r = _tvdb_req(f"/{seg}/{tvdb_id}/extended", token=token)
    trailers = (r.get("data") or {}).get("trailers") or []
    for t in trailers:
        if "youtu" in (t.get("url") or ""):
            return t["url"]
    return trailers[0]["url"] if trailers else None


def safe_filename(name):
    return re.sub(r'[<>:"/\\|?*]+', "", name or "").strip() or "trailer"


# Wall-clock cap per download attempt. Trailers are 10-40MB; this is generous
# for a poor connection while keeping the worst case (4 attempts) bounded.
ATTEMPT_TIMEOUT_SECS = 150

# Failures that will NEVER succeed on retry. Retrying these wastes time and
# makes needless requests to YouTube -- and worse, the generic "failed after 4
# attempts" message hides WHY, so the manual worklist says nothing actionable.
# (All four of TMDB's Better Call Saul trailers are region-blocked; the bulk run
#  hammered them 16 times and reported it as a plain failure. 2026-08-18.)
PERMANENT_ERRORS = (
    "not made this video available in your country",
    "video is unavailable",
    "video is private",
    "video has been removed",
    "account associated with this video has been terminated",
    "sign in to confirm your age",
    "members-only content",
)

# ── Output codecs ─────────────────────────────────────────────────────────
# "copy" is the default and stays the default: YouTube already hands us h264,
# and re-encoding an already-lossy 2-minute clip buys nothing on its own.
# H.265 exists for LIBRARY UNIFORMITY -- everything else Tony owns is HEVC
# CQ32 -- and because it permanently sidesteps the AV1/Roku question if YouTube
# ever stops serving h264. NVENC so it costs seconds, not minutes.
# ⚠️ YouTube never serves H.265; this is always a local transcode.
VIDEO_CODECS = {
    "copy":  None,
    "h264":  ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr",
              "-rc-lookahead", "32", "-cq", "32"],
    # ⚠️ CQ 32 + p4 + 10-bit + lookahead 32 is not a guess -- it is byte-for-byte
    # the Media Suite's library standard, so a trailer encoded here is
    # indistinguishable from everything else Tony owns. Do not "improve" it.
    "h265":  ["-c:v", "hevc_nvenc", "-preset", "p4", "-rc", "vbr",
              "-rc-lookahead", "32", "-cq", "32", "-pix_fmt", "p010le"],
}

# What each container will actually carry. ⚠️ AVI is a 1992 container: no HEVC
# in any player worth the name, and no modern subtitle support. It is offered
# because it was asked for, but h265+avi is refused rather than silently
# producing a file nothing can play.
CONTAINER_CODECS = {
    "mkv": {"copy", "h264", "h265"},
    "mp4": {"copy", "h264", "h265"},
    "mov": {"copy", "h264", "h265"},
    "avi": {"copy", "h264"},
}


# Default exported-cookie location. ~/.cache is excluded from os_backup.sh on purpose:
# this file is a signed-in Google session in plaintext and should not travel to a backup
# server or a rescue box. The user may point it elsewhere (Settings ▸ Use Browser
# Cookies ▸ Cookie file location…) — the dialog says plainly what they are moving.
_COOKIE_FILE_DEFAULT = os.path.expanduser("~/.cache/docflix/yt-cookies.txt")


def cookie_file_path():
    """Where the exported cookie jar lives — user-set, or the safe default."""
    return os.path.expanduser(
        load_trailer_prefs().get("cookie_file") or _COOKIE_FILE_DEFAULT)


def download_trailer(ytdlp, url, out_path, container="mkv", strip=True,
                     log=lambda s: None, stop_flag=None, vcodec="copy",
                     cookies_from=None):
    """Download `url` with yt-dlp into `container` (mkv|mp4); optionally strip metadata.
    Stream-copy (no re-encode) so it's fast. Returns (ok, message).
    MKV takes any codec; MP4 prefers MP4-friendly streams (h264/aac) so the copy-mux works."""
    if not ytdlp:
        return False, "yt-dlp not found. Install it and set its path."
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    # ── Format selection: PREFER, don't pin ───────────────────────────────
    # -f filters hard; -S expresses a preference and degrades gracefully. That
    # distinction is the whole lesson here.
    #
    # We want 1080p h264 + AAC: h264 because the Roku cannot decode AV1, and
    # YouTube's smallest 1080p IS AV1 -- so a naive "best video" lands on a
    # trailer that makes Avalon transcode every time you browse past it.
    #
    # ⚠️ But do NOT express that as a hard -f filter. Format availability varies
    # per video and per session, and format SELECTION cannot see that a URL will
    # 403 at fetch time -- so a pinned codec fails the entire download instead of
    # falling back. -S sorts by preference and takes the next best thing.
    #
    # ⚠️ And the reason trailers were 360p was never this string: yt-dlp was
    # pinned at 2026.01.29 and could only see format 18 (640x360 pre-merged), so
    # every format spec correctly picked "the best available". 91% of the 4,673
    # trailers in the library are 360p because of that one stale binary. Keep
    # yt-dlp current -- it degrades silently and reads as a downloader bug.
    # A JS runtime (deno, symlinked to /usr/local/bin 2026-08-17) is now needed
    # for full extraction; without one, formats quietly go missing again.
    _FMT  = "bv*[height<=1080]+ba/b[height<=1080]/bv*+ba/b"
    _SORT = "res:1080,vcodec:h264,acodec:aac"
    fmt = _FMT
    with tempfile.TemporaryDirectory() as td:
        tmp = os.path.join(td, "dl." + container)
        # ⚠️ --ignore-config is load-bearing. A user-level ~/.config/yt-dlp/config
        # (Tony had one from 2026-01-31) applies to every invocation, and its
        # --embed-thumbnail buried a cover.webp ATTACHMENT stream in the MKV. The
        # tag-strip below then died with "Attachment stream 2 has no mimetype tag",
        # which surfaced to Tony as "ffmpeg tag-strip failed" on a download that had
        # actually succeeded. That config also silently redirects -o to ~/Videos.
        # This tool must behave the same regardless of what is in that file.
        # --socket-timeout: without it a stalled connection hangs forever.
        cmd = [ytdlp, "--ignore-config", "-f", fmt, "-S", _SORT,
               "--socket-timeout", "30",
               "--merge-output-format", container,
               "--no-playlist", "--no-progress", "-o", tmp]
        # ── optional browser cookies ────────────────────────────────────────
        # ⚠️ THIS IS FOR THE BOT-CHECK, NOT THE 403s. Two different failures that
        # look similar in a log:
        #   "HTTP Error 403"                    -> probabilistic, per-URL. Cookies do
        #                                          NOT help. See the retry note below.
        #   "Sign in to confirm you're not a bot" -> IP-level flag after sustained
        #                                          volume. Cookies DO fix it; nothing
        #                                          else does (no player client works).
        # Hit 2026-08-21 after ~2,400 downloads in three days.
        #
        # ⚠️ OPT-IN, and the BULK job must never pass this. Authenticated bulk
        # downloading is what gets Google accounts terminated. Only the GUI Grabber
        # supplies it, for the handful of trailers a human fetches by hand — which is
        # why it is a parameter rather than a module-level setting.
        #
        # ⚠️ TWO THINGS ARE REQUIRED or it silently fails:
        #   1. The browser must be CLOSED — Chrome holds the cookie DB lock.
        #   2. `python3-secretstorage` must be installed — Chrome encrypts cookies
        #      with a key in the GNOME keyring. Without it yt-dlp extracts the rows
        #      and cannot decrypt them, reporting "N could not be decrypted" and
        #      failing anyway. Installed here 2026-08-22 from apt.
        # A previously EXPORTED cookie file wins over live browser extraction: it works
        # with the browser OPEN, which --cookies-from-browser cannot (Chrome holds the
        # DB lock). Export once with:
        #   yt-dlp --cookies-from-browser chrome --cookies <file> --skip-download --simulate <url>
        # ⚠️ That file is a LIVE CREDENTIAL — it carries SAPISID/LOGIN_INFO, i.e. a
        # signed-in Google session in plaintext. It lives under ~/.cache/docflix/
        # deliberately: ~/.cache is in os_backup.sh's EXCLUDES, so it does not
        # propagate to the backup server or the rescue box. Mode 600.
        # ⚠️ Cookies expire. When they do, the bot-check simply returns — re-export.
        _cj = cookie_file_path()
        if cookies_from == "file" or (cookies_from and os.path.isfile(_cj)):
            cmd += ["--cookies", _cj]
        elif cookies_from:
            cmd += ["--cookies-from-browser", cookies_from]
        cmd.append(url)
        # ── Retry on failure ──────────────────────────────────────────────
        # YouTube 403s the media URL *probabilistically*. Measured 2026-08-17 on
        # one trailer: identical command, identical player client (android_vr),
        # four attempts -> OK, OK, OK, 403. Nothing about the request differs; a
        # fresh invocation just gets fresh URLs.
        #
        # ⚠️ It is NOT the client, the format, or cookies -- all three were tested
        # and ruled out. Clients that can SEE the HD formats (android_vr) 403;
        # clients that fetch reliably (mweb, tv_simply) only offer 360p; browser
        # cookies made no difference. So do not "fix" this by pinning a client or
        # dropping to 360p.
        #
        # This is exactly Tony's own workaround -- "if I keep hitting fetch
        # trailer, eventually it will download it" -- just automated.
        rc, attempts, last_err = None, 4, ""
        for attempt in range(1, attempts + 1):
            if attempt > 1:
                log(f"-- retry {attempt} of {attempts} (YouTube 403s intermittently)")
                time.sleep(2)
            if os.path.isfile(tmp):
                try: os.remove(tmp)
                except OSError: pass
            log("$ " + " ".join(cmd))
            # ⚠️ start_new_session so the watchdog can kill the whole GROUP.
            # Killing yt-dlp alone is not enough: it spawns ffmpeg to merge the
            # video+audio streams, and that child INHERITS the stdout pipe. Kill
            # only the parent and the pipe stays open, so "for line in p.stdout"
            # blocks forever and the watchdog achieves nothing. Proved it with a
            # fake yt-dlp that spawned a sleeping child (2026-08-17).
            err_lines = []
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, start_new_session=True)
            # ── Watchdog ──────────────────────────────────────────────────
            # ⚠️ Reading stdout line-by-line blocks. If yt-dlp hangs SILENTLY the
            # loop below never runs again, so neither the elapsed check nor the
            # Cancel button can fire -- the GUI just sits there. With the retry
            # loop that is four indefinite hangs, not one. A separate poller is
            # the only thing that can break out of it.
            state = {"killed": None}

            def _watch():
                start = time.time()
                while p.poll() is None:
                    if stop_flag and stop_flag[0]:
                        state["killed"] = "cancelled"
                        break
                    if time.time() - start > ATTEMPT_TIMEOUT_SECS:
                        state["killed"] = "timeout"
                        break
                    time.sleep(0.5)
                if state["killed"]:
                    try:
                        os.killpg(os.getpgid(p.pid), signal.SIGKILL)
                    except Exception:
                        try:
                            p.kill()
                        except Exception:
                            pass

            wd = threading.Thread(target=_watch, daemon=True)
            wd.start()
            try:
                for line in p.stdout:
                    line = line.rstrip()
                    if "ERROR" in line:
                        err_lines.append(line)
                    log(line)
                p.wait()
            finally:
                rc = p.returncode
                wd.join(timeout=2)
            if state["killed"] == "cancelled":
                return False, "Cancelled."
            if state["killed"] == "timeout":
                log(f"-- attempt {attempt} timed out after {ATTEMPT_TIMEOUT_SECS}s")
                rc = rc or -1
            if rc == 0 and os.path.isfile(tmp):
                break
            last_err = err_lines[-1] if err_lines else last_err
            low = last_err.lower()
            if any(k in low for k in PERMANENT_ERRORS):
                # deterministic -- retrying cannot help
                reason = last_err.split(":")[-1].strip() or last_err
                return False, reason[:120]
        if rc != 0 or not os.path.isfile(tmp):
            if last_err:
                return False, last_err.split("ERROR:")[-1].strip()[:120]
            return False, f"yt-dlp failed after {attempts} attempts (exit {rc})."
        if vcodec not in CONTAINER_CODECS.get(container, {"copy"}):
            return False, (f"{vcodec.upper()} is not supported in .{container} "
                           f"-- pick MKV/MP4/MOV, or set the codec to Copy.")
        if strip or vcodec != "copy":
            log(("Encoding to " + vcodec + " -> " if vcodec != "copy"
                 else "Stripping tags -> ") + out_path)
            # ⚠️ Map v/a/s explicitly rather than "-map 0". "-map 0" also picks up
            # ATTACHMENT streams (embedded cover art), and stream-copying one into
            # matroska fails because the mimetype tag does not survive -- header
            # write dies with "incorrect codec parameters". Attachments are exactly
            # the sort of thing we are stripping anyway.
            fcmd = ["ffmpeg", "-y", "-i", tmp,
                    "-map", "0:v", "-map", "0:a?"]
            # AVI cannot carry subtitle streams at all; everything else can.
            if container != "avi":
                fcmd += ["-map", "0:s?"]
            venc = VIDEO_CODECS.get(vcodec)
            if venc:
                fcmd += venc + ["-c:a", "aac", "-b:a", "192k"]
            else:
                fcmd += ["-c", "copy"]
            fcmd += ["-map_metadata", "-1", "-map_chapters", "-1", out_path]
            fp = subprocess.run(fcmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            if fp.returncode != 0 or not os.path.isfile(out_path):
                log(fp.stdout)
                return False, "ffmpeg tag-strip failed."
        else:
            log("Keeping tags -> " + out_path)
            shutil.move(tmp, out_path)
    return True, "Saved: " + out_path


# ── prefs (self-contained read/modify/write of the shared prefs JSON) ──
def _prefs_path():
    return os.path.join(os.path.expanduser(PREFS_DIR), PREFS_FILENAME)


def load_trailer_prefs():
    try:
        with open(_prefs_path()) as f:
            return json.load(f).get("trailer_downloader", {})
    except Exception:
        return {}


def save_trailer_prefs(d):
    try:
        p = _prefs_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        prefs = {}
        if os.path.exists(p):
            with open(p) as f:
                prefs = json.load(f)
        prefs["trailer_downloader"] = d
        with open(p, "w") as f:
            json.dump(prefs, f, indent=2)
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════
#  GUI
# ══════════════════════════════════════════════════════════════════════

def _add_ctx_menu(widget):
    """Attach a right-click Cut/Copy/Paste/Select-All menu to an Entry."""
    m = tk.Menu(widget, tearoff=0)
    m.add_command(label="Cut",   command=lambda: widget.event_generate("<<Cut>>"))
    m.add_command(label="Copy",  command=lambda: widget.event_generate("<<Copy>>"))
    m.add_command(label="Paste", command=lambda: widget.event_generate("<<Paste>>"))
    m.add_separator()
    m.add_command(label="Select All",
                  command=lambda: (widget.select_range(0, "end"), widget.icursor("end")))

    def _show(e):
        widget.focus_set()
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()
    widget.bind("<Button-3>", _show)


def open_trailer_downloader(app):
    """Build and show the Trailer Grabber window (Toplevel on app.root)."""
    win = tk.Toplevel(app.root)
    win.withdraw()
    win.title("Docflix Trailer Grabber")
    win.geometry(scaled_geometry(win, 640, 440))
    win.minsize(*scaled_minsize(win, 560, 380))

    tprefs = load_trailer_prefs()
    tmdb_key = getattr(app, "_tmdb_api_key", "") or BETA_DEFAULT_TMDB_KEY
    tvdb_key = getattr(app, "_tvdb_api_key", "") or BETA_DEFAULT_TVDB_KEY

    ytdlp_path = [find_ytdlp(tprefs.get("ytdlp_path"))]
    results = []
    busy = [False]
    stop_flag = [False]
    log_visible = [False]
    tvdb_token = [None]              # lazy-login cache

    v_kind   = tk.StringVar(value=tprefs.get("kind", "movie"))
    v_query  = tk.StringVar()
    v_url    = tk.StringVar()
    v_dest   = tk.StringVar(value=tprefs.get("dest", os.path.expanduser("~")))
    v_container = tk.StringVar(value=tprefs.get("container", "mkv"))
    v_vcodec    = tk.StringVar(value=tprefs.get("vcodec", "copy"))
    v_strip     = tk.BooleanVar(value=tprefs.get("strip", True))
    # ⚠️ Browser cookies — OPT-IN, default off, GUI only. See download_trailer() for
    # why: this fixes the "Sign in to confirm you're not a bot" IP flag, NOT the
    # probabilistic 403s. Authenticated bulk downloading is what gets Google accounts
    # terminated, so the bulk job (trailer_refresh.py) never passes this.
    v_cookies   = tk.StringVar(value=tprefs.get("cookies_from", ""))
    v_source    = tk.StringVar(value=tprefs.get("source", "tmdb"))

    frm = ttk.Frame(win, padding=10)
    frm.pack(fill="both", expand=True)
    ttk.Label(frm, text="Docflix Trailer Grabber", font=("", 14, "bold")).pack(anchor="w")
    ttk.Label(frm, text="Search a title or paste a URL, then Fetch. Folder & yt-dlp live under Settings.",
              foreground="#888").pack(anchor="w", pady=(0, 8))

    # 1. find the trailer
    look = ttk.LabelFrame(frm, text="1. Find the trailer", padding=8)
    look.pack(fill="x")
    r1 = ttk.Frame(look)
    r1.pack(fill="x", pady=2)
    ttk.Label(r1, text="Type:").pack(side="left")
    ttk.Radiobutton(r1, text="Movie", variable=v_kind, value="movie").pack(side="left")
    ttk.Radiobutton(r1, text="TV Show", variable=v_kind, value="tv").pack(side="left", padx=(4, 12))
    q_entry = ttk.Entry(r1, textvariable=v_query)
    q_entry.pack(side="left", fill="x", expand=True)
    _add_ctx_menu(q_entry)
    search_btn = ttk.Button(r1, text="Search", command=lambda: do_search())
    search_btn.pack(side="left", padx=4)
    ttk.Button(r1, text="Clear", command=lambda: do_clear()).pack(side="left")
    res_box = tk.Listbox(look, height=5)
    res_box.pack(fill="x", pady=4)
    res_box.bind("<<ListboxSelect>>", lambda e: pick_result())
    r2 = ttk.Frame(look)
    r2.pack(fill="x", pady=2)
    ttk.Label(r2, text="Trailer URL:").pack(side="left")
    url_entry = ttk.Entry(r2, textvariable=v_url)
    url_entry.pack(side="left", fill="x", expand=True, padx=4)
    _add_ctx_menu(url_entry)

    # action row: Fetch + collapsible-log toggle
    btnrow = ttk.Frame(frm)
    btnrow.pack(fill="x", pady=6)
    fetch_btn = ttk.Button(btnrow, text="⬇  Fetch Trailer", command=lambda: do_fetch())
    fetch_btn.pack(side="left")
    show_log_btn = ttk.Button(btnrow, text="Show Log ▾", command=lambda: toggle_log())
    show_log_btn.pack(side="left", padx=6)

    # compact status (yt-dlp + effective save path)
    stat = ttk.Frame(frm)
    stat.pack(fill="x")
    y_lbl = ttk.Label(stat, text="", font=("", 8))
    y_lbl.pack(anchor="w")
    dest_lbl = ttk.Label(stat, text="", font=("", 8), foreground="#888")
    dest_lbl.pack(anchor="w")

    # log — hidden until "Show Log"
    log_box = scrolledtext.ScrolledText(frm, height=8, state="disabled", font=("monospace", 9))

    def _log(s):
        log_box.config(state="normal")
        log_box.insert("end", s + "\n")
        log_box.see("end")
        log_box.config(state="disabled")

    def log(s):
        win.after(0, lambda: _log(s))

    def toggle_log(force_show=False):
        if log_visible[0] and not force_show:
            log_box.pack_forget(); log_visible[0] = False
            show_log_btn.config(text="Show Log ▾")
            win.geometry(scaled_geometry(win, 640, 440))
        elif not log_visible[0]:
            log_box.pack(fill="both", expand=True, pady=(6, 0)); log_visible[0] = True
            show_log_btn.config(text="Hide Log ▲")
            win.geometry(scaled_geometry(win, 640, 680))

    def refresh_dest_label():
        f = v_dest.get() or os.path.expanduser("~")
        dest_lbl.config(text="saving to:  " + f)

    def refresh_ytdlp_label():
        if ytdlp_path[0]:
            y_lbl.config(text="yt-dlp ✓  " + ytdlp_path[0], foreground="#3a3")
        else:
            y_lbl.config(text="yt-dlp not found — Settings ▸ Set yt-dlp Path…", foreground="#c33")
        fetch_btn.config(state=("normal" if ytdlp_path[0] else "disabled"))

    def set_ytdlp():
        p = filedialog.askopenfilename(parent=win, title="Locate the yt-dlp binary")
        win.lift(); win.focus_force()
        if not p:
            return
        if os.access(p, os.X_OK):
            ytdlp_path[0] = p
            tp = load_trailer_prefs(); tp["ytdlp_path"] = p; save_trailer_prefs(tp)
            refresh_ytdlp_label()
        else:
            messagebox.showerror("yt-dlp", "That file isn't executable.", parent=win)

    def browse_dest():
        d = filedialog.askdirectory(parent=win, initialdir=v_dest.get() or os.path.expanduser("~"))
        win.lift(); win.focus_force()
        if d:
            v_dest.set(d); refresh_dest_label()
            tp = load_trailer_prefs(); tp["dest"] = d; save_trailer_prefs(tp)

    def _tvdb_tok():
        if tvdb_token[0] is None:
            tvdb_token[0] = tvdb_login(tvdb_key)
        return tvdb_token[0]

    def do_clear():
        v_query.set(""); v_url.set(""); res_box.delete(0, "end"); results.clear()

    def do_search():
        q = v_query.get().strip()
        if not q:
            return
        src = v_source.get()
        search_btn.config(state="disabled")
        res_box.delete(0, "end")
        _log(f"Searching {src.upper()} for: {q}")

        def worker():
            try:
                rs = (tvdb_search(_tvdb_tok(), q, v_kind.get()) if src == "tvdb"
                      else tmdb_search(tmdb_key, q, v_kind.get()))
            except Exception as e:
                log(f"{src.upper()} error: " + str(e)); rs = []

            def done():
                results.clear(); results.extend(rs)
                for r in rs:
                    res_box.insert("end", f"{r['title']} ({r['year']})" if r['year'] else r['title'])
                search_btn.config(state="normal")
                _log(f"{len(rs)} result(s).")
            win.after(0, done)
        threading.Thread(target=worker, daemon=True).start()

    def pick_result():
        sel = res_box.curselection()
        if not sel:
            return
        r = results[sel[0]]
        src = v_source.get()
        _log(f"Fetching trailer URL for {r['title']} ({r['year']})…")

        def worker():
            try:
                u = (tvdb_trailer_url(_tvdb_tok(), v_kind.get(), r["id"]) if src == "tvdb"
                     else tmdb_trailer_url(tmdb_key, v_kind.get(), r["id"]))
            except Exception as e:
                log(f"{src.upper()} error: " + str(e)); u = None

            def done():
                if u:
                    v_url.set(u); _log("Trailer: " + u)
                else:
                    _log(f"No trailer on {src.upper()} for that title.")
            win.after(0, done)
        threading.Thread(target=worker, daemon=True).start()

    def do_fetch():
        if busy[0]:
            return
        url = v_url.get().strip()
        if not url:
            messagebox.showwarning("Trailer", "Pick a result or paste a trailer URL first.", parent=win)
            return
        folder = v_dest.get().strip() or os.path.expanduser("~")
        base = None
        sel = res_box.curselection()
        if sel and results:
            r = results[sel[0]]
            base = f"{r['title']} ({r['year']})" if r['year'] else r['title']
        base = safe_filename(base or v_query.get() or "trailer")
        out = os.path.join(folder, base + "-trailer." + v_container.get())
        busy[0] = True; stop_flag[0] = False
        fetch_btn.config(state="disabled", text="Fetching…")
        toggle_log(force_show=True)      # reveal the log so progress is visible
        _log("")

        def worker():
            ok, msg = download_trailer(ytdlp_path[0], url, out,
                                       container=v_container.get(), strip=v_strip.get(),
                                       vcodec=v_vcodec.get(),
                                       log=log, stop_flag=stop_flag,
                                       cookies_from=(v_cookies.get() or None))

            def done():
                busy[0] = False
                fetch_btn.config(state="normal", text="⬇  Fetch Trailer")
                _log(("✓ " if ok else "✗ ") + msg)
                if ok:
                    tp = load_trailer_prefs()
                    tp.update({"kind": v_kind.get(), "dest": v_dest.get(),
                               "container": v_container.get(), "strip": v_strip.get(),
                               "vcodec": v_vcodec.get(),
                               "source": v_source.get(),
                               "cookies_from": v_cookies.get()})
                    save_trailer_prefs(tp)
                    messagebox.showinfo("Trailer Grabber", "Trailer saved:\n" + out, parent=win)
                else:
                    messagebox.showerror("Trailer Grabber", msg, parent=win)
                win.lift(); win.focus_force()
            win.after(0, done)
        threading.Thread(target=worker, daemon=True).start()

    # Settings menu — keeps the main window simple (folder + yt-dlp live here)
    menubar = tk.Menu(win)
    smenu = tk.Menu(menubar, tearoff=0)
    menubar.add_cascade(label="Settings", menu=smenu)
    smenu.add_command(label="Download Folder…", command=browse_dest)
    smenu.add_separator()
    srcmenu = tk.Menu(smenu, tearoff=0)
    srcmenu.add_radiobutton(label="TMDB  (recommended)", variable=v_source, value="tmdb")
    srcmenu.add_radiobutton(label="TVDB", variable=v_source, value="tvdb")
    smenu.add_cascade(label="Search Source", menu=srcmenu)
    cmenu = tk.Menu(smenu, tearoff=0)
    cmenu.add_radiobutton(label="MKV", variable=v_container, value="mkv")
    cmenu.add_radiobutton(label="MP4", variable=v_container, value="mp4")
    cmenu.add_radiobutton(label="MOV", variable=v_container, value="mov")
    cmenu.add_radiobutton(label="AVI  (no subtitles, no H.265)",
                          variable=v_container, value="avi")
    smenu.add_cascade(label="Container", menu=cmenu)
    # ⚠️ Persist on CHANGE, not on a successful download. The other prefs are saved
    # in the success path, which is wrong for this one: the user turns it on BECAUSE
    # downloads are failing, so saving only on success would lose the setting exactly
    # when it is needed.
    def _save_cookie_pref():
        tp = load_trailer_prefs()
        tp["cookies_from"] = v_cookies.get()
        save_trailer_prefs(tp)

    kmenu = tk.Menu(smenu, tearoff=0)
    kmenu.add_radiobutton(label="Off  (anonymous — default)",
                          variable=v_cookies, value="", command=_save_cookie_pref)
    kmenu.add_radiobutton(label="Chrome", variable=v_cookies, value="chrome",
                          command=_save_cookie_pref)
    kmenu.add_radiobutton(label="Firefox", variable=v_cookies, value="firefox",
                          command=_save_cookie_pref)
    kmenu.add_separator()

    def _cookie_path_label():
        cj = cookie_file_path()
        return cj if len(cj) <= 52 else "…" + cj[-51:]

    def _choose_cookie_file():
        """Point at an existing cookie file (or a location to keep one)."""
        cj = cookie_file_path()
        f = filedialog.askopenfilename(
            parent=win, title="Select cookie file",
            initialdir=os.path.dirname(cj) or os.path.expanduser("~"),
            initialfile=os.path.basename(cj),
            filetypes=[("Cookie file", "*.txt"), ("All files", "*")])
        win.lift(); win.focus_force()
        if not f:
            return
        tp = load_trailer_prefs(); tp["cookie_file"] = f; save_trailer_prefs(tp)
        _refresh_cookie_menu()
        messagebox.showinfo("Cookie file", "Now using:\n" + f, parent=win)

    def _export_cookies():
        """Export browser cookies to a file so the browser can stay OPEN."""
        import subprocess as _sp
        src = v_cookies.get() or "chrome"
        if src == "file":
            src = "chrome"

        # ⚠️ ASK WHERE. Everyone keeps credentials somewhere different, and a
        # hardcoded path is wrong for a shipped tool. The default is deliberate
        # though — see below.
        dest = filedialog.asksaveasfilename(
            parent=win, title="Save cookie file as",
            initialdir=os.path.dirname(cookie_file_path()),
            initialfile=os.path.basename(cookie_file_path()),
            defaultextension=".txt",
            filetypes=[("Cookie file", "*.txt"), ("All files", "*")])
        win.lift(); win.focus_force()
        if not dest:
            return

        # ⚠️ The default lives under ~/.cache because that path is in os_backup.sh's
        # EXCLUDES. Anywhere else and a signed-in Google session starts replicating to
        # the backup server and the rescue box. Say so rather than silently allowing it.
        warn_extra = ""
        if not dest.startswith(os.path.expanduser("~/.cache")):
            warn_extra = ("\n⚠  This location is OUTSIDE ~/.cache, so it may be\n"
                          "    included in backups. The default location is not.\n")

        if not messagebox.askyesno(
                "Export cookies",
                f"Export {src} cookies to:\n{dest}\n\n"
                f"⚠  {src.title()} must be CLOSED right now, or the cookies\n"
                f"    cannot be decrypted.\n\n"
                f"⚠  This file is a signed-in Google session in plain text.\n"
                f"    Anyone who has it is logged into your account.\n"
                f"    It is written owner-readable only (600).\n"
                f"{warn_extra}\n"
                f"Cookies expire — re-export when the bot-check returns.\n\n"
                f"Afterwards the browser can stay open.", parent=win):
            return

        d = os.path.dirname(dest)
        if d:
            os.makedirs(d, exist_ok=True)
            try:
                os.chmod(d, 0o700)
            except OSError:
                pass
        r = _sp.run([ytdlp_path[0] or "yt-dlp", "--ignore-config", "--no-warnings",
                     "--cookies-from-browser", src, "--cookies", dest,
                     "--skip-download", "--simulate",
                     "https://www.youtube.com/watch?v=jRkM_VroEsE"],
                    capture_output=True, text=True)
        if os.path.isfile(dest) and os.path.getsize(dest) > 1000:
            try:
                os.chmod(dest, 0o600)
            except OSError:
                pass
            tp = load_trailer_prefs(); tp["cookie_file"] = dest; save_trailer_prefs(tp)
            v_cookies.set("file"); _save_cookie_pref(); _refresh_cookie_menu()
            messagebox.showinfo("Export cookies",
                                "Exported. The browser can stay open from now on.",
                                parent=win)
        else:
            err = (r.stderr or r.stdout or "").strip()[-300:]
            messagebox.showerror(
                "Export failed",
                "Could not export cookies.\n\n"
                "Most likely the browser is still running, or\n"
                "python3-secretstorage is not installed.\n\n" + err, parent=win)

    kmenu.add_radiobutton(label="Use exported cookie file  (browser can stay open)",
                          variable=v_cookies, value="file", command=_save_cookie_pref)
    kmenu.add_command(label="Export cookies to file now…", command=_export_cookies)
    kmenu.add_command(label="Cookie file location…", command=_choose_cookie_file)
    _COOKIE_PATH_INDEX = kmenu.index("end") + 1
    kmenu.add_command(label="   " + _cookie_path_label(), state="disabled")

    def _refresh_cookie_menu():
        """Keep the greyed-out path line honest after the user changes it."""
        try:
            kmenu.entryconfigure(_COOKIE_PATH_INDEX,
                                 label="   " + _cookie_path_label())
        except Exception:
            pass

    kmenu.add_separator()
    kmenu.add_command(
        label="⚠  Browser must be CLOSED, and needs python3-secretstorage",
        state="disabled")
    kmenu.add_command(
        label="⚠  Uses YOUR account — fine for a few, not for bulk",
        state="disabled")
    smenu.add_cascade(label="Use Browser Cookies", menu=kmenu)
    vmenu = tk.Menu(smenu, tearoff=0)
    vmenu.add_radiobutton(label="Copy  (no re-encode, fastest)",
                          variable=v_vcodec, value="copy")
    vmenu.add_radiobutton(label="H.264  (NVENC)", variable=v_vcodec, value="h264")
    vmenu.add_radiobutton(label="H.265 / HEVC  (NVENC, matches library)",
                          variable=v_vcodec, value="h265")
    smenu.add_cascade(label="Video Codec", menu=vmenu)
    smenu.add_checkbutton(label="Strip metadata tags", variable=v_strip)
    smenu.add_separator()
    smenu.add_command(label="Set yt-dlp Path…", command=set_ytdlp)
    win.config(menu=menubar)

    refresh_ytdlp_label()
    refresh_dest_label()
    win.update_idletasks()
    win.deiconify()
    win.lift()
    win.focus_force()


def main():
    """Standalone entry point: docflix-trailer."""
    from .standalone import create_standalone_root
    root, app = create_standalone_root(
        title="Docflix Trailer Grabber", geometry="820x660", minsize=(680, 540))
    app._standalone_mode = True
    root.withdraw()
    open_trailer_downloader(app)
    root.mainloop()


if __name__ == "__main__":
    main()
