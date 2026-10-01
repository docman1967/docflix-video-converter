#!/usr/bin/env python3
"""Find must advance, and the Case checkbox must bind every button.

⚠️⚠️ WHY THIS FILE EXISTS — Tony, 2026-10-01:

  1. *"Find doesn't skip to the next instance. So if you search for a word and
     the first one you find doesn't need to be changed, hitting find again won't
     continue the search. If you click replace, it will move to the next
     instance."*
  2. *"we need to have a checkbox for case sensitive finds."*

⭐ HIS SECOND SENTENCE WAS THE DIAGNOSIS. Replace advanced and Find did not,
because `do_replace_one` anchored on `tree.selection()` and `do_find` always
jumped to `matches[0]` — it recomputed the whole match list every press and
never looked at where you already were.

⚠️ Case-sensitivity was hardcoded OFF in SIX places: `re.IGNORECASE` in three
regex branches and `.lower()` in three literal ones. They are now one
`_cue_matches()` / `_find_flags()` pair, so the checkbox cannot be honoured by
Find and ignored by Replace All — the failure `add_user_word` documents.

⚠️ The handlers live in a Tk closure needing a root, a loaded file and a live
app, so the shipped functions cannot be called directly. The pure decisions are
mirrored and tested; the wiring is checked against the source.
"""
from pathlib import Path

import pytest

SRC = (Path(__file__).resolve().parent.parent
       / 'modules' / 'subtitle_editor.py').read_text()
CODE = '\n'.join(l for l in SRC.splitlines() if not l.lstrip().startswith('#'))
FIND = CODE.split('def do_find():', 1)[1].split('\n        def ', 1)[0]


# ── Find advances ───────────────────────────────────────────────────────────

def _next_target(matches, cur, wrap):
    """The shipped decision, mirrored."""
    later = [i for i in matches if i > cur]
    if later:
        return later[0]
    return matches[0] if wrap else matches[-1]


def test_find_walks_forward_through_every_match():
    """⛔ THE REGRESSION. Before the fix every press returned 2."""
    matches, cur, seen = [2, 5, 9], -1, []
    for _ in range(3):
        cur = _next_target(matches, cur, wrap=False)
        seen.append(cur)
    assert seen == [2, 5, 9]


def test_find_stops_at_the_last_match_without_wrap():
    assert _next_target([2, 5, 9], 9, wrap=False) == 9


def test_find_wraps_when_asked():
    assert _next_target([2, 5, 9], 9, wrap=True) == 2


def test_find_starts_at_the_top_when_nothing_is_selected():
    assert _next_target([2, 5, 9], -1, wrap=False) == 2


# ── The wiring that makes advancing possible ────────────────────────────────

def test_find_anchors_on_the_current_selection():
    assert 'tree.selection()' in FIND, (
        'do_find must look at where we already are, like do_replace_one does')
    assert 'matches[0]' not in FIND or 'wrap_around.get()' in FIND, (
        'jumping unconditionally to matches[0] is the bug')


def test_the_selection_is_read_before_the_tree_is_repainted():
    """⚠️ refresh_tree() clears the selection. Read it after, and 'next after
    here' has nothing to anchor to — the bug would come back silently."""
    sel_at = FIND.index('tree.selection()')
    refresh_at = FIND.index('refresh_tree(cues, search_indices=matches)')
    assert sel_at < refresh_at, 'selection must be captured before the repaint'


# ── Case sensitivity ────────────────────────────────────────────────────────

def _matches(text, term, regex, case):
    import re
    flags = 0 if case else re.IGNORECASE
    if regex:
        return re.search(term, text, flags) is not None
    return (term in text) if case else (term.lower() in text.lower())


CUES = ['The Mentalist is on.', 'the mentalist is lowercase.', 'MENTALIST shouting.']


@pytest.mark.parametrize('regex,term,expected', [
    (False, 'Mentalist', [0]),
    (True, 'ment.list', [1]),
])
def test_case_sensitive_narrows_the_match(regex, term, expected):
    assert [i for i, c in enumerate(CUES)
            if _matches(c, term, regex, True)] == expected


@pytest.mark.parametrize('regex,term', [(False, 'Mentalist'), (True, 'ment.list')])
def test_case_insensitive_is_still_the_default_behaviour(regex, term):
    assert [i for i, c in enumerate(CUES)
            if _matches(c, term, regex, False)] == [0, 1, 2]


def test_the_checkbox_exists_and_defaults_off():
    assert "case_sensitive = tk.BooleanVar(value=False)" in CODE
    assert 'text="Case"' in SRC, 'no Case checkbox in the toolbar'


def test_every_search_path_goes_through_the_shared_rule():
    """⚠️⚠️ Six inline copies became one. If a branch starts deciding case for
    itself again, the checkbox silently stops applying to that button."""
    assert '_find_flags()' in CODE and '_cue_matches(' in CODE
    rep = CODE.split('def do_replace_one():', 1)[1].split('\n        def ', 1)[0]
    alls = CODE.split('def do_replace_all():', 1)[1].split('\n        def ', 1)[0]
    # ⚠️ Scoped to THESE THREE functions, not the whole module — re.IGNORECASE
    # is used legitimately elsewhere (a different replace at ~5313, progress
    # parsing at ~7041). A module-wide count was the first version of this test
    # and it failed on innocent code, which is a test bug, not a code bug.
    for name, body in (('do_find', FIND), ('replace_one', rep),
                       ('replace_all', alls)):
        assert 're.IGNORECASE' not in body, (
            f'{name} hardcodes IGNORECASE instead of asking _find_flags()')
    for name, body in (('replace_one', rep), ('replace_all', alls)):
        assert 'case_sensitive.get()' in body or '_find_flags()' in body, (
            f'{name} ignores the Case checkbox')
