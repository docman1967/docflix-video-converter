#!/usr/bin/env python3
"""Dialect `-in'` forms are a CLASS, not a list to be typed in one word at a time.

Tony, 2026-09-26: *"I'm noticing words that I know I put into the dictionary,
coming back in the next files I'm working on... most of them are contractions
like trailin', dyin', ridin'."*

⭐⭐ HIS DICTIONARY WAS NEVER BROKEN, and proving that first is what found the
real bug. It is global, it persists, and `trailin'` genuinely was in it — a scan
with his real 678-word list does not flag it. What he was fighting is a
TREADMILL: across 40 of his subtitle files there are **114 distinct** dropped-g
forms, he had added **44** of them one at a time (6% of everything he has ever
added), and every new file brings ones he has not met yet. Adding them
individually can never finish. ⭐ The lesson is older than this bug: when the
user reports "it isn't saving", check whether the thing is saving before
believing it — the complaint is real but the diagnosis in it may not be.

So the rule suppresses the class, exactly as is_ok_contraction does for
possessives. Measured with his full dictionary loaded, 40 files: 3,012 unknown
words -> 2,849. **163 suppressed, 93 distinct forms, 5.4% of all flags.**

⛔ THE APOSTROPHE IS REQUIRED, and that was a measurement, not a preference.
Of 278 candidates, 272 (97.8%) were written with one. The six that were not
include **westin** (the hotel) and **lavin** (a surname) — the lax rule would
have swallowed proper nouns as dialect to gain 2% coverage.

Run: python3 tests/test_dropped_g.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.spell_checker import (is_dropped_g, is_ok_contraction,   # noqa: E402
                                   iter_words)

fails = []


def check(label, got, want):
    if got == want:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}\n         got:  {got!r}\n         want: {want!r}")
        fails.append(label)


print("\n1. iter_words — the tokeniser that can see the apostrophe")
# ⚠️ WORD_RE itself is UNCHANGED. Appending ['’]? to the pattern would make
# UK-style single-quoted dialogue ('Run') yield "Run'" and pyspellchecker would
# flag a perfectly spelled word — the widened-band mistake the music-note work
# already paid for. The apostrophe is read from the text AFTER the match.
check("plain word, no apostrophe", list(iter_words("hello")), [("hello", False)])
check("dropped-g form is seen WITH its apostrophe",
      list(iter_words("runnin'")), [("runnin", True)])
check("curly apostrophe counts too",
      list(iter_words("runnin’")), [("runnin", True)])
check("an internal apostrophe is still part of the word",
      list(iter_words("don't")), [("don't", False)])
check("possessive is one token",
      list(iter_words("Rossi's")), [("Rossi's", False)])
# ⛔ THE CASE THAT FORBIDS WIDENING WORD_RE. Single-quoted dialogue must not
# have its closing quote glued onto the word... but note the token DOES report
# a trailing apostrophe, which is why is_dropped_g checks the stem as well.
check("⛔ single-quoted dialogue keeps the word clean",
      [w for w, _ in iter_words("'Run'")], ["Run"])
check("a comma between word and quote breaks the adjacency",
      list(iter_words("'Run,'"))[0], ("Run", False))
check("several words", [w for w, _ in iter_words("He was ridin' fast.")],
      ["He", "was", "ridin", "fast"])


print("\n2. the rule itself")


class Spell:
    """A stand-in dictionary — `unknown` returns the words it does NOT hold."""
    def __init__(self, words):
        self.words = {w.lower() for w in words}

    def unknown(self, ws):
        return {w for w in ws if w.lower() not in self.words}


sp = Spell(["running", "making", "saying", "nothing", "being", "westing",
            "in", "ring", "bring", "thing"])
check("runnin' -> suppressed", is_dropped_g("runnin", True, sp), True)
check("makin' -> suppressed", is_dropped_g("makin", True, sp), True)
check("capitalised at the start of a line still works",
      is_dropped_g("Runnin", True, sp), True)

print("\n3. ⛔ what it must NOT swallow")
# ⛔ THE LOAD-BEARING NEGATIVE. No apostrophe means it could be a typo or a
# proper noun, and 97.8% of real dialect forms DO carry one — so the cost of
# requiring it is almost nothing and the protection is real.
check("⛔ no apostrophe -> NOT suppressed", is_dropped_g("runnin", False, sp), False)
check("⛔ 'westin' the hotel, bare -> NOT suppressed",
      is_dropped_g("westin", False, sp), False)
# ⛔ A word whose +g form is not a word is not dialect, apostrophe or not.
check("⛔ stem+g is not a word -> NOT suppressed",
      is_dropped_g("zzqin", True, sp), False)
check("⛔ does not end in -in -> NOT suppressed",
      is_dropped_g("runnan", True, sp), False)
check("⛔ the ordinary word 'in' can never reach the lookup",
      is_dropped_g("in", True, sp), False)
check("⛔ too short to be dialect", is_dropped_g("bin", True, sp), False)
check("⛔ empty string", is_dropped_g("", True, sp), False)
# ⛔ It may only ever SUPPRESS. A broken dictionary object must fail closed —
# returning True on an exception would silently hide real misspellings.
class Boom:
    def unknown(self, ws):
        raise RuntimeError("dictionary exploded")


check("⛔ a raising dictionary -> False, never True",
      is_dropped_g("runnin", True, Boom()), False)


print("\n4. against the REAL pyspellchecker and real sentences")
try:
    import spellchecker                                             # noqa: F401
except ImportError:
    print("  SKIP  pyspellchecker not installed")
else:
    from modules.spell_checker import run_spell_highlight_scan

    class App:
        custom_spell_words = []          # ⭐ EMPTY — the whole point is that he
        custom_cap_words = []            # should not have to add these at all

    def flagged(text):
        idx = set()
        w = run_spell_highlight_scan(App(), None,
                                     [{'text': text, 'start': '0', 'end': '1'}],
                                     idx, quiet=True)
        return (w or {}).get(0)

    check("a line of dialect clears with an EMPTY dictionary",
          flagged("He was runnin' and makin' and sayin' nothin'."), None)
    check("⛔ the same words WITHOUT apostrophes are still flagged",
          flagged("He was runnin and makin and sayin nothin."),
          ["runnin", "makin", "sayin", "nothin"])
    check("⛔ a genuine misspelling still survives beside dialect",
          flagged("He beheves it and was walkin'."), ["beheves"])
    check("⛔ 'westin hotel' is still flagged",
          flagged("We stayed at the westin hotel."), ["westin"])
    # ⚠️ Known and accepted: "westin'" WITH an apostrophe is suppressed,
    # because "westing" is a word. Nobody writes the hotel that way, and the
    # measurement says requiring the apostrophe is still the safer rule.
    check("⚠️ westin' WITH an apostrophe is suppressed (accepted cost)",
          flagged("We stayed at the westin' hotel."), None)
    # ⛔ The older rule must still work — this sits alongside it, not over it.
    check("⛔ possessives still handled by is_ok_contraction",
          flagged("That is Whatever's problem."), None)

print("\n5. ⛔ the F7 dialog applies the SAME rule as the highlighter")
# ⚠️ A rule the tree honours and the dialog does not is the detector/doer split
# that keeps catching us: the row would stop being coloured while F7 still
# stopped on every "runnin'". Both now tokenise with iter_words and both call
# is_dropped_g.
import ast                                                          # noqa: E402
ED = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                  "..", "modules", "subtitle_editor.py")
src = open(ED).read()
fn = next(n for n in ast.walk(ast.parse(src))
          if isinstance(n, ast.FunctionDef) and n.name == "_find_next")
body = ast.get_source_segment(src, fn)
check("_find_next uses iter_words", "iter_words(clean)" in body, True)
check("⛔ ...and no longer uses findall", ".findall(clean)" in body, False)
check("_find_next applies is_dropped_g", "is_dropped_g(" in body, True)
check("   ...with the apostrophe flag from the tokeniser",
      "is_dropped_g(w, _apos, spell)" in body, True)

print()
if fails:
    print(f"FAILED: {len(fails)} check(s)")
    sys.exit(1)
print("all dropped-g checks passed")
