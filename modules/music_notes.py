"""Find ♪ music notes in a subtitle bitmap *before* Tesseract sees it.

⚠️ **WHY THIS EXISTS.** Tesseract cannot output `♪` at all. The character is not
in `eng.traineddata`'s unicharset — it is a property of the language data, not
the binary, so no newer Tesseract version fixes it. Pointing at a Latin pack
would add the glyph but costs real accuracy on English subtitles, and
fine-tuning a model was more than the problem is worth. Tony chose the third
route on 2026-08-27: **find the notes geometrically, erase them, OCR the rest,
then put them back.**

⚠️ **There was an older `_is_music_note_frame` heuristic and it never once
fired.** It claimed to catch a cue that is *nothing but* notes by measuring how
little ink was in the frame — but its comment said "after inversion, text is
dark on white" while it ran *before* the inversion, so it counted the black
background. A real note-only cue measured 0.88 against a `< 0.03` threshold.
Zero claims across 925 real frames, in either polarity. **Deleted 2026-08-27**
once this module was shown to cover both cases.

⚠️ Its obvious one-line polarity "fix" would have been a bug: the tiny cues in a
real episode are `Yeah.` `Mom.` `Hi.` `[ Sighs ]`, and every one of them fits
"small content area, narrow width". They were safe only because the gate was
broken. If you are ever tempted to reinstate an ink-volume shortcut, that is
what it costs.

## How a note is told apart from a letter

Four rounds of "obvious" classifiers each caught letters instead, and each one
looked right by its own numbers until the blobs were actually rendered and
LOOKED at. What survived, in the order the failures forced them:

    tall + bottom-heavy      -> also k b d J        (ascenders are bottom-heavy too)
    bimodal width profile    -> also T              (a crossbar is bimodal)
    + wide part low down     -> also C L E 2        (these are wide at the bottom)
    + the wide part is THICK -> also C E 2          (their strokes are thick too)
    + a NARROW WAIST         -> also L J d f t      (the real signature, but not alone)
    + a SOLID head           -> also t              (J's hook, d's bowl are open strokes)
    + head width vs height   -> notes only

The last two are the load-bearing ones. A note is *a solid filled ellipse with
almost nothing above it but a stem* — the middle third is just the stem, so the
waist is extreme, and the head is filled where `J`'s hook and `d`'s bowl are
open curves. `t` clears both and is killed only by proportion: its tail is
narrow relative to the glyph's height where a note head is about half of it.

Measured on 499 rendered frames of Glee S01E22 (a show chosen for having more
notes than you can shake a stick at): **10,910 blobs in, 214 notes out, no false
positives** — and the negatives are real dialogue from the same episode.

⚠️ **The bands below are deliberately wider than that measurement.** On Glee
alone `head_w/glyph_h` landed in 0.48-0.50, which is far too tight to be a rule
— it is one font at one size. The ratios should carry across fonts; the exact
numbers should not be trusted to.

⚠️ **Single `♪` only.** A beamed `♫` is two heads under one bar, which fails the
solid-head test on the gap between them, and is left for OCR to drop as before.
"""
import numpy as np


# Geometry bands. All ratios — nothing here may be a pixel count except the
# minimum size, or it stops working the moment the subtitle resolution changes.
# ⚠️ These were MEASURED on real note bitmaps, then widened by hand "because one
# font is too tight to trust" — and the widened set promptly ate the letter `u`
# out of live dialogue. The reasoning was fine and the numbers were invented.
# Widen these ONLY against the negative set; a band you cannot point at a
# measurement for is a guess wearing a constant's clothes.
#
# ⚠️⚠️ AND THEY WERE MEASURED ON ONE SHOW (Glee S01E22), WHICH IS THE BUG BELOW.
# Re-measured 2026-09-18 on Lucifer (3 episodes, 3,077 cues, 183 real notes and
# 74,773 letters, ground truth from the release's own SDH text). On that font:
#
#     real notes:  onestem 0.846-0.92   hd_h 0.37-0.42   waist 5.0-7.5
#     thresholds:  onestem >= 0.85      hd_h >= 0.44
#
# **0 of 183 real notes were caught** — the window sat entirely off the
# population. Meanwhile a capital `J` cleared every test (fill 0.63 against a
# 0.62 floor, by one hundredth) and was erased out of the word "Javier", with
# `reinsert_notes` then putting a plausible ♪ where the letter had been. Tony
# caught it by comparing against the video; nothing in the output looked wrong.
# ⭐ That is why the two bounds below are WIDENED and `_WAIST_MIN` is NOT
# touched: widening cannot newly reject a glyph that passes today, so it cannot
# break the font these were originally measured on. Tightening the waist to 4.0
# would also have killed the `J` — and would have been tuning on one show, which
# is precisely the mistake being fixed here.
_MIN_H          = 10      # px; below this there is not enough shape to judge
_WAIST_MIN      = 2.2     # head width / median stem width through the middle
_FILL_MIN       = 0.62    # ink fraction of the head's own bounding box
_HEAD_H_MIN     = 0.35    # head width / glyph height. 0.44 rejected 183/183 real
                          # Lucifer notes (0.37-0.42). `t` sits at 0.29.
_HEAD_H_MAX     = 0.60    # measured 0.48-0.50 (Glee), 0.37-0.42 (Lucifer)
_HEAD_W_MIN     = 0.45    # head width / glyph width
_HEAD_W_MAX     = 0.78    # measured 0.52-0.67
_ASPECT_MIN     = 1.00    # glyph height / glyph width — a note stands up
_ASPECT_MAX     = 2.20
_ONE_STEM_MIN   = 0.82    # fraction of middle-band rows that are a single run.
                          # Glee measured 0.94; Lucifer measures 0.846, and 0.85
                          # rejected 47 real notes by four thousandths.

# ⚠️ THE LETTER VETO — this is what actually stops `J`, and it is deliberately
# NOT a proportion. Every band above is a shape ratio that drifts with the font;
# this one is spacing, measured against the SAME LINE's own median glyph gap, so
# it carries across fonts instead of encoding one.
#
# A music note stands alone: there is a word space between it and the lyric.
# A `J` is the first letter of a word and its neighbour is 2-3px away.
# Measured: real notes bottom out at 0.80 of their line's median gap; the Javier
# `J` sat at 0.60. 0.70 splits them with margin either side.
#
# ⚠️ ♪♪ IS SAFE — Tony's first question about this idea. The veto only fires on a
# NON-note neighbour, so a note beside another note is untouched.
# ⚠️ Fails toward under-erasing: a note wrongly vetoed simply stays in the
# bitmap and is picked up downstream by the regex repair, which is the status
# quo. It cannot produce a worse result than doing nothing.
_GAP_MIN        = 0.70    # nearest non-note neighbour, in units of the line's
                          # own median inter-glyph gap


def _label(mask):
    """Connected components (8-way) via run-length union-find.

    Rolled by hand rather than importing `scipy.ndimage.label`: scipy is not a
    declared dependency of the Suite and pulling one in for this alone would be
    a heavy price.

    ⚠️ **Runs, not pixels.** The obvious per-pixel double loop is ~200k Python
    iterations for one 1920x100 crop, which is fine for a single thumbnail and
    hopeless across a full episode's cues. A row of subtitle text is a few dozen
    horizontal runs, so working run-to-run is roughly two orders of magnitude
    less work for an identical result.
    """
    h, w = mask.shape
    parent = [0]

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    lab = np.zeros((h, w), dtype=np.int32)
    prev_runs = []
    for y in range(h):
        row = mask[y]
        if not row.any():
            prev_runs = []
            continue
        # Run boundaries from a single diff — no per-pixel scan.
        edges = np.flatnonzero(np.diff(np.concatenate(([0], row.view(np.int8), [0]))))
        runs = []
        for a, b in zip(edges[::2], edges[1::2]):
            hits = [r[2] for r in prev_runs if r[0] <= b and a <= r[1]]  # 8-way
            if hits:
                cur = min(hits)
                for hcomp in hits:
                    union(cur, hcomp)
            else:
                parent.append(len(parent))
                cur = len(parent) - 1
            lab[y, a:b] = cur
            runs.append((a - 1, b, cur))
        prev_runs = runs

    remap = {0: 0}
    for i in range(1, len(parent)):
        r = find(i)
        if r not in remap:
            remap[r] = len(remap)
        remap[i] = remap[r]
    flat = np.array([remap.get(i, 0) for i in range(len(parent))], dtype=np.int32)
    return flat[lab], max(remap.values())


def _is_note(blob):
    """True if this component looks like a ♪. See the module docstring."""
    h, w = blob.shape
    if h < _MIN_H or w < 4:
        return False
    if not (_ASPECT_MIN <= h / w <= _ASPECT_MAX):
        return False

    rows = blob.sum(axis=1).astype(float)
    mid, bot_rows = rows[h // 3:2 * h // 3], rows[2 * h // 3:]
    if bot_rows.max() == 0 or not (mid > 0).any():
        return False

    # ⚠️ ONE STEM. This is what stops `u` — the test that was missing when this
    # module ate the letter out of "you", "judges" and "ourselves" in real
    # dialogue. A note has a single stem rising from its head; `u` `n` `m` `H`
    # have two verticals, so their middle band is two runs wide all the way
    # down. Everything else here is a proportion that drifts with the font;
    # this one is topology and does not.
    #
    # ⚠️ It is a MAJORITY, not all rows. Requiring every row to be a single run
    # rejected 68 of 68 real notes — the flag curls off the top of the stem and
    # dips into the band. Notes measure 0.94 and never lower; `u` is far below.
    band = blob[h // 3:2 * h // 3]
    single = sum(
        1 for row in band
        if int(np.diff(np.concatenate(([0], row.view(np.int8), [0]))).clip(min=0).sum()) == 1)
    if single / max(1, len(band)) < _ONE_STEM_MIN:
        return False

    head_w = bot_rows.max()
    # The signature: through the middle there is only a stem, so the head
    # towers over it. A `C` is widest through the middle; an `E` has a bar.
    if head_w / max(1.0, np.median(mid[mid > 0])) < _WAIST_MIN:
        return False
    if not (_HEAD_H_MIN <= head_w / h <= _HEAD_H_MAX):
        return False
    if not (_HEAD_W_MIN <= head_w / w <= _HEAD_W_MAX):
        return False

    # A note head is a solid filled ellipse. `J`'s hook and `d`'s bowl are open
    # strokes and leave their own bounding box mostly empty.
    bot = blob[2 * h // 3:]
    cols = np.where(bot.any(axis=0))[0]
    if not len(cols):
        return False
    fill = bot.sum() / max(1, len(bot) * (cols[-1] - cols[0] + 1))
    return fill >= _FILL_MIN


def _line_bands(boxes, gap):
    """Group glyph boxes into text lines by their vertical extent."""
    bands = []
    for y0, y1 in sorted((b[1], b[3]) for b in boxes):
        if bands and y0 <= bands[-1][1] + gap:
            bands[-1][1] = max(bands[-1][1], y1)
        else:
            bands.append([y0, y1])
    return bands


def _veto_letters(notes, glyphs):
    """Demote note-shaped glyphs that are sitting inside a word.

    Takes ``notes`` as ``[(box, label_id), ...]`` and ``glyphs`` as ``[box, ...]``;
    returns the same pair with any vetoed note moved across to *glyphs*.

    ⚠️⚠️ THIS IS THE `J` FIX. See `_GAP_MIN` above for the measurements. A capital
    `J` clears every shape test on some fonts — it is tall, single-stemmed,
    narrow-waisted and its hook is solid enough to pass the fill floor. What it
    can never do is stand alone: it is the first letter of a word, so the next
    glyph is a letter-width away. A real ♪ has a word space beside it.

    ⚠️ Compared against the line's OWN median gap, never a pixel count — the
    whole reason the shape bands failed is that they encoded one font's
    proportions. Do not replace this with a constant.

    ⚠️ ♪♪ survives: the veto only looks at NON-note neighbours.
    ⚠️ Fails toward keeping the letter (under-erasing), which degrades to the
    regex repair downstream rather than to a corrupted word.
    """
    if not notes:
        return notes, glyphs

    everything = [(b, True) for b, _ in notes] + [(b, False) for b in glyphs]
    bands = _line_bands([b for b, _ in everything], gap=2)

    kept, demoted = [], []
    for box, ident in notes:
        cy = (box[1] + box[3]) / 2
        line = next((k for k, (a, b) in enumerate(bands) if a <= cy <= b), None)
        if line is None:
            kept.append((box, ident))
            continue
        lo, hi = bands[line]
        row = sorted([e for e in everything if lo <= (e[0][1] + e[0][3]) / 2 <= hi],
                     key=lambda e: e[0][0])
        if len(row) < 2:
            kept.append((box, ident))        # alone on its line — nothing to compare
            continue

        gaps = [row[i + 1][0][0] - row[i][0][2] for i in range(len(row) - 1)]
        gaps = [g for g in gaps if g >= 0]
        median = sorted(gaps)[len(gaps) // 2] if gaps else 0
        if median <= 0:
            kept.append((box, ident))
            continue

        try:
            k = next(i for i, e in enumerate(row) if e[0] == box and e[1])
        except StopIteration:
            kept.append((box, ident))
            continue

        nearest = None
        for j, side in ((k - 1, 'L'), (k + 1, 'R')):
            if j < 0 or j >= len(row) or row[j][1]:
                continue                     # off the end, or the neighbour is a note
            g = (box[0] - row[j][0][2]) if side == 'L' else (row[j][0][0] - box[2])
            if nearest is None or g < nearest:
                nearest = g

        if nearest is not None and nearest / median < _GAP_MIN:
            demoted.append(box)              # a letter inside a word
        else:
            kept.append((box, ident))

    return kept, glyphs + demoted


def strip_notes(img, threshold=100):
    """Erase ♪ glyphs from a light-on-dark subtitle crop.

    Returns ``(image, marks)``. *marks* is a list of ``(line_index, side)`` with
    *side* ``'L'`` or ``'R'``, describing where each erased note sat relative to
    the words on its line. Feed it back to :func:`reinsert_notes` after OCR.

    ⚠️ Call this **before** the invert/upscale steps — it expects the bitmap the
    way the PGS decoder produced it, light text on a dark field.
    """
    arr = np.array(img.convert('L'))
    mask = arr > threshold
    if not mask.any():
        return img, []

    lab, n = _label(mask)
    if n == 0 or n > 400:          # 400+ components is not a subtitle line
        return img, []

    # ⚠️ Bounding boxes in ONE pass. Doing `np.where(lab == i)` per component
    # rescans the whole image once per glyph — 258 ms/frame on a subtitle line,
    # which is fine for one thumbnail and hopeless across an episode.
    ys, xs = np.nonzero(lab)
    ids = lab[ys, xs]
    order = np.argsort(ids, kind='stable')
    ys, xs, ids = ys[order], xs[order], ids[order]
    starts = np.searchsorted(ids, np.arange(1, n + 1), 'left')
    ends = np.searchsorted(ids, np.arange(1, n + 1), 'right')

    notes, glyphs = [], []
    for i, (a, b) in enumerate(zip(starts, ends), start=1):
        if a >= b:
            continue
        cy, cx = ys[a:b], xs[a:b]
        box = (int(cx.min()), int(cy.min()), int(cx.max()), int(cy.max()))
        if _is_note(lab[box[1]:box[3] + 1, box[0]:box[2] + 1] == i):
            notes.append((box, i))
        else:
            glyphs.append(box)

    if not notes:
        return img, []

    notes, glyphs = _veto_letters(notes, glyphs)
    if not notes:
        return img, []

    # ⚠️ gap=2, not a fraction of the glyph height. Subtitle line spacing leaves
    # only ~10px of clear air between rows while a note is ~30px tall, so any
    # generous fraction merges two lines into one band — which silently drops
    # the counts out of step with OCR's lines and forces the bracket fallback.
    # Glyphs on the SAME line overlap vertically regardless, so 2 is enough.
    bands = _line_bands([b for b, _ in notes] + glyphs, gap=2)

    marks = []
    out = arr.copy()
    erase = np.zeros_like(mask)
    for box, i in notes:
        cy = (box[1] + box[3]) / 2
        line = next((k for k, (a, b) in enumerate(bands) if a <= cy <= b), 0)
        peers = [g for g in glyphs if bands[line][0] <= (g[1] + g[3]) / 2 <= bands[line][1]]
        # No words on this line at all — a note-only line. Call it leading so it
        # is emitted once rather than doubled.
        side = 'L' if not peers or box[0] < min(g[0] for g in peers) else 'R'
        marks.append((line, side))
        erase |= (lab == i)

    # ⚠️ Erase the ANTIALIASED HALO too. The glyph mask is everything above the
    # threshold; the soft edge below it survives as a faint smudge, and a smudge
    # is exactly what Tesseract turns into a stray `.` or `'`. Grow the mask by
    # a couple of pixels, but only into background — `lab == 0` keeps the dilation
    # from biting a neighbouring letter that happens to sit close.
    grown = erase.copy()
    for _ in range(2):
        g = grown.copy()
        g[1:, :] |= grown[:-1, :]; g[:-1, :] |= grown[1:, :]
        g[:, 1:] |= grown[:, :-1]; g[:, :-1] |= grown[:, 1:]
        grown = g
    out[erase | (grown & (lab == 0))] = 0

    from PIL import Image
    return Image.fromarray(out), marks


def reinsert_notes(text, marks, note='♪'):
    """Put the erased notes back into Tesseract's output.

    ⚠️ OCR does not reliably return one line per bitmap line — it merges and it
    splits. When the counts disagree this deliberately stops trying to be clever
    and falls back to bracketing the whole cue, which is what a lyric looks like
    anyway. Guessing a line mapping would put a note in the wrong place, and a
    note in the wrong place is worse than a note at the edge.
    """
    if not marks:
        return text
    lines = [ln for ln in (text or '').splitlines() if ln.strip()]
    if not lines:
        # ⚠️⚠️ ONE NOTE PER MARK. This used to `return note` flat, so a cue that
        # is nothing but ♪♪ came back as a single ♪ no matter how many glyphs
        # were erased — Tony, 2026-09-18: "it's adding ♪ FE where there should
        # be two music notes... there should be ♪♪ instead."
        # ⭐ Doubles are a FEATURE he asked for on 2026-09-14 ("we're also going
        # to need for the music notes part of the ocr to pick up doubles") and
        # the regex path has emitted `'♪' * count` since. The geometric path
        # silently collapsed them, so the two halves of the same feature
        # disagreed depending on which one fired.
        # ⚠️ No space between them — matches `_notes_for` in subtitle_ocr, and
        # his search-and-replace expects the inline pair.
        return note * len(marks)

    n_lines = max(m[0] for m in marks) + 1
    if len(lines) == n_lines:
        for i, ln in enumerate(lines):
            lead = sum(1 for m in marks if m[0] == i and m[1] == 'L')
            trail = sum(1 for m in marks if m[0] == i and m[1] == 'R')
            # ⚠️ Same bug lived here: `lead`/`trail` were COUNTED and then one
            # note emitted regardless, so `♪♪ lyric ♪♪` lost half its notes.
            lines[i] = (f"{note * lead + ' ' if lead else ''}{ln}"
                        f"{' ' + note * trail if trail else ''}")
        return '\n'.join(lines)

    # Counts disagree — bracket the cue instead of guessing.
    n_lead = sum(1 for m in marks if m[1] == 'L')
    n_trail = sum(1 for m in marks if m[1] == 'R')
    if n_lead:
        lines[0] = f"{note * n_lead} {lines[0]}"
    if n_trail:
        lines[-1] = f"{lines[-1]} {note * n_trail}"
    return '\n'.join(lines)
