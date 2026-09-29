#!/usr/bin/env python3
"""A letter I/l standing in for the digit 1 inside a number.

⚠️⚠️ WHY THIS FILE EXISTS — Tony, 2026-09-29: *"OCR tends to make 1's I's so a
number with one in it could have I instead."* From a live review, two lines
apart in the same scene:

    Your grapple's only got 1,000 feet of cable.        <- correct
    ...should be able to dive at least I,000,           <- the same number

⭐ IT WAS INVISIBLE TO EVERY OTHER CHECK, and that is the point. words_of()
keeps only ALPHABETIC tokens of 4+ characters, so a number never reaches
check_confusions; and "I,000" is not a word, so the spell checker has no
opinion. The reverse case (a 1 inside a word, "1ike") was always caught — the
substitution check owns it. Only the numeric direction was unowned.

⭐⭐ THE MEASUREMENT IS THE REASON THIS SHIPPED, and it very nearly did not.
Run 2026-09-29 over 216,455 cues from 400 library .srt files:

    WITH '-' in the token    6 hits,  ALL of them US interstates (I-84, I-90)
                             -> 0% precision, would have trained him to ignore
                                the Note column entirely
    WITHOUT '-'              0 false positives in 216,455 cues

The interstate tests below are that finding, frozen. ⛔ If someone adds the
hyphen back to _DIGIT_CONFUSION_RE, these fail — which is the whole idea.
"""
import pytest

from modules.ocr_suspect import check_digit_letter_confusion as check
from modules.subtitle_editor import flag_ocr_cue, ocr_suspect_words


# ── Must fire ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize('text,tok,fix', [
    ('we know the Jumper should be able to dive at least I,000,',
     'I,000', '1,000'),                      # Tony's actual cue
    ('It was I0 past midnight.', 'I0', '10'),
    ('He owes me 2I dollars.', '2I', '21'),
    ('l5 minutes to go.', 'l5', '15'),       # lowercase L
    ('Meet me at I0:30.', 'I0:30', '10:30'),
    ('About I,500 feet.', 'I,500', '1,500'),
    ('It cost $I.50', 'I.50', '1.50'),
])
def test_catches_the_letter_for_one(text, tok, fix):
    assert check(text) == [(tok, fix)]


# ── Must never fire ─────────────────────────────────────────────────────────

@pytest.mark.parametrize('text', [
    'I am going home.',                      # the pronoun — no digit
    "I'll be there, I'm sure.",
    'I said I would.',
    "Your grapple's only got 1,000 feet of cable.",   # already correct
    'We need 2,000 more.',
    'It is 10:30 already.',
    'Chapter III begins.',                   # roman numerals — no digit
    'He came 1st.',                          # 's','t' break the token
    'That is 5ml of fluid.',                 # 'm' breaks the token
    'A 1080i broadcast.',                    # ⚠️ lowercase i is NOT a
                                             # confusion char, deliberately
])
def test_leaves_good_text_alone(text):
    assert check(text) == []


@pytest.mark.parametrize('text', [
    'In two miles, take the onramp towards the I-84 north.',
    'She stopped short on the I-90 once,',
    'Get on I-95 and head south.',
])
def test_interstates_are_not_flagged(text):
    """⛔ THE REGRESSION THAT MEASUREMENT CAUGHT.

    These were 6 of 6 hits — 100% of them — in the first run over 216,455
    real cues. Excluding '-' from the token makes "I-84" split into "I" and
    "84", neither of which qualifies. Put the hyphen back and this fails.
    """
    assert check(text) == []


# ── Wiring into the review pane ─────────────────────────────────────────────

def test_it_reaches_the_note_column():
    cue = {'text': 'we know the Jumper should be able to dive at least I,000,'}
    assert flag_ocr_cue(cue) == ('flag_ocr', 'I,000 -> 1,000?')


def test_the_menu_can_offer_it():
    text = 'we know the Jumper should be able to dive at least I,000,'
    assert [w for w, _ in ocr_suspect_words(text)] == ['I,000']


def test_per_episode_ignore_applies_to_it_too():
    cue = {'text': 'dive at least I,000,'}
    assert flag_ocr_cue(cue)[0] == 'flag_ocr'
    assert flag_ocr_cue(cue, {'i,000'}) == (None, '')


def test_a_numeric_token_is_not_offered_as_a_name():
    """⚠️ add_user_name() refuses a non-alphabetic token, so the menu must not
    offer "add it to the names list" for "I,000" — an action that silently does
    nothing is worse than no action. The menu gates on isalpha(); this pins the
    property that gate depends on."""
    from modules.subtitle_filters import add_user_name
    assert add_user_name('I,000') is False
    assert not 'I,000'.replace("'", '').isalpha()
