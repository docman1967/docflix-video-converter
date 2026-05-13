"""
Docflix Media Suite — Sub Extractor

Batch subtitle extraction tool. Probes video files for embedded
subtitle streams, lets the user select which English subtitle types
to extract (Main, Forced, SDH, or All English), and rips them to
SRT, ASS, or WebVTT files alongside the source videos.
"""

import json
import os
import re
import subprocess
import threading
import time as _time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import ttk, messagebox

from .constants import (APP_NAME, VIDEO_EXTENSIONS, BITMAP_SUB_CODECS,
                        SUBTITLE_LANGUAGES, LANG_CODE_TO_NAME)
from .gpu import detect_closed_captions, extract_closed_captions_to_srt
from .utils import (format_size, get_subtitle_streams, scaled_geometry,
                    scaled_minsize, center_window_on_parent, ask_open_files,
                    ask_directory)

try:
    from tkinterdnd2 import DND_FILES
    HAS_DND = True
except ImportError:
    HAS_DND = False


# ═══════════════════════════════════════════════════════════════════
# Output format settings
# ═══════════════════════════════════════════════════════════════════

_OUTPUT_FORMATS = {
    'Original': {'codec': 'copy',   'ext': None},   # stream copy, native ext
    'SRT':      {'codec': 'srt',    'ext': '.srt'},
    'ASS':      {'codec': 'ass',    'ext': '.ass'},
    'WebVTT':   {'codec': 'webvtt', 'ext': '.vtt'},
}

# Map ffprobe codec_name → native file extension for stream copy mode.
# Only text-based codecs — bitmap codecs are filtered out before this.
_CODEC_TO_EXT = {
    'subrip':       '.srt',
    'srt':          '.srt',
    'ass':          '.ass',
    'ssa':          '.ssa',
    'webvtt':       '.vtt',
    'mov_text':     '.srt',     # MP4 text subs → extract as SRT
    'text':         '.srt',     # generic text → SRT
    'ttml':         '.ttml',
    'dfxp':         '.ttml',
    'realtext':     '.rt',
    'sami':         '.smi',
    'microdvd':     '.sub',
    'mpl2':         '.mpl',
    'jacosub':      '.jss',
    'stl':          '.stl',
    'subviewer':    '.sub',
    'subviewer1':   '.sub',
    'vplayer':      '.txt',
    'pjs':          '.pjs',
}

# Language matching: 3-letter code is canonical, but ffprobe may
# report 2-letter ISO 639-1 codes.  Map common 2-letter → 3-letter.
_LANG_2TO3 = {
    'en': 'eng', 'es': 'spa', 'fr': 'fra', 'de': 'deu',
    'it': 'ita', 'pt': 'por', 'ru': 'rus', 'ja': 'jpn',
    'ko': 'kor', 'zh': 'zho', 'ar': 'ara', 'hi': 'hin',
    'nl': 'nld', 'pl': 'pol', 'sv': 'swe', 'tr': 'tur',
    'vi': 'vie',
}

_ALL_LANGUAGES = 'All Languages'

# Reverse lookup: display name → 3-letter code
_NAME_TO_CODE = {name: code for code, name in SUBTITLE_LANGUAGES}

# Reverse lookup: 3-letter code → display name (for dynamic dropdown)
_CODE_TO_NAME = {code: name for code, name in SUBTITLE_LANGUAGES}


# ═══════════════════════════════════════════════════════════════════
# Completion sound
# ═══════════════════════════════════════════════════════════════════

def _play_done_sound():
    """Play the freedesktop completion sound via ffplay."""
    try:
        sound = '/usr/share/sounds/freedesktop/stereo/complete.oga'
        if os.path.exists(sound):
            subprocess.Popen(
                ['ffplay', '-nodisp', '-autoexit', '-loglevel', 'quiet',
                 sound],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════
# Main window
# ═══════════════════════════════════════════════════════════════════

def open_sub_ripper(app):
    """Open the Sub Extractor window."""
    win = tk.Toplevel(app.root)
    win.withdraw()
    win.title("Docflix Sub Extractor")
    geom_str = scaled_geometry(win, 920, 720)
    win.geometry(geom_str)
    win.minsize(*scaled_minsize(win, 750, 520))
    win.update_idletasks()
    try:
        gm = re.match(r'(\d+)x(\d+)', geom_str)
        dw = int(gm.group(1)) if gm else win.winfo_reqwidth()
        dh = int(gm.group(2)) if gm else win.winfo_reqheight()
        pw = app.root.winfo_width()
        ph = app.root.winfo_height()
        px = app.root.winfo_x()
        py = app.root.winfo_y()
        x = px + (pw - dw) // 2
        y = py + (ph - dh) // 2
        win.geometry(f'{dw}x{dh}+{max(0, x)}+{max(0, y)}')
    except Exception:
        pass
    win.deiconify()

    # ── State ──
    sr_files = []           # list of file dicts
    _scanning = [False]     # prevent overlapping scans
    _processing = [False]
    _stop = [False]

    # ── Load preferences ──
    _sr_prefs = getattr(app, '_prefs', {}).get('sub_ripper', {})

    opt_language    = tk.StringVar(value=_sr_prefs.get('language', 'English'))
    opt_main        = tk.BooleanVar(value=_sr_prefs.get('main', True))
    opt_forced      = tk.BooleanVar(value=_sr_prefs.get('forced', False))
    opt_sdh         = tk.BooleanVar(value=_sr_prefs.get('sdh', False))
    opt_cc          = tk.BooleanVar(value=_sr_prefs.get('cc', False))
    opt_format      = tk.StringVar(value=_sr_prefs.get('format', 'SRT'))
    opt_overwrite   = tk.BooleanVar(value=_sr_prefs.get('overwrite', False))

    # ── Main frame ──
    main_frame = ttk.Frame(win, padding=10)
    main_frame.pack(fill='both', expand=True)
    main_frame.columnconfigure(0, weight=1)
    main_frame.rowconfigure(2, weight=1)   # file tree
    main_frame.rowconfigure(4, weight=1)   # log

    # ══════════════════════════════════════════════════════════════
    # Row 0 — Toolbar
    # ══════════════════════════════════════════════════════════════
    toolbar = ttk.Frame(main_frame)
    toolbar.grid(row=0, column=0, sticky='ew', pady=(0, 6))

    # ── File scanning ──
    def _normalize_lang(code):
        """Normalize a subtitle language code to 3-letter ISO 639-2."""
        code = (code or 'und').lower()
        return _LANG_2TO3.get(code, code)

    def _refresh_lang_dropdown():
        """Rebuild the language dropdown from languages found in loaded files.
        Always includes 'All Languages' and 'English' at the top."""
        # Collect unique normalized language codes from all loaded files
        found_codes = set()
        for f in sr_files:
            for s in f.get('sub_streams', []):
                code = _normalize_lang(s.get('language', 'und'))
                if code != 'und':
                    found_codes.add(code)

        # Build display names — known languages get their full name,
        # unknown codes are shown as-is (e.g. "tha" if not in our table)
        lang_names = set()
        for code in found_codes:
            name = _CODE_TO_NAME.get(code)
            if name and name != 'Undetermined':
                lang_names.add(name)
            else:
                # Unknown code — add it with the code as the display name
                lang_names.add(code)
                # Register in lookup so matching still works
                if code not in _NAME_TO_CODE:
                    _NAME_TO_CODE[code] = code

        # Always include English even if not found in files
        lang_names.add('English')

        # Sort alphabetically, but put English first
        sorted_names = sorted(lang_names, key=lambda n: (n != 'English', n.lower()))

        values = [_ALL_LANGUAGES] + sorted_names
        current = opt_language.get()
        lang_combo.configure(values=values)
        # Keep current selection if still valid, otherwise default to English
        if current not in values:
            opt_language.set('English')

    def _count_matching_subs(subs):
        """Count subtitle streams matching the currently selected language."""
        lang_name = opt_language.get()
        if lang_name == _ALL_LANGUAGES:
            return len(subs)
        target = _NAME_TO_CODE.get(lang_name, 'eng')
        return sum(1 for s in subs
                   if _normalize_lang(s.get('language', 'und'))
                   in (target, 'und'))

    def _add_one_file(filepath):
        """Probe a single file and append to sr_files."""
        for f in sr_files:
            if f['path'] == filepath:
                return
        name = os.path.basename(filepath)
        size = os.path.getsize(filepath)
        subs = get_subtitle_streams(filepath)
        has_cc = detect_closed_captions(filepath)
        sr_files.append({
            'path':       filepath,
            'name':       name,
            'size':       format_size(size),
            'size_bytes': size,
            'sub_streams': subs,
            'sub_count':  len(subs),
            'has_cc':     has_cc,
            'status':     'Pending',
        })

    def _add_files_threaded(file_paths, source_label="files"):
        """Probe and add files in a background thread with progress."""
        if _scanning[0] or not file_paths:
            return
        existing = {f['path'] for f in sr_files}
        to_add = [p for p in file_paths if p not in existing]
        if not to_add:
            return
        _scanning[0] = True
        total = len(to_add)

        def _worker():
            start = _time.monotonic()
            added = 0
            for i, fp in enumerate(to_add):
                elapsed = _time.monotonic() - start
                rate = (i + 1) / elapsed if elapsed > 0.1 else 0
                eta = f" — ETA {int((total - i - 1) / rate)}s" if rate > 0 else ""
                pct = ((i + 1) / total) * 100
                win.after(0, lambda p=pct: progress_var.set(p))
                win.after(0, lambda n=i+1, t=total, e=eta:
                          progress_label.configure(
                              text=f"Scanning {n}/{t}{e}"))
                try:
                    _add_one_file(fp)
                    added += 1
                except Exception:
                    pass
                if added > 0 and added % 20 == 0:
                    win.after(0, _rebuild_tree)

            elapsed = _time.monotonic() - start
            win.after(0, _rebuild_tree)
            win.after(0, lambda: progress_var.set(0))
            win.after(0, lambda: progress_label.configure(text="Ready"))
            _log(f"Added {added} {source_label} ({elapsed:.1f}s)", 'INFO')
            _scanning[0] = False

        threading.Thread(target=_worker, daemon=True).start()

    def _add_files():
        paths = ask_open_files(
            parent=win,
            title="Select Video Files",
            filetypes=[
                ("Video files",
                 "*.mkv *.mp4 *.avi *.mov *.wmv *.flv *.webm *.ts *.m2ts *.mts"),
                ("All files", "*.*")])
        if paths:
            _add_files_threaded(list(paths), "file(s)")

    def _add_folder():
        folder = ask_directory(title="Select Folder with Video Files",
                               parent=win)
        if not folder:
            return
        file_paths = []
        for ext in VIDEO_EXTENSIONS:
            for fp in Path(folder).rglob(f'*{ext}'):
                if fp.is_file() and not any(
                        part.startswith('.')
                        for part in fp.relative_to(folder).parts):
                    file_paths.append(str(fp))
        if file_paths:
            _add_files_threaded(file_paths, "video file(s) from folder")

    def _remove_selected():
        sel = tree.selection()
        if not sel or _processing[0]:
            return
        items = tree.get_children()
        indices = sorted([list(items).index(s) for s in sel], reverse=True)
        for idx in indices:
            del sr_files[idx]
        _rebuild_tree()

    def _clear_files():
        sr_files.clear()
        _rebuild_tree()

    ttk.Button(toolbar, text="📂 Add Files...",
               command=_add_files).pack(side='left', padx=2)
    ttk.Button(toolbar, text="📁 Add Folder...",
               command=_add_folder).pack(side='left', padx=2)
    ttk.Button(toolbar, text="🗑️ Remove",
               command=_remove_selected).pack(side='left', padx=2)
    ttk.Button(toolbar, text="✕ Clear",
               command=_clear_files).pack(side='left', padx=2)

    # ══════════════════════════════════════════════════════════════
    # Row 1 — Options panel
    # ══════════════════════════════════════════════════════════════
    options_frame = ttk.Frame(main_frame)
    options_frame.grid(row=1, column=0, sticky='ew', pady=(0, 6))

    # Subtitle Extraction LabelFrame — title updates with language
    lang_frame = ttk.LabelFrame(options_frame, text="English Subtitles",
                                 padding=6)
    lang_frame.pack(side='left', fill='x', expand=True)

    chk_frame = ttk.Frame(lang_frame)
    chk_frame.pack(side='left', fill='x')

    def _on_language_change(*_args):
        """Handle language dropdown change — update LabelFrame title,
        toggle checkboxes for All Languages, and refresh match counts."""
        lang_name = opt_language.get()
        if lang_name == _ALL_LANGUAGES:
            lang_frame.configure(text="All Languages — Subtitles")
            opt_main.set(True)
            opt_forced.set(True)
            opt_sdh.set(True)
            opt_cc.set(True)
            chk_main.configure(state='disabled')
            chk_forced.configure(state='disabled')
            chk_sdh.configure(state='disabled')
            chk_cc.configure(state='disabled')
        else:
            lang_frame.configure(text=f"{lang_name} Subtitles")
            chk_main.configure(state='normal')
            chk_forced.configure(state='normal')
            chk_sdh.configure(state='normal')
            chk_cc.configure(state='normal')
        # Refresh the Match column counts
        _rebuild_tree()

    # Language dropdown
    ttk.Label(chk_frame, text="Language:").pack(side='left', padx=(4, 4))
    lang_combo = ttk.Combobox(chk_frame, textvariable=opt_language,
                               values=[_ALL_LANGUAGES, 'English'],
                               width=14, state='readonly')
    lang_combo.pack(side='left', padx=(0, 12))
    lang_combo.bind('<<ComboboxSelected>>', _on_language_change)

    chk_main = ttk.Checkbutton(chk_frame, text="Main", variable=opt_main)
    chk_main.pack(side='left', padx=4)
    chk_forced = ttk.Checkbutton(chk_frame, text="Forced",
                                  variable=opt_forced)
    chk_forced.pack(side='left', padx=4)
    chk_sdh = ttk.Checkbutton(chk_frame, text="SDH", variable=opt_sdh)
    chk_sdh.pack(side='left', padx=4)
    chk_cc = ttk.Checkbutton(chk_frame, text="CC", variable=opt_cc)
    chk_cc.pack(side='left', padx=4)

    # Output format
    fmt_frame = ttk.Frame(options_frame)
    fmt_frame.pack(side='left', padx=(16, 4))
    ttk.Label(fmt_frame, text="Format:").pack(side='left', padx=(0, 4))
    fmt_combo = ttk.Combobox(fmt_frame, textvariable=opt_format,
                              values=list(_OUTPUT_FORMATS.keys()),
                              width=8, state='readonly')
    fmt_combo.pack(side='left')

    # Overwrite checkbox
    ttk.Checkbutton(options_frame, text="Overwrite existing files",
                    variable=opt_overwrite).pack(side='left', padx=(16, 4))

    # NOTE: _on_language_change() is called after tree is built (below)

    # ══════════════════════════════════════════════════════════════
    # Row 2 — File tree
    # ══════════════════════════════════════════════════════════════
    tree_frame = ttk.LabelFrame(main_frame, text="Files", padding=5)
    tree_frame.grid(row=2, column=0, sticky='nsew', pady=(0, 6))
    tree_frame.columnconfigure(0, weight=1)
    tree_frame.rowconfigure(0, weight=1)

    columns = ('name', 'subs', 'match', 'cc', 'size', 'status')
    tree = ttk.Treeview(tree_frame, columns=columns, show='headings',
                        height=8, selectmode='extended')
    tree.grid(row=0, column=0, sticky='nsew')

    # ── Column sorting state ──
    _sort_col = [None]
    _sort_reverse = [False]
    _col_labels = {
        'name': 'Filename', 'subs': 'Subs', 'match': 'Match',
        'cc': 'CC', 'size': 'Size', 'status': 'Status',
    }

    def _sort_by_column(col):
        if _sort_col[0] == col:
            _sort_reverse[0] = not _sort_reverse[0]
        else:
            _sort_col[0] = col
            _sort_reverse[0] = False

        def sort_key(f):
            if col == 'name':
                return f.get('name', '').lower()
            elif col == 'subs':
                return f.get('sub_count', 0)
            elif col == 'match':
                return _count_matching_subs(f.get('sub_streams', []))
            elif col == 'cc':
                return 1 if f.get('has_cc') else 0
            elif col == 'size':
                return f.get('size_bytes', 0)
            elif col == 'status':
                return f.get('status', '').lower()
            return ''

        sr_files.sort(key=sort_key, reverse=_sort_reverse[0])
        _rebuild_tree()

        arrow = ' ▼' if _sort_reverse[0] else ' ▲'
        for c, lbl in _col_labels.items():
            indicator = arrow if c == col else ''
            tree.heading(c, text=lbl + indicator)

    tree.heading('name',    text='Filename',
                 command=lambda: _sort_by_column('name'))
    tree.heading('subs',    text='Subs',
                 command=lambda: _sort_by_column('subs'))
    tree.heading('match',   text='Match',
                 command=lambda: _sort_by_column('match'))
    tree.heading('cc',      text='CC',
                 command=lambda: _sort_by_column('cc'))
    tree.heading('size',    text='Size',
                 command=lambda: _sort_by_column('size'))
    tree.heading('status',  text='Status',
                 command=lambda: _sort_by_column('status'))

    tree.column('name',    width=330, minwidth=200)
    tree.column('subs',    width=50,  minwidth=40,  anchor='center')
    tree.column('match',   width=60,  minwidth=45,  anchor='center')
    tree.column('cc',      width=40,  minwidth=30,  anchor='center')
    tree.column('size',    width=80,  minwidth=60,  anchor='e')
    tree.column('status',  width=140, minwidth=80,  anchor='center')

    scrollbar = ttk.Scrollbar(tree_frame, orient='vertical',
                               command=tree.yview)
    scrollbar.grid(row=0, column=1, sticky='ns')
    tree.configure(yscrollcommand=scrollbar.set)

    def _rebuild_tree():
        _refresh_lang_dropdown()
        tree.delete(*tree.get_children())
        for f in sr_files:
            cc_label = 'CC' if f.get('has_cc') else ''
            match_count = _count_matching_subs(f.get('sub_streams', []))
            tree.insert('', 'end', values=(
                f['name'], f['sub_count'], match_count,
                cc_label, f['size'], f['status'],
            ))

    # Apply initial language state (deferred until tree exists)
    _on_language_change()

    def _update_tree_status(index, status):
        items = tree.get_children()
        if index < len(items):
            sr_files[index]['status'] = status
            tree.set(items[index], 'status', status)

    # ── Delete key ──
    def _on_delete(evt):
        _remove_selected()
    tree.bind('<Delete>', _on_delete)

    # ── Shift+Arrow multi-select ──
    def _shift_arrow(evt, direction):
        items = tree.get_children()
        if not items:
            return 'break'
        focus = tree.focus()
        if not focus:
            return 'break'
        idx = list(items).index(focus)
        new_idx = idx + direction
        if new_idx < 0 or new_idx >= len(items):
            return 'break'
        new_item = items[new_idx]
        tree.focus(new_item)
        tree.see(new_item)
        tree.selection_add(new_item)
        return 'break'

    tree.bind('<Shift-Up>',   lambda e: _shift_arrow(e, -1))
    tree.bind('<Shift-Down>', lambda e: _shift_arrow(e, 1))

    # ── Right-click context menu ──
    def _show_context_menu(event):
        item = tree.identify_row(event.y)
        if not item or _processing[0]:
            return
        tree.selection_set(item)
        items = tree.get_children()
        index = list(items).index(item)

        ctx = tk.Menu(win, tearoff=0)
        ctx.add_command(label="🗑️ Remove Selected",
                        command=_remove_selected)
        ctx.add_command(label="Media Details...",
                        command=lambda: _show_media_details(index))
        ctx.tk_popup(event.x_root, event.y_root)

    tree.bind('<Button-3>', _show_context_menu)

    def _show_media_details(index):
        """Open the Enhanced Media Details dialog for the selected file."""
        filepath = sr_files[index]['path']
        try:
            from .media_info import show_enhanced_media_info
            show_enhanced_media_info(app, filepath, parent=win)
        except ImportError:
            try:
                import importlib.util
                _mi_path = os.path.join(
                    os.path.dirname(os.path.abspath(__file__)),
                    'media_info.py')
                if os.path.exists(_mi_path):
                    spec = importlib.util.spec_from_file_location(
                        'media_info', _mi_path)
                    mod = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(mod)
                    mod.show_enhanced_media_info(app, filepath, parent=win)
                else:
                    messagebox.showerror("Media Details",
                                         "modules/media_info.py not found.")
            except Exception as e:
                messagebox.showerror("Media Details",
                                     f"Could not open Media Details:\n{e}")

    # ══════════════════════════════════════════════════════════════
    # Row 3 — Progress bar + action buttons
    # ══════════════════════════════════════════════════════════════
    progress_frame = ttk.Frame(main_frame)
    progress_frame.grid(row=3, column=0, sticky='ew', pady=(0, 6))

    progress_var = tk.DoubleVar(value=0)

    # Action buttons — pack right first so they're always visible
    btn_frame = ttk.Frame(progress_frame)
    btn_frame.pack(side='right', padx=(8, 0))
    extract_btn = ttk.Button(btn_frame, text="▶ Extract All",
                              command=lambda: _start_extraction())
    extract_btn.pack(side='left', padx=2)
    stop_btn = ttk.Button(btn_frame, text="⏹ Stop",
                           command=lambda: _stop_extraction(),
                           state='disabled')
    stop_btn.pack(side='left', padx=2)

    # Label and progress bar fill remaining space
    progress_label = ttk.Label(progress_frame, text="Ready")
    progress_label.pack(side='left', padx=(0, 8))
    progress_bar = ttk.Progressbar(progress_frame, variable=progress_var,
                                    maximum=100, mode='determinate')
    progress_bar.pack(side='left', fill='x', expand=True)

    # ══════════════════════════════════════════════════════════════
    # Row 4 — Log panel
    # ══════════════════════════════════════════════════════════════
    log_frame = ttk.LabelFrame(main_frame, text="Log", padding=5)
    log_frame.grid(row=4, column=0, sticky='nsew')
    log_frame.columnconfigure(0, weight=1)
    log_frame.rowconfigure(0, weight=1)

    log_text = tk.Text(log_frame, height=8, wrap='word',
                       font=('Courier', 9), state='disabled',
                       bg='#1e1e1e', fg='#d4d4d4')
    log_text.grid(row=0, column=0, sticky='nsew')
    log_scroll = ttk.Scrollbar(log_frame, orient='vertical',
                                command=log_text.yview)
    log_scroll.grid(row=0, column=1, sticky='ns')
    log_text.configure(yscrollcommand=log_scroll.set)

    def _clear_log():
        log_text.configure(state='normal')
        log_text.delete('1.0', 'end')
        log_text.configure(state='disabled')

    ttk.Button(log_frame, text="Clear Log",
               command=_clear_log).grid(row=1, column=0, sticky='w',
                                         pady=(4, 0))

    # Log color tags
    log_text.tag_configure('INFO',    foreground='#d4d4d4')
    log_text.tag_configure('SUCCESS', foreground='#4ec9b0')
    log_text.tag_configure('WARNING', foreground='#dcdcaa')
    log_text.tag_configure('ERROR',   foreground='#f44747')
    log_text.tag_configure('SKIP',    foreground='#569cd6')
    log_text.tag_configure('FILENAME', foreground='#6aff6a')

    def _log(msg, level='INFO', filename=None):
        def _do():
            log_text.configure(state='normal')
            ts = datetime.now().strftime('%H:%M:%S')
            if filename:
                prefix = f"[{ts}] [{level}] "
                idx = msg.find(filename)
                if idx >= 0:
                    before = msg[:idx]
                    after = msg[idx + len(filename):]
                    log_text.insert('end', prefix + before, level)
                    log_text.insert('end', filename, 'FILENAME')
                    log_text.insert('end', after + '\n', level)
                else:
                    log_text.insert('end', f"{prefix}{msg}\n", level)
            else:
                log_text.insert('end',
                                f"[{ts}] [{level}] {msg}\n", level)
            log_text.see('end')
            log_text.configure(state='disabled')
        win.after(0, _do)

    # ══════════════════════════════════════════════════════════════
    # Extraction logic
    # ══════════════════════════════════════════════════════════════

    def _matches_language(stream):
        """Check if a subtitle stream matches the selected language.
        'All Languages' matches everything.  A specific language matches
        its 3-letter code and treats 'und' (undetermined) as a match."""
        lang_name = opt_language.get()
        if lang_name == _ALL_LANGUAGES:
            return True
        target = _NAME_TO_CODE.get(lang_name, 'eng')
        stream_lang = _normalize_lang(stream.get('language', 'und'))
        return stream_lang in (target, 'und')

    def _classify_stream(stream):
        """Return 'forced', 'sdh', or 'main' for a subtitle stream."""
        if stream.get('forced'):
            return 'forced'
        if stream.get('sdh'):
            return 'sdh'
        # Also detect SDH from track title
        title = (stream.get('title', '') or '').lower()
        if any(kw in title for kw in ('sdh', 'hearing', 'cc', ' hi')):
            return 'sdh'
        if 'forced' in title:
            return 'forced'
        return 'main'

    def _get_matching_streams(file_dict):
        """Return list of (stream, classification) tuples matching
        the current language and checkbox selections."""
        all_langs   = opt_language.get() == _ALL_LANGUAGES
        want_main   = opt_main.get()
        want_forced = opt_forced.get()
        want_sdh    = opt_sdh.get()

        matches = []
        for stream in file_dict['sub_streams']:
            if not _matches_language(stream):
                continue
            cls = _classify_stream(stream)
            if all_langs:
                # All Languages — include everything
                matches.append((stream, cls))
            elif cls == 'main' and want_main:
                matches.append((stream, cls))
            elif cls == 'forced' and want_forced:
                matches.append((stream, cls))
            elif cls == 'sdh' and want_sdh:
                matches.append((stream, cls))
        return matches

    def _get_output_ext(stream):
        """Return the file extension for the given stream based on
        the selected output format.  For 'Original' mode, derive the
        extension from the stream's codec; for explicit formats use
        the fixed extension."""
        fmt = _OUTPUT_FORMATS[opt_format.get()]
        if fmt['ext'] is not None:
            return fmt['ext']
        # Original mode — look up codec → extension
        codec = stream.get('codec_name', '').lower()
        return _CODEC_TO_EXT.get(codec, '.srt')

    def _get_output_codec(stream):
        """Return the ffmpeg subtitle codec flag for the given stream.
        For 'Original' mode returns 'copy' (stream copy); for explicit
        formats returns the target codec name."""
        fmt = _OUTPUT_FORMATS[opt_format.get()]
        codec_out = fmt['codec']
        if codec_out != 'copy':
            return codec_out
        # Original mode — stream copy, but mov_text can't exist
        # outside MP4 so convert to SRT instead of copying
        src_codec = stream.get('codec_name', '').lower()
        if src_codec in ('mov_text', 'text'):
            return 'srt'
        return 'copy'

    def _build_output_path(video_path, stream, cls, main_count,
                           main_index):
        """Build the output file path for an extracted subtitle."""
        ext = _get_output_ext(stream)
        stem = Path(video_path).stem
        parent = Path(video_path).parent
        # Use the stream's actual language for the filename tag
        lang = _normalize_lang(stream.get('language', 'und'))
        if lang == 'und':
            # Fall back to the selected language, or 'und' if All
            sel = opt_language.get()
            if sel != _ALL_LANGUAGES:
                lang = _NAME_TO_CODE.get(sel, 'und')

        if cls == 'forced':
            tag = f".{lang}.forced"
        elif cls == 'sdh':
            tag = f".{lang}.sdh"
        else:
            # Main stream — append stream index if multiple mains
            if main_count > 1:
                tag = f".{lang}.{main_index}"
            else:
                tag = f".{lang}"

        return str(parent / f"{stem}{tag}{ext}")

    def _start_extraction():
        if _processing[0] or not sr_files:
            if not sr_files:
                messagebox.showinfo("Sub Extractor", "No files loaded.")
            return

        # Validate at least one type selected
        all_langs = opt_language.get() == _ALL_LANGUAGES
        if not (all_langs or opt_main.get()
                or opt_forced.get() or opt_sdh.get()
                or opt_cc.get()):
            messagebox.showwarning(
                "Sub Extractor",
                "Select at least one subtitle type to extract.")
            return

        _processing[0] = True
        _stop[0] = False
        extract_btn.configure(state='disabled')
        stop_btn.configure(state='normal')

        _log("═" * 50, 'INFO')
        _log("Starting subtitle extraction", 'INFO')
        _log("═" * 50, 'INFO')

        threading.Thread(target=_extract_worker, daemon=True).start()

    def _stop_extraction():
        _stop[0] = True

    def _extract_worker():
        """Background thread: extract subtitles from all files."""
        overwrite = opt_overwrite.get()

        all_langs = opt_language.get() == _ALL_LANGUAGES
        want_cc = opt_cc.get() or all_langs
        total_files = len(sr_files)
        extracted_total = 0
        skipped_total = 0
        error_total = 0
        bitmap_total = 0
        cc_total = 0

        for fi, fdata in enumerate(sr_files):
            if _stop[0]:
                _log("Extraction stopped by user", 'WARNING')
                break

            filepath = fdata['path']
            name = fdata['name']
            has_cc = fdata.get('has_cc', False)

            matches = _get_matching_streams(fdata)
            # Count total jobs: subtitle streams + CC (if applicable)
            cc_job = 1 if (want_cc and has_cc) else 0
            total_jobs = len(matches) + cc_job

            if total_jobs == 0:
                win.after(0, lambda i=fi: _update_tree_status(i, 'No match'))
                lang_desc = opt_language.get()
                _log(f"{name}: no matching {lang_desc} subtitle streams",
                     'SKIP', filename=name)
                continue

            win.after(0, lambda i=fi: _update_tree_status(
                i, f'Extracting 0/{total_jobs}'))

            # Count main streams for disambiguation
            main_streams = [(s, c) for s, c in matches if c == 'main']
            main_count = len(main_streams)
            main_idx = 0

            file_extracted = 0
            file_skipped = 0
            file_errors = 0
            job_num = 0

            # ── Extract subtitle streams ──
            for si, (stream, cls) in enumerate(matches):
                if _stop[0]:
                    break

                stream_index = stream['index']
                codec_name = stream.get('codec_name', 'unknown')

                # Skip bitmap codecs
                if codec_name in BITMAP_SUB_CODECS:
                    _log(f"  {name}: stream #{stream_index} is bitmap "
                         f"({codec_name}) — cannot extract to text",
                         'WARNING', filename=name)
                    bitmap_total += 1
                    file_skipped += 1
                    job_num += 1
                    continue

                # Build output path
                if cls == 'main':
                    main_idx += 1
                    out_path = _build_output_path(
                        filepath, stream, cls, main_count, main_idx)
                else:
                    out_path = _build_output_path(
                        filepath, stream, cls, 0, 0)

                # Check overwrite
                if not overwrite and os.path.exists(out_path):
                    _log(f"  {name}: {os.path.basename(out_path)} exists "
                         f"— skipping", 'SKIP', filename=name)
                    file_skipped += 1
                    skipped_total += 1
                    job_num += 1
                    continue

                # Run ffmpeg — per-stream codec (copy or convert)
                stream_codec = _get_output_codec(stream)
                cmd = [
                    'ffmpeg', '-y', '-i', filepath,
                    '-map', f'0:{stream_index}',
                    '-c:s', stream_codec,
                    out_path,
                ]

                try:
                    proc = subprocess.run(
                        cmd,
                        capture_output=True, text=True, timeout=120)
                    if proc.returncode == 0:
                        out_name = os.path.basename(out_path)
                        _log(f"  {name} → {out_name}", 'SUCCESS',
                             filename=name)
                        file_extracted += 1
                        extracted_total += 1
                    else:
                        stderr = proc.stderr.strip().split('\n')[-1] \
                            if proc.stderr else 'Unknown error'
                        _log(f"  {name}: stream #{stream_index} failed — "
                             f"{stderr}", 'ERROR', filename=name)
                        file_errors += 1
                        error_total += 1
                except subprocess.TimeoutExpired:
                    _log(f"  {name}: stream #{stream_index} timed out "
                         f"(120s)", 'ERROR', filename=name)
                    file_errors += 1
                    error_total += 1
                except Exception as e:
                    _log(f"  {name}: stream #{stream_index} error — {e}",
                         'ERROR', filename=name)
                    file_errors += 1
                    error_total += 1

                job_num += 1
                # Update progress for this file
                win.after(0, lambda i=fi, s=job_num, t=total_jobs:
                          _update_tree_status(i, f'Extracting {s}/{t}'))

            # ── Extract closed captions ──
            if want_cc and has_cc and not _stop[0]:
                fmt_info = _OUTPUT_FORMATS[opt_format.get()]
                # CC is natively SRT — use SRT for Original mode too
                cc_ext = fmt_info['ext'] if fmt_info['ext'] else '.srt'
                cc_codec = fmt_info['codec'] if fmt_info['codec'] != 'copy' else 'srt'
                stem = Path(filepath).stem
                parent = Path(filepath).parent
                cc_out_path = str(parent / f"{stem}.eng.cc{cc_ext}")

                if not overwrite and os.path.exists(cc_out_path):
                    _log(f"  {name}: {os.path.basename(cc_out_path)} exists "
                         f"— skipping CC", 'SKIP', filename=name)
                    file_skipped += 1
                    skipped_total += 1
                else:
                    win.after(0, lambda i=fi, t=total_jobs:
                              _update_tree_status(
                                  i, f'Extracting CC...'))
                    # CC extraction always produces SRT first, then
                    # convert if a different format is requested
                    if cc_ext == '.srt':
                        cc_ok = extract_closed_captions_to_srt(
                            filepath, cc_out_path)
                    else:
                        # Extract to temp SRT, then convert
                        import tempfile
                        tmp = tempfile.NamedTemporaryFile(
                            suffix='.srt', delete=False)
                        tmp.close()
                        cc_ok = extract_closed_captions_to_srt(
                            filepath, tmp.name)
                        if cc_ok:
                            try:
                                conv_cmd = [
                                    'ffmpeg', '-y', '-i', tmp.name,
                                    '-c:s', cc_codec, cc_out_path]
                                proc = subprocess.run(
                                    conv_cmd, capture_output=True,
                                    text=True, timeout=30)
                                cc_ok = proc.returncode == 0
                            except Exception:
                                cc_ok = False
                        try:
                            os.unlink(tmp.name)
                        except OSError:
                            pass

                    if cc_ok:
                        cc_name = os.path.basename(cc_out_path)
                        _log(f"  {name} → {cc_name} (CC)",
                             'SUCCESS', filename=name)
                        file_extracted += 1
                        extracted_total += 1
                        cc_total += 1
                    else:
                        _log(f"  {name}: CC extraction failed "
                             f"(no CC data or extraction error)",
                             'ERROR', filename=name)
                        file_errors += 1
                        error_total += 1

                job_num += 1
                win.after(0, lambda i=fi, s=job_num, t=total_jobs:
                          _update_tree_status(i, f'Extracting {s}/{t}'))

            # Final status for this file
            if _stop[0]:
                status = 'Stopped'
            elif file_errors > 0:
                status = f'Done ({file_extracted} ok, {file_errors} err)'
            elif file_skipped > 0 and file_extracted == 0:
                status = 'Skipped'
            elif file_extracted > 0:
                status = f'Done ({file_extracted})'
            else:
                status = 'No match'
            win.after(0, lambda i=fi, s=status:
                      _update_tree_status(i, s))

            # Overall progress
            pct = ((fi + 1) / total_files) * 100
            win.after(0, lambda p=pct: progress_var.set(p))
            win.after(0, lambda n=fi+1, t=total_files:
                      progress_label.configure(
                          text=f"File {n}/{t}"))

        # ── Summary ──
        _log("═" * 50, 'INFO')
        parts = [f"{extracted_total} extracted"]
        if cc_total:
            parts.append(f"{cc_total} CC")
        if skipped_total:
            parts.append(f"{skipped_total} skipped")
        if bitmap_total:
            parts.append(f"{bitmap_total} bitmap (unsupported)")
        if error_total:
            parts.append(f"{error_total} errors")
        _log(f"Complete — {', '.join(parts)}", 'SUCCESS')
        _log("═" * 50, 'INFO')

        win.after(0, lambda: progress_label.configure(text="Ready"))
        win.after(0, lambda: progress_var.set(0))
        win.after(0, lambda: extract_btn.configure(state='normal'))
        win.after(0, lambda: stop_btn.configure(state='disabled'))

        _processing[0] = False

        # Completion sound
        _play_done_sound()

    # ══════════════════════════════════════════════════════════════
    # Drag and drop support
    # ══════════════════════════════════════════════════════════════
    try:
        win.drop_target_register('DND_Files')

        def _on_drop(event):
            raw = event.data.strip()
            paths = []
            if 'file://' in raw:
                from urllib.parse import unquote, urlparse
                for token in re.split(r'[\r\n\s]+', raw):
                    token = token.strip()
                    if token.startswith('file://'):
                        parsed = urlparse(token)
                        paths.append(unquote(parsed.path))
            else:
                paths = [p.strip('{}')
                         for p in re.findall(r'\{[^}]+\}|[^\s]+', raw)]

            file_paths = []
            for p in paths:
                if (os.path.isfile(p)
                        and os.path.splitext(p)[1].lower()
                        in VIDEO_EXTENSIONS):
                    file_paths.append(p)
                elif os.path.isdir(p):
                    for ext in VIDEO_EXTENSIONS:
                        for fp in Path(p).rglob(f'*{ext}'):
                            if fp.is_file() and not any(
                                    part.startswith('.')
                                    for part in fp.relative_to(p).parts):
                                file_paths.append(str(fp))
            if file_paths:
                _add_files_threaded(file_paths,
                                    "file(s) via drag-and-drop")

        win.dnd_bind('<<Drop>>', _on_drop)
    except Exception:
        pass  # tkinterdnd2 not available

    # ══════════════════════════════════════════════════════════════
    # Save / Close
    # ══════════════════════════════════════════════════════════════
    def _save_prefs():
        """Save current Sub Extractor settings to preferences."""
        sr_prefs = {
            'language':    opt_language.get(),
            'main':        opt_main.get(),
            'forced':      opt_forced.get(),
            'sdh':         opt_sdh.get(),
            'cc':          opt_cc.get(),
            'format':      opt_format.get(),
            'overwrite':   opt_overwrite.get(),
        }
        try:
            prefs_path = getattr(app, '_prefs_path', None)
            if prefs_path:
                if isinstance(prefs_path, str):
                    p = Path(prefs_path)
                else:
                    p = prefs_path() if callable(prefs_path) else Path(
                        str(prefs_path))
                if p.exists():
                    prefs = json.loads(p.read_text())
                else:
                    prefs = {}
                prefs['sub_ripper'] = sr_prefs
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(json.dumps(prefs, indent=2))
        except Exception:
            pass

    close_frame = ttk.Frame(main_frame)
    close_frame.grid(row=5, column=0, sticky='e', pady=(6, 0))

    def _close_window():
        _save_prefs()
        win.destroy()
        if getattr(app, '_standalone_mode', False):
            app.root.destroy()

    ttk.Button(close_frame, text="Close",
               command=_close_window).pack(side='right')
    win.protocol('WM_DELETE_WINDOW', _close_window)

    # Force Tk to render all widgets
    win.update_idletasks()

    _log("Docflix Sub Extractor ready — add video files and select "
         "subtitle types to extract", 'INFO')
    _log("Supports embedded subtitle streams and closed captions (CC)",
         'INFO')
    _log("Tip: drag and drop video files onto this window", 'INFO')


# ═══════════════════════════════════════════════════════════════════
# Standalone entry point
# ═══════════════════════════════════════════════════════════════════

def main():
    """Launch Sub Extractor as a standalone application."""
    from .standalone import create_standalone_root

    root, app = create_standalone_root(
        title="Docflix Sub Extractor",
        geometry="920x720",
        minsize=(750, 520),
    )

    app._standalone_mode = True
    root.withdraw()
    open_sub_ripper(app)

    root.mainloop()


if __name__ == '__main__':
    main()
