#!/usr/bin/env python3
"""
Docflix Video Converter - Standalone GUI Application
Convert MKV videos to H.265/HEVC format with CPU or GPU encoding

Features:
- CPU (libx265) and GPU (NVENC) encoding
- Bitrate and CRF quality modes
- Batch conversion with progress tracking
- Folder selection and file management
- Real-time logging and notifications

Requirements:
- ffmpeg with optional NVENC support
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
APP_VERSION = "1.0.0"
DEFAULT_BITRATE = "2M"
DEFAULT_CRF = 23
DEFAULT_PRESET = "ultrafast"
DEFAULT_GPU_PRESET = "p4"

# Video codec definitions
# Keys: display name -> dict with cpu_encoder, gpu_encoder (or None), cpu_presets, gpu_presets,
#       crf_range, crf_default, crf_flag (cpu), cq_flag (gpu), short_name
VIDEO_CODEC_MAP = {
    'H.265 / HEVC': {
        'cpu_encoder': 'libx265',
        'gpu_encoder': 'hevc_nvenc',
        'cpu_presets': ('ultrafast', 'superfast', 'veryfast', 'faster', 'fast',
                        'medium', 'slow', 'slower', 'veryslow'),
        'gpu_presets': ('p1', 'p2', 'p3', 'p4', 'p5', 'p6', 'p7'),
        'cpu_preset_default': 'ultrafast',
        'gpu_preset_default': 'p4',
        'crf_min': 0, 'crf_max': 51, 'crf_default': 23,
        'crf_flag': '-crf', 'cq_flag': '-cq',
        'short_name': 'H265',
    },
    'H.264 / AVC': {
        'cpu_encoder': 'libx264',
        'gpu_encoder': 'h264_nvenc',
        'cpu_presets': ('ultrafast', 'superfast', 'veryfast', 'faster', 'fast',
                        'medium', 'slow', 'slower', 'veryslow'),
        'gpu_presets': ('p1', 'p2', 'p3', 'p4', 'p5', 'p6', 'p7'),
        'cpu_preset_default': 'ultrafast',
        'gpu_preset_default': 'p4',
        'crf_min': 0, 'crf_max': 51, 'crf_default': 23,
        'crf_flag': '-crf', 'cq_flag': '-cq',
        'short_name': 'H264',
    },
    'AV1': {
        'cpu_encoder': 'libsvtav1',
        'gpu_encoder': 'av1_nvenc',
        'cpu_presets': ('0', '1', '2', '3', '4', '5', '6', '7', '8', '9', '10', '11', '12', '13'),
        'gpu_presets': ('p1', 'p2', 'p3', 'p4', 'p5', 'p6', 'p7'),
        'cpu_preset_default': '8',
        'gpu_preset_default': 'p4',
        'crf_min': 0, 'crf_max': 63, 'crf_default': 35,
        'crf_flag': '-crf', 'cq_flag': '-cq',
        'short_name': 'AV1',
    },
    'VP9': {
        'cpu_encoder': 'libvpx-vp9',
        'gpu_encoder': None,   # No NVENC for VP9
        'cpu_presets': ('0', '1', '2', '3', '4', '5'),
        'gpu_presets': (),
        'cpu_preset_default': '2',
        'gpu_preset_default': None,
        'crf_min': 0, 'crf_max': 63, 'crf_default': 33,
        'crf_flag': '-crf', 'cq_flag': None,
        'short_name': 'VP9',
    },
    'Copy (no re-encode)': {
        'cpu_encoder': 'copy',
        'gpu_encoder': 'copy',
        'cpu_presets': (),
        'gpu_presets': (),
        'cpu_preset_default': None,
        'gpu_preset_default': None,
        'crf_min': 0, 'crf_max': 51, 'crf_default': 23,
        'crf_flag': None, 'cq_flag': None,
        'short_name': 'copy',
    },
}

# Supported video extensions
VIDEO_EXTENSIONS = {'.mkv', '.mp4', '.avi', '.mov', '.wmv', '.flv', '.webm'}

# ============================================================================
# Utility Functions
# ============================================================================

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
            })
        return streams
    except Exception:
        return []


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
        encoder = settings.get('encoder', 'gpu')

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

def check_gpu_encoding():
    """Check if NVIDIA GPU encoding is available"""
    try:
        result = subprocess.run(['ffmpeg', '-encoders'], capture_output=True, text=True, timeout=10)
        output = result.stdout + result.stderr
        has_nvenc = 'hevc_nvenc' in output or 'h265_nvenc' in output
        has_cuda = 'cuda' in output.lower()
        return has_nvenc, has_cuda
    except Exception:
        return False, False

def get_gpu_name():
    """Get NVIDIA GPU name if available"""
    try:
        result = subprocess.run(['nvidia-smi', '--query-gpu=name', '--format=csv,noheader'],
                              capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            return result.stdout.strip().split('\n')[0]
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
            - encoder: 'cpu' or 'gpu'
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
            mode          = settings.get('mode', 'bitrate')
            two_pass      = settings.get('two_pass', False)

            # Two-pass only makes sense for CPU bitrate mode on supported codecs
            cpu_encoder = codec_info.get('cpu_encoder', '')
            TWO_PASS_SUPPORTED = {'libx265', 'libx264', 'libvpx-vp9'}
            use_two_pass = (
                two_pass and
                encoder == 'cpu' and
                mode == 'bitrate' and
                cpu_encoder in TWO_PASS_SUPPORTED and
                transcode_mode in ('video', 'both')
            )

            # GPU NVENC multipass (separate concept from CPU two-pass)
            gpu_encoder = codec_info.get('gpu_encoder', '')
            use_gpu_multipass = (
                two_pass and
                encoder == 'gpu' and
                mode == 'bitrate' and
                gpu_encoder in ('hevc_nvenc', 'h264_nvenc', 'av1_nvenc')
            )

            def _build_base_cmd():
                """Build the common part of the ffmpeg command."""
                c = ['ffmpeg', '-y']
                if hw_decode and encoder == 'gpu' and transcode_mode in ['video', 'both'] and codec_info['gpu_encoder'] not in (None, 'copy'):
                    c.extend(['-hwaccel', 'cuda', '-hwaccel_output_format', 'cuda'])
                c.extend(['-i', input_path])
                return c

            def _add_video_args(c, pass_num=None):
                """Add video encoding arguments. pass_num: None=single, 1=first, 2=second."""
                if transcode_mode in ['video', 'both']:
                    video_encoder = codec_info['gpu_encoder'] if encoder == 'gpu' else codec_info['cpu_encoder']
                    c.extend(['-c:v', video_encoder])

                    if video_encoder != 'copy':
                        preset = settings.get('preset', '')
                        if preset:
                            if codec_info['cpu_encoder'] == 'libvpx-vp9' and encoder == 'cpu':
                                c.extend(['-cpu-used', preset])
                            else:
                                c.extend(['-preset', preset])

                        if mode == 'crf':
                            crf_val = str(settings.get('crf', codec_info['crf_default']))
                            if encoder == 'gpu' and codec_info['cq_flag']:
                                c.extend([codec_info['cq_flag'], crf_val])
                            elif codec_info['crf_flag']:
                                c.extend([codec_info['crf_flag'], crf_val])
                                if codec_info['cpu_encoder'] == 'libvpx-vp9':
                                    c.extend(['-b:v', '0'])
                        else:
                            bitrate = settings.get('bitrate', DEFAULT_BITRATE)
                            c.extend(['-b:v', bitrate])
                            if encoder == 'cpu' and codec_info['cpu_encoder'] not in ('libsvtav1', 'libvpx-vp9'):
                                c.extend(['-minrate', bitrate, '-maxrate', bitrate, '-bufsize', bitrate])
                            if use_gpu_multipass:
                                c.extend(['-multipass', 'fullres'])

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
                """Add subtitle stream arguments."""
                sub_settings = settings.get('subtitle_settings', {})
                if not sub_settings:
                    c.extend(['-c:s', 'copy'])
                else:
                    c.extend(['-map', '0:v?', '-map', '0:a?'])
                    out_sub_idx = 0  # output subtitle stream counter
                    for stream_index, ss in sub_settings.items():
                        if not ss.get('keep', True):
                            continue
                        fmt = ss.get('format', 'copy')
                        if fmt == 'drop':
                            continue
                        if fmt == 'extract only':
                            fmt = 'copy'
                        c.extend(['-map', f"0:{stream_index}"])
                        c.extend([f'-c:s:{out_sub_idx}', fmt])
                        out_sub_idx += 1

            # ── Log what we're about to do ──
            video_encoder_name = codec_info['gpu_encoder'] if encoder == 'gpu' else codec_info['cpu_encoder']
            self.log(f"Video codec: {video_encoder_name}", 'INFO')
            self.log(f"Mode: {mode}" + (" (two-pass)" if use_two_pass else " (GPU multipass)" if use_gpu_multipass else ""), 'INFO')
            if hw_decode and encoder == 'gpu':
                self.log("Hardware decode: CUDA enabled", 'INFO')

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

            for line in self.current_process.stdout:
                if self.is_stopped:
                    self.current_process.terminate()
                    self.log("Conversion stopped by user", "WARNING")
                    return False

                while self.is_paused:
                    if self.is_stopped:
                        return False
                    time.sleep(0.5)

                line = line.strip()

                if 'time=' in line:
                    match = re.search(r'time=(\d+):(\d+):(\d+)', line)
                    if match and duration:
                        h, m, s = map(int, match.groups())
                        current_time = h * 3600 + m * 60 + s
                        progress = (current_time / duration) * 100

                        # Parse fps and speed from the same line
                        fps_match   = re.search(r'fps=\s*([\d.]+)', line)
                        speed_match = re.search(r'speed=\s*([\d.]+)x', line)
                        fps   = float(fps_match.group(1))   if fps_match   else None
                        speed = float(speed_match.group(1)) if speed_match else None

                        # Calculate ETA
                        eta = None
                        if speed and speed > 0 and duration:
                            remaining_video_secs = duration - current_time
                            eta = remaining_video_secs / speed  # real-time seconds remaining

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
        
        # Settings
        self.encoder_mode = tk.StringVar(value='gpu')
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
        self.has_gpu, has_cuda = check_gpu_encoding()
        self.gpu_name = get_gpu_name() if self.has_gpu else None

        # Hardware decode defaults to on if GPU is available
        self.hw_decode = tk.BooleanVar(value=bool(self.has_gpu))
        
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
        
        # Encoder toggle (also contains folder/refresh buttons)
        encoder_frame = ttk.Frame(header_frame, padding=5)
        encoder_frame.grid(row=0, column=1, sticky="e", padx=10)

        ttk.Button(encoder_frame, text="📁 Change Folder",
                  command=self.change_folder).pack(side='left', padx=(0, 5))

        ttk.Button(encoder_frame, text="🔄 Refresh",
                  command=self.refresh_files).pack(side='left', padx=(0, 10))

        ttk.Separator(encoder_frame, orient='vertical').pack(side='left', fill='y', padx=(0, 8))

        self.cpu_radio = ttk.Radiobutton(encoder_frame, text="CPU",
                                        variable=self.encoder_mode, value='cpu',
                                        command=self.on_encoder_change)
        self.cpu_radio.pack(side='left')
        
        self.gpu_radio = ttk.Radiobutton(encoder_frame, text="GPU",
                                        variable=self.encoder_mode, value='gpu',
                                        command=self.on_encoder_change,
                                        state='normal' if self.has_gpu else 'disabled')
        self.gpu_radio.pack(side='left', padx=10)
        
        if self.gpu_name:
            ttk.Label(encoder_frame, text=f"({self.gpu_name})",
                     font=('Helvetica', 8)).pack(side='left')

        # Hardware decode checkbox — only useful with GPU
        ttk.Separator(encoder_frame, orient='vertical').pack(side='left', fill='y', padx=(8, 0))
        self.hw_decode_check = tk.Checkbutton(
            encoder_frame, text="HW Decode",
            variable=self.hw_decode,
            state='normal' if self.has_gpu else 'disabled',
            relief='flat', bd=0)
        self.hw_decode_check.pack(side='left')

        # Output directory row
        out_frame = ttk.Frame(header_frame)
        out_frame.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        out_frame.columnconfigure(1, weight=1)

        ttk.Label(out_frame, text="Output Folder:").grid(row=0, column=0, sticky='w', padx=(0, 6))

        self.output_dir_label = ttk.Label(out_frame, text="Same as source file",
                                          foreground='gray', anchor='w')
        self.output_dir_label.grid(row=0, column=1, sticky='ew')

        ttk.Button(out_frame, text="📂 Set Output Folder",
                   command=self.change_output_folder).grid(row=0, column=2, padx=(6, 4))
        ttk.Button(out_frame, text="✖ Reset to Source",
                   command=self.reset_output_folder).grid(row=0, column=3)

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

        ttk.Separator(self.check_frame, orient='vertical').pack(side='left', fill='y', padx=8)

        ttk.Checkbutton(self.check_frame, text="🔔 Notify when done",
                       variable=self.notify_sound).pack(side='left', padx=(0, 4))

        SOUND_NAMES = [
            'complete', 'alarm-clock-elapsed', 'bell', 'message',
            'dialog-information', 'phone-incoming-call', 'service-login',
            'window-attention', 'audio-test-signal'
        ]
        self.sound_combo = ttk.Combobox(self.check_frame, textvariable=self.notify_sound_file,
                                        values=SOUND_NAMES, width=18, state='readonly')
        self.sound_combo.pack(side='left', padx=2)

        ttk.Button(self.check_frame, text="▶", width=2,
                   command=self.preview_sound).pack(side='left', padx=2)

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
        self.tree_context_menu.add_command(label="🎞️ Subtitle Tracks...", command=self.show_subtitle_dialog)
        self.tree_context_menu.add_separator()
        self.tree_context_menu.add_command(label="🗑️ Remove from list", command=self.remove_selected_file)
        self.file_tree.bind('<Button-3>', self.on_file_tree_right_click)
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
        """Redraw a single tree row to reflect override indicator and est size."""
        name = file_info['name']
        display_name = ('⚙️ ' + name) if 'overrides' in file_info else name
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
        cpu_rb = ttk.Radiobutton(enc_frame, text="CPU", variable=v_encoder, value='cpu',
                                 command=lambda: _update_presets())
        cpu_rb.pack(side='left')
        gpu_rb = ttk.Radiobutton(enc_frame, text="GPU", variable=v_encoder, value='gpu',
                                 command=lambda: _update_presets(),
                                 state='normal' if self.has_gpu else 'disabled')
        gpu_rb.pack(side='left', padx=8)
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
            if enc == 'gpu' and info['gpu_encoder']:
                presets = info['gpu_presets']
                default = info['gpu_preset_default']
            else:
                presets = info['cpu_presets']
                default = info['cpu_preset_default']
            preset_combo['values'] = presets
            if v_preset.get() not in presets:
                v_preset.set(default or (presets[0] if presets else ''))
            # HW decode only useful with GPU
            hw_cb.configure(state='normal' if (enc == 'gpu' and self.has_gpu) else 'disabled')

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

        # Probe subtitle streams in a thread so UI doesn't freeze
        streams = get_subtitle_streams(filepath)

        dlg = tk.Toplevel(self.root)
        dlg.title(f"Subtitle Tracks — {os.path.basename(filepath)}")
        dlg.geometry("700x420")
        dlg.transient(self.root)
        dlg.grab_set()
        self._center_on_main(dlg)
        dlg.resizable(True, True)

        # ── Header info ──
        if not streams:
            ttk.Label(dlg, text="No subtitle tracks found in this file.",
                      font=('Helvetica', 11), padding=20).pack()
            ttk.Button(dlg, text="Close", command=dlg.destroy).pack(pady=10)
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
        COL_WIDTHS = [40, 60, 70, 100, 0, 120]  # 0 = expand
        header_frame = ttk.Frame(dlg, padding=(10, 0, 10, 0))
        header_frame.pack(fill='x')
        header_frame.columnconfigure(4, weight=1)
        for col, (text, w) in enumerate(zip(
            ['Keep', 'Stream', 'Language', 'Codec', 'Title / Flags', 'Convert To'],
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

        # Mouse wheel scrolling
        def on_mousewheel(e):
            canvas.yview_scroll(int(-1 * (e.delta / 120)), 'units')
        canvas.bind_all('<MouseWheel>', on_mousewheel)
        # Linux scroll
        canvas.bind_all('<Button-4>', lambda e: canvas.yview_scroll(-1, 'units'))
        canvas.bind_all('<Button-5>', lambda e: canvas.yview_scroll(1, 'units'))

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

        # Unbind mousewheel when dialog closes
        def on_close():
            canvas.unbind_all('<MouseWheel>')
            canvas.unbind_all('<Button-4>')
            canvas.unbind_all('<Button-5>')
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

        dlg.wait_window()

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

        added = 0
        for path_str in paths:
            path = Path(path_str.strip())
            if path.is_dir():
                # Recursively add all video files from dropped folder
                for filepath in sorted(path.rglob('*')):
                    if filepath.is_file() and filepath.suffix.lower() in VIDEO_EXTENSIONS:
                        added += self._add_file_to_list(filepath)
            elif path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS:
                added += self._add_file_to_list(path)

        if added:
            self.add_log(f"Added {added} file(s) via drag & drop.", 'INFO')
        else:
            self.add_log("No supported video files found in dropped items.", 'WARNING')

    def _add_file_to_list(self, filepath):
        """Add a single file to the list if not already present. Returns 1 if added, 0 if skipped."""
        filepath = Path(filepath)
        f = filepath.name
        # Skip already-converted files
        if re.search(r'-(\d+(\.\d+)?M|CRF\d+)-(NVENC_|)(H265|H264|AV1|VP9)_|-video-copy|-audio-copy|-[A-Z0-9]+_\d+k', f):
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
            'status': 'Pending'
        }
        self.files.append(file_info)
        self.file_tree.insert('', 'end', values=(f, size, dur_str, est, 'Pending'))
        return 1

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
                       filedialog.askdirectory(initialdir=v_video_folder.get(),
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
                       filedialog.askdirectory(initialdir=v_output_folder.get() or str(Path.home()),
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
                dlg.geometry("640x400")
            else:
                custom_frame.grid_remove()
                dlg.geometry("640x360")

        v_player.trace_add('write', on_player_changed)
        # Set initial state
        if v_player.get() == 'Custom...':
            custom_frame.grid()
            dlg.geometry("640x400")
        else:
            custom_frame.grid_remove()
            dlg.geometry("640x360")

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
            'two_pass':             self.two_pass.get(),
            'verify_output':        self.verify_output.get(),
            'notify_sound':         self.notify_sound.get(),
            'notify_sound_file':    self.notify_sound_file.get(),
            'default_player':        self.default_player.get(),
            'default_video_folder':  str(self.working_dir),
            'default_output_folder': str(self.output_dir) if self.output_dir else '',
            'recent_folders':        self.recent_folders,
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
            self.encoder_mode.set(prefs.get('encoder',          self.encoder_mode.get()))
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
            self.two_pass.set(prefs.get('two_pass',             self.two_pass.get()))
            self.verify_output.set(prefs.get('verify_output',   self.verify_output.get()))
            self.notify_sound.set(prefs.get('notify_sound',     self.notify_sound.get()))
            self.notify_sound_file.set(prefs.get('notify_sound_file', self.notify_sound_file.get()))
            # Default folders
            self.recent_folders = prefs.get('recent_folders', [])
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
        self.encoder_mode.set('gpu')
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
        self.hw_decode.set(bool(self.has_gpu))
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
            f"Supports H.265, H.264, AV1, VP9 encoding\n"
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
    
    def update_progress(self, percent, details='', fps=None, eta=None, pass_label=None):
        """Update progress bar, fps, and ETA labels."""
        def _update():
            self.progress_var.set(percent)
            # FPS
            if fps is not None:
                self.fps_label.configure(text=f"⚡ {fps:.1f} fps")
            # ETA
            if eta is not None:
                pass_str = f" ({pass_label})" if pass_label else ""
                self.eta_label.configure(text=f"ETA{pass_str}: {format_time(eta)}")
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
            return {'transcode_mode': 'video', 'encoder': 'gpu',
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

    def change_output_folder(self):
        """Set a custom output directory."""
        folder = filedialog.askdirectory(
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
                        if not re.search(r'-(\d+(\.\d+)?M|CRF\d+)-(NVENC_|)(H265|H264|AV1|VP9)_|-video-copy|-audio-copy|-[A-Z0-9]+_\d+k', f):
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
                'status': 'Pending'
            }
            self.files.append(file_info)
            self.file_tree.insert('', 'end', values=(rel, size, '…', '…', 'Pending'))

        count = len(self.files)
        self.add_log(f"Found {count} video file(s) — loading metadata...", 'INFO')
        self.status_label.configure(text=f"Loading metadata for {count} file(s)...")

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
        settings = {
            'transcode_mode': self.transcode_mode.get(),
            'encoder':        self.encoder_mode.get(),
            'codec_info':     self.get_codec_info(),
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
            # Inject -t 30 before the output path
            import copy
            # Build command manually with -t 30
            cmd = ['ffmpeg', '-y']
            encoder  = settings['encoder']
            hw       = settings['hw_decode']
            ci       = settings['codec_info']
            tm       = settings['transcode_mode']
            if hw and encoder == 'gpu' and tm in ('video','both') and ci['gpu_encoder'] not in (None,'copy'):
                cmd.extend(['-hwaccel','cuda','-hwaccel_output_format','cuda'])
            cmd.extend(['-i', input_path, '-t', '30'])
            # Let convert_file handle the rest by calling _run_process via a temp wrapper
            # Instead build a simplified single-pass command
            video_enc = ci['gpu_encoder'] if encoder == 'gpu' else ci['cpu_encoder']
            if tm in ('video','both'):
                cmd.extend(['-c:v', video_enc])
                if video_enc != 'copy':
                    preset = settings['preset']
                    if preset:
                        if ci['cpu_encoder'] == 'libvpx-vp9' and encoder == 'cpu':
                            cmd.extend(['-cpu-used', preset])
                        else:
                            cmd.extend(['-preset', preset])
                    if settings['mode'] == 'crf':
                        crf_val = str(settings['crf'])
                        if encoder == 'gpu' and ci['cq_flag']:
                            cmd.extend([ci['cq_flag'], crf_val])
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

    def on_video_codec_change(self, event=None):
        """Handle video codec selection change."""
        info = self.get_codec_info()
        encoder = self.encoder_mode.get()

        # VP9 has no GPU encoder — force CPU and disable GPU radio
        if info['gpu_encoder'] is None:
            self.encoder_mode.set('cpu')
            self.gpu_radio.configure(state='disabled')
        else:
            if self.has_gpu:
                self.gpu_radio.configure(state='normal')

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

    def _apply_presets_for_codec(self, info, silent=True):
        """Update preset combobox values and selection for current codec+encoder."""
        encoder = self.encoder_mode.get()
        if encoder == 'gpu' and info['gpu_encoder'] and info['gpu_encoder'] != 'copy':
            presets = info['gpu_presets']
            default = info['gpu_preset_default']
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
        if self.two_pass.get() and self.encoder_mode.get() == 'gpu':
            messagebox.showinfo(
                "GPU Two-Pass Encoding",
                "On GPU (NVENC), two-pass uses the -multipass fullres flag which runs "
                "inside a single ffmpeg process.\n\n"
                "This is different from CPU two-pass which runs ffmpeg twice — "
                "once for analysis and once for encoding.\n\n"
                "NVENC multipass still improves quality slightly over single-pass, "
                "but don't expect it to take twice as long.\n\n"
                "For the most accurate bitrate targeting and best quality, "
                "use CPU encoding with two-pass enabled."
            )

    def _update_two_pass_state(self):
        """Enable two-pass checkbox only when applicable."""
        info = self.get_codec_info()
        encoder = self.encoder_mode.get()
        mode = self.quality_mode.get()
        cpu_enc = info.get('cpu_encoder', '')
        TWO_PASS_SUPPORTED = {'libx265', 'libx264', 'libvpx-vp9'}
        # CPU bitrate mode on supported codecs, OR GPU bitrate mode with NVENC
        gpu_enc = info.get('gpu_encoder', '')
        GPU_MULTIPASS = {'hevc_nvenc', 'h264_nvenc', 'av1_nvenc'}
        applicable = (
            mode == 'bitrate' and (
                (encoder == 'cpu' and cpu_enc in TWO_PASS_SUPPORTED) or
                (encoder == 'gpu' and gpu_enc in GPU_MULTIPASS)
            )
        )
        self.two_pass_check.configure(state='normal' if applicable else 'disabled')
        if not applicable:
            self.two_pass.set(False)

    def on_encoder_change(self, silent=False):
        """Handle CPU/GPU encoder toggle."""
        info = self.get_codec_info()
        encoder = self.encoder_mode.get()

        self._apply_presets_for_codec(info, silent=silent)

        # Enable/disable HW decode checkbox based on encoder selection
        if encoder == 'gpu' and self.has_gpu:
            self.hw_decode_check.configure(state='normal')
        else:
            self.hw_decode.set(False)
            self.hw_decode_check.configure(state='disabled')

        self._update_two_pass_state()

        if not silent:
            label = 'GPU (NVENC)' if encoder == 'gpu' else 'CPU'
            preset = self.preset_combo.get()
            self.add_log(f"Switched to {label} encoding (preset: {preset})", 'INFO')
        self._schedule_estimate_refresh()

    def on_preset_change(self, event=None):
        """Handle preset selection change."""
        preset = self.preset_combo.get()
        if self.encoder_mode.get() == 'gpu':
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
        if encoder == 'gpu' and not self.has_gpu:
            messagebox.showerror("Error", "GPU encoding is not available on this system.")
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
            video_encoder = codec_info['gpu_encoder'] if encoder == 'gpu' else codec_info['cpu_encoder']
            self.add_log(f"Video Codec: {self.video_codec.get()} ({video_encoder})", 'INFO')
            self.add_log(f"Encoder: {'GPU (NVENC)' if encoder == 'gpu' else 'CPU'}", 'INFO')
            if encoder == 'gpu' and self.hw_decode.get():
                self.add_log("Hardware Decode: CUDA (enabled)", 'INFO')
            elif encoder == 'gpu':
                self.add_log("Hardware Decode: disabled", 'INFO')
            if video_encoder != 'copy':
                self.add_log(f"Quality Mode: {self.quality_mode.get()}", 'INFO')
                if self.quality_mode.get() == 'bitrate':
                    self.add_log(f"Video Bitrate: {self.bitrate.get()}", 'INFO')
                    if self.two_pass.get():
                        info = self.get_codec_info()
                        cpu_enc = info.get('cpu_encoder', '')
                        gpu_enc = info.get('gpu_encoder', '')
                        TWO_PASS_CPU = {'libx265', 'libx264', 'libvpx-vp9'}
                        GPU_MULTIPASS = {'hevc_nvenc', 'h264_nvenc', 'av1_nvenc'}
                        if encoder == 'cpu' and cpu_enc in TWO_PASS_CPU:
                            self.add_log("Two-Pass Encoding: enabled (pass 1 = analysis, pass 2 = encode)", 'INFO')
                        elif encoder == 'gpu' and gpu_enc in GPU_MULTIPASS:
                            self.add_log("Two-Pass Encoding: GPU multipass fullres (NVENC)", 'INFO')
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
        completed = 0
        failed = 0
        skipped = 0
        
        settings = {
            'transcode_mode': self.transcode_mode.get(),
            'encoder': self.encoder_mode.get(),
            'codec_info': self.get_codec_info(),
            'mode': self.quality_mode.get(),
            'bitrate': self.bitrate.get(),
            'crf': int(self.crf.get()),
            'preset': self.preset_combo.get(),
            'gpu_preset': self.gpu_preset.get(),
            'audio_codec': self.get_audio_codec_name(),
            'audio_bitrate': self.audio_bitrate.get(),
            'hw_decode': self.hw_decode.get(),
            'two_pass': self.two_pass.get(),
            'subtitle_settings': {}  # per-file override below
        }

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

            file_settings = {
                'transcode_mode': ov.get('transcode_mode', settings['transcode_mode']),
                'encoder':        ov.get('encoder',        settings['encoder']),
                'codec_info':     ov.get('codec_info',     settings['codec_info']),
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
                elif quality_mode == 'crf':
                    suffix = f"-CRF{file_settings['crf']}"
                    suffix += f"-NVENC_{short}_{preset}" if encoder == 'gpu' else f"-{short}_{preset}"
                else:
                    suffix = f"-{file_settings['bitrate']}"
                    suffix += f"-NVENC_{short}_{preset}" if encoder == 'gpu' else f"-{short}_{preset}"

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

            # Convert
            self.current_output_path = output_path
            success = self.converter.convert_file(input_path, output_path, file_settings)
            self.current_output_path = None

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
            self.eta_label.configure(text="")
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

    # Run
    root.mainloop()

if __name__ == '__main__':
    main()
