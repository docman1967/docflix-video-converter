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


# ═══════════════════════════════════════════════════════════════════════════
# Cue segmentation regressions, added 2026-08-25.
#
# ⚠️ The bug most of these exist for was SILENT. `balance_lines` discarded every
# wrapped line past `max_lines`, so a cue whose text had no clean two-line split
# lost its final word — producing a perfectly valid .srt with a word missing
# from the transcript. No error, no warning. Measured at ~3.8% of cues in the
# 43-84 char window against real Haven commentary transcripts.
#
# **The assertion that would have caught it on day one is words_in == words_out.**
# Everything else here is secondary. If you touch segmentation, keep that check.
# ═══════════════════════════════════════════════════════════════════════════

from modules.whisper_subtitles import balance_lines, segment_into_cues



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


def test_leading_orphan_merges_forward_when_it_is_a_fragment():
    """The first cue has no predecessor — a FRAGMENT must pull forward."""
    segs = _segment("and so anyway that is what happened next", pause_before="and")
    out = segment_into_cues(segs)
    flat = out[0].text.replace("\n", " ")
    assert flat.startswith("and so"), flat
    assert len(flat) >= 15, f"leading fragment left stranded: {flat!r}"


def test_leading_orphan_stands_when_it_is_a_whole_sentence():
    """⚠️ ...but a complete short sentence must NOT be swallowed forward.

    The guards were originally only on the backward merge. The pre-existing
    suite caught it: "I don't know." (13 chars) was pulled into "Get out now!"
    across a 2-second pause — the documentary bug in the one unguarded position.
    """
    segs = _segment("Right. so anyway that is what happened next", pause_before="Right.")
    out = segment_into_cues(segs)
    assert out[0].text.strip() == "Right.", \
        f"a complete sentence was swallowed forward: {out[0].text!r}"


def test_pause_does_not_strand_a_dangling_function_word():
    """A cue may not END on "and"/"to"/"the" just because the speaker paused.

    `_NO_BREAK_AFTER` already forbids stranding these at the end of a LINE. The
    same convention was never applied to the end of a CUE, so a pause could do
    what a line break could not. Measured at 18% of cues on real commentary.
    """
    segs = _segment("I think this was the best year of the whole show", pause_before="the")
    out = segment_into_cues(segs)
    for cue in out[:-1]:                      # last cue may legitimately trail
        flat = cue.text.replace("\n", " ").strip()
        if flat.endswith((".", "!", "?", "…", ",", ";", ":")):
            continue                          # punctuated = a fine place to stop
        last = flat.split()[-1].strip('"\'’”)]}»').lower()
        assert last not in ("the", "and", "to", "of", "a"), \
            f"cue ends on a dangling function word: {flat!r}"


def test_budget_still_wins_over_the_dangling_rule():
    """⚠️ A dangling word is a wart; an over-budget cue is a bug. Budget wins."""
    text = " ".join(["the"] * 40)             # nothing but function words
    segs = _segment(text)
    out = segment_into_cues(segs, max_line_length=42, max_lines=2)
    for cue in out:
        assert len(cue.text.replace("\n", " ")) <= 42 * 2 + 8, \
            f"dangling rule blew the budget: {cue.text!r}"
    got = " ".join(c.text.replace("\n", " ") for c in out).split()
    assert got == text.split(), "words lost while avoiding a dangling break"


# ── documentary structure ───────────────────────────────────────────────────
# ⚠️ The orphan pass was tuned on COMMENTARY and broke scripted narration.
# Tony caught it by asking "a documentary is structurally different" BEFORE
# spending GPU time on one. These two tests are that question, frozen.

def test_documentary_beat_is_not_swallowed():
    """A short cue after a full stop and a held pause must stand on its own.

    Narration uses short cues deliberately:
        'For thirty years the colony thrived here.'   0.00 -> 2.41
        'Until now.'                                  4.12 -> 4.92
    Merging produced one 4.84s cue containing 1.7s of silence, and put
    "Until now." on screen 1.7s BEFORE it was spoken.
    """
    words = [("For", .2, .02), ("thirty", .2, .02), ("years", .2, .02),
             ("the", .2, .02), ("colony", .2, .02), ("thrived", .2, .02),
             ("here.", .3, 2.50), ("Until", .3, .02), ("now.", .4, 0)]
    ws, t = [], 0.0
    for w, d, g in words:
        ws.append(_W(w, t, t + d))
        t += d + g
    out = segment_into_cues([_Seg(ws)])
    assert len(out) == 2, f"the beat was swallowed: {[c.text for c in out]}"
    assert out[1].text.strip() == "Until now."
    assert out[1].start > out[0].end, "punchline must not appear before it is spoken"


def test_orphan_not_merged_across_a_long_silence():
    """Even mid-sentence, a held pause is not something to paper over."""
    words = [(w, .22, .02) for w in "and then the whole thing just".split()]
    words[-1] = ("just", .22, 2.2)
    words.append(("collapsed", .35, 0))
    ws, t = [], 0.0
    for w, d, g in words:
        ws.append(_W(w, t, t + d))
        t += d + g
    out = segment_into_cues([_Seg(ws)])
    assert len(out) == 2, f"merged across 2.2s of silence: {[c.text for c in out]}"


def test_orphan_rebalances_when_previous_cue_is_full():
    """A budget break must not split a compound noun and strand the tail.

    Real output before the fix:
        290  00:17:54,374 --> 00:17:58,861  ...fasten your seat   (83 chars)
        291  00:17:58,901 --> 00:17:59,162  belt.                 (5 chars, 0.26s)

    Merging is impossible (83 + 6 > 84), so words are pushed BACK from the full
    cue into the orphan until both are viable — combine, then re-split sensibly.
    """
    text = ("And if you think Air Force One has always looked as classy as this, "
            "fasten your seat belt.")
    ws, t = [], 0.0
    for w in text.split():
        ws.append(_W(w, t, t + 0.22))
        t += 0.24
    out = segment_into_cues([_Seg(ws)])
    flats = [c.text.replace("\n", " ") for c in out]
    assert all(len(f) >= 15 for f in flats), f"orphan left stranded: {flats}"
    assert any("seat belt." in f for f in flats), \
        f"compound noun split across cues: {flats}"
    # timings must still be monotonic and drawn from the real words
    for a, b in zip(out, out[1:]):
        assert a.end <= b.start, "rebalance produced overlapping cues"
    got = " ".join(flats).split()
    assert got == text.split(), "words lost during rebalance"
