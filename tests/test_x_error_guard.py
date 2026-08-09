"""
The X11 error guard — the fix for the "window just VANISHED" bug.

Background (2026-08-09): the Whisper Transcriber died mid drag-and-drop leaving
only five lines in the log:

    X Error of failed request:  BadWindow (invalid Window parameter)
      Major opcode of failed request:  20 (X_GetProperty)
      Resource id in failed request:  0x412128

Drag-and-drop reads properties off the DRAG SOURCE window. If that window id has
gone stale by the time the read lands, Xlib's DEFAULT error handler prints that
and calls exit() — below Python, so: no traceback, no faulthandler, no core
dump, and the window appears to simply vanish. Same root cause as the Subtitle
Editor vanishing on 2026-08-07.

These tests do the thing that matters: they REPRODUCE the crash unguarded, then
prove the guard survives it. A test that only checked "guard installs OK" would
have passed against the first, broken version of this code.

Requires xvfb-run and libX11. Skipped cleanly if either is missing.
"""

import ctypes.util
import os
import shutil
import subprocess
import sys
import textwrap

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

pytestmark = pytest.mark.skipif(
    shutil.which('xvfb-run') is None or not ctypes.util.find_library('X11'),
    reason='needs xvfb-run and libX11',
)

# The exact resource id from Tony's real crash log, so the reproduction is the
# actual failure and not a lookalike.
BOGUS_WINDOW = 0x412128

_PROG = textwrap.dedent('''
    import ctypes, ctypes.util, os, sys
    sys.path.insert(0, {repo!r})
    if os.environ.get("GUARD") == "1":
        from modules.utils import install_x_error_guard
        assert install_x_error_guard() is True, "guard failed to install"

    x = ctypes.CDLL(ctypes.util.find_library("X11"))
    x.XOpenDisplay.restype = ctypes.c_void_p
    d = x.XOpenDisplay(None)
    assert d, "no display"

    # Exactly what XDND does: read a property off a window that is already gone.
    at, af = ctypes.c_ulong(), ctypes.c_int()
    n, rem, prop = ctypes.c_ulong(), ctypes.c_ulong(), ctypes.c_void_p()
    x.XGetWindowProperty(
        ctypes.c_void_p(d), ctypes.c_ulong({win}), ctypes.c_ulong(1),
        ctypes.c_long(0), ctypes.c_long(1024), 0, ctypes.c_ulong(0),
        ctypes.byref(at), ctypes.byref(af), ctypes.byref(n),
        ctypes.byref(rem), ctypes.byref(prop))
    x.XSync(ctypes.c_void_p(d), 0)   # force the error to be delivered
    print("SURVIVED", flush=True)
''')


def _run(guard):
    env = dict(os.environ, GUARD='1' if guard else '0')
    return subprocess.run(
        ['xvfb-run', '-a', sys.executable, '-c',
         _PROG.format(repo=REPO, win=BOGUS_WINDOW)],
        capture_output=True, text=True, timeout=120, env=env,
    )


def test_bug_reproduces_without_the_guard():
    """Without the guard the process DIES. If this ever fails, the guard is
    being installed by something else and the other test proves nothing."""
    r = _run(guard=False)
    assert r.returncode != 0, f"expected the X error to kill it, got:\n{r.stdout}"
    assert 'SURVIVED' not in r.stdout
    # Xlib's default handler prints to STDOUT, not stderr — which is precisely
    # why the crash landed in the app's log file with no traceback beside it.
    assert 'BadWindow' in (r.stdout + r.stderr)


def test_guard_survives_the_same_error():
    """With the guard the process lives through the identical error."""
    r = _run(guard=True)
    assert r.returncode == 0, f"guard did not hold:\n{r.stdout}\n{r.stderr}"
    assert 'SURVIVED' in r.stdout


def test_guard_logs_correctly_decoded_fields():
    """⚠️ The regression that nearly shipped.

    The XErrorEvent field order was wrong (serial and resourceid swapped), so
    every field decoded as garbage — err=7, req=0, rid=0x7ffee4001403 — while
    the guard still 'worked'. Silent wrong-decoding means it would swallow the
    wrong error classes. Pin the real values from the real crash.
    """
    r = _run(guard=True)
    out = r.stdout + r.stderr
    assert 'BadWindow(3)' in out, f"error code decoded wrong:\n{out}"
    assert 'request 20' in out, f"request code decoded wrong:\n{out}"
    assert f'0x{BOGUS_WINDOW:x}' in out, f"resource id decoded wrong:\n{out}"


def test_struct_layout_matches_the_real_header():
    """Offsets verified against Xlib.h with offsetof() on 2026-08-09.
    Hard-coded because the header may not be installed everywhere."""
    from modules import utils
    assert utils.install_x_error_guard() is True
    # Rebuild the struct the same way the guard does and check the offsets.
    import ctypes

    class XErrorEvent(ctypes.Structure):
        _fields_ = [
            ('type', ctypes.c_int),
            ('display', ctypes.c_void_p),
            ('resourceid', ctypes.c_ulong),
            ('serial', ctypes.c_ulong),
            ('error_code', ctypes.c_ubyte),
            ('request_code', ctypes.c_ubyte),
            ('minor_code', ctypes.c_ubyte),
        ]

    assert ctypes.sizeof(XErrorEvent) == 40
    for field, off in (('type', 0), ('display', 8), ('resourceid', 16),
                       ('serial', 24), ('error_code', 32),
                       ('request_code', 33), ('minor_code', 34)):
        assert getattr(XErrorEvent, field).offset == off, \
            f'{field} moved — re-verify against Xlib.h'


def test_error_codes_are_the_real_ones():
    """BadValue is 2 and BadName is 15. An earlier draft had `15: BadValue`,
    which would have swallowed BadName and let BadValue through."""
    from modules.utils import _X_SURVIVABLE
    assert _X_SURVIVABLE == {3: 'BadWindow', 4: 'BadPixmap', 5: 'BadAtom',
                             8: 'BadMatch', 9: 'BadDrawable'}
    # Codes that indicate a real programming error must NOT be swallowed.
    for code in (2, 11, 16, 17):        # BadValue, BadAlloc, BadLength, BadImpl
        assert code not in _X_SURVIVABLE


def test_a_survived_error_is_recorded_durably(tmp_path, monkeypatch):
    """⚠️ This is what makes the fix TESTABLE IN THE WILD.

    The bug is intermittent, so "I dragged a file and it was fine" proves
    nothing — it was fine most of the time before the fix too. The app's own
    logs rotate at 10 runs, so a 2am hit would be gone by morning.

    Each line in this file is one process death that did NOT happen. It also
    DISCRIMINATES: if a window ever vanishes again and there is no matching
    line here, it is a DIFFERENT bug and we stop re-litigating this one.
    """
    from modules import utils
    log = tmp_path / 'x_errors.log'
    monkeypatch.setattr(utils, 'X_ERROR_LOG', str(log))
    utils._record_survived_x_error('BadWindow', 3, 20, 0x412128)
    line = log.read_text(encoding='utf-8').strip()
    assert 'BadWindow(3)' in line
    assert 'request=20' in line
    assert 'resource=0x412128' in line
    assert f'pid={os.getpid()}' in line


def test_recorder_never_raises(monkeypatch):
    """It runs INSIDE the X error handler. A failure to log must not become a
    second failure — that would resurrect the very crash we just fixed."""
    from modules import utils
    monkeypatch.setattr(utils, 'X_ERROR_LOG', '/proc/nonexistent/nope/x.log')
    utils._record_survived_x_error('BadWindow', 3, 20, 1)   # must not raise


def test_every_tk_root_installs_it():
    """Every standalone tool is its OWN process, so the guard has to be in both
    entry points. Guarding only the main app is the kind of half-fix that reads
    as finished — the same trap the wheel guard had."""
    for rel in ('video_converter.py', 'modules/standalone.py'):
        src = open(os.path.join(REPO, rel), encoding='utf-8').read()
        assert 'install_x_error_guard' in src, f'{rel} does not install the X guard'
