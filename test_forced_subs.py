#!/usr/bin/env python3
"""Bench the forced-subtitle detector against real files, before any GUI wiring.

Deliberately standalone: the algorithm needs tuning against real mixed-language
media (documentaries with foreign interview segments), and tuning it through a
GUI is slow and hides the numbers. This prints everything the thresholds depend
on so they can be set from evidence instead of guesswork.

Usage:
    python3 test_forced_subs.py /path/to/video.mkv [more.mkv ...]
    python3 test_forced_subs.py --model large-v3 --native en video.mkv

Writes <name>.forced.srt and <name>.full.srt next to each input unless --dry.
    -- Arthur & Tony, 2026-08-03
"""
import argparse
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "modules"))
import whisper_subtitles as ws  # noqa: E402


def extract_audio(path, tmpdir):
    out = Path(tmpdir) / "audio.wav"
    cmd = ["ffmpeg", "-y", "-i", str(path), "-vn", "-acodec", "pcm_s16le",
           "-ar", "16000", "-ac", "1", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg failed:\n" + r.stderr[-800:])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--model", default="large-v3")
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--native", default="en")
    ap.add_argument("--beam", type=int, default=5)
    ap.add_argument("--dry", action="store_true", help="don't write .srt files")
    a = ap.parse_args()

    print("thresholds: suspect_logprob=%s min_span=%ss confidence=%s merge_gap=%ss"
          % (ws.FORCED_SUSPECT_LOGPROB, ws.FORCED_MIN_SPAN,
             ws.FORCED_LANG_CONFIDENCE, ws.FORCED_MERGE_GAP))

    for f in a.files:
        path = Path(f).expanduser()
        print("\n" + "=" * 72)
        print(path.name)
        print("=" * 72)
        if not path.exists():
            print("  !! not found")
            continue

        t0 = time.time()
        with tempfile.TemporaryDirectory() as tmp:
            audio = extract_audio(path, tmp) if path.suffix.lower() != ".wav" else path
            main_segs, forced, report = ws.transcribe_with_forced(
                audio, a.model, None, a.device, a.beam, vad=True,
                native_lang=a.native, progress=lambda m: print("   " + m),
            )

        elapsed = time.time() - t0
        dur = report["duration"] or 1
        print("\n  -- report --")
        print("  duration        : %.0fs   (%.1fx realtime)" % (dur, dur / elapsed))
        print("  segments        : %d" % report["segments"])
        print("  windows checked : %d   %s" % (report["windows_checked"], report.get("by_source", {})))
        print("  foreign spans   : %d  %s" % (len(report["spans"]), report["languages"]))
        print("  coverage        : %.1f%%" % (report["coverage"] * 100))
        if report["skipped_reason"]:
            print("  SKIPPED         : " + report["skipped_reason"])
        for s in report["spans"]:
            print("     %7.1fs - %7.1fs  %-4s  %.0f%%"
                  % (s["start"], s["end"], s["lang"], s["prob"] * 100))
        print("  forced cues     : %d" % len(forced))
        for c in forced[:8]:
            print("     [%7.1f] %s" % (c.start, c.text[:70]))

        if forced and not a.dry:
            fp = path.with_suffix(".forced.srt")
            fp.write_text(ws.segments_to_srt(forced), encoding="utf-8")
            print("  wrote %s" % fp.name)
        if main_segs and not a.dry:
            mp = path.with_suffix(".full.srt")
            mp.write_text(ws.segments_to_srt(main_segs), encoding="utf-8")
            print("  wrote %s" % mp.name)


if __name__ == "__main__":
    main()
