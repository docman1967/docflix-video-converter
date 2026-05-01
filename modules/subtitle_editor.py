"""
Docflix Video Converter — Subtitle Editor

Full-featured subtitle editor with inline text editing,
filters, timing tools, search/replace, spell check,
Smart Sync, and OCR support.

Contains two editor variants:
  - open_standalone_subtitle_editor(app): Independent editor
    window accessible from Tools menu or standalone launch.
  - show_subtitle_editor(app, ...): Internal editor for
    editing subtitles attached to a conversion queue file.
"""

import json
import os
import re
import shutil
from pathlib import Path
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from .constants import (
    VIDEO_EXTENSIONS, SUBTITLE_EXTENSIONS, BITMAP_SUB_CODECS,
    MAX_CHARS_PER_LINE,
)
from .utils import (
    create_tooltip, get_subtitle_streams, get_video_duration,
    scaled_geometry, scaled_minsize,
)
from .subtitle_filters import (
    parse_srt, write_srt, srt_ts_to_ms, ms_to_srt_ts,
    filter_remove_hi, filter_remove_caps_hi,
    filter_remove_music_notes, filter_fix_caps,
    filter_remove_tags, filter_remove_ads,
    filter_remove_offscreen_quotes,
    filter_remove_leading_dashes,
    filter_remove_duplicates, filter_merge_short,
    filter_reduce_lines,
    shift_timestamps, stretch_timestamps, two_point_sync,
    BUILTIN_AD_PATTERNS,
)
from .smart_sync import smart_sync
from .waveform_timeline import WaveformTimeline

try:
    from tkinterdnd2 import DND_FILES
    HAS_DND = True
except ImportError:
    HAS_DND = False


def open_standalone_subtitle_editor(app):
        import tempfile

        editor = tk.Toplevel(app.root)
        editor.title("Subtitle Editor")
        editor.geometry(scaled_geometry(editor, 950, 650))
        editor.minsize(*scaled_minsize(editor, 700, 500))
        editor.resizable(True, True)
        app._center_on_main(editor)

        # ── Shared mutable state ──
        cues = []
        original_cues = []
        undo_stack = []
        redo_stack = []
        current_path = [None]  # mutable ref for current file path
        video_source = [None]  # set when editing a subtitle from a video file
        # When set: {'path': video_path, 'stream_index': N, 'temp_srt': path,
        #            'streams': [...], 'stream_info': {...}}

        # ── Color tag names ──
        TAG_MODIFIED = 'modified'
        TAG_HI = 'has_hi'
        TAG_TAGS = 'has_tags'
        TAG_LONG = 'long_line'
        TAG_SEARCH = 'search_match'
        TAG_SPELL = 'has_spelling'

        # ── Spell check state ──
        spell_error_indices = set()

        # ── Undo / Redo ──
        def push_undo():
            undo_stack.append([dict(c) for c in cues])
            redo_stack.clear()

        def do_undo(event=None):
            nonlocal cues
            if not undo_stack:
                return
            redo_stack.append([dict(c) for c in cues])
            cues = undo_stack.pop()
            refresh_tree(cues)

        def do_redo(event=None):
            nonlocal cues
            if not redo_stack:
                return
            undo_stack.append([dict(c) for c in cues])
            cues = redo_stack.pop()
            refresh_tree(cues)

        editor.bind('<Control-z>', do_undo)
        editor.bind('<Control-y>', do_redo)
        editor.bind('<Control-Z>', do_undo)
        editor.bind('<Control-Y>', do_redo)

        # ── Track state ──
        modified_count = tk.IntVar(value=0)
        deleted_count = tk.IntVar(value=0)

        # ── Classification for color coding ──
        _orig_texts = set()

        def _classify_cue(cue, orig_text=None):
            tags = set()
            text = cue['text']
            if orig_text is not None and text != orig_text:
                tags.add(TAG_MODIFIED)
            if re.search(r'\[.*?\]|\(.*?\)|♪|♫', text):
                tags.add(TAG_HI)
            if re.search(r'<[^>]+>|\{\\[^}]+\}', text):
                tags.add(TAG_TAGS)
            for line in text.split('\n'):
                if len(line) > MAX_CHARS_PER_LINE:
                    tags.add(TAG_LONG)
                    break
            return tags

        # ══════════════════════════════════════════════════════════════════════
        # Menu bar
        # ══════════════════════════════════════════════════════════════════════
        menubar = tk.Menu(editor)
        editor.configure(menu=menubar)

        # ── File menu ──
        file_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="File", menu=file_menu)

        def load_file(sub_path):
            """Load a subtitle file into the editor by path."""
            sub_exts = {'.srt', '.ass', '.ssa', '.vtt', '.sub'}
            ext = Path(sub_path).suffix.lower()
            if ext not in sub_exts:
                messagebox.showwarning("Unsupported Format",
                    f"Not a recognised subtitle file:\n{os.path.basename(sub_path)}",
                    parent=editor)
                return
            if ext in ('.srt',):
                try:
                    with open(sub_path, 'r', encoding='utf-8', errors='replace') as f:
                        srt_text = f.read()
                except Exception as e:
                    messagebox.showerror("Error", f"Failed to read file:\n{e}",
                                         parent=editor)
                    return
            else:
                # Convert to SRT via ffmpeg
                tmp_srt = tempfile.NamedTemporaryFile(suffix='.srt', delete=False,
                                                       mode='w', encoding='utf-8')
                tmp_srt.close()
                cmd = ['ffmpeg', '-y', '-i', sub_path, '-c:s', 'srt', tmp_srt.name]
                try:
                    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                    if result.returncode != 0:
                        messagebox.showerror("Error",
                            f"Failed to convert subtitle:\n{result.stderr[-300:]}",
                            parent=editor)
                        os.unlink(tmp_srt.name)
                        return
                except Exception as e:
                    messagebox.showerror("Error", f"Convert error:\n{e}", parent=editor)
                    os.unlink(tmp_srt.name)
                    return
                with open(tmp_srt.name, 'r', encoding='utf-8', errors='replace') as f:
                    srt_text = f.read()
                os.unlink(tmp_srt.name)

            title = f"Subtitle Editor — {os.path.basename(sub_path)}"
            if _load_cues_into_editor(srt_text, title, sub_path):
                app.add_log(f"Opened subtitle file: {os.path.basename(sub_path)} "
                             f"({len(cues)} entries)", 'INFO')

        def _load_cues_into_editor(srt_text, title, source_path):
            """Common logic: parse SRT text and load cues into the editor."""
            nonlocal cues, original_cues
            new_cues = parse_srt(srt_text)
            if not new_cues:
                messagebox.showwarning("Empty",
                    f"No subtitle cues found in:\n{os.path.basename(source_path)}",
                    parent=editor)
                return False

            cues = new_cues
            original_cues = [dict(c) for c in cues]
            _orig_texts.clear()
            _orig_texts.update(c['text'] for c in original_cues)
            undo_stack.clear()
            redo_stack.clear()
            current_path[0] = source_path
            editor.title(title)

            placeholder.pack_forget()
            content_frame.pack(fill='both', expand=True)
            _set_menus_state('normal')
            refresh_tree(cues)
            # Scroll to top for newly loaded file
            tree.yview_moveto(0)
            return True

        def load_video_subtitle(video_path):
            """Probe a video file for subtitle streams, let the user pick one,
            extract it, and load it into the editor for editing."""
            nonlocal cues, original_cues

            # Bitmap codecs that can't be converted to SRT
            BITMAP_CODECS = {'hdmv_pgs_subtitle', 'dvd_subtitle', 'dvb_subtitle',
                             'dvb_teletext', 'xsub'}

            streams = get_subtitle_streams(video_path)
            if not streams:
                messagebox.showinfo("No Subtitles",
                    f"No subtitle streams found in:\n{os.path.basename(video_path)}",
                    parent=editor)
                return

            # Filter out bitmap subtitles
            text_streams = [s for s in streams if s['codec_name'] not in BITMAP_CODECS]
            if not text_streams:
                messagebox.showwarning("No Editable Subtitles",
                    "This video only contains bitmap subtitle streams "
                    "(PGS/VobSub) which cannot be edited as text.",
                    parent=editor)
                return

            # If only one text stream, use it directly; otherwise show picker
            if len(text_streams) == 1:
                chosen = text_streams[0]
            else:
                chosen = [None]  # mutable ref for dialog result

                picker = tk.Toplevel(editor)
                picker.title("Select Subtitle Stream")
                picker.geometry("640x340")
                picker.transient(editor)
                picker.grab_set()
                app._center_on_main(picker)
                picker.resizable(True, True)

                ttk.Label(picker,
                          text=f"Select a subtitle stream to edit from:\n"
                               f"{os.path.basename(video_path)}",
                          padding=(10, 10)).pack()

                # ── Treeview with columns ──
                tree_frame = ttk.Frame(picker)
                tree_frame.pack(fill='both', expand=True, padx=10, pady=5)

                scrollbar = ttk.Scrollbar(tree_frame, orient='vertical')
                scrollbar.pack(side='right', fill='y')

                cols = ('stream', 'lang', 'format', 'title', 'flags')
                stream_tree = ttk.Treeview(tree_frame, columns=cols,
                                           show='headings', height=8,
                                           selectmode='browse',
                                           yscrollcommand=scrollbar.set)
                scrollbar.config(command=stream_tree.yview)

                stream_tree.heading('stream', text='#')
                stream_tree.heading('lang', text='Language')
                stream_tree.heading('format', text='Format')
                stream_tree.heading('title', text='Title')
                stream_tree.heading('flags', text='Flags')

                stream_tree.column('stream', width=50, minwidth=40, stretch=False)
                stream_tree.column('lang', width=90, minwidth=70, stretch=False)
                stream_tree.column('format', width=70, minwidth=60, stretch=False)
                stream_tree.column('title', width=200, minwidth=100, stretch=True)
                stream_tree.column('flags', width=140, minwidth=100, stretch=False)

                stream_tree.pack(fill='both', expand=True)

                for i, s in enumerate(text_streams):
                    lang = s['language'] if s['language'] != 'und' else 'Unknown'
                    flags = []
                    if s['default']:
                        flags.append('Default')
                    if s['sdh']:
                        flags.append('SDH')
                    if s['forced']:
                        flags.append('Forced')
                    flag_str = ', '.join(flags) if flags else ''
                    title_str = s['title'] if s['title'] else ''
                    stream_tree.insert('', 'end', iid=str(i),
                                       values=(s['index'], lang,
                                               s['codec_name'], title_str,
                                               flag_str))

                stream_tree.selection_set('0')

                def on_select():
                    sel = stream_tree.selection()
                    if sel:
                        chosen[0] = text_streams[int(sel[0])]
                    picker.destroy()

                def on_double_click(event):
                    on_select()

                stream_tree.bind('<Double-1>', on_double_click)

                btn_frame = ttk.Frame(picker, padding=(10, 8, 10, 10))
                btn_frame.pack(fill='x')
                ttk.Button(btn_frame, text="Edit Selected",
                           command=on_select).pack(side='right', padx=(4, 0))
                ttk.Button(btn_frame, text="Cancel",
                           command=picker.destroy).pack(side='right')

                picker.wait_window()

                chosen = chosen[0]
                if chosen is None:
                    return  # user cancelled

            # Extract the selected stream to a temp SRT file
            stream_index = chosen['index']
            tmp_srt = tempfile.NamedTemporaryFile(suffix='.srt', delete=False,
                                                   mode='w', encoding='utf-8')
            tmp_srt.close()
            cmd = ['ffmpeg', '-y', '-i', video_path,
                   '-map', f'0:{stream_index}', '-c:s', 'srt', tmp_srt.name]

            # ── Progress dialog during extraction ──
            prog_dlg = tk.Toplevel(editor)
            prog_dlg.title("Importing Subtitle")
            prog_dlg.resizable(False, False)
            prog_dlg.transient(editor)
            prog_dlg.overrideredirect(False)

            prog_f = ttk.Frame(prog_dlg, padding=20)
            prog_f.pack(fill='both', expand=True)
            ttk.Label(prog_f,
                      text=f"Importing subtitle stream #{stream_index} "
                           f"from {os.path.basename(video_path)}...",
                      wraplength=350).pack(pady=(0, 10))
            prog_bar = ttk.Progressbar(prog_f, mode='indeterminate', length=300)
            prog_bar.pack(pady=(0, 5))
            prog_bar.start(15)

            app._center_on_main(prog_dlg)
            prog_dlg.grab_set()
            prog_dlg.protocol('WM_DELETE_WINDOW', lambda: None)  # prevent closing

            extract_result = [None]  # (returncode, stderr) or Exception

            def _run_extract():
                try:
                    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                    extract_result[0] = ('ok', r.returncode, r.stderr)
                except Exception as e:
                    extract_result[0] = ('error', e)

            t = threading.Thread(target=_run_extract, daemon=True)
            t.start()

            def _check_extract():
                if t.is_alive():
                    editor.after(50, _check_extract)
                    return
                prog_bar.stop()
                prog_dlg.grab_release()
                prog_dlg.destroy()

                res = extract_result[0]
                if res is None:
                    os.unlink(tmp_srt.name)
                    return
                if res[0] == 'error':
                    messagebox.showerror("Error",
                        f"Extract error:\n{res[1]}", parent=editor)
                    os.unlink(tmp_srt.name)
                    return
                _, returncode, stderr = res
                if returncode != 0:
                    messagebox.showerror("Error",
                        f"Failed to extract subtitle stream #{stream_index}:\n"
                        f"{stderr[-300:]}",
                        parent=editor)
                    os.unlink(tmp_srt.name)
                    return

                with open(tmp_srt.name, 'r', encoding='utf-8',
                          errors='replace') as f:
                    srt_text = f.read()

                # Build title
                lang = chosen['language'] if chosen['language'] != 'und' else '?'
                title_str = (f"Subtitle Editor — Stream #{stream_index} ({lang}) — "
                             f"{os.path.basename(video_path)}")

                if _load_cues_into_editor(srt_text, title_str, tmp_srt.name):
                    # Store video source info for re-muxing on save
                    video_source[0] = {
                        'path': video_path,
                        'stream_index': stream_index,
                        'temp_srt': tmp_srt.name,
                        'streams': streams,
                        'stream_info': chosen,
                    }
                    app.add_log(
                        f"Opened video subtitle: stream #{stream_index} ({lang}) "
                        f"from {os.path.basename(video_path)} "
                        f"({len(cues)} entries)", 'INFO')
                    # Auto-load waveform timeline
                    editor.after(200, lambda: _load_waveform_for_video(video_path))
                else:
                    os.unlink(tmp_srt.name)

            editor.after(50, _check_extract)

        def do_open_file():
            path = filedialog.askopenfilename(
                parent=editor,
                title="Open Subtitle or Video File",
                filetypes=[
                    ('Subtitle files', '*.srt *.ass *.ssa *.vtt *.sub'),
                    ('Video files', '*.mkv *.mp4 *.avi *.mov *.wmv *.flv *.webm *.ts *.m2ts *.mts'),
                    ('All files', '*.*'),
                ]
            )
            if not path:
                return
            ext = Path(path).suffix.lower()
            if ext in VIDEO_EXTENSIONS:
                load_video_subtitle(path)
            else:
                video_source[0] = None  # clear video mode
                load_file(path)

        def on_drop_subtitle(event):
            """Handle subtitle or video files dragged and dropped onto the editor."""
            raw = event.data
            # tkinterdnd2 wraps paths with spaces in curly braces: {/path/to/my file.srt}
            # On Linux, file managers may also send file:// URIs (one per line)
            paths = []
            if 'file://' in raw:
                from urllib.parse import unquote, urlparse
                for line in raw.splitlines():
                    line = line.strip()
                    if line.startswith('file://'):
                        decoded = unquote(urlparse(line).path)
                        if decoded:
                            paths.append(decoded)
            else:
                i = 0
                while i < len(raw):
                    if raw[i] == '{':
                        end = raw.find('}', i)
                        paths.append(raw[i + 1:end])
                        i = end + 2
                    elif raw[i] == ' ':
                        i += 1
                    else:
                        end = raw.find(' ', i)
                        if end == -1:
                            paths.append(raw[i:])
                            break
                        else:
                            paths.append(raw[i:end])
                            i = end + 1
            if paths:
                path = paths[0]
                ext = Path(path).suffix.lower()
                if ext in VIDEO_EXTENSIONS:
                    load_video_subtitle(path)
                else:
                    video_source[0] = None  # clear video mode
                    load_file(path)

        # ── Register drag-and-drop on the editor window ──
        if HAS_DND:
            editor.drop_target_register(DND_FILES)
            editor.dnd_bind('<<Drop>>', on_drop_subtitle)

        def do_save_file():
            if not cues or not current_path[0]:
                return
            removed = len(original_cues) - len(cues)

            if video_source[0]:
                # ── Re-mux edited subtitle back into the video ──
                vs = video_source[0]
                video_path = vs['path']
                stream_idx = vs['stream_index']
                temp_srt = vs['temp_srt']
                streams = vs['streams']

                # Write edited SRT to temp file
                with open(temp_srt, 'w', encoding='utf-8') as f:
                    f.write(write_srt(cues))

                # Build ffmpeg command: map every stream in order, replacing
                # the target subtitle with the edited version to preserve track order
                tmp_out = str(Path(video_path).with_suffix('.tmp' + Path(video_path).suffix))
                cmd = ['ffmpeg', '-y', '-i', video_path, '-i', temp_srt]

                all_streams = get_all_streams(video_path)
                out_sub_count = 0
                replaced_out_sub_idx = None
                for s in all_streams:
                    if s['index'] == stream_idx:
                        # Replace this subtitle with the edited version
                        cmd.extend(['-map', '1:0'])
                        replaced_out_sub_idx = out_sub_count
                        out_sub_count += 1
                    else:
                        cmd.extend(['-map', f"0:{s['index']}"])
                        if s['codec_type'] == 'subtitle':
                            out_sub_count += 1

                # Copy all codecs (no re-encoding)
                cmd.extend(['-c', 'copy'])

                # Preserve metadata on the replaced subtitle stream
                orig = vs['stream_info']
                if replaced_out_sub_idx is not None:
                    if orig.get('language') and orig['language'] != 'und':
                        cmd.extend([f'-metadata:s:s:{replaced_out_sub_idx}',
                                    f"language={orig['language']}"])
                    if orig.get('title'):
                        cmd.extend([f'-metadata:s:s:{replaced_out_sub_idx}',
                                    f"title={orig['title']}"])
                    # Preserve disposition flags
                    disp_parts = []
                    if orig.get('default'):
                        disp_parts.append('default')
                    if orig.get('forced'):
                        disp_parts.append('forced')
                    if orig.get('sdh'):
                        disp_parts.append('hearing_impaired')
                    if disp_parts:
                        cmd.extend([f'-disposition:s:{replaced_out_sub_idx}',
                                    '+'.join(disp_parts)])

                    # For MP4 containers, subtitle codec must be mov_text
                    if Path(video_path).suffix.lower() in ('.mp4', '.m4v'):
                        cmd.extend([f'-c:s:{replaced_out_sub_idx}', 'mov_text'])

                cmd.append(tmp_out)

                app.add_log(f"Re-muxing subtitle into {os.path.basename(video_path)}...",
                             'INFO')
                app.add_log(f"ffmpeg command: {' '.join(cmd)}", 'INFO')
                editor.update_idletasks()

                try:
                    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
                    if result.returncode != 0:
                        app.add_log(f"Re-mux stderr: {result.stderr[-500:]}", 'ERROR')
                        messagebox.showerror("Re-mux Failed",
                            f"Failed to save subtitle back to video:\n\n"
                            f"{result.stderr[-400:]}",
                            parent=editor)
                        # Clean up failed temp output
                        if os.path.exists(tmp_out):
                            os.unlink(tmp_out)
                        return

                    # Atomic replace: swap temp output over original
                    os.replace(tmp_out, video_path)

                    # Cleanup temp SRT
                    try:
                        os.unlink(temp_srt)
                    except OSError:
                        pass

                    app.add_log(f"Subtitle re-muxed into video: {len(cues)} entries "
                                 f"({removed} removed) → {os.path.basename(video_path)}",
                                 'SUCCESS')
                    # Reset baseline so unsaved-changes check is accurate
                    original_cues[:] = [dict(c) for c in cues]
                    messagebox.showinfo("Saved",
                        f"Subtitle stream #{stream_idx} saved back to:\n"
                        f"{os.path.basename(video_path)}",
                        parent=editor)
                    video_source[0] = None  # clear video mode after successful save

                except Exception as e:
                    messagebox.showerror("Error", f"Re-mux error:\n{e}", parent=editor)
                    if os.path.exists(tmp_out):
                        os.unlink(tmp_out)
            else:
                # ── Normal subtitle file save ──
                with open(current_path[0], 'w', encoding='utf-8') as f:
                    f.write(write_srt(cues))
                app.add_log(f"Subtitle saved: {len(cues)} entries ({removed} removed) → "
                             f"{os.path.basename(current_path[0])}", 'SUCCESS')
                # Reset baseline so unsaved-changes check is accurate
                original_cues[:] = [dict(c) for c in cues]

        def do_save_as():
            if not cues:
                return
            if video_source[0]:
                ref_path = video_source[0]['path']
            elif current_path[0]:
                ref_path = current_path[0]
            else:
                ref_path = None
            out_dir = str(Path(ref_path).parent) if ref_path else ''
            default_name = (f"{Path(ref_path).stem}.srt"
                            if ref_path else "subtitle.srt")
            out_path = filedialog.asksaveasfilename(
                parent=editor,
                initialdir=out_dir,
                initialfile=default_name,
                defaultextension='.srt',
                filetypes=[('SubRip', '*.srt'), ('All files', '*.*')]
            )
            if not out_path:
                return
            with open(out_path, 'w', encoding='utf-8') as f:
                f.write(write_srt(cues))
            current_path[0] = out_path
            editor.title(f"Subtitle Editor — {os.path.basename(out_path)}")
            app.add_log(f"Subtitle saved as: {out_path}", 'SUCCESS')

        def do_export():
            if not cues:
                messagebox.showwarning("Empty", "No subtitle entries to export.",
                                       parent=editor)
                return
            # Use the video file path for directory/name when editing a video subtitle
            if video_source[0]:
                ref_path = video_source[0]['path']
            elif current_path[0]:
                ref_path = current_path[0]
            else:
                ref_path = None
            out_dir = str(Path(ref_path).parent) if ref_path else ''
            default_name = (f"{Path(ref_path).stem}.srt"
                            if ref_path else "subtitle.srt")
            out_path = filedialog.asksaveasfilename(
                parent=editor,
                initialdir=out_dir,
                initialfile=default_name,
                defaultextension='.srt',
                filetypes=[('SubRip', '*.srt'), ('All files', '*.*')]
            )
            if not out_path:
                return
            with open(out_path, 'w', encoding='utf-8') as f:
                f.write(write_srt(cues))
            app.add_log(f"Exported subtitle → {out_path}", 'SUCCESS')

        file_menu.add_command(label="Open...", accelerator="Ctrl+O", command=do_open_file)
        file_menu.add_separator()
        file_menu.add_command(label="Save", accelerator="Ctrl+S", command=do_save_file)
        file_menu.add_command(label="Save As...", accelerator="Ctrl+Shift+S",
                              command=do_save_as)
        file_menu.add_command(label="Export SRT...", command=do_export)
        file_menu.add_separator()
        file_menu.add_command(label="Batch Filter...", command=app.open_batch_filter)
        file_menu.add_separator()
        file_menu.add_command(label="Close", command=lambda: on_editor_close())

        editor.bind('<Control-o>', lambda e: do_open_file())
        editor.bind('<Control-O>', lambda e: do_open_file())
        editor.bind('<Control-s>', lambda e: do_save_file())
        editor.bind('<Control-S>', lambda e: do_save_file())

        # ── Filters menu ──
        def apply_filter(filter_func, name):
            nonlocal cues
            push_undo()
            before = len(cues)
            cues = filter_func(cues)
            after = len(cues)
            app.add_log(f"Filter '{name}': {before - after} entries removed, "
                         f"{after} remaining", 'INFO')
            refresh_tree(cues)

        def _is_mostly_allcaps():
            """Check if the subtitle text is mostly ALL CAPS."""
            if not cues:
                return False
            all_text = ' '.join(c['text'] for c in cues)
            alpha = re.sub(r'[^a-zA-Z]', '', all_text)
            if not alpha:
                return False
            return sum(1 for c in alpha if c.isupper()) / len(alpha) >= 0.6

        def apply_remove_hi():
            """Apply Remove HI, auto-running Fix ALL CAPS first if text is all-caps."""
            nonlocal cues
            if _is_mostly_allcaps():
                app.add_log("Text is mostly ALL CAPS — running Fix ALL CAPS first "
                             "to avoid false HI detection", 'INFO')
                push_undo()
                cues = filter_fix_caps(cues, app.custom_cap_words)
                refresh_tree(cues)
            apply_filter(filter_remove_hi, "Remove HI")

        def undo_all():
            nonlocal cues
            push_undo()
            cues = [dict(c) for c in original_cues]
            refresh_tree(cues)
            app.add_log("Subtitle edits reset to original", 'INFO')

        filter_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Tools", menu=filter_menu)
        filter_menu.add_command(label="Remove HI  [brackets] (parens) Speaker:",
                                command=lambda: apply_remove_hi())
        filter_menu.add_command(label="Remove Tags  <i> {\\an8}",
                                command=lambda: apply_filter(filter_remove_tags, "Remove Tags"))

        def apply_remove_ads():
            apply_filter(lambda c: filter_remove_ads(c, app.custom_ad_patterns),
                         "Remove Ads")

        filter_menu.add_command(label="Remove Ads / Credits", command=apply_remove_ads)
        filter_menu.add_command(label="Remove Stray Notes  ♪ ♫",
                                command=lambda: apply_filter(filter_remove_music_notes, "Remove Stray Notes"))
        filter_menu.add_command(label="Remove Leading Dashes  -",
                                command=lambda: apply_filter(filter_remove_leading_dashes, "Remove Leading Dashes"))
        filter_menu.add_command(label="Remove ALL CAPS HI  (UK style)",
                                command=lambda: apply_filter(filter_remove_caps_hi, "Remove CAPS HI"))
        filter_menu.add_command(label="Remove Off-Screen Quotes  ' '  (UK style)",
                                command=lambda: apply_filter(filter_remove_offscreen_quotes, "Remove Off-Screen Quotes"))
        filter_menu.add_separator()
        filter_menu.add_command(label="Remove Duplicates",
                                command=lambda: apply_filter(filter_remove_duplicates, "Remove Duplicates"))
        filter_menu.add_command(label="Merge Short Cues",
                                command=lambda: apply_filter(filter_merge_short, "Merge Short Cues"))
        filter_menu.add_command(label="Reduce to 2 Lines",
                                command=lambda: apply_filter(filter_reduce_lines, "Reduce to 2 Lines"))
        filter_menu.add_separator()

        # ── Fix ALL CAPS ──
        if not hasattr(app, 'custom_cap_words'):
            app.custom_cap_words = []

        def show_fix_caps_dialog():
            cd = tk.Toplevel(editor)
            cd.title("Fix ALL CAPS")
            cd.geometry("420x400")
            app._center_on_main(cd)
            cd.resizable(True, True)
            # Keep on top but don't grab — allows scrolling the subtitle list
            cd.attributes('-topmost', True)

            ttk.Label(cd, text="Converts ALL CAPS text to sentence case.\n"
                      "Add character names below to preserve their capitalisation.\n"
                      "You can scroll the subtitle list to find names.",
                      justify='center', padding=(10, 10)).pack()

            lf = ttk.LabelFrame(cd, text="Custom Names (saved across sessions)",
                                padding=8)
            lf.pack(fill='both', expand=True, padx=10, pady=5)

            word_list = tk.Listbox(lf, height=8, font=('Courier', 10))
            word_list.pack(fill='both', expand=True)
            for w in app.custom_cap_words:
                word_list.insert('end', w)

            add_frame = ttk.Frame(lf)
            add_frame.pack(fill='x', pady=(4, 0))
            new_word_var = tk.StringVar()
            word_entry = ttk.Entry(add_frame, textvariable=new_word_var)
            word_entry.pack(side='left', fill='x', expand=True, padx=(0, 4))
            word_entry.focus_set()
            # Right-click context menu for copy/paste
            _wm = tk.Menu(word_entry, tearoff=0)
            _wm.add_command(label="Cut", command=lambda: word_entry.event_generate('<<Cut>>'))
            _wm.add_command(label="Copy", command=lambda: word_entry.event_generate('<<Copy>>'))
            _wm.add_command(label="Paste", command=lambda: word_entry.event_generate('<<Paste>>'))
            _wm.add_separator()
            _wm.add_command(label="Select All",
                command=lambda: (word_entry.select_range(0, 'end'), word_entry.icursor('end')))
            word_entry.bind('<Button-3>', lambda e, m=_wm: m.tk_popup(e.x_root, e.y_root))

            def add_word():
                word = new_word_var.get().strip()
                if not word:
                    return
                if word.lower() not in [w.lower() for w in app.custom_cap_words]:
                    app.custom_cap_words.append(word)
                    word_list.insert('end', word)
                    app.save_preferences()
                new_word_var.set('')

            def remove_word():
                sel = word_list.curselection()
                if sel:
                    app.custom_cap_words.pop(sel[0])
                    word_list.delete(sel[0])
                    app.save_preferences()

            ttk.Button(add_frame, text="Add", command=add_word).pack(side='right')
            word_entry.bind('<Return>', lambda e: add_word())

            ttk.Label(lf, text="Names are saved automatically and persist between sessions.",
                      font=('Helvetica', 8), foreground='gray').pack(anchor='w')

            btn_frame = ttk.Frame(cd, padding=(10, 8, 10, 10))
            btn_frame.pack(fill='x')
            ttk.Button(btn_frame, text="Remove Selected", command=remove_word).pack(side='left')
            ttk.Button(btn_frame, text="Apply",
                       command=lambda: (cd.destroy(), apply_filter(
                           lambda c: filter_fix_caps(c, app.custom_cap_words),
                           "Fix ALL CAPS"))).pack(side='right')
            ttk.Button(btn_frame, text="Close", command=cd.destroy).pack(side='right', padx=4)

        filter_menu.add_command(label="Fix ALL CAPS...", command=show_fix_caps_dialog)
        filter_menu.add_separator()

        def show_ad_patterns_dialog():
            pd = tk.Toplevel(editor)
            pd.title("Ad / Credit Patterns")
            pd.geometry("500x420")
            pd.transient(editor)
            pd.grab_set()
            app._center_on_main(pd)
            pd.resizable(True, True)

            bf = ttk.LabelFrame(pd, text="Built-in Patterns (always active)", padding=8)
            bf.pack(fill='x', padx=10, pady=(10, 5))
            builtin_list = tk.Listbox(bf, height=6, font=('Courier', 9))
            builtin_list.pack(fill='x')
            for p in BUILTIN_AD_PATTERNS:
                builtin_list.insert('end', p)

            cf = ttk.LabelFrame(pd, text="Custom Patterns (saved to preferences)", padding=8)
            cf.pack(fill='both', expand=True, padx=10, pady=5)

            custom_list = tk.Listbox(cf, height=8, font=('Courier', 9))
            custom_list.pack(fill='both', expand=True)
            for p in app.custom_ad_patterns:
                custom_list.insert('end', p)

            add_frame = ttk.Frame(cf)
            add_frame.pack(fill='x', pady=(4, 0))
            new_pattern_var = tk.StringVar()
            pattern_entry = ttk.Entry(add_frame, textvariable=new_pattern_var)
            pattern_entry.pack(side='left', fill='x', expand=True, padx=(0, 4))

            def add_pattern():
                pat = new_pattern_var.get().strip()
                if not pat:
                    return
                try:
                    re.compile(pat)
                except re.error as e:
                    messagebox.showwarning("Invalid Pattern",
                                           f"Not a valid regex:\n{e}", parent=pd)
                    return
                if pat not in app.custom_ad_patterns:
                    app.custom_ad_patterns.append(pat)
                    custom_list.insert('end', pat)
                    new_pattern_var.set('')
                    app.add_log(f"Added custom ad pattern: {pat}", 'INFO')

            def remove_selected():
                sel = custom_list.curselection()
                if not sel:
                    return
                idx = sel[0]
                removed = app.custom_ad_patterns.pop(idx)
                custom_list.delete(idx)
                app.add_log(f"Removed custom ad pattern: {removed}", 'INFO')

            ttk.Button(add_frame, text="Add", command=add_pattern).pack(side='right')
            pattern_entry.bind('<Return>', lambda e: add_pattern())

            ttk.Label(cf, text="Patterns are case-insensitive regex matched at start of line.",
                      font=('Helvetica', 8), foreground='gray').pack(anchor='w')

            btn_frame = ttk.Frame(pd, padding=(10, 6, 10, 10))
            btn_frame.pack(fill='x')
            ttk.Button(btn_frame, text="Remove Selected", command=remove_selected).pack(side='left')

            def save_and_close():
                app.save_preferences()
                pd.destroy()

            ttk.Button(btn_frame, text="Save & Close", command=save_and_close).pack(side='right')
            ttk.Button(btn_frame, text="Cancel", command=pd.destroy).pack(side='right', padx=4)

        filter_menu.add_command(label="Manage Ad Patterns...",
                                command=show_ad_patterns_dialog)
        filter_menu.add_separator()
        filter_menu.add_command(label="Spell Check...",
                                accelerator="F7",
                                command=lambda: _show_spell_check())
        filter_menu.add_separator()
        filter_menu.add_command(label="Search/Replace List...",
                                command=lambda: _show_saved_replacements())

        def _show_saved_replacements():
            """Show dialog to manage and apply persistent search & replace pairs."""
            sd = tk.Toplevel(editor)
            sd.title("Search/Replace List")
            sd.geometry("550x450")
            sd.resizable(True, True)
            app._center_on_main(sd)
            sd.attributes('-topmost', True)

            f = ttk.Frame(sd, padding=12)
            f.pack(fill='both', expand=True)
            f.columnconfigure(0, weight=1)
            f.rowconfigure(1, weight=1)

            # ── Add new pair ──
            add_f = ttk.LabelFrame(f, text="Add Replacement", padding=6)
            add_f.grid(row=0, column=0, sticky='ew', pady=(0, 8))

            af = ttk.Frame(add_f)
            af.pack(fill='x')
            ttk.Label(af, text="Find:").pack(side='left', padx=(0, 4))
            sr_find = tk.StringVar()
            sr_find_entry = ttk.Entry(af, textvariable=sr_find, width=18)
            sr_find_entry.pack(side='left', padx=(0, 8))
            ttk.Label(af, text="Replace:").pack(side='left', padx=(0, 4))
            sr_repl = tk.StringVar()
            sr_repl_entry = ttk.Entry(af, textvariable=sr_repl, width=18)
            sr_repl_entry.pack(side='left', padx=(0, 8))
            sr_case = tk.BooleanVar(value=False)
            ttk.Checkbutton(af, text="Aa", variable=sr_case).pack(side='left', padx=(0, 4))

            def _add_pair():
                find = sr_find.get()
                if not find:
                    return
                repl = sr_repl.get()
                pair = [find, repl, sr_case.get()]
                if pair not in app.custom_replacements:
                    app.custom_replacements.append(pair)
                    app.save_preferences()
                _refresh_list()
                sr_find.set('')
                sr_repl.set('')

            ttk.Button(af, text="Add", command=_add_pair, width=5).pack(side='left', padx=2)

            # ── List ──
            list_f = ttk.Frame(f)
            list_f.grid(row=1, column=0, sticky='nsew')
            list_f.columnconfigure(0, weight=1)
            list_f.rowconfigure(0, weight=1)

            columns = ('find', 'replace', 'case')
            sr_tree = ttk.Treeview(list_f, columns=columns, show='headings', height=10)
            sr_tree.grid(row=0, column=0, sticky='nsew')
            sr_tree.heading('find', text='Find')
            sr_tree.heading('replace', text='Replace With')
            sr_tree.heading('case', text='Case')
            sr_tree.column('find', width=180, minwidth=100)
            sr_tree.column('replace', width=180, minwidth=100)
            sr_tree.column('case', width=50, minwidth=40, anchor='center')

            sr_scroll = ttk.Scrollbar(list_f, orient='vertical', command=sr_tree.yview)
            sr_scroll.grid(row=0, column=1, sticky='ns')
            sr_tree.configure(yscrollcommand=sr_scroll.set)

            def _refresh_list():
                sr_tree.delete(*sr_tree.get_children())
                for i, pair in enumerate(app.custom_replacements):
                    find, repl = pair[0], pair[1]
                    case = 'Yes' if (len(pair) > 2 and pair[2]) else 'No'
                    sr_tree.insert('', 'end', iid=str(i),
                                  values=(find, repl, case))

            def _remove_selected():
                sel = sr_tree.selection()
                if not sel:
                    return
                indices = sorted([int(s) for s in sel], reverse=True)
                for idx in indices:
                    if idx < len(app.custom_replacements):
                        del app.custom_replacements[idx]
                app.save_preferences()
                _refresh_list()

            def _clear_all():
                if messagebox.askyesno("Clear All",
                    "Remove all saved replacements?", parent=sd):
                    app.custom_replacements.clear()
                    app.save_preferences()
                    _refresh_list()

            # ── Buttons ──
            btn_f = ttk.Frame(f)
            btn_f.grid(row=2, column=0, sticky='ew', pady=(8, 0))

            def _apply_all():
                if not app.custom_replacements:
                    messagebox.showinfo("No Replacements",
                        "No saved replacements to apply.", parent=sd)
                    return
                push_undo()
                total_count = 0
                for pair in app.custom_replacements:
                    find, repl = pair[0], pair[1]
                    case_sensitive = len(pair) > 2 and pair[2]
                    for cue in cues:
                        old = cue['text']
                        if case_sensitive:
                            cue['text'] = cue['text'].replace(find, repl)
                        else:
                            cue['text'] = re.sub(re.escape(find), lambda m: repl,
                                                 cue['text'], flags=re.IGNORECASE)
                        if cue['text'] != old:
                            total_count += 1
                refresh_tree(cues)
                app.add_log(f"Applied {len(app.custom_replacements)} replacement rule(s), "
                             f"{total_count} cue(s) changed", 'INFO')
                messagebox.showinfo("Replacements Applied",
                    f"Applied {len(app.custom_replacements)} rule(s)\n"
                    f"{total_count} cue(s) modified", parent=sd)

            ttk.Button(btn_f, text="▶ Apply All", command=_apply_all).pack(side='left', padx=2)
            ttk.Button(btn_f, text="Remove", command=_remove_selected).pack(side='left', padx=2)
            ttk.Button(btn_f, text="Clear All", command=_clear_all).pack(side='left', padx=2)
            ttk.Button(btn_f, text="Close", command=sd.destroy).pack(side='right', padx=2)

            _refresh_list()

        def _show_spell_check():
            """Incremental spell check — scans and fixes as it goes."""
            if not cues:
                messagebox.showinfo("Spell Check", "No subtitle loaded.",
                                    parent=editor)
                return

            # ── Initialize spell checker ──
            try:
                from spellchecker import SpellChecker
            except ImportError:
                if messagebox.askyesno("Missing Package",
                    "pyspellchecker is not installed.\n\n"
                    "Would you like to install it now?",
                    parent=editor):
                    try:
                        _pip_result = subprocess.run(
                            [sys.executable, '-m', 'pip', 'install',
                             '--user', '--break-system-packages', 'pyspellchecker'],
                            capture_output=True, text=True, timeout=60)
                        if _pip_result.returncode == 0:
                            from spellchecker import SpellChecker
                        else:
                            messagebox.showerror("Install Failed",
                                f"pip install failed:\n{_pip_result.stderr[-300:]}",
                                parent=editor)
                            return
                    except Exception as _e:
                        messagebox.showerror("Install Failed",
                            f"Could not install pyspellchecker:\n{_e}",
                            parent=editor)
                        return
                else:
                    return

            spell = SpellChecker()
            known = [w.lower() for w in app.custom_cap_words + app.custom_spell_words]
            if known:
                spell.word_frequency.load_words(known)

            # ── Scan state ──
            scan_cue = [0]        # current cue index being scanned
            scan_word = [0]       # current word index within the cue
            ignored = set()
            error_count = [0]
            cues_checked = [0]

            # ── Build dialog ──
            sd = tk.Toplevel(editor)
            sd.withdraw()
            sd.title("Spell Check")
            sd.geometry("500x440")
            sd.resizable(True, True)
            sd.update_idletasks()
            # Center on editor window
            ew, eh = editor.winfo_width(), editor.winfo_height()
            ex, ey = editor.winfo_x(), editor.winfo_y()
            sw, sh = 500, 440
            sd.geometry(f"{sw}x{sh}+{ex + (ew - sw)//2}+{ey + (eh - sh)//2}")
            sd.deiconify()
            sd.attributes('-topmost', True)

            sf = ttk.Frame(sd, padding=12)
            sf.pack(fill='both', expand=True)
            sf.columnconfigure(1, weight=1)
            _sp = {'padx': 6, 'pady': 4}

            stats_lbl = ttk.Label(sf, text="Scanning...",
                                  font=('Helvetica', 9))
            stats_lbl.grid(row=0, column=0, columnspan=2, sticky='w', **_sp)

            ttk.Label(sf, text="Not in dictionary:",
                      font=('Helvetica', 10, 'bold')).grid(
                          row=1, column=0, sticky='w', **_sp)
            word_var = tk.StringVar()
            ttk.Entry(sf, textvariable=word_var, state='readonly',
                      font=('Courier', 12)).grid(
                          row=1, column=1, sticky='ew', **_sp)

            ttk.Label(sf, text="Context:").grid(
                row=2, column=0, sticky='nw', **_sp)
            ctx_var = tk.StringVar()
            ttk.Label(sf, textvariable=ctx_var, wraplength=380,
                      font=('Helvetica', 9),
                      foreground='gray').grid(
                          row=2, column=1, sticky='w', **_sp)

            ttk.Label(sf, text="Suggestions:").grid(
                row=3, column=0, sticky='nw', **_sp)
            sug_fr = ttk.Frame(sf)
            sug_fr.grid(row=3, column=1, sticky='nsew', **_sp)
            sug_fr.rowconfigure(0, weight=1)
            sug_fr.columnconfigure(0, weight=1)
            sf.rowconfigure(3, weight=1)

            sug_lb = tk.Listbox(sug_fr, height=6, font=('Courier', 10))
            sug_lb.grid(row=0, column=0, sticky='nsew')
            sug_sc = ttk.Scrollbar(sug_fr, orient='vertical',
                                   command=sug_lb.yview)
            sug_sc.grid(row=0, column=1, sticky='ns')
            sug_lb.configure(yscrollcommand=sug_sc.set)

            replace_var = tk.StringVar()
            def on_sug_sel(evt):
                sel = sug_lb.curselection()
                if sel:
                    replace_var.set(sug_lb.get(sel[0]))
            sug_lb.bind('<<ListboxSelect>>', on_sug_sel)

            ttk.Label(sf, text="Replace with:").grid(
                row=4, column=0, sticky='w', **_sp)
            ttk.Entry(sf, textvariable=replace_var,
                      font=('Courier', 11)).grid(
                          row=4, column=1, sticky='ew', **_sp)

            bf = ttk.Frame(sf)
            bf.grid(row=5, column=0, columnspan=2, sticky='ew',
                    pady=(8, 0))

            # ── Incremental scanner ──
            def _find_next():
                """Scan forward from current position for the next error.
                Returns (cue_idx, word, candidates) or None."""
                ci = scan_cue[0]
                wi = scan_word[0]
                while ci < len(cues):
                    cues_checked[0] = ci + 1
                    clean = re.sub(r'<[^>]+>|\{\\[^}]+\}|♪', '',
                                   cues[ci]['text'])
                    words = re.findall(r"[a-zA-Z]+(?:'[a-zA-Z]+)?",
                                       clean)
                    if words:
                        unknown = spell.unknown(words)
                        if unknown:
                            for j in range(wi, len(words)):
                                w = words[j]
                                if ((w.lower() in unknown or w in unknown)
                                        and w.lower() not in ignored):
                                    cands = spell.candidates(w)
                                    spell_error_indices.add(ci)
                                    scan_cue[0] = ci
                                    scan_word[0] = j + 1
                                    return (ci, w,
                                            sorted(cands) if cands else [])
                    ci += 1
                    wi = 0
                    scan_cue[0] = ci
                    scan_word[0] = 0
                return None

            # ── Current error state ──
            current_error = [None]  # (ci, word, candidates)

            def _show_next():
                """Find and display the next error."""
                result = _find_next()
                current_error[0] = result
                if result is None:
                    spell_error_indices.clear()
                    refresh_tree(cues)
                    messagebox.showinfo("Spell Check",
                        f"Spell check complete!\n"
                        f"{cues_checked[0]} cues checked, "
                        f"{error_count[0]} errors found.",
                        parent=sd)
                    sd.destroy()
                    return
                ci, w, ca = result
                error_count[0] += 1
                items = tree.get_children()
                if ci < len(items):
                    ahead = min(ci + 5, len(items) - 1)
                    tree.see(items[ahead])
                    tree.selection_set(items[ci])
                    tree.after(50, lambda: tree.see(items[ci]))
                word_var.set(w)
                ctx_var.set(cues[ci]['text'].replace('\n', ' / '))
                stats_lbl.configure(
                    text=f"Checking cue {ci + 1} of {len(cues)} "
                         f"({error_count[0]} errors found)")
                sug_lb.delete(0, 'end')
                for c in ca:
                    sug_lb.insert('end', c)
                if ca:
                    sug_lb.selection_set(0)
                    replace_var.set(ca[0])
                else:
                    replace_var.set(w)

            def _do_replace():
                if not current_error[0]:
                    return
                ci, w, _ = current_error[0]
                repl = replace_var.get().strip()
                if not repl:
                    return
                push_undo()
                txt = cues[ci]['text']
                pos = txt.find(w)
                if pos == -1:
                    pos = txt.lower().find(w.lower())
                if pos >= 0:
                    cues[ci]['text'] = (txt[:pos] + repl
                                       + txt[pos + len(w):])
                refresh_tree(cues)
                # Re-check same cue from current word position
                _show_next()

            def _do_replace_all():
                if not current_error[0]:
                    return
                _, w, _ = current_error[0]
                repl = replace_var.get().strip()
                if not repl:
                    return
                push_undo()
                for cue in cues:
                    if w in cue['text']:
                        cue['text'] = cue['text'].replace(w, repl)
                    elif w.lower() in cue['text'].lower():
                        cue['text'] = re.sub(re.escape(w), repl,
                                             cue['text'],
                                             flags=re.IGNORECASE)
                ignored.add(w.lower())
                refresh_tree(cues)
                _show_next()

            def _do_skip():
                _show_next()

            def _do_ignore():
                if current_error[0]:
                    ignored.add(current_error[0][1].lower())
                _show_next()

            def _do_add_dict():
                if not current_error[0]:
                    return
                w = current_error[0][1]
                if w.lower() not in [x.lower()
                                     for x in app.custom_spell_words]:
                    app.custom_spell_words.append(w)
                    spell.word_frequency.load_words([w.lower()])
                    app.save_preferences()
                ignored.add(w.lower())
                _show_next()

            def _do_add_name():
                if not current_error[0]:
                    return
                w = current_error[0][1]
                if w not in app.custom_cap_words:
                    app.custom_cap_words.append(w)
                if w.lower() not in [x.lower()
                                     for x in app.custom_spell_words]:
                    app.custom_spell_words.append(w)
                spell.word_frequency.load_words([w.lower()])
                app.save_preferences()
                ignored.add(w.lower())
                _show_next()

            bf1 = ttk.Frame(bf)
            bf1.pack(fill='x')
            ttk.Button(bf1, text="Replace", command=_do_replace,
                       width=10).pack(side='left', padx=2)
            ttk.Button(bf1, text="Replace All",
                       command=_do_replace_all,
                       width=10).pack(side='left', padx=2)
            ttk.Button(bf1, text="Skip", command=_do_skip,
                       width=6).pack(side='left', padx=2)
            ttk.Button(bf1, text="Ignore", command=_do_ignore,
                       width=8).pack(side='left', padx=2)

            bf2 = ttk.Frame(bf)
            bf2.pack(fill='x', pady=(4, 0))
            ttk.Button(bf2, text="Add to Dict",
                       command=_do_add_dict,
                       width=10).pack(side='left', padx=2)
            ttk.Button(bf2, text="Add as Name",
                       command=_do_add_name,
                       width=10).pack(side='left', padx=2)
            ttk.Button(bf2, text="Close", command=sd.destroy,
                       width=6).pack(side='right', padx=2)

            # Start scanning immediately
            _show_next()

        editor.bind('<F7>', lambda e: _show_spell_check())

        # ── Edit menu ──
        edit_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Edit", menu=edit_menu)
        edit_menu.add_command(label="Undo                Ctrl+Z", command=do_undo)
        edit_menu.add_command(label="Redo                Ctrl+Y", command=do_redo)
        edit_menu.add_command(label="Reset to Original", command=undo_all)
        edit_menu.add_separator()

        def delete_selected():
            nonlocal cues
            selected = tree.selection()
            if not selected:
                return
            push_undo()
            indices_to_remove = set(int(s) for s in selected)
            cues = [c for i, c in enumerate(cues) if i not in indices_to_remove]
            refresh_tree(cues)

        def split_selected():
            nonlocal cues
            selected = tree.selection()
            if len(selected) != 1:
                messagebox.showinfo("Split", "Select exactly one cue to split.",
                                    parent=editor)
                return
            idx = int(selected[0])
            cue = cues[idx]
            text = cue['text']
            lines = text.split('\n')
            if len(lines) < 2:
                mid = len(text) // 2
                space_pos = text.rfind(' ', 0, mid + 10)
                if space_pos > mid - 20:
                    mid = space_pos
                text1 = text[:mid].rstrip()
                text2 = text[mid:].lstrip()
            else:
                mid_line = len(lines) // 2
                text1 = '\n'.join(lines[:mid_line])
                text2 = '\n'.join(lines[mid_line:])
            if not text1 or not text2:
                return
            push_undo()
            start_ms = srt_ts_to_ms(cue['start'])
            end_ms = srt_ts_to_ms(cue['end'])
            mid_ms = (start_ms + end_ms) // 2
            cue1 = {**cue, 'text': text1, 'end': ms_to_srt_ts(mid_ms)}
            cue2 = {**cue, 'text': text2, 'start': ms_to_srt_ts(mid_ms + 1)}
            cues[idx:idx + 1] = [cue1, cue2]
            refresh_tree(cues)

        def join_selected():
            nonlocal cues
            selected = sorted(tree.selection(), key=int)
            if len(selected) < 2:
                messagebox.showinfo("Join", "Select two or more consecutive cues to join.",
                                    parent=editor)
                return
            indices = [int(s) for s in selected]
            for i in range(1, len(indices)):
                if indices[i] != indices[i - 1] + 1:
                    messagebox.showwarning("Join",
                        "Selected cues must be consecutive.", parent=editor)
                    return
            push_undo()
            first = cues[indices[0]]
            last = cues[indices[-1]]
            merged_text = ' '.join(cues[i]['text'] for i in indices)
            merged = {**first, 'end': last['end'], 'text': merged_text}
            cues[indices[0]:indices[-1] + 1] = [merged]
            refresh_tree(cues)

        edit_menu.add_command(label="Delete Selected     Del", command=delete_selected)
        edit_menu.add_command(label="Split Cue", command=split_selected)
        edit_menu.add_command(label="Join Selected Cues", command=join_selected)

        # ── Timing menu ──
        def show_timing_dialog():
            td = tk.Toplevel(editor)
            td.title("Timing Adjustment")
            td.geometry("440x380")
            td.transient(editor)
            app._center_on_main(td)
            td.resizable(False, False)
            td.attributes('-topmost', True)

            of = ttk.LabelFrame(td, text="Offset (shift all timestamps)", padding=8)
            of.pack(fill='x', padx=10, pady=(10, 5))
            offset_var = tk.StringVar(value="0")
            ttk.Label(of, text="Milliseconds (+/−):").pack(side='left')
            ttk.Entry(of, textvariable=offset_var, width=10).pack(side='left', padx=4)

            def apply_offset():
                nonlocal cues
                try:
                    ms = int(offset_var.get())
                except ValueError:
                    messagebox.showwarning("Invalid", "Enter a number in milliseconds.",
                                           parent=td)
                    return
                if ms == 0:
                    return
                push_undo()
                cues = shift_timestamps(cues, ms)
                refresh_tree(cues)
                direction = "forward" if ms > 0 else "backward"
                app.add_log(f"Shifted timestamps {direction} by {abs(ms)}ms", 'INFO')
                td.destroy()

            ttk.Button(of, text="Apply", command=apply_offset).pack(side='right')

            sf = ttk.LabelFrame(td, text="Stretch (scale timestamps)", padding=8)
            sf.pack(fill='x', padx=10, pady=5)
            stretch_var = tk.StringVar(value="1.0")
            ttk.Label(sf, text="Factor:").pack(side='left')
            ttk.Entry(sf, textvariable=stretch_var, width=10).pack(side='left', padx=4)

            def apply_stretch():
                nonlocal cues
                try:
                    factor = float(stretch_var.get())
                except ValueError:
                    messagebox.showwarning("Invalid", "Enter a decimal number (e.g. 1.04).",
                                           parent=td)
                    return
                if factor <= 0:
                    messagebox.showwarning("Invalid", "Factor must be positive.", parent=td)
                    return
                if factor == 1.0:
                    return
                push_undo()
                cues = stretch_timestamps(cues, factor)
                refresh_tree(cues)
                app.add_log(f"Stretched timestamps by factor {factor}", 'INFO')
                td.destroy()

            ttk.Button(sf, text="Apply", command=apply_stretch).pack(side='right')

            # ── Two-Point Sync ──
            tp = ttk.LabelFrame(td, text="Two-Point Sync (fix offset + drift)", padding=8)
            tp.pack(fill='x', padx=10, pady=5)

            ttk.Label(tp, text="Pick two cues and enter the correct start times.\n"
                              "All timestamps will be linearly adjusted.",
                      font=('Helvetica', 8), foreground='gray').pack(anchor='w')

            tp_grid = ttk.Frame(tp)
            tp_grid.pack(fill='x', pady=(4, 0))
            tp_grid.columnconfigure(2, weight=1)

            # Point A
            ttk.Label(tp_grid, text="Point A — Cue #:").grid(row=0, column=0, sticky='w', padx=(0, 4), pady=2)
            tp_a_cue = tk.StringVar(value="1")
            ttk.Entry(tp_grid, textvariable=tp_a_cue, width=6).grid(row=0, column=1, sticky='w', pady=2)
            ttk.Label(tp_grid, text="Correct time:").grid(row=0, column=2, sticky='e', padx=(8, 4), pady=2)
            tp_a_time = tk.StringVar(value="00:00:00,000")
            ttk.Entry(tp_grid, textvariable=tp_a_time, width=14).grid(row=0, column=3, sticky='w', pady=2)

            # Point B
            ttk.Label(tp_grid, text="Point B — Cue #:").grid(row=1, column=0, sticky='w', padx=(0, 4), pady=2)
            tp_b_cue = tk.StringVar(value=str(len(cues)))
            ttk.Entry(tp_grid, textvariable=tp_b_cue, width=6).grid(row=1, column=1, sticky='w', pady=2)
            ttk.Label(tp_grid, text="Correct time:").grid(row=1, column=2, sticky='e', padx=(8, 4), pady=2)
            tp_b_time = tk.StringVar(value="00:00:00,000")
            ttk.Entry(tp_grid, textvariable=tp_b_time, width=14).grid(row=1, column=3, sticky='w', pady=2)

            def _fill_current(var_cue, var_time):
                """Fill the time field with the current start time of the selected cue."""
                try:
                    idx = int(var_cue.get()) - 1
                    if 0 <= idx < len(cues):
                        var_time.set(cues[idx]['start'])
                        # Highlight the cue in the tree
                        items = tree.get_children()
                        if idx < len(items):
                            tree.see(items[idx])
                            tree.selection_set(items[idx])
                except (ValueError, IndexError):
                    pass

            fill_f = ttk.Frame(tp)
            fill_f.pack(fill='x', pady=(4, 0))
            ttk.Button(fill_f, text="Get A", width=6,
                       command=lambda: _fill_current(tp_a_cue, tp_a_time)).pack(side='left', padx=2)
            ttk.Button(fill_f, text="Get B", width=6,
                       command=lambda: _fill_current(tp_b_cue, tp_b_time)).pack(side='left', padx=2)
            ttk.Label(fill_f, text="(fills current time for that cue)",
                      font=('Helvetica', 8), foreground='gray').pack(side='left', padx=8)

            def apply_two_point():
                nonlocal cues
                try:
                    idx_a = int(tp_a_cue.get()) - 1
                    idx_b = int(tp_b_cue.get()) - 1
                except ValueError:
                    messagebox.showwarning("Invalid", "Enter cue numbers.", parent=td)
                    return
                if idx_a < 0 or idx_a >= len(cues) or idx_b < 0 or idx_b >= len(cues):
                    messagebox.showwarning("Invalid",
                        f"Cue numbers must be between 1 and {len(cues)}.", parent=td)
                    return
                if idx_a == idx_b:
                    messagebox.showwarning("Invalid",
                        "Point A and B must be different cues.", parent=td)
                    return
                try:
                    ms_a = srt_ts_to_ms(tp_a_time.get())
                    ms_b = srt_ts_to_ms(tp_b_time.get())
                except Exception:
                    messagebox.showwarning("Invalid",
                        "Enter times in SRT format: HH:MM:SS,mmm", parent=td)
                    return
                push_undo()
                cues = two_point_sync(cues, idx_a, ms_a, idx_b, ms_b)
                refresh_tree(cues)
                app.add_log(f"Two-point sync: cue #{idx_a+1} → {tp_a_time.get()}, "
                             f"cue #{idx_b+1} → {tp_b_time.get()}", 'INFO')
                td.destroy()

            ttk.Button(fill_f, text="Apply Sync", command=apply_two_point).pack(side='right', padx=2)

            ttk.Button(td, text="Close", command=td.destroy).pack(pady=(5, 10))

        timing_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Timing", menu=timing_menu)
        timing_menu.add_command(label="Offset / Stretch...", command=show_timing_dialog)
        timing_menu.add_command(label="Smart Sync...",
                                command=lambda: _show_smart_sync())

        # ── Quick Sync submenu ──
        quick_sync_menu = tk.Menu(timing_menu, tearoff=0)
        timing_menu.add_cascade(label="Quick Sync", menu=quick_sync_menu)

        def _quick_sync_first_cue():
            """Shift all cues so the first cue starts at a user-specified time.
            Includes an embedded mpv player for marking the exact time."""
            if not cues:
                messagebox.showinfo("No Subtitles", "Load subtitles first.",
                                    parent=editor)
                return

            qd = tk.Toplevel(editor)
            qd.title("Quick Sync — Set First Cue Time")
            qd.geometry("720x620")
            qd.minsize(640, 540)
            qd.resizable(True, True)
            app._center_on_main(qd)

            f = ttk.Frame(qd, padding=10)
            f.pack(fill='both', expand=True)
            f.columnconfigure(1, weight=1)
            f.rowconfigure(2, weight=1)  # video frame expands

            first_cue = cues[0]
            current_start = first_cue['start']
            preview_text = first_cue['text'].replace('\n', ' ')
            if len(preview_text) > 60:
                preview_text = preview_text[:57] + '...'

            # ── Video file ──
            ttk.Label(f, text="Video file:").grid(
                row=0, column=0, sticky='w', padx=4, pady=2)
            _qs_vpath = tk.StringVar()
            # Try to find video automatically
            try:
                if hasattr(editor, '_qs_last_video') and editor._qs_last_video:
                    _qs_vpath.set(editor._qs_last_video)
                elif current_path[0]:
                    _sub_dir = os.path.dirname(current_path[0])
                    _sub_stem = os.path.splitext(
                        os.path.basename(current_path[0]))[0]
                    for _i in range(3):
                        _dot = _sub_stem.rfind('.')
                        if _dot > 0:
                            _sub_stem = _sub_stem[:_dot]
                        else:
                            break
                    for ext in VIDEO_EXTENSIONS:
                        _vp = os.path.join(_sub_dir, _sub_stem + ext)
                        if os.path.isfile(_vp):
                            _qs_vpath.set(_vp)
                            break
            except Exception:
                pass

            _vpath_entry = ttk.Entry(f, textvariable=_qs_vpath)
            _vpath_entry.grid(row=0, column=1, sticky='ew', padx=4, pady=2)

            def _qs_browse():
                init_dir = os.path.dirname(_qs_vpath.get()) if _qs_vpath.get() \
                    else (os.path.dirname(current_path[0]) if current_path[0] else '')
                p = None
                if shutil.which('zenity'):
                    try:
                        cmd = ['zenity', '--file-selection',
                               '--title', 'Select Video File',
                               '--file-filter',
                               'Video files|*.mkv *.mp4 *.avi *.mov *.ts *.m2ts *.mts *.webm *.wmv *.flv',
                               '--file-filter', 'All files|*']
                        if init_dir:
                            cmd += ['--filename', init_dir + '/']
                        r = subprocess.run(cmd, capture_output=True,
                                           text=True, timeout=120)
                        if r.returncode == 0 and r.stdout.strip():
                            p = r.stdout.strip()
                    except Exception:
                        pass
                if not p:
                    p = filedialog.askopenfilename(
                        parent=qd, title="Select Video File",
                        initialdir=init_dir or None,
                        filetypes=[("Video files",
                                    "*.mkv *.mp4 *.avi *.mov *.ts *.m2ts"),
                                   ("All files", "*.*")])
                if p:
                    _qs_vpath.set(p)
                    # Auto-load the video after browse selection
                    qd.after(100, _play_video)
            ttk.Button(f, text="Browse...", command=_qs_browse).grid(
                row=0, column=2, padx=4, pady=2)

            # ── Embedded video player frame ──
            video_border = ttk.Frame(f, relief='sunken', borderwidth=2)
            video_border.grid(row=2, column=0, columnspan=3,
                              sticky='nsew', padx=4, pady=4)
            video_frame = tk.Frame(video_border, bg='black',
                                   width=640, height=360)
            video_frame.pack(fill='both', expand=True)
            video_frame.pack_propagate(False)

            _placeholder_label = tk.Label(video_frame,
                text="Drop a video file here or click Browse",
                bg='black', fg='#666', font=('Helvetica', 12))

            # ── Drag-and-drop support ──
            def _on_qs_drop(event):
                """Handle video files dropped onto the Quick Sync dialog."""
                raw = event.data
                paths = []
                if 'file://' in raw:
                    from urllib.parse import unquote, urlparse
                    for line in raw.splitlines():
                        line = line.strip()
                        if line.startswith('file://'):
                            decoded = unquote(urlparse(line).path)
                            if decoded:
                                paths.append(decoded)
                else:
                    i = 0
                    while i < len(raw):
                        if raw[i] == '{':
                            end = raw.find('}', i)
                            paths.append(raw[i + 1:end])
                            i = end + 2
                        elif raw[i] == ' ':
                            i += 1
                        else:
                            end = raw.find(' ', i)
                            if end == -1:
                                end = len(raw)
                            paths.append(raw[i:end])
                            i = end + 1

                # Find first video file in dropped paths
                for p in paths:
                    if os.path.isfile(p):
                        ext = os.path.splitext(p)[1].lower()
                        if ext in VIDEO_EXTENSIONS:
                            _qs_vpath.set(p)
                            qd.after(100, _play_video)
                            return

            try:
                qd.drop_target_register(DND_FILES)
                qd.dnd_bind('<<Drop>>', _on_qs_drop)
            except Exception:
                pass  # tkinterdnd2 not available
            _placeholder_label.place(relx=0.5, rely=0.5, anchor='center')

            # ── mpv player integration ──
            import tempfile as _qs_tempfile
            import socket as _qs_socket
            import json as _qs_json

            _mpv_proc = [None]
            _mpv_socket_path = os.path.join(
                _qs_tempfile.gettempdir(),
                f'docflix_mpv_{os.getpid()}')

            def _mpv_cmd(command_list):
                """Send a command to mpv via IPC and return the response."""
                try:
                    sock = _qs_socket.socket(
                        _qs_socket.AF_UNIX, _qs_socket.SOCK_STREAM)
                    sock.settimeout(2)
                    sock.connect(_mpv_socket_path)
                    payload = _qs_json.dumps(
                        {"command": command_list}) + '\n'
                    sock.sendall(payload.encode())
                    data = sock.recv(4096).decode()
                    sock.close()
                    return _qs_json.loads(data)
                except Exception:
                    return None

            def _play_video():
                vp = _qs_vpath.get().strip()
                if not vp or not os.path.isfile(vp):
                    messagebox.showwarning("No Video",
                        "Select a video file first.", parent=qd)
                    return

                # Kill previous mpv instance if running
                if _mpv_proc[0] and _mpv_proc[0].poll() is None:
                    _mpv_proc[0].terminate()
                    _mpv_proc[0].wait(timeout=5)

                # Clean up old socket
                if os.path.exists(_mpv_socket_path):
                    try:
                        os.unlink(_mpv_socket_path)
                    except OSError:
                        pass

                # Hide placeholder
                _placeholder_label.place_forget()

                # Get the X11 window ID for embedding
                video_frame.update_idletasks()
                wid = str(video_frame.winfo_id())

                # Launch mpv embedded in the video frame
                try:
                    _mpv_proc[0] = subprocess.Popen([
                        'mpv',
                        f'--input-ipc-server={_mpv_socket_path}',
                        f'--wid={wid}',
                        '--pause',
                        '--osd-level=2',
                        '--osd-fractions',
                        '--keep-open=yes',
                        '--no-border',
                        '--cursor-autohide=1000',
                        vp
                    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    mark_btn.configure(state='normal')
                    _mute_btn.configure(text="🔊")
                    _vol_var.set(100)
                    editor._qs_last_video = vp
                except FileNotFoundError:
                    messagebox.showerror("mpv Not Found",
                        "mpv is not installed.\n\n"
                        "Install with: sudo apt install mpv", parent=qd)
                    _placeholder_label.place(relx=0.5, rely=0.5, anchor='center')
                except Exception as e:
                    messagebox.showerror("Player Error",
                        f"Could not launch mpv:\n{e}", parent=qd)
                    _placeholder_label.place(relx=0.5, rely=0.5, anchor='center')

            def _mark_time():
                """Query mpv for current playback position and fill the time field."""
                if not _mpv_proc[0] or _mpv_proc[0].poll() is not None:
                    messagebox.showinfo("Player Closed",
                        "Load the video first.", parent=qd)
                    mark_btn.configure(state='disabled')
                    pass  # player closed
                    return

                resp = _mpv_cmd(["get_property", "playback-time"])
                if resp and 'data' in resp and resp['data'] is not None:
                    seconds = resp['data']
                    ms = int(seconds * 1000)
                    time_var.set(ms_to_srt_ts(ms))
                    time_entry.select_range(0, 'end')
                else:
                    messagebox.showwarning("Could Not Read Time",
                        "Could not get playback position from mpv.\n"
                        "Make sure the video is loaded.", parent=qd)

            def _mpv_seek(amount):
                """Seek mpv by amount in seconds."""
                if not _mpv_proc[0] or _mpv_proc[0].poll() is not None:
                    return
                _mpv_cmd(["seek", str(amount), "relative+exact"])

            def _mpv_frame_step(direction='forward'):
                """Step one frame forward or backward."""
                if not _mpv_proc[0] or _mpv_proc[0].poll() is not None:
                    return
                if direction == 'forward':
                    _mpv_cmd(["frame-step"])
                else:
                    _mpv_cmd(["frame-back-step"])

            def _mpv_pause_toggle():
                """Toggle play/pause."""
                if not _mpv_proc[0] or _mpv_proc[0].poll() is not None:
                    return
                _mpv_cmd(["cycle", "pause"])

            def _on_close():
                # Kill mpv and clean up socket
                if _mpv_proc[0] and _mpv_proc[0].poll() is None:
                    _mpv_proc[0].terminate()
                    try:
                        _mpv_proc[0].wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        _mpv_proc[0].kill()
                _mpv_proc[0] = None
                if os.path.exists(_mpv_socket_path):
                    try:
                        os.unlink(_mpv_socket_path)
                    except OSError:
                        pass
                # Reset state so next open starts fresh
                editor._qs_last_video = None
                qd.destroy()

            qd.protocol("WM_DELETE_WINDOW", _on_close)

            # ── Transport controls ──
            transport_f = ttk.Frame(f)
            transport_f.grid(row=3, column=0, columnspan=3, sticky='ew', pady=(4, 0))

            _tb_w = 3
            _transport_btns = [
                ("⏮",  lambda: _mpv_seek(-5),               "Rewind 5 seconds"),
                ("◀◀", lambda: _mpv_seek(-1),               "Rewind 1 second"),
                ("◀",  lambda: _mpv_seek(-0.1),             "Rewind 100ms"),
                ("|◀", lambda: _mpv_frame_step('backward'), "Back 1 frame"),
                ("⏯",  _mpv_pause_toggle,                   "Play / Pause"),
                ("▶|", lambda: _mpv_frame_step('forward'),  "Forward 1 frame"),
                ("▶",  lambda: _mpv_seek(0.1),              "Forward 100ms"),
                ("▶▶", lambda: _mpv_seek(1),                "Forward 1 second"),
                ("⏭",  lambda: _mpv_seek(5),                "Forward 5 seconds"),
            ]
            for _sym, _cmd, _tip in _transport_btns:
                _px = 2 if _sym == "⏯" else 1
                _b = ttk.Button(transport_f, text=_sym, width=_tb_w, command=_cmd)
                _b.pack(side='left', padx=_px)
                create_tooltip(_b, _tip)

            mark_btn = ttk.Button(transport_f, text="⏱ Mark",
                                  command=_mark_time, width=6,
                                  state='disabled')
            mark_btn.pack(side='left', padx=(6, 0))
            create_tooltip(mark_btn, "Capture current playback time")

            # ── Volume controls ──
            def _mpv_toggle_mute():
                if not _mpv_proc[0] or _mpv_proc[0].poll() is not None:
                    return
                _mpv_cmd(["cycle", "mute"])
                # Update mute button label
                resp = _mpv_cmd(["get_property", "mute"])
                if resp and 'data' in resp:
                    _mute_btn.configure(text="🔇" if resp['data'] else "🔊")

            def _mpv_set_volume(val):
                if not _mpv_proc[0] or _mpv_proc[0].poll() is not None:
                    return
                _mpv_cmd(["set_property", "volume", float(val)])

            _mute_btn = ttk.Button(transport_f, text="🔊", width=2,
                                   command=_mpv_toggle_mute)
            _mute_btn.pack(side='right', padx=(4, 0))
            create_tooltip(_mute_btn, "Mute / Unmute")

            _vol_var = tk.DoubleVar(value=100)
            _vol_scale = ttk.Scale(transport_f, from_=0, to=100,
                                   orient='horizontal', length=80,
                                   variable=_vol_var,
                                   command=_mpv_set_volume)
            _vol_scale.pack(side='right', padx=2)
            create_tooltip(_vol_scale, "Volume")

            # ── Sync controls ──
            ttk.Separator(f, orient='horizontal').grid(
                row=4, column=0, columnspan=3, sticky='ew', pady=6)

            sync_f = ttk.Frame(f)
            sync_f.grid(row=5, column=0, columnspan=3, sticky='ew', padx=4)
            sync_f.columnconfigure(1, weight=1)

            ttk.Label(sync_f, text="First cue:",
                      font=('Helvetica', 9, 'bold')).grid(
                          row=0, column=0, sticky='w', pady=1)
            ttk.Label(sync_f, text=f'"{preview_text}"',
                      font=('Helvetica', 9), foreground='gray').grid(
                          row=0, column=1, columnspan=2, sticky='w', padx=8, pady=1)

            ttk.Label(sync_f, text="Current:").grid(
                row=1, column=0, sticky='w', pady=1)
            ttk.Label(sync_f, text=current_start,
                      font=('Courier', 10)).grid(
                          row=1, column=1, sticky='w', padx=8, pady=1)

            ttk.Label(sync_f, text="New start:").grid(
                row=2, column=0, sticky='w', pady=2)
            time_var = tk.StringVar(value=current_start)
            _time_f = ttk.Frame(sync_f)
            _time_f.grid(row=2, column=1, columnspan=2, sticky='w', padx=8, pady=2)
            time_entry = ttk.Entry(_time_f, textvariable=time_var, width=16,
                                   font=('Courier', 10))
            time_entry.pack(side='left')
            ttk.Label(_time_f, text="HH:MM:SS,mmm",
                      foreground='gray', font=('Helvetica', 8)).pack(
                          side='left', padx=8)

            offset_var = tk.StringVar(value="Offset: 0ms")
            ttk.Label(sync_f, textvariable=offset_var,
                      font=('Helvetica', 9), foreground='#666').grid(
                          row=3, column=0, columnspan=3, sticky='w', pady=1)

            def _update_offset(*_args):
                try:
                    new_ms = srt_ts_to_ms(time_var.get().strip())
                    old_ms = srt_ts_to_ms(current_start)
                    diff = new_ms - old_ms
                    sign = '+' if diff >= 0 else ''
                    offset_var.set(f"Offset: {sign}{diff}ms ({sign}{diff/1000:.1f}s)")
                except Exception:
                    offset_var.set("Offset: (invalid time format)")
            time_var.trace_add('write', _update_offset)

            # ── Action buttons ──
            btn_f = ttk.Frame(f)
            btn_f.grid(row=6, column=0, columnspan=3, sticky='ew', pady=(8, 0))

            def _apply_first_cue():
                nonlocal cues
                try:
                    new_ms = srt_ts_to_ms(time_var.get().strip())
                except Exception:
                    messagebox.showwarning("Invalid Time",
                        "Enter time in SRT format: HH:MM:SS,mmm", parent=qd)
                    return
                old_ms = srt_ts_to_ms(current_start)
                offset = new_ms - old_ms
                if offset == 0:
                    _on_close()
                    return
                push_undo()
                cues = shift_timestamps(cues, offset)
                refresh_tree(cues)
                sign = '+' if offset > 0 else ''
                app.add_log(f"Quick Sync: shifted all cues {sign}{offset}ms "
                             f"(first cue → {time_var.get().strip()})", 'SUCCESS')
                _on_close()

            time_entry.bind('<Return>', lambda e: _apply_first_cue())
            _apply_btn = ttk.Button(btn_f, text="Apply",
                                    command=_apply_first_cue, width=8)
            _apply_btn.pack(side='left', padx=2)
            create_tooltip(_apply_btn, "Shift all cues by the offset and close")
            _cancel_btn = ttk.Button(btn_f, text="Cancel",
                                     command=_on_close, width=8)
            _cancel_btn.pack(side='left', padx=2)
            create_tooltip(_cancel_btn, "Close without applying changes")

            # Auto-load video if one was detected
            if _qs_vpath.get().strip() and os.path.isfile(_qs_vpath.get().strip()):
                qd.after(300, _play_video)

        quick_sync_menu.add_command(label="Set First Cue Time...",
                                    command=_quick_sync_first_cue)

        # ── View menu ──
        view_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="View", menu=view_menu)

        def _toggle_timeline_menu():
            _toggle_timeline()

        def _load_waveform_menu():
            """Load waveform from the associated video file."""
            vpath = _find_video_for_subtitle()
            if vpath:
                _load_waveform_for_video(vpath)
            else:
                # Ask user to pick a video file
                vpath = filedialog.askopenfilename(
                    parent=editor,
                    title="Select Video File for Waveform",
                    filetypes=[
                        ("Video files", " ".join(f"*{e}" for e in VIDEO_EXTENSIONS)),
                        ("All files", "*.*"),
                    ],
                )
                if vpath:
                    _load_waveform_for_video(vpath)

        view_menu.add_command(label="Load Waveform...",
                              command=_load_waveform_menu)
        view_menu.add_command(label="Show/Hide Timeline",
                              command=_toggle_timeline_menu,
                              accelerator="Ctrl+T")
        editor.bind('<Control-t>', lambda e: _toggle_timeline_menu())
        editor.bind('<Control-T>', lambda e: _toggle_timeline_menu())

        def _find_video_for_subtitle():
            """Try to find the video file for the current subtitle."""
            vpath = None
            if video_source and video_source[0]:
                vpath = video_source[0].get('path')
            if not vpath and current_path[0]:
                sub_dir = os.path.dirname(current_path[0])
                sub_stem = os.path.splitext(os.path.basename(current_path[0]))[0]
                for _ in range(3):
                    if '.' in sub_stem:
                        sub_stem = sub_stem.rsplit('.', 1)[0]
                for ext in VIDEO_EXTENSIONS:
                    candidate = os.path.join(sub_dir, sub_stem + ext)
                    if os.path.isfile(candidate):
                        return candidate
                for ext in VIDEO_EXTENSIONS:
                    for fp in Path(sub_dir).glob(f'*{ext}'):
                        if fp.is_file() and not fp.name.startswith('.'):
                            return str(fp)
            return vpath

        def _show_smart_sync():
            """Auto-sync subtitles using Whisper speech recognition."""
            import threading

            if not cues:
                messagebox.showinfo("No Subtitles", "Load subtitles first.", parent=editor)
                return

            # Check faster-whisper availability
            try:
                from faster_whisper import WhisperModel
            except ImportError:
                if messagebox.askyesno("Missing Package",
                    "faster-whisper is not installed.\n\n"
                    "Would you like to install it now?\n"
                    "(This may take a few minutes — downloads ~200MB)",
                    parent=editor):
                    try:
                        app.add_log("Installing faster-whisper...", 'INFO')
                        _pip_result = subprocess.run(
                            [sys.executable, '-m', 'pip', 'install',
                             '--user', '--break-system-packages', 'faster-whisper'],
                            capture_output=True, text=True, timeout=300)
                        if _pip_result.returncode == 0:
                            app.add_log("faster-whisper installed successfully", 'SUCCESS')
                        else:
                            messagebox.showerror("Install Failed",
                                f"pip install failed:\n{_pip_result.stderr[-300:]}",
                                parent=editor)
                            return
                    except Exception as _e:
                        messagebox.showerror("Install Failed",
                            f"Could not install:\n{_e}", parent=editor)
                        return
                else:
                    return

            vpath = _find_video_for_subtitle()

            sd = tk.Toplevel(editor)
            sd.title("Smart Sync")
            sd.geometry("560x580")
            sd.resizable(True, True)
            app._center_on_main(sd)

            f = ttk.Frame(sd, padding=12)
            f.pack(fill='both', expand=True)
            f.columnconfigure(1, weight=1)
            _sp = {'padx': 6, 'pady': 4}

            # ── Video file ──
            ttk.Label(f, text="Video file:").grid(row=0, column=0, sticky='w', **_sp)
            vpath_var = tk.StringVar(value=vpath or '')
            ttk.Entry(f, textvariable=vpath_var).grid(row=0, column=1, sticky='ew', **_sp)
            def _browse_vid():
                # Start in the subtitle's folder if available
                init_dir = ''
                if vpath_var.get():
                    init_dir = os.path.dirname(vpath_var.get())
                elif current_path[0]:
                    init_dir = os.path.dirname(current_path[0])
                # Try zenity first (better sizing), fall back to tkinter
                p = None
                if shutil.which('zenity'):
                    try:
                        cmd = ['zenity', '--file-selection',
                               '--title', 'Select Video File',
                               '--file-filter', 'Video files|*.mkv *.mp4 *.avi *.mov *.ts *.m2ts *.mts *.webm *.wmv *.flv',
                               '--file-filter', 'All files|*']
                        if init_dir:
                            cmd += ['--filename', init_dir + '/']
                        result = subprocess.run(cmd, capture_output=True,
                                                text=True, timeout=120)
                        if result.returncode == 0 and result.stdout.strip():
                            p = result.stdout.strip()
                    except Exception:
                        pass
                if not p:
                    p = filedialog.askopenfilename(
                        parent=sd,
                        title="Select Video File",
                        initialdir=init_dir or None,
                        filetypes=[("Video files", "*.mkv *.mp4 *.avi *.mov *.ts *.m2ts"),
                                   ("All files", "*.*")])
                if p:
                    vpath_var.set(p)
            ttk.Button(f, text="Browse...", command=_browse_vid).grid(row=0, column=2, **_sp)

            # ── Model selection ──
            model_label = ttk.Label(f, text="Whisper model:")
            model_label.grid(row=1, column=0, sticky='w', **_sp)
            model_f = ttk.Frame(f)
            model_f.grid(row=1, column=1, columnspan=2, sticky='w', **_sp)
            model_var = tk.StringVar(value='base')
            for m, tip in [('tiny', '~75MB, fastest'),
                           ('base', '~150MB, good balance'),
                           ('small', '~500MB, more accurate')]:
                ttk.Radiobutton(model_f, text=f"{m} ({tip})",
                               variable=model_var, value=m).pack(anchor='w')

            # ── Language ──
            ttk.Label(f, text="Language:").grid(row=2, column=0, sticky='w', **_sp)
            lang_var = tk.StringVar(value='en')
            lang_f = ttk.Frame(f)
            lang_f.grid(row=2, column=1, columnspan=2, sticky='w', **_sp)
            ttk.Entry(lang_f, textvariable=lang_var, width=5).pack(side='left')
            ttk.Label(lang_f, text="(en, fr, es, de, etc. — blank = auto-detect)",
                      foreground='gray', font=('Helvetica', 8)).pack(side='left', padx=8)

            # ── Engine selection ──
            ttk.Label(f, text="Engine:").grid(row=3, column=0, sticky='w', **_sp)
            engine_f = ttk.Frame(f)
            engine_f.grid(row=3, column=1, columnspan=2, sticky='w', **_sp)
            engine_var = tk.StringVar(value='faster-whisper')

            def _on_engine_change():
                eng = engine_var.get()
                if eng == 'whisperx':
                    finetune_var.set('200')
                    finetune_hint.config(
                        text="ms  (phoneme onset is ~200ms before perceived speech)")
                    direct_rb.configure(state='normal')
                else:
                    finetune_var.set('400')
                    finetune_hint.config(
                        text="ms  (applied after sync — compensates for Whisper timing)")
                    # Direct Align requires WhisperX — switch away if selected
                    if scan_mode_var.get() == 'direct':
                        scan_mode_var.set('quick')
                    direct_rb.configure(state='disabled')
                _on_scan_mode_change()

            ttk.Radiobutton(engine_f, text="Standard (faster-whisper)",
                           variable=engine_var, value='faster-whisper',
                           command=_on_engine_change).pack(anchor='w')
            ttk.Radiobutton(engine_f,
                           text="Precise (WhisperX) — phoneme-level alignment",
                           variable=engine_var, value='whisperx',
                           command=_on_engine_change).pack(anchor='w')

            # ── Scan mode ──
            ttk.Label(f, text="Scan mode:").grid(row=4, column=0, sticky='w', **_sp)
            scan_f = ttk.Frame(f)
            scan_f.grid(row=4, column=1, columnspan=2, sticky='w', **_sp)
            scan_mode_var = tk.StringVar(value='quick')

            def _on_scan_mode_change():
                mode = scan_mode_var.get()
                if mode == 'quick':
                    seg_label.grid()
                    sample_f.grid()
                    model_label.grid()
                    model_f.grid()
                elif mode == 'full':
                    seg_label.grid_remove()
                    sample_f.grid_remove()
                    model_label.grid()
                    model_f.grid()
                else:  # direct
                    seg_label.grid_remove()
                    sample_f.grid_remove()
                    model_label.grid_remove()
                    model_f.grid_remove()

            ttk.Radiobutton(scan_f, text="Quick Scan", variable=scan_mode_var,
                           value='quick', command=_on_scan_mode_change).pack(side='left', padx=(0, 8))
            ttk.Radiobutton(scan_f, text="Full Scan (for Re-time)",
                           variable=scan_mode_var, value='full',
                           command=_on_scan_mode_change).pack(side='left', padx=(0, 8))
            direct_rb = ttk.Radiobutton(scan_f,
                           text="Direct Align",
                           variable=scan_mode_var, value='direct',
                           command=_on_scan_mode_change, state='disabled')
            direct_rb.pack(side='left')

            seg_label = ttk.Label(f, text="Segments:")
            seg_label.grid(row=5, column=0, sticky='w', **_sp)
            sample_f = ttk.Frame(f)
            sample_f.grid(row=5, column=1, columnspan=2, sticky='w', **_sp)
            segments_var = tk.StringVar(value='3')
            seg_spin = tk.Spinbox(sample_f, textvariable=segments_var, from_=1, to=20,
                        width=3)
            seg_spin.pack(side='left')
            ttk.Label(sample_f, text="× ").pack(side='left')
            sample_len_var = tk.StringVar(value='5')
            len_spin = tk.Spinbox(sample_f, textvariable=sample_len_var, from_=1, to=30,
                        width=3)
            len_spin.pack(side='left')
            ttk.Label(sample_f, text="min each",
                      foreground='gray', font=('Helvetica', 8)).pack(side='left', padx=4)

            # ── Offset adjustment ──
            ttk.Label(f, text="Fine-tune:").grid(row=6, column=0, sticky='w', **_sp)
            finetune_f = ttk.Frame(f)
            finetune_f.grid(row=6, column=1, columnspan=2, sticky='w', **_sp)
            finetune_var = tk.StringVar(value='400')
            tk.Spinbox(finetune_f, textvariable=finetune_var, from_=-2000, to=2000,
                       increment=50, width=6).pack(side='left')
            finetune_hint = ttk.Label(finetune_f,
                      text="ms  (applied after sync — compensates for Whisper timing)",
                      foreground='gray', font=('Helvetica', 8))
            finetune_hint.pack(side='left', padx=4)

            # ── Progress ──
            status_var = tk.StringVar(value="Ready — click Start to begin")
            ttk.Label(f, textvariable=status_var, wraplength=450,
                      font=('Helvetica', 9)).grid(row=7, column=0, columnspan=3, sticky='w', **_sp)

            progress_var = tk.DoubleVar(value=0)
            ttk.Progressbar(f, variable=progress_var, maximum=100,
                           mode='determinate').grid(row=8, column=0, columnspan=3,
                                                      sticky='ew', **_sp)

            # ── Results ──
            result_frame = ttk.LabelFrame(f, text="Results", padding=6)
            result_frame.grid(row=9, column=0, columnspan=3, sticky='nsew', **_sp)
            result_frame.columnconfigure(0, weight=1)
            result_frame.rowconfigure(0, weight=1)
            f.rowconfigure(9, weight=1)

            result_text = tk.Text(result_frame, height=8, wrap='word',
                                 font=('Courier', 9), state='disabled',
                                 bg='#1e1e1e', fg='#d4d4d4')
            result_text.grid(row=0, column=0, sticky='nsew')
            r_scroll = ttk.Scrollbar(result_frame, orient='vertical', command=result_text.yview)
            r_scroll.grid(row=0, column=1, sticky='ns')
            result_text.configure(yscrollcommand=r_scroll.set)

            def _rlog(msg, color='#d4d4d4'):
                result_text.configure(state='normal')
                result_text.insert('end', msg + '\n')
                result_text.see('end')
                result_text.configure(state='disabled')

            # ── Buttons ──
            btn_f = ttk.Frame(f)
            btn_f.grid(row=10, column=0, columnspan=3, sticky='ew', pady=(8, 0))

            cancel_event = threading.Event()
            sync_result = [None]
            pre_sync_cues = [None]  # snapshot before sync — for repeatable Re-time

            def _start():
                vp = vpath_var.get().strip()
                if not vp or not os.path.isfile(vp):
                    messagebox.showwarning("No Video", "Select a video file.", parent=sd)
                    return

                # Save cues before sync so Re-time/Apply can repeat with different fine-tune
                import copy as _copy
                pre_sync_cues[0] = _copy.deepcopy(cues)

                # ── Engine-aware dependency check ──
                _engine = engine_var.get()
                if _engine == 'whisperx':
                    try:
                        import whisperx
                    except ImportError:
                        if messagebox.askyesno("Missing Package",
                            "WhisperX is not installed.\n\n"
                            "Would you like to install it now?\n"
                            "(Requires PyTorch — downloads ~2GB)",
                            parent=sd):
                            # Run pip install in background thread with progress
                            start_btn.configure(state='disabled')
                            status_var.set("Installing whisperx (downloading ~2GB)...")
                            app.add_log("Installing whisperx...", 'INFO')
                            _rlog("Installing whisperx — this may take several minutes...")
                            # Switch progress bar to indeterminate mode
                            _install_pbar = None
                            for _w in f.winfo_children():
                                if isinstance(_w, ttk.Progressbar):
                                    _install_pbar = _w
                                    break
                            if _install_pbar:
                                _install_pbar.configure(mode='indeterminate')
                                _install_pbar.start(15)

                            def _do_whisperx_install():
                                try:
                                    proc = subprocess.Popen(
                                        [sys.executable, '-m', 'pip', 'install',
                                         '--user', '--break-system-packages',
                                         '--progress-bar', 'off',
                                         'whisperx', 'transformers<4.45'],
                                        stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT,
                                        text=True)
                                    for line in proc.stdout:
                                        line = line.rstrip()
                                        if line:
                                            sd.after(0, lambda l=line:
                                                     status_var.set(l[:80]))
                                            sd.after(0, lambda l=line: _rlog(l))
                                    proc.wait(timeout=600)
                                    if proc.returncode == 0:
                                        sd.after(0, lambda: status_var.set(
                                            "whisperx installed — click Start"))
                                        sd.after(0, lambda: _rlog(
                                            "whisperx installed successfully"))
                                        sd.after(0, lambda: app.add_log(
                                            "whisperx installed successfully",
                                            'SUCCESS'))
                                    else:
                                        sd.after(0, lambda: status_var.set(
                                            "whisperx install failed"))
                                        sd.after(0, lambda: _rlog(
                                            "Install failed — check log above"))
                                except Exception as _e:
                                    sd.after(0, lambda: status_var.set(
                                        f"Install error: {_e}"))
                                    sd.after(0, lambda: _rlog(f"Error: {_e}"))
                                finally:
                                    def _reset_after_install():
                                        start_btn.configure(state='normal')
                                        if _install_pbar:
                                            _install_pbar.stop()
                                            _install_pbar.configure(
                                                mode='determinate')
                                            progress_var.set(0)
                                    sd.after(0, _reset_after_install)

                            import threading as _inst_threading
                            _inst_threading.Thread(
                                target=_do_whisperx_install,
                                daemon=True).start()
                            return  # exit _start(); user clicks Start after install
                        else:
                            return

                start_btn.configure(state='disabled')
                apply_btn.configure(state='disabled')
                cancel_event.clear()
                # Capture Tk variables on main thread before entering background thread
                lang = lang_var.get().strip() or None
                model = model_var.get()
                _scan_mode = scan_mode_var.get()
                if _scan_mode == 'direct':
                    engine_value = 'whisperx-align'
                else:
                    engine_value = engine_var.get()
                is_full_scan = _scan_mode == 'full'
                _seg_str = segments_var.get().strip()
                _len_str = sample_len_var.get().strip()
                n_segs = int(_seg_str) if _seg_str.isdigit() else 3
                s_mins = int(_len_str) if _len_str.isdigit() else 5
                _ft_str = finetune_var.get().strip().lstrip('+')
                finetune_ms = int(_ft_str) if _ft_str.lstrip('-').isdigit() else 400
                if is_full_scan or _scan_mode == 'direct':
                    n_segs = 0  # signal for full scan
                    s_mins = 0

                import time as _sync_time
                _last_ui_update = [0]

                def _progress(msg):
                    # Throttle UI updates to max 4 per second to avoid flooding Tk event queue
                    now = _sync_time.monotonic()
                    is_milestone = ('segment' in msg.lower() and '/' in msg) or \
                                   'Matched' in msg or 'Loading' in msg or \
                                   'Extracting' in msg or 'Transcribed' in msg or \
                                   'Aligning' in msg or 'alignment' in msg.lower() or \
                                   'WhisperX' in msg or 'Falling back' in msg or \
                                   'failed' in msg.lower() or 'complete' in msg.lower() or \
                                   'error' in msg.lower() or 'RESULT' in msg or \
                                   'Done' in msg or '===' in msg or 'Drift' in msg or \
                                   'Sync' in msg
                    if not is_milestone and (now - _last_ui_update[0]) < 0.25:
                        return
                    _last_ui_update[0] = now

                    def _do_update():
                        status_var.set(msg)
                        _rlog(msg)
                        import re as _re
                        m = _re.search(r'segment (\d+)/(\d+)', msg, _re.IGNORECASE)
                        if m:
                            seg_n, seg_t = int(m.group(1)), int(m.group(2))
                            progress_var.set((seg_n / seg_t) * 100)
                        m2 = _re.search(r'Matching cue (\d+)/(\d+)', msg)
                        if m2:
                            mc, mt = int(m2.group(1)), int(m2.group(2))
                            progress_var.set((mc / mt) * 100)
                        elif 'Extracting audio' in msg:
                            progress_var.set(0)
                    sd.after(0, _do_update)

                def _set_start_enabled():
                    start_btn.configure(state='normal')
                def _set_apply_enabled():
                    apply_btn.configure(state='normal')
                    progress_var.set(100)

                def _run():
                    try:
                        result = smart_sync(vp, cues, model_size=model,
                                            language=lang,
                                            num_segments=n_segs,
                                            sample_minutes=s_mins,
                                            progress_callback=_progress,
                                            cancel_event=cancel_event,
                                            engine=engine_value)
                    except Exception as _e:
                        _progress(f"Error: {_e}")
                        result = None
                    sync_result[0] = result

                    # Display results via _progress (proven reliable)
                    import time as _t
                    _t.sleep(0.3)  # let queued UI updates flush

                    if cancel_event.is_set():
                        _progress("Sync cancelled by user")
                    elif result:
                        try:
                            ro = result['offset_ms']
                            rd = result['drift_ms']
                            rm = result['matches']
                            sign = '+' if ro > 0 else ''
                            _progress(f"{'='*40}")
                            _progress(f"RESULT: Offset: {sign}{ro}ms ({sign}{ro/1000:.1f}s)")
                            _progress(f"Drift: {rd:+d}ms")
                            _progress(f"Matched: {len(rm)}/{len(cues)} cues")
                            _progress(f"{'='*40}")
                            for ci, wt, ct, sim, txt in rm[:10]:
                                _progress(f"  #{ci+1} sim={sim:.0%} "
                                          f"sub={ms_to_srt_ts(ct)[:8]} "
                                          f"audio={ms_to_srt_ts(wt)[:8]} "
                                          f"\"{txt}\"")
                            _progress(f"Done — click Apply Sync to apply {sign}{ro}ms offset")
                            sd.after(0, lambda: apply_btn.configure(state='normal'))
                            sd.after(0, lambda: retime_btn.configure(state='normal'))
                            sd.after(0, lambda: progress_var.set(100))
                        except Exception as _e:
                            _progress(f"Error displaying results: {_e}")
                    else:
                        _progress("Sync failed — no results")

                    sd.after(0, _set_start_enabled)

                t = threading.Thread(target=_run, daemon=True)
                t.start()

            def _get_finetune():
                _ft = finetune_var.get().strip().lstrip('+')
                return int(_ft) if _ft.lstrip('-').isdigit() else 400

            def _do_backup():
                backup_path = None
                if current_path[0] and os.path.isfile(current_path[0]):
                    base, ext = os.path.splitext(current_path[0])
                    backup_path = f"{base}_presync{ext}"
                    try:
                        write_srt_file(cues, backup_path)
                        _rlog(f"Backup saved: {os.path.basename(backup_path)}")
                        app.add_log(f"Pre-sync backup: {backup_path}", 'INFO')
                    except Exception as e:
                        _rlog(f"Warning: could not save backup: {e}")
                return backup_path

            def _apply():
                nonlocal cues
                if not sync_result[0]:
                    return
                offset = sync_result[0]['offset_ms']
                ft = _get_finetune()
                total_offset = offset + ft

                backup_path = _do_backup()

                # Always apply from the pre-sync snapshot so fine-tune is repeatable
                import copy as _copy
                push_undo()
                if pre_sync_cues[0] is not None:
                    cues = _copy.deepcopy(pre_sync_cues[0])
                cues = shift_timestamps(cues, total_offset)
                refresh_tree(cues)
                sign = '+' if total_offset > 0 else ''
                app.add_log(f"Smart Sync applied: {sign}{total_offset}ms "
                             f"(offset {offset:+d}ms + fine-tune {ft:+d}ms)", 'SUCCESS')
                _rlog(f"\nApplied: {sign}{total_offset}ms (offset {offset:+d} + fine-tune {ft:+d})")
                if backup_path:
                    _rlog(f"Original saved as: {os.path.basename(backup_path)}")
                status_var.set(f"Sync applied: {sign}{total_offset}ms")

            def _retime():
                nonlocal cues
                if not sync_result[0]:
                    return
                result = sync_result[0]
                matched = result['matches']
                ft = _get_finetune()

                backup_path = _do_backup()

                # Always retime from the pre-sync snapshot so fine-tune is repeatable
                import copy as _copy
                push_undo()
                if pre_sync_cues[0] is not None:
                    cues = _copy.deepcopy(pre_sync_cues[0])
                cues = retime_subtitles(cues, matched)
                # Apply fine-tune offset after re-timing
                if ft != 0:
                    cues = shift_timestamps(cues, ft)
                refresh_tree(cues)
                ft_msg = f" + fine-tune {ft:+d}ms" if ft else ""
                app.add_log(f"Re-timed {len(cues)} cues using {len(matched)} anchors{ft_msg}",
                             'SUCCESS')
                _rlog(f"\nRe-timed {len(cues)} cues using {len(matched)} anchors{ft_msg}")
                if backup_path:
                    _rlog(f"Original saved as: {os.path.basename(backup_path)}")
                status_var.set(f"Re-timed using {len(matched)} anchors{ft_msg}")

            def _cancel():
                cancel_event.set()
                status_var.set("Cancelling...")

            start_btn = ttk.Button(btn_f, text="▶ Start", command=_start, width=8)
            start_btn.pack(side='left', padx=2)
            apply_btn = ttk.Button(btn_f, text="Apply Sync", command=_apply,
                                    width=10, state='disabled')
            apply_btn.pack(side='left', padx=2)
            retime_btn = ttk.Button(btn_f, text="Re-time All", command=_retime,
                                     width=10, state='disabled')
            retime_btn.pack(side='left', padx=2)
            ttk.Button(btn_f, text="Cancel", command=_cancel, width=8).pack(side='left', padx=2)

            def _save_from_sync():
                do_save_file()
                _rlog("Saved.")
                status_var.set("Saved")

            ttk.Button(btn_f, text="💾 Save", command=_save_from_sync,
                       width=6).pack(side='right', padx=2)
            ttk.Button(btn_f, text="Close", command=sd.destroy, width=6).pack(side='right', padx=2)

        # ══════════════════════════════════════════════════════════════════════
        # Placeholder — shown when no file is loaded
        # ══════════════════════════════════════════════════════════════════════
        placeholder = ttk.Frame(editor)
        placeholder.pack(fill='both', expand=True)
        ph_label = ttk.Label(placeholder,
                             text="Open a subtitle file to begin editing\n\n"
                                  "File → Open   (Ctrl+O)\n\n"
                                  "or drag and drop a subtitle or video file here",
                             font=('Helvetica', 14),
                             foreground='gray',
                             justify='center',
                             anchor='center')
        ph_label.pack(expand=True)

        # ══════════════════════════════════════════════════════════════════════
        # Content frame — hidden until a file is loaded
        # ══════════════════════════════════════════════════════════════════════
        content_frame = ttk.Frame(editor)

        # ── Search & Replace toolbar ──
        find_var = tk.StringVar()
        replace_var = tk.StringVar()
        use_regex = tk.BooleanVar(value=False)
        wrap_around = tk.BooleanVar(value=False)

        def do_find():
            term = find_var.get()
            if not term:
                refresh_tree(cues)
                return
            matches = []
            for i, cue in enumerate(cues):
                try:
                    if use_regex.get():
                        if re.search(term, cue['text'], re.IGNORECASE):
                            matches.append(i)
                    else:
                        if term.lower() in cue['text'].lower():
                            matches.append(i)
                except re.error:
                    pass
            refresh_tree(cues, search_indices=matches)
            if matches:
                first_idx = matches[0]
                first = str(first_idx)
                def _scroll_to_match():
                    tree.selection_set(first)
                    # Scroll so the match is near the middle of the view, not at the edge
                    # Aim a few rows past the match so it's comfortably visible
                    ahead = min(first_idx + 5, len(cues) - 1)
                    tree.see(str(ahead))
                    tree.after(50, lambda: (tree.see(first), tree.selection_set(first)))
                tree.after_idle(_scroll_to_match)
            app.add_log(f"Search: {len(matches)} matches for '{term}'", 'INFO')

        def do_replace_one():
            """Replace the first occurrence of search term from current selection."""
            nonlocal cues
            term = find_var.get()
            repl = replace_var.get()
            if not term:
                return
            sel = tree.selection()
            start_idx = int(sel[0]) if sel else 0
            if wrap_around.get():
                order = list(range(start_idx, len(cues))) + list(range(0, start_idx))
            else:
                order = list(range(start_idx, len(cues)))
            for i in order:
                old_text = cues[i]['text']
                try:
                    if use_regex.get():
                        new_text = re.sub(term, lambda m: repl, old_text, count=1,
                                          flags=re.IGNORECASE)
                    else:
                        # Case-insensitive literal find + replace (preserves rest of line)
                        pos = old_text.lower().find(term.lower())
                        if pos >= 0:
                            new_text = old_text[:pos] + repl + old_text[pos + len(term):]
                        else:
                            new_text = old_text
                except re.error:
                    continue
                if new_text != old_text:
                    push_undo()
                    cues[i]['text'] = new_text
                    if not new_text.strip():
                        del cues[i]
                    refresh_tree(cues)
                    # Select and scroll to the next match
                    if wrap_around.get():
                        next_order = list(range(i + 1, len(cues))) + list(range(0, i + 1))
                    else:
                        next_order = list(range(i + 1, len(cues)))
                    for j in next_order:
                        try:
                            if use_regex.get():
                                if re.search(term, cues[j]['text'], re.IGNORECASE):
                                    tree.see(str(j))
                                    tree.selection_set(str(j))
                                    break
                            else:
                                if term.lower() in cues[j]['text'].lower():
                                    tree.see(str(j))
                                    tree.selection_set(str(j))
                                    break
                        except (re.error, IndexError):
                            pass
                    app.add_log(f"Replaced 1 occurrence of '{term}' → '{repl}'", 'INFO')
                    return
            app.add_log(f"No more matches found for '{term}'", 'INFO')

        def do_replace_all():
            nonlocal cues
            term = find_var.get()
            repl = replace_var.get()
            if not term:
                return
            push_undo()
            count = 0
            for cue in cues:
                old_text = cue['text']
                try:
                    if use_regex.get():
                        new_text = re.sub(term, lambda m: repl, old_text, flags=re.IGNORECASE)
                    else:
                        # Case-insensitive literal replace all
                        new_text = old_text
                        lower_text = new_text.lower()
                        lower_term = term.lower()
                        result = []
                        pos = 0
                        while True:
                            idx = lower_text.find(lower_term, pos)
                            if idx == -1:
                                result.append(new_text[pos:])
                                break
                            result.append(new_text[pos:idx])
                            result.append(repl)
                            pos = idx + len(term)
                        new_text = ''.join(result)
                except re.error:
                    continue
                if new_text != old_text:
                    cue['text'] = new_text
                    count += 1
            cues = [c for c in cues if c['text'].strip()]
            refresh_tree(cues)
            app.add_log(f"Replaced {count} occurrence(s) of '{term}' → '{repl}'", 'INFO')

        search_frame = ttk.Frame(content_frame, padding=(10, 4, 10, 4))
        search_frame.pack(fill='x')

        def _add_entry_context_menu(entry):
            """Attach a right-click Cut/Copy/Paste menu to a ttk.Entry."""
            menu = tk.Menu(entry, tearoff=0)
            menu.add_command(label="Cut",
                command=lambda: entry.event_generate('<<Cut>>'))
            menu.add_command(label="Copy",
                command=lambda: entry.event_generate('<<Copy>>'))
            menu.add_command(label="Paste",
                command=lambda: entry.event_generate('<<Paste>>'))
            menu.add_separator()
            menu.add_command(label="Select All",
                command=lambda: (entry.select_range(0, 'end'),
                                 entry.icursor('end')))
            def _show(event):
                menu.tk_popup(event.x_root, event.y_root)
            entry.bind('<Button-3>', _show)

        ttk.Label(search_frame, text="Find:").pack(side='left')
        find_entry = ttk.Entry(search_frame, textvariable=find_var, width=20)
        find_entry.pack(side='left', padx=(2, 6))
        find_entry.bind('<Return>', lambda e: do_find())
        _add_entry_context_menu(find_entry)

        ttk.Label(search_frame, text="Replace:").pack(side='left')
        replace_entry = ttk.Entry(search_frame, textvariable=replace_var, width=20)
        replace_entry.pack(side='left', padx=(2, 6))
        _add_entry_context_menu(replace_entry)

        ttk.Button(search_frame, text="Find", command=do_find).pack(side='left', padx=2)
        ttk.Button(search_frame, text="Replace",
                   command=do_replace_one).pack(side='left', padx=2)
        ttk.Button(search_frame, text="Replace All",
                   command=do_replace_all).pack(side='left', padx=2)
        ttk.Checkbutton(search_frame, text="Wrap",
                        variable=wrap_around).pack(side='left', padx=(6, 2))

        editor.bind('<Control-f>', lambda e: find_entry.focus_set())
        editor.bind('<Control-F>', lambda e: find_entry.focus_set())

        ttk.Separator(content_frame, orient='horizontal').pack(fill='x')

        # ── PanedWindow: (Video + Treeview) / Waveform Timeline ──
        paned = tk.PanedWindow(content_frame, orient='vertical',
                               sashwidth=6, sashrelief='raised')
        paned.pack(fill='both', expand=True, padx=10, pady=(4, 0))

        # ── Top section: horizontal split (Video | Treeview) ──
        top_paned = tk.PanedWindow(paned, orient='horizontal',
                                    sashwidth=6, sashrelief='raised')

        # ── Video panel ──
        video_panel = ttk.Frame(top_paned, relief='sunken', borderwidth=1)
        video_embed_frame = tk.Frame(video_panel, bg='black',
                                      width=320, height=240)
        video_embed_frame.pack(fill='both', expand=True)
        video_embed_frame.pack_propagate(False)
        _video_placeholder = ttk.Label(video_embed_frame,
                                        text="No video loaded\n\nUse View → Load Waveform\nto load a video file",
                                        anchor='center', justify='center')
        _video_placeholder.place(relx=0.5, rely=0.5, anchor='center')
        video_visible = [False]

        # ── Treeview ──
        tree_frame = ttk.Frame(top_paned)

        tree_scroll_y = ttk.Scrollbar(tree_frame, orient='vertical')
        tree_scroll_y.pack(side='right', fill='y')

        tree = ttk.Treeview(tree_frame, columns=('num', 'time', 'text'),
                            show='headings', yscrollcommand=tree_scroll_y.set,
                            selectmode='extended')
        tree_scroll_y.config(command=tree.yview)

        tree.heading('num', text='#')
        tree.heading('time', text='Timestamp')
        tree.heading('text', text='Text')
        tree.column('num', width=40, minwidth=30, stretch=False)
        tree.column('time', width=260, minwidth=220, stretch=False)
        tree.column('text', width=500, minwidth=200, stretch=True)
        tree.pack(fill='both', expand=True)

        # Color coding
        tree.tag_configure(TAG_MODIFIED, background='#fff3cd')
        tree.tag_configure(TAG_HI, background='#cce5ff')
        tree.tag_configure(TAG_TAGS, background='#f8d7da')
        tree.tag_configure(TAG_LONG, background='#ffe0b2')
        tree.tag_configure(TAG_SEARCH, background='#c8e6c9')
        tree.tag_configure(TAG_SPELL, background='#f5c6cb')

        # Mousewheel scrolling
        def on_tree_mousewheel(event):
            tree.yview_scroll(int(-1 * (event.delta / 120)), 'units')
            return 'break'

        def on_tree_scroll_up(event):
            tree.yview_scroll(-3, 'units')
            return 'break'

        def on_tree_scroll_down(event):
            tree.yview_scroll(3, 'units')
            return 'break'

        tree.bind('<MouseWheel>', on_tree_mousewheel)
        tree.bind('<Button-4>', on_tree_scroll_up)
        tree.bind('<Button-5>', on_tree_scroll_down)

        # ── Inline edit on double-click ──
        edit_entry = None

        def on_double_click(event):
            nonlocal edit_entry, cues
            item = tree.identify_row(event.y)
            col = tree.identify_column(event.x)
            if not item or col != '#3':
                return
            bbox = tree.bbox(item, col)
            if not bbox:
                return
            x, y, w, h = bbox
            idx = int(item)

            if edit_entry:
                edit_entry.destroy()

            push_undo()
            edit_entry = tk.Text(tree_frame, wrap='word', height=3)
            edit_entry.place(x=x, y=y, width=w, height=max(h, 60))
            edit_entry.insert('1.0', cues[idx]['text'])
            edit_entry.focus_set()
            edit_entry.tag_configure('sel', background='#4a90d9')

            def save_edit(e=None):
                nonlocal edit_entry
                new_text = edit_entry.get('1.0', 'end-1c').strip()
                if new_text:
                    cues[idx]['text'] = new_text
                    display = new_text.replace('\n', ' \\n ')
                    tree.set(item, 'text', display)
                    orig_text = original_cues[idx]['text'] if idx < len(original_cues) else None
                    ctags = _classify_cue(cues[idx], orig_text)
                    row_tag = ''
                    for t in (TAG_MODIFIED, TAG_HI, TAG_TAGS, TAG_LONG):
                        if t in ctags:
                            row_tag = t
                            break
                    tree.item(item, tags=(row_tag,) if row_tag else ())
                else:
                    del cues[idx]
                    refresh_tree(cues)
                edit_entry.destroy()
                edit_entry = None
                # Update stats
                deleted_count.set(len(original_cues) - len(cues))
                mod = sum(1 for i, c in enumerate(cues) if i < len(original_cues)
                          and c['text'] != original_cues[i]['text'])
                modified_count.set(mod)
                long_count = sum(1 for c in cues
                                 if any(len(l) > MAX_CHARS_PER_LINE
                                        for l in c['text'].split('\n')))
                stats_parts = [
                    f"{len(cues)} entries",
                    f"{modified_count.get()} modified",
                    f"{deleted_count.get()} removed",
                ]
                if long_count:
                    stats_parts.append(f"{long_count} long lines")
                stats_label.configure(text=" │ ".join(stats_parts))

            def cancel_edit(e=None):
                nonlocal edit_entry
                if edit_entry:
                    edit_entry.destroy()
                    edit_entry = None
                    if undo_stack:
                        undo_stack.pop()

            edit_entry.bind('<Escape>', cancel_edit)
            edit_entry.bind('<Control-Return>', save_edit)
            edit_entry.bind('<Tab>', save_edit)

            # Right-click context menu for copy/paste
            edit_ctx = tk.Menu(edit_entry, tearoff=0)

            def _edit_action(action):
                """Perform an edit action and refocus the edit widget."""
                if not edit_entry:
                    return
                if action == 'cut':
                    edit_entry.event_generate('<<Cut>>')
                elif action == 'copy':
                    edit_entry.event_generate('<<Copy>>')
                elif action == 'paste':
                    edit_entry.event_generate('<<Paste>>')
                elif action == 'select_all':
                    edit_entry.tag_add('sel', '1.0', 'end')
                    edit_entry.mark_set('insert', 'end')
                edit_entry.focus_force()

            edit_ctx.add_command(label="Cut", command=lambda: _edit_action('cut'))
            edit_ctx.add_command(label="Copy", command=lambda: _edit_action('copy'))
            edit_ctx.add_command(label="Paste", command=lambda: _edit_action('paste'))
            edit_ctx.add_separator()
            edit_ctx.add_command(label="Select All",
                                command=lambda: _edit_action('select_all'))

            _edit_ctx_open = [False]

            def show_edit_ctx(event):
                _edit_ctx_open[0] = True
                def on_menu_close():
                    _edit_ctx_open[0] = False
                    if edit_entry:
                        edit_entry.focus_force()
                edit_ctx.tk_popup(event.x_root, event.y_root)
                # tk_popup is blocking on some platforms; schedule cleanup
                edit_entry.after(50, on_menu_close)
                return 'break'
            edit_entry.bind('<Button-3>', show_edit_ctx)

            def on_focus_out(e):
                if not edit_entry:
                    return
                # Wait for context menu interactions to complete
                def deferred_save():
                    if not edit_entry:
                        return
                    if _edit_ctx_open[0]:
                        # Menu still active, check again later
                        edit_entry.after(200, deferred_save)
                        return
                    try:
                        if edit_entry.focus_get() == edit_entry:
                            return  # focus came back, don't save
                    except Exception:
                        pass
                    save_edit()
                edit_entry.after(300, deferred_save)
            edit_entry.bind('<FocusOut>', on_focus_out)

        tree.bind('<Double-1>', on_double_click)

        # ── Right-click context menu ──
        ctx_menu = tk.Menu(editor, tearoff=0)
        def insert_cue(position):
            """Insert a blank cue above or below the selected cue."""
            nonlocal cues
            selected = tree.selection()
            if not selected:
                return
            idx = int(selected[0])
            ref = cues[idx]

            if position == 'above':
                # Place the new cue just before the selected one
                ref_start_ms = srt_ts_to_ms(ref['start'])
                new_end_ms = max(ref_start_ms - 1, 0)
                new_start_ms = max(new_end_ms - 2000, 0)
                insert_idx = idx
            else:
                # Place the new cue just after the selected one
                ref_end_ms = srt_ts_to_ms(ref['end'])
                new_start_ms = ref_end_ms + 1
                new_end_ms = new_start_ms + 2000
                insert_idx = idx + 1

            push_undo()
            new_cue = {
                'index': 0,
                'start': ms_to_srt_ts(new_start_ms),
                'end': ms_to_srt_ts(new_end_ms),
                'text': ' ',
            }
            cues.insert(insert_idx, new_cue)
            refresh_tree(cues)
            # Select the new cue and scroll to it
            tree.see(str(insert_idx))
            tree.selection_set(str(insert_idx))

        ctx_menu.add_command(label="✂ Split cue", command=split_selected)
        ctx_menu.add_command(label="⊕ Join selected cues", command=join_selected)
        ctx_menu.add_separator()
        ctx_menu.add_command(label="⤒ Insert line above", command=lambda: insert_cue('above'))
        ctx_menu.add_command(label="⤓ Insert line below", command=lambda: insert_cue('below'))
        ctx_menu.add_separator()
        ctx_menu.add_command(label="🗑 Delete selected", command=delete_selected)

        def show_context_menu(event):
            item = tree.identify_row(event.y)
            if item and item not in tree.selection():
                tree.selection_set(item)
            ctx_menu.tk_popup(event.x_root, event.y_root)

        tree.bind('<Button-3>', show_context_menu)

        # ── Waveform Timeline ──
        timeline_frame = ttk.Frame(paned)
        timeline_visible = [False]

        def _on_timeline_cue_modified(cue_idx, new_start_ms, new_end_ms):
            """Called when a cue is dragged on the timeline."""
            if cue_idx < len(cues):
                cues[cue_idx]['start'] = ms_to_srt_ts(int(new_start_ms))
                cues[cue_idx]['end'] = ms_to_srt_ts(int(new_end_ms))
                refresh_tree(cues)

        def _on_timeline_selection(cue_idx):
            """Called when a cue is clicked on the timeline."""
            iid = str(cue_idx)
            if tree.exists(iid):
                tree.selection_set(iid)
                tree.see(iid)
                tree.focus(iid)

        timeline = WaveformTimeline(
            timeline_frame,
            cues_fn=lambda: cues,
            on_cue_modified=_on_timeline_cue_modified,
            on_selection_changed=_on_timeline_selection,
            push_undo=push_undo,
            log_fn=app.add_log,
            video_frame=video_embed_frame,
        )
        timeline.pack(fill='both', expand=True)

        # Tree → Timeline selection sync
        def _on_tree_select(event):
            sel = tree.selection()
            if sel:
                try:
                    idx = int(sel[0])
                    timeline.select_cue(idx)
                    # Don't scroll during drag — it shifts coordinates
                    if not timeline._drag:
                        timeline.scroll_to_cue(idx)
                except (ValueError, IndexError):
                    pass

        tree.bind('<<TreeviewSelect>>', _on_tree_select)

        # Build paned layout: top_paned (video | tree) in vertical paned with timeline
        top_paned.add(tree_frame, stretch='always')
        paned.add(top_paned, stretch='always')

        def _show_video():
            if not video_visible[0]:
                top_paned.add(video_panel, before=tree_frame, stretch='never',
                              width=360)
                video_visible[0] = True

        def _hide_video():
            if video_visible[0]:
                top_paned.forget(video_panel)
                video_visible[0] = False

        def _show_timeline():
            if not timeline_visible[0]:
                paned.add(timeline_frame, stretch='always')
                # Set initial sash position: 65% top, 35% timeline
                paned.update_idletasks()
                total_h = paned.winfo_height()
                if total_h > 100:
                    paned.sash_place(0, 0, int(total_h * 0.65))
                timeline_visible[0] = True

        def _hide_timeline():
            if timeline_visible[0]:
                paned.forget(timeline_frame)
                timeline_visible[0] = False

        def _toggle_timeline():
            if timeline_visible[0]:
                _hide_timeline()
            else:
                _show_timeline()

        def _load_waveform_for_video(video_path):
            """Load waveform from video, show the timeline and video panel."""
            if not video_path or not os.path.isfile(video_path):
                return
            _show_video()
            _video_placeholder.place_forget()
            _show_timeline()
            if not timeline.is_loaded:
                timeline.load_audio(video_path)

        # ── Status bar ──
        status_frame = ttk.Frame(content_frame, padding=(10, 6, 10, 6))
        status_frame.pack(fill='x')

        stats_label = ttk.Label(status_frame, text="0 entries")
        stats_label.pack(side='left')

        ttk.Button(status_frame, text="💾 Save", command=do_save_file).pack(side='right', padx=(4, 0))
        ttk.Button(status_frame, text="📤 Export SRT", command=do_export).pack(side='right', padx=4)

        # ── Refresh tree function ──
        def refresh_tree(new_cues, search_indices=None):
            nonlocal cues
            cues = new_cues
            tree.delete(*tree.get_children())
            search_set = set(search_indices or [])
            for i, cue in enumerate(cues):
                display = cue['text'].replace('\n', ' \\n ')
                ts = f"{cue['start']} → {cue['end']}"
                if cue['text'] in _orig_texts:
                    orig_text = cue['text']
                else:
                    orig_text = ''
                ctags = _classify_cue(cue, orig_text)
                if i in search_set:
                    ctags.add(TAG_SEARCH)
                if TAG_SEARCH in ctags:
                    row_tag = TAG_SEARCH
                elif i in spell_error_indices:
                    row_tag = TAG_SPELL
                elif TAG_MODIFIED in ctags:
                    row_tag = TAG_MODIFIED
                elif TAG_HI in ctags:
                    row_tag = TAG_HI
                elif TAG_TAGS in ctags:
                    row_tag = TAG_TAGS
                elif TAG_LONG in ctags:
                    row_tag = TAG_LONG
                else:
                    row_tag = ''
                tree.insert('', 'end', iid=str(i),
                            values=(i + 1, ts, display),
                            tags=(row_tag,) if row_tag else ())
            deleted_count.set(len(original_cues) - len(cues))
            mod = sum(1 for i, c in enumerate(cues) if i < len(original_cues)
                      and c['text'] != original_cues[i]['text'])
            modified_count.set(mod)
            long_count = sum(1 for c in cues
                             if any(len(l) > MAX_CHARS_PER_LINE for l in c['text'].split('\n')))
            stats_parts = [
                f"{len(cues)} entries",
                f"{modified_count.get()} modified",
                f"{deleted_count.get()} removed",
            ]
            if long_count:
                stats_parts.append(f"{long_count} long lines")
            stats_label.configure(text=" │ ".join(stats_parts))
            # Refresh waveform timeline cue blocks and live subtitles
            if timeline.is_loaded:
                timeline.refresh()
                timeline.reload_subtitles()

        # Delete key shortcut
        editor.bind('<Delete>', lambda e: None if isinstance(e.widget, tk.Text) else delete_selected())

        # ── Disable menus until a file is loaded ──
        def _set_menus_state(state):
            for menu_label in ('Tools', 'Edit', 'Timing', 'View'):
                try:
                    idx = menubar.index(menu_label)
                    menubar.entryconfigure(idx, state=state)
                except (tk.TclError, ValueError):
                    pass
            # Disable save/export in File menu (indices 2=Save, 3=Save As, 4=Export)
            for i in (2, 3, 4):
                try:
                    file_menu.entryconfigure(i, state=state)
                except tk.TclError:
                    pass

        _set_menus_state('disabled')

        # ── Cleanup temp files on editor close ──
        def _has_unsaved_changes():
            """Check if cues have been modified since last load/save."""
            if len(cues) != len(original_cues):
                return True
            for c, o in zip(cues, original_cues):
                if (c.get('start') != o.get('start') or
                        c.get('end') != o.get('end') or
                        c.get('text') != o.get('text')):
                    return True
            return False

        def on_editor_close():
            if cues and _has_unsaved_changes():
                result = messagebox.askyesnocancel(
                    "Unsaved Changes",
                    "You have unsaved changes.\n\n"
                    "Would you like to save before closing?",
                    parent=editor)
                if result is None:
                    return  # Cancel — don't close
                if result:
                    do_save_file()  # Save first
            timeline.cleanup()
            if video_source[0] and video_source[0].get('temp_srt'):
                try:
                    os.unlink(video_source[0]['temp_srt'])
                except OSError:
                    pass
            editor.destroy()
            # In standalone mode, quit the entire app
            if getattr(app, '_standalone_mode', False):
                app.root.destroy()

        editor.protocol('WM_DELETE_WINDOW', on_editor_close)

        # Auto-open file passed via command line (e.g. "Open with" from file manager)
        _start_path = getattr(app, '_open_file_on_start', None)
        if _start_path and os.path.isfile(_start_path):
            def _auto_open():
                ext = Path(_start_path).suffix.lower()
                if ext in VIDEO_EXTENSIONS:
                    load_video_subtitle(_start_path)
                else:
                    load_file(_start_path)
            editor.after(100, _auto_open)
            app._open_file_on_start = None  # only open once

        if not getattr(app, '_standalone_mode', False):
            editor.wait_window()

    # ── Media Processor ──────────────────────────────────────────────────────


def show_subtitle_editor(app, filepath, stream_index, file_info,
                             external_sub_path=None):
        """Show subtitle text editor for a subtitle stream or external file.

        For internal streams: extracts from filepath using stream_index.
        For external files: pass external_sub_path (stream_index is ignored).

        Full-featured editor with filters, search/replace, timing tools,
        undo/redo, duplicate detection, video preview, and export.
        """
        import tempfile
        import copy

        is_external = external_sub_path is not None

        if is_external:
            # Read external subtitle file directly
            sub_path = external_sub_path
            ext = Path(sub_path).suffix.lower()
            if ext in ('.srt',):
                # Already SRT — read directly
                try:
                    with open(sub_path, 'r', encoding='utf-8', errors='replace') as f:
                        srt_text = f.read()
                except Exception as e:
                    app.add_log(f"Failed to read subtitle file: {e}", 'ERROR')
                    return
            else:
                # Convert to SRT via ffmpeg
                tmp_srt = tempfile.NamedTemporaryFile(suffix='.srt', delete=False,
                                                       mode='w', encoding='utf-8')
                tmp_srt.close()
                cmd = ['ffmpeg', '-y', '-i', sub_path, '-c:s', 'srt', tmp_srt.name]
                try:
                    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                    if result.returncode != 0:
                        app.add_log(f"Failed to convert subtitle for editing: "
                                     f"{result.stderr[-200:]}", 'ERROR')
                        os.unlink(tmp_srt.name)
                        return
                except Exception as e:
                    app.add_log(f"Convert error: {e}", 'ERROR')
                    os.unlink(tmp_srt.name)
                    return
                with open(tmp_srt.name, 'r', encoding='utf-8', errors='replace') as f:
                    srt_text = f.read()
                os.unlink(tmp_srt.name)
        else:
            # Extract internal subtitle stream to temp SRT
            tmp_srt = tempfile.NamedTemporaryFile(suffix='.srt', delete=False, mode='w',
                                                   encoding='utf-8')
            tmp_srt.close()
            cmd = ['ffmpeg', '-y', '-i', filepath, '-map', f'0:{stream_index}',
                   '-c:s', 'srt', tmp_srt.name]
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                if result.returncode != 0:
                    app.add_log(f"Failed to extract subtitle #{stream_index} for editing: "
                                 f"{result.stderr[-200:]}", 'ERROR')
                    os.unlink(tmp_srt.name)
                    return
            except Exception as e:
                app.add_log(f"Extract error: {e}", 'ERROR')
                os.unlink(tmp_srt.name)
                return
            with open(tmp_srt.name, 'r', encoding='utf-8', errors='replace') as f:
                srt_text = f.read()
            os.unlink(tmp_srt.name)

        cues = parse_srt(srt_text)
        if not cues:
            label = os.path.basename(external_sub_path) if is_external else f"stream #{stream_index}"
            app.add_log(f"No subtitle cues found in {label}", 'WARNING')
            return

        # Keep original for undo-all
        original_cues = [dict(c) for c in cues]

        # ── Undo / Redo stack ──
        undo_stack = []   # list of previous cue states
        redo_stack = []

        def push_undo():
            """Save current state to undo stack."""
            undo_stack.append([dict(c) for c in cues])
            redo_stack.clear()  # new action clears redo

        def do_undo(event=None):
            nonlocal cues
            if not undo_stack:
                return
            redo_stack.append([dict(c) for c in cues])
            cues = undo_stack.pop()
            refresh_tree(cues)

        def do_redo(event=None):
            nonlocal cues
            if not redo_stack:
                return
            undo_stack.append([dict(c) for c in cues])
            cues = redo_stack.pop()
            refresh_tree(cues)

        # ── Editor Window ──
        editor = tk.Toplevel(app.root)
        if is_external:
            editor.title(f"Edit Subtitles — {os.path.basename(external_sub_path)}")
        else:
            editor.title(f"Edit Subtitles — Stream #{stream_index} — {os.path.basename(filepath)}")
        editor.geometry(scaled_geometry(editor, 950, 650))
        editor.transient(app.root)
        editor.grab_set()
        app._center_on_main(editor)
        editor.resizable(True, True)

        # Keyboard shortcuts
        editor.bind('<Control-z>', do_undo)
        editor.bind('<Control-y>', do_redo)
        editor.bind('<Control-Z>', do_undo)
        editor.bind('<Control-Y>', do_redo)

        # Track state
        modified_count = tk.IntVar(value=0)
        deleted_count = tk.IntVar(value=0)

        # ── Color tag styles for treeview ──
        # Will be configured after tree is created; define names here
        TAG_MODIFIED = 'modified'
        TAG_HI = 'has_hi'
        TAG_TAGS = 'has_tags'
        TAG_LONG = 'long_line'
        TAG_SEARCH = 'search_match'
        TAG_SPELL = 'has_spelling'

        # ── Spell check state ──
        spell_error_indices = set()

        def _classify_cue(cue, orig_text=None):
            """Return set of tag names for a cue based on its content."""
            tags = set()
            text = cue['text']
            # Modified from original
            if orig_text is not None and text != orig_text:
                tags.add(TAG_MODIFIED)
            # Contains HI annotations
            if re.search(r'\[.*?\]|\(.*?\)|♪|♫', text):
                tags.add(TAG_HI)
            # Contains HTML/ASS tags
            if re.search(r'<[^>]+>|\{\\[^}]+\}', text):
                tags.add(TAG_TAGS)
            # Long lines
            for line in text.split('\n'):
                if len(line) > MAX_CHARS_PER_LINE:
                    tags.add(TAG_LONG)
                    break
            return tags

        # Set of original texts for modified detection (index-independent)
        _orig_texts = {c['text'] for c in original_cues}

        def refresh_tree(new_cues, search_indices=None):
            """Reload the treeview with updated cues."""
            nonlocal cues
            cues = new_cues
            tree.delete(*tree.get_children())
            search_set = set(search_indices or [])
            for i, cue in enumerate(cues):
                display = cue['text'].replace('\n', ' \\n ')
                ts = f"{cue['start']} → {cue['end']}"
                # Determine color tags — compare against original text set
                # Pass a dummy different string to trigger "modified" if text not in originals
                if cue['text'] in _orig_texts:
                    orig_text = cue['text']  # matches — not modified
                else:
                    orig_text = ''  # doesn't match — will be flagged as modified
                ctags = _classify_cue(cue, orig_text)
                if i in search_set:
                    ctags.add(TAG_SEARCH)
                # Priority: search > modified > hi > tags > long
                if TAG_SEARCH in ctags:
                    row_tag = TAG_SEARCH
                elif i in spell_error_indices:
                    row_tag = TAG_SPELL
                elif TAG_MODIFIED in ctags:
                    row_tag = TAG_MODIFIED
                elif TAG_HI in ctags:
                    row_tag = TAG_HI
                elif TAG_TAGS in ctags:
                    row_tag = TAG_TAGS
                elif TAG_LONG in ctags:
                    row_tag = TAG_LONG
                else:
                    row_tag = ''
                tree.insert('', 'end', iid=str(i),
                            values=(i + 1, ts, display),
                            tags=(row_tag,) if row_tag else ())
            # Update stats
            deleted_count.set(len(original_cues) - len(cues))
            mod = sum(1 for i, c in enumerate(cues) if i < len(original_cues)
                      and c['text'] != original_cues[i]['text'])
            modified_count.set(mod)
            long_count = sum(1 for c in cues
                             if any(len(l) > MAX_CHARS_PER_LINE for l in c['text'].split('\n')))
            stats_parts = [
                f"{len(cues)} entries",
                f"{modified_count.get()} modified",
                f"{deleted_count.get()} removed",
            ]
            if long_count:
                stats_parts.append(f"{long_count} long lines")
            stats_label.configure(text=" │ ".join(stats_parts))
            # Refresh waveform timeline cue blocks and live subtitles (if loaded)
            try:
                if timeline_int.is_loaded:
                    timeline_int.refresh()
                    timeline_int.reload_subtitles()
            except NameError:
                pass  # timeline_int not yet defined during initial setup

        def apply_filter(filter_func, name):
            nonlocal cues
            push_undo()
            before = len(cues)
            cues = filter_func(cues)
            after = len(cues)
            app.add_log(f"Filter '{name}': {before - after} entries removed, "
                         f"{after} remaining", 'INFO')
            refresh_tree(cues)

        def _is_mostly_allcaps():
            """Check if the subtitle text is mostly ALL CAPS."""
            if not cues:
                return False
            all_text = ' '.join(c['text'] for c in cues)
            alpha = re.sub(r'[^a-zA-Z]', '', all_text)
            if not alpha:
                return False
            return sum(1 for c in alpha if c.isupper()) / len(alpha) >= 0.6

        def apply_remove_hi():
            """Apply Remove HI, auto-running Fix ALL CAPS first if text is all-caps."""
            nonlocal cues
            if _is_mostly_allcaps():
                app.add_log("Text is mostly ALL CAPS — running Fix ALL CAPS first "
                             "to avoid false HI detection", 'INFO')
                push_undo()
                cues = filter_fix_caps(cues, app.custom_cap_words)
                refresh_tree(cues)
            apply_filter(filter_remove_hi, "Remove HI")

        def undo_all():
            nonlocal cues
            push_undo()
            cues = [dict(c) for c in original_cues]
            refresh_tree(cues)
            app.add_log("Subtitle edits reset to original", 'INFO')

        def delete_selected():
            nonlocal cues
            selected = tree.selection()
            if not selected:
                return
            push_undo()
            indices_to_remove = set(int(s) for s in selected)
            cues = [c for i, c in enumerate(cues) if i not in indices_to_remove]
            refresh_tree(cues)

        def split_selected():
            """Split the selected cue into two at the midpoint."""
            nonlocal cues
            selected = tree.selection()
            if len(selected) != 1:
                messagebox.showinfo("Split", "Select exactly one cue to split.",
                                    parent=editor)
                return
            idx = int(selected[0])
            cue = cues[idx]
            text = cue['text']
            lines = text.split('\n')
            if len(lines) < 2:
                mid = len(text) // 2
                space_pos = text.rfind(' ', 0, mid + 10)
                if space_pos > mid - 20:
                    mid = space_pos
                text1 = text[:mid].rstrip()
                text2 = text[mid:].lstrip()
            else:
                mid_line = len(lines) // 2
                text1 = '\n'.join(lines[:mid_line])
                text2 = '\n'.join(lines[mid_line:])

            if not text1 or not text2:
                return

            push_undo()
            start_ms = srt_ts_to_ms(cue['start'])
            end_ms = srt_ts_to_ms(cue['end'])
            mid_ms = (start_ms + end_ms) // 2

            cue1 = {**cue, 'text': text1, 'end': ms_to_srt_ts(mid_ms)}
            cue2 = {**cue, 'text': text2, 'start': ms_to_srt_ts(mid_ms + 1)}
            cues[idx:idx + 1] = [cue1, cue2]
            refresh_tree(cues)

        def join_selected():
            """Join two or more selected consecutive cues into one."""
            nonlocal cues
            selected = sorted(tree.selection(), key=int)
            if len(selected) < 2:
                messagebox.showinfo("Join", "Select two or more consecutive cues to join.",
                                    parent=editor)
                return
            indices = [int(s) for s in selected]
            for i in range(1, len(indices)):
                if indices[i] != indices[i - 1] + 1:
                    messagebox.showwarning("Join",
                        "Selected cues must be consecutive.", parent=editor)
                    return

            push_undo()
            first = cues[indices[0]]
            last = cues[indices[-1]]
            merged_text = ' '.join(cues[i]['text'] for i in indices)
            merged = {
                **first,
                'end': last['end'],
                'text': merged_text
            }
            cues[indices[0]:indices[-1] + 1] = [merged]
            refresh_tree(cues)

        def show_timing_dialog():
            """Show a small dialog for timing offset and stretch."""
            td = tk.Toplevel(editor)
            td.title("Timing Adjustment")
            td.geometry("320x180")
            td.transient(editor)
            td.grab_set()
            app._center_on_main(td)
            td.resizable(False, False)

            of = ttk.LabelFrame(td, text="Offset (shift all timestamps)", padding=8)
            of.pack(fill='x', padx=10, pady=(10, 5))
            offset_var = tk.StringVar(value="0")
            ttk.Label(of, text="Milliseconds (+/−):").pack(side='left')
            ttk.Entry(of, textvariable=offset_var, width=10).pack(side='left', padx=4)

            def apply_offset():
                nonlocal cues
                try:
                    ms = int(offset_var.get())
                except ValueError:
                    messagebox.showwarning("Invalid", "Enter a number in milliseconds.",
                                           parent=td)
                    return
                if ms == 0:
                    return
                push_undo()
                cues = shift_timestamps(cues, ms)
                refresh_tree(cues)
                direction = "forward" if ms > 0 else "backward"
                app.add_log(f"Shifted timestamps {direction} by {abs(ms)}ms", 'INFO')
                td.destroy()

            ttk.Button(of, text="Apply", command=apply_offset).pack(side='right')

            sf = ttk.LabelFrame(td, text="Stretch (scale timestamps)", padding=8)
            sf.pack(fill='x', padx=10, pady=5)
            stretch_var = tk.StringVar(value="1.0")
            ttk.Label(sf, text="Factor:").pack(side='left')
            ttk.Entry(sf, textvariable=stretch_var, width=10).pack(side='left', padx=4)

            def apply_stretch():
                nonlocal cues
                try:
                    factor = float(stretch_var.get())
                except ValueError:
                    messagebox.showwarning("Invalid", "Enter a decimal number (e.g. 1.04).",
                                           parent=td)
                    return
                if factor <= 0:
                    messagebox.showwarning("Invalid", "Factor must be positive.", parent=td)
                    return
                if factor == 1.0:
                    return
                push_undo()
                cues = stretch_timestamps(cues, factor)
                refresh_tree(cues)
                app.add_log(f"Stretched timestamps by factor {factor}", 'INFO')
                td.destroy()

            ttk.Button(sf, text="Apply", command=apply_stretch).pack(side='right')
            ttk.Button(td, text="Close", command=td.destroy).pack(pady=(5, 10))

        # Search state
        find_var = tk.StringVar()
        replace_var = tk.StringVar()
        use_regex = tk.BooleanVar(value=False)  # reserved for future use
        wrap_around = tk.BooleanVar(value=False)

        def do_find():
            """Highlight all cues matching the search term."""
            term = find_var.get()
            if not term:
                refresh_tree(cues)
                return
            matches = []
            for i, cue in enumerate(cues):
                try:
                    if use_regex.get():
                        if re.search(term, cue['text'], re.IGNORECASE):
                            matches.append(i)
                    else:
                        if term.lower() in cue['text'].lower():
                            matches.append(i)
                except re.error:
                    pass
            refresh_tree(cues, search_indices=matches)
            if matches:
                first_idx = matches[0]
                first = str(first_idx)
                def _scroll_to_match():
                    tree.selection_set(first)
                    # Scroll so the match is near the middle of the view, not at the edge
                    # Aim a few rows past the match so it's comfortably visible
                    ahead = min(first_idx + 5, len(cues) - 1)
                    tree.see(str(ahead))
                    tree.after(50, lambda: (tree.see(first), tree.selection_set(first)))
                tree.after_idle(_scroll_to_match)
            app.add_log(f"Search: {len(matches)} matches for '{term}'", 'INFO')

        def do_replace_one():
            """Replace the first occurrence of search term from current selection."""
            nonlocal cues
            term = find_var.get()
            repl = replace_var.get()
            if not term:
                return
            sel = tree.selection()
            start_idx = int(sel[0]) if sel else 0
            if wrap_around.get():
                order = list(range(start_idx, len(cues))) + list(range(0, start_idx))
            else:
                order = list(range(start_idx, len(cues)))
            for i in order:
                old_text = cues[i]['text']
                try:
                    if use_regex.get():
                        new_text = re.sub(term, repl, old_text, count=1,
                                          flags=re.IGNORECASE)
                    else:
                        pat = re.escape(term)
                        new_text = re.sub(pat, repl, old_text, count=1,
                                          flags=re.IGNORECASE)
                except re.error:
                    continue
                if new_text != old_text:
                    push_undo()
                    cues[i]['text'] = new_text
                    if not new_text.strip():
                        del cues[i]
                    refresh_tree(cues)
                    if wrap_around.get():
                        next_order = list(range(i + 1, len(cues))) + list(range(0, i + 1))
                    else:
                        next_order = list(range(i + 1, len(cues)))
                    for j in next_order:
                        try:
                            if use_regex.get():
                                if re.search(term, cues[j]['text'], re.IGNORECASE):
                                    tree.see(str(j))
                                    tree.selection_set(str(j))
                                    break
                            else:
                                if term.lower() in cues[j]['text'].lower():
                                    tree.see(str(j))
                                    tree.selection_set(str(j))
                                    break
                        except (re.error, IndexError):
                            pass
                    app.add_log(f"Replaced 1 occurrence of '{term}' → '{repl}'", 'INFO')
                    return
            app.add_log(f"No more matches found for '{term}'", 'INFO')

        def do_replace_all():
            """Replace all occurrences of search term."""
            nonlocal cues
            term = find_var.get()
            repl = replace_var.get()
            if not term:
                return
            push_undo()
            count = 0
            for cue in cues:
                old_text = cue['text']
                try:
                    if use_regex.get():
                        new_text = re.sub(term, repl, old_text, flags=re.IGNORECASE)
                    else:
                        pattern = re.escape(term)
                        new_text = re.sub(pattern, repl, old_text, flags=re.IGNORECASE)
                except re.error:
                    continue
                if new_text != old_text:
                    cue['text'] = new_text
                    count += 1
            cues = [c for c in cues if c['text'].strip()]
            refresh_tree(cues)
            app.add_log(f"Replaced {count} occurrence(s) of '{term}' → '{repl}'", 'INFO')

        # ══════════════════════════════════════════════════════════════════════
        # Menu bar
        # ══════════════════════════════════════════════════════════════════════
        menubar = tk.Menu(editor)
        editor.configure(menu=menubar)

        # ── Filters menu ──
        filter_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Tools", menu=filter_menu)
        filter_menu.add_command(label="Remove HI  [brackets] (parens) Speaker:",
                                command=lambda: apply_remove_hi())
        filter_menu.add_command(label="Remove Tags  <i> {\\an8}",
                                command=lambda: apply_filter(filter_remove_tags, "Remove Tags"))
        def apply_remove_ads():
            apply_filter(lambda c: filter_remove_ads(c, app.custom_ad_patterns),
                         "Remove Ads")

        filter_menu.add_command(label="Remove Ads / Credits", command=apply_remove_ads)
        filter_menu.add_command(label="Remove Stray Notes  ♪ ♫",
                                command=lambda: apply_filter(filter_remove_music_notes, "Remove Stray Notes"))
        filter_menu.add_command(label="Remove Leading Dashes  -",
                                command=lambda: apply_filter(filter_remove_leading_dashes, "Remove Leading Dashes"))
        filter_menu.add_command(label="Remove ALL CAPS HI  (UK style)",
                                command=lambda: apply_filter(filter_remove_caps_hi, "Remove CAPS HI"))
        filter_menu.add_command(label="Remove Off-Screen Quotes  ' '  (UK style)",
                                command=lambda: apply_filter(filter_remove_offscreen_quotes, "Remove Off-Screen Quotes"))
        filter_menu.add_separator()
        filter_menu.add_command(label="Remove Duplicates",
                                command=lambda: apply_filter(filter_remove_duplicates, "Remove Duplicates"))
        filter_menu.add_command(label="Merge Short Cues",
                                command=lambda: apply_filter(filter_merge_short, "Merge Short Cues"))
        filter_menu.add_command(label="Reduce to 2 Lines",
                                command=lambda: apply_filter(filter_reduce_lines, "Reduce to 2 Lines"))
        filter_menu.add_separator()

        # ── Fix ALL CAPS ──
        # Custom capitalize words stored on the app instance
        if not hasattr(app, 'custom_cap_words'):
            app.custom_cap_words = []

        def show_fix_caps_dialog():
            """Show Fix ALL CAPS dialog with custom names management."""
            cd = tk.Toplevel(editor)
            cd.title("Fix ALL CAPS")
            cd.geometry("420x400")
            app._center_on_main(cd)
            cd.resizable(True, True)
            # Keep on top but don't grab — allows scrolling the subtitle list
            cd.attributes('-topmost', True)

            ttk.Label(cd, text="Converts ALL CAPS text to sentence case.\n"
                      "Add character names below to preserve their capitalisation.\n"
                      "You can scroll the subtitle list to find names.",
                      justify='center', padding=(10, 10)).pack()

            lf = ttk.LabelFrame(cd, text="Custom Names (saved across sessions)",
                                padding=8)
            lf.pack(fill='both', expand=True, padx=10, pady=5)

            word_list = tk.Listbox(lf, height=8, font=('Courier', 10))
            word_list.pack(fill='both', expand=True)
            for w in app.custom_cap_words:
                word_list.insert('end', w)

            add_frame = ttk.Frame(lf)
            add_frame.pack(fill='x', pady=(4, 0))
            new_word_var = tk.StringVar()
            word_entry = ttk.Entry(add_frame, textvariable=new_word_var)
            word_entry.pack(side='left', fill='x', expand=True, padx=(0, 4))
            word_entry.focus_set()
            # Right-click context menu for copy/paste
            _wm = tk.Menu(word_entry, tearoff=0)
            _wm.add_command(label="Cut", command=lambda: word_entry.event_generate('<<Cut>>'))
            _wm.add_command(label="Copy", command=lambda: word_entry.event_generate('<<Copy>>'))
            _wm.add_command(label="Paste", command=lambda: word_entry.event_generate('<<Paste>>'))
            _wm.add_separator()
            _wm.add_command(label="Select All",
                command=lambda: (word_entry.select_range(0, 'end'), word_entry.icursor('end')))
            word_entry.bind('<Button-3>', lambda e, m=_wm: m.tk_popup(e.x_root, e.y_root))

            def add_word():
                word = new_word_var.get().strip()
                if not word:
                    return
                if word.lower() not in [w.lower() for w in app.custom_cap_words]:
                    app.custom_cap_words.append(word)
                    word_list.insert('end', word)
                    app.save_preferences()
                new_word_var.set('')

            def remove_word():
                sel = word_list.curselection()
                if sel:
                    app.custom_cap_words.pop(sel[0])
                    word_list.delete(sel[0])
                    app.save_preferences()

            ttk.Button(add_frame, text="Add", command=add_word).pack(side='right')
            word_entry.bind('<Return>', lambda e: add_word())

            ttk.Label(lf, text="Names are saved automatically and persist between sessions.",
                      font=('Helvetica', 8), foreground='gray').pack(anchor='w')

            btn_frame = ttk.Frame(cd, padding=(10, 8, 10, 10))
            btn_frame.pack(fill='x')
            ttk.Button(btn_frame, text="Remove Selected", command=remove_word).pack(side='left')
            ttk.Button(btn_frame, text="Apply",
                       command=lambda: (cd.destroy(), apply_filter(
                           lambda c: filter_fix_caps(c, app.custom_cap_words),
                           "Fix ALL CAPS"))).pack(side='right')
            ttk.Button(btn_frame, text="Close", command=cd.destroy).pack(side='right', padx=4)

        filter_menu.add_command(label="Fix ALL CAPS...", command=show_fix_caps_dialog)
        filter_menu.add_separator()

        def show_ad_patterns_dialog():
            """Dialog to view built-in ad patterns and manage custom ones."""
            pd = tk.Toplevel(editor)
            pd.title("Ad / Credit Patterns")
            pd.geometry("500x420")
            pd.transient(editor)
            pd.grab_set()
            app._center_on_main(pd)
            pd.resizable(True, True)

            # Built-in patterns (read-only)
            bf = ttk.LabelFrame(pd, text="Built-in Patterns (always active)", padding=8)
            bf.pack(fill='x', padx=10, pady=(10, 5))
            builtin_list = tk.Listbox(bf, height=6, font=('Courier', 9))
            builtin_list.pack(fill='x')
            for p in BUILTIN_AD_PATTERNS:
                builtin_list.insert('end', p)

            # Custom patterns (editable)
            cf = ttk.LabelFrame(pd, text="Custom Patterns (saved to preferences)", padding=8)
            cf.pack(fill='both', expand=True, padx=10, pady=5)

            custom_list = tk.Listbox(cf, height=8, font=('Courier', 9))
            custom_list.pack(fill='both', expand=True)
            for p in app.custom_ad_patterns:
                custom_list.insert('end', p)

            add_frame = ttk.Frame(cf)
            add_frame.pack(fill='x', pady=(4, 0))
            new_pattern_var = tk.StringVar()
            pattern_entry = ttk.Entry(add_frame, textvariable=new_pattern_var)
            pattern_entry.pack(side='left', fill='x', expand=True, padx=(0, 4))

            def add_pattern():
                pat = new_pattern_var.get().strip()
                if not pat:
                    return
                # Validate regex
                try:
                    re.compile(pat)
                except re.error as e:
                    messagebox.showwarning("Invalid Pattern",
                                           f"Not a valid regex:\n{e}", parent=pd)
                    return
                if pat not in app.custom_ad_patterns:
                    app.custom_ad_patterns.append(pat)
                    custom_list.insert('end', pat)
                    new_pattern_var.set('')
                    app.add_log(f"Added custom ad pattern: {pat}", 'INFO')

            def remove_selected():
                sel = custom_list.curselection()
                if not sel:
                    return
                idx = sel[0]
                removed = app.custom_ad_patterns.pop(idx)
                custom_list.delete(idx)
                app.add_log(f"Removed custom ad pattern: {removed}", 'INFO')

            ttk.Button(add_frame, text="Add", command=add_pattern).pack(side='right')
            pattern_entry.bind('<Return>', lambda e: add_pattern())

            # Hint
            ttk.Label(cf, text="Patterns are case-insensitive regex matched at start of line.",
                      font=('Helvetica', 8), foreground='gray').pack(anchor='w')

            # Buttons
            btn_frame = ttk.Frame(pd, padding=(10, 6, 10, 10))
            btn_frame.pack(fill='x')
            ttk.Button(btn_frame, text="Remove Selected", command=remove_selected).pack(side='left')

            def save_and_close():
                app.save_preferences()
                pd.destroy()

            ttk.Button(btn_frame, text="Save & Close", command=save_and_close).pack(side='right')
            ttk.Button(btn_frame, text="Cancel", command=pd.destroy).pack(side='right', padx=4)

        filter_menu.add_command(label="Manage Ad Patterns...",
                                command=show_ad_patterns_dialog)
        filter_menu.add_separator()
        filter_menu.add_command(label="Spell Check...",
                                accelerator="F7",
                                command=lambda: _show_spell_check())
        filter_menu.add_separator()
        filter_menu.add_command(label="Search/Replace List...",
                                command=lambda: _show_saved_replacements())

        def _show_saved_replacements():
            """Show dialog to manage and apply persistent search & replace pairs."""
            sd = tk.Toplevel(editor)
            sd.title("Search/Replace List")
            sd.geometry("550x450")
            sd.resizable(True, True)
            app._center_on_main(sd)
            sd.attributes('-topmost', True)

            f = ttk.Frame(sd, padding=12)
            f.pack(fill='both', expand=True)
            f.columnconfigure(0, weight=1)
            f.rowconfigure(1, weight=1)

            # ── Add new pair ──
            add_f = ttk.LabelFrame(f, text="Add Replacement", padding=6)
            add_f.grid(row=0, column=0, sticky='ew', pady=(0, 8))

            af = ttk.Frame(add_f)
            af.pack(fill='x')
            ttk.Label(af, text="Find:").pack(side='left', padx=(0, 4))
            sr_find = tk.StringVar()
            sr_find_entry = ttk.Entry(af, textvariable=sr_find, width=18)
            sr_find_entry.pack(side='left', padx=(0, 8))
            ttk.Label(af, text="Replace:").pack(side='left', padx=(0, 4))
            sr_repl = tk.StringVar()
            sr_repl_entry = ttk.Entry(af, textvariable=sr_repl, width=18)
            sr_repl_entry.pack(side='left', padx=(0, 8))
            sr_case = tk.BooleanVar(value=False)
            ttk.Checkbutton(af, text="Aa", variable=sr_case).pack(side='left', padx=(0, 4))

            def _add_pair():
                find = sr_find.get()
                if not find:
                    return
                repl = sr_repl.get()
                pair = [find, repl, sr_case.get()]
                if pair not in app.custom_replacements:
                    app.custom_replacements.append(pair)
                    app.save_preferences()
                _refresh_list()
                sr_find.set('')
                sr_repl.set('')

            ttk.Button(af, text="Add", command=_add_pair, width=5).pack(side='left', padx=2)

            # ── List ──
            list_f = ttk.Frame(f)
            list_f.grid(row=1, column=0, sticky='nsew')
            list_f.columnconfigure(0, weight=1)
            list_f.rowconfigure(0, weight=1)

            columns = ('find', 'replace', 'case')
            sr_tree = ttk.Treeview(list_f, columns=columns, show='headings', height=10)
            sr_tree.grid(row=0, column=0, sticky='nsew')
            sr_tree.heading('find', text='Find')
            sr_tree.heading('replace', text='Replace With')
            sr_tree.heading('case', text='Case')
            sr_tree.column('find', width=180, minwidth=100)
            sr_tree.column('replace', width=180, minwidth=100)
            sr_tree.column('case', width=50, minwidth=40, anchor='center')

            sr_scroll = ttk.Scrollbar(list_f, orient='vertical', command=sr_tree.yview)
            sr_scroll.grid(row=0, column=1, sticky='ns')
            sr_tree.configure(yscrollcommand=sr_scroll.set)

            def _refresh_list():
                sr_tree.delete(*sr_tree.get_children())
                for i, pair in enumerate(app.custom_replacements):
                    find, repl = pair[0], pair[1]
                    case = 'Yes' if (len(pair) > 2 and pair[2]) else 'No'
                    sr_tree.insert('', 'end', iid=str(i),
                                  values=(find, repl, case))

            def _remove_selected():
                sel = sr_tree.selection()
                if not sel:
                    return
                indices = sorted([int(s) for s in sel], reverse=True)
                for idx in indices:
                    if idx < len(app.custom_replacements):
                        del app.custom_replacements[idx]
                app.save_preferences()
                _refresh_list()

            def _clear_all():
                if messagebox.askyesno("Clear All",
                    "Remove all saved replacements?", parent=sd):
                    app.custom_replacements.clear()
                    app.save_preferences()
                    _refresh_list()

            # ── Buttons ──
            btn_f = ttk.Frame(f)
            btn_f.grid(row=2, column=0, sticky='ew', pady=(8, 0))

            def _apply_all():
                if not app.custom_replacements:
                    messagebox.showinfo("No Replacements",
                        "No saved replacements to apply.", parent=sd)
                    return
                push_undo()
                total_count = 0
                for pair in app.custom_replacements:
                    find, repl = pair[0], pair[1]
                    case_sensitive = len(pair) > 2 and pair[2]
                    for cue in cues:
                        old = cue['text']
                        if case_sensitive:
                            cue['text'] = cue['text'].replace(find, repl)
                        else:
                            cue['text'] = re.sub(re.escape(find), lambda m: repl,
                                                 cue['text'], flags=re.IGNORECASE)
                        if cue['text'] != old:
                            total_count += 1
                refresh_tree(cues)
                app.add_log(f"Applied {len(app.custom_replacements)} replacement rule(s), "
                             f"{total_count} cue(s) changed", 'INFO')
                messagebox.showinfo("Replacements Applied",
                    f"Applied {len(app.custom_replacements)} rule(s)\n"
                    f"{total_count} cue(s) modified", parent=sd)

            ttk.Button(btn_f, text="▶ Apply All", command=_apply_all).pack(side='left', padx=2)
            ttk.Button(btn_f, text="Remove", command=_remove_selected).pack(side='left', padx=2)
            ttk.Button(btn_f, text="Clear All", command=_clear_all).pack(side='left', padx=2)
            ttk.Button(btn_f, text="Close", command=sd.destroy).pack(side='right', padx=2)

            _refresh_list()

        def _show_spell_check():
            """Incremental spell check — scans and fixes as it goes."""
            if not cues:
                messagebox.showinfo("Spell Check", "No subtitle loaded.",
                                    parent=editor)
                return

            # ── Initialize spell checker ──
            try:
                from spellchecker import SpellChecker
            except ImportError:
                if messagebox.askyesno("Missing Package",
                    "pyspellchecker is not installed.\n\n"
                    "Would you like to install it now?",
                    parent=editor):
                    try:
                        _pip_result = subprocess.run(
                            [sys.executable, '-m', 'pip', 'install',
                             '--user', '--break-system-packages', 'pyspellchecker'],
                            capture_output=True, text=True, timeout=60)
                        if _pip_result.returncode == 0:
                            from spellchecker import SpellChecker
                        else:
                            messagebox.showerror("Install Failed",
                                f"pip install failed:\n{_pip_result.stderr[-300:]}",
                                parent=editor)
                            return
                    except Exception as _e:
                        messagebox.showerror("Install Failed",
                            f"Could not install pyspellchecker:\n{_e}",
                            parent=editor)
                        return
                else:
                    return

            spell = SpellChecker()
            known = [w.lower() for w in app.custom_cap_words + app.custom_spell_words]
            if known:
                spell.word_frequency.load_words(known)

            # ── Scan state ──
            scan_cue = [0]        # current cue index being scanned
            scan_word = [0]       # current word index within the cue
            ignored = set()
            error_count = [0]
            cues_checked = [0]

            # ── Build dialog ──
            sd = tk.Toplevel(editor)
            sd.withdraw()
            sd.title("Spell Check")
            sd.geometry("500x440")
            sd.resizable(True, True)
            sd.update_idletasks()
            # Center on editor window
            ew, eh = editor.winfo_width(), editor.winfo_height()
            ex, ey = editor.winfo_x(), editor.winfo_y()
            sw, sh = 500, 440
            sd.geometry(f"{sw}x{sh}+{ex + (ew - sw)//2}+{ey + (eh - sh)//2}")
            sd.deiconify()
            sd.attributes('-topmost', True)

            sf = ttk.Frame(sd, padding=12)
            sf.pack(fill='both', expand=True)
            sf.columnconfigure(1, weight=1)
            _sp = {'padx': 6, 'pady': 4}

            stats_lbl = ttk.Label(sf, text="Scanning...",
                                  font=('Helvetica', 9))
            stats_lbl.grid(row=0, column=0, columnspan=2, sticky='w', **_sp)

            ttk.Label(sf, text="Not in dictionary:",
                      font=('Helvetica', 10, 'bold')).grid(
                          row=1, column=0, sticky='w', **_sp)
            word_var = tk.StringVar()
            ttk.Entry(sf, textvariable=word_var, state='readonly',
                      font=('Courier', 12)).grid(
                          row=1, column=1, sticky='ew', **_sp)

            ttk.Label(sf, text="Context:").grid(
                row=2, column=0, sticky='nw', **_sp)
            ctx_var = tk.StringVar()
            ttk.Label(sf, textvariable=ctx_var, wraplength=380,
                      font=('Helvetica', 9),
                      foreground='gray').grid(
                          row=2, column=1, sticky='w', **_sp)

            ttk.Label(sf, text="Suggestions:").grid(
                row=3, column=0, sticky='nw', **_sp)
            sug_fr = ttk.Frame(sf)
            sug_fr.grid(row=3, column=1, sticky='nsew', **_sp)
            sug_fr.rowconfigure(0, weight=1)
            sug_fr.columnconfigure(0, weight=1)
            sf.rowconfigure(3, weight=1)

            sug_lb = tk.Listbox(sug_fr, height=6, font=('Courier', 10))
            sug_lb.grid(row=0, column=0, sticky='nsew')
            sug_sc = ttk.Scrollbar(sug_fr, orient='vertical',
                                   command=sug_lb.yview)
            sug_sc.grid(row=0, column=1, sticky='ns')
            sug_lb.configure(yscrollcommand=sug_sc.set)

            replace_var = tk.StringVar()
            def on_sug_sel(evt):
                sel = sug_lb.curselection()
                if sel:
                    replace_var.set(sug_lb.get(sel[0]))
            sug_lb.bind('<<ListboxSelect>>', on_sug_sel)

            ttk.Label(sf, text="Replace with:").grid(
                row=4, column=0, sticky='w', **_sp)
            ttk.Entry(sf, textvariable=replace_var,
                      font=('Courier', 11)).grid(
                          row=4, column=1, sticky='ew', **_sp)

            bf = ttk.Frame(sf)
            bf.grid(row=5, column=0, columnspan=2, sticky='ew',
                    pady=(8, 0))

            # ── Incremental scanner ──
            def _find_next():
                """Scan forward from current position for the next error.
                Returns (cue_idx, word, candidates) or None."""
                ci = scan_cue[0]
                wi = scan_word[0]
                while ci < len(cues):
                    cues_checked[0] = ci + 1
                    clean = re.sub(r'<[^>]+>|\{\\[^}]+\}|♪', '',
                                   cues[ci]['text'])
                    words = re.findall(r"[a-zA-Z]+(?:'[a-zA-Z]+)?",
                                       clean)
                    if words:
                        unknown = spell.unknown(words)
                        if unknown:
                            for j in range(wi, len(words)):
                                w = words[j]
                                if ((w.lower() in unknown or w in unknown)
                                        and w.lower() not in ignored):
                                    cands = spell.candidates(w)
                                    spell_error_indices.add(ci)
                                    scan_cue[0] = ci
                                    scan_word[0] = j + 1
                                    return (ci, w,
                                            sorted(cands) if cands else [])
                    ci += 1
                    wi = 0
                    scan_cue[0] = ci
                    scan_word[0] = 0
                return None

            # ── Current error state ──
            current_error = [None]  # (ci, word, candidates)

            def _show_next():
                """Find and display the next error."""
                result = _find_next()
                current_error[0] = result
                if result is None:
                    spell_error_indices.clear()
                    refresh_tree(cues)
                    messagebox.showinfo("Spell Check",
                        f"Spell check complete!\n"
                        f"{cues_checked[0]} cues checked, "
                        f"{error_count[0]} errors found.",
                        parent=sd)
                    sd.destroy()
                    return
                ci, w, ca = result
                error_count[0] += 1
                items = tree.get_children()
                if ci < len(items):
                    ahead = min(ci + 5, len(items) - 1)
                    tree.see(items[ahead])
                    tree.selection_set(items[ci])
                    tree.after(50, lambda: tree.see(items[ci]))
                word_var.set(w)
                ctx_var.set(cues[ci]['text'].replace('\n', ' / '))
                stats_lbl.configure(
                    text=f"Checking cue {ci + 1} of {len(cues)} "
                         f"({error_count[0]} errors found)")
                sug_lb.delete(0, 'end')
                for c in ca:
                    sug_lb.insert('end', c)
                if ca:
                    sug_lb.selection_set(0)
                    replace_var.set(ca[0])
                else:
                    replace_var.set(w)

            def _do_replace():
                if not current_error[0]:
                    return
                ci, w, _ = current_error[0]
                repl = replace_var.get().strip()
                if not repl:
                    return
                push_undo()
                txt = cues[ci]['text']
                pos = txt.find(w)
                if pos == -1:
                    pos = txt.lower().find(w.lower())
                if pos >= 0:
                    cues[ci]['text'] = (txt[:pos] + repl
                                       + txt[pos + len(w):])
                refresh_tree(cues)
                # Re-check same cue from current word position
                _show_next()

            def _do_replace_all():
                if not current_error[0]:
                    return
                _, w, _ = current_error[0]
                repl = replace_var.get().strip()
                if not repl:
                    return
                push_undo()
                for cue in cues:
                    if w in cue['text']:
                        cue['text'] = cue['text'].replace(w, repl)
                    elif w.lower() in cue['text'].lower():
                        cue['text'] = re.sub(re.escape(w), repl,
                                             cue['text'],
                                             flags=re.IGNORECASE)
                ignored.add(w.lower())
                refresh_tree(cues)
                _show_next()

            def _do_skip():
                _show_next()

            def _do_ignore():
                if current_error[0]:
                    ignored.add(current_error[0][1].lower())
                _show_next()

            def _do_add_dict():
                if not current_error[0]:
                    return
                w = current_error[0][1]
                if w.lower() not in [x.lower()
                                     for x in app.custom_spell_words]:
                    app.custom_spell_words.append(w)
                    spell.word_frequency.load_words([w.lower()])
                    app.save_preferences()
                ignored.add(w.lower())
                _show_next()

            def _do_add_name():
                if not current_error[0]:
                    return
                w = current_error[0][1]
                if w not in app.custom_cap_words:
                    app.custom_cap_words.append(w)
                if w.lower() not in [x.lower()
                                     for x in app.custom_spell_words]:
                    app.custom_spell_words.append(w)
                spell.word_frequency.load_words([w.lower()])
                app.save_preferences()
                ignored.add(w.lower())
                _show_next()

            bf1 = ttk.Frame(bf)
            bf1.pack(fill='x')
            ttk.Button(bf1, text="Replace", command=_do_replace,
                       width=10).pack(side='left', padx=2)
            ttk.Button(bf1, text="Replace All",
                       command=_do_replace_all,
                       width=10).pack(side='left', padx=2)
            ttk.Button(bf1, text="Skip", command=_do_skip,
                       width=6).pack(side='left', padx=2)
            ttk.Button(bf1, text="Ignore", command=_do_ignore,
                       width=8).pack(side='left', padx=2)

            bf2 = ttk.Frame(bf)
            bf2.pack(fill='x', pady=(4, 0))
            ttk.Button(bf2, text="Add to Dict",
                       command=_do_add_dict,
                       width=10).pack(side='left', padx=2)
            ttk.Button(bf2, text="Add as Name",
                       command=_do_add_name,
                       width=10).pack(side='left', padx=2)
            ttk.Button(bf2, text="Close", command=sd.destroy,
                       width=6).pack(side='right', padx=2)

            # Start scanning immediately
            _show_next()

        editor.bind('<F7>', lambda e: _show_spell_check())

        # ── Edit menu ──
        edit_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Edit", menu=edit_menu)
        edit_menu.add_command(label="Undo                Ctrl+Z", command=do_undo)
        edit_menu.add_command(label="Redo                Ctrl+Y", command=do_redo)
        edit_menu.add_command(label="Reset to Original", command=undo_all)
        edit_menu.add_separator()
        edit_menu.add_command(label="Delete Selected     Del", command=delete_selected)
        edit_menu.add_command(label="Split Cue", command=split_selected)
        edit_menu.add_command(label="Join Selected Cues", command=join_selected)

        # ── Timing menu ──
        timing_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Timing", menu=timing_menu)
        timing_menu.add_command(label="Offset / Stretch...", command=show_timing_dialog)
        timing_menu.add_command(label="Smart Sync...",
                                command=lambda: _show_smart_sync())

        # ── Quick Sync submenu ──
        quick_sync_menu = tk.Menu(timing_menu, tearoff=0)
        timing_menu.add_cascade(label="Quick Sync", menu=quick_sync_menu)

        def _quick_sync_first_cue():
            """Shift all cues so the first cue starts at a user-specified time.
            Includes an embedded mpv player for marking the exact time."""
            if not cues:
                messagebox.showinfo("No Subtitles", "Load subtitles first.",
                                    parent=editor)
                return

            qd = tk.Toplevel(editor)
            qd.title("Quick Sync — Set First Cue Time")
            qd.geometry("720x620")
            qd.minsize(640, 540)
            qd.resizable(True, True)
            app._center_on_main(qd)

            f = ttk.Frame(qd, padding=10)
            f.pack(fill='both', expand=True)
            f.columnconfigure(1, weight=1)
            f.rowconfigure(2, weight=1)  # video frame expands

            first_cue = cues[0]
            current_start = first_cue['start']
            preview_text = first_cue['text'].replace('\n', ' ')
            if len(preview_text) > 60:
                preview_text = preview_text[:57] + '...'

            # ── Video file ──
            ttk.Label(f, text="Video file:").grid(
                row=0, column=0, sticky='w', padx=4, pady=2)
            _qs_vpath = tk.StringVar()
            # Try to find video automatically
            try:
                if hasattr(editor, '_qs_last_video') and editor._qs_last_video:
                    _qs_vpath.set(editor._qs_last_video)
                elif current_path[0]:
                    _sub_dir = os.path.dirname(current_path[0])
                    _sub_stem = os.path.splitext(
                        os.path.basename(current_path[0]))[0]
                    for _i in range(3):
                        _dot = _sub_stem.rfind('.')
                        if _dot > 0:
                            _sub_stem = _sub_stem[:_dot]
                        else:
                            break
                    for ext in VIDEO_EXTENSIONS:
                        _vp = os.path.join(_sub_dir, _sub_stem + ext)
                        if os.path.isfile(_vp):
                            _qs_vpath.set(_vp)
                            break
            except Exception:
                pass

            _vpath_entry = ttk.Entry(f, textvariable=_qs_vpath)
            _vpath_entry.grid(row=0, column=1, sticky='ew', padx=4, pady=2)

            def _qs_browse():
                init_dir = os.path.dirname(_qs_vpath.get()) if _qs_vpath.get() \
                    else (os.path.dirname(current_path[0]) if current_path[0] else '')
                p = None
                if shutil.which('zenity'):
                    try:
                        cmd = ['zenity', '--file-selection',
                               '--title', 'Select Video File',
                               '--file-filter',
                               'Video files|*.mkv *.mp4 *.avi *.mov *.ts *.m2ts *.mts *.webm *.wmv *.flv',
                               '--file-filter', 'All files|*']
                        if init_dir:
                            cmd += ['--filename', init_dir + '/']
                        r = subprocess.run(cmd, capture_output=True,
                                           text=True, timeout=120)
                        if r.returncode == 0 and r.stdout.strip():
                            p = r.stdout.strip()
                    except Exception:
                        pass
                if not p:
                    p = filedialog.askopenfilename(
                        parent=qd, title="Select Video File",
                        initialdir=init_dir or None,
                        filetypes=[("Video files",
                                    "*.mkv *.mp4 *.avi *.mov *.ts *.m2ts"),
                                   ("All files", "*.*")])
                if p:
                    _qs_vpath.set(p)
                    # Auto-load the video after browse selection
                    qd.after(100, _play_video)
            ttk.Button(f, text="Browse...", command=_qs_browse).grid(
                row=0, column=2, padx=4, pady=2)

            # ── Embedded video player frame ──
            video_border = ttk.Frame(f, relief='sunken', borderwidth=2)
            video_border.grid(row=2, column=0, columnspan=3,
                              sticky='nsew', padx=4, pady=4)
            video_frame = tk.Frame(video_border, bg='black',
                                   width=640, height=360)
            video_frame.pack(fill='both', expand=True)
            video_frame.pack_propagate(False)

            _placeholder_label = tk.Label(video_frame,
                text="Drop a video file here or click Browse",
                bg='black', fg='#666', font=('Helvetica', 12))

            # ── Drag-and-drop support ──
            def _on_qs_drop(event):
                """Handle video files dropped onto the Quick Sync dialog."""
                raw = event.data
                paths = []
                if 'file://' in raw:
                    from urllib.parse import unquote, urlparse
                    for line in raw.splitlines():
                        line = line.strip()
                        if line.startswith('file://'):
                            decoded = unquote(urlparse(line).path)
                            if decoded:
                                paths.append(decoded)
                else:
                    i = 0
                    while i < len(raw):
                        if raw[i] == '{':
                            end = raw.find('}', i)
                            paths.append(raw[i + 1:end])
                            i = end + 2
                        elif raw[i] == ' ':
                            i += 1
                        else:
                            end = raw.find(' ', i)
                            if end == -1:
                                end = len(raw)
                            paths.append(raw[i:end])
                            i = end + 1

                # Find first video file in dropped paths
                for p in paths:
                    if os.path.isfile(p):
                        ext = os.path.splitext(p)[1].lower()
                        if ext in VIDEO_EXTENSIONS:
                            _qs_vpath.set(p)
                            qd.after(100, _play_video)
                            return

            try:
                qd.drop_target_register(DND_FILES)
                qd.dnd_bind('<<Drop>>', _on_qs_drop)
            except Exception:
                pass  # tkinterdnd2 not available
            _placeholder_label.place(relx=0.5, rely=0.5, anchor='center')

            # ── mpv player integration ──
            import tempfile as _qs_tempfile
            import socket as _qs_socket
            import json as _qs_json

            _mpv_proc = [None]
            _mpv_socket_path = os.path.join(
                _qs_tempfile.gettempdir(),
                f'docflix_mpv_{os.getpid()}')

            def _mpv_cmd(command_list):
                """Send a command to mpv via IPC and return the response."""
                try:
                    sock = _qs_socket.socket(
                        _qs_socket.AF_UNIX, _qs_socket.SOCK_STREAM)
                    sock.settimeout(2)
                    sock.connect(_mpv_socket_path)
                    payload = _qs_json.dumps(
                        {"command": command_list}) + '\n'
                    sock.sendall(payload.encode())
                    data = sock.recv(4096).decode()
                    sock.close()
                    return _qs_json.loads(data)
                except Exception:
                    return None

            def _play_video():
                vp = _qs_vpath.get().strip()
                if not vp or not os.path.isfile(vp):
                    messagebox.showwarning("No Video",
                        "Select a video file first.", parent=qd)
                    return

                # Kill previous mpv instance if running
                if _mpv_proc[0] and _mpv_proc[0].poll() is None:
                    _mpv_proc[0].terminate()
                    _mpv_proc[0].wait(timeout=5)

                # Clean up old socket
                if os.path.exists(_mpv_socket_path):
                    try:
                        os.unlink(_mpv_socket_path)
                    except OSError:
                        pass

                # Hide placeholder
                _placeholder_label.place_forget()

                # Get the X11 window ID for embedding
                video_frame.update_idletasks()
                wid = str(video_frame.winfo_id())

                # Launch mpv embedded in the video frame
                try:
                    _mpv_proc[0] = subprocess.Popen([
                        'mpv',
                        f'--input-ipc-server={_mpv_socket_path}',
                        f'--wid={wid}',
                        '--pause',
                        '--osd-level=2',
                        '--osd-fractions',
                        '--keep-open=yes',
                        '--no-border',
                        '--cursor-autohide=1000',
                        vp
                    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    mark_btn.configure(state='normal')
                    _mute_btn.configure(text="🔊")
                    _vol_var.set(100)
                    editor._qs_last_video = vp
                except FileNotFoundError:
                    messagebox.showerror("mpv Not Found",
                        "mpv is not installed.\n\n"
                        "Install with: sudo apt install mpv", parent=qd)
                    _placeholder_label.place(relx=0.5, rely=0.5, anchor='center')
                except Exception as e:
                    messagebox.showerror("Player Error",
                        f"Could not launch mpv:\n{e}", parent=qd)
                    _placeholder_label.place(relx=0.5, rely=0.5, anchor='center')

            def _mark_time():
                """Query mpv for current playback position and fill the time field."""
                if not _mpv_proc[0] or _mpv_proc[0].poll() is not None:
                    messagebox.showinfo("Player Closed",
                        "Load the video first.", parent=qd)
                    mark_btn.configure(state='disabled')
                    pass  # player closed
                    return

                resp = _mpv_cmd(["get_property", "playback-time"])
                if resp and 'data' in resp and resp['data'] is not None:
                    seconds = resp['data']
                    ms = int(seconds * 1000)
                    time_var.set(ms_to_srt_ts(ms))
                    time_entry.select_range(0, 'end')
                else:
                    messagebox.showwarning("Could Not Read Time",
                        "Could not get playback position from mpv.\n"
                        "Make sure the video is loaded.", parent=qd)

            def _mpv_seek(amount):
                """Seek mpv by amount in seconds."""
                if not _mpv_proc[0] or _mpv_proc[0].poll() is not None:
                    return
                _mpv_cmd(["seek", str(amount), "relative+exact"])

            def _mpv_frame_step(direction='forward'):
                """Step one frame forward or backward."""
                if not _mpv_proc[0] or _mpv_proc[0].poll() is not None:
                    return
                if direction == 'forward':
                    _mpv_cmd(["frame-step"])
                else:
                    _mpv_cmd(["frame-back-step"])

            def _mpv_pause_toggle():
                """Toggle play/pause."""
                if not _mpv_proc[0] or _mpv_proc[0].poll() is not None:
                    return
                _mpv_cmd(["cycle", "pause"])

            def _on_close():
                # Kill mpv and clean up socket
                if _mpv_proc[0] and _mpv_proc[0].poll() is None:
                    _mpv_proc[0].terminate()
                    try:
                        _mpv_proc[0].wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        _mpv_proc[0].kill()
                _mpv_proc[0] = None
                if os.path.exists(_mpv_socket_path):
                    try:
                        os.unlink(_mpv_socket_path)
                    except OSError:
                        pass
                # Reset state so next open starts fresh
                editor._qs_last_video = None
                qd.destroy()

            qd.protocol("WM_DELETE_WINDOW", _on_close)

            # ── Transport controls ──
            transport_f = ttk.Frame(f)
            transport_f.grid(row=3, column=0, columnspan=3, sticky='ew', pady=(4, 0))

            _tb_w = 3
            _transport_btns = [
                ("⏮",  lambda: _mpv_seek(-5),               "Rewind 5 seconds"),
                ("◀◀", lambda: _mpv_seek(-1),               "Rewind 1 second"),
                ("◀",  lambda: _mpv_seek(-0.1),             "Rewind 100ms"),
                ("|◀", lambda: _mpv_frame_step('backward'), "Back 1 frame"),
                ("⏯",  _mpv_pause_toggle,                   "Play / Pause"),
                ("▶|", lambda: _mpv_frame_step('forward'),  "Forward 1 frame"),
                ("▶",  lambda: _mpv_seek(0.1),              "Forward 100ms"),
                ("▶▶", lambda: _mpv_seek(1),                "Forward 1 second"),
                ("⏭",  lambda: _mpv_seek(5),                "Forward 5 seconds"),
            ]
            for _sym, _cmd, _tip in _transport_btns:
                _px = 2 if _sym == "⏯" else 1
                _b = ttk.Button(transport_f, text=_sym, width=_tb_w, command=_cmd)
                _b.pack(side='left', padx=_px)
                create_tooltip(_b, _tip)

            mark_btn = ttk.Button(transport_f, text="⏱ Mark",
                                  command=_mark_time, width=6,
                                  state='disabled')
            mark_btn.pack(side='left', padx=(6, 0))
            create_tooltip(mark_btn, "Capture current playback time")

            # ── Volume controls ──
            def _mpv_toggle_mute():
                if not _mpv_proc[0] or _mpv_proc[0].poll() is not None:
                    return
                _mpv_cmd(["cycle", "mute"])
                # Update mute button label
                resp = _mpv_cmd(["get_property", "mute"])
                if resp and 'data' in resp:
                    _mute_btn.configure(text="🔇" if resp['data'] else "🔊")

            def _mpv_set_volume(val):
                if not _mpv_proc[0] or _mpv_proc[0].poll() is not None:
                    return
                _mpv_cmd(["set_property", "volume", float(val)])

            _mute_btn = ttk.Button(transport_f, text="🔊", width=2,
                                   command=_mpv_toggle_mute)
            _mute_btn.pack(side='right', padx=(4, 0))
            create_tooltip(_mute_btn, "Mute / Unmute")

            _vol_var = tk.DoubleVar(value=100)
            _vol_scale = ttk.Scale(transport_f, from_=0, to=100,
                                   orient='horizontal', length=80,
                                   variable=_vol_var,
                                   command=_mpv_set_volume)
            _vol_scale.pack(side='right', padx=2)
            create_tooltip(_vol_scale, "Volume")

            # ── Sync controls ──
            ttk.Separator(f, orient='horizontal').grid(
                row=4, column=0, columnspan=3, sticky='ew', pady=6)

            sync_f = ttk.Frame(f)
            sync_f.grid(row=5, column=0, columnspan=3, sticky='ew', padx=4)
            sync_f.columnconfigure(1, weight=1)

            ttk.Label(sync_f, text="First cue:",
                      font=('Helvetica', 9, 'bold')).grid(
                          row=0, column=0, sticky='w', pady=1)
            ttk.Label(sync_f, text=f'"{preview_text}"',
                      font=('Helvetica', 9), foreground='gray').grid(
                          row=0, column=1, columnspan=2, sticky='w', padx=8, pady=1)

            ttk.Label(sync_f, text="Current:").grid(
                row=1, column=0, sticky='w', pady=1)
            ttk.Label(sync_f, text=current_start,
                      font=('Courier', 10)).grid(
                          row=1, column=1, sticky='w', padx=8, pady=1)

            ttk.Label(sync_f, text="New start:").grid(
                row=2, column=0, sticky='w', pady=2)
            time_var = tk.StringVar(value=current_start)
            _time_f = ttk.Frame(sync_f)
            _time_f.grid(row=2, column=1, columnspan=2, sticky='w', padx=8, pady=2)
            time_entry = ttk.Entry(_time_f, textvariable=time_var, width=16,
                                   font=('Courier', 10))
            time_entry.pack(side='left')
            ttk.Label(_time_f, text="HH:MM:SS,mmm",
                      foreground='gray', font=('Helvetica', 8)).pack(
                          side='left', padx=8)

            offset_var = tk.StringVar(value="Offset: 0ms")
            ttk.Label(sync_f, textvariable=offset_var,
                      font=('Helvetica', 9), foreground='#666').grid(
                          row=3, column=0, columnspan=3, sticky='w', pady=1)

            def _update_offset(*_args):
                try:
                    new_ms = srt_ts_to_ms(time_var.get().strip())
                    old_ms = srt_ts_to_ms(current_start)
                    diff = new_ms - old_ms
                    sign = '+' if diff >= 0 else ''
                    offset_var.set(f"Offset: {sign}{diff}ms ({sign}{diff/1000:.1f}s)")
                except Exception:
                    offset_var.set("Offset: (invalid time format)")
            time_var.trace_add('write', _update_offset)

            # ── Action buttons ──
            btn_f = ttk.Frame(f)
            btn_f.grid(row=6, column=0, columnspan=3, sticky='ew', pady=(8, 0))

            def _apply_first_cue():
                nonlocal cues
                try:
                    new_ms = srt_ts_to_ms(time_var.get().strip())
                except Exception:
                    messagebox.showwarning("Invalid Time",
                        "Enter time in SRT format: HH:MM:SS,mmm", parent=qd)
                    return
                old_ms = srt_ts_to_ms(current_start)
                offset = new_ms - old_ms
                if offset == 0:
                    _on_close()
                    return
                push_undo()
                cues = shift_timestamps(cues, offset)
                refresh_tree(cues)
                sign = '+' if offset > 0 else ''
                app.add_log(f"Quick Sync: shifted all cues {sign}{offset}ms "
                             f"(first cue → {time_var.get().strip()})", 'SUCCESS')
                _on_close()

            time_entry.bind('<Return>', lambda e: _apply_first_cue())
            _apply_btn = ttk.Button(btn_f, text="Apply",
                                    command=_apply_first_cue, width=8)
            _apply_btn.pack(side='left', padx=2)
            create_tooltip(_apply_btn, "Shift all cues by the offset and close")
            _cancel_btn = ttk.Button(btn_f, text="Cancel",
                                     command=_on_close, width=8)
            _cancel_btn.pack(side='left', padx=2)
            create_tooltip(_cancel_btn, "Close without applying changes")

            # Auto-load video if one was detected
            if _qs_vpath.get().strip() and os.path.isfile(_qs_vpath.get().strip()):
                qd.after(300, _play_video)

        quick_sync_menu.add_command(label="Set First Cue Time...",
                                    command=_quick_sync_first_cue)

        # ── View menu ──
        view_menu_int = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="View", menu=view_menu_int)
        view_menu_int.add_command(label="Show/Hide Timeline",
                                  command=_toggle_timeline_int,
                                  accelerator="Ctrl+T")
        editor.bind('<Control-t>', lambda e: _toggle_timeline_int())
        editor.bind('<Control-T>', lambda e: _toggle_timeline_int())

        def _show_smart_sync():
            """Auto-sync subtitles using Whisper speech recognition."""
            import threading

            if not cues:
                messagebox.showinfo("No Subtitles", "Load subtitles first.", parent=editor)
                return

            # Check faster-whisper
            try:
                from faster_whisper import WhisperModel
            except ImportError:
                if messagebox.askyesno("Missing Package",
                    "faster-whisper is not installed.\n\n"
                    "Would you like to install it now?\n"
                    "(This may take a few minutes — downloads ~200MB)",
                    parent=editor):
                    try:
                        app.add_log("Installing faster-whisper...", 'INFO')
                        _pip_result = subprocess.run(
                            [sys.executable, '-m', 'pip', 'install',
                             '--user', '--break-system-packages', 'faster-whisper'],
                            capture_output=True, text=True, timeout=300)
                        if _pip_result.returncode == 0:
                            app.add_log("faster-whisper installed", 'SUCCESS')
                        else:
                            messagebox.showerror("Install Failed",
                                f"pip install failed:\n{_pip_result.stderr[-300:]}",
                                parent=editor)
                            return
                    except Exception as _e:
                        messagebox.showerror("Install Failed", str(_e), parent=editor)
                        return
                else:
                    return

            vpath = filepath  # internal editor always has a video file path

            sd = tk.Toplevel(editor)
            sd.title("Smart Sync")
            sd.geometry("560x580")
            sd.resizable(True, True)
            app._center_on_main(sd)

            f = ttk.Frame(sd, padding=12)
            f.pack(fill='both', expand=True)
            f.columnconfigure(1, weight=1)
            _sp = {'padx': 6, 'pady': 4}

            ttk.Label(f, text="Video file:").grid(row=0, column=0, sticky='w', **_sp)
            vpath_var = tk.StringVar(value=vpath or '')
            ttk.Entry(f, textvariable=vpath_var).grid(row=0, column=1, sticky='ew', **_sp)
            def _browse_vid():
                # Start in the subtitle's folder if available
                init_dir = ''
                if vpath_var.get():
                    init_dir = os.path.dirname(vpath_var.get())
                elif current_path[0]:
                    init_dir = os.path.dirname(current_path[0])
                # Try zenity first (better sizing), fall back to tkinter
                p = None
                if shutil.which('zenity'):
                    try:
                        cmd = ['zenity', '--file-selection',
                               '--title', 'Select Video File',
                               '--file-filter', 'Video files|*.mkv *.mp4 *.avi *.mov *.ts *.m2ts *.mts *.webm *.wmv *.flv',
                               '--file-filter', 'All files|*']
                        if init_dir:
                            cmd += ['--filename', init_dir + '/']
                        result = subprocess.run(cmd, capture_output=True,
                                                text=True, timeout=120)
                        if result.returncode == 0 and result.stdout.strip():
                            p = result.stdout.strip()
                    except Exception:
                        pass
                if not p:
                    p = filedialog.askopenfilename(
                        parent=sd,
                        title="Select Video File",
                        initialdir=init_dir or None,
                        filetypes=[("Video files", "*.mkv *.mp4 *.avi *.mov *.ts *.m2ts"),
                                   ("All files", "*.*")])
                if p:
                    vpath_var.set(p)
            ttk.Button(f, text="Browse...", command=_browse_vid).grid(row=0, column=2, **_sp)

            model_label = ttk.Label(f, text="Whisper model:")
            model_label.grid(row=1, column=0, sticky='w', **_sp)
            model_f = ttk.Frame(f)
            model_f.grid(row=1, column=1, columnspan=2, sticky='w', **_sp)
            model_var = tk.StringVar(value='base')
            for m, tip in [('tiny', '~75MB, fastest'),
                           ('base', '~150MB, good balance'),
                           ('small', '~500MB, more accurate')]:
                ttk.Radiobutton(model_f, text=f"{m} ({tip})",
                               variable=model_var, value=m).pack(anchor='w')

            ttk.Label(f, text="Language:").grid(row=2, column=0, sticky='w', **_sp)
            lang_var = tk.StringVar(value='en')
            lang_f = ttk.Frame(f)
            lang_f.grid(row=2, column=1, columnspan=2, sticky='w', **_sp)
            ttk.Entry(lang_f, textvariable=lang_var, width=5).pack(side='left')
            ttk.Label(lang_f, text="(en, fr, es, de, etc. — blank = auto-detect)",
                      foreground='gray', font=('Helvetica', 8)).pack(side='left', padx=8)

            # ── Engine selection ──
            ttk.Label(f, text="Engine:").grid(row=3, column=0, sticky='w', **_sp)
            engine_f = ttk.Frame(f)
            engine_f.grid(row=3, column=1, columnspan=2, sticky='w', **_sp)
            engine_var = tk.StringVar(value='faster-whisper')

            def _on_engine_change():
                eng = engine_var.get()
                if eng == 'whisperx':
                    finetune_var.set('200')
                    finetune_hint.config(
                        text="ms  (phoneme onset is ~200ms before perceived speech)")
                    direct_rb.configure(state='normal')
                else:
                    finetune_var.set('400')
                    finetune_hint.config(
                        text="ms  (applied after sync — compensates for Whisper timing)")
                    # Direct Align requires WhisperX — switch away if selected
                    if scan_mode_var.get() == 'direct':
                        scan_mode_var.set('quick')
                    direct_rb.configure(state='disabled')
                _on_scan_mode_change()

            ttk.Radiobutton(engine_f, text="Standard (faster-whisper)",
                           variable=engine_var, value='faster-whisper',
                           command=_on_engine_change).pack(anchor='w')
            ttk.Radiobutton(engine_f,
                           text="Precise (WhisperX) — phoneme-level alignment",
                           variable=engine_var, value='whisperx',
                           command=_on_engine_change).pack(anchor='w')

            # ── Scan mode ──
            ttk.Label(f, text="Scan mode:").grid(row=4, column=0, sticky='w', **_sp)
            scan_f = ttk.Frame(f)
            scan_f.grid(row=4, column=1, columnspan=2, sticky='w', **_sp)
            scan_mode_var = tk.StringVar(value='quick')

            def _on_scan_mode_change():
                mode = scan_mode_var.get()
                if mode == 'quick':
                    seg_label.grid()
                    sample_f.grid()
                    model_label.grid()
                    model_f.grid()
                elif mode == 'full':
                    seg_label.grid_remove()
                    sample_f.grid_remove()
                    model_label.grid()
                    model_f.grid()
                else:  # direct
                    seg_label.grid_remove()
                    sample_f.grid_remove()
                    model_label.grid_remove()
                    model_f.grid_remove()

            ttk.Radiobutton(scan_f, text="Quick Scan", variable=scan_mode_var,
                           value='quick', command=_on_scan_mode_change).pack(side='left', padx=(0, 8))
            ttk.Radiobutton(scan_f, text="Full Scan (for Re-time)",
                           variable=scan_mode_var, value='full',
                           command=_on_scan_mode_change).pack(side='left', padx=(0, 8))
            direct_rb = ttk.Radiobutton(scan_f,
                           text="Direct Align",
                           variable=scan_mode_var, value='direct',
                           command=_on_scan_mode_change, state='disabled')
            direct_rb.pack(side='left')

            seg_label = ttk.Label(f, text="Segments:")
            seg_label.grid(row=5, column=0, sticky='w', **_sp)
            sample_f = ttk.Frame(f)
            sample_f.grid(row=5, column=1, columnspan=2, sticky='w', **_sp)
            segments_var = tk.StringVar(value='3')
            seg_spin = tk.Spinbox(sample_f, textvariable=segments_var, from_=1, to=20,
                        width=3)
            seg_spin.pack(side='left')
            ttk.Label(sample_f, text="× ").pack(side='left')
            sample_len_var = tk.StringVar(value='5')
            len_spin = tk.Spinbox(sample_f, textvariable=sample_len_var, from_=1, to=30,
                        width=3)
            len_spin.pack(side='left')
            ttk.Label(sample_f, text="min each",
                      foreground='gray', font=('Helvetica', 8)).pack(side='left', padx=4)

            # ── Offset adjustment ──
            ttk.Label(f, text="Fine-tune:").grid(row=6, column=0, sticky='w', **_sp)
            finetune_f = ttk.Frame(f)
            finetune_f.grid(row=6, column=1, columnspan=2, sticky='w', **_sp)
            finetune_var = tk.StringVar(value='400')
            tk.Spinbox(finetune_f, textvariable=finetune_var, from_=-2000, to=2000,
                       increment=50, width=6).pack(side='left')
            finetune_hint = ttk.Label(finetune_f,
                      text="ms  (applied after sync — compensates for Whisper timing)",
                      foreground='gray', font=('Helvetica', 8))
            finetune_hint.pack(side='left', padx=4)

            status_var = tk.StringVar(value="Ready — click Start to begin")
            ttk.Label(f, textvariable=status_var, wraplength=450,
                      font=('Helvetica', 9)).grid(row=7, column=0, columnspan=3, sticky='w', **_sp)

            progress_var = tk.DoubleVar(value=0)
            ttk.Progressbar(f, variable=progress_var, maximum=100,
                           mode='determinate').grid(row=8, column=0, columnspan=3,
                                                      sticky='ew', **_sp)

            result_frame = ttk.LabelFrame(f, text="Results", padding=6)
            result_frame.grid(row=9, column=0, columnspan=3, sticky='nsew', **_sp)
            result_frame.columnconfigure(0, weight=1)
            result_frame.rowconfigure(0, weight=1)
            f.rowconfigure(9, weight=1)

            result_text = tk.Text(result_frame, height=8, wrap='word',
                                 font=('Courier', 9), state='disabled',
                                 bg='#1e1e1e', fg='#d4d4d4')
            result_text.grid(row=0, column=0, sticky='nsew')
            r_scroll = ttk.Scrollbar(result_frame, orient='vertical', command=result_text.yview)
            r_scroll.grid(row=0, column=1, sticky='ns')
            result_text.configure(yscrollcommand=r_scroll.set)

            def _rlog(msg):
                result_text.configure(state='normal')
                result_text.insert('end', msg + '\n')
                result_text.see('end')
                result_text.configure(state='disabled')

            btn_f = ttk.Frame(f)
            btn_f.grid(row=10, column=0, columnspan=3, sticky='ew', pady=(8, 0))

            cancel_event = threading.Event()
            sync_result = [None]
            pre_sync_cues = [None]  # snapshot before sync — for repeatable Re-time

            def _start():
                vp = vpath_var.get().strip()
                if not vp or not os.path.isfile(vp):
                    messagebox.showwarning("No Video", "Select a video file.", parent=sd)
                    return

                # Save cues before sync so Re-time/Apply can repeat with different fine-tune
                import copy as _copy
                pre_sync_cues[0] = _copy.deepcopy(cues)

                # ── Engine-aware dependency check ──
                _engine = engine_var.get()
                if _engine == 'whisperx':
                    try:
                        import whisperx
                    except ImportError:
                        if messagebox.askyesno("Missing Package",
                            "WhisperX is not installed.\n\n"
                            "Would you like to install it now?\n"
                            "(Requires PyTorch — downloads ~2GB)",
                            parent=sd):
                            # Run pip install in background thread with progress
                            start_btn.configure(state='disabled')
                            status_var.set("Installing whisperx (downloading ~2GB)...")
                            app.add_log("Installing whisperx...", 'INFO')
                            _rlog("Installing whisperx — this may take several minutes...")
                            # Switch progress bar to indeterminate mode
                            _install_pbar = None
                            for _w in f.winfo_children():
                                if isinstance(_w, ttk.Progressbar):
                                    _install_pbar = _w
                                    break
                            if _install_pbar:
                                _install_pbar.configure(mode='indeterminate')
                                _install_pbar.start(15)

                            def _do_whisperx_install():
                                try:
                                    proc = subprocess.Popen(
                                        [sys.executable, '-m', 'pip', 'install',
                                         '--user', '--break-system-packages',
                                         '--progress-bar', 'off',
                                         'whisperx', 'transformers<4.45'],
                                        stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT,
                                        text=True)
                                    for line in proc.stdout:
                                        line = line.rstrip()
                                        if line:
                                            sd.after(0, lambda l=line:
                                                     status_var.set(l[:80]))
                                            sd.after(0, lambda l=line: _rlog(l))
                                    proc.wait(timeout=600)
                                    if proc.returncode == 0:
                                        sd.after(0, lambda: status_var.set(
                                            "whisperx installed — click Start"))
                                        sd.after(0, lambda: _rlog(
                                            "whisperx installed successfully"))
                                        sd.after(0, lambda: app.add_log(
                                            "whisperx installed successfully",
                                            'SUCCESS'))
                                    else:
                                        sd.after(0, lambda: status_var.set(
                                            "whisperx install failed"))
                                        sd.after(0, lambda: _rlog(
                                            "Install failed — check log above"))
                                except Exception as _e:
                                    sd.after(0, lambda: status_var.set(
                                        f"Install error: {_e}"))
                                    sd.after(0, lambda: _rlog(f"Error: {_e}"))
                                finally:
                                    def _reset_after_install():
                                        start_btn.configure(state='normal')
                                        if _install_pbar:
                                            _install_pbar.stop()
                                            _install_pbar.configure(
                                                mode='determinate')
                                            progress_var.set(0)
                                    sd.after(0, _reset_after_install)

                            import threading as _inst_threading
                            _inst_threading.Thread(
                                target=_do_whisperx_install,
                                daemon=True).start()
                            return  # exit _start(); user clicks Start after install
                        else:
                            return

                start_btn.configure(state='disabled')
                apply_btn.configure(state='disabled')
                cancel_event.clear()
                # Capture Tk variables on main thread before entering background thread
                lang = lang_var.get().strip() or None
                model = model_var.get()
                _scan_mode = scan_mode_var.get()
                if _scan_mode == 'direct':
                    engine_value = 'whisperx-align'
                else:
                    engine_value = engine_var.get()
                is_full_scan = _scan_mode == 'full'
                _seg_str = segments_var.get().strip()
                _len_str = sample_len_var.get().strip()
                n_segs = int(_seg_str) if _seg_str.isdigit() else 3
                s_mins = int(_len_str) if _len_str.isdigit() else 5
                _ft_str = finetune_var.get().strip().lstrip('+')
                finetune_ms = int(_ft_str) if _ft_str.lstrip('-').isdigit() else 400
                if is_full_scan or _scan_mode == 'direct':
                    n_segs = 0  # signal for full scan
                    s_mins = 0

                import time as _sync_time
                _last_ui_update = [0]

                def _progress(msg):
                    # Throttle UI updates to max 4 per second to avoid flooding Tk event queue
                    now = _sync_time.monotonic()
                    is_milestone = ('segment' in msg.lower() and '/' in msg) or \
                                   'Matched' in msg or 'Loading' in msg or \
                                   'Extracting' in msg or 'Transcribed' in msg or \
                                   'Aligning' in msg or 'alignment' in msg.lower() or \
                                   'WhisperX' in msg or 'Falling back' in msg or \
                                   'failed' in msg.lower() or 'complete' in msg.lower() or \
                                   'error' in msg.lower() or 'RESULT' in msg or \
                                   'Done' in msg or '===' in msg or 'Drift' in msg or \
                                   'Sync' in msg
                    if not is_milestone and (now - _last_ui_update[0]) < 0.25:
                        return
                    _last_ui_update[0] = now

                    def _do_update():
                        status_var.set(msg)
                        _rlog(msg)
                        import re as _re
                        m = _re.search(r'segment (\d+)/(\d+)', msg, _re.IGNORECASE)
                        if m:
                            seg_n, seg_t = int(m.group(1)), int(m.group(2))
                            progress_var.set((seg_n / seg_t) * 100)
                        m2 = _re.search(r'Matching cue (\d+)/(\d+)', msg)
                        if m2:
                            mc, mt = int(m2.group(1)), int(m2.group(2))
                            progress_var.set((mc / mt) * 100)
                        elif 'Extracting audio' in msg:
                            progress_var.set(0)
                    sd.after(0, _do_update)

                def _set_start_enabled():
                    start_btn.configure(state='normal')
                def _set_apply_enabled():
                    apply_btn.configure(state='normal')
                    progress_var.set(100)

                def _run():
                    try:
                        result = smart_sync(vp, cues, model_size=model,
                                            language=lang,
                                            num_segments=n_segs,
                                            sample_minutes=s_mins,
                                            progress_callback=_progress,
                                            cancel_event=cancel_event,
                                            engine=engine_value)
                    except Exception as _e:
                        _progress(f"Error: {_e}")
                        result = None
                    sync_result[0] = result

                    import time as _t
                    _t.sleep(0.3)

                    if cancel_event.is_set():
                        _progress("Sync cancelled by user")
                    elif result:
                        try:
                            ro = result['offset_ms']
                            rd = result['drift_ms']
                            rm = result['matches']
                            sign = '+' if ro > 0 else ''
                            _progress(f"{'='*40}")
                            _progress(f"RESULT: Offset: {sign}{ro}ms ({sign}{ro/1000:.1f}s)")
                            _progress(f"Drift: {rd:+d}ms")
                            _progress(f"Matched: {len(rm)}/{len(cues)} cues")
                            _progress(f"{'='*40}")
                            for ci, wt, ct, sim, txt in rm[:10]:
                                _progress(f"  #{ci+1} sim={sim:.0%} "
                                          f"sub={ms_to_srt_ts(ct)[:8]} "
                                          f"audio={ms_to_srt_ts(wt)[:8]} "
                                          f"\"{txt}\"")
                            _progress(f"Done — click Apply Sync to apply {sign}{ro}ms offset")
                            sd.after(0, lambda: apply_btn.configure(state='normal'))
                            sd.after(0, lambda: retime_btn.configure(state='normal'))
                            sd.after(0, lambda: progress_var.set(100))
                        except Exception as _e:
                            _progress(f"Error displaying results: {_e}")
                    else:
                        _progress("Sync failed — no results")

                    sd.after(0, _set_start_enabled)

                t = threading.Thread(target=_run, daemon=True)
                t.start()

            def _apply():
                nonlocal cues
                if not sync_result[0]:
                    return
                offset = sync_result[0]['offset_ms']
                _ft = finetune_var.get().strip().lstrip('+')
                ft = int(_ft) if _ft.lstrip('-').isdigit() else 400
                total_offset = offset + ft

                # Always apply from the pre-sync snapshot so fine-tune is repeatable
                import copy as _copy
                push_undo()
                if pre_sync_cues[0] is not None:
                    cues = _copy.deepcopy(pre_sync_cues[0])
                cues = shift_timestamps(cues, total_offset)
                refresh_tree(cues)
                sign = '+' if total_offset > 0 else ''
                app.add_log(f"Smart Sync applied: {sign}{total_offset}ms "
                             f"(offset {offset:+d}ms + fine-tune {ft:+d}ms)", 'SUCCESS')
                _rlog(f"\nApplied: {sign}{total_offset}ms (offset {offset:+d} + fine-tune {ft:+d})")
                status_var.set(f"Sync applied: {sign}{total_offset}ms")

            def _retime():
                nonlocal cues
                if not sync_result[0]:
                    return
                result = sync_result[0]
                matched = result['matches']
                _ft = finetune_var.get().strip().lstrip('+')
                ft = int(_ft) if _ft.lstrip('-').isdigit() else 400

                # Always retime from the pre-sync snapshot so fine-tune is repeatable
                import copy as _copy
                push_undo()
                if pre_sync_cues[0] is not None:
                    cues = _copy.deepcopy(pre_sync_cues[0])
                cues = retime_subtitles(cues, matched)
                if ft != 0:
                    cues = shift_timestamps(cues, ft)
                refresh_tree(cues)
                ft_msg = f" + fine-tune {ft:+d}ms" if ft else ""
                app.add_log(f"Re-timed {len(cues)} cues using {len(matched)} anchors{ft_msg}",
                             'SUCCESS')
                _rlog(f"\nRe-timed {len(cues)} cues using {len(matched)} anchors{ft_msg}")
                status_var.set(f"Re-timed using {len(matched)} anchors{ft_msg}")

            def _cancel():
                cancel_event.set()
                status_var.set("Cancelling...")

            start_btn = ttk.Button(btn_f, text="▶ Start", command=_start, width=8)
            start_btn.pack(side='left', padx=2)
            apply_btn = ttk.Button(btn_f, text="Apply Sync", command=_apply,
                                    width=10, state='disabled')
            apply_btn.pack(side='left', padx=2)
            retime_btn = ttk.Button(btn_f, text="Re-time All", command=_retime,
                                     width=10, state='disabled')
            retime_btn.pack(side='left', padx=2)
            ttk.Button(btn_f, text="Cancel", command=_cancel, width=8).pack(side='left', padx=2)

            def _save_from_sync():
                do_save_file()
                _rlog("Saved.")
                status_var.set("Saved")

            ttk.Button(btn_f, text="💾 Save", command=_save_from_sync,
                       width=6).pack(side='right', padx=2)
            ttk.Button(btn_f, text="Close", command=sd.destroy, width=6).pack(side='right', padx=2)

        # ══════════════════════════════════════════════════════════════════════
        # Search & Replace toolbar (compact single row)
        # ══════════════════════════════════════════════════════════════════════
        search_frame = ttk.Frame(editor, padding=(10, 4, 10, 4))
        search_frame.pack(fill='x')

        def _add_entry_context_menu(entry):
            """Attach a right-click Cut/Copy/Paste menu to a ttk.Entry."""
            menu = tk.Menu(entry, tearoff=0)
            menu.add_command(label="Cut",
                command=lambda: entry.event_generate('<<Cut>>'))
            menu.add_command(label="Copy",
                command=lambda: entry.event_generate('<<Copy>>'))
            menu.add_command(label="Paste",
                command=lambda: entry.event_generate('<<Paste>>'))
            menu.add_separator()
            menu.add_command(label="Select All",
                command=lambda: (entry.select_range(0, 'end'),
                                 entry.icursor('end')))
            def _show(event):
                menu.tk_popup(event.x_root, event.y_root)
            entry.bind('<Button-3>', _show)

        ttk.Label(search_frame, text="Find:").pack(side='left')
        find_entry = ttk.Entry(search_frame, textvariable=find_var, width=20)
        find_entry.pack(side='left', padx=(2, 6))
        find_entry.bind('<Return>', lambda e: do_find())
        _add_entry_context_menu(find_entry)

        ttk.Label(search_frame, text="Replace:").pack(side='left')
        replace_entry = ttk.Entry(search_frame, textvariable=replace_var, width=20)
        replace_entry.pack(side='left', padx=(2, 6))
        _add_entry_context_menu(replace_entry)

        ttk.Button(search_frame, text="Find", command=do_find).pack(side='left', padx=2)
        ttk.Button(search_frame, text="Replace",
                   command=do_replace_one).pack(side='left', padx=2)
        ttk.Button(search_frame, text="Replace All",
                   command=do_replace_all).pack(side='left', padx=2)
        ttk.Checkbutton(search_frame, text="Wrap",
                        variable=wrap_around).pack(side='left', padx=(6, 2))

        # Bind Ctrl+F to focus the find field
        editor.bind('<Control-f>', lambda e: find_entry.focus_set())
        editor.bind('<Control-F>', lambda e: find_entry.focus_set())

        ttk.Separator(editor, orient='horizontal').pack(fill='x')

        # ══════════════════════════════════════════════════════════════════════
        # PanedWindow: (Video + Treeview) / Waveform Timeline
        # ══════════════════════════════════════════════════════════════════════
        paned = tk.PanedWindow(editor, orient='vertical',
                               sashwidth=6, sashrelief='raised')
        paned.pack(fill='both', expand=True, padx=10, pady=(4, 0))

        top_paned = tk.PanedWindow(paned, orient='horizontal',
                                    sashwidth=6, sashrelief='raised')

        # ── Video panel ──
        video_panel_int = ttk.Frame(top_paned, relief='sunken', borderwidth=1)
        video_embed_frame_int = tk.Frame(video_panel_int, bg='black',
                                          width=320, height=240)
        video_embed_frame_int.pack(fill='both', expand=True)
        video_embed_frame_int.pack_propagate(False)
        video_visible_int = [False]

        tree_frame = ttk.Frame(top_paned)

        tree_scroll_y = ttk.Scrollbar(tree_frame, orient='vertical')
        tree_scroll_y.pack(side='right', fill='y')

        tree = ttk.Treeview(tree_frame, columns=('num', 'time', 'text'),
                            show='headings', yscrollcommand=tree_scroll_y.set,
                            selectmode='extended')
        tree_scroll_y.config(command=tree.yview)

        tree.heading('num', text='#')
        tree.heading('time', text='Timestamp')
        tree.heading('text', text='Text')
        tree.column('num', width=40, minwidth=30, stretch=False)
        tree.column('time', width=260, minwidth=220, stretch=False)
        tree.column('text', width=500, minwidth=200, stretch=True)
        tree.pack(fill='both', expand=True)

        # ── Color coding ──
        tree.tag_configure(TAG_MODIFIED, background='#fff3cd')   # yellow — modified
        tree.tag_configure(TAG_HI, background='#cce5ff')         # blue — HI content
        tree.tag_configure(TAG_TAGS, background='#f8d7da')       # pink — has tags
        tree.tag_configure(TAG_LONG, background='#ffe0b2')       # orange — long lines
        tree.tag_configure(TAG_SEARCH, background='#c8e6c9')     # green — search match
        tree.tag_configure(TAG_SPELL, background='#f5c6cb')      # red/salmon — spelling errors

        # ── Mousewheel scrolling (consume events to prevent bleed-through) ──
        def on_tree_mousewheel(event):
            tree.yview_scroll(int(-1 * (event.delta / 120)), 'units')
            return 'break'

        def on_tree_scroll_up(event):
            tree.yview_scroll(-3, 'units')
            return 'break'

        def on_tree_scroll_down(event):
            tree.yview_scroll(3, 'units')
            return 'break'

        tree.bind('<MouseWheel>', on_tree_mousewheel)
        tree.bind('<Button-4>', on_tree_scroll_up)
        tree.bind('<Button-5>', on_tree_scroll_down)

        # ── Inline edit on double-click ──
        edit_entry = None

        def on_double_click(event):
            nonlocal edit_entry, cues
            item = tree.identify_row(event.y)
            col = tree.identify_column(event.x)
            if not item or col != '#3':  # Only edit the text column
                return

            bbox = tree.bbox(item, col)
            if not bbox:
                return
            x, y, w, h = bbox
            idx = int(item)

            if edit_entry:
                edit_entry.destroy()

            push_undo()
            edit_entry = tk.Text(tree_frame, wrap='word', height=3)
            edit_entry.place(x=x, y=y, width=w, height=max(h, 60))
            edit_entry.insert('1.0', cues[idx]['text'])
            edit_entry.focus_set()
            edit_entry.tag_configure('sel', background='#4a90d9')

            def save_edit(e=None):
                nonlocal edit_entry
                new_text = edit_entry.get('1.0', 'end-1c').strip()
                if new_text:
                    cues[idx]['text'] = new_text
                    display = new_text.replace('\n', ' \\n ')
                    tree.set(item, 'text', display)
                    # Update row color tag
                    orig_text = original_cues[idx]['text'] if idx < len(original_cues) else None
                    ctags = _classify_cue(cues[idx], orig_text)
                    row_tag = ''
                    for t in (TAG_MODIFIED, TAG_HI, TAG_TAGS, TAG_LONG):
                        if t in ctags:
                            row_tag = t
                            break
                    tree.item(item, tags=(row_tag,) if row_tag else ())
                else:
                    del cues[idx]
                    refresh_tree(cues)
                edit_entry.destroy()
                edit_entry = None
                # Update stats
                deleted_count.set(len(original_cues) - len(cues))
                mod = sum(1 for i, c in enumerate(cues) if i < len(original_cues)
                          and c['text'] != original_cues[i]['text'])
                modified_count.set(mod)
                long_count = sum(1 for c in cues
                                 if any(len(l) > MAX_CHARS_PER_LINE
                                        for l in c['text'].split('\n')))
                stats_parts = [
                    f"{len(cues)} entries",
                    f"{modified_count.get()} modified",
                    f"{deleted_count.get()} removed",
                ]
                if long_count:
                    stats_parts.append(f"{long_count} long lines")
                stats_label.configure(text=" │ ".join(stats_parts))

            def cancel_edit(e=None):
                nonlocal edit_entry
                if edit_entry:
                    edit_entry.destroy()
                    edit_entry = None
                    if undo_stack:
                        undo_stack.pop()  # discard the undo we pushed

            edit_entry.bind('<Escape>', cancel_edit)
            edit_entry.bind('<Control-Return>', save_edit)
            edit_entry.bind('<Tab>', save_edit)

            # Right-click context menu for copy/paste
            edit_ctx = tk.Menu(edit_entry, tearoff=0)

            def _edit_action(action):
                """Perform an edit action and refocus the edit widget."""
                if not edit_entry:
                    return
                if action == 'cut':
                    edit_entry.event_generate('<<Cut>>')
                elif action == 'copy':
                    edit_entry.event_generate('<<Copy>>')
                elif action == 'paste':
                    edit_entry.event_generate('<<Paste>>')
                elif action == 'select_all':
                    edit_entry.tag_add('sel', '1.0', 'end')
                    edit_entry.mark_set('insert', 'end')
                edit_entry.focus_force()

            edit_ctx.add_command(label="Cut", command=lambda: _edit_action('cut'))
            edit_ctx.add_command(label="Copy", command=lambda: _edit_action('copy'))
            edit_ctx.add_command(label="Paste", command=lambda: _edit_action('paste'))
            edit_ctx.add_separator()
            edit_ctx.add_command(label="Select All",
                                command=lambda: _edit_action('select_all'))

            _edit_ctx_open = [False]

            def show_edit_ctx(event):
                _edit_ctx_open[0] = True
                def on_menu_close():
                    _edit_ctx_open[0] = False
                    if edit_entry:
                        edit_entry.focus_force()
                edit_ctx.tk_popup(event.x_root, event.y_root)
                # tk_popup is blocking on some platforms; schedule cleanup
                edit_entry.after(50, on_menu_close)
                return 'break'
            edit_entry.bind('<Button-3>', show_edit_ctx)

            def on_focus_out(e):
                if not edit_entry:
                    return
                # Wait for context menu interactions to complete
                def deferred_save():
                    if not edit_entry:
                        return
                    if _edit_ctx_open[0]:
                        # Menu still active, check again later
                        edit_entry.after(200, deferred_save)
                        return
                    try:
                        if edit_entry.focus_get() == edit_entry:
                            return  # focus came back, don't save
                    except Exception:
                        pass
                    save_edit()
                edit_entry.after(300, deferred_save)
            edit_entry.bind('<FocusOut>', on_focus_out)

        tree.bind('<Double-1>', on_double_click)

        # ── Video preview on right-click ──
        def preview_at_cue(event=None):
            """Play a short clip starting at the selected cue's timestamp."""
            selected = tree.selection()
            if not selected:
                return
            idx = int(selected[0])
            start_ts = cues[idx]['start'].replace(',', '.')
            # Play 5 seconds starting from the cue timestamp
            cmd = ['ffplay', '-ss', start_ts, '-t', '5',
                   '-autoexit', '-window_title', f'Preview — cue #{idx + 1}',
                   '-loglevel', 'quiet', filepath]
            try:
                subprocess.Popen(cmd)
            except FileNotFoundError:
                app.add_log("ffplay not found — cannot preview video", 'WARNING')
            except Exception as e:
                app.add_log(f"Preview error: {e}", 'ERROR')

        # Right-click context menu
        ctx_menu = tk.Menu(editor, tearoff=0)
        ctx_menu.add_command(label="▶ Preview at this cue", command=preview_at_cue)
        ctx_menu.add_separator()
        def insert_cue(position):
            """Insert a blank cue above or below the selected cue."""
            nonlocal cues
            selected = tree.selection()
            if not selected:
                return
            idx = int(selected[0])
            ref = cues[idx]

            if position == 'above':
                # Place the new cue just before the selected one
                ref_start_ms = srt_ts_to_ms(ref['start'])
                new_end_ms = max(ref_start_ms - 1, 0)
                new_start_ms = max(new_end_ms - 2000, 0)
                insert_idx = idx
            else:
                # Place the new cue just after the selected one
                ref_end_ms = srt_ts_to_ms(ref['end'])
                new_start_ms = ref_end_ms + 1
                new_end_ms = new_start_ms + 2000
                insert_idx = idx + 1

            push_undo()
            new_cue = {
                'index': 0,
                'start': ms_to_srt_ts(new_start_ms),
                'end': ms_to_srt_ts(new_end_ms),
                'text': ' ',
            }
            cues.insert(insert_idx, new_cue)
            refresh_tree(cues)
            # Select the new cue and scroll to it
            tree.see(str(insert_idx))
            tree.selection_set(str(insert_idx))

        ctx_menu.add_command(label="✂ Split cue", command=split_selected)
        ctx_menu.add_command(label="⊕ Join selected cues", command=join_selected)
        ctx_menu.add_separator()
        ctx_menu.add_command(label="⤒ Insert line above", command=lambda: insert_cue('above'))
        ctx_menu.add_command(label="⤓ Insert line below", command=lambda: insert_cue('below'))
        ctx_menu.add_separator()
        ctx_menu.add_command(label="🗑 Delete selected", command=delete_selected)

        def show_context_menu(event):
            # Select the row under cursor if not already selected
            item = tree.identify_row(event.y)
            if item and item not in tree.selection():
                tree.selection_set(item)
            ctx_menu.tk_popup(event.x_root, event.y_root)

        tree.bind('<Button-3>', show_context_menu)

        # ── Waveform Timeline ──
        timeline_frame_int = ttk.Frame(paned)
        timeline_visible_int = [False]

        def _on_timeline_cue_modified_int(cue_idx, new_start_ms, new_end_ms):
            if cue_idx < len(cues):
                cues[cue_idx]['start'] = ms_to_srt_ts(int(new_start_ms))
                cues[cue_idx]['end'] = ms_to_srt_ts(int(new_end_ms))
                refresh_tree(cues)

        def _on_timeline_selection_int(cue_idx):
            iid = str(cue_idx)
            if tree.exists(iid):
                tree.selection_set(iid)
                tree.see(iid)
                tree.focus(iid)

        timeline_int = WaveformTimeline(
            timeline_frame_int,
            cues_fn=lambda: cues,
            on_cue_modified=_on_timeline_cue_modified_int,
            on_selection_changed=_on_timeline_selection_int,
            push_undo=push_undo,
            log_fn=app.add_log,
            video_frame=video_embed_frame_int,
        )
        timeline_int.pack(fill='both', expand=True)

        def _on_tree_select_int(event):
            sel = tree.selection()
            if sel:
                try:
                    idx = int(sel[0])
                    timeline_int.select_cue(idx)
                    if not timeline_int._drag:
                        timeline_int.scroll_to_cue(idx)
                except (ValueError, IndexError):
                    pass

        tree.bind('<<TreeviewSelect>>', _on_tree_select_int)

        # Build paned layout
        top_paned.add(tree_frame, stretch='always')
        paned.add(top_paned, stretch='always')

        def _show_video_int():
            if not video_visible_int[0]:
                top_paned.add(video_panel_int, before=tree_frame,
                              stretch='never', width=360)
                video_visible_int[0] = True

        def _show_timeline_int():
            if not timeline_visible_int[0]:
                paned.add(timeline_frame_int, stretch='always')
                paned.update_idletasks()
                total_h = paned.winfo_height()
                if total_h > 100:
                    paned.sash_place(0, 0, int(total_h * 0.65))
                timeline_visible_int[0] = True

        def _hide_timeline_int():
            if timeline_visible_int[0]:
                paned.forget(timeline_frame_int)
                timeline_visible_int[0] = False

        def _toggle_timeline_int():
            if timeline_visible_int[0]:
                _hide_timeline_int()
            else:
                _show_timeline_int()

        # Auto-load waveform from the video file (always available in internal editor)
        def _auto_load_waveform():
            _show_video_int()
            _show_timeline_int()
            if not timeline_int.is_loaded:
                timeline_int.load_audio(filepath)

        editor.after(200, _auto_load_waveform)

        # ══════════════════════════════════════════════════════════════════════
        # Status bar + action buttons
        # ══════════════════════════════════════════════════════════════════════
        status_frame = ttk.Frame(editor, padding=(10, 6, 10, 6))
        status_frame.pack(fill='x')

        stats_label = ttk.Label(status_frame,
                                text=f"{len(cues)} entries │ 0 modified │ 0 removed")
        stats_label.pack(side='left')

        def do_save():
            """Save edited subtitles (for encoding pipeline or external file)."""
            if not cues:
                messagebox.showwarning("Empty Subtitles",
                                       "All subtitle entries have been removed.\n"
                                       "Nothing to save.", parent=editor)
                return
            removed = len(original_cues) - len(cues)
            if is_external:
                # Write directly back to the external subtitle file
                with open(external_sub_path, 'w', encoding='utf-8') as f:
                    f.write(write_srt(cues))
                app.add_log(f"Subtitle edits saved: "
                             f"{len(cues)} entries ({removed} removed) → "
                             f"{os.path.basename(external_sub_path)}", 'SUCCESS')
            else:
                # Internal stream — write to temp file for encoding pipeline
                tmp_dir = tempfile.mkdtemp(prefix='docflix_sub_')
                out_path = os.path.join(tmp_dir, f"edited_stream{stream_index}.srt")
                with open(out_path, 'w', encoding='utf-8') as f:
                    f.write(write_srt(cues))
                if 'edited_subs' not in file_info:
                    file_info['edited_subs'] = {}
                file_info['edited_subs'][stream_index] = out_path
                app.add_log(f"Subtitle edits saved for stream #{stream_index}: "
                             f"{len(cues)} entries ({removed} removed) → {out_path}", 'SUCCESS')
            editor.destroy()

        def do_save_to_video():
            """Re-mux the edited subtitle directly back into the video file."""
            if not cues:
                messagebox.showwarning("Empty Subtitles",
                                       "All subtitle entries have been removed.\n"
                                       "Nothing to save.", parent=editor)
                return
            if is_external or not filepath:
                messagebox.showinfo("Not Available",
                    "Save to Video is only available for internal subtitle streams.",
                    parent=editor)
                return

            removed = len(original_cues) - len(cues)
            streams = get_subtitle_streams(filepath)

            # Write edited SRT to temp file
            tmp_srt = tempfile.NamedTemporaryFile(suffix='.srt', delete=False,
                                                   mode='w', encoding='utf-8')
            tmp_srt.close()
            with open(tmp_srt.name, 'w', encoding='utf-8') as f:
                f.write(write_srt(cues))

            # Build ffmpeg command: map every stream in order, replacing
            # the target subtitle with the edited version to preserve track order
            tmp_out = str(Path(filepath).with_suffix('.tmp' + Path(filepath).suffix))
            cmd = ['ffmpeg', '-y', '-i', filepath, '-i', tmp_srt.name]

            all_streams = get_all_streams(filepath)
            out_sub_count = 0
            replaced_out_sub_idx = None
            orig_stream = next(s for s in streams if s['index'] == stream_index)
            for s in all_streams:
                if s['index'] == stream_index:
                    # Replace this subtitle with the edited version
                    cmd.extend(['-map', '1:0'])
                    replaced_out_sub_idx = out_sub_count
                    out_sub_count += 1
                else:
                    cmd.extend(['-map', f"0:{s['index']}"])
                    if s['codec_type'] == 'subtitle':
                        out_sub_count += 1

            # Copy all codecs (no re-encoding)
            cmd.extend(['-c', 'copy'])

            # Preserve metadata on the replaced subtitle stream
            if replaced_out_sub_idx is not None:
                if orig_stream.get('language') and orig_stream['language'] != 'und':
                    cmd.extend([f'-metadata:s:s:{replaced_out_sub_idx}',
                                f"language={orig_stream['language']}"])
                if orig_stream.get('title'):
                    cmd.extend([f'-metadata:s:s:{replaced_out_sub_idx}',
                                f"title={orig_stream['title']}"])
                # Preserve disposition flags
                disp_parts = []
                if orig_stream.get('default'):
                    disp_parts.append('default')
                if orig_stream.get('forced'):
                    disp_parts.append('forced')
                if orig_stream.get('sdh'):
                    disp_parts.append('hearing_impaired')
                if disp_parts:
                    cmd.extend([f'-disposition:s:{replaced_out_sub_idx}',
                                '+'.join(disp_parts)])

                # For MP4 containers, subtitle codec must be mov_text
                if Path(filepath).suffix.lower() in ('.mp4', '.m4v'):
                    cmd.extend([f'-c:s:{replaced_out_sub_idx}', 'mov_text'])

            cmd.append(tmp_out)

            app.add_log(f"Re-muxing subtitle into {os.path.basename(filepath)}...", 'INFO')
            app.add_log(f"ffmpeg command: {' '.join(cmd)}", 'INFO')
            editor.update_idletasks()

            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
                if result.returncode != 0:
                    app.add_log(f"Re-mux stderr: {result.stderr[-500:]}", 'ERROR')
                    messagebox.showerror("Re-mux Failed",
                        f"Failed to save subtitle back to video:\n\n"
                        f"{result.stderr[-400:]}",
                        parent=editor)
                    if os.path.exists(tmp_out):
                        os.unlink(tmp_out)
                    os.unlink(tmp_srt.name)
                    return

                # Atomic replace: swap temp output over original
                os.replace(tmp_out, filepath)

                # Cleanup temp SRT
                try:
                    os.unlink(tmp_srt.name)
                except OSError:
                    pass

                app.add_log(f"Subtitle re-muxed into video: {len(cues)} entries "
                             f"({removed} removed) → {os.path.basename(filepath)}", 'SUCCESS')
                # Reset baseline so unsaved-changes check is accurate
                original_cues[:] = [dict(c) for c in cues]
                messagebox.showinfo("Saved",
                    f"Subtitle stream #{stream_index} saved back to:\n"
                    f"{os.path.basename(filepath)}",
                    parent=editor)
                editor.destroy()

            except Exception as e:
                messagebox.showerror("Error", f"Re-mux error:\n{e}", parent=editor)
                if os.path.exists(tmp_out):
                    os.unlink(tmp_out)
                try:
                    os.unlink(tmp_srt.name)
                except OSError:
                    pass

        def do_export():
            """Export the edited subtitle as a standalone .srt file."""
            if not cues:
                messagebox.showwarning("Empty", "No subtitle entries to export.",
                                       parent=editor)
                return
            out_dir = app.output_dir or Path(filepath).parent
            default_name = f"{Path(filepath).stem}.edited.srt"
            out_path = filedialog.asksaveasfilename(
                parent=editor,
                initialdir=str(out_dir),
                initialfile=default_name,
                defaultextension='.srt',
                filetypes=[('SubRip', '*.srt'), ('All files', '*.*')]
            )
            if not out_path:
                return
            with open(out_path, 'w', encoding='utf-8') as f:
                f.write(write_srt(cues))
            app.add_log(f"Exported edited subtitle → {out_path}", 'SUCCESS')

        ttk.Button(status_frame, text="💾 Save", command=do_save).pack(side='right', padx=(4, 0))
        if not is_external:
            ttk.Button(status_frame, text="💾 Save to Video",
                       command=do_save_to_video).pack(side='right', padx=(4, 0))
        ttk.Button(status_frame, text="Cancel", command=editor.destroy).pack(side='right')
        ttk.Button(status_frame, text="📤 Export SRT", command=do_export).pack(side='right', padx=4)
        ttk.Button(status_frame, text="▶ Preview",
                   command=preview_at_cue).pack(side='right', padx=4)

        # Populate tree
        refresh_tree(cues)

        # Delete key shortcut
        editor.bind('<Delete>', lambda e: None if isinstance(e.widget, tk.Text) else delete_selected())

        # Clean up timeline on close
        def _has_unsaved_changes_int():
            if len(cues) != len(original_cues):
                return True
            for c, o in zip(cues, original_cues):
                if (c.get('start') != o.get('start') or
                        c.get('end') != o.get('end') or
                        c.get('text') != o.get('text')):
                    return True
            return False

        def _on_internal_editor_close():
            if cues and _has_unsaved_changes_int():
                result = messagebox.askyesnocancel(
                    "Unsaved Changes",
                    "You have unsaved changes.\n\n"
                    "Would you like to save before closing?",
                    parent=editor)
                if result is None:
                    return  # Cancel — don't close
                if result:
                    do_save()  # Save first
            timeline_int.cleanup()
            editor.destroy()

        editor.protocol('WM_DELETE_WINDOW', _on_internal_editor_close)

        if not getattr(app, '_standalone_mode', False):
            editor.wait_window()



def main():
    """Launch Subtitle Editor as a standalone application.
    Accepts an optional file path as a command-line argument."""
    import sys
    from .standalone import create_standalone_root

    root, app = create_standalone_root(
        title="Subtitle Editor",
        geometry="900x650",
        minsize=(700, 500),
    )

    # Ensure required attributes exist on standalone context
    if not hasattr(app, "custom_ad_patterns"):
        app.custom_ad_patterns = []
    if not hasattr(app, "custom_replacements"):
        app.custom_replacements = []
    if not hasattr(app, "add_log"):
        app.add_log = lambda msg, level="INFO": None
    if not hasattr(app, "open_batch_filter"):
        from .batch_filter import open_batch_filter as _bf
        app.open_batch_filter = lambda: _bf(app)

    # Capture file argument before opening editor
    open_path = None
    if len(sys.argv) > 1 and os.path.isfile(sys.argv[1]):
        open_path = os.path.abspath(sys.argv[1])

    app._standalone_mode = True
    app._open_file_on_start = open_path
    root.withdraw()
    open_standalone_subtitle_editor(app)

    root.mainloop()


if __name__ == '__main__':
    main()
