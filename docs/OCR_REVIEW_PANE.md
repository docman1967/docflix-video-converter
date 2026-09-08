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

## ⭐ Prior art: this is a SubtitleEdit feature

Tony, 2026-09-08: *"This design isn't really my design. It's one of the features of Subtitle Edit
that I really like. Problem is that program doesn't do as well or as fast as ours and they don't
have music note support."*

That matters for two reasons. The interaction is **proven**, not speculative — there is a working
reference to look at rather than a UI to invent. And the framing is not "build a new thing," it is
"port a known-good feature into a pipeline that is already faster and already handles ♪"
([[project_music-note-ocr]] — Tesseract cannot emit U+266A at all, so the Suite erases the glyph
geometrically first; SubtitleEdit has no equivalent).

## Lifetime — SETTLED, per Tony

**The bitmaps live exactly as long as the review session. Saving the file, or opening it in the
Subtitle Editor, ends the review and deletes them.** Tony, 2026-09-08: *"Once the job is done and
the file is either saved or opened with the subtitle editor, they can be deleted."*

⚠️ Arthur had written this up as an open question with orphan-reaping and a "review requested" flag
as options. That was over-thinking it: **the action that ends the review is the trigger**, and both
of those actions are already explicit user gestures the code can hook. No background reaper needed.

He also judged the disk cost a non-issue (*"I don't think it will be too much per video file"*),
and the short lifetime makes it moot regardless — worth a single measurement when building, not a
design constraint.

## Open questions for when we build it
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
