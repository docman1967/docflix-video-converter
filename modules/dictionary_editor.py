"""
Docflix Media Suite — Dictionary & Names editor

Tony, 2026-09-26: *"I wonder if we can add a way to edit the Dictionary and the
Name list. I have US set as a name on accident. It would be nice to be able to
delete these accidents on my own."*

Both lists were **write-only**. Every "Add to Dict" / "Add as Name" button in
the suite appends here and nothing anywhere could take one back out — except a
444-row listbox buried inside the *Fix ALL CAPS* dialog, under a heading about
converting capitals to sentence case. A mistake made in one click took a text
editor and a restart to undo.

⚠️ Why an accidental NAME is worse than an accidental dictionary word, and why
this window exists at all: a name carries the claim *"there is exactly one
correct capitalisation of this"*. With `US` on the list, every ordinary "us"
becomes a wrong-case name. Measured on six Psych episodes the day he reported
it: **56 hits across 5,306 cues, every single one `us -> US`.** A dictionary
word only ever suppresses a flag; a name CREATES them.

⭐ So the Names pane marks the entries that can misfire — an ALL-CAPS entry
that is also an ordinary lowercase English word. There are eight in Tony's
list (ASAP AWOL BMW DI ETA IBM SWAT US) and they are exactly the accidents he
opened this window to find. Turning "scroll 444 rows and hope" into "here are
the eight causing you trouble" is the point of the feature, not decoration.

⚠️ The pure helpers below are deliberately free of Tk so they can be tested;
see tests/test_dictionary_editor.py. The window is a thin shell over them.
"""
import json
import os
import tkinter as tk
from tkinter import messagebox, ttk

# ⚠️ One rollback file, written immediately BEFORE the first destructive save of
# a session. These lists represent months of Tony's clicking — 678 dictionary
# words and 444 names when this was built — and a mis-aimed multi-select delete
# would otherwise be unrecoverable. Cheap insurance; invisible until needed.
BACKUP_NAME = "dictionary_backup.json"


def filter_words(words, query):
    """Entries containing `query`, case-insensitively. Empty query → all."""
    q = (query or "").strip().lower()
    if not q:
        return list(words)
    return [w for w in words if q in str(w).lower()]


def remove_words(words, doomed):
    """`words` minus `doomed`, matched EXACTLY (case-sensitive).

    ⚠️ Case-sensitive on purpose. The names list holds "US" and could equally
    hold "Us" as a surname; a case-insensitive removal would take both and the
    user would never know which one they lost.
    """
    kill = set(doomed)
    return [w for w in words if w not in kill]


def add_word(words, word, case_insensitive=True):
    """Append `word` if absent. Returns (new list, added?).

    `case_insensitive` matches add_user_word's existing rule: the dictionary
    dedupes ignoring case, the NAME list does not — "Mercy" and "mercy" are
    meaningfully different there.
    """
    word = (word or "").strip()
    if not word:
        return list(words), False
    if case_insensitive:
        if word.lower() in {str(w).lower() for w in words}:
            return list(words), False
    elif word in words:
        return list(words), False
    return list(words) + [word], True


def risky_names(names, is_ordinary_word):
    """Names that will misfire: ALL-CAPS entries that are also normal words.

    ⭐ `US` is the whole reason this exists. An ALL-CAPS name lowercases to an
    ordinary word, so every innocent use of that word reads as a wrong-case
    name. ⛔ Deliberately NOT every ALL-CAPS entry — FBI, LAPD and NYU are on
    the list on purpose and catching "fbi" is a genuine, wanted fix. Only the
    collisions with real English are accidents.

    `is_ordinary_word(w)` is injected so this stays testable without loading a
    spell checker.
    """
    out = []
    for n in names:
        s = str(n)
        if s.isupper() and len(s) > 1 and is_ordinary_word(s.lower()):
            out.append(s)
    return out


def write_backup(prefs_dir, spell_words, cap_words):
    """Snapshot both lists before a destructive change. Best effort, never raises."""
    try:
        os.makedirs(prefs_dir, exist_ok=True)
        with open(os.path.join(prefs_dir, BACKUP_NAME), "w") as fh:
            json.dump({"custom_spell_words": list(spell_words),
                       "custom_cap_words": list(cap_words)}, fh, indent=1)
        return True
    except OSError:
        return False        # a failed backup must not block the edit


def _ordinary_word_checker():
    """A `word -> bool` predicate using pyspellchecker, or None if unavailable.

    ⚠️⚠️ ITS OWN INSTANCE, NOT spell_checker.checker_for(). Two reasons, and the
    second one cost a 145 ms stall before it was caught:
      1. It must consult the PLAIN dictionary. Asking a checker loaded with
         Tony's custom words whether "us" is ordinary English would answer yes
         because *he added it* — the accident would certify itself.
      2. checker_for() is a single-slot cache keyed on the word list. Asking it
         for the empty list would evict the editor's real checker, and the next
         refresh_tree would rebuild it — 145 ms, on every repaint, forever.
    One construction per window open is the right trade.
    """
    try:
        from spellchecker import SpellChecker
    except ImportError:
        return None
    spell = SpellChecker()
    return lambda w: not spell.unknown([w])


def is_acronym(word):
    """True for an ALL-CAPS entry of two or more letters: LAPD, DEO, U.S.

    ⭐ Tony, 2026-10-12: *"I still need access to edit the acronyms."* They were
    always here, in the Names list — "is correct, stop flagging it" on an
    ALL-CAPS word saves it there — but mixed in with 400-odd names, so a wrong
    one was effectively unfindable. The window now shows them as their own
    pane. ⚠️ Same STORAGE (custom_cap_words), split VIEW; nothing moves.
    """
    letters = [c for c in str(word) if c.isalpha()]
    return len(letters) >= 2 and all(c.isupper() for c in letters)


def split_names(cap_words):
    """(names, acronyms) from the one stored list, order preserved."""
    names = [w for w in cap_words if not is_acronym(w)]
    acronyms = [w for w in cap_words if is_acronym(w)]
    return names, acronyms


def show_dictionary_editor(app, parent, on_change=None):
    """The Dictionary, Names & Acronyms window. `on_change` re-paints the editor."""
    win = tk.Toplevel(parent)
    win.title("Dictionary, Names & Acronyms")
    win.geometry("1020x560")
    win.resizable(True, True)
    try:
        app._center_on_main(win)
    except Exception:
        pass

    prefs_dir = os.path.expanduser("~/.local/share/docflix")
    backed_up = [False]
    ordinary = _ordinary_word_checker()

    ttk.Label(win, padding=(10, 8), justify='center',
              text="Everything “Add to Dict”, “Add as Name” and “stop flagging "
                   "it” has ever saved.\n"
                   "Select one or more entries and press Remove.").pack()

    panes = ttk.Frame(win, padding=(8, 0))
    panes.pack(fill='both', expand=True)
    for col in range(3):
        panes.columnconfigure(col, weight=1, uniform='pane')
    panes.rowconfigure(0, weight=1)

    def _save(why):
        # ⚠️ Back up ONCE per session, before the first change — not after.
        if not backed_up[0]:
            write_backup(prefs_dir, app.custom_spell_words, app.custom_cap_words)
            backed_up[0] = True
        try:
            app.save_preferences()
        except Exception as e:
            messagebox.showerror("Could not save", f"{why} failed:\n{e}",
                                 parent=win)
            return False
        if on_change:
            try:
                on_change()
            except Exception:
                pass          # a repaint failure must not lose the edit
        return True

    def build(col, title, getter, remove, add, risky_pane, clean=None):
        frame = ttk.LabelFrame(panes, text=title, padding=8)
        frame.grid(row=0, column=col, sticky='nsew',
                   padx=(0 if col == 0 else 4, 0 if col == 2 else 4))
        count = ttk.Label(frame, text="")
        count.pack(anchor='w')

        # ⭐ Filter box a third of the old width (Tony, 2026-10-12) — three
        # panes have to fit across, and a filter is only ever a few letters.
        bar = ttk.Frame(frame)
        bar.pack(fill='x', pady=(4, 4))
        ttk.Label(bar, text="Filter:").pack(side='left')
        fvar = tk.StringVar()
        ttk.Entry(bar, textvariable=fvar, width=10).pack(side='left',
                                                           padx=(4, 0))

        only_risky = tk.BooleanVar(value=False)
        if risky_pane:
            ttk.Checkbutton(frame, variable=only_risky,
                            text="⚠ only ones that are also real words",
                            command=lambda: repaint()).pack(anchor='w')

        addbar = ttk.Frame(frame)
        addbar.pack(side='bottom', fill='x', pady=(6, 0))

        lb = tk.Listbox(frame, selectmode='extended', font=('Courier', 10),
                        activestyle='none')
        sb = ttk.Scrollbar(frame, orient='vertical', command=lb.yview)
        lb.configure(yscrollcommand=sb.set)
        lb.pack(side='left', fill='both', expand=True)
        sb.pack(side='left', fill='y')

        shown = []

        def repaint(*_):
            words = getter()
            risky = (set(risky_names(words, ordinary))
                     if (ordinary and risky_pane) else set())
            pool = [w for w in words if w in risky] if (
                risky_pane and only_risky.get()) else words
            shown[:] = filter_words(pool, fvar.get())
            lb.delete(0, 'end')
            for w in shown:
                lb.insert('end', f"⚠  {w}" if w in risky else f"   {w}")
            note = f"  ({len(risky)} can misfire)" if risky else ""
            count.configure(
                text=f"{len(words)} saved · showing {len(shown)}{note}")

        fvar.trace_add('write', repaint)

        def do_remove():
            picked = [shown[i] for i in lb.curselection()]
            if not picked:
                return
            preview = ", ".join(picked[:6]) + ("…" if len(picked) > 6 else "")
            if not messagebox.askyesno(
                    "Remove", f"Remove {len(picked)} entr"
                              f"{'y' if len(picked) == 1 else 'ies'}?\n\n{preview}",
                    parent=win):
                return
            remove(picked)
            if _save("Remove"):
                repaint_all()

        def do_add():
            word = (clean or str.strip)(addvar.get())
            if not word or not add(word):
                addvar.set("")
                return
            if _save("Add"):
                addvar.set("")
                repaint_all()

        addvar = tk.StringVar()
        addent = ttk.Entry(addbar, textvariable=addvar)
        addent.pack(side='left', fill='x', expand=True)
        addent.bind('<Return>', lambda e: do_add())
        ttk.Button(addbar, text="Add", command=do_add, width=6).pack(
            side='left', padx=(4, 0))
        ttk.Button(addbar, text="Remove", command=do_remove,
                   width=8).pack(side='left', padx=(4, 0))
        repaint()
        return repaint

    # ⚠️ Every list is changed IN PLACE — other code holds references to them.
    def remove_spell(picked):
        app.custom_spell_words[:] = remove_words(app.custom_spell_words, picked)

    def add_spell(word):
        new, ok = add_word(app.custom_spell_words, word, True)
        app.custom_spell_words[:] = new
        return ok

    def remove_caps(picked):
        app.custom_cap_words[:] = remove_words(app.custom_cap_words, picked)

    def remove_acronyms(picked):
        # ⭐ An acronym taught from the OCR pane went into BOTH lists (see
        # add_user_word). Removing it only from Names would leave it silently
        # known to the spell checker, so take the exact-case copy out of the
        # Dictionary too. Exact case: removing "US" must never take "us".
        remove_caps(picked)
        remove_spell(picked)

    def add_caps(word):
        new, ok = add_word(app.custom_cap_words, word, False)
        app.custom_cap_words[:] = new
        return ok

    def add_name(word):
        if is_acronym(word):
            messagebox.showinfo(
                "That's an acronym",
                f"“{word}” is all capitals — add it in the Acronyms pane.",
                parent=win)
            return False
        return add_caps(word)

    repaints = [
        build(0, "Dictionary  (words to stop flagging)",
              lambda: app.custom_spell_words, remove_spell, add_spell, False),
        build(1, "Names  (protected capitalisation)",
              lambda: split_names(app.custom_cap_words)[0], remove_caps,
              add_name, False),
        build(2, "Acronyms  (always ALL CAPS)",
              lambda: split_names(app.custom_cap_words)[1], remove_acronyms,
              add_caps, True, clean=lambda w: w.strip().upper()),
    ]

    def repaint_all():
        for r in repaints:
            r()

    ttk.Label(win, padding=(10, 6), foreground='#666',
              text="A backup of all three lists is written to "
                   f"{BACKUP_NAME} before your first change.").pack()
    ttk.Button(win, text="Close", command=win.destroy).pack(pady=(0, 8))
    return win
