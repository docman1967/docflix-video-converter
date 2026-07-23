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

## Open
- **Consolidate the two subtitle-editor builders** (`open_standalone_subtitle_editor` +
  `show_subtitle_editor`) — they're a known duplication; every editor change has to be made twice.
- **video_converter 58-duplicate cull** — the live duplicates (format_size, OCR stack, CC
  helpers) still need call-site repointing to their module versions. Now safe with the test net.
- **Merlin KB power-wash (phase 2)** — dedup ~50K + re-subject ~38K subjectless (see the KB notes).
