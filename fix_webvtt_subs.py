#!/usr/bin/env python3
"""
Convert ffmpeg-unreadable text subtitle tracks in an MKV into SRT, in place.

WHY THIS EXISTS (Tony, 2026-09-02)
──────────────────────────────────
A WEB-DL screener arrived carrying its subtitles as WebVTT — Matroska CodecID
`S_TEXT/WEBVTT`, which is what streaming services serve natively and what
mkvmerge happily muxes.

⚠️ ffmpeg CANNOT READ IT BACK OUT. Tested against four builds on this box —
6.1.1, jellyfin 7.1.3, a March-2026 git build, and Emby's — all four produce a
ZERO-BYTE output and exit 0. It is not a version problem; ffmpeg can decode a
standalone .vtt and can *write* WebVTT into Matroska, but its Matroska demuxer
will not give those bytes back.

⚠️⚠️ AND THE STREAM'S MERE PRESENCE KILLS THE WHOLE ENCODE:

    Error sending frames to consumers: Function not implemented
    Task finished with error code: -38

Not "the subtitle is dropped" — no output file at all. So an affected film is
simply unprocessable by the Media Suite until this is run on it.

WHY A SEPARATE TOOL RATHER THAN A PIPELINE CHANGE
─────────────────────────────────────────────────
Subtitle mapping is spread across 8+ call sites in converter.py and
media_processor.py. Teaching all of them about unreadable streams means editing
the encode path of a tool Tony uses daily, to serve roughly 1 file in 120
(measured). This makes the file ORDINARY instead, and the pipeline never learns
anything new. Same shape as docflix_stamp.py: one tool, one job.

WHAT IT DOES
────────────
    1. EXTRACT   mkvextract tracks  ->  .vtt      (ffmpeg cannot do this step)
    2. CONVERT   _vtt_to_srt()      ->  .srt      (the Suite's OWN parser)
    3. REINSERT  mkvmerge --no-subtitles + the .srt

⚠️ Step 2 uses the Suite's `_vtt_to_srt`, NOT ffmpeg, deliberately.
subtitle_editor.py already documents why: "ffmpeg's webvtt demuxer silently
emits an EMPTY SRT for HLS/broadcast .vtt carrying an X-TIMESTAMP-MAP header".
Converting via ffmpeg happened to work on the first file tested only because it
had no such header. One import, no second copy of the logic to drift.

⚠️ Video and audio are STREAM COPIED. Nothing is re-encoded, ever.

SAFETY
──────
Nothing is replaced until it has been verified by CONTENT:
  * video and audio stream MD5s identical to the source
  * subtitle cue count preserved
  * ⚠️ existing Matroska tags preserved — the DOCFLIX_ENCODE stamp lives there,
    and three separate bugs have already eaten that stamp. The memory note says
    to expect a fourth and to look DOWNSTREAM of the encode. This tool is
    downstream of the encode. So it is checked, not assumed.
The original is left untouched unless --replace is given AND every check passed.
"""

import argparse
import json
import re
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from modules.subtitle_editor import _vtt_to_srt          # noqa: E402

# Matroska CodecIDs that are text subtitles ffmpeg's demuxer refuses to hand
# back. Keyed by CodecID because that is what mkvmerge reports and what is
# actually in the container — ffprobe just says "unknown".
UNREADABLE_TEXT_CODECS = {
    'S_TEXT/WEBVTT': '.vtt',
}


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def stream_md5(path, spec):
    """MD5 of one stream's packets, stream-copied. The honest equality check.

    ⚠️ Compares CONTENT, not size or duration. The backup-integrity lesson:
    a plausible-looking file is not a verified one.
    """
    r = run(['ffmpeg', '-v', 'error', '-i', path, '-map', spec,
             '-c', 'copy', '-f', 'md5', '-'])
    out = (r.stdout or '').strip()
    return out.split('=', 1)[1] if '=' in out else None


def get_tags(path):
    r = run(['mkvextract', 'tags', path])
    return (r.stdout or '').strip()


# ⚠️ Cheap pre-filter. The Matroska CodecID is a literal ASCII string in the
# file header, so a bounded read finds candidates ~20x faster than spawning
# mkvmerge -J per file. Scanning Tony's Process folder went from 1m35s to
# seconds. mkvmerge still does the authoritative identification on anything this
# flags — this only decides who is worth asking about.
_HEADER_BYTES = 2 * 1024 * 1024


def might_have_unreadable_subs(path):
    try:
        with open(path, 'rb') as f:
            head = f.read(_HEADER_BYTES)
    except OSError:
        return True                     # unreadable header -> let mkvmerge decide
    return any(c.encode('ascii') in head for c in UNREADABLE_TEXT_CODECS)


def find_unreadable_subs(path):
    """[(mkv_track_id, codec_id, language, track_name, default, forced), ...]"""
    r = run(['mkvmerge', '-J', path])
    if r.returncode not in (0, 1):
        raise RuntimeError('mkvmerge -J failed: %s' % (r.stderr or '').strip())
    data = json.loads(r.stdout)
    out = []
    for t in data.get('tracks', []):
        if t.get('type') != 'subtitles':
            continue
        p = t.get('properties', {})
        cid = p.get('codec_id', '')
        if cid in UNREADABLE_TEXT_CODECS:
            out.append({
                'id': t.get('id'),
                'codec_id': cid,
                'language': p.get('language') or 'und',
                'name': p.get('track_name') or '',
                'default': bool(p.get('default_track')),
                'forced': bool(p.get('forced_track')),
            })
    return out


def cue_count(srt_path):
    with open(srt_path, 'r', encoding='utf-8-sig', errors='replace') as f:
        return f.read().count(' --> ')


# ── zero-duration cue repair ────────────────────────────────────────────────
#
# ⚠️ THIS IS NOT COSMETIC — WITHOUT IT, LINES ARE SILENTLY LOST.
#
# The Mongoose's WebVTT contains 5 cues whose start and end are identical:
#
#     00:45:40.476 --> 00:45:40.476   "No."
#     01:03:11.822 --> 01:03:11.822   "F**k!"
#
# They never display — zero duration. And mkvmerge DROPS them on mux, correctly,
# because Matroska cannot carry a subtitle with no duration. So a naive
# extract→convert→remux quietly loses those lines: 1023 cues in, 1018 out, 7
# words gone, no error anywhere. Exactly the shape of the balance_lines bug that
# ate words from 3.8% of cues and was only ever caught by asserting counts.
#
# Subtitle Edit repairs 3 of the 5 when it converts the same file, which is what
# put me onto this. We repair all 5: give each a real duration, capped so it can
# never overlap the cue that follows. The output is then BETTER than the source —
# those lines display for the first time.
DEFAULT_CUE_MS = 1200          # a short line is readable in ~1.2s
MIN_GAP_MS = 40                # never butt straight up against the next cue

_TS_RE = re.compile(r'^(\d\d):(\d\d):(\d\d),(\d\d\d) --> (\d\d):(\d\d):(\d\d),(\d\d\d)\s*$')


def _ms(h, m, s, ms):
    return ((int(h) * 60 + int(m)) * 60 + int(s)) * 1000 + int(ms)


def _fmt(t):
    ms = t % 1000
    s = (t // 1000) % 60
    m = (t // 60000) % 60
    h = t // 3600000
    return '%02d:%02d:%02d,%03d' % (h, m, s, ms)


def repair_zero_durations(srt_text):
    """Give zero/negative-duration cues a real duration. Returns (text, n_fixed)."""
    blocks = re.split(r'\n\s*\n', srt_text.strip())
    parsed = []
    for b in blocks:
        lines = [l for l in b.splitlines() if l.strip() != '']
        ti = next((i for i, l in enumerate(lines) if _TS_RE.match(l)), None)
        if ti is None:
            parsed.append((None, None, b))
            continue
        g = _TS_RE.match(lines[ti]).groups()
        parsed.append((_ms(*g[:4]), _ms(*g[4:]), lines[ti + 1:]))

    starts = [p[0] for p in parsed if p[0] is not None]
    fixed = 0
    out = []
    idx = 0
    for i, (start, end, body) in enumerate(parsed):
        if start is None:
            out.append(body)
            continue
        if end <= start:
            nxt = next((parsed[j][0] for j in range(i + 1, len(parsed))
                        if parsed[j][0] is not None), None)
            end = start + DEFAULT_CUE_MS
            if nxt is not None and end > nxt - MIN_GAP_MS:
                end = max(start + 1, nxt - MIN_GAP_MS)
            fixed += 1
        idx += 1
        out.append('%d\n%s --> %s\n%s'
                   % (idx, _fmt(start), _fmt(end), '\n'.join(body)))
    void = len(starts)
    del void
    return '\n\n'.join(out) + '\n', fixed


def fix(path, replace=False, dry_run=False, verbose=True):
    def say(msg):
        if verbose:
            print(msg)

    if not os.path.isfile(path):
        say('  not a file: %s' % path)
        return False

    subs = find_unreadable_subs(path)
    if not subs:
        say('  no unreadable subtitle tracks — nothing to do')
        return True

    say('  %d unreadable subtitle track(s):' % len(subs))
    for s in subs:
        say('    track %s  %s  lang=%s%s' % (s['id'], s['codec_id'],
                                             s['language'],
                                             '  (default)' if s['default'] else ''))
    if dry_run:
        say('  --dry-run: stopping here')
        return True

    workdir = tempfile.mkdtemp(prefix='webvttfix-')
    made = []
    try:
        # ── 1 + 2: extract, then convert with the Suite's own parser ─────────
        for s in subs:
            ext = UNREADABLE_TEXT_CODECS[s['codec_id']]
            raw = os.path.join(workdir, 'track%s%s' % (s['id'], ext))
            r = run(['mkvextract', 'tracks', path, '%s:%s' % (s['id'], raw)])
            if r.returncode != 0 or not os.path.exists(raw):
                say('  ✗ mkvextract failed on track %s: %s'
                    % (s['id'], (r.stderr or r.stdout or '').strip()[:200]))
                return False
            with open(raw, 'r', encoding='utf-8-sig', errors='replace') as f:
                srt_text = _vtt_to_srt(f.read())
            # ⚠️ Must happen BEFORE the mux — mkvmerge drops zero-duration cues.
            srt_text, n_fixed = repair_zero_durations(srt_text)
            srt = os.path.join(workdir, 'track%s.srt' % s['id'])
            with open(srt, 'w', encoding='utf-8') as f:
                f.write(srt_text)
            n = cue_count(srt)
            if n == 0:
                say('  ✗ conversion produced 0 cues for track %s — refusing' % s['id'])
                return False
            say('  extracted + converted track %s: %d cues%s'
                % (s['id'], n,
                   '  (repaired %d zero-duration)' % n_fixed if n_fixed else ''))
            s['srt'] = srt
            s['cues'] = n
            made.append(srt)

        # ── 3: remux — video/audio stream-copied, originals' subs dropped ────
        out = os.path.join(workdir, 'fixed.mkv')
        cmd = ['mkvmerge', '-o', out, '--no-subtitles', path]
        for s in subs:
            cmd += ['--language', '0:%s' % s['language']]
            if s['name']:
                cmd += ['--track-name', '0:%s' % s['name']]
            cmd += ['--default-track', '0:%s' % ('yes' if s['default'] else 'no'),
                    '--forced-track', '0:%s' % ('yes' if s['forced'] else 'no'),
                    s['srt']]
        r = run(cmd)
        if r.returncode not in (0, 1) or not os.path.exists(out):
            say('  ✗ mkvmerge remux failed: %s'
                % (r.stderr or r.stdout or '').strip()[:300])
            return False

        # ── VERIFY BY CONTENT, before anything is replaced ───────────────────
        ok = True
        for spec, label in (('0:v', 'video'), ('0:a', 'audio')):
            a, b = stream_md5(path, spec), stream_md5(out, spec)
            same = (a is not None and a == b)
            say('  %s %s stream md5 %s' % ('✓' if same else '✗', label,
                                           'identical' if same else
                                           'DIFFERS (%s vs %s)' % (a, b)))
            ok = ok and same

        got = run(['mkvmerge', '-J', out])
        newsubs = [t for t in json.loads(got.stdout).get('tracks', [])
                   if t.get('type') == 'subtitles']
        say('  %s subtitle tracks out: %d (expected %d)'
            % ('✓' if len(newsubs) == len(subs) else '✗', len(newsubs), len(subs)))
        ok = ok and len(newsubs) == len(subs)

        for t in newsubs:
            cid = t.get('properties', {}).get('codec_id')
            good = cid == 'S_TEXT/UTF8'
            say('  %s output codec_id %s' % ('✓' if good else '✗', cid))
            ok = ok and good

        # ⚠️ Count cues in the FINISHED FILE, not in the .srt we handed to
        # mkvmerge. Checking the input to a step cannot detect the step losing
        # things — and mkvmerge silently dropped 5 zero-duration cues the first
        # time this ran. Verify the artifact you are actually shipping.
        expect = sum(s['cues'] for s in subs)
        rt = os.path.join(workdir, 'roundtrip.srt')
        rr = run(['ffmpeg', '-v', 'error', '-y', '-i', out, '-map', '0:s', rt])
        actual = cue_count(rt) if os.path.exists(rt) else -1
        say('  %s cues in the finished file: %d (expected %d)'
            % ('✓' if actual == expect else '✗', actual, expect))
        ok = ok and (actual == expect)
        del rr

        # ⚠️ Tag preservation — the DOCFLIX_ENCODE stamp lives in Matroska tags
        # and has been eaten three times before by things exactly like this.
        before, after = get_tags(path), get_tags(out)
        had_stamp = 'DOCFLIX_ENCODE' in before
        kept_stamp = 'DOCFLIX_ENCODE' in after
        if had_stamp:
            say('  %s DOCFLIX_ENCODE stamp %s'
                % ('✓' if kept_stamp else '✗',
                   'preserved' if kept_stamp else 'LOST — refusing to replace'))
            ok = ok and kept_stamp
        else:
            say('  · no DOCFLIX_ENCODE stamp on the source (nothing to preserve)')

        if not ok:
            say('  ✗ verification FAILED — original left untouched')
            say('    candidate kept at: %s' % out)
            return False

        say('  ✓ all checks passed')
        if not replace:
            final = os.path.splitext(path)[0] + '.fixed.mkv'
            os.replace(out, final)
            say('  wrote %s' % final)
            say('  (run with --replace to swap it in over the original)')
            made = []          # don't clean up the file we just delivered
            return True

        # Replace: keep the original until the new file is safely in place.
        backup = path + '.orig'
        os.replace(path, backup)
        try:
            os.replace(out, path)
        except Exception:
            os.replace(backup, path)          # put it back, lose nothing
            raise
        os.remove(backup)
        say('  ✓ replaced %s' % path)
        return True
    finally:
        for f in made:
            try:
                os.remove(f)
            except OSError:
                pass
        try:
            for f in os.listdir(workdir):
                os.remove(os.path.join(workdir, f))
            os.rmdir(workdir)
        except OSError:
            pass


def main():
    ap = argparse.ArgumentParser(
        description='Convert ffmpeg-unreadable text subtitles (WebVTT) in an '
                    'MKV to SRT. Video and audio are stream-copied.')
    ap.add_argument('files', nargs='+', help='.mkv file(s), or directories to scan')
    ap.add_argument('--replace', action='store_true',
                    help='swap the fixed file over the original (only after '
                         'every verification passes)')
    ap.add_argument('--dry-run', action='store_true',
                    help='report what would be done and stop')
    args = ap.parse_args()

    targets = []
    for p in args.files:
        if os.path.isdir(p):
            for root, _d, files in os.walk(p):
                targets += [os.path.join(root, f) for f in files
                            if f.lower().endswith('.mkv')]
        else:
            targets.append(p)

    # ⚠️ In batch, stay quiet about clean files. The first run over the Process
    # folder printed two lines for every one of ~200 untouched files, which
    # buries the one that matters. Report what needs attention, then a summary.
    batch = len(targets) > 1
    bad = clean = fixed = 0
    for t in sorted(targets):
        if not might_have_unreadable_subs(t):
            clean += 1
            if not batch:
                print('%s\n  no unreadable subtitle tracks — nothing to do'
                      % os.path.basename(t))
            continue
        print('%s' % os.path.basename(t))
        try:
            if fix(t, replace=args.replace, dry_run=args.dry_run):
                fixed += 1
            else:
                bad += 1
        except Exception as exc:
            print('  ✗ %s' % exc)
            bad += 1
    if batch:
        print('\n%d file(s) scanned — %d clean, %d needed work, %d failed'
              % (len(targets), clean, fixed, bad))
    sys.exit(1 if bad else 0)


if __name__ == '__main__':
    main()
