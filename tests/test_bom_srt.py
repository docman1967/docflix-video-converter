#!/usr/bin/env python3
"""A UTF-8 BOM must not eat the first subtitle.

Tony, 2026-09-28: *"When I load it into VLC and Gnome Subtitles, it starts with
Previously on Stargate SG-1 but when I load it into Docflix Subtitle Editor, that
first line is missing completely."*

⭐ THE BOM GLUES ITSELF TO THE FIRST CUE'S INDEX. The file's opening line is not
`1`, it is `﻿1` — so block 1 fails to parse and the editor silently opens at
cue 2. Everything after it is fine, because a BOM appears exactly once.

Measured on his Stargate SG-1 S02E01:
    encoding='utf-8'      664 cues, starts "It's over, Jack."
    encoding='utf-8-sig'  665 cues, starts "Previously / on stargate SG-1..."

⚠️ VLC and Gnome Subtitles both strip it, which is why the file looks perfect
everywhere else and only OUR editor looked broken. ⭐ When one tool disagrees
with two others about the same file, suspect the tool — and look at the bytes,
not the text. `file(1)` said "UTF-8 (with BOM)" in the first second.

⛔⛔ AND THE LESSON WAS ALREADY IN THE FILE. Nine lines below the broken `.srt`
branch, the `.vtt` branch read with utf-8-sig and carried the comment "utf-8-sig
drops the BOM". Someone hit this once, fixed the path in front of them, and the
sibling path six lines away never got it. Two readers, one fixed.

Run: python3 tests/test_bom_srt.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.subtitle_filters import parse_srt                       # noqa: E402

fails = []


def check(label, got, want):
    if got == want:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}\n         got:  {got!r}\n         want: {want!r}")
        fails.append(label)


# A real SRT body, CRLF like the file Tony hit — BOM added separately below.
BODY = (
    "1\n00:00:06,006 --> 00:00:08,091\nPreviously\non stargate SG-1...\n\n"
    "2\n00:00:08,383 --> 00:00:11,762\nIt's over, Jack.\n\n"
    "3\n00:00:12,054 --> 00:00:13,305\nNo extensions.\n\n"
)
BOM = "﻿"

print("\n1. the parser itself is blinded by a leading BOM")
check("clean text -> all three cues", len(parse_srt(BODY)), 3)
check("⭐ BOM-prefixed text LOSES the first cue", len(parse_srt(BOM + BODY)), 2)
check("   ...and the survivor is cue 2", parse_srt(BOM + BODY)[0]["text"].split("\n")[0],
      "It's over, Jack.")

print("\n2. ⭐ which is why the READ has to strip it, not the parser")
# ⚠️ Fixing this in parse_srt would be the wrong place: the BOM is an encoding
# artefact, and Python already removes it for free with the right codec. Every
# caller that reads a file the USER supplied needs utf-8-sig.
d = tempfile.mkdtemp()
p = os.path.join(d, "bom.srt")
with open(p, "wb") as fh:
    fh.write(BOM.encode("utf-8") + BODY.replace("\n", "\r\n").encode("utf-8"))

with open(p, "r", encoding="utf-8", errors="replace") as fh:
    plain = parse_srt(fh.read())
with open(p, "r", encoding="utf-8-sig", errors="replace") as fh:
    sig = parse_srt(fh.read())
check("utf-8 read -> first cue missing", len(plain), 2)
check("⭐ utf-8-sig read -> first cue present", len(sig), 3)
check("   ...and it is the right one", sig[0]["text"].split("\n")[0], "Previously")

# ⛔ utf-8-sig must be harmless on a file with NO BOM, or the fix trades one bug
# for another on every ordinary subtitle in the library.
p2 = os.path.join(d, "clean.srt")
with open(p2, "w", encoding="utf-8") as fh:
    fh.write(BODY)
with open(p2, "r", encoding="utf-8-sig", errors="replace") as fh:
    check("⛔ utf-8-sig on a BOM-LESS file is identical", len(parse_srt(fh.read())), 3)

print("\n3. the editor's .srt reader uses utf-8-sig")
SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "..", "modules", "subtitle_editor.py")
src = open(SRC).read()
i = src.index("if ext in ('.srt',):")
blk = src[i:i + 1400]
check("the .srt branch reads utf-8-sig", "encoding='utf-8-sig'" in blk, True)
check("⛔ ...and no longer plain utf-8", "encoding='utf-8'," in blk, False)
# ⚠️ The .vtt branch had it right all along; keep them in step.
j = src.index("elif ext == '.vtt':")
check("the .vtt branch still does too",
      "encoding='utf-8-sig'" in src[j:j + 600], True)

print()
if fails:
    print(f"FAILED: {len(fails)} check(s)")
    sys.exit(1)
print("all BOM checks passed")
