"""
Docflix Media Suite — Package Entry Point

Allows running the app via:
    python -m modules           # Full app
    python -m modules --subs    # Subtitle Editor only (future)
    python -m modules --rename  # TV Show Renamer only (future)
    python -m modules --media   # Media Processor only (future)
"""

import sys
import os

# Add the parent directory to sys.path so the legacy monolith can still
# be imported during the transition period.
_pkg_dir = os.path.dirname(os.path.abspath(__file__))
_project_dir = os.path.dirname(_pkg_dir)
if _project_dir not in sys.path:
    sys.path.insert(0, _project_dir)


def main():
    """Route to the appropriate entry point based on CLI flags."""

    # ── Subcommand routing (for future standalone tools) ──
    # These will be enabled as each tool module is extracted.
    #
    # if '--subs' in sys.argv:
    #     sys.argv.remove('--subs')
    #     from .subtitle_editor import main as subs_main
    #     subs_main()
    #     return
    #
    # if '--rename' in sys.argv:
    #     sys.argv.remove('--rename')
    #     from .tv_renamer import main as rename_main
    #     rename_main()
    #     return
    #
    # if '--media' in sys.argv:
    #     sys.argv.remove('--media')
    #     from .media_processor import main as media_main
    #     media_main()
    #     return

    # ── Default: launch the full app via the legacy monolith ──
    # During the transition, import the monolith file directly by path
    # since the package name shadows the .py file.
    # This will change once app.py is extracted (Phase 5).
    import importlib.util
    _mono = os.path.join(_project_dir, 'video_converter.py')
    spec = importlib.util.spec_from_file_location('_legacy_app', _mono)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.main()


if __name__ == '__main__':
    main()
