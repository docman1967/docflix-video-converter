"""
Docflix Media Suite — Subtitle Editor

Full-featured subtitle editor with inline text editing,
filters, timing tools, search/replace, spell check,
Smart Sync, and OCR support.

The editor is `open_standalone_subtitle_editor(app, ...)`: an independent
window opened from the Tools menu, a standalone launch, or — via
`auto_video`/`auto_stream`/`auto_external` — directly on a queue file's
subtitle stream or an external .srt. Formerly there were two editors that
had drifted apart; the stream editor was folded into this one (2026-07-23).
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
    scaled_geometry, scaled_minsize, ask_open_file, ask_save_file,
    center_window_on_parent,
)
from .subtitle_filters import (
    parse_srt, write_srt, srt_ts_to_ms, ms_to_srt_ts,
    cue_char_len, cue_start_ms, cue_duration_ms, cue_cps,
    filter_remove_hi, filter_remove_caps_hi,
    filter_remove_music_notes, filter_fix_caps,
    filter_remove_tags, filter_remove_ads,
    filter_fix_ocr,
    filter_remove_offscreen_quotes,
    filter_remove_leading_dashes,
    filter_remove_duplicates, filter_merge_duplicates, filter_merge_short,
    filter_reduce_lines, filter_collapse_paint_on,
    shift_timestamps, stretch_timestamps, two_point_sync,
    BUILTIN_AD_PATTERNS,
    # Names database (optional)
    load_names_db, unload_names_db, is_names_db_loaded,
    is_names_db_available, get_names_db_count,
    NAMES_DB_DIR, NAMES_DB_URLS,
)
from .smart_sync import smart_sync
from .waveform_timeline import WaveformTimeline
from .gpu import (detect_closed_captions, detect_cc_types,
                   extract_closed_captions_to_srt)
from .subtitle_ocr import ocr_bitmap_subtitle

try:
    from tkinterdnd2 import DND_FILES
    HAS_DND = True
except ImportError:
    HAS_DND = False


def install_cue_sort(tree, cues_getter):
    """Wire VIEW-ONLY column sorting onto a subtitle-editor cue tree.

    The tree's row iids ARE the cue indices (iid == str(index)), so sorting only
    reorders the *displayed* rows via tree.move() — the underlying cues list is
    never touched and stays in timeline order. Clickable headers:
        #          → reset to timeline order
        Timestamp  → sort by duration  (toggles asc/desc)
        Text       → sort by length    (toggles asc/desc; clusters junk cues at top)
    Deletes/edits still map iid→cue correctly and saving re-numbers, so the whole
    workflow is: sort → select the junk → delete → done. No re-sort, no restamp.
    """
    state = {"key": "timeline", "reverse": False}
    keyfns = {"length": cue_char_len, "duration": cue_duration_ms}

    def apply(key, toggle=True):
        cues = cues_getter() or []
        if key == "timeline":
            state["key"], state["reverse"] = "timeline", False
            order = list(range(len(cues)))
        else:
            if toggle:
                state["reverse"] = (not state["reverse"]) if state["key"] == key else False
                state["key"] = key
            order = sorted(range(len(cues)),
                           key=lambda i: keyfns[key](cues[i]),
                           reverse=state["reverse"])
        for pos, idx in enumerate(order):
            try:
                tree.move(str(idx), "", pos)
            except Exception:
                pass
        arrow = " ▾" if state["reverse"] else " ▴"
        tree.heading("num",  text="#" + (arrow if state["key"] == "timeline" else ""))
        tree.heading("time", text="Timestamp" + (arrow if state["key"] == "duration" else ""))
        tree.heading("text", text="Text" + (arrow if state["key"] == "length" else ""))

    tree.heading("num",  command=lambda: apply("timeline"))
    tree.heading("time", command=lambda: apply("duration"))
    tree.heading("text", command=lambda: apply("length"))


# Words to exclude — common short caps, pronouns, roman numerals
_CAPS_EXCLUDE = {'OK', 'I', 'A', 'TV', 'AM', 'PM', 'II', 'III',
                 'IV', 'VI', 'VII', 'VIII', 'IX', 'XI', 'XII',
                 'DJ', 'MC', 'ID'}
_CAPS_TAG_RE = re.compile(r'<[^>]+>')
_CAPS_RE = re.compile(r'\b([A-Z]{2,})\b')          # 2+ uppercase letters, no digits


def _name_case(s):
    """Normalise a selected name to the case a name is actually written in.

    ⚠️ WITHOUT THIS, THE FEATURE IS USELESS FOR ITS MAIN USE CASE. filter_fix_caps
    reproduces a custom name EXACTLY as stored, and the whole point of adding one
    is that you are looking at an ALL CAPS subtitle — so the obvious gesture,
    selecting "GRACE" out of "GRACE, COME HERE!", stores "GRACE" and the line
    stays shouting. Measured 2026-08-07:

        stored "Grace" -> "Grace, come here!"     what you wanted
        stored "GRACE" -> "GRACE, come here!"     what you would have got
        stored "grace" -> "grace, come here!"

    Only normalises when the selection is UNIFORMLY cased. Mixed case is assumed
    deliberate, so "McKay", "DeAngelo" and "van Helsing" are left exactly alone —
    guessing at those does more harm than leaving them.
    """
    if not s or not (s.isupper() or s.islower()):
        return s
    out = []
    for w in s.split(' '):
        if not w:
            continue
        w = w[:1].upper() + w[1:].lower()
        # Irish O' — common enough in subtitles to be worth the one rule.
        w = re.sub(r"^(O')(\w)", lambda m: m.group(1) + m.group(2).upper(), w)
        out.append(w)
    return ' '.join(out)


def paste_over_selection(event):
    """Make paste REPLACE the selection in a tk.Text, the way every other
    editor does.

    ⚠️ TK'S Text WIDGET DOES NOT DO THIS BY DEFAULT — it inserts at the cursor
    and leaves the selected text alone. Measured 2026-08-07: select "hello" in
    "hello world", paste "PASTED", and Tk gives you

        'helloPASTED world'      <- tk.Text   (wrong, and surprising)
        'PASTED world'           <- ttk.Entry (correct)

    So only the multi-line cue editor is affected; every Entry in the app is
    already fine. Tony hit it typing corrections into a cue.

    Bound to <<Paste>>, which is the virtual event BOTH Ctrl+V and the
    right-click menu route through — binding the keystroke instead would miss
    the menu, and binding both risks pasting twice.

    Returns "break" so the class binding does not then insert a second copy.
    """
    w = event.widget
    try:
        if w.tag_ranges('sel'):
            w.delete('sel.first', 'sel.last')
    except Exception:
        pass
    try:
        w.insert('insert', w.clipboard_get())
    except Exception:
        pass          # empty or non-text clipboard — leave the widget alone
    return "break"


def _match_case(src, repl):
    """Give `repl` the capitalisation of the text it is replacing."""
    if src.isupper() and len(src) > 1:
        return repl.upper()
    if src[:1].isupper():
        return repl[:1].upper() + repl[1:]
    return repl


def replace_word(text, word, repl, count=0, exact=False):
    """Replace WHOLE-WORD occurrences of `word`. count=0 means all.

    ⚠️ THE SPELL CHECKER USED TO DO THIS WITH str.find() AND str.replace(),
    i.e. plain substring matching, and it corrupted text. The scanner only ever
    reports whole words, so the trap is a real word that also sits INSIDE a
    longer word earlier in the same line — which is precisely what names do:

        "Anastasia met Ana."  fixing Ana->Anna   gave  "Annastasia met Ana."
        "Vanya, this is Van." fixing Van->Vance  gave  "Vanceya, this is Van."

    Note both failures at once: it mangled a word that was never flagged AND
    left the actual error untouched. Replace All was worse — str.replace()
    across every cue in the file, so one click could chew through the lot.

    Apostrophes count as word characters, or "dont" would half-match inside
    "don't". Matching is case-insensitive but the replacement inherits the case
    of what it replaced, so "Teh"->"The" and "WONT"->"WON'T" come out right
    instead of flattening a line's capitalisation.

    ⚠️ exact=True IS REQUIRED FOR RECASING and only for that. Fixing "hirst" ->
    "Hirst" the ordinary way is a no-op with a hole in it: the pattern also
    matches the "Hirst" that is ALREADY correct, and _match_case then rewrites
    it to itself. In a cue reading "Hirst said hirst" the count=1 replace is
    spent on the correct one and the broken one survives — while the scanner
    moves on, so it is never offered again. exact=True matches case-sensitively
    and inserts `repl` verbatim, so only the wrong spellings are touched.
    """
    pat = re.compile(r"(?<![A-Za-z'])" + re.escape(word) + r"(?![A-Za-z'])",
                     0 if exact else re.I)
    if exact:
        return pat.sub(lambda m: repl, text, count=count)
    return pat.sub(lambda m: _match_case(m.group(0), repl), text, count=count)


def scan_allcaps_words(cues):
    """Pure scan: return (indices, details) for cues containing ALL CAPS words.

    Skips only the common-short-word list (_CAPS_EXCLUDE).

    ⚠️ THE PERIOD-ADJACENCY CHECK WAS REMOVED 2026-08-06, and it matters.
    The old code skipped any match with a '.' immediately before or after it, to
    avoid flagging acronyms like U.S. / U.K. Measured: the regex is
    ``\\b([A-Z]{2,})\\b``, and a dotted acronym is single letters separated by
    periods — so U.S., U.K., F.B.I. and U.S.A. produce NO MATCH in the first
    place. The check therefore protected against nothing, while silently dropping
    every caps word at the END OF A SENTENCE:

        "Go to the HOSPITAL now."   ->  flagged
        "He went to the HOSPITAL."  ->  MISSED

    On UK-style SDH, where most lines end in a period, that was a large blind spot
    in the exact tool being used to catch what the filter leaves behind. Cost of
    removing it: a real acronym written without periods and followed by a period
    ("Call the USA.") now gets flagged. That's one glance to dismiss, against
    missing genuine words — recall over precision, on a review tool.

    Deliberately has NO side effects and touches no widget — the caller decides
    what to do with the result. It used to paint the Treeview directly and keep
    its findings in a local, which is why the highlighting vanished the moment
    anything rebuilt the tree. See refresh_tree(). (2026-08-06)
    """
    indices = set()
    details = {}
    for i, cue in enumerate(cues):
        text = _CAPS_TAG_RE.sub('', cue['text'])
        for m in _CAPS_RE.finditer(text):
            word = m.group(1)
            if word in _CAPS_EXCLUDE:
                continue
            indices.add(i)
            details.setdefault(i, set()).add(word)
    return indices, details


def _vtt_to_srt(vtt_text):
    """Native WebVTT → SRT text. Robust where ffmpeg's demuxer silently emits an EMPTY
    file: HLS/broadcast .vtt with an `X-TIMESTAMP-MAP` header, BOM, CRLF, missing cue IDs,
    or cue-setting suffixes on the timestamp line. Keeps <i>/<b>/<u> (SRT-valid), strips
    other WebVTT tags (<c...>, <00:00:00.000>, etc.)."""
    import re as _re
    text = (vtt_text or "").replace("\r\n", "\n").replace("\r", "\n")
    ts = _re.compile(r"(?:(\d+):)?(\d{2}):(\d{2})[.,](\d{3})\s*-->\s*"
                     r"(?:(\d+):)?(\d{2}):(\d{2})[.,](\d{3})")

    def _fmt(h, m, s, ms):
        return f"{int(h or 0):02d}:{int(m):02d}:{int(s):02d},{int(ms):03d}"

    lines = text.split("\n")
    cues, i = [], 0
    while i < len(lines):
        m = ts.search(lines[i])
        if not m:
            i += 1
            continue
        start = _fmt(m.group(1), m.group(2), m.group(3), m.group(4))
        end = _fmt(m.group(5), m.group(6), m.group(7), m.group(8))
        i += 1
        body = []
        while i < len(lines) and lines[i].strip() != "":
            ln = _re.sub(r"</?c[^>]*>|<\d{2}:\d{2}:\d{2}[.,]\d{3}>|</?v[^>]*>", "", lines[i])
            ln = _re.sub(r"<(?!/?[biu]\b)[^>]*>", "", ln)  # drop non-i/b/u tags
            body.append(ln.rstrip())
            i += 1
        if body:
            cues.append((start, end, "\n".join(body).strip("\n")))
    return "\n".join(f"{n}\n{s} --> {e}\n{txt}\n" for n, (s, e, txt) in enumerate(cues, 1))


def open_standalone_subtitle_editor(app, auto_video=None, auto_stream=None, auto_external=None):
        import tempfile

        editor = tk.Toplevel(app.root)
        editor.withdraw()
        editor.title("Docflix Subtitle Editor")
        geom_str = scaled_geometry(editor, 950, 650)
        editor.geometry(geom_str)
        editor.minsize(*scaled_minsize(editor, 700, 500))
        editor.resizable(True, True)
        editor.update_idletasks()
        try:
            import re as _re
            gm = _re.match(r'(\d+)x(\d+)', geom_str)
            dw = int(gm.group(1)) if gm else editor.winfo_reqwidth()
            dh = int(gm.group(2)) if gm else editor.winfo_reqheight()
            pw = app.root.winfo_width()
            ph = app.root.winfo_height()
            px = app.root.winfo_x()
            py = app.root.winfo_y()
            x = px + (pw - dw) // 2
            y = py + (ph - dh) // 2
            editor.geometry(f'{dw}x{dh}+{max(0, x)}+{max(0, y)}')
        except Exception:
            pass
        editor.deiconify()

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
        TAG_CAPS = 'has_allcaps'

        # ── Spell check state ──
        spell_error_indices = set()
        # Has a spelling scan been run for the current file? Drives the status-bar
        # count that replaced the old results popup, and distinguishes "scanned,
        # found nothing" from "never scanned" — without it, a clean file and an
        # unscanned file look identical.
        spell_scanned = [False]

        # ── Per-file character names ─────────────────────────────────────────
        # Tony, 2026-08-07: *"a 'add temp name' that would be cleared once the
        # file is saved. So the user gets a sub that has the name grace in it as
        # the main character, the user could add that name to the temp list and
        # not have to fix every instance of it."*
        #
        # ⚠️ THIS EXISTS BECAUSE SOME NAMES ARE ALSO COMMON WORDS, and no single
        # permanent list can be right about them. Measured on FIX ALL CAPS:
        #     without "Mark":  "I TOLD MARK"      -> "I told mark"        wrong
        #     with    "Mark":  "LEAVE YOUR MARK"  -> "Leave your Mark"    wrong
        # There is no setting that gets both. Grace, Bill, Art, Rose, Will,
        # Frank, Hope, Faith — adding any of them permanently trades one error
        # for another, and the common noun usually outnumbers the character.
        #
        # The ambiguity is scoped to the FILE — Grace is a character in this
        # episode and a noun in the next — so the list is scoped to the file too.
        #
        # ⚠️ CLEARED ON LOAD, NOT ON SAVE. Tony's first instinct was save, but
        # saving mid-session and carrying on is normal; wiping the list there
        # means the next Fix ALL CAPS silently lowercases the character again.
        # Load is the boundary that actually matches "different file, different
        # meaning", and it is where every other per-file flag already resets.
        temp_cap_words = []

        def _rebuild_stats():
            """Repaint the status bar. ONE definition, called from every path.

            ⚠️ There used to be two hand-copied versions of this — one in
            refresh_tree() and one in save_edit() — and they drifted. The caps
            count added 2026-08-06 went into refresh_tree only, so editing any
            cue inline silently wiped "N ALL CAPS" from the status bar. The
            highlighting stayed on; only the number vanished, which reads as
            "the mode turned itself off". Today's spell count would have
            inherited exactly the same bug. Duplicated display logic diverges;
            it is only a question of when.
            """
            deleted_count.set(len(original_cues) - len(cues))
            mod = sum(1 for i, c in enumerate(cues)
                      if i < len(original_cues)
                      and c['text'] != original_cues[i]['text'])
            modified_count.set(mod)
            long_count = sum(1 for c in cues
                             if any(len(l) > MAX_CHARS_PER_LINE
                                    for l in c['text'].split('\n')))
            parts = [
                f"{len(cues)} entries",
                f"{modified_count.get()} modified",
                f"{deleted_count.get()} removed",
            ]
            if long_count:
                parts.append(f"{long_count} long lines")
            if caps_highlight_on[0]:
                parts.append(f"{len(scan_allcaps_words(cues)[0])} ALL CAPS")
            # Shown only once a scan has run, so "0 misspelled" means
            # checked-and-clean rather than never-checked.
            if spell_scanned[0]:
                parts.append(f"{len(spell_error_indices)} misspelled")
            stats_label.configure(text=" │ ".join(parts))

        # ALL CAPS highlighting is a MODE, not a one-shot paint job.
        # Storing a set of matching row indices would go stale the instant a cue
        # is deleted — every index after it shifts by one. So keep only a flag and
        # let refresh_tree() re-scan; the scan is a regex pass over the cue list
        # and costs nothing next to rebuilding the Treeview itself.
        caps_highlight_on = [False]

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
            sub_exts = {'.srt', '.ass', '.ssa', '.vtt', '.sub', '.idx'}
            ext = Path(sub_path).suffix.lower()
            if ext in ('.idx', '.sub'):
                # .sub can be text (MicroDVD) or bitmap (VobSub).
                # .idx is always VobSub. Probe to detect bitmap codec.
                is_vobsub = (ext == '.idx')
                if ext == '.sub':
                    try:
                        probe = subprocess.run(
                            ['ffprobe', '-v', 'quiet', '-print_format', 'json',
                             '-show_streams', sub_path],
                            capture_output=True, text=True, timeout=15)
                        import json as _json
                        streams = _json.loads(probe.stdout).get('streams', [])
                        is_vobsub = any(s.get('codec_name') == 'dvd_subtitle'
                                        for s in streams)
                    except Exception:
                        pass
                if is_vobsub:
                    # Find the IDX file (needed for timing; ffmpeg reads
                    # the pair from whichever one you point it at)
                    stem = sub_path[:-4]
                    idx_path = stem + '.idx'
                    if not os.path.exists(idx_path):
                        idx_path = stem + '.IDX'
                    sub_pair = stem + '.sub'
                    if not os.path.exists(sub_pair):
                        sub_pair = stem + '.SUB'
                    if not os.path.exists(idx_path) or not os.path.exists(sub_pair):
                        missing = '.idx' if not os.path.exists(idx_path) else '.sub'
                        messagebox.showerror("Missing File",
                            f"Cannot find matching {missing} file for:\n"
                            f"{os.path.basename(sub_path)}\n\n"
                            f"IDX/SUB files must be in the same folder "
                            f"with the same name.",
                            parent=editor)
                        return
                    load_video_subtitle(idx_path)
                    return
                # Not VobSub — fall through to text subtitle handling
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
            elif ext == '.vtt':
                # Native WebVTT parse. ffmpeg's webvtt demuxer silently emits an EMPTY
                # SRT for HLS/broadcast .vtt carrying an `X-TIMESTAMP-MAP` header (common
                # in captured captions) — so parse it ourselves. utf-8-sig drops the BOM.
                try:
                    with open(sub_path, 'r', encoding='utf-8-sig', errors='replace') as f:
                        srt_text = _vtt_to_srt(f.read())
                except Exception as e:
                    messagebox.showerror("Error", f"Failed to read .vtt:\n{e}",
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

            title = f"Docflix Subtitle Editor — {os.path.basename(sub_path)}"
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
            # Highlight state belongs to the file that was open, not to the
            # window. Without this you load a fresh subtitle and it arrives
            # pre-highlighted from the last one — the marks would be real (the
            # scan re-runs) but you never asked for them, and for spelling they'd
            # be plain wrong: stale row indices pointing into a different file.
            # (Tony, 2026-08-06.)
            caps_highlight_on[0] = False
            spell_error_indices.clear()
            # Clear the SCANNED flag too, not just the results. Leaving it set
            # would show "0 misspelled" on a brand-new file that was never
            # checked — a clean bill of health nobody asked for and nobody
            # earned. Same class of lie as a stale highlight.
            spell_scanned[0] = False
            # Per-file character names die with the file they belonged to.
            # "Grace" is a character here and a noun in the next episode.
            temp_cap_words.clear()
            current_path[0] = source_path
            editor.title(title)
            # If a waveform is already up, it belongs to the PREVIOUS file's
            # video. Follow the new subtitle to its own. Deferred via after()
            # because this runs while the window is still being built on first
            # open, and the timeline does not exist yet at that point.
            editor.after(0, _follow_waveform_to_subtitle)

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

            # ── Scanning dialog — runs probes in background thread ──
            scan_dlg = tk.Toplevel(editor)
            scan_dlg.title("Scanning")
            scan_dlg.resizable(False, False)
            scan_dlg.transient(editor)
            scan_dlg.overrideredirect(False)

            scan_f = ttk.Frame(scan_dlg, padding=20)
            scan_f.pack(fill='both', expand=True)
            ttk.Label(scan_f,
                      text=f"Scanning for subtitles in\n"
                           f"{os.path.basename(video_path)}...",
                      wraplength=350).pack(pady=(0, 10))
            scan_bar = ttk.Progressbar(scan_f, mode='indeterminate', length=300)
            scan_bar.pack(pady=(0, 5))
            scan_bar.start(15)
            app._center_on_main(scan_dlg)
            scan_dlg.grab_set()
            scan_dlg.protocol('WM_DELETE_WINDOW', lambda: None)

            scan_result = [None]  # will hold (streams, cc_types)

            def _do_scan():
                s = get_subtitle_streams(video_path)
                cc = detect_cc_types(video_path)
                scan_result[0] = (s, cc)

            scan_thread = threading.Thread(target=_do_scan, daemon=True)
            scan_thread.start()

            def _check_scan():
                if scan_thread.is_alive():
                    editor.after(50, _check_scan)
                    return
                scan_bar.stop()
                scan_dlg.grab_release()
                scan_dlg.destroy()
                _finish_load_video(video_path, *scan_result[0])

            editor.after(50, _check_scan)

        def _finish_load_video(video_path, streams, cc_types):
            """Continue loading after subtitle/CC scanning completes."""
            nonlocal cues, original_cues

            BITMAP_CODECS = {'hdmv_pgs_subtitle', 'dvd_subtitle', 'dvb_subtitle',
                             'dvb_teletext', 'xsub'}

            # Separate text and bitmap subtitle streams
            text_streams = [s for s in streams
                            if s['codec_name'] not in BITMAP_CODECS]
            bitmap_streams = [s for s in streams
                              if s['codec_name'] in BITMAP_CODECS]

            # Build virtual CC entries for each detected type
            cc_entries = []
            if cc_types.get('eia_608'):
                cc_entries.append({
                    'index': -1,        # sentinel — not a real stream
                    'codec_name': 'eia_608',
                    'language': 'eng',
                    'title': 'Closed Captions (EIA-608)',
                    'default': False,
                    'forced': False,
                    'sdh': False,
                    '_is_cc': True,
                    '_cc_type': 'eia_608',
                })
            if cc_types.get('eia_708'):
                cc_entries.append({
                    'index': -2,        # sentinel — not a real stream
                    'codec_name': 'eia_708',
                    'language': 'eng',
                    'title': 'Closed Captions (CEA-708)',
                    'default': False,
                    'forced': False,
                    'sdh': False,
                    '_is_cc': True,
                    '_cc_type': 'eia_708',
                })

            if not text_streams and not bitmap_streams and not cc_entries:
                messagebox.showinfo("No Subtitles",
                    f"No subtitle streams found in:\n"
                    f"{os.path.basename(video_path)}",
                    parent=editor)
                return

            # If exactly one option total, use it directly
            # Mark bitmap streams so we know to use OCR
            for s in bitmap_streams:
                s['_is_bitmap'] = True
            all_options = text_streams + bitmap_streams + cc_entries
            if len(all_options) == 1:
                chosen = all_options[0]
            else:
                # Build combined list for picker: text streams + CC entries
                picker_streams = all_options
                chosen = [None]  # mutable ref for dialog result

                picker = tk.Toplevel(editor)
                picker.title("Select Subtitle Stream")
                picker.geometry(scaled_geometry(picker, 640, 420))
                picker.minsize(*scaled_minsize(picker, 500, 300))
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

                for i, s in enumerate(picker_streams):
                    is_cc = s.get('_is_cc', False)
                    is_bitmap = s.get('_is_bitmap', False)
                    lang = s['language'] if s['language'] != 'und' else 'Unknown'
                    flags = []
                    if s['default']:
                        flags.append('Default')
                    if s['sdh']:
                        flags.append('SDH')
                    if s['forced']:
                        flags.append('Forced')
                    if is_cc:
                        flags.append('CC')
                    if is_bitmap:
                        flags.append('OCR')
                    flag_str = ', '.join(flags) if flags else ''
                    title_str = s['title'] if s['title'] else ''
                    stream_id = 'CC' if is_cc else str(s['index'])
                    stream_tree.insert('', 'end', iid=str(i),
                                       values=(stream_id, lang,
                                               s['codec_name'], title_str,
                                               flag_str))

                stream_tree.selection_set('0')

                def on_select():
                    sel = stream_tree.selection()
                    if sel:
                        chosen[0] = picker_streams[int(sel[0])]
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
            is_cc = chosen.get('_is_cc', False)
            is_bitmap = chosen.get('_is_bitmap', False)
            stream_index = chosen['index']

            # ── Warn user about OCR for bitmap subtitles ──
            if is_bitmap:
                codec = chosen.get('codec_name', 'bitmap')
                lang = chosen.get('language', 'und')
                codec_label = {'hdmv_pgs_subtitle': 'PGS',
                               'dvd_subtitle': 'VobSub',
                               'dvb_subtitle': 'DVB'}.get(codec, codec)
                proceed = messagebox.askyesno(
                    "Bitmap Subtitle — OCR Required",
                    f"This subtitle stream is in {codec_label} format "
                    f"(bitmap/image-based).\n\n"
                    f"Stream #{stream_index} — {lang}\n\n"
                    f"Bitmap subtitles cannot be edited directly. "
                    f"They must be converted to text using OCR "
                    f"(Optical Character Recognition), which may "
                    f"take a few minutes and may not be 100% accurate.\n\n"
                    f"Continue with OCR?",
                    parent=editor)
                if not proceed:
                    return

            tmp_srt = tempfile.NamedTemporaryFile(suffix='.srt', delete=False,
                                                   mode='w', encoding='utf-8')
            tmp_srt.close()

            cc_type = chosen.get('_cc_type', 'eia_608') if is_cc else None

            if is_cc:
                type_label = 'EIA-608' if cc_type == 'eia_608' else 'CEA-708'
                extract_label = f"Extracting {type_label} closed captions"
            elif is_bitmap:
                extract_label = f"OCR bitmap subtitle stream #{stream_index}"
            else:
                extract_label = f"Importing subtitle stream #{stream_index}"

            # ── Bitmap OCR: launch live monitor window ──
            if is_bitmap:
                import time as _time
                try:
                    from PIL import Image, ImageTk
                    _has_pil = True
                except ImportError:
                    _has_pil = False

                ocr_lang = chosen.get('language', 'eng')
                if ocr_lang == 'und':
                    ocr_lang = 'eng'
                lang_display = chosen['language'] if chosen['language'] != 'und' else '?'

                cancel_event = threading.Event()
                ocr_result = [None]

                # ── Monitor window ──
                mon = tk.Toplevel(editor)
                mon.title(f"OCR — {os.path.basename(video_path)}")
                mon.geometry(scaled_geometry(mon, 800, 750))
                mon.minsize(*scaled_minsize(mon, 650, 550))
                mon.resizable(True, True)
                app._center_on_main(mon)

                main_f = ttk.Frame(mon, padding=10)
                main_f.pack(fill='both', expand=True)
                main_f.columnconfigure(0, weight=1)
                main_f.rowconfigure(2, weight=3)  # cue list gets more space
                main_f.rowconfigure(4, weight=1)  # log gets less space

                # ── Top: progress bar + stats ──
                top_f = ttk.Frame(main_f)
                top_f.grid(row=0, column=0, sticky='ew', pady=(0, 8))
                top_f.columnconfigure(1, weight=1)

                progress_var = tk.DoubleVar(value=0)
                status_label = ttk.Label(top_f, text="Initializing OCR...")
                status_label.grid(row=0, column=0, sticky='w', padx=(0, 8))
                progress_bar = ttk.Progressbar(top_f, variable=progress_var,
                                                maximum=100, mode='determinate')
                progress_bar.grid(row=0, column=1, sticky='ew')

                stats_label = ttk.Label(top_f, text="")
                stats_label.grid(row=1, column=0, columnspan=2, sticky='w', pady=(4, 0))

                # ── Middle: image preview + OCR text ──
                mid_f = ttk.LabelFrame(main_f, text="Current Frame", padding=6)
                mid_f.grid(row=1, column=0, sticky='ew', pady=(0, 8))
                mid_f.columnconfigure(1, weight=1)

                img_label = ttk.Label(mid_f, text="[waiting]", anchor='center',
                                      width=40, relief='sunken')
                img_label.grid(row=0, column=0, sticky='nsew', padx=(0, 8))
                img_label._photo = None

                text_frame = ttk.Frame(mid_f)
                text_frame.grid(row=0, column=1, sticky='nsew')
                text_frame.rowconfigure(0, weight=1)
                text_frame.columnconfigure(0, weight=1)

                ttk.Label(text_frame, text="OCR Text:",
                          font=('Helvetica', 9, 'bold')).grid(
                    row=0, column=0, sticky='nw')
                ocr_text_var = tk.StringVar(value="")
                ocr_text_label = ttk.Label(text_frame, textvariable=ocr_text_var,
                                            wraplength=350, justify='left',
                                            font=('Courier', 11))
                ocr_text_label.grid(row=1, column=0, sticky='nw')
                time_label = ttk.Label(text_frame, text="", foreground='gray')
                time_label.grid(row=2, column=0, sticky='sw', pady=(4, 0))

                # ── Bottom: scrolling cue list ──
                cue_frame = ttk.LabelFrame(main_f, text="Extracted Cues", padding=5)
                cue_frame.grid(row=2, column=0, sticky='nsew')
                cue_frame.columnconfigure(0, weight=1)
                cue_frame.rowconfigure(0, weight=1)

                cue_columns = ('idx', 'time', 'text')
                cue_tree = ttk.Treeview(cue_frame, columns=cue_columns,
                                        show='headings', height=8)
                cue_tree.grid(row=0, column=0, sticky='nsew')
                cue_tree.heading('idx',  text='#')
                cue_tree.heading('time', text='Time')
                cue_tree.heading('text', text='Text')
                cue_tree.column('idx',  width=40,  minwidth=30, anchor='center')
                cue_tree.column('time', width=180, minwidth=140)
                cue_tree.column('text', width=400, minwidth=200)
                cue_scroll = ttk.Scrollbar(cue_frame, orient='vertical',
                                            command=cue_tree.yview)
                cue_scroll.grid(row=0, column=1, sticky='ns')
                cue_tree.configure(yscrollcommand=cue_scroll.set)

                # ── Log window ──
                log_frame = ttk.LabelFrame(main_f, text="Log", padding=5)
                log_frame.grid(row=4, column=0, sticky='nsew', pady=(4, 0))
                log_frame.columnconfigure(0, weight=1)
                log_frame.rowconfigure(0, weight=1)

                log_text = tk.Text(log_frame, height=6, wrap='word',
                                   font=('Courier', 9), state='disabled',
                                   background='#1e1e1e', foreground='#d4d4d4',
                                   insertbackground='white')
                log_text.grid(row=0, column=0, sticky='nsew')
                log_scroll = ttk.Scrollbar(log_frame, orient='vertical',
                                           command=log_text.yview)
                log_scroll.grid(row=0, column=1, sticky='ns')
                log_text.configure(yscrollcommand=log_scroll.set)

                # ── Cancel button ──
                btn_f = ttk.Frame(main_f)
                btn_f.grid(row=5, column=0, sticky='e', pady=(8, 0))
                def _kill_ocr_processes():
                    """Kill any running ffmpeg/ffprobe/tesseract children."""
                    try:
                        import psutil
                        current = psutil.Process()
                        for child in current.children(recursive=True):
                            try:
                                name = child.name().lower()
                                if any(n in name for n in
                                       ('ffmpeg', 'ffprobe', 'tesseract')):
                                    child.kill()
                            except Exception:
                                pass
                    except ImportError:
                        import os as _os
                        pid = _os.getpid()
                        for cmd in ('ffmpeg', 'ffprobe', 'tesseract'):
                            try:
                                subprocess.run(
                                    ['pkill', '-P', str(pid), '-f', cmd],
                                    capture_output=True, timeout=2)
                            except Exception:
                                pass

                def _do_cancel():
                    """Cancel OCR: set event, kill processes, keep window open."""
                    cancel_event.set()
                    _kill_ocr_processes()

                def _do_retry():
                    """Re-run OCR with same file/stream (e.g. after adding rules)."""
                    nonlocal cancel_event
                    # Reset state
                    cancel_event = threading.Event()
                    ocr_result[0] = None
                    start_time[0] = _time.monotonic()
                    cue_count[0] = 0
                    progress_var.set(0)
                    status_label.configure(text="Restarting OCR...")
                    stats_label.configure(text="")
                    # Clear cue tree
                    for item in cue_tree.get_children():
                        cue_tree.delete(item)
                    # Clear log
                    try:
                        log_text.configure(state='normal')
                        log_text.delete('1.0', 'end')
                        log_text.configure(state='disabled')
                    except Exception:
                        pass
                    # Reset buttons
                    cancel_btn.configure(text="Cancel OCR", command=_do_cancel)
                    # Remove retry/save/load buttons if present
                    for widget in btn_f.winfo_children():
                        txt = ''
                        try:
                            txt = widget.cget('text')
                        except Exception:
                            pass
                        if txt in ('Retry', 'Save', 'Load into Editor'):
                            widget.destroy()
                    # Reload OCR rules (in case user added new ones)
                    from .subtitle_ocr import reload_ocr_rules
                    reload_ocr_rules()
                    # Start new OCR thread
                    t = threading.Thread(target=_ocr_thread, daemon=True)
                    t.start()

                cancel_btn = ttk.Button(btn_f, text="Cancel OCR",
                                        command=_do_cancel)
                cancel_btn.pack(side='right')

                def _show_ocr_rules():
                    """Show dialog to manage custom OCR replacement rules."""
                    from .subtitle_ocr import load_ocr_rules, save_ocr_rules

                    rules_win = tk.Toplevel(mon)
                    rules_win.title("Custom OCR Rules")
                    rules_win.geometry(scaled_geometry(rules_win, 520, 480))
                    rules_win.minsize(*scaled_minsize(rules_win, 400, 350))
                    rules_win.resizable(True, True)
                    app._center_on_main(rules_win)

                    ttk.Label(rules_win,
                              text="Find → Replace rules applied after Tesseract OCR.\n"
                                   "Add rules for characters Tesseract consistently misreads.",
                              padding=(10, 8)).pack(fill='x')

                    # ── Rules list ──
                    list_f = ttk.Frame(rules_win, padding=(10, 0, 10, 0))
                    list_f.pack(fill='both', expand=True)
                    list_f.columnconfigure(0, weight=1)
                    list_f.rowconfigure(0, weight=1)

                    cols = ('find', 'replace')
                    rules_tree = ttk.Treeview(list_f, columns=cols,
                                              show='headings', height=10)
                    rules_tree.grid(row=0, column=0, sticky='nsew')
                    rules_tree.heading('find', text='Find')
                    rules_tree.heading('replace', text='Replace')
                    rules_tree.column('find', width=200, minwidth=100)
                    rules_tree.column('replace', width=200, minwidth=100)
                    r_scroll = ttk.Scrollbar(list_f, orient='vertical',
                                             command=rules_tree.yview)
                    r_scroll.grid(row=0, column=1, sticky='ns')
                    rules_tree.configure(yscrollcommand=r_scroll.set)

                    # Load existing rules
                    current_rules = load_ocr_rules()
                    for find, replace in current_rules:
                        rules_tree.insert('', 'end', values=(find, replace))

                    # ── Add rule inputs ──
                    add_f = ttk.Frame(rules_win, padding=(10, 8))
                    add_f.pack(fill='x')

                    ttk.Label(add_f, text="Find:").pack(side='left')
                    find_var = tk.StringVar()
                    find_entry = ttk.Entry(add_f, textvariable=find_var, width=15)
                    find_entry.pack(side='left', padx=(4, 8))

                    ttk.Label(add_f, text="Replace:").pack(side='left')
                    replace_var = tk.StringVar()
                    replace_entry = ttk.Entry(add_f, textvariable=replace_var,
                                              width=15)
                    replace_entry.pack(side='left', padx=(4, 8))

                    def _add_rule():
                        f = find_var.get()
                        r = replace_var.get()
                        if f:
                            rules_tree.insert('', 'end', values=(f, r))
                            find_var.set('')
                            replace_var.set('')
                            find_entry.focus()

                    ttk.Button(add_f, text="Add", width=6,
                               command=_add_rule).pack(side='left', padx=(4, 0))
                    find_entry.bind('<Return>', lambda e: _add_rule())
                    replace_entry.bind('<Return>', lambda e: _add_rule())

                    # ── Buttons ──
                    btn_frame = ttk.Frame(rules_win, padding=(10, 4, 10, 10))
                    btn_frame.pack(fill='x')

                    def _delete_selected():
                        sel = rules_tree.selection()
                        for item in sel:
                            rules_tree.delete(item)

                    def _save_and_close():
                        rules = []
                        for item in rules_tree.get_children():
                            vals = rules_tree.item(item, 'values')
                            if vals and vals[0]:
                                rules.append((vals[0], vals[1]))
                        save_ocr_rules(rules)
                        from .subtitle_ocr import reload_ocr_rules
                        reload_ocr_rules()
                        rules_win.destroy()
                        if progress_callback:
                            _progress_queue.put(
                                f"OCR rules updated ({len(rules)} rules)")

                    ttk.Button(btn_frame, text="Delete Selected",
                               command=_delete_selected).pack(side='left')
                    ttk.Button(btn_frame, text="Save & Close",
                               command=_save_and_close).pack(side='right')
                    ttk.Button(btn_frame, text="Cancel",
                               command=rules_win.destroy).pack(
                                   side='right', padx=(0, 4))

                ttk.Button(btn_f, text="OCR Rules",
                           command=_show_ocr_rules).pack(
                               side='left')

                start_time = [_time.monotonic()]
                cue_count = [0]

                # ── Shared queues for thread-safe UI updates ──
                import queue as _queue
                _progress_queue = _queue.Queue()
                _frame_queue = _queue.Queue()

                # ── Callbacks: push to queue (thread-safe) ──
                def _on_frame(frame_idx, total, img_path, text, start_t, end_t):
                    _frame_queue.put((frame_idx, total, img_path, text, start_t, end_t))

                def _on_progress(msg):
                    _progress_queue.put(msg)

                _last_log_msg = ['']  # avoid duplicate log lines

                def _log(msg):
                    """Append a message to the log window."""
                    if msg == _last_log_msg[0]:
                        return  # skip duplicate consecutive messages
                    _last_log_msg[0] = msg
                    try:
                        log_text.configure(state='normal')
                        log_text.insert('end', msg + '\n')
                        log_text.see('end')
                        log_text.configure(state='disabled')
                    except tk.TclError:
                        pass

                # ── Periodic UI poll: drain queues and update widgets ──
                def _poll_ocr_updates():
                    # Drain progress queue
                    while not _progress_queue.empty():
                        try:
                            msg = _progress_queue.get_nowait()
                            status_label.configure(text=msg)
                            _log(msg)
                            import re as _re
                            pct_match = _re.search(r'\((\d+)%\)', msg)
                            if pct_match:
                                progress_var.set(float(pct_match.group(1)))
                            else:
                                frac_match = _re.search(r'(\d+)/(\d+)', msg)
                                if frac_match:
                                    n, t = int(frac_match.group(1)), int(frac_match.group(2))
                                    if t > 0:
                                        progress_var.set((n / t) * 100)
                        except _queue.Empty:
                            break

                    # Drain frame queue
                    while not _frame_queue.empty():
                        try:
                            (frame_idx, total, img_path, text,
                             start_t, end_t) = _frame_queue.get_nowait()

                            pct = ((frame_idx + 1) / total) * 100 if total > 0 else 0
                            progress_var.set(pct)
                            status_label.configure(
                                text=f"Frame {frame_idx + 1} / {total}")

                            elapsed = _time.monotonic() - start_time[0]
                            if frame_idx > 0:
                                per_frame = elapsed / (frame_idx + 1)
                                remaining = per_frame * (total - frame_idx - 1)
                                eta_m, eta_s = divmod(int(remaining), 60)
                                elapsed_m, elapsed_s = divmod(int(elapsed), 60)
                                stats_label.configure(
                                    text=f"Elapsed: {elapsed_m}m {elapsed_s}s  |  "
                                         f"ETA: {eta_m}m {eta_s}s  |  "
                                         f"Cues found: {cue_count[0]}")

                            if _has_pil and img_path and os.path.exists(img_path):
                                try:
                                    pil_img = Image.open(img_path)
                                    pil_img.thumbnail((320, 80), Image.LANCZOS)
                                    photo = ImageTk.PhotoImage(pil_img)
                                    img_label.configure(image=photo, text='')
                                    img_label._photo = photo
                                except Exception:
                                    img_label.configure(image='', text='[error]')
                            else:
                                img_label.configure(image='', text='[no image]')

                            ocr_text_var.set(text if text else '[empty]')
                            time_label.configure(text=f"{start_t} → {end_t}")

                            # Filter ghost/noise from the cue list display.
                            if text and not text.startswith('['):
                                cue_count[0] += 1
                                # Replace newlines with ⏎ for single-line display
                                display_text = text.replace('\n', ' ⏎ ')
                                cue_tree.insert('', 'end', values=(
                                    cue_count[0], f"{start_t} → {end_t}",
                                    display_text))
                                children = cue_tree.get_children()
                                if children:
                                    cue_tree.see(children[-1])
                        except _queue.Empty:
                            break

                    # Reschedule if monitor window still exists
                    try:
                        if mon.winfo_exists():
                            mon.after(200, _poll_ocr_updates)
                    except tk.TclError:
                        pass

                # Start the UI polling loop
                mon.after(200, _poll_ocr_updates)

                # ── OCR thread ──
                def _ocr_thread():
                    # Use current cancel_event (may be reassigned on retry)
                    _cancel = cancel_event
                    ocr_cues = ocr_bitmap_subtitle(
                        video_path, stream_index, ocr_lang,
                        progress_callback=_on_progress,
                        frame_callback=_on_frame,
                        cancel_event=_cancel)
                    ocr_result[0] = ocr_cues

                    def _finish():
                        elapsed = _time.monotonic() - start_time[0]
                        elapsed_m, elapsed_s = divmod(int(elapsed), 60)

                        if cancel_event.is_set():
                            cue_n = len(ocr_cues) if ocr_cues else 0
                            status_label.configure(
                                text=f"OCR cancelled — {cue_n} cues completed")
                            cancel_btn.configure(text="Close", command=mon.destroy)
                            ttk.Button(btn_f, text="Retry",
                                       command=_do_retry).pack(
                                           side='right', padx=(0, 8))

                            # Save partial results if any cues were completed
                            if ocr_cues:
                                srt_lines = []
                                for i, cue in enumerate(ocr_cues, 1):
                                    srt_lines.append(
                                        f"{i}\n{cue['start']} --> {cue['end']}\n{cue['text']}\n")
                                with open(tmp_srt.name, 'w', encoding='utf-8') as f:
                                    f.write('\n'.join(srt_lines))

                                def _save_partial():
                                    video_dir = os.path.dirname(video_path)
                                    video_stem = Path(video_path).stem
                                    default_name = f"{video_stem}.{ocr_lang}.partial.srt"
                                    save_path = ask_save_file(
                                        parent=mon, title="Save Partial OCR",
                                        initialdir=video_dir,
                                        initialfile=default_name,
                                        filetypes=[('SRT Subtitle', '*.srt'),
                                                   ('All files', '*.*')])
                                    if save_path:
                                        try:
                                            import shutil
                                            shutil.copy2(tmp_srt.name, save_path)
                                            status_label.configure(
                                                text=f"Saved {cue_n} cues: "
                                                     f"{os.path.basename(save_path)}")
                                        except Exception as e:
                                            messagebox.showerror("Save Error",
                                                f"Failed to save:\n{e}", parent=mon)

                                def _load_partial():
                                    mon.destroy()
                                    with open(tmp_srt.name, 'r', encoding='utf-8',
                                              errors='replace') as f:
                                        srt_data = f.read()
                                    title_str = (
                                        f"Docflix Subtitle Editor — "
                                        f"OCR Stream #{stream_index} "
                                        f"({lang_display}) [partial] — "
                                        f"{os.path.basename(video_path)}")
                                    if _load_cues_into_editor(srt_data, title_str,
                                                              tmp_srt.name):
                                        video_source[0] = {
                                            'path': video_path,
                                            'stream_index': stream_index,
                                            'temp_srt': tmp_srt.name,
                                            'streams': streams,
                                            'stream_info': chosen,
                                            'is_cc': False,
                                            'is_ocr': True,
                                            'ocr_lang': ocr_lang,
                                        }

                                ttk.Button(btn_f, text="Save",
                                           command=_save_partial).pack(
                                               side='right', padx=(0, 8))
                                ttk.Button(btn_f, text="Load into Editor",
                                           command=_load_partial).pack(
                                               side='right', padx=(0, 8))
                            else:
                                try:
                                    os.unlink(tmp_srt.name)
                                except Exception:
                                    pass
                            return

                        if ocr_cues:
                            # Write OCR results as SRT
                            srt_lines = []
                            for i, cue in enumerate(ocr_cues, 1):
                                srt_lines.append(
                                    f"{i}\n{cue['start']} --> {cue['end']}\n{cue['text']}\n")
                            srt_text = '\n'.join(srt_lines)
                            with open(tmp_srt.name, 'w', encoding='utf-8') as f:
                                f.write(srt_text)

                            status_label.configure(
                                text=f"Done — {len(ocr_cues)} cues in "
                                     f"{elapsed_m}m {elapsed_s}s")
                            progress_var.set(100)
                            cancel_btn.configure(text="Close", command=mon.destroy)

                            def _load_into_editor():
                                mon.destroy()
                                with open(tmp_srt.name, 'r', encoding='utf-8',
                                          errors='replace') as f:
                                    srt_data = f.read()
                                title_str = (
                                    f"Docflix Subtitle Editor — "
                                    f"OCR Stream #{stream_index} "
                                    f"({lang_display}) — "
                                    f"{os.path.basename(video_path)}")
                                if _load_cues_into_editor(srt_data, title_str,
                                                          tmp_srt.name):
                                    video_source[0] = {
                                        'path': video_path,
                                        'stream_index': stream_index,
                                        'temp_srt': tmp_srt.name,
                                        'streams': streams,
                                        'stream_info': chosen,
                                        'is_cc': False,
                                        'is_ocr': True,
                                        'ocr_lang': ocr_lang,
                                    }
                                    app.add_log(
                                        f"Opened video subtitle: OCR stream "
                                        f"#{stream_index} ({lang_display}) "
                                        f"from {os.path.basename(video_path)} "
                                        f"({len(cues)} entries)", 'INFO')
                                    editor.after(200, lambda:
                                        _load_waveform_for_video(video_path))
                                else:
                                    os.unlink(tmp_srt.name)

                            def _save_srt():
                                """Save OCR'd SRT alongside the video file."""
                                video_dir = os.path.dirname(video_path)
                                video_stem = Path(video_path).stem
                                default_name = f"{video_stem}.{ocr_lang}.srt"
                                save_path = ask_save_file(
                                    parent=mon,
                                    title="Save OCR Subtitle",
                                    initialdir=video_dir,
                                    initialfile=default_name,
                                    filetypes=[
                                        ('SRT Subtitle', '*.srt'),
                                        ('All files', '*.*'),
                                    ])
                                if save_path:
                                    try:
                                        import shutil
                                        shutil.copy2(tmp_srt.name, save_path)
                                        status_label.configure(
                                            text=f"Saved: {os.path.basename(save_path)}")
                                        app.add_log(
                                            f"OCR subtitle saved: {save_path}",
                                            'SUCCESS')
                                    except Exception as e:
                                        messagebox.showerror("Save Error",
                                            f"Failed to save:\n{e}",
                                            parent=mon)

                            ttk.Button(btn_f, text="Save",
                                       command=_save_srt).pack(
                                           side='right', padx=(0, 8))
                            ttk.Button(btn_f, text="Load into Editor",
                                       command=_load_into_editor).pack(
                                           side='right', padx=(0, 8))
                        else:
                            status_label.configure(text="OCR produced no output")
                            cancel_btn.configure(text="Close", command=mon.destroy)
                            os.unlink(tmp_srt.name)

                    editor.after(100, _finish)

                t = threading.Thread(target=_ocr_thread, daemon=True)
                t.start()

                mon.protocol('WM_DELETE_WINDOW', _do_cancel)
                return  # monitor window handles everything asynchronously

            # ── Non-bitmap: progress dialog during extraction ──
            prog_dlg = tk.Toplevel(editor)
            prog_dlg.title("Importing Subtitle")
            prog_dlg.resizable(False, False)
            prog_dlg.transient(editor)
            prog_dlg.overrideredirect(False)

            prog_f = ttk.Frame(prog_dlg, padding=20)
            prog_f.pack(fill='both', expand=True)
            prog_status = ttk.Label(prog_f,
                      text=f"{extract_label}\n"
                           f"from {os.path.basename(video_path)}...",
                      wraplength=350)
            prog_status.pack(pady=(0, 10))
            prog_var = tk.DoubleVar(value=0)
            prog_bar = ttk.Progressbar(prog_f, mode='determinate',
                                       variable=prog_var,
                                       maximum=100, length=300)
            prog_bar.pack(pady=(0, 5))

            cancel_flag = [False]
            proc_ref = [None]

            def _on_cancel_extract():
                cancel_flag[0] = True
                if proc_ref[0]:
                    try:
                        proc_ref[0].kill()
                    except Exception:
                        pass

            cancel_btn = ttk.Button(prog_f, text="Cancel",
                                    command=_on_cancel_extract)
            cancel_btn.pack(pady=(5, 0))

            app._center_on_main(prog_dlg)
            prog_dlg.grab_set()
            prog_dlg.protocol('WM_DELETE_WINDOW', _on_cancel_extract)

            extract_result = [None]  # (returncode, stderr) or Exception

            def _run_extract():
                try:
                    if is_cc:
                        # Delegate to the shared, verified CC engine
                        # (gpu.extract_closed_captions_to_srt): a 3-tier
                        # ccextractor → ffmpeg-pipe → lavfi[subcc] cascade,
                        # every tier timeout-guarded. This block used to
                        # re-implement those tiers inline and had drifted —
                        # notably a Tier-1 `proc.wait()` with NO timeout that
                        # could hang the whole extraction if ccextractor wedged
                        # on a file. One source of truth now; no hang.
                        from .utils import get_video_duration
                        dur = get_video_duration(video_path)
                        t_out = (max(120, int(dur * 0.5) + 60)
                                 if dur else 600)
                        try:
                            ok = extract_closed_captions_to_srt(
                                video_path, tmp_srt.name, cc_type, t_out)
                        except Exception:
                            ok = False

                        if cancel_flag[0]:
                            extract_result[0] = ('ok', 1, 'Cancelled')
                        elif ok:
                            extract_result[0] = ('ok', 0, '')
                        else:
                            extract_result[0] = ('ok', 1,
                                'CC extraction produced no output — '
                                'the file may not contain readable '
                                'captions.')
                    else:
                        cmd = ['ffmpeg', '-y', '-i', video_path,
                               '-map', f'0:{stream_index}',
                               '-c:s', 'srt', tmp_srt.name]
                        proc = subprocess.Popen(
                            cmd, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, text=True)
                        proc_ref[0] = proc
                        _, stderr = proc.communicate(timeout=60)
                        if cancel_flag[0]:
                            extract_result[0] = ('ok', 1, 'Cancelled')
                        else:
                            extract_result[0] = ('ok', proc.returncode,
                                                 stderr or '')
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
                    src_label = ("closed captions" if is_cc
                                 else f"subtitle stream #{stream_index}")
                    messagebox.showerror("Error",
                        f"Failed to extract {src_label}:\n"
                        f"{stderr[-300:]}",
                        parent=editor)
                    os.unlink(tmp_srt.name)
                    return

                with open(tmp_srt.name, 'r', encoding='utf-8',
                          errors='replace') as f:
                    srt_text = f.read()

                # Build title
                lang = chosen['language'] if chosen['language'] != 'und' else '?'
                if is_cc:
                    title_str = (f"Docflix Subtitle Editor — Closed Captions ({lang}) — "
                                 f"{os.path.basename(video_path)}")
                else:
                    title_str = (f"Docflix Subtitle Editor — Stream #{stream_index} ({lang}) — "
                                 f"{os.path.basename(video_path)}")

                if _load_cues_into_editor(srt_text, title_str, tmp_srt.name):
                    # Store video source info for re-muxing on save
                    video_source[0] = {
                        'path': video_path,
                        'stream_index': stream_index,
                        'temp_srt': tmp_srt.name,
                        'streams': streams,
                        'stream_info': chosen,
                        'is_cc': is_cc,
                    }
                    src_label = ("closed captions" if is_cc
                                 else f"stream #{stream_index}")
                    app.add_log(
                        f"Opened video subtitle: {src_label} ({lang}) "
                        f"from {os.path.basename(video_path)} "
                        f"({len(cues)} entries)", 'INFO')
                    # Auto-load waveform timeline
                    editor.after(200, lambda: _load_waveform_for_video(video_path))
                else:
                    os.unlink(tmp_srt.name)

            editor.after(50, _check_extract)

        def do_open_file():
            path = ask_open_file(
                parent=editor,
                title="Open Subtitle or Video File",
                filetypes=[
                    ('Subtitle files', '*.srt *.ass *.ssa *.vtt *.sub *.idx'),
                    ('Video files', '*.mkv *.mp4 *.avi *.mov *.wmv *.flv *.webm *.ts *.m2ts *.mts'),
                    ('All files', '*.*'),
                ]
            )
            if not path:
                return
            ext = Path(path).suffix.lower()
            if ext in VIDEO_EXTENSIONS or ext == '.idx':
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
                        depth = 1
                        end = i + 1
                        while end < len(raw) and depth > 0:
                            if raw[end] == '{':
                                depth += 1
                            elif raw[end] == '}':
                                depth -= 1
                            end += 1
                        paths.append(raw[i + 1:end - 1])
                        i = end + 1 if end < len(raw) else end
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
                # If a folder was dropped, look for a video file inside
                if os.path.isdir(path):
                    video_files = [
                        os.path.join(path, f) for f in sorted(os.listdir(path))
                        if Path(f).suffix.lower() in VIDEO_EXTENSIONS
                    ]
                    if video_files:
                        path = video_files[0]  # use first video found
                    else:
                        # Try subtitle files
                        sub_files = [
                            os.path.join(path, f) for f in sorted(os.listdir(path))
                            if Path(f).suffix.lower() in SUBTITLE_EXTENSIONS
                        ]
                        if sub_files:
                            path = sub_files[0]
                        else:
                            return  # no supported files in folder

                ext = Path(path).suffix.lower()
                if ext in VIDEO_EXTENSIONS or ext == '.idx':
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

            if video_source[0] and video_source[0].get('is_cc'):
                # ── CC source: save as SRT alongside the video ──
                vs = video_source[0]
                video_path = vs['path']
                srt_path = str(Path(video_path).with_suffix('.srt'))
                srt_text = write_srt(cues)
                with open(srt_path, 'w', encoding='utf-8') as f:
                    f.write(srt_text)
                app.add_log(
                    f"Closed captions saved as SRT: {len(cues)} entries "
                    f"({removed} removed) → {os.path.basename(srt_path)}",
                    'SUCCESS')
                original_cues[:] = [dict(c) for c in cues]
                current_path[0] = srt_path
                video_source[0] = None
                _flash_saved(f"✓ Saved — {len(cues)} entries → {os.path.basename(srt_path)}")

            elif video_source[0] and video_source[0].get('is_ocr'):
                # ── OCR source: save as SRT file (bitmap can't be re-muxed as text) ──
                vs = video_source[0]
                video_path = vs['path']
                ocr_lang = vs.get('ocr_lang', 'eng')
                video_stem = Path(video_path).stem
                default_name = f"{video_stem}.{ocr_lang}.srt"
                out_dir = str(Path(video_path).parent)
                out_path = ask_save_file(
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
                app.add_log(
                    f"OCR subtitle saved: {len(cues)} entries "
                    f"({removed} removed) → {os.path.basename(out_path)}",
                    'SUCCESS')
                original_cues[:] = [dict(c) for c in cues]
                current_path[0] = out_path
                video_source[0] = None
                editor.title(
                    f"Docflix Subtitle Editor — {os.path.basename(out_path)}")
                _flash_saved(
                    f"✓ Saved — {len(cues)} entries → "
                    f"{os.path.basename(out_path)}")

            elif video_source[0]:
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
                _flash_saved(f"✓ Saved — {len(cues)} entries")

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
            if (video_source[0] and video_source[0].get('is_ocr')
                    and ref_path):
                ocr_lang = video_source[0].get('ocr_lang', 'eng')
                default_name = f"{Path(ref_path).stem}.{ocr_lang}.srt"
            elif ref_path:
                default_name = f"{Path(ref_path).stem}.srt"
            else:
                default_name = "subtitle.srt"
            out_path = ask_save_file(
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
            editor.title(f"Docflix Subtitle Editor — {os.path.basename(out_path)}")
            app.add_log(f"Subtitle saved as: {out_path}", 'SUCCESS')
            _flash_saved(f"✓ Saved as — {os.path.basename(out_path)}")

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
            if (video_source[0] and video_source[0].get('is_ocr')
                    and ref_path):
                ocr_lang = video_source[0].get('ocr_lang', 'eng')
                default_name = f"{Path(ref_path).stem}.{ocr_lang}.srt"
            elif ref_path:
                default_name = f"{Path(ref_path).stem}.srt"
            else:
                default_name = "subtitle.srt"
            out_path = ask_save_file(
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
                cues = filter_fix_caps(cues,
                                      app.custom_cap_words + temp_cap_words,
                                      use_names_db=getattr(
                                          app, 'use_names_db', False))
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

        def _show_fix_music_notes_preview():
            """Show preview of OCR music note fixes before applying."""
            from .subtitle_filters import fix_music_note_text
            nonlocal cues

            # Find cues where fix_music_note_text changes the text
            fixes = []
            for i, cue in enumerate(cues):
                fixed = fix_music_note_text(cue['text'])
                if fixed != cue['text']:
                    fixes.append((i, cue, fixed))

            if not fixes:
                messagebox.showinfo("No Matches",
                    "No music note OCR misreads found.", parent=editor)
                return

            pw = tk.Toplevel(editor)
            pw.title(f"Fix Music Notes — {len(fixes)} cues matched")
            pw.geometry(scaled_geometry(pw, 750, 500))
            pw.minsize(400, 300)

            ttk.Label(pw, text="Uncheck cues you want to skip:",
                      font=('Helvetica', 10, 'bold')).pack(anchor='w', padx=10, pady=(10, 4))

            def _select_all():
                for _, var, _ in check_vars:
                    var.set(True)
            def _select_none():
                for _, var, _ in check_vars:
                    var.set(False)

            def _apply():
                nonlocal cues
                push_undo()
                apply_indices = {idx: fixed for idx, var, fixed
                                 in check_vars if var.get()}
                if not apply_indices:
                    pw.destroy()
                    return
                new_cues = []
                for i, cue in enumerate(cues):
                    if i in apply_indices:
                        new_cues.append({**cue, 'text': apply_indices[i]})
                    else:
                        new_cues.append(cue)
                cues = new_cues
                app.add_log(f"Filter 'Fix Music Notes': {len(apply_indices)} "
                             f"cues fixed", 'INFO')
                refresh_tree(cues)
                pw.destroy()

            # Buttons — single row matching ALL CAPS HI layout
            btn_frame = ttk.Frame(pw)
            btn_frame.pack(side='bottom', fill='x', padx=10, pady=(4, 10))
            ttk.Button(btn_frame, text="Select All", command=_select_all).pack(side='left', padx=(0, 4))
            ttk.Button(btn_frame, text="Select None", command=_select_none).pack(side='left')
            ttk.Label(btn_frame, text=f"{len(fixes)} cues matched",
                      foreground='gray').pack(side='left', padx=(10, 0))
            ttk.Button(btn_frame, text="Cancel", command=pw.destroy).pack(side='right', padx=(4, 0))
            ttk.Button(btn_frame, text="Apply", command=_apply).pack(side='right')

            # Scrollable checkbox list — fills remaining space
            canvas = tk.Canvas(pw)
            v_scroll = ttk.Scrollbar(pw, orient='vertical', command=canvas.yview)
            scroll_frame = ttk.Frame(canvas)
            scroll_frame.bind('<Configure>',
                lambda e: canvas.configure(scrollregion=canvas.bbox('all')))
            canvas.create_window((0, 0), window=scroll_frame, anchor='nw')
            canvas.configure(yscrollcommand=v_scroll.set)
            v_scroll.pack(side='right', fill='y', pady=4)
            canvas.pack(side='left', fill='both', expand=True, padx=(10, 0), pady=4)

            def _on_mousewheel(event):
                canvas.yview_scroll(-1 * (event.delta // 120 or (
                    -1 if event.num == 4 else 1)), 'units')
            canvas.bind('<MouseWheel>', _on_mousewheel)
            canvas.bind('<Button-4>', _on_mousewheel)
            canvas.bind('<Button-5>', _on_mousewheel)

            check_vars = []
            for idx, cue, fixed in fixes:
                var = tk.BooleanVar(value=True)
                check_vars.append((idx, var, fixed))
                frame = ttk.Frame(scroll_frame)
                frame.pack(fill='x', padx=4, pady=2)
                ttk.Checkbutton(frame, variable=var).pack(side='left')
                time_str = f"{cue['start']} → {cue['end']}"
                before_text = cue['text'].replace('\n', ' │ ')
                after_text = fixed.replace('\n', ' │ ')
                ttk.Label(frame,
                          text=f"#{idx+1}  {time_str}\n"
                               f"  Before: {before_text}\n"
                               f"  After:   {after_text}",
                          wraplength=630, justify='left').pack(side='left', padx=(4, 0))

        filter_menu.add_command(label="Fix Music Notes  ♪ (OCR)",
                                command=_show_fix_music_notes_preview)
        filter_menu.add_command(label="Fix OCR Errors  '' | 0",
                                command=lambda: apply_filter(filter_fix_ocr, "Fix OCR Errors"))
        filter_menu.add_command(label="Remove Leading Dashes  -",
                                command=lambda: apply_filter(filter_remove_leading_dashes, "Remove Leading Dashes"))

        def _show_caps_hi_preview():
            """Show preview window of cues to be removed by ALL CAPS HI filter."""
            from .subtitle_filters import _is_caps_hi_line
            nonlocal cues

            # Find cues that would be fully or partially removed
            removals = []
            for i, cue in enumerate(cues):
                lines = cue['text'].split('\n')
                caps_lines = [line for line in lines if _is_caps_hi_line(line)]
                if caps_lines:
                    removals.append((i, cue, caps_lines))

            if not removals:
                messagebox.showinfo("No Matches",
                    "No ALL CAPS HI cues found to remove.", parent=editor)
                return

            pw = tk.Toplevel(editor)
            pw.withdraw()
            pw.title(f"Remove ALL CAPS HI — {len(removals)} cues matched")
            pw.geometry(scaled_geometry(pw, 750, 500))
            pw.minsize(*scaled_minsize(pw, 650, 300))

            # Buttons packed first (bottom) so they are never clipped
            btn_frame = ttk.Frame(pw)
            btn_frame.pack(side='bottom', fill='x', padx=10, pady=(4, 10))

            ttk.Label(pw, text="Uncheck cues you want to keep:",
                      font=('Helvetica', 10, 'bold')).pack(anchor='w', padx=10, pady=(10, 4))

            # Scrollable checkbox list
            canvas = tk.Canvas(pw)
            v_scroll = ttk.Scrollbar(pw, orient='vertical', command=canvas.yview)
            scroll_frame = ttk.Frame(canvas)
            scroll_frame.bind('<Configure>',
                lambda e: canvas.configure(scrollregion=canvas.bbox('all')))
            canvas.create_window((0, 0), window=scroll_frame, anchor='nw')
            canvas.configure(yscrollcommand=v_scroll.set)
            canvas.pack(side='left', fill='both', expand=True, padx=(10, 0), pady=4)
            v_scroll.pack(side='right', fill='y', pady=4)

            def _on_mousewheel(event):
                canvas.yview_scroll(-1 * (event.delta // 120 or (
                    -1 if event.num == 4 else 1)), 'units')
            canvas.bind('<MouseWheel>', _on_mousewheel)
            canvas.bind('<Button-4>', _on_mousewheel)
            canvas.bind('<Button-5>', _on_mousewheel)

            check_vars = []
            for idx, cue, caps_lines in removals:
                var = tk.BooleanVar(value=True)
                check_vars.append((idx, var))
                frame = ttk.Frame(scroll_frame)
                frame.pack(fill='x', padx=4, pady=2)
                ttk.Checkbutton(frame, variable=var).pack(side='left')
                time_str = f"{cue['start']} → {cue['end']}"
                text_preview = cue['text'].replace('\n', ' │ ')
                ttk.Label(frame, text=f"#{idx+1}  {time_str}  —  {text_preview}",
                          wraplength=620, justify='left').pack(side='left', padx=(4, 0))

            def _select_all():
                for _, var in check_vars:
                    var.set(True)
            def _select_none():
                for _, var in check_vars:
                    var.set(False)

            def _apply():
                nonlocal cues
                push_undo()
                remove_indices = {idx for idx, var in check_vars if var.get()}
                if not remove_indices:
                    pw.destroy()
                    return
                new_cues = []
                for i, cue in enumerate(cues):
                    if i in remove_indices:
                        lines = cue['text'].split('\n')
                        kept_lines = [l for l in lines if not _is_caps_hi_line(l)]
                        text = '\n'.join(kept_lines).strip()
                        text = re.sub(r'^\s*-?\s*$', '', text, flags=re.MULTILINE)
                        text = re.sub(r'\n{2,}', '\n', text).strip()
                        if text:
                            new_cues.append({**cue, 'text': text})
                    else:
                        new_cues.append(cue)
                removed = len(cues) - len(new_cues)
                cues = new_cues
                app.add_log(f"Filter 'Remove CAPS HI': {removed} entries removed, "
                             f"{len(cues)} remaining", 'INFO')
                refresh_tree(cues)
                pw.destroy()

            ttk.Button(btn_frame, text="Select All", command=_select_all).pack(side='left', padx=(0, 4))
            ttk.Button(btn_frame, text="Select None", command=_select_none).pack(side='left')
            ttk.Label(btn_frame, text=f"{len(removals)} cues matched",
                      foreground='gray').pack(side='left', padx=(10, 0))
            ttk.Button(btn_frame, text="Cancel", command=pw.destroy).pack(side='right', padx=(4, 0))
            ttk.Button(btn_frame, text="Apply", command=_apply).pack(side='right')

            # Center on editor and show
            pw.update_idletasks()
            center_window_on_parent(pw, editor)
            pw.deiconify()

        filter_menu.add_command(label="Remove ALL CAPS HI  (UK style)",
                                command=_show_caps_hi_preview)
        filter_menu.add_command(label="Remove Off-Screen Quotes  ' '  (UK style)",
                                command=lambda: apply_filter(filter_remove_offscreen_quotes, "Remove Off-Screen Quotes"))
        filter_menu.add_separator()
        filter_menu.add_command(label="Remove Duplicates",
                                command=lambda: apply_filter(filter_remove_duplicates, "Remove Duplicates"))
        filter_menu.add_command(label="Merge Duplicates",
                                command=lambda: apply_filter(filter_merge_duplicates, "Merge Duplicates"))
        filter_menu.add_command(label="Merge Short Cues",
                                command=lambda: apply_filter(filter_merge_short, "Merge Short Cues"))
        filter_menu.add_command(label="Reduce to 2 Lines",
                                command=lambda: apply_filter(filter_reduce_lines, "Reduce to 2 Lines"))
        filter_menu.add_command(label="Collapse Paint-On CC",
                                command=lambda: apply_filter(filter_collapse_paint_on, "Collapse Paint-On CC"))
        filter_menu.add_separator()

        # ── Fix ALL CAPS ──
        if not hasattr(app, 'custom_cap_words'):
            app.custom_cap_words = []

        def show_fix_caps_dialog():
            cd = tk.Toplevel(editor)
            cd.title("Fix ALL CAPS")
            # 720 not 560: the dialog now carries THREE sections — permanent
            # names, this-file-only names, and the names database. It is
            # resizable, but the default should not open pre-cramped.
            cd.geometry("460x720")
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

            # ── This-file-only names ────────────────────────────────────────
            # Deliberately sits directly under the permanent list: the two frame
            # titles next to each other are the whole explanation of when to use
            # which, with no help text to read.
            tf = ttk.LabelFrame(cd, text="This File Only (cleared when you open another)",
                                padding=8)
            tf.pack(fill='both', expand=True, padx=10, pady=5)

            temp_list = tk.Listbox(tf, height=5, font=('Courier', 10))
            temp_list.pack(fill='both', expand=True)
            for w in temp_cap_words:
                temp_list.insert('end', w)

            temp_add_frame = ttk.Frame(tf)
            temp_add_frame.pack(fill='x', pady=(4, 0))
            temp_var = tk.StringVar()
            temp_entry = ttk.Entry(temp_add_frame, textvariable=temp_var)
            temp_entry.pack(side='left', fill='x', expand=True, padx=(0, 4))
            temp_entry.bind('<Button-3>',
                            lambda e, m=_wm: m.tk_popup(e.x_root, e.y_root))

            def add_temp():
                word = temp_var.get().strip()
                if not word:
                    return
                existing = [w.lower() for w in temp_cap_words + app.custom_cap_words]
                if word.lower() not in existing:
                    temp_cap_words.append(word)
                    temp_list.insert('end', word)
                temp_var.set('')

            def remove_temp():
                sel = temp_list.curselection()
                if sel:
                    temp_cap_words.pop(sel[0])
                    temp_list.delete(sel[0])

            ttk.Button(temp_add_frame, text="Add",
                       command=add_temp).pack(side='right')
            temp_entry.bind('<Return>', lambda e: add_temp())
            ttk.Button(tf, text="Remove Selected",
                       command=remove_temp).pack(anchor='w', pady=(4, 0))
            ttk.Label(tf,
                      text="For names that are also ordinary words — Grace, Mark, "
                           "Bill, Rose.\nAdding those permanently would capitalise "
                           "the noun too.",
                      font=('Helvetica', 8), foreground='gray',
                      justify='left').pack(anchor='w')

            # ── Names Database section ──
            nf = ttk.LabelFrame(cd, text="Names Database (optional)",
                                padding=8)
            nf.pack(fill='x', padx=10, pady=5)

            db_available = is_names_db_available()
            if db_available and is_names_db_loaded():
                status_text = f"Installed ({get_names_db_count():,} names loaded)"
            elif db_available:
                status_text = "Installed (not active)"
            else:
                status_text = "Not installed"
            status_var = tk.StringVar(value=status_text)
            ttk.Label(nf, textvariable=status_var,
                      font=('Helvetica', 9)).pack(anchor='w')

            use_db_var = tk.BooleanVar(
                value=getattr(app, 'use_names_db', False))

            def on_use_db_toggle():
                if use_db_var.get():
                    if not is_names_db_available():
                        messagebox.showinfo(
                            "Names Database",
                            "Names database not downloaded yet.\n"
                            "Click 'Download Names Database' first.",
                            parent=cd)
                        use_db_var.set(False)
                        return
                    if not is_names_db_loaded():
                        count = load_names_db()
                        status_var.set(
                            f"Installed ({count:,} names loaded)")
                else:
                    unload_names_db()
                    if is_names_db_available():
                        status_var.set("Installed (not active)")
                app.use_names_db = use_db_var.get()
                app.save_preferences()

            ttk.Checkbutton(nf, text="Use Names Database",
                            variable=use_db_var,
                            command=on_use_db_toggle).pack(anchor='w',
                                                           pady=(4, 0))

            def download_names_db():
                import urllib.request
                dl_btn.configure(state='disabled')
                status_var.set("Downloading...")
                cd.update_idletasks()

                def do_download():
                    try:
                        NAMES_DB_DIR.mkdir(parents=True, exist_ok=True)
                        for fname, url in NAMES_DB_URLS.items():
                            req = urllib.request.Request(url, headers={
                                'User-Agent': 'Docflix-Media-Suite/1.0'})
                            with urllib.request.urlopen(
                                    req, timeout=60) as resp:
                                data = resp.read()
                            (NAMES_DB_DIR / fname).write_bytes(data)
                        # Auto-load
                        count = load_names_db()
                        def on_done():
                            status_var.set(
                                f"Installed ({count:,} names loaded)")
                            use_db_var.set(True)
                            app.use_names_db = True
                            app.save_preferences()
                            dl_btn.configure(state='normal')
                        cd.after(0, on_done)
                    except Exception as e:
                        def on_err():
                            status_var.set(f"Download failed: {e}")
                            dl_btn.configure(state='normal')
                        cd.after(0, on_err)

                threading.Thread(target=do_download,
                                 daemon=True).start()

            dl_btn = ttk.Button(nf, text="Download Names Database",
                                command=download_names_db)
            dl_btn.pack(anchor='w', pady=(4, 0))
            ttk.Label(nf,
                      text="Downloads ~14 MB of first + last names "
                           "from GitHub\n(Aptivi/NamesList — open-source).",
                      font=('Helvetica', 8),
                      foreground='gray').pack(anchor='w')

            btn_frame = ttk.Frame(cd, padding=(10, 8, 10, 10))
            btn_frame.pack(fill='x')
            ttk.Button(btn_frame, text="Remove Selected", command=remove_word).pack(side='left')
            ttk.Button(btn_frame, text="Apply",
                       command=lambda: (cd.destroy(), apply_filter(
                           lambda c: filter_fix_caps(
                               # permanent + this-file-only, in that order
                               c, app.custom_cap_words + temp_cap_words,
                               use_names_db=getattr(
                                   app, 'use_names_db', False)),
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
        filter_menu.add_command(label="Highlight Spelling Errors",
                                command=lambda: _highlight_spelling())
        def _find_allcaps():
            """Toggle ALL CAPS highlighting: on if any are found, off if none."""
            if not cues:
                messagebox.showinfo("Find ALL CAPS", "No subtitle loaded.",
                                    parent=editor)
                return
            indices = scan_allcaps_words(cues)[0]
            # No modal on success — the highlighted rows ARE the result, and a
            # dialog you have to dismiss before you can look at them is pure
            # friction. The count goes to the status bar instead, so turning the
            # mode on with zero matches still says something rather than looking
            # like nothing happened. (Tony, 2026-08-06.)
            if not indices:
                caps_highlight_on[0] = False
                refresh_tree(cues)
                return
            caps_highlight_on[0] = True
            refresh_tree(cues)
            items = tree.get_children()
            first = min(indices)
            if first < len(items):
                tree.see(items[first])
                tree.selection_set(items[first])

        filter_menu.add_command(label="Find ALL CAPS Words...",
                                command=_find_allcaps)
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

            from modules.spell_checker import (is_ok_contraction,
                                               miscased_name, name_case_lut)

            spell = SpellChecker()
            all_names = app.custom_cap_words + temp_cap_words
            known = [w.lower() for w in all_names + app.custom_spell_words]
            if known:
                spell.word_frequency.load_words(known)
            # The authority for how each proper noun is really written. Rebuilt
            # by _do_add_name so a name added mid-scan starts being enforced on
            # the cues still ahead of the cursor.
            cap_lut = name_case_lut(all_names)

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
            # 560 not 500: the dictionary buttons now carry the current word in
            # their labels, and two of those plus Close overflowed a 500px row on
            # longer words. Truncation caps the label; this caps the worst case.
            sd.geometry("560x440")
            sd.resizable(True, True)
            sd.update_idletasks()
            # Center on editor window
            ew, eh = editor.winfo_width(), editor.winfo_height()
            ex, ey = editor.winfo_x(), editor.winfo_y()
            sw, sh = 560, 440
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

            # Not a constant label any more: a wrong-case name is a different
            # finding from an unknown word, and saying "Not in dictionary"
            # over a word that IS in the dictionary would be a plain lie.
            kind_var = tk.StringVar(value="Not in dictionary:")
            ttk.Label(sf, textvariable=kind_var,
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
                Returns (cue_idx, word, candidates, kind) or None.

                `kind` is 'spelling' (the dictionary does not know the word) or
                'caps' (the word IS known, as a name, but written in the wrong
                case). They are different problems with different confidence:
                a spelling candidate is a guess, a caps candidate is the exact
                string Tony stored in the name list. The UI says which.

                ⚠️ THE CAPS CHECK MUST RUN ON WORDS THE DICTIONARY ACCEPTS —
                that is the entire point, and it is why the word loop is no
                longer nested inside `if unknown:`. A name in custom_cap_words
                is by construction a known word, so anything gated on being
                unknown can never see it.
                """
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
                        for j in range(wi, len(words)):
                            w = words[j]
                            # Ignore is per-word and covers BOTH kinds — the
                            # user said "stop showing me this word".
                            if w.lower() in ignored:
                                continue
                            # Valid contraction / possessive whose ROOT is
                            # known? Not a spelling error. "Whatever's" is
                            # correct English, and once "Vanya" is in the
                            # dictionary "Vanya's" must stop being flagged too
                            # — otherwise adding a name never covers the form
                            # it usually appears in. See is_ok_contraction().
                            # It may still be MIS-CASED, so fall through
                            # rather than skipping the word entirely.
                            if ((w.lower() in unknown or w in unknown)
                                    and not is_ok_contraction(w, spell,
                                                              known)):
                                cands = spell.candidates(w)
                                spell_error_indices.add(ci)
                                scan_cue[0] = ci
                                scan_word[0] = j + 1
                                return (ci, w,
                                        sorted(cands) if cands else [],
                                        'spelling')
                            good = miscased_name(w, cap_lut)
                            if good:
                                spell_error_indices.add(ci)
                                scan_cue[0] = ci
                                scan_word[0] = j + 1
                                return (ci, w, [good], 'caps')
                    ci += 1
                    wi = 0
                    scan_cue[0] = ci
                    scan_word[0] = 0
                return None

            # ── Current error state ──
            current_error = [None]  # (ci, word, candidates)

            def _word_to_add():
                """Which word the dictionary buttons will actually store.

                ⚠️ "Replace with" WINS WHEN IT HAS CONTENT. Tony, 2026-08-07,
                on a cue reading "kazahrusian": *"There should be a mechanism to
                add unknown names to the name list. If it's already misspelled
                in the subtitle, you can't add it to the name list."*

                Exactly right. The scanned word is whatever the OCR produced —
                often lowercase, often wrong. The name list exists to tell the
                Fix ALL CAPS filter how a proper noun is really capitalised, so
                storing the broken form is worse than storing nothing: it
                teaches the filter the wrong answer.

                This stays unambiguous only because the BUTTON LABEL always
                shows the resolved word (see _update_add_labels). Do not add a
                silent preference here without keeping the label honest — the
                whole reason he had to ask which field the button used was that
                the UI implied one thing and the code did another.
                """
                typed = replace_var.get().strip()
                if typed:
                    return typed
                return current_error[0][1] if current_error[0] else ''

            def _count_changes(scanned, corrected):
                """How many cues would ACTUALLY change — not how many match.

                ⚠️ Counting matches overstates it. A cue that already reads
                "Kazahrusian" matches case-insensitively but rewrites to itself,
                so a match count of 3 next to 2 real edits is a small lie in the
                one place the user is deciding whether to click. Count the diff.
                """
                if not scanned or not corrected or scanned == corrected:
                    return 0
                return sum(1 for c in cues
                           if replace_word(c['text'], scanned, corrected)
                           != c['text'])

            def _update_add_labels(*_):
                """Keep the dictionary buttons showing the word they will add,
                and warn when adding will also rewrite the subtitle."""
                word = _word_to_add()
                if not word:
                    add_dict_btn.configure(text="Add to Dict")
                    add_name_btn.configure(text="Add as Name")
                    fix_hint.configure(text='')
                    return
                shown = word if len(word) <= 12 else word[:11] + '…'
                add_dict_btn.configure(text=f'Add "{shown}" to Dict')
                add_name_btn.configure(text=f'Add "{shown}" as Name')
                # ⚠️ ADDING A CORRECTION REWRITES THE FILE. Say so, with a count.
                # The buttons are labelled "Add", so a file-wide replace is a
                # side effect the label cannot carry — it would need ~35
                # characters. This line does the honest work instead, and it
                # doubles as an answer to "how many will it fix?" BEFORE the
                # click rather than after.
                scanned = current_error[0][1] if current_error[0] else ''
                n = _count_changes(scanned, word)
                if not n:
                    fix_hint.configure(text='')
                else:
                    verb = ('recase' if word.lower() == scanned.lower()
                            else 'fix')
                    fix_hint.configure(
                        text=f'↳ will also {verb} {n} '
                             f'cue{"" if n == 1 else "s"} of "{scanned}"')

            # NOTE: the trace that keeps these labels live is registered AFTER
            # the buttons are created (see below) — _update_add_labels touches
            # widgets that do not exist yet at this point in the build.

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
                ci, w, ca, kind = result
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
                    # ⚠️ SHOW NAMES THE WAY TONY WROTE THEM. pyspellchecker's
                    # word_frequency is lowercase-only, so a custom name offered
                    # as a candidate comes back lowercased — "kazahrusan"
                    # suggests "kazahrusian", not "Kazahrusian". Accepting that
                    # verbatim inserts a wrong-case name, which is precisely the
                    # defect the caps pass exists to find, and the scanner has
                    # already advanced past this word so it would NOT be caught
                    # this run. The tool would be manufacturing its own findings.
                    #
                    # Corrected HERE, at the point the lowercase artifact is
                    # created, rather than on the way out. Anything the user
                    # TYPES is left exactly as typed: a deliberate lowercase
                    # "mark" must stay "mark" even though "Mark" is a name, and
                    # snapping the case downstream could not tell the two apart.
                    sug_lb.insert('end', cap_lut.get(c.lower(), c))
                if kind == 'caps':
                    # THE ONE DOCUMENTED EXCEPTION to the no-pre-fill rule
                    # below, and it holds only because the suggestion is not a
                    # suggestion: it is the exact string Tony put in the name
                    # list, so there is nothing to guess and no wrong answer to
                    # click. Narrow on purpose — if a future finding is ever
                    # "probably X", it does NOT get to reuse this.
                    kind_var.set("Wrong capitalization:")
                    sug_lb.selection_set(0)
                    # Read back from the listbox, not from `ca`, so the
                    # pre-filled value can never disagree with the one line the
                    # user is looking at.
                    replace_var.set(sug_lb.get(0))
                    _update_add_labels()
                    return
                kind_var.set("Not in dictionary:")
                # ⚠️ DO NOT PRE-SELECT A SUGGESTION OR PRE-FILL "Replace with".
                # This used to auto-fill the top candidate, which meant every
                # proper noun arrived with a WRONG answer already loaded and the
                # destructive button one reflex-click away. Tony hit it on
                # "Vanya" — suggestions were 'tanya' and 'vanda', and "Replace
                # with" already said 'tanya'. One click would have renamed a
                # character and lowercased her. Replace with an empty field is a
                # no-op (see _do_replace), so the safe action is now the default
                # and picking a suggestion is a deliberate act. (2026-08-07)
                replace_var.set('')
                _update_add_labels()

            def _do_replace():
                if not current_error[0]:
                    return
                ci, w, _, kind = current_error[0]
                repl = replace_var.get().strip()
                if not repl:
                    return
                push_undo()
                # count=1: fix the first whole-word hit, then let the scanner
                # find the rest. Repeated errors in one cue still resolve one
                # click at a time, because each replace removes the match the
                # next search would have found.
                # exact=True for a recase, or the count=1 gets spent on an
                # already-correct "Hirst" earlier in the same cue and the
                # broken one survives unflagged — see replace_word().
                cues[ci]['text'] = replace_word(cues[ci]['text'], w, repl,
                                                count=1, exact=(kind == 'caps'))
                refresh_tree(cues)
                # Re-check same cue from current word position
                _show_next()

            def _do_replace_all():
                if not current_error[0]:
                    return
                _, w, _, kind = current_error[0]
                repl = replace_var.get().strip()
                if not repl:
                    return
                push_undo()
                # Whole-word across every cue. The old version used
                # str.replace() — substring, file-wide — which is the most
                # destructive button in the editor pointed at the least precise
                # matcher available. replace_word() also carries the original
                # capitalisation onto each hit, so a sentence-initial "Teh"
                # becomes "The" rather than "the".
                for cue in cues:
                    cue['text'] = replace_word(cue['text'], w, repl,
                                               exact=(kind == 'caps'))
                ignored.add(w.lower())
                refresh_tree(cues)
                _show_next()

            def _do_skip():
                _show_next()

            def _do_ignore():
                if current_error[0]:
                    ignored.add(current_error[0][1].lower())
                _show_next()

            # ⚠️ BOTH HANDLERS STORE _word_to_add() (the typed correction when
            # there is one) BUT IGNORE THE SCANNED WORD. Two different words on
            # purpose: the dictionary should learn "Kazahrusian", while the
            # scanner needs to stop stopping on "kazahrusian" — otherwise adding
            # a correction leaves the broken spelling flagged forever and the
            # button appears to have done nothing.
            def _apply_correction_everywhere(scanned, corrected):
                """Rewrite every whole-word occurrence of `scanned`. Returns the
                number of cues changed.

                ⚠️ FILE-WIDE, NOT JUST THIS CUE, AND THAT IS THE SAFE CHOICE —
                which is not obvious. Tony asked: "when the user adds a name does
                it fix every other occurrence?" It has to. Adding also calls
                ignored.add(scanned), so the scanner stops offering that word
                again; if only the current cue were fixed, every OTHER broken
                occurrence would stay wrong AND never be shown again. A partial
                fix plus a permanent silence is worse than no fix at all.

                Typing a correction is a definitional act — "this word is spelled
                like this" — so it applies to the whole file, same as the name it
                writes into custom_cap_words.
                """
                if not scanned or not corrected or scanned == corrected:
                    return 0
                changed = 0
                for cue in cues:
                    new = replace_word(cue['text'], scanned, corrected)
                    if new != cue['text']:
                        cue['text'] = new
                        changed += 1
                return changed

            def _do_add_dict():
                if not current_error[0]:
                    return
                w = _word_to_add()
                scanned = current_error[0][1]
                if not w:
                    return
                if w != scanned:
                    push_undo()
                    _apply_correction_everywhere(scanned, w)
                    refresh_tree(cues)
                if w.lower() not in [x.lower()
                                     for x in app.custom_spell_words]:
                    app.custom_spell_words.append(w)
                    app.save_preferences()
                spell.word_frequency.load_words([w.lower()])
                ignored.add(scanned.lower())
                _show_next()

            def _do_add_name():
                if not current_error[0]:
                    return
                w = _word_to_add()
                scanned = current_error[0][1]
                if not w:
                    return
                if w != scanned:
                    push_undo()
                    _apply_correction_everywhere(scanned, w)
                    refresh_tree(cues)
                # custom_cap_words is CASE-SENSITIVE on purpose — it is the
                # record of how the proper noun is really written, which is the
                # whole point of adding a corrected form rather than the OCR's.
                if w not in app.custom_cap_words:
                    app.custom_cap_words.append(w)
                if w.lower() not in [x.lower()
                                     for x in app.custom_spell_words]:
                    app.custom_spell_words.append(w)
                spell.word_frequency.load_words([w.lower()])
                # Teach the wrong-case check the name too, or the cues still
                # ahead of the cursor keep their broken capitalisation AND stop
                # being flagged — the exact blind spot this pass exists to
                # close, reopened one name at a time.
                cap_lut[w.lower()] = w
                app.save_preferences()
                ignored.add(scanned.lower())
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
            # No fixed width — the labels carry the current word and have to grow.
            # Both act on the "Not in dictionary" word, never on "Replace with";
            # _show_next() rewrites these so that is visible instead of implied.
            #   Add to Dict → custom_spell_words        (stop flagging it)
            #   Add as Name → + custom_cap_words        (also shields it from the
            #                                            Fix ALL CAPS filter)
            add_dict_btn = ttk.Button(bf2, text="Add to Dict",
                                      command=_do_add_dict)
            add_dict_btn.pack(side='left', padx=2)
            add_name_btn = ttk.Button(bf2, text="Add as Name",
                                      command=_do_add_name)
            add_name_btn.pack(side='left', padx=2)
            ttk.Button(bf2, text="Close", command=sd.destroy,
                       width=6).pack(side='right', padx=2)

            # Carries the side effect the "Add" labels cannot: adding a typed
            # correction rewrites the whole file, and this says how many cues
            # before the click.
            fix_hint = ttk.Label(bf, text='', font=('Helvetica', 8),
                                 foreground='#b35c00')
            fix_hint.pack(fill='x', pady=(4, 0))

            # Registered HERE, not where _update_add_labels is defined: the
            # callback configures add_dict_btn / add_name_btn, so the trace must
            # not be able to fire before they exist. Nothing writes replace_var
            # in between today, but that is an accident of ordering rather than
            # a guarantee, and a NameError inside a Tk trace fails quietly.
            replace_var.trace_add('write', _update_add_labels)

            # Start scanning immediately
            _show_next()

        editor.bind('<F7>', lambda e: _show_spell_check())

        def _highlight_spelling():
            """Highlight cues with spelling errors without opening the
            interactive correction dialog.  Useful for quickly spotting
            OCR errors at a glance."""
            if not cues:
                messagebox.showinfo("Highlight Spelling",
                                    "No subtitle loaded.",
                                    parent=editor)
                return
            from modules.spell_checker import run_spell_highlight_scan
            errors_by_cue = run_spell_highlight_scan(
                app, editor, cues, spell_error_indices)
            if errors_by_cue is None:
                return          # checker unavailable — it already said so
            spell_scanned[0] = True
            refresh_tree(cues)      # paints the rows AND updates the status bar
            # NO RESULTS POPUP. Tony, 2026-08-07: "yes please remove that popup.
            # It's not needed." Same call he made on the ALL CAPS highlighter the
            # day before — this tool's whole job is to colour rows, and a modal
            # you have to dismiss before you can look at them is working against
            # the one thing it does. The count now lives in the status bar next
            # to "N ALL CAPS", which is where the eye already goes.
            # The "no errors found" popup went too: the status bar says
            # "0 misspelled" instead, so a clean file still gives feedback
            # without stealing focus.
            if errors_by_cue:
                first = min(errors_by_cue.keys())
                items = tree.get_children()
                if first < len(items):
                    tree.see(items[first])
                    tree.selection_set(items[first])

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
            # Remember position for auto-select after delete
            next_idx = min(int(s) for s in selected)
            push_undo()
            indices_to_remove = set(int(s) for s in selected)
            cues = [c for i, c in enumerate(cues) if i not in indices_to_remove]
            refresh_tree(cues)
            # Auto-select the next cue so the user can keep deleting
            if cues:
                select_idx = min(next_idx, len(cues) - 1)
                iid = str(select_idx)
                tree.selection_set(iid)
                tree.focus(iid)
                tree.see(iid)

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
                    p = ask_open_file(
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
                            depth = 1
                            end = i + 1
                            while end < len(raw) and depth > 0:
                                if raw[end] == '{':
                                    depth += 1
                                elif raw[end] == '}':
                                    depth -= 1
                                end += 1
                            paths.append(raw[i + 1:end - 1])
                            i = end + 1 if end < len(raw) else end
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
                vpath = ask_open_file(
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

        def _open_forced_subs():
            """Forced Subtitle Editor — build a forced track for mixed-language media.

            Deliberately its OWN window with its OWN span list. This editor's cue
            model IS the live editing session; a bug in the forced-subs work must
            never be able to corrupt an edit in progress. It shares the
            WaveformTimeline widget, not the state.
            """
            try:
                from .forced_subs_panel import open_forced_subs_panel
            except Exception as exc:
                messagebox.showerror(
                    "Unavailable",
                    f"Forced Subtitle Editor could not load:\n{exc}", parent=editor)
                return
            try:
                vpath = _find_video_for_subtitle()
            except Exception:
                vpath = None
            open_forced_subs_panel(editor, video_path=vpath)

        view_menu.add_separator()
        view_menu.add_command(label="Forced Subtitle Editor...",
                              command=_open_forced_subs)

        def _find_video_for_subtitle():
            """The video file for the current subtitle, or None.

            Strips language/tag suffixes off the subtitle name and looks for a
            video with the same stem beside it — "Show - S03E08 - Title.eng.srt"
            finds "Show - S03E08 - Title.mkv". Verified against Tony's library:
            96 of 96 subtitles matched exactly.

            ⚠️ RETURNING NOTHING IS A VALID, CORRECT ANSWER. There used to be a
            fallback here that globbed the directory and returned the FIRST
            video it found. In a flat season folder that is episode 1, for every
            subtitle in the season — so a single rename or an odd extension
            would have you syncing S03E08's dialogue against S01E01's audio,
            with a waveform that looks perfectly normal. The caller already
            handles None by asking the user to pick the file, which is the whole
            point: "I could not work it out" is useful, a confident wrong guess
            is not. Do not reintroduce a fallback. (2026-08-07)
            """
            if video_source and video_source[0]:
                vpath = video_source[0].get('path')
                if vpath:
                    return vpath
            if not current_path[0]:
                return None
            sub_dir = os.path.dirname(current_path[0])
            sub_stem = os.path.splitext(os.path.basename(current_path[0]))[0]
            # Peel tag suffixes one at a time (".eng", ".forced", ".sdh"),
            # checking after each — so a title containing a period is not
            # eaten before it gets a chance to match.
            for _ in range(4):
                for ext in VIDEO_EXTENSIONS:
                    candidate = os.path.join(sub_dir, sub_stem + ext)
                    if os.path.isfile(candidate):
                        return candidate
                if '.' not in sub_stem:
                    break
                sub_stem = sub_stem.rsplit('.', 1)[0]
            return None

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
                    p = ask_open_file(
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
        install_cue_sort(tree, lambda: cues)
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
        tree.tag_configure(TAG_CAPS, background='#d7c4f2')    # lavender — ALL CAPS words

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
            # Paste must REPLACE the selection here. tk.Text does not do that on
            # its own — it inserts at the cursor and leaves the selected text in
            # place. See paste_over_selection(). (Tony, 2026-08-07)
            edit_entry.bind('<<Paste>>', paste_over_selection)

            def save_edit(e=None):
                nonlocal edit_entry
                new_text = edit_entry.get('1.0', 'end-1c').strip()
                if new_text:
                    cues[idx]['text'] = new_text
                    display = new_text.replace('\n', ' \\n ')
                    tree.set(item, 'text', display)
                    orig_text = original_cues[idx]['text'] if idx < len(original_cues) else None
                    ctags = _classify_cue(cues[idx], orig_text)
                    # ⚠️ MUST MATCH refresh_tree's PRIORITY CHAIN. This one used
                    # to check only (MODIFIED, HI, TAGS, LONG) — so editing a cue
                    # stripped its SPELL or CAPS highlight even when the row still
                    # qualified, and it came back the moment anything triggered a
                    # full refresh. A highlight that disappears on edit and
                    # reappears later reads as a flaky feature, not a stale cache.
                    # SEARCH is deliberately absent: the edit path has no access to
                    # the live search set, and a stale green row would be worse
                    # than none.
                    if idx in spell_error_indices:
                        row_tag = TAG_SPELL
                    elif (caps_highlight_on[0]
                          and idx in scan_allcaps_words(cues)[0]):
                        row_tag = TAG_CAPS
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
                    tree.item(item, tags=(row_tag,) if row_tag else ())
                else:
                    del cues[idx]
                    refresh_tree(cues)
                edit_entry.destroy()
                edit_entry = None
                _rebuild_stats()

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
            edit_ctx.add_separator()

            # ── Add to Temp Names, straight off the selection ────────────────
            # Tony, 2026-08-07: *"add the ability to add it to the temp name file
            # by selecting the name then right click --> add to temp name file."*
            #
            # Better than the dialog I built first, and the reason is instructive:
            # he went looking for this in the SPELL CHECKER because that is where
            # we had been working, not in Fix ALL CAPS where I had put it. A name
            # like Grace is never flagged (it is a dictionary word), so there is
            # no error to click — the only way in was to know which dialog it
            # lived in and type her name again. Selecting the word you can already
            # see removes both the hunting and the typing.
            def _selected_name():
                """The selected text, trimmed to something name-shaped. '' if none."""
                if not edit_entry:
                    return ''
                try:
                    if not edit_entry.tag_ranges('sel'):
                        return ''
                    raw = edit_entry.get('sel.first', 'sel.last')
                except Exception:
                    return ''
                # Trim surrounding punctuation/space but keep internal spaces and
                # apostrophes — filter_fix_caps handles multi-word entries, so
                # "Van Helsing" and "O'Brien" both need to survive intact.
                word = raw.strip().strip('.,!?;:"“”()[]-—…').strip()
                return _name_case(word)

            def _add_selection_to_temp():
                word = _selected_name()
                if not word:
                    return
                existing = [w.lower()
                            for w in temp_cap_words + app.custom_cap_words]
                if word.lower() not in existing:
                    temp_cap_words.append(word)
                if edit_entry:
                    edit_entry.focus_force()

            edit_ctx.add_command(label="Add to Temp Names",
                                 command=_add_selection_to_temp)
            _TEMP_ITEM = edit_ctx.index('end')      # remembered so the label can
            #                                          be rewritten per popup

            _edit_ctx_open = [False]

            def show_edit_ctx(event):
                _edit_ctx_open[0] = True
                # Name the word on the menu item, and disable it when there is
                # nothing selected — same contract as the spell dialog's Add
                # buttons: never offer an action without saying what it acts on.
                word = _selected_name()
                if word:
                    shown = word if len(word) <= 18 else word[:17] + '…'
                    already = word.lower() in [
                        w.lower() for w in temp_cap_words + app.custom_cap_words]
                    edit_ctx.entryconfigure(
                        _TEMP_ITEM,
                        label=(f'Already a known name: "{shown}"' if already
                               else f'Add "{shown}" to Temp Names'),
                        state=('disabled' if already else 'normal'))
                else:
                    edit_ctx.entryconfigure(
                        _TEMP_ITEM,
                        label="Add to Temp Names  (select a name first)",
                        state='disabled')
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
            # mpv draws into video_embed_frame; the placeholder has to get out
            # of the way at that exact moment, and only the timeline knows when
            # playback actually starts.
            on_video_start=lambda: _video_placeholder.place_forget(),
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

        def _set_video_note(text):
            """Put a message in the (otherwise black) video pane."""
            _video_placeholder.configure(text=text)
            _video_placeholder.place(relx=0.5, rely=0.5, anchor='center')

        def _load_waveform_for_video(video_path):
            """Load waveform from video, show the timeline and video panel.

            ⚠️ THE PANE IS BLACK UNTIL YOU PRESS PLAY, AND THAT IS BY DESIGN —
            mpv is only launched from the timeline's play button. This used to
            call _video_placeholder.place_forget() here, which DELETED the one
            line of text explaining that, so clicking "Load Waveform" replaced a
            helpful hint with an empty black rectangle. Combined with the ~7s
            ffmpeg extraction (measured: 6.9s for a 43-minute episode) the whole
            thing was indistinguishable from broken, and Tony reasonably
            concluded it had failed and dragged the video in by hand.
            Nothing was wrong. It just never said so. (2026-08-07)
            """
            if not video_path or not os.path.isfile(video_path):
                return
            _show_video()
            _show_timeline()
            # ⚠️ COMPARE THE PATH, NOT `is_loaded`. Guarding on is_loaded meant
            # the first video won for the life of the editor — open a second
            # subtitle and you kept the first episode's audio under the new
            # cues, silently. Tony hit this one for real.
            if timeline.current_video == video_path:
                return
            name = os.path.basename(video_path)
            _set_video_note(f"⏳ Extracting audio…\n\n{name}\n\n"
                            f"a few seconds for a full episode")

            def _done(ok):
                if ok:
                    _set_video_note(f"▶  Press Play on the timeline\n"
                                    f"to start video\n\n{name}")
                else:
                    _set_video_note(f"⚠ Could not read audio from\n\n{name}\n\n"
                                    f"See the log for details.")
            timeline.load_audio(video_path, done_callback=_done)

        def _follow_waveform_to_subtitle():
            """Point an already-open waveform at the newly loaded subtitle.

            Tony, 2026-08-07: *"I had the waveform up and working but when I
            changed the subtitle it didn't drop the video file and load the new
            one."* Only runs when a waveform is already showing — opening a
            subtitle never starts a 7-second extraction you did not ask for.

            ⚠️ WHEN THE VIDEO CANNOT BE FOUND, SAY SO INSTEAD OF LEAVING IT.
            Silently keeping the previous episode's audio under new cues is the
            precise failure this whole change exists to remove, and it would be
            worse here than before — the waveform would look freshly loaded.
            """
            try:
                if not timeline_visible[0] or not timeline.is_loaded:
                    return
            except NameError:          # window still being built
                return
            vpath = _find_video_for_subtitle()
            if vpath == timeline.current_video:
                return
            if vpath:
                _load_waveform_for_video(vpath)
            else:
                stale = os.path.basename(timeline.current_video or '?')
                _set_video_note(
                    f"⚠ No video found for this subtitle.\n\n"
                    f"The waveform below is still\n{stale}\n\n"
                    f"Use View → Load Waveform to pick the right file.")

        # ── Status bar ──
        status_frame = ttk.Frame(content_frame, padding=(10, 6, 10, 6))
        status_frame.pack(fill='x')

        stats_label = ttk.Label(status_frame, text="0 entries")
        stats_label.pack(side='left')

        saved_label = ttk.Label(status_frame, text="", foreground='green')
        saved_label.pack(side='left', padx=(10, 0))

        def _flash_saved(msg="✓ Saved"):
            saved_label.configure(text=msg)
            editor.after(3000, lambda: saved_label.configure(text=""))

        ttk.Button(status_frame, text="💾 Save", command=do_save_file).pack(side='right', padx=(4, 0))
        ttk.Button(status_frame, text="📤 Export SRT", command=do_export).pack(side='right', padx=4)
        ttk.Button(status_frame, text="🗑 Delete", command=delete_selected).pack(side='right', padx=4)

        # ── Refresh tree function ──
        def refresh_tree(new_cues, search_indices=None):
            nonlocal cues
            cues = new_cues
            tree.delete(*tree.get_children())
            search_set = set(search_indices or [])
            # Re-scan for ALL CAPS on every rebuild while the mode is on, so the
            # highlighting survives deletes, edits, undo and filtering.
            caps_set = scan_allcaps_words(cues)[0] if caps_highlight_on[0] else set()
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
                elif i in caps_set:
                    # Above MODIFIED on purpose: once you fix the caps word the
                    # row stops matching and drops to yellow, which is a useful
                    # "done" signal while working through them.
                    row_tag = TAG_CAPS
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
            _rebuild_stats()
            # Refresh waveform timeline cue blocks and live subtitles
            if timeline.is_loaded:
                timeline.refresh()
                timeline.reload_subtitles()

        # Delete key shortcut
        editor.bind('<Delete>', lambda e: None if isinstance(e.widget, tk.Text) else delete_selected())

        # ── Disable menus until a file is loaded ──
        # View is deliberately NOT in this list. Tools/Edit/Timing all act on the
        # loaded cues and are meaningless without them, but every View item works
        # on an empty editor:
        #   Load Waveform...        prompts for a video when there's no subtitle
        #   Show/Hide Timeline      just toggles the pane
        #   Forced Subtitle Editor  its own window, accepts video_path=None
        # And since 3.12.5 the Forced Subtitle Editor is reachable ONLY from here,
        # so greying View out made it unreachable until you loaded an unrelated
        # subtitle first. (2026-08-05)
        def _set_menus_state(state):
            for menu_label in ('Tools', 'Edit', 'Timing'):
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

        # Direct entry: opened with a specific subtitle to edit — the path the
        # double-click→edit callers will use once redirected here (Stage 2). Reuses
        # editor 1's own loaders; purely additive — a no-op when no params are passed.
        # (auto_stream is accepted for caller-signature compatibility; the loader's
        #  own picker handles multi-stream selection.)
        if auto_external and os.path.isfile(auto_external):
            editor.after(120, lambda p=auto_external: load_file(p))
        elif auto_video and os.path.isfile(auto_video):
            editor.after(120, lambda p=auto_video: load_video_subtitle(p))

        if not getattr(app, '_standalone_mode', False):
            editor.wait_window()

    # ── Media Processor ──────────────────────────────────────────────────────





def main():
    """Launch Subtitle Editor as a standalone application.
    Accepts an optional file path as a command-line argument."""
    import sys
    from .standalone import create_standalone_root

    root, app = create_standalone_root(
        title="Docflix Subtitle Editor",
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
