#!/usr/bin/env python3
"""Post-processing filters applied inside the OCR review pane.

⚠️ WHY THIS FILE EXISTS. The review pane keeps each cue linked to the bitmap it
was read from ('img'), and that link is the only way to check OCR against the
picture. A filter that rebuilds cue dicts from scratch silently severs it —
`filter_merge_duplicates` did exactly that until 2026-09-13, and reading the
filters would not have caught it. So this runs every filter and checks.

Tony's constraint, verbatim: "without losing the bitmaps or causing the cues to
get out of order".
"""
import copy
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import modules.subtitle_filters as F                          # noqa: E402


def _review_cues():
    return [
        {'index': 1, 'start': '00:00:01,000', 'end': '00:00:02,000',
         'text': '[DOOR SLAMS]', 'img': '/t/1.bmp', 'blank': False},
        {'index': 2, 'start': '00:00:03,000', 'end': '00:00:04,000',
         'text': '<i>Hello</i> there', 'img': '/t/2.bmp', 'blank': False},
        {'index': 3, 'start': '00:00:05,000', 'end': '00:00:06,000',
         'text': 'MAN: Over here', 'img': '/t/3.bmp', 'blank': False},
        {'index': 4, 'start': '00:00:07,000', 'end': '00:00:08,000',
         'text': '♪ la la ♪', 'img': '/t/4.bmp', 'blank': False},
        {'index': 5, 'start': '00:00:09,000', 'end': '00:00:10,000',
         'text': '- Hi.\n- Hello.', 'img': '/t/5.bmp', 'blank': False},
    ]


ALL_FILTERS = sorted(n for n in dir(F) if n.startswith('filter_'))


@pytest.mark.parametrize("name", ALL_FILTERS)
def test_every_filter_preserves_the_bitmap_link(name):
    """⭐ The one that caught filter_merge_duplicates.

    A filter may DROP a cue; it may not return a cue that has lost keys it
    knew nothing about. Dict-spread (`{**cue, 'text': new}`) is the pattern —
    constructing a fresh dict is the bug.
    """
    fn = getattr(F, name)
    try:
        out = fn(copy.deepcopy(_review_cues()))
    except TypeError:
        pytest.skip(f"{name} needs extra arguments")
    if not isinstance(out, list):
        pytest.skip(f"{name} does not return a cue list")
    stripped = [c for c in out if 'img' not in c]
    assert not stripped, (
        f"{name} returned {len(stripped)} cue(s) with no 'img' — the bitmap "
        f"link is severed and the review pane can no longer show the picture")


@pytest.mark.parametrize("name", ALL_FILTERS)
def test_every_filter_preserves_order(name):
    fn = getattr(F, name)
    try:
        out = fn(copy.deepcopy(_review_cues()))
    except TypeError:
        pytest.skip(f"{name} needs extra arguments")
    if not isinstance(out, list) or not out:
        pytest.skip("nothing to check")
    starts = [c['start'] for c in out if 'start' in c]
    assert starts == sorted(starts), f"{name} reordered the cues"


def test_merge_duplicates_keeps_the_first_cues_bitmap():
    """Regression: it rebuilt the merged cue and lost every extra key."""
    cues = [
        {'index': 1, 'start': '00:00:01,000', 'end': '00:00:02,000',
         'text': 'same', 'img': '/t/1.bmp'},
        {'index': 2, 'start': '00:00:02,040', 'end': '00:00:03,000',
         'text': 'same', 'img': '/t/2.bmp'},
    ]
    out = F.filter_merge_duplicates(copy.deepcopy(cues))
    assert len(out) == 1
    assert out[0]['img'] == '/t/1.bmp', "merged cue lost the bitmap"
    assert out[0]['start'] == '00:00:01,000'
    assert out[0]['end'] == '00:00:03,000'


def test_filters_discard_unread_cues_which_is_why_the_pane_warns():
    """⚠️⚠️ DOCUMENTED, NOT DESIRED.

    An unresolved OCR miss has empty text, and the filters drop empty cues.
    So applying filters deletes exactly the cues the review pane exists to
    surface, and they cannot be recovered without re-running OCR. The pane
    therefore asks before applying when any are present (_apply_filters).

    If this test ever fails because the filters started KEEPING empty cues,
    that is good news — but the confirmation prompt should then be revisited
    rather than left to nag about something that no longer happens.
    """
    cues = _review_cues() + [
        {'index': 6, 'start': '00:00:11,000', 'end': '00:00:12,000',
         'text': '', 'img': '/t/6.bmp', 'blank': False}]
    out = F.filter_remove_hi(copy.deepcopy(cues))
    survived = [c for c in out if c.get('img') == '/t/6.bmp']
    assert not survived, (
        "filters now keep unread cues — revisit the warning in _apply_filters")


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-q']))
