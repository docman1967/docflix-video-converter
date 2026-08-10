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
