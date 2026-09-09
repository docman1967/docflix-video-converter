#!/usr/bin/env python3
"""
fix_alphas_caps — un-shout the Alphas subtitles (24 episodes, all-caps CC rip).

Third in the series after fix_caps_covert_affairs.py and fix_tara_caps.py. The
Suite's `filter_fix_caps` does the bulk; this wrapper supplies what it cannot
know, plus two general fixes that keep recurring.

⚠️⚠️ THE DICTIONARY METHOD IS BLIND TO NAMES THAT ARE ALSO WORDS.
The show-name list for the last two jobs was built by subtracting a system
dictionary from the all-caps tokens. That finds Rosen/Skylar/Bazevich/Persky
fine — and misses every character whose name is an ordinary English word. On
the Tara job Tony caught "Marsh" that way, after shipping. Checked FIRST this
time, and on this show it is not a rounding error:

    GARY 652 · BILL 385 · RACHEL 278 · NINA 249 · HICKS 209 · PARISH 205
    CAMERON 91 · STANTON 86 · LEE 61 · HARKEN 59 · MARCUS 58 · BELL 23

Over 2,700 occurrences the dictionary pass alone would have lowercased.

Usage:
    fix_alphas_caps.py DIR              # preview, writes nothing
    fix_alphas_caps.py DIR --commit     # rewrite in place (.bak kept)
"""
import argparse
import html
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from modules.subtitle_filters import (          # noqa: E402
    parse_srt, write_srt, filter_fix_caps,
    load_names_db, is_names_db_available,
)

# ⚠️ Display case, NOT lowercase. apply_custom_names() substitutes the string
# verbatim, so a lowercase entry replaces "gary" with "gary" and silently does
# nothing. (Learned on the Tara job: "THIS IS TARA" came out "This is tara".)
SHOW_NAMES = [
    # Found by dictionary subtraction
    'Rosen', 'Dani', 'Skylar', 'Kat', 'Cley', 'Doka', 'Bazevich', 'Persky',
    'Kosar', 'Theroux', 'Dunham', 'Zelanski', 'Pirzad', 'Gar',
    # ⚠️ Found ONLY by checking known character names against the text —
    # every one of these is a dictionary word and was invisible to the
    # subtraction method that produced the list above.
    'Gary', 'Bill', 'Rachel', 'Nina', 'Hicks', 'Parish', 'Cameron',
    'Stanton', 'Lee', 'Harken', 'Marcus', 'Bennett', 'Ayers', 'John',
    # Organisations / in-world terms
    'DARPA', 'Binghamton', 'Alpha', 'Alphas', 'Red Flag',
    'Quantico', 'CSI', 'Elkhart',
    # ⚠️ 'Rich' and 'Angel' are BOTH ordinary words. Safe here only because
    # every occurrence was listed first and each one is a name:
    #   rich  — 1 total: "There's one to rich Wallace."  (Rich Wallace)
    #   angel — 2 total: "Oh, angel." / "Sleep well, sweet angel."  (direct
    #           address; Tony's call — he's watched it, they're names not
    #           endearments)
    # ⛔ Do NOT copy these into another show's list without re-checking. In a
    # show where someone says "a rich man" or "you're an angel", this would be
    # actively wrong.
    'Rich', 'Angel', 'Parsonetti', 'Griffin',
    # ⚠️ Same caveat as Rich/Angel — ordinary words, safe only because every
    # occurrence was read first and each is a personal name:
    #   bask  — 1: "to belong to Fred bask."      (surname)
    #   sari  — 2: "congratulate sari,"            (given name, not the garment)
    #   rains — 1: "off with Claude rains."        (the actor Claude Rains)
    'Bask', 'Sari', 'Rains',
    # ⚠️ "Don" — a character, and Tony caught it. 17 standalone lowercase uses
    # ("Thank you, don.") against 13 already correct at sentence starts, so it
    # is genuinely inconsistent rather than uniformly wrong, which is exactly
    # the shape that reads as a typo.
    # ⚠️⚠️ SAFE ALONGSIDE 739 "don't". apply_custom_names() matches whole
    # tokens INCLUDING apostrophes (the O'Brien fix), so "don't" is looked up
    # as "don't" — not found — and its possessive fallback only accepts a
    # trailing "s" or "", never "t". Verified on the real files after adding.
    'Don',
]

# ⚠️ DOTTED ACRONYMS — 88 of them (D.O.D. 17, D.C.I.S. 11, N.S.A., F.B.I.,
# I.E.D., E.E.G. ...). filter_fix_caps lowercases everything first, and its
# sentence-capitalisation rule needs WHITESPACE after the period
# (`[.!?]["']?\s+[a-z]`), which "d.o.d." does not have. So they come out
# permanently lowercase. Restored by exact match after the filter runs.
# ⚠️ CASE-INSENSITIVE on the letters, deliberately. v1 was `[a-z]` only and
# missed 13 of 88 — an acronym at the START of a cue gets its first letter
# capitalised by filter_fix_caps ("D.c.", "I.s.", "F.b."), so an all-lowercase
# pattern skips exactly the ones that look worst. Nothing was lost, just
# mis-cased, which is the kind of miss a count alone would not surface: 75
# "correct" acronyms looked like a pass until the total came up 13 short.
_ACRONYM_RE = re.compile(r"\b(?:[A-Za-z]\.){2,}")

# ⚠️ Auxiliary contractions whose ROOTS are real surnames in the 1.1M-name DB
# (Didn, Wasn, Aren...). `_cap_word` matches r'\b[a-zA-Z]+\b' and an apostrophe
# is a word boundary, so "didn't" is looked up as "didn" and comes back
# "Didn't". Only these fixed-function words are safe to force lowercase —
# "Rosen's", "Gary's", "Bill's" are correct possessives.
_AUX = ('didn', 'wasn', 'weren', 'aren', 'isn', 'ain', 'doesn', 'don',
        'couldn', 'shouldn', 'wouldn', 'hasn', 'haven', 'hadn', 'won',
        'mustn', 'needn', 'shan', 'mightn', 'daren')
_AUX_RE = re.compile(r"([a-z,]\s+)(" + '|'.join(c.capitalize() for c in _AUX) + r")'")

# ⚠️ BELL and CLAY are BOTH characters and common nouns in this show:
#   "AGENT BELL" / "SANDRA BELL"   vs  "A FRICKIN' BELL ON YOU"
#   "WHAT WAS THAT, CLAY?"          vs  "HAVE FEET OF CLAY"
# Capitalised by default (they are names far more often), then these specific
# common-noun uses are put back. Verified by reading every occurrence — 23
# BELL and 4 CLAY in total, so this is exhaustive rather than a guess.
_COMMON_NOUN_FIXES = (
    ("feet of Clay", "feet of clay"),
    ("frickin' Bell", "frickin' bell"),
    ("a Bell on", "a bell on"),
    # ⚠️ Puts back the ONE genuine month. "in the merry month of May" is
    # mid-sentence and preceded by a lowercase word, so the _COMMON_WORDS rule
    # above lowercases it along with the 24 modal verbs. Found by listing every
    # "May" before adding the rule rather than after — a blanket lowercase
    # would have quietly produced "month of may".
    ("month of may", "month of May"),
)


# ⚠️⚠️ COMMON WORDS THE NAMES DB WRONGLY CAPITALISES.
# Place/Drive/Street/Lane are all real surnames in the 1.1M-name database, so
# `_cap_word` capitalises them wherever they appear — including as ordinary
# nouns and verbs. 145 instances here:
#     "It's this Place"  "at my Place last night"  "Just don't Drive"
#     "across the Street"  "Street cameras"  "memory Lane"
# Same root cause as the Didn/Wasn contraction problem: the names DB is a list
# of NAMES, not a claim that a token IS a name in context.
#
# ⚠️ Only lowercased MID-SENTENCE (preceded by a lowercase word), never at a
# sentence start and never after a capitalised word — so a genuine address
# like "Main Street" or "Elm Drive" survives, as does a sentence beginning
# "Place the file...". Checked every occurrence before adding each word:
# `Bell` (Mr./Mrs. Bell) and `Will` (the character) are CORRECT here and are
# deliberately NOT in this list.
# ⚠️ 'May' is here for a different reason than the others: it is not a surname
# artefact but the MODAL VERB, capitalised because May is also a first name.
# 24 mid-sentence instances — "They May even indicate", "which means it May
# take weeks", "If I May...". Sentence-initial "May I?" and "May her soul be
# blessed" are correct and are not touched, since the rule requires a
# preceding lowercase word.
_COMMON_WORDS = ('Place', 'Drive', 'Street', 'Lane', 'May')
_COMMON_RE = re.compile(r"([a-z,]\s+)(" + '|'.join(_COMMON_WORDS) + r")\b")

# ⚠️ Acronyms WITHOUT dots — the dotted-acronym restorer cannot see these.
# Applied case-insensitively because they arrive in mixed states: "cdc" (5),
# "dcis" (2) AND "Dcis" (5) — the capitalised variants come from cues where
# the acronym starts a sentence, exactly the split that hid 13 dotted
# acronyms earlier today.
_BARE_ACRONYMS = ('CDC', 'DCIS')
_BARE_ACR_RE = re.compile(r"\b(" + '|'.join(_BARE_ACRONYMS) + r")\b", re.I)


def lowercase_common_words(text):
    return _COMMON_RE.sub(lambda m: m.group(1) + m.group(2).lower(), text)


# ⚠️ MKUltra arrives in FOUR spellings across these files — "mk ultra" (9),
# "M.K. Ultra" (5), "MKULTRA" (2), plus case variants. Tony asked for one
# canonical form. Normalised BEFORE restore_acronyms, or "m.k." would first be
# promoted to "M.K." and then need a second rule to undo.
_MKULTRA_RE = re.compile(r"\bm\.?\s*k\.?\s*ultra\b", re.I)

# ⚠️⚠️ THE ACRONYM-PERIOD BUG — Tony spotted this one, and it is mine.
# A dotted acronym ends in a period, so filter_fix_caps' sentence rule
# (`[.!?]\s+[a-z]` -> capitalise) reads "D.O.D. is" as a sentence boundary and
# produces "D.O.D. Is". 23 instances: "The D.O.D. Is going", "N.S.A. Won't
# tell you", "every E.R. In the city", "8:00 P.M. Tonight".
# ⚠️ Runs AFTER restore_acronyms, since it matches on the uppercase form.
_ACR_NEXT_RE = re.compile(r"(\b(?:[A-Z]\.){2,}\s+)([A-Z])(?=[a-z])")

# ⚠️ ONE genuine exception, found by reading the full cue rather than the
# match: "D.O.D. Stand down! / Get down on the ground now." is somebody
# SHOUTING — "D.O.D.! Stand down!" — so that capital is correct. Restored
# after the blanket rule. Every one of the other 22 was checked and is a real
# mid-sentence continuation.
_ACR_NEXT_KEEP = (("D.O.D. stand down", "D.O.D. Stand down"),)


def normalise_mkultra(text):
    return _MKULTRA_RE.sub("MKUltra", text)


def fix_after_acronym(text):
    text = _ACR_NEXT_RE.sub(lambda m: m.group(1) + m.group(2).lower(), text)
    for wrong, right in _ACR_NEXT_KEEP:
        text = text.replace(wrong, right)
    return text


def restore_acronyms(text):
    text = _ACRONYM_RE.sub(lambda m: m.group(0).upper(), text)
    return _BARE_ACR_RE.sub(lambda m: m.group(1).upper(), text)


def fix_aux_contractions(text):
    return _AUX_RE.sub(lambda m: m.group(1) + m.group(2).lower() + "'", text)


def fix_common_nouns(text):
    for wrong, right in _COMMON_NOUN_FIXES:
        text = text.replace(wrong, right)
    return text


# ⚠️⚠️ SOUND-EFFECT CUES — the only DESTRUCTIVE step in this script.
# Gary perceives electromagnetic signals, and the SDH rip transcribes his
# reaction as standalone "Bzz." / "Buzz." / "Zz." cues. Tony, 2026-09-09:
# *"these words... are being displayed when one of the characters is using
# how powers. Please remove these... they aren't spoken dialogue."*
#
# ⚠️ ANCHORED (^...$) so it only matches a cue that is NOTHING BUT the sound.
# A substring match would destroy real dialogue — there are five legitimate
# uses in these files that MUST survive:
#     "Do you hear that buzzing?"   "It...it's buzzing."   "It's all buzzy."
#     "No more buzz."               "memory lane is killing my buzz."
# Verified by listing every occurrence before writing this: 6 solo cues to
# drop, 5 dialogue lines to keep, and all 6 are consecutive (18-23) in a
# single episode.
_SFX_RE = re.compile(r'^\s*[-\s]*(?:bzz+|buzz|zz+)[.!?,\s]*$', re.I)


def drop_sound_effect_cues(cues):
    """Remove cues whose ENTIRE text is a power-effect noise.

    write_srt() renumbers from 1 on output, so removing cues cannot leave a
    numbering gap. Returns (kept_cues, dropped_texts) — the dropped text is
    returned rather than discarded so the caller can print exactly what was
    deleted; a destructive step should never be silent about what it took.
    """
    kept, dropped = [], []
    for c in cues:
        lines = [l for l in c['text'].split('\n') if l.strip()]
        if lines and all(_SFX_RE.match(l) for l in lines):
            dropped.append(c['text'].replace('\n', ' / '))
        else:
            kept.append(c)
    return kept, dropped


def cap_after_music(cues):
    """Capitalize a cue following a ♪ lyric cue.

    filter_fix_caps decides capitalisation by asking whether the PREVIOUS cue
    ended in [.!?] — correct for sentences spanning two cues, wrong after a
    song, which ends in ♪. 344 music lines in this show, so this matters more
    here than it did on Tara.
    """
    fixed = 0
    for i, c in enumerate(cues):
        if i == 0 or not cues[i - 1]['text'].rstrip().endswith('♪'):
            continue
        m = re.match(r'^(\s*(?:-\s*)?)([a-z])', c['text'])
        if m:
            c['text'] = c['text'][:m.start(2)] + m.group(2).upper() + c['text'][m.end(2):]
            fixed += 1
    return fixed


def process(path, commit):
    raw = open(path, encoding='utf-8', errors='replace').read()
    cues = parse_srt(html.unescape(raw))
    before = write_srt(cues)

    cues = filter_fix_caps(cues, custom_names=SHOW_NAMES, use_names_db=True)
    for c in cues:
        t = c['text']
        # ⚠️ ORDER IS LOAD-BEARING. MKUltra first (before "m.k." becomes an
        # acronym), then acronyms restored, then the after-acronym fix which
        # matches on the restored uppercase form.
        t = normalise_mkultra(t)
        t = restore_acronyms(t)
        t = fix_after_acronym(t)
        t = fix_aux_contractions(t)
        t = lowercase_common_words(t)
        t = fix_common_nouns(t)
        c['text'] = t
    music = cap_after_music(cues)
    cues, dropped = drop_sound_effect_cues(cues)

    after = write_srt(cues)
    changed = after != before
    if commit and changed:
        shutil.copy2(path, path + '.bak')
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(after)
    return changed, music, dropped


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('directory')
    ap.add_argument('--commit', action='store_true')
    a = ap.parse_args()

    if is_names_db_available():
        load_names_db()

    files = sorted(f for f in os.listdir(a.directory) if f.endswith('.srt'))
    tot_music = 0
    all_dropped = []
    for f in files:
        changed, music, dropped = process(os.path.join(a.directory, f), a.commit)
        tot_music += music
        for d in dropped:
            all_dropped.append((f, d))
        print('%-52s music=%-3d drop=%-3d %s'
              % (f[:52], music, len(dropped), 'changed' if changed else '-'))
    # ⚠️ Name every deleted cue. Removing content is the one step that cannot be
    # reviewed after the fact from the output alone.
    if all_dropped:
        print('\nDropped %d sound-effect cue(s):' % len(all_dropped))
        for f, d in all_dropped:
            print('   %-46s %s' % (f[:46], d))
    print('\n%d file(s), %d post-lyric capitals, %d cue(s) dropped. %s'
          % (len(files), tot_music, len(all_dropped),
             'WRITTEN (.bak kept)' if a.commit else 'PREVIEW ONLY — rerun with --commit'))


if __name__ == '__main__':
    main()
