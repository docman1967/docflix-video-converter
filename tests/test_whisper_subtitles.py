"""The broadcast-style cue segmenter (added 2026-07-22) + its helpers."""
import modules.whisper_subtitles as W


def _w(text, start, end):
    return W.SubSegment(start=start, end=end, text=text)


def test_fmt_srt_time():
    assert W._fmt_srt_time(0) == "00:00:00,000"
    assert W._fmt_srt_time(3661.234) == "01:01:01,234"


def test_balance_lines_respects_width_and_max_lines():
    text = "the quick brown fox jumps over the lazy dog again and again and again"
    out = W.balance_lines(text, max_len=42, max_lines=2)
    assert out.count("\n") <= 1
    for line in out.split("\n"):
        assert len(line) <= 42


def test_segment_into_cues_splits_on_pause_and_respects_constraints():
    # Two sentences separated by a ~2s pause → must become at least two cues.
    words = [("I", 0.0, 0.2), ("don't", 0.2, 0.5), ("know.", 0.5, 1.0),
             ("Get", 3.0, 3.3), ("out", 3.3, 3.6), ("now!", 3.6, 4.0)]
    seg = W.SubSegment(
        start=0.0, end=4.0,
        text=" ".join(w[0] for w in words),
        words=[_w(t, s, e) for t, s, e in words],
    )
    cues = W.segment_into_cues([seg], max_line_length=42, reading_speed=17.0,
                               split_gap=0.5, min_duration=0.8, max_duration=7.0)
    assert len(cues) >= 2                       # the pause splits it
    for c in cues:
        assert c.end > c.start                  # positive duration
        for line in c.text.split("\n"):
            assert len(line) <= 42              # width respected
    for a, b in zip(cues, cues[1:]):
        assert a.start <= b.start               # sorted by start


def test_segments_to_srt_shape():
    srt = W.segments_to_srt([_w("Hello", 0.0, 1.0), _w("World", 1.0, 2.0)])
    assert "00:00:00,000 --> 00:00:01,000" in srt
    assert "Hello" in srt and "World" in srt
