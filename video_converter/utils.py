"""
Docflix Video Converter — Shared Utility Functions

Format helpers, ffprobe wrappers, tooltip widget, and other
utilities used across multiple modules.
"""

import json
import os
import shutil
import subprocess
import tkinter as tk
from tkinter import filedialog


# ═══════════════════════════════════════════════════════════════════
# Formatting helpers
# ═══════════════════════════════════════════════════════════════════

def format_size(size_bytes):
    """Format file size in human-readable format."""
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
    """Format duration as HH:MM:SS or MM:SS."""
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
    """Format seconds into human-readable time string."""
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


# ═══════════════════════════════════════════════════════════════════
# ffprobe wrappers
# ═══════════════════════════════════════════════════════════════════

def get_video_duration(filepath):
    """Get video duration in seconds using ffprobe."""
    try:
        cmd = [
            'ffprobe', '-v', 'error',
            '-show_entries', 'format=duration',
            '-of', 'default=noprint_wrappers=1:nokey=1',
            filepath,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=30)
        if result.returncode == 0:
            return float(result.stdout.strip())
    except Exception:
        pass
    return None


def get_subtitle_streams(filepath):
    """Return a list of subtitle stream dicts for the given file.

    Each dict has: index, codec_name, language, title, forced, sdh,
    default, empty, num_frames.
    """
    try:
        cmd = [
            'ffprobe', '-v', 'quiet',
            '-print_format', 'json',
            '-show_streams',
            '-select_streams', 's',
            filepath,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=30)
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
    """Return a list of all stream dicts (video, audio, subtitle, etc.).

    Each dict has: index, codec_type, codec_name.
    """
    try:
        cmd = [
            'ffprobe', '-v', 'quiet',
            '-print_format', 'json',
            '-show_streams',
            filepath,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=30)
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

    Each dict has: index, codec_name, codec_long_name, channels,
    sample_rate, bit_rate, language, title.
    """
    try:
        cmd = [
            'ffprobe', '-v', 'quiet',
            '-print_format', 'json',
            '-show_streams',
            '-select_streams', 'a',
            filepath,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=30)
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


# ═══════════════════════════════════════════════════════════════════
# Tkinter helpers
# ═══════════════════════════════════════════════════════════════════

def create_tooltip(widget, text):
    """Attach a hover tooltip to a tkinter widget."""
    tip = None

    def _show(event):
        nonlocal tip
        if tip:
            return
        tip = tk.Toplevel(widget)
        tip.wm_overrideredirect(True)
        tip.wm_geometry(f"+{event.x_root + 12}+{event.y_root + 8}")
        lbl = tk.Label(tip, text=text, background='#ffffe0',
                       relief='solid', borderwidth=1,
                       font=('Helvetica', 9), padx=6, pady=2)
        lbl.pack()

    def _hide(event):
        nonlocal tip
        if tip:
            tip.destroy()
            tip = None

    widget.bind('<Enter>', _show, add='+')
    widget.bind('<Leave>', _hide, add='+')


def ask_directory(initialdir=None, title="Select Folder", parent=None):
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
                cmd, capture_output=True, text=True, timeout=120)
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
            return ''
        except Exception:
            pass
    kwargs = {'initialdir': initialdir, 'title': title}
    if parent:
        kwargs['parent'] = parent
    return filedialog.askdirectory(**kwargs)


def center_window_on_screen(win):
    """Center a Toplevel or Tk window on the screen containing the
    mouse pointer."""
    win.update_idletasks()
    # Use requested geometry if winfo returns tiny defaults
    width = win.winfo_width()
    height = win.winfo_height()
    if width <= 10 or height <= 10:
        # Parse from geometry string (e.g. "950x650")
        try:
            geo = win.geometry()
            wh = geo.split('+')[0]
            width, height = (int(x) for x in wh.split('x'))
        except (ValueError, IndexError):
            width, height = 800, 600
    screen_w = win.winfo_screenwidth()
    screen_h = win.winfo_screenheight()
    x = (screen_w - width) // 2
    y = (screen_h - height) // 2
    win.geometry(f'{width}x{height}+{x}+{y}')


def center_window_on_parent(win, parent):
    """Center a Toplevel window on its parent window."""
    win.update_idletasks()
    w = win.winfo_width()
    h = win.winfo_height()
    pw = parent.winfo_width()
    ph = parent.winfo_height()
    px = parent.winfo_x()
    py = parent.winfo_y()
    x = max(0, px + (pw - w) // 2)
    y = max(0, py + (ph - h) // 2)
    win.geometry(f'{w}x{h}+{x}+{y}')
