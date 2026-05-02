"""
Docflix Media Suite — Waveform Timeline Widget

Audio waveform display with subtitle cue overlay for visual timing
adjustment.  Extracts audio from video via ffmpeg, renders the waveform
on a Tkinter Canvas, and overlays subtitle cues as draggable blocks.

Usage:
    timeline = WaveformTimeline(parent, cues_fn, on_cue_modified, ...)
    timeline.load_audio('/path/to/video.mkv')
"""

import json as _json
import os
import socket as _socket
import struct
import subprocess
import tempfile
import threading
import tkinter as tk
from tkinter import ttk

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    np = None
    HAS_NUMPY = False


# ── Constants ────────────────────────────────────────────────────────────────

SAMPLE_RATE = 8000          # 8 kHz mono — sufficient for visual display
BYTES_PER_SAMPLE = 2        # 16-bit PCM
WAVEFORM_COLOR = '#4a90d9'  # blue waveform
WAVEFORM_BG = '#1a1a2e'     # dark background
RULER_BG = '#2a2a3e'        # time ruler background
RULER_FG = '#aaaacc'        # time ruler text/ticks
CUE_FILL = '#d4a843'        # cue block fill (gold)
CUE_FILL_SEL = '#e8c84a'    # selected cue fill
CUE_BORDER = '#ffffff'      # cue block border
CUE_BORDER_SEL = '#ff4444'  # selected cue border
CUE_TEXT_COLOR = '#1a1a1a'  # cue text
CURSOR_COLOR = '#ff3333'    # playback cursor
MARKER_A_COLOR = '#33cc33'  # marker A (green)
MARKER_B_COLOR = '#cccc33'  # marker B (yellow)
MIN_ZOOM_PX_PER_SEC = 2     # zoomed all the way out
MAX_ZOOM_PX_PER_SEC = 500   # zoomed all the way in (0.5s per 250px)
RULER_HEIGHT = 24           # pixels for the time ruler
EDGE_GRAB_PX = 6            # pixels from edge to trigger resize drag
CURSOR_POLL_MS = 80         # playback cursor poll interval


def _format_time(ms):
    """Format milliseconds as M:SS or H:MM:SS."""
    total_sec = int(ms / 1000)
    h = total_sec // 3600
    m = (total_sec % 3600) // 60
    s = total_sec % 60
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


# ═════════════════════════════════════════════════════════════════════════════
# WaveformTimeline Widget
# ═════════════════════════════════════════════════════════════════════════════

class WaveformTimeline(tk.Frame):
    """Audio waveform display with subtitle cue overlay.

    Parameters
    ----------
    parent : tk widget
        Parent container.
    cues_fn : callable
        Returns the current list of cue dicts
        [{'index', 'start', 'end', 'text'}, ...].
    on_cue_modified : callable(cue_index, new_start_ms, new_end_ms)
        Called when a cue is dragged to a new position.
    on_selection_changed : callable(cue_index)
        Called when a cue is clicked on the timeline.
    push_undo : callable or None
        Called before a drag operation to save undo state.
    log_fn : callable(msg, level) or None
        Logging callback.
    """

    def __init__(self, parent, cues_fn=None, on_cue_modified=None,
                 on_selection_changed=None, push_undo=None, log_fn=None,
                 video_frame=None, **kwargs):
        super().__init__(parent, **kwargs)

        self._cues_fn = cues_fn or (lambda: [])
        self._on_cue_modified = on_cue_modified
        self._on_selection_changed = on_selection_changed
        self._push_undo = push_undo
        self._log = log_fn or (lambda msg, level='INFO': None)
        self._video_frame = video_frame  # Tk frame for embedded mpv video

        # ── Waveform data ──
        self._raw_samples = None      # numpy int16 array (full resolution)
        self._duration_ms = 0         # total audio duration in ms
        self._peaks = None            # downsampled (min, max) pairs for display
        self._temp_wav = None         # path to temp WAV file

        # ── View state ──
        self._view_start_ms = 0       # left edge of visible window
        self._view_end_ms = 0         # right edge of visible window
        self._selected_cue_idx = None
        self._playback_pos_ms = None

        # ── Drag state ──
        self._drag = None  # dict or None

        # ── Marker state ──
        self._marker_a_ms = None  # marker A position (ms)
        self._marker_b_ms = None  # marker B position (ms)

        # ── Playback state ──
        self._video_path = None   # path to source video for playback
        self._mpv_proc = None     # mpv subprocess
        self._mpv_socket = None   # path to mpv IPC socket
        self._playing = False
        self._cursor_poll_id = None
        self._temp_srt = None     # temp SRT file for live subtitle preview

        # ── Loading state ──
        self._loading = False
        self._load_cancelled = False

        # ── Build UI ──
        self._build_ui()

    # ─────────────────────────────────────────────────────────────────────
    # UI Construction
    # ─────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        """Build the toolbar, canvas, and scrollbar."""
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        # ── Toolbar ──
        toolbar = ttk.Frame(self)
        toolbar.grid(row=0, column=0, sticky='ew', padx=2, pady=(2, 0))

        # Step navigation
        ttk.Button(toolbar, text="◀◀ 1s", width=6,
                   command=lambda: self._step_view(-1000)).pack(side='left', padx=1)
        ttk.Button(toolbar, text="◀ 100ms", width=8,
                   command=lambda: self._step_view(-100)).pack(side='left', padx=1)
        ttk.Button(toolbar, text="◀ Frame", width=7,
                   command=lambda: self._step_frame(-1)).pack(side='left', padx=1)
        ttk.Button(toolbar, text="Frame ▶", width=7,
                   command=lambda: self._step_frame(1)).pack(side='left', padx=1)
        ttk.Button(toolbar, text="100ms ▶", width=8,
                   command=lambda: self._step_view(100)).pack(side='left', padx=1)
        ttk.Button(toolbar, text="1s ▶▶", width=6,
                   command=lambda: self._step_view(1000)).pack(side='left', padx=1)

        ttk.Separator(toolbar, orient='vertical').pack(
            side='left', fill='y', padx=6, pady=2)

        # Playback buttons
        self._play_btn = ttk.Button(toolbar, text="▶ Play", width=7,
                                     command=self._toggle_playback)
        self._play_btn.pack(side='left', padx=1)
        ttk.Button(toolbar, text="■ Stop", width=7,
                   command=self._stop_playback).pack(side='left', padx=1)


        self._time_label = ttk.Label(toolbar, text="", font=('Helvetica', 9))
        self._time_label.pack(side='right', padx=6)

        self._status_label = ttk.Label(toolbar, text="No audio loaded",
                                        font=('Helvetica', 9))
        self._status_label.pack(side='left', padx=10)

        # ── Canvas ──
        self._canvas = tk.Canvas(self, bg=WAVEFORM_BG, highlightthickness=0)
        self._canvas.grid(row=1, column=0, sticky='nsew')

        # ── Horizontal scrollbar ──
        self._hscroll = ttk.Scrollbar(self, orient='horizontal',
                                       command=self._on_scroll)
        self._hscroll.grid(row=2, column=0, sticky='ew')

        # ── Canvas event bindings ──
        self._canvas.bind('<Configure>', self._on_canvas_resize)
        self._canvas.bind('<MouseWheel>', self._on_mousewheel)       # Windows/macOS
        self._canvas.bind('<Button-4>', self._on_mousewheel_linux)    # Linux scroll up
        self._canvas.bind('<Button-5>', self._on_mousewheel_linux)    # Linux scroll down
        self._canvas.bind('<ButtonPress-1>', self._on_mouse_down)
        self._canvas.bind('<B1-Motion>', self._on_mouse_move)
        self._canvas.bind('<ButtonRelease-1>', self._on_mouse_up)
        self._canvas.bind('<Motion>', self._on_hover)

        # Right-click context menu
        self._ctx_menu = tk.Menu(self._canvas, tearoff=0)
        self._ctx_menu.add_command(label="Add Cue Here",
                                    command=self._add_cue_at_cursor)
        self._canvas.bind('<ButtonPress-3>', self._on_right_click)

        # Key bindings (canvas must have focus)
        self._canvas.bind('<space>', lambda e: self._toggle_playback())
        self._canvas.bind('<Left>', lambda e: self._step_view(-100))
        self._canvas.bind('<Right>', lambda e: self._step_view(100))
        self._canvas.bind('<Shift-Left>', lambda e: self._step_view(-1000))
        self._canvas.bind('<Shift-Right>', lambda e: self._step_view(1000))
        self._canvas.bind('<FocusIn>', lambda e: None)
        # Make canvas focusable
        self._canvas.configure(takefocus=True)

        # Debounce timer for resize redraws
        self._resize_after_id = None

    # ─────────────────────────────────────────────────────────────────────
    # Audio Loading
    # ─────────────────────────────────────────────────────────────────────

    def load_audio(self, video_path, done_callback=None):
        """Extract audio from video and load waveform data.

        Runs ffmpeg in a background thread.  Calls done_callback(success)
        on the main thread when finished.
        """
        if self._loading:
            return
        if not video_path or not os.path.isfile(video_path):
            self._log("Waveform: no video file", 'WARNING')
            return

        self._video_path = video_path
        self._loading = True
        self._load_cancelled = False
        self._status_label.configure(text="Extracting audio...")

        def _worker():
            try:
                # Create temp WAV file
                fd, wav_path = tempfile.mkstemp(suffix='.wav',
                                                 prefix='docflix_waveform_')
                os.close(fd)
                self._temp_wav = wav_path

                # Extract audio: 8kHz, mono, 16-bit PCM
                cmd = [
                    'ffmpeg', '-y', '-i', video_path,
                    '-vn', '-acodec', 'pcm_s16le',
                    '-ar', str(SAMPLE_RATE), '-ac', '1',
                    wav_path,
                ]
                proc = subprocess.run(
                    cmd, capture_output=True, timeout=300,
                    stdin=subprocess.DEVNULL,
                )

                if self._load_cancelled:
                    self._cleanup_temp()
                    return

                if proc.returncode != 0:
                    err = proc.stderr.decode('utf-8', errors='replace')[-200:]
                    self.after(0, lambda: self._log(
                        f"Waveform: ffmpeg error: {err}", 'ERROR'))
                    self.after(0, lambda: self._on_load_done(False,
                                                              done_callback))
                    return

                # Load WAV data (skip 44-byte header)
                file_size = os.path.getsize(wav_path)
                data_size = file_size - 44
                if data_size <= 0:
                    self.after(0, lambda: self._log(
                        "Waveform: empty audio", 'WARNING'))
                    self.after(0, lambda: self._on_load_done(False,
                                                              done_callback))
                    return

                if HAS_NUMPY:
                    samples = np.fromfile(wav_path, dtype=np.int16, offset=44)
                else:
                    # Pure Python fallback (slower but functional)
                    with open(wav_path, 'rb') as f:
                        f.seek(44)
                        raw = f.read()
                    n = len(raw) // BYTES_PER_SAMPLE
                    samples = list(struct.unpack(f'<{n}h', raw[:n * 2]))

                num_samples = len(samples) if not HAS_NUMPY else samples.shape[0]
                duration_ms = (num_samples / SAMPLE_RATE) * 1000

                # Schedule UI update on main thread
                self.after(0, lambda: self._on_audio_loaded(
                    samples, duration_ms, done_callback))

            except subprocess.TimeoutExpired:
                self.after(0, lambda: self._log(
                    "Waveform: ffmpeg timed out", 'ERROR'))
                self.after(0, lambda: self._on_load_done(False,
                                                          done_callback))
            except Exception as e:
                self.after(0, lambda: self._log(
                    f"Waveform: load error: {e}", 'ERROR'))
                self.after(0, lambda: self._on_load_done(False,
                                                          done_callback))

        thread = threading.Thread(target=_worker, daemon=True)
        thread.start()

    def _on_audio_loaded(self, samples, duration_ms, done_callback):
        """Called on main thread after audio extraction completes."""
        self._raw_samples = samples
        self._duration_ms = duration_ms
        # Start zoomed in — show first 3 seconds
        self._view_start_ms = 0
        self._view_end_ms = min(3000, duration_ms)

        dur_str = _format_time(duration_ms)
        n_samples = len(samples) if not HAS_NUMPY else samples.shape[0]
        size_mb = (n_samples * BYTES_PER_SAMPLE) / (1024 * 1024)
        self._status_label.configure(
            text=f"Audio loaded — {dur_str} ({size_mb:.1f} MB)")
        self._log(f"Waveform: loaded {dur_str}, {n_samples:,} samples",
                  'INFO')

        self._full_redraw()
        self._on_load_done(True, done_callback)

    def _on_load_done(self, success, done_callback):
        """Finalize loading state."""
        self._loading = False
        if not success:
            self._status_label.configure(text="Audio load failed")
        if done_callback:
            done_callback(success)

    def cancel_load(self):
        """Cancel an in-progress load."""
        self._load_cancelled = True

    def _cleanup_temp(self):
        """Remove temporary WAV file."""
        if self._temp_wav and os.path.exists(self._temp_wav):
            try:
                os.remove(self._temp_wav)
            except OSError:
                pass
            self._temp_wav = None

    def cleanup(self):
        """Clean up resources.  Call when the editor window closes."""
        self._stop_mpv()
        self.cancel_load()
        self._cleanup_temp()

    # ─────────────────────────────────────────────────────────────────────
    # Downsampling
    # ─────────────────────────────────────────────────────────────────────

    def _downsample(self, canvas_width):
        """Downsample raw audio to (min, max) peak pairs for display.

        Returns a numpy array of shape (N, 2) where N <= canvas_width,
        or a list of (min, max) tuples if numpy is not available.
        """
        if self._raw_samples is None:
            return None

        # Samples in the visible window
        total_samples = (len(self._raw_samples) if not HAS_NUMPY
                         else self._raw_samples.shape[0])
        start_sample = int((self._view_start_ms / self._duration_ms)
                           * total_samples)
        end_sample = int((self._view_end_ms / self._duration_ms)
                         * total_samples)
        start_sample = max(0, start_sample)
        end_sample = min(total_samples, end_sample)

        if end_sample <= start_sample:
            return None

        if HAS_NUMPY:
            window = self._raw_samples[start_sample:end_sample]
            n = len(window)
            cols = min(canvas_width, n)
            if cols <= 0:
                return None

            # Reshape into chunks and take min/max per chunk
            chunk_size = n // cols
            if chunk_size < 1:
                chunk_size = 1
                cols = n
            trimmed = window[:cols * chunk_size].reshape(cols, chunk_size)
            peaks = np.column_stack([trimmed.min(axis=1),
                                      trimmed.max(axis=1)])
            return peaks
        else:
            # Pure Python fallback
            window = self._raw_samples[start_sample:end_sample]
            n = len(window)
            cols = min(canvas_width, n)
            if cols <= 0:
                return None
            chunk_size = max(1, n // cols)
            peaks = []
            for i in range(cols):
                chunk = window[i * chunk_size:(i + 1) * chunk_size]
                if chunk:
                    peaks.append((min(chunk), max(chunk)))
                else:
                    peaks.append((0, 0))
            return peaks

    # ─────────────────────────────────────────────────────────────────────
    # Rendering
    # ─────────────────────────────────────────────────────────────────────

    def _full_redraw(self):
        """Redraw everything: ruler, waveform, cue blocks, cursor."""
        self._canvas.delete('all')
        cw = self._canvas.winfo_width()
        ch = self._canvas.winfo_height()
        if cw < 10 or ch < 10:
            return

        self._draw_ruler(cw)
        self._draw_waveform(cw, ch)
        self._draw_cue_blocks(cw, ch)
        self._draw_markers(cw, ch)
        self._draw_playback_cursor(cw, ch)
        self._update_scrollbar()
        self._update_time_label()

    def _draw_ruler(self, cw):
        """Draw the time ruler along the top of the canvas."""
        # Ruler background
        self._canvas.create_rectangle(
            0, 0, cw, RULER_HEIGHT,
            fill=RULER_BG, outline='', tags='ruler')

        if self._duration_ms <= 0:
            return

        view_span_ms = self._view_end_ms - self._view_start_ms
        if view_span_ms <= 0:
            return

        # Choose tick interval based on zoom level
        px_per_sec = cw / (view_span_ms / 1000)
        if px_per_sec > 200:
            tick_ms = 1000        # 1s
        elif px_per_sec > 80:
            tick_ms = 5000        # 5s
        elif px_per_sec > 30:
            tick_ms = 10000       # 10s
        elif px_per_sec > 10:
            tick_ms = 30000       # 30s
        elif px_per_sec > 4:
            tick_ms = 60000       # 1m
        else:
            tick_ms = 300000      # 5m

        # Draw ticks
        first_tick = (int(self._view_start_ms / tick_ms) + 1) * tick_ms
        t = first_tick
        while t < self._view_end_ms:
            x = self._ms_to_x(t, cw)
            self._canvas.create_line(
                x, RULER_HEIGHT - 8, x, RULER_HEIGHT,
                fill=RULER_FG, tags='ruler')
            self._canvas.create_text(
                x, RULER_HEIGHT - 12, text=_format_time(t),
                fill=RULER_FG, font=('Helvetica', 8), anchor='s',
                tags='ruler')
            t += tick_ms

    def _draw_waveform(self, cw, ch):
        """Draw the audio waveform as a single polyline."""
        if self._raw_samples is None:
            return

        wave_height = ch - RULER_HEIGHT - 4  # leave room for ruler + padding
        if wave_height < 10:
            return

        peaks = self._downsample(cw)
        if peaks is None:
            return

        mid_y = RULER_HEIGHT + wave_height // 2
        if HAS_NUMPY:
            n = peaks.shape[0]
        else:
            n = len(peaks)

        if n == 0:
            return

        # Find the max amplitude for normalization
        if HAS_NUMPY:
            max_amp = max(abs(peaks[:, 0].min()), abs(peaks[:, 1].max()), 1)
        else:
            max_amp = max(
                max(abs(p[0]) for p in peaks),
                max(abs(p[1]) for p in peaks),
                1
            )

        scale = (wave_height / 2 - 2) / max_amp

        # Build a single polyline: go across top (max), then back across
        # bottom (min) to form a filled polygon
        top_coords = []
        bottom_coords = []

        for i in range(n):
            x = i * cw / n
            if HAS_NUMPY:
                y_min = mid_y - int(peaks[i, 1] * scale)
                y_max = mid_y - int(peaks[i, 0] * scale)
            else:
                y_min = mid_y - int(peaks[i][1] * scale)
                y_max = mid_y - int(peaks[i][0] * scale)
            top_coords.extend([x, y_min])
            bottom_coords.extend([x, y_max])

        # Reverse bottom coords to close the polygon
        bottom_coords_rev = []
        for i in range(n - 1, -1, -1):
            bottom_coords_rev.extend([bottom_coords[i * 2],
                                       bottom_coords[i * 2 + 1]])

        all_coords = top_coords + bottom_coords_rev

        if len(all_coords) >= 6:  # need at least 3 points for polygon
            self._canvas.create_polygon(
                *all_coords, fill=WAVEFORM_COLOR, outline='',
                tags='waveform')

        # Center line
        self._canvas.create_line(
            0, mid_y, cw, mid_y,
            fill='#3a6090', dash=(2, 4), tags='waveform')

    def _draw_cue_blocks(self, cw, ch):
        """Draw subtitle cue blocks overlaid on the waveform."""
        self._canvas.delete('cue')
        cues = self._cues_fn()
        if not cues or self._duration_ms <= 0:
            return

        from .subtitle_filters import srt_ts_to_ms

        wave_top = RULER_HEIGHT + 4
        wave_bottom = ch - 4
        block_top = wave_bottom - 28
        block_bottom = wave_bottom - 4

        for idx, cue in enumerate(cues):
            try:
                start_ms = srt_ts_to_ms(cue['start'])
                end_ms = srt_ts_to_ms(cue['end'])
            except (KeyError, ValueError):
                continue

            # Skip cues outside visible range
            if end_ms < self._view_start_ms or start_ms > self._view_end_ms:
                continue

            x1 = max(0, self._ms_to_x(start_ms, cw))
            x2 = min(cw, self._ms_to_x(end_ms, cw))

            if x2 - x1 < 2:
                x2 = x1 + 2  # minimum visible width

            is_selected = (idx == self._selected_cue_idx)
            fill = CUE_FILL_SEL if is_selected else CUE_FILL
            border = CUE_BORDER_SEL if is_selected else CUE_BORDER
            border_w = 2 if is_selected else 1

            self._canvas.create_rectangle(
                x1, block_top, x2, block_bottom,
                fill=fill, outline=border, width=border_w,
                tags=('cue', f'cue_{idx}'))

            # Cue text label (truncated)
            if x2 - x1 > 30:
                text = cue.get('text', '').replace('\n', ' ')
                label = f"{idx + 1}: {text}"
                # Rough truncation
                max_chars = int((x2 - x1) / 7)
                if len(label) > max_chars:
                    label = label[:max_chars - 1] + '…'
                self._canvas.create_text(
                    (x1 + x2) / 2, (block_top + block_bottom) / 2,
                    text=label, fill=CUE_TEXT_COLOR,
                    font=('Helvetica', 8), anchor='center',
                    tags=('cue', f'cue_{idx}'))

    def _draw_playback_cursor(self, cw, ch):
        """Draw the playback position cursor."""
        self._canvas.delete('cursor')
        if self._playback_pos_ms is None or self._duration_ms <= 0:
            return

        if (self._playback_pos_ms < self._view_start_ms or
                self._playback_pos_ms > self._view_end_ms):
            return

        x = self._ms_to_x(self._playback_pos_ms, cw)
        self._canvas.create_line(
            x, RULER_HEIGHT, x, ch,
            fill=CURSOR_COLOR, width=2, tags='cursor')

    def _draw_markers(self, cw, ch):
        """Draw marker A and marker B on the waveform."""
        self._canvas.delete('marker')
        if self._duration_ms <= 0:
            return

        for ms, color, label in [
            (self._marker_a_ms, MARKER_A_COLOR, 'A'),
            (self._marker_b_ms, MARKER_B_COLOR, 'B'),
        ]:
            if ms is None:
                continue
            if ms < self._view_start_ms or ms > self._view_end_ms:
                continue
            x = self._ms_to_x(ms, cw)
            self._canvas.create_line(
                x, RULER_HEIGHT, x, ch,
                fill=color, width=2, dash=(4, 2), tags='marker')
            self._canvas.create_text(
                x, RULER_HEIGHT + 2, text=f"{label} {_format_time(ms)}",
                fill=color, font=('Helvetica', 8, 'bold'),
                anchor='nw', tags='marker')

        # Highlight region between A and B
        if (self._marker_a_ms is not None and
                self._marker_b_ms is not None):
            a = min(self._marker_a_ms, self._marker_b_ms)
            b = max(self._marker_a_ms, self._marker_b_ms)
            if b > self._view_start_ms and a < self._view_end_ms:
                x1 = max(0, self._ms_to_x(a, cw))
                x2 = min(cw, self._ms_to_x(b, cw))
                self._canvas.create_rectangle(
                    x1, RULER_HEIGHT, x2, ch,
                    fill='', outline=MARKER_A_COLOR,
                    dash=(2, 2), width=1,
                    stipple='gray12', tags='marker')

    # ─────────────────────────────────────────────────────────────────────
    # Coordinate conversion
    # ─────────────────────────────────────────────────────────────────────

    def _ms_to_x(self, ms, canvas_width):
        """Convert milliseconds to canvas x coordinate."""
        view_span = self._view_end_ms - self._view_start_ms
        if view_span <= 0:
            return 0
        return ((ms - self._view_start_ms) / view_span) * canvas_width

    def _x_to_ms(self, x, canvas_width):
        """Convert canvas x coordinate to milliseconds."""
        view_span = self._view_end_ms - self._view_start_ms
        if canvas_width <= 0:
            return self._view_start_ms
        return self._view_start_ms + (x / canvas_width) * view_span

    # ─────────────────────────────────────────────────────────────────────
    # Zoom and Scroll
    # ─────────────────────────────────────────────────────────────────────

    def zoom_in(self, center_ms=None):
        """Zoom in (show less time, more detail)."""
        self._zoom(0.5, center_ms)

    def zoom_out(self, center_ms=None):
        """Zoom out (show more time, less detail)."""
        self._zoom(2.0, center_ms)

    def zoom_to_fit(self):
        """Zoom to show the entire audio duration."""
        if self._duration_ms <= 0:
            return
        self._view_start_ms = 0
        self._view_end_ms = self._duration_ms
        self._full_redraw()

    def _zoom(self, factor, center_ms=None):
        """Zoom by a factor around a center point."""
        if self._duration_ms <= 0:
            return

        view_span = self._view_end_ms - self._view_start_ms
        if center_ms is None:
            center_ms = (self._view_start_ms + self._view_end_ms) / 2

        new_span = view_span * factor

        # Enforce min/max zoom
        cw = self._canvas.winfo_width()
        if cw > 0:
            min_span = (cw / MAX_ZOOM_PX_PER_SEC) * 1000
            max_span = self._duration_ms
            new_span = max(min_span, min(max_span, new_span))

        # Center the view on the zoom point
        ratio = ((center_ms - self._view_start_ms) / view_span
                 if view_span > 0 else 0.5)
        self._view_start_ms = center_ms - new_span * ratio
        self._view_end_ms = self._view_start_ms + new_span

        # Clamp to bounds
        if self._view_start_ms < 0:
            self._view_start_ms = 0
            self._view_end_ms = new_span
        if self._view_end_ms > self._duration_ms:
            self._view_end_ms = self._duration_ms
            self._view_start_ms = max(0, self._duration_ms - new_span)

        self._full_redraw()

    def _on_scroll(self, *args):
        """Handle horizontal scrollbar events."""
        if self._duration_ms <= 0:
            return
        action = args[0]
        if action == 'moveto':
            fraction = float(args[1])
            view_span = self._view_end_ms - self._view_start_ms
            self._view_start_ms = fraction * self._duration_ms
            self._view_end_ms = self._view_start_ms + view_span
            # Clamp
            if self._view_end_ms > self._duration_ms:
                self._view_end_ms = self._duration_ms
                self._view_start_ms = max(0, self._duration_ms - view_span)
            self._full_redraw()
        elif action == 'scroll':
            amount = int(args[1])
            units = args[2] if len(args) > 2 else 'units'
            view_span = self._view_end_ms - self._view_start_ms
            if units == 'pages':
                shift = view_span * 0.8 * amount
            else:
                shift = view_span * 0.1 * amount
            self._view_start_ms += shift
            self._view_end_ms += shift
            # Clamp
            if self._view_start_ms < 0:
                self._view_end_ms -= self._view_start_ms
                self._view_start_ms = 0
            if self._view_end_ms > self._duration_ms:
                self._view_start_ms -= (self._view_end_ms - self._duration_ms)
                self._view_end_ms = self._duration_ms
                self._view_start_ms = max(0, self._view_start_ms)
            self._full_redraw()

    def _update_scrollbar(self):
        """Update the scrollbar position to reflect the current view."""
        if self._duration_ms <= 0:
            self._hscroll.set(0, 1)
            return
        lo = self._view_start_ms / self._duration_ms
        hi = self._view_end_ms / self._duration_ms
        self._hscroll.set(lo, hi)

    def _update_time_label(self):
        """Update the time range label."""
        if self._duration_ms <= 0:
            self._time_label.configure(text="")
            return
        self._time_label.configure(
            text=f"{_format_time(self._view_start_ms)} — "
                 f"{_format_time(self._view_end_ms)}")

    # ─────────────────────────────────────────────────────────────────────
    # Mouse Events
    # ─────────────────────────────────────────────────────────────────────

    def _on_canvas_resize(self, event):
        """Debounced redraw on canvas resize."""
        if self._resize_after_id:
            self.after_cancel(self._resize_after_id)
        self._resize_after_id = self.after(100, self._full_redraw)

    def _on_mousewheel(self, event):
        """Handle mouse wheel (Windows/macOS)."""
        # Ctrl+scroll = 100ms steps, plain scroll = 1s steps
        if event.state & 0x4:  # Ctrl held
            step = -100 if event.delta > 0 else 100
        else:
            step = -1000 if event.delta > 0 else 1000
        self._step_view(step)

    def _on_mousewheel_linux(self, event):
        """Handle mouse wheel on Linux (Button-4/5)."""
        # Ctrl+scroll = 100ms steps, plain scroll = 1s steps
        if event.state & 0x4:  # Ctrl held
            step = -100 if event.num == 4 else 100
        else:
            step = -1000 if event.num == 4 else 1000
        self._step_view(step)

    def _scroll_by(self, shift_ms):
        """Scroll the view by shift_ms milliseconds."""
        self._view_start_ms += shift_ms
        self._view_end_ms += shift_ms
        # Clamp
        if self._view_start_ms < 0:
            self._view_end_ms -= self._view_start_ms
            self._view_start_ms = 0
        if self._view_end_ms > self._duration_ms:
            self._view_start_ms -= (self._view_end_ms - self._duration_ms)
            self._view_end_ms = self._duration_ms
            self._view_start_ms = max(0, self._view_start_ms)
        self._full_redraw()

    def _step_view(self, ms):
        """Step the view and playback position by ms milliseconds.
        Also seeks mpv if playing."""
        self._scroll_by(ms)
        # Move playback cursor too
        if self._playback_pos_ms is not None:
            self._playback_pos_ms = max(0, min(self._duration_ms,
                                                self._playback_pos_ms + ms))
        else:
            self._playback_pos_ms = max(0, self._view_start_ms)
        # Seek mpv if running
        if self._mpv_proc and self._mpv_proc.poll() is None:
            self._mpv_cmd(["seek", str(self._playback_pos_ms / 1000),
                           "absolute+exact"])

    def _step_frame(self, direction):
        """Step forward or backward by one frame using mpv.
        direction: 1 = forward, -1 = backward."""
        if not self._mpv_proc or self._mpv_proc.poll() is not None:
            # No mpv running — approximate with ~42ms (~24fps)
            self._step_view(42 * direction)
            return
        # Pause first if playing
        resp = self._mpv_cmd(["get_property", "pause"])
        if resp and resp.get('data') is False:
            self._mpv_cmd(["set_property", "pause", True])
            self._playing = False
            self._play_btn.configure(text="▶ Play")
        # Step frame
        if direction > 0:
            self._mpv_cmd(["frame-step"])
        else:
            self._mpv_cmd(["frame-back-step"])
        # Update cursor position from mpv after a brief delay
        self.after(100, self._sync_cursor_from_mpv)

    def _sync_cursor_from_mpv(self):
        """Read current position from mpv and update cursor/view."""
        if not self._mpv_proc or self._mpv_proc.poll() is not None:
            return
        resp = self._mpv_cmd(["get_property", "playback-time"])
        if resp and resp.get('data') is not None:
            self._playback_pos_ms = resp['data'] * 1000
            # Scroll view to keep cursor visible
            if (self._playback_pos_ms < self._view_start_ms or
                    self._playback_pos_ms > self._view_end_ms):
                view_span = self._view_end_ms - self._view_start_ms
                self._view_start_ms = max(0, self._playback_pos_ms - view_span / 2)
                self._view_end_ms = self._view_start_ms + view_span
                if self._view_end_ms > self._duration_ms:
                    self._view_end_ms = self._duration_ms
                    self._view_start_ms = max(0, self._duration_ms - view_span)
            self._full_redraw()

    def _on_mouse_down(self, event):
        """Handle mouse button press — start drag or select cue."""
        cw = self._canvas.winfo_width()
        ch = self._canvas.winfo_height()
        click_ms = self._x_to_ms(event.x, cw)

        # Hit-test cue blocks
        cues = self._cues_fn()
        if cues and self._duration_ms > 0:
            from .subtitle_filters import srt_ts_to_ms

            wave_bottom = ch - 4
            block_top = wave_bottom - 28
            block_bottom = wave_bottom - 4

            # Check if click is in the cue block area
            if block_top <= event.y <= block_bottom:
                for idx, cue in enumerate(cues):
                    try:
                        start_ms = srt_ts_to_ms(cue['start'])
                        end_ms = srt_ts_to_ms(cue['end'])
                    except (KeyError, ValueError):
                        continue

                    x1 = self._ms_to_x(start_ms, cw)
                    x2 = self._ms_to_x(end_ms, cw)

                    if x1 - 2 <= event.x <= x2 + 2:
                        # Determine drag type: edge resize or move
                        if abs(event.x - x1) <= EDGE_GRAB_PX:
                            drag_type = 'resize_start'
                        elif abs(event.x - x2) <= EDGE_GRAB_PX:
                            drag_type = 'resize_end'
                        else:
                            drag_type = 'move'

                        # Push undo state before drag
                        if self._push_undo:
                            self._push_undo()

                        self._drag = {
                            'type': drag_type,
                            'cue_idx': idx,
                            'start_ms': click_ms,
                            'orig_start_ms': start_ms,
                            'orig_end_ms': end_ms,
                        }

                        # Select the cue (but don't scroll — we're dragging)
                        self._selected_cue_idx = idx
                        if self._on_selection_changed:
                            self._on_selection_changed(idx)
                        # Redraw cue blocks only (not full redraw to avoid view changes)
                        self._draw_cue_blocks(cw, ch)
                        return

        # Click on empty area — set playback position and deselect
        self._playback_pos_ms = click_ms
        self._selected_cue_idx = None
        # Seek mpv if playing
        if self._mpv_proc and self._mpv_proc.poll() is None:
            self._mpv_cmd(["seek", str(click_ms / 1000), "absolute+exact"])
        self._full_redraw()

    def _on_mouse_move(self, event):
        """Handle mouse drag — move or resize cue."""
        if not self._drag:
            return

        cw = self._canvas.winfo_width()
        current_ms = self._x_to_ms(event.x, cw)
        delta_ms = current_ms - self._drag['start_ms']

        cues = self._cues_fn()
        idx = self._drag['cue_idx']
        if idx >= len(cues):
            self._drag = None
            return

        from .subtitle_filters import ms_to_srt_ts

        cue = cues[idx]
        orig_start = self._drag['orig_start_ms']
        orig_end = self._drag['orig_end_ms']

        if self._drag['type'] == 'move':
            new_start = max(0, orig_start + delta_ms)
            new_end = new_start + (orig_end - orig_start)
            cue['start'] = ms_to_srt_ts(int(new_start))
            cue['end'] = ms_to_srt_ts(int(new_end))
        elif self._drag['type'] == 'resize_start':
            new_start = max(0, min(orig_end - 100, orig_start + delta_ms))
            cue['start'] = ms_to_srt_ts(int(new_start))
        elif self._drag['type'] == 'resize_end':
            new_end = max(orig_start + 100, orig_end + delta_ms)
            cue['end'] = ms_to_srt_ts(int(new_end))

        # Redraw only cue blocks (not waveform) for performance
        cw = self._canvas.winfo_width()
        ch = self._canvas.winfo_height()
        self._draw_cue_blocks(cw, ch)

    def _on_mouse_up(self, event):
        """Handle mouse button release — finalize drag."""
        if not self._drag:
            return

        cues = self._cues_fn()
        idx = self._drag['cue_idx']
        self._drag = None

        if idx < len(cues):
            from .subtitle_filters import srt_ts_to_ms
            cue = cues[idx]
            try:
                new_start = srt_ts_to_ms(cue['start'])
                new_end = srt_ts_to_ms(cue['end'])
            except (KeyError, ValueError):
                return
            if self._on_cue_modified:
                self._on_cue_modified(idx, new_start, new_end)

    def _on_hover(self, event):
        """Change cursor based on hover position."""
        if self._drag:
            return

        cw = self._canvas.winfo_width()
        ch = self._canvas.winfo_height()
        cues = self._cues_fn()

        if not cues or self._duration_ms <= 0:
            self._canvas.configure(cursor='')
            return

        from .subtitle_filters import srt_ts_to_ms

        wave_bottom = ch - 4
        block_top = wave_bottom - 28
        block_bottom = wave_bottom - 4

        if block_top <= event.y <= block_bottom:
            for cue in cues:
                try:
                    start_ms = srt_ts_to_ms(cue['start'])
                    end_ms = srt_ts_to_ms(cue['end'])
                except (KeyError, ValueError):
                    continue

                x1 = self._ms_to_x(start_ms, cw)
                x2 = self._ms_to_x(end_ms, cw)

                if x1 - 2 <= event.x <= x2 + 2:
                    if (abs(event.x - x1) <= EDGE_GRAB_PX or
                            abs(event.x - x2) <= EDGE_GRAB_PX):
                        self._canvas.configure(cursor='sb_h_double_arrow')
                    else:
                        self._canvas.configure(cursor='fleur')
                    return

        self._canvas.configure(cursor='')

    def _on_right_click(self, event):
        """Show context menu on right-click."""
        if self._duration_ms <= 0:
            return
        self._ctx_menu.tk_popup(event.x_root, event.y_root)

    def _add_cue_at_cursor(self):
        """Add a 3-second cue starting at the current playback cursor position."""
        if self._duration_ms <= 0:
            return

        from .subtitle_filters import ms_to_srt_ts, srt_ts_to_ms

        # Use playback cursor position, or start of view if no cursor
        start_ms = self._playback_pos_ms
        if start_ms is None:
            start_ms = self._view_start_ms
        start_ms = max(0, start_ms)
        end_ms = min(self._duration_ms, start_ms + 500)

        if self._push_undo:
            self._push_undo()

        cues = self._cues_fn()
        new_cue = {
            'index': len(cues) + 1,
            'start': ms_to_srt_ts(int(start_ms)),
            'end': ms_to_srt_ts(int(end_ms)),
            'text': '',
        }

        # Insert in sorted order by start time
        insert_idx = len(cues)
        for i, c in enumerate(cues):
            try:
                if srt_ts_to_ms(c['start']) > start_ms:
                    insert_idx = i
                    break
            except (KeyError, ValueError):
                continue

        cues.insert(insert_idx, new_cue)

        # Re-index
        for i, c in enumerate(cues):
            c['index'] = i + 1

        self._log(f"Added cue #{insert_idx + 1}: "
                  f"{_format_time(start_ms)} → {_format_time(end_ms)}", 'SUCCESS')

        # Notify editor to refresh tree and select the new cue
        if self._on_cue_modified:
            self._on_cue_modified(insert_idx, start_ms, end_ms)

        # Select the new cue
        self._selected_cue_idx = insert_idx
        if self._on_selection_changed:
            self._on_selection_changed(insert_idx)

    def _set_marker_a_at_click(self, event):
        """Set marker A at the current mouse position (A key)."""
        if self._duration_ms <= 0:
            return
        cw = self._canvas.winfo_width()
        ms = self._x_to_ms(event.x, cw)
        self._marker_a_ms = max(0, min(self._duration_ms, ms))
        self._log(f"Marker A set: {_format_time(self._marker_a_ms)}", 'INFO')
        self._full_redraw()

    def _set_marker_b_at_click(self, event):
        """Set marker B at the current mouse position (B key)."""
        if self._duration_ms <= 0:
            return
        cw = self._canvas.winfo_width()
        ms = self._x_to_ms(event.x, cw)
        self._marker_b_ms = max(0, min(self._duration_ms, ms))
        self._log(f"Marker B set: {_format_time(self._marker_b_ms)}", 'INFO')
        self._full_redraw()

    def _set_marker_a_at_cursor(self):
        """Set marker A at the current playback position."""
        if self._playback_pos_ms is not None:
            self._marker_a_ms = self._playback_pos_ms
            self._log(f"Marker A set: {_format_time(self._marker_a_ms)}", 'INFO')
            self._full_redraw()

    def _set_marker_b_at_cursor(self):
        """Set marker B at the current playback position."""
        if self._playback_pos_ms is not None:
            self._marker_b_ms = self._playback_pos_ms
            self._log(f"Marker B set: {_format_time(self._marker_b_ms)}", 'INFO')
            self._full_redraw()

    def _create_cue_from_markers(self):
        """Create a new subtitle cue between markers A and B."""
        if self._marker_a_ms is None or self._marker_b_ms is None:
            self._log("Set both markers A and B first", 'WARNING')
            return

        from .subtitle_filters import ms_to_srt_ts

        start = min(self._marker_a_ms, self._marker_b_ms)
        end = max(self._marker_a_ms, self._marker_b_ms)
        if end - start < 100:
            self._log("Markers too close together (min 100ms)", 'WARNING')
            return

        if self._push_undo:
            self._push_undo()

        cues = self._cues_fn()
        new_cue = {
            'index': len(cues) + 1,
            'start': ms_to_srt_ts(int(start)),
            'end': ms_to_srt_ts(int(end)),
            'text': '',
        }

        # Insert in sorted order
        from .subtitle_filters import srt_ts_to_ms
        insert_idx = len(cues)
        for i, c in enumerate(cues):
            try:
                if srt_ts_to_ms(c['start']) > start:
                    insert_idx = i
                    break
            except (KeyError, ValueError):
                continue

        cues.insert(insert_idx, new_cue)

        # Re-index
        for i, c in enumerate(cues):
            c['index'] = i + 1

        self._log(f"Created cue #{insert_idx + 1}: "
                  f"{_format_time(start)} → {_format_time(end)}", 'SUCCESS')

        if self._on_cue_modified:
            self._on_cue_modified(insert_idx, start, end)

    def _set_cue_start_from_marker(self):
        """Set the selected cue's start time to marker A."""
        if self._marker_a_ms is None:
            self._log("Set marker A first", 'WARNING')
            return
        if self._selected_cue_idx is None:
            self._log("Select a cue first", 'WARNING')
            return

        from .subtitle_filters import ms_to_srt_ts, srt_ts_to_ms

        cues = self._cues_fn()
        idx = self._selected_cue_idx
        if idx >= len(cues):
            return

        if self._push_undo:
            self._push_undo()

        cue = cues[idx]
        end_ms = srt_ts_to_ms(cue['end'])
        new_start = min(self._marker_a_ms, end_ms - 100)
        cue['start'] = ms_to_srt_ts(int(new_start))

        self._log(f"Cue #{idx + 1} start → {_format_time(new_start)}", 'INFO')
        if self._on_cue_modified:
            self._on_cue_modified(idx, new_start, end_ms)

    def _set_cue_end_from_marker(self):
        """Set the selected cue's end time to marker B."""
        if self._marker_b_ms is None:
            self._log("Set marker B first", 'WARNING')
            return
        if self._selected_cue_idx is None:
            self._log("Select a cue first", 'WARNING')
            return

        from .subtitle_filters import ms_to_srt_ts, srt_ts_to_ms

        cues = self._cues_fn()
        idx = self._selected_cue_idx
        if idx >= len(cues):
            return

        if self._push_undo:
            self._push_undo()

        cue = cues[idx]
        start_ms = srt_ts_to_ms(cue['start'])
        new_end = max(self._marker_b_ms, start_ms + 100)
        cue['end'] = ms_to_srt_ts(int(new_end))

        self._log(f"Cue #{idx + 1} end → {_format_time(new_end)}", 'INFO')
        if self._on_cue_modified:
            self._on_cue_modified(idx, start_ms, new_end)

    # ─────────────────────────────────────────────────────────────────────
    # Playback (mpv IPC)
    # ─────────────────────────────────────────────────────────────────────

    def _mpv_cmd(self, command_list):
        """Send a command to mpv via IPC and return the response."""
        if not self._mpv_socket:
            return None
        try:
            sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
            sock.settimeout(2)
            sock.connect(self._mpv_socket)
            payload = _json.dumps({"command": command_list}) + '\n'
            sock.sendall(payload.encode())
            data = sock.recv(4096).decode()
            sock.close()
            return _json.loads(data)
        except Exception:
            return None

    def _write_temp_srt(self):
        """Write current cues to a temporary SRT file for mpv subtitle display."""
        from .subtitle_filters import write_srt
        cues = self._cues_fn()
        if not cues:
            return None

        if not self._temp_srt:
            fd, path = tempfile.mkstemp(suffix='.srt',
                                         prefix='docflix_live_sub_')
            os.close(fd)
            self._temp_srt = path

        with open(self._temp_srt, 'w', encoding='utf-8') as f:
            f.write(write_srt(cues))
        return self._temp_srt

    def reload_subtitles(self):
        """Re-write the temp SRT and tell mpv to reload it.
        Call after any cue modification (edit, drag, filter)."""
        if not self._mpv_proc or self._mpv_proc.poll() is not None:
            return
        if not self._temp_srt:
            return
        self._write_temp_srt()
        self._mpv_cmd(["sub-reload"])

    def _start_mpv(self, start_sec=0):
        """Launch mpv for playback with IPC and live subtitle preview."""
        if not self._video_path:
            self._log("No video loaded for playback", 'WARNING')
            return False

        self._stop_mpv()

        # Write current cues to temp SRT for subtitle display
        srt_path = self._write_temp_srt()

        # Create IPC socket path
        self._mpv_socket = os.path.join(
            tempfile.gettempdir(),
            f'docflix_waveform_mpv_{os.getpid()}')
        if os.path.exists(self._mpv_socket):
            try:
                os.unlink(self._mpv_socket)
            except OSError:
                pass

        try:
            cmd = [
                'mpv',
                f'--input-ipc-server={self._mpv_socket}',
                '--keep-open=yes',
                f'--start={start_sec}',
                '--sub-auto=no',  # don't auto-load subs from filesystem
                '--sid=no',       # start with no subs — we add ours via IPC
            ]
            if self._video_frame:
                # Embed video in the provided Tk frame
                self._video_frame.update_idletasks()
                wid = str(self._video_frame.winfo_id())
                cmd.extend([
                    f'--wid={wid}',
                    '--no-border',
                    '--cursor-autohide=1000',
                    '--osd-level=1',
                ])
            else:
                cmd.append('--no-video')
            cmd.append(self._video_path)
            self._mpv_proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self._playing = True
            self._play_btn.configure(text="⏸ Pause")
            # Wait briefly for IPC socket, then add our subtitle track
            if srt_path:
                self.after(500, lambda: self._load_live_subtitles(srt_path))
            # Start cursor polling
            self._poll_playback_position()
            return True
        except FileNotFoundError:
            self._log("mpv not found — install mpv for playback", 'ERROR')
            return False
        except Exception as e:
            self._log(f"Playback error: {e}", 'ERROR')
            return False

    def _load_live_subtitles(self, srt_path, retries=5):
        """Add our external subtitle file via IPC and select it."""
        if not self._mpv_proc or self._mpv_proc.poll() is not None:
            return
        # sub-add with "select" flag adds and immediately displays the track
        resp = self._mpv_cmd(["sub-add", srt_path, "select"])
        if resp and resp.get('error') == 'success':
            return
        # IPC socket might not be ready yet — retry
        if retries > 0:
            self.after(300, lambda: self._load_live_subtitles(srt_path,
                                                                retries - 1))

    def _stop_mpv(self):
        """Stop mpv playback."""
        if self._cursor_poll_id:
            self.after_cancel(self._cursor_poll_id)
            self._cursor_poll_id = None
        if self._mpv_proc and self._mpv_proc.poll() is None:
            try:
                self._mpv_proc.terminate()
                self._mpv_proc.wait(timeout=5)
            except Exception:
                pass
        self._mpv_proc = None
        self._playing = False
        self._play_btn.configure(text="▶ Play")
        if self._mpv_socket and os.path.exists(self._mpv_socket):
            try:
                os.unlink(self._mpv_socket)
            except OSError:
                pass
            self._mpv_socket = None
        if self._temp_srt and os.path.exists(self._temp_srt):
            try:
                os.unlink(self._temp_srt)
            except OSError:
                pass
            self._temp_srt = None

    def _toggle_playback(self):
        """Play/pause toggle."""
        if not self._mpv_proc or self._mpv_proc.poll() is not None:
            # No mpv running — start playback
            start_sec = 0
            if self._playback_pos_ms is not None:
                start_sec = self._playback_pos_ms / 1000
            elif self._marker_a_ms is not None:
                start_sec = self._marker_a_ms / 1000
            self._start_mpv(start_sec)
        else:
            # Toggle pause
            self._mpv_cmd(["cycle", "pause"])
            resp = self._mpv_cmd(["get_property", "pause"])
            if resp and resp.get('data') is True:
                self._playing = False
                self._play_btn.configure(text="▶ Play")
            else:
                self._playing = True
                self._play_btn.configure(text="⏸ Pause")

    def _stop_playback(self):
        """Stop playback completely."""
        self._stop_mpv()

    def _poll_playback_position(self):
        """Poll mpv for current playback position and update cursor.
        Only updates cursor position when actively playing — when paused,
        the cursor stays where the user clicked (millisecond-precise)."""
        if not self._mpv_proc or self._mpv_proc.poll() is not None:
            self._playing = False
            self._play_btn.configure(text="▶ Play")
            return

        # Only move the cursor when actively playing
        if self._playing:
            resp = self._mpv_cmd(["get_property", "playback-time"])
            if resp and resp.get('data') is not None:
                pos_sec = resp['data']
                self._playback_pos_ms = pos_sec * 1000
                cw = self._canvas.winfo_width()
                ch = self._canvas.winfo_height()
                if cw > 0 and ch > 0:
                    self._draw_playback_cursor(cw, ch)

                # Auto-scroll to keep cursor visible
                if self._playback_pos_ms > self._view_end_ms:
                    view_span = self._view_end_ms - self._view_start_ms
                    self._view_start_ms = self._playback_pos_ms
                    self._view_end_ms = self._view_start_ms + view_span
                    if self._view_end_ms > self._duration_ms:
                        self._view_end_ms = self._duration_ms
                        self._view_start_ms = max(0, self._duration_ms - view_span)
                    self._full_redraw()

        self._cursor_poll_id = self.after(CURSOR_POLL_MS,
                                           self._poll_playback_position)

    # ─────────────────────────────────────────────────────────────────────
    # Public API for Editor Integration
    # ─────────────────────────────────────────────────────────────────────

    def select_cue(self, idx):
        """Highlight a cue (called from tree selection changes)."""
        if idx == self._selected_cue_idx:
            return
        self._selected_cue_idx = idx
        cw = self._canvas.winfo_width()
        ch = self._canvas.winfo_height()
        if cw > 0 and ch > 0:
            self._draw_cue_blocks(cw, ch)

    def scroll_to_cue(self, idx):
        """Pan the view so the given cue is visible and centered."""
        cues = self._cues_fn()
        if not cues or idx >= len(cues) or self._duration_ms <= 0:
            return

        from .subtitle_filters import srt_ts_to_ms

        try:
            start_ms = srt_ts_to_ms(cues[idx]['start'])
            end_ms = srt_ts_to_ms(cues[idx]['end'])
        except (KeyError, ValueError):
            return

        cue_mid = (start_ms + end_ms) / 2
        view_span = self._view_end_ms - self._view_start_ms

        # Only scroll if cue is not already visible
        if start_ms >= self._view_start_ms and end_ms <= self._view_end_ms:
            return

        self._view_start_ms = max(0, cue_mid - view_span / 2)
        self._view_end_ms = self._view_start_ms + view_span
        if self._view_end_ms > self._duration_ms:
            self._view_end_ms = self._duration_ms
            self._view_start_ms = max(0, self._duration_ms - view_span)

        self._full_redraw()

    def set_playback_position(self, ms):
        """Update the playback cursor position."""
        self._playback_pos_ms = ms
        cw = self._canvas.winfo_width()
        ch = self._canvas.winfo_height()
        if cw > 0 and ch > 0:
            self._draw_playback_cursor(cw, ch)

    def refresh(self):
        """Full redraw — call after cue list changes (undo/redo/filters)."""
        self._full_redraw()

    @property
    def is_loaded(self):
        """True if audio waveform data is loaded."""
        return self._raw_samples is not None
