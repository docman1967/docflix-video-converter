#!/usr/bin/env python3
"""The ♪ eraser must not eat a capital J — and must still catch real notes.

⚠️⚠️ WHY THIS FILE EXISTS. Tony, 2026-09-18, on Lucifer:

    "I noticed that capital J's were left off of a lot of cues... I went to the
     original video, and the J is definitely there so it's being pulled at
     extraction. My theory is that the script that does this thinks it's a music
     note. There is always a mis-placed music note when the J is missing."

He was right on all three counts. `_is_note` classified the `J` of "Javier" as a
music note, `strip_notes` erased it from the bitmap before Tesseract ever saw it,
and `reinsert_notes` then put a ♪ where the letter had been.

⭐ THE FAILURE WAS INVISIBLE. The output was not garbled — it was a plausible
lyric-looking cue with a note in it. Only comparing against the video caught it.

⚠️ AND THE SAME MEASUREMENT SHOWED THE ERASER WAS CATCHING NOTHING. Across three
Lucifer episodes (3,077 cues, 183 real notes, 74,773 letters):

      real notes:  onestem 0.846-0.92   hd_h 0.37-0.42
      thresholds:  onestem >= 0.85      hd_h >= 0.44     -> 0 of 183 caught

Every band in `music_notes` was measured on ONE show (Glee S01E22). On a
different subtitle font the window sat entirely off the population: it erased a
letter and caught no notes at all. Arthur's own memory recorded the eraser as
"never fires on real material" — which was true, and was the symptom, not a
reassurance.

⭐ The fix widens the two bounds that rejected real notes (widening cannot newly
reject anything, so it cannot break Glee) and adds a veto that is SPACING, not
proportion: a note-shaped glyph with a NON-note neighbour at intra-word distance
is a letter. Measured against the line's own median gap, so it travels between
fonts. Real notes bottom out at 0.80 of that median; the Javier `J` sat at 0.60.

Fixtures are REAL PGS bitmaps decoded out of the remuxes, not synthetic glyphs —
a fixture that cannot show the failure proves nothing.

Run:  python3 tests/test_note_letter_veto.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from PIL import Image                                          # noqa: E402
from modules import music_notes as M                           # noqa: E402

FIX = os.path.join(HERE, 'fixtures', 'notes')


def _load(name):
    return Image.open(os.path.join(FIX, name))


# ── The bug ─────────────────────────────────────────────────────────────────

def test_capital_J_inside_a_word_is_not_erased():
    """⭐⭐ THE REPORTED BUG. "NAOMI: / You think I killed Javier?" — no music
    anywhere in the cue. The J must survive."""
    img = _load('letter_J_javier.png')
    out, marks = M.strip_notes(img)
    assert marks == [], (
        f"the eraser removed {len(marks)} glyph(s) from a cue with no music in "
        f"it — the J of 'Javier' is being eaten again: {marks}")


def test_the_J_would_have_been_erased_without_the_veto():
    """⚠️ A test that cannot demonstrate the failure proves nothing. With the
    veto disabled the J must still be classified as a note — otherwise this
    whole file is passing for the wrong reason."""
    saved = M._GAP_MIN
    try:
        M._GAP_MIN = 0.0                      # veto can never fire
        _, marks = M.strip_notes(_load('letter_J_javier.png'))
        assert marks, ("with the veto disabled the J is no longer detected as a "
                       "note — the shape bands changed and this test is now vacuous")
    finally:
        M._GAP_MIN = saved


# ── The thing it must not break ─────────────────────────────────────────────

def test_real_notes_are_still_erased():
    """⚠️ The other direction. These are genuine lyric cues; every one must
    still have its notes removed, or Tesseract gets a glyph it cannot encode."""
    for n in (1, 2, 3):
        name = f'note_lyric_{n}.png'
        _, marks = M.strip_notes(_load(name))
        assert marks, f"{name}: a real music cue had nothing erased"


def test_a_note_beside_another_note_survives():
    """⚠️⚠️ TONY'S FIRST QUESTION ABOUT THIS FIX: "a note can have a
    neighbour....another note. How can we work it out so the second note doesn't
    interfere?"

    The veto only considers NON-note neighbours, so ♪♪ is untouched. Proven
    directly against the helper rather than trusting the prose."""
    # two note-shaped glyphs side by side, tight together, plus a distant word
    notes = [((100, 10, 120, 50), 1), ((124, 10, 144, 50), 2)]
    glyphs = [(200, 10, 260, 50)]
    kept, demoted = M._veto_letters(notes, glyphs)
    assert len(kept) == 2, (
        f"a note adjacent to another note was vetoed — ♪♪ is broken ({kept})")


def test_a_note_next_to_a_far_away_word_survives():
    """`♪ Yeah` — a real note has a word space beside it, not letter spacing."""
    notes = [((10, 10, 30, 50), 1)]
    glyphs = [(90, 10, 110, 50), (114, 10, 134, 50), (138, 10, 158, 50)]
    kept, _ = M._veto_letters(notes, glyphs)
    assert len(kept) == 1, "a leading note before a lyric was wrongly vetoed"


def test_the_veto_needs_gap_VARIANCE_not_just_a_close_neighbour():
    """⚠️⚠️ THERE IS DELIBERATELY NO SYNTHETIC VERSION OF THE `J` CASE.

    Arthur wrote one twice and it failed both times, for a reason worth keeping:
    a hand-built line gives every letter an identical gap, so the line's MEDIAN
    equals the letter spacing and the ratio comes out 1.0. Real text is not that
    regular — the Javier line's median was 5px against a 3px J-gap, because
    kerning and word spaces pull the median above the tightest pairs.

    ⭐ So the veto's discriminating power comes from GAP VARIANCE on the line,
    not merely from "has a close neighbour". A uniform synthetic cannot express
    that, and contriving one until it passes would be fitting the fixture to the
    answer. The proof lives in `test_capital_J_inside_a_word_is_not_erased`,
    which runs on the real decoded bitmap.

    This test pins the property that DOES hold synthetically: identical spacing
    everywhere means no veto, which is the same statement as the limitation
    below."""
    notes = [((200, 10, 220, 50), 1)]
    glyphs = [(10, 10, 30, 50), (33, 10, 53, 50), (56, 10, 76, 50),
              (223, 10, 243, 50), (246, 10, 266, 50)]
    kept, _ = M._veto_letters(notes, glyphs)
    assert len(kept) == 1, (
        "uniform letter spacing now triggers the veto — that is a behaviour "
        "change; re-measure against the 183 real notes before trusting it")


def test_known_limitation_a_single_word_line_cannot_be_judged():
    """⚠️⚠️ WRITTEN DOWN BECAUSE IT IS REAL, NOT BECAUSE IT IS FIXED.

    The veto compares a glyph's neighbour gap against its line's MEDIAN gap. On
    a line that is one single word, the median IS the letter spacing, the ratio
    is ~1.0, and the veto cannot fire. A note-shaped letter alone on such a line
    would still be erased.

    ⭐ Measured exposure is small: across 74,773 letters in three episodes only
    THREE cleared the shape bands at all, and the veto caught the one that was
    genuinely a letter. But if a `J`-eating report ever comes back, THIS is the
    first thing to check — pull the cue and count the words on its line.

    ⛔ Do not "fix" this by reaching for a pixel constant. That is what put the
    font's proportions into the shape bands and caused the original bug."""
    notes = [((100, 10, 120, 50), 1)]
    glyphs = [(123, 10, 143, 50), (146, 10, 166, 50)]     # one word, even spacing
    kept, _ = M._veto_letters(notes, glyphs)
    assert len(kept) == 1, (
        "the single-word limitation has changed behaviour — if this now vetoes, "
        "re-measure against real notes before celebrating: it may be over-firing")


# ── Doubles: one note back per note erased ──────────────────────────────────

def test_a_note_only_cue_returns_one_note_per_mark():
    """⚠️⚠️ Tony, 2026-09-18: "it's adding ♪ FE where there should be two music
    notes... there should be ♪♪ instead."

    `reinsert_notes` had a flat `return note` for the no-text case, so a cue that
    is nothing but ♪♪ came back as a single ♪ regardless of how many glyphs were
    erased. ⭐ Doubles are a feature he asked for on 2026-09-14 and the REGEX
    path has emitted one note per J since — so the two halves of the same
    feature disagreed depending on which one fired."""
    assert M.reinsert_notes('', [(0, 'L'), (0, 'L')]) == '♪♪'
    assert M.reinsert_notes('', [(0, 'L')]) == '♪'
    assert M.reinsert_notes('', [(0, 'L')] * 3) == '♪♪♪'


def test_the_real_two_note_bitmap_yields_two_notes():
    """End to end on the actual cue from Tony's screenshot (Pops, 00:06:25)."""
    img = _load('two_notes.png')
    out, marks = M.strip_notes(img)
    assert len(marks) == 2, f"expected two notes erased, got {marks}"
    assert M.reinsert_notes('', marks) == '♪♪'


def test_doubles_around_a_lyric_are_not_collapsed():
    """⚠️ The same flaw lived in the aligned branch: lead/trail were counted and
    then ONE note emitted regardless."""
    got = M.reinsert_notes('I was walking', [(0, 'L'), (0, 'L'), (0, 'R'), (0, 'R')])
    assert got == '♪♪ I was walking ♪♪', got


def test_no_space_between_stacked_notes():
    """⚠️ Matches `_notes_for` in subtitle_ocr, which emits `'♪' * count`. Tony's
    search-and-replace for dropping one of a pair expects the inline form."""
    assert ' ' not in M.reinsert_notes('', [(0, 'L'), (0, 'L')])


# ── The blank page ──────────────────────────────────────────────────────────

def test_an_all_notes_cue_leaves_no_glyph_pixels():
    """⚠️⚠️ Tesseract does NOT return '' for a blank frame — it invents text.

    Tony, 2026-09-18: "I'm getting the 2 music notes plus the FE." After both
    notes were erased from a ♪♪ cue the frame was empty, and Tesseract at
    --psm 6 returned `FE` — deterministically, every time, on 38 surviving
    anti-aliased pixels at value <=15.

    ⚠️ The first guard tested `max() < 10` and did NOT fire, because the fringe
    peaks at 15. The gate has to be the SAME threshold strip_notes uses to call
    something a glyph (>100), not "near zero".

    ⚠️ It is also frame-size dependent: the identical blank content at 216x166
    read as '' while 216x188 read as 'FE'. An isolated reproduction of the cue
    looked fine while the real pipeline was broken — twice.
    """
    import numpy as np
    out, marks = M.strip_notes(_load('two_notes.png'))
    assert len(marks) == 2
    arr = np.asarray(out.convert('L'))
    assert not (arr > 100).any(), (
        f"glyph-level ink survives an all-notes cue (max={arr.max()}) — Tesseract "
        f"would be handed a near-blank frame and will hallucinate on it")


def test_the_ocr_path_skips_tesseract_on_an_emptied_cue():
    """Structural: both OCR routes must bail before the Tesseract call when
    notes were erased and no glyph ink remains.

    ⚠️ TWO call sites on purpose — the PGS route and the DVB/VobSub route. The
    second is the one that historically gets fixed later and stays broken."""
    here = os.path.dirname(os.path.abspath(__file__))
    src = open(os.path.join(here, '..', 'modules', 'subtitle_ocr.py'),
               encoding='utf-8').read()
    n = src.count("> 100).any()")
    assert n >= 2, (
        f"only {n} blank-frame guard(s) found; both OCR paths need one or a "
        f"note-only cue will be handed to Tesseract again")
    assert "_notes and not (" in src, (
        "the guard no longer requires that notes were actually erased — a dim "
        "cue with no notes must still reach Tesseract, or that is lost dialogue")


# ── The review preview must show what was ON SCREEN ─────────────────────────

def test_the_preview_is_rendered_from_the_pre_strip_image():
    """⭐⭐ Tony, 2026-09-18: "the music notes aren't being put back on the
    bitmap like before... it was more of a comfort to be able to look at the
    bitmap and know immediately that the music notes belonged."

    The thumbnail is his EVIDENCE that a ♪ in the text is real. Once the eraser
    started actually working, the saved preview became the post-erase frame and
    the notes disappeared from it — so the text asserted a note the picture no
    longer showed. Tesseract gets the stripped image; the human gets the
    original."""
    here = os.path.dirname(os.path.abspath(__file__))
    src = open(os.path.join(here, '..', 'modules', 'subtitle_ocr.py'),
               encoding='utf-8').read()
    assert 'img_on_screen = img' in src, (
        "the pre-strip frame is no longer kept — the review pane will show "
        "cues whose text claims a ♪ the bitmap does not contain")
    i_keep = src.index('img_on_screen = img')
    i_strip = src.index('_strip_music_notes(img)', i_keep)
    assert i_keep < i_strip, "the frame must be captured BEFORE the notes are erased"
    assert src.count('_save_ocr_preview(img_on_screen') >= 2, (
        "both the blank-cue path and the normal path must save the original "
        "frame; a note-only cue with no thumbnail reads as 'nothing was here'")


def test_the_preview_helper_counts_its_own_failures():
    """⚠️⚠️ The first version of this helper referenced `Image` without
    importing it — `Image` is NOT at module scope in subtitle_ocr — so every
    call raised NameError, a bare `except: pass` ate it, and every note-bearing
    cue came back with no thumbnail. Silent.

    ⛔ A guard that hides its own breakage is the exact failure this module
    produced three times in one day. It must count."""
    here = os.path.dirname(os.path.abspath(__file__))
    src = open(os.path.join(here, '..', 'modules', 'subtitle_ocr.py'),
               encoding='utf-8').read()
    i = src.index('def _save_ocr_preview')
    body = src[i:src.index('def _strip_music_notes', i)]
    assert 'from PIL import Image' in body, (
        "Image is not imported inside the helper and is not at module scope — "
        "every preview will raise NameError")
    assert "_note_stats['preview_failures'] += 1" in body, (
        "the helper swallows exceptions without counting them again")
    assert 'preview_failures' in src[src.index('def note_stats_summary'):
                                     src.index('def _save_ocr_preview')], (
        "preview failures are counted but never surfaced in the run summary")


# ── The bands themselves ────────────────────────────────────────────────────

def test_the_widened_bands_cover_real_measurements():
    """⚠️ These are not round numbers — they are below the measured minimum of
    183 real notes. If someone raises them back, 183/183 stop being caught."""
    assert M._ONE_STEM_MIN <= 0.84, (
        "one-stem floor is back above Lucifer's measured 0.846 — that rejected "
        "47 of 183 real notes by four thousandths")
    assert M._HEAD_H_MIN <= 0.36, (
        "head-height floor is back above Lucifer's measured 0.37 — that rejected "
        "all 183 real notes")


def test_waist_was_deliberately_not_tightened():
    """⛔ Raising _WAIST_MIN to ~4.0 also kills the J and is tempting. It is
    tuning on ONE show, which is the exact mistake that caused this bug. The
    veto is font-relative; the waist is not."""
    assert M._WAIST_MIN <= 2.2, (
        "_WAIST_MIN was tightened — that encodes Lucifer's font the way the old "
        "bands encoded Glee's. Use the spacing veto instead.")


if __name__ == '__main__':
    fails = []
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except AssertionError as e:
                fails.append(name)
                print(f"  FAIL  {name}: {e}")
    print(f"\n{'ALL PASS' if not fails else str(len(fails)) + ' FAILED'}")
    sys.exit(1 if fails else 0)
