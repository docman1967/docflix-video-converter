#!/usr/bin/env python3
"""Startup work must live in the path the app actually runs.

⛔⛔ WHY THIS FILE EXISTS, and it is the sharpest lesson of the week.

On 2026-09-29 a names-replay was added so hand-taught names survive a restart.
It was put in `modules/preferences.py`. That module is imported by NOTHING —
its own header says *"in practice superseded by VideoConverterApp.
save_preferences (video_converter.py)"* — so the feature was dead on arrival
and stayed dead until 2026-10-01.

⭐⭐ AND THE UNIT TEST PASSED THE WHOLE TIME. `test_taught_name_survives_a_
names_db_reload` called `add_user_name()` directly and proved the FUNCTION
worked. It never asked whether the APP called it. A test that exercises your
code in isolation proves the code works; it says nothing about whether anything
reaches it.

⚠️ So these tests check WIRING, not behaviour — they are the thing that was
missing. ⛔ Do not "simplify" them into function tests; that is precisely the
hole they exist to plug.
"""
import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LIVE = (ROOT / 'video_converter.py').read_text()
DEAD = (ROOT / 'modules' / 'preferences.py').read_text()


def _live_load_prefs():
    """The body of VideoConverterApp.load_preferences — the real one, with
    COMMENTS STRIPPED.

    ⚠️⚠️ The stripping is load-bearing, and a negative control proved it. The
    first version of this file searched raw source — and with the replay
    DELETED it still passed, because `add_user_name` appears in the comment
    explaining the bug. A test that cannot tell code from prose is not a test.
    Same trap as grepping for `pop(sel[0])` in test_caps_dialog_removal.
    """
    tree = ast.parse(LIVE)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == 'load_preferences':
            seg = ast.get_source_segment(LIVE, node)
            if seg and 'self._prefs_path()' in seg:
                return '\n'.join(l for l in seg.splitlines()
                                 if not l.lstrip().startswith('#'))
    pytest.fail('VideoConverterApp.load_preferences not found')


# ── The module that looks like it runs and does not ─────────────────────────

def test_modules_preferences_is_still_unimported():
    """If this ever starts failing, someone wired it up and the warning inside
    it needs removing — and these tests need rethinking."""
    hits = []
    for p in ROOT.rglob('*.py'):
        if 'tests' in p.parts or p.name == 'preferences.py':
            continue
        t = p.read_text()
        if 'from .preferences import' in t or 'from modules.preferences import' in t:
            hits.append(p.name)
    assert not hits, f'modules/preferences.py is now imported by {hits}'


def test_the_dead_module_warns_about_itself():
    assert 'DO NOT PUT STARTUP WORK IN THIS MODULE' in DEAD, (
        'the warning that explains why this module never runs has been removed')


def test_no_startup_side_effects_hide_in_the_dead_module():
    """⛔ THE REGRESSION. add_user_name() sat here for two days doing nothing."""
    assert 'add_user_name' not in DEAD, (
        'startup work is back in the module nothing imports')


# ── The replay must be in the path that runs ────────────────────────────────

def test_the_names_replay_is_in_the_live_load_path():
    body = _live_load_prefs()
    assert 'add_user_name' in body, (
        'hand-taught names are not replayed on startup — "Lorne is a name" '
        'will be forgotten on every restart, exactly as it was 09-29 to 10-01')


def test_the_replay_runs_after_the_standalone_merge():
    """⚠️ Names arriving from the "Open with" prefs file are appended to
    custom_cap_words by that merge. Replay before it and those are missed."""
    body = _live_load_prefs()
    assert body.index('alt_list') < body.index('add_user_name'), (
        'the replay must come after the standalone-prefs merge')


# ── The highlight preferences, in both directions ───────────────────────────

@pytest.mark.parametrize('key', ['sub_caps_highlight', 'sub_spell_highlight'])
def test_highlight_modes_are_saved_and_loaded_by_the_live_code(key):
    """A preference written but never read — or read but never written — is a
    setting that silently forgets itself."""
    assert f"'{key}':" in LIVE, f'{key} is never SAVED by the live path'
    assert f"prefs.get('{key}'" in LIVE, f'{key} is never LOADED by the live path'


def test_the_editor_seeds_its_toggles_from_the_preference():
    src = (ROOT / 'modules' / 'subtitle_editor.py').read_text()
    assert "getattr(app, 'sub_caps_highlight', False)" in src
    assert "getattr(app, 'sub_spell_highlight', False)" in src


def test_loading_a_file_keeps_the_MODE_but_clears_the_RESULTS():
    """⭐ The distinction the whole feature rests on. Tony's 2026-08-06 note
    gave two reasons for resetting: "you never asked for them" (now superseded
    — he IS asking) and "stale row indices pointing into a different file"
    (still true). Mode persists; results do not."""
    src = (ROOT / 'modules' / 'subtitle_editor.py').read_text()
    block = src.split('            spell_error_indices.clear()', 1)[0][-900:]
    assert "caps_highlight_on[0] = bool(getattr(app, 'sub_caps_highlight'" in block, (
        'the caps MODE is being reset to False on load instead of honoured')
    # the results still get cleared
    for must in ('spell_error_indices.clear()', 'spell_error_words.clear()',
                 'spell_name_fixes.clear()'):
        assert must in src, f'{must} — results must still be cleared on load'
