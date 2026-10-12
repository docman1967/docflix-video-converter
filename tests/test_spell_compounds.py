"""Hyphenated words are spell-checked WHOLE, and can be taught whole.

Tony, 2026-10-12, on Supergirl: the spell checker made no allowance for
"Wi-Fi". Split at the hyphen, "Kal-El" was flagged as "Kal" + "El", and adding
"Kal-El" to the dictionary never matched anything.

    python3 -m pytest -q tests/test_spell_compounds.py
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytest.importorskip("spellchecker")

from modules.spell_checker import (WORD_RE, checker_for,         # noqa: E402
                                   settle_compounds)
from modules.subtitle_editor import drop_recurring_words        # noqa: E402


def _flags(text, known=()):
    spell = checker_for([w.lower() for w in known])
    words = WORD_RE.findall(text)
    unknown = settle_compounds(spell, spell.unknown(words), known)
    return [w for w in words if w.lower() in unknown]


def test_a_compound_is_one_token():
    assert WORD_RE.findall("Kal-El's Wi-Fi isn't well-known.") == \
        ["Kal-El's", "Wi-Fi", "isn't", "well-known"]


def test_an_untaught_compound_is_flagged_whole():
    assert _flags("Tell Kal-El.") == ["Kal-El"]


def test_teaching_the_whole_compound_silences_it():
    assert _flags("Tell Kal-El about the Wi-Fi.", known=["Kal-El", "Wi-Fi"]) == []


@pytest.mark.parametrize("text", [
    "It's well-known.", "Twenty-two of them.", "I-I-I don't know.",
    "A self-made man.", "My mother-in-law's house.",
])
def test_a_compound_of_real_words_is_not_flagged(text):
    assert _flags(text) == []


def test_a_typo_inside_a_compound_is_still_caught():
    assert _flags("A well-knwon fact.") == ["well-knwon"]


def test_a_recurring_compound_counts_as_recurring():
    cues = [{'text': "Ker-rah!"}] * 3
    errors = {i: ["Ker-rah"] for i in range(3)}
    assert drop_recurring_words(cues, errors) == (set(), {})


@pytest.mark.parametrize("text", [
    "Bio-weapons.", "A bio-engineer.", "You can't un-see it.",
    "That's pre-emptive.", "Stop zig-zagging.", "Non-stop.",
])
def test_a_prefix_or_solid_spelling_makes_a_compound_known(text):
    assert _flags(text) == []


def test_a_prefix_does_not_excuse_a_typo():
    assert _flags("Bio-wepaons.") == ["Bio-wepaons"]
