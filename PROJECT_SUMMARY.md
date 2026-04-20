# Video Converter — Project Summary

**Last Updated:** 2026-04-20 (rev 5)  
**Source / Backup:** `/home/docman1967/scripts/video_converter/`  
**Installed To:** `~/.local/share/docflix/`  
**GitHub:** https://github.com/docman1967/docflix-video-converter  
**Purpose:** Batch convert video files to H.265/HEVC format using ffmpeg, with support for CPU and multi-GPU encoding (NVIDIA NVENC, Intel QSV, AMD VAAPI).

---

## File Inventory

| File | Size | Modified | Description |
|------|------|----------|-------------|
| `video_converter.py` | ~272 KB / 6,201 lines | 2026-04-20 | Primary Tkinter desktop GUI app |
| `convert_videos.sh` | ~20 KB / 541 lines | 2026-04-19 | Standalone bash CLI batch converter |
| `run_converter.sh` | ~2 KB / 57 lines | 2026-04-05 | Launcher for Tkinter desktop app |
| `install.sh` | ~11 KB / 309 lines | 2026-04-06 | Installer / uninstaller |
| `logo.png` | 136 KB | 2026-03-27 | Original app logo (RGB, 840×958) |
| `logo_transparent.png` | 100 KB | 2026-04-05 | Background-stripped version used in title bar |
| `screenshot.png` | — | 2026-04-06 | App screenshot (used in GitHub README) |
| `README.md` | — | 2026-04-06 | GitHub repository README |
| `LICENSE` | — | 2026-04-05 | MIT License |
| `.gitignore` | — | 2026-04-05 | Git ignore rules |
| `PROJECT_SUMMARY.md` | — | 2026-04-19 | This file |
| `logs/` | dir | — | Timestamped launch logs (auto-pruned to 10) |

---

## Architecture

```
┌─────────────────────────────────────────┐
│           User Interfaces (2)           │
│                                         │
│  convert_videos.sh    run_converter.sh  │
│  (Bash CLI —          (launches ↓)      │
│   headless)           video_converter.py│
│                       (Tkinter Desktop) │
└─────────────────────────────────────────┘
         Both interfaces call ffmpeg
```

---

## Interface 1: Tkinter Desktop GUI (`video_converter.py`)

The primary interface. Launched via `run_converter.sh`, the `docflix` terminal command, or the system app menu.

**Dependencies:** Python 3, `tkinter`, `tkinterdnd2`, `Pillow`, `ffmpeg`

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
- **External subtitle support:**
  - Drag-and-drop `.srt`, `.ass`, `.ssa`, `.vtt`, `.sub`, `.idx`, `.sup` files onto the queue
  - Auto-matches subtitles to video files by filename stem (strips language codes, "forced", "sdh" suffixes)
  - Auto-detects **language** from filename (e.g. `.eng.srt`, `.en.srt`)
  - Auto-detects **forced** flag from filename (e.g. `.forced.srt`)
  - Auto-sets **default** flag on the first plain (non-forced, non-SDH) subtitle
  - Two modes: **embed** (soft sub, muxed as stream) or **burn-in** (hardcoded onto video)
  - Per-subtitle **Default** and **Forced** disposition flags
  - **"Remove existing subtitle tracks from source"** option to replace internal subs
  - 📎 icon indicator on files with external subs attached
  - Folder scan prompts to attach matching subtitle files found alongside videos
- **Internal subtitle** (🎞️) dialog — per-stream format control for subtitle tracks already in the source file (double-click a file to open)
- **Subtitle editor** — full-featured text editor for internal subtitle streams:
  - Direct inline text editing (double-click a cell)
  - Filters: Remove HI `[brackets]` `(parens)`, Remove Tags, Remove Ads/Credits, Remove Speaker Labels, Remove Music Notes, Remove Duplicates, Merge Short Cues
  - Custom ad pattern management (saved to preferences)
  - Search & Replace across all cues
  - Timing tools: offset (shift ±ms) and stretch (scale by factor)
  - Split/Join cues
  - Undo/Redo stack (Ctrl+Z / Ctrl+Y) with full reset
  - Color-coded rows: yellow=modified, blue=HI content, pink=tags, orange=long lines, green=search match
  - Character count warning for lines exceeding 42 characters
  - Video preview at selected cue timestamp (via ffplay)
  - Export edited subtitle as standalone `.srt` file
  - Right-click context menu (preview, split, join, delete)
  - Edited subtitles are automatically embedded during encoding
- **Batch ETA** — real-time estimated time remaining for the entire batch, based on rolling average encoding speed weighted by file duration
- **Estimated output size** calculation before conversion
- **Media info** panel (shows codec, resolution, duration, streams)
- **Test encode** (30-second preview clip of settings)
- **✅ Clear Finished** button — removes completed/skipped files from queue, leaving failed files for retry
- Source file and output file **playback** via configurable media player:
  - **System Default** — delegates to `xdg-open`
  - **auto** — tries common players in order: vlc → mpv → totem → ffplay
  - Named player (vlc, mpv, etc.) — uses that specific player if installed
  - **Custom path** — full path to any executable
- Open output folder in system file manager
- Sortable, reorderable file queue
- Collapsible settings panel and detachable log window
- **Sound notification** on conversion completion (configurable in Default Settings)
- **Preferences** auto-saved to JSON on dialog close — no manual save required
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

### GPU Backend Configuration (`GPU_BACKENDS` dict)

Each backend defines:
- `hwaccel` flags (e.g. `-hwaccel cuda`)
- Per-codec encoder names (e.g. `hevc_nvenc`, `hevc_qsv`, `hevc_vaapi`)
- Presets and defaults
- Quality flag (`-cq`, `-global_quality`, `-qp`)
- Multipass support and args
- Detection method (ffmpeg encoder check + GPU name via nvidia-smi / lspci)

---

## Interface 2: Bash CLI (`convert_videos.sh`)

Headless batch converter; runs in the **current directory** and converts all `.mkv` files found there. Best for scripted/automated use.

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
| NVIDIA driver + NVENC-enabled ffmpeg | NVIDIA GPU encoding (optional) | System-specific |
| Intel media driver + QSV-enabled ffmpeg | Intel QSV encoding (optional) | System-specific |
| Mesa VAAPI driver + VAAPI-enabled ffmpeg | AMD VAAPI encoding (optional) | System-specific |

---

## Known Issues / Notes

1. **HW Decode compatibility** — Some source files (particularly those with mid-stream resolution changes or oddly encoded content) fail with hardware decode enabled. The NVENC backend no longer uses `-hwaccel_output_format cuda` to avoid filter reinitialization errors on variable-resolution sources. Workaround for remaining issues: uncheck **HW Decode** for the affected file via per-file settings override, or disable it globally in Default Settings. The GPU still handles encoding; only decoding falls back to CPU.

2. **Burn-in subtitles + HW Decode** — Burn-in subtitles require CPU-side video filtering, which is incompatible with hardware decode. The app automatically disables HW decode when any external subtitle is set to burn-in mode.

3. **Audio handling** — Default audio codec is AC3 (Dolby Digital) at 320k. Can be changed per-file via settings override or globally in Default Settings.

4. **Subtitle handling** — The desktop GUI supports both internal subtitle management (per-stream format control) and external subtitle attachment (embed/burn-in with language, default, forced flags). All subtitle streams are correctly preserved in both the default conversion path and the per-file subtitle dialog path.

5. **QSV/VAAPI without hardware** — ffmpeg may report QSV or VAAPI encoders as available even without matching GPU hardware (the encoders are compiled in but will fail at encode time). The app detects these via `ffmpeg -encoders` and shows them in the dropdown, but encoding will fail if the hardware isn't present. The error is caught and reported in the log.

---

## Change Log

### 2026-04-20
1. **NVENC hwaccel fix** — Removed `-hwaccel_output_format cuda` from the NVENC backend. Sources with mid-stream resolution changes (e.g. varying letterbox ratios) caused `scale_cuda` filter reinitialization failures ("Error reinitializing filters / Function not implemented"). Without `-hwaccel_output_format cuda`, frames pass through system memory between decode and encode; CUDA decoding and NVENC encoding are still hardware-accelerated with negligible performance difference.
2. **Batch ETA** — Added real-time estimated time remaining for the entire batch during multi-file encoding. Uses rolling average encoding speed (video-seconds per wall-second) from completed files, weighted by remaining file durations. Displayed as "Batch: Xh Ym left" in the status bar. Self-corrects as files complete. Bootstraps from current file progress before the first file finishes.
3. **Subtitle editor** — Full-featured inline text editor for internal subtitle streams. Accessed via ✏️ button in the Internal Subtitles dialog or by double-clicking a file. Features:
   - SRT parser with round-trip read/write
   - Inline text editing (double-click cell, Ctrl+Enter to save)
   - Filter menu: Remove HI, Remove Tags, Remove Ads/Credits, Remove Speaker Labels, Remove Music Notes, Remove Duplicates, Merge Short Cues
   - Custom ad pattern management with regex support (saved to preferences)
   - Search & Replace across all cues
   - Timing tools: offset (shift ±ms) and stretch (scale by factor)
   - Split cue at midpoint / Join consecutive cues
   - Per-action Undo/Redo stack (Ctrl+Z / Ctrl+Y) with full reset
   - Color-coded rows: yellow=modified, blue=HI, pink=tags, orange=long lines (>42 chars), green=search match
   - Video preview at cue timestamp via ffplay (right-click or ▶ button)
   - Export edited subtitle as standalone `.srt` file
   - Right-click context menu
   - Edited subtitles automatically embedded during encoding (replaces original stream via additional ffmpeg input)
4. **Double-click to open subtitles** — Double-clicking a file in the queue opens the Internal Subtitles dialog directly.
5. **Notify moved to settings** — Sound notification controls (enable, sound selection, preview) moved from the main toolbar to Default Settings dialog to reduce main page clutter.
6. **Scroll bleed-through fix** — Internal Subtitles dialog and subtitle editor now use local widget scroll bindings instead of `bind_all`, preventing mousewheel events from bleeding through to parent windows.
7. **Speaker label removal improved** — Now handles mixed-case speaker names (e.g. `narrator:`, `mom:`) while avoiding false positives on timestamps (e.g. `2:30`, `12:00`) and single-character labels.
8. **Ad removal improved** — URL-only lines (`www.*`) are only removed when paired with other ad content or when the cue contains nothing but a URL. Dialogue mentioning websites (e.g. "Go to www.fbi.gov") is preserved.

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
