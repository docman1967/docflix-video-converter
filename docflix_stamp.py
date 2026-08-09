#!/usr/bin/env python3
"""
docflix_stamp — write a DOCFLIX_ENCODE stamp into MKVs that are missing one.

WHY THIS EXISTS
---------------
The stamp is normally written by the encoder, derived from the settings
actually used. But files encoded before 3.17.0 — and every file whose stamp
was destroyed by the Media Processor's MP4 container bug (fixed in 3.18.2) —
carry nothing, even though they were encoded with known settings.

Re-encoding them to recover a label would be absurd. mkvpropedit edits the
Matroska header IN PLACE: no remux, no re-encode, no rewrite of the file body.
Stamping a folder of 8 GB episodes takes seconds and cannot touch the video.

⚠️ THE HONESTY PROBLEM, AND HOW THIS HANDLES IT
-----------------------------------------------
An encoder-written stamp is measured truth. A stamp written here is something
you asserted, and "a file that confidently misdescribes its own encode is
worse than one carrying no tag at all" (build_encode_tag, video_converter.py).

So this does not just take your word for it. Half the tag IS measurable from
the file — bit depth, audio codec, audio bitrate, video codec — and those are
checked against what you are claiming. A mismatch is reported and the file is
SKIPPED unless you pass --force.

What cannot be verified, ever, because it leaves no trace in the file:
CRF value, encoder preset, AQ, and hevc_nvenc-vs-libx265. You are on your own
for those; only stamp folders you actually know the history of.

USAGE
-----
    # preview (default) — shows every mismatch, writes nothing
    ./docflix_stamp.py "CRF32 hevc_nvenc p4 10bit ac3@384k v3.18.2" /path/to/show

    ./docflix_stamp.py "<tag>" DIR [DIR...] --commit
    ./docflix_stamp.py "<tag>" DIR --commit --force     # ignore mismatches
    ./docflix_stamp.py "<tag>" DIR --commit --overwrite # replace existing stamps

Files that are MP4 wearing a .mkv extension are reported separately — they
CANNOT hold a stamp (MP4 drops custom keys). Remux them to real MKV first
with the Media Processor (container .mkv), which also recovers SRT subtitles
from mov_text.
"""

import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from modules.utils import (           # noqa: E402
    is_matroska, read_encode_stamp, write_encode_stamp,
)

G, Y, R, B, D, NC = ("\033[92m", "\033[93m", "\033[91m",
                     "\033[94m", "\033[90m", "\033[0m")

VIDEO_EXTS = {'.mkv'}


def measure(path):
    """What the file demonstrably IS: (vcodec, depth, acodec, akbps)."""
    try:
        r = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries',
             'stream=codec_type,codec_name,pix_fmt,bit_rate',
             '-of', 'json', path],
            capture_output=True, text=True, timeout=60)
        data = json.loads(r.stdout) if r.stdout.strip() else {}
    except Exception:
        return None

    v = a = None
    depth = akbps = None
    for s in data.get('streams', []):
        if s.get('codec_type') == 'video' and v is None:
            v = (s.get('codec_name') or '').lower()
            pf = (s.get('pix_fmt') or '')
            depth = '10' if ('10' in pf or '12' in pf) else '8'
        elif s.get('codec_type') == 'audio' and a is None:
            a = (s.get('codec_name') or '').lower()
            br = s.get('bit_rate')
            if br:
                akbps = int(int(br) / 1000)
    return v, depth, a, akbps


def check(tag, m):
    """Disagreements between the claimed tag and the measured file."""
    if not m:
        return ['could not probe']
    v, depth, a, akbps = m
    t = tag.lower()
    bad = []

    if '10bit' in t and depth == '8':
        bad.append(f'tag says 10bit, file is 8-bit ({v})')
    if '8bit' in t and depth == '10':
        bad.append(f'tag says 8bit, file is 10-bit ({v})')

    # video codec family
    if 'hevc' in t or 'h265' in t or 'x265' in t:
        if v and v not in ('hevc', 'h265'):
            bad.append(f'tag says HEVC, file is {v}')
    elif ('h264' in t or 'x264' in t or 'avc' in t) and v and v != 'h264':
        bad.append(f'tag says H.264, file is {v}')

    # audio codec + bitrate, e.g. "ac3@384k"
    for codec in ('ac3', 'eac3', 'aac', 'opus', 'flac', 'dts'):
        if f'{codec}@' in t:
            if a and a != codec:
                bad.append(f'tag says {codec}, file audio is {a}')
            want = t.split(f'{codec}@', 1)[1].split()[0].rstrip('k')
            if want.isdigit() and akbps and abs(akbps - int(want)) > 40:
                bad.append(f'tag says {want}k audio, file is ~{akbps}k')
            break
    return bad


def walk(targets):
    out = []
    for t in targets:
        if os.path.isfile(t):
            if os.path.splitext(t)[1].lower() in VIDEO_EXTS:
                out.append(t)
        else:
            for dirpath, _dirs, files in os.walk(t):
                for f in sorted(files):
                    if os.path.splitext(f)[1].lower() in VIDEO_EXTS:
                        out.append(os.path.join(dirpath, f))
    return out


def main():
    ap = argparse.ArgumentParser(
        description='Write DOCFLIX_ENCODE into MKVs, in place, no remux.')
    ap.add_argument('tag', help='the stamp, e.g. '
                                '"CRF32 hevc_nvenc p4 10bit ac3@384k v3.18.2"')
    ap.add_argument('targets', nargs='+', help='folders and/or .mkv files')
    ap.add_argument('--commit', action='store_true',
                    help='actually write (default: preview)')
    ap.add_argument('--force', action='store_true',
                    help='stamp even when the tag contradicts the file')
    ap.add_argument('--overwrite', action='store_true',
                    help='replace an existing stamp (default: skip)')
    args = ap.parse_args()

    files = walk(args.targets)
    if not files:
        sys.exit(f'{R}no .mkv files found{NC}')

    print(f'{B}Tag:{NC}   {args.tag}')
    print(f'{B}Files:{NC} {len(files):,}\n')

    todo, already, notmkv, mismatch = [], [], [], []
    for p in files:
        if not is_matroska(p):
            notmkv.append(p)
            continue
        existing = read_encode_stamp(p)
        if existing and not args.overwrite:
            already.append((p, existing))
            continue
        bad = check(args.tag, measure(p))
        if bad and not args.force:
            mismatch.append((p, bad))
            continue
        todo.append(p)

    if notmkv:
        print(f'{R}✗ {len(notmkv)} file(s) are MP4 wearing .mkv — '
              f'cannot hold a stamp:{NC}')
        for p in notmkv[:8]:
            print(f'   {os.path.basename(p)[:72]}')
        if len(notmkv) > 8:
            print(f'   {D}… and {len(notmkv) - 8} more{NC}')
        print(f'   {D}remux to real MKV first (Media Processor, '
              f'container .mkv){NC}\n')

    if mismatch:
        print(f'{Y}⚠ {len(mismatch)} file(s) contradict the tag — skipped '
              f'(use --force to override):{NC}')
        for p, bad in mismatch[:8]:
            print(f'   {os.path.basename(p)[:60]}')
            for b in bad:
                print(f'      {Y}{b}{NC}')
        if len(mismatch) > 8:
            print(f'   {D}… and {len(mismatch) - 8} more{NC}')
        print()

    if already:
        print(f'{D}• {len(already)} already stamped (skipped; '
              f'--overwrite to replace){NC}')
        for p, s in already[:3]:
            print(f'   {D}{os.path.basename(p)[:52]} → {s}{NC}')
        print()

    print(f'{G}→ {len(todo)} file(s) to stamp{NC}')
    if not args.commit:
        print(f'\n{Y}Preview only. Add --commit to write.{NC}')
        return
    if not todo:
        return

    ok = fail = 0
    for i, p in enumerate(todo, 1):
        if write_encode_stamp(p, args.tag,
                              lambda m, l='INFO': None):
            ok += 1
        else:
            fail += 1
            print(f'   {R}failed: {os.path.basename(p)}{NC}')
        if i % 25 == 0 or i == len(todo):
            print(f'   {i:,}/{len(todo):,}', flush=True)
    print(f'\n{G}✓ stamped {ok:,}{NC}'
          + (f'   {R}failed {fail:,}{NC}' if fail else ''))


if __name__ == '__main__':
    main()
