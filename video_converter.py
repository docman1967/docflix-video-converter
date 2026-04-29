#!/usr/bin/env python3
"""
Docflix Video Converter - Standalone GUI Application
Convert MKV videos to H.265/HEVC format with CPU or GPU encoding

Features:
- CPU and multi-GPU encoding (NVIDIA NVENC, Intel QSV, AMD VAAPI/AMF)
- Bitrate and CRF quality modes
- Batch conversion with progress tracking
- Folder selection and file management
- Real-time logging and notifications

Requirements:
- ffmpeg with optional GPU encoder support
- Python 3.8+
- tkinter (usually included with Python)

Usage:
    python video_converter.py
    python video_converter.py --gpu-test-mode   # skip GPU test encodes (detection only)
"""

# ── GPU Test Mode ──
# When True, GPU detection skips the test encode (Tier 2) and relies only on
# ffmpeg encoder availability (Tier 1) + lspci identification (Tier 3).
# Activated via --gpu-test-mode command-line flag.
GPU_TEST_MODE = False

import os
import sys
import json
import subprocess
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
from datetime import datetime
from pathlib import Path
import re
import shutil

try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    HAS_DND = True
except ImportError:
    HAS_DND = False

# ============================================================================
# Configuration
# ============================================================================

APP_NAME = "Docflix Video Converter"
APP_VERSION = "2.0.7"
DEFAULT_BITRATE = "2M"
DEFAULT_CRF = 23
DEFAULT_PRESET = "ultrafast"
DEFAULT_GPU_PRESET = "p4"

# Bitmap subtitle codecs that cannot be converted to text formats without OCR
BITMAP_SUB_CODECS = frozenset({'hdmv_pgs_subtitle', 'dvd_subtitle', 'dvb_subtitle', 'dvb_teletext', 'xsub'})

# ── Edition presets for container title tagging ──
EDITION_PRESETS = [
    '',                     # (no edition)
    'Theatrical',
    "Director's Cut",
    'Extended',
    'Extended Director\'s Cut',
    'Unrated',
    'Special Edition',
    'IMAX',
    'Criterion',
    'Remastered',
    'Anniversary Edition',
    'Ultimate Edition',
    'Custom...',
]

# ── GPU Backend Definitions ──
# Each backend defines its hwaccel flags, per-codec encoders, presets, quality
# flags, and how to detect whether the hardware is present.
GPU_BACKENDS = {
    'nvenc': {
        'label':        'NVIDIA (NVENC)',
        'short':        'NVENC',
        'hwaccel':      ['-hwaccel', 'cuda'],
        'scale_filter': 'scale_cuda=format=yuv420p',
        'detect_encoders': ['hevc_nvenc'],          # checked in ffmpeg -encoders
        'detect_cmd':   ['nvidia-smi', '--query-gpu=name', '--format=csv,noheader'],
        'encoders': {
            'H.265 / HEVC': 'hevc_nvenc',
            'H.264 / AVC':  'h264_nvenc',
            'AV1':          'av1_nvenc',
            'VP9':          None,
            'MPEG-4':       None,
            'ProRes (QuickTime)': None,
            'Copy (no re-encode)': 'copy',
        },
        'presets':        ('p1', 'p2', 'p3', 'p4', 'p5', 'p6', 'p7'),
        'preset_default': 'p4',
        'preset_flag':    '-preset',
        'cq_flag':        '-cq',
        'multipass_encoders': {'hevc_nvenc', 'h264_nvenc', 'av1_nvenc'},
        'multipass_args':     ['-multipass', 'fullres'],
    },
    'qsv': {
        'label':        'Intel (QSV)',
        'short':        'QSV',
        'hwaccel':      ['-hwaccel', 'qsv', '-hwaccel_output_format', 'qsv'],
        'scale_filter': 'scale_qsv=format=nv12',
        'detect_encoders': ['hevc_qsv'],
        'detect_cmd':   None,       # GPU name detected via lspci fallback
        'encoders': {
            'H.265 / HEVC': 'hevc_qsv',
            'H.264 / AVC':  'h264_qsv',
            'AV1':          'av1_qsv',
            'VP9':          'vp9_qsv',
            'MPEG-4':       None,
            'ProRes (QuickTime)': None,
            'Copy (no re-encode)': 'copy',
        },
        'presets':        ('veryfast', 'faster', 'fast', 'medium', 'slow', 'slower', 'veryslow'),
        'preset_default': 'medium',
        'preset_flag':    '-preset',
        'cq_flag':        '-global_quality',
        'multipass_encoders': set(),
        'multipass_args':     [],
    },
    'vaapi': {
        'label':        'AMD / VAAPI',
        'short':        'VAAPI',
        'hwaccel':      ['-hwaccel', 'vaapi', '-hwaccel_output_format', 'vaapi',
                         '-vaapi_device', '/dev/dri/renderD128'],
        'scale_filter': 'scale_vaapi=format=nv12',
        'detect_encoders': ['hevc_vaapi'],
        'detect_cmd':   None,
        'encoders': {
            'H.265 / HEVC': 'hevc_vaapi',
            'H.264 / AVC':  'h264_vaapi',
            'AV1':          'av1_vaapi',
            'VP9':          'vp9_vaapi',
            'MPEG-4':       None,
            'ProRes (QuickTime)': None,
            'Copy (no re-encode)': 'copy',
        },
        'presets':        (),       # VAAPI has no presets
        'preset_default': None,
        'preset_flag':    None,
        'cq_flag':        '-qp',    # VAAPI uses -qp for constant quality
        'multipass_encoders': set(),
        'multipass_args':     [],
    },
}

# Video codec definitions
# Keys: display name -> dict with cpu_encoder, cpu_presets,
#       crf_range, crf_default, crf_flag, short_name
# GPU encoders/presets are now looked up from GPU_BACKENDS above.
VIDEO_CODEC_MAP = {
    'H.265 / HEVC': {
        'cpu_encoder': 'libx265',
        'cpu_presets': ('ultrafast', 'superfast', 'veryfast', 'faster', 'fast',
                        'medium', 'slow', 'slower', 'veryslow'),
        'cpu_preset_default': 'ultrafast',
        'crf_min': 0, 'crf_max': 51, 'crf_default': 23,
        'crf_flag': '-crf',
        'short_name': 'H265',
    },
    'H.264 / AVC': {
        'cpu_encoder': 'libx264',
        'cpu_presets': ('ultrafast', 'superfast', 'veryfast', 'faster', 'fast',
                        'medium', 'slow', 'slower', 'veryslow'),
        'cpu_preset_default': 'ultrafast',
        'crf_min': 0, 'crf_max': 51, 'crf_default': 23,
        'crf_flag': '-crf',
        'short_name': 'H264',
    },
    'AV1': {
        'cpu_encoder': 'libsvtav1',
        'cpu_presets': ('0', '1', '2', '3', '4', '5', '6', '7', '8', '9', '10', '11', '12', '13'),
        'cpu_preset_default': '8',
        'crf_min': 0, 'crf_max': 63, 'crf_default': 35,
        'crf_flag': '-crf',
        'short_name': 'AV1',
    },
    'VP9': {
        'cpu_encoder': 'libvpx-vp9',
        'cpu_presets': ('0', '1', '2', '3', '4', '5'),
        'cpu_preset_default': '2',
        'crf_min': 0, 'crf_max': 63, 'crf_default': 33,
        'crf_flag': '-crf',
        'short_name': 'VP9',
    },
    'MPEG-4': {
        'cpu_encoder': 'mpeg4',
        'cpu_presets': (),
        'cpu_preset_default': None,
        'crf_min': 1, 'crf_max': 31, 'crf_default': 4,
        'crf_flag': '-q:v',
        'short_name': 'MPEG4',
    },
    'ProRes (QuickTime)': {
        'cpu_encoder': 'prores_ks',
        'cpu_presets': ('proxy', 'lt', 'standard', 'hq', '4444', '4444xq'),
        'cpu_preset_default': 'hq',
        'crf_min': 0, 'crf_max': 64, 'crf_default': 10,
        'crf_flag': '-q:v',
        'short_name': 'ProRes',
    },
    'Copy (no re-encode)': {
        'cpu_encoder': 'copy',
        'cpu_presets': (),
        'cpu_preset_default': None,
        'crf_min': 0, 'crf_max': 51, 'crf_default': 23,
        'crf_flag': None,
        'short_name': 'copy',
    },
}

def get_gpu_encoder(codec_name, backend_id):
    """Return the GPU encoder name for a codec + backend, or None."""
    backend = GPU_BACKENDS.get(backend_id)
    if not backend:
        return None
    return backend['encoders'].get(codec_name)

def get_gpu_presets(backend_id):
    """Return (presets_tuple, default) for a GPU backend."""
    backend = GPU_BACKENDS.get(backend_id)
    if not backend:
        return (), None
    return backend['presets'], backend['preset_default']

def get_cq_flag(backend_id):
    """Return the constant-quality flag for a GPU backend (e.g. -cq, -global_quality, -qp)."""
    backend = GPU_BACKENDS.get(backend_id)
    if not backend:
        return None
    return backend.get('cq_flag')

# Supported video extensions
VIDEO_EXTENSIONS = {'.mkv', '.mp4', '.avi', '.mov', '.wmv', '.flv', '.webm', '.ts', '.m2ts', '.mts'}

# Supported external subtitle extensions
SUBTITLE_EXTENSIONS = {'.srt', '.ass', '.ssa', '.vtt', '.sub', '.idx', '.sup'}

# Subtitle extension to ffmpeg codec name (for embedding)
SUBTITLE_EXT_TO_CODEC = {
    '.srt': 'srt',
    '.ass': 'ass',
    '.ssa': 'ass',
    '.vtt': 'webvtt',
    '.sub': 'dvd_subtitle',
    '.idx': 'dvd_subtitle',
    '.sup': 'hdmv_pgs_subtitle',
}

# Common language codes for subtitle tagging
SUBTITLE_LANGUAGES = [
    ('und', 'Undetermined'),
    ('eng', 'English'),
    ('spa', 'Spanish'),
    ('fra', 'French'),
    ('deu', 'German'),
    ('ita', 'Italian'),
    ('por', 'Portuguese'),
    ('rus', 'Russian'),
    ('jpn', 'Japanese'),
    ('kor', 'Korean'),
    ('zho', 'Chinese'),
    ('ara', 'Arabic'),
    ('hin', 'Hindi'),
    ('nld', 'Dutch'),
    ('pol', 'Polish'),
    ('swe', 'Swedish'),
    ('tur', 'Turkish'),
    ('vie', 'Vietnamese'),
]

# ============================================================================
# Utility Functions
# ============================================================================

def _create_tooltip(widget, text):
    """Attach a hover tooltip to a tkinter widget."""
    tip = None

    def _show(event):
        nonlocal tip
        if tip:
            return
        tip = tk.Toplevel(widget)
        tip.wm_overrideredirect(True)
        tip.wm_geometry(f"+{event.x_root + 12}+{event.y_root + 8}")
        lbl = tk.Label(tip, text=text, background='#ffffe0', relief='solid',
                       borderwidth=1, font=('Helvetica', 9), padx=6, pady=2)
        lbl.pack()

    def _hide(event):
        nonlocal tip
        if tip:
            tip.destroy()
            tip = None

    widget.bind('<Enter>', _show, add='+')
    widget.bind('<Leave>', _hide, add='+')


def format_size(size_bytes):
    """Format file size in human readable format"""
    if size_bytes == 0:
        return "0 B"
    units = ['B', 'KB', 'MB', 'GB', 'TB']
    unit_index = 0
    size = float(size_bytes)
    while size >= 1024 and unit_index < len(units) - 1:
        size /= 1024
        unit_index += 1
    return f"{size:.1f} {units[unit_index]}"

def format_duration(seconds):
    """Format duration as HH:MM:SS or MM:SS"""
    if seconds is None:
        return '?'
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"

def format_time(seconds):
    """Format seconds into human-readable time"""
    if seconds < 60:
        return f"{int(seconds)}s"
    elif seconds < 3600:
        minutes = int(seconds // 60)
        secs = int(seconds % 60)
        return f"{minutes}m {secs}s"
    else:
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        return f"{hours}h {minutes}m {secs}s"

def get_subtitle_streams(filepath):
    """
    Return a list of subtitle stream dicts for the given file.
    Each dict has: index, codec_name, language, title, forced, sdh
    """
    try:
        cmd = [
            'ffprobe', '-v', 'quiet',
            '-print_format', 'json',
            '-show_streams',
            '-select_streams', 's',
            filepath
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return []
        data = json.loads(result.stdout)
        streams = []
        for s in data.get('streams', []):
            tags = s.get('tags', {})
            disp = s.get('disposition', {})
            # Detect empty tracks via muxer statistics
            num_frames_str = (tags.get('NUMBER_OF_FRAMES') or
                              tags.get('NUMBER_OF_FRAMES-eng') or '')
            num_bytes_str = (tags.get('NUMBER_OF_BYTES') or
                             tags.get('NUMBER_OF_BYTES-eng') or '')
            try:
                num_frames = int(num_frames_str) if num_frames_str else -1
            except ValueError:
                num_frames = -1
            try:
                num_bytes = int(num_bytes_str) if num_bytes_str else -1
            except ValueError:
                num_bytes = -1
            is_empty = (num_frames == 0 or num_bytes == 0)
            streams.append({
                'index':      s.get('index', 0),
                'codec_name': s.get('codec_name', 'unknown'),
                'language':   tags.get('language', 'und'),
                'title':      tags.get('title', ''),
                'forced':     bool(disp.get('forced', 0)),
                'sdh':        bool(disp.get('hearing_impaired', 0)),
                'default':    bool(disp.get('default', 0)),
                'empty':      is_empty,
                'num_frames': num_frames,
            })
        return streams
    except Exception:
        return []


def get_all_streams(filepath):
    """Return a list of all stream dicts (video, audio, subtitle, attachment, data, etc.).
    Each dict has: index, codec_type, codec_name."""
    try:
        cmd = [
            'ffprobe', '-v', 'quiet',
            '-print_format', 'json',
            '-show_streams',
            filepath
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return []
        data = json.loads(result.stdout)
        return [
            {
                'index':      s.get('index', 0),
                'codec_type': s.get('codec_type', 'unknown'),
                'codec_name': s.get('codec_name', 'unknown'),
            }
            for s in data.get('streams', [])
        ]
    except Exception:
        return []


def get_audio_info(filepath):
    """Return a list of audio stream dicts for the given file.
    Each dict has: index, codec_name, codec_long_name, channels, sample_rate, bit_rate, language, title."""
    try:
        cmd = [
            'ffprobe', '-v', 'quiet',
            '-print_format', 'json',
            '-show_streams',
            '-select_streams', 'a',
            filepath
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return []
        data = json.loads(result.stdout)
        streams = []
        for s in data.get('streams', []):
            tags = s.get('tags', {})
            streams.append({
                'index':          s.get('index', 0),
                'codec_name':     s.get('codec_name', 'unknown'),
                'codec_long_name': s.get('codec_long_name', ''),
                'channels':       s.get('channels', 0),
                'sample_rate':    s.get('sample_rate', ''),
                'bit_rate':       s.get('bit_rate', ''),
                'language':       tags.get('language', 'und'),
                'title':          tags.get('title', ''),
            })
        return streams
    except Exception:
        return []


def ocr_bitmap_subtitle(filepath, stream_index, language='eng',
                        progress_callback=None, frame_callback=None,
                        cancel_event=None):
    """OCR a bitmap subtitle stream (PGS/VobSub) to a list of SRT cues.

    Uses ffmpeg to render each subtitle event as an image on a black canvas,
    then Tesseract OCR to extract text from each image.

    Args:
        filepath: Path to the video file.
        stream_index: Absolute ffmpeg stream index of the subtitle track.
        language: ISO 639-2 language code (e.g. 'eng', 'fre').
        progress_callback: Optional callable(message) for status updates.
        frame_callback: Optional callable(frame_index, total, img_path,
                        ocr_text, start_time, end_time) called after each frame.
        cancel_event: Optional threading.Event — if set, OCR aborts early.

    Returns:
        List of dicts: [{'index': 1, 'start': '00:01:23,456',
                         'end': '00:01:26,789', 'text': 'Hello'}, ...]
        Returns empty list on failure.
    """
    import tempfile
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        if progress_callback:
            progress_callback("pytesseract or Pillow not installed — cannot OCR")
        return []

    if not shutil.which('tesseract'):
        if progress_callback:
            progress_callback("tesseract not found — install with: sudo apt install tesseract-ocr")
        return []

    # Map ISO 639-2/B → Tesseract language codes
    LANG_MAP = {
        'eng': 'eng', 'fre': 'fra', 'fra': 'fra', 'ger': 'deu', 'deu': 'deu',
        'spa': 'spa', 'ita': 'ita', 'por': 'por', 'rus': 'rus', 'jpn': 'jpn',
        'kor': 'kor', 'chi': 'chi_sim', 'zho': 'chi_sim', 'ara': 'ara',
        'hin': 'hin', 'und': 'eng', 'nld': 'nld', 'pol': 'pol', 'tur': 'tur',
        'swe': 'swe', 'nor': 'nor', 'dan': 'dan', 'fin': 'fin',
    }
    tess_lang = LANG_MAP.get(language, language)

    # Check if Tesseract has the required language data
    try:
        langs_result = subprocess.run(['tesseract', '--list-langs'],
                                       capture_output=True, text=True, timeout=10)
        available = langs_result.stderr + langs_result.stdout  # varies by version
        if tess_lang not in available:
            if progress_callback:
                progress_callback(f"Tesseract language pack '{tess_lang}' not installed — "
                                  f"install with: sudo apt install tesseract-ocr-{tess_lang}")
            return []
    except Exception:
        pass  # proceed anyway

    tmpdir = tempfile.mkdtemp(prefix='docflix_ocr_')

    try:
        # ── Phase 1: Get subtitle packet timestamps via ffprobe ──
        if progress_callback:
            progress_callback("Probing subtitle packet timestamps...")

        probe_cmd = [
            'ffprobe', '-v', 'quiet', '-print_format', 'json',
            '-show_entries', 'packet=pts_time,duration_time,size',
            '-select_streams', str(stream_index),
            filepath
        ]
        result = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            if progress_callback:
                progress_callback("ffprobe failed to read subtitle packets")
            return []

        all_packets = json.loads(result.stdout).get('packets', [])

        # Filter out zero-size packets (PGS clear/end events) and build timing list
        packets = []
        for pkt in all_packets:
            size = int(pkt.get('size', 0))
            pts = pkt.get('pts_time')
            if size > 0 and pts is not None:
                try:
                    pts_f = float(pts)
                except (ValueError, TypeError):
                    continue
                dur = float(pkt.get('duration_time', 0) or 0)
                packets.append({'pts': pts_f, 'duration': dur})

        if not packets:
            if progress_callback:
                progress_callback("No subtitle packets found in stream")
            return []

        # Calculate durations from gaps where duration is missing
        for i, pkt in enumerate(packets):
            if pkt['duration'] <= 0:
                if i + 1 < len(packets):
                    pkt['duration'] = min(packets[i + 1]['pts'] - pkt['pts'], 10.0)
                else:
                    pkt['duration'] = 3.0
            # Clamp to reasonable range
            pkt['duration'] = max(0.5, min(pkt['duration'], 15.0))

        total = len(packets)
        if progress_callback:
            progress_callback(f"Found {total} subtitle events — starting OCR...")

        # ── Phase 2: Compute relative subtitle stream index ──
        all_streams = get_all_streams(filepath)
        rel_idx = 0
        for s in all_streams:
            if s['index'] == stream_index:
                break
            if s['codec_type'] == 'subtitle':
                rel_idx += 1

        # ── Phase 3: Batch-extract all subtitle images in one ffmpeg pass ──
        # Overlay subtitle stream on a black canvas, use scene detection to
        # output one frame per subtitle change (appear + disappear).
        if progress_callback:
            progress_callback("Rendering subtitle images (single pass)...")

        # Get video duration for the lavfi color source
        duration = get_video_duration(filepath) or 7200  # fallback 2h

        img_pattern = os.path.join(tmpdir, 'frame_%05d.png')
        extract_cmd = [
            'ffmpeg', '-y', '-progress', 'pipe:1', '-stats_period', '1',
            '-f', 'lavfi', '-i', f'color=c=black:s=1920x1080:r=10:d={int(duration) + 10}',
            '-i', filepath,
            '-filter_complex',
            f"[0:v][1:s:{rel_idx}]overlay,select='gt(scene\\,0.001)',setpts=N/TB",
            '-vsync', 'vfr',
            img_pattern
        ]
        try:
            proc = subprocess.Popen(extract_cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, bufsize=1)
            # Parse ffmpeg progress output in real-time
            render_frames = [0]
            while True:
                if cancel_event and cancel_event.is_set():
                    proc.terminate()
                    if progress_callback:
                        progress_callback("Rendering cancelled by user")
                    return []
                line = proc.stdout.readline()
                if not line and proc.poll() is not None:
                    break
                if line.startswith('out_time_ms='):
                    try:
                        us = int(line.split('=')[1].strip())
                        secs = us / 1_000_000
                        pct = min(99, (secs / duration) * 100)
                        if progress_callback:
                            mins = int(secs) // 60
                            s = int(secs) % 60
                            progress_callback(f"Rendering subtitle images... "
                                              f"{mins}m{s:02d}s / {int(duration)//60}m "
                                              f"({pct:.0f}%)")
                    except (ValueError, ZeroDivisionError):
                        pass
                elif line.startswith('frame='):
                    try:
                        render_frames[0] = int(line.split('=')[1].strip())
                    except ValueError:
                        pass
            proc.wait()
            if proc.returncode != 0:
                stderr_out = proc.stderr.read()
                if progress_callback:
                    progress_callback(f"Failed to render subtitle images: {stderr_out[-300:]}")
                return []
            if progress_callback:
                progress_callback(f"Rendering complete — {render_frames[0]} frames extracted")
        except Exception as e:
            if progress_callback:
                progress_callback(f"Error during rendering: {e}")
            return []

        # Collect generated image files
        import glob
        img_files = sorted(glob.glob(os.path.join(tmpdir, 'frame_*.png')))

        if not img_files:
            if progress_callback:
                progress_callback("No subtitle images were rendered")
            return []

        if progress_callback:
            progress_callback(f"Rendered {len(img_files)} frames — filtering and running OCR...")

        # ── Phase 4: Filter non-blank images, match to timestamps, OCR ──
        # The scene-change filter produces frames for both subtitle-on and
        # subtitle-off transitions.  We only want the subtitle-on frames
        # (those with visible text, i.e. non-black content).
        # Timestamps come from ffprobe packets; we filter to only the
        # "display" packets (size > 100 bytes — clear events are ~30 bytes).
        display_packets = [p for p in packets if p.get('_size', p.get('duration', 1)) >= 0]
        # Use the filtered large packets for timing
        large_packets = []
        for pkt in all_packets:
            size = int(pkt.get('size', 0))
            pts = pkt.get('pts_time')
            if size > 100 and pts is not None:
                try:
                    pts_f = float(pts)
                except (ValueError, TypeError):
                    continue
                dur = float(pkt.get('duration_time', 0) or 0)
                large_packets.append({'pts': pts_f, 'duration': dur})

        # Recalculate durations for large packets
        for i, pkt in enumerate(large_packets):
            if pkt['duration'] <= 0:
                if i + 1 < len(large_packets):
                    pkt['duration'] = min(large_packets[i + 1]['pts'] - pkt['pts'], 10.0)
                else:
                    pkt['duration'] = 3.0
            pkt['duration'] = max(0.5, min(pkt['duration'], 15.0))

        # ── Pass A: Filter out blank frames (fast scan) ──
        if progress_callback:
            progress_callback("Filtering blank frames...")

        non_blank = []  # list of (img_path, original_index)
        total_all = len(img_files)
        for i, img_path in enumerate(img_files):
            if cancel_event and cancel_event.is_set():
                if progress_callback:
                    progress_callback("Cancelled during filtering")
                return []
            try:
                img = Image.open(img_path).convert('L')
                img.thumbnail((96, 54))  # fast resize for blank detection
                lo, hi = img.getextrema()
                if hi >= 30:
                    non_blank.append((img_path, i))
            except Exception:
                pass
            if progress_callback and (i % 50 == 0 or i == total_all - 1):
                progress_callback(f"Filtering blank frames... {i+1}/{total_all} "
                                  f"({len(non_blank)} with content)")

        if progress_callback:
            progress_callback(f"Found {len(non_blank)} non-blank frames out of "
                              f"{total_all} — starting OCR...")

        # Pair non-blank frames with display packet timestamps
        total = len(non_blank)
        ocr_jobs = []  # list of (img_path, pts, dur, original_index)
        for j, (img_path, orig_idx) in enumerate(non_blank):
            if j < len(large_packets):
                pts = large_packets[j]['pts']
                dur = large_packets[j]['duration']
            else:
                pts = 0
                dur = 3.0
            ocr_jobs.append((img_path, pts, dur, orig_idx))

        # ── Pass B: Parallel OCR ──
        from concurrent.futures import ThreadPoolExecutor, as_completed
        import threading as _thr

        try:
            max_workers = min(os.cpu_count() or 4, 8)
        except Exception:
            max_workers = 4

        cues = []
        cues_lock = _thr.Lock()
        completed_count = [0]

        def _ocr_one(job):
            """OCR a single subtitle image. Returns (pts, dur, text, img_path) or None."""
            img_path, pts, dur, orig_idx = job
            try:
                img = Image.open(img_path).convert('L')

                # Invert if dark background (white-on-black subtitle)
                lo, hi = img.getextrema()
                if hi < 30:
                    return (pts, dur, '', img_path)  # blank
                # Use mean of extrema as a quick avg proxy
                if (lo + hi) / 2 < 128:
                    img = Image.eval(img, lambda x: 255 - x)

                # Crop to bounding box of non-black content + padding
                bbox = img.getbbox()
                if bbox:
                    pad = 8
                    x1 = max(0, bbox[0] - pad)
                    y1 = max(0, bbox[1] - pad)
                    x2 = min(img.width, bbox[2] + pad)
                    y2 = min(img.height, bbox[3] + pad)
                    img = img.crop((x1, y1, x2, y2))

                    # Save cropped version for preview in monitor window
                    try:
                        img.save(img_path)
                    except Exception:
                        pass

                # Check if this is likely a music note frame
                if _is_music_note_frame(img):
                    return (pts, dur, '♪', img_path)

                text = pytesseract.image_to_string(
                    img, lang=tess_lang,
                    config='--psm 6 -c tessedit_char_blacklist=|'
                ).strip()
                text = _fix_ocr_text(text)
                return (pts, dur, text, img_path)
            except Exception:
                return (pts, dur, '', img_path)

        if progress_callback:
            progress_callback(f"OCR: {total} frames, {max_workers} parallel workers...")

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_job = {}
            for job in ocr_jobs:
                if cancel_event and cancel_event.is_set():
                    break
                future = executor.submit(_ocr_one, job)
                future_to_job[future] = job

            # Collect results as they complete (but we'll sort by timestamp later)
            raw_results = []
            for future in as_completed(future_to_job):
                if cancel_event and cancel_event.is_set():
                    executor.shutdown(wait=False, cancel_futures=True)
                    if progress_callback:
                        progress_callback("OCR cancelled by user")
                    break

                completed_count[0] += 1
                try:
                    result = future.result()
                    pts, dur, text, img_path = result
                    raw_results.append(result)

                    # Notify frame callback
                    if frame_callback:
                        frame_callback(completed_count[0] - 1, total,
                                       img_path, text or '[empty]',
                                       _seconds_to_srt_time(pts),
                                       _seconds_to_srt_time(pts + dur))
                except Exception:
                    completed_count[0]  # already incremented

        # Sort results by timestamp and build cue list
        raw_results.sort(key=lambda r: r[0])  # sort by pts
        for pts, dur, text, img_path in raw_results:
            if text:
                cues.append({
                    'index': len(cues) + 1,
                    'start': _seconds_to_srt_time(pts),
                    'end': _seconds_to_srt_time(pts + dur),
                    'text': text,
                })

        if progress_callback:
            progress_callback(f"OCR complete: {len(cues)} cues extracted from {total} frames")

        return cues

    finally:
        import shutil as _shutil_cleanup
        _shutil_cleanup.rmtree(tmpdir, ignore_errors=True)


def _seconds_to_srt_time(seconds):
    """Convert seconds (float) to SRT timestamp format: HH:MM:SS,mmm"""
    if seconds < 0:
        seconds = 0
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt_file(cues, output_path):
    """Write a list of SRT cue dicts to an SRT file.
    Each cue: {'index': 1, 'start': '00:01:23,456', 'end': '00:01:26,789', 'text': 'Hello'}"""
    with open(output_path, 'w', encoding='utf-8') as f:
        for cue in cues:
            f.write(f"{cue['index']}\n")
            f.write(f"{cue['start']} --> {cue['end']}\n")
            f.write(f"{cue['text']}\n\n")


def _is_music_note_frame(img):
    """Detect if a subtitle image likely contains only music notes (♪/♫).
    Music note frames have small, isolated content with very few non-black pixels
    compared to normal text subtitles."""
    try:
        w, h = img.size
        total_pixels = w * h
        if total_pixels == 0:
            return False
        # Count non-white pixels (after inversion, text is dark on white)
        pixels = list(img.getdata())
        dark_pixels = sum(1 for p in pixels if p < 128)
        dark_ratio = dark_pixels / total_pixels
        # Music notes: very small content area (< 3% of frame)
        # and narrow width (< 15% of original 1920px frame)
        if dark_ratio < 0.03 and w < 300:
            return True
        # Also check: very few dark pixels total (music notes are tiny)
        if dark_pixels < 500 and w < 400:
            return True
    except Exception:
        pass
    return False


def _fix_ocr_text(text):
    """Fix common Tesseract OCR mistakes in subtitle text."""
    if not text:
        return text

    # Fix | and / first (convert to letters before I/l rules run)
    # Replace | with I everywhere (pipe never appears in subtitles)
    text = text.replace('|', 'I')

    # Fix / and // misread as I, l, ll and /7 as I'l (7 = apostrophe shape)
    # Order matters: fix multi-char patterns first, then single /

    # Fix / and // misread as I, l, ll and /7 as I'l (7 = apostrophe shape)
    # Order matters: fix longest/most-specific patterns first

    # /17/ → I'll: So /17/ be back → So I'll be back
    text = re.sub(r'/17/', "I'll", text)

    # 17/I → I'll: And 17/I stand → And I'll stand
    text = re.sub(r'17/I', "I'll", text)
    # 17/ → I'll (1=I, 7=', /=l): And 17/ want → And I'll want
    text = re.sub(r'17/', "I'll", text)

    # /7/ → I'll: /7/ care → I'll care
    text = re.sub(r'/7/', "I'll", text)
    # /711 → I'll: /711 write → I'll write
    text = re.sub(r'/711', "I'll", text)
    # /71 → I'll
    text = re.sub(r'/71\b', "I'll", text)
    # /7 before space → I'll: /7 help → I'll help
    text = re.sub(r'/7\s+', "I'll ", text)

    # // → ll (double slash = double l): we// → well, ev//. → evil.
    text = re.sub(r'//', 'll', text)

    # /1 → Il
    text = re.sub(r'/1', 'Il', text)

    # /[ → I (bracket misread): /[s → Is
    text = re.sub(r'/\[', 'I', text)

    # /I → I (slash-I = garbled I): /I could → I could
    text = re.sub(r'(?<![a-zA-Z0-9])/I\b', 'I', text)

    # Standalone / as a word → I: "/ have" → "I have", "/ swear" → "I swear"
    text = re.sub(r'(?<![a-zA-Z0-9/])/(?![a-zA-Z0-9/])', 'I', text)

    # / after uppercase letter at word boundary → l: A/ → Al
    text = re.sub(r'(?<=[A-Z])/(?=\s)', 'l', text)

    # / between letters → l: G/inda → Glinda, specu/ation → speculation
    text = re.sub(r'(?<=[a-zA-Z])/(?=[a-z])', 'l', text)
    # / at start of word before lowercase → l (then l→I rules fix if needed)
    text = re.sub(r'(?<![a-zA-Z0-9])/(?=[a-z])', 'l', text)

    # Fix 1 misread as I — only in word/letter context, not in actual numbers
    text = re.sub(r'(?<![0-9a-zA-Z])1(?![0-9a-zA-Z\-])', 'I', text)  # standalone
    text = re.sub(r'(?<![0-9])1(?=[\'\']\s*[a-z])', 'I', text)       # 1'm → I'm
    text = re.sub(r'^1(?= [a-z])', 'I', text, flags=re.MULTILINE)    # 1 am → I am
    text = re.sub(r'(?<![0-9a-zA-Z])1(?=[tTfFnNsS][^0-9])', 'I', text)  # 1t → It
    text = re.sub(r'(?<![0-9a-zA-Z])1(?=t\'s)', 'I', text)           # 1t's → It's

    # Fix ! misread as I in common patterns
    text = re.sub(r'!\s*(?=[\'\']\s*[a-z])', 'I', text)     # !'m !'ll !'ve !'d
    text = re.sub(r'(?<!\w)!(?=[tf]\s)', 'I', text)          # !t !f at word boundary
    text = re.sub(r'(?<!\w)!(?=t\'s)', 'I', text)            # !t's → It's
    text = re.sub(r'(?<!\w)!(?=n\b)', 'I', text)             # !n → In
    text = re.sub(r'(?<!\w)!(?=s\b)', 'I', text)             # !s → Is
    text = re.sub(r'(?<!\w)!(?= [a-z])', 'I', text)          # ! followed by space + lowercase

    # Fix l/I confusion — l at start of sentence or standalone should be I
    text = re.sub(r'^l(?= [a-z])', 'I', text, flags=re.MULTILINE)     # l am → I am
    text = re.sub(r'^l(?=[\'\']\s*[a-z])', 'I', text, flags=re.MULTILINE)  # l'm → I'm
    text = re.sub(r'(?<!\w)l(?!\w)', 'I', text)              # standalone l → I
    # l before common word-starts when l is at word boundary: lt's → It's, ls → Is, ln → In
    text = re.sub(r'(?<!\w)l(?=t\')', 'I', text)             # lt's → It's
    text = re.sub(r'(?<!\w)l(?=[snf]\b)', 'I', text)         # ls → Is, ln → In, lf → If
    text = re.sub(r'(?<!\w)l(?=[snf] )', 'I', text)          # ls dead → Is dead

    # Fix ™ misread as apostrophe: I'™m → I'm
    text = re.sub(r"'™", "'", text)   # '™ → ' (avoid double apostrophe)
    text = text.replace('™', "'")      # standalone ™ → '

    # ── Music note (♪) detection ──
    # Tesseract misreads ♪ as: 2 > $ & £ © » # * ? Sf D> P If at start/end of lines

    # End-of-line garbled ♪: Sf, D>, P, If, f (various misreadings)
    text = re.sub(r'\s+[SD][f>]\s*$', ' ♪', text, flags=re.MULTILINE)  # Sf, D>
    text = re.sub(r'\s+P\s*$', ' ♪', text, flags=re.MULTILINE)         # trailing P
    text = re.sub(r'\s+If\s*$', ' ♪', text, flags=re.MULTILINE)        # trailing If
    text = re.sub(r'\s+f\s*$', ' ♪', text, flags=re.MULTILINE)         # trailing f

    # Fix $f / £f ligature (garbled ♪♪ or ♪): replace with ♪
    text = re.sub(r'[\$£]f\b', '♪', text)

    # Fix -) at start of line (misread -♪)
    text = re.sub(r'^-\)\s*', '-♪ ', text, flags=re.MULTILINE)

    # Music note marker after [Speaker] brackets: [Ozians] $ text → [Ozians] ♪ text
    text = re.sub(r'(\])\s*[2>$&£©»#*?]+\s*', r'\1 ♪ ', text)

    # Start-of-line markers: 2, >, $, &, £, ©, », #, *, ? (with optional leading -)
    # Allow marker to be directly attached to text (no space): >And → ♪ And
    _MUSIC_START = r'^-?[2>$&£©»#*?]+\s*(?=[A-Za-z\'\'"/])'  # ♪ at start (optional space)
    _MUSIC_END   = r'\s+[>£&©$»#*]\s*$'                 # ♪ at end of line

    # Replace music note markers at start and end of lines
    text = re.sub(_MUSIC_START, '♪ ', text, flags=re.MULTILINE)
    text = re.sub(_MUSIC_END, ' ♪', text, flags=re.MULTILINE)

    # Detect garbled music note OCR output — entire cue is just garbage chars
    stripped = text.strip()
    if len(stripped) <= 3 and stripped and all(c in 'Jjd}]){><%#@~^*_=2$&£©»♪ ' for c in stripped):
        return '♪'

    # Clean up common OCR artifacts
    text = re.sub(r'\s{2,}', ' ', text)          # collapse multiple spaces
    text = re.sub(r'^\s+|\s+$', '', text, flags=re.MULTILINE)  # trim lines

    return text.strip()


def detect_closed_captions(filepath):
    """Detect ATSC A53 closed captions (EIA-608/CEA-708) embedded in video frame side data.
    Returns True if CC data is found, False otherwise.
    These are common in MPEG-2 transport stream (.ts) HDTV recordings."""
    try:
        cmd = [
            'ffprobe', '-v', 'quiet',
            '-read_intervals', '%+#30',   # read only first 30 frames (fast)
            '-show_entries', 'frame=side_data_list:side_data=side_data_type',
            '-print_format', 'json',
            '-select_streams', 'v:0',
            filepath
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return False
        return 'ATSC A53' in result.stdout or 'Closed Captions' in result.stdout
    except Exception:
        return False


def extract_closed_captions_to_srt(filepath, output_srt_path, timeout=None):
    """Extract ATSC A53 closed captions to SRT using ccextractor (if available).
    Returns True on success (and output file has content), False otherwise.
    timeout is calculated from video duration if not provided."""
    import shutil
    if not shutil.which('ccextractor'):
        return False
    try:
        if timeout is None:
            dur = get_video_duration(filepath)
            # Allow roughly 1/4 of real-time plus a generous base
            timeout = max(120, int(dur * 0.25) + 60) if dur else 600
        cmd = ['ccextractor', filepath, '-o', output_srt_path, '--no_progress_bar', '-utf8']
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if os.path.exists(output_srt_path) and os.path.getsize(output_srt_path) > 10:
            return True
        return False
    except Exception:
        return False


# Encoder flags to enable A53 CC passthrough (embedded in video bitstream)
_A53CC_ENCODER_FLAGS = {
    'libx264':     [],            # a53cc defaults to true
    'libx265':     ['-a53cc', '1'],
    'hevc_nvenc':  ['-a53cc', '1'],
    'h264_nvenc':  ['-a53cc', '1'],
    'hevc_qsv':    [],            # uses -sei a53_cc which is on by default
    'h264_qsv':    [],
    'hevc_vaapi':  [],            # uses -sei a53_cc which is on by default
    'h264_vaapi':  [],
}


def get_video_pix_fmt(filepath):
    """Return the pixel format string of the first video stream (e.g. 'yuv420p', 'yuv420p10le')."""
    try:
        cmd = [
            'ffprobe', '-v', 'error', '-select_streams', 'v:0',
            '-show_entries', 'stream=pix_fmt',
            '-of', 'default=noprint_wrappers=1:nokey=1',
            filepath
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# SRT Parser & Subtitle Filters
# ═══════════════════════════════════════════════════════════════════════════════

def parse_srt(text):
    """Parse SRT subtitle text into a list of cue dicts.

    Each cue: {'index': int, 'start': str, 'end': str, 'text': str}
    Timestamps are kept as original strings (HH:MM:SS,mmm).
    """
    cues = []
    blocks = re.split(r'\n\n+', text.strip())
    for block in blocks:
        lines = block.strip().split('\n')
        if len(lines) < 2:
            continue
        # First line should be the index number
        try:
            idx = int(lines[0].strip())
        except ValueError:
            # Sometimes the index is missing; generate one
            idx = len(cues) + 1
            # Try parsing this line as timestamp instead
            if '-->' in lines[0]:
                lines = ['0'] + lines  # dummy index
                idx = len(cues) + 1
            else:
                continue
        # Second line: timestamp
        ts_match = re.match(
            r'(\d{2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,.]\d{3})',
            lines[1].strip()
        )
        if not ts_match:
            continue
        start, end = ts_match.group(1), ts_match.group(2)
        # Remaining lines: subtitle text
        sub_text = '\n'.join(lines[2:])
        cues.append({'index': idx, 'start': start, 'end': end, 'text': sub_text})
    return cues


def write_srt(cues):
    """Convert a list of cue dicts back to SRT format string."""
    parts = []
    for i, cue in enumerate(cues, 1):
        parts.append(f"{i}\n{cue['start']} --> {cue['end']}\n{cue['text']}\n")
    return '\n'.join(parts)


def filter_remove_hi(cues):
    """Remove hearing-impaired annotations and speaker labels.

    Removes: [brackets], (parentheses), speaker labels (Name:),
    and ALL CAPS HI descriptor labels (HIGH-PITCHED:, MUFFLED:, etc.)
    where the entire line is removed since the content after the label
    is part of the sound description, not dialogue.
    """
    hi_patterns = [
        re.compile(r'\[.*?\]', re.DOTALL),          # [music playing] — including multi-line
        re.compile(r'^\[(?!.*\]).*', re.DOTALL),    # unclosed [ at start — remove entire cue text
        re.compile(r'\(.*?\)', re.DOTALL),          # (laughing) — including multi-line
    ]
    # Speaker label pattern (same as filter_remove_speaker_labels)
    speaker_pattern = re.compile(r'^(-?\s*)[A-Za-z][A-Za-z\s\d\'\.]{0,29}[A-Za-z]:\s*\n?', re.MULTILINE)

    # ALL CAPS HI descriptor labels — these describe sounds/actions, not speakers.
    # Only the label up to and including the colon is removed; text after is kept.
    # Matches: HIGH-PITCHED: ..., MUFFLED: ..., OFF-SCREEN: ..., DEEP VOICE: ...
    # Each ALL CAPS word must be 4+ letters or contain a hyphen.
    # This preserves short acronyms (FBI:, BBC:, NHS:, CIA:, MR:) while
    # catching HI descriptors (MUFFLED:, NARRATOR:, HIGH-PITCHED:, DEEP VOICE:).
    caps_hi_label = re.compile(
        r'^(-?\s*)(?:[A-Z]{4,}|[A-Z][A-Z\-]*-[A-Z\-]*)(?:\s+(?:[A-Z]{4,}|[A-Z][A-Z\-]*-[A-Z\-]*))*:\s*',
        re.MULTILINE
    )

    def _speaker_replace(m):
        label = m.group(0).lstrip('- ')
        name_part = label.split(':')[0].strip()
        # Protect pure numbers (e.g. timestamps like "2:30")
        if re.match(r'^\d+$', name_part):
            return m.group(0)
        # Protect single-character labels
        if len(name_part) <= 1:
            return m.group(0)
        return m.group(1)

    # Reuse _is_caps_hi_line from filter_remove_caps_hi to also catch
    # ALL CAPS HI lines (UK style) in one pass
    caps_hi_checker = _build_caps_hi_checker()

    result = []
    for cue in cues:
        text = cue['text']
        # Remove ALL CAPS HI descriptor labels, keep text after the colon
        text = caps_hi_label.sub(r'\1', text)
        for pat in hi_patterns:
            text = pat.sub('', text)
        # Remove speaker labels
        text = speaker_pattern.sub(_speaker_replace, text)
        # Remove ALL CAPS HI lines (UK style — e.g. SHEENA LAUGHS, THANK YOU.)
        lines = text.split('\n')
        lines = [line for line in lines if not caps_hi_checker(line)]
        text = '\n'.join(lines)
        # Clean up orphaned colons left after HI removal
        # "Speaker (description): text" → "Speaker : text" after (description) removed
        # Remove "word(s) :" speaker label remnants where parens were stripped
        text = re.sub(r'^(-?\s*)[A-Za-z][A-Za-z\s\d\'\.]{0,29}[A-Za-z]\s+:\s*', r'\1', text, flags=re.MULTILINE)
        # Colon at start of line: "(gasps): text" → ": text" → "text"
        text = re.sub(r'^\s*:\s*', '', text, flags=re.MULTILINE)
        text = re.sub(r'\n\s*:\s*', '\n', text)
        # Dash + colon: "-(whispers): text" → "-: text" → "- text"
        text = re.sub(r'^(-\s*):\s*', r'\1', text, flags=re.MULTILINE)
        # Clean up leftover whitespace and blank lines
        text = re.sub(r'^\s*-?\s*$', '', text, flags=re.MULTILINE)
        text = re.sub(r'\n{2,}', '\n', text)
        text = re.sub(r'^\n+', '', text)
        text = text.strip()
        if text:
            result.append({**cue, 'text': text})
    return result


# ── Shared ALL CAPS HI line detection ──
# Used by both filter_remove_hi and filter_remove_caps_hi.

# Single-word HI terms that should be removed even as standalone words
_CAPS_HI_SINGLE_WORDS = {
    'applause', 'laughter', 'laughing', 'laughs', 'chuckling', 'chuckles',
    'giggling', 'giggles', 'snickering', 'sniggering',
    'screaming', 'screams', 'shrieking', 'shrieks', 'shriek',
    'crying', 'cries', 'sobbing', 'sobs', 'weeping', 'weeps',
    'gasping', 'gasps', 'groaning', 'groans', 'moaning', 'moans',
    'sighing', 'sighs', 'panting', 'pants',
    'coughing', 'coughs', 'sneezing', 'sneezes', 'sniffing', 'sniffs',
    'whispering', 'whispers', 'muttering', 'mutters', 'mumbling', 'mumbles',
    'shouting', 'shouts', 'yelling', 'yells', 'exclaiming', 'exclaims',
    'stuttering', 'stutters', 'stammering', 'stammers',
    'silence', 'inaudible', 'indistinct', 'unintelligible',
    'music', 'singing', 'humming', 'whistling', 'chanting',
    'cheering', 'cheers', 'booing', 'boos', 'jeering',
    'thunder', 'explosion', 'gunshot', 'gunshots', 'gunfire',
    'sirens', 'alarm', 'buzzing', 'ringing', 'beeping', 'bleeping',
    'knocking', 'banging', 'crashing', 'thudding', 'thumping',
    'squeaking', 'creaking', 'rustling', 'clattering', 'rattling',
    'splashing', 'dripping', 'sizzling', 'bubbling',
    'doorbell', 'telephone', 'ringtone',
    'snoring', 'yawning', 'hiccupping', 'hiccups', 'belching', 'retching',
    'growling', 'barking', 'howling', 'whimpering', 'purring', 'meowing',
    'neighing', 'chirping', 'squawking',
    'clapping', 'footsteps', 'static', 'feedback', 'interference',
    'continues', 'resumes', 'stops', 'ends', 'fades',
}

# Acronyms and short words to always preserve (even if all-caps line)
_CAPS_HI_KEEP_WORDS = {
    # Common acronyms
    'ok', 'okay', 'no', 'oh', 'hi', 'hey', 'yes', 'yeah', 'god', 'oi',
    'ha', 'ah', 'uh', 'hm', 'mm', 'sh', 'shh', 'psst', 'wow', 'boo',
    # Known acronyms / initialisms
    'fbi', 'cia', 'nsa', 'dea', 'atf', 'nypd', 'lapd', 'swat',
    'nasa', 'nato', 'un', 'eu', 'uk', 'usa', 'us',
    'bbc', 'itv', 'cnn', 'nbc', 'cbs', 'abc', 'hbo', 'pbs', 'nhs',
    'ceo', 'cfo', 'cto', 'vip', 'rip', 'awol', 'mia', 'pow',
    'dna', 'hiv', 'aids', 'icu', 'cpr', 'gps', 'eta', 'asap',
    'tv', 'pc', 'dj', 'mc', 'id', 'iq', 'phd', 'md',
    'mph', 'rpm', 'atm', 'suv', 'ufo', 'aka',
    'nyc', 'la', 'dc', 'sf',
}


def _is_caps_hi_line(line):
    """Determine if a line is an ALL CAPS HI description.

    Multi-word all-caps lines (2+ words) are removed.
    Single all-caps words are only removed if they match known HI keywords.
    Short words (≤3 chars) and known acronyms are always kept.
    """
    stripped = line.strip()
    if not stripped:
        return False

    # Remove leading dash/hyphen for analysis
    clean = re.sub(r'^-\s*', '', stripped)
    if not clean:
        return False

    # Get just the letter content to check if it's all uppercase
    letters = re.sub(r'[^a-zA-Z]', '', clean)
    if not letters:
        return False

    # Must be ALL CAPS (every letter is uppercase)
    if not letters.isupper():
        return False

    # Split into words
    words = clean.split()

    # Single word — only remove if it's a known HI keyword
    if len(words) == 1:
        word_lower = letters.lower()
        if len(letters) <= 3:
            return False
        if word_lower in _CAPS_HI_KEEP_WORDS:
            return False
        return word_lower in _CAPS_HI_SINGLE_WORDS

    # Multi-word all-caps line
    # Check if ALL words are known acronyms/keep-words — if so, preserve
    all_words_lower = [re.sub(r'[^a-z]', '', w.lower()) for w in words]
    all_words_lower = [w for w in all_words_lower if w]
    if all_words_lower and all(w in _CAPS_HI_KEEP_WORDS for w in all_words_lower):
        return False

    # Multi-word all-caps line that isn't all acronyms → remove
    return True


def _build_caps_hi_checker():
    """Return the _is_caps_hi_line function for use by other filters."""
    return _is_caps_hi_line


def filter_remove_caps_hi(cues):
    """Remove ALL CAPS hearing-impaired descriptions common in UK subtitles.

    Targets entire lines that are ALL CAPS and describe actions or sounds, e.g.:
      'SHEENA LAUGHS', 'DOOR SLAMS SHUT', 'DRAMATIC MUSIC', 'APPLAUSE'

    Rules:
      - Multi-word all-caps lines (2+ words) are removed — these are almost
        always HI descriptions (e.g. 'HE SIGHS', 'TENSE MUSIC PLAYS').
      - Single all-caps words are only removed if they match known HI keywords
        (e.g. 'APPLAUSE', 'LAUGHTER', 'SILENCE').
      - Short words (≤3 chars) like OK, NO, OH, YES, HI are always kept.
      - Known acronyms (FBI, BBC, NASA, etc.) are always kept.
      - Lines with mixed case are never touched.
      - Processes line-by-line within each cue, so mixed cues like:
          'She can get free trams.\\nSHEENA LAUGHS'
        become: 'She can get free trams.'
    """
    result = []
    for cue in cues:
        lines = cue['text'].split('\n')
        kept_lines = [line for line in lines if not _is_caps_hi_line(line)]
        text = '\n'.join(kept_lines).strip()
        # Clean up leftover blank lines and orphaned dashes
        text = re.sub(r'^\s*-?\s*$', '', text, flags=re.MULTILINE)
        text = re.sub(r'\n{2,}', '\n', text).strip()
        if text:
            result.append({**cue, 'text': text})
    return result


def filter_remove_music_notes(cues):
    """Remove cues that contain only music note symbols (♪ ♫) and whitespace/dashes.

    Keeps cues that have actual lyrics or dialogue alongside the notes.
    """
    result = []
    for cue in cues:
        stripped = re.sub(r'[♪♫\s\-]', '', cue['text'])
        if stripped:  # has real text, keep it
            result.append(cue)
    return result


# ── Proper nouns for case conversion ──
# Words that should always be capitalized after converting from ALL CAPS
PROPER_NOUNS = {
    # Days
    'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday',
    # Months
    'january', 'february', 'march', 'april', 'may', 'june',
    'july', 'august', 'september', 'october', 'november', 'december',
    # Holidays
    'christmas', 'easter', 'halloween', 'thanksgiving', 'hanukkah',
    'kwanzaa', 'valentines', "valentine's", 'ramadan', 'diwali',
    'passover', 'new year', "new year's", "mother's", "father's",
    # Countries (common)
    'america', 'american', 'americans', 'england', 'english',
    'france', 'french', 'germany', 'german', 'italy', 'italian',
    'spain', 'spanish', 'china', 'chinese', 'japan', 'japanese',
    'russia', 'russian', 'canada', 'canadian', 'mexico', 'mexican',
    'australia', 'australian', 'india', 'indian', 'brazil', 'brazilian',
    'korea', 'korean', 'ireland', 'irish', 'scotland', 'scottish',
    'africa', 'african', 'europe', 'european', 'asia', 'asian',
    'british', 'britain', 'uk', 'usa',
    # US States (common in dialogue)
    'california', 'texas', 'florida', 'new york', 'york', 'new jersey', 'jersey',
    'massachusetts', 'virginia', 'carolina', 'georgia', 'ohio',
    'michigan', 'illinois', 'pennsylvania', 'arizona', 'colorado',
    'washington', 'oregon', 'nevada', 'hawaii', 'alaska',
    'montana', 'connecticut', 'louisiana', 'tennessee', 'kentucky',
    'minnesota', 'mississippi', 'alabama', 'oklahoma', 'wisconsin',
    'maryland', 'missouri',
    # Cities (common)
    'london', 'paris', 'tokyo', 'beijing', 'moscow', 'berlin',
    'rome', 'madrid', 'sydney', 'toronto', 'chicago', 'boston',
    'miami', 'seattle', 'dallas', 'denver', 'atlanta', 'detroit',
    'houston', 'phoenix', 'vegas', 'portland', 'hollywood',
    'manhattan', 'brooklyn', 'queens', 'bronx', 'harlem',
    # Common abbreviations / titles
    'mr', 'mrs', 'ms', 'dr', 'jr', 'sr', 'st', 'mt',
    'ave', 'blvd', 'dept', 'sgt', 'cpl', 'pvt', 'lt', 'capt',
    'gen', 'col', 'cmdr', 'prof', 'rev', 'hon',
    # Address / place words (capitalised when part of a name)
    'street', 'avenue', 'road', 'drive', 'lane', 'boulevard',
    'court', 'place', 'terrace', 'highway', 'parkway', 'plaza',
    'bridge', 'park', 'lake', 'river', 'mountain', 'island',
    'north', 'south', 'east', 'west',
    # Religious / cultural
    'god', 'jesus', 'christ', 'bible', 'catholic', 'christian',
    'muslim', 'islam', 'jewish', 'buddhist', 'hindu',
    # Other proper nouns common in subtitles
    'internet', 'facebook', 'google', 'twitter', 'instagram',
    'youtube', 'netflix', 'amazon', 'apple', 'microsoft',
    'fbi', 'cia', 'nsa', 'dea', 'atf', 'nypd', 'lapd',
    'nasa', 'nato', 'un', 'eu',
}


def filter_fix_caps(cues, custom_names=None):
    """Convert ALL CAPS subtitles to proper sentence case.

    - Lowercases everything first
    - Capitalizes first letter of each sentence/line
    - Capitalizes standalone "I" and contractions (I'm, I'll, I've, I'd)
    - Capitalizes known proper nouns (days, months, countries, etc.)
    - Capitalizes custom names if provided

    custom_names: optional set/list of additional words to capitalize
    """
    all_proper = set(PROPER_NOUNS)
    if custom_names:
        all_proper.update(w.lower() for w in custom_names)

    # Build a regex that matches any proper noun as a whole word
    # Sort by length descending so longer matches take priority
    sorted_nouns = sorted(all_proper, key=len, reverse=True)
    # Separate multi-word phrases from single words
    phrases = [n for n in sorted_nouns if ' ' in n]
    words = [n for n in sorted_nouns if ' ' not in n]

    def fix_case(text, cap_first=True):
        # Only process lines that are mostly uppercase
        alpha = re.sub(r'[^a-zA-Z]', '', text)
        if not alpha:
            return text
        upper_ratio = sum(1 for c in alpha if c.isupper()) / len(alpha)
        if upper_ratio < 0.6:
            return text  # not all-caps, leave it alone

        # Step 1: lowercase everything
        text = text.lower()

        # Step 2: capitalize first letter of each line, but only if it's a
        # sentence start.  The first line is capitalized only if cap_first
        # is True (i.e. the previous cue ended with punctuation).  Subsequent
        # lines are capitalized only when:
        #   - The previous line ended with sentence-ending punctuation (.!?)
        #   - The line starts with a dash (dialogue from a different speaker)
        lines = text.split('\n')
        capped_lines = []
        for idx, line in enumerate(lines):
            line = line.strip()
            if line:
                is_first_line = (idx == 0)
                prev_ended_sentence = (idx > 0 and capped_lines
                    and re.search(r'[.!?]["\'\u201d\u2019]?\s*$', capped_lines[-1]))
                starts_with_dash = line.startswith('-')

                should_cap = starts_with_dash or prev_ended_sentence
                if is_first_line:
                    should_cap = cap_first or starts_with_dash

                if should_cap:
                    line = re.sub(r'^(-\s*)?([a-z])',
                                  lambda m: (m.group(1) or '') + m.group(2).upper(), line)
            capped_lines.append(line)
        text = '\n'.join(capped_lines)

        # Step 3: capitalize after sentence-ending punctuation (including after quotes)
        text = re.sub(r'([.!?]["\'\u201d\u2019]?[\s]+)([a-z])',
                      lambda m: m.group(1) + m.group(2).upper(), text)

        # Step 4: capitalize standalone "I" and contractions
        text = re.sub(r"\bi\b", "I", text)
        text = re.sub(r"\bi'(m|ll|ve|d|s)\b", lambda m: "I'" + m.group(1), text)

        # Step 5: capitalize multi-word proper noun phrases
        for phrase in phrases:
            pattern = re.compile(re.escape(phrase), re.IGNORECASE)
            text = pattern.sub(phrase.title(), text)

        # Step 6: capitalize single-word proper nouns
        # Abbreviations that should be ALL CAPS (not title case)
        _ALLCAPS_ABBREVS = {
            'fbi', 'cia', 'nsa', 'dea', 'atf', 'nypd', 'lapd',
            'nasa', 'nato', 'un', 'eu', 'uk', 'usa', 'tv', 'dna',
            'ceo', 'cfo', 'cto', 'phd', 'md', 'dj', 'pc', 'id',
            'ok', 'ad', 'bc', 'ac', 'dc', 'hq',
        }

        def _cap_word(m):
            word = m.group(0)
            lower = word.lower()
            # Check all-caps abbreviations first
            if lower in _ALLCAPS_ABBREVS:
                return lower.upper()
            # Check proper nouns
            if lower in all_proper:
                return word.capitalize()
            return word

        text = re.sub(r'\b[a-zA-Z]+\b', _cap_word, text)

        return text

    def apply_custom_names(text):
        """Capitalize custom names even in already-converted (non-all-caps) text.

        This runs as a second pass so that names added after an initial
        Fix ALL CAPS run still get capitalized.
        """
        if not custom_names:
            return text

        # Multi-word custom phrases first
        custom_phrases = [n for n in custom_names if ' ' in n]
        for phrase in custom_phrases:
            pattern = re.compile(re.escape(phrase), re.IGNORECASE)
            text = pattern.sub(phrase.title(), text)

        # Single-word custom names
        custom_single = {n.lower(): n for n in custom_names if ' ' not in n}
        if custom_single:
            def _cap_custom(m):
                word = m.group(0)
                original = custom_single.get(word.lower())
                if original:
                    return original  # use the exact casing the user entered
                return word
            text = re.sub(r'\b[a-zA-Z]+\b', _cap_custom, text)

        return text

    result = []
    prev_text = ''
    for cue in cues:
        text = cue['text']

        # Check if this cue continues a sentence from the previous cue.
        # If the previous cue didn't end with sentence-ending punctuation,
        # don't capitalize the first word of this cue.
        prev_ended_sentence = (not prev_text
            or bool(re.search(r'[.!?]["\'\u201d\u2019]?\s*$', prev_text)))

        text = fix_case(text, cap_first=prev_ended_sentence)

        # Always fix sentence-start capitalization, even if fix_case skipped
        # (text wasn't all-caps).  This handles second-pass runs where the
        # user adds custom names after an initial Fix ALL CAPS.
        if prev_ended_sentence:
            # Capitalize first letter of the cue (respecting leading dashes)
            text = re.sub(r'^(-\s*)?([a-z])',
                          lambda m: (m.group(1) or '') + m.group(2).upper(), text)

        # Always apply custom names, even if fix_case skipped (text wasn't all-caps)
        text = apply_custom_names(text)
        prev_text = text
        result.append({**cue, 'text': text})
    return result


def filter_remove_tags(cues):
    """Remove HTML/formatting tags: <i>, </i>, <b>, <font ...>, {\\an8}, etc."""
    tag_patterns = [
        re.compile(r'<[^>]+>'),           # HTML tags
        re.compile(r'\{\\[^}]+\}'),       # ASS override tags like {\an8}
    ]
    result = []
    for cue in cues:
        text = cue['text']
        for pat in tag_patterns:
            text = pat.sub('', text)
        text = text.strip()
        if text:
            result.append({**cue, 'text': text})
    return result


# Built-in ad/credit patterns (always present)
BUILTIN_AD_PATTERNS = [
    r'subtitl(es|ed)\s+by\b.*',
    r'synced?\s*((&|and)\s*corrected)?\s+by\b.*',
    r'caption(s|ed|ing)?\s+by\b.*',
    r'translated\s+by\b.*',
    r'corrections?\s+by\b.*',
    r'encoded\s+by\b.*',
    r'ripped\s+by\b.*',
    r'opensubtitles\S*',
    r'addic7ed\S*',
    r'subscene\S*',
]


def filter_remove_ads(cues, custom_patterns=None):
    """Remove common ad/credit lines from subtitles.

    custom_patterns: optional list of additional pattern strings (case-insensitive).
    URL lines (www.*) are only removed if the cue also contains another
    ad indicator — this avoids stripping real dialogue that mentions websites.
    """
    all_pattern_strs = list(BUILTIN_AD_PATTERNS)
    if custom_patterns:
        all_pattern_strs.extend(custom_patterns)

    ad_patterns = []
    for p in all_pattern_strs:
        try:
            ad_patterns.append(re.compile(r'(?i)^\s*' + p + r'\s*$', re.MULTILINE))
        except re.error:
            pass  # skip invalid patterns

    # URL pattern — only applied when cue is already flagged as an ad
    url_pattern = re.compile(r'(?i)^\s*(?:https?://|www\.)\S+\s*$', re.MULTILINE)
    # Quick check to detect if a cue has any ad content
    ad_check_parts = [
        r'(subtitl(es|ed)|synced?|caption(s|ed|ing)?|translated|corrections?|encoded|ripped)\s+by\b',
        r'opensubtitles', r'addic7ed', r'subscene',
    ]
    if custom_patterns:
        for p in custom_patterns:
            try:
                re.compile(p)  # validate
                ad_check_parts.append(p)
            except re.error:
                pass
    ad_check = re.compile(r'(?i)(' + '|'.join(ad_check_parts) + r')')

    result = []
    for cue in cues:
        text = cue['text']
        has_ad = bool(ad_check.search(text))
        for pat in ad_patterns:
            text = pat.sub('', text)
        # Only strip URLs if this cue had other ad content, or if the
        # entire cue is nothing but a URL (no other dialogue)
        if has_ad or not re.sub(r'(?i)(?:https?://|www\.)\S+', '', text).strip():
            text = url_pattern.sub('', text)
        text = re.sub(r'\n{2,}', '\n', text).strip()
        if text:
            result.append({**cue, 'text': text})
    return result


def filter_remove_offscreen_quotes(cues):
    """Remove wrapping single quotes used for off-screen dialogue (UK style).

    UK subtitles often wrap lines in single quotes when the speaker is
    off-screen.  The quotes may span across cues, so a cue might have:
      - Both opening and closing:  'She said hello.'
      - Only opening (speech continues in next cue):  'She said hello
      - Only closing (speech started in previous cue):  and goodbye.'
      - Continuation with opening only:  'and then she left

    Rules:
      - Opening and closing quotes are handled independently — they don't
        need to appear as a pair within the same line.
      - Opening ' is a wrapping quote unless the word after it is a known
        contraction ('cause, 'til, 'bout, 'cos, 'em, etc.).
      - Closing ' is a wrapping quote if NOT preceded by a letter
        (preserves dropped-g words like somethin', thinkin', nothin').
      - Internal apostrophes (don't, she's, I'm) are never affected.
    """
    # Words that form contractions with a leading apostrophe — keep the '
    CONTRACTION_WORDS = {
        'cause', 'cos', 'coz', 'til', 'bout', 'em', 'im',
        'twas', 'tis', 'neath', 'ere', 'appen',
        'ave', 'alf', 'ad', 'ow', 'owt', 'nowt',  # UK dialect
    }

    result = []
    for cue in cues:
        lines = cue['text'].split('\n')
        cleaned = []
        for line in lines:
            stripped = line.strip()
            # Separate leading dash if present
            leading = ''
            inner = stripped
            m = re.match(r'^(-\s*)', inner)
            if m:
                leading = m.group(1)
                inner = inner[len(leading):]

            # ── Opening quote ──
            if inner and inner[0] == "'":
                # Get the first word after the apostrophe
                word_match = re.match(r"'([a-zA-Z]+)", inner)
                if word_match:
                    first_word = word_match.group(1).lower()
                    # Only keep the ' if it's a known contraction
                    if first_word not in CONTRACTION_WORDS:
                        inner = inner[1:]  # strip opening '
                else:
                    # ' followed by space or non-letter — wrapping quote
                    inner = inner[1:]

            # ── Closing quote ──
            if inner and inner[-1] == "'":
                # Wrapping quote if NOT preceded by a letter
                # (letter before ' = contraction like somethin', nothin')
                if not inner[-2:-1].isalpha():
                    inner = inner[:-1]

            cleaned.append(leading + inner)

        text = '\n'.join(cleaned).strip()
        if text:
            result.append({**cue, 'text': text})
    return result


def filter_remove_speaker_labels(cues):
    """Remove speaker name labels from start of lines.

    Matches optional dash, then 1-3 words (any case) followed by a colon,
    as long as it's near the start of the line (≤30 chars before the colon)
    and NOT a time-like pattern (e.g. '2:30', '12:00').
    Examples removed: 'John:', 'MARY:', '- Detective Smith:', 'man 1:'
    Examples kept: '2:30', 'Wait: what?', 'At 12:00 we left'
    """
    # Match: optional dash/space, then letters (with spaces/numbers for "Man 1")
    # up to 30 chars, ending with colon+space — but first char must be a letter
    pattern = re.compile(r'^(-?\s*)[A-Za-z][A-Za-z\s\d\'\.]{0,29}[A-Za-z]:\s*\n?', re.MULTILINE)
    result = []
    for cue in cues:
        text = cue['text']
        # Apply pattern but verify each match isn't a time or single short word
        def _replace(m):
            label = m.group(0).lstrip('- ')
            name_part = label.split(':')[0].strip()
            # Skip if it looks like a time (digits with colon, or ends with digit)
            if re.match(r'^\d+$', name_part):
                return m.group(0)  # pure number before colon (e.g. "12:")
            if re.search(r'\d$', name_part):
                return m.group(0)  # ends with digit (e.g. "At 2:", "Chapter 3:")
            # Skip very short labels (1 char) — likely not names
            if len(name_part) <= 1:
                return m.group(0)
            return m.group(1)  # keep the leading dash/space if present
        text = pattern.sub(_replace, text)
        text = re.sub(r'^\s*-?\s*$', '', text, flags=re.MULTILINE)
        text = re.sub(r'\n{2,}', '\n', text).strip()
        if text:
            result.append({**cue, 'text': text})
    return result


def filter_remove_leading_dashes(cues):
    """Remove leading dashes from each line of subtitle text.

    Handles dashes with or without trailing spaces, e.g.:
      '- Hello there'  →  'Hello there'
      '-Hello'         →  'Hello'
      '- Hello\n- World' → 'Hello\nWorld'
    Removes empty cues that result from the operation.
    """
    result = []
    for cue in cues:
        lines = cue['text'].split('\n')
        cleaned = [re.sub(r'^-\s*', '', line) for line in lines]
        text = '\n'.join(cleaned).strip()
        if text:
            result.append({**cue, 'text': text})
    return result


def srt_ts_to_ms(ts):
    """Convert SRT timestamp string 'HH:MM:SS,mmm' to milliseconds."""
    ts = ts.replace(',', '.').replace(';', '.')
    parts = ts.split(':')
    h, m = int(parts[0]), int(parts[1])
    s_parts = parts[2].split('.')
    s = int(s_parts[0])
    ms = int(s_parts[1]) if len(s_parts) > 1 else 0
    return (h * 3600 + m * 60 + s) * 1000 + ms


def ms_to_srt_ts(ms):
    """Convert milliseconds to SRT timestamp string 'HH:MM:SS,mmm'."""
    if ms < 0:
        ms = 0
    h = ms // 3600000
    ms %= 3600000
    m = ms // 60000
    ms %= 60000
    s = ms // 1000
    ms %= 1000
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def filter_remove_duplicates(cues):
    """Remove duplicate cues (same text and overlapping/identical timestamps)."""
    if not cues:
        return cues
    result = [cues[0]]
    for cue in cues[1:]:
        prev = result[-1]
        # Same text and same or overlapping timestamps
        if (cue['text'].strip() == prev['text'].strip() and
                cue['start'] == prev['start'] and cue['end'] == prev['end']):
            continue
        result.append(cue)
    return result


def filter_merge_short(cues, max_gap_ms=1000):
    """Merge consecutive cues with a small time gap that appear to be fragments."""
    if not cues:
        return cues
    result = [dict(cues[0])]
    for cue in cues[1:]:
        prev = result[-1]
        prev_end = srt_ts_to_ms(prev['end'])
        cur_start = srt_ts_to_ms(cue['start'])
        gap = cur_start - prev_end
        # Merge if gap is small and previous cue text is short (fragment)
        prev_text = prev['text'].strip()
        if 0 <= gap <= max_gap_ms and len(prev_text) < 40 and not prev_text.endswith(('.', '!', '?')):
            result[-1] = {
                **prev,
                'end': cue['end'],
                'text': prev['text'].rstrip() + ' ' + cue['text'].lstrip()
            }
        else:
            result.append(dict(cue))
    return result


def filter_reduce_lines(cues, max_lines=2, max_chars=42):
    """Reflow subtitle cues to max_lines, keeping sentences together where possible.

    For each cue with more than max_lines:
      1. If the cue has dialogue dashes (- Speaker), keeps each speaker on a line
      2. Tries to split at sentence boundaries (. ! ?) with balanced line lengths
      3. Falls back to splitting at the nearest word boundary to the midpoint
      4. Short text (≤ max_chars) stays on one line
    """
    if not cues:
        return cues

    def _reflow(text):
        lines = text.split('\n')
        if len(lines) <= max_lines:
            return text

        # ── Dialogue: lines starting with "- " get one per line ──
        # Join all lines, then re-split by dialogue dashes
        flat = ' '.join(l.strip() for l in lines if l.strip())
        if flat.startswith('- ') and '- ' in flat[2:]:
            # Split on " - " that starts a new speaker turn
            parts = re.split(r'(?<=\S) (?=- )', flat)
            if len(parts) == max_lines:
                return '\n'.join(p.strip() for p in parts)
            elif len(parts) > max_lines:
                # Too many speakers — combine overflow onto last line
                kept = parts[:max_lines - 1]
                kept.append(' '.join(parts[max_lines - 1:]))
                return '\n'.join(p.strip() for p in kept)

        # ── Short enough for one line ──
        if len(flat) <= max_chars:
            return flat

        # ── Try sentence boundary split ──
        # Find all positions after sentence-ending punctuation + space
        split_points = []
        for m in re.finditer(r'[.!?]+[\'"»\)]*\s+', flat):
            pos = m.end()
            if pos < len(flat):
                split_points.append(pos)

        # Pick the split point that best balances line lengths
        best_split = None
        best_diff = len(flat)
        for pos in split_points:
            line1 = flat[:pos].rstrip()
            line2 = flat[pos:].lstrip()
            # Both lines must be reasonable length
            if max(len(line1), len(line2)) <= max_chars + 10:
                diff = abs(len(line1) - len(line2))
                if diff < best_diff:
                    best_diff = diff
                    best_split = pos

        if best_split is not None:
            return flat[:best_split].rstrip() + '\n' + flat[best_split:].lstrip()

        # ── Fall back to word-boundary split near midpoint ──
        mid = len(flat) // 2
        # Search outward from midpoint for a space
        best_pos = None
        for offset in range(len(flat) // 2):
            for pos in (mid + offset, mid - offset):
                if 0 < pos < len(flat) and flat[pos] == ' ':
                    best_pos = pos
                    break
            if best_pos is not None:
                break

        if best_pos is not None:
            return flat[:best_pos] + '\n' + flat[best_pos + 1:]

        # Last resort — just return the flattened text
        return flat

    result = []
    for cue in cues:
        text = cue['text']
        if len(text.split('\n')) > max_lines:
            text = _reflow(text)
        result.append({**cue, 'text': text})
    return result


def shift_timestamps(cues, offset_ms):
    """Shift all cue timestamps by offset_ms (positive = later, negative = earlier)."""
    result = []
    for cue in cues:
        new_start = srt_ts_to_ms(cue['start']) + offset_ms
        new_end = srt_ts_to_ms(cue['end']) + offset_ms
        if new_end > 0:  # Drop cues shifted entirely before 0
            result.append({
                **cue,
                'start': ms_to_srt_ts(max(0, new_start)),
                'end': ms_to_srt_ts(new_end)
            })
    return result


def stretch_timestamps(cues, factor):
    """Scale all timestamps by a factor (e.g. 1.04 to speed up 4%)."""
    if factor <= 0:
        return cues
    result = []
    for cue in cues:
        new_start = int(srt_ts_to_ms(cue['start']) * factor)
        new_end = int(srt_ts_to_ms(cue['end']) * factor)
        result.append({
            **cue,
            'start': ms_to_srt_ts(new_start),
            'end': ms_to_srt_ts(new_end)
        })
    return result


def two_point_sync(cues, idx_a, correct_a_ms, idx_b, correct_b_ms):
    """Linearly resync all timestamps using two reference points.

    Given two cue indices and what their correct start times should be,
    computes a linear transform (offset + scale) and applies it to all cues.
    Fixes both fixed offset and gradual drift in one operation.

    Args:
        cues: list of cue dicts
        idx_a: index of first reference cue (typically near the start)
        correct_a_ms: what cue A's start time should be (in ms)
        idx_b: index of second reference cue (typically near the end)
        correct_b_ms: what cue B's start time should be (in ms)

    Returns:
        New list of cue dicts with adjusted timestamps.
    """
    if idx_a == idx_b or idx_a >= len(cues) or idx_b >= len(cues):
        return cues

    # Current timestamps of the two reference points
    current_a = srt_ts_to_ms(cues[idx_a]['start'])
    current_b = srt_ts_to_ms(cues[idx_b]['start'])

    if current_a == current_b:
        return cues  # can't compute slope from identical points

    # Linear transform: correct = slope * current + intercept
    slope = (correct_b_ms - correct_a_ms) / (current_b - current_a)
    intercept = correct_a_ms - slope * current_a

    result = []
    for cue in cues:
        old_start = srt_ts_to_ms(cue['start'])
        old_end = srt_ts_to_ms(cue['end'])
        new_start = int(slope * old_start + intercept)
        new_end = int(slope * old_end + intercept)
        if new_end > 0:
            result.append({
                **cue,
                'start': ms_to_srt_ts(max(0, new_start)),
                'end': ms_to_srt_ts(max(0, new_end))
            })
    return result


def retime_subtitles(cues, matches):
    """Re-time all subtitle cues using matched anchor points with interpolation.

    For each matched cue, uses the Whisper-detected timestamp directly.
    For unmatched cues, linearly interpolates from the nearest matched anchors.

    Args:
        cues: List of subtitle cue dicts.
        matches: List of (cue_idx, whisper_time_ms, cue_time_ms, similarity, text)
                 tuples from smart_sync.

    Returns:
        New list of cue dicts with adjusted timestamps.
    """
    if not matches or not cues:
        return cues

    # Build anchor points: sorted list of (old_time_ms, new_time_ms)
    anchors = []
    for ci, wt_ms, ct_ms, sim, _ in matches:
        anchors.append((ct_ms, wt_ms))
    anchors.sort(key=lambda x: x[0])

    # Remove duplicate old_times (keep highest similarity match)
    seen = {}
    for old_t, new_t in anchors:
        seen[old_t] = new_t  # later entries overwrite, but sorted so it's fine
    anchors = sorted(seen.items())

    if len(anchors) < 2:
        # Only one anchor — fall back to simple offset
        offset = anchors[0][1] - anchors[0][0]
        return shift_timestamps(cues, offset)

    def _interpolate(old_ms):
        """Map an old timestamp to a new timestamp using piecewise linear interpolation."""
        # Before first anchor — extrapolate from first two anchors
        if old_ms <= anchors[0][0]:
            old_a, new_a = anchors[0]
            old_b, new_b = anchors[1]
            if old_b == old_a:
                return new_a + (old_ms - old_a)
            slope = (new_b - new_a) / (old_b - old_a)
            return int(new_a + slope * (old_ms - old_a))

        # After last anchor — extrapolate from last two anchors
        if old_ms >= anchors[-1][0]:
            old_a, new_a = anchors[-2]
            old_b, new_b = anchors[-1]
            if old_b == old_a:
                return new_b + (old_ms - old_b)
            slope = (new_b - new_a) / (old_b - old_a)
            return int(new_b + slope * (old_ms - old_b))

        # Between two anchors — linear interpolation
        for i in range(len(anchors) - 1):
            old_a, new_a = anchors[i]
            old_b, new_b = anchors[i + 1]
            if old_a <= old_ms <= old_b:
                if old_b == old_a:
                    return new_a
                t = (old_ms - old_a) / (old_b - old_a)
                return int(new_a + t * (new_b - new_a))

        # Fallback (shouldn't reach here)
        return old_ms

    result = []
    for cue in cues:
        old_start = srt_ts_to_ms(cue['start'])
        old_end = srt_ts_to_ms(cue['end'])
        new_start = _interpolate(old_start)
        new_end = _interpolate(old_end)
        # Preserve minimum cue duration
        if new_end <= new_start:
            new_end = new_start + max(500, old_end - old_start)
        result.append({
            **cue,
            'start': ms_to_srt_ts(max(0, new_start)),
            'end': ms_to_srt_ts(max(0, new_end)),
        })
    return result


def smart_sync(video_path, cues, model_size='base', language=None,
               num_segments=3, sample_minutes=5,
               progress_callback=None, cancel_event=None,
               engine='faster-whisper'):
    """Auto-sync subtitles to video audio using Whisper speech recognition.

    Transcribes the audio, matches Whisper segments to subtitle cues by text
    similarity, and computes the optimal timestamp offset.

    Args:
        video_path: Path to the video file.
        cues: List of subtitle cue dicts.
        model_size: Whisper model size ('tiny', 'base', 'small', 'medium', 'large').
        language: Language code (e.g. 'en'). None = auto-detect.
        progress_callback: Optional callable(message) for status updates.
        cancel_event: Optional threading.Event for cancellation.
        engine: 'faster-whisper' (standard ~400ms accuracy) or
                'whisperx' (precise ~50ms accuracy via forced alignment).

    Returns:
        dict with keys:
            'offset_ms': int — median offset in milliseconds
            'matches': list of (cue_idx, whisper_time_ms, cue_time_ms, similarity, text) tuples
            'drift_ms': int — estimated drift (difference between first and last match offsets)
            'whisper_segments': list of Whisper segment dicts
        Returns None on failure.
    """
    import tempfile
    from difflib import SequenceMatcher

    if engine in ('whisperx', 'whisperx-align'):
        try:
            import whisperx
            import torch
        except ImportError as _imp_err:
            if progress_callback:
                _err_msg = str(_imp_err)
                if 'is_offline_mode' in _err_msg:
                    progress_callback(
                        "WhisperX/transformers version conflict: " + _err_msg)
                    progress_callback(
                        "Fix: pip install --user 'transformers<4.45'")
                else:
                    progress_callback("whisperx not installed")
            return None
    else:
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            if progress_callback:
                progress_callback("faster-whisper not installed")
            return None

    tmpdir = tempfile.mkdtemp(prefix='docflix_sync_')

    try:
        # ══════════════════════════════════════════════════════════════
        # Direct Align mode — skip Whisper, align subtitle text directly
        # against the audio waveform using wav2vec2 forced alignment.
        # Every cue gets its own precise timestamp.
        # ══════════════════════════════════════════════════════════════
        if engine == 'whisperx-align':
            duration = get_video_duration(video_path) or 7200

            # ── Extract full audio ──
            if progress_callback:
                progress_callback(f"Extracting audio ({duration/60:.0f} min)...")
            audio_path = os.path.join(tmpdir, 'audio_full.wav')
            extract_timeout = max(120, int(duration / 60) * 2 + 60)
            try:
                _ext = subprocess.run(
                    ['ffmpeg', '-y', '-i', video_path,
                     '-vn', '-acodec', 'pcm_s16le', '-ar', '16000', '-ac', '1',
                     audio_path],
                    capture_output=True, text=True, timeout=extract_timeout)
                if _ext.returncode != 0 or not os.path.exists(audio_path):
                    if progress_callback:
                        progress_callback("Audio extraction failed")
                    return None
            except subprocess.TimeoutExpired:
                if progress_callback:
                    progress_callback("Audio extraction timed out")
                return None

            if cancel_event and cancel_event.is_set():
                return None

            # ── Load alignment model only (no Whisper model needed) ──
            _wx_device = "cuda" if torch.cuda.is_available() else "cpu"
            _wx_lang = language or 'en'
            if progress_callback:
                _dev = 'GPU' if _wx_device == 'cuda' else 'CPU'
                progress_callback(f"Loading alignment model for '{_wx_lang}' "
                                  f"on {_dev}...")
            try:
                align_model, align_metadata = whisperx.load_align_model(
                    language_code=_wx_lang, device=_wx_device)
            except Exception as e:
                if progress_callback:
                    progress_callback(f"Failed to load alignment model: {e}")
                return None

            if cancel_event and cancel_event.is_set():
                return None

            # ── Build segments from subtitle cues ──
            if progress_callback:
                progress_callback(f"Aligning {len(cues)} cues against audio "
                                  f"(forced alignment)...")
            segments = []
            for cue in cues:
                text = cue['text'].replace('\n', ' ').strip()
                # Skip empty / music-only / HI-only cues
                clean = re.sub(r'[♪♫\[\]\(\)]', '', text).strip()
                if not clean or len(clean) < 2:
                    continue
                segments.append({
                    'start': srt_ts_to_ms(cue['start']) / 1000,
                    'end': srt_ts_to_ms(cue['end']) / 1000,
                    'text': text,
                })

            if not segments:
                if progress_callback:
                    progress_callback("No alignable subtitle cues found")
                return None

            # ── Forced alignment — subtitle text against audio ──
            try:
                audio_array = whisperx.load_audio(audio_path)
                aligned = whisperx.align(
                    segments, align_model, align_metadata,
                    audio_array, _wx_device,
                    return_char_alignments=True)
            except Exception as e:
                if progress_callback:
                    progress_callback(f"Alignment failed: {e}")
                return None

            if cancel_event and cancel_event.is_set():
                return None

            # ── Build per-cue matches from aligned results ──
            aligned_segs = aligned.get('segments', [])
            if progress_callback:
                progress_callback(f"Aligned {len(aligned_segs)}/{len(segments)} "
                                  f"segments. Building matches...")

            matches = []
            whisper_segments = []
            # Map aligned segments back to original cue indices
            seg_idx = 0  # index into the segments list we built
            for ci, cue in enumerate(cues):
                text = cue['text'].replace('\n', ' ').strip()
                clean = re.sub(r'[♪♫\[\]\(\)]', '', text).strip()
                if not clean or len(clean) < 2:
                    continue  # this cue was skipped during segment building

                if seg_idx >= len(aligned_segs):
                    break

                aseg = aligned_segs[seg_idx]
                seg_idx += 1

                # Get precise start — prefer char-level, fall back to word, then segment
                precise_start = None
                chars = aseg.get('chars', [])
                if chars:
                    for c in chars:
                        if 'start' in c:
                            precise_start = c['start']
                            break
                if precise_start is None:
                    words = aseg.get('words', [])
                    if words:
                        for w in words:
                            if 'start' in w:
                                precise_start = w['start']
                                break
                if precise_start is None:
                    precise_start = aseg.get('start')
                if precise_start is None:
                    continue  # alignment failed for this segment

                whisper_ms = int(precise_start * 1000)
                cue_ms = srt_ts_to_ms(cue['start'])

                matches.append((ci, whisper_ms, cue_ms, 1.0,
                               cue['text'][:40].replace('\n', ' ')))
                whisper_segments.append({
                    'start': precise_start,
                    'end': aseg.get('end', precise_start + 1),
                    'text': aseg.get('text', '').strip(),
                })

            if not matches:
                if progress_callback:
                    progress_callback("Direct alignment produced no matches")
                return None

            # ── VAD boundary snapping ──
            # Snap cue start times to actual speech onsets detected by Silero VAD.
            # WhisperX alignment gives phoneme positions (~50ms); VAD detects the
            # exact silence→speech transition (~20ms).
            try:
                import bisect
                import wave as _wave
                import numpy as _np
                from faster_whisper.vad import get_speech_timestamps, VadOptions

                if progress_callback:
                    progress_callback("Running VAD for boundary snapping...")

                # Load audio as float32 numpy array
                with _wave.open(audio_path, 'r') as _wf:
                    _frames = _wf.readframes(_wf.getnframes())
                    _audio_np = _np.frombuffer(
                        _frames, dtype=_np.int16).astype(_np.float32) / 32768.0

                # Run VAD with tight parameters — no padding, detect short gaps
                _vad_opts = VadOptions(
                    min_silence_duration_ms=150,
                    speech_pad_ms=0,
                    threshold=0.5,
                    min_speech_duration_ms=100,
                )
                _speech_ts = get_speech_timestamps(
                    _audio_np, vad_options=_vad_opts)

                if _speech_ts:
                    # Build sorted onset/offset lists in ms (16 samples = 1ms at 16kHz)
                    _onsets_ms = sorted(
                        int(ts['start'] / 16) for ts in _speech_ts)
                    _offsets_ms = sorted(
                        int(ts['end'] / 16) for ts in _speech_ts)

                    SNAP_WINDOW_MS = 150  # only snap if boundary within ±150ms
                    _snapped = 0

                    for i, (ci, wt_ms, ct_ms, sim, txt) in enumerate(matches):
                        # Find nearest VAD speech onset to this cue's start
                        idx = bisect.bisect_left(_onsets_ms, wt_ms)
                        best_onset = None
                        best_dist = SNAP_WINDOW_MS + 1
                        for j in (idx - 1, idx):
                            if 0 <= j < len(_onsets_ms):
                                dist = abs(_onsets_ms[j] - wt_ms)
                                if dist < best_dist:
                                    best_dist = dist
                                    best_onset = _onsets_ms[j]
                        if best_onset is not None and best_dist <= SNAP_WINDOW_MS:
                            matches[i] = (ci, best_onset, ct_ms, sim, txt)
                            _snapped += 1

                    if progress_callback:
                        progress_callback(
                            f"VAD snap: {_snapped}/{len(matches)} cue starts "
                            f"snapped to speech onsets "
                            f"({len(_onsets_ms)} speech segments detected)")
                else:
                    if progress_callback:
                        progress_callback("VAD detected no speech — skipping snap")

            except ImportError:
                if progress_callback:
                    progress_callback("VAD snap skipped — faster-whisper not installed")
            except Exception as _vad_err:
                if progress_callback:
                    progress_callback(f"VAD snap skipped: {_vad_err}")

            # ── Calculate offset ──
            offsets = [wt - ct for _, wt, ct, _, _ in matches]
            offsets.sort()
            median_offset = offsets[len(offsets) // 2]

            mid_time = srt_ts_to_ms(cues[len(cues)//2]['start'])
            early = [wt - ct for _, wt, ct, _, _ in matches if ct < mid_time]
            late = [wt - ct for _, wt, ct, _, _ in matches if ct >= mid_time]
            drift = ((sum(late)/len(late)) - (sum(early)/len(early))) \
                    if early and late else 0

            if progress_callback:
                progress_callback(f"Direct Align: {len(matches)}/{len(cues)} cues "
                                  f"aligned. Offset: {median_offset:+d}ms, "
                                  f"Drift: {drift:+.0f}ms")

            return {
                'offset_ms': median_offset,
                'matches': matches,
                'drift_ms': int(drift),
                'whisper_segments': whisper_segments,
            }

        # ══════════════════════════════════════════════════════════════
        # Standard / Precise mode — Whisper transcription + matching
        # ══════════════════════════════════════════════════════════════

        # ── Pre-check: Compare video and subtitle durations ──
        duration = get_video_duration(video_path) or 7200
        if cues:
            last_cue_ms = srt_ts_to_ms(cues[-1]['end'])
            sub_duration = last_cue_ms / 1000
            diff_pct = abs(duration - sub_duration) / max(duration, 1) * 100
            if diff_pct > 15:
                if progress_callback:
                    progress_callback(f"⚠ Duration mismatch: video is {duration/60:.0f} min, "
                                      f"subtitles span {sub_duration/60:.0f} min "
                                      f"({diff_pct:.0f}% difference). "
                                      f"These may be different cuts.")

        # ── Phase 1: Get video duration and plan sample segments ──
        full_scan = (num_segments <= 0 or sample_minutes <= 0)

        if full_scan:
            # Full Scan — extract entire audio as one segment
            samples = [(0, duration)]
            if progress_callback:
                progress_callback(f"Full Scan — extracting {duration/60:.0f} min of audio...")
        else:
            SAMPLE_LEN = sample_minutes * 60
            n_segs = max(1, num_segments)
            if duration <= SAMPLE_LEN * 2 or n_segs == 1:
                samples = [(0, min(duration, SAMPLE_LEN) if n_segs == 1 else duration)]
            else:
                samples = []
                for i in range(n_segs):
                    center = duration * (i / (n_segs - 1)) if n_segs > 1 else 0
                    seg_start = max(0, center - SAMPLE_LEN / 2)
                    seg_end = min(duration, seg_start + SAMPLE_LEN)
                    seg_start = max(0, seg_end - SAMPLE_LEN)
                    samples.append((seg_start, seg_end))

            if progress_callback:
                total_sample = sum(e - s for s, e in samples)
                progress_callback(f"Quick Scan — {len(samples)} segments "
                                  f"({total_sample/60:.0f} min of {duration/60:.0f} min total)...")

        # ── Phase 2: Extract audio samples ──
        audio_paths = []
        for si, (ss, se) in enumerate(samples):
            if cancel_event and cancel_event.is_set():
                return None
            audio_path = os.path.join(tmpdir, f'audio_{si}.wav')
            extract_cmd = [
                'ffmpeg', '-y',
                '-ss', f'{ss:.1f}', '-t', f'{se - ss:.1f}',
                '-i', video_path,
                '-vn', '-acodec', 'pcm_s16le', '-ar', '16000', '-ac', '1',
                audio_path
            ]
            try:
                seg_duration = se - ss
                # Timeout: at least 120s, scale with segment length (~1s per minute of audio)
                extract_timeout = max(120, int(seg_duration / 60) * 2 + 60)
                if progress_callback:
                    progress_callback(f"Extracting audio segment {si+1}/{len(samples)} "
                                      f"({ss/60:.0f}m–{se/60:.0f}m)...")
                result = subprocess.run(extract_cmd, capture_output=True,
                                        text=True, timeout=extract_timeout)
                if result.returncode == 0 and os.path.exists(audio_path):
                    audio_paths.append((ss, audio_path))
            except subprocess.TimeoutExpired:
                continue

        if not audio_paths:
            if progress_callback:
                progress_callback("Audio extraction failed for all segments")
            return None

        # ── Phase 3: Load Whisper model ──
        if engine == 'whisperx':
            if progress_callback:
                _dev_label = 'GPU (CUDA)' if torch.cuda.is_available() else 'CPU'
                progress_callback(f"Loading WhisperX model ({model_size}) on {_dev_label}...")
            try:
                _wx_device = "cuda" if torch.cuda.is_available() else "cpu"
                _wx_compute = "float16" if _wx_device == "cuda" else "int8"
                model = whisperx.load_model(model_size, _wx_device,
                                            compute_type=_wx_compute)
            except Exception as e:
                if progress_callback:
                    progress_callback(f"Failed to load WhisperX model: {e}")
                    if 'is_offline_mode' in str(e) or 'transformers' in str(e):
                        progress_callback(
                            "Fix: pip install --user 'transformers<4.45'")
                return None
        else:
            if progress_callback:
                progress_callback(f"Loading Whisper model ({model_size})...")
            try:
                model = WhisperModel(model_size, device="cpu", compute_type="int8")
            except Exception as e:
                if progress_callback:
                    progress_callback(f"Failed to load Whisper model: {e}")
                return None

        if cancel_event and cancel_event.is_set():
            return None

        # ── Phase 4: Transcribe each sample ──
        whisper_segments = []

        if engine == 'whisperx':
            # ── WhisperX: batch transcription + forced alignment per segment ──
            _wx_lang = language or 'en'
            _align_model = None
            _align_metadata = None

            for si, (offset_s, apath) in enumerate(audio_paths):
                if cancel_event and cancel_event.is_set():
                    return None
                if progress_callback:
                    progress_callback(f"Transcribing segment {si+1}/{len(audio_paths)} "
                                      f"(WhisperX)...")
                try:
                    audio_array = whisperx.load_audio(apath)
                    tx_result = model.transcribe(audio_array, batch_size=16,
                                                 language=_wx_lang)
                    # Auto-detect language from first segment if not specified
                    detected_lang = tx_result.get('language', _wx_lang)
                    if not language and detected_lang:
                        _wx_lang = detected_lang

                    if cancel_event and cancel_event.is_set():
                        return None

                    # Load alignment model once (per language)
                    if _align_model is None:
                        if progress_callback:
                            progress_callback(f"Loading alignment model for "
                                              f"'{_wx_lang}'...")
                        try:
                            _align_model, _align_metadata = \
                                whisperx.load_align_model(
                                    language_code=_wx_lang,
                                    device=_wx_device)
                        except Exception as ae:
                            if progress_callback:
                                progress_callback(f"Alignment model failed: {ae}")
                                progress_callback("Falling back to segment-level "
                                                  "timestamps (less precise)")
                            # Fall back: use unaligned segment timestamps
                            for seg in tx_result.get('segments', []):
                                whisper_segments.append({
                                    'start': seg['start'] + offset_s,
                                    'end': seg['end'] + offset_s,
                                    'text': seg.get('text', '').strip(),
                                })
                            continue

                    if cancel_event and cancel_event.is_set():
                        return None

                    # ── Forced alignment — phoneme-level precision ──
                    if progress_callback:
                        progress_callback(f"Aligning segment {si+1}/{len(audio_paths)} "
                                          f"(forced alignment)...")
                    aligned = whisperx.align(
                        tx_result['segments'], _align_model, _align_metadata,
                        audio_array, _wx_device,
                        return_char_alignments=True)

                    # Collect aligned segments — prefer char-level timestamps
                    count = 0
                    for seg in aligned.get('segments', []):
                        precise_start = None
                        # Char-level (most precise)
                        chars = seg.get('chars', [])
                        if chars:
                            for c in chars:
                                if 'start' in c:
                                    precise_start = c['start'] + offset_s
                                    break
                        # Word-level fallback
                        if precise_start is None:
                            words = seg.get('words', [])
                            if words:
                                for w in words:
                                    if 'start' in w:
                                        precise_start = w['start'] + offset_s
                                        break
                        # Segment-level fallback
                        if precise_start is None:
                            precise_start = seg['start'] + offset_s

                        whisper_segments.append({
                            'start': precise_start,
                            'end': seg['end'] + offset_s,
                            'text': seg.get('text', '').strip(),
                        })
                        count += 1
                        if progress_callback and count % 10 == 0:
                            progress_callback(f"Segment {si+1}: {count} phrases "
                                              f"(aligned)...")

                except Exception as e:
                    if progress_callback:
                        progress_callback(f"WhisperX error in segment {si+1}: {e}")
                    continue
        else:
            # ── faster-whisper: streaming transcription per segment ──
            for si, (offset_s, apath) in enumerate(audio_paths):
                if cancel_event and cancel_event.is_set():
                    return None
                if progress_callback:
                    progress_callback(f"Transcribing segment {si+1}/{len(audio_paths)}...")
                try:
                    segments_gen, info = model.transcribe(
                        apath,
                        language=language,
                        word_timestamps=True,
                        vad_filter=True,
                    )
                    count = 0
                    for seg in segments_gen:
                        if cancel_event and cancel_event.is_set():
                            return None
                        # Use word-level timestamp for more precise start time
                        # The first word's start is more accurate than the segment start
                        words = seg.words if hasattr(seg, 'words') and seg.words else None
                        if words:
                            precise_start = words[0].start + offset_s
                        else:
                            precise_start = seg.start + offset_s
                        whisper_segments.append({
                            'start': precise_start,
                            'end': seg.end + offset_s,
                            'text': seg.text.strip(),
                        })
                        count += 1
                        if progress_callback and count % 10 == 0:
                            progress_callback(f"Segment {si+1}: {count} phrases "
                                              f"({seg.end:.0f}s)...")
                except Exception as e:
                    if progress_callback:
                        progress_callback(f"Transcription error in segment {si+1}: {e}")
                    continue

        if not whisper_segments:
            if progress_callback:
                progress_callback("Whisper produced no transcription")
            return None

        if progress_callback:
            progress_callback(f"Transcribed {len(whisper_segments)} phrases from "
                              f"{len(audio_paths)} segments. Matching to subtitles...")

        # ── Phase 3: Two-pass matching ──
        def _normalize(text):
            """Normalize text for comparison: lowercase, strip punctuation,
            remove speaker labels, HI annotations, and music notes."""
            text = text.lower()
            # Remove speaker labels: "JUNIOR: text" → "text"
            text = re.sub(r'^[a-z][a-z\s\'\.]{0,25}:\s*', '', text, flags=re.MULTILINE)
            # Remove HI annotations: [brackets] and (parentheses)
            text = re.sub(r'\[.*?\]', '', text)
            text = re.sub(r'\(.*?\)', '', text)
            # Remove music notes
            text = text.replace('♪', '').replace('♫', '')
            # Strip punctuation and normalize whitespace
            text = re.sub(r'[^\w\s]', '', text)
            text = re.sub(r'\s+', ' ', text).strip()
            return text

        # Pre-normalize all Whisper segments for speed
        norm_segments = []
        for seg in whisper_segments:
            nt = _normalize(seg['text'])
            if nt and len(nt) >= 3:
                norm_segments.append((seg, nt))

        if progress_callback:
            progress_callback(f"{len(norm_segments)} usable phrases "
                              f"(of {len(whisper_segments)} total)")

        total_cues = len(cues)

        def _match_sequential():
            """Sequential matching: walk through cues and Whisper segments in order.
            Each match must come AFTER the previous match in the Whisper timeline.
            This prevents cross-matching and handles different cuts/edits."""
            matches = []
            seg_start = 0  # start searching from this Whisper segment index
            # Scale search window with segment density — Full Scan on long files
            # can produce 1000+ segments; a fixed window of 100 (~5 min) loses
            # sync after any extended gap (music, credits, silence).
            # Use at least 100, scale up to cover ~1/3 of all segments.
            SEARCH_WINDOW = max(100, len(norm_segments) // 3)
            _consec_misses = 0  # track consecutive unmatched cues

            for ci, cue in enumerate(cues):
                if cancel_event and cancel_event.is_set():
                    return None

                if progress_callback and (ci % 10 == 0 or ci == total_cues - 1):
                    progress_callback(f"Matching cue {ci+1}/{total_cues} "
                                      f"({len(matches)} matched)...")

                cue_text = _normalize(cue['text'])
                if len(cue_text) < 3:
                    continue

                cue_start_ms = srt_ts_to_ms(cue['start'])
                best_sim = 0
                best_idx = None

                # Only search forward from last match position
                search_end = min(seg_start + SEARCH_WINDOW, len(norm_segments))
                for si in range(seg_start, search_end):
                    seg, seg_text = norm_segments[si]

                    # Length ratio filter — relaxed to handle different
                    # sentence splitting between subtitles and Whisper
                    len_ratio = len(cue_text) / max(len(seg_text), 1)
                    if len_ratio < 0.2 or len_ratio > 5.0:
                        continue

                    sim = SequenceMatcher(None, cue_text, seg_text).ratio()
                    if sim > best_sim:
                        best_sim = sim
                        best_idx = si

                if best_sim > 0.6 and best_idx is not None:
                    seg = norm_segments[best_idx][0]
                    whisper_ms = int(seg['start'] * 1000)
                    this_offset = whisper_ms - cue_start_ms

                    # Consistency check: reject if offset changes too dramatically
                    # from recent matches (catches bad jumps from wrong matches)
                    accept = True
                    if matches:
                        recent_offsets = [wt - ct for _, wt, ct, _, _ in matches[-5:]]
                        avg_recent = sum(recent_offsets) / len(recent_offsets)
                        # Allow up to 30 seconds of drift from recent average
                        if abs(this_offset - avg_recent) > 30000:
                            accept = False  # skip this suspicious match

                    if accept:
                        matches.append((ci, whisper_ms, cue_start_ms, best_sim,
                                        cue['text'][:40].replace('\n', ' ')))
                        # Advance search start past this match
                        seg_start = best_idx + 1
                        _consec_misses = 0
                    else:
                        _consec_misses += 1
                else:
                    _consec_misses += 1

                # If we've gone 50+ cues without a match, try resetting the
                # search position based on time — we may have lost sync
                if _consec_misses >= 50 and norm_segments:
                    # Estimate where in the segments we should be, based on
                    # the cue's timestamp relative to total duration
                    cue_frac = cue_start_ms / max(
                        srt_ts_to_ms(cues[-1]['end']), 1)
                    est_idx = int(cue_frac * len(norm_segments))
                    # Only jump forward, never backward past confirmed matches
                    if est_idx > seg_start:
                        seg_start = max(seg_start, est_idx - SEARCH_WINDOW // 4)
                        _consec_misses = 0
                        if progress_callback:
                            progress_callback(
                                f"  Re-syncing search at segment {seg_start} "
                                f"(cue {ci+1} had {_consec_misses} misses)...")

            return matches

        # ── Sequential matching ──
        if progress_callback:
            progress_callback("Matching subtitles to audio (sequential)...")

        matches = _match_sequential()
        if matches is None:
            return None  # cancelled

        if not matches:
            if progress_callback:
                progress_callback("No text matches found between subtitles and audio")
            return None

        # ── Phase 4: Calculate offset ──
        try:
            offsets = [wt - ct for (_, wt, ct, _, _) in matches]
            offsets.sort()
            median_offset = offsets[len(offsets) // 2]

            mid_time = srt_ts_to_ms(cues[len(cues)//2]['start'])
            early = [wt - ct for _, wt, ct, _, _ in matches if ct < mid_time]
            late = [wt - ct for _, wt, ct, _, _ in matches if ct >= mid_time]
            if early and late:
                drift = (sum(late) / len(late)) - (sum(early) / len(early))
            else:
                drift = 0

            if progress_callback:
                progress_callback(f"Matched {len(matches)}/{len(cues)} cues. "
                                  f"Offset: {median_offset:+d}ms, Drift: {drift:+.0f}ms")

            return {
                'offset_ms': median_offset,
                'matches': matches,
                'drift_ms': int(drift),
                'whisper_segments': whisper_segments,
            }
        except Exception as e:
            if progress_callback:
                progress_callback(f"Error calculating offset: {e}")
            return None

    finally:
        import shutil as _shutil_cleanup
        _shutil_cleanup.rmtree(tmpdir, ignore_errors=True)


# Max recommended characters per subtitle line for readability
MAX_CHARS_PER_LINE = 42


def get_video_duration(filepath):
    """Get video duration in seconds using ffprobe"""
    try:
        cmd = [
            'ffprobe', '-v', 'error',
            '-show_entries', 'format=duration',
            '-of', 'default=noprint_wrappers=1:nokey=1',
            filepath
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            return float(result.stdout.strip())
    except Exception:
        pass
    return None

def estimate_output_size(filepath, settings):
    """
    Estimate output file size based on codec settings and source duration.
    Returns a formatted string like '245.3 MB' or '~245 MB', or '?' if unknown.
    """
    try:
        duration = get_video_duration(filepath)
        if not duration or duration <= 0:
            return '?'

        codec_info = settings.get('codec_info', VIDEO_CODEC_MAP['H.265 / HEVC'])
        transcode_mode = settings.get('transcode_mode', 'video')
        quality_mode = settings.get('mode', 'bitrate')
        encoder = settings.get('encoder', 'cpu')

        # Video bitrate estimate (bits/sec)
        video_bps = 0
        if transcode_mode in ('video', 'both'):
            if codec_info.get('cpu_encoder') == 'copy':
                # Copy — use source video bitrate as estimate
                src_size = Path(filepath).stat().st_size
                video_bps = (src_size * 8) / duration
            elif quality_mode == 'bitrate':
                bitrate_str = settings.get('bitrate', '2M')
                multiplier = 1_000_000 if 'M' in bitrate_str else 1_000
                video_bps = float(bitrate_str.replace('M','').replace('K','').replace('k','')) * multiplier
            else:
                # CRF — heuristic: estimate bps from CRF value and codec
                crf = int(settings.get('crf', 23))
                short = codec_info.get('short_name', 'H265')
                # Rough CRF→bitrate mapping (very approximate, 1080p baseline)
                if short == 'H264':
                    video_bps = 12_000_000 * (0.85 ** (crf - 18))
                elif short == 'H265':
                    video_bps = 6_000_000 * (0.85 ** (crf - 23))
                elif short == 'AV1':
                    video_bps = 4_000_000 * (0.85 ** (crf - 35))
                elif short == 'MPEG4':
                    video_bps = 10_000_000 * (0.80 ** (crf - 4))
                elif short == 'ProRes':
                    # ProRes is high-bitrate intra-frame; q:v 10 ≈ 100 Mbps at 1080p
                    video_bps = 100_000_000 * (0.90 ** (crf - 10))
                else:  # VP9
                    video_bps = 5_000_000 * (0.85 ** (crf - 33))

        # Audio bitrate estimate (bits/sec)
        audio_bps = 0
        if transcode_mode in ('audio', 'both'):
            audio_codec = settings.get('audio_codec', 'aac')
            if audio_codec == 'copy':
                # Assume ~256kbps for copy
                audio_bps = 256_000
            elif audio_codec in ('flac', 'alac', 'pcm_s16le', 'pcm_s24le', 'wavpack', 'tta'):
                # Lossless — rough estimate ~1Mbps stereo
                audio_bps = 1_000_000
            else:
                abr = settings.get('audio_bitrate', '128k')
                audio_bps = float(abr.replace('k','').replace('K','')) * 1000

        total_bps = video_bps + audio_bps
        if total_bps <= 0:
            return '?'

        estimated_bytes = (total_bps * duration) / 8
        return '~' + format_size(int(estimated_bytes))
    except Exception:
        return '?'


def verify_output_file(output_path, input_path=None):
    """
    Verify an output file is valid and playable using ffprobe.
    Returns (ok: bool, issues: list[str])
    """
    issues = []

    # 1. File must exist and have size > 0
    try:
        size = Path(output_path).stat().st_size
        if size == 0:
            return False, ["Output file is empty (0 bytes)"]
    except FileNotFoundError:
        return False, ["Output file not found"]

    # 2. ffprobe container check
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'error',
             '-show_entries', 'format=duration,size',
             '-show_entries', 'stream=codec_type,codec_name',
             '-of', 'default=noprint_wrappers=1',
             output_path],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            issues.append(f"ffprobe error: {result.stderr.strip()[:200]}")
            return False, issues

        output = result.stdout + result.stderr

        # 3. Check for error messages in ffprobe output
        for line in result.stderr.splitlines():
            if line.strip():
                issues.append(f"Stream warning: {line.strip()[:120]}")

        # 4. Check at least one video or audio stream exists
        has_video = 'codec_type=video' in output
        has_audio = 'codec_type=audio' in output
        if not has_video and not has_audio:
            issues.append("No video or audio streams found in output file")
            return False, issues

        # 5. Check duration matches source (within 5%)
        if input_path:
            src_dur = get_video_duration(input_path)
            out_dur = get_video_duration(output_path)
            if src_dur and out_dur:
                diff = abs(src_dur - out_dur)
                tolerance = src_dur * 0.05  # 5%
                if diff > tolerance and diff > 2.0:  # also ignore < 2s diff
                    issues.append(
                        f"Duration mismatch: source={format_time(src_dur)}, "
                        f"output={format_time(out_dur)} (diff={diff:.1f}s)"
                    )

    except subprocess.TimeoutExpired:
        issues.append("ffprobe timed out during verification")
        return False, issues
    except Exception as e:
        issues.append(f"Verification error: {e}")
        return False, issues

    ok = len([i for i in issues if 'warning' not in i.lower()]) == 0
    return ok, issues


def check_ffmpeg():
    """Check if ffmpeg is installed and get version"""
    try:
        result = subprocess.run(['ffmpeg', '-version'], capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            version_line = result.stdout.split('\n')[0]
            return True, version_line
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return False, "ffmpeg not found"

def _verify_gpu_encoder(backend_id, backend):
    """Run a quick test encode to verify a GPU backend actually works.

    Returns True if the encoder produced output successfully, False otherwise.
    This catches cases where the encoder is compiled into ffmpeg but the
    hardware driver/runtime is missing or misconfigured (e.g. Intel QSV
    without libmfx/oneVPL, NVIDIA without drivers, VAAPI without va-driver).

    For QSV on Linux, multiple initialization methods are tried because
    some systems only support QSV through the VAAPI backend (libvpl)
    rather than direct MFX session creation.

    Returns a truthy string indicating the method that worked, or False.
    For QSV: 'direct', 'vaapi_backend', or 'init_device'.
    For others: 'direct' or False.
    """
    test_encoder = None
    for enc in backend['detect_encoders']:
        test_encoder = enc
        break
    if not test_encoder:
        return False

    def _run_test(cmd):
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            return result.returncode == 0
        except Exception:
            return False

    if backend_id == 'vaapi':
        # VAAPI needs device init + hwupload to get frames onto the GPU
        if _run_test([
            'ffmpeg', '-y', '-loglevel', 'error',
            '-vaapi_device', '/dev/dri/renderD128',
            '-f', 'lavfi', '-i', 'color=black:s=256x256:d=0.1:r=1',
            '-vf', 'format=nv12,hwupload',
            '-c:v', test_encoder,
            '-frames:v', '1',
            '-f', 'null', '-'
        ]):
            return 'direct'
        return False

    elif backend_id == 'qsv':
        # QSV on Linux can be initialized in several ways depending on the
        # driver stack. Try each method — if any succeeds, QSV is usable.

        # Method 1: Direct QSV (works with legacy libmfx)
        if _run_test([
            'ffmpeg', '-y', '-loglevel', 'error',
            '-f', 'lavfi', '-i', 'color=black:s=256x256:d=0.1:r=1',
            '-c:v', test_encoder,
            '-frames:v', '1',
            '-f', 'null', '-'
        ]):
            return 'direct'

        # Method 2: QSV via VAAPI backend (modern libvpl / oneVPL on Linux)
        # This is how HandBrake initializes QSV on many Linux systems
        if _run_test([
            'ffmpeg', '-y', '-loglevel', 'error',
            '-init_hw_device', 'vaapi=va:/dev/dri/renderD128',
            '-init_hw_device', 'qsv=qsv@va',
            '-f', 'lavfi', '-i', 'color=black:s=256x256:d=0.1:r=1',
            '-c:v', test_encoder,
            '-frames:v', '1',
            '-f', 'null', '-'
        ]):
            return 'vaapi_backend'

        # Method 3: QSV with explicit device init
        if _run_test([
            'ffmpeg', '-y', '-loglevel', 'error',
            '-init_hw_device', 'qsv=qsv',
            '-f', 'lavfi', '-i', 'color=black:s=256x256:d=0.1:r=1',
            '-c:v', test_encoder,
            '-frames:v', '1',
            '-f', 'null', '-'
        ]):
            return 'init_device'

        return False

    else:
        # NVENC and others: straightforward test
        if _run_test([
            'ffmpeg', '-y', '-loglevel', 'error',
            '-f', 'lavfi', '-i', 'color=black:s=256x256:d=0.1:r=1',
            '-c:v', test_encoder,
            '-frames:v', '1',
            '-f', 'null', '-'
        ]):
            return 'direct'
        return False


def detect_gpu_backends():
    """Detect all available GPU encoding backends.

    Returns a dict: { backend_id: gpu_name_or_True, ... }
    Backends are included only if:
      1. Their key encoder is found in ``ffmpeg -encoders``
      2. A quick test encode succeeds (verifies driver/runtime is working)

    For QSV, if the VAAPI-backed init method works (but direct MFX doesn't),
    the backend's hwaccel flags are updated to use the VAAPI init path.
    """
    available = {}
    try:
        result = subprocess.run(['ffmpeg', '-encoders'], capture_output=True, text=True, timeout=10)
        encoder_output = result.stdout + result.stderr
    except Exception:
        return available

    for bid, backend in GPU_BACKENDS.items():
        # Check if the key encoder(s) are present in ffmpeg output
        if any(enc in encoder_output for enc in backend['detect_encoders']):
            if GPU_TEST_MODE:
                # Skip test encode — accept encoder as available based on ffmpeg listing alone
                gpu_name = _detect_gpu_name(bid, backend)
                available[bid] = gpu_name or True
            else:
                # Verify the encoder actually works with a quick test
                method = _verify_gpu_encoder(bid, backend)
                if method:
                    # If QSV works via VAAPI backend, update hwaccel flags
                    if bid == 'qsv' and method == 'vaapi_backend':
                        backend['hwaccel'] = [
                            '-init_hw_device', 'vaapi=va:/dev/dri/renderD128',
                            '-init_hw_device', 'qsv=qsv@va',
                            '-hwaccel', 'qsv',
                            '-hwaccel_output_format', 'qsv',
                        ]
                    gpu_name = _detect_gpu_name(bid, backend)
                    available[bid] = gpu_name or True
    return available


def _detect_gpu_name(backend_id, backend):
    """Try to get the GPU name for a backend."""
    # If the backend defines a detection command, try it first
    if backend.get('detect_cmd'):
        try:
            result = subprocess.run(backend['detect_cmd'],
                                    capture_output=True, text=True, timeout=10)
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip().split('\n')[0]
        except Exception:
            pass

    # Fallback: parse lspci for known GPU vendors
    try:
        result = subprocess.run(['lspci'], capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            vendor_patterns = {
                'nvenc': 'NVIDIA',
                'qsv':   'Intel.*(?:Graphics|Iris|UHD|Arc)',
                'vaapi':  r'AMD|\bATI\b|Radeon',
            }
            pattern = vendor_patterns.get(backend_id)
            if pattern:
                for line in result.stdout.splitlines():
                    if re.search(r'VGA|3D|Display', line, re.IGNORECASE):
                        if re.search(pattern, line, re.IGNORECASE):
                            # Extract the device description after the colon
                            parts = line.split(': ', 1)
                            if len(parts) == 2:
                                return parts[1].strip()
    except Exception:
        pass
    return None


def _short_gpu_name(raw_name, backend_id):
    """Extract a concise GPU model name from detection output.

    nvidia-smi returns e.g. 'NVIDIA GeForce RTX 3080' or 'Tesla T4'.
    lspci returns e.g. 'NVIDIA Corporation GP106 [GeForce GTX 1060 6GB]'
                   or  'Intel Corporation UHD Graphics 630 (Desktop)'
                   or  'Advanced Micro Devices, Inc. [AMD/ATI] Navi 14 [Radeon RX 5500]'
    """
    name = raw_name.strip()

    # Strip trailing parenthetical like '(rev 01)' FIRST so bracket extraction works
    name = re.sub(r'\s*\((?:Desktop|Mobile|Server|rev\s+\w+)\)\s*$', '', name, flags=re.IGNORECASE).strip()

    # If lspci format with brackets, prefer the LAST bracketed model name
    # (skips vendor brackets like [AMD/ATI] and grabs [GeForce RTX 4090])
    bracket = re.search(r'\[([^\]]+)\]\s*$', name)
    if bracket:
        name = bracket.group(1)

    # Strip common vendor prefixes
    name = re.sub(r'^(?:NVIDIA\s+(?:Corporation\s+)?|'
                  r'Intel\s+(?:Corporation\s+)?|'
                  r'Advanced Micro Devices,?\s*Inc\.?\s*|'
                  r'\[?AMD/?ATI\]?\s*)', '', name, flags=re.IGNORECASE).strip()

    # Strip trailing chip IDs like 'GP106' if a model name follows
    name = re.sub(r'^[A-Z]{2}\d{3,4}\s+', '', name).strip()

    return name or raw_name.strip()


# ============================================================================
# Video Converter Class
# ============================================================================

class VideoConverter:
    """Handles video conversion using ffmpeg"""
    
    def __init__(self, log_callback=None, progress_callback=None):
        self.log_callback = log_callback
        self.progress_callback = progress_callback
        self.current_process = None
        self.is_paused = False
        self.is_stopped = False
    
    def log(self, message, level='INFO'):
        """Send log message to callback"""
        if self.log_callback:
            timestamp = datetime.now().strftime('%H:%M:%S')
            self.log_callback(f"[{timestamp}] [{level}] {message}")
    
    def convert_file(self, input_path, output_path, settings):
        """
        Convert a single video file

        settings dict:
            - transcode_mode: 'video', 'audio', or 'both'
            - encoder: 'cpu' or GPU backend id (e.g. 'nvenc', 'qsv', 'vaapi')
            - mode: 'bitrate' or 'crf'
            - bitrate: e.g., '2M'
            - crf: int 0-51
            - preset: CPU preset or GPU preset
            - audio_codec: 'aac', 'mp3', 'opus', etc.
            - audio_bitrate: e.g., '128k'
        """
        self.is_paused = False
        self.is_stopped = False

        try:
            encoder       = settings.get('encoder', 'cpu')
            hw_decode     = settings.get('hw_decode', False)
            transcode_mode = settings.get('transcode_mode', 'both')
            codec_info    = settings.get('codec_info', VIDEO_CODEC_MAP['H.265 / HEVC'])
            codec_name    = settings.get('codec_name', 'H.265 / HEVC')
            mode          = settings.get('mode', 'bitrate')
            two_pass      = settings.get('two_pass', False)
            is_gpu        = encoder != 'cpu'
            backend       = GPU_BACKENDS.get(encoder) if is_gpu else None

            # Resolve the actual ffmpeg encoder name
            if is_gpu:
                video_enc_name = get_gpu_encoder(codec_name, encoder) or codec_info['cpu_encoder']
            else:
                video_enc_name = codec_info['cpu_encoder']

            # Two-pass only makes sense for CPU bitrate mode on supported codecs
            cpu_encoder = codec_info.get('cpu_encoder', '')
            TWO_PASS_SUPPORTED = {'libx265', 'libx264', 'libvpx-vp9', 'mpeg4'}
            use_two_pass = (
                two_pass and
                encoder == 'cpu' and
                mode == 'bitrate' and
                cpu_encoder in TWO_PASS_SUPPORTED and
                transcode_mode in ('video', 'both')
            )

            # GPU multipass (separate concept from CPU two-pass)
            use_gpu_multipass = (
                two_pass and
                is_gpu and
                backend is not None and
                mode == 'bitrate' and
                video_enc_name in backend.get('multipass_encoders', set())
            )

            # ── External subtitle handling ──
            ext_subs = settings.get('external_subs', [])
            embed_subs = [s for s in ext_subs if s['mode'] == 'embed']
            # Forced subtitles first so they appear as the first subtitle track(s)
            embed_subs.sort(key=lambda s: (not s.get('forced', False)))
            burn_in_subs = [s for s in ext_subs if s['mode'] == 'burn_in']
            has_burn_in = bool(burn_in_subs)

            # Burn-in is incompatible with hardware decode
            effective_hw = hw_decode and not has_burn_in

            if has_burn_in and hw_decode:
                self.log("Hardware decode disabled: burn-in subtitles require CPU filter pipeline", 'WARNING')
            if len(burn_in_subs) > 1:
                self.log("Warning: only the first burn-in subtitle will be rendered", 'WARNING')

            # ── Pixel format compatibility check ──
            # When using GPU hwaccel, frames stay on the device in the source
            # pixel format.  If the source is 10-bit (e.g. yuv420p10le) but the
            # target encoder only supports 8-bit (e.g. h264_nvenc), we need a
            # scale filter to convert the pixel format.
            # NOTE: We no longer use -hwaccel_output_format cuda because it
            # fails on sources with mid-stream resolution changes (the scale_cuda
            # filter doesn't support filter graph reinit).  Without it, frames
            # pass through system memory where format conversion is automatic.
            needs_pix_fmt_convert = False
            if effective_hw and is_gpu and backend and video_enc_name not in (None, 'copy'):
                src_pix_fmt = get_video_pix_fmt(input_path) or ''
                is_10bit_src = '10' in src_pix_fmt  # yuv420p10le, p010le, etc.
                _8bit_only_encoders = {'h264_nvenc', 'h264_qsv', 'h264_vaapi'}
                if is_10bit_src and video_enc_name in _8bit_only_encoders:
                    needs_pix_fmt_convert = True
                    self.log(f"Source is 10-bit ({src_pix_fmt}) — adding pixel format "
                             f"conversion for {video_enc_name}", 'INFO')

            # Edited subtitles — maps stream_index → (temp_srt_path, input_index)
            edited_subs = settings.get('edited_subs', {})
            # Build ordered list of edited sub inputs for consistent input indexing
            _edited_sub_inputs = sorted(edited_subs.items())  # [(stream_idx, path), ...]

            # ── Closed caption handling (ATSC A53 / EIA-608 / CEA-708) ──
            cc_srt_path = None
            has_cc = settings.get('has_closed_captions', False)
            if has_cc and settings.get('extract_cc', True):
                import tempfile
                import shutil as _shutil
                if _shutil.which('ccextractor'):
                    cc_tmp = tempfile.NamedTemporaryFile(suffix='_cc.srt', delete=False, dir=os.path.dirname(output_path))
                    cc_tmp.close()
                    self.log("Extracting ATSC A53 closed captions with ccextractor…", 'INFO')
                    if extract_closed_captions_to_srt(input_path, cc_tmp.name):
                        cc_srt_path = cc_tmp.name
                        self.log("Closed captions extracted to SRT successfully", 'SUCCESS')
                    else:
                        self.log("ccextractor could not extract caption data", 'WARNING')
                        try:
                            os.remove(cc_tmp.name)
                        except OSError:
                            pass
                else:
                    self.log("ccextractor not found — CC will be preserved via A53 passthrough only", 'INFO')

            # A53 CC passthrough: embed CC data in the output video bitstream
            # This preserves CC for players that support it (VLC, mpv, etc.)
            cc_passthrough_flags = []
            if has_cc and video_enc_name and video_enc_name != 'copy':
                flags = _A53CC_ENCODER_FLAGS.get(video_enc_name)
                if flags is not None:
                    cc_passthrough_flags = flags
                    self.log(f"A53 CC passthrough enabled for {video_enc_name}", 'INFO')

            # ── Chapter injection ──
            chapters_metadata_path = None
            chapters = settings.get('chapters', [])
            if chapters and not settings.get('strip_chapters', False):
                try:
                    from modules.chapters import chapters_to_ffmetadata
                except ImportError:
                    import importlib.util
                    _ch_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                             'modules', 'chapters.py')
                    _spec = importlib.util.spec_from_file_location('chapters', _ch_path)
                    _mod = importlib.util.module_from_spec(_spec)
                    _spec.loader.exec_module(_mod)
                    chapters_to_ffmetadata = _mod.chapters_to_ffmetadata
                chapters_metadata_path = chapters_to_ffmetadata(chapters)
                if chapters_metadata_path:
                    self.log(f"Adding {len(chapters)} chapters to output", 'INFO')

            def _build_base_cmd():
                """Build the common part of the ffmpeg command."""
                c = ['ffmpeg', '-y']
                if effective_hw and is_gpu and backend and transcode_mode in ['video', 'both'] and video_enc_name not in (None, 'copy'):
                    c.extend(backend['hwaccel'])
                c.extend(['-i', input_path])
                # Add external embed subtitle inputs
                for es in embed_subs:
                    c.extend(['-i', es['path']])
                # Add edited subtitle inputs
                for _si, _path in _edited_sub_inputs:
                    c.extend(['-i', _path])
                # Add extracted closed caption SRT as input
                if cc_srt_path:
                    c.extend(['-i', cc_srt_path])
                # Add chapter metadata file as input
                if chapters_metadata_path:
                    c.extend(['-i', chapters_metadata_path])
                return c

            def _edited_sub_input_idx(stream_index):
                """Return the ffmpeg input index for an edited subtitle, or None."""
                for i, (si, _path) in enumerate(_edited_sub_inputs):
                    if si == stream_index:
                        # Input 0 = main file, 1..N = embed_subs, N+1.. = edited subs
                        return 1 + len(embed_subs) + i
                return None

            def _add_video_args(c, pass_num=None):
                """Add video encoding arguments. pass_num: None=single, 1=first, 2=second."""
                if transcode_mode in ['video', 'both']:

                    if video_enc_name != 'copy':
                        # Video filters MUST come before -c:v for proper filter
                        # chain initialization with hwaccel_output_format surfaces
                        if has_burn_in:
                            sub_path = burn_in_subs[0]['path']
                            ext = Path(sub_path).suffix.lower()
                            # Escape special chars for ffmpeg filter syntax
                            escaped = sub_path.replace('\\', '\\\\\\\\').replace(':', '\\:').replace("'", "\\'")
                            if ext in ('.ass', '.ssa'):
                                c.extend(['-vf', f"ass='{escaped}'"])
                            else:
                                c.extend(['-vf', f"subtitles='{escaped}'"])
                        elif needs_pix_fmt_convert and backend:
                            # GPU hwaccel: convert pixel format on-device
                            c.extend(['-vf', backend['scale_filter']])
                        elif codec_info['cpu_encoder'] == 'prores_ks':
                            # ProRes requires 4:2:2 or 4:4:4
                            prores_profile = settings.get('preset', '') or 'hq'
                            if prores_profile in ('4444', '4444xq'):
                                pix = 'yuva444p10le'
                            else:
                                pix = 'yuv422p10le'
                            c.extend(['-vf', f'format={pix}'])

                    c.extend(['-c:v', video_enc_name])

                    # A53 CC passthrough flags
                    if cc_passthrough_flags:
                        c.extend(cc_passthrough_flags)

                    if video_enc_name != 'copy':
                        preset = settings.get('preset', '')
                        if preset:
                            if codec_info['cpu_encoder'] == 'libvpx-vp9' and encoder == 'cpu':
                                c.extend(['-cpu-used', preset])
                            elif codec_info['cpu_encoder'] == 'prores_ks' and encoder == 'cpu':
                                c.extend(['-profile:v', preset])
                            elif is_gpu and backend and backend.get('preset_flag'):
                                c.extend([backend['preset_flag'], preset])
                            elif not is_gpu:
                                c.extend(['-preset', preset])

                        if mode == 'crf':
                            crf_val = str(settings.get('crf', codec_info['crf_default']))
                            if is_gpu and backend:
                                cq_flag = backend.get('cq_flag')
                                if cq_flag:
                                    c.extend([cq_flag, crf_val])
                            elif codec_info['crf_flag']:
                                c.extend([codec_info['crf_flag'], crf_val])
                                if codec_info['cpu_encoder'] == 'libvpx-vp9':
                                    c.extend(['-b:v', '0'])
                        else:
                            bitrate = settings.get('bitrate', DEFAULT_BITRATE)
                            c.extend(['-b:v', bitrate])
                            if encoder == 'cpu' and codec_info['cpu_encoder'] not in ('libsvtav1', 'libvpx-vp9', 'prores_ks', 'mpeg4'):
                                c.extend(['-minrate', bitrate, '-maxrate', bitrate, '-bufsize', bitrate])
                            if use_gpu_multipass and backend:
                                c.extend(backend['multipass_args'])

                        if pass_num is not None:
                            c.extend(['-pass', str(pass_num)])
                            c.extend(['-passlogfile', passlog])

                elif transcode_mode == 'audio':
                    c.extend(['-c:v', 'copy'])

            def _add_audio_args(c):
                """Add audio encoding arguments."""
                EXPERIMENTAL_CODECS = {'opus', 'vorbis'}
                LOSSLESS_CODECS = {'flac', 'alac', 'pcm_s16le', 'pcm_s24le', 'wavpack', 'tta'}
                audio_codec = settings.get('audio_codec', 'aac')
                audio_bitrate = settings.get('audio_bitrate', '128k')
                if audio_codec == 'copy':
                    c.extend(['-c:a', 'copy'])
                else:
                    c.extend(['-c:a', audio_codec])
                    if audio_codec in EXPERIMENTAL_CODECS:
                        c.extend(['-strict', '-2'])
                    if audio_codec not in LOSSLESS_CODECS:
                        c.extend(['-b:a', audio_bitrate])

            def _add_subtitle_args(c):
                """Add subtitle stream arguments (internal + external embed).

                Output order: forced external subs first, then internal/configured
                subs, then remaining external subs.  This ensures forced tracks
                appear as the first subtitle stream(s) in the output.
                """
                container = settings.get('container', '.mkv')
                # Helper to compute the ffmpeg input index for the extracted CC SRT
                def _cc_input_idx():
                    return 1 + len(embed_subs) + len(_edited_sub_inputs)

                # AVI does not support embedded subtitle streams
                if container == '.avi':
                    c.extend(['-map', '0:v:0?', '-map', '0:a?'])
                    self.log("Subtitles skipped: AVI container does not support embedded subtitles", 'WARNING')
                    if cc_srt_path:
                        self.log("Closed captions also skipped: AVI does not support subtitles", 'WARNING')
                    return

                # MPEG-TS only supports DVB subtitles — drop text-based subs
                if container == '.ts':
                    c.extend(['-map', '0:v:0?', '-map', '0:a?'])
                    # Map through any DVB subtitle streams from the source
                    try:
                        int_streams = get_subtitle_streams(input_path)
                        for si, ist in enumerate(int_streams):
                            if ist.get('codec_name') == 'dvb_subtitle':
                                c.extend(['-map', f'0:s:{si}', f'-c:s:{si}', 'copy'])
                    except Exception:
                        pass
                    self.log("Text subtitles skipped: MPEG-TS container only supports DVB subtitles", 'WARNING')
                    if cc_srt_path:
                        self.log("Closed captions also skipped: MPEG-TS output does not support SRT", 'WARNING')
                    return

                sub_settings = settings.get('subtitle_settings', {})
                strip_internal = settings.get('strip_internal_subs', False)

                if not sub_settings and not embed_subs and not strip_internal and not edited_subs:
                    # Simple case: no per-file config, no external subs, no edits, keep internals
                    c.extend(['-map', '0:v:0?', '-map', '0:a?', '-map', '0:s?'])
                    # Handle subtitle codec compatibility between containers
                    BITMAP_CODECS = {'hdmv_pgs_subtitle', 'dvd_subtitle', 'dvb_subtitle'}
                    try:
                        int_streams = get_subtitle_streams(input_path)
                    except Exception:
                        int_streams = []

                    out_sub_idx = len(int_streams)

                    if container in ('.mp4', '.mov'):
                        # MP4/MOV only support mov_text — convert text subs, drop bitmap subs
                        if int_streams:
                            for si, ist in enumerate(int_streams):
                                if ist['codec_name'] in BITMAP_CODECS:
                                    c.extend([f'-c:s:{si}', 'copy'])
                                    self.log(f"Subtitle stream {ist['index']} ({ist['codec_name']}): "
                                             f"bitmap format, may not be supported in {container}", 'WARNING')
                                else:
                                    c.extend([f'-c:s:{si}', 'mov_text'])
                        else:
                            c.extend(['-c:s', 'mov_text'])
                    else:
                        # MKV/other containers: copy most subs, but convert mov_text to srt
                        # (mov_text is MP4-only and not supported in MKV)
                        if int_streams and any(s['codec_name'] == 'mov_text' for s in int_streams):
                            for si, ist in enumerate(int_streams):
                                if ist['codec_name'] == 'mov_text':
                                    c.extend([f'-c:s:{si}', 'srt'])
                                else:
                                    c.extend([f'-c:s:{si}', 'copy'])
                        else:
                            c.extend(['-c:s', 'copy'])

                    # Map extracted closed captions as an additional subtitle track
                    if cc_srt_path:
                        cc_idx = _cc_input_idx()
                        c.extend(['-map', f'{cc_idx}:s:0'])
                        if container in ('.mp4', '.mov'):
                            c.extend([f'-c:s:{out_sub_idx}', 'mov_text'])
                        else:
                            c.extend([f'-c:s:{out_sub_idx}', 'srt'])
                        c.extend([f'-metadata:s:s:{out_sub_idx}', 'language=eng'])
                        c.extend([f'-metadata:s:s:{out_sub_idx}', 'title=Closed Captions (CC)'])
                        self.log("Mapping extracted closed captions as subtitle track", 'INFO')
                    return

                # We need explicit mapping when we have external subs, per-file config,
                # or are stripping internal tracks
                c.extend(['-map', '0:v:0?', '-map', '0:a?'])
                out_sub_idx = 0
                container = settings.get('container', '.mkv')

                # Bitmap subtitle codecs that cannot be converted to mov_text
                _BITMAP_SUB_CODECS = {'hdmv_pgs_subtitle', 'dvd_subtitle', 'dvb_subtitle'}

                def _internal_sub_codec(ist):
                    """Return the codec to use when copying an internal subtitle stream."""
                    if container in ('.mp4', '.mov') and ist.get('codec_name') not in _BITMAP_SUB_CODECS:
                        return 'mov_text'
                    return 'copy'

                def _map_embed_sub(i, es):
                    """Map a single external embed subtitle input.
                    i is the index in embed_subs (input_idx = 1 + i)."""
                    nonlocal out_sub_idx
                    input_idx = 1 + i
                    c.extend(['-map', f'{input_idx}:s:0'])
                    if container in ('.mp4', '.mov'):
                        codec = 'mov_text'
                    elif container == '.ts':
                        codec = 'dvb_subtitle'
                    else:
                        codec = es.get('format', 'srt')
                    c.extend([f'-c:s:{out_sub_idx}', codec])
                    # Language metadata
                    lang = es.get('language', 'und')
                    if lang and lang != 'und':
                        c.extend([f'-metadata:s:s:{out_sub_idx}', f'language={lang}'])
                    # Disposition flags (default / forced / hearing_impaired)
                    disp_parts = []
                    if es.get('default'):
                        disp_parts.append('default')
                    if es.get('sdh'):
                        disp_parts.append('hearing_impaired')
                    if es.get('forced'):
                        disp_parts.append('forced')
                    if disp_parts:
                        c.extend([f'-disposition:s:{out_sub_idx}', '+'.join(disp_parts)])
                    # Track title — makes flags visible in MediaInfo and players
                    title_parts = []
                    if lang and lang != 'und':
                        lang_name = lang
                        for lc, ln in SUBTITLE_LANGUAGES:
                            if lc == lang:
                                lang_name = ln
                                break
                        title_parts.append(lang_name)
                    if es.get('sdh'):
                        title_parts.append('SDH')
                    if es.get('forced'):
                        title_parts.append('Forced')
                    if title_parts:
                        c.extend([f'-metadata:s:s:{out_sub_idx}', f'title={" - ".join(title_parts)}'])
                    out_sub_idx += 1

                # ── Phase 1: Map forced external subs first ──
                for i, es in enumerate(embed_subs):
                    if es.get('forced'):
                        _map_embed_sub(i, es)

                # ── Phase 2: Map internal / per-file-configured subs ──
                if strip_internal:
                    self.log("Stripping internal subtitle tracks (replaced by external subs)", 'INFO')
                elif not sub_settings:
                    # Map internal subtitle streams, auto-replacing any that
                    # conflict with external subs of the same language + type.
                    try:
                        internal_streams = get_subtitle_streams(input_path)
                    except Exception:
                        internal_streams = []

                    if embed_subs and internal_streams:
                        replaced = []
                        for ist in internal_streams:
                            ist_lang = ist.get('language', 'und')
                            ist_forced = ist.get('forced', False)
                            # Check if any external sub replaces this internal one
                            conflict = False
                            for es in embed_subs:
                                es_lang = es.get('language', 'und')
                                es_forced = es.get('forced', False)
                                # Match: same language (or either is 'und') and same type
                                lang_match = (ist_lang == es_lang
                                              or ist_lang == 'und'
                                              or es_lang == 'und')
                                type_match = (ist_forced == es_forced)
                                if lang_match and type_match:
                                    conflict = True
                                    break
                            if conflict:
                                replaced.append(ist)
                            else:
                                c.extend(['-map', f"0:{ist['index']}"])
                                c.extend([f'-c:s:{out_sub_idx}', _internal_sub_codec(ist)])
                                out_sub_idx += 1
                        if replaced:
                            labels = []
                            for r in replaced:
                                rl = r.get('language', 'und')
                                rt = ' (forced)' if r.get('forced') else ''
                                labels.append(f"{rl}{rt}")
                            self.log(f"Auto-replaced {len(replaced)} internal subtitle(s) "
                                     f"matching external subs: {', '.join(labels)}", 'INFO')
                    elif internal_streams:
                        # No external subs — keep all internal streams
                        if not edited_subs:
                            c.extend(['-map', '0:s?'])
                            for ist in internal_streams:
                                c.extend([f'-c:s:{out_sub_idx}', _internal_sub_codec(ist)])
                                out_sub_idx += 1
                        else:
                            # Some streams edited — map individually
                            for ist in internal_streams:
                                ed_input = _edited_sub_input_idx(ist['index'])
                                if ed_input is not None:
                                    c.extend(['-map', f'{ed_input}:s:0'])
                                    c.extend([f'-c:s:{out_sub_idx}', 'srt'])
                                    self.log(f"Using edited subtitle for stream #{ist['index']}", 'INFO')
                                else:
                                    c.extend(['-map', f"0:{ist['index']}"])
                                    c.extend([f'-c:s:{out_sub_idx}', _internal_sub_codec(ist)])
                                out_sub_idx += 1
                else:
                    for stream_index, ss in sub_settings.items():
                        if not ss.get('keep', True):
                            continue
                        fmt = ss.get('format', 'copy')
                        if fmt == 'drop':
                            continue
                        if fmt == 'extract only':
                            fmt = 'copy'
                        # Check if this stream has been edited
                        ed_input = _edited_sub_input_idx(stream_index)
                        if ed_input is not None:
                            c.extend(['-map', f'{ed_input}:s:0'])
                            c.extend([f'-c:s:{out_sub_idx}', 'srt'])
                            self.log(f"Using edited subtitle for stream #{stream_index}", 'INFO')
                        else:
                            c.extend(['-map', f"0:{stream_index}"])
                            c.extend([f'-c:s:{out_sub_idx}', fmt])
                        out_sub_idx += 1

                # ── Phase 3: Map remaining (non-forced) external subs ──
                for i, es in enumerate(embed_subs):
                    if not es.get('forced'):
                        _map_embed_sub(i, es)

                # ── Phase 4: Map extracted closed captions ──
                if cc_srt_path:
                    cc_idx = _cc_input_idx()
                    c.extend(['-map', f'{cc_idx}:s:0'])
                    if container in ('.mp4', '.mov'):
                        c.extend([f'-c:s:{out_sub_idx}', 'mov_text'])
                    else:
                        c.extend([f'-c:s:{out_sub_idx}', 'srt'])
                    c.extend([f'-metadata:s:s:{out_sub_idx}', 'language=eng'])
                    c.extend([f'-metadata:s:s:{out_sub_idx}', 'title=Closed Captions (CC)'])
                    out_sub_idx += 1
                    self.log("Mapping extracted closed captions as subtitle track", 'INFO')

            def _add_metadata_args(c):
                """Add metadata cleanup and track metadata flags."""
                # Add chapters or strip chapters (mutually exclusive)
                if chapters_metadata_path:
                    # Map chapters from the metadata file input
                    ch_idx = 1 + len(embed_subs) + len(_edited_sub_inputs) + (1 if cc_srt_path else 0)
                    c.extend(['-map_chapters', str(ch_idx)])
                elif settings.get('strip_chapters', False):
                    c.extend(['-map_chapters', '-1'])
                    self.log("Stripping chapters from output", 'INFO')

                # Strip global tags/metadata
                if settings.get('strip_metadata_tags', False):
                    c.extend(['-map_metadata', '-1'])
                    self.log("Stripping global tags/metadata from output", 'INFO')

                # Set track metadata (language, clear names/title)
                if settings.get('set_track_metadata', False):
                    video_lang = settings.get('meta_video_lang', 'und')
                    audio_lang = settings.get('meta_audio_lang', 'eng')
                    sub_lang   = settings.get('meta_sub_lang', 'eng')
                    # Container title
                    c.extend(['-metadata', 'title='])
                    # Video track
                    c.extend(['-metadata:s:v:0', f'language={video_lang}',
                              '-metadata:s:v:0', 'title='])
                    # Audio track
                    c.extend(['-metadata:s:a:0', f'language={audio_lang}',
                              '-metadata:s:a:0', 'title='])
                    # First subtitle track
                    c.extend(['-metadata:s:s:0', f'language={sub_lang}',
                              '-metadata:s:s:0', f'title={sub_lang.upper() if len(sub_lang) <= 3 else sub_lang}'])
                    self.log(f"Setting track metadata: video={video_lang}, audio={audio_lang}, sub={sub_lang}", 'INFO')

                # Edition tag — write to container title
                # Placed after set_track_metadata so it overrides the title= clear
                # Works independently — doesn't require set_track_metadata to be on
                edition = settings.get('edition_tag', '')
                if edition:
                    c.extend(['-metadata', f'title={edition}'])
                    self.log(f"Setting edition tag: {edition}", 'INFO')

            # ── Log what we're about to do ──
            self.log(f"Video codec: {video_enc_name}", 'INFO')
            self.log(f"Mode: {mode}" + (" (two-pass)" if use_two_pass else " (GPU multipass)" if use_gpu_multipass else ""), 'INFO')
            if hw_decode and is_gpu and backend:
                self.log(f"Hardware decode: {backend['hwaccel'][1]} enabled", 'INFO')

            import tempfile
            passlog = None

            if use_two_pass:
                # Create a temp passlog file prefix
                tmp = tempfile.NamedTemporaryFile(delete=False, suffix='_passlog')
                passlog = tmp.name
                tmp.close()

                # ── Pass 1 ──
                self.log("Two-pass encoding: starting pass 1 of 2...", 'INFO')
                cmd1 = _build_base_cmd()
                _add_video_args(cmd1, pass_num=1)
                cmd1.extend(['-an'])          # no audio in pass 1
                cmd1.extend(['-f', 'null', '/dev/null'])
                self.log(f"Pass 1 command: {' '.join(cmd1)}", 'INFO')

                if not self._run_process(cmd1, input_path, pass_label="Pass 1/2"):
                    return False
                if self.is_stopped:
                    return False

                # ── Pass 2 ──
                self.log("Two-pass encoding: starting pass 2 of 2...", 'INFO')
                cmd2 = _build_base_cmd()
                _add_video_args(cmd2, pass_num=2)
                _add_audio_args(cmd2)
                _add_subtitle_args(cmd2)
                _add_metadata_args(cmd2)
                cmd2.append(output_path)
                self.log(f"Pass 2 command: {' '.join(cmd2)}", 'INFO')

                success = self._run_process(cmd2, input_path, pass_label="Pass 2/2")

                # Clean up passlog files
                for ext in ('', '.log', '.log.mbtree', '-0.log', '-0.log.mbtree'):
                    try:
                        os.remove(passlog + ext)
                    except FileNotFoundError:
                        pass

                if success:
                    self.log(f"Two-pass complete: {os.path.basename(output_path)}", "SUCCESS")
                return success

            else:
                # ── Single pass ──
                cmd = _build_base_cmd()
                _add_video_args(cmd)
                _add_audio_args(cmd)
                _add_subtitle_args(cmd)
                _add_metadata_args(cmd)
                cmd.append(output_path)
                self.log(f"Command: {' '.join(cmd)}", 'INFO')
                return self._run_process(cmd, input_path)
            
            # Audio encoding
            audio_codec = settings.get('audio_codec', 'aac')
            audio_bitrate = settings.get('audio_bitrate', '128k')
            
            # Codecs that require -strict -2 (experimental in ffmpeg)
            EXPERIMENTAL_CODECS = {'opus', 'vorbis'}
            # Lossless codecs — don't set a bitrate target
            LOSSLESS_CODECS = {'flac', 'alac', 'pcm_s16le', 'pcm_s24le', 'wavpack', 'tta'}

            if audio_codec == 'copy':
                cmd.extend(['-c:a', 'copy'])
                self.log("Audio stream: copying (no re-encode)", 'INFO')
            else:
                cmd.extend(['-c:a', audio_codec])
                if audio_codec in EXPERIMENTAL_CODECS:
                    cmd.extend(['-strict', '-2'])
                    self.log(f"Audio codec {audio_codec}: enabling experimental mode", 'INFO')
                if audio_codec not in LOSSLESS_CODECS:
                    cmd.extend(['-b:a', audio_bitrate])
                    self.log(f"Audio encoding: {audio_codec} at {audio_bitrate}", 'INFO')
                else:
                    self.log(f"Audio encoding: {audio_codec} (lossless)", 'INFO')
            
            # Subtitle handling
            sub_settings = settings.get('subtitle_settings', {})
            if not sub_settings:
                # No per-file settings — copy all subtitle streams
                cmd.extend(['-c:s', 'copy'])
                self.log("Subtitle streams: copying all (no re-encode)", 'INFO')
            else:
                # Explicit per-stream mapping — must also map video and audio
                # otherwise ffmpeg disables default stream selection
                cmd.extend(['-map', '0:v:0?', '-map', '0:a?'])
        except Exception as e:
            self.log(f"Conversion error: {str(e)}", "ERROR")
            return False
        finally:
            self.current_process = None
            # Clean up extracted CC temp file
            if cc_srt_path:
                try:
                    os.remove(cc_srt_path)
                except OSError:
                    pass
            # Clean up chapter metadata temp file
            if chapters_metadata_path:
                try:
                    os.remove(chapters_metadata_path)
                except OSError:
                    pass

    def _run_process(self, cmd, input_path, pass_label=None):
        """Run an ffmpeg subprocess, parse progress, handle pause/stop. Returns True on success."""
        import time
        try:
            self.current_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1
            )

            duration = get_video_duration(input_path)
            label = f"[{pass_label}] " if pass_label else ""

            # ── ETA calculation ──
            # Blended approach: FPS-based ETA (responsive, good early) mixed
            # with wall-clock average ETA (stable, good over time).  The blend
            # shifts from 100% FPS-based → 100% wall-clock over BLEND_SECS.
            process_start = time.monotonic()
            paused_total = 0.0
            pause_began = None
            BLEND_SECS = 30             # seconds to fully transition to wall-clock ETA

            # Get total frame count for FPS-based ETA
            total_frames = None
            try:
                probe_cmd = [
                    'ffprobe', '-v', 'error', '-select_streams', 'v:0',
                    '-show_entries', 'stream=nb_frames,r_frame_rate',
                    '-of', 'default=noprint_wrappers=1', input_path
                ]
                probe_result = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=10)
                for pline in probe_result.stdout.strip().split('\n'):
                    if pline.startswith('nb_frames=') and pline.split('=')[1].strip().isdigit():
                        total_frames = int(pline.split('=')[1].strip())
                    elif pline.startswith('r_frame_rate='):
                        num, den = pline.split('=')[1].strip().split('/')
                        source_fps = float(num) / float(den)
                # If nb_frames not available, estimate from duration × fps
                if total_frames is None and duration and source_fps:
                    total_frames = int(duration * source_fps)
            except Exception:
                pass

            for line in self.current_process.stdout:
                if self.is_stopped:
                    self.current_process.terminate()
                    self.log("Conversion stopped by user", "WARNING")
                    return False

                while self.is_paused:
                    if pause_began is None:
                        pause_began = time.monotonic()
                    if self.is_stopped:
                        return False
                    time.sleep(0.5)
                if pause_began is not None:
                    paused_total += time.monotonic() - pause_began
                    pause_began = None

                line = line.strip()

                if 'time=' in line:
                    match = re.search(r'time=(\d+):(\d+):(\d+)', line)
                    if match and duration:
                        h, m, s = map(int, match.groups())
                        current_time = h * 3600 + m * 60 + s
                        progress = (current_time / duration) * 100

                        # Parse frame, fps, and speed from the same line
                        frame_match = re.search(r'frame=\s*(\d+)', line)
                        fps_match   = re.search(r'fps=\s*([\d.]+)', line)
                        speed_match = re.search(r'speed=\s*([\d.]+)x', line)
                        cur_frame = int(frame_match.group(1))   if frame_match else None
                        fps       = float(fps_match.group(1))   if fps_match   else None
                        speed     = float(speed_match.group(1)) if speed_match else None

                        # Calculate blended ETA
                        eta = None
                        wall_elapsed = time.monotonic() - process_start - paused_total

                        # FPS-based ETA: remaining_frames / current_fps
                        fps_eta = None
                        if fps and fps > 0 and total_frames and cur_frame is not None:
                            remaining_frames = total_frames - cur_frame
                            if remaining_frames > 0:
                                fps_eta = remaining_frames / fps

                        # Wall-clock ETA: remaining_video / avg_speed
                        wall_eta = None
                        if current_time > 0 and wall_elapsed > 0:
                            avg_speed = current_time / wall_elapsed
                            remaining_video_secs = duration - current_time
                            wall_eta = remaining_video_secs / avg_speed

                        # Blend: start with FPS-based, shift to wall-clock over time
                        if fps_eta is not None and wall_eta is not None:
                            w = min(wall_elapsed / BLEND_SECS, 1.0)
                            eta = w * wall_eta + (1 - w) * fps_eta
                        elif fps_eta is not None:
                            eta = fps_eta
                        elif wall_eta is not None:
                            eta = wall_eta

                        if self.progress_callback:
                            self.progress_callback(progress, f"{label}{line}",
                                                   fps=fps, eta=eta, pass_label=pass_label)

                if any(kw in line.lower() for kw in ['error', 'warning', 'failed']):
                    self.log(f"{label}{line}", "ERROR" if 'error' in line.lower() else "WARNING")

            return_code = self.current_process.wait()
            if return_code == 0:
                if not pass_label:
                    self.log(f"Conversion complete: {os.path.basename(cmd[-1])}", "SUCCESS")
                return True
            else:
                self.log(f"{label}Conversion failed with code {return_code}", "ERROR")
                return False

        except Exception as e:
            self.log(f"Process error: {e}", "ERROR")
            return False
        finally:
            self.current_process = None
    
    def pause(self):
        """Pause conversion"""
        self.is_paused = True
        self.log("Conversion paused", "WARNING")
    
    def resume(self):
        """Resume conversion"""
        self.is_paused = False
        self.log("Conversion resumed", "INFO")
    
    def stop(self):
        """Stop conversion"""
        self.is_stopped = True
        if self.current_process:
            self.current_process.terminate()

# ============================================================================
# Main Application Class
# ============================================================================

class VideoConverterApp:
    """Main GUI Application"""
    
    def __init__(self, root):
        self.root = root
        self.root.title(f"{APP_NAME} v{APP_VERSION}")
        self.root.geometry("1200x800")
        self.root.minsize(900, 600)
        
        # State
        self.working_dir = Path.home()
        self.output_dir = None  # None means "same as source file"
        self.recent_folders = []  # list of Path strings, max 5
        self.custom_ad_patterns = []  # user-defined ad patterns for subtitle editor
        self.custom_cap_words = []   # user-defined names for Fix ALL CAPS filter
        self.custom_spell_words = [] # user-defined words for spell checker dictionary
        self.custom_replacements = []  # list of [find, replace] pairs for batch S&R
        self.files = []
        self.converter = VideoConverter(
            log_callback=self.add_log,
            progress_callback=self.update_progress
        )
        self.is_converting = False
        self.current_file_index = 0
        self.conversion_thread = None
        self.start_time = None
        self.current_output_path = None
        # Batch ETA tracking
        self._batch_speed_samples = []  # list of (duration_secs, wall_secs) per completed file
        self._file_start_time = None    # monotonic time when current file started encoding
        
        # Settings
        self.encoder_mode = tk.StringVar(value='cpu')  # updated after GPU detection
        self.video_codec = tk.StringVar(value='H.265 / HEVC')
        self.container_format = tk.StringVar(value='.mkv')
        self.transcode_mode = tk.StringVar(value='video')  # 'video', 'audio', or 'both'
        self.quality_mode = tk.StringVar(value='bitrate')
        self.bitrate = tk.StringVar(value='2M')
        self.crf = tk.StringVar(value='23')
        self.cpu_preset = tk.StringVar(value='ultrafast')
        self.gpu_preset = tk.StringVar(value='p4')
        self.skip_existing = tk.BooleanVar(value=True)
        self.delete_originals = tk.BooleanVar(value=False)
        self.strip_internal_subs = tk.BooleanVar(value=False)
        self.two_pass = tk.BooleanVar(value=False)
        self.verify_output = tk.BooleanVar(value=True)
        self.notify_sound = tk.BooleanVar(value=True)
        self.default_player = tk.StringVar(value='auto')
        self.notify_sound_file = tk.StringVar(value='complete')

        # Audio settings
        self.audio_codec = tk.StringVar(value='aac')
        self.audio_bitrate = tk.StringVar(value='128k')

        # Metadata cleanup settings
        self.strip_chapters = tk.BooleanVar(value=False)
        self.strip_metadata_tags = tk.BooleanVar(value=False)
        self.set_track_metadata = tk.BooleanVar(value=False)
        self.meta_video_lang = tk.StringVar(value='und')
        self.meta_audio_lang = tk.StringVar(value='eng')
        self.meta_sub_lang = tk.StringVar(value='eng')
        # Edition tagging
        self.edition_tag = tk.StringVar(value='')
        self.edition_in_filename = tk.BooleanVar(value=False)
        # Chapter insertion
        self.add_chapters = tk.BooleanVar(value=False)
        self.chapter_interval = tk.IntVar(value=5)  # minutes

        # Check system capabilities
        self.has_ffmpeg, self.ffmpeg_version = check_ffmpeg()
        self.gpu_backends = detect_gpu_backends()   # {backend_id: gpu_name_or_True}
        self.has_gpu = bool(self.gpu_backends)

        # Build the encoder choices list: ['cpu', 'nvenc', 'qsv', 'vaapi', ...]
        self._encoder_choices = ['cpu'] + list(self.gpu_backends.keys())

        # Backward-compat: pick first available GPU as default, or 'cpu'
        self._default_gpu = self._encoder_choices[1] if self.has_gpu else 'cpu'
        # Set encoder mode to best available GPU (or cpu)
        self.encoder_mode.set(self._default_gpu)

        # Hardware decode defaults to on if any GPU backend is available
        self.hw_decode = tk.BooleanVar(value=self.has_gpu)
        
        # Setup UI
        self.setup_ui()
        # Don't auto-scan on startup — user must select a folder
        # (rglob on home dir would be too slow)
        
        # Show welcome message
        if not self.has_ffmpeg:
            messagebox.showwarning(
                "ffmpeg Not Found",
                "ffmpeg is not installed or not in PATH.\n\n"
                "Please install ffmpeg:\n"
                "Ubuntu/Debian: sudo apt install ffmpeg\n"
                "Fedora: sudo dnf install ffmpeg\n"
                "macOS: brew install ffmpeg\n"
                "Windows: Download from ffmpeg.org"
            )
    
    def setup_ui(self):
        """Setup the user interface"""
        # Menu bar
        self.setup_menubar()

        # Main container
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.grid(row=0, column=0, sticky="nsew")
        
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        main_frame.columnconfigure(0, weight=1)
        main_frame.rowconfigure(2, weight=1)  # file list is now the expanding row

        # Header
        self.setup_header(main_frame)

        # Settings panel
        self.setup_settings(main_frame)

        # File list
        self.setup_file_list(main_frame)

        # Status bar
        self.setup_status_bar(main_frame)

        # Create the detached log window (hidden initially)
        self.setup_log_panel()

        # Initialize preset combo to match the default encoder selection
        self.on_encoder_change(silent=True)

        # Load saved preferences
        self.load_preferences()
    
    def setup_menubar(self):
        """Setup the menu bar."""
        menubar = tk.Menu(self.root)
        self.root.configure(menu=menubar)

        # File menu
        file_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="File", menu=file_menu)

        file_menu.add_command(label="Open File(s)...",
                              accelerator="Ctrl+O",
                              command=self.open_files)
        file_menu.add_command(label="Open Folder...",
                              accelerator="Ctrl+Shift+O",
                              command=self.change_folder)
        file_menu.add_separator()

        # Recent Folders submenu
        self.recent_menu = tk.Menu(file_menu, tearoff=0)
        file_menu.add_cascade(label="Recent Folders", menu=self.recent_menu)
        self._rebuild_recent_menu()

        file_menu.add_separator()
        file_menu.add_command(label="Clear File List",
                              command=self.clear_files)
        file_menu.add_separator()
        file_menu.add_command(label="Exit",
                              accelerator="Ctrl+Q",
                              command=self.root.quit)

        # Settings menu
        settings_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Settings", menu=settings_menu)

        settings_menu.add_command(label="Default Settings...",
                                  command=self.show_default_settings)
        settings_menu.add_separator()
        settings_menu.add_command(label="Reset to Defaults",
                                  command=self.reset_preferences)

        # View menu
        view_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="View", menu=view_menu)

        view_menu.add_command(label="Show/Hide Log",
                              accelerator="Ctrl+L",
                              command=self.toggle_log_window)
        view_menu.add_command(label="Show/Hide Settings Panel",
                              accelerator="Ctrl+Shift+S",
                              command=self.toggle_settings_panel)

        # Tools menu
        tools_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Tools", menu=tools_menu)

        tools_menu.add_command(label="▶ Play Source File",
                               accelerator="Ctrl+P",
                               command=self.play_source_file)
        tools_menu.add_command(label="▶ Play Output File",
                               accelerator="Ctrl+Shift+P",
                               command=self.play_output_file)
        tools_menu.add_separator()
        tools_menu.add_command(label="Media Details...",
                               accelerator="Ctrl+I",
                               command=self.show_media_info)
        tools_menu.add_command(label="Enhanced Media Details...",
                               accelerator="Ctrl+Shift+I",
                               command=self.show_enhanced_media_info)
        tools_menu.add_command(label="Test Encode (30s)...",
                               accelerator="Ctrl+T",
                               command=self.test_encode)
        tools_menu.add_separator()
        tools_menu.add_command(label="Open Output Folder",
                               accelerator="Ctrl+Shift+F",
                               command=self.open_output_folder)
        tools_menu.add_separator()
        tools_menu.add_command(label="✏ Subtitle Editor...",
                               command=self.open_standalone_subtitle_editor)
        tools_menu.add_command(label="📦 Batch Filter Subtitles...",
                               command=self.open_batch_filter)
        tools_menu.add_separator()
        tools_menu.add_command(label="🔧 Media Processor...",
                               accelerator="Ctrl+M",
                               command=self.open_media_processor)
        tools_menu.add_command(label="📺 TV Show Renamer...",
                               command=self.open_tv_renamer)
        # Help menu
        help_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Help", menu=help_menu)

        help_menu.add_command(label="User Manual",
                              command=self.show_user_manual)
        help_menu.add_command(label="Keyboard Shortcuts",
                              accelerator="F1",
                              command=self.show_keyboard_shortcuts)
        help_menu.add_separator()
        help_menu.add_command(label="About",
                              command=self.show_about)

        # Bind keyboard shortcuts
        self.root.bind('<Control-o>', lambda e: self.open_files())
        self.root.bind('<Control-O>', lambda e: self.change_folder())
        self.root.bind('<Control-q>', lambda e: self.root.quit())
        self.root.bind('<Control-l>', lambda e: self.toggle_log_window())
        self.root.bind('<Control-L>', lambda e: self.toggle_settings_panel())
        self.root.bind('<F1>',        lambda e: self.show_keyboard_shortcuts())
        self.root.bind('<Control-p>', lambda e: self.play_source_file())
        self.root.bind('<Control-P>', lambda e: self.play_output_file())
        self.root.bind('<Control-i>', lambda e: self.show_media_info())
        self.root.bind('<Control-I>', lambda e: self.show_enhanced_media_info())
        self.root.bind('<Control-t>', lambda e: self.test_encode())
        self.root.bind('<Control-F>', lambda e: self.open_output_folder())
        self.root.bind('<Control-m>', lambda e: self.open_media_processor())

    def open_files(self):
        """Open a file picker and add selected video files to the queue."""
        filetypes = [
            ("Video files", " ".join(f"*{e}" for e in sorted(VIDEO_EXTENSIONS))),
            ("All files", "*.*")
        ]
        paths = filedialog.askopenfilenames(
            title="Select Video File(s)",
            filetypes=filetypes,
            initialdir=self.working_dir
        )
        if not paths:
            return
        added = 0
        for path in paths:
            added += self._add_file_to_list(Path(path))
        if added:
            self.add_log(f"Added {added} file(s) via File menu.", 'INFO')
        else:
            self.add_log("No new files added (already in list or unsupported format).", 'WARNING')

    def setup_header(self, parent):
        """Setup header section"""
        header_frame = ttk.Frame(parent)
        header_frame.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        header_frame.columnconfigure(0, weight=1)
        
        # Title
        title_frame = ttk.Frame(header_frame)
        title_frame.grid(row=0, column=0, sticky="w")
        
        # Logo image (falls back to emoji if file missing or PIL unavailable)
        self.logo_image = None
        try:
            from PIL import Image, ImageTk
            _logo_path = Path(__file__).parent / 'logo_transparent.png'
            if _logo_path.exists():
                _img = Image.open(_logo_path)
                _img = _img.resize((32, 32), Image.LANCZOS)
                self.logo_image = ImageTk.PhotoImage(_img)
        except Exception:
            pass

        if self.logo_image:
            tk.Label(title_frame, image=self.logo_image,
                     bg=self.root.cget('bg'), bd=0).pack(side='left', padx=(0, 6))
            ttk.Label(title_frame, text=APP_NAME,
                      font=('Helvetica', 18, 'bold')).pack(side='left')
        else:
            ttk.Label(title_frame, text=f"🎬 {APP_NAME}",
                      font=('Helvetica', 18, 'bold')).pack(side='left')
        
        # ── Encoder selector (right side of title row) ──
        encoder_frame = ttk.Frame(header_frame)
        encoder_frame.grid(row=0, column=1, sticky="e", padx=10)

        # Build display labels for the encoder combobox
        self._encoder_labels = {}  # display_label -> backend_id
        self._encoder_ids = {}     # backend_id -> display_label
        cpu_label = 'CPU'
        self._encoder_labels[cpu_label] = 'cpu'
        self._encoder_ids['cpu'] = cpu_label
        for bid in self.gpu_backends:
            backend = GPU_BACKENDS[bid]
            gpu_name = self.gpu_backends[bid]
            if gpu_name and gpu_name is not True:
                # Extract short GPU model name from detection output
                short_name = _short_gpu_name(gpu_name, bid)
                lbl_text = f"{short_name} ({backend['short']})"
            else:
                lbl_text = backend['label']
            self._encoder_labels[lbl_text] = bid
            self._encoder_ids[bid] = lbl_text

        self.encoder_combo = ttk.Combobox(
            encoder_frame, state='readonly',
            values=list(self._encoder_labels.keys()),
            width=max(len(l) for l in self._encoder_labels) + 2)
        self.encoder_combo.set(self._encoder_ids.get(self.encoder_mode.get(), cpu_label))
        self.encoder_combo.pack(side='left', padx=(0, 8))
        self.encoder_combo.bind('<<ComboboxSelected>>', lambda e: self._on_encoder_combo())

        # Hardware decode checkbox
        self.hw_decode_check = tk.Checkbutton(
            encoder_frame, text="HW Decode",
            variable=self.hw_decode,
            state='normal' if self.has_gpu else 'disabled',
            relief='flat', bd=0)
        self.hw_decode_check.pack(side='left')

        # ── Separator ──
        ttk.Separator(header_frame, orient='horizontal').grid(
            row=1, column=0, columnspan=2, sticky='ew', pady=(6, 4))

        # ── Toolbar row: folder controls + output path ──
        toolbar = ttk.Frame(header_frame)
        toolbar.grid(row=2, column=0, columnspan=2, sticky='ew')
        toolbar.columnconfigure(2, weight=1)  # output path stretches

        ttk.Button(toolbar, text="📁 Change Folder",
                   command=self.change_folder).grid(row=0, column=0, padx=(0, 4))
        ttk.Button(toolbar, text="🔄 Refresh",
                   command=self.refresh_files).grid(row=0, column=1, padx=(0, 8))

        # Output path display (stretches to fill)
        out_inner = ttk.Frame(toolbar)
        out_inner.grid(row=0, column=2, sticky='ew', padx=(4, 4))
        out_inner.columnconfigure(1, weight=1)

        ttk.Label(out_inner, text="Output:").grid(row=0, column=0, padx=(0, 4))
        self.output_dir_label = ttk.Label(out_inner, text="Same as source file",
                                          foreground='gray', anchor='w')
        self.output_dir_label.grid(row=0, column=1, sticky='ew')

        ttk.Button(toolbar, text="📂 Set Output",
                   command=self.change_output_folder).grid(row=0, column=3, padx=(4, 4))
        ttk.Button(toolbar, text="✖ Reset",
                   command=self.reset_output_folder).grid(row=0, column=4)

    def setup_settings(self, parent):
        """Setup settings panel"""
        self.settings_frame = ttk.LabelFrame(parent, text="Settings", padding=10)
        self.settings_frame.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        settings_frame = self.settings_frame
        settings_frame.columnconfigure(1, weight=1)
        
        # Video Codec selector - Row 0
        row = 0
        ttk.Label(settings_frame, text="Video Codec:").grid(row=row, column=0, sticky='w')
        codec_frame = ttk.Frame(settings_frame)
        codec_frame.grid(row=row, column=1, sticky='w')
        self.codec_combo = ttk.Combobox(codec_frame, textvariable=self.video_codec,
                                        values=list(VIDEO_CODEC_MAP.keys()),
                                        width=22, state='readonly')
        self.codec_combo.pack(side='left')
        self.codec_combo.bind('<<ComboboxSelected>>', self.on_video_codec_change)

        ttk.Label(codec_frame, text="  Container:").pack(side='left')
        self.container_combo = ttk.Combobox(codec_frame, textvariable=self.container_format,
                                            values=['.mkv', '.mp4', '.webm', '.avi', '.mov', '.ts'],
                                            width=7, state='readonly')
        self.container_combo.pack(side='left', padx=(2, 0))

        # Transcode mode (Video, Audio, or Both)
        row = 1
        ttk.Label(settings_frame, text="Transcode Mode:").grid(row=row, column=0, sticky='w')
        
        mode_frame = ttk.Frame(settings_frame)
        mode_frame.grid(row=row, column=1, sticky='w')
        
        ttk.Radiobutton(mode_frame, text="🎬 Video Only",
                       variable=self.transcode_mode, value='video',
                       command=self.on_transcode_mode_change).pack(side='left')
        ttk.Radiobutton(mode_frame, text="🎵 Audio Only",
                       variable=self.transcode_mode, value='audio',
                       command=self.on_transcode_mode_change).pack(side='left', padx=10)
        ttk.Radiobutton(mode_frame, text="🎬🎵 Both",
                       variable=self.transcode_mode, value='both',
                       command=self.on_transcode_mode_change).pack(side='left', padx=10)
        
        # Quality mode (only shown for video/both modes)
        row = 2
        self.quality_mode_frame = ttk.Frame(settings_frame)
        self.quality_mode_frame.grid(row=row, column=0, columnspan=2, sticky='w', pady=5)
        
        ttk.Label(self.quality_mode_frame, text="Quality Mode:").pack(side='left')
        
        mode_sub_frame = ttk.Frame(self.quality_mode_frame)
        mode_sub_frame.pack(side='left', padx=5)
        
        ttk.Radiobutton(mode_sub_frame, text="Bitrate (fixed size)",
                       variable=self.quality_mode, value='bitrate',
                       command=self.on_quality_mode_change).pack(side='left')
        ttk.Radiobutton(mode_sub_frame, text="CRF (constant quality)",
                       variable=self.quality_mode, value='crf',
                       command=self.on_quality_mode_change).pack(side='left', padx=10)
        
        # Bitrate settings - Row 3
        row = 3
        self.bitrate_frame = ttk.Frame(settings_frame)
        self.bitrate_frame.grid(row=row, column=0, columnspan=2, sticky='ew', pady=5)
        
        ttk.Label(self.bitrate_frame, text="Bitrate:").pack(side='left')
        
        # Create numeric variable for slider
        self.bitrate_var = tk.DoubleVar(value=2.0)
        
        bitrate_slider = ttk.Scale(self.bitrate_frame, from_=0.5, to=20,
                                   orient='horizontal', variable=self.bitrate_var)
        bitrate_slider.pack(side='left', padx=5)
        bitrate_slider.configure(command=self.on_bitrate_change)
        
        # Editable bitrate entry with validation
        self.bitrate_entry = ttk.Entry(self.bitrate_frame, width=8,
                                       textvariable=self.bitrate_var,
                                       validate='key')
        self.bitrate_entry['validatecommand'] = (self.bitrate_entry.register(self.validate_bitrate), '%P')
        self.bitrate_entry.pack(side='left', padx=5)
        self.bitrate_entry.bind('<FocusOut>', self.on_bitrate_entry_focus_out)
        self.bitrate_entry.bind('<Return>', self.on_bitrate_entry_return)
        
        ttk.Label(self.bitrate_frame, text="M").pack(side='left')
        
        # Quick preset buttons - Row 4
        self.bitrate_preset_frame = ttk.Frame(settings_frame)
        self.bitrate_preset_frame.grid(row=4, column=0, columnspan=2, sticky='w', pady=2)
        
        ttk.Button(self.bitrate_preset_frame, text="1M", width=6,
                  command=lambda: self.set_bitrate(1.0)).pack(side='left', padx=2)
        ttk.Button(self.bitrate_preset_frame, text="2M", width=6,
                  command=lambda: self.set_bitrate(2.0)).pack(side='left', padx=2)
        ttk.Button(self.bitrate_preset_frame, text="3M", width=6,
                  command=lambda: self.set_bitrate(3.0)).pack(side='left', padx=2)
        ttk.Button(self.bitrate_preset_frame, text="4M", width=6,
                  command=lambda: self.set_bitrate(4.0)).pack(side='left', padx=2)
        ttk.Button(self.bitrate_preset_frame, text="8M", width=6,
                  command=lambda: self.set_bitrate(8.0)).pack(side='left', padx=2)
        ttk.Button(self.bitrate_preset_frame, text="16M", width=6,
                  command=lambda: self.set_bitrate(16.0)).pack(side='left', padx=2)
        
        # CRF settings row - Row 3 (same as bitrate, they swap)
        row = 3
        self.crf_frame = ttk.Frame(settings_frame)
        self.crf_frame.grid(row=row, column=0, columnspan=2, sticky='w', pady=5)
        
        ttk.Label(self.crf_frame, text="CRF Value:").pack(side='left')
        
        # Create numeric variable for CRF slider
        self.crf_var = tk.IntVar(value=23)
        
        crf_slider = ttk.Scale(self.crf_frame, from_=0, to=51,
                               orient='horizontal', variable=self.crf_var)
        crf_slider.pack(side='left', padx=5)
        crf_slider.configure(command=self.on_crf_change)
        
        # Editable CRF entry with validation
        self.crf_entry = ttk.Entry(self.crf_frame, width=6,
                                   textvariable=self.crf_var,
                                   validate='key')
        self.crf_entry['validatecommand'] = (self.crf_entry.register(self.validate_crf), '%P')
        self.crf_entry.pack(side='left', padx=5)
        self.crf_entry.bind('<FocusOut>', self.on_crf_entry_focus_out)
        self.crf_entry.bind('<Return>', self.on_crf_entry_return)
        
        ttk.Label(self.crf_frame, text="(0-51, lower=better)",
                 font=('Helvetica', 9)).pack(side='left', padx=5)
        
        # CRF preset buttons - Row 4 (same as bitrate preset, they swap)
        self.crf_preset_frame = ttk.Frame(settings_frame)
        self.crf_preset_frame.grid(row=4, column=0, columnspan=2, sticky='w', pady=2)
        
        ttk.Button(self.crf_preset_frame, text="18", width=6,
                  command=lambda: self.set_crf(18)).pack(side='left', padx=2)
        ttk.Button(self.crf_preset_frame, text="20", width=6,
                  command=lambda: self.set_crf(20)).pack(side='left', padx=2)
        ttk.Button(self.crf_preset_frame, text="23", width=6,
                  command=lambda: self.set_crf(23)).pack(side='left', padx=2)
        ttk.Button(self.crf_preset_frame, text="28", width=6,
                  command=lambda: self.set_crf(28)).pack(side='left', padx=2)
        ttk.Button(self.crf_preset_frame, text="30", width=6,
                  command=lambda: self.set_crf(30)).pack(side='left', padx=2)
        ttk.Button(self.crf_preset_frame, text="32", width=6,
                  command=lambda: self.set_crf(32)).pack(side='left', padx=2)
        
        # Hide CRF controls initially (bitrate mode is default)
        self.crf_frame.grid_remove()
        self.crf_preset_frame.grid_remove()
        
        # Preset dropdown - Row 5
        self.preset_label = ttk.Label(settings_frame, text="Preset:")
        self.preset_label.grid(row=5, column=0, sticky='w', pady=(10, 0))

        self.preset_combo = ttk.Combobox(settings_frame, textvariable=self.cpu_preset,
                                        width=20, state='readonly')
        self.preset_combo['values'] = ('ultrafast', 'superfast', 'veryfast',
                                       'faster', 'fast', 'medium', 'slow', 'slower', 'veryslow')
        self.preset_combo.grid(row=5, column=1, sticky='w', padx=5, pady=(10, 0))
        self.preset_combo.bind('<<ComboboxSelected>>', self.on_preset_change)
        
        # Audio settings (only shown for audio/both modes)
        row = 6
        self.audio_frame = ttk.Frame(settings_frame)
        self.audio_frame.grid(row=row, column=0, columnspan=2, sticky='w', pady=5)
        
        ttk.Label(self.audio_frame, text="Audio Codec:").pack(side='left', padx=(0, 5))
        
        # Audio codec mapping: display name -> ffmpeg codec name
        self.audio_codec_map = {
            'aac': 'aac',
            'ac3 (Dolby Digital)': 'ac3',
            'eac3 (Dolby Digital+)': 'eac3',
            'mp3': 'mp3',
            'mp2 (MPEG Layer 2)': 'mp2',
            'opus': 'opus',
            'flac': 'flac',
            'vorbis': 'vorbis',
            'alac (Apple Lossless)': 'alac',
            'dts': 'dca',
            'wavpack': 'wavpack',
            'tta (True Audio)': 'tta',
            'pcm 16-bit': 'pcm_s16le',
            'pcm 24-bit': 'pcm_s24le',
            'copy': 'copy'
        }
        
        self.audio_codec_combo = ttk.Combobox(self.audio_frame, textvariable=self.audio_codec,
                                              width=22, state='readonly')
        self.audio_codec_combo['values'] = list(self.audio_codec_map.keys())
        self.audio_codec_combo.set('aac')  # Default
        self.audio_codec_combo.pack(side='left', padx=5)
        
        ttk.Label(self.audio_frame, text="Bitrate:").pack(side='left', padx=(15, 5))
        
        self.audio_bitrate_combo = ttk.Combobox(self.audio_frame, textvariable=self.audio_bitrate,
                                                width=8, state='readonly')
        self.audio_bitrate_combo['values'] = ('32k', '48k', '64k', '96k', '128k', '160k', '192k', '256k', '320k', '384k', '448k', '512k', '640k')
        self.audio_bitrate_combo.set('128k')  # Default
        self.audio_bitrate_combo.pack(side='left', padx=5)
        
        # Audio frame always visible — disabled when in video-only mode
        self.audio_codec_combo.configure(state='disabled')
        self.audio_bitrate_combo.configure(state='disabled')

        # Checkboxes - Row 7
        self.check_frame = ttk.Frame(settings_frame)
        self.check_frame.grid(row=7, column=0, columnspan=2, sticky='w', pady=10)
        
        ttk.Checkbutton(self.check_frame, text="Skip existing files",
                       variable=self.skip_existing).pack(side='left', padx=5)
        ttk.Checkbutton(self.check_frame, text="Delete originals after conversion",
                       variable=self.delete_originals).pack(side='left', padx=5)
        self.two_pass_check = ttk.Checkbutton(self.check_frame, text="Two-pass encoding",
                       variable=self.two_pass, command=self.on_two_pass_change)
        self.two_pass_check.pack(side='left', padx=5)
        ttk.Checkbutton(self.check_frame, text="Verify output",
                       variable=self.verify_output).pack(side='left', padx=5)
        ttk.Checkbutton(self.check_frame, text="Remove existing subtitles",
                       variable=self.strip_internal_subs).pack(side='left', padx=5)

        # Metadata cleanup options - Row 8
        self.metadata_frame = ttk.Frame(settings_frame)
        self.metadata_frame.grid(row=8, column=0, columnspan=2, sticky='w', pady=(0, 6))

        ttk.Checkbutton(self.metadata_frame, text="Strip chapters",
                       variable=self.strip_chapters).pack(side='left', padx=5)
        ttk.Checkbutton(self.metadata_frame, text="Strip tags",
                       variable=self.strip_metadata_tags).pack(side='left', padx=5)

        self.meta_check = ttk.Checkbutton(self.metadata_frame, text="Set track metadata:",
                       variable=self.set_track_metadata, command=self._on_metadata_toggle)
        self.meta_check.pack(side='left', padx=5)

        self.meta_detail_frame = ttk.Frame(self.metadata_frame)
        self.meta_detail_frame.pack(side='left', padx=(0, 5))

        ttk.Label(self.meta_detail_frame, text="V:").pack(side='left')
        self.meta_video_entry = ttk.Entry(self.meta_detail_frame, textvariable=self.meta_video_lang, width=4)
        self.meta_video_entry.pack(side='left', padx=(2, 6))

        ttk.Label(self.meta_detail_frame, text="A:").pack(side='left')
        self.meta_audio_entry = ttk.Entry(self.meta_detail_frame, textvariable=self.meta_audio_lang, width=4)
        self.meta_audio_entry.pack(side='left', padx=(2, 6))

        ttk.Label(self.meta_detail_frame, text="S:").pack(side='left')
        self.meta_sub_entry = ttk.Entry(self.meta_detail_frame, textvariable=self.meta_sub_lang, width=4)
        self.meta_sub_entry.pack(side='left', padx=(2, 0))

        # Initial state
        self._on_metadata_toggle()

        # ── Row 9: Edition tagging ──
        self.edition_frame = ttk.Frame(settings_frame)
        self.edition_frame.grid(row=9, column=0, columnspan=2, sticky='w', pady=(0, 6))

        ttk.Label(self.edition_frame, text="Edition:").pack(side='left', padx=(5, 2))
        self.edition_combo = ttk.Combobox(
            self.edition_frame, textvariable=self.edition_tag,
            values=EDITION_PRESETS, width=22, state='readonly')
        self.edition_combo.pack(side='left', padx=(0, 4))
        self.edition_combo.set('')

        self.edition_custom_entry = ttk.Entry(self.edition_frame,
                                               textvariable=self._edition_custom_var(),
                                               width=22)
        # Hidden by default — shown when "Custom..." is selected
        self._edition_custom_sv = tk.StringVar(value='')

        def _on_edition_select(event=None):
            sel = self.edition_combo.get()
            if sel == 'Custom...':
                self.edition_custom_entry.pack(side='left', padx=(0, 4))
                self.edition_custom_entry.focus()
            else:
                self.edition_custom_entry.pack_forget()
                self.edition_tag.set(sel)

        self.edition_combo.bind('<<ComboboxSelected>>', _on_edition_select)

        # Trace the custom entry to update edition_tag
        def _on_custom_change(*args):
            if self.edition_combo.get() == 'Custom...':
                self.edition_tag.set(self._edition_custom_sv.get())
        self._edition_custom_sv.trace_add('write', _on_custom_change)

        ttk.Checkbutton(self.edition_frame, text="Add to filename (Plex)",
                        variable=self.edition_in_filename).pack(side='left', padx=(8, 0))


        # ── Row 10: Chapter insertion ──
        self.chapter_frame = ttk.Frame(settings_frame)
        self.chapter_frame.grid(row=10, column=0, columnspan=2, sticky='w', pady=(0, 6))

        def _on_add_chapters_toggle():
            """Mutual exclusion: add_chapters unchecks strip_chapters."""
            if self.add_chapters.get():
                self.strip_chapters.set(False)
            st = 'normal' if self.add_chapters.get() else 'disabled'
            self.chapter_interval_spin.configure(state=st)

        ttk.Checkbutton(self.chapter_frame, text="Add chapters every",
                        variable=self.add_chapters,
                        command=_on_add_chapters_toggle).pack(side='left', padx=(5, 2))
        self.chapter_interval_spin = tk.Spinbox(
            self.chapter_frame, textvariable=self.chapter_interval,
            from_=1, to=60, width=3, state='disabled')
        self.chapter_interval_spin.pack(side='left', padx=(0, 2))
        ttk.Label(self.chapter_frame, text="minutes").pack(side='left')

        # Wire strip_chapters to uncheck add_chapters
        def _on_strip_chapters_trace(*args):
            if self.strip_chapters.get() and self.add_chapters.get():
                self.add_chapters.set(False)
                _on_add_chapters_toggle()
        self.strip_chapters.trace_add('write', _on_strip_chapters_trace)


    def _edition_custom_var(self):
        """Return the StringVar for the custom edition entry."""
        if not hasattr(self, '_edition_custom_sv'):
            self._edition_custom_sv = tk.StringVar(value='')
        return self._edition_custom_sv

    def setup_file_list(self, parent):
        """Setup file list section"""
        file_frame = ttk.LabelFrame(parent, text="Video Files", padding=10)
        file_frame.grid(row=2, column=0, sticky="nsew", pady=(0, 10))
        file_frame.columnconfigure(0, weight=1)
        file_frame.rowconfigure(1, weight=1)
        
        # File list controls
        control_frame = ttk.Frame(file_frame)
        control_frame.grid(row=0, column=0, sticky='ew', pady=(0, 5))
        
        ttk.Button(control_frame, text="▶️ Start Conversion",
                  command=self.start_conversion).pack(side='left', padx=2)
        
        self.pause_btn = ttk.Button(control_frame, text="⏸️ Pause",
                                   command=self.toggle_pause, state='disabled')
        self.pause_btn.pack(side='left', padx=2)
        
        self.stop_btn = ttk.Button(control_frame, text="⏹️ Stop",
                                  command=self.stop_conversion, state='disabled')
        self.stop_btn.pack(side='left', padx=2)

        ttk.Button(control_frame, text="🗑️ Clear",
                  command=self.clear_files).pack(side='left', padx=2)

        ttk.Button(control_frame, text="✅ Clear Finished",
                  command=self.clear_finished).pack(side='left', padx=2)

        ttk.Separator(control_frame, orient='vertical').pack(side='left', fill='y', padx=6)

        ttk.Button(control_frame, text="⬆ Up",
                  command=self.move_file_up).pack(side='left', padx=2)
        ttk.Button(control_frame, text="⬇ Down",
                  command=self.move_file_down).pack(side='left', padx=2)
        
        # Progress bar
        self.progress_var = tk.DoubleVar(value=0)
        progress_frame = ttk.Frame(file_frame)
        progress_frame.grid(row=0, column=1, sticky='ew', padx=10)
        
        self.progress_bar = ttk.Progressbar(progress_frame, variable=self.progress_var,
                                           maximum=100, mode='determinate')
        self.progress_bar.pack(fill='x')
        
        self.progress_label = ttk.Label(progress_frame, text="0 / 0 files (0%)")
        self.progress_label.pack()
        
        # File list
        columns = ('name', 'size', 'duration', 'est_size', 'status')
        self.file_tree = ttk.Treeview(file_frame, columns=columns, show='headings', height=8)
        self.file_tree.grid(row=1, column=0, sticky="nsew")

        self._sort_col = None
        self._sort_reverse = False

        for col, label in [('name', 'Filename'), ('size', 'Source Size'),
                           ('duration', 'Duration'), ('est_size', 'Est. Output'),
                           ('status', 'Status')]:
            self.file_tree.heading(col, text=label,
                                   command=lambda c=col: self._sort_by_column(c))

        self.file_tree.column('name',     width=320)
        self.file_tree.column('size',     width=85)
        self.file_tree.column('duration', width=75)
        self.file_tree.column('est_size', width=85)
        self.file_tree.column('status',   width=85)
        
        # Scrollbar
        scrollbar = ttk.Scrollbar(file_frame, orient='vertical',
                                 command=self.file_tree.yview)
        scrollbar.grid(row=1, column=1, sticky='ns')
        self.file_tree.configure(yscrollcommand=scrollbar.set)

        # Right-click context menu
        self.tree_context_menu = tk.Menu(self.root, tearoff=0)
        self.tree_context_menu.add_command(label="▶ Play Source File",  command=self.play_source_file)
        self.tree_context_menu.add_command(label="▶ Play Output File",  command=self.play_output_file)
        self.tree_context_menu.add_command(label="ℹ️ Enhanced Media Details...", command=self.show_enhanced_media_info)
        self.tree_context_menu.add_separator()
        self.tree_context_menu.add_command(label="⚙️ Override Settings...", command=self.show_override_dialog)
        self.tree_context_menu.add_command(label="✖ Clear Override", command=self.clear_override)
        self.tree_context_menu.add_separator()
        self.tree_context_menu.add_command(label="🎞️ Internal Subtitles...", command=self.show_subtitle_dialog)
        self.tree_context_menu.add_command(label="📎 External Subtitles...", command=self.show_external_subtitle_dialog)
        self.tree_context_menu.add_command(label="✖ Remove External Subs", command=self.remove_external_subs)
        self.tree_context_menu.add_separator()
        self.tree_context_menu.add_command(label="🗑️ Remove from list", command=self.remove_selected_file)
        self.file_tree.bind('<Button-3>', self.on_file_tree_right_click)
        def on_file_double_click(event):
            # Identify which row was double-clicked
            item = self.file_tree.identify_row(event.y)
            if not item:
                return
            # Select and focus the item
            self.file_tree.selection_set(item)
            self.file_tree.focus(item)
            # Use after_idle so the selection is fully processed first
            self.root.after_idle(self.show_subtitle_dialog)
        self.file_tree.bind('<Double-1>', on_file_double_click)
        self.file_tree.bind('<Delete>', lambda e: self.remove_selected_file())

        file_frame.rowconfigure(1, weight=1)

        # Drag-and-drop support
        if HAS_DND:
            self.file_tree.drop_target_register(DND_FILES)
            self.file_tree.dnd_bind('<<Drop>>', self.on_drop)
            # Hint label
            ttk.Label(file_frame, text="💡 Drag & drop video files or folders here",
                      font=('Helvetica', 8), foreground='gray').grid(
                row=2, column=0, columnspan=2, sticky='w', pady=(2, 0))

    def on_file_tree_right_click(self, event):
        """Select the row under the cursor and show the context menu."""
        row = self.file_tree.identify_row(event.y)
        if row:
            self.file_tree.selection_set(row)
            self.tree_context_menu.tk_popup(event.x_root, event.y_root)

    def remove_selected_file(self):
        """Remove the selected file from the list."""
        selected = self.file_tree.selection()
        if not selected:
            return
        item = selected[0]
        # Find its index in self.files by matching the tree item's position
        all_items = self.file_tree.get_children()
        index = list(all_items).index(item)
        removed_name = self.files[index]['name']
        # Remove from data and tree
        self.files.pop(index)
        self.file_tree.delete(item)
        self.add_log(f"Removed from list: {removed_name}", 'INFO')

    def _center_on_main(self, dlg):
        """Position a dialog centered over the main window (keeps it on the same screen)."""
        self.root.update_idletasks()
        dlg.update_idletasks()
        rx = self.root.winfo_x()
        ry = self.root.winfo_y()
        rw = self.root.winfo_width()
        rh = self.root.winfo_height()
        # Use actual window size if available, fall back to requested size
        dw = dlg.winfo_width()
        dh = dlg.winfo_height()
        if dw <= 1 or dh <= 1:
            # Window not yet mapped — parse from geometry string if set
            geo = dlg.geometry()
            try:
                size_part = geo.split('+')[0]
                if 'x' in size_part:
                    dw, dh = map(int, size_part.split('x'))
            except (ValueError, IndexError):
                dw = dlg.winfo_reqwidth()
                dh = dlg.winfo_reqheight()
        x = rx + (rw - dw) // 2
        y = ry + (rh - dh) // 2
        # Ensure it stays on screen
        x = max(0, x)
        y = max(0, y)
        dlg.geometry(f"{dw}x{dh}+{x}+{y}")

    def _get_selected_file_index(self):
        """Return (item_id, index) for the currently selected tree row, or (None, None)."""
        selected = self.file_tree.selection()
        if not selected:
            return None, None
        item = selected[0]
        all_items = list(self.file_tree.get_children())
        index = all_items.index(item)
        return item, index

    def _refresh_tree_row(self, item, file_info):
        """Redraw a single tree row to reflect override/subtitle indicators and est size."""
        name = file_info['name']
        prefix = ''
        if 'overrides' in file_info:
            prefix += '⚙️ '
        if file_info.get('external_subs'):
            prefix += '📎 '
        if file_info.get('has_closed_captions'):
            prefix += 'CC '
        display_name = prefix + name
        self.file_tree.item(item, values=(
            display_name,
            file_info['size'],
            file_info.get('duration_str', '?'),
            file_info.get('est_size', '?'),
            file_info['status']
        ))

    def clear_override(self):
        """Remove per-file overrides from the selected file."""
        item, index = self._get_selected_file_index()
        if index is None:
            return
        file_info = self.files[index]
        if 'overrides' in file_info:
            del file_info['overrides']
            self._refresh_tree_row(item, file_info)
            self.add_log(f"Cleared overrides: {file_info['name']}", 'INFO')
        else:
            self.add_log(f"No overrides to clear for: {file_info['name']}", 'INFO')

    def remove_external_subs(self):
        """Remove all external subtitles from the selected file."""
        item, index = self._get_selected_file_index()
        if index is None:
            return
        file_info = self.files[index]
        if file_info.get('external_subs'):
            file_info['external_subs'] = []
            self._refresh_tree_row(item, file_info)
            self.add_log(f"Removed external subs: {file_info['name']}", 'INFO')
        else:
            self.add_log(f"No external subs to remove for: {file_info['name']}", 'INFO')

    def show_external_subtitle_dialog(self):
        """Show a dialog to manage external subtitle files attached to the selected video."""
        item, index = self._get_selected_file_index()
        if index is None:
            return
        file_info = self.files[index]

        dlg = tk.Toplevel(self.root)
        dlg.title(f"External Subtitles — {os.path.basename(file_info['name'])}")
        dlg.geometry("820x500")
        dlg.transient(self.root)
        self._center_on_main(dlg)
        dlg.resizable(True, True)
        dlg.minsize(700, 420)

        # Working copy of external subs
        subs = [dict(s) for s in file_info.get('external_subs', [])]

        # ── Scrollable list area ──
        list_frame = ttk.LabelFrame(dlg, text="Attached Subtitle Files", padding=8)
        list_frame.pack(fill='both', expand=True, padx=10, pady=(10, 4))

        canvas = tk.Canvas(list_frame, highlightthickness=0)
        scrollbar = ttk.Scrollbar(list_frame, orient='vertical', command=canvas.yview)
        inner = ttk.Frame(canvas)

        inner.bind('<Configure>', lambda e: canvas.configure(scrollregion=canvas.bbox('all')))
        inner_window = canvas.create_window((0, 0), window=inner, anchor='nw')
        canvas.configure(yscrollcommand=scrollbar.set)

        # Keep inner frame width in sync with canvas so grid stretches properly
        def _on_canvas_resize(event):
            canvas.itemconfigure(inner_window, width=event.width)
        canvas.bind('<Configure>', _on_canvas_resize)

        canvas.pack(side='left', fill='both', expand=True)
        scrollbar.pack(side='right', fill='y')

        lang_values = [f"{code} — {name}" for code, name in SUBTITLE_LANGUAGES]
        lang_codes = [code for code, _ in SUBTITLE_LANGUAGES]

        # Grid column configuration
        COL_W = {'lang': 16, 'mode': 8}

        def _rebuild_list():
            """Rebuild the subtitle list UI from the working copy."""
            for w in inner.winfo_children():
                w.destroy()

            # Filename column stretches; others are fixed width on the right
            inner.columnconfigure(0, weight=1)
            for c in range(1, 8):
                inner.columnconfigure(c, weight=0)

            if not subs:
                ttk.Label(inner, text="No external subtitles attached.\n\n"
                          "Use  ➕ Add Subtitle File  below,\n"
                          "or drag .srt / .ass / .vtt files onto the file queue.",
                          foreground='gray', justify='center').grid(row=0, column=0,
                          columnspan=8, padx=40, pady=30)
                return

            # ── Column headers ──
            hdr_font = ('Helvetica', 9, 'bold')
            pad = {'padx': 4, 'pady': (0, 6)}
            ttk.Label(inner, text="Filename",  font=hdr_font, anchor='w').grid(row=0, column=0, sticky='w', **pad)
            ttk.Label(inner, text="Language",   font=hdr_font, anchor='w').grid(row=0, column=1, sticky='e', **pad)
            ttk.Label(inner, text="Mode",       font=hdr_font, anchor='w').grid(row=0, column=2, sticky='e', **pad)
            ttk.Label(inner, text="Default",    font=hdr_font, anchor='center').grid(row=0, column=3, sticky='e', **pad)
            ttk.Label(inner, text="SDH",        font=hdr_font, anchor='center').grid(row=0, column=4, sticky='e', **pad)
            ttk.Label(inner, text="Forced",     font=hdr_font, anchor='center').grid(row=0, column=5, sticky='e', **pad)
            ttk.Label(inner, text="",           font=hdr_font).grid(row=0, column=6, **pad)
            ttk.Label(inner, text="",           font=hdr_font).grid(row=0, column=7, **pad)

            # Separator under headers
            ttk.Separator(inner, orient='horizontal').grid(
                row=1, column=0, columnspan=8, sticky='ew', pady=(0, 4))

            # ── Subtitle rows ──
            for i, sub in enumerate(subs):
                r = i + 2  # row offset for headers + separator
                rpad = {'padx': 4, 'pady': 3}

                # Filename — full width, stretches with dialog
                name_lbl = ttk.Label(inner, text=sub['label'], anchor='w')
                name_lbl.grid(row=r, column=0, sticky='ew', **rpad)
                _create_tooltip(name_lbl, sub['label'])

                # Language
                lang_var = tk.StringVar()
                try:
                    li = lang_codes.index(sub['language'])
                    lang_var.set(lang_values[li])
                except ValueError:
                    lang_var.set(lang_values[0])
                lang_cb = ttk.Combobox(inner, textvariable=lang_var,
                                       values=lang_values, width=COL_W['lang'], state='readonly')
                lang_cb.grid(row=r, column=1, sticky='e', **rpad)
                def _on_lang(evt, idx=i, var=lang_var):
                    subs[idx]['language'] = var.get().split(' — ')[0] if ' — ' in var.get() else 'und'
                lang_cb.bind('<<ComboboxSelected>>', _on_lang)

                # Mode
                mode_var = tk.StringVar(value=sub['mode'])
                bitmap_fmts = {'hdmv_pgs_subtitle', 'dvd_subtitle'}
                mode_values = ['embed'] if sub['format'] in bitmap_fmts else ['embed', 'burn_in']
                mode_cb = ttk.Combobox(inner, textvariable=mode_var,
                                       values=mode_values, width=COL_W['mode'], state='readonly')
                mode_cb.grid(row=r, column=2, sticky='e', **rpad)
                def _on_mode(evt, idx=i, var=mode_var):
                    subs[idx]['mode'] = var.get()
                    _update_warning()
                mode_cb.bind('<<ComboboxSelected>>', _on_mode)

                # Default checkbox
                def_var = tk.BooleanVar(value=sub.get('default', False))
                ttk.Checkbutton(inner, variable=def_var).grid(row=r, column=3, sticky='e', **rpad)
                def _on_default(idx=i, var=def_var):
                    subs[idx]['default'] = var.get()
                def_var.trace_add('write', lambda *a, idx=i, var=def_var: _on_default(idx, var))

                # SDH checkbox
                sdh_var = tk.BooleanVar(value=sub.get('sdh', False))
                ttk.Checkbutton(inner, variable=sdh_var).grid(row=r, column=4, sticky='e', **rpad)
                def _on_sdh(idx=i, var=sdh_var):
                    subs[idx]['sdh'] = var.get()
                sdh_var.trace_add('write', lambda *a, idx=i, var=sdh_var: _on_sdh(idx, var))

                # Forced checkbox
                forced_var = tk.BooleanVar(value=sub.get('forced', False))
                ttk.Checkbutton(inner, variable=forced_var).grid(row=r, column=5, sticky='e', **rpad)
                def _on_forced(idx=i, var=forced_var):
                    subs[idx]['forced'] = var.get()
                forced_var.trace_add('write', lambda *a, idx=i, var=forced_var: _on_forced(idx, var))

                # Edit button (text-based subs only)
                _BITMAP_FMTS = {'hdmv_pgs_subtitle', 'dvd_subtitle', 'sup', 'sub', 'idx'}
                sub_ext = Path(sub['path']).suffix.lower().lstrip('.')
                if sub_ext not in _BITMAP_FMTS and sub.get('format') not in _BITMAP_FMTS:
                    ttk.Button(inner, text="✏️", width=3,
                               command=lambda s=sub: (
                                   dlg.grab_release(),
                                   self.show_subtitle_editor(file_info['path'], None, file_info,
                                                             external_sub_path=s['path']),
                                   dlg.grab_set() if dlg.winfo_exists() else None
                               )).grid(row=r, column=6, sticky='e', **rpad)

                # Remove button
                ttk.Button(inner, text="✖", width=3,
                           command=lambda idx=i: _remove_sub(idx)).grid(row=r, column=7, sticky='e', **rpad)

            _update_warning()

        # ── Strip existing tracks option ──
        strip_existing = tk.BooleanVar(value=file_info.get('strip_internal_subs', False))
        strip_frame = ttk.Frame(dlg)
        strip_frame.pack(fill='x', padx=14, pady=(4, 0))
        ttk.Checkbutton(strip_frame, text="Remove existing subtitle tracks from source",
                        variable=strip_existing).pack(side='left')
        ttk.Label(strip_frame, text="(only external subs will be included in output)",
                  foreground='gray', font=('Helvetica', 8)).pack(side='left', padx=(6, 0))

        # ── Burn-in warning ──
        warn_var = tk.StringVar()
        warn_label = ttk.Label(dlg, textvariable=warn_var, foreground='#cc6600',
                               wraplength=780, justify='left')
        warn_label.pack(fill='x', padx=14, pady=(0, 2))

        def _update_warning():
            has_burn = any(s['mode'] == 'burn_in' for s in subs)
            burn_count = sum(1 for s in subs if s['mode'] == 'burn_in')
            msgs = []
            if has_burn:
                msgs.append("⚠ Burn-in subtitles require CPU filtering — "
                            "hardware decode will be automatically disabled for this file.")
            if burn_count > 1:
                msgs.append("⚠ Only the first burn-in subtitle will be rendered. "
                            "Set the others to 'embed' or remove them.")
            warn_var.set('\n'.join(msgs))

        def _remove_sub(idx):
            subs.pop(idx)
            _rebuild_list()

        def _add_sub():
            filetypes = [("Subtitle files", " ".join(f"*{ext}" for ext in SUBTITLE_EXTENSIONS)),
                         ("All files", "*.*")]
            paths = filedialog.askopenfilenames(
                title="Add External Subtitle File(s)",
                filetypes=filetypes,
                initialdir=str(Path(file_info['path']).parent))
            for p in paths:
                p = Path(p)
                ext = p.suffix.lower()
                if ext not in SUBTITLE_EXTENSIONS:
                    continue
                if any(s['path'] == str(p) for s in subs):
                    continue
                # Auto-detect language from filename tokens
                _lang2to3 = {
                    'en': 'eng', 'es': 'spa', 'fr': 'fra', 'de': 'deu',
                    'it': 'ita', 'pt': 'por', 'ru': 'rus', 'ja': 'jpn',
                    'ko': 'kor', 'zh': 'zho', 'ar': 'ara', 'hi': 'hin',
                    'nl': 'nld', 'pl': 'pol', 'sv': 'swe', 'tr': 'tur',
                    'vi': 'vie',
                }
                lang = 'und'
                stem_tokens = p.stem.lower().split('.')
                for token in reversed(stem_tokens[1:]):
                    if token in _lang2to3:
                        lang = _lang2to3[token]
                        break
                    elif any(token == lc for lc, _ in SUBTITLE_LANGUAGES):
                        lang = token
                        break
                # Auto-detect forced / SDH / default from filename tokens
                is_forced = 'forced' in stem_tokens
                is_sdh = 'sdh' in stem_tokens or 'cc' in stem_tokens
                is_plain = not is_forced and not is_sdh
                existing_has_default = any(s.get('default') for s in subs)
                sub_info = {
                    'path': str(p),
                    'label': p.name,
                    'language': lang,
                    'mode': 'embed',
                    'format': SUBTITLE_EXT_TO_CODEC.get(ext, 'srt'),
                    'default': is_plain and not existing_has_default,
                    'sdh': is_sdh,
                    'forced': is_forced,
                }
                subs.append(sub_info)
            _rebuild_list()

        # ── Buttons ──
        btn_frame = ttk.Frame(dlg, padding=(10, 4, 10, 10))
        btn_frame.pack(fill='x')

        ttk.Button(btn_frame, text="➕ Add Subtitle File...", command=_add_sub).pack(side='left')

        def _on_save():
            file_info['external_subs'] = subs
            file_info['strip_internal_subs'] = strip_existing.get()
            self._refresh_tree_row(item, file_info)
            count = len(subs)
            strip_msg = " (replacing internal tracks)" if strip_existing.get() else ""
            self.add_log(f"External subs updated: {file_info['name']} ({count} file(s)){strip_msg}", 'INFO')
            dlg.destroy()

        ttk.Button(btn_frame, text="Cancel", command=dlg.destroy).pack(side='right', padx=(4, 0))
        ttk.Button(btn_frame, text="Save", command=_on_save).pack(side='right')

        _rebuild_list()
        dlg.update_idletasks()
        dlg.grab_set()
        dlg.wait_window()

    def show_override_dialog(self):
        """Show a per-file settings override dialog."""
        item, index = self._get_selected_file_index()
        if index is None:
            return
        file_info = self.files[index]
        existing = file_info.get('overrides', {})

        dlg = tk.Toplevel(self.root)
        dlg.title(f"Override Settings — {os.path.basename(file_info['name'])}")
        dlg.geometry("520x640")
        dlg.transient(self.root)
        dlg.grab_set()
        self._center_on_main(dlg)
        dlg.resizable(False, False)

        pad = {'padx': 10, 'pady': 4}

        # ── Helper to pre-fill from override or fall back to global ──
        def ov(key, global_val):
            return existing.get(key, global_val)

        # ── Variables ──
        v_encoder      = tk.StringVar(value=ov('encoder',      self.encoder_mode.get()))
        v_video_codec  = tk.StringVar(value=ov('video_codec',  self.video_codec.get()))
        v_quality_mode = tk.StringVar(value=ov('quality_mode', self.quality_mode.get()))
        v_bitrate      = tk.StringVar(value=ov('bitrate',      self.bitrate.get()))
        v_crf          = tk.StringVar(value=ov('crf',          self.crf.get()))
        v_preset       = tk.StringVar(value=ov('preset',       self.preset_combo.get()))
        v_transcode    = tk.StringVar(value=ov('transcode_mode', self.transcode_mode.get()))
        v_audio_codec  = tk.StringVar(value=ov('audio_codec',  self.audio_codec.get()))
        v_audio_br     = tk.StringVar(value=ov('audio_bitrate', self.audio_bitrate.get()))
        v_skip         = tk.BooleanVar(value=ov('skip_existing',    self.skip_existing.get()))
        v_delete       = tk.BooleanVar(value=ov('delete_originals', self.delete_originals.get()))
        v_hw_decode    = tk.BooleanVar(value=ov('hw_decode',   self.hw_decode.get()))

        f = ttk.Frame(dlg, padding=10)
        f.pack(fill='both', expand=True)
        f.columnconfigure(1, weight=1)
        row = 0

        def lbl(text, r):
            ttk.Label(f, text=text).grid(row=r, column=0, sticky='w', **pad)

        # ── Video Codec ──
        lbl("Video Codec:", row)
        codec_combo = ttk.Combobox(f, textvariable=v_video_codec,
                                   values=list(VIDEO_CODEC_MAP.keys()), width=24, state='readonly')
        codec_combo.grid(row=row, column=1, sticky='w', **pad); row += 1

        # ── Encoder ──
        lbl("Encoder:", row)
        enc_frame = ttk.Frame(f)
        enc_frame.grid(row=row, column=1, sticky='w', **pad); row += 1

        # Build encoder choices for the override dialog
        ovr_encoder_labels = {}   # display -> id
        ovr_encoder_ids = {}      # id -> display
        ovr_encoder_labels['CPU'] = 'cpu'
        ovr_encoder_ids['cpu'] = 'CPU'
        for bid in self.gpu_backends:
            bk = GPU_BACKENDS[bid]
            lbl_txt = bk['label']
            ovr_encoder_labels[lbl_txt] = bid
            ovr_encoder_ids[bid] = lbl_txt

        ovr_enc_combo = ttk.Combobox(enc_frame, state='readonly',
                                      values=list(ovr_encoder_labels.keys()),
                                      width=max(len(l) for l in ovr_encoder_labels) + 2 if ovr_encoder_labels else 12)
        ovr_enc_combo.set(ovr_encoder_ids.get(v_encoder.get(), 'CPU'))
        ovr_enc_combo.pack(side='left')
        def _on_ovr_enc(evt=None):
            bid = ovr_encoder_labels.get(ovr_enc_combo.get(), 'cpu')
            v_encoder.set(bid)
            _update_presets()
        ovr_enc_combo.bind('<<ComboboxSelected>>', _on_ovr_enc)

        hw_cb = ttk.Checkbutton(enc_frame, text="HW Decode", variable=v_hw_decode,
                                state='normal' if self.has_gpu else 'disabled')
        hw_cb.pack(side='left', padx=8)

        # ── Transcode Mode ──
        lbl("Transcode Mode:", row)
        tm_frame = ttk.Frame(f)
        tm_frame.grid(row=row, column=1, sticky='w', **pad); row += 1
        for txt, val in [("Video Only", "video"), ("Audio Only", "audio"), ("Both", "both")]:
            ttk.Radiobutton(tm_frame, text=txt, variable=v_transcode, value=val,
                            command=lambda: _update_audio_state()).pack(side='left', padx=(0, 6))

        # ── Quality Mode ──
        lbl("Quality Mode:", row)
        qm_frame = ttk.Frame(f)
        qm_frame.grid(row=row, column=1, sticky='w', **pad); row += 1
        ttk.Radiobutton(qm_frame, text="Bitrate", variable=v_quality_mode, value='bitrate',
                        command=lambda: _update_quality()).pack(side='left')
        ttk.Radiobutton(qm_frame, text="CRF", variable=v_quality_mode, value='crf',
                        command=lambda: _update_quality()).pack(side='left', padx=8)

        # ── Bitrate ──
        lbl("Bitrate:", row)
        br_frame = ttk.Frame(f)
        br_frame.grid(row=row, column=1, sticky='w', **pad)
        br_entry = ttk.Entry(br_frame, textvariable=v_bitrate, width=8)
        br_entry.pack(side='left')
        ttk.Label(br_frame, text="M").pack(side='left', padx=(2,8))
        for bv in ('1', '2', '3', '4', '8', '16'):
            ttk.Button(br_frame, text=f"{bv}M", width=4,
                       command=lambda b=bv: v_bitrate.set(f"{b}M")).pack(side='left', padx=1)
        br_row = row; row += 1

        # ── CRF ──
        lbl("CRF:", row)
        crf_frame = ttk.Frame(f)
        crf_frame.grid(row=row, column=1, sticky='w', **pad)
        crf_entry = ttk.Entry(crf_frame, textvariable=v_crf, width=6)
        crf_entry.pack(side='left')
        for cv in ('18', '23', '28', '35'):
            ttk.Button(crf_frame, text=cv, width=4,
                       command=lambda c=cv: v_crf.set(c)).pack(side='left', padx=1)
        crf_row = row; row += 1

        # ── Preset ──
        lbl("Preset:", row)
        preset_combo = ttk.Combobox(f, textvariable=v_preset, width=20, state='readonly')
        preset_combo.grid(row=row, column=1, sticky='w', **pad); row += 1

        # ── Audio Codec ──
        lbl("Audio Codec:", row)
        audio_frame = ttk.Frame(f)
        audio_frame.grid(row=row, column=1, sticky='w', **pad)
        audio_codec_combo = ttk.Combobox(audio_frame, textvariable=v_audio_codec,
                                         values=list(self.audio_codec_map.keys()),
                                         width=22, state='readonly')
        audio_codec_combo.pack(side='left')
        audio_br_combo = ttk.Combobox(audio_frame, textvariable=v_audio_br,
                                      values=('32k','48k','64k','96k','128k','160k',
                                              '192k','256k','320k','384k','448k','512k','640k'),
                                      width=7, state='readonly')
        audio_br_combo.pack(side='left', padx=6)
        audio_row = row; row += 1

        # ── Checkboxes ──
        check_frame = ttk.Frame(f)
        check_frame.grid(row=row, column=0, columnspan=2, sticky='w', **pad); row += 1
        ttk.Checkbutton(check_frame, text="Skip existing",    variable=v_skip).pack(side='left', padx=4)
        ttk.Checkbutton(check_frame, text="Delete originals", variable=v_delete).pack(side='left', padx=4)

        # ── Metadata cleanup ──
        v_strip_chapters    = tk.BooleanVar(value=ov('strip_chapters',      self.strip_chapters.get()))
        v_strip_meta_tags   = tk.BooleanVar(value=ov('strip_metadata_tags', self.strip_metadata_tags.get()))
        v_set_track_meta    = tk.BooleanVar(value=ov('set_track_metadata',  self.set_track_metadata.get()))
        v_meta_video_lang   = tk.StringVar(value=ov('meta_video_lang',      self.meta_video_lang.get()))
        v_meta_audio_lang   = tk.StringVar(value=ov('meta_audio_lang',      self.meta_audio_lang.get()))
        v_meta_sub_lang     = tk.StringVar(value=ov('meta_sub_lang',        self.meta_sub_lang.get()))

        meta_frame = ttk.Frame(f)
        meta_frame.grid(row=row, column=0, columnspan=2, sticky='w', **pad); row += 1
        ttk.Checkbutton(meta_frame, text="Strip chapters",  variable=v_strip_chapters).pack(side='left', padx=4)
        ttk.Checkbutton(meta_frame, text="Strip tags",      variable=v_strip_meta_tags).pack(side='left', padx=4)

        meta_track_frame = ttk.Frame(f)
        meta_track_frame.grid(row=row, column=0, columnspan=2, sticky='w', **pad); row += 1

        def _toggle_ovr_meta():
            st = 'normal' if v_set_track_meta.get() else 'disabled'
            ovr_mv.configure(state=st)
            ovr_ma.configure(state=st)
            ovr_ms.configure(state=st)

        ttk.Checkbutton(meta_track_frame, text="Set track metadata:",
                       variable=v_set_track_meta, command=_toggle_ovr_meta).pack(side='left', padx=4)
        ttk.Label(meta_track_frame, text="V:").pack(side='left')
        ovr_mv = ttk.Entry(meta_track_frame, textvariable=v_meta_video_lang, width=4)
        ovr_mv.pack(side='left', padx=(2, 6))
        ttk.Label(meta_track_frame, text="A:").pack(side='left')
        ovr_ma = ttk.Entry(meta_track_frame, textvariable=v_meta_audio_lang, width=4)
        ovr_ma.pack(side='left', padx=(2, 6))
        ttk.Label(meta_track_frame, text="S:").pack(side='left')
        ovr_ms = ttk.Entry(meta_track_frame, textvariable=v_meta_sub_lang, width=4)
        ovr_ms.pack(side='left', padx=(2, 0))
        _toggle_ovr_meta()

        # ── Edition tagging ──
        v_edition = tk.StringVar(value=ov('edition_tag', self.edition_tag.get()))
        v_edition_fn = tk.BooleanVar(value=ov('edition_in_filename', self.edition_in_filename.get()))

        edition_frame = ttk.Frame(f)
        edition_frame.grid(row=row, column=0, columnspan=2, sticky='w', **pad); row += 1

        ttk.Label(edition_frame, text="Edition:").pack(side='left', padx=(4, 2))
        ovr_edition_combo = ttk.Combobox(edition_frame, textvariable=v_edition,
                                          values=EDITION_PRESETS, width=22, state='readonly')
        ovr_edition_combo.pack(side='left', padx=(0, 4))

        _ovr_edition_custom_sv = tk.StringVar(value=v_edition.get() if v_edition.get() not in EDITION_PRESETS else '')
        ovr_edition_custom = ttk.Entry(edition_frame, textvariable=_ovr_edition_custom_sv, width=22)

        # If loaded value is a custom edition (not in presets), show custom entry
        if v_edition.get() and v_edition.get() not in EDITION_PRESETS:
            _ovr_edition_custom_sv.set(v_edition.get())
            ovr_edition_combo.set('Custom...')
            ovr_edition_custom.pack(side='left', padx=(0, 4))

        def _on_ovr_edition_select(event=None):
            sel = ovr_edition_combo.get()
            if sel == 'Custom...':
                ovr_edition_custom.pack(side='left', padx=(0, 4))
                ovr_edition_custom.focus()
            else:
                ovr_edition_custom.pack_forget()
                v_edition.set(sel)
        ovr_edition_combo.bind('<<ComboboxSelected>>', _on_ovr_edition_select)

        def _on_ovr_custom_change(*args):
            if ovr_edition_combo.get() == 'Custom...':
                v_edition.set(_ovr_edition_custom_sv.get())
        _ovr_edition_custom_sv.trace_add('write', _on_ovr_custom_change)

        ttk.Checkbutton(edition_frame, text="Add to filename (Plex)",
                        variable=v_edition_fn).pack(side='left', padx=(8, 0))

        # ── Dynamic update helpers ──
        def _update_presets():
            info = VIDEO_CODEC_MAP.get(v_video_codec.get(), VIDEO_CODEC_MAP['H.265 / HEVC'])
            enc = v_encoder.get()
            codec_nm = v_video_codec.get()
            if enc != 'cpu':
                gpu_enc = get_gpu_encoder(codec_nm, enc)
                if gpu_enc and gpu_enc != 'copy':
                    presets, default = get_gpu_presets(enc)
                else:
                    presets = info['cpu_presets']
                    default = info['cpu_preset_default']
            else:
                presets = info['cpu_presets']
                default = info['cpu_preset_default']
            preset_combo['values'] = presets
            if v_preset.get() not in presets:
                v_preset.set(default or (presets[0] if presets else ''))
            # HW decode only useful with GPU
            hw_cb.configure(state='normal' if (enc != 'cpu' and enc in self.gpu_backends) else 'disabled')
            # Update encoder combo values based on codec support
            labels = ['CPU']
            for bid in self.gpu_backends:
                if self._encoder_has_codec(codec_nm, bid):
                    labels.append(ovr_encoder_ids[bid])
            ovr_enc_combo['values'] = labels

        def _update_quality():
            if v_quality_mode.get() == 'crf':
                f.grid_slaves(row=br_row, column=0)[0].grid_remove() if f.grid_slaves(row=br_row, column=0) else None
                f.grid_slaves(row=br_row, column=1)[0].grid_remove() if f.grid_slaves(row=br_row, column=1) else None
                f.grid_slaves(row=crf_row, column=0)[0].grid() if f.grid_slaves(row=crf_row, column=0) else None
                f.grid_slaves(row=crf_row, column=1)[0].grid() if f.grid_slaves(row=crf_row, column=1) else None
            else:
                f.grid_slaves(row=crf_row, column=0)[0].grid_remove() if f.grid_slaves(row=crf_row, column=0) else None
                f.grid_slaves(row=crf_row, column=1)[0].grid_remove() if f.grid_slaves(row=crf_row, column=1) else None
                f.grid_slaves(row=br_row, column=0)[0].grid() if f.grid_slaves(row=br_row, column=0) else None
                f.grid_slaves(row=br_row, column=1)[0].grid() if f.grid_slaves(row=br_row, column=1) else None

        def _update_audio_state():
            state = 'normal' if v_transcode.get() in ('audio', 'both') else 'disabled'
            audio_codec_combo.configure(state=state if state == 'disabled' else 'readonly')
            audio_br_combo.configure(state=state if state == 'disabled' else 'readonly')

        codec_combo.bind('<<ComboboxSelected>>', lambda e: _update_presets())

        # Initial state
        _update_presets()
        _update_quality()
        _update_audio_state()

        # ── Buttons ──
        btn_frame = ttk.Frame(dlg, padding=(10, 0, 10, 10))
        btn_frame.pack(fill='x')

        def on_save():
            overrides = {
                'encoder':           v_encoder.get(),
                'video_codec':       v_video_codec.get(),
                'codec_info':        VIDEO_CODEC_MAP.get(v_video_codec.get(), VIDEO_CODEC_MAP['H.265 / HEVC']),
                'quality_mode':      v_quality_mode.get(),
                'bitrate':           v_bitrate.get() if not v_bitrate.get().endswith('M') else v_bitrate.get(),
                'crf':               v_crf.get(),
                'preset':            v_preset.get(),
                'transcode_mode':    v_transcode.get(),
                'audio_codec':       self.audio_codec_map.get(v_audio_codec.get(), v_audio_codec.get()),
                'audio_bitrate':     v_audio_br.get(),
                'skip_existing':     v_skip.get(),
                'delete_originals':  v_delete.get(),
                'hw_decode':         v_hw_decode.get(),
                'strip_chapters':      v_strip_chapters.get(),
                'strip_metadata_tags': v_strip_meta_tags.get(),
                'set_track_metadata':  v_set_track_meta.get(),
                'meta_video_lang':     v_meta_video_lang.get(),
                'meta_audio_lang':     v_meta_audio_lang.get(),
                'meta_sub_lang':       v_meta_sub_lang.get(),
                'edition_tag':         v_edition.get(),
                'edition_in_filename': v_edition_fn.get(),
            }
            file_info['overrides'] = overrides
            self._refresh_tree_row(item, file_info)
            self.add_log(f"Override saved: {file_info['name']}", 'INFO')
            dlg.destroy()

        def on_cancel():
            dlg.destroy()

        ttk.Button(btn_frame, text="Save Override", command=on_save).pack(side='right', padx=(4, 0))
        ttk.Button(btn_frame, text="Cancel", command=on_cancel).pack(side='right')

        dlg.wait_window()

    def show_subtitle_dialog(self):
        """Show subtitle track selector and extractor dialog."""
        item, index = self._get_selected_file_index()
        if index is None:
            return
        file_info = self.files[index]
        filepath = file_info['path']

        # Probe subtitle streams
        streams = get_subtitle_streams(filepath)

        dlg = tk.Toplevel(self.root)
        dlg.title(f"Internal Subtitles — {os.path.basename(filepath)}")
        dlg.geometry("700x420")
        dlg.transient(self.root)
        self._center_on_main(dlg)
        dlg.resizable(True, True)

        # ── Header info ──
        if not streams:
            has_cc = file_info.get('has_closed_captions', False)
            if has_cc:
                import shutil as _shutil
                has_ccextractor = bool(_shutil.which('ccextractor'))

                cc_frame = ttk.Frame(dlg, padding=20)
                cc_frame.pack(fill='x')
                ttk.Label(cc_frame,
                          text="No subtitle streams found, but ATSC A53 closed captions detected.",
                          font=('Helvetica', 11)).pack(anchor='w')

                # CC passthrough is always enabled (embedded in video bitstream)
                ttk.Label(cc_frame,
                          text="✔ CC data will be preserved in the video stream (A53 passthrough).",
                          font=('Helvetica', 10)).pack(anchor='w', pady=(4, 0))

                if has_ccextractor:
                    ttk.Label(cc_frame,
                              text="✔ ccextractor found — CC can also be extracted as a separate SRT subtitle track.",
                              font=('Helvetica', 10)).pack(anchor='w', pady=(2, 8))
                    cc_var = tk.BooleanVar(value=file_info.get('extract_cc', True))
                    def on_cc_toggle():
                        file_info['extract_cc'] = cc_var.get()
                    tk.Checkbutton(cc_frame, text="Extract CC to SRT subtitle track during conversion",
                                   variable=cc_var, command=on_cc_toggle).pack(anchor='w')
                else:
                    ttk.Label(cc_frame,
                              text="ℹ Install ccextractor to also extract CC as a separate SRT subtitle track.",
                              font=('Helvetica', 10)).pack(anchor='w', pady=(2, 8))
            else:
                ttk.Label(dlg, text="No subtitle tracks found in this file.",
                          font=('Helvetica', 11), padding=20).pack()
            ttk.Button(dlg, text="Close", command=dlg.destroy).pack(pady=10)
            dlg.grab_set()
            dlg.wait_window()
            return

        # ── Top bar: track count + check-all toggle ──
        top_bar = ttk.Frame(dlg, padding=(10, 6, 10, 2))
        top_bar.pack(fill='x')

        ttk.Label(top_bar, text=f"{len(streams)} subtitle track(s) found:",
                  font=('Helvetica', 10, 'bold')).pack(side='left')

        # ── Subtitle output format options ──
        SUB_FORMATS = ['copy', 'srt', 'ass', 'webvtt', 'ttml', 'extract only', 'drop']

        check_all_var = tk.BooleanVar(value=True)
        def on_check_all():
            for v in track_vars:
                v[0].set(check_all_var.get())
        tk.Checkbutton(top_bar, text="Check All", variable=check_all_var,
                       command=on_check_all, relief='flat', bd=0).pack(side='right')

        # ── Set All To: dropdown ──
        set_all_var = tk.StringVar(value='copy')
        def on_set_all():
            fmt = set_all_var.get()
            for _keep_var, fmt_var in track_vars:
                fmt_var.set(fmt)
        ttk.Button(top_bar, text="Apply", command=on_set_all, width=6).pack(side='right', padx=(4, 8))
        ttk.Combobox(top_bar, textvariable=set_all_var,
                     values=SUB_FORMATS, width=12, state='readonly').pack(side='right', padx=(2, 0))
        ttk.Label(top_bar, text="Set All To:").pack(side='right', padx=(8, 2))

        # ── Column headers (outside scroll area so they stay fixed) ──
        COL_WIDTHS = [40, 60, 70, 100, 0, 120, 50]  # 0 = expand
        header_frame = ttk.Frame(dlg, padding=(10, 0, 10, 0))
        header_frame.pack(fill='x')
        header_frame.columnconfigure(4, weight=1)
        for col, (text, w) in enumerate(zip(
            ['Keep', 'Stream', 'Language', 'Codec', 'Title / Flags', 'Convert To', ''],
            COL_WIDTHS
        )):
            ttk.Label(header_frame, text=text,
                      font=('Helvetica', 9, 'bold'),
                      width=w if w else None).grid(row=0, column=col, sticky='w', padx=4)
        ttk.Separator(dlg, orient='horizontal').pack(fill='x', padx=10, pady=2)

        # ── Scrollable track list ──
        scroll_container = ttk.Frame(dlg, padding=(10, 0, 10, 0))
        scroll_container.pack(fill='both', expand=True)

        canvas = tk.Canvas(scroll_container, highlightthickness=0)
        vsb = ttk.Scrollbar(scroll_container, orient='vertical', command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side='right', fill='y')
        canvas.pack(side='left', fill='both', expand=True)

        list_frame = ttk.Frame(canvas)
        list_frame.columnconfigure(4, weight=1)
        canvas_window = canvas.create_window((0, 0), window=list_frame, anchor='nw')

        def on_frame_configure(e):
            canvas.configure(scrollregion=canvas.bbox('all'))
        def on_canvas_configure(e):
            canvas.itemconfig(canvas_window, width=e.width)
        list_frame.bind('<Configure>', on_frame_configure)
        canvas.bind('<Configure>', on_canvas_configure)

        # Mouse wheel scrolling (bind to dialog widgets, not globally)
        def on_mousewheel(e):
            canvas.yview_scroll(int(-1 * (e.delta / 120)), 'units')
            return 'break'

        def _bind_scroll(widget):
            widget.bind('<MouseWheel>', on_mousewheel)
            widget.bind('<Button-4>', lambda e: (canvas.yview_scroll(-1, 'units'), 'break')[-1])
            widget.bind('<Button-5>', lambda e: (canvas.yview_scroll(1, 'units'), 'break')[-1])

        _bind_scroll(canvas)
        _bind_scroll(list_frame)

        # Load existing subtitle settings if any
        existing_sub = file_info.get('subtitle_settings', {})

        track_vars = []  # list of (keep_var, format_var) per stream
        for i, s in enumerate(streams):
            r = i

            # Pre-fill from saved settings
            saved = existing_sub.get(s['index'], {})
            keep_default = saved.get('keep', True)
            fmt_default  = saved.get('format', 'copy')

            keep_var = tk.BooleanVar(value=keep_default)
            fmt_var  = tk.StringVar(value=fmt_default)
            track_vars.append((keep_var, fmt_var))

            # Keep checkbox
            ttk.Checkbutton(list_frame, variable=keep_var).grid(row=r, column=0, padx=4)

            # Stream index
            ttk.Label(list_frame, text=f"#{s['index']}").grid(row=r, column=1, sticky='w', padx=4)

            # Language
            ttk.Label(list_frame, text=s['language'].upper()).grid(row=r, column=2, sticky='w', padx=4)

            # Codec
            ttk.Label(list_frame, text=s['codec_name']).grid(row=r, column=3, sticky='w', padx=4)

            # Title + flags
            flags = []
            if s['forced']: flags.append('Forced')
            if s['sdh']:    flags.append('SDH')
            if s.get('empty'):  flags.append('⚠ EMPTY')
            title_text = s['title']
            if flags: title_text += f"  [{', '.join(flags)}]"
            title_fg = 'red' if s.get('empty') else 'gray'
            ttk.Label(list_frame, text=title_text, foreground=title_fg).grid(
                row=r, column=4, sticky='w', padx=4)

            # Convert To dropdown — disable for empty tracks
            is_empty = s.get('empty', False)
            fmt_combo = ttk.Combobox(list_frame, textvariable=fmt_var,
                                     values=SUB_FORMATS, width=12,
                                     state='disabled' if is_empty else 'readonly')
            fmt_combo.grid(row=r, column=5, padx=4, pady=2)
            if is_empty:
                keep_var.set(False)  # uncheck by default
                fmt_var.set('drop')

            # Edit button (only for text-based, non-empty subtitles)
            if s['codec_name'] not in BITMAP_SUB_CODECS and not is_empty:
                edit_btn = ttk.Button(list_frame, text="✏️", width=3,
                    command=lambda si=s['index'], fi=file_info, fp=filepath: (
                        self.show_subtitle_editor(fp, si, fi)
                    ))
                edit_btn.grid(row=r, column=6, padx=2, pady=2)

        # Unbind mousewheel when dialog closes
        def on_close():
            dlg.destroy()
        dlg.protocol('WM_DELETE_WINDOW', on_close)

        # ── Extract button ──
        def do_extract():
            out_dir = self.output_dir or Path(filepath).parent

            # Check for checked tracks with no extractable format selected
            bad_tracks = []
            for s, (keep_var, fmt_var) in zip(streams, track_vars):
                if keep_var.get() and fmt_var.get() in ('copy', 'drop'):
                    lang = s['language'].upper()
                    bad_tracks.append(f"  • Track #{s['index']} ({lang}) — format set to '{fmt_var.get()}'")

            if bad_tracks:
                messagebox.showwarning(
                    "No Extract Format Selected",
                    "The following checked tracks have no extractable format selected.\n"
                    "Please choose srt, ass, webvtt, ttml, or 'extract only':\n\n" +
                    "\n".join(bad_tracks)
                )
                return

            # Make sure at least one track is ready to extract
            extractable = [
                (s, fv.get()) for s, (kv, fv) in zip(streams, track_vars)
                if kv.get() and fv.get() not in ('copy', 'drop')
            ]
            if not extractable:
                messagebox.showwarning(
                    "Nothing to Extract",
                    "No tracks are checked with an extractable format.\n"
                    "Check at least one track and set its format to srt, ass, webvtt, ttml, or 'extract only'."
                )
                return

            extracted = 0
            for s, (keep_var, fmt_var) in zip(streams, track_vars):
                fmt = fmt_var.get()
                if not keep_var.get() and fmt != 'extract only':
                    continue
                if fmt in ('copy', 'drop'):
                    continue
                # Skip empty tracks
                if s.get('empty', False):
                    self.add_log(f"Skipping empty subtitle track #{s['index']}", 'WARNING')
                    continue
                # Determine output extension
                ext_map = {'srt': '.srt', 'ass': '.ass', 'webvtt': '.vtt',
                           'ttml': '.ttml', 'extract only': '.srt'}
                out_ext = ext_map.get(fmt, '.srt')
                out_codec = fmt if fmt != 'extract only' else 'srt'
                lang = s['language']
                title_raw = (s['title'] or '').strip()
                # Filter out tag-only titles that duplicate flag-based suffixes
                _TAG_TITLES = {'forced', 'sdh', 'cc', 'hi', 'default',
                               'commentary', 'signs', 'songs',
                               'signs & songs', 'signs and songs'}
                title_slug = ''
                if title_raw and title_raw.lower() not in _TAG_TITLES:
                    title_slug = title_raw.replace(' ', '_')
                is_sdh = (title_raw.lower() in ('sdh', 'cc', 'hi')
                          or s.get('sdh', False))
                out_name = f"{Path(filepath).stem}.{lang}"
                if title_slug: out_name += f".{title_slug}"
                if s['forced']: out_name += ".forced"
                if is_sdh: out_name += ".sdh"
                out_name += out_ext
                out_path = str(out_dir / out_name)

                # ── Bitmap subtitle → text format requires OCR ──
                is_bitmap = s['codec_name'] in BITMAP_SUB_CODECS
                is_text_target = fmt in ('srt', 'ass', 'webvtt', 'ttml', 'extract only')

                if is_bitmap and is_text_target:
                    if not shutil.which('tesseract'):
                        self.add_log(f"Tesseract not installed — cannot OCR #{s['index']}. "
                                     "Install with: sudo apt install tesseract-ocr tesseract-ocr-eng", 'ERROR')
                        continue

                    self.add_log(f"OCR: bitmap subtitle #{s['index']} ({lang}) → {out_name}", 'INFO')

                    # Launch OCR monitor window
                    ocr_result = self._run_ocr_with_monitor(
                        filepath, s['index'], lang, out_path, out_name, file_info)

                    if ocr_result:
                        extracted += 1
                    continue

                # ── Normal text subtitle extraction via ffmpeg ──
                cmd = [
                    'ffmpeg', '-y', '-i', filepath,
                    '-map', f"0:{s['index']}",
                    '-c:s', out_codec,
                    out_path
                ]
                self.add_log(f"Extracting subtitle #{s['index']} ({lang}) → {out_name}", 'INFO')
                try:
                    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                    if result.returncode == 0:
                        self.add_log(f"Extracted: {out_name}", 'SUCCESS')
                        extracted += 1
                    else:
                        self.add_log(f"Failed to extract #{s['index']}: {result.stderr[-200:]}", 'ERROR')
                except Exception as e:
                    self.add_log(f"Extract error: {e}", 'ERROR')
            if extracted:
                messagebox.showinfo("Extraction Complete",
                                    f"Extracted {extracted} subtitle file(s) to:\n{out_dir}")

        # ── Save and close ──
        def do_save():
            sub_settings = {}
            for s, (keep_var, fmt_var) in zip(streams, track_vars):
                sub_settings[s['index']] = {
                    'keep':   keep_var.get(),
                    'format': fmt_var.get(),
                }
            file_info['subtitle_settings'] = sub_settings
            # Update visual indicator in tree
            self._refresh_tree_row(item, file_info)
            kept = sum(1 for v in track_vars if v[0].get())
            self.add_log(f"Subtitle settings saved: {kept}/{len(streams)} tracks kept — {os.path.basename(filepath)}", 'INFO')
            on_close()

        btn_frame = ttk.Frame(dlg, padding=(10, 8, 10, 10))
        btn_frame.pack(fill='x')
        ttk.Button(btn_frame, text="📤 Extract Selected", command=do_extract).pack(side='left', padx=2)
        ttk.Button(btn_frame, text="💾 Save & Close", command=do_save).pack(side='right', padx=(4, 0))
        ttk.Button(btn_frame, text="Cancel", command=on_close).pack(side='right')

        dlg.update_idletasks()  # ensure all widgets are rendered
        dlg.grab_set()
        dlg.wait_window()

    def open_standalone_subtitle_editor(self):
        """Open the subtitle editor as a standalone app window with its own File menu."""
        import tempfile

        editor = tk.Toplevel(self.root)
        editor.title("Subtitle Editor")
        editor.geometry("950x650")
        editor.minsize(700, 500)
        editor.resizable(True, True)
        self._center_on_main(editor)

        # ── Shared mutable state ──
        cues = []
        original_cues = []
        undo_stack = []
        redo_stack = []
        current_path = [None]  # mutable ref for current file path
        video_source = [None]  # set when editing a subtitle from a video file
        # When set: {'path': video_path, 'stream_index': N, 'temp_srt': path,
        #            'streams': [...], 'stream_info': {...}}

        # ── Color tag names ──
        TAG_MODIFIED = 'modified'
        TAG_HI = 'has_hi'
        TAG_TAGS = 'has_tags'
        TAG_LONG = 'long_line'
        TAG_SEARCH = 'search_match'
        TAG_SPELL = 'has_spelling'

        # ── Spell check state ──
        spell_error_indices = set()

        # ── Undo / Redo ──
        def push_undo():
            undo_stack.append([dict(c) for c in cues])
            redo_stack.clear()

        def do_undo(event=None):
            nonlocal cues
            if not undo_stack:
                return
            redo_stack.append([dict(c) for c in cues])
            cues = undo_stack.pop()
            refresh_tree(cues)

        def do_redo(event=None):
            nonlocal cues
            if not redo_stack:
                return
            undo_stack.append([dict(c) for c in cues])
            cues = redo_stack.pop()
            refresh_tree(cues)

        editor.bind('<Control-z>', do_undo)
        editor.bind('<Control-y>', do_redo)
        editor.bind('<Control-Z>', do_undo)
        editor.bind('<Control-Y>', do_redo)

        # ── Track state ──
        modified_count = tk.IntVar(value=0)
        deleted_count = tk.IntVar(value=0)

        # ── Classification for color coding ──
        _orig_texts = set()

        def _classify_cue(cue, orig_text=None):
            tags = set()
            text = cue['text']
            if orig_text is not None and text != orig_text:
                tags.add(TAG_MODIFIED)
            if re.search(r'\[.*?\]|\(.*?\)|♪|♫', text):
                tags.add(TAG_HI)
            if re.search(r'<[^>]+>|\{\\[^}]+\}', text):
                tags.add(TAG_TAGS)
            for line in text.split('\n'):
                if len(line) > MAX_CHARS_PER_LINE:
                    tags.add(TAG_LONG)
                    break
            return tags

        # ══════════════════════════════════════════════════════════════════════
        # Menu bar
        # ══════════════════════════════════════════════════════════════════════
        menubar = tk.Menu(editor)
        editor.configure(menu=menubar)

        # ── File menu ──
        file_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="File", menu=file_menu)

        def load_file(sub_path):
            """Load a subtitle file into the editor by path."""
            sub_exts = {'.srt', '.ass', '.ssa', '.vtt', '.sub'}
            ext = Path(sub_path).suffix.lower()
            if ext not in sub_exts:
                messagebox.showwarning("Unsupported Format",
                    f"Not a recognised subtitle file:\n{os.path.basename(sub_path)}",
                    parent=editor)
                return
            if ext in ('.srt',):
                try:
                    with open(sub_path, 'r', encoding='utf-8', errors='replace') as f:
                        srt_text = f.read()
                except Exception as e:
                    messagebox.showerror("Error", f"Failed to read file:\n{e}",
                                         parent=editor)
                    return
            else:
                # Convert to SRT via ffmpeg
                tmp_srt = tempfile.NamedTemporaryFile(suffix='.srt', delete=False,
                                                       mode='w', encoding='utf-8')
                tmp_srt.close()
                cmd = ['ffmpeg', '-y', '-i', sub_path, '-c:s', 'srt', tmp_srt.name]
                try:
                    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                    if result.returncode != 0:
                        messagebox.showerror("Error",
                            f"Failed to convert subtitle:\n{result.stderr[-300:]}",
                            parent=editor)
                        os.unlink(tmp_srt.name)
                        return
                except Exception as e:
                    messagebox.showerror("Error", f"Convert error:\n{e}", parent=editor)
                    os.unlink(tmp_srt.name)
                    return
                with open(tmp_srt.name, 'r', encoding='utf-8', errors='replace') as f:
                    srt_text = f.read()
                os.unlink(tmp_srt.name)

            title = f"Subtitle Editor — {os.path.basename(sub_path)}"
            if _load_cues_into_editor(srt_text, title, sub_path):
                self.add_log(f"Opened subtitle file: {os.path.basename(sub_path)} "
                             f"({len(cues)} entries)", 'INFO')

        def _load_cues_into_editor(srt_text, title, source_path):
            """Common logic: parse SRT text and load cues into the editor."""
            nonlocal cues, original_cues
            new_cues = parse_srt(srt_text)
            if not new_cues:
                messagebox.showwarning("Empty",
                    f"No subtitle cues found in:\n{os.path.basename(source_path)}",
                    parent=editor)
                return False

            cues = new_cues
            original_cues = [dict(c) for c in cues]
            _orig_texts.clear()
            _orig_texts.update(c['text'] for c in original_cues)
            undo_stack.clear()
            redo_stack.clear()
            current_path[0] = source_path
            editor.title(title)

            placeholder.pack_forget()
            content_frame.pack(fill='both', expand=True)
            _set_menus_state('normal')
            refresh_tree(cues)
            # Scroll to top for newly loaded file
            tree.yview_moveto(0)
            return True

        def load_video_subtitle(video_path):
            """Probe a video file for subtitle streams, let the user pick one,
            extract it, and load it into the editor for editing."""
            nonlocal cues, original_cues

            # Bitmap codecs that can't be converted to SRT
            BITMAP_CODECS = {'hdmv_pgs_subtitle', 'dvd_subtitle', 'dvb_subtitle',
                             'dvb_teletext', 'xsub'}

            streams = get_subtitle_streams(video_path)
            if not streams:
                messagebox.showinfo("No Subtitles",
                    f"No subtitle streams found in:\n{os.path.basename(video_path)}",
                    parent=editor)
                return

            # Filter out bitmap subtitles
            text_streams = [s for s in streams if s['codec_name'] not in BITMAP_CODECS]
            if not text_streams:
                messagebox.showwarning("No Editable Subtitles",
                    "This video only contains bitmap subtitle streams "
                    "(PGS/VobSub) which cannot be edited as text.",
                    parent=editor)
                return

            # If only one text stream, use it directly; otherwise show picker
            if len(text_streams) == 1:
                chosen = text_streams[0]
            else:
                chosen = [None]  # mutable ref for dialog result

                picker = tk.Toplevel(editor)
                picker.title("Select Subtitle Stream")
                picker.geometry("640x340")
                picker.transient(editor)
                picker.grab_set()
                self._center_on_main(picker)
                picker.resizable(True, True)

                ttk.Label(picker,
                          text=f"Select a subtitle stream to edit from:\n"
                               f"{os.path.basename(video_path)}",
                          padding=(10, 10)).pack()

                # ── Treeview with columns ──
                tree_frame = ttk.Frame(picker)
                tree_frame.pack(fill='both', expand=True, padx=10, pady=5)

                scrollbar = ttk.Scrollbar(tree_frame, orient='vertical')
                scrollbar.pack(side='right', fill='y')

                cols = ('stream', 'lang', 'format', 'title', 'flags')
                stream_tree = ttk.Treeview(tree_frame, columns=cols,
                                           show='headings', height=8,
                                           selectmode='browse',
                                           yscrollcommand=scrollbar.set)
                scrollbar.config(command=stream_tree.yview)

                stream_tree.heading('stream', text='#')
                stream_tree.heading('lang', text='Language')
                stream_tree.heading('format', text='Format')
                stream_tree.heading('title', text='Title')
                stream_tree.heading('flags', text='Flags')

                stream_tree.column('stream', width=50, minwidth=40, stretch=False)
                stream_tree.column('lang', width=90, minwidth=70, stretch=False)
                stream_tree.column('format', width=70, minwidth=60, stretch=False)
                stream_tree.column('title', width=200, minwidth=100, stretch=True)
                stream_tree.column('flags', width=140, minwidth=100, stretch=False)

                stream_tree.pack(fill='both', expand=True)

                for i, s in enumerate(text_streams):
                    lang = s['language'] if s['language'] != 'und' else 'Unknown'
                    flags = []
                    if s['default']:
                        flags.append('Default')
                    if s['sdh']:
                        flags.append('SDH')
                    if s['forced']:
                        flags.append('Forced')
                    flag_str = ', '.join(flags) if flags else ''
                    title_str = s['title'] if s['title'] else ''
                    stream_tree.insert('', 'end', iid=str(i),
                                       values=(s['index'], lang,
                                               s['codec_name'], title_str,
                                               flag_str))

                stream_tree.selection_set('0')

                def on_select():
                    sel = stream_tree.selection()
                    if sel:
                        chosen[0] = text_streams[int(sel[0])]
                    picker.destroy()

                def on_double_click(event):
                    on_select()

                stream_tree.bind('<Double-1>', on_double_click)

                btn_frame = ttk.Frame(picker, padding=(10, 8, 10, 10))
                btn_frame.pack(fill='x')
                ttk.Button(btn_frame, text="Edit Selected",
                           command=on_select).pack(side='right', padx=(4, 0))
                ttk.Button(btn_frame, text="Cancel",
                           command=picker.destroy).pack(side='right')

                picker.wait_window()

                chosen = chosen[0]
                if chosen is None:
                    return  # user cancelled

            # Extract the selected stream to a temp SRT file
            stream_index = chosen['index']
            tmp_srt = tempfile.NamedTemporaryFile(suffix='.srt', delete=False,
                                                   mode='w', encoding='utf-8')
            tmp_srt.close()
            cmd = ['ffmpeg', '-y', '-i', video_path,
                   '-map', f'0:{stream_index}', '-c:s', 'srt', tmp_srt.name]

            # ── Progress dialog during extraction ──
            prog_dlg = tk.Toplevel(editor)
            prog_dlg.title("Importing Subtitle")
            prog_dlg.resizable(False, False)
            prog_dlg.transient(editor)
            prog_dlg.overrideredirect(False)

            prog_f = ttk.Frame(prog_dlg, padding=20)
            prog_f.pack(fill='both', expand=True)
            ttk.Label(prog_f,
                      text=f"Importing subtitle stream #{stream_index} "
                           f"from {os.path.basename(video_path)}...",
                      wraplength=350).pack(pady=(0, 10))
            prog_bar = ttk.Progressbar(prog_f, mode='indeterminate', length=300)
            prog_bar.pack(pady=(0, 5))
            prog_bar.start(15)

            self._center_on_main(prog_dlg)
            prog_dlg.grab_set()
            prog_dlg.protocol('WM_DELETE_WINDOW', lambda: None)  # prevent closing

            extract_result = [None]  # (returncode, stderr) or Exception

            def _run_extract():
                try:
                    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                    extract_result[0] = ('ok', r.returncode, r.stderr)
                except Exception as e:
                    extract_result[0] = ('error', e)

            t = threading.Thread(target=_run_extract, daemon=True)
            t.start()

            def _check_extract():
                if t.is_alive():
                    editor.after(50, _check_extract)
                    return
                prog_bar.stop()
                prog_dlg.grab_release()
                prog_dlg.destroy()

                res = extract_result[0]
                if res is None:
                    os.unlink(tmp_srt.name)
                    return
                if res[0] == 'error':
                    messagebox.showerror("Error",
                        f"Extract error:\n{res[1]}", parent=editor)
                    os.unlink(tmp_srt.name)
                    return
                _, returncode, stderr = res
                if returncode != 0:
                    messagebox.showerror("Error",
                        f"Failed to extract subtitle stream #{stream_index}:\n"
                        f"{stderr[-300:]}",
                        parent=editor)
                    os.unlink(tmp_srt.name)
                    return

                with open(tmp_srt.name, 'r', encoding='utf-8',
                          errors='replace') as f:
                    srt_text = f.read()

                # Build title
                lang = chosen['language'] if chosen['language'] != 'und' else '?'
                title_str = (f"Subtitle Editor — Stream #{stream_index} ({lang}) — "
                             f"{os.path.basename(video_path)}")

                if _load_cues_into_editor(srt_text, title_str, tmp_srt.name):
                    # Store video source info for re-muxing on save
                    video_source[0] = {
                        'path': video_path,
                        'stream_index': stream_index,
                        'temp_srt': tmp_srt.name,
                        'streams': streams,
                        'stream_info': chosen,
                    }
                    self.add_log(
                        f"Opened video subtitle: stream #{stream_index} ({lang}) "
                        f"from {os.path.basename(video_path)} "
                        f"({len(cues)} entries)", 'INFO')
                else:
                    os.unlink(tmp_srt.name)

            editor.after(50, _check_extract)

        def do_open_file():
            path = filedialog.askopenfilename(
                parent=editor,
                title="Open Subtitle or Video File",
                filetypes=[
                    ('Subtitle files', '*.srt *.ass *.ssa *.vtt *.sub'),
                    ('Video files', '*.mkv *.mp4 *.avi *.mov *.wmv *.flv *.webm *.ts *.m2ts *.mts'),
                    ('All files', '*.*'),
                ]
            )
            if not path:
                return
            ext = Path(path).suffix.lower()
            if ext in VIDEO_EXTENSIONS:
                load_video_subtitle(path)
            else:
                video_source[0] = None  # clear video mode
                load_file(path)

        def on_drop_subtitle(event):
            """Handle subtitle or video files dragged and dropped onto the editor."""
            raw = event.data
            # tkinterdnd2 wraps paths with spaces in curly braces: {/path/to/my file.srt}
            # On Linux, file managers may also send file:// URIs (one per line)
            paths = []
            if 'file://' in raw:
                from urllib.parse import unquote, urlparse
                for line in raw.splitlines():
                    line = line.strip()
                    if line.startswith('file://'):
                        decoded = unquote(urlparse(line).path)
                        if decoded:
                            paths.append(decoded)
            else:
                i = 0
                while i < len(raw):
                    if raw[i] == '{':
                        end = raw.find('}', i)
                        paths.append(raw[i + 1:end])
                        i = end + 2
                    elif raw[i] == ' ':
                        i += 1
                    else:
                        end = raw.find(' ', i)
                        if end == -1:
                            paths.append(raw[i:])
                            break
                        else:
                            paths.append(raw[i:end])
                            i = end + 1
            if paths:
                path = paths[0]
                ext = Path(path).suffix.lower()
                if ext in VIDEO_EXTENSIONS:
                    load_video_subtitle(path)
                else:
                    video_source[0] = None  # clear video mode
                    load_file(path)

        # ── Register drag-and-drop on the editor window ──
        if HAS_DND:
            editor.drop_target_register(DND_FILES)
            editor.dnd_bind('<<Drop>>', on_drop_subtitle)

        def do_save_file():
            if not cues or not current_path[0]:
                return
            removed = len(original_cues) - len(cues)

            if video_source[0]:
                # ── Re-mux edited subtitle back into the video ──
                vs = video_source[0]
                video_path = vs['path']
                stream_idx = vs['stream_index']
                temp_srt = vs['temp_srt']
                streams = vs['streams']

                # Write edited SRT to temp file
                with open(temp_srt, 'w', encoding='utf-8') as f:
                    f.write(write_srt(cues))

                # Build ffmpeg command: map every stream in order, replacing
                # the target subtitle with the edited version to preserve track order
                tmp_out = str(Path(video_path).with_suffix('.tmp' + Path(video_path).suffix))
                cmd = ['ffmpeg', '-y', '-i', video_path, '-i', temp_srt]

                all_streams = get_all_streams(video_path)
                out_sub_count = 0
                replaced_out_sub_idx = None
                for s in all_streams:
                    if s['index'] == stream_idx:
                        # Replace this subtitle with the edited version
                        cmd.extend(['-map', '1:0'])
                        replaced_out_sub_idx = out_sub_count
                        out_sub_count += 1
                    else:
                        cmd.extend(['-map', f"0:{s['index']}"])
                        if s['codec_type'] == 'subtitle':
                            out_sub_count += 1

                # Copy all codecs (no re-encoding)
                cmd.extend(['-c', 'copy'])

                # Preserve metadata on the replaced subtitle stream
                orig = vs['stream_info']
                if replaced_out_sub_idx is not None:
                    if orig.get('language') and orig['language'] != 'und':
                        cmd.extend([f'-metadata:s:s:{replaced_out_sub_idx}',
                                    f"language={orig['language']}"])
                    if orig.get('title'):
                        cmd.extend([f'-metadata:s:s:{replaced_out_sub_idx}',
                                    f"title={orig['title']}"])
                    # Preserve disposition flags
                    disp_parts = []
                    if orig.get('default'):
                        disp_parts.append('default')
                    if orig.get('forced'):
                        disp_parts.append('forced')
                    if orig.get('sdh'):
                        disp_parts.append('hearing_impaired')
                    if disp_parts:
                        cmd.extend([f'-disposition:s:{replaced_out_sub_idx}',
                                    '+'.join(disp_parts)])

                    # For MP4 containers, subtitle codec must be mov_text
                    if Path(video_path).suffix.lower() in ('.mp4', '.m4v'):
                        cmd.extend([f'-c:s:{replaced_out_sub_idx}', 'mov_text'])

                cmd.append(tmp_out)

                self.add_log(f"Re-muxing subtitle into {os.path.basename(video_path)}...",
                             'INFO')
                self.add_log(f"ffmpeg command: {' '.join(cmd)}", 'INFO')
                editor.update_idletasks()

                try:
                    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
                    if result.returncode != 0:
                        self.add_log(f"Re-mux stderr: {result.stderr[-500:]}", 'ERROR')
                        messagebox.showerror("Re-mux Failed",
                            f"Failed to save subtitle back to video:\n\n"
                            f"{result.stderr[-400:]}",
                            parent=editor)
                        # Clean up failed temp output
                        if os.path.exists(tmp_out):
                            os.unlink(tmp_out)
                        return

                    # Atomic replace: swap temp output over original
                    os.replace(tmp_out, video_path)

                    # Cleanup temp SRT
                    try:
                        os.unlink(temp_srt)
                    except OSError:
                        pass

                    self.add_log(f"Subtitle re-muxed into video: {len(cues)} entries "
                                 f"({removed} removed) → {os.path.basename(video_path)}",
                                 'SUCCESS')
                    messagebox.showinfo("Saved",
                        f"Subtitle stream #{stream_idx} saved back to:\n"
                        f"{os.path.basename(video_path)}",
                        parent=editor)
                    video_source[0] = None  # clear video mode after successful save

                except Exception as e:
                    messagebox.showerror("Error", f"Re-mux error:\n{e}", parent=editor)
                    if os.path.exists(tmp_out):
                        os.unlink(tmp_out)
            else:
                # ── Normal subtitle file save ──
                with open(current_path[0], 'w', encoding='utf-8') as f:
                    f.write(write_srt(cues))
                self.add_log(f"Subtitle saved: {len(cues)} entries ({removed} removed) → "
                             f"{os.path.basename(current_path[0])}", 'SUCCESS')

        def do_save_as():
            if not cues:
                return
            if video_source[0]:
                ref_path = video_source[0]['path']
            elif current_path[0]:
                ref_path = current_path[0]
            else:
                ref_path = None
            out_dir = str(Path(ref_path).parent) if ref_path else ''
            default_name = (f"{Path(ref_path).stem}.srt"
                            if ref_path else "subtitle.srt")
            out_path = filedialog.asksaveasfilename(
                parent=editor,
                initialdir=out_dir,
                initialfile=default_name,
                defaultextension='.srt',
                filetypes=[('SubRip', '*.srt'), ('All files', '*.*')]
            )
            if not out_path:
                return
            with open(out_path, 'w', encoding='utf-8') as f:
                f.write(write_srt(cues))
            current_path[0] = out_path
            editor.title(f"Subtitle Editor — {os.path.basename(out_path)}")
            self.add_log(f"Subtitle saved as: {out_path}", 'SUCCESS')

        def do_export():
            if not cues:
                messagebox.showwarning("Empty", "No subtitle entries to export.",
                                       parent=editor)
                return
            # Use the video file path for directory/name when editing a video subtitle
            if video_source[0]:
                ref_path = video_source[0]['path']
            elif current_path[0]:
                ref_path = current_path[0]
            else:
                ref_path = None
            out_dir = str(Path(ref_path).parent) if ref_path else ''
            default_name = (f"{Path(ref_path).stem}.srt"
                            if ref_path else "subtitle.srt")
            out_path = filedialog.asksaveasfilename(
                parent=editor,
                initialdir=out_dir,
                initialfile=default_name,
                defaultextension='.srt',
                filetypes=[('SubRip', '*.srt'), ('All files', '*.*')]
            )
            if not out_path:
                return
            with open(out_path, 'w', encoding='utf-8') as f:
                f.write(write_srt(cues))
            self.add_log(f"Exported subtitle → {out_path}", 'SUCCESS')

        file_menu.add_command(label="Open...", accelerator="Ctrl+O", command=do_open_file)
        file_menu.add_separator()
        file_menu.add_command(label="Save", accelerator="Ctrl+S", command=do_save_file)
        file_menu.add_command(label="Save As...", accelerator="Ctrl+Shift+S",
                              command=do_save_as)
        file_menu.add_command(label="Export SRT...", command=do_export)
        file_menu.add_separator()
        file_menu.add_command(label="Batch Filter...", command=self.open_batch_filter)
        file_menu.add_separator()
        file_menu.add_command(label="Close", command=lambda: on_editor_close())

        editor.bind('<Control-o>', lambda e: do_open_file())
        editor.bind('<Control-O>', lambda e: do_open_file())
        editor.bind('<Control-s>', lambda e: do_save_file())
        editor.bind('<Control-S>', lambda e: do_save_file())

        # ── Filters menu ──
        def apply_filter(filter_func, name):
            nonlocal cues
            push_undo()
            before = len(cues)
            cues = filter_func(cues)
            after = len(cues)
            self.add_log(f"Filter '{name}': {before - after} entries removed, "
                         f"{after} remaining", 'INFO')
            refresh_tree(cues)

        def _is_mostly_allcaps():
            """Check if the subtitle text is mostly ALL CAPS."""
            if not cues:
                return False
            all_text = ' '.join(c['text'] for c in cues)
            alpha = re.sub(r'[^a-zA-Z]', '', all_text)
            if not alpha:
                return False
            return sum(1 for c in alpha if c.isupper()) / len(alpha) >= 0.6

        def apply_remove_hi():
            """Apply Remove HI, auto-running Fix ALL CAPS first if text is all-caps."""
            nonlocal cues
            if _is_mostly_allcaps():
                self.add_log("Text is mostly ALL CAPS — running Fix ALL CAPS first "
                             "to avoid false HI detection", 'INFO')
                push_undo()
                cues = filter_fix_caps(cues, self.custom_cap_words)
                refresh_tree(cues)
            apply_filter(filter_remove_hi, "Remove HI")

        def undo_all():
            nonlocal cues
            push_undo()
            cues = [dict(c) for c in original_cues]
            refresh_tree(cues)
            self.add_log("Subtitle edits reset to original", 'INFO')

        filter_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Tools", menu=filter_menu)
        filter_menu.add_command(label="Remove HI  [brackets] (parens) Speaker:",
                                command=lambda: apply_remove_hi())
        filter_menu.add_command(label="Remove Tags  <i> {\\an8}",
                                command=lambda: apply_filter(filter_remove_tags, "Remove Tags"))

        def apply_remove_ads():
            apply_filter(lambda c: filter_remove_ads(c, self.custom_ad_patterns),
                         "Remove Ads")

        filter_menu.add_command(label="Remove Ads / Credits", command=apply_remove_ads)
        filter_menu.add_command(label="Remove Stray Notes  ♪ ♫",
                                command=lambda: apply_filter(filter_remove_music_notes, "Remove Stray Notes"))
        filter_menu.add_command(label="Remove Leading Dashes  -",
                                command=lambda: apply_filter(filter_remove_leading_dashes, "Remove Leading Dashes"))
        filter_menu.add_command(label="Remove ALL CAPS HI  (UK style)",
                                command=lambda: apply_filter(filter_remove_caps_hi, "Remove CAPS HI"))
        filter_menu.add_command(label="Remove Off-Screen Quotes  ' '  (UK style)",
                                command=lambda: apply_filter(filter_remove_offscreen_quotes, "Remove Off-Screen Quotes"))
        filter_menu.add_separator()
        filter_menu.add_command(label="Remove Duplicates",
                                command=lambda: apply_filter(filter_remove_duplicates, "Remove Duplicates"))
        filter_menu.add_command(label="Merge Short Cues",
                                command=lambda: apply_filter(filter_merge_short, "Merge Short Cues"))
        filter_menu.add_command(label="Reduce to 2 Lines",
                                command=lambda: apply_filter(filter_reduce_lines, "Reduce to 2 Lines"))
        filter_menu.add_separator()

        # ── Fix ALL CAPS ──
        if not hasattr(self, 'custom_cap_words'):
            self.custom_cap_words = []

        def show_fix_caps_dialog():
            cd = tk.Toplevel(editor)
            cd.title("Fix ALL CAPS")
            cd.geometry("420x400")
            self._center_on_main(cd)
            cd.resizable(True, True)
            # Keep on top but don't grab — allows scrolling the subtitle list
            cd.attributes('-topmost', True)

            ttk.Label(cd, text="Converts ALL CAPS text to sentence case.\n"
                      "Add character names below to preserve their capitalisation.\n"
                      "You can scroll the subtitle list to find names.",
                      justify='center', padding=(10, 10)).pack()

            lf = ttk.LabelFrame(cd, text="Custom Names (saved across sessions)",
                                padding=8)
            lf.pack(fill='both', expand=True, padx=10, pady=5)

            word_list = tk.Listbox(lf, height=8, font=('Courier', 10))
            word_list.pack(fill='both', expand=True)
            for w in self.custom_cap_words:
                word_list.insert('end', w)

            add_frame = ttk.Frame(lf)
            add_frame.pack(fill='x', pady=(4, 0))
            new_word_var = tk.StringVar()
            word_entry = ttk.Entry(add_frame, textvariable=new_word_var)
            word_entry.pack(side='left', fill='x', expand=True, padx=(0, 4))
            word_entry.focus_set()
            # Right-click context menu for copy/paste
            _wm = tk.Menu(word_entry, tearoff=0)
            _wm.add_command(label="Cut", command=lambda: word_entry.event_generate('<<Cut>>'))
            _wm.add_command(label="Copy", command=lambda: word_entry.event_generate('<<Copy>>'))
            _wm.add_command(label="Paste", command=lambda: word_entry.event_generate('<<Paste>>'))
            _wm.add_separator()
            _wm.add_command(label="Select All",
                command=lambda: (word_entry.select_range(0, 'end'), word_entry.icursor('end')))
            word_entry.bind('<Button-3>', lambda e, m=_wm: m.tk_popup(e.x_root, e.y_root))

            def add_word():
                word = new_word_var.get().strip()
                if not word:
                    return
                if word.lower() not in [w.lower() for w in self.custom_cap_words]:
                    self.custom_cap_words.append(word)
                    word_list.insert('end', word)
                    self.save_preferences()
                new_word_var.set('')

            def remove_word():
                sel = word_list.curselection()
                if sel:
                    self.custom_cap_words.pop(sel[0])
                    word_list.delete(sel[0])
                    self.save_preferences()

            ttk.Button(add_frame, text="Add", command=add_word).pack(side='right')
            word_entry.bind('<Return>', lambda e: add_word())

            ttk.Label(lf, text="Names are saved automatically and persist between sessions.",
                      font=('Helvetica', 8), foreground='gray').pack(anchor='w')

            btn_frame = ttk.Frame(cd, padding=(10, 8, 10, 10))
            btn_frame.pack(fill='x')
            ttk.Button(btn_frame, text="Remove Selected", command=remove_word).pack(side='left')
            ttk.Button(btn_frame, text="Apply",
                       command=lambda: (cd.destroy(), apply_filter(
                           lambda c: filter_fix_caps(c, self.custom_cap_words),
                           "Fix ALL CAPS"))).pack(side='right')
            ttk.Button(btn_frame, text="Close", command=cd.destroy).pack(side='right', padx=4)

        filter_menu.add_command(label="Fix ALL CAPS...", command=show_fix_caps_dialog)
        filter_menu.add_separator()

        def show_ad_patterns_dialog():
            pd = tk.Toplevel(editor)
            pd.title("Ad / Credit Patterns")
            pd.geometry("500x420")
            pd.transient(editor)
            pd.grab_set()
            self._center_on_main(pd)
            pd.resizable(True, True)

            bf = ttk.LabelFrame(pd, text="Built-in Patterns (always active)", padding=8)
            bf.pack(fill='x', padx=10, pady=(10, 5))
            builtin_list = tk.Listbox(bf, height=6, font=('Courier', 9))
            builtin_list.pack(fill='x')
            for p in BUILTIN_AD_PATTERNS:
                builtin_list.insert('end', p)

            cf = ttk.LabelFrame(pd, text="Custom Patterns (saved to preferences)", padding=8)
            cf.pack(fill='both', expand=True, padx=10, pady=5)

            custom_list = tk.Listbox(cf, height=8, font=('Courier', 9))
            custom_list.pack(fill='both', expand=True)
            for p in self.custom_ad_patterns:
                custom_list.insert('end', p)

            add_frame = ttk.Frame(cf)
            add_frame.pack(fill='x', pady=(4, 0))
            new_pattern_var = tk.StringVar()
            pattern_entry = ttk.Entry(add_frame, textvariable=new_pattern_var)
            pattern_entry.pack(side='left', fill='x', expand=True, padx=(0, 4))

            def add_pattern():
                pat = new_pattern_var.get().strip()
                if not pat:
                    return
                try:
                    re.compile(pat)
                except re.error as e:
                    messagebox.showwarning("Invalid Pattern",
                                           f"Not a valid regex:\n{e}", parent=pd)
                    return
                if pat not in self.custom_ad_patterns:
                    self.custom_ad_patterns.append(pat)
                    custom_list.insert('end', pat)
                    new_pattern_var.set('')
                    self.add_log(f"Added custom ad pattern: {pat}", 'INFO')

            def remove_selected():
                sel = custom_list.curselection()
                if not sel:
                    return
                idx = sel[0]
                removed = self.custom_ad_patterns.pop(idx)
                custom_list.delete(idx)
                self.add_log(f"Removed custom ad pattern: {removed}", 'INFO')

            ttk.Button(add_frame, text="Add", command=add_pattern).pack(side='right')
            pattern_entry.bind('<Return>', lambda e: add_pattern())

            ttk.Label(cf, text="Patterns are case-insensitive regex matched at start of line.",
                      font=('Helvetica', 8), foreground='gray').pack(anchor='w')

            btn_frame = ttk.Frame(pd, padding=(10, 6, 10, 10))
            btn_frame.pack(fill='x')
            ttk.Button(btn_frame, text="Remove Selected", command=remove_selected).pack(side='left')

            def save_and_close():
                self.save_preferences()
                pd.destroy()

            ttk.Button(btn_frame, text="Save & Close", command=save_and_close).pack(side='right')
            ttk.Button(btn_frame, text="Cancel", command=pd.destroy).pack(side='right', padx=4)

        filter_menu.add_command(label="Manage Ad Patterns...",
                                command=show_ad_patterns_dialog)
        filter_menu.add_separator()
        filter_menu.add_command(label="Spell Check...",
                                accelerator="F7",
                                command=lambda: _show_spell_check())
        filter_menu.add_separator()
        filter_menu.add_command(label="Search/Replace List...",
                                command=lambda: _show_saved_replacements())

        def _show_saved_replacements():
            """Show dialog to manage and apply persistent search & replace pairs."""
            sd = tk.Toplevel(editor)
            sd.title("Search/Replace List")
            sd.geometry("550x450")
            sd.resizable(True, True)
            self._center_on_main(sd)
            sd.attributes('-topmost', True)

            f = ttk.Frame(sd, padding=12)
            f.pack(fill='both', expand=True)
            f.columnconfigure(0, weight=1)
            f.rowconfigure(1, weight=1)

            # ── Add new pair ──
            add_f = ttk.LabelFrame(f, text="Add Replacement", padding=6)
            add_f.grid(row=0, column=0, sticky='ew', pady=(0, 8))

            af = ttk.Frame(add_f)
            af.pack(fill='x')
            ttk.Label(af, text="Find:").pack(side='left', padx=(0, 4))
            sr_find = tk.StringVar()
            sr_find_entry = ttk.Entry(af, textvariable=sr_find, width=18)
            sr_find_entry.pack(side='left', padx=(0, 8))
            ttk.Label(af, text="Replace:").pack(side='left', padx=(0, 4))
            sr_repl = tk.StringVar()
            sr_repl_entry = ttk.Entry(af, textvariable=sr_repl, width=18)
            sr_repl_entry.pack(side='left', padx=(0, 8))
            sr_case = tk.BooleanVar(value=False)
            ttk.Checkbutton(af, text="Aa", variable=sr_case).pack(side='left', padx=(0, 4))

            def _add_pair():
                find = sr_find.get()
                if not find:
                    return
                repl = sr_repl.get()
                pair = [find, repl, sr_case.get()]
                if pair not in self.custom_replacements:
                    self.custom_replacements.append(pair)
                    self.save_preferences()
                _refresh_list()
                sr_find.set('')
                sr_repl.set('')

            ttk.Button(af, text="Add", command=_add_pair, width=5).pack(side='left', padx=2)

            # ── List ──
            list_f = ttk.Frame(f)
            list_f.grid(row=1, column=0, sticky='nsew')
            list_f.columnconfigure(0, weight=1)
            list_f.rowconfigure(0, weight=1)

            columns = ('find', 'replace', 'case')
            sr_tree = ttk.Treeview(list_f, columns=columns, show='headings', height=10)
            sr_tree.grid(row=0, column=0, sticky='nsew')
            sr_tree.heading('find', text='Find')
            sr_tree.heading('replace', text='Replace With')
            sr_tree.heading('case', text='Case')
            sr_tree.column('find', width=180, minwidth=100)
            sr_tree.column('replace', width=180, minwidth=100)
            sr_tree.column('case', width=50, minwidth=40, anchor='center')

            sr_scroll = ttk.Scrollbar(list_f, orient='vertical', command=sr_tree.yview)
            sr_scroll.grid(row=0, column=1, sticky='ns')
            sr_tree.configure(yscrollcommand=sr_scroll.set)

            def _refresh_list():
                sr_tree.delete(*sr_tree.get_children())
                for i, pair in enumerate(self.custom_replacements):
                    find, repl = pair[0], pair[1]
                    case = 'Yes' if (len(pair) > 2 and pair[2]) else 'No'
                    sr_tree.insert('', 'end', iid=str(i),
                                  values=(find, repl, case))

            def _remove_selected():
                sel = sr_tree.selection()
                if not sel:
                    return
                indices = sorted([int(s) for s in sel], reverse=True)
                for idx in indices:
                    if idx < len(self.custom_replacements):
                        del self.custom_replacements[idx]
                self.save_preferences()
                _refresh_list()

            def _clear_all():
                if messagebox.askyesno("Clear All",
                    "Remove all saved replacements?", parent=sd):
                    self.custom_replacements.clear()
                    self.save_preferences()
                    _refresh_list()

            # ── Buttons ──
            btn_f = ttk.Frame(f)
            btn_f.grid(row=2, column=0, sticky='ew', pady=(8, 0))

            def _apply_all():
                if not self.custom_replacements:
                    messagebox.showinfo("No Replacements",
                        "No saved replacements to apply.", parent=sd)
                    return
                push_undo()
                total_count = 0
                for pair in self.custom_replacements:
                    find, repl = pair[0], pair[1]
                    case_sensitive = len(pair) > 2 and pair[2]
                    for cue in cues:
                        old = cue['text']
                        if case_sensitive:
                            cue['text'] = cue['text'].replace(find, repl)
                        else:
                            cue['text'] = re.sub(re.escape(find), lambda m: repl,
                                                 cue['text'], flags=re.IGNORECASE)
                        if cue['text'] != old:
                            total_count += 1
                refresh_tree(cues)
                self.add_log(f"Applied {len(self.custom_replacements)} replacement rule(s), "
                             f"{total_count} cue(s) changed", 'INFO')
                messagebox.showinfo("Replacements Applied",
                    f"Applied {len(self.custom_replacements)} rule(s)\n"
                    f"{total_count} cue(s) modified", parent=sd)

            ttk.Button(btn_f, text="▶ Apply All", command=_apply_all).pack(side='left', padx=2)
            ttk.Button(btn_f, text="Remove", command=_remove_selected).pack(side='left', padx=2)
            ttk.Button(btn_f, text="Clear All", command=_clear_all).pack(side='left', padx=2)
            ttk.Button(btn_f, text="Close", command=sd.destroy).pack(side='right', padx=2)

            _refresh_list()

        def _run_spell_check():
            """Scan all cues for spelling errors. Returns errors dict or None."""
            try:
                from spellchecker import SpellChecker
            except ImportError:
                if messagebox.askyesno("Missing Package",
                    "pyspellchecker is not installed.\n\n"
                    "Would you like to install it now?",
                    parent=editor):
                    try:
                        self.add_log("Installing pyspellchecker...", 'INFO')
                        _pip_result = subprocess.run(
                            [sys.executable, '-m', 'pip', 'install',
                             '--user', '--break-system-packages', 'pyspellchecker'],
                            capture_output=True, text=True, timeout=60)
                        if _pip_result.returncode == 0:
                            from spellchecker import SpellChecker
                            self.add_log("pyspellchecker installed successfully", 'SUCCESS')
                        else:
                            messagebox.showerror("Install Failed",
                                f"pip install failed:\n{_pip_result.stderr[-300:]}",
                                parent=editor)
                            return None
                    except Exception as _e:
                        messagebox.showerror("Install Failed",
                            f"Could not install pyspellchecker:\n{_e}",
                            parent=editor)
                        return None
                else:
                    return None
            spell = SpellChecker()
            known = [w.lower() for w in self.custom_cap_words + self.custom_spell_words]
            if known:
                spell.word_frequency.load_words(known)
            spell_error_indices.clear()
            errors_by_cue = {}
            for i, cue in enumerate(cues):
                clean = re.sub(r'<[^>]+>|\{\\[^}]+\}|♪', '', cue['text'])
                words = re.findall(r"[a-zA-Z]+(?:'[a-zA-Z]+)?", clean)
                if not words:
                    continue
                unknown = spell.unknown(words)
                if unknown:
                    spell_error_indices.add(i)
                    errors_by_cue[i] = []
                    for w in words:
                        if w.lower() in unknown or w in unknown:
                            cands = spell.candidates(w)
                            errors_by_cue[i].append((w, sorted(cands) if cands else []))
            return errors_by_cue

        def _show_spell_check():
            """Run spell check and show interactive correction dialog."""
            errors_by_cue = _run_spell_check()
            if errors_by_cue is None:
                return
            if not errors_by_cue:
                spell_error_indices.clear()
                refresh_tree(cues)
                messagebox.showinfo("Spell Check", "No spelling errors found!", parent=editor)
                return
            refresh_tree(cues)

            error_list = []
            for ci in sorted(errors_by_cue.keys()):
                for word, cands in errors_by_cue[ci]:
                    error_list.append((ci, word, cands))

            current = [0]
            ignored = set()

            sd = tk.Toplevel(editor)
            sd.title("Spell Check")
            sd.geometry("500x440")
            sd.resizable(True, True)
            self._center_on_main(sd)
            sd.attributes('-topmost', True)

            sf = ttk.Frame(sd, padding=12)
            sf.pack(fill='both', expand=True)
            sf.columnconfigure(1, weight=1)
            _sp = {'padx': 6, 'pady': 4}

            stats_lbl = ttk.Label(sf, text=f"Found {len(error_list)} errors in {len(errors_by_cue)} cues",
                                  font=('Helvetica', 9))
            stats_lbl.grid(row=0, column=0, columnspan=2, sticky='w', **_sp)

            ttk.Label(sf, text="Not in dictionary:", font=('Helvetica', 10, 'bold')).grid(
                row=1, column=0, sticky='w', **_sp)
            word_var = tk.StringVar()
            ttk.Entry(sf, textvariable=word_var, state='readonly',
                      font=('Courier', 12)).grid(row=1, column=1, sticky='ew', **_sp)

            ttk.Label(sf, text="Context:").grid(row=2, column=0, sticky='nw', **_sp)
            ctx_var = tk.StringVar()
            ttk.Label(sf, textvariable=ctx_var, wraplength=380,
                      font=('Helvetica', 9), foreground='gray').grid(row=2, column=1, sticky='w', **_sp)

            ttk.Label(sf, text="Suggestions:").grid(row=3, column=0, sticky='nw', **_sp)
            sug_fr = ttk.Frame(sf)
            sug_fr.grid(row=3, column=1, sticky='nsew', **_sp)
            sug_fr.rowconfigure(0, weight=1)
            sug_fr.columnconfigure(0, weight=1)
            sf.rowconfigure(3, weight=1)

            sug_lb = tk.Listbox(sug_fr, height=6, font=('Courier', 10))
            sug_lb.grid(row=0, column=0, sticky='nsew')
            sug_sc = ttk.Scrollbar(sug_fr, orient='vertical', command=sug_lb.yview)
            sug_sc.grid(row=0, column=1, sticky='ns')
            sug_lb.configure(yscrollcommand=sug_sc.set)

            replace_var = tk.StringVar()
            def on_sug_sel(evt):
                sel = sug_lb.curselection()
                if sel:
                    replace_var.set(sug_lb.get(sel[0]))
            sug_lb.bind('<<ListboxSelect>>', on_sug_sel)

            ttk.Label(sf, text="Replace with:").grid(row=4, column=0, sticky='w', **_sp)
            ttk.Entry(sf, textvariable=replace_var, font=('Courier', 11)).grid(
                row=4, column=1, sticky='ew', **_sp)

            bf = ttk.Frame(sf)
            bf.grid(row=5, column=0, columnspan=2, sticky='ew', pady=(8, 0))

            def _show_err(idx):
                while idx < len(error_list):
                    ci, w, ca = error_list[idx]
                    if w.lower() not in ignored:
                        break
                    idx += 1
                else:
                    spell_error_indices.clear()
                    refresh_tree(cues)
                    messagebox.showinfo("Spell Check", "Spell check complete!", parent=sd)
                    sd.destroy()
                    return
                current[0] = idx
                ci, w, ca = error_list[idx]
                items = tree.get_children()
                if ci < len(items):
                    # Scroll so the match is near the middle, not at the edge
                    ahead = min(ci + 5, len(items) - 1)
                    tree.see(items[ahead])
                    tree.selection_set(items[ci])
                    tree.after(50, lambda: tree.see(items[ci]))
                word_var.set(w)
                ctx_var.set(cues[ci]['text'].replace('\n', ' / '))
                stats_lbl.configure(text=f"Error {idx+1} of {len(error_list)} (cue #{ci+1})")
                sug_lb.delete(0, 'end')
                for c in ca:
                    sug_lb.insert('end', c)
                if ca:
                    sug_lb.selection_set(0)
                    replace_var.set(ca[0])
                else:
                    replace_var.set(w)

            def _do_replace():
                ci, w, _ = error_list[current[0]]
                repl = replace_var.get().strip()
                if not repl: return
                push_undo()
                # Use str.replace for safe, literal replacement (first occurrence only)
                txt = cues[ci]['text']
                pos = txt.find(w)
                if pos == -1:
                    # Try case-insensitive find
                    pos = txt.lower().find(w.lower())
                if pos >= 0:
                    cues[ci]['text'] = txt[:pos] + repl + txt[pos + len(w):]
                refresh_tree(cues)
                _show_err(current[0] + 1)

            def _do_replace_all():
                _, w, _ = error_list[current[0]]
                repl = replace_var.get().strip()
                if not repl: return
                push_undo()
                for cue in cues:
                    # Case-sensitive replace first, then case-insensitive fallback
                    if w in cue['text']:
                        cue['text'] = cue['text'].replace(w, repl)
                    elif w.lower() in cue['text'].lower():
                        cue['text'] = re.sub(re.escape(w), repl, cue['text'], flags=re.IGNORECASE)
                refresh_tree(cues)
                _show_err(current[0] + 1)

            def _do_skip():
                _show_err(current[0] + 1)

            def _do_ignore():
                _, w, _ = error_list[current[0]]
                ignored.add(w.lower())
                _show_err(current[0] + 1)

            def _do_add_dict():
                _, w, _ = error_list[current[0]]
                if w.lower() not in [x.lower() for x in self.custom_spell_words]:
                    self.custom_spell_words.append(w)
                    self.save_preferences()
                ignored.add(w.lower())
                _show_err(current[0] + 1)

            def _do_add_name():
                _, w, _ = error_list[current[0]]
                # Add to custom_cap_words (character names — used by Fix ALL CAPS + spell check)
                if w not in self.custom_cap_words:
                    self.custom_cap_words.append(w)
                # Also add to spell words so it's never flagged
                if w.lower() not in [x.lower() for x in self.custom_spell_words]:
                    self.custom_spell_words.append(w)
                self.save_preferences()
                ignored.add(w.lower())
                _show_err(current[0] + 1)

            bf1 = ttk.Frame(bf)
            bf1.pack(fill='x')
            ttk.Button(bf1, text="Replace", command=_do_replace, width=10).pack(side='left', padx=2)
            ttk.Button(bf1, text="Replace All", command=_do_replace_all, width=10).pack(side='left', padx=2)
            ttk.Button(bf1, text="Skip", command=_do_skip, width=6).pack(side='left', padx=2)
            ttk.Button(bf1, text="Ignore", command=_do_ignore, width=8).pack(side='left', padx=2)

            bf2 = ttk.Frame(bf)
            bf2.pack(fill='x', pady=(4, 0))
            ttk.Button(bf2, text="Add to Dict", command=_do_add_dict, width=10).pack(side='left', padx=2)
            ttk.Button(bf2, text="Add as Name", command=_do_add_name, width=10).pack(side='left', padx=2)
            ttk.Button(bf2, text="Close", command=sd.destroy, width=6).pack(side='right', padx=2)

            _show_err(0)

        editor.bind('<F7>', lambda e: _show_spell_check())

        # ── Edit menu ──
        edit_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Edit", menu=edit_menu)
        edit_menu.add_command(label="Undo                Ctrl+Z", command=do_undo)
        edit_menu.add_command(label="Redo                Ctrl+Y", command=do_redo)
        edit_menu.add_command(label="Reset to Original", command=undo_all)
        edit_menu.add_separator()

        def delete_selected():
            nonlocal cues
            selected = tree.selection()
            if not selected:
                return
            push_undo()
            indices_to_remove = set(int(s) for s in selected)
            cues = [c for i, c in enumerate(cues) if i not in indices_to_remove]
            refresh_tree(cues)

        def split_selected():
            nonlocal cues
            selected = tree.selection()
            if len(selected) != 1:
                messagebox.showinfo("Split", "Select exactly one cue to split.",
                                    parent=editor)
                return
            idx = int(selected[0])
            cue = cues[idx]
            text = cue['text']
            lines = text.split('\n')
            if len(lines) < 2:
                mid = len(text) // 2
                space_pos = text.rfind(' ', 0, mid + 10)
                if space_pos > mid - 20:
                    mid = space_pos
                text1 = text[:mid].rstrip()
                text2 = text[mid:].lstrip()
            else:
                mid_line = len(lines) // 2
                text1 = '\n'.join(lines[:mid_line])
                text2 = '\n'.join(lines[mid_line:])
            if not text1 or not text2:
                return
            push_undo()
            start_ms = srt_ts_to_ms(cue['start'])
            end_ms = srt_ts_to_ms(cue['end'])
            mid_ms = (start_ms + end_ms) // 2
            cue1 = {**cue, 'text': text1, 'end': ms_to_srt_ts(mid_ms)}
            cue2 = {**cue, 'text': text2, 'start': ms_to_srt_ts(mid_ms + 1)}
            cues[idx:idx + 1] = [cue1, cue2]
            refresh_tree(cues)

        def join_selected():
            nonlocal cues
            selected = sorted(tree.selection(), key=int)
            if len(selected) < 2:
                messagebox.showinfo("Join", "Select two or more consecutive cues to join.",
                                    parent=editor)
                return
            indices = [int(s) for s in selected]
            for i in range(1, len(indices)):
                if indices[i] != indices[i - 1] + 1:
                    messagebox.showwarning("Join",
                        "Selected cues must be consecutive.", parent=editor)
                    return
            push_undo()
            first = cues[indices[0]]
            last = cues[indices[-1]]
            merged_text = ' '.join(cues[i]['text'] for i in indices)
            merged = {**first, 'end': last['end'], 'text': merged_text}
            cues[indices[0]:indices[-1] + 1] = [merged]
            refresh_tree(cues)

        edit_menu.add_command(label="Delete Selected     Del", command=delete_selected)
        edit_menu.add_command(label="Split Cue", command=split_selected)
        edit_menu.add_command(label="Join Selected Cues", command=join_selected)

        # ── Timing menu ──
        def show_timing_dialog():
            td = tk.Toplevel(editor)
            td.title("Timing Adjustment")
            td.geometry("440x380")
            td.transient(editor)
            self._center_on_main(td)
            td.resizable(False, False)
            td.attributes('-topmost', True)

            of = ttk.LabelFrame(td, text="Offset (shift all timestamps)", padding=8)
            of.pack(fill='x', padx=10, pady=(10, 5))
            offset_var = tk.StringVar(value="0")
            ttk.Label(of, text="Milliseconds (+/−):").pack(side='left')
            ttk.Entry(of, textvariable=offset_var, width=10).pack(side='left', padx=4)

            def apply_offset():
                nonlocal cues
                try:
                    ms = int(offset_var.get())
                except ValueError:
                    messagebox.showwarning("Invalid", "Enter a number in milliseconds.",
                                           parent=td)
                    return
                if ms == 0:
                    return
                push_undo()
                cues = shift_timestamps(cues, ms)
                refresh_tree(cues)
                direction = "forward" if ms > 0 else "backward"
                self.add_log(f"Shifted timestamps {direction} by {abs(ms)}ms", 'INFO')
                td.destroy()

            ttk.Button(of, text="Apply", command=apply_offset).pack(side='right')

            sf = ttk.LabelFrame(td, text="Stretch (scale timestamps)", padding=8)
            sf.pack(fill='x', padx=10, pady=5)
            stretch_var = tk.StringVar(value="1.0")
            ttk.Label(sf, text="Factor:").pack(side='left')
            ttk.Entry(sf, textvariable=stretch_var, width=10).pack(side='left', padx=4)

            def apply_stretch():
                nonlocal cues
                try:
                    factor = float(stretch_var.get())
                except ValueError:
                    messagebox.showwarning("Invalid", "Enter a decimal number (e.g. 1.04).",
                                           parent=td)
                    return
                if factor <= 0:
                    messagebox.showwarning("Invalid", "Factor must be positive.", parent=td)
                    return
                if factor == 1.0:
                    return
                push_undo()
                cues = stretch_timestamps(cues, factor)
                refresh_tree(cues)
                self.add_log(f"Stretched timestamps by factor {factor}", 'INFO')
                td.destroy()

            ttk.Button(sf, text="Apply", command=apply_stretch).pack(side='right')

            # ── Two-Point Sync ──
            tp = ttk.LabelFrame(td, text="Two-Point Sync (fix offset + drift)", padding=8)
            tp.pack(fill='x', padx=10, pady=5)

            ttk.Label(tp, text="Pick two cues and enter the correct start times.\n"
                              "All timestamps will be linearly adjusted.",
                      font=('Helvetica', 8), foreground='gray').pack(anchor='w')

            tp_grid = ttk.Frame(tp)
            tp_grid.pack(fill='x', pady=(4, 0))
            tp_grid.columnconfigure(2, weight=1)

            # Point A
            ttk.Label(tp_grid, text="Point A — Cue #:").grid(row=0, column=0, sticky='w', padx=(0, 4), pady=2)
            tp_a_cue = tk.StringVar(value="1")
            ttk.Entry(tp_grid, textvariable=tp_a_cue, width=6).grid(row=0, column=1, sticky='w', pady=2)
            ttk.Label(tp_grid, text="Correct time:").grid(row=0, column=2, sticky='e', padx=(8, 4), pady=2)
            tp_a_time = tk.StringVar(value="00:00:00,000")
            ttk.Entry(tp_grid, textvariable=tp_a_time, width=14).grid(row=0, column=3, sticky='w', pady=2)

            # Point B
            ttk.Label(tp_grid, text="Point B — Cue #:").grid(row=1, column=0, sticky='w', padx=(0, 4), pady=2)
            tp_b_cue = tk.StringVar(value=str(len(cues)))
            ttk.Entry(tp_grid, textvariable=tp_b_cue, width=6).grid(row=1, column=1, sticky='w', pady=2)
            ttk.Label(tp_grid, text="Correct time:").grid(row=1, column=2, sticky='e', padx=(8, 4), pady=2)
            tp_b_time = tk.StringVar(value="00:00:00,000")
            ttk.Entry(tp_grid, textvariable=tp_b_time, width=14).grid(row=1, column=3, sticky='w', pady=2)

            def _fill_current(var_cue, var_time):
                """Fill the time field with the current start time of the selected cue."""
                try:
                    idx = int(var_cue.get()) - 1
                    if 0 <= idx < len(cues):
                        var_time.set(cues[idx]['start'])
                        # Highlight the cue in the tree
                        items = tree.get_children()
                        if idx < len(items):
                            tree.see(items[idx])
                            tree.selection_set(items[idx])
                except (ValueError, IndexError):
                    pass

            fill_f = ttk.Frame(tp)
            fill_f.pack(fill='x', pady=(4, 0))
            ttk.Button(fill_f, text="Get A", width=6,
                       command=lambda: _fill_current(tp_a_cue, tp_a_time)).pack(side='left', padx=2)
            ttk.Button(fill_f, text="Get B", width=6,
                       command=lambda: _fill_current(tp_b_cue, tp_b_time)).pack(side='left', padx=2)
            ttk.Label(fill_f, text="(fills current time for that cue)",
                      font=('Helvetica', 8), foreground='gray').pack(side='left', padx=8)

            def apply_two_point():
                nonlocal cues
                try:
                    idx_a = int(tp_a_cue.get()) - 1
                    idx_b = int(tp_b_cue.get()) - 1
                except ValueError:
                    messagebox.showwarning("Invalid", "Enter cue numbers.", parent=td)
                    return
                if idx_a < 0 or idx_a >= len(cues) or idx_b < 0 or idx_b >= len(cues):
                    messagebox.showwarning("Invalid",
                        f"Cue numbers must be between 1 and {len(cues)}.", parent=td)
                    return
                if idx_a == idx_b:
                    messagebox.showwarning("Invalid",
                        "Point A and B must be different cues.", parent=td)
                    return
                try:
                    ms_a = srt_ts_to_ms(tp_a_time.get())
                    ms_b = srt_ts_to_ms(tp_b_time.get())
                except Exception:
                    messagebox.showwarning("Invalid",
                        "Enter times in SRT format: HH:MM:SS,mmm", parent=td)
                    return
                push_undo()
                cues = two_point_sync(cues, idx_a, ms_a, idx_b, ms_b)
                refresh_tree(cues)
                self.add_log(f"Two-point sync: cue #{idx_a+1} → {tp_a_time.get()}, "
                             f"cue #{idx_b+1} → {tp_b_time.get()}", 'INFO')
                td.destroy()

            ttk.Button(fill_f, text="Apply Sync", command=apply_two_point).pack(side='right', padx=2)

            ttk.Button(td, text="Close", command=td.destroy).pack(pady=(5, 10))

        timing_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Timing", menu=timing_menu)
        timing_menu.add_command(label="Offset / Stretch...", command=show_timing_dialog)
        timing_menu.add_command(label="Smart Sync...",
                                command=lambda: _show_smart_sync())

        # ── Quick Sync submenu ──
        quick_sync_menu = tk.Menu(timing_menu, tearoff=0)
        timing_menu.add_cascade(label="Quick Sync", menu=quick_sync_menu)

        def _quick_sync_first_cue():
            """Shift all cues so the first cue starts at a user-specified time.
            Includes an embedded mpv player for marking the exact time."""
            if not cues:
                messagebox.showinfo("No Subtitles", "Load subtitles first.",
                                    parent=editor)
                return

            qd = tk.Toplevel(editor)
            qd.title("Quick Sync — Set First Cue Time")
            qd.geometry("720x620")
            qd.minsize(640, 540)
            qd.resizable(True, True)
            self._center_on_main(qd)

            f = ttk.Frame(qd, padding=10)
            f.pack(fill='both', expand=True)
            f.columnconfigure(1, weight=1)
            f.rowconfigure(2, weight=1)  # video frame expands

            first_cue = cues[0]
            current_start = first_cue['start']
            preview_text = first_cue['text'].replace('\n', ' ')
            if len(preview_text) > 60:
                preview_text = preview_text[:57] + '...'

            # ── Video file ──
            ttk.Label(f, text="Video file:").grid(
                row=0, column=0, sticky='w', padx=4, pady=2)
            _qs_vpath = tk.StringVar()
            # Try to find video automatically
            try:
                if hasattr(editor, '_qs_last_video') and editor._qs_last_video:
                    _qs_vpath.set(editor._qs_last_video)
                elif current_path[0]:
                    _sub_dir = os.path.dirname(current_path[0])
                    _sub_stem = os.path.splitext(
                        os.path.basename(current_path[0]))[0]
                    for _i in range(3):
                        _dot = _sub_stem.rfind('.')
                        if _dot > 0:
                            _sub_stem = _sub_stem[:_dot]
                        else:
                            break
                    for ext in VIDEO_EXTENSIONS:
                        _vp = os.path.join(_sub_dir, _sub_stem + ext)
                        if os.path.isfile(_vp):
                            _qs_vpath.set(_vp)
                            break
            except Exception:
                pass

            _vpath_entry = ttk.Entry(f, textvariable=_qs_vpath)
            _vpath_entry.grid(row=0, column=1, sticky='ew', padx=4, pady=2)

            def _qs_browse():
                init_dir = os.path.dirname(_qs_vpath.get()) if _qs_vpath.get() \
                    else (os.path.dirname(current_path[0]) if current_path[0] else '')
                p = None
                if shutil.which('zenity'):
                    try:
                        cmd = ['zenity', '--file-selection',
                               '--title', 'Select Video File',
                               '--file-filter',
                               'Video files|*.mkv *.mp4 *.avi *.mov *.ts *.m2ts *.mts *.webm *.wmv *.flv',
                               '--file-filter', 'All files|*']
                        if init_dir:
                            cmd += ['--filename', init_dir + '/']
                        r = subprocess.run(cmd, capture_output=True,
                                           text=True, timeout=120)
                        if r.returncode == 0 and r.stdout.strip():
                            p = r.stdout.strip()
                    except Exception:
                        pass
                if not p:
                    p = filedialog.askopenfilename(
                        parent=qd, title="Select Video File",
                        initialdir=init_dir or None,
                        filetypes=[("Video files",
                                    "*.mkv *.mp4 *.avi *.mov *.ts *.m2ts"),
                                   ("All files", "*.*")])
                if p:
                    _qs_vpath.set(p)
                    # Auto-load the video after browse selection
                    qd.after(100, _play_video)
            ttk.Button(f, text="Browse...", command=_qs_browse).grid(
                row=0, column=2, padx=4, pady=2)

            # ── Embedded video player frame ──
            video_border = ttk.Frame(f, relief='sunken', borderwidth=2)
            video_border.grid(row=2, column=0, columnspan=3,
                              sticky='nsew', padx=4, pady=4)
            video_frame = tk.Frame(video_border, bg='black',
                                   width=640, height=360)
            video_frame.pack(fill='both', expand=True)
            video_frame.pack_propagate(False)

            _placeholder_label = tk.Label(video_frame,
                text="Drop a video file here or click Browse",
                bg='black', fg='#666', font=('Helvetica', 12))

            # ── Drag-and-drop support ──
            def _on_qs_drop(event):
                """Handle video files dropped onto the Quick Sync dialog."""
                raw = event.data
                paths = []
                if 'file://' in raw:
                    from urllib.parse import unquote, urlparse
                    for line in raw.splitlines():
                        line = line.strip()
                        if line.startswith('file://'):
                            decoded = unquote(urlparse(line).path)
                            if decoded:
                                paths.append(decoded)
                else:
                    i = 0
                    while i < len(raw):
                        if raw[i] == '{':
                            end = raw.find('}', i)
                            paths.append(raw[i + 1:end])
                            i = end + 2
                        elif raw[i] == ' ':
                            i += 1
                        else:
                            end = raw.find(' ', i)
                            if end == -1:
                                end = len(raw)
                            paths.append(raw[i:end])
                            i = end + 1

                # Find first video file in dropped paths
                for p in paths:
                    if os.path.isfile(p):
                        ext = os.path.splitext(p)[1].lower()
                        if ext in VIDEO_EXTENSIONS:
                            _qs_vpath.set(p)
                            qd.after(100, _play_video)
                            return

            try:
                qd.drop_target_register(DND_FILES)
                qd.dnd_bind('<<Drop>>', _on_qs_drop)
            except Exception:
                pass  # tkinterdnd2 not available
            _placeholder_label.place(relx=0.5, rely=0.5, anchor='center')

            # ── mpv player integration ──
            import tempfile as _qs_tempfile
            import socket as _qs_socket
            import json as _qs_json

            _mpv_proc = [None]
            _mpv_socket_path = os.path.join(
                _qs_tempfile.gettempdir(),
                f'docflix_mpv_{os.getpid()}')

            def _mpv_cmd(command_list):
                """Send a command to mpv via IPC and return the response."""
                try:
                    sock = _qs_socket.socket(
                        _qs_socket.AF_UNIX, _qs_socket.SOCK_STREAM)
                    sock.settimeout(2)
                    sock.connect(_mpv_socket_path)
                    payload = _qs_json.dumps(
                        {"command": command_list}) + '\n'
                    sock.sendall(payload.encode())
                    data = sock.recv(4096).decode()
                    sock.close()
                    return _qs_json.loads(data)
                except Exception:
                    return None

            def _play_video():
                vp = _qs_vpath.get().strip()
                if not vp or not os.path.isfile(vp):
                    messagebox.showwarning("No Video",
                        "Select a video file first.", parent=qd)
                    return

                # Kill previous mpv instance if running
                if _mpv_proc[0] and _mpv_proc[0].poll() is None:
                    _mpv_proc[0].terminate()
                    _mpv_proc[0].wait(timeout=5)

                # Clean up old socket
                if os.path.exists(_mpv_socket_path):
                    try:
                        os.unlink(_mpv_socket_path)
                    except OSError:
                        pass

                # Hide placeholder
                _placeholder_label.place_forget()

                # Get the X11 window ID for embedding
                video_frame.update_idletasks()
                wid = str(video_frame.winfo_id())

                # Launch mpv embedded in the video frame
                try:
                    _mpv_proc[0] = subprocess.Popen([
                        'mpv',
                        f'--input-ipc-server={_mpv_socket_path}',
                        f'--wid={wid}',
                        '--pause',
                        '--osd-level=2',
                        '--osd-fractions',
                        '--keep-open=yes',
                        '--no-border',
                        '--cursor-autohide=1000',
                        vp
                    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    mark_btn.configure(state='normal')
                    _mute_btn.configure(text="🔊")
                    _vol_var.set(100)
                    editor._qs_last_video = vp
                except FileNotFoundError:
                    messagebox.showerror("mpv Not Found",
                        "mpv is not installed.\n\n"
                        "Install with: sudo apt install mpv", parent=qd)
                    _placeholder_label.place(relx=0.5, rely=0.5, anchor='center')
                except Exception as e:
                    messagebox.showerror("Player Error",
                        f"Could not launch mpv:\n{e}", parent=qd)
                    _placeholder_label.place(relx=0.5, rely=0.5, anchor='center')

            def _mark_time():
                """Query mpv for current playback position and fill the time field."""
                if not _mpv_proc[0] or _mpv_proc[0].poll() is not None:
                    messagebox.showinfo("Player Closed",
                        "Load the video first.", parent=qd)
                    mark_btn.configure(state='disabled')
                    pass  # player closed
                    return

                resp = _mpv_cmd(["get_property", "playback-time"])
                if resp and 'data' in resp and resp['data'] is not None:
                    seconds = resp['data']
                    ms = int(seconds * 1000)
                    time_var.set(ms_to_srt_ts(ms))
                    time_entry.select_range(0, 'end')
                else:
                    messagebox.showwarning("Could Not Read Time",
                        "Could not get playback position from mpv.\n"
                        "Make sure the video is loaded.", parent=qd)

            def _mpv_seek(amount):
                """Seek mpv by amount in seconds."""
                if not _mpv_proc[0] or _mpv_proc[0].poll() is not None:
                    return
                _mpv_cmd(["seek", str(amount), "relative+exact"])

            def _mpv_frame_step(direction='forward'):
                """Step one frame forward or backward."""
                if not _mpv_proc[0] or _mpv_proc[0].poll() is not None:
                    return
                if direction == 'forward':
                    _mpv_cmd(["frame-step"])
                else:
                    _mpv_cmd(["frame-back-step"])

            def _mpv_pause_toggle():
                """Toggle play/pause."""
                if not _mpv_proc[0] or _mpv_proc[0].poll() is not None:
                    return
                _mpv_cmd(["cycle", "pause"])

            def _on_close():
                # Kill mpv and clean up socket
                if _mpv_proc[0] and _mpv_proc[0].poll() is None:
                    _mpv_proc[0].terminate()
                    try:
                        _mpv_proc[0].wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        _mpv_proc[0].kill()
                _mpv_proc[0] = None
                if os.path.exists(_mpv_socket_path):
                    try:
                        os.unlink(_mpv_socket_path)
                    except OSError:
                        pass
                # Reset state so next open starts fresh
                editor._qs_last_video = None
                qd.destroy()

            qd.protocol("WM_DELETE_WINDOW", _on_close)

            # ── Transport controls ──
            transport_f = ttk.Frame(f)
            transport_f.grid(row=3, column=0, columnspan=3, sticky='ew', pady=(4, 0))

            _tb_w = 3
            _transport_btns = [
                ("⏮",  lambda: _mpv_seek(-5),               "Rewind 5 seconds"),
                ("◀◀", lambda: _mpv_seek(-1),               "Rewind 1 second"),
                ("◀",  lambda: _mpv_seek(-0.1),             "Rewind 100ms"),
                ("|◀", lambda: _mpv_frame_step('backward'), "Back 1 frame"),
                ("⏯",  _mpv_pause_toggle,                   "Play / Pause"),
                ("▶|", lambda: _mpv_frame_step('forward'),  "Forward 1 frame"),
                ("▶",  lambda: _mpv_seek(0.1),              "Forward 100ms"),
                ("▶▶", lambda: _mpv_seek(1),                "Forward 1 second"),
                ("⏭",  lambda: _mpv_seek(5),                "Forward 5 seconds"),
            ]
            for _sym, _cmd, _tip in _transport_btns:
                _px = 2 if _sym == "⏯" else 1
                _b = ttk.Button(transport_f, text=_sym, width=_tb_w, command=_cmd)
                _b.pack(side='left', padx=_px)
                _create_tooltip(_b, _tip)

            mark_btn = ttk.Button(transport_f, text="⏱ Mark",
                                  command=_mark_time, width=6,
                                  state='disabled')
            mark_btn.pack(side='left', padx=(6, 0))
            _create_tooltip(mark_btn, "Capture current playback time")

            # ── Volume controls ──
            def _mpv_toggle_mute():
                if not _mpv_proc[0] or _mpv_proc[0].poll() is not None:
                    return
                _mpv_cmd(["cycle", "mute"])
                # Update mute button label
                resp = _mpv_cmd(["get_property", "mute"])
                if resp and 'data' in resp:
                    _mute_btn.configure(text="🔇" if resp['data'] else "🔊")

            def _mpv_set_volume(val):
                if not _mpv_proc[0] or _mpv_proc[0].poll() is not None:
                    return
                _mpv_cmd(["set_property", "volume", float(val)])

            _mute_btn = ttk.Button(transport_f, text="🔊", width=2,
                                   command=_mpv_toggle_mute)
            _mute_btn.pack(side='right', padx=(4, 0))
            _create_tooltip(_mute_btn, "Mute / Unmute")

            _vol_var = tk.DoubleVar(value=100)
            _vol_scale = ttk.Scale(transport_f, from_=0, to=100,
                                   orient='horizontal', length=80,
                                   variable=_vol_var,
                                   command=_mpv_set_volume)
            _vol_scale.pack(side='right', padx=2)
            _create_tooltip(_vol_scale, "Volume")

            # ── Sync controls ──
            ttk.Separator(f, orient='horizontal').grid(
                row=4, column=0, columnspan=3, sticky='ew', pady=6)

            sync_f = ttk.Frame(f)
            sync_f.grid(row=5, column=0, columnspan=3, sticky='ew', padx=4)
            sync_f.columnconfigure(1, weight=1)

            ttk.Label(sync_f, text="First cue:",
                      font=('Helvetica', 9, 'bold')).grid(
                          row=0, column=0, sticky='w', pady=1)
            ttk.Label(sync_f, text=f'"{preview_text}"',
                      font=('Helvetica', 9), foreground='gray').grid(
                          row=0, column=1, columnspan=2, sticky='w', padx=8, pady=1)

            ttk.Label(sync_f, text="Current:").grid(
                row=1, column=0, sticky='w', pady=1)
            ttk.Label(sync_f, text=current_start,
                      font=('Courier', 10)).grid(
                          row=1, column=1, sticky='w', padx=8, pady=1)

            ttk.Label(sync_f, text="New start:").grid(
                row=2, column=0, sticky='w', pady=2)
            time_var = tk.StringVar(value=current_start)
            _time_f = ttk.Frame(sync_f)
            _time_f.grid(row=2, column=1, columnspan=2, sticky='w', padx=8, pady=2)
            time_entry = ttk.Entry(_time_f, textvariable=time_var, width=16,
                                   font=('Courier', 10))
            time_entry.pack(side='left')
            ttk.Label(_time_f, text="HH:MM:SS,mmm",
                      foreground='gray', font=('Helvetica', 8)).pack(
                          side='left', padx=8)

            offset_var = tk.StringVar(value="Offset: 0ms")
            ttk.Label(sync_f, textvariable=offset_var,
                      font=('Helvetica', 9), foreground='#666').grid(
                          row=3, column=0, columnspan=3, sticky='w', pady=1)

            def _update_offset(*_args):
                try:
                    new_ms = srt_ts_to_ms(time_var.get().strip())
                    old_ms = srt_ts_to_ms(current_start)
                    diff = new_ms - old_ms
                    sign = '+' if diff >= 0 else ''
                    offset_var.set(f"Offset: {sign}{diff}ms ({sign}{diff/1000:.1f}s)")
                except Exception:
                    offset_var.set("Offset: (invalid time format)")
            time_var.trace_add('write', _update_offset)

            # ── Action buttons ──
            btn_f = ttk.Frame(f)
            btn_f.grid(row=6, column=0, columnspan=3, sticky='ew', pady=(8, 0))

            def _apply_first_cue():
                nonlocal cues
                try:
                    new_ms = srt_ts_to_ms(time_var.get().strip())
                except Exception:
                    messagebox.showwarning("Invalid Time",
                        "Enter time in SRT format: HH:MM:SS,mmm", parent=qd)
                    return
                old_ms = srt_ts_to_ms(current_start)
                offset = new_ms - old_ms
                if offset == 0:
                    _on_close()
                    return
                push_undo()
                cues = shift_timestamps(cues, offset)
                refresh_tree(cues)
                sign = '+' if offset > 0 else ''
                self.add_log(f"Quick Sync: shifted all cues {sign}{offset}ms "
                             f"(first cue → {time_var.get().strip()})", 'SUCCESS')
                _on_close()

            time_entry.bind('<Return>', lambda e: _apply_first_cue())
            _apply_btn = ttk.Button(btn_f, text="Apply",
                                    command=_apply_first_cue, width=8)
            _apply_btn.pack(side='left', padx=2)
            _create_tooltip(_apply_btn, "Shift all cues by the offset and close")
            _cancel_btn = ttk.Button(btn_f, text="Cancel",
                                     command=_on_close, width=8)
            _cancel_btn.pack(side='left', padx=2)
            _create_tooltip(_cancel_btn, "Close without applying changes")

            # Auto-load video if one was detected
            if _qs_vpath.get().strip() and os.path.isfile(_qs_vpath.get().strip()):
                qd.after(300, _play_video)

        quick_sync_menu.add_command(label="Set First Cue Time...",
                                    command=_quick_sync_first_cue)

        def _find_video_for_subtitle():
            """Try to find the video file for the current subtitle."""
            vpath = None
            if video_source and video_source[0]:
                vpath = video_source[0].get('path')
            if not vpath and current_path[0]:
                sub_dir = os.path.dirname(current_path[0])
                sub_stem = os.path.splitext(os.path.basename(current_path[0]))[0]
                for _ in range(3):
                    if '.' in sub_stem:
                        sub_stem = sub_stem.rsplit('.', 1)[0]
                for ext in VIDEO_EXTENSIONS:
                    candidate = os.path.join(sub_dir, sub_stem + ext)
                    if os.path.isfile(candidate):
                        return candidate
                for ext in VIDEO_EXTENSIONS:
                    for fp in Path(sub_dir).glob(f'*{ext}'):
                        if fp.is_file() and not fp.name.startswith('.'):
                            return str(fp)
            return vpath

        def _show_smart_sync():
            """Auto-sync subtitles using Whisper speech recognition."""
            import threading

            if not cues:
                messagebox.showinfo("No Subtitles", "Load subtitles first.", parent=editor)
                return

            # Check faster-whisper availability
            try:
                from faster_whisper import WhisperModel
            except ImportError:
                if messagebox.askyesno("Missing Package",
                    "faster-whisper is not installed.\n\n"
                    "Would you like to install it now?\n"
                    "(This may take a few minutes — downloads ~200MB)",
                    parent=editor):
                    try:
                        self.add_log("Installing faster-whisper...", 'INFO')
                        _pip_result = subprocess.run(
                            [sys.executable, '-m', 'pip', 'install',
                             '--user', '--break-system-packages', 'faster-whisper'],
                            capture_output=True, text=True, timeout=300)
                        if _pip_result.returncode == 0:
                            self.add_log("faster-whisper installed successfully", 'SUCCESS')
                        else:
                            messagebox.showerror("Install Failed",
                                f"pip install failed:\n{_pip_result.stderr[-300:]}",
                                parent=editor)
                            return
                    except Exception as _e:
                        messagebox.showerror("Install Failed",
                            f"Could not install:\n{_e}", parent=editor)
                        return
                else:
                    return

            vpath = _find_video_for_subtitle()

            sd = tk.Toplevel(editor)
            sd.title("Smart Sync")
            sd.geometry("560x580")
            sd.resizable(True, True)
            self._center_on_main(sd)

            f = ttk.Frame(sd, padding=12)
            f.pack(fill='both', expand=True)
            f.columnconfigure(1, weight=1)
            _sp = {'padx': 6, 'pady': 4}

            # ── Video file ──
            ttk.Label(f, text="Video file:").grid(row=0, column=0, sticky='w', **_sp)
            vpath_var = tk.StringVar(value=vpath or '')
            ttk.Entry(f, textvariable=vpath_var).grid(row=0, column=1, sticky='ew', **_sp)
            def _browse_vid():
                # Start in the subtitle's folder if available
                init_dir = ''
                if vpath_var.get():
                    init_dir = os.path.dirname(vpath_var.get())
                elif current_path[0]:
                    init_dir = os.path.dirname(current_path[0])
                # Try zenity first (better sizing), fall back to tkinter
                p = None
                if shutil.which('zenity'):
                    try:
                        cmd = ['zenity', '--file-selection',
                               '--title', 'Select Video File',
                               '--file-filter', 'Video files|*.mkv *.mp4 *.avi *.mov *.ts *.m2ts *.mts *.webm *.wmv *.flv',
                               '--file-filter', 'All files|*']
                        if init_dir:
                            cmd += ['--filename', init_dir + '/']
                        result = subprocess.run(cmd, capture_output=True,
                                                text=True, timeout=120)
                        if result.returncode == 0 and result.stdout.strip():
                            p = result.stdout.strip()
                    except Exception:
                        pass
                if not p:
                    p = filedialog.askopenfilename(
                        parent=sd,
                        title="Select Video File",
                        initialdir=init_dir or None,
                        filetypes=[("Video files", "*.mkv *.mp4 *.avi *.mov *.ts *.m2ts"),
                                   ("All files", "*.*")])
                if p:
                    vpath_var.set(p)
            ttk.Button(f, text="Browse...", command=_browse_vid).grid(row=0, column=2, **_sp)

            # ── Model selection ──
            model_label = ttk.Label(f, text="Whisper model:")
            model_label.grid(row=1, column=0, sticky='w', **_sp)
            model_f = ttk.Frame(f)
            model_f.grid(row=1, column=1, columnspan=2, sticky='w', **_sp)
            model_var = tk.StringVar(value='base')
            for m, tip in [('tiny', '~75MB, fastest'),
                           ('base', '~150MB, good balance'),
                           ('small', '~500MB, more accurate')]:
                ttk.Radiobutton(model_f, text=f"{m} ({tip})",
                               variable=model_var, value=m).pack(anchor='w')

            # ── Language ──
            ttk.Label(f, text="Language:").grid(row=2, column=0, sticky='w', **_sp)
            lang_var = tk.StringVar(value='en')
            lang_f = ttk.Frame(f)
            lang_f.grid(row=2, column=1, columnspan=2, sticky='w', **_sp)
            ttk.Entry(lang_f, textvariable=lang_var, width=5).pack(side='left')
            ttk.Label(lang_f, text="(en, fr, es, de, etc. — blank = auto-detect)",
                      foreground='gray', font=('Helvetica', 8)).pack(side='left', padx=8)

            # ── Engine selection ──
            ttk.Label(f, text="Engine:").grid(row=3, column=0, sticky='w', **_sp)
            engine_f = ttk.Frame(f)
            engine_f.grid(row=3, column=1, columnspan=2, sticky='w', **_sp)
            engine_var = tk.StringVar(value='faster-whisper')

            def _on_engine_change():
                eng = engine_var.get()
                if eng == 'whisperx':
                    finetune_var.set('200')
                    finetune_hint.config(
                        text="ms  (phoneme onset is ~200ms before perceived speech)")
                    direct_rb.configure(state='normal')
                else:
                    finetune_var.set('400')
                    finetune_hint.config(
                        text="ms  (applied after sync — compensates for Whisper timing)")
                    # Direct Align requires WhisperX — switch away if selected
                    if scan_mode_var.get() == 'direct':
                        scan_mode_var.set('quick')
                    direct_rb.configure(state='disabled')
                _on_scan_mode_change()

            ttk.Radiobutton(engine_f, text="Standard (faster-whisper)",
                           variable=engine_var, value='faster-whisper',
                           command=_on_engine_change).pack(anchor='w')
            ttk.Radiobutton(engine_f,
                           text="Precise (WhisperX) — phoneme-level alignment",
                           variable=engine_var, value='whisperx',
                           command=_on_engine_change).pack(anchor='w')

            # ── Scan mode ──
            ttk.Label(f, text="Scan mode:").grid(row=4, column=0, sticky='w', **_sp)
            scan_f = ttk.Frame(f)
            scan_f.grid(row=4, column=1, columnspan=2, sticky='w', **_sp)
            scan_mode_var = tk.StringVar(value='quick')

            def _on_scan_mode_change():
                mode = scan_mode_var.get()
                if mode == 'quick':
                    seg_label.grid()
                    sample_f.grid()
                    model_label.grid()
                    model_f.grid()
                elif mode == 'full':
                    seg_label.grid_remove()
                    sample_f.grid_remove()
                    model_label.grid()
                    model_f.grid()
                else:  # direct
                    seg_label.grid_remove()
                    sample_f.grid_remove()
                    model_label.grid_remove()
                    model_f.grid_remove()

            ttk.Radiobutton(scan_f, text="Quick Scan", variable=scan_mode_var,
                           value='quick', command=_on_scan_mode_change).pack(side='left', padx=(0, 8))
            ttk.Radiobutton(scan_f, text="Full Scan (for Re-time)",
                           variable=scan_mode_var, value='full',
                           command=_on_scan_mode_change).pack(side='left', padx=(0, 8))
            direct_rb = ttk.Radiobutton(scan_f,
                           text="Direct Align",
                           variable=scan_mode_var, value='direct',
                           command=_on_scan_mode_change, state='disabled')
            direct_rb.pack(side='left')

            seg_label = ttk.Label(f, text="Segments:")
            seg_label.grid(row=5, column=0, sticky='w', **_sp)
            sample_f = ttk.Frame(f)
            sample_f.grid(row=5, column=1, columnspan=2, sticky='w', **_sp)
            segments_var = tk.StringVar(value='3')
            seg_spin = tk.Spinbox(sample_f, textvariable=segments_var, from_=1, to=20,
                        width=3)
            seg_spin.pack(side='left')
            ttk.Label(sample_f, text="× ").pack(side='left')
            sample_len_var = tk.StringVar(value='5')
            len_spin = tk.Spinbox(sample_f, textvariable=sample_len_var, from_=1, to=30,
                        width=3)
            len_spin.pack(side='left')
            ttk.Label(sample_f, text="min each",
                      foreground='gray', font=('Helvetica', 8)).pack(side='left', padx=4)

            # ── Offset adjustment ──
            ttk.Label(f, text="Fine-tune:").grid(row=6, column=0, sticky='w', **_sp)
            finetune_f = ttk.Frame(f)
            finetune_f.grid(row=6, column=1, columnspan=2, sticky='w', **_sp)
            finetune_var = tk.StringVar(value='400')
            tk.Spinbox(finetune_f, textvariable=finetune_var, from_=-2000, to=2000,
                       increment=50, width=6).pack(side='left')
            finetune_hint = ttk.Label(finetune_f,
                      text="ms  (applied after sync — compensates for Whisper timing)",
                      foreground='gray', font=('Helvetica', 8))
            finetune_hint.pack(side='left', padx=4)

            # ── Progress ──
            status_var = tk.StringVar(value="Ready — click Start to begin")
            ttk.Label(f, textvariable=status_var, wraplength=450,
                      font=('Helvetica', 9)).grid(row=7, column=0, columnspan=3, sticky='w', **_sp)

            progress_var = tk.DoubleVar(value=0)
            ttk.Progressbar(f, variable=progress_var, maximum=100,
                           mode='determinate').grid(row=8, column=0, columnspan=3,
                                                      sticky='ew', **_sp)

            # ── Results ──
            result_frame = ttk.LabelFrame(f, text="Results", padding=6)
            result_frame.grid(row=9, column=0, columnspan=3, sticky='nsew', **_sp)
            result_frame.columnconfigure(0, weight=1)
            result_frame.rowconfigure(0, weight=1)
            f.rowconfigure(9, weight=1)

            result_text = tk.Text(result_frame, height=8, wrap='word',
                                 font=('Courier', 9), state='disabled',
                                 bg='#1e1e1e', fg='#d4d4d4')
            result_text.grid(row=0, column=0, sticky='nsew')
            r_scroll = ttk.Scrollbar(result_frame, orient='vertical', command=result_text.yview)
            r_scroll.grid(row=0, column=1, sticky='ns')
            result_text.configure(yscrollcommand=r_scroll.set)

            def _rlog(msg, color='#d4d4d4'):
                result_text.configure(state='normal')
                result_text.insert('end', msg + '\n')
                result_text.see('end')
                result_text.configure(state='disabled')

            # ── Buttons ──
            btn_f = ttk.Frame(f)
            btn_f.grid(row=10, column=0, columnspan=3, sticky='ew', pady=(8, 0))

            cancel_event = threading.Event()
            sync_result = [None]
            pre_sync_cues = [None]  # snapshot before sync — for repeatable Re-time

            def _start():
                vp = vpath_var.get().strip()
                if not vp or not os.path.isfile(vp):
                    messagebox.showwarning("No Video", "Select a video file.", parent=sd)
                    return

                # Save cues before sync so Re-time/Apply can repeat with different fine-tune
                import copy as _copy
                pre_sync_cues[0] = _copy.deepcopy(cues)

                # ── Engine-aware dependency check ──
                _engine = engine_var.get()
                if _engine == 'whisperx':
                    try:
                        import whisperx
                    except ImportError:
                        if messagebox.askyesno("Missing Package",
                            "WhisperX is not installed.\n\n"
                            "Would you like to install it now?\n"
                            "(Requires PyTorch — downloads ~2GB)",
                            parent=sd):
                            # Run pip install in background thread with progress
                            start_btn.configure(state='disabled')
                            status_var.set("Installing whisperx (downloading ~2GB)...")
                            self.add_log("Installing whisperx...", 'INFO')
                            _rlog("Installing whisperx — this may take several minutes...")
                            # Switch progress bar to indeterminate mode
                            _install_pbar = None
                            for _w in f.winfo_children():
                                if isinstance(_w, ttk.Progressbar):
                                    _install_pbar = _w
                                    break
                            if _install_pbar:
                                _install_pbar.configure(mode='indeterminate')
                                _install_pbar.start(15)

                            def _do_whisperx_install():
                                try:
                                    proc = subprocess.Popen(
                                        [sys.executable, '-m', 'pip', 'install',
                                         '--user', '--break-system-packages',
                                         '--progress-bar', 'off',
                                         'whisperx', 'transformers<4.45'],
                                        stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT,
                                        text=True)
                                    for line in proc.stdout:
                                        line = line.rstrip()
                                        if line:
                                            sd.after(0, lambda l=line:
                                                     status_var.set(l[:80]))
                                            sd.after(0, lambda l=line: _rlog(l))
                                    proc.wait(timeout=600)
                                    if proc.returncode == 0:
                                        sd.after(0, lambda: status_var.set(
                                            "whisperx installed — click Start"))
                                        sd.after(0, lambda: _rlog(
                                            "whisperx installed successfully"))
                                        sd.after(0, lambda: self.add_log(
                                            "whisperx installed successfully",
                                            'SUCCESS'))
                                    else:
                                        sd.after(0, lambda: status_var.set(
                                            "whisperx install failed"))
                                        sd.after(0, lambda: _rlog(
                                            "Install failed — check log above"))
                                except Exception as _e:
                                    sd.after(0, lambda: status_var.set(
                                        f"Install error: {_e}"))
                                    sd.after(0, lambda: _rlog(f"Error: {_e}"))
                                finally:
                                    def _reset_after_install():
                                        start_btn.configure(state='normal')
                                        if _install_pbar:
                                            _install_pbar.stop()
                                            _install_pbar.configure(
                                                mode='determinate')
                                            progress_var.set(0)
                                    sd.after(0, _reset_after_install)

                            import threading as _inst_threading
                            _inst_threading.Thread(
                                target=_do_whisperx_install,
                                daemon=True).start()
                            return  # exit _start(); user clicks Start after install
                        else:
                            return

                start_btn.configure(state='disabled')
                apply_btn.configure(state='disabled')
                cancel_event.clear()
                # Capture Tk variables on main thread before entering background thread
                lang = lang_var.get().strip() or None
                model = model_var.get()
                _scan_mode = scan_mode_var.get()
                if _scan_mode == 'direct':
                    engine_value = 'whisperx-align'
                else:
                    engine_value = engine_var.get()
                is_full_scan = _scan_mode == 'full'
                _seg_str = segments_var.get().strip()
                _len_str = sample_len_var.get().strip()
                n_segs = int(_seg_str) if _seg_str.isdigit() else 3
                s_mins = int(_len_str) if _len_str.isdigit() else 5
                _ft_str = finetune_var.get().strip().lstrip('+')
                finetune_ms = int(_ft_str) if _ft_str.lstrip('-').isdigit() else 400
                if is_full_scan or _scan_mode == 'direct':
                    n_segs = 0  # signal for full scan
                    s_mins = 0

                import time as _sync_time
                _last_ui_update = [0]

                def _progress(msg):
                    # Throttle UI updates to max 4 per second to avoid flooding Tk event queue
                    now = _sync_time.monotonic()
                    is_milestone = ('segment' in msg.lower() and '/' in msg) or \
                                   'Matched' in msg or 'Loading' in msg or \
                                   'Extracting' in msg or 'Transcribed' in msg or \
                                   'Aligning' in msg or 'alignment' in msg.lower() or \
                                   'WhisperX' in msg or 'Falling back' in msg or \
                                   'failed' in msg.lower() or 'complete' in msg.lower() or \
                                   'error' in msg.lower() or 'RESULT' in msg or \
                                   'Done' in msg or '===' in msg or 'Drift' in msg or \
                                   'Sync' in msg
                    if not is_milestone and (now - _last_ui_update[0]) < 0.25:
                        return
                    _last_ui_update[0] = now

                    def _do_update():
                        status_var.set(msg)
                        _rlog(msg)
                        import re as _re
                        m = _re.search(r'segment (\d+)/(\d+)', msg, _re.IGNORECASE)
                        if m:
                            seg_n, seg_t = int(m.group(1)), int(m.group(2))
                            progress_var.set((seg_n / seg_t) * 100)
                        m2 = _re.search(r'Matching cue (\d+)/(\d+)', msg)
                        if m2:
                            mc, mt = int(m2.group(1)), int(m2.group(2))
                            progress_var.set((mc / mt) * 100)
                        elif 'Extracting audio' in msg:
                            progress_var.set(0)
                    sd.after(0, _do_update)

                def _set_start_enabled():
                    start_btn.configure(state='normal')
                def _set_apply_enabled():
                    apply_btn.configure(state='normal')
                    progress_var.set(100)

                def _run():
                    try:
                        result = smart_sync(vp, cues, model_size=model,
                                            language=lang,
                                            num_segments=n_segs,
                                            sample_minutes=s_mins,
                                            progress_callback=_progress,
                                            cancel_event=cancel_event,
                                            engine=engine_value)
                    except Exception as _e:
                        _progress(f"Error: {_e}")
                        result = None
                    sync_result[0] = result

                    # Display results via _progress (proven reliable)
                    import time as _t
                    _t.sleep(0.3)  # let queued UI updates flush

                    if cancel_event.is_set():
                        _progress("Sync cancelled by user")
                    elif result:
                        try:
                            ro = result['offset_ms']
                            rd = result['drift_ms']
                            rm = result['matches']
                            sign = '+' if ro > 0 else ''
                            _progress(f"{'='*40}")
                            _progress(f"RESULT: Offset: {sign}{ro}ms ({sign}{ro/1000:.1f}s)")
                            _progress(f"Drift: {rd:+d}ms")
                            _progress(f"Matched: {len(rm)}/{len(cues)} cues")
                            _progress(f"{'='*40}")
                            for ci, wt, ct, sim, txt in rm[:10]:
                                _progress(f"  #{ci+1} sim={sim:.0%} "
                                          f"sub={ms_to_srt_ts(ct)[:8]} "
                                          f"audio={ms_to_srt_ts(wt)[:8]} "
                                          f"\"{txt}\"")
                            _progress(f"Done — click Apply Sync to apply {sign}{ro}ms offset")
                            sd.after(0, lambda: apply_btn.configure(state='normal'))
                            sd.after(0, lambda: retime_btn.configure(state='normal'))
                            sd.after(0, lambda: progress_var.set(100))
                        except Exception as _e:
                            _progress(f"Error displaying results: {_e}")
                    else:
                        _progress("Sync failed — no results")

                    sd.after(0, _set_start_enabled)

                t = threading.Thread(target=_run, daemon=True)
                t.start()

            def _get_finetune():
                _ft = finetune_var.get().strip().lstrip('+')
                return int(_ft) if _ft.lstrip('-').isdigit() else 400

            def _do_backup():
                backup_path = None
                if current_path[0] and os.path.isfile(current_path[0]):
                    base, ext = os.path.splitext(current_path[0])
                    backup_path = f"{base}_presync{ext}"
                    try:
                        write_srt_file(cues, backup_path)
                        _rlog(f"Backup saved: {os.path.basename(backup_path)}")
                        self.add_log(f"Pre-sync backup: {backup_path}", 'INFO')
                    except Exception as e:
                        _rlog(f"Warning: could not save backup: {e}")
                return backup_path

            def _apply():
                nonlocal cues
                if not sync_result[0]:
                    return
                offset = sync_result[0]['offset_ms']
                ft = _get_finetune()
                total_offset = offset + ft

                backup_path = _do_backup()

                # Always apply from the pre-sync snapshot so fine-tune is repeatable
                import copy as _copy
                push_undo()
                if pre_sync_cues[0] is not None:
                    cues = _copy.deepcopy(pre_sync_cues[0])
                cues = shift_timestamps(cues, total_offset)
                refresh_tree(cues)
                sign = '+' if total_offset > 0 else ''
                self.add_log(f"Smart Sync applied: {sign}{total_offset}ms "
                             f"(offset {offset:+d}ms + fine-tune {ft:+d}ms)", 'SUCCESS')
                _rlog(f"\nApplied: {sign}{total_offset}ms (offset {offset:+d} + fine-tune {ft:+d})")
                if backup_path:
                    _rlog(f"Original saved as: {os.path.basename(backup_path)}")
                status_var.set(f"Sync applied: {sign}{total_offset}ms")

            def _retime():
                nonlocal cues
                if not sync_result[0]:
                    return
                result = sync_result[0]
                matched = result['matches']
                ft = _get_finetune()

                backup_path = _do_backup()

                # Always retime from the pre-sync snapshot so fine-tune is repeatable
                import copy as _copy
                push_undo()
                if pre_sync_cues[0] is not None:
                    cues = _copy.deepcopy(pre_sync_cues[0])
                cues = retime_subtitles(cues, matched)
                # Apply fine-tune offset after re-timing
                if ft != 0:
                    cues = shift_timestamps(cues, ft)
                refresh_tree(cues)
                ft_msg = f" + fine-tune {ft:+d}ms" if ft else ""
                self.add_log(f"Re-timed {len(cues)} cues using {len(matched)} anchors{ft_msg}",
                             'SUCCESS')
                _rlog(f"\nRe-timed {len(cues)} cues using {len(matched)} anchors{ft_msg}")
                if backup_path:
                    _rlog(f"Original saved as: {os.path.basename(backup_path)}")
                status_var.set(f"Re-timed using {len(matched)} anchors{ft_msg}")

            def _cancel():
                cancel_event.set()
                status_var.set("Cancelling...")

            start_btn = ttk.Button(btn_f, text="▶ Start", command=_start, width=8)
            start_btn.pack(side='left', padx=2)
            apply_btn = ttk.Button(btn_f, text="Apply Sync", command=_apply,
                                    width=10, state='disabled')
            apply_btn.pack(side='left', padx=2)
            retime_btn = ttk.Button(btn_f, text="Re-time All", command=_retime,
                                     width=10, state='disabled')
            retime_btn.pack(side='left', padx=2)
            ttk.Button(btn_f, text="Cancel", command=_cancel, width=8).pack(side='left', padx=2)

            def _save_from_sync():
                do_save_file()
                _rlog("Saved.")
                status_var.set("Saved")

            ttk.Button(btn_f, text="💾 Save", command=_save_from_sync,
                       width=6).pack(side='right', padx=2)
            ttk.Button(btn_f, text="Close", command=sd.destroy, width=6).pack(side='right', padx=2)

        # ══════════════════════════════════════════════════════════════════════
        # Placeholder — shown when no file is loaded
        # ══════════════════════════════════════════════════════════════════════
        placeholder = ttk.Frame(editor)
        placeholder.pack(fill='both', expand=True)
        ph_label = ttk.Label(placeholder,
                             text="Open a subtitle file to begin editing\n\n"
                                  "File → Open   (Ctrl+O)\n\n"
                                  "or drag and drop a subtitle or video file here",
                             font=('Helvetica', 14),
                             foreground='gray',
                             justify='center',
                             anchor='center')
        ph_label.pack(expand=True)

        # ══════════════════════════════════════════════════════════════════════
        # Content frame — hidden until a file is loaded
        # ══════════════════════════════════════════════════════════════════════
        content_frame = ttk.Frame(editor)

        # ── Search & Replace toolbar ──
        find_var = tk.StringVar()
        replace_var = tk.StringVar()
        use_regex = tk.BooleanVar(value=False)
        wrap_around = tk.BooleanVar(value=False)

        def do_find():
            term = find_var.get()
            if not term:
                refresh_tree(cues)
                return
            matches = []
            for i, cue in enumerate(cues):
                try:
                    if use_regex.get():
                        if re.search(term, cue['text'], re.IGNORECASE):
                            matches.append(i)
                    else:
                        if term.lower() in cue['text'].lower():
                            matches.append(i)
                except re.error:
                    pass
            refresh_tree(cues, search_indices=matches)
            if matches:
                first_idx = matches[0]
                first = str(first_idx)
                def _scroll_to_match():
                    tree.selection_set(first)
                    # Scroll so the match is near the middle of the view, not at the edge
                    # Aim a few rows past the match so it's comfortably visible
                    ahead = min(first_idx + 5, len(cues) - 1)
                    tree.see(str(ahead))
                    tree.after(50, lambda: (tree.see(first), tree.selection_set(first)))
                tree.after_idle(_scroll_to_match)
            self.add_log(f"Search: {len(matches)} matches for '{term}'", 'INFO')

        def do_replace_one():
            """Replace the first occurrence of search term from current selection."""
            nonlocal cues
            term = find_var.get()
            repl = replace_var.get()
            if not term:
                return
            sel = tree.selection()
            start_idx = int(sel[0]) if sel else 0
            if wrap_around.get():
                order = list(range(start_idx, len(cues))) + list(range(0, start_idx))
            else:
                order = list(range(start_idx, len(cues)))
            for i in order:
                old_text = cues[i]['text']
                try:
                    if use_regex.get():
                        new_text = re.sub(term, lambda m: repl, old_text, count=1,
                                          flags=re.IGNORECASE)
                    else:
                        # Case-insensitive literal find + replace (preserves rest of line)
                        pos = old_text.lower().find(term.lower())
                        if pos >= 0:
                            new_text = old_text[:pos] + repl + old_text[pos + len(term):]
                        else:
                            new_text = old_text
                except re.error:
                    continue
                if new_text != old_text:
                    push_undo()
                    cues[i]['text'] = new_text
                    if not new_text.strip():
                        del cues[i]
                    refresh_tree(cues)
                    # Select and scroll to the next match
                    if wrap_around.get():
                        next_order = list(range(i + 1, len(cues))) + list(range(0, i + 1))
                    else:
                        next_order = list(range(i + 1, len(cues)))
                    for j in next_order:
                        try:
                            if use_regex.get():
                                if re.search(term, cues[j]['text'], re.IGNORECASE):
                                    tree.see(str(j))
                                    tree.selection_set(str(j))
                                    break
                            else:
                                if term.lower() in cues[j]['text'].lower():
                                    tree.see(str(j))
                                    tree.selection_set(str(j))
                                    break
                        except (re.error, IndexError):
                            pass
                    self.add_log(f"Replaced 1 occurrence of '{term}' → '{repl}'", 'INFO')
                    return
            self.add_log(f"No more matches found for '{term}'", 'INFO')

        def do_replace_all():
            nonlocal cues
            term = find_var.get()
            repl = replace_var.get()
            if not term:
                return
            push_undo()
            count = 0
            for cue in cues:
                old_text = cue['text']
                try:
                    if use_regex.get():
                        new_text = re.sub(term, lambda m: repl, old_text, flags=re.IGNORECASE)
                    else:
                        # Case-insensitive literal replace all
                        new_text = old_text
                        lower_text = new_text.lower()
                        lower_term = term.lower()
                        result = []
                        pos = 0
                        while True:
                            idx = lower_text.find(lower_term, pos)
                            if idx == -1:
                                result.append(new_text[pos:])
                                break
                            result.append(new_text[pos:idx])
                            result.append(repl)
                            pos = idx + len(term)
                        new_text = ''.join(result)
                except re.error:
                    continue
                if new_text != old_text:
                    cue['text'] = new_text
                    count += 1
            cues = [c for c in cues if c['text'].strip()]
            refresh_tree(cues)
            self.add_log(f"Replaced {count} occurrence(s) of '{term}' → '{repl}'", 'INFO')

        search_frame = ttk.Frame(content_frame, padding=(10, 4, 10, 4))
        search_frame.pack(fill='x')

        def _add_entry_context_menu(entry):
            """Attach a right-click Cut/Copy/Paste menu to a ttk.Entry."""
            menu = tk.Menu(entry, tearoff=0)
            menu.add_command(label="Cut",
                command=lambda: entry.event_generate('<<Cut>>'))
            menu.add_command(label="Copy",
                command=lambda: entry.event_generate('<<Copy>>'))
            menu.add_command(label="Paste",
                command=lambda: entry.event_generate('<<Paste>>'))
            menu.add_separator()
            menu.add_command(label="Select All",
                command=lambda: (entry.select_range(0, 'end'),
                                 entry.icursor('end')))
            def _show(event):
                menu.tk_popup(event.x_root, event.y_root)
            entry.bind('<Button-3>', _show)

        ttk.Label(search_frame, text="Find:").pack(side='left')
        find_entry = ttk.Entry(search_frame, textvariable=find_var, width=20)
        find_entry.pack(side='left', padx=(2, 6))
        find_entry.bind('<Return>', lambda e: do_find())
        _add_entry_context_menu(find_entry)

        ttk.Label(search_frame, text="Replace:").pack(side='left')
        replace_entry = ttk.Entry(search_frame, textvariable=replace_var, width=20)
        replace_entry.pack(side='left', padx=(2, 6))
        _add_entry_context_menu(replace_entry)

        ttk.Button(search_frame, text="Find", command=do_find).pack(side='left', padx=2)
        ttk.Button(search_frame, text="Replace",
                   command=do_replace_one).pack(side='left', padx=2)
        ttk.Button(search_frame, text="Replace All",
                   command=do_replace_all).pack(side='left', padx=2)
        ttk.Checkbutton(search_frame, text="Wrap",
                        variable=wrap_around).pack(side='left', padx=(6, 2))

        editor.bind('<Control-f>', lambda e: find_entry.focus_set())
        editor.bind('<Control-F>', lambda e: find_entry.focus_set())

        ttk.Separator(content_frame, orient='horizontal').pack(fill='x')

        # ── Treeview ──
        tree_frame = ttk.Frame(content_frame)
        tree_frame.pack(fill='both', expand=True, padx=10, pady=(4, 0))

        tree_scroll_y = ttk.Scrollbar(tree_frame, orient='vertical')
        tree_scroll_y.pack(side='right', fill='y')

        tree = ttk.Treeview(tree_frame, columns=('num', 'time', 'text'),
                            show='headings', yscrollcommand=tree_scroll_y.set,
                            selectmode='extended')
        tree_scroll_y.config(command=tree.yview)

        tree.heading('num', text='#')
        tree.heading('time', text='Timestamp')
        tree.heading('text', text='Text')
        tree.column('num', width=40, minwidth=30, stretch=False)
        tree.column('time', width=260, minwidth=220, stretch=False)
        tree.column('text', width=500, minwidth=200, stretch=True)
        tree.pack(fill='both', expand=True)

        # Color coding
        tree.tag_configure(TAG_MODIFIED, background='#fff3cd')
        tree.tag_configure(TAG_HI, background='#cce5ff')
        tree.tag_configure(TAG_TAGS, background='#f8d7da')
        tree.tag_configure(TAG_LONG, background='#ffe0b2')
        tree.tag_configure(TAG_SEARCH, background='#c8e6c9')
        tree.tag_configure(TAG_SPELL, background='#f5c6cb')

        # Mousewheel scrolling
        def on_tree_mousewheel(event):
            tree.yview_scroll(int(-1 * (event.delta / 120)), 'units')
            return 'break'

        def on_tree_scroll_up(event):
            tree.yview_scroll(-3, 'units')
            return 'break'

        def on_tree_scroll_down(event):
            tree.yview_scroll(3, 'units')
            return 'break'

        tree.bind('<MouseWheel>', on_tree_mousewheel)
        tree.bind('<Button-4>', on_tree_scroll_up)
        tree.bind('<Button-5>', on_tree_scroll_down)

        # ── Inline edit on double-click ──
        edit_entry = None

        def on_double_click(event):
            nonlocal edit_entry, cues
            item = tree.identify_row(event.y)
            col = tree.identify_column(event.x)
            if not item or col != '#3':
                return
            bbox = tree.bbox(item, col)
            if not bbox:
                return
            x, y, w, h = bbox
            idx = int(item)

            if edit_entry:
                edit_entry.destroy()

            push_undo()
            edit_entry = tk.Text(tree_frame, wrap='word', height=3)
            edit_entry.place(x=x, y=y, width=w, height=max(h, 60))
            edit_entry.insert('1.0', cues[idx]['text'])
            edit_entry.focus_set()
            edit_entry.tag_configure('sel', background='#4a90d9')

            def save_edit(e=None):
                nonlocal edit_entry
                new_text = edit_entry.get('1.0', 'end-1c').strip()
                if new_text:
                    cues[idx]['text'] = new_text
                    display = new_text.replace('\n', ' \\n ')
                    tree.set(item, 'text', display)
                    orig_text = original_cues[idx]['text'] if idx < len(original_cues) else None
                    ctags = _classify_cue(cues[idx], orig_text)
                    row_tag = ''
                    for t in (TAG_MODIFIED, TAG_HI, TAG_TAGS, TAG_LONG):
                        if t in ctags:
                            row_tag = t
                            break
                    tree.item(item, tags=(row_tag,) if row_tag else ())
                else:
                    del cues[idx]
                    refresh_tree(cues)
                edit_entry.destroy()
                edit_entry = None
                # Update stats
                deleted_count.set(len(original_cues) - len(cues))
                mod = sum(1 for i, c in enumerate(cues) if i < len(original_cues)
                          and c['text'] != original_cues[i]['text'])
                modified_count.set(mod)
                long_count = sum(1 for c in cues
                                 if any(len(l) > MAX_CHARS_PER_LINE
                                        for l in c['text'].split('\n')))
                stats_parts = [
                    f"{len(cues)} entries",
                    f"{modified_count.get()} modified",
                    f"{deleted_count.get()} removed",
                ]
                if long_count:
                    stats_parts.append(f"{long_count} long lines")
                stats_label.configure(text=" │ ".join(stats_parts))

            def cancel_edit(e=None):
                nonlocal edit_entry
                if edit_entry:
                    edit_entry.destroy()
                    edit_entry = None
                    if undo_stack:
                        undo_stack.pop()

            edit_entry.bind('<Escape>', cancel_edit)
            edit_entry.bind('<Control-Return>', save_edit)
            edit_entry.bind('<Tab>', save_edit)

            # Right-click context menu for copy/paste
            edit_ctx = tk.Menu(edit_entry, tearoff=0)

            def _edit_action(action):
                """Perform an edit action and refocus the edit widget."""
                if not edit_entry:
                    return
                if action == 'cut':
                    edit_entry.event_generate('<<Cut>>')
                elif action == 'copy':
                    edit_entry.event_generate('<<Copy>>')
                elif action == 'paste':
                    edit_entry.event_generate('<<Paste>>')
                elif action == 'select_all':
                    edit_entry.tag_add('sel', '1.0', 'end')
                    edit_entry.mark_set('insert', 'end')
                edit_entry.focus_force()

            edit_ctx.add_command(label="Cut", command=lambda: _edit_action('cut'))
            edit_ctx.add_command(label="Copy", command=lambda: _edit_action('copy'))
            edit_ctx.add_command(label="Paste", command=lambda: _edit_action('paste'))
            edit_ctx.add_separator()
            edit_ctx.add_command(label="Select All",
                                command=lambda: _edit_action('select_all'))

            _edit_ctx_open = [False]

            def show_edit_ctx(event):
                _edit_ctx_open[0] = True
                def on_menu_close():
                    _edit_ctx_open[0] = False
                    if edit_entry:
                        edit_entry.focus_force()
                edit_ctx.tk_popup(event.x_root, event.y_root)
                # tk_popup is blocking on some platforms; schedule cleanup
                edit_entry.after(50, on_menu_close)
                return 'break'
            edit_entry.bind('<Button-3>', show_edit_ctx)

            def on_focus_out(e):
                if not edit_entry:
                    return
                # Wait for context menu interactions to complete
                def deferred_save():
                    if not edit_entry:
                        return
                    if _edit_ctx_open[0]:
                        # Menu still active, check again later
                        edit_entry.after(200, deferred_save)
                        return
                    try:
                        if edit_entry.focus_get() == edit_entry:
                            return  # focus came back, don't save
                    except Exception:
                        pass
                    save_edit()
                edit_entry.after(300, deferred_save)
            edit_entry.bind('<FocusOut>', on_focus_out)

        tree.bind('<Double-1>', on_double_click)

        # ── Right-click context menu ──
        ctx_menu = tk.Menu(editor, tearoff=0)
        def insert_cue(position):
            """Insert a blank cue above or below the selected cue."""
            nonlocal cues
            selected = tree.selection()
            if not selected:
                return
            idx = int(selected[0])
            ref = cues[idx]

            if position == 'above':
                # Place the new cue just before the selected one
                ref_start_ms = srt_ts_to_ms(ref['start'])
                new_end_ms = max(ref_start_ms - 1, 0)
                new_start_ms = max(new_end_ms - 2000, 0)
                insert_idx = idx
            else:
                # Place the new cue just after the selected one
                ref_end_ms = srt_ts_to_ms(ref['end'])
                new_start_ms = ref_end_ms + 1
                new_end_ms = new_start_ms + 2000
                insert_idx = idx + 1

            push_undo()
            new_cue = {
                'index': 0,
                'start': ms_to_srt_ts(new_start_ms),
                'end': ms_to_srt_ts(new_end_ms),
                'text': ' ',
            }
            cues.insert(insert_idx, new_cue)
            refresh_tree(cues)
            # Select the new cue and scroll to it
            tree.see(str(insert_idx))
            tree.selection_set(str(insert_idx))

        ctx_menu.add_command(label="✂ Split cue", command=split_selected)
        ctx_menu.add_command(label="⊕ Join selected cues", command=join_selected)
        ctx_menu.add_separator()
        ctx_menu.add_command(label="⤒ Insert line above", command=lambda: insert_cue('above'))
        ctx_menu.add_command(label="⤓ Insert line below", command=lambda: insert_cue('below'))
        ctx_menu.add_separator()
        ctx_menu.add_command(label="🗑 Delete selected", command=delete_selected)

        def show_context_menu(event):
            item = tree.identify_row(event.y)
            if item and item not in tree.selection():
                tree.selection_set(item)
            ctx_menu.tk_popup(event.x_root, event.y_root)

        tree.bind('<Button-3>', show_context_menu)

        # ── Status bar ──
        status_frame = ttk.Frame(content_frame, padding=(10, 6, 10, 6))
        status_frame.pack(fill='x')

        stats_label = ttk.Label(status_frame, text="0 entries")
        stats_label.pack(side='left')

        ttk.Button(status_frame, text="💾 Save", command=do_save_file).pack(side='right', padx=(4, 0))
        ttk.Button(status_frame, text="📤 Export SRT", command=do_export).pack(side='right', padx=4)

        # ── Refresh tree function ──
        def refresh_tree(new_cues, search_indices=None):
            nonlocal cues
            cues = new_cues
            tree.delete(*tree.get_children())
            search_set = set(search_indices or [])
            for i, cue in enumerate(cues):
                display = cue['text'].replace('\n', ' \\n ')
                ts = f"{cue['start']} → {cue['end']}"
                if cue['text'] in _orig_texts:
                    orig_text = cue['text']
                else:
                    orig_text = ''
                ctags = _classify_cue(cue, orig_text)
                if i in search_set:
                    ctags.add(TAG_SEARCH)
                if TAG_SEARCH in ctags:
                    row_tag = TAG_SEARCH
                elif i in spell_error_indices:
                    row_tag = TAG_SPELL
                elif TAG_MODIFIED in ctags:
                    row_tag = TAG_MODIFIED
                elif TAG_HI in ctags:
                    row_tag = TAG_HI
                elif TAG_TAGS in ctags:
                    row_tag = TAG_TAGS
                elif TAG_LONG in ctags:
                    row_tag = TAG_LONG
                else:
                    row_tag = ''
                tree.insert('', 'end', iid=str(i),
                            values=(i + 1, ts, display),
                            tags=(row_tag,) if row_tag else ())
            deleted_count.set(len(original_cues) - len(cues))
            mod = sum(1 for i, c in enumerate(cues) if i < len(original_cues)
                      and c['text'] != original_cues[i]['text'])
            modified_count.set(mod)
            long_count = sum(1 for c in cues
                             if any(len(l) > MAX_CHARS_PER_LINE for l in c['text'].split('\n')))
            stats_parts = [
                f"{len(cues)} entries",
                f"{modified_count.get()} modified",
                f"{deleted_count.get()} removed",
            ]
            if long_count:
                stats_parts.append(f"{long_count} long lines")
            stats_label.configure(text=" │ ".join(stats_parts))

        # Delete key shortcut
        editor.bind('<Delete>', lambda e: None if isinstance(e.widget, tk.Text) else delete_selected())

        # ── Disable menus until a file is loaded ──
        def _set_menus_state(state):
            for menu_label in ('Filters', 'Edit', 'Timing'):
                try:
                    idx = menubar.index(menu_label)
                    menubar.entryconfigure(idx, state=state)
                except (tk.TclError, ValueError):
                    pass
            # Disable save/export in File menu (indices 2=Save, 3=Save As, 4=Export)
            for i in (2, 3, 4):
                try:
                    file_menu.entryconfigure(i, state=state)
                except tk.TclError:
                    pass

        _set_menus_state('disabled')

        # ── Cleanup temp files on editor close ──
        def on_editor_close():
            if video_source[0] and video_source[0].get('temp_srt'):
                try:
                    os.unlink(video_source[0]['temp_srt'])
                except OSError:
                    pass
            editor.destroy()

        editor.protocol('WM_DELETE_WINDOW', on_editor_close)

        editor.wait_window()

    # ── Media Processor ──────────────────────────────────────────────────────

    def open_media_processor(self):
        """Open the standalone Media Processor window for remux-only post-processing."""
        import time as _time
        import tempfile
        import threading
        from concurrent.futures import ThreadPoolExecutor, as_completed

        win = tk.Toplevel(self.root)
        win.title("🔧 Media Processor")
        win.geometry("920x1080")
        win.minsize(750, 850)
        self._center_on_main(win)

        # ── State ──
        mp_files = []        # list of dicts
        mp_processing = [False]
        mp_stop = [False]
        mp_lock = threading.Lock()  # protects mp_files during parallel access

        # ── Options ──
        # Map display names → ffmpeg codec names (subset for remux use)
        mp_audio_codec_map = {
            'aac': 'aac',
            'ac3 (Dolby Digital)': 'ac3',
            'eac3 (Dolby Digital+)': 'eac3',
            'mp3': 'mp3',
            'opus': 'opus',
            'flac': 'flac',
            'copy (no re-encode)': 'copy',
        }
        mp_audio_codec_reverse = {v: k for k, v in mp_audio_codec_map.items()}

        # Load saved Media Processor preferences (empty dict on fresh install)
        _mp = getattr(self, '_media_proc_prefs', {})

        opt_convert_audio  = tk.BooleanVar(value=_mp.get('convert_audio', False))
        opt_audio_codec    = tk.StringVar(value=_mp.get('audio_codec', 'ac3 (Dolby Digital)'))
        opt_audio_bitrate  = tk.StringVar(value=_mp.get('audio_bitrate', '384k'))
        opt_strip_chapters = tk.BooleanVar(value=_mp.get('strip_chapters', False))
        opt_strip_tags     = tk.BooleanVar(value=_mp.get('strip_tags', False))
        opt_strip_subs     = tk.BooleanVar(value=_mp.get('strip_subs', False))
        opt_set_metadata   = tk.BooleanVar(value=_mp.get('set_metadata', False))
        opt_meta_video     = tk.StringVar(value=_mp.get('meta_video', 'und'))
        opt_meta_audio     = tk.StringVar(value=_mp.get('meta_audio', 'eng'))
        opt_meta_sub       = tk.StringVar(value=_mp.get('meta_sub', 'eng'))
        opt_mux_subs       = tk.BooleanVar(value=_mp.get('mux_subs', False))
        opt_sub_lang       = tk.StringVar(value=_mp.get('sub_lang', 'eng'))
        opt_output_mode    = tk.StringVar(value=_mp.get('output_mode', 'inplace'))
        opt_output_folder  = tk.StringVar(value=_mp.get('output_folder', ''))
        opt_container      = tk.StringVar(value=_mp.get('container', '.mkv'))
        opt_parallel       = tk.BooleanVar(value=_mp.get('parallel', False))
        try:
            _cpu_count = os.cpu_count() or 4
        except Exception:
            _cpu_count = 4
        opt_max_jobs       = tk.IntVar(value=_mp.get('max_jobs', min(_cpu_count, 8)))
        opt_edition_tag    = tk.StringVar(value=_mp.get('edition_tag', ''))
        opt_edition_fn     = tk.BooleanVar(value=_mp.get('edition_in_filename', False))
        _edition_custom_sv = tk.StringVar(value='')
        opt_add_chapters   = tk.BooleanVar(value=_mp.get('add_chapters', False))
        opt_ch_interval    = tk.IntVar(value=_mp.get('chapter_interval', 5))

        # ── Layout ──
        main_frame = ttk.Frame(win, padding=10)
        main_frame.pack(fill='both', expand=True)
        main_frame.columnconfigure(0, weight=1)
        main_frame.rowconfigure(1, weight=1)   # file list
        main_frame.rowconfigure(4, weight=1)   # log

        # ── Toolbar ──
        toolbar = ttk.Frame(main_frame)
        toolbar.grid(row=0, column=0, sticky='ew', pady=(0, 6))

        def _add_files():
            paths = filedialog.askopenfilenames(
                title="Select Video Files",
                filetypes=[("Video files", "*.mkv *.mp4 *.avi *.mov *.wmv *.flv *.webm *.ts *.m2ts *.mts"),
                           ("All files", "*.*")])
            for p in paths:
                _add_one_file(p)
            _rebuild_tree()

        def _add_folder():
            folder = self._ask_directory(title="Select Folder with Video Files")
            if not folder:
                return
            count = 0
            for ext in VIDEO_EXTENSIONS:
                for fp in Path(folder).glob(f'*{ext}'):
                    if fp.is_file() and not fp.name.startswith('.'):
                        _add_one_file(str(fp))
                        count += 1
            _rebuild_tree()
            _log(f"Scanned folder: {count} video file(s) found", 'INFO')

        def _detect_ext_subs(filepath):
            """Detect matching subtitle files alongside a video file.
            Uses the configured subtitle language code."""
            base = os.path.splitext(filepath)[0]
            lang = opt_sub_lang.get().strip() or 'eng'
            ext_subs_found = []
            # Try language-specific patterns first
            main_srt = f"{base}.{lang}.srt"
            forced_srt = f"{base}.{lang}.forced.srt"
            # Fallback: bare .srt (no language code)
            bare_srt = f"{base}.srt"
            if os.path.isfile(main_srt):
                ext_subs_found.append(('main', main_srt))
            elif os.path.isfile(bare_srt):
                ext_subs_found.append(('main', bare_srt))
            if os.path.isfile(forced_srt):
                ext_subs_found.append(('forced', forced_srt))
            return ext_subs_found

        def _add_one_file(filepath):
            # Skip duplicates
            for f in mp_files:
                if f['path'] == filepath:
                    return
            name = os.path.basename(filepath)
            size = os.path.getsize(filepath)
            audio = get_audio_info(filepath)
            subs = get_subtitle_streams(filepath)
            ext_subs_found = _detect_ext_subs(filepath)
            # Audio codec display
            if audio:
                acodec = audio[0]['codec_name'].upper()
                if acodec in ('AC3', 'EAC3'):
                    acodec += ' ✓'
            else:
                acodec = '—'
            mp_files.append({
                'path': filepath,
                'name': name,
                'size': size,
                'audio_info': audio,
                'audio_display': acodec,
                'sub_count': len(subs),
                'ext_subs': ext_subs_found,
                'status': 'Ready',
                'overrides': {},  # per-file operation overrides
            })

        def _clear_files():
            mp_files.clear()
            _rebuild_tree()

        def _rescan_subs():
            """Re-detect external subtitle files for all files using current language setting."""
            for f in mp_files:
                f['ext_subs'] = _detect_ext_subs(f['path'])
            _rebuild_tree()
            _log(f"Re-scanned subtitles with language code: {opt_sub_lang.get()}", 'INFO')

        ttk.Button(toolbar, text="📂 Add Files...", command=_add_files).pack(side='left', padx=2)
        ttk.Button(toolbar, text="📁 Add Folder...", command=_add_folder).pack(side='left', padx=2)
        ttk.Button(toolbar, text="🗑️ Clear", command=_clear_files).pack(side='left', padx=2)

        # ── File list (Treeview) ──
        tree_frame = ttk.LabelFrame(main_frame, text="Files", padding=5)
        tree_frame.grid(row=1, column=0, sticky='nsew', pady=(0, 6))
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)

        columns = ('name', 'audio', 'subs', 'ext_subs', 'size', 'status')
        tree = ttk.Treeview(tree_frame, columns=columns, show='headings', height=6)
        tree.grid(row=0, column=0, sticky='nsew')

        tree.heading('name',     text='Filename')
        tree.heading('audio',    text='Audio')
        tree.heading('subs',     text='Int Subs')
        tree.heading('ext_subs', text='Ext Subs')
        tree.heading('size',     text='Size')
        tree.heading('status',   text='Status')

        tree.column('name',     width=280, minwidth=150)
        tree.column('audio',    width=80,  minwidth=60,  anchor='center')
        tree.column('subs',     width=60,  minwidth=40,  anchor='center')
        tree.column('ext_subs', width=80,  minwidth=60,  anchor='center')
        tree.column('size',     width=80,  minwidth=60,  anchor='e')
        tree.column('status',   width=120, minwidth=80,  anchor='center')

        scrollbar = ttk.Scrollbar(tree_frame, orient='vertical', command=tree.yview)
        scrollbar.grid(row=0, column=1, sticky='ns')
        tree.configure(yscrollcommand=scrollbar.set)

        def _rebuild_tree():
            tree.delete(*tree.get_children())
            for f in mp_files:
                size_mb = f'{f["size"] / (1024*1024):.1f} MB'
                ext_str = ', '.join(t for t, _ in f['ext_subs']) if f['ext_subs'] else '—'
                name_display = f['name']
                if f.get('overrides'):
                    name_display = '⚙️ ' + name_display
                tree.insert('', 'end', values=(
                    name_display, f['audio_display'], f['sub_count'],
                    ext_str, size_mb, f['status']
                ))

        def _update_tree_status(index, status):
            items = tree.get_children()
            if index < len(items):
                mp_files[index]['status'] = status
                tree.set(items[index], 'status', status)

        def _update_tree_row(index):
            """Refresh a single row's display data (after re-probe)."""
            items = tree.get_children()
            if index < len(items):
                f = mp_files[index]
                size_mb = f'{f["size"] / (1024*1024):.1f} MB'
                ext_str = ', '.join(t for t, _ in f['ext_subs']) if f['ext_subs'] else '—'
                name_display = f['name']
                if f.get('overrides'):
                    name_display = '⚙️ ' + name_display
                tree.set(items[index], 'name', name_display)
                tree.set(items[index], 'audio', f['audio_display'])
                tree.set(items[index], 'subs', f['sub_count'])
                tree.set(items[index], 'ext_subs', ext_str)
                tree.set(items[index], 'size', size_mb)

        # ── Delete key to remove selected ──
        def _on_delete(evt):
            sel = tree.selection()
            if not sel or mp_processing[0]:
                return
            items = tree.get_children()
            indices = sorted([list(items).index(s) for s in sel], reverse=True)
            for idx in indices:
                del mp_files[idx]
            _rebuild_tree()
        tree.bind('<Delete>', _on_delete)

        # ── Right-click context menu (per-file overrides + subtitle management) ──
        def _show_context_menu(event):
            item = tree.identify_row(event.y)
            if not item or mp_processing[0]:
                return
            tree.selection_set(item)
            items = tree.get_children()
            index = list(items).index(item)

            ctx = tk.Menu(win, tearoff=0)
            ctx.add_command(label="⚙️ Override Settings...",
                           command=lambda: _show_file_override(index))
            ctx.add_command(label="📎 Manage Subtitles...",
                           command=lambda: _show_sub_manager(index))
            ctx.add_separator()
            ctx.add_command(label="🔄 Re-probe File",
                           command=lambda: _reprobe_file(index))
            ctx.add_command(label="❌ Clear Override",
                           command=lambda: _clear_override(index))
            ctx.add_separator()
            ctx.add_command(label="🗑️ Remove from List",
                           command=lambda: _remove_file(index))
            ctx.tk_popup(event.x_root, event.y_root)

        tree.bind('<Button-3>', _show_context_menu)

        def _remove_file(index):
            del mp_files[index]
            _rebuild_tree()

        def _clear_override(index):
            mp_files[index]['overrides'] = {}
            _rebuild_tree()
            _log(f"Cleared overrides for: {mp_files[index]['name']}", 'INFO')

        def _reprobe_file(index):
            """Re-probe a file's audio and subtitle info and update the tree."""
            f = mp_files[index]
            audio = get_audio_info(f['path'])
            subs = get_subtitle_streams(f['path'])
            f['audio_info'] = audio
            f['sub_count'] = len(subs)
            f['size'] = os.path.getsize(f['path']) if os.path.exists(f['path']) else 0
            f['ext_subs'] = _detect_ext_subs(f['path'])
            if audio:
                acodec = audio[0]['codec_name'].upper()
                if acodec in ('AC3', 'EAC3'):
                    acodec += ' ✓'
                f['audio_display'] = acodec
            else:
                f['audio_display'] = '—'
            win.after(0, lambda: _update_tree_row(index))
            _log(f"Re-probed: {f['name']} — audio={f['audio_display']}, subs={f['sub_count']}", 'INFO')

        def _show_file_override(index):
            """Show per-file override dialog."""
            f = mp_files[index]
            ov = f.get('overrides', {})

            dlg = tk.Toplevel(win)
            dlg.title(f"Override — {f['name']}")
            dlg.geometry("480x400")
            dlg.transient(win)
            dlg.grab_set()
            dlg.resizable(False, False)
            self._center_on_main(dlg)

            fr = ttk.Frame(dlg, padding=12)
            fr.pack(fill='both', expand=True)
            pad = {'padx': 8, 'pady': 4}
            row = 0

            # ── Convert audio ──
            v_conv_audio = tk.BooleanVar(value=ov.get('convert_audio', opt_convert_audio.get()))
            ttk.Checkbutton(fr, text="Convert audio:", variable=v_conv_audio).grid(
                row=row, column=0, sticky='w', **pad)
            v_acodec = tk.StringVar(value=ov.get('audio_codec_display',
                mp_audio_codec_reverse.get(ov.get('audio_codec', ''), mp_ac_combo.get())))
            acodec_combo = ttk.Combobox(fr, textvariable=v_acodec,
                values=list(mp_audio_codec_map.keys()), width=18, state='readonly')
            acodec_combo.grid(row=row, column=1, sticky='w', **pad)
            row += 1
            ttk.Label(fr, text="Audio bitrate:").grid(row=row, column=0, sticky='w', **pad)
            v_abitrate = tk.StringVar(value=ov.get('audio_bitrate', opt_audio_bitrate.get()))
            ttk.Combobox(fr, textvariable=v_abitrate,
                values=('128k','192k','256k','320k','384k','448k','512k','640k'),
                width=7, state='readonly').grid(row=row, column=1, sticky='w', **pad)
            row += 1

            # ── Strip options ──
            v_strip_ch = tk.BooleanVar(value=ov.get('strip_chapters', opt_strip_chapters.get()))
            v_strip_tg = tk.BooleanVar(value=ov.get('strip_tags', opt_strip_tags.get()))
            v_strip_sb = tk.BooleanVar(value=ov.get('strip_subs', opt_strip_subs.get()))
            cf = ttk.Frame(fr)
            cf.grid(row=row, column=0, columnspan=2, sticky='w', **pad); row += 1
            ttk.Checkbutton(cf, text="Strip chapters", variable=v_strip_ch).pack(side='left', padx=4)
            ttk.Checkbutton(cf, text="Strip tags", variable=v_strip_tg).pack(side='left', padx=4)
            ttk.Checkbutton(cf, text="Strip subs", variable=v_strip_sb).pack(side='left', padx=4)

            # ── Mux subs ──
            v_mux = tk.BooleanVar(value=ov.get('mux_subs', opt_mux_subs.get()))
            ttk.Checkbutton(fr, text="Mux external subtitles", variable=v_mux).grid(
                row=row, column=0, columnspan=2, sticky='w', **pad); row += 1

            # ── Metadata ──
            v_meta = tk.BooleanVar(value=ov.get('set_metadata', opt_set_metadata.get()))
            ttk.Checkbutton(fr, text="Set track metadata", variable=v_meta).grid(
                row=row, column=0, columnspan=2, sticky='w', **pad); row += 1

            mf = ttk.Frame(fr)
            mf.grid(row=row, column=0, columnspan=2, sticky='w', **pad); row += 1
            ttk.Label(mf, text="V:").pack(side='left')
            v_mv = tk.StringVar(value=ov.get('meta_video', opt_meta_video.get()))
            ttk.Entry(mf, textvariable=v_mv, width=4).pack(side='left', padx=(2,6))
            ttk.Label(mf, text="A:").pack(side='left')
            v_ma = tk.StringVar(value=ov.get('meta_audio', opt_meta_audio.get()))
            ttk.Entry(mf, textvariable=v_ma, width=4).pack(side='left', padx=(2,6))
            ttk.Label(mf, text="S:").pack(side='left')
            v_ms = tk.StringVar(value=ov.get('meta_sub', opt_meta_sub.get()))
            ttk.Entry(mf, textvariable=v_ms, width=4).pack(side='left', padx=(2,0))

            # ── Container ──
            ttk.Label(fr, text="Output container:").grid(row=row, column=0, sticky='w', **pad)
            v_ctr = tk.StringVar(value=ov.get('container', opt_container.get()))
            ttk.Combobox(fr, textvariable=v_ctr, values=('.mkv', '.mp4'),
                         width=6, state='readonly').grid(row=row, column=1, sticky='w', **pad)
            row += 1

            # ── Buttons ──
            bf = ttk.Frame(dlg, padding=(12,0,12,12))
            bf.pack(fill='x')
            def _save_ovr():
                f['overrides'] = {
                    'convert_audio': v_conv_audio.get(),
                    'audio_codec': mp_audio_codec_map.get(v_acodec.get(), v_acodec.get()),
                    'audio_codec_display': v_acodec.get(),
                    'audio_bitrate': v_abitrate.get(),
                    'strip_chapters': v_strip_ch.get(),
                    'strip_tags': v_strip_tg.get(),
                    'strip_subs': v_strip_sb.get(),
                    'mux_subs': v_mux.get(),
                    'set_metadata': v_meta.get(),
                    'meta_video': v_mv.get(),
                    'meta_audio': v_ma.get(),
                    'meta_sub': v_ms.get(),
                    'container': v_ctr.get(),
                }
                _rebuild_tree()
                _log(f"Override saved for: {f['name']}", 'INFO')
                dlg.destroy()
            ttk.Button(bf, text="Save Override", command=_save_ovr).pack(side='right', padx=(4,0))
            ttk.Button(bf, text="Cancel", command=dlg.destroy).pack(side='right')

        def _show_sub_manager(index):
            """Show subtitle file manager for a single file."""
            f = mp_files[index]
            dlg = tk.Toplevel(win)
            dlg.title(f"Subtitles — {f['name']}")
            dlg.geometry("500x300")
            dlg.transient(win)
            dlg.grab_set()
            dlg.resizable(True, True)
            self._center_on_main(dlg)

            subs = list(f['ext_subs'])  # work on a copy

            fr = ttk.Frame(dlg, padding=12)
            fr.pack(fill='both', expand=True)
            fr.columnconfigure(0, weight=1)
            fr.rowconfigure(0, weight=1)

            sub_list = tk.Listbox(fr, height=6)
            sub_list.grid(row=0, column=0, sticky='nsew')

            def _refresh():
                sub_list.delete(0, 'end')
                for stype, spath in subs:
                    sub_list.insert('end', f"[{stype}] {os.path.basename(spath)}")

            def _add_sub():
                paths = filedialog.askopenfilenames(
                    title="Select Subtitle Files",
                    filetypes=[("Subtitle files", "*.srt *.ass *.ssa *.vtt *.sub"),
                               ("All files", "*.*")])
                for p in paths:
                    # Guess type from filename
                    bn = os.path.basename(p).lower()
                    if 'forced' in bn:
                        stype = 'forced'
                    else:
                        stype = 'main'
                    subs.append((stype, p))
                _refresh()

            def _remove_sub():
                sel = sub_list.curselection()
                if sel:
                    for i in sorted(sel, reverse=True):
                        del subs[i]
                    _refresh()

            def _toggle_type():
                sel = sub_list.curselection()
                if sel:
                    i = sel[0]
                    stype, spath = subs[i]
                    new_type = 'forced' if stype == 'main' else 'main'
                    subs[i] = (new_type, spath)
                    _refresh()

            def _move_up():
                sel = sub_list.curselection()
                if sel and sel[0] > 0:
                    i = sel[0]
                    subs[i-1], subs[i] = subs[i], subs[i-1]
                    _refresh()
                    sub_list.selection_set(i-1)

            def _move_down():
                sel = sub_list.curselection()
                if sel and sel[0] < len(subs) - 1:
                    i = sel[0]
                    subs[i], subs[i+1] = subs[i+1], subs[i]
                    _refresh()
                    sub_list.selection_set(i+1)

            btn_fr = ttk.Frame(fr)
            btn_fr.grid(row=1, column=0, sticky='ew', pady=(6,0))
            ttk.Button(btn_fr, text="Add...", command=_add_sub).pack(side='left', padx=2)
            ttk.Button(btn_fr, text="Remove", command=_remove_sub).pack(side='left', padx=2)
            ttk.Button(btn_fr, text="Toggle Main/Forced", command=_toggle_type).pack(side='left', padx=2)
            ttk.Separator(btn_fr, orient='vertical').pack(side='left', fill='y', padx=6)
            ttk.Button(btn_fr, text="⬆ Up", command=_move_up, width=4).pack(side='left', padx=2)
            ttk.Button(btn_fr, text="⬇ Down", command=_move_down, width=5).pack(side='left', padx=2)

            bot = ttk.Frame(dlg, padding=(12,0,12,12))
            bot.pack(fill='x')
            def _save_subs():
                f['ext_subs'] = list(subs)
                _rebuild_tree()
                _log(f"Subtitles updated for: {f['name']} ({len(subs)} file(s))", 'INFO')
                dlg.destroy()
            ttk.Button(bot, text="Save", command=_save_subs).pack(side='right', padx=(4,0))
            ttk.Button(bot, text="Cancel", command=dlg.destroy).pack(side='right')
            _refresh()

        # Double-click opens override dialog
        def _on_double_click(event):
            item = tree.identify_row(event.y)
            if item and not mp_processing[0]:
                items = tree.get_children()
                index = list(items).index(item)
                _show_file_override(index)
        tree.bind('<Double-1>', _on_double_click)

        # ── Operations panel ──
        ops_frame = ttk.LabelFrame(main_frame, text="Operations", padding=8)
        ops_frame.grid(row=2, column=0, sticky='ew', pady=(0, 6))

        # Row 1: Audio + strip options
        ops_row1 = ttk.Frame(ops_frame)
        ops_row1.pack(fill='x', pady=2)

        def _toggle_audio_controls():
            st = 'readonly' if opt_convert_audio.get() else 'disabled'
            mp_ac_combo.configure(state=st)
            mp_br_combo.configure(state=st)

        ttk.Checkbutton(ops_row1, text="Convert audio:",
                       variable=opt_convert_audio, command=_toggle_audio_controls).pack(side='left', padx=(0, 4))
        mp_ac_combo = ttk.Combobox(ops_row1, textvariable=opt_audio_codec,
                                   width=18, state='readonly')
        mp_ac_combo['values'] = list(mp_audio_codec_map.keys())
        mp_ac_combo.pack(side='left', padx=(0, 8))
        ttk.Label(ops_row1, text="Bitrate:").pack(side='left', padx=(0, 2))
        mp_br_combo = ttk.Combobox(ops_row1, textvariable=opt_audio_bitrate,
                                   values=('128k', '192k', '256k', '320k', '384k', '448k', '512k', '640k'),
                                   width=6, state='readonly')
        mp_br_combo.pack(side='left', padx=(0, 16))

        ttk.Checkbutton(ops_row1, text="Strip chapters",
                       variable=opt_strip_chapters).pack(side='left', padx=4)
        ttk.Checkbutton(ops_row1, text="Strip tags",
                       variable=opt_strip_tags).pack(side='left', padx=4)

        # Row 2: Strip subs + mux subs + sub language
        ops_row2 = ttk.Frame(ops_frame)
        ops_row2.pack(fill='x', pady=2)

        ttk.Checkbutton(ops_row2, text="Strip existing subtitles",
                       variable=opt_strip_subs).pack(side='left', padx=(0, 4))
        ttk.Checkbutton(ops_row2, text="Mux external subtitles",
                       variable=opt_mux_subs).pack(side='left', padx=(16, 4))
        ttk.Label(ops_row2, text="Lang:").pack(side='left', padx=(8, 2))
        sub_lang_entry = ttk.Entry(ops_row2, textvariable=opt_sub_lang, width=4)
        sub_lang_entry.pack(side='left', padx=(0, 4))
        ttk.Button(ops_row2, text="🔄 Rescan", command=_rescan_subs, width=8).pack(side='left', padx=4)

        # Row 3: Track metadata
        ops_row3 = ttk.Frame(ops_frame)
        ops_row3.pack(fill='x', pady=2)

        def _toggle_meta_fields():
            st = 'normal' if opt_set_metadata.get() else 'disabled'
            mp_mv.configure(state=st)
            mp_ma.configure(state=st)
            mp_ms.configure(state=st)

        ttk.Checkbutton(ops_row3, text="Set track metadata:",
                       variable=opt_set_metadata, command=_toggle_meta_fields).pack(side='left', padx=(0, 4))
        ttk.Label(ops_row3, text="V:").pack(side='left')
        mp_mv = ttk.Entry(ops_row3, textvariable=opt_meta_video, width=4)
        mp_mv.pack(side='left', padx=(2, 6))
        ttk.Label(ops_row3, text="A:").pack(side='left')
        mp_ma = ttk.Entry(ops_row3, textvariable=opt_meta_audio, width=4)
        mp_ma.pack(side='left', padx=(2, 6))
        ttk.Label(ops_row3, text="S:").pack(side='left')
        mp_ms = ttk.Entry(ops_row3, textvariable=opt_meta_sub, width=4)
        mp_ms.pack(side='left', padx=(2, 0))

        _toggle_meta_fields()
        _toggle_audio_controls()

        # Row 3b: Edition tagging
        ops_row3b = ttk.Frame(ops_frame)
        ops_row3b.pack(fill='x', pady=2)

        ttk.Label(ops_row3b, text="Edition:").pack(side='left', padx=(0, 2))
        mp_edition_combo = ttk.Combobox(ops_row3b, textvariable=opt_edition_tag,
                                         values=EDITION_PRESETS, width=22, state='readonly')
        mp_edition_combo.pack(side='left', padx=(0, 4))

        mp_edition_custom = ttk.Entry(ops_row3b, textvariable=_edition_custom_sv, width=22)

        if opt_edition_tag.get() and opt_edition_tag.get() not in EDITION_PRESETS:
            _edition_custom_sv.set(opt_edition_tag.get())
            mp_edition_combo.set('Custom...')
            mp_edition_custom.pack(side='left', padx=(0, 4))

        def _on_mp_edition_select(event=None):
            sel = mp_edition_combo.get()
            if sel == 'Custom...':
                mp_edition_custom.pack(side='left', padx=(0, 4))
                mp_edition_custom.focus()
            else:
                mp_edition_custom.pack_forget()
                opt_edition_tag.set(sel)
        mp_edition_combo.bind('<<ComboboxSelected>>', _on_mp_edition_select)

        def _on_mp_edition_custom(*args):
            if mp_edition_combo.get() == 'Custom...':
                opt_edition_tag.set(_edition_custom_sv.get())
        _edition_custom_sv.trace_add('write', _on_mp_edition_custom)

        ttk.Checkbutton(ops_row3b, text="Add to filename (Plex)",
                        variable=opt_edition_fn).pack(side='left', padx=(8, 0))

        # Row 3c: Chapter insertion
        ops_row3c = ttk.Frame(ops_frame)
        ops_row3c.pack(fill='x', pady=2)

        def _toggle_ch_spin():
            mp_ch_spin.configure(state='normal' if opt_add_chapters.get() else 'disabled')
            if opt_add_chapters.get():
                opt_strip_chapters.set(False)

        ttk.Checkbutton(ops_row3c, text="Add chapters every",
                        variable=opt_add_chapters,
                        command=_toggle_ch_spin).pack(side='left', padx=(0, 2))
        mp_ch_spin = tk.Spinbox(ops_row3c, textvariable=opt_ch_interval,
                                from_=1, to=60, width=3, state='disabled')
        mp_ch_spin.pack(side='left', padx=(0, 2))
        ttk.Label(ops_row3c, text="minutes").pack(side='left')
        _toggle_ch_spin()

        # Row 4: Output + parallel + container
        ops_row4 = ttk.Frame(ops_frame)
        ops_row4.pack(fill='x', pady=2)

        ttk.Label(ops_row4, text="Output:").pack(side='left', padx=(0, 4))
        ttk.Radiobutton(ops_row4, text="Replace in-place", variable=opt_output_mode,
                        value='inplace', command=lambda: _toggle_output_folder()).pack(side='left', padx=(0, 4))
        ttk.Radiobutton(ops_row4, text="Save to folder:", variable=opt_output_mode,
                        value='folder', command=lambda: _toggle_output_folder()).pack(side='left', padx=(0, 4))
        mp_out_entry = ttk.Entry(ops_row4, textvariable=opt_output_folder, width=24, state='disabled')
        mp_out_entry.pack(side='left', padx=(0, 4))
        mp_out_btn = ttk.Button(ops_row4, text="Browse…", state='disabled',
            command=lambda: opt_output_folder.set(
                self._ask_directory(title="Select Output Folder") or opt_output_folder.get()))
        mp_out_btn.pack(side='left', padx=(0, 12))

        ttk.Label(ops_row4, text="Container:").pack(side='left', padx=(0, 2))
        ttk.Combobox(ops_row4, textvariable=opt_container,
                     values=('.mkv', '.mp4'), width=5, state='readonly').pack(side='left', padx=(0, 12))

        ttk.Checkbutton(ops_row4, text="Parallel",
                       variable=opt_parallel).pack(side='left', padx=(0, 2))
        ttk.Label(ops_row4, text="Jobs:").pack(side='left', padx=(0, 2))
        ttk.Spinbox(ops_row4, textvariable=opt_max_jobs, from_=1, to=32,
                    width=3).pack(side='left')

        def _toggle_output_folder():
            st = 'normal' if opt_output_mode.get() == 'folder' else 'disabled'
            mp_out_entry.configure(state=st)
            mp_out_btn.configure(state=st)
        _toggle_output_folder()

        # ── Progress bar ──
        progress_frame = ttk.Frame(main_frame)
        progress_frame.grid(row=3, column=0, sticky='ew', pady=(0, 6))
        progress_frame.columnconfigure(1, weight=1)

        mp_progress_var = tk.DoubleVar(value=0)
        mp_progress_label = ttk.Label(progress_frame, text="Ready")
        mp_progress_label.grid(row=0, column=0, sticky='w', padx=(0, 8))
        mp_progress_bar = ttk.Progressbar(progress_frame, variable=mp_progress_var,
                                          maximum=100, mode='determinate')
        mp_progress_bar.grid(row=0, column=1, sticky='ew')

        # ── Action buttons ──
        btn_frame = ttk.Frame(progress_frame)
        btn_frame.grid(row=0, column=2, padx=(8, 0))
        process_btn = ttk.Button(btn_frame, text="▶ Process All", command=lambda: _start_processing())
        process_btn.pack(side='left', padx=2)
        stop_btn = ttk.Button(btn_frame, text="⏹ Stop", command=lambda: _stop_processing(), state='disabled')
        stop_btn.pack(side='left', padx=2)

        # ── Log ──
        log_frame = ttk.LabelFrame(main_frame, text="Log", padding=5)
        log_frame.grid(row=4, column=0, sticky='nsew')
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)

        log_text = tk.Text(log_frame, height=8, wrap='word', font=('Courier', 9),
                          state='disabled', bg='#1e1e1e', fg='#d4d4d4')
        log_text.grid(row=0, column=0, sticky='nsew')
        log_scroll = ttk.Scrollbar(log_frame, orient='vertical', command=log_text.yview)
        log_scroll.grid(row=0, column=1, sticky='ns')
        log_text.configure(yscrollcommand=log_scroll.set)

        def _clear_log():
            log_text.configure(state='normal')
            log_text.delete('1.0', 'end')
            log_text.configure(state='disabled')

        ttk.Button(log_frame, text="Clear Log", command=_clear_log).grid(row=1, column=0, sticky='w', pady=(4, 0))

        # Log color tags
        log_text.tag_configure('INFO',    foreground='#d4d4d4')
        log_text.tag_configure('SUCCESS', foreground='#4ec9b0')
        log_text.tag_configure('WARNING', foreground='#dcdcaa')
        log_text.tag_configure('ERROR',   foreground='#f44747')
        log_text.tag_configure('SKIP',    foreground='#569cd6')

        def _log(msg, level='INFO'):
            def _do():
                log_text.configure(state='normal')
                ts = datetime.now().strftime('%H:%M:%S')
                log_text.insert('end', f"[{ts}] [{level}] {msg}\n", level)
                log_text.see('end')
                log_text.configure(state='disabled')
            # Safe to call from any thread
            win.after(0, _do)

        # ── Preflight check ──
        def _preflight():
            """Validate files before processing. Returns True if OK to proceed."""
            errors = 0
            warnings = 0
            _log("═" * 50, 'INFO')
            _log("Pre-flight check", 'INFO')
            _log("═" * 50, 'INFO')

            for i, f in enumerate(mp_files):
                filepath = f['path']
                name = f['name']
                ov = f.get('overrides', {})

                # Readable?
                if not os.access(filepath, os.R_OK):
                    _log(f"  {name}: File is not readable", 'ERROR')
                    errors += 1; continue

                # Empty?
                if os.path.getsize(filepath) == 0:
                    _log(f"  {name}: File is empty (0 bytes)", 'ERROR')
                    errors += 1; continue

                # Missing subtitle files?
                do_mux = ov.get('mux_subs', opt_mux_subs.get())
                if do_mux and not f['ext_subs']:
                    lang = opt_sub_lang.get()
                    _log(f"  {name}: No .{lang}.srt found alongside file", 'WARNING')
                    warnings += 1

                # Audio info?
                if not f['audio_info']:
                    _log(f"  {name}: Could not read audio stream", 'WARNING')
                    warnings += 1

            if errors > 0:
                _log(f"Pre-flight FAILED — {errors} error(s), {warnings} warning(s)", 'ERROR')
                return False
            elif warnings > 0:
                _log(f"Pre-flight PASSED with {warnings} warning(s)", 'WARNING')
            else:
                _log("Pre-flight PASSED — all checks OK", 'SUCCESS')

            _log("═" * 50, 'INFO')
            return True

        # ── Resolve effective setting (per-file override > global) ──
        def _ov(f, key, global_var):
            """Get effective value: per-file override if set, else global."""
            ov = f.get('overrides', {})
            if key in ov:
                return ov[key]
            if isinstance(global_var, (tk.BooleanVar, tk.StringVar, tk.IntVar)):
                return global_var.get()
            return global_var

        # ── Build ffmpeg command for one file ──
        def _build_cmd(f):
            """Build a single ffmpeg remux command for the given file dict.
            Returns (cmd_list, output_path) or (None, None) on error."""
            input_path = f['path']
            base, ext = os.path.splitext(input_path)
            ov = f.get('overrides', {})

            # Determine output container
            out_ext = _ov(f, 'container', opt_container)

            # Edition filename tag for Plex
            edition_fn_part = ''
            edition = _ov(f, 'edition_tag', opt_edition_tag)
            if isinstance(edition, tk.StringVar):
                edition = edition.get()
            edition_in_fn = _ov(f, 'edition_in_filename', opt_edition_fn)
            if isinstance(edition_in_fn, tk.BooleanVar):
                edition_in_fn = edition_in_fn.get()
            if edition and edition_in_fn:
                edition_fn_part = ' {edition-' + edition + '}'

            # Determine output path
            if opt_output_mode.get() == 'folder' and opt_output_folder.get():
                out_dir = opt_output_folder.get()
                out_name = os.path.join(out_dir,
                                         os.path.basename(base) + edition_fn_part + out_ext)
            else:
                # In-place: write to temp, replace on success
                out_name = f"{base}_mp_tmp{out_ext}"

            cmd = ['ffmpeg', '-y', '-i', input_path]

            # Additional inputs: external subtitle files
            sub_inputs = []
            do_mux = _ov(f, 'mux_subs', opt_mux_subs)
            if do_mux:
                for stype, spath in f['ext_subs']:
                    cmd.extend(['-i', spath])
                    sub_inputs.append((stype, spath))

            # Chapter injection
            ch_meta_path = None
            ch_input_idx = None
            do_add_ch = _ov(f, 'add_chapters', opt_add_chapters)
            if isinstance(do_add_ch, tk.BooleanVar):
                do_add_ch = do_add_ch.get()
            if do_add_ch and f.get('duration_secs'):
                ch_intv = _ov(f, 'chapter_interval', opt_ch_interval)
                if isinstance(ch_intv, tk.IntVar):
                    ch_intv = ch_intv.get()
                try:
                    from modules.chapters import generate_auto_chapters, chapters_to_ffmetadata
                except ImportError:
                    import importlib.util as _ilu
                    _ch_p = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                          'modules', 'chapters.py')
                    _sp = _ilu.spec_from_file_location('chapters', _ch_p)
                    _md = _ilu.module_from_spec(_sp)
                    _sp.loader.exec_module(_md)
                    generate_auto_chapters = _md.generate_auto_chapters
                    chapters_to_ffmetadata = _md.chapters_to_ffmetadata
                chs = generate_auto_chapters(f['duration_secs'], ch_intv)
                if chs:
                    ch_meta_path = chapters_to_ffmetadata(chs)
                    if ch_meta_path:
                        ch_input_idx = 1 + len(sub_inputs)
                        cmd.extend(['-i', ch_meta_path])
                        f['_ch_meta_path'] = ch_meta_path

            # ── Mapping ──
            cmd.extend(['-map', '0:v:0?', '-map', '0:a?'])

            # Subtitle mapping
            do_strip_subs = _ov(f, 'strip_subs', opt_strip_subs)
            if do_strip_subs:
                pass  # Don't map source subtitles
            else:
                cmd.extend(['-map', '0:s?'])

            # Map external subtitle inputs
            for idx, (stype, spath) in enumerate(sub_inputs):
                input_idx = 1 + idx
                cmd.extend(['-map', f'{input_idx}:0'])

            # ── Video: always copy (remux only) ──
            cmd.extend(['-c:v', 'copy'])

            # ── Audio ──
            do_convert_audio = _ov(f, 'convert_audio', opt_convert_audio)
            if do_convert_audio:
                # Resolve codec
                if 'audio_codec' in ov:
                    target_codec = ov['audio_codec']
                else:
                    selected_display = opt_audio_codec.get()
                    target_codec = mp_audio_codec_map.get(selected_display, selected_display)

                audio_bitrate = _ov(f, 'audio_bitrate', opt_audio_bitrate)

                if target_codec == 'copy':
                    cmd.extend(['-c:a', 'copy'])
                else:
                    audio = f.get('audio_info', [])
                    src_codec = audio[0]['codec_name'] if audio else ''
                    codec_aliases = {
                        'ac3': ('ac3', 'eac3'),
                        'eac3': ('eac3',),
                        'aac': ('aac',),
                        'mp3': ('mp3',),
                        'opus': ('opus',),
                        'flac': ('flac',),
                    }
                    match_set = codec_aliases.get(target_codec, (target_codec,))
                    if src_codec in match_set:
                        cmd.extend(['-c:a', 'copy'])
                        _log(f"  Audio: already {src_codec.upper()}, copying", 'SKIP')
                    else:
                        LOSSLESS = {'flac'}
                        EXPERIMENTAL = {'opus', 'vorbis'}
                        cmd.extend(['-c:a', target_codec])
                        if target_codec in EXPERIMENTAL:
                            cmd.extend(['-strict', '-2'])
                        if target_codec not in LOSSLESS:
                            cmd.extend(['-b:a', audio_bitrate])
            else:
                cmd.extend(['-c:a', 'copy'])

            # ── Subtitles codec ──
            if not do_strip_subs:
                # Handle container compatibility for internal subs
                if out_ext == '.mp4':
                    cmd.extend(['-c:s', 'mov_text'])
                else:
                    cmd.extend(['-c:s', 'copy'])
            # External subs
            out_sub_idx = 0
            if not do_strip_subs:
                out_sub_idx = f.get('sub_count', 0)

            do_set_meta = _ov(f, 'set_metadata', opt_set_metadata)
            s_lang = _ov(f, 'meta_sub', opt_meta_sub)
            for idx, (stype, spath) in enumerate(sub_inputs):
                si = out_sub_idx + idx
                sub_codec = 'mov_text' if out_ext == '.mp4' else 'srt'
                cmd.extend([f'-c:s:{si}', sub_codec])
                sub_lang = s_lang if do_set_meta else opt_sub_lang.get()
                cmd.extend([f'-metadata:s:s:{si}', f'language={sub_lang}'])
                if stype == 'main':
                    cmd.extend([f'-metadata:s:s:{si}', 'title=English'])
                    cmd.extend([f'-disposition:s:{si}', 'default'])
                elif stype == 'forced':
                    cmd.extend([f'-metadata:s:s:{si}', 'title=Forced'])
                    cmd.extend([f'-disposition:s:{si}', 'forced'])

            # ── Chapters ──
            if ch_meta_path:
                cmd.extend(['-map_chapters', str(ch_input_idx)])
            elif _ov(f, 'strip_chapters', opt_strip_chapters):
                cmd.extend(['-map_chapters', '-1'])

            # ── Tags ──
            if _ov(f, 'strip_tags', opt_strip_tags):
                cmd.extend(['-map_metadata', '-1'])

            # ── Track metadata ──
            if do_set_meta:
                v_lang = _ov(f, 'meta_video', opt_meta_video)
                a_lang = _ov(f, 'meta_audio', opt_meta_audio)
                cmd.extend(['-metadata', 'title='])
                cmd.extend(['-metadata:s:v:0', f'language={v_lang}', '-metadata:s:v:0', 'title='])
                cmd.extend(['-metadata:s:a:0', f'language={a_lang}', '-metadata:s:a:0', 'title='])
                if (not do_strip_subs and f.get('sub_count', 0) > 0) or sub_inputs:
                    if not sub_inputs:
                        cmd.extend([f'-metadata:s:s:0', f'language={s_lang}'])

            # ── Edition tag ──
            if edition:
                cmd.extend(['-metadata', f'title={edition}'])

            cmd.append(out_name)
            return cmd, out_name

        # ── Process a single file (called from thread pool or sequential loop) ──
        def _process_one(i, f):
            """Process a single file. Returns ('done'|'failed'|'skipped', index)."""
            if mp_stop[0]:
                return ('stopped', i)

            name = f['name']
            win.after(0, lambda idx=i: _update_tree_status(idx, '⏳ Processing'))

            _log(f"── {name} ──", 'INFO')

            try:
                cmd, out_path = _build_cmd(f)
                if cmd is None:
                    _log(f"  Skipped (could not build command)", 'WARNING')
                    win.after(0, lambda idx=i: _update_tree_status(idx, '⏭️ Skipped'))
                    return ('skipped', i)

                _log(f"  Command: {' '.join(cmd)}", 'INFO')

                proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1)

                last_lines = []
                for line in proc.stdout:
                    if mp_stop[0]:
                        proc.terminate()
                        return ('stopped', i)
                    line = line.strip()
                    if line:
                        last_lines.append(line)
                        if len(last_lines) > 5:
                            last_lines.pop(0)

                proc.wait()

                if proc.returncode == 0:
                    # In-place mode: replace original
                    is_inplace = opt_output_mode.get() == 'inplace' or not opt_output_folder.get()
                    if is_inplace:
                        original = f['path']
                        try:
                            os.replace(out_path, original)
                        except OSError as e:
                            _log(f"  Warning: could not replace original: {e}", 'WARNING')
                        final_path = original
                    else:
                        final_path = out_path

                    out_size = os.path.getsize(final_path) if os.path.exists(final_path) else 0
                    out_mb = f'{out_size / (1024*1024):.1f} MB' if out_size else '?'
                    _log(f"  ✅ Done — output: {out_mb}", 'SUCCESS')
                    win.after(0, lambda idx=i: _update_tree_status(idx, '✅ Done'))
                    return ('done', i)
                else:
                    _log(f"  ❌ Failed (exit code {proc.returncode})", 'ERROR')
                    for ll in last_lines:
                        _log(f"    {ll}", 'ERROR')
                    win.after(0, lambda idx=i: _update_tree_status(idx, '❌ Failed'))
                    # Clean up partial output
                    if os.path.exists(out_path):
                        try:
                            os.remove(out_path)
                        except OSError:
                            pass
                    return ('failed', i)

            except Exception as e:
                _log(f"  ❌ Error: {e}", 'ERROR')
                win.after(0, lambda idx=i: _update_tree_status(idx, '❌ Error'))
                return ('failed', i)
            finally:
                # Clean up chapter metadata temp file
                ch_tmp = f.pop('_ch_meta_path', None)
                if ch_tmp:
                    try:
                        os.remove(ch_tmp)
                    except OSError:
                        pass

        # ── Processing thread (orchestrator) ──
        def _process_files():
            mp_processing[0] = True
            mp_stop[0] = False
            total = len(mp_files)
            completed = [0]
            failed = [0]
            processed_count = [0]

            win.after(0, lambda: process_btn.configure(state='disabled'))
            win.after(0, lambda: stop_btn.configure(state='normal'))

            use_parallel = opt_parallel.get()
            max_jobs = opt_max_jobs.get()

            mode_label = f"parallel ({max_jobs} jobs)" if use_parallel else "sequential"
            _log(f"Processing {total} file(s) — {mode_label}", 'INFO')

            def _on_result(result, idx):
                status, _ = result
                with mp_lock:
                    processed_count[0] += 1
                    if status == 'done':
                        completed[0] += 1
                    elif status == 'failed':
                        failed[0] += 1
                    pct = (processed_count[0] / total) * 100
                win.after(0, lambda p=pct: mp_progress_var.set(p))
                win.after(0, lambda c=processed_count[0]: mp_progress_label.configure(
                    text=f"Processing {c}/{total}"))

            if use_parallel and max_jobs > 1:
                with ThreadPoolExecutor(max_workers=max_jobs) as executor:
                    futures = {}
                    for i, f in enumerate(mp_files):
                        if mp_stop[0]:
                            break
                        future = executor.submit(_process_one, i, f)
                        futures[future] = i

                    for future in as_completed(futures):
                        idx = futures[future]
                        try:
                            result = future.result()
                            _on_result(result, idx)
                        except Exception as e:
                            _log(f"  ❌ Unexpected error: {e}", 'ERROR')
                            with mp_lock:
                                processed_count[0] += 1
                                failed[0] += 1
            else:
                for i, f in enumerate(mp_files):
                    if mp_stop[0]:
                        _log("Processing stopped by user", 'WARNING')
                        break
                    result = _process_one(i, f)
                    _on_result(result, i)

            # Done
            _log("═" * 50, 'INFO')
            _log(f"Complete — {completed[0]} succeeded, {failed[0]} failed, "
                 f"{total - completed[0] - failed[0]} skipped/stopped", 'SUCCESS')
            _log("═" * 50, 'INFO')

            # Clean up .srt files if muxing was enabled
            if opt_mux_subs.get() and completed[0] > 0:
                cleaned = 0
                for f in mp_files:
                    if f['status'] == '✅ Done':
                        for stype, spath in f.get('ext_subs', []):
                            if os.path.exists(spath):
                                try:
                                    os.remove(spath)
                                    cleaned += 1
                                except OSError:
                                    pass
                if cleaned:
                    _log(f"Cleaned up {cleaned} subtitle file(s)", 'INFO')

            # Re-probe completed files to update display
            if completed[0] > 0:
                _log("Re-probing completed files...", 'INFO')
                for i, f in enumerate(mp_files):
                    if f['status'] == '✅ Done' and os.path.exists(f['path']):
                        _reprobe_file(i)

            win.after(0, lambda: process_btn.configure(state='normal'))
            win.after(0, lambda: stop_btn.configure(state='disabled'))
            win.after(0, lambda: mp_progress_label.configure(
                text=f"Done — {completed[0]}/{total} processed"))
            mp_processing[0] = False

        def _start_processing():
            if not mp_files:
                _log("No files to process", 'WARNING')
                return
            if mp_processing[0]:
                return

            # Validate output folder if using folder mode
            if opt_output_mode.get() == 'folder':
                folder = opt_output_folder.get().strip()
                if not folder:
                    _log("Output folder not set — select a folder or use in-place mode", 'ERROR')
                    return
                os.makedirs(folder, exist_ok=True)

            # Run preflight
            if not _preflight():
                return

            # Start in thread
            t = threading.Thread(target=_process_files, daemon=True)
            t.start()

        def _stop_processing():
            mp_stop[0] = True

        # ── Drag and drop support ──
        try:
            win.drop_target_register('DND_Files')
            def _on_drop(event):
                raw = event.data
                # Parse file:// URIs and space-separated paths
                paths = []
                if 'file://' in raw:
                    from urllib.parse import unquote, urlparse
                    for token in raw.split():
                        token = token.strip()
                        if token.startswith('file://'):
                            parsed = urlparse(token)
                            paths.append(unquote(parsed.path))
                elif raw.startswith('{') and raw.endswith('}'):
                    # Tcl list format for paths with spaces
                    paths = [p.strip('{}') for p in re.findall(r'\{[^}]+\}|[^\s]+', raw)]
                else:
                    paths = raw.split()

                added = 0
                for p in paths:
                    if os.path.isfile(p) and os.path.splitext(p)[1].lower() in VIDEO_EXTENSIONS:
                        _add_one_file(p)
                        added += 1
                    elif os.path.isdir(p):
                        for ext in VIDEO_EXTENSIONS:
                            for fp in Path(p).glob(f'*{ext}'):
                                if fp.is_file() and not fp.name.startswith('.'):
                                    _add_one_file(str(fp))
                                    added += 1
                if added:
                    _rebuild_tree()
                    _log(f"Added {added} file(s) via drag-and-drop", 'INFO')

            win.dnd_bind('<<Drop>>', _on_drop)
        except Exception:
            pass  # tkinterdnd2 not available

        # ── Save / Close ──
        def _save_mp_prefs():
            """Save current Media Processor settings to preferences."""
            mp_prefs = {
                'convert_audio':  opt_convert_audio.get(),
                'audio_codec':    opt_audio_codec.get(),
                'audio_bitrate':  opt_audio_bitrate.get(),
                'strip_chapters': opt_strip_chapters.get(),
                'strip_tags':     opt_strip_tags.get(),
                'strip_subs':     opt_strip_subs.get(),
                'set_metadata':   opt_set_metadata.get(),
                'meta_video':     opt_meta_video.get(),
                'meta_audio':     opt_meta_audio.get(),
                'meta_sub':       opt_meta_sub.get(),
                'mux_subs':       opt_mux_subs.get(),
                'sub_lang':       opt_sub_lang.get(),
                'output_mode':    opt_output_mode.get(),
                'output_folder':  opt_output_folder.get(),
                'container':      opt_container.get(),
                'parallel':       opt_parallel.get(),
                'max_jobs':       opt_max_jobs.get(),
                'edition_tag':    opt_edition_tag.get(),
                'edition_in_filename': opt_edition_fn.get(),
                'add_chapters':   opt_add_chapters.get(),
                'chapter_interval': opt_ch_interval.get(),
            }
            self._media_proc_prefs = mp_prefs
            try:
                p = self._prefs_path()
                if p.exists():
                    prefs = json.loads(p.read_text())
                else:
                    prefs = {}
                prefs['media_processor'] = mp_prefs
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(json.dumps(prefs, indent=2))
            except Exception:
                pass  # best-effort save

        close_frame = ttk.Frame(main_frame)
        close_frame.grid(row=5, column=0, sticky='e', pady=(6, 0))
        def _close_window():
            _save_mp_prefs()
            win.destroy()
        ttk.Button(close_frame, text="Close", command=_close_window).pack(side='right')
        win.protocol('WM_DELETE_WINDOW', _close_window)

        _log("Media Processor ready — add files and click Process All", 'INFO')
        _log("Tip: drag and drop video files onto this window", 'INFO')
        _log(f"Subtitle matching: *.{opt_sub_lang.get()}.srt / *.{opt_sub_lang.get()}.forced.srt", 'INFO')

    # ── TV Show Renamer ────────────────────────────────────────────────────
    def open_tv_renamer(self):
        """Open the TV Show Renamer tool with TVDB integration."""
        import urllib.request
        import urllib.parse
        import json as _json

        TVDB_BASE = 'https://api4.thetvdb.com/v4'
        TMDB_BASE = 'https://api.themoviedb.org/3'
        TMDB_IMG_BASE = 'https://image.tmdb.org/t/p'

        win = tk.Toplevel(self.root)
        win.title("📺 TV Show Renamer")
        win.geometry("960x650")
        win.minsize(800, 550)
        win.resizable(True, True)
        self._center_on_main(win)

        # ── State ──
        _tvdb_token = [None]
        _all_shows = {}      # {show_name: {(season, ep): ep_data, ...}}
        _file_items = []     # list of {'path': ..., 'season': N, 'episode': N, 'ext': ...}
        _rename_history = []  # list of [(old_path, new_path), ...] for undo

        # Load API keys and preferences
        _saved_key = getattr(self, '_tvdb_api_key', '')
        _saved_tmdb_key = getattr(self, '_tmdb_api_key', '')
        _saved_provider = getattr(self, '_tv_rename_provider', 'TVDB')
        _saved_template = getattr(self, '_tv_rename_template',
                                  '{show} S{season}E{episode} {title}')

        # ── TVDB API helpers ──
        def _tvdb_request(method, path, body=None, token=None):
            """Make a TVDB v4 API request."""
            url = TVDB_BASE + path
            headers = {'Content-Type': 'application/json'}
            if token:
                headers['Authorization'] = f'Bearer {token}'
            data = _json.dumps(body).encode() if body else None
            req = urllib.request.Request(url, data=data, headers=headers,
                                         method=method)
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    return _json.loads(resp.read().decode())
            except urllib.error.HTTPError as e:
                try:
                    err_body = e.read().decode()
                    return _json.loads(err_body)
                except Exception:
                    return {'status': 'error', 'message': str(e)}
            except Exception as e:
                return {'status': 'error', 'message': str(e)}

        def _tvdb_login():
            """Authenticate with TVDB and store token."""
            key = api_key_var.get().strip()
            if not key:
                _log("Enter your TVDB API key", 'WARNING')
                return False
            _log(f"Logging in to TVDB (key: {key[:8]}...)")
            result = _tvdb_request('POST', '/login', {'apikey': key})
            _log(f"Login response: {result.get('status') if result else 'None'}")
            if result and result.get('status') == 'success':
                _tvdb_token[0] = result['data']['token']
                _log("TVDB login successful")
                self._tvdb_api_key = key
                self.save_preferences()
                return True
            else:
                msg = result.get('message', 'Login failed') if result else 'No response'
                _log(f"TVDB login failed: {msg}", 'ERROR')
                return False

        def _tvdb_search(query):
            """Search TVDB for TV series and movies."""
            if not _tvdb_token[0]:
                _log("No token — logging in...")
                if not _tvdb_login():
                    _log("Login failed — cannot search", 'ERROR')
                    return []
            encoded_q = urllib.parse.quote(query)
            # Search without type filter to get both series and movies
            url = f'/search?query={encoded_q}'
            result = _tvdb_request('GET', url, token=_tvdb_token[0])
            if result:
                if result.get('status') == 'success':
                    data = result.get('data', [])
                    # Filter to series and movies only
                    data = [r for r in data
                            if r.get('type') in ('series', 'movie', None)]
                    _log(f"TVDB search returned {len(data)} results")
                    return data
                else:
                    _log(f"Search error: {result.get('message', 'unknown')}", 'ERROR')
            else:
                _log("Search returned no response", 'ERROR')
            return []

        def _tvdb_get_episodes(series_id):
            """Get all episodes for a series."""
            if not _tvdb_token[0]:
                _log("No token — cannot fetch episodes", 'ERROR')
                return []
            all_eps = []
            page = 0
            while True:
                url = f'/series/{series_id}/episodes/default?page={page}'
                _log(f"Fetching: {TVDB_BASE}{url}")
                result = _tvdb_request('GET', url, token=_tvdb_token[0])
                if not result:
                    _log("No response from episodes endpoint", 'ERROR')
                    break
                if result.get('status') != 'success':
                    _log(f"Episodes error: {result.get('message', 'unknown')}", 'ERROR')
                    break
                eps = result.get('data', {}).get('episodes', [])
                if not eps:
                    _log(f"No episodes on page {page}")
                    break
                all_eps.extend(eps)
                # Check if there are more pages
                links = result.get('links', {})
                if links.get('next'):
                    page += 1
                else:
                    break
            return all_eps

        # ── TMDB API helpers ──
        def _tmdb_request(path):
            """Make a TMDB v3 API GET request."""
            key = tmdb_key_var.get().strip()
            if not key:
                return None
            sep = '&' if '?' in path else '?'
            url = f'{TMDB_BASE}{path}{sep}api_key={key}'
            req = urllib.request.Request(url, headers={
                'Accept': 'application/json',
                'User-Agent': 'DocflixVideoConverter/1.9'})
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    return _json.loads(resp.read().decode())
            except urllib.error.HTTPError as e:
                try:
                    err_body = e.read().decode()
                    return _json.loads(err_body)
                except Exception:
                    return {'status_code': e.code, 'status_message': str(e)}
            except Exception as e:
                return {'status_code': 0, 'status_message': str(e)}

        def _tmdb_search(query):
            """Search TMDB for TV series and movies. Returns results
            normalized to the same dict format as TVDB for the
            disambiguation dialog."""
            encoded_q = urllib.parse.quote(query)
            normalized = []
            seen_ids = set()

            # Search TV series
            tv_result = _tmdb_request(f'/search/tv?query={encoded_q}')
            if tv_result and 'results' in tv_result:
                for r in tv_result['results']:
                    rid = ('tv', r.get('id', ''))
                    if rid in seen_ids:
                        continue
                    seen_ids.add(rid)
                    year = ''
                    fad = r.get('first_air_date', '')
                    if fad and len(fad) >= 4:
                        year = fad[:4]
                    countries = r.get('origin_country', [])
                    country = countries[0] if countries else ''
                    poster = r.get('poster_path', '')
                    normalized.append({
                        'name': r.get('name', ''),
                        'year': year,
                        'country': country,
                        'network': 'TV Series',
                        'overview': r.get('overview', ''),
                        'thumbnail': (f'{TMDB_IMG_BASE}/w92{poster}'
                                      if poster else ''),
                        'image_url': (f'{TMDB_IMG_BASE}/w300{poster}'
                                      if poster else ''),
                        'id': r.get('id', ''),
                        'tvdb_id': '',
                        '_provider': 'tmdb',
                        '_media_type': 'tv',
                    })

            # Search movies
            movie_result = _tmdb_request(f'/search/movie?query={encoded_q}')
            if movie_result and 'results' in movie_result:
                for r in movie_result['results']:
                    rid = ('movie', r.get('id', ''))
                    if rid in seen_ids:
                        continue
                    seen_ids.add(rid)
                    year = ''
                    rd = r.get('release_date', '')
                    if rd and len(rd) >= 4:
                        year = rd[:4]
                    poster = r.get('poster_path', '')
                    normalized.append({
                        'name': r.get('title', ''),
                        'year': year,
                        'country': '',
                        'network': 'Movie',
                        'overview': r.get('overview', ''),
                        'thumbnail': (f'{TMDB_IMG_BASE}/w92{poster}'
                                      if poster else ''),
                        'image_url': (f'{TMDB_IMG_BASE}/w300{poster}'
                                      if poster else ''),
                        'id': r.get('id', ''),
                        'tvdb_id': '',
                        '_provider': 'tmdb',
                        '_media_type': 'movie',
                    })

            if not normalized:
                _log("TMDB search: no results", 'WARNING')
            else:
                _log(f"TMDB search returned {len(normalized)} results")
            return normalized

        def _tmdb_get_episodes(series_id):
            """Get all episodes for a TMDB series. Fetches show details first
            to get the number of seasons, then fetches each season.
            Returns episodes normalized to TVDB episode dict format."""
            # Get show details for season count
            details = _tmdb_request(f'/tv/{series_id}')
            if not details or 'number_of_seasons' not in details:
                msg = (details.get('status_message', 'unknown')
                       if details else 'No response')
                _log(f"TMDB show details error: {msg}", 'ERROR')
                return []
            num_seasons = details['number_of_seasons']
            all_eps = []
            for sn in range(1, num_seasons + 1):
                season_data = _tmdb_request(f'/tv/{series_id}/season/{sn}')
                if not season_data or 'episodes' not in season_data:
                    _log(f"  Season {sn}: no data")
                    continue
                for ep in season_data['episodes']:
                    all_eps.append({
                        'seasonNumber': ep.get('season_number', sn),
                        'number': ep.get('episode_number'),
                        'name': ep.get('name', ''),
                        'aired': ep.get('air_date', ''),
                        'year': (ep.get('air_date', '')[:4]
                                 if ep.get('air_date') else ''),
                    })
                _log(f"  Season {sn}: {len(season_data['episodes'])} episodes")
            return all_eps

        # ── Provider-agnostic search & episode fetch ──
        def _provider_search(query):
            """Search the active provider for a TV series."""
            prov = provider_var.get()
            if prov == 'TMDB':
                return _tmdb_search(query)
            else:
                return _tvdb_search(query)

        def _provider_get_episodes(series_id, provider=None):
            """Fetch episodes from the active (or specified) provider."""
            prov = provider or provider_var.get()
            if prov == 'TMDB':
                return _tmdb_get_episodes(series_id)
            else:
                return _tvdb_get_episodes(series_id)

        def _provider_get_series_id(result):
            """Extract the series ID from a search result dict."""
            prov = result.get('_provider', provider_var.get().lower())
            if prov == 'tmdb':
                return result.get('id', '')
            else:
                sid = result.get('tvdb_id', result.get('id', ''))
                if isinstance(sid, str) and sid.startswith('series-'):
                    sid = sid[7:]
                return sid

        # ── Episode number parser ──
        def _parse_episode_info(filename):
            """Extract season and episode numbers from a filename.
            Returns (season, episode) for single-episode files,
            (season, [ep1, ep2, ...]) for multi-episode files,
            or ('date', 'YYYY-MM-DD') for date-based episodes."""
            name = os.path.basename(filename)
            # S01E01E02, S01E01-E03, S01E01E02E03 (multi-episode)
            m = re.search(r'[Ss](\d{1,2})\s*[Ee](\d{1,3})(?:\s*-?\s*[Ee](\d{1,3}))+', name)
            if m:
                season = int(m.group(1))
                # Extract all episode numbers from the full match
                eps = [int(x) for x in re.findall(r'[Ee](\d{1,3})', m.group(0))]
                if len(eps) > 1:
                    # Check for range pattern like S01E01-E03 (fill in gaps)
                    if len(eps) == 2 and eps[1] > eps[0] + 1:
                        eps = list(range(eps[0], eps[1] + 1))
                    return season, eps
                return season, eps[0]
            # S01E01, s1e1 (single episode)
            m = re.search(r'[Ss](\d{1,2})\s*[Ee](\d{1,3})', name)
            if m:
                return int(m.group(1)), int(m.group(2))
            # 1x01, 01x01
            m = re.search(r'(\d{1,2})[xX](\d{1,3})', name)
            if m:
                return int(m.group(1)), int(m.group(2))
            # Season 1 Episode 1
            m = re.search(r'[Ss]eason\s*(\d+).*?[Ee]pisode\s*(\d+)', name)
            if m:
                return int(m.group(1)), int(m.group(2))
            # Date-based: 2026.04.22, 2026-04-22, 2026 04 22
            m = re.search(r'((?:19|20)\d{2})[.\-\s](0[1-9]|1[0-2])[.\-\s](0[1-9]|[12]\d|3[01])', name)
            if m:
                return 'date', f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
            # E01 or Ep01 (season assumed from folder or default 1)
            m = re.search(r'[Ee](?:p|pisode)?\s*(\d{1,3})', name)
            if m:
                return None, int(m.group(1))
            return None, None

        def _sanitize_filename(name):
            """Remove characters not allowed in filenames."""
            # Replace : with space (common in episode titles), strip others
            name = name.replace(':', ' ').replace('/', '-').replace('\\', '-')
            name = re.sub(r'[<>"|?*]', '', name)
            # Collapse multiple spaces
            name = re.sub(r'\s+', ' ', name).strip()
            # Remove trailing dots/spaces (Windows compatibility)
            name = name.rstrip('. ')
            return name

        def _match_file_to_show(item):
            """Match a file to one of the loaded shows by filename."""
            if not _all_shows:
                return None
            fname = os.path.splitext(os.path.basename(item['path']))[0]
            cleaned = _clean_show_name(fname).lower()
            if not cleaned:
                return None

            best_match = None
            best_score = 0.0
            for show_name in _all_shows:
                show_lower = show_name.lower()
                # Exact match
                if show_lower == cleaned:
                    return show_name
                # Show name contained in filename
                if show_lower in cleaned:
                    score = len(show_lower) / max(len(cleaned), 1)
                    if score > best_score:
                        best_score = score
                        best_match = show_name
                # Filename contained in show name
                elif cleaned in show_lower:
                    score = len(cleaned) / max(len(show_lower), 1) * 0.8
                    if score > best_score:
                        best_score = score
                        best_match = show_name

            # Word-level overlap fallback
            if best_score < 0.4:
                cleaned_words = set(cleaned.split())
                for show_name in _all_shows:
                    show_words = set(show_name.lower().split())
                    if show_words and cleaned_words:
                        overlap = len(cleaned_words & show_words) / len(show_words)
                        if overlap > best_score and overlap >= 0.5:
                            best_score = overlap
                            best_match = show_name

            return best_match if best_score >= 0.3 else None

        # ISO 639-1 → ISO 639-2/B (3-letter) mapping for subtitle language codes
        _LANG_2TO3 = {
            'en': 'eng', 'es': 'spa', 'fr': 'fre', 'de': 'ger', 'it': 'ita',
            'pt': 'por', 'ja': 'jpn', 'ko': 'kor', 'zh': 'chi', 'ru': 'rus',
            'ar': 'ara', 'hi': 'hin', 'nl': 'dut', 'sv': 'swe', 'da': 'dan',
            'no': 'nor', 'fi': 'fin', 'pl': 'pol', 'cs': 'cze', 'el': 'gre',
            'he': 'heb', 'tr': 'tur', 'th': 'tha', 'vi': 'vie', 'uk': 'ukr',
            'ro': 'rum', 'hu': 'hun', 'bg': 'bul', 'hr': 'hrv', 'sk': 'slo',
            'sl': 'slv', 'ms': 'may', 'id': 'ind', 'tl': 'fil', 'af': 'afr',
            'ca': 'cat', 'cy': 'wel', 'et': 'est', 'ga': 'gle', 'lv': 'lav',
            'lt': 'lit', 'mk': 'mac', 'mt': 'mlt', 'sq': 'alb', 'sr': 'srp',
            'sw': 'swa', 'ta': 'tam', 'te': 'tel', 'ur': 'urd', 'bn': 'ben',
        }

        def _detect_language_from_content(filepath):
            """Detect language from subtitle file content using langdetect.
            Returns a 3-letter ISO 639-2 code, or None on failure."""
            try:
                from langdetect import detect
                ext = os.path.splitext(filepath)[1].lower()
                if ext not in ('.srt', '.ass', '.ssa', '.vtt', '.sub'):
                    return None
                # Read file, try common encodings
                text = None
                for enc in ('utf-8', 'latin-1', 'cp1252'):
                    try:
                        with open(filepath, 'r', encoding=enc) as f:
                            text = f.read(8192)  # first 8KB is enough
                        break
                    except (UnicodeDecodeError, UnicodeError):
                        continue
                if not text:
                    return None
                # Strip SRT formatting (timestamps, tags, numbers)
                cleaned = re.sub(r'\d+\s*\n\d{2}:\d{2}:\d{2}[.,]\d+ --> '
                                 r'\d{2}:\d{2}:\d{2}[.,]\d+\s*\n', '', text)
                cleaned = re.sub(r'<[^>]+>', '', cleaned)
                cleaned = re.sub(r'\{[^}]+\}', '', cleaned)
                cleaned = re.sub(r'♪[^\n]*', '', cleaned)
                # Strip ASS header/style sections
                cleaned = re.sub(r'\[Script Info\].*?\[Events\]',
                                 '', cleaned, flags=re.DOTALL)
                cleaned = re.sub(r'Dialogue:\s*\d+,\d[^,]*,\d[^,]*,[^,]*,'
                                 r'[^,]*,\d+,\d+,\d+,[^,]*,', '', cleaned)
                # Collapse whitespace
                cleaned = re.sub(r'\s+', ' ', cleaned).strip()
                if len(cleaned) < 20:
                    return None
                lang_2 = detect(cleaned)
                return _LANG_2TO3.get(lang_2, lang_2)
            except Exception:
                return None

        def _detect_sub_tags(filename):
            """Detect language, forced, and SDH tags from a subtitle filename.
            Returns a string like '.eng.forced' or '.eng.sdh' to insert
            before the extension. Language is detected from filename first,
            then verified/detected from file content via langdetect."""
            stem = os.path.splitext(os.path.basename(filename))[0].lower()
            parts = stem.split('.')
            tags = []
            # Walk trailing dot-separated tokens for known tags
            # Common patterns: .eng.forced.srt, .en.sdh.srt, .forced.srt
            _LANG_CODES = {
                'en', 'eng', 'es', 'spa', 'fr', 'fra', 'fre', 'de', 'deu',
                'ger', 'it', 'ita', 'pt', 'por', 'ja', 'jpn', 'ko', 'kor',
                'zh', 'zho', 'chi', 'ru', 'rus', 'ar', 'ara', 'hi', 'hin',
                'nl', 'nld', 'dut', 'sv', 'swe', 'da', 'dan', 'no', 'nor',
                'fi', 'fin', 'pl', 'pol', 'cs', 'ces', 'cze', 'el', 'ell',
                'gre', 'he', 'heb', 'tr', 'tur', 'th', 'tha', 'vi', 'vie',
                'uk', 'ukr', 'ro', 'ron', 'rum', 'hu', 'hun', 'bg', 'bul',
                'hr', 'hrv', 'sk', 'slk', 'slo', 'sl', 'slv', 'ms', 'msa',
                'may', 'id', 'ind', 'tl', 'fil', 'und',
            }
            _TAG_WORDS = {'forced', 'sdh', 'cc', 'hi'}
            filename_lang = None
            found_tags = []
            # Scan from the end of the parts list
            for part in reversed(parts):
                p = part.strip().lower()
                if p in _TAG_WORDS:
                    found_tags.insert(0, p)
                elif p in _LANG_CODES and filename_lang is None:
                    filename_lang = p
                else:
                    break  # stop at first non-tag token

            # Normalize 2-letter filename codes to 3-letter
            if filename_lang and len(filename_lang) == 2:
                filename_lang = _LANG_2TO3.get(filename_lang, filename_lang)

            # Detect language from file content
            content_lang = _detect_language_from_content(filename)

            # Use content detection, fall back to filename, then default 'eng'
            if content_lang:
                lang = content_lang
                if filename_lang and filename_lang != content_lang:
                    _log(f"  Language: filename says '{filename_lang}', "
                         f"content detected '{content_lang}' — "
                         f"using '{content_lang}'")
            else:
                lang = filename_lang if filename_lang else 'eng'

            tags.append(lang)
            for t in found_tags:
                tags.append(t)
            return '.' + '.'.join(tags)

        def _build_new_name(item, template, show_name):
            """Build a new filename from template and episode data."""
            if not show_name:
                return None
            show_data = _all_shows.get(show_name, {})

            # ── Movie mode — no season/episode needed ──
            if isinstance(show_data, dict) and show_data.get('_is_movie'):
                year = show_data.get('_year', '')
                # For movies, use show name + year as filename
                name = f"{show_name} ({year})" if year else show_name
                ext = item['ext']
                sub_tags = ''
                if ext in SUBTITLE_EXTENSIONS:
                    sub_tags = _detect_sub_tags(item['path'])
                return _sanitize_filename(name) + sub_tags + ext

            # ── Date-based episode mode ──
            air_date = item.get('air_date')
            if air_date:
                ep_data = show_data.get(('date', air_date))
                if ep_data:
                    title = ep_data.get('name', '')
                    s = ep_data.get('seasonNumber', 1)
                    e = ep_data.get('number', 0)
                    name = template.format(
                        show=show_name,
                        season=str(s).zfill(2),
                        episode=str(e).zfill(2),
                        title=title,
                        year=ep_data.get('year', air_date[:4]),
                    )
                else:
                    # No episode data found — use date as title
                    name = f"{show_name} - {air_date}"
                ext = item['ext']
                sub_tags = ''
                if ext in SUBTITLE_EXTENSIONS:
                    sub_tags = _detect_sub_tags(item['path'])
                return _sanitize_filename(name) + sub_tags + ext

            # ── TV series mode — need season/episode ──
            s = item.get('season')
            e = item.get('episode')
            if s is None or e is None:
                return None

            # ── Multi-episode support ──
            if isinstance(e, list) and len(e) > 1:
                # Build combined episode tag: E01-E02 or E01E02E03
                first_ep, last_ep = e[0], e[-1]
                if e == list(range(first_ep, last_ep + 1)):
                    ep_tag = f"E{str(first_ep).zfill(2)}-E{str(last_ep).zfill(2)}"
                else:
                    ep_tag = ''.join(f"E{str(x).zfill(2)}" for x in e)
                # Collect titles from each episode
                titles = []
                year = ''
                for ep_num in e:
                    ep_data = show_data.get((s, ep_num))
                    if ep_data:
                        t = ep_data.get('name', '')
                        if t:
                            titles.append(t)
                        if not year:
                            year = ep_data.get('year', '')
                title = ' & '.join(titles) if titles else ''
                name = template.format(
                    show=show_name,
                    season=str(s).zfill(2),
                    episode=ep_tag,
                    title=title,
                    year=year,
                )
            else:
                # Single episode
                ep_num = e[0] if isinstance(e, list) else e
                ep_data = show_data.get((s, ep_num))
                title = ep_data.get('name', '') if ep_data else ''
                name = template.format(
                    show=show_name,
                    season=str(s).zfill(2),
                    episode=str(ep_num).zfill(2),
                    title=title,
                    year=ep_data.get('year', '') if ep_data else '',
                )
            ext = item['ext']
            # For subtitle files, preserve language/forced/SDH tags
            sub_tags = ''
            if ext in SUBTITLE_EXTENSIONS:
                sub_tags = _detect_sub_tags(item['path'])
            return _sanitize_filename(name) + sub_tags + ext

        # ── Logging ──
        def _log(msg, level='INFO'):
            log_text.configure(state='normal')
            log_text.insert('end', msg + '\n')
            log_text.see('end')
            log_text.configure(state='disabled')

        # ══════════════════════════════════════════════════════════════
        # UI Layout
        # ══════════════════════════════════════════════════════════════

        main_f = ttk.Frame(win, padding=8)
        main_f.pack(fill='both', expand=True)
        main_f.columnconfigure(1, weight=1)

        # ── API keys ──
        api_key_var = tk.StringVar(value='8903a14b-8b71-436e-a48a-d553884f2991')
        tmdb_key_var = tk.StringVar(value='9375eb1401938b7615afd69988611a74')
        provider_var = tk.StringVar(value=_saved_provider)

        def _on_provider_change(*_args):
            # Save preference
            self._tv_rename_provider = provider_var.get()
            self.save_preferences()
            # Clear loaded shows and re-search with the new provider
            _all_shows.clear()
            for item in _file_items:
                item.pop('matched_show', None)
            if _file_items:
                _auto_load_shows()
            else:
                _refresh_preview()

        def _file_items_refresh_matches():
            """Re-run matching and refresh preview after provider change."""
            for item in _file_items:
                item.pop('matched_show', None)
            _refresh_preview()

        provider_var.trace_add('write', _on_provider_change)

        # Save TMDB key on change
        def _save_tmdb_key(*_args):
            self._tmdb_api_key = tmdb_key_var.get().strip()
            self.save_preferences()

        # ── Row 0: Loaded Shows ──

        def _clean_show_name(raw):
            """Strip episode info, quality tags, and release group from a show name."""
            name = re.sub(r'[._\-]', ' ', raw).strip()
            # Truncate at episode markers (including multi-episode S01E01E02)
            name = re.sub(r'\s*[Ss]\d{1,2}\s*[Ee]\d.*', '', name)
            name = re.sub(r'\s*\d{1,2}[xX]\d.*', '', name)
            # Truncate at date-based episode markers (2026 04 22)
            name = re.sub(r'\s*(?:19|20)\d{2}\s+(?:0[1-9]|1[0-2])\s+(?:0[1-9]|[12]\d|3[01]).*', '', name)
            # Truncate at quality/resolution tags
            name = re.sub(r'\s*(?:720|1080|2160|480)[pPiI].*', '', name)
            # Truncate at common release tags
            name = re.sub(r'\s*(?:WEB|HDTV|BluRay|BDRip|DVDRip|REMUX|PROPER).*',
                          '', name, flags=re.IGNORECASE)
            # Strip trailing year (e.g. "Rise Of The Conqueror 2026")
            name = re.sub(r'\s+(?:19|20)\d{2}\s*$', '', name)
            return name.strip()

        def _remove_show_for_selected():
            """Remove the loaded show and all its matched files from the queue."""
            sel = tree.selection()
            if not sel:
                return
            removed = set()
            for iid in sel:
                idx = tree.index(iid)
                if idx < len(_file_items):
                    show = _file_items[idx].get('matched_show')
                    if show and show not in removed:
                        removed.add(show)
            if removed:
                # Remove files whose matched_show is in the removed set
                before = len(_file_items)
                _file_items[:] = [f for f in _file_items
                                  if f.get('matched_show') not in removed]
                count = before - len(_file_items)
                # Now remove the show data
                for name in removed:
                    _all_shows.pop(name, None)
                    _log(f"Removed \"{name}\" — {count} file(s) removed")
                _refresh_preview()

        def _clear_all_shows():
            """Remove all loaded shows."""
            _all_shows.clear()
            _refresh_preview()
            _log("All shows cleared")

        def _ask_user_pick_show(query, candidates):
            """Show a dialog for the user to pick from multiple show matches.
            candidates: list of dicts from TVDB search results.
            Returns the chosen dict, or None if cancelled."""
            dlg = tk.Toplevel(win)
            dlg.title("Multiple Matches")
            dlg.geometry("700x500")
            dlg.minsize(500, 350)
            dlg.resizable(True, True)
            dlg.transient(win)
            dlg.grab_set()

            ttk.Label(dlg, text=f"Multiple shows found for \"{query}\":",
                      font=('Helvetica', 11, 'bold'),
                      padding=(10, 10, 10, 4)).pack(anchor='w')

            # ── Scrollable list area ──
            outer_f = ttk.Frame(dlg)
            outer_f.pack(fill='both', expand=True, padx=10, pady=4)

            canvas = tk.Canvas(outer_f, highlightthickness=0)
            scrollbar = ttk.Scrollbar(outer_f, orient='vertical',
                                       command=canvas.yview)
            scroll_frame = ttk.Frame(canvas)

            scroll_frame.bind('<Configure>',
                              lambda e: canvas.configure(
                                  scrollregion=canvas.bbox('all')))
            canvas_win = canvas.create_window((0, 0), window=scroll_frame,
                                               anchor='nw')
            canvas.configure(yscrollcommand=scrollbar.set)

            # Make scroll_frame fill canvas width on resize
            def _on_canvas_resize(event):
                canvas.itemconfig(canvas_win, width=event.width)
            canvas.bind('<Configure>', _on_canvas_resize)

            canvas.pack(side='left', fill='both', expand=True)
            scrollbar.pack(side='right', fill='y')

            # Mousewheel scrolling
            def _on_mousewheel(event):
                canvas.yview_scroll(int(-1 * (event.delta / 120)), 'units')
            def _on_button4(event):
                canvas.yview_scroll(-3, 'units')
            def _on_button5(event):
                canvas.yview_scroll(3, 'units')
            canvas.bind_all('<MouseWheel>', _on_mousewheel)
            canvas.bind_all('<Button-4>', _on_button4)
            canvas.bind_all('<Button-5>', _on_button5)

            chosen = [None]
            selected_idx = [0]
            row_frames = []
            _thumb_refs = []  # prevent GC of PhotoImages

            def _select_row(idx):
                """Highlight the selected row."""
                selected_idx[0] = idx
                for i, rf in enumerate(row_frames):
                    if i == idx:
                        rf.configure(style='Selected.TFrame')
                        for child in rf.winfo_children():
                            try:
                                child.configure(style='Selected.TLabel')
                            except Exception:
                                pass
                    else:
                        rf.configure(style='TFrame')
                        for child in rf.winfo_children():
                            try:
                                child.configure(style='TLabel')
                            except Exception:
                                pass

            # Style for selected row
            style = ttk.Style()
            style.configure('Selected.TFrame', background='#3a6ea5')
            style.configure('Selected.TLabel', background='#3a6ea5',
                            foreground='white')

            def _ok():
                chosen[0] = candidates[selected_idx[0]]
                dlg.destroy()

            # ── Build show cards ──
            for i, r in enumerate(candidates):
                name = r.get('name', r.get('objectName', ''))
                year = r.get('year', '')
                country = r.get('country', '').upper()
                network = r.get('network', '')
                overview = r.get('overview', '')

                title = f"{name} ({year})" if year else name
                meta_parts = []
                if country:
                    meta_parts.append(country)
                if network:
                    meta_parts.append(network)
                meta_line = '  |  '.join(meta_parts)

                row_f = ttk.Frame(scroll_frame, padding=(8, 6),
                                  relief='flat')
                row_f.pack(fill='x', padx=2, pady=2)
                row_f.columnconfigure(1, weight=1)
                row_frames.append(row_f)

                # Click to select
                def _click(event, idx=i):
                    _select_row(idx)
                def _dblclick(event, idx=i):
                    _select_row(idx)
                    _ok()
                row_f.bind('<Button-1>', _click)
                row_f.bind('<Double-1>', _dblclick)

                # Thumbnail placeholder (load async later)
                thumb_label = ttk.Label(row_f, text='', width=10)
                thumb_label.grid(row=0, column=0, rowspan=3, sticky='n',
                                 padx=(0, 10), pady=2)
                thumb_label.bind('<Button-1>', _click)
                thumb_label.bind('<Double-1>', _dblclick)

                # Title
                title_lbl = ttk.Label(row_f, text=title,
                                      font=('Helvetica', 11, 'bold'))
                title_lbl.grid(row=0, column=1, sticky='w')
                title_lbl.bind('<Button-1>', _click)
                title_lbl.bind('<Double-1>', _dblclick)

                # Meta line (country | network)
                if meta_line:
                    meta_lbl = ttk.Label(row_f, text=meta_line,
                                         font=('Helvetica', 9),
                                         foreground='#888')
                    meta_lbl.grid(row=1, column=1, sticky='w')
                    meta_lbl.bind('<Button-1>', _click)
                    meta_lbl.bind('<Double-1>', _dblclick)

                # Overview (show synopsis)
                if overview:
                    ov_lbl = ttk.Label(row_f, text=overview,
                                       wraplength=500,
                                       font=('Helvetica', 9),
                                       justify='left')
                    ov_lbl.grid(row=2, column=1, sticky='w', pady=(2, 0))
                    ov_lbl.bind('<Button-1>', _click)
                    ov_lbl.bind('<Double-1>', _dblclick)

                # Separator between cards
                if i < len(candidates) - 1:
                    ttk.Separator(scroll_frame, orient='horizontal').pack(
                        fill='x', padx=8, pady=0)

            # Select first row
            if row_frames:
                _select_row(0)

            # ── Load thumbnails in background ──
            def _load_thumbs():
                import io
                for i, r in enumerate(candidates):
                    thumb_url = r.get('thumbnail', '')
                    if not thumb_url:
                        continue
                    try:
                        req = urllib.request.Request(thumb_url, headers={
                            'User-Agent': 'DocflixVideoConverter/1.8'})
                        resp = urllib.request.urlopen(req, timeout=5)
                        img_data = resp.read()
                        # Schedule PhotoImage creation on the main thread
                        dlg.after(0, _apply_thumb, i, img_data)
                    except Exception:
                        pass

            def _apply_thumb(idx, img_data):
                """Create PhotoImage and apply to widget (must run on main thread)."""
                try:
                    import io
                    from PIL import Image, ImageTk
                    img = Image.open(io.BytesIO(img_data))
                    img.thumbnail((60, 90), Image.LANCZOS)
                    photo = ImageTk.PhotoImage(img)
                    _thumb_refs.append(photo)
                    rf = row_frames[idx]
                    for child in rf.grid_slaves(row=0, column=0):
                        child.configure(image=photo, width=0)
                        break
                except Exception:
                    pass

            thumb_thread = threading.Thread(target=_load_thumbs, daemon=True)
            thumb_thread.start()

            # ── Buttons ──
            btn_f = ttk.Frame(dlg, padding=(10, 6))
            btn_f.pack(fill='x')
            ttk.Button(btn_f, text="Load", command=_ok,
                       width=10).pack(side='left', padx=4)

            # Unbind mousewheel on close to prevent leaking into parent
            def _on_close():
                canvas.unbind_all('<MouseWheel>')
                canvas.unbind_all('<Button-4>')
                canvas.unbind_all('<Button-5>')
                dlg.destroy()
            dlg.protocol('WM_DELETE_WINDOW', _on_close)

            self._center_on_main(dlg)
            win.wait_window(dlg)
            return chosen[0]

        def _load_show_by_name(query):
            """Search the active provider for a show name and auto-load the
            best match. Prompts the user if multiple shows share the same name.
            Returns the loaded show name, or None on failure."""
            if not query:
                return None
            prov = provider_var.get()
            results = _provider_search(query)
            if not results:
                _log(f"  No {prov} results for \"{query}\"", 'WARNING')
                return None

            # Check if there are multiple results with the same/similar name
            # First collect both exact matches AND close matches (name contains
            # query or vice versa), then decide whether to prompt the user.
            # This catches cases like "Ghosts" returning "Ghosts", "Ghosts (US)",
            # "Ghosts (2019)", "Ghosts (DE)" — all should be presented.
            query_lower = query.lower()
            close_matches = []
            seen_ids = set()
            for r in results[:15]:  # limit to top 15
                rname = r.get('name', r.get('objectName', '')).lower()
                rid = r.get('tvdb_id', r.get('id', ''))
                if rid in seen_ids:
                    continue
                if rname == query_lower or query_lower in rname or rname in query_lower:
                    close_matches.append(r)
                    seen_ids.add(rid)

            if len(close_matches) > 1:
                # Multiple shows match — ask the user to pick
                _log(f"  Found {len(close_matches)} matches for \"{query}\" — asking...")
                win.update_idletasks()
                best = _ask_user_pick_show(query, close_matches)
                if not best:
                    _log(f"  Skipped \"{query}\"")
                    return None
            elif len(close_matches) == 1:
                best = close_matches[0]
            else:
                best = results[0]

            show_name = best.get('name', best.get('objectName', ''))
            if show_name in _all_shows:
                _log(f"  \"{show_name}\" already loaded")
                return show_name

            series_id = _provider_get_series_id(best)
            media_type = best.get('_media_type', best.get('type', 'series'))

            if media_type == 'movie':
                # Movies have no episodes — store a single entry
                year = best.get('year', '')
                _all_shows[show_name] = {
                    '_is_movie': True,
                    '_year': year,
                    '_name': show_name,
                }
                _log(f"  Loaded movie \"{show_name}\" ({year})")
                return show_name

            eps = _provider_get_episodes(series_id)
            if not eps:
                _log(f"  No episodes found for \"{show_name}\"", 'WARNING')
                return None

            show_eps = {}
            seasons = set()
            for ep in eps:
                s = ep.get('seasonNumber')
                e = ep.get('number')
                if s is not None and e is not None:
                    show_eps[(s, e)] = ep
                    seasons.add(s)
                # Also index by air date for date-based episodes
                aired = ep.get('aired') or ep.get('air_date') or ''
                if aired and len(aired) >= 10:
                    show_eps[('date', aired[:10])] = ep

            _all_shows[show_name] = show_eps
            real_seasons = {s for s in seasons if s > 0} or seasons
            _log(f"  Loaded \"{show_name}\" — {len(eps)} eps, "
                 f"{len(real_seasons)} seasons")
            return show_name

        def _auto_load_shows():
            """Detect unique show names from file list and load them all
            in a background thread with progress indication."""
            if not _file_items:
                _log("No files loaded — add files first", 'WARNING')
                return

            # Extract unique show names from filenames
            show_names = set()
            for item in _file_items:
                fname = os.path.splitext(os.path.basename(item['path']))[0]
                cleaned = _clean_show_name(fname).strip()
                if cleaned:
                    show_names.add(cleaned)

            if not show_names:
                _log("Could not detect any show names from filenames", 'WARNING')
                return

            # Filter out names that are already matched by a loaded show
            to_search = set()
            for name in show_names:
                already = False
                name_lower = name.lower()
                for loaded in _all_shows:
                    if loaded.lower() in name_lower or name_lower in loaded.lower():
                        already = True
                        break
                if not already:
                    to_search.add(name)

            if not to_search:
                _log(f"All {len(show_names)} detected shows are already loaded")
                _refresh_preview()
                return

            total = len(to_search)
            _log(f"Auto-loading {total} show(s) from {provider_var.get()}...")

            # ── Progress bar ──
            prog_f = ttk.Frame(main_f)
            prog_f.grid(row=4, column=0, columnspan=3, sticky='ew',
                        padx=4, pady=(2, 0))
            prog_lbl = ttk.Label(prog_f, text="Loading shows...",
                                 font=('Helvetica', 9))
            prog_lbl.pack(side='left', padx=(0, 8))
            prog_bar = ttk.Progressbar(prog_f, maximum=total, mode='determinate')
            prog_bar.pack(side='left', fill='x', expand=True)

            _api_cancel = [False]

            def _cancel_load():
                _api_cancel[0] = True
                cancel_btn_api.configure(state='disabled')

            cancel_btn_api = ttk.Button(prog_f, text="Cancel",
                                        command=_cancel_load, width=7)
            cancel_btn_api.pack(side='right', padx=(4, 0))

            def _worker():
                loaded_count = 0
                for i, name in enumerate(sorted(to_search)):
                    if _api_cancel[0]:
                        win.after(0, lambda: _log("Auto-load cancelled", 'WARNING'))
                        break
                    win.after(0, lambda n=name: _log(f"Searching: \"{n}\"..."))
                    win.after(0, lambda n=name, idx=i:
                              (prog_lbl.configure(
                                  text=f"Loading {idx + 1}/{total}: {n}"),
                               prog_bar.configure(value=idx)))
                    try:
                        # _load_show_by_name may open a picker dialog,
                        # which needs to run on the main thread
                        import queue
                        result_q = queue.Queue()

                        def _do_load(q=name):
                            try:
                                r = _load_show_by_name(q)
                                result_q.put(('ok', r))
                            except Exception as ex:
                                result_q.put(('error', ex))

                        win.after(0, _do_load)
                        # Wait for result (check periodically)
                        result = None
                        while True:
                            try:
                                status, val = result_q.get(timeout=0.1)
                                if status == 'ok':
                                    result = val
                                else:
                                    raise val
                                break
                            except queue.Empty:
                                if _api_cancel[0]:
                                    break
                                continue
                        if _api_cancel[0]:
                            break
                        if result:
                            loaded_count += 1
                    except Exception as e:
                        win.after(0, lambda n=name, err=e:
                                  _log(f"  Error loading \"{n}\": {err}", 'ERROR'))

                def _finish(cnt=loaded_count, tot=total):
                    prog_f.destroy()
                    _log(f"Auto-load complete: {cnt}/{tot} shows loaded",
                         'SUCCESS')
                    _refresh_preview()
                win.after(0, _finish)

            t = threading.Thread(target=_worker, daemon=True)
            t.start()

        template_var = tk.StringVar(value=_saved_template)

        # Save template on change
        def _on_template_change(*_):
            self._tv_rename_template = template_var.get()
            self.save_preferences()
            _refresh_preview()
        template_var.trace_add('write', _on_template_change)

        # ── Row 1: File list (treeview) ──
        tree_f = ttk.Frame(main_f)
        tree_f.grid(row=1, column=0, columnspan=3, sticky='nsew', padx=4, pady=4)
        main_f.rowconfigure(1, weight=1)

        columns = ('current', 'new_name')
        tree = ttk.Treeview(tree_f, columns=columns, show='headings',
                            selectmode='extended')
        tree.heading('current', text='Current Filename')
        tree.heading('new_name', text='New Filename')
        tree.column('current', width=350, minwidth=150)
        tree.column('new_name', width=400, minwidth=150)

        tree_scroll = ttk.Scrollbar(tree_f, orient='vertical', command=tree.yview)
        tree.configure(yscrollcommand=tree_scroll.set)
        tree.pack(side='left', fill='both', expand=True)
        tree_scroll.pack(side='right', fill='y')

        def _refresh_preview():
            """Update the treeview with current/new filenames."""
            tree.delete(*tree.get_children())
            template = template_var.get().strip()

            for item in _file_items:
                cur_name = os.path.basename(item['path'])
                s = item.get('season')
                e = item.get('episode')

                # Match file to a loaded show
                matched = _match_file_to_show(item)
                item['matched_show'] = matched

                new_name = ''
                is_movie = (isinstance(_all_shows.get(matched), dict)
                            and _all_shows.get(matched, {}).get('_is_movie'))
                has_ep = (s is not None and e is not None)
                has_date = item.get('air_date') is not None
                if matched and (is_movie or has_ep or has_date):
                    try:
                        new_name = _build_new_name(item, template, matched) or ''
                    except (KeyError, ValueError):
                        new_name = '(template error)'

                iid = tree.insert('', 'end',
                                  values=(cur_name, new_name))
                # Color rows without matches
                if not new_name or new_name == '(template error)':
                    tree.item(iid, tags=('nomatch',))

            tree.tag_configure('nomatch', foreground='#999')
            # Update undo button state
            try:
                undo_btn.configure(
                    state='normal' if _rename_history else 'disabled')
            except Exception:
                pass

        # ── Right-click context menu ──
        _tree_ctx = tk.Menu(tree, tearoff=0)

        def _open_containing_folder():
            """Open the folder containing the selected file."""
            sel = tree.selection()
            if not sel:
                return
            idx = tree.index(sel[0])
            if idx < len(_file_items):
                folder = os.path.dirname(_file_items[idx]['path'])
                try:
                    subprocess.Popen(['xdg-open', folder])
                except Exception:
                    pass

        def _copy_new_name():
            """Copy the new filename of the selected file to clipboard."""
            sel = tree.selection()
            if not sel:
                return
            vals = tree.item(sel[0], 'values')
            if vals and len(vals) > 1 and vals[1]:
                win.clipboard_clear()
                win.clipboard_append(vals[1])

        def _on_tree_right_click(event):
            iid = tree.identify_row(event.y)
            if iid:
                if iid not in tree.selection():
                    tree.selection_set(iid)
            _tree_ctx.delete(0, 'end')
            sel = tree.selection()
            if sel:
                idx = tree.index(sel[0])
                # ── Per-file actions ──
                _tree_ctx.add_command(
                    label="Set Episode...",
                    command=_set_episode_for_selected)
                # Copy new name
                vals = tree.item(sel[0], 'values')
                if vals and len(vals) > 1 and vals[1]:
                    _tree_ctx.add_command(
                        label="Copy New Name",
                        command=_copy_new_name)
                _tree_ctx.add_command(
                    label="Open Folder",
                    command=_open_containing_folder)
                _tree_ctx.add_separator()
                _tree_ctx.add_command(
                    label=f"Remove Selected ({len(sel)} file{'s' if len(sel) > 1 else ''})",
                    command=_remove_selected_files)
                # "Remove show" — unload the matched show for the selected file
                if idx < len(_file_items):
                    show = _file_items[idx].get('matched_show')
                    if show:
                        _tree_ctx.add_command(
                            label=f"Remove show \"{show}\"",
                            command=_remove_show_for_selected)
                _tree_ctx.add_separator()
            _tree_ctx.add_command(label="Clear all files",
                                 command=_clear_files)
            _tree_ctx.tk_popup(event.x_root, event.y_root)

        def _remove_selected_files():
            """Remove selected files from the queue."""
            sel = tree.selection()
            if not sel:
                return
            # Get indices in reverse order to avoid shifting
            indices = sorted([tree.index(iid) for iid in sel], reverse=True)
            for idx in indices:
                if idx < len(_file_items):
                    _file_items.pop(idx)
            _log(f"Removed {len(indices)} file(s)")
            # Remove shows that no longer have any files matched
            remaining_shows = {f.get('matched_show') for f in _file_items
                               if f.get('matched_show')}
            orphaned = [s for s in _all_shows if s not in remaining_shows]
            for s in orphaned:
                _all_shows.pop(s, None)
            _refresh_preview()

        tree.bind('<Button-3>', _on_tree_right_click)

        # ── Drag and drop ──
        _RENAME_EXTENSIONS = VIDEO_EXTENSIONS | SUBTITLE_EXTENSIONS

        def _add_paths(paths):
            """Add files/folders to the file list. Recursively scans folders."""
            added = 0
            for p in paths:
                if os.path.isdir(p):
                    for root_dir, _dirs, files in os.walk(p):
                        _dirs.sort()
                        for f in sorted(files):
                            fp = os.path.join(root_dir, f)
                            ext = os.path.splitext(f)[1].lower()
                            if ext in _RENAME_EXTENSIONS:
                                s, e = _parse_episode_info(f)
                                item = {'path': fp, 'season': s,
                                        'episode': e, 'ext': ext}
                                if s == 'date':
                                    item['air_date'] = e
                                    item['season'] = None
                                    item['episode'] = None
                                _file_items.append(item)
                                added += 1
                elif os.path.isfile(p):
                    ext = os.path.splitext(p)[1].lower()
                    if ext in _RENAME_EXTENSIONS:
                        s, e = _parse_episode_info(p)
                        item = {'path': p, 'season': s,
                                'episode': e, 'ext': ext}
                        if s == 'date':
                            item['air_date'] = e
                            item['season'] = None
                            item['episode'] = None
                        _file_items.append(item)
                        added += 1
            _v = sum(1 for i in _file_items if i['ext'] in VIDEO_EXTENSIONS)
            _s = sum(1 for i in _file_items if i['ext'] in SUBTITLE_EXTENSIONS)
            _log(f"Added {added} files ({_v} video, {_s} subtitle)")
            # Auto-load any new shows detected from the added files
            has_key = (api_key_var.get().strip()
                       if provider_var.get() == 'TVDB'
                       else tmdb_key_var.get().strip())
            if added > 0 and has_key:
                _auto_load_shows()
            elif added > 0 and provider_var.get() == 'TMDB' and not tmdb_key_var.get().strip():
                _log("TMDB selected but no API key entered. "
                     "Get a free key at themoviedb.org", 'WARNING')
                _refresh_preview()
            else:
                _refresh_preview()

        def _on_drop(event):
            raw = event.data
            paths = []
            if 'file://' in raw:
                from urllib.parse import unquote, urlparse
                for line in raw.splitlines():
                    line = line.strip()
                    if line.startswith('file://'):
                        decoded = unquote(urlparse(line).path)
                        if decoded:
                            paths.append(decoded)
            else:
                i = 0
                while i < len(raw):
                    if raw[i] == '{':
                        end = raw.find('}', i)
                        paths.append(raw[i + 1:end])
                        i = end + 2
                    elif raw[i] == ' ':
                        i += 1
                    else:
                        end = raw.find(' ', i)
                        if end == -1:
                            end = len(raw)
                        paths.append(raw[i:end])
                        i = end + 1
            if paths:
                _add_paths(paths)

        try:
            win.drop_target_register(DND_FILES)
            win.dnd_bind('<<Drop>>', _on_drop)
        except Exception:
            pass

        # ── Row 2: Buttons ──
        btn_f = ttk.Frame(main_f)
        btn_f.grid(row=2, column=0, columnspan=3, sticky='ew', padx=4, pady=(4, 0))

        def _do_rename():
            """Rename all files with valid new names."""
            template = template_var.get().strip()
            if not template:
                messagebox.showwarning("No Template", "Enter a filename template.",
                                       parent=win)
                return
            renamed = 0
            skipped = 0
            errors = 0
            batch_history = []  # [(old_path, new_path), ...]
            for item in _file_items:
                try:
                    matched = item.get('matched_show')
                    if not matched:
                        skipped += 1
                        continue
                    new_name = _build_new_name(item, template, matched)
                    if not new_name:
                        skipped += 1
                        continue
                    old_path = item['path']
                    new_path = os.path.join(os.path.dirname(old_path), new_name)
                    if old_path == new_path:
                        continue
                    if os.path.exists(new_path):
                        _log(f"Skipped (exists): {new_name}", 'WARNING')
                        skipped += 1
                        continue
                    os.rename(old_path, new_path)
                    batch_history.append((old_path, new_path))
                    item['path'] = new_path
                    renamed += 1
                except Exception as e:
                    _log(f"Error renaming: {e}", 'ERROR')
                    errors += 1
            # Save undo history
            if batch_history:
                _rename_history.append(batch_history)
            parts = [f"Renamed {renamed} files"]
            if skipped:
                parts.append(f"{skipped} skipped (no match)")
            if errors:
                parts.append(f"{errors} errors")
            msg = " — ".join(parts)
            _log(msg, 'SUCCESS')
            _refresh_preview()
            if errors:
                messagebox.showwarning("Rename Complete", msg, parent=win)
            else:
                messagebox.showinfo("Rename Complete", msg, parent=win)

        def _do_undo():
            """Undo the last rename batch."""
            if not _rename_history:
                _log("Nothing to undo", 'WARNING')
                return
            batch = _rename_history.pop()
            undone = 0
            errors = 0
            # Reverse in reverse order for safety
            for old_path, new_path in reversed(batch):
                try:
                    if os.path.exists(new_path) and not os.path.exists(old_path):
                        os.rename(new_path, old_path)
                        # Update _file_items to reflect the old path
                        for item in _file_items:
                            if item['path'] == new_path:
                                item['path'] = old_path
                                break
                        undone += 1
                    else:
                        _log(f"Cannot undo: {os.path.basename(new_path)}", 'WARNING')
                        errors += 1
                except Exception as e:
                    _log(f"Undo error: {e}", 'ERROR')
                    errors += 1
            msg = f"Undone {undone} rename(s)"
            if errors:
                msg += f" ({errors} errors)"
            _log(msg, 'SUCCESS' if not errors else 'WARNING')
            _refresh_preview()

        def _set_episode_for_selected():
            """Open a dialog to manually set season/episode for selected files."""
            sel = tree.selection()
            if not sel:
                return
            idx = tree.index(sel[0])
            if idx >= len(_file_items):
                return
            item = _file_items[idx]

            dlg = tk.Toplevel(win)
            dlg.title("Set Episode")
            dlg.geometry("320x180")
            dlg.resizable(False, False)
            dlg.transient(win)
            dlg.grab_set()
            self._center_on_main(dlg)

            f = ttk.Frame(dlg, padding=16)
            f.pack(fill='both', expand=True)

            ttk.Label(f, text=os.path.basename(item['path']),
                      font=('Helvetica', 9), wraplength=280).grid(
                          row=0, column=0, columnspan=2, sticky='w', pady=(0, 10))

            cur_s = item.get('season')
            cur_e = item.get('episode')
            # For multi-episode, show the first episode
            if isinstance(cur_e, list):
                cur_e = cur_e[0] if cur_e else ''

            ttk.Label(f, text="Season:").grid(row=1, column=0, sticky='w', pady=4)
            s_var = tk.StringVar(value=str(cur_s) if cur_s is not None else '')
            s_entry = ttk.Entry(f, textvariable=s_var, width=8)
            s_entry.grid(row=1, column=1, sticky='w', padx=(8, 0), pady=4)

            ttk.Label(f, text="Episode:").grid(row=2, column=0, sticky='w', pady=4)
            e_var = tk.StringVar(value=str(cur_e) if cur_e is not None else '')
            e_entry = ttk.Entry(f, textvariable=e_var, width=8)
            e_entry.grid(row=2, column=1, sticky='w', padx=(8, 0), pady=4)

            def _apply():
                try:
                    sv = s_var.get().strip()
                    ev = e_var.get().strip()
                    new_s = int(sv) if sv else None
                    new_e = int(ev) if ev else None
                except ValueError:
                    messagebox.showwarning("Invalid", "Enter valid numbers.",
                                           parent=dlg)
                    return
                # Apply to all selected files
                for iid in sel:
                    i = tree.index(iid)
                    if i < len(_file_items):
                        _file_items[i]['season'] = new_s
                        _file_items[i]['episode'] = new_e
                dlg.destroy()
                _refresh_preview()

            btn_f = ttk.Frame(f)
            btn_f.grid(row=3, column=0, columnspan=2, sticky='e', pady=(12, 0))
            ttk.Button(btn_f, text="Apply", command=_apply,
                       width=8).pack(side='right', padx=(4, 0))
            ttk.Button(btn_f, text="Cancel", command=dlg.destroy,
                       width=8).pack(side='right')

            s_entry.focus_set()
            s_entry.select_range(0, 'end')
            dlg.wait_window()

        rename_btn = ttk.Button(btn_f, text="✏ Rename All", command=_do_rename,
                                width=12)
        rename_btn.pack(side='left', padx=2)
        _create_tooltip(rename_btn, "Rename all files to their new names")

        undo_btn = ttk.Button(btn_f, text="↩ Undo", command=_do_undo,
                              width=8, state='disabled')
        undo_btn.pack(side='left', padx=2)
        _create_tooltip(undo_btn, "Undo the last rename operation")

        def _clear_files():
            _file_items.clear()
            _all_shows.clear()
            _rename_history.clear()
            tree.delete(*tree.get_children())
            _log("File list cleared")
            undo_btn.configure(state='disabled')

        clear_btn = ttk.Button(btn_f, text="Clear", command=_clear_files, width=8)
        clear_btn.pack(side='left', padx=2)
        _create_tooltip(clear_btn, "Remove all files from the list")

        def _browse_files():
            paths = filedialog.askopenfilenames(
                parent=win, title="Select Video Files",
                filetypes=[("Video files", "*.mkv *.mp4 *.avi *.mov *.ts *.m2ts"),
                           ("All files", "*.*")])
            if paths:
                _add_paths(list(paths))

        def _browse_folder():
            path = filedialog.askdirectory(parent=win, title="Select Folder")
            if path:
                _add_paths([path])

        # ── Row 3: Log ──
        log_f = ttk.LabelFrame(main_f, text="Log", padding=4)
        log_f.grid(row=3, column=0, columnspan=3, sticky='nsew', padx=4, pady=(4, 0))
        main_f.rowconfigure(3, weight=1)
        log_text = tk.Text(log_f, height=4, wrap='word', font=('Courier', 9),
                           state='disabled', bg='#1e1e1e', fg='#d4d4d4')
        log_scroll = ttk.Scrollbar(log_f, orient='vertical', command=log_text.yview)
        log_text.configure(yscrollcommand=log_scroll.set)
        log_text.pack(side='left', fill='both', expand=True)
        log_scroll.pack(side='right', fill='y')

        def _clear_log():
            log_text.configure(state='normal')
            log_text.delete('1.0', 'end')
            log_text.configure(state='disabled')

        clear_log_btn = ttk.Button(log_f, text="Clear Log", command=_clear_log, width=8)
        clear_log_btn.pack(side='bottom', anchor='e', pady=(4, 0))

        # ══════════════════════════════════════════════════════════════
        # Menu Bar
        # ══════════════════════════════════════════════════════════════

        menubar = tk.Menu(win)
        win.configure(menu=menubar)

        # ── File menu ──
        file_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="File", menu=file_menu)
        file_menu.add_command(label="Add Files...", command=_browse_files,
                              accelerator="Ctrl+O")
        file_menu.add_command(label="Add Folder...", command=_browse_folder,
                              accelerator="Ctrl+Shift+O")
        file_menu.add_separator()
        file_menu.add_command(label="Rename All", command=_do_rename,
                              accelerator="Ctrl+R")
        file_menu.add_separator()
        file_menu.add_command(label="Clear All", command=_clear_files)
        file_menu.add_command(label="Clear Log", command=_clear_log)
        file_menu.add_separator()
        file_menu.add_command(label="Close", command=win.destroy,
                              accelerator="Ctrl+W")

        # ── Edit menu ──
        edit_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Edit", menu=edit_menu)
        edit_menu.add_command(label="Undo Rename", command=_do_undo,
                              accelerator="Ctrl+Z")
        edit_menu.add_separator()
        edit_menu.add_command(label="Set Episode...",
                              command=_set_episode_for_selected)
        edit_menu.add_separator()
        edit_menu.add_command(label="Select All",
                              command=lambda: tree.selection_set(
                                  tree.get_children()),
                              accelerator="Ctrl+A")
        edit_menu.add_command(label="Remove Selected",
                              command=_remove_selected_files,
                              accelerator="Delete")

        # ── Settings menu ──
        settings_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Settings", menu=settings_menu)

        # Provider submenu
        provider_menu = tk.Menu(settings_menu, tearoff=0)
        settings_menu.add_cascade(label="Provider", menu=provider_menu)
        provider_menu.add_radiobutton(label="TVDB", variable=provider_var,
                                       value='TVDB')
        provider_menu.add_radiobutton(label="TMDB", variable=provider_var,
                                       value='TMDB')

        # Template dialog
        def _open_template_settings():
            dlg = tk.Toplevel(win)
            dlg.title("Filename Template")
            dlg.geometry("520x420")
            dlg.minsize(450, 380)
            dlg.resizable(True, True)
            dlg.transient(win)
            dlg.grab_set()
            self._center_on_main(dlg)

            f = ttk.Frame(dlg, padding=20)
            f.pack(fill='both', expand=True)

            ttk.Label(f, text="Filename template for TV episodes:",
                      font=('Helvetica', 11)).grid(
                          row=0, column=0, columnspan=2, sticky='w', pady=(0, 10))

            ttk.Label(f, text="Template:").grid(
                row=1, column=0, sticky='w', padx=(0, 8))
            t_entry = ttk.Entry(f, textvariable=template_var, width=50,
                                font=('Helvetica', 10))
            t_entry.grid(row=1, column=1, sticky='ew')
            f.columnconfigure(1, weight=1)

            ttk.Label(f, text="Available variables:",
                      font=('Helvetica', 10, 'bold')).grid(
                          row=2, column=0, columnspan=2, sticky='w',
                          pady=(16, 6))

            vars_text = (
                "{show}       — Show name\n"
                "{season}     — Season number (zero-padded)\n"
                "{episode}    — Episode number (zero-padded)\n"
                "{title}      — Episode title\n"
                "{year}       — Air year"
            )
            ttk.Label(f, text=vars_text, font=('Courier', 10),
                      justify='left').grid(
                          row=3, column=0, columnspan=2, sticky='w',
                          padx=(15, 0))

            # Preset templates
            ttk.Label(f, text="Presets:",
                      font=('Helvetica', 9, 'bold')).grid(
                          row=4, column=0, columnspan=2, sticky='w',
                          pady=(12, 4))

            presets = [
                ('{show} S{season}E{episode} {title}',
                 'Show S01E01 Title'),
                ('{show} - S{season}E{episode} - {title}',
                 'Show - S01E01 - Title'),
                ('{show} {season}x{episode} {title}',
                 'Show 01x01 Title'),
                ('{show} - {season}x{episode} - {title}',
                 'Show - 01x01 - Title'),
            ]
            for i, (tmpl, desc) in enumerate(presets):
                def _set(t=tmpl):
                    template_var.set(t)
                ttk.Button(f, text=desc, command=_set, width=30).grid(
                    row=5 + i, column=0, columnspan=2, sticky='w',
                    padx=(10, 0), pady=1)

            ttk.Button(f, text="Close", command=dlg.destroy,
                       width=8).grid(row=5 + len(presets), column=1,
                                     sticky='e', pady=(12, 0))

            dlg.wait_window()

        settings_menu.add_command(label="Filename Template...",
                                  command=_open_template_settings)

        # TMDB Key dialog
        def _open_api_key_settings():
            dlg = tk.Toplevel(win)
            dlg.title("API Keys")
            dlg.geometry("520x320")
            dlg.minsize(450, 280)
            dlg.resizable(True, True)
            dlg.transient(win)
            dlg.grab_set()
            self._center_on_main(dlg)

            f = ttk.Frame(dlg, padding=20)
            f.pack(fill='both', expand=True)
            f.columnconfigure(1, weight=1)

            # ── TVDB ──
            ttk.Label(f, text="TVDB API Key:",
                      font=('Helvetica', 10, 'bold')).grid(
                          row=0, column=0, columnspan=2, sticky='w',
                          pady=(0, 4))
            tvdb_entry = ttk.Entry(f, textvariable=api_key_var, width=45)
            tvdb_entry.grid(row=1, column=0, columnspan=2, sticky='ew',
                            pady=(0, 2))

            tvdb_link = ttk.Label(
                f, text="Get a free key at thetvdb.com/dashboard/account/apikey",
                foreground='#3a6ea5', font=('Helvetica', 9, 'underline'),
                cursor='hand2')
            tvdb_link.grid(row=2, column=0, columnspan=2, sticky='w',
                           pady=(0, 16))
            tvdb_link.bind('<Button-1>', lambda e: subprocess.Popen(
                ['xdg-open', 'https://thetvdb.com/dashboard/account/apikey']))

            # ── TMDB ──
            ttk.Label(f, text="TMDB API Key (v3):",
                      font=('Helvetica', 10, 'bold')).grid(
                          row=3, column=0, columnspan=2, sticky='w',
                          pady=(0, 4))
            tmdb_entry = ttk.Entry(f, textvariable=tmdb_key_var, width=45)
            tmdb_entry.grid(row=4, column=0, columnspan=2, sticky='ew',
                            pady=(0, 2))

            tmdb_link = ttk.Label(
                f, text="Get a free key at themoviedb.org/settings/api",
                foreground='#3a6ea5', font=('Helvetica', 9, 'underline'),
                cursor='hand2')
            tmdb_link.grid(row=5, column=0, columnspan=2, sticky='w',
                           pady=(0, 16))
            tmdb_link.bind('<Button-1>', lambda e: subprocess.Popen(
                ['xdg-open', 'https://www.themoviedb.org/settings/api']))

            def _save_and_close():
                self._tvdb_api_key = api_key_var.get().strip()
                self._tmdb_api_key = tmdb_key_var.get().strip()
                self.save_preferences()
                _log("API keys saved")
                dlg.destroy()

            btn_f = ttk.Frame(f)
            btn_f.grid(row=6, column=0, columnspan=2, sticky='e',
                       pady=(8, 0))
            ttk.Button(btn_f, text="Save", command=_save_and_close,
                       width=8).pack(side='right', padx=(4, 0))
            ttk.Button(btn_f, text="Cancel", command=dlg.destroy,
                       width=8).pack(side='right')

            dlg.wait_window()

        settings_menu.add_command(label="API Keys...",
                                  command=_open_api_key_settings)

        # ── Help menu ──
        help_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Help", menu=help_menu)

        def _show_about():
            messagebox.showinfo("About TV Show Renamer",
                f"TV Show Renamer\n"
                f"Part of {APP_NAME} v{APP_VERSION}\n\n"
                f"Rename video and subtitle files using\n"
                f"episode data from TVDB or TMDB.\n\n"
                f"Drag and drop files or folders to begin.",
                parent=win)

        help_menu.add_command(label="Template Variables...",
                              command=_open_template_settings)
        help_menu.add_separator()
        help_menu.add_command(label="About...", command=_show_about)

        # ── Keyboard shortcuts ──
        win.bind('<Control-o>', lambda e: _browse_files())
        win.bind('<Control-O>', lambda e: _browse_folder())
        win.bind('<Control-r>', lambda e: _do_rename())
        win.bind('<Control-R>', lambda e: _do_rename())
        win.bind('<Control-z>', lambda e: _do_undo())
        win.bind('<Control-Z>', lambda e: _do_undo())
        win.bind('<Control-w>', lambda e: win.destroy())
        win.bind('<Control-W>', lambda e: win.destroy())
        win.bind('<Control-a>', lambda e: tree.selection_set(
            tree.get_children()))
        win.bind('<Delete>', lambda e: _remove_selected_files())

        _log(f"TV Show Renamer ready — provider: {provider_var.get()}")
        _log("Drag and drop video files or folders to begin")

    def open_batch_filter(self):
        """Open a batch filter window to apply filters to multiple subtitle files at once."""
        import tempfile

        win = tk.Toplevel(self.root)
        win.title("Batch Filter Subtitles")
        win.geometry("620x700")
        win.resizable(True, True)
        self._center_on_main(win)

        file_paths = []  # list of absolute paths

        # ══════════════════════════════════════════════════════════════════════
        # Files section
        # ══════════════════════════════════════════════════════════════════════
        files_frame = ttk.LabelFrame(win, text="Subtitle Files", padding=8)
        files_frame.pack(fill='both', expand=True, padx=10, pady=(10, 5))

        list_frame = ttk.Frame(files_frame)
        list_frame.pack(fill='both', expand=True)

        list_scroll = ttk.Scrollbar(list_frame, orient='vertical')
        list_scroll.pack(side='right', fill='y')

        file_listbox = tk.Listbox(list_frame, height=10, font=('Courier', 9),
                                  selectmode='extended',
                                  yscrollcommand=list_scroll.set)
        file_listbox.pack(fill='both', expand=True)
        list_scroll.config(command=file_listbox.yview)

        file_count_var = tk.StringVar(value="0 files loaded")

        def _update_file_count():
            n = len(file_paths)
            file_count_var.set(f"{n} file{'s' if n != 1 else ''} loaded")

        def _add_paths(paths):
            """Add valid subtitle paths, skipping duplicates."""
            sub_exts = {'.srt', '.ass', '.ssa', '.vtt', '.sub'}
            added = 0
            for p in paths:
                if Path(p).suffix.lower() in sub_exts and p not in file_paths:
                    file_paths.append(p)
                    file_listbox.insert('end', os.path.basename(p))
                    added += 1
            if added:
                _update_file_count()
                self.add_log(f"Batch filter: added {added} subtitle file(s)", 'INFO')

        def add_files():
            paths = filedialog.askopenfilenames(
                parent=win,
                title="Select Subtitle Files",
                filetypes=[
                    ('Subtitle files', '*.srt *.ass *.ssa *.vtt *.sub'),
                    ('SubRip', '*.srt'),
                    ('All files', '*.*'),
                ]
            )
            if paths:
                _add_paths(paths)

        def remove_selected():
            selected = sorted(file_listbox.curselection(), reverse=True)
            for idx in selected:
                file_listbox.delete(idx)
                del file_paths[idx]
            _update_file_count()

        def clear_all():
            file_listbox.delete(0, 'end')
            file_paths.clear()
            _update_file_count()

        btn_frame = ttk.Frame(files_frame)
        btn_frame.pack(fill='x', pady=(6, 0))
        ttk.Button(btn_frame, text="Add Files...", command=add_files).pack(side='left', padx=(0, 4))
        ttk.Button(btn_frame, text="Remove Selected", command=remove_selected).pack(side='left', padx=4)
        ttk.Button(btn_frame, text="Clear All", command=clear_all).pack(side='left', padx=4)
        ttk.Label(btn_frame, textvariable=file_count_var,
                  foreground='gray').pack(side='right')

        # Drag-and-drop hint
        ttk.Label(files_frame, text="Drag and drop subtitle files here",
                  font=('Helvetica', 9), foreground='gray').pack(anchor='center', pady=(4, 0))

        # DnD registration
        def on_batch_drop(event):
            raw = event.data
            paths = []
            i = 0
            while i < len(raw):
                if raw[i] == '{':
                    end = raw.find('}', i)
                    paths.append(raw[i + 1:end])
                    i = end + 2
                elif raw[i] == ' ':
                    i += 1
                else:
                    end = raw.find(' ', i)
                    if end == -1:
                        paths.append(raw[i:])
                        break
                    else:
                        paths.append(raw[i:end])
                        i = end + 1
            _add_paths(paths)

        if HAS_DND:
            win.drop_target_register(DND_FILES)
            win.dnd_bind('<<Drop>>', on_batch_drop)

        # ══════════════════════════════════════════════════════════════════════
        # Filters section
        # ══════════════════════════════════════════════════════════════════════
        filters_frame = ttk.LabelFrame(win, text="Filters to Apply", padding=8)
        filters_frame.pack(fill='x', padx=10, pady=5)

        if not hasattr(self, 'custom_cap_words'):
            self.custom_cap_words = []

        # Define filters: (key, label, filter_function)
        filter_defs = [
            ('remove_hi',      "Remove HI  [brackets] (parens) Speaker:",  filter_remove_hi),
            ('remove_tags',    "Remove Tags  <i> {\\an8}",        filter_remove_tags),
            ('remove_ads',     "Remove Ads / Credits",
             lambda c: filter_remove_ads(c, self.custom_ad_patterns)),
            ('remove_music',   "Remove Stray Notes  ♪ ♫",        filter_remove_music_notes),
            ('remove_dashes',  "Remove Leading Dashes  -",        filter_remove_leading_dashes),
            ('remove_caps_hi', "Remove ALL CAPS HI (UK style)",   filter_remove_caps_hi),
            ('remove_quotes',  "Remove Off-Screen Quotes ' ' (UK style)", filter_remove_offscreen_quotes),
            ('remove_dupes',   "Remove Duplicates",               filter_remove_duplicates),
            ('merge_short',    "Merge Short Cues",                filter_merge_short),
            ('reduce_lines',   "Reduce to 2 Lines",               filter_reduce_lines),
            ('fix_caps',       "Fix ALL CAPS",
             lambda c: filter_fix_caps(c, self.custom_cap_words)),
        ]

        filter_vars = {}
        # Two-column grid layout for filters
        cols_frame = ttk.Frame(filters_frame)
        cols_frame.pack(fill='x')
        left_col = ttk.Frame(cols_frame)
        left_col.pack(side='left', fill='both', expand=True, anchor='n')
        right_col = ttk.Frame(cols_frame)
        right_col.pack(side='left', fill='both', expand=True, anchor='n')

        mid = (len(filter_defs) + 1) // 2  # split point
        for i, (key, label, _) in enumerate(filter_defs):
            var = tk.BooleanVar(value=False)
            filter_vars[key] = var
            col = left_col if i < mid else right_col
            if key == 'fix_caps':
                caps_row = ttk.Frame(col)
                caps_row.pack(fill='x', anchor='w')
                ttk.Checkbutton(caps_row, text=label, variable=var).pack(side='left')
                ttk.Button(caps_row, text="Names...",
                           command=lambda: show_batch_names_dialog()).pack(side='left', padx=(4, 0))
            else:
                ttk.Checkbutton(col, text=label, variable=var).pack(anchor='w')

        def show_batch_names_dialog():
            """Open the custom names editor from the batch filter window."""
            nd = tk.Toplevel(win)
            nd.title("Custom Names — Fix ALL CAPS")
            nd.geometry("400x350")
            self._center_on_main(nd)
            nd.resizable(True, True)
            nd.attributes('-topmost', True)

            ttk.Label(nd, text="Add character names to preserve their\n"
                      "capitalisation when Fix ALL CAPS is applied.",
                      justify='center', padding=(10, 10)).pack()

            lf = ttk.LabelFrame(nd, text="Custom Names (saved across sessions)",
                                padding=8)
            lf.pack(fill='both', expand=True, padx=10, pady=5)

            word_list = tk.Listbox(lf, height=8, font=('Courier', 10))
            word_list.pack(fill='both', expand=True)
            for w in self.custom_cap_words:
                word_list.insert('end', w)

            add_frame = ttk.Frame(lf)
            add_frame.pack(fill='x', pady=(4, 0))
            new_word_var = tk.StringVar()
            word_entry = ttk.Entry(add_frame, textvariable=new_word_var)
            word_entry.pack(side='left', fill='x', expand=True, padx=(0, 4))
            word_entry.focus_set()
            # Right-click context menu for copy/paste
            _wm = tk.Menu(word_entry, tearoff=0)
            _wm.add_command(label="Cut", command=lambda: word_entry.event_generate('<<Cut>>'))
            _wm.add_command(label="Copy", command=lambda: word_entry.event_generate('<<Copy>>'))
            _wm.add_command(label="Paste", command=lambda: word_entry.event_generate('<<Paste>>'))
            _wm.add_separator()
            _wm.add_command(label="Select All",
                command=lambda: (word_entry.select_range(0, 'end'), word_entry.icursor('end')))
            word_entry.bind('<Button-3>', lambda e, m=_wm: m.tk_popup(e.x_root, e.y_root))

            def add_word():
                word = new_word_var.get().strip()
                if not word:
                    return
                if word.lower() not in [w.lower() for w in self.custom_cap_words]:
                    self.custom_cap_words.append(word)
                    word_list.insert('end', word)
                    self.save_preferences()
                new_word_var.set('')

            def remove_word():
                sel = word_list.curselection()
                if sel:
                    self.custom_cap_words.pop(sel[0])
                    word_list.delete(sel[0])
                    self.save_preferences()

            ttk.Button(add_frame, text="Add", command=add_word).pack(side='right')
            word_entry.bind('<Return>', lambda e: add_word())

            ttk.Label(lf, text="Names are saved automatically and persist between sessions.",
                      font=('Helvetica', 8), foreground='gray').pack(anchor='w')

            btn_frame = ttk.Frame(nd, padding=(10, 8, 10, 10))
            btn_frame.pack(fill='x')
            ttk.Button(btn_frame, text="Remove Selected", command=remove_word).pack(side='left')
            ttk.Button(btn_frame, text="Close", command=nd.destroy).pack(side='right')

        sel_frame = ttk.Frame(filters_frame)
        sel_frame.pack(fill='x', pady=(6, 0))

        def select_all_filters():
            for v in filter_vars.values():
                v.set(True)

        def deselect_all_filters():
            for v in filter_vars.values():
                v.set(False)

        ttk.Button(sel_frame, text="Select All", command=select_all_filters).pack(side='left', padx=(0, 4))
        ttk.Button(sel_frame, text="Deselect All", command=deselect_all_filters).pack(side='left')

        # ══════════════════════════════════════════════════════════════════════
        # Search & Replace section
        # ══════════════════════════════════════════════════════════════════════
        sr_frame = ttk.LabelFrame(win, text="Search && Replace (applied to all files)",
                                  padding=8)
        sr_frame.pack(fill='both', expand=True, padx=10, pady=5)

        # Input row
        sr_input = ttk.Frame(sr_frame)
        sr_input.pack(fill='x')

        ttk.Label(sr_input, text="Find:").pack(side='left')
        sr_find_var = tk.StringVar()
        sr_find_entry = ttk.Entry(sr_input, textvariable=sr_find_var, width=18)
        sr_find_entry.pack(side='left', padx=(2, 6))

        ttk.Label(sr_input, text="Replace:").pack(side='left')
        sr_repl_var = tk.StringVar()
        sr_repl_entry = ttk.Entry(sr_input, textvariable=sr_repl_var, width=18)
        sr_repl_entry.pack(side='left', padx=(2, 6))

        sr_case_var = tk.BooleanVar(value=False)

        def sr_add_pair():
            find = sr_find_var.get()
            if not find:
                return
            repl = sr_repl_var.get()
            pair = [find, repl, sr_case_var.get()]
            # Avoid exact duplicates
            if pair not in self.custom_replacements:
                self.custom_replacements.append(pair)
                case_str = " [Aa]" if not pair[2] else ""
                sr_listbox.insert('end', f'"{find}" → "{repl}"{case_str}')
                self.save_preferences()
            sr_find_var.set('')
            sr_repl_var.set('')

        ttk.Button(sr_input, text="Add", command=sr_add_pair).pack(side='left', padx=2)

        # Right-click copy/paste on find/replace entries
        for entry in (sr_find_entry, sr_repl_entry):
            _m = tk.Menu(entry, tearoff=0)
            _m.add_command(label="Cut", command=lambda e=entry: e.event_generate('<<Cut>>'))
            _m.add_command(label="Copy", command=lambda e=entry: e.event_generate('<<Copy>>'))
            _m.add_command(label="Paste", command=lambda e=entry: e.event_generate('<<Paste>>'))
            _m.add_separator()
            _m.add_command(label="Select All",
                command=lambda e=entry: (e.select_range(0, 'end'), e.icursor('end')))
            entry.bind('<Button-3>', lambda ev, m=_m: m.tk_popup(ev.x_root, ev.y_root))

        # Options row
        sr_opts = ttk.Frame(sr_frame)
        sr_opts.pack(fill='x', pady=(4, 4))
        ttk.Checkbutton(sr_opts, text="Case sensitive",
                        variable=sr_case_var).pack(side='left')

        # Replacement list
        sr_list_frame = ttk.Frame(sr_frame)
        sr_list_frame.pack(fill='both', expand=True)

        sr_scroll = ttk.Scrollbar(sr_list_frame, orient='vertical')
        sr_scroll.pack(side='right', fill='y')

        sr_listbox = tk.Listbox(sr_list_frame, height=4, font=('Courier', 9),
                                yscrollcommand=sr_scroll.set)
        sr_listbox.pack(fill='both', expand=True)
        sr_scroll.config(command=sr_listbox.yview)

        # Populate from saved replacements
        for pair in self.custom_replacements:
            find, repl = pair[0], pair[1]
            case_sensitive = pair[2] if len(pair) > 2 else False
            case_str = "" if case_sensitive else " [Aa]"
            sr_listbox.insert('end', f'"{find}" → "{repl}"{case_str}')

        def sr_remove_selected():
            sel = sorted(sr_listbox.curselection(), reverse=True)
            for idx in sel:
                sr_listbox.delete(idx)
                del self.custom_replacements[idx]
            if sel:
                self.save_preferences()

        def sr_clear_all():
            sr_listbox.delete(0, 'end')
            self.custom_replacements.clear()
            self.save_preferences()

        sr_btn_frame = ttk.Frame(sr_frame)
        sr_btn_frame.pack(fill='x', pady=(4, 0))
        ttk.Button(sr_btn_frame, text="Remove Selected",
                   command=sr_remove_selected).pack(side='left', padx=(0, 4))
        ttk.Button(sr_btn_frame, text="Clear All",
                   command=sr_clear_all).pack(side='left')
        ttk.Label(sr_btn_frame, text="Saved across sessions",
                  font=('Helvetica', 8), foreground='gray').pack(side='right')

        sr_find_entry.bind('<Return>', lambda e: sr_add_pair())

        # ══════════════════════════════════════════════════════════════════════
        # Output section
        # ══════════════════════════════════════════════════════════════════════
        output_frame = ttk.LabelFrame(win, text="Output", padding=8)
        output_frame.pack(fill='x', padx=10, pady=5)

        output_mode = tk.StringVar(value='overwrite')
        subfolder_name = tk.StringVar(value='filtered')

        ttk.Radiobutton(output_frame, text="Overwrite original files",
                        variable=output_mode, value='overwrite').pack(anchor='w')

        sub_row = ttk.Frame(output_frame)
        sub_row.pack(fill='x', anchor='w')
        ttk.Radiobutton(sub_row, text="Save to subfolder:",
                        variable=output_mode, value='subfolder').pack(side='left')
        ttk.Entry(sub_row, textvariable=subfolder_name, width=20).pack(side='left', padx=(4, 0))

        # ══════════════════════════════════════════════════════════════════════
        # Progress + Apply
        # ══════════════════════════════════════════════════════════════════════
        progress_frame = ttk.Frame(win, padding=(10, 6, 10, 6))
        progress_frame.pack(fill='x')

        progress_var = tk.DoubleVar(value=0)
        progress_bar = ttk.Progressbar(progress_frame, variable=progress_var,
                                        maximum=100)
        progress_bar.pack(fill='x', side='left', expand=True, padx=(0, 8))

        progress_label = ttk.Label(progress_frame, text="")
        progress_label.pack(side='right')

        action_frame = ttk.Frame(win, padding=(10, 4, 10, 10))
        action_frame.pack(fill='x')

        result_label = ttk.Label(action_frame, text="", foreground='green')
        result_label.pack(side='left')

        def do_batch_apply():
            """Apply selected filters to all loaded files and save."""
            if not file_paths:
                messagebox.showwarning("No Files",
                    "Add subtitle files before applying filters.", parent=win)
                return

            # Gather selected filters in order
            active_filters = [(key, label, func) for key, label, func in filter_defs
                              if filter_vars[key].get()]
            # If both Fix ALL CAPS and Remove HI are selected, ensure Fix ALL CAPS
            # runs first to avoid false HI detection on all-caps text
            active_keys = {k for k, _, _ in active_filters}
            if 'fix_caps' in active_keys and 'remove_hi' in active_keys:
                fix_entry = next(e for e in active_filters if e[0] == 'fix_caps')
                active_filters.remove(fix_entry)
                hi_idx = next(i for i, e in enumerate(active_filters) if e[0] == 'remove_hi')
                active_filters.insert(hi_idx, fix_entry)
            # Strip the key, keep only (label, func)
            active_filters = [(label, func) for _, label, func in active_filters]
            has_replacements = bool(self.custom_replacements)
            if not active_filters and not has_replacements:
                messagebox.showwarning("Nothing to Apply",
                    "Select at least one filter or add search & replace pairs.",
                    parent=win)
                return

            apply_btn.configure(state='disabled')
            total = len(file_paths)
            success = 0
            errors = 0

            for idx, fpath in enumerate(file_paths):
                progress_label.configure(text=f"{idx + 1}/{total}")
                progress_var.set((idx / total) * 100)
                win.update_idletasks()

                try:
                    # Read / convert to SRT
                    ext = Path(fpath).suffix.lower()
                    if ext in ('.srt',):
                        with open(fpath, 'r', encoding='utf-8', errors='replace') as f:
                            srt_text = f.read()
                    else:
                        tmp_srt = tempfile.NamedTemporaryFile(
                            suffix='.srt', delete=False, mode='w', encoding='utf-8')
                        tmp_srt.close()
                        cmd = ['ffmpeg', '-y', '-i', fpath, '-c:s', 'srt', tmp_srt.name]
                        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                        if result.returncode != 0:
                            self.add_log(f"Batch: failed to convert {os.path.basename(fpath)}: "
                                         f"{result.stderr[-200:]}", 'ERROR')
                            os.unlink(tmp_srt.name)
                            errors += 1
                            continue
                        with open(tmp_srt.name, 'r', encoding='utf-8',
                                  errors='replace') as f:
                            srt_text = f.read()
                        os.unlink(tmp_srt.name)

                    cues = parse_srt(srt_text)
                    if not cues:
                        self.add_log(f"Batch: no cues found in {os.path.basename(fpath)}", 'WARNING')
                        errors += 1
                        continue

                    # Apply each active filter in order
                    before = len(cues)
                    for f_label, f_func in active_filters:
                        cues = f_func(cues)

                    # Apply search & replace pairs
                    for pair in self.custom_replacements:
                        find_str, repl_str = pair[0], pair[1]
                        case_sensitive = pair[2] if len(pair) > 2 else False
                        flags = 0 if case_sensitive else re.IGNORECASE
                        pattern = re.escape(find_str)
                        for cue in cues:
                            cue['text'] = re.sub(pattern, repl_str, cue['text'],
                                                 flags=flags)
                    # Remove any cues that became empty after replacements
                    cues = [c for c in cues if c['text'].strip()]

                    if not cues:
                        self.add_log(f"Batch: all cues removed from {os.path.basename(fpath)}, "
                                     "skipping save", 'WARNING')
                        errors += 1
                        continue

                    # Determine output path
                    if output_mode.get() == 'subfolder':
                        sub_dir = os.path.join(os.path.dirname(fpath),
                                               subfolder_name.get() or 'filtered')
                        os.makedirs(sub_dir, exist_ok=True)
                        # Always save as .srt (since we converted to SRT)
                        out_name = Path(fpath).stem + '.srt'
                        out_path = os.path.join(sub_dir, out_name)
                    else:
                        # Overwrite — if original was .srt, overwrite it;
                        # otherwise save as .srt alongside original
                        if ext == '.srt':
                            out_path = fpath
                        else:
                            out_path = str(Path(fpath).with_suffix('.srt'))

                    with open(out_path, 'w', encoding='utf-8') as f:
                        f.write(write_srt(cues))

                    removed = before - len(cues)
                    self.add_log(f"Batch: {os.path.basename(fpath)} → "
                                 f"{len(cues)} entries ({removed} removed) → "
                                 f"{os.path.basename(out_path)}", 'SUCCESS')
                    success += 1

                    # Highlight processed file in the listbox
                    file_listbox.itemconfig(idx, fg='green')

                except Exception as e:
                    self.add_log(f"Batch: error processing {os.path.basename(fpath)}: {e}",
                                 'ERROR')
                    file_listbox.itemconfig(idx, fg='red')
                    errors += 1

            progress_var.set(100)
            progress_label.configure(text=f"{total}/{total}")
            apply_btn.configure(state='normal')

            filters_used = ", ".join(label for label, _ in active_filters)
            if self.custom_replacements:
                filters_used += f", {len(self.custom_replacements)} replacement(s)"
            result_label.configure(
                text=f"Done — {success} succeeded, {errors} failed",
                foreground='green' if errors == 0 else 'orange')
            self.add_log(f"Batch filter complete: {success}/{total} files processed. "
                         f"Filters: {filters_used}", 'SUCCESS')

        apply_btn = ttk.Button(action_frame, text="Apply Filters", command=do_batch_apply)
        apply_btn.pack(side='right', padx=(4, 0))
        ttk.Button(action_frame, text="Close", command=win.destroy).pack(side='right')

        win.wait_window()

    def _run_ocr_with_monitor(self, filepath, stream_index, language,
                              out_path, out_name, file_info):
        """Run bitmap subtitle OCR with a live monitor window.
        Returns True if OCR succeeded and cues were written."""
        import threading
        import time as _time
        from PIL import Image, ImageTk

        # ── State ──
        cancel_event = threading.Event()
        ocr_result = [None]  # [list of cues] or None
        ocr_done = [False]

        # ── Monitor window ──
        mon = tk.Toplevel(self.root)
        mon.title(f"OCR — {os.path.basename(filepath)}")
        mon.geometry("750x580")
        mon.transient(self.root)
        mon.minsize(600, 450)
        self._center_on_main(mon)

        main_f = ttk.Frame(mon, padding=10)
        main_f.pack(fill='both', expand=True)
        main_f.columnconfigure(0, weight=1)
        main_f.rowconfigure(2, weight=1)  # cue list

        # ── Top: progress bar + stats ──
        top_f = ttk.Frame(main_f)
        top_f.grid(row=0, column=0, sticky='ew', pady=(0, 8))
        top_f.columnconfigure(1, weight=1)

        progress_var = tk.DoubleVar(value=0)
        status_label = ttk.Label(top_f, text="Initializing OCR...")
        status_label.grid(row=0, column=0, sticky='w', padx=(0, 8))
        progress_bar = ttk.Progressbar(top_f, variable=progress_var,
                                        maximum=100, mode='determinate')
        progress_bar.grid(row=0, column=1, sticky='ew')

        stats_label = ttk.Label(top_f, text="")
        stats_label.grid(row=1, column=0, columnspan=2, sticky='w', pady=(4, 0))

        # ── Middle: image preview + OCR text ──
        mid_f = ttk.LabelFrame(main_f, text="Current Frame", padding=6)
        mid_f.grid(row=1, column=0, sticky='ew', pady=(0, 8))
        mid_f.columnconfigure(1, weight=1)

        # Image preview (resized to fit)
        img_label = ttk.Label(mid_f, text="[waiting]", anchor='center',
                              width=40, relief='sunken')
        img_label.grid(row=0, column=0, sticky='nsew', padx=(0, 8))
        img_label._photo = None  # prevent GC

        # OCR text result
        text_frame = ttk.Frame(mid_f)
        text_frame.grid(row=0, column=1, sticky='nsew')
        text_frame.rowconfigure(0, weight=1)
        text_frame.columnconfigure(0, weight=1)

        ttk.Label(text_frame, text="OCR Text:", font=('Helvetica', 9, 'bold')).grid(
            row=0, column=0, sticky='nw')
        ocr_text_var = tk.StringVar(value="")
        ocr_text_label = ttk.Label(text_frame, textvariable=ocr_text_var,
                                    wraplength=350, justify='left',
                                    font=('Courier', 11))
        ocr_text_label.grid(row=1, column=0, sticky='nw')

        time_label = ttk.Label(text_frame, text="", foreground='gray')
        time_label.grid(row=2, column=0, sticky='sw', pady=(4, 0))

        # ── Bottom: scrolling cue list ──
        cue_frame = ttk.LabelFrame(main_f, text="Extracted Cues", padding=5)
        cue_frame.grid(row=2, column=0, sticky='nsew')
        cue_frame.columnconfigure(0, weight=1)
        cue_frame.rowconfigure(0, weight=1)

        cue_columns = ('idx', 'time', 'text')
        cue_tree = ttk.Treeview(cue_frame, columns=cue_columns,
                                show='headings', height=8)
        cue_tree.grid(row=0, column=0, sticky='nsew')

        cue_tree.heading('idx',  text='#')
        cue_tree.heading('time', text='Time')
        cue_tree.heading('text', text='Text')
        cue_tree.column('idx',  width=40,  minwidth=30, anchor='center')
        cue_tree.column('time', width=180, minwidth=140)
        cue_tree.column('text', width=400, minwidth=200)

        cue_scroll = ttk.Scrollbar(cue_frame, orient='vertical', command=cue_tree.yview)
        cue_scroll.grid(row=0, column=1, sticky='ns')
        cue_tree.configure(yscrollcommand=cue_scroll.set)

        # ── Cancel button ──
        btn_f = ttk.Frame(main_f)
        btn_f.grid(row=3, column=0, sticky='e', pady=(8, 0))
        cancel_btn = ttk.Button(btn_f, text="Cancel OCR",
                                command=lambda: cancel_event.set())
        cancel_btn.pack(side='right')

        # ── Track timing ──
        start_time = [_time.monotonic()]
        cue_count = [0]

        # ── Frame callback (called from OCR thread for each frame) ──
        def _on_frame(frame_idx, total, img_path, text, start_t, end_t):
            def _update():
                # Progress
                pct = ((frame_idx + 1) / total) * 100
                progress_var.set(pct)
                status_label.configure(text=f"Frame {frame_idx + 1} / {total}")

                # Elapsed + ETA
                elapsed = _time.monotonic() - start_time[0]
                if frame_idx > 0:
                    per_frame = elapsed / (frame_idx + 1)
                    remaining = per_frame * (total - frame_idx - 1)
                    eta_m, eta_s = divmod(int(remaining), 60)
                    elapsed_m, elapsed_s = divmod(int(elapsed), 60)
                    stats_label.configure(
                        text=f"Elapsed: {elapsed_m}m {elapsed_s}s  |  "
                             f"ETA: {eta_m}m {eta_s}s  |  "
                             f"Cues found: {cue_count[0]}")
                else:
                    stats_label.configure(text=f"Starting...")

                # Image preview
                if img_path and os.path.exists(img_path):
                    try:
                        pil_img = Image.open(img_path)
                        # Resize to fit preview (max 320x80)
                        pil_img.thumbnail((320, 80), Image.LANCZOS)
                        photo = ImageTk.PhotoImage(pil_img)
                        img_label.configure(image=photo, text='')
                        img_label._photo = photo  # prevent GC
                    except Exception:
                        img_label.configure(image='', text='[error]')
                else:
                    img_label.configure(image='', text='[no image]')

                # OCR text
                ocr_text_var.set(text if text else '[empty]')
                time_label.configure(text=f"{start_t} → {end_t}")

                # Add to cue list if it's real text (not a status marker)
                if text and not text.startswith('['):
                    cue_count[0] += 1
                    cue_tree.insert('', 'end', values=(
                        cue_count[0], f"{start_t} → {end_t}", text))
                    # Auto-scroll to bottom
                    children = cue_tree.get_children()
                    if children:
                        cue_tree.see(children[-1])

            mon.after(0, _update)

        def _on_progress(msg):
            def _do():
                status_label.configure(text=msg)
                # Extract percentage from messages like "... (30%)" or "... 40/1932 ..."
                import re as _re
                pct_match = _re.search(r'\((\d+)%\)', msg)
                if pct_match:
                    progress_var.set(float(pct_match.group(1)))
                else:
                    frac_match = _re.search(r'(\d+)/(\d+)', msg)
                    if frac_match:
                        n, t = int(frac_match.group(1)), int(frac_match.group(2))
                        if t > 0:
                            progress_var.set((n / t) * 100)
            mon.after(0, _do)

        # ── OCR thread ──
        def _ocr_thread():
            cues = ocr_bitmap_subtitle(
                filepath, stream_index, language,
                progress_callback=_on_progress,
                frame_callback=_on_frame,
                cancel_event=cancel_event
            )
            ocr_result[0] = cues
            ocr_done[0] = True

            def _finish():
                elapsed = _time.monotonic() - start_time[0]
                elapsed_m, elapsed_s = divmod(int(elapsed), 60)

                if cues:
                    write_srt_file(cues, out_path)
                    self.add_log(f"OCR complete: {out_name} ({len(cues)} cues, "
                                 f"{elapsed_m}m {elapsed_s}s)", 'SUCCESS')
                    status_label.configure(text=f"Done — {len(cues)} cues extracted "
                                                f"in {elapsed_m}m {elapsed_s}s")
                    progress_var.set(100)
                    cancel_btn.configure(text="Close", command=mon.destroy)

                    # Add buttons for next steps
                    ttk.Button(btn_f, text="Open in Editor",
                               command=lambda: (
                                   mon.destroy(),
                                   self.show_subtitle_editor(
                                       filepath, stream_index, file_info,
                                       external_sub_path=out_path)
                               )).pack(side='right', padx=(0, 8))
                else:
                    status_label.configure(text="OCR produced no output")
                    self.add_log(f"OCR produced no output for stream #{stream_index}",
                                 'WARNING')
                    cancel_btn.configure(text="Close", command=mon.destroy)

            mon.after(0, _finish)

        t = threading.Thread(target=_ocr_thread, daemon=True)
        t.start()

        # Handle window close = cancel
        def _on_close():
            if not ocr_done[0]:
                cancel_event.set()
            mon.destroy()
        mon.protocol('WM_DELETE_WINDOW', _on_close)

        # Block until window closes
        mon.grab_set()
        mon.wait_window()

        return bool(ocr_result[0])

    def show_subtitle_editor(self, filepath, stream_index, file_info,
                                external_sub_path=None):
        """Show subtitle text editor for a subtitle stream or external file.

        For internal streams: extracts from filepath using stream_index.
        For external files: pass external_sub_path (stream_index is ignored).

        Full-featured editor with filters, search/replace, timing tools,
        undo/redo, duplicate detection, video preview, and export.
        """
        import tempfile
        import copy

        is_external = external_sub_path is not None

        if is_external:
            # Read external subtitle file directly
            sub_path = external_sub_path
            ext = Path(sub_path).suffix.lower()
            if ext in ('.srt',):
                # Already SRT — read directly
                try:
                    with open(sub_path, 'r', encoding='utf-8', errors='replace') as f:
                        srt_text = f.read()
                except Exception as e:
                    self.add_log(f"Failed to read subtitle file: {e}", 'ERROR')
                    return
            else:
                # Convert to SRT via ffmpeg
                tmp_srt = tempfile.NamedTemporaryFile(suffix='.srt', delete=False,
                                                       mode='w', encoding='utf-8')
                tmp_srt.close()
                cmd = ['ffmpeg', '-y', '-i', sub_path, '-c:s', 'srt', tmp_srt.name]
                try:
                    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                    if result.returncode != 0:
                        self.add_log(f"Failed to convert subtitle for editing: "
                                     f"{result.stderr[-200:]}", 'ERROR')
                        os.unlink(tmp_srt.name)
                        return
                except Exception as e:
                    self.add_log(f"Convert error: {e}", 'ERROR')
                    os.unlink(tmp_srt.name)
                    return
                with open(tmp_srt.name, 'r', encoding='utf-8', errors='replace') as f:
                    srt_text = f.read()
                os.unlink(tmp_srt.name)
        else:
            # Extract internal subtitle stream to temp SRT
            tmp_srt = tempfile.NamedTemporaryFile(suffix='.srt', delete=False, mode='w',
                                                   encoding='utf-8')
            tmp_srt.close()
            cmd = ['ffmpeg', '-y', '-i', filepath, '-map', f'0:{stream_index}',
                   '-c:s', 'srt', tmp_srt.name]
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                if result.returncode != 0:
                    self.add_log(f"Failed to extract subtitle #{stream_index} for editing: "
                                 f"{result.stderr[-200:]}", 'ERROR')
                    os.unlink(tmp_srt.name)
                    return
            except Exception as e:
                self.add_log(f"Extract error: {e}", 'ERROR')
                os.unlink(tmp_srt.name)
                return
            with open(tmp_srt.name, 'r', encoding='utf-8', errors='replace') as f:
                srt_text = f.read()
            os.unlink(tmp_srt.name)

        cues = parse_srt(srt_text)
        if not cues:
            label = os.path.basename(external_sub_path) if is_external else f"stream #{stream_index}"
            self.add_log(f"No subtitle cues found in {label}", 'WARNING')
            return

        # Keep original for undo-all
        original_cues = [dict(c) for c in cues]

        # ── Undo / Redo stack ──
        undo_stack = []   # list of previous cue states
        redo_stack = []

        def push_undo():
            """Save current state to undo stack."""
            undo_stack.append([dict(c) for c in cues])
            redo_stack.clear()  # new action clears redo

        def do_undo(event=None):
            nonlocal cues
            if not undo_stack:
                return
            redo_stack.append([dict(c) for c in cues])
            cues = undo_stack.pop()
            refresh_tree(cues)

        def do_redo(event=None):
            nonlocal cues
            if not redo_stack:
                return
            undo_stack.append([dict(c) for c in cues])
            cues = redo_stack.pop()
            refresh_tree(cues)

        # ── Editor Window ──
        editor = tk.Toplevel(self.root)
        if is_external:
            editor.title(f"Edit Subtitles — {os.path.basename(external_sub_path)}")
        else:
            editor.title(f"Edit Subtitles — Stream #{stream_index} — {os.path.basename(filepath)}")
        editor.geometry("950x650")
        editor.transient(self.root)
        editor.grab_set()
        self._center_on_main(editor)
        editor.resizable(True, True)

        # Keyboard shortcuts
        editor.bind('<Control-z>', do_undo)
        editor.bind('<Control-y>', do_redo)
        editor.bind('<Control-Z>', do_undo)
        editor.bind('<Control-Y>', do_redo)

        # Track state
        modified_count = tk.IntVar(value=0)
        deleted_count = tk.IntVar(value=0)

        # ── Color tag styles for treeview ──
        # Will be configured after tree is created; define names here
        TAG_MODIFIED = 'modified'
        TAG_HI = 'has_hi'
        TAG_TAGS = 'has_tags'
        TAG_LONG = 'long_line'
        TAG_SEARCH = 'search_match'
        TAG_SPELL = 'has_spelling'

        # ── Spell check state ──
        spell_error_indices = set()

        def _classify_cue(cue, orig_text=None):
            """Return set of tag names for a cue based on its content."""
            tags = set()
            text = cue['text']
            # Modified from original
            if orig_text is not None and text != orig_text:
                tags.add(TAG_MODIFIED)
            # Contains HI annotations
            if re.search(r'\[.*?\]|\(.*?\)|♪|♫', text):
                tags.add(TAG_HI)
            # Contains HTML/ASS tags
            if re.search(r'<[^>]+>|\{\\[^}]+\}', text):
                tags.add(TAG_TAGS)
            # Long lines
            for line in text.split('\n'):
                if len(line) > MAX_CHARS_PER_LINE:
                    tags.add(TAG_LONG)
                    break
            return tags

        # Set of original texts for modified detection (index-independent)
        _orig_texts = {c['text'] for c in original_cues}

        def refresh_tree(new_cues, search_indices=None):
            """Reload the treeview with updated cues."""
            nonlocal cues
            cues = new_cues
            tree.delete(*tree.get_children())
            search_set = set(search_indices or [])
            for i, cue in enumerate(cues):
                display = cue['text'].replace('\n', ' \\n ')
                ts = f"{cue['start']} → {cue['end']}"
                # Determine color tags — compare against original text set
                # Pass a dummy different string to trigger "modified" if text not in originals
                if cue['text'] in _orig_texts:
                    orig_text = cue['text']  # matches — not modified
                else:
                    orig_text = ''  # doesn't match — will be flagged as modified
                ctags = _classify_cue(cue, orig_text)
                if i in search_set:
                    ctags.add(TAG_SEARCH)
                # Priority: search > modified > hi > tags > long
                if TAG_SEARCH in ctags:
                    row_tag = TAG_SEARCH
                elif i in spell_error_indices:
                    row_tag = TAG_SPELL
                elif TAG_MODIFIED in ctags:
                    row_tag = TAG_MODIFIED
                elif TAG_HI in ctags:
                    row_tag = TAG_HI
                elif TAG_TAGS in ctags:
                    row_tag = TAG_TAGS
                elif TAG_LONG in ctags:
                    row_tag = TAG_LONG
                else:
                    row_tag = ''
                tree.insert('', 'end', iid=str(i),
                            values=(i + 1, ts, display),
                            tags=(row_tag,) if row_tag else ())
            # Update stats
            deleted_count.set(len(original_cues) - len(cues))
            mod = sum(1 for i, c in enumerate(cues) if i < len(original_cues)
                      and c['text'] != original_cues[i]['text'])
            modified_count.set(mod)
            long_count = sum(1 for c in cues
                             if any(len(l) > MAX_CHARS_PER_LINE for l in c['text'].split('\n')))
            stats_parts = [
                f"{len(cues)} entries",
                f"{modified_count.get()} modified",
                f"{deleted_count.get()} removed",
            ]
            if long_count:
                stats_parts.append(f"{long_count} long lines")
            stats_label.configure(text=" │ ".join(stats_parts))

        def apply_filter(filter_func, name):
            nonlocal cues
            push_undo()
            before = len(cues)
            cues = filter_func(cues)
            after = len(cues)
            self.add_log(f"Filter '{name}': {before - after} entries removed, "
                         f"{after} remaining", 'INFO')
            refresh_tree(cues)

        def _is_mostly_allcaps():
            """Check if the subtitle text is mostly ALL CAPS."""
            if not cues:
                return False
            all_text = ' '.join(c['text'] for c in cues)
            alpha = re.sub(r'[^a-zA-Z]', '', all_text)
            if not alpha:
                return False
            return sum(1 for c in alpha if c.isupper()) / len(alpha) >= 0.6

        def apply_remove_hi():
            """Apply Remove HI, auto-running Fix ALL CAPS first if text is all-caps."""
            nonlocal cues
            if _is_mostly_allcaps():
                self.add_log("Text is mostly ALL CAPS — running Fix ALL CAPS first "
                             "to avoid false HI detection", 'INFO')
                push_undo()
                cues = filter_fix_caps(cues, self.custom_cap_words)
                refresh_tree(cues)
            apply_filter(filter_remove_hi, "Remove HI")

        def undo_all():
            nonlocal cues
            push_undo()
            cues = [dict(c) for c in original_cues]
            refresh_tree(cues)
            self.add_log("Subtitle edits reset to original", 'INFO')

        def delete_selected():
            nonlocal cues
            selected = tree.selection()
            if not selected:
                return
            push_undo()
            indices_to_remove = set(int(s) for s in selected)
            cues = [c for i, c in enumerate(cues) if i not in indices_to_remove]
            refresh_tree(cues)

        def split_selected():
            """Split the selected cue into two at the midpoint."""
            nonlocal cues
            selected = tree.selection()
            if len(selected) != 1:
                messagebox.showinfo("Split", "Select exactly one cue to split.",
                                    parent=editor)
                return
            idx = int(selected[0])
            cue = cues[idx]
            text = cue['text']
            lines = text.split('\n')
            if len(lines) < 2:
                mid = len(text) // 2
                space_pos = text.rfind(' ', 0, mid + 10)
                if space_pos > mid - 20:
                    mid = space_pos
                text1 = text[:mid].rstrip()
                text2 = text[mid:].lstrip()
            else:
                mid_line = len(lines) // 2
                text1 = '\n'.join(lines[:mid_line])
                text2 = '\n'.join(lines[mid_line:])

            if not text1 or not text2:
                return

            push_undo()
            start_ms = srt_ts_to_ms(cue['start'])
            end_ms = srt_ts_to_ms(cue['end'])
            mid_ms = (start_ms + end_ms) // 2

            cue1 = {**cue, 'text': text1, 'end': ms_to_srt_ts(mid_ms)}
            cue2 = {**cue, 'text': text2, 'start': ms_to_srt_ts(mid_ms + 1)}
            cues[idx:idx + 1] = [cue1, cue2]
            refresh_tree(cues)

        def join_selected():
            """Join two or more selected consecutive cues into one."""
            nonlocal cues
            selected = sorted(tree.selection(), key=int)
            if len(selected) < 2:
                messagebox.showinfo("Join", "Select two or more consecutive cues to join.",
                                    parent=editor)
                return
            indices = [int(s) for s in selected]
            for i in range(1, len(indices)):
                if indices[i] != indices[i - 1] + 1:
                    messagebox.showwarning("Join",
                        "Selected cues must be consecutive.", parent=editor)
                    return

            push_undo()
            first = cues[indices[0]]
            last = cues[indices[-1]]
            merged_text = ' '.join(cues[i]['text'] for i in indices)
            merged = {
                **first,
                'end': last['end'],
                'text': merged_text
            }
            cues[indices[0]:indices[-1] + 1] = [merged]
            refresh_tree(cues)

        def show_timing_dialog():
            """Show a small dialog for timing offset and stretch."""
            td = tk.Toplevel(editor)
            td.title("Timing Adjustment")
            td.geometry("320x180")
            td.transient(editor)
            td.grab_set()
            self._center_on_main(td)
            td.resizable(False, False)

            of = ttk.LabelFrame(td, text="Offset (shift all timestamps)", padding=8)
            of.pack(fill='x', padx=10, pady=(10, 5))
            offset_var = tk.StringVar(value="0")
            ttk.Label(of, text="Milliseconds (+/−):").pack(side='left')
            ttk.Entry(of, textvariable=offset_var, width=10).pack(side='left', padx=4)

            def apply_offset():
                nonlocal cues
                try:
                    ms = int(offset_var.get())
                except ValueError:
                    messagebox.showwarning("Invalid", "Enter a number in milliseconds.",
                                           parent=td)
                    return
                if ms == 0:
                    return
                push_undo()
                cues = shift_timestamps(cues, ms)
                refresh_tree(cues)
                direction = "forward" if ms > 0 else "backward"
                self.add_log(f"Shifted timestamps {direction} by {abs(ms)}ms", 'INFO')
                td.destroy()

            ttk.Button(of, text="Apply", command=apply_offset).pack(side='right')

            sf = ttk.LabelFrame(td, text="Stretch (scale timestamps)", padding=8)
            sf.pack(fill='x', padx=10, pady=5)
            stretch_var = tk.StringVar(value="1.0")
            ttk.Label(sf, text="Factor:").pack(side='left')
            ttk.Entry(sf, textvariable=stretch_var, width=10).pack(side='left', padx=4)

            def apply_stretch():
                nonlocal cues
                try:
                    factor = float(stretch_var.get())
                except ValueError:
                    messagebox.showwarning("Invalid", "Enter a decimal number (e.g. 1.04).",
                                           parent=td)
                    return
                if factor <= 0:
                    messagebox.showwarning("Invalid", "Factor must be positive.", parent=td)
                    return
                if factor == 1.0:
                    return
                push_undo()
                cues = stretch_timestamps(cues, factor)
                refresh_tree(cues)
                self.add_log(f"Stretched timestamps by factor {factor}", 'INFO')
                td.destroy()

            ttk.Button(sf, text="Apply", command=apply_stretch).pack(side='right')
            ttk.Button(td, text="Close", command=td.destroy).pack(pady=(5, 10))

        # Search state
        find_var = tk.StringVar()
        replace_var = tk.StringVar()
        use_regex = tk.BooleanVar(value=False)  # reserved for future use
        wrap_around = tk.BooleanVar(value=False)

        def do_find():
            """Highlight all cues matching the search term."""
            term = find_var.get()
            if not term:
                refresh_tree(cues)
                return
            matches = []
            for i, cue in enumerate(cues):
                try:
                    if use_regex.get():
                        if re.search(term, cue['text'], re.IGNORECASE):
                            matches.append(i)
                    else:
                        if term.lower() in cue['text'].lower():
                            matches.append(i)
                except re.error:
                    pass
            refresh_tree(cues, search_indices=matches)
            if matches:
                first_idx = matches[0]
                first = str(first_idx)
                def _scroll_to_match():
                    tree.selection_set(first)
                    # Scroll so the match is near the middle of the view, not at the edge
                    # Aim a few rows past the match so it's comfortably visible
                    ahead = min(first_idx + 5, len(cues) - 1)
                    tree.see(str(ahead))
                    tree.after(50, lambda: (tree.see(first), tree.selection_set(first)))
                tree.after_idle(_scroll_to_match)
            self.add_log(f"Search: {len(matches)} matches for '{term}'", 'INFO')

        def do_replace_one():
            """Replace the first occurrence of search term from current selection."""
            nonlocal cues
            term = find_var.get()
            repl = replace_var.get()
            if not term:
                return
            sel = tree.selection()
            start_idx = int(sel[0]) if sel else 0
            if wrap_around.get():
                order = list(range(start_idx, len(cues))) + list(range(0, start_idx))
            else:
                order = list(range(start_idx, len(cues)))
            for i in order:
                old_text = cues[i]['text']
                try:
                    if use_regex.get():
                        new_text = re.sub(term, repl, old_text, count=1,
                                          flags=re.IGNORECASE)
                    else:
                        pat = re.escape(term)
                        new_text = re.sub(pat, repl, old_text, count=1,
                                          flags=re.IGNORECASE)
                except re.error:
                    continue
                if new_text != old_text:
                    push_undo()
                    cues[i]['text'] = new_text
                    if not new_text.strip():
                        del cues[i]
                    refresh_tree(cues)
                    if wrap_around.get():
                        next_order = list(range(i + 1, len(cues))) + list(range(0, i + 1))
                    else:
                        next_order = list(range(i + 1, len(cues)))
                    for j in next_order:
                        try:
                            if use_regex.get():
                                if re.search(term, cues[j]['text'], re.IGNORECASE):
                                    tree.see(str(j))
                                    tree.selection_set(str(j))
                                    break
                            else:
                                if term.lower() in cues[j]['text'].lower():
                                    tree.see(str(j))
                                    tree.selection_set(str(j))
                                    break
                        except (re.error, IndexError):
                            pass
                    self.add_log(f"Replaced 1 occurrence of '{term}' → '{repl}'", 'INFO')
                    return
            self.add_log(f"No more matches found for '{term}'", 'INFO')

        def do_replace_all():
            """Replace all occurrences of search term."""
            nonlocal cues
            term = find_var.get()
            repl = replace_var.get()
            if not term:
                return
            push_undo()
            count = 0
            for cue in cues:
                old_text = cue['text']
                try:
                    if use_regex.get():
                        new_text = re.sub(term, repl, old_text, flags=re.IGNORECASE)
                    else:
                        pattern = re.escape(term)
                        new_text = re.sub(pattern, repl, old_text, flags=re.IGNORECASE)
                except re.error:
                    continue
                if new_text != old_text:
                    cue['text'] = new_text
                    count += 1
            cues = [c for c in cues if c['text'].strip()]
            refresh_tree(cues)
            self.add_log(f"Replaced {count} occurrence(s) of '{term}' → '{repl}'", 'INFO')

        # ══════════════════════════════════════════════════════════════════════
        # Menu bar
        # ══════════════════════════════════════════════════════════════════════
        menubar = tk.Menu(editor)
        editor.configure(menu=menubar)

        # ── Filters menu ──
        filter_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Tools", menu=filter_menu)
        filter_menu.add_command(label="Remove HI  [brackets] (parens) Speaker:",
                                command=lambda: apply_remove_hi())
        filter_menu.add_command(label="Remove Tags  <i> {\\an8}",
                                command=lambda: apply_filter(filter_remove_tags, "Remove Tags"))
        def apply_remove_ads():
            apply_filter(lambda c: filter_remove_ads(c, self.custom_ad_patterns),
                         "Remove Ads")

        filter_menu.add_command(label="Remove Ads / Credits", command=apply_remove_ads)
        filter_menu.add_command(label="Remove Stray Notes  ♪ ♫",
                                command=lambda: apply_filter(filter_remove_music_notes, "Remove Stray Notes"))
        filter_menu.add_command(label="Remove Leading Dashes  -",
                                command=lambda: apply_filter(filter_remove_leading_dashes, "Remove Leading Dashes"))
        filter_menu.add_command(label="Remove ALL CAPS HI  (UK style)",
                                command=lambda: apply_filter(filter_remove_caps_hi, "Remove CAPS HI"))
        filter_menu.add_command(label="Remove Off-Screen Quotes  ' '  (UK style)",
                                command=lambda: apply_filter(filter_remove_offscreen_quotes, "Remove Off-Screen Quotes"))
        filter_menu.add_separator()
        filter_menu.add_command(label="Remove Duplicates",
                                command=lambda: apply_filter(filter_remove_duplicates, "Remove Duplicates"))
        filter_menu.add_command(label="Merge Short Cues",
                                command=lambda: apply_filter(filter_merge_short, "Merge Short Cues"))
        filter_menu.add_command(label="Reduce to 2 Lines",
                                command=lambda: apply_filter(filter_reduce_lines, "Reduce to 2 Lines"))
        filter_menu.add_separator()

        # ── Fix ALL CAPS ──
        # Custom capitalize words stored on the app instance
        if not hasattr(self, 'custom_cap_words'):
            self.custom_cap_words = []

        def show_fix_caps_dialog():
            """Show Fix ALL CAPS dialog with custom names management."""
            cd = tk.Toplevel(editor)
            cd.title("Fix ALL CAPS")
            cd.geometry("420x400")
            self._center_on_main(cd)
            cd.resizable(True, True)
            # Keep on top but don't grab — allows scrolling the subtitle list
            cd.attributes('-topmost', True)

            ttk.Label(cd, text="Converts ALL CAPS text to sentence case.\n"
                      "Add character names below to preserve their capitalisation.\n"
                      "You can scroll the subtitle list to find names.",
                      justify='center', padding=(10, 10)).pack()

            lf = ttk.LabelFrame(cd, text="Custom Names (saved across sessions)",
                                padding=8)
            lf.pack(fill='both', expand=True, padx=10, pady=5)

            word_list = tk.Listbox(lf, height=8, font=('Courier', 10))
            word_list.pack(fill='both', expand=True)
            for w in self.custom_cap_words:
                word_list.insert('end', w)

            add_frame = ttk.Frame(lf)
            add_frame.pack(fill='x', pady=(4, 0))
            new_word_var = tk.StringVar()
            word_entry = ttk.Entry(add_frame, textvariable=new_word_var)
            word_entry.pack(side='left', fill='x', expand=True, padx=(0, 4))
            word_entry.focus_set()
            # Right-click context menu for copy/paste
            _wm = tk.Menu(word_entry, tearoff=0)
            _wm.add_command(label="Cut", command=lambda: word_entry.event_generate('<<Cut>>'))
            _wm.add_command(label="Copy", command=lambda: word_entry.event_generate('<<Copy>>'))
            _wm.add_command(label="Paste", command=lambda: word_entry.event_generate('<<Paste>>'))
            _wm.add_separator()
            _wm.add_command(label="Select All",
                command=lambda: (word_entry.select_range(0, 'end'), word_entry.icursor('end')))
            word_entry.bind('<Button-3>', lambda e, m=_wm: m.tk_popup(e.x_root, e.y_root))

            def add_word():
                word = new_word_var.get().strip()
                if not word:
                    return
                if word.lower() not in [w.lower() for w in self.custom_cap_words]:
                    self.custom_cap_words.append(word)
                    word_list.insert('end', word)
                    self.save_preferences()
                new_word_var.set('')

            def remove_word():
                sel = word_list.curselection()
                if sel:
                    self.custom_cap_words.pop(sel[0])
                    word_list.delete(sel[0])
                    self.save_preferences()

            ttk.Button(add_frame, text="Add", command=add_word).pack(side='right')
            word_entry.bind('<Return>', lambda e: add_word())

            ttk.Label(lf, text="Names are saved automatically and persist between sessions.",
                      font=('Helvetica', 8), foreground='gray').pack(anchor='w')

            btn_frame = ttk.Frame(cd, padding=(10, 8, 10, 10))
            btn_frame.pack(fill='x')
            ttk.Button(btn_frame, text="Remove Selected", command=remove_word).pack(side='left')
            ttk.Button(btn_frame, text="Apply",
                       command=lambda: (cd.destroy(), apply_filter(
                           lambda c: filter_fix_caps(c, self.custom_cap_words),
                           "Fix ALL CAPS"))).pack(side='right')
            ttk.Button(btn_frame, text="Close", command=cd.destroy).pack(side='right', padx=4)

        filter_menu.add_command(label="Fix ALL CAPS...", command=show_fix_caps_dialog)
        filter_menu.add_separator()

        def show_ad_patterns_dialog():
            """Dialog to view built-in ad patterns and manage custom ones."""
            pd = tk.Toplevel(editor)
            pd.title("Ad / Credit Patterns")
            pd.geometry("500x420")
            pd.transient(editor)
            pd.grab_set()
            self._center_on_main(pd)
            pd.resizable(True, True)

            # Built-in patterns (read-only)
            bf = ttk.LabelFrame(pd, text="Built-in Patterns (always active)", padding=8)
            bf.pack(fill='x', padx=10, pady=(10, 5))
            builtin_list = tk.Listbox(bf, height=6, font=('Courier', 9))
            builtin_list.pack(fill='x')
            for p in BUILTIN_AD_PATTERNS:
                builtin_list.insert('end', p)

            # Custom patterns (editable)
            cf = ttk.LabelFrame(pd, text="Custom Patterns (saved to preferences)", padding=8)
            cf.pack(fill='both', expand=True, padx=10, pady=5)

            custom_list = tk.Listbox(cf, height=8, font=('Courier', 9))
            custom_list.pack(fill='both', expand=True)
            for p in self.custom_ad_patterns:
                custom_list.insert('end', p)

            add_frame = ttk.Frame(cf)
            add_frame.pack(fill='x', pady=(4, 0))
            new_pattern_var = tk.StringVar()
            pattern_entry = ttk.Entry(add_frame, textvariable=new_pattern_var)
            pattern_entry.pack(side='left', fill='x', expand=True, padx=(0, 4))

            def add_pattern():
                pat = new_pattern_var.get().strip()
                if not pat:
                    return
                # Validate regex
                try:
                    re.compile(pat)
                except re.error as e:
                    messagebox.showwarning("Invalid Pattern",
                                           f"Not a valid regex:\n{e}", parent=pd)
                    return
                if pat not in self.custom_ad_patterns:
                    self.custom_ad_patterns.append(pat)
                    custom_list.insert('end', pat)
                    new_pattern_var.set('')
                    self.add_log(f"Added custom ad pattern: {pat}", 'INFO')

            def remove_selected():
                sel = custom_list.curselection()
                if not sel:
                    return
                idx = sel[0]
                removed = self.custom_ad_patterns.pop(idx)
                custom_list.delete(idx)
                self.add_log(f"Removed custom ad pattern: {removed}", 'INFO')

            ttk.Button(add_frame, text="Add", command=add_pattern).pack(side='right')
            pattern_entry.bind('<Return>', lambda e: add_pattern())

            # Hint
            ttk.Label(cf, text="Patterns are case-insensitive regex matched at start of line.",
                      font=('Helvetica', 8), foreground='gray').pack(anchor='w')

            # Buttons
            btn_frame = ttk.Frame(pd, padding=(10, 6, 10, 10))
            btn_frame.pack(fill='x')
            ttk.Button(btn_frame, text="Remove Selected", command=remove_selected).pack(side='left')

            def save_and_close():
                self.save_preferences()
                pd.destroy()

            ttk.Button(btn_frame, text="Save & Close", command=save_and_close).pack(side='right')
            ttk.Button(btn_frame, text="Cancel", command=pd.destroy).pack(side='right', padx=4)

        filter_menu.add_command(label="Manage Ad Patterns...",
                                command=show_ad_patterns_dialog)
        filter_menu.add_separator()
        filter_menu.add_command(label="Spell Check...",
                                accelerator="F7",
                                command=lambda: _show_spell_check())
        filter_menu.add_separator()
        filter_menu.add_command(label="Search/Replace List...",
                                command=lambda: _show_saved_replacements())

        def _show_saved_replacements():
            """Show dialog to manage and apply persistent search & replace pairs."""
            sd = tk.Toplevel(editor)
            sd.title("Search/Replace List")
            sd.geometry("550x450")
            sd.resizable(True, True)
            self._center_on_main(sd)
            sd.attributes('-topmost', True)

            f = ttk.Frame(sd, padding=12)
            f.pack(fill='both', expand=True)
            f.columnconfigure(0, weight=1)
            f.rowconfigure(1, weight=1)

            # ── Add new pair ──
            add_f = ttk.LabelFrame(f, text="Add Replacement", padding=6)
            add_f.grid(row=0, column=0, sticky='ew', pady=(0, 8))

            af = ttk.Frame(add_f)
            af.pack(fill='x')
            ttk.Label(af, text="Find:").pack(side='left', padx=(0, 4))
            sr_find = tk.StringVar()
            sr_find_entry = ttk.Entry(af, textvariable=sr_find, width=18)
            sr_find_entry.pack(side='left', padx=(0, 8))
            ttk.Label(af, text="Replace:").pack(side='left', padx=(0, 4))
            sr_repl = tk.StringVar()
            sr_repl_entry = ttk.Entry(af, textvariable=sr_repl, width=18)
            sr_repl_entry.pack(side='left', padx=(0, 8))
            sr_case = tk.BooleanVar(value=False)
            ttk.Checkbutton(af, text="Aa", variable=sr_case).pack(side='left', padx=(0, 4))

            def _add_pair():
                find = sr_find.get()
                if not find:
                    return
                repl = sr_repl.get()
                pair = [find, repl, sr_case.get()]
                if pair not in self.custom_replacements:
                    self.custom_replacements.append(pair)
                    self.save_preferences()
                _refresh_list()
                sr_find.set('')
                sr_repl.set('')

            ttk.Button(af, text="Add", command=_add_pair, width=5).pack(side='left', padx=2)

            # ── List ──
            list_f = ttk.Frame(f)
            list_f.grid(row=1, column=0, sticky='nsew')
            list_f.columnconfigure(0, weight=1)
            list_f.rowconfigure(0, weight=1)

            columns = ('find', 'replace', 'case')
            sr_tree = ttk.Treeview(list_f, columns=columns, show='headings', height=10)
            sr_tree.grid(row=0, column=0, sticky='nsew')
            sr_tree.heading('find', text='Find')
            sr_tree.heading('replace', text='Replace With')
            sr_tree.heading('case', text='Case')
            sr_tree.column('find', width=180, minwidth=100)
            sr_tree.column('replace', width=180, minwidth=100)
            sr_tree.column('case', width=50, minwidth=40, anchor='center')

            sr_scroll = ttk.Scrollbar(list_f, orient='vertical', command=sr_tree.yview)
            sr_scroll.grid(row=0, column=1, sticky='ns')
            sr_tree.configure(yscrollcommand=sr_scroll.set)

            def _refresh_list():
                sr_tree.delete(*sr_tree.get_children())
                for i, pair in enumerate(self.custom_replacements):
                    find, repl = pair[0], pair[1]
                    case = 'Yes' if (len(pair) > 2 and pair[2]) else 'No'
                    sr_tree.insert('', 'end', iid=str(i),
                                  values=(find, repl, case))

            def _remove_selected():
                sel = sr_tree.selection()
                if not sel:
                    return
                indices = sorted([int(s) for s in sel], reverse=True)
                for idx in indices:
                    if idx < len(self.custom_replacements):
                        del self.custom_replacements[idx]
                self.save_preferences()
                _refresh_list()

            def _clear_all():
                if messagebox.askyesno("Clear All",
                    "Remove all saved replacements?", parent=sd):
                    self.custom_replacements.clear()
                    self.save_preferences()
                    _refresh_list()

            # ── Buttons ──
            btn_f = ttk.Frame(f)
            btn_f.grid(row=2, column=0, sticky='ew', pady=(8, 0))

            def _apply_all():
                if not self.custom_replacements:
                    messagebox.showinfo("No Replacements",
                        "No saved replacements to apply.", parent=sd)
                    return
                push_undo()
                total_count = 0
                for pair in self.custom_replacements:
                    find, repl = pair[0], pair[1]
                    case_sensitive = len(pair) > 2 and pair[2]
                    for cue in cues:
                        old = cue['text']
                        if case_sensitive:
                            cue['text'] = cue['text'].replace(find, repl)
                        else:
                            cue['text'] = re.sub(re.escape(find), lambda m: repl,
                                                 cue['text'], flags=re.IGNORECASE)
                        if cue['text'] != old:
                            total_count += 1
                refresh_tree(cues)
                self.add_log(f"Applied {len(self.custom_replacements)} replacement rule(s), "
                             f"{total_count} cue(s) changed", 'INFO')
                messagebox.showinfo("Replacements Applied",
                    f"Applied {len(self.custom_replacements)} rule(s)\n"
                    f"{total_count} cue(s) modified", parent=sd)

            ttk.Button(btn_f, text="▶ Apply All", command=_apply_all).pack(side='left', padx=2)
            ttk.Button(btn_f, text="Remove", command=_remove_selected).pack(side='left', padx=2)
            ttk.Button(btn_f, text="Clear All", command=_clear_all).pack(side='left', padx=2)
            ttk.Button(btn_f, text="Close", command=sd.destroy).pack(side='right', padx=2)

            _refresh_list()

        def _run_spell_check():
            """Scan all cues for spelling errors. Returns errors dict or None."""
            try:
                from spellchecker import SpellChecker
            except ImportError:
                if messagebox.askyesno("Missing Package",
                    "pyspellchecker is not installed.\n\n"
                    "Would you like to install it now?",
                    parent=editor):
                    try:
                        self.add_log("Installing pyspellchecker...", 'INFO')
                        _pip_result = subprocess.run(
                            [sys.executable, '-m', 'pip', 'install',
                             '--user', '--break-system-packages', 'pyspellchecker'],
                            capture_output=True, text=True, timeout=60)
                        if _pip_result.returncode == 0:
                            from spellchecker import SpellChecker
                            self.add_log("pyspellchecker installed successfully", 'SUCCESS')
                        else:
                            messagebox.showerror("Install Failed",
                                f"pip install failed:\n{_pip_result.stderr[-300:]}",
                                parent=editor)
                            return None
                    except Exception as _e:
                        messagebox.showerror("Install Failed",
                            f"Could not install pyspellchecker:\n{_e}",
                            parent=editor)
                        return None
                else:
                    return None
            spell = SpellChecker()
            known = [w.lower() for w in self.custom_cap_words + self.custom_spell_words]
            if known:
                spell.word_frequency.load_words(known)
            spell_error_indices.clear()
            errors_by_cue = {}
            for i, cue in enumerate(cues):
                clean = re.sub(r'<[^>]+>|\{\\[^}]+\}|♪', '', cue['text'])
                words = re.findall(r"[a-zA-Z]+(?:'[a-zA-Z]+)?", clean)
                if not words:
                    continue
                unknown = spell.unknown(words)
                if unknown:
                    spell_error_indices.add(i)
                    errors_by_cue[i] = []
                    for w in words:
                        if w.lower() in unknown or w in unknown:
                            cands = spell.candidates(w)
                            errors_by_cue[i].append((w, sorted(cands) if cands else []))
            return errors_by_cue

        def _show_spell_check():
            """Run spell check and show interactive correction dialog."""
            errors_by_cue = _run_spell_check()
            if errors_by_cue is None:
                return
            if not errors_by_cue:
                spell_error_indices.clear()
                refresh_tree(cues)
                messagebox.showinfo("Spell Check", "No spelling errors found!", parent=editor)
                return
            refresh_tree(cues)

            error_list = []
            for ci in sorted(errors_by_cue.keys()):
                for word, cands in errors_by_cue[ci]:
                    error_list.append((ci, word, cands))

            current = [0]
            ignored = set()

            sd = tk.Toplevel(editor)
            sd.title("Spell Check")
            sd.geometry("500x440")
            sd.resizable(True, True)
            self._center_on_main(sd)
            sd.attributes('-topmost', True)

            sf = ttk.Frame(sd, padding=12)
            sf.pack(fill='both', expand=True)
            sf.columnconfigure(1, weight=1)
            _sp = {'padx': 6, 'pady': 4}

            stats_lbl = ttk.Label(sf, text=f"Found {len(error_list)} errors in {len(errors_by_cue)} cues",
                                  font=('Helvetica', 9))
            stats_lbl.grid(row=0, column=0, columnspan=2, sticky='w', **_sp)

            ttk.Label(sf, text="Not in dictionary:", font=('Helvetica', 10, 'bold')).grid(
                row=1, column=0, sticky='w', **_sp)
            word_var = tk.StringVar()
            ttk.Entry(sf, textvariable=word_var, state='readonly',
                      font=('Courier', 12)).grid(row=1, column=1, sticky='ew', **_sp)

            ttk.Label(sf, text="Context:").grid(row=2, column=0, sticky='nw', **_sp)
            ctx_var = tk.StringVar()
            ttk.Label(sf, textvariable=ctx_var, wraplength=380,
                      font=('Helvetica', 9), foreground='gray').grid(row=2, column=1, sticky='w', **_sp)

            ttk.Label(sf, text="Suggestions:").grid(row=3, column=0, sticky='nw', **_sp)
            sug_fr = ttk.Frame(sf)
            sug_fr.grid(row=3, column=1, sticky='nsew', **_sp)
            sug_fr.rowconfigure(0, weight=1)
            sug_fr.columnconfigure(0, weight=1)
            sf.rowconfigure(3, weight=1)

            sug_lb = tk.Listbox(sug_fr, height=6, font=('Courier', 10))
            sug_lb.grid(row=0, column=0, sticky='nsew')
            sug_sc = ttk.Scrollbar(sug_fr, orient='vertical', command=sug_lb.yview)
            sug_sc.grid(row=0, column=1, sticky='ns')
            sug_lb.configure(yscrollcommand=sug_sc.set)

            replace_var = tk.StringVar()
            def on_sug_sel(evt):
                sel = sug_lb.curselection()
                if sel:
                    replace_var.set(sug_lb.get(sel[0]))
            sug_lb.bind('<<ListboxSelect>>', on_sug_sel)

            ttk.Label(sf, text="Replace with:").grid(row=4, column=0, sticky='w', **_sp)
            ttk.Entry(sf, textvariable=replace_var, font=('Courier', 11)).grid(
                row=4, column=1, sticky='ew', **_sp)

            bf = ttk.Frame(sf)
            bf.grid(row=5, column=0, columnspan=2, sticky='ew', pady=(8, 0))

            def _show_err(idx):
                while idx < len(error_list):
                    ci, w, ca = error_list[idx]
                    if w.lower() not in ignored:
                        break
                    idx += 1
                else:
                    spell_error_indices.clear()
                    refresh_tree(cues)
                    messagebox.showinfo("Spell Check", "Spell check complete!", parent=sd)
                    sd.destroy()
                    return
                current[0] = idx
                ci, w, ca = error_list[idx]
                items = tree.get_children()
                if ci < len(items):
                    # Scroll so the match is near the middle, not at the edge
                    ahead = min(ci + 5, len(items) - 1)
                    tree.see(items[ahead])
                    tree.selection_set(items[ci])
                    tree.after(50, lambda: tree.see(items[ci]))
                word_var.set(w)
                ctx_var.set(cues[ci]['text'].replace('\n', ' / '))
                stats_lbl.configure(text=f"Error {idx+1} of {len(error_list)} (cue #{ci+1})")
                sug_lb.delete(0, 'end')
                for c in ca:
                    sug_lb.insert('end', c)
                if ca:
                    sug_lb.selection_set(0)
                    replace_var.set(ca[0])
                else:
                    replace_var.set(w)

            def _do_replace():
                ci, w, _ = error_list[current[0]]
                repl = replace_var.get().strip()
                if not repl: return
                push_undo()
                # Use str.replace for safe, literal replacement (first occurrence only)
                txt = cues[ci]['text']
                pos = txt.find(w)
                if pos == -1:
                    # Try case-insensitive find
                    pos = txt.lower().find(w.lower())
                if pos >= 0:
                    cues[ci]['text'] = txt[:pos] + repl + txt[pos + len(w):]
                refresh_tree(cues)
                _show_err(current[0] + 1)

            def _do_replace_all():
                _, w, _ = error_list[current[0]]
                repl = replace_var.get().strip()
                if not repl: return
                push_undo()
                for cue in cues:
                    # Case-sensitive replace first, then case-insensitive fallback
                    if w in cue['text']:
                        cue['text'] = cue['text'].replace(w, repl)
                    elif w.lower() in cue['text'].lower():
                        cue['text'] = re.sub(re.escape(w), repl, cue['text'], flags=re.IGNORECASE)
                refresh_tree(cues)
                _show_err(current[0] + 1)

            def _do_skip():
                _show_err(current[0] + 1)

            def _do_ignore():
                _, w, _ = error_list[current[0]]
                ignored.add(w.lower())
                _show_err(current[0] + 1)

            def _do_add_dict():
                _, w, _ = error_list[current[0]]
                if w.lower() not in [x.lower() for x in self.custom_spell_words]:
                    self.custom_spell_words.append(w)
                    self.save_preferences()
                ignored.add(w.lower())
                _show_err(current[0] + 1)

            def _do_add_name():
                _, w, _ = error_list[current[0]]
                # Add to custom_cap_words (character names — used by Fix ALL CAPS + spell check)
                if w not in self.custom_cap_words:
                    self.custom_cap_words.append(w)
                # Also add to spell words so it's never flagged
                if w.lower() not in [x.lower() for x in self.custom_spell_words]:
                    self.custom_spell_words.append(w)
                self.save_preferences()
                ignored.add(w.lower())
                _show_err(current[0] + 1)

            bf1 = ttk.Frame(bf)
            bf1.pack(fill='x')
            ttk.Button(bf1, text="Replace", command=_do_replace, width=10).pack(side='left', padx=2)
            ttk.Button(bf1, text="Replace All", command=_do_replace_all, width=10).pack(side='left', padx=2)
            ttk.Button(bf1, text="Skip", command=_do_skip, width=6).pack(side='left', padx=2)
            ttk.Button(bf1, text="Ignore", command=_do_ignore, width=8).pack(side='left', padx=2)

            bf2 = ttk.Frame(bf)
            bf2.pack(fill='x', pady=(4, 0))
            ttk.Button(bf2, text="Add to Dict", command=_do_add_dict, width=10).pack(side='left', padx=2)
            ttk.Button(bf2, text="Add as Name", command=_do_add_name, width=10).pack(side='left', padx=2)
            ttk.Button(bf2, text="Close", command=sd.destroy, width=6).pack(side='right', padx=2)

            _show_err(0)

        editor.bind('<F7>', lambda e: _show_spell_check())

        # ── Edit menu ──
        edit_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Edit", menu=edit_menu)
        edit_menu.add_command(label="Undo                Ctrl+Z", command=do_undo)
        edit_menu.add_command(label="Redo                Ctrl+Y", command=do_redo)
        edit_menu.add_command(label="Reset to Original", command=undo_all)
        edit_menu.add_separator()
        edit_menu.add_command(label="Delete Selected     Del", command=delete_selected)
        edit_menu.add_command(label="Split Cue", command=split_selected)
        edit_menu.add_command(label="Join Selected Cues", command=join_selected)

        # ── Timing menu ──
        timing_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Timing", menu=timing_menu)
        timing_menu.add_command(label="Offset / Stretch...", command=show_timing_dialog)
        timing_menu.add_command(label="Smart Sync...",
                                command=lambda: _show_smart_sync())

        # ── Quick Sync submenu ──
        quick_sync_menu = tk.Menu(timing_menu, tearoff=0)
        timing_menu.add_cascade(label="Quick Sync", menu=quick_sync_menu)

        def _quick_sync_first_cue():
            """Shift all cues so the first cue starts at a user-specified time.
            Includes an embedded mpv player for marking the exact time."""
            if not cues:
                messagebox.showinfo("No Subtitles", "Load subtitles first.",
                                    parent=editor)
                return

            qd = tk.Toplevel(editor)
            qd.title("Quick Sync — Set First Cue Time")
            qd.geometry("720x620")
            qd.minsize(640, 540)
            qd.resizable(True, True)
            self._center_on_main(qd)

            f = ttk.Frame(qd, padding=10)
            f.pack(fill='both', expand=True)
            f.columnconfigure(1, weight=1)
            f.rowconfigure(2, weight=1)  # video frame expands

            first_cue = cues[0]
            current_start = first_cue['start']
            preview_text = first_cue['text'].replace('\n', ' ')
            if len(preview_text) > 60:
                preview_text = preview_text[:57] + '...'

            # ── Video file ──
            ttk.Label(f, text="Video file:").grid(
                row=0, column=0, sticky='w', padx=4, pady=2)
            _qs_vpath = tk.StringVar()
            # Try to find video automatically
            try:
                if hasattr(editor, '_qs_last_video') and editor._qs_last_video:
                    _qs_vpath.set(editor._qs_last_video)
                elif current_path[0]:
                    _sub_dir = os.path.dirname(current_path[0])
                    _sub_stem = os.path.splitext(
                        os.path.basename(current_path[0]))[0]
                    for _i in range(3):
                        _dot = _sub_stem.rfind('.')
                        if _dot > 0:
                            _sub_stem = _sub_stem[:_dot]
                        else:
                            break
                    for ext in VIDEO_EXTENSIONS:
                        _vp = os.path.join(_sub_dir, _sub_stem + ext)
                        if os.path.isfile(_vp):
                            _qs_vpath.set(_vp)
                            break
            except Exception:
                pass

            _vpath_entry = ttk.Entry(f, textvariable=_qs_vpath)
            _vpath_entry.grid(row=0, column=1, sticky='ew', padx=4, pady=2)

            def _qs_browse():
                init_dir = os.path.dirname(_qs_vpath.get()) if _qs_vpath.get() \
                    else (os.path.dirname(current_path[0]) if current_path[0] else '')
                p = None
                if shutil.which('zenity'):
                    try:
                        cmd = ['zenity', '--file-selection',
                               '--title', 'Select Video File',
                               '--file-filter',
                               'Video files|*.mkv *.mp4 *.avi *.mov *.ts *.m2ts *.mts *.webm *.wmv *.flv',
                               '--file-filter', 'All files|*']
                        if init_dir:
                            cmd += ['--filename', init_dir + '/']
                        r = subprocess.run(cmd, capture_output=True,
                                           text=True, timeout=120)
                        if r.returncode == 0 and r.stdout.strip():
                            p = r.stdout.strip()
                    except Exception:
                        pass
                if not p:
                    p = filedialog.askopenfilename(
                        parent=qd, title="Select Video File",
                        initialdir=init_dir or None,
                        filetypes=[("Video files",
                                    "*.mkv *.mp4 *.avi *.mov *.ts *.m2ts"),
                                   ("All files", "*.*")])
                if p:
                    _qs_vpath.set(p)
                    # Auto-load the video after browse selection
                    qd.after(100, _play_video)
            ttk.Button(f, text="Browse...", command=_qs_browse).grid(
                row=0, column=2, padx=4, pady=2)

            # ── Embedded video player frame ──
            video_border = ttk.Frame(f, relief='sunken', borderwidth=2)
            video_border.grid(row=2, column=0, columnspan=3,
                              sticky='nsew', padx=4, pady=4)
            video_frame = tk.Frame(video_border, bg='black',
                                   width=640, height=360)
            video_frame.pack(fill='both', expand=True)
            video_frame.pack_propagate(False)

            _placeholder_label = tk.Label(video_frame,
                text="Drop a video file here or click Browse",
                bg='black', fg='#666', font=('Helvetica', 12))

            # ── Drag-and-drop support ──
            def _on_qs_drop(event):
                """Handle video files dropped onto the Quick Sync dialog."""
                raw = event.data
                paths = []
                if 'file://' in raw:
                    from urllib.parse import unquote, urlparse
                    for line in raw.splitlines():
                        line = line.strip()
                        if line.startswith('file://'):
                            decoded = unquote(urlparse(line).path)
                            if decoded:
                                paths.append(decoded)
                else:
                    i = 0
                    while i < len(raw):
                        if raw[i] == '{':
                            end = raw.find('}', i)
                            paths.append(raw[i + 1:end])
                            i = end + 2
                        elif raw[i] == ' ':
                            i += 1
                        else:
                            end = raw.find(' ', i)
                            if end == -1:
                                end = len(raw)
                            paths.append(raw[i:end])
                            i = end + 1

                # Find first video file in dropped paths
                for p in paths:
                    if os.path.isfile(p):
                        ext = os.path.splitext(p)[1].lower()
                        if ext in VIDEO_EXTENSIONS:
                            _qs_vpath.set(p)
                            qd.after(100, _play_video)
                            return

            try:
                qd.drop_target_register(DND_FILES)
                qd.dnd_bind('<<Drop>>', _on_qs_drop)
            except Exception:
                pass  # tkinterdnd2 not available
            _placeholder_label.place(relx=0.5, rely=0.5, anchor='center')

            # ── mpv player integration ──
            import tempfile as _qs_tempfile
            import socket as _qs_socket
            import json as _qs_json

            _mpv_proc = [None]
            _mpv_socket_path = os.path.join(
                _qs_tempfile.gettempdir(),
                f'docflix_mpv_{os.getpid()}')

            def _mpv_cmd(command_list):
                """Send a command to mpv via IPC and return the response."""
                try:
                    sock = _qs_socket.socket(
                        _qs_socket.AF_UNIX, _qs_socket.SOCK_STREAM)
                    sock.settimeout(2)
                    sock.connect(_mpv_socket_path)
                    payload = _qs_json.dumps(
                        {"command": command_list}) + '\n'
                    sock.sendall(payload.encode())
                    data = sock.recv(4096).decode()
                    sock.close()
                    return _qs_json.loads(data)
                except Exception:
                    return None

            def _play_video():
                vp = _qs_vpath.get().strip()
                if not vp or not os.path.isfile(vp):
                    messagebox.showwarning("No Video",
                        "Select a video file first.", parent=qd)
                    return

                # Kill previous mpv instance if running
                if _mpv_proc[0] and _mpv_proc[0].poll() is None:
                    _mpv_proc[0].terminate()
                    _mpv_proc[0].wait(timeout=5)

                # Clean up old socket
                if os.path.exists(_mpv_socket_path):
                    try:
                        os.unlink(_mpv_socket_path)
                    except OSError:
                        pass

                # Hide placeholder
                _placeholder_label.place_forget()

                # Get the X11 window ID for embedding
                video_frame.update_idletasks()
                wid = str(video_frame.winfo_id())

                # Launch mpv embedded in the video frame
                try:
                    _mpv_proc[0] = subprocess.Popen([
                        'mpv',
                        f'--input-ipc-server={_mpv_socket_path}',
                        f'--wid={wid}',
                        '--pause',
                        '--osd-level=2',
                        '--osd-fractions',
                        '--keep-open=yes',
                        '--no-border',
                        '--cursor-autohide=1000',
                        vp
                    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    mark_btn.configure(state='normal')
                    _mute_btn.configure(text="🔊")
                    _vol_var.set(100)
                    editor._qs_last_video = vp
                except FileNotFoundError:
                    messagebox.showerror("mpv Not Found",
                        "mpv is not installed.\n\n"
                        "Install with: sudo apt install mpv", parent=qd)
                    _placeholder_label.place(relx=0.5, rely=0.5, anchor='center')
                except Exception as e:
                    messagebox.showerror("Player Error",
                        f"Could not launch mpv:\n{e}", parent=qd)
                    _placeholder_label.place(relx=0.5, rely=0.5, anchor='center')

            def _mark_time():
                """Query mpv for current playback position and fill the time field."""
                if not _mpv_proc[0] or _mpv_proc[0].poll() is not None:
                    messagebox.showinfo("Player Closed",
                        "Load the video first.", parent=qd)
                    mark_btn.configure(state='disabled')
                    pass  # player closed
                    return

                resp = _mpv_cmd(["get_property", "playback-time"])
                if resp and 'data' in resp and resp['data'] is not None:
                    seconds = resp['data']
                    ms = int(seconds * 1000)
                    time_var.set(ms_to_srt_ts(ms))
                    time_entry.select_range(0, 'end')
                else:
                    messagebox.showwarning("Could Not Read Time",
                        "Could not get playback position from mpv.\n"
                        "Make sure the video is loaded.", parent=qd)

            def _mpv_seek(amount):
                """Seek mpv by amount in seconds."""
                if not _mpv_proc[0] or _mpv_proc[0].poll() is not None:
                    return
                _mpv_cmd(["seek", str(amount), "relative+exact"])

            def _mpv_frame_step(direction='forward'):
                """Step one frame forward or backward."""
                if not _mpv_proc[0] or _mpv_proc[0].poll() is not None:
                    return
                if direction == 'forward':
                    _mpv_cmd(["frame-step"])
                else:
                    _mpv_cmd(["frame-back-step"])

            def _mpv_pause_toggle():
                """Toggle play/pause."""
                if not _mpv_proc[0] or _mpv_proc[0].poll() is not None:
                    return
                _mpv_cmd(["cycle", "pause"])

            def _on_close():
                # Kill mpv and clean up socket
                if _mpv_proc[0] and _mpv_proc[0].poll() is None:
                    _mpv_proc[0].terminate()
                    try:
                        _mpv_proc[0].wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        _mpv_proc[0].kill()
                _mpv_proc[0] = None
                if os.path.exists(_mpv_socket_path):
                    try:
                        os.unlink(_mpv_socket_path)
                    except OSError:
                        pass
                # Reset state so next open starts fresh
                editor._qs_last_video = None
                qd.destroy()

            qd.protocol("WM_DELETE_WINDOW", _on_close)

            # ── Transport controls ──
            transport_f = ttk.Frame(f)
            transport_f.grid(row=3, column=0, columnspan=3, sticky='ew', pady=(4, 0))

            _tb_w = 3
            _transport_btns = [
                ("⏮",  lambda: _mpv_seek(-5),               "Rewind 5 seconds"),
                ("◀◀", lambda: _mpv_seek(-1),               "Rewind 1 second"),
                ("◀",  lambda: _mpv_seek(-0.1),             "Rewind 100ms"),
                ("|◀", lambda: _mpv_frame_step('backward'), "Back 1 frame"),
                ("⏯",  _mpv_pause_toggle,                   "Play / Pause"),
                ("▶|", lambda: _mpv_frame_step('forward'),  "Forward 1 frame"),
                ("▶",  lambda: _mpv_seek(0.1),              "Forward 100ms"),
                ("▶▶", lambda: _mpv_seek(1),                "Forward 1 second"),
                ("⏭",  lambda: _mpv_seek(5),                "Forward 5 seconds"),
            ]
            for _sym, _cmd, _tip in _transport_btns:
                _px = 2 if _sym == "⏯" else 1
                _b = ttk.Button(transport_f, text=_sym, width=_tb_w, command=_cmd)
                _b.pack(side='left', padx=_px)
                _create_tooltip(_b, _tip)

            mark_btn = ttk.Button(transport_f, text="⏱ Mark",
                                  command=_mark_time, width=6,
                                  state='disabled')
            mark_btn.pack(side='left', padx=(6, 0))
            _create_tooltip(mark_btn, "Capture current playback time")

            # ── Volume controls ──
            def _mpv_toggle_mute():
                if not _mpv_proc[0] or _mpv_proc[0].poll() is not None:
                    return
                _mpv_cmd(["cycle", "mute"])
                # Update mute button label
                resp = _mpv_cmd(["get_property", "mute"])
                if resp and 'data' in resp:
                    _mute_btn.configure(text="🔇" if resp['data'] else "🔊")

            def _mpv_set_volume(val):
                if not _mpv_proc[0] or _mpv_proc[0].poll() is not None:
                    return
                _mpv_cmd(["set_property", "volume", float(val)])

            _mute_btn = ttk.Button(transport_f, text="🔊", width=2,
                                   command=_mpv_toggle_mute)
            _mute_btn.pack(side='right', padx=(4, 0))
            _create_tooltip(_mute_btn, "Mute / Unmute")

            _vol_var = tk.DoubleVar(value=100)
            _vol_scale = ttk.Scale(transport_f, from_=0, to=100,
                                   orient='horizontal', length=80,
                                   variable=_vol_var,
                                   command=_mpv_set_volume)
            _vol_scale.pack(side='right', padx=2)
            _create_tooltip(_vol_scale, "Volume")

            # ── Sync controls ──
            ttk.Separator(f, orient='horizontal').grid(
                row=4, column=0, columnspan=3, sticky='ew', pady=6)

            sync_f = ttk.Frame(f)
            sync_f.grid(row=5, column=0, columnspan=3, sticky='ew', padx=4)
            sync_f.columnconfigure(1, weight=1)

            ttk.Label(sync_f, text="First cue:",
                      font=('Helvetica', 9, 'bold')).grid(
                          row=0, column=0, sticky='w', pady=1)
            ttk.Label(sync_f, text=f'"{preview_text}"',
                      font=('Helvetica', 9), foreground='gray').grid(
                          row=0, column=1, columnspan=2, sticky='w', padx=8, pady=1)

            ttk.Label(sync_f, text="Current:").grid(
                row=1, column=0, sticky='w', pady=1)
            ttk.Label(sync_f, text=current_start,
                      font=('Courier', 10)).grid(
                          row=1, column=1, sticky='w', padx=8, pady=1)

            ttk.Label(sync_f, text="New start:").grid(
                row=2, column=0, sticky='w', pady=2)
            time_var = tk.StringVar(value=current_start)
            _time_f = ttk.Frame(sync_f)
            _time_f.grid(row=2, column=1, columnspan=2, sticky='w', padx=8, pady=2)
            time_entry = ttk.Entry(_time_f, textvariable=time_var, width=16,
                                   font=('Courier', 10))
            time_entry.pack(side='left')
            ttk.Label(_time_f, text="HH:MM:SS,mmm",
                      foreground='gray', font=('Helvetica', 8)).pack(
                          side='left', padx=8)

            offset_var = tk.StringVar(value="Offset: 0ms")
            ttk.Label(sync_f, textvariable=offset_var,
                      font=('Helvetica', 9), foreground='#666').grid(
                          row=3, column=0, columnspan=3, sticky='w', pady=1)

            def _update_offset(*_args):
                try:
                    new_ms = srt_ts_to_ms(time_var.get().strip())
                    old_ms = srt_ts_to_ms(current_start)
                    diff = new_ms - old_ms
                    sign = '+' if diff >= 0 else ''
                    offset_var.set(f"Offset: {sign}{diff}ms ({sign}{diff/1000:.1f}s)")
                except Exception:
                    offset_var.set("Offset: (invalid time format)")
            time_var.trace_add('write', _update_offset)

            # ── Action buttons ──
            btn_f = ttk.Frame(f)
            btn_f.grid(row=6, column=0, columnspan=3, sticky='ew', pady=(8, 0))

            def _apply_first_cue():
                nonlocal cues
                try:
                    new_ms = srt_ts_to_ms(time_var.get().strip())
                except Exception:
                    messagebox.showwarning("Invalid Time",
                        "Enter time in SRT format: HH:MM:SS,mmm", parent=qd)
                    return
                old_ms = srt_ts_to_ms(current_start)
                offset = new_ms - old_ms
                if offset == 0:
                    _on_close()
                    return
                push_undo()
                cues = shift_timestamps(cues, offset)
                refresh_tree(cues)
                sign = '+' if offset > 0 else ''
                self.add_log(f"Quick Sync: shifted all cues {sign}{offset}ms "
                             f"(first cue → {time_var.get().strip()})", 'SUCCESS')
                _on_close()

            time_entry.bind('<Return>', lambda e: _apply_first_cue())
            _apply_btn = ttk.Button(btn_f, text="Apply",
                                    command=_apply_first_cue, width=8)
            _apply_btn.pack(side='left', padx=2)
            _create_tooltip(_apply_btn, "Shift all cues by the offset and close")
            _cancel_btn = ttk.Button(btn_f, text="Cancel",
                                     command=_on_close, width=8)
            _cancel_btn.pack(side='left', padx=2)
            _create_tooltip(_cancel_btn, "Close without applying changes")

            # Auto-load video if one was detected
            if _qs_vpath.get().strip() and os.path.isfile(_qs_vpath.get().strip()):
                qd.after(300, _play_video)

        quick_sync_menu.add_command(label="Set First Cue Time...",
                                    command=_quick_sync_first_cue)

        def _show_smart_sync():
            """Auto-sync subtitles using Whisper speech recognition."""
            import threading

            if not cues:
                messagebox.showinfo("No Subtitles", "Load subtitles first.", parent=editor)
                return

            # Check faster-whisper
            try:
                from faster_whisper import WhisperModel
            except ImportError:
                if messagebox.askyesno("Missing Package",
                    "faster-whisper is not installed.\n\n"
                    "Would you like to install it now?\n"
                    "(This may take a few minutes — downloads ~200MB)",
                    parent=editor):
                    try:
                        self.add_log("Installing faster-whisper...", 'INFO')
                        _pip_result = subprocess.run(
                            [sys.executable, '-m', 'pip', 'install',
                             '--user', '--break-system-packages', 'faster-whisper'],
                            capture_output=True, text=True, timeout=300)
                        if _pip_result.returncode == 0:
                            self.add_log("faster-whisper installed", 'SUCCESS')
                        else:
                            messagebox.showerror("Install Failed",
                                f"pip install failed:\n{_pip_result.stderr[-300:]}",
                                parent=editor)
                            return
                    except Exception as _e:
                        messagebox.showerror("Install Failed", str(_e), parent=editor)
                        return
                else:
                    return

            vpath = filepath  # internal editor always has a video file path

            sd = tk.Toplevel(editor)
            sd.title("Smart Sync")
            sd.geometry("560x580")
            sd.resizable(True, True)
            self._center_on_main(sd)

            f = ttk.Frame(sd, padding=12)
            f.pack(fill='both', expand=True)
            f.columnconfigure(1, weight=1)
            _sp = {'padx': 6, 'pady': 4}

            ttk.Label(f, text="Video file:").grid(row=0, column=0, sticky='w', **_sp)
            vpath_var = tk.StringVar(value=vpath or '')
            ttk.Entry(f, textvariable=vpath_var).grid(row=0, column=1, sticky='ew', **_sp)
            def _browse_vid():
                # Start in the subtitle's folder if available
                init_dir = ''
                if vpath_var.get():
                    init_dir = os.path.dirname(vpath_var.get())
                elif current_path[0]:
                    init_dir = os.path.dirname(current_path[0])
                # Try zenity first (better sizing), fall back to tkinter
                p = None
                if shutil.which('zenity'):
                    try:
                        cmd = ['zenity', '--file-selection',
                               '--title', 'Select Video File',
                               '--file-filter', 'Video files|*.mkv *.mp4 *.avi *.mov *.ts *.m2ts *.mts *.webm *.wmv *.flv',
                               '--file-filter', 'All files|*']
                        if init_dir:
                            cmd += ['--filename', init_dir + '/']
                        result = subprocess.run(cmd, capture_output=True,
                                                text=True, timeout=120)
                        if result.returncode == 0 and result.stdout.strip():
                            p = result.stdout.strip()
                    except Exception:
                        pass
                if not p:
                    p = filedialog.askopenfilename(
                        parent=sd,
                        title="Select Video File",
                        initialdir=init_dir or None,
                        filetypes=[("Video files", "*.mkv *.mp4 *.avi *.mov *.ts *.m2ts"),
                                   ("All files", "*.*")])
                if p:
                    vpath_var.set(p)
            ttk.Button(f, text="Browse...", command=_browse_vid).grid(row=0, column=2, **_sp)

            model_label = ttk.Label(f, text="Whisper model:")
            model_label.grid(row=1, column=0, sticky='w', **_sp)
            model_f = ttk.Frame(f)
            model_f.grid(row=1, column=1, columnspan=2, sticky='w', **_sp)
            model_var = tk.StringVar(value='base')
            for m, tip in [('tiny', '~75MB, fastest'),
                           ('base', '~150MB, good balance'),
                           ('small', '~500MB, more accurate')]:
                ttk.Radiobutton(model_f, text=f"{m} ({tip})",
                               variable=model_var, value=m).pack(anchor='w')

            ttk.Label(f, text="Language:").grid(row=2, column=0, sticky='w', **_sp)
            lang_var = tk.StringVar(value='en')
            lang_f = ttk.Frame(f)
            lang_f.grid(row=2, column=1, columnspan=2, sticky='w', **_sp)
            ttk.Entry(lang_f, textvariable=lang_var, width=5).pack(side='left')
            ttk.Label(lang_f, text="(en, fr, es, de, etc. — blank = auto-detect)",
                      foreground='gray', font=('Helvetica', 8)).pack(side='left', padx=8)

            # ── Engine selection ──
            ttk.Label(f, text="Engine:").grid(row=3, column=0, sticky='w', **_sp)
            engine_f = ttk.Frame(f)
            engine_f.grid(row=3, column=1, columnspan=2, sticky='w', **_sp)
            engine_var = tk.StringVar(value='faster-whisper')

            def _on_engine_change():
                eng = engine_var.get()
                if eng == 'whisperx':
                    finetune_var.set('200')
                    finetune_hint.config(
                        text="ms  (phoneme onset is ~200ms before perceived speech)")
                    direct_rb.configure(state='normal')
                else:
                    finetune_var.set('400')
                    finetune_hint.config(
                        text="ms  (applied after sync — compensates for Whisper timing)")
                    # Direct Align requires WhisperX — switch away if selected
                    if scan_mode_var.get() == 'direct':
                        scan_mode_var.set('quick')
                    direct_rb.configure(state='disabled')
                _on_scan_mode_change()

            ttk.Radiobutton(engine_f, text="Standard (faster-whisper)",
                           variable=engine_var, value='faster-whisper',
                           command=_on_engine_change).pack(anchor='w')
            ttk.Radiobutton(engine_f,
                           text="Precise (WhisperX) — phoneme-level alignment",
                           variable=engine_var, value='whisperx',
                           command=_on_engine_change).pack(anchor='w')

            # ── Scan mode ──
            ttk.Label(f, text="Scan mode:").grid(row=4, column=0, sticky='w', **_sp)
            scan_f = ttk.Frame(f)
            scan_f.grid(row=4, column=1, columnspan=2, sticky='w', **_sp)
            scan_mode_var = tk.StringVar(value='quick')

            def _on_scan_mode_change():
                mode = scan_mode_var.get()
                if mode == 'quick':
                    seg_label.grid()
                    sample_f.grid()
                    model_label.grid()
                    model_f.grid()
                elif mode == 'full':
                    seg_label.grid_remove()
                    sample_f.grid_remove()
                    model_label.grid()
                    model_f.grid()
                else:  # direct
                    seg_label.grid_remove()
                    sample_f.grid_remove()
                    model_label.grid_remove()
                    model_f.grid_remove()

            ttk.Radiobutton(scan_f, text="Quick Scan", variable=scan_mode_var,
                           value='quick', command=_on_scan_mode_change).pack(side='left', padx=(0, 8))
            ttk.Radiobutton(scan_f, text="Full Scan (for Re-time)",
                           variable=scan_mode_var, value='full',
                           command=_on_scan_mode_change).pack(side='left', padx=(0, 8))
            direct_rb = ttk.Radiobutton(scan_f,
                           text="Direct Align",
                           variable=scan_mode_var, value='direct',
                           command=_on_scan_mode_change, state='disabled')
            direct_rb.pack(side='left')

            seg_label = ttk.Label(f, text="Segments:")
            seg_label.grid(row=5, column=0, sticky='w', **_sp)
            sample_f = ttk.Frame(f)
            sample_f.grid(row=5, column=1, columnspan=2, sticky='w', **_sp)
            segments_var = tk.StringVar(value='3')
            seg_spin = tk.Spinbox(sample_f, textvariable=segments_var, from_=1, to=20,
                        width=3)
            seg_spin.pack(side='left')
            ttk.Label(sample_f, text="× ").pack(side='left')
            sample_len_var = tk.StringVar(value='5')
            len_spin = tk.Spinbox(sample_f, textvariable=sample_len_var, from_=1, to=30,
                        width=3)
            len_spin.pack(side='left')
            ttk.Label(sample_f, text="min each",
                      foreground='gray', font=('Helvetica', 8)).pack(side='left', padx=4)

            # ── Offset adjustment ──
            ttk.Label(f, text="Fine-tune:").grid(row=6, column=0, sticky='w', **_sp)
            finetune_f = ttk.Frame(f)
            finetune_f.grid(row=6, column=1, columnspan=2, sticky='w', **_sp)
            finetune_var = tk.StringVar(value='400')
            tk.Spinbox(finetune_f, textvariable=finetune_var, from_=-2000, to=2000,
                       increment=50, width=6).pack(side='left')
            finetune_hint = ttk.Label(finetune_f,
                      text="ms  (applied after sync — compensates for Whisper timing)",
                      foreground='gray', font=('Helvetica', 8))
            finetune_hint.pack(side='left', padx=4)

            status_var = tk.StringVar(value="Ready — click Start to begin")
            ttk.Label(f, textvariable=status_var, wraplength=450,
                      font=('Helvetica', 9)).grid(row=7, column=0, columnspan=3, sticky='w', **_sp)

            progress_var = tk.DoubleVar(value=0)
            ttk.Progressbar(f, variable=progress_var, maximum=100,
                           mode='determinate').grid(row=8, column=0, columnspan=3,
                                                      sticky='ew', **_sp)

            result_frame = ttk.LabelFrame(f, text="Results", padding=6)
            result_frame.grid(row=9, column=0, columnspan=3, sticky='nsew', **_sp)
            result_frame.columnconfigure(0, weight=1)
            result_frame.rowconfigure(0, weight=1)
            f.rowconfigure(9, weight=1)

            result_text = tk.Text(result_frame, height=8, wrap='word',
                                 font=('Courier', 9), state='disabled',
                                 bg='#1e1e1e', fg='#d4d4d4')
            result_text.grid(row=0, column=0, sticky='nsew')
            r_scroll = ttk.Scrollbar(result_frame, orient='vertical', command=result_text.yview)
            r_scroll.grid(row=0, column=1, sticky='ns')
            result_text.configure(yscrollcommand=r_scroll.set)

            def _rlog(msg):
                result_text.configure(state='normal')
                result_text.insert('end', msg + '\n')
                result_text.see('end')
                result_text.configure(state='disabled')

            btn_f = ttk.Frame(f)
            btn_f.grid(row=10, column=0, columnspan=3, sticky='ew', pady=(8, 0))

            cancel_event = threading.Event()
            sync_result = [None]
            pre_sync_cues = [None]  # snapshot before sync — for repeatable Re-time

            def _start():
                vp = vpath_var.get().strip()
                if not vp or not os.path.isfile(vp):
                    messagebox.showwarning("No Video", "Select a video file.", parent=sd)
                    return

                # Save cues before sync so Re-time/Apply can repeat with different fine-tune
                import copy as _copy
                pre_sync_cues[0] = _copy.deepcopy(cues)

                # ── Engine-aware dependency check ──
                _engine = engine_var.get()
                if _engine == 'whisperx':
                    try:
                        import whisperx
                    except ImportError:
                        if messagebox.askyesno("Missing Package",
                            "WhisperX is not installed.\n\n"
                            "Would you like to install it now?\n"
                            "(Requires PyTorch — downloads ~2GB)",
                            parent=sd):
                            # Run pip install in background thread with progress
                            start_btn.configure(state='disabled')
                            status_var.set("Installing whisperx (downloading ~2GB)...")
                            self.add_log("Installing whisperx...", 'INFO')
                            _rlog("Installing whisperx — this may take several minutes...")
                            # Switch progress bar to indeterminate mode
                            _install_pbar = None
                            for _w in f.winfo_children():
                                if isinstance(_w, ttk.Progressbar):
                                    _install_pbar = _w
                                    break
                            if _install_pbar:
                                _install_pbar.configure(mode='indeterminate')
                                _install_pbar.start(15)

                            def _do_whisperx_install():
                                try:
                                    proc = subprocess.Popen(
                                        [sys.executable, '-m', 'pip', 'install',
                                         '--user', '--break-system-packages',
                                         '--progress-bar', 'off',
                                         'whisperx', 'transformers<4.45'],
                                        stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT,
                                        text=True)
                                    for line in proc.stdout:
                                        line = line.rstrip()
                                        if line:
                                            sd.after(0, lambda l=line:
                                                     status_var.set(l[:80]))
                                            sd.after(0, lambda l=line: _rlog(l))
                                    proc.wait(timeout=600)
                                    if proc.returncode == 0:
                                        sd.after(0, lambda: status_var.set(
                                            "whisperx installed — click Start"))
                                        sd.after(0, lambda: _rlog(
                                            "whisperx installed successfully"))
                                        sd.after(0, lambda: self.add_log(
                                            "whisperx installed successfully",
                                            'SUCCESS'))
                                    else:
                                        sd.after(0, lambda: status_var.set(
                                            "whisperx install failed"))
                                        sd.after(0, lambda: _rlog(
                                            "Install failed — check log above"))
                                except Exception as _e:
                                    sd.after(0, lambda: status_var.set(
                                        f"Install error: {_e}"))
                                    sd.after(0, lambda: _rlog(f"Error: {_e}"))
                                finally:
                                    def _reset_after_install():
                                        start_btn.configure(state='normal')
                                        if _install_pbar:
                                            _install_pbar.stop()
                                            _install_pbar.configure(
                                                mode='determinate')
                                            progress_var.set(0)
                                    sd.after(0, _reset_after_install)

                            import threading as _inst_threading
                            _inst_threading.Thread(
                                target=_do_whisperx_install,
                                daemon=True).start()
                            return  # exit _start(); user clicks Start after install
                        else:
                            return

                start_btn.configure(state='disabled')
                apply_btn.configure(state='disabled')
                cancel_event.clear()
                # Capture Tk variables on main thread before entering background thread
                lang = lang_var.get().strip() or None
                model = model_var.get()
                _scan_mode = scan_mode_var.get()
                if _scan_mode == 'direct':
                    engine_value = 'whisperx-align'
                else:
                    engine_value = engine_var.get()
                is_full_scan = _scan_mode == 'full'
                _seg_str = segments_var.get().strip()
                _len_str = sample_len_var.get().strip()
                n_segs = int(_seg_str) if _seg_str.isdigit() else 3
                s_mins = int(_len_str) if _len_str.isdigit() else 5
                _ft_str = finetune_var.get().strip().lstrip('+')
                finetune_ms = int(_ft_str) if _ft_str.lstrip('-').isdigit() else 400
                if is_full_scan or _scan_mode == 'direct':
                    n_segs = 0  # signal for full scan
                    s_mins = 0

                import time as _sync_time
                _last_ui_update = [0]

                def _progress(msg):
                    # Throttle UI updates to max 4 per second to avoid flooding Tk event queue
                    now = _sync_time.monotonic()
                    is_milestone = ('segment' in msg.lower() and '/' in msg) or \
                                   'Matched' in msg or 'Loading' in msg or \
                                   'Extracting' in msg or 'Transcribed' in msg or \
                                   'Aligning' in msg or 'alignment' in msg.lower() or \
                                   'WhisperX' in msg or 'Falling back' in msg or \
                                   'failed' in msg.lower() or 'complete' in msg.lower() or \
                                   'error' in msg.lower() or 'RESULT' in msg or \
                                   'Done' in msg or '===' in msg or 'Drift' in msg or \
                                   'Sync' in msg
                    if not is_milestone and (now - _last_ui_update[0]) < 0.25:
                        return
                    _last_ui_update[0] = now

                    def _do_update():
                        status_var.set(msg)
                        _rlog(msg)
                        import re as _re
                        m = _re.search(r'segment (\d+)/(\d+)', msg, _re.IGNORECASE)
                        if m:
                            seg_n, seg_t = int(m.group(1)), int(m.group(2))
                            progress_var.set((seg_n / seg_t) * 100)
                        m2 = _re.search(r'Matching cue (\d+)/(\d+)', msg)
                        if m2:
                            mc, mt = int(m2.group(1)), int(m2.group(2))
                            progress_var.set((mc / mt) * 100)
                        elif 'Extracting audio' in msg:
                            progress_var.set(0)
                    sd.after(0, _do_update)

                def _set_start_enabled():
                    start_btn.configure(state='normal')
                def _set_apply_enabled():
                    apply_btn.configure(state='normal')
                    progress_var.set(100)

                def _run():
                    try:
                        result = smart_sync(vp, cues, model_size=model,
                                            language=lang,
                                            num_segments=n_segs,
                                            sample_minutes=s_mins,
                                            progress_callback=_progress,
                                            cancel_event=cancel_event,
                                            engine=engine_value)
                    except Exception as _e:
                        _progress(f"Error: {_e}")
                        result = None
                    sync_result[0] = result

                    import time as _t
                    _t.sleep(0.3)

                    if cancel_event.is_set():
                        _progress("Sync cancelled by user")
                    elif result:
                        try:
                            ro = result['offset_ms']
                            rd = result['drift_ms']
                            rm = result['matches']
                            sign = '+' if ro > 0 else ''
                            _progress(f"{'='*40}")
                            _progress(f"RESULT: Offset: {sign}{ro}ms ({sign}{ro/1000:.1f}s)")
                            _progress(f"Drift: {rd:+d}ms")
                            _progress(f"Matched: {len(rm)}/{len(cues)} cues")
                            _progress(f"{'='*40}")
                            for ci, wt, ct, sim, txt in rm[:10]:
                                _progress(f"  #{ci+1} sim={sim:.0%} "
                                          f"sub={ms_to_srt_ts(ct)[:8]} "
                                          f"audio={ms_to_srt_ts(wt)[:8]} "
                                          f"\"{txt}\"")
                            _progress(f"Done — click Apply Sync to apply {sign}{ro}ms offset")
                            sd.after(0, lambda: apply_btn.configure(state='normal'))
                            sd.after(0, lambda: retime_btn.configure(state='normal'))
                            sd.after(0, lambda: progress_var.set(100))
                        except Exception as _e:
                            _progress(f"Error displaying results: {_e}")
                    else:
                        _progress("Sync failed — no results")

                    sd.after(0, _set_start_enabled)

                t = threading.Thread(target=_run, daemon=True)
                t.start()

            def _apply():
                nonlocal cues
                if not sync_result[0]:
                    return
                offset = sync_result[0]['offset_ms']
                _ft = finetune_var.get().strip().lstrip('+')
                ft = int(_ft) if _ft.lstrip('-').isdigit() else 400
                total_offset = offset + ft

                # Always apply from the pre-sync snapshot so fine-tune is repeatable
                import copy as _copy
                push_undo()
                if pre_sync_cues[0] is not None:
                    cues = _copy.deepcopy(pre_sync_cues[0])
                cues = shift_timestamps(cues, total_offset)
                refresh_tree(cues)
                sign = '+' if total_offset > 0 else ''
                self.add_log(f"Smart Sync applied: {sign}{total_offset}ms "
                             f"(offset {offset:+d}ms + fine-tune {ft:+d}ms)", 'SUCCESS')
                _rlog(f"\nApplied: {sign}{total_offset}ms (offset {offset:+d} + fine-tune {ft:+d})")
                status_var.set(f"Sync applied: {sign}{total_offset}ms")

            def _retime():
                nonlocal cues
                if not sync_result[0]:
                    return
                result = sync_result[0]
                matched = result['matches']
                _ft = finetune_var.get().strip().lstrip('+')
                ft = int(_ft) if _ft.lstrip('-').isdigit() else 400

                # Always retime from the pre-sync snapshot so fine-tune is repeatable
                import copy as _copy
                push_undo()
                if pre_sync_cues[0] is not None:
                    cues = _copy.deepcopy(pre_sync_cues[0])
                cues = retime_subtitles(cues, matched)
                if ft != 0:
                    cues = shift_timestamps(cues, ft)
                refresh_tree(cues)
                ft_msg = f" + fine-tune {ft:+d}ms" if ft else ""
                self.add_log(f"Re-timed {len(cues)} cues using {len(matched)} anchors{ft_msg}",
                             'SUCCESS')
                _rlog(f"\nRe-timed {len(cues)} cues using {len(matched)} anchors{ft_msg}")
                status_var.set(f"Re-timed using {len(matched)} anchors{ft_msg}")

            def _cancel():
                cancel_event.set()
                status_var.set("Cancelling...")

            start_btn = ttk.Button(btn_f, text="▶ Start", command=_start, width=8)
            start_btn.pack(side='left', padx=2)
            apply_btn = ttk.Button(btn_f, text="Apply Sync", command=_apply,
                                    width=10, state='disabled')
            apply_btn.pack(side='left', padx=2)
            retime_btn = ttk.Button(btn_f, text="Re-time All", command=_retime,
                                     width=10, state='disabled')
            retime_btn.pack(side='left', padx=2)
            ttk.Button(btn_f, text="Cancel", command=_cancel, width=8).pack(side='left', padx=2)

            def _save_from_sync():
                do_save_file()
                _rlog("Saved.")
                status_var.set("Saved")

            ttk.Button(btn_f, text="💾 Save", command=_save_from_sync,
                       width=6).pack(side='right', padx=2)
            ttk.Button(btn_f, text="Close", command=sd.destroy, width=6).pack(side='right', padx=2)

        # ══════════════════════════════════════════════════════════════════════
        # Search & Replace toolbar (compact single row)
        # ══════════════════════════════════════════════════════════════════════
        search_frame = ttk.Frame(editor, padding=(10, 4, 10, 4))
        search_frame.pack(fill='x')

        def _add_entry_context_menu(entry):
            """Attach a right-click Cut/Copy/Paste menu to a ttk.Entry."""
            menu = tk.Menu(entry, tearoff=0)
            menu.add_command(label="Cut",
                command=lambda: entry.event_generate('<<Cut>>'))
            menu.add_command(label="Copy",
                command=lambda: entry.event_generate('<<Copy>>'))
            menu.add_command(label="Paste",
                command=lambda: entry.event_generate('<<Paste>>'))
            menu.add_separator()
            menu.add_command(label="Select All",
                command=lambda: (entry.select_range(0, 'end'),
                                 entry.icursor('end')))
            def _show(event):
                menu.tk_popup(event.x_root, event.y_root)
            entry.bind('<Button-3>', _show)

        ttk.Label(search_frame, text="Find:").pack(side='left')
        find_entry = ttk.Entry(search_frame, textvariable=find_var, width=20)
        find_entry.pack(side='left', padx=(2, 6))
        find_entry.bind('<Return>', lambda e: do_find())
        _add_entry_context_menu(find_entry)

        ttk.Label(search_frame, text="Replace:").pack(side='left')
        replace_entry = ttk.Entry(search_frame, textvariable=replace_var, width=20)
        replace_entry.pack(side='left', padx=(2, 6))
        _add_entry_context_menu(replace_entry)

        ttk.Button(search_frame, text="Find", command=do_find).pack(side='left', padx=2)
        ttk.Button(search_frame, text="Replace",
                   command=do_replace_one).pack(side='left', padx=2)
        ttk.Button(search_frame, text="Replace All",
                   command=do_replace_all).pack(side='left', padx=2)
        ttk.Checkbutton(search_frame, text="Wrap",
                        variable=wrap_around).pack(side='left', padx=(6, 2))

        # Bind Ctrl+F to focus the find field
        editor.bind('<Control-f>', lambda e: find_entry.focus_set())
        editor.bind('<Control-F>', lambda e: find_entry.focus_set())

        ttk.Separator(editor, orient='horizontal').pack(fill='x')

        # ══════════════════════════════════════════════════════════════════════
        # Treeview with subtitle entries
        # ══════════════════════════════════════════════════════════════════════
        tree_frame = ttk.Frame(editor)
        tree_frame.pack(fill='both', expand=True, padx=10, pady=(4, 0))

        tree_scroll_y = ttk.Scrollbar(tree_frame, orient='vertical')
        tree_scroll_y.pack(side='right', fill='y')

        tree = ttk.Treeview(tree_frame, columns=('num', 'time', 'text'),
                            show='headings', yscrollcommand=tree_scroll_y.set,
                            selectmode='extended')
        tree_scroll_y.config(command=tree.yview)

        tree.heading('num', text='#')
        tree.heading('time', text='Timestamp')
        tree.heading('text', text='Text')
        tree.column('num', width=40, minwidth=30, stretch=False)
        tree.column('time', width=260, minwidth=220, stretch=False)
        tree.column('text', width=500, minwidth=200, stretch=True)
        tree.pack(fill='both', expand=True)

        # ── Color coding ──
        tree.tag_configure(TAG_MODIFIED, background='#fff3cd')   # yellow — modified
        tree.tag_configure(TAG_HI, background='#cce5ff')         # blue — HI content
        tree.tag_configure(TAG_TAGS, background='#f8d7da')       # pink — has tags
        tree.tag_configure(TAG_LONG, background='#ffe0b2')       # orange — long lines
        tree.tag_configure(TAG_SEARCH, background='#c8e6c9')     # green — search match
        tree.tag_configure(TAG_SPELL, background='#f5c6cb')      # red/salmon — spelling errors

        # ── Mousewheel scrolling (consume events to prevent bleed-through) ──
        def on_tree_mousewheel(event):
            tree.yview_scroll(int(-1 * (event.delta / 120)), 'units')
            return 'break'

        def on_tree_scroll_up(event):
            tree.yview_scroll(-3, 'units')
            return 'break'

        def on_tree_scroll_down(event):
            tree.yview_scroll(3, 'units')
            return 'break'

        tree.bind('<MouseWheel>', on_tree_mousewheel)
        tree.bind('<Button-4>', on_tree_scroll_up)
        tree.bind('<Button-5>', on_tree_scroll_down)

        # ── Inline edit on double-click ──
        edit_entry = None

        def on_double_click(event):
            nonlocal edit_entry, cues
            item = tree.identify_row(event.y)
            col = tree.identify_column(event.x)
            if not item or col != '#3':  # Only edit the text column
                return

            bbox = tree.bbox(item, col)
            if not bbox:
                return
            x, y, w, h = bbox
            idx = int(item)

            if edit_entry:
                edit_entry.destroy()

            push_undo()
            edit_entry = tk.Text(tree_frame, wrap='word', height=3)
            edit_entry.place(x=x, y=y, width=w, height=max(h, 60))
            edit_entry.insert('1.0', cues[idx]['text'])
            edit_entry.focus_set()
            edit_entry.tag_configure('sel', background='#4a90d9')

            def save_edit(e=None):
                nonlocal edit_entry
                new_text = edit_entry.get('1.0', 'end-1c').strip()
                if new_text:
                    cues[idx]['text'] = new_text
                    display = new_text.replace('\n', ' \\n ')
                    tree.set(item, 'text', display)
                    # Update row color tag
                    orig_text = original_cues[idx]['text'] if idx < len(original_cues) else None
                    ctags = _classify_cue(cues[idx], orig_text)
                    row_tag = ''
                    for t in (TAG_MODIFIED, TAG_HI, TAG_TAGS, TAG_LONG):
                        if t in ctags:
                            row_tag = t
                            break
                    tree.item(item, tags=(row_tag,) if row_tag else ())
                else:
                    del cues[idx]
                    refresh_tree(cues)
                edit_entry.destroy()
                edit_entry = None
                # Update stats
                deleted_count.set(len(original_cues) - len(cues))
                mod = sum(1 for i, c in enumerate(cues) if i < len(original_cues)
                          and c['text'] != original_cues[i]['text'])
                modified_count.set(mod)
                long_count = sum(1 for c in cues
                                 if any(len(l) > MAX_CHARS_PER_LINE
                                        for l in c['text'].split('\n')))
                stats_parts = [
                    f"{len(cues)} entries",
                    f"{modified_count.get()} modified",
                    f"{deleted_count.get()} removed",
                ]
                if long_count:
                    stats_parts.append(f"{long_count} long lines")
                stats_label.configure(text=" │ ".join(stats_parts))

            def cancel_edit(e=None):
                nonlocal edit_entry
                if edit_entry:
                    edit_entry.destroy()
                    edit_entry = None
                    if undo_stack:
                        undo_stack.pop()  # discard the undo we pushed

            edit_entry.bind('<Escape>', cancel_edit)
            edit_entry.bind('<Control-Return>', save_edit)
            edit_entry.bind('<Tab>', save_edit)

            # Right-click context menu for copy/paste
            edit_ctx = tk.Menu(edit_entry, tearoff=0)

            def _edit_action(action):
                """Perform an edit action and refocus the edit widget."""
                if not edit_entry:
                    return
                if action == 'cut':
                    edit_entry.event_generate('<<Cut>>')
                elif action == 'copy':
                    edit_entry.event_generate('<<Copy>>')
                elif action == 'paste':
                    edit_entry.event_generate('<<Paste>>')
                elif action == 'select_all':
                    edit_entry.tag_add('sel', '1.0', 'end')
                    edit_entry.mark_set('insert', 'end')
                edit_entry.focus_force()

            edit_ctx.add_command(label="Cut", command=lambda: _edit_action('cut'))
            edit_ctx.add_command(label="Copy", command=lambda: _edit_action('copy'))
            edit_ctx.add_command(label="Paste", command=lambda: _edit_action('paste'))
            edit_ctx.add_separator()
            edit_ctx.add_command(label="Select All",
                                command=lambda: _edit_action('select_all'))

            _edit_ctx_open = [False]

            def show_edit_ctx(event):
                _edit_ctx_open[0] = True
                def on_menu_close():
                    _edit_ctx_open[0] = False
                    if edit_entry:
                        edit_entry.focus_force()
                edit_ctx.tk_popup(event.x_root, event.y_root)
                # tk_popup is blocking on some platforms; schedule cleanup
                edit_entry.after(50, on_menu_close)
                return 'break'
            edit_entry.bind('<Button-3>', show_edit_ctx)

            def on_focus_out(e):
                if not edit_entry:
                    return
                # Wait for context menu interactions to complete
                def deferred_save():
                    if not edit_entry:
                        return
                    if _edit_ctx_open[0]:
                        # Menu still active, check again later
                        edit_entry.after(200, deferred_save)
                        return
                    try:
                        if edit_entry.focus_get() == edit_entry:
                            return  # focus came back, don't save
                    except Exception:
                        pass
                    save_edit()
                edit_entry.after(300, deferred_save)
            edit_entry.bind('<FocusOut>', on_focus_out)

        tree.bind('<Double-1>', on_double_click)

        # ── Video preview on right-click ──
        def preview_at_cue(event=None):
            """Play a short clip starting at the selected cue's timestamp."""
            selected = tree.selection()
            if not selected:
                return
            idx = int(selected[0])
            start_ts = cues[idx]['start'].replace(',', '.')
            # Play 5 seconds starting from the cue timestamp
            cmd = ['ffplay', '-ss', start_ts, '-t', '5',
                   '-autoexit', '-window_title', f'Preview — cue #{idx + 1}',
                   '-loglevel', 'quiet', filepath]
            try:
                subprocess.Popen(cmd)
            except FileNotFoundError:
                self.add_log("ffplay not found — cannot preview video", 'WARNING')
            except Exception as e:
                self.add_log(f"Preview error: {e}", 'ERROR')

        # Right-click context menu
        ctx_menu = tk.Menu(editor, tearoff=0)
        ctx_menu.add_command(label="▶ Preview at this cue", command=preview_at_cue)
        ctx_menu.add_separator()
        def insert_cue(position):
            """Insert a blank cue above or below the selected cue."""
            nonlocal cues
            selected = tree.selection()
            if not selected:
                return
            idx = int(selected[0])
            ref = cues[idx]

            if position == 'above':
                # Place the new cue just before the selected one
                ref_start_ms = srt_ts_to_ms(ref['start'])
                new_end_ms = max(ref_start_ms - 1, 0)
                new_start_ms = max(new_end_ms - 2000, 0)
                insert_idx = idx
            else:
                # Place the new cue just after the selected one
                ref_end_ms = srt_ts_to_ms(ref['end'])
                new_start_ms = ref_end_ms + 1
                new_end_ms = new_start_ms + 2000
                insert_idx = idx + 1

            push_undo()
            new_cue = {
                'index': 0,
                'start': ms_to_srt_ts(new_start_ms),
                'end': ms_to_srt_ts(new_end_ms),
                'text': ' ',
            }
            cues.insert(insert_idx, new_cue)
            refresh_tree(cues)
            # Select the new cue and scroll to it
            tree.see(str(insert_idx))
            tree.selection_set(str(insert_idx))

        ctx_menu.add_command(label="✂ Split cue", command=split_selected)
        ctx_menu.add_command(label="⊕ Join selected cues", command=join_selected)
        ctx_menu.add_separator()
        ctx_menu.add_command(label="⤒ Insert line above", command=lambda: insert_cue('above'))
        ctx_menu.add_command(label="⤓ Insert line below", command=lambda: insert_cue('below'))
        ctx_menu.add_separator()
        ctx_menu.add_command(label="🗑 Delete selected", command=delete_selected)

        def show_context_menu(event):
            # Select the row under cursor if not already selected
            item = tree.identify_row(event.y)
            if item and item not in tree.selection():
                tree.selection_set(item)
            ctx_menu.tk_popup(event.x_root, event.y_root)

        tree.bind('<Button-3>', show_context_menu)

        # ══════════════════════════════════════════════════════════════════════
        # Status bar + action buttons
        # ══════════════════════════════════════════════════════════════════════
        status_frame = ttk.Frame(editor, padding=(10, 6, 10, 6))
        status_frame.pack(fill='x')

        stats_label = ttk.Label(status_frame,
                                text=f"{len(cues)} entries │ 0 modified │ 0 removed")
        stats_label.pack(side='left')

        def do_save():
            """Save edited subtitles (for encoding pipeline or external file)."""
            if not cues:
                messagebox.showwarning("Empty Subtitles",
                                       "All subtitle entries have been removed.\n"
                                       "Nothing to save.", parent=editor)
                return
            removed = len(original_cues) - len(cues)
            if is_external:
                # Write directly back to the external subtitle file
                with open(external_sub_path, 'w', encoding='utf-8') as f:
                    f.write(write_srt(cues))
                self.add_log(f"Subtitle edits saved: "
                             f"{len(cues)} entries ({removed} removed) → "
                             f"{os.path.basename(external_sub_path)}", 'SUCCESS')
            else:
                # Internal stream — write to temp file for encoding pipeline
                tmp_dir = tempfile.mkdtemp(prefix='docflix_sub_')
                out_path = os.path.join(tmp_dir, f"edited_stream{stream_index}.srt")
                with open(out_path, 'w', encoding='utf-8') as f:
                    f.write(write_srt(cues))
                if 'edited_subs' not in file_info:
                    file_info['edited_subs'] = {}
                file_info['edited_subs'][stream_index] = out_path
                self.add_log(f"Subtitle edits saved for stream #{stream_index}: "
                             f"{len(cues)} entries ({removed} removed) → {out_path}", 'SUCCESS')
            editor.destroy()

        def do_save_to_video():
            """Re-mux the edited subtitle directly back into the video file."""
            if not cues:
                messagebox.showwarning("Empty Subtitles",
                                       "All subtitle entries have been removed.\n"
                                       "Nothing to save.", parent=editor)
                return
            if is_external or not filepath:
                messagebox.showinfo("Not Available",
                    "Save to Video is only available for internal subtitle streams.",
                    parent=editor)
                return

            removed = len(original_cues) - len(cues)
            streams = get_subtitle_streams(filepath)

            # Write edited SRT to temp file
            tmp_srt = tempfile.NamedTemporaryFile(suffix='.srt', delete=False,
                                                   mode='w', encoding='utf-8')
            tmp_srt.close()
            with open(tmp_srt.name, 'w', encoding='utf-8') as f:
                f.write(write_srt(cues))

            # Build ffmpeg command: map every stream in order, replacing
            # the target subtitle with the edited version to preserve track order
            tmp_out = str(Path(filepath).with_suffix('.tmp' + Path(filepath).suffix))
            cmd = ['ffmpeg', '-y', '-i', filepath, '-i', tmp_srt.name]

            all_streams = get_all_streams(filepath)
            out_sub_count = 0
            replaced_out_sub_idx = None
            orig_stream = next(s for s in streams if s['index'] == stream_index)
            for s in all_streams:
                if s['index'] == stream_index:
                    # Replace this subtitle with the edited version
                    cmd.extend(['-map', '1:0'])
                    replaced_out_sub_idx = out_sub_count
                    out_sub_count += 1
                else:
                    cmd.extend(['-map', f"0:{s['index']}"])
                    if s['codec_type'] == 'subtitle':
                        out_sub_count += 1

            # Copy all codecs (no re-encoding)
            cmd.extend(['-c', 'copy'])

            # Preserve metadata on the replaced subtitle stream
            if replaced_out_sub_idx is not None:
                if orig_stream.get('language') and orig_stream['language'] != 'und':
                    cmd.extend([f'-metadata:s:s:{replaced_out_sub_idx}',
                                f"language={orig_stream['language']}"])
                if orig_stream.get('title'):
                    cmd.extend([f'-metadata:s:s:{replaced_out_sub_idx}',
                                f"title={orig_stream['title']}"])
                # Preserve disposition flags
                disp_parts = []
                if orig_stream.get('default'):
                    disp_parts.append('default')
                if orig_stream.get('forced'):
                    disp_parts.append('forced')
                if orig_stream.get('sdh'):
                    disp_parts.append('hearing_impaired')
                if disp_parts:
                    cmd.extend([f'-disposition:s:{replaced_out_sub_idx}',
                                '+'.join(disp_parts)])

                # For MP4 containers, subtitle codec must be mov_text
                if Path(filepath).suffix.lower() in ('.mp4', '.m4v'):
                    cmd.extend([f'-c:s:{replaced_out_sub_idx}', 'mov_text'])

            cmd.append(tmp_out)

            self.add_log(f"Re-muxing subtitle into {os.path.basename(filepath)}...", 'INFO')
            self.add_log(f"ffmpeg command: {' '.join(cmd)}", 'INFO')
            editor.update_idletasks()

            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
                if result.returncode != 0:
                    self.add_log(f"Re-mux stderr: {result.stderr[-500:]}", 'ERROR')
                    messagebox.showerror("Re-mux Failed",
                        f"Failed to save subtitle back to video:\n\n"
                        f"{result.stderr[-400:]}",
                        parent=editor)
                    if os.path.exists(tmp_out):
                        os.unlink(tmp_out)
                    os.unlink(tmp_srt.name)
                    return

                # Atomic replace: swap temp output over original
                os.replace(tmp_out, filepath)

                # Cleanup temp SRT
                try:
                    os.unlink(tmp_srt.name)
                except OSError:
                    pass

                self.add_log(f"Subtitle re-muxed into video: {len(cues)} entries "
                             f"({removed} removed) → {os.path.basename(filepath)}", 'SUCCESS')
                messagebox.showinfo("Saved",
                    f"Subtitle stream #{stream_index} saved back to:\n"
                    f"{os.path.basename(filepath)}",
                    parent=editor)
                editor.destroy()

            except Exception as e:
                messagebox.showerror("Error", f"Re-mux error:\n{e}", parent=editor)
                if os.path.exists(tmp_out):
                    os.unlink(tmp_out)
                try:
                    os.unlink(tmp_srt.name)
                except OSError:
                    pass

        def do_export():
            """Export the edited subtitle as a standalone .srt file."""
            if not cues:
                messagebox.showwarning("Empty", "No subtitle entries to export.",
                                       parent=editor)
                return
            out_dir = self.output_dir or Path(filepath).parent
            default_name = f"{Path(filepath).stem}.edited.srt"
            out_path = filedialog.asksaveasfilename(
                parent=editor,
                initialdir=str(out_dir),
                initialfile=default_name,
                defaultextension='.srt',
                filetypes=[('SubRip', '*.srt'), ('All files', '*.*')]
            )
            if not out_path:
                return
            with open(out_path, 'w', encoding='utf-8') as f:
                f.write(write_srt(cues))
            self.add_log(f"Exported edited subtitle → {out_path}", 'SUCCESS')

        ttk.Button(status_frame, text="💾 Save", command=do_save).pack(side='right', padx=(4, 0))
        if not is_external:
            ttk.Button(status_frame, text="💾 Save to Video",
                       command=do_save_to_video).pack(side='right', padx=(4, 0))
        ttk.Button(status_frame, text="Cancel", command=editor.destroy).pack(side='right')
        ttk.Button(status_frame, text="📤 Export SRT", command=do_export).pack(side='right', padx=4)
        ttk.Button(status_frame, text="▶ Preview",
                   command=preview_at_cue).pack(side='right', padx=4)

        # Populate tree
        refresh_tree(cues)

        # Delete key shortcut
        editor.bind('<Delete>', lambda e: None if isinstance(e.widget, tk.Text) else delete_selected())

        editor.wait_window()

    def on_drop(self, event):
        """Handle files/folders dropped onto the file list."""
        raw = event.data
        # tkinterdnd2 wraps paths with spaces in curly braces: {/path/to/my file.mkv}
        # On Linux, file managers may also send file:// URIs (one per line)
        # Parse them properly
        paths = []
        if 'file://' in raw:
            # URI list format: one file:// URI per line, percent-encoded
            from urllib.parse import unquote, urlparse
            for line in raw.splitlines():
                line = line.strip()
                if line.startswith('file://'):
                    decoded = unquote(urlparse(line).path)
                    if decoded:
                        paths.append(decoded)
        else:
            i = 0
            while i < len(raw):
                if raw[i] == '{':
                    end = raw.find('}', i)
                    paths.append(raw[i+1:end])
                    i = end + 2
                else:
                    end = raw.find(' ', i)
                    if end == -1:
                        paths.append(raw[i:])
                        break
                    else:
                        paths.append(raw[i:end])
                        i = end + 1

        # Separate dropped paths into video files and subtitle files
        video_paths = []
        sub_paths = []
        for path_str in paths:
            path = Path(path_str.strip())
            if path.is_dir():
                for filepath in sorted(path.rglob('*')):
                    if any(part.startswith('.') for part in filepath.relative_to(path).parts):
                        continue
                    if filepath.is_file():
                        if filepath.suffix.lower() in VIDEO_EXTENSIONS:
                            video_paths.append(filepath)
                        elif filepath.suffix.lower() in SUBTITLE_EXTENSIONS:
                            sub_paths.append(filepath)
            elif path.is_file():
                if path.suffix.lower() in VIDEO_EXTENSIONS:
                    video_paths.append(path)
                elif path.suffix.lower() in SUBTITLE_EXTENSIONS:
                    sub_paths.append(path)

        # Add video files first
        added = 0
        for filepath in video_paths:
            added += self._add_file_to_list(filepath)

        # Associate subtitle files with video files
        attached = 0
        unmatched = []
        for sub_path in sub_paths:
            if self._associate_external_sub(sub_path):
                attached += 1
            else:
                unmatched.append(sub_path)

        # Unmatched subs: try to attach to currently selected file
        if unmatched:
            item, index = self._get_selected_file_index()
            if index is not None and len(unmatched) > 0:
                for sub_path in unmatched:
                    self._attach_external_sub(self.files[index], sub_path)
                    attached += 1
                self._refresh_tree_row(item, self.files[index])
                unmatched = []

        if added:
            self.add_log(f"Added {added} file(s) via drag & drop.", 'INFO')
        if attached:
            self.add_log(f"Attached {attached} external subtitle(s).", 'INFO')
        if unmatched:
            names = ', '.join(p.name for p in unmatched)
            self.add_log(f"Could not match subtitle(s): {names} — select a video file first.", 'WARNING')
        if not added and not attached and not unmatched:
            self.add_log("No supported video or subtitle files found in dropped items.", 'WARNING')

    def _add_file_to_list(self, filepath):
        """Add a single file to the list if not already present. Returns 1 if added, 0 if skipped."""
        filepath = Path(filepath)
        f = filepath.name
        # Skip already-converted files
        if re.search(r'-(\d+(\.\d+)?M|CRF\d+)-(NVENC_|)(H265|H264|AV1|VP9|MPEG4|ProRes)_|-video-copy|-audio-copy|-[A-Z0-9]+_\d+k', f):
            return 0
        # Skip duplicates already in the list
        existing_paths = {fi['path'] for fi in self.files}
        if str(filepath) in existing_paths:
            return 0
        size = format_size(filepath.stat().st_size)
        est = estimate_output_size(str(filepath), self._current_settings())
        dur_secs = get_video_duration(str(filepath))
        dur_str = format_duration(dur_secs)
        # Detect ATSC A53 closed captions (common in MPEG-2 .ts files)
        has_cc = False
        if filepath.suffix.lower() in ('.ts', '.m2ts', '.mts'):
            has_cc = detect_closed_captions(str(filepath))

        file_info = {
            'name': f,
            'path': str(filepath),
            'size': size,
            'duration_str': dur_str,
            'duration_secs': dur_secs,
            'est_size': est,
            'status': 'Pending',
            'external_subs': [],
            'has_closed_captions': has_cc,
            'extract_cc': has_cc,  # auto-extract by default if CC detected
        }
        self.files.append(file_info)
        prefix = ''
        if has_cc:
            prefix = 'CC '
        self.file_tree.insert('', 'end', values=(prefix + f, size, dur_str, est, 'Pending'))
        return 1

    def _associate_external_sub(self, sub_path):
        """Try to auto-associate an external subtitle file with a video in the queue.

        Matches by filename stem — e.g. ``movie.srt`` matches ``movie.mkv``,
        ``movie.en.srt`` matches ``movie.mkv``, and
        ``movie.en.forced.srt`` matches ``movie.mkv``
        (progressively strips trailing dot-separated suffixes).
        Returns True if a match was found and attached.
        """
        sub_path = Path(sub_path)
        sub_stem = sub_path.stem.lower()

        # Build a list of candidate stems by progressively stripping
        # trailing dot-separated tokens (e.g. lang codes, "forced", "sdh")
        candidates = [sub_stem]
        stem = sub_stem
        for _ in range(3):  # strip up to 3 trailing tokens
            if '.' not in stem:
                break
            stem = stem.rsplit('.', 1)[0]
            candidates.append(stem)

        for i, file_info in enumerate(self.files):
            video_stem = Path(file_info['path']).stem.lower()
            if video_stem in candidates:
                self._attach_external_sub(file_info, sub_path)
                items = self.file_tree.get_children()
                if i < len(items):
                    self._refresh_tree_row(items[i], file_info)
                return True
        return False

    def _attach_external_sub(self, file_info, sub_path):
        """Attach an external subtitle file to a file_info dict."""
        sub_path = Path(sub_path)
        ext = sub_path.suffix.lower()

        # Try to detect language from filename suffixes
        # Handles: movie.en.srt, movie.eng.srt, movie.en.forced.srt, etc.
        lang = 'und'
        _lang2to3 = {
            'en': 'eng', 'es': 'spa', 'fr': 'fra', 'de': 'deu',
            'it': 'ita', 'pt': 'por', 'ru': 'rus', 'ja': 'jpn',
            'ko': 'kor', 'zh': 'zho', 'ar': 'ara', 'hi': 'hin',
            'nl': 'nld', 'pl': 'pol', 'sv': 'swe', 'tr': 'tur',
            'vi': 'vie',
        }
        # Check trailing dot-separated tokens for language codes
        stem_tokens = sub_path.stem.lower().split('.')
        for token in reversed(stem_tokens[1:]):  # skip the first token (main filename)
            if token in _lang2to3:
                lang = _lang2to3[token]
                break
            elif any(token == lc for lc, _ in SUBTITLE_LANGUAGES):
                lang = token
                break

        # Auto-detect flags from filename tokens
        stem_tokens = [t.lower() for t in sub_path.stem.split('.')]
        is_forced = 'forced' in stem_tokens
        is_sdh = 'sdh' in stem_tokens or 'cc' in stem_tokens
        # "Plain" subtitle = not forced, not SDH/CC — candidate for default track
        is_plain = not is_forced and not is_sdh
        # Only set default if no other sub already has it for this file
        existing_has_default = any(s.get('default') for s in file_info.get('external_subs', []))

        sub_info = {
            'path': str(sub_path),
            'label': sub_path.name,
            'language': lang,
            'mode': 'embed',        # 'embed' or 'burn_in'
            'format': SUBTITLE_EXT_TO_CODEC.get(ext, 'srt'),
            'default': is_plain and not existing_has_default,
            'sdh': is_sdh,
            'forced': is_forced,
        }

        # Avoid duplicate attachments
        existing_paths = {s['path'] for s in file_info.get('external_subs', [])}
        if str(sub_path) not in existing_paths:
            file_info.setdefault('external_subs', []).append(sub_info)

    def setup_log_panel(self):
        """Create the detached log window (hidden until user clicks Log button)."""
        self.log_window = tk.Toplevel(self.root)
        self.log_window.title(f"{APP_NAME} — Log")
        self.log_window.geometry("900x400")
        self.log_window.protocol("WM_DELETE_WINDOW", self.hide_log_window)
        self.log_window.resizable(True, True)

        log_frame = ttk.Frame(self.log_window, padding=8)
        log_frame.pack(fill='both', expand=True)
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(1, weight=1)

        # Toolbar
        log_toolbar = ttk.Frame(log_frame)
        log_toolbar.grid(row=0, column=0, sticky='ew', pady=(0, 4))
        ttk.Button(log_toolbar, text="🗑️ Clear Log",
                   command=self.clear_log).pack(side='right')

        self.log_text = scrolledtext.ScrolledText(log_frame, height=20, wrap='word')
        self.log_text.grid(row=1, column=0, sticky="nsew")

        # Configure tags for different log levels
        self.log_text.tag_configure('INFO', foreground='blue')
        self.log_text.tag_configure('SUCCESS', foreground='green')
        self.log_text.tag_configure('WARNING', foreground='orange')
        self.log_text.tag_configure('ERROR', foreground='red')

        # Hide it initially
        self.log_window.withdraw()

    def toggle_log_window(self):
        """Show or hide the log window."""
        if self.log_window.winfo_viewable():
            self.hide_log_window()
        else:
            self.show_log_window()

    def show_log_window(self):
        """Show the log window, positioned below the main window."""
        self.log_window.deiconify()
        self.log_window.lift()
        # Position it just below the main window
        x = self.root.winfo_x()
        y = self.root.winfo_y() + self.root.winfo_height() + 5
        self.log_window.geometry(f"+{x}+{y}")
        self.log_btn.configure(text="📋 Log ✓")

    def hide_log_window(self):
        """Hide the log window."""
        self.log_window.withdraw()
        self.log_btn.configure(text="📋 Log")

    def toggle_settings_panel(self):
        """Show or hide the settings panel."""
        if self.settings_frame.winfo_viewable():
            self.settings_frame.grid_remove()
        else:
            self.settings_frame.grid()

    # ── Preferences ──────────────────────────────────────────────────────────

    def _prefs_path(self):
        return Path.home() / '.config' / 'docflix_video_converter' / 'prefs.json'

    def show_default_settings(self):
        """Show the Default Settings dialog."""
        dlg = tk.Toplevel(self.root)
        dlg.title("Default Settings")
        dlg.geometry("640x320")
        dlg.grab_set()
        dlg.resizable(True, True)
        dlg.minsize(640, 320)
        self._center_on_main(dlg)

        # Load existing prefs for pre-fill
        try:
            prefs = json.loads(self._prefs_path().read_text()) if self._prefs_path().exists() else {}
        except Exception:
            prefs = {}

        f = ttk.Frame(dlg, padding=16)
        f.pack(fill='both', expand=True)
        f.columnconfigure(1, weight=1)

        pad = {'padx': 8, 'pady': 6}

        # ── Default Video Folder ──
        ttk.Label(f, text="Default Video Folder:").grid(row=0, column=0, sticky='w', **pad)
        v_video_folder = tk.StringVar(value=prefs.get('default_video_folder', str(self.working_dir)))
        vf_frame = ttk.Frame(f)
        vf_frame.grid(row=0, column=1, sticky='ew', **pad)
        vf_frame.columnconfigure(0, weight=1)
        ttk.Entry(vf_frame, textvariable=v_video_folder).grid(row=0, column=0, sticky='ew')
        ttk.Button(vf_frame, text="Browse…",
                   command=lambda: v_video_folder.set(
                       self._ask_directory(initialdir=v_video_folder.get(),
                                           title="Select Default Video Folder")
                       or v_video_folder.get()
                   )).grid(row=0, column=1, padx=(4, 0))

        # ── Default Save To Folder ──
        ttk.Label(f, text="Default Save To Folder:").grid(row=1, column=0, sticky='w', **pad)
        v_output_folder = tk.StringVar(value=prefs.get('default_output_folder', ''))
        of_frame = ttk.Frame(f)
        of_frame.grid(row=1, column=1, sticky='ew', **pad)
        of_frame.columnconfigure(0, weight=1)
        ttk.Entry(of_frame, textvariable=v_output_folder).grid(row=0, column=0, sticky='ew')
        ttk.Button(of_frame, text="Browse…",
                   command=lambda: v_output_folder.set(
                       self._ask_directory(initialdir=v_output_folder.get() or str(Path.home()),
                                           title="Select Default Save To Folder")
                       or v_output_folder.get()
                   )).grid(row=0, column=1, padx=(4, 0))
        ttk.Label(f, text="(leave blank to save alongside source files)",
                  foreground='gray', font=('Helvetica', 8)).grid(
                  row=2, column=1, sticky='w', padx=8)

        # ── Default Video Codec ──
        ttk.Label(f, text="Default Video Codec:").grid(row=3, column=0, sticky='w', **pad)
        v_codec = tk.StringVar(value=prefs.get('video_codec', self.video_codec.get()))
        ttk.Combobox(f, textvariable=v_codec,
                     values=list(VIDEO_CODEC_MAP.keys()),
                     width=24, state='readonly').grid(row=3, column=1, sticky='w', **pad)

        # ── Default Audio Codec ──
        ttk.Label(f, text="Default Audio Codec:").grid(row=4, column=0, sticky='w', **pad)
        v_audio = tk.StringVar(value=prefs.get('audio_codec', self.audio_codec.get()))
        ttk.Combobox(f, textvariable=v_audio,
                     values=list(self.audio_codec_map.keys()),
                     width=24, state='readonly').grid(row=4, column=1, sticky='w', **pad)

        # ── Default Video Player ──
        ttk.Label(f, text="Default Video Player:").grid(row=5, column=0, sticky='w', **pad)
        # Detect which players are installed
        available = ['System Default', 'auto']
        for p in ('vlc', 'mpv', 'totem', 'ffplay', 'smplayer', 'celluloid', 'mplayer'):
            if shutil.which(p):
                available.append(p)
        available.append('Custom...')

        current_player = prefs.get('default_player', self.default_player.get())
        # If saved value isn't in the detected list, it's a custom path
        if current_player not in available and current_player != 'auto':
            v_player = tk.StringVar(value='Custom...')
            v_custom = tk.StringVar(value=current_player)
        else:
            v_player = tk.StringVar(value=current_player)
            v_custom = tk.StringVar(value=prefs.get('custom_player', ''))

        player_frame = ttk.Frame(f)
        player_frame.grid(row=5, column=1, sticky='ew', **pad)
        player_frame.columnconfigure(1, weight=1)

        player_combo = ttk.Combobox(player_frame, textvariable=v_player,
                                    values=available, width=14, state='readonly')
        player_combo.grid(row=0, column=0, sticky='w')
        ttk.Label(player_frame, text="(System Default = use xdg-open  |  auto = try installed players in order)",
                  foreground='gray', font=('Helvetica', 8)).grid(
                  row=0, column=1, sticky='w', padx=(8, 0))

        # Custom path row — shown only when "Custom..." is selected
        custom_frame = ttk.Frame(f)
        custom_frame.grid(row=6, column=0, columnspan=2, sticky='ew', padx=8)
        custom_frame.columnconfigure(1, weight=1)
        ttk.Label(custom_frame, text="Custom path:").grid(row=0, column=0, sticky='w', padx=(0, 8))
        custom_entry = ttk.Entry(custom_frame, textvariable=v_custom)
        custom_entry.grid(row=0, column=1, sticky='ew')
        ttk.Button(custom_frame, text="Browse…",
                   command=lambda: v_custom.set(
                       filedialog.askopenfilename(title="Select Video Player Executable",
                                                  initialdir='/usr/bin')
                       or v_custom.get()
                   )).grid(row=0, column=2, padx=(4, 0))

        def on_player_changed(*args):
            if v_player.get() == 'Custom...':
                custom_frame.grid()
                dlg.geometry("640x420")
            else:
                custom_frame.grid_remove()
                dlg.geometry("640x380")

        v_player.trace_add('write', on_player_changed)
        # Set initial state
        if v_player.get() == 'Custom...':
            custom_frame.grid()
        else:
            custom_frame.grid_remove()

        # ── Notification Sound ──
        ttk.Label(f, text="Notify When Done:").grid(row=7, column=0, sticky='w', **pad)
        notify_frame = ttk.Frame(f)
        notify_frame.grid(row=7, column=1, sticky='w', **pad)

        v_notify = tk.BooleanVar(value=self.notify_sound.get())
        ttk.Checkbutton(notify_frame, text="Play sound",
                       variable=v_notify).pack(side='left', padx=(0, 8))

        SOUND_NAMES = [
            'complete', 'alarm-clock-elapsed', 'bell', 'message',
            'dialog-information', 'phone-incoming-call', 'service-login',
            'window-attention', 'audio-test-signal'
        ]
        v_sound_file = tk.StringVar(value=self.notify_sound_file.get())
        ttk.Combobox(notify_frame, textvariable=v_sound_file,
                     values=SOUND_NAMES, width=18, state='readonly').pack(side='left', padx=2)
        ttk.Button(notify_frame, text="🔊", width=2,
                   command=lambda: (
                       self.notify_sound_file.set(v_sound_file.get()),
                       self.preview_sound()
                   )).pack(side='left', padx=2)

        # Adjust dialog height for all content
        dlg.geometry("640x380")

        # ── Buttons ──
        btn_frame = ttk.Frame(dlg, padding=(16, 0, 16, 12))
        btn_frame.pack(fill='x')

        def on_save():
            # Apply to UI
            vf = v_video_folder.get().strip()
            if vf and Path(vf).is_dir():
                self.working_dir = Path(vf)

            of = v_output_folder.get().strip()
            if of and Path(of).is_dir():
                self.output_dir = Path(of)
                self.output_dir_label.configure(text=of, foreground='black')
            elif not of:
                self.output_dir = None
                self.output_dir_label.configure(text="Same as source file", foreground='gray')

            self.video_codec.set(v_codec.get())
            self.on_video_codec_change()

            self.audio_codec.set(v_audio.get())

            # Player — if Custom..., use the custom path entry value
            if v_player.get() == 'Custom...':
                custom = v_custom.get().strip()
                if custom:
                    self.default_player.set(custom)
                else:
                    messagebox.showwarning("Custom Player",
                                           "Please enter a path for the custom player.")
                    return
            else:
                self.default_player.set(v_player.get())

            # Notification
            self.notify_sound.set(v_notify.get())
            self.notify_sound_file.set(v_sound_file.get())

            # Persist to prefs file
            self.save_preferences()
            dlg.destroy()

        ttk.Button(btn_frame, text="Save", command=on_save).pack(side='right', padx=(4, 0))
        ttk.Button(btn_frame, text="Cancel", command=dlg.destroy).pack(side='right')

    def save_preferences(self):
        """Save current settings to a JSON preferences file."""
        prefs = {
            'encoder':              self.encoder_mode.get(),
            'video_codec':          self.video_codec.get(),
            'container':            self.container_format.get(),
            'transcode_mode':       self.transcode_mode.get(),
            'quality_mode':         self.quality_mode.get(),
            'crf':                  self.crf.get(),
            'cpu_preset':           self.cpu_preset.get(),
            'gpu_preset':           self.gpu_preset.get(),
            'audio_codec':          self.audio_codec.get(),
            'audio_bitrate':        self.audio_bitrate.get(),
            'skip_existing':        self.skip_existing.get(),
            'delete_originals':     self.delete_originals.get(),
            'hw_decode':            self.hw_decode.get(),
            'strip_internal_subs':  self.strip_internal_subs.get(),
            'two_pass':             self.two_pass.get(),
            'verify_output':        self.verify_output.get(),
            'notify_sound':         self.notify_sound.get(),
            'notify_sound_file':    self.notify_sound_file.get(),
            'default_player':        self.default_player.get(),
            'default_video_folder':  str(self.working_dir),
            'default_output_folder': str(self.output_dir) if self.output_dir else '',
            'recent_folders':        self.recent_folders,
            'strip_chapters':        self.strip_chapters.get(),
            'strip_metadata_tags':   self.strip_metadata_tags.get(),
            'set_track_metadata':    self.set_track_metadata.get(),
            'meta_video_lang':       self.meta_video_lang.get(),
            'meta_audio_lang':       self.meta_audio_lang.get(),
            'meta_sub_lang':         self.meta_sub_lang.get(),
            'edition_tag':           self.edition_tag.get(),
            'edition_in_filename':   self.edition_in_filename.get(),
            'add_chapters':          self.add_chapters.get(),
            'chapter_interval':      self.chapter_interval.get(),
            'custom_ad_patterns':    self.custom_ad_patterns,
            'custom_cap_words':      self.custom_cap_words,
            'custom_replacements':   self.custom_replacements,
            'custom_spell_words':    self.custom_spell_words,
            'tvdb_api_key':          getattr(self, '_tvdb_api_key', ''),
            'tmdb_api_key':          getattr(self, '_tmdb_api_key', ''),
            'tv_rename_provider':    getattr(self, '_tv_rename_provider', 'TVDB'),
            'tv_rename_template':    getattr(self, '_tv_rename_template', '{show} S{season}E{episode} {title}'),
        }
        try:
            self._prefs_path().parent.mkdir(parents=True, exist_ok=True)
            self._prefs_path().write_text(json.dumps(prefs, indent=2))
            self.add_log(f"Preferences saved to {self._prefs_path()}", 'SUCCESS')
        except Exception as e:
            self.add_log(f"Failed to save preferences: {e}", 'ERROR')
            messagebox.showerror("Error", f"Failed to save preferences:\n{e}")

    def load_preferences(self):
        """Load preferences from JSON file if it exists."""
        if not self._prefs_path().exists():
            return
        try:
            prefs = json.loads(self._prefs_path().read_text())
            saved_encoder = prefs.get('encoder', self.encoder_mode.get())
            # Backward compat: map old 'gpu' value to first available GPU backend
            if saved_encoder == 'gpu':
                saved_encoder = self._default_gpu
            # Validate that the saved backend is actually available
            if saved_encoder != 'cpu' and saved_encoder not in self.gpu_backends:
                saved_encoder = 'cpu'
            self.encoder_mode.set(saved_encoder)
            self.video_codec.set(prefs.get('video_codec',       self.video_codec.get()))
            self.container_format.set(prefs.get('container',    self.container_format.get()))
            # Always start in video-only mode regardless of saved preference
            self.transcode_mode.set('video')
            self.quality_mode.set(prefs.get('quality_mode',     self.quality_mode.get()))
            # Bitrate intentionally not saved/loaded — always starts at default (2.0M)
            # to avoid hidden mismatches between saved value and UI slider position
            self.crf.set(prefs.get('crf',                       self.crf.get()))
            self.cpu_preset.set(prefs.get('cpu_preset',         self.cpu_preset.get()))
            self.gpu_preset.set(prefs.get('gpu_preset',         self.gpu_preset.get()))
            self.audio_codec.set(prefs.get('audio_codec',       self.audio_codec.get()))
            self.audio_bitrate.set(prefs.get('audio_bitrate',   self.audio_bitrate.get()))
            self.skip_existing.set(prefs.get('skip_existing',   self.skip_existing.get()))
            self.delete_originals.set(prefs.get('delete_originals', self.delete_originals.get()))
            self.hw_decode.set(prefs.get('hw_decode',           self.hw_decode.get()))
            self.strip_internal_subs.set(prefs.get('strip_internal_subs', self.strip_internal_subs.get()))
            self.two_pass.set(prefs.get('two_pass',             self.two_pass.get()))
            self.strip_chapters.set(prefs.get('strip_chapters', self.strip_chapters.get()))
            self.strip_metadata_tags.set(prefs.get('strip_metadata_tags', self.strip_metadata_tags.get()))
            self.set_track_metadata.set(prefs.get('set_track_metadata', self.set_track_metadata.get()))
            self.meta_video_lang.set(prefs.get('meta_video_lang', self.meta_video_lang.get()))
            self.meta_audio_lang.set(prefs.get('meta_audio_lang', self.meta_audio_lang.get()))
            self.meta_sub_lang.set(prefs.get('meta_sub_lang', self.meta_sub_lang.get()))
            self.edition_tag.set(prefs.get('edition_tag', ''))
            self.edition_in_filename.set(prefs.get('edition_in_filename', False))
            self.add_chapters.set(prefs.get('add_chapters', False))
            self.chapter_interval.set(prefs.get('chapter_interval', 5))
            self.verify_output.set(prefs.get('verify_output',   self.verify_output.get()))
            self.notify_sound.set(prefs.get('notify_sound',     self.notify_sound.get()))
            self.notify_sound_file.set(prefs.get('notify_sound_file', self.notify_sound_file.get()))
            # Default folders
            self.recent_folders = prefs.get('recent_folders', [])
            self.custom_ad_patterns = prefs.get('custom_ad_patterns', [])
            self.custom_cap_words = prefs.get('custom_cap_words', [])
            self.custom_spell_words = prefs.get('custom_spell_words', [])
            self.custom_replacements = prefs.get('custom_replacements', [])
            self._tvdb_api_key = prefs.get('tvdb_api_key', '')
            self._tmdb_api_key = prefs.get('tmdb_api_key', '')
            self._tv_rename_provider = prefs.get('tv_rename_provider', 'TVDB')
            self._tv_rename_template = prefs.get('tv_rename_template',
                                                  '{show} S{season}E{episode} {title}')
            # Media Processor
            self._media_proc_prefs = prefs.get('media_processor', {})
            self._rebuild_recent_menu()
            self.default_player.set(prefs.get('default_player', 'auto'))
            dvf = prefs.get('default_video_folder', '')
            if dvf and Path(dvf).is_dir():
                self.working_dir = Path(dvf)
            dof = prefs.get('default_output_folder', '')
            if dof and Path(dof).is_dir():
                self.output_dir = Path(dof)
                self.output_dir_label.configure(text=dof, foreground='black')
            self.add_log("Preferences loaded.", 'INFO')
        except Exception as e:
            self.add_log(f"Failed to load preferences: {e}", 'WARNING')

    def reset_preferences(self):
        """Reset all settings to defaults."""
        if not messagebox.askyesno("Reset to Defaults",
                                   "Reset all settings to their defaults?"):
            return
        self.encoder_mode.set(self._default_gpu if self.has_gpu else 'cpu')
        self.video_codec.set('H.265 / HEVC')
        self.container_format.set('.mkv')
        self.transcode_mode.set('video')
        self.quality_mode.set('bitrate')
        self.bitrate.set('2M')
        self.crf.set('23')
        self.cpu_preset.set('ultrafast')
        self.gpu_preset.set('p4')
        self.audio_codec.set('aac')
        self.audio_bitrate.set('128k')
        self.skip_existing.set(True)
        self.delete_originals.set(False)
        self.hw_decode.set(self.has_gpu)
        self.two_pass.set(False)
        self.verify_output.set(True)
        self.notify_sound.set(True)
        self.notify_sound_file.set('complete')
        self.strip_chapters.set(False)
        self.strip_metadata_tags.set(False)
        self.set_track_metadata.set(False)
        self.meta_video_lang.set('und')
        self.meta_audio_lang.set('eng')
        self.meta_sub_lang.set('eng')
        self.edition_tag.set('')
        self.edition_in_filename.set(False)
        self.add_chapters.set(False)
        self.chapter_interval.set(5)
        self._on_metadata_toggle()
        # Refresh UI state
        self.on_encoder_change(silent=True)
        self.on_video_codec_change()
        self.on_transcode_mode_change()
        self.on_quality_mode_change()
        self.add_log("Settings reset to defaults.", 'INFO')

    # ── Help ─────────────────────────────────────────────────────────────────

    def show_keyboard_shortcuts(self):
        """Show keyboard shortcuts dialog."""
        dlg = tk.Toplevel(self.root)
        dlg.title("Keyboard Shortcuts")
        dlg.transient(self.root)
        dlg.grab_set()
        dlg.resizable(False, False)

        sections = [
            ("File", [
                ("Ctrl+O",         "Open File(s)"),
                ("Ctrl+Shift+O",   "Open Folder"),
                ("Ctrl+Q",         "Exit"),
            ]),
            ("Settings", [
            ]),
            ("View", [
                ("Ctrl+L",         "Show/Hide Log"),
                ("Ctrl+Shift+S",   "Show/Hide Settings Panel"),
                ("F1",             "Keyboard Shortcuts"),
            ]),
            ("Tools", [
                ("Ctrl+P",         "Play Source File"),
                ("Ctrl+Shift+P",   "Play Output File"),
                ("Ctrl+I",         "Media Details"),
                ("Ctrl+T",         "Test Encode (30s)"),
                ("Ctrl+Shift+F",   "Open Output Folder"),
                ("Ctrl+M",         "Media Processor"),
            ]),
            ("File List", [
                ("Delete",         "Remove selected file from list"),
                ("Up / Down",      "Reorder files in queue"),
            ]),
        ]

        outer = ttk.Frame(dlg, padding=16)
        outer.pack(fill='both', expand=True)

        for section, items in sections:
            # Section header
            ttk.Label(outer, text=section,
                      font=('Helvetica', 10, 'bold')).pack(anchor='w', pady=(10, 2))
            # Grid frame for shortcut rows
            grid = ttk.Frame(outer)
            grid.pack(fill='x', padx=(12, 0))
            for row, (key, desc) in enumerate(items):
                ttk.Label(grid, text=key, font=('Courier', 10),
                          foreground='blue', width=16,
                          anchor='w').grid(row=row, column=0, sticky='w', pady=1)
                ttk.Label(grid, text=desc,
                          anchor='w').grid(row=row, column=1, sticky='w', padx=(8, 0), pady=1)

        ttk.Separator(dlg, orient='horizontal').pack(fill='x', pady=(12, 0))
        ttk.Button(dlg, text="Close", command=dlg.destroy).pack(pady=8)

        # Auto-size window to content then center on main window
        dlg.update_idletasks()
        dlg.geometry(f"{dlg.winfo_reqwidth() + 20}x{dlg.winfo_reqheight() + 10}")
        self._center_on_main(dlg)

    def show_user_manual(self):
        """Open the built-in user manual viewer."""
        try:
            from modules.manual_viewer import show_manual
            show_manual(self)
        except ImportError:
            import importlib.util
            _mv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     'modules', 'manual_viewer.py')
            if os.path.exists(_mv_path):
                spec = importlib.util.spec_from_file_location('manual_viewer', _mv_path)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                mod.show_manual(self)
            else:
                messagebox.showinfo("User Manual", "Manual viewer not found.")

    def show_about(self):
        """Show About dialog."""
        messagebox.showinfo(
            f"About {APP_NAME}",
            f"{APP_NAME}\nVersion {APP_VERSION}\n\n"
            f"A full-featured video transcoding application\n"
            f"powered by ffmpeg.\n\n"
            f"Supports H.265, H.264, AV1, VP9, MPEG-4, ProRes encoding\n"
            f"with NVIDIA NVENC GPU acceleration.\n\n"
            f"Built with Python + Tkinter."
        )

    def setup_status_bar(self, parent):
        """Setup status bar"""
        status_frame = ttk.Frame(parent)
        status_frame.grid(row=3, column=0, sticky="ew")

        self.status_label = ttk.Label(status_frame, text="Ready")
        self.status_label.pack(side='left')

        self.log_btn = ttk.Button(status_frame, text="📋 Log",
                                  command=self.toggle_log_window)
        self.log_btn.pack(side='right', padx=(0, 6))

        self.time_label = ttk.Label(status_frame, text="Elapsed: 0s")
        self.time_label.pack(side='right', padx=10)

        self.batch_eta_label = ttk.Label(status_frame, text="")
        self.batch_eta_label.pack(side='right', padx=10)

        self.eta_label = ttk.Label(status_frame, text="")
        self.eta_label.pack(side='right', padx=10)

        self.fps_label = ttk.Label(status_frame, text="")
        self.fps_label.pack(side='right', padx=10)
    
    def _get_sound_path(self, name):
        """Return the full path to a freedesktop sound file."""
        return f"/usr/share/sounds/freedesktop/stereo/{name}.oga"

    def play_notification_sound(self):
        """Play the selected notification sound in a background thread."""
        if not self.notify_sound.get():
            return
        sound_path = self._get_sound_path(self.notify_sound_file.get())
        def _play():
            try:
                subprocess.run(
                    ['ffplay', '-nodisp', '-autoexit', '-loglevel', 'quiet', sound_path],
                    timeout=10
                )
            except Exception:
                pass
        threading.Thread(target=_play, daemon=True).start()

    def preview_sound(self):
        """Preview the currently selected notification sound."""
        self.play_notification_sound()

    def clear_log(self):
        """Clear the log panel."""
        self.log_text.delete('1.0', 'end')

    def add_log(self, message, level='INFO'):
        """Add message to log panel"""
        def _add():
            self.log_text.insert('end', message + '\n', level)
            self.log_text.see('end')
        self.root.after(0, _add)
    
    def _calc_batch_eta(self, current_file_eta=None):
        """Calculate estimated time remaining for the entire batch.

        Uses the rolling average encoding speed (video-seconds per wall-second)
        from completed files, plus the current file's remaining ETA, to estimate
        how long the rest of the batch will take.

        Returns seconds remaining, or None if not enough data.
        """
        if len(self.files) <= 1:
            return None  # No batch ETA for single files

        # Calculate average speed from completed files
        if self._batch_speed_samples:
            total_vid = sum(d for d, _ in self._batch_speed_samples)
            total_wall = sum(w for _, w in self._batch_speed_samples)
            avg_speed = total_vid / total_wall if total_wall > 0 else None
        else:
            avg_speed = None

        # Sum durations of files not yet started (after current)
        remaining_duration = 0.0
        for j in range(self.current_file_index + 1, len(self.files)):
            fi = self.files[j]
            if fi.get('status') not in ('skipped',):
                remaining_duration += fi.get('duration_secs', 0) or 0

        # Estimate time for remaining files
        if avg_speed and avg_speed > 0:
            remaining_files_eta = remaining_duration / avg_speed
        elif self._file_start_time is not None:
            # No completed files yet — use current file's progress to estimate speed
            import time as _time
            wall_so_far = _time.monotonic() - self._file_start_time
            cur_dur = (self.files[self.current_file_index].get('duration_secs') or 0)
            if wall_so_far > 2 and cur_dur > 0:
                # Estimate speed from current file progress
                cur_speed = cur_dur / (wall_so_far + (current_file_eta or 0))
                remaining_files_eta = remaining_duration / cur_speed if cur_speed > 0 else None
            else:
                return None
        else:
            return None

        # Add current file's remaining time
        batch_remaining = (current_file_eta or 0) + remaining_files_eta
        return batch_remaining if batch_remaining > 0 else None

    def update_progress(self, percent, details='', fps=None, eta=None, pass_label=None):
        """Update progress bar, fps, elapsed timer, and ETA labels."""
        def _update():
            self.progress_var.set(percent)
            # FPS
            if fps is not None:
                self.fps_label.configure(text=f"⚡ {fps:.1f} fps")
            # Elapsed timer
            if self.start_time is not None:
                elapsed = (datetime.now() - self.start_time).total_seconds()
                self.time_label.configure(text=f"Elapsed: {format_time(elapsed)}")
            # Per-file ETA
            if eta is not None:
                pass_str = f" ({pass_label})" if pass_label else ""
                self.eta_label.configure(text=f"ETA{pass_str}: {format_time(eta)}")
            # Batch ETA (only show when multiple files)
            if len(self.files) > 1 and self.is_converting:
                batch_eta = self._calc_batch_eta(current_file_eta=eta)
                if batch_eta is not None:
                    self.batch_eta_label.configure(
                        text=f"Batch: {format_time(batch_eta)} left")
                else:
                    self.batch_eta_label.configure(text="")
        self.root.after(0, _update)
    
    def _current_settings(self):
        """Return a settings dict reflecting current UI state (for estimates)."""
        try:
            return {
                'transcode_mode': self.transcode_mode.get(),
                'encoder':        self.encoder_mode.get(),
                'codec_info':     self.get_codec_info(),
                'mode':           self.quality_mode.get(),
                'bitrate':        self.bitrate.get(),
                'crf':            int(self.crf.get()),
                'audio_codec':    self.get_audio_codec_name(),
                'audio_bitrate':  self.audio_bitrate.get(),
            }
        except Exception:
            return {'transcode_mode': 'video', 'encoder': self._default_gpu,
                    'codec_info': VIDEO_CODEC_MAP['H.265 / HEVC'],
                    'mode': 'bitrate', 'bitrate': '2M', 'crf': 23,
                    'audio_codec': 'aac', 'audio_bitrate': '128k'}

    def refresh_estimated_sizes(self):
        """Recalculate estimated output sizes for all files and update the tree."""
        settings = self._current_settings()
        for file_info in self.files:
            ov = file_info.get('overrides', {})
            eff = dict(settings)
            if ov:
                eff.update({
                    'transcode_mode': ov.get('transcode_mode', settings['transcode_mode']),
                    'encoder':        ov.get('encoder',        settings['encoder']),
                    'codec_info':     ov.get('codec_info',     settings['codec_info']),
                    'mode':           ov.get('quality_mode',   settings['mode']),
                    'bitrate':        ov.get('bitrate',        settings['bitrate']),
                    'crf':            int(ov.get('crf',        settings['crf'])),
                    'audio_codec':    ov.get('audio_codec',    settings['audio_codec']),
                    'audio_bitrate':  ov.get('audio_bitrate',  settings['audio_bitrate']),
                })
            file_info['est_size'] = estimate_output_size(file_info['path'], eff)
        # Redraw tree
        for item, file_info in zip(self.file_tree.get_children(), self.files):
            self._refresh_tree_row(item, file_info)

    def _ask_directory(self, initialdir=None, title="Select Folder"):
        """Open a folder-selection dialog.

        Tries zenity first (GTK dialog with proper single-click + Open
        button behaviour), then falls back to tkinter's askdirectory.
        """
        if initialdir:
            initialdir = str(initialdir)
        if shutil.which('zenity'):
            try:
                cmd = [
                    'zenity', '--file-selection', '--directory',
                    '--title', title,
                ]
                if initialdir:
                    cmd += ['--filename', initialdir + '/']
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=120
                )
                if result.returncode == 0 and result.stdout.strip():
                    return result.stdout.strip()
                return ''            # user cancelled
            except Exception:
                pass                 # fall through to tkinter
        return filedialog.askdirectory(initialdir=initialdir, title=title)

    def change_output_folder(self):
        """Set a custom output directory."""
        folder = self._ask_directory(
            initialdir=self.output_dir or self.working_dir,
            title="Select Output Folder"
        )
        if folder:
            self.output_dir = Path(folder)
            self.output_dir_label.configure(text=str(self.output_dir), foreground='black')
            self.add_log(f"Output folder set to: {folder}", 'INFO')

    def reset_output_folder(self):
        """Reset output directory to same as source."""
        self.output_dir = None
        self.output_dir_label.configure(text="Same as source file", foreground='gray')
        self.add_log("Output folder reset to same as source.", 'INFO')

    def move_file_up(self):
        """Move the selected file up one position in the queue."""
        item, index = self._get_selected_file_index()
        if index is None or index == 0:
            return
        # Swap in data list
        self.files[index], self.files[index - 1] = self.files[index - 1], self.files[index]
        # Rebuild tree
        self._rebuild_tree()
        # Re-select moved item
        new_item = self.file_tree.get_children()[index - 1]
        self.file_tree.selection_set(new_item)
        self.file_tree.see(new_item)

    def move_file_down(self):
        """Move the selected file down one position in the queue."""
        item, index = self._get_selected_file_index()
        if index is None or index >= len(self.files) - 1:
            return
        self.files[index], self.files[index + 1] = self.files[index + 1], self.files[index]
        self._rebuild_tree()
        new_item = self.file_tree.get_children()[index + 1]
        self.file_tree.selection_set(new_item)
        self.file_tree.see(new_item)

    def _sort_by_column(self, col):
        """Sort file list by the clicked column, toggle asc/desc."""
        if self._sort_col == col:
            self._sort_reverse = not self._sort_reverse
        else:
            self._sort_col = col
            self._sort_reverse = False

        def sort_key(f):
            if col == 'name':
                return f.get('name', '').lower()
            elif col == 'size':
                # Sort by raw bytes for accurate size ordering
                try:
                    return Path(f['path']).stat().st_size
                except Exception:
                    return 0
            elif col == 'duration':
                return f.get('duration_secs') or 0
            elif col == 'est_size':
                # Parse '~245.3 MB' → float bytes for sorting
                raw = f.get('est_size', '?').replace('~', '').strip()
                try:
                    val, unit = raw.split()
                    mult = {'B': 1, 'KB': 1024, 'MB': 1024**2, 'GB': 1024**3, 'TB': 1024**4}
                    return float(val) * mult.get(unit, 1)
                except Exception:
                    return 0
            elif col == 'status':
                return f.get('status', '').lower()
            return ''

        self.files.sort(key=sort_key, reverse=self._sort_reverse)
        self._rebuild_tree()

        # Update column headers to show sort indicator
        arrow = ' ▼' if self._sort_reverse else ' ▲'
        labels = {'name': 'Filename', 'size': 'Source Size',
                  'duration': 'Duration', 'est_size': 'Est. Output', 'status': 'Status'}
        for c, lbl in labels.items():
            indicator = arrow if c == col else ''
            self.file_tree.heading(c, text=lbl + indicator)

    def _rebuild_tree(self):
        """Redraw the entire file tree from self.files."""
        for item in self.file_tree.get_children():
            self.file_tree.delete(item)
        for file_info in self.files:
            name = ('⚙️ ' + file_info['name']) if 'overrides' in file_info else file_info['name']
            self.file_tree.insert('', 'end', values=(
                name,
                file_info['size'],
                file_info.get('duration_str', '?'),
                file_info.get('est_size', '?'),
                file_info['status']
            ))

    def clear_files(self):
        """Clear the file list"""
        for item in self.file_tree.get_children():
            self.file_tree.delete(item)
        self.files = []
        self.add_log("File list cleared.", 'INFO')

    def clear_finished(self):
        """Remove all successfully completed and skipped files from the queue."""
        remove_indices = []
        for i, item in enumerate(self.file_tree.get_children()):
            status = self.file_tree.item(item, 'values')[4]
            if status.startswith('✅') or status.startswith('⏭️'):
                remove_indices.append(i)

        if not remove_indices:
            self.add_log("No finished files to clear.", 'INFO')
            return

        # Remove in reverse order so indices stay valid
        items = self.file_tree.get_children()
        for i in reversed(remove_indices):
            self.file_tree.delete(items[i])
            del self.files[i]

        self.add_log(f"Cleared {len(remove_indices)} finished file(s) from queue.", 'INFO')

    def refresh_files(self):
        """Refresh file list from working directory.
        Phase 1 (instant): filesystem scan, populate tree immediately.
        Phase 2 (background): ffprobe each file for duration + est size.
        """
        # Clear existing items
        for item in self.file_tree.get_children():
            self.file_tree.delete(item)
        self.files = []

        # ── Phase 1: fast filesystem scan ──
        try:
            found = []
            for filepath in sorted(Path(self.working_dir).rglob('*')):
                if any(part.startswith('.') for part in filepath.relative_to(self.working_dir).parts):
                    continue
                if filepath.is_file():
                    ext = filepath.suffix.lower()
                    if ext in VIDEO_EXTENSIONS:
                        f = filepath.name
                        if not re.search(r'-(\d+(\.\d+)?M|CRF\d+)-(NVENC_|)(H265|H264|AV1|VP9|MPEG4|ProRes)_|-video-copy|-audio-copy|-[A-Z0-9]+_\d+k', f):
                            found.append(filepath)
        except Exception as e:
            self.add_log(f"Error scanning directory: {e}", 'ERROR')
            return

        # Sort and populate tree immediately with placeholders
        found.sort(key=lambda p: str(p).lower())
        settings = self._current_settings()
        for filepath in found:
            rel = str(filepath.relative_to(self.working_dir))
            size = format_size(filepath.stat().st_size)
            file_info = {
                'name': rel,
                'path': str(filepath),
                'size': size,
                'duration_str': '…',
                'duration_secs': None,
                'est_size': '…',
                'status': 'Pending',
                'external_subs': [],
            }
            self.files.append(file_info)
            self.file_tree.insert('', 'end', values=(rel, size, '…', '…', 'Pending'))

        count = len(self.files)
        self.add_log(f"Found {count} video file(s) — loading metadata...", 'INFO')
        self.status_label.configure(text=f"Loading metadata for {count} file(s)...")

        # Check for subtitle files in the same directory and offer to attach them
        if count > 0:
            self._offer_subtitle_association()

        # ── Phase 2: background ffprobe pass ──
        def _load_metadata():
            for idx, file_info in enumerate(self.files):
                if self.is_converting:
                    break  # don't probe during active conversion
                dur_secs = get_video_duration(file_info['path'])
                dur_str = format_duration(dur_secs)
                est = estimate_output_size(file_info['path'], settings)
                file_info['duration_str'] = dur_str
                file_info['duration_secs'] = dur_secs
                file_info['est_size'] = est

                # Update the tree row on the main thread
                def _update_row(i=idx, ds=dur_str, es=est):
                    try:
                        items = self.file_tree.get_children()
                        if i < len(items):
                            item = items[i]
                            vals = list(self.file_tree.item(item, 'values'))
                            vals[2] = ds  # duration
                            vals[3] = es  # est size
                            self.file_tree.item(item, values=vals)
                    except Exception:
                        pass
                self.root.after(0, _update_row)

            # Done
            def _done():
                self.status_label.configure(text="Ready")
                self.add_log(f"Metadata loaded for {count} file(s).", 'INFO')
            self.root.after(0, _done)

        threading.Thread(target=_load_metadata, daemon=True).start()

    def _offer_subtitle_association(self):
        """Check for subtitle files in the working directory and offer to attach them."""
        try:
            sub_files = []
            for filepath in sorted(Path(self.working_dir).rglob('*')):
                if any(part.startswith('.') for part in filepath.relative_to(self.working_dir).parts):
                    continue
                if filepath.is_file() and filepath.suffix.lower() in SUBTITLE_EXTENSIONS:
                    sub_files.append(filepath)
        except Exception:
            return

        if not sub_files:
            return

        # Ask the user
        count = len(sub_files)
        msg = (f"Found {count} subtitle file(s) in this folder.\n\n"
               f"Would you like to attach them to matching video files?")
        if not messagebox.askyesno("Subtitles Found", msg):
            return

        # Auto-associate each subtitle file
        attached = 0
        unmatched = []
        for sub_path in sub_files:
            if self._associate_external_sub(sub_path):
                attached += 1
            else:
                unmatched.append(sub_path)

        if attached:
            self.add_log(f"Attached {attached} subtitle file(s) to matching videos.", 'INFO')
        if unmatched:
            names = ', '.join(p.name for p in unmatched[:5])
            extra = f" (and {len(unmatched) - 5} more)" if len(unmatched) > 5 else ""
            self.add_log(f"Could not match {len(unmatched)} subtitle(s): {names}{extra}", 'WARNING')

    def change_folder(self):
        """Open custom single-click folder browser dialog"""
        dialog = tk.Toplevel(self.root)
        dialog.title("Select Video Folder")
        dialog.geometry("550x450")
        dialog.transient(self.root)
        dialog.grab_set()
        self._center_on_main(dialog)

        selected_path = tk.StringVar(value=str(self.working_dir))

        # Current path display
        path_frame = ttk.Frame(dialog, padding=(8, 8, 8, 0))
        path_frame.pack(fill='x')
        ttk.Label(path_frame, text="Current:").pack(side='left')
        path_label = ttk.Label(path_frame, textvariable=selected_path,
                               foreground='blue', anchor='w')
        path_label.pack(side='left', fill='x', expand=True, padx=(5, 0))

        # Treeview for folder browsing
        tree_frame = ttk.Frame(dialog, padding=8)
        tree_frame.pack(fill='both', expand=True)

        tree = ttk.Treeview(tree_frame, selectmode='browse', show='tree')
        vsb = ttk.Scrollbar(tree_frame, orient='vertical', command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side='left', fill='both', expand=True)
        vsb.pack(side='right', fill='y')

        def populate_tree(parent_id, path):
            """Add immediate subdirectories under parent_id."""
            try:
                entries = sorted(
                    [e for e in Path(path).iterdir() if e.is_dir() and not e.name.startswith('.')],
                    key=lambda e: e.name.lower()
                )
            except PermissionError:
                return
            for entry in entries:
                node = tree.insert(parent_id, 'end', text=entry.name,
                                   values=[str(entry)], open=False)
                # Insert a dummy child so the expand arrow appears
                tree.insert(node, 'end', text='__dummy__')

        def on_open(event):
            """Expand a node and populate its children on first open."""
            node = tree.focus()
            children = tree.get_children(node)
            if len(children) == 1 and tree.item(children[0], 'text') == '__dummy__':
                tree.delete(children[0])
                path = tree.item(node, 'values')[0]
                populate_tree(node, path)

        def on_select(event):
            """Update the path label on single click."""
            node = tree.focus()
            if node:
                path = tree.item(node, 'values')[0]
                selected_path.set(path)

        tree.bind('<<TreeviewOpen>>', on_open)
        tree.bind('<<TreeviewSelect>>', on_select)

        # Seed the tree with filesystem roots and expand to working_dir
        # Add home directory and / as top-level roots
        home = str(Path.home())
        roots = [('/ (root)', '/'), (f'~ (home: {Path.home().name})', home)]
        for label, rpath in roots:
            node = tree.insert('', 'end', text=label, values=[rpath], open=False)
            populate_tree(node, rpath)

        # Auto-expand and select current working_dir
        def expand_to(target):
            target = Path(target).resolve()
            parts = target.parts  # e.g. ('/', 'home', 'user', 'videos')
            # Walk tree nodes to find and expand the path
            def find_and_expand(parent_id, remaining):
                if not remaining:
                    return
                for child in tree.get_children(parent_id):
                    child_path = tree.item(child, 'values')
                    if not child_path:
                        continue
                    child_path = Path(child_path[0]).resolve()
                    try:
                        rel = child_path.relative_to(target.parents[len(remaining)-1] if len(remaining) > 1 else target.parent)
                        # simpler: just check if target starts with child_path
                    except Exception:
                        pass
                    if str(target).startswith(str(child_path)):
                        # expand this node
                        children = tree.get_children(child)
                        if len(children) == 1 and tree.item(children[0], 'text') == '__dummy__':
                            tree.delete(children[0])
                            populate_tree(child, str(child_path))
                        tree.item(child, open=True)
                        if child_path == target:
                            tree.selection_set(child)
                            tree.focus(child)
                            tree.see(child)
                            selected_path.set(str(target))
                            return
                        find_and_expand(child, remaining[1:])
                        return
            find_and_expand('', list(parts))

        dialog.after(100, lambda: expand_to(str(self.working_dir)))

        # Buttons
        btn_frame = ttk.Frame(dialog, padding=(8, 4, 8, 8))
        btn_frame.pack(fill='x')

        result = {'folder': None}

        def on_ok():
            result['folder'] = selected_path.get()
            dialog.destroy()

        def on_cancel():
            dialog.destroy()

        ttk.Button(btn_frame, text="Select Folder", command=on_ok).pack(side='right', padx=(4, 0))
        ttk.Button(btn_frame, text="Cancel", command=on_cancel).pack(side='right')

        # Also allow double-click to confirm
        tree.bind('<Double-1>', lambda e: on_ok())

        dialog.wait_window()

        if result['folder']:
            self.working_dir = Path(result['folder'])
            self._add_recent_folder(result['folder'])
            self.refresh_files()
            self.add_log(f"Changed directory to: {result['folder']}", 'INFO')
    
    # ── Recent Folders ───────────────────────────────────────────────────────

    def _add_recent_folder(self, folder):
        """Add a folder to the recent list (max 5, no duplicates)."""
        folder = str(folder)
        if folder in self.recent_folders:
            self.recent_folders.remove(folder)
        self.recent_folders.insert(0, folder)
        self.recent_folders = self.recent_folders[:5]
        self._rebuild_recent_menu()

    def _rebuild_recent_menu(self):
        """Rebuild the Recent Folders submenu from self.recent_folders."""
        self.recent_menu.delete(0, 'end')
        if not self.recent_folders:
            self.recent_menu.add_command(label="(none)", state='disabled')
        else:
            for folder in self.recent_folders:
                self.recent_menu.add_command(
                    label=folder,
                    command=lambda f=folder: self._open_recent_folder(f)
                )
            self.recent_menu.add_separator()
            self.recent_menu.add_command(label="Clear Recent",
                                         command=self._clear_recent_folders)

    def _open_recent_folder(self, folder):
        """Load a recent folder."""
        if not Path(folder).is_dir():
            messagebox.showwarning("Folder Not Found",
                                   f"This folder no longer exists:\n{folder}")
            self.recent_folders.remove(folder)
            self._rebuild_recent_menu()
            return
        self.working_dir = Path(folder)
        self.refresh_files()
        self.add_log(f"Opened recent folder: {folder}", 'INFO')

    def _clear_recent_folders(self):
        """Clear the recent folders list."""
        self.recent_folders = []
        self._rebuild_recent_menu()
        self.add_log("Recent folders cleared.", 'INFO')

    # ── Tools ────────────────────────────────────────────────────────────────

    def show_media_info(self):
        """Run ffprobe on the selected file and show a formatted info dialog."""
        item, index = self._get_selected_file_index()
        if index is None:
            messagebox.showinfo("Media Details", "Please select a file from the list first.")
            return
        filepath = self.files[index]['path']

        try:
            result = subprocess.run(
                ['ffprobe', '-v', 'quiet',
                 '-print_format', 'json',
                 '-show_format', '-show_streams',
                 filepath],
                capture_output=True, text=True, timeout=15
            )
            if result.returncode != 0:
                messagebox.showerror("Media Details Error", result.stderr[:300])
                return
            import json as _json
            data = _json.loads(result.stdout)
        except Exception as e:
            messagebox.showerror("Media Details Error", str(e))
            return

        dlg = tk.Toplevel(self.root)
        dlg.title(f"Media Details — {os.path.basename(filepath)}")
        dlg.geometry("620x520")
        dlg.transient(self.root)
        dlg.resizable(True, True)
        self._center_on_main(dlg)

        text = scrolledtext.ScrolledText(dlg, wrap='word', font=('Courier', 9))
        text.pack(fill='both', expand=True, padx=8, pady=8)

        fmt = data.get('format', {})
        lines = []
        lines.append(f"{'='*50}")
        lines.append(f"  FILE: {os.path.basename(filepath)}")
        lines.append(f"{'='*50}")
        lines.append(f"  Format:    {fmt.get('format_long_name', fmt.get('format_name', '?'))}")
        dur = float(fmt.get('duration', 0))
        lines.append(f"  Duration:  {format_duration(dur)} ({dur:.2f}s)")
        size = int(fmt.get('size', 0))
        lines.append(f"  File Size: {format_size(size)}")
        br = int(fmt.get('bit_rate', 0))
        lines.append(f"  Bitrate:   {br // 1000} kbps" if br else "  Bitrate:   ?")

        for i, stream in enumerate(data.get('streams', [])):
            lines.append("")
            ctype = stream.get('codec_type', '?').upper()
            cname = stream.get('codec_long_name', stream.get('codec_name', '?'))
            lines.append(f"  STREAM #{stream.get('index','?')} — {ctype}")
            lines.append(f"  {'─'*46}")
            lines.append(f"    Codec:      {cname}")
            if ctype == 'VIDEO':
                lines.append(f"    Resolution: {stream.get('width','?')}x{stream.get('height','?')}")
                lines.append(f"    Frame Rate: {stream.get('r_frame_rate','?')}")
                lines.append(f"    Pixel Fmt:  {stream.get('pix_fmt','?')}")
                lines.append(f"    Profile:    {stream.get('profile','?')}")
                sbr = stream.get('bit_rate')
                if sbr:
                    lines.append(f"    Bitrate:    {int(sbr)//1000} kbps")
            elif ctype == 'AUDIO':
                lines.append(f"    Sample Rate: {stream.get('sample_rate','?')} Hz")
                lines.append(f"    Channels:    {stream.get('channels','?')}")
                lines.append(f"    Layout:      {stream.get('channel_layout','?')}")
                sbr = stream.get('bit_rate')
                if sbr:
                    lines.append(f"    Bitrate:     {int(sbr)//1000} kbps")
            elif ctype == 'SUBTITLE':
                tags = stream.get('tags', {})
                disp = stream.get('disposition', {})
                lang  = tags.get('language', '?')
                title = tags.get('title', '')
                lines.append(f"    Language:   {lang.upper()}")
                if title:
                    lines.append(f"    Title:      {title}")
                lines.append(f"    Codec:      {stream.get('codec_name','?')}")
                flags = []
                if disp.get('forced'):  flags.append('Forced')
                if disp.get('hearing_impaired'): flags.append('SDH')
                if disp.get('default'): flags.append('Default')
                if disp.get('commentary'): flags.append('Commentary')
                if flags:
                    lines.append(f"    Flags:      {', '.join(flags)}")
            else:
                tags = stream.get('tags', {})
                lang = tags.get('language')
                title = tags.get('title')
                if lang:  lines.append(f"    Language:   {lang}")
                if title: lines.append(f"    Title:      {title}")

        lines.append("")
        lines.append(f"{'='*50}")
        text.insert('end', '\n'.join(lines))
        text.configure(state='disabled')

        ttk.Button(dlg, text="Close", command=dlg.destroy).pack(pady=(0, 8))

    def show_enhanced_media_info(self):
        """Show the enhanced media info dialog for the selected file."""
        item, index = self._get_selected_file_index()
        if index is None:
            messagebox.showinfo("Enhanced Media Details",
                                "Please select a file from the list first.")
            return
        filepath = self.files[index]['path']
        try:
            from modules.media_info import show_enhanced_media_info as _show
            _show(self, filepath)
        except ImportError:
            # Fallback: try loading from the same directory as this script
            import importlib.util
            _mi_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     'modules', 'media_info.py')
            if os.path.exists(_mi_path):
                spec = importlib.util.spec_from_file_location('media_info', _mi_path)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                mod.show_enhanced_media_info(self, filepath)
            else:
                messagebox.showerror("Enhanced Media Details",
                                     "modules/media_info.py not found.")

    def test_encode(self):
        """Encode the first 30 seconds of the selected file with current settings."""
        item, index = self._get_selected_file_index()
        if index is None:
            messagebox.showinfo("Test Encode", "Please select a file from the list first.")
            return
        if self.is_converting:
            messagebox.showwarning("Test Encode", "A conversion is already running.")
            return

        file_info = self.files[index]
        input_path = file_info['path']
        base = Path(input_path).stem
        out_dir = self.output_dir or Path(input_path).parent
        test_output = str(out_dir / f"{base}-TEST30s{self.container_format.get()}")

        if not messagebox.askyesno("Test Encode",
                f"Encode the first 30 seconds of:\n{os.path.basename(input_path)}\n\n"
                f"Output: {os.path.basename(test_output)}\n\n"
                f"Using current settings. Continue?"):
            return

        # Build settings same as run_conversion
        codec_name = self.video_codec.get()
        settings = {
            'transcode_mode': self.transcode_mode.get(),
            'encoder':        self.encoder_mode.get(),
            'codec_info':     self.get_codec_info(),
            'codec_name':     codec_name,
            'mode':           self.quality_mode.get(),
            'bitrate':        self.bitrate.get(),
            'crf':            int(self.crf.get()),
            'preset':         self.preset_combo.get(),
            'gpu_preset':     self.gpu_preset.get(),
            'audio_codec':    self.get_audio_codec_name(),
            'audio_bitrate':  self.audio_bitrate.get(),
            'hw_decode':      self.hw_decode.get(),
            'two_pass':       False,  # no two-pass for test
            'subtitle_settings': {},
        }

        self.add_log(f"Test encode starting: {os.path.basename(input_path)} (first 30s)", 'INFO')
        self.status_label.configure(text="Test encoding (30s)...")

        def _run():
            # Build command manually with -t 30
            cmd = ['ffmpeg', '-y']
            encoder  = settings['encoder']
            hw       = settings['hw_decode']
            ci       = settings['codec_info']
            cn       = settings['codec_name']
            tm       = settings['transcode_mode']
            is_gpu   = encoder != 'cpu'
            backend  = GPU_BACKENDS.get(encoder) if is_gpu else None

            # Resolve video encoder name
            if is_gpu:
                video_enc = get_gpu_encoder(cn, encoder) or ci['cpu_encoder']
            else:
                video_enc = ci['cpu_encoder']

            effective_hw = hw and is_gpu and backend and tm in ('video','both') and video_enc not in (None,'copy')
            if effective_hw:
                cmd.extend(backend['hwaccel'])
            cmd.extend(['-i', input_path, '-t', '30'])

            # Check for 10-bit → 8-bit pixel format conversion
            test_pix_convert = False
            if effective_hw:
                src_pix = get_video_pix_fmt(input_path) or ''
                _8bit_only = {'h264_nvenc', 'h264_qsv', 'h264_vaapi'}
                if '10' in src_pix and video_enc in _8bit_only:
                    test_pix_convert = True

            if tm in ('video','both'):
                if video_enc != 'copy':
                    # Filters MUST come before -c:v for hwaccel compatibility
                    if test_pix_convert and backend:
                        cmd.extend(['-vf', backend['scale_filter']])
                    elif ci['cpu_encoder'] == 'prores_ks':
                        prores_profile = settings['preset'] or 'hq'
                        if prores_profile in ('4444', '4444xq'):
                            pix = 'yuva444p10le'
                        else:
                            pix = 'yuv422p10le'
                        cmd.extend(['-vf', f'format={pix}'])
                cmd.extend(['-c:v', video_enc])
                if video_enc != 'copy':
                    preset = settings['preset']
                    if preset:
                        if ci['cpu_encoder'] == 'libvpx-vp9' and encoder == 'cpu':
                            cmd.extend(['-cpu-used', preset])
                        elif ci['cpu_encoder'] == 'prores_ks' and encoder == 'cpu':
                            cmd.extend(['-profile:v', preset])
                        elif is_gpu and backend and backend.get('preset_flag'):
                            cmd.extend([backend['preset_flag'], preset])
                        elif not is_gpu:
                            cmd.extend(['-preset', preset])
                    if settings['mode'] == 'crf':
                        crf_val = str(settings['crf'])
                        if is_gpu and backend:
                            cq_flag = backend.get('cq_flag')
                            if cq_flag:
                                cmd.extend([cq_flag, crf_val])
                        elif ci['crf_flag']:
                            cmd.extend([ci['crf_flag'], crf_val])
                    else:
                        cmd.extend(['-b:v', settings['bitrate']])
            elif tm == 'audio':
                cmd.extend(['-c:v', 'copy'])
            LOSSLESS = {'flac','alac','pcm_s16le','pcm_s24le','wavpack','tta'}
            EXPERIMENTAL = {'opus','vorbis'}
            ac = settings['audio_codec']
            if ac == 'copy':
                cmd.extend(['-c:a','copy'])
            else:
                cmd.extend(['-c:a', ac])
                if ac in EXPERIMENTAL:
                    cmd.extend(['-strict','-2'])
                if ac not in LOSSLESS:
                    cmd.extend(['-b:a', settings['audio_bitrate']])
            cmd.extend(['-c:s','copy', test_output])

            self.add_log(f"Test command: {' '.join(cmd)}", 'INFO')
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
                if result.returncode == 0:
                    size = format_size(Path(test_output).stat().st_size)
                    self.add_log(f"Test encode complete: {os.path.basename(test_output)} ({size})", 'SUCCESS')
                    self.root.after(0, lambda: messagebox.showinfo(
                        "Test Encode Complete",
                        f"Test encode finished!\n\n"
                        f"Output: {os.path.basename(test_output)}\n"
                        f"Size: {size}\n\n"
                        f"Saved to:\n{out_dir}"
                    ))
                else:
                    self.add_log(f"Test encode failed: {result.stderr[-300:]}", 'ERROR')
                    self.root.after(0, lambda: messagebox.showerror(
                        "Test Encode Failed",
                        f"ffmpeg returned an error.\nCheck the log for details."
                    ))
            except Exception as e:
                self.add_log(f"Test encode error: {e}", 'ERROR')
            finally:
                self.root.after(0, lambda: self.status_label.configure(text="Ready"))

        threading.Thread(target=_run, daemon=True).start()

    def _play_file(self, filepath):
        """Launch a video player for the given file path."""
        if not filepath or not Path(filepath).exists():
            messagebox.showwarning("Play File",
                                   f"File not found:\n{filepath}")
            return
        preferred = self.default_player.get()
        if preferred == 'System Default':
            # Use the OS default application for the file type (xdg-open on Linux)
            xdg = shutil.which('xdg-open')
            if xdg:
                try:
                    subprocess.Popen([xdg, filepath])
                    self.add_log(f"Playing with system default app: {os.path.basename(filepath)}", 'INFO')
                    return
                except Exception as e:
                    self.add_log(f"Failed to launch system default: {e}", 'WARNING')
            else:
                self.add_log("xdg-open not found; falling back to auto.", 'WARNING')
        elif preferred and preferred != 'auto':
            # Accept either a plain name or a full path
            player_cmd = preferred if Path(preferred).is_absolute() else shutil.which(preferred)
            if player_cmd:
                try:
                    subprocess.Popen([player_cmd, filepath])
                    self.add_log(f"Playing with {os.path.basename(player_cmd)}: {os.path.basename(filepath)}", 'INFO')
                    return
                except Exception as e:
                    self.add_log(f"Failed to launch {preferred}: {e}", 'WARNING')
            else:
                self.add_log(f"Preferred player '{preferred}' not found, falling back to auto.", 'WARNING')
        # Auto: try common players in order
        for player in ('vlc', 'mpv', 'totem', 'ffplay'):
            if shutil.which(player):
                try:
                    subprocess.Popen([player, filepath])
                    self.add_log(f"Playing with {player}: {os.path.basename(filepath)}", 'INFO')
                    return
                except Exception as e:
                    self.add_log(f"Failed to launch {player}: {e}", 'WARNING')
        messagebox.showerror("No Player Found",
                             "Could not find a video player (vlc, mpv, totem, ffplay).\n"
                             "Please install a video player or set a custom one in\n"
                             "Settings → Default Settings.")

    def play_source_file(self):
        """Play the source file for the selected queue item."""
        item, index = self._get_selected_file_index()
        if index is None:
            messagebox.showinfo("Play Source File",
                                "Please select a file from the list first.")
            return
        self._play_file(self.files[index]['path'])

    def play_output_file(self):
        """Play the converted output file for the selected queue item."""
        item, index = self._get_selected_file_index()
        if index is None:
            messagebox.showinfo("Play Output File",
                                "Please select a file from the list first.")
            return
        file_info = self.files[index]
        output_path = file_info.get('output_path')
        if not output_path or not Path(output_path).exists():
            messagebox.showinfo("Play Output File",
                                "No converted output file found for this item.\n"
                                "Convert it first, then try again.")
            return
        self._play_file(output_path)

    def open_output_folder(self):
        """Open the output folder in the system file manager."""
        folder = self.output_dir or self.working_dir
        if not folder or not Path(folder).is_dir():
            messagebox.showwarning("Open Output Folder",
                                   "No valid output folder is set.")
            return
        try:
            subprocess.Popen(['xdg-open', str(folder)])
            self.add_log(f"Opened folder: {folder}", 'INFO')
        except Exception as e:
            messagebox.showerror("Error", f"Could not open folder:\n{e}")

    def get_codec_info(self):
        """Return the VIDEO_CODEC_MAP entry for the currently selected codec."""
        return VIDEO_CODEC_MAP.get(self.video_codec.get(), VIDEO_CODEC_MAP['H.265 / HEVC'])

    def _get_encoder_backend(self):
        """Return the current GPU backend id or 'cpu'."""
        return self.encoder_mode.get()

    def _get_video_encoder_name(self, codec_name=None, encoder=None):
        """Return the ffmpeg encoder name for the current codec + encoder selection."""
        if codec_name is None:
            codec_name = self.video_codec.get()
        if encoder is None:
            encoder = self.encoder_mode.get()
        if encoder == 'cpu':
            return VIDEO_CODEC_MAP.get(codec_name, VIDEO_CODEC_MAP['H.265 / HEVC'])['cpu_encoder']
        return get_gpu_encoder(codec_name, encoder)

    def _encoder_has_codec(self, codec_name, encoder):
        """Check if the given encoder backend supports the given codec."""
        if encoder == 'cpu':
            return True
        gpu_enc = get_gpu_encoder(codec_name, encoder)
        return gpu_enc is not None and gpu_enc != 'copy'

    def _encoder_display_label(self, encoder=None):
        """Human-readable label for the current encoder (e.g. 'NVIDIA (NVENC)')."""
        if encoder is None:
            encoder = self.encoder_mode.get()
        if encoder == 'cpu':
            return 'CPU'
        backend = GPU_BACKENDS.get(encoder)
        return backend['label'] if backend else encoder

    def on_video_codec_change(self, event=None):
        """Handle video codec selection change."""
        info = self.get_codec_info()
        encoder = self.encoder_mode.get()
        codec_name = self.video_codec.get()

        # If current GPU backend doesn't support this codec, fall back to CPU
        if encoder != 'cpu' and not self._encoder_has_codec(codec_name, encoder):
            self.encoder_mode.set('cpu')
            self.encoder_combo.set(self._encoder_ids['cpu'])

        # Update encoder combo — filter out GPU backends that don't support this codec
        self._rebuild_encoder_combo(codec_name)

        # Copy mode — hide quality / preset controls entirely
        if info['cpu_encoder'] == 'copy':
            self.quality_mode_frame.grid_remove()
            self.bitrate_frame.grid_remove()
            self.bitrate_preset_frame.grid_remove()
            self.crf_frame.grid_remove()
            self.crf_preset_frame.grid_remove()
            self.preset_label.grid_remove()
            self.preset_combo.grid_remove()
        else:
            # Update CRF slider range and default
            crf_min = info['crf_min']
            crf_max = info['crf_max']
            crf_default = info['crf_default']
            # Re-build crf slider range by reconfiguring the scale widget
            try:
                crf_slider = self.crf_frame.winfo_children()[1]
                crf_slider.configure(from_=crf_min, to=crf_max)
            except (IndexError, tk.TclError):
                pass
            # Update CRF hint label
            try:
                hint = self.crf_frame.winfo_children()[3]
                hint.configure(text=f"({crf_min}–{crf_max}, lower=better)")
            except (IndexError, tk.TclError):
                pass
            # Clamp current CRF to new range
            try:
                current = int(self.crf.get())
                if current < crf_min or current > crf_max:
                    self.crf.set(str(crf_default))
                    self.crf_var.set(crf_default)
            except (ValueError, tk.TclError):
                self.crf.set(str(crf_default))
                self.crf_var.set(crf_default)

            # Update presets
            self._apply_presets_for_codec(info, silent=False)

            # Re-show controls if we were in copy mode before
            self.on_transcode_mode_change()

        self.add_log(f"Video codec: {self.video_codec.get()}", 'INFO')
        self._schedule_estimate_refresh()

    def _rebuild_encoder_combo(self, codec_name=None):
        """Rebuild the encoder combobox values, disabling backends that don't support the codec."""
        if codec_name is None:
            codec_name = self.video_codec.get()
        labels = [self._encoder_ids['cpu']]
        for bid in self.gpu_backends:
            if self._encoder_has_codec(codec_name, bid) or codec_name == 'Copy (no re-encode)':
                labels.append(self._encoder_ids[bid])
        self.encoder_combo['values'] = labels

    def _apply_presets_for_codec(self, info, silent=True):
        """Update preset combobox values and selection for current codec+encoder."""
        encoder = self.encoder_mode.get()
        codec_name = self.video_codec.get()

        if encoder != 'cpu':
            gpu_enc = get_gpu_encoder(codec_name, encoder)
            if gpu_enc and gpu_enc != 'copy':
                presets, default = get_gpu_presets(encoder)
            else:
                presets = info['cpu_presets']
                default = info['cpu_preset_default']
        else:
            presets = info['cpu_presets']
            default = info['cpu_preset_default']

        if presets:
            self.preset_combo['values'] = presets
            # Keep current selection if it's valid, else use default
            current = self.preset_combo.get()
            if current not in presets:
                self.preset_combo.set(default or presets[0])
            self.preset_label.grid()
            self.preset_combo.grid()
        else:
            self.preset_combo.set('')
            self.preset_label.grid_remove()
            self.preset_combo.grid_remove()

    def _on_metadata_toggle(self):
        """Enable/disable the track language fields based on the Set track metadata checkbox."""
        state = 'normal' if self.set_track_metadata.get() else 'disabled'
        self.meta_video_entry.configure(state=state)
        self.meta_audio_entry.configure(state=state)
        self.meta_sub_entry.configure(state=state)

    def on_two_pass_change(self):
        """Notify user when two-pass is enabled on GPU."""
        encoder = self.encoder_mode.get()
        if self.two_pass.get() and encoder != 'cpu':
            backend = GPU_BACKENDS.get(encoder)
            if backend and backend['multipass_encoders']:
                messagebox.showinfo(
                    "GPU Two-Pass Encoding",
                    f"On {backend['label']}, two-pass uses the "
                    f"{' '.join(backend['multipass_args'])} flag which runs "
                    "inside a single ffmpeg process.\n\n"
                    "This is different from CPU two-pass which runs ffmpeg twice — "
                    "once for analysis and once for encoding.\n\n"
                    "For the most accurate bitrate targeting and best quality, "
                    "use CPU encoding with two-pass enabled."
                )

    def _update_two_pass_state(self):
        """Enable two-pass checkbox only when applicable."""
        info = self.get_codec_info()
        encoder = self.encoder_mode.get()
        mode = self.quality_mode.get()
        codec_name = self.video_codec.get()
        cpu_enc = info.get('cpu_encoder', '')
        TWO_PASS_SUPPORTED = {'libx265', 'libx264', 'libvpx-vp9', 'mpeg4'}

        # Check GPU multipass support for current backend
        gpu_multipass = False
        if encoder != 'cpu':
            backend = GPU_BACKENDS.get(encoder)
            if backend:
                gpu_enc = get_gpu_encoder(codec_name, encoder)
                gpu_multipass = gpu_enc in backend.get('multipass_encoders', set())

        applicable = (
            mode == 'bitrate' and (
                (encoder == 'cpu' and cpu_enc in TWO_PASS_SUPPORTED) or
                (encoder != 'cpu' and gpu_multipass)
            )
        )
        self.two_pass_check.configure(state='normal' if applicable else 'disabled')
        if not applicable:
            self.two_pass.set(False)

    def _on_encoder_combo(self):
        """Handle encoder combobox selection."""
        label = self.encoder_combo.get()
        bid = self._encoder_labels.get(label, 'cpu')
        self.encoder_mode.set(bid)
        self.on_encoder_change()

    def on_encoder_change(self, silent=False):
        """Handle encoder selection change (CPU or any GPU backend)."""
        info = self.get_codec_info()
        encoder = self.encoder_mode.get()

        # Sync the combobox display
        display = self._encoder_ids.get(encoder, self._encoder_ids.get('cpu', 'CPU'))
        if self.encoder_combo.get() != display:
            self.encoder_combo.set(display)

        self._apply_presets_for_codec(info, silent=silent)

        # Enable/disable HW decode checkbox based on encoder selection
        if encoder != 'cpu' and encoder in self.gpu_backends:
            self.hw_decode_check.configure(state='normal')
        else:
            self.hw_decode.set(False)
            self.hw_decode_check.configure(state='disabled')

        self._update_two_pass_state()

        if not silent:
            label = self._encoder_display_label(encoder)
            preset = self.preset_combo.get()
            self.add_log(f"Switched to {label} encoding (preset: {preset})", 'INFO')
        self._schedule_estimate_refresh()

    def on_preset_change(self, event=None):
        """Handle preset selection change."""
        preset = self.preset_combo.get()
        if self.encoder_mode.get() != 'cpu':
            self.gpu_preset.set(preset)
        else:
            self.cpu_preset.set(preset)
    
    def validate_bitrate(self, new_value):
        """Validate bitrate input - only allow numbers and one decimal point"""
        if new_value == "":
            return True
        try:
            val = float(new_value)
            # Allow 0.1 to 99.9 range during typing
            return 0 <= val <= 99.9
        except ValueError:
            return False
    
    def on_bitrate_change(self, value):
        """Handle bitrate slider change - updates entry field"""
        try:
            bitrate_val = float(value)
            self.bitrate_var.set(round(bitrate_val, 1))
            self.bitrate.set(f"{bitrate_val:.1f}M")
        except (ValueError, tk.TclError):
            pass
        self._schedule_estimate_refresh()
    
    def on_bitrate_entry_focus_out(self, event):
        """Handle bitrate entry losing focus - validate and clamp value"""
        self.validate_and_apply_bitrate()
    
    def on_bitrate_entry_return(self, event):
        """Handle Enter key in bitrate entry"""
        self.validate_and_apply_bitrate()
        # Move focus to next widget
        event.widget.master.focus_next()
    
    def validate_and_apply_bitrate(self):
        """Validate bitrate entry and apply to slider"""
        try:
            value = float(self.bitrate_var.get())
            # Clamp to valid range
            value = max(0.1, min(99.9, value))
            self.bitrate_var.set(round(value, 1))
            self.bitrate.set(f"{value:.1f}M")
            # Update slider position
            self.bitrate_frame.winfo_children()[1].set(value)
        except (ValueError, tk.TclError):
            # Reset to last valid value
            self.bitrate_var.set(2.0)
            self.bitrate.set("2.0M")
    
    def set_bitrate(self, value):
        """Set bitrate from preset button"""
        self.bitrate_var.set(value)
        self.bitrate.set(f"{value:.1f}M")
        # Update slider position
        try:
            self.bitrate_frame.winfo_children()[1].set(value)
        except (IndexError, tk.TclError):
            pass
    
    def validate_crf(self, new_value):
        """Validate CRF input - only allow integers 0-51"""
        if new_value == "":
            return True
        try:
            val = int(new_value)
            return 0 <= val <= 51
        except ValueError:
            return False
    
    def on_crf_change(self, value):
        """Handle CRF slider change - updates entry field"""
        try:
            crf_val = int(float(value))
            self.crf_var.set(crf_val)
            self.crf.set(str(crf_val))
        except (ValueError, tk.TclError):
            pass
        self._schedule_estimate_refresh()
    
    def on_crf_entry_focus_out(self, event):
        """Handle CRF entry losing focus - validate and clamp value"""
        self.validate_and_apply_crf()
    
    def on_crf_entry_return(self, event):
        """Handle Enter key in CRF entry"""
        self.validate_and_apply_crf()
        event.widget.master.focus_next()
    
    def validate_and_apply_crf(self):
        """Validate CRF entry and apply to slider"""
        try:
            value = int(self.crf_var.get())
            # Clamp to valid range
            value = max(0, min(51, value))
            self.crf_var.set(value)
            self.crf.set(str(value))
            # Update slider position
            self.crf_frame.winfo_children()[1].set(value)
        except (ValueError, tk.TclError):
            # Reset to last valid value
            self.crf_var.set(23)
            self.crf.set("23")
    
    def set_crf(self, value):
        """Set CRF from preset button"""
        self.crf_var.set(value)
        self.crf.set(str(value))
        # Update slider position
        try:
            self.crf_frame.winfo_children()[1].set(value)
        except (IndexError, tk.TclError):
            pass
    
    def get_audio_codec_name(self):
        """Get the actual ffmpeg codec name from the display name"""
        display_name = self.audio_codec.get()
        return self.audio_codec_map.get(display_name, 'aac')
    
    def _schedule_estimate_refresh(self):
        """Schedule a refresh of estimated sizes (debounced by 400ms)."""
        if hasattr(self, '_estimate_refresh_job'):
            self.root.after_cancel(self._estimate_refresh_job)
        self._estimate_refresh_job = self.root.after(400, self.refresh_estimated_sizes)

    def on_transcode_mode_change(self):
        """Handle transcode mode change (video/audio/both)"""
        mode = self.transcode_mode.get()
        
        if mode == 'audio':
            # Audio only - hide video quality controls
            self.quality_mode_frame.grid_remove()
            self.bitrate_frame.grid_remove()
            self.bitrate_preset_frame.grid_remove()
            self.crf_frame.grid_remove()
            self.crf_preset_frame.grid_remove()
            self.preset_label.grid_remove()
            self.preset_combo.grid_remove()
            # Show audio controls (enabled)
            self.audio_frame.grid(row=3)
            self.audio_codec_combo.configure(state='readonly')
            self.audio_bitrate_combo.configure(state='readonly')
            self.check_frame.grid(row=4)
            self.add_log("Audio-only transcoding mode selected", 'INFO')
        else:
            # Video or Both - show video quality controls
            self.quality_mode_frame.grid(row=2)

            # Update quality mode controls (will position everything correctly)
            self.on_quality_mode_change()

            if mode == 'both':
                self.add_log("Video + Audio transcoding mode selected", 'INFO')
            else:
                self.add_log("Video-only transcoding mode selected (audio will be copied)", 'INFO')
        self._schedule_estimate_refresh()
    
    def on_quality_mode_change(self):
        """Handle quality mode change - show/hide appropriate controls"""
        # Only update if in video or both mode
        if self.transcode_mode.get() == 'audio':
            return
        
        # Determine if audio frame should be shown
        show_audio = self.transcode_mode.get() == 'both'
        
        # First, hide video quality frames to prevent overlap
        self.bitrate_frame.grid_remove()
        self.bitrate_preset_frame.grid_remove()
        self.crf_frame.grid_remove()
        self.crf_preset_frame.grid_remove()
        
        if self.quality_mode.get() == 'crf':
            # CRF Mode: Show CRF controls at rows 3-4
            self.crf_frame.grid(row=3)
            self.crf_preset_frame.grid(row=4)
        else:
            # Bitrate Mode: Show bitrate controls at rows 3-4
            self.bitrate_frame.grid(row=3)
            self.bitrate_preset_frame.grid(row=4)

        # Preset dropdown always stays at row 5
        self.preset_label.grid(row=5)
        self.preset_combo.grid(row=5)

        # Audio controls are always visible but disabled when not in 'both' mode
        self.audio_frame.grid(row=6)
        self.check_frame.grid(row=7)
        audio_state = 'readonly' if show_audio else 'disabled'
        self.audio_codec_combo.configure(state=audio_state)
        self.audio_bitrate_combo.configure(state=audio_state)
        self._update_two_pass_state()
        self._schedule_estimate_refresh()

    def start_conversion(self):
        """Start batch conversion"""
        if not self.files:
            messagebox.showinfo("No Files", "No video files found in the selected folder.")
            return
        
        if not self.has_ffmpeg:
            messagebox.showerror("Error", "ffmpeg is not installed.")
            return
        
        # Confirm settings
        encoder = self.encoder_mode.get()
        if encoder != 'cpu' and encoder not in self.gpu_backends:
            messagebox.showerror("Error", f"GPU backend '{encoder}' is not available on this system.")
            return

        # Container / codec compatibility check
        container = self.container_format.get()
        codec_name = self.video_codec.get()
        CONTAINER_CODEC_COMPAT = {
            '.avi':  {'H.264 / AVC', 'MPEG-4', 'Copy (no re-encode)'},
            '.webm': {'VP9', 'AV1', 'Copy (no re-encode)'},
            '.mov':  {'H.265 / HEVC', 'H.264 / AVC', 'ProRes (QuickTime)', 'MPEG-4', 'Copy (no re-encode)'},
            '.ts':   {'H.265 / HEVC', 'H.264 / AVC', 'MPEG-4', 'Copy (no re-encode)'},
        }
        allowed = CONTAINER_CODEC_COMPAT.get(container)
        if allowed and codec_name not in allowed:
            supported = ', '.join(sorted(c for c in allowed if c != 'Copy (no re-encode)'))
            messagebox.showerror(
                "Incompatible Settings",
                f"The {container} container does not support {codec_name}.\n\n"
                f"Supported codecs for {container}: {supported}\n\n"
                f"Please change the codec or container format.")
            return

        # Disable controls
        self.is_converting = True
        self.pause_btn.configure(state='normal')
        self.stop_btn.configure(state='normal')
        
        # Start conversion thread
        self.conversion_thread = threading.Thread(target=self.run_conversion)
        self.conversion_thread.daemon = True
        self.conversion_thread.start()
        
        self.add_log("=" * 50, 'INFO')
        self.add_log(f"Starting batch conversion", 'INFO')
        
        # Log transcode mode
        mode = self.transcode_mode.get()
        if mode == 'video':
            self.add_log("Transcode Mode: Video Only (audio will be copied)", 'INFO')
        elif mode == 'audio':
            self.add_log("Transcode Mode: Audio Only (video will be copied)", 'INFO')
        else:
            self.add_log("Transcode Mode: Video + Audio", 'INFO')
        
        # Log video settings (if applicable)
        if mode in ['video', 'both']:
            codec_info = self.get_codec_info()
            codec_name = self.video_codec.get()
            is_gpu = encoder != 'cpu'
            backend = GPU_BACKENDS.get(encoder) if is_gpu else None
            if is_gpu:
                video_encoder = get_gpu_encoder(codec_name, encoder) or codec_info['cpu_encoder']
            else:
                video_encoder = codec_info['cpu_encoder']
            self.add_log(f"Video Codec: {codec_name} ({video_encoder})", 'INFO')
            self.add_log(f"Encoder: {self._encoder_display_label(encoder)}", 'INFO')
            if is_gpu and self.hw_decode.get():
                hwaccel_type = backend['hwaccel'][1] if backend else 'unknown'
                self.add_log(f"Hardware Decode: {hwaccel_type} (enabled)", 'INFO')
            elif is_gpu:
                self.add_log("Hardware Decode: disabled", 'INFO')
            if video_encoder != 'copy':
                self.add_log(f"Quality Mode: {self.quality_mode.get()}", 'INFO')
                if self.quality_mode.get() == 'bitrate':
                    self.add_log(f"Video Bitrate: {self.bitrate.get()}", 'INFO')
                    if self.two_pass.get():
                        info = self.get_codec_info()
                        cpu_enc = info.get('cpu_encoder', '')
                        TWO_PASS_CPU = {'libx265', 'libx264', 'libvpx-vp9', 'mpeg4'}
                        if encoder == 'cpu' and cpu_enc in TWO_PASS_CPU:
                            self.add_log("Two-Pass Encoding: enabled (pass 1 = analysis, pass 2 = encode)", 'INFO')
                        elif is_gpu and backend:
                            gpu_enc = get_gpu_encoder(codec_name, encoder)
                            if gpu_enc in backend.get('multipass_encoders', set()):
                                mp_label = ' '.join(backend['multipass_args'])
                                self.add_log(f"Two-Pass Encoding: GPU multipass {mp_label} ({backend['short']})", 'INFO')
                            else:
                                self.add_log("Two-Pass Encoding: requested but not supported for this codec — using single pass", 'WARNING')
                        else:
                            self.add_log("Two-Pass Encoding: requested but not supported for this codec — using single pass", 'WARNING')
                    else:
                        self.add_log("Two-Pass Encoding: disabled (single pass)", 'INFO')
                else:
                    self.add_log(f"CRF: {self.crf.get()}", 'INFO')
                self.add_log(f"Preset: {self.preset_combo.get()}", 'INFO')
        
        # Log audio settings (if applicable)
        if mode in ['audio', 'both']:
            audio_codec_display = self.audio_codec.get()
            audio_codec_name = self.get_audio_codec_name()
            if audio_codec_name == 'copy':
                self.add_log("Audio: Copying original stream", 'INFO')
            else:
                self.add_log(f"Audio Codec: {audio_codec_display}", 'INFO')
                self.add_log(f"Audio Bitrate: {self.audio_bitrate.get()}", 'INFO')
        
        self.add_log("Subtitles: Copying all streams (no re-encode)", 'INFO')
        self.add_log(f"Files to convert: {len(self.files)}", 'INFO')
        self.add_log("=" * 50, 'INFO')
    
    def run_conversion(self):
        """Run batch conversion in background thread"""
        self.start_time = datetime.now()
        self.current_file_index = 0
        self._batch_speed_samples = []  # reset for new batch
        self._file_start_time = None
        completed = 0
        failed = 0
        skipped = 0
        
        settings = {
            'transcode_mode': self.transcode_mode.get(),
            'encoder': self.encoder_mode.get(),
            'codec_info': self.get_codec_info(),
            'codec_name': self.video_codec.get(),
            'mode': self.quality_mode.get(),
            'bitrate': self.bitrate.get(),
            'crf': int(self.crf.get()),
            'preset': self.preset_combo.get(),
            'gpu_preset': self.gpu_preset.get(),
            'audio_codec': self.get_audio_codec_name(),
            'audio_bitrate': self.audio_bitrate.get(),
            'hw_decode': self.hw_decode.get(),
            'two_pass': self.two_pass.get(),
            'subtitle_settings': {},  # per-file override below
            'external_subs': [],     # per-file — populated from file_info
            'container': self.container_format.get(),
            'strip_chapters':      self.strip_chapters.get(),
            'strip_metadata_tags': self.strip_metadata_tags.get(),
            'set_track_metadata':  self.set_track_metadata.get(),
            'meta_video_lang':     self.meta_video_lang.get(),
            'meta_audio_lang':     self.meta_audio_lang.get(),
            'meta_sub_lang':       self.meta_sub_lang.get(),
            'edition_tag':         self.edition_tag.get(),
            'edition_in_filename': self.edition_in_filename.get(),
            'add_chapters':        self.add_chapters.get(),
            'chapter_interval':    self.chapter_interval.get(),
        }

        renamed_candidates = []  # (output_path, original_input_path) for files whose originals were deleted

        for i, file_info in enumerate(self.files):
            if not self.is_converting:
                break

            self.current_file_index = i
            input_path = file_info['path']
            base_name = Path(input_path).stem

            # Merge global settings with per-file overrides
            ov = file_info.get('overrides', {})
            if ov:
                self.add_log(f"Using overrides for: {file_info['name']}", 'INFO')

            def _ov(key):
                return ov.get(key, settings[key])

            # Handle backward-compat for overrides with 'gpu' encoder value
            ov_encoder = ov.get('encoder', settings['encoder'])
            if ov_encoder == 'gpu':
                ov_encoder = self._default_gpu

            file_settings = {
                'transcode_mode': ov.get('transcode_mode', settings['transcode_mode']),
                'encoder':        ov_encoder,
                'codec_info':     ov.get('codec_info',     settings['codec_info']),
                'codec_name':     ov.get('video_codec',    settings['codec_name']),
                'mode':           ov.get('quality_mode',   settings['mode']),
                'bitrate':        ov.get('bitrate',        settings['bitrate']),
                'crf':            int(ov.get('crf',        settings['crf'])),
                'preset':         ov.get('preset',         settings['preset']),
                'gpu_preset':     ov.get('preset',         settings['gpu_preset']),
                'audio_codec':    ov.get('audio_codec',    settings['audio_codec']),
                'audio_bitrate':  ov.get('audio_bitrate',  settings['audio_bitrate']),
                'hw_decode':         ov.get('hw_decode',      settings['hw_decode']),
                'two_pass':          ov.get('two_pass',        settings['two_pass']),
                'subtitle_settings': file_info.get('subtitle_settings', {}),
                'edited_subs':       file_info.get('edited_subs', {}),
                'external_subs':     file_info.get('external_subs', []),
                'strip_internal_subs': file_info.get('strip_internal_subs', self.strip_internal_subs.get()),
                'container':         ov.get('container', self.container_format.get()),
                'has_closed_captions': file_info.get('has_closed_captions', False),
                'extract_cc':          file_info.get('extract_cc', False),
                'strip_chapters':      ov.get('strip_chapters',      settings['strip_chapters']),
                'strip_metadata_tags': ov.get('strip_metadata_tags', settings['strip_metadata_tags']),
                'set_track_metadata':  ov.get('set_track_metadata',  settings['set_track_metadata']),
                'meta_video_lang':     ov.get('meta_video_lang',     settings['meta_video_lang']),
                'meta_audio_lang':     ov.get('meta_audio_lang',     settings['meta_audio_lang']),
                'meta_sub_lang':       ov.get('meta_sub_lang',       settings['meta_sub_lang']),
                'edition_tag':         ov.get('edition_tag',         settings['edition_tag']),
                'edition_in_filename': ov.get('edition_in_filename', settings['edition_in_filename']),
            }

            # Generate chapters if requested (auto-generate from duration)
            file_chapters = file_info.get('chapters', [])
            if not file_chapters:
                add_ch = ov.get('add_chapters', settings.get('add_chapters', False))
                ch_interval = ov.get('chapter_interval', settings.get('chapter_interval', 5))
                if add_ch and file_info.get('duration_secs'):
                    try:
                        from modules.chapters import generate_auto_chapters
                    except ImportError:
                        import importlib.util
                        _ch_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                 'modules', 'chapters.py')
                        _spec = importlib.util.spec_from_file_location('chapters', _ch_path)
                        _mod = importlib.util.module_from_spec(_spec)
                        _spec.loader.exec_module(_mod)
                        generate_auto_chapters = _mod.generate_auto_chapters
                    file_chapters = generate_auto_chapters(file_info['duration_secs'], ch_interval)
            file_settings['chapters'] = file_chapters

            transcode_mode = file_settings['transcode_mode']
            encoder        = file_settings['encoder']
            codec_info     = file_settings['codec_info']
            quality_mode   = file_settings['mode']
            preset         = file_settings['preset']
            audio_codec    = file_settings['audio_codec']
            audio_bitrate  = file_settings['audio_bitrate']
            skip_existing  = ov.get('skip_existing',    self.skip_existing.get())
            delete_orig    = ov.get('delete_originals', self.delete_originals.get())

            # Generate output filename
            container = ov.get('container', self.container_format.get())
            if transcode_mode == 'audio':
                if audio_codec == 'copy':
                    suffix = '-audio-copy'
                else:
                    suffix = f"-{audio_codec.upper()}_{audio_bitrate}"
                output_ext = container
            else:
                short = codec_info['short_name']
                if codec_info['cpu_encoder'] == 'copy':
                    suffix = '-video-copy'
                else:
                    gpu_short = ''
                    if encoder != 'cpu':
                        bk = GPU_BACKENDS.get(encoder)
                        gpu_short = bk['short'] if bk else 'GPU'

                    if quality_mode == 'crf':
                        suffix = f"-CRF{file_settings['crf']}"
                    else:
                        suffix = f"-{file_settings['bitrate']}"
                    if encoder != 'cpu':
                        suffix += f"-{gpu_short}_{short}_{preset}"
                    else:
                        suffix += f"-{short}_{preset}"

                if transcode_mode == 'both' and audio_codec != 'copy':
                    suffix += f"-{audio_codec.upper()}_{audio_bitrate}"
                output_ext = container

            out_dir = self.output_dir if self.output_dir else Path(input_path).parent
            # Edition tag in filename for Plex: {edition-Director's Cut}
            edition_part = ''
            edition = file_settings.get('edition_tag', '')
            if edition and file_settings.get('edition_in_filename', False):
                edition_part = ' {edition-' + edition + '}'
            output_path = str(out_dir / f"{base_name}{edition_part}{suffix}{output_ext}")

            # Check if output exists
            if skip_existing and os.path.exists(output_path):
                self.add_log(f"Skipping (exists): {file_info['name']}", 'WARNING')
                skipped += 1
                self.update_file_status(i, "⏭️ Skipped")
                continue

            # Update UI
            self.update_file_status(i, "⏳ Converting")
            self.root.after(0, lambda p=input_path: self.status_label.configure(
                text=f"Converting: {os.path.basename(p)}"
            ))

            # Convert — track timing for batch ETA
            import time as _time
            self._file_start_time = _time.monotonic()
            self.current_output_path = output_path
            success = self.converter.convert_file(input_path, output_path, file_settings)

            # ── GPU → CPU fallback ──
            # If GPU encoding failed, retry with CPU settings automatically
            if not success and file_settings.get('encoder', 'cpu') != 'cpu':
                gpu_encoder = file_settings['encoder']
                gpu_label = GPU_BACKENDS.get(gpu_encoder, {}).get('label', gpu_encoder)
                self.add_log(f"GPU encoding failed ({gpu_label}), retrying with CPU...",
                             'WARNING')
                # Clean up the failed output file
                if os.path.exists(output_path):
                    try:
                        os.unlink(output_path)
                    except OSError:
                        pass
                # Build CPU fallback settings
                cpu_settings = dict(file_settings)
                cpu_settings['encoder'] = 'cpu'
                cpu_settings['hw_decode'] = False
                # Map GPU preset to a reasonable CPU preset
                codec_name = cpu_settings.get('video_codec', 'H.265 / HEVC')
                cpu_codec_info = VIDEO_CODEC_MAP.get(codec_name, {})
                cpu_presets = cpu_codec_info.get('cpu_presets')
                if cpu_presets:
                    cpu_settings['preset'] = cpu_codec_info.get('cpu_preset_default', 'medium')
                # Update output filename to reflect CPU encoding
                cpu_short = cpu_codec_info.get('short_name', 'H265')
                cpu_preset = cpu_settings.get('preset', 'medium')
                old_suffix = Path(output_path).stem.split(base_name, 1)[-1]
                if quality_mode == 'crf':
                    new_suffix = f"-CRF{file_settings['crf']}-{cpu_short}_{cpu_preset}"
                else:
                    new_suffix = f"-{file_settings['bitrate']}-{cpu_short}_{cpu_preset}"
                output_path = str(out_dir / f"{base_name}{edition_part}{new_suffix}{output_ext}")
                self.current_output_path = output_path
                success = self.converter.convert_file(input_path, output_path, cpu_settings)
                if success:
                    self.add_log(f"CPU fallback succeeded: {os.path.basename(output_path)}",
                                 'SUCCESS')

            file_wall_secs = _time.monotonic() - self._file_start_time
            self._file_start_time = None
            self.current_output_path = None

            # Record speed sample for batch ETA (only for successfully encoded files)
            file_dur = file_info.get('duration_secs') or 0
            if file_dur > 0 and file_wall_secs > 1:
                self._batch_speed_samples.append((file_dur, file_wall_secs))

            if success:
                # Verify output file if enabled
                if self.verify_output.get():
                    self.add_log(f"Verifying: {os.path.basename(output_path)}", 'INFO')
                    ok, issues = verify_output_file(output_path, input_path)
                    if ok:
                        self.add_log(f"Verification passed: {os.path.basename(output_path)}", 'SUCCESS')
                        if issues:  # warnings only
                            for w in issues:
                                self.add_log(f"  ⚠ {w}", 'WARNING')
                    else:
                        self.add_log(f"Verification FAILED: {os.path.basename(output_path)}", 'ERROR')
                        for issue in issues:
                            self.add_log(f"  • {issue}", 'ERROR')
                        failed += 1
                        self.update_file_status(i, '⚠️ Verify Failed')
                        continue

                completed += 1
                # Store output path on file_info for "Play Output File"
                file_info['output_path'] = output_path
                # Show actual output file size
                try:
                    actual_size = format_size(Path(output_path).stat().st_size)
                    self.update_file_status(i, f'✅ {actual_size}')
                except Exception:
                    self.update_file_status(i, '✅ Done')

                if delete_orig:
                    try:
                        os.remove(input_path)
                        self.add_log(f"Deleted original: {file_info['name']}", 'INFO')
                        renamed_candidates.append((output_path, input_path))
                    except Exception as e:
                        self.add_log(f"Failed to delete original: {e}", 'ERROR')
            else:
                failed += 1
                self.update_file_status(i, '❌ Failed')
            
            # Update overall progress
            total = len(self.files)
            processed = completed + failed + skipped
            percent = (processed / total) * 100 if total > 0 else 0
            self.root.after(0, lambda p=processed, t=total, pc=percent: (
                self.progress_var.set(pc),
                self.progress_label.configure(text=f"{p} / {t} files ({pc:.0f}%)")
            ))
        
        # Conversion complete
        elapsed = datetime.now() - self.start_time
        self.is_converting = False
        self.root.after(0, lambda: (
            self.pause_btn.configure(state='disabled'),
            self.stop_btn.configure(state='disabled'),
            self.status_label.configure(text=f"Complete! {completed} converted, {failed} failed, {skipped} skipped"),
            self.time_label.configure(text=f"Elapsed: {format_time(elapsed.total_seconds())}"),
            self.fps_label.configure(text=""),
            self.eta_label.configure(text=""),
            self.batch_eta_label.configure(text="")
        ))
        
        self.add_log("=" * 50, 'INFO')
        self.add_log(f"Conversion complete!", 'SUCCESS')
        self.add_log(f"Completed: {completed}", 'INFO')
        self.add_log(f"Failed: {failed}", 'INFO')
        self.add_log(f"Skipped: {skipped}", 'INFO')
        self.add_log(f"Time elapsed: {format_time(elapsed.total_seconds())}", 'INFO')
        self.add_log("=" * 50, 'INFO')

        # Play notification sound
        self.play_notification_sound()

        # Show completion dialog
        self.root.after(0, lambda: messagebox.showinfo(
            "Conversion Complete",
            f"Completed: {completed}\nFailed: {failed}\nSkipped: {skipped}\n\n"
            f"Time: {format_time(elapsed.total_seconds())}"
        ))

        # Offer to rename encoded files back to original names (only if originals were deleted)
        if renamed_candidates:
            def _ask_rename():
                answer = messagebox.askyesno(
                    "Rename Encoded Files",
                    f"{len(renamed_candidates)} original file(s) were deleted.\n\n"
                    "Would you like to rename the encoded files back to\n"
                    "their original file names?"
                )
                if answer:
                    renamed = 0
                    for output_path, original_input_path in renamed_candidates:
                        try:
                            original_stem = Path(original_input_path).stem
                            output_p = Path(output_path)
                            new_path = output_p.parent / f"{original_stem}{output_p.suffix}"
                            if new_path.exists():
                                self.add_log(
                                    f"Cannot rename, file already exists: {new_path.name}",
                                    'WARNING'
                                )
                                continue
                            os.rename(output_path, str(new_path))
                            self.add_log(
                                f"Renamed: {output_p.name} → {new_path.name}", 'INFO'
                            )
                            renamed += 1
                        except Exception as e:
                            self.add_log(f"Failed to rename {Path(output_path).name}: {e}", 'ERROR')
                    self.add_log(f"Renamed {renamed} of {len(renamed_candidates)} file(s)", 'SUCCESS')
            self.root.after(500, _ask_rename)
    
    def update_file_status(self, index, status):
        """Update status of a file in the tree"""
        def _update():
            item = self.file_tree.get_children()[index]
            values = list(self.file_tree.item(item, 'values'))
            values[4] = status  # status is column index 4
            self.file_tree.item(item, values=values)
        self.root.after(0, _update)
    
    def toggle_pause(self):
        """Toggle pause state"""
        if self.converter.is_paused:
            self.converter.resume()
            self.pause_btn.configure(text="⏸️ Pause")
        else:
            self.converter.pause()
            self.pause_btn.configure(text="▶️ Resume")
    
    def stop_conversion(self):
        """Stop conversion"""
        if messagebox.askyesno("Stop Conversion",
                               "Are you sure you want to stop the conversion?"):
            # Snapshot the output path before stopping (background thread clears it)
            partial_file = self.current_output_path
            self.is_converting = False
            self.converter.stop()
            self.pause_btn.configure(state='disabled')
            self.stop_btn.configure(state='disabled')
            self.status_label.configure(text="Stopped by user")
            self.fps_label.configure(text="")
            self.eta_label.configure(text="")

            # Offer to delete the incomplete output file
            def check_partial():
                if partial_file and os.path.exists(partial_file):
                    if messagebox.askyesno(
                        "Delete Incomplete File",
                        f"An incomplete output file was left on disk:\n\n"
                        f"{os.path.basename(partial_file)}\n\n"
                        f"Delete it?"
                    ):
                        try:
                            os.remove(partial_file)
                            self.add_log(f"Deleted incomplete file: {os.path.basename(partial_file)}", 'INFO')
                        except Exception as e:
                            self.add_log(f"Failed to delete incomplete file: {e}", 'ERROR')
                    else:
                        self.add_log(f"Incomplete file kept: {os.path.basename(partial_file)}", 'WARNING')

            # Delay slightly to let ffmpeg finish terminating and flush the file
            self.root.after(1500, check_partial)

# ============================================================================
# Main Entry Point
# ============================================================================

def _configure_dpi_scaling(root):
    """Configure Tk scaling for high-DPI displays.

    Tkinter on Linux defaults to 96 DPI and ignores the desktop
    environment's scaling settings.  This function detects the real
    DPI (via the X server, GDK, or environment variables) and tells
    Tk to scale all widgets, fonts, and geometry accordingly.

    Call once, immediately after creating the root Tk window and
    before building any widgets.
    """
    try:
        # Method 1: Xft.dpi from X resources (set by most DEs)
        real_dpi = None
        try:
            xrdb = subprocess.check_output(
                ['xrdb', '-query'], stderr=subprocess.DEVNULL, timeout=2
            ).decode('utf-8', errors='replace')
            for line in xrdb.splitlines():
                if 'Xft.dpi' in line:
                    real_dpi = float(line.split(':')[-1].strip())
                    break
        except Exception:
            pass

        # Method 2: GDK_SCALE environment variable (GNOME/GTK)
        if real_dpi is None:
            gdk_scale = os.environ.get('GDK_SCALE')
            if gdk_scale:
                try:
                    real_dpi = 96.0 * float(gdk_scale)
                except (ValueError, TypeError):
                    pass

        # Method 3: QT_SCALE_FACTOR (KDE/Qt)
        if real_dpi is None:
            qt_scale = os.environ.get('QT_SCALE_FACTOR')
            if qt_scale:
                try:
                    real_dpi = 96.0 * float(qt_scale)
                except (ValueError, TypeError):
                    pass

        if real_dpi and real_dpi > 96:
            # Tk scaling factor: 1.0 = 72 DPI (Tk's internal unit)
            # Default Tk scaling on 96 DPI display = 96/72 = 1.333...
            # For a 192 DPI display we want 192/72 = 2.666...
            factor = real_dpi / 72.0
            root.tk.call('tk', 'scaling', factor)
    except Exception:
        pass  # never break app startup over scaling


def main():
    """Main entry point"""
    # ── Info flags (print and exit) ──
    if '--version' in sys.argv or '-v' in sys.argv:
        print(f"{APP_NAME} v{APP_VERSION}")
        sys.exit(0)
    if '--which' in sys.argv:
        print(os.path.realpath(__file__))
        sys.exit(0)

    global GPU_TEST_MODE
    if '--gpu-test-mode' in sys.argv:
        GPU_TEST_MODE = True
        sys.argv.remove('--gpu-test-mode')
        print("*** GPU TEST MODE ENABLED — skipping GPU test encodes, detection only ***")

    root = TkinterDnD.Tk() if HAS_DND else tk.Tk()

    # Apply high-DPI scaling before any widgets are created
    _configure_dpi_scaling(root)

    # Hide window until fully built and positioned — prevents flicker/wrong-monitor flash
    root.withdraw()

    # Hide dotfiles in Tk file dialogs by default
    try:
        root.tk.call('catch', 'tk_getOpenFile foo bar')
        root.tk.call('set', '::tk::dialog::file::showHiddenVar', '0')
        root.tk.call('set', '::tk::dialog::file::showHiddenBtn', '1')
    except Exception:
        pass

    # Set theme
    try:
        style = ttk.Style()
        if 'clam' in style.theme_names():
            style.theme_use('clam')
    except Exception:
        pass

    # Create application
    app = VideoConverterApp(root)

    # Center on the monitor that contains the mouse pointer
    root.update_idletasks()
    width  = root.winfo_width()
    height = root.winfo_height()
    # winfo_pointerx/y gives the current mouse position — always on the active monitor
    ptr_x  = root.winfo_pointerx()
    ptr_y  = root.winfo_pointery()
    x = ptr_x - (width  // 2)
    y = ptr_y - (height // 2)
    # Clamp so the window never goes off-screen
    x = max(0, x)
    y = max(0, y)
    root.geometry(f'{width}x{height}+{x}+{y}')

    # Now show it — single, clean appearance on the right monitor
    root.deiconify()

    # Add files passed as command-line arguments (e.g. from "Open with" in file manager)
    if len(sys.argv) > 1:
        added = 0
        for arg in sys.argv[1:]:
            p = Path(arg)
            if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS:
                added += app._add_file_to_list(str(p))
            elif p.is_dir():
                # If a directory is passed, set it as working dir and scan
                app.working_dir = p
                app.refresh_file_list()
                break
        if added:
            app.add_log(f"Added {added} file(s) from command line", 'INFO')

    # Run
    root.mainloop()

if __name__ == '__main__':
    main()
