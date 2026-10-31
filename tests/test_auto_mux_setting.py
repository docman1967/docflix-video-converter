"""Default Settings: Mux external subtitles automatically (Tony, 2026-10-31)."""
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from video_converter import VideoConverterApp as App


def fake_app(on, tmp, prefs=None):
    logs = []
    attached = []
    return SimpleNamespace(
        auto_mux_external_subs=SimpleNamespace(get=lambda: on),
        working_dir=str(tmp),
        files=[],
        add_log=lambda m, level='INFO': logs.append(m),
        _add_file_placeholder=lambda p: {'path': p},
        _associate_external_sub=lambda p: attached.append(p) or True,
        _enqueue_probe=lambda *a, **k: None,
        _prefs_path=lambda: prefs,
        logs=logs, attached=attached)


def test_off_leaves_subtitles_beside_the_video_alone():
    a = fake_app(False, '/tmp')
    App._add_files_threaded(a, ['a.mkv'], ['a.eng.srt'])
    assert a.attached == []
    assert any('unattached' in m for m in a.logs)


def test_on_still_attaches():
    a = fake_app(True, '/tmp')
    App._add_files_threaded(a, ['a.mkv'], ['a.eng.srt'])
    assert a.attached == ['a.eng.srt']


def test_off_does_not_offer_the_folder_prompt():
    with tempfile.TemporaryDirectory() as d:
        Path(d, 'a.eng.srt').write_text('x')
        a = fake_app(False, d)
        with mock.patch('video_converter.messagebox.askyesno') as ask:
            App._offer_subtitle_association(a)
        ask.assert_not_called()


def test_on_still_offers_the_folder_prompt():
    with tempfile.TemporaryDirectory() as d:
        Path(d, 'a.eng.srt').write_text('x')
        a = fake_app(True, d)
        with mock.patch('video_converter.messagebox.askyesno', return_value=False) as ask:
            App._offer_subtitle_association(a)
        ask.assert_called_once()
