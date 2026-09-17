#!/usr/bin/env python3
"""The Rescaler's queue must be read LIVE, not snapshotted when Process is hit.

⚠️⚠️ WHY THIS FILE EXISTS. Tony, 2026-09-17:

    "If a user adds a file and starts the process and then adds more while that
     first one is running, when the first one finishes, it doesn't continue to
     the ones just added."

`_start_processing._run()` did:

    total = len(files)          # snapshotted BEFORE any work starts
    ...
    idx = counters['next']
    if idx >= total:            # compared against the stale snapshot
        return

The added files DID reach `files` — Add Files is not disabled during a run — so
the list grew, the tree grew, and the user could see the new rows sitting there.
The worker simply stopped counting at the old total and returned.

⭐ The failure is SILENT and looks like success: the run "completes", the button
re-enables, and the summary says "Done: 1/1 files" while three untouched rows sit
on screen. Nothing errors. That is why it needs a test rather than a fix alone.

⚠️ There is a second bug in the same three lines. `Clear` is also live during a
run, and `files[idx]` evaluated OUTSIDE the lock on a just-emptied list raises
IndexError on a worker thread — which surfaces as a lane that quietly stops, not
as a traceback anyone sees. Reading the item inside the lock fixes both, and
makes Clear mean "stop after the current file".

Run:  python3 tests/test_rescaler_live_queue.py
"""
import os
import re
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, '..', 'modules', 'video_scaler.py')


# ── 1. Behavioural: the worker pattern itself ───────────────────────────────
#
# The real worker lives several closures deep inside a GUI builder and cannot be
# imported, so replicate the pattern exactly and prove the property. The
# structural tests below then pin that the shipped code uses this shape.

def _run_workers(files, lanes, add_after_first=None):
    """Drive the worker loop the way video_scaler does. Returns processed items."""
    counters = {'done': 0, 'failed': 0, 'next': 0}
    clock = threading.Lock()
    processed = []
    first_done = threading.Event()

    def worker():
        while True:
            with clock:
                idx = counters['next']
                if idx >= len(files):          # ← LIVE, the fix
                    return
                item = files[idx]              # ← inside the lock, the fix
                counters['next'] += 1
            processed.append(item)
            with clock:
                counters['done'] += 1
            if not first_done.is_set():
                first_done.set()
                if add_after_first is not None:
                    # the user drops more files in while lane 0 is mid-file
                    files.extend(add_after_first)

    threads = [threading.Thread(target=worker) for _ in range(lanes)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return processed


def test_files_added_mid_run_are_picked_up():
    """⭐ THE REPORTED BUG. Start with one file, add three while it runs; all
    four must be processed."""
    files = ['a.mkv']
    got = _run_workers(files, lanes=1, add_after_first=['b.mkv', 'c.mkv', 'd.mkv'])
    assert got == ['a.mkv', 'b.mkv', 'c.mkv', 'd.mkv'], got
    assert len(got) == 4, f"stopped at {len(got)} — the queue was snapshotted"


def test_the_snapshot_version_actually_fails():
    """⚠️ A test that cannot show the failure proves nothing. Same scenario with
    the OLD code shape must stop at 1 — otherwise the test above is vacuous."""
    files = ['a.mkv']
    total = len(files)                          # the OLD line
    processed = []
    idx = 0
    while idx < total:                          # the OLD comparison
        processed.append(files[idx])
        idx += 1
        if len(processed) == 1:
            files.extend(['b.mkv', 'c.mkv', 'd.mkv'])
    assert processed == ['a.mkv'], processed
    assert len(processed) == 1, "the old shape did not reproduce the bug"


def test_multi_gpu_lanes_share_the_growing_queue():
    """Two lanes must not double-process, and must both see the additions."""
    files = ['a.mkv', 'b.mkv']
    got = _run_workers(files, lanes=2, add_after_first=['c.mkv', 'd.mkv', 'e.mkv'])
    assert sorted(got) == ['a.mkv', 'b.mkv', 'c.mkv', 'd.mkv', 'e.mkv'], got
    assert len(got) == len(set(got)), f"a file was processed twice: {got}"


def test_clearing_mid_run_stops_cleanly():
    """⚠️ Clear is live during a run. Emptying the list must end the loop, not
    raise IndexError on a worker thread (which reads as a dead lane)."""
    files = ['a.mkv', 'b.mkv', 'c.mkv']
    counters = {'next': 0}
    clock = threading.Lock()
    processed, errors = [], []

    def worker():
        try:
            while True:
                with clock:
                    idx = counters['next']
                    if idx >= len(files):
                        return
                    item = files[idx]
                    counters['next'] += 1
                processed.append(item)
                if len(processed) == 1:
                    files.clear()               # user hits Clear
        except Exception as e:                  # noqa: BLE001
            errors.append(e)

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert not errors, f"Clear raised on the worker thread: {errors}"
    assert processed == ['a.mkv'], processed


# ── 2. Structural: the shipped code uses the live length ────────────────────

def _src():
    with open(SRC, encoding='utf-8') as fh:
        return fh.read()


def _worker_body():
    s = _src()
    i = s.index('def _worker(gpu):')
    j = s.index('threads = [threading.Thread(target=_worker', i)
    return s[i:j]


def test_worker_compares_against_the_live_length():
    body = _worker_body()
    assert 'idx >= len(files)' in body, (
        "the worker no longer bounds-checks against the live list length")
    assert not re.search(r'if\s+idx\s*>=\s*total\b', body), (
        "the `total` snapshot comparison is back — that IS the bug")


def test_worker_reads_the_item_inside_the_lock():
    """⚠️ `files[idx]` outside the lock races with Clear."""
    body = _worker_body()
    lock_open = body.index('with clock:')
    call = body.index('_process_one(')
    assert 'files[idx]' not in body[call:], (
        "_process_one is indexing the live list outside the lock again")
    assert 'f_item = files[idx]' in body[lock_open:call], (
        "the queue item is no longer taken inside the lock")


def test_the_summary_uses_the_live_count():
    """⚠️ With the snapshot, a run that grew reported 'Done: 1/1 files' after
    doing four, and the skipped figure could go negative."""
    s = _src()
    i = s.index("_log(f\"Complete: {done} done")
    tail = s[i - 300:i + 400]
    assert 'final_total = len(files)' in tail, "the summary still uses a snapshot"
    assert 'max(0,' in tail, "skipped can still go negative"
    assert '{total - done - failed}' not in tail


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
    print(f"\n{'ALL PASS' if not fails else str(len(fails)) + ' FAILED'}")
    sys.exit(1 if fails else 0)
