#!/usr/bin/env python3
"""Dropping a file into the Subtitle Editor must not wait on CC detection.

⚠️⚠️ WHY THIS FILE EXISTS. Tony, 2026-09-18:

    "When I drop a file into the Subtitle Editor and it's running its scan, it
     takes a real long time to present the list of subtitles that can be
     extracted. We had this similar issue with the subtitle extractor and it
     turned out to be the cc detector."

Right again. Measured on a 9.1 GB Lucifer remux:

    get_subtitle_streams            0.03 s   <- the list he is waiting for
    detect_cc_types                16.34 s
      of which ccextractor direct  10.48 s — and it FAILS (rc=10) before
      falling through to the TS-pipe tier anyway

⭐ The list is ready in 30 milliseconds and then sits behind a probe whose
answer is not needed to draw it.

⚠️⚠️ THE DANGEROUS PART, AND WHY THIS TEST EXISTS RATHER THAN JUST THE FIX.
Downstream, `all_options = text_streams + bitmap_streams + cc_entries`, and
`len(all_options) == 1` opens that option WITHOUT showing a picker. So simply
publishing the list before CC is known would change behaviour: a file with one
text track plus one CC would silently auto-open the text track instead of
asking which one.

⭐ The rule is therefore: defer ONLY when >= 2 real streams already exist. A
picker is then guaranteed and nothing downstream can turn on the CC answer.

⛔ Do NOT "optimise" this by skipping CC detection when text subtitles exist.
That is the detector/doer divergence trap — the check would stop knowing what
the extractor knows, and a file with both real subs and CCs would quietly lose
the CC option.

Structural, because the scan lives several closures deep inside a Tk builder
and cannot be imported. Same approach as test_log_isolation.py.

Run:  python3 tests/test_editor_scan_defers_cc.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, '..', 'modules', 'subtitle_editor.py')


def _src():
    with open(SRC, encoding='utf-8') as fh:
        return fh.read()


def _scan_block():
    s = _src()
    i = s.index('def _do_scan():')
    j = s.index('def _convert_unreadable_subs', i)
    return s[i:j]


# ── The deferral, and its one safety condition ──────────────────────────────

def test_the_stream_list_is_published_before_cc_detection():
    """`get_subtitle_streams` result must be readable by the UI thread before
    `detect_cc_types` has returned."""
    b = _scan_block()
    assert 'streams_holder[0] = get_subtitle_streams' in b, (
        "the stream list is no longer published separately — the drop will "
        "block on CC detection again")
    i_streams = b.index('streams_holder[0] = get_subtitle_streams')
    i_cc = b.index('detect_cc_types(video_path)')
    assert i_streams < i_cc, "streams must be published BEFORE CC detection runs"


def test_the_fast_path_requires_two_or_more_streams():
    """⚠️⚠️ THE SAFETY CONDITION. With fewer than two streams the CC answer can
    change whether a picker appears at all, so the wait is still correct there.
    If this guard is ever loosened, a one-text-track file with closed captions
    starts auto-opening the wrong thing silently."""
    b = _scan_block()
    assert 'len(streams_holder[0]) >= 2' in b, (
        "the >=2 guard is gone — deferring CC with 0 or 1 streams changes "
        "whether the picker appears, and the single-option path auto-opens "
        "without asking")


def test_the_slow_path_still_exists():
    """The original behaviour must remain for the <2 case — not be deleted."""
    b = _scan_block()
    assert 'scan_thread.is_alive()' in b and '*scan_result[0]' in b, (
        "the blocking path was removed; files with 0 or 1 streams need it")


def test_the_dialog_is_only_dismissed_once():
    """⚠️ Both paths can reach dismissal. Destroying an already-destroyed
    Toplevel raises TclError on a Tk callback, which reads as a frozen app."""
    b = _scan_block()
    assert 'dismissed[0]' in b, "no re-entry guard on the scan dialog teardown"
    assert 'except tk.TclError' in b, (
        "dialog teardown is not guarded against an already-destroyed window")


# ── The placeholder row ─────────────────────────────────────────────────────

def test_a_placeholder_tells_the_user_cc_is_still_coming():
    """⭐ Silence would read as "this file has no closed captions". Say so."""
    s = _src()
    assert 'Checking for ' in s and 'closed captions' in s, (
        "the picker no longer shows that CC detection is still running — a "
        "user wanting CCs would conclude the file has none")


def test_the_placeholder_cannot_be_chosen_as_a_stream():
    """⚠️ Its iid is non-numeric and it has no entry in picker_streams;
    `int(sel[0])` on it raises ValueError inside a Tk callback, which surfaces
    as a dead OK button rather than an error anyone sees."""
    s = _src()
    assert "sel[0].isdigit()" in s, (
        "on_select no longer guards against the non-numeric placeholder iid")


def test_cc_rows_are_appended_with_matching_indices():
    """⚠️ Selection maps tree iid -> picker_streams index. A row appended to the
    tree must have its entry appended to the list in the same step, or the user
    picks one stream and gets another."""
    s = _src()
    i_append = s.index('picker_streams.append(entry)')
    i_insert = s.index('str(len(picker_streams) - 1)', i_append)
    assert i_append < i_insert, (
        "the tree row is inserted before its picker_streams entry exists, or "
        "the iid no longer matches the list index")


# ── The thing that must not be 'optimised' ──────────────────────────────────

def test_cc_detection_still_always_runs():
    """⛔ The tempting shortcut is skipping CC detection when text subtitles
    exist. That is the detector/doer divergence: the check stops knowing what
    the extractor knows. Detection must run unconditionally; only the WAITING
    was removed."""
    b = _scan_block()
    i_cc = b.index('detect_cc_types(video_path)')
    before = b[:i_cc]
    assert 'if ' not in before.split('def _do_scan():')[1], (
        "detect_cc_types is now conditional inside _do_scan — it must always "
        "run; only the blocking wait was supposed to go")


if __name__ == '__main__':
    fails = []
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except AssertionError as e:
                fails.append(name)
                print(f"  FAIL  {name}: {e}")
            except Exception as e:                       # noqa: BLE001
                fails.append(name)
                print(f"  ERROR {name}: {e}")
    print(f"\n{'ALL PASS' if not fails else str(len(fails)) + ' FAILED'}")
    sys.exit(1 if fails else 0)
