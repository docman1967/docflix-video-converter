#!/usr/bin/env python3
"""The typographic apostrophe (’) must behave exactly like the straight one.

⚠️⚠️ WHY THIS FILE EXISTS. Two separate features split subtitle text into
words with a regex that only knew `'`. About 4% of library files use
typographic quotes, and in those files:

    SPELLING   "isn’t" was torn into "isn" + "t" BEFORE is_ok_contraction()
               — which DOES normalise ’ — could see it. So `isn`, `hasn`,
               `doesn`, `wasn`, `couldn`, `wouldn`, `didn`, `aren`, `weren`
               were reported as misspellings.

    FIX ALL    "O’BRIEN" matched as "O" then "BRIEN", both lookups missed,
    CAPS       and the output came back "O’brien" — a visibly WRONG result,
               not merely a skipped one.

⭐ WHY IT HID FOR YEARS: most contractions degrade into real words. "that’s"
splits into "that" + "s", both known, nothing flagged. The bug only surfaces
on stems that are not themselves words, and on names that contain an
apostrophe. Both are a minority of a minority.

⚠️ THE TRAP THAT ALMOST SHIPPED A REGRESSION: "HIRST’S" worked BEFORE the fix,
by accident — the old pattern stopped at the curly, so "HIRST" matched the
whole-token lookup and never reached the possessive branch. Widening the
pattern without also widening `rpartition("'")` would have broken a case that
already worked. The tests below cover both apostrophes on every path for
exactly that reason.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.spell_checker import (WORD_RE,                    # noqa: E402
                                   contraction_roots,
                                   is_ok_contraction)
from modules.subtitle_filters import filter_fix_caps           # noqa: E402


def _cue(text):
    return {'index': 1, 'start': '00:00:01,000', 'end': '00:00:02,000',
            'text': text}


# ─────────────────────────── the tokeniser ───────────────────────────

def test_a_curly_contraction_is_ONE_token():
    """⚠️ THE ROOT CAUSE. If this splits, everything downstream is wrong."""
    assert WORD_RE.findall("it isn’t broken") == ['it', 'isn’t', 'broken']
    assert WORD_RE.findall("it isn't broken") == ['it', "isn't", 'broken']


def test_both_apostrophes_tokenise_identically():
    for straight, curly in (("doesn't", "doesn’t"), ("we'll", "we’ll"),
                            ("O'Brien", "O’Brien"), ("boys'", "boys’")):
        a = WORD_RE.findall(f'the {straight} thing')
        b = WORD_RE.findall(f'the {curly} thing')
        assert len(a) == len(b), f'{straight!r} and {curly!r} split differently'


def test_contraction_roots_already_handled_curly():
    """It always did — which is exactly why the bug was upstream of it, and
    why reading this function first sent Arthur to the wrong place."""
    assert contraction_roots("isn’t") == contraction_roots("isn't")


# ─────────────────────── spelling: no false flags ───────────────────────

class _Spell:
    """Minimal stand-in: knows a few words, nothing else."""
    KNOWN = {'is', 'has', 'does', 'was', 'could', 'would', 'did', 'are',
             'were', 'it', 'she', 'he', 'we', 'broken', 'shown', 'care'}

    def unknown(self, words):
        return {w for w in words if w.lower() not in self.KNOWN}


def test_the_stems_that_used_to_be_flagged_are_not():
    """isn / hasn / doesn / wasn / couldn / wouldn / didn / aren / weren."""
    sp = _Spell()
    for phrase in ("it isn’t broken", "she hasn’t shown", "he doesn’t care",
                   "it wasn’t broken", "we couldn’t care", "we wouldn’t care",
                   "she didn’t care", "we aren’t broken", "we weren’t broken"):
        toks = WORD_RE.findall(phrase)
        unknown = sp.unknown(toks)
        flagged = [w for w in toks
                   if w in unknown and not is_ok_contraction(w, sp, [])]
        assert not flagged, f'{phrase!r} flagged {flagged}'


def test_a_genuinely_unknown_root_is_STILL_flagged():
    """⚠️ The fix must not turn the speller off. "Xyzzy’s" has no known root
    and must survive as a flag, or a misspelled name stops being caught."""
    sp = _Spell()
    toks = WORD_RE.findall("Xyzzy’s car")
    flagged = [w for w in toks
               if w in sp.unknown(toks) and not is_ok_contraction(w, sp, [])]
    assert 'Xyzzy’s' in flagged


# ─────────────────── Fix ALL CAPS: names with apostrophes ───────────────────

def _fix(text, names=("O'Brien", "Hirst")):
    return filter_fix_caps([_cue(text)], custom_names=list(names))[0]['text']


def test_a_curly_name_is_capitalised():
    """⚠️ THE VISIBLE BUG: this produced "O’brien" — wrong output, not a
    skipped one."""
    assert _fix("O’BRIEN IS HERE.") == "O’Brien is here."
    assert _fix("O'BRIEN IS HERE.") == "O'Brien is here."


def test_the_possessive_path_still_works_BOTH_ways():
    """⚠️ THE NEAR-REGRESSION. "HIRST’S" worked before the fix by accident,
    via the whole-token lookup. If rpartition is ever narrowed back to a bare
    straight quote this goes red."""
    assert _fix("HIRST’S CAR IS OUTSIDE.") == "Hirst’s car is outside."
    assert _fix("HIRST'S CAR IS OUTSIDE.") == "Hirst's car is outside."


def test_a_name_with_an_apostrophe_in_its_possessive():
    assert _fix("O’BRIEN’S CAR.") == "O’Brien’s car."
    assert _fix("O'BRIEN'S CAR.") == "O'Brien's car."


def test_mixed_apostrophe_styles_in_one_word():
    """Real files are inconsistent. Each apostrophe keeps its own character."""
    assert _fix("O'BRIEN’S CAR.") == "O'Brien’s car."


def test_his_typography_is_never_rewritten():
    """⚠️ The stored name is the authority on CAPITALISATION, not on
    punctuation. Swapping ’ for ' would edit his text to suit our dictionary
    and leave one straight quote in a line of curly ones."""
    out = _fix("O’BRIEN IS HERE.")
    assert '’' in out and "'" not in out


def test_an_ordinary_contraction_is_left_alone():
    """"don't" is not a name; neither half is in the list; it must pass
    through untouched apart from the ordinary caps fix."""
    assert _fix("I DON’T KNOW.") == "I don’t know."
    assert _fix("I DON'T KNOW.") == "I don't know."


def test_a_plain_name_is_unaffected_by_any_of_this():
    assert _fix("HIRST WAS HERE.") == "Hirst was here."
