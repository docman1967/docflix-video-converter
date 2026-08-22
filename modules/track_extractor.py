"""
Docflix Media Suite — Track Extractor

Pull selected tracks out of a video file, losslessly, into their own containers.

⚠️ THIS IS A REMUX, NOT AN EXTRACTION, and the distinction is the whole point.
`mkvextract tracks` writes a RAW elementary stream — an .ac3 is just frames, with
nowhere to keep a title, a language or a disposition. Measured 2026-08-22 moving
one commentary between two files:

    remux into .mka   title, language and `comment`  ALL SURVIVE
    raw .ac3          ALL LOST

So audio lands in .mka (Matroska Audio) and everything it knew comes with it. That
is what makes the round trip work: extract here, drop the file beside a video, and
the Media Processor's _detect_ext_audio() picks it up and muxes it back in with its
title intact — no retyping, no naming convention carrying the meaning.

Output is named from the SOURCE stem, deliberately. Tony renames sources through the
Media Renamer before using them, so by extraction time the stem already matches the
file he is muxing into (his call, 2026-08-22 — the alternative was teaching this tool
about destination files, which his workflow had already solved).
"""

import os
import queue
import subprocess
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk, messagebox

from .constants import VIDEO_EXTENSIONS
from .utils import (get_audio_streams, get_subtitle_streams,
                    describe_audio_stream, ask_open_files, ask_directory,
                    scaled_geometry, scaled_minsize)

try:
    from tkinterdnd2 import DND_FILES
    HAS_DND = True
except ImportError:
    HAS_DND = False

GLYPH_ON, GLYPH_OFF = '☑', '☐'

COLOR_QUEUED = "gray"
COLOR_ACTIVE = "#e8a317"
COLOR_DONE = "#2e8b57"
COLOR_ERROR = "#cd3333"

# Subtitle codecs that are text and can sit in a .srt; everything else is a
# bitmap and has to keep its own container.
_TEXT_SUB_CODECS = {'subrip', 'srt', 'ass', 'ssa', 'mov_text', 'webvtt', 'text'}
_SUB_EXT_FOR = {'hdmv_pgs_subtitle': '.sup', 'dvd_subtitle': '.sub',
                'dvb_subtitle': '.sub', 'ass': '.ass', 'ssa': '.ssa'}

SEED_MODES = ('Commentary only', 'All audio', 'Nothing')


def probe_tracks(path):
    """Audio + subtitle tracks of *path*, in one list, richly described."""
    tracks = []
    for s in get_audio_streams(str(path)):
        tracks.append({
            'kind': 'audio', 'index': s['index'], 'ord': s['ord'],
            'language': s.get('language', 'und'),
            'title': s.get('title', ''),
            'role': s.get('role', 'main'),
            'codec': s.get('codec_name', ''),
            'label': describe_audio_stream(s),
        })
    for i, s in enumerate(get_subtitle_streams(str(path))):
        bits = [f"S{i + 1}"]
        lang = s.get('language') or 'und'
        if lang != 'und':
            bits.append(lang)
        bits.append(s.get('codec_name', ''))
        if s.get('forced'):
            bits.append('Forced')
        if s.get('sdh'):
            bits.append('SDH')
        if s.get('title'):
            bits.append(f'"{s["title"][:34]}"')
        tracks.append({
            'kind': 'subtitle', 'index': s['index'], 'ord': i,
            'language': lang, 'title': s.get('title', ''),
            'role': 'forced' if s.get('forced') else ('sdh' if s.get('sdh') else 'main'),
            'codec': s.get('codec_name', ''),
            'label': '  '.join(b for b in bits if b),
        })
    return tracks


def output_name(src: Path, track, seq):
    """Filename for one extracted track.

    ⚠️ These names are the CONTRACT with the Media Processor. Its
    _detect_ext_audio()/_detect_ext_subs() match on `<video stem>` followed by
    tokens, and read `commentary<N>` to flag the track and order it. Change the
    shape here and the round trip quietly stops working.
    """
    stem = src.stem
    lang = track.get('language') or 'und'
    if track['kind'] == 'audio':
        if track['role'] == 'commentary':
            return f"{stem}.commentary{seq}.mka"
        if track['role'] == 'descriptive':
            return f"{stem}.descriptive{seq}.mka"
        return f"{stem}.audio{seq}.{lang}.mka"
    # subtitles keep their native shape so players and the Processor both cope
    codec = (track.get('codec') or '').lower()
    if codec in _TEXT_SUB_CODECS:
        ext = '.srt'
    else:
        ext = _SUB_EXT_FOR.get(codec, '.mks')
    tags = ''
    if track['role'] == 'forced':
        tags = '.forced'
    elif track['role'] == 'sdh':
        tags = '.sdh'
    return f"{stem}.{lang}{tags}{ext}"


def build_command(src: Path, track, out_path: Path):
    """ffmpeg args to lift one track out, byte-for-byte.

    ⚠️ `-c copy`, always. Re-encoding here would be silent generation loss on a
    track the user is only moving, and for a commentary that has already been
    through their pipeline it would be a second lossy pass for nothing.
    """
    codec_arg = 'copy'
    if track['kind'] == 'subtitle' and out_path.suffix == '.srt':
        # A text sub going into .srt may need transcoding from ass/mov_text;
        # copy only works when the codec already IS subrip.
        if (track.get('codec') or '').lower() not in ('subrip', 'srt'):
            codec_arg = 'srt'
    return ['ffmpeg', '-y', '-i', str(src),
            '-map', f"0:{track['index']}",
            '-c', codec_arg,
            str(out_path)]


class ExtractWorker(threading.Thread):
    """Runs the queued extractions, one ffmpeg per track."""

    def __init__(self, q, jobs):
        super().__init__(daemon=True)
        self.q = q
        self.jobs = jobs
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        for i, job in enumerate(self.jobs):
            if self._stop.is_set():
                self.q.put(('log', 'Stopped.'))
                break
            src, track, out = job['src'], job['track'], job['out']
            self.q.put(('start', (i, job)))
            try:
                out.parent.mkdir(parents=True, exist_ok=True)
                cmd = build_command(src, track, out)
                self.q.put(('log', f"  {out.name}"))
                r = subprocess.run(cmd, capture_output=True, text=True)
                if r.returncode != 0:
                    raise RuntimeError((r.stderr or '')[-400:])
                size = out.stat().st_size if out.exists() else 0
                self.q.put(('done', (i, job, size)))
            except Exception as exc:
                self.q.put(('error', (i, job, str(exc))))
        self.q.put(('finished', None))


def open_track_extractor(app):
    """Open the Track Extractor window."""
    win = tk.Toplevel(app.root)
    win.withdraw()
    win.title("Docflix Track Extractor")
    win.geometry(scaled_geometry(win, 1000, 720))
    win.minsize(*scaled_minsize(win, 840, 600))
    win.update_idletasks()
    win.deiconify()

    # ── State ──
    _files = []          # list of Path
    _tracks = {}         # str(path) -> [track dicts]
    _want = {}           # str(path) -> {(kind, ord), ...}
    _jobs = []
    _worker = [None]
    _running = [False]
    _q = queue.Queue()

    frame = ttk.Frame(win, padding=8)
    frame.pack(fill='both', expand=True)
    frame.columnconfigure(0, weight=1)
    frame.rowconfigure(1, weight=3)
    frame.rowconfigure(4, weight=1)

    # ── Toolbar ──
    bar = ttk.Frame(frame)
    bar.grid(row=0, column=0, sticky='ew', pady=(0, 4))
    _btns = []

    def _tracks_of(path):
        key = str(path)
        if key not in _tracks:
            _tracks[key] = probe_tracks(path)
        return _tracks[key]

    def _seed(path):
        mode = seed_var.get()
        ts = _tracks_of(path)
        if mode == 'All audio':
            return {(t['kind'], t['ord']) for t in ts if t['kind'] == 'audio'}
        if mode == 'Commentary only':
            return {(t['kind'], t['ord']) for t in ts
                    if t['kind'] == 'audio' and t['role'] == 'commentary'}
        return set()

    def _wants(path):
        key = str(path)
        if key not in _want:
            _want[key] = _seed(path)
        return _want[key]

    def _add_paths(paths):
        added = 0
        for p in paths:
            path = Path(p)
            if path.is_dir():
                for child in sorted(path.iterdir()):
                    if (child.is_file()
                            and child.suffix.lower() in VIDEO_EXTENSIONS
                            and child not in _files):
                        _files.append(child)
                        added += 1
            elif (path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS
                    and path not in _files):
                _files.append(path)
                added += 1
        if added:
            _rebuild()
            _log(f"Added {added} file(s).")
        return added

    def _add_files():
        paths = ask_open_files(
            parent=win, title="Add video files",
            filetypes=[("Video files", " ".join(f"*{e}" for e in sorted(VIDEO_EXTENSIONS))),
                       ("All files", "*.*")])
        _add_paths(paths or [])

    def _add_folder():
        d = ask_directory(parent=win, title="Select a folder of video files")
        if d:
            _add_paths([d])

    def _remove_sel():
        rows = sorted({_row_of(i) for i in tree.selection()} - {None}, reverse=True)
        for r in rows:
            _want.pop(str(_files[r]), None)
            _files.pop(r)
        _rebuild()

    def _clear():
        _files.clear()
        _want.clear()
        _tracks.clear()
        _rebuild()

    for txt, cmd in (("Add Files", _add_files), ("Add Folder", _add_folder),
                     ("Remove", _remove_sel), ("Clear", _clear)):
        b = ttk.Button(bar, text=txt, command=cmd)
        b.pack(side='left', padx=(0, 4))
        _btns.append(b)

    ttk.Label(bar, text="Select:").pack(side='left', padx=(12, 4))
    seed_var = tk.StringVar(value=SEED_MODES[0])
    seed_cb = ttk.Combobox(bar, textvariable=seed_var, values=list(SEED_MODES),
                           state='readonly', width=16)
    seed_cb.pack(side='left')

    # ── Track tree ──
    tree_fr = ttk.LabelFrame(frame, text="Files and tracks")
    tree_fr.grid(row=1, column=0, sticky='nsew')
    tree_fr.columnconfigure(0, weight=1)
    tree_fr.rowconfigure(0, weight=1)

    tree = ttk.Treeview(tree_fr, columns=('use', 'out'), show='tree headings',
                        selectmode='extended')
    tree.heading('#0', text='File / track', anchor='w')
    tree.heading('use', text='Use', anchor='center')
    tree.heading('out', text='Will be written as', anchor='w')
    tree.column('#0', width=360, minwidth=200, stretch=True)
    tree.column('use', width=52, minwidth=44, stretch=False, anchor='center')
    tree.column('out', width=330, minwidth=160, stretch=True)
    tree.grid(row=0, column=0, sticky='nsew')
    sb = ttk.Scrollbar(tree_fr, orient='vertical', command=tree.yview)
    sb.grid(row=0, column=1, sticky='ns')
    tree['yscrollcommand'] = sb.set
    for tag, col in (('queued', COLOR_QUEUED), ('active', COLOR_ACTIVE),
                     ('done', COLOR_DONE), ('error', COLOR_ERROR)):
        tree.tag_configure(tag, foreground=col)

    def _row_of(iid):
        try:
            return int(iid.split('t')[0].lstrip('f'))
        except (ValueError, IndexError):
            return None

    def _key_of(iid):
        """(kind, ord) for a track row, or None for a file row."""
        if 't' not in iid:
            return None
        kind, _, o = iid.split('t')[1].partition('_')
        try:
            return ({'a': 'audio', 's': 'subtitle'}[kind], int(o))
        except (KeyError, ValueError):
            return None

    def _out_dir_for(src: Path):
        d = outdir_var.get().strip()
        return Path(d) if d else src.parent

    def _planned(path):
        """[(track, out_path)] for one file, with commentary numbering applied."""
        chosen = _wants(path)
        out = []
        counters = {}
        for t in _tracks_of(path):
            if (t['kind'], t['ord']) not in chosen:
                continue
            # Number within the ROLE, and only across chosen tracks, so ticking
            # just the second commentary still yields ...commentary1.mka rather
            # than a gap.
            role = t['role'] if t['kind'] == 'audio' else 'sub'
            counters[role] = counters.get(role, 0) + 1
            out.append((t, _out_dir_for(path) / output_name(path, t, counters[role])))
        return out

    def _rebuild():
        open_rows = {_row_of(i) for i in tree.get_children('')
                     if tree.item(i, 'open')}
        for i in tree.get_children(''):
            tree.delete(i)
        for row, path in enumerate(_files):
            ts = _tracks_of(path)
            chosen = _wants(path)
            plan = dict((id(t), o) for t, o in _planned(path))
            fid = f"f{row}"
            tree.insert('', 'end', iid=fid, text=path.name,
                        values=(f"{len(chosen)} / {len(ts)}", ''),
                        tags=('queued',),
                        open=(row in open_rows) or len(_files) <= 8)
            for t in ts:
                short = 'a' if t['kind'] == 'audio' else 's'
                on = (t['kind'], t['ord']) in chosen
                dest = plan.get(id(t))
                tree.insert(fid, 'end', iid=f"{fid}t{short}_{t['ord']}",
                            text='    ' + t['label'],
                            values=(GLYPH_ON if on else GLYPH_OFF,
                                    dest.name if dest else ''),
                            tags=('queued',))
        _refresh_summary()

    def _refresh_summary():
        n = sum(len(_planned(p)) for p in _files)
        count_var.set(f"{len(_files)} file(s)  --  {n} track"
                      f"{'s' if n != 1 else ''} to extract")
        extract_btn.config(state=('normal' if n and not _running[0] else 'disabled'))

    def _set_track(row, key, state):
        path = _files[row]
        chosen = _wants(path)
        if state:
            chosen.add(key)
        else:
            chosen.discard(key)

    def _on_click(event):
        if _running[0]:
            return None
        if tree.identify_region(event.x, event.y) != 'cell':
            return None
        if tree.identify_column(event.x) != '#1':
            return None
        iid = tree.identify_row(event.y)
        row = _row_of(iid)
        if row is None:
            return None
        key = _key_of(iid)
        if key is None:                       # file row: toggle everything
            ts = _tracks_of(_files[row])
            chosen = _wants(_files[row])
            state = len(chosen) < len(ts)
            for t in ts:
                _set_track(row, (t['kind'], t['ord']), state)
        else:
            _set_track(row, key, key not in _wants(_files[row]))
        _rebuild()
        return 'break'

    tree.bind('<Button-1>', _on_click, add='+')

    def _on_seed_change(_e=None):
        # Same contract as the Transcriber and sub_ripper: the dropdown is a
        # BULK SEED and re-picking it discards hand edits.
        if _running[0]:
            return
        for p in _files:
            _want[str(p)] = _seed(p)
        _rebuild()

    seed_cb.bind('<<ComboboxSelected>>', _on_seed_change)

    if HAS_DND:
        def _on_drop(event):
            if _running[0]:
                return
            try:
                paths = list(win.tk.splitlist(event.data))
            except Exception:
                paths = [p for p in (event.data or '').split() if p]
            _add_paths(paths)
        try:
            tree.drop_target_register(DND_FILES)
            tree.dnd_bind('<<Drop>>', _on_drop)
            win.drop_target_register(DND_FILES)
            win.dnd_bind('<<Drop>>', _on_drop)
        except Exception:
            pass

    count_var = tk.StringVar(value="0 file(s)")
    ttk.Label(frame, textvariable=count_var, anchor='w').grid(
        row=2, column=0, sticky='ew', pady=(2, 0))

    # ── Output ──
    out_fr = ttk.LabelFrame(frame, text="Output")
    out_fr.grid(row=3, column=0, sticky='ew', pady=(6, 0))
    outdir_var = tk.StringVar(value='')
    ttk.Label(out_fr, text="Folder:").pack(side='left', padx=(6, 4), pady=6)
    ttk.Entry(out_fr, textvariable=outdir_var).pack(
        side='left', fill='x', expand=True, pady=6)

    def _browse():
        d = ask_directory(parent=win, title="Where should extracted tracks go?")
        if d:
            outdir_var.set(d)
            _rebuild()

    ttk.Button(out_fr, text="Browse...", command=_browse).pack(
        side='left', padx=4, pady=6)

    def _same_folder():
        outdir_var.set('')
        _rebuild()

    ttk.Button(out_fr, text="Beside source", command=_same_folder).pack(
        side='left', padx=(0, 6), pady=6)

    # ── Log ──
    log_fr = ttk.LabelFrame(frame, text="Log")
    log_fr.grid(row=4, column=0, sticky='nsew', pady=(6, 0))
    log_fr.columnconfigure(0, weight=1)
    log_fr.rowconfigure(0, weight=1)
    log = tk.Text(log_fr, height=8, wrap='word', state='disabled')
    log.grid(row=0, column=0, sticky='nsew')
    log_sb = ttk.Scrollbar(log_fr, orient='vertical', command=log.yview)
    log_sb.grid(row=0, column=1, sticky='ns')
    log['yscrollcommand'] = log_sb.set

    def _log(msg):
        log.config(state='normal')
        log.insert('end', msg + '\n')
        log.see('end')
        log.config(state='disabled')

    # ── Actions ──
    act = ttk.Frame(frame)
    act.grid(row=5, column=0, sticky='ew', pady=(6, 0))

    def _start():
        jobs = []
        for path in _files:
            for t, out in _planned(path):
                jobs.append({'src': path, 'track': t, 'out': out})
        if not jobs:
            messagebox.showwarning("Nothing selected",
                                   "Tick at least one track to extract.",
                                   parent=win)
            return
        clashes = [j['out'] for j in jobs if j['out'].exists()]
        if clashes:
            if not messagebox.askyesno(
                    "Overwrite?",
                    f"{len(clashes)} file(s) already exist and will be "
                    f"overwritten, starting with:\n\n{clashes[0].name}\n\nContinue?",
                    parent=win):
                return
        _jobs.clear()
        _jobs.extend(jobs)
        _running[0] = True
        extract_btn.config(state='disabled')
        stop_btn.config(state='normal')
        for b in _btns:
            b.config(state='disabled')
        _log(f"Extracting {len(jobs)} track(s)...")
        _worker[0] = ExtractWorker(_q, jobs)
        _worker[0].start()

    def _stop():
        if _worker[0]:
            _worker[0].stop()
        stop_btn.config(state='disabled')

    extract_btn = ttk.Button(act, text="Extract Tracks", command=_start,
                             state='disabled')
    extract_btn.pack(side='left')
    stop_btn = ttk.Button(act, text="Stop", command=_stop, state='disabled')
    stop_btn.pack(side='left', padx=6)

    def _tag(job, tag):
        row = _files.index(job['src']) if job['src'] in _files else None
        if row is None:
            return
        t = job['track']
        short = 'a' if t['kind'] == 'audio' else 's'
        for iid in (f"f{row}t{short}_{t['ord']}", f"f{row}"):
            if tree.exists(iid):
                tree.item(iid, tags=(tag,))

    def _poll():
        try:
            while True:
                ev, data = _q.get_nowait()
                if ev == 'log':
                    _log(data)
                elif ev == 'start':
                    _tag(data[1], 'active')
                elif ev == 'done':
                    i, job, size = data
                    _tag(job, 'done')
                    _log(f"    -> {size / 1024 / 1024:.1f} MB")
                elif ev == 'error':
                    i, job, msg = data
                    _tag(job, 'error')
                    _log(f"    FAILED: {msg}")
                elif ev == 'finished':
                    _running[0] = False
                    stop_btn.config(state='disabled')
                    for b in _btns:
                        b.config(state='normal')
                    _refresh_summary()
                    _log("Done.")
        except queue.Empty:
            pass
        win.after(100, _poll)

    _log("Track Extractor ready — add a video file and tick the tracks to pull out.")
    _log("Audio is written as .mka so its title, language and flags survive;")
    _log("a raw .ac3 would lose all three.")
    _poll()
    return win


def main():
    root = tk.Tk()
    root.withdraw()

    class _App:
        pass
    app = _App()
    app.root = root
    w = open_track_extractor(app)
    w.protocol("WM_DELETE_WINDOW", root.destroy)
    root.mainloop()


if __name__ == '__main__':
    main()
