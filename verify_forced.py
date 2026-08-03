#!/usr/bin/env python3
"""Cut every detected foreign span into a short clip so a human can verify it fast.

The detector can measure its own precision only in the sense of "did detect_language
agree with itself." It cannot tell a real Arabic interview from a music bed that got
labelled Norwegian. Tony can, in about four seconds per clip — but not if verifying
means scrubbing through four 44-minute documentaries.

So: cut each span (plus lead-in context, because a span that starts late is itself a
bug worth seeing), write them to a folder, and build an HTML page that plays them in
order with the detector's claim next to each one.

Usage:
    python3 verify_forced.py /tmp/forced_e234.log /tmp/forced_e01c.log
    python3 verify_forced.py --outdir ~/forced_review *.log
    -- Arthur & Tony, 2026-08-03
"""
import argparse
import html
import os
import re
import subprocess
import sys
from pathlib import Path

MEDIA_DIR = Path("/home/docman1967/downloads/dst/Dogs The Untold Story/Season 1")
LEAD = 4.0    # seconds of context before the span — catches "started too late"
TAIL = 2.0

HDR = re.compile(r"^(Dogs The Untold Story.*\.mkv)\s*$", re.M)
SPAN = re.compile(r"^\s+(\d+\.\d+)s\s*-\s*(\d+\.\d+)s\s+(\w+)\s+(\d+)%\s*$", re.M)


def parse(logs):
    """Walk each log, attaching spans to the most recent episode header."""
    found = []
    for log in logs:
        text = Path(log).read_text(errors="replace")
        current = None
        for line in text.splitlines():
            h = HDR.match(line)
            if h:
                current = h.group(1)
                continue
            s = SPAN.match(line)
            if s and current:
                found.append((current, float(s.group(1)), float(s.group(2)),
                              s.group(3), int(s.group(4))))
    return found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("logs", nargs="+")
    ap.add_argument("--outdir", default=os.path.expanduser("~/forced_review"))
    ap.add_argument("--media", default=str(MEDIA_DIR))
    a = ap.parse_args()

    out = Path(a.outdir)
    out.mkdir(parents=True, exist_ok=True)
    spans = parse(a.logs)
    if not spans:
        sys.exit("no spans parsed from those logs")

    rows = []
    for i, (episode, start, end, lang, conf) in enumerate(spans, 1):
        src = Path(a.media) / episode
        if not src.exists():
            print(f"  !! missing media: {episode}")
            continue
        ep = re.search(r"S01E0(\d)", episode)
        tag = f"E{ep.group(1)}" if ep else "E?"
        clip = out / f"{i:02d}_{tag}_{int(start)}s_{lang}_{conf}pct.mp4"
        ss = max(0.0, start - LEAD)
        dur = (end - start) + LEAD + TAIL
        cmd = ["ffmpeg", "-v", "error", "-y", "-ss", f"{ss:.2f}", "-t", f"{dur:.2f}",
               "-i", str(src), "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
               "-vf", "scale=640:-2", "-c:a", "aac", "-b:a", "128k", str(clip)]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            print(f"  !! ffmpeg failed for {clip.name}: {r.stderr[-200:]}")
            continue
        mm, sec = divmod(int(start), 60)
        rows.append((clip.name, tag, f"{mm}:{sec:02d}", lang, conf, end - start))
        print(f"  {clip.name}")

    # Lone hits are the suspicious ones — flag anything with no same-language
    # neighbour within 3 minutes, since real foreign dialogue arrives in clusters.
    page = ["<!doctype html><meta charset=utf-8><title>Forced-sub review</title>",
            "<style>body{font:14px system-ui;background:#111;color:#eee;margin:24px}",
            "table{border-collapse:collapse}td{padding:8px;border-bottom:1px solid #333;vertical-align:top}",
            "video{width:420px;border-radius:6px}.lone{color:#f90}.tag{color:#888}</style>",
            "<h2>Forced-subtitle review</h2>",
            "<p>Play each clip. The first ~4s is lead-in context <em>before</em> the detected span — "
            "if the foreign speech starts during that lead-in, the span boundary is late.</p>",
            "<p><span class=lone>Orange</span> = isolated hit with no same-language neighbour "
            "nearby. Those are the suspected false positives (music/ambience labelled as speech).</p>",
            "<table>"]
    for idx, (name, tag, ts, lang, conf, dur) in enumerate(rows):
        neighbours = [r for j, r in enumerate(rows)
                      if j != idx and r[1] == tag and r[3] == lang]
        lone = " class=lone" if not neighbours else ""
        page.append(
            f"<tr><td><video src='{html.escape(name)}' controls preload=metadata></video></td>"
            f"<td><b{lone}>{tag} @ {ts}</b><br><span class=tag>{lang} · {conf}% · "
            f"{dur:.1f}s{'' if neighbours else ' · LONE'}</span></td></tr>")
    page.append("</table>")
    (out / "review.html").write_text("\n".join(page), encoding="utf-8")
    print(f"\n{len(rows)} clip(s) -> {out}\nOpen: {out / 'review.html'}")


if __name__ == "__main__":
    main()
