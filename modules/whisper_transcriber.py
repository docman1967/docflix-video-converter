"""
Docflix Media Suite — Whisper Transcriber

GUI tool for extracting subtitles from video/audio files using
faster-whisper or WhisperX.  Supports batch processing, drag-and-drop,
translation, word-level timestamps, and subtitle preview.
"""

import io
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from datetime import timedelta
from pathlib import Path
from tkinter import messagebox, ttk

from .constants import VIDEO_EXTENSIONS
from .utils import scaled_geometry, scaled_minsize, ask_open_files, ask_directory
from .whisper_subtitles import (
    BACKENDS,
    VIDEO_EXTENSIONS as WS_VIDEO_EXTENSIONS,
    AUDIO_EXTENSIONS,
    SubSegment,
    segments_to_srt,
    segments_to_vtt,
    write_output,
    post_process_segments,
    trim_lead_time,
    find_media_files,
    subtitle_exists,
    is_backend_available,
)

# ── optional drag-and-drop support ───────────────────────────────────────────
try:
    from tkinterdnd2 import DND_FILES
    HAS_DND = True
except ImportError:
    HAS_DND = False

# ── constants ────────────────────────────────────────────────────────────────

MODELS = [
    "tiny", "tiny.en",
    "base", "base.en",
    "small", "small.en",
    "medium", "medium.en",
    "large-v1", "large-v2", "large-v3",
]

LANGUAGES = {
    "Auto-detect": None,
    "English": "en",
    "Spanish": "es",
    "French": "fr",
    "German": "de",
    "Italian": "it",
    "Portuguese": "pt",
    "Dutch": "nl",
    "Russian": "ru",
    "Chinese": "zh",
    "Japanese": "ja",
    "Korean": "ko",
    "Arabic": "ar",
    "Hindi": "hi",
    "Turkish": "tr",
    "Polish": "pl",
    "Swedish": "sv",
    "Norwegian": "no",
    "Danish": "da",
    "Finnish": "fi",
}

TASKS = {
    "Transcribe": "transcribe",
    "Translate to English": "translate",
}

DEVICES = ["auto", "cpu", "cuda"]

ALL_EXTS = (
    [f"*{e}" for e in sorted(WS_VIDEO_EXTENSIONS)]
    + [f"*{e}" for e in sorted(AUDIO_EXTENSIONS)]
)

# File list status colors
COLOR_QUEUED = "gray"
COLOR_ACTIVE = "#e8a317"
COLOR_DONE   = "#2e8b57"
COLOR_ERROR  = "#cd3333"
COLOR_SKIP   = "#4682b4"


# ── helpers ──────────────────────────────────────────────────────────────────


def detect_device() -> str:
    try:
        import ctranslate2
        return "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
    except Exception:
        return "cpu"


def check_deps() -> list[str]:
    missing = []
    has_fw = is_backend_available("faster-whisper")
    has_wx = is_backend_available("whisperx")
    if not has_fw and not has_wx:
        missing.append("No transcription backend installed -- install at least one:")
        missing.append("  faster-whisper  (pip install faster-whisper)")
        missing.append("  whisperx        (pip install whisperx)")
    if not shutil.which("ffmpeg"):
        missing.append("ffmpeg  (https://ffmpeg.org/download.html)")
    return missing


def send_notification(title: str, message: str):
    """Send a desktop notification (best-effort, non-blocking)."""
    try:
        if sys.platform == "linux":
            subprocess.Popen(
                ["notify-send", "-a", "Whisper Subtitles", title, message],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        elif sys.platform == "darwin":
            subprocess.Popen(
                ["osascript", "-e",
                 f'display notification "{message}" with title "{title}"'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
    except FileNotFoundError:
        pass


# ── stream / logging redirectors ─────────────────────────────────────────────


class QueueStream(io.TextIOBase):
    """A write-only stream that forwards lines to the GUI log queue.

    Attach as sys.stdout / sys.stderr inside the worker thread so that
    library output (tqdm progress bars, huggingface_hub downloads,
    pyannote warnings, lightning messages, etc.) appears in the log panel.
    """

    def __init__(self, q: queue.Queue, prefix: str = ""):
        super().__init__()
        self._q = q
        self._prefix = prefix
        self._buf = ""

    def write(self, text: str) -> int:
        if not text:
            return 0
        self._buf += text
        while "\n" in self._buf or "\r" in self._buf:
            nl = self._buf.find("\n")
            cr = self._buf.find("\r")
            if nl == -1:
                idx, skip = cr, 1
            elif cr == -1:
                idx, skip = nl, 1
            else:
                idx, skip = min(nl, cr), 1
            line = self._buf[:idx].rstrip()
            self._buf = self._buf[idx + skip:]
            if line:
                self._q.put(("log", f"{self._prefix}{line}"))
        return len(text)

    def flush(self):
        if self._buf.strip():
            self._q.put(("log", f"{self._prefix}{self._buf.strip()}"))
            self._buf = ""


class QueueLogHandler(logging.Handler):
    """Logging handler that sends log records to the GUI queue."""

    def __init__(self, q: queue.Queue):
        super().__init__()
        self._q = q

    def emit(self, record):
        try:
            msg = self.format(record)
            if msg.strip():
                self._q.put(("log", f"   {msg}"))
        except Exception:
            pass


# ── worker thread ────────────────────────────────────────────────────────────


class BatchTranscribeWorker(threading.Thread):
    """
    Processes a list of input files sequentially in a background thread.

    Queue events emitted:
        ("log",        message_str)
        ("next_file",  (index, total, path))   -- about to start a file
        ("skip_file",  (index, path, reason))  -- skipped
        ("progress",   (current_sec, total_sec))
        ("file_done",  (index, path, [segments]))
        ("file_error", (index, path, exception))
        ("batch_done", None)
    """

    def __init__(self, q: queue.Queue, paths: list[Path], model_size: str,
                 language: str | None, device: str, beam_size: int, vad: bool,
                 task: str = "transcribe", word_timestamps: bool = False,
                 skip_existing: bool = False, output_dir: str | None = None,
                 output_formats: list[str] | None = None,
                 backend: str = "faster-whisper", batch_size: int = 16):
        super().__init__(daemon=True)
        self.q = q
        self.paths = paths
        self.model_size = model_size
        self.language = language
        self.device = device
        self.beam_size = beam_size
        self.vad = vad
        self.task = task
        self.word_timestamps = word_timestamps
        self.skip_existing = skip_existing
        self.output_dir = output_dir
        self.output_formats = output_formats or ["srt"]
        self.backend = backend
        self.batch_size = batch_size
        self._stop_event = threading.Event()

    def stop(self):
        self._stop_event.set()

    def run(self):
        old_stdout, old_stderr = sys.stdout, sys.stderr
        sys.stdout = QueueStream(self.q, prefix="   ")
        sys.stderr = QueueStream(self.q, prefix="   ")

        log_handler = QueueLogHandler(self.q)
        log_handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
        root_logger = logging.getLogger()
        root_logger.addHandler(log_handler)

        try:
            self._run()
        except Exception as exc:
            self.q.put(("log", f"Fatal: {exc}"))
            self.q.put(("batch_done", None))
        finally:
            sys.stdout.flush()
            sys.stderr.flush()
            sys.stdout, sys.stderr = old_stdout, old_stderr
            root_logger.removeHandler(log_handler)

    def _run(self):
        if self.backend == "whisperx":
            self._run_whisperx()
        else:
            self._run_faster_whisper()

    def _run_whisperx(self):
        import whisperx

        compute_type = "float16" if self.device == "cuda" else "int8"
        self.q.put(("log", f"Loading WhisperX model '{self.model_size}'  [{self.device}, {compute_type}]..."))
        model = whisperx.load_model(
            self.model_size, self.device,
            compute_type=compute_type,
            language=self.language,
            task=self.task,
        )
        self.q.put(("log", "WhisperX model ready."))

        total = len(self.paths)
        for idx, path in enumerate(self.paths):
            if self._stop_event.is_set():
                self.q.put(("log", "Batch cancelled."))
                break

            if self.skip_existing and subtitle_exists(
                    path, self.output_dir, self.output_formats):
                self.q.put(("skip_file", (idx, path, "subtitle already exists")))
                self.q.put(("log", f"Skipping (already exists): {path.name}"))
                continue

            self.q.put(("next_file", (idx, total, path)))
            self.q.put(("log", f"\n-- [{idx+1}/{total}] {path.name}"))

            try:
                segments = self._process_one_whisperx(whisperx, model, path)
                self.q.put(("file_done", (idx, path, segments)))
                self.q.put(("log", f"Done: {len(segments)} segments  ->  {path.name}"))
            except Exception as exc:
                self.q.put(("file_error", (idx, path, exc)))
                self.q.put(("log", f"Error: {path.name}: {exc}"))

        self.q.put(("batch_done", None))

    def _process_one_whisperx(self, whisperx, model, path: Path) -> list:
        with tempfile.TemporaryDirectory() as tmp_dir:
            suffix = path.suffix.lower()
            if suffix not in AUDIO_EXTENSIONS:
                self.q.put(("log", "   Extracting audio..."))
                out_audio = Path(tmp_dir) / "audio.wav"
                cmd = [
                    "ffmpeg", "-y", "-i", str(path),
                    "-vn", "-acodec", "pcm_s16le",
                    "-ar", "16000", "-ac", "1",
                    str(out_audio),
                ]
                result = subprocess.run(cmd, capture_output=True, text=True)
                if result.returncode != 0:
                    raise RuntimeError(f"ffmpeg failed:\n{result.stderr[-800:]}")
                audio_path = out_audio
            else:
                audio_path = path

            if self._stop_event.is_set():
                raise RuntimeError("Cancelled")

            task_label = "Translating" if self.task == "translate" else "Transcribing"
            self.q.put(("log", f"   {task_label} with WhisperX..."))

            audio = whisperx.load_audio(str(audio_path))
            wx_result = model.transcribe(audio, batch_size=self.batch_size, language=self.language)

            detected_lang = wx_result.get("language", self.language or "unknown")
            n_segs = len(wx_result.get("segments", []))
            self.q.put(("log", f"   Language: {detected_lang}  Segments: {n_segs}"))

            if self.word_timestamps and wx_result.get("segments"):
                self.q.put(("log", f"   Aligning words ({detected_lang})..."))
                try:
                    model_a, metadata = whisperx.load_align_model(
                        language_code=detected_lang, device=self.device,
                    )
                    wx_result = whisperx.align(
                        wx_result["segments"], model_a, metadata, audio,
                        self.device, return_char_alignments=False,
                    )
                    self.q.put(("log", "   Forced alignment complete."))
                except Exception as exc:
                    self.q.put(("log", f"   Alignment failed ({exc}), using unaligned timestamps."))

            if self._stop_event.is_set():
                raise RuntimeError("Cancelled")

            segments = []
            for seg_dict in wx_result.get("segments", []):
                start = seg_dict.get("start", 0.0)
                end = seg_dict.get("end", 0.0)
                text = seg_dict.get("text", "").strip()
                if not text:
                    continue

                words = []
                if self.word_timestamps and "words" in seg_dict:
                    for w in seg_dict["words"]:
                        words.append(SubSegment(
                            start=w.get("start", start),
                            end=w.get("end", end),
                            text=w.get("word", "").strip(),
                        ))

                segments.append(SubSegment(start=start, end=end, text=text, words=words))

            self.q.put(("progress", (1, 1)))
            return segments

    def _run_faster_whisper(self):
        from faster_whisper import WhisperModel

        self.q.put(("log", f"Loading model '{self.model_size}'  [{self.device}]..."))
        model = WhisperModel(self.model_size, device=self.device, compute_type="auto")
        self.q.put(("log", "Model ready."))

        total = len(self.paths)
        for idx, path in enumerate(self.paths):
            if self._stop_event.is_set():
                self.q.put(("log", "Batch cancelled."))
                break

            if self.skip_existing and subtitle_exists(
                    path, self.output_dir, self.output_formats):
                self.q.put(("skip_file", (idx, path, "subtitle already exists")))
                self.q.put(("log", f"Skipping (already exists): {path.name}"))
                continue

            self.q.put(("next_file", (idx, total, path)))
            self.q.put(("log", f"\n-- [{idx+1}/{total}] {path.name}"))

            try:
                segments = self._process_one(model, path)
                self.q.put(("file_done", (idx, path, segments)))
                self.q.put(("log", f"Done: {len(segments)} segments  ->  {path.name}"))
            except Exception as exc:
                self.q.put(("file_error", (idx, path, exc)))
                self.q.put(("log", f"Error: {path.name}: {exc}"))

        self.q.put(("batch_done", None))

    def _process_one(self, model, path: Path) -> list:
        with tempfile.TemporaryDirectory() as tmp_dir:
            suffix = path.suffix.lower()
            if suffix not in AUDIO_EXTENSIONS:
                self.q.put(("log", "   Extracting audio..."))
                out_audio = Path(tmp_dir) / "audio.wav"
                cmd = [
                    "ffmpeg", "-y", "-i", str(path),
                    "-vn", "-acodec", "pcm_s16le",
                    "-ar", "16000", "-ac", "1",
                    str(out_audio),
                ]
                result = subprocess.run(cmd, capture_output=True, text=True)
                if result.returncode != 0:
                    raise RuntimeError(f"ffmpeg failed:\n{result.stderr[-800:]}")
                audio_path = out_audio
            else:
                audio_path = path

            if self._stop_event.is_set():
                raise RuntimeError("Cancelled")

            task_label = "Translating" if self.task == "translate" else "Transcribing"
            self.q.put(("log", f"   {task_label}..."))

            segments_gen, info = model.transcribe(
                str(audio_path),
                beam_size=self.beam_size,
                language=self.language,
                vad_filter=self.vad,
                vad_parameters=dict(min_silence_duration_ms=500),
                task=self.task,
                word_timestamps=self.word_timestamps,
            )
            duration = info.duration
            lang = info.language
            conf = info.language_probability
            self.q.put(("log",
                f"   Language: {lang} ({conf:.0%})  Duration: {timedelta(seconds=int(duration))}"))
            self.q.put(("progress", (0, duration)))

            collected = []
            for seg in segments_gen:
                if self._stop_event.is_set():
                    raise RuntimeError("Cancelled")
                collected.append(seg)
                self.q.put(("progress", (seg.end, duration)))

            return collected


# ── main GUI window ──────────────────────────────────────────────────────────


def open_whisper_transcriber(app):
    """Open the Whisper Transcriber tool window."""

    win = tk.Toplevel(app.root)
    win.withdraw()
    win.title("Whisper Transcriber")
    geom_str = scaled_geometry(win, 1100, 800)
    win.geometry(geom_str)
    win.minsize(*scaled_minsize(win, 920, 700))
    win.update_idletasks()
    try:
        gm = re.match(r'(\d+)x(\d+)', geom_str)
        dw = int(gm.group(1)) if gm else win.winfo_reqwidth()
        dh = int(gm.group(2)) if gm else win.winfo_reqheight()
        pw = app.root.winfo_width()
        ph = app.root.winfo_height()
        px = app.root.winfo_x()
        py = app.root.winfo_y()
        x = px + (pw - dw) // 2
        y = py + (ph - dh) // 2
        win.geometry(f'{dw}x{dh}+{max(0, x)}+{max(0, y)}')
    except Exception:
        pass
    win.deiconify()

    # ── State ──
    _processing = [False]
    _worker = [None]
    _queue = queue.Queue()
    _file_paths = []       # list of Path objects
    _results = {}          # index -> list of segments
    _preview_idx = [None]

    # ── Load saved preferences ──
    _wp = getattr(app, '_whisper_prefs', {})

    # ── Main layout ──
    main_frame = ttk.Frame(win, padding=8)
    main_frame.pack(fill='both', expand=True)
    main_frame.columnconfigure(0, weight=1)
    main_frame.columnconfigure(1, weight=1)
    main_frame.rowconfigure(0, weight=1)

    # Left column
    left = ttk.Frame(main_frame)
    left.grid(row=0, column=0, sticky='nsew', padx=(0, 5))
    left.columnconfigure(0, weight=1)
    left.rowconfigure(1, weight=1)  # file list expands

    # Right column
    right = ttk.Frame(main_frame)
    right.grid(row=0, column=1, sticky='nsew', padx=(5, 0))
    right.columnconfigure(0, weight=1)
    right.rowconfigure(1, weight=1)
    right.rowconfigure(3, weight=1)

    # ══════════════════════════════════════════════════════════════════
    # File list panel
    # ══════════════════════════════════════════════════════════════════

    file_toolbar = ttk.Frame(left)
    file_toolbar.grid(row=0, column=0, sticky='ew', pady=(0, 4))

    _file_btns = []

    def _add_files():
        paths = ask_open_files(
            parent=win, title="Add video or audio files",
            filetypes=[
                ("Video & Audio files", " ".join(ALL_EXTS)),
                ("Video files", " ".join(f"*{e}" for e in sorted(WS_VIDEO_EXTENSIONS))),
                ("Audio files", " ".join(f"*{e}" for e in sorted(AUDIO_EXTENSIONS))),
                ("All files", "*.*"),
            ],
        )
        added = 0
        for p in paths:
            path = Path(p)
            if path not in _file_paths:
                _file_paths.append(path)
                file_listbox.insert("end", path.name)
                file_listbox.itemconfig("end", fg=COLOR_QUEUED)
                added += 1
        if added:
            _refresh_count()
            _status_var.set(f"Added {added} file(s) -- {len(_file_paths)} total")

    def _add_folder():
        directory = ask_directory(parent=win, title="Select folder with video/audio files")
        if not directory:
            return
        media_files = find_media_files(Path(directory))
        if not media_files:
            messagebox.showinfo("No files found",
                                f"No video or audio files found in:\n{directory}",
                                parent=win)
            return

        added = 0
        for path in media_files:
            if path not in _file_paths:
                _file_paths.append(path)
                file_listbox.insert("end", path.name)
                file_listbox.itemconfig("end", fg=COLOR_QUEUED)
                added += 1
        if added:
            _refresh_count()
            _status_var.set(f"Added {added} file(s) from folder -- {len(_file_paths)} total")
            _log_write(f"Added {added} media file(s) from {directory}", "info")

    def _remove_selected():
        selected = list(file_listbox.curselection())
        if not selected:
            return
        for idx in reversed(selected):
            file_listbox.delete(idx)
            _file_paths.pop(idx)
            _results.pop(idx, None)
            new_results = {}
            for k, v in _results.items():
                new_results[k if k < idx else k - 1] = v
            _results.clear()
            _results.update(new_results)
        _refresh_count()

    def _clear_files():
        file_listbox.delete(0, "end")
        _file_paths.clear()
        _results.clear()
        _preview_idx[0] = None
        _clear_preview()
        _refresh_count()

    for txt, cmd in [
        ("Add Files", _add_files),
        ("Add Folder", _add_folder),
        ("Remove", _remove_selected),
        ("Clear", _clear_files),
    ]:
        btn = ttk.Button(file_toolbar, text=txt, command=cmd)
        btn.pack(side='left', padx=(0, 4))
        _file_btns.append(btn)

    # Listbox with scrollbar
    file_list_frame = ttk.LabelFrame(left, text="Files")
    file_list_frame.grid(row=1, column=0, sticky='nsew')
    file_list_frame.columnconfigure(0, weight=1)
    file_list_frame.rowconfigure(0, weight=1)

    file_listbox = tk.Listbox(
        file_list_frame,
        relief="flat", activestyle="none",
        highlightthickness=0, bd=0,
    )
    file_listbox.grid(row=0, column=0, sticky='nsew')

    file_sb = ttk.Scrollbar(file_list_frame, orient='vertical',
                             command=file_listbox.yview)
    file_sb.grid(row=0, column=1, sticky='ns')
    file_listbox['yscrollcommand'] = file_sb.set

    def _on_list_double_click(_event=None):
        sel = file_listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        if idx in _results:
            _show_preview(idx)
        else:
            _status_var.set("File not yet transcribed -- run extraction first.")

    file_listbox.bind("<Double-Button-1>", _on_list_double_click)

    # Enable drag-and-drop
    if HAS_DND:
        try:
            file_listbox.drop_target_register(DND_FILES)
            file_listbox.dnd_bind("<<Drop>>", lambda e: _on_drop(e))
            win.drop_target_register(DND_FILES)
            win.dnd_bind("<<Drop>>", lambda e: _on_drop(e))
        except Exception:
            pass

    # Count label + DnD hint
    count_frame = ttk.Frame(left)
    count_frame.grid(row=2, column=0, sticky='ew', pady=(2, 0))
    count_frame.columnconfigure(0, weight=1)

    _file_count_var = tk.StringVar(value="0 files")
    ttk.Label(count_frame, textvariable=_file_count_var,
              anchor='w').grid(row=0, column=0, sticky='w')

    if HAS_DND:
        ttk.Label(count_frame, text="(drag & drop supported)",
                  anchor='e').grid(row=0, column=1, sticky='e')

    def _refresh_count():
        n = len(_file_paths)
        _file_count_var.set(f"{n} file{'s' if n != 1 else ''}")

    def _on_drop(event):
        if _processing[0]:
            return
        raw = event.data
        paths = []
        for match in re.finditer(r'\{([^}]+)\}|(\S+)', raw):
            p = match.group(1) or match.group(2)
            paths.append(p)

        all_exts = WS_VIDEO_EXTENSIONS | AUDIO_EXTENSIONS
        added = 0
        for p in paths:
            path = Path(p)
            if path.is_dir():
                for media_file in find_media_files(path):
                    if media_file not in _file_paths:
                        _file_paths.append(media_file)
                        file_listbox.insert("end", media_file.name)
                        file_listbox.itemconfig("end", fg=COLOR_QUEUED)
                        added += 1
            elif path.is_file() and path.suffix.lower() in all_exts:
                if path not in _file_paths:
                    _file_paths.append(path)
                    file_listbox.insert("end", path.name)
                    file_listbox.itemconfig("end", fg=COLOR_QUEUED)
                    added += 1

        if added:
            _refresh_count()
            _status_var.set(f"Dropped {added} file(s) -- {len(_file_paths)} total")
            _log_write(f"Dropped {added} file(s).", "info")

    def _set_file_buttons_state(state: str):
        for btn in _file_btns:
            btn.config(state=state)

    def _safe_itemconfig(idx: int, **kw):
        if 0 <= idx < file_listbox.size():
            file_listbox.itemconfig(idx, **kw)

    # ══════════════════════════════════════════════════════════════════
    # Settings panel
    # ══════════════════════════════════════════════════════════════════

    settings_frame = ttk.LabelFrame(left, text="Settings")
    settings_frame.grid(row=3, column=0, sticky='ew', pady=(8, 0))
    settings_frame.columnconfigure(1, weight=1)

    row = 0

    # ── Output directory ──
    ttk.Label(settings_frame, text="Output Directory:").grid(
        row=row, column=0, sticky='w', padx=(4, 4), pady=(4, 2))
    row += 1

    out_row = ttk.Frame(settings_frame)
    out_row.grid(row=row, column=0, columnspan=2, sticky='ew', padx=4)
    out_row.columnconfigure(0, weight=1)
    row += 1

    _outdir_var = tk.StringVar(value=_wp.get('outdir', 'Same folder as each input'))
    outdir_entry = ttk.Entry(out_row, textvariable=_outdir_var)
    outdir_entry.grid(row=0, column=0, sticky='ew')

    def _browse_outdir():
        d = ask_directory(parent=win, title="Select output directory")
        if d:
            _outdir_var.set(d)

    ttk.Button(out_row, text="Browse...", command=_browse_outdir).grid(
        row=0, column=1, padx=(4, 0))

    # ── Backend + Model ──
    bm_frame = ttk.Frame(settings_frame)
    bm_frame.grid(row=row, column=0, columnspan=2, sticky='ew', padx=4, pady=(6, 0))
    bm_frame.columnconfigure(1, weight=1)
    bm_frame.columnconfigure(3, weight=1)
    row += 1

    ttk.Label(bm_frame, text="Backend:").grid(row=0, column=0, sticky='w', padx=(0, 4))
    _backend_var = tk.StringVar(value=_wp.get('backend', 'faster-whisper'))
    backend_cb = ttk.Combobox(bm_frame, textvariable=_backend_var,
                               values=list(BACKENDS), state='readonly', width=14)
    backend_cb.grid(row=0, column=1, sticky='ew', padx=(0, 10))

    def _on_backend_change(_event=None):
        backend = _backend_var.get()
        if backend == "whisperx" and not is_backend_available("whisperx"):
            _log_write("whisperx is not installed. Run: pip install whisperx", "warning")
        elif backend == "faster-whisper" and not is_backend_available("faster-whisper"):
            _log_write("faster-whisper is not installed. Run: pip install faster-whisper", "warning")

    backend_cb.bind("<<ComboboxSelected>>", _on_backend_change)

    ttk.Label(bm_frame, text="Model:").grid(row=0, column=2, sticky='w', padx=(0, 4))
    _model_var = tk.StringVar(value=_wp.get('model', 'small'))
    model_cb = ttk.Combobox(bm_frame, textvariable=_model_var,
                             values=MODELS, state='readonly', width=14)
    model_cb.grid(row=0, column=3, sticky='ew')

    # Backend availability hint
    hints = []
    if is_backend_available("faster-whisper"):
        hints.append("faster-whisper: available")
    else:
        hints.append("faster-whisper: not installed")
    if is_backend_available("whisperx"):
        hints.append("whisperx: available")
    else:
        hints.append("whisperx: not installed")
    ttk.Label(settings_frame, text=" | ".join(hints), anchor='w').grid(
        row=row, column=0, columnspan=2, sticky='w', padx=4)
    row += 1

    # ── Language + Task ──
    lt_frame = ttk.Frame(settings_frame)
    lt_frame.grid(row=row, column=0, columnspan=2, sticky='ew', padx=4, pady=(6, 0))
    lt_frame.columnconfigure(1, weight=1)
    lt_frame.columnconfigure(3, weight=1)
    row += 1

    ttk.Label(lt_frame, text="Language:").grid(row=0, column=0, sticky='w', padx=(0, 4))
    _lang_var = tk.StringVar(value=_wp.get('language', 'Auto-detect'))
    lang_cb = ttk.Combobox(lt_frame, textvariable=_lang_var,
                            values=list(LANGUAGES.keys()), state='readonly', width=14)
    lang_cb.grid(row=0, column=1, sticky='ew', padx=(0, 10))

    ttk.Label(lt_frame, text="Task:").grid(row=0, column=2, sticky='w', padx=(0, 4))
    _task_var = tk.StringVar(value=_wp.get('task', 'Transcribe'))
    task_cb = ttk.Combobox(lt_frame, textvariable=_task_var,
                            values=list(TASKS.keys()), state='readonly', width=18)
    task_cb.grid(row=0, column=3, sticky='ew')

    # ── Output format ──
    fmt_frame = ttk.Frame(settings_frame)
    fmt_frame.grid(row=row, column=0, columnspan=2, sticky='w', padx=4, pady=(6, 0))
    row += 1

    ttk.Label(fmt_frame, text="Format:").pack(side='left', padx=(0, 6))
    _fmt_srt = tk.BooleanVar(value=_wp.get('fmt_srt', True))
    _fmt_vtt = tk.BooleanVar(value=_wp.get('fmt_vtt', False))
    ttk.Checkbutton(fmt_frame, text="SRT", variable=_fmt_srt).pack(side='left', padx=(0, 10))
    ttk.Checkbutton(fmt_frame, text="VTT", variable=_fmt_vtt).pack(side='left', padx=(0, 10))

    # VTT style entry
    ttk.Label(fmt_frame, text="VTT Style:").pack(side='left', padx=(10, 4))
    _vtt_style_var = tk.StringVar(value=_wp.get('vtt_style', ''))
    ttk.Entry(fmt_frame, textvariable=_vtt_style_var, width=20).pack(side='left')

    # ── Advanced ──
    adv_frame = ttk.LabelFrame(settings_frame, text="Advanced")
    adv_frame.grid(row=row, column=0, columnspan=2, sticky='ew', padx=4, pady=(6, 0))
    adv_frame.columnconfigure(1, weight=1)
    adv_frame.columnconfigure(3, weight=1)
    row += 1

    # Row 1: Device, Beam, VAD
    adv1 = ttk.Frame(adv_frame)
    adv1.pack(fill='x', padx=4, pady=2)

    ttk.Label(adv1, text="Device:").pack(side='left', padx=(0, 4))
    # Default device: auto-detect GPU on startup so the user sees
    # "cuda" or "cpu" instead of a vague "auto"
    _default_device = _wp.get('device', '')
    if not _default_device or _default_device == 'auto':
        _default_device = detect_device()
    _device_var = tk.StringVar(value=_default_device)
    dev_cb = ttk.Combobox(adv1, textvariable=_device_var,
                           values=DEVICES, state='readonly', width=7)
    dev_cb.pack(side='left', padx=(0, 10))

    ttk.Label(adv1, text="Beam:").pack(side='left', padx=(0, 4))
    _beam_var = tk.IntVar(value=_wp.get('beam_size', 5))
    tk.Spinbox(adv1, from_=1, to=10, textvariable=_beam_var,
               width=4).pack(side='left', padx=(0, 10))

    _vad_var = tk.BooleanVar(value=_wp.get('vad', True))
    ttk.Checkbutton(adv1, text="VAD filter", variable=_vad_var).pack(side='left')

    # Row 2: Offset, Max width, Max lead
    adv2 = ttk.Frame(adv_frame)
    adv2.pack(fill='x', padx=4, pady=2)

    ttk.Label(adv2, text="Offset (s):").pack(side='left', padx=(0, 4))
    _offset_var = tk.DoubleVar(value=_wp.get('offset', 0.0))
    tk.Spinbox(adv2, from_=-999, to=999, increment=0.5,
               textvariable=_offset_var, width=6).pack(side='left', padx=(0, 10))

    ttk.Label(adv2, text="Max width:").pack(side='left', padx=(0, 4))
    _max_width_var = tk.IntVar(value=_wp.get('max_width', 0))
    tk.Spinbox(adv2, from_=0, to=200, increment=1,
               textvariable=_max_width_var, width=5).pack(side='left', padx=(0, 10))

    ttk.Label(adv2, text="Max lead (s):").pack(side='left', padx=(0, 4))
    _max_lead_var = tk.DoubleVar(value=_wp.get('max_lead', 0.0))
    tk.Spinbox(adv2, from_=0, to=10, increment=0.25,
               textvariable=_max_lead_var, width=5).pack(side='left', padx=(0, 4))
    ttk.Label(adv2, text="(0=off, 0.5 rec.)").pack(side='left')

    # Row 3: WhisperX batch size
    adv3 = ttk.Frame(adv_frame)
    adv3.pack(fill='x', padx=4, pady=2)

    ttk.Label(adv3, text="WX Batch Size:").pack(side='left', padx=(0, 4))
    _batch_size_var = tk.IntVar(value=_wp.get('batch_size', 16))
    tk.Spinbox(adv3, from_=1, to=64, increment=1,
               textvariable=_batch_size_var, width=5).pack(side='left', padx=(0, 4))
    ttk.Label(adv3, text="(WhisperX only -- lower if GPU OOM)").pack(side='left')

    # Row 4: Checkbuttons
    adv4 = ttk.Frame(adv_frame)
    adv4.pack(fill='x', padx=4, pady=(2, 4))

    _word_ts_var = tk.BooleanVar(value=_wp.get('word_timestamps', False))
    ttk.Checkbutton(adv4, text="Word timestamps",
                    variable=_word_ts_var).pack(side='left', padx=(0, 16))

    _skip_existing_var = tk.BooleanVar(value=_wp.get('skip_existing', False))
    ttk.Checkbutton(adv4, text="Skip already-subtitled",
                    variable=_skip_existing_var).pack(side='left')

    # ── Buttons ──
    btn_row = ttk.Frame(settings_frame)
    btn_row.grid(row=row, column=0, columnspan=2, sticky='ew', padx=4, pady=(8, 4))
    btn_row.columnconfigure(0, weight=1)
    btn_row.columnconfigure(1, weight=1)
    row += 1

    start_btn = ttk.Button(btn_row, text="Extract Subtitles", command=lambda: _start())
    start_btn.grid(row=0, column=0, sticky='ew', padx=(0, 4))

    cancel_btn = ttk.Button(btn_row, text="Cancel", command=lambda: _cancel(),
                             state='disabled')
    cancel_btn.grid(row=0, column=1, sticky='ew')

    # ── Progress ──
    progress_var = tk.DoubleVar(value=0)
    progress_bar = ttk.Progressbar(settings_frame, variable=progress_var,
                                    maximum=100, mode='determinate')
    progress_bar.grid(row=row, column=0, columnspan=2, sticky='ew', padx=4, pady=(4, 0))
    row += 1

    progress_lbl = ttk.Label(settings_frame, text="", anchor='w')
    progress_lbl.grid(row=row, column=0, columnspan=2, sticky='w', padx=4)
    row += 1

    # ══════════════════════════════════════════════════════════════════
    # Log panel
    # ══════════════════════════════════════════════════════════════════

    log_lf = ttk.LabelFrame(right, text="Log")
    log_lf.grid(row=0, column=0, sticky='nsew', rowspan=2)
    log_lf.columnconfigure(0, weight=1)
    log_lf.rowconfigure(0, weight=1)

    log_text = tk.Text(log_lf, wrap='word', state='disabled',
                        padx=8, pady=6, borderwidth=1, relief='sunken')
    log_text.grid(row=0, column=0, sticky='nsew')
    log_sb = ttk.Scrollbar(log_lf, orient='vertical', command=log_text.yview)
    log_sb.grid(row=0, column=1, sticky='ns')
    log_text['yscrollcommand'] = log_sb.set

    log_text.tag_config("info")
    log_text.tag_config("success", foreground="#2e8b57")
    log_text.tag_config("warning", foreground="#e8a317")
    log_text.tag_config("error",   foreground="#cd3333")

    # ══════════════════════════════════════════════════════════════════
    # Preview panel
    # ══════════════════════════════════════════════════════════════════

    _preview_title = tk.StringVar(value="Subtitle Preview")

    preview_lf = tk.LabelFrame(right, textvariable=_preview_title,
                               padx=4, pady=4)
    preview_lf.grid(row=2, column=0, sticky='nsew', pady=(8, 0), rowspan=2)
    preview_lf.columnconfigure(0, weight=1)
    preview_lf.rowconfigure(0, weight=1)

    ttk.Label(preview_lf, text="(double-click a file to preview)",
              anchor='w').grid(row=0, column=0, sticky='w', padx=4)

    preview_text = tk.Text(preview_lf, wrap='none', state='disabled',
                            padx=8, pady=6, borderwidth=1, relief='sunken')
    preview_text.grid(row=1, column=0, sticky='nsew')
    preview_sb = ttk.Scrollbar(preview_lf, orient='vertical',
                                command=preview_text.yview)
    preview_sb.grid(row=1, column=1, sticky='ns')
    preview_text['yscrollcommand'] = preview_sb.set

    # ══════════════════════════════════════════════════════════════════
    # Status bar (simple ttk.Label at bottom)
    # ══════════════════════════════════════════════════════════════════

    status_frame = ttk.Frame(win)
    status_frame.pack(fill='x', side='bottom', padx=8, pady=(0, 4))
    status_frame.columnconfigure(0, weight=1)

    _status_var = tk.StringVar(value="Ready")
    ttk.Label(status_frame, textvariable=_status_var,
              anchor='w').grid(row=0, column=0, sticky='w')

    gpu = detect_device()
    badge_txt = "GPU" if gpu == "cuda" else "CPU only"
    ttk.Label(status_frame, text=badge_txt, anchor='e').grid(
        row=0, column=1, sticky='e')

    # ══════════════════════════════════════════════════════════════════
    # Helper functions
    # ══════════════════════════════════════════════════════════════════

    def _log_write(msg: str, tag: str = "info"):
        log_text.config(state="normal")
        log_text.insert("end", msg + "\n", tag)
        log_text.see("end")
        log_text.config(state="disabled")

    def _log_clear():
        log_text.config(state="normal")
        log_text.delete("1.0", "end")
        log_text.config(state="disabled")

    def _show_preview(idx: int):
        segments = _results.get(idx)
        if not segments:
            return
        _preview_idx[0] = idx
        name = _file_paths[idx].name
        _preview_title.set(f"Preview -- {name}")

        vtt_style = _vtt_style_var.get().strip() or None
        if _fmt_srt.get():
            text = segments_to_srt(segments)
        else:
            text = segments_to_vtt(segments, style=vtt_style)

        preview_text.config(state="normal")
        preview_text.delete("1.0", "end")
        preview_text.insert("end", text)
        preview_text.config(state="disabled")

    def _clear_preview():
        _preview_title.set("Subtitle Preview")
        preview_text.config(state="normal")
        preview_text.delete("1.0", "end")
        preview_text.config(state="disabled")

    def _get_output_dir() -> str | None:
        outdir_str = _outdir_var.get().strip()
        return None if outdir_str in ("", "Same folder as each input") else outdir_str

    def _get_fmt_list() -> list[str]:
        fmt_parts = []
        if _fmt_srt.get():
            fmt_parts.append("srt")
        if _fmt_vtt.get():
            fmt_parts.append("vtt")
        return fmt_parts

    # ── dep check ──
    def _check_deps_on_start():
        missing = check_deps()
        if missing:
            msg = "Missing dependencies:\n\n" + "\n".join(f"  {m}" for m in missing)
            _log_write(msg, "error")
            _status_var.set("Missing dependencies -- see log")
        else:
            _log_write("All dependencies found.", "success")
            dnd_msg = "  Drag & drop files, or use the buttons above." if HAS_DND else ""
            _log_write(f"Add files to the list, then click Extract.{dnd_msg}", "info")

    # ── preferences ──
    def _gather_settings() -> dict:
        return {
            "backend": _backend_var.get(),
            "model": _model_var.get(),
            "language": _lang_var.get(),
            "task": _task_var.get(),
            "device": _device_var.get(),
            "beam_size": _beam_var.get(),
            "vad": _vad_var.get(),
            "fmt_srt": _fmt_srt.get(),
            "fmt_vtt": _fmt_vtt.get(),
            "vtt_style": _vtt_style_var.get(),
            "outdir": _outdir_var.get(),
            "offset": _offset_var.get(),
            "max_width": _max_width_var.get(),
            "word_timestamps": _word_ts_var.get(),
            "skip_existing": _skip_existing_var.get(),
            "max_lead": _max_lead_var.get(),
            "batch_size": _batch_size_var.get(),
        }

    def _apply_settings(settings: dict):
        if not settings:
            return
        for key, var in [
            ("backend", _backend_var),
            ("model", _model_var),
            ("language", _lang_var),
            ("task", _task_var),
            ("device", _device_var),
            ("outdir", _outdir_var),
            ("vtt_style", _vtt_style_var),
        ]:
            if key in settings:
                var.set(settings[key])

        if "beam_size" in settings:
            _beam_var.set(int(settings["beam_size"]))
        if "vad" in settings:
            _vad_var.set(bool(settings["vad"]))
        if "fmt_srt" in settings:
            _fmt_srt.set(bool(settings["fmt_srt"]))
        if "fmt_vtt" in settings:
            _fmt_vtt.set(bool(settings["fmt_vtt"]))
        if "offset" in settings:
            _offset_var.set(float(settings["offset"]))
        if "max_width" in settings:
            _max_width_var.set(int(settings["max_width"]))
        if "word_timestamps" in settings:
            _word_ts_var.set(bool(settings["word_timestamps"]))
        if "skip_existing" in settings:
            _skip_existing_var.set(bool(settings["skip_existing"]))
        if "max_lead" in settings:
            _max_lead_var.set(float(settings["max_lead"]))
        if "batch_size" in settings:
            _batch_size_var.set(int(settings["batch_size"]))

    def _save_whisper_prefs():
        sp = _gather_settings()
        app._whisper_prefs = sp
        try:
            prefs_path = getattr(app, '_prefs_path', None)
            if prefs_path:
                if isinstance(prefs_path, str):
                    p = Path(prefs_path)
                else:
                    p = prefs_path() if callable(prefs_path) else Path(str(prefs_path))
                if p.exists():
                    prefs = json.loads(p.read_text())
                else:
                    prefs = {}
                prefs['whisper_transcriber'] = sp
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(json.dumps(prefs, indent=2))
        except Exception:
            pass

    # Apply loaded prefs
    _apply_settings(_wp)

    # ── start / cancel ──

    def _start():
        try:
            _start_inner()
        except Exception as exc:
            import traceback
            tb = traceback.format_exc()
            _log_write(f"Error starting transcription:\n{tb}", "error")
            messagebox.showerror("Error", f"Failed to start:\n{exc}",
                                 parent=win)
            _processing[0] = False
            start_btn.config(state="normal")
            cancel_btn.config(state="disabled")
            _set_file_buttons_state("normal")

    def _start_inner():
        if not _file_paths:
            messagebox.showwarning("No files", "Add at least one file to the list.",
                                   parent=win)
            return
        if not _fmt_srt.get() and not _fmt_vtt.get():
            messagebox.showwarning("No format", "Select at least one output format.",
                                   parent=win)
            return

        # Check that at least one backend is available
        backend = _backend_var.get()
        if not is_backend_available(backend):
            other = "whisperx" if backend == "faster-whisper" else "faster-whisper"
            if is_backend_available(other):
                messagebox.showwarning(
                    "Backend Not Found",
                    f"'{backend}' is not installed.\n\n"
                    f"Switching to '{other}'.",
                    parent=win)
                _backend_var.set(other)
                backend = other
            else:
                messagebox.showerror(
                    "No Backend",
                    "No transcription backend installed.\n\n"
                    "Install one:\n"
                    "  pip install faster-whisper\n"
                    "  pip install whisperx",
                    parent=win)
                return

        _results.clear()
        _preview_idx[0] = None
        _clear_preview()
        _log_clear()
        progress_var.set(0)
        progress_lbl.config(text="")
        _processing[0] = True
        start_btn.config(state="disabled")
        cancel_btn.config(state="normal")
        _set_file_buttons_state("disabled")

        for i in range(file_listbox.size()):
            file_listbox.itemconfig(i, fg=COLOR_QUEUED)

        n = len(_file_paths)
        _status_var.set(f"Processing 0 / {n}...")

        lang_code = LANGUAGES[_lang_var.get()]
        task_code = TASKS[_task_var.get()]
        device = _device_var.get()
        if not device or device == "auto":
            device = detect_device()
            _device_var.set(device)

        output_dir = _get_output_dir()
        fmt_list = _get_fmt_list()

        backend = _backend_var.get()
        task_label = "translate->en" if task_code == "translate" else "transcribe"
        _log_write(
            f"Starting batch: {n} file(s)  "
            f"[backend={backend} model={_model_var.get()} lang={lang_code or 'auto'} "
            f"task={task_label} device={device}]",
            "info",
        )

        _worker[0] = BatchTranscribeWorker(
            q=_queue,
            paths=list(_file_paths),
            model_size=_model_var.get(),
            language=lang_code,
            device=device,
            beam_size=_beam_var.get(),
            vad=_vad_var.get(),
            task=task_code,
            word_timestamps=_word_ts_var.get(),
            skip_existing=_skip_existing_var.get(),
            output_dir=output_dir,
            output_formats=fmt_list,
            backend=backend,
            batch_size=_batch_size_var.get(),
        )
        _worker[0].start()

    def _cancel():
        if _worker[0]:
            _worker[0].stop()
        cancel_btn.config(state="disabled")
        _status_var.set("Cancelling...")

    # ── save one file ──

    def _save_one(idx: int):
        segments = _results.get(idx)
        if not segments:
            return

        segments = post_process_segments(
            segments,
            word_timestamps=_word_ts_var.get(),
            max_line_length=_max_width_var.get(),
            offset=_offset_var.get(),
            max_lead=_max_lead_var.get(),
        )
        _results[idx] = segments

        output = _get_output_dir()
        fmt_parts = _get_fmt_list()
        fmt = ",".join(fmt_parts)
        vtt_style = _vtt_style_var.get().strip() or None

        path = _file_paths[idx]
        try:
            write_output(segments, path, output, fmt, vtt_style=vtt_style)
            base = Path(output) if output else path.parent
            for ext in fmt_parts:
                saved = str(base / (path.stem + f".{ext}"))
                _log_write(f"Saved -> {saved}", "success")
        except Exception as exc:
            _log_write(f"Save failed for {path.name}: {exc}", "error")

    # ── batch completion ──

    def _on_batch_done():
        progress_var.set(100)
        done = len(_results)
        total = len(_file_paths)
        failed = total - done
        _processing[0] = False
        start_btn.config(state="normal")
        cancel_btn.config(state="disabled")
        _set_file_buttons_state("normal")

        if done > 0:
            first_done = next(iter(_results))
            _show_preview(first_done)

        summary = f"{done}/{total} complete"
        if failed:
            summary += f"  |  {failed} failed/skipped (see log)"
        _log_write(f"\nBatch done -- {summary}", "success")
        _status_var.set(summary)
        progress_lbl.config(text="Complete")

        send_notification(
            "Whisper Transcriber -- Batch Complete",
            f"{done}/{total} files transcribed successfully.",
        )

        _save_whisper_prefs()

    # ── queue polling ──

    def _poll_queue():
        try:
            while True:
                event, data = _queue.get_nowait()

                if event == "log":
                    tag = ("success" if "Done" in data[:10] or "ready" in data.lower()[:20]
                           else "error"   if "Error" in data[:10] or "Fatal" in data[:10]
                           else "warning" if "Skip" in data[:10] or "Cancelled" in data[:15]
                           else "info")
                    _log_write(data, tag)

                elif event == "next_file":
                    idx, total, path = data
                    _safe_itemconfig(idx, fg=COLOR_ACTIVE)
                    if 0 <= idx < file_listbox.size():
                        file_listbox.see(idx)
                    progress_var.set(0)
                    _status_var.set(f"Processing {idx+1} / {total}  --  {path.name}")

                elif event == "skip_file":
                    idx, path, reason = data
                    _safe_itemconfig(idx, fg=COLOR_SKIP)

                elif event == "progress":
                    current, total = data
                    pct = min(100.0, (current / total * 100) if total > 0 else 0)
                    progress_var.set(pct)
                    progress_lbl.config(
                        text=f"{timedelta(seconds=int(current))} / "
                             f"{timedelta(seconds=int(total))}  ({pct:.0f}%)"
                    )

                elif event == "file_done":
                    idx, path, segments = data
                    _results[idx] = segments
                    _safe_itemconfig(idx, fg=COLOR_DONE)
                    _save_one(idx)

                elif event == "file_error":
                    idx, path, exc = data
                    _safe_itemconfig(idx, fg=COLOR_ERROR)
                    _log_write(f"Error: {path.name}: {exc}", "error")

                elif event == "batch_done":
                    _on_batch_done()

        except queue.Empty:
            pass
        win.after(80, _poll_queue)

    # ── close handler ──

    def _close():
        _save_whisper_prefs()
        win.destroy()
        if getattr(app, '_standalone_mode', False):
            app.root.destroy()

    win.protocol('WM_DELETE_WINDOW', _close)

    # ── Initialize ──
    _check_deps_on_start()
    _poll_queue()

    win.update_idletasks()
    _log_write("Whisper Transcriber ready.", "info")


# ═══════════════════════════════════════════════════════════════════
# Standalone launcher
# ═══════════════════════════════════════════════════════════════════

def main():
    """Standalone entry point for the Whisper Transcriber."""
    from .standalone import create_standalone_root

    root, app = create_standalone_root(
        title="Whisper Transcriber",
        geometry="1100x800",
        minsize=(920, 700),
    )
    app._standalone_mode = True

    # Load whisper prefs from shared preferences
    prefs = getattr(app, '_prefs', {})
    app._whisper_prefs = prefs.get('whisper_transcriber', {})

    open_whisper_transcriber(app)
    root.mainloop()


if __name__ == '__main__':
    main()
