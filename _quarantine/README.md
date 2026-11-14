# Quarantine

Files pulled out of the app on purpose, kept here (not deleted) in case something turns
out to need them. Nothing in here is imported, installed or tested.

| File | Quarantined | From | Why | Delete after |
|---|---|---|---|---|
| `converter.py` | 2026-11-14 | `modules/converter.py` | Unwired copy of the `VideoConverter` engine (an unfinished extraction from `video_converter.py`, last synced 2026-09-02). The GUI runs its own copy in `video_converter.py`; this one had drifted (e.g. NVENC AQ always on, no GPU tone map) and every fix had to be made twice. | ~2026-11-28, if nothing broke |

If you need it back: `git mv _quarantine/converter.py modules/converter.py`.
