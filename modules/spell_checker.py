"""
Docflix Media Suite — Subtitle Spell Checker

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


# ── Contractions and possessives ─────────────────────────────────────────────
# Tony, 2026-08-07, on "Whatever's" being flagged with suggestions 'whatever'
# and 'whitener's': *"Most spell checkers will flag things with an apostrophe
# like what's, like's etc... We should have a way to determine whether it's a
# contraction and spell check the root and not the whole word. This happens
# with names as well."*
#
# pyspellchecker only knows whole tokens, so every possessive of a proper noun
# is a false positive — and it stays one FOREVER, because adding "Vanya" to the
# custom dictionary does nothing for "Vanya's". That is what makes this worth
# fixing rather than skipping past: the dictionary silently fails to cover the
# form the name most often appears in.
_CONTRACTION_SUFFIXES = {'s', 're', 've', 'll', 'd', 'm', 't'}

# ⚠️⚠️ THE APOSTROPHE CLASS MUST INCLUDE THE CURLY ONE, and this regex is the
# reason a fix lives here rather than in three copies. Found 2026-09-20 by the
# Note column making it visible: the tokeniser only knew `'`, so on a file
# using typographic quotes `isn’t` was torn into `isn` + `t` BEFORE
# is_ok_contraction() — which does normalise `’` — ever saw the word. The doer
# was curly-aware; the splitter was not, so the word never reached it.
# (Same shape as [[reference_detector-doer-divergence]].)
#
# ⭐ WHY IT HID FOR SO LONG: most contractions degrade into real words.
# `that’s` -> `that` + `s`, both known, nothing flagged. It only surfaces on
# stems that are not words — isn, hasn, doesn, wasn, couldn, wouldn, didn,
# aren, weren, shouldn, hadn.
#
# ⚠️ Measured across 250 library files (151,618 cues): only 4% of files use
# curly apostrophes and the fix removes 167 false positives — about 1% of all
# flags. Small. It is worth fixing anyway because a false positive the user can
# ACT ON is worse than one he can only ignore: `sp: isn` invites a right-click
# "add to dictionary", which permanently poisons custom_spell_words — the same
# list that feeds the Fix ALL CAPS filter.
#
# ⛔ Do not re-narrow this to a bare `'`. Use WORD_RE everywhere a subtitle is
# split into words for spelling; there were three separate copies of the old
# pattern and all three had the bug.
WORD_RE = re.compile(r"[a-zA-Z]+(?:['’][a-zA-Z]+)?")

# No letter-root to recover — these do not decompose.
_IRREGULAR_CONTRACTIONS = {
    "won't": ["will"], "shan't": ["shall"], "ain't": ["be", "is", "am"],
    "o'clock": ["clock"], "y'all": ["you", "all"],
}


def contraction_roots(word):
    """Candidate roots for a contraction/possessive; [] if it is not one.

    ⚠️ RETURNS CANDIDATES, NOT ONE ANSWER, because n't is genuinely ambiguous:
        doesn't = does + n't   -> strip 3
        can't   = can  + 't    -> strip 2
    "can" is the one root that already ends in n, so no single rule covers both.
    Rather than enumerate exceptions, hand the dictionary both and let it pick;
    the caller accepts the word if ANY candidate is known.

    Curly apostrophes are normalised first — subtitles are full of them and
    "Whatever's" must behave exactly like "Whatever's".
    """
    w = (word or '').replace('’', "'")
    if "'" not in w:
        return []
    low = w.lower()
    if low in _IRREGULAR_CONTRACTIONS:
        return _IRREGULAR_CONTRACTIONS[low]
    out = []
    if low.endswith("n't"):
        out.append(w[:-3])      # doesn't -> does
        out.append(w[:-2])      # can't   -> can
    root, _, suffix = w.rpartition("'")
    # suffix == '' covers plural possessives: boys'
    if root and (suffix == '' or suffix.lower() in _CONTRACTION_SUFFIXES):
        out.append(root)
    return [r for r in dict.fromkeys(out) if r]


def is_ok_contraction(word, spell, extra_known=()):
    """True if `word` is a contraction/possessive whose ROOT is known.

    ⚠️ Only ever SUPPRESSES a flag — it can never create one. If the root is
    unknown too ("Xyzzy's"), this returns False and the word stays flagged, so
    a genuinely misspelled name is still caught. Adding the root to the
    dictionary then clears every possessive of it at once, which is the point.
    """
    roots = contraction_roots(word)
    if not roots:
        return False
    known = {str(x).lower() for x in extra_known}
    for r in roots:
        if r.lower() in known:
            return True
        try:
            if not spell.unknown([r]):
                return True
        except Exception:
            pass
    return False


def iter_words(text):
    """Yield (word, followed_by_apostrophe) for every token in `text`.

    ⚠️⚠️ WHY THIS EXISTS RATHER THAN A WIDER WORD_RE. The dropped-g rule below
    needs to know whether a token was written `trailin'` or `trailin`, and
    WORD_RE throws the trailing apostrophe away. The obvious fix — append
    `['’]?` to the pattern — would change EVERY token in the suite: UK-style
    single-quoted dialogue (`'Run'`) would start yielding `Run'`, and
    pyspellchecker would flag a word that is spelled perfectly. That is the
    widened-band mistake the music-note work already paid for once.

    So the pattern stays exactly as it is and the apostrophe is read from the
    text AFTER the match. ⭐ And it lives here, called by all THREE scanners
    (both in this file and _find_next in the editor), because a rule the
    highlighter honours and the F7 dialog does not is the detector/doer split
    that keeps catching us.
    """
    for m in WORD_RE.finditer(text):
        yield m.group(0), text[m.end():m.end() + 1] in ("'", "’")


def is_dropped_g(word, had_apostrophe, spell):
    """True if `word` is the dialect `-in'` spelling of a known `-ing` word.

    Tony, 2026-09-26: *"I'm noticing words that I know I put into the
    dictionary, coming back in the next files I'm working on... most of them
    are contractions like trailin', dyin', ridin'."*

    ⭐ HIS DICTIONARY WAS NEVER THE PROBLEM — it is global and it persists, and
    `trailin'` really was in it. What he was fighting is a TREADMILL: measured
    across 40 of his subtitle files there are 114 distinct dropped-g forms, he
    had added 44 of them one at a time, and every new file arrives carrying
    ones he has not met yet (runnin, makin, killin, sayin, walkin, mornin...).
    Adding them one by one can never finish. So this suppresses the CLASS,
    exactly as is_ok_contraction does for possessives.

    ⛔ THE APOSTROPHE IS REQUIRED, and the measurement is why. Of 278
    candidates in those 40 files, 272 (97.8%) were written with one. The six
    that were not include **westin** (the hotel) and **lavin** (a surname) —
    so the lax version of this rule would have quietly accepted proper nouns
    as dialect, while gaining 2% coverage. Requiring the apostrophe is both
    safer AND catches nearly everything.

    ⚠️ Like is_ok_contraction this can only ever SUPPRESS a flag, never create
    one — so the worst case is a missed correction, not a wrong one.
    """
    if not had_apostrophe:
        return False
    wl = word.lower().replace("’", "'").rstrip("'")
    # len>3 so the ordinary word "in" can never reach the dictionary lookup.
    if len(wl) < 4 or not wl.endswith("in"):
        return False
    try:
        # ⭐ The whole test: is this word, with its dropped g put back, a word?
        return not spell.unknown([wl + "g"])
    except Exception:
        return False


# ── Wrong-case proper nouns ──────────────────────────────────────────────────
# Tony, 2026-08-07: *"I just found a name that is in the list but it didn't
# repair it. Name is hirst and it's line #250."*
#
# ⚠️ ADDING A NAME TO THE LIST IS WHAT MAKES THE SPELL CHECKER BLIND TO IT.
# custom_cap_words is loaded into pyspellchecker lowercased, and pyspellchecker
# lowercases before lookup anyway, so once "Hirst" is a known name every one of
# "Hirst" / "hirst" / "HIRST" is a correctly spelled word. The scanner walks
# past all of them and reports "spell check complete". Measured on the episode
# he found it in: 30 wrong-case names across 6 distinct names, all silent.
#
# The gap is that "hirst" is not a SPELLING error at all — it is a CAPITALISATION
# error, and the only tool that looked at capitalisation was the bulk Fix ALL
# CAPS filter. A name added to the list AFTER a file was filtered therefore fell
# between the two tools permanently.
#
# Unlike a misspelling, this has exactly ONE right answer and it is already
# stored — so the correction is authoritative rather than a guess.
def name_case_lut(names):
    """{lowercase: canonical} for the single-token names in `names`.

    ⚠️ MULTI-WORD NAMES ARE DELIBERATELY EXCLUDED. Splitting "Van Gogh" into
    van->Van and gogh->Gogh looks like a free win, but the same rule applied to
    "New York" would make every ordinary "new" a wrong-case name. The Fix ALL
    CAPS filter matches phrases and handles those correctly; this stays out of
    the way rather than inventing a false positive the filter does not have.
    """
    return {n.lower(): n for n in names if n and ' ' not in n}


def miscased_name(word, lut):
    """The correctly-cased form of `word`, or None if there is nothing to fix.

    Handles possessives too ("hirst's" -> "Hirst's"), for the same reason
    is_ok_contraction exists: a name overwhelmingly appears in its possessive
    form, so a check that only matched the bare token would miss the occurrences
    that matter most.
    """
    if not word:
        return None
    # ALL CAPS is emphasis, signage or a hard-of-hearing tag — not something to
    # correct word by word. Leave it to the Fix ALL CAPS filter.
    if word.isupper():
        return None
    good = lut.get(word.lower())
    if good is not None:
        return good if good != word else None
    # Possessive / contraction: correct the root, keep the suffix verbatim.
    root, sep, suffix = word.replace('’', "'").rpartition("'")
    if not sep or not root or root.isupper():
        return None
    if suffix and suffix.lower() not in _CONTRACTION_SUFFIXES:
        return None
    good = lut.get(root.lower())
    if good is None or good == root:
        return None
    # Index into the ORIGINAL word so a curly apostrophe survives — the
    # normalisation above is 1 char for 1 char, so the offsets still line up.
    return good + word[len(root):]


# ── One SpellChecker, reused ─────────────────────────────────────────────────
# ⭐ MEASURED 2026-09-26, and it is the whole reason live highlighting is
# possible at all:
#
#     SpellChecker()  construction : ~145 ms   every single call
#     the scan loop, 814 cues      :  ~17 ms
#     the scan loop, 535 cues      :   ~7 ms
#
# The scan was only ever run from a menu item, so 145 ms nobody could feel. The
# moment it moved into refresh_tree — which fires on every delete, edit, undo
# and filter — that constructor became the cost of the feature. Cached, a
# rescan is ~15 ms and sits in the same league as scan_allcaps_words.
#
# ⭐⭐ KEYED ON THE WORD LIST, WHICH IS THE POINT. Tony, 2026-09-26: *"Once a
# word or name has been added to the dictionary or name list, it doesn't clear
# the remaining errors."* There are NINE separate doors that add a word (two
# dialog buttons, two right-click menus, the OCR pane, three preferences
# controls, the temp-names menu) and asking each of them to remember to call an
# invalidate function is how this bug happens again. Instead the cache key IS
# the known-word list: add a word anywhere and the key changes, so the next
# scan rebuilds and the error clears everywhere. Nothing to remember.
#
# ⛔ The INTERACTIVE dialog deliberately does NOT use this. It calls
# `spell.word_frequency.load_words([w])` directly on its own instance (see
# _do_add_dict) — a mutation the key knows nothing about, which would leave a
# cached checker claiming to hold a word list it no longer matches.
_CHECKER = {"key": None, "spell": None}


def checker_for(known):
    """A SpellChecker loaded with `known`, reused until `known` changes.

    ⚠️ The key is the SORTED, LOWERCASED, DEDUPED set — so re-ordering the
    preferences list, or adding a word that differs only in case, does not throw
    away a perfectly good checker and pay 145 ms for nothing.
    """
    from spellchecker import SpellChecker
    key = tuple(sorted({str(w).lower() for w in known if w}))
    if _CHECKER["spell"] is None or _CHECKER["key"] != key:
        spell = SpellChecker()
        if key:
            spell.word_frequency.load_words(list(key))
        _CHECKER["key"], _CHECKER["spell"] = key, spell
    return _CHECKER["spell"]


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
        words = WORD_RE.findall(clean)
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


def run_spell_highlight_scan(app, parent_window, cues, spell_error_indices,
                             name_fixes=None, quiet=False, extra_known=()):
    """Scan all cues for spelling errors (highlight-only, no candidates).

    Faster than run_spell_check_scan() because it skips candidate
    generation — only identifies which cues contain misspelled words.

    ⭐ THIS IS NOW CALLED FROM refresh_tree, not just from the menu. Tony,
    2026-09-26: *"if I delete a line, the remaining spelling errors don't move
    with the new line numbers."* They could not — the result is keyed by
    POSITION and ~16 operations renumber the cues under it (delete, split,
    join, insert, undo, redo, Reset to Original, filters, Replace All). The
    ALL CAPS highlighter two lines above in refresh_tree has always re-scanned
    on every rebuild for exactly this reason; the spell highlighter was the one
    that cached. So it stops caching, and the drift stops being possible rather
    than being fixed one caller at a time.

    Args:
        app: Application context with custom_cap_words, custom_spell_words.
        parent_window: Tk window for dialogs.
        cues: List of subtitle cue dicts.
        spell_error_indices: Set to populate with cue indices that have
                             errors. CLEARED first.
        name_fixes: Optional dict to fill with {cue_index: [(bad, good), ...]}
                    for wrong-case names. See the miscased block below.
        quiet: True to return None instead of offering to pip-install the
               spell checker. ⚠️ LOAD-BEARING for the refresh_tree caller: a
               modal that is fine once from a menu is unusable when it fires on
               every repaint of the tree.
        extra_known: Additional known words/names — the per-file "This File
                     Only" temp names.

    Returns:
        Dict of {cue_index: [word, ...]} listing misspelled words per cue,
        or None if spell checker is not available.
    """
    try:
        from spellchecker import SpellChecker            # noqa: F401
    except ImportError:
        if quiet:
            return None
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

    cap_words = list(getattr(app, 'custom_cap_words', []))
    spell_words = list(getattr(app, 'custom_spell_words', []))
    extra = [w for w in extra_known if w]
    known = [w.lower() for w in cap_words + spell_words + extra]
    spell = checker_for(known)
    # ⚠️ Names only — NOT custom_spell_words. A word in the plain dictionary is
    # an ordinary word ("beheves" -> "behaves"); only the name list carries the
    # claim that there is one correct capitalisation.
    lut = name_case_lut(cap_words + extra)

    spell_error_indices.clear()
    errors_by_cue = {}
    for i, cue in enumerate(cues):
        clean = re.sub(r'<[^>]+>|\{\\[^}]+\}|♪', '', cue['text'])
        pairs = list(iter_words(clean))
        if not pairs:
            continue
        words = [w for w, _ in pairs]
        unknown = spell.unknown(words)
        cue_words, cue_names = [], []
        for w, apos in pairs:
            if ((w.lower() in unknown or w in unknown)
                    # Skip valid contractions/possessives — see the note on
                    # is_ok_contraction. Without this, every "Whatever's" and
                    # every possessive of a known name is a false positive.
                    and not is_ok_contraction(w, spell, known)
                    # ...and dialect dropped-g forms, which are a class, not a
                    # list. See is_dropped_g — 114 distinct forms in 40 files.
                    and not is_dropped_g(w, apos, spell)):
                cue_words.append(w)
                continue
            # ⭐ WRONG-CASE NAMES, added 2026-09-26 at Tony's request: *"Yes I
            # want names to be lit as well."*
            # ⚠️⚠️ THIS MUST RUN ON *KNOWN* WORDS, which is why it sits after
            # the `continue` and not inside `if unknown`. Adding "Hirst" to the
            # name list is precisely what makes "hirst" a correctly-spelled
            # word — see the long note above name_case_lut. A caps error is
            # invisible to a spell checker by construction, so a scan that only
            # looked at unknown words could never find one.
            # ⚠️ The `continue` above stops a word being reported as BOTH a
            # misspelling and a caps error. It is defensive, not load-bearing:
            # a mutation test on 2026-09-26 removed it and every check still
            # passed, because a name in the lut is also loaded into the
            # dictionary, so it can never appear in `unknown`. Recorded so the
            # next reader neither deletes it as dead code nor trusts it as a
            # guarantee — it starts earning its keep the moment names stop
            # being loaded into the checker.
            good = miscased_name(w, lut)
            if good:
                cue_names.append((w, good))
        if cue_words or cue_names:
            spell_error_indices.add(i)
            if cue_words:
                errors_by_cue[i] = cue_words
            if cue_names and name_fixes is not None:
                name_fixes[i] = cue_names
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
