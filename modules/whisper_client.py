"""Client side of the isolated whisper engine — runs in the Suite's own interpreter.

Drop-in replacements for the three transcribe entry points in
`modules/whisper_subtitles.py`. Same arguments, same return shapes — the difference is
that the work happens in a subprocess using the engine venv, so the Suite never imports
whisperx or torch into its own process and never installs them into the user's Python.

    from modules import whisper_client as wc
    segs = wc.transcribe(audio, "large-v2", "en", "cuda", 5, True)
    main, forced, report = wc.transcribe_with_forced(audio, ...)

⚠️ `available()` is the gate. If the engine is not built these raise EngineMissing, and
the CALLER decides what to do — normally show the disclosure from whisper_engine and
offer to build it. This module never installs anything on its own; that is a decision
the user makes with the facts in front of them.

RETURN SHAPES
Segments come back as plain dicts, not SubSegment objects — they crossed a process
boundary as JSON. Fields: start, end, text, and words[] when word_timestamps was on.
⚠️ Anything downstream doing `seg.start` needs `seg["start"]`, or wrap with _Seg below.
"""

import json
import os
import subprocess
import tempfile

from . import whisper_engine

WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "whisper_worker.py")
SUITE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# A transcription of a long film can legitimately run a long time; the cap exists only
# to stop a truly wedged process sitting there forever.
TIMEOUT = 60 * 60 * 4


class EngineMissing(RuntimeError):
    """The isolated engine has not been built yet."""


class WorkerFailed(RuntimeError):
    """The worker ran and failed. Carries the child's traceback when there was one."""

    def __init__(self, message, tb=None):
        super().__init__(message)
        self.tb = tb


class _Seg(dict):
    """Attribute access over a segment dict, so `seg.start` keeps working downstream.

    The Suite's existing code was written against SubSegment objects. Rather than
    rewrite every consumer, segments come back as dicts that also answer to attributes.
    """

    def __getattr__(self, k):
        try:
            v = self[k]
        except KeyError:
            raise AttributeError(k)
        if k == "words" and isinstance(v, list):
            return [_Seg(w) if isinstance(w, dict) else w for w in v]
        return v


def available():
    return whisper_engine.is_installed()


def _run(mode, args, progress=None):
    """Run one job in the engine venv and return its parsed result."""
    py = whisper_engine.venv_python()
    if not py:
        raise EngineMissing(
            "The Whisper engine is not installed. "
            "Install it from the Transcriber, or call whisper_engine.build().")

    job = {"mode": mode, "suite_dir": SUITE_DIR,
           "args": {k: (str(v) if hasattr(v, "__fspath__") else v)
                    for k, v in args.items()}}

    jf = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump(job, jf)
    jf.close()

    result = None
    err = None
    try:
        proc = subprocess.Popen([py, WORKER, jf.name],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, bufsize=1)
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                # Stray output — pip warnings, library chatter. Not fatal, but pass it
                # along so it is visible rather than swallowed.
                if progress:
                    progress(line[:300])
                continue
            kind = msg.get("kind")
            if kind == "progress" and progress:
                progress(msg.get("message", ""))
            elif kind == "result":
                result = msg
            elif kind == "error":
                err = msg
        try:
            proc.wait(timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise WorkerFailed(f"transcription exceeded {TIMEOUT // 3600}h and was stopped")

        if err:
            raise WorkerFailed(err.get("message", "worker failed"), err.get("traceback"))
        if result is None:
            # ⚠️ Exit code alone is not proof. A worker that exits 0 having emitted no
            # result has failed, and saying so here beats returning an empty list that
            # looks like "this file has no speech".
            stderr = (proc.stderr.read() or "").strip()[-1000:]
            raise WorkerFailed(
                f"worker produced no result (exit {proc.returncode})"
                + (f"\n{stderr}" if stderr else ""))
        return result
    finally:
        try:
            os.unlink(jf.name)
        except OSError:
            pass


def transcribe_batch(paths, model_size, language, device, engine="faster-whisper",
                     beam_size=5, vad=True, task="transcribe", word_timestamps=False,
                     batch_size=16, on_file=None, on_error=None, progress=None,
                     should_stop=None, on_start=None):
    """Transcribe many files with the model loaded ONCE.

    ⚠️ Use this for batches, never a loop over transcribe(). `medium` costs ~40s to
    load cold, so a 50-file batch that reloaded per file would spend half an hour doing
    nothing but loading. The Suite's in-process loop already loaded once; this preserves
    that rather than regressing it.

    Callbacks fire as results stream in, so a GUI stays live:
        on_start(idx, path)            a file is beginning
        on_file(idx, path, segments)   one finished file
        on_error(idx, path, message)   one failed file — the batch CONTINUES
        progress(text)                 human-readable status
        should_stop()                  return True to cancel

    ⚠️ Cancellation KILLS the worker, which interrupts mid-file. The old in-process loop
    could only check between files.

    Returns {idx: segments} for everything that succeeded.
    """
    py = whisper_engine.venv_python()
    if not py:
        raise EngineMissing("The Whisper engine is not installed.")

    job = {"mode": "batch", "suite_dir": SUITE_DIR, "args": {
        "paths": [str(p) for p in paths], "engine": engine,
        "model_size": model_size, "language": language, "device": device,
        "beam_size": beam_size, "vad": vad, "task": task,
        "word_timestamps": word_timestamps, "batch_size": batch_size}}

    jf = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump(job, jf)
    jf.close()

    done = {}
    proc = None
    try:
        proc = subprocess.Popen([py, WORKER, jf.name], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, bufsize=1)
        for line in proc.stdout:
            if should_stop and should_stop():
                proc.kill()
                if progress:
                    progress("Batch cancelled.")
                break
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                if progress:
                    progress(line[:300])
                continue
            k = msg.get("kind")
            if k == "progress" and progress:
                progress(msg.get("message", ""))
            elif k == "file_start" and on_start:
                on_start(msg["idx"], msg.get("path"))
            elif k == "file_done":
                done[msg["idx"]] = [_Seg(s) for s in msg.get("segments") or []]
                if on_file:
                    on_file(msg["idx"], msg.get("path"), done[msg["idx"]])
            elif k == "file_error" and on_error:
                on_error(msg["idx"], msg.get("path"), msg.get("message", "failed"))
            elif k == "error":
                raise WorkerFailed(msg.get("message", "worker failed"),
                                   msg.get("traceback"))
        if proc.poll() is None:
            proc.wait(timeout=60)
        return done
    finally:
        if proc and proc.poll() is None:
            proc.kill()
        try:
            os.unlink(jf.name)
        except OSError:
            pass


def transcribe(audio_path, model_size, language, device, beam_size, vad,
               task="transcribe", word_timestamps=False, progress=None):
    r = _run("transcribe", dict(
        audio_path=audio_path, model_size=model_size, language=language,
        device=device, beam_size=beam_size, vad=vad, task=task,
        word_timestamps=word_timestamps), progress)
    return [_Seg(s) for s in r.get("segments") or []]


def transcribe_whisperx(audio_path, model_size, language, device, beam_size,
                        task="transcribe", word_timestamps=False, batch_size=16,
                        progress=None):
    r = _run("transcribe_whisperx", dict(
        audio_path=audio_path, model_size=model_size, language=language,
        device=device, beam_size=beam_size, task=task,
        word_timestamps=word_timestamps, batch_size=batch_size), progress)
    return [_Seg(s) for s in r.get("segments") or []]


def transcribe_with_forced(audio_path, model_size, language, device, beam_size, vad,
                           word_timestamps=False, native_lang="en", progress=None):
    """Returns (main_segs, forced, report) — the same 3-tuple as the in-process version."""
    r = _run("transcribe_with_forced", dict(
        audio_path=audio_path, model_size=model_size, language=language,
        device=device, beam_size=beam_size, vad=vad,
        word_timestamps=word_timestamps, native_lang=native_lang), progress)
    return ([_Seg(s) for s in r.get("segments") or []],
            [_Seg(s) for s in r.get("forced") or []],
            r.get("report"))
