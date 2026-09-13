#!/usr/bin/env python3
"""OCR review pane: the advisory flags, and the SRT writer that must never
emit the cues they flag.

⚠️ WHY THIS FILE EXISTS. ocr_bitmap_subtitle(for_review=True) deliberately
returns cues with NO text so the review pane can flag them — a bitmap with ink
that OCR'd to nothing is invisible in the output and can only be caught there.
That makes an empty cue a NORMAL object inside the review, and a CATASTROPHE if
it reaches a finished .srt as a blank entry. Two hand-rolled SRT writers in
subtitle_editor.py would have done exactly that before being routed through
write_srt_file. These tests pin both halves.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.subtitle_editor import flag_ocr_cue, _real_cue_count   # noqa: E402
from modules.subtitle_ocr import write_srt_file                     # noqa: E402


def _cue(text, **kw):
    d = {'index': 1, 'start': '00:00:01,000', 'end': '00:00:02,000',
         'text': text}
    d.update(kw)
    return d


def test_empty_cue_is_flagged():
    tag, reason = flag_ocr_cue(_cue(''))
    assert tag == 'flag_empty', tag
    assert 'no text' in reason
    # whitespace-only counts as empty too
    assert flag_ocr_cue(_cue('   \n  '))[0] == 'flag_empty'


def test_ordinary_text_is_not_flagged():
    for good in ("Hello there.",
                 "I don't know what you mean.",
                 "— Are you coming?\n— In a minute.",
                 "It's 1974, and nothing is easy.",
                 "Café... naïve, façade",
                 "♪ music plays ♪",
                 "[DOOR CREAKS]"):
        assert flag_ocr_cue(_cue(good))[0] is None, good


def test_junk_characters_are_flagged():
    for bad in ("He|lo there", "what~ever", "a¤b", "x×y"):
        tag, reason = flag_ocr_cue(_cue(bad))
        assert tag == 'flag_junk', (bad, tag)
        assert 'odd char' in reason


def test_almost_no_letters_is_flagged():
    assert flag_ocr_cue(_cue('...'))[0] == 'flag_short'
    assert flag_ocr_cue(_cue('- -'))[0] == 'flag_short'
    # a single real word is fine
    assert flag_ocr_cue(_cue('No.'))[0] is None


def test_flags_never_filter():
    """A flag must be advisory only — it may not remove or alter the cue."""
    c = _cue('')
    before = dict(c)
    flag_ocr_cue(c)
    assert c == before, "flag_ocr_cue mutated the cue"


def test_real_cue_count_ignores_empties():
    cues = [_cue('one'), _cue(''), _cue('two'), _cue('   ')]
    assert _real_cue_count(cues) == 2
    assert _real_cue_count([]) == 0
    assert _real_cue_count(None) == 0


def test_write_srt_skips_empties_and_renumbers():
    """⚠️ The one that matters: review cues must never reach a real .srt."""
    cues = [_cue('one', index=1),
            dict(_cue('', index=2), empty=True),
            _cue('three', index=3),
            dict(_cue('  ', index=4), empty=True),
            _cue('five', index=5)]
    path = os.path.join(tempfile.mkdtemp(), 'out.srt')
    write_srt_file(cues, path)
    out = open(path, encoding='utf-8').read()

    assert 'one' in out and 'three' in out and 'five' in out
    # numbering must be contiguous 1..3, NOT the original 1,3,5
    nums = [ln.strip() for ln in out.splitlines() if ln.strip().isdigit()]
    assert nums == ['1', '2', '3'], nums
    # no blank cue bodies
    for block in out.strip().split('\n\n'):
        lines = [x for x in block.splitlines() if x.strip()]
        assert len(lines) >= 3, f"blank cue leaked: {block!r}"


# ── Filtering and empty-removal ──────────────────────────────────────────────
# ⚠️ Tony's two concerns, verbatim: "without losing the bitmaps or causing the
# cues to get out of order". These pin both.

from modules.subtitle_editor import (            # noqa: E402
    cue_passes_ocr_filter, drop_empty_cues,
    FILTER_ALL, FILTER_FLAGGED, FILTER_EMPTY, FILTER_EDITED)


def _review_cues():
    return [
        {'index': 1, 'start': 'a', 'end': 'b', 'text': 'one',   'img': '/t/1.bmp'},
        {'index': 2, 'start': 'a', 'end': 'b', 'text': '',      'img': '/t/2.bmp'},
        {'index': 3, 'start': 'a', 'end': 'b', 'text': 'thr|ee', 'img': '/t/3.bmp'},
        {'index': 4, 'start': 'a', 'end': 'b', 'text': 'four',  'img': '/t/4.bmp',
         'edited': True},
        {'index': 5, 'start': 'a', 'end': 'b', 'text': '   ',   'img': '/t/5.bmp'},
    ]


def test_filter_all_shows_everything():
    cues = _review_cues()
    assert all(cue_passes_ocr_filter(c, FILTER_ALL) for c in cues)


def test_filter_selects_the_right_cues():
    cues = _review_cues()
    empty = [i for i, c in enumerate(cues)
             if cue_passes_ocr_filter(c, FILTER_EMPTY)]
    assert empty == [1, 4], empty
    edited = [i for i, c in enumerate(cues)
              if cue_passes_ocr_filter(c, FILTER_EDITED)]
    assert edited == [3], edited
    flagged = [i for i, c in enumerate(cues)
               if cue_passes_ocr_filter(c, FILTER_FLAGGED)]
    assert flagged == [1, 2, 4], flagged      # 2 empties + the '|' junk cue


def test_filtering_never_mutates_cues():
    cues = _review_cues()
    import copy
    before = copy.deepcopy(cues)
    for mode in (FILTER_ALL, FILTER_FLAGGED, FILTER_EMPTY, FILTER_EDITED):
        for c in cues:
            cue_passes_ocr_filter(c, mode)
    assert cues == before, "filtering altered the cue list"


def test_filtered_view_maps_back_to_true_indices():
    """⚠️ The order concern. A filtered view shows rows 0,1,2 — but they must
    map to the ORIGINAL cue indices, or an edit lands on the wrong cue."""
    cues = _review_cues()
    view = [(i, c) for i, c in enumerate(cues)
            if cue_passes_ocr_filter(c, FILTER_FLAGGED)]
    assert [i for i, _ in view] == [1, 2, 4]
    # editing the 2nd visible row must hit cue 2, not cue 1
    true_idx = view[1][0]
    cues[true_idx]['text'] = 'fixed'
    assert cues[2]['text'] == 'fixed'
    assert cues[1]['text'] == ''          # untouched
    assert cues[4]['text'] == '   '       # untouched


def test_drop_empty_preserves_order_and_bitmaps():
    cues = _review_cues()
    imgs_before = [c['img'] for c in cues if (c.get('text') or '').strip()]
    texts_before = [c['text'] for c in cues if (c.get('text') or '').strip()]
    gone = drop_empty_cues(cues)
    assert gone == 2, gone
    assert [c['text'] for c in cues] == texts_before, "order changed"
    assert [c['img'] for c in cues] == imgs_before, "bitmap lost or remapped"
    assert [c['index'] for c in cues] == [1, 2, 3], "not renumbered contiguously"


def test_drop_empty_is_in_place():
    """⚠️ The caller holds this exact list (ocr_result[0]); rebinding would
    leave every other reference pointing at the old one."""
    cues = _review_cues()
    same = cues
    drop_empty_cues(cues)
    assert same is cues and len(same) == 3


def test_drop_empty_on_clean_list_is_a_noop():
    cues = [{'index': 1, 'text': 'a'}, {'index': 2, 'text': 'b'}]
    assert drop_empty_cues(cues) == 0
    assert [c['text'] for c in cues] == ['a', 'b']


if __name__ == '__main__':
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except AssertionError as e:
                fails += 1
                print(f"  FAIL  {name}: {e}")
    print(f"\n{'all passed' if not fails else str(fails) + ' FAILED'}")
    sys.exit(1 if fails else 0)
