"""
Docflix Media Suite — Whisper Subtitles Backend

Transcription engine for extracting subtitles from video/audio files
using faster-whisper or WhisperX.  Imported by whisper_transcriber.py.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
from datetime import timedelta
from pathlib import Path

__version__ = "2.1.0"

# ── dependency checks ────────────────────────────────────────────────────────


BACKENDS = ("faster-whisper", "whisperx")


def check_dependencies(backend: str = "faster-whisper"):
    """Verify that required packages and binaries are present."""
    missing = []

    if backend == "whisperx":
        try:
            import whisperx  # noqa: F401
        except ImportError:
            missing.append("whisperx        →  pip install whisperx")
    else:
        try:
            import faster_whisper  # noqa: F401
        except ImportError:
            missing.append("faster-whisper  →  pip install faster-whisper")

    if not shutil.which("ffmpeg"):
        missing.append("ffmpeg          →  https://ffmpeg.org/download.html")

    if missing:
        msg = "Missing dependencies:\n" + "\n".join(f"  {m}" for m in missing)
        raise RuntimeError(msg)


def is_backend_available(backend: str) -> bool:
    """Check if a backend is importable without exiting."""
    if backend == "whisperx":
        try:
            import whisperx  # noqa: F401
            return True
        except ImportError:
            return False
    else:
        try:
            import faster_whisper  # noqa: F401
            return True
        except ImportError:
            return False


# ── lightweight segment container ────────────────────────────────────────────


class SubSegment:
    """Segment container used after post-processing (offset / wrap / regroup).

    The ``word`` attribute is used when a SubSegment represents a single word
    inside another segment's ``words`` list (needed by regroup_words_into_segments).
    """
    __slots__ = ("start", "end", "text", "words", "word")

    def __init__(self, start: float, end: float, text: str, words=None, word: str | None = None):
        self.start = start
        self.end = end
        self.text = text
        self.words = words or []
        self.word = word if word is not None else text


# ── formatting helpers ───────────────────────────────────────────────────────


def _fmt_srt_time(seconds: float) -> str:
    """Format seconds as SRT timestamp  HH:MM:SS,mmm"""
    td = timedelta(seconds=seconds)
    total_ms = int(td.total_seconds() * 1000)
    h, remainder = divmod(total_ms, 3_600_000)
    m, remainder = divmod(remainder, 60_000)
    s, ms = divmod(remainder, 1_000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _fmt_vtt_time(seconds: float) -> str:
    """Format seconds as WebVTT timestamp  HH:MM:SS.mmm"""
    return _fmt_srt_time(seconds).replace(",", ".")


def segments_to_srt(segments) -> str:
    lines = []
    for i, seg in enumerate(segments, start=1):
        lines.append(str(i))
        lines.append(f"{_fmt_srt_time(seg.start)} --> {_fmt_srt_time(seg.end)}")
        lines.append(seg.text.strip())
        lines.append("")
    return "\n".join(lines)


def segments_to_vtt(segments, style: str | None = None) -> str:
    lines = ["WEBVTT", ""]
    if style and style.strip():
        lines.append("STYLE")
        lines.append("::cue {")
        for prop in style.split(";"):
            prop = prop.strip()
            if prop:
                lines.append(f"  {prop};")
        lines.append("}")
        lines.append("")
    for i, seg in enumerate(segments, start=1):
        lines.append(f"NOTE {i}")
        lines.append(f"{_fmt_vtt_time(seg.start)} --> {_fmt_vtt_time(seg.end)}")
        lines.append(seg.text.strip())
        lines.append("")
    return "\n".join(lines)


# ── post-processing helpers ──────────────────────────────────────────────────


def apply_offset(segments, offset_seconds: float):
    """Shift all timestamps by offset_seconds (positive = later, negative = earlier)."""
    if offset_seconds == 0:
        return segments
    result = []
    for seg in segments:
        new_start = max(0.0, seg.start + offset_seconds)
        new_end = max(0.0, seg.end + offset_seconds)
        if new_end > 0:
            result.append(SubSegment(start=new_start, end=new_end, text=seg.text.strip()))
    return result


def apply_line_wrap(segments, max_width: int, max_lines: int = 2):
    """Wrap subtitle text to max_width characters per line, max_lines per cue.

    If a segment's text exceeds max_lines after wrapping, it is split into
    multiple cues (each up to max_lines lines) with the timing distributed
    proportionally by character count.
    """
    if max_width <= 0:
        return segments
    result = []
    for seg in segments:
        lines = textwrap.wrap(seg.text.strip(), width=max_width)
        if not lines:
            result.append(SubSegment(start=seg.start, end=seg.end, text=""))
            continue
        if len(lines) <= max_lines:
            result.append(SubSegment(start=seg.start, end=seg.end,
                                     text="\n".join(lines)))
        else:
            # Split into chunks of max_lines and distribute timing
            total_chars = sum(len(line) for line in lines)
            duration = seg.end - seg.start
            pos = seg.start
            for i in range(0, len(lines), max_lines):
                chunk = lines[i:i + max_lines]
                chunk_chars = sum(len(line) for line in chunk)
                chunk_dur = duration * (chunk_chars / total_chars) if total_chars else 0
                chunk_end = min(pos + chunk_dur, seg.end)
                result.append(SubSegment(start=pos, end=chunk_end,
                                         text="\n".join(chunk)))
                pos = chunk_end
    return result


def trim_lead_time(segments, max_lead: float = 0.5):
    """Trim subtitle start times that begin too far before the actual speech.

    When word-level timestamps are available, each segment's start is snapped
    to ``first_word.start - buffer`` (buffer = max_lead * 0.5, min 0.15 s) if it
    currently begins more than *max_lead* seconds before the first word.

    Without word timestamps a heuristic is used: if the gap between the
    previous segment's end and the current segment's start exceeds
    *max_lead*, the start is pulled forward to ``previous_end + buffer``.
    The very first segment is left untouched in the no-word-timestamps path
    since there is no reference point.

    Negative timestamps are clamped to 0.
    """
    if max_lead <= 0:
        return segments

    buffer = max(0.15, max_lead * 0.5)
    result = []
    prev_end = 0.0

    for seg in segments:
        words = getattr(seg, "words", None)

        if words:
            # Word-level path — find the first word with a valid start time
            first_word_start = None
            for w in words:
                ws = getattr(w, "start", None)
                if ws is not None:
                    first_word_start = ws
                    break

            if first_word_start is not None and (first_word_start - seg.start) > max_lead:
                new_start = max(0.0, first_word_start - buffer)
                result.append(SubSegment(
                    start=new_start, end=seg.end,
                    text=seg.text if isinstance(seg.text, str) else seg.text,
                    words=words,
                ))
                prev_end = seg.end
                continue

        elif result:
            # No word data — heuristic: tighten if gap before this segment is too large
            gap = seg.start - prev_end
            if gap > max_lead:
                new_start = max(0.0, prev_end + buffer)
                # Don't push start past the segment's own end
                new_start = min(new_start, seg.end - 0.1)
                result.append(SubSegment(
                    start=max(0.0, new_start), end=seg.end,
                    text=seg.text if isinstance(seg.text, str) else seg.text,
                ))
                prev_end = seg.end
                continue

        # No adjustment needed — keep as-is
        result.append(seg)
        prev_end = seg.end

    return result


def regroup_words_into_segments(segments, max_chars: int = 42):
    """When word-level timestamps are available, create tighter sub-segments.

    Each sub-segment is at most *max_chars* characters, split on word
    boundaries using the per-word timing provided by faster-whisper.
    """
    new_segments = []
    for seg in segments:
        words = getattr(seg, "words", None)
        if not words:
            # No word-level data — keep segment as-is
            new_segments.append(seg)
            continue

        current_words = []
        current_text = ""
        start_time = None

        for word in words:
            word_text = word.word.strip()
            if not word_text:
                continue

            test_text = (current_text + " " + word_text).strip() if current_text else word_text

            if len(test_text) > max_chars and current_words:
                new_segments.append(SubSegment(
                    start=start_time,
                    end=current_words[-1].end,
                    text=current_text,
                ))
                current_words = [word]
                current_text = word_text
                start_time = word.start
            else:
                if not current_words:
                    start_time = word.start
                current_words.append(word)
                current_text = test_text

        if current_words:
            new_segments.append(SubSegment(
                start=start_time,
                end=current_words[-1].end,
                text=current_text,
            ))

    return new_segments


def post_process_segments(segments, *, word_timestamps: bool = False,
                          max_line_length: int = 0, offset: float = 0.0,
                          max_chars_per_group: int = 42,
                          max_lead: float = 0.0):
    """Apply all post-processing steps to a list of segments."""
    result = segments
    if max_lead > 0:
        result = trim_lead_time(result, max_lead=max_lead)
    if word_timestamps:
        result = regroup_words_into_segments(result, max_chars=max_chars_per_group)
    if max_line_length > 0:
        result = apply_line_wrap(result, max_width=max_line_length)
    if offset != 0:
        result = apply_offset(result, offset)
    return result


# ── file discovery ───────────────────────────────────────────────────────────

VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv",
    ".webm", ".m4v", ".ts", ".mpg", ".mpeg",
}

AUDIO_EXTENSIONS = {
    ".mp3", ".wav", ".flac", ".aac", ".ogg",
    ".m4a", ".opus", ".wma",
}


def find_media_files(directory: Path) -> list[Path]:
    """Recursively find all video and audio files in a directory."""
    all_exts = VIDEO_EXTENSIONS | AUDIO_EXTENSIONS
    found = []
    for root, _dirs, files in os.walk(directory):
        for fname in sorted(files):
            if Path(fname).suffix.lower() in all_exts:
                found.append(Path(root) / fname)
    return found


def subtitle_exists(input_path: Path, output_dir: str | None,
                    formats: list[str]) -> bool:
    """Return True if a subtitle file already exists for *input_path*."""
    for fmt in formats:
        ext = f".{fmt}"
        if output_dir:
            check = Path(output_dir) / (input_path.stem + ext)
        else:
            check = input_path.with_suffix(ext)
        if check.exists():
            return True
    return False


# ── audio extraction ─────────────────────────────────────────────────────────


def extract_audio(input_path: Path, tmp_dir: str) -> Path:
    """
    If the input is a video file, extract a mono 16 kHz WAV with ffmpeg.
    If it's already audio, return as-is (whisper handles most audio formats).
    """
    suffix = input_path.suffix.lower()
    if suffix in AUDIO_EXTENSIONS:
        return input_path

    if suffix not in VIDEO_EXTENSIONS:
        # Unknown extension – try treating it as video anyway
        print(f"⚠️  Unknown file extension '{suffix}', attempting to extract audio…")

    out_audio = Path(tmp_dir) / "audio.wav"
    print("🎬  Extracting audio from video…")
    cmd = [
        "ffmpeg", "-y",
        "-i", str(input_path),
        "-vn",                  # drop video
        "-acodec", "pcm_s16le", # 16-bit PCM
        "-ar", "16000",         # 16 kHz (Whisper native)
        "-ac", "1",             # mono
        str(out_audio),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed:\n{result.stderr[-2000:]}")

    print(f"✅  Audio extracted → {out_audio.name}\n")
    return out_audio


# ── transcription ────────────────────────────────────────────────────────────


def transcribe(
    audio_path: Path,
    model_size: str,
    language: str | None,
    device: str,
    beam_size: int,
    vad: bool,
    task: str = "transcribe",
    word_timestamps: bool = False,
) -> list:
    """Run faster-whisper and return a list of Segment objects."""
    from faster_whisper import WhisperModel

    print(f"🤖  Loading model  : {model_size}  (device={device})")
    model = WhisperModel(model_size, device=device, compute_type="auto")

    kwargs = dict(
        beam_size=beam_size,
        language=language,            # None = auto-detect
        vad_filter=vad,
        vad_parameters=dict(min_silence_duration_ms=500),
        task=task,
        word_timestamps=word_timestamps,
    )

    task_label = "Translating → English" if task == "translate" else "Transcribing"
    print(f"🔊  {task_label}…   (this may take a while for long files)\n")

    segments, info = model.transcribe(str(audio_path), **kwargs)

    detected = info.language
    confidence = info.language_probability
    duration = info.duration
    print(f"   Detected language : {detected}  (confidence {confidence:.0%})")
    print(f"   Audio duration    : {timedelta(seconds=int(duration))}\n")

    # Materialise the lazy generator while showing progress
    try:
        from tqdm import tqdm
        collected = []
        with tqdm(
            total=duration,
            unit="s",
            unit_scale=True,
            desc="   Progress",
            bar_format="{l_bar}{bar}| {n:.0f}/{total:.0f}s [{elapsed}<{remaining}]",
        ) as pbar:
            prev = 0.0
            for seg in segments:
                collected.append(seg)
                pbar.update(seg.end - prev)
                prev = seg.end
    except ImportError:
        collected = list(segments)

    print(f"\n✅  {len(collected)} subtitle segments generated.")
    return collected


def transcribe_whisperx(
    audio_path: Path,
    model_size: str,
    language: str | None,
    device: str,
    beam_size: int,
    task: str = "transcribe",
    word_timestamps: bool = False,
    batch_size: int = 16,
) -> list:
    """Run WhisperX and return a list of SubSegment objects.

    WhisperX provides better word-level alignment via wav2vec2 forced
    phoneme alignment.  Speaker diarization is intentionally not used.
    """
    import whisperx

    compute_type = "float16" if device == "cuda" else "int8"
    print(f"🤖  Loading WhisperX model  : {model_size}  (device={device}, compute={compute_type})")
    model = whisperx.load_model(
        model_size,
        device,
        compute_type=compute_type,
        language=language,
        task=task,
    )

    task_label = "Translating → English" if task == "translate" else "Transcribing"
    print(f"🔊  {task_label} with WhisperX…   (this may take a while for long files)\n")

    audio = whisperx.load_audio(str(audio_path))
    result = model.transcribe(audio, batch_size=batch_size, language=language)

    detected_lang = result.get("language", language or "unknown")
    print(f"   Detected language : {detected_lang}")
    print(f"   Segments (pre-align): {len(result.get('segments', []))}")

    # ── forced alignment for precise word timestamps ────────────────────────
    if word_timestamps and result.get("segments"):
        align_lang = detected_lang
        print(f"   🔧  Loading alignment model for '{align_lang}'…")
        try:
            model_a, metadata = whisperx.load_align_model(
                language_code=align_lang, device=device,
            )
            result = whisperx.align(
                result["segments"], model_a, metadata, audio, device,
                return_char_alignments=False,
            )
            print("   ✅  Forced alignment complete.")
        except Exception as exc:
            print(f"   ⚠️  Alignment failed ({exc}), using unaligned timestamps.")

    # ── convert WhisperX dicts → SubSegment objects ─────────────────────────
    segments = []
    for seg_dict in result.get("segments", []):
        start = seg_dict.get("start", 0.0)
        end = seg_dict.get("end", 0.0)
        text = seg_dict.get("text", "").strip()
        if not text:
            continue

        words = []
        if word_timestamps and "words" in seg_dict:
            for w in seg_dict["words"]:
                words.append(SubSegment(
                    start=w.get("start", start),
                    end=w.get("end", end),
                    text=w.get("word", "").strip(),
                ))

        segments.append(SubSegment(start=start, end=end, text=text, words=words))

    print(f"\n✅  {len(segments)} subtitle segments generated (WhisperX).")
    return segments


# ── output writing ───────────────────────────────────────────────────────────


def write_output(segments, input_path: Path, output: str | None, fmt: str,
                 vtt_style: str | None = None):
    formats = [f.strip().lower() for f in fmt.split(",")]

    for f in formats:
        if f == "srt":
            text = segments_to_srt(segments)
            ext = ".srt"
        elif f == "vtt":
            text = segments_to_vtt(segments, style=vtt_style)
            ext = ".vtt"
        else:
            print(f"⚠️  Unknown format '{f}', skipping.")
            continue

        if output:
            out_path = Path(output)
            # If user gave a directory, auto-name the file
            if out_path.is_dir():
                out_path = out_path / (input_path.stem + ext)
            elif not out_path.suffix:
                out_path = out_path.with_suffix(ext)
        else:
            out_path = input_path.with_suffix(ext)

        out_path.write_text(text, encoding="utf-8")
        print(f"💾  Saved {f.upper():<4} → {out_path}")

