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
from .utils import (scaled_geometry, scaled_minsize, ask_open_files, ask_directory,
                    get_audio_streams, describe_audio_stream)
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
from .subtitle_filter_panel import SubtitleFilterPanel

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

# ── audio track selection ────────────────────────────────────────────────────
# A file may hold several audio tracks and only one of them is usually the one
# you want transcribed. These are BULK choices applied to every queued file;
# each file contributes whatever it actually has, and a file that offers
# nothing matching is skipped with a reason rather than silently transcribed
# from the wrong track.
AUDIO_TRACK_DEFAULT = "Default track"
AUDIO_TRACK_MODES = (
    AUDIO_TRACK_DEFAULT,   # ffmpeg's own pick -- behaviour before this existed
    "All tracks",
    "Commentary only",
    "Main only",
    "Track 1", "Track 2", "Track 3", "Track 4",
)

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
                 backend: str = "faster-whisper", batch_size: int = 16,
                 device_index: int = 0,
                 tracks: list[int | None] | None = None,
                 tags: list[str] | None = None):
        super().__init__(daemon=True)
        self.q = q
        self.paths = paths
        # One entry per path. `tracks` is the absolute ffprobe audio stream to
        # feed whisper (None = ffmpeg's own pick); `tags` is the filename suffix
        # that keeps two transcripts of the same video apart. The same path can
        # appear twice with different tracks — that is a two-commentary disc.
        self.tracks = tracks if tracks is not None else [None] * len(paths)
        self.tags = tags if tags is not None else [""] * len(paths)
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
        # ⚠️ WHICH GPU. Both backends take device_index and both defaulted to 0,
        # so a two-card box always loaded onto cuda:0 no matter what else was
        # living there. Tony hit this with only ~8 GB free on GPU0 (Merlin's
        # Coqui TTS had moved onto it) while GPU1 sat with ~12 GB spare —
        # large-v2 would not fit on the card it insisted on using.
        self.device_index = int(device_index or 0)
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
        # ⚠️ Both backends run in the ISOLATED ENGINE (a dedicated venv), not in this
        # process. The Suite does not import whisperx/torch at all and does not install
        # them into the user's Python. See modules/whisper_engine.py for why: on
        # 2026-08-18 installing WhisperX for the Suite reached into shared site-packages
        # and took an unrelated always-on voice assistant's STT offline.
        #
        # The old in-process paths (_run_whisperx / _run_faster_whisper) were removed
        # 2026-08-29 — confirmed unreachable (no caller but themselves, no fallback
        # branch ever reaches them, not even the "engine not installed" case below) and
        # they were the last remaining callers of `import whisperx` in the Suite.
        self._run_isolated()

    # ── isolated engine path ────────────────────────────────────────────────────
    def _run_isolated(self):
        """Batch-transcribe via the engine venv, preserving the GUI queue protocol.

        ⚠️ skip-existing stays HERE, not in the worker — it needs output_dir and
        output_formats, which are UI concerns. That means the worker sees a FILTERED
        list, so its indices are not the caller's indices. `keep` maps them back;
        getting that wrong would report results against the wrong file.
        """
        from . import whisper_client as wc
        from . import whisper_engine as we

        total = len(self.paths)

        # Filter first, announcing skips against their ORIGINAL index.
        keep = []
        for idx, path in enumerate(self.paths):
            tag = self.tags[idx] if idx < len(self.tags) else ""
            if self.skip_existing and subtitle_exists(
                    path, self.output_dir, self.output_formats, suffix=tag):
                self.q.put(("skip_file", (idx, path, "subtitle already exists")))
                self.q.put(("log", f"Skipping (already exists): {path.name}{tag}"))
                continue
            keep.append((idx, path))

        if not keep:
            self.q.put(("batch_done", None))
            return

        if not we.is_installed():
            self.q.put(("log",
                        "The Whisper engine is not installed. Open the Transcriber's "
                        "backend selector to install it — it runs in its own isolated "
                        "environment and will not modify your system Python."))
            self.q.put(("batch_done", None))
            return

        self.q.put(("log", f"Loading {self.backend} model '{self.model_size}' "
                           f"[{self.device}:{self.device_index}] in the isolated engine..."))

        def _on_start(w_idx, path_str):
            idx, path = keep[w_idx]
            self.q.put(("next_file", (idx, total, path)))
            self.q.put(("log", f"\n-- [{idx+1}/{total}] {path.name}"))

        def _on_file(w_idx, path_str, segments):
            idx, path = keep[w_idx]
            self.q.put(("file_done", (idx, path, segments)))
            self.q.put(("log", f"Done: {len(segments)} segments  ->  {path.name}"))

        def _on_error(w_idx, path_str, message):
            idx, path = keep[w_idx]
            self.q.put(("file_error", (idx, path, RuntimeError(message))))
            self.q.put(("log", f"Error: {path.name}: {message}"))

        try:
            wc.transcribe_batch(
                [p for _, p in keep],
                tracks=[self.tracks[i] if i < len(self.tracks) else None
                        for i, _ in keep],
                model_size=self.model_size,
                language=self.language,
                device=self.device,
                engine=self.backend,
                beam_size=getattr(self, "beam_size", 5),
                vad=getattr(self, "vad", True),
                task=self.task,
                word_timestamps=getattr(self, "word_timestamps", False),
                batch_size=getattr(self, "batch_size", 16),
                on_start=_on_start,
                on_file=_on_file,
                on_error=_on_error,
                progress=lambda t: self.q.put(("log", f"   {t}")) if t else None,
                should_stop=lambda: self._stop_event.is_set(),
            )
        except wc.EngineMissing as e:
            self.q.put(("log", str(e)))
        except Exception as exc:
            self.q.put(("log", f"Engine error: {exc}"))
            tb = getattr(exc, "tb", None)
            if tb:
                self.q.put(("log", tb))

        if self._stop_event.is_set():
            self.q.put(("log", "Batch cancelled."))
        self.q.put(("batch_done", None))


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
    _file_paths = []       # list of Path objects  (one entry per LIST ROW)
    _results = {}          # job index -> list of segments
    _preview_idx = [None]
    # ⚠️ A JOB IS NOT A FILE. With audio-track selection one video can produce
    # several transcripts, so the batch runs over jobs while the tree still has
    # one PARENT per file. Every queue event carries a JOB index; use _jobs[idx]
    # (never _file_paths[idx]) to find the source file, and _tag_job() to colour
    # it. Built fresh by _build_jobs() at the start of every run.
    _jobs = []             # list of {"path", "track", "row", "ord", "tag", ...}
    # Per-file chosen audio ordinals: str(path) -> {0, 2}. Seeded from the Audio
    # dropdown, then hand-editable in the tree. THIS is what actually gets
    # transcribed — the dropdown is only ever a bulk seed, as in sub_ripper.
    _want = {}

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
                added += 1
        if added:
            _rebuild_tree()
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
                added += 1
        if added:
            _rebuild_tree()
            _status_var.set(f"Added {added} file(s) from folder -- {len(_file_paths)} total")
            _log_write(f"Added {added} media file(s) from {directory}", "info")

    def _selected_rows():
        """File rows implied by the tree selection (a child selects its parent)."""
        rows = set()
        for iid in file_tree.selection():
            row = _row_of_iid(iid)
            if row is not None:
                rows.add(row)
        return sorted(rows)

    def _remove_selected():
        rows = _selected_rows()
        if not rows:
            return
        for row in reversed(rows):
            _want.pop(str(_file_paths[row]), None)
            _file_paths.pop(row)
        # Results are keyed by JOB index and jobs are rebuilt per run, so a
        # removal mid-session invalidates them rather than shifting them.
        _results.clear()
        _preview_idx[0] = None
        _clear_preview()
        _rebuild_tree()

    def _clear_files():
        _file_paths.clear()
        _want.clear()
        _results.clear()
        _jobs.clear()
        _preview_idx[0] = None
        _clear_preview()
        _rebuild_tree()

    for txt, cmd in [
        ("Add Files", _add_files),
        ("Add Folder", _add_folder),
        ("Remove", _remove_selected),
        ("Clear", _clear_files),
    ]:
        btn = ttk.Button(file_toolbar, text=txt, command=cmd)
        btn.pack(side='left', padx=(0, 4))
        _file_btns.append(btn)

    # Post-transcription filters — the same set the Sub Extractor offers, applied
    # to each .srt after it is written. Shared panel so the two lists can't drift.
    #
    # ⚠️ Lives in the TOP toolbar next to Clear, not in Advanced. It started in the
    # Advanced row and Tony could not find it — that whole box sits below the file
    # list, so a short window pushes it off the bottom entirely. A control you have
    # to resize the window to reach is one nobody uses. Up here it is always
    # visible, and it reads as an action alongside the other actions rather than
    # as a setting among spinboxes.
    _filter_panel = SubtitleFilterPanel(
        win, app, file_toolbar, saved=_wp.get('filters', {}),
        title="Post-Transcription Filters",
        blurb=("Selected filters are applied to each .srt\n"
               "after it is written. VTT output is not filtered."))

    # ── file tree: one parent per file, one child per audio track ───────────
    # Mirrors the Sub Extractor's "global defaults + per-file override": the
    # Audio dropdown SEEDS the ticks, then you hand-correct any file. Audio
    # can't use sub_ripper's fixed type columns because "commentary" is not one
    # thing — a disc can carry two and you may want only the second.
    file_list_frame = ttk.LabelFrame(left, text="Files")
    file_list_frame.grid(row=1, column=0, sticky='nsew')
    file_list_frame.columnconfigure(0, weight=1)
    file_list_frame.rowconfigure(0, weight=1)

    GLYPH_ON, GLYPH_OFF = '☑', '☐'      # ☑ ☐

    file_tree = ttk.Treeview(file_list_frame, columns=('use', 'status'),
                             show='tree headings', selectmode='extended')
    file_tree.heading('#0', text='File / audio track', anchor='w')
    file_tree.heading('use', text='Use', anchor='center')
    file_tree.heading('status', text='', anchor='w')
    file_tree.column('#0', width=360, minwidth=200, stretch=True)
    file_tree.column('use', width=52, minwidth=44, stretch=False, anchor='center')
    file_tree.column('status', width=96, minwidth=60, stretch=False, anchor='w')
    file_tree.grid(row=0, column=0, sticky='nsew')

    file_sb = ttk.Scrollbar(file_list_frame, orient='vertical',
                            command=file_tree.yview)
    file_sb.grid(row=0, column=1, sticky='ns')
    file_tree['yscrollcommand'] = file_sb.set

    for tag, colour in (('queued', COLOR_QUEUED), ('active', COLOR_ACTIVE),
                        ('done', COLOR_DONE), ('error', COLOR_ERROR),
                        ('skip', COLOR_SKIP)):
        file_tree.tag_configure(tag, foreground=colour)

    def _row_of_iid(iid):
        """File row for a tree item, whether it is a file or one of its tracks."""
        if not iid:
            return None
        head = iid.split('t')[0]
        try:
            return int(head.lstrip('f'))
        except ValueError:
            return None

    def _ord_of_iid(iid):
        """Audio ordinal for a track item, or None if it is a file row."""
        if 't' not in iid:
            return None
        try:
            return int(iid.split('t')[1])
        except (IndexError, ValueError):
            return None

    def _on_tree_double_click(_event=None):
        sel = file_tree.selection()
        if not sel:
            return
        iid = sel[0]
        row, o = _row_of_iid(iid), _ord_of_iid(iid)
        for j_idx, job in enumerate(_jobs):
            if job["row"] != row or j_idx not in _results:
                continue
            if o is None or job.get("ord") == o:
                _show_preview(j_idx)
                return
        _status_var.set("Not yet transcribed -- run extraction first.")

    file_tree.bind("<Double-Button-1>", _on_tree_double_click)

    _range_anchor = {'iid': None, 'state': None}

    def _set_track(row, o, state):
        """Tick or clear one track. Returns True if it changed."""
        path = _file_paths[row]
        chosen = _wants(path)
        if state and o not in chosen:
            chosen.add(o)
        elif not state and o in chosen:
            chosen.discard(o)
        else:
            return False
        iid = f"f{row}t{o}"
        if file_tree.exists(iid):
            file_tree.set(iid, 'use', GLYPH_ON if state else GLYPH_OFF)
        if file_tree.exists(f"f{row}"):
            streams = _audio_streams_cached(path)
            file_tree.set(f"f{row}", 'use', f"{len(chosen)} / {len(streams)}")
        return True

    def _on_tree_click(event):
        """Toggle on the Use column; shift-click extends over a range.

        Clicking a FILE row toggles all of its tracks at once — the quick way
        to say "none of this one" without opening it.
        """
        if _processing[0]:
            return None
        if file_tree.identify_region(event.x, event.y) != 'cell':
            return None
        if file_tree.identify_column(event.x) != '#1':      # the Use column
            return None
        iid = file_tree.identify_row(event.y)
        if not iid:
            return None
        row, o = _row_of_iid(iid), _ord_of_iid(iid)
        if row is None:
            return None

        if o is None:                                       # a file row
            chosen = _wants(_file_paths[row])
            streams = _audio_streams_cached(_file_paths[row])
            new_state = len(chosen) < len(streams)          # partial -> all on
            for s in streams:
                _set_track(row, s['ord'], new_state)
            _range_anchor.update(iid=iid, state=new_state)
            _refresh_count()
            _refresh_audio_hint()
            return 'break'

        shift = bool(event.state & 0x0001)
        anchor = _range_anchor
        if shift and anchor['iid'] and 't' in anchor['iid']:
            # Walk the flattened visible order between anchor and here.
            flat = []
            for fid in file_tree.get_children(''):
                flat.append(fid)
                flat.extend(file_tree.get_children(fid))
            try:
                lo, hi = sorted((flat.index(anchor['iid']), flat.index(iid)))
            except ValueError:
                lo = hi = None
            if lo is not None:
                for mid in flat[lo:hi + 1]:
                    mo = _ord_of_iid(mid)
                    if mo is not None:
                        _set_track(_row_of_iid(mid), mo, anchor['state'])
                _refresh_count()
                _refresh_audio_hint()
                return 'break'

        new_state = o not in _wants(_file_paths[row])
        _set_track(row, o, new_state)
        _range_anchor.update(iid=iid, state=new_state)
        _refresh_count()
        _refresh_audio_hint()
        return 'break'

    file_tree.bind('<Button-1>', _on_tree_click, add='+')

    # Enable drag-and-drop
    if HAS_DND:
        try:
            file_tree.drop_target_register(DND_FILES)
            file_tree.dnd_bind("<<Drop>>", lambda e: _on_drop(e))
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
        jobs = sum(len(_wants(p)) for p in _file_paths) if _file_paths else 0
        _file_count_var.set(
            f"{n} file{'s' if n != 1 else ''}  --  {jobs} transcript"
            f"{'s' if jobs != 1 else ''} selected")

    def _wants(path):
        """This file's chosen audio ordinals, seeded from the Audio dropdown.

        Seeded LAZILY rather than when the file is added, so changing the
        dropdown before adding files still does the obvious thing — the same
        reason sub_ripper's _wants() is lazy.
        """
        key = str(path)
        if key not in _want:
            _want[key] = _seed_for(path, _audio_var.get())
        return _want[key]

    def _seed_for(path, mode):
        """Which ordinals the bulk mode would tick for this file."""
        if path.suffix.lower() in AUDIO_EXTENSIONS:
            return {0}
        streams = _audio_streams_cached(path)
        if not streams:
            return set()
        if mode == AUDIO_TRACK_DEFAULT:
            # ffmpeg's pick: most channels, ties to the lowest index.
            best = max(streams, key=lambda s: (s.get('channels', 0), -s['ord']))
            return {best['ord']}
        if mode == "All tracks":
            return {s['ord'] for s in streams}
        if mode == "Commentary only":
            return {s['ord'] for s in streams if s['role'] == 'commentary'}
        if mode == "Main only":
            return {s['ord'] for s in streams if s['role'] == 'main'}
        if mode.startswith("Track "):
            want = int(mode.split()[1]) - 1
            return {s['ord'] for s in streams if s['ord'] == want}
        return set()

    def _reseed_wants():
        """Re-apply the bulk mode over every file, discarding hand edits."""
        mode = _audio_var.get()
        for p in _file_paths:
            _want[str(p)] = _seed_for(p, mode)

    def _rebuild_tree():
        """Redraw the whole tree from _file_paths + _want.

        Cheap enough to do wholesale: ffprobe results are cached per path, so
        this is string formatting, not I/O.
        """
        open_rows = {_row_of_iid(i) for i in file_tree.get_children('')
                     if file_tree.item(i, 'open')}
        sel_rows = {_row_of_iid(i) for i in file_tree.selection()}
        for i in file_tree.get_children(''):
            file_tree.delete(i)

        for row, path in enumerate(_file_paths):
            chosen = _wants(path)
            streams = ([] if path.suffix.lower() in AUDIO_EXTENSIONS
                       else _audio_streams_cached(path))
            fid = f"f{row}"
            count = (f"{len(chosen)} / {len(streams)}" if streams
                     else ("audio" if not streams else ""))
            file_tree.insert('', 'end', iid=fid, text=path.name,
                             values=(count, ''), tags=('queued',),
                             open=(row in open_rows) or len(_file_paths) <= 12)
            for s in streams:
                file_tree.insert(
                    fid, 'end', iid=f"{fid}t{s['ord']}",
                    text='    ' + describe_audio_stream(s),
                    values=(GLYPH_ON if s['ord'] in chosen else GLYPH_OFF, ''),
                    tags=('queued',))
            if row in sel_rows:
                file_tree.selection_add(fid)
        _refresh_count()
        try:
            _refresh_audio_hint()
        except NameError:
            pass

    def _on_drop(event):
        if _processing[0]:
            return
        # ⚠️ DO NOT parse this with a regex. tkinterdnd2 hands over a Tcl LIST,
        # where a path containing spaces is wrapped in braces — and Tcl braces
        # NEST. The old pattern (r'\{([^}]+)\}|(\S+)') stopped at the first
        # inner '}', so any file with braces in its NAME was torn in half:
        #
        #   {/x/Haven - S02E11 {Commentary}.mkv}
        #     ->  '/x/Haven - S02E11 {Commentary'   and   '.mkv}'
        #
        # Neither is a real path, so the drop silently added nothing. That hits
        # every `{Commentary}` and `{edition-...}` file. tk.splitlist() is Tcl's
        # own parser and gets nesting right, including two braced groups in one
        # name ("... {Commentary} {Commentary}.mkv").
        try:
            paths = list(win.tk.splitlist(event.data))
        except Exception:
            paths = [p for p in (event.data or "").split() if p]

        all_exts = WS_VIDEO_EXTENSIONS | AUDIO_EXTENSIONS
        added = 0
        for p in paths:
            path = Path(p)
            if path.is_dir():
                for media_file in find_media_files(path):
                    if media_file not in _file_paths:
                        _file_paths.append(media_file)
                        added += 1
            elif path.is_file() and path.suffix.lower() in all_exts:
                if path not in _file_paths:
                    _file_paths.append(path)
                    added += 1

        if added:
            _rebuild_tree()
            _status_var.set(f"Dropped {added} file(s) -- {len(_file_paths)} total")
            _log_write(f"Dropped {added} file(s).", "info")

    def _set_file_buttons_state(state: str):
        for btn in _file_btns:
            btn.config(state=state)

    def _tag_job(idx: int, tag: str):
        """Colour a job's track row, and its file row, by status tag."""
        if not (0 <= idx < len(_jobs)):
            return
        job = _jobs[idx]
        row, o = job["row"], job.get("ord")
        for iid in (f"f{row}t{o}" if o is not None else None, f"f{row}"):
            if iid and file_tree.exists(iid):
                file_tree.item(iid, tags=(tag,))

    def _reset_tags():
        for fid in file_tree.get_children(''):
            file_tree.item(fid, tags=('queued',))
            for cid in file_tree.get_children(fid):
                file_tree.item(cid, tags=('queued',))

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
        if backend == "whisperx" and not _is_backend_cached("whisperx"):
            if _prompt_install_backend("whisperx"):
                _backend_cache.pop("whisperx", None)  # clear cache after install
                _log_write("WhisperX installed successfully.", "success")
                _refresh_backend_hints()
            else:
                _log_write("whisperx is not installed.", "warning")
        elif backend == "faster-whisper" and not _is_backend_cached("faster-whisper"):
            if _prompt_install_backend("faster-whisper"):
                _backend_cache.pop("faster-whisper", None)
                _log_write("faster-whisper installed successfully.", "success")
                _refresh_backend_hints()
            else:
                _log_write("faster-whisper is not installed.", "warning")

    backend_cb.bind("<<ComboboxSelected>>", _on_backend_change)

    ttk.Label(bm_frame, text="Model:").grid(row=0, column=2, sticky='w', padx=(0, 4))
    _model_var = tk.StringVar(value=_wp.get('model', 'small'))
    model_cb = ttk.Combobox(bm_frame, textvariable=_model_var,
                             values=MODELS, state='readonly', width=14)
    model_cb.grid(row=0, column=3, sticky='ew')

    # Backend availability hint (updated after installs)
    _hint_var = tk.StringVar(value="Checking backends...")

    # Cache for backend availability — avoids repeated 5s imports
    _backend_cache = {}  # backend_name -> bool

    def _is_backend_cached(backend):
        """Check backend availability with caching."""
        if backend not in _backend_cache:
            _backend_cache[backend] = is_backend_available(backend)
        return _backend_cache[backend]

    def _refresh_backend_hints():
        hints = []
        if _is_backend_cached("faster-whisper"):
            hints.append("faster-whisper: available")
        else:
            hints.append("faster-whisper: not installed")
        if _is_backend_cached("whisperx"):
            hints.append("whisperx: available")
        else:
            hints.append("whisperx: not installed")
        _hint_var.set(" | ".join(hints))

    def _refresh_backend_hints_async(on_complete=None):
        """Check backends in a subprocess so the window isn't blocked by GIL.

        Importing faster-whisper/whisperx pulls in CTranslate2, transformers,
        and PyTorch (~5s). Even in a thread, these hold the GIL and freeze Tk.
        A subprocess checks importability and detects GPU in one shot.
        """
        def _check():
            # Check backend availability + detect GPU device in one subprocess
            # to avoid importing heavy packages in the main process at startup.
            check_script = (
                "import json, sys\n"
                "result = {}\n"
                "for pkg in ['faster_whisper', 'whisperx']:\n"
                "    try:\n"
                "        __import__(pkg)\n"
                "        result[pkg] = True\n"
                "    except ImportError:\n"
                "        result[pkg] = False\n"
                "try:\n"
                "    import ctranslate2\n"
                "    result['device'] = 'cuda' if ctranslate2.get_cuda_device_count() > 0 else 'cpu'\n"
                "except Exception:\n"
                "    result['device'] = 'cpu'\n"
                "print(json.dumps(result))\n"
            )
            try:
                proc = subprocess.run(
                    [sys.executable, '-c', check_script],
                    capture_output=True, text=True, timeout=30)
                if proc.returncode == 0 and proc.stdout.strip():
                    import json
                    data = json.loads(proc.stdout.strip())
                    _backend_cache["faster-whisper"] = data.get("faster_whisper", False)
                    _backend_cache["whisperx"] = data.get("whisperx", False)
                    _backend_cache["_device"] = data.get("device", "cpu")
                else:
                    _backend_cache["faster-whisper"] = False
                    _backend_cache["whisperx"] = False
                    _backend_cache["_device"] = "cpu"
            except Exception:
                _backend_cache["faster-whisper"] = False
                _backend_cache["whisperx"] = False
                _backend_cache["_device"] = "cpu"
            try:
                def _update():
                    _refresh_backend_hints()
                    # Update device combobox if still set to "auto"
                    detected = _backend_cache.get("_device", "cpu")
                    if _device_var.get() == 'auto':
                        _device_var.set(detected)
                    # Update GPU/CPU badge in status bar
                    _gpu_badge_var.set("GPU" if detected == "cuda" else "CPU only")
                    if on_complete:
                        on_complete()
                win.after(0, _update)
            except Exception:
                pass
        threading.Thread(target=_check, daemon=True).start()

    ttk.Label(settings_frame, textvariable=_hint_var, anchor='w').grid(
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

    # ── Audio track ──
    # ⚠️ Why this exists: with no -map, ffmpeg extracts the audio stream with the
    # MOST CHANNELS. On a disc rip that is the 5.1 feature, so a stereo commentary
    # was unreachable and a second commentary invisible. Default stays 'Default
    # track', which is the old behaviour exactly.
    ttk.Label(lt_frame, text="Audio:").grid(row=1, column=0, sticky='w',
                                            padx=(0, 4), pady=(4, 0))
    _audio_var = tk.StringVar(value=_wp.get('audio_tracks', AUDIO_TRACK_DEFAULT))
    audio_cb = ttk.Combobox(lt_frame, textvariable=_audio_var,
                            values=list(AUDIO_TRACK_MODES), state='readonly',
                            width=14)
    audio_cb.grid(row=1, column=1, sticky='ew', padx=(0, 10), pady=(4, 0))

    _audio_hint_var = tk.StringVar(value="")
    ttk.Label(lt_frame, textvariable=_audio_hint_var, anchor='w').grid(
        row=1, column=2, columnspan=2, sticky='ew', pady=(4, 0))

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
    # Start with saved preference or "auto"; actual GPU detection
    # runs in the background check thread to avoid blocking the UI
    # for ~5s (importing ctranslate2 pulls in PyTorch/transformers).
    _default_device = _wp.get('device', 'auto') or 'auto'
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
    _max_width_var = tk.IntVar(value=_wp.get('max_width', 42))
    tk.Spinbox(adv2, from_=0, to=200, increment=1,
               textvariable=_max_width_var, width=5).pack(side='left', padx=(0, 10))

    ttk.Label(adv2, text="Max lead (s):").pack(side='left', padx=(0, 4))
    _max_lead_var = tk.DoubleVar(value=_wp.get('max_lead', 0.0))
    tk.Spinbox(adv2, from_=0, to=10, increment=0.25,
               textvariable=_max_lead_var, width=5).pack(side='left', padx=(0, 4))
    ttk.Label(adv2, text="(0=off, 0.5 rec.)").pack(side='left')

    # Post-transcription filters — the same set the Sub Extractor offers, applied
    # to each .srt after it is written. Shared panel so the two lists can't drift.
    # (the Filters button lives in the top toolbar next to Clear — see above)

    # Row 2b: readability — reading speed + pause-split threshold (cue segmenter)
    adv2b = ttk.Frame(adv_frame)
    adv2b.pack(fill='x', padx=4, pady=2)

    ttk.Label(adv2b, text="Reading (cps):").pack(side='left', padx=(0, 4))
    _cps_var = tk.DoubleVar(value=_wp.get('reading_speed', 17.0))
    tk.Spinbox(adv2b, from_=5, to=30, increment=0.5,
               textvariable=_cps_var, width=5).pack(side='left', padx=(0, 10))

    ttk.Label(adv2b, text="Split gap (s):").pack(side='left', padx=(0, 4))
    _gap_var = tk.DoubleVar(value=_wp.get('split_gap', 0.5))
    tk.Spinbox(adv2b, from_=0.1, to=3.0, increment=0.05,
               textvariable=_gap_var, width=5).pack(side='left', padx=(0, 4))
    ttk.Label(adv2b, text="(needs word timestamps)").pack(side='left')

    # Row 3: WhisperX batch size
    adv3 = ttk.Frame(adv_frame)
    adv3.pack(fill='x', padx=4, pady=2)

    # ── GPU picker ──
    # ⚠️ Both backends default to device_index=0, so on a two-card box this tool
    # always grabbed cuda:0 regardless of what else was resident there. Tony,
    # 2026-08-09, with Merlin's TTS on GPU0: "can we force it to use GPU1?
    # Might not be a bad idea to have a drop down for using either or."
    # Free VRAM is shown in the label because that is the number that decides
    # it — large-v2 needs ~10 GB and will OOM on a card that looks idle.
    # ⚠️ No "Auto" entry on purpose. Nothing here picks a card automatically —
    # both backends just default to index 0 — so an "Auto" label would promise
    # behaviour that does not exist. List the real cards and let the free-VRAM
    # figure make the choice obvious.
    _gpu_choices = {}
    try:
        from .gpu import enumerate_nvidia_gpus
        for _g in enumerate_nvidia_gpus():
            _i = int(_g.get('index', 0))
            # ⚠️ keys are vram_total / vram_used (MiB), NOT memory.total.
            # The first version guessed nvidia-smi's names, hit the except, and
            # would have silently rendered labels with no free-VRAM figure —
            # dropping the one number that makes this dropdown worth having.
            _name = str(_g.get('name', '?')).replace('NVIDIA ', '')
            _tot, _use = _g.get('vram_total'), _g.get('vram_used')
            if _tot is not None and _use is not None:
                _lbl = f"GPU {_i} — {_name} ({(int(_tot)-int(_use))/1024:.1f} GB free)"
            else:
                _lbl = f"GPU {_i} — {_name}"
            _gpu_choices[_lbl] = _i
    except Exception:
        pass
    ttk.Label(adv3, text="GPU:").pack(side='left', padx=(0, 4))
    if not _gpu_choices:                       # no NVIDIA card / nvidia-smi absent
        _gpu_choices = {"GPU 0": 0}
    _saved_idx = int(_wp.get('device_index', 0))
    _gpu_var = tk.StringVar(value=list(_gpu_choices)[0])
    for _l, _v in _gpu_choices.items():
        if _v == _saved_idx:
            _gpu_var.set(_l)
            break
    ttk.Combobox(adv3, textvariable=_gpu_var, values=list(_gpu_choices),
                 state='readonly', width=30).pack(side='left', padx=(0, 12))

    ttk.Label(adv3, text="WX Batch Size:").pack(side='left', padx=(0, 4))
    _batch_size_var = tk.IntVar(value=_wp.get('batch_size', 16))
    tk.Spinbox(adv3, from_=1, to=64, increment=1,
               textvariable=_batch_size_var, width=5).pack(side='left', padx=(0, 4))
    ttk.Label(adv3, text="(WhisperX only -- lower if GPU OOM)").pack(side='left')

    # Row 4: Checkbuttons
    adv4 = ttk.Frame(adv_frame)
    adv4.pack(fill='x', padx=4, pady=(2, 4))

    # On by default — word timing drives the scene-aware cue segmenter.
    _word_ts_var = tk.BooleanVar(value=_wp.get('word_timestamps', True))
    ttk.Checkbutton(adv4, text="Word timestamps (scene-aware cues)",
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

    _preview_title_text = "Subtitle Preview"

    preview_lf = ttk.LabelFrame(right, text=_preview_title_text)
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

    _gpu_badge_var = tk.StringVar(value="detecting...")
    ttk.Label(status_frame, textvariable=_gpu_badge_var, anchor='e').grid(
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
        preview_lf.config(text=f"Preview -- {name}")

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
        preview_lf.config(text="Subtitle Preview")
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

    # ── pip install helper ──
    def _prompt_install_backend(backend):
        """Ask the user if they want to install a missing backend.
        Returns True if installed successfully, False otherwise."""
        if backend == 'whisperx':
            display_name = 'WhisperX'
            # ⚠️ DO NOT PIN transformers BACK HERE. This used to read
            # ['whisperx', 'transformers<4.45'] to dodge whisperx 3.3.x importing
            # transformers.utils.is_offline_mode (moved in transformers 4.45).
            # That pin became a trap: modern whisperx REQUIRES transformers>=4.45,
            # so pip satisfied the constraint by backtracking to whisperx 3.3.1 —
            # which hard-pins faster-whisper==1.1.0 and ctranslate2<4.5.0. And
            # ctranslate2 4.4 links cuDNN 8 while this box has cuDNN 9, so the ASR
            # engine could not load at all.
            #
            # It reached well past the Suite: merlin-voice.service runs
            # /usr/bin/python3 with NO venv, so it shares this same ~/.local
            # site-packages and uses whisperx for speech-to-text. Clicking "yes"
            # on this dialog on 2026-08-18 took Merlin's ears out.
            #
            # ctranslate2>=4.5 is explicit on purpose — it keeps pip from ever
            # backtracking into a cuDNN 8 build again, whatever whisperx asks for.
            pip_args = ['whisperx', 'ctranslate2>=4.5']
        else:
            display_name = 'faster-whisper'
            pip_args = ['faster-whisper']

        # ⚠️ Both backends live in the ISOLATED ENGINE now, so there is one install
        # and one dialog. The old prompt said only "(This may take several minutes)"
        # and then pip-installed into the user's system Python — the exact behaviour
        # that took an unrelated voice assistant's STT offline on 2026-08-18.
        # ensure_engine_ui states the size for THIS machine, where it goes, what is
        # NOT touched, and how to remove it, then lets the user decide.
        from .whisper_engine import ensure_engine_ui
        return ensure_engine_ui(win)

    # ── dep check ──
    # ── dep check ──
    def _check_deps_on_start():
        # ⚠️ ONE engine now, so there is no "which backend to install" choice — both
        # faster-whisper and whisperx live in the same isolated venv. The old dialog
        # asked the user to pick, then pip-installed the winner into their system
        # Python. ensure_engine_ui shows the disclosure and builds the engine.
        from .whisper_engine import is_installed as _engine_installed
        if not _engine_installed():
            if not _prompt_install_backend("engine"):
                _log_write("The Whisper engine was not installed.", "warning")
                _status_var.set("Engine not installed")
                return
            _log_write("Whisper engine installed successfully.", "success")
            _refresh_backend_hints()


        # Check for ffmpeg
        if not shutil.which("ffmpeg"):
            _log_write("ffmpeg not found — install from https://ffmpeg.org/download.html",
                       "error")
            _status_var.set("Missing ffmpeg -- see log")
            return

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
            "audio_tracks": _audio_var.get(),
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
            "device_index": _gpu_choices.get(_gpu_var.get(), 0),
            "reading_speed": _cps_var.get(),
            "split_gap": _gap_var.get(),
            "filters": _filter_panel.get_prefs(),
        }

    def _apply_settings(settings: dict):
        if not settings:
            return
        for key, var in [
            ("backend", _backend_var),
            ("model", _model_var),
            ("language", _lang_var),
            ("task", _task_var),
            ("audio_tracks", _audio_var),
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
        if "device_index" in settings:
            for _l, _v in _gpu_choices.items():
                if _v == int(settings["device_index"]):
                    _gpu_var.set(_l); break
        if "reading_speed" in settings:
            _cps_var.set(float(settings["reading_speed"]))
        if "split_gap" in settings:
            _gap_var.set(float(settings["split_gap"]))

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

    _track_cache = {}

    def _audio_streams_cached(path):
        """ffprobe each file at most once — the hint re-runs on every mode change
        and on every add, and probing a 40-file queue repeatedly is felt."""
        key = str(path)
        if key not in _track_cache:
            _track_cache[key] = get_audio_streams(key)
        return _track_cache[key]

    def _sub_lang_for(stream):
        """Language code for the SUBTITLE this track will produce.

        ⚠️ Not simply the audio track's language. Translating to English makes
        an English subtitle out of a Japanese track, so the task wins; only
        then does the track's own tag apply, and only then the language picked
        for transcription.
        """
        if TASKS.get(_task_var.get()) == "translate":
            return "eng"
        lang = (stream or {}).get("language")
        if lang and lang != "und":
            return lang
        return LANGUAGES.get(_lang_var.get()) or "eng"

    def _build_jobs(_mode=None):
        """Expand the ticked tracks into (file, audio track) jobs.

        Reads the TREE, not the dropdown — the dropdown only ever seeds the
        ticks, so a hand edit survives. Returns (jobs, notes); one job per
        transcript that will be produced, so a video with two ticked commentary
        tracks yields two. `row` points back at its file row.

        ⚠️ The suffix is only added when a file yields MORE THAN ONE job. A
        single-track file keeps its plain `name.srt`, so turning this feature on
        does not silently rename everything that already worked.
        """
        jobs, notes = [], []
        for row_idx, path in enumerate(_file_paths):
            if path.suffix.lower() in AUDIO_EXTENSIONS:
                # Already audio -- there is no track to choose.
                jobs.append({"path": path, "track": None, "row": row_idx,
                             "ord": 0, "tag": "", "short": "audio",
                             "label": path.name})
                continue

            streams = _audio_streams_cached(path)
            if not streams:
                notes.append(f"{path.name}: no audio tracks found -- using default")
                jobs.append({"path": path, "track": None, "row": row_idx,
                             "ord": 0, "tag": "", "short": "default",
                             "label": path.name})
                continue

            wanted = _wants(path)
            chosen = [s for s in streams if s["ord"] in wanted]
            if not chosen:
                notes.append(f"{path.name}: no track ticked -- skipped")
                continue

            for s in chosen:
                # ⚠️ A COMMENTARY TRANSCRIPT IS NAMED FOR WHAT IT IS, even when
                # it is the only one. `<stem>.commentary.eng.srt` is what the
                # Media Processor's _detect_ext_subs() reads to flag the track
                # `comment` and keep it out of the ordinary subtitle rotation.
                # Left as a plain `.srt`, a commentary transcript is muxed back
                # as a NORMAL English subtitle — and with SubtitleMode 0 that
                # can auto-select over the real subs, so you start the episode
                # reading two people discussing it.
                #
                # Ordinary tracks keep the plain `<stem>.srt` they have always
                # had; that is the name players and Jellyfin expect, and
                # changing it would break every existing workflow to solve a
                # problem only commentary has.
                role = s["role"]
                if role in ("commentary", "descriptive"):
                    same = [x for x in chosen if x["role"] == role]
                    n = f".{same.index(s) + 1}" if len(same) > 1 else ""
                    tag = f".{role}.{_sub_lang_for(s)}{n}"
                elif len(chosen) == 1:
                    tag = ""
                else:
                    tag = f".track{s['ord'] + 1}"
                short = f"A{s['ord'] + 1}"
                if s["role"] != "main":
                    short += f" {s['role'].capitalize()}"
                jobs.append({"path": path, "track": s["index"], "row": row_idx,
                             "ord": s["ord"], "tag": tag, "short": short,
                             "label": f"{path.name}  [{describe_audio_stream(s)}]"})
        return jobs, notes

    def _refresh_audio_hint(_event=None):
        """Summarise the current ticks under the dropdown."""
        if not _file_paths:
            _audio_hint_var.set("tick tracks per file below")
            return
        try:
            jobs, notes = _build_jobs()
        except Exception:
            _audio_hint_var.set("")
            return
        skipped = sum(1 for n in notes if "skipped" in n)
        msg = (f"{len(jobs)} transcript{'s' if len(jobs) != 1 else ''} "
               f"from {len(_file_paths)} file(s)")
        if skipped:
            msg += f"  --  {skipped} with nothing ticked"
        _audio_hint_var.set(msg)

    def _on_audio_mode_change(_event=None):
        """The dropdown is a BULK SEED, not the source of truth.

        Choosing a mode re-ticks every file, discarding hand edits — same
        contract as sub_ripper, where changing the language re-seeds the
        per-file type boxes. The tree is what actually gets transcribed.
        """
        if _processing[0]:
            return
        _reseed_wants()
        _rebuild_tree()

    audio_cb.bind("<<ComboboxSelected>>", _on_audio_mode_change)
    _refresh_audio_hint()

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
        if not _is_backend_cached(backend):
            other = "whisperx" if backend == "faster-whisper" else "faster-whisper"
            if _is_backend_cached(other):
                messagebox.showwarning(
                    "Backend Not Found",
                    f"'{backend}' is not installed.\n\n"
                    f"Switching to '{other}'.",
                    parent=win)
                _backend_var.set(other)
                backend = other
            else:
                # Neither available — offer to install selected backend
                if _prompt_install_backend(backend):
                    _log_write(f"{backend} installed successfully.", "success")
                    _refresh_backend_hints()
                else:
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

        _reset_tags()

        # Expand ticked tracks into jobs BEFORE anything else uses a count --
        # from here on "n" means transcripts to produce, not files queued.
        audio_mode = _audio_var.get()
        jobs, notes = _build_jobs()
        for note in notes:
            _log_write(note, "warning")
        if not jobs:
            messagebox.showwarning(
                "Nothing selected",
                "No audio track is ticked. Open a file in the list and tick "
                "the track(s) you want transcribed, or pick a mode from the "
                "Audio dropdown to tick them in bulk.",
                parent=win)
            _processing[0] = False
            start_btn.config(state="normal")
            cancel_btn.config(state="disabled")
            _set_file_buttons_state("normal")
            return
        _jobs.clear()
        _jobs.extend(jobs)

        n = len(_jobs)
        _status_var.set(f"Processing 0 / {n}...")

        lang_code = LANGUAGES[_lang_var.get()]
        task_code = TASKS[_task_var.get()]
        device = _device_var.get()
        if not device or device == "auto":
            # Use cached result from background check if available
            device = _backend_cache.get("_device") or detect_device()
            _device_var.set(device)

        output_dir = _get_output_dir()
        fmt_list = _get_fmt_list()

        backend = _backend_var.get()
        task_label = "translate->en" if task_code == "translate" else "transcribe"
        _log_write(
            f"Starting batch: {n} transcript(s) from {len(_file_paths)} file(s)  "
            f"[backend={backend} model={_model_var.get()} lang={lang_code or 'auto'} "
            f"task={task_label} device={device} audio={audio_mode}]",
            "info",
        )
        if audio_mode != AUDIO_TRACK_DEFAULT:
            for j in _jobs:
                _log_write(f"   {j['label']}", "info")

        _worker[0] = BatchTranscribeWorker(
            q=_queue,
            paths=[j["path"] for j in _jobs],
            tracks=[j["track"] for j in _jobs],
            tags=[j["tag"] for j in _jobs],
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
            # ⚠️ The construction site. Adding a setting to the UI and to
            # prefs does NOTHING unless it is passed here — the exact way
            # the bit-depth setting shipped broken on 2026-08-08.
            device_index=_gpu_choices.get(_gpu_var.get(), 0),
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
            reading_speed=_cps_var.get(),
            split_gap=_gap_var.get(),
        )
        _results[idx] = segments

        output = _get_output_dir()
        fmt_parts = _get_fmt_list()
        fmt = ",".join(fmt_parts)
        vtt_style = _vtt_style_var.get().strip() or None

        # ⚠️ _jobs[idx], not _file_paths[idx] -- with track selection the two
        # lists are different lengths and idx counts jobs.
        job = _jobs[idx] if idx < len(_jobs) else {"path": _file_paths[idx], "tag": ""}
        path, tag = job["path"], job.get("tag", "")
        try:
            write_output(segments, path, output, fmt, vtt_style=vtt_style,
                         suffix=tag)
            base = Path(output) if output else path.parent
            for ext in fmt_parts:
                saved = str(base / (path.stem + tag + f".{ext}"))
                _log_write(f"Saved -> {saved}", "success")
                # ⚠️ SRT only — the filters parse and rewrite SubRip. Running
                # them on a .vtt would strip its header and corrupt the file.
                if ext != "srt":
                    continue
                try:
                    counts = _filter_panel.apply_to_file(saved)
                except Exception as exc:
                    # ⚠️ Say so. sub_ripper's copy swallows this and returns
                    # None, which reads exactly like "no filters selected" —
                    # the file is left unfiltered and nobody finds out.
                    _log_write(f"  Filters FAILED on {Path(saved).name}: {exc}"
                               "  (file left unfiltered)", "error")
                else:
                    if counts:
                        before, after = counts
                        _log_write(f"  Filters applied: {before} -> {after} cues",
                                   "info")
        except Exception as exc:
            _log_write(f"Save failed for {path.name}: {exc}", "error")

    # ── batch completion ──

    def _on_batch_done():
        progress_var.set(100)
        done = len(_results)
        total = len(_jobs) or len(_file_paths)
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
                    _tag_job(idx, 'active')
                    if 0 <= idx < len(_jobs):
                        row, o = _jobs[idx]["row"], _jobs[idx].get("ord")
                        iid = f"f{row}t{o}" if o is not None else f"f{row}"
                        if file_tree.exists(iid):
                            file_tree.see(iid)
                    progress_var.set(0)
                    label = (_jobs[idx].get("label") if idx < len(_jobs)
                             else path.name) or path.name
                    _status_var.set(f"Processing {idx+1} / {total}  --  {label}")

                elif event == "skip_file":
                    idx, path, reason = data
                    _tag_job(idx, 'skip')

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
                    _tag_job(idx, 'done')
                    _save_one(idx)

                elif event == "file_error":
                    idx, path, exc = data
                    _tag_job(idx, 'error')
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
    # Backend checks run in a background thread so the window appears
    # instantly instead of blocking for ~5s on import faster_whisper.
    # _check_deps_on_start runs on the main thread after the check completes.
    _refresh_backend_hints_async(on_complete=_check_deps_on_start)
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
