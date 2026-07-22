# Docflix Media Suite — TODO

## Subtitle Editor: column sorting (view-only) — queued 2026-07-22

**Goal:** clickable column sorting in the subtitle editor's cue Treeview, so junk
cues are trivial to find and delete. Inspired by Subtitle Edit — but *shorter*.

**The core design decision (this is what makes ours better):**
The sort is **DISPLAY-ONLY**. The underlying `cues` data model stays in **time order
at all times** — sorting only changes how the Treeview is *displayed*, never the data.

Why it matters — it collapses Subtitle Edit's 4-step chore into 2:
- Subtitle Edit reorders the actual data, so its workflow is:
  sort by length → delete → **sort back by time → renumber/restamp** (the 2 wasted steps).
- Ours: sort view by length → delete the junk → **done.**
  - No "sort back by time": the model was never unsorted.
  - No "restamp/renumber": `write_srt()` already renumbers sequentially on save.

**Sort keys to offer (all view-only):**
- **Character length** — the killer feature: clusters junk (`#`, `.`, `-`, stray
  music-note fragments, OCR garbage, single-char lines) at the top. Sort ascending → hit-list.
- **Duration** — surfaces too-short flashes and too-long lingerers.
- **CPS / reading speed** — pairs with the 2026-07-22 cue segmenter; finds cues that read too fast.
- **Start time** — the canonical order / reset.

**Implementation notes:**
- The editor's cue list is a `ttk.Treeview` in `modules/subtitle_editor.py`. NOTE: there are
  TWO editor builders — `open_standalone_subtitle_editor` (L122) and `show_subtitle_editor`
  (L4653). Either add sorting to both, OR use this feature as the reason to consolidate them
  into one shared implementation (they're already a known duplication).
- Pattern to borrow: the main app's `_sort_by_column` on `file_tree` in `video_converter.py`
  (clickable sortable headers).
- Display-only mechanism: reorder the *tree rows* by the sort key (e.g. `tree.move()` or
  rebuild row order from a sorted view of cue indices) WITHOUT touching the `cues` list.
  On delete, remove from `cues` by cue identity (map tree iid → cue), so `cues` stays
  time-ordered. Save/export uses `write_srt(cues)` (already renumbers).
- Bonus (do both, let user pick): a "flag cues under N chars" highlight (red tag) that
  lights junk up without sorting at all.
- Tests: with the new pytest suite in place, add pure tests for the sort-key helpers
  (length/duration/cps of a cue) — keep it test-backed.
