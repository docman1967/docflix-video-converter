"""
Docflix Media Suite — Smart Sync (Whisper-based)

Auto-sync subtitles to video audio using faster-whisper
(Standard engine) or WhisperX (Precise engine) speech
recognition. Supports Quick Scan, Full Scan, and Direct
Align modes.
"""

import os
from pathlib import Path
import re
import subprocess
import tempfile

from .subtitle_filters import srt_ts_to_ms, ms_to_srt_ts


def smart_sync(video_path, cues, model_size='base', language=None,
               num_segments=3, sample_minutes=5,
               progress_callback=None, cancel_event=None,
               engine='faster-whisper'):
    """Auto-sync subtitles to video audio using Whisper speech recognition.

    Transcribes the audio, matches Whisper segments to subtitle cues by text
    similarity, and computes the optimal timestamp offset.

    Args:
        video_path: Path to the video file.
        cues: List of subtitle cue dicts.
        model_size: Whisper model size ('tiny', 'base', 'small', 'medium', 'large').
        language: Language code (e.g. 'en'). None = auto-detect.
        progress_callback: Optional callable(message) for status updates.
        cancel_event: Optional threading.Event for cancellation.
        engine: 'faster-whisper' (standard ~400ms accuracy) or
                'whisperx' (precise ~50ms accuracy via forced alignment).

    Returns:
        dict with keys:
            'offset_ms': int — median offset in milliseconds
            'matches': list of (cue_idx, whisper_time_ms, cue_time_ms, similarity, text) tuples
            'drift_ms': int — estimated drift (difference between first and last match offsets)
            'whisper_segments': list of Whisper segment dicts
        Returns None on failure.
    """
    import tempfile
    from difflib import SequenceMatcher

    if engine in ('whisperx', 'whisperx-align'):
        try:
            import whisperx
            import torch
        except ImportError as _imp_err:
            if progress_callback:
                _err_msg = str(_imp_err)
                if 'is_offline_mode' in _err_msg:
                    progress_callback(
                        "WhisperX/transformers version conflict: " + _err_msg)
                    progress_callback(
                        "Fix: pip install --user 'transformers<4.45'")
                else:
                    progress_callback("whisperx not installed")
            return None
    else:
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            if progress_callback:
                progress_callback("faster-whisper not installed")
            return None

    tmpdir = tempfile.mkdtemp(prefix='docflix_sync_')

    try:
        # ══════════════════════════════════════════════════════════════
        # Direct Align mode — skip Whisper, align subtitle text directly
        # against the audio waveform using wav2vec2 forced alignment.
        # Every cue gets its own precise timestamp.
        # ══════════════════════════════════════════════════════════════
        if engine == 'whisperx-align':
            duration = get_video_duration(video_path) or 7200

            # ── Extract full audio ──
            if progress_callback:
                progress_callback(f"Extracting audio ({duration/60:.0f} min)...")
            audio_path = os.path.join(tmpdir, 'audio_full.wav')
            extract_timeout = max(120, int(duration / 60) * 2 + 60)
            try:
                _ext = subprocess.run(
                    ['ffmpeg', '-y', '-i', video_path,
                     '-vn', '-acodec', 'pcm_s16le', '-ar', '16000', '-ac', '1',
                     audio_path],
                    capture_output=True, text=True, timeout=extract_timeout)
                if _ext.returncode != 0 or not os.path.exists(audio_path):
                    if progress_callback:
                        progress_callback("Audio extraction failed")
                    return None
            except subprocess.TimeoutExpired:
                if progress_callback:
                    progress_callback("Audio extraction timed out")
                return None

            if cancel_event and cancel_event.is_set():
                return None

            # ── Load alignment model only (no Whisper model needed) ──
            _wx_device = "cuda" if torch.cuda.is_available() else "cpu"
            _wx_lang = language or 'en'
            if progress_callback:
                _dev = 'GPU' if _wx_device == 'cuda' else 'CPU'
                progress_callback(f"Loading alignment model for '{_wx_lang}' "
                                  f"on {_dev}...")
            try:
                align_model, align_metadata = whisperx.load_align_model(
                    language_code=_wx_lang, device=_wx_device)
            except Exception as e:
                if progress_callback:
                    progress_callback(f"Failed to load alignment model: {e}")
                return None

            if cancel_event and cancel_event.is_set():
                return None

            # ── Build segments from subtitle cues ──
            if progress_callback:
                progress_callback(f"Aligning {len(cues)} cues against audio "
                                  f"(forced alignment)...")
            segments = []
            for cue in cues:
                text = cue['text'].replace('\n', ' ').strip()
                # Skip empty / music-only / HI-only cues
                clean = re.sub(r'[♪♫\[\]\(\)]', '', text).strip()
                if not clean or len(clean) < 2:
                    continue
                segments.append({
                    'start': srt_ts_to_ms(cue['start']) / 1000,
                    'end': srt_ts_to_ms(cue['end']) / 1000,
                    'text': text,
                })

            if not segments:
                if progress_callback:
                    progress_callback("No alignable subtitle cues found")
                return None

            # ── Forced alignment — subtitle text against audio ──
            try:
                audio_array = whisperx.load_audio(audio_path)
                aligned = whisperx.align(
                    segments, align_model, align_metadata,
                    audio_array, _wx_device,
                    return_char_alignments=True)
            except Exception as e:
                if progress_callback:
                    progress_callback(f"Alignment failed: {e}")
                return None

            if cancel_event and cancel_event.is_set():
                return None

            # ── Build per-cue matches from aligned results ──
            aligned_segs = aligned.get('segments', [])
            if progress_callback:
                progress_callback(f"Aligned {len(aligned_segs)}/{len(segments)} "
                                  f"segments. Building matches...")

            matches = []
            whisper_segments = []
            # Map aligned segments back to original cue indices
            seg_idx = 0  # index into the segments list we built
            for ci, cue in enumerate(cues):
                text = cue['text'].replace('\n', ' ').strip()
                clean = re.sub(r'[♪♫\[\]\(\)]', '', text).strip()
                if not clean or len(clean) < 2:
                    continue  # this cue was skipped during segment building

                if seg_idx >= len(aligned_segs):
                    break

                aseg = aligned_segs[seg_idx]
                seg_idx += 1

                # Get precise start — prefer char-level, fall back to word, then segment
                precise_start = None
                chars = aseg.get('chars', [])
                if chars:
                    for c in chars:
                        if 'start' in c:
                            precise_start = c['start']
                            break
                if precise_start is None:
                    words = aseg.get('words', [])
                    if words:
                        for w in words:
                            if 'start' in w:
                                precise_start = w['start']
                                break
                if precise_start is None:
                    precise_start = aseg.get('start')
                if precise_start is None:
                    continue  # alignment failed for this segment

                whisper_ms = int(precise_start * 1000)
                cue_ms = srt_ts_to_ms(cue['start'])

                matches.append((ci, whisper_ms, cue_ms, 1.0,
                               cue['text'][:40].replace('\n', ' ')))
                whisper_segments.append({
                    'start': precise_start,
                    'end': aseg.get('end', precise_start + 1),
                    'text': aseg.get('text', '').strip(),
                })

            if not matches:
                if progress_callback:
                    progress_callback("Direct alignment produced no matches")
                return None

            # ── VAD boundary snapping ──
            # Snap cue start times to actual speech onsets detected by Silero VAD.
            # WhisperX alignment gives phoneme positions (~50ms); VAD detects the
            # exact silence→speech transition (~20ms).
            try:
                import bisect
                import wave as _wave
                import numpy as _np
                from faster_whisper.vad import get_speech_timestamps, VadOptions

                if progress_callback:
                    progress_callback("Running VAD for boundary snapping...")

                # Load audio as float32 numpy array
                with _wave.open(audio_path, 'r') as _wf:
                    _frames = _wf.readframes(_wf.getnframes())
                    _audio_np = _np.frombuffer(
                        _frames, dtype=_np.int16).astype(_np.float32) / 32768.0

                # Run VAD with tight parameters — no padding, detect short gaps
                _vad_opts = VadOptions(
                    min_silence_duration_ms=150,
                    speech_pad_ms=0,
                    threshold=0.5,
                    min_speech_duration_ms=100,
                )
                _speech_ts = get_speech_timestamps(
                    _audio_np, vad_options=_vad_opts)

                if _speech_ts:
                    # Build sorted onset/offset lists in ms (16 samples = 1ms at 16kHz)
                    _onsets_ms = sorted(
                        int(ts['start'] / 16) for ts in _speech_ts)
                    _offsets_ms = sorted(
                        int(ts['end'] / 16) for ts in _speech_ts)

                    SNAP_WINDOW_MS = 150  # only snap if boundary within ±150ms
                    _snapped = 0

                    for i, (ci, wt_ms, ct_ms, sim, txt) in enumerate(matches):
                        # Find nearest VAD speech onset to this cue's start
                        idx = bisect.bisect_left(_onsets_ms, wt_ms)
                        best_onset = None
                        best_dist = SNAP_WINDOW_MS + 1
                        for j in (idx - 1, idx):
                            if 0 <= j < len(_onsets_ms):
                                dist = abs(_onsets_ms[j] - wt_ms)
                                if dist < best_dist:
                                    best_dist = dist
                                    best_onset = _onsets_ms[j]
                        if best_onset is not None and best_dist <= SNAP_WINDOW_MS:
                            matches[i] = (ci, best_onset, ct_ms, sim, txt)
                            _snapped += 1

                    if progress_callback:
                        progress_callback(
                            f"VAD snap: {_snapped}/{len(matches)} cue starts "
                            f"snapped to speech onsets "
                            f"({len(_onsets_ms)} speech segments detected)")
                else:
                    if progress_callback:
                        progress_callback("VAD detected no speech — skipping snap")

            except ImportError:
                if progress_callback:
                    progress_callback("VAD snap skipped — faster-whisper not installed")
            except Exception as _vad_err:
                if progress_callback:
                    progress_callback(f"VAD snap skipped: {_vad_err}")

            # ── Calculate offset ──
            offsets = [wt - ct for _, wt, ct, _, _ in matches]
            offsets.sort()
            median_offset = offsets[len(offsets) // 2]

            mid_time = srt_ts_to_ms(cues[len(cues)//2]['start'])
            early = [wt - ct for _, wt, ct, _, _ in matches if ct < mid_time]
            late = [wt - ct for _, wt, ct, _, _ in matches if ct >= mid_time]
            drift = ((sum(late)/len(late)) - (sum(early)/len(early))) \
                    if early and late else 0

            if progress_callback:
                progress_callback(f"Direct Align: {len(matches)}/{len(cues)} cues "
                                  f"aligned. Offset: {median_offset:+d}ms, "
                                  f"Drift: {drift:+.0f}ms")

            return {
                'offset_ms': median_offset,
                'matches': matches,
                'drift_ms': int(drift),
                'whisper_segments': whisper_segments,
            }

        # ══════════════════════════════════════════════════════════════
        # Standard / Precise mode — Whisper transcription + matching
        # ══════════════════════════════════════════════════════════════

        # ── Pre-check: Compare video and subtitle durations ──
        duration = get_video_duration(video_path) or 7200
        if cues:
            last_cue_ms = srt_ts_to_ms(cues[-1]['end'])
            sub_duration = last_cue_ms / 1000
            diff_pct = abs(duration - sub_duration) / max(duration, 1) * 100
            if diff_pct > 15:
                if progress_callback:
                    progress_callback(f"⚠ Duration mismatch: video is {duration/60:.0f} min, "
                                      f"subtitles span {sub_duration/60:.0f} min "
                                      f"({diff_pct:.0f}% difference). "
                                      f"These may be different cuts.")

        # ── Phase 1: Get video duration and plan sample segments ──
        full_scan = (num_segments <= 0 or sample_minutes <= 0)

        if full_scan:
            # Full Scan — extract entire audio as one segment
            samples = [(0, duration)]
            if progress_callback:
                progress_callback(f"Full Scan — extracting {duration/60:.0f} min of audio...")
        else:
            SAMPLE_LEN = sample_minutes * 60
            n_segs = max(1, num_segments)
            if duration <= SAMPLE_LEN * 2 or n_segs == 1:
                samples = [(0, min(duration, SAMPLE_LEN) if n_segs == 1 else duration)]
            else:
                samples = []
                for i in range(n_segs):
                    center = duration * (i / (n_segs - 1)) if n_segs > 1 else 0
                    seg_start = max(0, center - SAMPLE_LEN / 2)
                    seg_end = min(duration, seg_start + SAMPLE_LEN)
                    seg_start = max(0, seg_end - SAMPLE_LEN)
                    samples.append((seg_start, seg_end))

            if progress_callback:
                total_sample = sum(e - s for s, e in samples)
                progress_callback(f"Quick Scan — {len(samples)} segments "
                                  f"({total_sample/60:.0f} min of {duration/60:.0f} min total)...")

        # ── Phase 2: Extract audio samples ──
        audio_paths = []
        for si, (ss, se) in enumerate(samples):
            if cancel_event and cancel_event.is_set():
                return None
            audio_path = os.path.join(tmpdir, f'audio_{si}.wav')
            extract_cmd = [
                'ffmpeg', '-y',
                '-ss', f'{ss:.1f}', '-t', f'{se - ss:.1f}',
                '-i', video_path,
                '-vn', '-acodec', 'pcm_s16le', '-ar', '16000', '-ac', '1',
                audio_path
            ]
            try:
                seg_duration = se - ss
                # Timeout: at least 120s, scale with segment length (~1s per minute of audio)
                extract_timeout = max(120, int(seg_duration / 60) * 2 + 60)
                if progress_callback:
                    progress_callback(f"Extracting audio segment {si+1}/{len(samples)} "
                                      f"({ss/60:.0f}m–{se/60:.0f}m)...")
                result = subprocess.run(extract_cmd, capture_output=True,
                                        text=True, timeout=extract_timeout)
                if result.returncode == 0 and os.path.exists(audio_path):
                    audio_paths.append((ss, audio_path))
            except subprocess.TimeoutExpired:
                continue

        if not audio_paths:
            if progress_callback:
                progress_callback("Audio extraction failed for all segments")
            return None

        # ── Phase 3: Load Whisper model ──
        if engine == 'whisperx':
            if progress_callback:
                _dev_label = 'GPU (CUDA)' if torch.cuda.is_available() else 'CPU'
                progress_callback(f"Loading WhisperX model ({model_size}) on {_dev_label}...")
            try:
                _wx_device = "cuda" if torch.cuda.is_available() else "cpu"
                _wx_compute = "float16" if _wx_device == "cuda" else "int8"
                model = whisperx.load_model(model_size, _wx_device,
                                            compute_type=_wx_compute)
            except Exception as e:
                if progress_callback:
                    progress_callback(f"Failed to load WhisperX model: {e}")
                    if 'is_offline_mode' in str(e) or 'transformers' in str(e):
                        progress_callback(
                            "Fix: pip install --user 'transformers<4.45'")
                return None
        else:
            if progress_callback:
                progress_callback(f"Loading Whisper model ({model_size})...")
            try:
                model = WhisperModel(model_size, device="cpu", compute_type="int8")
            except Exception as e:
                if progress_callback:
                    progress_callback(f"Failed to load Whisper model: {e}")
                return None

        if cancel_event and cancel_event.is_set():
            return None

        # ── Phase 4: Transcribe each sample ──
        whisper_segments = []

        if engine == 'whisperx':
            # ── WhisperX: batch transcription + forced alignment per segment ──
            _wx_lang = language or 'en'
            _align_model = None
            _align_metadata = None

            for si, (offset_s, apath) in enumerate(audio_paths):
                if cancel_event and cancel_event.is_set():
                    return None
                if progress_callback:
                    progress_callback(f"Transcribing segment {si+1}/{len(audio_paths)} "
                                      f"(WhisperX)...")
                try:
                    audio_array = whisperx.load_audio(apath)
                    tx_result = model.transcribe(audio_array, batch_size=16,
                                                 language=_wx_lang)
                    # Auto-detect language from first segment if not specified
                    detected_lang = tx_result.get('language', _wx_lang)
                    if not language and detected_lang:
                        _wx_lang = detected_lang

                    if cancel_event and cancel_event.is_set():
                        return None

                    # Load alignment model once (per language)
                    if _align_model is None:
                        if progress_callback:
                            progress_callback(f"Loading alignment model for "
                                              f"'{_wx_lang}'...")
                        try:
                            _align_model, _align_metadata = \
                                whisperx.load_align_model(
                                    language_code=_wx_lang,
                                    device=_wx_device)
                        except Exception as ae:
                            if progress_callback:
                                progress_callback(f"Alignment model failed: {ae}")
                                progress_callback("Falling back to segment-level "
                                                  "timestamps (less precise)")
                            # Fall back: use unaligned segment timestamps
                            for seg in tx_result.get('segments', []):
                                whisper_segments.append({
                                    'start': seg['start'] + offset_s,
                                    'end': seg['end'] + offset_s,
                                    'text': seg.get('text', '').strip(),
                                })
                            continue

                    if cancel_event and cancel_event.is_set():
                        return None

                    # ── Forced alignment — phoneme-level precision ──
                    if progress_callback:
                        progress_callback(f"Aligning segment {si+1}/{len(audio_paths)} "
                                          f"(forced alignment)...")
                    aligned = whisperx.align(
                        tx_result['segments'], _align_model, _align_metadata,
                        audio_array, _wx_device,
                        return_char_alignments=True)

                    # Collect aligned segments — prefer char-level timestamps
                    count = 0
                    for seg in aligned.get('segments', []):
                        precise_start = None
                        # Char-level (most precise)
                        chars = seg.get('chars', [])
                        if chars:
                            for c in chars:
                                if 'start' in c:
                                    precise_start = c['start'] + offset_s
                                    break
                        # Word-level fallback
                        if precise_start is None:
                            words = seg.get('words', [])
                            if words:
                                for w in words:
                                    if 'start' in w:
                                        precise_start = w['start'] + offset_s
                                        break
                        # Segment-level fallback
                        if precise_start is None:
                            precise_start = seg['start'] + offset_s

                        whisper_segments.append({
                            'start': precise_start,
                            'end': seg['end'] + offset_s,
                            'text': seg.get('text', '').strip(),
                        })
                        count += 1
                        if progress_callback and count % 10 == 0:
                            progress_callback(f"Segment {si+1}: {count} phrases "
                                              f"(aligned)...")

                except Exception as e:
                    if progress_callback:
                        progress_callback(f"WhisperX error in segment {si+1}: {e}")
                    continue
        else:
            # ── faster-whisper: streaming transcription per segment ──
            for si, (offset_s, apath) in enumerate(audio_paths):
                if cancel_event and cancel_event.is_set():
                    return None
                if progress_callback:
                    progress_callback(f"Transcribing segment {si+1}/{len(audio_paths)}...")
                try:
                    segments_gen, info = model.transcribe(
                        apath,
                        language=language,
                        word_timestamps=True,
                        vad_filter=True,
                    )
                    count = 0
                    for seg in segments_gen:
                        if cancel_event and cancel_event.is_set():
                            return None
                        # Use word-level timestamp for more precise start time
                        # The first word's start is more accurate than the segment start
                        words = seg.words if hasattr(seg, 'words') and seg.words else None
                        if words:
                            precise_start = words[0].start + offset_s
                        else:
                            precise_start = seg.start + offset_s
                        whisper_segments.append({
                            'start': precise_start,
                            'end': seg.end + offset_s,
                            'text': seg.text.strip(),
                        })
                        count += 1
                        if progress_callback and count % 10 == 0:
                            progress_callback(f"Segment {si+1}: {count} phrases "
                                              f"({seg.end:.0f}s)...")
                except Exception as e:
                    if progress_callback:
                        progress_callback(f"Transcription error in segment {si+1}: {e}")
                    continue

        if not whisper_segments:
            if progress_callback:
                progress_callback("Whisper produced no transcription")
            return None

        if progress_callback:
            progress_callback(f"Transcribed {len(whisper_segments)} phrases from "
                              f"{len(audio_paths)} segments. Matching to subtitles...")

        # ── Phase 3: Two-pass matching ──
        def _normalize(text):
            """Normalize text for comparison: lowercase, strip punctuation,
            remove speaker labels, HI annotations, and music notes."""
            text = text.lower()
            # Remove speaker labels: "JUNIOR: text" → "text"
            text = re.sub(r'^[a-z][a-z\s\'\.]{0,25}:\s*', '', text, flags=re.MULTILINE)
            # Remove HI annotations: [brackets] and (parentheses)
            text = re.sub(r'\[.*?\]', '', text)
            text = re.sub(r'\(.*?\)', '', text)
            # Remove music notes
            text = text.replace('♪', '').replace('♫', '')
            # Strip punctuation and normalize whitespace
            text = re.sub(r'[^\w\s]', '', text)
            text = re.sub(r'\s+', ' ', text).strip()
            return text

        # Pre-normalize all Whisper segments for speed
        norm_segments = []
        for seg in whisper_segments:
            nt = _normalize(seg['text'])
            if nt and len(nt) >= 3:
                norm_segments.append((seg, nt))

        if progress_callback:
            progress_callback(f"{len(norm_segments)} usable phrases "
                              f"(of {len(whisper_segments)} total)")

        total_cues = len(cues)

        def _match_sequential():
            """Sequential matching: walk through cues and Whisper segments in order.
            Each match must come AFTER the previous match in the Whisper timeline.
            This prevents cross-matching and handles different cuts/edits."""
            matches = []
            seg_start = 0  # start searching from this Whisper segment index
            # Scale search window with segment density — Full Scan on long files
            # can produce 1000+ segments; a fixed window of 100 (~5 min) loses
            # sync after any extended gap (music, credits, silence).
            # Use at least 100, scale up to cover ~1/3 of all segments.
            SEARCH_WINDOW = max(100, len(norm_segments) // 3)
            _consec_misses = 0  # track consecutive unmatched cues

            for ci, cue in enumerate(cues):
                if cancel_event and cancel_event.is_set():
                    return None

                if progress_callback and (ci % 10 == 0 or ci == total_cues - 1):
                    progress_callback(f"Matching cue {ci+1}/{total_cues} "
                                      f"({len(matches)} matched)...")

                cue_text = _normalize(cue['text'])
                if len(cue_text) < 3:
                    continue

                cue_start_ms = srt_ts_to_ms(cue['start'])
                best_sim = 0
                best_idx = None

                # Only search forward from last match position
                search_end = min(seg_start + SEARCH_WINDOW, len(norm_segments))
                for si in range(seg_start, search_end):
                    seg, seg_text = norm_segments[si]

                    # Length ratio filter — relaxed to handle different
                    # sentence splitting between subtitles and Whisper
                    len_ratio = len(cue_text) / max(len(seg_text), 1)
                    if len_ratio < 0.2 or len_ratio > 5.0:
                        continue

                    sim = SequenceMatcher(None, cue_text, seg_text).ratio()
                    if sim > best_sim:
                        best_sim = sim
                        best_idx = si

                if best_sim > 0.6 and best_idx is not None:
                    seg = norm_segments[best_idx][0]
                    whisper_ms = int(seg['start'] * 1000)
                    this_offset = whisper_ms - cue_start_ms

                    # Consistency check: reject if offset changes too dramatically
                    # from recent matches (catches bad jumps from wrong matches)
                    accept = True
                    if matches:
                        recent_offsets = [wt - ct for _, wt, ct, _, _ in matches[-5:]]
                        avg_recent = sum(recent_offsets) / len(recent_offsets)
                        # Allow up to 30 seconds of drift from recent average
                        if abs(this_offset - avg_recent) > 30000:
                            accept = False  # skip this suspicious match

                    if accept:
                        matches.append((ci, whisper_ms, cue_start_ms, best_sim,
                                        cue['text'][:40].replace('\n', ' ')))
                        # Advance search start past this match
                        seg_start = best_idx + 1
                        _consec_misses = 0
                    else:
                        _consec_misses += 1
                else:
                    _consec_misses += 1

                # If we've gone 50+ cues without a match, try resetting the
                # search position based on time — we may have lost sync
                if _consec_misses >= 50 and norm_segments:
                    # Estimate where in the segments we should be, based on
                    # the cue's timestamp relative to total duration
                    cue_frac = cue_start_ms / max(
                        srt_ts_to_ms(cues[-1]['end']), 1)
                    est_idx = int(cue_frac * len(norm_segments))
                    # Only jump forward, never backward past confirmed matches
                    if est_idx > seg_start:
                        seg_start = max(seg_start, est_idx - SEARCH_WINDOW // 4)
                        _consec_misses = 0
                        if progress_callback:
                            progress_callback(
                                f"  Re-syncing search at segment {seg_start} "
                                f"(cue {ci+1} had {_consec_misses} misses)...")

            return matches

        # ── Sequential matching ──
        if progress_callback:
            progress_callback("Matching subtitles to audio (sequential)...")

        matches = _match_sequential()
        if matches is None:
            return None  # cancelled

        if not matches:
            if progress_callback:
                progress_callback("No text matches found between subtitles and audio")
            return None

        # ── Phase 4: Calculate offset ──
        try:
            offsets = [wt - ct for (_, wt, ct, _, _) in matches]
            offsets.sort()
            median_offset = offsets[len(offsets) // 2]

            mid_time = srt_ts_to_ms(cues[len(cues)//2]['start'])
            early = [wt - ct for _, wt, ct, _, _ in matches if ct < mid_time]
            late = [wt - ct for _, wt, ct, _, _ in matches if ct >= mid_time]
            if early and late:
                drift = (sum(late) / len(late)) - (sum(early) / len(early))
            else:
                drift = 0

            if progress_callback:
                progress_callback(f"Matched {len(matches)}/{len(cues)} cues. "
                                  f"Offset: {median_offset:+d}ms, Drift: {drift:+.0f}ms")

            return {
                'offset_ms': median_offset,
                'matches': matches,
                'drift_ms': int(drift),
                'whisper_segments': whisper_segments,
            }
        except Exception as e:
            if progress_callback:
                progress_callback(f"Error calculating offset: {e}")
            return None

    finally:
        import shutil as _shutil_cleanup
        _shutil_cleanup.rmtree(tmpdir, ignore_errors=True)


# Max recommended characters per subtitle line for readability
MAX_CHARS_PER_LINE = 42

