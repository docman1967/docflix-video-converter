#!/usr/bin/env python3
"""Regression test: bit-depth and adaptive-quantisation flags.

WHY THIS FILE EXISTS
────────────────────
Tony, 2026-08-08: *"We'll need to add a way to include 10 bit into the encoding
on the Docflix Media Suite. I'm not sure how it determines whether it should be
8/10 bit now."*

It didn't. The main encode path never passed `-pix_fmt` at all, so ffmpeg
inherited whatever the SOURCE was. That is why ~43% of his library was already
10-bit without anyone choosing it — those releases happened to be 10-bit.

The same investigation found that NVENC's `-temporal-aq/-spatial-aq`, which
were unconditionally on, cost a great deal for nothing measurable. Measured at
identical CQ on two deliberately opposite sources:

    grainy 2009 BluRay   +48% bitrate    VMAF 92.15 vs 92.26
    modern WEB h264      +19% bitrate    VMAF 84.91 vs 85.03

So they became opt-in, defaulting OFF.

⚠️ THE DANGEROUS CASE IS FORCING 10-BIT ON A CODEC THAT CANNOT DO IT. An
unsupported `-pix_fmt` is a hard ffmpeg failure, not a graceful downgrade — the
job dies. ProRes carries its format per profile, MPEG-4 Part 2 is 8-bit only,
and VAAPI takes its format through the hwupload chain. Those must emit NO
-pix_fmt rather than a wrong one, which is what the None entries encode.

    python3 tests/test_bitdepth_aq.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def load_tables():
    """Pull the two config tables out of video_converter.py WITHOUT importing
    it — importing pulls in Tk and the whole GUI."""
    import ast
    src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), 'video_converter.py'), encoding='utf-8').read()
    tree = ast.parse(src)
    out = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id in ('GPU_BACKENDS',
                                                        'VIDEO_CODEC_MAP'):
                    out[t.id] = ast.literal_eval(node.value)
    return out.get('GPU_BACKENDS', {}), out.get('VIDEO_CODEC_MAP', {})


def main():
    backends, codecs = load_tables()
    fails = 0

    print("\n  every GPU backend declares aq_args and pix_fmt_10bit")
    for name, b in sorted(backends.items()):
        for key in ('aq_args', 'pix_fmt_10bit'):
            ok = key in b
            fails += not ok
            print(f"    {'ok  ' if ok else 'FAIL'} {name}.{key}"
                  f"{'' if ok else '  <- MISSING, .get() would silently skip it'}")

    print("\n  AQ flags are NOT in quality_args any more")
    for name, b in sorted(backends.items()):
        qa = ' '.join(b.get('quality_args', []))
        bad = 'temporal-aq' in qa or 'spatial-aq' in qa
        fails += bad
        print(f"    {'FAIL' if bad else 'ok  '} {name}: {qa or '(none)'}")

    print("\n  nvenc keeps lookahead (it measured +2% bitrate for +0.07 VMAF)")
    nv = ' '.join(backends.get('nvenc', {}).get('quality_args', []))
    ok = 'rc-lookahead' in nv
    fails += not ok
    print(f"    {'ok  ' if ok else 'FAIL'} {nv}")

    print("\n  nvenc AQ flags survive, just moved")
    aq = ' '.join(backends.get('nvenc', {}).get('aq_args', []))
    ok = 'temporal-aq' in aq and 'spatial-aq' in aq
    fails += not ok
    print(f"    {'ok  ' if ok else 'FAIL'} {aq}")

    # ⚠️ The important half. A codec that cannot do 10-bit must say None, not
    # be missing the key and not carry a plausible-looking format.
    print("\n  codecs that CANNOT take an explicit -pix_fmt must declare None")
    # ProRes carries its format per profile; MPEG-4 Part 2 is 8-bit only;
    # stream copy has no pixel format at all and -pix_fmt beside -c:v copy is a
    # hard ffmpeg error. All three must emit NOTHING, for 8-bit as well as 10 —
    # the first version of the guard hard-coded yuv420p for 8-bit and would
    # have killed every ProRes and every copy job.
    CANNOT = {'ProRes (QuickTime)', 'MPEG-4', 'Copy (no re-encode)'}
    for name, c in sorted(codecs.items()):
        if 'pix_fmt_10bit' not in c:
            print(f"    FAIL {name}: no pix_fmt_10bit key")
            fails += 1
            continue
        v = c['pix_fmt_10bit']
        want_none = name in CANNOT
        ok = (v is None) if want_none else (v is not None)
        fails += not ok
        print(f"    {'ok  ' if ok else 'FAIL'} {name:<22} {str(v):<14}"
              f"{'(8-bit only)' if want_none else ''}")

    print("\n  VAAPI cannot take -pix_fmt here (format goes via hwupload)")
    v = backends.get('vaapi', {}).get('pix_fmt_10bit', 'MISSING')
    ok = v is None
    fails += not ok
    print(f"    {'ok  ' if ok else 'FAIL'} vaapi.pix_fmt_10bit = {v}")

    # ⚠️ The guard as the encoder actually applies it, for EVERY combination.
    # The table above says what each entry declares; this says what ffmpeg
    # would receive — which is the thing that breaks a job.
    print("\n  what -pix_fmt the builder would emit (None = emits nothing)")

    def emit(codec, is_gpu, backend_name, depth):
        ci = codecs[codec]
        if depth not in ('8', '10'):
            return None
        pf10 = ci.get('pix_fmt_10bit')
        if pf10 and is_gpu and backend_name:
            pf10 = backends[backend_name].get('pix_fmt_10bit')
        return ('yuv420p' if depth == '8' else pf10) if pf10 else None

    for codec in sorted(codecs):
        cannot = codec in CANNOT
        for is_gpu, bname, depth in ((False, None, '8'), (False, None, '10'),
                                     (True, 'nvenc', '10'), (True, 'vaapi', '10')):
            got = emit(codec, is_gpu, bname, depth)
            # Anything in CANNOT, and anything on vaapi, must emit nothing.
            must_be_none = cannot or bname == 'vaapi'
            ok = (got is None) if must_be_none else (got is not None)
            if not ok:
                fails += 1
                print(f"    FAIL {codec} {bname or 'cpu'} {depth}-bit -> {got}")
    print("    ok   all codec x backend x depth combinations behave")

    print("\n  'auto' must emit nothing at all — the historical behaviour")
    auto_ok = all(emit(c, g, b, 'auto') is None
                  for c in codecs for g, b in ((False, None), (True, 'nvenc')))
    fails += not auto_ok
    print(f"    {'ok  ' if auto_ok else 'FAIL'} auto -> no -pix_fmt anywhere")

    total = (len(backends) * 2 + len(backends) + 2 + len(codecs) + 3)
    print(f"\n  {total - fails}/{total} checks pass\n")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
