"""
Docflix Media Suite — GPU Detection & Verification

GPU backend detection (NVIDIA NVENC, Intel QSV, AMD VAAPI),
test encode verification, ffmpeg availability check,
closed caption detection, and video analysis utilities.
"""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from .constants import GPU_BACKENDS
from .utils import get_video_duration, format_size

# Module-level flag — set via --gpu-test-mode CLI flag
GPU_TEST_MODE = False


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


