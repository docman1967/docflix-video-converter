"""Pre-flag OCR cues that look misread, for the review pane.

⚠️⚠️ **PROPOSES, NEVER FILTERS.** Every cue stays in the list; a flag only
colours the row and fills the Note column. Hiding a cue Tony would have caught
is the one unforgivable failure here (docs/OCR_REVIEW_PANE.md). Same discipline
as the Forced Subtitle Editor's language scan, which says "Norwegian 87%" and
gets a vote, never a veto.

⚠️ **A heuristic that fires on good text is worse than no heuristic**, because
it trains you to ignore the column — and then the one real flag goes past
unread. So everything here is measured against real subtitles before it ships,
and the tuning notes stay in the docstrings rather than in a commit message
nobody will find again.

Three checks, in descending order of precision:

1. ``check_confusions``  — a non-dictionary word that becomes a dictionary word
   after exactly ONE classic OCR substitution (l/1/I, 0/O, rn→m, cl→d). This is
   the high-precision one: it does not merely say "unknown word", it names the
   correction. `Weil` -> `Well`, `dinosaurs` untouched.
2. ``check_nondictionary`` — a word in neither the dictionary nor the 1.1M-name
   DB. Broader, noisier, and full of traps (see the warnings on that function).
3. ``check_width_ratio`` — the OCR text is far too short for how wide the
   bitmap was. Catches partial reads that produce plausible-looking words.
"""
import os
import re

# ── Dictionary ──────────────────────────────────────────────────────────────
# Both spellings, unioned. Tony's library is a mix of US and UK releases, and
# WhisperX gets Commonwealth spelling right unprompted on Australian accents
# (favour, jeopardise) — flagging those as misreads would be pure noise.
_DICT_PATHS = (
    '/usr/share/dict/american-english',
    '/usr/share/dict/british-english',
    '/usr/share/dict/words',
)

_words = set()
_words_loaded = False

# ⭐ Words the wordlist carries ONLY in capitalised form — 9,639 of them:
# proper nouns and acronyms (NORAD, FBI, CIA, NASA, IBM, JFK, NAFTA).
# ⚠️ THE CASE INFORMATION IS LOAD-BEARING AND WAS BEING THROWN AWAY. Storing
# every entry lowercased made `norad`, `fbi` and `cia` look like ordinary
# words, so a wrongly-lowercased acronym could never be flagged. Tony,
# 2026-09-13: "I'd rather have them flagged and be right than not flagged and
# be wrong." Keeping the caps-only set is what makes that possible.
_caps_only = set()          # stored UPPERCASED, e.g. {'NORAD', 'FBI'}

# ⚠️ CONTRACTION FRAGMENTS. Splitting "didn't" on the apostrophe leaves "didn",
# which is in NO dictionary — and worse, IS in the surname DB, so the names
# lookup does not rescue it either ([[reference_caps-filter-and-names]]).
# Left alone, every contraction in every subtitle flags. This is the single
# biggest false-positive source in the whole file.
_CONTRACTION_TAILS = {
    's', 't', 're', 've', 'll', 'd', 'm', 'em', 'n', 'clock', 'til', 'tis',
}
_CONTRACTION_STEMS = {
    'didn', 'don', 'doesn', 'isn', 'wasn', 'aren', 'weren', 'hasn', 'haven',
    'hadn', 'won', 'wouldn', 'couldn', 'shouldn', 'can', 'cannot', 'ain',
    'mustn', 'needn', 'shan', 'mightn', 'oughtn', 'daren', 'y', 'ya', 'gon',
    'gonna', 'wanna', 'gotta', 'lemme', 'gimme', 'kinda', 'sorta', 'outta',
    'dunno', 'c', 'o', 'ol', 'em', 'im', 'til', 'tis', 'twas', 'cause',
}

# Words that are ordinary in dialogue and absent from a 1922-vintage wordlist.
# ⚠️ Grown ONLY from measured false positives on Tony's own library, never
# from imagination — an invented list is how a filter starts hiding real
# errors. Add to it when a measurement shows a miss, not when one seems likely.
_DIALOGUE_EXTRAS = {
    'okay', 'yeah', 'yep', 'nope', 'hey', 'huh', 'hmm', 'mmm', 'uh', 'um',
    'ah', 'oh', 'ooh', 'whoa', 'wow', 'ugh', 'shh', 'psst', 'aw', 'eh',
    'gosh', 'jeez', 'geez', 'wanna', 'gonna', 'gotta', 'ya', 'yah', 'nah',
    'mom', 'mum', 'dad', 'grandpa', 'grandma', 'sir', 'ma', 'mister',
    'email', 'online', 'website', 'internet', 'phone', 'tv', 'ok',
    'guys', 'kinda', 'sorta', 'dunno', 'lemme', 'gimme', 'hi', 'bye',
}


def load_dictionary():
    """Load the system wordlist(s). Returns the word count. Idempotent.

    ⚠️ Keeps TWO sets. `_words` is everything lowercased (for ordinary
    lookups), and `_caps_only` holds entries that appear ONLY capitalised —
    the proper nouns and acronyms. Without the second set a lowercase `norad`
    matches `NORAD` and nothing can tell that the case is wrong.
    """
    global _words, _words_loaded, _caps_only
    if _words_loaded:
        return len(_words)
    words = set(_DIALOGUE_EXTRAS)
    seen_lower = set()
    seen_caps = set()
    for path in _DICT_PATHS:
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding='utf-8', errors='ignore') as f:
                for line in f:
                    w = line.strip()
                    if not w:
                        continue
                    # Keep the possessive/contracted forms the list carries
                    # ("won't", "Tony's") by storing them lowercased whole.
                    words.add(w.lower())
                    if "'" in w:
                        words.add(w.split("'")[0].lower())
                    if w.isalpha():
                        (seen_caps if w[0].isupper() else seen_lower).add(w)
        except Exception:
            continue
    _words = words
    # ⚠️ ACRONYMS ONLY — entries that are wholly UPPERCASE in the wordlist,
    # with no lowercase twin. Including merely Capitalised entries (English,
    # Monday, Paris) breaks it: `english` would be "corrected" to `ENGLISH`,
    # which is not the right answer even though the lowercase IS wrong.
    # Restricting to isupper() keeps the suggestion exactly right or absent.
    _caps_only = {w.upper() for w in seen_caps
                  if w.isupper() and len(w) >= 2
                  and w.lower() not in seen_lower
                  and w.lower() not in _DIALOGUE_EXTRAS}
    _words_loaded = bool(words)
    return len(_words)


# ⚠️ Grown ONLY from measured false positives on Tony's own library, never
# from imagination. Each of these is in the wordlist as an acronym but reads
# perfectly naturally in lowercase in a subtitle:
#   www / http  — web addresses are lowercase by convention
#   pow         — a comic-book sound effect far more often than a prisoner
#   ing         — a Dutch bank; here, always a word fragment
#   pac         — part of a rapper's name, not an acronym
#   ira / isis  — genuine given names (Ira, Isis) as often as organisations
_NOT_ACRONYMS = {'www', 'http', 'https', 'pow', 'ing', 'pac', 'ira', 'isis'}

_LOWER_TOKEN_RE = re.compile(r"\b[a-z]{2,}\b")


def lowercase_tokens(text):
    """Whole all-lowercase words in the *prose* part of a cue.

    ⚠️ WORD BOUNDARIES ARE LOAD-BEARING. Without `\\b` this matches the
    lowercase RUN inside a mixed-case word — "Set" yields "et", which is a
    real acronym (ET) and produced a confident nonsense flag on the lyric
    "♪ Set me free ♪".

    ⚠️ Runs on _prose_of, so ♪ lines, [HI] and speaker labels are skipped —
    the same exclusions every other check here uses. Duplicating the scan
    without them is how one check starts disagreeing with its neighbours.
    """
    return _LOWER_TOKEN_RE.findall(_prose_of(text))


def get_caps_only():
    """The acronym set. ⚠️ ALWAYS call this, never import `_caps_only`.

    load_dictionary() REBINDS the module global, so a `from ... import
    _caps_only` captures the empty set it had at import time and never
    updates — the same trap that cost a full 371k-cue measurement pass with
    the names DB earlier today. Arthur walked straight into it a second time
    within the hour while testing this very function.
    """
    if not _words_loaded:
        load_dictionary()
    return _caps_only


def wrongly_lowercased(word):
    """If *word* is the lowercase form of a caps-only entry, return that form.

    `norad` -> `NORAD`, `fbi` -> `FBI`. An exact wordlist lookup rather than a
    heuristic, so it has no tuning and no false-positive band to manage.

    ⭐ WHY IT MATTERS. Fix ALL CAPS lowercases anything it does not recognise,
    and on a line that reads as shouting an acronym is exactly what it fails to
    recognise: "NASA and NORAD." -> "NASA and norad.". Nothing downstream could
    see that, because `norad` looked like a dictionary word.

    ⚠️ Only fires on an ALL-LOWERCASE token. "Norad" mid-sentence is left
    alone — that is a plausible proper noun and second-guessing capitalisation
    is not this function's job.
    """
    if not _words_loaded:
        load_dictionary()
    w = word.strip("'’")
    if not w or not w.isalpha() or not w.islower():
        return None
    # ⚠️ THREE CHARACTERS MINIMUM. Measured over 368,196 library cues:
    #     min 2 -> 1 hit in 676 cues, almost all wrong
    #     min 3 -> 1 hit in 5,844
    #     min 4 -> 1 hit in 73,639 (but loses fbi, cia, atf — the whole point)
    # The two-letter set is dominated by US STATE CODES (DE, AL, CO, NE, VA,
    # WA, IL, RI) which collide with ordinary fragments and foreign words:
    # `de`, `da`, `er`, `al`, `un`, `se`, `ne`. 114 of the 416 known acronyms
    # are two letters and they are collectively worthless here.
    if len(w) < 3:
        return None
    if w in _NOT_ACRONYMS:
        return None
    if w.upper() in _caps_only:
        return w.upper()
    return None


def is_dictionary_word(word):
    """True if *word* is known. Handles contractions and possessives."""
    if not _words_loaded:
        load_dictionary()
    w = word.lower().strip("'")
    if not w:
        return True
    if w in _words:
        return True
    # "didn't" -> stem "didn" + tail "t"
    if "'" in w:
        stem, _, tail = w.rpartition("'")
        if stem in _CONTRACTION_STEMS or stem in _words:
            if tail in _CONTRACTION_TAILS or not tail:
                return True
        # possessive on a known noun/name
        if tail == 's' and (stem in _words or stem in _CONTRACTION_STEMS):
            return True
    if w in _CONTRACTION_STEMS:
        return True
    # simple plural / past of a known word
    for suffix in ("s", "es", "ed", "ing", "'s"):
        if w.endswith(suffix) and w[:-len(suffix)] in _words:
            return True
    return False


# ── Tokenising ──────────────────────────────────────────────────────────────
# ⚠️ Keep the apostrophe INSIDE the token. Splitting on it is what creates the
# "didn" problem above. Hyphens split, because OCR routinely produces
# "Cham--" and each side deserves judging on its own.
_TOKEN_RE = re.compile(r"[A-Za-zÀ-ÿ]+(?:['’][A-Za-zÀ-ÿ]+)*")

# ⚠️ Bracketed HI text, speaker labels and ♪ lines are NOT ordinary prose and
# must be skipped wholesale: [DOOR SLAMS], MAN:, ♪ lyrics ♪. Judging them as
# dictionary text flags a large slice of every SDH track — and SDH is most of
# what Tony OCRs.
_SKIP_LINE_RE = re.compile(r'^\s*[\[(].*[\])]\s*$')
_SPEAKER_RE = re.compile(r'^\s*[-—]?\s*[A-Z][A-Z .\'#0-9-]{1,24}:\s*')
_TAG_RE = re.compile(r'<[^>]+>|\{[^}]*\}')


def _prose_of(text):
    """The part of a cue worth spell-checking, or '' if none."""
    if not text:
        return ''
    out = []
    for line in text.splitlines():
        line = _TAG_RE.sub('', line)
        if _SKIP_LINE_RE.match(line):
            continue                      # a whole line of [SOUND EFFECT]
        if '♪' in line:
            continue                      # lyrics: names, ad-libs, nonsense
        line = _SPEAKER_RE.sub('', line)  # strip "MAN:" but keep the speech
        # drop any inline [bracketed] or (parenthetical) aside
        line = re.sub(r'[\[(][^\])]*[\])]', ' ', line)
        out.append(line)
    return '\n'.join(out)


def words_of(text):
    """Tokens worth judging: alphabetic, 4+ chars, not ALL CAPS."""
    words = []
    for m in _TOKEN_RE.finditer(_prose_of(text)):
        w = m.group(0)
        # ⚠️ 3 chars and under are skipped. Short tokens are where OCR noise
        # and legitimate interjections are indistinguishable ("Ow", "Mm",
        # "Er"), and they were the bulk of the noise when measured.
        if len(w.strip("'’")) < 4:
            continue
        # ALL CAPS is a shout or an unstripped label, not prose.
        if w.isupper():
            continue
        words.append(w)
    return words


# ── 1. OCR confusion pairs (highest precision) ──────────────────────────────
# ⚠️ Directional and single-application. Applying several at once turns almost
# any string into some dictionary word, which destroys the precision that makes
# this check worth having.
_CONFUSIONS = (
    ('l', 'I'), ('I', 'l'), ('l', '1'), ('1', 'l'), ('I', '1'), ('1', 'I'),
    ('0', 'o'), ('o', '0'), ('0', 'O'), ('O', '0'),
    ('rn', 'm'), ('m', 'rn'), ('cl', 'd'), ('d', 'cl'),
    ('vv', 'w'), ('w', 'vv'), ('nn', 'm'),
    ('5', 'S'), ('S', '5'), ('8', 'B'), ('B', '8'), ('6', 'G'), ('2', 'Z'),
    ('ii', 'n'), ('li', 'h'), ('ri', 'n'), ('tl', 'd'),
)


def one_substitution_fixes(word):
    """If ONE classic OCR substitution turns *word* into a real word, name it.

    Returns the corrected word, or None. This is the check worth trusting:
    it does not say "I don't know this word", it says "this is `Well` with the
    second `l` read as an `i`" — which is checkable at a glance against the
    bitmap sitting next to it.
    """
    if is_dictionary_word(word):
        return None
    for a, b in _CONFUSIONS:
        start = 0
        while True:
            i = word.find(a, start)
            if i < 0:
                break
            cand = word[:i] + b + word[i + len(a):]
            if cand != word and is_dictionary_word(cand):
                return cand
            start = i + 1
    return None


def _is_name(word, names):
    if not names:
        return False
    bare = word.strip("'’")
    return bare.capitalize() in names or bare in names


# ⚠️ A CLOSED SET, and it has to stay closed. These are words that never begin
# an English compound, which is the entire reason this check has any precision.
_FUNCTION_WORDS = {
    'we', 'you', 'they', 'the', 'that', 'this', 'what', 'when', 'where',
    'there', 'here', 'and', 'but', 'for', 'with', 'from', 'have', 'was',
    'were', 'will', 'would', 'could', 'should', 'it', 'he', 'she', 'who',
    'why', 'how', 'are', 'his', 'her', 'them', 'their', 'been', 'then',
    'than', 'into', 'your', 'our',
}


def splits_into_words(word):
    """If *word* is a function word run into the next one, return it spaced.

    `Wejust` -> `We just`, `foryou` -> `for you`. OCR drops the space when two
    words nearly touch in the bitmap, producing a single unknown token that
    reads like a typo rather than a misread.

    ⭐ FOUND BY MEASURING, not by guessing — it surfaced in the residue of the
    broad non-dictionary check while working out what that check uniquely
    contributed. The check that was not worth shipping earned its keep by
    pointing at one that was.

    ⚠️⚠️ THE FIRST PART MUST BE A FUNCTION WORD. The obvious version — "splits
    into any two dictionary words" — was measured at **1 in 67 cues and almost
    entirely wrong**, because English compounds are everywhere: `Aquaman` ->
    `Aqua man`, `Batmobile` -> `Bat mobile`, `thrusters` -> `thrust ers`,
    `somethin` -> `some thin`. Restricting the first part to words that never
    begin a compound took it to **1 in 856 with only two visible false
    positives in the top 25** (`Hedare`, a proper noun, and `thataway`).

    That is a 13x difference in noise from one constraint. Do not loosen it,
    and do not add compound-capable words like `over`, `under` or `out` to
    _FUNCTION_WORDS — that is precisely the change that collapses it.
    """
    if is_dictionary_word(word):
        return None
    w = word.strip("'’")
    if not w.isalpha() or len(w) < 6:
        return None
    for i in range(2, len(w) - 2):
        a, b = w[:i], w[i:]
        if a.lower() in _FUNCTION_WORDS and len(b) >= 3 \
                and is_dictionary_word(b):
            return f"{a} {b}"
    return None


def check_confusions(text, names=None):
    """[(word, suggestion), ...] for words one OCR slip from a real word.

    ⚠️ PASS THE NAMES DB. Measured over 371,234 library cues without it, the
    check "corrected" real surnames into other words — `Arnie`->`Amie`,
    `Henning`->`Heming`, `Renn`->`Rem`, all via rn->m. Those were the bulk of
    the false positives, and every one of them is a name sitting in the 1.1M
    DB. Without `names` this still works but is markedly noisier.
    """
    hits = []
    for w in words_of(text):
        if _is_name(w, names):
            continue
        fix = one_substitution_fixes(w)
        if fix:
            hits.append((w, fix))
    return hits


# ── 2. Non-dictionary words (broader, noisier) ──────────────────────────────

def check_nondictionary(text, names=None):
    """Words in neither the dictionary nor the names DB.

    ⚠️⚠️ THE NAMES-DB TRAP. The 1.1M-name DB is what stops every proper noun
    flagging — but it also contains contraction fragments as real surnames
    (`didn`, `wasn`, `aren` are all attested), so a naive lookup SUPPRESSES
    flags it should raise ([[reference_caps-filter-and-names]]). That is why
    contractions are handled in the tokeniser and never reach this lookup.

    ⚠️ Capitalised words are only excused when the names DB is loaded. With no
    DB, excusing every capitalised word would blind the check to misread proper
    nouns — and not excusing them floods it. Loaded DB is the supported path.
    """
    hits = []
    for w in words_of(text):
        if is_dictionary_word(w) or _is_dropped_g(w):
            continue
        if _is_name(w, names):
            continue
        hits.append(w)
    return hits


def _is_dropped_g(word):
    """`goin` / `doin'` / `somethin` — dialect, not a misread.

    ⚠️ Measured: dropped-g forms were 5 of the top 12 non-dictionary hits
    across 371k library cues (goin 279, doin 237, comin 117, gettin 105,
    somethin 103). They are spelled deliberately and flagging them is pure
    noise. Note the apostrophe is often ABSENT in real subtitles, so the
    tokeniser cannot catch these on punctuation alone.
    """
    w = word.lower().strip("'’")
    # ⚠️ >= 4, not > 4. `goin` and `doin` are both exactly 4 characters and
    # were the two most common dropped-g forms in the corpus (279 and 237).
    # An off-by-one here left the single biggest noise source unfixed.
    return w.endswith('in') and len(w) >= 4 and is_dictionary_word(w + 'g')


def suspect_words_in_cues(cues, names=None):
    """{cue index -> [words]} for non-dictionary words that occur ONCE.

    ⭐ THE REFINEMENT THAT MAKES THIS CHECK USABLE. Per-cue, "not in the
    dictionary" fires on 1 cue in 8 — unusable next to flags measured at 1 in
    4,000. Almost all of it is the show's own vocabulary: `Aquaman` (432),
    `Superfriends` (240), `UnSub` (341), `Hotch` (135). Invented proper nouns
    are exactly what a dictionary cannot know and what a names DB will not
    have.

    But they RECUR. A show says `Hotch` a hundred times an episode; an OCR
    misread of a real word is almost always a one-off. So the signal is not
    "unknown word", it is **"unknown word that appears exactly once in this
    file"** — which needs the whole cue list, not one cue.

    ⚠️ Consequence worth knowing: a word misread the SAME wrong way every time
    will not flag. That is a real blind spot and an acceptable one — a
    consistent misreading is visible on the first cue Tony reads, whereas the
    one-off is the kind that slips past.
    """
    from collections import Counter, defaultdict
    counts = Counter()
    per_cue = defaultdict(list)
    for i, c in enumerate(cues):
        for w in check_nondictionary(c.get('text') or '', names):
            key = w.lower().strip("'’")
            counts[key] += 1
            per_cue[i].append((w, key))
    out = {}
    for i, pairs in per_cue.items():
        once = [w for w, key in pairs if counts[key] == 1]
        if once:
            out[i] = once
    return out


# ── 3. Text far too short for the bitmap ────────────────────────────────────

def check_width_ratio(text, img_w, min_width=260, max_px_per_char=120):
    """True if *img_w* pixels produced implausibly few characters.

    A wide bitmap that yields three characters is a PARTIAL read — and unlike
    an empty result it looks like a perfectly good cue, so nothing downstream
    will ever question it.

    ⚠️ THRESHOLD IS MEASURED, NOT GUESSED. Over all 1,278 cues of Warehouse 13
    S01E01 (real PGS, after the inversion fix):

        median  49.9 px/char
        p95     73.0 px/char
        MAX     84.7 px/char   ('No?' in a 254px bitmap)

    So correct OCR never exceeds ~85. The default of 120 sits ~40% above the
    worst legitimate case and flagged **0 of 1,278**. At 80 it flags three good
    cues ('No?', 'Ow!', 'Wow.'); at 60 it flags 1 cue in 5 and is worthless.
    ⛔ Do not tighten this below ~100 without re-measuring on a real episode.

    ⚠️ Like flag_lost, this is a net you hope never fires. Zero hits on a clean
    episode is the expected result, not evidence it is broken.

    ⚠️ min_width exists because a narrow bitmap legitimately holds one short
    word and the ratio means nothing there — 'No?' at 254px is the widest
    per-character case in the whole episode and sits just under it.
    """
    if not img_w or img_w < min_width:
        return False
    n = len((text or '').strip())
    if n == 0:
        return False                # that is the empty case, flagged elsewhere
    return (img_w / n) > max_px_per_char
