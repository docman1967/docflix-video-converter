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
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, '..', 'modules', 'video_scaler.py')


# ── 1. Behavioural: the worker pattern itself ───────────────────────────────
#
# The real worker lives several closures deep inside a GUI builder and cannot be
# imported, so replicate the pattern exactly and prove the property. The
# structural tests below then pin that the shipped code uses this shape.

def _run_workers(files, lanes, add_during_first=None, hold=0.05):
    """Drive the worker loop the way video_scaler does. Returns (processed, lanes_used).

    `add_during_first` is injected WHILE the first file is still being processed —
    which is the real scenario and the one that killed the second lane.
    """
    counters = {'done': 0, 'failed': 0, 'next': 0, 'busy': 0}
    clock = threading.Condition()
    processed, lanes_used = [], set()
    injected = threading.Event()

    def worker(lane):
        while True:
            with clock:
                while True:
                    if counters['next'] < len(files):
                        idx = counters['next']
                        item = files[idx]
                        counters['next'] += 1
                        counters['busy'] += 1
                        break
                    if counters['busy'] == 0:      # empty AND nobody can add more
                        clock.notify_all()
                        return
                    clock.wait(0.25)               # ← idle lane WAITS, does not exit
            # "process" the file
            # ⚠️ EVERY file takes time, not just the first. An earlier version of
            # this harness slept only on the first file, so lane 0 swallowed the
            # whole queue in microseconds before lane 1 could wake — and the test
            # reported a dead lane that was actually fine. A simulation that is
            # faster than reality invents races reality does not have.
            if not injected.is_set():
                injected.set()
                if add_during_first is not None:
                    time.sleep(hold / 2)           # user adds files mid-file
                    files.extend(add_during_first)
            time.sleep(hold)
            processed.append(item)
            lanes_used.add(lane)
            with clock:
                counters['busy'] -= 1
                counters['done'] += 1
                clock.notify_all()

    while True:
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(lanes)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        with clock:
            if counters['next'] >= len(files):
                break
    return processed, lanes_used


def test_files_added_mid_run_are_picked_up():
    """⭐ BUG #1. Start with one file, add three while it runs; all four must be
    processed."""
    files = ['a.mkv']
    got, _ = _run_workers(files, lanes=1, add_during_first=['b.mkv', 'c.mkv', 'd.mkv'])
    assert sorted(got) == ['a.mkv', 'b.mkv', 'c.mkv', 'd.mkv'], got
    assert len(got) == 4, f"stopped at {len(got)} — the queue was snapshotted"


def test_the_second_lane_survives_a_short_starting_queue():
    """⭐⭐ BUG #2 — the one the first fix missed. Tony, 2026-09-17: "If I add 1
    title, start the process and then add several more, it stays at using one GPU
    only."

    Lanes are spawned once, up front. With ONE file queued and TWO lanes, lane 1
    finds the queue empty and exits within a millisecond — and an exited thread
    cannot be revived, so the rest of the run limps along on a single card.
    Starting from a full queue hid it completely, which is why it survived the
    first fix AND its tests."""
    files = ['a.mkv']
    got, used = _run_workers(files, lanes=2,
                             add_during_first=['b.mkv', 'c.mkv', 'd.mkv'])
    assert len(got) == 4, f"only {len(got)} processed: {got}"
    assert len(used) == 2, (
        f"only lane(s) {sorted(used)} did any work — the idle lane exited instead "
        f"of waiting, so the run stayed on one GPU")


def test_an_exiting_lane_is_what_the_old_shape_did():
    """⚠️ Prove the failure is real: with the exit-on-empty shape, lane 1 dies and
    lane 0 does everything."""
    files = ['a.mkv']
    counters = {'next': 0}
    clock = threading.Lock()
    used, processed = set(), []

    def worker(lane):
        while True:
            with clock:
                if counters['next'] >= len(files):
                    return                      # ← the OLD shape: exit on empty
                idx = counters['next']; item = files[idx]; counters['next'] += 1
            if len(processed) == 0:
                time.sleep(0.05)
                files.extend(['b.mkv', 'c.mkv'])
            processed.append(item); used.add(lane)

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for t in ts: t.start()
    for t in ts: t.join()
    assert len(used) == 1, f"expected a single surviving lane, got {sorted(used)}"


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
    got, used = _run_workers(files, lanes=2, add_during_first=['c.mkv', 'd.mkv', 'e.mkv'])
    assert sorted(got) == ['a.mkv', 'b.mkv', 'c.mkv', 'd.mkv', 'e.mkv'], got
    assert len(got) == len(set(got)), f"a file was processed twice: {got}"
    assert len(used) == 2, f"both lanes should have worked, got {sorted(used)}"


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


def test_a_raising_job_cannot_hang_the_other_lanes():
    """⚠️⚠️ NEW RISK FROM THE FIX. The idle-wait blocks while busy > 0, so if the
    per-file work raises and `busy` is never decremented, every other lane waits
    forever and the app looks frozen with no error. The old exit-on-empty worker
    had no such failure mode. try/finally is load-bearing."""
    files = ['boom.mkv', 'b.mkv']
    counters = {'next': 0, 'busy': 0}
    clock = threading.Condition()
    processed = []

    def worker(lane):
        while True:
            with clock:
                while True:
                    if counters['next'] < len(files):
                        idx = counters['next']; item = files[idx]
                        counters['next'] += 1; counters['busy'] += 1
                        break
                    if counters['busy'] == 0:
                        clock.notify_all(); return
                    clock.wait(0.25)
            try:
                if item == 'boom.mkv':
                    raise RuntimeError("ffmpeg blew up")
                processed.append(item)
            except RuntimeError:
                pass
            finally:                              # ← the guard under test
                with clock:
                    counters['busy'] -= 1
                    clock.notify_all()

    ts = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=5)
    assert not any(t.is_alive() for t in ts), (
        "a lane is still waiting — busy never came back down and the run hung")
    assert processed == ['b.mkv'], processed


def test_the_shipped_worker_decrements_busy_in_a_finally():
    body = _worker_body()
    assert 'finally:' in body, (
        "busy is decremented outside a finally — a raising job hangs every other lane")
    i_try, i_fin = body.index('try:'), body.index('finally:')
    i_dec = body.index("counters['busy'] -= 1")
    assert i_try < i_fin < i_dec, "the decrement is not inside the finally block"


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
    assert "counters['next'] < len(files)" in body, (
        "the worker no longer bounds-checks against the live list length")
    assert not re.search(r'if\s+idx\s*>=\s*total\b', body), (
        "the `total` snapshot comparison is back — that IS the bug")


def test_an_idle_lane_waits_instead_of_exiting():
    """⭐⭐ The structural half of BUG #2."""
    body = _worker_body()
    assert 'clock.wait(' in body, (
        "an idle lane exits again instead of waiting — the second GPU will die "
        "whenever the starting queue is shorter than the lane count")
    assert "counters['busy'] == 0" in body, (
        "the run no longer ends on 'queue empty AND every lane idle'")
    src = _src()
    assert 'threading.Condition()' in src, "a bare Lock cannot wait for new work"


def test_the_run_respawns_lanes_if_files_arrive_after_the_join():
    src = _src()
    i = src.index('threads = [threading.Thread(target=_worker')
    tail = src[i - 400:i + 400]
    assert "counters['next'] >= len(files)" in tail, (
        "the respawn loop's exit condition is gone")


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
