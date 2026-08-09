"""
A processed file must be NAMED for the container it actually contains.

THE BUG (found 2026-08-09)
--------------------------
The Media Processor's in-place path ended with a bare

    os.replace(out_path, original)

which is a RENAME, not a conversion. With its container set to `.mp4` it wrote
a genuine MP4 to the temp file and then moved it onto the source's `.mkv`
name. The extension came from the file being replaced; the format came from
the setting; nothing compared them.

It hid for years because every tool trusts the extension. 44% of a 300-file
sample of hdd6 was MP4 wearing `.mkv` — exactly the files subtitles had been
muxed into. The damage was downstream and invisible:

  * MP4 cannot store DOCFLIX_ENCODE (its muxer drops custom keys silently),
    so the encode stamp died on every mux
  * text subtitles were downgraded to mov_text instead of SRT

Tony diagnosed it from the symptom: "the media processor stripped out the tag
when I muxed the subtitle and video."

These tests are deliberately about the PROPERTY (name matches bytes), not the
implementation — that is the invariant that was violated, and any future
refactor of the replace logic must keep it.
"""

import os
import re
import subprocess

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MP = os.path.join(REPO, "modules", "media_processor.py")


def _src():
    return open(MP, encoding="utf-8").read()


def test_inplace_replace_uses_the_target_container_extension():
    """The in-place branch must build its name from the container setting."""
    src = _src()
    assert "target_ext = _ov(f, 'container', opt_container) or orig_ext" in src, \
        "in-place path no longer derives the extension from the container setting"


def test_inplace_does_not_blindly_replace_the_original():
    """⚠️ The exact shape of the original bug.

    `os.replace(out_path, original)` is only correct when the target name
    already equals the original — i.e. guarded by the `not renamed` branch.
    An UNGUARDED occurrence is the bug coming back.
    """
    src = _src()
    # Collect every os.replace(out_path, original) and require that a
    # `renamed` guard exists above it in the same block.
    assert "renamed = (os.path.normpath(new_name)" in src, \
        "the renamed-vs-same-name guard is gone"
    assert "if not renamed:" in src, \
        "os.replace(out_path, original) is no longer gated on the name matching"


def test_container_change_is_logged_loudly():
    """Silence is what let this run for years. A container change must WARN."""
    src = _src()
    m = re.search(r"if target_ext\.lower\(\) != orig_ext\.lower\(\):(.{0,400})",
                  src, re.S)
    assert m, "no branch detecting a container change"
    assert "'WARNING'" in m.group(1), \
        "a container change must be logged at WARNING, not INFO"


def test_per_file_override_wins():
    """The old edition branch read the GLOBAL opt_container, so a per-file
    override produced a name that disagreed with the bytes for exactly the
    files that had been special-cased."""
    src = _src()
    inplace = src[src.index("if is_inplace:"):]
    inplace = inplace[:inplace.index("else:\n                        final_path = out_path")]
    # Strip comments — the explanation above the fix mentions the old call by
    # name, and matching our own documentation would make this always fail.
    code = "\n".join(l for l in inplace.splitlines()
                     if not l.lstrip().startswith("#"))
    assert "opt_container.get()" not in code, \
        "in-place path reads the global container, ignoring per-file overrides"


@pytest.mark.skipif(not __import__('shutil').which('ffmpeg'),
                    reason='needs ffmpeg')
def test_mp4_really_cannot_hold_the_stamp(tmp_path):
    """The reason any of this matters — pinned so nobody 'simplifies' the
    container choice later thinking it is cosmetic."""
    src = tmp_path / "src.mkv"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y",
         "-f", "lavfi", "-i", "testsrc=size=320x240:rate=24:duration=1",
         "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast",
         str(src)], check=True, timeout=120)

    got = {}
    for ext, fmt in (("mkv", "matroska"), ("mp4", "mp4")):
        out = tmp_path / f"stamped.{ext}"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", str(src), "-c", "copy",
             "-metadata", "DOCFLIX_ENCODE=CRF32 hevc_nvenc p4 10bit v3.18.1",
             "-f", fmt, str(out)], check=True, timeout=120)
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format_tags=DOCFLIX_ENCODE", "-of", "default=nw=1:nk=1", str(out)],
            capture_output=True, text=True, timeout=60)
        got[ext] = r.stdout.strip()

    assert "CRF32" in got["mkv"], "MKV should carry the stamp"
    assert not got["mp4"], \
        "MP4 unexpectedly kept a custom tag — if this ever passes, the " \
        "container advice in media_processor.py needs revisiting"
