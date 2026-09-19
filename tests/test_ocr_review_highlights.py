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

⛔ A "stranded line break" rule lived here for one morning: line 1 ending on
an article or preposition, narrowed from 1-in-7 to 1-in-34 and measured on
109,812 real two-line cues. Tony removed it after using it — "it's cluttering."
Recorded because it is the lesson this file is really about: ACCURATE is not
the same as WELCOME, and only the real pane can tell you which one you built.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.subtitle_editor import (cue_midword_capital,       # noqa: E402
                                     drop_recurring_words,
                                     add_user_word)


class _App:
    """Stand-in for the app object — only what add_user_word touches."""
    def __init__(self, spell=None, cap=None):
        self.custom_spell_words = list(spell or [])
        self.custom_cap_words = list(cap or [])
        self.saves = 0

    def save_preferences(self):
        self.saves += 1


def _cue(text):
    return {'index': 1, 'start': '00:00:01,000', 'end': '00:00:02,000',
            'text': text}


# ─────────────────────────── user dictionary ───────────────────────────
#
# ⚠️ These pin the rules shared by the Spell Check dialog's two buttons AND
# the OCR pane's right-click menu. The two were written separately first —
# if they ever drift, the same word is known in one pane and unknown in the
# other, and the dictionary looks broken.

def test_add_to_dictionary_is_case_insensitive():
    app = _App(spell=['Amenadiel'])
    assert add_user_word(app, 'amenadiel') is False
    assert app.custom_spell_words == ['Amenadiel'], "must not double-add"
    assert app.saves == 0, "a no-op must not write two prefs stores"


def test_add_as_name_goes_into_BOTH_lists():
    """⚠️ cap_words alone would keep it correctly-cased AND still flagged."""
    app = _App()
    assert add_user_word(app, 'Amenadiel', as_name=True) is True
    assert app.custom_cap_words == ['Amenadiel']
    assert app.custom_spell_words == ['Amenadiel']


def test_plain_add_does_NOT_touch_cap_words():
    """"Add to dictionary" means stop flagging it, not "this is a name"."""
    app = _App()
    add_user_word(app, 'bababooey')
    assert app.custom_spell_words == ['bababooey']
    assert app.custom_cap_words == []


def test_cap_words_are_CASE_SENSITIVE():
    """It records how the proper noun is really written — 'chloe' and 'Chloe'
    are not the same entry, which is the entire point of the list."""
    app = _App(cap=['Chloe'])
    assert add_user_word(app, 'chloe', as_name=True) is True
    assert app.custom_cap_words == ['Chloe', 'chloe']


def test_a_name_already_known_as_a_word_still_reaches_cap_words():
    """The half-added case: spelled-ok but never recorded as a name."""
    app = _App(spell=['Decker'])
    assert add_user_word(app, 'Decker', as_name=True) is True
    assert app.custom_cap_words == ['Decker']
    assert app.custom_spell_words == ['Decker'], "no duplicate"
    assert app.saves == 1


def test_saves_once_per_real_change():
    app = _App()
    add_user_word(app, 'Maze')
    add_user_word(app, 'Maze')
    add_user_word(app, 'MAZE')
    assert app.saves == 1, "only the first add changed anything"


def test_never_mutates_a_cue():
    """add_user_word records a judgement; applying a correction is the
    caller's job. Propose, never apply."""
    app = _App()
    cue = _cue('He beheves it.')
    before = dict(cue)
    add_user_word(app, 'beheves')
    assert cue == before


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
