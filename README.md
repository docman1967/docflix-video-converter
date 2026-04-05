# 🎬 Docflix Video Converter

A desktop application for batch converting video files to H.265/HEVC format using ffmpeg.  
Supports both CPU (libx265) and NVIDIA GPU (NVENC) encoding.

![App Logo](logo.png)

---

## Features

- **Drag-and-drop** file queuing
- **CPU and GPU encoding** — libx265 or NVIDIA NVENC
- **Bitrate and CRF** quality modes
- **Per-file settings overrides** — different encoder settings per file
- **Two-pass encoding** support
- **Subtitle track** detection and extraction
- **Estimated output size** before conversion starts
- **Media info** panel — codec, resolution, duration, streams
- **Test encode** — 30-second preview clip of your settings
- **Source and output file playback** via configurable media player
- **Sound notification** on completion
- Sortable, reorderable file queue
- Collapsible settings panel and detachable log window
- GPU auto-detection with per-codec presets
- Preferences auto-saved — no manual save required
- Also includes a **bash CLI** (`convert_videos.sh`) for headless/scripted use

---

## Requirements

| Dependency | Purpose | Install |
|------------|---------|---------|
| `python3` | Runtime | `sudo apt install python3` |
| `tkinter` | GUI toolkit | `sudo apt install python3-tk` |
| `ffmpeg` | Video encoding | `sudo apt install ffmpeg` |
| `pip3` | Python packages | `sudo apt install python3-pip` |
| `tkinterdnd2` | Drag & drop | installed by `install.sh` |
| `Pillow` | Logo image | installed by `install.sh` |
| NVIDIA driver + NVENC ffmpeg | GPU encoding (optional) | system-specific |

---

## Installation

```bash
git clone https://github.com/docman1967/docflix-video-converter.git
cd docflix-video-converter
./install.sh
```

The installer will:
- Verify system dependencies and advise on anything missing
- Install required Python packages (`tkinterdnd2`, `Pillow`)
- Copy app files to `~/.local/share/docflix/`
- Add **Docflix Video Converter** to your system app menu
- Create a `docflix` terminal command

### Launch

After installing, launch any of these ways:
- Search your app menu for **Docflix Video Converter**
- Run `docflix` in a terminal

### Uninstall

```bash
./install.sh --uninstall
```

---

## Manual Launch (without installing)

```bash
# Clone and run directly
git clone https://github.com/docman1967/docflix-video-converter.git
cd docflix-video-converter
python3 video_converter.py
```

---

## Bash CLI

For headless or scripted batch conversion, use `convert_videos.sh` from the folder containing your video files:

```bash
cd /path/to/your/videos

# CPU encoding — default settings
/path/to/convert_videos.sh

# GPU encoding (NVIDIA)
/path/to/convert_videos.sh -g

# CRF quality mode
/path/to/convert_videos.sh -q 22

# Custom bitrate
/path/to/convert_videos.sh -b 4M
```

| Flag | Description | Default |
|------|-------------|---------|
| `-b`, `--bitrate` | Video bitrate | `2M` |
| `-q`, `--crf` | CRF quality (disables bitrate mode) | off |
| `-p`, `--preset` | CPU preset | `ultrafast` |
| `-g`, `--gpu` | Use NVIDIA GPU | off |
| `-P`, `--gpu-preset` | GPU preset (p1–p7) | `p1` |
| `-s`, `--suffix` | Output filename suffix | `-2mbps-UF_265` |
| `-o`, `--overwrite` | Overwrite existing files | skip |
| `-c`, `--cleanup` | Delete originals after success | off |
| `-h`, `--help` | Show usage | — |

---

## Encoding Reference

### CPU (libx265)
| Mode | Parameter | Recommended Range |
|------|-----------|-------------------|
| Bitrate | `-b:v` | 1M – 8M+ |
| CRF | `-crf` | 18–28 (lower = better quality) |

**Presets (fastest → best quality):**  
`ultrafast` · `superfast` · `veryfast` · `faster` · `fast` · `medium` · `slow` · `slower` · `veryslow`

### GPU (hevc_nvenc)
| Mode | Parameter | Recommended Range |
|------|-----------|-------------------|
| Bitrate | `-b:v` | 1M – 8M+ |
| CRF/CQ | `-cq` | 15–25 (lower = better quality) |

**Presets (fastest → best quality):** `p1` · `p2` · `p3` · `p4` · `p5` · `p6` · `p7`

---

## License

MIT License — see [LICENSE](LICENSE) for details.
