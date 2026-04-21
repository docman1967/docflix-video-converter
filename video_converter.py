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
"""

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
APP_VERSION = "1.3.0"
DEFAULT_BITRATE = "2M"
DEFAULT_CRF = 23
DEFAULT_PRESET = "ultrafast"
DEFAULT_GPU_PRESET = "p4"

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
VIDEO_EXTENSIONS = {'.mkv', '.mp4', '.avi', '.mov', '.wmv', '.flv', '.webm'}

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
            streams.append({
                'index':      s.get('index', 0),
                'codec_name': s.get('codec_name', 'unknown'),
                'language':   tags.get('language', 'und'),
                'title':      tags.get('title', ''),
                'forced':     bool(disp.get('forced', 0)),
                'sdh':        bool(disp.get('hearing_impaired', 0)),
                'default':    bool(disp.get('default', 0)),
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
    speaker_pattern = re.compile(r'^(-?\s*)[A-Za-z][A-Za-z\s\d\'\.]{0,29}:\s*\n?', re.MULTILINE)

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
        if re.match(r'^\d+$', name_part):
            return m.group(0)
        if re.search(r'\d$', name_part):
            return m.group(0)
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
        # Clean up orphaned colons left after HI removal (e.g. "(gasps): " → ": ")
        text = re.sub(r'^\s*:\s*', '', text, flags=re.MULTILINE)
        text = re.sub(r'\n\s*:\s*', '\n', text)
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

    def fix_case(text):
        # Only process lines that are mostly uppercase
        alpha = re.sub(r'[^a-zA-Z]', '', text)
        if not alpha:
            return text
        upper_ratio = sum(1 for c in alpha if c.isupper()) / len(alpha)
        if upper_ratio < 0.6:
            return text  # not all-caps, leave it alone

        # Step 1: lowercase everything
        text = text.lower()

        # Step 2: capitalize first letter of each line
        lines = text.split('\n')
        capped_lines = []
        for line in lines:
            line = line.strip()
            if line:
                # Capitalize after leading dash/hyphen
                line = re.sub(r'^(-\s*)', lambda m: m.group(1), line)
                # Capitalize first real letter
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

    result = []
    for cue in cues:
        result.append({**cue, 'text': fix_case(cue['text'])})
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
    pattern = re.compile(r'^(-?\s*)[A-Za-z][A-Za-z\s\d\'\.]{0,29}:\s*\n?', re.MULTILINE)
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
    """
    # Pick the first available encoder from the backend
    test_encoder = None
    for enc in backend['detect_encoders']:
        test_encoder = enc
        break
    if not test_encoder:
        return False

    # Build a minimal test encode command using a synthetic input.
    # Each backend needs slightly different setup to initialize the hardware.
    cmd = ['ffmpeg', '-y', '-loglevel', 'error']

    if backend_id == 'vaapi':
        # VAAPI needs device init + hwupload to get frames onto the GPU
        cmd.extend([
            '-vaapi_device', '/dev/dri/renderD128',
            '-f', 'lavfi', '-i', 'color=black:s=64x64:d=0.1:r=1',
            '-vf', 'format=nv12,hwupload',
            '-c:v', test_encoder,
            '-frames:v', '1',
            '-f', 'null', '-'
        ])
    elif backend_id == 'qsv':
        # QSV: test without hwaccel flags (encode-only, no device-bound input)
        cmd.extend([
            '-f', 'lavfi', '-i', 'color=black:s=64x64:d=0.1:r=1',
            '-c:v', test_encoder,
            '-frames:v', '1',
            '-f', 'null', '-'
        ])
    else:
        # NVENC and others: straightforward test
        cmd.extend([
            '-f', 'lavfi', '-i', 'color=black:s=64x64:d=0.1:r=1',
            '-c:v', test_encoder,
            '-frames:v', '1',
            '-f', 'null', '-'
        ])

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        return result.returncode == 0
    except Exception:
        return False


def detect_gpu_backends():
    """Detect all available GPU encoding backends.

    Returns a dict: { backend_id: gpu_name_or_True, ... }
    Backends are included only if:
      1. Their key encoder is found in ``ffmpeg -encoders``
      2. A quick test encode succeeds (verifies driver/runtime is working)
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
            # Verify the encoder actually works with a quick test
            if _verify_gpu_encoder(bid, backend):
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
                'vaapi':  'AMD|ATI|Radeon',
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
                # AVI does not support embedded subtitle streams
                if container == '.avi':
                    c.extend(['-map', '0:v?', '-map', '0:a?'])
                    self.log("Subtitles skipped: AVI container does not support embedded subtitles", 'WARNING')
                    return

                sub_settings = settings.get('subtitle_settings', {})
                strip_internal = settings.get('strip_internal_subs', False)

                if not sub_settings and not embed_subs and not strip_internal and not edited_subs:
                    # Simple case: no per-file config, no external subs, no edits, keep internals
                    c.extend(['-map', '0:v?', '-map', '0:a?', '-map', '0:s?'])
                    if container in ('.mp4', '.mov'):
                        # MP4/MOV only support mov_text — convert text subs, drop bitmap subs
                        BITMAP_CODECS = {'hdmv_pgs_subtitle', 'dvd_subtitle', 'dvb_subtitle'}
                        try:
                            int_streams = get_subtitle_streams(input_path)
                        except Exception:
                            int_streams = []
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
                        c.extend(['-c:s', 'copy'])
                    return

                # We need explicit mapping when we have external subs, per-file config,
                # or are stripping internal tracks
                c.extend(['-map', '0:v?', '-map', '0:a?'])
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
                    else:
                        codec = es.get('format', 'srt')
                    c.extend([f'-c:s:{out_sub_idx}', codec])
                    # Language metadata
                    lang = es.get('language', 'und')
                    if lang and lang != 'und':
                        c.extend([f'-metadata:s:s:{out_sub_idx}', f'language={lang}'])
                    # Disposition flags (default / forced)
                    disp_parts = []
                    if es.get('default'):
                        disp_parts.append('default')
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
                cmd.extend(['-map', '0:v?', '-map', '0:a?'])
        except Exception as e:
            self.log(f"Conversion error: {str(e)}", "ERROR")
            return False
        finally:
            self.current_process = None

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
        tools_menu.add_command(label="Media Info...",
                               accelerator="Ctrl+I",
                               command=self.show_media_info)
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
        # Help menu
        help_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Help", menu=help_menu)

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
        self.root.bind('<Control-t>', lambda e: self.test_encode())
        self.root.bind('<Control-F>', lambda e: self.open_output_folder())

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
                                            values=['.mkv', '.mp4', '.webm', '.avi', '.mov'],
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
        
        # Hide audio frame initially (video mode is default)
        self.audio_frame.grid_remove()
        
        # Checkboxes - Row 5 (default, moves to row 6 when audio shown)
        self.check_frame = ttk.Frame(settings_frame)
        self.check_frame.grid(row=6, column=0, columnspan=2, sticky='w', pady=10)
        
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
        dw = dlg.winfo_reqwidth()
        dh = dlg.winfo_reqheight()
        x = rx + (rw - dw) // 2
        y = ry + (rh - dh) // 2
        dlg.geometry(f"+{x}+{y}")

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
            for c in range(1, 7):
                inner.columnconfigure(c, weight=0)

            if not subs:
                ttk.Label(inner, text="No external subtitles attached.\n\n"
                          "Use  ➕ Add Subtitle File  below,\n"
                          "or drag .srt / .ass / .vtt files onto the file queue.",
                          foreground='gray', justify='center').grid(row=0, column=0,
                          columnspan=6, padx=40, pady=30)
                return

            # ── Column headers ──
            hdr_font = ('Helvetica', 9, 'bold')
            pad = {'padx': 4, 'pady': (0, 6)}
            ttk.Label(inner, text="Filename",  font=hdr_font, anchor='w').grid(row=0, column=0, sticky='w', **pad)
            ttk.Label(inner, text="Language",   font=hdr_font, anchor='w').grid(row=0, column=1, sticky='e', **pad)
            ttk.Label(inner, text="Mode",       font=hdr_font, anchor='w').grid(row=0, column=2, sticky='e', **pad)
            ttk.Label(inner, text="Default",    font=hdr_font, anchor='center').grid(row=0, column=3, sticky='e', **pad)
            ttk.Label(inner, text="Forced",     font=hdr_font, anchor='center').grid(row=0, column=4, sticky='e', **pad)
            ttk.Label(inner, text="",           font=hdr_font).grid(row=0, column=5, **pad)
            ttk.Label(inner, text="",           font=hdr_font).grid(row=0, column=6, **pad)

            # Separator under headers
            ttk.Separator(inner, orient='horizontal').grid(
                row=1, column=0, columnspan=7, sticky='ew', pady=(0, 4))

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

                # Forced checkbox
                forced_var = tk.BooleanVar(value=sub.get('forced', False))
                ttk.Checkbutton(inner, variable=forced_var).grid(row=r, column=4, sticky='e', **rpad)
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
                               )).grid(row=r, column=5, sticky='e', **rpad)

                # Remove button
                ttk.Button(inner, text="✖", width=3,
                           command=lambda idx=i: _remove_sub(idx)).grid(row=r, column=6, sticky='e', **rpad)

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
        dlg.geometry("520x540")
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

        check_all_var = tk.BooleanVar(value=True)
        def on_check_all():
            for v in track_vars:
                v[0].set(check_all_var.get())
        tk.Checkbutton(top_bar, text="Check All", variable=check_all_var,
                       command=on_check_all, relief='flat', bd=0).pack(side='right')

        # ── Subtitle output format options ──
        SUB_FORMATS = ['copy', 'srt', 'ass', 'webvtt', 'ttml', 'extract only', 'drop']

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
            title_text = s['title']
            if flags: title_text += f"  [{', '.join(flags)}]"
            ttk.Label(list_frame, text=title_text, foreground='gray').grid(
                row=r, column=4, sticky='w', padx=4)

            # Convert To dropdown
            fmt_combo = ttk.Combobox(list_frame, textvariable=fmt_var,
                                     values=SUB_FORMATS, width=12, state='readonly')
            fmt_combo.grid(row=r, column=5, padx=4, pady=2)

            # Edit button (only for text-based subtitles)
            _BITMAP_SUB_CODECS_SET = {'hdmv_pgs_subtitle', 'dvd_subtitle', 'dvb_subtitle'}
            if s['codec_name'] not in _BITMAP_SUB_CODECS_SET:
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
                # Determine output extension
                ext_map = {'srt': '.srt', 'ass': '.ass', 'webvtt': '.vtt',
                           'ttml': '.ttml', 'extract only': '.srt'}
                out_ext = ext_map.get(fmt, '.srt')
                out_codec = fmt if fmt != 'extract only' else 'srt'
                lang = s['language']
                title_slug = s['title'].replace(' ', '_') if s['title'] else ''
                out_name = f"{Path(filepath).stem}.{lang}"
                if title_slug: out_name += f".{title_slug}"
                if s['forced']: out_name += ".forced"
                out_name += out_ext
                out_path = str(out_dir / out_name)
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
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                if result.returncode != 0:
                    messagebox.showerror("Error",
                        f"Failed to extract subtitle stream #{stream_index}:\n"
                        f"{result.stderr[-300:]}",
                        parent=editor)
                    os.unlink(tmp_srt.name)
                    return
            except Exception as e:
                messagebox.showerror("Error", f"Extract error:\n{e}", parent=editor)
                os.unlink(tmp_srt.name)
                return

            with open(tmp_srt.name, 'r', encoding='utf-8', errors='replace') as f:
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
                self.add_log(f"Opened video subtitle: stream #{stream_index} ({lang}) "
                             f"from {os.path.basename(video_path)} "
                             f"({len(cues)} entries)", 'INFO')
            else:
                os.unlink(tmp_srt.name)

        def do_open_file():
            path = filedialog.askopenfilename(
                parent=editor,
                title="Open Subtitle or Video File",
                filetypes=[
                    ('Subtitle files', '*.srt *.ass *.ssa *.vtt *.sub'),
                    ('Video files', '*.mkv *.mp4 *.avi *.mov *.wmv *.flv *.webm'),
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

        def undo_all():
            nonlocal cues
            push_undo()
            cues = [dict(c) for c in original_cues]
            refresh_tree(cues)
            self.add_log("Subtitle edits reset to original", 'INFO')

        filter_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Filters", menu=filter_menu)
        filter_menu.add_command(label="Remove HI  [brackets] (parens) Speaker:",
                                command=lambda: apply_filter(filter_remove_hi, "Remove HI"))
        filter_menu.add_command(label="Remove Tags  <i> {\\an8}",
                                command=lambda: apply_filter(filter_remove_tags, "Remove Tags"))

        def apply_remove_ads():
            apply_filter(lambda c: filter_remove_ads(c, self.custom_ad_patterns),
                         "Remove Ads")

        filter_menu.add_command(label="Remove Ads / Credits", command=apply_remove_ads)
        filter_menu.add_command(label="Remove Music Notes  ♪ ♫",
                                command=lambda: apply_filter(filter_remove_music_notes, "Remove Music Notes"))
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
        filter_menu.add_separator()

        # ── Fix ALL CAPS ──
        if not hasattr(self, 'custom_cap_words'):
            self.custom_cap_words = []

        def apply_fix_caps():
            apply_filter(lambda c: filter_fix_caps(c, self.custom_cap_words),
                         "Fix ALL CAPS")

        def show_fix_caps_dialog():
            cd = tk.Toplevel(editor)
            cd.title("Fix ALL CAPS — Custom Names")
            cd.geometry("420x380")
            cd.transient(editor)
            cd.grab_set()
            self._center_on_main(cd)
            cd.resizable(True, True)

            ttk.Label(cd, text="Add character names and other proper nouns\n"
                      "that should be capitalized after conversion.",
                      justify='center', padding=(10, 10)).pack()

            lf = ttk.LabelFrame(cd, text="Custom Names (in addition to built-in list)",
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

            def add_word():
                word = new_word_var.get().strip()
                if not word:
                    return
                if word.lower() not in [w.lower() for w in self.custom_cap_words]:
                    self.custom_cap_words.append(word)
                    word_list.insert('end', word)
                new_word_var.set('')

            def remove_word():
                sel = word_list.curselection()
                if sel:
                    self.custom_cap_words.pop(sel[0])
                    word_list.delete(sel[0])

            ttk.Button(add_frame, text="Add", command=add_word).pack(side='right')
            word_entry.bind('<Return>', lambda e: add_word())

            ttk.Label(lf, text="Tip: Add character names like 'John', 'Sarah', 'Dr. House'",
                      font=('Helvetica', 8), foreground='gray').pack(anchor='w')

            btn_frame = ttk.Frame(cd, padding=(10, 8, 10, 10))
            btn_frame.pack(fill='x')
            ttk.Button(btn_frame, text="Remove Selected", command=remove_word).pack(side='left')
            ttk.Button(btn_frame, text="Apply Fix Caps",
                       command=lambda: (cd.destroy(), apply_fix_caps())).pack(side='right')
            ttk.Button(btn_frame, text="Cancel", command=cd.destroy).pack(side='right', padx=4)

        filter_menu.add_command(label="Fix ALL CAPS", command=apply_fix_caps)
        filter_menu.add_command(label="Fix ALL CAPS + Add Names...",
                                command=show_fix_caps_dialog)
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

        timing_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Timing", menu=timing_menu)
        timing_menu.add_command(label="Offset / Stretch...", command=show_timing_dialog)

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
                tree.see(str(matches[0]))
                tree.selection_set(str(matches[0]))
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

    def open_batch_filter(self):
        """Open a batch filter window to apply filters to multiple subtitle files at once."""
        import tempfile

        win = tk.Toplevel(self.root)
        win.title("Batch Filter Subtitles")
        win.geometry("620x720")
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
            ('remove_music',   "Remove Music Notes  ♪ ♫",        filter_remove_music_notes),
            ('remove_dashes',  "Remove Leading Dashes  -",        filter_remove_leading_dashes),
            ('remove_caps_hi', "Remove ALL CAPS HI (UK style)",   filter_remove_caps_hi),
            ('remove_quotes',  "Remove Off-Screen Quotes ' ' (UK style)", filter_remove_offscreen_quotes),
            ('remove_dupes',   "Remove Duplicates",               filter_remove_duplicates),
            ('merge_short',    "Merge Short Cues",                filter_merge_short),
            ('fix_caps',       "Fix ALL CAPS",
             lambda c: filter_fix_caps(c, self.custom_cap_words)),
        ]

        filter_vars = {}
        for key, label, _ in filter_defs:
            var = tk.BooleanVar(value=False)
            filter_vars[key] = var
            ttk.Checkbutton(filters_frame, text=label, variable=var).pack(anchor='w')

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
            active_filters = [(label, func) for key, label, func in filter_defs
                              if filter_vars[key].get()]
            if not active_filters:
                messagebox.showwarning("No Filters",
                    "Select at least one filter to apply.", parent=win)
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
            result_label.configure(
                text=f"Done — {success} succeeded, {errors} failed",
                foreground='green' if errors == 0 else 'orange')
            self.add_log(f"Batch filter complete: {success}/{total} files processed. "
                         f"Filters: {filters_used}", 'SUCCESS')

        apply_btn = ttk.Button(action_frame, text="Apply Filters", command=do_batch_apply)
        apply_btn.pack(side='right', padx=(4, 0))
        ttk.Button(action_frame, text="Close", command=win.destroy).pack(side='right')

        win.wait_window()

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
                tree.see(str(matches[0]))
                tree.selection_set(str(matches[0]))
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
        menubar.add_cascade(label="Filters", menu=filter_menu)
        filter_menu.add_command(label="Remove HI  [brackets] (parens) Speaker:",
                                command=lambda: apply_filter(filter_remove_hi, "Remove HI"))
        filter_menu.add_command(label="Remove Tags  <i> {\\an8}",
                                command=lambda: apply_filter(filter_remove_tags, "Remove Tags"))
        def apply_remove_ads():
            apply_filter(lambda c: filter_remove_ads(c, self.custom_ad_patterns),
                         "Remove Ads")

        filter_menu.add_command(label="Remove Ads / Credits", command=apply_remove_ads)
        filter_menu.add_command(label="Remove Music Notes  ♪ ♫",
                                command=lambda: apply_filter(filter_remove_music_notes, "Remove Music Notes"))
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
        filter_menu.add_separator()

        # ── Fix ALL CAPS ──
        # Custom capitalize words stored on the app instance
        if not hasattr(self, 'custom_cap_words'):
            self.custom_cap_words = []

        def apply_fix_caps():
            apply_filter(lambda c: filter_fix_caps(c, self.custom_cap_words),
                         "Fix ALL CAPS")

        def show_fix_caps_dialog():
            """Apply Fix Caps with option to add custom names first."""
            cd = tk.Toplevel(editor)
            cd.title("Fix ALL CAPS — Custom Names")
            cd.geometry("420x380")
            cd.transient(editor)
            cd.grab_set()
            self._center_on_main(cd)
            cd.resizable(True, True)

            ttk.Label(cd, text="Add character names and other proper nouns\n"
                      "that should be capitalized after conversion.",
                      justify='center', padding=(10, 10)).pack()

            # Current custom words
            lf = ttk.LabelFrame(cd, text="Custom Names (in addition to built-in list)",
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

            def add_word():
                word = new_word_var.get().strip()
                if not word:
                    return
                if word.lower() not in [w.lower() for w in self.custom_cap_words]:
                    self.custom_cap_words.append(word)
                    word_list.insert('end', word)
                new_word_var.set('')

            def remove_word():
                sel = word_list.curselection()
                if sel:
                    self.custom_cap_words.pop(sel[0])
                    word_list.delete(sel[0])

            ttk.Button(add_frame, text="Add", command=add_word).pack(side='right')
            word_entry.bind('<Return>', lambda e: add_word())

            ttk.Label(lf, text="Tip: Add character names like 'John', 'Sarah', 'Dr. House'",
                      font=('Helvetica', 8), foreground='gray').pack(anchor='w')

            btn_frame = ttk.Frame(cd, padding=(10, 8, 10, 10))
            btn_frame.pack(fill='x')
            ttk.Button(btn_frame, text="Remove Selected", command=remove_word).pack(side='left')
            ttk.Button(btn_frame, text="Apply Fix Caps",
                       command=lambda: (cd.destroy(), apply_fix_caps())).pack(side='right')
            ttk.Button(btn_frame, text="Cancel", command=cd.destroy).pack(side='right', padx=4)

        filter_menu.add_command(label="Fix ALL CAPS", command=apply_fix_caps)
        filter_menu.add_command(label="Fix ALL CAPS + Add Names...",
                                command=show_fix_caps_dialog)
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
        # Parse them properly
        paths = []
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
        file_info = {
            'name': f,
            'path': str(filepath),
            'size': size,
            'duration_str': dur_str,
            'duration_secs': dur_secs,
            'est_size': est,
            'status': 'Pending',
            'external_subs': [],
        }
        self.files.append(file_info)
        self.file_tree.insert('', 'end', values=(f, size, dur_str, est, 'Pending'))
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
        dlg.transient(self.root)
        dlg.grab_set()
        dlg.resizable(False, False)
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
            'bitrate':              self.bitrate.get(),
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
            'custom_ad_patterns':    self.custom_ad_patterns,
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
            self.transcode_mode.set(prefs.get('transcode_mode', self.transcode_mode.get()))
            self.quality_mode.set(prefs.get('quality_mode',     self.quality_mode.get()))
            self.bitrate.set(prefs.get('bitrate',               self.bitrate.get()))
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
            self.verify_output.set(prefs.get('verify_output',   self.verify_output.get()))
            self.notify_sound.set(prefs.get('notify_sound',     self.notify_sound.get()))
            self.notify_sound_file.set(prefs.get('notify_sound_file', self.notify_sound_file.get()))
            # Default folders
            self.recent_folders = prefs.get('recent_folders', [])
            self.custom_ad_patterns = prefs.get('custom_ad_patterns', [])
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
                ("Ctrl+I",         "Media Info"),
                ("Ctrl+T",         "Test Encode (30s)"),
                ("Ctrl+Shift+F",   "Open Output Folder"),
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
            messagebox.showinfo("Media Info", "Please select a file from the list first.")
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
                messagebox.showerror("Media Info Error", result.stderr[:300])
                return
            import json as _json
            data = _json.loads(result.stdout)
        except Exception as e:
            messagebox.showerror("Media Info Error", str(e))
            return

        dlg = tk.Toplevel(self.root)
        dlg.title(f"Media Info — {os.path.basename(filepath)}")
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
            # Show audio controls
            self.audio_frame.grid(row=3)
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

        # Audio and checkboxes position based on mode
        if show_audio:
            self.audio_frame.grid(row=6)
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
            }

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

def main():
    """Main entry point"""
    root = TkinterDnD.Tk() if HAS_DND else tk.Tk()

    # Hide window until fully built and positioned — prevents flicker/wrong-monitor flash
    root.withdraw()

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
