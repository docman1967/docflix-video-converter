#!/usr/bin/env python3
"""
Docflix Media Suite - Standalone GUI Application
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

APP_NAME = "Docflix Media Suite"
APP_VERSION = "2.9.3"
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


def _probe_video_bitrate(filepath, duration):
    """Probe the source video stream bitrate in bits/sec.

    Tries ffprobe's stream-level bit_rate first, then falls back to
    estimating from total file size minus a rough audio allowance.
    """
    try:
        cmd = [
            'ffprobe', '-v', 'error', '-select_streams', 'v:0',
            '-show_entries', 'stream=bit_rate',
            '-of', 'default=noprint_wrappers=1:nokey=1',
            filepath,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            val = result.stdout.strip()
            if val and val != 'N/A':
                return float(val)
    except Exception:
        pass
    # Fallback: estimate from file size (subtract ~256kbps for audio)
    try:
        src_size = Path(filepath).stat().st_size
        total_bps = (src_size * 8) / duration
        return max(0, total_bps - 256_000)
    except Exception:
        return 0


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
                video_bps = _probe_video_bitrate(filepath, duration)
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
        elif transcode_mode == 'audio':
            # Audio-only mode: video is copied through, include source video bitrate
            video_bps = _probe_video_bitrate(filepath, duration)

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
                    try:
                        self.current_process.kill()
                    except OSError:
                        pass
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
        """Stop conversion — kill immediately (SIGKILL).
        SIGTERM is unreliable with ffmpeg, and wait() can deadlock when
        the conversion thread is reading stdout. SIGKILL is immediate
        and the broken pipe exits the read loop in _run_process."""
        self.is_stopped = True
        if self.current_process:
            try:
                self.current_process.kill()
            except OSError:
                pass

# ============================================================================
# Main Application Class
# ============================================================================

class VideoConverterApp:
    """Main GUI Application"""
    
    def __init__(self, root):
        self.root = root
        self.root.title(f"{APP_NAME} v{APP_VERSION}")
        self.root.geometry("1200x800")
        self.root.minsize(800, 500)
        
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
        main_frame.rowconfigure(1, weight=1)  # paned window is the expanding row

        # Header
        self.setup_header(main_frame)

        # PanedWindow for settings + file list (user can drag the divider)
        self.main_paned = ttk.PanedWindow(main_frame, orient='vertical')
        self.main_paned.grid(row=1, column=0, sticky="nsew", pady=(0, 5))

        # Settings panel (top pane)
        self.setup_settings(self.main_paned)
        self.main_paned.add(self.settings_frame, weight=0)

        # File list (bottom pane)
        self.setup_file_list(self.main_paned)
        self.main_paned.add(self.file_list_frame, weight=1)

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
                               command=self.show_enhanced_media_info)
        tools_menu.add_command(label="Test Encode (30s)...",
                               accelerator="Ctrl+T",
                               command=self.test_encode)
        tools_menu.add_separator()
        tools_menu.add_command(label="Open Output Folder",
                               accelerator="Ctrl+Shift+F",
                               command=self.open_output_folder)
        tools_menu.add_separator()
        tools_menu.add_command(label="Docflix Subtitle Editor...",
                               command=self.open_standalone_subtitle_editor)
        tools_menu.add_command(label="Batch Filter Subtitles...",
                               command=self.open_batch_filter)
        tools_menu.add_separator()
        tools_menu.add_command(label="Docflix Media Processor...",
                               accelerator="Ctrl+M",
                               command=self.open_media_processor)
        tools_menu.add_command(label="Docflix Media Renamer...",
                               command=self.open_tv_renamer)
        tools_menu.add_command(label="Docflix Media Rescale...",
                               accelerator="Ctrl+Shift+R",
                               command=self.open_video_scaler)
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
        self.root.bind('<Control-i>', lambda e: self.show_enhanced_media_info())
        self.root.bind('<Control-t>', lambda e: self.test_encode())
        self.root.bind('<Control-F>', lambda e: self.open_output_folder())
        self.root.bind('<Control-m>', lambda e: self.open_media_processor())
        self.root.bind('<Control-R>', lambda e: self.open_video_scaler())

    def open_files(self):
        """Open a file picker and add selected video files to the queue."""
        filetypes = [
            ("Video files", " ".join(f"*{e}" for e in sorted(VIDEO_EXTENSIONS))),
            ("All files", "*.*")
        ]
        paths = self._ask_open_files(
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
        """Setup settings panel with scrollable canvas for small screens"""
        self.settings_frame = ttk.LabelFrame(parent, text="Settings", padding=(10, 5))
        self.settings_frame.columnconfigure(0, weight=1)
        self.settings_frame.rowconfigure(0, weight=1)

        # Scrollable canvas inside the LabelFrame
        self._settings_canvas = tk.Canvas(self.settings_frame, highlightthickness=0,
                                          borderwidth=0)
        self._settings_scrollbar = ttk.Scrollbar(self.settings_frame, orient='vertical',
                                                  command=self._settings_canvas.yview)
        self._settings_canvas.configure(yscrollcommand=self._settings_scrollbar.set)

        # Inner frame holds all settings widgets
        settings_frame = ttk.Frame(self._settings_canvas)
        self._settings_inner = settings_frame
        self._settings_canvas_win = self._settings_canvas.create_window(
            (0, 0), window=settings_frame, anchor='nw')

        self._settings_canvas.grid(row=0, column=0, sticky='nsew')
        # Scrollbar starts hidden — shown only when content overflows
        self._settings_scrollbar.grid(row=0, column=1, sticky='ns')
        self._settings_scrollbar.grid_remove()

        # Keep canvas window width in sync with canvas width
        def _on_canvas_configure(event):
            self._settings_canvas.itemconfigure(self._settings_canvas_win, width=event.width)
        self._settings_canvas.bind('<Configure>', _on_canvas_configure)

        # Update scrollregion and show/hide scrollbar when inner frame resizes
        def _on_inner_configure(event):
            self._settings_canvas.configure(scrollregion=self._settings_canvas.bbox('all'))
            # Show scrollbar only when content is taller than visible area
            canvas_h = self._settings_canvas.winfo_height()
            inner_h = settings_frame.winfo_reqheight()
            if inner_h > canvas_h > 1:
                self._settings_scrollbar.grid()
            else:
                self._settings_scrollbar.grid_remove()
        settings_frame.bind('<Configure>', _on_inner_configure)

        # Mouse wheel scrolling
        def _on_mousewheel(event):
            if self._settings_canvas.winfo_height() < settings_frame.winfo_reqheight():
                self._settings_canvas.yview_scroll(int(-1 * (event.delta or event.num)), 'units')
        def _on_wheel_up(event):
            if self._settings_canvas.winfo_height() < settings_frame.winfo_reqheight():
                self._settings_canvas.yview_scroll(-3, 'units')
        def _on_wheel_down(event):
            if self._settings_canvas.winfo_height() < settings_frame.winfo_reqheight():
                self._settings_canvas.yview_scroll(3, 'units')

        # Bind mousewheel to canvas and all descendants
        def _bind_wheel(widget):
            widget.bind('<Button-4>', _on_wheel_up, add='+')
            widget.bind('<Button-5>', _on_wheel_down, add='+')
            widget.bind('<MouseWheel>', _on_mousewheel, add='+')
        _bind_wheel(self._settings_canvas)
        _bind_wheel(settings_frame)
        self._settings_bind_wheel = _bind_wheel

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

        # Bind mousewheel to all child widgets inside the scrollable settings
        def _bind_wheel_recursive(widget):
            self._settings_bind_wheel(widget)
            for child in widget.winfo_children():
                _bind_wheel_recursive(child)
        _bind_wheel_recursive(settings_frame)


    def _edition_custom_var(self):
        """Return the StringVar for the custom edition entry."""
        if not hasattr(self, '_edition_custom_sv'):
            self._edition_custom_sv = tk.StringVar(value='')
        return self._edition_custom_sv

    def setup_file_list(self, parent):
        """Setup file list section"""
        file_frame = ttk.LabelFrame(parent, text="Video Files", padding=10)
        self.file_list_frame = file_frame
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
        self.file_tree = ttk.Treeview(file_frame, columns=columns, show='headings', height=8,
                                          selectmode='extended')
        self.file_tree.grid(row=1, column=0, sticky="nsew")

        # Scale row height for high-DPI / fractional scaling displays
        try:
            import tkinter.font as tkfont
            default_font = tkfont.nametofont('TkDefaultFont')
            font_height = default_font.metrics('linespace')
            row_height = max(24, font_height + 8)
            style = ttk.Style()
            style.configure('Treeview', rowheight=row_height)
        except Exception:
            pass

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

        # ── Shift+Arrow multi-select ──
        def _shift_arrow(evt, direction):
            items = self.file_tree.get_children()
            if not items:
                return 'break'
            focus = self.file_tree.focus()
            if not focus:
                return 'break'
            idx = list(items).index(focus)
            new_idx = idx + direction
            if new_idx < 0 or new_idx >= len(items):
                return 'break'
            new_item = items[new_idx]
            self.file_tree.focus(new_item)
            self.file_tree.see(new_item)
            self.file_tree.selection_add(new_item)
            return 'break'

        self.file_tree.bind('<Shift-Up>',   lambda e: _shift_arrow(e, -1))
        self.file_tree.bind('<Shift-Down>', lambda e: _shift_arrow(e, 1))

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
            paths = self._ask_open_files(
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
        """Open the subtitle editor as a standalone window."""
        try:
            from modules.subtitle_editor import open_standalone_subtitle_editor
            open_standalone_subtitle_editor(self)
        except ImportError:
            import importlib.util
            _se_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     'modules', 'subtitle_editor.py')
            if os.path.exists(_se_path):
                spec = importlib.util.spec_from_file_location('subtitle_editor', _se_path)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                mod.open_standalone_subtitle_editor(self)
            else:
                messagebox.showerror("Docflix Subtitle Editor", "modules/subtitle_editor.py not found.")

    def open_media_processor(self):
        """Open the Media Processor window."""
        try:
            from modules.media_processor import open_media_processor
            open_media_processor(self)
        except ImportError:
            import importlib.util
            _mp_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     'modules', 'media_processor.py')
            if os.path.exists(_mp_path):
                spec = importlib.util.spec_from_file_location('media_processor', _mp_path)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                mod.open_media_processor(self)
            else:
                messagebox.showerror("Docflix Media Processor", "modules/media_processor.py not found.")

    # ── File Renamer ────────────────────────────────────────────────────
    def open_video_scaler(self):
        """Open the Docflix Media Rescale tool."""
        try:
            from modules.video_scaler import open_video_scaler
            open_video_scaler(self)
        except ImportError:
            import importlib.util
            _vs_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     'modules', 'video_scaler.py')
            if os.path.exists(_vs_path):
                spec = importlib.util.spec_from_file_location('video_scaler', _vs_path)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                mod.open_video_scaler(self)
            else:
                messagebox.showerror("Docflix Media Rescale", "modules/video_scaler.py not found.")

    def open_tv_renamer(self):
        """Open the File Renamer tool."""
        try:
            from modules.tv_renamer import open_tv_renamer
            open_tv_renamer(self)
        except ImportError:
            import importlib.util
            _tr_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     'modules', 'tv_renamer.py')
            if os.path.exists(_tr_path):
                spec = importlib.util.spec_from_file_location('tv_renamer', _tr_path)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                mod.open_tv_renamer(self)
            else:
                messagebox.showerror("Docflix Media Renamer", "modules/tv_renamer.py not found.")

    def open_batch_filter(self):
        """Open the Batch Filter window."""
        try:
            from modules.batch_filter import open_batch_filter
            open_batch_filter(self)
        except ImportError:
            import importlib.util
            _bf_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     'modules', 'batch_filter.py')
            if os.path.exists(_bf_path):
                spec = importlib.util.spec_from_file_location('batch_filter', _bf_path)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                mod.open_batch_filter(self)
            else:
                messagebox.showerror("Batch Filter", "modules/batch_filter.py not found.")


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
        """Show subtitle text editor for a subtitle stream or external file."""
        try:
            from modules.subtitle_editor import show_subtitle_editor
            show_subtitle_editor(self, filepath, stream_index, file_info,
                                  external_sub_path=external_sub_path)
        except ImportError:
            import importlib.util
            _se_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     'modules', 'subtitle_editor.py')
            if os.path.exists(_se_path):
                spec = importlib.util.spec_from_file_location('subtitle_editor', _se_path)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                mod.show_subtitle_editor(self, filepath, stream_index, file_info,
                                          external_sub_path=external_sub_path)
            else:
                messagebox.showerror("Docflix Subtitle Editor", "modules/subtitle_editor.py not found.")

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
        panes = self.main_paned.panes()
        if str(self.settings_frame) in panes:
            self.main_paned.forget(self.settings_frame)
        else:
            self.main_paned.insert(0, self.settings_frame, weight=0)

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
                       self._ask_open_file(title="Select Video Player Executable",
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
            # Clear the reset override so save_preferences uses working_dir
            self._default_video_folder = vf

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
            'default_video_folder':  getattr(self, '_default_video_folder', str(self.working_dir)),
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
            'tv_rename_provider':    getattr(self, '_tv_rename_provider', 'TVDB'),
            'tv_rename_template':    getattr(self, '_tv_rename_template', '{show} S{season}E{episode} {title}'),
            'movie_rename_template': getattr(self, '_movie_rename_template', '{show} ({year})'),
            'custom_rename_templates': getattr(self, '_custom_rename_templates', []),
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
            self._tv_rename_provider = prefs.get('tv_rename_provider', 'TVDB')
            self._tv_rename_template = prefs.get('tv_rename_template',
                                                  '{show} S{season}E{episode} {title}')
            self._movie_rename_template = prefs.get('movie_rename_template',
                                                     '{show} ({year})')
            self._custom_rename_templates = prefs.get(
                'custom_rename_templates', [])
            # Media Processor
            self._media_proc_prefs = prefs.get('media_processor', {})
            self._scaler_prefs = prefs.get('video_scaler', {})
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
        self.delete_originals.set(False)
        self.hw_decode.set(self.has_gpu)
        self.two_pass.set(False)
        self.verify_output.set(True)
        self.notify_sound.set(False)
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
        self.default_player.set('auto')
        # Default Settings dialog values — clear folder defaults
        self.working_dir = Path.home()
        self._default_video_folder = ''   # blank = no saved default
        self.output_dir = None
        self.output_dir_label.configure(text="Same as source file", foreground='gray')
        self._on_metadata_toggle()
        # Refresh UI state
        self.on_encoder_change(silent=True)
        self.on_video_codec_change()
        self.on_transcode_mode_change()
        self.on_quality_mode_change()
        # Persist reset values so Default Settings dialog picks them up
        self.save_preferences()
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
                ("Ctrl+I",         "Media Details (Enhanced)"),
                ("Ctrl+T",         "Test Encode (30s)"),
                ("Ctrl+Shift+F",   "Open Output Folder"),
                ("Ctrl+M",         "Docflix Media Processor"),
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
        status_frame.grid(row=2, column=0, sticky="ew")

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

        Dynamically scans self.files by status so the ETA updates correctly
        when files are added or removed during conversion.

        Returns seconds remaining, or None if not enough data.
        """
        # Count pending files (not yet completed/skipped/failed)
        pending_count = sum(1 for f in self.files
                          if f.get('status') in ('Pending', None, '⏳ Converting'))
        if pending_count <= 0:
            return None

        # Calculate average speed from completed files
        if self._batch_speed_samples:
            total_vid = sum(d for d, _ in self._batch_speed_samples)
            total_wall = sum(w for _, w in self._batch_speed_samples)
            avg_speed = total_vid / total_wall if total_wall > 0 else None
        else:
            avg_speed = None

        # Sum durations of files still pending (not yet started)
        remaining_duration = 0.0
        for fi in self.files:
            if fi.get('status') in ('Pending', None):
                remaining_duration += fi.get('duration_secs', 0) or 0

        # Estimate time for remaining files
        if avg_speed and avg_speed > 0:
            remaining_files_eta = remaining_duration / avg_speed
        elif self._file_start_time is not None:
            # No completed files yet — use current file's progress to estimate speed
            import time as _time
            wall_so_far = _time.monotonic() - self._file_start_time
            # Find the currently converting file by status
            cur_dur = 0
            for fi in self.files:
                if fi.get('status') == '⏳ Converting':
                    cur_dur = fi.get('duration_secs', 0) or 0
                    break
            if wall_so_far > 2 and cur_dur > 0:
                # Estimate speed from current file progress
                cur_speed = cur_dur / (wall_so_far + (current_file_eta or 0))
                remaining_files_eta = remaining_duration / cur_speed if cur_speed > 0 else None
            else:
                return None
        else:
            return None

        # Add current file's remaining time
        batch_remaining = (current_file_eta or 0) + (remaining_files_eta or 0)
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
            # Batch ETA (show whenever there are pending files beyond the current one)
            if self.is_converting:
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

    def _ask_open_files(self, **kwargs):
        """Open a multi-file picker via zenity helpers, tk fallback."""
        try:
            from modules.utils import ask_open_files
            return ask_open_files(**kwargs)
        except ImportError:
            return list(filedialog.askopenfilenames(**kwargs) or [])

    def _ask_open_file(self, **kwargs):
        """Open a single-file picker via zenity helpers, tk fallback."""
        try:
            from modules.utils import ask_open_file
            return ask_open_file(**kwargs)
        except ImportError:
            return filedialog.askopenfilename(**kwargs) or ''

    def _ask_save_file(self, **kwargs):
        """Open a save-file dialog via zenity helpers, tk fallback."""
        try:
            from modules.utils import ask_save_file
            return ask_save_file(**kwargs)
        except ImportError:
            return filedialog.asksaveasfilename(**kwargs) or ''

    def _ask_directory(self, initialdir=None, title="Select Folder"):
        """Open a folder-selection dialog.

        Tries zenity first (GTK dialog with proper single-click + Open
        button behaviour), then falls back to tkinter's askdirectory.
        """
        if initialdir:
            initialdir = str(initialdir)
        if shutil.which('zenity'):
            env = os.environ.copy()
            env['GTK_USE_PORTAL'] = '0'
            env['GDK_BACKEND'] = 'x11'
            env['NO_AT_BRIDGE'] = '1'
            try:
                cmd = [
                    'zenity', '--file-selection', '--directory',
                    '--title', title,
                ]
                if initialdir:
                    cmd += ['--filename', initialdir + '/']
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=120,
                    env=env
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
        """Open folder browser dialog to select a video source directory."""
        folder = self._ask_directory(
            initialdir=self.working_dir,
            title="Select Video Folder"
        )
        if folder:
            self.working_dir = Path(folder)
            self._add_recent_folder(folder)
            self.refresh_files()
            self.add_log(f"Changed directory to: {folder}", 'INFO')
    
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

        # Audio controls: visible and enabled in 'both' mode, hidden in 'video' mode
        if show_audio:
            self.audio_frame.grid(row=6)
            self.audio_codec_combo.configure(state='readonly')
            self.audio_bitrate_combo.configure(state='readonly')
            self.check_frame.grid(row=7)
        else:
            self.audio_frame.grid_remove()
            self.check_frame.grid(row=6)
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
            # Use the correct preset for the active encoder
            if encoder != 'cpu':
                preset = file_settings.get('gpu_preset', file_settings['preset'])
            else:
                preset = file_settings['preset']
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
            # When Plex edition is active, use clean filename without encoding suffix
            edition = file_settings.get('edition_tag', '')
            if edition and file_settings.get('edition_in_filename', False):
                edition_part = ' {edition-' + edition + '}'
                output_path = str(out_dir / f"{base_name}{edition_part}{output_ext}")
            else:
                output_path = str(out_dir / f"{base_name}{suffix}{output_ext}")

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
            # If GPU encoding failed (but NOT stopped by user), retry with CPU
            if (not success and not self.converter.is_stopped
                    and file_settings.get('encoder', 'cpu') != 'cpu'):
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
                if edition and file_settings.get('edition_in_filename', False):
                    output_path = str(out_dir / f"{base_name} {{edition-{edition}}}{output_ext}")
                else:
                    output_path = str(out_dir / f"{base_name}{new_suffix}{output_ext}")
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
        """Update status of a file in both the tree and the files list."""
        # Update the data model so batch ETA can scan by status
        if 0 <= index < len(self.files):
            self.files[index]['status'] = status
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

    # Set readable font sizes for all Tk named fonts — affects ALL dialogs
    # including file pickers, message boxes, etc.
    try:
        import tkinter.font as tkfont
        for font_name in ('TkDefaultFont', 'TkTextFont', 'TkMenuFont',
                          'TkHeadingFont', 'TkCaptionFont', 'TkSmallCaptionFont',
                          'TkIconFont', 'TkTooltipFont', 'TkFixedFont'):
            try:
                f = tkfont.nametofont(font_name)
                current_size = f.actual()['size']
                if abs(current_size) < 10:
                    f.configure(size=11)
            except Exception:
                pass
    except Exception:
        pass

    # Scale ttk checkbox / radiobutton indicators to match font size
    try:
        from modules.utils import _scale_check_radio_indicators
        _scale_check_radio_indicators(root)
    except Exception:
        pass


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

    root = TkinterDnD.Tk(className='docflix') if HAS_DND else tk.Tk(className='docflix')

    # Apply high-DPI scaling before any widgets are created
    _configure_dpi_scaling(root)

    # Set taskbar/dock icon and name so it shows "Docflix" instead of "Tk"
    try:
        from PIL import Image, ImageTk
        _icon_path = Path(__file__).parent / 'logo_transparent.png'
        if not _icon_path.exists():
            _icon_path = Path(__file__).parent / 'logo.png'
        if _icon_path.exists():
            _icon_img = Image.open(_icon_path)
            _icon_photo = ImageTk.PhotoImage(_icon_img)
            root.iconphoto(True, _icon_photo)
            root._icon_ref = _icon_photo  # prevent garbage collection
    except Exception:
        pass

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

    # Center on the monitor that contains the mouse pointer, fitting to screen
    root.update_idletasks()
    ptr_x = root.winfo_pointerx()
    ptr_y = root.winfo_pointery()

    # Detect the actual monitor the mouse is on (xrandr gives per-monitor geometry)
    mon_x, mon_y, mon_w, mon_h = 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()
    try:
        import subprocess as _sp, re as _re
        _xr = _sp.run(['xrandr', '--query'], capture_output=True, text=True, timeout=3)
        for _m in _re.finditer(r'\bconnected\s+(?:primary\s+)?(\d+)x(\d+)\+(\d+)\+(\d+)', _xr.stdout):
            mw, mh, mx, my = int(_m.group(1)), int(_m.group(2)), int(_m.group(3)), int(_m.group(4))
            if mx <= ptr_x < mx + mw and my <= ptr_y < my + mh:
                mon_x, mon_y, mon_w, mon_h = mx, my, mw, mh
                break
    except Exception:
        pass

    # Reserve space for taskbar/panel and margin
    avail_w = mon_w - 40
    avail_h = mon_h - 80
    # Desired size — shrink to fit if monitor is smaller
    width = min(1200, avail_w)
    height = min(800, avail_h)
    root.geometry(f'{width}x{height}')
    root.update_idletasks()
    # Center on the detected monitor
    x = mon_x + (mon_w - width) // 2
    y = mon_y + (mon_h - height) // 2
    # Clamp so the window stays within the monitor bounds
    x = max(mon_x, min(x, mon_x + mon_w - width))
    y = max(mon_y, min(y, mon_y + mon_h - height - 60))
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
