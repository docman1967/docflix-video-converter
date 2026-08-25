"""Regression tests for Whisper cue segmentation.

⚠️ The bug these exist for was SILENT. `balance_lines` discarded every wrapped
line past `max_lines`, so a cue whose text had no clean two-line split lost its
final word — and produced a perfectly valid .srt with a word missing from the
transcript. No error, no warning, nothing to notice. Measured at ~3.8% of cues
in the 43-84 character window against real Haven commentary transcripts.

**The assertion that would have caught it on day one is `words_in == words_out`.**
Everything else here is secondary. If you touch segmentation, keep that check.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from modules.whisper_subtitles import balance_lines, segment_into_cues  # noqa: E402


class _W:
    """Minimal stand-in for a faster-whisper word."""
    def __init__(self, word, start, end):
        self.word, self.start, self.end = word, start, end


class _Seg:
    """Minimal stand-in for a transcript segment carrying word timings."""
    def __init__(self, words):
        self.words = words
        self.start = words[0].start
        self.end = words[-1].end
        self.text = " ".join(w.word for w in words)


def _segment(text, *, pause_before=None, word_dur=0.22, gap=0.02):
    """Build one segment from *text*, optionally inserting a long pause."""
    words, t = [], 0.0
    for w in text.split():
        words.append(_W(w, t, t + word_dur))
        t += word_dur + (0.6 if w == pause_before else gap)
    return [_Seg(words)]


# ── the one that matters ────────────────────────────────────────────────────

def test_balance_lines_never_drops_words():
    """No wrapping decision may ever discard text."""
    cases = [
        # 83 chars, no clean 2-way split within 42 — the original failure.
        "this is a deliberately long stretch of speech that runs right up against the eighty",
        "the back? That, if you recognize that, that is the house that the farmer, the guy in",
        "We were going to use it a bunch more times, and I don't think we ever did. We almost",
        "talk about that. We've talked from the beginning that Nathan really is laconic and",
    ]
    for text in cases:
        out = balance_lines(text, max_len=42, max_lines=2)
        assert out.replace("\n", " ").split() == text.split(), (
            f"words lost wrapping: {text!r} -> {out!r}")


def test_segment_into_cues_never_drops_words():
    """Round-tripping a transcript must conserve every word."""
    text = ("I'm John Hamm, I play Don Draper with me, R. the back? That, if you "
            "recognize that, that is the house that the farmer, the guy in")
    segs = _segment(text, pause_before="me,")
    out = segment_into_cues(segs)
    got = " ".join(c.text.replace("\n", " ") for c in out).split()
    assert got == text.split(), f"expected {len(text.split())} words, got {len(got)}"


# ── the orphan pass ─────────────────────────────────────────────────────────

def test_pause_does_not_strand_a_tiny_cue():
    """A speaker's pause must not leave a two-character cue on its own.

    Real output before the fix:
        00:00:12,378 --> 00:00:14,789   I'm John Hamm, I play Don Draper with me,
        00:00:15,020 --> 00:00:15,820   R.
    """
    segs = _segment("I'm John Hamm, I play Don Draper with me, R.", pause_before="me,")
    out = segment_into_cues(segs)
    assert len(out) == 1, f"expected the orphan merged away, got {[c.text for c in out]}"
    assert "R." in out[0].text


def test_orphan_pass_is_opt_out():
    """min_cue_chars=0 restores the old splitting behaviour."""
    segs = _segment("I'm John Hamm, I play Don Draper with me, R.", pause_before="me,")
    assert len(segment_into_cues(segs, min_cue_chars=0)) == 2


def test_orphan_not_merged_past_the_character_budget():
    """⚠️ An orphan is annoying; an over-long cue is worse. Guard wins."""
    full = ("this is a deliberately long stretch of speech that runs right up "
            "against the eighty four character budget indeed")
    segs = _segment(full + " Ah.", pause_before="indeed")
    out = segment_into_cues(segs, max_line_length=42, max_lines=2)
    for cue in out:
        flat = cue.text.replace("\n", " ")
        assert len(flat) <= 42 * 2 + 8, f"cue blew the budget: {flat!r}"


def test_leading_orphan_merges_forward():
    """The first cue has no predecessor — it must pull forward, not survive alone."""
    segs = _segment("Right. so anyway that is what happened next", pause_before="Right.")
    out = segment_into_cues(segs)
    assert out[0].text.replace("\n", " ").startswith("Right."), out[0].text
    assert len(out[0].text.replace("\n", " ")) >= 15
