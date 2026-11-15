"""PGS cue durations come from the disc's own clear events.

⚠️ WHY THIS FILE EXISTS (2026-11-15): the OCR used to end every cue at the next cue's
start (capped at 10 s) and never read the clears, so lines lingered through every pause.
Starts were right, so it looked fine while people talked. Real case from Stranger Things
S01E01: "See you tomorrow." shows at 282.491, the disc clears it at 284.160, and the next
line is at 299.800. The old rule made it 10 s on screen.
"""
from modules.subtitle_ocr import pgs_durations


def test_cue_ends_at_the_disc_clear_not_the_next_line():
    durs = pgs_durations([282.491, 299.800], [284.160])
    assert round(durs[0], 3) == 1.669


def test_next_line_replacing_it_wins_over_a_later_clear():
    # no clear between two lines: the second one replaces the first
    durs = pgs_durations([10.0, 12.0], [15.0])
    assert durs == [2.0, 3.0]


def test_clear_at_the_same_instant_as_the_start_is_not_its_end():
    # the previous line's clear sits a hair before/at this start (S05E06 "Max lied to us")
    durs = pgs_durations([2276.52, 2278.067], [2278.03, 2279.65])
    assert round(durs[1], 3) == 1.583


def test_no_clears_at_all_falls_back_to_the_old_rule():
    assert pgs_durations([0.0, 20.0], []) == [10.0, 3.0]


def test_clamped_to_half_a_second_minimum():
    assert pgs_durations([5.0, 9.0], [5.1]) == [0.5, 3.0]
