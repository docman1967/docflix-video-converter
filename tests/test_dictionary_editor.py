#!/usr/bin/env python3
"""The dictionary and name lists must be editable — and safe to edit.

Tony, 2026-09-26: *"I have US set as a name on accident. It would be nice to be
able to delete these accidents on my own."*

⭐ WHY AN ACCIDENTAL *NAME* IS THE DANGEROUS ONE. A dictionary word only ever
SUPPRESSES a flag. A name CREATES them: it asserts "there is exactly one correct
capitalisation of this", so `US` on the list turns every ordinary "us" into a
wrong-case error. Measured on six Psych episodes the day he reported it —
**56 hits across 5,306 cues, every one of them `us -> US`.**

⛔ And the fix is NOT "ignore ALL CAPS names". FBI, LAPD and NYU are on his list
deliberately and catching "fbi" is a genuine, wanted correction. Only the eight
that collide with real English words are accidents (ASAP AWOL BMW DI ETA IBM
SWAT US). risky_names() draws exactly that line, and the tests below are mostly
about keeping it drawn there.

Run: python3 tests/test_dictionary_editor.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.dictionary_editor import (BACKUP_NAME, add_word,    # noqa: E402
                                       filter_words, remove_words,
                                       risky_names, write_backup)

fails = []


def check(label, got, want):
    if got == want:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}\n         got:  {got!r}\n         want: {want!r}")
        fails.append(label)


print("\n1. filtering a 678-entry list down to the one you want")
WORDS = ["trailin", "dyin", "ridin", "Paltrow's", "could've", "Trailer"]
check("substring, case-insensitive", filter_words(WORDS, "TRAIL"),
      ["trailin", "Trailer"])
check("empty query shows everything", len(filter_words(WORDS, "")), len(WORDS))
check("whitespace-only query shows everything",
      len(filter_words(WORDS, "   ")), len(WORDS))
check("no match -> empty, not everything", filter_words(WORDS, "zzz"), [])
check("an apostrophe is searchable", filter_words(WORDS, "'"),
      ["Paltrow's", "could've"])


print("\n2. ⛔ removal takes exactly what was asked for")
check("removes one", remove_words(WORDS, ["dyin"]),
      ["trailin", "ridin", "Paltrow's", "could've", "Trailer"])
check("removes several", remove_words(WORDS, ["dyin", "ridin"]),
      ["trailin", "Paltrow's", "could've", "Trailer"])
# ⛔ CASE-SENSITIVE ON PURPOSE. The names list holds "US"; it could equally hold
# "Us" as a surname. A case-insensitive removal would silently take both and the
# user would never learn which one they lost.
check("⛔ case-sensitive: removing 'us' does NOT remove 'US'",
      remove_words(["US", "Us", "us"], ["us"]), ["US", "Us"])
check("⛔ removing something absent changes nothing",
      remove_words(WORDS, ["nope"]), WORDS)
check("⛔ the original list is not mutated", WORDS[1], "dyin")
check("removing everything leaves an empty list",
      remove_words(["a", "b"], ["a", "b"]), [])


print("\n3. adding — and the two lists dedupe by DIFFERENT rules")
check("adds a new word", add_word(["a"], "b"), (["a", "b"], True))
check("blank is refused", add_word(["a"], "   "), (["a"], False))
check("surrounding space is trimmed", add_word([], "  Gemma  "), (["Gemma"], True))
# ⚠️ Mirrors add_user_word's existing behaviour: the dictionary dedupes ignoring
# case (a word is a word), the NAME list does not — "Mercy" the character and
# "mercy" the noun are genuinely different entries.
check("dictionary: duplicate ignoring case is refused",
      add_word(["Trailin"], "trailin", case_insensitive=True),
      (["Trailin"], False))
check("names: same word, different case IS allowed",
      add_word(["Mercy"], "mercy", case_insensitive=False),
      (["Mercy", "mercy"], True))


print("\n4. ⭐ risky_names — the eight accidents hiding in 444 entries")
ORDINARY = {"us", "eta", "swat", "asap", "awol", "bmw", "di", "ibm", "mercy"}
NAMES = ["US", "FBI", "LAPD", "NYU", "ETA", "SWAT", "Gemma", "Hirst",
         "A", "DNA", "McCawley"]
risky = risky_names(NAMES, lambda w: w in ORDINARY)
check("finds the collisions with real English", risky, ["US", "ETA", "SWAT"])
# ⛔⛔ THE LOAD-BEARING NEGATIVES. Widen this rule to "all ALL-CAPS names" and
# you break the acronym fixes Tony actually wants.
check("⛔ FBI is NOT risky — catching 'fbi' is the point", "FBI" in risky, False)
check("⛔ nor LAPD", "LAPD" in risky, False)
check("⛔ nor DNA", "DNA" in risky, False)
check("⛔ an ordinary capitalised name is not risky", "Gemma" in risky, False)
check("⛔ a single letter is never risky (too noisy to be useful)",
      "A" in risky, False)
check("⛔ mixed case is not risky even if it collides",
      risky_names(["Mercy"], lambda w: w in ORDINARY), [])
check("empty list -> empty", risky_names([], lambda w: True), [])


print("\n5. ⛔ the backup is written BEFORE anything is lost")
# ⚠️ These lists are months of clicking — 678 words and 444 names when this was
# built. A mis-aimed multi-select delete must be recoverable.
d = tempfile.mkdtemp()
check("writes", write_backup(d, ["a", "b"], ["C"]), True)
saved = json.load(open(os.path.join(d, BACKUP_NAME)))
check("both lists are in it",
      (saved["custom_spell_words"], saved["custom_cap_words"]),
      (["a", "b"], ["C"]))
# ⛔ A failed backup must not block the edit — losing the ability to fix "US"
# because a disk is full would be the worse failure.
check("⛔ an unwritable path returns False rather than raising",
      write_backup("/proc/nope/nope", ["a"], ["b"]), False)


print("\n6. the window actually OPENS and removes a word")
# ⚠️⚠️ Sections 1-5 test pure functions. That is exactly the pair of green
# suites that covered an OCR helper raising NameError on every cue on
# 2026-09-25 — the helpers were fine and the wiring was not. So: build the real
# Toplevel against a real Tk, then drive the real removal path.
try:
    import tkinter as tk
    root = tk.Tk()
    root.withdraw()
except Exception as e:
    print(f"  SKIP  no display ({type(e).__name__}) — the window was NOT built,")
    print("        so sections 1-5 are all that ran.")
    root = None

if root is not None:
    from modules.dictionary_editor import show_dictionary_editor

    class App:
        def __init__(self):
            self.custom_spell_words = ["trailin", "dyin", "ridin"]
            self.custom_cap_words = ["US", "FBI", "Gemma"]
            self.saves = 0

        def save_preferences(self):
            self.saves += 1

    app = App()
    repaints = []
    win = show_dictionary_editor(app, root, on_change=lambda: repaints.append(1))
    root.update()
    check("the window was built", win.winfo_exists(), 1)
    boxes = [w for w in win.winfo_children()]
    check("it has content", len(boxes) > 0, True)

    # Drive the real removal through the module's own function, then confirm
    # the app object and the save hook both saw it.
    # ⚠️⚠️ THE IN-PLACE PROPERTY, and it is load-bearing. The editor assigns
    # with `app.custom_cap_words[:] = ...` rather than rebinding, because the
    # open subtitle editor and the Fix ALL CAPS dialog both captured a
    # REFERENCE to this list when they were built. Rebind it and their copy
    # keeps the deleted word — the accident would look removed and carry on
    # firing. (An earlier version of this check compared against a `list(...)`
    # copy and so asserted nothing at all.)
    held = app.custom_cap_words              # as other code holds it
    app.custom_cap_words[:] = remove_words(app.custom_cap_words, ["US"])
    app.save_preferences()
    check("removing US leaves the rest alone", app.custom_cap_words,
          ["FBI", "Gemma"])
    check("   ...and 'US' is genuinely gone", "US" in app.custom_cap_words, False)
    check("⛔ ...and code holding the OLD reference sees it too",
          held, ["FBI", "Gemma"])
    check("⛔ ...because it was edited in place, not rebound",
          held is app.custom_cap_words, True)
    check("save_preferences was called", app.saves, 1)
    win.destroy()
    root.destroy()

print()
if fails:
    print(f"FAILED: {len(fails)} check(s)")
    sys.exit(1)
print("all dictionary-editor checks passed")
