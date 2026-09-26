#!/usr/bin/env python3
"""The spelling highlight must stay true as the file changes underneath it.

Tony, 2026-09-26: *"if I delete a line, the remaining spelling errors don't move
with the new line numbers. Also, once a word or name has been added to the
dictionary or name list, it doesn't clear the remaining errors. I have to select
'Highlight Spelling Errors' again to get them to clear."*

⭐ ONE ROOT CAUSE, TWO SYMPTOMS: the scan result is derived data, keyed BY CUE
INDEX, cached from a single menu-driven run — and ~16 operations renumber the
cues under it (delete, split, join, insert, undo, redo, Reset to Original,
apply_filter, Replace All...). The ALL CAPS highlighter beside it has re-scanned
on every rebuild since the day it shipped. The spell highlighter was the one
that cached, so it was the one that drifted.

⭐ The fix is a rescan at the chokepoint (refresh_tree) rather than index
arithmetic at each of the 16 sites — a site added next year cannot forget a
rescan it does not have to call. It is affordable ONLY because checker_for()
caches the SpellChecker: the constructor is ~145 ms, the loop ~15 ms.

⚠️ WHAT THIS FILE DOES AND DOES NOT COVER. Everything below RUNS the real scan
against the real pyspellchecker, and sections 2-3 drive the real editor-side
re-derivation (rescan_spell_state) against live containers. The one link it
cannot execute is `refresh_tree` itself, which needs a live Tk editor window —
that is asserted structurally in section 6 and is the last link Tony's own
click closes. Say so out loud rather than letting a green run imply more than
it proved.

⭐ MUTATION-TESTED 2026-09-26, which is the only way to know a suite bites:
  - refresh_tree stops calling the rescan      -> caught (§6)
  - the rescan forgets to clear the detail map -> caught (§3)
  - the `continue` that stops double-reporting -> SURVIVED, and that was the
    useful one: a name in the lut is also loaded into the dictionary, so it can
    never be in `unknown` and the guard has no reachable case today. The
    comment in spell_checker.py was corrected to say so instead of claiming it
    was load-bearing.

Run: python3 tests/test_spell_highlight_live.py
"""
import ast
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules import spell_checker as SC                             # noqa: E402

fails = []


def check(label, got, want):
    if got == want:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}\n         got:  {got!r}\n         want: {want!r}")
        fails.append(label)


class App:
    """Stand-in for the app object — only what the scan reads."""
    def __init__(self, spell=(), cap=()):
        self.custom_spell_words = list(spell)
        self.custom_cap_words = list(cap)


def cues(*texts):
    return [{'text': t, 'start': '00:00:00,000', 'end': '00:00:01,000'}
            for t in texts]


def scan(app, cs, extra=()):
    """(error indices, words by cue, name fixes by cue) — the real scan."""
    idx, names = set(), {}
    words = SC.run_spell_highlight_scan(app, None, cs, idx, name_fixes=names,
                                        quiet=True, extra_known=extra)
    return idx, (words or {}), names


try:
    import spellchecker                                             # noqa: F401
except ImportError:
    print("SKIP — pyspellchecker is not installed; nothing below can run.")
    sys.exit(0)


print("\n1. the scan still finds ordinary misspellings")
app = App()
FILE = cues("Hello there.",                 # 0 clean
            "He beheves it.",               # 1 misspelling
            "Nothing wrong here.",          # 2 clean
            "Another beheves line.")        # 3 misspelling
idx, words, names = scan(app, FILE)
check("two cues flagged", sorted(idx), [1, 3])
check("and it names the word", words.get(1), ["beheves"])
check("no name fixes without a name list", names, {})


# ⚠️⚠️ WHY SECTIONS 2 AND 3 DRIVE rescan_spell_state AND NOT scan().
# The first draft of this file called run_spell_highlight_scan twice and checked
# the second answer — and that proves NOTHING about Tony's bug. That function
# was always a pure function of its inputs; it would have passed against the
# broken code too. The defect was never inside the scan, it was that **nothing
# called it again**. So the thing under test has to be the editor-side
# re-derivation that refresh_tree performs, mutating the editor's live
# containers. That is rescan_spell_state, which exists as a module-level
# function for exactly this reason.
from modules.subtitle_editor import rescan_spell_state               # noqa: E402


class Editor:
    """The editor's live highlight containers, as refresh_tree holds them."""
    def __init__(self):
        self.indices, self.words, self.fixes = set(), {}, {}

    def repaint(self, app, cs, temp=()):
        """What refresh_tree does to the spell state on every rebuild."""
        return rescan_spell_state(app, None, cs, self.indices, self.words,
                                  self.fixes, temp)


print("\n2. ⭐ DELETE A CUE AND THE MARKS FOLLOW — Tony's first symptom")
ed = Editor()
ed.repaint(app, FILE)                       # user clicks Highlight Spelling
check("scanned: cues 1 and 3 are lit", sorted(ed.indices), [1, 3])
# ⭐ THE BUG. The old code kept {1, 3}; deleting cue 0 left cue 3's mark on a
# row that had moved to index 2 — Tony: "the remaining spelling errors don't
# move with the new line numbers."
ed.repaint(app, FILE[1:])                   # delete cue 0, tree rebuilds
check("⭐ after deleting cue 0 the marks moved down", sorted(ed.indices), [0, 2])
check("   ...and the word travelled with the row", ed.words.get(2), ["beheves"])
check("   ...and the vacated index is genuinely clear", 3 in ed.indices, False)
# ⛔ NEGATIVE CONTROL: a real re-derivation, not a blanket shift. Dropping a
# LATER cue must leave the earlier mark exactly where it was.
ed.repaint(app, [FILE[0], FILE[1], FILE[3]])
check("⛔ deleting a later cue leaves earlier marks alone",
      sorted(ed.indices), [1, 2])
# ⛔ And an INSERT must push marks the other way — the same set of sites that
# renumber on delete renumber on insert (split, insert cue, Reset to Original).
ed.repaint(app, cues("Brand new first line.") + FILE)
check("⛔ inserting at the top pushes marks up", sorted(ed.indices), [2, 4])


print("\n3. ⭐ ADDING A WORD CLEARS IT EVERYWHERE — Tony's second symptom")
# The cache key IS the known-word list, so no site has to call an invalidate
# function. Nine separate doors add words; none of them has to remember.
ed = Editor()
ed.repaint(app, FILE)
check("flagged before the word is added", sorted(ed.indices), [1, 3])
app.custom_spell_words.append("beheves")    # "Add to Dict"
ed.repaint(app, FILE)                       # the very next repaint
check("⭐ cleared from EVERY cue, not just the one being looked at",
      sorted(ed.indices), [])
check("   ...and the detail map was emptied too", ed.words, {})
# ⛔ NEGATIVE CONTROL: adding an unrelated word must change nothing. Without
# this, "it cleared" could simply mean "it clears on every repaint".
app.custom_spell_words.remove("beheves")
app.custom_spell_words.append("zzzunrelated")
ed.repaint(app, FILE)
check("⛔ an unrelated word does not clear a real error",
      sorted(ed.indices), [1, 3])

print("\n3b. ⛔ a missing spell checker must not wipe the marks")
# ⚠️ Wiping row colours because a package is missing reads as "your file is
# clean now" — the one direction of lie that costs the user work. This depends
# on run_spell_highlight_scan's quiet path returning BEFORE it clears the index
# set, which is an ordering worth asserting rather than trusting.
import builtins                                                      # noqa: E402
_real_import = builtins.__import__


def _no_spellchecker(name, *a, **k):
    if name == "spellchecker":
        raise ImportError("simulated: package not installed")
    return _real_import(name, *a, **k)


ed = Editor()
ed.repaint(app, FILE)
_before = (set(ed.indices), dict(ed.words))
builtins.__import__ = _no_spellchecker
try:
    _ok = ed.repaint(app, FILE)
finally:
    builtins.__import__ = _real_import
check("returns False when the checker is unavailable", _ok, False)
check("⛔ the marks survive untouched", ed.indices, _before[0])
check("⛔    ...and so does the word detail", ed.words, _before[1])


print("\n4. wrong-case names light up too (Tony, 2026-09-26: 'yes I want names")
print("   to be lit as well')")
# ⚠️⚠️ THE SUBTLE PART. Adding "Hirst" to the name list is exactly what makes
# "hirst" a correctly-SPELLED word — pyspellchecker lowercases before lookup.
# So a scan that only examined UNKNOWN words could never find this, which is
# why the check sits after the `continue` in the scan loop, not inside it.
napp = App(cap=["Hirst", "Gemma"])
NFILE = cues("I saw hirst today.",          # 0 miscased
             "Hirst was here.",             # 1 correct
             "hirst's car is gone.",        # 2 miscased possessive
             "GEMMA SHOUTED.",              # 3 ALL CAPS — emphasis, not an error
             "He beheves it.")              # 4 plain misspelling
nidx, nwords, nfix = scan(napp, NFILE)
check("miscased name flagged", 0 in nidx, True)
check("   ...and the correction is authoritative", nfix.get(0), [("hirst", "Hirst")])
check("⛔ correctly-cased name NOT flagged", 1 in nidx, False)
check("possessive flagged too", nfix.get(2), [("hirst's", "Hirst's")])
check("⛔ ALL CAPS left to the Fix ALL CAPS filter", 3 in nidx, False)
check("a plain misspelling is still a misspelling", nwords.get(4), ["beheves"])
check("   ...and is NOT also reported as a name", 4 in nfix, False)
# ⛔ The two maps must stay disjoint in MEANING: a cue with only a caps fault
# has no entry in the spelling words map, or the Note would say `sp: hirst`
# about a word that is spelled perfectly.
check("⛔ a caps-only cue has no spelling word", 0 in nwords, False)


print("\n5. the checker cache — what makes a rescan affordable")
SC._CHECKER["key"], SC._CHECKER["spell"] = None, None
t0 = time.perf_counter()
a = SC.checker_for(["alpha", "beta"])
cold = (time.perf_counter() - t0) * 1000
t1 = time.perf_counter()
b = SC.checker_for(["alpha", "beta"])
warm = (time.perf_counter() - t1) * 1000
c = SC.checker_for(["beta", "ALPHA"])       # same set, different order/case
d = SC.checker_for(["alpha", "beta", "gamma"])
print(f"       cold {cold:.0f} ms   warm {warm:.2f} ms")
check("same word list -> the same checker", a is b, True)
check("order and case do not matter", a is c, True)
check("⭐ a NEW word rebuilds it (this is what clears the error)", a is d, False)
# ⚠️ Assert the SPEEDUP, not just the identity. `a is b` would still hold if
# someone made the constructor cheap by breaking it; the point of the cache is
# that the second call does no work.
check("the cached call is at least 20x faster", warm * 20 < cold, True)


print("\n6. the wiring — structural, and only structural")
# ⚠️ Sections 1-5 all RUN. This one cannot: refresh_tree needs a live Tk editor
# window. It is here to catch the rescan being deleted or un-gated, NOT to
# prove the feature works — that is Tony's click. Yesterday a pair of green
# structural tests covered an OCR helper that raised NameError on every cue.
ED = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                  "..", "modules", "subtitle_editor.py")
src = open(ED).read()
tree = ast.parse(src)
rt = [n for n in ast.walk(tree)
      if isinstance(n, ast.FunctionDef) and n.name == "refresh_tree"]
check("refresh_tree exists exactly once", len(rt), 1)
calls = [n for n in ast.walk(rt[0]) if isinstance(n, ast.Call)
         and isinstance(n.func, ast.Name)
         and n.func.id == "rescan_spell_state"]
check("refresh_tree re-derives the spell state", len(calls), 1)
# ⚠️ An earlier version of this section indexed calls[0] unguarded, so when the
# call moved it raised IndexError and the whole file ABORTED mid-run — sections
# below it never executed and the harness saw a crash rather than a failure.
# Yesterday a grep-based harness read exactly that silence as a pass.
if calls:
    # ⛔ The gate must be the CALL, not a comment. Everything the helper needs
    # is positional here, so assert the shape rather than keyword names.
    check("   ...passing the editor's live containers (7 args)",
          len(calls[0].args), 7)
# ⚠️ The helper's own contract: quiet, and it must ask for the name fixes.
helper = [n for n in ast.walk(tree)
          if isinstance(n, ast.FunctionDef) and n.name == "rescan_spell_state"]
check("rescan_spell_state is module-level (so a test can drive it)",
      len(helper), 1)
if helper:
    hcalls = [n for n in ast.walk(helper[0]) if isinstance(n, ast.Call)
              and isinstance(n.func, ast.Name)
              and n.func.id == "run_spell_highlight_scan"]
    check("   ...and it is the one thing that calls the scan", len(hcalls), 1)
    if hcalls:
        kw = {k.arg for k in hcalls[0].keywords}
        check("   ...quietly (no pip-install modal on every repaint)",
              "quiet" in kw, True)
        check("   ...asking for the name fixes", "name_fixes" in kw, True)
        check("   ...and honouring the per-file temp names",
              "extra_known" in kw, True)
    # ⚠️ The import must be INSIDE the helper or it is a NameError at call
    # time — the exact failure that shipped in subtitle_ocr.py on 2026-09-25.
    imports = [n for n in ast.walk(helper[0]) if isinstance(n, ast.ImportFrom)]
    check("the import is inside the helper, not borrowed from elsewhere",
          any(any(a.name == "run_spell_highlight_scan" for a in n.names)
              for n in imports), True)
# ⛔ It must be GATED. An ungated rescan would highlight a file nobody asked to
# check, and pay the scan on every repaint forever.
guards = [n for n in ast.walk(rt[0]) if isinstance(n, ast.If)
          and any(isinstance(s, ast.Subscript)
                  and getattr(s.value, "id", "") == "spell_scanned"
                  for s in ast.walk(n.test))]
check("⛔ the rescan is gated on spell_scanned", len(guards), 1)

# ⚠️ _row_note carries a documented contract: every signal that can lose the
# background colour must appear there. A new tag without a Note entry is a
# signal that vanishes whenever a rarer one shares its row.
note = [n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_row_note"]
check("the new name marker is in _row_note (the contract)",
      "name: " in ast.get_source_segment(src, note[0]), True)

print()
if fails:
    print(f"FAILED: {len(fails)} check(s)")
    sys.exit(1)
print("all spell-highlight checks passed")
