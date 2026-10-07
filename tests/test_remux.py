"""Remux engine: what gets written is decided here, so pin it down."""
from pathlib import Path
from modules import remux as R


def _t(tid, ttype, lang='eng', **kw):
    t = dict(id=tid, type=ttype, codec='x', lang=lang, name='', default=False,
             forced=False, commentary=False, hi=False, channels=None, pixels='', set={})
    t.update(kw)
    return t


MAIN = dict(sid=1, path=Path('/m.mkv'), main=True,
            tracks=[_t(0, 'video'), _t(1, 'audio'), _t(2, 'audio', 'spa'), _t(3, 'subtitles')])
DONOR = dict(sid=7, path=Path('/d.mkv'), main=False,
             tracks=[_t(0, 'video'), _t(1, 'audio'), _t(2, 'audio', commentary=True)])
SRT = dict(sid=9, path=Path('/m.eng.forced.srt'), main=False,
           tracks=[_t(0, 'subtitles', 'und', set={'lang': 'eng', 'forced': True})])


def test_drop_a_track_and_add_one_from_a_donor():
    order = [(1, 0), (1, 1), (7, 2), (1, 3)]
    cmd = R.build_command([MAIN, DONOR], order, '/o.mkv')
    i_main, i_donor = cmd.index('/m.mkv'), cmd.index('/d.mkv')
    main_args, donor_args = cmd[:i_main], cmd[i_main:i_donor]
    assert main_args[main_args.index('--audio-tracks') + 1] == '1'      # spa dropped
    assert '--no-video' in donor_args and '--no-subtitles' in donor_args
    assert donor_args[donor_args.index('--audio-tracks') + 1] == '2'
    # an added file never brings its chapters/tags into this one
    assert {'--no-chapters', '--no-global-tags', '--no-attachments'} <= set(donor_args)
    assert '--no-global-tags' not in main_args       # the DOCFLIX stamp lives there
    assert cmd[-1] == '0:0,0:1,1:2,0:3'


def test_untouched_donor_is_not_opened_and_file_indices_close_up():
    order = [(1, 0), (1, 1), (9, 0)]
    cmd = R.build_command([MAIN, DONOR, SRT], order, '/o.mkv')
    assert '/d.mkv' not in cmd
    assert cmd[-1] == '0:0,0:1,1:0'
    assert ['--language', '0:eng'] == cmd[cmd.index('--language'):cmd.index('--language') + 2]
    assert '--forced-display-flag' in cmd


def test_tags_from_name():
    assert R.tags_from_name('Show - S01E01.eng.forced.srt') == {'lang': 'eng', 'forced': True}
    assert R.tags_from_name('x.spa.sdh.srt') == {'lang': 'spa', 'hi': True}
    assert R.tags_from_name('Plain Name.srt') == {}


def test_insert_keeps_type_groups_together():
    types = {(1, 0): 'video', (1, 1): 'audio', (1, 3): 'subtitles'}
    order = [(1, 0), (1, 1), (1, 3)]
    assert R.insert_pos(order, 'audio', types.get) == 2     # after the audio, before subs
    assert R.insert_pos(order, 'subtitles', types.get) == 3
