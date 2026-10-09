"""
Docflix Media Suite — Remux

One file, its tracks, tick what stays, drop in what's added, SEE the result,
then write it. Lossless: nothing is converted, nothing is "cleaned up".

⭐ Tony, 2026-10-07: the last app he used that wasn't his was MKVToolNix, and
only for two things — remuxing a file without its foreign audio, and adding a
commentary .mka from another copy. *"Being able to see the results before I
write them is handy."* So the right-hand pane is the point of this tool: it is
the file that WILL be written, in order, before anything touches the disk.

⛔ No metadata editing here, on purpose. Track names and flags are done in Media
Details (his call, same day). A track brings its own title/language/flags along.

Under the hood it is mkvmerge, the way the encoder is ffmpeg: plumbing.

⚠️ mkvmerge track IDs are per INPUT FILE. Every track here is keyed by
(source id, track id), and source ids are stable counters, not list positions —
removing an added file must not renumber the tracks of the ones after it. Input
FILE indices for --track-order are worked out only at command-build time.
"""

import json
import os
import queue
import re
import subprocess
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk, messagebox

from .constants import VIDEO_EXTENSIONS, SUBTITLE_EXTENSIONS, LANG_CODE_TO_NAME
from .utils import (ask_open_files, scaled_geometry, scaled_minsize,
                    load_module_prefs, save_module_prefs)

try:
    from tkinterdnd2 import DND_FILES
    HAS_DND = True
except ImportError:
    HAS_DND = False

PREFS = 'remux'
GLYPH_ON, GLYPH_OFF = '☑', '☐'
ADDABLE = VIDEO_EXTENSIONS | {'.mka'} | (SUBTITLE_EXTENSIONS - {'.idx'})
_TYPE_RANK = {'video': 0, 'audio': 1, 'subtitles': 2}
_TYPE_LETTER = {'video': 'V', 'audio': 'A', 'subtitles': 'S'}
_ENGLISH = {'eng', 'en'}
# ⚠️ 'und' counts as English for "English only": a main track nobody labelled is
# far more often the film's own audio than a dub, and unticking it would write a
# file with NO audio. Conservative: keep what we cannot classify.
_KEEP_AS_ENGLISH = _ENGLISH | {'und', ''}


# ── Engine (no Tk) ─────────────────────────────────────────────────────────────

def probe(path):
    """mkvmerge -J of *path* → (tracks, duration_seconds or None)."""
    r = subprocess.run(['mkvmerge', '-J', str(path)],
                       capture_output=True, text=True, timeout=60)
    d = json.loads(r.stdout or '{}')
    if not d.get('container', {}).get('recognized', True) or 'tracks' not in d:
        raise ValueError(f"mkvmerge can't read {Path(path).name}")
    dur = d.get('container', {}).get('properties', {}).get('duration')
    tracks = []
    for t in d['tracks']:
        p = t.get('properties', {})
        tracks.append({
            'id': t['id'], 'type': t['type'], 'codec': t.get('codec', ''),
            'lang': p.get('language') or 'und',
            'name': p.get('track_name') or '',
            'default': bool(p.get('default_track')),
            'forced': bool(p.get('forced_track')),
            'commentary': bool(p.get('flag_commentary')),
            'hi': bool(p.get('flag_hearing_impaired')),
            'channels': p.get('audio_channels'),
            'pixels': p.get('pixel_dimensions') or '',
            'set': {},            # overrides from the filename, see tags_from_name
        })
    return tracks, (dur / 1e9 if dur else None)


def tags_from_name(path):
    """Language/forced/SDH from a loose subtitle's filename.

    A bare .srt carries no language, so mkvmerge calls it 'und'. The Track
    Extractor and Tony's own files name them `<stem>.<lang>[.forced|.sdh].srt`,
    so the meaning lives in the name — read it back rather than writing an
    'und' track he then has to fix in Media Details.
    """
    out = {}
    for tok in Path(path).stem.lower().split('.')[1:]:
        if tok in LANG_CODE_TO_NAME and tok != 'und':
            out['lang'] = tok
        elif tok == 'forced':
            out['forced'] = True
        elif tok in ('sdh', 'cc', 'hi'):
            out['hi'] = True
    return out


def describe(t):
    """One-line description of a track for both panes."""
    lang = t['set'].get('lang', t['lang'])
    bits = [lang, t['codec']]
    if t['type'] == 'video' and t['pixels']:
        bits.append(t['pixels'])
    if t['type'] == 'audio' and t['channels']:
        bits.append({1: 'mono', 2: '2.0', 6: '5.1', 8: '7.1'}.get(
            t['channels'], f"{t['channels']}ch"))
    flags = [f for f, on in (('default', t['default']),
                             ('forced', t['set'].get('forced', t['forced'])),
                             ('commentary', t['commentary']),
                             ('SDH', t['set'].get('hi', t['hi']))) if on]
    if flags:
        bits.append('[' + ', '.join(flags) + ']')
    if t['name']:
        bits.append(f'"{t["name"]}"')
    return '  '.join(b for b in bits if b)


def insert_pos(order, track_type, type_of):
    """Where a newly kept track goes: after the last kept track of its type
    (or of an earlier type), so video/audio/subtitle groups stay together."""
    rank = _TYPE_RANK.get(track_type, 3)
    pos = 0
    for i, key in enumerate(order):
        if _TYPE_RANK.get(type_of(key), 3) <= rank:
            pos = i + 1
    return pos


def build_command(sources, order, out_path):
    """mkvmerge args writing the tracks in *order* to *out_path*.

    sources: [{'sid', 'path', 'tracks', 'main': bool}], main first.
    order:   [(sid, track_id)] — exactly the tracks to keep, in output order.

    The main file keeps its chapters, attachments and global tags (that's where
    the DOCFLIX_ENCODE stamp lives). Added files contribute TRACKS only: their
    chapters and tags would otherwise be merged into this file's.
    """
    keep = set(order)
    cmd = ['mkvmerge', '--gui-mode', '-o', str(out_path)]
    file_idx = {}
    for src in sources:
        mine = [t for t in src['tracks'] if (src['sid'], t['id']) in keep]
        if not mine and not src['main']:
            continue                       # nothing taken from it: don't open it
        file_idx[src['sid']] = len(file_idx)
        for ttype, keep_opt, none_opt in (
                ('video', '--video-tracks', '--no-video'),
                ('audio', '--audio-tracks', '--no-audio'),
                ('subtitles', '--subtitle-tracks', '--no-subtitles')):
            ids = [str(t['id']) for t in mine if t['type'] == ttype]
            cmd += [keep_opt, ','.join(ids)] if ids else [none_opt]
        # buttons/data tracks: never carried
        cmd += ['--no-buttons']
        if not src['main']:
            cmd += ['--no-chapters', '--no-attachments', '--no-global-tags']
        for t in mine:
            s = t['set']
            if 'lang' in s:
                cmd += ['--language', f"{t['id']}:{s['lang']}"]
            if s.get('forced'):
                cmd += ['--forced-display-flag', f"{t['id']}:1"]
            if s.get('hi'):
                cmd += ['--hearing-impaired-flag', f"{t['id']}:1"]
        cmd.append(str(src['path']))
    cmd += ['--track-order',
            ','.join(f"{file_idx[sid]}:{tid}" for sid, tid in order)]
    return cmd


_EPISODE = re.compile(r'[Ss](\d{1,2})[Ee](\d{1,3})')


def episode_key(path):
    """(season, episode) from a file name, or None — how a dropped .mka finds
    its video when several episodes are loaded at once."""
    m = _EPISODE.search(Path(path).name)
    return (int(m.group(1)), int(m.group(2))) if m else None


def job_summary(job):
    """(summary text, changed?) for one file's job, without touching the window."""
    srcs, order = job['sources'], job['order']
    m = srcs[0]
    type_of = {(s['sid'], t['id']): t['type'] for s in srcs for t in s['tracks']}
    dropped = sum(1 for t in m['tracks'] if (m['sid'], t['id']) not in order)
    added = sum(1 for k in order if k[0] != m['sid'])
    counts = {ty: sum(1 for k in order if type_of.get(k) == ty)
              for ty in ('video', 'audio', 'subtitles')}
    text = (f"{counts['video']} video · {counts['audio']} audio · "
            f"{counts['subtitles']} subtitle   —   {dropped} dropped, {added} added")
    changed = order != [(m['sid'], t['id']) for t in m['tracks']]
    return text, changed


def new_file_name(main_path):
    p = Path(main_path)
    return p.with_name(f"{p.stem} (remux){p.suffix if p.suffix == '.mkv' else '.mkv'}")


def verify(out_path, expected_tracks, main_duration):
    """Check the written file BEFORE it is allowed to replace anything.

    ⚠️ mkvmerge exit 0/1 is not proof: this re-reads the output and checks the
    track count and the length. A short file must never be swapped in over the
    original — that is the one mistake this tool can make that costs a file.
    """
    tracks, dur = probe(out_path)
    if len(tracks) != expected_tracks:
        return f"expected {expected_tracks} tracks, the new file has {len(tracks)}"
    if main_duration and (dur is None or dur < main_duration - 1.0):
        got = f"{dur:.1f}s" if dur else "unknown"
        return f"the new file is shorter than the original ({got} vs {main_duration:.1f}s)"
    return None


# ── Window ─────────────────────────────────────────────────────────────────────

def open_remux(app, path=None):
    """Open the Remux window, optionally on *path*."""
    win = tk.Toplevel(app.root)
    win.withdraw()
    win.title("Docflix Remux")
    win.geometry(scaled_geometry(win, 1180, 680))
    win.minsize(*scaled_minsize(win, 900, 520))
    win.update_idletasks()
    win.deiconify()

    prefs = load_module_prefs(PREFS)
    # ⭐ Batch (Tony, 2026-10-09 — after two days of one at a time): several
    # files can be loaded at once. Each is a JOB with its own sources/order;
    # `sources` and `order` below are always the job ON SCREEN, so every
    # single-file path (tick, move, add, remove) works unchanged on it.
    # The safety habit he chose one-at-a-time for is kept: each file is still
    # looked at in the Result pane, and each is verified before it replaces.
    jobs = []                    # [{'sources', 'order', 'state'}]
    sources = []                 # [{'sid','path','tracks','main','duration'}]
    order = []                   # [(sid, tid)] kept, in output order
    _sid = [0]
    _running = [False]
    _proc = [None]
    _q = queue.Queue()
    _quiet = [False]             # set while the code itself selects a file row

    def _main():
        return sources[0] if sources and sources[0]['main'] else None

    def _track(key):
        for s in sources:
            if s['sid'] == key[0]:
                for t in s['tracks']:
                    if t['id'] == key[1]:
                        return s, t
        return None, None

    def _type_of(key):
        return _track(key)[1]['type']

    def _cur():
        return next((i for i, j in enumerate(jobs) if j['sources'] is sources), None)

    def _show(i):
        nonlocal sources, order
        sources, order = jobs[i]['sources'], jobs[i]['order']

    frame = ttk.Frame(win, padding=8)
    frame.pack(fill='both', expand=True)
    frame.columnconfigure(0, weight=3)
    frame.columnconfigure(1, weight=2)
    frame.rowconfigure(2, weight=1)

    # ── Toolbar ──
    bar = ttk.Frame(frame)
    bar.grid(row=0, column=0, columnspan=2, sticky='ew', pady=(0, 6))
    file_var = tk.StringVar(value="Drop a video file here, or Open…")
    _btns = []

    # ── Files (only shown when more than one is loaded) ──
    files = ttk.LabelFrame(frame, text="Files  (click one to see and change it · Delete takes it out)")
    files.columnconfigure(0, weight=1)
    jt = ttk.Treeview(files, columns=('st', 'file', 'sum'), show='headings',
                      selectmode='browse', height=6)
    jt.heading('st', text='', anchor='center')
    jt.heading('file', text='File', anchor='w')
    jt.heading('sum', text='Result', anchor='w')
    jt.column('st', width=34, minwidth=30, stretch=False, anchor='center')
    jt.column('file', width=520, minwidth=200, stretch=True)
    jt.column('sum', width=420, minwidth=200, stretch=True)
    jt.grid(row=0, column=0, sticky='nsew')
    jsb = ttk.Scrollbar(files, orient='vertical', command=jt.yview)
    jsb.grid(row=0, column=1, sticky='ns')
    jt['yscrollcommand'] = jsb.set
    jt.tag_configure('same', foreground='gray')
    jt.tag_configure('bad', foreground='#b00020')

    # ── Left: tracks ──
    left = ttk.LabelFrame(frame, text="Tracks  (tick what stays · drop .mka / .srt / another video to add)")
    left.grid(row=2, column=0, sticky='nsew', padx=(0, 6))
    left.columnconfigure(0, weight=1)
    left.rowconfigure(0, weight=1)
    tree = ttk.Treeview(left, columns=('keep', 'what'), show='tree headings',
                        selectmode='browse')
    tree.heading('#0', text='', anchor='w')
    tree.heading('keep', text='Keep', anchor='center')
    tree.heading('what', text='Track', anchor='w')
    tree.column('#0', width=150, minwidth=90, stretch=False)
    tree.column('keep', width=52, minwidth=44, stretch=False, anchor='center')
    tree.column('what', width=520, minwidth=200, stretch=True)
    tree.grid(row=0, column=0, sticky='nsew')
    sb = ttk.Scrollbar(left, orient='vertical', command=tree.yview)
    sb.grid(row=0, column=1, sticky='ns')
    tree['yscrollcommand'] = sb.set
    tree.tag_configure('src', font=('TkDefaultFont', 10, 'bold'))
    tree.tag_configure('off', foreground='gray')

    # ── Right: the result ──
    right = ttk.LabelFrame(frame, text="Result  (what will be written)")
    right.grid(row=2, column=1, sticky='nsew')
    right.columnconfigure(0, weight=1)
    right.rowconfigure(0, weight=1)
    res = ttk.Treeview(right, columns=('n', 't', 'what'), show='headings',
                       selectmode='browse')
    res.heading('n', text='#', anchor='center')
    res.heading('t', text='', anchor='center')
    res.heading('what', text='Track', anchor='w')
    res.column('n', width=34, minwidth=30, stretch=False, anchor='center')
    res.column('t', width=30, minwidth=26, stretch=False, anchor='center')
    res.column('what', width=360, minwidth=160, stretch=True)
    res.grid(row=0, column=0, sticky='nsew')
    rsb = ttk.Scrollbar(right, orient='vertical', command=res.yview)
    rsb.grid(row=0, column=1, sticky='ns')
    res['yscrollcommand'] = rsb.set
    res.tag_configure('added', foreground='#1f6fb2')
    mv = ttk.Frame(right)
    mv.grid(row=1, column=0, columnspan=2, sticky='ew', pady=(4, 2))
    summary_var = tk.StringVar(value='')
    ttk.Label(right, textvariable=summary_var, anchor='w').grid(
        row=2, column=0, columnspan=2, sticky='ew', padx=4, pady=(0, 4))

    # ── Bottom ──
    bot = ttk.Frame(frame)
    bot.grid(row=3, column=0, columnspan=2, sticky='ew', pady=(8, 0))
    replace_var = tk.BooleanVar(value=bool(prefs.get('replace', False)))
    status_var = tk.StringVar(value='')
    prog = ttk.Progressbar(bot, mode='determinate', maximum=100, length=220)

    def _rebuild():
        tree.delete(*tree.get_children())
        for s in sources:
            label = 'This file' if s['main'] else 'Added'
            parent = tree.insert('', 'end', iid=f"s{s['sid']}", text=label,
                                 values=('', s['path'].name), open=True, tags=('src',))
            seen = {}
            for t in s['tracks']:
                on = (s['sid'], t['id']) in order
                # Counted per type (S1 = the first subtitle), as a person counts
                # them — not mkvmerge's file-wide ID, where S1 is often "S2".
                seen[t['type']] = seen.get(t['type'], 0) + 1
                tree.insert(parent, 'end', iid=f"s{s['sid']}t{t['id']}",
                            text=f"   {_TYPE_LETTER.get(t['type'], '?')}{seen[t['type']]}",
                            values=(GLYPH_ON if on else GLYPH_OFF, describe(t)),
                            tags=() if on else ('off',))
        sel = res.selection()
        res.delete(*res.get_children())
        for n, key in enumerate(order, 1):
            s, t = _track(key)
            what = describe(t) + ('' if s['main'] else f"   ← {s['path'].name}")
            res.insert('', 'end', iid=f"r{key[0]}_{key[1]}",
                       values=(n, _TYPE_LETTER.get(t['type'], '?'), what),
                       tags=() if s['main'] else ('added',))
        for iid in sel:
            if res.exists(iid):
                res.selection_set(iid)
        m = _main()
        if m:
            file_var.set(str(m['path']))
            summary_var.set(job_summary({'sources': sources, 'order': order})[0])
        else:
            file_var.set("Drop a video file here, or Open…")
            summary_var.set('')
        _rebuild_files()
        _refresh_buttons()

    def _rebuild_files():
        if len(jobs) < 2:
            files.grid_remove()
            return
        files.grid(row=1, column=0, columnspan=2, sticky='nsew', pady=(0, 6))
        jt.delete(*jt.get_children())
        for i, j in enumerate(jobs):
            text, changed = job_summary(j)
            # An added file that contributes nothing is the batch mistake worth
            # catching at a glance: it was paired, but nothing of it is ticked.
            idle = [s['path'].name for s in j['sources'][1:]
                    if not any(k[0] == s['sid'] for k in j['order'])]
            if idle:
                text += f"   ⚠ nothing taken from {', '.join(idle)}"
            tag = ('bad',) if j['state'] == '✖' or idle else (() if changed else ('same',))
            jt.insert('', 'end', iid=f"j{i}", tags=tag,
                      values=(j['state'], j['sources'][0]['path'].name,
                              text if changed or idle else 'no change — will be skipped'))
        c = _cur()
        if c is not None:
            _quiet[0] = True
            jt.selection_set(f"j{c}")
            jt.see(f"j{c}")
            win.after_idle(lambda: _quiet.__setitem__(0, False))

    def _on_pick(_event=None):
        if _quiet[0] or _running[0]:
            return
        sel = jt.selection()
        if sel:
            i = int(sel[0][1:])
            if i != _cur():
                _show(i)
                _rebuild()

    def _drop_job(_event=None):
        if _running[0] or len(jobs) < 2:
            return
        sel = jt.selection()
        if not sel:
            return
        i = int(sel[0][1:])
        jobs.pop(i)
        _show(min(i, len(jobs) - 1))
        _rebuild()

    jt.bind('<<TreeviewSelect>>', _on_pick)
    jt.bind('<Delete>', _drop_job)

    def _to_write():
        return [j for j in jobs if job_summary(j)[1] and j['order']]

    def _refresh_buttons():
        for b in _btns:
            b.configure(state='normal' if (not _running[0]) else 'disabled')
        n = len(_to_write())
        write_btn.configure(text="Write" if len(jobs) < 2 else f"Write All ({n})",
                            state='normal' if (n and not _running[0]) else 'disabled')

    def _add_source(path, main):
        try:
            tracks, dur = probe(path)
        except Exception as e:
            messagebox.showerror("Remux", f"Couldn't read {Path(path).name}:\n{e}",
                                 parent=win)
            return
        _sid[0] += 1
        sid = _sid[0]
        ext = Path(path).suffix.lower()
        if ext in SUBTITLE_EXTENSIONS:
            tg = tags_from_name(path)
            for t in tracks:
                if tg.get('lang') and t['lang'] == 'und':
                    t['set']['lang'] = tg['lang']
                if tg.get('forced'):
                    t['set']['forced'] = True
                if tg.get('hi'):
                    t['set']['hi'] = True
        src = {'sid': sid, 'path': Path(path), 'tracks': tracks,
               'main': main, 'duration': dur}
        if main:
            sources.clear()
            order.clear()
            sources.append(src)
            # Everything ticked: untick what goes. (Same rule as the Track
            # Extractor, Tony 2026-08-22.)
            order.extend((sid, t['id']) for t in tracks)
        else:
            sources.append(src)
            # ⭐ An added .mka/.srt was dropped FOR its tracks — tick them.
            # An added VIDEO is a donor you take one or two tracks from, so
            # nothing is ticked: you'd otherwise get its whole picture and
            # soundtrack stacked on yours.
            if ext not in VIDEO_EXTENSIONS:
                for t in tracks:
                    order.insert(insert_pos(order, t['type'], _type_of),
                                 (sid, t['id']))

    def _new_job(p):
        """A new file of its own. Returns its index, or None if unreadable."""
        nonlocal sources, order
        keep = (sources, order)
        sources, order = [], []
        _add_source(p, main=True)
        if not sources:
            sources, order = keep
            return None
        jobs.append({'sources': sources, 'order': order, 'state': ''})
        return len(jobs) - 1

    def _job_for(p):
        """The loaded file *p* belongs to, by its S01E01 — None if it has no
        episode number, -1 if it has one that matches no loaded file."""
        k = episode_key(p)
        if not k:
            return None
        hits = [i for i, j in enumerate(jobs)
                if episode_key(j['sources'][0]['path']) == k
                and j['sources'][0]['path'] != p]
        return hits[0] if len(hits) == 1 else -1

    def _take_paths(paths):
        if _running[0]:
            return
        paths = [Path(p) for p in paths if Path(p).is_file()]
        vids = [p for p in paths if p.suffix.lower() in VIDEO_EXTENSIONS]
        rest = [p for p in paths if p.suffix.lower() in ADDABLE and p not in vids]
        show = _cur()
        if not jobs:
            if not vids:
                messagebox.showinfo("Remux", "Start with the video file you want to "
                                    "remux — then drop the tracks to add.", parent=win)
                return
            keys = [episode_key(v) for v in vids]
            if any(k and keys.count(k) > 1 for k in keys):
                messagebox.showinfo(
                    "Remux", "Two of those videos are the same episode, so I can't "
                    "tell which one you're remuxing.\n\nDrop the files to remux "
                    "first, then the copies to take tracks from.", parent=win)
                return
            for v in vids:
                i = _new_job(v)
                if show is None and i is not None:
                    show = i
        else:
            rest = vids + rest
        mains = {j['sources'][0]['path'] for j in jobs}
        lost = []
        for p in rest:
            if p in mains:
                continue
            i = _job_for(p)
            if i == -1 or (i is None and len(jobs) > 1 and p in vids):
                if p in vids and i == -1:
                    _new_job(p)            # another episode: a file of its own
                else:
                    lost.append(p.name)
                continue
            if i is None:
                i = show if show is not None else 0   # no episode number: this file
            if any(s['path'] == p for s in jobs[i]['sources']):
                continue
            _show(i)
            _add_source(p, main=False)
        if jobs:
            _show(show if show is not None and show < len(jobs) else 0)
        _rebuild()
        if lost:
            messagebox.showinfo(
                "Remux", "These don't match any file that's loaded (by S01E01), "
                "so they weren't added:\n\n" + "\n".join(lost), parent=win)

    def _open():
        paths = ask_open_files(
            parent=win, title="Open videos to remux",
            filetypes=[("Video files", " ".join(f"*{e}" for e in sorted(VIDEO_EXTENSIONS))),
                       ("All files", "*.*")])
        if paths:
            _clear()
            _take_paths(paths)

    def _add():
        if not _main():
            return _open()
        paths = ask_open_files(
            parent=win, title="Add tracks from…",
            filetypes=[("Tracks or videos", " ".join(f"*{e}" for e in sorted(ADDABLE))),
                       ("All files", "*.*")])
        _take_paths(paths or [])

    def _clear():
        nonlocal sources, order
        if _running[0]:
            return
        jobs.clear()
        sources, order = [], []
        status_var.set('')
        prog['value'] = 0
        _rebuild()

    def _remove_added():
        sel = tree.selection()
        if not sel:
            return
        iid = sel[0]
        sid = int(re.match(r's(\d+)', iid).group(1))
        src = next((s for s in sources if s['sid'] == sid), None)
        if not src or src['main']:
            return
        sources.remove(src)
        order[:] = [k for k in order if k[0] != sid]
        _rebuild()

    def _english_only():
        # Every loaded file: with a season loaded, this is the point of it.
        for j in jobs:
            for s in j['sources']:
                for t in s['tracks']:
                    key = (s['sid'], t['id'])
                    lang = t['set'].get('lang', t['lang'])
                    if (t['type'] in ('audio', 'subtitles') and key in j['order']
                            and lang not in _KEEP_AS_ENGLISH):
                        j['order'].remove(key)
        _rebuild()

    def _move(delta):
        sel = res.selection()
        if not sel:
            return
        m = re.match(r'r(\d+)_(\d+)', sel[0])
        key = (int(m.group(1)), int(m.group(2)))
        i = order.index(key)
        j = i + delta
        if 0 <= j < len(order):
            order[i], order[j] = order[j], order[i]
            _rebuild()
            res.see(f"r{key[0]}_{key[1]}")

    def _on_click(event):
        if _running[0] or tree.identify_region(event.x, event.y) != 'cell':
            return None
        if tree.identify_column(event.x) != '#1':
            return None
        m = re.match(r's(\d+)t(\d+)$', tree.identify_row(event.y) or '')
        if not m:
            return None
        key = (int(m.group(1)), int(m.group(2)))
        if key in order:
            order.remove(key)
        else:
            order.insert(insert_pos(order, _type_of(key), _type_of), key)
        _rebuild()
        return 'break'

    tree.bind('<Button-1>', _on_click, add='+')

    for txt, cmd in (("Open…", _open), ("Add Tracks…", _add),
                     ("Remove Added File", _remove_added),
                     ("English Only", _english_only), ("Clear", _clear)):
        b = ttk.Button(bar, text=txt, command=cmd)
        b.pack(side='left', padx=(0, 6))
        _btns.append(b)
    ttk.Label(bar, textvariable=file_var, anchor='w', foreground='gray').pack(
        side='left', fill='x', expand=True, padx=(8, 0))
    for txt, d in (("▲ Up", -1), ("▼ Down", 1)):
        b = ttk.Button(mv, text=txt, command=lambda d=d: _move(d))
        b.pack(side='left', padx=(4, 0))
        _btns.append(b)

    if HAS_DND:
        def _on_drop(event):
            try:
                paths = list(win.tk.splitlist(event.data))
            except Exception:
                paths = [p for p in (event.data or '').split() if p]
            # ⚠️ Deferred: never do real work (dialogs, probes) inside the
            # drop callback itself — see the 2026-08-07 vanishing-window bug.
            win.after(30, lambda: _take_paths(paths))
        for w in (win, tree, res, jt):
            try:
                w.drop_target_register(DND_FILES)
                w.dnd_bind('<<Drop>>', _on_drop)
            except Exception:
                pass

    # ── Write ──
    def _save_pref():
        save_module_prefs(PREFS, {'replace': bool(replace_var.get())})

    ttk.Checkbutton(bot, text="Replace the original file (after the new one is checked)",
                    variable=replace_var, command=_save_pref).pack(side='left')
    write_btn = ttk.Button(bot, text="Write", state='disabled')
    write_btn.pack(side='right')
    prog.pack(side='right', padx=(0, 8))
    ttk.Label(bot, textvariable=status_var, anchor='e').pack(
        side='right', padx=(0, 8))

    def _write():
        todo = _to_write()
        if not todo or _running[0]:
            return

        def has_video(j):
            tt = {(s['sid'], t['id']): t['type'] for s in j['sources'] for t in s['tracks']}
            return any(tt.get(k) == 'video' for k in j['order'])
        novid = [j['sources'][0]['path'].name for j in todo if not has_video(j)]
        if novid and not messagebox.askyesno(
                "Remux", "No video track in the result for:\n\n" + "\n".join(novid)
                + "\n\nWrite anyway?", parent=win, default='no'):
            return
        replace = bool(replace_var.get())
        plan = []
        for j in todo:
            mp = j['sources'][0]['path']
            out = (mp.with_name(f".{mp.stem}.remux-tmp.mkv") if replace
                   else new_file_name(mp))
            plan.append((j, out, build_command(j['sources'], j['order'], out),
                         len(j['order']), j['sources'][0]['duration']))
        if not replace:
            there = [out.name for _, out, *_ in plan if out.exists()]
            if there and not messagebox.askyesno(
                    "Remux", "Already exists:\n\n" + "\n".join(there)
                    + "\n\nOverwrite?", parent=win, default='no'):
                return
        for j in jobs:
            j['state'] = '…' if any(p[0] is j for p in plan) else j['state']
        _running[0] = True
        _refresh_buttons()
        _rebuild_files()
        prog['value'] = 0
        status_var.set("Writing…")
        total = len(plan)

        def one(k, j, out, cmd, n_tracks, duration):
            msgs = []
            try:
                p = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True)
                _proc[0] = p
                for line in p.stdout:
                    line = line.strip()
                    mm = re.match(r'#GUI#progress (\d+)%', line)
                    if mm:
                        _q.put(('prog', (k, total, int(mm.group(1)))))
                    elif line.startswith(('#GUI#error', '#GUI#warning')):
                        msgs.append(line.split(' ', 1)[-1])
                rc = p.wait()
                if rc not in (0, 1):
                    raise RuntimeError("mkvmerge failed:\n" + "\n".join(msgs[-6:]))
                problem = verify(out, n_tracks, duration)
                if problem:
                    raise RuntimeError(f"The new file failed its check: {problem}.\n\n"
                                       "Nothing was replaced.")
                final = out
                if replace:
                    os.replace(out, j['sources'][0]['path'])
                    final = j['sources'][0]['path']
                return final, msgs, None
            except Exception as e:
                # A file that failed is never left lying around to be mistaken
                # for a good one. (`out` is never the original: replace mode
                # writes a hidden temp beside it.)
                if out.exists():
                    try:
                        out.unlink()
                    except OSError:
                        pass
                return None, msgs, str(e)
            finally:
                _proc[0] = None

        def work():
            # One after another, never in parallel: each is verified before it
            # replaces anything, and a failure stops nothing but itself.
            results = []
            for k, (j, out, cmd, n_tracks, duration) in enumerate(plan):
                if not _running[0]:
                    break
                r = one(k, j, out, cmd, n_tracks, duration)
                results.append((j,) + r)
                _q.put(('one', (j, r[2] is None)))
            _q.put(('done', results))

        threading.Thread(target=work, daemon=True).start()

    write_btn.configure(command=_write)

    def _reload(j, final):
        """Show what's now on disk for job *j*, so the panes tell the truth."""
        here = _cur()
        _show(next(i for i, x in enumerate(jobs) if x is j))
        sources.clear()
        order.clear()
        _add_source(final, main=True)
        j['state'] = '✔'
        if here is not None:
            _show(here)

    def _poll():
        try:
            while True:
                kind, val = _q.get_nowait()
                if kind == 'prog':
                    k, total, pct = val
                    prog['value'] = int((k * 100 + pct) / total)
                    status_var.set(f"Writing… {pct}%" if total == 1
                                   else f"Writing {k + 1} of {total}… {pct}%")
                elif kind == 'one':
                    j, ok = val
                    j['state'] = '✔' if ok else '✖'
                    _rebuild_files()
                elif kind == 'done':
                    _running[0] = False
                    ok = [(j, final, msgs) for j, final, msgs, err in val if not err]
                    bad = [(j, err) for j, final, msgs, err in val if err]
                    warn = [m for _, _, msgs, _ in val for m in msgs]
                    for j, final, _ in ok:
                        if hasattr(app, 'add_log'):
                            app.add_log(f"Remux written: {final}", 'SUCCESS')
                        if any(x is j for x in jobs):
                            _reload(j, final)
                    prog['value'] = 100 if ok and not bad else prog['value']
                    if len(val) == 1 and ok:
                        status_var.set(f"✔ Written: {ok[0][1].name}")
                    elif len(val) == 1:
                        status_var.set("✖ Failed")
                    else:
                        status_var.set(f"✔ {len(ok)} written" +
                                       (f", ✖ {len(bad)} failed" if bad else ""))
                    _rebuild()
                    if bad:
                        messagebox.showerror(
                            "Remux", "\n\n".join(
                                (f"{j['sources'][0]['path'].name}:\n" if len(val) > 1 else "")
                                + err for j, err in bad), parent=win)
                    elif warn:
                        messagebox.showinfo("Remux — written with warnings",
                                            "\n".join(warn[-6:]), parent=win)
        except queue.Empty:
            pass
        if win.winfo_exists():
            win.after(150, _poll)

    def _on_close():
        if _running[0]:
            if not messagebox.askyesno("Remux", "A remux is being written.\n\n"
                                       "Stop it and close?", parent=win, default='no'):
                return
            _running[0] = False          # no further files after this one
            p = _proc[0]
            if p:
                p.kill()
        win.destroy()

    win.protocol('WM_DELETE_WINDOW', _on_close)
    win.add_paths = _take_paths     # same as a drop; lets other tools hand files over
    _poll()
    _rebuild()
    if path:
        win.after(50, lambda: _take_paths([path]))
    return win


def main():
    import sys
    try:
        from tkinterdnd2 import TkinterDnD
        root = TkinterDnD.Tk()
    except Exception:
        root = tk.Tk()
    root.withdraw()

    class _App:
        pass
    app = _App()
    app.root = root
    w = open_remux(app, sys.argv[1] if len(sys.argv) > 1 else None)
    w.protocol("WM_DELETE_WINDOW", root.destroy)
    root.mainloop()


if __name__ == '__main__':
    main()
