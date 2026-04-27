"""
Docflix Video Converter — Bitmap Subtitle OCR

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
import tempfile
import threading
import tkinter as tk
from tkinter import ttk
from concurrent.futures import ThreadPoolExecutor, as_completed

from .utils import get_video_duration, get_all_streams


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

