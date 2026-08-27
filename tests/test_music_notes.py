"""Tests for pre-OCR ♪ detection (modules/music_notes.py).

⚠️ **THE FIRST TEST IS THE ONE THAT MATTERS.** Four classifiers in a row looked
correct by their own numbers and were wrong when the blobs were actually
rendered and looked at. The last of them ate the letter `u` out of live
dialogue — "Didn't you hear?" became "Didn't yo hear?" — and it did so silently,
because a subtitle missing one letter is still a valid subtitle. Nothing errors.
Nothing warns. It is the same shape as the `balance_lines` word-deletion bug in
`whisper_subtitles`: the only way to catch it is to assert that the text you did
not target is still there.

**Keep `test_does_not_erase_letters` first and keep it exhaustive.**
"""
import os
import sys

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.music_notes import (  # noqa: E402
    strip_notes, reinsert_notes, _is_note, _label,
)


def _font(size=48):
    for path in ('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
                 '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
                 '/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf'):
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    pytest.skip('no scalable font available to render test subtitles')


def _render(text, size=48):
    """White-on-black subtitle bitmap, the way a PGS decoder hands it over."""
    font = _font(size)
    img = Image.new('L', (1400, 90 + 60 * text.count('\n')), 0)
    ImageDraw.Draw(img).multiline_text((20, 10), text, fill=255, font=font, spacing=14)
    return img.crop(img.getbbox())


def _ink(img):
    return int((np.array(img.convert('L')) > 100).sum())


# ── the regression that nearly shipped ────────────────────────────────────────

@pytest.mark.parametrize('line', [
    "Didn't you hear?",            # `u` — the letter it actually ate
    "I'm one of the judges.",      # `j` `u` `d` `g`
    "We have worked ourselves",    # `u` again, mid-word
    "to pop up and start singing", # `u` `p`
    "You think either one of us",
    "What about Finn?",
    "He's your best friend.",
    "I keep expecting",
    "in three years?",
    "THE QUICK BROWN FOX JUMPS",   # every capital
    "abcdefghijklmnopqrstuvwxyz",  # every lowercase
    "0123456789",
])
def test_does_not_erase_letters(line):
    """⚠️ Plain dialogue must come through byte-identical. No note, no erasure.

    This is the assertion the `u` bug slipped past, because nobody was checking
    that untargeted pixels survived.
    """
    img = _render(line)
    before = _ink(img)
    out, marks = strip_notes(img)
    assert marks == [], f"claimed a ♪ in plain dialogue: {line!r}"
    assert _ink(out) == before, f"erased ink from plain dialogue: {line!r}"


def test_lowercase_u_specifically():
    """`u` has two stems; a ♪ has one. This is the single-stem rule's whole job."""
    img = _render('u u u u u')
    out, marks = strip_notes(img)
    assert marks == []
    assert _ink(out) == _ink(img)


# ── finding real notes ────────────────────────────────────────────────────────

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        'fixtures', 'pgs_notes')


def _fixtures(kind):
    if not os.path.isdir(FIXTURES):
        pytest.skip('PGS fixtures missing')
    return sorted(f for f in os.listdir(FIXTURES) if kind in f)


@pytest.mark.parametrize('name', _fixtures('note') if os.path.isdir(FIXTURES) else [])
def test_finds_the_notes_in_real_pgs(name):
    """These are REAL PGS bitmaps, rendered from a BluRay rip.

    ⚠️ **Look in the download pipeline, not the library.** Every library copy is
    `subrip` — a scan of 100+ TV files and movies found zero PGS — because the
    bitmaps are converted on the way in. The sources still holding PGS sit in
    `~/downloads/dst/`, and only until they are processed. Concluding "PGS no
    longer exists here" from the library alone is wrong, and was wrong once.

    To re-render, black canvas + overlay the subtitle stream, dedupe, one frame
    per cue (this is the Suite's own technique):

        ffmpeg -f lavfi -i color=c=black:s=1920x1080:r=2 -i <file.mkv> \\
          -filter_complex "[0:v][1:<sub_idx>]overlay=shortest=1,\\
                           mpdecimate=hi=1:lo=1:frac=1,setpts=N/TB" \\
          -vsync 0 f_%05d.png

    Everything here was validated by rendering the blobs and LOOKING at them —
    four classifiers passed their own numbers and failed the contact sheet. The
    fixtures are that judgement, preserved.
    """
    expected = int(name.split('_')[1][0])
    img = Image.open(os.path.join(FIXTURES, name))
    _out, marks = strip_notes(img)
    assert len(marks) == expected, f"{name}: expected {expected} notes, got {marks}"


@pytest.mark.parametrize('name', _fixtures('dialogue') if os.path.isdir(FIXTURES) else [])
def test_real_dialogue_is_untouched(name):
    """Note-free PGS lines must come back with every pixel intact.

    ⚠️ The `dan` / `fre` / `nor` / `spa` fixtures are the accented-glyph guard.
    The same disc carries eight PGS language tracks, so `ø å æ ä ö ñ ç é è à`
    are free negatives — shapes the classifier was never tuned against, and
    exactly where a geometric rule would be expected to break. 710 frames
    across those four languages produced zero false positives; these four are
    the widest cue from each, kept so a future tweak has to keep it that way.

    (Those tracks contain no ♪ at all — the foreign subs don't caption the
    songs — which is what makes them a clean negative set.)
    """
    img = Image.open(os.path.join(FIXTURES, name))
    out, marks = strip_notes(img)
    assert marks == []
    assert _ink(out) == _ink(img)


@pytest.mark.xfail(reason=(
    "KNOWN LIMIT — the geometry bands are tuned to ONE subtitle font, because "
    "one BluRay rip was the only real PGS in the pipeline when this was written. "
    "Desktop fonts draw ♪ with a much longer stem (head_w/glyph_h 0.29-0.37 vs "
    "the 0.48-0.50 measured here) and some draw the flag as a curve that splits "
    "the middle band, defeating the single-stem rule. ⚠️ Do NOT fix this by "
    "widening the bands until you have a NEGATIVE set from the same font — "
    "widening them blind is exactly how this module came to erase the letter `u` "
    "from live dialogue. Add the new disc's bitmaps to tests/fixtures/ first, "
    "then tune. Diagonal skew (top-third centroid right of bottom-third) is the "
    "best candidate feature: stable at +0.25..+0.51 across 12 fonts."),
    strict=False)
def test_generalises_to_other_fonts():
    path = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'
    if not os.path.exists(path):
        pytest.skip('DejaVu not installed')
    img = Image.new('L', (300, 140), 0)
    ImageDraw.Draw(img).text((30, 10), '♪', fill=255,
                             font=ImageFont.truetype(path, 48))
    bb = img.getbbox()
    if not bb:
        pytest.skip('font has no glyph for U+266A')
    blob = np.array(img.crop(bb)) > 100
    lab, n = _label(blob)
    found = False
    for i in range(1, n + 1):
        ys, xs = np.nonzero(lab == i)
        if len(ys) < 20:
            continue
        sub = lab[ys.min():ys.max() + 1, xs.min():xs.max() + 1] == i
        found = found or _is_note(sub)
    assert found, 'the ♪ glyph itself was not classified as a note'


# ── labelling ─────────────────────────────────────────────────────────────────

def test_label_counts_separate_shapes():
    m = np.zeros((30, 60), dtype=bool)
    m[5:15, 5:15] = True
    m[5:15, 30:40] = True
    _lab, n = _label(m)
    assert n == 2


def test_label_joins_diagonal_contact():
    """8-way connectivity — antialiased strokes touch at corners."""
    m = np.zeros((20, 20), dtype=bool)
    m[5:10, 5:10] = True
    m[10:15, 10:15] = True
    _lab, n = _label(m)
    assert n == 1


# ── putting the notes back ────────────────────────────────────────────────────

def test_reinsert_brackets_a_single_line():
    assert reinsert_notes("Don't stop believin'", [(0, 'L'), (0, 'R')]) \
        == "♪ Don't stop believin' ♪"


def test_reinsert_places_per_line_when_counts_agree():
    out = reinsert_notes('You raise me up\nHave to believe\nwe are magic',
                         [(0, 'L'), (0, 'R'), (1, 'L'), (2, 'R')])
    assert out == '♪ You raise me up ♪\n♪ Have to believe\nwe are magic ♪'


def test_reinsert_falls_back_when_ocr_splits_differently():
    """⚠️ OCR does not reliably return one line per bitmap line.

    When the counts disagree, bracketing the cue is correct and guessing a
    line mapping is not — a ♪ in the wrong place reads worse than one at the
    edge, and it is not recoverable by any later filter.
    """
    out = reinsert_notes('one line', [(0, 'L'), (1, 'R')])
    assert out == '♪ one line ♪'


def test_reinsert_is_a_no_op_without_marks():
    assert reinsert_notes('plain dialogue', []) == 'plain dialogue'


def test_reinsert_handles_empty_ocr():
    """A note-only cue whose text OCR'd to nothing still yields the note."""
    assert reinsert_notes('', [(0, 'L')]) == '♪'


def test_reinsert_does_not_double_up_repeated_notes():
    """`♪♪ [ Continues ]` — two leading notes, one marker."""
    assert reinsert_notes('[ Continues ]', [(0, 'L'), (0, 'L')]) == '♪ [ Continues ]'


# ── failing open ──────────────────────────────────────────────────────────────

def test_blank_image_is_safe():
    img = Image.new('L', (100, 40), 0)
    out, marks = strip_notes(img)
    assert marks == []
    assert _ink(out) == 0


def test_ocr_wrapper_fails_open(monkeypatch):
    """⚠️ A crash must cost one character, never the whole cue.

    OCR runs unattended across an episode; an exception escaping here would
    blank subtitles wholesale and the first anyone would know is a silent file.
    """
    from modules import subtitle_ocr
    monkeypatch.setattr(subtitle_ocr, '_strip_music_notes',
                        subtitle_ocr._strip_music_notes)
    import modules.music_notes as mn
    monkeypatch.setattr(mn, 'strip_notes',
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('boom')))
    img = Image.new('L', (100, 40), 0)
    out, marks = subtitle_ocr._strip_music_notes(img)
    assert out is img and marks == []
    assert subtitle_ocr._reinsert_music_notes('text', [(0, 'L')]) is not None
