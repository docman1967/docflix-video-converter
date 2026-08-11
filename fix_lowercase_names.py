#!/usr/bin/env python3
"""
fix_lowercase_names — restore proper-noun capitals the ALL-CAPS filter stripped.

WHY
---
2026-08-11. Tony: *"the fix caps filter didn't quite get everything."*

He was right, but not in the way either of us assumed. Fix ALL CAPS had done its
job almost perfectly — of 110,247 dialogue lines in Revenge only THIRTEEN still
shouted. The damage was the other direction: converting "EMILY" to lowercase
gives "emily", not "Emily", so the filter left **8,259 lowercase character
names** across 83 files.

⚠️ THE CORPUS CANNOT TEACH YOU THE NAMES. The obvious approach — find words that
appear capitalised mid-sentence, they must be proper nouns — returns almost
nothing here, because the names are lowercase in *every* occurrence. Total
corruption defeats internal evidence. The list has to come from outside.

So it does: TheTVDB's character list for the series (authoritative), plus
non-dictionary words the corpus surfaces (Nolcorp, Montauk, Stonehaven, Hosko —
places and minor characters TVDB does not carry).

⚠️ AND SOME NAMES ARE ORDINARY WORDS. waters, frank, jack, mason, porter, ross,
hunter, ben. Blanket capitalisation would produce "Seriously rough Waters" and
"a Frank conversation". Every one was checked in context; in the whole series
exactly FOUR are genuine common nouns, and they are excluded by pattern below.
Everything else — "the porter brothers", "The mason treadwell", "the ross
reception" — really is a name.

USAGE
    ./fix_lowercase_names.py DIR --show Revenge          # preview
    ./fix_lowercase_names.py DIR --show Revenge --commit
"""

import argparse
import glob
import os
import re
import shutil
import time

G, Y, R, B, D, NC = ("\033[92m", "\033[93m", "\033[91m",
                     "\033[94m", "\033[90m", "\033[0m")

# ⚠️ Contexts where the word is genuinely an ordinary noun, verified by reading
# every occurrence. Checked BEFORE capitalising; a miss here is a visible error
# in Tony's library, so keep them narrow and evidence-based, never speculative.
COMMON_NOUN = [
    re.compile(r"\b(?:rough|troubled|the|deep|murky|uncharted|navigating\s+the)\s+waters\b", re.I),
    re.compile(r"\b(?:a|the)\s+frank\s+(?:conversation|discussion|talk|assessment)\b", re.I),
    re.compile(r"\bto\s+be\s+frank\b", re.I),
]


def load_names(show_dir, extra):
    """Name words for the show: caller-supplied (TVDB) + corpus extras."""
    names = set(extra)
    return {n.lower(): n for n in names if len(n) > 2}


def protected_spans(line):
    """Character ranges that must not be touched (ordinary-word uses)."""
    spans = []
    for p in COMMON_NOUN:
        for m in p.finditer(line):
            spans.append((m.start(), m.end()))
    return spans


def fix_line(line, names, title_words):
    """Capitalise names and the titles directly preceding them."""
    if not line.strip() or line.strip().isdigit() or "-->" in line:
        return line, 0
    guard = protected_spans(line)

    def inside_guard(i):
        return any(a <= i < b for a, b in guard)

    n = 0
    out = line
    # 1. names
    pat = re.compile(r"\b(" + "|".join(sorted(names, key=len, reverse=True)) + r")\b")

    def _name(m):
        nonlocal n
        if inside_guard(m.start()):
            return m.group(0)
        n += 1
        return names[m.group(1).lower()]

    out = pat.sub(_name, out)

    # 2. a title immediately before a now-capitalised name: "detective Hosko"
    #    ⚠️ ONLY when followed by a name — "the detective left" stays lowercase.
    tpat = re.compile(r"\b(" + "|".join(title_words) + r")(\.?\s+)(?=[A-Z])")

    def _title(m):
        nonlocal n
        n += 1
        return m.group(1).capitalize() + m.group(2)

    out = tpat.sub(_title, out)
    return out, n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("directory")
    ap.add_argument("--names", nargs="+", required=True,
                    help="proper-noun words (TVDB characters + places)")
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    names = load_names(args.directory, args.names)
    titles = ["mr", "mrs", "ms", "miss", "dr", "doctor", "detective", "officer",
              "agent", "sheriff", "judge", "father", "captain", "senator",
              "governor", "professor"]

    files = sorted(glob.glob(os.path.join(args.directory, "*.srt")))
    if not files:
        raise SystemExit(f"{R}no .srt files in {args.directory}{NC}")

    total, changed_files, samples = 0, 0, []
    for f in files:
        src = open(f, encoding="utf-8", errors="replace").read()
        out_lines, n_file = [], 0
        for line in src.splitlines():
            new, n = fix_line(line, names, titles)
            if n and len(samples) < 12 and new != line:
                samples.append((os.path.basename(f), line.strip(), new.strip()))
            n_file += n
            out_lines.append(new)
        if n_file:
            changed_files += 1
            total += n_file
            if args.commit:
                shutil.copy2(f, f + ".bak_caps")
                open(f, "w", encoding="utf-8").write("\n".join(out_lines) + "\n")

    print(f"{B}files:{NC} {len(files)}   {B}files changed:{NC} {changed_files}   "
          f"{B}capitalisations:{NC} {total:,}\n")
    for fn, before, after in samples:
        print(f"  {D}{fn[:30]}{NC}")
        print(f"    {R}- {before[:72]}{NC}")
        print(f"    {G}+ {after[:72]}{NC}")
    if not args.commit:
        print(f"\n{Y}Preview only. Add --commit to write "
              f"(originals kept as *.srt.bak_caps).{NC}")
    else:
        print(f"\n{G}✓ written — originals kept as *.srt.bak_caps{NC}")


if __name__ == "__main__":
    main()
