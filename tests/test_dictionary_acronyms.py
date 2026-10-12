"""Acronyms get their own pane in the Dictionary window (Tony, 2026-10-12).

They're stored in the Names list (custom_cap_words) and always were; the
window now splits that one list into Names and Acronyms for editing.

    python3 -m pytest -q tests/test_dictionary_acronyms.py
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.dictionary_editor import is_acronym, split_names   # noqa: E402


@pytest.mark.parametrize("word", ["LAPD", "DEO", "US", "U.S.", "HIVE"])
def test_all_caps_is_an_acronym(word):
    assert is_acronym(word)


@pytest.mark.parametrize("word", ["Kal-El", "Ker-rah", "McDonald", "I", "A"])
def test_names_and_single_letters_are_not(word):
    assert not is_acronym(word)


def test_split_keeps_order_and_loses_nothing():
    caps = ["Kara", "DEO", "Kal-El", "NCPD", "Alex"]
    names, acronyms = split_names(caps)
    assert names == ["Kara", "Kal-El", "Alex"]
    assert acronyms == ["DEO", "NCPD"]
    assert sorted(names + acronyms) == sorted(caps)


def test_removing_an_acronym_takes_it_from_both_lists_exact_case():
    """The window's remove_acronyms path, driven headless."""
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    root.withdraw()
    from unittest import mock
    from modules import dictionary_editor as D

    class App:
        custom_spell_words = ["us", "ABQ", "Kal-El"]
        custom_cap_words = ["Kal-El", "ABQ", "DEO"]
        def save_preferences(self):
            pass

    app = App()
    win = D.show_dictionary_editor(app, root)
    # The Acronyms pane's listbox is the last one built.
    lbs = [w for w in _walk(win) if isinstance(w, tk.Listbox)]
    assert len(lbs) == 3
    acr = lbs[2]
    items = [acr.get(i).strip() for i in range(acr.size())]
    assert items == ["ABQ", "DEO"]
    acr.selection_set(0)
    remove_btn = [w for w in _walk(acr.master) if
                  getattr(w, 'cget', None) and _text(w) == "Remove"][0]
    with mock.patch.object(D.messagebox, 'askyesno', return_value=True), \
            mock.patch.object(D, 'write_backup'):
        remove_btn.invoke()
    assert app.custom_cap_words == ["Kal-El", "DEO"]
    assert app.custom_spell_words == ["us", "Kal-El"]       # "us" untouched
    win.destroy()
    root.destroy()


def _walk(w):
    for c in w.winfo_children():
        yield c
        yield from _walk(c)


def _text(w):
    try:
        return w.cget('text')
    except Exception:
        return None
