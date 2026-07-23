# Docflix Media Suite — TODO

## ✅ DONE — Subtitle editor view-only column sorting (2026-07-23, `31d5ecc`)
Clickable cue-tree headers, display-only (cues stay time-ordered): **Text → length**
(junk-finder), **Timestamp → duration**, **# → timeline reset**; toggles asc/desc with an
arrow indicator. Wired into BOTH editor builders. Pure sort-key helpers
(`cue_char_len/duration/cps/start`) in `subtitle_filters.py` with tests. Workflow is now
sort → select junk → delete → done (no re-sort, no restamp).

### Small follow-ups still open on the sort feature
- **CPS/reading-speed sort** — the `cue_cps` helper exists and is tested, but isn't wired to a
  header yet (no CPS column). Add via a small "Sort ▾" control or a cycle on the Timestamp header.
- **"Flag cues under N chars" highlight** — the bonus red-tag idea (light junk up without
  sorting). Not built.

## ✅ DONE — Merge subtitle editors → one editor (2026-07-23, `45c9743` → `1200569` → `9e2eb9d`)
Kept the menu-bar editor (`open_standalone_subtitle_editor`), deleted the stream editor
(`show_subtitle_editor`, −3,117 lines). 3 safe stages, app never left broken:
1. Direct-entry params on editor 1 (`auto_video`/`auto_stream`/`auto_external`) — additive.
2. Repointed all 8 callers (video_converter.py ×7 + subtitle_ocr.py) → editor 1.
3. Deleted the now-orphaned stream editor.
Erased the 22-function drift bug class for free (only one editor left, so they can't disagree).
No feature loss — editor 1 already did open/load-from-video/edit/save/re-mux; the stream editor's
only extras (waveform + inline video preview) were unwanted. 15/15 tests green.
subtitle_editor.py: 7,864 → 4,747 lines.

## Open
- **video_converter 58-duplicate cull** — the live duplicates (format_size, OCR stack, CC
  helpers) still need call-site repointing to their module versions. Now safe with the test net.
- **Merlin KB power-wash (phase 2)** — dedup ~50K + re-subject ~38K subjectless (see the KB notes).
