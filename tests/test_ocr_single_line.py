#!/usr/bin/env python3
"""One line on screen is read as one line (--psm 7), everything else as a block (--psm 6).

⚠️⚠️ WHY THIS FILE EXISTS. The Chosen S01E01 (Blu-ray, thin anti-aliased sans) broke
--psm 6's block layout analysis on perfectly legible single lines (Tony, 2026-11-13):
    "- I'm scared."      → "I'm / scare"
    "- Adonai's words."  → "- Adonai's / Adonai's words."
    '"Fear not--"'       → '"Fea'
    "and yet never drown?" → a page of garbage
--psm 7 read the same bitmaps right. Measured on that episode: 34 cues corrected and 3
cues that had been lost outright recovered, one small new error ("iit"). The control,
Star Trek: Enterprise S01E14 (576 PGS cues), came out byte-identical.

⚠️ The detector must fail SAFE. A two-line cue judged "one line" would be handed to
--psm 7, which expects a single line — so two-line fixtures are the important half.
⚠️ And it must not count rows by density: rows holding only an apostrophe or a full
stop fall under any density cut and split one line into "several" (measured: 33 of 402
single lines counted as one at a 1% cut).
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.subtitle_ocr import _is_single_line, normalise_for_ocr   # noqa: E402

PIL = pytest.importorskip("PIL")
from PIL import Image, ImageDraw, ImageFont, ImageOps                  # noqa: E402


def _font(size=48):
    for p in ("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return None


def _cue(text, pad=12, spacing=8):
    """A cue as _ocr_cue receives it: dark text on light, white border."""
    font = _font()
    if font is None:
        pytest.skip("no TrueType font available")
    probe = Image.new("L", (2000, 400), 0)
    ImageDraw.Draw(probe).multiline_text((50, 50), text, fill=235, font=font,
                                         spacing=spacing, align="center")
    b = probe.getbbox()
    img = probe.crop((b[0] - pad, b[1] - pad, b[2] + pad, b[3] + pad))
    return ImageOps.expand(normalise_for_ocr(img), border=20, fill=255)


@pytest.mark.parametrize("text", [
    "- I'm scared.",
    "\"Fear not--\"",
    "and yet never drown?",
    "- Hey.",
    "...",                       # dots only: tiny ink, must not crash or split
])
def test_one_line_is_one_line(text):
    assert _is_single_line(_cue(text))


@pytest.mark.parametrize("text,spacing", [
    ("- You should be sleeping, little one.\n- I can't sleep.", 8),
    ("Is your head\nhurting you again?", 8),
    ("- Okay.\n- Okay.", 2),     # tight leading: the case that must not slip to psm 7
    ("I\n.", 4),                 # tiny glyphs on both lines
])
def test_two_lines_are_not_one(text, spacing):
    assert not _is_single_line(_cue(text, spacing=spacing))


def test_blank_image_is_not_a_line():
    assert not _is_single_line(Image.new("L", (300, 80), 255))


# ── the rescue: only when the first read looks broken (Tony, 2026-11-13) ─────────────
# Same episode, second failure: clean bitmaps read as NOTHING ("- How can I?", "40
# days."), an italic line as garbage, two-line cues missing their second line. Shrink
# + threshold read them; applied to every cue it worsened half of 12 changed Enterprise
# reads, so it is a rescue, gated by _looks_broken.

from modules.subtitle_ocr import _looks_broken, _shrink_and_binarise   # noqa: E402


def test_empty_read_is_broken():
    assert _looks_broken(_cue("- How can I?"), "")


def test_junk_characters_are_broken():
    assert _looks_broken(_cue("- Mary..."), "- Mary. B®")


def test_one_line_read_as_several_is_broken():
    assert _looks_broken(_cue("- I'm scared."), "I'm\nscare")


def test_two_lines_read_as_one_is_broken():
    """The dropped-second-line case."""
    img = _cue("to stop Shabbat fishing,\nwhich was already our law,")
    assert _looks_broken(img, "to stop Shabbat fishing,")


def test_a_good_read_is_left_alone():
    assert not _looks_broken(_cue("- I'm scared."), "- I'm scared.")
    two = _cue("- You should be sleeping, little one.\n- I can't sleep.")
    assert not _looks_broken(two, "- You should be sleeping, little one.\n- I can't sleep.")


def test_shrink_and_binarise_is_two_tone_and_smaller():
    img = _cue("- Stoic, huh?")
    out = _shrink_and_binarise(img)
    assert set(out.getdata()) <= {0, 255}
    assert out.height < img.height
