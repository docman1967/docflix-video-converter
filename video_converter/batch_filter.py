"""
Docflix Video Converter — Batch Filter

Apply subtitle filters to multiple files at once.
Supports all filter types plus search/replace pairs.
"""

import os
from pathlib import Path
import re
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from .subtitle_filters import (
    parse_srt, write_srt,
    filter_remove_hi, filter_remove_caps_hi,
    filter_remove_music_notes, filter_fix_caps,
    filter_remove_tags, filter_remove_ads,
    filter_remove_offscreen_quotes,
    filter_remove_leading_dashes,
    filter_remove_duplicates, filter_merge_short,
    filter_reduce_lines,
)

try:
    from tkinterdnd2 import DND_FILES
    HAS_DND = True
except ImportError:
    HAS_DND = False


def open_batch_filter(app):
        import tempfile

        win = tk.Toplevel(app.root)
        win.title("Batch Filter Subtitles")
        win.geometry("620x700")
        win.resizable(True, True)
        app._center_on_main(win)

        file_paths = []  # list of absolute paths

        # ══════════════════════════════════════════════════════════════════════
        # Files section
        # ══════════════════════════════════════════════════════════════════════
        files_frame = ttk.LabelFrame(win, text="Subtitle Files", padding=8)
        files_frame.pack(fill='both', expand=True, padx=10, pady=(10, 5))

        list_frame = ttk.Frame(files_frame)
        list_frame.pack(fill='both', expand=True)

        list_scroll = ttk.Scrollbar(list_frame, orient='vertical')
        list_scroll.pack(side='right', fill='y')

        file_listbox = tk.Listbox(list_frame, height=10, font=('Courier', 9),
                                  selectmode='extended',
                                  yscrollcommand=list_scroll.set)
        file_listbox.pack(fill='both', expand=True)
        list_scroll.config(command=file_listbox.yview)

        file_count_var = tk.StringVar(value="0 files loaded")

        def _update_file_count():
            n = len(file_paths)
            file_count_var.set(f"{n} file{'s' if n != 1 else ''} loaded")

        def _add_paths(paths):
            """Add valid subtitle paths, skipping duplicates."""
            sub_exts = {'.srt', '.ass', '.ssa', '.vtt', '.sub'}
            added = 0
            for p in paths:
                if Path(p).suffix.lower() in sub_exts and p not in file_paths:
                    file_paths.append(p)
                    file_listbox.insert('end', os.path.basename(p))
                    added += 1
            if added:
                _update_file_count()
                app.add_log(f"Batch filter: added {added} subtitle file(s)", 'INFO')

        def add_files():
            paths = filedialog.askopenfilenames(
                parent=win,
                title="Select Subtitle Files",
                filetypes=[
                    ('Subtitle files', '*.srt *.ass *.ssa *.vtt *.sub'),
                    ('SubRip', '*.srt'),
                    ('All files', '*.*'),
                ]
            )
            if paths:
                _add_paths(paths)

        def remove_selected():
            selected = sorted(file_listbox.curselection(), reverse=True)
            for idx in selected:
                file_listbox.delete(idx)
                del file_paths[idx]
            _update_file_count()

        def clear_all():
            file_listbox.delete(0, 'end')
            file_paths.clear()
            _update_file_count()

        btn_frame = ttk.Frame(files_frame)
        btn_frame.pack(fill='x', pady=(6, 0))
        ttk.Button(btn_frame, text="Add Files...", command=add_files).pack(side='left', padx=(0, 4))
        ttk.Button(btn_frame, text="Remove Selected", command=remove_selected).pack(side='left', padx=4)
        ttk.Button(btn_frame, text="Clear All", command=clear_all).pack(side='left', padx=4)
        ttk.Label(btn_frame, textvariable=file_count_var,
                  foreground='gray').pack(side='right')

        # Drag-and-drop hint
        ttk.Label(files_frame, text="Drag and drop subtitle files here",
                  font=('Helvetica', 9), foreground='gray').pack(anchor='center', pady=(4, 0))

        # DnD registration
        def on_batch_drop(event):
            raw = event.data
            paths = []
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
            _add_paths(paths)

        if HAS_DND:
            win.drop_target_register(DND_FILES)
            win.dnd_bind('<<Drop>>', on_batch_drop)

        # ══════════════════════════════════════════════════════════════════════
        # Filters section
        # ══════════════════════════════════════════════════════════════════════
        filters_frame = ttk.LabelFrame(win, text="Filters to Apply", padding=8)
        filters_frame.pack(fill='x', padx=10, pady=5)

        if not hasattr(app, 'custom_cap_words'):
            app.custom_cap_words = []

        # Define filters: (key, label, filter_function)
        filter_defs = [
            ('remove_hi',      "Remove HI  [brackets] (parens) Speaker:",  filter_remove_hi),
            ('remove_tags',    "Remove Tags  <i> {\\an8}",        filter_remove_tags),
            ('remove_ads',     "Remove Ads / Credits",
             lambda c: filter_remove_ads(c, app.custom_ad_patterns)),
            ('remove_music',   "Remove Stray Notes  ♪ ♫",        filter_remove_music_notes),
            ('remove_dashes',  "Remove Leading Dashes  -",        filter_remove_leading_dashes),
            ('remove_caps_hi', "Remove ALL CAPS HI (UK style)",   filter_remove_caps_hi),
            ('remove_quotes',  "Remove Off-Screen Quotes ' ' (UK style)", filter_remove_offscreen_quotes),
            ('remove_dupes',   "Remove Duplicates",               filter_remove_duplicates),
            ('merge_short',    "Merge Short Cues",                filter_merge_short),
            ('reduce_lines',   "Reduce to 2 Lines",               filter_reduce_lines),
            ('fix_caps',       "Fix ALL CAPS",
             lambda c: filter_fix_caps(c, app.custom_cap_words)),
        ]

        filter_vars = {}
        # Two-column grid layout for filters
        cols_frame = ttk.Frame(filters_frame)
        cols_frame.pack(fill='x')
        left_col = ttk.Frame(cols_frame)
        left_col.pack(side='left', fill='both', expand=True, anchor='n')
        right_col = ttk.Frame(cols_frame)
        right_col.pack(side='left', fill='both', expand=True, anchor='n')

        mid = (len(filter_defs) + 1) // 2  # split point
        for i, (key, label, _) in enumerate(filter_defs):
            var = tk.BooleanVar(value=False)
            filter_vars[key] = var
            col = left_col if i < mid else right_col
            if key == 'fix_caps':
                caps_row = ttk.Frame(col)
                caps_row.pack(fill='x', anchor='w')
                ttk.Checkbutton(caps_row, text=label, variable=var).pack(side='left')
                ttk.Button(caps_row, text="Names...",
                           command=lambda: show_batch_names_dialog()).pack(side='left', padx=(4, 0))
            else:
                ttk.Checkbutton(col, text=label, variable=var).pack(anchor='w')

        def show_batch_names_dialog():
            """Open the custom names editor from the batch filter window."""
            nd = tk.Toplevel(win)
            nd.title("Custom Names — Fix ALL CAPS")
            nd.geometry("400x350")
            app._center_on_main(nd)
            nd.resizable(True, True)
            nd.attributes('-topmost', True)

            ttk.Label(nd, text="Add character names to preserve their\n"
                      "capitalisation when Fix ALL CAPS is applied.",
                      justify='center', padding=(10, 10)).pack()

            lf = ttk.LabelFrame(nd, text="Custom Names (saved across sessions)",
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

            btn_frame = ttk.Frame(nd, padding=(10, 8, 10, 10))
            btn_frame.pack(fill='x')
            ttk.Button(btn_frame, text="Remove Selected", command=remove_word).pack(side='left')
            ttk.Button(btn_frame, text="Close", command=nd.destroy).pack(side='right')

        sel_frame = ttk.Frame(filters_frame)
        sel_frame.pack(fill='x', pady=(6, 0))

        def select_all_filters():
            for v in filter_vars.values():
                v.set(True)

        def deselect_all_filters():
            for v in filter_vars.values():
                v.set(False)

        ttk.Button(sel_frame, text="Select All", command=select_all_filters).pack(side='left', padx=(0, 4))
        ttk.Button(sel_frame, text="Deselect All", command=deselect_all_filters).pack(side='left')

        # ══════════════════════════════════════════════════════════════════════
        # Search & Replace section
        # ══════════════════════════════════════════════════════════════════════
        sr_frame = ttk.LabelFrame(win, text="Search && Replace (applied to all files)",
                                  padding=8)
        sr_frame.pack(fill='both', expand=True, padx=10, pady=5)

        # Input row
        sr_input = ttk.Frame(sr_frame)
        sr_input.pack(fill='x')

        ttk.Label(sr_input, text="Find:").pack(side='left')
        sr_find_var = tk.StringVar()
        sr_find_entry = ttk.Entry(sr_input, textvariable=sr_find_var, width=18)
        sr_find_entry.pack(side='left', padx=(2, 6))

        ttk.Label(sr_input, text="Replace:").pack(side='left')
        sr_repl_var = tk.StringVar()
        sr_repl_entry = ttk.Entry(sr_input, textvariable=sr_repl_var, width=18)
        sr_repl_entry.pack(side='left', padx=(2, 6))

        sr_case_var = tk.BooleanVar(value=False)

        def sr_add_pair():
            find = sr_find_var.get()
            if not find:
                return
            repl = sr_repl_var.get()
            pair = [find, repl, sr_case_var.get()]
            # Avoid exact duplicates
            if pair not in app.custom_replacements:
                app.custom_replacements.append(pair)
                case_str = " [Aa]" if not pair[2] else ""
                sr_listbox.insert('end', f'"{find}" → "{repl}"{case_str}')
                app.save_preferences()
            sr_find_var.set('')
            sr_repl_var.set('')

        ttk.Button(sr_input, text="Add", command=sr_add_pair).pack(side='left', padx=2)

        # Right-click copy/paste on find/replace entries
        for entry in (sr_find_entry, sr_repl_entry):
            _m = tk.Menu(entry, tearoff=0)
            _m.add_command(label="Cut", command=lambda e=entry: e.event_generate('<<Cut>>'))
            _m.add_command(label="Copy", command=lambda e=entry: e.event_generate('<<Copy>>'))
            _m.add_command(label="Paste", command=lambda e=entry: e.event_generate('<<Paste>>'))
            _m.add_separator()
            _m.add_command(label="Select All",
                command=lambda e=entry: (e.select_range(0, 'end'), e.icursor('end')))
            entry.bind('<Button-3>', lambda ev, m=_m: m.tk_popup(ev.x_root, ev.y_root))

        # Options row
        sr_opts = ttk.Frame(sr_frame)
        sr_opts.pack(fill='x', pady=(4, 4))
        ttk.Checkbutton(sr_opts, text="Case sensitive",
                        variable=sr_case_var).pack(side='left')

        # Replacement list
        sr_list_frame = ttk.Frame(sr_frame)
        sr_list_frame.pack(fill='both', expand=True)

        sr_scroll = ttk.Scrollbar(sr_list_frame, orient='vertical')
        sr_scroll.pack(side='right', fill='y')

        sr_listbox = tk.Listbox(sr_list_frame, height=4, font=('Courier', 9),
                                yscrollcommand=sr_scroll.set)
        sr_listbox.pack(fill='both', expand=True)
        sr_scroll.config(command=sr_listbox.yview)

        # Populate from saved replacements
        for pair in app.custom_replacements:
            find, repl = pair[0], pair[1]
            case_sensitive = pair[2] if len(pair) > 2 else False
            case_str = "" if case_sensitive else " [Aa]"
            sr_listbox.insert('end', f'"{find}" → "{repl}"{case_str}')

        def sr_remove_selected():
            sel = sorted(sr_listbox.curselection(), reverse=True)
            for idx in sel:
                sr_listbox.delete(idx)
                del app.custom_replacements[idx]
            if sel:
                app.save_preferences()

        def sr_clear_all():
            sr_listbox.delete(0, 'end')
            app.custom_replacements.clear()
            app.save_preferences()

        sr_btn_frame = ttk.Frame(sr_frame)
        sr_btn_frame.pack(fill='x', pady=(4, 0))
        ttk.Button(sr_btn_frame, text="Remove Selected",
                   command=sr_remove_selected).pack(side='left', padx=(0, 4))
        ttk.Button(sr_btn_frame, text="Clear All",
                   command=sr_clear_all).pack(side='left')
        ttk.Label(sr_btn_frame, text="Saved across sessions",
                  font=('Helvetica', 8), foreground='gray').pack(side='right')

        sr_find_entry.bind('<Return>', lambda e: sr_add_pair())

        # ══════════════════════════════════════════════════════════════════════
        # Output section
        # ══════════════════════════════════════════════════════════════════════
        output_frame = ttk.LabelFrame(win, text="Output", padding=8)
        output_frame.pack(fill='x', padx=10, pady=5)

        output_mode = tk.StringVar(value='overwrite')
        subfolder_name = tk.StringVar(value='filtered')

        ttk.Radiobutton(output_frame, text="Overwrite original files",
                        variable=output_mode, value='overwrite').pack(anchor='w')

        sub_row = ttk.Frame(output_frame)
        sub_row.pack(fill='x', anchor='w')
        ttk.Radiobutton(sub_row, text="Save to subfolder:",
                        variable=output_mode, value='subfolder').pack(side='left')
        ttk.Entry(sub_row, textvariable=subfolder_name, width=20).pack(side='left', padx=(4, 0))

        # ══════════════════════════════════════════════════════════════════════
        # Progress + Apply
        # ══════════════════════════════════════════════════════════════════════
        progress_frame = ttk.Frame(win, padding=(10, 6, 10, 6))
        progress_frame.pack(fill='x')

        progress_var = tk.DoubleVar(value=0)
        progress_bar = ttk.Progressbar(progress_frame, variable=progress_var,
                                        maximum=100)
        progress_bar.pack(fill='x', side='left', expand=True, padx=(0, 8))

        progress_label = ttk.Label(progress_frame, text="")
        progress_label.pack(side='right')

        action_frame = ttk.Frame(win, padding=(10, 4, 10, 10))
        action_frame.pack(fill='x')

        result_label = ttk.Label(action_frame, text="", foreground='green')
        result_label.pack(side='left')

        def do_batch_apply():
            """Apply selected filters to all loaded files and save."""
            if not file_paths:
                messagebox.showwarning("No Files",
                    "Add subtitle files before applying filters.", parent=win)
                return

            # Gather selected filters in order
            active_filters = [(key, label, func) for key, label, func in filter_defs
                              if filter_vars[key].get()]
            # If both Fix ALL CAPS and Remove HI are selected, ensure Fix ALL CAPS
            # runs first to avoid false HI detection on all-caps text
            active_keys = {k for k, _, _ in active_filters}
            if 'fix_caps' in active_keys and 'remove_hi' in active_keys:
                fix_entry = next(e for e in active_filters if e[0] == 'fix_caps')
                active_filters.remove(fix_entry)
                hi_idx = next(i for i, e in enumerate(active_filters) if e[0] == 'remove_hi')
                active_filters.insert(hi_idx, fix_entry)
            # Strip the key, keep only (label, func)
            active_filters = [(label, func) for _, label, func in active_filters]
            has_replacements = bool(app.custom_replacements)
            if not active_filters and not has_replacements:
                messagebox.showwarning("Nothing to Apply",
                    "Select at least one filter or add search & replace pairs.",
                    parent=win)
                return

            apply_btn.configure(state='disabled')
            total = len(file_paths)
            success = 0
            errors = 0

            for idx, fpath in enumerate(file_paths):
                progress_label.configure(text=f"{idx + 1}/{total}")
                progress_var.set((idx / total) * 100)
                win.update_idletasks()

                try:
                    # Read / convert to SRT
                    ext = Path(fpath).suffix.lower()
                    if ext in ('.srt',):
                        with open(fpath, 'r', encoding='utf-8', errors='replace') as f:
                            srt_text = f.read()
                    else:
                        tmp_srt = tempfile.NamedTemporaryFile(
                            suffix='.srt', delete=False, mode='w', encoding='utf-8')
                        tmp_srt.close()
                        cmd = ['ffmpeg', '-y', '-i', fpath, '-c:s', 'srt', tmp_srt.name]
                        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                        if result.returncode != 0:
                            app.add_log(f"Batch: failed to convert {os.path.basename(fpath)}: "
                                         f"{result.stderr[-200:]}", 'ERROR')
                            os.unlink(tmp_srt.name)
                            errors += 1
                            continue
                        with open(tmp_srt.name, 'r', encoding='utf-8',
                                  errors='replace') as f:
                            srt_text = f.read()
                        os.unlink(tmp_srt.name)

                    cues = parse_srt(srt_text)
                    if not cues:
                        app.add_log(f"Batch: no cues found in {os.path.basename(fpath)}", 'WARNING')
                        errors += 1
                        continue

                    # Apply each active filter in order
                    before = len(cues)
                    for f_label, f_func in active_filters:
                        cues = f_func(cues)

                    # Apply search & replace pairs
                    for pair in app.custom_replacements:
                        find_str, repl_str = pair[0], pair[1]
                        case_sensitive = pair[2] if len(pair) > 2 else False
                        flags = 0 if case_sensitive else re.IGNORECASE
                        pattern = re.escape(find_str)
                        for cue in cues:
                            cue['text'] = re.sub(pattern, repl_str, cue['text'],
                                                 flags=flags)
                    # Remove any cues that became empty after replacements
                    cues = [c for c in cues if c['text'].strip()]

                    if not cues:
                        app.add_log(f"Batch: all cues removed from {os.path.basename(fpath)}, "
                                     "skipping save", 'WARNING')
                        errors += 1
                        continue

                    # Determine output path
                    if output_mode.get() == 'subfolder':
                        sub_dir = os.path.join(os.path.dirname(fpath),
                                               subfolder_name.get() or 'filtered')
                        os.makedirs(sub_dir, exist_ok=True)
                        # Always save as .srt (since we converted to SRT)
                        out_name = Path(fpath).stem + '.srt'
                        out_path = os.path.join(sub_dir, out_name)
                    else:
                        # Overwrite — if original was .srt, overwrite it;
                        # otherwise save as .srt alongside original
                        if ext == '.srt':
                            out_path = fpath
                        else:
                            out_path = str(Path(fpath).with_suffix('.srt'))

                    with open(out_path, 'w', encoding='utf-8') as f:
                        f.write(write_srt(cues))

                    removed = before - len(cues)
                    app.add_log(f"Batch: {os.path.basename(fpath)} → "
                                 f"{len(cues)} entries ({removed} removed) → "
                                 f"{os.path.basename(out_path)}", 'SUCCESS')
                    success += 1

                    # Highlight processed file in the listbox
                    file_listbox.itemconfig(idx, fg='green')

                except Exception as e:
                    app.add_log(f"Batch: error processing {os.path.basename(fpath)}: {e}",
                                 'ERROR')
                    file_listbox.itemconfig(idx, fg='red')
                    errors += 1

            progress_var.set(100)
            progress_label.configure(text=f"{total}/{total}")
            apply_btn.configure(state='normal')

            filters_used = ", ".join(label for label, _ in active_filters)
            if app.custom_replacements:
                filters_used += f", {len(app.custom_replacements)} replacement(s)"
            result_label.configure(
                text=f"Done — {success} succeeded, {errors} failed",
                foreground='green' if errors == 0 else 'orange')
            app.add_log(f"Batch filter complete: {success}/{total} files processed. "
                         f"Filters: {filters_used}", 'SUCCESS')

        apply_btn = ttk.Button(action_frame, text="Apply Filters", command=do_batch_apply)
        apply_btn.pack(side='right', padx=(4, 0))
        def _close_window():
            win.destroy()
            if getattr(app, '_standalone_mode', False):
                app.root.destroy()

        ttk.Button(action_frame, text="Close", command=_close_window).pack(side='right')
        win.protocol('WM_DELETE_WINDOW', _close_window)

        if not getattr(app, '_standalone_mode', False):
            win.wait_window()



def main():
    """Launch Batch Filter as a standalone application."""
    from .standalone import create_standalone_root

    root, app = create_standalone_root(
        title="Batch Filter",
        geometry="700x600",
        minsize=(600, 450),
    )

    # Ensure required attributes exist on standalone context
    if not hasattr(app, "custom_ad_patterns"):
        app.custom_ad_patterns = []
    if not hasattr(app, "custom_cap_words"):
        app.custom_cap_words = []
    if not hasattr(app, "custom_replacements"):
        app.custom_replacements = []
    if not hasattr(app, "add_log"):
        app.add_log = lambda msg, level="INFO": print(f"[{level}] {msg}")

    app._standalone_mode = True
    root.withdraw()
    open_batch_filter(app)

    root.mainloop()


if __name__ == '__main__':
    main()
