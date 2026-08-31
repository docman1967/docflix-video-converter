"""
The probe worker must keep probing while an encode is running.

Background (2026-08-31): Tony reported two things, and they turned out to be one
bug with two doors:

  1. Starting a batch while a scan was still running stopped the scan.
  2. Adding new media while the encoder was already running didn't probe it.

Both probe loops did::

    if self.is_converting:
        break        # don't hog CPU during active conversion

``break`` leaves the loop permanently. There was no resume and no re-queue, so
door 1 abandoned every file the loop hadn't reached yet, and door 2 tripped the
guard on the *first* iteration and probed nothing at all. Neither was visible,
because the "Metadata loaded for N file(s)" message ran unconditionally
afterwards and reported success either way.

⚠️ The consequence is not cosmetic. ``has_closed_captions`` defaults to False and
is only ever set by the probe. It feeds the encode, where converter.py gates the
A53 CC passthrough flag on it — so an unprobed file comes out of a re-encode
with its closed captions silently dropped.

These tests drive the REAL methods against a stub host (no Tk, no display) and
assert the properties that failed before: work is never abandoned, files added
mid-drain are picked up, and CC detection actually lands on every file.

⚠️ Every one of these fails against the old `break` implementation — which is the
only reason they're worth having.
"""

import threading
import time

import pytest

import video_converter as vc


class StubApp:
    """The minimum surface _enqueue_probe / _probe_worker_loop touch.

    ``root.after`` runs the callback inline instead of on a Tk event loop, which
    is fine here: every callback is a widget update or a log line.
    """

    PROBE_THROTTLE_SECS = 0.0   # real value is 2.0s; we're not testing patience

    def __init__(self):
        self.files = []
        self.is_converting = False
        self._probe_queue = vc.queue.Queue()
        self._probe_thread = None
        self._probe_lock = threading.Lock()
        self._probe_total = 0
        self._probe_done = 0
        self._probe_started = None
        self._scanning_files = False
        self.logs = []
        self.root = self
        self.progress_var = self
        self.progress_label = self
        self.status_label = self
        self.file_tree = self

    # -- Tk stand-ins -----------------------------------------------------
    def after(self, _delay, fn=None, *a):
        if fn is not None:
            fn(*a)

    def set(self, _v):
        pass

    def configure(self, **_kw):
        pass

    def get_children(self):
        return []

    def add_log(self, msg, level='INFO'):
        self.logs.append((level, msg))

    def _refresh_tree_row(self, *_a):
        pass

    def _current_settings(self):
        return {}

    # -- the real code under test ----------------------------------------
    _enqueue_probe = vc.VideoConverterApp._enqueue_probe
    _probe_worker_loop = vc.VideoConverterApp._probe_worker_loop
    _probe_one = vc.VideoConverterApp._probe_one
    _refresh_row_for = vc.VideoConverterApp._refresh_row_for
    _update_probe_progress = vc.VideoConverterApp._update_probe_progress
    _probe_drained = vc.VideoConverterApp._probe_drained


@pytest.fixture
def app(monkeypatch):
    """A stub app whose probe primitives are cheap and counted."""
    probed = []

    monkeypatch.setattr(vc, 'get_video_duration', lambda p: (probed.append(p), 60.0)[1])
    monkeypatch.setattr(vc, 'format_duration', lambda s: '00:01:00')
    monkeypatch.setattr(vc, 'estimate_output_size', lambda p, s: '~100 MB')
    monkeypatch.setattr(vc, 'detect_closed_captions', lambda p: True)

    a = StubApp()
    a.probed = probed
    return a


def _make_files(app, n, start=0):
    infos = [{'path': f'/fake/file{i}.mkv', 'duration_secs': None,
              'has_closed_captions': False} for i in range(start, start + n)]
    app.files.extend(infos)
    return infos


def _drain(app, timeout=10.0):
    """Wait for the probe worker to finish, without sleeping blindly."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        t = app._probe_thread
        if t is None and app._probe_queue.empty():
            return True
        time.sleep(0.02)
    return False


# ── The regression: an encode must not abandon the queue ──────────────────

def test_probing_continues_while_converting(app):
    """The bug, door 1: a batch starting mid-scan used to kill the scan.

    Against the old `break`, this probes ZERO files.
    """
    infos = _make_files(app, 12)
    app.is_converting = True          # encode running the whole time

    app._enqueue_probe(infos)
    assert _drain(app), "probe worker never drained"

    assert len(app.probed) == 12, (
        f"only {len(app.probed)}/12 files probed while converting — "
        "the encode is still interrupting the probe")
    assert all(f['duration_secs'] == 60.0 for f in infos)


def test_closed_captions_detected_during_encode(app):
    """The consequence that actually costs data.

    An unprobed file keeps has_closed_captions=False, which drops the A53 CC
    passthrough flag in converter.py and strips captions out of the re-encode.
    """
    infos = _make_files(app, 5)
    app.is_converting = True

    app._enqueue_probe(infos)
    assert _drain(app)

    assert all(f['has_closed_captions'] for f in infos), (
        "CC detection did not run on every file — captions would be silently "
        "dropped from the re-encode")


def test_files_added_mid_drain_are_picked_up(app):
    """The bug, door 2: media added while the encoder runs was never probed."""
    first = _make_files(app, 6)
    app.is_converting = True
    app._enqueue_probe(first)

    # Land a second batch while the worker is mid-flight, exactly like dropping
    # files into the window during a batch.
    second = _make_files(app, 6, start=100)
    app._enqueue_probe(second)

    assert _drain(app)
    assert len(app.probed) == 12
    assert all(f['duration_secs'] == 60.0 for f in first + second)


def test_enqueue_after_worker_exits_restarts_it(app):
    """The wake-up race: enqueueing once the queue has drained must restart it.

    The worker clears _probe_thread under the lock and only when the queue is
    genuinely empty; _enqueue_probe puts first, then starts under that same
    lock. If that pairing is ever broken, the second batch sits forever.
    """
    first = _make_files(app, 3)
    app._enqueue_probe(first)
    assert _drain(app)
    assert app._probe_thread is None

    second = _make_files(app, 3, start=200)
    app._enqueue_probe(second)
    assert _drain(app), "worker did not restart for a batch enqueued after drain"
    assert len(app.probed) == 6


def test_already_probed_files_are_skipped(app):
    """Re-queueing the whole list must be free — the folder scan does exactly that."""
    infos = _make_files(app, 4)
    app._enqueue_probe(infos)
    assert _drain(app)
    assert len(app.probed) == 4

    app._enqueue_probe(infos)          # same list again
    assert _drain(app)
    assert len(app.probed) == 4, "already-probed files were probed a second time"


def test_removed_files_are_not_probed(app):
    """A file the user deleted from the queue shouldn't cost a probe."""
    infos = _make_files(app, 4)
    doomed = infos[1]
    app._probe_queue.put((doomed, {}))
    with app._probe_lock:
        app._probe_total += 1
    app.files.remove(doomed)

    app._enqueue_probe([f for f in infos if f is not doomed])
    assert _drain(app)

    assert doomed['path'] not in app.probed
    assert doomed['duration_secs'] is None


def test_completion_message_reports_the_real_count(app):
    """The old code logged success even when it had abandoned everything."""
    infos = _make_files(app, 7)
    app.is_converting = True
    app._enqueue_probe(infos)
    assert _drain(app)

    done = [m for lvl, m in app.logs if m.startswith('Metadata loaded for')]
    assert done, "no completion message logged"
    assert '7 file(s)' in done[-1], f"completion message misreports the count: {done[-1]}"
    assert app._scanning_files is False
