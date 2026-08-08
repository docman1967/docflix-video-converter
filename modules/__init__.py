"""
Docflix Media Suite

Batch convert video files to H.265/HEVC format using ffmpeg,
with support for CPU and multi-GPU encoding (NVIDIA NVENC,
Intel QSV, AMD VAAPI).

Package structure.

⚠️ THIS LIST WAS DANGEROUSLY STALE UNTIL 2026-08-08. It marked nine modules
"(future)" that had long since been extracted and were live and imported —
gpu, subtitle_editor, subtitle_filters, spell_checker, subtitle_ocr,
batch_filter, media_processor, tv_renamer, smart_sync. It also omitted
torch_upscale_worker.py, which is load-bearing.

That combination is how a roadmap turns into a hazard: read it cold and the
obvious conclusion is "these are unused, delete them." Keep it current or
delete it — a wrong map is worse than no map.

    LIVE — imported and in use
    constants.py         — Shared constants, codec maps, GPU backends
    utils.py             — Format helpers, ffprobe wrappers, Tk utilities
    standalone.py        — Lightweight context for standalone tool launches
    gpu.py               — GPU detection
    tv_renamer.py        — TV Show Renamer tool
    media_processor.py   — Media Processor tool
    subtitle_editor.py   — Subtitle Editor tool
    subtitle_filters.py  — Subtitle filter functions
    smart_sync.py        — Whisper-based subtitle sync
    spell_checker.py     — Subtitle spell checker
    subtitle_ocr.py      — Bitmap subtitle OCR
    batch_filter.py      — Batch filter window
    media_info.py        — Enhanced media details dialog
    chapters.py          — Chapter generation, parsing, FFMETADATA writing
    manual_viewer.py     — Built-in user manual viewer
    video_scaler.py      — Video Scaler tool
    waveform_timeline.py — Waveform timeline + embedded mpv playback
    torch_upscaler.py    — PyTorch/CUDA AI upscale (launcher side)

    LIVE, BUT NEVER IMPORTED — ⚠️ do not judge this one by import count
    torch_upscale_worker.py — run as a SUBPROCESS by torch_upscaler.py:84,
        under a foreign torch-capable venv. It deliberately has no package
        imports, so every "find unused modules" sweep calls it an orphan.
        It is not. Deleting it breaks the fast AI upscaler.

    EXTRACTED BUT NOT YET WIRED UP — real work in progress, not dead code
    converter.py         — VideoConverter engine, 1,100+ lines and
        complete-looking; video_converter.py still uses its own copy
    preferences.py       — save/load/reset; in practice superseded by
        VideoConverterApp.save_preferences (video_converter.py:7961)
    app.py               — Main VideoConverterApp GUI (not started)
"""

from .constants import APP_NAME, APP_VERSION

__version__ = APP_VERSION
__app_name__ = APP_NAME
