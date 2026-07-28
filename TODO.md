# Docflix Media Suite — TODO

## ✅ DONE — Source pre-clean pass before AI upscale (2026-07-28, v3.10.0)
Optional **"Clean source" (Off/Light/Heavy)** in the Rescaler's AI panel — an ffmpeg
deblock→*light* hqdn3d→deband pass run BEFORE the upscale so the model works on honest pixels
instead of amplifying compression/film artifacts. **Engine-agnostic:** cleans to a near-lossless
H.264 temp (NVDEC-decodable → the PyTorch stream stays intact) carrying audio+subs, feeds it to
whichever engine, auto-deletes it after; falls back to the original on any failure. Grays with the
AI controls, persists to prefs (`source_clean`). Code: `video_scaler.py` `_apply_source_clean()`
+ rowD combo + wiring in `_process_one_ai()`.
**Proven on The Ruff and Reddy Show (1957).** The restoration recipe for soft vintage cartoons:
**Clean source → Light + "Anime Stills (best)" model + Strength ~80–85%.** (Heavy preset is
deblock-forward for genuinely blocky 352×240 DivX; may retune it denoise-forward later, since Tony's
library skews film-grain-soft over DivX-blocky.)

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
- **Audio cleanup step (Media Suite) — Tony's ask 2026-07-28, build next** — old cartoon rips often
  have botched audio. Proven diagnose→fix on *Yippee Yappee & Yahooey E04* (128k mp3, **DEAD right
  channel** = lopsided fake-stereo, + quiet at −20 LUFS). One-pass ffmpeg fix, **video untouched**:
  `pan=stereo|c0=c0|c1=c0` (revive/balance the dead channel) → `loudnorm=I=-16:TP=-1.5:LRA=11` (level)
  → gentle `highshelf=g=1.5:f=6000` (presence for the low-bitrate muffle). Verified: channels
  −20/−74 → −19.25/−19.25 dB, loudness −20 → −16 LUFS. Build as a toggle/preset beside the video
  pre-clean: detect+fix dead/imbalanced channel, normalize loudness, optional presence lift; batchable
  across the vault. (Diagnosis method = ffprobe + astats + ebur128 + a `showspectrumpic` spectrogram.)
  This is one **Audio Tools** suite (shared core; standalone tool + Rescaler toggle). Includes:
  - **(a) Fix botched audio** — dead/imbalanced channel, quiet, low-bitrate muffle, optional denoise.
  - **(b) Batch loudness-normalize a FOLDER to a uniform target** (Tony's ask 7/28) — feed a folder,
    every file comes out at the same loudness (`loudnorm`, two-pass for accuracy; target e.g. −16 LUFS,
    user-settable). Fits his uniform-library ethos ([[feedback_library-uniformity]]) — the volume version.
  - **(c) Atmos / lossless → AC3 or AAC downconvert** (Tony's ask 7/28, "not sure if possible" — it IS):
    Atmos is object-based, riding a 5.1/7.1 bed inside TrueHD or E-AC3(JOC). AC3/AAC are channel-based, so
    the object/height metadata can't survive — but ffmpeg decodes the Atmos stream down to its channel
    **bed** (5.1/7.1) and re-encodes to **AC3 (5.1)** or **AAC**. Objects flatten to the bed (unavoidable &
    fine for compatibility/size — usually the whole point). Same path handles TrueHD/DTS-HD → AC3/AAC.
- **video_converter 58-duplicate cull** — the live duplicates (format_size, OCR stack, CC
  helpers) still need call-site repointing to their module versions. Now safe with the test net.
- **Merlin KB power-wash (phase 2)** — dedup ~50K + re-subject ~38K subjectless (see the KB notes).
