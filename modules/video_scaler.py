"""
Docflix Video Converter — Video Scaler

Standalone tool for batch resizing/scaling video files.
Supports CPU and GPU-accelerated scaling (NVIDIA NVENC,
Intel QSV, AMD VAAPI) with aspect ratio preservation.
"""

import json
import os
from pathlib import Path
import subprocess
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from .constants import VIDEO_EXTENSIONS, GPU_BACKENDS, VIDEO_CODEC_MAP

try:
    from tkinterdnd2 import DND_FILES
    HAS_DND = True
except ImportError:
    HAS_DND = False


# ═══════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════

RESOLUTION_PRESETS = [
    'Original',
    '2160p (4K)',
    '1440p (2K)',
    '1080p',
    '720p',
    '480p',
    'Custom',
]

RESOLUTION_MAP = {
    'Original':     None,
    '2160p (4K)':   (3840, 2160),
    '1440p (2K)':   (2560, 1440),
    '1080p':        (1920, 1080),
    '720p':         (1280, 720),
    '480p':         (854, 480),
}


# ═══════════════════════════════════════════════════════════════════
# ffprobe helpers
# ═══════════════════════════════════════════════════════════════════

def _probe_video_info(filepath):
    """Probe a video file and return (width, height, duration_secs, codec_name) or Nones.

    Uses a 1-frame decode to get the actual content dimensions (which may
    differ from container dimensions for letterboxed/cropped content).
    Falls back to ffprobe stream metadata if decode probe fails.
    """
    width, height, duration, codec = None, None, None, None

    # Get duration and codec from ffprobe (fast metadata read)
    try:
        cmd = [
            'ffprobe', '-v', 'quiet', '-print_format', 'json',
            '-show_format', '-show_streams', '-select_streams', 'v:0',
            filepath
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if r.returncode == 0:
            data = json.loads(r.stdout)
            streams = data.get('streams', [])
            fmt = data.get('format', {})
            if streams:
                s = streams[0]
                width = s.get('width')
                height = s.get('height')
                codec = s.get('codec_name', '?')
            dur = fmt.get('duration')
            duration = float(dur) if dur else None
    except Exception:
        pass

    # Decode 1 frame from mid-file to get actual content dimensions.
    # Some files have variable resolution (e.g. 1080p intro, 960p main content)
    # or H.264 SPS crop metadata. Seeking to 30% ensures we sample the main content.
    try:
        import tempfile
        seek_pos = int(duration * 0.3) if duration and duration > 60 else 10
        _tmpf = tempfile.NamedTemporaryFile(suffix='.png', delete=False)
        _tmpf.close()
        cmd = [
            'ffmpeg', '-y', '-ss', str(seek_pos),
            '-i', filepath, '-vframes', '1',
            '-update', '1', _tmpf.name
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if r.returncode == 0 and os.path.isfile(_tmpf.name):
            # Read actual PNG dimensions
            with open(_tmpf.name, 'rb') as pf:
                header = pf.read(32)
                if len(header) >= 24 and header[:8] == b'\x89PNG\r\n\x1a\n':
                    import struct
                    pw = struct.unpack('>I', header[16:20])[0]
                    ph = struct.unpack('>I', header[20:24])[0]
                    if pw > 0 and ph > 0:
                        width = pw
                        height = ph
        try:
            os.remove(_tmpf.name)
        except OSError:
            pass
    except Exception:
        pass  # fall back to ffprobe dimensions

    return width, height, duration, codec


def _detect_gpu_backends_quick():
    """Quick GPU detection — check which encoders are available in ffmpeg."""
    backends = {}
    try:
        r = subprocess.run(['ffmpeg', '-hide_banner', '-encoders'],
                          capture_output=True, text=True, timeout=10)
        output = r.stdout
        for bid, binfo in GPU_BACKENDS.items():
            for enc in binfo.get('detect_encoders', []):
                if enc in output:
                    backends[bid] = binfo['label']
                    break
    except Exception:
        pass
    return backends


# ═══════════════════════════════════════════════════════════════════
# Scale filter builder
# ═══════════════════════════════════════════════════════════════════

def _build_scale_filter(target_w, target_h, backend_id=None):
    """Build the -vf scale filter string.

    Uses explicit target_w and target_h (pre-calculated from the actual
    decoded content dimensions to preserve aspect ratio correctly).
    Both must be even numbers.

    Returns the filter string or None if target_h is not set.
    """
    if target_h is None or target_h <= 0:
        return None

    # Ensure even dimensions (required by most codecs)
    if target_w and target_w % 2 != 0:
        target_w += 1
    if target_h % 2 != 0:
        target_h += 1

    if backend_id and backend_id in GPU_BACKENDS:
        backend = GPU_BACKENDS[backend_id]
        hwaccel_flags = backend.get('hwaccel', [])
        has_hw_output_fmt = '-hwaccel_output_format' in hwaccel_flags

        if has_hw_output_fmt:
            # GPU scale filter (QSV, VAAPI — frames on GPU)
            scale_name = backend['scale_filter'].split('=')[0]
            fmt = backend['scale_filter'].split('=', 1)[1] if '=' in backend['scale_filter'] else ''
            if fmt:
                return f"{scale_name}=w={target_w}:h={target_h}:{fmt}"
            else:
                return f"{scale_name}=w={target_w}:h={target_h}"
        else:
            # CPU scale filter (NVENC — frames in system memory)
            return f"scale={target_w}:{target_h}"
    else:
        return f"scale={target_w}:{target_h}"


# ═══════════════════════════════════════════════════════════════════
# Main tool window
# ═══════════════════════════════════════════════════════════════════

def open_video_scaler(app):
    """Open the Video Scaler tool window."""

    win = tk.Toplevel(app.root)
    win.title("Video Scaler")
    win.geometry("920x750")
    win.minsize(750, 550)
    try:
        app._center_on_main(win)
    except Exception:
        pass

    # ── State ──
    files = []          # list of dicts: path, name, width, height, duration, codec, target, status
    processing = [False]
    stop_flag = [False]

    # ── Load saved preferences ──
    _sp = getattr(app, '_scaler_prefs', {})

    # ── Options ──
    opt_resolution   = tk.StringVar(value=_sp.get('resolution', '1080p'))
    opt_custom_w     = tk.StringVar(value=_sp.get('custom_w', '1280'))
    opt_custom_h     = tk.StringVar(value=_sp.get('custom_h', '720'))
    opt_encoder      = tk.StringVar(value=_sp.get('encoder', 'cpu'))
    opt_codec        = tk.StringVar(value=_sp.get('codec', 'H.265 / HEVC'))
    opt_preset       = tk.StringVar(value=_sp.get('preset', 'medium'))
    opt_crf          = tk.StringVar(value=_sp.get('crf', '23'))
    opt_audio        = tk.StringVar(value=_sp.get('audio', 'copy'))
    opt_container    = tk.StringVar(value=_sp.get('container', '.mkv'))
    opt_output_mode  = tk.StringVar(value=_sp.get('output_mode', 'folder'))
    opt_output_folder = tk.StringVar(value=_sp.get('output_folder', ''))

    # Detect GPU backends
    gpu_backends = _detect_gpu_backends_quick()
    encoder_labels = ['CPU']
    encoder_ids = {'CPU': 'cpu'}
    for bid, label in gpu_backends.items():
        encoder_labels.append(label)
        encoder_ids[label] = bid

    # ── Main layout ──
    main_frame = ttk.Frame(win, padding=8)
    main_frame.pack(fill='both', expand=True)
    main_frame.columnconfigure(0, weight=1)
    main_frame.rowconfigure(1, weight=1)   # file list
    main_frame.rowconfigure(4, weight=1)   # log

    # ── Toolbar ──
    toolbar = ttk.Frame(main_frame)
    toolbar.grid(row=0, column=0, sticky='ew', pady=(0, 4))

    def _add_files():
        paths = filedialog.askopenfilenames(
            parent=win, title="Select Video Files",
            filetypes=[("Video files", "*.mkv *.mp4 *.avi *.mov *.wmv *.flv *.webm *.ts *.m2ts *.mts"),
                       ("All files", "*.*")])
        for p in paths:
            _add_one_file(p)
        _rebuild_tree()

    def _add_folder():
        folder = filedialog.askdirectory(parent=win, title="Select Folder")
        if folder:
            added = 0
            for root_dir, dirs, fnames in os.walk(folder):
                # Skip hidden directories
                dirs[:] = [d for d in dirs if not d.startswith('.')]
                for fn in sorted(fnames):
                    if fn.startswith('.'):
                        continue
                    if Path(fn).suffix.lower() in VIDEO_EXTENSIONS:
                        _add_one_file(os.path.join(root_dir, fn))
                        added += 1
            _rebuild_tree()
            _log(f"Added {added} file(s) from {folder}", 'INFO')

    ttk.Button(toolbar, text="Add Files", command=_add_files).pack(side='left', padx=(0, 4))
    ttk.Button(toolbar, text="Add Folder", command=_add_folder).pack(side='left', padx=(0, 4))
    ttk.Button(toolbar, text="Clear", command=lambda: _clear_files()).pack(side='left', padx=(0, 12))

    process_btn = ttk.Button(toolbar, text="Process All", command=lambda: _start_processing())
    process_btn.pack(side='right', padx=(4, 0))
    stop_btn = ttk.Button(toolbar, text="Stop", command=lambda: _stop(), state='disabled')
    stop_btn.pack(side='right')

    # ── File list ──
    tree_frame = ttk.Frame(main_frame)
    tree_frame.grid(row=1, column=0, sticky='nsew', pady=(0, 4))
    tree_frame.columnconfigure(0, weight=1)
    tree_frame.rowconfigure(0, weight=1)

    tree = ttk.Treeview(tree_frame, columns=('source_res', 'target_res', 'size', 'status'),
                        show='headings', selectmode='extended')
    tree.heading('source_res', text='Source')
    tree.heading('target_res', text='Target')
    tree.heading('size', text='Size')
    tree.heading('status', text='Status')

    tree.column('source_res', width=100, minwidth=80)
    tree.column('target_res', width=100, minwidth=80)
    tree.column('size', width=80, minwidth=60)
    tree.column('status', width=100, minwidth=80)

    # Add filename as the tree item text via #0
    tree['displaycolumns'] = ('source_res', 'target_res', 'size', 'status')
    tree['show'] = ('tree', 'headings')
    tree.column('#0', width=300, minwidth=200)
    tree.heading('#0', text='Filename')

    tree_scroll = ttk.Scrollbar(tree_frame, orient='vertical', command=tree.yview)
    tree.configure(yscrollcommand=tree_scroll.set)
    tree.grid(row=0, column=0, sticky='nsew')
    tree_scroll.grid(row=0, column=1, sticky='ns')

    # ── Settings panel ──
    settings_frame = ttk.Frame(main_frame, padding=(0, 4))
    settings_frame.grid(row=2, column=0, sticky='ew', pady=(0, 4))

    # Row 1: Resolution
    row1 = ttk.Frame(settings_frame)
    row1.pack(fill='x', pady=2)

    ttk.Label(row1, text="Resolution:").pack(side='left', padx=(0, 4))
    res_combo = ttk.Combobox(row1, textvariable=opt_resolution,
                              values=RESOLUTION_PRESETS, width=14, state='readonly')
    res_combo.pack(side='left', padx=(0, 6))

    custom_frame = ttk.Frame(row1)
    custom_w = ttk.Entry(custom_frame, textvariable=opt_custom_w, width=6)
    custom_w.pack(side='left')
    ttk.Label(custom_frame, text="x").pack(side='left', padx=2)
    custom_h = ttk.Entry(custom_frame, textvariable=opt_custom_h, width=6)
    custom_h.pack(side='left')

    def _on_res_change(event=None):
        if opt_resolution.get() == 'Custom':
            custom_frame.pack(side='left', padx=(0, 6))
        else:
            custom_frame.pack_forget()
        _update_targets()
    res_combo.bind('<<ComboboxSelected>>', _on_res_change)

    ttk.Separator(row1, orient='vertical').pack(side='left', fill='y', padx=8)

    ttk.Label(row1, text="Encoder:").pack(side='left', padx=(0, 2))
    enc_combo = ttk.Combobox(row1, textvariable=opt_encoder,
                              values=encoder_labels, width=16, state='readonly')
    enc_combo.set('CPU')
    enc_combo.pack(side='left', padx=(0, 6))

    ttk.Label(row1, text="Preset:").pack(side='left', padx=(0, 2))
    preset_combo = ttk.Combobox(row1, textvariable=opt_preset, width=10, state='readonly')
    preset_combo.pack(side='left')

    def _on_encoder_change(event=None):
        enc_label = opt_encoder.get()
        bid = encoder_ids.get(enc_label, 'cpu')
        codec_name = opt_codec.get()
        if bid == 'cpu':
            info = VIDEO_CODEC_MAP.get(codec_name, VIDEO_CODEC_MAP['H.265 / HEVC'])
            preset_combo['values'] = info['cpu_presets']
            if opt_preset.get() not in info['cpu_presets']:
                opt_preset.set(info.get('cpu_preset_default', 'medium'))
        else:
            backend = GPU_BACKENDS.get(bid, {})
            presets = backend.get('presets', ())
            preset_combo['values'] = presets
            if opt_preset.get() not in presets:
                opt_preset.set(backend.get('preset_default', presets[0] if presets else ''))
    enc_combo.bind('<<ComboboxSelected>>', _on_encoder_change)

    # Row 2: Codec, CRF, Audio, Container, Output
    row2 = ttk.Frame(settings_frame)
    row2.pack(fill='x', pady=2)

    ttk.Label(row2, text="Codec:").pack(side='left', padx=(0, 2))
    codec_combo = ttk.Combobox(row2, textvariable=opt_codec,
                                values=['H.265 / HEVC', 'H.264 / AVC', 'AV1'],
                                width=14, state='readonly')
    codec_combo.pack(side='left', padx=(0, 6))
    codec_combo.bind('<<ComboboxSelected>>', _on_encoder_change)

    ttk.Label(row2, text="CRF:").pack(side='left', padx=(0, 2))
    crf_entry = ttk.Entry(row2, textvariable=opt_crf, width=4)
    crf_entry.pack(side='left', padx=(0, 6))

    ttk.Label(row2, text="Audio:").pack(side='left', padx=(0, 2))
    ttk.Combobox(row2, textvariable=opt_audio,
                 values=['copy', 'aac', 'ac3', 'eac3', 'mp3', 'opus', 'flac'],
                 width=6, state='readonly').pack(side='left', padx=(0, 6))

    ttk.Label(row2, text="Container:").pack(side='left', padx=(0, 2))
    ttk.Combobox(row2, textvariable=opt_container,
                 values=['.mkv', '.mp4'], width=5, state='readonly').pack(side='left', padx=(0, 6))

    # Row 3: Output
    row3 = ttk.Frame(settings_frame)
    row3.pack(fill='x', pady=2)

    ttk.Label(row3, text="Output:").pack(side='left', padx=(0, 4))
    ttk.Radiobutton(row3, text="Replace in-place", variable=opt_output_mode,
                    value='inplace').pack(side='left', padx=(0, 4))
    ttk.Radiobutton(row3, text="Save to folder:", variable=opt_output_mode,
                    value='folder').pack(side='left', padx=(0, 4))
    out_entry = ttk.Entry(row3, textvariable=opt_output_folder, width=24)
    out_entry.pack(side='left', padx=(0, 4))
    ttk.Button(row3, text="Browse...",
               command=lambda: opt_output_folder.set(
                   filedialog.askdirectory(parent=win, title="Select Output Folder")
                   or opt_output_folder.get())).pack(side='left')

    # Initialize presets
    _on_encoder_change()

    # ── Progress bar + status ──
    progress_frame = ttk.Frame(main_frame)
    progress_frame.grid(row=3, column=0, sticky='ew', pady=(0, 4))
    progress_frame.columnconfigure(0, weight=1)

    progress_var = tk.DoubleVar(value=0.0)
    progress_bar = ttk.Progressbar(progress_frame, variable=progress_var,
                                    maximum=100, mode='determinate')
    progress_bar.grid(row=0, column=0, sticky='ew', padx=(0, 8))

    progress_label = ttk.Label(progress_frame, text="", width=40, anchor='e')
    progress_label.grid(row=0, column=1, sticky='e')

    # ── Log panel ──
    log_frame = ttk.Frame(main_frame)
    log_frame.grid(row=4, column=0, sticky='nsew', pady=(0, 4))
    log_frame.columnconfigure(0, weight=1)
    log_frame.rowconfigure(0, weight=1)

    log_text = tk.Text(log_frame, height=6, wrap='word', font=('Consolas', 9),
                       bg='#1e1e2e', fg='#d4d4d4', borderwidth=1, relief='sunken')
    log_scroll = ttk.Scrollbar(log_frame, orient='vertical', command=log_text.yview)
    log_text.configure(yscrollcommand=log_scroll.set, state='disabled')
    log_text.grid(row=0, column=0, sticky='nsew')
    log_scroll.grid(row=0, column=1, sticky='ns')

    # Log tag colors
    log_text.tag_configure('INFO', foreground='#d4d4d4')
    log_text.tag_configure('SUCCESS', foreground='#66bb6a')
    log_text.tag_configure('WARNING', foreground='#ffa726')
    log_text.tag_configure('ERROR', foreground='#ef5350')

    # ── Close button ──
    close_frame = ttk.Frame(main_frame)
    close_frame.grid(row=5, column=0, sticky='e', pady=(0, 0))

    def _save_scaler_prefs():
        """Save Video Scaler settings to preferences."""
        sp = {
            'resolution':    opt_resolution.get(),
            'custom_w':      opt_custom_w.get(),
            'custom_h':      opt_custom_h.get(),
            'encoder':       opt_encoder.get(),
            'codec':         opt_codec.get(),
            'preset':        opt_preset.get(),
            'crf':           opt_crf.get(),
            'audio':         opt_audio.get(),
            'container':     opt_container.get(),
            'output_mode':   opt_output_mode.get(),
            'output_folder': opt_output_folder.get(),
        }
        app._scaler_prefs = sp
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
                prefs['video_scaler'] = sp
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(json.dumps(prefs, indent=2))
        except Exception:
            pass

    def _close():
        _save_scaler_prefs()
        win.destroy()
        if getattr(app, '_standalone_mode', False):
            app.root.destroy()
    ttk.Button(close_frame, text="Close", command=_close).pack(side='right')
    win.protocol('WM_DELETE_WINDOW', _close)

    # ═══════════════════════════════════════════════════════════════
    # Helper functions
    # ═══════════════════════════════════════════════════════════════

    def _log(msg, level='INFO'):
        def _do():
            log_text.configure(state='normal')
            log_text.insert('end', msg + '\n', level)
            log_text.see('end')
            log_text.configure(state='disabled')
        if threading.current_thread() is threading.main_thread():
            _do()
        else:
            win.after(0, _do)

    def _add_one_file(filepath):
        """Add a single file to the list."""
        # Skip duplicates
        for f in files:
            if f['path'] == filepath:
                return
        if Path(filepath).suffix.lower() not in VIDEO_EXTENSIONS:
            return

        w, h, dur, codec = _probe_video_info(filepath)
        size_bytes = os.path.getsize(filepath) if os.path.isfile(filepath) else 0
        if size_bytes >= 1_073_741_824:
            size_str = f"{size_bytes / 1_073_741_824:.1f} GB"
        else:
            size_str = f"{size_bytes / 1_048_576:.0f} MB"

        files.append({
            'path': filepath,
            'name': os.path.basename(filepath),
            'width': w,
            'height': h,
            'duration': dur,
            'codec': codec,
            'size_str': size_str,
            'status': '',
        })

    def _get_target(f):
        """Get target resolution (w, h) for a file, or None for Original."""
        res = opt_resolution.get()
        if res == 'Original':
            return None
        if res == 'Custom':
            try:
                tw = int(opt_custom_w.get())
                th = int(opt_custom_h.get())
                return (tw, th)
            except ValueError:
                return None
        dims = RESOLUTION_MAP.get(res)
        return dims

    def _target_str(f):
        """Get display string for target resolution."""
        target = _get_target(f)
        if target is None:
            return 'Original'
        _, th = target
        src_w, src_h = f.get('width'), f.get('height')
        # Calculate actual output width preserving aspect ratio
        if src_w and src_h and src_h > 0:
            actual_w = int(round(src_w * th / src_h))
            if actual_w % 2 != 0:
                actual_w += 1
            return f"{actual_w}x{th}"
        return f"?x{th}"

    def _is_upscale(f):
        """Check if target is larger than source."""
        target = _get_target(f)
        if target is None:
            return False
        _, th = target
        src_h = f.get('height')
        if src_h and th > src_h:
            return True
        return False

    def _rebuild_tree():
        """Rebuild the treeview from the files list."""
        tree.delete(*tree.get_children())
        for i, f in enumerate(files):
            src = f"{f['width']}x{f['height']}" if f['width'] else '?'
            tgt = _target_str(f)
            warn = ' (upscale)' if _is_upscale(f) else ''
            iid = tree.insert('', 'end', text=f['name'],
                              values=(src, tgt + warn, f['size_str'], f['status']))

    def _update_targets():
        """Update target column for all files."""
        items = tree.get_children()
        for i, iid in enumerate(items):
            if i < len(files):
                f = files[i]
                tgt = _target_str(f)
                warn = ' (upscale)' if _is_upscale(f) else ''
                tree.set(iid, 'target_res', tgt + warn)

    # Bind custom entry changes to update targets
    opt_custom_w.trace_add('write', lambda *a: _update_targets())
    opt_custom_h.trace_add('write', lambda *a: _update_targets())

    def _clear_files():
        files.clear()
        tree.delete(*tree.get_children())

    def _update_status(index, status):
        items = tree.get_children()
        if index < len(items):
            tree.set(items[index], 'status', status)
        if index < len(files):
            files[index]['status'] = status

    def _update_progress(pct, eta_str=''):
        """Update progress bar and label from any thread."""
        def _do():
            progress_var.set(min(100.0, pct))
            progress_label.configure(text=eta_str)
        win.after(0, _do)

    def _reset_progress():
        """Reset progress bar and label."""
        def _do():
            progress_var.set(0.0)
            progress_label.configure(text='')
        win.after(0, _do)

    def _parse_ffmpeg_time(time_str):
        """Parse ffmpeg time string (HH:MM:SS.xx) to seconds."""
        try:
            parts = time_str.strip().split(':')
            if len(parts) == 3:
                return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
            elif len(parts) == 2:
                return int(parts[0]) * 60 + float(parts[1])
            return float(parts[0])
        except (ValueError, IndexError):
            return 0.0

    # ═══════════════════════════════════════════════════════════════
    # Processing
    # ═══════════════════════════════════════════════════════════════

    def _build_cmd(f):
        """Build ffmpeg command for scaling a single file.
        Returns (cmd_list, output_path) or (None, None).
        """
        input_path = f['path']
        base = Path(input_path).stem
        ext = opt_container.get()

        target = _get_target(f)
        if target is None:
            return None, None  # no scaling needed

        _, target_h = target

        # Encoder setup
        enc_label = opt_encoder.get()
        bid = encoder_ids.get(enc_label, 'cpu')
        codec_name = opt_codec.get()
        codec_info = VIDEO_CODEC_MAP.get(codec_name, VIDEO_CODEC_MAP['H.265 / HEVC'])

        if bid == 'cpu':
            video_enc = codec_info['cpu_encoder']
        else:
            backend = GPU_BACKENDS.get(bid, {})
            video_enc = backend.get('encoders', {}).get(codec_name)
            if not video_enc:
                video_enc = codec_info['cpu_encoder']
                bid = 'cpu'

        # Output path
        res_tag = f"{target_h}p"
        if opt_output_mode.get() == 'folder' and opt_output_folder.get():
            out_dir = opt_output_folder.get()
            os.makedirs(out_dir, exist_ok=True)
            out_path = os.path.join(out_dir, f"{base}-{res_tag}{ext}")
        else:
            out_path = str(Path(input_path).parent / f"{base}-{res_tag}{ext}")

        # Calculate exact target dimensions from actual content aspect ratio
        # (probed via 1-frame decode which respects H.264 SPS crop metadata)
        src_w = f.get('width') or 1920
        src_h = f.get('height') or 1080
        if src_h > 0:
            target_w = int(round(src_w * target_h / src_h))
            # Ensure even
            if target_w % 2 != 0:
                target_w += 1
        else:
            target_w = int(round(target_h * 16 / 9))

        # Build command
        cmd = ['ffmpeg', '-y']

        # HW accel for GPU decode
        if bid != 'cpu' and bid in GPU_BACKENDS:
            backend_info = GPU_BACKENDS[bid]
            cmd.extend(backend_info['hwaccel'])

        cmd.extend(['-i', input_path])

        # Scale filter — uses explicit dimensions calculated from actual
        # decoded content size, so it works correctly with CUDA hwaccel
        # (which may deliver padded frames for cropped H.264 content)
        scale_vf = _build_scale_filter(target_w, target_h, bid if bid != 'cpu' else None)
        if scale_vf:
            # Append setsar=1:1 to force square pixels in the output.
            # Without this, the encoder may inherit the source SAR and
            # set a non-square SAR (e.g. 8:9) that squishes the image.
            cmd.extend(['-vf', f'{scale_vf},setsar=1:1'])

        # Video encoder
        cmd.extend(['-c:v', video_enc])

        # Preset
        preset = opt_preset.get()
        if preset:
            if bid != 'cpu' and bid in GPU_BACKENDS:
                flag = GPU_BACKENDS[bid].get('preset_flag', '-preset')
                if flag:
                    cmd.extend([flag, preset])
            else:
                cmd.extend(['-preset', preset])

        # CRF / quality
        crf = opt_crf.get()
        if crf:
            if bid == 'cpu':
                crf_flag = codec_info.get('crf_flag', '-crf')
                if crf_flag:
                    cmd.extend([crf_flag, crf])
            else:
                backend = GPU_BACKENDS.get(bid, {})
                cq_flag = backend.get('cq_flag')
                if cq_flag:
                    cmd.extend([cq_flag, crf])

        # Audio
        audio = opt_audio.get()
        if audio == 'copy':
            cmd.extend(['-c:a', 'copy'])
        else:
            cmd.extend(['-c:a', audio])
            if audio not in ('flac',):
                cmd.extend(['-b:a', '128k'])

        # Copy subtitles
        cmd.extend(['-c:s', 'copy'])

        cmd.append(out_path)
        return cmd, out_path

    def _process_one(i, f):
        """Process a single file. Returns True on success."""
        if stop_flag[0]:
            return False

        target = _get_target(f)
        if target is None:
            win.after(0, lambda: _update_status(i, 'Skipped'))
            _log(f"  Skipped (Original): {f['name']}", 'WARNING')
            return True

        if _is_upscale(f):
            _log(f"  Warning: upscaling {f['name']} ({f['height']}p -> {target[1]}p)", 'WARNING')

        win.after(0, lambda: _update_status(i, 'Processing...'))
        _log(f"  Scaling: {f['name']} -> {_target_str(f)}", 'INFO')
        _update_progress(0.0, f"File {i + 1}/{len(files)}: {f['name']}")

        try:
            cmd, out_path = _build_cmd(f)
            if cmd is None:
                win.after(0, lambda: _update_status(i, 'Skipped'))
                return True

            _log(f"  {' '.join(cmd)}", 'INFO')

            import time as _time
            import re as _re
            file_start_time = _time.monotonic()
            duration = f.get('duration') or 0
            last_update = [0.0]  # throttle UI updates

            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1)

            # ffmpeg outputs progress with \r (carriage return), not \n
            # Read character by character and split on \r or \n
            line_buf = []
            while True:
                ch = proc.stdout.read(1)
                if not ch:
                    break
                if ch in ('\r', '\n'):
                    line = ''.join(line_buf).strip()
                    line_buf = []
                    if not line:
                        continue

                    time_match = _re.search(r'time=(\d+:\d+:\d+\.\d+)', line)
                    speed_match = _re.search(r'speed=\s*([\d.]+)x', line)

                    if time_match and duration > 0:
                        now = _time.monotonic()
                        if now - last_update[0] < 0.3:
                            continue  # throttle to ~3 updates/sec
                        last_update[0] = now

                        current_time = _parse_ffmpeg_time(time_match.group(1))
                        pct = min(99.9, (current_time / duration) * 100)
                        remaining = duration - current_time

                        eta_str = ''
                        if speed_match:
                            speed = float(speed_match.group(1))
                            if speed > 0:
                                eta_secs = remaining / speed
                                if eta_secs >= 3600:
                                    eta_str = f"{int(eta_secs // 3600)}h {int((eta_secs % 3600) // 60)}m left"
                                elif eta_secs >= 60:
                                    eta_str = f"{int(eta_secs // 60)}m {int(eta_secs % 60)}s left"
                                else:
                                    eta_str = f"{int(eta_secs)}s left"

                        status_text = f"{pct:.0f}%"
                        if eta_str:
                            status_text += f" ({eta_str})"
                        label_text = f"File {i + 1}/{len(files)}: {pct:.0f}%"
                        if eta_str:
                            label_text += f" \u2014 {eta_str}"

                        _update_progress(pct, label_text)
                        win.after(0, lambda s=status_text: _update_status(i, s))
                else:
                    line_buf.append(ch)

            proc.wait()
            elapsed = _time.monotonic() - file_start_time

            if proc.returncode == 0 and os.path.isfile(out_path):
                size = os.path.getsize(out_path)
                if size > 0:
                    if size >= 1_073_741_824:
                        sz = f"{size / 1_073_741_824:.1f} GB"
                    else:
                        sz = f"{size / 1_048_576:.0f} MB"
                    # Format elapsed time
                    if elapsed >= 3600:
                        elapsed_str = f"{int(elapsed // 3600)}h {int((elapsed % 3600) // 60)}m {int(elapsed % 60)}s"
                    elif elapsed >= 60:
                        elapsed_str = f"{int(elapsed // 60)}m {int(elapsed % 60)}s"
                    else:
                        elapsed_str = f"{int(elapsed)}s"
                    _update_progress(100.0, f"File {i + 1}/{len(files)}: Done in {elapsed_str}")
                    win.after(0, lambda: _update_status(i, f'Done ({sz})'))
                    _log(f"  Done: {os.path.basename(out_path)} ({sz}, {elapsed_str})", 'SUCCESS')

                    # In-place: replace original
                    if opt_output_mode.get() == 'inplace':
                        try:
                            os.replace(out_path, f['path'])
                            _log(f"  Replaced original", 'INFO')
                        except OSError as e:
                            _log(f"  Could not replace original: {e}", 'WARNING')
                    return True
                else:
                    os.remove(out_path)
                    win.after(0, lambda: _update_status(i, 'Failed'))
                    _log(f"  Failed: empty output", 'ERROR')
                    return False
            else:
                win.after(0, lambda: _update_status(i, 'Failed'))
                _log(f"  Failed: ffmpeg returned {proc.returncode}", 'ERROR')
                if os.path.isfile(out_path):
                    try:
                        os.remove(out_path)
                    except OSError:
                        pass
                return False

        except Exception as e:
            win.after(0, lambda: _update_status(i, 'Error'))
            _log(f"  Error: {e}", 'ERROR')
            return False

    def _start_processing():
        if processing[0]:
            return
        if not files:
            messagebox.showinfo("Video Scaler", "Add files first.", parent=win)
            return
        if _get_target(files[0]) is None:
            messagebox.showinfo("Video Scaler",
                                "Resolution is set to 'Original' — nothing to scale.",
                                parent=win)
            return
        if opt_output_mode.get() == 'folder' and not opt_output_folder.get():
            messagebox.showinfo("Video Scaler",
                                "Please select an output folder.", parent=win)
            return

        processing[0] = True
        stop_flag[0] = False
        process_btn.configure(state='disabled')
        stop_btn.configure(state='normal')

        def _run():
            total = len(files)
            done = 0
            failed = 0
            _log(f"Processing {total} file(s)...", 'INFO')

            for i, f in enumerate(files):
                if stop_flag[0]:
                    _log("Stopped by user.", 'WARNING')
                    break
                if _process_one(i, f):
                    done += 1
                else:
                    failed += 1

            _log(f"Complete: {done} done, {failed} failed, "
                 f"{total - done - failed} skipped", 'INFO')
            _update_progress(100.0 if done > 0 else 0.0,
                            f"Done: {done}/{total} files")
            processing[0] = False
            win.after(0, lambda: process_btn.configure(state='normal'))
            win.after(0, lambda: stop_btn.configure(state='disabled'))

        t = threading.Thread(target=_run, daemon=True)
        t.start()

    def _stop():
        stop_flag[0] = True

    # ── Drag and drop ──
    if HAS_DND:
        try:
            win.drop_target_register(DND_FILES)
            def _on_drop(event):
                raw = event.data
                paths = []
                if 'file://' in raw:
                    from urllib.parse import unquote, urlparse
                    for token in raw.split():
                        token = token.strip()
                        if token.startswith('file://'):
                            parsed = urlparse(token)
                            paths.append(unquote(parsed.path))
                elif raw.startswith('{') and raw.endswith('}'):
                    paths = [raw[1:-1]]
                else:
                    paths = raw.split()

                added = 0
                for p in paths:
                    p = p.strip()
                    if not p or p.startswith('.'):
                        continue
                    if os.path.isfile(p) and Path(p).suffix.lower() in VIDEO_EXTENSIONS:
                        _add_one_file(p)
                        added += 1
                    elif os.path.isdir(p):
                        for root_dir, dirs, fnames in os.walk(p):
                            dirs[:] = [d for d in dirs if not d.startswith('.')]
                            for fn in sorted(fnames):
                                if fn.startswith('.'):
                                    continue
                                if Path(fn).suffix.lower() in VIDEO_EXTENSIONS:
                                    _add_one_file(os.path.join(root_dir, fn))
                                    added += 1
                if added:
                    _rebuild_tree()
                    _log(f"Added {added} file(s) via drag-and-drop", 'INFO')
            win.dnd_bind('<<Drop>>', _on_drop)
        except Exception:
            pass

    _log("Video Scaler ready -- add files and select a target resolution", 'INFO')
    _log("Tip: drag and drop video files onto this window", 'INFO')


# ═══════════════════════════════════════════════════════════════════
# Standalone launcher
# ═══════════════════════════════════════════════════════════════════

def main():
    """Standalone entry point for the Video Scaler."""
    from .standalone import create_standalone_root

    root, app = create_standalone_root(
        title="Video Scaler",
        geometry="920x750",
        minsize=(750, 550),
    )
    app._standalone_mode = True
    open_video_scaler(app)
    root.mainloop()


if __name__ == '__main__':
    main()
