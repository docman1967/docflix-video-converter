"""
Docflix Video Converter — Subtitle Spell Checker

Interactive spell check dialog for subtitle cues. Uses pyspellchecker
for detection, with custom dictionary and character name support.

Usage from a subtitle editor:
    run_spell_check(app, editor_window, cues, tree, refresh_tree,
                    push_undo, spell_error_indices)
"""

import re
import subprocess
import sys
import tkinter as tk
from tkinter import ttk, messagebox


def run_spell_check_scan(app, parent_window, cues, spell_error_indices):
    """Scan all cues for spelling errors.

    Args:
        app: Application context with custom_cap_words, custom_spell_words.
        parent_window: Tk window for dialogs.
        cues: List of subtitle cue dicts.
        spell_error_indices: Set to populate with cue indices that have
                             errors.

    Returns:
        Dict of {cue_index: [(word, [candidates]), ...]} or None if
        spell checker is not available.
    """
    try:
        from spellchecker import SpellChecker
    except ImportError:
        if messagebox.askyesno(
                "Missing Package",
                "pyspellchecker is not installed.\n\n"
                "Would you like to install it now?",
                parent=parent_window):
            try:
                if hasattr(app, 'add_log'):
                    app.add_log("Installing pyspellchecker...", 'INFO')
                _pip_result = subprocess.run(
                    [sys.executable, '-m', 'pip', 'install',
                     '--user', '--break-system-packages',
                     'pyspellchecker'],
                    capture_output=True, text=True, timeout=60)
                if _pip_result.returncode == 0:
                    from spellchecker import SpellChecker
                    if hasattr(app, 'add_log'):
                        app.add_log(
                            "pyspellchecker installed successfully",
                            'SUCCESS')
                else:
                    messagebox.showerror(
                        "Install Failed",
                        f"pip install failed:\n"
                        f"{_pip_result.stderr[-300:]}",
                        parent=parent_window)
                    return None
            except Exception as _e:
                messagebox.showerror(
                    "Install Failed",
                    f"Could not install pyspellchecker:\n{_e}",
                    parent=parent_window)
                return None
        else:
            return None

    spell = SpellChecker()
    cap_words = getattr(app, 'custom_cap_words', [])
    spell_words = getattr(app, 'custom_spell_words', [])
    known = [w.lower() for w in cap_words + spell_words]
    if known:
        spell.word_frequency.load_words(known)

    spell_error_indices.clear()
    errors_by_cue = {}
    for i, cue in enumerate(cues):
        clean = re.sub(r'<[^>]+>|\{\\[^}]+\}|♪', '', cue['text'])
        words = re.findall(r"[a-zA-Z]+(?:'[a-zA-Z]+)?", clean)
        if not words:
            continue
        unknown = spell.unknown(words)
        if unknown:
            spell_error_indices.add(i)
            errors_by_cue[i] = []
            for w in words:
                if w.lower() in unknown or w in unknown:
                    cands = spell.candidates(w)
                    errors_by_cue[i].append(
                        (w, sorted(cands) if cands else []))
    return errors_by_cue


def show_spell_check_dialog(app, editor_window, cues, tree,
                            refresh_tree_func, push_undo_func,
                            spell_error_indices):
    """Run spell check and show interactive correction dialog.

    Args:
        app: Application context with custom_cap_words,
             custom_spell_words, save_preferences, _center_on_main.
        editor_window: The parent editor Toplevel/Tk window.
        cues: List of subtitle cue dicts (modified in place).
        tree: The treeview widget displaying cues.
        refresh_tree_func: Callable to refresh the treeview.
        push_undo_func: Callable to push undo state.
        spell_error_indices: Set of cue indices with errors.
    """
    errors_by_cue = run_spell_check_scan(
        app, editor_window, cues, spell_error_indices)
    if errors_by_cue is None:
        return
    if not errors_by_cue:
        spell_error_indices.clear()
        refresh_tree_func(cues)
        messagebox.showinfo(
            "Spell Check", "No spelling errors found!",
            parent=editor_window)
        return
    refresh_tree_func(cues)

    error_list = []
    for ci in sorted(errors_by_cue.keys()):
        for word, cands in errors_by_cue[ci]:
            error_list.append((ci, word, cands))

    current = [0]
    ignored = set()

    sd = tk.Toplevel(editor_window)
    sd.title("Spell Check")
    sd.geometry("500x440")
    sd.resizable(True, True)
    app._center_on_main(sd)
    sd.attributes('-topmost', True)

    sf = ttk.Frame(sd, padding=12)
    sf.pack(fill='both', expand=True)
    sf.columnconfigure(1, weight=1)
    _sp = {'padx': 6, 'pady': 4}

    stats_lbl = ttk.Label(
        sf,
        text=f"Found {len(error_list)} errors in "
             f"{len(errors_by_cue)} cues",
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
    bf.grid(row=5, column=0, columnspan=2, sticky='ew', pady=(8, 0))

    def _show_err(idx):
        while idx < len(error_list):
            ci, w, ca = error_list[idx]
            if w.lower() not in ignored:
                break
            idx += 1
        else:
            spell_error_indices.clear()
            refresh_tree_func(cues)
            messagebox.showinfo(
                "Spell Check", "Spell check complete!",
                parent=sd)
            sd.destroy()
            return
        current[0] = idx
        ci, w, ca = error_list[idx]
        items = tree.get_children()
        if ci < len(items):
            ahead = min(ci + 5, len(items) - 1)
            tree.see(items[ahead])
            tree.selection_set(items[ci])
            tree.after(50, lambda: tree.see(items[ci]))
        word_var.set(w)
        ctx_var.set(cues[ci]['text'].replace('\n', ' / '))
        stats_lbl.configure(
            text=f"Error {idx + 1} of {len(error_list)} "
                 f"(cue #{ci + 1})")
        sug_lb.delete(0, 'end')
        for c in ca:
            sug_lb.insert('end', c)
        if ca:
            sug_lb.selection_set(0)
            replace_var.set(ca[0])
        else:
            replace_var.set(w)

    def _do_replace():
        ci, w, _ = error_list[current[0]]
        repl = replace_var.get().strip()
        if not repl:
            return
        push_undo_func()
        txt = cues[ci]['text']
        pos = txt.find(w)
        if pos == -1:
            pos = txt.lower().find(w.lower())
        if pos >= 0:
            cues[ci]['text'] = txt[:pos] + repl + txt[pos + len(w):]
        refresh_tree_func(cues)
        _show_err(current[0] + 1)

    def _do_replace_all():
        _, w, _ = error_list[current[0]]
        repl = replace_var.get().strip()
        if not repl:
            return
        push_undo_func()
        for cue in cues:
            if w in cue['text']:
                cue['text'] = cue['text'].replace(w, repl)
            elif w.lower() in cue['text'].lower():
                cue['text'] = re.sub(
                    re.escape(w), repl, cue['text'],
                    flags=re.IGNORECASE)
        refresh_tree_func(cues)
        _show_err(current[0] + 1)

    def _do_skip():
        _show_err(current[0] + 1)

    def _do_ignore():
        _, w, _ = error_list[current[0]]
        ignored.add(w.lower())
        _show_err(current[0] + 1)

    def _do_add_dict():
        _, w, _ = error_list[current[0]]
        spell_words = getattr(app, 'custom_spell_words', [])
        if w.lower() not in [x.lower() for x in spell_words]:
            spell_words.append(w)
            app.custom_spell_words = spell_words
            app.save_preferences()
        ignored.add(w.lower())
        _show_err(current[0] + 1)

    def _do_add_name():
        _, w, _ = error_list[current[0]]
        cap_words = getattr(app, 'custom_cap_words', [])
        spell_words = getattr(app, 'custom_spell_words', [])
        if w not in cap_words:
            cap_words.append(w)
            app.custom_cap_words = cap_words
        if w.lower() not in [x.lower() for x in spell_words]:
            spell_words.append(w)
            app.custom_spell_words = spell_words
        app.save_preferences()
        ignored.add(w.lower())
        _show_err(current[0] + 1)

    bf1 = ttk.Frame(bf)
    bf1.pack(fill='x')
    ttk.Button(bf1, text="Replace", command=_do_replace,
               width=10).pack(side='left', padx=2)
    ttk.Button(bf1, text="Replace All", command=_do_replace_all,
               width=10).pack(side='left', padx=2)
    ttk.Button(bf1, text="Skip", command=_do_skip,
               width=6).pack(side='left', padx=2)
    ttk.Button(bf1, text="Ignore", command=_do_ignore,
               width=8).pack(side='left', padx=2)

    bf2 = ttk.Frame(bf)
    bf2.pack(fill='x', pady=(4, 0))
    ttk.Button(bf2, text="Add to Dict", command=_do_add_dict,
               width=10).pack(side='left', padx=2)
    ttk.Button(bf2, text="Add as Name", command=_do_add_name,
               width=10).pack(side='left', padx=2)
    ttk.Button(bf2, text="Close", command=sd.destroy,
               width=6).pack(side='right', padx=2)

    _show_err(0)
