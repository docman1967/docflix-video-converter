#!/usr/bin/env python3
"""Regression test: names in the list must be caught when written in wrong case.

WHY THIS FILE EXISTS
────────────────────
Tony, 2026-08-07: *"I just found a name that is in the list but it didn't
repair it. Name is hirst and it's line #250."*

The cause is genuinely backwards from how it reads: **adding a name to the list
is what made the spell checker blind to it.** custom_cap_words is loaded into
pyspellchecker lowercased, and pyspellchecker lowercases before lookup anyway,
so "Hirst" being a known name makes "hirst" a correctly spelled word. The
scanner walked past 30 wrong-case names in that one episode and reported
"spell check complete!".

That is the failure mode that keeps showing up in this project: **the tool
produced a clean, confident, wrong result.** No error, no warning, no clue.

    python3 tests/test_name_case.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from modules.spell_checker import miscased_name, name_case_lut  # noqa: E402
from modules.subtitle_editor import replace_word                # noqa: E402

NAMES = ['Hirst', 'Rikers', 'Van', 'Kazahrusian', 'Van Gogh']
LUT = name_case_lut(NAMES)

# (word, expected correction or None, note)
CASES = [
    ("hirst", "Hirst", "the reported case"),
    ("Hirst", None, "already correct — must not flag"),
    ("HIRST", None, "ALL CAPS is emphasis; Fix ALL CAPS owns that"),
    ("hirst's", "Hirst's", "possessive — the form names usually appear in"),
    ("hirst’s", "Hirst’s", "curly apostrophe survives verbatim"),
    ("Hirst's", None, "correct possessive must not flag"),
    ("rikers", "Rikers", "a second name from the same episode"),
    ("kazahrusian", "Kazahrusian", "long name, still just a recase"),
    ("hello", None, "ordinary word, not a name"),
    ("hirstle", None, "longer word merely starting with the name"),
    ("o'brien", None, "not a name in the list at all"),
    # ⚠️ multi-word names are deliberately NOT decomposed — see name_case_lut.
    ("gogh", None, "half of a two-word name: the filter owns phrases"),
    ("van", "Van", "but 'Van' alone IS in the list on its own"),
]

# The recase has to survive a cue that ALREADY contains the correct form.
# Without exact=True the count=1 replace is spent on "Hirst" and "hirst" lives.
RECASE = [
    ("Hirst said hirst.", "hirst", "Hirst", 1, "Hirst said Hirst.",
     "correct form earlier in the SAME cue — the count=1 trap"),
    ("Director hirst.", "hirst", "Hirst", 1, "Director Hirst.",
     "the ordinary single fix"),
    ("hirst and hirst", "hirst", "Hirst", 0, "Hirst and Hirst",
     "replace-all recases every occurrence"),
    ("HIRST SHOUTED", "hirst", "Hirst", 0, "HIRST SHOUTED",
     "ALL CAPS untouched — exact match is case-sensitive"),
    ("Whirst hirst", "hirst", "Hirst", 0, "Whirst Hirst",
     "must not match inside a longer word"),
]


def main():
    fails = 0
    print("\n  detection")
    for word, want, note in CASES:
        got = miscased_name(word, LUT)
        ok = got == want
        fails += not ok
        print(f"    {'ok  ' if ok else 'FAIL'} {word:14} -> {got!r:16} ({note})")
        if not ok:
            print(f"           want {want!r}")

    print("\n  recasing (replace_word exact=True)")
    for txt, w, repl, count, want, note in RECASE:
        got = replace_word(txt, w, repl, count=count, exact=True)
        ok = got == want
        fails += not ok
        print(f"    {'ok  ' if ok else 'FAIL'} {note}")
        if not ok:
            print(f"           in   {txt!r}")
            print(f"           got  {got!r}")
            print(f"           want {want!r}")

    # ⚠️ A SUGGESTED NAME COMES BACK LOWERCASED. word_frequency is
    # lowercase-only, so offering a candidate verbatim would insert a wrong-case
    # name — the tool manufacturing the very defect it just learned to find.
    # The dialog maps candidates through the same lut before displaying them;
    # this pins the mapping, and pins that a TYPED word is left alone.
    print("\n  suggested names are shown the way they were written")
    SUGGEST = [
        ("kazahrusian", "Kazahrusian", "candidate from word_frequency"),
        ("hirst", "Hirst", "same, short name"),
        ("firsts", "firsts", "ordinary word, not a name — untouched"),
    ]
    for cand, want, note in SUGGEST:
        got = LUT.get(cand.lower(), cand)
        ok = got == want
        fails += not ok
        print(f"    {'ok  ' if ok else 'FAIL'} {cand:14} -> {got:14} ({note})")

    # ⚠️ The whole feature rests on this being true. If pyspellchecker ever
    # becomes case-aware, the caps pass turns into duplicate reporting rather
    # than the only reporting — and this is the line that would say so.
    print("\n  the premise: pyspellchecker cannot see case")
    try:
        from spellchecker import SpellChecker
        s = SpellChecker()
        s.word_frequency.load_words(['hirst'])
        blind = not s.unknown(['hirst']) and not s.unknown(['Hirst'])
        print(f"    {'ok  ' if blind else 'FAIL'} 'hirst' and 'Hirst' both "
              f"read as known -> the caps pass is load-bearing")
        fails += not blind
    except ImportError:
        print("    pyspellchecker not installed — skipping")

    total = len(CASES) + len(RECASE) + len(SUGGEST) + 1
    print(f"\n  {total - fails}/{total} pass\n")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
