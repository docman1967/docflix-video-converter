"""
Italic / bold / underline in the Subtitle Editor.

Tony, 2026-08-10: *"There should be a way to edit the text for italics, bold,
etc."* The editor could already STRIP tags (the "Remove Tags" filter) but never
add them — you could take formatting away and never put it back.

`toggle_srt_tag()` is module-level and pure ON PURPOSE: its caller lives in a
nested closure inside the tree-edit handler, where a test can reach nothing.
Pulling it out is what surfaced a live NameError during the refactor (the
closure still referenced `lead`/`trail` after the whitespace handling moved).
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from modules.subtitle_editor import toggle_srt_tag        # noqa: E402


@pytest.mark.parametrize('tag', ['i', 'b', 'u'])
def test_wrap_then_unwrap_round_trips(tag):
    original = 'Come with me if you want to live.'
    wrapped = toggle_srt_tag(original, tag)
    assert wrapped == f'<{tag}>{original}</{tag}>'
    assert toggle_srt_tag(wrapped, tag) == original


def test_whitespace_stays_outside_the_tag():
    """⚠️ '<i> word </i>' renders with stray spaces inside the italics."""
    assert toggle_srt_tag('  spaced  ', 'i') == '  <i>spaced</i>  '
    assert toggle_srt_tag('\nline\n', 'i') == '\n<i>line</i>\n'


def test_tags_nest_rather_than_collide():
    """Bolding something already italic must not unwrap the italics."""
    assert toggle_srt_tag('<i>x</i>', 'b') == '<b><i>x</i></b>'
    assert toggle_srt_tag('<b><i>x</i></b>', 'b') == '<i>x</i>'


def test_partial_or_mismatched_tags_are_left_alone():
    """Only an exact wrap unwraps. A cue with an inline tag in the MIDDLE must
    be wrapped, not mangled — that text is still valid SRT."""
    assert toggle_srt_tag('say <i>this</i> now', 'i') == '<i>say <i>this</i> now</i>'
    # Already-malformed input wraps rather than being "repaired" — the result is
    # visibly wrong in the cue list, which is better than silently guessing at
    # what the user meant. Only an EXACT wrap unwraps.
    assert toggle_srt_tag('<i>unclosed', 'i') == '<i><i>unclosed</i>'


def test_empty_and_blank_are_no_ops():
    for blank in ('', '   ', '\n', None):
        assert toggle_srt_tag(blank, 'i') == blank


def test_the_editor_actually_binds_it():
    """The logic being right is useless if nothing calls it. Guards the
    context-menu entries and the Ctrl+I/B/U bindings."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(root, 'modules', 'subtitle_editor.py'),
               encoding='utf-8').read()
    assert 'toggle_srt_tag(raw, tag)' in src, 'closure no longer calls the helper'
    for label in ('Italic', 'Bold', 'Underline'):
        assert label in src, f'{label} missing from the context menu'
    assert "edit_entry.bind(f'<Control-{_k}>'" in src, 'Ctrl+I/B/U not bound'
    # ⚠️ tk.Text binds Ctrl+B/I/U to its own cursor-movement defaults; without
    # 'break' those fire too and move the insert point out from under the edit.
    assert "'break')[1]" in src, "the Ctrl binding no longer returns 'break'"


@pytest.mark.skipif(not __import__('shutil').which('xvfb-run'),
                    reason='needs xvfb-run')
def test_selection_is_replaced_not_deleted():
    """⚠️ THE 3.19.0 BUG, in a real Tk widget.

    'sel.first'/'sel.last' are LIVE index expressions, not positions. delete()
    removes the selection, so the next insert('sel.first', …) raises and the
    text is already gone. Tony hit it within ten minutes of shipping:
    *"it deletes the text completely instead of changing it."*

    The pure-function tests all passed, because the bug was never in the string
    handling — it was in the Tk index lifetime. This has to run against a real
    widget or it proves nothing.
    """
    import subprocess
    import textwrap
    prog = textwrap.dedent('''
        import sys, tkinter as tk
        sys.path.insert(0, %r)
        from modules.subtitle_editor import toggle_srt_tag
        root = tk.Tk(); root.withdraw()
        t = tk.Text(root); t.insert('1.0', 'Come with me if you want to live.')
        t.tag_add('sel', '1.5', '1.9')                 # "with"
        start, end = t.index('sel.first'), t.index('sel.last')   # the fix
        raw = t.get(start, end)
        new = toggle_srt_tag(raw, 'i')
        t.delete(start, end); t.insert(start, new)
        print(t.get('1.0', 'end-1c'))
    ''') % os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    r = subprocess.run(['xvfb-run', '-a', sys.executable, '-c', prog],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == 'Come <i>with</i> me if you want to live.', \
        f'selection was not replaced correctly: {r.stdout!r}'


def test_indices_are_frozen_before_the_edit():
    """Source guard for the same bug — the fix is one call that is easy to
    'simplify' away later."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(root, 'modules', 'subtitle_editor.py'),
               encoding='utf-8').read()
    block = src[src.index('def _toggle_format('):]
    block = block[:block.index('edit_ctx.add_command(label="Italic')]
    assert 'start = edit_entry.index(start)' in block, \
        "sel.first/sel.last are no longer frozen before delete() — the text " \
        "will vanish again"
    assert 'except Exception:\n                    pass' not in block, \
        'a silent except is back; that is what hid this bug'


# ── multi-cue formatting (Tony, 2026-08-10) ─────────────────────────────────

def _group_apply(texts, tag):
    """Mirror of format_selected()'s decision logic, which lives in a closure."""
    from modules.subtitle_editor import has_srt_tag
    idx = [i for i, t in enumerate(texts) if t.strip()]
    all_wrapped = all(has_srt_tag(texts[i], tag) for i in idx)
    out = list(texts)
    for i in idx:
        if all_wrapped or not has_srt_tag(out[i], tag):
            out[i] = toggle_srt_tag(out[i], tag)
    return out


def test_group_wraps_then_unwraps():
    plain = ['One', 'Two', 'Three']
    italic = _group_apply(plain, 'i')
    assert italic == ['<i>One</i>', '<i>Two</i>', '<i>Three</i>']
    assert _group_apply(italic, 'i') == plain


def test_mixed_selection_converges_instead_of_inverting():
    """⚠️ The design decision. Per-cue toggling across a mixed selection would
    flip each one and leave it MORE mixed — the opposite of "make these all
    italic". Group semantics: wrap the stragglers, leave the rest."""
    mixed = ['<i>One</i>', 'Two', '<i>Three</i>']
    once = _group_apply(mixed, 'i')
    assert once == ['<i>One</i>', '<i>Two</i>', '<i>Three</i>'], \
        'a mixed selection must converge to all-wrapped'
    assert _group_apply(once, 'i') == ['One', 'Two', 'Three'], \
        'a fully-wrapped selection must then unwrap'


def test_blank_cues_are_skipped_not_tagged():
    """An empty cue must not become '<i></i>'."""
    assert _group_apply(['Real', '   ', 'Also real'], 'i') == \
        ['<i>Real</i>', '   ', '<i>Also real</i>']


def test_has_srt_tag_requires_an_exact_wrap():
    from modules.subtitle_editor import has_srt_tag
    assert has_srt_tag('<i>x</i>', 'i') is True
    assert has_srt_tag('  <i>x</i>  ', 'i') is True
    assert has_srt_tag('say <i>x</i> now', 'i') is False
    assert has_srt_tag('<i>x', 'i') is False
    assert has_srt_tag('', 'i') is False


def test_batch_is_one_undo_and_restores_the_selection():
    """Source guards. push_undo() must be called ONCE outside the loop, or
    Ctrl+Z takes back a single cue instead of the operation; and refresh_tree
    rebuilds the rows, so the selection has to be put back or the next
    shortcut press acts on nothing."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(root, 'modules', 'subtitle_editor.py'),
               encoding='utf-8').read()
    block = src[src.index('def format_selected('):]
    block = block[:block.index('def split_selected(')]
    # Drop the docstring — it explains the batching in prose and mentions
    # push_undo() by name, so counting the raw block matches our own comment.
    # (Third time today a source-level test caught its own documentation.)
    block = block.split('"""', 2)[2]
    assert block.count('push_undo()') == 1, 'undo must cover the whole batch'
    assert block.index('push_undo()') < block.index('for i in indices:'), \
        'push_undo() must run before the loop, not inside it'
    assert 'tree.selection_add' in block, 'selection not restored after refresh'
    # And it must actually be reachable from the tree.
    assert "format_selected('i')" in src and "format_selected(t), 'break')" in src, \
        'multi-cue formatting is not wired to the menu and/or Ctrl shortcuts'


def test_no_stale_names_from_the_refactor():
    """The whitespace handling moved into toggle_srt_tag; the closure must not
    still reference lead/trail. This shipped broken for about ninety seconds."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(root, 'modules', 'subtitle_editor.py'),
               encoding='utf-8').read()
    block = src[src.index('def _toggle_format('):]
    block = block[:block.index('edit_ctx.add_command(label="Italic')]
    assert 'lead' not in block and 'trail' not in block, \
        'closure still references names that moved into toggle_srt_tag'
