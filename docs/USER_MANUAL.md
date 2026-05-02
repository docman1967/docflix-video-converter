# Docflix Media Suite — User Manual

**Version 2.0.5** | &copy; 2026 Tony Davis | MIT License

---

## Table of Contents

1. [Getting Started](#1-getting-started)
2. [Quick Start](#2-quick-start)
3. [Main Window](#3-main-window)
4. [Video Settings](#4-video-settings)
5. [Audio Settings](#5-audio-settings)
6. [Metadata & Tagging](#6-metadata--tagging)
7. [Subtitles](#7-subtitles)
8. [Tools](#8-tools)
9. [Per-File Overrides](#9-per-file-overrides)
10. [CLI Usage](#10-cli-usage)
11. [Keyboard Shortcuts](#11-keyboard-shortcuts)
12. [Preferences](#12-preferences)
13. [Troubleshooting](#13-troubleshooting)
14. [Encoding Reference](#14-encoding-reference)

---

## 1. Getting Started

### System Requirements

| Dependency | Required For | Install Command |
|---|---|---|
| `ffmpeg` | All features | `sudo apt install ffmpeg` |
| `python3` | Desktop GUI | `sudo apt install python3` |
| `python3-tk` | Desktop GUI | `sudo apt install python3-tk` |
| `tkinterdnd2` | Drag & drop | `pip install tkinterdnd2` |
| `Pillow` | App logo | `pip install Pillow` |

#### Optional Dependencies

| Dependency | Feature | Install Command |
|---|---|---|
| `zenity` | Native folder dialogs | `sudo apt install zenity` |
| `ccextractor` | Closed caption extraction from .ts | `sudo apt install ccextractor` |
| `tesseract-ocr` | Bitmap subtitle OCR | `sudo apt install tesseract-ocr tesseract-ocr-eng` |
| `pytesseract` | Python Tesseract bindings | `pip install pytesseract` |
| `pyspellchecker` | Subtitle spell checker | `pip install pyspellchecker` |
| `faster-whisper` | Smart Sync -- Standard engine | `pip install faster-whisper` |
| `whisperx` | Smart Sync -- Precise engine | `pip install whisperx 'transformers<4.45'` |
| `langdetect` | Subtitle language detection | `pip install langdetect` |
| `mpv` | Quick Sync video playback | `sudo apt install mpv` |

### Installation

```bash
git clone https://github.com/docman1967/docflix-video-converter.git
cd docflix-video-converter
./install.sh
```

The installer will:
- Check and report missing system dependencies
- Install Python packages (`tkinterdnd2`, `Pillow`) for your user
- Copy app files to `~/.local/share/docflix/`
- Create a `.desktop` entry (appears in your system app menu)
- Create a `docflix` terminal command in `~/.local/bin/`
- Create standalone tool commands: `docflix-subs`, `docflix-rename`, `docflix-media`

No `sudo` required. To uninstall: `./install.sh --uninstall`

### Launching the App

| Method | Command |
|---|---|
| App menu | Search "Docflix Media Suite" |
| Terminal | `docflix` |
| Open a file | `docflix /path/to/video.mkv` |
| From source | `python3 video_converter.py` |
| Background with logging | `./run_converter.sh` |

#### Standalone Tools

| Command | Tool |
|---|---|
| `docflix-subs` | Subtitle Editor |
| `docflix-rename` | TV Show Renamer |
| `docflix-media` | Media Processor |
| `docflix-scale` | Video Scaler |

---

## 2. Quick Start

1. **Launch** the app via the system menu, terminal (`docflix`), or by running `python3 video_converter.py`
2. **Add files** -- drag and drop video files onto the window, or use `Ctrl+O` to open files / `Ctrl+Shift+O` to open a folder
3. **Choose settings** -- select your encoder (CPU or GPU), video codec, quality mode, and container format
4. **Click "Start Conversion"** -- the app will convert all queued files
5. **Monitor progress** -- a progress bar and log show real-time encoding status with batch ETA

> **Tip:** For a quick test, select a file and press `Ctrl+T` to encode just the first 30 seconds.

---

## 3. Main Window

### Menu Bar

| Menu | Key Items |
|---|---|
| **File** | Open File(s), Open Folder, Exit |
| **Settings** | Default Settings, Reset to Defaults |
| **View** | Show/Hide Log, Show/Hide Settings |
| **Tools** | Play Source/Output, Media Details, Enhanced Media Details, Test Encode, Open Output Folder, Media Processor, Subtitle Editor, TV Show Renamer |
| **Help** | User Manual, Keyboard Shortcuts, About |

### Encoder Selection

The encoder dropdown (top-right) shows all detected backends:

| Encoder | Hardware | Speed | Quality |
|---|---|---|---|
| **CPU** | Any CPU | Slowest | Best at equivalent settings |
| **NVIDIA (NVENC)** | NVIDIA GPU | Very fast | Excellent |
| **Intel (QSV)** | Intel GPU/iGPU | Fast | Good |
| **AMD / VAAPI** | AMD GPU | Fast | Good |

> **Note:** The app auto-detects available GPU backends at startup using a test encode. Only working backends are shown.

### Settings Panel

Toggle with `Ctrl+Shift+S`. Contains:

- **Video Codec** -- H.265/HEVC, H.264/AVC, AV1, VP9, MPEG-4, ProRes, Copy
- **Container** -- .mkv, .mp4, .ts
- **Transcode Mode** -- Video Only, Audio Only, Both
- **Quality Mode** -- Bitrate (fixed size) or CRF (constant quality)
- **Bitrate slider** -- 1M to 16M with quick-set buttons
- **CRF value** -- lower = better quality
- **Preset** -- speed/quality trade-off
- **Audio** -- codec and bitrate dropdowns
- **Options** -- Skip existing, Delete originals, HW Decode, Two-pass, Verify output
- **Metadata** -- Strip chapters, Strip tags, Set track metadata
- **Edition** -- Tag videos with version info
- **Chapters** -- Auto-generate chapter markers

### File Queue

- **Add files** -- drag and drop, `Ctrl+O` (files), `Ctrl+Shift+O` (folder)
- **Remove files** -- select and press `Delete`, or right-click > Remove
- **Double-click** -- opens Internal Subtitles dialog
- **Right-click** -- context menu with play, overrides, subtitles, Enhanced Media Details

#### Queue Indicators

| Icon | Meaning |
|---|---|
| :gear: | Per-file settings override applied |
| :paperclip: | External subtitles attached |
| CC | Closed captions detected (MPEG-TS) |

### Starting a Conversion

1. Add files to the queue
2. Adjust settings (or use per-file overrides)
3. Click **Start Conversion**
4. Use **Pause** / **Stop** to control
5. Sound notification on completion
6. Click **Clear Finished** to remove completed files

> **Tip:** The **Batch ETA** shows estimated time remaining for all files.

---

## 4. Video Settings

### Video Codecs

| Codec | CPU Encoder | Best For |
|---|---|---|
| **H.265 / HEVC** | libx265 | General use -- best compression |
| **H.264 / AVC** | libx264 | Maximum compatibility |
| **AV1** | libsvtav1 | Next-gen, best compression, slower |
| **VP9** | libvpx-vp9 | WebM, web streaming |
| **MPEG-4** | mpeg4 | Legacy compatibility |
| **ProRes** | prores_ks | Professional editing, Apple |
| **Copy** | copy | No re-encoding, just remux |

### Quality Modes

#### Bitrate Mode (Fixed Size)
Sets a target bitrate. Output file size is predictable. Slider range: 1M-16M.

#### CRF Mode (Constant Quality)
Maintains consistent visual quality. File size varies by content.

| CRF | Quality | File Size |
|---|---|---|
| 18 | Visually lossless | Large |
| 23 | Good (default) | Medium |
| 28 | Acceptable | Small |
| 35+ | Noticeable loss | Very small |

### Presets

**CPU (libx265/libx264):** `ultrafast` > `superfast` > `veryfast` > `faster` > `fast` > `medium` > `slow` > `slower` > `veryslow`

**NVIDIA NVENC:** `p1` (fastest) > `p4` (default) > `p7` (best quality)

**Intel QSV:** `veryfast` > `medium` (default) > `veryslow`

### Two-Pass Encoding

Analyzes video in first pass, optimizes in second. Better quality at a given bitrate. Roughly doubles encoding time.

### Hardware Decode

Offloads decoding to GPU. Auto-disabled for burn-in subtitles. Uncheck if you encounter errors with certain source files.

### Container Formats

| Container | Subtitle Support | Notes |
|---|---|---|
| **.mkv** | All formats | Most flexible, recommended |
| **.mp4** | mov_text only | Best device compatibility |
| **.ts** | DVB only | Transport stream format |

---

## 5. Audio Settings

Available when Transcode Mode is "Audio Only" or "Both".

| Setting | Options | Default |
|---|---|---|
| **Audio Codec** | AAC, AC3, EAC3, MP3, Opus, FLAC, Copy | AAC |
| **Audio Bitrate** | 32k - 640k | 128k |

> **Tip:** Use "Copy" to keep original audio without re-encoding.

---

## 6. Metadata & Tagging

### Strip Chapters / Strip Tags

- **Strip chapters** -- removes all chapter markers (`-map_chapters -1`)
- **Strip tags** -- removes all global metadata (`-map_metadata -1`)

### Set Track Metadata

Sets language codes and clears track names/container title.

| Track | Default | Purpose |
|---|---|---|
| Video (V:) | `und` | Undetermined |
| Audio (A:) | `eng` | English |
| Subtitle (S:) | `eng` | English |

### Edition Tagging

Tag videos with version info written to the container `title` metadata.

**Presets:** Theatrical, Director's Cut, Extended, Extended Director's Cut, Unrated, Special Edition, IMAX, Criterion, Remastered, Anniversary Edition, Ultimate Edition, Custom...

**Plex filename tag:** When "Add to filename (Plex)" is checked, output includes `{edition-Director's Cut}` for Plex edition detection.

Example: `Superman {edition-Director's Cut}-2M-H265_ultrafast.mkv`

### Add Chapters

Auto-generate chapter markers at a configurable interval (1-60 minutes, default 5).

"Add chapters" and "Strip chapters" are mutually exclusive.

---

## 7. Subtitles

### Internal Subtitles

Double-click a file (or right-click > Internal Subtitles) to manage embedded tracks:
- Keep or drop tracks
- Convert formats (SRT, ASS, WebVTT, TTML)
- Extract to standalone files
- Edit in the subtitle editor
- Bitmap subs (PGS/VobSub) can be OCR'd to text

### External Subtitles

- **Drag and drop** `.srt`, `.ass`, `.ssa`, `.vtt`, `.sub`, `.idx`, `.sup` files
- **Auto-matching** by filename stem
- **Auto-detection** of language, forced, SDH flags from filename
- **Embed** (selectable stream) or **Burn-in** (permanent)

### Subtitle Editor

Full-featured editor (Tools > Subtitle Editor or edit any subtitle track):

- **Inline editing** -- double-click cells
- **Filters** -- Remove HI, Remove Tags, Remove Ads, Remove Stray Notes, Remove Leading Dashes, Remove ALL CAPS HI, Remove Off-Screen Quotes, Remove Duplicates, Merge Short Cues, Reduce to 2 Lines, Fix ALL CAPS
- **Search & Replace** with wrap-around
- **Search/Replace List** -- persistent correction pairs
- **Timing tools** -- offset and stretch
- **Split / Join / Insert** cues
- **Undo/Redo** (`Ctrl+Z` / `Ctrl+Y`)
- **Video preview** via ffplay
- **Color-coded rows** -- yellow=modified, blue=HI, pink=tags, orange=long lines, green=search match
- **Save to Video** -- re-mux edited subtitle back without re-encoding
- **Video subtitle editing** -- drag a video to extract, edit, and re-mux an internal stream

### Smart Sync (Whisper-based Auto-Sync)

Timing > Smart Sync in the subtitle editor.

| Engine | Accuracy | Speed | Size |
|---|---|---|---|
| **Standard** (faster-whisper) | ~400ms | Fast (CPU) | ~200MB |
| **Precise** (WhisperX) | ~50ms | Faster with GPU | ~2GB |

**Scan modes:** Quick Scan (sampled), Full Scan (entire audio), Direct Align (WhisperX only)

**Apply methods:** Apply Sync (global offset), Re-time All (per-cue interpolation)

### Bitmap Subtitle OCR

Convert PGS/VobSub bitmap subs to SRT via Tesseract:
- Single-pass rendering (~2 min for 1-hour episode)
- Parallel OCR across CPU cores
- Smart cropping (~13x fewer pixels)
- Live monitor with progress and preview
- Comprehensive OCR post-processing

### Spell Checker

`F7` or Tools > Spell Check. Interactive dialog with Replace, Replace All, Skip, Ignore, Add to Dict, Add as Name. Custom dictionary saved to preferences.

### Batch Filter

Tools > Batch Filter. Process multiple subtitle files at once with filter checkboxes and batch Search & Replace.

---

## 8. Tools

### Media Processor

Remux-only post-processing (Tools > Media Processor, `Ctrl+M`). No re-encoding (`-c:v copy`).

**Operations:**
- Convert audio (AAC, AC3, EAC3, MP3, Opus, FLAC, Copy) + bitrate
- Strip chapters / tags / existing subtitles
- Mux external subtitles (auto-detects `*.eng.srt` alongside videos)
- Set track metadata with language codes
- Edition tagging (same presets as main converter)
- Add chapters (auto-generate at intervals)
- Parallel processing (multi-threaded)
- Output: replace in-place or save to folder
- Per-file overrides via right-click

### TV Show Renamer

Batch rename TV/movie files using TVDB/TMDB (Tools > TV Show Renamer):
- Auto-detects show/movie names from filenames
- Disambiguation dialog with poster thumbnails
- Movie support (`Name (Year).ext`)
- Multi-episode (`S01E01E02`, `S01E01-E03`)
- Date-based episodes for daily shows
- Subtitle tag preservation
- Undo rename (`Ctrl+Z`)
- Configurable filename template with flat and folder presets

#### Folder Templates

Use `/` in the filename template to automatically create folder hierarchies during rename. For example:

| Template | Result |
|---|---|
| `{show} S{season}E{episode} {title}` | `Breaking Bad S01E01 Pilot.mkv` |
| `{show}/Season {season}/{show} S{season}E{episode} {title}` | `Breaking Bad/Season 01/Breaking Bad S01E01 Pilot.mkv` |
| `{show}/S{season}/{show} S{season}E{episode} {title}` | `Breaking Bad/S01/Breaking Bad S01E01 Pilot.mkv` |

Folders are created relative to the source file's location. Undo will move files back and clean up any empty folders that were created.

### Enhanced Media Details

Comprehensive file analysis (right-click > Enhanced Media Details, `Ctrl+Shift+I`):

| Tab | Information |
|---|---|
| **General** | Format, duration, size, bitrate, title/edition |
| **Video** | Codec/profile/level, resolution/SAR/DAR, fps, scan type, bit depth, color space, HDR (HDR10/HLG/DV), mastering display, content light level |
| **Audio** | Codec/profile, sample rate, channels, layout, bitrate, language |
| **Subtitles** | Codec, events, resolution (bitmap), disposition |
| **Chapters** | Full listing with timestamps and titles |
| **Attachments** | Fonts, images, MIME types |
| **Metadata** | All container tags |
| **Full Report** | All sections combined |

Copy to Clipboard and Copy Full Report buttons included.

### Video Scaler

Batch resize video files (Tools > Video Scaler, `Ctrl+Shift+R`). Also available as `docflix-scale` standalone command.

**Resolution Presets:** Original, 2160p (4K), 1440p (2K), 1080p, 720p, 480p, Custom WxH

**Features:**
- GPU-accelerated scaling (NVENC, QSV, VAAPI)
- Aspect ratio preservation -- width auto-calculated from actual decoded content
- Upscale warning -- flags files where target exceeds source
- Smart probing -- extracts frame at 30% of duration for actual content size
- Encoder selection with preset and CRF controls
- Audio passthrough (copy) or re-encode
- Real-time progress bar with ETA
- Drag-and-drop, settings saved to preferences

### Test Encode

`Ctrl+T` -- encode first 30 seconds with current settings for preview.

---

## 9. Per-File Overrides

Right-click > Override Settings for individual file settings:
- Encoder, video codec, quality mode, bitrate/CRF, preset
- Audio codec and bitrate
- Skip existing, delete originals, HW decode
- Strip chapters, strip tags, set track metadata
- Edition tag and Plex filename option

Files with overrides show a :gear: icon. Double-click to edit.

---

## 10. CLI Usage

```bash
# CPU encoding, default settings
./convert_videos.sh

# NVIDIA GPU
./convert_videos.sh --gpu

# Intel QSV
./convert_videos.sh --qsv

# AMD VAAPI
./convert_videos.sh --vaapi

# CRF quality mode
./convert_videos.sh --crf 22

# GPU, high quality, overwrite
./convert_videos.sh --gpu --gpu-preset p5 --overwrite
```

| Flag | Description | Default |
|---|---|---|
| `-b, --bitrate` | Video bitrate | `2M` |
| `-q, --crf` | CRF quality (disables bitrate) | disabled |
| `-p, --preset` | CPU encoding preset | `ultrafast` |
| `-g, --gpu` | Use NVIDIA GPU | off |
| `--qsv` | Use Intel QSV | off |
| `--vaapi` | Use VAAPI encoding | off |
| `-P, --gpu-preset` | GPU preset | varies |
| `-s, --suffix` | Output filename suffix | `-2mbps-UF_265` |
| `-o, --overwrite` | Overwrite existing | skip |
| `-c, --cleanup` | Delete originals | off |
| `-n, --no-log` | Disable log file | off |

---

## 11. Keyboard Shortcuts

| Shortcut | Action |
|---|---|
| `Ctrl+O` | Open File(s) |
| `Ctrl+Shift+O` | Open Folder |
| `Ctrl+P` | Play Source File |
| `Ctrl+Shift+P` | Play Output File |
| `Ctrl+I` | Media Details |
| `Ctrl+Shift+I` | Enhanced Media Details |
| `Ctrl+T` | Test Encode (30s) |
| `Ctrl+M` | Media Processor |
| `Ctrl+Shift+R` | Video Scaler |
| `Ctrl+Shift+F` | Open Output Folder |
| `Ctrl+L` | Show/Hide Log |
| `Ctrl+Shift+S` | Show/Hide Settings Panel |
| `F1` | Keyboard Shortcuts |
| `Ctrl+Q` | Exit |
| `Delete` | Remove selected file |

### Subtitle Editor

| Shortcut | Action |
|---|---|
| `Ctrl+Z` | Undo |
| `Ctrl+Y` | Redo |
| `Ctrl+F` | Find |
| `Ctrl+H` | Find & Replace |
| `F7` | Spell Check |
| `Ctrl+S` | Save |
| `Ctrl+Enter` | Save inline edit |
| `Escape` | Cancel inline edit |

---

## 12. Preferences

Auto-saved to `~/.local/share/docflix/preferences.json` when Default Settings dialog is closed.

**Saved:** Encoder, codec, container, quality, CRF, presets, audio, all checkboxes, metadata options, edition tag, chapter settings, player preference, recent folders, custom patterns, dictionaries, API keys, Media Processor settings.

**Not saved (intentionally):**
- **Video bitrate** -- resets to 2M on every launch
- **Transcode mode** -- always starts as Video Only

Reset: Settings > Reset to Defaults, or delete `preferences.json`.

---

## 13. Troubleshooting

### GPU encoding fails
- Try unchecking **HW Decode**
- Ensure GPU drivers are up to date
- Check ffmpeg GPU encoder support

### Small or empty output files
- Check the log for errors
- Try a different encoder (CPU)
- Try a different container
- Verify source file is not corrupted

### Subtitles not appearing
- **MPEG-TS output** -- text subtitles not supported, only DVB
- **MP4 output** -- only `mov_text` supported (auto-converted)
- Check "Strip existing subtitle tracks" is not enabled

### App appears tiny on high-DPI monitor
- Set `GDK_SCALE=2` before launching
- Or set `Xft.dpi: 192` in `~/.Xresources`

### Drag and drop not working
- Install `tkinterdnd2`: `pip install tkinterdnd2`

### Zenity folder dialogs not appearing
- Install zenity: `sudo apt install zenity`

---

## 14. Encoding Reference

### CPU (libx265)

| Mode | Flag | Recommended Range |
|---|---|---|
| Bitrate | `-b:v` | 1M - 8M+ |
| CRF | `-crf` | 18-28 (lower = better) |

### NVIDIA GPU (hevc_nvenc)

| Mode | Flag | Recommended Range |
|---|---|---|
| Bitrate | `-b:v` | 1M - 8M+ |
| CQ | `-cq` | 15-25 (lower = better) |

### Intel GPU (hevc_qsv)

| Mode | Flag | Recommended Range |
|---|---|---|
| Bitrate | `-b:v` | 1M - 8M+ |
| Quality | `-global_quality` | 15-25 (lower = better) |

### AMD GPU (hevc_vaapi)

| Mode | Flag | Recommended Range |
|---|---|---|
| Bitrate | `-b:v` | 1M - 8M+ |
| Quality | `-qp` | 15-25 (lower = better) |

> **Note:** GPU encoding is significantly faster but may produce slightly larger files at equivalent quality. The quality difference is minimal with modern GPU encoders.

---

*Docflix Media Suite v2.0.5 -- &copy; 2026 Tony Davis -- MIT License*
