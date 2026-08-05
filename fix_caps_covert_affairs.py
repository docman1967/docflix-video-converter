#!/usr/bin/env python3
"""One-off ALL-CAPS cleanup for the Covert Affairs SDH subtitles.

Built 2026-08-05 for Tony. The Suite's own filter_fix_caps() does 95% of this
job well — it lowercases, restores sentence case, handles "I", and capitalises
~217 built-in proper nouns plus a 1.1M-entry names database. This script runs
THAT first and then repairs the three things it demonstrably gets wrong on this
show, each one found by measurement rather than guesswork:

  1. SURNAMES.        The names DB knows given names, so "ANNIE WALKER" comes
                      out "Annie walker" (162 times in this folder). Fixed
                      CONTEXT-SENSITIVELY: a surname is only capitalised when it
                      follows a known given name or a title (Mr/Agent/Dr...).
                      This matters because walker, price, smith, cook, long and
                      miller are all ordinary English words — capitalising them
                      unconditionally would turn "the price of freedom" into
                      "the Price of freedom".

  2. DOTTED ACRONYMS. filter_fix_caps matches on \\b[a-zA-Z]+\\b, which splits
                      "d.c." into "d" and "c", so its 'dc' rule never fires and
                      you get "Washington, d.c.".

  3. UNAMBIGUOUS NAMES / ORGS. Words that appear nowhere in an English
                      dictionary: FSB, NCTC, FARC, CNI, Rossabi, Solstar,
                      Hillcrest, Tikrit... These are safe to capitalise
                      unconditionally precisely BECAUSE they aren't real words.

Usage:
    fix_caps_covert_affairs.py --dry FILE      # show what would change
    fix_caps_covert_affairs.py --dry           # whole folder, summary only
    fix_caps_covert_affairs.py --apply         # rewrite in place + drop '.sdh'
Originals are copied to a .bak sibling before anything is written.
"""

import argparse
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import modules.subtitle_filters as SF
from modules.subtitle_filters import parse_srt, write_srt, filter_fix_caps, load_names_db

FOLDER = '/home/docman1967/downloads/dst/Process/02 Encode TV/Covert Affairs'

# ── 1. Surnames — only capitalised after a given name or a title ──────────────
# Every one of these was found in the text, not recalled from the show.
SURNAMES = {
    'walker', 'rossabi', 'smith', 'cook', 'price', 'long', 'miller',
    'newman', 'brewer', 'barber', 'schinderman', 'campbell', 'anderson',
    'wilcox', 'mercer', 'lavin', 'michaels', 'braga', 'fischer', 'sheehan',
    'brooks', 'carr', 'mcquaid',
}
TITLES = (r"Mr|Mrs|Ms|Miss|Dr|Agent|Officer|Director|Senator|Colonel|Captain|"
          r"Detective|President|Sir|Lieutenant|Sergeant|Ambassador")

# ── 2. Dotted acronyms ────────────────────────────────────────────────────────
DOTTED = {
    'd.c.': 'D.C.', 'u.s.': 'U.S.', 'u.s.a.': 'U.S.A.', 'u.k.': 'U.K.',
    'f.b.i.': 'F.B.I.', 'c.i.a.': 'C.I.A.', 'a.m.': 'a.m.', 'p.m.': 'p.m.',
    'i.d.': 'I.D.', 'p.o.': 'P.O.', 'e.t.a.': 'E.T.A.',
}

# ── 3. Unambiguous — not English words, so always safe ────────────────────────
ALWAYS_UPPER = {'fsb', 'nctc', 'farc', 'cni', 'gps', 'tac', 'nsa', 'dni',
                'sim', 'atm', 'suv', 'rpg', 'ied'}
# 'ops' and 'osi' deliberately NOT here — "an OPS team" reads wrong; it's "ops".

# ── 4. Entries the BUILT-IN filter capitalises that it shouldn't ──────────────
# filter_fix_caps carries 217 proper nouns, 51 of which are also ordinary
# English words. On this folder that produced ~470 real errors: "in the first
# Place", "It May take some coaxing", "you could stand to Drive a nicer car".
# Suppress the ones where the common-word sense dominates. Kept: china, japan,
# phoenix, god, catholic, french, bible, thanksgiving — those are nearly always
# the proper noun even in ordinary dialogue.
SUPPRESS_BUILTIN = {
    'place', 'drive', 'may', 'march', 'august', 'court', 'park', 'lane',
    'island', 'lake', 'river', 'mountain', 'street', 'road', 'bridge',
    'avenue', 'boulevard', 'blvd', 'highway', 'plaza', 'terrace', 'parkway',
    'east', 'west', 'north', 'south', 'apple', 'amazon', 'jersey', 'queens',
    'internet', 'col', 'gen', 'dept', 'prof', 'rev', 'ms', 'google', 'twitter',
    "father's", "mother's", "valentine's", 'valentines',
}
ALWAYS_TITLE = {
    'rossabi', 'solstar', 'bluebonnet', 'hillcrest', 'tikrit', 'datatech',
    'altans', 'turbaco', 'krepost', 'caymans', 'chechens', 'schinderman',
    'langley', 'georgetown', 'quantico',
}
SPECIAL = {'mcquaid': 'McQuaid', 'mcqaid': 'McQuaid'}


def _post_fix(text, given_names):
    """Repair what filter_fix_caps leaves behind."""
    # Dotted acronyms. GENERAL rule, not a lookup table: any run of two or more
    # single-letter-plus-dot groups is an acronym, so upper-case the lot.
    # A table missed D.I.A., N.S.A., I.R.A., D.C.S. and — worse — matching 'd.c.'
    # inside 'd.c.s.' produced "D.C.s.". Case-insensitive because the base filter
    # has already mangled some of them halfway.
    def _dots(m):
        s = m.group(0)
        return s.upper() if s.lower() not in ('a.m.', 'p.m.') else s.lower()
    text = re.sub(r"\b(?:[A-Za-z]\.){2,}", _dots, text)

    # unambiguous acronyms / proper nouns
    def _w(m):
        w = m.group(0)
        low = w.lower()
        if low in SPECIAL:
            return SPECIAL[low]
        if low in ALWAYS_UPPER:
            return low.upper()
        if low in ALWAYS_TITLE:
            return low.capitalize()
        return w
    text = re.sub(r"\b[A-Za-z]{2,}\b", _w, text)

    # Mc- names: "Mcquaid" -> "McQuaid", "Mcauley" -> "McAuley". General rule
    # rather than a per-name special case. Deliberately NOT applied to "Mac",
    # which collides with ordinary words (machine, Mack, macaroni).
    text = re.sub(r"\bMc([a-z])", lambda m: 'Mc' + m.group(1).upper(), text)

    # Titles. Several of these (ms, col, gen, prof, rev, dept) had to be dropped
    # from the built-in list because they double as ordinary words, which then
    # left "tell me something, ms. Price". Restore them CONTEXTUALLY: a title is
    # only a title when a capitalised name follows it.
    text = re.sub(r"\b(mr|mrs|ms|dr|col|gen|prof|rev|sgt|lt|capt|sen)\.(\s+)(?=[A-Z])",
                  lambda m: m.group(1).capitalize() + '.' + m.group(2), text)
    # Titles written out in full carry no period, so the rule above misses them:
    # "miss Walker" (20), "agent Rossabi" (11), "senator Pierson" (8). Same
    # guard — only when a capitalised name follows.
    text = re.sub(r"\b(miss|agent|senator|director|officer|captain|colonel|"
                  r"detective|ambassador|president|lieutenant|sergeant|doctor|"
                  r"congressman|congresswoman|chief|deputy)(\s+)(?=[A-Z][a-z])",
                  lambda m: m.group(1).capitalize() + m.group(2), text)
    # Two-word place names whose first half is an ordinary word.
    for a, b in (('west', 'Virginia'), ('west', 'Africa'), ('west', 'African'),
                 ('tel', 'Aviv'), ('new', 'York'),
                 ('new', 'Jersey'), ('new', 'Orleans'), ('north', 'Korea'),
                 ('south', 'Korea'), ('saudi', 'Arabia'), ('hong', 'Kong'),
                 ('costa', 'Rica'), ('sri', 'Lanka'), ('el', 'Salvador'),
                 ('san', 'Francisco'), ('los', 'Angeles'), ('las', 'Vegas'),
                 ('united', 'States'), ('white', 'House')):
        text = re.sub(r"\b" + a + r"(\s+)" + b + r"\b",
                      lambda m, A=a, B=b: A.capitalize() + m.group(1) + B, text)

    # surnames, ONLY after a title or a known given name
    sur = '|'.join(sorted(SURNAMES, key=len, reverse=True))
    text = re.sub(r"\b(" + TITLES + r")\.?(\s+)(" + sur + r")\b",
                  lambda m: f"{m.group(1)}.{m.group(2)}{m.group(3).capitalize()}"
                  if m.group(0)[len(m.group(1))] == '.'
                  else f"{m.group(1)}{m.group(2)}{m.group(3).capitalize()}",
                  text, flags=re.IGNORECASE)

    def _pair(m):
        first, gap, second = m.group(1), m.group(2), m.group(3)
        if first in given_names and second.lower() in SURNAMES:
            return first + gap + second.capitalize()
        return m.group(0)
    text = re.sub(r"\b([A-Z][a-z]{2,})(\s+)([a-z]{3,})\b", _pair, text)

    # Written-out titles again, AFTER the surnames have been capitalised.
    # Running it only before left 25 "miss Walker": at that point the line still
    # read "miss walker", so the "capitalised name follows" guard didn't fire.
    text = re.sub(r"\b(miss|agent|senator|director|officer|captain|colonel|"
                  r"detective|ambassador|president|lieutenant|sergeant|doctor|"
                  r"congressman|congresswoman|chief|deputy)(\s+)(?=[A-Z][a-z])",
                  lambda m: m.group(1).capitalize() + m.group(2), text)

    # The names database has 1.1M entries and some of them are contraction
    # fragments — "Didn", "Doesn", "Isn" are all somebody's surname somewhere, so
    # "I didn't" came out "I Didn't". Put them back unless they start the line.
    # A line break is NOT a sentence break — a two-line cue reading
    # "Helen and Walter / Didn't come back here." is one sentence, so guarding on
    # start-of-line kept 49 of these wrongly capitalised. Decide on what actually
    # precedes it in the whole cue instead.
    CONTR = (r"(Didn|Doesn|Isn|Wasn|Aren|Wouldn|Couldn|Shouldn|Hasn|Haven|"
             r"Hadn|Won|Can|Don|Ain|Weren|Mustn|Needn)('[a-z])")

    def _contraction(m):
        head = text[:m.start()]
        stripped = head.rstrip()
        # Keep the capital only for a genuine sentence start.
        if not stripped:
            return m.group(0)
        if re.search(r"[.!?][\"'”’]?$", stripped):
            return m.group(0)
        if stripped.endswith('-'):          # dialogue dash: "- Didn't he?"
            return m.group(0)
        return m.group(1).lower() + m.group(2)

    text = re.sub(r"\b" + CONTR, _contraction, text)
    return text


def fix_file(path, given_names):
    raw = open(path, encoding='utf-8-sig', errors='replace').read()
    cues = parse_srt(raw)
    out = filter_fix_caps([dict(c) for c in cues], custom_names=[],
                          use_names_db=True)
    changed = []
    for before, after in zip(cues, out):
        after['text'] = _post_fix(after['text'], given_names)
        if before['text'] != after['text']:
            changed.append((before['text'], after['text']))
    return cues, out, changed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry', nargs='?', const='ALL')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--show', type=int, default=25)
    a = ap.parse_args()

    load_names_db()
    given_names = getattr(SF, '_names_db', set())
    print(f"names db: {len(given_names):,} entries")
    # Strip the ambiguous built-ins before filter_fix_caps ever sees them.
    before = len(SF.PROPER_NOUNS)
    SF.PROPER_NOUNS = type(SF.PROPER_NOUNS)(
        w for w in SF.PROPER_NOUNS if w not in SUPPRESS_BUILTIN)
    print(f"built-in proper nouns: {before} -> {len(SF.PROPER_NOUNS)} "
          f"({before - len(SF.PROPER_NOUNS)} ambiguous ones suppressed)")

    if a.dry and a.dry != 'ALL':
        files = [a.dry]
    else:
        files = sorted(f for f in
                       (os.path.join(FOLDER, x) for x in os.listdir(FOLDER))
                       if f.endswith('.sdh.srt'))
    print(f"files: {len(files)}\n")

    total_changed = 0
    for path in files:
        cues, out, changed = fix_file(path, given_names)
        total_changed += len(changed)
        if a.dry and a.dry != 'ALL':
            for b, aft in changed[:a.show]:
                print("  BEFORE:", b.replace('\n', ' / '))
                print("  AFTER :", aft.replace('\n', ' / '))
                print()
            print(f"{os.path.basename(path)}: {len(changed)}/{len(cues)} cues changed")
        elif a.apply:
            shutil.copy2(path, path + '.bak')
            with open(path, 'w', encoding='utf-8') as fh:
                fh.write(write_srt(out))
            new = path.replace('.eng.sdh.srt', '.eng.srt')
            if new != path:
                os.rename(path, new)
            print(f"  {os.path.basename(new)}  ({len(changed)} cues)")
    if not (a.dry and a.dry != 'ALL'):
        print(f"\ntotal cues changed: {total_changed}")


if __name__ == '__main__':
    main()
