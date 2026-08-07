#!/usr/bin/env python3
"""Regression test: contractions and possessives must not be false positives.

WHY THIS FILE EXISTS
────────────────────
Tony, 2026-08-07, with a screenshot of Spell Check stopped on "Whatever's",
offering 'whatever' and 'whitener's': *"Most spell checkers will flag things
with an apostrophe... We should have a way to determine whether it's a
contraction and spell check the root and not the whole word. This happens with
names as well."*

pyspellchecker only knows whole tokens. The names half is the part that really
bites: adding "Vanya" to the custom dictionary does NOTHING for "Vanya's", so
the dictionary silently fails to cover the form the name usually appears in,
and the same false positive comes back every episode forever.

⚠️ THE SUPPRESSION MUST BE ONE-WAY. is_ok_contraction can only ever hide a
flag, never raise one — so the negative cases below ("Xyzzy's", "teh") matter
more than the positive ones. If those ever start passing, the checker has gone
blind to real errors.

    python3 tests/test_contractions.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from modules.spell_checker import contraction_roots, is_ok_contraction  # noqa

# (word, expected roots, note)
ROOTS = [
    ("Whatever's", ["Whatever"], "the reported case"),
    ("Vanya's", ["Vanya"], "possessive of a custom name"),
    ("boys'", ["boys"], "plural possessive, trailing apostrophe"),
    ("they're", ["they"], "'re"),
    ("I've", ["I"], "'ve"),
    ("we'll", ["we"], "'ll"),
    ("I'm", ["I"], "'m"),
    ("doesn't", ["does", "doesn"], "n't strips 3"),
    ("can't", ["ca", "can"], "n't is AMBIGUOUS — can = can + 't"),
    ("won't", ["will"], "irregular, no letter-root"),
    ("Whatever’s", ["Whatever"], "curly apostrophe normalises"),
    ("O'Brien", [], "not a contraction — 'Brien is not a suffix"),
    ("hello", [], "no apostrophe at all"),
]


def main():
    try:
        from spellchecker import SpellChecker
    except ImportError:
        print("  pyspellchecker not installed — skipping")
        sys.exit(0)

    fails = 0
    print("\n  root extraction")
    for word, want, note in ROOTS:
        got = contraction_roots(word)
        ok = got == want
        fails += not ok
        print(f"    {'ok  ' if ok else 'FAIL'} {word:14} -> {got}  ({note})")
        if not ok:
            print(f"           want {want}")

    spell = SpellChecker()
    custom = ['vanya', 'kazahrusian']
    spell.word_frequency.load_words(custom)

    # (word, should_still_be_flagged, note)
    LIVE = [
        ("Whatever's", False, "valid contraction — must NOT flag"),
        ("boys'", False, "plural possessive — must NOT flag"),
        ("Vanya's", False, "root is a known custom name — must NOT flag"),
        ("Kazahrusian's", False, "root is a known custom name"),
        ("Whatever’s", False, "curly apostrophe behaves the same"),
        # ⚠️ the ones that must SURVIVE — suppression is one-way
        ("Xyzzy's", True, "root unknown — must STILL flag"),
        ("teh", True, "plain typo, no apostrophe — must STILL flag"),
    ]
    print("\n  live behaviour against the real dictionary")
    for word, want_flag, note in LIVE:
        flagged = bool(spell.unknown([word]))
        still = flagged and not is_ok_contraction(word, spell, custom)
        ok = still == want_flag
        fails += not ok
        print(f"    {'ok  ' if ok else 'FAIL'} {word:14} "
              f"flagged={still!s:5} ({note})")

    total = len(ROOTS) + len(LIVE)
    print(f"\n  {total - fails}/{total} pass\n")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
