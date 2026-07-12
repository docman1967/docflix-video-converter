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
import subprocess
import tempfile
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


def tmdb_trailer_url(api_key, kind, tmdb_id):
    """Best YouTube trailer URL for a TMDB movie/tv id, or None.
    Preference: official Trailer > any Trailer > Teaser; YouTube only."""
    data = _tmdb_get(api_key, f"/{kind}/{tmdb_id}/videos")
    vids = [v for v in data.get("results", []) if v.get("site") == "YouTube" and v.get("key")]
    if not vids:
        return None
    def score(v):
        t = (v.get("type") or "").lower()
        s = (10 if t == "trailer" else 5 if t == "teaser" else 0)
        if v.get("official"):
            s += 3
        if "official" in (v.get("name") or "").lower():
            s += 1
        return s
    return _YT_WATCH.format(max(vids, key=score)["key"])


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


def download_trailer(ytdlp, url, out_path, container="mkv", strip=True,
                     log=lambda s: None, stop_flag=None):
    """Download `url` with yt-dlp into `container` (mkv|mp4); optionally strip metadata.
    Stream-copy (no re-encode) so it's fast. Returns (ok, message).
    MKV takes any codec; MP4 prefers MP4-friendly streams (h264/aac) so the copy-mux works."""
    if not ytdlp:
        return False, "yt-dlp not found. Install it and set its path."
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    fmt = ("bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b" if container == "mp4"
           else "bv*+ba/b")
    with tempfile.TemporaryDirectory() as td:
        tmp = os.path.join(td, "dl." + container)
        cmd = [ytdlp, "-f", fmt, "--merge-output-format", container,
               "--no-playlist", "--no-progress", "-o", tmp, url]
        log("$ " + " ".join(cmd))
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in p.stdout:
            if stop_flag and stop_flag[0]:
                p.terminate()
                return False, "Cancelled."
            log(line.rstrip())
        p.wait()
        if p.returncode != 0 or not os.path.isfile(tmp):
            return False, f"yt-dlp failed (exit {p.returncode})."
        if strip:
            log("Stripping tags -> " + out_path)
            fcmd = ["ffmpeg", "-y", "-i", tmp, "-map", "0", "-c", "copy",
                    "-map_metadata", "-1", "-map_chapters", "-1", out_path]
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
    v_strip     = tk.BooleanVar(value=tprefs.get("strip", True))
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
                                       log=log, stop_flag=stop_flag)

            def done():
                busy[0] = False
                fetch_btn.config(state="normal", text="⬇  Fetch Trailer")
                _log(("✓ " if ok else "✗ ") + msg)
                if ok:
                    tp = load_trailer_prefs()
                    tp.update({"kind": v_kind.get(), "dest": v_dest.get(),
                               "container": v_container.get(), "strip": v_strip.get(),
                               "source": v_source.get()})
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
    smenu.add_cascade(label="Container", menu=cmenu)
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
