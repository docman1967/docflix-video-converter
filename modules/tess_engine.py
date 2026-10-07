"""Persistent Tesseract engines via the system libtesseract C API.

⭐ WHY THIS EXISTS (measured 2026-10-07, Arrow S02E05, 584 PGS cues):
`pytesseract.image_to_string` launches the `tesseract` PROGRAM for every cue, and the
program reloads the 15 MB language model each time. A BLANK image cost 218 ms; a real
subtitle line 264 ms. ~80% of every OCR call was start-up. Calling the same library
the program uses, with the model loaded once, brought it to 62 ms per cue with
byte-identical text on all 584 cues (same engine, same version, same traineddata).

⭐ ctypes releases the GIL during the foreign call, so worker threads run Tesseract in
genuine parallel. pytesseract's subprocess did too, but each also paid the start-up.

⚠️ Engines live in a POOL, not in threading.local(). The OCR ThreadPoolExecutor is
created per run; a thread-local engine would die with its thread and leak ~15 MB of
model per worker per episode for the whole session. Pooled engines are reused across
runs and freed at interpreter exit (which also silences libtesseract's "LEAK!" noise).

⚠️ If the library can't be loaded or initialised, `image_to_string` returns None and
the caller MUST fall back to pytesseract. Never let this module turn a working OCR
into empty cues — both OCR call sites swallow exceptions, so a failure here would be
SILENT (see _ocr_cue's docstring).
"""
import atexit
import ctypes
import ctypes.util
import threading

_lib = None
_load_failed = False
_lock = threading.Lock()
_idle = {}        # lang -> [engine handles not in use]
_all = []         # every engine ever created, for cleanup


def _load():
    global _lib, _load_failed
    if _lib is not None or _load_failed:
        return _lib
    try:
        lib = ctypes.CDLL(ctypes.util.find_library("tesseract") or "libtesseract.so.5")
        lib.TessBaseAPICreate.restype = ctypes.c_void_p
        lib.TessBaseAPIInit2.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                         ctypes.c_char_p, ctypes.c_int]
        lib.TessBaseAPISetPageSegMode.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.TessBaseAPISetImage.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int,
                                            ctypes.c_int, ctypes.c_int, ctypes.c_int]
        lib.TessBaseAPIGetUTF8Text.argtypes = [ctypes.c_void_p]
        lib.TessBaseAPIGetUTF8Text.restype = ctypes.c_void_p
        lib.TessDeleteText.argtypes = [ctypes.c_void_p]
        lib.TessBaseAPIClear.argtypes = [ctypes.c_void_p]
        lib.TessBaseAPIEnd.argtypes = [ctypes.c_void_p]
        lib.TessBaseAPIDelete.argtypes = [ctypes.c_void_p]
        _lib = lib
    except Exception:
        _load_failed = True
    return _lib


def _checkout(lang):
    with _lock:
        free = _idle.setdefault(lang, [])
        if free:
            return free.pop()
    lib = _load()
    if lib is None:
        return None
    api = lib.TessBaseAPICreate()
    # 3 = OEM_DEFAULT and 6 = PSM_SINGLE_BLOCK: the same as '--psm 6 --oem 3'.
    if not api or lib.TessBaseAPIInit2(api, None, lang.encode(), 3) != 0:
        if api:
            lib.TessBaseAPIDelete(api)
        return None
    lib.TessBaseAPISetPageSegMode(api, 6)
    with _lock:
        _all.append(api)
    return api


def _checkin(lang, api):
    with _lock:
        _idle.setdefault(lang, []).append(api)


def image_to_string(img, lang):
    """OCR a PIL image at --psm 6 --oem 3. Returns None if the engine is unavailable."""
    import numpy as np
    api = _checkout(lang)
    if api is None:
        return None
    try:
        arr = np.ascontiguousarray(np.asarray(img.convert("L"), dtype=np.uint8))
        h, w = arr.shape
        _lib.TessBaseAPISetImage(api, arr.ctypes.data, w, h, 1, w)
        p = _lib.TessBaseAPIGetUTF8Text(api)
        text = ctypes.string_at(p).decode("utf-8", "replace") if p else ""
        if p:
            _lib.TessDeleteText(p)
        _lib.TessBaseAPIClear(api)
        return text
    finally:
        _checkin(lang, api)


@atexit.register
def _shutdown():
    with _lock:
        for api in _all:
            try:
                _lib.TessBaseAPIEnd(api)
                _lib.TessBaseAPIDelete(api)
            except Exception:
                pass
        _all.clear()
        _idle.clear()
