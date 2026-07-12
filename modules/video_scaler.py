"""
Docflix Media Suite — Docflix Media Rescale

Standalone tool for batch resizing/scaling video files.
Supports CPU and GPU-accelerated scaling (NVIDIA NVENC,
Intel QSV, AMD VAAPI) with aspect ratio preservation.
"""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import tkinter as tk
from tkinter import ttk, messagebox

from .constants import VIDEO_EXTENSIONS, GPU_BACKENDS, VIDEO_CODEC_MAP
from .utils import scaled_geometry, scaled_minsize, ask_open_files, ask_directory

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
    """Probe a video file and return (width, height, duration_secs, codec_name, hdr_format) or Nones.

    Uses a 1-frame decode to get the actual content dimensions (which may
    differ from container dimensions for letterboxed/cropped content).
    Falls back to ffprobe stream metadata if decode probe fails.
    hdr_format is a string like 'HDR10', 'HLG', 'DoVi', or '' for SDR.
    """
    width, height, duration, codec, hdr_format = None, None, None, None, ''

    # Get duration, codec, and HDR info from ffprobe (fast metadata read)
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
                # Detect HDR from color transfer characteristics
                transfer = s.get('color_transfer', '')
                primaries = s.get('color_primaries', '')
                pix_fmt = s.get('pix_fmt', '')
                if transfer == 'smpte2084':
                    hdr_format = 'HDR10'
                elif transfer == 'arib-std-b67':
                    hdr_format = 'HLG'
                elif 'bt2020' in primaries and '10' in pix_fmt:
                    hdr_format = 'HDR'
            dur = fmt.get('duration')
            duration = float(dur) if dur else None
    except Exception:
        pass

    # Check for Dolby Vision via side data (overrides HDR10 label)
    if hdr_format:
        try:
            cmd_sd = [
                'ffprobe', '-v', 'quiet', '-print_format', 'json',
                '-select_streams', 'v:0', '-read_intervals', '%+#1',
                '-show_frames', '-show_entries',
                'frame=side_data_list', filepath
            ]
            r2 = subprocess.run(cmd_sd, capture_output=True, text=True, timeout=15)
            if r2.returncode == 0:
                sd_data = json.loads(r2.stdout)
                for frame in sd_data.get('frames', []):
                    for sd in frame.get('side_data_list', []):
                        sd_type = sd.get('side_data_type', '')
                        if 'Dolby Vision' in sd_type or 'DOVI' in sd_type:
                            hdr_format = 'DoVi'
                            break
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

    return width, height, duration, codec, hdr_format


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
    """Open the Docflix Media Rescale tool window."""

    win = tk.Toplevel(app.root)
    win.withdraw()
    win.title("Docflix Media Rescale")
    geom_str = scaled_geometry(win, 920, 750)
    win.geometry(geom_str)
    win.minsize(*scaled_minsize(win, 750, 550))
    win.update_idletasks()
    try:
        import re as _re
        gm = _re.match(r'(\d+)x(\d+)', geom_str)
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
    files = []          # list of dicts: path, name, width, height, duration, codec, target, status
    processing = [False]
    stop_flag = [False]
    current_proc = [None]  # hold reference to running ffmpeg subprocess

    # ── Load saved preferences ──
    _sp = getattr(app, '_scaler_prefs', {})

    # ── AI upscaler import ──
    from . import ai_upscaler

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
    opt_hdr_to_sdr   = tk.BooleanVar(value=_sp.get('hdr_to_sdr', False))
    opt_upscale_method = tk.StringVar(value=_sp.get('upscale_method', 'Standard (ffmpeg)'))
    opt_ai_model     = tk.StringVar(value=_sp.get('ai_model', ai_upscaler.DEFAULT_MODEL))
    opt_ai_gpu       = tk.StringVar(value=_sp.get('ai_gpu', 'Auto (all GPUs)'))
    opt_ai_preview_start = tk.StringVar(value=_sp.get('ai_preview_start', '120'))
    opt_ai_tta       = tk.BooleanVar(value=_sp.get('ai_tta', False))

    # Real-ESRGAN GPU choices — label -> value accepted by ai_upscaler.normalize_gpu_ids()
    _ai_gpu_choices = {'Auto (all GPUs)': 'auto'}
    for _g in ai_upscaler.detect_gpus():
        _ai_gpu_choices[f"GPU {_g['index']}"] = str(_g['index'])
    _ai_gpu_choices['CPU (slow)'] = 'cpu'

    def _resolve_ai_gpu():
        """Selected GPU label -> value for AIUpscaleJob (defaults to 'auto')."""
        return _ai_gpu_choices.get(opt_ai_gpu.get(), 'auto')

    # Detect GPU backends
    gpu_backends = _detect_gpu_backends_quick()
    encoder_labels = ['CPU']
    encoder_ids = {'CPU': 'cpu'}
    for bid, label in gpu_backends.items():
        encoder_labels.append(label)
        encoder_ids[label] = bid

    # Default to first available GPU if no saved preference
    if not _sp.get('encoder') and gpu_backends:
        first_gpu_label = next(iter(gpu_backends.values()))
        opt_encoder.set(first_gpu_label)

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
        paths = ask_open_files(
            parent=win, title="Select Video Files",
            filetypes=[("Video files", "*.mkv *.mp4 *.avi *.mov *.wmv *.flv *.webm *.ts *.m2ts *.mts"),
                       ("All files", "*.*")])
        if paths:
            _add_files_threaded(list(paths))

    def _add_folder():
        folder = ask_directory(parent=win, title="Select Folder")
        if folder:
            collected = []
            for root_dir, dirs, fnames in os.walk(folder):
                dirs[:] = [d for d in dirs if not d.startswith('.')]
                for fn in sorted(fnames):
                    if fn.startswith('.'):
                        continue
                    if Path(fn).suffix.lower() in VIDEO_EXTENSIONS:
                        collected.append(os.path.join(root_dir, fn))
            _add_files_threaded(collected, source_label=f"from {folder}")

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

    # ── Column sorting state ──
    _sort_col = [None]
    _sort_reverse = [False]
    _col_labels = {
        '#0': 'Filename', 'source_res': 'Source',
        'target_res': 'Target', 'size': 'Size', 'status': 'Status'
    }

    def _sort_by_column(col):
        if _sort_col[0] == col:
            _sort_reverse[0] = not _sort_reverse[0]
        else:
            _sort_col[0] = col
            _sort_reverse[0] = False

        def sort_key(f):
            if col == '#0':
                return f.get('name', '').lower()
            elif col == 'source_res':
                return (f.get('width') or 0) * (f.get('height') or 0)
            elif col == 'target_res':
                return (f.get('width') or 0) * (f.get('height') or 0)
            elif col == 'size':
                try:
                    return os.path.getsize(f['path'])
                except Exception:
                    return 0
            elif col == 'status':
                return f.get('status', '').lower()
            return ''

        files.sort(key=sort_key, reverse=_sort_reverse[0])
        _rebuild_tree()

        # Update headers with sort arrow
        arrow = ' ▼' if _sort_reverse[0] else ' ▲'
        for c, lbl in _col_labels.items():
            indicator = arrow if c == col else ''
            tree.heading(c, text=lbl + indicator)

    tree.heading('source_res', text='Source',  command=lambda: _sort_by_column('source_res'))
    tree.heading('target_res', text='Target',  command=lambda: _sort_by_column('target_res'))
    tree.heading('size', text='Size',          command=lambda: _sort_by_column('size'))
    tree.heading('status', text='Status',      command=lambda: _sort_by_column('status'))

    tree.column('source_res', width=130, minwidth=100)
    tree.column('target_res', width=100, minwidth=80)
    tree.column('size', width=80, minwidth=60)
    tree.column('status', width=100, minwidth=80)

    # Add filename as the tree item text via #0
    tree['displaycolumns'] = ('source_res', 'target_res', 'size', 'status')
    tree['show'] = ('tree', 'headings')
    tree.column('#0', width=300, minwidth=200)
    tree.heading('#0', text='Filename', command=lambda: _sort_by_column('#0'))

    tree_scroll = ttk.Scrollbar(tree_frame, orient='vertical', command=tree.yview)
    tree.configure(yscrollcommand=tree_scroll.set)
    tree.grid(row=0, column=0, sticky='nsew')
    tree_scroll.grid(row=0, column=1, sticky='ns')

    # ── Shift+Arrow multi-select ──
    def _shift_arrow(evt, direction):
        items = tree.get_children()
        if not items:
            return 'break'
        focus = tree.focus()
        if not focus:
            return 'break'
        idx = list(items).index(focus)
        new_idx = idx + direction
        if new_idx < 0 or new_idx >= len(items):
            return 'break'
        new_item = items[new_idx]
        tree.focus(new_item)
        tree.see(new_item)
        tree.selection_add(new_item)
        return 'break'

    tree.bind('<Shift-Up>',   lambda e: _shift_arrow(e, -1))
    tree.bind('<Shift-Down>', lambda e: _shift_arrow(e, 1))

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
    # Don't override — opt_encoder already has the right default (GPU if available, CPU otherwise)
    enc_combo.pack(side='left', padx=(0, 6))

    preset_label = ttk.Label(row1, text="Preset:")
    preset_label.pack(side='left', padx=(0, 2))
    preset_combo = ttk.Combobox(row1, textvariable=opt_preset, width=10, state='readonly')
    preset_combo.pack(side='left')

    def _on_encoder_change(event=None):
        enc_label = opt_encoder.get()
        bid = encoder_ids.get(enc_label, 'cpu')
        codec_name = opt_codec.get()
        if bid == 'cpu':
            info = VIDEO_CODEC_MAP.get(codec_name, VIDEO_CODEC_MAP['H.265 / HEVC'])
            # Presets — show and populate for CPU
            preset_label.pack(side='left', padx=(0, 2))
            preset_combo.pack(side='left')
            preset_combo['values'] = info['cpu_presets']
            if opt_preset.get() not in info['cpu_presets']:
                opt_preset.set(info.get('cpu_preset_default', 'medium'))
            # Quality label
            crf_label.configure(text="CRF:")
        else:
            backend = GPU_BACKENDS.get(bid, {})
            presets = backend.get('presets', ())
            # Presets — hide if backend has none (e.g. VAAPI)
            if presets:
                preset_label.pack(side='left', padx=(0, 2))
                preset_combo.pack(side='left')
                preset_combo['values'] = presets
                if opt_preset.get() not in presets:
                    opt_preset.set(backend.get('preset_default', presets[0] if presets else ''))
            else:
                preset_label.pack_forget()
                preset_combo.pack_forget()
            # Quality label — use backend-appropriate name
            cq_flag = backend.get('cq_flag', '')
            if cq_flag == '-cq':
                crf_label.configure(text="CQ:")
            elif cq_flag == '-qp':
                crf_label.configure(text="QP:")
            elif cq_flag == '-global_quality':
                crf_label.configure(text="Quality:")
            else:
                crf_label.configure(text="CQ:")
    enc_combo.bind('<<ComboboxSelected>>', _on_encoder_change)

    # Row 1b: Upscale Method (only visible when upscaling)
    row1b = ttk.Frame(settings_frame)
    # Initially hidden — shown/hidden by _on_method_change

    upscale_methods = ['Standard (ffmpeg)', 'AI (Real-ESRGAN)']
    ttk.Label(row1b, text="Upscale Method:").pack(side='left', padx=(0, 4))
    method_combo = ttk.Combobox(row1b, textvariable=opt_upscale_method,
                                 values=upscale_methods, width=18, state='readonly')
    method_combo.pack(side='left', padx=(0, 8))

    ttk.Separator(row1b, orient='vertical').pack(side='left', fill='y', padx=4)

    ai_model_label = ttk.Label(row1b, text="AI Model:")
    ai_model_label.pack(side='left', padx=(8, 4))
    ai_model_combo = ttk.Combobox(row1b, textvariable=opt_ai_model,
                                    values=list(ai_upscaler.MODELS.keys()),
                                    width=20, state='readonly')
    ai_model_combo.pack(side='left', padx=(0, 8))

    ai_gpu_label = ttk.Label(row1b, text="GPU:")
    ai_gpu_label.pack(side='left', padx=(8, 4))
    ai_gpu_combo = ttk.Combobox(row1b, textvariable=opt_ai_gpu,
                                 values=list(_ai_gpu_choices.keys()),
                                 width=16, state='readonly')
    ai_gpu_combo.pack(side='left', padx=(0, 8))

    # Quick 30s side-by-side preview (original | AI-upscaled)
    ai_preview_frame = ttk.Frame(row1b)
    ai_preview_frame.pack(side='left', padx=(6, 4))
    ai_preview_btn = ttk.Button(ai_preview_frame, text="👁 Preview 30s")
    ai_preview_btn.pack(side='left', padx=(0, 2))
    ttk.Label(ai_preview_frame, text="@").pack(side='left')
    ttk.Entry(ai_preview_frame, textvariable=opt_ai_preview_start,
              width=4).pack(side='left', padx=(2, 1))
    ttk.Label(ai_preview_frame, text="s").pack(side='left')

    ai_tta_check = ttk.Checkbutton(row1b, text="Max Quality (~8× slower)",
                                   variable=opt_ai_tta)
    ai_tta_check.pack(side='left', padx=(6, 4))

    # AI status label (shows installed/not installed)
    ai_status_var = tk.StringVar(value='')
    ai_status_label = ttk.Label(row1b, textvariable=ai_status_var,
                                 font=('', 9))
    ai_status_label.pack(side='left', padx=(0, 4))

    # Download/Install button
    ai_download_btn = ttk.Button(row1b, text="Download Real-ESRGAN")
    ai_download_btn.pack(side='left', padx=(0, 4))

    def _update_ai_status():
        """Update AI status label and button visibility."""
        if ai_upscaler.is_installed():
            ver = ai_upscaler.get_version() or ''
            ai_status_var.set(f"✓ Installed ({ver})" if ver else "✓ Installed")
            ai_status_label.configure(foreground='green')
            ai_download_btn.pack_forget()
        else:
            ai_status_var.set("Not installed")
            ai_status_label.configure(foreground='red')
            ai_download_btn.pack(side='left', padx=(0, 4))

    def _download_realesrgan():
        """Download Real-ESRGAN in a background thread."""
        ai_download_btn.configure(state='disabled', text="Downloading...")

        def _worker():
            try:
                ai_upscaler.download_and_install(
                    progress_callback=lambda p, s: win.after(0, lambda: (
                        _update_progress(p, s)
                    )),
                    log_callback=lambda m, l: _log(f"  [AI] {m}", l),
                )
                win.after(0, lambda: (
                    _update_ai_status(),
                    _reset_progress(),
                    _log("Real-ESRGAN installed successfully!", 'SUCCESS'),
                ))
            except Exception as e:
                win.after(0, lambda: (
                    _log(f"Download failed: {e}", 'ERROR'),
                    ai_download_btn.configure(state='normal', text="Download Real-ESRGAN"),
                    _reset_progress(),
                ))

        threading.Thread(target=_worker, daemon=True).start()

    ai_download_btn.configure(command=_download_realesrgan)

    _preview_job = [None]  # holds the running preview AIUpscaleJob (or None)

    def _cancel_preview():
        job = _preview_job[0]
        if job is not None:
            _log("Cancelling preview…", 'INFO')
            job.cancel()

    def _preview_selected():
        """Generate a quick 30s side-by-side (original | AI-upscaled) preview of
        the selected file using the CURRENT AI settings, then open it."""
        if not ai_upscaler.is_installed():
            _log("Real-ESRGAN not installed — use the Download button first.", 'ERROR')
            return
        if not files:
            _log("Add a file first, then Preview.", 'ERROR')
            return
        items = list(tree.get_children())
        focus = tree.focus()
        idx = items.index(focus) if focus in items else 0
        f = files[idx]

        target = _get_target(f)
        target_h = target[1] if target else None

        # Encoder — mirror the full AI job build
        bid = encoder_ids.get(opt_encoder.get(), 'cpu')
        codec_name = opt_codec.get()
        codec_info = VIDEO_CODEC_MAP.get(codec_name, VIDEO_CODEC_MAP['H.265 / HEVC'])
        if bid == 'cpu':
            video_enc = codec_info['cpu_encoder']
        else:
            video_enc = (GPU_BACKENDS.get(bid, {}).get('encoders', {}).get(codec_name)
                         or codec_info['cpu_encoder'])

        try:
            start = float(opt_ai_preview_start.get())
        except (ValueError, TypeError):
            start = 120.0

        out_path = os.path.join(tempfile.gettempdir(),
                                f"docflix_preview_{Path(f['path']).stem}.mkv")

        ai_preview_btn.configure(text="✖ Cancel", command=_cancel_preview)
        _log(f"Preview: {f['name']} — 30s @ {start:.0f}s "
             f"({opt_ai_model.get()}, GPU={_resolve_ai_gpu()})", 'INFO')

        def _worker():
            ok = False
            was_cancelled = False
            try:
                job = ai_upscaler.AIUpscaleJob(
                    input_path=f['path'], output_path=out_path,
                    model_name=opt_ai_model.get(), target_height=target_h,
                    video_encoder=video_enc, crf=opt_crf.get(),
                    preset=opt_preset.get(), audio_codec=opt_audio.get(),
                    gpu_id=_resolve_ai_gpu(),
                    tta=opt_ai_tta.get(),
                    log_callback=lambda m, l: win.after(
                        0, lambda m=m, l=l: _log(f"  [preview] {m}", l)),
                    progress_callback=lambda p, s: win.after(
                        0, lambda p=p, s=s: _update_progress(p, f"Preview: {s}")),
                )
                _preview_job[0] = job
                ok = job.run_preview(start=start, duration=30.0)
                was_cancelled = job._cancelled
            except Exception as e:
                win.after(0, lambda e=e: _log(f"Preview error: {e}", 'ERROR'))
            finally:
                _preview_job[0] = None

            def _done():
                ai_preview_btn.configure(text="👁 Preview 30s", command=_preview_selected)
                _reset_progress()
                if ok and os.path.isfile(out_path):
                    _log(f"Preview ready → {out_path} (opening…)", 'SUCCESS')
                    try:
                        subprocess.Popen(['xdg-open', out_path],
                                         stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL)
                    except Exception:
                        _log("(Couldn't auto-open; file is at the path above.)", 'INFO')
                elif was_cancelled:
                    _log("Preview cancelled.", 'INFO')
                else:
                    _log("Preview failed — see the log above.", 'ERROR')
            win.after(0, _done)

        threading.Thread(target=_worker, daemon=True).start()

    ai_preview_btn.configure(command=_preview_selected)

    def _on_method_change(*args):
        """Show/hide AI-specific controls based on upscale method."""
        method = opt_upscale_method.get()
        if method == 'AI (Real-ESRGAN)':
            ai_model_label.pack(side='left', padx=(8, 4))
            ai_model_combo.pack(side='left', padx=(0, 8))
            ai_gpu_label.pack(side='left', padx=(8, 4))
            ai_gpu_combo.pack(side='left', padx=(0, 8))
            ai_preview_frame.pack(side='left', padx=(6, 4))
            ai_tta_check.pack(side='left', padx=(6, 4))
            ai_status_label.pack(side='left', padx=(0, 4))
            _update_ai_status()
            # Use lower CRF for AI upscale (more detail to preserve)
            if opt_crf.get() in ('23', '28'):
                opt_crf.set('18')
        else:
            ai_model_label.pack_forget()
            ai_model_combo.pack_forget()
            ai_gpu_label.pack_forget()
            ai_gpu_combo.pack_forget()
            ai_preview_frame.pack_forget()
            ai_tta_check.pack_forget()
            ai_status_label.pack_forget()
            ai_download_btn.pack_forget()

    method_combo.bind('<<ComboboxSelected>>', _on_method_change)

    def _check_show_method_row():
        """Show the method row when any file would be upscaled."""
        any_upscale = any(_is_upscale(f) for f in files) if files else False
        if any_upscale:
            row1b.pack(fill='x', pady=2, after=row1)
            _on_method_change()
        else:
            row1b.pack_forget()

    # Row 2: Codec, CRF, Audio, Container, Output
    row2 = ttk.Frame(settings_frame)
    row2.pack(fill='x', pady=2)

    ttk.Label(row2, text="Codec:").pack(side='left', padx=(0, 2))
    codec_combo = ttk.Combobox(row2, textvariable=opt_codec,
                                values=['H.265 / HEVC', 'H.264 / AVC', 'AV1'],
                                width=14, state='readonly')
    codec_combo.pack(side='left', padx=(0, 6))
    codec_combo.bind('<<ComboboxSelected>>', _on_encoder_change)

    crf_label = ttk.Label(row2, text="CRF:")
    crf_label.pack(side='left', padx=(0, 2))
    crf_entry = ttk.Entry(row2, textvariable=opt_crf, width=4)
    crf_entry.pack(side='left', padx=(0, 6))

    ttk.Label(row2, text="Audio:").pack(side='left', padx=(0, 2))
    ttk.Combobox(row2, textvariable=opt_audio,
                 values=['copy', 'aac', 'ac3', 'eac3', 'mp3', 'opus', 'flac'],
                 width=6, state='readonly').pack(side='left', padx=(0, 6))

    ttk.Label(row2, text="Container:").pack(side='left', padx=(0, 2))
    ttk.Combobox(row2, textvariable=opt_container,
                 values=['.mkv', '.mp4'], width=5, state='readonly').pack(side='left', padx=(0, 6))

    ttk.Separator(row2, orient='vertical').pack(side='left', fill='y', padx=8)
    ttk.Checkbutton(row2, text="Convert HDR → SDR",
                    variable=opt_hdr_to_sdr).pack(side='left', padx=(0, 6))

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
                   ask_directory(parent=win, title="Select Output Folder")
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
    progress_bar.grid(row=0, column=0, columnspan=2, sticky='ew')

    # Status text on its own full-width row, left-aligned — never truncates and
    # grows with the window (was width=40/anchor='e' in a fixed column).
    progress_label = ttk.Label(progress_frame, text="", anchor='w')
    progress_label.grid(row=1, column=0, columnspan=2, sticky='ew', pady=(2, 0))

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
        """Save Docflix Media Rescale settings to preferences."""
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
            'hdr_to_sdr':    opt_hdr_to_sdr.get(),
            'upscale_method': opt_upscale_method.get(),
            'ai_model':      opt_ai_model.get(),
            'ai_gpu':        opt_ai_gpu.get(),
            'ai_preview_start': opt_ai_preview_start.get(),
            'ai_tta':        opt_ai_tta.get(),
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

    _scanning = [False]  # mutable flag to prevent overlapping scans

    def _add_one_file(filepath):
        """Add a single file to the list (called from any thread)."""
        # Skip duplicates
        for f in files:
            if f['path'] == filepath:
                return False
        if Path(filepath).suffix.lower() not in VIDEO_EXTENSIONS:
            return False

        w, h, dur, codec, hdr_fmt = _probe_video_info(filepath)
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
            'hdr': hdr_fmt,
            'size_str': size_str,
            'status': '',
        })
        return True

    def _add_files_threaded(file_paths, source_label=""):
        """Probe and add files in a background thread with progress feedback."""
        if _scanning[0]:
            _log("Scan already in progress — please wait", 'WARNING')
            return
        _scanning[0] = True
        total = len(file_paths)
        if total == 0:
            _scanning[0] = False
            return

        def _worker():
            added = 0
            import time
            t0 = time.monotonic()
            for i, p in enumerate(file_paths):
                pct = (i / total) * 100
                elapsed = time.monotonic() - t0
                if i > 0:
                    per_file = elapsed / i
                    eta = per_file * (total - i)
                    eta_str = f"Scanning {i + 1}/{total} — ETA {int(eta)}s"
                else:
                    eta_str = f"Scanning 1/{total}…"
                _update_progress(pct, eta_str)
                if _add_one_file(p):
                    added += 1
            elapsed = time.monotonic() - t0
            label = f" {source_label}" if source_label else ""
            _log(f"Added {added} file(s){label} in {elapsed:.1f}s", 'INFO')
            def _finish():
                _rebuild_tree()
                _reset_progress()
                _scanning[0] = False
            win.after(0, _finish)

        threading.Thread(target=_worker, daemon=True).start()

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
            if f.get('hdr'):
                src += f" {f['hdr']}"
            tgt = _target_str(f)
            warn = ' (upscale)' if _is_upscale(f) else ''
            if warn and opt_upscale_method.get() == 'AI (Real-ESRGAN)':
                warn = ' (AI upscale)'
            iid = tree.insert('', 'end', text=f['name'],
                              values=(src, tgt + warn, f['size_str'], f['status']))
        _check_show_method_row()

    def _update_targets():
        """Update target column for all files."""
        items = tree.get_children()
        for i, iid in enumerate(items):
            if i < len(files):
                f = files[i]
                tgt = _target_str(f)
                warn = ' (upscale)' if _is_upscale(f) else ''
                if warn and opt_upscale_method.get() == 'AI (Real-ESRGAN)':
                    warn = ' (AI upscale)'
                tree.set(iid, 'target_res', tgt + warn)
        _check_show_method_row()

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

        # HDR → SDR tone mapping requested and file is actually HDR?
        do_tonemap = opt_hdr_to_sdr.get() and bool(f.get('hdr'))

        # Build command
        cmd = ['ffmpeg', '-y']

        # HW accel for GPU decode — disabled when tone mapping because
        # the zscale/tonemap filters require CPU (system memory) frames
        if bid != 'cpu' and bid in GPU_BACKENDS and not do_tonemap:
            backend_info = GPU_BACKENDS[bid]
            cmd.extend(backend_info['hwaccel'])

        cmd.extend(['-i', input_path])

        # Map all streams — without explicit -map, ffmpeg only picks
        # one "best" stream per type (stripping extra audio/subtitle tracks)
        cmd.extend(['-map', '0:v:0?',   # first video stream
                    '-map', '0:a?',      # all audio streams
                    '-map', '0:s?'])      # all subtitle streams

        # Scale filter — uses explicit dimensions calculated from actual
        # decoded content size, so it works correctly with CUDA hwaccel
        # (which may deliver padded frames for cropped H.264 content)
        scale_vf = _build_scale_filter(target_w, target_h, bid if bid != 'cpu' and not do_tonemap else None)

        if do_tonemap:
            # HDR → SDR tone mapping filter chain (CPU filters):
            #   zscale  — convert to linear light in BT.2020
            #   tonemap — compress HDR luminance to SDR range
            #   zscale  — convert to BT.709 (standard HD color space)
            #   format  — convert to 8-bit YUV for SDR output
            tonemap_chain = (
                'zscale=t=linear:npl=100,'
                'format=gbrpf32le,'
                'tonemap=hable:desat=0,'
                'zscale=p=bt709:t=bt709:m=bt709:r=tv,'
                'format=yuv420p'
            )
            if scale_vf:
                cmd.extend(['-vf', f'{scale_vf},{tonemap_chain},setsar=1:1'])
            else:
                cmd.extend(['-vf', f'{tonemap_chain},setsar=1:1'])
        elif scale_vf:
            if bid != 'cpu' and bid in GPU_BACKENDS:
                # GPU path: keep frames in GPU memory for the encoder.
                # Use scale_cuda only; SAR is set via encoder option below.
                cmd.extend(['-vf', scale_vf])
            else:
                # CPU path: straightforward filter chain with setsar
                cmd.extend(['-vf', f'{scale_vf},setsar=1:1'])

        # Video encoder
        cmd.extend(['-c:v', video_enc])

        # Force square pixels (SAR 1:1) for GPU path — can't use setsar
        # filter on hardware frames, so set it as an encoder/muxer option.
        # When tone mapping, filters run on CPU so setsar is already applied.
        if bid != 'cpu' and bid in GPU_BACKENDS and not do_tonemap:
            cmd.extend(['-sar', '1:1'])

        # When tone mapping HDR → SDR, explicitly tag the output as BT.709
        # so players don't misinterpret the color space
        if do_tonemap:
            cmd.extend(['-colorspace', 'bt709', '-color_primaries', 'bt709',
                        '-color_trc', 'bt709'])

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

        use_ai = (_is_upscale(f)
                  and opt_upscale_method.get() == 'AI (Real-ESRGAN)')

        if _is_upscale(f) and not use_ai:
            _log(f"  Warning: upscaling {f['name']} ({f['height']}p -> {target[1]}p)", 'WARNING')

        win.after(0, lambda: _update_status(i, 'Processing...'))

        if use_ai:
            return _process_one_ai(i, f, target)

        hdr_note = f" (HDR → SDR)" if opt_hdr_to_sdr.get() and f.get('hdr') else ''
        _log(f"  Scaling: {f['name']} -> {_target_str(f)}{hdr_note}", 'INFO')
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
            current_proc[0] = proc

            # ffmpeg outputs progress with \r (carriage return), not \n
            # Read character by character and split on \r or \n
            line_buf = []
            while True:
                if stop_flag[0]:
                    try:
                        proc.kill()
                    except OSError:
                        pass
                    break
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
            current_proc[0] = None
            elapsed = _time.monotonic() - file_start_time

            # If stopped by user, clean up partial file and return
            if stop_flag[0]:
                if os.path.isfile(out_path):
                    try:
                        os.remove(out_path)
                    except OSError:
                        pass
                _log(f"  Stopped: {f['name']}", 'WARNING')
                win.after(0, lambda: _update_status(i, 'Stopped'))
                return False

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

    def _process_one_ai(i, f, target):
        """Process a single file using AI upscaling. Returns True on success."""
        import time as _time

        if not ai_upscaler.is_installed():
            _log("  Real-ESRGAN not installed — use the Download button", 'ERROR')
            win.after(0, lambda: _update_status(i, 'Error'))
            return False

        _, target_h = target
        model_name = opt_ai_model.get()
        _log(f"  AI upscaling: {f['name']} -> {_target_str(f)} ({model_name})", 'INFO')
        _update_progress(0.0, f"File {i + 1}/{len(files)}: AI upscale — {f['name']}")

        # Build output path
        input_path = f['path']
        base = Path(input_path).stem
        ext = opt_container.get()
        res_tag = f"{target_h}p-ai"
        if opt_output_mode.get() == 'folder' and opt_output_folder.get():
            out_dir = opt_output_folder.get()
            os.makedirs(out_dir, exist_ok=True)
            out_path = os.path.join(out_dir, f"{base}-{res_tag}{ext}")
        else:
            out_path = str(Path(input_path).parent / f"{base}-{res_tag}{ext}")

        # Determine encoder
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

        file_start_time = _time.monotonic()

        job = ai_upscaler.AIUpscaleJob(
            input_path=input_path,
            output_path=out_path,
            model_name=model_name,
            target_height=target_h,
            video_encoder=video_enc,
            crf=opt_crf.get(),
            preset=opt_preset.get(),
            audio_codec=opt_audio.get(),
            gpu_id=_resolve_ai_gpu(),
            tta=opt_ai_tta.get(),
            log_callback=lambda m, l: _log(f"  {m}", l),
            progress_callback=lambda p, s: (
                _update_progress(p, f"File {i + 1}/{len(files)}: {s}"),
                win.after(0, lambda s2=f"{p:.0f}%": _update_status(i, s2))
                if not stop_flag[0] else None
            ),
        )
        current_proc[0] = job  # store for cancel

        # Check for stop
        def _check_stop():
            if stop_flag[0]:
                job.cancel()
        stop_check_id = win.after(500, _check_stop)

        try:
            success = job.run()
        finally:
            try:
                win.after_cancel(stop_check_id)
            except Exception:
                pass
            current_proc[0] = None

        elapsed = _time.monotonic() - file_start_time

        if stop_flag[0]:
            if os.path.isfile(out_path):
                try:
                    os.remove(out_path)
                except OSError:
                    pass
            _log(f"  Stopped: {f['name']}", 'WARNING')
            win.after(0, lambda: _update_status(i, 'Stopped'))
            return False

        if success and os.path.isfile(out_path):
            size = os.path.getsize(out_path)
            if size > 0:
                if size >= 1_073_741_824:
                    sz = f"{size / 1_073_741_824:.1f} GB"
                else:
                    sz = f"{size / 1_048_576:.0f} MB"
                if elapsed >= 3600:
                    elapsed_str = f"{int(elapsed // 3600)}h {int((elapsed % 3600) // 60)}m"
                elif elapsed >= 60:
                    elapsed_str = f"{int(elapsed // 60)}m {int(elapsed % 60)}s"
                else:
                    elapsed_str = f"{int(elapsed)}s"
                _update_progress(100.0, f"File {i + 1}/{len(files)}: Done in {elapsed_str}")
                win.after(0, lambda: _update_status(i, f'Done ({sz})'))
                _log(f"  Done: {os.path.basename(out_path)} ({sz}, {elapsed_str})", 'SUCCESS')

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
        _log(f"  AI upscale failed for: {f['name']}", 'ERROR')
        return False

    def _start_processing():
        if processing[0]:
            return
        if not files:
            messagebox.showinfo("Docflix Media Rescale", "Add files first.", parent=win)
            return
        if _get_target(files[0]) is None:
            messagebox.showinfo("Docflix Media Rescale",
                                "Resolution is set to 'Original' — nothing to scale.",
                                parent=win)
            return
        if opt_output_mode.get() == 'folder' and not opt_output_folder.get():
            messagebox.showinfo("Docflix Media Rescale",
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
        # Kill running process immediately
        proc = current_proc[0]
        if proc:
            try:
                if isinstance(proc, ai_upscaler.AIUpscaleJob):
                    proc.cancel()
                else:
                    proc.kill()
            except OSError:
                pass

    # ── Drag and drop ──
    if HAS_DND:
        try:
            def _parse_drop_paths(raw):
                """Parse file paths from drag-and-drop event data."""
                paths = []
                if 'file://' in raw:
                    from urllib.parse import unquote, urlparse
                    # file:// URIs may be separated by \r\n or \n, not just spaces
                    import re as _dnd_re
                    uris = _dnd_re.findall(r'file://\S+', raw)
                    for uri in uris:
                        parsed = urlparse(uri)
                        paths.append(unquote(parsed.path))
                elif raw.startswith('{'):
                    # Tk wraps paths with spaces in braces: {/path/with spaces}
                    import re as _dnd_re
                    paths = _dnd_re.findall(r'\{([^}]+)\}', raw)
                    # Also grab any non-braced tokens
                    remainder = _dnd_re.sub(r'\{[^}]+\}', '', raw).strip()
                    if remainder:
                        paths.extend(remainder.split())
                else:
                    paths = raw.split()
                return paths

            def _on_drop(event):
                raw = event.data
                paths = _parse_drop_paths(raw)

                collected = []
                for p in paths:
                    p = p.strip()
                    if not p or os.path.basename(p).startswith('.'):
                        continue
                    if os.path.isfile(p) and Path(p).suffix.lower() in VIDEO_EXTENSIONS:
                        collected.append(p)
                    elif os.path.isdir(p):
                        for root_dir, dirs, fnames in os.walk(p):
                            dirs[:] = [d for d in dirs if not d.startswith('.')]
                            for fn in sorted(fnames):
                                if fn.startswith('.'):
                                    continue
                                if Path(fn).suffix.lower() in VIDEO_EXTENSIONS:
                                    collected.append(os.path.join(root_dir, fn))
                if collected:
                    _add_files_threaded(collected, source_label="via drag-and-drop")

            # Register DnD on both the tree widget and the window for broad coverage
            tree.drop_target_register(DND_FILES)
            tree.dnd_bind('<<Drop>>', _on_drop)
            win.drop_target_register(DND_FILES)
            win.dnd_bind('<<Drop>>', _on_drop)
        except Exception:
            pass

    # Force Tk to calculate geometry and render all widgets — prevents
    # invisible/blank controls on high-DPI displays until mouse-over
    win.update_idletasks()

    _log("Docflix Media Rescale ready -- add files and select a target resolution", 'INFO')
    _log("Tip: drag and drop video files onto this window", 'INFO')


# ═══════════════════════════════════════════════════════════════════
# Standalone launcher
# ═══════════════════════════════════════════════════════════════════

def main():
    """Standalone entry point for the Docflix Media Rescale."""
    from .standalone import create_standalone_root

    root, app = create_standalone_root(
        title="Docflix Media Rescale",
        geometry="920x750",
        minsize=(750, 550),
    )
    app._standalone_mode = True
    open_video_scaler(app)
    root.mainloop()


if __name__ == '__main__':
    main()
