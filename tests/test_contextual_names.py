#!/usr/bin/env python3
"""Custom names that are also ordinary English words.

⚠️⚠️ WHY THIS EXISTS. Tony, 2026-09-20: *"I've been handing them to you
because you do a way better job than the filter so if we can fix the filter
then that would be better."* He had been passing all-caps shows to Arthur by
hand — a manual step that existed only because Fix ALL CAPS fell short.

Salem is the case that breaks the old design: its main characters are
**Mercy, Increase, Cotton, John and Hale**. Adding those to custom_cap_words
used to capitalise every occurrence, so "have mercy on me" became "have Mercy
on me" and "a cotton shirt" became "a Cotton shirt" — worse than leaving them
alone. Leaving them out meant ~356 lowercase character names across 26
episodes.

The fix: a custom name the system dictionary knows as a common word is
applied ONLY next to an anchor. One that is not — Tituba, Samhain, Kenaima —
is applied everywhere, exactly as before. The split is automatic, so Tony
never has to classify a name when he adds it.

Measured on the 26 Salem episodes:

    filter before          —    ~356 lowercase
    Arthur by hand        400     178
    filter after          432     146     <- better than the manual pass

⛔ THE DESIGN THAT WAS REJECTED, so nobody rebuilds it: using the 12,033
names the DB *discards* as the candidate set. 32 of 33 obviously-dangerous
words are in there — The, And, But, You, Will, May, God, Sir, Let — because a
1.1M name list contains nearly every short word as somebody's name. "the
Alden house" would have become "The Alden house". Only a list Tony curated
himself is safe.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules import subtitle_filters as sf                     # noqa: E402

NAMES = ['Tituba', 'Mercy', 'Increase', 'Cotton', 'John', 'Hale',
         'Alden', 'Sibley', 'Mather', 'Lewis', 'Anne']


def _cue(text):
    return {'index': 1, 'start': '00:00:01,000', 'end': '00:00:02,000',
            'text': text}


def _fix(text, names=None):
    return sf.filter_fix_caps([_cue(text)], names or NAMES,
                              use_names_db=True)[0]['text']


# ────────────────────────────── the split ──────────────────────────────

def test_the_split_is_automatic():
    """⭐ Tony never classifies a name. The dictionary decides."""
    glob_, ctx = sf._split_custom_names(NAMES)
    assert 'Tituba' in glob_ and 'Mather' in glob_
    for n in ('Mercy', 'Increase', 'Cotton', 'John', 'Hale'):
        assert n in ctx, f'{n} is an English word and must be contextual'


def test_multi_word_names_are_always_global():
    """"Mary Sibley" needs no anchor — the phrase IS the disambiguation."""
    glob_, ctx = sf._split_custom_names(['Mary Sibley', 'Mercy'])
    assert 'Mary Sibley' in glob_
    assert ctx == ['Mercy']


# ──────────────────── an anchor proves it is a name ────────────────────

def test_ambiguous_name_before_a_known_surname():
    assert _fix('MERCY LEWIS BLEEDS.') == 'Mercy Lewis bleeds.'
    assert _fix('COTTON MATHER EXAMINED ME.') == 'Cotton Mather examined me.'
    assert _fix('THE ALMIGHTY INCREASE MATHER.') == 'The almighty Increase Mather.'


def test_the_possessive_does_not_break_the_anchor():
    """⚠️ The names DB holds "Alden", not "Alden's". The first pass over the
    real Salem files missed 12 occurrences of "john Alden's" for exactly this
    reason — strip the possessive before the lookup, both apostrophes."""
    assert _fix("TELL ME JOHN ALDEN'S SECRET.") == "Tell me John Alden's secret."
    assert _fix('TELL ME JOHN ALDEN’S SECRET.') == 'Tell me John Alden’s secret.'


def test_a_title_is_an_anchor():
    assert _fix('COME ALONG, MISS MERCY.') == 'Come along, Miss Mercy.'


def test_the_anchor_can_be_on_the_LEFT():
    """"Anne hale" — the ambiguous name is the SURNAME here. Measured: 33
    occurrences of <known name> <ambiguous name> across 26 episodes, 32 of
    them "anne hale" and correct."""
    assert _fix('ANNE HALE ARRIVED.') == 'Anne Hale arrived.'


# ─────────────────── no anchor means leave it alone ───────────────────

def test_the_common_noun_sense_is_NOT_capitalised():
    """⚠️⚠️ THE TESTS THE WHOLE DESIGN EXISTS FOR. If any of these go red,
    the filter is now actively damaging ordinary sentences."""
    assert _fix('HAVE MERCY ON ME.') == 'Have mercy on me.'
    assert _fix('A COTTON SHIRT.') == 'A cotton shirt.'
    assert _fix('AN INCREASE IN TAXES.') == 'An increase in taxes.'
    assert _fix('SHE IS HALE AND HEARTY.') == 'She is hale and hearty.'


def test_a_vocative_has_no_anchor_and_stays_lowercase():
    """⚠️ ACCEPTED COST, recorded so it is not a surprise. "Where are you,
    john" is genuinely a name, and there is no signal to prove it. 146 of
    these remain across 26 Salem episodes. A rule that capitalised them would
    be wrong more often than right — "have mercy" outnumbers them."""
    assert _fix('WHERE ARE YOU, JOHN?') == 'Where are you, john?'


def test_an_ordinary_word_before_a_name_is_not_dragged_in():
    """⛔ THE REGRESSION GUARD for the rejected design. "the"/"and" must never
    anchor, or every sentence would start acquiring capitals mid-flow."""
    assert _fix('THE MATHER HOUSE.') == 'The Mather house.'
    assert _fix('AND MATHER SAID SO.') == 'And Mather said so.'


# ───────────────────── unambiguous names are unchanged ─────────────────────

def test_a_name_that_is_not_a_word_still_applies_everywhere():
    """⚠️ The old behaviour must survive for the names it was right about."""
    assert _fix('TITUBA IS HERE.') == 'Tituba is here.'
    assert _fix('I SPOKE TO TITUBA.') == 'I spoke to Tituba.'
    assert _fix('SHE FEARED TITUBA MOST.') == 'She feared Tituba most.'


def test_no_custom_names_at_all_is_safe():
    out = sf.filter_fix_caps([_cue('HAVE MERCY ON ME.')], [], use_names_db=True)
    assert out[0]['text'] == 'Have mercy on me.'
    out = sf.filter_fix_caps([_cue('HAVE MERCY ON ME.')], None, use_names_db=True)
    assert out[0]['text'] == 'Have mercy on me.'


def test_the_filter_still_will_not_rerun_on_its_own_output():
    """⚠️ The 60% guard is what makes conversion effectively one-way. Pinned
    here because the contextual pass could have tempted someone to relax it."""
    once = _fix('HAVE MERCY ON ME.')
    twice = _fix(once)
    assert once == twice
