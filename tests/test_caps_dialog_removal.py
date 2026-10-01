#!/usr/bin/env python3
"""The Fix ALL CAPS dialogs must delete the WORD you picked, not an index.

⚠️⚠️ WHY THIS FILE EXISTS. Both caps dialogs did:

    app.custom_cap_words.pop(sel[0])      # LISTBOX POSITION, not the word

The listbox is populated ONCE when the dialog opens. The dialog is deliberately
non-modal and topmost ("Keep on top but don't grab — allows scrolling the
subtitle list"), so `app.custom_cap_words` can be mutated the entire time it
sits open — by Tools ▸ Dictionary & Names, by "Add as a name" on the editor
tree, by the OCR review pane's teach actions. Every one of those shifts the
later indices, and Remove then silently deleted a DIFFERENT word than the one
highlighted, with no way for Tony to notice.

⭐ Found 2026-10-01 while chasing a name ("Increase") that kept reappearing
after he deleted it. It was not the cause of that — the cause is still open —
but it is a real, silent data-loss bug in the same list, and it is the reason
this file exists.

⚠️ THESE ARE SOURCE-STRUCTURE TESTS plus a simulation. The handlers live in Tk
closures needing a root, a loaded subtitle and a live app object, so the shipped
`remove_word` cannot be called directly. What CAN be pinned is that the unsafe
idiom is gone from both copies and that the safe one behaves.
"""
import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FILES = {
    'subtitle_editor': ROOT / 'modules' / 'subtitle_editor.py',
    'batch_filter': ROOT / 'modules' / 'batch_filter.py',
}


# ── The unsafe idiom must be gone from BOTH copies ──────────────────────────

@pytest.mark.parametrize('name', sorted(FILES))
def test_no_live_pop_by_index_on_the_names_list(name):
    """⛔ THE REGRESSION. Checked on the SYNTAX TREE, not by grep — the text
    still appears in the docstring explaining what was wrong, and a text search
    cannot tell code from prose."""
    src = FILES[name].read_text()
    offenders = []
    for node in ast.walk(ast.parse(src)):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == 'pop'):
            seg = ast.get_source_segment(src, node) or ''
            if 'custom_cap_words' in seg:
                offenders.append((node.lineno, seg))
    assert not offenders, f'{name}: delete-by-index is back: {offenders}'


@pytest.mark.parametrize('name', sorted(FILES))
def test_removal_asks_the_widget_which_word(name):
    src = FILES[name].read_text()
    body = src.split('def remove_word():', 1)[1][:1400]
    assert 'word_list.get(sel[0])' in body, (
        'must read the WORD from the listbox, not trust the index')
    assert 'custom_cap_words.remove(' in body, 'must remove by value'


# ── Both dialogs must say they share one list ───────────────────────────────

@pytest.mark.parametrize('name', sorted(FILES))
def test_the_dialog_admits_it_is_the_same_list(name):
    """⭐ Three UIs onto ONE app.custom_cap_words under two different names, and
    nothing said so. Delete from one, re-add from the other, and it is
    indistinguishable from a broken delete — which is exactly what it looked
    like to Tony for two days."""
    src = FILES[name].read_text()
    assert 'same list as' in src, f'{name}: the shared-list warning is gone'
    assert 'Custom Names (saved across sessions)' not in src, (
        'the old title that hid the sharing is back')


# ── The behaviour the fix depends on ────────────────────────────────────────

def _safe_remove(words, listbox_rows, sel):
    """The shipped logic, mirrored minimally: take the WORD, remove by value."""
    word = listbox_rows[sel]
    try:
        words.remove(word)
    except ValueError:
        pass
    return words


def test_removes_the_right_word_when_the_list_shifted_underneath():
    """⭐⭐ THE ACTUAL BUG, simulated.

    The dialog opened showing [Alpha, Beta, Gamma]. While it stayed open,
    something else removed 'Alpha' from the list — Dictionary & Names, say.
    Tony then selects 'Gamma' (listbox row 2) and presses Remove.

    By index: pop(2) on [Beta, Gamma] is out of range or takes the wrong one.
    By value: 'Gamma' goes, which is what he pointed at.
    """
    rows = ['Alpha', 'Beta', 'Gamma']          # what the listbox still shows
    words = ['Beta', 'Gamma']                  # what the list actually holds now
    assert _safe_remove(list(words), rows, 2) == ['Beta']


def test_removing_an_already_gone_word_is_harmless():
    """If the word vanished from under us, the row still goes and nothing
    else is touched — never an exception, never a wrong deletion."""
    rows = ['Alpha', 'Beta']
    assert _safe_remove(['Beta'], rows, 0) == ['Beta']


def test_removes_only_one_and_only_the_named_one():
    words = ['Mercy', 'mercy', 'Cotton']
    assert _safe_remove(list(words), words, 1) == ['Mercy', 'Cotton']
