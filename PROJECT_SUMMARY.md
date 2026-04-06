# Video Converter — Project Summary

**Last Updated:** 2026-04-06  
**Source / Backup:** `/home/docman1967/scripts/video_converter/`  
**Installed To:** `~/.local/share/docflix/`  
**GitHub:** https://github.com/docman1967/docflix-video-converter  
**Purpose:** Batch convert video files to H.265/HEVC format using ffmpeg, with support for both CPU (libx265) and NVIDIA GPU (NVENC) encoding.

---

## File Inventory

| File | Size | Modified | Description |
|------|------|----------|-------------|
| `video_converter.py` | ~166 KB / 3,828 lines | 2026-04-06 | Primary Tkinter desktop GUI app |
| `convert_videos.sh` | ~16 KB / 455 lines | 2026-03-28 | Standalone bash CLI batch converter |
| `run_converter.sh` | ~2 KB / 57 lines | 2026-04-05 | Launcher for Tkinter desktop app |
| `install.sh` | ~11 KB / 285 lines | 2026-04-05 | Installer / uninstaller |
| `logo.png` | 136 KB | 2026-03-27 | Original app logo (RGB, 840×958) |
| `logo_transparent.png` | 100 KB | 2026-04-05 | Background-stripped version used in title bar |
| `screenshot.png` | — | 2026-04-06 | App screenshot (used in GitHub README) |
| `README.md` | — | 2026-04-06 | GitHub repository README |
| `LICENSE` | — | 2026-04-05 | MIT License |
| `.gitignore` | — | 2026-04-05 | Git ignore rules |
| `PROJECT_SUMMARY.md` | — | 2026-04-06 | This file |
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
- **Two-pass encoding** support
- **Subtitle stream** detection and extraction (with per-stream format control)
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
- **Sound notification** on conversion completion (preview-able)
- **Preferences** auto-saved to JSON on dialog close — no manual save required
- Recent folders menu
- Keyboard shortcuts panel
- GPU auto-detection; presets auto-apply per codec
- **Custom logo** in title bar (`logo_transparent.png` at 32×32 px); falls back to 🎬 emoji if unavailable

### UI / UX Notes
- Title bar shows app name only — no working directory path displayed
- Settings menu has no "Save Preferences" item; preferences auto-save when the Default Settings dialog is closed via Save
- Preference saves are confirmed via a log entry only — no popup dialogs
- Window launches on the monitor containing the mouse pointer (no wrong-monitor flash)

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
| `-P`, `--gpu-preset` | GPU preset p1–p7 | `p1` |
| `-s`, `--suffix` | Output filename suffix | `-2mbps-UF_265` |
| `-o`, `--overwrite` | Overwrite existing output files | skip |
| `-c`, `--cleanup` | Delete originals after success | off |
| `-n`, `--no-log` | Disable log file | off |
| `-h`, `--help` | Show usage | — |

### Output Naming Convention
Input: `movie.mkv` → Output: `movie-2mbps-UF_265.mkv` (suffix varies by mode/preset)

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

### GPU Encoding (NVIDIA hevc_nvenc)
| Mode | Parameter | Recommended Range |
|------|-----------|-------------------|
| Bitrate | `-b:v` | 1M – 8M+ |
| CRF/CQ | `-cq` | 15–25 (lower = better quality) |

**Presets (fastest → best quality):** `p1` · `p2` · `p3` · `p4` · `p5` · `p6` · `p7`

> **Note:** GPU encoding is significantly faster but may produce slightly larger files at equivalent quality settings. Audio is always stream-copied by default.

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
/home/docman1967/scripts/video_converter/convert_videos.sh -g       # GPU fastest
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
| `zenity` | Bash CLI (optional) | `sudo apt install zenity` |
| NVIDIA driver + NVENC-enabled ffmpeg | GPU encoding (optional) | System-specific |

---

## Known Issues / Notes

1. **CUDA HW Decode compatibility** — Some source files (particularly older or oddly encoded ones) fail with error code -38 (`Function not implemented`) when Hardware Decode is enabled. Workaround: uncheck **HW Decode** for the affected file via per-file settings override, or disable it globally in Default Settings. The GPU still handles encoding; only decoding falls back to CPU.

2. **Audio handling** — Default audio codec is AC3 (Dolby Digital) at 320k. Can be changed per-file via settings override or globally in Default Settings.

3. **Subtitle handling** — The desktop GUI supports per-stream subtitle format control. Fixed bug where multiple subtitle streams (e.g. forced + full) were not all preserved correctly due to incorrect ffmpeg `-c:s` stream specifier usage.

---

## Change Log

### 2026-04-06
1. **Multi-subtitle stream bug fix** — Fixed ffmpeg command generation for files with more than one subtitle stream. Previously, only the first subtitle stream was correctly handled; subsequent streams had their codec specifier overwritten. Fixed by using per-output-stream specifiers (`-c:s:0`, `-c:s:1`, etc.) instead of a single `-c:s` flag.
2. **✅ Clear Finished button** — Added to the control bar. Removes all successfully completed (`✅`) and skipped (`⏭️`) files from the queue, leaving failed (`❌`) and pending files for retry. Useful when re-running failed files with different settings (e.g. disabling HW Decode after a CUDA error).

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
