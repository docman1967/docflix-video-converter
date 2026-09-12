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
