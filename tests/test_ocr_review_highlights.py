#!/usr/bin/env python3
"""OCR review pane: the three highlights added 2026-09-19.

⚠️ WHY THIS FILE EXISTS. Both functions here are *bands* — a rule that says
"this cue is worth a second look". A band that fires too often is not a mild
annoyance, it is a feature that destroys the pane it lives in: Tony stops
reading the Note column, and the real catches go with the noise. The Media
Suite has shipped that mistake twice (the music-note geometry bands that ate
the letter `u`; `_is_music_note_frame`, which never once returned True).

So every test below is really a test about FREQUENCY and FALSE POSITIVES, not
just about correctness. The negative cases are the point.

⭐ The measurement these encode, from 109,812 real two-line cues:
       any function word ends line 1  : 1 in 7   (13.2%)  <- rejected, wallpaper
       rightward-binding words only   : 1 in 33  (3.0%)   <- shipped
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.subtitle_editor import (cue_stranded_break,        # noqa: E402
                                     cue_midword_capital,
                                     drop_recurring_words)


def _cue(text):
    return {'index': 1, 'start': '00:00:01,000', 'end': '00:00:02,000',
            'text': text}


# ─────────────────────────── stranded line breaks ───────────────────────────

def test_stranded_preposition_is_caught():
    """The real defect: line 1 ends on a word that belongs to line 2."""
    assert cue_stranded_break(
        _cue("You weren't part of\nthe Seer's prophecy, Mr. Fitz.")) == 'of'
    assert cue_stranded_break(
        _cue("I'm late for\nmy morning check-in.")) == 'for'
    assert cue_stranded_break(
        _cue("Anyway, he's at\nthe end of the bar.")) == 'at'
    assert cue_stranded_break(
        _cue("and stay in touch with\nthe people we fight for.")) == 'with'


def test_stranded_article_is_caught():
    assert cue_stranded_break(
        _cue("I just have kind of a\ncomplicated relationship.")) == 'a'
    assert cue_stranded_break(
        _cue("He walked straight into the\nroom without knocking.")) == 'the'


def test_conjunction_at_end_of_line_is_NOT_a_defect():
    """⚠️⚠️ THE TEST THAT DEFINES THE BAND.

    Breaking before a conjunction is correct subtitling — the conjunction
    opens the next clause, which is exactly where the phrase wants to bend.
    The first version of this rule flagged these and fired on 1 cue in 7.
    If this test ever goes red, somebody has widened _STRANDED_WORDS without
    re-measuring, and the pane is about to become wallpaper.
    """
    for text in (
        "I might have joined you\nif I'd been 20 years young.",
        "Where's that wisdom come from\nif not from the Lord?",
        "He said he'd be here and\nhe never showed up.",
        "I'd tell you the truth but\nyou wouldn't believe me.",
        "We can go now or\nwe can wait until morning.",
        "She left him because\nhe never listened.",
    ):
        assert cue_stranded_break(_cue(text)) is None, text


def test_pronoun_and_auxiliary_are_NOT_flagged():
    """Also deliberately out of the band — same wallpaper risk."""
    for text in ("Nobody ever told me that I\nwould have to do this alone.",
                 "The only thing he was\nafraid of was the dark.",
                 "I don't think they\nknew what they were doing."):
        assert cue_stranded_break(_cue(text)) is None, text


def test_terminal_punctuation_means_the_break_was_deliberate():
    """"Wait for it. / Now." is two sentences, not a stranded preposition."""
    for text in ("Get in.\nThe car's running.",
                 "Are you in?\nThe others already said yes.",
                 "Stop it!\nThe neighbours will hear.",
                 "I wanted to tell you —\nthe truth is complicated."):
        assert cue_stranded_break(_cue(text)) is None, text


def test_speaker_label_colon_is_not_a_stranded_break():
    """A trailing colon is a speaker label; the colon highlight owns that."""
    assert cue_stranded_break(_cue("CHLOE:\nthe body was moved.")) is None


def test_single_line_and_three_line_cues_are_ignored():
    """A one-line cue has no break to judge; 3+ lines is a different problem."""
    assert cue_stranded_break(_cue("You weren't part of the prophecy.")) is None
    assert cue_stranded_break(
        _cue("You weren't part of\nthe Seer's\nprophecy.")) is None
    assert cue_stranded_break(_cue("")) is None
    assert cue_stranded_break(_cue("   \n   ")) is None


def test_case_and_formatting_tags_do_not_defeat_the_rule():
    """OCR output carries <i> tags; the band must see through them."""
    assert cue_stranded_break(
        _cue("<i>You weren't part of</i>\nthe prophecy.")) == 'of'
    assert cue_stranded_break(
        _cue("You weren't part OF\nthe prophecy.")) == 'OF'


def test_returns_the_word_not_just_true():
    """The Note column says `break: of` — naming the word IS the feature."""
    got = cue_stranded_break(_cue("He was late for\nhis own wedding."))
    assert got == 'for'
    assert isinstance(got, str)


# ───────────────────────── mid-word capitals ─────────────────────────

def test_real_midword_capitals_from_the_lucifer_sweep():
    """Every one of these is a real line from the library (2026-09-19)."""
    assert cue_midword_capital(_cue('But the rock cried out,\n"lI can\'t hide you"')) == 'lI'
    assert cue_midword_capital(_cue('SO hO more games...\nwhat\'s in it?')) == 'hO'
    assert cue_midword_capital(_cue('sO... handsome.')) == 'sO'
    assert cue_midword_capital(_cue('This line of questioning\niS now over.')) == 'iS'
    assert cue_midword_capital(_cue("Ash's eX.")) == 'eX'


def test_ordinary_text_is_never_flagged():
    """⚠️ At 0.4 hits per episode this is the RAREST signal in the pane.
    If ordinary dialogue starts matching, that claim dies and so does the
    precedence it was given."""
    for text in ("I found the body at nine.",
                 "CHLOE: Get down!",
                 "DETECTIVE DECKER is here.",
                 "He said I should go.",
                 "It's a long way to Tipperary.",
                 "Well, THAT went well.",
                 "A B C D E F G",
                 "♪ We are the champions ♪"):
        assert cue_midword_capital(_cue(text)) is None, text


def test_a_word_starting_with_a_capital_can_never_match():
    """McDonald / O'Brien / MacArthur are capital-INITIAL, not capital-inside.
    That is why the detector needs no exception list for them — if this ever
    goes red somebody has loosened the leading [a-z]+ anchor."""
    for text in ("We ate at McDonald's.",
                 "O'Brien called twice.",
                 "General MacArthur returned.",
                 "She works for DeSoto Motors."):
        assert cue_midword_capital(_cue(text)) is None, text


def test_real_brand_names_are_excused():
    """The short allow-list. Each entry is a hole, so it stays short."""
    for text in ("Check my iPhone.", "He bought an iPad.",
                 "It's on eBay.", "Watch it on YouTube.",
                 "Sync it to iTunes."):
        assert cue_midword_capital(_cue(text)) is None, text


def test_an_unlisted_brand_is_still_flagged_and_that_is_correct():
    """⚠️ ACCEPTED COST, recorded so it is not a surprise: a CamelCase brand
    that is not on the list reads as an OCR error. One glance to dismiss, and
    the alternative — a long allow-list — is a long list of blind spots."""
    assert cue_midword_capital(_cue('Posted it on myFace.')) == 'myFace'


def test_returns_the_word_so_the_note_can_name_it():
    got = cue_midword_capital(_cue('sO many questions,'))
    assert got == 'sO'


def test_formatting_tags_do_not_hide_it():
    assert cue_midword_capital(_cue('<i>sO many questions</i>')) == 'sO'


def test_empty_and_missing_text_are_safe():
    assert cue_midword_capital(_cue('')) is None
    assert cue_midword_capital({}) is None


# ───────────────────────── recurring-word filter ─────────────────────────

def test_recurring_word_is_treated_as_a_proper_noun():
    """A name the dictionary never heard of recurs; a typo does not."""
    cues = [_cue('Amenadiel is here.'), _cue('Where is Amenadiel?'),
            _cue('Amenadiel left.'), _cue('He beheves it was poison.')]
    errors = {0: ['Amenadiel'], 1: ['Amenadiel'], 2: ['Amenadiel'],
              3: ['beheves']}
    idx, details = drop_recurring_words(cues, errors)
    assert idx == {3}
    assert details[3] == ['beheves']


def test_a_typo_repeated_three_times_is_sacrificed_on_purpose():
    """⚠️ The known, ACCEPTED cost of the rule — recorded so it is not a surprise.

    The same OCR misread happening 3+ times in one episode gets dismissed as a
    proper noun. That is the trade the -43%/-54% noise reduction is bought
    with. If this ever needs revisiting, the fix is a name list, not a wider
    band.
    """
    cues = [_cue('He beheves it.')] * 3
    idx, _ = drop_recurring_words(cues, {0: ['beheves'], 1: ['beheves'],
                                         2: ['beheves']})
    assert idx == set()


def test_threshold_is_three_not_two():
    """Twice is still a typo; the band starts at three."""
    cues = [_cue('He beheves it.'), _cue('She beheves it.'),
            _cue('Nothing here.')]
    idx, _ = drop_recurring_words(cues, {0: ['beheves'], 1: ['beheves']})
    assert idx == {0, 1}


def test_counting_is_case_insensitive():
    """A name is still a name at the start of a sentence."""
    cues = [_cue('Urich called.'), _cue('I saw urich.'), _cue('URICH again.'),
            _cue('It cohapsed.')]
    idx, _ = drop_recurring_words(cues, {0: ['Urich'], 1: ['urich'],
                                        2: ['URICH'], 3: ['cohapsed']})
    assert idx == {3}


def test_possessive_counts_against_its_root():
    """"Owlsley's" must count toward "Owlsley" or names survive as possessives."""
    cues = [_cue('Owlsley is here.'), _cue('Owlsley left.'),
            _cue("Owlsley's car."), _cue('It cohapsed.')]
    idx, _ = drop_recurring_words(cues, {2: ["Owlsley's"], 3: ['cohapsed']})
    assert 2 not in idx
    assert idx == {3}


def test_cue_keeps_its_other_errors_when_one_word_is_dismissed():
    """Dropping a name must not drop the real typo sharing the cue."""
    cues = ([_cue('Amenadiel spoke.')] * 3
            + [_cue('Amenadiel beheves it.')])
    idx, details = drop_recurring_words(
        cues, {3: ['Amenadiel', 'beheves']})
    assert idx == {3}
    assert details[3] == ['beheves']


def test_empty_input_is_safe_and_does_not_mutate():
    cues = [_cue('Nothing wrong here.')]
    original = {0: ['x']}
    idx, details = drop_recurring_words(cues, {})
    assert (idx, details) == (set(), {})
    drop_recurring_words(cues, original)
    assert original == {0: ['x']}, "must not mutate the caller's dict"
