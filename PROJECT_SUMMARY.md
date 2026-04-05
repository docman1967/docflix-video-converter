# Video Converter — Project Summary

**Last Updated:** 2026-04-05 14:45  
**Location:** `/home/docman1967/video_converter/`  
**Purpose:** Batch convert MKV video files to H.265/HEVC format using ffmpeg, with support for both CPU (libx265) and NVIDIA GPU (NVENC) encoding.

---

## File Inventory

| File | Size | Modified | Description |
|------|------|----------|-------------|
| `video_converter.py` | ~169 KB / 3,804 lines | 2026-04-05 | Primary Tkinter desktop GUI app |
| `video_converter_gui.html` | ~62 KB / 1,758 lines | 2026-03-28 | Web-based GUI frontend |
| `video_converter_server.py` | ~16 KB / 449 lines | 2026-03-28 | HTTP REST API backend for web GUI |
| `convert_videos.sh` | ~16 KB / 455 lines | 2026-03-28 | Standalone bash CLI batch converter |
| `launch_gui.sh` | ~3 KB / 98 lines | 2026-03-28 | Launcher for web GUI (server + browser) |
| `run_converter.sh` | ~2 KB / 57 lines | 2026-04-05 | Launcher for Tkinter desktop app |
| `logo.png` | 136 KB | 2026-03-27 | Original app logo (RGB, 840×958) |
| `logo_transparent.png` | 100 KB | 2026-04-05 | Background-stripped version used in title bar |
| `PROJECT_SUMMARY.md` | — | 2026-04-05 | This file |
| `logs/` | dir | 2026-04-05 | Timestamped launch logs (auto-pruned to 10) |

---

## Architecture

```
┌──────────────────────────────────────────────────────┐
│                  User Interfaces (3)                 │
│                                                      │
│  convert_videos.sh       run_converter.sh            │
│  (Bash CLI — headless)   (launches ↓)                │
│                          video_converter.py          │
│                          (Tkinter Desktop GUI)       │
│                                                      │
│  launch_gui.sh  ──────►  video_converter_server.py  │
│  (launcher)              (REST API on port 8765)     │
│                          ▲                           │
│                          │ HTTP                      │
│                          video_converter_gui.html    │
│                          (Web GUI / browser)         │
└──────────────────────────────────────────────────────┘
                  All interfaces call ffmpeg
```

---

## Interface 1: Tkinter Desktop GUI (`video_converter.py`)

The primary and most feature-complete interface. Launched via `run_converter.sh` or directly with `python3 video_converter.py`.

**Dependencies:** Python 3, `tkinter`, `tkinterdnd2`, `Pillow`, `ffmpeg`

### Core Classes
- **`VideoConverter`** — Conversion engine; builds and runs ffmpeg commands, supports pause/resume/stop via threading.
- **`VideoConverterApp`** — Full Tkinter UI with all user-facing features.

### Key Features
- Drag-and-drop file queuing
- Per-file **settings override** (different encoder settings per file)
- **Two-pass encoding** support
- **Subtitle stream** detection and extraction
- **Estimated output size** calculation before conversion
- **Media info** panel (shows codec, resolution, duration, streams)
- **Test encode** (short clip preview of settings)
- Source file and output file **playback** via configurable media player:
  - **System Default** — delegates to `xdg-open` (uses whatever the OS is set to open video files)
  - **auto** — tries common players in order: vlc → mpv → totem → ffplay
  - Named player (vlc, mpv, etc.) — uses that specific player if installed
  - **Custom path** — full path to any executable
- Open output folder in system file manager
- Sortable, reorderable file queue
- Collapsible settings panel and detachable log window
- **Sound notification** on conversion completion (preview-able)
- **Preferences** auto-saved to JSON on dialog close (encoder, quality, output folder, player, sounds, etc.) — no manual save required
- Recent folders menu
- Keyboard shortcuts panel
- GPU auto-detection; presets auto-apply per codec
- **Custom logo** in title bar (`logo_transparent.png` at 32×32 px); falls back to 🎬 emoji if image unavailable

### UI / UX Notes
- Title bar shows app name only — no working directory path displayed
- Settings menu has no "Save Preferences" item; preferences auto-save when the Default Settings dialog is closed via Save
- Preference saves are confirmed via a log entry only — no popup dialogs
- Window launches on the monitor containing the mouse pointer (no wrong-monitor flash)

---

## Interface 2: Web GUI (`video_converter_gui.html` + `video_converter_server.py`)

A browser-based interface launched via `launch_gui.sh`. The server runs on **port 8765** and serves the HTML file as its root.

**Dependencies:** Python 3, `ffmpeg` (server); any modern browser (GUI)

### Server API Endpoints (`video_converter_server.py`)

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/` | GET | Serve the HTML GUI |
| `/state` | GET | Live conversion progress, file list, logs |
| `/settings` | GET | GPU availability, working directory |
| `/files` | GET | MKV files in current working directory |
| `/start` | POST | Begin conversion with provided settings |
| `/pause` | POST | Toggle pause/resume |
| `/stop` | POST | Stop conversion |
| `/set-directory` | POST | Change working directory |
| `/list-folders` | POST | Folder browser navigation |
| `/open-folder` | POST | Open directory in system file manager |
| `/logs` | GET | Poll conversion log messages |

### Web GUI Features
- CPU/GPU toggle, bitrate slider (1–20M), CRF slider (0–51)
- Preset selector (dynamically switches between CPU/GPU preset lists)
- File list with status indicators
- Real-time progress bar, FPS display, elapsed time, ETA
- Polled log console with auto-scroll
- Folder browser modal (navigable filesystem tree)
- Settings persistence via `localStorage`
- Browser desktop notifications
- **Demo/standalone mode** when no backend is running (simulates conversion)

---

## Interface 3: Bash CLI (`convert_videos.sh`)

Headless batch converter; runs in the **current directory** and converts all `.mkv` files found there. Best for servers or scripted/automated use.

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

```bash
# Launch the app
./run_converter.sh

# Follow the log
tail -f logs/video_converter_YYYYMMDD_HHMMSS.log
```

---

## Encoding Options (All Interfaces)

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

> **Note:** GPU encoding is significantly faster but may produce slightly larger files at equivalent quality settings.

---

## Quick Start

```bash
# Option 1: Tkinter Desktop GUI (background, with logging)
cd /home/docman1967/video_converter
./run_converter.sh

# Option 2: Web GUI
./launch_gui.sh
# Opens http://localhost:8765 in default browser

# Option 3: Bash CLI (run from the folder containing your MKV files)
cd /path/to/your/videos
/home/docman1967/video_converter/convert_videos.sh          # CPU defaults
/home/docman1967/video_converter/convert_videos.sh -g       # GPU fastest
/home/docman1967/video_converter/convert_videos.sh -q 22    # CRF quality mode
```

---

## Pending / Next Steps

1. **Install process** — An `install.sh` script is planned to:
   - Check and install system dependencies (`python3`, `python3-tk`, `ffmpeg`, `pip`)
   - Install Python packages (`tkinterdnd2`, `Pillow`)
   - Copy files to install directory (user-local `~/.local/share/docflix` preferred over system-wide `/opt`)
   - Create a `.desktop` file for the system app launcher/menu with `logo.png` as the icon
   - Create a `~/.local/bin/docflix` symlink for terminal launch
   - Optional `--uninstall` flag for clean removal
   - Handle re-runs gracefully (update in place)

2. **GitHub repository** — Project is ready to be published. Steps needed:
   - Create repo at github.com (suggested name: `docflix-video-converter`)
   - Create `.gitignore` (exclude `__pycache__/`, `logs/`, `*.pyc`, `preferences.json`, `logo_transparent.png`)
   - Convert or supplement `PROJECT_SUMMARY.md` into a `README.md` with screenshots and install instructions
   - Choose a license (MIT recommended for open source)
   - `git init` → `git add .` → initial commit → push to remote

---

## Known Issues / Notes

1. **Web GUI — `browseToHome()` bug:** Uses `process.env?.HOME` (a Node.js-ism) which is not available in browser JavaScript. Falls back to `/home` hardcoded. Fix: replace with a `/settings` API call to get the server's home directory.

2. **Web GUI — demo mode FPS/ETA:** When running without the backend, FPS and ETA are randomly simulated values, not real encoding stats.

3. **Tkinter app is the most actively developed** (last modified 2026-04-05 vs. 2026-03-28 for all others). It has the most features and should be considered the canonical interface.

4. **Audio handling:** All interfaces default to `copy` (stream copy) for audio — no re-encoding unless explicitly configured.

5. **Subtitle handling:** The Tkinter GUI supports subtitle stream detection and extraction; the bash script and web GUI do not currently expose subtitle options.

---

## Dependencies Summary

| Dependency | Required By | Install |
|------------|-------------|---------|
| `ffmpeg` | All interfaces | `sudo apt install ffmpeg` |
| `python3` | Tkinter app, Web GUI | `sudo apt install python3` |
| `tkinter` | Tkinter app | `sudo apt install python3-tk` |
| `tkinterdnd2` | Tkinter app (drag & drop) | `pip install tkinterdnd2` |
| `Pillow` | Tkinter app (logo image) | `pip install Pillow` |
| `zenity` | Bash CLI (optional) | `sudo apt install zenity` |
| Any browser | Web GUI | — |
| NVIDIA driver + NVENC-enabled ffmpeg | GPU encoding | System-specific |

---

## Change Log

### 2026-04-05
1. **System Default player** — Added `"System Default"` to the video player dropdown in Default Settings. Uses `xdg-open` to open files with whatever the OS is configured to use. Falls back to `auto` if `xdg-open` is unavailable.
2. **Removed "Settings Saved" popup** — Preferences save silently; result is written to the log panel only. Dialog closes immediately on save.
3. **Removed "Save Preferences" from Settings menu** — Removed menu item, `Ctrl+S` key binding, and its entry in the Keyboard Shortcuts help dialog. Preferences auto-save when the Default Settings dialog is closed via Save.
4. **Removed path label from title bar** — The dynamic working-directory path displayed next to the app title has been removed. Title shows app name only.
5. **Custom logo in title bar** — `logo.png` background stripped to transparent (`logo_transparent.png`) and displayed at 32×32 px using PIL/ImageTk. Falls back to 🎬 emoji if image or PIL is unavailable.
6. **Multi-monitor launch fix** — Window now launches on the monitor containing the mouse pointer. Uses `withdraw()` during build and `deiconify()` after positioning to eliminate the wrong-monitor flash.
7. **Background launcher with logging** — `run_converter.sh` updated to launch via `nohup ... &`, log stdout/stderr to a timestamped file in `logs/`, print the PID and `tail -f` command, and auto-prune logs to the 10 most recent.
