#!/usr/bin/env python3
"""Regression test: the mouse wheel must not silently edit settings.

WHY THIS FILE EXISTS
────────────────────
Tony, 2026-08-09, after finding 19 episodes encoded at preset p1 when he had set
p4: *"Some of the drop downs also change when you use the mouse wheel. I've done
this before where I used the mouse wheel to scroll down in that top window and
ended up changing settings without realizing it. We should fix that."*

ttk widgets respond to a wheel event by changing their VALUE. So scrolling the
settings panel, with the pointer happening to pass over a dropdown, changes a
setting — **no click, no confirmation, no visible cue, no log entry.** Silent
wrongness with a real cost: a whole batch encoded at the wrong preset, and by
his account it had happened before and gone unnoticed.

⚠️ IT WAS ONLY EVER CAUGHT BECAUSE OF THE DOCFLIX_ENCODE STAMP. Nothing else in
the system records what a file was actually encoded with. That is worth
remembering before anyone decides the stamp is not pulling its weight.

WHAT THIS CHECKS
────────────────
Both halves, because either alone is a broken fix:

  1. a wheel event over a Combobox does NOT change its value
  2. the wheel STILL SCROLLS the panel — a guard that simply swallowed the
     event would "pass" the first check while making the UI feel dead

The reproduction below drives the real widget the same way the toolkit does, and
without the guard it reproduces Tony's exact symptom: p4 -> p1.

    python3 tests/test_wheel_guard.py     (needs a display; uses xvfb in CI)
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def build(guarded):
    """A settings-panel-shaped window: scrollable canvas, dropdown inside."""
    import tkinter as tk
    from tkinter import ttk
    from modules.utils import install_wheel_guard

    root = tk.Tk()
    root.geometry('400x200')
    canvas = tk.Canvas(root)
    canvas.pack(fill='both', expand=True)
    inner = ttk.Frame(canvas)
    canvas.create_window((0, 0), window=inner, anchor='nw')
    # Tall enough that the canvas genuinely has somewhere to scroll to.
    for i in range(40):
        ttk.Label(inner, text=f'row {i}').pack()
    cb = ttk.Combobox(inner, values=['p1', 'p2', 'p3', 'p4', 'p5', 'p6', 'p7'],
                      state='readonly')
    cb.set('p4')
    cb.pack()
    inner.update_idletasks()
    canvas.configure(scrollregion=canvas.bbox('all'))
    if guarded:
        install_wheel_guard(root)
    root.update()
    return root, canvas, cb


def wheel(widget, times=3):
    for _ in range(times):
        widget.event_generate('<Button-4>', x=5, y=5)
        widget.event_generate('<MouseWheel>', delta=120, x=5, y=5)


def main():
    if not os.environ.get('DISPLAY'):
        print("\n  no DISPLAY — skipping (run under xvfb-run)\n")
        sys.exit(0)
    try:
        import tkinter  # noqa: F401
    except Exception:
        print("\n  tkinter unavailable — skipping\n")
        sys.exit(0)

    fails = 0
    print()

    # ── 1. without the guard, the bug must reproduce ────────────────────
    # ⚠️ If this stops reproducing, the test can no longer tell a fixed app
    # from a broken one and its green result means nothing.
    root, canvas, cb = build(guarded=False)
    before = cb.get()
    wheel(cb)
    root.update()
    after = cb.get()
    root.destroy()
    reproduced = before != after
    fails += not reproduced
    print(f"    {'ok  ' if reproduced else 'FAIL'} unguarded: the bug still "
          f"reproduces ({before} -> {after})")
    if not reproduced:
        print("           this test can no longer discriminate — investigate")

    # ── 2. with the guard, the value must not move ──────────────────────
    root, canvas, cb = build(guarded=True)
    before = cb.get()
    wheel(cb)
    root.update()
    after = cb.get()
    held = before == after
    fails += not held
    print(f"    {'ok  ' if held else 'FAIL'} guarded: value unchanged "
          f"({before} -> {after})")

    # ── 3. ...but the panel must still scroll ───────────────────────────
    # A guard that just swallows the wheel passes check 2 and makes the UI
    # feel broken. Both halves or it is not a fix.
    canvas.yview_moveto(0.5)
    root.update()
    pos_before = canvas.yview()[0]
    wheel(cb, times=5)
    root.update()
    pos_after = canvas.yview()[0]
    scrolled = abs(pos_after - pos_before) > 1e-6
    fails += not scrolled
    print(f"    {'ok  ' if scrolled else 'FAIL'} guarded: the panel STILL "
          f"scrolls ({pos_before:.3f} -> {pos_after:.3f})")
    root.destroy()

    total = 3
    print(f"\n  {total - fails}/{total} pass\n")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
