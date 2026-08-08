#!/usr/bin/env python3
"""Regression test: a saved setting must come back the way it went in.

WHY THIS FILE EXISTS
────────────────────
Tony, 2026-08-08: *"the crf value isn't persistent. I changed the value to 32
then closed with the x in the top right. When I relaunched it 23 was the value.
In that line, that is the only setting that isn't persisting."*

CRF lives in TWO Tk variables:

    self.crf       StringVar  — what the ENCODER reads, and what prefs save
    self.crf_var   IntVar     — what the SLIDER and the ENTRY BOX both bind to

load_preferences() restored only `self.crf`. The slider stayed at its 23
default, and the next event that reached on_crf_change() read the SLIDER and
wrote it back over the loaded value. So a saved 32 became 23 on the next
launch — and then got SAVED as 23 on close, quietly destroying the setting for
good rather than just displaying it wrong.

⚠️ THE CODEBASE ALREADY KNEW ABOUT THIS CLASS OF BUG. Directly above the CRF
line in load_preferences:

    # Bitrate intentionally not saved/loaded — always starts at default (2.0M)
    # to avoid hidden mismatches between saved value and UI slider position

Someone hit it with bitrate and dodged it by not persisting bitrate at all.
CRF has the identical two-variable shape but IS persisted, so it walked
straight into the mismatch the comment describes.

WHAT THIS TEST ACTUALLY CHECKS
──────────────────────────────
Not "does load_preferences mention crf" — that was true while it was broken.
It checks the PAIRING: for every setting that has both a persisted variable and
a paired UI variable, load_preferences must restore BOTH. That is the property
that broke.

    python3 tests/test_prefs_roundtrip.py
"""

import ast
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, 'video_converter.py')


def load_preferences_body(src):
    """Source text of load_preferences(), so we assert on the real function
    rather than anywhere the string happens to appear."""
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == 'load_preferences':
            return ast.get_source_segment(src, node) or ''
    return ''


def main():
    src = open(SRC, encoding='utf-8').read()
    body = load_preferences_body(src)
    fails = 0

    if not body:
        print("\n  FAIL could not find load_preferences()\n")
        sys.exit(1)

    # (persisted var, paired UI var, note)
    PAIRS = [
        ('crf', 'crf_var',
         'CRF: slider + entry both bind to crf_var; restoring only crf '
         'let the next event overwrite it'),
    ]

    print("\n  settings with a paired UI variable restore BOTH")
    for var, ui_var, note in PAIRS:
        restores_value = re.search(rf"self\.{var}\.set\(", body) is not None
        restores_ui = re.search(rf"self\.{ui_var}\.set\(", body) is not None
        ok = restores_value and restores_ui
        fails += not ok
        print(f"    {'ok  ' if ok else 'FAIL'} {var:<6} value={restores_value} "
              f"ui({ui_var})={restores_ui}")
        if not ok:
            print(f"           {note}")

    # ⚠️ A pairing that exists in the widgets but not in this test's list is a
    # gap the test cannot see. Surface any OTHER `self.X` / `self.X_var` pair so
    # nobody has to remember to come back here.
    print("\n  any other value/UI variable pairs in the app?")
    declared = set(re.findall(r"self\.([a-z_]+)\s*=\s*tk\.\w+Var\(", src))
    pairs = sorted(v for v in declared
                   if f"{v}_var" in declared or (v.endswith('_var')
                                                 and v[:-4] in declared))
    known = {p for pair in PAIRS for p in pair[:2]}
    extra = [p for p in pairs if p not in known]
    if extra:
        print(f"    note  also paired, NOT covered here: {', '.join(extra)}")
        print("          check each is either restored in both halves, or")
        print("          deliberately not persisted (as bitrate is)")
    else:
        print("    ok    no uncovered pairs")

    # The bitrate comment is load-bearing documentation for exactly this trap.
    print("\n  the bitrate warning comment is still present")
    has = 'Bitrate intentionally not saved/loaded' in body
    fails += not has
    print(f"    {'ok  ' if has else 'FAIL'} it explains why this class of bug "
          f"exists — do not delete it")

    total = len(PAIRS) + 1
    print(f"\n  {total - fails}/{total} pass\n")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
