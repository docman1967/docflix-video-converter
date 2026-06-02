"""
Docflix Media Suite — Bitmap Subtitle OCR

Convert PGS/VobSub bitmap subtitles to SRT text via
Tesseract OCR. Single-pass rendering, parallel OCR,
smart cropping, and music note detection.
"""

import glob
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from tkinter import ttk
from concurrent.futures import ThreadPoolExecutor, as_completed

from .utils import get_video_duration, get_all_streams


# ── Map ISO 639-2/B → Tesseract language codes ──
LANG_MAP = {
    'eng': 'eng', 'fre': 'fra', 'fra': 'fra', 'ger': 'deu', 'deu': 'deu',
    'spa': 'spa', 'ita': 'ita', 'por': 'por', 'rus': 'rus', 'jpn': 'jpn',
    'kor': 'kor', 'chi': 'chi_sim', 'zho': 'chi_sim', 'ara': 'ara',
    'hin': 'hin', 'und': 'eng', 'nld': 'nld', 'pol': 'pol', 'tur': 'tur',
    'swe': 'swe', 'nor': 'nor', 'dan': 'dan', 'fin': 'fin',
}


def _auto_install_packages(apt_packages, pip_packages=None, progress_callback=None):
    """Offer to install missing system/pip packages for OCR.

    Shows a tkinter confirmation dialog, then installs via pkexec (graphical
    sudo) for apt packages and pip for Python packages.

    Args:
        apt_packages: List of apt package names to install (e.g. ['tesseract-ocr']).
        pip_packages: List of pip package names to install (e.g. ['pytesseract']).
        progress_callback: Optional callable(message) for status updates.

    Returns:
        True if all installations succeeded, False otherwise.
    """
    from tkinter import messagebox

    all_pkgs = list(apt_packages or []) + list(pip_packages or [])
    if not all_pkgs:
        return True

    pkg_list = ', '.join(all_pkgs)
    msg = (f"The following packages are required for bitmap subtitle OCR "
           f"but are not installed:\n\n"
           f"  {pkg_list}\n\n"
           f"Would you like to install them now?")
    if not messagebox.askyesno("Install OCR Dependencies", msg):
        return False

    # ── Install apt packages ──
    if apt_packages:
        if progress_callback:
            progress_callback(f"Installing system packages: {', '.join(apt_packages)}...")
        # Try pkexec first (graphical polkit prompt), fall back to sudo
        for installer in (['pkexec', 'apt', 'install', '-y'],
                          ['sudo', 'apt', 'install', '-y']):
            cmd = installer + list(apt_packages)
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
                if result.returncode == 0:
                    if progress_callback:
                        progress_callback(f"Installed: {', '.join(apt_packages)}")
                    break
            except FileNotFoundError:
                continue  # pkexec not available, try sudo
            except subprocess.TimeoutExpired:
                if progress_callback:
                    progress_callback("Package installation timed out")
                return False
        else:
            # Neither pkexec nor sudo worked
            if progress_callback:
                progress_callback(f"Failed to install system packages. "
                                  f"Please run manually:\n"
                                  f"  sudo apt install {' '.join(apt_packages)}")
            messagebox.showerror("Installation Failed",
                                 f"Could not install system packages.\n\n"
                                 f"Please run manually in a terminal:\n"
                                 f"  sudo apt install {' '.join(apt_packages)}")
            return False

    # ── Install pip packages ──
    if pip_packages:
        if progress_callback:
            progress_callback(f"Installing Python packages: {', '.join(pip_packages)}...")
        cmd = [sys.executable, '-m', 'pip', 'install'] + list(pip_packages)
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode != 0:
                # Try with --break-system-packages for PEP 668 environments
                cmd += ['--break-system-packages']
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode == 0:
                if progress_callback:
                    progress_callback(f"Installed: {', '.join(pip_packages)}")
            else:
                if progress_callback:
                    progress_callback(f"Failed to install Python packages: {result.stderr[-200:]}")
                messagebox.showerror("Installation Failed",
                                     f"Could not install Python packages.\n\n"
                                     f"Please run manually in a terminal:\n"
                                     f"  pip install {' '.join(pip_packages)}")
                return False
        except subprocess.TimeoutExpired:
            if progress_callback:
                progress_callback("pip install timed out")
            return False

    return True


def _ensure_tesseract_deps(language='eng', progress_callback=None):
    """Check for all Tesseract OCR dependencies and offer to install missing ones.

    Checks for: pytesseract, Pillow, tesseract binary, and the required
    language pack. If anything is missing, prompts the user to install.

    Args:
        language: ISO 639-2 language code (e.g. 'eng', 'fre').
        progress_callback: Optional callable(message) for status updates.

    Returns:
        (True, tess_lang) if all deps are satisfied, (False, None) otherwise.
    """
    tess_lang = LANG_MAP.get(language, language)

    # ── Check Python packages ──
    missing_pip = []
    try:
        import pytesseract  # noqa: F401
    except ImportError:
        missing_pip.append('pytesseract')
    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        missing_pip.append('Pillow')

    if missing_pip:
        if _auto_install_packages([], missing_pip, progress_callback):
            # Re-check after install
            try:
                import pytesseract  # noqa: F401, F811
                from PIL import Image  # noqa: F401, F811
            except ImportError:
                if progress_callback:
                    progress_callback("Python packages still missing after install — cannot OCR")
                return False, None
        else:
            if progress_callback:
                progress_callback("Required Python packages not installed — cannot OCR")
            return False, None

    # ── Check tesseract binary ──
    if not shutil.which('tesseract'):
        apt_pkgs = ['tesseract-ocr', f'tesseract-ocr-{tess_lang}']
        if tess_lang == 'eng':
            apt_pkgs = ['tesseract-ocr', 'tesseract-ocr-eng']
        if _auto_install_packages(apt_pkgs, progress_callback=progress_callback):
            if not shutil.which('tesseract'):
                if progress_callback:
                    progress_callback("tesseract still not found after install — cannot OCR")
                return False, None
        else:
            if progress_callback:
                progress_callback("tesseract not installed — cannot OCR")
            return False, None

    # ── Check language pack ──
    try:
        langs_result = subprocess.run(['tesseract', '--list-langs'],
                                       capture_output=True, text=True, timeout=10)
        available = langs_result.stderr + langs_result.stdout  # varies by version
        if tess_lang not in available:
            apt_pkg = f'tesseract-ocr-{tess_lang}'
            if _auto_install_packages([apt_pkg], progress_callback=progress_callback):
                # Verify it's now available
                langs_result = subprocess.run(['tesseract', '--list-langs'],
                                               capture_output=True, text=True, timeout=10)
                available = langs_result.stderr + langs_result.stdout
                if tess_lang not in available:
                    if progress_callback:
                        progress_callback(f"Tesseract language pack '{tess_lang}' still not "
                                          f"available after install — cannot OCR")
                    return False, None
            else:
                if progress_callback:
                    progress_callback(f"Tesseract language pack '{tess_lang}' not installed — cannot OCR")
                return False, None
    except Exception:
        pass  # proceed anyway — tesseract is installed, lang check is best-effort

    return True, tess_lang


def _ocr_overlay_approach(filepath, stream_index, language, tess_lang,
                          tmpdir, progress_callback=None, frame_callback=None,
                          cancel_event=None):
    """OCR bitmap subtitles (DVB/VobSub) via ffmpeg overlay rendering.

    Renders subtitles on a black canvas at the video's native resolution,
    captures unique frames via scene-change detection, then OCRs each frame
    with Tesseract. Slower than the native PGS parser but works for any
    bitmap subtitle codec that ffmpeg can decode.
    """
    import pytesseract
    from PIL import Image, ImageOps
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import time as _time

    phase1_start = _time.monotonic()

    # ── Pre-check: detect empty subtitle tracks via packet sizes ──
    # DVB subtitle tracks often have keepalive/clear packets (14 bytes)
    # with no actual subtitle content.  MKV stats tags
    # (NUMBER_OF_FRAMES/NUMBER_OF_BYTES) are frequently absent for
    # DVB streams, so the normal empty-track detection in
    # get_all_streams() misses them.  A fast ffprobe packet scan
    # catches it before we waste time on the full overlay render.
    try:
        pkt_cmd = [
            'ffprobe', '-v', 'quiet', '-print_format', 'json',
            '-show_entries', 'packet=size',
            '-select_streams', str(stream_index),
            filepath,
        ]
        pkt_result = subprocess.run(pkt_cmd, capture_output=True,
                                    text=True, timeout=60)
        pkt_data = __import__('json').loads(pkt_result.stdout)
        packets = pkt_data.get('packets', [])
        # DVB clear/keepalive segments are ≤14 bytes.  If every
        # packet in the stream is that small, there's no subtitle
        # content to OCR.
        _DVB_EMPTY_THRESHOLD = 20   # bytes — generous for padding
        if packets and all(int(p.get('size', 0)) <= _DVB_EMPTY_THRESHOLD
                           for p in packets):
            msg = (f"Subtitle track #{stream_index} is empty "
                   f"({len(packets)} packets, all ≤{_DVB_EMPTY_THRESHOLD} "
                   f"bytes — no subtitle content)")
            if progress_callback:
                progress_callback(msg)
            return []
    except Exception:
        pass  # probe failed — fall through to render attempt

    # ── Get video resolution and duration ──
    try:
        probe_cmd = [
            'ffprobe', '-v', 'quiet', '-print_format', 'json',
            '-show_entries', 'stream=width,height,codec_type',
            '-show_entries', 'format=duration',
            filepath,
        ]
        probe_result = subprocess.run(probe_cmd, capture_output=True,
                                      text=True, timeout=30)
        probe_data = __import__('json').loads(probe_result.stdout)
        vid_w, vid_h = 1920, 1080  # fallback
        for s in probe_data.get('streams', []):
            if s.get('codec_type') == 'video':
                vid_w = s.get('width', vid_w)
                vid_h = s.get('height', vid_h)
                break
        duration = float(probe_data.get('format', {}).get('duration', 0))
    except Exception:
        vid_w, vid_h, duration = 1920, 1080, 0

    if progress_callback:
        progress_callback(
            f"Rendering subtitles via overlay ({vid_w}×{vid_h})...")

    # ── Calculate subtitle stream's relative index within subtitle type ──
    # ffmpeg overlay filter uses subtitle stream index relative to all
    # subtitle streams (si=N), not absolute stream index.
    try:
        streams = get_all_streams(filepath)
        sub_streams = [s for s in streams if s['codec_type'] == 'subtitle']
        si = 0
        for i, s in enumerate(sub_streams):
            if s['index'] == stream_index:
                si = i
                break
    except Exception:
        si = 0

    # ── Phase 1: Render subtitles on black canvas with scene detection ──
    # showinfo filter outputs pts_time for each frame that passes through,
    # giving us accurate subtitle timestamps even with scene-detection VFR.
    frame_dir = os.path.join(tmpdir, 'frames')
    os.makedirs(frame_dir, exist_ok=True)

    dur_arg = max(duration, 60)
    render_cmd = [
        'ffmpeg', '-y', '-v', 'info',
        '-f', 'lavfi', '-i',
        f'color=c=black:s={vid_w}x{vid_h}:d={dur_arg}:r=10',
        '-i', filepath,
        '-filter_complex',
        f'[0:v][1:s:{si}]overlay=x=(W-w)/2:y=(H-h)/2,'
        f"select='gt(scene\\,0.01)',showinfo",
        '-vsync', 'vfr',
        os.path.join(frame_dir, 'frame_%06d.png'),
    ]

    # Run ffmpeg, parse showinfo for pts_time per frame
    import re as _re
    pts_re = _re.compile(r'pts_time:(\d+\.?\d*)')
    frame_pts = []  # pts_time for each output frame (ordered)
    try:
        proc = subprocess.Popen(
            render_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        # Read stderr line-by-line for showinfo + progress
        stderr_buf = b''
        while True:
            if cancel_event and cancel_event.is_set():
                proc.terminate()
                proc.wait()
                return []
            chunk = proc.stderr.read(4096)
            if not chunk:
                break
            stderr_buf += chunk
            while b'\n' in stderr_buf:
                line, stderr_buf = stderr_buf.split(b'\n', 1)
                line_str = line.decode(errors='replace')
                m = pts_re.search(line_str)
                if m:
                    frame_pts.append(float(m.group(1)))
                    if progress_callback and len(frame_pts) % 50 == 0:
                        progress_callback(
                            f"Rendering... {len(frame_pts)} scene changes")
            # Also check for process exit
            if proc.poll() is not None:
                # Read remaining
                rest = proc.stderr.read()
                if rest:
                    stderr_buf += rest
                    while b'\n' in stderr_buf:
                        line, stderr_buf = stderr_buf.split(b'\n', 1)
                        line_str = line.decode(errors='replace')
                        m = pts_re.search(line_str)
                        if m:
                            frame_pts.append(float(m.group(1)))
                    # Check last line without newline
                    if stderr_buf.strip():
                        m = pts_re.search(stderr_buf.decode(errors='replace'))
                        if m:
                            frame_pts.append(float(m.group(1)))
                break

        proc.wait()
        if proc.returncode != 0:
            if progress_callback:
                progress_callback("Render failed (ffmpeg error)")
            return []

    except Exception as e:
        if progress_callback:
            progress_callback(f"Render error: {e}")
        return []

    # ── Collect rendered frames with timestamps from showinfo ──
    import glob as _glob
    frame_files = sorted(_glob.glob(os.path.join(frame_dir, 'frame_*.png')))
    if not frame_files:
        if progress_callback:
            progress_callback(
                "No frames rendered — subtitle track may be empty "
                "(no visible subtitle content found)")
        return []

    if progress_callback:
        progress_callback(
            f"Rendered {len(frame_files)} frames — filtering blanks...")

    # Build (frame_path, pts) pairs — showinfo frame N maps to file N+1
    frame_with_pts = []
    for i, fp in enumerate(frame_files):
        pts = frame_pts[i] if i < len(frame_pts) else i * 0.1
        frame_with_pts.append((fp, pts))

    # ── Filter: keep only frames with actual subtitle content ──
    content_frames = []  # list of (frame_path, pts)
    for fp, pts in frame_with_pts:
        if cancel_event and cancel_event.is_set():
            return []
        try:
            img = Image.open(fp).convert('L')
            if img.getextrema()[1] <= 10:
                continue  # blank
            import numpy as _np
            arr = _np.array(img)
            bright_pct = (arr >= 30).sum() / arr.size * 100
            if bright_pct < 0.5:
                continue  # ghost frame
            content_frames.append((fp, pts))
        except Exception:
            continue

    if not content_frames:
        if progress_callback:
            progress_callback("No subtitle content found in rendered frames")
        return []

    if progress_callback:
        elapsed = _time.monotonic() - phase1_start
        progress_callback(
            f"Found {len(content_frames)} subtitle frames in {elapsed:.1f}s")

    # ── Group consecutive scene-change frames into subtitle events ──
    # Scene detection fires on both subtitle-on and subtitle-off
    # transitions, plus within the same subtitle (minor pixel changes).
    # Group frames that are close in time (< 1.0s gap) into one event.
    events = []  # list of (frame_path, start_seconds, end_seconds)
    groups = [[content_frames[0]]]
    for i in range(1, len(content_frames)):
        prev_pts = content_frames[i - 1][1]
        curr_pts = content_frames[i][1]
        if curr_pts - prev_pts < 1.0:
            groups[-1].append(content_frames[i])
        else:
            groups.append([content_frames[i]])

    for gi, group in enumerate(groups):
        start_pts = group[0][1]
        # End time: either next group's start, or last frame + default dur
        if gi + 1 < len(groups):
            end_pts = groups[gi + 1][0][1]
        else:
            end_pts = group[-1][1] + 3.0
        dur = max(end_pts - start_pts, 0.5)
        dur = min(dur, 15.0)
        events.append((group[0][0], start_pts, start_pts + dur))

    if progress_callback:
        progress_callback(
            f"Grouped into {len(events)} subtitle events — starting OCR...")

    # ── Phase 2: Parallel OCR ──
    total = len(events)
    try:
        max_workers = min(os.cpu_count() or 4, 8)
    except Exception:
        max_workers = 4

    completed_count = [0]
    completed_lock = threading.Lock()

    def _ocr_frame(args):
        """OCR a single subtitle frame image."""
        idx, frame_path, start_s, end_s = args
        try:
            img = Image.open(frame_path).convert('L')

            # Crop to content bounding box
            bbox = img.getbbox()
            if not bbox:
                return (start_s, end_s - start_s, '', frame_path)
            pad = 12
            x1 = max(0, bbox[0] - pad)
            y1 = max(0, bbox[1] - pad)
            x2 = min(img.width, bbox[2] + pad)
            y2 = min(img.height, bbox[3] + pad)
            img = img.crop((x1, y1, x2, y2))

            if _is_music_note_frame(img):
                return (start_s, end_s - start_s, '♪', frame_path)

            # Invert: subtitle text is light on black bg → make dark on white
            corners = [img.getpixel((0, 0)),
                       img.getpixel((img.width - 1, 0)),
                       img.getpixel((0, img.height - 1)),
                       img.getpixel((img.width - 1, img.height - 1))]
            if sum(corners) / len(corners) < 128:
                img = ImageOps.invert(img)

            # Upscale small text
            if img.height < 100:
                scale = max(2, 100 // img.height)
                img = img.resize((img.width * scale, img.height * scale),
                                 Image.LANCZOS)

            # White border padding
            img = ImageOps.expand(img, border=20, fill=255)

            # Save processed image for monitor preview
            processed_path = os.path.join(
                tmpdir, f'ocr_{idx + 1:05d}.png')
            img.save(processed_path)

            text = pytesseract.image_to_string(
                img, lang=tess_lang,
                config='--psm 6 --oem 3'
            ).strip()
            text = _fix_ocr_text(text)
            return (start_s, end_s - start_s, text, processed_path)
        except Exception:
            return (start_s, end_s - start_s, '', frame_path)

    all_args = [
        (i, fp, start_s, end_s)
        for i, (fp, start_s, end_s) in enumerate(events)
    ]

    raw_results = []
    executor = ThreadPoolExecutor(max_workers=max_workers)
    try:
        future_to_idx = {}
        for args in all_args:
            if cancel_event and cancel_event.is_set():
                break
            future = executor.submit(_ocr_frame, args)
            future_to_idx[future] = args[0]

        for future in as_completed(future_to_idx):
            if cancel_event and cancel_event.is_set():
                executor.shutdown(wait=False, cancel_futures=True)
                if progress_callback:
                    progress_callback(
                        f"OCR cancelled — {len(raw_results)} frames completed")
                break

            with completed_lock:
                completed_count[0] += 1
            try:
                result = future.result(timeout=30)
                pts, dur, text, img_path = result
                raw_results.append(result)

                if frame_callback:
                    frame_callback(
                        completed_count[0] - 1, total,
                        img_path, text or '[empty]',
                        _seconds_to_srt_time(pts),
                        _seconds_to_srt_time(pts + dur))

                if progress_callback and completed_count[0] % 5 == 0:
                    progress_callback(
                        f"OCR: {completed_count[0]}/{total} "
                        f"({completed_count[0] * 100 // total}%)")
            except Exception:
                pass
    finally:
        executor.shutdown(wait=False)

    # ── Sort by timestamp and build cue list ──
    raw_results.sort(key=lambda r: r[0])
    cues = []
    for pts, dur, text, img_path in raw_results:
        if text:
            cues.append({
                'index': len(cues) + 1,
                'start': _seconds_to_srt_time(pts),
                'end': _seconds_to_srt_time(pts + dur),
                'text': text,
            })

    with_text = sum(1 for r in raw_results if r[2])
    empty = sum(1 for r in raw_results if not r[2])
    if progress_callback:
        elapsed = _time.monotonic() - phase1_start
        progress_callback(
            f"OCR complete: {len(cues)} cues extracted in {elapsed:.1f}s")
        progress_callback(f"  Total OCR'd: {len(raw_results)}")
        progress_callback(f"  With text: {with_text}")
        progress_callback(f"  Empty/blank: {empty}")

    return cues


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

    # ── Ensure all OCR dependencies are installed ──
    deps_ok, tess_lang = _ensure_tesseract_deps(language, progress_callback)
    if not deps_ok:
        return []

    import pytesseract
    from PIL import Image

    tmpdir = tempfile.mkdtemp(prefix='docflix_ocr_')

    try:
        # ── Detect codec to choose extraction strategy ──
        codec_name = 'hdmv_pgs_subtitle'  # default assumption
        try:
            streams = get_all_streams(filepath)
            for s in streams:
                if s.get('index') == stream_index:
                    codec_name = s.get('codec_name', codec_name)
                    break
        except Exception:
            pass

        # DVB and VobSub subtitles can't be extracted to .sup (PGS-only).
        # Use ffmpeg overlay rendering for these codecs.
        if codec_name in ('dvb_subtitle', 'dvd_subtitle'):
            return _ocr_overlay_approach(
                filepath, stream_index, language, tess_lang,
                tmpdir, progress_callback, frame_callback, cancel_event)

        # ── Phase 1: Extract PGS stream and decode bitmaps directly ──
        # Much faster than the ffmpeg overlay approach — only reads the
        # subtitle stream data instead of processing the entire movie.
        from concurrent.futures import ThreadPoolExecutor, as_completed
        import struct
        import numpy as np
        import time as _time

        phase1_start = _time.monotonic()

        # ── Step 1: Extract raw PGS stream to .sup file ──
        if progress_callback:
            progress_callback("Extracting subtitle stream...")

        sup_path = os.path.join(tmpdir, 'subs.sup')
        extract_cmd = [
            'ffmpeg', '-y', '-v', 'error',
            '-i', filepath,
            '-map', f'0:{stream_index}',
            '-c:s', 'copy',
            sup_path
        ]
        try:
            proc = subprocess.Popen(extract_cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE)
            while proc.poll() is None:
                if cancel_event and cancel_event.is_set():
                    proc.terminate()
                    proc.wait()
                    return []
                try:
                    proc.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    continue
            if proc.returncode != 0:
                stderr_out = proc.stderr.read().decode(errors='replace')
                if progress_callback:
                    progress_callback(f"Extraction failed: {stderr_out[-300:]}")
                return []
        except Exception as e:
            if progress_callback:
                progress_callback(f"Extraction error: {e}")
            return []

        if not os.path.exists(sup_path) or os.path.getsize(sup_path) == 0:
            if progress_callback:
                progress_callback("Extracted subtitle stream is empty")
            return []

        sup_size = os.path.getsize(sup_path)
        if progress_callback:
            elapsed = _time.monotonic() - phase1_start
            progress_callback(f"Extracted {sup_size // 1024} KB in {elapsed:.1f}s")

        # ── Step 2: Parse PGS/SUP binary format ──
        if progress_callback:
            progress_callback("Parsing PGS segments...")

        try:
            with open(sup_path, 'rb') as f:
                sup_data = f.read()
        except Exception as e:
            if progress_callback:
                progress_callback(f"Error reading SUP file: {e}")
            return []

        pos = 0
        sup_len = len(sup_data)
        current_palette = {}
        current_pts = 0.0
        obj_data_buf = bytearray()
        obj_width = 0
        obj_height = 0
        display_sets = []  # (pts, palette_dict, rle_bytes, width, height)
        seg_count = 0

        while pos + 13 <= sup_len:
            if cancel_event and cancel_event.is_set():
                return []

            if sup_data[pos:pos+2] != b'PG':
                pos += 1
                continue

            pts_raw = struct.unpack('>I', sup_data[pos+2:pos+6])[0]
            pts_sec = pts_raw / 90000.0
            seg_type = sup_data[pos+10]
            seg_size = struct.unpack('>H', sup_data[pos+11:pos+13])[0]
            seg_data = sup_data[pos+13:pos+13+seg_size]
            pos += 13 + seg_size
            seg_count += 1

            if len(seg_data) < seg_size:
                break

            if seg_type == 0x16:  # PCS — Presentation Composition
                current_pts = pts_sec
                obj_data_buf = bytearray()
                obj_width = 0
                obj_height = 0

            elif seg_type == 0x14:  # PDS — Palette Definition
                if len(seg_data) >= 2:
                    p = 2
                    while p + 5 <= len(seg_data):
                        idx = seg_data[p]
                        y_val = seg_data[p+1]
                        cr = seg_data[p+2]
                        cb = seg_data[p+3]
                        alpha = seg_data[p+4]
                        r = max(0, min(255, int(y_val + 1.402 * (cr - 128))))
                        g = max(0, min(255, int(y_val - 0.344136 * (cb - 128) - 0.714136 * (cr - 128))))
                        b = max(0, min(255, int(y_val + 1.772 * (cb - 128))))
                        current_palette[idx] = (r, g, b, alpha)
                        p += 5

            elif seg_type == 0x15:  # ODS — Object Definition
                if len(seg_data) >= 4:
                    seq_flag = seg_data[3]
                    # Check if this segment has width/height header
                    # (present in first fragment or single-segment ODS).
                    # The header is: obj_id(2) + ver(1) + flag(1) +
                    #   data_len(3) + width(2) + height(2) = 11 bytes
                    # Detect by: if we don't have dimensions yet AND
                    # segment is large enough to contain the header.
                    if obj_width == 0 and len(seg_data) >= 11:
                        # First fragment (regardless of flag value)
                        obj_width = struct.unpack('>H', seg_data[7:9])[0]
                        obj_height = struct.unpack('>H', seg_data[9:11])[0]
                        obj_data_buf = bytearray(seg_data[11:])
                    elif obj_width > 0:
                        # Continuation fragment — append RLE data
                        obj_data_buf.extend(seg_data[4:])

            elif seg_type == 0x80:  # END — End of Display Set
                if obj_data_buf and obj_width > 0 and obj_height > 0:
                    display_sets.append((
                        current_pts,
                        dict(current_palette),
                        bytes(obj_data_buf),
                        obj_width,
                        obj_height
                    ))

            if progress_callback and seg_count % 500 == 0:
                pct = (pos / sup_len) * 100
                progress_callback(
                    f"Parsing PGS segments... {pct:.0f}% "
                    f"({len(display_sets)} subtitles found)")

        if not display_sets:
            if progress_callback:
                progress_callback("No subtitle images found in PGS stream")
            return []

        # Calculate durations from gaps between display sets
        for i in range(len(display_sets)):
            if i + 1 < len(display_sets):
                dur = min(display_sets[i + 1][0] - display_sets[i][0], 10.0)
            else:
                dur = 3.0
            dur = max(0.5, min(dur, 15.0))
            display_sets[i] = display_sets[i] + (dur,)

        if progress_callback:
            elapsed = _time.monotonic() - phase1_start
            progress_callback(
                f"Parsed {seg_count} segments → "
                f"{len(display_sets)} subtitles in {elapsed:.1f}s")

        # ── Step 3: Decode + OCR each frame (combined, parallel) ──
        # Instead of decoding ALL bitmaps first then OCR'ing, we combine
        # both into a single step per frame. Results appear immediately
        # in the monitor as each frame completes.
        total = len(display_sets)

        try:
            max_workers = min(os.cpu_count() or 4, 8)
        except Exception:
            max_workers = 4

        completed_count = [0]
        completed_lock = threading.Lock()

        if progress_callback:
            elapsed = _time.monotonic() - phase1_start
            progress_callback(
                f"Parsed {total} subtitles in {elapsed:.1f}s — "
                f"starting OCR ({max_workers} workers)...")

        def _decode_and_ocr(args):
            """Decode PGS RLE bitmap + OCR in one step."""
            i, pts, palette, rle_data, w, h, dur = args
            img_path = os.path.join(tmpdir, f'frame_{i+1:05d}.bmp')
            try:
                # ── Sanity check ──
                if w <= 0 or h <= 0 or w > 4096 or h > 4096:
                    return (pts, dur, '', img_path)
                expected = w * h
                if expected > 4096 * 4096:
                    return (pts, dur, '', img_path)

                # ── Decode PGS RLE ──
                pixels = bytearray(expected)
                pp = 0
                dp = 0
                rle_len = len(rle_data)
                max_iters = rle_len * 2 + expected
                iters = 0
                while dp < rle_len and pp < expected:
                    iters += 1
                    if iters > max_iters:
                        break
                    byte1 = rle_data[dp]; dp += 1
                    if byte1 != 0:
                        pixels[pp] = byte1; pp += 1
                    else:
                        if dp >= rle_len: break
                        byte2 = rle_data[dp]; dp += 1
                        if byte2 == 0:
                            mod = pp % w
                            if mod != 0: pp += w - mod
                        elif byte2 < 0x40:
                            pp += min(byte2, expected - pp)
                        elif byte2 < 0x80:
                            if dp >= rle_len: break
                            byte3 = rle_data[dp]; dp += 1
                            pp += min(((byte2 & 0x3F) << 8) | byte3, expected - pp)
                        elif byte2 < 0xC0:
                            run_len = byte2 & 0x3F
                            if dp >= rle_len: break
                            color = rle_data[dp]; dp += 1
                            run_len = min(run_len, expected - pp)
                            pixels[pp:pp+run_len] = bytes([color]) * run_len
                            pp += run_len
                        else:
                            if dp + 1 >= rle_len: break
                            byte3 = rle_data[dp]; dp += 1
                            color = rle_data[dp]; dp += 1
                            run_len = min(((byte2 & 0x3F) << 8) | byte3, expected - pp)
                            pixels[pp:pp+run_len] = bytes([color]) * run_len
                            pp += run_len

                # ── Palette → grayscale image ──
                # Use luminance AND alpha to separate text from shadow.
                # PGS subtitles have bright text (white, lum~220) with
                # dark shadow/outline (lum~30) behind it. Both have
                # high alpha. Using alpha alone makes shadows appear as
                # thick dark borders that merge characters together.
                # Instead: composite on white bg, then invert. Bright
                # text stays bright on white → inverts to dark. Dark
                # shadow stays dark on white → inverts to light/white.
                idx_arr = np.frombuffer(pixels, dtype=np.uint8)
                pal_r = np.zeros(256, dtype=np.uint8)
                pal_g = np.zeros(256, dtype=np.uint8)
                pal_b = np.zeros(256, dtype=np.uint8)
                pal_a = np.zeros(256, dtype=np.uint8)
                for idx, (r, g, b, a) in palette.items():
                    if idx < 256:
                        pal_r[idx] = r; pal_g[idx] = g
                        pal_b[idx] = b; pal_a[idx] = a

                r_arr = pal_r[idx_arr].astype(np.float32)
                g_arr = pal_g[idx_arr].astype(np.float32)
                b_arr = pal_b[idx_arr].astype(np.float32)
                a_arr = pal_a[idx_arr].astype(np.float32) / 255.0

                # Grayscale luminance
                lum = 0.299 * r_arr + 0.587 * g_arr + 0.114 * b_arr

                # Composite on BLACK background: 0*(1-a) + lum*a = lum*a
                # Result: bright text stays bright, dark shadow stays dark,
                # transparent background = black (0).
                # The _ocr_one function will then see dark corners (bg),
                # invert the image → white bg, dark text, light shadow.
                gray = (lum * a_arr).clip(0, 255).astype(np.uint8)

                if gray.max() < 10:
                    return (pts, dur, '', img_path)  # blank

                img = Image.fromarray(gray.reshape(h, w), mode='L')

                # ── Crop to bounding box + padding ──
                bbox = img.getbbox()
                if bbox:
                    pad = 12
                    x1 = max(0, bbox[0] - pad)
                    y1 = max(0, bbox[1] - pad)
                    x2 = min(img.width, bbox[2] + pad)
                    y2 = min(img.height, bbox[3] + pad)
                    img = img.crop((x1, y1, x2, y2))

                if _is_music_note_frame(img):
                    img.save(img_path)
                    return (pts, dur, '♪', img_path)

                # ── Upscale for Tesseract ──
                if img.height < 100:
                    scale = max(2, 100 // img.height)
                    img = img.resize((img.width * scale, img.height * scale),
                                     Image.LANCZOS)

                # ── Add white border padding ──
                from PIL import ImageOps
                img = ImageOps.expand(img, border=20, fill=255)

                # Save for monitor preview
                img.save(img_path)

                # ── Tesseract OCR ──
                text = pytesseract.image_to_string(
                    img, lang=tess_lang,
                    config='--psm 6 --oem 3'
                ).strip()
                text = _fix_ocr_text(text)
                return (pts, dur, text, img_path)
            except Exception:
                return (pts, dur, '', img_path)

        # Build args list
        all_args = [
            (i, pts, palette, rle_data, w, h, dur)
            for i, (pts, palette, rle_data, w, h, dur) in enumerate(display_sets)
        ]

        raw_results = []
        executor = ThreadPoolExecutor(max_workers=max_workers)
        try:
            future_to_idx = {}
            for args in all_args:
                if cancel_event and cancel_event.is_set():
                    break
                future = executor.submit(_decode_and_ocr, args)
                future_to_idx[future] = args[0]

            for future in as_completed(future_to_idx):
                if cancel_event and cancel_event.is_set():
                    executor.shutdown(wait=False, cancel_futures=True)
                    if progress_callback:
                        progress_callback(f"OCR cancelled — {len(raw_results)} frames completed")
                    break  # exit loop but keep partial results

                with completed_lock:
                    completed_count[0] += 1
                try:
                    result = future.result(timeout=30)
                    pts, dur, text, img_path = result
                    raw_results.append(result)

                    if frame_callback:
                        frame_callback(completed_count[0] - 1, total,
                                       img_path, text or '[empty]',
                                       _seconds_to_srt_time(pts),
                                       _seconds_to_srt_time(pts + dur))

                    if progress_callback and completed_count[0] % 5 == 0:
                        progress_callback(
                            f"OCR: {completed_count[0]}/{total} "
                            f"({completed_count[0]*100//total}%)")
                except Exception:
                    pass
        finally:
            executor.shutdown(wait=False)

        # Sort results by timestamp and build cue list
        raw_results.sort(key=lambda r: r[0])
        cues = []
        for pts, dur, text, img_path in raw_results:
            if text:
                cues.append({
                    'index': len(cues) + 1,
                    'start': _seconds_to_srt_time(pts),
                    'end': _seconds_to_srt_time(pts + dur),
                    'text': text,
                })

        # Count how many had text vs empty
        with_text = sum(1 for r in raw_results if r[2])
        empty = sum(1 for r in raw_results if not r[2])
        if progress_callback:
            progress_callback(f"OCR complete: {len(cues)} cues extracted")
            progress_callback(f"  Total OCR'd: {len(raw_results)}")
            progress_callback(f"  With text: {with_text}")
            progress_callback(f"  Empty/blank: {empty}")

        return cues

    except Exception as e:
        if progress_callback:
            import traceback
            progress_callback(f"OCR ERROR: {e}")
            progress_callback(traceback.format_exc()[-500:])
        return []
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

    # ── Fix dialogue dash misreads ──
    # Tesseract often misreads the subtitle dialogue dash (-) as = or ~-
    # These appear at the start of lines in multi-speaker subtitles
    # ~- → - (tilde-dash misread)
    text = re.sub(r'^~\s*-', '-', text, flags=re.MULTILINE)
    # = at start of line → - (equals misread as dash)
    text = re.sub(r'^=\s*', '- ', text, flags=re.MULTILINE)
    # ~= at start of line → -
    text = re.sub(r'^~\s*=\s*', '- ', text, flags=re.MULTILINE)

    # Fix | and / first (convert to letters before I/l rules run)
    # Replace | with I everywhere (pipe never appears in subtitles)
    text = text.replace('|', 'I')

    # Fix '' (two single quotes) misread as apostrophe — very common OCR error.
    # In contractions: I''m → I'm, don''t → don't, it''s → it's, I''ll → I'll
    # Between letters: that''s → that's, he''d → he'd, we''re → we're
    text = re.sub(r"(?<=[a-zA-Z])''(?=[a-zA-Z])", "'", text)

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

    # ── Fix curly/smart quotes and backticks to straight apostrophe ──
    text = text.replace('\u2018', "'")   # ' left single quote
    text = text.replace('\u2019', "'")   # ' right single quote
    text = text.replace('\u201C', '"')   # " left double quote
    text = text.replace('\u201D', '"')   # " right double quote
    text = text.replace('`', "'")        # backtick → apostrophe
    text = text.replace('\u00B4', "'")   # ´ acute accent → apostrophe

    # ── Fix broken contractions ──
    # Tesseract often splits contractions or garbles the apostrophe.
    # These patterns fix the most common English contractions.

    # Fix I + apostrophe garble: I 'm → I'm, I 'll → I'll, I 've → I've, I 'd → I'd
    text = re.sub(r"\bI\s+'(m|ll|ve|d)\b", r"I'\1", text)
    # Fix space before contraction: do n't → don't, ca n't → can't, etc.
    text = re.sub(r"\b(do|ca|wo|is|are|was|were|has|have|had|does|did|could|would|should|must|need|dare|ai)\s*n\s*'?\s*t\b",
                  lambda m: m.group(1) + "n't", text, flags=re.IGNORECASE)
    # Fix "' " (space after apostrophe in contractions): don' t → don't
    text = re.sub(r"(\w)'\s+([tsmd]\b|re\b|ve\b|ll\b)", r"\1'\2", text)

    # ── Fix 0/O confusion ──
    # 0 inside words should be O: g0 → go, wh0 → who, d0 → do, n0 → no
    text = re.sub(r'(?<=[a-zA-Z])0(?=[a-zA-Z])', 'O', text)  # mid-word: h0me → hOme
    text = re.sub(r'(?<=[a-zA-Z])0\b', 'o', text)            # end of word: g0 → go
    text = re.sub(r'\b0(?=[a-z])', 'O', text)                 # start of word: 0nly → Only
    # 0 as standalone word (not a number like "100") → O (rare but happens)
    text = re.sub(r'(?<![0-9])\b0\b(?![0-9])', 'O', text)

    # ── Fix rn → m confusion (Tesseract often reads 'm' as 'rn') ──
    # Only fix in common words where 'rn' is clearly wrong
    _RN_WORDS = {
        'corning': 'coming', 'sornething': 'something', 'sornebody': 'somebody',
        'sorneone': 'someone', 'sornewhere': 'somewhere', 'sornehow': 'somehow',
        'sornetime': 'sometime', 'becorne': 'become', 'welcorne': 'welcome',
        'horneward': 'homeward', 'horne': 'home', 'corne': 'come',
        'narne': 'name', 'garne': 'game', 'farne': 'fame', 'sarne': 'same',
        'tirne': 'time', 'rnake': 'make', 'rnore': 'more', 'rnuch': 'much',
        'rnan': 'man', 'rnen': 'men', 'rnine': 'mine', 'rnind': 'mind',
        'rnay': 'may', 'rnust': 'must', 'rneet': 'meet', 'rnove': 'move',
        'rnoving': 'moving', 'rnorning': 'morning', 'rnother': 'mother',
        'rnoney': 'money', 'rnoment': 'moment', 'rnouth': 'mouth',
        'wornan': 'woman', 'wornen': 'women', 'hurnan': 'human',
        'rernoving': 'removing', 'rernainder': 'remainder',
        'cornrnand': 'command', 'cornrnander': 'commander',
        'cornrnunity': 'community', 'cornrnit': 'commit',
        'accornpany': 'accompany', 'accornplish': 'accomplish',
        'recornrnend': 'recommend', 'cornpany': 'company',
        'cornplete': 'complete', 'cornputer': 'computer',
        'cornfort': 'comfort', 'cornbat': 'combat',
        'irnportant': 'important', 'irnpossible': 'impossible',
        'irnagine': 'imagine', 'irnpact': 'impact',
        'rnarriage': 'marriage', 'rnarried': 'married',
        'rnassive': 'massive', 'rnaster': 'master',
        'rnatter': 'matter', 'rnaterial': 'material',
        'rnachine': 'machine', 'rnajor': 'major',
        'rniddle': 'middle', 'rnilitary': 'military',
        'rnillion': 'million', 'rnissing': 'missing',
        'rnission': 'mission', 'rnistake': 'mistake',
        'rnurder': 'murder', 'rnusic': 'music',
        'rernerrber': 'remember', 'rernernber': 'remember',
        'rernerber': 'remember', 'rernember': 'remember',
    }
    for wrong, right in _RN_WORDS.items():
        text = re.sub(r'\b' + wrong + r'\b', right, text, flags=re.IGNORECASE)

    # ── Fix other common OCR character confusions ──
    # Fix "ii" that should be "ll" in common words: kiII → kill, wiII → will
    text = re.sub(r'\b([Ww])iII\b', r'\1ill', text)
    text = re.sub(r'\b([Kk])iII\b', r'\1ill', text)
    text = re.sub(r'\b([Ff])iII\b', r'\1ill', text)
    text = re.sub(r'\b([Ss])tiII\b', r'\1till', text)

    # Fix "Il" at start of common words that should be "Il" or "I'll"
    # "lI" → "ll": alI → all, welI → well, telI → tell
    text = re.sub(r'(?<=[a-z])lI\b', 'll', text)

    # ── Music note (♪) detection ──
    # Tesseract misreads ♪ as: 2 > $ & £ © » # * ? Sf D> P If J j 7» at start/end of lines

    # Standalone J or j at start/end of line → ♪ (music note misread)
    # J/j is rarely a standalone word in subtitles; almost always a ♪
    text = re.sub(r'^(-?\s*)[Jj]\s+', r'\1♪ ', text, flags=re.MULTILINE)  # J at start
    text = re.sub(r'\s+[Jj]\s*$', ' ♪', text, flags=re.MULTILINE)         # j at end
    # Standalone J or j as the entire line
    text = re.sub(r'^(-?\s*)[Jj]\s*$', r'\1♪', text, flags=re.MULTILINE)

    # 7» → ♪ (7 + right guillemet misread)
    text = text.replace('7»', '♪')
    # »7 variant
    text = text.replace('»7', '♪')

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

    # Detect garbled OCR output — short strings of mostly non-word characters.
    # These come from music notes, symbols, or decorative elements that
    # Tesseract can't read. Convert to ♪ if very short, or empty if garble.
    _GARBLE_CHARS = set('Jjd}]){><%#@~^*_=2$&£©»♪.,;:\'"!?/\\|-+`´ ')
    stripped = text.strip()
    if stripped:
        if len(stripped) <= 3 and all(c in _GARBLE_CHARS for c in stripped):
            return '♪'
        # Longer garble: if the text has no word with 2+ consecutive letters,
        # it's likely OCR noise from a symbol/music note bitmap
        if len(stripped) <= 15 and not re.search(r'[a-zA-Z]{2,}', stripped):
            return '♪' if any(c in 'Jjd>$£©»♪' for c in stripped) else ''

    # Clean up common OCR artifacts
    text = re.sub(r'\s{2,}', ' ', text)          # collapse multiple spaces
    text = re.sub(r'^\s+|\s+$', '', text, flags=re.MULTILINE)  # trim lines

    # Apply user's custom OCR rules (loaded from prefs)
    for find, replace in _get_custom_ocr_rules():
        text = text.replace(find, replace)

    return text.strip()


# ── Custom OCR Rules ────────────────────────────────────────────

_OCR_RULES_FILE = os.path.join(
    os.path.expanduser('~/.local/share/docflix'), 'ocr_rules.json')

_custom_rules_cache = None  # cached list of (find, replace) tuples


def _get_custom_ocr_rules():
    """Return list of (find, replace) tuples from the user's custom rules."""
    global _custom_rules_cache
    if _custom_rules_cache is not None:
        return _custom_rules_cache
    _custom_rules_cache = load_ocr_rules()
    return _custom_rules_cache


def load_ocr_rules():
    """Load custom OCR replacement rules from prefs file."""
    try:
        if os.path.exists(_OCR_RULES_FILE):
            with open(_OCR_RULES_FILE, 'r', encoding='utf-8') as f:
                rules = json.load(f)
            if isinstance(rules, list):
                return [(r['find'], r['replace']) for r in rules
                        if isinstance(r, dict) and 'find' in r and 'replace' in r]
    except Exception:
        pass
    return []


def save_ocr_rules(rules):
    """Save custom OCR replacement rules to prefs file.
    rules: list of (find, replace) tuples."""
    global _custom_rules_cache
    try:
        os.makedirs(os.path.dirname(_OCR_RULES_FILE), exist_ok=True)
        data = [{'find': f, 'replace': r} for f, r in rules]
        with open(_OCR_RULES_FILE, 'w', encoding='utf-8') as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        _custom_rules_cache = list(rules)
    except Exception:
        pass


def reload_ocr_rules():
    """Force reload of custom OCR rules from disk."""
    global _custom_rules_cache
    _custom_rules_cache = None
    return _get_custom_ocr_rules()


def run_ocr_with_monitor(app, filepath, stream_index, language,
                         on_complete=None):
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
        mon = tk.Toplevel(app.root)
        mon.title(f"OCR — {os.path.basename(filepath)}")
        mon.geometry("750x580")
        mon.transient(app.root)
        mon.minsize(600, 450)
        app._center_on_main(mon)

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
                    app.add_log(f"OCR complete: {out_name} ({len(cues)} cues, "
                                 f"{elapsed_m}m {elapsed_s}s)", 'SUCCESS')
                    status_label.configure(text=f"Done — {len(cues)} cues extracted "
                                                f"in {elapsed_m}m {elapsed_s}s")
                    progress_var.set(100)
                    cancel_btn.configure(text="Close", command=mon.destroy)

                    # Add buttons for next steps
                    ttk.Button(btn_f, text="Open in Editor",
                               command=lambda: (
                                   mon.destroy(),
                                   app.show_subtitle_editor(
                                       filepath, stream_index, file_info,
                                       external_sub_path=out_path)
                               )).pack(side='right', padx=(0, 8))
                else:
                    status_label.configure(text="OCR produced no output")
                    app.add_log(f"OCR produced no output for stream #{stream_index}",
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

