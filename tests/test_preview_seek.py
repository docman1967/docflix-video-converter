#!/usr/bin/env python3
"""Selecting a cue must show THAT cue's text in the video preview.

Tony, 2026-09-26: *"load a subtitle, click view, waveform, delete a cue. Text it
shows on the screen doesn't match the selected cue like it should."* His
screenshot: selected row 00:26:43,394 → 00:26:49,442 "Maybe I'll pick up / one
for you too.", picture reading "So get one." — the cue ENDING at 00:26:43,394.

⭐⭐ NOT an off-by-one in our indices, which is where this looked like it was
going. Subtitle files are usually GAPLESS: on the episode he was editing, 493 of
550 cues (90%) start on the exact millisecond the previous one ends. Seeking to
that millisecond lands on the nearest decodable frame at or before it, and at
23.976 fps frames are 41.7 ms apart — so about a third of the time that frame is
still inside the outgoing cue and mpv renders its text. Intermittent, and
entirely dependent on where the cue boundary falls between two frames.

⭐ MEASURED against the real episode over mpv's IPC socket, 40 cues:
      seek to cue start           12/40 showed the previous cue
      seek via preview_seek_ms()   0/40
      control, seek mid-cue        0/40

⚠️⚠️ THE MEASUREMENT TOOK FOUR ATTEMPTS AND THE FIRST THREE WERE THE INSTRUMENT,
not the bug — worth recording because every one looked like a result:
  1. passed --sub-file, but the MKV carries 8 embedded subtitle tracks and mpv
     had selected --sid=1. The probe was reading a different subtitle entirely.
  2. read sub-text after a fixed sleep, so it sometimes read before the seek
     landed.
  3. with --vo=null and --pause mpv decodes NO frame after a seek, so sub-text
     stayed empty and every cue read as "wrong". Fixed with frame-step.
⭐ What caught all three was the same thing: a CONTROL that seeks to the MIDDLE
of a cue, where the answer cannot be anything but right. Until that read 0/N,
every number the probe produced was noise — and two of them were plausible
enough to have shipped a wrong fix. Never measure a boundary without it.

Run: python3 tests/test_preview_seek.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.waveform_timeline import (PREVIEW_NUDGE_MS,              # noqa: E402
                                       preview_seek_ms)

fails = []


def check(label, got, want):
    if got == want:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}\n         got:  {got!r}\n         want: {want!r}")
        fails.append(label)


DUR = 2_700_000          # a ~45 minute episode

print("\n1. the nudge lands inside the cue, past one frame")
# ⚠️ 41.7 ms is one frame at 23.976 fps. A nudge smaller than that buys nothing:
# the seek can still resolve to the frame before the cue starts.
check("one frame at 23.976 fps is 41.7 ms — the nudge must exceed it",
      PREVIEW_NUDGE_MS > 1000 / 23.976, True)
check("ordinary cue gets the full nudge",
      preview_seek_ms(1_603_394, 1_609_442, DUR), 1_603_394 + PREVIEW_NUDGE_MS)
check("   ...which is Tony's cue, moved off the boundary",
      preview_seek_ms(1_603_394, 1_609_442, DUR) > 1_603_394, True)
check("   ...and still well inside it",
      preview_seek_ms(1_603_394, 1_609_442, DUR) < 1_609_442, True)

print("\n2. ⛔ a short cue is never overshot")
# ⚠️ THE LOAD-BEARING CLAMP. A blunt +50 ms would sail past a 60 ms cue
# entirely and show the NEXT one — trading Tony's bug for its mirror image.
for dur in (30, 60, 90, 120, 150, 300, 1376):
    s, e = 10_000, 10_000 + dur
    got = preview_seek_ms(s, e, DUR)
    check(f"{dur:5d} ms cue -> seeks {got - s:3d} ms in, still inside",
          s <= got < e, True)
check("a 60 ms cue nudges by a third, not by 50", preview_seek_ms(0, 60, DUR), 20)
check("a 1376 ms cue (the shortest in his file) gets the full nudge",
      preview_seek_ms(0, 1376, DUR), PREVIEW_NUDGE_MS)

print("\n3. ⛔ degenerate input must not invent a position")
check("zero-length cue -> the start itself", preview_seek_ms(500, 500, DUR), 500)
check("reversed cue -> the start itself", preview_seek_ms(900, 100, DUR), 900)
check("cue at time zero", preview_seek_ms(0, 5000, DUR), PREVIEW_NUDGE_MS)

print("\n4. ⛔ never seeks past the end of the media")
# A final cue that runs to the last frame must not be nudged off the end, or
# mpv seeks past EOF and the preview goes black.
# ⚠️ The first version of this check expected DUR exactly, which was the TEST
# being wrong, not the code: a 20 ms final cue nudges by 6 ms and lands at
# DUR-14, correctly INSIDE the cue. Clamping to DUR would be the worse answer —
# that is a seek to EOF, where there is no frame and no subtitle.
check("a short last cue lands inside itself, not at EOF",
      DUR - 20 <= preview_seek_ms(DUR - 20, DUR, DUR) < DUR, True)
check("a normal last cue still lands inside the media",
      preview_seek_ms(DUR - 3000, DUR, DUR) < DUR, True)
# ⛔ The clamp still has to exist for a cue whose END is past the media length
# (a subtitle running long, which does happen on re-encodes).
check("⛔ a cue running past the end is clamped to the duration",
      preview_seek_ms(DUR - 10, DUR + 5000, DUR), DUR)

print("\n5. the playback position is NOT the preview position")
# ⭐ seek_to_cue stores the TRUE start in _playback_pos_ms and only sends the
# nudged value to mpv. So the waveform cursor sits on the real boundary and
# Play starts at the first word — the preview alone moves forward.
import ast                                                            # noqa: E402
SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "..", "modules", "waveform_timeline.py")
src = open(SRC).read()
fn = next(n for n in ast.walk(ast.parse(src))
          if isinstance(n, ast.FunctionDef) and n.name == "seek_to_cue")
body = ast.get_source_segment(src, fn)
check("_playback_pos_ms is assigned the raw start",
      "self._playback_pos_ms = start_ms" in body, True)
check("the mpv seek uses the nudged value", "preview_seek_ms(" in body, True)
check("⛔ and NOT the raw start",
      'self._mpv_cmd(["seek", str(start_ms / 1000)' in body, False)

print()
if fails:
    print(f"FAILED: {len(fails)} check(s)")
    sys.exit(1)
print("all preview-seek checks passed")
