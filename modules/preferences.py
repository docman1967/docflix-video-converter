"""
Docflix Media Suite — Preferences Management

Save, load, and reset user preferences. These methods are
designed to be mixed into the VideoConverterApp class.

During the transition period, the monolith still uses its
own copy. Once the app is refactored, these will become
the canonical implementation.
"""

import json
from pathlib import Path
from tkinter import messagebox


def save_preferences(app):
    """Save current settings to a JSON preferences file."""
    prefs = {
        'encoder':              app.encoder_mode.get(),
        'video_codec':          app.video_codec.get(),
        'container':            app.container_format.get(),
        'transcode_mode':       app.transcode_mode.get(),
        'quality_mode':         app.quality_mode.get(),
        'crf':                  app.crf.get(),
        'cpu_preset':           app.cpu_preset.get(),
        'gpu_preset':           app.gpu_preset.get(),
        'audio_codec':          app.audio_codec.get(),
        'audio_bitrate':        app.audio_bitrate.get(),
        'skip_existing':        app.skip_existing.get(),
        'delete_originals':     app.delete_originals.get(),
        'hw_decode':            app.hw_decode.get(),
        'strip_internal_subs':  app.strip_internal_subs.get(),
        'two_pass':             app.two_pass.get(),
        'verify_output':        app.verify_output.get(),
        'notify_sound':         app.notify_sound.get(),
        'notify_sound_file':    app.notify_sound_file.get(),
        'default_player':        app.default_player.get(),
        'default_video_folder':  str(app.working_dir),
        'default_output_folder': str(app.output_dir) if app.output_dir else '',
        'recent_folders':        app.recent_folders,
        'strip_chapters':        app.strip_chapters.get(),
        'strip_metadata_tags':   app.strip_metadata_tags.get(),
        'set_track_metadata':    app.set_track_metadata.get(),
        'meta_video_lang':       app.meta_video_lang.get(),
        'meta_audio_lang':       app.meta_audio_lang.get(),
        'meta_sub_lang':         app.meta_sub_lang.get(),
        'custom_ad_patterns':    app.custom_ad_patterns,
        'custom_cap_words':      app.custom_cap_words,
        'custom_replacements':   app.custom_replacements,
        'custom_spell_words':    app.custom_spell_words,
        'tvdb_api_key':          getattr(self, '_tvdb_api_key', ''),
        'tmdb_api_key':          getattr(self, '_tmdb_api_key', ''),
        'tv_rename_provider':    getattr(self, '_tv_rename_provider', 'TVDB'),
        'tv_rename_template':    getattr(self, '_tv_rename_template', '{show} S{season}E{episode} {title}'),
        'media_processor':       getattr(app, '_media_proc_prefs', {}),
    }
    try:
        app._prefs_path().parent.mkdir(parents=True, exist_ok=True)
        app._prefs_path().write_text(json.dumps(prefs, indent=2))
        app.add_log(f"Preferences saved to {app._prefs_path()}", 'SUCCESS')
    except Exception as e:
        app.add_log(f"Failed to save preferences: {e}", 'ERROR')
        messagebox.showerror("Error", f"Failed to save preferences:\n{e}")


def load_preferences(app):
    """Load preferences from JSON file if it exists."""
    if not app._prefs_path().exists():
        return
    try:
        prefs = json.loads(app._prefs_path().read_text())
        saved_encoder = prefs.get('encoder', app.encoder_mode.get())
        # Backward compat: map old 'gpu' value to first available GPU backend
        if saved_encoder == 'gpu':
            saved_encoder = app._default_gpu
        # Validate that the saved backend is actually available
        if saved_encoder != 'cpu' and saved_encoder not in app.gpu_backends:
            saved_encoder = 'cpu'
        app.encoder_mode.set(saved_encoder)
        app.video_codec.set(prefs.get('video_codec',       app.video_codec.get()))
        app.container_format.set(prefs.get('container',    app.container_format.get()))
        # Always start in video-only mode regardless of saved preference
        app.transcode_mode.set('video')
        app.quality_mode.set(prefs.get('quality_mode',     app.quality_mode.get()))
        # Bitrate intentionally not saved/loaded — always starts at default (2.0M)
        # to avoid hidden mismatches between saved value and UI slider position
        app.crf.set(prefs.get('crf',                       app.crf.get()))
        app.cpu_preset.set(prefs.get('cpu_preset',         app.cpu_preset.get()))
        app.gpu_preset.set(prefs.get('gpu_preset',         app.gpu_preset.get()))
        app.audio_codec.set(prefs.get('audio_codec',       app.audio_codec.get()))
        app.audio_bitrate.set(prefs.get('audio_bitrate',   app.audio_bitrate.get()))
        app.skip_existing.set(prefs.get('skip_existing',   app.skip_existing.get()))
        app.delete_originals.set(prefs.get('delete_originals', app.delete_originals.get()))
        app.hw_decode.set(prefs.get('hw_decode',           app.hw_decode.get()))
        app.strip_internal_subs.set(prefs.get('strip_internal_subs', app.strip_internal_subs.get()))
        app.two_pass.set(prefs.get('two_pass',             app.two_pass.get()))
        app.strip_chapters.set(prefs.get('strip_chapters', app.strip_chapters.get()))
        app.strip_metadata_tags.set(prefs.get('strip_metadata_tags', app.strip_metadata_tags.get()))
        app.set_track_metadata.set(prefs.get('set_track_metadata', app.set_track_metadata.get()))
        app.meta_video_lang.set(prefs.get('meta_video_lang', app.meta_video_lang.get()))
        app.meta_audio_lang.set(prefs.get('meta_audio_lang', app.meta_audio_lang.get()))
        app.meta_sub_lang.set(prefs.get('meta_sub_lang', app.meta_sub_lang.get()))
        app.verify_output.set(prefs.get('verify_output',   app.verify_output.get()))
        app.notify_sound.set(prefs.get('notify_sound',     app.notify_sound.get()))
        app.notify_sound_file.set(prefs.get('notify_sound_file', app.notify_sound_file.get()))
        # Default folders
        app.recent_folders = prefs.get('recent_folders', [])
        app.custom_ad_patterns = prefs.get('custom_ad_patterns', [])
        app.custom_cap_words = prefs.get('custom_cap_words', [])
        app.custom_spell_words = prefs.get('custom_spell_words', [])
        app.custom_replacements = prefs.get('custom_replacements', [])
        app._tvdb_api_key = prefs.get('tvdb_api_key', '')
        app._tmdb_api_key = prefs.get('tmdb_api_key', '')
        app._tv_rename_provider = prefs.get('tv_rename_provider', 'TVDB')
        app._tv_rename_template = prefs.get('tv_rename_template',
                                              '{show} S{season}E{episode} {title}')
        # Media Processor
        app._media_proc_prefs = prefs.get('media_processor', {})
        app._rebuild_recent_menu()
        app.default_player.set(prefs.get('default_player', 'auto'))
        dvf = prefs.get('default_video_folder', '')
        if dvf and Path(dvf).is_dir():
            app.working_dir = Path(dvf)
        dof = prefs.get('default_output_folder', '')
        if dof and Path(dof).is_dir():
            app.output_dir = Path(dof)
            app.output_dir_label.configure(text=dof, foreground='black')
        app.add_log("Preferences loaded.", 'INFO')
    except Exception as e:
        app.add_log(f"Failed to load preferences: {e}", 'WARNING')


def reset_preferences(app):
    """Reset all settings to defaults."""
    if not messagebox.askyesno("Reset to Defaults",
                               "Reset all settings to their defaults?"):
        return
    app.encoder_mode.set(app._default_gpu if app.has_gpu else 'cpu')
    app.video_codec.set('H.265 / HEVC')
    app.container_format.set('.mkv')
    app.transcode_mode.set('video')
    app.quality_mode.set('bitrate')
    app.bitrate.set('2M')
    app.crf.set('23')
    app.cpu_preset.set('ultrafast')
    app.gpu_preset.set('p4')
    app.audio_codec.set('aac')
    app.audio_bitrate.set('128k')
    app.skip_existing.set(True)
    app.delete_originals.set(False)
    app.hw_decode.set(app.has_gpu)
    app.two_pass.set(False)
    app.verify_output.set(True)
    app.notify_sound.set(True)
    app.notify_sound_file.set('complete')
    app.strip_chapters.set(False)
    app.strip_metadata_tags.set(False)
    app.set_track_metadata.set(False)
    app.meta_video_lang.set('und')
    app.meta_audio_lang.set('eng')
    app.meta_sub_lang.set('eng')
    app._on_metadata_toggle()
    # Refresh UI state
    app.on_encoder_change(silent=True)
    app.on_video_codec_change()
    app.on_transcode_mode_change()
    app.on_quality_mode_change()
    app.add_log("Settings reset to defaults.", 'INFO')
