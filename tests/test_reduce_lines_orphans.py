#!/usr/bin/env python3
"""Reduce to 2 Lines must not strand the first word on its own line.

⚠️⚠️ WHY THIS FILE EXISTS — Tony, 2026-09-30: *"The 'Reduce to two lines' filter
is cutting at the first comma and in many cases, that's after the first word of
the sentence."* Reproduced exactly:

    'Yeah,'                                                      ( 5)
    'we know the Jumper should be able to dive at least 1,000,'  (57)

⭐ THE CAUSE: `_best_in_tier` balances WITHIN a tier, but the five tiers are
ABSOLUTE. One comma after word one was the only tier-2 candidate, so it won by
default — and tier 5 (midpoint), which would have given a 31/31 split, was never
consulted. There was no floor on how lopsided a tier winner could be.

⭐⭐ THE ASYMMETRY THAT MAKES THIS SUBTLE, and the reason the floor is NOT
applied to tier 1: a SENTENCE boundary is semantically load-bearing and earns
its own line even when short. 'Holy shit.' (10) followed by a 51-char sentence
is CORRECT — forcing balance there would break the second sentence mid-phrase.
A comma after "Yeah" carries no such meaning. Both shapes are pinned below;
a fix that "improves" the tier-1 cases has broken something.

⚠️ MIN_SHORT_LINE = 12 is tuned against observed failures, NOT a large corpus —
raw multi-line OCR output never reaches disk (it exists only inside the review
pane), so there is nothing to measure at scale. These tests are the record of
what it was tuned to.
"""
import pytest

from modules.subtitle_filters import filter_reduce_lines


def reflow(text):
    return filter_reduce_lines([{'text': text}])[0]['text']


def lines(text):
    return reflow(text).split('\n')


# ── The reported bug ────────────────────────────────────────────────────────

@pytest.mark.parametrize('text', [
    'Yeah,\nwe know the Jumper should\nbe able to dive at least 1,000,',
    'Well, I can tell you\nwhen they up\nthe dosage,',
    'So,\nif he is above 2,000\nwe should still be able\nto get him with the grapple.',
])
def test_no_orphan_first_word_at_a_comma(text):
    """⛔ THE REGRESSION. Before the fix these produced 5/57, 5/39 and 3/73."""
    out = lines(text)
    assert len(out) == 2
    assert min(len(out[0]), len(out[1])) >= 12, (
        f'stranded an orphan line: {out!r}')


def test_the_exact_cue_tony_reported():
    out = lines('Yeah,\nwe know the Jumper should\nbe able to dive at least 1,000,')
    assert out[0] != 'Yeah,', 'the orphan is back'
    assert len(out[0]) >= 12 and len(out[1]) >= 12


# ── What must NOT change: sentence boundaries keep their short line ──────────

@pytest.mark.parametrize('text,short', [
    ('Holy shit.\nProtests demanding justice\nfor lost ship Canterbury', 'Holy shit.'),
    ('Exit for Star Helix\nPolice and Emergency Services.\nExcuse me.', 'Excuse me.'),
])
def test_tier1_sentence_breaks_are_exempt_from_the_floor(text, short):
    """⚠️ A 10-char line is CORRECT when it is a whole sentence. If a future
    change applies MIN_SHORT_LINE to tier 1, these fail — which is the point."""
    assert short in lines(text)


def test_a_short_but_complete_clause_survives():
    """'He walked in' is 12 chars — a complete clause, right at the floor."""
    out = lines('He walked in\nand sat down without\nsaying anything at all.')
    assert out[0] == 'He walked in'


# ── The guard that matters most ─────────────────────────────────────────────

@pytest.mark.parametrize('text', [
    'Yeah,\nwe know the Jumper should\nbe able to dive at least 1,000,',
    'Well, I can tell you\nwhen they up\nthe dosage,',
    'So,\nif he is above 2,000\nwe should still be able\nto get him with the grapple.',
    'He walked in\nand sat down without\nsaying anything at all.',
    'Holy shit.\nProtests demanding justice\nfor lost ship Canterbury',
    'One\ntwo\nthree\nfour\nfive\nsix\nseven\neight\nnine\nten eleven twelve.',
])
def test_no_words_are_lost(text):
    """⛔⛔ THE NON-NEGOTIABLE ONE. `balance_lines` in the Whisper cue path was
    SILENTLY DELETING WORDS and it was caught only by asserting this. Any
    reflow change must prove words_in == words_out.
    (see project_whisper-cue-segmentation)"""
    assert len(reflow(text).split()) == len(text.split())


# ── Untouched behaviour ─────────────────────────────────────────────────────

def test_two_line_cues_are_left_alone():
    """The filter only acts on cues EXCEEDING max_lines. A 2-line cue is
    already compliant — this is why the library shows almost no reflows."""
    t = 'Already two lines\nand perfectly fine.'
    assert reflow(t) == t


def test_single_line_is_left_alone():
    t = 'Just the one line here, nice and short.'
    assert reflow(t) == t


def test_dialogue_dashes_still_split_by_speaker():
    """Speaker markers are handled before any tier and must stay that way."""
    out = lines('- Where are you going?\n- To find him\nbefore they do.')
    assert len(out) == 2
    assert out[0].startswith('-') and out[1].startswith('-')
