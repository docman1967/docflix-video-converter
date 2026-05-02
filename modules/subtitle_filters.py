"""
Docflix Media Suite — Subtitle Filters and SRT Utilities

SRT parsing/writing, all subtitle filter functions (Remove HI, Fix CAPS,
Remove Tags, Remove Ads, etc.), and timing manipulation utilities.

All filter functions accept a list of cue dicts and return a new list.
Each cue dict: {'index': int, 'start': str, 'end': str, 'text': str}
"""

import re


# ═══════════════════════════════════════════════════════════════════
# SRT Parsing / Writing
# ═══════════════════════════════════════════════════════════════════

def parse_srt(text):
    """Parse SRT subtitle text into a list of cue dicts.

    Each cue: {'index': int, 'start': str, 'end': str, 'text': str}
    Timestamps are kept as original strings (HH:MM:SS,mmm).
    """
    cues = []
    blocks = re.split(r'\n\n+', text.strip())
    for block in blocks:
        lines = block.strip().split('\n')
        if len(lines) < 2:
            continue
        # First line should be the index number
        try:
            idx = int(lines[0].strip())
        except ValueError:
            # Sometimes the index is missing; generate one
            idx = len(cues) + 1
            # Try parsing this line as timestamp instead
            if '-->' in lines[0]:
                lines = ['0'] + lines  # dummy index
                idx = len(cues) + 1
            else:
                continue
        # Second line: timestamp
        ts_match = re.match(
            r'(\d{2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,.]\d{3})',
            lines[1].strip()
        )
        if not ts_match:
            continue
        start, end = ts_match.group(1), ts_match.group(2)
        # Remaining lines: subtitle text
        sub_text = '\n'.join(lines[2:])
        cues.append({'index': idx, 'start': start, 'end': end,
                     'text': sub_text})
    return cues


def write_srt(cues):
    """Convert a list of cue dicts back to SRT format string."""
    parts = []
    for i, cue in enumerate(cues, 1):
        parts.append(
            f"{i}\n{cue['start']} --> {cue['end']}\n{cue['text']}\n")
    return '\n'.join(parts)


# ═══════════════════════════════════════════════════════════════════
# Timestamp Conversion
# ═══════════════════════════════════════════════════════════════════

def srt_ts_to_ms(ts):
    """Convert SRT timestamp string 'HH:MM:SS,mmm' to milliseconds."""
    ts = ts.replace(',', '.').replace(';', '.')
    parts = ts.split(':')
    h, m = int(parts[0]), int(parts[1])
    s_parts = parts[2].split('.')
    s = int(s_parts[0])
    ms = int(s_parts[1]) if len(s_parts) > 1 else 0
    return (h * 3600 + m * 60 + s) * 1000 + ms


def ms_to_srt_ts(ms):
    """Convert milliseconds to SRT timestamp string 'HH:MM:SS,mmm'."""
    if ms < 0:
        ms = 0
    h = ms // 3600000
    ms %= 3600000
    m = ms // 60000
    ms %= 60000
    s = ms // 1000
    ms %= 1000
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


# ═══════════════════════════════════════════════════════════════════
# ALL CAPS HI Detection (shared by multiple filters)
# ═══════════════════════════════════════════════════════════════════

# Single-word HI terms that should be removed even as standalone words
_CAPS_HI_SINGLE_WORDS = {
    'applause', 'laughter', 'laughing', 'laughs', 'chuckling', 'chuckles',
    'giggling', 'giggles', 'snickering', 'sniggering',
    'screaming', 'screams', 'shrieking', 'shrieks', 'shriek',
    'crying', 'cries', 'sobbing', 'sobs', 'weeping', 'weeps',
    'gasping', 'gasps', 'groaning', 'groans', 'moaning', 'moans',
    'sighing', 'sighs', 'panting', 'pants',
    'coughing', 'coughs', 'sneezing', 'sneezes', 'sniffing', 'sniffs',
    'whispering', 'whispers', 'muttering', 'mutters', 'mumbling',
    'mumbles',
    'shouting', 'shouts', 'yelling', 'yells', 'exclaiming', 'exclaims',
    'stuttering', 'stutters', 'stammering', 'stammers',
    'silence', 'inaudible', 'indistinct', 'unintelligible',
    'music', 'singing', 'humming', 'whistling', 'chanting',
    'cheering', 'cheers', 'booing', 'boos', 'jeering',
    'thunder', 'explosion', 'gunshot', 'gunshots', 'gunfire',
    'sirens', 'alarm', 'buzzing', 'ringing', 'beeping', 'bleeping',
    'knocking', 'banging', 'crashing', 'thudding', 'thumping',
    'squeaking', 'creaking', 'rustling', 'clattering', 'rattling',
    'splashing', 'dripping', 'sizzling', 'bubbling',
    'doorbell', 'telephone', 'ringtone',
    'snoring', 'yawning', 'hiccupping', 'hiccups', 'belching',
    'retching',
    'growling', 'barking', 'howling', 'whimpering', 'purring',
    'meowing',
    'neighing', 'chirping', 'squawking',
    'clapping', 'footsteps', 'static', 'feedback', 'interference',
    'continues', 'resumes', 'stops', 'ends', 'fades',
}

# Acronyms and short words to always preserve (even if all-caps line)
_CAPS_HI_KEEP_WORDS = {
    # Common acronyms
    'ok', 'okay', 'no', 'oh', 'hi', 'hey', 'yes', 'yeah', 'god', 'oi',
    'ha', 'ah', 'uh', 'hm', 'mm', 'sh', 'shh', 'psst', 'wow', 'boo',
    # Known acronyms / initialisms
    'fbi', 'cia', 'nsa', 'dea', 'atf', 'nypd', 'lapd', 'swat',
    'nasa', 'nato', 'un', 'eu', 'uk', 'usa', 'us',
    'bbc', 'itv', 'cnn', 'nbc', 'cbs', 'abc', 'hbo', 'pbs', 'nhs',
    'ceo', 'cfo', 'cto', 'vip', 'rip', 'awol', 'mia', 'pow',
    'dna', 'hiv', 'aids', 'icu', 'cpr', 'gps', 'eta', 'asap',
    'tv', 'pc', 'dj', 'mc', 'id', 'iq', 'phd', 'md',
    'mph', 'rpm', 'atm', 'suv', 'ufo', 'aka',
    'nyc', 'la', 'dc', 'sf',
}


def _is_caps_hi_line(line):
    """Determine if a line is an ALL CAPS HI description.

    Multi-word all-caps lines (2+ words) are removed.
    Single all-caps words are only removed if they match known HI
    keywords. Short words (<=3 chars) and known acronyms are always
    kept.
    """
    stripped = line.strip()
    if not stripped:
        return False

    # Remove leading dash/hyphen for analysis
    clean = re.sub(r'^-\s*', '', stripped)
    if not clean:
        return False

    # Get just the letter content to check if it's all uppercase
    letters = re.sub(r'[^a-zA-Z]', '', clean)
    if not letters:
        return False

    # Must be ALL CAPS (every letter is uppercase)
    if not letters.isupper():
        return False

    # Split into words
    words = clean.split()

    # Single word — only remove if it's a known HI keyword
    if len(words) == 1:
        word_lower = letters.lower()
        if len(letters) <= 3:
            return False
        if word_lower in _CAPS_HI_KEEP_WORDS:
            return False
        return word_lower in _CAPS_HI_SINGLE_WORDS

    # Multi-word all-caps line
    # Check if ALL words are known acronyms/keep-words — if so, preserve
    all_words_lower = [re.sub(r'[^a-z]', '', w.lower()) for w in words]
    all_words_lower = [w for w in all_words_lower if w]
    if all_words_lower and all(
            w in _CAPS_HI_KEEP_WORDS for w in all_words_lower):
        return False

    # Multi-word all-caps line that isn't all acronyms -> remove
    return True


def _build_caps_hi_checker():
    """Return the _is_caps_hi_line function for use by other filters."""
    return _is_caps_hi_line


# ═══════════════════════════════════════════════════════════════════
# Filter Functions
# ═══════════════════════════════════════════════════════════════════

def filter_remove_hi(cues):
    """Remove hearing-impaired annotations and speaker labels.

    Removes: [brackets], (parentheses), speaker labels (Name:),
    and ALL CAPS HI descriptor labels (HIGH-PITCHED:, MUFFLED:, etc.)
    """
    hi_patterns = [
        re.compile(r'\[.*?\]', re.DOTALL),
        re.compile(r'^\[(?!.*\]).*', re.DOTALL),
        re.compile(r'\(.*?\)', re.DOTALL),
    ]
    speaker_pattern = re.compile(
        r'^(-?\s*)[A-Za-z][A-Za-z\s\d\'\.]{0,29}[A-Za-z]:\s*\n?', re.MULTILINE)
    caps_hi_label = re.compile(
        r'^(-?\s*)(?:[A-Z]{4,}|[A-Z][A-Z\-]*-[A-Z\-]*)'
        r'(?:\s+(?:[A-Z]{4,}|[A-Z][A-Z\-]*-[A-Z\-]*))*:\s*',
        re.MULTILINE)

    def _speaker_replace(m):
        label = m.group(0).lstrip('- ')
        name_part = label.split(':')[0].strip()
        if re.match(r'^\d+$', name_part):
            return m.group(0)
        if len(name_part) <= 1:
            return m.group(0)
        return m.group(1)

    caps_hi_checker = _build_caps_hi_checker()

    result = []
    for cue in cues:
        text = cue['text']
        text = caps_hi_label.sub(r'\1', text)
        for pat in hi_patterns:
            text = pat.sub('', text)
        text = speaker_pattern.sub(_speaker_replace, text)
        lines = text.split('\n')
        lines = [line for line in lines if not caps_hi_checker(line)]
        text = '\n'.join(lines)
        text = re.sub(
            r'^(-?\s*)[A-Za-z][A-Za-z\s\d\'\.]{0,29}[A-Za-z]\s+:\s*', r'\1',
            text, flags=re.MULTILINE)
        text = re.sub(r'^\s*:\s*', '', text, flags=re.MULTILINE)
        text = re.sub(r'\n\s*:\s*', '\n', text)
        text = re.sub(r'^(-\s*):\s*', r'\1', text, flags=re.MULTILINE)
        text = re.sub(r'^\s*-?\s*$', '', text, flags=re.MULTILINE)
        text = re.sub(r'\n{2,}', '\n', text)
        text = re.sub(r'^\n+', '', text)
        text = text.strip()
        if text:
            result.append({**cue, 'text': text})
    return result


def filter_remove_caps_hi(cues):
    """Remove ALL CAPS hearing-impaired descriptions (UK style).

    Targets entire lines that are ALL CAPS and describe actions or
    sounds, e.g.: 'SHEENA LAUGHS', 'DOOR SLAMS SHUT', 'APPLAUSE'.
    """
    result = []
    for cue in cues:
        lines = cue['text'].split('\n')
        kept_lines = [line for line in lines
                      if not _is_caps_hi_line(line)]
        text = '\n'.join(kept_lines).strip()
        text = re.sub(r'^\s*-?\s*$', '', text, flags=re.MULTILINE)
        text = re.sub(r'\n{2,}', '\n', text).strip()
        if text:
            result.append({**cue, 'text': text})
    return result


def filter_remove_music_notes(cues):
    """Remove cues that contain only music note symbols.

    Keeps cues that have actual lyrics or dialogue alongside the notes.
    """
    result = []
    for cue in cues:
        stripped = re.sub(r'[♪♫\s\-]', '', cue['text'])
        if stripped:
            result.append(cue)
    return result


# ── Proper nouns for case conversion ──
PROPER_NOUNS = {
    # Days
    'monday', 'tuesday', 'wednesday', 'thursday', 'friday',
    'saturday', 'sunday',
    # Months
    'january', 'february', 'march', 'april', 'may', 'june',
    'july', 'august', 'september', 'october', 'november', 'december',
    # Holidays
    'christmas', 'easter', 'halloween', 'thanksgiving', 'hanukkah',
    'kwanzaa', 'valentines', "valentine's", 'ramadan', 'diwali',
    'passover', 'new year', "new year's", "mother's", "father's",
    # Countries (common)
    'america', 'american', 'americans', 'england', 'english',
    'france', 'french', 'germany', 'german', 'italy', 'italian',
    'spain', 'spanish', 'china', 'chinese', 'japan', 'japanese',
    'russia', 'russian', 'canada', 'canadian', 'mexico', 'mexican',
    'australia', 'australian', 'india', 'indian', 'brazil',
    'brazilian',
    'korea', 'korean', 'ireland', 'irish', 'scotland', 'scottish',
    'africa', 'african', 'europe', 'european', 'asia', 'asian',
    'british', 'britain', 'uk', 'usa',
    # US States (common in dialogue)
    'california', 'texas', 'florida', 'new york', 'york',
    'new jersey', 'jersey',
    'massachusetts', 'virginia', 'carolina', 'georgia', 'ohio',
    'michigan', 'illinois', 'pennsylvania', 'arizona', 'colorado',
    'washington', 'oregon', 'nevada', 'hawaii', 'alaska',
    'montana', 'connecticut', 'louisiana', 'tennessee', 'kentucky',
    'minnesota', 'mississippi', 'alabama', 'oklahoma', 'wisconsin',
    'maryland', 'missouri',
    # Cities (common)
    'london', 'paris', 'tokyo', 'beijing', 'moscow', 'berlin',
    'rome', 'madrid', 'sydney', 'toronto', 'chicago', 'boston',
    'miami', 'seattle', 'dallas', 'denver', 'atlanta', 'detroit',
    'houston', 'phoenix', 'vegas', 'portland', 'hollywood',
    'manhattan', 'brooklyn', 'queens', 'bronx', 'harlem',
    # Common abbreviations / titles
    'mr', 'mrs', 'ms', 'dr', 'jr', 'sr', 'st', 'mt',
    'ave', 'blvd', 'dept', 'sgt', 'cpl', 'pvt', 'lt', 'capt',
    'gen', 'col', 'cmdr', 'prof', 'rev', 'hon',
    # Address / place words
    'street', 'avenue', 'road', 'drive', 'lane', 'boulevard',
    'court', 'place', 'terrace', 'highway', 'parkway', 'plaza',
    'bridge', 'park', 'lake', 'river', 'mountain', 'island',
    'north', 'south', 'east', 'west',
    # Religious / cultural
    'god', 'jesus', 'christ', 'bible', 'catholic', 'christian',
    'muslim', 'islam', 'jewish', 'buddhist', 'hindu',
    # Other proper nouns common in subtitles
    'internet', 'facebook', 'google', 'twitter', 'instagram',
    'youtube', 'netflix', 'amazon', 'apple', 'microsoft',
    'fbi', 'cia', 'nsa', 'dea', 'atf', 'nypd', 'lapd',
    'nasa', 'nato', 'un', 'eu',
}


def filter_fix_caps(cues, custom_names=None):
    """Convert ALL CAPS subtitles to proper sentence case.

    - Lowercases everything first
    - Capitalizes first letter of each sentence/line
    - Capitalizes standalone "I" and contractions (I'm, I'll, etc.)
    - Capitalizes known proper nouns
    - Capitalizes custom names if provided
    """
    all_proper = set(PROPER_NOUNS)
    if custom_names:
        all_proper.update(w.lower() for w in custom_names)

    sorted_nouns = sorted(all_proper, key=len, reverse=True)
    phrases = [n for n in sorted_nouns if ' ' in n]

    def fix_case(text, cap_first=True):
        alpha = re.sub(r'[^a-zA-Z]', '', text)
        if not alpha:
            return text
        upper_ratio = sum(1 for c in alpha if c.isupper()) / len(alpha)
        if upper_ratio < 0.6:
            return text

        text = text.lower()

        lines = text.split('\n')
        capped_lines = []
        for idx, line in enumerate(lines):
            line = line.strip()
            if line:
                is_first_line = (idx == 0)
                prev_ended_sentence = (
                    idx > 0 and capped_lines
                    and re.search(
                        r'[.!?]["\'\u201d\u2019]?\s*$',
                        capped_lines[-1]))
                starts_with_dash = line.startswith('-')

                should_cap = starts_with_dash or prev_ended_sentence
                if is_first_line:
                    should_cap = cap_first or starts_with_dash

                if should_cap:
                    line = re.sub(
                        r'^(-\s*)?([a-z])',
                        lambda m: (m.group(1) or '') + m.group(2).upper(),
                        line)
            capped_lines.append(line)
        text = '\n'.join(capped_lines)

        text = re.sub(
            r'([.!?]["\'\u201d\u2019]?[\s]+)([a-z])',
            lambda m: m.group(1) + m.group(2).upper(), text)

        text = re.sub(r"\bi\b", "I", text)
        text = re.sub(
            r"\bi'(m|ll|ve|d|s)\b",
            lambda m: "I'" + m.group(1), text)

        for phrase in phrases:
            pattern = re.compile(re.escape(phrase), re.IGNORECASE)
            text = pattern.sub(phrase.title(), text)

        _ALLCAPS_ABBREVS = {
            'fbi', 'cia', 'nsa', 'dea', 'atf', 'nypd', 'lapd',
            'nasa', 'nato', 'un', 'eu', 'uk', 'usa', 'tv', 'dna',
            'ceo', 'cfo', 'cto', 'phd', 'md', 'dj', 'pc', 'id',
            'ok', 'ad', 'bc', 'ac', 'dc', 'hq',
        }

        def _cap_word(m):
            word = m.group(0)
            lower = word.lower()
            if lower in _ALLCAPS_ABBREVS:
                return lower.upper()
            if lower in all_proper:
                return word.capitalize()
            return word

        text = re.sub(r'\b[a-zA-Z]+\b', _cap_word, text)
        return text

    def apply_custom_names(text):
        if not custom_names:
            return text
        custom_phrases = [n for n in custom_names if ' ' in n]
        for phrase in custom_phrases:
            pattern = re.compile(re.escape(phrase), re.IGNORECASE)
            text = pattern.sub(phrase.title(), text)
        custom_single = {n.lower(): n for n in custom_names
                         if ' ' not in n}
        if custom_single:
            def _cap_custom(m):
                word = m.group(0)
                original = custom_single.get(word.lower())
                if original:
                    return original
                return word
            text = re.sub(r'\b[a-zA-Z]+\b', _cap_custom, text)
        return text

    result = []
    prev_text = ''
    for cue in cues:
        text = cue['text']
        prev_ended_sentence = (
            not prev_text
            or bool(re.search(
                r'[.!?]["\'\u201d\u2019]?\s*$', prev_text)))
        text = fix_case(text, cap_first=prev_ended_sentence)
        if prev_ended_sentence:
            text = re.sub(
                r'^(-\s*)?([a-z])',
                lambda m: (m.group(1) or '') + m.group(2).upper(), text)
        text = apply_custom_names(text)
        prev_text = text
        result.append({**cue, 'text': text})
    return result


def filter_remove_tags(cues):
    """Remove HTML/formatting tags: <i>, </i>, <b>, {\\an8}, etc."""
    tag_patterns = [
        re.compile(r'<[^>]+>'),
        re.compile(r'\{\\[^}]+\}'),
    ]
    result = []
    for cue in cues:
        text = cue['text']
        for pat in tag_patterns:
            text = pat.sub('', text)
        text = text.strip()
        if text:
            result.append({**cue, 'text': text})
    return result


# Built-in ad/credit patterns (always present)
BUILTIN_AD_PATTERNS = [
    r'subtitl(es|ed)\s+by\b.*',
    r'synced?\s*((&|and)\s*corrected)?\s+by\b.*',
    r'caption(s|ed|ing)?\s+by\b.*',
    r'translated\s+by\b.*',
    r'corrections?\s+by\b.*',
    r'encoded\s+by\b.*',
    r'ripped\s+by\b.*',
    r'opensubtitles\S*',
    r'addic7ed\S*',
    r'subscene\S*',
]


def filter_remove_ads(cues, custom_patterns=None):
    """Remove common ad/credit lines from subtitles.

    URL lines are only removed if the cue also contains another ad
    indicator.
    """
    all_pattern_strs = list(BUILTIN_AD_PATTERNS)
    if custom_patterns:
        all_pattern_strs.extend(custom_patterns)

    ad_patterns = []
    for p in all_pattern_strs:
        try:
            ad_patterns.append(
                re.compile(r'(?i)^\s*' + p + r'\s*$', re.MULTILINE))
        except re.error:
            pass

    url_pattern = re.compile(
        r'(?i)^\s*(?:https?://|www\.)\S+\s*$', re.MULTILINE)
    ad_check_parts = [
        r'(subtitl(es|ed)|synced?|caption(s|ed|ing)?|translated'
        r'|corrections?|encoded|ripped)\s+by\b',
        r'opensubtitles', r'addic7ed', r'subscene',
    ]
    if custom_patterns:
        for p in custom_patterns:
            try:
                re.compile(p)
                ad_check_parts.append(p)
            except re.error:
                pass
    ad_check = re.compile(
        r'(?i)(' + '|'.join(ad_check_parts) + r')')

    result = []
    for cue in cues:
        text = cue['text']
        has_ad = bool(ad_check.search(text))
        for pat in ad_patterns:
            text = pat.sub('', text)
        if has_ad or not re.sub(
                r'(?i)(?:https?://|www\.)\S+', '', text).strip():
            text = url_pattern.sub('', text)
        text = re.sub(r'\n{2,}', '\n', text).strip()
        if text:
            result.append({**cue, 'text': text})
    return result


def filter_remove_offscreen_quotes(cues):
    """Remove wrapping single quotes used for off-screen dialogue
    (UK style).

    Preserves contractions ('cause, 'til) and dropped-g words
    (somethin', thinkin').
    """
    CONTRACTION_WORDS = {
        'cause', 'cos', 'coz', 'til', 'bout', 'em', 'im',
        'twas', 'tis', 'neath', 'ere', 'appen',
        'ave', 'alf', 'ad', 'ow', 'owt', 'nowt',
    }

    result = []
    for cue in cues:
        lines = cue['text'].split('\n')
        cleaned = []
        for line in lines:
            stripped = line.strip()
            leading = ''
            inner = stripped
            m = re.match(r'^(-\s*)', inner)
            if m:
                leading = m.group(1)
                inner = inner[len(leading):]

            # Opening quote
            if inner and inner[0] == "'":
                word_match = re.match(r"'([a-zA-Z]+)", inner)
                if word_match:
                    first_word = word_match.group(1).lower()
                    if first_word not in CONTRACTION_WORDS:
                        inner = inner[1:]
                else:
                    inner = inner[1:]

            # Closing quote
            if inner and inner[-1] == "'":
                if not inner[-2:-1].isalpha():
                    inner = inner[:-1]

            cleaned.append(leading + inner)

        text = '\n'.join(cleaned).strip()
        if text:
            result.append({**cue, 'text': text})
    return result


def filter_remove_speaker_labels(cues):
    """Remove speaker name labels from start of lines.

    Examples removed: 'John:', 'MARY:', '- Detective Smith:'
    Examples kept: '2:30', 'Wait: what?'
    """
    pattern = re.compile(
        r'^(-?\s*)[A-Za-z][A-Za-z\s\d\'\.]{0,29}[A-Za-z]:\s*\n?',
        re.MULTILINE)

    result = []
    for cue in cues:
        text = cue['text']

        def _replace(m):
            label = m.group(0).lstrip('- ')
            name_part = label.split(':')[0].strip()
            if re.match(r'^\d+$', name_part):
                return m.group(0)
            if re.search(r'\d$', name_part):
                return m.group(0)
            if len(name_part) <= 1:
                return m.group(0)
            return m.group(1)

        text = pattern.sub(_replace, text)
        text = re.sub(r'^\s*-?\s*$', '', text, flags=re.MULTILINE)
        text = re.sub(r'\n{2,}', '\n', text).strip()
        if text:
            result.append({**cue, 'text': text})
    return result


def filter_remove_leading_dashes(cues):
    """Remove leading dashes from each line of subtitle text."""
    result = []
    for cue in cues:
        lines = cue['text'].split('\n')
        cleaned = [re.sub(r'^-\s*', '', line) for line in lines]
        text = '\n'.join(cleaned).strip()
        if text:
            result.append({**cue, 'text': text})
    return result


def filter_remove_duplicates(cues):
    """Remove duplicate cues (same text and identical timestamps)."""
    if not cues:
        return cues
    result = [cues[0]]
    for cue in cues[1:]:
        prev = result[-1]
        if (cue['text'].strip() == prev['text'].strip()
                and cue['start'] == prev['start']
                and cue['end'] == prev['end']):
            continue
        result.append(cue)
    return result


def filter_merge_short(cues, max_gap_ms=1000):
    """Merge consecutive cues with a small time gap that appear to be
    fragments."""
    if not cues:
        return cues
    result = [dict(cues[0])]
    for cue in cues[1:]:
        prev = result[-1]
        prev_end = srt_ts_to_ms(prev['end'])
        cur_start = srt_ts_to_ms(cue['start'])
        gap = cur_start - prev_end
        prev_text = prev['text'].strip()
        if (0 <= gap <= max_gap_ms and len(prev_text) < 40
                and not prev_text.endswith(('.', '!', '?'))):
            result[-1] = {
                **prev,
                'end': cue['end'],
                'text': prev['text'].rstrip() + ' ' + cue['text'].lstrip()
            }
        else:
            result.append(dict(cue))
    return result


def filter_reduce_lines(cues, max_lines=2, max_chars=42):
    """Reflow subtitle cues to max_lines, keeping sentences together
    where possible."""
    if not cues:
        return cues

    def _reflow(text):
        lines = text.split('\n')
        if len(lines) <= max_lines:
            return text

        flat = ' '.join(l.strip() for l in lines if l.strip())
        if flat.startswith('- ') and '- ' in flat[2:]:
            parts = re.split(r'(?<=\S) (?=- )', flat)
            if len(parts) == max_lines:
                return '\n'.join(p.strip() for p in parts)
            elif len(parts) > max_lines:
                kept = parts[:max_lines - 1]
                kept.append(' '.join(parts[max_lines - 1:]))
                return '\n'.join(p.strip() for p in kept)

        if len(flat) <= max_chars:
            return flat

        split_points = []
        for m in re.finditer(r'[.!?]+[\'"»\)]*\s+', flat):
            pos = m.end()
            if pos < len(flat):
                split_points.append(pos)

        best_split = None
        best_diff = len(flat)
        for pos in split_points:
            line1 = flat[:pos].rstrip()
            line2 = flat[pos:].lstrip()
            if max(len(line1), len(line2)) <= max_chars + 10:
                diff = abs(len(line1) - len(line2))
                if diff < best_diff:
                    best_diff = diff
                    best_split = pos

        if best_split is not None:
            return (flat[:best_split].rstrip() + '\n'
                    + flat[best_split:].lstrip())

        mid = len(flat) // 2
        best_pos = None
        for offset in range(len(flat) // 2):
            for pos in (mid + offset, mid - offset):
                if 0 < pos < len(flat) and flat[pos] == ' ':
                    best_pos = pos
                    break
            if best_pos is not None:
                break

        if best_pos is not None:
            return flat[:best_pos] + '\n' + flat[best_pos + 1:]

        return flat

    result = []
    for cue in cues:
        text = cue['text']
        if len(text.split('\n')) > max_lines:
            text = _reflow(text)
        result.append({**cue, 'text': text})
    return result


# ═══════════════════════════════════════════════════════════════════
# Timing Manipulation
# ═══════════════════════════════════════════════════════════════════

def shift_timestamps(cues, offset_ms):
    """Shift all cue timestamps by offset_ms (positive = later)."""
    result = []
    for cue in cues:
        new_start = srt_ts_to_ms(cue['start']) + offset_ms
        new_end = srt_ts_to_ms(cue['end']) + offset_ms
        if new_end > 0:
            result.append({
                **cue,
                'start': ms_to_srt_ts(max(0, new_start)),
                'end': ms_to_srt_ts(new_end),
            })
    return result


def stretch_timestamps(cues, factor):
    """Scale all timestamps by a factor (e.g. 1.04 to speed up 4%)."""
    if factor <= 0:
        return cues
    result = []
    for cue in cues:
        new_start = int(srt_ts_to_ms(cue['start']) * factor)
        new_end = int(srt_ts_to_ms(cue['end']) * factor)
        result.append({
            **cue,
            'start': ms_to_srt_ts(new_start),
            'end': ms_to_srt_ts(new_end),
        })
    return result


def two_point_sync(cues, idx_a, correct_a_ms, idx_b, correct_b_ms):
    """Linearly resync all timestamps using two reference points.

    Given two cue indices and what their correct start times should be,
    computes a linear transform (offset + scale) and applies it.
    """
    if idx_a == idx_b or idx_a >= len(cues) or idx_b >= len(cues):
        return cues

    current_a = srt_ts_to_ms(cues[idx_a]['start'])
    current_b = srt_ts_to_ms(cues[idx_b]['start'])

    if current_a == current_b:
        return cues

    slope = (correct_b_ms - correct_a_ms) / (current_b - current_a)
    intercept = correct_a_ms - slope * current_a

    result = []
    for cue in cues:
        old_start = srt_ts_to_ms(cue['start'])
        old_end = srt_ts_to_ms(cue['end'])
        new_start = int(slope * old_start + intercept)
        new_end = int(slope * old_end + intercept)
        if new_end > 0:
            result.append({
                **cue,
                'start': ms_to_srt_ts(max(0, new_start)),
                'end': ms_to_srt_ts(max(0, new_end)),
            })
    return result


def retime_subtitles(cues, matches):
    """Re-time all subtitle cues using matched anchor points with
    piecewise linear interpolation.

    For matched cues, uses the Whisper-detected timestamp directly.
    For unmatched cues, linearly interpolates from nearest anchors.

    Args:
        cues: List of subtitle cue dicts.
        matches: List of (cue_idx, whisper_time_ms, cue_time_ms,
                 similarity, text) tuples from smart_sync.

    Returns:
        New list of cue dicts with adjusted timestamps.
    """
    if not matches or not cues:
        return cues

    anchors = []
    for ci, wt_ms, ct_ms, sim, _ in matches:
        anchors.append((ct_ms, wt_ms))
    anchors.sort(key=lambda x: x[0])

    seen = {}
    for old_t, new_t in anchors:
        seen[old_t] = new_t
    anchors = sorted(seen.items())

    if len(anchors) < 2:
        offset = anchors[0][1] - anchors[0][0]
        return shift_timestamps(cues, offset)

    def _interpolate(old_ms):
        if old_ms <= anchors[0][0]:
            old_a, new_a = anchors[0]
            old_b, new_b = anchors[1]
            if old_b == old_a:
                return new_a + (old_ms - old_a)
            slope = (new_b - new_a) / (old_b - old_a)
            return int(new_a + slope * (old_ms - old_a))

        if old_ms >= anchors[-1][0]:
            old_a, new_a = anchors[-2]
            old_b, new_b = anchors[-1]
            if old_b == old_a:
                return new_b + (old_ms - old_b)
            slope = (new_b - new_a) / (old_b - old_a)
            return int(new_b + slope * (old_ms - old_b))

        for i in range(len(anchors) - 1):
            old_a, new_a = anchors[i]
            old_b, new_b = anchors[i + 1]
            if old_a <= old_ms <= old_b:
                if old_b == old_a:
                    return new_a
                t = (old_ms - old_a) / (old_b - old_a)
                return int(new_a + t * (new_b - new_a))

        return old_ms

    result = []
    for cue in cues:
        old_start = srt_ts_to_ms(cue['start'])
        old_end = srt_ts_to_ms(cue['end'])
        new_start = _interpolate(old_start)
        new_end = _interpolate(old_end)
        if new_end <= new_start:
            new_end = new_start + max(500, old_end - old_start)
        result.append({
            **cue,
            'start': ms_to_srt_ts(max(0, new_start)),
            'end': ms_to_srt_ts(max(0, new_end)),
        })
    return result
