"""Shared post-processing filter panel for subtitle-producing tools.

A "🔧 Filters (n)" button plus its dialog, the active-filter lookup, and the
apply-to-file / apply-to-cues logic — everything a tool needs to offer the
standard subtitle filter set after it has written a subtitle.

⚠️ **WHY THIS IS SHARED.** `sub_ripper` grew this inline first and it works well;
the Whisper Transcriber needs the same thing. Copying it would give two filter
lists that drift, which is the failure the two-prefs-store split already caused
once (a Fix-ALL-CAPS setting written in one store and read from the other, so it
silently did nothing). One list, one dialog, one apply path.

✅ **`sub_ripper` was migrated onto this on 2026-08-27.** This is now the ONLY
definition of the filter set — add a filter here and both tools get it.

⚠️ sub_ripper's five call sites were left UNCHANGED on purpose; it keeps two thin
wrappers with the old signatures so the migration could not alter behaviour in
its OCR, extraction or CC paths. The one deliberate difference: this panel RAISES
where sub_ripper's old copy swallowed the exception and returned None — which was
indistinguishable from "no filters selected", so a throwing filter left the file
untouched and nobody found out. The wrappers log and continue.
"""
import tkinter as tk
from tkinter import ttk
import re

from .utils import center_window_on_parent


# ⚠️ THE definition. Both the Sub Extractor and the Whisper Transcriber read
# this list — add a filter here and both get it. There is no second copy.
FILTER_DEFS = [
    ('remove_hi',      "Remove HI  [brackets] (parens) Speaker:"),
    ('remove_tags',    "Remove Tags  <i> {\\an8}"),
    ('remove_ads',     "Remove Ads / Credits"),
    ('remove_music',   "Remove Stray Notes  ♪ ♫"),
    ('fix_music',      "Fix Music Notes  ♪ (OCR)"),
    ('fix_ocr',        "Fix OCR Errors  '' | 0"),
    ('remove_dashes',  "Remove Leading Dashes  -"),
    ('remove_caps_hi', "Remove ALL CAPS HI (UK)"),
    ('remove_quotes',  "Remove Off-Screen Quotes (UK)"),
    ('remove_dupes',   "Remove Duplicates"),
    ('merge_dupes',    "Merge Duplicates"),
    ('merge_short',    "Merge Short Cues"),
    ('reduce_lines',   "Reduce to 2 Lines"),
    ('collapse_cc',    "Collapse Paint-On CC"),
    ('fix_caps',       "Fix ALL CAPS"),
]


def _func_map(app):
    """Map filter keys to callables, binding the app's custom word lists."""
    from .subtitle_filters import (
        filter_remove_hi, filter_remove_tags, filter_remove_ads,
        filter_remove_music_notes, filter_fix_music_notes,
        filter_fix_ocr, filter_remove_leading_dashes,
        filter_remove_caps_hi, filter_remove_offscreen_quotes,
        filter_remove_duplicates, filter_merge_duplicates,
        filter_merge_short, filter_reduce_lines,
        filter_collapse_paint_on, filter_fix_caps,
    )
    return {
        'remove_hi':      filter_remove_hi,
        'remove_tags':    filter_remove_tags,
        'remove_ads':     lambda c: filter_remove_ads(
            c, getattr(app, 'custom_ad_patterns', [])),
        'remove_music':   filter_remove_music_notes,
        'fix_music':      filter_fix_music_notes,
        'fix_ocr':        filter_fix_ocr,
        'remove_dashes':  filter_remove_leading_dashes,
        'remove_caps_hi': filter_remove_caps_hi,
        'remove_quotes':  filter_remove_offscreen_quotes,
        'remove_dupes':   filter_remove_duplicates,
        'merge_dupes':    filter_merge_duplicates,
        'merge_short':    filter_merge_short,
        'reduce_lines':   filter_reduce_lines,
        'collapse_cc':    filter_collapse_paint_on,
        'fix_caps':       lambda c: filter_fix_caps(
            c, getattr(app, 'custom_cap_words', []),
            use_names_db=getattr(app, 'use_names_db', False)),
    }


class SubtitleFilterPanel:
    """A filter button + dialog, and the logic to apply the selection.

    Build it with the frame the button should live in; it packs itself.
    """

    def __init__(self, parent_win, app, container, saved=None,
                 title="Post-Processing Filters", blurb=None, on_change=None,
                 side='left'):
        """*side* is where the button packs in *container*.

        'right' anchors it to the far edge of a row of left-packed settings,
        which reads better for an action sitting among spinboxes and keeps it
        clear of them as the row fills up.
        """
        self.win = parent_win
        self.app = app
        self._on_change = on_change
        self.title = title
        self.blurb = blurb or ("Selected filters are applied to each subtitle\n"
                               "file after it is written.")
        saved = saved or {}
        self.vars = {key: tk.BooleanVar(value=saved.get(key, False))
                     for key, _ in FILTER_DEFS}
        self.apply_sr = tk.BooleanVar(value=saved.get('search_replace', False))

        self.button = ttk.Button(container, text="🔧 Filters...",
                                 command=self.open_dialog)
        self.button.pack(side=side, padx=(8, 4))
        self._refresh_button()

    # ── state ──────────────────────────────────────────────────────────────

    def active_count(self):
        n = sum(1 for v in self.vars.values() if v.get())
        return n + (1 if self.apply_sr.get() else 0)

    def get_prefs(self):
        """Serialisable selection, for saving alongside the tool's own prefs."""
        d = {key: var.get() for key, var in self.vars.items()}
        d['search_replace'] = self.apply_sr.get()
        return d

    def _refresh_button(self):
        n = self.active_count()
        self.button.configure(text=f"🔧 Filters ({n})" if n else "🔧 Filters...")
        if self._on_change:
            self._on_change()

    # ── dialog ─────────────────────────────────────────────────────────────

    def open_dialog(self):
        dlg = tk.Toplevel(self.win)
        dlg.title(self.title)
        dlg.transient(self.win)
        dlg.grab_set()
        dlg.resizable(False, False)

        frm = ttk.Frame(dlg, padding=12)
        frm.pack(fill='both', expand=True)
        ttk.Label(frm, text=self.blurb, wraplength=340).pack(pady=(0, 8))

        chk = ttk.Frame(frm)
        chk.pack(fill='x')
        for i, (key, label) in enumerate(FILTER_DEFS):
            ttk.Checkbutton(chk, text=label, variable=self.vars[key]).grid(
                row=i, column=0, sticky='w', pady=1)

        sr_pairs = getattr(self.app, 'custom_replacements', [])
        sr_label = (f"Search && Replace ({len(sr_pairs)} pairs)" if sr_pairs
                    else "Search && Replace (none configured)")
        row = len(FILTER_DEFS)
        ttk.Separator(chk, orient='horizontal').grid(
            row=row, column=0, sticky='ew', pady=4)
        sr_chk = ttk.Checkbutton(chk, text=sr_label, variable=self.apply_sr)
        sr_chk.grid(row=row + 1, column=0, sticky='w', pady=1)
        if not sr_pairs:
            sr_chk.configure(state='disabled')
            self.apply_sr.set(False)

        btns = ttk.Frame(frm)
        btns.pack(fill='x', pady=(10, 0))
        ttk.Button(btns, text="Select All",
                   command=lambda: [v.set(True) for v in self.vars.values()]
                   ).pack(side='left', padx=(0, 4))
        ttk.Button(btns, text="Select None",
                   command=lambda: [v.set(False) for v in self.vars.values()]
                   ).pack(side='left')
        ttk.Button(btns, text="Close", command=dlg.destroy).pack(side='right')

        dlg.protocol('WM_DELETE_WINDOW', dlg.destroy)
        center_window_on_parent(dlg, self.win)
        dlg.bind('<Destroy>',
                 lambda e: self._refresh_button() if e.widget == dlg else None)

    # ── applying ───────────────────────────────────────────────────────────

    def active_filters(self):
        """[(label, func), ...] for the ticked filters, in dialog order."""
        fmap = _func_map(self.app)
        return [(label, fmap[key]) for key, label in FILTER_DEFS
                if self.vars[key].get()]

    def _search_replace(self, cues):
        pairs = getattr(self.app, 'custom_replacements', [])
        if not self.apply_sr.get() or not pairs:
            return cues
        for pair in pairs:
            find_str, repl = pair[0], pair[1]
            cs = pair[2] if len(pair) > 2 else False
            flags = 0 if cs else re.IGNORECASE
            pat = re.escape(find_str)
            for cue in cues:
                cue['text'] = re.sub(pat, repl, cue['text'], flags=flags)
        return [c for c in cues if c['text'].strip()]

    def apply_to_cues(self, cues):
        active = self.active_filters()
        if not active and not self.apply_sr.get():
            return cues
        # Two passes when several filters are on — one filter's output can
        # expose work for another (e.g. removing tags reveals an HI bracket).
        for _ in range(2 if len(active) > 1 else 1):
            for _label, func in active:
                cues = func(cues)
        return self._search_replace(cues)

    def apply_to_file(self, path):
        """Filter an SRT in place.

        Returns ``(before, after)`` cue counts, ``None`` if nothing was
        selected, or raises. ⚠️ Deliberately does NOT swallow exceptions the way
        sub_ripper's copy does — a filter that throws there leaves the file
        untouched and returns None, which is indistinguishable from "no filters
        selected". The caller must be able to tell those apart and say so.
        """
        from .subtitle_filters import parse_srt, write_srt
        if not self.active_filters() and not self.apply_sr.get():
            return None
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            cues = parse_srt(f.read())
        if not cues:
            return None
        before = len(cues)
        cues = self.apply_to_cues(cues)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(write_srt(cues))
        return (before, len(cues))
