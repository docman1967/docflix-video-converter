"""
Docflix Media Suite — Chapter Utilities

Generate, parse, and write chapter data for ffmpeg injection.
Chapters are stored as a list of dicts:
    [{'start': float_secs, 'end': float_secs, 'title': str}, ...]

ffmpeg reads chapters from an FFMETADATA1 file added as an extra
input with -map_chapters <index>.
"""

import os
import re
import tempfile


def generate_auto_chapters(duration_secs, interval_minutes=5):
    """Generate evenly-spaced chapters at a given interval.

    Args:
        duration_secs: Total duration in seconds.
        interval_minutes: Chapter interval in minutes (default 5).

    Returns:
        List of chapter dicts with start, end, title.
        Returns empty list if duration is unknown or too short.
    """
    if not duration_secs or duration_secs <= 0:
        return []

    interval_secs = interval_minutes * 60
    if interval_secs <= 0:
        return []

    chapters = []
    start = 0.0
    num = 1

    while start < duration_secs:
        end = min(start + interval_secs, duration_secs)
        chapters.append({
            'start': start,
            'end': end,
            'title': f'Chapter {num}',
        })
        start = end
        num += 1

    return chapters


def parse_chapter_file(filepath):
    """Auto-detect chapter file format and parse to chapter dicts.

    Supports:
        - FFMETADATA1 format (;FFMETADATA1 header, [CHAPTER] sections)
        - OGM format (CHAPTER01=HH:MM:SS.mmm / CHAPTER01NAME=Title)

    Returns:
        List of chapter dicts, or empty list on failure.
    """
    try:
        with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
            text = f.read()
    except Exception:
        return []

    text = text.strip()

    # Detect format
    if text.startswith(';FFMETADATA1'):
        return _parse_ffmetadata_chapters(text)
    elif re.search(r'CHAPTER\d+=', text, re.IGNORECASE):
        return _parse_ogm_chapters(text)
    else:
        return []


def _parse_ffmetadata_chapters(text):
    """Parse ;FFMETADATA1 format chapters.

    Format:
        ;FFMETADATA1

        [CHAPTER]
        TIMEBASE=1/1000
        START=0
        END=300000
        title=Chapter 1
    """
    chapters = []
    current = None
    timebase_num = 1
    timebase_den = 1000  # default: milliseconds

    for line in text.splitlines():
        line = line.strip()
        if line == '[CHAPTER]':
            if current:
                chapters.append(current)
            current = {'start': 0.0, 'end': 0.0, 'title': ''}
            timebase_num = 1
            timebase_den = 1000
        elif current is not None:
            if line.startswith('TIMEBASE='):
                tb = line.split('=', 1)[1].strip()
                if '/' in tb:
                    parts = tb.split('/')
                    try:
                        timebase_num = int(parts[0])
                        timebase_den = int(parts[1])
                    except (ValueError, IndexError):
                        pass
            elif line.startswith('START='):
                try:
                    raw = int(line.split('=', 1)[1].strip())
                    current['start'] = raw * timebase_num / timebase_den
                except (ValueError, ZeroDivisionError):
                    pass
            elif line.startswith('END='):
                try:
                    raw = int(line.split('=', 1)[1].strip())
                    current['end'] = raw * timebase_num / timebase_den
                except (ValueError, ZeroDivisionError):
                    pass
            elif line.startswith('title='):
                current['title'] = line.split('=', 1)[1].strip()

    if current:
        chapters.append(current)

    # Fill missing titles
    for i, ch in enumerate(chapters):
        if not ch['title']:
            ch['title'] = f'Chapter {i + 1}'

    return chapters


def _parse_ogm_chapters(text):
    """Parse OGM chapter format.

    Format:
        CHAPTER01=00:00:00.000
        CHAPTER01NAME=Introduction
        CHAPTER02=00:05:00.000
        CHAPTER02NAME=Act One
    """
    times = {}
    names = {}

    for line in text.splitlines():
        line = line.strip()
        # CHAPTER01=HH:MM:SS.mmm
        m = re.match(r'CHAPTER(\d+)\s*=\s*(\d+):(\d+):(\d+)\.?(\d*)', line, re.IGNORECASE)
        if m:
            num = int(m.group(1))
            h, mi, s = int(m.group(2)), int(m.group(3)), int(m.group(4))
            ms = int(m.group(5).ljust(3, '0')[:3]) if m.group(5) else 0
            times[num] = h * 3600 + mi * 60 + s + ms / 1000.0
            continue
        # CHAPTER01NAME=Title
        m = re.match(r'CHAPTER(\d+)NAME\s*=\s*(.*)', line, re.IGNORECASE)
        if m:
            num = int(m.group(1))
            names[num] = m.group(2).strip()

    if not times:
        return []

    # Build chapter list
    sorted_nums = sorted(times.keys())
    chapters = []
    for i, num in enumerate(sorted_nums):
        start = times[num]
        # End = start of next chapter, or we'll fill it later
        if i + 1 < len(sorted_nums):
            end = times[sorted_nums[i + 1]]
        else:
            end = start  # will be filled by caller with file duration
        title = names.get(num, f'Chapter {num}')
        chapters.append({'start': start, 'end': end, 'title': title})

    return chapters


def chapters_to_ffmetadata(chapters, output_path=None):
    """Write chapter list to an FFMETADATA1 file.

    Args:
        chapters: List of chapter dicts with start, end, title.
        output_path: Path to write to. If None, creates a temp file.

    Returns:
        Path to the written file, or None on failure.
    """
    if not chapters:
        return None

    lines = [';FFMETADATA1', '']

    for ch in chapters:
        start_ms = int(ch['start'] * 1000)
        end_ms = int(ch['end'] * 1000)
        title = ch.get('title', '')
        lines.append('[CHAPTER]')
        lines.append('TIMEBASE=1/1000')
        lines.append(f'START={start_ms}')
        lines.append(f'END={end_ms}')
        lines.append(f'title={title}')
        lines.append('')

    content = '\n'.join(lines)

    try:
        if output_path:
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(content)
            return output_path
        else:
            fd, path = tempfile.mkstemp(suffix='_chapters.txt', prefix='docflix_')
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(content)
            return path
    except Exception:
        return None


def format_chapter_time(seconds):
    """Format seconds as HH:MM:SS.mmm for display."""
    if seconds is None or seconds < 0:
        return '0:00:00.000'
    h = int(seconds) // 3600
    m = (int(seconds) % 3600) // 60
    s = seconds % 60
    return f'{h}:{m:02d}:{s:06.3f}'


def parse_chapter_time(time_str):
    """Parse HH:MM:SS.mmm or MM:SS.mmm or SS.mmm to seconds.

    Returns float seconds, or None on parse failure.
    """
    time_str = time_str.strip()
    try:
        parts = time_str.split(':')
        if len(parts) == 3:
            h, m, s = parts
            return int(h) * 3600 + int(m) * 60 + float(s)
        elif len(parts) == 2:
            m, s = parts
            return int(m) * 60 + float(s)
        elif len(parts) == 1:
            return float(parts[0])
    except (ValueError, IndexError):
        pass
    return None
