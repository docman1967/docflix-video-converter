#!/usr/bin/env python3
"""The cheap A53 gate in front of the expensive CC probe.

⚠️⚠️ WHY THIS EXISTS. detect_cc_types() runs three tiers — ccextractor over
the whole file, ccextractor over a TS pipe, then an ffprobe side-data scan —
and on a 12 GB Blu-ray REMUX that is 21-28 seconds to reach the answer "this
file has no closed captions". Tony, 2026-09-20, on dropping one into the
Subtitle Editor: "takes quite a while to scan". Measured: 0 of 300 sampled
library files carry CC at all, so that cost is paid almost entirely to confirm
a negative.

any_a53_side_data() samples five points through the file (~1-3 s) and lets the
negative case stop early. A hit still runs every tier, because only they can
tell EIA-608 from CEA-708 and the editor's picker builds a separate row for
each.

    Luther S01E02, 12.5 GB    21.6 s -> 3.0 s
    files that DO have CC     ~5 s, unchanged answer (608)

⚠️⚠️ THE MOST IMPORTANT TEST IN THIS FILE IS test_gate_fails_OPEN.
A gate that fails CLOSED reports "no captions" on every file the moment
ffprobe breaks or goes missing — and that is far worse than being slow,
because it looks exactly like a real answer. There is no error, no log line
and no symptom until someone notices captions stopped being offered.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules import gpu                                        # noqa: E402


class _Result:
    def __init__(self, stdout='', returncode=0):
        self.stdout = stdout
        self.stderr = ''
        self.returncode = returncode


A53 = '{"frames":[{"side_data_list":[{"side_data_type":"ATSC A53 Part 4 Closed Captions"}]}]}'
EMPTY = '{"frames":[{"side_data_list":[]}]}'


# ───────────────────────────── the gate itself ─────────────────────────────

def test_finds_a53_when_present(monkeypatch):
    monkeypatch.setattr(gpu.subprocess, 'run', lambda *a, **k: _Result(A53))
    assert gpu.any_a53_side_data('x.mkv') is True


def test_reports_nothing_when_absent(monkeypatch):
    monkeypatch.setattr(gpu.subprocess, 'run', lambda *a, **k: _Result(EMPTY))
    assert gpu.any_a53_side_data('x.mkv') is False


def test_gate_fails_OPEN(monkeypatch):
    """⚠️⚠️ THE ONE THAT MATTERS. Any failure must mean "I don't know", which
    sends the caller to the full probe — never "no captions"."""
    def boom(*a, **k):
        raise OSError('ffprobe not found')
    monkeypatch.setattr(gpu.subprocess, 'run', boom)
    assert gpu.any_a53_side_data('x.mkv') is True, \
        'an exception must fail OPEN, or a broken ffprobe silently reports no CC'

    monkeypatch.setattr(gpu.subprocess, 'run',
                        lambda *a, **k: _Result('', returncode=1))
    assert gpu.any_a53_side_data('x.mkv') is True, \
        'a non-zero exit is not a "no"'


def test_samples_more_than_the_opening(monkeypatch):
    """⚠️ Five points, not one. A cold open, a network ident or a title
    sequence can all run before the first caption — 2026-08-23 has a real .mp4
    where a 30-frame look found nothing and an extraction found 827 cues."""
    seen = {}

    def capture(cmd, *a, **k):
        seen['iv'] = cmd[cmd.index('-read_intervals') + 1]
        return _Result(EMPTY)
    monkeypatch.setattr(gpu.subprocess, 'run', capture)
    gpu.any_a53_side_data('x.mkv')
    assert seen['iv'].count('#') >= 5, \
        f'expected several sample points, got {seen["iv"]!r}'
    assert seen['iv'] != '%+#30', 'that is the OLD single-point look'


# ─────────────────────────── the gate in context ───────────────────────────

def test_no_a53_anywhere_short_circuits_the_whole_probe(monkeypatch):
    """The 21-28 s path must not run at all when the gate finds nothing."""
    monkeypatch.setattr(gpu, 'any_a53_side_data', lambda *a, **k: False)

    def fail(*a, **k):
        raise AssertionError('the expensive probe ran despite a clean gate')
    monkeypatch.setattr(gpu.shutil, 'which', fail)

    assert gpu.detect_cc_types('x.mkv') == {'eia_608': False, 'eia_708': False}


def test_a_hit_still_runs_the_full_probe(monkeypatch):
    """⚠️ The gate is a GATE, not a detector. It cannot tell 608 from 708, and
    the editor builds a separate picker row for each — so a hit must escalate,
    never answer on its own."""
    ran = []
    monkeypatch.setattr(gpu, 'any_a53_side_data', lambda *a, **k: True)
    monkeypatch.setattr(gpu.shutil, 'which', lambda n: ran.append(n) or None)
    monkeypatch.setattr(gpu, 'detect_closed_captions', lambda p: False)
    gpu.detect_cc_types('x.mkv')
    assert ran, 'a gate hit must fall through to the real tiers'


def test_a_broken_ffprobe_does_not_silence_detection(monkeypatch):
    """End to end on the failure that matters: ffprobe missing entirely must
    still reach the full probe rather than returning a confident no."""
    def boom(*a, **k):
        raise FileNotFoundError('ffprobe')
    monkeypatch.setattr(gpu.subprocess, 'run', boom)
    reached = []
    monkeypatch.setattr(gpu.shutil, 'which',
                        lambda n: reached.append(n) or None)
    monkeypatch.setattr(gpu, 'detect_closed_captions', lambda p: False)
    gpu.detect_cc_types('x.mkv')
    assert reached, 'a dead ffprobe must not short-circuit to "no captions"'


# ──────────────────────── the deliberate divergence ────────────────────────

def test_the_bulk_add_path_keeps_its_cheaper_look(monkeypatch):
    """⛔ detect_closed_captions() stays a 30-frame look ON PURPOSE. The main
    window probes files in bulk as they are added; the 5-point sample would
    cost ~75 s on a 50-file drop against ~7 s. The editor opens one file at a
    time and can afford the better look. Two costs, two tools, on purpose."""
    seen = {}

    def capture(cmd, *a, **k):
        seen['iv'] = cmd[cmd.index('-read_intervals') + 1]
        return _Result(EMPTY)
    monkeypatch.setattr(gpu.subprocess, 'run', capture)
    gpu.detect_closed_captions('x.mkv')
    assert seen['iv'] == '%+#30', \
        'the bulk-add probe was widened — price the 50-file drop first'
