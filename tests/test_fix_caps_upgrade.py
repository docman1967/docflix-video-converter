"""Fix ALL CAPS: what four shows of hand repair taught it, 2026-10-30."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from modules.subtitle_filters import _measure_case_damage, filter_fix_caps


def fix(*lines, **kw):
    cues = [{'text': t} for t in lines]
    return [c['text'] for c in filter_fix_caps(cues, **kw)]


def test_dotted_acronyms_stay_one_token():
    assert fix('THE CHICAGO P.D. ARRIVED') == ['The Chicago P.D. arrived']
    assert fix('THE U.S. WAS PROBABLY') == ['The U.S. was probably']
    assert fix('THE L.E.D. LIGHTS AND THE PVC PIPE') == ['The L.E.D. lights and the PVC pipe']


def test_time_abbreviations_stay_lowercase():
    assert fix('AT 9 A.M. THEY LEFT') == ['At 9 a.m. they left']


def test_after_a_dotted_acronym_a_name_keeps_its_capital():
    assert fix('THE U.S. NAVY SAILED') == ['The U.S. Navy sailed']


def test_proper_words_and_phrases():
    assert fix('400 DEGREES FAHRENHEIT') == ['400 degrees Fahrenheit']
    assert 'Second World War' in fix('THE SECOND WORLD WAR')[0]
    assert fix('AFTER WORLD WAR II') == ['After World War II']
    assert fix('AFTER WORLD WAR IN EUROPE')[0].startswith('After world war in')


def test_ordinary_words_are_not_proper_nouns():
    out = fix('WE VISIT A PLACE ON THE ROAD NEAR THE RIVER')[0]
    assert 'place' in out and 'road' in out and 'river' in out
    assert fix('APPLE PIE IS GOOD') == ['Apple pie is good']
    assert 'apple pie' in fix('WE MAKE APPLE PIE')[0]


def test_anchored_context_words_still_work():
    assert 'Wilshire Boulevard' in fix('ON WILSHIRE BOULEVARD', custom_names=['Wilshire'])[0]


def test_a_label_line_is_kept():
    assert fix('(MARC SUMMERS)\nHELLO THERE', custom_names=['Marc', 'Summers']) == [
        '(Marc Summers)\nHello there']


def test_tags_and_ie_survive():
    assert fix('<i>I SAID HELLO</i>') == ['<i>I said hello</i>']


# This week's mistakes, by hand and by script. They must never come back.
def test_little_miss_leaves_missiles_alone():
    assert 'missiles' in fix('THE MISSILES WERE READY')[0]


def test_ordinals():
    assert '17th' in fix('IN THE 17TH CENTURY')[0]


def damaged_file():
    cues = []
    for i in range(40):
        cues.append({'text': 'the workers mix the batter'})
        cues.append({'text': 'And then they Bake it.'})
    return cues


def test_vote_sees_caps_on_continuation():
    assert _measure_case_damage(damaged_file())['caps_on_continuation']


def clean_file():
    cues = []
    for i in range(40):
        cues.append({'text': 'The workers mix the batter'})
        cues.append({'text': 'and then they bake it.'})
        cues.append({'text': 'After that it goes in the oven'})
        cues.append({'text': 'for an hour.'})
    return cues


def test_vote_leaves_clean_files_alone():
    assert not any(_measure_case_damage(clean_file()).values())


def test_continuation_repair_lowers_the_start():
    out = filter_fix_caps(damaged_file())
    assert out[1]['text'].startswith('and then')


def test_clean_text_is_untouched():
    cues = clean_file() + [
        {'text': 'The U.S. Navy and the U.S. Air Force'},
        {'text': 'sailed in the north of the island.'},
        {'text': 'The American west, a Roman road.'},
        {'text': 'Look, a package.'},
    ]
    assert [c['text'] for c in filter_fix_caps(cues)] == [c['text'] for c in cues]


def test_short_capitals_in_a_cased_file_are_left_alone():
    cues = clean_file() + [{'text': 'IED.'}, {'text': 'No, I...'}, {'text': 'VIP.'}]
    out = [c['text'] for c in filter_fix_caps(cues)]
    assert out[-3:] == ['IED.', 'No, I...', 'VIP.']
