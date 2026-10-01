#!/usr/bin/env python3
"""Offering to APPLY a proposed correction, not just flag it.

⚠️⚠️ WHY THIS FILE EXISTS — Tony, 2026-09-30: *"When a word comes up that the
app thinks is misspelled or not capitalized, it would be nice to have an option
to change it automatically."*

⭐⭐ THE FIND THAT MADE IT CHEAP: the app ALREADY computed the correction and
threw it away. `run_spell_highlight_scan` builds `{idx: [(bad, good), ...]}`
whenever `name_fixes` is a dict — and the OCR review pane called it with the
default `None`, so a cue whose ONLY fault was a miscased name produced no entry
and never went pink. Tony asked for that on 2026-09-26 (*"Yes I want names to
be lit as well"*); it worked in the Subtitle Editor and was silently dead in
the review pane for five days. The first test below is that wiring.

⚠️⚠️ THIS CROSSES A DELIBERATE RULE AND THE BOUNDARY MATTERS. `flag_ocr_cue`:
*"PROPOSES, NEVER FILTERS."* `_teach_word`: *"he only said 'this word is fine',
which is not a licence to touch his text."* The pre-existing carve-out is the
Spell Check dialog, which MAY rewrite because the user CHOSE the correction. A
menu item that NAMES BOTH WORDS is that same consent. ⛔ An apply-to-all sweep
is NOT, and has not been asked for — `test_no_apply_all_sweep_exists` guards it.
"""
import re
from pathlib import Path

import pytest

from modules.spell_checker import run_spell_highlight_scan
from modules.subtitle_editor import apply_word_fix
import types

SRC = (Path(__file__).resolve().parent.parent
       / 'modules' / 'subtitle_editor.py').read_text()
CODE = '\n'.join(l for l in SRC.splitlines() if not l.lstrip().startswith('#'))


# ── The wiring that was dead ────────────────────────────────────────────────

def test_the_pane_now_asks_for_name_fixes():
    """⛔ THE REGRESSION. Passing no name_fixes (the default None) makes the
    scan compute the pairs and silently drop them."""
    assert 'name_fixes=review_names' in CODE, (
        'the review pane must pass a dict, or wrong-case names vanish again')
    assert 'run_spell_highlight_scan(app, mon, cues, set())' not in CODE, (
        'the old discarding call is back')


def test_name_fixes_are_produced_when_asked_for():
    """The underlying scan, exercised for real."""
    app = types.SimpleNamespace(custom_cap_words=['Hirst'],
                                custom_spell_words=['Hirst'])
    nf = {}
    run_spell_highlight_scan(app, None, [{'text': 'I saw hirst at the door.'}],
                             set(), name_fixes=nf, quiet=True)
    assert nf == {0: [('hirst', 'Hirst')]}


def test_name_fixes_are_absent_when_not_asked_for():
    """Pins WHY the bug existed: same input, no dict, no findings anywhere."""
    app = types.SimpleNamespace(custom_cap_words=['Hirst'],
                                custom_spell_words=['Hirst'])
    raw = run_spell_highlight_scan(app, None, [{'text': 'I saw hirst at the door.'}],
                                   set(), quiet=True)
    assert raw == {}


# ── The substitution rules ──────────────────────────────────────────────────

def _apply(bad, good, old):
    """Calls the REAL shared helper.

    ⚠️ This used to MIRROR the regex, which made the test a second
    implementation that could pass while the shipped one drifted — the exact
    failure add_user_word() documents. Fixed 2026-10-01 when the substitution
    was extracted to module level for the Subtitle Editor to share.
    """
    new, _why = apply_word_fix(old, bad, good)
    return new


@pytest.mark.parametrize('bad,good,before,after', [
    ('hirst', 'Hirst', 'I saw hirst at the door.', 'I saw Hirst at the door.'),
    ('I,000', '1,000', 'dive at least I,000, they said',
     'dive at least 1,000, they said'),          # punctuation inside the token
    ('Lome', 'Lorne', 'Good call. Lome out.', 'Good call. Lorne out.'),
])
def test_applies_the_named_change(bad, good, before, after):
    assert _apply(bad, good, before) == after


def test_whole_words_only():
    """⚠️ 'hirst' must not match inside 'Hirstwood'."""
    out = _apply('hirst', 'Hirst', 'Walk through Hirstwood, not hirst.')
    assert out == 'Walk through Hirstwood, not Hirst.'


def test_case_sensitive():
    """⚠️ A case-insensitive replace would rewrite the ALREADY-CORRECT
    occurrences, which is the whole thing a caps fix is trying to preserve."""
    assert _apply('hirst', 'Hirst', 'Hirst met hirst.') == 'Hirst met Hirst.'


def test_no_match_is_a_no_op():
    assert _apply('hirst', 'Hirst', 'nothing to see') == 'nothing to see'


@pytest.mark.parametrize('bad,good,text', [
    ('hirst', 'Hirst', 'I saw hirst at the door.'),
    ('I,000', '1,000', 'dive at least I,000, they said'),
    ('Lome', 'Lorne', 'Good call. Lome out.'),
])
def test_word_count_never_changes(bad, good, text):
    """⛔⛔ `balance_lines` silently DELETED WORDS and was caught only by this.
    _apply_fix refuses the edit outright if the count would move."""
    assert len(_apply(bad, good, text).split()) == len(text.split())


# ── The safety properties, read off the source ──────────────────────────────

def test_apply_is_undoable_and_marks_the_cue():
    body = CODE.split('def _apply_fix(', 1)[1].split('\n                def ', 1)[0]
    assert 'filter_undo[0]' in body, 'must snapshot for undo'
    assert 'undo_filters_btn' in body, 'must enable the Undo button'
    assert '_write_cue_text' in body, (
        'must go through _write_cue_text — that marks edited AND clears '
        "_ocr_kept['saved'], or the unsaved-close guard goes stale")
    assert '_rebuild_cue_tree' in body, 'must re-scan or the flag persists'


def test_apply_refuses_to_change_the_word_count():
    """⚠️ The guard lives in the SHARED helper, not in either pane's handler —
    moved there 2026-10-01 so the editor inherits it rather than needing its
    own copy. Behaviour is covered by test_refuses_a_count_changing_substitution;
    this pins the location, so it cannot be quietly dropped from one caller."""
    body = CODE.split('def apply_word_fix(', 1)[1].split('\ndef ', 1)[0]
    assert 'len(new.split()) != len(text.split())' in body
    # and neither pane may bypass it
    ocr = CODE.split('def _apply_fix(', 1)[1].split('\n                def ', 1)[0]
    assert 'apply_word_fix(' in ocr, 'the OCR pane must delegate, not reimplement'


def test_the_menu_names_both_words():
    """⚠️ The naming IS the consent. A generic 'Fix this' would not be."""
    assert 'Change  "{b}"  →  "{g}"' in SRC


def test_no_apply_all_sweep_exists():
    """⛔ Tony asked for a per-word option. A bulk sweep over cues he has not
    looked at is the thing 'propose, never apply' exists to prevent."""
    for banned in ('apply_all_fixes', 'fix_all_cues', 'Apply All Fixes'):
        assert banned not in SRC, f'{banned} — not asked for, do not add'


# ── Both panes share ONE substitution ───────────────────────────────────────

def test_the_substitution_is_module_level_and_shared():
    """⚠️⚠️ Tony, 2026-10-01: *"We should have #2 in the subtitle editor as well
    as the OCR preview."* Two call sites, one rule. add_user_word() carries the
    scar from the alternative: *"They were written separately first, which is
    how the same word ends up known in one pane and unknown in the other."*"""
    assert 'def apply_word_fix(' in SRC, 'the shared helper is gone'
    assert CODE.count('apply_word_fix(') >= 3, (
        'expected the definition plus BOTH callers (OCR pane + editor)')
    # ⛔ Neither pane may carry its own copy of the regex.
    assert CODE.count("re.sub(r'(?<!\\w)'") <= 1, (
        'a second inline substitution has appeared — use apply_word_fix')


def test_the_editor_applies_through_the_shared_helper():
    body = CODE.split('def _apply_fix_from_tree(', 1)[1].split('\n        def ', 1)[0]
    assert 'apply_word_fix(' in body
    assert 'push_undo()' in body, 'must be undoable in the editor too'
    assert 'refresh_tree(cues)' in body, (
        're-paint, which also re-runs rescan_spell_state so the flag clears')


def test_refuses_a_count_changing_substitution():
    """⛔⛔ The guard that balance_lines needed."""
    out, why = apply_word_fix('say hello there', 'hello', 'hello there')
    assert out == 'say hello there' and 'word count' in why
