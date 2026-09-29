#!/usr/bin/env python3
"""Closing the OCR preview must not silently bin the run.

⚠️⚠️ WHY THIS FILE EXISTS — Tony, 2026-09-29: *"in the OCR preview, if you hit
close, it just....closes. It should remind the user that the subtitles haven't
been saved."*

⭐ WHAT IS ACTUALLY AT RISK IS THE WHOLE OCR RUN, not just the hand edits. The
cues live in a TEMP file; "Save" copies it out and "Load into Editor" hands it
on. Every other way out of that window ends at mon.destroy(), and minutes of
OCR go with it.

⚠️ THIS IS A SOURCE-STRUCTURE TEST, which is unusual and deliberate — the same
call the codebase already made in test_editor_tag_chain_parity.py. The guard
lives inside a 7,000-line closure that needs a Tk root, a loaded video and a
finished OCR run to reach, so there is no honest way to drive it in a unit
test. What CAN be checked is that no exit path bypasses it.

⭐⭐ AND THAT IS THE REAL RISK HERE, because subtitle_editor.py already carries
this warning about this very window:

    "Bound to <Destroy> rather than patched into each mon.destroy call site —
     there are FIVE of them (Close, cancel, save, load, WM close) and a missed
     one leaks the bitmap dir silently."

Writing this guard turned up a SIXTH and SEVENTH: the cancelled-OCR branch has
its own _save_partial/_load_partial pair, which the first pass missed. A future
eighth is exactly what these tests are for.
"""
import re
from pathlib import Path

import pytest

from modules.subtitle_editor import _real_cue_count

SRC = (Path(__file__).resolve().parent.parent
       / 'modules' / 'subtitle_editor.py').read_text()

# ⚠️ Proximity checks run against a COMMENT-STRIPPED copy. Measuring "is the
# flag set near the copy?" on raw source measures how much I wrote ABOUT the
# code, not the code — the first cut of this file failed purely because the
# explanatory comments pushed the anchors out of the window. Caught 2026-09-29.
CODE = '\n'.join(l for l in SRC.splitlines() if not l.lstrip().startswith('#'))


# ── The real function the guard gates on ────────────────────────────────────

def test_real_cue_count_ignores_empty_frames():
    """⚠️ The guard asks _real_cue_count, not len(cues). for_review=True keeps
    empty-OCR frames in the list, and warning "3 cues will be discarded" over
    three blank bitmaps would be a modal that means nothing."""
    assert _real_cue_count([]) == 0
    assert _real_cue_count([{'text': ''}, {'text': '   '}]) == 0
    assert _real_cue_count([{'text': 'real'}, {'text': ''}]) == 1


# ── No exit may bypass the guard ────────────────────────────────────────────

def test_every_close_button_goes_through_the_guard():
    """⛔ A new branch that wires Close straight to mon.destroy is the bug
    coming back. There are three such branches today: OCR cancelled, OCR done,
    and OCR produced no output."""
    stragglers = re.findall(r'cancel_btn\.configure\(\s*text="Close",\s*'
                            r'command=mon\.destroy\s*\)', SRC)
    assert not stragglers, (
        f'{len(stragglers)} Close button(s) still call mon.destroy directly; '
        f'they must call _close_monitor')
    guarded = re.findall(r'cancel_btn\.configure\(\s*text="Close",\s*'
                         r'command=_close_monitor\s*\)', SRC)
    assert len(guarded) == 3, f'expected 3 guarded Close buttons, found {len(guarded)}'


def test_the_guard_exists_and_asks_before_destroying():
    assert 'def _close_monitor():' in SRC
    body = SRC.split('def _close_monitor():', 1)[1].split('\n                def ', 1)[0]
    assert 'askyesno' in body, 'the guard must actually ask'
    assert "default='no'" in body, (
        'the safe answer must be the default — Return on an unexpected dialog '
        'must not discard the run')
    assert '_real_cue_count' in body
    assert "_ocr_kept['saved']" in body and "_ocr_kept['loaded']" in body


# ── Both disposal pairs must mark the work as kept ──────────────────────────

def test_both_save_paths_mark_it_saved():
    """⚠️ TWO save paths: the completed run and the cancelled-run partial.
    Marking only one nags him after he has already saved."""
    assert SRC.count("_ocr_kept['saved'] = True") == 2, (
        "expected both _save_srt and _save_partial to mark the run saved")


def test_both_load_paths_mark_it_loaded():
    assert SRC.count("_ocr_kept['loaded'] = True") == 2, (
        "expected both _load_into_editor and _load_partial to mark it loaded")


def test_saving_is_marked_only_after_the_copy_succeeds():
    """⚠️ A failed write must still count as unsaved — that is the one case
    where the work really is about to be lost."""
    for chunk in CODE.split("_ocr_kept['saved'] = True")[:-1]:
        tail = chunk[-300:]
        assert 'shutil.copy2' in tail, (
            'the saved flag must be set after the copy, inside the try')


# ── Editing after a save makes it dirty again ───────────────────────────────

def test_mutations_clear_the_saved_flag():
    """⚠️ Otherwise the warning is right once and then silently wrong for the
    rest of the session. A deletion leaves no 'edited' marker anywhere, so it
    has to clear the flag itself."""
    assert SRC.count("_ocr_kept['saved'] = False") == 2, (
        'both the inline edit and the delete path must mark the run dirty')
    edit_block = CODE.split("cues[idx]['edited'] = True", 1)[1][:200]
    assert "_ocr_kept['saved'] = False" in edit_block, 'inline edit must dirty it'
    del_block = CODE.split('gone = delete_cues(cues, idxs)', 1)[1][:200]
    assert "_ocr_kept['saved'] = False" in del_block, 'delete must dirty it'
