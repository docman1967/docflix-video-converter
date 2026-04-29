"""
Docflix Video Converter — Built-in Manual Viewer

Native Tkinter manual viewer with sidebar navigation and
formatted text display. No external browser or dependencies.
"""

import tkinter as tk
from tkinter import ttk


# ═══════════════════════════════════════════════════════════════════
# Manual content — structured as sections with formatted text
# ═══════════════════════════════════════════════════════════════════

# Each section: (title, content_lines)
# Content lines are tuples: (tag, text)
#   tag: 'h2', 'h3', 'h4', 'p', 'bullet', 'tip', 'warning', 'note',
#        'code', 'table_header', 'table_row', 'sep'

MANUAL_SECTIONS = [
    ("Getting Started", [
        ("h2", "1. Getting Started"),
        ("h3", "System Requirements"),
        ("table_header", "Dependency|Required For|Install Command"),
        ("table_row", "ffmpeg|All features|sudo apt install ffmpeg"),
        ("table_row", "python3|Desktop GUI|sudo apt install python3"),
        ("table_row", "python3-tk|Desktop GUI|sudo apt install python3-tk"),
        ("table_row", "tkinterdnd2|Drag & drop|pip install tkinterdnd2"),
        ("table_row", "Pillow|App logo|pip install Pillow"),
        ("p", ""),
        ("h4", "Optional Dependencies"),
        ("table_header", "Dependency|Feature|Install Command"),
        ("table_row", "zenity|Native folder dialogs|sudo apt install zenity"),
        ("table_row", "ccextractor|CC extraction from .ts|sudo apt install ccextractor"),
        ("table_row", "tesseract-ocr|Bitmap subtitle OCR|sudo apt install tesseract-ocr tesseract-ocr-eng"),
        ("table_row", "pytesseract|Python Tesseract bindings|pip install pytesseract"),
        ("table_row", "pyspellchecker|Subtitle spell checker|pip install pyspellchecker"),
        ("table_row", "faster-whisper|Smart Sync (Standard)|pip install faster-whisper"),
        ("table_row", "whisperx|Smart Sync (Precise)|pip install whisperx 'transformers<4.45'"),
        ("table_row", "langdetect|Subtitle language detection|pip install langdetect"),
        ("table_row", "mpv|Quick Sync video playback|sudo apt install mpv"),
        ("p", ""),
        ("h3", "Installation"),
        ("code", "git clone https://github.com/docman1967/docflix-video-converter.git"),
        ("code", "cd docflix-video-converter"),
        ("code", "./install.sh"),
        ("p", ""),
        ("p", "The installer will:"),
        ("bullet", "Check and report missing system dependencies"),
        ("bullet", "Install Python packages (tkinterdnd2, Pillow) for your user"),
        ("bullet", "Copy app files to ~/.local/share/docflix/"),
        ("bullet", "Create a .desktop entry (appears in your system app menu)"),
        ("bullet", "Create a docflix terminal command in ~/.local/bin/"),
        ("bullet", "Create standalone tool commands: docflix-subs, docflix-rename, docflix-media"),
        ("p", ""),
        ("p", "No sudo required. To uninstall: ./install.sh --uninstall"),
        ("p", ""),
        ("h3", "Launching the App"),
        ("table_header", "Method|Command"),
        ("table_row", "App menu|Search \"Docflix Video Converter\""),
        ("table_row", "Terminal|docflix"),
        ("table_row", "Open a file|docflix /path/to/video.mkv"),
        ("table_row", "From source|python3 video_converter.py"),
        ("table_row", "Background + logging|./run_converter.sh"),
        ("p", ""),
        ("h4", "Standalone Tools"),
        ("table_header", "Command|Tool"),
        ("table_row", "docflix-subs|Subtitle Editor"),
        ("table_row", "docflix-rename|TV Show Renamer"),
        ("table_row", "docflix-media|Media Processor"),
    ]),

    ("Quick Start", [
        ("h2", "2. Quick Start"),
        ("p", "1. Launch the app via the system menu, terminal (docflix), or python3 video_converter.py"),
        ("p", "2. Add files \u2014 drag and drop video files, or Ctrl+O (files) / Ctrl+Shift+O (folder)"),
        ("p", "3. Choose settings \u2014 encoder, video codec, quality mode, container format"),
        ("p", "4. Click \"Start Conversion\" \u2014 converts all queued files"),
        ("p", "5. Monitor progress \u2014 progress bar and log with batch ETA"),
        ("p", ""),
        ("tip", "For a quick test, select a file and press Ctrl+T to encode just the first 30 seconds."),
    ]),

    ("Main Window", [
        ("h2", "3. Main Window"),
        ("h3", "Menu Bar"),
        ("table_header", "Menu|Key Items"),
        ("table_row", "File|Open File(s), Open Folder, Exit"),
        ("table_row", "Settings|Default Settings, Reset to Defaults"),
        ("table_row", "View|Show/Hide Log, Show/Hide Settings"),
        ("table_row", "Tools|Play, Media Details, Test Encode, Media Processor, Subtitle Editor, TV Show Renamer"),
        ("table_row", "Help|User Manual, Keyboard Shortcuts, About"),
        ("p", ""),
        ("h3", "Encoder Selection"),
        ("p", "The encoder dropdown (top-right) shows all detected backends:"),
        ("table_header", "Encoder|Hardware|Speed|Quality"),
        ("table_row", "CPU|Any CPU|Slowest|Best at equivalent settings"),
        ("table_row", "NVIDIA (NVENC)|NVIDIA GPU|Very fast|Excellent"),
        ("table_row", "Intel (QSV)|Intel GPU/iGPU|Fast|Good"),
        ("table_row", "AMD / VAAPI|AMD GPU|Fast|Good"),
        ("p", ""),
        ("note", "The app auto-detects available GPU backends at startup using a test encode. Only working backends appear."),
        ("p", ""),
        ("h3", "Settings Panel"),
        ("p", "Toggle with Ctrl+Shift+S. Contains all encoding controls:"),
        ("bullet", "Video Codec \u2014 H.265/HEVC, H.264/AVC, AV1, VP9, MPEG-4, ProRes, Copy"),
        ("bullet", "Container \u2014 .mkv, .mp4, .ts"),
        ("bullet", "Transcode Mode \u2014 Video Only, Audio Only, Both"),
        ("bullet", "Quality Mode \u2014 Bitrate (fixed size) or CRF (constant quality)"),
        ("bullet", "Bitrate slider \u2014 1M to 16M with quick-set buttons"),
        ("bullet", "CRF value \u2014 lower = better quality, higher = smaller file"),
        ("bullet", "Preset \u2014 speed/quality trade-off"),
        ("bullet", "Audio \u2014 codec and bitrate dropdowns"),
        ("bullet", "Options \u2014 Skip existing, Delete originals, HW Decode, Two-pass, Verify output"),
        ("bullet", "Metadata \u2014 Strip chapters, Strip tags, Set track metadata"),
        ("bullet", "Edition \u2014 Tag videos with version info"),
        ("bullet", "Chapters \u2014 Auto-generate chapter markers"),
        ("p", ""),
        ("h3", "File Queue"),
        ("bullet", "Add files \u2014 drag and drop, Ctrl+O (files), Ctrl+Shift+O (folder)"),
        ("bullet", "Remove files \u2014 select and press Delete, or right-click \u2192 Remove"),
        ("bullet", "Double-click \u2014 opens Internal Subtitles dialog"),
        ("bullet", "Right-click \u2014 context menu with play, overrides, subtitles, Enhanced Media Details"),
        ("p", ""),
        ("h4", "Queue Indicators"),
        ("table_header", "Icon|Meaning"),
        ("table_row", "\u2699\ufe0f|Per-file settings override applied"),
        ("table_row", "\ud83d\udcce|External subtitles attached"),
        ("table_row", "CC|Closed captions detected (MPEG-TS)"),
        ("p", ""),
        ("h3", "Starting a Conversion"),
        ("p", "1. Add files to the queue"),
        ("p", "2. Adjust settings (or use per-file overrides for individual files)"),
        ("p", "3. Click Start Conversion"),
        ("p", "4. Use Pause / Stop to control the process"),
        ("p", "5. Sound notification plays on completion"),
        ("p", "6. Click Clear Finished to remove completed files"),
        ("p", ""),
        ("tip", "The Batch ETA in the status bar shows estimated time remaining for all files."),
    ]),

    ("Video Settings", [
        ("h2", "4. Video Settings"),
        ("h3", "Video Codecs"),
        ("table_header", "Codec|CPU Encoder|Best For"),
        ("table_row", "H.265 / HEVC|libx265|General use \u2014 best compression"),
        ("table_row", "H.264 / AVC|libx264|Maximum compatibility"),
        ("table_row", "AV1|libsvtav1|Next-gen, best compression, slower"),
        ("table_row", "VP9|libvpx-vp9|WebM, web streaming"),
        ("table_row", "MPEG-4|mpeg4|Legacy compatibility"),
        ("table_row", "ProRes|prores_ks|Professional editing, Apple"),
        ("table_row", "Copy|copy|No re-encoding, just remux"),
        ("p", ""),
        ("h3", "Quality Modes"),
        ("h4", "Bitrate Mode (Fixed Size)"),
        ("p", "Sets a target bitrate. Output file size is predictable. Slider: 1M\u201316M."),
        ("p", ""),
        ("h4", "CRF Mode (Constant Quality)"),
        ("p", "Maintains consistent visual quality. File size varies by content."),
        ("table_header", "CRF|Quality|File Size"),
        ("table_row", "18|Visually lossless|Large"),
        ("table_row", "23|Good (default)|Medium"),
        ("table_row", "28|Acceptable|Small"),
        ("table_row", "35+|Noticeable loss|Very small"),
        ("p", ""),
        ("h3", "Presets"),
        ("p", "CPU: ultrafast \u2192 superfast \u2192 veryfast \u2192 faster \u2192 fast \u2192 medium \u2192 slow \u2192 slower \u2192 veryslow"),
        ("p", "NVENC: p1 (fastest) \u2192 p4 (default) \u2192 p7 (best quality)"),
        ("p", "QSV: veryfast \u2192 medium (default) \u2192 veryslow"),
        ("p", ""),
        ("h3", "Two-Pass Encoding"),
        ("p", "Analyzes video in first pass, optimizes in second. Better quality at a given bitrate. Roughly doubles encoding time. Most beneficial in bitrate mode."),
        ("p", ""),
        ("h3", "Hardware Decode"),
        ("p", "Offloads decoding to GPU when a GPU encoder is selected. Speeds up encoding."),
        ("warning", "Auto-disabled for burn-in subtitles. May cause issues with some source files. Uncheck if you encounter errors."),
        ("p", ""),
        ("h3", "Container Formats"),
        ("table_header", "Container|Subtitle Support|Notes"),
        ("table_row", ".mkv (Matroska)|All formats|Most flexible, recommended"),
        ("table_row", ".mp4|mov_text only|Best device compatibility"),
        ("table_row", ".ts (MPEG-TS)|DVB only|Transport stream format"),
    ]),

    ("Audio Settings", [
        ("h2", "5. Audio Settings"),
        ("p", "Available when Transcode Mode is \"Audio Only\" or \"Both\"."),
        ("table_header", "Setting|Options|Default"),
        ("table_row", "Audio Codec|AAC, AC3, EAC3, MP3, Opus, FLAC, Copy|AAC"),
        ("table_row", "Audio Bitrate|32k \u2013 640k|128k"),
        ("p", ""),
        ("tip", "Use \"Copy\" to keep the original audio stream without re-encoding. Fastest and preserves quality."),
    ]),

    ("Metadata & Tagging", [
        ("h2", "6. Metadata & Tagging"),
        ("h3", "Strip Chapters / Strip Tags"),
        ("bullet", "Strip chapters \u2014 removes all chapter markers (-map_chapters -1)"),
        ("bullet", "Strip tags \u2014 removes all global metadata (-map_metadata -1)"),
        ("p", ""),
        ("h3", "Set Track Metadata"),
        ("p", "Sets language codes and clears track names and container title."),
        ("table_header", "Track|Default|Purpose"),
        ("table_row", "Video (V:)|und|Undetermined"),
        ("table_row", "Audio (A:)|eng|English"),
        ("table_row", "Subtitle (S:)|eng|English"),
        ("p", ""),
        ("h3", "Edition Tagging"),
        ("p", "Tag videos with version info written to the container title metadata. Displayed by VLC, Plex, Kodi, Jellyfin."),
        ("p", "Presets: Theatrical, Director's Cut, Extended, Extended Director's Cut, Unrated, Special Edition, IMAX, Criterion, Remastered, Anniversary Edition, Ultimate Edition, Custom..."),
        ("p", ""),
        ("p", "Plex filename tag: When \"Add to filename (Plex)\" is checked, output includes {edition-Director's Cut} for Plex edition detection."),
        ("note", "The edition tag works independently of \"Set track metadata\". If both are active, the edition overrides the title-clearing."),
        ("p", ""),
        ("h3", "Add Chapters"),
        ("p", "Auto-generate evenly-spaced chapter markers at a configurable interval (1\u201360 minutes, default 5)."),
        ("p", "\"Add chapters\" and \"Strip chapters\" are mutually exclusive \u2014 checking one unchecks the other."),
        ("tip", "Chapters make it easy to jump through long videos. Most players display chapter markers in the seek bar."),
    ]),

    ("Subtitles", [
        ("h2", "7. Subtitles"),
        ("h3", "Internal Subtitles"),
        ("p", "Double-click a file (or right-click \u2192 Internal Subtitles) to manage embedded tracks:"),
        ("bullet", "Keep or drop tracks"),
        ("bullet", "Convert formats (SRT, ASS, WebVTT, TTML)"),
        ("bullet", "Extract to standalone files"),
        ("bullet", "Edit in the subtitle editor"),
        ("bullet", "Bitmap subs (PGS/VobSub) can be OCR'd to text"),
        ("p", ""),
        ("h3", "External Subtitles"),
        ("bullet", "Drag and drop .srt, .ass, .ssa, .vtt, .sub, .idx, .sup files"),
        ("bullet", "Auto-matching by filename stem"),
        ("bullet", "Auto-detection of language, forced, SDH flags from filename"),
        ("p", ""),
        ("h4", "Embed vs Burn-In"),
        ("table_header", "Mode|Description|Togglable?"),
        ("table_row", "Embed|Muxed as a selectable stream|Yes (player controls)"),
        ("table_row", "Burn-in|Rendered permanently onto video|No (permanent)"),
        ("p", ""),
        ("h3", "Subtitle Editor"),
        ("p", "Full-featured editor (Tools \u2192 Subtitle Editor, or edit any track):"),
        ("bullet", "Inline editing \u2014 double-click cells"),
        ("bullet", "Filters \u2014 Remove HI, Tags, Ads, Stray Notes, Leading Dashes, ALL CAPS HI, Off-Screen Quotes, Duplicates, Merge Short Cues, Reduce to 2 Lines, Fix ALL CAPS"),
        ("bullet", "Search & Replace with wrap-around"),
        ("bullet", "Search/Replace List \u2014 persistent correction pairs"),
        ("bullet", "Timing tools \u2014 offset and stretch"),
        ("bullet", "Split / Join / Insert cues"),
        ("bullet", "Undo/Redo (Ctrl+Z / Ctrl+Y)"),
        ("bullet", "Video preview via ffplay"),
        ("bullet", "Color-coded rows \u2014 yellow=modified, blue=HI, pink=tags, orange=long, green=match"),
        ("bullet", "Save to Video \u2014 re-mux without re-encoding"),
        ("p", ""),
        ("h3", "Smart Sync (Whisper-based Auto-Sync)"),
        ("p", "Timing \u2192 Smart Sync in the subtitle editor."),
        ("table_header", "Engine|Accuracy|Speed|Size"),
        ("table_row", "Standard (faster-whisper)|~400ms|Fast (CPU)|~200MB"),
        ("table_row", "Precise (WhisperX)|~50ms|Faster with GPU|~2GB"),
        ("p", ""),
        ("p", "Scan modes: Quick Scan (sampled), Full Scan (entire audio), Direct Align (WhisperX only)"),
        ("p", "Apply methods: Apply Sync (global offset), Re-time All (per-cue interpolation)"),
        ("p", ""),
        ("h3", "Bitmap Subtitle OCR"),
        ("p", "Convert PGS/VobSub bitmap subs to SRT via Tesseract:"),
        ("bullet", "Single-pass rendering (~2 min for 1-hour episode)"),
        ("bullet", "Parallel OCR across CPU cores"),
        ("bullet", "Smart cropping (~13x fewer pixels)"),
        ("bullet", "Live monitor with progress and preview"),
        ("p", ""),
        ("h3", "Spell Checker"),
        ("p", "F7 or Tools \u2192 Spell Check. Interactive dialog with Replace, Replace All, Skip, Ignore, Add to Dict, Add as Name."),
        ("p", ""),
        ("h3", "Batch Filter"),
        ("p", "Tools \u2192 Batch Filter. Process multiple subtitle files at once with filter checkboxes and batch Search & Replace."),
    ]),

    ("Tools", [
        ("h2", "8. Tools"),
        ("h3", "Media Processor"),
        ("p", "Remux-only post-processing (Tools \u2192 Media Processor, Ctrl+M). No re-encoding."),
        ("bullet", "Convert audio (AAC, AC3, EAC3, MP3, Opus, FLAC, Copy) + bitrate"),
        ("bullet", "Strip chapters / tags / existing subtitles"),
        ("bullet", "Mux external subtitles (auto-detects *.eng.srt alongside videos)"),
        ("bullet", "Set track metadata with language codes"),
        ("bullet", "Edition tagging (same presets as main converter)"),
        ("bullet", "Add chapters (auto-generate at intervals)"),
        ("bullet", "Parallel processing (multi-threaded)"),
        ("bullet", "Output: replace in-place or save to folder"),
        ("bullet", "Per-file overrides via right-click"),
        ("p", ""),
        ("h3", "TV Show Renamer"),
        ("p", "Batch rename TV/movie files using TVDB/TMDB (Tools \u2192 TV Show Renamer):"),
        ("bullet", "Auto-detects show/movie names from filenames"),
        ("bullet", "Disambiguation dialog with poster thumbnails"),
        ("bullet", "Movie support (Name (Year).ext)"),
        ("bullet", "Multi-episode (S01E01E02, S01E01-E03)"),
        ("bullet", "Date-based episodes for daily shows"),
        ("bullet", "Subtitle tag preservation"),
        ("bullet", "Undo rename (Ctrl+Z)"),
        ("bullet", "Configurable filename template"),
        ("p", ""),
        ("h3", "Enhanced Media Details"),
        ("p", "Comprehensive file analysis (right-click \u2192 Enhanced Media Details, Ctrl+Shift+I):"),
        ("table_header", "Tab|Information"),
        ("table_row", "General|Format, duration, size, bitrate, title/edition"),
        ("table_row", "Video|Codec/profile/level, resolution, fps, scan type, bit depth, color space, HDR"),
        ("table_row", "Audio|Codec/profile, sample rate, channels, layout, bitrate, language"),
        ("table_row", "Subtitles|Codec, events, resolution (bitmap), disposition"),
        ("table_row", "Chapters|Full listing with timestamps and titles"),
        ("table_row", "Attachments|Fonts, images, MIME types"),
        ("table_row", "Metadata|All container tags"),
        ("table_row", "Full Report|All sections combined"),
        ("p", ""),
        ("p", "Includes Copy to Clipboard and Copy Full Report buttons."),
        ("p", ""),
        ("h3", "Test Encode"),
        ("p", "Ctrl+T \u2014 encode first 30 seconds with current settings for preview."),
    ]),

    ("Per-File Overrides", [
        ("h2", "9. Per-File Overrides"),
        ("p", "Right-click a file \u2192 Override Settings for individual encoding parameters:"),
        ("bullet", "Encoder, video codec, quality mode, bitrate/CRF, preset"),
        ("bullet", "Audio codec and bitrate"),
        ("bullet", "Skip existing, delete originals, HW decode"),
        ("bullet", "Strip chapters, strip tags, set track metadata"),
        ("bullet", "Edition tag and Plex filename option"),
        ("p", ""),
        ("p", "Files with overrides show a \u2699\ufe0f icon. Double-click to edit."),
    ]),

    ("CLI Usage", [
        ("h2", "10. CLI Usage"),
        ("p", "The convert_videos.sh script provides headless batch conversion. Run from the directory containing your videos:"),
        ("code", "# CPU encoding, default settings"),
        ("code", "./convert_videos.sh"),
        ("code", ""),
        ("code", "# NVIDIA GPU encoding"),
        ("code", "./convert_videos.sh --gpu"),
        ("code", ""),
        ("code", "# Intel QSV / AMD VAAPI"),
        ("code", "./convert_videos.sh --qsv"),
        ("code", "./convert_videos.sh --vaapi"),
        ("code", ""),
        ("code", "# CRF quality mode"),
        ("code", "./convert_videos.sh --crf 22"),
        ("code", ""),
        ("code", "# GPU, high quality, overwrite"),
        ("code", "./convert_videos.sh --gpu --gpu-preset p5 --overwrite"),
        ("p", ""),
        ("h4", "CLI Options"),
        ("table_header", "Flag|Description|Default"),
        ("table_row", "-b, --bitrate|Video bitrate|2M"),
        ("table_row", "-q, --crf|CRF quality (disables bitrate)|disabled"),
        ("table_row", "-p, --preset|CPU encoding preset|ultrafast"),
        ("table_row", "-g, --gpu|Use NVIDIA GPU|off"),
        ("table_row", "--qsv|Use Intel QSV|off"),
        ("table_row", "--vaapi|Use VAAPI encoding|off"),
        ("table_row", "-P, --gpu-preset|GPU preset|varies"),
        ("table_row", "-s, --suffix|Output filename suffix|-2mbps-UF_265"),
        ("table_row", "-o, --overwrite|Overwrite existing|skip"),
        ("table_row", "-c, --cleanup|Delete originals|off"),
        ("table_row", "-n, --no-log|Disable log file|off"),
    ]),

    ("Keyboard Shortcuts", [
        ("h2", "11. Keyboard Shortcuts"),
        ("table_header", "Shortcut|Action"),
        ("table_row", "Ctrl+O|Open File(s)"),
        ("table_row", "Ctrl+Shift+O|Open Folder"),
        ("table_row", "Ctrl+P|Play Source File"),
        ("table_row", "Ctrl+Shift+P|Play Output File"),
        ("table_row", "Ctrl+I|Media Details"),
        ("table_row", "Ctrl+Shift+I|Enhanced Media Details"),
        ("table_row", "Ctrl+T|Test Encode (30s)"),
        ("table_row", "Ctrl+M|Media Processor"),
        ("table_row", "Ctrl+Shift+F|Open Output Folder"),
        ("table_row", "Ctrl+L|Show/Hide Log"),
        ("table_row", "Ctrl+Shift+S|Show/Hide Settings Panel"),
        ("table_row", "F1|Keyboard Shortcuts"),
        ("table_row", "Ctrl+Q|Exit"),
        ("table_row", "Delete|Remove selected file"),
        ("p", ""),
        ("h4", "Subtitle Editor"),
        ("table_header", "Shortcut|Action"),
        ("table_row", "Ctrl+Z|Undo"),
        ("table_row", "Ctrl+Y|Redo"),
        ("table_row", "Ctrl+F|Find"),
        ("table_row", "Ctrl+H|Find & Replace"),
        ("table_row", "F7|Spell Check"),
        ("table_row", "Ctrl+S|Save"),
        ("table_row", "Ctrl+Enter|Save inline edit"),
        ("table_row", "Escape|Cancel inline edit"),
    ]),

    ("Preferences", [
        ("h2", "12. Preferences"),
        ("p", "Auto-saved to ~/.local/share/docflix/preferences.json when Default Settings dialog is closed."),
        ("p", ""),
        ("h4", "What is saved"),
        ("bullet", "Encoder, codec, container, quality mode, CRF, presets"),
        ("bullet", "Audio codec and bitrate"),
        ("bullet", "All checkboxes (skip existing, delete originals, HW decode, two-pass, verify, notify)"),
        ("bullet", "Metadata options (strip chapters/tags, track metadata, language codes)"),
        ("bullet", "Edition tag and Plex filename option"),
        ("bullet", "Chapter insertion settings"),
        ("bullet", "Media player preference, recent folders"),
        ("bullet", "Custom ad patterns, character names, spell check dictionary"),
        ("bullet", "TV Show Renamer API keys, provider, template"),
        ("bullet", "Media Processor settings (all options)"),
        ("p", ""),
        ("h4", "What is NOT saved (intentionally)"),
        ("bullet", "Video bitrate \u2014 resets to 2M on every launch"),
        ("bullet", "Transcode mode \u2014 always starts as Video Only"),
        ("p", ""),
        ("p", "To reset: Settings \u2192 Reset to Defaults, or delete preferences.json."),
    ]),

    ("Troubleshooting", [
        ("h2", "13. Troubleshooting"),
        ("h3", "GPU encoding fails"),
        ("bullet", "Try unchecking HW Decode"),
        ("bullet", "Ensure GPU drivers are up to date"),
        ("bullet", "Check ffmpeg GPU encoder support"),
        ("p", ""),
        ("h3", "Small or empty output files"),
        ("bullet", "Check the log for error messages"),
        ("bullet", "Try a different encoder (switch from GPU to CPU)"),
        ("bullet", "Try a different container format"),
        ("bullet", "Verify source file is not corrupted"),
        ("p", ""),
        ("h3", "Subtitles not appearing"),
        ("bullet", "MPEG-TS output \u2014 text subtitles not supported, only DVB"),
        ("bullet", "MP4 output \u2014 only mov_text supported (auto-converted)"),
        ("bullet", "Check \"Strip existing subtitle tracks\" is not enabled"),
        ("p", ""),
        ("h3", "App appears tiny on high-DPI monitor"),
        ("bullet", "Set GDK_SCALE=2 environment variable before launching"),
        ("bullet", "Or set Xft.dpi: 192 in ~/.Xresources"),
        ("p", ""),
        ("h3", "Drag and drop not working"),
        ("p", "Install tkinterdnd2: pip install tkinterdnd2"),
        ("p", ""),
        ("h3", "Zenity folder dialogs not appearing"),
        ("p", "Install zenity: sudo apt install zenity"),
    ]),

    ("Encoding Reference", [
        ("h2", "14. Encoding Reference"),
        ("h3", "CPU (libx265)"),
        ("table_header", "Mode|Flag|Recommended Range"),
        ("table_row", "Bitrate|-b:v|1M \u2013 8M+"),
        ("table_row", "CRF|-crf|18\u201328 (lower = better)"),
        ("p", ""),
        ("h3", "NVIDIA GPU (hevc_nvenc)"),
        ("table_header", "Mode|Flag|Recommended Range"),
        ("table_row", "Bitrate|-b:v|1M \u2013 8M+"),
        ("table_row", "CQ|-cq|15\u201325 (lower = better)"),
        ("p", ""),
        ("h3", "Intel GPU (hevc_qsv)"),
        ("table_header", "Mode|Flag|Recommended Range"),
        ("table_row", "Bitrate|-b:v|1M \u2013 8M+"),
        ("table_row", "Quality|-global_quality|15\u201325 (lower = better)"),
        ("p", ""),
        ("h3", "AMD GPU (hevc_vaapi)"),
        ("table_header", "Mode|Flag|Recommended Range"),
        ("table_row", "Bitrate|-b:v|1M \u2013 8M+"),
        ("table_row", "Quality|-qp|15\u201325 (lower = better)"),
        ("p", ""),
        ("note", "GPU encoding is significantly faster but may produce slightly larger files at equivalent quality. The difference is minimal with modern encoders."),
    ]),
]


# ═══════════════════════════════════════════════════════════════════
# Viewer window
# ═══════════════════════════════════════════════════════════════════

def show_manual(app):
    """Open the built-in user manual viewer.

    Args:
        app: The VideoConverterApp or StandaloneContext instance.
    """
    win = tk.Toplevel(app.root)
    win.title("Docflix Video Converter \u2014 User Manual")
    win.geometry("960x700")
    win.minsize(700, 500)
    win.resizable(True, True)
    try:
        app._center_on_main(win)
    except Exception:
        pass

    # ── Colors ──
    bg = '#1e1e2e'
    bg2 = '#181828'
    fg = '#d4d4d4'
    fg2 = '#a0a0b0'
    accent = '#e94560'
    accent2 = '#bb86fc'
    link = '#64b5f6'
    code_bg = '#0d1117'
    border = '#2a2a4a'
    tip_bg = '#1a2e1a'
    warn_bg = '#2e2a1a'
    note_bg = '#1a1a2e'

    # ── Main paned window ──
    paned = tk.PanedWindow(win, orient='horizontal', bg=bg,
                           sashwidth=4, sashrelief='flat')
    paned.pack(fill='both', expand=True)

    # ── Sidebar ──
    sidebar_frame = tk.Frame(paned, bg=bg2, width=220)

    sidebar_label = tk.Label(sidebar_frame, text="Contents", font=('Helvetica', 12, 'bold'),
                             bg=bg2, fg=accent, anchor='w', padx=12, pady=8)
    sidebar_label.pack(fill='x')

    sidebar_list = tk.Listbox(sidebar_frame, bg=bg2, fg=fg, font=('Helvetica', 11),
                              selectbackground=accent, selectforeground='#ffffff',
                              activestyle='none', borderwidth=0, highlightthickness=0,
                              relief='flat')
    sidebar_list.pack(fill='both', expand=True, padx=4, pady=(0, 8))

    for title, _ in MANUAL_SECTIONS:
        sidebar_list.insert('end', f'  {title}')

    paned.add(sidebar_frame, minsize=180, width=220)

    # ── Content area ──
    content_frame = tk.Frame(paned, bg=bg)

    text = tk.Text(content_frame, wrap='word', bg=bg, fg=fg,
                   font=('Helvetica', 11), padx=20, pady=16,
                   insertbackground=fg, selectbackground='#264f78',
                   selectforeground='#ffffff', borderwidth=0,
                   highlightthickness=0, spacing1=1, spacing3=1)
    scrollbar = ttk.Scrollbar(content_frame, orient='vertical', command=text.yview)
    text.configure(yscrollcommand=scrollbar.set)
    scrollbar.pack(side='right', fill='y')
    text.pack(fill='both', expand=True)

    paned.add(content_frame, minsize=400)

    # ── Text tags ──
    text.tag_configure('h2', font=('Helvetica', 18, 'bold'), foreground=accent,
                       spacing1=16, spacing3=8)
    text.tag_configure('h3', font=('Helvetica', 14, 'bold'), foreground=accent2,
                       spacing1=12, spacing3=6)
    text.tag_configure('h4', font=('Helvetica', 12, 'bold'), foreground=fg,
                       spacing1=10, spacing3=4)
    text.tag_configure('p', font=('Helvetica', 11), foreground=fg,
                       spacing1=2, spacing3=2)
    text.tag_configure('bullet', font=('Helvetica', 11), foreground=fg,
                       lmargin1=28, lmargin2=28, spacing1=1, spacing3=1)
    text.tag_configure('code', font=('Consolas', 10), foreground='#98c379',
                       background=code_bg, lmargin1=20, lmargin2=20,
                       spacing1=1, spacing3=1)
    text.tag_configure('tip', font=('Helvetica', 11), foreground='#66bb6a',
                       background=tip_bg, lmargin1=12, lmargin2=12,
                       spacing1=4, spacing3=4)
    text.tag_configure('warning', font=('Helvetica', 11), foreground='#ffa726',
                       background=warn_bg, lmargin1=12, lmargin2=12,
                       spacing1=4, spacing3=4)
    text.tag_configure('note', font=('Helvetica', 11), foreground=link,
                       background=note_bg, lmargin1=12, lmargin2=12,
                       spacing1=4, spacing3=4)
    text.tag_configure('table_header', font=('Consolas', 10, 'bold'),
                       foreground='#ffffff', background='#0f3460',
                       lmargin1=12, lmargin2=12, spacing1=3, spacing3=1)
    text.tag_configure('table_row', font=('Consolas', 10), foreground=fg2,
                       background='#12122a', lmargin1=12, lmargin2=12,
                       spacing1=1, spacing3=1)
    text.tag_configure('sep', foreground=border)

    # ── Section anchor indices for navigation ──
    section_indices = {}  # title -> text index string (e.g. "42.0")

    def _render_all():
        """Render all sections into the text widget."""
        text.configure(state='normal')
        text.delete('1.0', 'end')

        # Title
        text.insert('end', 'Docflix Video Converter\n', 'h2')
        text.insert('end', 'User Manual \u2014 Version 2.0.7\n\n', 'p')

        for section_title, lines in MANUAL_SECTIONS:
            # Record the index where this section starts
            section_indices[section_title] = text.index('end-1c')

            for tag, content in lines:
                if tag == 'bullet':
                    text.insert('end', f'  \u2022 {content}\n', 'bullet')
                elif tag == 'tip':
                    text.insert('end', f'  Tip: {content}\n', 'tip')
                elif tag == 'warning':
                    text.insert('end', f'  Warning: {content}\n', 'warning')
                elif tag == 'note':
                    text.insert('end', f'  Note: {content}\n', 'note')
                elif tag == 'table_header':
                    cols = content.split('|')
                    row = '  ' + '  '.join(f'{c:<24}' for c in cols)
                    text.insert('end', row + '\n', 'table_header')
                elif tag == 'table_row':
                    cols = content.split('|')
                    row = '  ' + '  '.join(f'{c:<24}' for c in cols)
                    text.insert('end', row + '\n', 'table_row')
                elif tag == 'sep':
                    text.insert('end', '\u2500' * 60 + '\n', 'sep')
                elif tag == 'code':
                    text.insert('end', f'  {content}\n', 'code')
                elif content:
                    text.insert('end', content + '\n', tag)
                else:
                    text.insert('end', '\n', 'p')

            text.insert('end', '\n')

        text.insert('end', '\n\u00a9 2026 Tony Davis \u2014 MIT License\n', 'p')
        text.configure(state='disabled')

    def _on_sidebar_select(event=None):
        """Navigate to the selected section."""
        sel = sidebar_list.curselection()
        if not sel:
            return
        title = MANUAL_SECTIONS[sel[0]][0]
        idx = section_indices.get(title)
        if idx:
            # Use the raw Tk yview command with a text index —
            # this scrolls the given index to the top of the window
            text.tk.call(text._w, 'yview', idx)

    sidebar_list.bind('<<ListboxSelect>>', _on_sidebar_select)
    # Also bind single-click directly in case <<ListboxSelect>> is unreliable
    sidebar_list.bind('<ButtonRelease-1>', _on_sidebar_select)

    # Render content
    _render_all()

    # Select first section
    sidebar_list.selection_set(0)
