# Docflix Media Suite — Project Summary

**Last Updated:** 2026-05-11 (rev 63)  
**Version:** 2.9.7  
**Source / Backup:** `/home/docman1967/scripts/video_converter/`  
**Installed To:** `~/.local/share/docflix/`  
**GitHub:** https://github.com/docman1967/docflix-video-converter  
**Purpose:** Batch video converter, subtitle editor, media processor, and media tools suite. Converts video files to H.265/HEVC format using ffmpeg, with support for CPU and multi-GPU encoding (NVIDIA NVENC, Intel QSV, AMD VAAPI).

---

## File Inventory

### Top-Level Files

| File | Size | Modified | Description |
|------|------|----------|-------------|
| `video_converter.py` | 8,870 lines | 2026-05-05 | Monolith — primary Tkinter desktop GUI app; tools import from modules |
| `convert_videos.sh` | ~541 lines | 2026-04-22 | Standalone bash CLI batch converter |
| `run_converter.sh` | 60 lines | 2026-04-27 | Launcher for Tkinter desktop app |
| `install.sh` | 423 lines | 2026-05-01 | Installer / uninstaller (package + standalone tool commands + subtitle editor "Open with" .desktop) |
| `logo.png` | 136 KB | 2026-03-27 | Original app logo (RGB, 840×958) |
| `logo_transparent.png` | 100 KB | 2026-04-05 | Background-stripped version used in title bar |
| `screenshot.png` | — | 2026-04-06 | App screenshot (used in GitHub README) |
| `README.md` | — | 2026-04-06 | GitHub repository README |
| `LICENSE` | — | 2026-04-05 | MIT License |
| `.gitignore` | — | 2026-04-05 | Git ignore rules |
| `PROJECT_SUMMARY.md` | — | 2026-04-27 | This file |
| `docs/` | dir | — | User documentation |
| `docs/user_manual.html` | — | 2026-04-29 | HTML user manual (launched from Help menu) |
| `docs/USER_MANUAL.md` | — | 2026-04-29 | Markdown user manual (PDF-ready) |
| `logs/` | dir | — | Timestamped launch logs (auto-pruned to 10) |

### Package: `modules/` (v2.0.0 — Modular Architecture)

| Module | Lines | Description |
|--------|-------|-------------|
| `__init__.py` | 33 | Package init, exports APP_NAME, APP_VERSION |
| `__main__.py` | 59 | Entry point for `python -m modules` |
| `constants.py` | 259 | APP_NAME, APP_VERSION, GPU_BACKENDS, VIDEO_CODEC_MAP, EDITION_PRESETS, LANG_CODE_TO_NAME, SUBTITLE_LANGUAGES, extensions, codec maps |
| `utils.py` | 757 | Format helpers, ffprobe wrappers, tooltips, zenity dialogs (file/folder/save), DPI scaling, font sizing |
| `standalone.py` | 194 | StandaloneContext class for standalone tool launches, shared preferences, window management, dock icon |
| `gpu.py` | 474 | GPU detection (NVENC/QSV/VAAPI), test encode verification, ffmpeg check, CC detection, video analysis |
| `converter.py` | 856 | VideoConverter engine class — ffmpeg command building, pause/resume/stop, two-pass, subtitle/metadata/chapter handling |
| `preferences.py` | 169 | Preferences save/load/reset as standalone functions |
| `subtitle_filters.py` | 1,050+ | SRT parsing/writing, all filter functions (Remove HI, Fix CAPS, etc.), timestamp manipulation, retime; optional names database (Aptivi/NamesList) for Fix CAPS with system dictionary false-positive filtering |
| `subtitle_editor.py` | 6,034 | Both editor variants (standalone + internal), inline editing, filters, timing, search/replace, waveform timeline with embedded video, video preview, "Open with" file argument support; withdraw/deiconify window positioning |
| `smart_sync.py` | 735 | Whisper-based auto-sync (faster-whisper + WhisperX), Quick/Full Scan, Direct Align, VAD snapping |
| `spell_checker.py` | 319 | Unified incremental spell check dialog with custom dictionary and character name support |
| `subtitle_ocr.py` | 795 | Bitmap subtitle OCR (PGS/VobSub → SRT via Tesseract), parallel OCR, live monitor window |
| `tv_renamer.py` | 3,540 | File Renamer — TVDB/TMDB API, multi-episode, folder templates, custom templates, TVDB/TMDB ID variables, undo, threaded loading, disambiguation; multi-separator subtitle tag detection; already-named files cleared from list; episode title matching for files without SxxExx; Edit Name dialog for manual overrides |
| `media_processor.py` | 1,645 | Media Processor — remux, audio conversion, metadata, subtitle muxing, edition tagging, chapters, track naming templates, parallel processing, per-file progress, Settings menu |
| `batch_filter.py` | 675 | Batch Filter — multi-file filter processing with search/replace pairs, Settings menu; withdraw/deiconify window positioning |
| `media_info.py` | 1,768 | Media Details — comprehensive file analysis and tag editor with editable track names, language, disposition flags, chapter editor, save via ffmpeg remux with progress bar |
| `chapters.py` | 257 | Chapter generation, parsing (FFMETADATA1, OGM), writing |
| `manual_viewer.py` | 737 | Built-in user manual viewer with sidebar navigation |
| `waveform_timeline.py` | 1,498 | Waveform Timeline widget — audio extraction, waveform rendering, cue block overlay, drag-to-move/resize, embedded mpv video playback, live subtitle preview, step navigation |
| `video_scaler.py` | 1,088 | Video Scaler — batch resize with GPU-accelerated scaling, threaded file scanning with progress/ETA, preferences; withdraw/deiconify window positioning |
| **Total** | **23,859** | **21 modules** |

### Standalone Tool Commands

| Command | Module | Description |
|---------|--------|-------------|
| `docflix` | `video_converter.py` | Full converter app |
| `docflix-subs` | `subtitle_editor.py` | Subtitle Editor (standalone, "Open with" for subtitle files) |
| `docflix-rename` | `tv_renamer.py` | TV Show Renamer (standalone) |
| `docflix-media` | `media_processor.py` | Media Processor (standalone) |
| `docflix-scale` | `video_scaler.py` | Video Scaler (standalone) |

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                  Launch Commands                        │
│                                                         │
│  docflix              Full app (video_converter.py)      │
│  docflix-subs         Subtitle Editor (standalone)       │
│  docflix-rename       TV Show Renamer (standalone)       │
│  docflix-media        Media Processor (standalone)       │
│  convert_videos.sh   Bash CLI (headless)                │
│                                                         │
├─────────────────────────────────────────────────────────┤
│                  modules/ package                        │
│                                                         │
│  constants.py ─── utils.py ─── standalone.py            │
│       │                │                                │
│  gpu.py          subtitle_filters.py                    │
│  converter.py    subtitle_editor.py ─┬─ smart_sync.py   │
│  preferences.py  batch_filter.py     ├─ spell_checker.py│
│                  tv_renamer.py       └─ subtitle_ocr.py │
│                  media_processor.py                     │
│                                                         │
├─────────────────────────────────────────────────────────┤
│  video_converter.py (monolith — legacy, still works)    │
└─────────────────────────────────────────────────────────┘
                  All interfaces call ffmpeg
```

---

## Interface 1: Tkinter Desktop GUI (`video_converter.py`)

The primary interface. Launched via `run_converter.sh`, the `docflix` terminal command, or the system app menu.

**Dependencies:** Python 3, `tkinter`, `tkinterdnd2`, `Pillow`, `ffmpeg`  
**Optional:** `ccextractor` (for extracting ATSC A53 closed captions to SRT subtitle tracks)

### Core Classes
- **`VideoConverter`** — Conversion engine; builds and runs ffmpeg commands, supports pause/resume/stop via threading.
- **`VideoConverterApp`** — Full Tkinter UI with all user-facing features.

### Key Features
- Drag-and-drop file queuing
- Per-file **settings override** (different encoder settings per file)
- **Multi-GPU encoding** — auto-detects and supports:
  - NVIDIA NVENC (presets p1–p7, `-cq` quality flag)
  - Intel Quick Sync Video / QSV (presets veryfast–veryslow, `-global_quality` flag)
  - AMD VAAPI (no presets, `-qp` quality flag)
  - CPU fallback (libx265/libx264/libsvtav1/libvpx-vp9)
- Encoder selection via **dropdown combobox** showing only detected backends
- **Two-pass encoding** support (CPU two-pass and GPU multipass where supported)
- **HW Decode** checkbox — enables hardware-accelerated decoding (auto-disabled for burn-in subtitles)
- **MPEG Transport Stream (.ts) support:**
  - Input formats: `.ts`, `.m2ts`, `.mts`
  - Output container: `.ts` (supports H.265, H.264, MPEG-4, and stream copy)
  - Drag-and-drop with `file://` URI handling for Linux file managers
  - **ATSC A53 closed caption detection** — auto-detects EIA-608/CEA-708 CC embedded in MPEG-2 video frame side data via ffprobe (runs in ~40ms)
  - **CC passthrough** — A53 CC data is automatically preserved in the output video stream when transcoding with libx264, libx265, NVENC, QSV, or VAAPI encoders
  - **CC extraction to SRT** — if `ccextractor` is installed, CC is also extracted to a separate SRT subtitle track embedded in the output container (MKV/MP4)
  - "CC" badge on files with detected closed captions in the file queue
  - Subtitle dialog shows CC status and extraction toggle
  - Container-aware subtitle handling: MPEG-TS output drops text-based subs (only DVB subtitles supported)
- **External subtitle support:**
  - Drag-and-drop `.srt`, `.ass`, `.ssa`, `.vtt`, `.sub`, `.idx`, `.sup` files onto the queue
  - Auto-matches subtitles to video files by filename stem (strips language codes, "forced", "sdh" suffixes)
  - Auto-detects **language** from filename (e.g. `.eng.srt`, `.en.srt`)
  - Auto-detects **forced** flag from filename (e.g. `.forced.srt`)
  - Auto-sets **default** flag on the first plain (non-forced, non-SDH) subtitle
  - Two modes: **embed** (soft sub, muxed as stream) or **burn-in** (hardcoded onto video)
  - Per-subtitle **Default**, **SDH**, and **Forced** disposition flags
  - **"Remove existing subtitle tracks from source"** option to replace internal subs
  - 📎 icon indicator on files with external subs attached
  - Folder scan prompts to attach matching subtitle files found alongside videos
- **Internal subtitle** (🎞️) dialog — per-stream format control for subtitle tracks already in the source file (double-click a file to open)
- **Subtitle editor** — full-featured text editor for internal streams and external subtitle files:
  - **Standalone mode** — accessible from Tools → Subtitle Editor; opens as an independent app window with File menu (Open, Save, Save As, Export, Batch Filter, Close), drag-and-drop support for subtitle and video files, and all editing capabilities without needing the converter pipeline
  - **Video subtitle editing** — drag a video file onto the editor to extract an internal subtitle stream, edit it, and re-mux it back into the video (preserves stream order, metadata, language, disposition flags; no re-encoding); animated progress dialog during extraction
  - **Stream picker** — when opening a video with multiple subtitle streams, shows a table with columns for Stream #, Language, Format, Title, and Flags (Default/SDH/Forced) for easy selection; filters out bitmap subtitles (PGS/VobSub)
  - **Batch filter** — process multiple subtitle files at once (Tools → Batch Filter or File → Batch Filter from the editor); drag-and-drop multiple files, select filters via checkboxes, choose overwrite or subfolder output, progress bar with per-file status
  - Direct inline text editing (double-click a cell) with right-click context menu for Cut/Copy/Paste/Select All
  - **Filters menu:**
    - Remove HI — strips `[brackets]`, `(parentheses)`, speaker labels (`Name:`), ALL CAPS HI descriptor labels (`HIGH-PITCHED:`, `MUFFLED:`, etc.), and ALL CAPS HI lines (UK style) in one pass
    - Remove Tags — strips `<i>`, `</i>`, `{\an8}`, etc.
    - Remove Ads/Credits — with custom pattern management (saved to preferences)
    - Remove Stray Notes — removes cues containing only `♪`/`♫` symbols
    - Remove Leading Dashes — strips leading `-` from subtitle lines
    - Remove ALL CAPS HI (UK style) — removes unbracketed all-caps HI descriptions; preserves short words (OK, NO), known acronyms (FBI, BBC), and single-word non-HI terms
    - Remove Off-Screen Quotes (UK style) — strips wrapping single quotes used for off-screen dialogue while preserving contractions (`'cause`, `'til`) and dropped-g words (`somethin'`, `thinkin'`)
    - Remove Duplicates — removes consecutive identical cues
    - Merge Short Cues — combines sentence fragments with <1s gap
    - Reduce to 2 Lines — intelligently reflows 3+ line cues to 2 lines; respects dialogue dashes, splits at sentence boundaries, falls back to midpoint word split
    - Fix ALL CAPS — converts all-caps to sentence case; respects sentence boundaries across cues and across lines within cues; second-pass safe (custom names applied even on already-converted text); custom character name support via non-modal dialog (scroll subtitle list while adding names)
    - Manage Ad Patterns — view built-in patterns, add/remove custom regex patterns
  - **Search & Replace** with Find, Replace (single), and Replace All buttons; optional wrap-around checkbox; right-click copy/paste on Find/Replace fields
  - **Insert line** above/below via right-click context menu
  - Timing tools: offset (shift ±ms) and stretch (scale by factor)
  - Split cue at midpoint / Join consecutive cues
  - Per-action Undo/Redo stack (Ctrl+Z / Ctrl+Y) with full reset
  - Color-coded rows: yellow=modified, blue=HI, pink=tags, orange=long lines (>42 chars), green=search match
  - Video preview at cue timestamp via ffplay (right-click context menu)
  - Export edited subtitle as standalone `.srt` file (defaults to video's folder when editing internal streams)
  - **Save to Video** button — re-muxes edited subtitle directly back into the video file (available in the converter's internal subtitle editor)
  - Right-click context menu: Split, Join, Insert above/below, Delete (dismisses properly on click outside)
  - Edited subtitles are automatically embedded during encoding
- **Metadata cleanup options** (in conversion pipeline settings panel):
  - **Strip chapters** — removes all chapter markers from output (`-map_chapters -1`)
  - **Strip tags** — removes all global metadata/tags from output (`-map_metadata -1`)
  - **Set track metadata** — sets language and clears names on video/audio/subtitle tracks, clears container title; configurable per-track language codes (V: `und`, A: `eng`, S: `eng`)
  - All three options available as per-file overrides in the Override Settings dialog
  - Persisted to preferences
- **Media Processor** (Tools → Media Processor, `Ctrl+M`) — standalone remux-only post-processing tool:
  - Processes already-encoded files without re-encoding (`-c:v copy`)
  - All operations combined into a single ffmpeg command per file
  - **Convert audio** — codec dropdown (aac, ac3, eac3, mp3, opus, flac, copy) + bitrate; auto-skips if source already matches target codec
  - **Strip chapters / tags / existing subtitles** — same ffmpeg flags as conversion pipeline
  - **Mux external subtitles** — auto-detects subtitle files alongside videos using configurable language code (default: `eng`); matches `*.{lang}.srt`, `*.{lang}.forced.srt`, falls back to bare `*.srt`; 🔄 Rescan button to re-detect with a different language; sets disposition flags (default/forced) and track titles
  - **Set track metadata** — language codes for video/audio/subtitle tracks, clears container title and track names
  - **Track naming templates** — configurable templates for video, audio, and subtitle track names using variables `{lang}`, `{codec}`, `{channels}`, `{bitrate}`, `{flags}`; `{flags}` resolves from disposition flags first, then falls back to parsing existing track titles for keywords (SDH, Forced, Commentary); per-stream resolution from probed data; persisted to preferences
  - **Settings menu** — Preferences dialog with organized sections: Cleanup (strip chapters/tags/subs), Subtitles (mux external subs, language, rescan), Chapters (auto-generate every N min), Output (in-place/folder, container), Track Names (templates with variable reference), Processing (parallel, jobs)
  - **Parallel processing** — checkbox + jobs spinner (defaults to CPU core count, max 8); uses `ThreadPoolExecutor` for concurrent ffmpeg execution; thread-safe logging and progress updates; falls back to sequential when disabled
  - **Output folder option** — radio buttons: "Replace in-place" (default, atomic temp file → replace) or "Save to folder:" (preserves originals); auto-creates output folder
  - **Output container selection** — `.mkv` or `.mp4` dropdown; auto-handles subtitle codec compatibility (`mov_text` for MP4, `srt`/`copy` for MKV)
  - **Per-file overrides** — right-click context menu: ⚙️ Override Settings (per-file audio codec, bitrate, strip/mux/metadata options, container), 📎 Manage Subtitles (add/remove/toggle main↔forced), Media Details (editable tag editor), 🔄 Re-probe File, ❌ Clear Override, 🗑️ Remove; ⚙️ icon on files with overrides; double-click opens Media Details
  - **File re-probe after processing** — automatically re-probes all completed files to update Audio, Subs, and Size columns with fresh data; confirms changes took effect; also available manually via right-click
  - **Preflight check** — validates file readability, empty files, missing subtitles (language-aware), audio codec detection before processing
  - File list with audio codec, internal sub count, external sub detection columns
  - Drag-and-drop file/folder support
  - Threaded processing with progress bar, per-file status, stop button
  - Color-coded log panel with Clear Log button
  - Cleans up `.srt` files after successful muxing
  - `get_audio_info()` ffprobe helper for audio stream detection (replaces `mediainfo` dependency)
  - **Saved preferences** — all options auto-save to `preferences.json` (under `media_processor` key) when the window is closed and are restored on next open; fresh installs start with all operations unchecked so the user chooses their own defaults
- **Bitmap subtitle OCR** (PGS/VobSub → SRT via Tesseract):
  - Extracts bitmap subtitle streams and converts to text via OCR
  - **Single-pass rendering** — overlays subtitle stream on a black canvas with scene-change detection, outputs one PNG per subtitle event in one ffmpeg pass (~2 min for a 1-hour episode)
  - **Parallel OCR** — `ThreadPoolExecutor` runs Tesseract on multiple CPU cores simultaneously
  - **Smart cropping** — `getbbox()` crops each frame to just the subtitle text region before OCR (~13x fewer pixels for Tesseract to process)
  - **Music note detection** — `_is_music_note_frame()` detects tiny isolated content (♪/♫ symbols) by pixel density and replaces with ♪ without running Tesseract
  - **Live OCR monitor window** — real-time progress bar with ETA, current subtitle image preview, OCR'd text result, scrolling cue list that builds live, cancel button
  - **OCR post-processing** (`_fix_ocr_text()`) — comprehensive regex-based cleanup for common Tesseract mistakes:
    - `|` → `I` (pipe never appears in subtitles)
    - `/` → `l` or `I` (context-dependent: between letters → `l`, standalone → `I`)
    - `//` → `ll`, `/7/` → `I'll`, `17/` → `I'll`, `/17/` → `I'll` (slash/digit combos = I'll)
    - `1` → `I` (in word context, preserves real numbers like `10`, `1-0`)
    - `!` → `I` (before contractions: `!'m` → `I'm`)
    - `l` → `I` (standalone or at sentence start: `l am` → `I am`)
    - `/[` → `I`, `/I` → `I` (bracket/slash garble)
    - `™` → `'` (trademark symbol misread as apostrophe)
    - Music note markers: `2`, `>`, `$`, `&`, `£`, `©`, `»`, `#`, `*`, `?` at start/end of lines → `♪`; handles dash-prefixed (`-2`, `-£`, `->`) and no-space variants (`>And`); end-of-line garble (`Sf`, `D>`, `P`, `If`, `f`) → `♪`; `$f`/`£f` ligatures → `♪`; `-)`→ `-♪`; markers after `[Speaker]` brackets → `♪`
    - Garbled-only cues (1-3 junk characters) → `♪`
  - **Empty subtitle track detection** — probes `NUMBER_OF_FRAMES` / `NUMBER_OF_BYTES` from muxer statistics; empty tracks shown with `[⚠ EMPTY]` flag in red, unchecked by default, format dropdown disabled, edit button hidden, skipped during extraction
  - **"Set All To" dropdown** — in Internal Subtitles dialog, sets all tracks' "Convert To" format at once (copy, srt, ass, webvtt, ttml, extract only, drop)
  - Integrated into Internal Subtitles dialog — automatically triggers OCR when a bitmap track is set to a text format and Extract is clicked
  - Offers to open OCR results in the subtitle editor for review/cleanup
  - **Dependencies:** `tesseract-ocr` + `tesseract-ocr-eng` (system), `pytesseract` (pip) — added to installer with auto-detection
- **Spell checker** (Tools → Spell Check, F7 in subtitle editor):
  - Scans all cues for spelling errors using `pyspellchecker`
  - Interactive correction dialog: Replace, Replace All, Skip, Ignore, Add to Dict, Add as Name
  - Salmon/red row highlighting for cues with spelling errors
  - Custom dictionary (`custom_spell_words`) persisted to preferences
  - Integrates `custom_cap_words` (character names) as known words
  - Auto-install prompt if `pyspellchecker` is not installed
  - Available in both standalone and internal subtitle editors
- **Search/Replace List** (Tools → Search/Replace List in subtitle editor):
  - Persistent find/replace pairs for common corrections
  - Add, remove, clear pairs with case-sensitive toggle
  - "Apply All" runs every rule across all cues in one pass with undo
  - Shared with Batch Filter's search & replace pairs
- **Smart Sync** (Timing → Smart Sync in subtitle editor):
  - Auto-syncs subtitles to video audio using Whisper speech recognition
  - **Engine selection:**
    - **Standard (faster-whisper)** — segment/word-level timestamps (~400ms accuracy), CPU-optimized via CTranslate2, lightweight (~200MB)
    - **Precise (WhisperX)** — phoneme-level forced alignment via `wav2vec2` (~50ms accuracy), GPU-accelerated when CUDA available, requires PyTorch (~2GB)
  - **Quick Scan** — samples N segments (configurable) across the video for fast offset detection
  - **Full Scan** — transcribes entire audio for maximum anchor points (for Re-time)
  - **Direct Align** (WhisperX only) — skips transcription; aligns subtitle text directly against audio via forced alignment; produces per-cue timestamps for every cue; fastest mode, same-language only
  - **Apply Sync** — single global offset (median of matched pairs)
  - **Re-time All** — per-cue timestamp adjustment using matched anchor points with piecewise linear interpolation; handles frame rate changes, different cuts, different sources (streaming → Blu-ray)
  - `retime_subtitles()` utility: builds anchor map from matches, linearly interpolates unmatched cues, extrapolates before/after anchors
  - **Sequential matching** with offset consistency check (±30s tolerance) — prevents cross-matching of repeated phrases
  - **Text normalization** strips speaker labels, HI annotations, music notes before comparison — enables SDH ↔ regular subtitle matching
  - **Word-level timestamps** from Whisper for ~300ms better precision (Standard); phoneme-level alignment for ~50ms precision (Precise)
  - **Fine-tune offset** — configurable ±2000ms adjustment (default +400ms for Standard, +200ms for Precise) to compensate for timing differences
  - **Duration mismatch warning** — alerts if video and subtitle lengths differ by >15%
  - Whisper model selection (tiny/base/small), language setting, configurable segments
  - Auto-detects video file from subtitle path; zenity file picker with subtitle directory as starting point
  - Auto-backup (`_presync` file) before applying sync
  - Progress bar, results log, cancel support
  - Auto-install prompt for `faster-whisper` and `whisperx`
  - **Dependencies:** `faster-whisper` (pip) for Standard engine; `whisperx` (pip, pulls in PyTorch) for Precise engine — both auto-installable from the dialog
- **Quick Sync** (Timing → Quick Sync submenu in subtitle editor):
  - **Set First Cue Time** — shift all cues so the first cue starts at a user-specified timestamp; live offset preview updates as you type
  - **mpv player integration** — ▶ Play Video launches mpv with IPC socket, paused, with OSD timestamp (including milliseconds); ⏱ Mark Time queries mpv's exact playback position and fills the timestamp field; auto-detects video file from subtitle path; cleans up mpv on dialog close
  - **Dependencies:** `mpv` (system) — optional, for Play/Mark workflow; manual timestamp entry works without it
- **"Open with" support** — app appears in file manager right-click menu for video files; also accepts files via command line (`docflix video.mkv`)
- **Batch ETA** — real-time estimated time remaining for the entire batch, based on rolling average encoding speed weighted by file duration
- **Estimated output size** calculation before conversion
- **Media info** panel (shows codec, resolution, duration, streams)
- **Video Scaler** (Tools > Video Scaler, `Ctrl+Shift+R`) — standalone batch video scaling tool with resolution presets (Original, 2160p, 1440p, 1080p, 720p, 480p, Custom WxH), GPU-accelerated scaling (scale_cuda, scale_qsv, scale_vaapi), aspect ratio preservation, upscale warning, encoder/preset/CRF selection, audio passthrough or re-encode, drag-and-drop, output to folder or in-place replacement, file list with source/target resolution columns. Also available as `docflix-scale` standalone command.
- **Add chapters** — auto-generate evenly-spaced chapter markers at a configurable interval (1–60 minutes, default 5); injected via FFMETADATA1 temp file as an extra ffmpeg input with `-map_chapters`; mutually exclusive with "Strip chapters" (checking one unchecks the other); available in main settings panel, per-file overrides, and Media Processor; supports import of chapter files (FFMETADATA1 and OGM formats); persisted to preferences
- **Edition tagging** — tag videos with version info (Theatrical, Director's Cut, Extended, IMAX, etc.) written to the container `title` metadata field via ffmpeg `-metadata title=...`; preset dropdown with 12 common editions plus custom text entry; optional Plex-compatible `{edition-...}` tag in output filename; works independently of "Set track metadata"; available in main settings panel, per-file override dialog, and Media Processor; persisted to preferences
- **Media Details** (right-click → Media Details, `Ctrl+Shift+I`, or Tools menu) — comprehensive file analysis and tag editor in a tabbed dialog (General, Video, Audio, Subtitles, Chapters, Attachments, Full Report):
  - **Editable General tab** — container title/edition as editable text field; read-only file info (format, duration, size, bitrate, streams)
  - **Editable Video tab** — full read-only stream info (codec, profile, level, resolution, frame rate, pixel format, bit depth, color range/space/transfer/primaries, HDR format with mastering display and content light level, bitrate, duration) + editable Title and Language per stream
  - **Editable Audio tab** — read-only stream info (codec, profile, sample rate, channels, layout, bitrate) + editable Title, Language, and Flags (Default, Commentary) per stream
  - **Editable Subtitles tab** — read-only stream info (codec, events, resolution) + editable Title, Language, and Flags (Default, Forced, Hearing impaired/SDH, Commentary) per stream
  - **Chapters tab** — two modes: view mode (read-only treeview with chapter count and "Edit Chapters..." button) when chapters exist; edit mode (Add, Remove, Clear All, auto-generate every N minutes, double-click inline title editing) when no chapters or user clicks Edit
  - **Attachments tab** — read-only attachment/data stream info
  - **Full Report tab** — complete text dump of all probe data
  - **Save via ffmpeg remux** — builds `-metadata:s:TYPE:N` for track names/language and `-disposition:STREAM_IDX` for flags; chapters via FFMETADATA1 temp file; atomic temp file replacement; background thread with real-time progress bar parsing ffmpeg `time=` output; chapter temp file cleanup
  - **Disposition safety** — when any disposition flag changes, ALL streams of that type get explicit flags (prevents multiple defaults)
  - **Unsaved changes warning** — Yes (save then close) / No (discard) / Cancel on close with pending changes
  - **Window centering** — centers on parent window (main app or Media Processor) via withdraw/position/deiconify pattern
  - Copy to Clipboard and Copy Full Report buttons
- **Test encode** (30-second preview clip of settings)
- **✅ Clear Finished** button — removes completed/skipped files from queue, leaving failed files for retry
- Source file and output file **playback** via configurable media player:
  - **System Default** — delegates to `xdg-open`
  - **auto** — tries common players in order: vlc → mpv → totem → ffplay
  - Named player (vlc, mpv, etc.) — uses that specific player if installed
  - **Custom path** — full path to any executable
- Open output folder in system file manager
- Sortable, reorderable file queue
- Collapsible, scrollable settings panel (PanedWindow with draggable divider) and detachable log window
- **Sound notification** on conversion completion (configurable in Default Settings)
- **Preferences** auto-saved to JSON on dialog close — no manual save required
  - **Note:** Video bitrate is intentionally excluded from saved preferences — it always resets to 2.0M on launch to prevent hidden mismatches between a saved value and the UI slider
- Recent folders menu
- Keyboard shortcuts panel
- **Custom logo** in title bar (`logo_transparent.png` at 32×32 px); falls back to 🎬 emoji if unavailable

### UI / UX Notes
- Title bar shows app name only — no working directory path displayed
- Settings menu has no "Save Preferences" item; preferences auto-save when the Default Settings dialog is closed via Save
- Preference saves are confirmed via a log entry only — no popup dialogs
- Window launches on the monitor containing the mouse pointer (no wrong-monitor flash)
- Header layout (Option C): Title + encoder combo on top row, separator, then toolbar row with folder controls + output path
- Folder browser dialogs use **zenity** (GTK native, single-click + Open) with tkinter `askdirectory` fallback
- GPU backend names in encoder dropdown are short labels (e.g. "NVIDIA (NVENC)") without GPU model names
- External subtitle dialog uses grid layout with right-justified controls; filename column stretches on resize
- Backward compatibility: old `encoder: 'gpu'` preference values auto-map to first available GPU backend
- **High-DPI scaling** — auto-detects display DPI via `Xft.dpi` / `GDK_SCALE` / `QT_SCALE_FACTOR` and applies `tk scaling` at startup; all widgets, fonts, and dialogs scale to match the system's display settings

### GPU Backend Configuration (`GPU_BACKENDS` dict)

Each backend defines:
- `hwaccel` flags (e.g. `-hwaccel cuda`)
- Per-codec encoder names (e.g. `hevc_nvenc`, `hevc_qsv`, `hevc_vaapi`)
- Presets and defaults
- Quality flag (`-cq`, `-global_quality`, `-qp`)
- Multipass support and args
- Detection method (ffmpeg encoder check + test encode verification + GPU name via nvidia-smi / lspci)
- QSV multi-method initialization (direct MFX, VAAPI backend via libvpl/oneVPL, explicit device init)
- Automatic CPU fallback when GPU encoding fails mid-conversion
- **GPU Test Mode** (`--gpu-test-mode`) — skips the test encode verification (Tier 2) and accepts GPU backends based on ffmpeg encoder availability (Tier 1) + lspci GPU name identification (Tier 3) only. Useful for testing GPU detection logic in environments without real GPU hardware (e.g. VMs with spoofed PCI devices).

---

## Interface 2: Bash CLI (`convert_videos.sh`)

Headless batch converter; runs in the **current directory** and converts all supported video files found there (`.mkv`, `.mp4`, `.avi`, `.mov`, `.wmv`, `.flv`, `.webm`, `.ts`, `.m2ts`, `.mts`). Best for scripted/automated use.

**Dependencies:** `bash`, `ffmpeg`, `zenity` (optional, for desktop popups)

### Command-Line Options

| Flag | Description | Default |
|------|-------------|---------|
| `-b`, `--bitrate` | Video bitrate | `2M` |
| `-q`, `--crf` | CRF quality value (disables bitrate mode) | disabled |
| `-p`, `--preset` | CPU ffmpeg preset | `ultrafast` |
| `-g`, `--gpu` | Use NVIDIA GPU (hevc_nvenc) | off |
| `--qsv` | Use Intel Quick Sync Video (hevc_qsv) | off |
| `--vaapi` | Use VAAPI encoding (hevc_vaapi) | off |
| `-P`, `--gpu-preset` | GPU preset (NVENC: p1–p7, QSV: veryfast–veryslow) | varies |
| `-s`, `--suffix` | Output filename suffix | `-2mbps-UF_265` |
| `-o`, `--overwrite` | Overwrite existing output files | skip |
| `-c`, `--cleanup` | Delete originals after success | off |
| `-n`, `--no-log` | Disable log file | off |
| `-h`, `--help` | Show usage | — |

### Output Naming Convention
Input: `movie.mkv` → Output: `movie-2mbps-UF_265.mkv` (suffix varies by mode/preset/backend)
GPU outputs include backend short name: `-NVENC_H265_p4`, `-QSV_H265_medium`, `-VAAPI_H265_default`

### Notifications
- Uses **zenity** desktop popups if available
- Falls back to terminal summary if not

---

## Launcher: `run_converter.sh`

Launches the Tkinter GUI as a background process with full logging.

- Checks for `python3`, `tkinter`, and `ffmpeg` before launching
- Creates a timestamped log file in `logs/video_converter_YYYYMMDD_HHMMSS.log`
- Launches via `nohup ... &` — terminal is free immediately after launch
- Prints the PID and a `tail -f` command to follow the log
- Auto-prunes the `logs/` folder to the 10 most recent files

---

## Installer: `install.sh`

Installs the app to user-local directories — no `sudo` required.

| Path | Purpose |
|------|---------|
| `~/.local/share/docflix/` | App files |
| `~/.local/share/icons/docflix.png` | App icon |
| `~/.local/share/applications/docflix.desktop` | System app menu entry |
| `~/.local/share/applications/docflix-subs.desktop` | "Open with" entry for subtitle files (NoDisplay) |
| `~/.local/bin/docflix` | Terminal launch command |

**Steps performed by the installer:**
1. Check all required source files are present
2. Check and report missing system dependencies (`python3`, `tkinter`, `ffmpeg`, `pip3`)
3. Install Python packages (`tkinterdnd2`, `Pillow`) via `pip install --user`
4. Copy app files to `~/.local/share/docflix/`
5. Generate `logo_transparent.png` from `logo.png` using Pillow
6. Install icon to `~/.local/share/icons/`
7. Create `.desktop` entry for system app menu
8. Create `docflix` terminal command in `~/.local/bin/`

```bash
./install.sh             # Install or update
./install.sh --uninstall # Remove all installed files
```

---

## Encoding Options

### CPU Encoding (libx265)
| Mode | Parameter | Recommended Range |
|------|-----------|-------------------|
| Bitrate | `-b:v` | 1M – 8M+ |
| CRF | `-crf` | 18–28 (lower = better quality) |

**Presets (fastest → best quality):**  
`ultrafast` · `superfast` · `veryfast` · `faster` · `fast` · `medium` · `slow` · `slower` · `veryslow`

### GPU Encoding — NVIDIA (NVENC)
| Mode | Parameter | Recommended Range |
|------|-----------|-------------------|
| Bitrate | `-b:v` | 1M – 8M+ |
| CRF/CQ | `-cq` | 15–25 (lower = better quality) |

**Presets (fastest → best quality):** `p1` · `p2` · `p3` · `p4` · `p5` · `p6` · `p7`

### GPU Encoding — Intel (QSV)
| Mode | Parameter | Recommended Range |
|------|-----------|-------------------|
| Bitrate | `-b:v` | 1M – 8M+ |
| Quality | `-global_quality` | 15–25 (lower = better quality) |

**Presets (fastest → best quality):** `veryfast` · `faster` · `fast` · `medium` · `slow` · `slower` · `veryslow`

### GPU Encoding — AMD / VAAPI
| Mode | Parameter | Recommended Range |
|------|-----------|-------------------|
| Bitrate | `-b:v` | 1M – 8M+ |
| Quality | `-qp` | 15–25 (lower = better quality) |

**Presets:** None — quality controlled via bitrate/QP only.

> **Note:** GPU encoding is significantly faster but may produce slightly larger files at equivalent quality settings. Audio is always stream-copied by default.

---

## External Subtitle Support

### Supported Formats
`.srt`, `.ass`, `.ssa`, `.vtt`, `.sub`, `.idx`, `.sup`

### Auto-Detection from Filename
| Filename Pattern | Language | Forced | Default |
|---|---|---|---|
| `movie.srt` | und | ☐ | ✅ (first plain sub) |
| `movie.eng.srt` | eng | ☐ | ✅ (first plain sub) |
| `movie.eng.forced.srt` | eng | ✅ | ☐ |
| `movie.eng.sdh.srt` | eng | ☐ | ☐ |
| `movie.eng.cc.srt` | eng | ☐ | ☐ |
| `movie.fra.srt` | fra | ☐ | ☐ (default already taken) |

### Filename Stem Matching
Progressively strips up to 3 trailing dot-separated tokens:
- `movie.eng.forced.srt` → tries `movie.eng.forced`, `movie.eng`, `movie` → matches `movie.mkv`

### Embed vs Burn-in
| Mode | How it works | HW Decode | Togglable |
|---|---|---|---|
| **embed** | Muxed as subtitle stream (`-i sub.srt -map`) | Compatible | Yes (player controls) |
| **burn_in** | Rendered onto video (`-vf subtitles=`) | Auto-disabled | No (permanent) |

### Container Considerations
- **MKV**: supports all subtitle formats natively
- **MP4**: external subs auto-converted to `mov_text`
- **TS (MPEG-TS)**: text subtitles dropped (only DVB subtitles supported); CC data preserved via A53 passthrough
- **AVI**: no subtitle support
- Bitmap subtitles (`.sup` PGS, `.sub` VobSub): embed only — cannot be burned in

---

## Quick Start

```bash
# App menu — search "Docflix Video Converter"

# Terminal
docflix

# Or directly from source
cd /home/docman1967/scripts/video_converter
./run_converter.sh

# CLI (run from the folder containing your video files)
cd /path/to/your/videos
/home/docman1967/scripts/video_converter/convert_videos.sh          # CPU defaults
/home/docman1967/scripts/video_converter/convert_videos.sh -g       # GPU NVIDIA fastest
/home/docman1967/scripts/video_converter/convert_videos.sh --qsv    # GPU Intel QSV
/home/docman1967/scripts/video_converter/convert_videos.sh --vaapi  # GPU AMD VAAPI
/home/docman1967/scripts/video_converter/convert_videos.sh -q 22    # CRF quality mode

# GPU test mode (skip hardware verification — detection only)
python3 video_converter.py --gpu-test-mode
```

---

## GitHub Workflow

```bash
# After making changes to source files:
cd /home/docman1967/scripts/video_converter
git add -A
git commit -m "Description of what changed"
git push

# Then reinstall to apply changes:
./install.sh
```

---

## Dependencies Summary

| Dependency | Required By | Install |
|------------|-------------|---------|
| `ffmpeg` | Both interfaces | `sudo apt install ffmpeg` |
| `python3` | Desktop GUI | `sudo apt install python3` |
| `tkinter` | Desktop GUI | `sudo apt install python3-tk` |
| `tkinterdnd2` | Desktop GUI (drag & drop) | `pip install tkinterdnd2` |
| `Pillow` | Desktop GUI (logo image) | `pip install Pillow` |
| `zenity` | Both (folder dialogs, CLI popups) | `sudo apt install zenity` |
| `ccextractor` | CC extraction from .ts files (optional) | `sudo apt install ccextractor` |
| `tesseract-ocr` | Bitmap subtitle OCR (optional) | `sudo apt install tesseract-ocr tesseract-ocr-eng` |
| `pytesseract` | Python bindings for Tesseract (optional) | `pip install pytesseract` |
| `pyspellchecker` | Subtitle spell checker (optional) | `pip install pyspellchecker` |
| `faster-whisper` | Smart Sync — Standard engine (optional) | `pip install faster-whisper` |
| `whisperx` | Smart Sync — Precise engine (optional, pulls in PyTorch ~2GB) | `pip install whisperx 'transformers<4.45'` |
| `mpv` | Quick Sync — video playback with Mark Time (optional) | `sudo apt install mpv` |
| `langdetect` | Subtitle language detection in TV Show Renamer (optional) | `pip install langdetect` |
| NVIDIA driver + NVENC-enabled ffmpeg | NVIDIA GPU encoding (optional) | System-specific |
| Intel media driver + QSV-enabled ffmpeg | Intel QSV encoding (optional) | System-specific |
| Mesa VAAPI driver + VAAPI-enabled ffmpeg | AMD VAAPI encoding (optional) | System-specific |

---

## Known Issues / Notes

1. **HW Decode compatibility** — Some source files (particularly those with mid-stream resolution changes or oddly encoded content) fail with hardware decode enabled. The NVENC backend no longer uses `-hwaccel_output_format cuda` to avoid filter reinitialization errors on variable-resolution sources. Workaround for remaining issues: uncheck **HW Decode** for the affected file via per-file settings override, or disable it globally in Default Settings. The GPU still handles encoding; only decoding falls back to CPU.

2. **Burn-in subtitles + HW Decode** — Burn-in subtitles require CPU-side video filtering, which is incompatible with hardware decode. The app automatically disables HW decode when any external subtitle is set to burn-in mode.

3. **Audio handling** — Default audio codec is AC3 (Dolby Digital) at 320k. Can be changed per-file via settings override or globally in Default Settings.

4. **Subtitle handling** — The desktop GUI supports both internal subtitle management (per-stream format control) and external subtitle attachment (embed/burn-in with language, default, forced flags). All subtitle streams are correctly preserved in both the default conversion path and the per-file subtitle dialog path.

5. **QSV/VAAPI without hardware** — ffmpeg may report QSV or VAAPI encoders as available even without matching GPU hardware (the encoders are compiled in but will fail at encode time). As of v1.3.1, the app runs a quick test encode during startup to verify each GPU backend actually works before showing it in the dropdown. If a GPU passes detection but fails during a real encode, the app automatically retries with CPU encoding. The `--gpu-test-mode` flag intentionally bypasses this verification for testing GPU detection in VMs without real hardware — **do not use test mode for actual encoding**, as the selected GPU encoder will fail without working hardware/drivers.

6. **MP4 cover art / embedded PNG** — Some MP4 files contain a PNG image as a second video stream (album art / thumbnail). Mapping all video streams (`-map 0:v?`) causes encoding to fail because the PNG can't go through the video encoder pipeline. Fixed by mapping only the first video stream (`-map 0:v:0?`).

7. **mov_text subtitles in MKV** — MP4 files with `mov_text` subtitle streams will fail if copied directly into MKV containers (MKV doesn't support `mov_text`). The app now auto-detects `mov_text` streams and converts them to SRT when outputting to MKV.

8. **Video bitrate not persisted** — Video bitrate is intentionally excluded from saved preferences. It always resets to the default (2.0M) on launch to avoid silent mismatches where a saved value differs from what the user expects. The slider/entry in the UI is the single source of truth.

---

## TODO / Roadmap

- [x] ~~**Support for Transport files** — Add support for MPEG Transport Stream (`.ts`, `.mts`, `.m2ts`) input files~~ *(completed 2026-04-22)*
- [x] ~~**Media Processor / metadata cleanup** — Integrate remux post-processing pipeline (audio conversion, metadata, tag/chapter stripping, subtitle muxing) into the GUI as both conversion pipeline options and a standalone Tools → Media Processor window~~ *(completed 2026-04-25)*
- [x] ~~**Video scaling / resolution change** — Standalone Video Scaler tool with resolution presets (2160p, 1440p, 1080p, 720p, 480p, Custom), GPU-accelerated scaling, aspect ratio preservation, upscale warning, progress/ETA, preferences~~ *(completed 2026-04-29)*
- [x] ~~**Subtitle spell checker** — Spell checking in subtitle editor with interactive correction dialog, custom dictionary, auto-install~~ *(completed 2026-04-25)*
- [x] ~~**Smart Sync** — Whisper-based auto-sync with Quick Scan / Full Scan modes, Apply Sync (offset) and Re-time All (per-cue interpolation)~~ *(completed 2026-04-25)*
- [x] ~~**Smart Sync matching improvements** — Sequential matching, offset consistency check, two-pass matching~~ *(completed 2026-04-25)*
- [x] ~~**WhisperX integration** — Add WhisperX as a "Precise" mode in Smart Sync for phoneme-level forced alignment (~50ms timestamp accuracy vs ~400ms with faster-whisper); eliminates the need for fine-tune offset; requires PyTorch (~2GB)~~ *(completed 2026-04-26)*
- [x] ~~**Modular package architecture (v2.0.0)** — Split monolith into 16 independent modules under `modules/` package. Standalone tool launchers (`docflix-subs`, `docflix-rename`, `docflix-media`). StandaloneContext for shared preferences. Incremental spell checker. Installer updated for package structure.~~ *(completed 2026-04-27)*
- [x] ~~**Video scaling / resolution change** — Standalone Video Scaler tool with resolution presets (2160p, 1440p, 1080p, 720p, 480p, Custom), GPU-accelerated scaling, aspect ratio preservation, upscale warning, progress/ETA, preferences~~ *(completed 2026-04-29)*
- [ ] **Compile to binary / code protection** — Use Nuitka to compile to a standalone executable for distribution; make GitHub repo private; change license from MIT to proprietary
- [x] ~~**Remove dead code from monolith** — Removed ~8,560 lines of duplicated code marked UNUSED in `video_converter.py` (standalone subtitle editor, media processor, TV renamer, internal subtitle editor) now that Tools menu launches use the module imports~~ *(completed 2026-05-01)*
- [ ] **Complete monolith migration** — Continue migrating remaining inline code in `video_converter.py` to modules; eventually remove the monolith entirely and run from the package
- [ ] **Standalone app launcher icons** — Create unique icons for each standalone tool (Subtitle Editor, Media Processor, Media Renamer, Media Rescale) so they're visually distinct from the main suite icon
- [ ] **App menu integration** — Add `.desktop` entries for each standalone tool with right-click quick actions (Option C) or individual launcher entries; requires unique icons first

### TV Show Renamer Improvements
- [x] ~~**Undo after rename** — Keep a rename history so the user can revert file renames (store old → new path mappings, add an Undo button)~~ *(completed 2026-04-27)*
- [x] ~~**Manual episode number editing** — Allow the user to manually set/correct season and episode numbers in the treeview when the filename parser fails (right-click → Set Episode or Edit → Set Episode)~~ *(completed 2026-04-27)*
- [x] ~~**TMDB support** — Add The Movie Database (TMDB) as an alternative metadata source alongside TVDB; let the user choose which provider to query~~ *(completed 2026-04-27)*
- [x] ~~**Multi-episode file support** — Handle multi-episode filenames like `S01E01E02` or `S01E01-E03`; generate combined episode titles in the output name (e.g. `Show - S01E01-E02 - Title 1 & Title 2`)~~ *(completed 2026-04-27)*
- [x] ~~**Right-click context menu on treeview** — Add context menu with Set Episode, Remove Selected, Open Folder, Copy New Name, Remove Show, Clear All~~ *(completed 2026-04-27)*
- [x] ~~**Progress indication during API calls** — Run TVDB/TMDB API calls in a background thread with a progress bar, per-show status, and cancel button~~ *(completed 2026-04-27)*
- [ ] **Import templates from Filebot** — Add an "Import from Filebot" option in the File Renamer's Templates dialog that converts Filebot Groovy template syntax to Docflix format. Variable mapping: `{n}`→`{show}`, `{s00e00}`→`S{season}E{episode}`, `{t}`→`{title}`, `{y}`→`{year}`, `{s}`/`{s00}`→`{season}`, `{e}`/`{e00}`→`{episode}`. Warn on unsupported Groovy expressions (method calls, conditionals). Accept paste, file import (`.groovy`), or `presets.json` import.

---

## Change Log

### 2026-05-11 (Enhancement + Bug Fix)
277. **File Renamer foreign/localized title handling** — Added two improvements for non-English titles: (1) **Transliteration retry** — when the initial API search returns no results, retry with diacritics/accents stripped (e.g. "Château" → "Chateau", "Señor" → "Senor") using `unicodedata.normalize('NFKD')`. (2) **Alias/original name matching** — the close-matches loop now checks `original_name` (TMDB's original language title) and `aliases` (TVDB's alternative titles) when matching search results to the query. This catches cases where the filename uses a foreign/alternative title that differs from the primary English title. Added `original_name` field to TMDB TV and movie search result normalization.
276. **File Renamer year-based auto-disambiguation** — When multiple shows match a search query (e.g. "Ghosts" returns "Ghosts (2019)", "Ghosts (US)", "Ghosts (DE)"), the renamer now extracts a year from the filename or parent folder (e.g. "Ghosts (2019)" or "Battlestar.Galactica.2003") and automatically selects the show whose premiere year matches, skipping the disambiguation dialog. Only triggers when exactly one result matches the year. Falls through to the user dialog if zero or multiple shows match.
275. **File Renamer special episodes / Season 0** — Added detection for special episode patterns: `SP01`/`SP 01` (mapped to Season 0), keyword-based markers (`Special`, `Bonus`, `Extra`, `Behind the Scenes`) with optional episode numbers. `S00Exx` was already handled by the existing `SxxExx` regex. Updated `_clean_show_name()` to strip `SP`, `OVA`, and keyword markers so they don't pollute show name extraction (with a safety check for "Special" — only truncates if preceded by at least 2 words, preserving names like "Special Agent Oso"). Updated `_tmdb_get_episodes()` to fetch Season 0 (specials) from TMDB when the show has a specials season.
274. **File Renamer release group and codec tag filtering** — Enhanced `_clean_show_name()` with comprehensive scene release tag stripping: (1) **Bracketed tags** stripped early (`[YTS]`, `[RARBG]`, `[YTS.MX]`). (2) **Codec tags** truncate at `x264`, `x265`, `h264`, `h265`, `HEVC`, `AVC`, `AAC`, `DDP5.1`, `FLAC`, `10bit`, `ATMOS`, `TrueHD`, `DTS-HD`. (3) **Streaming service tags** truncate at `AMZN`, `NF`, `HULU`, `DSNP`, `ATVP`, `PCOK`, `PMTP`, `STAN`, `CRAV`, `MAX`, `HBO`, `APTV`. (4) **Trailing release groups** stripped (`-GRACE`, `-DHD`, `-FLUX`) with a safety check requiring the first character to be a letter (protects show names like "9-1-1"). Same patterns added to `_match_episode_by_title()` for consistency.
273. **File Renamer date-based underscore separator** — Added underscore (`_`) to the date separator character class in `_parse_episode_info()`, so `YYYY_MM_DD` formatted dates in filenames are recognized alongside the existing `.`, `-`, and space separators.
272. **File Renamer episode-only filenames use folder name** — Fixed files with only season/episode markers in the filename (e.g. `S01E03.mkv`, `01x05.mkv`) not being matched to any show. Root cause: `_clean_show_name("S01E03")` returns an empty string (the entire filename is consumed by the episode marker regex), causing both `_auto_load_shows()` and `_match_file_to_show()` to skip the file entirely. Two fixes: (1) In `_auto_load_shows()`, when the cleaned filename is empty, fall back to the parent folder name via `_get_show_folder()` and add it directly to `show_names` — so a file at `Ghosts (US)/Season 1/S01E03.mkv` triggers an API search for "Ghosts (US)". (2) In `_match_file_to_show()`, when the cleaned filename is empty, use `folder_cleaned` as the search key instead of returning `None` — so the file gets matched to the loaded show via folder name.
271. **Fix ALL CAPS — optional names database** — Added an optional downloadable names database (1.1M+ first and last names from Aptivi/NamesList) to the Fix ALL CAPS filter for improved proper name capitalization, especially in documentary subtitles. Changes across 5 files: (1) **`subtitle_filters.py`** — new module-level names DB infrastructure: `NAMES_DB_DIR`, `NAMES_DB_URLS`, `load_names_db()`, `unload_names_db()`, `is_names_db_available()`, `is_names_db_loaded()`, `get_names_db_count()`. The loader reads `FirstNames.txt` + `Surnames.txt` from `~/.local/share/docflix/names/` and filters out common English words using the system dictionary (`/usr/share/dict/words`) — only lowercase dictionary entries are used as exclusions, so proper nouns (Eisenhower, Kennedy, Roosevelt, Lincoln, Washington, etc.) are preserved while common words (the, and, nation, fight, will, grace, stone, etc.) are excluded. Falls back to a comprehensive hardcoded `_NAMES_AMBIGUOUS` set (~500 words) when no system dictionary is available. Added `use_names_db` parameter to `filter_fix_caps()` and `_cap_word()` closure. (2) **`subtitle_editor.py`** — added "Names Database (optional)" LabelFrame to both copies of `show_fix_caps_dialog()` with: status label, "Use Names Database" checkbox (loads/unloads DB on toggle), "Download Names Database" button (threaded download from GitHub raw URLs, auto-loads and enables on completion), description text. Updated all `filter_fix_caps()` call sites (Apply buttons + auto-caps in Remove HI) to pass `use_names_db`. (3) **`preferences.py`** — save/load `use_names_db` preference; auto-load names DB on startup when preference is enabled. (4) **`standalone.py`** — same preference save/load/auto-load for standalone editor launches. (5) **`batch_filter.py`** — pass `use_names_db` to Fix ALL CAPS lambda.
270. **File Renamer subtitle-only files fail show detection** — Fixed "Could not detect any show names from filenames" when loading only subtitle files (.srt, .ass, etc.) without any video files. Root cause: `_auto_load_shows()` unconditionally skipped all files with subtitle extensions to avoid language/tag tokens (`.eng`, `.forced`, `.sdh`) polluting show name extraction. When no video files were present, `show_names` was always empty. Fixed by checking `has_video` first — subtitle files are still skipped when video files are present (preferred), but when only subtitle files are loaded, they are processed with trailing subtitle tag tokens stripped before `_clean_show_name()` runs. The stripping walks backward through dot-separated filename parts removing known language codes and tag words (forced, sdh, cc, hi). Same fallback applied to the source folder detection in `_ask_user_pick_show()`.

### 2026-05-10 (Bug Fix)
269. **File Renamer Multiple Matches dialog blank on high-DPI** — Fixed the Multiple Matches disambiguation dialog rendering as a completely empty dark window on high-resolution displays (200% scaling). Root cause: `tk.Label` placeholder thumbnails were created with `width=int(60*dpi)` and `height=int(90*dpi)` but **without an image**, so Tk interpreted those values as *character units* (characters wide × lines tall) instead of pixels. At 200% scaling, each placeholder became 120 characters wide × 180 lines tall = ~964×3424 pixels — each card consumed the entire dialog, pushing all show titles, metadata, and synopses out of view. Also caused cascading "Thumbnail error" log messages because the broken layout corrupted grid operations when async thumbnails tried to apply. Fixed by creating a blank `tk.PhotoImage` of the desired pixel dimensions and assigning it to each placeholder label — `tk.Label` with an image treats `width`/`height` as pixels. The blank image is stored on the widget (`._blank`) to prevent garbage collection. Standard DPI (100%) was unaffected because 60×90 character units happened to be large enough to still show content.

### 2026-05-09 (Enhancement)
266. **File Renamer episode title matching for files without SxxExx** — Files without season/episode markers in the filename (e.g. `America.Facts.vs.Fiction.World.War.II.720p.WEB.x264-DHD-Obfuscated`) now get their episode identified via three changes: (1) **Period stripping in `_normalize_for_match()`** — added `.replace('.', ' ')` so abbreviations like `vs.` in API names match `vs` in filenames (dots in filenames are separators, never punctuation). This was causing substring match failures everywhere: `"america facts vs fiction"` was not found in `"america facts vs. fiction"`. Also fixes `Dr.`, `Mr.`, `St.`, etc. (2) **Progressive query shortening** in `_load_show_by_name()` — when the initial API search returns 0 results and the query has many words (because the filename has no SxxExx separator and `_clean_show_name` can't tell where the show name ends and the episode title begins), the search progressively strips trailing words (e.g. `"America Facts vs Fiction World War II"` → `"America Facts vs Fiction World War"` → `"America Facts vs Fiction"`) until the API returns results. (3) **Episode title matching** via new `_match_episode_by_title()` function in `_refresh_preview()` — once the show is loaded with episode data, extracts the portion of the filename after the show name, strips quality/release tags, and compares it against all episode titles. Includes Roman numeral ↔ digit normalization (`II` → `2`, `III` → `3`, etc.) so `"World War II"` in the filename matches `"World War 2"` from the API. Also includes word-overlap fallback with prefix matching (first 4 chars) to handle typos in scene release filenames (e.g. `"Villians"` matches `"Villains"` via shared prefix `"vill"`); requires ≥80% word overlap to avoid false positives. For subtitle files, strips trailing language/tag suffixes (`.eng`, `.eng.forced`, `.sdh`, `.hi`, `.cc`) from the stem before matching, since `os.path.splitext` only removes the final `.srt` extension. When a match is found (exact or ≥60% overlap), the item's `season` and `episode` are populated automatically, producing a complete renamed filename (e.g. `America Facts vs. Fiction - S05E02 - World War 2.mkv`). Title matches logged for visibility.

### 2026-05-09 (v2.9.3 — File Renamer Title Matching, Edit Name, Thumbnail Fix)
268. **File Renamer Edit Name dialog** — Added "Edit Name..." to the right-click context menu and the Edit menu. Opens a dialog where the user can manually type the desired output filename (without extension). The custom name overrides the template-generated name and is shown in the New Filename column with type label "Edit". Useful when TVDB/TMDB doesn't have episode data yet (e.g. new seasons) or when the user wants a custom name. Clearing the field reverts to the template. Custom names are preserved through preview refreshes and used by the rename operation. Subtitle tags (`.eng`, `.forced`) are automatically appended.
267. **File Renamer Multiple Matches thumbnail errors on close** — Fixed "bad window path name" errors logged when the Multiple Matches disambiguation dialog is closed while thumbnail images are still downloading in the background. The async thumbnail thread and the `_apply_thumb` callback now check `dlg.winfo_exists()` before accessing any dialog widgets. The download thread also stops fetching remaining thumbnails once the dialog is closed, avoiding wasted network requests.
266. **File Renamer episode title matching for files without SxxExx** — Files without season/episode markers in the filename (e.g. `America.Facts.vs.Fiction.World.War.II.720p.WEB.x264-DHD-Obfuscated`) now get their episode identified via four changes: (1) **Period stripping in `_normalize_for_match()`** — added `.replace('.', ' ')` so abbreviations like `vs.` in API names match `vs` in filenames (dots in filenames are separators, never punctuation). Also fixes `Dr.`, `Mr.`, `St.`, etc. (2) **Progressive query shortening** in `_load_show_by_name()` — when the initial API search returns 0 results and the query has many words (because the filename has no SxxExx separator and `_clean_show_name` can't tell where the show name ends and the episode title begins), the search progressively strips trailing words (e.g. `"America Facts vs Fiction World War II"` → ... → `"America Facts vs Fiction"`) until the API returns results. (3) **Episode title matching** via new `_match_episode_by_title()` function in `_refresh_preview()` — once the show is loaded with episode data, extracts the portion of the filename after the show name, strips quality/release tags, and compares it against all episode titles. Includes Roman numeral ↔ digit normalization (`II` → `2`, `III` → `3`, etc.) so `"World War II"` in the filename matches `"World War 2"` from the API. Also includes word-overlap fallback with prefix matching (first 4 chars) to handle typos in scene release filenames (e.g. `"Villians"` matches `"Villains"` via shared prefix `"vill"`); requires ≥80% word overlap to avoid false positives. For subtitle files, strips trailing language/tag suffixes (`.eng`, `.eng.forced`, `.sdh`, `.hi`, `.cc`) from the stem before matching, since `os.path.splitext` only removes the final `.srt` extension. When a match is found (exact or ≥60% overlap), the item's `season` and `episode` are populated automatically, producing a complete renamed filename (e.g. `America Facts vs. Fiction - S05E12 - American Villains.mkv`). Title matches logged for visibility.

### 2026-05-08 (Bug Fix)
264. **File Renamer folder template rename with Season subfolders** — Fixed folder templates (e.g. `{show} {{{tvdb}}}/{show} - S{season}E{episode}...`) failing when files are inside Season subfolders. Two issues: (1) the rename logic treated `Season 1` as the parent and renamed it instead of the show folder — now uses `_SEASON_FOLDER_RE` to detect Season-style parents and goes up one level; (2) after renaming the show folder, `old_path` still referenced the old folder name, causing every file rename to fail with `ENOENT` — now rebuilds `old_path` through the renamed show folder + original Season subfolder before moving the file. The `_renamed_parents` tracking is keyed by show folder path so files from all seasons share the same rename record. After all files are moved, empty Season subdirectories left behind inside the renamed show folder are automatically removed.
263. **File Renamer skip Season subfolders in show detection** — Fixed the auto-load and file matching using Season subfolder names ("Season 1", "Season 2", etc.) as show search queries instead of the actual show folder. When files are organized as `Ghosts UK/Season 1/episode.mkv`, the parent folder is "Season 1", not "Ghosts UK". Added `_get_show_folder()` helper with `_SEASON_FOLDER_RE` regex that detects Season-style folders (`Season N`, `Series N`, `S01`, etc.) and returns the grandparent folder name instead. Used by both `_auto_load_shows()` and `_match_file_to_show()`.
265. **File Renamer Multiple Matches thumbnails on high-DPI (take 2)** — Fixed thumbnails still not displaying on high-resolution monitors despite previous DPI scaling fixes. The placeholder used `ttk.Label` which has known issues rendering images on high-DPI Tk — switched to `tk.Label` with explicit pixel dimensions (`width`/`height` set to `60*dpi` × `90*dpi`). The `_apply_thumb` function now sets `width=photo.width(), height=photo.height()` instead of `width=0` which could collapse the label. Also changed the silent `except: pass` to log thumbnail errors for debugging.
262. **File Renamer apostrophe matching fix** — Fixed show matching failing for titles with apostrophes (e.g. `Grey's Anatomy`, `Schitt's Creek`, `The Handmaid's Tale`). Filenames never contain apostrophes (dots/spaces used instead), so `"Greys Anatomy"` couldn't match the TVDB name `"Grey's Anatomy"` — no substring check worked because `"greys"` ≠ `"grey's"`. This caused the search retry to strip "Anatomy" and search just "Greys", producing wrong results. Fixed by stripping apostrophes (straight `'` and curly `\u2018`/`\u2019`) in `_normalize_for_match()`. Both sides now normalize to `"greys anatomy"` → exact match.
261. **File Renamer API keys hardcoded, settings removed** — Removed the API Keys settings dialog and menu entry from the Media Renamer. TVDB and TMDB keys are now hardcoded in `api_key_var` and `tmdb_key_var` StringVars (values unchanged). Removed: API Keys dialog (`_open_api_key_settings`), Settings → API Keys menu entry, `_save_tmdb_key` trace callback, API key save/load from preferences (`preferences.py` and `video_converter.py` monolith), empty-key warning in `_add_paths`, and empty-key guard in `_tvdb_login`. Simplified `_add_paths` to always call `_auto_load_shows()` when files are added (no key check needed).
260. **Reduce to 2 Lines filter: natural-flow line breaking** — Rewrote the `_reflow()` function inside `filter_reduce_lines()` with a 5-tier priority system for finding the most natural split point when reducing 3+ line cues to 2 lines. Tiers (each picks the split closest to the midpoint for balanced lines): (1) sentence endings `.!?`, (2) clause boundaries `,:;`, (3) before conjunctions (`and, but, or, because, when, while, if, before, after, which, who`, etc.), (4) before prepositions (`in, on, at, to, for, with, from, by, of, about, into, through`, etc.), (5) nearest midpoint space as last resort. "that" excluded from conjunctions because it's too ambiguous — often a demonstrative adjective (`"in that place"`) rather than a clause-introducing conjunction. Dialog lines (`- speaker`) handled separately as before. Short text (≤42 chars) collapses to one line.
259. **File Renamer Add Files/Folder dialog error after rename** — Fixed the Add Files and Add Folder dialogs throwing an error when opened after a folder rename operation. The file dialog (zenity/GTK) internally remembers the last-opened directory, so after renaming that folder, the dialog would try to navigate to the now-nonexistent path. Added `_last_browse_dir` tracker with a `_get_browse_dir()` helper that validates the path exists before passing it as `initialdir`. If the last directory was renamed/deleted, falls back to its parent directory; if that's also gone, falls back to the home directory. Both `_browse_files()` and `_browse_folder()` now explicitly pass `initialdir` and update the tracker after each use.
258. **File Renamer query→show mapping for folder-based matching** — Fixed files from different folders (e.g. "Ghosts UK" and "Ghosts") being matched to the same show even after the user explicitly picked different shows from the Multiple Matches dialog. Root cause: the folder name "Ghosts UK" and the TVDB show name "Ghosts (2019)" have no substring relationship, so neither the already-loaded filter nor `_match_file_to_show()` could connect them. Added a `_query_to_show` dict that records every search query → loaded show name association when the user picks from the dialog (or when a show auto-loads). Three uses: (1) `_match_file_to_show()` checks the file's parent folder against the map FIRST — if the folder was previously used as a search query, the mapped show is returned directly without scoring; (2) the already-loaded filter in `_auto_load_shows()` checks the map to avoid re-searching a query that was already resolved (prevents the second drop re-triggering the dialog for "Ghosts UK"); (3) `_fallback_from_filename()` also records its mappings. The map is cleared on "Clear", "Clear All Shows", and individual show removal.
257. **File Renamer folder disambiguation not overriding clear filename match** — Fixed `_match_file_to_show()` folder disambiguation overriding an unambiguous filename match. When two shows with similar names were loaded (e.g. "Ghosts" and "Ghosts (US)"), a file named `Ghosts (US) S01E01.mkv` in folder `Ghosts/` would score 1.0 against "Ghosts (US)" and 0.6 against "Ghosts" by filename — a clear winner. But folder disambiguation ran unconditionally, found that folder name "ghosts" exactly matched show "Ghosts" (the UK version), and returned the wrong show. Fixed by adding a score-gap check before folder disambiguation: if the top candidate's filename score leads the runner-up by ≥0.3, the filename match is trusted and folder disambiguation is skipped. Folder disambiguation now only runs when filename scores are close (ambiguous), which is the correct use case (e.g. filename "Ghosts S01E01" matches both "Ghosts" and "Ghosts (US)" at similar scores — then the folder breaks the tie).
256. **File Renamer provider search retry without qualifier** — Fixed folder-derived search queries with qualifiers (e.g. "Ghosts UK") failing to find the correct show when the provider calls it by a shorter name (e.g. just "Ghosts"). When a search produces ≤1 close match and the query has multiple words, `_load_show_by_name()` now retries by stripping the last word (e.g. "Ghosts UK" → "Ghosts"). The retry results are merged with the original close matches (deduplicated by ID), and if the merged set has more matches, the Multiple Matches dialog appears so the user can pick the correct version. This works alongside the existing And↔& retry. Logged as `Retrying search as "..."`.
255. **File Renamer folder-aware show matching** — Fixed two TV shows with the same name (e.g. "Ghosts" US vs UK) being matched as the same show when filenames don't contain enough distinguishing info. Both `_auto_load_shows()` and `_match_file_to_show()` now read the parent folder name alongside the filename. In `_auto_load_shows()`: when multiple files produce the same cleaned filename name but come from different parent folders (e.g. `Ghosts (US)/` vs `Ghosts (2019)/`), the folder names are used as separate search queries instead of collapsing to one. When the folder name is related to the filename name (either direction — folder contains filename or vice versa), the folder name is always preferred since it's the user's chosen label: a broader folder like `"Ghosts"` produces a wider TMDB search than a filename like `"Ghosts (US)"`, and a more specific folder like `"Ghosts (2019)"` carries extra context the filename may lack. Unrelated folders (e.g. `"Downloads"`) are ignored and the filename is used. In `_match_file_to_show()`: when the filename matches multiple loaded shows equally (e.g. both "Ghosts" and "Ghosts" from different providers), the parent folder name is checked against each candidate to disambiguate — exact folder match wins, then substring scoring. Falls back to highest filename score if no folder disambiguation is possible.
254. **File Renamer "No Match Found" button in Multiple Matches dialog** — Added a "No Match Found" button (right-aligned) to the bottom of the Multiple Matches disambiguation dialog. When the user sees the list of TVDB/TMDB search results but none of them are correct, clicking this button skips the provider results and falls back to `_fallback_from_filename(query)`, which derives the show/movie name directly from the filename (with year extraction and TV vs Movie detection). The dialog returns a `'__filename_fallback__'` sentinel that `_load_show_by_name()` intercepts before the normal `None`/cancel check. This gives users an explicit escape hatch instead of having to cancel and manually handle unmatched files.
253. **File Renamer filename-derived fallback when no provider match** — When the TVDB/TMDB search returns no results for a file, the renamer now falls back to deriving the show/movie name from the filename instead of leaving the file unmatched. New `_fallback_from_filename()` function scans `_file_items` to determine whether the query is a movie (no episode info detected) or TV (has season/episode markers), extracts the year from the original filename via regex (handles both `Movie 2026` and `Movie (2026)` formats), and creates a synthetic entry in `_all_shows`. For movies, the entry includes `_is_movie=True` and the extracted year so the movie template can apply. For TV shows, the entry provides the show name so `{show}`, `{season}`, `{episode}` template variables still resolve (episode titles will be empty since there's no provider data). Log messages indicate the fallback: `No provider match — using "Show Name" from filename`. The user can still search manually or switch providers to get full metadata.
252. **File Renamer movie search not triggering Multiple Matches** — Fixed movies not appearing in the Multiple Matches disambiguation dialog when searching a short name like "Mars". The TMDB search fetches TV results first, then movies, appending them all to a single list. The close-match filter in `_load_show_by_name()` scanned only the first 15 results (`results[:15]`), so when 15+ TV shows contained the query as a substring (e.g. "Veronica Mars", "Mars Attacks", etc.), the actual movie "Mars" was pushed past the cutoff and never checked. Fixed by sorting results so exact name matches rank first (before substring matches) via a `_match_rank()` key function, ensuring the movie "Mars" appears in the top 15 alongside TV shows with similar names. Also fixed the `seen_ids` dedup key to include `_media_type` alongside `id`, since a TV show and movie can share the same numeric TMDB ID.
251. **File Renamer Multiple Matches thumbnails on high-DPI** — Fixed thumbnails not showing in the Multiple Matches disambiguation dialog on high-resolution displays. Two issues: (1) the window used hardcoded `"700x500"` geometry instead of `scaled_geometry()`, so on high-DPI the window was physically small while all widgets were DPI-scaled, leaving no room for thumbnails; (2) thumbnail images were created at a fixed 60×90 pixels, but Tk's DPI scaling renders each pixel at a fraction of a logical pixel (e.g. 30×45 at 200%), making them too small to display in the `ttk.Label`. Fixed by applying `scaled_geometry`/`scaled_minsize` to the dialog, scaling thumbnail pixel dimensions by the DPI factor (`int(60 * dpi)` × `int(90 * dpi)`), and scaling the overview `wraplength`. Thumbnails now appear at the correct visual size on any display.
250. **File Renamer template dialog scrollbar on high-DPI** — Fixed the vertical scrollbar not appearing in the Filename Template dialog on high-resolution displays. The scrollbar was packed unconditionally at startup but the `scrollregion` was never explicitly set after content was built — on high-DPI displays where `scaled_geometry` made the window large enough to contain all content, the scrollbar had zero range and was non-functional. Fixed by: (1) deferring scrollbar packing to an `_update_scrollbar()` helper that shows/hides based on whether content height exceeds viewport height, (2) calling `_update_scrollbar()` from both `<Configure>` handlers and after initial content build, (3) adding `update_idletasks()` before the check so geometry is fully resolved. The scrollbar now auto-shows when content overflows (e.g. many custom templates) and auto-hides when it fits.

### 2026-05-07 (Bug Fix)
243. **File Renamer undo cleanup for folder templates** — Fixed undo leaving empty Season folders and renamed parent directories behind. The undo now runs in 4 phases: (1) undo file renames (moves files back to parent root), (2) clean up empty subdirectories (Season folders), (3) undo parent folder renames (restores original folder name), (4) clean up any remaining empty directories. File renames and folder renames are separated in the undo batch so they can be processed in the correct order. File paths in `_file_items` are updated to reflect parent folder rename-backs.
242. **File Renamer folder templates rename parent directory** — Fixed folder templates (containing `/`) creating a new subdirectory instead of renaming the existing parent folder. Previously, a file at `/downloads/Show.Name.Messy/S01E01.mkv` with template `{show}/Season {season}/...` would create `/downloads/Show.Name.Messy/Show Name/Season 01/...` (nested). Now the parent folder is **renamed** first (`Show.Name.Messy` → `Show Name`), then Season subfolders and files are created within it: `/downloads/Show Name/Season 01/...`. Uses a `_renamed_parents` tracking dict so each parent folder is only renamed once even with multiple files. Folder renames are included in undo history. Flat templates (no `/`) are unchanged.
249. **Template Wizard Check All / Uncheck All** — Added Check All and Uncheck All buttons to the media tags step of the Template Wizard for quick toggling of all tag checkboxes.
248. **Template Wizard "Movie Year/filename" folder option** — Added a second folder structure option for movies: "Movie Year/filename" (without parentheses) alongside the existing "Movie (Year)/filename". Common organization style for movie libraries.
247. **Auto-probed media tag template variables** — Added 5 new template variables that are auto-detected from each video file via ffprobe: `{resolution}` (2160p/1080p/720p/480p from width/height), `{vcodec}` (x265/x264/AV1/etc. from codec_name), `{acodec}` (AAC/AC3/DTS/TrueHD/Atmos/etc. with DTS-HD and Atmos profile detection), `{hdr}` (HDR10/HDR/SDR from color_transfer/color_primaries), `{source}` (BluRay/WEB-DL/HDTV/etc. from filename keywords). Files are probed when added to the renamer; subtitle files inherit tags from their matched video. The Template Wizard extras step now uses checkboxes instead of dropdowns — check a tag to include it, values are filled automatically per-file at rename time. Variables reference in the Template dialog updated. Added `_probe_media_tags()` function with `_VCODEC_MAP`, `_ACODEC_MAP`, and `_SOURCE_PATTERNS` lookup tables.
246. **Template Wizard extras step** — Added a new "Add media tags?" step to the Template Wizard (step 5 of 6) with dropdown selectors for Resolution (2160p/1080p/720p/480p), Video codec (x265/x264/HEVC/AV1/etc.), Audio codec (AAC/AC3/DTS/TrueHD/Atmos/etc.), Source (BluRay/WEB-DL/HDTV/REMUX/etc.), HDR (HDR/HDR10/HDR10+/DV/SDR), and a free-text Custom field. All fields are optional — leave blank to skip. Tags are appended as literal text to the filename before the provider ID. Live preview updates as tags are selected.
245. **File Renamer Template Wizard** — New guided wizard (Settings → Template Wizard) that walks users through building a filename template step by step. 5 steps: (1) Type — TV Shows or Movies, (2) Naming Style — compact/dashes/classic variants, (3) Folder Structure — flat, Show/Season XX, or Show/SXX, (4) Provider ID — none/TVDB/TMDB with placement choice: in the filename (`...Title {tmdb-1396}.mkv`) or in the folder name (`Show {tmdb-1396}/...`); current provider highlighted; folder placement disabled when flat structure selected, (5) Confirm — Apply or Save as Custom. Live preview shows the template string and an example filename at every step, updating as choices change. Back/Next navigation between steps. Apply sets the template immediately; Save as Custom adds it to the appropriate custom list (TV or Movie) in the template dialog.
244. **File Renamer custom templates redesign** — Replaced the confusing shared Saved Templates listbox (which mixed TV and Movie templates, only loaded into TV via "Use" button, and had ambiguous "Save Current") with separate custom template sections inside each preset column. Each column (TV Presets, Movie Presets) now has a "Custom:" section at the bottom with: clickable buttons to use each saved template, a "✕" delete button per template, and a "+ Save Current" button that saves the active template for that type. Custom templates stored as separate `_custom_tv_templates` and `_custom_movie_templates` lists in preferences. Old shared `_custom_rename_templates` auto-migrated to TV list on first load.
241. **File Renamer template dialog scrollbar** — Added a vertical scrollbar to the Filename Template settings dialog. The content area (template entries, saved templates, variables reference, and preset buttons) is now inside a scrollable canvas so all content is accessible even when the window is smaller than its content. Mousewheel scrolling bound to all child widgets. Close button stays fixed at the bottom outside the scroll area.
240. **File Renamer TV/Movie type column and template display** — Added a "Type" column to the File Renamer treeview showing `TV`, `Movie`, or `—` for each file so the user can see at a glance which template will be applied. Added a template display row above the file list showing both active templates: `TV: {show} S{season}E{episode} {title}` (in blue) and `Movie: {show} ({year})` (in red), updated live as templates change. Prevents accidentally renaming TV episodes with the movie template. Updated "Copy New Name" context menu to read from the correct column index (shifted from index 1 to 2).
239. **Batch Filter "no subtitles found" warning** — Added an info dialog when the user adds a folder (via "Add Folder..." button or drag-and-drop) that contains no subtitle files. Previously the operation silently did nothing, leaving the user unsure if something went wrong.
238. **Media Processor threaded file scanning with progress** — All file-adding paths (Add Files, Add Folder, drag-and-drop) now probe files in a background thread via `_add_files_threaded()`. Shows live progress in the existing progress bar (`Scanning 3/25 — ETA 12s`), logs elapsed time on completion, refreshes the tree every 20 files for visual feedback, and prevents overlapping scans with a `_scanning` flag. File path collection (directory walking) is done on the main thread first (fast), then the slow ffprobe calls (`get_audio_info`, `get_subtitle_streams`, `_detect_ext_subs`) run in the background thread. UI stays responsive during large folder drops.
237. **Remove ALL CAPS HI preview minimize/maximize** — Removed `transient()` and `grab_set()` from the Remove ALL CAPS HI preview window so it can be minimized and maximized on Linux window managers. Same fix previously applied to other tool windows (Media Processor, File Renamer, etc.). Applied to both standalone and internal subtitle editor variants.
236. **Preview window UI polish** — Renamed "Apply Checked" to "Apply" in the Fix Music Notes preview and "Remove Checked" to "Apply" in the Remove ALL CAPS HI preview for consistency. Consolidated the Fix Music Notes preview buttons from two rows into a single row matching the ALL CAPS HI layout: `Select All | Select None | count ... Apply | Cancel` with Apply and Cancel right-aligned. Applied to both standalone and internal subtitle editor variants.
235. **Separated Remove HI from Remove ALL CAPS HI** — The "Remove HI [brackets] (parens) Speaker:" filter no longer automatically removes standalone ALL CAPS HI lines (UK style). Previously, `filter_remove_hi()` included a call to `_is_caps_hi_line()` that removed lines like `SHEENA LAUGHS` and `DOOR SLAMS` alongside bracket/paren/speaker removal. This is now the user's choice — they must explicitly run "Remove ALL CAPS HI (UK style)" as a separate filter. The colon-based ALL CAPS labels (`HIGH-PITCHED:`, `MUFFLED:`) are still removed by Remove HI since those are inline annotations, not standalone descriptions. Batch Filter already had them as independent checkboxes.
233. **Remove ALL CAPS HI preview window centering** — Fixed the preview window opening off-screen instead of on the same monitor as the subtitle editor. Applied the withdraw→position→deiconify pattern: window is created withdrawn, all widgets are built, then `center_window_on_parent()` positions it over the editor before showing. `grab_set()` moved after `deiconify()` to avoid grabbing an invisible window. Applied to both standalone and internal editor variants.
232. **Remove ALL CAPS HI preview window layout fix** — Fixed the preview window for "Remove ALL CAPS HI (UK style)" filter not showing all buttons. The button row (Select All, Select None, count label, Cancel, Remove Checked) was packed after the scrollable list, so on smaller windows or high-DPI displays the buttons were clipped off the bottom. Fixed by packing the button frame `side='bottom'` first (before the canvas), added `minsize` (650x300 scaled), widened the window from 700→750, and increased text `wraplength` from 580→620. Applied to both standalone and internal subtitle editor variants.
231. **Video-only mode hides audio controls** — Fixed the main window not hiding audio codec/bitrate controls when "Video Only" transcode mode is selected. Previously, audio controls were always visible but grayed out in video-only mode, while audio-only mode properly hid the video controls. Now video-only mode hides the audio frame entirely (matching the symmetry of audio-only hiding video controls), and the checkboxes row shifts up to fill the gap. "Video + Audio" mode shows both control groups as before.
230. **Removed "Skip existing files" checkbox** — Removed the "Skip existing files" checkbox from the main window settings panel and the per-file override dialog. The option was redundant given the "Delete originals after conversion" option and unlikely to be used. The underlying `skip_existing` variable is kept at `True` internally so the behavior is preserved (output files are never silently overwritten), but there's no UI to toggle it. Preferences save/load still handles the key for backward compatibility.
229. **Reset to Defaults — correct default values** — Updated `reset_preferences()` to match the intended Default Settings defaults: "Notify When Done" now resets to **unchecked** (was `True`), "Default Video Folder" and "Default Save To Folder" reset to **blank** (were not being reset at all — kept their saved values), and the output folder label updates to "Same as source file". Added `_default_video_folder` attribute to decouple the saved preference from the runtime `working_dir` (which defaults to `Path.home()`) — reset sets it to `''` so the JSON saves blank, while normal saves use the user's chosen folder. Applied to both monolith and `modules/preferences.py`.
228. **Reset to Defaults not persisting** — Fixed "Reset to Defaults" not updating the Default Settings dialog. `reset_preferences()` reset the in-memory `self.*` variables but never called `save_preferences()`, so the prefs JSON file kept the old values. Since the Default Settings dialog reads from the JSON file on open, it still showed the pre-reset values. Added `save_preferences()` call at the end of reset. Also added `default_player` reset to `'auto'` which was missing. Fixed in both `video_converter.py` monolith and `modules/preferences.py`.
229. **Media Rescaler HDR → SDR tone mapping** — Added a "Convert HDR → SDR" checkbox to the Media Rescaler (`video_scaler.py`). When enabled, HDR content (HDR10, HLG, Dolby Vision) is tone-mapped to standard BT.709 SDR during rescaling. Changes: (1) Enhanced `_probe_video_info()` to detect HDR format from `color_transfer`, `color_primaries`, and Dolby Vision side data — returns a 5th value (`hdr_format`: `'HDR10'`, `'HLG'`, `'DoVi'`, or `''`). (2) Source column in the file list now shows HDR format tag (e.g. `"3840x2160 DoVi"`). (3) New `opt_hdr_to_sdr` BooleanVar with checkbox on settings row 2. (4) `_build_cmd()` inserts a `zscale→tonemap(hable)→zscale→format` filter chain when tone mapping, plus explicit BT.709 color metadata tags. GPU hwaccel decode is disabled during tone mapping since the filters require CPU frames (GPU encoder still works — it accepts CPU frames and uploads internally). Preference saved/restored.
228. **Media Renamer Multiple Matches folder path** — Added the source directory path at the top of the "Multiple Matches" disambiguation dialog in `tv_renamer.py`. When multiple shows match a query (e.g. "Ghosts"), the dialog now displays the full folder path above the query header so the user can tell which directory's files are being matched. Especially helpful when two shows have similar names in different folders (e.g. `Ghosts (US)/` vs `Ghosts (2019)/`). The folder is found by matching the query back to `_file_items` via normalized folder and filename comparison, and skips Season subfolders to show the show-level directory.
227. **Media Processor per-file subtitle cleanup** — Fixed subtitle files being deleted for all completed files when the global "Mux external subtitles" option was enabled. Previously, the cleanup pass ran based solely on the global `opt_mux_subs` flag and the file's `✅ Done` status — so files that completed without muxing (e.g. per-file override disabled muxing, or no subs were detected) would still have their subtitle files deleted. Now `_process_one()` sets a `_subs_muxed` flag on each file only when subtitles were actually included in the ffmpeg command, and the cleanup pass only removes subtitle files for files with that flag set.
226. **Media Processor all subtitle formats** — Extended fuzzy subtitle detection to support all subtitle formats (`.srt`, `.ass`, `.ssa`, `.vtt`, `.sub`, `.idx`, `.sup`) by using the canonical `SUBTITLE_EXTENSIONS` constant from `constants.py` instead of a hardcoded subset that was missing `.idx` and `.sup`.
225. **Media Processor fuzzy subtitle detection** — Rewrote `_detect_ext_subs()` from rigid exact-pattern matching to fuzzy stem-based matching. Previously looked for a fixed set of patterns like `base.eng.forced.srt` which failed on non-standard filenames (doubled tags, extra tokens, unusual separators). New approach: scans all subtitle files (`.srt`, `.ass`, `.ssa`, `.vtt`, `.sub`) in the same directory, matches any file whose name starts with the video's stem (case-insensitive), then parses the remaining suffix tokens for language codes (2-letter, 3-letter, and ISO 639-2/B alternates), forced tags, and SDH/HI/CC tags. Defaults: language=`eng`, type=`main`, sdh=`False`. Language preference filtering still applies (single language or all-languages mode). Handles any separator style (dots, spaces, underscores, hyphens) and any token order. Added `_normalize_lang()` helper and pre-built `_ALL_LANG_CODES` set from all known code variants.
224. **File Renamer duplicate subtitle tags** — Fixed `_detect_sub_tags()` producing doubled tags like `.eng.forced.forced` when renaming files that already had subtitle tags in the filename. When a file like `Show.S01E01.eng.forced.srt` was renamed, the tags `.eng.forced` were appended again, producing `Show S01E01 Title.eng.forced.eng.forced.srt`. On a second rename the reversed scan would collect `forced` twice into `found_tags`. The Media Processor's subtitle detection expects exact patterns like `base.eng.forced.srt` and couldn't match the doubled-tag filenames. Fixed by deduplicating `found_tags` with a `seen` set before building the tag string.
223. **File Renamer subtitle files polluting TVDB/TMDB searches** — Fixed subtitle files (`.srt`, `.ass`, etc.) being included in the auto-load show name detection, causing their language/forced/SDH tags to be sent to the API as part of the show name (e.g. searching for `"The Drama (2026) eng"` instead of `"The Drama"`). `_auto_load_shows()` now skips files with subtitle extensions — only video files are used to detect show names. Subtitle files are still renamed correctly via `_match_file_to_show()` which matches them to their video counterparts. Also fixed `_clean_show_name()` not stripping parenthesized years (e.g. `Movie (2026)` → `Movie`) — the trailing year regex now handles both bare years (`2026`) and parenthesized years (`(2026)`).
222. **File Renamer undo restores files to list** — Fixed undo not restoring files to the file list after a rename. Previously, `_do_rename()` removed all renamed items from `_file_items` (line 1559) but `_do_undo()` only renamed the files on disk — it searched `_file_items` for the renamed paths but they had already been purged. This meant after undo the file list was empty and the user had to re-add files to try a different template. Now the rename history saves a copy of each renamed item (with all parsed metadata: season, episode, matched_show, etc.) and undo restores them to the list with their original paths, preserving show matches so a different template can be applied immediately without re-querying TVDB/TMDB.
221. **Batch Filter drag-and-drop fix** — Fixed drag-and-drop not working in the Batch Filter window. The old drop handler used a basic Tcl list parser that didn't handle `file://` URIs (which is what Linux file managers send). Rewrote `on_batch_drop()` to parse `file://` URIs with percent-decoding (matching the pattern used in Media Processor and other tools), with Tcl list format as fallback. Also added recursive directory expansion when folders are dropped (scans for `.srt`, `.ass`, `.ssa`, `.vtt`, `.sub` with hidden directory filtering). Registered DnD on both the window and the file listbox widget for broader drop target coverage.
220. **Estimated output size fix for audio-only mode** — Fixed the Est. Output Size calculation showing only the audio size when "Audio Only" transcode mode is selected. In audio-only mode the video stream is copied through (`-c:v copy`), so the output file contains both the original video and the re-encoded audio. Added `_probe_video_bitrate()` helper that queries ffprobe for the source video stream's bitrate (falls back to estimating from file size minus ~256kbps for audio). The video bitrate is now included in the estimate for audio-only mode and for the "Copy (no re-encode)" codec. Fixed in both `modules/gpu.py` and `video_converter.py` monolith.
219. **Media Processor completion notification** — Added an audible sound and a summary dialog when processing finishes. Plays the freedesktop `complete.oga` sound via `ffplay` (same as the main converter) and shows a messagebox with the success/failure count. Sound and dialog are suppressed if the user clicked Stop. Works for both single-file and batch processing.
218. **Media Processor recursive folder scan** — Fixed "Add Folder" and drag-and-drop folder scanning only finding video files one level deep. Changed `Path.glob()` to `Path.rglob()` in both `_add_folder()` and `_on_drop()` so subfolders are scanned recursively. Hidden directories (names starting with `.`) are filtered out via `relative_to()` path part checks.

### 2026-05-06 (Bug Fix)
217. **Media Renamer Refresh button** — Added a "🔄 Refresh" button to the TV Show Renamer toolbar (between Undo and Clear). Clears cached show data and matched show assignments, then re-queries the active provider for all detected shows. Useful when switching providers or when API results may have changed.
216. **Media Renamer thumbnail dropout on maximize** — Fixed thumbnails disappearing in the Multiple Matches dialog when the window is maximized at high DPI / 200% scaling. Added a debounced canvas scrollregion recalculation after resize (100ms delay to avoid thrashing). Also store the `PhotoImage` reference directly on the label widget (`child._photo = photo`) as a secondary GC guard alongside the `_thumb_refs` list.
215. **Media Renamer dialog centering fix** — Fixed the Templates dialog (and all other TV Renamer dialogs) opening off-screen at 200% scaling. Root cause: dialogs were centered on the main converter window (`app.root`) via `_center_on_main()` instead of the TV Renamer window (`win`). Added a local `_center_on_parent(dlg, win)` helper and replaced all 4 `_center_on_main` calls in `tv_renamer.py`.
214. **Media Renamer TMDB/TVDB ID fix** — Fixed `{tmdb}` and `{tvdb}` template variables rendering as empty `{}` when the other provider was active. Previously, only the active provider's variable was populated (e.g. `{tmdb}` was empty when using TVDB). Now both `{tvdb}` and `{tmdb}` resolve to the active provider's prefixed ID (e.g. `tmdb-271578` or `tvdb-12345`) so either template variable works regardless of which provider is selected.
213. **Media Renamer template Save buttons** — Added a "Save" button at the end of each template entry line (TV template and Movie template) in the Templates dialog (`tv_renamer.py`). Each button saves the corresponding template to the Saved Templates list. Clearer than the previous "Save Current" button which required the user to know which entry had focus. Also renamed "Saved:" label to "Saved\nTemplates:" for clarity.
212. **Media Renamer thumbnail double-click to select** — Re-bind `<Button-1>` and `<Double-1>` click events on thumbnail labels after the async image loads in the Multiple Matches dialog (`tv_renamer.py`). Double-clicking a thumbnail now selects the row and confirms the choice (same as clicking Load).
211. **Media Renamer Multiple Matches minimize/maximize** — Removed `transient()` and `grab_set()` from the "Multiple Matches" disambiguation dialog in `tv_renamer.py` so it can be minimized and maximized on Linux window managers.
210. **Media Processor Process All/Stop button fix at 200% scaling** — Fixed the "Process All" and "Stop" buttons disappearing when the Media Processor window is maximized at high DPI (200% scaling). Root cause: the progress bar row used a grid layout where column 1 (progress bar) had `weight=1` but column 2 (buttons) had no weight or minimum size, allowing the progress bar to consume all horizontal space and clip the buttons. Fixed by switching from grid to pack layout — buttons are packed `side='right'` first (reserving their space), then the label and progress bar fill the remainder with `fill='x', expand=True`.
209. **Media Processor multi-language subtitle support** — Rewrote the Media Processor's external subtitle detection to support all languages, not just English. Added "Include all subtitle languages" checkbox in Settings → Subtitles; when enabled, the Lang entry is grayed out and all known language codes are scanned (eng, spa, fra, deu, etc. plus 2-letter variants and ISO 639-2/B alternate codes like `ger`→`deu`, `fre`→`fra`, `dut`→`nld`, `chi`→`zho`). Changed ext_subs data model from tuples `(type, path)` to dicts `{'path', 'lang', 'type', 'sdh'}`. Detection patterns: `*.lang.srt`, `*.lang.forced.srt`, `*.lang.sdh.srt`, `*.lang.hi.srt`, `*.lang.cc.srt`. English subtitles sort first. Ext Subs column now shows `eng, eng.forced, deu.sdh` instead of just `main, forced`. Subtitle manager shows `[English — main] filename.srt` with language and type. Toggle Type button cycles main → forced → SDH. Added files auto-detect language from filename (including alternate codes). ffmpeg command uses per-subtitle language codes, correct disposition flags (English main → default, forced → forced, SDH → hearing_impaired), and language-aware track titles. Preference saved/restored as `all_subs`.
208. **Fix Music Notes filter with preview** — Added a new "Fix Music Notes ♪ (OCR)" filter to the subtitle editor Tools menu (both standalone and internal variants). Fixes common Tesseract OCR misreads of music note symbols (♪): `2 > $ & £ © » # * ? Sf D> P If f` at start/end of lines, `$f`/`£f` ligatures, `-)` → `-♪`, garbled markers after brackets, and short garbage-only cues. Opens a preview window showing before/after text for each matched cue with checkboxes, Select All/None buttons, and Apply Checked/Cancel. Supports undo. Added `fix_music_note_text()` helper and `filter_fix_music_notes()` cue-level wrapper in `subtitle_filters.py`. Also registered in `batch_filter.py` for batch processing.
207. **Remove HI speaker label fix** — Fixed the "Remove HI [brackets] (parens) Speaker:" filter not removing speaker labels ending with a digit (e.g. `MAN 2:`, `WOMAN 3:`, `MAN 3:`) or lowercase single-word labels (e.g. `rebel:`, `keegan:`, `lior:`). Rewrote the speaker label regex as an alternation: single-word labels match any case (`rebel:`, `Narrator:`), multi-word labels require an uppercase first character (`MAN 2:`, `Detective Smith:`) to avoid false positives on lowercase sentences ending with a colon (`things to get:`). Applied to both the main speaker pattern and the orphaned-colon cleanup pass in `subtitle_filters.py`.

### 2026-05-05 (Bug Fixes)
206. **Remove ALL CAPS HI preview window** — The "Remove ALL CAPS HI (UK style)" filter in both subtitle editor variants now opens a preview window listing all matched cues with checkboxes instead of immediately deleting them. Users can uncheck cues they want to keep, then click "Remove Checked" to apply. Includes Select All / Select None buttons, scrollable list with cue number/timestamp/text, and respects undo.
205. **Subtitle Editor Shift+Arrow multi-select** — Added Shift+Up/Down keyboard bindings to both subtitle editor variants (standalone and internal) for extending cue selection in the treeview.
204. **Subtitle Editor save confirmation** — Added a green "✓ Saved" flash indicator in the status bar of the standalone subtitle editor. Appears for 3 seconds after Save (Ctrl+S) or Save As, showing entry count. Non-intrusive alternative to a modal dialog. Both standalone and internal editor variants updated (internal editor already closes on save so the flash applies only to Save to Video which already had a messagebox).
203. **Shift+Arrow multi-select** — Added Shift+Up/Down keyboard bindings to all file list Treeviews: main converter, Media Processor, Media Rescaler, and TV Renamer. Main converter and Media Processor also updated to `selectmode='extended'` (were default `browse`). Holding Shift while pressing Up/Down adds adjacent items to the selection.
202. **Media Rescaler column sorting** — Added clickable column sorting to the Media Rescaler file list Treeview. Sorts by: filename (alpha), source resolution (pixel count), target resolution (pixel count), file size (bytes), status (alpha). Same sort arrow UX as main converter and Media Processor.
201. **Media Processor column sorting** — Added clickable column sorting to the Media Processor file list Treeview. Clicking a column header sorts by that column (ascending); clicking again toggles descending. Sort arrows (▲/▼) shown in the active header. Sorts by: filename (alpha), audio codec (alpha), internal sub count (numeric), external sub count (numeric), file size (bytes), status (alpha). Matches the sorting pattern used in the main converter.

### 2026-05-04 (v2.5.0 — Media Details Editor, Media Processor Settings Menu, Rescale GPU Fix)
200. **Media Rescale full GPU scaling pipeline** — Fixed NVENC GPU scaling being no faster than CPU. The root cause was missing `-hwaccel_output_format cuda` in the NVENC backend config, causing frames to be downloaded from GPU to CPU for scaling (`scale` filter) then re-uploaded for encoding. Added `-hwaccel_output_format cuda` to `GPU_BACKENDS['nvenc']['hwaccel']` and updated the filter chain to use `scale_cuda` (GPU-resident scaling) with `-sar 1:1` as an encoder option (since `setsar` filter doesn't support hardware frames). The entire decode→scale→encode pipeline now stays in VRAM. ~15x realtime on NVIDIA GPU vs ~3x previously.
199. **Media Rescale stop button fix** — Stop button did nothing once ffmpeg started processing a file. The `stop_flag` was only checked between files, not during the per-character ffmpeg output read loop. Added `current_proc` state to hold the running subprocess reference; `_stop()` now calls `proc.kill()` (SIGKILL) to immediately terminate ffmpeg. Also added stop flag check inside the read loop and cleanup of incomplete output files on stop.
198. **Media Rescale drag-and-drop fix** — Fixed drag-and-drop not working for files or directories. Registered DnD on both the tree widget and the window (Linux tkinterdnd2 doesn't always propagate drop events from Toplevel to children). Also fixed path parsing: `file://` URIs are now extracted via regex (`file://\S+`) instead of naive `raw.split()` which broke paths with spaces; brace-wrapped paths (`{/path/with spaces}`) parsed with regex instead of assuming single-item.
197. **Track naming template fallback to title keywords** — The `{flags}` template variable in track naming now checks disposition flags first, then falls back to parsing the existing track title for keywords (SDH, Forced, Commentary, HI, CC). Files with unflagged but titled tracks (e.g., "English (SDH)") now resolve correctly.
196. **Media Processor drag-and-drop fix** — Fixed multi-file drag-and-drop not adding files. Linux file managers send plain space-separated paths (no `file://` URIs); the parser now uses Tcl list regex for both braced and unbraced path formats.
195. **Media Processor double-click opens Media Details** — Changed double-click on a file in the Media Processor from opening the Override Settings dialog to opening the Media Details editor. Override is still accessible via right-click.
194. **Window centering fix** — Media Processor and Media Details windows now use withdraw/position/deiconify pattern to prevent visible flash in the upper-left corner before centering. Dimensions parsed from geometry string instead of `winfo_width()`/`winfo_height()` (which return 1 on withdrawn windows).
193. **Media Details unsaved changes warning** — Close button and window X button now prompt "Save before closing?" (Yes/No/Cancel) when editable fields have been modified. "Yes" saves then auto-closes after completion. `WM_DELETE_WINDOW` intercepted immediately via forward-reference wrapper.
192. **Media Details save progress bar** — Added real-time progress bar that appears during save. Parses ffmpeg's `time=` output character-by-character for `\r`-terminated progress lines. Shows "Saving..." with percentage, then "Saved!" on completion. Hidden when not saving.
191. **Media Details chapter editor** — Replaced read-only Chapters tab with two-mode editor. View mode: treeview showing existing chapters with "Edit Chapters..." button. Edit mode: toolbar (Add, Remove, Clear All, auto-generate every N minutes), treeview with double-click inline title editing. Chapters saved via FFMETADATA1 temp file with `-map_chapters`. Clearing all chapters uses `-map_chapters -1`. Temp file cleaned up after remux.
190. **Removed Metadata tab** — Removed redundant Metadata tab from Media Details. Container title is editable in General tab; all other metadata visible in Full Report tab.
189. **Media Details → editable tag editor** — Rewrote `modules/media_info.py` (798 → 1,768 lines) from read-only viewer to full tag editor. General tab: editable container title. Video/Audio/Subtitles tabs: structured grid layout with read-only stream info (codec, resolution, bitrate, HDR, etc.) above a separator and editable Title, Language (combobox with 81 language codes), and Disposition flags (checkboxes) below. Save via ffmpeg remux with atomic temp file replacement. Disposition safety: changing any flag on one stream emits explicit flags for ALL streams of that type. Scrollable frames for files with many streams.
188. **Media Details in Media Processor right-click** — Added "Media Details..." to the Media Processor's right-click context menu with `show_enhanced_media_info()` import and `importlib` fallback. Passes `parent=win` for proper centering.
187. **Language code lookup table** — Added `LANG_CODE_TO_NAME` dictionary to `constants.py` with 81 language codes (ISO 639-2 three-letter + ISO 639-1 two-letter) mapped to full language names.
186. **Media Processor track naming templates** — Added configurable templates for naming video, audio, and subtitle tracks. Variables: `{lang}` (full language name from code), `{codec}` (friendly codec name), `{channels}` (2.0/5.1/7.1), `{bitrate}` (kbps), `{flags}` (SDH/Forced/Commentary from disposition or title). `_resolve_track_name()` function resolves templates per-stream using probed data. Works with or without "Set track metadata" enabled. Preferences: `name_tracks`, `name_video`, `name_audio`, `name_sub`.
185. **Media Processor Settings menu** — Added menu bar with Settings → Preferences dialog. Moved 8 options from the Operations panel into organized LabelFrame sections: Cleanup (Strip chapters/tags/subs), Subtitles (Mux external subs, language, rescan), Chapters (auto-generate every N min with mutual exclusion), Output (in-place/folder with browse, container), Track Names (templates with variable reference), Processing (parallel, jobs). Options use the same `tk.Var` objects — changes take effect immediately. Dialog blocked during processing.
184. **Media Processor Operations panel slimmed** — Operations panel reduced from 5 rows to 3: Row 1 (Convert audio + codec + bitrate), Row 2 (Set track metadata + V/A/S), Row 3 (Edition + Plex). Default window height reduced from 880→720.
183. **Version bumped to 2.5.0.**

### 2026-05-05 (v2.5.0 — Multi-monitor support, scrollable settings, UI fixes)
201. **Fix duplicate APP_VERSION** — `video_converter.py` monolith had its own `APP_VERSION = "2.4.1"` on line 52, separate from `modules/constants.py`. Updated to `2.5.0` so the title bar shows the correct version.
200. **Video Scaler GPU scaling fix** — GPU path now keeps frames in GPU memory (`-hwaccel_output_format cuda`); SAR set via `-sar 1:1` encoder option instead of `setsar` filter (which fails on hardware frames). CPU path unchanged.
199. **Video Scaler stop button fix** — Added `current_proc` reference to track the running ffmpeg subprocess. Stop button now sends `SIGKILL` immediately. Partial output files cleaned up on stop.
198. **Video Scaler DnD improvements** — Drag-and-drop registered on both tree widget and window for broader coverage. Path parser handles `file://` URIs with `\r\n` separators and brace-wrapped paths with spaces.
197. **Threaded file scanning in Video Scaler** — All file-adding paths (Add Files, Add Folder, drag-and-drop) now probe files in a background thread using `_add_files_threaded()`. Shows live progress in the existing progress bar (`Scanning 3/25 — ETA 12s`), logs elapsed time on completion, and prevents overlapping scans. UI stays responsive during large drops.
196. **Scrollable settings panel** — Main window settings are now inside a scrollable Canvas with auto-hiding scrollbar. On small screens the settings scroll within a capped area, leaving adequate room for the file list. Mouse wheel scrolling bound to all child widgets. Combined with PanedWindow for user-adjustable split.
195. **PanedWindow layout** — Replaced fixed grid layout for settings + file list with a `ttk.PanedWindow` (vertical). User can drag the divider between settings and file list. Toggle settings (Ctrl+L) uses `paned.forget()`/`paned.insert()`. File list pane has weight=1 for expansion.
194. **Per-monitor window sizing** — Main window startup uses `xrandr` to detect the actual monitor the mouse is on and sizes the window to fit (e.g. 1200×688 on a 1366×768 display, full 1200×800 on 1080p). Fixes Tk's `winfo_screenheight()` returning the tallest monitor in multi-monitor setups.
193. **Tool window flicker fix** — Subtitle Editor, Batch Filter, Media Renamer, and Video Scaler now use the withdraw→position→deiconify pattern (matching Media Processor) instead of `_center_on_main()`. Eliminates the brief flash in the upper-right corner before centering.
192. **Missing `LANG_CODE_TO_NAME` constant** — Added `LANG_CODE_TO_NAME` dict (derived from `SUBTITLE_LANGUAGES`) to `constants.py`. Was referenced by `media_processor.py` and `media_info.py` but never defined, causing Media Processor to fail to launch.
191. **Renamer clears already-named files** — Files whose name already matches the target are now marked as `_renamed` and counted in the total, so they clear from the list along with all other files. Removes confusion when dragging large batches where some files are already correctly named.
190. **Multi-separator subtitle tag detection** — `_detect_sub_tags()` in `tv_renamer.py` now splits on dots, spaces, underscores, and hyphens (`re.split(r'[\.\s_\-]+', stem)`) instead of dots only. Fixes forced/SDH/language detection for filenames like `Movie_eng_forced.srt` or `Show - eng forced.srt`.

### 2026-05-02 (v2.4.0 — Renamed to Docflix Media Suite)
182. **Removed emoji icons from Tools menu** — Stripped leading emoji icons (✏, 📦, 🔧, 📺, 📐) from all Tools menu entries except the Play Source/Output File entries which keep their ▶ icons.
181. **Renamed Video Scaler to "Docflix Media Rescale"** — Updated window title, standalone launcher title, Tools menu label, all message dialogs, log message, module docstring, error dialog, and installer standalone command listing.
180. **Renamed File Renamer to "Docflix Media Renamer"** — Updated window title, standalone launcher title, Tools menu label, About dialog, error dialog, log message, module docstring, and installer standalone command listing.
179. **Renamed Media Processor to "Docflix Media Processor"** — Updated window title, standalone launcher title, Tools menu label, keyboard shortcuts panel, error dialog, log message, and installer standalone command listing.
178. **Renamed Subtitle Editor to "Docflix Subtitle Editor"** — Updated all window titles (standalone, file open, video stream, save as), standalone launcher title, Tools menu label, error dialogs, and installer standalone command listing.
177. **Removed redundant Media Details dialog** — Removed the basic `show_media_info()` method and its Tools menu entry. The Enhanced Media Details (tabbed dialog with HDR info, chapters, metadata, etc.) now takes over the `Ctrl+I` shortcut and the "Media Details..." menu label. The right-click context menu already pointed to Enhanced only.
176. **Project renamed from "Docflix Video Converter" to "Docflix Media Suite"** — Updated APP_NAME in constants.py, video_converter.py, and install.sh. Updated all module docstrings (21 modules), shell scripts (run_converter.sh, convert_videos.sh), installer (.desktop entry name and comment), user manual (HTML and Markdown), built-in manual viewer, and README.md. The project has grown well beyond a video converter into a full media tools suite with subtitle editing, OCR, Whisper sync, file renaming, media processing, and video scaling.

### 2026-05-02 (v2.3.4 — Internal Subtitle Editor Fix, Zenity Dialog Fixes)
172. **File Renamer template dialog minimize/maximize fix** — Removed `transient(win)` from the Filename Template dialog. Same fix as #164 — on Linux, `transient()` tells the window manager it's a dependent dialog, which strips the minimize and maximize buttons.
171. **File Renamer template dialog high-DPI scaling** — Applied `scaled_geometry` and `scaled_minsize` to the Filename Template dialog in the File Renamer (Settings → Filename Template). Previously used hardcoded `860x750` / `780x650` which appeared too compact on high-resolution displays.
175. **Media Details high-DPI scaling** — Applied `scaled_geometry` and `scaled_minsize` to the Media Details dialog in `video_converter.py`. Previously used hardcoded `620x520` with no minimum size, which appeared too compact on high-resolution displays.
174. **Enhanced Media Details high-DPI scaling** — Applied `scaled_geometry` and `scaled_minsize` to the Enhanced Media Details dialog. Previously used hardcoded `780x620` / `600x400` which appeared too compact on high-resolution displays.
173. **Batch Filter Add Folder button** — Added an "Add Folder..." button next to "Add Files..." in the Batch Filter. Recursively scans the selected folder for subtitle files (`.srt`, `.ass`, `.ssa`, `.vtt`, `.sub`), skipping hidden directories and files. Uses `ask_directory` (zenity).
170. **Batch Filter file dialog switched to zenity** — Replaced `filedialog.askopenfilenames` in the Batch Filter (Add Files button) with `ask_open_files` from `utils.py`. Removed unused `filedialog` import.
169. **Media Processor file dialogs switched to zenity** — Replaced both `filedialog.askopenfilenames` calls in the Media Processor (Add Files toolbar button, Add Subtitle in Manage Subtitles dialog) with `ask_open_files` from `utils.py`. Removed unused `filedialog` import.
168. **File Renamer file dialogs switched to zenity** — Replaced both `filedialog` calls in the File Renamer (Add Files, Add Folder) with `ask_open_files` and `ask_directory` from `utils.py`. Removed unused `filedialog` import.
167. **Video Scaler file dialogs switched to zenity** — Replaced all 3 `filedialog` calls in the Video Scaler (Add Files, Add Folder, Browse output folder) with `ask_open_files` and `ask_directory` from `utils.py`. These use zenity (GTK native) with Tk fallback, matching the rest of the app. Removed unused `filedialog` import.
166. **Internal subtitle editor empty window fix** — Fixed `UnboundLocalError` that caused the internal subtitle editor (Internal Subtitles → Edit button) to open as an empty window with menus but no content. The View menu's "Show/Hide Timeline" command referenced `_toggle_timeline_int` directly (`command=_toggle_timeline_int`), but the function was defined ~900 lines later in the code. Python evaluates `command=` arguments immediately, so this raised `UnboundLocalError` before any content widgets (search bar, treeview, status bar) were created. Fixed by wrapping in a lambda (`command=lambda: _toggle_timeline_int()`) to defer evaluation until the menu item is clicked — matching the pattern already used for the keyboard bindings on the same function. The standalone editor was unaffected because its equivalent function (`_toggle_timeline_menu`) was defined before the menu reference.

### 2026-05-01 (v2.3.3 — Media Details Fix, Subtitle Editor Zenity Dialogs)
165. **Subtitle editor file dialogs switched to zenity** — Replaced all 9 `filedialog.askopenfilename`/`asksaveasfilename` calls in the subtitle editor with `ask_open_file`/`ask_save_file` from `utils.py`. These use zenity (GTK native) with Tk fallback, matching the rest of the app. Fixes the subtitle editor showing a different (old-style Tk) file picker than all other tools. Applies to File Open, Save As, Export SRT, Smart Sync video browse, Quick Sync video browse, and Waveform video browse.
164. **Media Details minimize/maximize fix** — Removed `transient(self.root)` from the `show_media_info()` dialog. On Linux, `transient()` tells the window manager it's a dependent dialog, which strips the minimize and maximize buttons.

### 2026-05-01 (v2.3.2 — High-DPI Window Scaling)
163. **High-DPI window scaling** — Added `get_dpi_scale()`, `scaled_geometry()`, and `scaled_minsize()` helper functions to `utils.py`. All tool windows (Media Processor, Subtitle Editor, File Renamer, Video Scaler, Batch Filter) and standalone launchers now scale their window size and minimum size by the current DPI factor. On standard 96 DPI displays nothing changes; on high-DPI displays (150%, 200%, etc.) windows open at the correct size so all controls are visible without manual resizing. Fixes tools launching too small on high-resolution monitors and widgets disappearing until mouse-over.

### 2026-05-01 (v2.3.1 — Movie Template, Stop Fix, Filename Fix, Batch ETA)
162. **Batch ETA: file status sync** — `update_file_status()` only updated the treeview display but never set `self.files[i]['status']`. The batch ETA scan couldn't find the converting file (always saw `'Pending'`) so it returned None. Now updates both the tree and the data model.
161. **Stop → CPU fallback prevention** — When Stop killed a GPU encode, `convert_file()` returned False which triggered the GPU→CPU fallback (starting a new CPU encode instead of stopping). Now checks `converter.is_stopped` before attempting fallback — a user-initiated stop is not a GPU failure.
160. **Stop button deadlock fix** — The SIGTERM + `wait(3s)` approach deadlocked because the conversion thread held the stdout pipe open. `wait()` blocks until the process exits, but the process can't exit while the pipe is held. Now uses `kill()` (SIGKILL) directly — immediate, no deadlock, broken pipe exits the read loop naturally.
159. **Batch ETA dynamic update** — Replaced index-based remaining file calculation (`self.current_file_index`) with status-based scan. The old approach broke when files were added or removed mid-conversion because list indices shifted. Now scans `self.files` by status (`'Pending'`, `'⏳ Converting'`) so the ETA dynamically reflects the actual queue.
158. **Output filename GPU preset fix** — Fixed the output filename suffix showing the CPU preset (e.g. `ultrafast`) instead of the GPU preset (e.g. `p4`) when GPU encoding was selected. Now correctly reads `gpu_preset` from file settings when the encoder is not CPU, producing filenames like `-2.0M-NVENC_H265_p4` instead of `-2.0M-H265_ultrafast`.
157. **Stop button fix** — Fixed the Stop button graying out but not actually stopping the ffmpeg encode. `terminate()` sends SIGTERM which ffmpeg often ignores. Now uses `kill()` (SIGKILL) directly — cannot be ignored, kills immediately. Applied to both `stop()` method and the inline `is_stopped` check in `_run_process()`, in both `video_converter.py` and `modules/converter.py`.
156. **File Renamer movie template** — Added a dedicated movie naming template alongside the existing TV template. Movies now use their own configurable pattern (default: `{show} ({year})`) with variables `{show}`, `{year}`, `{tvdb}`, `{tmdb}`. Settings → Filename Template dialog shows both TV and Movie entries with side-by-side preset columns (TV Presets left, Movie Presets right), each with Flat and Folder sections. Template persisted to preferences (`movie_rename_template` key). Template dialog clipping fixed with bottom-up Close button packing.
155. **Version bumped to 2.3.1.**

### 2026-05-01 (v2.2.9 — Dock Icon, "Open With" Support)
154. **"Open with" support for subtitle files** — `docflix-subs` now accepts a file path as a command-line argument and auto-opens it on launch. Works with both subtitle files (`.srt`, `.ass`, `.ssa`, `.vtt`) and video files (extracts internal subtitles). New `docflix-subs.desktop` file with subtitle MIME types (`application/x-subrip`, `text/x-ssa`, `text/x-ass`, `text/vtt`) so the editor appears in the file manager's right-click "Open with" menu. `NoDisplay=true` keeps it out of the app launcher. Uninstaller updated to clean up the new `.desktop` file.
153. **Dock/taskbar icon and name** — Set `className='docflix'` on root `Tk()` window and `root.iconphoto()` with `logo_transparent.png` so the dock/taskbar shows the Docflix logo and app name instead of the default "Tk" gear icon. Added `StartupWMClass=docflix` to the `.desktop` file so the desktop environment associates the running window with the app launcher. Applied to both main app (`video_converter.py`) and standalone tool launchers (`modules/standalone.py`).
152. **Version bumped to 2.2.9.**

### 2026-05-01 (v2.2.7 — Batch Filter Layout Overhaul, High-DPI Fixes)
149. **High-DPI widget rendering fix** — Added `win.update_idletasks()` after layout construction in Media Processor, Video Scaler, and Batch Filter. Fixes invisible/blank controls on high-DPI displays that only appeared after mouse-over triggered a redraw.
150. **Batch Filter layout overhaul** — Moved Search & Replace pairs management into a Settings menu dialog. Added "Apply search & replace" checkbox with pair count and Edit button inline with the filters. Output options merged into a single row inside the Filters section. File buttons (Add Files, Remove Selected, Clear All) moved above the file listbox. Bottom sections (filters, progress, apply/close) packed from the bottom first so they are never clipped regardless of window size or DPI scaling. Removed ~520-line inline copy from monolith, now imports from `modules/batch_filter.py`.
151. **Monolith batch filter migration** — Replaced the inline `open_batch_filter()` in `video_converter.py` (520 lines) with a module import from `modules/batch_filter.py`. Monolith reduced from 9,343 to 8,840 lines.

### 2026-05-01 (v2.2.1 — Dead Code Removal, File Renamer Matching Fixes)
145. **Dead code removal** — Removed ~8,560 lines of deprecated UNUSED code from `video_converter.py`: `_open_standalone_subtitle_editor_UNUSED` (2,918 lines), `_open_media_processor_UNUSED` (1,299 lines), `_open_tv_renamer_UNUSED` (1,851 lines), `_show_subtitle_editor_UNUSED` (2,492 lines). Monolith reduced from 17,903 to 9,343 lines. All four tools now run exclusively from their `modules/` imports.
146. **File Renamer hyphen preservation** — Fixed `_clean_show_name()` replacing all hyphens with spaces, which broke show names containing hyphens (e.g. `9-1-1` became `9 1 1`, failing API lookup). Dots and underscores are still replaced with spaces, but hyphens between non-space characters are now preserved. Fixes `9-1-1`, `X-Men`, and similar show names.
147. **File Renamer And/& matching** — Added `_normalize_for_match()` helper that normalizes `&` → `and`, strips colons, and lowercases text for comparison. Applied to `_match_file_to_show()` (all comparison paths: exact, substring, word-overlap) and `_load_show_by_name()` (search result filtering). Filenames like `Law.And.Order` now correctly match TVDB/TMDB results stored as `Law & Order`. Also handles colon differences (`Law & Order: SVU` vs `Law And Order SVU`).
148. **File Renamer search retry with And↔& swap** — When the initial API search returns no results, `_load_show_by_name()` now automatically retries with `And` replaced by `&` (or vice versa). Ensures shows like `Law & Order` are found even when the filename uses `And`.

### 2026-04-29 (v2.1.1 — TV Renamer Folder Templates, HiDPI Treeview, Installer Fix)
### 2026-04-29 (v2.2.0 — Waveform Timeline, Module Imports, File Renamer Enhancements)
140. **Waveform Timeline** — New `modules/waveform_timeline.py` (1,498 lines). Audio waveform display with subtitle cue overlay for visual timing adjustment. Extracts audio from video via ffmpeg (8kHz mono), renders waveform on Tkinter Canvas with numpy downsampling. Features: drag cue blocks to move timing, drag edges to resize start/end, embedded mpv video player with live subtitle preview, step navigation (1s, 100ms, frame-by-frame), playback cursor tracking, right-click "Add Cue Here" (500ms cue at cursor), horizontal scroll/scrollbar, auto-scroll during playback. Integrated into both standalone and internal subtitle editors via resizable PanedWindow layout.
141. **Module imports** — `video_converter.py` now imports subtitle editor, TV renamer, and media processor from their modules instead of using inline copies. Tools menu launches use the module versions with all new features. *(Inline copies removed in v2.2.1.)*
142. **File Renamer enhancements** — Renamed "TV Show Renamer" to "File Renamer". Added `{tvdb}` and `{tmdb}` template variables for provider IDs (e.g. `tvdb-475560`). Added saveable custom templates with Use/Save/Delete buttons, persisted to preferences. Template variables text box now selectable/copyable with right-click context menu. Renamed files cleared from list after successful rename.
143. **Unsaved changes warning** — Both subtitle editor variants now prompt "Save before closing?" when the user closes the editor with unsaved modifications.
144. **Subtitle live preview** — During waveform playback, current edited subtitles are written to a temp SRT and loaded into mpv via IPC (`sub-add`/`sub-reload`). Edits (text, timing, filters) update the displayed subtitles in real-time.

136. **TV Renamer folder templates** — Filename templates now support `/` path separators to automatically create folder hierarchies during rename. New `_sanitize_path()` function sanitizes each path component individually while preserving the directory structure. `_do_rename()` creates parent directories with `os.makedirs()` before moving files. Undo cleans up empty directories (deepest-first) that were created during rename. Template dialog updated with folder preset buttons (e.g. `{show}/Season {season}/{show} S{season}E{episode} {title}`) and usage documentation. Both monolith (`video_converter.py`) and standalone module (`modules/tv_renamer.py`) updated.
137. **TV Renamer template dialog maximize/minimize** — Removed `transient()` and `grab_set()` from the Filename Template dialog so the window manager provides full minimize/maximize/close decorations on Linux.
138. **HiDPI Treeview row height** — File list Treeview row height now scales dynamically based on the current font's `linespace` metric plus padding. Fixes clipped/invisible row text on high-DPI displays with fractional scaling.
139. **Installer PEP 668 fix** — `install.sh` now handles Python's externally-managed-environment restriction (PEP 668) on newer Debian/Ubuntu. Tries `pip3 install --user` first, falls back to `--break-system-packages` if blocked, and shows a manual install hint on failure.

135. **User manual updated** — Added Video Scaler tool documentation (resolution presets, GPU scaling, smart probing, progress/ETA, preferences), `docflix-scale` standalone command, `Ctrl+Shift+R` keyboard shortcut, Plex edition filename details, and Media Processor progress bar to all three manual formats (built-in viewer, HTML, markdown). Updated all module line counts in project summary.

### 2026-04-29 (v2.1.0 — Video Scaler Fixes, Zenity Dialogs, Plex Edition, MP Progress)
126. **Video Scaler progress bar and ETA** — Real-time per-file progress parsing from ffmpeg's `time=` and `speed=` output. Shows percentage, estimated time remaining, and elapsed time on completion. Character-by-character reading to handle ffmpeg's `\r`-terminated progress lines. Throttled to ~3 updates/sec.
127. **Video Scaler aspect ratio fixes** — Fixed three separate scaling issues: (a) missing `-vf scale` filter caused by `_build_scale_filter` returning None; (b) CUDA hwaccel delivering padded frames for letterboxed/cropped content — fixed by probing actual decoded dimensions via 1-frame extraction at 30% of duration, reading PNG header directly for pixel-accurate sizes; (c) NVENC encoder inheriting non-square SAR (8:9) from source — fixed by appending `setsar=1:1` to the filter chain. Scale filter now uses explicit pre-calculated dimensions instead of `-2` auto-calculation.
128. **Video Scaler preferences** — All settings (resolution, encoder, preset, CRF, audio, container, output mode, output folder) saved to `preferences.json` under `video_scaler` key on window close, restored on next open. Works in both main app and standalone launcher.
129. **Zenity file dialogs** — Switched all 16 file dialog calls (open files, open file, save file, folder browser) from Tk dialogs to zenity (GTK native) with proper font scaling and system theme. Added `ask_open_file()`, `ask_open_files()`, `ask_save_file()` helpers to `modules/utils.py`. Wrapper methods `_ask_open_files()`, `_ask_open_file()`, `_ask_save_file()` on VideoConverterApp with importlib fallback. Tk dialogs remain as automatic fallbacks.
130. **Zenity speedup** — Added `_run_zenity()` helper that sets `GTK_USE_PORTAL=0` (bypass xdg-desktop-portal D-Bus), `GDK_BACKEND=x11` (skip Wayland detection), `NO_AT_BRIDGE=1` (skip AT-SPI accessibility) to reduce GTK startup from 4-5 seconds to under 1 second.
131. **Change Folder switched to zenity** — Replaced the 130-line custom Tk Treeview folder browser with `_ask_directory()` zenity call. Both Change Folder and Set Output now use the same GTK native dialog.
132. **Tk font size fix** — All Tk named fonts (TkDefaultFont, TkTextFont, TkMenuFont, etc.) bumped to 11pt if under 10pt during `configure_dpi_scaling()`. Affects all remaining Tk dialogs including file pickers and message boxes.
133. **Plex edition filename** — When Plex edition is enabled, output filename uses clean format without encoding suffix: `Superman {edition-Director's Cut}.mkv`. In-place mode renames the file to include the edition tag and removes the original. Applied to main converter and Media Processor (both monolith and module).
134. **Media Processor per-file progress** — Added real-time progress bar to Media Processor. Character-by-character ffmpeg output parsing for `\r`-terminated progress lines. Shows per-file percentage in status column and progress label, combined batch + file progress in the progress bar. Uses outer-scope batch tracking variables accessible from both `_process_one` and `_process_files`.

### 2026-04-29 (v2.0.9 — Video Scaler Tool)
125. **Video Scaler** — New standalone batch video scaling tool (`modules/video_scaler.py`). Resolution presets: Original, 2160p (4K), 1440p (2K), 1080p, 720p, 480p, Custom WxH. GPU-accelerated scaling using `scale_cuda` (NVENC), `scale_qsv` (QSV), `scale_vaapi` (VAAPI), or CPU `scale` filter. Aspect ratio preservation via `-2` auto-dimension. Upscale detection with warning indicators. Encoder selection with preset and CRF controls. Audio passthrough (copy) or re-encode. Container selection (.mkv, .mp4). Output to folder or in-place replacement. File list with source resolution, target resolution, size, and status columns. Drag-and-drop support. Threaded processing with stop button. Color-coded log panel. Accessed via Tools > Video Scaler (`Ctrl+Shift+R`) in the main app or `docflix-scale` standalone command. Installer updated with `docflix-scale` launcher.

### 2026-04-29 (v2.0.8 — Media Processor Layout Overhaul, Manual Viewer Fixes)
120. **Media Processor layout overhaul** — Reorganized the operations panel from a flat 6-row layout into 4 logical groups (Audio, Metadata, Subtitles, Output) using plain Frames with padding for clean visual separation without borders. Edition and chapter controls merged onto one row with a vertical separator. Output and Container/Parallel split into separate rows to prevent controls from overflowing off-screen on high-DPI displays.
121. **Manual viewer emoji crash fix** — Fixed rendering crash caused by invalid UTF-16 surrogate pair (`\ud83d\udcce`) in the Queue Indicators table. The surrogate caused a TclError that killed the rendering loop, preventing sections 4–14 from being rendered. Replaced with text labels and added try/except safety net around each line render.
122. **Manual viewer navigation fix** — Fixed sidebar navigation for all 14 sections. Stored line numbers during rendering, used direct Tk `yview` command with widget temporarily enabled for scrolling. Multiple fallback methods (raw Tk yview, yview_moveto fraction, text.see).
123. **Manual viewer text selection** — Replaced PanedWindow (which intercepted mouse drag events) with grid-based layout. Added `tag_raise('sel')` to ensure selection highlight renders above styled content tags. Configured `inactiveselectbackground` and `exportselection=False` so selection stays visible. Keyboard input blocked except Ctrl+C and navigation keys. Right-click context menu with Copy.
124. **Media Details rename** — Renamed "Media Info" to "Media Details" and "Enhanced Media Info" to "Enhanced Media Details" across all UI strings (menu labels, context menu, dialog titles, error messages, keyboard shortcuts help, README, module docstring).

### 2026-04-29 (v2.0.7 — Built-in Manual Viewer)
119. **Built-in manual viewer** — Replaced browser-based manual launch with a native Tkinter viewer (`modules/manual_viewer.py`). Dark-themed window (960x700) with sidebar section list (Listbox navigation) and formatted text content area (Text widget with styled tags). All 14 manual sections rendered with proper formatting: headers (h2/h3/h4), paragraphs, bullet lists, code blocks, tables (fixed-width columns), and colored callout boxes (tip/green, warning/amber, note/blue). Section marks for instant navigation via sidebar click. No external dependencies or browser required. Help > User Manual opens the viewer directly within the app.

### 2026-04-29 (v2.0.6 — User Manual)
118. **User Manual** — Comprehensive user documentation in two formats: `docs/user_manual.html` (dark-themed HTML with sidebar navigation, styled tables, tip/warning/note callouts, print-friendly CSS, 14 sections) and `docs/USER_MANUAL.md` (PDF-ready markdown). Covers: Getting Started (requirements, installation, launching), Quick Start, Main Window (menu bar, encoder selection, settings panel, file queue, conversion), Video Settings (codecs, quality modes, presets, two-pass, HW decode, containers), Audio Settings, Metadata & Tagging (strip options, track metadata, edition tagging, add chapters), Subtitles (internal, external, subtitle editor, Smart Sync, bitmap OCR, spell checker, batch filter), Tools (Media Processor, TV Show Renamer, Enhanced Media Details, Test Encode), Per-File Overrides, CLI Usage, Keyboard Shortcuts, Preferences, Troubleshooting, and Encoding Reference. HTML manual launched from Help > User Manual in the menu bar via `webbrowser.open()`. Installer updated to copy `docs/` directory.

### 2026-04-29 (v2.0.5 — Add Chapters)
117. **Add chapters** — New feature to auto-generate evenly-spaced chapter markers at a configurable interval (1–60 minutes, default 5). New `modules/chapters.py` (233 lines) with utility functions: `generate_auto_chapters()` generates chapter dicts from file duration, `parse_chapter_file()` auto-detects and parses FFMETADATA1 and OGM chapter file formats, `chapters_to_ffmetadata()` writes chapter dicts to an FFMETADATA1 temp file for ffmpeg injection. Chapter metadata file is added as an extra ffmpeg `-i` input and mapped via `-map_chapters <index>` in `_add_metadata_args()`. Mutually exclusive with "Strip chapters" via trace callbacks — checking one unchecks the other. Chapters are generated per-file based on `duration_secs` in the conversion loop. Temp files cleaned up in `finally` blocks. Settings panel row: "Add chapters every [N] minutes" with a spinbox (disabled when unchecked). Media Processor: same chapter checkbox + interval spinner + chapter injection in `_build_cmd()` with cleanup. Both converters (monolith `_add_metadata_args` at line ~3660 and `modules/converter.py` at line ~535) updated with chapter injection and input index calculation. Persisted to preferences (save/load/reset). Applied to both `video_converter.py` and `modules/converter.py`.

### 2026-04-29 (v2.0.4 — Edition Tagging)
116. **Edition tagging** — New feature to tag video files with version/edition info (e.g., Theatrical, Director's Cut, Extended, IMAX). Writes to the container `title` metadata field via ffmpeg `-metadata title=...`. Preset dropdown with 12 common editions (Theatrical, Director's Cut, Extended, Extended Director's Cut, Unrated, Special Edition, IMAX, Criterion, Remastered, Anniversary Edition, Ultimate Edition) plus a "Custom..." option that reveals a free-text entry field. Optional "Add to filename (Plex)" checkbox inserts a `{edition-Director's Cut}` tag into the output filename for Plex media server edition detection. Edition tag is placed after `-metadata title=` in the ffmpeg command so it overrides the title-clearing behavior of "Set track metadata" when both are active, but works independently — doesn't require "Set track metadata" to be on. Added to: main settings panel (row 9), per-file override dialog (with custom entry and Plex checkbox), and Media Processor (operations panel + output filename + per-file overrides). Persisted to preferences (save/load/reset). Edition presets defined as `EDITION_PRESETS` constant in both the monolith and `modules/constants.py`. Enhanced Media Info now shows the container title as "Title/Edition" in the General section. Applied to both `video_converter.py` (monolith) and `modules/converter.py` (package).

### 2026-04-29 (v2.0.3 — Enhanced Media Info)
115. **Enhanced Media Info** — New comprehensive file analysis tool (`modules/media_info.py`, 792 lines). Runs two targeted ffprobe commands: main probe (`-show_format -show_streams -show_chapters`) plus a first-frame HDR probe (`-read_intervals "%+#1" -show_entries frame=side_data_list`). Displays results in a tabbed dialog with dark-themed Courier text: General (format, duration, size, bitrate, stream count), Video (codec/profile/level/tag, resolution/SAR/DAR, frame rate/VFR detection/frame count, scan type, pixel format/bit depth, color range/space/transfer/primaries, HDR format detection with Mastering Display Metadata and Content Light Level, Dolby Vision config, bitrate/max bitrate, reference frames, closed captions flag), Audio (codec/profile/tag, sample rate/channels/layout/sample format/bits per sample, bitrate, language/title, disposition flags), Subtitles (codec, event count, bitmap resolution, disposition), Chapters (full listing with timestamps and titles), Attachments (fonts, images, MIME types), and Metadata (all container tags in priority order). Includes Copy to Clipboard (current tab) and Copy Full Report buttons. Accessed via right-click context menu (ℹ️ Enhanced Media Info...), Tools menu (Ctrl+Shift+I), and keyboard shortcut.

### 2026-04-29 (v2.0.2 — Media Processor Preferences, High-DPI Scaling)
111. **Media Processor defaults changed** — All operation checkboxes (Convert Audio, Strip Chapters, Strip Tags, Strip Existing Subtitles, Set Track Metadata, Mux External Subtitles, Parallel Processing) now default to unchecked on fresh installs instead of checked. Users choose their own options; dropdown defaults (audio codec, bitrate, languages, container) are unchanged.
112. **Media Processor preferences save/load** — All Media Processor settings are now saved to the shared `preferences.json` file (under the `media_processor` key) when the window is closed (Close button or window X) and restored when the window is next opened. Works in both the main app and standalone `docflix-media` launcher. The prefs file is read/updated incrementally so other saved preferences are preserved.
113. **Media Processor UI state fix** — Added `_toggle_audio_controls()` call after widget creation so the audio codec and bitrate dropdowns are correctly disabled when Convert Audio starts unchecked. Removed the hardcoded `mp_ac_combo.set('ac3 (Dolby Digital)')` that was overriding the loaded preference value. Applied to both `media_processor.py` (package) and `video_converter.py` (monolith).
114. **High-DPI display scaling** — Added `configure_dpi_scaling()` function that detects the real display DPI and sets Tk's scaling factor so all widgets, fonts, and geometry are properly sized on high-resolution monitors with desktop scaling enabled. Detection uses three methods in priority order: `Xft.dpi` from X resources (via `xrdb -query` — set by most desktop environments including GNOME, KDE, XFCE), `GDK_SCALE` environment variable (GNOME/GTK fractional scaling), and `QT_SCALE_FACTOR` environment variable (KDE/Qt). The scaling factor is applied via `tk scaling` before any widgets are created. Added to `utils.py` (package, shared by all standalone tools via `create_standalone_root()`) and as `_configure_dpi_scaling()` in the monolith's `main()`. Wrapped in a try/except so scaling issues never prevent the app from starting. Fixes tiny UI on high-DPI displays where Tkinter previously ignored the system's scaling settings.

### 2026-04-28 (v2.0.1 — UI Polish, Date Episodes, Subtitle Fixes)
100. **Tool window minimize/maximize** — Removed `transient()` from the TV Show Renamer, Media Processor, and standalone Subtitle Editor windows (both package modules and monolith). `transient()` told the window manager these were dependent dialogs, which stripped the minimize and maximize buttons on most Linux desktop environments. Sub-dialogs within those tools (pickers, progress bars, settings) retain `transient` since they are true dialogs.
101. **Default Settings window improvements** — Made the Default Settings dialog resizable (`resizable(True, True)` with `minsize(640, 320)`) and removed `transient()` so it has minimize/maximize buttons.
102. **Hidden file filtering** — All file scan locations (folder browse, drag-and-drop, recursive discovery via `rglob`, `glob`, `os.walk`) now skip hidden files and directories (names starting with `.`). Applies to the main converter, TV Show Renamer, Media Processor, Subtitle Editor, and Batch Filter across both package modules and monolith.
103. **Hide dotfiles in Tk file dialogs** — Set `::tk::dialog::file::showHiddenVar` to `0` at startup so the Tk `askopenfilenames` dialog hides dotfiles by default. Added a "Show Hidden" toggle button (`showHiddenBtn`) so users can still reveal them. Applied to both the monolith root and `standalone.py`.
104. **SDH checkbox in External Subtitles dialog** — Added an SDH column (between Default and Forced) with per-subtitle checkbox. SDH is auto-detected from filename tokens (`.sdh.srt`, `.cc.srt`). The `hearing_impaired` disposition flag is now set on output subtitle tracks marked SDH, and "SDH" is included in the track title (e.g. "English - SDH"). The `sdh` field is now stored in the `sub_info` dict in both auto-detect paths (dialog add and drag-and-drop/folder scan). Updated in both `converter.py` (package) and `video_converter.py` (monolith).
105. **Date-based episode support in TV Show Renamer** — The episode parser now detects date-based filenames (`2026.04.22`, `2026-04-22`, `2026 04 22`) used by daily shows (e.g. talk shows, news programs). Date is stored as `air_date` in the file item. Episode data from TVDB/TMDB is indexed by air date in addition to season/episode number. Date-based files are matched to episodes by air date and renamed using the standard template with the real S##E## from the API. Falls back to `Show Name - YYYY-MM-DD` if no episode match is found. `_clean_show_name()` updated to truncate at date patterns.
106. **Colon replacement in TV Show Renamer** — Changed filename sanitization to replace `:` with a space instead of ` -`. Colons in episode titles (e.g. "Rise of Evil: Part One") now produce cleaner filenames without the extra dash.
107. **HI filter speaker label fix** — Fixed the Remove HI filter incorrectly matching timestamps as speaker labels. The regex `[A-Za-z\s\d]{0,29}:` could match text like `He was found at 6:` (treating `6:30` as a speaker label colon). Fixed by requiring the character immediately before `:` to be a letter (`[A-Za-z]`), which rules out timestamps (`6:30`, `12:00`). Applied to the main speaker pattern, the orphaned-colon cleanup pattern, and the standalone speaker label filter in both `subtitle_filters.py` and the monolith.
108. **Subtitle editor scroll-to-top on file load** — Added `tree.yview_moveto(0)` after `refresh_tree()` in `_load_cues_into_editor()` so the treeview scrolls to the top when a new file is loaded via drag-and-drop or File → Open. Previously, the scroll position from the previous file was retained.
109. **TMDB air date preservation** — TMDB episode data now includes the `aired` field (mapped from TMDB's `air_date`) so date-based episode lookup works with both providers.
110. **Package directory renamed** — Renamed `video_converter/` to `modules/` for clearer distinction between the package directory and the monolith file (`video_converter.py`). All internal imports use relative paths so no module code changes were needed. Updated `install.sh` and `__main__.py` references. Requires `./install.sh` to update installed tool commands.

### 2026-04-27 (v2.0.0 — Modular Package Architecture)
92. **Modular package structure** — Split the 17,220-line monolith (`video_converter.py`) into 16 independent modules under a `modules/` Python package (14,142 lines total). Each module has focused responsibilities: `constants.py` (config/codec maps), `utils.py` (format helpers, ffprobe wrappers, tooltips), `gpu.py` (GPU detection, CC detection), `converter.py` (VideoConverter engine), `preferences.py` (save/load/reset), `subtitle_filters.py` (all filter functions + SRT parsing), `subtitle_editor.py` (both editor variants), `smart_sync.py` (Whisper-based sync), `spell_checker.py` (unified incremental spell check), `subtitle_ocr.py` (Tesseract OCR pipeline), `tv_renamer.py` (TV Show Renamer), `media_processor.py` (Media Processor), `batch_filter.py` (Batch Filter). The monolith is preserved and still functional — both layouts coexist during the transition.
93. **Standalone tool launchers** — Three new terminal commands installed to `~/.local/bin/`: `docflix-subs` (Subtitle Editor), `docflix-rename` (TV Show Renamer), `docflix-media` (Media Processor). Each launches its tool independently without loading the full converter app. Uses `StandaloneContext` class for shared preferences and window management.
94. **StandaloneContext** — Lightweight application context (`standalone.py`) that provides the same interface tool modules expect from `VideoConverterApp`: preferences load/save (shared JSON file), window centering, and root window access. Tools work identically whether launched from the main app or standalone.
95. **Incremental spell checker** — Rewrote the spell check dialog to scan and fix as it goes instead of scanning the entire file first. Dialog opens immediately, finds errors one at a time, shows progress ("Checking cue 42 of 500"), and lets the user fix each error before continuing. Words added to dictionary are immediately recognized for remaining cues.
96. **Installer updated** — `install.sh` now copies the `modules/` package directory (16 modules), creates the three standalone tool commands (`docflix-subs`, `docflix-rename`, `docflix-media`), validates the package exists during source check, and cleans up all tool commands on uninstall.
97. **Provider auto-reload** — Switching TVDB/TMDB provider in the TV Show Renamer now automatically re-searches all loaded files with the new provider instead of requiring the user to clear and re-add files.
98. **Spell check dialog positioning** — Sub-dialogs (spell check, settings, etc.) now center correctly on the tool window in standalone mode using `withdraw`/`deiconify` to prevent visible positioning jumps.
99. **Version bumped to 2.0.0** — marks the transition from monolithic (v1.9.x) to modular package architecture.

### 2026-04-27 (v1.9.2 — TV Show Renamer Enhancements)
87. **Undo after rename** — Added undo support to the TV Show Renamer. Each Rename All operation saves a complete history of old → new path mappings. The ↩ Undo button (also Edit → Undo Rename, Ctrl+Z) reverts the last rename batch by renaming files back to their original names. Multiple undo levels supported (one per rename batch). Undo button is disabled when no history exists. History is cleared when the file list is cleared.
88. **Manual episode number editing** — New "Set Episode..." dialog accessible via right-click context menu or Edit menu. Allows the user to manually set or correct the season and episode numbers for selected files when the filename parser fails or detects incorrectly. Applies to all selected files simultaneously. Changes are reflected immediately in the preview.
89. **Multi-episode file support** — The episode parser now detects multi-episode filenames: `S01E01E02`, `S01E01E02E03` (consecutive), and `S01E01-E03` (range, fills gaps). Multi-episode files generate combined output names with episode range tags (`E01-E02`) and concatenated titles joined with `&` (e.g. `Show - S01E01-E02 - Title 1 & Title 2`). Single-episode files are unaffected.
90. **Enhanced right-click context menu** — The treeview right-click menu now includes per-file actions: Set Episode (opens manual edit dialog), Copy New Name (copies the generated filename to clipboard), Open Folder (opens the containing directory in the file manager via `xdg-open`), Remove Selected (with file count), Remove Show, and Clear All Files.
91. **Progress indication during API calls** — Auto-loading shows from TVDB/TMDB now runs in a background thread with a progress bar showing the current show being loaded (e.g. "Loading 2/5: Show Name"), a determinate progress bar, and a Cancel button. The UI remains responsive during network requests. Show picker dialogs (for disambiguation) are dispatched to the main thread to ensure proper Tk modal behavior.

### 2026-04-27 (v1.9.1 — GPU Test Mode & Detection Bugfix)
84. **GPU Test Mode** (`--gpu-test-mode`) — Added a `--gpu-test-mode` command-line flag that skips the GPU test encode verification (Tier 2) during backend detection. When enabled, `detect_gpu_backends()` accepts GPU backends based solely on ffmpeg encoder availability (Tier 1: `ffmpeg -encoders` lists the encoder) and GPU name identification (Tier 3: `lspci` or vendor-specific commands). Designed for testing GPU detection logic in virtualized environments where real GPU hardware is unavailable (e.g. VMs with spoofed PCI device IDs). A `GPU_TEST_MODE` global flag is set at startup and a console banner prints confirmation. Normal operation is completely unchanged when the flag is not passed.
85. **VAAPI GPU name detection bugfix** — Fixed a regex bug in `_detect_gpu_name()` where the VAAPI vendor pattern `AMD|ATI|Radeon` incorrectly matched the substring "ati" in "comp**ati**ble" on every `lspci` VGA line. On dual-GPU systems (e.g. Intel + AMD), this caused the VAAPI backend to display the Intel GPU name instead of the AMD GPU name. Fixed by adding word boundaries: `AMD|\bATI\b|Radeon`. Discovered during VM-based GPU spoofing edge case testing with simultaneous Intel and AMD VGA entries in `lspci` output.
86. **GPU name extraction fix for `(rev XX)` suffixes** — Fixed `_short_gpu_name()` failing to extract the bracketed model name (e.g. `[Arc A770]`, `[GeForce RTX 4090]`) from `lspci` output when the line ended with a revision suffix like `(rev a1)` or `(rev 08)`. The bracket-extraction regex `\[...\]\s*$` expected brackets at the end of the string, but the trailing `(rev XX)` prevented the match. Fixed by moving the `(rev ...)` stripping step **before** bracket extraction. Affected all NVIDIA and some Intel discrete GPU names from `lspci`. Names now extract cleanly: `"DG2 [Arc A770]"` → `"Arc A770"`, `"AD102 [GeForce RTX 4090] (rev a1)"` → `"GeForce RTX 4090"`.

### 2026-04-27 (v1.9.0 — TV Show Renamer Overhaul & Subtitle Improvements)
64. **Multi-show auto-loading** — TV Show Renamer completely reworked from a single-show search workflow to a fully automatic multi-show system. When files are added (drag-and-drop or folder browse), unique show names are auto-detected from filenames, searched on TVDB, and episodes loaded automatically. Each file is matched to its correct show via fuzzy filename matching (`_match_file_to_show()`) using substring, ratio, and word-overlap scoring. The old Search/Show/Season/API Key UI rows are removed — the tool now has a streamlined layout: Template → File List → Buttons → Log.
65. **Multi-show state** — Replaced single `_episodes` dict and `_series_name` with `_all_shows` dict keyed by show name. `_build_new_name()` now takes a `show_name` parameter. Files store their `matched_show` for correct episode lookup across multiple loaded shows.
66. **User disambiguation dialog** — When TVDB returns multiple shows with the same or similar names (e.g. "Ghosts" matching US, UK, DE, 1995 versions), a rich card-based picker dialog shows all candidates with poster thumbnails (loaded asynchronously), show synopsis, country, network, and year. Resizable window (700×500 default), scrollable with mousewheel, click to select, double-click to load.
67. **Disambiguation matching fix** — The original two-pass matching (exact name match, then close match) missed cases where only one exact match existed alongside multiple similar names (e.g. "Ghosts" exact-matched the 1995 show while "Ghosts (US)" and "Ghosts (2019)" were ignored). Merged into a single pass that collects both exact and close matches, presenting all of them in the picker dialog. Scans top 15 results (up from 8) with duplicate ID filtering.
68. **Right-click context menu** — Treeview now has a right-click menu with "Remove show" (unloads a show and all its matched files) and "Clear all files" options.
69. **Show state cleanup on clear** — Clear button and file removal now also clear loaded show data from `_all_shows`, so re-adding a file triggers a fresh TVDB search with the picker dialog instead of silently reusing a previously loaded (possibly wrong) show. Orphaned shows with no remaining matched files are automatically cleaned up.
70. **Rename completion popup** — "Rename All" now shows a messagebox summarizing how many files were renamed, skipped, and errored.
71. **Subtitle tag preservation** — Subtitle files renamed via the TV Show Renamer now preserve language, forced, SDH, CC, and HI tags from the original filename (e.g. `Show.S01E06.eng.forced.srt` → `Show - S01E06 - Title.eng.forced.srt`). Language code always included, defaulting to `eng` if not detected.
72. **Subtitle language detection from content** — New `_detect_language_from_content()` function reads the actual subtitle file text and detects the language using `langdetect`. Strips SRT timestamps, ASS headers, HTML tags, and music notes before detection. Tries UTF-8, Latin-1, and CP1252 encodings. Content detection takes priority over filename-based language; logs a notice if they disagree. Falls back to filename tag, then to `eng` default.
73. **Subtitle extraction forced/SDH tag fix** — Fixed subtitle extraction generating `movie.eng.Forced.forced.srt` when a track's title was "Forced" and the forced disposition flag was also set. Tag-only titles ("Forced", "SDH", "CC", "HI", "Default", "Commentary", "Signs", "Songs") are now filtered out of the filename slug. SDH suffix (`.sdh`) is now automatically added when the `hearing_impaired` disposition flag is set or the track title is "SDH"/"CC"/"HI".
74. **TVDB API key hardcoded** — API key no longer requires user entry; embedded directly in the tool.
75. **Wider default window** — Increased default size to 960×650 and column widths (350/400) for better readability with multi-show filenames.
76. **Importing subtitle progress dialog** — When opening a video file in the standalone subtitle editor and selecting a stream to edit, a modal progress dialog now appears during ffmpeg extraction. Shows an animated indeterminate progress bar with the message "Importing subtitle stream #N from filename...". Extraction runs in a background thread so the UI stays responsive. Dialog auto-closes on completion; error handling preserved.
77. **TMDB provider support** — Added The Movie Database (TMDB) as an alternative metadata provider alongside TVDB. Provider-agnostic routing via `_provider_search()`, `_provider_get_episodes()`, and `_provider_get_series_id()`. TMDB search results normalized to the same dict format as TVDB so the disambiguation dialog, fuzzy matching, and episode loading work identically with both providers. TMDB API key hardcoded; user-editable via Settings → API Keys. Poster thumbnails served from TMDB's image CDN (`image.tmdb.org/t/p/w92/`).
78. **Movie support** — Both TVDB and TMDB searches now include movies (previously only TV series). TVDB searches without `type=series` filter; TMDB searches both `/search/tv` and `/search/movie` endpoints. Movies are stored with `_is_movie` flag — no episode fetch needed. Movie filenames renamed to `Movie Name (Year).ext`. The `_clean_show_name()` function now strips trailing years from filenames (e.g. `Rise.Of.The.Conqueror.2026.1080p...` → `Rise Of The Conqueror`). `_refresh_preview()` updated to allow movies through without requiring season/episode numbers.
79. **Menu bar** — Replaced inline UI controls with a full menu bar. **File:** Add Files (Ctrl+O), Add Folder (Ctrl+Shift+O), Rename All (Ctrl+R), Clear All, Clear Log, Close (Ctrl+W). **Edit:** Select All (Ctrl+A), Remove Selected (Delete). **Settings:** Provider (TVDB/TMDB radio), Filename Template dialog (with variable reference and preset buttons), API Keys dialog (both TVDB and TMDB keys with clickable links to registration pages). **Help:** Template Variables, About. All keyboard shortcuts wired up. Template and provider rows removed from main layout; button bar simplified to Rename All + Clear only.
80. **API Keys dialog** — Combined TVDB + TMDB key management in Settings → API Keys. Both keys editable with clickable blue underlined links that open the browser to each provider's key registration page. Save/Cancel buttons.
81. **Filename Template dialog** — Settings → Filename Template opens a 520×420 resizable dialog with the template entry field, variable reference table, and 4 preset template buttons for quick selection.
82. **Window positioning fix** — Added `transient(self.root)` to the subtitle editor and TV show renamer windows, matching the media processor. Ensures consistent centering over the main window instead of snapping to bottom of screen.
83. **Version bumped to 1.9.0.**

### 2026-04-26 (v1.8.0 — WhisperX Integration)
44. **WhisperX "Precise" engine** — Added WhisperX as an alternative speech recognition engine in Smart Sync (Timing → Smart Sync in both editors). WhisperX adds a forced alignment step using `wav2vec2` phoneme models on top of Whisper transcription, producing word-level timestamps with ~50ms accuracy (vs ~400ms with faster-whisper). New "Engine" radio button row in the Smart Sync dialog: "Standard (faster-whisper)" and "Precise (WhisperX) — phoneme-level alignment". When Precise is selected, fine-tune offset defaults to 0ms (forced alignment eliminates the systematic timing lag). GPU-accelerated when CUDA is available (uses float16 on GPU, int8 on CPU). Alignment model (~300MB per language) auto-downloads on first use. Auto-install prompt for `whisperx` package (pulls in PyTorch ~2GB).
45. **Fine-tune offset added to standalone editor** — The standalone subtitle editor's Smart Sync dialog was missing the Fine-tune offset spinbox (existed only in the internal editor). Added the fine-tune row with the same ±2000ms range, 50ms increment, and 400ms default. Both editors now have identical Smart Sync dialogs.
46. **Apply Sync double-shift fix** — Fixed a bug in the internal subtitle editor's Smart Sync where Apply Sync shifted timestamps twice (once by `offset`, then again by `offset + fine-tune`), resulting in a total shift of `2×offset + fine-tune`. Now correctly applies a single shift of `offset + fine-tune`, matching the standalone editor's behavior.
48. **Threaded WhisperX install** — The `pip install whisperx` (~2GB download) now runs in a background thread with an indeterminate progress bar and live pip output streamed to the results log. The UI remains responsive during the download. After install completes, the Start button re-enables and the user can click it to proceed. Applied to both subtitle editor dialogs. Install command pins `transformers<4.45` to avoid the `is_offline_mode` import error in newer transformers versions.
49. **WhisperX fine-tune default** — Changed WhisperX fine-tune offset from 0ms to +200ms. WhisperX's forced alignment detects phoneme onsets (~50ms precision), but perceptible speech starts ~200ms after the physical waveform onset. The +200ms default compensates for this, preventing subtitles from appearing noticeably before the dialogue. User-adjustable via the Fine-tune spinbox.
50. **Smart Sync matching improvements** — Fixed low anchor count on Full Scan by scaling the search window dynamically: `max(100, segments // 3)` instead of fixed 100 (a 60-min file with 1000+ segments now searches up to ~333 ahead instead of 100). Relaxed length ratio filter from 0.3–3.0× to 0.2–5.0× to handle different sentence splitting between subtitles and Whisper. Added time-based re-sync: after 50 consecutive unmatched cues, estimates the correct search position by timestamp proportion and jumps forward. Logs usable segment count for diagnostics. Removed duplicate "no matches" check.
51. **Repeatable Apply Sync / Re-time All** — Both buttons now stay enabled after clicking, and always re-apply from a snapshot of the original cues taken when Start was clicked (`pre_sync_cues` deep copy). This allows the user to adjust the fine-tune offset and click Apply/Re-time again without re-running the scan. Previously, both buttons disabled after first click, and clicking Re-time a second time (after undo) would retime already-retimed cues with stale anchor data — producing no visible change when fine-tune was adjusted. Undo still works for reverting to the pre-sync state.
52. **Direct Align mode** — New scan mode in Smart Sync that skips Whisper transcription entirely. Instead of transcribing the audio and text-matching against subtitles, it passes the subtitle text directly to WhisperX's `wav2vec2` forced alignment model, which finds exactly where each cue's words are spoken in the audio waveform. Produces per-cue precise timestamps (~50ms) for every alignable cue — no sampling, no anchor interpolation. Fastest mode (only needs audio extraction + alignment, no transcription). Whisper model selection hides when Direct Align is active (not needed). Only available when WhisperX engine is selected. Best for same-language subtitles; won't work for translated subs or HI-only cues (`[door slams]`, `♪ music ♪` are automatically skipped).
53. **VAD boundary snapping** — After Direct Align produces per-cue timestamps from WhisperX forced alignment (~50ms), Silero VAD runs on the full audio to detect exact speech onset/offset boundaries (~20ms). Each cue's start time is snapped to the nearest VAD-detected speech onset within a ±150ms window. Uses `faster_whisper.vad` (ONNX, already installed) with tight parameters: no padding, 150ms min silence gap, 100ms min speech duration. Logs snap count and total speech segments detected. Gracefully skips if VAD fails or `faster_whisper` is not installed.
54. **Character-level alignment** — WhisperX alignment now requests character-level timestamps (`return_char_alignments=True`) in both Precise and Direct Align modes. Timestamp extraction priority: char-level (~10-20ms) → word-level (~50ms) → segment-level (~200ms). Tightest available precision is used for each cue.
55. **`transformers` version pin** — WhisperX install command now pins `transformers<4.45` to avoid the `is_offline_mode` import error in newer transformers versions. Error handling in `smart_sync()` detects this specific error and shows the fix command.
56. **Smart Sync Save button** — Added 💾 Save button to the Smart Sync dialog button row in both editors. Saves the file immediately after Apply Sync / Re-time All without needing to close the dialog first. Calls the same `do_save_file()` as File → Save.
57. **Smart Sync menu renamed** — "Smart Sync (Whisper)..." renamed to "Smart Sync..." in both editors' Timing menu.
58. **Remove Stray Notes** — Renamed "Remove Music Notes" to "Remove Stray Notes" in both editors' Tools menu and Batch Filter.
59. **Reduce to 2 Lines filter** — New subtitle filter (Tools menu + Batch Filter) that intelligently reflows cues with 3+ lines down to 2 lines. Logic: respects dialogue dashes (each speaker on their own line), splits at sentence boundaries (`.` `!` `?`) with balanced line lengths, falls back to word-boundary split near midpoint, collapses short text to one line (≤42 chars). Much smarter than a simple line join.
60. **Quick Sync submenu** — New `Timing → Quick Sync` submenu in both editors with "Set First Cue Time..." command. Opens a dialog showing the first cue's text and current timestamp. User enters the correct start time (with live offset preview) and all cues shift accordingly.
61. **mpv player integration** — Set First Cue Time dialog includes ▶ Play Video and ⏱ Mark Time buttons. Play launches mpv with IPC socket (`--input-ipc-server`), paused, with OSD time display and millisecond fractions. Mark Time queries mpv's playback position via JSON IPC and fills the timestamp field. Auto-detects video file from subtitle path. Cleans up mpv process and socket on dialog close. Requires `mpv` (not bundled).
62. **TV Show Renamer** — Tool (Tools → 📺 TV Show Renamer) for batch renaming TV show and movie files using TVDB or TMDB metadata. Features: **dual provider support** (TVDB v4 and TMDB v3, switchable via Settings → Provider), **automatic multi-show loading** (detects unique show/movie names from filenames and auto-searches the active provider with threaded progress bar and cancel button), **movie support** (movies renamed to `Name (Year).ext`), fuzzy filename-to-show matching (substring, ratio, and word-overlap scoring), user disambiguation dialog with poster thumbnails and show synopsis, **menu bar** (File, Edit, Settings, Help) with keyboard shortcuts, **undo after rename** (↩ Undo button, Ctrl+Z — reverts the last rename batch; multiple undo levels supported), **manual episode editing** (right-click → Set Episode or Edit → Set Episode to correct season/episode numbers), **multi-episode file support** (`S01E01E02`, `S01E01-E03` — generates combined names like `Show - S01E01-E02 - Title 1 & Title 2`), configurable filename template via Settings → Filename Template with preset buttons, API Keys dialog for both providers, episode number parser (handles S01E01, 1x01, Season 1 Episode 1, S01E01E02 multi-episode, and date-based `2026.04.22` patterns), drag-and-drop files/folders, subtitle tag preservation (language/forced/SDH detected from filename and file content via `langdetect`), live preview of old → new filenames, **enhanced right-click context menu** (Set Episode, Copy New Name, Open Folder, Remove Selected, Remove Show, Clear All), filename sanitization, skip-if-exists safety.
63. **Version bumped to 1.8.0.**

### 2026-04-25 (v1.7.0 — Spell Checker, Smart Sync & Subtitle Editor Enhancements)
19. **Subtitle spell checker** — Interactive spell check dialog (Tools → Spell Check, F7) in both subtitle editors. Scans all cues using `pyspellchecker`, highlights errors in salmon/red, navigates through errors with Replace, Replace All, Skip, Ignore, Add to Dict, Add as Name buttons. Custom dictionary (`custom_spell_words`) persisted to preferences. Integrates `custom_cap_words` as known words. Auto-install prompt if not installed.
20. **Search/Replace List** — Persistent find/replace pairs accessible from Tools menu in both editors. Add/remove/clear pairs, case-sensitive toggle, "Apply All" runs all rules across all cues. Shared with Batch Filter pairs.
21. **Smart Sync (Whisper)** — Auto-sync subtitles to video using `faster-whisper` speech recognition. Timing → Smart Sync in both editors. Quick Scan (configurable segments × minutes) for fast offset detection, Full Scan (entire audio) for per-cue re-timing. Text matching via `SequenceMatcher` with >60% similarity threshold. Auto-backup (`_presync` file), progress bar, results log, cancel support, auto-install prompt.
22. **Re-time All** — Per-cue timestamp adjustment using `retime_subtitles()`. Builds anchor points from matched subtitle↔Whisper pairs, linearly interpolates unmatched cues, extrapolates before/after anchors. Handles frame rate changes, different cuts, different sources (streaming → Blu-ray).
23. **`two_point_sync()` utility** — Linear timestamp resync using two reference points. Computes slope + intercept and applies to all cues. Available in Timing → Offset / Stretch dialog.
24. **Subtitle editor menu renamed** — "Filters" menu renamed to "Tools" in both editors.
25. **HI filter improvements** — Fixed orphaned colons after parenthetical removal (`-(whispers): text` → `-text`; `Woman 2 (on TV): text` → `text`). Fixed speaker labels ending with digits (`Announcer 1:`, `Woman 2:`) not being removed.
26. **Search & Replace fix** — Fixed text truncation bug in Replace/Replace All: switched from `re.sub` (which interprets replacement as regex) to safe literal string replacement.
27. **Find scroll fix** — Search results and spell check now scroll the found cue to the middle of the treeview (not the bottom edge) using a scroll-past-then-back technique.
28. **`_center_on_main()` fix** — Dialog centering now uses actual window geometry instead of `winfo_reqwidth()`, properly centers large windows like the subtitle editor.
29. **OCR `/` → `l`/`I` improvements** — Added comprehensive slash-combo fixes: `//` → `ll`, `/7/`/`17/`/`/17/` → `I'll`, `/I` → `I`, `A/` → `Al`. Fixed ordering so `/` → `l` runs before `l` → `I` rules.
30. **OCR music note improvements** — Added trailing `f` → `♪`, `$f`/`£f` ligatures → `♪`, `-)`→ `-♪`, `[Speaker]` + marker → `[Speaker] ♪`, no-space start markers (`>And` → `♪ And`), expanded milestone keywords in throttle.
31. **Version bumped to 1.7.0.**

### 2026-04-25 (v1.7.1 — Smart Sync Refinements)
32. **Sequential matching** — Replaced unordered matching with sequential matcher that walks through cues and Whisper segments in chronological order. Each match must come after the previous in the timeline. Prevents cross-matching where repeated phrases match to wrong positions.
33. **Offset consistency check** — New matches are rejected if their offset differs from the average of the last 5 matches by more than ±30 seconds. Catches bad jumps that would cascade through the rest of the file.
34. **Improved text normalization** — `_normalize()` now strips speaker labels (`JUNIOR:`, `JIMMY:`), HI annotations (`[ELEVATOR BELL]`), parenthetical descriptions, and music notes before text comparison. Enables matching between SDH and regular subtitles.
35. **Search window expanded** — Sequential matcher looks up to 100 Whisper segments ahead (was 50) to handle files with different cue splitting.
36. **Word-level timestamps** — Uses `seg.words[0].start` (first word onset) instead of `seg.start` (segment start with silence padding) for ~300ms more precise anchor points.
37. **Full Scan mode** — "Full Scan (for Re-time)" radio button transcribes entire audio instead of sampling. Segments row hides when Full Scan is selected. Dynamic timeout scales with audio duration (`max(120, minutes × 2 + 60)`).
38. **Fine-tune offset** — Configurable ±2000ms adjustment (default +400ms) applied after sync/re-time to compensate for Whisper's early speech detection. Spinbox in the Smart Sync dialog, applied to both Apply Sync and Re-time All.
39. **Duration mismatch warning** — Pre-check compares video duration vs subtitle duration before scanning. Warns if >15% difference (likely different cuts).
40. **Scan mode UI** — Quick Scan / Full Scan radio buttons with segments row that hides/shows. Separator line removed for cleaner layout.
41. **Results display fix** — Replaced unreliable `sd.after(0, _done)` callback with direct `_progress()` reporting for results. Throttle bypass expanded to cover all result keywords. 300ms flush delay before results.
42. **Spinbox display fix** — Changed `ttk.Spinbox` to `tk.Spinbox` for segment fields to fix empty display on some Tk versions. `StringVar` with `.isdigit()` validation instead of `IntVar`.
43. **Zenity file picker** — Browse button for video file uses zenity (GTK native dialog) with better sizing, starts in subtitle's directory. Falls back to tkinter if zenity not available. Parented to Smart Sync window to prevent focus stealing.

### 2026-04-25 (v1.6.0 — Bitmap Subtitle OCR & Internal Subtitle Enhancements)
11. **Bitmap subtitle OCR** — PGS and VobSub bitmap subtitles can now be converted to SRT text via Tesseract OCR. Single-pass ffmpeg rendering overlays the subtitle stream on a black canvas with scene-change detection (~2 min for a 1-hour episode). Parallel OCR via `ThreadPoolExecutor` across multiple CPU cores. Smart cropping via `getbbox()` reduces Tesseract workload by ~13x. Music note frame detection replaces tiny isolated symbols with ♪ without running OCR.
12. **Live OCR monitor window** — Real-time progress window showing: progress bar with ETA, current subtitle image preview (cropped), OCR'd text result, scrolling cue list building live, cancel button. All phases show progress: rendering (time-based %), blank frame filtering (count), OCR (frame count with parallel workers).
13. **OCR post-processing (`_fix_ocr_text()`)** — Comprehensive regex cleanup for Tesseract mistakes: `|`/`1`/`!`/`l` → `I` (context-aware, preserves real numbers); `/` → `l` or `I` (between letters vs standalone); `//` → `ll`; `/7/`, `17/`, `/17/` → `I'll` (slash+digit combos); `™` → `'`; music note markers (`2`, `>`, `$`, `&`, `£`, `©`, `»`, `#`, `*`, `?`, `Sf`, `D>`, `P`, `f`) → `♪` at start/end of lines including dash-prefixed and no-space variants; `[Speaker]` + marker → `[Speaker] ♪`; garbled-only cues → `♪`.
14. **Empty subtitle track detection** — `get_subtitle_streams()` now reads `NUMBER_OF_FRAMES`/`NUMBER_OF_BYTES` from muxer statistics. Empty tracks shown with red `[⚠ EMPTY]` flag, unchecked by default, format dropdown disabled, edit button hidden, skipped during extraction. Prevents 0-byte extraction and "file in use" errors.
15. **"Set All To" dropdown** — Internal Subtitles dialog now has a "Set All To" combobox + Apply button in the top bar to set all tracks' output format at once.
16. **`BITMAP_SUB_CODECS` constant** — Consolidated bitmap subtitle codec set (`hdmv_pgs_subtitle`, `dvd_subtitle`, `dvb_subtitle`, `dvb_teletext`, `xsub`) as a module-level `frozenset`, replacing four inline definitions.
17. **Installer updated** — `tesseract-ocr` and `tesseract-ocr-eng` detected in system dependency check (warning if missing). `pytesseract` auto-installed via pip.
18. **Version bumped to 1.6.0.**

### 2026-04-25 (v1.5.1 — Media Processor Enhancements)
5. **Parallel processing** — Media Processor now supports concurrent file processing via `ThreadPoolExecutor`. Parallel checkbox + Jobs spinner in the operations panel (defaults to CPU core count, capped at 8). Thread-safe logging via `win.after()` and `threading.Lock` for shared counters. Stop button terminates all in-flight processes. Falls back to sequential when disabled or Jobs=1.
6. **Output folder option** — Added "Replace in-place" / "Save to folder:" radio buttons. When saving to a folder, originals are preserved. Browse button + entry field for output path. Auto-creates output directory. Validates folder is set before processing.
7. **Per-file operation overrides** — Right-click context menu on any file: ⚙️ Override Settings (audio codec/bitrate, strip chapters/tags/subs, mux subs, metadata, container), 📎 Manage Subtitles (add/remove files, toggle main↔forced), 🔄 Re-probe File, ❌ Clear Override, 🗑️ Remove. ⚙️ icon on files with overrides. Double-click opens override dialog. `_ov()` helper resolves per-file → global fallback throughout `_build_cmd()`.
8. **Custom subtitle matching** — Configurable language code field (default: `eng`) replaces hardcoded `.eng.srt` pattern. Matches `*.{lang}.srt` and `*.{lang}.forced.srt`, falls back to bare `*.srt`. 🔄 Rescan button re-detects subtitle files with the current language setting. Subtitle manager dialog allows manual add/remove and main↔forced toggle per file.
9. **Output container selection** — `.mkv` / `.mp4` dropdown in operations panel and per-file override dialog. Auto-handles subtitle codec compatibility: `mov_text` for MP4 output, `srt`/`copy` for MKV.
10. **File re-probe after processing** — Automatically re-probes all completed files after batch finishes. Updates Audio, Internal Subs, External Subs, and Size columns with fresh data from ffprobe. Also available manually via right-click → 🔄 Re-probe File.

### 2026-04-25 (v1.5.0 — Media Processor & Metadata Cleanup)
1. **Metadata cleanup options in conversion pipeline** — Three new checkboxes in the settings panel: "Strip chapters" (`-map_chapters -1`), "Strip tags" (`-map_metadata -1`), and "Set track metadata" (per-track language codes with configurable V/A/S fields, clears container title and track names). All options available as per-file overrides. New `_add_metadata_args()` helper called in both single-pass and two-pass ffmpeg command paths. Settings persisted to preferences with save/load/reset support.
2. **Media Processor** — New standalone tool window (Tools → Media Processor, `Ctrl+M`) for remux-only post-processing of already-encoded files. Replicates the functionality of `scripts/media-process.sh` in a GUI with a single ffmpeg command per file (`-c:v copy`, no re-encoding). Features: convert audio (codec dropdown with aac/ac3/eac3/mp3/opus/flac/copy + bitrate), strip chapters/tags/subtitles, mux external subtitles (auto-detects `*.eng.srt` / `*.eng.forced.srt`), set track metadata. Includes preflight validation, threaded processing, progress bar, color-coded log with Clear Log button, drag-and-drop support, and atomic file replacement via temp file.
3. **`get_audio_info()` helper** — New ffprobe-based utility function for probing audio streams (codec, channels, sample rate, bit rate, language, title). Replaces the `mediainfo` dependency used in the bash script. Used by the Media Processor for smart audio codec skip logic (copies if source already matches target).
4. **Audio codec selection in Media Processor** — "Convert audio to AC-3" replaced with a general "Convert audio:" checkbox + codec dropdown (aac, ac3 Dolby Digital, eac3 Dolby Digital+, mp3, opus, flac, copy) + bitrate dropdown. Smart skip: auto-copies if source already matches target codec. Handles experimental codecs (`-strict -2`) and lossless codecs (no bitrate flag).

### 2026-04-22 (post v1.4.0)
11. **Video bitrate excluded from preferences** — Removed video bitrate from the save/load preferences cycle. Bitrate always starts at the default 2.0M on launch. Prevents hidden mismatches where a previously saved bitrate silently overrides what the user sets via the slider, leading to unexpected output filenames and quality.

### 2026-04-22 (v1.4.0 — Transport Stream & Closed Caption Support)
1. **MPEG Transport Stream (.ts) input support** — Added `.ts`, `.m2ts`, and `.mts` to `VIDEO_EXTENSIONS`. Files can be loaded via file picker, drag-and-drop, and folder scanning.
2. **MPEG-TS output container** — Added `.ts` as an output container option in the container dropdown. Container-codec compatibility matrix allows H.265, H.264, MPEG-4, and stream copy.
3. **MPEG-TS subtitle handling** — Text-based subtitles (SRT, ASS, etc.) are skipped when outputting to `.ts` (only DVB subtitles supported). DVB subtitle streams from the source are preserved via copy. External subtitle embedding falls back to `dvb_subtitle` codec for `.ts` output.
4. **Drag-and-drop `file://` URI fix** — Both `on_drop` (main file list) and `on_drop_subtitle` (subtitle editor) now detect and parse `file://` URIs with percent-encoded paths, which is how Linux file managers (Nautilus, Thunar, Dolphin, Nemo) send drag-and-drop data via tkinterdnd2. Previously, files with spaces in the name were split incorrectly and rejected. Uses `urllib.parse.unquote` + `urlparse` for proper decoding.
5. **ATSC A53 closed caption detection** — New `detect_closed_captions()` utility function uses ffprobe to scan the first 30 video frames for "ATSC A53 Part 4 Closed Captions" in frame side data (`-show_entries frame=side_data_list:side_data=side_data_type`). Runs in ~40ms. Automatically triggered when `.ts`/`.m2ts`/`.mts` files are added to the queue.
6. **CC badge in file queue** — Files with detected closed captions show a "CC" prefix in the file list, alongside existing ⚙️ (overrides) and 📎 (external subs) indicators.
7. **A53 CC passthrough** — When transcoding files with detected CC, ffmpeg encoders are configured to preserve A53 CC data in the output video stream. Encoder-specific flags: `-a53cc 1` for libx265, NVENC; on by default for libx264, QSV, VAAPI. CC data remains embedded in the video bitstream and can be displayed by players that support it (VLC, mpv, etc.).
8. **CC extraction to SRT** — If `ccextractor` is installed, closed captions are extracted to a temporary SRT file before encoding and embedded as a separate subtitle track in the output container (SRT for MKV, mov_text for MP4). Timeout auto-calculated from video duration. Temp files cleaned up in `finally` block.
9. **Subtitle dialog CC info** — When a file has CC but no subtitle streams, the Internal Subtitles dialog shows: CC detection status, A53 passthrough confirmation, ccextractor availability, and a toggle checkbox for SRT extraction (if ccextractor is installed). If ccextractor is not found, shows install suggestion.
10. **CLI multi-format support** — `convert_videos.sh` now scans for all supported video formats (`.mkv`, `.mp4`, `.avi`, `.mov`, `.wmv`, `.flv`, `.webm`, `.ts`, `.m2ts`, `.mts`) instead of only `.mkv`. Updated log messages from "MKV files" to "video files".

### 2026-04-21 / 2026-04-22 (v1.3.0 → v1.3.1)
1. **Standalone subtitle editor** — Full app-style subtitle editor accessible from Tools → Subtitle Editor. Opens a blank window with File menu (Open, Save, Save As, Export SRT, Batch Filter, Close), drag-and-drop for subtitle and video files, and all filters/editing tools. Independent of the converter pipeline.
2. **Video subtitle extraction & re-mux** — Drag a video file onto the standalone editor (or use File → Open) to extract an internal subtitle stream. Stream picker dialog shows Language, Format, Title, and Flags in a proper table view. Edited subtitles can be saved directly back into the video via instant re-mux (`-c copy`, no re-encoding). Preserves stream order, metadata (language, title), and disposition flags (default, forced, SDH). Handles MP4 containers (`mov_text` codec).
3. **Save to Video button** — Added to the converter's internal subtitle editor for direct re-mux of edited subtitles back into the source video without going through the full encoding pipeline.
4. **Batch filter** — New window (Tools → Batch Filter or File → Batch Filter from editor) for applying filters to multiple subtitle files at once. Drag-and-drop multiple files, select filters via checkboxes (Select All/Deselect All), choose overwrite or subfolder output, progress bar with per-file color-coded status (green=success, red=error).
5. **New subtitle filters:**
   - Remove Leading Dashes — strips leading `-` from each subtitle line
   - Remove ALL CAPS HI (UK style) — removes unbracketed all-caps HI descriptions (e.g. `SHEENA LAUGHS`, `DOOR SLAMS`); preserves short words (≤3 chars), known acronyms (FBI, BBC, NHS), and single non-HI words; integrated into the main Remove HI filter as well
   - Remove Off-Screen Quotes (UK style) — strips wrapping single quotes used for off-screen dialogue (`'Hello there.'` → `Hello there.`); preserves contractions (`'cause`, `'til`, `'bout`, `'em`) via blocklist; preserves dropped-g words (`somethin'`, `thinkin'`) by checking character before closing quote; handles opening/closing quotes independently across cues
   - ALL CAPS HI descriptor labels (`HIGH-PITCHED:`, `MUFFLED:`, `NARRATOR:`) — stripped by the Remove HI filter, keeping text after the colon; each ALL CAPS word must be 4+ letters or contain a hyphen to preserve short acronyms (FBI:, BBC:)
6. **Speaker labels merged into Remove HI** — Remove Speaker Labels removed as a separate menu item since Remove HI already handles speaker label removal.
7. **Fix ALL CAPS improvements:**
   - Cross-cue sentence awareness — doesn't capitalize the first word of a cue if the previous cue didn't end with sentence-ending punctuation (fixes false capitals on continued sentences like `toe-jam-eating` after `puke-spouting slime`)
   - Cross-line sentence awareness — doesn't capitalize the first word of subsequent lines within a cue unless the previous line ended with `.!?` or the line starts with a dash
   - Second-pass safe — custom names are applied even on already-converted text (separate `apply_custom_names` pass that always runs regardless of uppercase ratio)
   - Sentence-start capitalization always applied — even on re-runs where text is no longer all-caps
   - Non-modal Add Names dialog — uses `attributes('-topmost', True)` instead of `grab_set()` so users can scroll the subtitle list behind it to find names while the dialog is open
   - Custom names preserve exact user-entered casing (e.g. `McDonald` stays `McDonald`, not `Mcdonald`)
8. **Search & Replace improvements:**
   - Replace (single) button — replaces the first match starting from current selection, then auto-selects and scrolls to next match
   - Wrap around checkbox — optional wrap from end to beginning when using Replace
   - Right-click Copy/Paste/Cut/Select All on Find and Replace entry fields
9. **Insert line above/below** — Right-click context menu option to insert a blank subtitle cue above or below the selected cue with auto-timed timestamps.
10. **Inline editor improvements:**
    - Right-click Cut/Copy/Paste/Select All context menu (with focus management to prevent edit box from closing)
    - Delete key no longer deletes treeview rows while inline editing (checks if event came from `tk.Text` widget)
11. **Context menu fix** — Changed from `ctx_menu.post()` to `ctx_menu.tk_popup()` so right-click menus dismiss when clicking outside.
12. **Timestamp column widened** — Increased from 180px to 260px (min 220px) to prevent overlap with text column.
13. **GPU backend verification** — `detect_gpu_backends()` now runs a quick 1-frame test encode for each detected backend at startup. Backends that fail (missing drivers, broken runtime) are excluded from the dropdown. Fixed test resolution from 64x64 to 256x256 (NVENC requires minimum resolution).
14. **Intel QSV VAAPI backend support** — QSV detection tries three initialization methods: direct MFX session, QSV via VAAPI backend (libvpl/oneVPL — how HandBrake does it on modern Linux), and explicit device init. When QSV works via VAAPI backend, the hwaccel flags are automatically updated to use that init path during encoding.
15. **Automatic CPU fallback** — When GPU encoding fails mid-conversion, the app automatically retries with CPU encoding (logs a warning, cleans up failed output, builds CPU settings with default preset, updates output filename).
16. **MP4 cover art fix** — Changed video stream mapping from `-map 0:v?` (all video streams) to `-map 0:v:0?` (first video stream only). Prevents embedded PNG cover art/thumbnails from being sent through the video encoder, which caused "Function not implemented" errors.
17. **mov_text → SRT conversion** — When outputting to MKV, `mov_text` subtitle streams (MP4-only codec) are now auto-detected and converted to SRT instead of failing with "Subtitle codec mov_text is not supported."
18. **Audio controls always visible** — Audio codec and bitrate dropdowns are always shown in the settings panel. Greyed out in Video Only mode, enabled in Video + Audio and Audio Only modes. Eliminates confusion about where audio settings are.
19. **Default transcode mode** — Always starts in Video Only mode regardless of saved preferences.
20. **Version bumped to 1.3.1.**
21. **Fix ALL CAPS combined dialog** — Merged "Fix ALL CAPS" and "Fix ALL CAPS + Add Names..." into a single "Fix ALL CAPS..." menu item. Opens a dialog showing saved custom names with an Apply button. No more two separate entries.
22. **Custom names persisted** — `custom_cap_words` saved to preferences JSON and loaded on startup. Names added in the dialog auto-save immediately and persist across sessions.
23. **Fix ALL CAPS second-pass sentence capitalization** — When re-running Fix ALL CAPS on already-converted text, sentence-start capitalization is always applied (fixes the case where "of" stayed lowercase after a cue ending with a period).
24. **Remove HI auto-runs Fix ALL CAPS on all-caps files** — When Remove HI is clicked and the subtitle text is mostly ALL CAPS (≥60%), Fix ALL CAPS runs first automatically to prevent false HI detection on regular dialogue. Logged as an info message. In batch filter, if both are checked, Fix ALL CAPS is reordered to run before Remove HI.
25. **Batch Search & Replace** — New section in the Batch Filter window with persistent find/replace pairs. Features: case-sensitive toggle, listbox of saved pairs, Add/Remove/Clear All buttons, right-click copy/paste on entry fields. Replacements are applied after filters to every file. Can be used standalone without filters. Pairs saved to preferences across sessions.
26. **Address/place words in PROPER_NOUNS** — Added Street, Avenue, Road, Drive, Lane, Boulevard, Court, Place, Terrace, Highway, Parkway, Plaza, Bridge, Park, Lake, River, Mountain, Island, North, South, East, West to the proper noun set for Fix ALL CAPS.
27. **Batch filter two-column layout** — Filter checkboxes split into two columns for better use of vertical space. Window height reduced.
28. **Manage Names in batch filter** — "Names..." button next to Fix ALL CAPS checkbox in batch filter opens the custom names editor.

### 2026-04-20
1. **NVENC hwaccel fix** — Removed `-hwaccel_output_format cuda` from the NVENC backend. Sources with mid-stream resolution changes (e.g. varying letterbox ratios) caused `scale_cuda` filter reinitialization failures ("Error reinitializing filters / Function not implemented"). Without `-hwaccel_output_format cuda`, frames pass through system memory between decode and encode; CUDA decoding and NVENC encoding are still hardware-accelerated with negligible performance difference.
2. **Batch ETA** — Added real-time estimated time remaining for the entire batch during multi-file encoding. Uses rolling average encoding speed (video-seconds per wall-second) from completed files, weighted by remaining file durations. Displayed as "Batch: Xh Ym left" in the status bar. Self-corrects as files complete. Bootstraps from current file progress before the first file finishes.
3. **Subtitle editor** — Full-featured inline text editor for both internal subtitle streams and external subtitle files. Accessed via ✏️ button in the Internal/External Subtitles dialog or by double-clicking a file. Features:
   - SRT parser with round-trip read/write
   - Inline text editing (double-click cell, Ctrl+Enter to save)
   - Menu bar with Filters, Edit, and Timing menus (replaced toolbar buttons)
   - **Filters menu:**
     - Remove HI — strips `[brackets]`, `(parentheses)`, and speaker labels (`Name:`) in one pass; handles multi-line brackets and unclosed brackets; cleans orphaned colons and leftover newlines
     - Remove Tags — strips `<i>`, `</i>`, `{\an8}`, etc.
     - Remove Ads/Credits — strips "Subtitled by...", site names, URLs (only when paired with other ad content); supports custom patterns saved to preferences
     - Remove Speaker Labels — standalone filter for `Name:` labels; handles mixed case; avoids timestamps and single-char labels
     - Remove Stray Notes — removes cues containing only `♪`/`♫` symbols (keeps lyrics with text)
     - Remove Duplicates — removes consecutive identical cues
     - Merge Short Cues — combines sentence fragments with <1s gap
    - Reduce to 2 Lines — intelligently reflows 3+ line cues to 2 lines; respects dialogue dashes, splits at sentence boundaries, falls back to midpoint word split
     - Fix ALL CAPS — converts all-caps to sentence case with proper noun capitalization (days, months, countries, cities, holidays, abbreviations); custom character name support via dialog
     - Manage Ad Patterns — view built-in patterns, add/remove custom patterns (saved to preferences)
   - Search & Replace across all cues
   - Timing tools: offset (shift ±ms) and stretch (scale by factor)
   - Split cue at midpoint / Join consecutive cues
   - Per-action Undo/Redo stack (Ctrl+Z / Ctrl+Y) with full reset
   - Color-coded rows: yellow=modified, blue=HI, pink=tags, orange=long lines (>42 chars), green=search match; index-independent (survives row deletion)
   - Video preview at cue timestamp via ffplay (right-click or ▶ button)
   - Export edited subtitle as standalone `.srt` file
   - Right-click context menu (preview, split, join, delete)
   - External subtitle editing — ✏️ button in External Subtitles dialog; reads/writes directly to `.srt` files; converts other formats via ffmpeg
   - Edited internal subtitles automatically embedded during encoding (replaces original stream via additional ffmpeg input)
4. **Double-click to open subtitles** — Double-clicking a file in the queue opens the Internal Subtitles dialog directly. Uses `after_idle` scheduling with `grab_set` deferred to prevent empty window rendering.
5. **"Open with" support** — App appears in the file manager's right-click "Open with" menu for video files. Desktop file includes `MimeType` for video MIME types and `%F` in Exec. Files passed as command-line arguments are auto-added to the queue on startup. Also works from terminal: `docflix /path/to/video.mkv`.
6. **Notify moved to settings** — Sound notification controls (enable, sound selection, preview) moved from the main toolbar to Default Settings dialog to reduce main page clutter.
7. **Scroll bleed-through fix** — Internal Subtitles dialog and subtitle editor now use local widget scroll bindings instead of `bind_all`, preventing mousewheel events from bleeding through to parent windows.
8. **Speaker label removal improved** — Now handles mixed-case speaker names (e.g. `narrator:`, `mom:`) while avoiding false positives on timestamps (e.g. `2:30`, `12:00`) and single-character labels. Consumes trailing newlines after labels.
9. **Ad removal improved** — URL-only lines (`www.*`) are only removed when paired with other ad content or when the cue contains nothing but a URL. Dialogue mentioning websites (e.g. "Go to www.fbi.gov") is preserved. Added `captioning` pattern variant.
10. **HI removal improved** — Brackets and parentheses now match across newlines (`re.DOTALL`). Unclosed brackets at start of cue (e.g. `[Captioning sponsored\nby NICKELODEON`) are removed. Orphaned colons and newlines cleaned up after removal. Speaker labels included in HI filter for one-pass SDH cleanup.
11. **External Subtitles dialog fix** — Increased dialog size and minimum height so Add Subtitle File, Save, and Cancel buttons are fully visible. Added proper `grab_set`/`wait_window` for modal behavior.
12. **Version bumped to 1.2.0.**

### 2026-04-19
1. **Multi-GPU support** — Replaced single NVIDIA-only GPU support with a pluggable backend system (`GPU_BACKENDS` dict). Now auto-detects and supports NVIDIA NVENC, Intel QSV, and AMD VAAPI. Each backend defines its own hwaccel flags, encoder names, presets, quality flags, and detection method. UI changed from CPU/GPU radio buttons to a dropdown combobox showing only detected backends. Backward compatible with old `encoder: 'gpu'` preference values. Version bumped to 1.1.0.
2. **Bash CLI multi-GPU** — Added `--qsv` and `--vaapi` flags to `convert_videos.sh` alongside existing `-g` for NVIDIA. Backend-specific detection, hwaccel flags, and encoder options.
3. **External subtitle support** — Full drag-and-drop external subtitle system. Auto-matches by filename stem (strips up to 3 trailing tokens for patterns like `.eng.forced.srt`). Auto-detects language, forced flag, and default flag from filename. Two modes: embed (soft sub) and burn-in (hardcoded). Per-subtitle language, default, and forced disposition flags. "Remove existing subtitle tracks" option to replace internal subs. Folder scan prompts to attach matching subtitles. 📎 icon indicator on files with external subs. Grid-based dialog with right-justified controls and responsive resize.
4. **Folder browser fix** — Folder selection dialogs now use zenity (GTK native dialog with proper single-click + Open button) with tkinter `askdirectory` fallback. Applied to all 3 folder browse locations.
5. **Header layout redesign** — Reorganized header to Option C layout: title + encoder combo on first row, horizontal separator, then toolbar row with Change Folder, Refresh, output path, Set Output, and Reset controls.
6. **UI polish** — Renamed "Subtitle Tracks" to "Internal Subtitles" in context menu and dialog. Changed notification preview button from ▶ (play) to 🔊 (speaker) to avoid confusion with media playback. Shortened encoder dropdown labels (removed GPU model names).

### 2026-04-06
1. **install.sh bug fix** — `logo_transparent.png` was listed as a required source file but is excluded from the GitHub repo (it's a generated file). Fresh clones from GitHub would fail at the source file check. Fixed by removing it from the required files list and adding a generation step to the installer that creates it from `logo.png` using Pillow. Falls back gracefully to the 🎬 emoji in the title bar if generation fails.
2. **Multi-subtitle stream bug fix (part 2)** — Fixed the default conversion path (no subtitle dialog used) dropping all but the first subtitle stream. Root cause: ffmpeg's default stream selection only picks one subtitle track unless explicitly told otherwise. Fixed by replacing `-c:s copy` with `-map 0:v? -map 0:a? -map 0:s? -c:s copy` so all subtitle streams are always preserved. The `?` suffix makes each map conditional so files with no subtitles are unaffected.
2. **Multi-subtitle stream bug fix (part 1)** — Fixed ffmpeg command generation in the per-file subtitle dialog path for files with more than one subtitle stream. Previously, only the first subtitle stream was correctly handled; subsequent streams had their codec specifier overwritten. Fixed by using per-output-stream specifiers (`-c:s:0`, `-c:s:1`, etc.) instead of a single `-c:s` flag.
3. **✅ Clear Finished button** — Added to the control bar. Removes all successfully completed (`✅`) and skipped (`⏭️`) files from the queue, leaving failed (`❌`) and pending files for retry. Useful when re-running failed files with different settings (e.g. disabling HW Decode after a CUDA error).

### 2026-04-05
1. **Web GUI removed** — `video_converter_gui.html`, `video_converter_server.py`, and `launch_gui.sh` removed. The Tkinter desktop app is the sole interface going forward.
2. **install.sh created** — Full installer/uninstaller. No sudo required. Creates system app menu entry, terminal command, and icon.
3. **GitHub repository published** — https://github.com/docman1967/docflix-video-converter
4. **README.md created** — Full GitHub README with features, install instructions, CLI reference, encoding guide, and screenshot.
5. **System Default player** — Added `"System Default"` to the video player dropdown. Uses `xdg-open`.
6. **Removed "Settings Saved" popup** — Preferences save silently to log only.
7. **Removed "Save Preferences" from Settings menu** — Preferences auto-save on dialog close.
8. **Removed path label from title bar** — Title shows app name only.
9. **Custom logo in title bar** — `logo_transparent.png` at 32×32 px via PIL/ImageTk.
10. **Multi-monitor launch fix** — Window launches on the monitor containing the mouse pointer.
11. **Background launcher with logging** — `run_converter.sh` uses `nohup ... &` with timestamped log files, auto-pruned to 10.
