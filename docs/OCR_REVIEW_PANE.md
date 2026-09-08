# OCR review pane — proofread cues against their source bitmaps

**Status: DESIGN NOTE, not built.** Tony's ask, 2026-09-08: *"We can't get into it now but I want
to do some work on the OCR window."* Written down while the thinking was fresh so it does not have
to be re-derived later.

## The workflow he described

1. Initiate OCR on a PGS subtitle stream — unchanged.
2. It does its work — unchanged.
3. **At the end, before saving or opening in the editor**, scan the cues for errors.
4. Click a suspect cue → **see the original bitmap that produced it**.
5. Fix the text inline, reading from the picture.

## Why it matters

OCR errors are currently *unfixable by inspection*. When a cue comes out wrong the source of truth
is gone, so the only options are guessing from context or re-running the whole job. Putting the
bitmap next to the text turns proofreading from inference into simply reading. This is the same
shape as the Forced Subtitle Editor ([[project_forced-subtitle-editor]]) — the machine proposes,
the human decides, with the evidence actually on screen.

## ⚠️ The load-bearing constraint: the bitmaps are deleted

`modules/subtitle_ocr.py`, end of `ocr_bitmap_subtitle()`:

```python
    finally:
        import shutil as _shutil_cleanup
        _shutil_cleanup.rmtree(tmpdir, ignore_errors=True)   # ~line 1054
```

Every frame lives under `tempfile.mkdtemp(prefix='docflix_ocr_')` and the whole tree is removed the
moment OCR returns. **Tony identified this himself** — *"This would also need for the bitmaps to
stay active somehow"* — and he is right that it is the crux, not an implementation detail.

## ⭐ The good news: the cue↔bitmap mapping already exists

`_decode_and_ocr()` already returns **`(pts, dur, text, img_path)`** per subtitle — the bitmap path
is paired with the cue that came from it. Nothing needs to be threaded through or re-derived; the
association is already computed and then thrown away with the directory.

So the work is roughly:
- **Keep the temp dir alive** for the review session instead of unconditionally rmtree-ing it, and
  delete it when the review pane closes (or the job is abandoned).
- **Carry `img_path` out** with the cues rather than dropping it at the return boundary.
- **A review pane** — cue list on one side, bitmap + editable text on the other.

## Open questions for when we build it

- **Lifetime/ownership.** Who deletes the temp dir, and what guarantees it happens if the app
  crashes mid-review? ⚠️ A PGS stream for a film is thousands of frames; leaking that per job would
  quietly fill a disk. Options: keep the `finally` but gate it on a "review requested" flag, or
  move the frames somewhere owned and reap on next launch.
- **Disk cost.** Worth measuring actual bytes for a feature-length PGS stream before deciding
  whether to keep BMPs, convert to PNG, or keep only frames for cues flagged suspect.
- **What counts as "suspect"?** Cheap heuristics could pre-flag likely errors (non-dictionary
  words, `|`/`1`/`l` confusions, zero-length OCR from a non-empty bitmap, unusually short text for
  a wide bitmap) so he is not reading all 1,200 cues. ⚠️ Must PROPOSE, never filter — hiding a cue
  he would have caught is the one unforgivable failure here, same rule as
  [[project_forced-subtitle-editor]]'s language scan.
- **Where it lives.** New pane in the OCR window, or a mode of the existing Subtitle Editor? The
  editor already has cue navigation and text editing; the bitmap panel may be the only genuinely
  new widget.
- ⚠️ `detect_cc_types` runs a **180-second CC probe** whenever the Subtitle Editor opens a video
  (known, pre-existing). If the review pane is built into that editor, this lands directly in the
  middle of the new workflow and would need addressing first.

## Related

- [[project_music-note-ocr]] — the ♪ erase-before-Tesseract work; same OCR path.
- ⚠️ PGS sources live in `~/downloads/dst`, **not** the library — the library is all subrip.
