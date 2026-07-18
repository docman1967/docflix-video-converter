"""
Docflix Media Suite — AI Upscaler

Manages Real-ESRGAN ncnn-vulkan for AI-powered video upscaling.
Handles download, installation, and frame-based upscaling pipeline:
  extract frames → AI upscale → reassemble with audio/subs.

Integrates with video_scaler.py as an alternative upscale method.
"""

import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
import threading
import time
import zipfile
from pathlib import Path
from urllib.request import urlretrieve
from urllib.error import URLError


# ═══════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════

INSTALL_DIR = Path.home() / '.local' / 'share' / 'docflix' / 'realesrgan'
BINARY_NAME = 'realesrgan-ncnn-vulkan'

# GitHub release info — v0.2.5.0 is the latest with pre-built binaries
RELEASE_TAG = 'v0.2.5.0'
RELEASE_URLS = {
    'Linux': (
        'https://github.com/xinntao/Real-ESRGAN/releases/download/'
        'v0.2.5.0/realesrgan-ncnn-vulkan-20220424-ubuntu.zip'
    ),
    'Darwin': (
        'https://github.com/xinntao/Real-ESRGAN/releases/download/'
        'v0.2.5.0/realesrgan-ncnn-vulkan-20220424-macos.zip'
    ),
    'Windows': (
        'https://github.com/xinntao/Real-ESRGAN/releases/download/'
        'v0.2.5.0/realesrgan-ncnn-vulkan-20220424-windows.zip'
    ),
}

# Available upscaling models (bundled with the release)
MODELS = {
    'General (x4)': {
        'id': 'realesrgan-x4plus',
        'scale': 4,
        'description': 'Best for live-action video and photos',
    },
    'General (x4) Fast': {
        'id': 'realesr-animevideov3',
        'scale': 4,
        'description': 'Faster, good for most content (video-optimized)',
    },
    'Anime (x4)': {
        'id': 'realesrgan-x4plus-anime',
        'scale': 4,
        'description': 'Optimized for anime and animation',
    },
}

DEFAULT_MODEL = 'General (x4) Fast'

# Supported image formats for frame extraction
FRAME_FORMAT = 'png'  # lossless intermediate frames


# ═══════════════════════════════════════════════════════════════════
# Installation management
# ═══════════════════════════════════════════════════════════════════

def get_binary_path():
    """Return the path to the Real-ESRGAN binary, or None if not installed.

    Checks in order:
      1. System PATH
      2. Local install dir (~/.local/share/docflix/realesrgan/)
    """
    # Check system PATH first
    system_bin = shutil.which(BINARY_NAME)
    if system_bin:
        return system_bin

    # Check local install
    local_bin = INSTALL_DIR / BINARY_NAME
    if local_bin.exists() and os.access(local_bin, os.X_OK):
        return str(local_bin)

    return None


def is_installed():
    """Check if Real-ESRGAN is available."""
    return get_binary_path() is not None


def get_version():
    """Get the installed version string, or None."""
    binary = get_binary_path()
    if not binary:
        return None
    try:
        r = subprocess.run(
            [binary, '--version'],
            capture_output=True, text=True, timeout=10,
        )
        # The binary doesn't have a --version flag; it prints usage on error
        # Just return the release tag we installed
        if INSTALL_DIR / BINARY_NAME == Path(binary):
            version_file = INSTALL_DIR / '.version'
            if version_file.exists():
                return version_file.read_text().strip()
        return 'system'
    except Exception:
        return None


def download_and_install(progress_callback=None, log_callback=None):
    """Download and install Real-ESRGAN ncnn-vulkan binary.

    Args:
        progress_callback: fn(percent: float, status: str) called during download
        log_callback: fn(message: str, level: str) for log messages

    Returns:
        str: path to installed binary, or None on failure

    Raises:
        RuntimeError: if download or extraction fails
    """
    system_name = platform.system()
    url = RELEASE_URLS.get(system_name)
    if not url:
        raise RuntimeError(f"No pre-built binary available for {system_name}")

    def _log(msg, level='INFO'):
        if log_callback:
            log_callback(msg, level)

    def _progress(pct, status):
        if progress_callback:
            progress_callback(pct, status)

    _log(f"Downloading Real-ESRGAN {RELEASE_TAG} for {system_name}...")
    _progress(0, "Downloading Real-ESRGAN...")

    # Create install directory
    INSTALL_DIR.mkdir(parents=True, exist_ok=True)

    # Download to temp file
    zip_path = INSTALL_DIR / 'download.zip'
    try:
        def _reporthook(block_num, block_size, total_size):
            if total_size > 0:
                pct = min(90.0, (block_num * block_size / total_size) * 90)
                downloaded = block_num * block_size / 1024 / 1024
                total = total_size / 1024 / 1024
                _progress(pct, f"Downloading: {downloaded:.0f} / {total:.0f} MB")

        urlretrieve(url, str(zip_path), reporthook=_reporthook)
    except (URLError, OSError) as e:
        zip_path.unlink(missing_ok=True)
        raise RuntimeError(f"Download failed: {e}")

    _log("Download complete. Extracting...")
    _progress(90, "Extracting...")

    # Extract zip
    try:
        with zipfile.ZipFile(str(zip_path), 'r') as zf:
            zf.extractall(str(INSTALL_DIR))
    except zipfile.BadZipFile as e:
        zip_path.unlink(missing_ok=True)
        raise RuntimeError(f"Extraction failed: {e}")

    zip_path.unlink(missing_ok=True)

    # The zip extracts into a subdirectory — find and move contents up
    # e.g., realesrgan-ncnn-vulkan-20220424-ubuntu/
    for child in INSTALL_DIR.iterdir():
        if child.is_dir() and child.name.startswith('realesrgan'):
            # Move all contents to INSTALL_DIR
            for item in child.iterdir():
                dest = INSTALL_DIR / item.name
                if dest.exists():
                    if dest.is_dir():
                        shutil.rmtree(dest)
                    else:
                        dest.unlink()
                shutil.move(str(item), str(dest))
            child.rmdir()
            break

    # Make binary executable
    binary = INSTALL_DIR / BINARY_NAME
    if binary.exists():
        binary.chmod(0o755)
    else:
        raise RuntimeError(
            f"Binary not found after extraction. "
            f"Contents: {[f.name for f in INSTALL_DIR.iterdir()]}"
        )

    # Write version marker
    (INSTALL_DIR / '.version').write_text(RELEASE_TAG)

    _log(f"Real-ESRGAN {RELEASE_TAG} installed to {INSTALL_DIR}", 'SUCCESS')
    _progress(100, "Installed!")

    return str(binary)


def uninstall():
    """Remove the local Real-ESRGAN installation."""
    if INSTALL_DIR.exists():
        shutil.rmtree(INSTALL_DIR)
        return True
    return False


# ═══════════════════════════════════════════════════════════════════
# AI Upscaling Pipeline
# ═══════════════════════════════════════════════════════════════════

def detect_gpus():
    """Detect GPUs available to Real-ESRGAN (ncnn-vulkan).

    Returns a list like [{'index': 0, 'name': 'NVIDIA ...'}, ...]; an empty
    list means none were found (caller falls back to CPU via -g -1).

    NOTE: Real-ESRGAN addresses GPUs by *Vulkan* device index. On a standard
    NVIDIA-only host these align 1:1 with nvidia-smi indices (0, 1, ...), which
    is what we query here. (vulkaninfo would be authoritative but isn't required
    for this setup.)
    """
    gpus = []
    try:
        r = subprocess.run(
            ['nvidia-smi', '--query-gpu=index,name', '--format=csv,noheader'],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode == 0:
            for line in r.stdout.strip().splitlines():
                parts = [p.strip() for p in line.split(',', 1)]
                if len(parts) == 2 and parts[0].isdigit():
                    gpus.append({'index': int(parts[0]), 'name': parts[1]})
    except (OSError, subprocess.SubprocessError):
        pass
    return gpus


def normalize_gpu_ids(gpu_id):
    """Normalize a GPU selection into a list of int indices.

    Accepts:
      int          -> [n]           (single GPU; -1 = CPU)
      list / tuple -> [ints]
      'auto'/'all' -> every detected GPU (or [-1] CPU if none found)
      'cpu'/'-1'   -> [-1]
      '0,1'        -> [0, 1]        (comma-separated string)
    Always returns a non-empty list.
    """
    if isinstance(gpu_id, str):
        s = gpu_id.strip().lower()
        if s in ('auto', 'all'):
            gpus = detect_gpus()
            return [g['index'] for g in gpus] if gpus else [-1]
        if s in ('cpu', '-1'):
            return [-1]
        try:
            ids = [int(x) for x in s.split(',') if x.strip() != '']
            return ids or [0]
        except ValueError:
            return [0]
    if isinstance(gpu_id, (list, tuple)):
        ids = [int(x) for x in gpu_id]
        return ids or [0]
    return [int(gpu_id)]


def _best_temp_dir(est_bytes):
    """Prefer the RAM disk (/dev/shm) for the work dir when it has room for
    est_bytes + 30% margin; otherwise return None so the caller falls back to
    /tmp. Keeps huge full-movie frame sets off RAM while letting bounded jobs
    (previews, short clips) run in memory — where the GPUs stop starving on disk.
    """
    shm = '/dev/shm'
    try:
        if est_bytes > 0 and os.path.isdir(shm):
            if shutil.disk_usage(shm).free > est_bytes * 1.3:
                return shm
    except OSError:
        pass
    return None


class AIUpscaleJob:
    """Manages the frame-based AI upscaling pipeline for a single video.

    Pipeline:
      1. Extract frames from source video (ffmpeg → PNG)
      2. Upscale frames with Real-ESRGAN (batch processing)
      3. Reassemble upscaled frames + original audio/subs (ffmpeg)

    The pipeline preserves:
      - All audio tracks (copied, not re-encoded)
      - All subtitle tracks (copied)
      - Chapter markers
      - Video metadata
    """

    # NVENC encoder presets and quality settings
    NVENC_ENCODERS = {
        'hevc_nvenc': {
            'quality_args': ['-rc', 'vbr', '-cq', '22', '-rc-lookahead', '32',
                             '-temporal-aq', '1', '-spatial-aq', '1'],
            'preset_flag': '-preset',
            'preset_default': 'p5',
            'pix_fmt': 'p010le',  # 10-bit for HEVC
        },
        'h264_nvenc': {
            'quality_args': ['-rc', 'vbr', '-cq', '22', '-rc-lookahead', '32',
                             '-temporal-aq', '1', '-spatial-aq', '1'],
            'preset_flag': '-preset',
            'preset_default': 'p5',
            'pix_fmt': 'yuv420p',
        },
        'av1_nvenc': {
            'quality_args': ['-rc', 'vbr', '-cq', '22', '-rc-lookahead', '32'],
            'preset_flag': '-preset',
            'preset_default': 'p5',
            'pix_fmt': 'p010le',
        },
    }

    def __init__(self, input_path, output_path, model_name=None,
                 target_height=None, video_encoder='libx265',
                 crf='18', preset='medium', audio_codec='copy',
                 gpu_id=0, tta=False, strength=100,
                 log_callback=None, progress_callback=None):
        """
        Args:
            input_path: source video file
            output_path: output video file
            model_name: key from MODELS dict (default: 'General (x4) Fast')
            target_height: if set, scale DOWN after upscale (e.g., 480p→4x→1920p→1080p)
            video_encoder: ffmpeg encoder name (libx265, libx264, etc.)
            crf: quality setting
            preset: encoder preset
            audio_codec: 'copy' or specific codec
            gpu_id: GPU selection — int index, list, 'auto'/'all', or '0,1' (-1 = CPU)
            tta: Real-ESRGAN TTA mode (-x) — 8-orientation averaging, higher quality, ~8x slower
            log_callback: fn(msg, level)
            progress_callback: fn(percent, status_str)
        """
        self.input_path = str(input_path)
        self.output_path = str(output_path)
        self.model_name = model_name or DEFAULT_MODEL
        self.target_height = target_height
        self.video_encoder = video_encoder
        self.crf = crf
        self.preset = preset
        self.audio_codec = audio_codec
        self.gpu_ids = normalize_gpu_ids(gpu_id)
        self.gpu_id = self.gpu_ids[0] if self.gpu_ids else -1  # primary (back-compat)
        self.tta = bool(tta)
        self.strength = strength   # 0-100: how much AI vs. original grain to keep at reassembly
        self._log_cb = log_callback
        self._progress_cb = progress_callback
        self._cancelled = False
        self._process = None
        self._temp_dir = None

    def cancel(self):
        """Cancel the running job."""
        self._cancelled = True
        if self._process:
            try:
                self._process.kill()
            except OSError:
                pass

    def _log(self, msg, level='INFO'):
        if self._log_cb:
            self._log_cb(msg, level)

    def _progress(self, pct, status=''):
        if self._progress_cb:
            self._progress_cb(pct, status)

    def run(self):
        """Execute the full upscale pipeline. Returns True on success."""
        binary = get_binary_path()
        if not binary:
            self._log("Real-ESRGAN not installed. Use the download button.", 'ERROR')
            return False

        model_info = MODELS.get(self.model_name)
        if not model_info:
            self._log(f"Unknown model: {self.model_name}", 'ERROR')
            return False

        # ── Step 1: Probe first, so the work dir can be sized (RAM vs disk) ──
        self._progress(0, "Analyzing video...")
        duration, fps, src_w, src_h = self._probe_video()
        if not fps or not duration:
            self._log("Could not determine video FPS or duration", 'ERROR')
            return False
        total_frames = int(duration * fps)
        self._log(
            f"Source: {src_w}x{src_h} @ {fps:.2f} fps, "
            f"{total_frames} frames, {duration:.1f}s"
        )

        self._temp_dir = self._make_workdir(
            'docflix_upscale_', total_frames, src_w, src_h, model_info['scale'])
        frames_in = os.path.join(self._temp_dir, 'frames_in')
        frames_out = os.path.join(self._temp_dir, 'frames_out')
        os.makedirs(frames_in)
        os.makedirs(frames_out)

        try:
            # ── Step 2: Extract frames ──
            self._progress(2, "Extracting frames...")
            if not self._extract_frames(frames_in, fps, total_frames):
                return False
            if self._cancelled:
                return False

            frame_count = len([
                f for f in os.listdir(frames_in)
                if f.endswith(f'.{FRAME_FORMAT}')
            ])
            self._log(f"Extracted {frame_count} frames")

            # ── Step 3: AI upscale ──
            self._progress(15, "AI upscaling frames...")
            if not self._upscale_frames(binary, frames_in, frames_out, model_info,
                                         frame_count):
                return False
            if self._cancelled:
                return False

            # ── Step 4: Reassemble ──
            self._progress(90, "Reassembling video...")
            if not self._reassemble(frames_out, fps, model_info['scale'],
                                     src_w, src_h, frame_count):
                return False

            self._progress(100, "Complete!")
            out_size = os.path.getsize(self.output_path) / 1024 / 1024
            self._log(f"Output: {self.output_path} ({out_size:.0f} MB)", 'SUCCESS')
            return True

        except Exception as e:
            self._log(f"Upscale failed: {e}", 'ERROR')
            return False
        finally:
            # Cleanup temp directory
            if self._temp_dir and os.path.exists(self._temp_dir):
                try:
                    shutil.rmtree(self._temp_dir)
                except OSError:
                    pass

    def _run_proc(self, cmd):
        """Run a cancellable subprocess (ffmpeg); True on success."""
        try:
            self._process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )
            for _line in self._process.stdout:
                if self._cancelled:
                    self._process.kill()
                    return False
            self._process.wait()
            return self._process.returncode == 0
        except Exception as e:
            self._log(f"Subprocess failed: {e}", 'ERROR')
            return False

    @staticmethod
    def _estimate_workdir_bytes(n_frames, src_w, src_h, scale):
        """Rough total size of the input+output PNG frame sets (~2 bytes/px for
        photographic content) — used to decide RAM vs disk for the work dir."""
        in_px = max(1, (src_w or 0) * (src_h or 0))
        out_px = in_px * scale * scale
        return int(max(1, n_frames) * (in_px + out_px) * 2)

    def _make_workdir(self, prefix, n_frames, src_w, src_h, scale):
        """Create the temp frame dir — on the RAM disk when the frame set fits,
        else on /tmp. RAM keeps the GPUs fed (the pipeline is otherwise I/O-bound)."""
        base = _best_temp_dir(
            self._estimate_workdir_bytes(n_frames, src_w, src_h, scale))
        d = tempfile.mkdtemp(prefix=prefix, dir=base)
        self._log("Work dir on " + ("RAM disk (fast)" if base == '/dev/shm' else "disk"))
        return d

    def run_preview(self, start=120.0, duration=30.0):
        """Generate a fast side-by-side (original | AI-upscaled) preview clip.

        Uses the IDENTICAL model / scale / target / encoder as a full job, but on
        a short segment only — so what you see is what the full encode produces.
        Writes a left(original, bicubic) / right(AI-upscaled) hstack to
        self.output_path. The window is clamped to fit short sources.
        """
        binary = get_binary_path()
        if not binary:
            self._log("Real-ESRGAN not installed. Use the download button.", 'ERROR')
            return False
        model_info = MODELS.get(self.model_name)
        if not model_info:
            self._log(f"Unknown model: {self.model_name}", 'ERROR')
            return False
        scale = model_info['scale']

        self._progress(0, "Analyzing video...")
        total_dur, fps, src_w, src_h = self._probe_video()
        if not fps or not total_dur or not src_w:
            self._log("Could not probe video for preview", 'ERROR')
            return False

        # Clamp the preview window to fit the source length
        if total_dur <= duration:
            start, clip = 0.0, max(1.0, total_dur)
        elif start + duration > total_dur:
            start = max(0.0, min(total_dur * 0.1, total_dur - duration))
            clip = duration
        else:
            clip = duration
        self._log(
            f"Preview: {clip:.0f}s @ {start:.0f}s  "
            f"(source {src_w}x{src_h} @ {fps:.2f}fps, {total_dur:.0f}s total)"
        )

        self._temp_dir = self._make_workdir(
            'docflix_preview_', int(clip * fps), src_w, src_h, scale)
        frames_in = os.path.join(self._temp_dir, 'frames_in')
        frames_out = os.path.join(self._temp_dir, 'frames_out')
        os.makedirs(frames_in)
        os.makedirs(frames_out)

        try:

            # Per-panel dimensions = exactly what the full run would output
            if self.target_height:
                panel_h = int(self.target_height)
                panel_w = int(round(src_w * panel_h / src_h)) // 2 * 2
            else:
                panel_h = src_h * scale
                panel_w = src_w * scale

            # ── Extract just the segment (fast -ss before -i) ──
            self._progress(5, "Extracting preview segment...")
            extract = [
                'ffmpeg', '-y', '-ss', f'{start:.3f}', '-t', f'{clip:.3f}',
                '-i', self.input_path, '-vsync', '0',
                '-compression_level', '1',  # fast PNG: ~half the CPU, still lossless
                os.path.join(frames_in, f'frame_%08d.{FRAME_FORMAT}'),
            ]
            if not self._run_proc(extract):
                self._log("Preview segment extract failed", 'ERROR')
                return False
            if self._cancelled:
                return False
            fcount = len([f for f in os.listdir(frames_in)
                          if f.endswith(f'.{FRAME_FORMAT}')])
            if fcount == 0:
                self._log("No frames extracted for preview", 'ERROR')
                return False

            # ── Upscale the segment (multi-GPU applies automatically) ──
            self._progress(15, "Upscaling preview...")
            if not self._upscale_frames(binary, frames_in, frames_out,
                                        model_info, fcount):
                return False
            if self._cancelled:
                return False

            # ── Stitch side-by-side: original(bicubic) | AI-upscaled ──
            self._progress(92, "Building side-by-side...")
            enc = self.NVENC_ENCODERS.get(self.video_encoder)
            if enc:
                enc_args = [enc['preset_flag'], enc.get('preset_default', 'p5')]
                enc_args += enc['quality_args']
                pix = enc['pix_fmt']
            else:
                enc_args = ['-preset', str(self.preset), '-crf', str(self.crf)]
                pix = 'yuv420p'
            filt = (
                f"[1:v]scale={panel_w}:{panel_h}:flags=bicubic,setsar=1,fps={fps}[orig];"
                f"[0:v]scale={panel_w}:{panel_h}:flags=lanczos,setsar=1,fps={fps}[up];"
                f"[orig][up]hstack=inputs=2[out]"
            )
            build = [
                'ffmpeg', '-y',
                '-framerate', f'{fps}',
                '-i', os.path.join(frames_out, f'frame_%08d.{FRAME_FORMAT}'),
                '-ss', f'{start:.3f}', '-t', f'{clip:.3f}', '-i', self.input_path,
                '-filter_complex', filt, '-map', '[out]',
                '-c:v', self.video_encoder, *enc_args, '-pix_fmt', pix,
                '-an', '-sn',
                self.output_path,
            ]
            if not self._run_proc(build):
                self._log("Preview side-by-side build failed", 'ERROR')
                return False

            self._progress(100, "Preview ready")
            self._log(f"Preview saved: {self.output_path}", 'SUCCESS')
            return True
        finally:
            if self._temp_dir and os.path.isdir(self._temp_dir):
                shutil.rmtree(self._temp_dir, ignore_errors=True)

    def _probe_video(self):
        """Probe input video for duration, fps, width, height."""
        cmd = [
            'ffprobe', '-v', 'quiet', '-print_format', 'json',
            '-show_format', '-show_streams', '-select_streams', 'v:0',
            self.input_path,
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            data = json.loads(r.stdout)
            stream = data.get('streams', [{}])[0]
            fmt = data.get('format', {})

            w = stream.get('width')
            h = stream.get('height')
            dur = float(fmt.get('duration', 0))

            # Parse FPS from r_frame_rate (e.g., "24000/1001")
            fps_str = stream.get('r_frame_rate', '0/1')
            if '/' in fps_str:
                num, den = fps_str.split('/')
                fps = float(num) / float(den) if float(den) > 0 else 0
            else:
                fps = float(fps_str)

            return dur, fps, w, h
        except Exception as e:
            self._log(f"ffprobe failed: {e}", 'ERROR')
            return None, None, None, None

    def _extract_frames(self, out_dir, fps, total_frames):
        """Extract all frames from the video as PNG images."""
        cmd = ['ffmpeg', '-y']
        # GPU-accelerated decode (NVDEC) when a CUDA GPU is selected — offloads the heavy
        # HEVC/H.264 decode off the CPU. PNG writing still happens on the CPU.
        _dec_ids = [i for i in (self.gpu_ids or []) if i is not None and int(i) >= 0]
        if _dec_ids:
            cmd += ['-hwaccel', 'cuda', '-hwaccel_device', str(_dec_ids[0])]
        cmd += [
            '-i', self.input_path,
            '-vsync', '0',
            '-frame_pts', '1',
            '-compression_level', '1',  # fast PNG: ~half the CPU vs default, still lossless
            os.path.join(out_dir, f'frame_%08d.{FRAME_FORMAT}'),
        ]
        self._log(f"Extracting frames to {out_dir}...")
        try:
            self._process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True,
            )
            for line in self._process.stdout:
                if self._cancelled:
                    self._process.kill()
                    return False
                # Parse frame count from ffmpeg output
                m = re.search(r'frame=\s*(\d+)', line)
                if m:
                    frame_num = int(m.group(1))
                    # Extraction is 2-15% of total progress
                    pct = 2 + min(13, (frame_num / max(1, total_frames)) * 13)
                    self._progress(pct, f"Extracting frame {frame_num}...")
            self._process.wait()
            return self._process.returncode == 0
        except Exception as e:
            self._log(f"Frame extraction failed: {e}", 'ERROR')
            return False

    def _gpu_args(self):
        """Build Real-ESRGAN -g/-j args.

        -g selects the GPU(s) (comma list for multi). -j load:proc:save tunes
        threads: load/save do the CPU-side PNG decode/encode — the REAL
        bottleneck (with too few, the GPUs starve waiting on frames). We scale
        load/save to the core count so both cards stay fed; proc = per-GPU workers.
        Measured ~4x faster than the old 2:2,2:2 on a 16-core/32-thread box.
            2 GPUs, 32 threads -> -g 0,1  -j 16:4,4:16
            1 GPU              -> -g 0     -j 16:4:16
            CPU                -> -g -1
        """
        ids = self.gpu_ids or [0]
        args = ['-g', ','.join(str(i) for i in ids)]
        if -1 not in ids:
            cores = os.cpu_count() or 8
            io = min(16, max(4, cores // 2))      # load == save (PNG codec threads)
            proc = ','.join(['4'] * len(ids))     # GPU worker threads per card
            args += ['-j', f'{io}:{proc}:{io}']
        return args

    def _upscale_frames(self, binary, in_dir, out_dir, model_info, total_frames):
        """Run Real-ESRGAN on extracted frames."""
        model_id = model_info['id']
        scale = model_info['scale']

        cmd = [
            binary,
            '-i', in_dir,
            '-o', out_dir,
            '-n', model_id,
            '-s', str(scale),
            '-f', FRAME_FORMAT,
        ]
        cmd += self._gpu_args()
        if self.tta:
            cmd.append('-x')  # TTA: 8-orientation averaging — higher quality, ~8x slower

        # Add model path if using local install
        models_dir = INSTALL_DIR / 'models'
        if models_dir.exists():
            cmd.extend(['-m', str(models_dir)])

        self._log(f"AI upscaling with {model_id} ({scale}x)...")
        self._log(f"  Command: {' '.join(cmd)}")

        try:
            # Progress is driven by the OUTPUT frame count (monotonic + accurate),
            # NOT Real-ESRGAN's stdout %, which cycles per-thread under multi-GPU
            # and makes the bar appear to loop. stdout/stderr are discarded so the
            # pipe can't fill and stall the process.
            self._process = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )

            t0 = time.monotonic()
            processed = 0
            while self._process.poll() is None:
                if self._cancelled:
                    self._process.kill()
                    return False
                processed = len([f for f in os.listdir(out_dir)
                                 if f.endswith(f'.{FRAME_FORMAT}')])
                elapsed = time.monotonic() - t0
                fps_rate = processed / elapsed if elapsed > 0 else 0
                remaining = (total_frames - processed) / fps_rate if fps_rate > 0 else 0
                # Upscaling is 15-90% of total progress
                pct = 15 + (min(processed, total_frames) / max(1, total_frames)) * 75
                self._progress(pct, self._format_eta(
                    processed, total_frames, fps_rate, remaining,
                ))
                time.sleep(0.5)

            if self._process.returncode != 0:
                self._log("Real-ESRGAN exited with error", 'ERROR')
                return False

            processed = len([f for f in os.listdir(out_dir)
                             if f.endswith(f'.{FRAME_FORMAT}')])
            self._log(
                f"Upscaled {processed} frames in {time.monotonic() - t0:.1f}s "
                f"({processed / max(1, time.monotonic() - t0):.1f} fps)"
            )
            return True

        except Exception as e:
            self._log(f"AI upscale failed: {e}", 'ERROR')
            return False

    def _format_eta(self, done, total, fps, remaining_secs):
        """Format a progress/ETA string."""
        pct = (done / max(1, total)) * 100
        parts = [f"Frame {done}/{total} ({pct:.0f}%)"]
        if fps > 0:
            parts.append(f"{fps:.1f} fps")
        if remaining_secs > 0:
            if remaining_secs >= 3600:
                parts.append(f"~{remaining_secs / 3600:.1f}h left")
            elif remaining_secs >= 60:
                parts.append(f"~{int(remaining_secs // 60)}m {int(remaining_secs % 60)}s left")
            else:
                parts.append(f"~{int(remaining_secs)}s left")
        return ' — '.join(parts)

    def _reassemble(self, frames_dir, fps, scale_factor, src_w, src_h,
                    total_frames):
        """Reassemble upscaled frames with original audio/subs into output video."""
        # Calculate output resolution
        out_w = src_w * scale_factor
        out_h = src_h * scale_factor

        # If target_height is set, scale down after AI upscale
        # e.g., 480p source → 4x = 1920p → scale to 1080p
        scale_filter = ''
        if self.target_height and self.target_height < out_h:
            target_w = int(round(src_w * self.target_height / src_h))
            if target_w % 2 != 0:
                target_w += 1
            target_h = self.target_height
            if target_h % 2 != 0:
                target_h += 1
            scale_filter = f',scale={target_w}:{target_h}'
            out_w, out_h = target_w, target_h
            self._log(
                f"Post-upscale resize: {src_w * scale_factor}x{src_h * scale_factor} "
                f"→ {out_w}x{out_h}"
            )

        self._log(f"Reassembling: {out_w}x{out_h} @ {fps:.2f} fps")

        # Video encoder + pixel format (needed up-front for the filter graph)
        is_nvenc = self.video_encoder in self.NVENC_ENCODERS
        nvenc_cfg = self.NVENC_ENCODERS.get(self.video_encoder, {})
        pix_fmt = nvenc_cfg.get('pix_fmt', 'yuv420p') if is_nvenc else 'yuv420p'

        # "Strength" — blend the AI result back over a plain Lanczos upscale of the
        # ORIGINAL frames, so cartoons keep some native grain/texture instead of being
        # fully scrubbed ("freshen, don't redraw"). 100 = full AI (default, unchanged).
        blend = max(0, min(100, int(getattr(self, 'strength', 100)))) / 100.0
        use_blend = blend < 0.999

        cmd = [
            'ffmpeg', '-y',
            # Input 0: upscaled (AI) frames
            '-framerate', str(fps),
            '-i', os.path.join(frames_dir, f'frame_%08d.{FRAME_FORMAT}'),
            # Input 1: original file (for audio/subs/chapters)
            '-i', self.input_path,
        ]
        if use_blend:
            ai_w, ai_h = src_w * scale_factor, src_h * scale_factor
            # Input 2: the original extracted frames (still on disk) — Lanczos up to AI
            # resolution, then weighted-blend with the AI frames.
            cmd += ['-framerate', str(fps),
                    '-i', os.path.join(self._temp_dir, 'frames_in',
                                       f'frame_%08d.{FRAME_FORMAT}')]
            fc = (f"[2:v]scale={ai_w}:{ai_h}:flags=lanczos,setsar=1[orig];"
                  f"[0:v][orig]blend=all_expr=A*{blend:.4f}+B*{1.0 - blend:.4f}[mix];"
                  f"[mix]format={pix_fmt}{scale_filter}[vout]")
            cmd += ['-filter_complex', fc,
                    '-map', '[vout]', '-map', '1:a?', '-map', '1:s?']
            self._log(f"  Strength {int(round(blend * 100))}% — blending AI over original grain")
        else:
            cmd += ['-map', '0:v:0', '-map', '1:a?', '-map', '1:s?',
                    '-vf', f'format={pix_fmt}{scale_filter}']

        cmd.extend(['-c:v', self.video_encoder])

        if is_nvenc:
            # NVENC hardware encoding — use quality args (VBR + CQ + lookahead + AQ)
            preset_flag = nvenc_cfg.get('preset_flag', '-preset')
            nvenc_preset = self.preset if self.preset.startswith('p') else nvenc_cfg.get('preset_default', 'p5')
            cmd.extend([preset_flag, nvenc_preset])
            # Quality: use CRF value as CQ level, plus NVENC quality tuning
            quality_args = list(nvenc_cfg.get('quality_args', []))
            # Override -cq value with user's CRF setting (maps 1:1 for visual quality)
            for i, arg in enumerate(quality_args):
                if arg == '-cq' and i + 1 < len(quality_args):
                    quality_args[i + 1] = str(self.crf)
                    break
            cmd.extend(quality_args)
            self._log(f"  NVENC: {self.video_encoder}, preset={nvenc_preset}, cq={self.crf}")
        else:
            # CPU encoding (libx265, libx264, svtav1)
            cmd.extend(['-preset', self.preset])
            if self.video_encoder in ('libx265', 'libx264', 'libsvtav1'):
                cmd.extend(['-crf', self.crf])

        # Audio
        if self.audio_codec == 'copy':
            cmd.extend(['-c:a', 'copy'])
        else:
            cmd.extend(['-c:a', self.audio_codec])
            if self.audio_codec not in ('flac',):
                cmd.extend(['-b:a', '128k'])

        # Copy subtitles
        cmd.extend(['-c:s', 'copy'])

        # Copy chapters from original
        cmd.extend(['-map_chapters', '1'])

        # Output
        cmd.append(self.output_path)

        self._log(f"  {' '.join(cmd)}")

        try:
            self._process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True,
            )
            for line in self._process.stdout:
                if self._cancelled:
                    self._process.kill()
                    return False
                m = re.search(r'frame=\s*(\d+)', line)
                if m:
                    frame_num = int(m.group(1))
                    frac = min(1.0, frame_num / max(1, total_frames))
                    pct = 90 + frac * 9  # 90 → 99 across the whole encode
                    if frac >= 0.99:
                        self._progress(
                            pct, "Finalizing — writing file to disk…")
                    else:
                        self._progress(pct, f"Encoding frame {frame_num}...")
            self._process.wait()
            return self._process.returncode == 0
        except Exception as e:
            self._log(f"Reassembly failed: {e}", 'ERROR')
            return False


# ═══════════════════════════════════════════════════════════════════
# Convenience function for non-GUI usage
# ═══════════════════════════════════════════════════════════════════

def detect_best_encoder():
    """Detect the best available video encoder. Prefers NVENC over CPU.

    Returns:
        tuple: (encoder_name, preset, description)
    """
    # Check for NVENC
    try:
        r = subprocess.run(
            ['ffmpeg', '-hide_banner', '-encoders'],
            capture_output=True, text=True, timeout=10,
        )
        output = r.stdout
        if 'hevc_nvenc' in output:
            return ('hevc_nvenc', 'p5', 'NVIDIA NVENC H.265')
        if 'h264_nvenc' in output:
            return ('h264_nvenc', 'p5', 'NVIDIA NVENC H.264')
    except Exception:
        pass
    # Fallback to CPU
    return ('libx265', 'medium', 'CPU x265')


def upscale_video(input_path, output_path, model='General (x4) Fast',
                  target_height=None, encoder=None, crf='18',
                  preset=None, gpu_id=0):
    """Upscale a video file using AI. Simple wrapper for scripts/CLI.

    If encoder is None, auto-detects the best available (NVENC preferred).
    Returns True on success, False on failure.
    """
    if not is_installed():
        print("Real-ESRGAN not installed. Installing...")
        download_and_install(
            progress_callback=lambda p, s: print(f"  [{p:.0f}%] {s}"),
            log_callback=lambda m, l: print(f"  [{l}] {m}"),
        )

    # Auto-detect best encoder if not specified
    if encoder is None:
        encoder, auto_preset, desc = detect_best_encoder()
        if preset is None:
            preset = auto_preset
        print(f"  [INFO] Using encoder: {desc} ({encoder})")
    if preset is None:
        preset = 'medium'

    job = AIUpscaleJob(
        input_path=input_path,
        output_path=output_path,
        model_name=model,
        target_height=target_height,
        video_encoder=encoder,
        crf=crf,
        preset=preset,
        gpu_id=gpu_id,
        log_callback=lambda m, l: print(f"  [{l}] {m}"),
        progress_callback=lambda p, s: print(f"  [{p:.0f}%] {s}"),
    )
    return job.run()
