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

## Pre-flagging suspect cues — WANTED, and also prior art

Tony, 2026-09-08: *"a pre-flag on things that might be suspect is a good thing to add and in fact
subtitle edit does that as well."* So this is settled in principle too — the only open part is
which checks to run.

⚠️⚠️ **It must PROPOSE, never FILTER.** Show every cue, mark the suspicious ones. Hiding a cue he
would have caught is the one unforgivable failure here — exactly the rule
[[project_forced-subtitle-editor]]'s language scan already follows (it labels music as "Norwegian
87%", so it gets a vote, never a veto). The flag is a hint about where to look first, not a claim.

Candidate checks, cheapest first — most of the ingredients already exist in this repo:
- **Non-dictionary words**, subtracting a system dictionary AND the 1.1M-name DB in
  `~/.local/share/docflix/names/` (already loaded by `subtitle_filters`). ⚠️ The names DB is what
  keeps every proper noun from flagging — but see [[reference_caps-filter-and-names]]: it also
  matches contraction fragments (`didn`, `wasn`, `aren` are all real surnames), so a naive lookup
  will *suppress* flags it should raise.
- **Classic OCR confusions** — `l`/`1`/`I`/`|`, `0`/`O`, `rn`→`m`, `cl`→`d`. A word that is
  non-dictionary *and* becomes a dictionary word after one such substitution is a strong signal.
- **Empty or near-empty OCR from a non-empty bitmap** — the bitmap has ink, the text does not.
  ⚠️ This one is nearly free and catches total failures, which are the worst kind because they are
  invisible in the output.
- **Text length wildly out of proportion to bitmap width** — a wide image yielding three characters.
- **Suspicious characters** that should never survive `_vtt_to_srt`/OCR cleanup at all.

⚠️ Whatever the check set, it needs the same discipline as the ♪ band work: **measure it against
real output before trusting it** ([[project_music-note-ocr]] — a band that was measured, then
widened by hand and not re-tested, silently ate the letter `u` out of live dialogue; caught by a
contact sheet, not by a count).

## Open questions for when we build it
- **Where it lives.** New pane in the OCR window, or a mode of the existing Subtitle Editor? The
  editor already has cue navigation and text editing; the bitmap panel may be the only genuinely
  new widget.
- ⚠️ `detect_cc_types` runs a **180-second CC probe** whenever the Subtitle Editor opens a video
  (known, pre-existing). If the review pane is built into that editor, this lands directly in the
  middle of the new workflow and would need addressing first.

## Related

- [[project_music-note-ocr]] — the ♪ erase-before-Tesseract work; same OCR path.
- ⚠️ PGS sources live in `~/downloads/dst`, **not** the library — the library is all subrip.
- `docs/SUBTITLE_EDITOR_SCAN_MOVE_BUG.md` — separate bug queued for the same session:
  moving the scan dialog mid-scan loses the subtitle track list (Subtitle Editor, not OCR).

---

## ✅ BUILT 2026-09-13 — and what the measurements decided

All three candidate checks from the list above were built and measured against
**368,196 real cues from 397 of Tony's own subtitle files**, plus **1,278 live PGS cues** from
Warehouse 13 S01E01. Two shipped, one was dropped on the evidence.

| check | measured | verdict |
|---|---|---|
| **One-substitution confusion** (`Iike`→`like`) | **1 in 209 cues**, ~all genuine | ✅ shipped |
| **Function-word run-together** (`foryou`→`for you`) | **1 in 856 cues**, 2 visible FPs in top 25 | ✅ shipped |
| **Text too short for bitmap width** | **0 of 1,278** on a clean episode | ✅ shipped as a net |
| ⛔ **Plain non-dictionary word** | **1 in 54 cues, ~7% precision** | ❌ **DROPPED** |

### ⛔ Why the non-dictionary check was dropped — do not rebuild it

It fires on 1 cue in 8 raw, and **1 in 54** even after excluding the names DB, contraction fragments
and dropped-g dialect. The residue is the show's own invented vocabulary, which is exactly what a
fixed wordlist cannot know: `Aquaman` (432), `UnSub` (341), `Superfriends` (240), `Hotchner`,
`Quantico`, `TroubAlert` — plus ordinary words a 1922 wordlist lacks: `bloke`, `ahold`, `bollocks`,
`nowt`, `superhero`, `backseat`, `handedly`.

⭐ **The decisive measurement:** of the words it uniquely found (6,710 hits that the substitution
check did *not* already catch), roughly **2 in 30 were real errors**. Its true positives were
largely a *subset* of a check with 4x better precision.

⚠️ A "hapax" refinement — flag only words occurring ONCE in the file, since a proper noun recurs and
a misread is usually a one-off — improved it from 1-in-8 to 1-in-54 and still was not enough.

⭐ **But it earned its keep on the way out.** Looking at its unique residue is what surfaced
`Wejust` → `We just`, the missing-space failure that became the second shipped check.

### ⚠️ The trap that cost a full measurement pass

`load_names_db()` **rebinds** the module global, so `from .subtitle_filters import _names_db`
captures an empty set that never updates. The first pass over 371k cues ran with **no names DB at
all** and "corrected" real surnames (`Arnie`→`Amie`, `Henning`→`Heming`). Fixed by adding
`get_names_db()`; ⛔ **never import that global directly.**

### ⚠️ The constraint that makes the split check work

"Splits into any two dictionary words" measures at **1 in 67 and is almost entirely wrong** —
`Aquaman`→`Aqua man`, `Batmobile`→`Bat mobile`, `thrusters`→`thrust ers`. Restricting the first part
to a **closed set of function words** (which never begin an English compound) takes it to 1 in 856.
⛔ Do not add `over`/`under`/`out`/`back` to that set.
