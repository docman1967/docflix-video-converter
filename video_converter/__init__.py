"""
Docflix Video Converter

Batch convert video files to H.265/HEVC format using ffmpeg,
with support for CPU and multi-GPU encoding (NVIDIA NVENC,
Intel QSV, AMD VAAPI).

Package structure:
    constants.py      — Shared constants, codec maps, GPU backends
    utils.py          — Format helpers, ffprobe wrappers, Tk utilities
    standalone.py     — Lightweight context for standalone tool launches
    app.py            — Main VideoConverterApp GUI (future)
    converter.py      — VideoConverter engine (future)
    gpu.py            — GPU detection (future)
    tv_renamer.py     — TV Show Renamer tool (future)
    media_processor.py — Media Processor tool (future)
    subtitle_editor.py — Subtitle Editor tool (future)
    subtitle_filters.py — Subtitle filter functions (future)
    smart_sync.py     — Whisper-based subtitle sync (future)
    spell_checker.py  — Subtitle spell checker (future)
    subtitle_ocr.py   — Bitmap subtitle OCR (future)
    batch_filter.py   — Batch filter window (future)
    preferences.py    — Preferences management (future)
"""

from .constants import APP_NAME, APP_VERSION

__version__ = APP_VERSION
__app_name__ = APP_NAME
