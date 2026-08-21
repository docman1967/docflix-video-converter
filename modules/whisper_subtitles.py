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

__version__ = "2.3.0"

# ── dependency checks ────────────────────────────────────────────────────────


BACKENDS = ("faster-whisper", "whisperx")


def check_dependencies(backend: str = "faster-whisper"):
    """Verify that required packages and binaries are present."""
    missing = []

    # ⚠️ Ask the engine, not this interpreter — see is_backend_available(). And never
    # tell the user to pip-install into their own Python; the Suite offers to build the
    # isolated engine with a full disclosure instead.
    if not is_backend_available(backend):
        missing.append("Whisper engine  →  install it from the Transcriber "
                       "(runs in its own isolated environment)")

    if not shutil.which("ffmpeg"):
        missing.append("ffmpeg          →  https://ffmpeg.org/download.html")

    if missing:
        msg = "Missing dependencies:\n" + "\n".join(f"  {m}" for m in missing)
        raise RuntimeError(msg)


def is_backend_available(backend: str) -> bool:
    """Is this backend usable?

    ⚠️ ASKS THE ENGINE, NOT THIS INTERPRETER. Both backends live in the isolated
    whisper venv, so `import whisperx` in the Suite's own process is the wrong
    question — after isolation a user will NOT have whisperx in their system Python,
    and the old check would have reported "not installed" to everyone with a perfectly
    good engine built. It only appeared to work on the dev machine because a leftover
    system install happened to still be there.

    ⚠️ This module is ALSO imported by whisper_worker.py inside the venv, where the
    packages genuinely are importable — hence the fallback. Do not remove it.
    """
    try:
        from . import whisper_engine
        return whisper_engine.is_installed()
    except ImportError:
        pass
    # Running inside the engine venv (the worker), where a direct import is correct.
    mod = "whisperx" if backend == "whisperx" else "faster_whisper"
    try:
        __import__(mod)
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


# ── broadcast-style cue segmentation ─────────────────────────────────────────

# Function words that should not be left dangling at the end of a subtitle
# line — the eye expects them to lead into the next word (subtitling convention).
_NO_BREAK_AFTER = frozenset({
    "a", "an", "the", "and", "but", "or", "nor", "for", "so", "yet",
    "to", "of", "in", "on", "at", "by", "with", "from", "as", "if",
    "into", "onto", "upon", "per", "via", "than", "that", "this", "these",
    "those", "my", "your", "his", "her", "its", "our", "their",
    "is", "are", "was", "were", "be", "been", "being", "am",
    "will", "would", "can", "could", "should", "shall", "may", "might",
    "must", "do", "does", "did", "not", "no",
})

_SENTENCE_END = ("." , "!", "?", "…")
_CLAUSE_END = (",", ";", ":", "—", "–")


def _ends_sentence(token: str) -> bool:
    """True if a word token ends a sentence (ignoring trailing quotes/brackets)."""
    t = token.rstrip('"\'’”)]}»').rstrip()
    return t.endswith(_SENTENCE_END)


def balance_lines(text: str, max_len: int = 42, max_lines: int = 2) -> str:
    """Wrap a cue's text into at most *max_lines* visually balanced lines.

    Prefers to break after punctuation and avoids leaving a short function
    word (article/preposition/conjunction) stranded at the end of a line —
    the same conventions professional subtitles follow.
    """
    text = " ".join(text.split())
    if len(text) <= max_len:
        return text

    words = text.split(" ")
    best = None  # (score, line1, line2)
    for k in range(1, len(words)):
        l1 = " ".join(words[:k])
        l2 = " ".join(words[k:])
        if len(l1) > max_len or len(l2) > max_len:
            continue
        score = abs(len(l1) - len(l2))          # favour balance
        if _ends_sentence(l1):
            score -= 12                          # great place to break
        elif l1[-1:] in _CLAUSE_END:
            score -= 6                           # good place to break
        last = words[k - 1].strip(".,!?;:—–\"'").lower()
        if last in _NO_BREAK_AFTER:
            score += 10                          # don't strand a function word
        first_next = words[k].strip(".,!?;:—–\"'").lower()
        if first_next in _NO_BREAK_AFTER:
            score -= 3                            # nice: function word leads line 2
        if best is None or score < best[0]:
            best = (score, l1, l2)

    if best is None:
        # No single split fits within max_len — fall back to a greedy wrap.
        wrapped = textwrap.wrap(text, width=max_len)
        return "\n".join(wrapped[:max_lines])
    return best[1] + "\n" + best[2]


def _pseudo_words(seg):
    """Split a wordless segment's text into evenly-timed pseudo-words.

    Used when word-level timestamps are unavailable, so the same cue-building
    logic (punctuation, length, reading-speed) still applies. Timing within
    the segment is interpolated by character length; real pauses only exist at
    the segment boundaries, which are preserved.
    """
    txt = (getattr(seg, "text", "") or "").strip()
    toks = txt.split()
    if not toks:
        return []
    start = float(getattr(seg, "start", 0.0) or 0.0)
    end = float(getattr(seg, "end", start) or start)
    dur = max(0.01, end - start)
    total = sum(len(t) for t in toks) or len(toks)
    out = []
    t0 = start
    for tk in toks:
        share = len(tk) / total if total else 1.0 / len(toks)
        t1 = min(end, t0 + dur * share)
        out.append((t0, t1, tk))
        t0 = t1
    return out


def _word_stream(segments):
    """Flatten segments into a (start, end, token) stream, using real word
    timings where present and interpolated pseudo-words otherwise."""
    stream = []
    for seg in segments:
        words = getattr(seg, "words", None)
        if words:
            for w in words:
                tok = (getattr(w, "word", None) or getattr(w, "text", "") or "").strip()
                if not tok:
                    continue
                ws = getattr(w, "start", None)
                we = getattr(w, "end", None)
                ws = float(seg.start if ws is None else ws)
                we = float(seg.end if we is None else we)
                if we < ws:
                    we = ws
                stream.append((ws, we, tok))
        else:
            stream.extend(_pseudo_words(seg))
    return stream


def segment_into_cues(segments, *, max_line_length: int = 42, max_lines: int = 2,
                      reading_speed: float = 17.0, split_gap: float = 0.5,
                      min_duration: float = 0.8, max_duration: float = 7.0):
    """Re-cut transcript segments into broadcast-style subtitle cues.

    A new cue is started when any of these happen (checked before each word):
      • the previous word ends a sentence (. ! ? …)
      • a pause of >= *split_gap* seconds precedes the next word (natural break —
        lines up with scene cuts and speaker hand-offs)
      • adding the word would exceed the character budget (max_lines × width)
      • the cue would exceed *max_duration* seconds

    Afterwards each cue's duration is stretched (without overlapping the next)
    so it never reads faster than *reading_speed* chars/sec and never flashes
    shorter than *min_duration*. Text is wrapped into balanced lines.
    """
    max_chars = max(1, max_line_length) * max_lines
    stream = _word_stream(segments)
    if not stream:
        return []

    cues = []          # list of [start, end, [tokens]]
    for ws, we, tok in stream:
        if not cues:
            cues.append([ws, we, [tok]])
            continue

        cur = cues[-1]
        prev_end = cur[1]
        cur_text = " ".join(cur[2])
        gap = ws - prev_end
        would_len = len(cur_text) + 1 + len(tok)

        force_break = (
            _ends_sentence(cur[2][-1])
            or gap >= split_gap
            or would_len > max_chars
            or (we - cur[0]) > max_duration
        )
        if force_break:
            cues.append([ws, we, [tok]])
        else:
            cur[1] = we
            cur[2].append(tok)

    # Build SubSegments with wrapped text, then polish timing for readability.
    result = []
    n = len(cues)
    for i, (start, end, toks) in enumerate(cues):
        text = balance_lines(" ".join(toks), max_len=max_line_length,
                             max_lines=max_lines)
        chars = len(text.replace("\n", " "))
        dur = end - start
        need = max(min_duration, chars / reading_speed if reading_speed > 0 else 0)
        if dur < need:
            new_end = start + min(need, max_duration)
            if i + 1 < n:
                new_end = min(new_end, cues[i + 1][0] - 0.04)  # ~1 frame gap
            if new_end > end:
                end = new_end
        if end <= start:
            end = start + 0.04
        result.append(SubSegment(start=start, end=end, text=text))
    return result


def post_process_segments(segments, *, word_timestamps: bool = False,
                          max_line_length: int = 0, offset: float = 0.0,
                          max_chars_per_group: int = 42,
                          max_lead: float = 0.0,
                          reading_speed: float = 17.0, split_gap: float = 0.5,
                          min_duration: float = 0.8, max_duration: float = 7.0):
    """Apply all post-processing steps to a list of segments."""
    result = segments
    if max_lead > 0:
        result = trim_lead_time(result, max_lead=max_lead)
    if word_timestamps:
        # Broadcast-style re-segmentation (pauses + punctuation + reading speed)
        result = segment_into_cues(
            result,
            max_line_length=max_line_length or 42,
            reading_speed=reading_speed,
            split_gap=split_gap,
            min_duration=min_duration,
            max_duration=max_duration,
        )
    elif max_line_length > 0:
        # No word timing — best we can do is balance long segments' lines.
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


# ── forced-subtitle detection (mixed-language media) ─────────────────────────
#
# The problem: Whisper detects ONE language for a whole file. A documentary that's
# 95% English with a 40-second Spanish interview gives you two bad options —
# transcribe mode mangles the Spanish, or translate mode round-trips the English
# through a translator. Neither produces a FORCED track, which is what that film
# actually needs: a sub shown by default that covers only the foreign dialogue.
#
# The approach: transcribe normally, then find where the model was GUESSING.
# Foreign speech decoded as English scores a poor avg_logprob, so that's a cheap
# first-pass filter — only those windows pay for a real language-detection call.
# Confirmed foreign spans get re-run with task="translate" and are emitted twice:
# merged into the main track, and alone as the forced track.
#
# KNOWN LIMIT — audio only. A real forced track also translates ON-SCREEN text
# (signs, chyrons, location cards). Whisper can't see, so this covers the
# foreign-DIALOGUE half. For documentaries that's most of the value, but it is
# not the whole job and shouldn't be described as if it were.
#     -- Arthur & Tony, 2026-08-03

FORCED_SUSPECT_LOGPROB = -0.75   # below this the decoder was guessing -> worth checking
FORCED_MIN_SPAN = 2.5            # seconds; shorter than this and detection is a coin flip
FORCED_LANG_CONFIDENCE = 0.60    # detect_language probability floor
FORCED_MERGE_GAP = 3.0           # stitch spans separated by less than this
FORCED_MAX_COVERAGE = 0.60       # beyond this it's a foreign FILM, not forced subs
FORCED_MIN_GAP = 8.0             # a transcription hole this long is worth investigating
FORCED_SCAN_WIDTH = 10.0         # detector window — wide enough to ID, narrow enough not to mix
FORCED_SCAN_HOP = 4.0            # slide step; overlapping so a clip can't fall between windows
FORCED_SCAN_PAD = 4.0            # look this far outside a candidate (speech starts before the cue)
FORCED_NATIVE_MAX = 0.50         # foreign when confidence in the NATIVE language drops below this


def _suspect_windows(segments, *, logprob_threshold=FORCED_SUSPECT_LOGPROB,
                     merge_gap=FORCED_MERGE_GAP, min_span=FORCED_MIN_SPAN):
    """Contiguous runs of low-confidence segments, merged into candidate windows.

    Cheap pre-filter: running detect_language on every segment of a 50-minute
    documentary is wasteful, and avg_logprob already tells us where the decoder
    struggled. Missing a window here only costs recall, never correctness.
    """
    runs, cur = [], None
    for seg in segments:
        lp = getattr(seg, "avg_logprob", 0.0)
        if lp is not None and lp < logprob_threshold:
            if cur and seg.start - cur[1] <= merge_gap:
                cur[1] = seg.end
            else:
                if cur:
                    runs.append(cur)
                cur = [seg.start, seg.end]
        elif cur and seg.start - cur[1] > merge_gap:
            runs.append(cur)
            cur = None
    if cur:
        runs.append(cur)
    return [(a, b) for a, b in runs if (b - a) >= min_span]


def _gap_windows(segments, audio, *, sample_rate=16000, min_gap=FORCED_MIN_GAP,
                 min_span=FORCED_MIN_SPAN):
    """Transcription HOLES that actually contain speech.

    This is the failure mode that matters most, and the one the logprob filter is
    structurally blind to. Whisper doesn't always mangle foreign speech into bad
    English — often it emits NOTHING for it. No segment means no avg_logprob means
    nothing to be suspicious of, so the foreign dialogue is invisible to a filter
    that only looks at what was transcribed.

    Found 2026-08-03 in Tony's own WhisperX output on "Dogs: The Untold Story":
    E03 had SEVENTEEN gaps of 15s+ in a continuously-narrated documentary. He'd
    described it as "foreign languages that didn't get picked up" — literally true.

    Silero VAD separates "nobody is talking" (music bed, wind, b-roll) from
    "someone is talking and we transcribed none of it". Only the latter is a lead.
    """
    try:
        from faster_whisper.vad import get_speech_timestamps, VadOptions
    except ImportError:
        return []

    covered = sorted((s.start, s.end) for s in segments)
    total = len(audio) / sample_rate
    holes, cursor = [], 0.0
    for start, end in covered:
        if start - cursor >= min_gap:
            holes.append((cursor, start))
        cursor = max(cursor, end)
    if total - cursor >= min_gap:
        holes.append((cursor, total))

    out = []
    for start, end in holes:
        chunk = audio[int(start * sample_rate):int(end * sample_rate)]
        if len(chunk) < sample_rate:
            continue
        try:
            speech = get_speech_timestamps(
                chunk, VadOptions(min_speech_duration_ms=int(min_span * 1000)))
        except Exception:
            continue
        for sp in speech:
            a = start + sp["start"] / sample_rate
            b = start + sp["end"] / sample_rate
            if b - a >= min_span:
                out.append((a, b))
    return out


def _stretched_windows(segments, *, min_span=FORCED_MIN_SPAN, min_cps=5.0):
    """Audio INSIDE a cue that produced no words — the sneakiest failure mode.

    Tony's observation, 2026-08-03: *"whisperx gets confused. If there are foreign
    languages, it tends to attribute that to the next English part — the subtitle
    for the English part just after gets stretched to include the foreign language."*

    This one leaves no evidence for the other two detectors. There's no HOLE (a cue
    covers the span) and no bad avg_logprob (the English text is correct). Only the
    TIMING is wrong, and wrong timing looks like nothing at all.

    Word timestamps expose it: a cue starting at 400s whose first word lands at 418s
    has 18 seconds inside it that produced no words. That's the foreign speech.

    Without word timestamps, fall back to characters-per-second — normal speech runs
    ~12-20 cps, so a cue under `min_cps` is covering audio it never transcribed.
    """
    out = []
    for seg in segments:
        words = [w for w in (getattr(seg, "words", None) or [])
                 if getattr(w, "start", None) is not None]
        if words:
            # Silence before the first word, and any wide gap between words.
            edges = [(seg.start, words[0].start)]
            for a, b in zip(words, words[1:]):
                edges.append((a.end, b.start))
            for a, b in edges:
                if b - a >= min_span:
                    out.append((a, b))
        else:
            text = (getattr(seg, "text", "") or "").strip()
            dur = seg.end - seg.start
            if dur >= min_span and text and (len(text) / dur) < min_cps:
                # Can't localise it without words; hand over the whole cue.
                out.append((seg.start, seg.end))
    return out


def detect_foreign_spans(model, audio, segments, *, native_lang="en",
                         sample_rate=16000, confidence=FORCED_LANG_CONFIDENCE,
                         progress=None, full_sweep=True):
    """Return [(start, end, lang, prob), ...] for spans NOT in `native_lang`.

    `audio` is the decoded float32 mono array (faster_whisper.audio.decode_audio).

    Two candidate sources, because foreign speech fails in two different ways:
      1. transcribed as nonsense English -> poor avg_logprob  (_suspect_windows)
      2. not transcribed at all          -> a speech-bearing hole (_gap_windows)
    """
    total = len(audio) / sample_rate
    if full_sweep:
        # SWEEP THE WHOLE FILE. No candidate windows at all.
        #
        # We used to derive candidates from three heuristics (bad avg_logprob,
        # transcription holes, cues containing no words). All three depend on
        # Whisper's SEGMENTATION, which is not stable across runs or settings —
        # and on 2026-08-03 that cost us a real miss: ten seconds of Shona at 6:33
        # in "Dogs: The Untold Story" E01. A probe found a clean 9.6s hole there,
        # but in the full run (word timestamps + VAD) a segment covered it, so no
        # candidate window was generated and 393s was NEVER EXAMINED.
        #
        # That's a whole class of invisible failure, and it existed only to avoid
        # a cost nobody measured: ~660 detect_language calls for a 44-minute file,
        # against a transcribe that already takes minutes. If we never choose where
        # to look, we can never fail to look somewhere.
        padded = [(0.0, total)]
    else:
        windows = sorted(set(_suspect_windows(segments))
                         | set(_gap_windows(segments, audio, sample_rate=sample_rate))
                         | set(_stretched_windows(segments)))
        padded = []
        for start, end in windows:
            a, b = max(0.0, start - FORCED_SCAN_PAD), min(total, end + FORCED_SCAN_PAD)
            if padded and a <= padded[-1][1]:
                padded[-1] = (padded[-1][0], max(padded[-1][1], b))
            else:
                padded.append((a, b))

    hits = []
    scanned = 0
    for i, (start, end) in enumerate(padded):
        # SLIDE a short detector rather than asking once about the whole span.
        # Measured 2026-08-03 on "Dogs: The Untold Story" E01 @ 6:33 — Shona speech
        # surrounded by English narration:
        #     393-403s -> sn 39%     <- the truth, only at this exact cut
        #     393-413s -> en 98%     <- ten seconds wider and it's gone
        #     380-410s -> en 100%
        # One question about a contaminated window always returns the majority
        # language. A short window can't be outvoted by narration it doesn't contain.
        t = start
        while t < end - 1.0:
            w_end = min(t + FORCED_SCAN_WIDTH, end)
            chunk = audio[int(t * sample_rate):int(w_end * sample_rate)]
            if len(chunk) >= sample_rate:
                try:
                    lang, prob, all_probs = model.detect_language(audio=chunk)
                except Exception:
                    lang, prob, all_probs = native_lang, 0.0, []
                # Don't use an absolute floor on the WINNER — Whisper's language ID is
                # far less certain on low-resource languages (Shona topped out at 39%,
                # under the old 0.60 floor, and was still correct). Ask the better
                # question: has confidence in the NATIVE language collapsed?
                p_native = dict(all_probs or []).get(native_lang, 1.0 if lang == native_lang else 0.0)
                if lang != native_lang and p_native < FORCED_NATIVE_MAX:
                    hits.append((t, w_end, lang, prob))
            scanned += 1
            if progress and scanned % 50 == 0:
                progress(f"  scanned {scanned} windows ({t:.0f}s/{total:.0f}s)…")
            t += FORCED_SCAN_HOP

    # Stitch overlapping/adjacent hits of the same language back into spans.
    spans = []
    for start, end, lang, prob in sorted(hits):
        if spans and spans[-1][2] == lang and start - spans[-1][1] <= FORCED_SCAN_WIDTH:
            prev = spans[-1]
            spans[-1] = (prev[0], max(prev[1], end), lang, max(prev[3], prob))
        else:
            spans.append((start, end, lang, prob))
    return spans


def _merge_spans(spans, gap=FORCED_MERGE_GAP):
    """Stitch adjacent same-language spans separated by a short gap."""
    out = []
    for start, end, lang, prob in sorted(spans):
        if out and out[-1][2] == lang and start - out[-1][1] <= gap:
            prev = out[-1]
            out[-1] = (prev[0], max(prev[1], end), lang, max(prev[3], prob))
        else:
            out.append((start, end, lang, prob))
    return out


def transcribe_with_forced(
    audio_path: Path,
    model_size: str,
    language: str | None,
    device: str,
    beam_size: int,
    vad: bool,
    word_timestamps: bool = False,
    native_lang: str = "en",
    progress=None,
):
    """Transcribe, then find + translate foreign-language spans.

    Returns (main_segments, forced_segments, report). `main_segments` is the full
    track with foreign spans replaced by their translations; `forced_segments` is
    just those translations, suitable for a track flagged `forced`.

    The model is loaded ONCE and reused for detection and the translate pass —
    reloading per span would dominate the runtime.
    """
    from faster_whisper import WhisperModel
    from faster_whisper.audio import decode_audio

    def say(msg):
        if progress:
            progress(msg)
        else:
            print(msg)

    say(f"Loading model {model_size} (device={device})…")
    model = WhisperModel(model_size, device=device, compute_type="auto")

    segments, info = model.transcribe(
        str(audio_path),
        beam_size=beam_size,
        language=language or native_lang,
        vad_filter=vad,
        vad_parameters=dict(min_silence_duration_ms=500),
        task="transcribe",
        # Word timestamps are NOT optional here: the "stretched cue" failure mode
        # is only visible as audio inside a cue that produced no words.
        word_timestamps=True,
    )
    main = list(segments)
    duration = info.duration or 0.0
    say(f"Transcribed {len(main)} segments ({duration:.0f}s).")

    report = {
        "duration": duration,
        "segments": len(main),
        "windows_checked": 0,
        "spans": [],
        "languages": [],
        "coverage": 0.0,
        "by_source": {},
        "skipped_reason": None,
    }
    if not main:
        return main, [], report

    audio = decode_audio(str(audio_path), sampling_rate=16000)
    # Break the candidates out by source — which detector earns its keep is the
    # whole tuning question, and on real documentary audio the answer surprised us
    # (see the module notes: the logprob filter found literally nothing).
    w_suspect = set(_suspect_windows(main))
    w_gap = set(_gap_windows(main, audio))
    w_stretch = set(_stretched_windows(main))
    windows = sorted(w_suspect | w_gap | w_stretch)
    report["windows_checked"] = len(windows)
    report["by_source"] = {"logprob": len(w_suspect), "gap": len(w_gap),
                           "stretched": len(w_stretch)}
    say("candidate windows: %d total (logprob=%d, gaps=%d, stretched=%d)"
        % (len(windows), len(w_suspect), len(w_gap), len(w_stretch)))

    spans = _merge_spans(detect_foreign_spans(
        model, audio, main, native_lang=native_lang, progress=progress))
    spans = [s for s in spans if (s[1] - s[0]) >= FORCED_MIN_SPAN]
    if not spans:
        say("No foreign-language spans found — no forced track needed.")
        return main, [], report

    covered = sum(e - s for s, e, _, _ in spans)
    report["coverage"] = covered / duration if duration else 0.0
    # A file that's mostly non-native isn't an English doc with foreign clips —
    # it's a foreign film, and a "forced" track covering 80% of it is nonsense.
    if report["coverage"] > FORCED_MAX_COVERAGE:
        report["skipped_reason"] = (
            "%.0f%% of the audio is non-%s — this looks like a foreign-language "
            "film, not forced subtitles. Transcribe it with language=%s instead."
            % (report["coverage"] * 100, native_lang, spans[0][2])
        )
        say(report["skipped_reason"])
        return main, [], report

    forced = []
    for i, (start, end, lang, prob) in enumerate(spans):
        say(f"Translating {lang} span {i + 1}/{len(spans)} "
            f"({start:.0f}s–{end:.0f}s, {prob:.0%} confident)…")
        chunk = audio[int(start * 16000):int(end * 16000)]
        try:
            segs, _ = model.transcribe(
                chunk, beam_size=beam_size, task="translate",
                language=lang, vad_filter=False,
                word_timestamps=word_timestamps,
            )
        except Exception as exc:
            say(f"  ! translate failed for {start:.0f}s–{end:.0f}s: {exc}")
            continue
        for s in segs:
            # Chunk-relative -> absolute. Slicing the audio ourselves (rather than
            # using clip_timestamps) keeps this offset explicit and predictable.
            forced.append(SubSegment(s.start + start, s.end + start, s.text.strip()))
        report["spans"].append({"start": start, "end": end,
                                "lang": lang, "prob": prob})

    report["languages"] = sorted({s["lang"] for s in report["spans"]})

    # Main track = everything, with the foreign stretches replaced by translations.
    kept = [s for s in main
            if not any(s.start < e and s.end > st for st, e, _, _ in spans)]
    merged = sorted(
        [SubSegment(s.start, s.end, s.text.strip()) for s in kept] + forced,
        key=lambda s: s.start,
    )
    say(f"Found {len(spans)} foreign span(s): {', '.join(report['languages'])} "
        f"→ {len(forced)} forced cue(s).")
    return merged, forced, report


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
    import torch
    import whisperx

    # ⚠️ PyTorch 2.6 flipped torch.load's `weights_only` default False → True.
    # WhisperX's bundled VAD (whisperx/assets/pytorch_model.bin) is a pytorch-
    # lightning checkpoint — it pickles whole objects, not a bare state_dict —
    # so the new safe-unpickler refuses it and EVERY model size fails
    # identically at load time ("Unsupported global: omegaconf...ListConfig").
    # That identical-failure-across-models is the tell: it's the VAD, not the ASR.
    #
    # add_safe_globals() is not the fix. Allowlisting ListConfig just surfaces
    # TorchVersion behind it, and another behind that — a lightning checkpoint
    # has an open-ended set of globals. Verified 2026-08-18 on torch 2.8.0+cu128.
    #
    # So: scope weights_only=False to this load only, and always restore. The
    # checkpoint ships inside the whisperx package we installed — same trust
    # level as the code executing this line.
    _orig_load = torch.load

    def _load_trusted(*a, **kw):
        kw["weights_only"] = False
        return _orig_load(*a, **kw)

    compute_type = "float16" if device == "cuda" else "int8"
    print(f"🤖  Loading WhisperX model  : {model_size}  (device={device}, compute={compute_type})")
    torch.load = _load_trusted
    try:
        model = whisperx.load_model(
            model_size,
            device,
            compute_type=compute_type,
            language=language,
            task=task,
        )
    finally:
        torch.load = _orig_load    # restore even if the load raises

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

