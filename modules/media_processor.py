"""
Docflix Media Suite — Media Processor

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
from tkinter import ttk, messagebox

from .constants import (VIDEO_EXTENSIONS, SUBTITLE_EXTENSIONS, EDITION_PRESETS,
                        LANG_CODE_TO_NAME, SUBTITLE_LANGUAGES)
from .chapters import generate_auto_chapters, chapters_to_ffmetadata
from .utils import get_audio_info, get_subtitle_streams, ask_directory, ask_open_files, scaled_geometry, scaled_minsize
from .gpu import detect_closed_captions, get_video_codec, CC_STRIP_BSF

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
        win.withdraw()
        win.title("🔧 Docflix Media Processor")
        geom_str = scaled_geometry(win, 920, 720)
        win.geometry(geom_str)
        win.minsize(*scaled_minsize(win, 750, 520))
        win.update_idletasks()
        try:
            # Parse requested size from geometry string (e.g. "920x720")
            import re as _re
            gm = _re.match(r'(\d+)x(\d+)', geom_str)
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
        mp_files = []        # list of dicts
        mp_processing = [False]
        mp_stop = [False]
        mp_lock = threading.Lock()  # protects mp_files during parallel access
        mp_batch_total = [0]          # total files in current batch
        mp_batch_done = [0]           # files completed so far

        def _play_notification_sound():
            """Play the freedesktop completion sound via ffplay."""
            sound = '/usr/share/sounds/freedesktop/stereo/complete.oga'
            if not os.path.exists(sound):
                return
            def _play():
                try:
                    subprocess.run(
                        ['ffplay', '-nodisp', '-autoexit', '-loglevel', 'quiet', sound],
                        timeout=10)
                except Exception:
                    pass
            threading.Thread(target=_play, daemon=True).start()

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
        opt_strip_cc       = tk.BooleanVar(value=_mp.get('strip_cc', False))
        opt_set_metadata   = tk.BooleanVar(value=_mp.get('set_metadata', False))
        opt_meta_video     = tk.StringVar(value=_mp.get('meta_video', 'und'))
        opt_meta_audio     = tk.StringVar(value=_mp.get('meta_audio', 'eng'))
        opt_meta_sub       = tk.StringVar(value=_mp.get('meta_sub', 'eng'))
        opt_mux_subs       = tk.BooleanVar(value=_mp.get('mux_subs', False))
        opt_sub_lang       = tk.StringVar(value=_mp.get('sub_lang', 'eng'))
        opt_all_subs       = tk.BooleanVar(value=_mp.get('all_subs', False))
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

        # Track naming templates
        opt_name_tracks    = tk.BooleanVar(value=_mp.get('name_tracks', False))
        opt_name_video     = tk.StringVar(value=_mp.get('name_video', ''))
        opt_name_audio     = tk.StringVar(value=_mp.get('name_audio', '{lang} - {codec} {channels}'))
        opt_name_sub       = tk.StringVar(value=_mp.get('name_sub', '{lang}{flags}'))

        def _resolve_track_name(template, info):
            """Resolve a track naming template using stream info dict.
            Supported variables: {lang}, {codec}, {channels}, {bitrate}, {flags}"""
            if not template:
                return ''
            lang_code = info.get('language', 'und')
            lang_name = LANG_CODE_TO_NAME.get(lang_code, lang_code.upper() if lang_code != 'und' else '')

            codec_raw = info.get('codec_name', '')
            codec_display = codec_raw.upper()
            # Friendly codec names
            _codec_names = {
                'aac': 'AAC', 'ac3': 'AC3', 'eac3': 'EAC3', 'mp3': 'MP3',
                'opus': 'Opus', 'flac': 'FLAC', 'dts': 'DTS', 'truehd': 'TrueHD',
                'pcm_s16le': 'PCM', 'pcm_s24le': 'PCM', 'pcm_s32le': 'PCM',
                'vorbis': 'Vorbis', 'subrip': 'SRT', 'ass': 'ASS',
                'webvtt': 'WebVTT', 'mov_text': 'SRT',
                'hevc': 'HEVC', 'h264': 'H.264', 'av1': 'AV1', 'mpeg2video': 'MPEG-2',
            }
            codec_display = _codec_names.get(codec_raw, codec_display)

            # Channel layout
            channels = info.get('channels', 0)
            ch_map = {1: 'Mono', 2: '2.0', 6: '5.1', 8: '7.1'}
            ch_display = ch_map.get(channels, f'{channels}ch' if channels else '')

            # Bitrate
            bitrate_raw = info.get('bit_rate', '')
            if bitrate_raw and bitrate_raw.isdigit():
                br_kbps = int(bitrate_raw) // 1000
                br_display = f'{br_kbps}k'
            else:
                br_display = ''

            # Flags (for subtitles: SDH, Forced, Commentary)
            # Check disposition flags first, then fall back to parsing track title
            flags_parts = []
            title_raw = (info.get('title', '') or '').lower()
            # SDH / Hearing Impaired
            if info.get('sdh') or info.get('hearing_impaired'):
                flags_parts.append('SDH')
            elif any(kw in title_raw for kw in ('sdh', 'hearing', 'cc', ' hi')):
                flags_parts.append('SDH')
            # Forced
            if info.get('forced'):
                if 'Forced' not in flags_parts:
                    flags_parts.append('Forced')
            elif 'forced' in title_raw:
                flags_parts.append('Forced')
            # Commentary
            if info.get('comment'):
                flags_parts.append('Commentary')
            elif 'comment' in title_raw:
                flags_parts.append('Commentary')
            flags_display = (' - ' + ' / '.join(flags_parts)) if flags_parts else ''

            result = template.replace('{lang}', lang_name)
            result = result.replace('{codec}', codec_display)
            result = result.replace('{channels}', ch_display)
            result = result.replace('{bitrate}', br_display)
            result = result.replace('{flags}', flags_display)
            # Clean up double spaces and trailing separators
            result = re.sub(r'\s{2,}', ' ', result).strip()
            result = re.sub(r'[\s\-]+$', '', result).strip()
            return result

        # ── Menu bar ──
        menubar = tk.Menu(win)
        win.configure(menu=menubar)
        settings_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Settings", menu=settings_menu)
        settings_menu.add_command(label="Preferences...",
                                 command=lambda: _open_settings_dialog())

        def _open_settings_dialog():
            """Open the settings dialog for cleanup, subtitle, output, and processing options."""
            if mp_processing[0]:
                return  # don't allow settings changes during processing

            dlg = tk.Toplevel(win)
            dlg.title("Preferences")
            dlg.geometry(scaled_geometry(dlg, 500, 660))
            dlg.minsize(*scaled_minsize(dlg, 440, 580))
            dlg.transient(win)
            dlg.grab_set()
            dlg.resizable(True, True)
            app._center_on_main(dlg)

            fr = ttk.Frame(dlg, padding=12)
            fr.pack(fill='both', expand=True)
            fr.columnconfigure(0, weight=1)

            pad = {'padx': 8, 'pady': 2}

            # ── Cleanup ──
            cleanup_fr = ttk.LabelFrame(fr, text="Cleanup", padding=6)
            cleanup_fr.grid(row=0, column=0, sticky='ew', **pad)
            cr = ttk.Frame(cleanup_fr)
            cr.pack(fill='x')
            ttk.Checkbutton(cr, text="Strip chapters",
                            variable=opt_strip_chapters).pack(side='left', padx=4)
            ttk.Checkbutton(cr, text="Strip tags",
                            variable=opt_strip_tags).pack(side='left', padx=4)
            ttk.Checkbutton(cr, text="Strip existing subtitles",
                            variable=opt_strip_subs).pack(side='left', padx=4)
            ttk.Checkbutton(cr, text="Strip closed captions",
                            variable=opt_strip_cc).pack(side='left', padx=4)

            # ── Subtitles ──
            sub_fr = ttk.LabelFrame(fr, text="Subtitles", padding=6)
            sub_fr.grid(row=1, column=0, sticky='ew', **pad)
            sr = ttk.Frame(sub_fr)
            sr.pack(fill='x')
            ttk.Checkbutton(sr, text="Mux external subtitles",
                            variable=opt_mux_subs).pack(side='left', padx=(4, 8))
            _lang_lbl = ttk.Label(sr, text="Lang:")
            _lang_lbl.pack(side='left', padx=(8, 2))
            _lang_entry = ttk.Entry(sr, textvariable=opt_sub_lang, width=4)
            _lang_entry.pack(side='left', padx=(0, 4))
            ttk.Button(sr, text="Rescan", command=_rescan_subs, width=7).pack(side='left', padx=4)

            sr2 = ttk.Frame(sub_fr)
            sr2.pack(fill='x', pady=(4, 0))

            def _toggle_all_subs():
                if opt_all_subs.get():
                    _lang_entry.configure(state='disabled')
                    _lang_lbl.configure(foreground='gray')
                else:
                    _lang_entry.configure(state='normal')
                    _lang_lbl.configure(foreground='')

            ttk.Checkbutton(sr2, text="Include all subtitle languages",
                            variable=opt_all_subs,
                            command=_toggle_all_subs).pack(side='left', padx=(4, 8))
            _toggle_all_subs()  # apply initial state

            # ── Chapters ──
            ch_fr = ttk.LabelFrame(fr, text="Chapters", padding=6)
            ch_fr.grid(row=2, column=0, sticky='ew', **pad)
            chr_ = ttk.Frame(ch_fr)
            chr_.pack(fill='x')

            _ch_spin = tk.Spinbox(chr_, textvariable=opt_ch_interval,
                                  from_=1, to=60, width=3, state='disabled')

            def _toggle_ch():
                _ch_spin.configure(state='normal' if opt_add_chapters.get() else 'disabled')
                if opt_add_chapters.get():
                    opt_strip_chapters.set(False)

            ttk.Checkbutton(chr_, text="Add chapters every",
                            variable=opt_add_chapters,
                            command=_toggle_ch).pack(side='left', padx=(4, 2))
            _ch_spin.pack(side='left', padx=(0, 2))
            ttk.Label(chr_, text="min").pack(side='left')
            _toggle_ch()

            # ── Output ──
            out_fr = ttk.LabelFrame(fr, text="Output", padding=6)
            out_fr.grid(row=3, column=0, sticky='ew', **pad)

            or1 = ttk.Frame(out_fr)
            or1.pack(fill='x', pady=(0, 4))
            ttk.Radiobutton(or1, text="Replace in-place", variable=opt_output_mode,
                            value='inplace', command=lambda: _toggle_out()).pack(side='left', padx=(4, 8))
            ttk.Radiobutton(or1, text="Save to folder:", variable=opt_output_mode,
                            value='folder', command=lambda: _toggle_out()).pack(side='left', padx=(0, 4))
            _out_entry = ttk.Entry(or1, textvariable=opt_output_folder, width=20, state='disabled')
            _out_entry.pack(side='left', padx=(0, 4))
            _out_btn = ttk.Button(or1, text="Browse…", state='disabled',
                command=lambda: opt_output_folder.set(
                    ask_directory(title="Select Output Folder", parent=dlg) or opt_output_folder.get()))
            _out_btn.pack(side='left')

            or2 = ttk.Frame(out_fr)
            or2.pack(fill='x')
            ttk.Label(or2, text="Container:").pack(side='left', padx=(4, 2))
            ttk.Combobox(or2, textvariable=opt_container,
                         values=('.mkv', '.mp4'), width=5, state='readonly').pack(side='left')

            def _toggle_out():
                st = 'normal' if opt_output_mode.get() == 'folder' else 'disabled'
                _out_entry.configure(state=st)
                _out_btn.configure(state=st)
            _toggle_out()

            # ── Track Names ──
            tags_fr = ttk.LabelFrame(fr, text="Track Names", padding=6)
            tags_fr.grid(row=4, column=0, sticky='ew', **pad)
            tags_fr.columnconfigure(1, weight=1)

            _name_entries = []

            def _toggle_name_fields():
                st = 'normal' if opt_name_tracks.get() else 'disabled'
                for w in _name_entries:
                    w.configure(state=st)

            ttk.Checkbutton(tags_fr, text="Set track names from templates",
                            variable=opt_name_tracks,
                            command=_toggle_name_fields).grid(
                row=0, column=0, columnspan=2, sticky='w', padx=4, pady=(0, 4))

            ttk.Label(tags_fr, text="Video:").grid(row=1, column=0, sticky='w', padx=(4, 2), pady=1)
            _nv = ttk.Entry(tags_fr, textvariable=opt_name_video, width=36)
            _nv.grid(row=1, column=1, sticky='ew', padx=(0, 4), pady=1)
            _name_entries.append(_nv)

            ttk.Label(tags_fr, text="Audio:").grid(row=2, column=0, sticky='w', padx=(4, 2), pady=1)
            _na = ttk.Entry(tags_fr, textvariable=opt_name_audio, width=36)
            _na.grid(row=2, column=1, sticky='ew', padx=(0, 4), pady=1)
            _name_entries.append(_na)

            ttk.Label(tags_fr, text="Subtitle:").grid(row=3, column=0, sticky='w', padx=(4, 2), pady=1)
            _ns = ttk.Entry(tags_fr, textvariable=opt_name_sub, width=36)
            _ns.grid(row=3, column=1, sticky='ew', padx=(0, 4), pady=1)
            _name_entries.append(_ns)

            _toggle_name_fields()

            vars_text = "{lang}  {codec}  {channels}  {bitrate}  {flags}"
            ttk.Label(tags_fr, text=f"Variables: {vars_text}",
                      foreground='gray').grid(row=4, column=0, columnspan=2,
                                              sticky='w', padx=4, pady=(4, 0))

            # ── Processing ──
            proc_fr = ttk.LabelFrame(fr, text="Processing", padding=6)
            proc_fr.grid(row=5, column=0, sticky='ew', **pad)
            pr = ttk.Frame(proc_fr)
            pr.pack(fill='x')
            ttk.Checkbutton(pr, text="Parallel",
                            variable=opt_parallel).pack(side='left', padx=(4, 8))
            ttk.Label(pr, text="Jobs:").pack(side='left', padx=(0, 2))
            ttk.Spinbox(pr, textvariable=opt_max_jobs, from_=1, to=32,
                        width=3).pack(side='left')

            # ── Close button ──
            btn_fr = ttk.Frame(fr)
            btn_fr.grid(row=6, column=0, sticky='e', pady=(8, 0))
            ttk.Button(btn_fr, text="Close", command=dlg.destroy).pack(side='right')

        # ── Layout ──
        main_frame = ttk.Frame(win, padding=10)
        main_frame.pack(fill='both', expand=True)
        main_frame.columnconfigure(0, weight=1)
        main_frame.rowconfigure(1, weight=1)   # file list
        main_frame.rowconfigure(4, weight=1)   # log

        # ── Toolbar ──
        toolbar = ttk.Frame(main_frame)
        toolbar.grid(row=0, column=0, sticky='ew', pady=(0, 6))

        _scanning = [False]  # prevent overlapping scans

        def _add_files_threaded(file_paths, source_label="files"):
            """Probe and add a list of file paths in a background thread
            with progress shown in the existing progress bar."""
            if _scanning[0] or not file_paths:
                return
            # De-duplicate against already loaded files
            existing = {f['path'] for f in mp_files}
            to_add = [p for p in file_paths if p not in existing]
            if not to_add:
                return
            _scanning[0] = True
            total = len(to_add)

            def _worker():
                import time as _time
                start = _time.monotonic()
                added = 0
                for i, fp in enumerate(to_add):
                    # Progress update
                    elapsed = _time.monotonic() - start
                    rate = (i + 1) / elapsed if elapsed > 0.1 else 0
                    eta = f" — ETA {int((total - i - 1) / rate)}s" if rate > 0 else ""
                    pct = ((i + 1) / total) * 100
                    win.after(0, lambda p=pct: mp_progress_var.set(p))
                    win.after(0, lambda n=i+1, t=total, e=eta:
                              mp_progress_label.configure(
                                  text=f"Scanning {n}/{t}{e}"))
                    try:
                        _add_one_file(fp)
                        added += 1
                    except Exception:
                        pass
                    # Periodic tree refresh every 20 files
                    if added > 0 and added % 20 == 0:
                        win.after(0, _rebuild_tree)

                elapsed = _time.monotonic() - start
                win.after(0, _rebuild_tree)
                win.after(0, lambda: mp_progress_var.set(0))
                win.after(0, lambda: mp_progress_label.configure(text="Ready"))
                _log(f"Added {added} {source_label} ({elapsed:.1f}s)", 'INFO')
                _scanning[0] = False

            threading.Thread(target=_worker, daemon=True).start()

        def _add_files():
            paths = ask_open_files(
                parent=win,
                title="Select Video Files",
                filetypes=[("Video files", "*.mkv *.mp4 *.avi *.mov *.wmv *.flv *.webm *.ts *.m2ts *.mts"),
                           ("All files", "*.*")])
            if paths:
                _add_files_threaded(list(paths), "file(s)")

        def _add_folder():
            folder = ask_directory(title="Select Folder with Video Files", parent=win)
            if not folder:
                return
            # Collect paths first (fast), then probe in background
            file_paths = []
            for ext in VIDEO_EXTENSIONS:
                for fp in Path(folder).rglob(f'*{ext}'):
                    if fp.is_file() and not any(
                            part.startswith('.') for part in fp.relative_to(folder).parts):
                        file_paths.append(str(fp))
            if file_paths:
                _add_files_threaded(file_paths, "video file(s) from folder")

        # 2-letter → 3-letter language code mapping
        _lang2to3 = {
            'en': 'eng', 'es': 'spa', 'fr': 'fra', 'de': 'deu',
            'it': 'ita', 'pt': 'por', 'ru': 'rus', 'ja': 'jpn',
            'ko': 'kor', 'zh': 'zho', 'ar': 'ara', 'hi': 'hin',
            'nl': 'nld', 'pl': 'pol', 'sv': 'swe', 'tr': 'tur',
            'vi': 'vie',
        }
        # ISO 639-2/B (bibliographic) → ISO 639-2/T (terminological) mapping
        # Some languages have two 3-letter codes; subtitle files commonly use
        # the /B variant while we store the /T variant internally.
        _lang_alt3 = {
            'ger': 'deu', 'fre': 'fra', 'dut': 'nld', 'chi': 'zho',
            'cze': 'ces', 'gre': 'ell', 'rum': 'ron', 'per': 'fas',
            'mac': 'mkd', 'may': 'msa', 'bur': 'mya', 'tib': 'bod',
            'wel': 'cym', 'baq': 'eus', 'arm': 'hye', 'geo': 'kat',
            'ice': 'isl', 'alb': 'sqi',
        }

        # All known language codes for subtitle tag detection
        _ALL_LANG_CODES = set()
        for _code, _name in SUBTITLE_LANGUAGES:
            if _code != 'und':
                _ALL_LANG_CODES.add(_code)
        _ALL_LANG_CODES.update(_lang2to3.keys())        # 2-letter codes
        _ALL_LANG_CODES.update(_lang_alt3.keys())        # /B 3-letter alternates
        _TAG_FORCED = {'forced'}
        _TAG_SDH = {'sdh', 'hi', 'cc'}
        _SUB_EXTENSIONS = SUBTITLE_EXTENSIONS  # .srt .ass .ssa .vtt .sub .idx .sup

        def _normalize_lang(code):
            """Normalize a language code to its canonical 3-letter form."""
            code = code.lower()
            # 2-letter → 3-letter
            if code in _lang2to3:
                return _lang2to3[code]
            # /B alternate → /T canonical (e.g. ger → deu)
            if code in _lang_alt3:
                return _lang_alt3[code]
            return code

        def _detect_ext_subs(filepath):
            """Detect matching subtitle files alongside a video file.

            Uses fuzzy matching: finds all subtitle files in the same
            directory whose name starts with the video's base name, then
            parses the remaining tokens for language, forced, and SDH tags.

            Defaults: language='eng', type='main', sdh=False.

            Each result is a dict: {'path', 'lang', 'type', 'sdh'}.
            English subtitles are sorted first.
            """
            video_dir = os.path.dirname(filepath)
            video_stem = os.path.splitext(os.path.basename(filepath))[0]
            video_stem_lower = video_stem.lower()
            ext_subs_found = []
            seen_paths = set()

            # Scan all subtitle files in the same directory
            try:
                entries = os.listdir(video_dir)
            except OSError:
                return []

            for fname in entries:
                if fname.startswith('.'):
                    continue
                fpath = os.path.join(video_dir, fname)
                if not os.path.isfile(fpath):
                    continue
                name_lower = fname.lower()
                ext = os.path.splitext(name_lower)[1]
                if ext not in _SUB_EXTENSIONS:
                    continue

                # Check if this subtitle belongs to this video —
                # its name must start with the video's stem
                sub_stem = os.path.splitext(fname)[0]
                sub_stem_lower = sub_stem.lower()
                if not sub_stem_lower.startswith(video_stem_lower):
                    continue

                # Get the suffix after the video stem (e.g. ".eng.forced")
                suffix = sub_stem[len(video_stem):]

                # Parse suffix tokens for language, forced, sdh
                tokens = re.split(r'[\.\s_\-]+', suffix.lower())
                tokens = [t for t in tokens if t]  # drop empty

                lang = None
                is_forced = False
                is_sdh = False

                for tok in tokens:
                    if tok in _TAG_FORCED:
                        is_forced = True
                    elif tok in _TAG_SDH:
                        is_sdh = True
                    elif tok in _ALL_LANG_CODES and lang is None:
                        lang = _normalize_lang(tok)

                # Default language
                if lang is None:
                    lang = 'eng'

                # Filter by language preference (unless "all" mode)
                if not opt_all_subs.get():
                    target_lang = opt_sub_lang.get().strip() or 'eng'
                    target_norm = _normalize_lang(target_lang)
                    if lang != target_norm:
                        continue

                stype = 'forced' if is_forced else 'main'

                if fpath not in seen_paths:
                    seen_paths.add(fpath)
                    ext_subs_found.append({
                        'path': fpath, 'lang': lang,
                        'type': stype, 'sdh': is_sdh,
                    })

            # Sort: English first, then by language, forced/sdh after main
            def _sort_key(s):
                type_order = 0 if s['type'] == 'main' and not s['sdh'] else (
                    1 if s['type'] == 'forced' else 2)
                return (0 if s['lang'] == 'eng' else 1, s['lang'], type_order)
            ext_subs_found.sort(key=_sort_key)

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
            # Detect video codec and closed captions
            vcodec = get_video_codec(filepath) or ''
            has_cc = detect_closed_captions(filepath)

            mp_files.append({
                'path': filepath,
                'name': name,
                'size': size,
                'audio_info': audio,
                'audio_display': acodec,
                'sub_info': subs,
                'sub_count': len(subs),
                'ext_subs': ext_subs_found,
                'video_codec': vcodec,
                'has_cc': has_cc,
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
            if opt_all_subs.get():
                _log("Re-scanned subtitles for all languages", 'INFO')
            else:
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
        tree = ttk.Treeview(tree_frame, columns=columns, show='headings', height=6,
                            selectmode='extended')
        tree.grid(row=0, column=0, sticky='nsew')

        # ── Column sorting state ──
        mp_sort_col = [None]
        mp_sort_reverse = [False]
        mp_col_labels = {
            'name': 'Filename', 'audio': 'Audio', 'subs': 'Int Subs',
            'ext_subs': 'Ext Subs', 'size': 'Size', 'status': 'Status'
        }

        def _sort_by_column(col):
            if mp_sort_col[0] == col:
                mp_sort_reverse[0] = not mp_sort_reverse[0]
            else:
                mp_sort_col[0] = col
                mp_sort_reverse[0] = False

            def sort_key(f):
                if col == 'name':
                    return f.get('name', '').lower()
                elif col == 'audio':
                    return f.get('audio_display', '').lower()
                elif col == 'subs':
                    return f.get('sub_count', 0)
                elif col == 'ext_subs':
                    return len(f.get('ext_subs', []))
                elif col == 'size':
                    return f.get('size', 0)
                elif col == 'status':
                    return f.get('status', '').lower()
                return ''

            mp_files.sort(key=sort_key, reverse=mp_sort_reverse[0])
            _rebuild_tree()

            # Update headers with sort arrow
            arrow = ' ▼' if mp_sort_reverse[0] else ' ▲'
            for c, lbl in mp_col_labels.items():
                indicator = arrow if c == col else ''
                tree.heading(c, text=lbl + indicator)

        tree.heading('name',     text='Filename',  command=lambda: _sort_by_column('name'))
        tree.heading('audio',    text='Audio',     command=lambda: _sort_by_column('audio'))
        tree.heading('subs',     text='Int Subs',  command=lambda: _sort_by_column('subs'))
        tree.heading('ext_subs', text='Ext Subs',  command=lambda: _sort_by_column('ext_subs'))
        tree.heading('size',     text='Size',      command=lambda: _sort_by_column('size'))
        tree.heading('status',   text='Status',    command=lambda: _sort_by_column('status'))

        tree.column('name',     width=280, minwidth=150)
        tree.column('audio',    width=80,  minwidth=60,  anchor='center')
        tree.column('subs',     width=60,  minwidth=40,  anchor='center')
        tree.column('ext_subs', width=80,  minwidth=60,  anchor='center')
        tree.column('size',     width=80,  minwidth=60,  anchor='e')
        tree.column('status',   width=120, minwidth=80,  anchor='center')

        scrollbar = ttk.Scrollbar(tree_frame, orient='vertical', command=tree.yview)
        scrollbar.grid(row=0, column=1, sticky='ns')
        tree.configure(yscrollcommand=scrollbar.set)

        def _ext_sub_label(s):
            """Build a short display label for an external subtitle dict."""
            lang = s.get('lang', 'und')
            stype = s.get('type', 'main')
            sdh = s.get('sdh', False)
            parts = [lang]
            if stype == 'forced':
                parts.append('forced')
            if sdh:
                parts.append('sdh')
            return '.'.join(parts)

        def _rebuild_tree():
            tree.delete(*tree.get_children())
            for f in mp_files:
                size_mb = f'{f["size"] / (1024*1024):.1f} MB'
                ext_str = ', '.join(_ext_sub_label(s) for s in f['ext_subs']) if f['ext_subs'] else '—'
                name_display = f['name']
                if f.get('has_cc'):
                    name_display = 'CC ' + name_display
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
                ext_str = ', '.join(_ext_sub_label(s) for s in f['ext_subs']) if f['ext_subs'] else '—'
                name_display = f['name']
                if f.get('has_cc'):
                    name_display = 'CC ' + name_display
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

        # ── Shift+Arrow multi-select ──
        def _shift_arrow(evt, direction):
            items = tree.get_children()
            if not items:
                return 'break'
            sel = tree.selection()
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
            ctx.add_command(label="Media Details...",
                           command=lambda: _show_media_details(index))
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

        def _show_media_details(index):
            """Open the Enhanced Media Details dialog for the selected file."""
            filepath = mp_files[index]['path']
            try:
                from .media_info import show_enhanced_media_info
                show_enhanced_media_info(app, filepath, parent=win)
            except ImportError:
                try:
                    import importlib.util
                    _mi_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                            'media_info.py')
                    if os.path.exists(_mi_path):
                        spec = importlib.util.spec_from_file_location('media_info', _mi_path)
                        mod = importlib.util.module_from_spec(spec)
                        spec.loader.exec_module(mod)
                        mod.show_enhanced_media_info(app, filepath, parent=win)
                    else:
                        messagebox.showerror("Media Details",
                                             "modules/media_info.py not found.")
                except Exception as e:
                    messagebox.showerror("Media Details", f"Could not open Media Details:\n{e}")

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
            f['sub_info'] = subs
            f['sub_count'] = len(subs)
            f['size'] = os.path.getsize(f['path']) if os.path.exists(f['path']) else 0
            f['ext_subs'] = _detect_ext_subs(f['path'])
            f['video_codec'] = get_video_codec(f['path']) or ''
            f['has_cc'] = detect_closed_captions(f['path'])
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
            v_strip_cc = tk.BooleanVar(value=ov.get('strip_cc', opt_strip_cc.get()))
            cf = ttk.Frame(fr)
            cf.grid(row=row, column=0, columnspan=2, sticky='w', **pad); row += 1
            ttk.Checkbutton(cf, text="Strip chapters", variable=v_strip_ch).pack(side='left', padx=4)
            ttk.Checkbutton(cf, text="Strip tags", variable=v_strip_tg).pack(side='left', padx=4)
            ttk.Checkbutton(cf, text="Strip subs", variable=v_strip_sb).pack(side='left', padx=4)
            ttk.Checkbutton(cf, text="Strip CC", variable=v_strip_cc).pack(side='left', padx=4)

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
                    'strip_cc': v_strip_cc.get(),
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

            subs = [dict(s) for s in f['ext_subs']]  # work on a deep copy

            fr = ttk.Frame(dlg, padding=12)
            fr.pack(fill='both', expand=True)
            fr.columnconfigure(0, weight=1)
            fr.rowconfigure(0, weight=1)

            sub_list = tk.Listbox(fr, height=6)
            sub_list.grid(row=0, column=0, sticky='nsew')

            def _refresh():
                sub_list.delete(0, 'end')
                for s in subs:
                    lang = s.get('lang', 'und')
                    lang_name = LANG_CODE_TO_NAME.get(lang, lang)
                    stype = s.get('type', 'main')
                    sdh = ' SDH' if s.get('sdh') else ''
                    sub_list.insert('end',
                        f"[{lang_name} — {stype}{sdh}] {os.path.basename(s['path'])}")

            def _add_sub():
                paths = ask_open_files(
                    parent=win,
                    title="Select Subtitle Files",
                    filetypes=[("Subtitle files", "*.srt *.ass *.ssa *.vtt *.sub"),
                               ("All files", "*.*")])
                for p in paths:
                    # Detect language from filename tokens
                    stem_tokens = Path(p).stem.lower().split('.')
                    lang = opt_sub_lang.get().strip() or 'eng'
                    for token in reversed(stem_tokens[1:]):
                        if token in _lang2to3:
                            lang = _lang2to3[token]
                            break
                        elif token in _lang_alt3:
                            lang = _lang_alt3[token]
                            break
                        elif any(token == lc for lc, _ in SUBTITLE_LANGUAGES):
                            lang = token
                            break
                    # Detect type/flags from filename
                    stype = 'forced' if 'forced' in stem_tokens else 'main'
                    sdh = 'sdh' in stem_tokens or 'cc' in stem_tokens or 'hi' in stem_tokens
                    subs.append({
                        'path': p, 'lang': lang,
                        'type': stype, 'sdh': sdh,
                    })
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
                    s = subs[i]
                    # Cycle: main → forced → main (sdh) → forced
                    if s['type'] == 'main' and not s.get('sdh'):
                        s['type'] = 'forced'
                    elif s['type'] == 'forced':
                        s['type'] = 'main'
                        s['sdh'] = True
                    else:
                        s['type'] = 'main'
                        s['sdh'] = False
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
            ttk.Button(btn_fr, text="Toggle Type", command=_toggle_type).pack(side='left', padx=2)
            ttk.Separator(btn_fr, orient='vertical').pack(side='left', fill='y', padx=6)
            ttk.Button(btn_fr, text="⬆ Up", command=_move_up, width=4).pack(side='left', padx=2)
            ttk.Button(btn_fr, text="⬇ Down", command=_move_down, width=5).pack(side='left', padx=2)

            bot = ttk.Frame(dlg, padding=(12,0,12,12))
            bot.pack(fill='x')
            def _save_subs():
                f['ext_subs'] = [dict(s) for s in subs]
                _rebuild_tree()
                _log(f"Subtitles updated for: {f['name']} ({len(subs)} file(s))", 'INFO')
                dlg.destroy()
            ttk.Button(bot, text="Save", command=_save_subs).pack(side='right', padx=(4,0))
            ttk.Button(bot, text="Cancel", command=dlg.destroy).pack(side='right')
            _refresh()

        # Double-click opens Media Details
        def _on_double_click(event):
            item = tree.identify_row(event.y)
            if item and not mp_processing[0]:
                items = tree.get_children()
                index = list(items).index(item)
                _show_media_details(index)
        tree.bind('<Double-1>', _on_double_click)

        # ── Operations panel ──
        ops_frame = ttk.LabelFrame(main_frame, text="Operations", padding=8)
        ops_frame.grid(row=2, column=0, sticky='ew', pady=(0, 6))

        # Row 1: Audio conversion
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
        mp_br_combo.pack(side='left')

        # Row 2: Track metadata
        ops_row2 = ttk.Frame(ops_frame)
        ops_row2.pack(fill='x', pady=2)

        def _toggle_meta_fields():
            st = 'normal' if opt_set_metadata.get() else 'disabled'
            mp_mv.configure(state=st)
            mp_ma.configure(state=st)
            mp_ms.configure(state=st)

        ttk.Checkbutton(ops_row2, text="Set track metadata:",
                       variable=opt_set_metadata, command=_toggle_meta_fields).pack(side='left', padx=(0, 4))
        ttk.Label(ops_row2, text="V:").pack(side='left')
        mp_mv = ttk.Entry(ops_row2, textvariable=opt_meta_video, width=4)
        mp_mv.pack(side='left', padx=(2, 6))
        ttk.Label(ops_row2, text="A:").pack(side='left')
        mp_ma = ttk.Entry(ops_row2, textvariable=opt_meta_audio, width=4)
        mp_ma.pack(side='left', padx=(2, 6))
        ttk.Label(ops_row2, text="S:").pack(side='left')
        mp_ms = ttk.Entry(ops_row2, textvariable=opt_meta_sub, width=4)
        mp_ms.pack(side='left', padx=(2, 0))

        _toggle_meta_fields()
        _toggle_audio_controls()

        # Row 3: Edition tagging
        ops_row3 = ttk.Frame(ops_frame)
        ops_row3.pack(fill='x', pady=2)

        ttk.Label(ops_row3, text="Edition:").pack(side='left', padx=(0, 2))
        mp_edition_combo = ttk.Combobox(ops_row3, textvariable=opt_edition_tag,
                                         values=EDITION_PRESETS, width=18, state='readonly')
        mp_edition_combo.pack(side='left', padx=(0, 4))

        mp_edition_custom = ttk.Entry(ops_row3, textvariable=_edition_custom_sv, width=18)

        if opt_edition_tag.get() and opt_edition_tag.get() not in EDITION_PRESETS:
            _edition_custom_sv.set(opt_edition_tag.get())
            mp_edition_combo.set('Custom...')
            mp_edition_custom.pack(side='left', padx=(0, 4))  # show custom entry on load

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

        ttk.Checkbutton(ops_row3, text="Plex",
                        variable=opt_edition_fn).pack(side='left', padx=(4, 0))

        # ── Progress bar ──
        # Pack layout: buttons reserved on right first, progress bar fills remainder
        progress_frame = ttk.Frame(main_frame)
        progress_frame.grid(row=3, column=0, sticky='ew', pady=(0, 6))

        mp_progress_var = tk.DoubleVar(value=0)

        # Action buttons — pack right first so they're always visible
        btn_frame = ttk.Frame(progress_frame)
        btn_frame.pack(side='right', padx=(8, 0))
        process_btn = ttk.Button(btn_frame, text="▶ Process All", command=lambda: _start_processing())
        process_btn.pack(side='left', padx=2)
        stop_btn = ttk.Button(btn_frame, text="⏹ Stop", command=lambda: _stop_processing(), state='disabled')
        stop_btn.pack(side='left', padx=2)

        # Label and progress bar fill remaining space
        mp_progress_label = ttk.Label(progress_frame, text="Ready")
        mp_progress_label.pack(side='left', padx=(0, 8))
        mp_progress_bar = ttk.Progressbar(progress_frame, variable=mp_progress_var,
                                          maximum=100, mode='determinate')
        mp_progress_bar.pack(side='left', fill='x', expand=True)

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
        log_text.tag_configure('FILENAME', foreground='#6aff6a')

        def _log(msg, level='INFO', filename=None):
            def _do():
                log_text.configure(state='normal')
                ts = datetime.now().strftime('%H:%M:%S')
                if filename:
                    # Insert prefix, then filename in green, then remainder
                    prefix = f"[{ts}] [{level}] "
                    # Split msg around the filename
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
                for s in f['ext_subs']:
                    cmd.extend(['-i', s['path']])
                    sub_inputs.append(s)

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
            for idx, s in enumerate(sub_inputs):
                input_idx = 1 + idx
                cmd.extend(['-map', f'{input_idx}:0'])

            # ── Video: always copy (remux only) ──
            cmd.extend(['-c:v', 'copy'])

            # ── Strip closed captions (EIA-608/CEA-708 in video SEI) ──
            do_strip_cc = _ov(f, 'strip_cc', opt_strip_cc)
            if do_strip_cc and f.get('has_cc'):
                vcodec = f.get('video_codec', '')
                bsf = CC_STRIP_BSF.get(vcodec)
                if bsf:
                    cmd.extend(['-bsf:v', bsf])
                    _log(f"  Stripping closed captions ({vcodec})", 'INFO')

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
                    # Compare source bitrate against target bitrate
                    src_br_raw = audio[0].get('bit_rate', '') if audio else ''
                    try:
                        src_kbps = int(src_br_raw) // 1000 if src_br_raw else 0
                    except (ValueError, TypeError):
                        src_kbps = 0
                    # Parse target bitrate (e.g. '384k' → 384)
                    tgt_str = audio_bitrate.lower().rstrip('k')
                    try:
                        tgt_kbps = int(tgt_str)
                    except (ValueError, TypeError):
                        tgt_kbps = 0
                    # Skip transcoding only if codec AND bitrate match
                    # (10% tolerance for bitrate comparison)
                    bitrate_matches = (
                        src_kbps == 0 or tgt_kbps == 0
                        or abs(src_kbps - tgt_kbps) <= tgt_kbps * 0.10
                    )
                    if src_codec in match_set and bitrate_matches:
                        cmd.extend(['-c:a', 'copy'])
                        _log(f"  Audio: already {src_codec.upper()}"
                             f" @ {src_kbps}k, copying", 'SKIP')
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
            do_name_tracks = opt_name_tracks.get()
            s_lang = _ov(f, 'meta_sub', opt_meta_sub)
            eng_default_set = False  # track whether we've set an English default
            for idx, s in enumerate(sub_inputs):
                si = out_sub_idx + idx
                sub_codec = 'mov_text' if out_ext == '.mp4' else 'srt'
                cmd.extend([f'-c:s:{si}', sub_codec])
                stype = s.get('type', 'main')
                sdh = s.get('sdh', False)
                # Use per-subtitle language; fall back to metadata override
                sub_lang = s.get('lang', 'eng')
                if do_set_meta and s.get('lang', 'eng') == (opt_sub_lang.get().strip() or 'eng'):
                    sub_lang = s_lang  # apply metadata language override for primary lang
                cmd.extend([f'-metadata:s:s:{si}', f'language={sub_lang}'])
                if do_name_tracks and opt_name_sub.get():
                    sub_tpl_info = {
                        'language': sub_lang,
                        'codec_name': 'subrip',
                        'forced': stype == 'forced',
                        'sdh': sdh,
                    }
                    title = _resolve_track_name(opt_name_sub.get(), sub_tpl_info)
                    if title:
                        cmd.extend([f'-metadata:s:s:{si}', f'title={title}'])
                else:
                    lang_name = LANG_CODE_TO_NAME.get(sub_lang, sub_lang)
                    if stype == 'forced':
                        cmd.extend([f'-metadata:s:s:{si}', f'title={lang_name} - Forced'])
                    elif sdh:
                        cmd.extend([f'-metadata:s:s:{si}', f'title={lang_name} - SDH'])
                    else:
                        cmd.extend([f'-metadata:s:s:{si}', f'title={lang_name}'])
                # Disposition: English main (non-SDH) gets default; forced gets forced;
                # SDH gets hearing_impaired
                disp_parts = []
                if stype == 'main' and not sdh and sub_lang == 'eng' and not eng_default_set:
                    disp_parts.append('default')
                    eng_default_set = True
                if stype == 'forced':
                    disp_parts.append('forced')
                if sdh:
                    disp_parts.append('hearing_impaired')
                if disp_parts:
                    cmd.extend([f'-disposition:s:{si}', '+'.join(disp_parts)])

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
                # Video track(s): language + title
                cmd.extend(['-metadata:s:v:0', f'language={v_lang}'])
                if do_name_tracks and opt_name_video.get():
                    v_info = {'language': v_lang, 'codec_name': '', 'channels': 0}
                    v_title = _resolve_track_name(opt_name_video.get(), v_info)
                    cmd.extend(['-metadata:s:v:0', f'title={v_title}'])
                else:
                    cmd.extend(['-metadata:s:v:0', 'title='])
                # Audio track(s): language + title per stream
                audio_streams = f.get('audio_info') or []
                if audio_streams:
                    for ai, ainfo in enumerate(audio_streams):
                        cmd.extend([f'-metadata:s:a:{ai}', f'language={a_lang}'])
                        if do_name_tracks and opt_name_audio.get():
                            a_title = _resolve_track_name(opt_name_audio.get(), ainfo)
                            cmd.extend([f'-metadata:s:a:{ai}', f'title={a_title}'])
                        else:
                            cmd.extend([f'-metadata:s:a:{ai}', 'title='])
                else:
                    cmd.extend(['-metadata:s:a:0', f'language={a_lang}', '-metadata:s:a:0', 'title='])
                # Internal subtitle track(s): language + title per stream
                if not do_strip_subs and f.get('sub_count', 0) > 0:
                    int_subs = f.get('sub_info') or []
                    if not sub_inputs:
                        for si_idx, sinfo in enumerate(int_subs):
                            cmd.extend([f'-metadata:s:s:{si_idx}', f'language={s_lang}'])
                            if do_name_tracks and opt_name_sub.get():
                                s_title = _resolve_track_name(opt_name_sub.get(), sinfo)
                                cmd.extend([f'-metadata:s:s:{si_idx}', f'title={s_title}'])
                        if not int_subs:
                            cmd.extend([f'-metadata:s:s:0', f'language={s_lang}'])
            elif do_name_tracks:
                # Track naming without full metadata set — apply titles only
                # Video
                if opt_name_video.get():
                    v_info = {'language': 'und', 'codec_name': '', 'channels': 0}
                    v_title = _resolve_track_name(opt_name_video.get(), v_info)
                    if v_title:
                        cmd.extend(['-metadata:s:v:0', f'title={v_title}'])
                # Audio
                if opt_name_audio.get():
                    audio_streams = f.get('audio_info') or []
                    for ai, ainfo in enumerate(audio_streams):
                        a_title = _resolve_track_name(opt_name_audio.get(), ainfo)
                        if a_title:
                            cmd.extend([f'-metadata:s:a:{ai}', f'title={a_title}'])
                # Internal subtitles
                if opt_name_sub.get() and not do_strip_subs:
                    int_subs = f.get('sub_info') or []
                    for si_idx, sinfo in enumerate(int_subs):
                        s_title = _resolve_track_name(opt_name_sub.get(), sinfo)
                        if s_title:
                            cmd.extend([f'-metadata:s:s:{si_idx}', f'title={s_title}'])

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

                import re as _re
                import time as _time
                last_lines = []
                last_update = [0.0]
                duration = f.get('duration_secs') or 0
                line_buf = []

                while True:
                    ch = proc.stdout.read(1)
                    if not ch:
                        break
                    if ch in ('\r', '\n'):
                        line = ''.join(line_buf).strip()
                        line_buf = []
                        if not line:
                            continue
                        if mp_stop[0]:
                            proc.terminate()
                            return ('stopped', i)
                        last_lines.append(line)
                        if len(last_lines) > 5:
                            last_lines.pop(0)

                        # Parse progress
                        if duration > 0:
                            time_match = _re.search(r'time=(\d+:\d+:\d+\.\d+)', line)
                            if time_match:
                                now = _time.monotonic()
                                if now - last_update[0] >= 0.3:
                                    last_update[0] = now
                                    parts = time_match.group(1).split(':')
                                    cur = int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
                                    file_pct = min(99.9, (cur / duration) * 100)
                                    batch_pct = ((mp_batch_done[0] + file_pct / 100) / max(1, mp_batch_total[0])) * 100
                                    win.after(0, lambda p=batch_pct: mp_progress_var.set(p))
                                    win.after(0, lambda p=file_pct: mp_progress_label.configure(
                                        text=f"File {mp_batch_done[0] + 1}/{mp_batch_total[0]}: {p:.0f}%"))
                                    win.after(0, lambda p=file_pct: _update_tree_status(i, f'{p:.0f}%'))
                    else:
                        line_buf.append(ch)

                proc.wait()

                if proc.returncode == 0:
                    # In-place mode: replace original
                    is_inplace = opt_output_mode.get() == 'inplace' or not opt_output_folder.get()
                    # Compute edition filename part for rename
                    _ed = _ov(f, 'edition_tag', opt_edition_tag)
                    if isinstance(_ed, tk.StringVar):
                        _ed = _ed.get()
                    _ed_fn = _ov(f, 'edition_in_filename', opt_edition_fn)
                    if isinstance(_ed_fn, tk.BooleanVar):
                        _ed_fn = _ed_fn.get()
                    _edition_fn_part = (' {edition-' + _ed + '}') if (_ed and _ed_fn) else ''

                    if is_inplace:
                        original = f['path']
                        if _edition_fn_part:
                            # Rename to include edition tag in filename
                            orig_base, orig_ext = os.path.splitext(original)
                            _ext = opt_container.get() or orig_ext
                            new_name = orig_base + _edition_fn_part + _ext
                            try:
                                os.replace(out_path, new_name)
                                final_path = new_name
                                if os.path.normpath(new_name) != os.path.normpath(original):
                                    try:
                                        os.remove(original)
                                    except OSError:
                                        pass
                                _log(f"  Renamed to: {os.path.basename(new_name)}", 'INFO')
                            except OSError as e:
                                _log(f"  Warning: could not rename: {e}", 'WARNING')
                                try:
                                    os.replace(out_path, original)
                                except OSError:
                                    pass
                                final_path = original
                        else:
                            try:
                                os.replace(out_path, original)
                            except OSError as e:
                                _log(f"  Warning: could not replace original: {e}", 'WARNING')
                            final_path = original
                    else:
                        final_path = out_path

                    # Strip MKV Tags (DURATION, BPS, etc.) that ffmpeg's
                    # Matroska muxer writes automatically on every remux.
                    # -map_metadata -1 only strips metadata, not MKV Tag
                    # elements — mkvpropedit is needed to remove them.
                    if (_ov(f, 'strip_tags', opt_strip_tags)
                            and final_path.lower().endswith('.mkv')
                            and shutil.which('mkvpropedit')):
                        try:
                            _mkv_r = subprocess.run(
                                ['mkvpropedit', final_path, '--tags', 'all:'],
                                capture_output=True, text=True, timeout=30)
                            if _mkv_r.returncode == 0:
                                _log("  Stripped MKV tags (mkvpropedit)", 'INFO')
                            else:
                                _log(f"  mkvpropedit warning: {_mkv_r.stderr.strip()}", 'WARNING')
                        except Exception as _e:
                            _log(f"  mkvpropedit error: {_e}", 'WARNING')

                    # Track whether subtitles were muxed for this file
                    do_mux = _ov(f, 'mux_subs', opt_mux_subs)
                    if do_mux and f.get('ext_subs'):
                        f['_subs_muxed'] = True

                    out_size = os.path.getsize(final_path) if os.path.exists(final_path) else 0
                    out_mb = f'{out_size / (1024*1024):.1f} MB' if out_size else '?'
                    final_name = os.path.basename(final_path)
                    _log(f"  ✅ Done — {final_name} ({out_mb})", 'SUCCESS',
                         filename=final_name)
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
            mp_batch_total[0] = total
            mp_batch_done[0] = 0
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
                    mp_batch_done[0] = processed_count[0]
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

            # Clean up subtitle files only for files that had subs muxed
            if completed[0] > 0:
                cleaned = 0
                for f in mp_files:
                    if f.get('_subs_muxed') and f['status'] == '✅ Done':
                        for s in f.get('ext_subs', []):
                            spath = s['path']
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

            # Play completion notification sound
            if completed[0] > 0 and not mp_stop[0]:
                _play_notification_sound()

            win.after(0, lambda c=completed[0], t=total: messagebox.showinfo(
                "Processing Complete",
                f"{c} of {t} file(s) processed successfully."
                + (f"\n{failed[0]} failed." if failed[0] else ""),
                parent=win))

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
                raw = event.data.strip()
                # Parse dropped paths — three formats:
                # 1. file:// URIs separated by \r\n or spaces
                # 2. Tcl list: {/path with spaces/file.mkv} {/another/file.mkv}
                # 3. Plain space-separated paths: /path/file1.mkv /path/file2.mkv
                paths = []
                if 'file://' in raw:
                    from urllib.parse import unquote, urlparse
                    for token in re.split(r'[\r\n\s]+', raw):
                        token = token.strip()
                        if token.startswith('file://'):
                            parsed = urlparse(token)
                            paths.append(unquote(parsed.path))
                else:
                    # Tcl list format handles both braced and unbraced paths
                    # {/path with spaces/file.mkv} becomes one token
                    # /path/file.mkv becomes one token
                    paths = [p.strip('{}') for p in re.findall(r'\{[^}]+\}|[^\s]+', raw)]

                # Collect all file paths first (fast), then probe in background
                file_paths = []
                for p in paths:
                    if os.path.isfile(p) and os.path.splitext(p)[1].lower() in VIDEO_EXTENSIONS:
                        file_paths.append(p)
                    elif os.path.isdir(p):
                        for ext in VIDEO_EXTENSIONS:
                            for fp in Path(p).rglob(f'*{ext}'):
                                if fp.is_file() and not any(
                                        part.startswith('.') for part in fp.relative_to(p).parts):
                                    file_paths.append(str(fp))
                if file_paths:
                    _add_files_threaded(file_paths, "file(s) via drag-and-drop")

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
                'strip_cc':       opt_strip_cc.get(),
                'set_metadata':   opt_set_metadata.get(),
                'meta_video':     opt_meta_video.get(),
                'meta_audio':     opt_meta_audio.get(),
                'meta_sub':       opt_meta_sub.get(),
                'mux_subs':       opt_mux_subs.get(),
                'sub_lang':       opt_sub_lang.get(),
                'all_subs':       opt_all_subs.get(),
                'output_mode':    opt_output_mode.get(),
                'output_folder':  opt_output_folder.get(),
                'container':      opt_container.get(),
                'parallel':       opt_parallel.get(),
                'max_jobs':       opt_max_jobs.get(),
                'edition_tag':    opt_edition_tag.get(),
                'edition_in_filename': opt_edition_fn.get(),
                'add_chapters':   opt_add_chapters.get(),
                'chapter_interval': opt_ch_interval.get(),
                'name_tracks':    opt_name_tracks.get(),
                'name_video':     opt_name_video.get(),
                'name_audio':     opt_name_audio.get(),
                'name_sub':       opt_name_sub.get(),
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

        # Force Tk to calculate geometry and render all widgets — prevents
        # invisible/blank controls on high-DPI displays until mouse-over
        win.update_idletasks()

        _log("Docflix Media Processor ready — add files and click Process All", 'INFO')
        _log("Tip: drag and drop video files onto this window", 'INFO')
        if opt_all_subs.get():
            _log("Subtitle matching: all languages (*.eng.srt, *.deu.srt, etc.)", 'INFO')
        else:
            _log(f"Subtitle matching: *.{opt_sub_lang.get()}.srt / *.{opt_sub_lang.get()}.forced.srt", 'INFO')

    # ── TV Show Renamer ────────────────────────────────────────────────────


def main():
    """Launch Media Processor as a standalone application."""
    from .standalone import create_standalone_root

    root, app = create_standalone_root(
        title="Docflix Media Processor",
        geometry="920x880",
        minsize=(750, 650),
    )

    app._standalone_mode = True
    root.withdraw()
    open_media_processor(app)

    root.mainloop()


if __name__ == '__main__':
    main()
