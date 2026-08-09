"""
write_encode_stamp() — add DOCFLIX_ENCODE to an existing MKV, in place.

Exists because re-encoding a file just to recover a *label* is absurd:
mkvpropedit edits the Matroska header without touching the file body, so
stamping a folder of 8 GB episodes costs seconds and cannot degrade video.

⚠️ The subtle part is the MERGE. `mkvpropedit --tags all:<file>` REPLACES the
whole Tags section, and real files carry ENCODER / DURATION / per-track
statistics tags in there. Writing only our stamp would silently delete them —
a data-loss bug that would look like a working feature.
"""

import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.skipif(
    not (shutil.which('ffmpeg') and shutil.which('mkvpropedit')
         and shutil.which('mkvextract')),
    reason='needs ffmpeg + mkvtoolnix',
)

STAMP = 'CRF32 hevc_nvenc p4 10bit ac3@384k v3.18.2'


def _mkv(path, mp4=False, depth10=False):
    subprocess.run(
        ['ffmpeg', '-v', 'error', '-y',
         '-f', 'lavfi', '-i', 'testsrc=size=320x240:rate=24:duration=1',
         '-pix_fmt', 'yuv420p10le' if depth10 else 'yuv420p',
         '-c:v', 'libx264', '-preset', 'ultrafast']
        + (['-f', 'mp4'] if mp4 else [])
        + [str(path)], check=True, timeout=180)
    return str(path)


def test_stamp_round_trips(tmp_path):
    from modules.utils import write_encode_stamp, read_encode_stamp
    f = _mkv(tmp_path / 'a.mkv')
    assert write_encode_stamp(f, STAMP) is True
    assert read_encode_stamp(f) == STAMP


def test_merge_preserves_existing_tags(tmp_path):
    """⚠️ The data-loss bug this function is shaped to avoid."""
    from modules.utils import write_encode_stamp
    f = _mkv(tmp_path / 'b.mkv')
    before = subprocess.run(['mkvextract', 'tags', f],
                            capture_output=True, text=True).stdout
    assert 'ENCODER' in before, 'fixture has no pre-existing tags to protect'

    write_encode_stamp(f, STAMP)
    after = subprocess.run(['mkvextract', 'tags', f],
                           capture_output=True, text=True).stdout
    assert 'ENCODER' in after, 'merge destroyed the existing ENCODER tag'
    assert 'DURATION' in after, 'merge destroyed per-track statistics tags'
    assert 'DOCFLIX_ENCODE' in after


def test_restamping_replaces_never_duplicates(tmp_path):
    from modules.utils import write_encode_stamp, read_encode_stamp
    f = _mkv(tmp_path / 'c.mkv')
    write_encode_stamp(f, STAMP)
    write_encode_stamp(f, 'CRF28 hevc_nvenc p6 10bit ac3@448k v3.18.2')
    tags = subprocess.run(['mkvextract', 'tags', f],
                          capture_output=True, text=True).stdout
    assert tags.count('DOCFLIX_ENCODE') == 1, 'duplicate stamps in one file'
    assert read_encode_stamp(f).startswith('CRF28')


def test_mp4_wearing_mkv_is_detected(tmp_path):
    """The container bug (fixed 3.18.2) left a lot of these on disk. They
    cannot hold a stamp and must never be reported as if they did."""
    from modules.utils import is_matroska, write_encode_stamp
    real = _mkv(tmp_path / 'real.mkv')
    fake = _mkv(tmp_path / 'fake.mkv', mp4=True)
    assert is_matroska(real) is True
    assert is_matroska(fake) is False
    assert write_encode_stamp(fake, STAMP) is False, \
        'claimed success on a file that cannot carry the stamp'


def test_strip_then_stamp_still_agree(tmp_path):
    """The two tag helpers must not fight: strip preserves, write merges."""
    from modules.utils import (write_encode_stamp, read_encode_stamp,
                               strip_mkv_tags_keeping_stamp)
    f = _mkv(tmp_path / 'd.mkv')
    write_encode_stamp(f, STAMP)
    strip_mkv_tags_keeping_stamp(f)
    assert read_encode_stamp(f) == STAMP, \
        'strip ate a stamp that write_encode_stamp had just added'


# ── the CLI's honesty check ──────────────────────────────────────────────────

def test_check_catches_a_lying_tag():
    """An asserted stamp is only as good as its agreement with the file.
    Half the tag IS measurable, so contradictions must be caught."""
    import importlib.util
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    spec = importlib.util.spec_from_file_location(
        'docflix_stamp', os.path.join(root, 'docflix_stamp.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # measured: (vcodec, depth, acodec, akbps)
    assert mod.check(STAMP, ('hevc', '10', 'ac3', 384)) == []
    assert mod.check(STAMP, ('h264', '8', 'ac3', 192)), 'accepted a wrong file'
    assert any('10bit' in b for b in mod.check(STAMP, ('hevc', '8', 'ac3', 384)))
    assert any('384k' in b for b in mod.check(STAMP, ('hevc', '10', 'ac3', 192)))
    # CRF and preset are NOT verifiable — must never be claimed as checked
    assert mod.check('CRF18 hevc_nvenc p7 10bit ac3@384k v1',
                     ('hevc', '10', 'ac3', 384)) == []
