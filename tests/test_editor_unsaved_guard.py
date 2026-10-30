#!/usr/bin/env python3
"""Replacing the open subtitle must not silently throw away unsaved edits.

Tony, 2026-10-30: edit a subtitle, drag another file in without saving, and the
edits are gone with no warning. Only closing the window ever asked.

Drives the real editor window (needs a display, or run under xvfb-run). The
handlers are closures, so they are found through gc and called directly.
"""
import gc, os, sys, tempfile, tkinter as tk
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from modules import subtitle_editor as se

SRT = "1\n00:00:01,000 --> 00:00:02,000\n%s\n\n"


def closure(name):
    hits = [f for f in gc.get_objects()
            if callable(f) and getattr(f, '__name__', '') == name
            and getattr(f, '__module__', '') == se.__name__]
    assert hits, name
    return hits[-1]


def cell(fn, name):
    return fn.__closure__[fn.__code__.co_freevars.index(name)].cell_contents


def main():
    tmp = tempfile.mkdtemp()
    a, b = os.path.join(tmp, 'a.srt'), os.path.join(tmp, 'b.srt')
    open(a, 'w').write(SRT % 'first file')
    open(b, 'w').write(SRT % 'second file')

    from tkinterdnd2 import TkinterDnD
    root = TkinterDnD.Tk()
    app = mock.MagicMock(root=root, custom_cap_words=[], custom_replacements=[],
                         custom_ad_patterns=[], custom_spell_words=[], _prefs={},
                         _standalone_mode=False)
    def body():
        try:
            drop = closure('on_drop_subtitle')
            dirty = closure('_has_unsaved_changes')
            save_as = closure('do_save_as')
            current_path = cell(closure('do_save_file'), 'current_path')

            def text():
                return cell(dirty, 'cues')[0]['text']

            def dropped():
                drop(SimpleNamespace(data=b))
                for _ in range(20):
                    root.update(); root.after(10)

            def fresh():
                """Back to a.srt, clean, then make one unsaved edit."""
                open(a, 'w').write(SRT % 'first file')
                with mock.patch.object(se.messagebox, 'askyesnocancel', return_value=False):
                    drop(SimpleNamespace(data=a))
                    for _ in range(20):
                        root.update(); root.after(10)
                assert text() == 'first file' and not dirty()
                cell(dirty, 'cues')[0]['text'] = 'EDITED'
                assert dirty()

            # Cancel keeps the edits and the file open
            fresh()
            with mock.patch.object(se.messagebox, 'askyesnocancel', return_value=None) as q:
                dropped()
            assert q.call_count == 1 and text() == 'EDITED', 'cancel'
            print('ok  cancel keeps the edits')

            # No discards on request
            with mock.patch.object(se.messagebox, 'askyesnocancel', return_value=False):
                dropped()
            assert text() == 'second file', 'no'
            assert 'first file' in open(a).read()
            print('ok  no discards and loads the new file')

            # Yes saves, then loads
            fresh()
            with mock.patch.object(se.messagebox, 'askyesnocancel', return_value=True):
                dropped()
            assert 'EDITED' in open(a).read() and text() == 'second file', 'yes'
            print('ok  yes saves, then loads the new file')

            # Yes with a save that fails must not load anything
            fresh()
            with mock.patch.object(se.messagebox, 'askyesnocancel', return_value=True), \
                    mock.patch.object(se.messagebox, 'showerror') as err, \
                    mock.patch('builtins.open', side_effect=OSError('disk full')):
                drop(SimpleNamespace(data=b))
                for _ in range(20):
                    root.update(); root.after(10)
            assert text() == 'EDITED' and err.called, 'failed save'
            print('ok  a failed save keeps the edits and stays put')

            # Clean file: no question at all
            open(a, 'w').write(SRT % 'first file')
            with mock.patch.object(se.messagebox, 'askyesnocancel', return_value=False):
                drop(SimpleNamespace(data=a))
                for _ in range(20):
                    root.update(); root.after(10)
            with mock.patch.object(se.messagebox, 'askyesnocancel') as q:
                dropped()
            assert not q.called and text() == 'second file', 'clean'
            print('ok  a clean file is not asked about')

            # Save As counts as saved
            fresh()
            c = os.path.join(tmp, 'c.srt')
            with mock.patch.object(se, 'ask_save_file', return_value=c):
                save_as()
            assert not dirty() and current_path[0] == c, 'save as'
            print('ok  save as clears the unsaved state')
            print('PASS', flush=True)
            os._exit(0)
        except BaseException:
            import traceback
            traceback.print_exc()
            os._exit(1)

    # The editor blocks in wait_window, so the checks run from inside it.
    root.after(1200, body)
    se.open_standalone_subtitle_editor(app, auto_external=a)


if __name__ == '__main__':
    main()
