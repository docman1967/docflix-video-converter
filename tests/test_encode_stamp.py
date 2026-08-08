#!/usr/bin/env python3
"""Regression test: the strip-tags post-step must not eat the encode stamp.

WHY THIS FILE EXISTS
────────────────────
Tony, 2026-08-08, minutes after the DOCFLIX_ENCODE stamp shipped: *"so for this
file I had the tag settings checked but I don't see anything written to the
metadata."*

Two features that each worked perfectly, cancelling each other out:

  1. ffmpeg's `-metadata DOCFLIX_ENCODE=...` writes into the Matroska **Tags**
     section.
  2. The strip-metadata post-step runs `mkvpropedit --tags all:`, which removes
     that entire section.

So with strip-tags enabled — which is Tony's normal setting — the stamp was
written and silently wiped moments later. **Nothing errored, nothing logged,
and the encode looked completely successful.** The same silent-wrongness this
codebase keeps producing.

⚠️ NEITHER FEATURE'S OWN TESTS COULD HAVE CAUGHT THIS. The stamp test encoded a
file and read the tag back — correct, because it never ran the post-step. The
interaction is the bug, so the test has to exercise the interaction.

The fix reads the stamp back BEFORE stripping and rewrites it after, rather than
re-deriving it, so the restored value is byte-for-byte what ffmpeg actually
wrote and cannot drift from the encode it describes.

    python3 tests/test_encode_stamp.py
"""

import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

STAMP = "CRF32 hevc_nvenc p6 10bit ac3@384k vTEST"


def have(*tools):
    return all(shutil.which(t) for t in tools)


def read_stamp(path):
    r = subprocess.run(['ffprobe', '-v', 'quiet', '-show_entries',
                        'format_tags=DOCFLIX_ENCODE', '-of',
                        'default=nw=1:nk=1', path],
                       capture_output=True, text=True, timeout=30)
    return (r.stdout or '').strip() or None


def make_mkv(path, with_stamp):
    """A 1-second synthetic MKV, so the test needs no media on disk."""
    cmd = ['ffmpeg', '-v', 'error', '-y',
           '-f', 'lavfi', '-i', 'testsrc=size=320x240:rate=10:duration=1',
           '-c:v', 'libx264', '-preset', 'ultrafast']
    if with_stamp:
        cmd += ['-metadata', f'DOCFLIX_ENCODE={STAMP}']
    cmd += [path]
    subprocess.run(cmd, capture_output=True, timeout=120)


def main():
    if not have('ffmpeg', 'ffprobe', 'mkvpropedit'):
        print("\n  ffmpeg/ffprobe/mkvpropedit not all present — skipping\n")
        sys.exit(0)

    from video_converter import VideoConverterApp   # noqa: E402
    strip = VideoConverterApp.strip_mkv_tags_keeping_stamp

    fails = 0
    tmp = tempfile.mkdtemp(prefix='docflix_stamp_test_')
    print()
    try:
        # ── the reported bug ────────────────────────────────────────────
        p = os.path.join(tmp, 'stamped.mkv')
        make_mkv(p, with_stamp=True)
        before = read_stamp(p)
        ok_written = before == STAMP
        fails += not ok_written
        print(f"    {'ok  ' if ok_written else 'FAIL'} ffmpeg writes the stamp "
              f"({before!r})")

        strip(p)
        after = read_stamp(p)
        ok_kept = after == STAMP
        fails += not ok_kept
        print(f"    {'ok  ' if ok_kept else 'FAIL'} stamp SURVIVES "
              f"`mkvpropedit --tags all:` ({after!r})")

        # ⚠️ The strip must still actually strip. If it quietly stopped
        # removing ffmpeg's BPS/DURATION/ENCODER bloat, this test would still
        # be green while the feature was broken the other way.
        r = subprocess.run(['ffprobe', '-v', 'quiet', '-show_entries',
                            'format_tags', '-of', 'json', p],
                           capture_output=True, text=True, timeout=30)
        import json
        tags = (json.loads(r.stdout or '{}').get('format', {})
                .get('tags') or {})
        # ⚠️ `encoder` is NOT a Matroska Tag — it is the WritingApp header
        # element, which ffprobe surfaces alongside format tags but
        # `--tags all:` cannot remove. It survives a strip in production too.
        # Asserting it away would be asserting a falsehood; the first version
        # of this check did exactly that and failed against correct behaviour.
        HEADER_FIELDS = {'encoder'}
        others = {k: v for k, v in tags.items()
                  if k != 'DOCFLIX_ENCODE' and k not in HEADER_FIELDS}
        ok_stripped = not others
        fails += not ok_stripped
        print(f"    {'ok  ' if ok_stripped else 'FAIL'} everything ELSE is "
              f"still stripped ({others or 'nothing left but header fields'})")

        # ── must not invent a stamp that was never there ─────────────────
        p2 = os.path.join(tmp, 'unstamped.mkv')
        make_mkv(p2, with_stamp=False)
        strip(p2)
        ok_none = read_stamp(p2) is None
        fails += not ok_none
        print(f"    {'ok  ' if ok_none else 'FAIL'} a file with NO stamp does "
              f"not gain one")

        # ── logging is optional (it is a staticmethod for testability) ───
        seen = []
        p3 = os.path.join(tmp, 'logged.mkv')
        make_mkv(p3, with_stamp=True)
        strip(p3, lambda m, l='INFO': seen.append(m))
        ok_log = any('Restored encode tag' in m for m in seen)
        fails += not ok_log
        print(f"    {'ok  ' if ok_log else 'FAIL'} the restore is reported in "
              f"the log, not silent")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    total = 5
    print(f"\n  {total - fails}/{total} pass\n")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
