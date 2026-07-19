"""
Docflix Media Suite — PyTorch/CUDA AI Upscaler (fast engine, NVIDIA only).

An OPTIONAL alternative to the ncnn-vulkan engine (ai_upscaler.py). Same job
interface (TorchUpscaleJob mirrors AIUpscaleJob) so the GUI can pick either one.

Why it exists: measured ~3.9x faster than ncnn-vulkan on an RTX 2000E Ada —
because it runs the upscale on the tensor cores in fp16 AND streams frames
straight through ffmpeg (NVDEC decode → GPU → NVENC encode) with NO PNG round-
trip. A 20-min cartoon that took 90+ min on two cards lands in ~20-25 min.

Portability: ncnn-vulkan stays the universal default (any Vulkan GPU, zero deps).
THIS engine needs an NVIDIA card + a torch-capable Python. We keep torch OUT of
the main GUI process by running the upscale in a subprocess (torch_upscale_worker.py)
under whatever interpreter has torch+CUDA+spandrel — an existing one we auto-detect,
or a dedicated venv the one-click installer builds. A CUDA crash can't take the app down.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.request import urlretrieve
from urllib.error import URLError

from .ai_upscaler import detect_gpus, normalize_gpu_ids

# Reuse the same install dir as ncnn — .pth models live alongside the .bin/.param.
INSTALL_DIR = Path.home() / '.local' / 'share' / 'docflix' / 'realesrgan'
VENV_DIR = Path.home() / '.local' / 'share' / 'docflix' / 'torch-engine' / 'venv'

# Torch models (.pth). Keys MATCH ai_upscaler.MODELS so the one AI-model dropdown
# drives either engine. Each maps to a spandrel-loadable Real-ESRGAN checkpoint.
# Keys/order MUST mirror ai_upscaler.MODELS so the one AI-model dropdown drives either
# engine. Each maps to a spandrel-loadable Real-ESRGAN .pth checkpoint.
MODELS = {
    'Cartoon/Anime Video (fast)': {
        'file': 'realesr-animevideov3.pth', 'scale': 4,
        'url': ('https://github.com/xinntao/Real-ESRGAN/releases/download/'
                'v0.2.5.0/realesr-animevideov3.pth'),
    },
    'Photo / Live-action': {
        'file': 'RealESRGAN_x4plus.pth', 'scale': 4,
        'url': ('https://github.com/xinntao/Real-ESRGAN/releases/download/'
                'v0.1.0/RealESRGAN_x4plus.pth'),
    },
    'Anime Stills (slow, best)': {
        'file': 'RealESRGAN_x4plus_anime_6B.pth', 'scale': 4,
        'url': ('https://github.com/xinntao/Real-ESRGAN/releases/download/'
                'v0.2.2.4/RealESRGAN_x4plus_anime_6B.pth'),
    },
}
DEFAULT_MODEL = 'Cartoon/Anime Video (fast)'

# Candidate interpreters to probe for torch+CUDA+spandrel, best-effort order:
#   1. our managed venv  2. a ComfyUI venv (common)  3. the running interpreter
_PY_CANDIDATES = [
    VENV_DIR / 'bin' / 'python',
    Path.home() / 'venvs' / 'comfyui' / 'bin' / 'python',
    Path(sys.executable),
]

_PROBE = ("import torch,spandrel,sys;"
          "sys.stdout.write('OK' if torch.cuda.is_available() else 'NOCUDA')")

_cached_python = None  # memoized torch-python path (str) once found


def _worker_path():
    return str(Path(__file__).with_name('torch_upscale_worker.py'))


def _probe_python(py):
    """True if interpreter `py` can import torch+spandrel AND sees a CUDA device."""
    try:
        r = subprocess.run([str(py), '-c', _PROBE],
                           capture_output=True, text=True, timeout=60)
        return r.returncode == 0 and r.stdout.strip() == 'OK'
    except (OSError, subprocess.SubprocessError):
        return False


def find_torch_python(force=False):
    """Return a torch+CUDA+spandrel-capable interpreter path, or None.

    Result is memoized (pass force=True to re-probe, e.g. after install()).
    """
    global _cached_python
    if _cached_python and not force:
        return _cached_python
    _cached_python = None
    for cand in _PY_CANDIDATES:
        if cand and Path(cand).exists() and _probe_python(cand):
            _cached_python = str(cand)
            break
    return _cached_python


def models_present(model_name=None):
    """True if the .pth for model_name (or the default) is on disk."""
    info = MODELS.get(model_name or DEFAULT_MODEL)
    return bool(info) and (INSTALL_DIR / info['file']).exists()


def model_path(model_name):
    """Absolute path to the model .pth (may not exist yet)."""
    info = MODELS.get(model_name or DEFAULT_MODEL)
    if not info:
        return None
    return str(INSTALL_DIR / info['file'])


def is_available(model_name=None):
    """Ready to run right now: torch-python found, an NVIDIA GPU present, model on disk."""
    return bool(find_torch_python()) and bool(detect_gpus()) and models_present(model_name)


def engine_status():
    """Rich status for the GUI: what's present and what's missing."""
    py = find_torch_python()
    gpus = detect_gpus()
    have_models = models_present()
    missing = []
    if not py:
        missing.append('torch runtime')
    if not gpus:
        missing.append('NVIDIA GPU')
    if not have_models:
        missing.append('model weights')
    return {
        'python': py, 'gpus': gpus, 'models_present': have_models,
        'ready': bool(py and gpus and have_models), 'missing': missing,
    }


# ═══════════════════════════════════════════════════════════════════
# One-click install: (a) model weights, (b) a torch venv if none found
# ═══════════════════════════════════════════════════════════════════

def download_models(model_names=None, progress_callback=None, log_callback=None):
    """Download the .pth weights for the given models (default: just the fast one)."""
    def _log(m, l='INFO'):
        if log_callback:
            log_callback(m, l)

    def _prog(p, s):
        if progress_callback:
            progress_callback(p, s)

    names = model_names or [DEFAULT_MODEL]
    INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    for i, name in enumerate(names):
        info = MODELS.get(name)
        if not info:
            continue
        dest = INSTALL_DIR / info['file']
        if dest.exists():
            _log(f"Model already present: {info['file']}")
            continue
        _log(f"Downloading {info['file']}...")

        def _hook(bn, bs, ts, _base=i, _n=len(names)):
            if ts > 0:
                frac = min(1.0, bn * bs / ts)
                _prog((_base + frac) / _n * 100,
                      f"Downloading {info['file']}: {bn * bs / 1e6:.0f}/{ts / 1e6:.0f} MB")
        try:
            urlretrieve(info['url'], str(dest), reporthook=_hook)
        except (URLError, OSError) as e:
            dest.unlink(missing_ok=True)
            raise RuntimeError(f"Download failed for {info['file']}: {e}")
        _log(f"Installed {info['file']}", 'SUCCESS')
    return True


def install(model_names=None, progress_callback=None, log_callback=None):
    """One-click setup for the fast engine.

    1. If no torch-capable Python is found, build a dedicated venv and pip-install
       torch + spandrel (+ pillow, numpy). torch ships CUDA wheels on Linux, so a
       plain install lands a CUDA build for most NVIDIA setups.
    2. Download the model weights.

    Returns the torch-python path on success. Raises RuntimeError on failure.
    """
    def _log(m, l='INFO'):
        if log_callback:
            log_callback(m, l)

    def _prog(p, s):
        if progress_callback:
            progress_callback(p, s)

    py = find_torch_python()
    if not py:
        _log("No torch runtime found — building a dedicated venv (this is the big, "
             "one-time step; torch is ~2 GB)...", 'INFO')
        _prog(2, "Creating Python venv...")
        VENV_DIR.parent.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run([sys.executable, '-m', 'venv', str(VENV_DIR)],
                           check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"venv creation failed: {e.stderr or e}")
        venv_py = str(VENV_DIR / 'bin' / 'python')
        _prog(8, "Installing torch + spandrel (several minutes)...")
        _log("pip install torch spandrel pillow numpy — streaming below:", 'INFO')
        proc = subprocess.Popen(
            [venv_py, '-m', 'pip', 'install', '--upgrade',
             'torch', 'spandrel', 'pillow', 'numpy'],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                _log("  " + line)
        proc.wait()
        if proc.returncode != 0:
            raise RuntimeError("pip install failed — see log above")
        if not _probe_python(venv_py):
            raise RuntimeError(
                "Installed torch, but CUDA is not visible to it. Your NVIDIA driver "
                "may be too old for this torch build, or no NVIDIA GPU is present.")
        py = find_torch_python(force=True)
        _log("Torch runtime ready.", 'SUCCESS')
    else:
        _log(f"Using existing torch runtime: {py}")

    _prog(85, "Downloading model weights...")
    download_models(model_names or [DEFAULT_MODEL],
                    progress_callback=lambda p, s: _prog(85 + p * 0.15, s),
                    log_callback=log_callback)
    _prog(100, "Fast engine ready!")
    return py


# ═══════════════════════════════════════════════════════════════════
# The streamed job — mirrors ai_upscaler.AIUpscaleJob's interface
# ═══════════════════════════════════════════════════════════════════

class TorchUpscaleJob:
    """Streamed PyTorch/CUDA upscale of one video. Same call surface as AIUpscaleJob.

    Fans out one worker subprocess per selected GPU (each on an exact frame range),
    concatenates the segments, then muxes the original audio/subs/chapters back in.
    """

    def __init__(self, input_path, output_path, model_name=None,
                 target_height=None, video_encoder='hevc_nvenc',
                 crf='18', preset='p5', audio_codec='copy',
                 gpu_id=0, tta=False, strength=100, batch=1,
                 log_callback=None, progress_callback=None):
        self.input_path = str(input_path)
        self.output_path = str(output_path)
        self.model_name = model_name or DEFAULT_MODEL
        self.target_height = target_height
        self.video_encoder = video_encoder if 'nvenc' in video_encoder else 'hevc_nvenc'
        self.crf = str(crf)
        self.preset = preset if str(preset).startswith('p') else 'p5'
        self.audio_codec = audio_codec
        self.gpu_ids = [g for g in normalize_gpu_ids(gpu_id) if g >= 0] or \
                       [g['index'] for g in detect_gpus()[:1]] or [0]
        self.tta = bool(tta)          # not used by this engine (kept for interface parity)
        self.strength = strength
        self.batch = max(1, int(batch))
        self._log_cb = log_callback
        self._progress_cb = progress_callback
        self._cancelled = False
        self._procs = []
        self._temp_dir = None

    # ── interface parity with AIUpscaleJob ──
    def cancel(self):
        self._cancelled = True
        for p in self._procs:
            try:
                p.kill()
            except OSError:
                pass

    def _log(self, msg, level='INFO'):
        if self._log_cb:
            self._log_cb(msg, level)

    def _progress(self, pct, status=''):
        if self._progress_cb:
            self._progress_cb(pct, status)

    def _pix_fmt(self):
        # 10-bit for HEVC/AV1 (matches ncnn path), 8-bit for H.264.
        return 'yuv420p' if self.video_encoder == 'h264_nvenc' else 'p010le'

    def _probe(self):
        """(duration, fps, w, h) via ffprobe — mirrors ai_upscaler._probe_video."""
        cmd = ['ffprobe', '-v', 'quiet', '-print_format', 'json',
               '-show_format', '-show_streams', '-select_streams', 'v:0',
               self.input_path]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            data = json.loads(r.stdout)
            st = data.get('streams', [{}])[0]
            fmt = data.get('format', {})
            w, h = st.get('width'), st.get('height')
            dur = float(fmt.get('duration', 0))
            fr = st.get('r_frame_rate', '0/1')
            if '/' in fr:
                num, den = fr.split('/')
                fps = float(num) / float(den) if float(den) else 0
            else:
                fps = float(fr)
            return dur, fps, w, h
        except Exception as e:  # noqa: BLE001
            self._log(f"ffprobe failed: {e}", 'ERROR')
            return None, None, None, None

    def _ranges(self, total, n):
        """Split [0,total) into n contiguous frame ranges. Last one runs to EOF (-1)."""
        if n <= 1:
            return [(0, -1)]
        step = total // n
        out = []
        for i in range(n):
            start = i * step
            end = -1 if i == n - 1 else (i + 1) * step - 1
            out.append((start, end))
        return out

    def run(self):
        """Execute the streamed upscale. Returns True on success."""
        py = find_torch_python()
        if not py:
            self._log("Fast engine unavailable — no torch runtime. Use Install.", 'ERROR')
            return False
        info = MODELS.get(self.model_name)
        if not info:
            self._log(f"Unknown model: {self.model_name}", 'ERROR')
            return False
        pth = INSTALL_DIR / info['file']
        if not pth.exists():
            self._log(f"Model weights missing: {info['file']}. Use Install.", 'ERROR')
            return False

        self._progress(0, "Analyzing video...")
        dur, fps, w, h = self._probe()
        if not fps or not dur or not w:
            self._log("Could not probe video", 'ERROR')
            return False
        total = int(round(dur * fps))
        gpus = self.gpu_ids
        self._log(f"Source {w}x{h} @ {fps:.2f}fps, ~{total} frames — "
                  f"fast engine on GPU(s) {','.join(map(str, gpus))}")

        self._temp_dir = tempfile.mkdtemp(prefix='docflix_torch_')
        try:
            segs = self._run_workers(py, str(pth), info['scale'], w, h, fps,
                                     total, gpus)
            if segs is None:
                return False
            self._progress(92, "Muxing audio, subtitles, chapters...")
            if not self._mux(segs):
                return False
            self._progress(100, "Complete!")
            sz = os.path.getsize(self.output_path) / 1e6
            self._log(f"Output: {self.output_path} ({sz:.0f} MB)", 'SUCCESS')
            return True
        except Exception as e:  # noqa: BLE001
            self._log(f"Fast upscale failed: {e}", 'ERROR')
            return False
        finally:
            if self._temp_dir and os.path.isdir(self._temp_dir):
                shutil.rmtree(self._temp_dir, ignore_errors=True)

    def _run_workers(self, py, pth, scale, w, h, fps, total, gpus):
        """Spawn one worker per GPU, aggregate live progress, return segment paths."""
        ranges = self._ranges(total, len(gpus))
        worker = _worker_path()
        procs, seg_paths, counts, expected = [], [], {}, {}
        for i, (gpu, (f0, f1)) in enumerate(zip(gpus, ranges)):
            seg = os.path.join(self._temp_dir, f'seg_{i:02d}.ts')
            seg_paths.append(seg)
            exp = (total - f0) if f1 < 0 else (f1 - f0 + 1)
            expected[i] = max(1, exp)
            job = {
                'input': self.input_path, 'output': seg, 'model_pth': pth,
                'scale': scale, 'in_w': w, 'in_h': h, 'fps': f"{fps}",
                'frame_start': f0, 'frame_end': f1, 'batch': self.batch,
                'strength': int(self.strength), 'target_h': self.target_height,
                'encoder': self.video_encoder, 'cq': self.crf, 'preset': self.preset,
                'pix_fmt': self._pix_fmt(), 'expected_frames': expected[i],
            }
            job_file = os.path.join(self._temp_dir, f'job_{i:02d}.json')
            with open(job_file, 'w') as jf:
                json.dump(job, jf)
            env = dict(os.environ, CUDA_VISIBLE_DEVICES=str(gpu))
            self._log(f"  GPU {gpu}: frames {f0}..{'end' if f1 < 0 else f1} → seg_{i:02d}")
            p = subprocess.Popen([py, worker, job_file], env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, bufsize=1)
            procs.append(p)
            counts[i] = 0
        self._procs = procs

        # Drain all workers' stdout concurrently via threads → shared counts.
        import threading
        errors = {}

        def _drain(idx, proc):
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                t = msg.get('t')
                if t == 'progress':
                    counts[idx] = msg.get('frame', counts[idx])
                elif t == 'error':
                    errors[idx] = msg.get('msg', 'unknown error')
                elif t == 'done':
                    counts[idx] = msg.get('frames', counts[idx])
            proc.wait()

        threads = [threading.Thread(target=_drain, args=(i, p), daemon=True)
                   for i, p in enumerate(procs)]
        for th in threads:
            th.start()

        t0 = time.monotonic()
        exp_total = sum(expected.values())
        while any(th.is_alive() for th in threads):
            if self._cancelled:
                for p in procs:
                    try:
                        p.kill()
                    except OSError:
                        pass
                self._log("Cancelled.", 'WARNING')
                return None
            done = sum(counts.values())
            el = time.monotonic() - t0
            rate = done / el if el > 0 else 0
            eta = (exp_total - done) / rate if rate > 0 else 0
            pct = 2 + min(88.0, done / max(1, exp_total) * 88.0)
            self._progress(pct, self._fmt_eta(done, exp_total, rate, eta))
            time.sleep(0.4)
        for th in threads:
            th.join(timeout=5)

        if errors:
            for idx, msg in errors.items():
                self._log(f"  worker {idx} error: {msg}", 'ERROR')
            return None
        if any(p.returncode not in (0, None) for p in procs):
            self._log("A worker exited with an error", 'ERROR')
            return None
        total_done = sum(counts.values())
        self._log(f"Upscaled {total_done} frames in {time.monotonic() - t0:.1f}s "
                  f"({total_done / max(0.1, time.monotonic() - t0):.1f} fps combined)",
                  'SUCCESS')
        return seg_paths

    def _fmt_eta(self, done, total, fps, remaining):
        pct = done / max(1, total) * 100
        parts = [f"Frame {done}/{total} ({pct:.0f}%)"]
        if fps > 0:
            parts.append(f"{fps:.1f} fps")
        if remaining > 0:
            if remaining >= 3600:
                parts.append(f"~{remaining / 3600:.1f}h left")
            elif remaining >= 60:
                parts.append(f"~{int(remaining // 60)}m {int(remaining % 60)}s left")
            else:
                parts.append(f"~{int(remaining)}s left")
        return ' — '.join(parts)

    def _mux(self, segs):
        """Concat the per-GPU segments (if >1) and mux original audio/subs/chapters."""
        cmd = ['ffmpeg', '-y']
        if len(segs) > 1:
            listfile = os.path.join(self._temp_dir, 'segs.txt')
            with open(listfile, 'w') as lf:
                for s in segs:
                    lf.write(f"file '{s}'\n")
            cmd += ['-f', 'concat', '-safe', '0', '-i', listfile]
        else:
            cmd += ['-i', segs[0]]
        cmd += ['-i', self.input_path,
                '-map', '0:v:0', '-map', '1:a?', '-map', '1:s?',
                '-c', 'copy', '-map_chapters', '1']
        if self.audio_codec != 'copy':
            cmd += ['-c:a', self.audio_codec]
            if self.audio_codec not in ('flac',):
                cmd += ['-b:a', '128k']
        cmd.append(self.output_path)
        self._log("  mux: " + ' '.join(cmd))
        try:
            self._procs = [subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                            stderr=subprocess.STDOUT, text=True)]
            for _line in self._procs[0].stdout:
                if self._cancelled:
                    self._procs[0].kill()
                    return False
            self._procs[0].wait()
            return self._procs[0].returncode == 0
        except Exception as e:  # noqa: BLE001
            self._log(f"Mux failed: {e}", 'ERROR')
            return False

    def run_preview(self, start=120.0, duration=30.0):
        """Side-by-side (original | AI) preview — same math as a full run, short clip.

        Runs a single worker on a short segment to an upscaled-only temp file, then
        hstacks it against the bicubic-scaled original. Mirrors AIUpscaleJob.run_preview.
        """
        py = find_torch_python()
        info = MODELS.get(self.model_name)
        if not py or not info:
            self._log("Fast engine not ready for preview.", 'ERROR')
            return False
        pth = INSTALL_DIR / info['file']
        if not pth.exists():
            self._log(f"Model weights missing: {info['file']}. Use Install.", 'ERROR')
            return False

        self._progress(0, "Analyzing video...")
        dur, fps, w, h = self._probe()
        if not fps or not dur or not w:
            self._log("Could not probe video for preview", 'ERROR')
            return False
        if dur <= duration:
            start, clip = 0.0, max(1.0, dur)
        elif start + duration > dur:
            start, clip = max(0.0, min(dur * 0.1, dur - duration)), duration
        else:
            clip = duration
        f0 = int(round(start * fps))
        f1 = f0 + int(round(clip * fps)) - 1
        total = f1 - f0 + 1
        scale = info['scale']
        self._log(f"Preview: {clip:.0f}s @ {start:.0f}s ({total} frames)")

        self._temp_dir = tempfile.mkdtemp(prefix='docflix_torch_prev_')
        try:
            seg = os.path.join(self._temp_dir, 'up.ts')
            gpu = self.gpu_ids[0]
            # Upscaled-only panel dims = exactly what a full run makes.
            if self.target_height:
                panel_h = int(self.target_height)
                panel_w = int(round(w * panel_h / h)) // 2 * 2
            else:
                panel_h, panel_w = h * scale, w * scale
            job = {
                'input': self.input_path, 'output': seg, 'model_pth': str(pth),
                'scale': scale, 'in_w': w, 'in_h': h, 'fps': f"{fps}",
                'frame_start': f0, 'frame_end': f1, 'batch': self.batch,
                'strength': int(self.strength), 'target_h': self.target_height,
                'encoder': self.video_encoder, 'cq': self.crf, 'preset': self.preset,
                'pix_fmt': self._pix_fmt(), 'expected_frames': total,
            }
            jf = os.path.join(self._temp_dir, 'job.json')
            with open(jf, 'w') as f:
                json.dump(job, f)
            env = dict(os.environ, CUDA_VISIBLE_DEVICES=str(gpu))
            self._progress(10, "Upscaling preview...")
            p = subprocess.Popen([py, _worker_path(), jf], env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, bufsize=1)
            self._procs = [p]
            err = None
            for line in p.stdout:
                line = line.strip()
                if self._cancelled:
                    p.kill()
                    return False
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if msg.get('t') == 'progress':
                    frac = msg.get('frame', 0) / max(1, total)
                    self._progress(10 + frac * 80, f"Upscaling preview... {int(frac*100)}%")
                elif msg.get('t') == 'error':
                    err = msg.get('msg')
            p.wait()
            if err or p.returncode not in (0, None):
                self._log(f"Preview upscale failed: {err or p.returncode}", 'ERROR')
                return False

            self._progress(92, "Building side-by-side...")
            filt = (f"[1:v]scale={panel_w}:{panel_h}:flags=bicubic,setsar=1[orig];"
                    f"[0:v]scale={panel_w}:{panel_h}:flags=lanczos,setsar=1[up];"
                    f"[orig][up]hstack=inputs=2[out]")
            build = ['ffmpeg', '-y', '-i', seg,
                     '-ss', f'{start:.3f}', '-t', f'{clip:.3f}', '-i', self.input_path,
                     '-filter_complex', filt, '-map', '[out]',
                     '-c:v', self.video_encoder, '-preset', self.preset,
                     '-cq', self.crf, '-pix_fmt', self._pix_fmt(),
                     '-an', '-sn', self.output_path]
            pp = subprocess.Popen(build, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True)
            self._procs = [pp]
            for _l in pp.stdout:
                if self._cancelled:
                    pp.kill()
                    return False
            pp.wait()
            if pp.returncode != 0:
                self._log("Preview side-by-side build failed", 'ERROR')
                return False
            self._progress(100, "Preview ready")
            self._log(f"Preview saved: {self.output_path}", 'SUCCESS')
            return True
        finally:
            if self._temp_dir and os.path.isdir(self._temp_dir):
                shutil.rmtree(self._temp_dir, ignore_errors=True)
