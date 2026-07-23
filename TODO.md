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
- **Merge subtitle editors → KEEP the menu-bar one, DELETE the stream one (Tony's call 2026-07-23).**
  Analysis: editor 1 `open_standalone_subtitle_editor` (L165) & editor 2 `show_subtitle_editor` (L4697)
  share 100 nested funcs — 78 byte-identical, **22 DRIFTED** (same name / different code: refresh_tree,
  push_undo, do_export, do_replace_all/one, save_edit, split/join_selected, _classify_cue,
  show_context_menu, on_double_click, _retime, _apply, _start, _run, do_find… — the drift IS the "few
  things" that behaved inconsistently between the two).
  **NO feature loss deleting editor 2:** editor 1 already does open-.srt, load-subs-from-video
  (`load_video_subtitle` L343), edit, save-.srt, AND re-mux-back-into-video (`do_save_file` L1470).
  Editor 2's ONLY extras are the **waveform timeline + inline video preview** — both unwanted.
  PLAN — 3 safe, independently-committable stages (app never left broken):
  1. Add a direct entry to editor 1: `open_standalone_subtitle_editor(app, auto_video=None,
     auto_stream=None, auto_external=None)` — at the tail, `editor.after(120, …)` → `load_file(auto_external)`
     if external sub, else `load_video_subtitle(auto_video)`. Purely additive, zero behavior change. → commit
  2. Redirect the **8 callers** of `show_subtitle_editor` → editor 1 (video_converter.py L5428, 6348, 6358,
     7093, 7131, 7159, 7169; subtitle_ocr.py L1568). Map: external_sub_path → auto_external; else
     video+stream → auto_video. Editor 2 now orphaned, nothing broken. → commit
  3. Delete editor 2 (`show_subtitle_editor`, ~L4697–7813, ≈3,100 lines). → commit
  Verify each stage: compile + pytest + Xvfb screenshot of opening a video's sub. Bonus: erases the 22-drift
  bug class for free (only editor 1 remains).
- **video_converter 58-duplicate cull** — the live duplicates (format_size, OCR stack, CC
  helpers) still need call-site repointing to their module versions. Now safe with the test net.
- **Merlin KB power-wash (phase 2)** — dedup ~50K + re-subject ~38K subjectless (see the KB notes).
