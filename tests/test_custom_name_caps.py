#!/usr/bin/env python3
"""Regression test: Fix ALL CAPS must capitalise custom names in EVERY form.

WHY THIS FILE EXISTS
────────────────────
Two bugs, on the same line, on the same day, in opposite directions — which is
the whole reason this file has to assert both at once.

  MORNING  the word pattern was r'\\b[a-zA-Z]+\\b', so "o'brien" split into "o"
           and "brien" and no custom name containing an apostrophe was ever
           applied. O'Brien, O'Neill, D'Angelo silently did nothing.
           Fixed by putting the apostrophe in the pattern.

  AFTERNOON that fix made "sho's" a SINGLE token, and _cap_custom only did a
           whole-token lookup — "sho's" is not in the list, "Sho" is. Before the
           morning fix, "sho's" split into "sho" + "s" and the name half matched
           BY ACCIDENT. Removing the accident left every possessive of every
           custom name unfixed: 64 across 26 episodes of Tony's library, which
           only surfaced because the spell checker (miscased_name) catches them
           and Fix ALL CAPS did not.

⚠️ SO: FIXING EITHER ONE ALONE RE-BREAKS THE OTHER. The O'Brien cases and the
possessive cases below must pass together or the change is not done. That is
also why the two features share the rule — if Fix ALL CAPS and Spell Check
disagree about the same word, one of them is lying to Tony.

    python3 tests/test_custom_name_caps.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from modules.subtitle_filters import filter_fix_caps       # noqa: E402
from modules.spell_checker import miscased_name, name_case_lut  # noqa: E402

NAMES = ["Sho", "Zann", "Remi", "Hirst", "O'Brien", "Weitz", "Van Gogh"]

# (input cue text, expected output, note)
CASES = [
    # ── the afternoon bug: possessives ───────────────────────────────────
    ("Where are we on sho's laptop?", "Where are we on Sho's laptop?",
     "possessive of a custom name — the 64-occurrence bug"),
    ("Finding it from\nsho's computer.", "Finding it from\nSho's computer.",
     "possessive across a line break"),
    ("zann's office and remi's car.", "Zann's office and Remi's car.",
     "two possessives in one cue"),
    ("that's weitz's problem.", "That's Weitz's problem.",
     "a real contraction next to a name possessive"),

    # ── the morning bug: apostrophes inside the name itself ──────────────
    ("ask o'brien about it.", "Ask O'Brien about it.",
     "name CONTAINING an apostrophe — must not split into o + brien"),
    ("o'brien's badge.", "O'Brien's badge.",
     "possessive OF an apostrophe name — both bugs at once"),

    # ── plain names still work ───────────────────────────────────────────
    ("sho and zann arrived.", "Sho and Zann arrived.",
     "ordinary bare names"),
    ("Sho's laptop.", "Sho's laptop.",
     "already correct — must be left alone"),

    # ── ⚠️ MUST NOT TOUCH: ordinary words that look like the pattern ─────
    # Kept out of position 0. Fix ALL CAPS capitalises the first word of a cue
    # by design, so a sensitive word at the start tests the sentence-caser
    # rather than the name lookup — the first draft of this file got that wrong
    # and reported four failures against perfectly correct code. The leading
    # capital in each expectation below IS the filter working.
    ("please don't stop, it's fine.", "Please don't stop, it's fine.",
     "contractions whose root is not a name"),
    ("in the boys' room.", "In the boys' room.",
     "plural possessive of an ordinary word"),
    ("well she's a doctor.", "Well she's a doctor.",
     "'s on a common word"),
    ("my shoes and shoving.", "My shoes and shoving.",
     "words merely STARTING with a name must not match"),
    ("we saw shopping and shone.", "We saw shopping and shone.",
     "more Sho- prefixes that must survive"),
]


def main():
    fails = 0
    print("\n  Fix ALL CAPS — custom names in every form")
    for text, want, note in CASES:
        cue = {'text': text, 'start': '00:00:00,000', 'end': '00:00:01,000'}
        got = filter_fix_caps([cue], NAMES)[0]['text']
        ok = got == want
        fails += not ok
        print(f"    {'ok  ' if ok else 'FAIL'} {note}")
        if not ok:
            print(f"           in   {text!r}")
            print(f"           got  {got!r}")
            print(f"           want {want!r}")

    # ⚠️ The two features must agree. Spell Check flags a wrong-case name and
    # Fix ALL CAPS repairs it; if they disagree about the same word, one of them
    # is telling Tony something false about his file.
    print("\n  Fix ALL CAPS agrees with Spell Check (miscased_name)")
    lut = name_case_lut(NAMES)
    PROBES = ["sho's", "zann's", "o'brien's", "sho", "don't", "boys'",
              "she's", "shoes"]
    for word in PROBES:
        spell_says = miscased_name(word, lut)          # None = nothing to fix
        # "we saw " prefix keeps the probe out of sentence-initial position,
        # where the filter would capitalise it for an unrelated reason.
        text = f"we saw {word} there"
        got = filter_fix_caps(
            [{'text': text, 'start': '00:00:00,000', 'end': '00:00:01,000'}],
            NAMES)[0]['text']
        filter_fixed = got[len("we saw "):-len(" there")]
        filter_fixed = filter_fixed if filter_fixed != word else None
        ok = spell_says == filter_fixed
        fails += not ok
        print(f"    {'ok  ' if ok else 'FAIL'} {word:12} "
              f"spell={str(spell_says):12} filter={str(filter_fixed)}")

    total = len(CASES) + len(PROBES)
    print(f"\n  {total - fails}/{total} pass\n")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
