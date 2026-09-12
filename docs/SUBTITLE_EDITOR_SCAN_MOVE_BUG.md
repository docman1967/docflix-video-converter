# Subtitle Editor — moving the scan dialog loses the subtitle track list

**Status: REPORTED, not yet reproduced or fixed.** Tony, 2026-09-12:

> *"If a user moves the scan window and the scan finishes before the window move is finished, it
> doesn't display the subtitle tracks. But that's in the subtitle editor not the OCR part."*

Separate component from [[OCR_REVIEW_PANE]] — both are on the same Media Suite session, but this is
`modules/subtitle_editor.py`, not `modules/subtitle_ocr.py`.

## Repro (to confirm first)

1. Open a video in the Subtitle Editor → the "Scanning" dialog appears.
2. **Grab that dialog by its title bar and keep dragging** while the scan runs.
3. Let the scan finish *while the drag is still in progress*.
4. Dialog closes, but the subtitle track list never populates.

## ⭐ Candidate cause — the payload sits behind the teardown

`modules/subtitle_editor.py`, ~line 735:

```python
def _check_scan():
    if scan_thread.is_alive():
        editor.after(50, _check_scan)
        return
    scan_bar.stop()
    scan_dlg.grab_release()                            # <-- can raise mid-move
    scan_dlg.destroy()                                 # <-- can raise mid-move
    _finish_load_video(video_path, *scan_result[0])    # <-- NEVER RUNS if either did
```

`_finish_load_video()` is what populates the track list. It is the **last** statement, after three
teardown calls on a Toplevel that the window manager may currently hold in an interactive move —
during which the WM owns the pointer grab. If `grab_release()` or `destroy()` throws, the callback
dies silently and the scan result in `scan_result[0]` is simply discarded.

⚠️ Fits the symptom precisely: **the scan succeeded, the dialog went away, and the data never
arrived.** Nothing looks like a crash.

Relevant context: `scan_dlg.grab_set()` is called at line 722, and the dialog is `transient(editor)`
while the polling callback is scheduled on **`editor`**, not on `scan_dlg` — so the poll survives
the dialog, but the work after the teardown does not.

## ⭐⭐ THE REAL ROOT CAUSE — the dialog never centres, so he moves it

Tony, 2026-09-12: *"I'm a fidgiter when it comes to my windows. The scan window doesn't center when
it runs and part of me wants it centered so I move it without thinking about it."*

**The moving is a SYMPTOM, not the cause.** The chain is:

    centring fails -> dialog lands off-centre -> he drags it without thinking
    -> scan finishes mid-drag -> track list lost

⭐ **Fix the centring and the trigger disappears.** That is a better fix than tolerating the race,
and it also removes a papercut he hits every single time he opens a video.

### Why the centring fails — two separate faults

**1. The scan dialog uses the path that is documented not to work.** `video_converter.py:4784`
carries a long comment from measurements on 2026-08-05:

    d.geometry("520x640")   -> still reports "1x1+0+0"
    d.update_idletasks()    -> reports correctly ...and MAPS it (the flash)
    withdraw() first        -> stays "1x1" forever; the request is lost

The documented escape is for the **caller** to withdraw at creation and pass its known size:

    dlg = tk.Toplevel(self.root); dlg.withdraw()
    ...build widgets...
    self._center_on_main(dlg, size=(W, H))
    dlg.deiconify()

`subtitle_editor.py` ~line 706 does **neither** — no `withdraw()`, no `size=`. So the geometry is
read as 1x1 and the placement maths is wrong.

**2. ⚠️ The two `_center_on_main` implementations have DIFFERENT SIGNATURES:**

| where | signature |
|---|---|
| `video_converter.py:4784` | `_center_on_main(self, dlg, size=None)` |
| `modules/standalone.py:192` | `_center_on_main(self, win)` — **no `size` at all** |

So the documented fix cannot be applied in standalone mode without a `TypeError`. ⚠️ **Fix the
signature mismatch first**, or the fix works in the Suite and crashes the standalone tool.

Also in standalone mode, `_center_on_main` walks `self.root.winfo_children()` for a *viewable*
sibling Toplevel and **silently returns without centring** if it finds none — "let the window
manager place it". A plausible third path to "it didn't centre".

### ⚠️ Which path actually runs for him is NOT yet established

Confirmed by reading code, not by running it. Before fixing: determine whether he hits the Suite
copy or the standalone copy, and whether centring is computing wrong coordinates or being skipped
entirely. `center_window_on_parent()` (`modules/utils.py`) does call `update_idletasks()` first, so
in the Suite path the size *should* resolve — meaning a wrong-parent or skipped-centring
explanation is more likely than a 1x1 one. **Measure before changing.**

### Suggested order of work

1. **Fix the centring** — removes the trigger and a daily annoyance. Highest value.
2. **Unify the two `_center_on_main` signatures** — otherwise (1) breaks standalone.
3. **Make the teardown non-fatal** (below) — belt and braces, since any modal dialog can be dragged
   at the wrong moment and losing the scan result should never be the consequence.

## Likely fix shape (do NOT apply before reproducing)

Make the teardown incapable of preventing the payload:

```python
def _check_scan():
    if scan_thread.is_alive():
        editor.after(50, _check_scan)
        return
    try:
        scan_bar.stop()
        scan_dlg.grab_release()
        scan_dlg.destroy()
    except Exception:
        pass                      # a cosmetic teardown failure must not eat the result
    _finish_load_video(video_path, *scan_result[0])
```

⚠️ **Verify the exception theory before "fixing" it.** If nothing is actually raising, this change
makes the bug *invisible* rather than absent, and the real cause (a dropped/rescheduled `after`
during the WM move, or a `_finish_load_video` that fails on its own) goes unfound. Put a real
`except Exception as e: log(...)` in temporarily and reproduce, rather than assuming.

## ⚠️ This is a known neighbourhood

- [[project_subtitle-editor-drop-crash]] — dragging onto this same editor previously hit an
  **Xlib BadWindow** that killed the process below Python (no traceback, no core dump), fixed with
  a ctypes `XSetErrorHandler` guard. Window-manager interactions with this editor have form.
  ⚠️ Check whether the guard is swallowing an X error here too.
- `detect_cc_types()` runs a **180-second CC probe** inside `_do_scan()` — which is *why* the scan
  dialog is on screen long enough for a user to think about moving it. Shortening that probe would
  reduce exposure, but is a separate issue.

## Related

- `docs/OCR_REVIEW_PANE.md` — the other item queued for the same session.
