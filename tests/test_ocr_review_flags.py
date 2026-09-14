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
    # No 'img' and no 'blank' key -> treated as a blank frame.
    tag, reason = flag_ocr_cue(_cue(''))
    assert tag == 'flag_empty', tag
    assert 'blank' in reason
    # whitespace-only counts as empty too
    assert flag_ocr_cue(_cue('   \n  '))[0] == 'flag_empty'
    # ...but the same empty text WITH a bitmap behind it is the other event.
    assert flag_ocr_cue(_cue('', img='/t/1.bmp'))[0] == 'flag_lost'


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
    """⚠️ Cue 2 is a BLANK frame (no bitmap was ever written) and cue 5 is an
    OCR MISS (a bitmap with ink that read as nothing). Both are 'empty'; only
    the first is safe to discard. Keeping both here is the point of the
    fixture — a test set with only blanks cannot catch the dangerous case."""
    return [
        {'index': 1, 'start': 'a', 'end': 'b', 'text': 'one',   'img': '/t/1.bmp'},
        {'index': 2, 'start': 'a', 'end': 'b', 'text': '',      'img': None,
         'blank': True},
        {'index': 3, 'start': 'a', 'end': 'b', 'text': 'thr|ee', 'img': '/t/3.bmp'},
        {'index': 4, 'start': 'a', 'end': 'b', 'text': 'four',  'img': '/t/4.bmp',
         'edited': True},
        {'index': 5, 'start': 'a', 'end': 'b', 'text': '   ',   'img': '/t/5.bmp',
         'blank': False},
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
    gone = drop_empty_cues(cues)
    assert gone == 1, gone                      # only the blank frame
    assert [c['index'] for c in cues] == [1, 2, 3, 4], "not renumbered"
    assert [c['img'] for c in cues] == ['/t/1.bmp', '/t/3.bmp',
                                        '/t/4.bmp', '/t/5.bmp'], "bitmap lost"
    assert [c['text'] for c in cues] == ['one', 'thr|ee', 'four', '   ']


def test_drop_empty_is_in_place():
    """⚠️ The caller holds this exact list (ocr_result[0]); rebinding would
    leave every other reference pointing at the old one."""
    cues = _review_cues()
    same = cues
    drop_empty_cues(cues)
    assert same is cues and len(same) == 4


def test_drop_empty_on_clean_list_is_a_noop():
    cues = [{'index': 1, 'text': 'a'}, {'index': 2, 'text': 'b'}]
    assert drop_empty_cues(cues) == 0
    assert [c['text'] for c in cues] == ['a', 'b']


# ── ⭐⭐ Blank frame vs lost dialogue ────────────────────────────────────────
# Tony, 2026-09-13: "So are you saying that an empty cue could actually have
# something in it and OCR just didn't find anything?" Yes — and these pin the
# difference, because auto-removing empties would otherwise hide exactly that.

from modules.subtitle_editor import is_blank_frame        # noqa: E402


def test_blank_frame_vs_failed_ocr():
    blank = {'text': '', 'img': None, 'blank': True}
    lost = {'text': '', 'img': '/t/9.bmp', 'blank': False}
    assert is_blank_frame(blank) is True
    assert is_blank_frame(lost) is False
    assert is_blank_frame({'text': 'hello', 'img': None}) is False


def test_failed_ocr_is_never_auto_dropped():
    """⚠️⚠️ THE ONE THAT MATTERS. A bitmap with ink that OCR'd to nothing is a
    missing line of dialogue. Auto-removal must not take it."""
    cues = [{'index': 1, 'text': 'real', 'img': '/t/1.bmp', 'blank': False},
            {'index': 2, 'text': '', 'img': None, 'blank': True},
            {'index': 3, 'text': '', 'img': '/t/3.bmp', 'blank': False}]
    drop_empty_cues(cues)
    survivors = [c['img'] for c in cues]
    assert '/t/3.bmp' in survivors, "LOST DIALOGUE WAS SILENTLY DROPPED"
    assert len(cues) == 2


def test_the_two_empties_flag_differently():
    blank = {'text': '', 'img': None, 'blank': True}
    lost = {'text': '', 'img': '/t/9.bmp', 'blank': False}
    assert flag_ocr_cue(blank)[0] == 'flag_empty'
    tag, reason = flag_ocr_cue(lost)
    assert tag == 'flag_lost', tag
    assert 'OCR read nothing' in reason


def test_missing_blank_key_errs_toward_keeping():
    """Old cue dicts have no 'blank'. Guessing wrong must not delete data."""
    legacy_with_bitmap = {'text': '', 'img': '/t/1.bmp'}
    assert is_blank_frame(legacy_with_bitmap) is False
    cues = [dict(legacy_with_bitmap, index=1)]
    assert drop_empty_cues(cues) == 0, "legacy cue with a bitmap was dropped"


def test_blank_only_false_restores_old_behaviour():
    cues = [{'index': 1, 'text': 'a', 'img': '/t/1.bmp'},
            {'index': 2, 'text': '', 'img': '/t/2.bmp', 'blank': False}]
    assert drop_empty_cues(cues, blank_only=False) == 1
    assert len(cues) == 1



# ── Music-note highlighting ─────────────────────────────────────────────────
# ⭐ Tony, 2026-09-13: "If lines were highlighted that contain music notes, it
# would be easier to spot errors."
# ⚠️ Written in this file's plain-assert style on purpose — it has its own
# __main__ runner that walks globals() for test_ functions, so a
# pytest.mark.parametrize decorator would both fail to import and silently
# break `python3 tests/test_ocr_review_flags.py`.

from modules.subtitle_editor import cue_has_music, FILTER_MUSIC   # noqa: E402


def test_music_cues_are_detected():
    for text in ("\u266a Set me free \u266a", "\u266b la la \u266b",
                 "\u2669 one note", "sing \u266c along", "\u266a\u266a\u266a"):
        assert cue_has_music({'text': text}), text


def test_non_music_cues_are_not():
    for text in ("Hello there.", "[FJ]", "[DOOR SLAMS]", "", "MAN: Over here"):
        assert not cue_has_music({'text': text}), text


def test_music_is_not_a_flag():
    """⚠️⚠️ THE POINT. Music cues are ~28% of a musical episode (228 of 822 in
    Glee S01E01). Routing them through the flag would fill "Flagged only" with
    a quarter of the file and destroy the one view that finds real problems."""
    cue = {'text': '\u266a Set me free \u266a'}
    assert cue_has_music(cue)
    assert flag_ocr_cue(cue)[0] is None
    assert not cue_passes_ocr_filter(cue, FILTER_FLAGGED)


def test_music_only_filter_selects_them():
    cues = [{'text': '\u266a la la \u266a'}, {'text': 'Hello.'},
            {'text': 'sing \u266b'}]
    got = [i for i, c in enumerate(cues)
           if cue_passes_ocr_filter(c, FILTER_MUSIC)]
    assert got == [0, 2], got


def test_music_check_never_mutates():
    cue = {'text': '\u266a la la \u266a', 'img': '/t/1.bmp'}
    before = dict(cue)
    cue_has_music(cue)
    cue_passes_ocr_filter(cue, FILTER_MUSIC)
    assert cue == before


# ── Deleting cues ───────────────────────────────────────────────────────────
# ⭐ Tony, 2026-09-13: "I need to be able to delete cues. Some cues slip past
# the filter because a [ is mistaken for an I or L so a cue like Iscreams]
# makes it past the filter." No rule catches those reliably — a human deletes
# them. ⚠️ Plain-assert style to match this file's own __main__ runner.

from modules.subtitle_editor import delete_cues                   # noqa: E402


def _del_cues():
    return [{'index': i + 1, 'text': t, 'img': f'/t/{i + 1}.bmp'}
            for i, t in enumerate(['one', 'Iscreams]', 'three',
                                   '[DOOR]', 'five'])]


def test_delete_one_cue_and_renumber():
    cues = _del_cues()
    assert delete_cues(cues, [1]) == 1
    assert [c['text'] for c in cues] == ['one', 'three', '[DOOR]', 'five']
    assert [c['index'] for c in cues] == [1, 2, 3, 4], "not renumbered"
    assert [c['img'] for c in cues] == ['/t/1.bmp', '/t/3.bmp',
                                        '/t/4.bmp', '/t/5.bmp'], "bitmap lost"


def test_delete_several_at_once():
    cues = _del_cues()
    assert delete_cues(cues, [1, 3]) == 2
    assert [c['text'] for c in cues] == ['one', 'three', 'five']


def test_indices_need_not_be_sorted():
    """⚠️ Removing a low index first shifts everything after it, so a naive
    loop would take the wrong cue on the second pass — the classic off-by-one
    that silently eats a neighbour."""
    cues = _del_cues()
    assert delete_cues(cues, [3, 1]) == 2
    assert [c['text'] for c in cues] == ['one', 'three', 'five']


def test_nothing_selected_is_a_noop():
    cues = _del_cues()
    assert delete_cues(cues, []) == 0
    assert len(cues) == 5


def test_out_of_range_indices_are_ignored():
    cues = _del_cues()
    assert delete_cues(cues, [99, -1, 500]) == 0
    assert len(cues) == 5


def test_delete_is_in_place():
    """⚠️ The caller holds this exact list (ocr_result[0]); rebinding would
    leave every other reference pointing at the old one."""
    cues = _del_cues()
    same = cues
    delete_cues(cues, [0])
    assert same is cues and len(same) == 4


def test_deleting_everything_is_allowed():
    cues = _del_cues()
    assert delete_cues(cues, list(range(5))) == 5
    assert cues == []


# ── Where the selection lands after a delete ────────────────────────────────
# ⭐ Tony, 2026-09-14: "if I delete a cue, it throws me back to the top of the
# file, cue 1. It should just go to the next cue in line." The jump-to-top is
# a DELIBERATE behaviour of _rebuild_cue_tree (his own request the day before,
# for when OCR finishes and when the filter changes) — delete merely inherited
# it. So this is about delete being an exception, not about removing it.

from modules.subtitle_editor import index_after_delete             # noqa: E402


def test_lands_on_the_following_cue():
    cases = [
        (5, [2],    2),      # middle — the cue that was 3 now sits at 2
        (5, [0],    0),      # head — the next cue slides into 0
        (5, [4],    3),      # tail — nothing follows, so land on the new last
        (5, [1, 2], 1),      # a contiguous run of junk, the common case
        (5, [1, 3], 2),      # non-contiguous: survivors 0,2,4 — old 4 at 2
        (5, [0, 4], 2),      # both ends gone
    ]
    for n, idxs, want in cases:
        assert index_after_delete(n, idxs) == want, (n, idxs)


def test_nothing_to_select_returns_none():
    """None means "leave the tree alone" — an empty list has no row to focus,
    and inventing one would make the pane lie about what it holds."""
    for n, idxs in ((1, [0]), (5, list(range(5))), (5, []), (5, [99])):
        assert index_after_delete(n, idxs) is None, (n, idxs)


def test_agrees_with_the_real_delete():
    """⚠️⚠️ THE TEST THAT MATTERS. The others check the formula against
    arithmetic Arthur did in his head; this one tracks the actual cue OBJECT
    through the real delete_cues and asks where it ended up. A formula that
    only agrees with its author's reasoning is not verified.
    """
    for n, idxs in ((5, [2]), (5, [4]), (5, [0]), (5, [1, 3]),
                    (6, [2, 3, 4]), (7, [0, 6]), (4, [1, 2])):
        cues = [{'index': i + 1, 'text': f"cue{i}"} for i in range(n)]
        follow = cues[max(idxs) + 1] if max(idxs) + 1 < n else None
        predicted = index_after_delete(n, idxs)
        delete_cues(cues, idxs)
        if follow is not None:
            actual = next(k for k, c in enumerate(cues) if c is follow)
        else:
            actual = len(cues) - 1 if cues else None
        assert predicted == actual, (n, idxs, predicted, actual)


# ── Colon marking ───────────────────────────────────────────────────────────
# ⭐ Tony, 2026-09-13: "can we highlight : characters? When I'm scrolling
# through, they can get lost in the sea of text." A LOOK-AT-THIS, not a flag —
# 1 cue in 102 across the library, and a surviving colon is usually a long
# speaker label that filter_remove_hi CORRECTLY declined to strip.

from modules.subtitle_editor import cue_has_colon, FILTER_COLON   # noqa: E402


def test_speaker_labels_are_marked():
    for t in ("HOTCH: Today will change everything.", "Announcer:",
              "Two words, honey:", "Question:", "Dr. Smith: Come in."):
        assert cue_has_colon({'text': t}), t


def test_numbered_speaker_labels_are_marked():
    """⚠️ THE BUG THE TEST CAUGHT. The obvious regex `(?<!\\d):(?!\\d)` also
    demands a non-digit BEFORE the colon, so it missed `MAN 2:` and
    `SOLDIER 3:` — numbered labels, exactly the ones worth marking. Only the
    FOLLOWING character distinguishes a clock time."""
    for t in ("MAN 2: Gold clear.", "SOLDIER 3: Move out.", "UNIT 7:"):
        assert cue_has_colon({'text': t}), t


def test_clock_times_stay_quiet():
    """The commonest colon in ordinary dialogue; marking it would drown the
    signal he actually wants."""
    for t in ("at 3:45 tomorrow", "It is 12:30 now", "Meet me at 9:00."):
        assert not cue_has_colon({'text': t}), t


def test_ordinary_text_has_no_colon_marker():
    for t in ("Hello there.", "I don't know.", "[DOOR SLAMS]",
              "\u266a Set me free \u266a", ""):
        assert not cue_has_colon({'text': t}), t


def test_colon_is_not_a_flag():
    """⚠️ 1 in 102 cues — an order of magnitude too common for "Flagged only",
    which exists to make real problems findable."""
    cue = {'text': 'HOTCH: Today will change everything.'}
    assert cue_has_colon(cue)
    assert flag_ocr_cue(cue)[0] is None
    assert not cue_passes_ocr_filter(cue, FILTER_FLAGGED)


def test_colons_only_filter_selects_them():
    cues = [{'text': 'HOTCH: Hello'}, {'text': 'Plain line.'},
            {'text': 'at 3:45'}, {'text': 'MAN 2: Go'}]
    got = [i for i, c in enumerate(cues)
           if cue_passes_ocr_filter(c, FILTER_COLON)]
    assert got == [0, 3], got


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
