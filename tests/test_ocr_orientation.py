#!/usr/bin/env python3
"""The white-border-on-a-black-box bug: Tesseract silently reads NOTHING.

⚠️⚠️ WHY THIS FILE EXISTS. Subtitle bitmaps are light text on a dark
background. Both OCR paths pad the image with a WHITE border before handing it
to Tesseract. If the image has not been inverted first, that produces a white
page with a black rectangle on it — and Tesseract's layout analysis classifies
a small black rectangle as an IMAGE, not text. It returns an empty string with
no error, no partial read and no low confidence, which is indistinguishable
from a genuinely blank frame.

Measured on Warehouse 13 S01E01 (1278 real PGS cues) on 2026-09-13:
    before  131 (10.3%) empty, PLUS 173 more that read as different, worse
            text = 304 cues (23.8% of the episode) wrong in some way
    after   0 empty, 0 text differences

⚠️ The first pass counted ONLY empty cues and reported 10.3%. That missed more
than half the damage: Tesseract's models expect dark-on-light, so given the
inverse it refuses some bitmaps and misreads the others. Tony caught the second
half from the output ("[Phone Rings]" arriving as "SSFFGHUYJI"). Counting total
failures is not the same as measuring quality.

Every lost cue was a SHORT one ("Pete?", "Okay.", "Hey.") — a small box is
likelier to be written off as a picture than a wide one. That is why this was
invisible for so long: the episode still had most of its subtitles.

⚠️ A test that only checks `normalise_for_ocr` flips pixels would NOT have
caught this, because the bug was never about the pixels — it was about what
Tesseract does with them. So the important test here runs Tesseract.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.subtitle_ocr import normalise_for_ocr            # noqa: E402

PIL = pytest.importorskip("PIL")
from PIL import Image, ImageDraw, ImageFont, ImageOps         # noqa: E402


def _font(size=64):
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
              "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"):
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return None


def _subtitle_bitmap(text, pad=12):
    """A cue as the decoder produces it: light text on black, cropped to ink."""
    font = _font()
    if font is None:
        pytest.skip("no TrueType font available")
    probe = Image.new("L", (2000, 300), 0)
    d = ImageDraw.Draw(probe)
    d.text((50, 50), text, fill=235, font=font)
    bbox = probe.getbbox()
    return probe.crop((bbox[0] - pad, bbox[1] - pad,
                       bbox[2] + pad, bbox[3] + pad))


def _ocr(img):
    pytesseract = pytest.importorskip("pytesseract")
    try:
        return pytesseract.image_to_string(
            img, lang="eng", config="--psm 6 --oem 3").strip()
    except Exception as e:                       # tesseract not installed
        pytest.skip(f"tesseract unavailable: {e}")


# ── the unit behaviour ──────────────────────────────────────────────────────

def test_inverts_light_on_dark():
    img = _subtitle_bitmap("Pete?")
    assert img.getpixel((0, 0)) < 128, "fixture should be dark-background"
    out = normalise_for_ocr(img)
    assert out.getpixel((0, 0)) > 128, "background not flipped to light"


def test_leaves_dark_on_light_alone():
    img = ImageOps.invert(_subtitle_bitmap("Pete?"))
    out = normalise_for_ocr(img)
    assert list(out.getdata()) == list(img.getdata()), "inverted a good image"


def test_decides_on_corners_not_mean():
    """⚠️ A wide line of heavy text can be mostly-dark and still be
    dark-on-light. Deciding on the mean would invert it and break it."""
    img = ImageOps.invert(_subtitle_bitmap(
        "WWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWW"))
    import numpy as np
    assert np.asarray(img).mean() < 200, "fixture is not heavy enough to matter"
    out = normalise_for_ocr(img)
    assert out.getpixel((0, 0)) > 128, "flipped a dark-on-light image"


# ── ⭐ the one that actually matters ────────────────────────────────────────

SHORT_CUES = ["Pete?", "Okay.", "Hey.", "Yeah.", "Stop.", "No?"]


@pytest.mark.parametrize("text", SHORT_CUES)
def test_tesseract_reads_short_cues_after_normalising(text):
    """⭐ The real regression. Short cues were the ones being lost."""
    img = ImageOps.expand(normalise_for_ocr(_subtitle_bitmap(text)),
                          border=20, fill=255)
    got = _ocr(img)
    assert got, f"read NOTHING from {text!r} — the 10% bug is back"
    letters = "".join(c for c in got.lower() if c.isalnum())
    want = "".join(c for c in text.lower() if c.isalnum())
    assert want in letters or letters in want, f"{text!r} -> {got!r}"


def test_unnormalised_short_cue_is_what_broke():
    """Pin the FAILING behaviour so the reason for the fix stays visible.

    ⚠️ xfail, not an assertion that it fails: Tesseract versions differ and
    this must never become a red build on someone else's machine. The point is
    the documented shape — white border + black box = empty string.
    """
    img = ImageOps.expand(_subtitle_bitmap("Pete?"), border=20, fill=255)
    if _ocr(img):
        pytest.skip("this Tesseract copes with the black box; fix still needed")
    pytest.xfail("reproduces the bug: white border around a black box reads as nothing")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
