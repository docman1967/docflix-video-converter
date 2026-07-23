"""SRT parse/write round-trip + core filters — the heart of the subtitle tools."""
import modules.subtitle_filters as F

SRT = (
    "1\n00:00:01,000 --> 00:00:03,000\nHello world\n\n"
    "2\n00:00:04,000 --> 00:00:05,500\n[door slams]\nGet out!\n"
)


def _cue(start, end, text):
    return {"index": 1, "start": start, "end": end, "text": text}


def test_cue_char_len_ignores_newlines():
    assert F.cue_char_len(_cue("00:00:01,000", "00:00:02,000", "#")) == 1
    assert F.cue_char_len(_cue("00:00:01,000", "00:00:02,000", "Hello\nworld")) == 10


def test_cue_duration_and_start_ms():
    c = _cue("00:00:01,000", "00:00:03,500", "hi")
    assert F.cue_start_ms(c) == 1000
    assert F.cue_duration_ms(c) == 2500


def test_cue_cps_and_zero_duration():
    # 17 chars over 1.0s = 17 cps
    assert F.cue_cps(_cue("00:00:01,000", "00:00:02,000", "seventeen chars!!")) == 17.0
    # zero-duration cue with text → infinity (surfaces as a problem)
    assert F.cue_cps(_cue("00:00:01,000", "00:00:01,000", "x")) == float("inf")


def test_sort_by_length_surfaces_junk():
    cues = [_cue("00:00:05,000", "00:00:06,000", "A real line of dialogue here"),
            _cue("00:00:01,000", "00:00:02,000", "#"),
            _cue("00:00:03,000", "00:00:04,000", ".")]
    order = sorted(range(len(cues)), key=lambda i: F.cue_char_len(cues[i]))
    # the two junk cues cluster at the top; the data list itself is untouched
    assert [cues[i]["text"] for i in order[:2]] == ["#", "."]
    assert cues[0]["text"].startswith("A real line")   # original order preserved


def test_parse_srt_basic():
    cues = F.parse_srt(SRT)
    assert len(cues) == 2
    assert cues[0] == {
        "index": 1, "start": "00:00:01,000", "end": "00:00:03,000",
        "text": "Hello world",
    }


def test_parse_write_roundtrip():
    cues = F.parse_srt(SRT)
    recues = F.parse_srt(F.write_srt(cues))
    # timing + text must survive a full round trip
    assert [(c["start"], c["end"], c["text"]) for c in cues] == \
           [(c["start"], c["end"], c["text"]) for c in recues]


def test_remove_hi_strips_bracket_lines_keeps_dialogue():
    cues = F.parse_srt(SRT)
    out = F.filter_remove_hi([dict(cues[1])])
    assert out[0]["text"] == "Get out!"   # "[door slams]" removed, dialogue kept


def test_reduce_lines_caps_lines_per_cue():
    cue = {
        "index": 1, "start": "00:00:01,000", "end": "00:00:06,000",
        "text": "line one\nline two\nline three\nline four",
    }
    out = F.filter_reduce_lines([cue], max_lines=2, max_chars=42)
    for c in out:
        assert c["text"].count("\n") <= 1   # never more than 2 lines
