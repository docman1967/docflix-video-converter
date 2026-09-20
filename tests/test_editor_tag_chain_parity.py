#!/usr/bin/env python3
"""The editor tree's priority chain exists TWICE. Keep the copies honest.

⚠️⚠️ WHY THIS FILE EXISTS — this has already gone wrong once, and the code
says so. `refresh_tree()` decides a row's colour when the whole list is
repainted; `save_edit()` decides it again after an inline edit. They are two
hand-written copies of one decision, and subtitle_editor.py carries this note
above the second one:

    "This one used to check only (MODIFIED, HI, TAGS, LONG) — so editing a cue
     stripped its SPELL or CAPS highlight even when the row still qualified,
     and it came back the moment anything triggered a full refresh. A highlight
     that disappears on edit and reappears later reads as a flaky feature, not
     a stale cache."

That is a silent, intermittent, user-visible bug caused purely by the
duplication. Two tags were added to both chains on 2026-09-20 (midcap, colon)
and the Note column with them, which doubles the chance of the next drift.

⚠️ This is a SOURCE-STRUCTURE test, which is unusual and deliberate. Both
chains live inside a 7,000-line closure that needs a Tk root, a loaded file
and a live app object to reach, so there is no honest way to exercise them in
a unit test. The duplication is the defect; until the two are merged into one
function, checking that they mention the same tags is the only guard available.

⭐ THE REAL FIX, when someone has the appetite: extract the chain into a module
-level `pick_row_tag(...)` that both call. Then delete this file.
"""
import ast
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), 'modules', 'subtitle_editor.py')
sys.path.insert(0, os.path.dirname(HERE))

TAG_RE = re.compile(r'\bTAG_[A-Z]+\b')
# SEARCH is legitimately absent from save_edit — the edit path has no access to
# the live search set, and the code says so. MODIFIED/HI/TAGS/LONG come out of
# `ctags`, the rest are computed.
EXPECTED_ONLY_IN_REFRESH = {'TAG_SEARCH'}


def _func_source(name):
    """Return the source of the innermost function called *name*."""
    src = open(SRC, encoding='utf8').read()
    tree = ast.parse(src)
    lines = src.split('\n')
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return '\n'.join(lines[node.lineno - 1:node.end_lineno])
    raise AssertionError(f'{name}() not found in subtitle_editor.py')


def _tags_assigned(body):
    """Tag constants that appear as `row_tag = TAG_X` in *body*."""
    return set(TAG_RE.findall(
        '\n'.join(l for l in body.split('\n') if 'row_tag' in l)))


def test_both_chains_assign_the_same_tags():
    """⚠️ THE TEST. If a tag is painted on a full repaint but not after an
    inline edit, the highlight vanishes when you edit the cue and returns on
    the next refresh — flaky, and blamed on the detector rather than on this."""
    refresh = _tags_assigned(_func_source('refresh_tree'))
    save = _tags_assigned(_func_source('save_edit'))
    only_refresh = refresh - save
    only_save = save - refresh
    assert only_refresh <= EXPECTED_ONLY_IN_REFRESH, (
        f'painted on repaint but NOT after an edit: {sorted(only_refresh)} — '
        'the highlight will disappear when the cue is edited')
    assert not only_save, (
        f'painted after an edit but NOT on repaint: {sorted(only_save)} — '
        'the highlight will appear then vanish on the next refresh')


def test_the_new_tags_are_in_both():
    """Pins the 2026-09-20 port specifically — these are the ones just added."""
    refresh = _tags_assigned(_func_source('refresh_tree'))
    save = _tags_assigned(_func_source('save_edit'))
    for tag in ('TAG_MIDCAP', 'TAG_COLON'):
        assert tag in refresh, f'{tag} missing from refresh_tree'
        assert tag in save, f'{tag} missing from save_edit'


def test_both_paths_write_the_note_column():
    """⚠️ A stale Note is worse than none. Fixing `beheves` must clear
    "sp: beheves", not leave it contradicting the text on screen."""
    assert '_row_note(' in _func_source('refresh_tree'), \
        'refresh_tree does not write the Note column'
    assert '_row_note(' in _func_source('save_edit'), \
        'save_edit does not update the Note column after an edit'


def test_every_tag_in_the_chain_has_a_colour():
    """A tag with no tag_configure() is an invisible highlight — the row is
    'flagged' and looks identical to an unflagged one."""
    src = open(SRC, encoding='utf8').read()
    configured = set()
    for m in re.finditer(r'tree\.tag_configure\((TAG_[A-Z]+)', src):
        configured.add(m.group(1))
    used = _tags_assigned(_func_source('refresh_tree'))
    missing = used - configured
    assert not missing, f'no tag_configure() for: {sorted(missing)}'


def test_music_was_NOT_given_its_own_tag():
    """⛔ Tony, 2026-09-20: "I want to keep the music notes highlight just like
    it is. No changes to it." A ♪ cue is caught by _classify_cue's HI rule and
    shows as TAG_HI, same as [SIGHS]. Arthur proposed splitting it out; Tony
    declined — he has a working procedure built on the current behaviour.
    This test is the do-not-re-pitch, made executable."""
    src = open(SRC, encoding='utf8').read()
    assert 'TAG_MUSIC' not in src, \
        'a music tag was added to the editor tree — Tony declined this'
    classify = _func_source('_classify_cue')
    assert '♪' in classify and 'TAG_HI' in classify, \
        '_classify_cue no longer routes ♪ to TAG_HI — that behaviour is pinned'
