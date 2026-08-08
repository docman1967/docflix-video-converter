#!/usr/bin/env python3
"""Regression test: no module may USE a name it never BINDS.

WHY THIS FILE EXISTS
────────────────────
2026-08-07, from a crash log Tony hit:

    subtitle_editor.py:1752, in do_save_file
        all_streams = get_all_streams(video_path)
    NameError: name 'get_all_streams' is not defined

The function existed in modules/utils.py. It was simply never added to the
import list. So "save an edited subtitle back into the video" had NEVER worked —
on that one branch. Editing external .srt files, which is the common case, was
unaffected, so nothing looked wrong.

Chasing that one turned up FIVE MORE of the same shape, and one was serious:

    subtitle_editor  retime_subtitles   <- THE CORE OF SMART SYNC
    subtitle_editor  write_srt_file     <- Smart Sync's pre-sync backup
    batch_filter     subprocess         <- extracting subs from a video
    gpu              VIDEO_CODEC_MAP    <- a codec-info fallback
    gpu              format_time        <- duration-mismatch message

`retime_subtitles` is the one that matters. `_retime()` called it, hit
NameError, and the app's global `report_callback_exception` guard swallowed the
error to keep the app alive — so **the Re-time button silently did nothing.** A
whole feature was dead and the app reported no problem at all. That is the
signature failure of this codebase: a confident, silent, wrong result.

⚠️ NONE OF THESE WERE FOUND BY READING CODE. Six existed simultaneously in a
project under active development, because a missing name on a rarely-taken
branch is invisible until someone takes that branch. That is what this test is
for — the whole point is that human review does not catch it.

HOW IT WORKS — and why it under-reports on purpose
──────────────────────────────────────────────────
It pools EVERY binding in a file into one set (imports, assignments, def/class,
params, comprehension targets, for/with/except targets, global/nonlocal) rather
than doing real scope analysis. Consequences:

  - It CANNOT false-positive on a legitimate closure, which matters in a
    5,400-line file built almost entirely out of nested functions.
  - It only catches names bound NOWHERE in the file. A name defined in the
    wrong scope still slips through.

Under-reporting is the right trade here: a noisy checker in this codebase would
be switched off, and a checker that is switched off catches nothing.

    python3 tests/test_no_undefined_names.py
"""

import ast
import builtins
import glob
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# Names Python provides at module scope that are not AST bindings.
MODULE_DUNDERS = {
    '__file__', '__name__', '__doc__', '__package__', '__spec__',
    '__loader__', '__builtins__', '__path__',
}

# ⚠️ KNOWN, TRIAGED, AND DELIBERATELY LEFT — NOT a suppression list.
# Every entry below is a real NameError. They are tolerated only because the
# code containing them is provably DEAD: nothing imports or calls it, and the
# NameError is itself the evidence — a function that raises on its main path has
# never been executed.
#
#   modules/preferences.py        save/load/reset_preferences have NO importers.
#                                 Superseded by the App method at
#                                 video_converter.py:7961 and standalone.py:115.
#                                 Two dict keys use `self` in a function whose
#                                 parameter is `app`.
#   modules/subtitle_ocr.py       run_ocr_with_monitor is not called; the live
#                                 one is video_converter.py:6809
#                                 (_run_ocr_with_monitor). Its _finish() uses
#                                 out_path / out_name / file_info, none of which
#                                 are ever assigned.
#
# ⚠️ THE RIGHT FIX IS DELETING THE DEAD MODULES, not growing this list. Both are
# stale copies of code that moved into video_converter.py, and a duplicate that
# cannot run is worse than no duplicate: it reads as a live implementation.
# Raised with Tony 2026-08-08; left in place pending his call, since deleting a
# module is his decision and neither is costing anything today.
#
# Adding to this list requires the same standard: prove it cannot run, say why
# here, and date it. If you cannot prove that, it is a bug — fix it.
KNOWN_DEAD = {
    ('modules/preferences.py', 'self'),
    ('modules/subtitle_ocr.py', 'out_path'),
    ('modules/subtitle_ocr.py', 'out_name'),
    ('modules/subtitle_ocr.py', 'file_info'),
}


def analyse(path):
    """Return (undefined_names, has_star_import) for one file."""
    tree = ast.parse(open(path, encoding='utf-8').read())
    bound, used, star = set(MODULE_DUNDERS), {}, False
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom):
            for a in n.names:
                if a.name == '*':
                    star = True
                bound.add(a.asname or a.name.split('.')[0])
        elif isinstance(n, ast.Import):
            for a in n.names:
                bound.add(a.asname or a.name.split('.')[0])
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                            ast.ClassDef)):
            bound.add(n.name)
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            bound.update(n.names)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            bound.add(n.name)
        elif isinstance(n, ast.arg):
            bound.add(n.arg)
        elif isinstance(n, ast.Name):
            if isinstance(n.ctx, (ast.Store, ast.Del)):
                bound.add(n.id)
            else:
                used.setdefault(n.id, n.lineno)
    missing = {k: v for k, v in used.items()
               if k not in bound and not hasattr(builtins, k)}
    return missing, star


def main():
    files = sorted(glob.glob(os.path.join(ROOT, 'modules', '*.py')))
    files += [os.path.join(ROOT, 'video_converter.py')]
    findings = 0
    skipped = []
    known_seen = set()
    print()
    for path in files:
        rel = os.path.relpath(path, ROOT)
        try:
            missing, star = analyse(path)
        except SyntaxError as e:
            print(f"  FAIL {rel}: SYNTAX ERROR {e}")
            findings += 1
            continue
        if star:
            # `import *` makes the binding set unknowable — say so rather than
            # reporting a clean result we cannot stand behind.
            skipped.append(rel)
            continue
        for name, line in sorted(missing.items(), key=lambda kv: kv[1]):
            if (rel, name) in KNOWN_DEAD:
                # Reported, never hidden. A baseline nobody can see is how a
                # green test stops meaning anything.
                print(f"  known {rel}:{line}: '{name}' (dead code — see "
                      f"KNOWN_DEAD)")
                known_seen.add((rel, name))
                continue
            print(f"  FAIL {rel}:{line}: undefined name '{name}'")
            findings += 1

    # A baseline entry that no longer fires means the code was fixed or deleted.
    # Say so and require the list to shrink, or it silently rots into a
    # permanent excuse.
    stale = KNOWN_DEAD - known_seen
    if stale:
        print()
        for rel, name in sorted(stale):
            print(f"  STALE baseline: {rel} '{name}' no longer undefined — "
                  f"remove it from KNOWN_DEAD")
        findings += len(stale)

    print(f"\n  {len(files)} files checked", end='')
    if skipped:
        # ⚠️ Loud on purpose. A silently skipped file is exactly how a clean
        # result stops meaning anything.
        print(f", {len(skipped)} SKIPPED for `import *`: {', '.join(skipped)}")
    else:
        print()
    if findings:
        print(f"  {findings} undefined name(s) — each is a NameError waiting "
              f"for someone to take that branch\n")
        sys.exit(1)
    print("  no undefined names\n")


if __name__ == "__main__":
    main()
