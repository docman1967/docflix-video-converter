#!/usr/bin/env python3
"""A manual edit must rescan the spelling highlight — in BOTH panes.

Tony, 2026-10-04: *"When I make manual corrections to a name, I have to select
Tools --> Highlight Spelling Errors to turn it off and do it again to turn it on
so it flags all the rest... I need the app to auto refresh the subs and at the
same time, keeping me on the cue I'm currently on."*

The 9/26 fix put a rescan in refresh_tree, but the two manual-edit paths never go
through it — they patch ONE row:
  * Subtitle Editor `save_edit` tests `idx in spell_error_indices`, a set nothing
    on that path rescans, so the edited row keeps its old verdict.
  * OCR review `_write_cue_text` repaints one row, but its scans are WHOLE-LIST
    (drop_recurring_words hides a word seen 3+ times as a probable name), so
    fixing two of five copies of a mangled name should light the other three.

⚠️ STRUCTURAL, like §6 of test_spell_highlight_live.py: both sites are closures
inside a live Tk window and cannot be driven headless. The behaviour itself is
Tony's click to confirm. Checked to FAIL against the pre-fix code (git HEAD,
2026-10-04) before being trusted.
"""
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = open(os.path.join(HERE, '..', 'modules', 'subtitle_editor.py'),
           encoding='utf-8').read()


def _body(name):
    """Source of the nested def `name`, up to the next def at its own indent."""
    m = re.search(rf'^( *)def {name}\(.*?$', SRC, re.M)
    assert m, f'{name} not found'
    indent = m.group(1)
    rest = SRC[m.end():]
    end = re.search(rf'^{indent}def \w', rest, re.M)
    return rest[:end.start() if end else len(rest)]


def test_editor_save_edit_rescans_and_keeps_place():
    body = _body('save_edit')
    assert re.search(r'if new_text and spell_scanned\[0\]:\s*\n\s*_yv = tree\.yview\(\)\[0\]'
                     r'\s*\n\s*refresh_tree\(cues\)', body), \
        'save_edit must rebuild (=rescan) when spelling highlight is on'
    assert 'tree.yview_moveto(_yv)' in body, 'must restore the scroll position'
    assert 'tree.selection_set(item)' in body, 'must keep the edited cue selected'


def test_ocr_write_cue_text_rescans_whole_list_in_place():
    body = _body('_write_cue_text')
    assert '_run_review_scans()' in body, 'manual OCR edit must rescan'
    assert re.search(r'for _it, _ci in list\(_row_cue\.items\(\)\):', body), \
        'must repaint every row, not just the edited one'
    # In place — a rebuild would recurse through _end_inline and lose his place.
    assert '_rebuild_cue_tree' not in re.sub(r'#.*', '', body)


if __name__ == '__main__':
    import pytest, sys
    sys.exit(pytest.main([__file__, '-q']))
