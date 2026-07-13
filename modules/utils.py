"""
Docflix Media Suite — Shared Utility Functions

Format helpers, ffprobe wrappers, tooltip widget, and other
utilities used across multiple modules.
"""

import json
import os
import shutil
import subprocess
import tkinter as tk
from tkinter import filedialog


# ═══════════════════════════════════════════════════════════════════
# Formatting helpers
# ═══════════════════════════════════════════════════════════════════

def format_size(size_bytes):
    """Format file size in human-readable format."""
    if size_bytes == 0:
        return "0 B"
    units = ['B', 'KB', 'MB', 'GB', 'TB']
    unit_index = 0
    size = float(size_bytes)
    while size >= 1024 and unit_index < len(units) - 1:
        size /= 1024
        unit_index += 1
    return f"{size:.1f} {units[unit_index]}"


def format_duration(seconds):
    """Format duration as HH:MM:SS or MM:SS."""
    if seconds is None:
        return '?'
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def format_time(seconds):
    """Format seconds into human-readable time string."""
    if seconds < 60:
        return f"{int(seconds)}s"
    elif seconds < 3600:
        minutes = int(seconds // 60)
        secs = int(seconds % 60)
        return f"{minutes}m {secs}s"
    else:
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        return f"{hours}h {minutes}m {secs}s"


# ═══════════════════════════════════════════════════════════════════
# ffprobe wrappers
# ═══════════════════════════════════════════════════════════════════

def get_video_duration(filepath):
    """Get video duration in seconds using ffprobe."""
    try:
        cmd = [
            'ffprobe', '-v', 'error',
            '-show_entries', 'format=duration',
            '-of', 'default=noprint_wrappers=1:nokey=1',
            filepath,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=30)
        if result.returncode == 0:
            return float(result.stdout.strip())
    except Exception:
        pass
    return None


def _probe_bitmap_empty(filepath, stream_index, threshold=20):
    """Fast check whether a bitmap subtitle stream is empty.

    DVB subtitle tracks in HDTV recordings often contain only
    keepalive/clear segments (≤14 bytes each) with no actual subtitle
    graphics.  MKV muxer statistics (NUMBER_OF_FRAMES/NUMBER_OF_BYTES)
    are frequently absent for these tracks, so the normal empty
    detection misses them.

    Probes the first 200 packets in the stream — if ALL are ≤
    *threshold* bytes, the track is considered empty.  Returns True
    (empty) or False (has content).
    """
    try:
        cmd = [
            'ffprobe', '-v', 'quiet', '-print_format', 'json',
            '-show_entries', 'packet=size',
            '-select_streams', str(stream_index),
            '-read_intervals', '%+120',    # first 120s is enough
            filepath,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=30)
        if result.returncode != 0:
            return False
        data = json.loads(result.stdout)
        packets = data.get('packets', [])
        if not packets:
            return True   # no packets at all → empty
        return all(int(p.get('size', 0)) <= threshold for p in packets)
    except Exception:
        return False


def get_subtitle_streams(filepath):
    """Return a list of subtitle stream dicts for the given file.

    Each dict has: index, codec_name, language, title, forced, sdh,
    default, empty, num_frames.
    """
    try:
        cmd = [
            'ffprobe', '-v', 'quiet',
            '-print_format', 'json',
            '-show_streams',
            '-select_streams', 's',
            filepath,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=30)
        if result.returncode != 0:
            return []
        data = json.loads(result.stdout)
        streams = []
        for s in data.get('streams', []):
            tags = s.get('tags', {})
            disp = s.get('disposition', {})
            # Detect empty tracks via muxer statistics
            num_frames_str = (tags.get('NUMBER_OF_FRAMES') or
                              tags.get('NUMBER_OF_FRAMES-eng') or '')
            num_bytes_str = (tags.get('NUMBER_OF_BYTES') or
                             tags.get('NUMBER_OF_BYTES-eng') or '')
            try:
                num_frames = int(num_frames_str) if num_frames_str else -1
            except ValueError:
                num_frames = -1
            try:
                num_bytes = int(num_bytes_str) if num_bytes_str else -1
            except ValueError:
                num_bytes = -1
            is_empty = (num_frames == 0 or num_bytes == 0)
            # DVB/VobSub tracks often lack MKV stats tags.  Detect
            # empty DVB tracks by probing actual packet sizes — if
            # every packet is ≤20 bytes it's just keepalive/clear
            # segments with no subtitle content.
            codec_name = s.get('codec_name', 'unknown')
            if (not is_empty
                    and codec_name in ('dvb_subtitle', 'dvd_subtitle')
                    and num_frames == -1 and num_bytes == -1):
                is_empty = _probe_bitmap_empty(filepath,
                                               s.get('index', 0))
            streams.append({
                'index':      s.get('index', 0),
                'codec_name': codec_name,
                'language':   tags.get('language', 'und'),
                'title':      tags.get('title', ''),
                'forced':     bool(disp.get('forced', 0)),
                'sdh':        bool(disp.get('hearing_impaired', 0)),
                'default':    bool(disp.get('default', 0)),
                'empty':      is_empty,
                'num_frames': num_frames,
            })
        return streams
    except Exception:
        return []


def get_all_streams(filepath):
    """Return a list of all stream dicts (video, audio, subtitle, etc.).

    Each dict has: index, codec_type, codec_name.
    """
    try:
        cmd = [
            'ffprobe', '-v', 'quiet',
            '-print_format', 'json',
            '-show_streams',
            filepath,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=30)
        if result.returncode != 0:
            return []
        data = json.loads(result.stdout)
        return [
            {
                'index':      s.get('index', 0),
                'codec_type': s.get('codec_type', 'unknown'),
                'codec_name': s.get('codec_name', 'unknown'),
            }
            for s in data.get('streams', [])
        ]
    except Exception:
        return []


def get_audio_info(filepath):
    """Return a list of audio stream dicts for the given file.

    Each dict has: index, codec_name, codec_long_name, profile, channels,
    sample_rate, bit_rate, language, title.
    """
    try:
        cmd = [
            'ffprobe', '-v', 'quiet',
            '-print_format', 'json',
            '-show_streams',
            '-select_streams', 'a',
            filepath,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=30)
        if result.returncode != 0:
            return []
        data = json.loads(result.stdout)
        streams = []
        for s in data.get('streams', []):
            tags = s.get('tags', {})
            streams.append({
                'index':          s.get('index', 0),
                'codec_name':     s.get('codec_name', 'unknown'),
                'codec_long_name': s.get('codec_long_name', ''),
                'profile':        s.get('profile', ''),
                'channels':       s.get('channels', 0),
                'sample_rate':    s.get('sample_rate', ''),
                'bit_rate':       s.get('bit_rate', ''),
                'language':       tags.get('language', 'und'),
                'title':          tags.get('title', ''),
            })
        return streams
    except Exception:
        return []


# ═══════════════════════════════════════════════════════════════════
# Tkinter helpers
# ═══════════════════════════════════════════════════════════════════

def create_tooltip(widget, text):
    """Attach a hover tooltip to a tkinter widget."""
    tip = None

    def _show(event):
        nonlocal tip
        if tip:
            return
        tip = tk.Toplevel(widget)
        tip.wm_overrideredirect(True)
        tip.wm_geometry(f"+{event.x_root + 12}+{event.y_root + 8}")
        lbl = tk.Label(tip, text=text, background='#ffffe0',
                       relief='solid', borderwidth=1,
                       font=('Helvetica', 9), padx=6, pady=2)
        lbl.pack()

    def _hide(event):
        nonlocal tip
        if tip:
            tip.destroy()
            tip = None

    widget.bind('<Enter>', _show, add='+')
    widget.bind('<Leave>', _hide, add='+')


def _run_zenity(cmd, timeout=120):
    """Run a zenity command with optimized GTK startup.

    Sets environment variables to reduce GTK initialization overhead:
    - GTK_USE_PORTAL=0: bypass xdg-desktop-portal (slow D-Bus roundtrip)
    - GDK_BACKEND=x11: skip Wayland detection on X11 systems
    - NO_AT_BRIDGE=1: skip accessibility bridge (AT-SPI) startup

    Returns (returncode, stdout) tuple.
    """
    env = os.environ.copy()
    env['GTK_USE_PORTAL'] = '0'
    env['GDK_BACKEND'] = 'x11'
    env['NO_AT_BRIDGE'] = '1'
    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=timeout, env=env)
        return result.returncode, result.stdout
    except Exception:
        return 1, ''


def _ensure_dir(path):
    """If *path* is a file, return its parent directory; otherwise return
    it unchanged.  Used to sanitize folder-selection dialogs that allow
    the user to select files (e.g. tkinter askdirectory on some GTK
    backends)."""
    if path and os.path.isfile(path):
        return os.path.dirname(path)
    return path


def ask_directory(initialdir=None, title="Select Folder", parent=None,
                  multiple=False):
    """Open a folder-selection dialog.

    Tries zenity first (GTK dialog with proper single-click + Open
    button behaviour), then falls back to tkinter's askdirectory.

    When *multiple* is True, returns a list of selected folder paths
    (empty list on cancel).  When False, returns a single path string
    (empty string on cancel).
    """
    if initialdir:
        initialdir = str(initialdir)
    if shutil.which('zenity'):
        cmd = [
            'zenity', '--file-selection', '--directory',
            '--title', title,
        ]
        if multiple:
            cmd += ['--multiple', '--separator', '\n']
        if initialdir:
            cmd += ['--filename', initialdir + '/']
        rc, stdout = _run_zenity(cmd)
        if rc == 0 and stdout.strip():
            if multiple:
                paths = [p for p in stdout.strip().split('\n') if p]
                return list(dict.fromkeys(
                    _ensure_dir(p) for p in paths))
            return _ensure_dir(stdout.strip())
        return [] if multiple else ''
    kwargs = {'initialdir': initialdir, 'title': title}
    if parent:
        kwargs['parent'] = parent
    if multiple:
        # tkinter's askdirectory doesn't support multi-select —
        # open the dialog in a loop until the user cancels
        folders = []
        while True:
            path = filedialog.askdirectory(**kwargs)
            if not path:
                break
            path = _ensure_dir(path)
            folders.append(path)
            kwargs['initialdir'] = os.path.dirname(path)
            kwargs['title'] = (f"Select another folder "
                               f"({len(folders)} selected) — "
                               f"Cancel to finish")
        return folders
    return _ensure_dir(filedialog.askdirectory(**kwargs))


def ask_open_files(initialdir=None, title="Select Files", parent=None,
                    filetypes=None):
    """Open a file-selection dialog (multi-select).

    Tries zenity first (GTK dialog with proper theming and font scaling),
    then falls back to tkinter's askopenfilenames.

    Returns a list of file paths, or an empty list if cancelled.
    """
    if initialdir:
        initialdir = str(initialdir)
    if shutil.which('zenity'):
        cmd = [
            'zenity', '--file-selection', '--multiple',
            '--separator', '\n',
            '--title', title,
        ]
        if initialdir:
            cmd += ['--filename', initialdir + '/']
        if filetypes:
            for label, pattern in filetypes:
                if pattern and pattern != '*.*':
                    cmd += ['--file-filter',
                            f'{label} | {pattern.replace(" ", " ")}']
            cmd += ['--file-filter', 'All files | *']
        rc, stdout = _run_zenity(cmd)
        if rc == 0 and stdout.strip():
            return [p for p in stdout.strip().split('\n') if p]
        return []
    # Fallback to tkinter
    kwargs = {'initialdir': initialdir, 'title': title}
    if parent:
        kwargs['parent'] = parent
    if filetypes:
        kwargs['filetypes'] = filetypes
    result = filedialog.askopenfilenames(**kwargs)
    return list(result) if result else []


def ask_open_file(initialdir=None, title="Select File", parent=None,
                   filetypes=None):
    """Open a file-selection dialog (single file).

    Tries zenity first, falls back to tkinter.
    Returns a file path string, or '' if cancelled.
    """
    if initialdir:
        initialdir = str(initialdir)
    if shutil.which('zenity'):
        cmd = [
            'zenity', '--file-selection',
            '--title', title,
        ]
        if initialdir:
            cmd += ['--filename', initialdir + '/']
        if filetypes:
            for label, pattern in filetypes:
                if pattern and pattern != '*.*':
                    cmd += ['--file-filter',
                            f'{label} | {pattern.replace(" ", " ")}']
            cmd += ['--file-filter', 'All files | *']
        rc, stdout = _run_zenity(cmd)
        if rc == 0 and stdout.strip():
            return stdout.strip()
        return ''
    kwargs = {'initialdir': initialdir, 'title': title}
    if parent:
        kwargs['parent'] = parent
    if filetypes:
        kwargs['filetypes'] = filetypes
    result = filedialog.askopenfilename(**kwargs)
    return result or ''


def _zenity_major_version():
    """Return the major version of zenity (e.g. 3 or 4), or 0 if unknown."""
    try:
        result = subprocess.run(['zenity', '--version'], capture_output=True,
                                text=True, timeout=5)
        return int(result.stdout.strip().split('.')[0])
    except Exception:
        return 0


def ask_save_file(initialdir=None, initialfile=None, title="Save As",
                   parent=None, filetypes=None, defaultextension=None):
    """Open a save-file dialog.

    Tries zenity first, falls back to tkinter.
    zenity 4.x (GTK4) has a bug where --filename no longer pre-fills
    the filename in save dialogs, so we skip zenity for save dialogs
    when an initialfile is requested and zenity >= 4.
    Returns a file path string, or '' if cancelled.
    """
    if initialdir:
        initialdir = str(initialdir)
    # zenity 4.x save dialogs ignore the filename portion of --filename
    # (GTK4 GtkFileDialog bug) — only use zenity when no initialfile is
    # needed, or when zenity < 4 where --filename works correctly.
    use_zenity = (shutil.which('zenity')
                  and (not initialfile or _zenity_major_version() < 4))
    if use_zenity:
        cmd = [
            'zenity', '--file-selection', '--save',
            '--confirm-overwrite',
            '--title', title,
        ]
        if initialdir and initialfile:
            cmd += ['--filename', os.path.join(initialdir, initialfile)]
        elif initialdir:
            cmd += ['--filename', initialdir + '/']
        elif initialfile:
            cmd += ['--filename', initialfile]
        if filetypes:
            for label, pattern in filetypes:
                if pattern and pattern != '*.*':
                    cmd += ['--file-filter',
                            f'{label} | {pattern.replace(" ", " ")}']
            cmd += ['--file-filter', 'All files | *']
        rc, stdout = _run_zenity(cmd)
        if rc == 0 and stdout.strip():
            path = stdout.strip()
            if defaultextension and not os.path.splitext(path)[1]:
                path += defaultextension
            return path
        return ''
    kwargs = {'initialdir': initialdir, 'title': title}
    if parent:
        kwargs['parent'] = parent
    if filetypes:
        kwargs['filetypes'] = filetypes
    if initialfile:
        kwargs['initialfile'] = initialfile
    if defaultextension:
        kwargs['defaultextension'] = defaultextension
    result = filedialog.asksaveasfilename(**kwargs)
    return result or ''


def get_dpi_scale(widget):
    """Return the DPI scale factor relative to the standard 96 DPI.
    Returns 1.0 on a standard display, ~1.5 at 150% scaling, 2.0 at 200%, etc.
    The widget must already exist (any Tk widget will do)."""
    try:
        tk_scaling = widget.tk.call('tk', 'scaling')
        # Tk scaling of 1.333 = 96 DPI (standard), 2.666 = 192 DPI (2×)
        return float(tk_scaling) / 1.333333
    except Exception:
        return 1.0


def scaled_geometry(widget, width, height):
    """Return a Tk geometry string scaled for the current DPI,
    clamped to the screen size so the window never opens larger
    than the display (important for high-DPI / small screens).
    Usage: win.geometry(scaled_geometry(win, 920, 880))"""
    s = get_dpi_scale(widget)
    if s <= 1.05:  # No scaling needed at or below 100%
        sw, sh = width, height
    else:
        sw = int(width * s)
        sh = int(height * s)
    # Clamp to screen size with margin for taskbar / panels
    try:
        screen_w = widget.winfo_screenwidth()
        screen_h = widget.winfo_screenheight()
        margin = 80  # pixels reserved for taskbar / dock
        sw = min(sw, screen_w - margin)
        sh = min(sh, screen_h - margin)
    except Exception:
        pass
    return f"{sw}x{sh}"


def scaled_minsize(widget, width, height):
    """Return a (width, height) tuple scaled for the current DPI,
    clamped to the screen size so minsize never exceeds the display.
    Usage: win.minsize(*scaled_minsize(win, 750, 650))"""
    s = get_dpi_scale(widget)
    if s <= 1.05:
        mw, mh = width, height
    else:
        mw, mh = int(width * s), int(height * s)
    # Clamp to screen size with margin
    try:
        screen_w = widget.winfo_screenwidth()
        screen_h = widget.winfo_screenheight()
        margin = 80
        mw = min(mw, screen_w - margin)
        mh = min(mh, screen_h - margin)
    except Exception:
        pass
    return (mw, mh)


def _saved_ui_scale():
    """Read the user's manual UI-Scale override from prefs, if any.

    Returns a float scale factor (1.25 == 125%) or None for 'auto'. Read straight from the
    prefs FILE (not an app object) so that EVERY launch path honors the same setting — the
    main app AND every standalone tool ("Open with" a renamer, subtitle editor, etc.) — since
    this runs at startup before any app object exists. This is the reliable fallback for
    desktops whose scaling none of the auto-detect methods can read (e.g. GNOME/Zorin
    fractional scaling launched standalone). Accepts either 1.5 or 150 (percent).
    (Arthur 2026-07-13.)
    """
    try:
        from .constants import PREFS_DIR, PREFS_FILENAME
        path = os.path.join(os.path.expanduser(PREFS_DIR), PREFS_FILENAME)
        with open(path) as f:
            val = json.load(f).get('ui_scale', 'auto')
        if val in (None, '', 'auto', 'Auto'):
            return None
        s = float(val)
        if s > 10:                 # a percentage like 150 → 1.5
            s = s / 100.0
        if 0.5 <= s <= 4.0:        # sane bounds
            return s
    except Exception:
        pass
    return None


def configure_dpi_scaling(root):
    """Configure Tk scaling for high-DPI displays.

    Tkinter on Linux defaults to 96 DPI and ignores the desktop
    environment's scaling settings.  This function detects the real
    DPI (via the X server, GDK, or environment variables) and tells
    Tk to scale all widgets, fonts, and geometry accordingly.

    Call once, immediately after creating the root Tk window and
    before building any widgets.
    """
    try:
        real_dpi = None

        # Method 1: Xft.dpi from X resources (set by most DEs)
        try:
            xrdb = subprocess.check_output(
                ['xrdb', '-query'], stderr=subprocess.DEVNULL, timeout=2
            ).decode('utf-8', errors='replace')
            for line in xrdb.splitlines():
                if 'Xft.dpi' in line:
                    real_dpi = float(line.split(':')[-1].strip())
                    break
        except Exception:
            pass

        # Method 2: GDK_SCALE environment variable (GNOME/GTK)
        if real_dpi is None:
            gdk_scale = os.environ.get('GDK_SCALE')
            if gdk_scale:
                try:
                    real_dpi = 96.0 * float(gdk_scale)
                except (ValueError, TypeError):
                    pass

        # Method 3: QT_SCALE_FACTOR (KDE/Qt)
        if real_dpi is None:
            qt_scale = os.environ.get('QT_SCALE_FACTOR')
            if qt_scale:
                try:
                    real_dpi = 96.0 * float(qt_scale)
                except (ValueError, TypeError):
                    pass

        # Method 4: GNOME gsettings text-scaling-factor
        # Catches GNOME/Wayland setups where Xft.dpi is 96 and
        # GDK_SCALE isn't set for non-GTK apps launched via "Open with".
        if real_dpi is None:
            try:
                gs = subprocess.check_output(
                    ['gsettings', 'get', 'org.gnome.desktop.interface',
                     'text-scaling-factor'],
                    stderr=subprocess.DEVNULL, timeout=2
                ).decode().strip()
                ts = float(gs)
                if ts > 1.05:
                    real_dpi = 96.0 * ts
            except Exception:
                pass

        # Method 5: GNOME gsettings scaling-factor (integer scale, e.g. 2)
        if real_dpi is None:
            try:
                gs = subprocess.check_output(
                    ['gsettings', 'get', 'org.gnome.desktop.interface',
                     'scaling-factor'],
                    stderr=subprocess.DEVNULL, timeout=2
                ).decode().strip()
                # gsettings returns "uint32 2" — extract the number
                import re as _re
                m = _re.search(r'(\d+)', gs)
                if m:
                    sf = int(m.group(1))
                    if sf >= 2:
                        real_dpi = 96.0 * sf
            except Exception:
                pass

        # Method 6: Tk's own fpixels detection (Wayland with Tk 8.6.13+)
        # Some modern Wayland compositors inform Tk directly.
        if real_dpi is None:
            try:
                fpx = float(root.tk.call('winfo', 'fpixels', root, '1i'))
                if fpx > 100:
                    real_dpi = fpx
            except Exception:
                pass

        # Manual UI-Scale override (Settings) WINS over anything auto-detect found — the
        # reliable path for displays the auto methods can't read. A value of 100% (1.0)
        # explicitly forces NO scaling even if auto-detect wanted some (lets a user turn
        # off a bad auto-guess). 'auto'/unset → leaves auto-detect's result in place.
        _manual = _saved_ui_scale()
        if _manual:
            real_dpi = 96.0 * _manual

        if real_dpi and real_dpi > 96:
            # Tk scaling factor: 1.0 = 72 DPI (Tk's internal unit)
            # Default Tk scaling on 96 DPI display = 96/72 = 1.333...
            # For a 192 DPI display we want 192/72 = 2.666...
            factor = real_dpi / 72.0
            root.tk.call('tk', 'scaling', factor)
    except Exception:
        pass  # never break app startup over scaling

    # Set readable font sizes for all Tk named fonts — affects ALL dialogs
    # including file pickers, message boxes, etc.
    try:
        import tkinter.font as tkfont
        # Increase the size of all standard Tk fonts
        for font_name in ('TkDefaultFont', 'TkTextFont', 'TkMenuFont',
                          'TkHeadingFont', 'TkCaptionFont', 'TkSmallCaptionFont',
                          'TkIconFont', 'TkTooltipFont', 'TkFixedFont'):
            try:
                f = tkfont.nametofont(font_name)
                current_size = f.actual()['size']
                # Only increase if currently small (< 10pt)
                if abs(current_size) < 10:
                    f.configure(size=11)
            except Exception:
                pass
    except Exception:
        pass

    # Scale ttk checkbox / radiobutton indicators to match font size
    _scale_check_radio_indicators(root)


def _scale_check_radio_indicators(root):
    """Create scaled checkbox and radiobutton indicator images.

    The clam (and most ttk) themes use fixed-size bitmap indicators that
    don't respond to ``tk scaling``.  This draws replacement images via
    tk.PhotoImage so they match the surrounding text on high-DPI displays.
    """
    try:
        from tkinter import ttk
        import tkinter.font as tkfont

        style = ttk.Style()

        # Determine indicator size from the default font's line height
        try:
            font = tkfont.nametofont('TkDefaultFont')
            font_height = font.metrics('linespace')
        except Exception:
            font_height = 16
        size = max(13, font_height)
        # Keep size odd so the checkmark / dot centers cleanly
        if size % 2 == 0:
            size += 1

        # ── Helper: draw a border + fill on a PhotoImage ──
        def _make_box(sz, fill, border):
            img = tk.PhotoImage(width=sz, height=sz)
            # Fill entire image with border color, then fill interior
            img.put(border, to=(0, 0, sz, sz))
            img.put(fill, to=(2, 2, sz - 2, sz - 2))
            return img

        def _make_checked_box(sz, fill, border, check_color):
            img = _make_box(sz, fill, border)
            # Draw an X mark (two diagonal strokes)
            pad = max(3, sz // 4)
            thickness = max(1, sz // 8)
            # Stroke 1: top-left to bottom-right
            for i in range(sz - 2 * pad):
                x = pad + i
                y = pad + i
                for t in range(thickness):
                    if x + t < sz - 2 and y < sz - 2:
                        img.put(check_color, to=(x + t, y, x + t + 1, y + 1))
                    if y + t < sz - 2 and x < sz - 2:
                        img.put(check_color, to=(x, y + t, x + 1, y + t + 1))
            # Stroke 2: top-right to bottom-left
            for i in range(sz - 2 * pad):
                x = sz - pad - 1 - i
                y = pad + i
                for t in range(thickness):
                    if x - t >= 2 and y < sz - 2:
                        img.put(check_color, to=(x - t, y, x - t + 1, y + 1))
                    if y + t < sz - 2 and x >= 2:
                        img.put(check_color, to=(x, y + t, x + 1, y + t + 1))
            return img

        def _make_radio(sz, fill, border):
            """Draw a circle for radiobutton indicators."""
            img = tk.PhotoImage(width=sz, height=sz)
            # Transparent background
            img.put('', to=(0, 0, sz, sz))
            cx, cy = sz // 2, sz // 2
            r_outer = sz // 2 - 1
            r_inner = r_outer - 2
            for y in range(sz):
                for x in range(sz):
                    dx, dy = x - cx, y - cy
                    dist_sq = dx * dx + dy * dy
                    if dist_sq <= r_outer * r_outer:
                        if dist_sq <= r_inner * r_inner:
                            img.put(fill, to=(x, y, x + 1, y + 1))
                        else:
                            img.put(border, to=(x, y, x + 1, y + 1))
            return img

        def _make_radio_selected(sz, fill, border, dot_color):
            img = _make_radio(sz, fill, border)
            cx, cy = sz // 2, sz // 2
            r_dot = max(2, sz // 5)
            for y in range(sz):
                for x in range(sz):
                    dx, dy = x - cx, y - cy
                    if dx * dx + dy * dy <= r_dot * r_dot:
                        img.put(dot_color, to=(x, y, x + 1, y + 1))
            return img

        # ── Colors ──
        bg       = '#ffffff'
        border   = '#888888'
        active   = '#eeeeee'
        check    = '#000000'
        disabled_bg = '#d9d9d9'
        disabled_chk = '#a0a0a0'

        # ── Checkbox images ──
        cb_unchecked    = _make_box(size, bg, border)
        cb_checked      = _make_checked_box(size, bg, border, check)
        cb_unchecked_a  = _make_box(size, active, border)
        cb_checked_a    = _make_checked_box(size, active, border, check)
        cb_unchecked_d  = _make_box(size, disabled_bg, disabled_chk)
        cb_checked_d    = _make_checked_box(size, disabled_bg, disabled_chk, disabled_chk)

        # ── Radiobutton images ──
        rb_unchecked    = _make_radio(size, bg, border)
        rb_selected     = _make_radio_selected(size, bg, border, check)
        rb_unchecked_a  = _make_radio(size, active, border)
        rb_selected_a   = _make_radio_selected(size, active, border, check)
        rb_unchecked_d  = _make_radio(size, disabled_bg, disabled_chk)
        rb_selected_d   = _make_radio_selected(size, disabled_bg, disabled_chk, disabled_chk)

        # Keep references so images aren't garbage-collected
        root._scaled_indicators = [
            cb_unchecked, cb_checked, cb_unchecked_a, cb_checked_a,
            cb_unchecked_d, cb_checked_d,
            rb_unchecked, rb_selected, rb_unchecked_a, rb_selected_a,
            rb_unchecked_d, rb_selected_d,
        ]

        # ── Apply to ttk style ──
        style.element_create('custom_check', 'image', cb_unchecked,
            ('disabled selected', cb_checked_d),
            ('disabled', cb_unchecked_d),
            ('active selected', cb_checked_a),
            ('active', cb_unchecked_a),
            ('selected', cb_checked),
        )
        style.layout('TCheckbutton', [
            ('Checkbutton.padding', {'sticky': 'nswe', 'children': [
                ('custom_check', {'side': 'left', 'sticky': ''}),
                ('Checkbutton.focus', {'side': 'left', 'sticky': '',
                    'children': [
                        ('Checkbutton.label', {'sticky': 'nswe'}),
                    ]}),
            ]}),
        ])

        style.element_create('custom_radio', 'image', rb_unchecked,
            ('disabled selected', rb_selected_d),
            ('disabled', rb_unchecked_d),
            ('active selected', rb_selected_a),
            ('active', rb_unchecked_a),
            ('selected', rb_selected),
        )
        style.layout('TRadiobutton', [
            ('Radiobutton.padding', {'sticky': 'nswe', 'children': [
                ('custom_radio', {'side': 'left', 'sticky': ''}),
                ('Radiobutton.focus', {'side': 'left', 'sticky': '',
                    'children': [
                        ('Radiobutton.label', {'sticky': 'nswe'}),
                    ]}),
            ]}),
        ])

    except Exception:
        pass  # never break app startup over indicator scaling

    # ── Style Combobox and Button widgets: white when active, gray when disabled ──
    try:
        from tkinter import ttk
        style = ttk.Style()

        # Combobox: white fieldbackground when readonly, gray when disabled
        style.map('TCombobox',
            fieldbackground=[
                ('disabled', '#d9d9d9'),
                ('readonly', '#ffffff'),
            ],
            background=[
                ('disabled', '#d9d9d9'),
                ('readonly', '#ffffff'),
                ('active', '#eeeeee'),
            ],
            foreground=[
                ('disabled', '#a0a0a0'),
            ],
        )

        # TButton: white background when normal, gray when disabled
        style.map('TButton',
            background=[
                ('disabled', '#d9d9d9'),
                ('pressed', '#c0c0c0'),
                ('active', '#eeeeee'),
                ('!disabled', '#ffffff'),
            ],
            foreground=[
                ('disabled', '#a0a0a0'),
            ],
        )

        # Entry: white when normal, gray when disabled/readonly
        style.map('TEntry',
            fieldbackground=[
                ('disabled', '#d9d9d9'),
                ('readonly', '#d9d9d9'),
                ('!disabled !readonly', '#ffffff'),
            ],
            foreground=[
                ('disabled', '#a0a0a0'),
                ('readonly', '#a0a0a0'),
            ],
        )
    except Exception:
        pass


def center_window_on_screen(win):
    """Center a Toplevel or Tk window on the screen containing the
    mouse pointer."""
    win.update_idletasks()
    # Use requested geometry if winfo returns tiny defaults
    width = win.winfo_width()
    height = win.winfo_height()
    if width <= 10 or height <= 10:
        # Parse from geometry string (e.g. "950x650")
        try:
            geo = win.geometry()
            wh = geo.split('+')[0]
            width, height = (int(x) for x in wh.split('x'))
        except (ValueError, IndexError):
            width, height = 800, 600
    screen_w = win.winfo_screenwidth()
    screen_h = win.winfo_screenheight()
    x = (screen_w - width) // 2
    y = (screen_h - height) // 2
    win.geometry(f'{width}x{height}+{x}+{y}')


def center_window_on_parent(win, parent):
    """Center a Toplevel window on its parent window."""
    win.update_idletasks()
    w = win.winfo_width()
    h = win.winfo_height()
    pw = parent.winfo_width()
    ph = parent.winfo_height()
    px = parent.winfo_x()
    py = parent.winfo_y()
    x = max(0, px + (pw - w) // 2)
    y = max(0, py + (ph - h) // 2)
    win.geometry(f'{w}x{h}+{x}+{y}')
