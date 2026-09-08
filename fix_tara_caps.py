#!/usr/bin/env python3
"""
fix_tara_caps — un-shout the United States of Tara subtitles.

These are all-caps closed-caption rips. The Media Suite's own
`filter_fix_caps` does the heavy lifting; this wrapper exists for the two
things it cannot know on its own:

  * ⚠️ **"T" is a character name**, not a stray capital. Tara's alters call
    her T ("T, CAN WE PLEASE GO", "THAT'S T. LIKE THE LETTER,"). 114 genuine
    standalone uses across the season. A generic lowercaser turns those into
    "t", which reads as a typo in every single one. Single letters are exactly
    the case an automated filter gets wrong, because it cannot tell a name from
    a fragment without knowing the show.
  * **Show-specific proper nouns** that the generic list misses — surnames and
    invented names (Gregson, Shoshana, Barnabeez, Beaverlamp, Hattaras...).
    Derived from the files themselves by subtracting a system dictionary, then
    hand-checked, rather than guessed at.

⚠️ ALSO DECODES HTML ENTITIES. The source carries 1,636 `&gt;` and 3 `&amp;`.
Most players do NOT decode entities in SubRip, so on screen those render
literally as `&gt;&gt; THAT WASN'T ME.` That is a visible defect independent of
the casing, and this is the pass that is already rewriting every file.

Usage:
    fix_tara_caps.py DIR              # preview: report only, writes nothing
    fix_tara_caps.py DIR --commit     # rewrite in place (.bak kept)
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

# ⚠️ Derived from the subtitles themselves (tokens absent from
# /usr/share/dict/words, used 3+ times), then read by eye to drop the
# vocalizations — HMM/OOH/DOO/WHOO/AAH are not names and must stay lowercase.
#
# ⚠️⚠️ WRITE THESE IN THEIR FINAL DISPLAY CASE, not lowercase.
# `apply_custom_names()` substitutes the string **verbatim** as given
# (`custom_single = {n.lower(): n ...}` then returns `original`), so a
# lowercase entry replaces "tara" with "tara" and silently does nothing.
# Caught on the first test file: "THIS IS TARA" came out "This is tara".
SHOW_NAMES = [
    'Gregson', 'Shoshana', 'Barnabeez', 'Moosh', 'Craine', 'Hattaras',
    'Hawkwind', 'Pammy', 'Vita', 'Schoenbaum', 'Zach', 'Johanssen',
    'Charmie', 'Beaverlamp', 'Bev', 'Maxie', 'Hany', 'Gershenoff',
    'Niigata', 'Xanax', 'YoGoHut', 'Trane', 'Doobie',
    'Tara', 'Marshall', 'Kate', 'Max', 'Charmaine', 'Neil', 'Lynda',
    'Alice', 'Buck', 'Ted', 'Pete', 'Nick',
    # ⚠️ "Marsh" — Marshall's nickname, 26 uses, every one a direct address
    # ("Thanks, marsh." / "Okay, marsh, am I dropping you?"). It is a real
    # dictionary word, so subtracting a dictionary — which is how the rest of
    # this list was built — could never have surfaced it. Tony caught it.
    # ⚠️ Checked the other direction too: every lowercase non-dictionary word
    # used in direct address was reviewed, and Marsh is the only real name
    # among them (the rest are contraction fragments and insults).
    'Marsh',
    # The show title, quoted in every "Previously on..." recap (35 of them).
    # A phrase entry — apply_custom_names() handles multi-word names
    # separately from single tokens.
    'United States of Tara',
]

# ⚠️ Restored AFTER filter_fix_caps runs, because the filter lowercases
# everything first and a bare "t" is indistinguishable from a fragment by then.
# Matched only where it stands alone as a word AND is not part of a
# contraction — `(?<!')` keeps DON'T/WASN'T/COULDN'T intact, which is the
# trap: an apostrophe is a word boundary, so a naive \bt\b hits all of them.
_BARE_T = re.compile(r"(?<!')\bt\b(?!')")


def fix_text(text):
    """Entity-decode, then restore the bare-T name."""
    text = html.unescape(text)
    return text


def restore_t(text):
    return _BARE_T.sub('T', text)


# ⚠️ `>>` is the closed-caption speaker-change marker — functionally the same
# as the leading `-` that filter_fix_caps already treats as a sentence start.
# It does not know about `>>`, so 632 lines here came out ">> previously on"
# instead of ">> Previously on". (These arrive HTML-escaped as `&gt;&gt;`,
# which is why they must be decoded before this runs, not after.)
_SPEAKER_MARK = re.compile(r"^(\s*>>+\s*)([a-z])", re.M)

# ⚠️ apply_custom_names() substitutes phrases with `phrase.title()`, and
# .title() capitalises EVERY word — so the show title comes back as
# "United States Of Tara". Passing the correct casing does not help; the
# filter re-titles it. Fixed after the fact rather than by changing a shared
# filter that every other show also uses.
_TITLECASE_FIXES = (
    ('United States Of Tara', 'United States of Tara'),
)


def fix_speaker_marks(text):
    return _SPEAKER_MARK.sub(lambda m: m.group(1) + m.group(2).upper(), text)


def fix_titlecase_phrases(text):
    for wrong, right in _TITLECASE_FIXES:
        text = text.replace(wrong, right)
    return text


# ⚠️ Auxiliary-verb contraction roots that are ALSO real surnames in the
# 1.1M-name database: Didn, Wasn, Aren, Ain, Weren... `_cap_word()` in the
# shared filter matches on r'\b[a-zA-Z]+\b', and an apostrophe is a word
# boundary, so "didn't" is seen as "didn" + "t" — the root gets looked up on
# its own, hits the surname list, and comes back "Didn't". 224 occurrences
# here. ⚠️ The same split is ALREADY documented as fixed for *custom* names
# (the O'Brien case) but the names-DB path still uses the old pattern.
#
# ⚠️ Only these fixed-function words are safe to force lowercase.
# "Tara's", "Neil's", "Marshall's", "Kate's" are correct possessives of real
# names and MUST be left alone — a blanket rule here would break every one.
_AUX_CONTRACTIONS = (
    'didn', 'wasn', 'weren', 'aren', 'isn', 'ain', 'doesn', 'don',
    'couldn', 'shouldn', 'wouldn', 'hasn', 'haven', 'hadn', 'won',
    'mustn', 'needn', 'shan', 'mightn', 'daren',
)
# ⚠️ A capture group, NOT a lookbehind. Python's re requires fixed-width
# lookbehinds, so `(?<=[a-z,]\s)` spans exactly one space and silently missed
# "you  Didn't" (double space — real, one instance in S02). Matching the
# preceding word explicitly handles any run of whitespace.
_AUX_RE = re.compile(
    r"([a-z,]\s+)(" + '|'.join(c.capitalize() for c in _AUX_CONTRACTIONS) + r")'",
)


def fix_aux_contractions(text):
    """Lowercase auxiliary contractions the names DB wrongly capitalized.

    Only rewrites where the word is clearly MID-SENTENCE — preceded by a
    lowercase letter or comma plus whitespace. A sentence-initial "Didn't you
    know?" is correct and must survive untouched, which a naive global
    lowercase would destroy.
    """
    return _AUX_RE.sub(lambda m: m.group(1) + m.group(2).lower() + "'", text)


def fix_speaker_label_cues(cues):
    """Re-case a cue whose SHOUTING line hid behind a mixed-case speaker label.

    ⚠️ filter_fix_caps skips any cue less than 60% uppercase — a good guard,
    because it stops the filter mangling text that is already correctly cased.
    But a few cues look like:

        Female passer-by:
        IT'S FRANKLIN!

    The mixed-case label drags the whole-cue ratio to ~0.48, under the
    threshold, so the ALL-CAPS dialogue underneath is skipped with it. The
    guard is measuring the cue when the shouting is per-line.

    Only **3 lines in 36 episodes** hit this, so it is deliberately handled
    here as a narrow post-pass rather than by loosening a threshold that is
    doing real work everywhere else. Re-cases just the offending LINE and
    leaves the label alone.
    """
    fixed = 0
    for c in cues:
        lines = c['text'].split('\n')
        out = []
        for ln in lines:
            alpha = re.sub(r'[^A-Za-z]', '', ln)
            if len(alpha) >= 4 and sum(1 for ch in alpha if ch.isupper()) / len(alpha) > 0.9:
                new = ln.lower()
                new = re.sub(r'^(\s*(?:-\s*)?)([a-z])',
                             lambda m: m.group(1) + m.group(2).upper(), new)
                new = re.sub(r"\bi\b", 'I', new)
                new = re.sub(r"\bi'(m|ll|ve|d|s)\b", lambda m: "I'" + m.group(1), new)
                for nm in SHOW_NAMES:
                    new = re.sub(r'\b%s\b' % re.escape(nm.lower()), nm, new)
                out.append(new)
                fixed += 1
            else:
                out.append(ln)
        c['text'] = '\n'.join(out)
    return fixed


def cap_after_music(cues):
    """Capitalize a cue that follows a song-lyric cue.

    ⚠️ filter_fix_caps decides whether to capitalize a cue's first line by
    asking if the PREVIOUS cue ended in [.!?] — a sensible rule, since
    subtitle sentences routinely span two cues and capitalizing a
    continuation is worse than not. But a lyric cue ends in ♪, not a full
    stop, so the line after a song is misread as a continuation and left
    lowercase.

    Measured across all 36 episodes: 3,011 cues start lowercase, and only
    **84** of them follow a ♪ cue. The other ~2,900 were sampled and are
    genuine mid-sentence continuations, correctly left alone — so this fixes
    the real defect without disturbing the rule that is doing its job.

    Done HERE rather than in modules/subtitle_filters.py on purpose: that
    filter is shared by every show, and changing its sentence-boundary rule
    for one all-caps CC rip is not a trade worth making without Tony's say-so.
    """
    fixed = 0
    for i, c in enumerate(cues):
        if i == 0:
            continue
        if not cues[i - 1]['text'].rstrip().endswith('♪'):
            continue
        t = c['text']
        m = re.match(r'^(\s*(?:-\s*)?)([a-z])', t)
        if m:
            c['text'] = t[:m.start(2)] + m.group(2).upper() + t[m.end(2):]
            fixed += 1
    return fixed


def process(path, commit):
    raw = open(path, encoding='utf-8', errors='replace').read()
    decoded = html.unescape(raw)
    entities = len(re.findall(r'&(?:gt|lt|amp|quot|#\d+);', raw))

    cues = parse_srt(decoded)
    before = write_srt(cues)

    cues = filter_fix_caps(cues, custom_names=SHOW_NAMES, use_names_db=True)
    fix_speaker_label_cues(cues)
    for c in cues:
        t = c['text']
        t = restore_t(t)
        t = fix_aux_contractions(t)
        t = fix_speaker_marks(t)
        t = fix_titlecase_phrases(t)
        c['text'] = t
    music_fixed = cap_after_music(cues)

    after = write_srt(cues)
    changed = (after != before) or entities

    if commit and changed:
        shutil.copy2(path, path + '.bak')
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(after)
    return entities, changed, music_fixed, before, after


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('directory')
    ap.add_argument('--commit', action='store_true')
    a = ap.parse_args()

    if is_names_db_available():
        load_names_db()

    files = sorted(f for f in os.listdir(a.directory) if f.endswith('.srt'))
    tot_ent = tot_music = 0
    for f in files:
        p = os.path.join(a.directory, f)
        ent, changed, music, before, after = process(p, a.commit)
        tot_ent += ent
        tot_music += music
        print('%-58s ent=%-5d music=%-3d %s' % (f[:58], ent, music,
                                                'changed' if changed else '-'))
    print('\n%d file(s), %d entit(ies) decoded, %d post-lyric capitals. %s'
          % (len(files), tot_ent, tot_music,
             'WRITTEN (.bak kept)' if a.commit else 'PREVIEW ONLY — rerun with --commit'))


if __name__ == '__main__':
    main()
