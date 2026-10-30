#!/usr/bin/env python3
"""Subtitle Editor: "Ignore in this file", and highlight modes across restarts.

Tony, 2026-10-30. Drives the real editor (run under xvfb-run). Uses a temporary
HOME, so the real preferences are never touched.
"""
import gc, json, os, sys, tempfile

home = tempfile.mkdtemp()
os.environ['HOME'] = home
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from tkinterdnd2 import TkinterDnD
from modules import standalone, subtitle_editor as se

SRT = ("1\n00:00:01,000 --> 00:00:02,000\nThe zzqxy went to NASA today.\n\n"
       "2\n00:00:03,000 --> 00:00:04,000\nAnother zzqxy and a blorpt.\n\n")


def closure(name):
    hits = [f for f in gc.get_objects() if callable(f)
            and getattr(f, '__name__', '') == name
            and getattr(f, '__module__', '') == se.__name__]
    assert hits, name
    return hits[-1]


def cell(fn, name):
    return fn.__closure__[fn.__code__.co_freevars.index(name)].cell_contents


def main():
    a = os.path.join(home, 'a.srt')
    open(a, 'w').write(SRT)
    root = TkinterDnD.Tk()
    app = standalone.StandaloneContext(root)
    app.add_log = lambda *x, **k: None
    app.open_batch_filter = lambda: None
    assert app.sub_spell_highlight is False

    def body():
        try:
            refresh = closure('refresh_tree')
            ignore = closure('_ignore_from_tree')
            known = closure('_known_caps')
            words = cell(closure('_drop_ignored_spell'), 'spell_error_words')
            indices = cell(closure('_drop_ignored_spell'), 'spell_error_indices')
            cues = lambda: cell(closure('_has_unsaved_changes'), 'cues')
            scanned = cell(refresh, 'spell_scanned')

            # Turn both modes on through the real toggles' effect, then restart
            scanned[0] = True
            app.sub_caps_highlight = app.sub_spell_highlight = True
            app.save_preferences()
            again = standalone.StandaloneContext(root)
            assert again.sub_caps_highlight and again.sub_spell_highlight
            print('ok  both modes survive a restart', flush=True)

            refresh(cues())
            flat = {w for ws in words.values() for w in ws}
            assert {'zzqxy', 'blorpt'} <= flat, flat
            ignore('zzqxy')
            flat = {w for ws in words.values() for w in ws}
            assert 'zzqxy' not in flat and 'blorpt' in flat, flat
            assert 0 in indices and 1 in indices      # NASA still flags cue 0
            print('ok  ignore clears every cue with that word, leaves the rest', flush=True)

            ignore('NASA')
            assert 'NASA' in known()
            assert not se.scan_allcaps_words(cues(), known())[0]
            assert 0 not in indices and 1 in indices  # both scans cleared
            print('ok  ignore works for an ALL CAPS word', flush=True)

            assert 'zzqxy' not in app.custom_spell_words
            assert 'zzqxy' not in app.custom_cap_words
            assert 'NASA' not in app.custom_cap_words
            print('ok  nothing reached the dictionary', flush=True)

            closure('on_drop_subtitle')(type('E', (), {'data': a}))
            for _ in range(30):
                root.update(); root.after(10)
            flat = {w for ws in words.values() for w in ws}
            assert 'zzqxy' in flat and se.scan_allcaps_words(cues(), known())[0]
            print('ok  loading a file clears the ignores', flush=True)
            print('PASS', flush=True)
            os._exit(0)
        except BaseException:
            import traceback
            traceback.print_exc()
            os._exit(1)

    root.after(1500, body)
    se.open_standalone_subtitle_editor(app, auto_external=a)
    root.mainloop()


if __name__ == '__main__':
    main()
