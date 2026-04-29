"""
Docflix Video Converter — Media Processor

Standalone remux-only post-processing tool for already-encoded
files. Supports audio conversion, metadata cleanup, subtitle
muxing, and parallel processing — all without re-encoding.
"""

from datetime import datetime
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from .constants import VIDEO_EXTENSIONS, EDITION_PRESETS
from .chapters import generate_auto_chapters, chapters_to_ffmetadata
from .utils import get_audio_info, get_subtitle_streams, ask_directory

try:
    from tkinterdnd2 import DND_FILES
    HAS_DND = True
except ImportError:
    HAS_DND = False


def open_media_processor(app):
        import time as _time
        import tempfile
        import threading
        from concurrent.futures import ThreadPoolExecutor, as_completed

        win = tk.Toplevel(app.root)
        win.title("🔧 Media Processor")
        win.geometry("920x1080")
        win.minsize(750, 850)
        app._center_on_main(win)

        # ── State ──
        mp_files = []        # list of dicts
        mp_processing = [False]
        mp_stop = [False]
        mp_lock = threading.Lock()  # protects mp_files during parallel access

        # ── Options ──
        # Map display names → ffmpeg codec names (subset for remux use)
        mp_audio_codec_map = {
            'aac': 'aac',
            'ac3 (Dolby Digital)': 'ac3',
            'eac3 (Dolby Digital+)': 'eac3',
            'mp3': 'mp3',
            'opus': 'opus',
            'flac': 'flac',
            'copy (no re-encode)': 'copy',
        }
        mp_audio_codec_reverse = {v: k for k, v in mp_audio_codec_map.items()}

        # Load saved Media Processor preferences (empty dict on fresh install)
        _mp = getattr(app, '_media_proc_prefs', {})

        opt_convert_audio  = tk.BooleanVar(value=_mp.get('convert_audio', False))
        opt_audio_codec    = tk.StringVar(value=_mp.get('audio_codec', 'ac3 (Dolby Digital)'))
        opt_audio_bitrate  = tk.StringVar(value=_mp.get('audio_bitrate', '384k'))
        opt_strip_chapters = tk.BooleanVar(value=_mp.get('strip_chapters', False))
        opt_strip_tags     = tk.BooleanVar(value=_mp.get('strip_tags', False))
        opt_strip_subs     = tk.BooleanVar(value=_mp.get('strip_subs', False))
        opt_set_metadata   = tk.BooleanVar(value=_mp.get('set_metadata', False))
        opt_meta_video     = tk.StringVar(value=_mp.get('meta_video', 'und'))
        opt_meta_audio     = tk.StringVar(value=_mp.get('meta_audio', 'eng'))
        opt_meta_sub       = tk.StringVar(value=_mp.get('meta_sub', 'eng'))
        opt_mux_subs       = tk.BooleanVar(value=_mp.get('mux_subs', False))
        opt_sub_lang       = tk.StringVar(value=_mp.get('sub_lang', 'eng'))
        opt_output_mode    = tk.StringVar(value=_mp.get('output_mode', 'inplace'))
        opt_output_folder  = tk.StringVar(value=_mp.get('output_folder', ''))
        opt_container      = tk.StringVar(value=_mp.get('container', '.mkv'))
        opt_parallel       = tk.BooleanVar(value=_mp.get('parallel', False))
        try:
            _cpu_count = os.cpu_count() or 4
        except Exception:
            _cpu_count = 4
        opt_max_jobs       = tk.IntVar(value=_mp.get('max_jobs', min(_cpu_count, 8)))
        opt_edition_tag    = tk.StringVar(value=_mp.get('edition_tag', ''))
        opt_edition_fn     = tk.BooleanVar(value=_mp.get('edition_in_filename', False))
        _edition_custom_sv = tk.StringVar(value='')
        opt_add_chapters   = tk.BooleanVar(value=_mp.get('add_chapters', False))
        opt_ch_interval    = tk.IntVar(value=_mp.get('chapter_interval', 5))

        # ── Layout ──
        main_frame = ttk.Frame(win, padding=10)
        main_frame.pack(fill='both', expand=True)
        main_frame.columnconfigure(0, weight=1)
        main_frame.rowconfigure(1, weight=1)   # file list
        main_frame.rowconfigure(4, weight=1)   # log

        # ── Toolbar ──
        toolbar = ttk.Frame(main_frame)
        toolbar.grid(row=0, column=0, sticky='ew', pady=(0, 6))

        def _add_files():
            paths = filedialog.askopenfilenames(
                parent=win,
                title="Select Video Files",
                filetypes=[("Video files", "*.mkv *.mp4 *.avi *.mov *.wmv *.flv *.webm *.ts *.m2ts *.mts"),
                           ("All files", "*.*")])
            for p in paths:
                _add_one_file(p)
            _rebuild_tree()

        def _add_folder():
            folder = ask_directory(title="Select Folder with Video Files", parent=win)
            if not folder:
                return
            count = 0
            for ext in VIDEO_EXTENSIONS:
                for fp in Path(folder).glob(f'*{ext}'):
                    if fp.is_file() and not fp.name.startswith('.'):
                        _add_one_file(str(fp))
                        count += 1
            _rebuild_tree()
            _log(f"Scanned folder: {count} video file(s) found", 'INFO')

        def _detect_ext_subs(filepath):
            """Detect matching subtitle files alongside a video file.
            Uses the configured subtitle language code."""
            base = os.path.splitext(filepath)[0]
            lang = opt_sub_lang.get().strip() or 'eng'
            ext_subs_found = []
            # Try language-specific patterns first
            main_srt = f"{base}.{lang}.srt"
            forced_srt = f"{base}.{lang}.forced.srt"
            # Fallback: bare .srt (no language code)
            bare_srt = f"{base}.srt"
            if os.path.isfile(main_srt):
                ext_subs_found.append(('main', main_srt))
            elif os.path.isfile(bare_srt):
                ext_subs_found.append(('main', bare_srt))
            if os.path.isfile(forced_srt):
                ext_subs_found.append(('forced', forced_srt))
            return ext_subs_found

        def _add_one_file(filepath):
            # Skip duplicates
            for f in mp_files:
                if f['path'] == filepath:
                    return
            name = os.path.basename(filepath)
            size = os.path.getsize(filepath)
            audio = get_audio_info(filepath)
            subs = get_subtitle_streams(filepath)
            ext_subs_found = _detect_ext_subs(filepath)
            # Audio codec display
            if audio:
                acodec = audio[0]['codec_name'].upper()
                if acodec in ('AC3', 'EAC3'):
                    acodec += ' ✓'
            else:
                acodec = '—'
            mp_files.append({
                'path': filepath,
                'name': name,
                'size': size,
                'audio_info': audio,
                'audio_display': acodec,
                'sub_count': len(subs),
                'ext_subs': ext_subs_found,
                'status': 'Ready',
                'overrides': {},  # per-file operation overrides
            })

        def _clear_files():
            mp_files.clear()
            _rebuild_tree()

        def _rescan_subs():
            """Re-detect external subtitle files for all files using current language setting."""
            for f in mp_files:
                f['ext_subs'] = _detect_ext_subs(f['path'])
            _rebuild_tree()
            _log(f"Re-scanned subtitles with language code: {opt_sub_lang.get()}", 'INFO')

        ttk.Button(toolbar, text="📂 Add Files...", command=_add_files).pack(side='left', padx=2)
        ttk.Button(toolbar, text="📁 Add Folder...", command=_add_folder).pack(side='left', padx=2)
        ttk.Button(toolbar, text="🗑️ Clear", command=_clear_files).pack(side='left', padx=2)

        # ── File list (Treeview) ──
        tree_frame = ttk.LabelFrame(main_frame, text="Files", padding=5)
        tree_frame.grid(row=1, column=0, sticky='nsew', pady=(0, 6))
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)

        columns = ('name', 'audio', 'subs', 'ext_subs', 'size', 'status')
        tree = ttk.Treeview(tree_frame, columns=columns, show='headings', height=6)
        tree.grid(row=0, column=0, sticky='nsew')

        tree.heading('name',     text='Filename')
        tree.heading('audio',    text='Audio')
        tree.heading('subs',     text='Int Subs')
        tree.heading('ext_subs', text='Ext Subs')
        tree.heading('size',     text='Size')
        tree.heading('status',   text='Status')

        tree.column('name',     width=280, minwidth=150)
        tree.column('audio',    width=80,  minwidth=60,  anchor='center')
        tree.column('subs',     width=60,  minwidth=40,  anchor='center')
        tree.column('ext_subs', width=80,  minwidth=60,  anchor='center')
        tree.column('size',     width=80,  minwidth=60,  anchor='e')
        tree.column('status',   width=120, minwidth=80,  anchor='center')

        scrollbar = ttk.Scrollbar(tree_frame, orient='vertical', command=tree.yview)
        scrollbar.grid(row=0, column=1, sticky='ns')
        tree.configure(yscrollcommand=scrollbar.set)

        def _rebuild_tree():
            tree.delete(*tree.get_children())
            for f in mp_files:
                size_mb = f'{f["size"] / (1024*1024):.1f} MB'
                ext_str = ', '.join(t for t, _ in f['ext_subs']) if f['ext_subs'] else '—'
                name_display = f['name']
                if f.get('overrides'):
                    name_display = '⚙️ ' + name_display
                tree.insert('', 'end', values=(
                    name_display, f['audio_display'], f['sub_count'],
                    ext_str, size_mb, f['status']
                ))

        def _update_tree_status(index, status):
            items = tree.get_children()
            if index < len(items):
                mp_files[index]['status'] = status
                tree.set(items[index], 'status', status)

        def _update_tree_row(index):
            """Refresh a single row's display data (after re-probe)."""
            items = tree.get_children()
            if index < len(items):
                f = mp_files[index]
                size_mb = f'{f["size"] / (1024*1024):.1f} MB'
                ext_str = ', '.join(t for t, _ in f['ext_subs']) if f['ext_subs'] else '—'
                name_display = f['name']
                if f.get('overrides'):
                    name_display = '⚙️ ' + name_display
                tree.set(items[index], 'name', name_display)
                tree.set(items[index], 'audio', f['audio_display'])
                tree.set(items[index], 'subs', f['sub_count'])
                tree.set(items[index], 'ext_subs', ext_str)
                tree.set(items[index], 'size', size_mb)

        # ── Delete key to remove selected ──
        def _on_delete(evt):
            sel = tree.selection()
            if not sel or mp_processing[0]:
                return
            items = tree.get_children()
            indices = sorted([list(items).index(s) for s in sel], reverse=True)
            for idx in indices:
                del mp_files[idx]
            _rebuild_tree()
        tree.bind('<Delete>', _on_delete)

        # ── Right-click context menu (per-file overrides + subtitle management) ──
        def _show_context_menu(event):
            item = tree.identify_row(event.y)
            if not item or mp_processing[0]:
                return
            tree.selection_set(item)
            items = tree.get_children()
            index = list(items).index(item)

            ctx = tk.Menu(win, tearoff=0)
            ctx.add_command(label="⚙️ Override Settings...",
                           command=lambda: _show_file_override(index))
            ctx.add_command(label="📎 Manage Subtitles...",
                           command=lambda: _show_sub_manager(index))
            ctx.add_separator()
            ctx.add_command(label="🔄 Re-probe File",
                           command=lambda: _reprobe_file(index))
            ctx.add_command(label="❌ Clear Override",
                           command=lambda: _clear_override(index))
            ctx.add_separator()
            ctx.add_command(label="🗑️ Remove from List",
                           command=lambda: _remove_file(index))
            ctx.tk_popup(event.x_root, event.y_root)

        tree.bind('<Button-3>', _show_context_menu)

        def _remove_file(index):
            del mp_files[index]
            _rebuild_tree()

        def _clear_override(index):
            mp_files[index]['overrides'] = {}
            _rebuild_tree()
            _log(f"Cleared overrides for: {mp_files[index]['name']}", 'INFO')

        def _reprobe_file(index):
            """Re-probe a file's audio and subtitle info and update the tree."""
            f = mp_files[index]
            audio = get_audio_info(f['path'])
            subs = get_subtitle_streams(f['path'])
            f['audio_info'] = audio
            f['sub_count'] = len(subs)
            f['size'] = os.path.getsize(f['path']) if os.path.exists(f['path']) else 0
            f['ext_subs'] = _detect_ext_subs(f['path'])
            if audio:
                acodec = audio[0]['codec_name'].upper()
                if acodec in ('AC3', 'EAC3'):
                    acodec += ' ✓'
                f['audio_display'] = acodec
            else:
                f['audio_display'] = '—'
            win.after(0, lambda: _update_tree_row(index))
            _log(f"Re-probed: {f['name']} — audio={f['audio_display']}, subs={f['sub_count']}", 'INFO')

        def _show_file_override(index):
            """Show per-file override dialog."""
            f = mp_files[index]
            ov = f.get('overrides', {})

            dlg = tk.Toplevel(win)
            dlg.title(f"Override — {f['name']}")
            dlg.geometry("480x400")
            dlg.transient(win)
            dlg.grab_set()
            dlg.resizable(False, False)
            app._center_on_main(dlg)

            fr = ttk.Frame(dlg, padding=12)
            fr.pack(fill='both', expand=True)
            pad = {'padx': 8, 'pady': 4}
            row = 0

            # ── Convert audio ──
            v_conv_audio = tk.BooleanVar(value=ov.get('convert_audio', opt_convert_audio.get()))
            ttk.Checkbutton(fr, text="Convert audio:", variable=v_conv_audio).grid(
                row=row, column=0, sticky='w', **pad)
            v_acodec = tk.StringVar(value=ov.get('audio_codec_display',
                mp_audio_codec_reverse.get(ov.get('audio_codec', ''), mp_ac_combo.get())))
            acodec_combo = ttk.Combobox(fr, textvariable=v_acodec,
                values=list(mp_audio_codec_map.keys()), width=18, state='readonly')
            acodec_combo.grid(row=row, column=1, sticky='w', **pad)
            row += 1
            ttk.Label(fr, text="Audio bitrate:").grid(row=row, column=0, sticky='w', **pad)
            v_abitrate = tk.StringVar(value=ov.get('audio_bitrate', opt_audio_bitrate.get()))
            ttk.Combobox(fr, textvariable=v_abitrate,
                values=('128k','192k','256k','320k','384k','448k','512k','640k'),
                width=7, state='readonly').grid(row=row, column=1, sticky='w', **pad)
            row += 1

            # ── Strip options ──
            v_strip_ch = tk.BooleanVar(value=ov.get('strip_chapters', opt_strip_chapters.get()))
            v_strip_tg = tk.BooleanVar(value=ov.get('strip_tags', opt_strip_tags.get()))
            v_strip_sb = tk.BooleanVar(value=ov.get('strip_subs', opt_strip_subs.get()))
            cf = ttk.Frame(fr)
            cf.grid(row=row, column=0, columnspan=2, sticky='w', **pad); row += 1
            ttk.Checkbutton(cf, text="Strip chapters", variable=v_strip_ch).pack(side='left', padx=4)
            ttk.Checkbutton(cf, text="Strip tags", variable=v_strip_tg).pack(side='left', padx=4)
            ttk.Checkbutton(cf, text="Strip subs", variable=v_strip_sb).pack(side='left', padx=4)

            # ── Mux subs ──
            v_mux = tk.BooleanVar(value=ov.get('mux_subs', opt_mux_subs.get()))
            ttk.Checkbutton(fr, text="Mux external subtitles", variable=v_mux).grid(
                row=row, column=0, columnspan=2, sticky='w', **pad); row += 1

            # ── Metadata ──
            v_meta = tk.BooleanVar(value=ov.get('set_metadata', opt_set_metadata.get()))
            ttk.Checkbutton(fr, text="Set track metadata", variable=v_meta).grid(
                row=row, column=0, columnspan=2, sticky='w', **pad); row += 1

            mf = ttk.Frame(fr)
            mf.grid(row=row, column=0, columnspan=2, sticky='w', **pad); row += 1
            ttk.Label(mf, text="V:").pack(side='left')
            v_mv = tk.StringVar(value=ov.get('meta_video', opt_meta_video.get()))
            ttk.Entry(mf, textvariable=v_mv, width=4).pack(side='left', padx=(2,6))
            ttk.Label(mf, text="A:").pack(side='left')
            v_ma = tk.StringVar(value=ov.get('meta_audio', opt_meta_audio.get()))
            ttk.Entry(mf, textvariable=v_ma, width=4).pack(side='left', padx=(2,6))
            ttk.Label(mf, text="S:").pack(side='left')
            v_ms = tk.StringVar(value=ov.get('meta_sub', opt_meta_sub.get()))
            ttk.Entry(mf, textvariable=v_ms, width=4).pack(side='left', padx=(2,0))

            # ── Container ──
            ttk.Label(fr, text="Output container:").grid(row=row, column=0, sticky='w', **pad)
            v_ctr = tk.StringVar(value=ov.get('container', opt_container.get()))
            ttk.Combobox(fr, textvariable=v_ctr, values=('.mkv', '.mp4'),
                         width=6, state='readonly').grid(row=row, column=1, sticky='w', **pad)
            row += 1

            # ── Buttons ──
            bf = ttk.Frame(dlg, padding=(12,0,12,12))
            bf.pack(fill='x')
            def _save_ovr():
                f['overrides'] = {
                    'convert_audio': v_conv_audio.get(),
                    'audio_codec': mp_audio_codec_map.get(v_acodec.get(), v_acodec.get()),
                    'audio_codec_display': v_acodec.get(),
                    'audio_bitrate': v_abitrate.get(),
                    'strip_chapters': v_strip_ch.get(),
                    'strip_tags': v_strip_tg.get(),
                    'strip_subs': v_strip_sb.get(),
                    'mux_subs': v_mux.get(),
                    'set_metadata': v_meta.get(),
                    'meta_video': v_mv.get(),
                    'meta_audio': v_ma.get(),
                    'meta_sub': v_ms.get(),
                    'container': v_ctr.get(),
                }
                _rebuild_tree()
                _log(f"Override saved for: {f['name']}", 'INFO')
                dlg.destroy()
            ttk.Button(bf, text="Save Override", command=_save_ovr).pack(side='right', padx=(4,0))
            ttk.Button(bf, text="Cancel", command=dlg.destroy).pack(side='right')

        def _show_sub_manager(index):
            """Show subtitle file manager for a single file."""
            f = mp_files[index]
            dlg = tk.Toplevel(win)
            dlg.title(f"Subtitles — {f['name']}")
            dlg.geometry("500x300")
            dlg.transient(win)
            dlg.grab_set()
            dlg.resizable(True, True)
            app._center_on_main(dlg)

            subs = list(f['ext_subs'])  # work on a copy

            fr = ttk.Frame(dlg, padding=12)
            fr.pack(fill='both', expand=True)
            fr.columnconfigure(0, weight=1)
            fr.rowconfigure(0, weight=1)

            sub_list = tk.Listbox(fr, height=6)
            sub_list.grid(row=0, column=0, sticky='nsew')

            def _refresh():
                sub_list.delete(0, 'end')
                for stype, spath in subs:
                    sub_list.insert('end', f"[{stype}] {os.path.basename(spath)}")

            def _add_sub():
                paths = filedialog.askopenfilenames(
                    parent=win,
                    title="Select Subtitle Files",
                    filetypes=[("Subtitle files", "*.srt *.ass *.ssa *.vtt *.sub"),
                               ("All files", "*.*")])
                for p in paths:
                    # Guess type from filename
                    bn = os.path.basename(p).lower()
                    if 'forced' in bn:
                        stype = 'forced'
                    else:
                        stype = 'main'
                    subs.append((stype, p))
                _refresh()

            def _remove_sub():
                sel = sub_list.curselection()
                if sel:
                    for i in sorted(sel, reverse=True):
                        del subs[i]
                    _refresh()

            def _toggle_type():
                sel = sub_list.curselection()
                if sel:
                    i = sel[0]
                    stype, spath = subs[i]
                    new_type = 'forced' if stype == 'main' else 'main'
                    subs[i] = (new_type, spath)
                    _refresh()

            def _move_up():
                sel = sub_list.curselection()
                if sel and sel[0] > 0:
                    i = sel[0]
                    subs[i-1], subs[i] = subs[i], subs[i-1]
                    _refresh()
                    sub_list.selection_set(i-1)

            def _move_down():
                sel = sub_list.curselection()
                if sel and sel[0] < len(subs) - 1:
                    i = sel[0]
                    subs[i], subs[i+1] = subs[i+1], subs[i]
                    _refresh()
                    sub_list.selection_set(i+1)

            btn_fr = ttk.Frame(fr)
            btn_fr.grid(row=1, column=0, sticky='ew', pady=(6,0))
            ttk.Button(btn_fr, text="Add...", command=_add_sub).pack(side='left', padx=2)
            ttk.Button(btn_fr, text="Remove", command=_remove_sub).pack(side='left', padx=2)
            ttk.Button(btn_fr, text="Toggle Main/Forced", command=_toggle_type).pack(side='left', padx=2)
            ttk.Separator(btn_fr, orient='vertical').pack(side='left', fill='y', padx=6)
            ttk.Button(btn_fr, text="⬆ Up", command=_move_up, width=4).pack(side='left', padx=2)
            ttk.Button(btn_fr, text="⬇ Down", command=_move_down, width=5).pack(side='left', padx=2)

            bot = ttk.Frame(dlg, padding=(12,0,12,12))
            bot.pack(fill='x')
            def _save_subs():
                f['ext_subs'] = list(subs)
                _rebuild_tree()
                _log(f"Subtitles updated for: {f['name']} ({len(subs)} file(s))", 'INFO')
                dlg.destroy()
            ttk.Button(bot, text="Save", command=_save_subs).pack(side='right', padx=(4,0))
            ttk.Button(bot, text="Cancel", command=dlg.destroy).pack(side='right')
            _refresh()

        # Double-click opens override dialog
        def _on_double_click(event):
            item = tree.identify_row(event.y)
            if item and not mp_processing[0]:
                items = tree.get_children()
                index = list(items).index(item)
                _show_file_override(index)
        tree.bind('<Double-1>', _on_double_click)

        # ── Operations panel ──
        ops_frame = ttk.LabelFrame(main_frame, text="Operations", padding=8)
        ops_frame.grid(row=2, column=0, sticky='ew', pady=(0, 6))

        # Row 1: Audio + strip options
        ops_row1 = ttk.Frame(ops_frame)
        ops_row1.pack(fill='x', pady=2)

        def _toggle_audio_controls():
            st = 'readonly' if opt_convert_audio.get() else 'disabled'
            mp_ac_combo.configure(state=st)
            mp_br_combo.configure(state=st)

        ttk.Checkbutton(ops_row1, text="Convert audio:",
                       variable=opt_convert_audio, command=_toggle_audio_controls).pack(side='left', padx=(0, 4))
        mp_ac_combo = ttk.Combobox(ops_row1, textvariable=opt_audio_codec,
                                   width=18, state='readonly')
        mp_ac_combo['values'] = list(mp_audio_codec_map.keys())
        mp_ac_combo.pack(side='left', padx=(0, 8))
        ttk.Label(ops_row1, text="Bitrate:").pack(side='left', padx=(0, 2))
        mp_br_combo = ttk.Combobox(ops_row1, textvariable=opt_audio_bitrate,
                                   values=('128k', '192k', '256k', '320k', '384k', '448k', '512k', '640k'),
                                   width=6, state='readonly')
        mp_br_combo.pack(side='left', padx=(0, 16))

        ttk.Checkbutton(ops_row1, text="Strip chapters",
                       variable=opt_strip_chapters).pack(side='left', padx=4)
        ttk.Checkbutton(ops_row1, text="Strip tags",
                       variable=opt_strip_tags).pack(side='left', padx=4)

        # Row 2: Strip subs + mux subs + sub language
        ops_row2 = ttk.Frame(ops_frame)
        ops_row2.pack(fill='x', pady=2)

        ttk.Checkbutton(ops_row2, text="Strip existing subtitles",
                       variable=opt_strip_subs).pack(side='left', padx=(0, 4))
        ttk.Checkbutton(ops_row2, text="Mux external subtitles",
                       variable=opt_mux_subs).pack(side='left', padx=(16, 4))
        ttk.Label(ops_row2, text="Lang:").pack(side='left', padx=(8, 2))
        sub_lang_entry = ttk.Entry(ops_row2, textvariable=opt_sub_lang, width=4)
        sub_lang_entry.pack(side='left', padx=(0, 4))
        ttk.Button(ops_row2, text="🔄 Rescan", command=_rescan_subs, width=8).pack(side='left', padx=4)

        # Row 3: Track metadata
        ops_row3 = ttk.Frame(ops_frame)
        ops_row3.pack(fill='x', pady=2)

        def _toggle_meta_fields():
            st = 'normal' if opt_set_metadata.get() else 'disabled'
            mp_mv.configure(state=st)
            mp_ma.configure(state=st)
            mp_ms.configure(state=st)

        ttk.Checkbutton(ops_row3, text="Set track metadata:",
                       variable=opt_set_metadata, command=_toggle_meta_fields).pack(side='left', padx=(0, 4))
        ttk.Label(ops_row3, text="V:").pack(side='left')
        mp_mv = ttk.Entry(ops_row3, textvariable=opt_meta_video, width=4)
        mp_mv.pack(side='left', padx=(2, 6))
        ttk.Label(ops_row3, text="A:").pack(side='left')
        mp_ma = ttk.Entry(ops_row3, textvariable=opt_meta_audio, width=4)
        mp_ma.pack(side='left', padx=(2, 6))
        ttk.Label(ops_row3, text="S:").pack(side='left')
        mp_ms = ttk.Entry(ops_row3, textvariable=opt_meta_sub, width=4)
        mp_ms.pack(side='left', padx=(2, 0))

        _toggle_meta_fields()
        _toggle_audio_controls()

        # Row 3b: Edition tagging
        ops_row3b = ttk.Frame(ops_frame)
        ops_row3b.pack(fill='x', pady=2)

        ttk.Label(ops_row3b, text="Edition:").pack(side='left', padx=(0, 2))
        mp_edition_combo = ttk.Combobox(ops_row3b, textvariable=opt_edition_tag,
                                         values=EDITION_PRESETS, width=22, state='readonly')
        mp_edition_combo.pack(side='left', padx=(0, 4))

        mp_edition_custom = ttk.Entry(ops_row3b, textvariable=_edition_custom_sv, width=22)

        # If loaded value is a custom edition (not in presets), show custom entry
        if opt_edition_tag.get() and opt_edition_tag.get() not in EDITION_PRESETS:
            _edition_custom_sv.set(opt_edition_tag.get())
            mp_edition_combo.set('Custom...')
            mp_edition_custom.pack(side='left', padx=(0, 4))

        def _on_mp_edition_select(event=None):
            sel = mp_edition_combo.get()
            if sel == 'Custom...':
                mp_edition_custom.pack(side='left', padx=(0, 4))
                mp_edition_custom.focus()
            else:
                mp_edition_custom.pack_forget()
                opt_edition_tag.set(sel)
        mp_edition_combo.bind('<<ComboboxSelected>>', _on_mp_edition_select)

        def _on_mp_edition_custom(*args):
            if mp_edition_combo.get() == 'Custom...':
                opt_edition_tag.set(_edition_custom_sv.get())
        _edition_custom_sv.trace_add('write', _on_mp_edition_custom)

        ttk.Checkbutton(ops_row3b, text="Add to filename (Plex)",
                        variable=opt_edition_fn).pack(side='left', padx=(8, 0))

        # Row 3c: Chapter insertion
        ops_row3c = ttk.Frame(ops_frame)
        ops_row3c.pack(fill='x', pady=2)

        def _toggle_ch_spin():
            mp_ch_spin.configure(state='normal' if opt_add_chapters.get() else 'disabled')
            if opt_add_chapters.get():
                opt_strip_chapters.set(False)

        ttk.Checkbutton(ops_row3c, text="Add chapters every",
                        variable=opt_add_chapters,
                        command=_toggle_ch_spin).pack(side='left', padx=(0, 2))
        mp_ch_spin = tk.Spinbox(ops_row3c, textvariable=opt_ch_interval,
                                from_=1, to=60, width=3, state='disabled')
        mp_ch_spin.pack(side='left', padx=(0, 2))
        ttk.Label(ops_row3c, text="minutes").pack(side='left')
        _toggle_ch_spin()

        # Row 4: Output + parallel + container
        ops_row4 = ttk.Frame(ops_frame)
        ops_row4.pack(fill='x', pady=2)

        ttk.Label(ops_row4, text="Output:").pack(side='left', padx=(0, 4))
        ttk.Radiobutton(ops_row4, text="Replace in-place", variable=opt_output_mode,
                        value='inplace', command=lambda: _toggle_output_folder()).pack(side='left', padx=(0, 4))
        ttk.Radiobutton(ops_row4, text="Save to folder:", variable=opt_output_mode,
                        value='folder', command=lambda: _toggle_output_folder()).pack(side='left', padx=(0, 4))
        mp_out_entry = ttk.Entry(ops_row4, textvariable=opt_output_folder, width=24, state='disabled')
        mp_out_entry.pack(side='left', padx=(0, 4))
        mp_out_btn = ttk.Button(ops_row4, text="Browse…", state='disabled',
            command=lambda: opt_output_folder.set(
                ask_directory(title="Select Output Folder", parent=win) or opt_output_folder.get()))
        mp_out_btn.pack(side='left', padx=(0, 12))

        ttk.Label(ops_row4, text="Container:").pack(side='left', padx=(0, 2))
        ttk.Combobox(ops_row4, textvariable=opt_container,
                     values=('.mkv', '.mp4'), width=5, state='readonly').pack(side='left', padx=(0, 12))

        ttk.Checkbutton(ops_row4, text="Parallel",
                       variable=opt_parallel).pack(side='left', padx=(0, 2))
        ttk.Label(ops_row4, text="Jobs:").pack(side='left', padx=(0, 2))
        ttk.Spinbox(ops_row4, textvariable=opt_max_jobs, from_=1, to=32,
                    width=3).pack(side='left')

        def _toggle_output_folder():
            st = 'normal' if opt_output_mode.get() == 'folder' else 'disabled'
            mp_out_entry.configure(state=st)
            mp_out_btn.configure(state=st)
        _toggle_output_folder()

        # ── Progress bar ──
        progress_frame = ttk.Frame(main_frame)
        progress_frame.grid(row=3, column=0, sticky='ew', pady=(0, 6))
        progress_frame.columnconfigure(1, weight=1)

        mp_progress_var = tk.DoubleVar(value=0)
        mp_progress_label = ttk.Label(progress_frame, text="Ready")
        mp_progress_label.grid(row=0, column=0, sticky='w', padx=(0, 8))
        mp_progress_bar = ttk.Progressbar(progress_frame, variable=mp_progress_var,
                                          maximum=100, mode='determinate')
        mp_progress_bar.grid(row=0, column=1, sticky='ew')

        # ── Action buttons ──
        btn_frame = ttk.Frame(progress_frame)
        btn_frame.grid(row=0, column=2, padx=(8, 0))
        process_btn = ttk.Button(btn_frame, text="▶ Process All", command=lambda: _start_processing())
        process_btn.pack(side='left', padx=2)
        stop_btn = ttk.Button(btn_frame, text="⏹ Stop", command=lambda: _stop_processing(), state='disabled')
        stop_btn.pack(side='left', padx=2)

        # ── Log ──
        log_frame = ttk.LabelFrame(main_frame, text="Log", padding=5)
        log_frame.grid(row=4, column=0, sticky='nsew')
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)

        log_text = tk.Text(log_frame, height=8, wrap='word', font=('Courier', 9),
                          state='disabled', bg='#1e1e1e', fg='#d4d4d4')
        log_text.grid(row=0, column=0, sticky='nsew')
        log_scroll = ttk.Scrollbar(log_frame, orient='vertical', command=log_text.yview)
        log_scroll.grid(row=0, column=1, sticky='ns')
        log_text.configure(yscrollcommand=log_scroll.set)

        def _clear_log():
            log_text.configure(state='normal')
            log_text.delete('1.0', 'end')
            log_text.configure(state='disabled')

        ttk.Button(log_frame, text="Clear Log", command=_clear_log).grid(row=1, column=0, sticky='w', pady=(4, 0))

        # Log color tags
        log_text.tag_configure('INFO',    foreground='#d4d4d4')
        log_text.tag_configure('SUCCESS', foreground='#4ec9b0')
        log_text.tag_configure('WARNING', foreground='#dcdcaa')
        log_text.tag_configure('ERROR',   foreground='#f44747')
        log_text.tag_configure('SKIP',    foreground='#569cd6')

        def _log(msg, level='INFO'):
            def _do():
                log_text.configure(state='normal')
                ts = datetime.now().strftime('%H:%M:%S')
                log_text.insert('end', f"[{ts}] [{level}] {msg}\n", level)
                log_text.see('end')
                log_text.configure(state='disabled')
            # Safe to call from any thread
            win.after(0, _do)

        # ── Preflight check ──
        def _preflight():
            """Validate files before processing. Returns True if OK to proceed."""
            errors = 0
            warnings = 0
            _log("═" * 50, 'INFO')
            _log("Pre-flight check", 'INFO')
            _log("═" * 50, 'INFO')

            for i, f in enumerate(mp_files):
                filepath = f['path']
                name = f['name']
                ov = f.get('overrides', {})

                # Readable?
                if not os.access(filepath, os.R_OK):
                    _log(f"  {name}: File is not readable", 'ERROR')
                    errors += 1; continue

                # Empty?
                if os.path.getsize(filepath) == 0:
                    _log(f"  {name}: File is empty (0 bytes)", 'ERROR')
                    errors += 1; continue

                # Missing subtitle files?
                do_mux = ov.get('mux_subs', opt_mux_subs.get())
                if do_mux and not f['ext_subs']:
                    lang = opt_sub_lang.get()
                    _log(f"  {name}: No .{lang}.srt found alongside file", 'WARNING')
                    warnings += 1

                # Audio info?
                if not f['audio_info']:
                    _log(f"  {name}: Could not read audio stream", 'WARNING')
                    warnings += 1

            if errors > 0:
                _log(f"Pre-flight FAILED — {errors} error(s), {warnings} warning(s)", 'ERROR')
                return False
            elif warnings > 0:
                _log(f"Pre-flight PASSED with {warnings} warning(s)", 'WARNING')
            else:
                _log("Pre-flight PASSED — all checks OK", 'SUCCESS')

            _log("═" * 50, 'INFO')
            return True

        # ── Resolve effective setting (per-file override > global) ──
        def _ov(f, key, global_var):
            """Get effective value: per-file override if set, else global."""
            ov = f.get('overrides', {})
            if key in ov:
                return ov[key]
            if isinstance(global_var, (tk.BooleanVar, tk.StringVar, tk.IntVar)):
                return global_var.get()
            return global_var

        # ── Build ffmpeg command for one file ──
        def _build_cmd(f):
            """Build a single ffmpeg remux command for the given file dict.
            Returns (cmd_list, output_path) or (None, None) on error."""
            input_path = f['path']
            base, ext = os.path.splitext(input_path)
            ov = f.get('overrides', {})

            # Determine output container
            out_ext = _ov(f, 'container', opt_container)

            # Edition filename tag for Plex
            edition_fn_part = ''
            edition = _ov(f, 'edition_tag', opt_edition_tag)
            if isinstance(edition, tk.StringVar):
                edition = edition.get()
            edition_in_fn = _ov(f, 'edition_in_filename', opt_edition_fn)
            if isinstance(edition_in_fn, tk.BooleanVar):
                edition_in_fn = edition_in_fn.get()
            if edition and edition_in_fn:
                edition_fn_part = ' {edition-' + edition + '}'

            # Determine output path
            if opt_output_mode.get() == 'folder' and opt_output_folder.get():
                out_dir = opt_output_folder.get()
                out_name = os.path.join(out_dir,
                                         os.path.basename(base) + edition_fn_part + out_ext)
            else:
                # In-place: write to temp, replace on success
                out_name = f"{base}_mp_tmp{out_ext}"

            cmd = ['ffmpeg', '-y', '-i', input_path]

            # Additional inputs: external subtitle files
            sub_inputs = []
            do_mux = _ov(f, 'mux_subs', opt_mux_subs)
            if do_mux:
                for stype, spath in f['ext_subs']:
                    cmd.extend(['-i', spath])
                    sub_inputs.append((stype, spath))

            # Chapter injection
            ch_meta_path = None
            ch_input_idx = None
            do_add_ch = _ov(f, 'add_chapters', opt_add_chapters)
            if isinstance(do_add_ch, tk.BooleanVar):
                do_add_ch = do_add_ch.get()
            if do_add_ch and f.get('duration_secs'):
                ch_intv = _ov(f, 'chapter_interval', opt_ch_interval)
                if isinstance(ch_intv, tk.IntVar):
                    ch_intv = ch_intv.get()
                chs = generate_auto_chapters(f['duration_secs'], ch_intv)
                if chs:
                    ch_meta_path = chapters_to_ffmetadata(chs)
                    if ch_meta_path:
                        ch_input_idx = 1 + len(sub_inputs)  # input 0=main, 1..N=subs
                        cmd.extend(['-i', ch_meta_path])
                        f['_ch_meta_path'] = ch_meta_path  # for cleanup

            # ── Mapping ──
            cmd.extend(['-map', '0:v:0?', '-map', '0:a?'])

            # Subtitle mapping
            do_strip_subs = _ov(f, 'strip_subs', opt_strip_subs)
            if do_strip_subs:
                pass  # Don't map source subtitles
            else:
                cmd.extend(['-map', '0:s?'])

            # Map external subtitle inputs
            for idx, (stype, spath) in enumerate(sub_inputs):
                input_idx = 1 + idx
                cmd.extend(['-map', f'{input_idx}:0'])

            # ── Video: always copy (remux only) ──
            cmd.extend(['-c:v', 'copy'])

            # ── Audio ──
            do_convert_audio = _ov(f, 'convert_audio', opt_convert_audio)
            if do_convert_audio:
                # Resolve codec
                if 'audio_codec' in ov:
                    target_codec = ov['audio_codec']
                else:
                    selected_display = opt_audio_codec.get()
                    target_codec = mp_audio_codec_map.get(selected_display, selected_display)

                audio_bitrate = _ov(f, 'audio_bitrate', opt_audio_bitrate)

                if target_codec == 'copy':
                    cmd.extend(['-c:a', 'copy'])
                else:
                    audio = f.get('audio_info', [])
                    src_codec = audio[0]['codec_name'] if audio else ''
                    codec_aliases = {
                        'ac3': ('ac3', 'eac3'),
                        'eac3': ('eac3',),
                        'aac': ('aac',),
                        'mp3': ('mp3',),
                        'opus': ('opus',),
                        'flac': ('flac',),
                    }
                    match_set = codec_aliases.get(target_codec, (target_codec,))
                    if src_codec in match_set:
                        cmd.extend(['-c:a', 'copy'])
                        _log(f"  Audio: already {src_codec.upper()}, copying", 'SKIP')
                    else:
                        LOSSLESS = {'flac'}
                        EXPERIMENTAL = {'opus', 'vorbis'}
                        cmd.extend(['-c:a', target_codec])
                        if target_codec in EXPERIMENTAL:
                            cmd.extend(['-strict', '-2'])
                        if target_codec not in LOSSLESS:
                            cmd.extend(['-b:a', audio_bitrate])
            else:
                cmd.extend(['-c:a', 'copy'])

            # ── Subtitles codec ──
            if not do_strip_subs:
                # Handle container compatibility for internal subs
                if out_ext == '.mp4':
                    cmd.extend(['-c:s', 'mov_text'])
                else:
                    cmd.extend(['-c:s', 'copy'])
            # External subs
            out_sub_idx = 0
            if not do_strip_subs:
                out_sub_idx = f.get('sub_count', 0)

            do_set_meta = _ov(f, 'set_metadata', opt_set_metadata)
            s_lang = _ov(f, 'meta_sub', opt_meta_sub)
            for idx, (stype, spath) in enumerate(sub_inputs):
                si = out_sub_idx + idx
                sub_codec = 'mov_text' if out_ext == '.mp4' else 'srt'
                cmd.extend([f'-c:s:{si}', sub_codec])
                sub_lang = s_lang if do_set_meta else opt_sub_lang.get()
                cmd.extend([f'-metadata:s:s:{si}', f'language={sub_lang}'])
                if stype == 'main':
                    cmd.extend([f'-metadata:s:s:{si}', 'title=English'])
                    cmd.extend([f'-disposition:s:{si}', 'default'])
                elif stype == 'forced':
                    cmd.extend([f'-metadata:s:s:{si}', 'title=Forced'])
                    cmd.extend([f'-disposition:s:{si}', 'forced'])

            # ── Chapters ──
            if ch_meta_path:
                cmd.extend(['-map_chapters', str(ch_input_idx)])
            elif _ov(f, 'strip_chapters', opt_strip_chapters):
                cmd.extend(['-map_chapters', '-1'])

            # ── Tags ──
            if _ov(f, 'strip_tags', opt_strip_tags):
                cmd.extend(['-map_metadata', '-1'])

            # ── Track metadata ──
            if do_set_meta:
                v_lang = _ov(f, 'meta_video', opt_meta_video)
                a_lang = _ov(f, 'meta_audio', opt_meta_audio)
                cmd.extend(['-metadata', 'title='])
                cmd.extend(['-metadata:s:v:0', f'language={v_lang}', '-metadata:s:v:0', 'title='])
                cmd.extend(['-metadata:s:a:0', f'language={a_lang}', '-metadata:s:a:0', 'title='])
                if (not do_strip_subs and f.get('sub_count', 0) > 0) or sub_inputs:
                    if not sub_inputs:
                        cmd.extend([f'-metadata:s:s:0', f'language={s_lang}'])

            # ── Edition tag ──
            edition = _ov(f, 'edition_tag', opt_edition_tag)
            if isinstance(edition, tk.StringVar):
                edition = edition.get()
            if edition:
                cmd.extend(['-metadata', f'title={edition}'])

            cmd.append(out_name)
            return cmd, out_name

        # ── Process a single file (called from thread pool or sequential loop) ──
        def _process_one(i, f):
            """Process a single file. Returns ('done'|'failed'|'skipped', index)."""
            if mp_stop[0]:
                return ('stopped', i)

            name = f['name']
            win.after(0, lambda idx=i: _update_tree_status(idx, '⏳ Processing'))

            _log(f"── {name} ──", 'INFO')

            try:
                cmd, out_path = _build_cmd(f)
                if cmd is None:
                    _log(f"  Skipped (could not build command)", 'WARNING')
                    win.after(0, lambda idx=i: _update_tree_status(idx, '⏭️ Skipped'))
                    return ('skipped', i)

                _log(f"  Command: {' '.join(cmd)}", 'INFO')

                proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1)

                last_lines = []
                for line in proc.stdout:
                    if mp_stop[0]:
                        proc.terminate()
                        return ('stopped', i)
                    line = line.strip()
                    if line:
                        last_lines.append(line)
                        if len(last_lines) > 5:
                            last_lines.pop(0)

                proc.wait()

                if proc.returncode == 0:
                    # In-place mode: replace original
                    is_inplace = opt_output_mode.get() == 'inplace' or not opt_output_folder.get()
                    if is_inplace:
                        original = f['path']
                        try:
                            os.replace(out_path, original)
                        except OSError as e:
                            _log(f"  Warning: could not replace original: {e}", 'WARNING')
                        final_path = original
                    else:
                        final_path = out_path

                    out_size = os.path.getsize(final_path) if os.path.exists(final_path) else 0
                    out_mb = f'{out_size / (1024*1024):.1f} MB' if out_size else '?'
                    _log(f"  ✅ Done — output: {out_mb}", 'SUCCESS')
                    win.after(0, lambda idx=i: _update_tree_status(idx, '✅ Done'))
                    return ('done', i)
                else:
                    _log(f"  ❌ Failed (exit code {proc.returncode})", 'ERROR')
                    for ll in last_lines:
                        _log(f"    {ll}", 'ERROR')
                    win.after(0, lambda idx=i: _update_tree_status(idx, '❌ Failed'))
                    # Clean up partial output
                    if os.path.exists(out_path):
                        try:
                            os.remove(out_path)
                        except OSError:
                            pass
                    return ('failed', i)

            except Exception as e:
                _log(f"  ❌ Error: {e}", 'ERROR')
                win.after(0, lambda idx=i: _update_tree_status(idx, '❌ Error'))
                return ('failed', i)
            finally:
                # Clean up chapter metadata temp file
                ch_tmp = f.pop('_ch_meta_path', None)
                if ch_tmp:
                    try:
                        os.remove(ch_tmp)
                    except OSError:
                        pass

        # ── Processing thread (orchestrator) ──
        def _process_files():
            mp_processing[0] = True
            mp_stop[0] = False
            total = len(mp_files)
            completed = [0]
            failed = [0]
            processed_count = [0]

            win.after(0, lambda: process_btn.configure(state='disabled'))
            win.after(0, lambda: stop_btn.configure(state='normal'))

            use_parallel = opt_parallel.get()
            max_jobs = opt_max_jobs.get()

            mode_label = f"parallel ({max_jobs} jobs)" if use_parallel else "sequential"
            _log(f"Processing {total} file(s) — {mode_label}", 'INFO')

            def _on_result(result, idx):
                status, _ = result
                with mp_lock:
                    processed_count[0] += 1
                    if status == 'done':
                        completed[0] += 1
                    elif status == 'failed':
                        failed[0] += 1
                    pct = (processed_count[0] / total) * 100
                win.after(0, lambda p=pct: mp_progress_var.set(p))
                win.after(0, lambda c=processed_count[0]: mp_progress_label.configure(
                    text=f"Processing {c}/{total}"))

            if use_parallel and max_jobs > 1:
                with ThreadPoolExecutor(max_workers=max_jobs) as executor:
                    futures = {}
                    for i, f in enumerate(mp_files):
                        if mp_stop[0]:
                            break
                        future = executor.submit(_process_one, i, f)
                        futures[future] = i

                    for future in as_completed(futures):
                        idx = futures[future]
                        try:
                            result = future.result()
                            _on_result(result, idx)
                        except Exception as e:
                            _log(f"  ❌ Unexpected error: {e}", 'ERROR')
                            with mp_lock:
                                processed_count[0] += 1
                                failed[0] += 1
            else:
                for i, f in enumerate(mp_files):
                    if mp_stop[0]:
                        _log("Processing stopped by user", 'WARNING')
                        break
                    result = _process_one(i, f)
                    _on_result(result, i)

            # Done
            _log("═" * 50, 'INFO')
            _log(f"Complete — {completed[0]} succeeded, {failed[0]} failed, "
                 f"{total - completed[0] - failed[0]} skipped/stopped", 'SUCCESS')
            _log("═" * 50, 'INFO')

            # Clean up .srt files if muxing was enabled
            if opt_mux_subs.get() and completed[0] > 0:
                cleaned = 0
                for f in mp_files:
                    if f['status'] == '✅ Done':
                        for stype, spath in f.get('ext_subs', []):
                            if os.path.exists(spath):
                                try:
                                    os.remove(spath)
                                    cleaned += 1
                                except OSError:
                                    pass
                if cleaned:
                    _log(f"Cleaned up {cleaned} subtitle file(s)", 'INFO')

            # Re-probe completed files to update display
            if completed[0] > 0:
                _log("Re-probing completed files...", 'INFO')
                for i, f in enumerate(mp_files):
                    if f['status'] == '✅ Done' and os.path.exists(f['path']):
                        _reprobe_file(i)

            win.after(0, lambda: process_btn.configure(state='normal'))
            win.after(0, lambda: stop_btn.configure(state='disabled'))
            win.after(0, lambda: mp_progress_label.configure(
                text=f"Done — {completed[0]}/{total} processed"))
            mp_processing[0] = False

        def _start_processing():
            if not mp_files:
                _log("No files to process", 'WARNING')
                return
            if mp_processing[0]:
                return

            # Validate output folder if using folder mode
            if opt_output_mode.get() == 'folder':
                folder = opt_output_folder.get().strip()
                if not folder:
                    _log("Output folder not set — select a folder or use in-place mode", 'ERROR')
                    return
                os.makedirs(folder, exist_ok=True)

            # Run preflight
            if not _preflight():
                return

            # Start in thread
            t = threading.Thread(target=_process_files, daemon=True)
            t.start()

        def _stop_processing():
            mp_stop[0] = True

        # ── Drag and drop support ──
        try:
            win.drop_target_register('DND_Files')
            def _on_drop(event):
                raw = event.data
                # Parse file:// URIs and space-separated paths
                paths = []
                if 'file://' in raw:
                    from urllib.parse import unquote, urlparse
                    for token in raw.split():
                        token = token.strip()
                        if token.startswith('file://'):
                            parsed = urlparse(token)
                            paths.append(unquote(parsed.path))
                elif raw.startswith('{') and raw.endswith('}'):
                    # Tcl list format for paths with spaces
                    paths = [p.strip('{}') for p in re.findall(r'\{[^}]+\}|[^\s]+', raw)]
                else:
                    paths = raw.split()

                added = 0
                for p in paths:
                    if os.path.isfile(p) and os.path.splitext(p)[1].lower() in VIDEO_EXTENSIONS:
                        _add_one_file(p)
                        added += 1
                    elif os.path.isdir(p):
                        for ext in VIDEO_EXTENSIONS:
                            for fp in Path(p).glob(f'*{ext}'):
                                if fp.is_file() and not fp.name.startswith('.'):
                                    _add_one_file(str(fp))
                                    added += 1
                if added:
                    _rebuild_tree()
                    _log(f"Added {added} file(s) via drag-and-drop", 'INFO')

            win.dnd_bind('<<Drop>>', _on_drop)
        except Exception:
            pass  # tkinterdnd2 not available

        # ── Save / Close ──
        def _save_mp_prefs():
            """Save current Media Processor settings to preferences."""
            mp_prefs = {
                'convert_audio':  opt_convert_audio.get(),
                'audio_codec':    opt_audio_codec.get(),
                'audio_bitrate':  opt_audio_bitrate.get(),
                'strip_chapters': opt_strip_chapters.get(),
                'strip_tags':     opt_strip_tags.get(),
                'strip_subs':     opt_strip_subs.get(),
                'set_metadata':   opt_set_metadata.get(),
                'meta_video':     opt_meta_video.get(),
                'meta_audio':     opt_meta_audio.get(),
                'meta_sub':       opt_meta_sub.get(),
                'mux_subs':       opt_mux_subs.get(),
                'sub_lang':       opt_sub_lang.get(),
                'output_mode':    opt_output_mode.get(),
                'output_folder':  opt_output_folder.get(),
                'container':      opt_container.get(),
                'parallel':       opt_parallel.get(),
                'max_jobs':       opt_max_jobs.get(),
                'edition_tag':    opt_edition_tag.get(),
                'edition_in_filename': opt_edition_fn.get(),
                'add_chapters':   opt_add_chapters.get(),
                'chapter_interval': opt_ch_interval.get(),
            }
            app._media_proc_prefs = mp_prefs
            # Write to shared preferences file
            try:
                prefs_path = getattr(app, '_prefs_path', None)
                if prefs_path:
                    # StandaloneContext uses a string path
                    if isinstance(prefs_path, str):
                        p = Path(prefs_path)
                    else:
                        # Main app uses a method
                        p = prefs_path() if callable(prefs_path) else Path(str(prefs_path))
                    if p.exists():
                        prefs = json.loads(p.read_text())
                    else:
                        prefs = {}
                    prefs['media_processor'] = mp_prefs
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text(json.dumps(prefs, indent=2))
            except Exception:
                pass  # best-effort save

        close_frame = ttk.Frame(main_frame)
        close_frame.grid(row=5, column=0, sticky='e', pady=(6, 0))
        def _close_window():
            _save_mp_prefs()
            win.destroy()
            if getattr(app, '_standalone_mode', False):
                app.root.destroy()

        ttk.Button(close_frame, text="Close", command=_close_window).pack(side='right')
        win.protocol('WM_DELETE_WINDOW', _close_window)

        _log("Media Processor ready — add files and click Process All", 'INFO')
        _log("Tip: drag and drop video files onto this window", 'INFO')
        _log(f"Subtitle matching: *.{opt_sub_lang.get()}.srt / *.{opt_sub_lang.get()}.forced.srt", 'INFO')

    # ── TV Show Renamer ────────────────────────────────────────────────────


def main():
    """Launch Media Processor as a standalone application."""
    from .standalone import create_standalone_root

    root, app = create_standalone_root(
        title="Media Processor",
        geometry="920x1080",
        minsize=(750, 850),
    )

    app._standalone_mode = True
    root.withdraw()
    open_media_processor(app)

    root.mainloop()


if __name__ == '__main__':
    main()
