#!/usr/bin/env python3
"""Italic subtitles must survive OCR.

Tony, 2026-09-25: *"It really struggles with italics.....how can we make it better?"*

⭐ RECOGNITION WAS NEVER BROKEN. The OCR reads `♪ Hard row to hoe / all by yourself`
perfectly off his disc — it simply throws the italic away, because nothing in the OCR
path has ever emitted `<i>`. He asked for the editor's manual italic buttons back in
August for exactly this reason, and has been re-italicising whole scenes of narration
and song lyrics by hand ever since. This is the other half of that request.

⚠️⚠️ THE FIXTURE LABELS WERE WRONG THREE TIMES and every correction went the same way:
the measurement was right and the label was wrong. See tests/fixtures/italics/README.md.
There is NO text-level shortcut — brackets do not mean italic, ♪ does not mean italic,
and plain dialogue is sometimes italic (off-screen voice). The picture is ground truth.

Run: python3 tests/test_italic_detection.py
"""
import ast
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image                                             # noqa: E402
from modules.subtitle_ocr import (                                # noqa: E402
    ITALIC_SLANT_DEG, apply_italic_tag, detect_slant_deg, looks_italic)

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fixtures", "italics")

# ⚠️ The fixtures are real subtitle frames off Tony's own discs, so they are NOT in the
# repository — see tests/fixtures/italics/README.md and .gitignore. Everything that
# needs a picture SKIPS when they are absent; everything that does not (tagging, the
# bracket repair and its negative set, the one-OCR-path structure check) always runs.
# ⛔ A skip must never be silent — say so, loudly, so a clean clone knows what it did
# not verify rather than reading green and assuming full coverage.
HAVE_FIXTURES = bool(glob.glob(os.path.join(FIX, "italic", "*.bmp")))
SRC = os.path.join(HERE, "..", "modules", "subtitle_ocr.py")

fails = []


def check(label, got, want):
    if got == want:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}\n         got:  {got!r}\n         want: {want!r}")
        fails.append(label)


print("\n1. every real frame is classified correctly")
angles = {"italic": [], "upright": []}
if not HAVE_FIXTURES:
    print("  SKIP  no fixtures on disk — sections 1, 2, 4 and 7 not verified")
    print("        (real frames from Tony's discs; kept local, see fixtures README)")
for label in (("italic", "upright") if HAVE_FIXTURES else ()):
    want = label == "italic"
    files = sorted(glob.glob(os.path.join(FIX, label, "*.bmp")))
    check(f"{label}: fixtures present", len(files) > 0, True)
    for f in files:
        img = Image.open(f)
        deg = detect_slant_deg(img)
        angles[label].append(deg)
        check(f"{label}: {os.path.basename(f)} ({deg}°)", looks_italic(img), want)

print("\n2. the two populations actually separate")
if not HAVE_FIXTURES:
    print("  SKIP  (needs fixtures)")
if HAVE_FIXTURES:
# ⭐ This is the check that earns the threshold. Unlike the wake-word voiceprint bands
# or the memory-relevance scores — where the distributions overlap and NO cut works —
# these are ~16° apart, so the threshold sits in open water.
    worst_italic = max(angles["italic"])      # closest to upright
    worst_upright = min(angles["upright"])    # closest to italic
    gap = worst_upright - worst_italic
    print(f"       italic  {min(angles['italic']):.0f}°..{worst_italic:.0f}°   "
          f"upright {worst_upright:.0f}°..{max(angles['upright']):.0f}°   gap {gap:.0f}°")
    check("separation is at least 8°", gap >= 8, True)
    check("the threshold sits BETWEEN them, not on an edge",
          worst_italic < ITALIC_SLANT_DEG < worst_upright, True)

# ⚠️ A result pinned to the end of the search range is a broken metric, not a steep
# italic — three earlier versions of this detector did exactly that.
from modules.subtitle_ocr import _SLANT_SEARCH                    # noqa: E402
allv = angles["italic"] + angles["upright"]
if allv:
    check("no frame is pinned to the search-range edge",
          any(v in _SLANT_SEARCH for v in allv), False)

print("\n3. detect_slant_deg is safe on rubbish input")
check("blank image -> None", detect_slant_deg(Image.new("L", (200, 60), 0)), None)
check("all-white image -> None", detect_slant_deg(Image.new("L", (200, 60), 255)), None)
check("one-pixel-tall -> None", detect_slant_deg(Image.new("L", (200, 1), 0)), None)

print("\n4. polarity does not matter")
# ⚠️ An earlier version assumed light-on-dark and was handed a post-invert frame, so it
# measured the BACKGROUND and returned the same answer for everything.
from PIL import ImageOps                                          # noqa: E402
sample = os.path.join(FIX, "italic", "lyric_03.bmp")
if os.path.exists(sample):
    im = Image.open(sample).convert("L")
    check("same verdict inverted", looks_italic(ImageOps.invert(im)), looks_italic(im))

print("\n5. tagging")
check("wraps", apply_italic_tag("♪ Hard row to hoe", True), "<i>♪ Hard row to hoe</i>")
check("keeps the line break",
      apply_italic_tag("one\ntwo", True), "<i>one\ntwo</i>")
check("no-op when upright", apply_italic_tag("Up here.", False), "Up here.")
check("never double-wraps", apply_italic_tag("<i>x</i>", True), "<i>x</i>")
check("empty stays empty", apply_italic_tag("", True), "")

print("\n6. ONE OCR path, not two")
# ⚠️ The PGS and DVB/VobSub paths had byte-identical OCR tails, and this file's own
# warning says the DVB one "historically gets fixed second and stays broken longest".
# A rule pasted into both drifts apart; a rule CALLED by both cannot.
src = open(SRC).read()
tree = ast.parse(src)
calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
         and isinstance(n.func, ast.Attribute) and n.func.attr == "image_to_string"]
check("Tesseract is invoked in exactly one place", len(calls), 1)
helper = [n for n in ast.walk(tree)
          if isinstance(n, ast.FunctionDef) and n.name == "_ocr_cue"]
check("_ocr_cue exists", len(helper), 1)
users = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
         and isinstance(n.func, ast.Name) and n.func.id == "_ocr_cue"]
check("both paths call it", len(users), 2)
check("the helper tags italics", len([n for n in ast.walk(helper[0])
      if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
      and n.func.id == "apply_italic_tag"]), 1)

print("\n7. _ocr_cue actually RUNS (not just called)")
# ⚠️⚠️ THE CHECK THAT WAS MISSING. Section 6 asserted both paths CALL _ocr_cue and went
# green while the helper raised NameError on every cue — `pytesseract` is imported
# inside the two OCR functions, not at module level, and this helper lives outside
# both. Both call sites wrap it in `except Exception`, so every cue came back EMPTY and
# the run reported success. Structure is not behaviour: execute the thing.
from modules.subtitle_ocr import _ocr_cue                          # noqa: E402
_it = os.path.join(FIX, "italic", "lyric_05.bmp")       # "Hard row to hoe"
_up = os.path.join(FIX, "upright", "dialogue_04.bmp")   # "In the back."
if os.path.exists(_it) and os.path.exists(_up):
    _ti = _ocr_cue(Image.open(_it), "eng", [])
    _tu = _ocr_cue(Image.open(_up), "eng", [])
    check("italic frame -> non-empty text", bool(_ti.strip()), True)
    check("   ...and it is tagged", _ti.startswith("<i>"), True)
    check("upright frame -> non-empty text", bool(_tu.strip()), True)
    check("   ...and it is NOT tagged", _tu.startswith("<i>"), False)
    print(f"       italic : {_ti[:70]!r}")
    print(f"       upright: {_tu[:70]!r}")
else:
    print("  SKIP  (needs fixtures)")

print("\n8. SDH brackets: the ] Tesseract reads as I")
# Tony, 2026-09-25: "it's still having problems picking up the brackets.....mainly the
# right side one it appears." ⚠️ NOT an image fault — the ] is crisp in the bitmap; it
# is Tesseract's ] / I / l / | confusion, so the repair is post-processing.
from modules.subtitle_ocr import _repair_sdh_brackets as _R      # noqa: E402
REPAIRS = [
    ("[ Men Chattering In Spanish I", "[ Men Chattering In Spanish ]"),
    ("[ Gunshots I", "[ Gunshots ]"),
    ("[ Glass Shattering I", "[ Glass Shattering ]"),
    ("\u266a\u266a [Rock I", "\u266a\u266a [Rock ]"),
    ("\u266a\u266a [ Tejano On Car Stereo I", "\u266a\u266a [ Tejano On Car Stereo ]"),
    ("[I Cawing I", "[ Cawing ]"),
    ("[I Creaking ]", "[ Creaking ]"),
    ("- Who?\n- [ Cawing I", "- Who?\n- [ Cawing ]"),          # per line
]
for src, want in REPAIRS:
    check(f"repair {src[:34]!r}", _R(src), want)

# ⛔⛔ THE LOAD-BEARING HALF. A blunt I -> ] would wreck every line ending in the
# pronoun. The rule is bracket-BALANCED, so only a line with an unclosed [ can gain a ].
# Extend this set BEFORE ever widening those patterns — this is the same discipline the
# music-note work needed after a widened geometry band ate the letter "u".
KEEP = [
    "Neither did I",
    "That's what I said, I",
    "[Man] I told you",
    "[ Sighs ] I don't know.",
    "I said I would, and I",
    "[I want to go]",        # next word lowercase -> not an SDH descriptor
    "Up here.",
    "[ Gunshots ]",          # already correct, must not be touched twice
    "",
]
for src in KEEP:
    check(f"untouched {src[:36]!r}", _R(src), src)

print()
if fails:
    print(f"FAILED: {len(fails)} check(s)")
    sys.exit(1)
print("all italic-detection checks passed")
