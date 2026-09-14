#!/usr/bin/env python3
"""One tool's log output must not land in another tool's window — or worse,
replace the crash log for the whole process.

⚠️⚠️ WHY THIS FILE EXISTS. `TranscribeWorker.run()` did:

    sys.stdout = QueueStream(self.q, prefix="   ")
    sys.stderr = QueueStream(self.q, prefix="   ")

QueueStream's own docstring says "attach inside the worker thread". That was
the intent, and it is NOT what a bare assignment does — sys.stdout and
sys.stderr are PROCESS-GLOBAL. So for the entire duration of a transcribe
batch, everything anywhere in the Suite that wrote to stderr was funnelled into
the Whisper window's Log panel, and logs/video_converter_*.log received nothing
at all.

Tony saw the cosmetic half on 2026-09-14: "I'm running a whisper transcribe
batch and decided to launch the subtitle editor. I noticed the log output is
showing in the whisper app."

⚠️ The serious half was invisible. subtitle_editor._trace() writes to stderr
*specifically* so crash diagnostics survive the window being torn down — its
docstring forbids the GUI logger for that exact reason. The redirect turned it
back into a GUI-only logger, so an editor crash mid-batch put the evidence into
a Tk widget that then died with the app. That instrumentation was written
because the vanishing-window bug cost two days for want of such a trace.

⭐ So the first test below is the one that matters: not "does the worker's
output reach the panel" but "does everyone ELSE's output still reach the file".
"""
import io
import os
import queue
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.whisper_transcriber import (                        # noqa: E402
    QueueStream, _ThreadScopedStream)


def _drain(q):
    out = []
    while not q.empty():
        item = q.get()
        out.append(item[1] if isinstance(item, tuple) else item)
    return out


def test_other_threads_still_reach_the_real_stream():
    """⚠️⚠️ THE ONE THAT MATTERS. A write from any thread but the worker must
    reach the real stderr, which is what run_converter.sh captures into
    logs/video_converter_*.log. This is the crash-diagnostic path."""
    q, real = queue.Queue(), io.StringIO()
    done = threading.Event()

    def worker():
        me = threading.current_thread()
        scoped = _ThreadScopedStream(me, QueueStream(q, prefix="   "), real)
        t = threading.Thread(
            target=lambda: scoped.write("[Docflix/SubEditor] drop -> x.mkv\n"))
        t.start()
        t.join()
        done.set()

    w = threading.Thread(target=worker)
    w.start()
    w.join()
    assert done.is_set()
    assert "[Docflix/SubEditor]" in real.getvalue()
    assert not any("SubEditor" in line for line in _drain(q)), (
        "another tool's output leaked into the transcriber's log panel")


def test_the_worker_thread_still_reaches_the_panel():
    """The feature must keep working — library chatter (tqdm, pyannote,
    huggingface) is the reason the redirect exists at all."""
    q, real = queue.Queue(), io.StringIO()

    def worker():
        me = threading.current_thread()
        scoped = _ThreadScopedStream(me, QueueStream(q, prefix="   "), real)
        scoped.write("Performing voice activity detection...\n")

    w = threading.Thread(target=worker)
    w.start()
    w.join()
    assert any("voice activity" in line for line in _drain(q))
    assert "voice activity" not in real.getvalue()


def test_nested_workers_do_not_trap_each_other():
    """⚠️ Two batches can overlap, so the second wraps the first. A write from
    neither thread has to fall through BOTH layers to the real stream."""
    q1, q2, real = queue.Queue(), queue.Queue(), io.StringIO()
    inner_holder = {}

    def outer():
        outer_stream = _ThreadScopedStream(
            threading.current_thread(), QueueStream(q1, prefix="   "), real)

        def inner():
            inner_holder['s'] = _ThreadScopedStream(
                threading.current_thread(), QueueStream(q2, prefix="   "),
                outer_stream)
            t = threading.Thread(
                target=lambda: inner_holder['s'].write("third-party line\n"))
            t.start()
            t.join()

        t = threading.Thread(target=inner)
        t.start()
        t.join()

    w = threading.Thread(target=outer)
    w.start()
    w.join()
    assert "third-party line" in real.getvalue()
    assert not _drain(q1) and not _drain(q2)


def test_a_dead_passthrough_cannot_kill_a_batch():
    """⚠️ Fails open. OCR and transcribe both run unattended across whole
    episodes; an exception raised from a log write would abort the run, which
    is far worse than a lost line."""
    class Dead:
        def write(self, _):
            raise ValueError("stream closed")

        def flush(self):
            raise ValueError("stream closed")

    q = queue.Queue()
    scoped = _ThreadScopedStream(object(), QueueStream(q, prefix=""), Dead())
    assert scoped.write("anything\n") == 0       # not-owner -> dead passthrough
    scoped.flush()                               # must not raise


# ── The editor's own Log panel ──────────────────────────────────────────────
# ⭐ Tony, same report: "It's also being written to the encoder log as well."
# _trace()'s GUI half called app.add_log, which writes to the MAIN window's log
# panel — so every drop and close of the subtitle editor was narrated into the
# encoder's log. Structural checks, because _trace lives several closures deep
# inside a GUI builder and cannot be imported. Same approach as
# test_ocr_save_flush.py.

def _editor_source():
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, '..', 'modules', 'subtitle_editor.py')
    with open(path, encoding='utf-8') as fh:
        return fh.read()


def test_trace_prefers_the_editors_own_log():
    src = _editor_source()
    assert '_editor_log = [None]' in src, "the holder is gone"
    assert 'sink = _editor_log[0]' in src, (
        "_trace no longer consults the editor's own log panel")


def test_the_holder_is_cleared_on_close():
    """⚠️ A stale reference would send teardown traces into a widget being
    destroyed — the precise failure _trace's docstring exists to avoid. Closing
    must drop it back to stderr, which outlives the window."""
    src = _editor_source()
    i_clear = src.find('_editor_log[0] = None')
    i_trace = src.find('_trace("close handler invoked")')
    assert i_clear != -1, "the holder is never cleared on close"
    assert i_trace != -1
    assert i_clear < i_trace, (
        "the holder must be cleared BEFORE the close trace is written")


def test_the_holder_dies_with_the_window_that_owns_the_panel():
    """⚠️⚠️ THE DEFECT IN THE FIRST VERSION OF THIS FIX. The Log panel belongs
    to `mon`, the OCR monitor Toplevel, which is destroyed independently of the
    editor from five different call sites. Clearing the holder only on EDITOR
    close left it pointing at a dead widget — and because `_log` catches
    tk.TclError itself and returns quietly, _trace would treat that as a
    successful write and the message would vanish with no fallback at all.
    Worse than the bug being fixed."""
    src = _editor_source()
    assert src.count('_editor_log[0] = None') >= 2, (
        "the holder must be cleared on BOTH mon destroy and editor close")
    mon_hook = src.find('def _on_mon_destroy')
    assert mon_hook != -1
    tail = src[mon_hook:mon_hook + 900]
    assert '_editor_log[0] = None' in tail, (
        "mon's destroy hook does not clear the log holder")


def test_the_sink_lets_a_dead_panel_fail_loudly():
    """A sink that cannot fail cannot fall back. _log swallows tk.TclError, so
    the registered sink must check the widget itself and raise."""
    src = _editor_source()
    assert 'def _editor_log_sink' in src
    assert 'log_text.winfo_exists()' in src, (
        "the sink no longer checks the panel is alive")
    assert '_editor_log[0] = _editor_log_sink' in src, (
        "_log is being registered directly again — it cannot report failure")
