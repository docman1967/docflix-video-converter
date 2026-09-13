#!/usr/bin/env python3
"""Remove HI must not eat ordinary lines that contain a colon.

⚠️⚠️ TWO BUGS, ONE CAUSE. `filter_remove_hi`'s speaker pattern allows SPACES
inside the name — `[A-Z][A-Za-z\\s\\d'.#]{1,29}[A-Za-z\\d]:` — so it matches
most of a sentence that happens to contain a colon. That produced two distinct
failures:

  1. Colon at the END of a line -> the whole line matched, was stripped, and
     the now-empty cue was DELETED. Tony, 2026-09-13: "a lot of times the first
     cue reads Previously on show_name: ... that whole cue disappears."
     Also killed: "Next week on X:", "Chapter One:", "Dear John:".

  2. Colon MID-line -> everything before it was deleted and the cue survived
     looking fine. "Here's the deal: you leave now." -> "you leave now."
     This one is quieter and worse: nothing is missing from the file, so
     there is no gap to notice.

Measured over 223,811 cues from 250 of Tony's subtitle files: the fix rescues
44 cues from outright deletion and 153 from truncation, with 0 regressions.
⚠️ Far higher on OCR'd PGS specifically — "NARRATOR: Previously on <show>:" is
the FIRST CUE OF EVERY EPISODE of a great many series.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.subtitle_filters import (                          # noqa: E402
    filter_remove_hi, _looks_like_speaker_label)


def _run(text):
    """Return the filtered text, or None if the cue was dropped."""
    out = filter_remove_hi([{'index': 1, 'start': 'a', 'end': 'b',
                             'text': text}])
    return out[0]['text'] if out else None


# ── 1. Colon at the end of the line: the cue must survive ───────────────────

@pytest.mark.parametrize("text", [
    "Previously on Warehouse 13:",
    "NARRATOR: Previously on Warehouse 13:",
    "Next week on Warehouse 13:",
    "Chapter One:",
    "Dear John:",
    "Meanwhile in Boston:",
    "Tonight's top story:",
    "Rule number one:",
    "You have one of two options:",
    "And the cab driver said to me:",
])
def test_line_ending_in_a_colon_is_not_deleted(text):
    """⭐ Tony's find. Every one of these was measured being deleted."""
    got = _run(text)
    assert got is not None, f"CUE DELETED: {text!r}"
    assert got.strip(), f"cue emptied: {text!r}"


def test_narrator_label_still_comes_off_the_front():
    """The label goes; the sentence it introduces stays."""
    assert _run("NARRATOR: Previously on Warehouse 13:") == \
        "Previously on Warehouse 13:"


# ── 2. Colon mid-line: the front of the line must survive ───────────────────

@pytest.mark.parametrize("text", [
    "Here's the deal: you leave now.",
    "One more thing: be careful.",
    "I'll tell you what I think: nothing.",
    "Military taught me three things:",
    "That's all you gotta say:",
])
def test_midline_colon_keeps_the_front_of_the_line(text):
    """⚠️ The quiet one. Nothing is missing from the FILE, so there is no gap
    to notice — the cue is just half of what it should be."""
    assert _run(text) == text, f"front of the line eaten: {text!r}"


# ── What must STILL be removed ──────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("MAN: Over here", "Over here"),
    ("NARRATOR: The story begins.", "The story begins."),
    ("MAN 2: Gold clear.", "Gold clear."),
    ("Dr. Smith: Come in.", "Come in."),
    ("Ron: Hello there.", "Hello there."),
    ("MYKA: Gordon Letanik?", "Gordon Letanik?"),
])
def test_real_speaker_labels_are_still_stripped(text, expected):
    assert _run(text) == expected


@pytest.mark.parametrize("text", [
    "[DOOR SLAMS]", "(sighs)", "Announcer:", "Ron:",
    "- \n-", "-\n-",
])
def test_genuine_hi_and_empty_dashes_are_still_removed(text):
    """⚠️ `- \\n-` is an EMPTY two-speaker dialogue pair and must still go.

    The first cut of the never-empty rule counted raw whitespace tokens, so it
    saw "-" and "-" as two words and resurrected 152 of these. Count only
    tokens containing a letter or digit."""
    assert _run(text) is None, f"should have been removed: {text!r}"


# ── The label-shape rule ────────────────────────────────────────────────────

@pytest.mark.parametrize("name", [
    "NARRATOR", "CHILDREN", "MAN 2", "Dr. Smith", "Ron", "AVA", "Woman",
])
def test_label_shapes_accepted(name):
    assert _looks_like_speaker_label(name), name


@pytest.mark.parametrize("name", [
    "Here's the deal", "One more thing", "Tonight's top story",
    "Previously on Warehouse 13", "Meanwhile in Boston",
    "I'll tell you what I think",
])
def test_prose_is_not_a_label(name):
    """⚠️ A lowercase word is the tell. Measured: 85% of real labels are one
    word, 95% are three or fewer, and every one is capitalised/ALL-CAPS/numeric
    per word."""
    assert not _looks_like_speaker_label(name), name


def test_label_word_limit():
    assert not _looks_like_speaker_label("One Two Three Four")


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-q']))
