"""
Docflix Media Suite — Standalone Context

Lightweight application context for running individual tools
(Subtitle Editor, TV Show Renamer, Media Processor) as standalone
programs without loading the full converter app.

Provides the same interface that tool modules expect from the main
VideoConverterApp — preferences, window centering, etc.
"""

import json
import os
from pathlib import Path
import tkinter as tk
from tkinter import ttk

from .constants import APP_NAME, APP_VERSION, PREFS_DIR, PREFS_FILENAME
from .utils import (center_window_on_screen, center_window_on_parent,
                     configure_dpi_scaling)


class StandaloneContext:
    """Minimal app context for standalone tool launches.

    Provides the subset of VideoConverterApp's interface that tool
    windows use: preferences, window centering, and root window access.
    """

    def __init__(self, root):
        self.root = root
        self._prefs_path = os.path.join(
            os.path.expanduser(PREFS_DIR), PREFS_FILENAME)
        self._load_preferences()

    # ── Preferences ──

    def _load_preferences(self):
        """Load preferences from the shared JSON file."""
        try:
            with open(self._prefs_path, 'r') as f:
                prefs = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            prefs = {}

        # TV Show Renamer
        self._tvdb_api_key = prefs.get('tvdb_api_key', '')
        self._tmdb_api_key = prefs.get('tmdb_api_key', '')
        self._tv_rename_provider = prefs.get('tv_rename_provider', 'TVDB')
        self._tv_rename_template = prefs.get(
            'tv_rename_template',
            '{show} S{season}E{episode} {title}')
        self._movie_rename_template = prefs.get(
            'movie_rename_template',
            '{show} ({year})')
        self._custom_rename_templates = prefs.get(
            'custom_rename_templates', [])
        self._custom_tv_templates = prefs.get(
            'custom_tv_templates', [])
        self._custom_movie_templates = prefs.get(
            'custom_movie_templates', [])

        # Subtitle editor
        self.custom_cap_words = prefs.get('custom_cap_words', [])
        self.use_names_db = prefs.get('use_names_db', False)
        self.custom_spell_words = prefs.get('custom_spell_words', [])
        self.custom_ad_patterns = prefs.get('custom_ad_patterns', [])
        self.search_replace_pairs = prefs.get('search_replace_pairs', [])

        # Media processor
        self._media_proc_prefs = prefs.get('media_processor', {})

        # Video scaler
        self._scaler_prefs = prefs.get('video_scaler', {})

        # Auto-load names database if preference is enabled
        if self.use_names_db:
            from .subtitle_filters import is_names_db_available, load_names_db
            if is_names_db_available():
                load_names_db()

        # Store full prefs for pass-through
        self._prefs = prefs

    def save_preferences(self):
        """Save preferences to the shared JSON file."""
        prefs = getattr(self, '_prefs', {})

        # Update with current values
        prefs['tvdb_api_key'] = getattr(self, '_tvdb_api_key', '')
        prefs['tmdb_api_key'] = getattr(self, '_tmdb_api_key', '')
        prefs['tv_rename_provider'] = getattr(
            self, '_tv_rename_provider', 'TVDB')
        prefs['tv_rename_template'] = getattr(
            self, '_tv_rename_template',
            '{show} S{season}E{episode} {title}')
        prefs['movie_rename_template'] = getattr(
            self, '_movie_rename_template',
            '{show} ({year})')
        prefs['custom_rename_templates'] = getattr(
            self, '_custom_rename_templates', [])
        prefs['custom_tv_templates'] = getattr(
            self, '_custom_tv_templates', [])
        prefs['custom_movie_templates'] = getattr(
            self, '_custom_movie_templates', [])
        prefs['custom_cap_words'] = getattr(self, 'custom_cap_words', [])
        prefs['use_names_db'] = getattr(self, 'use_names_db', False)
        prefs['custom_spell_words'] = getattr(
            self, 'custom_spell_words', [])
        prefs['custom_ad_patterns'] = getattr(
            self, 'custom_ad_patterns', [])
        prefs['search_replace_pairs'] = getattr(
            self, 'search_replace_pairs', [])

        self._prefs = prefs

        # Ensure directory exists
        os.makedirs(os.path.dirname(self._prefs_path), exist_ok=True)
        try:
            with open(self._prefs_path, 'w') as f:
                json.dump(prefs, f, indent=2)
        except Exception:
            pass

    # ── Window centering ──

    def _center_on_main(self, win):
        """Center a child window on the main root window.
        In standalone mode, skip centering for the main tool window
        (let the window manager place it) but center sub-dialogs."""
        if getattr(self, '_standalone_mode', False):
            # For sub-dialogs (not the main tool window), center on
            # the visible tool window
            for child in self.root.winfo_children():
                if (isinstance(child, tk.Toplevel)
                        and child.winfo_viewable()
                        and child is not win):
                    center_window_on_parent(win, child)
                    return
            # Main tool window — let the window manager place it
            return
        else:
            center_window_on_parent(win, self.root)


def create_standalone_root(title, geometry="960x650", minsize=(800, 550)):
    """Create a Tk root window styled for standalone tool use.

    Returns (root, app_context) tuple.
    """
    try:
        from tkinterdnd2 import TkinterDnD
        root = TkinterDnD.Tk(className='docflix')
    except ImportError:
        root = tk.Tk(className='docflix')

    # Apply high-DPI scaling before any widgets are created
    configure_dpi_scaling(root)

    root.title(title)

    # Set taskbar/dock icon so it shows "Docflix" instead of "Tk"
    try:
        from PIL import Image, ImageTk
        _icon_path = Path(__file__).parent.parent / 'logo_transparent.png'
        if not _icon_path.exists():
            _icon_path = Path(__file__).parent.parent / 'logo.png'
        if _icon_path.exists():
            _icon_img = Image.open(_icon_path)
            _icon_photo = ImageTk.PhotoImage(_icon_img)
            root.iconphoto(True, _icon_photo)
            root._icon_ref = _icon_photo  # prevent garbage collection
    except Exception:
        pass
    # Scale geometry for high-DPI displays
    from .utils import get_dpi_scale
    s = get_dpi_scale(root)
    if s > 1.05:
        # Parse WxH from geometry string and scale
        import re as _re
        m = _re.match(r'(\d+)x(\d+)', geometry)
        if m:
            geometry = f"{int(int(m.group(1)) * s)}x{int(int(m.group(2)) * s)}"
        minsize = (int(minsize[0] * s), int(minsize[1] * s))
    root.geometry(geometry)
    root.minsize(*minsize)

    # Hide dotfiles in Tk file dialogs by default
    try:
        root.tk.call('catch', 'tk_getOpenFile foo bar')
        root.tk.call('set', '::tk::dialog::file::showHiddenVar', '0')
        root.tk.call('set', '::tk::dialog::file::showHiddenBtn', '1')
    except Exception:
        pass

    # Apply theme
    try:
        style = ttk.Style()
        if 'clam' in style.theme_names():
            style.theme_use('clam')
    except Exception:
        pass

    app = StandaloneContext(root)

    # Center on screen
    root.update_idletasks()
    center_window_on_screen(root)

    return root, app
