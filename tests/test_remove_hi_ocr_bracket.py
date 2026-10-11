"""Remove HI must never delete a line just because it has an unclosed "[".

Tony, 2026-10-11: Remove HI alone kept "<i>[ was there.</i>", but HI + Remove
Tags deleted the cue. The OCR panel runs its filters twice; the first pass
stripped the italics, and the second pass saw an unclosed "[" at the start of
the line and took it for a cut-off HI tag. It is just as often a capital I
OCR'd as "[", or a tag whose "]" was OCR'd as "|" with dialogue after it.

    python3 -m pytest -q tests/test_remove_hi_ocr_bracket.py
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.subtitle_filters import (                          # noqa: E402
    filter_remove_hi, filter_remove_tags)


def _run(text, *filters):
    cues = [{'index': 1, 'start': 'a', 'end': 'b', 'text': text}]
    for f in filters or (filter_remove_hi,):
        cues = f(cues)
    return cues[0]['text'] if cues else None


@pytest.mark.parametrize("text", [
    "[ think so.",
    "[ don't know.",
    "[t's time to send",
    "['m fine.",
    "[Hank| Good luck to you!",
    "[ was there.\n- No, you weren't.",
])
def test_an_unclosed_bracket_line_is_kept(text):
    assert _run(text) == text


def test_tags_then_hi_twice_keeps_the_line():
    """The exact path Tony hit: the panel's two passes over HI + Tags."""
    two_pass = (filter_remove_hi, filter_remove_tags) * 2
    assert _run("<i>[ was there.</i>", *two_pass) == "[ was there."


def test_closed_tags_still_go():
    assert _run("[DOOR SLAMS]\nGet in the car!") == "Get in the car!"
    assert _run("[Indistinct\nconversation]") is None
