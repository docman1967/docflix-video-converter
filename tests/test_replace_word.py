#!/usr/bin/env python3
"""Regression test for the spell checker's whole-word replacement.

WHY THIS FILE EXISTS
────────────────────
Spell Check's Replace and Replace All used str.find() / str.replace() — plain
substring matching — until 2026-08-07. The scanner only ever reports whole
words, so the trap is a real word that also sits INSIDE a longer word earlier
in the same line. Names do this constantly:

    "Anastasia met Ana."   fixing Ana->Anna   gave  "Annastasia met Ana."
    "Vanya, this is Van."  fixing Van->Vance  gave  "Vanceya, this is Van."

Both failures at once: a word that was never flagged got mangled, and the
actual error was left alone. Replace All was worse — str.replace() across every
cue in the file.

⚠️ THIS IS NOT A UNIT TEST OF A HELPER. It is the list of inputs that used to
produce corrupted subtitles. Adding a case here is cheaper than finding out
from a mangled episode months later.

    python3 tests/test_replace_word.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from modules.subtitle_editor import replace_word   # noqa: E402


# (cue text, misspelled word, replacement, expected, note)
CASES = [
    # ── the corruption that prompted this ────────────────────────────────
    ("Anastasia met Ana.", "Ana", "Anna", "Anastasia met Anna.",
     "word inside a LONGER word earlier in the line"),
    ("Vanya, this is Van.", "Van", "Vance", "Vanya, this is Vance.",
     "Tony's actual subtitle — Vanya must survive"),
    ("The Kingsman kings arrive.", "kings", "king's",
     "The Kingsman king's arrive.", "prefix of a longer word"),

    # ── case is carried onto the replacement ─────────────────────────────
    ("Teh start. teh end.", "teh", "the", "The start. the end.",
     "sentence-initial keeps its capital"),
    ("I wont. He WONT.", "wont", "won't", "I won't. He WON'T.",
     "ALL CAPS stays ALL CAPS"),

    # ── apostrophes are word characters ──────────────────────────────────
    ("dont stop", "dont", "don't", "don't stop",
     "replacement may contain an apostrophe"),
    ("I don't. I dont.", "dont", "don't", "I don't. I don't.",
     "must not half-match inside an existing don't"),

    # ── ordinary behaviour ───────────────────────────────────────────────
    ("teh cat and teh dog", "teh", "the", "the cat and the dog",
     "every occurrence when count=0"),
    ("nothing to do here", "absent", "x", "nothing to do here",
     "no match leaves the text alone"),
]

SINGLE = [
    ("teh cat and teh dog", "teh", "the", "the cat and teh dog",
     "count=1 fixes only the first — the Replace button"),
]


def main():
    fails = 0
    print("\n  whole-word replace (Replace All / count=0)")
    for txt, w, repl, want, note in CASES:
        got = replace_word(txt, w, repl)
        ok = got == want
        fails += not ok
        print(f"    {'ok  ' if ok else 'FAIL'} {note}")
        if not ok:
            print(f"           in   {txt!r}  {w}->{repl}")
            print(f"           got  {got!r}")
            print(f"           want {want!r}")

    print("\n  single replace (Replace button / count=1)")
    for txt, w, repl, want, note in SINGLE:
        got = replace_word(txt, w, repl, count=1)
        ok = got == want
        fails += not ok
        print(f"    {'ok  ' if ok else 'FAIL'} {note}")
        if not ok:
            print(f"           got  {got!r}")
            print(f"           want {want!r}")

    total = len(CASES) + len(SINGLE)
    print(f"\n  {total - fails}/{total} pass\n")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
