#!/usr/bin/env python3
"""Word-level suspect flags for the OCR review pane.

⚠️⚠️ EVERY THRESHOLD IN modules/ocr_suspect.py WAS MEASURED against 368,196
real cues from 397 of Tony's own subtitle files, plus 1,278 live PGS cues from
Warehouse 13 S01E01. These tests pin the behaviour those measurements bought.
A change that makes them pass by loosening an assertion has thrown the
measurement away — re-measure instead.

The governing rule (docs/OCR_REVIEW_PANE.md): a heuristic that fires on good
text is WORSE than no heuristic, because it trains you to ignore the column and
then the one real flag goes past unread. So the "must NOT flag" tests below
matter more than the "must flag" ones.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.ocr_suspect import (                               # noqa: E402
    load_dictionary, is_dictionary_word, words_of, _is_dropped_g,
    one_substitution_fixes, splits_into_words, check_confusions,
    check_width_ratio, _FUNCTION_WORDS)

load_dictionary()


# ── Dictionary handling ─────────────────────────────────────────────────────

@pytest.mark.parametrize("word", [
    "hello", "dinosaurs", "champagne",
    "favour", "jeopardise",          # ⚠️ Commonwealth spelling is NOT an error
    "okay", "yeah", "gonna",         # dialogue words a 1922 wordlist lacks
])
def test_known_words(word):
    assert is_dictionary_word(word), word


@pytest.mark.parametrize("word", [
    "didn't", "don't", "isn't", "wouldn't", "I've", "they're", "o'clock",
])
def test_contractions_are_known(word):
    """⚠️ THE BIGGEST FALSE-POSITIVE SOURCE. Splitting on the apostrophe
    leaves `didn`, which is in no dictionary AND is a real surname, so the
    names DB does not rescue it either. Every contraction would flag."""
    assert is_dictionary_word(word), word


@pytest.mark.parametrize("word", ["goin", "doin", "comin", "gettin", "somethin"])
def test_dropped_g_is_dialect_not_error(word):
    """⚠️ goin and doin are exactly 4 chars; an off-by-one in the length
    bound left the two most common forms (279 and 237 hits) unfixed."""
    assert _is_dropped_g(word), word


# ── Tokenising ──────────────────────────────────────────────────────────────

def test_skips_hi_lyrics_and_speaker_labels():
    """SDH is most of what Tony OCRs; judging its furniture as prose would
    flag a large slice of every track."""
    assert words_of("[DOOR SLAMS]") == []
    assert words_of("♪ Doobie doobie wah ♪") == []
    assert words_of("<i>Hello</i> there") == ["Hello", "there"]
    got = words_of("MAN: Over here")
    assert "Over" in got and "MAN" not in got


def test_skips_short_and_shouted_tokens():
    assert words_of("Ow! Mm. Er?") == []          # under 4 chars
    assert words_of("STOP THAT NOW") == []        # ALL CAPS is a shout


# ── 1. One-substitution confusions (the high-precision check) ───────────────

@pytest.mark.parametrize("bad,good", [
    ("Iike", "like"), ("wouId", "would"), ("onIy", "only"),
    ("lnspector", "Inspector"), ("ltalian", "Italian"), ("feeI", "feel"),
])
def test_real_confusions_are_caught(bad, good):
    """Every one of these was found in Tony's existing library."""
    assert one_substitution_fixes(bad) == good


@pytest.mark.parametrize("word", [
    "hello", "dinosaurs", "champagne", "beautiful", "something",
])
def test_good_words_are_not_corrected(word):
    assert one_substitution_fixes(word) is None


def test_names_are_not_corrected_into_other_words():
    """⚠️ Without the names DB this "fixed" Arnie->Amie, Henning->Heming,
    Renn->Rem via rn->m — the bulk of the false positives in the first
    measurement pass, which ran with an accidentally EMPTY names set."""
    names = {"Arnie", "Henning", "Renn"}
    assert check_confusions("Arnie and Henning", names) == []
    assert check_confusions("Arnie and Henning", None) != [], (
        "fixture no longer exercises the names path")


# ── 2. Function-word run-together ───────────────────────────────────────────

@pytest.mark.parametrize("bad,good", [
    ("foryou", "for you"), ("Wejust", "We just"), ("whatyou", "what you"),
    ("thejungle", "the jungle"), ("forthe", "for the"),
])
def test_missing_space_is_caught(bad, good):
    assert splits_into_words(bad) == good


@pytest.mark.parametrize("word", [
    "Aquaman", "Batmobile", "Superfriends", "thrusters", "somethin",
    "backseat", "payback", "blowfish",
])
def test_compounds_are_not_split(word):
    """⚠️⚠️ THE CONSTRAINT THAT MAKES THIS CHECK WORK. "splits into any two
    dictionary words" measured at 1 in 67 cues and was almost entirely wrong,
    because English compounds are everywhere. Restricting the first part to
    function words took it to 1 in 856. Do not loosen it."""
    assert splits_into_words(word) is None


def test_function_word_set_stays_closed():
    """⛔ Adding a compound-capable word here is the change that collapses the
    precision. over/under/out/up/down all begin real compounds."""
    forbidden = {'over', 'under', 'out', 'up', 'down', 'back', 'off', 'on',
                 'in', 'super', 'after', 'air', 'side'}
    assert not (_FUNCTION_WORDS & forbidden), (
        f"compound-capable words added to _FUNCTION_WORDS: "
        f"{_FUNCTION_WORDS & forbidden}")


# ── 3. Text too short for the bitmap ────────────────────────────────────────

def test_width_ratio_ignores_real_cues():
    """⚠️ Measured on 1,278 live PGS cues: median 49.9 px/char, max 84.7.
    The default of 120 flagged ZERO of them."""
    for img_w, text in ((254, "No?"), (246, "Ow!"), (320, "Wow."),
                        (792, "[COW MOOS]"), (1232, "[SHOWER RUNNING]"),
                        (756, "Champagne?")):
        assert not check_width_ratio(text, img_w), (text, img_w)


def test_width_ratio_catches_a_partial_read():
    """A 1200px bitmap yielding three characters is ~400 px/char — far outside
    anything real text produces."""
    assert check_width_ratio("the", 1200)


def test_width_ratio_needs_a_wide_bitmap():
    """A narrow bitmap legitimately holds one short word."""
    assert not check_width_ratio("No?", 200)


def test_width_ratio_ignores_empty_text():
    """That is the empty/lost case and it is flagged elsewhere; double-flagging
    would bury the more specific message."""
    assert not check_width_ratio("", 1200)


# ── End to end through the review pane's flag ───────────────────────────────

def test_flag_ocr_cue_end_to_end():
    from modules.subtitle_editor import flag_ocr_cue
    assert flag_ocr_cue({'text': 'Iike this'})[0] == 'flag_ocr'
    assert flag_ocr_cue({'text': 'foryou'})[0] == 'flag_ocr'
    for clean in ("Hello there.", "the dinosaurs.", "I don't know.",
                  "Café... naïve", "♪ Set me free ♪", "[DOOR SLAMS]",
                  "MAN: Over here", "Aquaman and Superfriends"):
        assert flag_ocr_cue({'text': clean})[0] is None, clean


def test_flag_names_the_correction():
    """The Note column has to be checkable at a glance against the bitmap —
    "suspect" is useless, "Iike -> like?" is not."""
    from modules.subtitle_editor import flag_ocr_cue
    _tag, reason = flag_ocr_cue({'text': 'Iike this'})
    assert 'like' in reason and '->' in reason




# ── Lowercased acronyms ─────────────────────────────────────────────────────
# ⭐ Tony, 2026-09-13: "I'd rather have them flagged and be right than not
# flagged and be wrong." Fix ALL CAPS lowercases what it does not recognise,
# and on a line that reads as shouting an acronym is exactly that:
# "NASA and NORAD." -> "NASA and norad.". Nothing could see it, because the
# dictionary loader lowercased every entry and `norad` looked like a word.

from modules.ocr_suspect import (                               # noqa: E402
    wrongly_lowercased, lowercase_tokens, get_caps_only, _NOT_ACRONYMS)


@pytest.mark.parametrize("low,caps", [
    ("norad", "NORAD"), ("fbi", "FBI"), ("cia", "CIA"),
    ("nasa", "NASA"), ("dna", "DNA"), ("ibm", "IBM"),
])
def test_lowercased_acronyms_are_caught(low, caps):
    assert wrongly_lowercased(low) == caps


@pytest.mark.parametrize("word", [
    "the", "deal", "warehouse", "police", "doctor", "well", "look",
    # ⚠️ These broke the first cut. Restricting the set to entries that are
    # wholly UPPERCASE in the wordlist fixed them — including only-capitalised
    # entries flagged `english` -> `ENGLISH`, which is not even the right
    # answer (it is `English`).
    "english", "monday", "paris", "china", "march", "may", "august",
])
def test_ordinary_words_are_not_acronyms(word):
    assert wrongly_lowercased(word) is None, word


@pytest.mark.parametrize("word", ["de", "al", "co", "ne", "va", "er", "da"])
def test_two_letter_acronyms_are_ignored(word):
    """⚠️ 114 of the 416 known acronyms are two letters and they are US STATE
    CODES colliding with ordinary fragments and foreign words. Measured: min 2
    fires on 1 cue in 676 and is almost all wrong; min 3 on 1 in 5,844."""
    assert wrongly_lowercased(word) is None, word


@pytest.mark.parametrize("word", sorted(_NOT_ACRONYMS))
def test_measured_exclusions_stay_excluded(word):
    """Each was a real false positive on Tony's library, not a guess."""
    assert wrongly_lowercased(word) is None, word


def test_case_is_only_wrong_when_fully_lowercase():
    """"Norad" mid-sentence is a plausible proper noun; second-guessing
    capitalisation is not this check's job."""
    assert wrongly_lowercased("Norad") is None
    assert wrongly_lowercased("NORAD") is None


def test_tokens_respect_word_boundaries():
    """⚠️ Without \\b this matches the lowercase RUN inside a mixed-case word:
    "Set" yields "et", a real acronym, and produced a confident nonsense flag
    on the lyric "♪ Set me free ♪"."""
    assert "et" not in lowercase_tokens("Set me free")
    assert lowercase_tokens("♪ Set me free ♪") == []       # ♪ lines skipped
    assert "norad" in lowercase_tokens("I work for norad.")


def test_acronym_flag_end_to_end():
    from modules.subtitle_editor import flag_ocr_cue
    tag, reason = flag_ocr_cue({'text': 'NASA and norad.'})
    assert tag == 'flag_ocr'
    assert 'NORAD' in reason
    # and the correct form must stay clean
    assert flag_ocr_cue({'text': 'NASA and NORAD.'})[0] is None
    assert flag_ocr_cue({'text': '♪ Set me free ♪'})[0] is None


def test_caps_only_set_is_reachable():
    """⚠️ load_dictionary REBINDS the global; importing `_caps_only` directly
    captures an empty set. Arthur hit this trap twice in one day — once with
    the names DB, once with this very set."""
    assert len(get_caps_only()) > 100


# ── Unmatched brackets: a mangled HI annotation ─────────────────────────────
# ⭐ Tony, 2026-09-13: "a [ is mistaken for an I or L so a cue like Iscreams]
# makes it past the filter." With the opening bracket gone it stops looking
# like an annotation to filter_remove_hi and survives into the .srt.

from modules.ocr_suspect import unbalanced_brackets               # noqa: E402


@pytest.mark.parametrize("text", [
    "Iscreams]", "lscreams]", "[SCREAMS", "Indistinct]",
    "[CELL DOOR UNLOCKS}",                 # real: `}` misread for `]`
    "[Soneji's] Taped Voice,\nIndistinct]",  # real: a doubled `]`
])
def test_mangled_annotations_are_caught(text):
    assert unbalanced_brackets(text), text


@pytest.mark.parametrize("text", [
    "[SCREAMS]", "[DOOR SLAMS]", "Hello there.", "MAN: Over here",
    "\u266a Set me free \u266a", "",
    # ⚠️⚠️ PARENTHESES ARE DELIBERATELY NOT CHECKED. Measured over 368,196
    # library cues: adding ( ) took the rate from 1-in-184,098 with ZERO false
    # positives to 1-in-28,322 with ELEVEN. Every one of these is a real line.
    "(sighs)",
    "A) Terror as a political instrument",
    "I was working on C).",
    "in recent times (but goes back",              # opens, closes NEXT cue
    "to the '70s, I've found), is:",               # the closing half
])
def test_legitimate_text_is_not_flagged(text):
    assert not unbalanced_brackets(text), text


def test_curly_braces_are_not_treated_as_brackets():
    """⚠️ Counting `{`/`}` as bracket-like would BALANCE "[CELL DOOR UNLOCKS}"
    and lose the catch — and would flag every {\\an8} positioning tag."""
    assert unbalanced_brackets("[CELL DOOR UNLOCKS}")
    assert not unbalanced_brackets("{\\an8}Hello there")


def test_bracket_flag_end_to_end():
    from modules.subtitle_editor import flag_ocr_cue
    tag, reason = flag_ocr_cue({'text': 'Iscreams]'})
    assert tag == 'flag_junk', tag
    assert 'unmatched' in reason
    assert flag_ocr_cue({'text': '[SCREAMS]'})[0] is None


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-q']))
