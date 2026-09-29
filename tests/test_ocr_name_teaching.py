#!/usr/bin/env python3
"""Teaching a name must silence the OCR suspect flag. It used to be impossible.

⚠️⚠️ WHY THIS FILE EXISTS — Tony, 2026-09-29, with a screenshot:

    Note column:   Lorne -> Lome?
    Right-click:   "No unknown words in this cue"   (greyed out)

Both statements were true and the combination was a dead end. `Lorne` is a
real word to the spell checker and is not ALL CAPS, so BOTH menu groups were
empty — and the third detector, `check_confusions`, had no menu group at all.
His words: *"It gives me what the word might be, but there is nothing I can do
with it"* and *"Lorne is a name and I should be able to add it to the names
list."*

⭐ THE SHAPE, and it is why these tests are worth keeping: the OCR review pane
has THREE detectors feeding one Note column, and the right-click menu was built
from TWO of them. The ALL CAPS group was added 2026-09-20 to fix this exact
dead end — with the exact same "No unknown words in this cue" symptom — and the
third door was never built. A fourth detector would break it again, so these
tests pin the behaviour rather than the implementation.

⚠️ The second half matters as much as the first: teaching the name had to reach
the list `check_confusions` ACTUALLY CONSULTS. `_is_name()` reads the names DB,
NOT `custom_cap_words`, so an "Add as a name" that only wrote to the latter
would look right and change nothing.
"""
import types
import pytest

from modules.subtitle_editor import (ocr_suspect_words, flag_ocr_cue,
                                     add_user_word)
from modules import subtitle_filters as sf


CUE = {'text': 'Good call. Lorne out.'}


@pytest.fixture(autouse=True)
def _clean_names():
    """Snapshot and restore the module-level name sets.

    ⚠️ add_user_name() mutates process globals by design — that is the whole
    point of it — so without this a test that teaches "Lorne" silently makes
    every later test's "is it flagged?" assertion pass for the wrong reason.
    """
    users, db = set(sf._user_names), set(sf._names_db)
    yield
    sf._user_names.clear()
    sf._user_names.update(users)
    sf._names_db.clear()
    sf._names_db.update(db)


def _app():
    return types.SimpleNamespace(custom_cap_words=[], custom_spell_words=[],
                                 save_preferences=lambda: None)


# ── The bug as Tony reported it ─────────────────────────────────────────────

def test_lorne_is_flagged_to_begin_with():
    """The reproduction. If this stops failing-by-default the test is moot."""
    tag, reason = flag_ocr_cue(CUE)
    assert tag == 'flag_ocr'
    assert reason == 'Lorne -> Lome?'


def test_the_menu_can_see_the_flagged_word():
    """⭐ THE ACTUAL BUG: the note named a word the menu could not offer."""
    assert [w for w, _ in ocr_suspect_words(CUE['text'])] == ['Lorne']


def test_adding_as_a_name_clears_the_flag():
    """End to end through the real menu action."""
    app = _app()
    assert flag_ocr_cue(CUE)[0] == 'flag_ocr'
    add_user_word(app, 'Lorne', as_name=True)
    assert flag_ocr_cue(CUE) == (None, '')


def test_add_as_name_reaches_the_names_db_not_just_cap_words():
    """⚠️ THE HALF THAT WOULD HAVE SHIPPED BROKEN.

    custom_cap_words is what the spell checker reads; the names DB is what
    check_confusions reads. Writing only the first looks correct and silences
    nothing.
    """
    app = _app()
    add_user_word(app, 'Lorne', as_name=True)
    assert 'Lorne' in app.custom_cap_words       # spell side
    assert 'Lorne' in sf.get_names_db()          # OCR-suspect side


# ── Per-episode ignore ──────────────────────────────────────────────────────

def test_ignore_suppresses_without_teaching():
    assert flag_ocr_cue(CUE, {'lorne'}) == (None, '')
    assert 'Lorne' not in sf.get_names_db()      # ignoring is NOT teaching


def test_ignore_is_case_insensitive():
    """Matches how review_ignore works for the spell checker."""
    assert flag_ocr_cue(CUE, {'LoRnE'}) == (None, '')
    assert ocr_suspect_words(CUE['text'], {'LORNE'}) == []


def test_ignore_defaults_to_nothing_ignored():
    """The default argument must not silently suppress everything."""
    assert flag_ocr_cue(CUE)[0] == 'flag_ocr'
    assert flag_ocr_cue(CUE, ())[0] == 'flag_ocr'
    assert flag_ocr_cue(CUE, None)[0] == 'flag_ocr'


# ── Persistence of a taught name ────────────────────────────────────────────

def test_taught_name_survives_a_names_db_reload():
    """⚠️ load_names_db() REBINDS the set. A taught name must outlive that, or
    it un-teaches itself the next time the bulk list is rebuilt."""
    sf.add_user_name('Lorne')
    assert 'Lorne' in sf.get_names_db()
    sf.unload_names_db()
    assert 'Lorne' in sf.get_names_db(), "unload must not un-teach Tony's names"


def test_possessive_is_stored_bare_to_match_the_lookup():
    """_is_name() strips apostrophes before looking up, so teaching must too."""
    sf.add_user_name("Sheppard's")
    assert 'Sheppard' in sf.get_names_db()


def test_add_user_name_rejects_junk():
    assert sf.add_user_name('') is False
    assert sf.add_user_name(None) is False
    assert sf.add_user_name('x1y') is False      # not alpha; not a name


# ── The property that keeps the two callers honest ──────────────────────────

def test_note_and_menu_agree_about_which_word_is_suspect():
    """⭐ THE ANTI-DRIFT TEST, and the reason ocr_suspect_words() exists.

    flag_ocr_cue() renders 'W -> fix?' for the Note column; the menu lists the
    words it can act on. If those ever come from two implementations, the pane
    goes back to naming a word the menu cannot offer — which is exactly the bug
    this file is named after.
    """
    for text in ('Good call. Lorne out.',
                 'Wejust need a minute.',
                 'nasa called about the launch.',
                 'Nothing suspect about this line at all.'):
        tag, reason = flag_ocr_cue({'text': text})
        hits = ocr_suspect_words(text)
        if tag != 'flag_ocr':
            assert not hits, f'note said clean, menu offered {hits!r}: {text!r}'
        else:
            assert hits, f'note said {reason!r} but menu had nothing: {text!r}'
            assert reason == f'{hits[0][0]} -> {hits[0][1]}?'
