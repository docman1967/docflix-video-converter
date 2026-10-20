"""An accented letter is part of the word (Tony, 2026-10-20, The Flash).

"Last year, my fiancé died." was flagged as "fianc": the word pattern only
knew A-Z, so é split the word in two.

    python3 -m pytest -q tests/test_spell_accents.py
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytest.importorskip("spellchecker")

from modules.spell_checker import (WORD_RE, checker_for,         # noqa: E402
                                   settle_compounds)


def _flags(text, known=()):
    spell = checker_for([w.lower() for w in known])
    words = WORD_RE.findall(text)
    unknown = settle_compounds(spell, spell.unknown(words), known)
    return [w for w in words if w.lower() in unknown]


def test_accented_words_are_whole_tokens():
    assert WORD_RE.findall("My fiancé's café résumé, naïve Pokémon.") == \
        ["My", "fiancé's", "café", "résumé", "naïve", "Pokémon"]


@pytest.mark.parametrize("text", [
    "Last year, my fiancé died.", "Meet me at the café.", "What a cliché.",
    "His fiancée is here.",
])
def test_dictionary_accented_words_are_not_flagged(text):
    assert _flags(text) == []


def test_an_unknown_accented_word_is_flagged_whole_and_can_be_taught():
    assert _flags("Send your résumé.") == ["résumé"]
    assert _flags("Send your résumé.", known=["résumé"]) == []


def test_a_speck_read_as_an_accent_is_still_caught():
    """'thé' must NOT pass just because 'the' is a word."""
    assert _flags("Open thé door.") == ["thé"]


def test_plain_ascii_unchanged():
    assert WORD_RE.findall("Kal-El's Wi-Fi isn't well-known.") == \
        ["Kal-El's", "Wi-Fi", "isn't", "well-known"]


def test_digits_and_underscores_are_not_letters():
    assert WORD_RE.findall("Earth-2 and S01E05 and foo_bar") == \
        ["Earth", "and", "S", "E", "and", "foo", "bar"]
