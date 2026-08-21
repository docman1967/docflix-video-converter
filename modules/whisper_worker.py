#!/usr/bin/env python3
"""Runs INSIDE the isolated whisper venv. Not imported by the Suite.

    <venv>/bin/python modules/whisper_worker.py job.json

Reads a job description, runs one whole transcription, and writes newline-delimited
JSON to stdout: progress lines while it works, then exactly one result line.

WHY A WORKER AND NOT RPC
────────────────────────
The obvious design — proxy each whisper call across the process boundary — makes
`transcribe_with_forced()` painful, because it is iterative by nature: find suspect
windows by avg_logprob, run detect_language() on only those, re-transcribe clips with
clip_timestamps. Proxying that means many round trips and a model held or reloaded per
call.

So the worker does the WHOLE job instead. The chatty algorithm stays chatty — it just
does its chattering in here, where the model already lives. One subprocess call per file.

⚠️ IT IMPORTS THE SUITE'S OWN FUNCTIONS RATHER THAN REIMPLEMENTING THEM.
`modules/whisper_subtitles.py` imports only stdlib at module level (whisperx and
faster_whisper are imported inside the functions), so it loads cleanly in here: the venv
supplies the ML packages, the Suite supplies the algorithm. This matters more than it
looks — the forced-subtitle thresholds were tuned BY EAR against real releases, so this
is the same code running in a different interpreter, not a port that might drift.
A second copy of that algorithm is exactly the bug we deleted 726 lines to remove.
"""

import json
import os
import sys
import traceback
from pathlib import Path


def emit(kind, **payload):
    """One NDJSON line on stdout. Flushed, because the parent reads live."""
    sys.stdout.write(json.dumps({"kind": kind, **payload}) + "\n")
    sys.stdout.flush()


def ser(obj):
    """Serialise a segment/word to a plain dict.

    Duck-typed on purpose: this has to handle the Suite's SubSegment AND
    faster-whisper's own Segment and Word, which are different classes with
    overlapping attributes. Anything with .start/.end is a time-bearing thing.
    """
    if obj is None:
        return None
    if isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, (list, tuple)):
        return [ser(x) for x in obj]
    if isinstance(obj, dict):
        return {k: ser(v) for k, v in obj.items()}

    out = {}
    for attr in ("start", "end", "text", "word", "probability", "score",
                 "avg_logprob", "no_speech_prob", "language"):
        if hasattr(obj, attr):
            v = getattr(obj, attr)
            if not callable(v):
                out[attr] = ser(v)
    if hasattr(obj, "words"):
        w = getattr(obj, "words")
        if w:
            out["words"] = [ser(x) for x in w]
    return out or str(obj)


def main():
    if len(sys.argv) < 2:
        emit("error", message="usage: whisper_worker.py job.json")
        return 2

    try:
        job = json.load(open(sys.argv[1]))
    except Exception as e:
        emit("error", message=f"could not read job file: {e}")
        return 2

    # The Suite's own modules — supplied by the parent, not installed in the venv.
    suite_dir = job.get("suite_dir")
    if suite_dir and suite_dir not in sys.path:
        sys.path.insert(0, suite_dir)

    try:
        from modules.whisper_subtitles import (
            transcribe, transcribe_with_forced, transcribe_whisperx,
        )
    except Exception as e:
        emit("error", message=f"could not import the Suite's whisper module: {e}",
             traceback=traceback.format_exc()[-1500:])
        return 3

    mode = job.get("mode", "transcribe")
    a = job.get("args", {})
    audio = Path(a.get("audio_path", ""))

    # transcribe_with_forced takes a progress callback. Across a process boundary that
    # becomes a progress line the parent can render — same shape as torch_upscaler.
    def progress(*args):
        try:
            msg = " ".join(str(x) for x in args if x is not None)
            if msg:
                emit("progress", message=msg[:400])
        except Exception:
            pass

    # ── batch mode ────────────────────────────────────────────────────────────
    # ⚠️ THE MODEL IS LOADED ONCE FOR THE WHOLE BATCH. This is the entire reason
    # batch mode exists as a separate path: `medium` takes ~40s to load cold, so a
    # 50-file batch that reloaded per file would spend half an hour doing nothing but
    # loading. The Suite's in-process batch loop already worked this way and the
    # subprocess design has to preserve it, not regress it.
    #
    # ffmpeg is a SYSTEM binary, not a venv dependency, so audio extraction happens in
    # here — that keeps a file's whole journey on one side of the boundary.
    #
    # Cancellation is the parent killing this process. That is strictly better than the
    # old between-files `_stop_event` check, which could not interrupt mid-file.
    if mode == "batch":
        import subprocess as _sp
        import tempfile as _tf
        paths = a.get("paths") or []
        engine = a.get("engine", "faster-whisper")
        emit("progress", message=f"loading {engine} model '{a['model_size']}' once for "
                                 f"{len(paths)} file(s)...")
        AUDIO_EXT = {".wav", ".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wma"}
        for idx, p in enumerate(paths):
            src = Path(p)
            try:
                emit("file_start", idx=idx, path=str(src))
                with _tf.TemporaryDirectory() as tmp:
                    if src.suffix.lower() not in AUDIO_EXT:
                        emit("progress", message=f"[{idx+1}/{len(paths)}] extracting audio...")
                        wav = Path(tmp) / "audio.wav"
                        r = _sp.run(["ffmpeg", "-y", "-i", str(src), "-vn",
                                     "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
                                     str(wav)], capture_output=True, text=True)
                        if r.returncode != 0:
                            raise RuntimeError(f"ffmpeg failed:\n{(r.stderr or '')[-600:]}")
                        audio_in = wav
                    else:
                        audio_in = src

                    emit("progress", message=f"[{idx+1}/{len(paths)}] {src.name}")
                    if engine == "whisperx":
                        segs = transcribe_whisperx(
                            audio_in, a["model_size"], a.get("language"), a["device"],
                            a.get("beam_size", 5), task=a.get("task", "transcribe"),
                            word_timestamps=a.get("word_timestamps", False),
                            batch_size=a.get("batch_size", 16))
                    else:
                        segs = transcribe(
                            audio_in, a["model_size"], a.get("language"), a["device"],
                            a.get("beam_size", 5), a.get("vad", True),
                            task=a.get("task", "transcribe"),
                            word_timestamps=a.get("word_timestamps", False))
                emit("file_done", idx=idx, path=str(src), segments=ser(segs))
            except Exception as e:
                # One bad file must not kill the batch — same as the in-process loop,
                # which caught per-file and carried on.
                emit("file_error", idx=idx, path=str(src), message=str(e),
                     traceback=traceback.format_exc()[-1200:])
        emit("batch_done")
        return 0

    # ── translate_spans (forced-subtitle panel) ───────────────────────────────
    # ⚠️ Decodes the audio ONCE and loads the model ONCE, then slices per span —
    # exactly what the in-process loop did. Doing it per span would re-decode a whole
    # film's audio for every foreign-speech fragment.
    #
    # Per-span language handling is preserved: a span keeps its own detected language
    # if the scan identified one it trusts, otherwise Whisper picks. That is Tony's
    # forced-subtitle behaviour and must not drift — see the notes in
    # whisper_subtitles about the detector proposing rather than deciding.
    if mode == "translate_spans":
        from faster_whisper import WhisperModel
        from faster_whisper.audio import decode_audio
        SR = 16000
        spans = a.get("spans") or []
        emit("progress", message=f"decoding audio and loading "
                                 f"'{a['model_size']}' for {len(spans)} span(s)...")
        audio = decode_audio(a["audio_path"], sampling_rate=SR)
        model = WhisperModel(a["model_size"], device=a.get("device", "cuda"),
                             compute_type=a.get("compute_type", "auto"))
        for i, sp in enumerate(spans):
            try:
                chunk = audio[int(sp["start_ms"] / 1000 * SR):
                              int(sp["end_ms"] / 1000 * SR)]
                if len(chunk) < SR:          # under a second — skip, as before
                    emit("span_skipped", idx=i)
                    continue
                emit("progress", message=f"Translating {i+1}/{len(spans)}…")
                lang = sp.get("lang") if sp.get("lang") and sp.get("lang") != "?" else None
                segs, info = model.transcribe(chunk, task="translate",
                                              language=lang, vad_filter=False)
                text = " ".join(s.text.strip() for s in segs).strip()
                emit("span_done", idx=i, text=text,
                     lang=getattr(info, "language", sp.get("lang")))
            except Exception as e:
                emit("span_error", idx=i, message=str(e))
        emit("batch_done")
        return 0

    # ── smart_sync ────────────────────────────────────────────────────────────
    # ⚠️ Runs the Suite's OWN smart_sync() in here, whole. It cannot be expressed with
    # the transcribe modes above: it uses whisperx's lower-level API directly —
    # load_audio, model.transcribe, a lazily-loaded per-language alignment model, custom
    # per-sample offsets, and a fallback path when alignment fails. Proxying those
    # individually would be many round trips AND would fork logic Tony has tuned.
    #
    # Verified in a BARE venv (zero packages): modules.smart_sync imports cleanly. Its
    # transitive imports are subtitle_filters (stdlib only) and utils (stdlib + tkinter,
    # which every venv has — importing it headless is fine, only creating a root needs
    # a display).
    if mode == "smart_sync":
        from modules.smart_sync import smart_sync as _smart_sync
        try:
            res = _smart_sync(
                a["video_path"], a.get("cues") or [],
                model_size=a.get("model_size", "base"),
                language=a.get("language"),
                num_segments=a.get("num_segments", 3),
                sample_minutes=a.get("sample_minutes", 5),
                progress_callback=lambda m: emit("progress", message=str(m)[:400]),
                cancel_event=None,   # cancellation = the parent kills this process
                engine=a.get("engine", "faster-whisper"),
            )
        except Exception as e:
            emit("error", message=str(e), traceback=traceback.format_exc()[-3000:])
            return 1
        # smart_sync returns a result dict, or None when it cannot sync.
        emit("result", sync=ser(res))
        return 0

    try:
        emit("progress", message=f"loading {mode} engine...")

        if mode == "transcribe":
            result = transcribe(
                audio, a["model_size"], a.get("language"), a["device"],
                a["beam_size"], a["vad"],
                task=a.get("task", "transcribe"),
                word_timestamps=a.get("word_timestamps", False),
            )
        elif mode == "transcribe_whisperx":
            result = transcribe_whisperx(
                audio, a["model_size"], a.get("language"), a["device"],
                a["beam_size"],
                task=a.get("task", "transcribe"),
                word_timestamps=a.get("word_timestamps", False),
                batch_size=a.get("batch_size", 16),
            )
        elif mode == "transcribe_with_forced":
            result = transcribe_with_forced(
                audio, a["model_size"], a.get("language"), a["device"],
                a["beam_size"], a["vad"],
                word_timestamps=a.get("word_timestamps", False),
                native_lang=a.get("native_lang", "en"),
                progress=progress,
            )
        else:
            emit("error", message=f"unknown mode: {mode}")
            return 2

    except Exception as e:
        # ⚠️ Send the traceback back. A worker that dies silently in another interpreter
        # is far worse to debug than one that dies in-process, so it has to carry its
        # own diagnosis home.
        emit("error", message=str(e), traceback=traceback.format_exc()[-3000:])
        return 1

    # ⚠️ transcribe_with_forced returns a 3-TUPLE: (main_segs, forced, report).
    # Verified against every return statement in the function and against its caller in
    # test_forced_subs.py — NOT assumed from the name. transcribe() and
    # transcribe_whisperx() both return a plain list.
    if mode == "transcribe_with_forced":
        main_segs, forced, report = result
        emit("result", segments=ser(main_segs), forced=ser(forced), report=ser(report))
    else:
        emit("result", segments=ser(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
