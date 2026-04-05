# 🎬 Docflix Video Converter

A batch video converter that encodes files to **H.265/HEVC** using `ffmpeg`, with support for both CPU (`libx265`) and NVIDIA GPU (`hevc_nvenc`) encoding. Includes a full-featured desktop GUI and a headless CLI tool for scripted/automated use.

---

## Screenshots

![Docflix Video Converter](screenshot.png)

---

## Features

### Desktop GUI
- 🖱️ **Drag-and-drop** file queuing
- ⚙️ **Per-file settings overrides** — different encoder settings per file
- 🎞️ **Subtitle track** detection and extraction
- 📊 **Media info** panel — codec, resolution, duration, streams
- 🔬 **Test encode** — 30-second preview clip before full conversion
- 📐 **Estimated output size** before conversion starts
- ▶️ **Playback** of source and output files via configurable media player
- 📁 **Open output folder** in system file manager
- 🔁 **Two-pass encoding** support
- 🔔 **Sound notification** on completion (preview-able)
- 💾 **Auto-saved preferences** — encoder, quality, output folder, player, sounds
- 📂 **Recent folders** menu
- ⌨️ **Keyboard shortcuts** panel
- 🎮 **GPU auto-detection** with per-codec preset switching
- 🖥️ **Multi-monitor aware** — launches on the monitor containing the mouse

### CLI (`convert_videos.sh`)
- Batch converts all MKV files in the current directory
- CPU and GPU encoding modes
- Bitrate and CRF quality modes
- Configurable output filename suffix
- Optional cleanup of originals after successful conversion
- Timestamped log file per run
- Desktop notifications via `zenity` (optional)

---

## Requirements

| Dependency | Required By | Install |
|------------|-------------|---------|
| `ffmpeg` | Both | `sudo apt install ffmpeg` |
| `python3` | Desktop GUI | `sudo apt install python3` |
| `tkinter` | Desktop GUI | `sudo apt install python3-tk` |
| `tkinterdnd2` | Desktop GUI (drag & drop) | `pip install tkinterdnd2` |
| `Pillow` | Desktop GUI (logo image) | `pip install Pillow` |
| `zenity` | CLI notifications (optional) | `sudo apt install zenity` |
| NVIDIA driver + NVENC | GPU encoding (optional) | System-specific |

---

## Installation

### Recommended — use the installer

```bash
git clone https://github.com/docman1967/docflix-video-converter.git
cd docflix-video-converter
./install.sh
```

The installer will:
- Check and report any missing system dependencies
- Install Python packages (`tkinterdnd2`, `Pillow`) for your user
- Copy app files to `~/.local/share/docflix/`
- Create a `.desktop` entry so the app appears in your system app menu
- Create a `docflix` terminal command in `~/.local/bin/`

No `sudo` required.

### Uninstall

```bash
./install.sh --uninstall
```

---

## Running Without Installing

```bash
# Desktop GUI (background, with logging)
./run_converter.sh

# Desktop GUI (foreground)
python3 video_converter.py

# CLI — run from the folder containing your MKV files
cd /path/to/your/videos
/path/to/docflix-video-converter/convert_videos.sh
```

---

## CLI Usage

```
convert_videos.sh [OPTIONS]

Options:
  -b, --bitrate VALUE     Video bitrate (default: 2M)
  -q, --crf VALUE         CRF quality value — disables bitrate mode (0–51)
  -p, --preset PRESET     CPU encoding preset (default: ultrafast)
  -g, --gpu               Use NVIDIA GPU encoding (hevc_nvenc)
  -P, --gpu-preset P1-P7  GPU preset (default: p1)
  -s, --suffix SUFFIX     Output filename suffix (default: -2mbps-UF_265)
  -o, --overwrite         Overwrite existing output files
  -c, --cleanup           Delete originals after successful conversion
  -n, --no-log            Disable log file
  -h, --help              Show usage
```

### Examples

```bash
# CPU encoding, default bitrate (2M), ultrafast preset
./convert_videos.sh

# GPU encoding, fastest preset
./convert_videos.sh --gpu

# CRF quality mode (visually lossless)
./convert_videos.sh --crf 22

# GPU, high quality preset, overwrite existing files
./convert_videos.sh --gpu --gpu-preset p5 --overwrite
```

---

## Encoding Reference

### CPU (`libx265`)

| Mode | Flag | Recommended Range |
|------|------|-------------------|
| Bitrate | `-b:v` | `1M` – `8M`+ |
| CRF | `-crf` | `18`–`28` (lower = better quality) |

**Presets (fastest → best quality):**
`ultrafast` · `superfast` · `veryfast` · `faster` · `fast` · `medium` · `slow` · `slower` · `veryslow`

### GPU (`hevc_nvenc`)

| Mode | Flag | Recommended Range |
|------|------|-------------------|
| Bitrate | `-b:v` | `1M` – `8M`+ |
| CQ | `-cq` | `15`–`25` (lower = better quality) |

**Presets (fastest → best quality):** `p1` · `p2` · `p3` · `p4` · `p5` · `p6` · `p7`

> **Note:** GPU encoding is significantly faster but may produce slightly larger files at equivalent quality settings. Audio is always stream-copied (no re-encoding).

---

## Project Structure

```
docflix-video-converter/
├── video_converter.py    # Desktop GUI application (Tkinter)
├── convert_videos.sh     # Headless CLI batch converter
├── run_converter.sh      # Desktop GUI launcher (background + logging)
├── install.sh            # Installer / uninstaller
├── logo.png              # App icon
├── LICENSE               # MIT License
└── README.md             # This file
```

---

## License

[MIT](LICENSE) © 2026 Tony Davis
