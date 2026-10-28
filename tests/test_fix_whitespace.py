"""filter_fix_whitespace: no-break / padded spaces from web-sourced subs (2026-10-28)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'modules'))
from subtitle_filters import filter_fix_whitespace  # noqa: E402

NB = ' '


def cue(text):
    return {'index': 1, 'start': '00:00:01,000', 'end': '00:00:02,000', 'text': text}


def test_nbsp_padding_collapses_and_keeps_line_break():
    # The real shape from The Librarians S02E12: every space is U+00A0 and a run
    # of them pads the first line out before the break.
    text = f"yet{NB}they{NB}fail{NB}to{NB}dislodge{NB}{NB} {NB} {NB} \n{NB} {NB} the{NB}sword."
    assert filter_fix_whitespace([cue(text)])[0]['text'] == "yet they fail to dislodge\nthe sword."


def test_words_never_change():
    text = f"Look{NB}at{NB}me,{NB}Merlin,{NB}{NB}{NB}\t\nhow{NB}can{NB}I"
    out = filter_fix_whitespace([cue(text)])[0]['text']
    assert out.split() == text.replace(NB, ' ').split()


def test_padding_only_line_is_dropped_and_empty_cue_removed():
    assert filter_fix_whitespace([cue(f"Hi\n{NB}{NB} {NB}")])[0]['text'] == "Hi"
    assert filter_fix_whitespace([cue(f"{NB} {NB}")]) == []


def test_clean_cue_is_untouched():
    c = cue("Nothing odd here.\nSecond line.")
    assert filter_fix_whitespace([c]) == [c]
