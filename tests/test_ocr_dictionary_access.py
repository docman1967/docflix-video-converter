#!/usr/bin/env python3
"""The OCR review pane must reach Dictionary & Names without leaving it.

⚠️ WHY — Tony, 2026-10-02: *"I was using the OCR and needed to add a name but
couldn't through the OCR, so I opened a different subtitle so I could have
access to the Dictionary and Names widget... made me realize that I need to
have access to that widget in the OCR menu."*

He had to open an UNRELATED subtitle in another window to edit the list the OCR
pane depends on. ⭐ Third time this exact dead-end has been found in this pane
(right-click teaching 09-19, the ALL-CAPS group 09-20, now this): a judgement is
shown with no way to act on it from where it is shown.
"""
import ast
from pathlib import Path

SRC = (Path(__file__).resolve().parent.parent / 'modules' / 'subtitle_editor.py').read_text()
CODE = '\n'.join(l for l in SRC.splitlines() if not l.lstrip().startswith('#'))


def test_the_pane_can_open_the_dictionary_editor():
    assert 'def _show_dict_editor():' in CODE
    body = CODE.split('def _show_dict_editor():', 1)[1].split('\n                ttk.Button', 1)[0]
    assert 'show_dictionary_editor(app, mon' in body, 'must parent to the OCR window'
    assert 'on_change=_rebuild_cue_tree' in body, (
        'must re-scan after an edit, or the pane keeps showing a judgement the '
        'dictionary no longer supports')


def test_it_is_reachable_from_BOTH_the_button_row_and_the_right_click():
    """⚠️ Two routes on purpose: the button for 'I want the whole list', the menu
    for 'I am looking at this word right now'."""
    assert CODE.count('command=_show_dict_editor') == 2, (
        'expected the button AND the right-click entry')
    assert 'text="Dictionary & Names"' in SRC, 'button missing'
    assert 'label="Dictionary & Names…"' in SRC, 'menu entry missing'


def test_the_menu_entry_is_offered_even_when_the_cue_teaches_nothing():
    """⛔ THE DEAD END. It must NOT sit inside the `if words/caps/ocr` block —
    the whole point is reaching the full list from a row with nothing flagged."""
    menu_fn = CODE.split('def _popup_dict_menu(event):', 1)[1].split('\n                _flag_cue', 1)[0]
    i_entry = menu_fn.index('label="Dictionary & Names…"')
    i_empty = menu_fn.index('No unknown words in this cue')
    assert i_entry > i_empty, (
        'the dictionary entry must come after the empty-case branch, '
        'i.e. unconditionally')


def test_the_closure_actually_resolves():
    """⚠️ _show_dict_editor is USED ~800 lines before it is DEFINED. That is fine
    in a closure — but only if both sit inside the same enclosing function, and
    this file is 8,000 lines of nested scopes. Checked, not assumed."""
    tree = ast.parse(SRC)
    def chain(lineno):
        return [n.name for n in sorted(
            (n for n in ast.walk(tree)
             if isinstance(n, ast.FunctionDef)
             and n.lineno <= lineno <= (n.end_lineno or n.lineno)),
            key=lambda n: n.lineno)]
    defn = next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == '_show_dict_editor')
    uses = [n.lineno for n in ast.walk(tree)
            if isinstance(n, ast.Name) and n.id == '_show_dict_editor'
            and isinstance(n.ctx, ast.Load)]
    assert uses, 'nothing calls it'
    owner = chain(defn.lineno)[-2]          # the function it is defined inside
    for u in uses:
        assert owner in chain(u), f'use at {u} is outside {owner} — NameError at runtime'
