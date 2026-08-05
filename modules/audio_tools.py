"""
Audio Tools — shared audio-processing core for the Docflix Media Suite.

Analyze-then-fix: measures each file and applies ONLY what's actually broken, so it's
safe to run across a whole library (heals the botched, leaves the healthy alone).

Four blades on one handle:
  (a) fix        — dead/imbalanced channel, quiet, low-bitrate muffle, optional denoise
  (b) normalize  — batch a FOLDER to a uniform loudness target (two-pass loudnorm)
  (c) downconvert— Atmos / TrueHD / DTS-HD → AC3 (5.1) or AAC (objects flatten to the bed)

Used by both the standalone Audio Tools window and the Rescaler's audio-cleanup toggle.
Video is always stream-copied (bit-for-bit, no re-encode) unless the caller says otherwise.

Built 2026-07-28 with Tony (proven fix on Yippee Yappee & Yahooey E04: dead R channel + quiet).
"""
import json
import os
import re
import subprocess

# codecs that may carry Atmos / are lossless-big → downconvert candidates
ADVANCED_CODECS = {"truehd", "eac3", "e-ac-3", "dts", "mlp", "dts-hd"}
DEFAULT_LUFS = -16.0
IMBALANCE_DB = 20.0   # one channel >20 dB below the loudest = effectively dead

# remember the window's last-used settings in the app's prefs file
_PREFS_FILE = os.path.expanduser("~/.config/docflix_video_converter/prefs.json")


def _load_prefs():
    try:
        with open(_PREFS_FILE) as f:
            return (json.load(f) or {}).get("audio_tools", {}) or {}
    except Exception:
        return {}


def _save_prefs(d):
    try:
        os.makedirs(os.path.dirname(_PREFS_FILE), exist_ok=True)
        allp = {}
        try:
            with open(_PREFS_FILE) as f:
                allp = json.load(f) or {}
        except Exception:
            pass
        allp["audio_tools"] = d
        with open(_PREFS_FILE, "w") as f:
            json.dump(allp, f, indent=2)
    except Exception:
        pass


def _run(cmd, timeout=None):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def probe_audio(path):
    """Stream facts + per-channel RMS + integrated loudness. None if no audio."""
    r = _run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
              "stream=codec_name,channels,channel_layout,sample_rate,bit_rate",
              "-of", "json", path])
    try:
        info = json.loads(r.stdout)["streams"][0]
    except Exception:
        return None
    info["channels"] = int(info.get("channels") or 0)
    # per-channel RMS via astats (prints each channel, then Overall)
    a = _run(["ffmpeg", "-hide_banner", "-i", path, "-af", "astats=metadata=1", "-f", "null", "-"])
    rms = [float(x) for x in re.findall(r"RMS level dB:\s*(-?\d+\.?\d*)", a.stderr)]
    ch = info["channels"]
    info["rms_per_channel"] = rms[:ch] if ch and len(rms) >= ch else rms
    # integrated loudness
    e = _run(["ffmpeg", "-hide_banner", "-i", path, "-af", "ebur128", "-f", "null", "-"])
    m = re.search(r"I:\s*(-?\d+\.?\d*)\s*LUFS", e.stderr)
    info["loudness_lufs"] = float(m.group(1)) if m else None
    return info


def diagnose(info):
    """What's wrong? Returns a dict of detected issues (empty = healthy)."""
    issues = {}
    ch = info.get("channels", 0)
    rms = info.get("rms_per_channel") or []
    # dead/imbalanced channel — only meaningful for 2-ch "fake stereo"
    if ch == 2 and len(rms) == 2:
        hi, lo = max(rms), min(rms)
        if hi - lo > IMBALANCE_DB:
            issues["dead_channel"] = {"live_index": rms.index(hi), "gap_db": round(hi - lo, 1)}
    L = info.get("loudness_lufs")
    if L is not None and abs(L - DEFAULT_LUFS) > 2:
        issues["loudness"] = {"measured": L}
    codec = (info.get("codec_name") or "").lower()
    if codec in ADVANCED_CODECS:
        issues["advanced_codec"] = codec
    return issues


def build_af(info, opts):
    """ffmpeg -af chain from diagnosis + options. '' means audio is fine as-is.
    opts keys: fix_channel(bool), normalize(bool), target_lufs(float), presence(bool), denoise(bool)."""
    issues = diagnose(info)
    chain = []
    if opts.get("fix_channel", True) and "dead_channel" in issues:
        live = issues["dead_channel"]["live_index"]
        chain.append(f"pan=stereo|c0=c{live}|c1=c{live}")   # copy the live channel to both
    if opts.get("normalize", True) and ("loudness" in issues or opts.get("force_normalize")):
        chain.append(f"loudnorm=I={opts.get('target_lufs', DEFAULT_LUFS)}:TP=-1.5:LRA=11")
    if opts.get("presence") and "advanced_codec" not in issues:
        chain.append("highshelf=g=1.5:f=6000")
    if opts.get("denoise"):
        chain.append("afftdn=nf=-25")
    return ",".join(chain)


def _audio_encoder_args(codec, bitrate=None, channels=None):
    codec = codec.lower()
    if codec == "ac3":
        args = ["-c:a", "ac3", "-b:a", bitrate or "640k"]
        args += ["-ac", str(min(channels, 6))] if channels and channels > 6 else []  # 7.1→5.1
        return args
    if codec in ("aac",):
        args = ["-c:a", "aac", "-b:a", bitrate or "256k"]
        return args + (["-ac", str(channels)] if channels else [])
    return ["-c:a", codec] + (["-b:a", bitrate] if bitrate else [])


def process_file(path, out_path, opts, log=print):
    """Fix / clean / convert one file's audio; VIDEO IS STREAM-COPIED. Returns (ok, msg).
    Re-encodes when there's a fix to apply OR the chosen codec differs from the source
    (a codec change IS the downconvert — e.g. Atmos/TrueHD/DTS-HD → AC3/AAC → the bed)."""
    info = probe_audio(path)
    if not info:
        return False, "no audio stream"
    af = build_af(info, opts)
    src = (info.get("codec_name") or "").lower()
    acodec = (opts.get("audio_codec") or "aac").lower()
    stereo = bool(opts.get("stereo"))
    codec_change = acodec != src
    if not af and not codec_change and not stereo and not opts.get("recode_anyway"):
        log(f"  {os.path.basename(path)}: audio already healthy — skipping")
        return True, "healthy (skipped)"
    cmd = ["ffmpeg", "-y", "-i", path, "-map", "0:v?", "-map", "0:a:0", "-map", "0:s?",
           "-c:v", "copy", "-c:s", "copy"]
    if af:
        cmd += ["-af", af]
    if stereo:
        cmd += ["-ac", "2"]
    cmd += _audio_encoder_args(acodec, opts.get("audio_bitrate"),
                               2 if stereo else info.get("channels"))
    cmd += [out_path]
    r = _run(cmd)
    if r.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
        tags = (["fixed"] if af else []) + ([f"{src}→{acodec}"] if codec_change else []) + (["stereo"] if stereo else [])
        return True, ", ".join(tags) or "recoded"
    return False, f"ffmpeg rc={r.returncode}: {r.stderr.strip()[-160:]}"


def downconvert_file(path, out_path, codec="ac3", bitrate=None, stereo=False, log=print):
    """Atmos/TrueHD/DTS-HD → AC3/AAC (decode to channel bed; objects flatten). Video copied."""
    info = probe_audio(path)
    if not info:
        return False, "no audio stream"
    ch = 2 if stereo else info.get("channels", 6)
    cmd = ["ffmpeg", "-y", "-i", path, "-map", "0:v?", "-map", "0:a:0", "-map", "0:s?",
           "-c:v", "copy", "-c:s", "copy"]
    if stereo:
        cmd += ["-ac", "2"]
    cmd += _audio_encoder_args(codec, bitrate, ch) + [out_path]
    r = _run(cmd)
    if r.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
        return True, f"{info.get('codec_name')} → {codec}"
    return False, f"ffmpeg rc={r.returncode}: {r.stderr.strip()[-160:]}"


# ---- Standalone GUI window (Media Suite Tools menu) ----
def open_audio_tools(app):
    """Audio Tools window — fix / normalize / downconvert. Toplevel on app.root;
    runs the shared core (above) in a background thread. Video is always stream-copied."""
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
    import threading
    try:
        from .constants import AUDIO_BITRATES
    except ImportError:
        # This file is also runnable directly (python3 modules/audio_tools.py),
        # where there's no parent package for a relative import. Keep the list
        # in constants.py canonical; this is only a last-resort fallback.
        AUDIO_BITRATES = ('32k', '48k', '64k', '96k', '128k', '160k', '192k',
                          '256k', '320k', '384k', '448k', '512k', '640k')

    win = tk.Toplevel(app.root)
    win.title("🔊 Docflix Audio Tools")
    win.geometry("800x700")
    win.minsize(700, 580)
    try:
        win.transient(app.root)
    except Exception:
        pass

    files = []
    processing = [False]
    stop = [False]
    _ap = _load_prefs()   # remember last-used settings

    def _persist():
        _save_prefs({"mode": mode.get(), "codec": codec.get(), "bitrate": bitrate.get(),
                     "target": target.get(), "presence": presence.get(),
                     "denoise": denoise.get(), "stereo": stereo.get()})

    # ── file list ──
    top = ttk.Frame(win); top.pack(fill='both', expand=True, padx=10, pady=(10, 4))
    ttk.Label(top, text="Files:").pack(anchor='w')
    lfr = ttk.Frame(top); lfr.pack(fill='both', expand=True)
    lb = tk.Listbox(lfr, height=8, selectmode='extended')
    sb = ttk.Scrollbar(lfr, orient='vertical', command=lb.yview)
    lb.configure(yscrollcommand=sb.set)
    lb.pack(side='left', fill='both', expand=True); sb.pack(side='right', fill='y')

    def _refresh():
        lb.delete(0, 'end')
        for f in files:
            lb.insert('end', os.path.basename(f))
        cnt.configure(text=f"{len(files)} file(s)")

    def add_files():
        for p in filedialog.askopenfilenames(
                title="Add files",
                filetypes=[("Video/Audio", "*.mkv *.mp4 *.avi *.m4v *.mov *.ts *.webm"), ("All", "*.*")]):
            if p not in files:
                files.append(p)
        _refresh()

    def add_folder():
        d = filedialog.askdirectory(title="Add a folder (searched recursively)")
        if not d:
            return
        for r_, _d, fns in os.walk(d):
            for fn in fns:
                if fn.lower().endswith(_VIDEO_EXTS):
                    p = os.path.join(r_, fn)
                    if p not in files:
                        files.append(p)
        _refresh()

    def remove_sel():
        for i in reversed(lb.curselection()):
            del files[i]
        _refresh()

    def clear_all():
        files.clear(); _refresh()

    bar = ttk.Frame(top); bar.pack(fill='x', pady=4)
    ttk.Button(bar, text="Add Files…", command=add_files).pack(side='left', padx=2)
    ttk.Button(bar, text="Add Folder…", command=add_folder).pack(side='left', padx=2)
    ttk.Button(bar, text="Remove", command=remove_sel).pack(side='left', padx=2)
    ttk.Button(bar, text="Clear", command=clear_all).pack(side='left', padx=2)
    cnt = ttk.Label(bar, text="0 file(s)"); cnt.pack(side='right')

    def _add_path(p):
        p = p.strip().strip('{}')  # tkdnd wraps paths with spaces in braces
        if os.path.isdir(p):
            for r_, _d, fns in os.walk(p):
                for fn in fns:
                    if fn.lower().endswith(_VIDEO_EXTS):
                        fp = os.path.join(r_, fn)
                        if fp not in files:
                            files.append(fp)
        elif os.path.isfile(p) and p.lower().endswith(_VIDEO_EXTS):
            if p not in files:
                files.append(p)

    def _parse_drop(raw):
        """Robust drop parser (matches the main app): file:// URIs, {brace-wrapped} paths, or spaces.
        NEVER raises — a raising drop handler crashes the whole app via tkdnd's C layer."""
        paths = []
        try:
            raw = raw or ""
            if "file://" in raw:
                from urllib.parse import unquote, urlparse
                for line in raw.splitlines():
                    line = line.strip()
                    if line.startswith("file://"):
                        d = unquote(urlparse(line).path)
                        if d:
                            paths.append(d)
            else:
                i = 0
                while i < len(raw):
                    if raw[i] == "{":
                        depth, end = 1, i + 1
                        while end < len(raw) and depth > 0:
                            depth += (raw[end] == "{") - (raw[end] == "}")
                            end += 1
                        paths.append(raw[i + 1:end - 1])
                        i = end + 1 if end < len(raw) else end
                    else:
                        end = raw.find(" ", i)
                        if end == -1:
                            paths.append(raw[i:]); break
                        paths.append(raw[i:end]); i = end + 1
        except Exception:
            pass
        return paths

    try:  # drag-and-drop files/folders onto the list (tkinterdnd2, already in the app)
        from tkinterdnd2 import DND_FILES

        def _on_drop(event):
            try:
                for p in _parse_drop(getattr(event, "data", "")):
                    _add_path(p)
                _refresh()
            except Exception:
                pass  # a raising drop handler hard-kills the app — swallow everything
        lb.drop_target_register(DND_FILES)
        lb.dnd_bind('<<Drop>>', _on_drop)
        ttk.Label(top, text="↳ or drag files / folders onto the list", font=('', 8)).pack(anchor='w')
    except Exception:
        pass

    # ── mode + options ──
    mode = tk.StringVar(value=_ap.get("mode", "fix"))
    codec = tk.StringVar(value=_ap.get("codec", "aac"))
    bitrate = tk.StringVar(value=_ap.get("bitrate", "256k"))
    target = tk.StringVar(value=_ap.get("target", "-16"))
    presence = tk.BooleanVar(value=_ap.get("presence", False))
    denoise = tk.BooleanVar(value=_ap.get("denoise", False))
    stereo = tk.BooleanVar(value=_ap.get("stereo", False))

    mfr = ttk.LabelFrame(win, text="Mode"); mfr.pack(fill='x', padx=10, pady=6)
    mrow = ttk.Frame(mfr); mrow.pack(fill='x', padx=6, pady=4)
    ttk.Radiobutton(mrow, text="Fix (auto: dead channel, level, muffle)", variable=mode, value="fix").pack(side='left')
    ttk.Radiobutton(mrow, text="Normalize (force to target loudness)", variable=mode, value="normalize").pack(side='left', padx=12)
    o = ttk.Frame(mfr); o.pack(fill='x', padx=6, pady=(0, 6))
    ttk.Label(o, text="Codec:").grid(row=0, column=0, sticky='w')
    ttk.Combobox(o, textvariable=codec, values=["aac", "ac3", "eac3", "mp3", "opus", "flac"],
                 width=7, state="readonly").grid(row=0, column=1, padx=4)
    ttk.Label(o, text="Bitrate:").grid(row=0, column=2, sticky='w')
    # Dropdown, not a free-text box — matches the Codec combo beside it and every
    # other bitrate control in the Suite. Any previously-typed custom value is kept
    # in the list, so moving to a dropdown can't silently change a saved setting.
    # (2026-08-05)
    _br_values = list(AUDIO_BITRATES)
    if bitrate.get() and bitrate.get() not in _br_values:
        _br_values.append(bitrate.get())
    ttk.Combobox(o, textvariable=bitrate, values=_br_values,
                 width=7, state="readonly").grid(row=0, column=3, padx=4)
    ttk.Label(o, text="Target LUFS:").grid(row=0, column=4, sticky='w')
    ttk.Entry(o, textvariable=target, width=6).grid(row=0, column=5, padx=4)
    ttk.Checkbutton(o, text="Presence lift", variable=presence).grid(row=1, column=0, columnspan=2, sticky='w', pady=2)
    ttk.Checkbutton(o, text="Denoise", variable=denoise).grid(row=1, column=2, columnspan=2, sticky='w')
    ttk.Checkbutton(o, text="Downmix→stereo", variable=stereo).grid(row=1, column=4, columnspan=2, sticky='w')

    # ── output ──
    ofr = ttk.LabelFrame(win, text="Output"); ofr.pack(fill='x', padx=10, pady=6)
    outdir = tk.StringVar(value="")
    orow = ttk.Frame(ofr); orow.pack(fill='x', padx=6, pady=6)
    ttk.Label(orow, text="Folder:").pack(side='left')
    ttk.Entry(orow, textvariable=outdir).pack(side='left', fill='x', expand=True, padx=4)
    ttk.Button(orow, text="Browse…",
               command=lambda: outdir.set(filedialog.askdirectory(title="Output folder") or outdir.get())).pack(side='left')
    ttk.Label(ofr, text="(blank = a '_audio_fixed' folder next to each source — originals never touched)",
              font=('', 8)).pack(anchor='w', padx=6, pady=(0, 4))

    # ── log + progress ──
    lf = ttk.Frame(win); lf.pack(fill='both', expand=True, padx=10, pady=(4, 4))
    logw = tk.Text(lf, height=9, wrap='word', state='disabled')
    lsb = ttk.Scrollbar(lf, orient='vertical', command=logw.yview)
    logw.configure(yscrollcommand=lsb.set)
    logw.pack(side='left', fill='both', expand=True); lsb.pack(side='right', fill='y')
    prog = ttk.Progressbar(win, mode='determinate'); prog.pack(fill='x', padx=10)

    import queue as _queue
    msgq = _queue.Queue()

    def _drain():
        try:
            while True:
                item = msgq.get_nowait()
                kind = item[0]
                if kind == 'log':
                    logw.configure(state='normal'); logw.insert('end', item[1] + "\n")
                    logw.see('end'); logw.configure(state='disabled')
                elif kind == 'max':
                    prog.configure(maximum=max(1, item[1]), value=0)
                elif kind == 'prog':
                    prog.configure(value=item[1])
                elif kind == 'done':
                    processing[0] = False
                    run_btn.configure(state='normal'); stop_btn.configure(state='disabled')
        except _queue.Empty:
            pass
        win.after(120, _drain)
    win.after(120, _drain)

    def worker():
        m = mode.get(); total = len(files); ok = 0
        msgq.put(('max', total))
        for i, f in enumerate(files, 1):
            if stop[0]:
                msgq.put(('log', "Stopped.")); break
            base = outdir.get().strip() or os.path.join(os.path.dirname(f), "_audio_fixed")
            os.makedirs(base, exist_ok=True)
            out = os.path.join(base, os.path.splitext(os.path.basename(f))[0] + ".mkv")
            msgq.put(('log', f"[{i}/{total}] {os.path.basename(f)} …"))   # live "starting" line
            try:
                tgt = float(target.get())
            except Exception:
                tgt = DEFAULT_LUFS
            good, info = process_file(f, out, {
                "audio_codec": codec.get(), "audio_bitrate": bitrate.get() or None,
                "presence": presence.get(), "denoise": denoise.get(), "target_lufs": tgt,
                "stereo": stereo.get(),
                "force_normalize": (m == "normalize"), "normalize": True})
            ok += good
            msgq.put(('log', ("   ✓ " if good else "   ✗ ") + f"[{info}]"))
            msgq.put(('prog', i))
        msgq.put(('log', f"\nDone: {ok}/{total}"))
        msgq.put(('done',))

    def start():
        if processing[0]:
            return
        if not files:
            messagebox.showinfo("Audio Tools", "Add some files first.", parent=win); return
        processing[0] = True; stop[0] = False
        _persist()   # remember these settings for next time
        run_btn.configure(state='disabled'); stop_btn.configure(state='normal')
        logw.configure(state='normal'); logw.delete('1.0', 'end'); logw.configure(state='disabled')
        threading.Thread(target=worker, daemon=True).start()

    ctrl = ttk.Frame(win); ctrl.pack(fill='x', padx=10, pady=(4, 10))
    run_btn = ttk.Button(ctrl, text="▶ Process", command=start); run_btn.pack(side='left')
    stop_btn = ttk.Button(ctrl, text="■ Stop", state='disabled',
                          command=lambda: stop.__setitem__(0, True)); stop_btn.pack(side='left', padx=6)

    def _on_close():
        try:
            _persist()
        except Exception:
            pass
        win.destroy()
    win.protocol("WM_DELETE_WINDOW", _on_close)
    _refresh()
    return win


# ---- CLI (the GUI wraps these same functions) ----
_VIDEO_EXTS = (".mkv", ".mp4", ".avi", ".m4v", ".mov", ".ts", ".webm")


def _folder_files(folder):
    return sorted(os.path.join(folder, f) for f in os.listdir(folder)
                  if f.lower().endswith(_VIDEO_EXTS))


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Media Suite audio tools (fix / normalize / downconvert)")
    sub = ap.add_subparsers(dest="mode", required=True)
    for name in ("fix", "normalize", "downconvert"):
        s = sub.add_parser(name)
        s.add_argument("path", help="a file or a folder")
        s.add_argument("-o", "--outdir", help="output dir (default: <path>/_audio_fixed)")
        s.add_argument("--codec", default="ac3" if name == "downconvert" else "aac")
        s.add_argument("--bitrate")
        s.add_argument("--target", type=float, default=DEFAULT_LUFS, help="LUFS target (normalize)")
        s.add_argument("--stereo", action="store_true", help="downmix to stereo")
        s.add_argument("--presence", action="store_true")
        s.add_argument("--denoise", action="store_true")
    a = ap.parse_args()

    files = _folder_files(a.path) if os.path.isdir(a.path) else [a.path]
    base = a.path if os.path.isdir(a.path) else os.path.dirname(a.path)
    outdir = a.outdir or os.path.join(base, "_audio_fixed")
    os.makedirs(outdir, exist_ok=True)
    print(f"{a.mode}: {len(files)} file(s) → {outdir}")

    ok = 0
    for f in files:
        stem = os.path.splitext(os.path.basename(f))[0]
        out = os.path.join(outdir, stem + (".mkv"))
        if a.mode == "downconvert":
            good, msg = downconvert_file(f, out, a.codec, a.bitrate, a.stereo)
        else:
            opts = {"audio_codec": a.codec, "audio_bitrate": a.bitrate,
                    "presence": a.presence, "denoise": a.denoise,
                    "target_lufs": a.target,
                    "force_normalize": a.mode == "normalize", "normalize": True}
            good, msg = process_file(f, out, opts)
        print(("  ✓ " if good else "  ✗ ") + os.path.basename(f) + f"  [{msg}]")
        ok += good
    print(f"\nDone: {ok}/{len(files)}")


if __name__ == "__main__":
    main()
