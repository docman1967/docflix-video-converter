"""
Docflix Media Suite — Enhanced Media Details

Comprehensive media file analysis and tag editor using ffprobe.
Displays container, video, audio, subtitle, chapter, and
metadata information in a tabbed dialog. Editable fields for
track names, language codes, and disposition flags with save
via ffmpeg remux.
"""

import json
import os
import subprocess
import threading
import tkinter as tk
from tkinter import ttk, messagebox

from .constants import SUBTITLE_LANGUAGES, LANG_CODE_TO_NAME
from .chapters import generate_auto_chapters, chapters_to_ffmetadata, format_chapter_time, parse_chapter_time
from .utils import scaled_geometry, scaled_minsize


# ═══════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════

# Language codes for the dropdown (code, display)
_LANG_VALUES = [f'{code} ({name})' for code, name in SUBTITLE_LANGUAGES]

# Disposition flags per stream type
_DISP_FLAGS_VIDEO = []
_DISP_FLAGS_AUDIO = [
    ('default', 'Default track'),
    ('comment', 'Commentary'),
]
_DISP_FLAGS_SUBTITLE = [
    ('default', 'Default track'),
    ('forced', 'Forced display'),
    ('hearing_impaired', 'Hearing impaired (SDH)'),
    ('comment', 'Commentary'),
]


# ═══════════════════════════════════════════════════════════════════
# ffprobe helpers
# ═══════════════════════════════════════════════════════════════════

def _run_ffprobe(args, timeout=15):
    """Run ffprobe with given args, return parsed JSON or None."""
    cmd = ['ffprobe', '-v', 'quiet', '-print_format', 'json'] + args
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if r.returncode == 0 and r.stdout.strip():
            return json.loads(r.stdout)
    except Exception:
        pass
    return None


def probe_file(filepath):
    """Run comprehensive ffprobe on a file.

    Returns a dict with keys: format, streams, chapters, hdr.
    """
    # Main probe: format + streams + chapters
    data = _run_ffprobe([
        '-show_format', '-show_streams', '-show_chapters',
        filepath
    ]) or {}

    # HDR probe: first frame side data (mastering display, content light level, DV)
    hdr_data = _run_ffprobe([
        '-select_streams', 'v:0',
        '-read_intervals', '%+#1',
        '-show_entries', 'frame=side_data_list',
        filepath
    ], timeout=10)

    hdr = {}
    if hdr_data:
        frames = hdr_data.get('frames', [])
        if frames:
            for sd in frames[0].get('side_data_list', []):
                sd_type = sd.get('side_data_type', '')
                if 'Mastering display' in sd_type:
                    hdr['mastering_display'] = sd
                elif 'Content light level' in sd_type:
                    hdr['content_light'] = sd
                elif 'DOVI' in sd_type or 'Dolby Vision' in sd_type:
                    hdr['dolby_vision'] = sd

    return {
        'format': data.get('format', {}),
        'streams': data.get('streams', []),
        'chapters': data.get('chapters', []),
        'hdr': hdr,
    }


# ═══════════════════════════════════════════════════════════════════
# Formatting helpers
# ═══════════════════════════════════════════════════════════════════

def _fmt_size(size_bytes):
    """Format file size in human-readable format."""
    if not size_bytes:
        return '?'
    size_bytes = int(size_bytes)
    if size_bytes == 0:
        return '0 B'
    units = ['B', 'KB', 'MB', 'GB', 'TB']
    idx = 0
    size = float(size_bytes)
    while size >= 1024 and idx < len(units) - 1:
        size /= 1024
        idx += 1
    return f'{size:.1f} {units[idx]}'


def _fmt_duration(seconds):
    """Format duration as HH:MM:SS.mmm."""
    if seconds is None:
        return '?'
    try:
        seconds = float(seconds)
    except (ValueError, TypeError):
        return '?'
    h = int(seconds) // 3600
    m = (int(seconds) % 3600) // 60
    s = seconds % 60
    if h > 0:
        return f'{h}:{m:02d}:{s:06.3f}'
    return f'{m}:{s:06.3f}'


def _fmt_bitrate(bps):
    """Format bitrate in kbps or Mbps."""
    if not bps:
        return '?'
    try:
        bps = int(bps)
    except (ValueError, TypeError):
        return '?'
    if bps >= 1_000_000:
        return f'{bps / 1_000_000:.1f} Mbps'
    return f'{bps // 1000} kbps'


def _fmt_framerate(rate_str):
    """Format frame rate string (e.g. '24000/1001' → '23.976 fps')."""
    if not rate_str or rate_str == '0/0':
        return '?'
    try:
        if '/' in str(rate_str):
            num, den = rate_str.split('/')
            fps = int(num) / int(den)
        else:
            fps = float(rate_str)
        if fps == 0:
            return '?'
        return f'{fps:.3f} fps'
    except (ValueError, ZeroDivisionError):
        return rate_str


def _safe(val, suffix='', fallback='?'):
    """Return val with optional suffix, or fallback if empty/None."""
    if val is None or val == '' or val == 'unknown' or val == 'N/A':
        return fallback
    return f'{val}{suffix}'


def _derive_hdr_format(stream, hdr):
    """Derive an HDR format label from stream and side data."""
    transfer = stream.get('color_transfer', '')
    primaries = stream.get('color_primaries', '')
    pix_fmt = stream.get('pix_fmt', '')

    if hdr.get('dolby_vision'):
        dv = hdr['dolby_vision']
        profile = dv.get('dv_profile', '?')
        bl_compat = dv.get('dv_bl_signal_compatibility_id', '?')
        return f'Dolby Vision (Profile {profile}, BL compat {bl_compat})'

    if transfer == 'smpte2084':
        if hdr.get('mastering_display') or hdr.get('content_light'):
            return 'HDR10'
        return 'PQ (HDR10 compatible)'
    if transfer == 'arib-std-b67':
        return 'HLG'

    if 'bt2020' in primaries or '10' in pix_fmt:
        return 'Wide Color Gamut (potential HDR)'

    return 'SDR'


def _lang_code_from_display(display_val):
    """Extract the language code from a display string like 'eng (English)' or plain 'eng'."""
    if not display_val:
        return 'und'
    display_val = display_val.strip()
    if ' (' in display_val:
        return display_val.split(' (')[0].strip()
    return display_val


def _lang_display(code):
    """Convert a language code to display format: 'eng (English)'."""
    if not code or code == 'und':
        return 'und (Undetermined)'
    name = LANG_CODE_TO_NAME.get(code, '')
    if name:
        return f'{code} ({name})'
    return code


# ═══════════════════════════════════════════════════════════════════
# Report builder — produces the text for each section
# (kept for Full Report, Chapters, Attachments, Metadata tabs)
# ═══════════════════════════════════════════════════════════════════

def _build_general_section(filepath, fmt):
    """Build the General / Container section."""
    lines = []
    lines.append(f'  File Name:      {os.path.basename(filepath)}')
    lines.append(f'  Directory:      {os.path.dirname(filepath)}')

    title = fmt.get('tags', {}).get('title', '')
    if title:
        lines.append(f'  Title/Edition:  {title}')

    lines.append(f'  Format:         {fmt.get("format_long_name", fmt.get("format_name", "?"))}')

    profile = fmt.get('profile')
    if profile:
        lines.append(f'  Format Profile: {profile}')

    dur = fmt.get('duration')
    if dur:
        lines.append(f'  Duration:       {_fmt_duration(dur)} ({float(dur):.2f}s)')

    start = fmt.get('start_time')
    if start and float(start) != 0:
        lines.append(f'  Start Time:     {_fmt_duration(start)}')

    lines.append(f'  File Size:      {_fmt_size(fmt.get("size"))}')
    lines.append(f'  Overall Bitrate:{" "}{_fmt_bitrate(fmt.get("bit_rate"))}')
    lines.append(f'  Streams:        {fmt.get("nb_streams", "?")}')

    nb_prog = fmt.get('nb_programs')
    if nb_prog and int(nb_prog) > 0:
        lines.append(f'  Programs:       {nb_prog}')

    return lines


def _build_video_section(stream, hdr, stream_num):
    """Build a Video stream section."""
    tags = stream.get('tags', {})
    disp = stream.get('disposition', {})
    lines = []

    lang = tags.get('language', '')
    title = tags.get('title', '')
    hdr_label = _derive_hdr_format(stream, hdr)
    header_parts = [f'STREAM #{stream.get("index", "?")} — VIDEO']
    if lang and lang != 'und':
        header_parts.append(f'[{lang.upper()}]')
    lines.append(f'  {"  ".join(header_parts)}')
    lines.append(f'  {"─" * 52}')

    codec_name = stream.get('codec_long_name', stream.get('codec_name', '?'))
    lines.append(f'    Codec:            {codec_name}')
    lines.append(f'    Codec Tag:        {_safe(stream.get("codec_tag_string"))}')
    lines.append(f'    Profile:          {_safe(stream.get("profile"))}')

    level = stream.get('level')
    if level and level != -99:
        if level > 9:
            lines.append(f'    Level:            {level / 10:.1f}')
        else:
            lines.append(f'    Level:            {level}')

    if title:
        lines.append(f'    Title:            {title}')

    lines.append(f'    Resolution:       {stream.get("width", "?")}x{stream.get("height", "?")}')
    coded_w = stream.get('coded_width')
    coded_h = stream.get('coded_height')
    if coded_w and coded_h:
        w, h = stream.get('width'), stream.get('height')
        if coded_w != w or coded_h != h:
            lines.append(f'    Coded Size:       {coded_w}x{coded_h}')

    sar = stream.get('sample_aspect_ratio')
    dar = stream.get('display_aspect_ratio')
    if sar and sar != '1:1' and sar != '0:1':
        lines.append(f'    SAR:              {sar}')
    if dar:
        lines.append(f'    DAR:              {dar}')

    lines.append(f'    Frame Rate:       {_fmt_framerate(stream.get("r_frame_rate"))}')
    avg_fps = stream.get('avg_frame_rate')
    r_fps = stream.get('r_frame_rate')
    if avg_fps and r_fps and avg_fps != r_fps and avg_fps != '0/0':
        lines.append(f'    Avg Frame Rate:   {_fmt_framerate(avg_fps)} (VFR)')

    nb_frames = stream.get('nb_frames')
    if nb_frames and nb_frames != 'N/A':
        lines.append(f'    Frame Count:      {nb_frames}')

    field_order = stream.get('field_order')
    if field_order and field_order != 'unknown':
        scan = 'Progressive' if field_order == 'progressive' else f'Interlaced ({field_order})'
        lines.append(f'    Scan Type:        {scan}')

    lines.append(f'    Pixel Format:     {_safe(stream.get("pix_fmt"))}')

    bit_depth = stream.get('bits_per_raw_sample')
    if bit_depth:
        lines.append(f'    Bit Depth:        {bit_depth}-bit')

    color_range = stream.get('color_range')
    if color_range and color_range != 'unknown':
        label = 'Limited (TV)' if color_range == 'tv' else 'Full (PC)' if color_range == 'pc' else color_range
        lines.append(f'    Color Range:      {label}')

    color_space = stream.get('color_space')
    if color_space and color_space != 'unknown':
        lines.append(f'    Color Space:      {color_space}')

    color_transfer = stream.get('color_transfer')
    if color_transfer and color_transfer != 'unknown':
        lines.append(f'    Color Transfer:   {color_transfer}')

    color_primaries = stream.get('color_primaries')
    if color_primaries and color_primaries != 'unknown':
        lines.append(f'    Color Primaries:  {color_primaries}')

    chroma = stream.get('chroma_location')
    if chroma and chroma != 'unknown' and chroma != 'unspecified':
        lines.append(f'    Chroma Location:  {chroma}')

    lines.append(f'    HDR Format:       {hdr_label}')

    md = hdr.get('mastering_display')
    if md:
        lines.append(f'    Mastering Display:')
        for key in ('red_x', 'red_y', 'green_x', 'green_y',
                     'blue_x', 'blue_y', 'white_point_x', 'white_point_y'):
            val = md.get(key)
            if val:
                lines.append(f'      {key}: {val}')
        min_lum = md.get('min_luminance')
        max_lum = md.get('max_luminance')
        if min_lum or max_lum:
            lines.append(f'      Luminance: {_safe(min_lum)} - {_safe(max_lum)} cd/m²')

    cl = hdr.get('content_light')
    if cl:
        lines.append(f'    Content Light:    MaxCLL={cl.get("max_content", "?")}, '
                      f'MaxFALL={cl.get("max_average", "?")}')

    dv = hdr.get('dolby_vision')
    if dv:
        lines.append(f'    Dolby Vision:     Profile {dv.get("dv_profile", "?")}, '
                      f'Level {dv.get("dv_level", "?")}, '
                      f'BL compat {dv.get("dv_bl_signal_compatibility_id", "?")}')

    sbr = stream.get('bit_rate')
    if sbr:
        lines.append(f'    Bitrate:          {_fmt_bitrate(sbr)}')
    max_br = stream.get('max_bit_rate')
    if max_br:
        lines.append(f'    Max Bitrate:      {_fmt_bitrate(max_br)}')

    refs = stream.get('refs')
    if refs:
        lines.append(f'    Reference Frames: {refs}')

    cc = stream.get('closed_captions')
    if cc and int(cc) == 1:
        lines.append(f'    Closed Captions:  Yes')

    flags = []
    if disp.get('default'):       flags.append('Default')
    if disp.get('attached_pic'):  flags.append('Cover Art')
    if flags:
        lines.append(f'    Disposition:      {", ".join(flags)}')

    sdur = stream.get('duration') or tags.get('DURATION')
    if sdur:
        try:
            lines.append(f'    Duration:         {_fmt_duration(float(sdur))}')
        except (ValueError, TypeError):
            if ':' in str(sdur):
                lines.append(f'    Duration:         {sdur}')

    return lines


def _build_audio_section(stream):
    """Build an Audio stream section."""
    tags = stream.get('tags', {})
    disp = stream.get('disposition', {})
    lines = []

    lang = tags.get('language', '')
    title = tags.get('title', '')
    header_parts = [f'STREAM #{stream.get("index", "?")} — AUDIO']
    if lang and lang != 'und':
        header_parts.append(f'[{lang.upper()}]')
    lines.append(f'  {"  ".join(header_parts)}')
    lines.append(f'  {"─" * 52}')

    codec_name = stream.get('codec_long_name', stream.get('codec_name', '?'))
    lines.append(f'    Codec:            {codec_name}')
    lines.append(f'    Codec Tag:        {_safe(stream.get("codec_tag_string"))}')

    profile = stream.get('profile')
    if profile and profile != 'unknown':
        lines.append(f'    Profile:          {profile}')

    if title:
        lines.append(f'    Title:            {title}')

    lines.append(f'    Sample Rate:      {_safe(stream.get("sample_rate"), " Hz")}')
    lines.append(f'    Channels:         {_safe(stream.get("channels"))}')

    layout = stream.get('channel_layout')
    if layout:
        lines.append(f'    Channel Layout:   {layout}')

    sample_fmt = stream.get('sample_fmt')
    if sample_fmt:
        lines.append(f'    Sample Format:    {sample_fmt}')

    bps = stream.get('bits_per_raw_sample') or stream.get('bits_per_sample')
    if bps and int(bps) > 0:
        lines.append(f'    Bits per Sample:  {bps}')

    sbr = stream.get('bit_rate')
    if sbr:
        lines.append(f'    Bitrate:          {_fmt_bitrate(sbr)}')
    max_br = stream.get('max_bit_rate')
    if max_br:
        lines.append(f'    Max Bitrate:      {_fmt_bitrate(max_br)}')

    flags = []
    if disp.get('default'):           flags.append('Default')
    if disp.get('forced'):            flags.append('Forced')
    if disp.get('visual_impaired'):   flags.append('Visual Impaired')
    if disp.get('comment'):           flags.append('Commentary')
    if disp.get('original'):          flags.append('Original')
    if flags:
        lines.append(f'    Disposition:      {", ".join(flags)}')

    sdur = stream.get('duration') or tags.get('DURATION')
    if sdur:
        try:
            lines.append(f'    Duration:         {_fmt_duration(float(sdur))}')
        except (ValueError, TypeError):
            if ':' in str(sdur):
                lines.append(f'    Duration:         {sdur}')

    return lines


def _build_subtitle_section(stream):
    """Build a Subtitle stream section."""
    tags = stream.get('tags', {})
    disp = stream.get('disposition', {})
    lines = []

    lang = tags.get('language', '')
    title = tags.get('title', '')
    header_parts = [f'STREAM #{stream.get("index", "?")} — SUBTITLE']
    if lang and lang != 'und':
        header_parts.append(f'[{lang.upper()}]')
    lines.append(f'  {"  ".join(header_parts)}')
    lines.append(f'  {"─" * 52}')

    lines.append(f'    Codec:            {stream.get("codec_name", "?")}')
    codec_long = stream.get('codec_long_name')
    if codec_long and codec_long != stream.get('codec_name'):
        lines.append(f'    Codec Name:       {codec_long}')

    if title:
        lines.append(f'    Title:            {title}')

    nb = tags.get('NUMBER_OF_FRAMES') or stream.get('nb_frames')
    if nb and nb != 'N/A' and nb != '0':
        lines.append(f'    Events:           {nb}')

    w = stream.get('width')
    h = stream.get('height')
    if w and h and int(w) > 0:
        lines.append(f'    Resolution:       {w}x{h}')

    flags = []
    if disp.get('default'):            flags.append('Default')
    if disp.get('forced'):             flags.append('Forced')
    if disp.get('hearing_impaired'):   flags.append('SDH')
    if disp.get('comment'):            flags.append('Commentary')
    if flags:
        lines.append(f'    Disposition:      {", ".join(flags)}')

    sdur = stream.get('duration') or tags.get('DURATION')
    if sdur:
        try:
            lines.append(f'    Duration:         {_fmt_duration(float(sdur))}')
        except (ValueError, TypeError):
            if ':' in str(sdur):
                lines.append(f'    Duration:         {sdur}')

    return lines


def _build_attachment_section(stream):
    """Build an Attachment/Data stream section."""
    tags = stream.get('tags', {})
    lines = []

    ctype = stream.get('codec_type', 'data').upper()
    lines.append(f'  STREAM #{stream.get("index", "?")} — {ctype}')
    lines.append(f'  {"─" * 52}')

    codec = stream.get('codec_name') or stream.get('codec_long_name')
    if codec:
        lines.append(f'    Codec:            {codec}')

    fname = tags.get('filename')
    if fname:
        lines.append(f'    Filename:         {fname}')

    mime = tags.get('mimetype')
    if mime:
        lines.append(f'    MIME Type:        {mime}')

    title = tags.get('title')
    if title:
        lines.append(f'    Title:            {title}')

    return lines


def _build_chapters_section(chapters):
    """Build the Chapters section."""
    if not chapters:
        return ['  No chapters found.']

    lines = []
    lines.append(f'  {len(chapters)} chapter(s):')
    lines.append(f'  {"─" * 52}')

    for i, ch in enumerate(chapters):
        start = float(ch.get('start_time', 0))
        end = float(ch.get('end_time', 0))
        title = ch.get('tags', {}).get('title', f'Chapter {i + 1}')
        lines.append(f'    #{i + 1:<3}  {_fmt_duration(start)} → {_fmt_duration(end)}  "{title}"')

    return lines


def _build_metadata_section(fmt):
    """Build the Metadata / Tags section from format tags."""
    tags = fmt.get('tags', {})
    if not tags:
        return ['  No container metadata found.']

    lines = []
    lines.append(f'  {"─" * 52}')

    priority_keys = [
        ('title', 'Title'), ('artist', 'Artist'), ('album', 'Album'),
        ('album_artist', 'Album Artist'), ('date', 'Date'),
        ('creation_time', 'Creation Time'), ('year', 'Year'),
        ('genre', 'Genre'), ('show', 'Show'), ('season_number', 'Season'),
        ('episode_sort', 'Episode'), ('episode_id', 'Episode ID'),
        ('network', 'Network'), ('description', 'Description'),
        ('synopsis', 'Synopsis'), ('comment', 'Comment'),
        ('copyright', 'Copyright'), ('encoder', 'Encoder'),
        ('encoding_tool', 'Encoding Tool'), ('handler_name', 'Handler'),
        ('ENCODER', 'Encoder'), ('COMPATIBLE_BRANDS', 'Compatible Brands'),
    ]

    shown = set()
    for key, label in priority_keys:
        val = tags.get(key) or tags.get(key.upper()) or tags.get(key.lower())
        if val and key.lower() not in shown:
            lines.append(f'    {label + ":":<22}{val}')
            shown.add(key.lower())

    for key, val in sorted(tags.items()):
        if key.lower() not in shown:
            lines.append(f'    {key + ":":<22}{val}')

    return lines


# ═══════════════════════════════════════════════════════════════════
# Full report builder
# ═══════════════════════════════════════════════════════════════════

def build_full_report(filepath, data):
    """Build a complete text report from probe data.

    Returns a dict of section_name → text_content for tabbed display,
    plus 'full' key with the complete combined report.
    """
    fmt = data['format']
    streams = data['streams']
    chapters = data['chapters']
    hdr = data['hdr']

    sections = {}

    # ── General ──
    general_lines = []
    general_lines.append(f'{"═" * 56}')
    general_lines.append(f'  GENERAL')
    general_lines.append(f'{"═" * 56}')
    general_lines.extend(_build_general_section(filepath, fmt))
    sections['General'] = '\n'.join(general_lines)

    # ── Video streams ──
    video_lines = []
    video_count = 0
    for s in streams:
        if s.get('codec_type') == 'video' and not s.get('disposition', {}).get('attached_pic'):
            if video_lines:
                video_lines.append('')
            video_lines.extend(_build_video_section(s, hdr, video_count))
            video_count += 1
    if video_lines:
        header = [f'{"═" * 56}', f'  VIDEO ({video_count} stream{"s" if video_count != 1 else ""})',
                  f'{"═" * 56}']
        sections['Video'] = '\n'.join(header + video_lines)

    # ── Audio streams ──
    audio_lines = []
    audio_count = 0
    for s in streams:
        if s.get('codec_type') == 'audio':
            if audio_lines:
                audio_lines.append('')
            audio_lines.extend(_build_audio_section(s))
            audio_count += 1
    if audio_lines:
        header = [f'{"═" * 56}', f'  AUDIO ({audio_count} stream{"s" if audio_count != 1 else ""})',
                  f'{"═" * 56}']
        sections['Audio'] = '\n'.join(header + audio_lines)

    # ── Subtitle streams ──
    sub_lines = []
    sub_count = 0
    for s in streams:
        if s.get('codec_type') == 'subtitle':
            if sub_lines:
                sub_lines.append('')
            sub_lines.extend(_build_subtitle_section(s))
            sub_count += 1
    if sub_lines:
        header = [f'{"═" * 56}', f'  SUBTITLES ({sub_count} stream{"s" if sub_count != 1 else ""})',
                  f'{"═" * 56}']
        sections['Subtitles'] = '\n'.join(header + sub_lines)

    # ── Chapters ──
    chap_header = [f'{"═" * 56}', f'  CHAPTERS', f'{"═" * 56}']
    sections['Chapters'] = '\n'.join(chap_header + _build_chapters_section(chapters))

    # ── Attachments / Data ──
    attach_lines = []
    attach_count = 0
    for s in streams:
        if s.get('codec_type') in ('attachment', 'data'):
            if attach_lines:
                attach_lines.append('')
            attach_lines.extend(_build_attachment_section(s))
            attach_count += 1
    for s in streams:
        if s.get('codec_type') == 'video' and s.get('disposition', {}).get('attached_pic'):
            if attach_lines:
                attach_lines.append('')
            attach_lines.extend(_build_attachment_section(s))
            attach_count += 1
    if attach_lines:
        header = [f'{"═" * 56}',
                  f'  ATTACHMENTS ({attach_count} stream{"s" if attach_count != 1 else ""})',
                  f'{"═" * 56}']
        sections['Attachments'] = '\n'.join(header + attach_lines)

    # ── Metadata / Tags ──
    meta_header = [f'{"═" * 56}', f'  METADATA', f'{"═" * 56}']
    sections['Metadata'] = '\n'.join(meta_header + _build_metadata_section(fmt))

    # ── Full combined report ──
    full_parts = []
    for name in ('General', 'Video', 'Audio', 'Subtitles', 'Chapters',
                 'Attachments', 'Metadata'):
        if name in sections:
            full_parts.append(sections[name])
    sections['full'] = '\n\n'.join(full_parts)

    return sections


# ═══════════════════════════════════════════════════════════════════
# Editable UI helpers
# ═══════════════════════════════════════════════════════════════════

def _add_readonly_row(parent, row, label, value, col_offset=0):
    """Add a read-only label + value row to a grid."""
    ttk.Label(parent, text=label, font=('Courier', 10),
              foreground='#666666').grid(
        row=row, column=col_offset, sticky='ne', padx=(8, 4), pady=1)
    ttk.Label(parent, text=str(value), font=('Courier', 10)).grid(
        row=row, column=col_offset + 1, sticky='nw', padx=(0, 8), pady=1)


def _create_scrollable_frame(parent):
    """Create a scrollable frame. Returns (canvas, inner_frame)."""
    # Use the default ttk background so empty space matches the widget theme
    style = ttk.Style()
    bg_color = style.lookup('TFrame', 'background') or '#d9d9d9'
    canvas = tk.Canvas(parent, bg=bg_color, highlightthickness=0, borderwidth=0)
    scrollbar = ttk.Scrollbar(parent, orient='vertical', command=canvas.yview)
    inner = ttk.Frame(canvas)

    inner.bind('<Configure>', lambda e: canvas.configure(scrollregion=canvas.bbox('all')))
    canvas.create_window((0, 0), window=inner, anchor='nw', tags='inner')

    # Make the inner frame expand horizontally with the canvas
    def _on_canvas_configure(event):
        canvas.itemconfig('inner', width=event.width)
    canvas.bind('<Configure>', _on_canvas_configure)

    canvas.configure(yscrollcommand=scrollbar.set)

    canvas.pack(side='left', fill='both', expand=True)
    scrollbar.pack(side='right', fill='y')

    # Mouse wheel scrolling
    def _on_mousewheel(event):
        canvas.yview_scroll(int(-1 * (event.delta / 120)), 'units')

    def _on_button4(event):
        canvas.yview_scroll(-3, 'units')

    def _on_button5(event):
        canvas.yview_scroll(3, 'units')

    # Bind to canvas and inner frame
    for widget in (canvas, inner):
        widget.bind('<MouseWheel>', _on_mousewheel)
        widget.bind('<Button-4>', _on_button4)
        widget.bind('<Button-5>', _on_button5)

    return canvas, inner


def _compute_type_index(streams, abs_index, codec_type):
    """Compute the type-relative stream index for ffmpeg specifiers."""
    count = 0
    for s in streams:
        if int(s.get('index', -1)) == abs_index:
            return count
        if s.get('codec_type') == codec_type:
            count += 1
    return 0


# ═══════════════════════════════════════════════════════════════════
# Tkinter dialog
# ═══════════════════════════════════════════════════════════════════

def show_enhanced_media_info(app, filepath, parent=None):
    """Show the enhanced media info dialog for a file.

    Editable tabs for General, Video, Audio, and Subtitles allow
    modifying track names, language codes, and disposition flags.
    Changes are saved via ffmpeg remux.

    Args:
        app: The VideoConverterApp or StandaloneContext instance.
        filepath: Path to the video file.
        parent: Optional parent window to center on (defaults to app.root).
    """
    # Probe in foreground (fast — typically <1s)
    data = probe_file(filepath)
    if not data['format']:
        messagebox.showerror('Media Details', f'ffprobe failed to read:\n{filepath}')
        return

    sections = build_full_report(filepath, data)

    # ── Change tracking ──
    # Stores original values and current widget variables for diffing
    originals = {}       # (stream_index, field) → original value
    edit_vars = {}       # (stream_index, field) → tk.StringVar or tk.BooleanVar
    container_title_var = tk.StringVar()

    # ── Window ──
    parent_win = parent or app.root
    dlg = tk.Toplevel(parent_win)
    dlg.withdraw()  # hide until positioned
    dlg.title(f'Media Details — {os.path.basename(filepath)}')
    import re as _re
    geom_str = scaled_geometry(dlg, 800, 680)
    dlg.geometry(geom_str)
    dlg.minsize(*scaled_minsize(dlg, 640, 450))
    dlg.resizable(True, True)
    dlg.update_idletasks()
    try:
        gm = _re.match(r'(\d+)x(\d+)', geom_str)
        dw = int(gm.group(1)) if gm else 800
        dh = int(gm.group(2)) if gm else 680
        pw = parent_win.winfo_width()
        ph = parent_win.winfo_height()
        px = parent_win.winfo_x()
        py = parent_win.winfo_y()
        x = px + (pw - dw) // 2
        y = py + (ph - dh) // 2
        dlg.geometry(f'{dw}x{dh}+{max(0, x)}+{max(0, y)}')
    except Exception:
        pass
    dlg.deiconify()  # show after positioning

    # Intercept window close immediately — _on_close defined later via wrapper
    def _close_handler():
        if _on_close_fn[0]:
            _on_close_fn[0]()
        else:
            dlg.destroy()
    _on_close_fn = [None]  # forward reference, set after _on_close is defined
    dlg.protocol('WM_DELETE_WINDOW', _close_handler)

    main_frame = ttk.Frame(dlg, padding=6)
    main_frame.pack(fill='both', expand=True)
    main_frame.columnconfigure(0, weight=1)
    main_frame.rowconfigure(0, weight=1)

    # ── Notebook (tabs) ──
    notebook = ttk.Notebook(main_frame)
    notebook.grid(row=0, column=0, sticky='nsew')

    tab_widgets = {}   # name → Text widget (for read-only tabs and copy)

    # ── Helper: build a read-only text tab ──
    def _add_text_tab(tab_name, content):
        frame = ttk.Frame(notebook, padding=4)
        notebook.add(frame, text=f' {tab_name} ')
        text_frame = ttk.Frame(frame)
        text_frame.pack(fill='both', expand=True)
        text_frame.columnconfigure(0, weight=1)
        text_frame.rowconfigure(0, weight=1)
        text = tk.Text(text_frame, wrap='word', font=('Courier', 10),
                       bg='#1e1e1e', fg='#d4d4d4', insertbackground='#d4d4d4',
                       selectbackground='#264f78', selectforeground='#ffffff',
                       padx=8, pady=8, borderwidth=0, highlightthickness=0)
        text.grid(row=0, column=0, sticky='nsew')
        scrollbar = ttk.Scrollbar(text_frame, orient='vertical', command=text.yview)
        scrollbar.grid(row=0, column=1, sticky='ns')
        text.configure(yscrollcommand=scrollbar.set)
        text.insert('1.0', content)
        text.configure(state='disabled')
        tab_widgets[tab_name] = text

    # ── Helper: add editable fields for a stream ──
    def _build_stream_editor(parent, stream, disp_flags, row_start):
        """Add editable Title, Language, and Disposition widgets for a stream.
        Returns the next available row number."""
        tags = stream.get('tags', {})
        disp = stream.get('disposition', {})
        abs_idx = int(stream.get('index', 0))
        row = row_start

        # Title
        title_var = tk.StringVar(value=tags.get('title', ''))
        originals[(abs_idx, 'title')] = tags.get('title', '')
        edit_vars[(abs_idx, 'title')] = title_var
        ttk.Label(parent, text='Title:', font=('Courier', 10),
                  foreground='#666666').grid(row=row, column=0, sticky='ne', padx=(8, 4), pady=2)
        ttk.Entry(parent, textvariable=title_var, width=40,
                  font=('Courier', 10)).grid(row=row, column=1, sticky='ew', padx=(0, 8), pady=2)
        row += 1

        # Language
        orig_lang = tags.get('language', 'und')
        lang_var = tk.StringVar(value=_lang_display(orig_lang))
        originals[(abs_idx, 'language')] = orig_lang
        edit_vars[(abs_idx, 'language')] = lang_var
        ttk.Label(parent, text='Language:', font=('Courier', 10),
                  foreground='#666666').grid(row=row, column=0, sticky='ne', padx=(8, 4), pady=2)
        lang_combo = ttk.Combobox(parent, textvariable=lang_var,
                                   values=_LANG_VALUES, width=20,
                                   font=('Courier', 10))
        lang_combo.grid(row=row, column=1, sticky='w', padx=(0, 8), pady=2)
        row += 1

        # Disposition flags (skip if none defined for this stream type)
        if disp_flags:
            ttk.Label(parent, text='Flags:', font=('Courier', 10),
                      foreground='#666666').grid(row=row, column=0, sticky='ne', padx=(8, 4), pady=2)
            flags_fr = ttk.Frame(parent)
            flags_fr.grid(row=row, column=1, sticky='w', padx=(0, 8), pady=2)

            for flag_key, flag_label in disp_flags:
                orig_val = bool(disp.get(flag_key, 0))
                flag_var = tk.BooleanVar(value=orig_val)
                originals[(abs_idx, f'disp_{flag_key}')] = orig_val
                edit_vars[(abs_idx, f'disp_{flag_key}')] = flag_var
                ttk.Checkbutton(flags_fr, text=flag_label,
                                variable=flag_var).pack(side='left', padx=(0, 12))
            row += 1

        return row

    # ══════════════════════════════════════════════════════════════
    # TAB: General (editable container title)
    # ══════════════════════════════════════════════════════════════
    gen_frame = ttk.Frame(notebook, padding=4)
    notebook.add(gen_frame, text=' General ')
    gen_canvas, gen_inner = _create_scrollable_frame(gen_frame)
    gen_inner.columnconfigure(1, weight=1)

    fmt = data['format']
    grow = 0

    _add_readonly_row(gen_inner, grow, 'File Name:', os.path.basename(filepath)); grow += 1
    _add_readonly_row(gen_inner, grow, 'Directory:', os.path.dirname(filepath)); grow += 1

    # Editable container title
    orig_title = fmt.get('tags', {}).get('title', '')
    container_title_var.set(orig_title)
    originals[('container', 'title')] = orig_title
    ttk.Label(gen_inner, text='Title/Edition:', font=('Courier', 10),
              foreground='#666666').grid(row=grow, column=0, sticky='ne', padx=(8, 4), pady=2)
    ttk.Entry(gen_inner, textvariable=container_title_var, width=40,
              font=('Courier', 10)).grid(row=grow, column=1, sticky='ew', padx=(0, 8), pady=2)
    grow += 1

    _add_readonly_row(gen_inner, grow, 'Format:',
                      fmt.get('format_long_name', fmt.get('format_name', '?'))); grow += 1
    profile = fmt.get('profile')
    if profile:
        _add_readonly_row(gen_inner, grow, 'Format Profile:', profile); grow += 1
    dur = fmt.get('duration')
    if dur:
        _add_readonly_row(gen_inner, grow, 'Duration:',
                          f'{_fmt_duration(dur)} ({float(dur):.2f}s)'); grow += 1
    _add_readonly_row(gen_inner, grow, 'File Size:', _fmt_size(fmt.get('size'))); grow += 1
    _add_readonly_row(gen_inner, grow, 'Overall Bitrate:', _fmt_bitrate(fmt.get('bit_rate'))); grow += 1
    _add_readonly_row(gen_inner, grow, 'Streams:', fmt.get('nb_streams', '?')); grow += 1

    tab_widgets['General'] = None  # editable tab — no Text widget for copy

    # ══════════════════════════════════════════════════════════════
    # TAB: Video (editable per stream)
    # ══════════════════════════════════════════════════════════════
    video_streams = [s for s in data['streams']
                     if s.get('codec_type') == 'video'
                     and not s.get('disposition', {}).get('attached_pic')]
    if video_streams:
        vid_frame = ttk.Frame(notebook, padding=4)
        notebook.add(vid_frame, text=' Video ')
        vid_canvas, vid_inner = _create_scrollable_frame(vid_frame)
        vid_inner.columnconfigure(1, weight=1)

        vrow = 0
        for si, stream in enumerate(video_streams):
            tags = stream.get('tags', {})
            hdr_label = _derive_hdr_format(stream, data['hdr'])
            lang = tags.get('language', '')

            # Stream header
            header = f'Stream #{stream.get("index", "?")} — VIDEO'
            if lang and lang != 'und':
                header += f'  [{lang.upper()}]'
            lf = ttk.LabelFrame(vid_inner, text=header, padding=6)
            lf.grid(row=vrow, column=0, columnspan=2, sticky='ew', padx=4, pady=(4, 2))
            lf.columnconfigure(1, weight=1)
            vrow += 1

            r = 0
            # Read-only fields — full detail
            codec_name = stream.get('codec_long_name', stream.get('codec_name', '?'))
            _add_readonly_row(lf, r, 'Codec:', codec_name); r += 1
            _add_readonly_row(lf, r, 'Codec Tag:', _safe(stream.get('codec_tag_string'))); r += 1
            _add_readonly_row(lf, r, 'Profile:', _safe(stream.get('profile'))); r += 1
            level = stream.get('level')
            if level and level != -99:
                lv = f'{level / 10:.1f}' if level > 9 else str(level)
                _add_readonly_row(lf, r, 'Level:', lv); r += 1
            _add_readonly_row(lf, r, 'Resolution:',
                              f'{stream.get("width", "?")}x{stream.get("height", "?")}'); r += 1
            coded_w = stream.get('coded_width')
            coded_h = stream.get('coded_height')
            if coded_w and coded_h:
                w, h = stream.get('width'), stream.get('height')
                if coded_w != w or coded_h != h:
                    _add_readonly_row(lf, r, 'Coded Size:', f'{coded_w}x{coded_h}'); r += 1
            sar = stream.get('sample_aspect_ratio')
            dar = stream.get('display_aspect_ratio')
            if sar and sar != '1:1' and sar != '0:1':
                _add_readonly_row(lf, r, 'SAR:', sar); r += 1
            if dar:
                _add_readonly_row(lf, r, 'DAR:', dar); r += 1
            _add_readonly_row(lf, r, 'Frame Rate:',
                              _fmt_framerate(stream.get('r_frame_rate'))); r += 1
            avg_fps = stream.get('avg_frame_rate')
            r_fps = stream.get('r_frame_rate')
            if avg_fps and r_fps and avg_fps != r_fps and avg_fps != '0/0':
                _add_readonly_row(lf, r, 'Avg Frame Rate:', f'{_fmt_framerate(avg_fps)} (VFR)'); r += 1
            nb_frames = stream.get('nb_frames')
            if nb_frames and nb_frames != 'N/A':
                _add_readonly_row(lf, r, 'Frame Count:', nb_frames); r += 1
            field_order = stream.get('field_order')
            if field_order and field_order != 'unknown':
                scan = 'Progressive' if field_order == 'progressive' else f'Interlaced ({field_order})'
                _add_readonly_row(lf, r, 'Scan Type:', scan); r += 1
            _add_readonly_row(lf, r, 'Pixel Format:', _safe(stream.get('pix_fmt'))); r += 1
            bit_depth = stream.get('bits_per_raw_sample')
            if bit_depth:
                _add_readonly_row(lf, r, 'Bit Depth:', f'{bit_depth}-bit'); r += 1
            color_range = stream.get('color_range')
            if color_range and color_range != 'unknown':
                cr_label = 'Limited (TV)' if color_range == 'tv' else 'Full (PC)' if color_range == 'pc' else color_range
                _add_readonly_row(lf, r, 'Color Range:', cr_label); r += 1
            color_space = stream.get('color_space')
            if color_space and color_space != 'unknown':
                _add_readonly_row(lf, r, 'Color Space:', color_space); r += 1
            color_transfer = stream.get('color_transfer')
            if color_transfer and color_transfer != 'unknown':
                _add_readonly_row(lf, r, 'Color Transfer:', color_transfer); r += 1
            color_primaries = stream.get('color_primaries')
            if color_primaries and color_primaries != 'unknown':
                _add_readonly_row(lf, r, 'Color Primaries:', color_primaries); r += 1
            _add_readonly_row(lf, r, 'HDR Format:', hdr_label); r += 1
            md = data['hdr'].get('mastering_display')
            if md:
                min_lum = md.get('min_luminance', '?')
                max_lum = md.get('max_luminance', '?')
                _add_readonly_row(lf, r, 'Mastering Lum:', f'{min_lum} - {max_lum} cd/m²'); r += 1
            cl = data['hdr'].get('content_light')
            if cl:
                _add_readonly_row(lf, r, 'Content Light:',
                                  f'MaxCLL={cl.get("max_content", "?")}, MaxFALL={cl.get("max_average", "?")}'); r += 1
            sbr = stream.get('bit_rate')
            if sbr:
                _add_readonly_row(lf, r, 'Bitrate:', _fmt_bitrate(sbr)); r += 1
            max_br = stream.get('max_bit_rate')
            if max_br:
                _add_readonly_row(lf, r, 'Max Bitrate:', _fmt_bitrate(max_br)); r += 1
            refs = stream.get('refs')
            if refs:
                _add_readonly_row(lf, r, 'Ref Frames:', refs); r += 1
            cc = stream.get('closed_captions')
            if cc and int(cc) == 1:
                _add_readonly_row(lf, r, 'Closed Captions:', 'Yes'); r += 1
            sdur = stream.get('duration') or tags.get('DURATION')
            if sdur:
                try:
                    _add_readonly_row(lf, r, 'Duration:', _fmt_duration(float(sdur))); r += 1
                except (ValueError, TypeError):
                    if ':' in str(sdur):
                        _add_readonly_row(lf, r, 'Duration:', sdur); r += 1

            # Separator before editable section
            ttk.Separator(lf, orient='horizontal').grid(
                row=r, column=0, columnspan=2, sticky='ew', pady=4); r += 1

            # Editable fields
            r = _build_stream_editor(lf, stream, _DISP_FLAGS_VIDEO, r)

        tab_widgets['Video'] = None

    # ══════════════════════════════════════════════════════════════
    # TAB: Audio (editable per stream)
    # ══════════════════════════════════════════════════════════════
    audio_streams = [s for s in data['streams'] if s.get('codec_type') == 'audio']
    if audio_streams:
        aud_frame = ttk.Frame(notebook, padding=4)
        notebook.add(aud_frame, text=' Audio ')
        aud_canvas, aud_inner = _create_scrollable_frame(aud_frame)
        aud_inner.columnconfigure(1, weight=1)

        arow = 0
        for stream in audio_streams:
            tags = stream.get('tags', {})
            lang = tags.get('language', '')

            header = f'Stream #{stream.get("index", "?")} — AUDIO'
            if lang and lang != 'und':
                header += f'  [{lang.upper()}]'
            lf = ttk.LabelFrame(aud_inner, text=header, padding=6)
            lf.grid(row=arow, column=0, columnspan=2, sticky='ew', padx=4, pady=(4, 2))
            lf.columnconfigure(1, weight=1)
            arow += 1

            r = 0
            codec_name = stream.get('codec_long_name', stream.get('codec_name', '?'))
            _add_readonly_row(lf, r, 'Codec:', codec_name); r += 1
            _add_readonly_row(lf, r, 'Codec Tag:', _safe(stream.get('codec_tag_string'))); r += 1
            a_profile = stream.get('profile')
            if a_profile and a_profile != 'unknown':
                _add_readonly_row(lf, r, 'Profile:', a_profile); r += 1
            _add_readonly_row(lf, r, 'Sample Rate:',
                              _safe(stream.get('sample_rate'), ' Hz')); r += 1
            _add_readonly_row(lf, r, 'Channels:', _safe(stream.get('channels'))); r += 1
            a_layout = stream.get('channel_layout')
            if a_layout:
                _add_readonly_row(lf, r, 'Channel Layout:', a_layout); r += 1
            sample_fmt = stream.get('sample_fmt')
            if sample_fmt:
                _add_readonly_row(lf, r, 'Sample Format:', sample_fmt); r += 1
            bps = stream.get('bits_per_raw_sample') or stream.get('bits_per_sample')
            if bps and str(bps) != '0':
                _add_readonly_row(lf, r, 'Bits/Sample:', bps); r += 1
            a_sbr = stream.get('bit_rate')
            if a_sbr:
                _add_readonly_row(lf, r, 'Bitrate:', _fmt_bitrate(a_sbr)); r += 1
            a_max_br = stream.get('max_bit_rate')
            if a_max_br:
                _add_readonly_row(lf, r, 'Max Bitrate:', _fmt_bitrate(a_max_br)); r += 1
            a_sdur = stream.get('duration') or tags.get('DURATION')
            if a_sdur:
                try:
                    _add_readonly_row(lf, r, 'Duration:', _fmt_duration(float(a_sdur))); r += 1
                except (ValueError, TypeError):
                    if ':' in str(a_sdur):
                        _add_readonly_row(lf, r, 'Duration:', a_sdur); r += 1

            ttk.Separator(lf, orient='horizontal').grid(
                row=r, column=0, columnspan=2, sticky='ew', pady=4); r += 1

            r = _build_stream_editor(lf, stream, _DISP_FLAGS_AUDIO, r)

        tab_widgets['Audio'] = None

    # ══════════════════════════════════════════════════════════════
    # TAB: Subtitles (editable per stream)
    # ══════════════════════════════════════════════════════════════
    sub_streams = [s for s in data['streams'] if s.get('codec_type') == 'subtitle']
    if sub_streams:
        sub_frame = ttk.Frame(notebook, padding=4)
        notebook.add(sub_frame, text=' Subtitles ')
        sub_canvas, sub_inner = _create_scrollable_frame(sub_frame)
        sub_inner.columnconfigure(1, weight=1)

        srow = 0
        for stream in sub_streams:
            tags = stream.get('tags', {})
            lang = tags.get('language', '')

            header = f'Stream #{stream.get("index", "?")} — SUBTITLE'
            if lang and lang != 'und':
                header += f'  [{lang.upper()}]'
            lf = ttk.LabelFrame(sub_inner, text=header, padding=6)
            lf.grid(row=srow, column=0, columnspan=2, sticky='ew', padx=4, pady=(4, 2))
            lf.columnconfigure(1, weight=1)
            srow += 1

            r = 0
            _add_readonly_row(lf, r, 'Codec:', stream.get('codec_name', '?')); r += 1
            s_codec_long = stream.get('codec_long_name')
            if s_codec_long and s_codec_long != stream.get('codec_name'):
                _add_readonly_row(lf, r, 'Codec Name:', s_codec_long); r += 1
            s_nb = tags.get('NUMBER_OF_FRAMES') or stream.get('nb_frames')
            if s_nb and s_nb != 'N/A' and s_nb != '0':
                _add_readonly_row(lf, r, 'Events:', s_nb); r += 1
            s_w = stream.get('width')
            s_h = stream.get('height')
            if s_w and s_h and int(s_w) > 0:
                _add_readonly_row(lf, r, 'Resolution:', f'{s_w}x{s_h}'); r += 1
            s_sdur = stream.get('duration') or tags.get('DURATION')
            if s_sdur:
                try:
                    _add_readonly_row(lf, r, 'Duration:', _fmt_duration(float(s_sdur))); r += 1
                except (ValueError, TypeError):
                    if ':' in str(s_sdur):
                        _add_readonly_row(lf, r, 'Duration:', s_sdur); r += 1

            ttk.Separator(lf, orient='horizontal').grid(
                row=r, column=0, columnspan=2, sticky='ew', pady=4); r += 1

            r = _build_stream_editor(lf, stream, _DISP_FLAGS_SUBTITLE, r)

        tab_widgets['Subtitles'] = None

    # ══════════════════════════════════════════════════════════════
    # TAB: Chapters (view mode if chapters exist, edit mode if none)
    # ══════════════════════════════════════════════════════════════
    ch_frame = ttk.Frame(notebook, padding=4)
    notebook.add(ch_frame, text=' Chapters ')
    ch_frame.columnconfigure(0, weight=1)
    ch_frame.rowconfigure(0, weight=1)

    # Chapter data — mutable list of dicts
    chapter_list = []
    for ch in data.get('chapters', []):
        chapter_list.append({
            'start': float(ch.get('start_time', 0)),
            'end': float(ch.get('end_time', 0)),
            'title': ch.get('tags', {}).get('title', ''),
        })
    # Snapshot for change detection
    _orig_chapters = [dict(c) for c in chapter_list]

    # Get file duration for auto-generate and end-time defaults
    _file_duration = 0.0
    try:
        _file_duration = float(data['format'].get('duration', 0))
    except (ValueError, TypeError):
        pass

    # ── View mode frame (chapters exist) ──
    ch_view_frame = ttk.Frame(ch_frame)

    # Treeview for display
    ch_view_tree_frame = ttk.Frame(ch_view_frame)
    ch_view_tree_frame.pack(fill='both', expand=True)
    ch_view_tree_frame.columnconfigure(0, weight=1)
    ch_view_tree_frame.rowconfigure(0, weight=1)

    ch_view_tree = ttk.Treeview(ch_view_tree_frame, columns=('num', 'start', 'end', 'title'),
                                 show='headings', height=12)
    ch_view_tree.grid(row=0, column=0, sticky='nsew')
    ch_view_tree.heading('num', text='#')
    ch_view_tree.heading('start', text='Start')
    ch_view_tree.heading('end', text='End')
    ch_view_tree.heading('title', text='Title')
    ch_view_tree.column('num', width=40, minwidth=30, anchor='center')
    ch_view_tree.column('start', width=110, minwidth=80, anchor='center')
    ch_view_tree.column('end', width=110, minwidth=80, anchor='center')
    ch_view_tree.column('title', width=300, minwidth=150)

    ch_view_scroll = ttk.Scrollbar(ch_view_tree_frame, orient='vertical', command=ch_view_tree.yview)
    ch_view_scroll.grid(row=0, column=1, sticky='ns')
    ch_view_tree.configure(yscrollcommand=ch_view_scroll.set)

    ch_view_btn_frame = ttk.Frame(ch_view_frame)
    ch_view_btn_frame.pack(fill='x', pady=(6, 0))
    ttk.Label(ch_view_btn_frame,
              text=f'{len(chapter_list)} chapter(s)').pack(side='left', padx=4)
    ttk.Button(ch_view_btn_frame, text='Edit Chapters...',
               command=lambda: _ch_switch_to_edit()).pack(side='right', padx=4)

    def _ch_populate_view():
        ch_view_tree.delete(*ch_view_tree.get_children())
        for i, ch in enumerate(chapter_list):
            ch_view_tree.insert('', 'end', values=(
                i + 1,
                format_chapter_time(ch['start']),
                format_chapter_time(ch['end']),
                ch['title'],
            ))

    # ── Edit mode frame (no chapters, or user clicked Edit) ──
    ch_edit_outer = ttk.Frame(ch_frame)
    ch_edit_outer.columnconfigure(0, weight=1)
    ch_edit_outer.rowconfigure(1, weight=1)

    # Edit toolbar
    ch_toolbar = ttk.Frame(ch_edit_outer)
    ch_toolbar.grid(row=0, column=0, sticky='ew', pady=(0, 4))

    # Edit treeview
    ch_tree_frame = ttk.Frame(ch_edit_outer)
    ch_tree_frame.grid(row=1, column=0, sticky='nsew')
    ch_tree_frame.columnconfigure(0, weight=1)
    ch_tree_frame.rowconfigure(0, weight=1)

    ch_tree = ttk.Treeview(ch_tree_frame, columns=('start', 'end', 'title'),
                            show='headings', height=10)
    ch_tree.grid(row=0, column=0, sticky='nsew')
    ch_tree.heading('start', text='Start')
    ch_tree.heading('end', text='End')
    ch_tree.heading('title', text='Title')
    ch_tree.column('start', width=120, minwidth=80, anchor='center')
    ch_tree.column('end', width=120, minwidth=80, anchor='center')
    ch_tree.column('title', width=300, minwidth=150)

    ch_scroll = ttk.Scrollbar(ch_tree_frame, orient='vertical', command=ch_tree.yview)
    ch_scroll.grid(row=0, column=1, sticky='ns')
    ch_tree.configure(yscrollcommand=ch_scroll.set)

    def _ch_rebuild():
        ch_tree.delete(*ch_tree.get_children())
        for i, ch in enumerate(chapter_list):
            ch_tree.insert('', 'end', values=(
                format_chapter_time(ch['start']),
                format_chapter_time(ch['end']),
                ch['title'],
            ))

    # ── Double-click inline editing on title column ──
    _ch_edit_widget = [None]  # mutable ref for the floating Entry

    def _ch_on_double_click(event):
        """Open an inline Entry over the title cell for editing."""
        item = ch_tree.identify_row(event.y)
        col = ch_tree.identify_column(event.x)
        if not item or col != '#3':  # #3 = title column
            return

        items = ch_tree.get_children()
        idx = list(items).index(item)
        if idx >= len(chapter_list):
            return

        # Dismiss any existing edit widget
        _ch_dismiss_edit()

        # Get cell bounding box
        bbox = ch_tree.bbox(item, column='title')
        if not bbox:
            return
        x, y, w, h = bbox

        # Create Entry widget over the cell
        current_title = chapter_list[idx]['title']
        entry = tk.Entry(ch_tree, font=('TkDefaultFont', 10))
        entry.insert(0, current_title)
        entry.select_range(0, 'end')
        entry.place(x=x, y=y, width=w, height=h)
        entry.focus_set()
        _ch_edit_widget[0] = (entry, idx)

        def _save_edit(event=None):
            new_title = entry.get()
            chapter_list[idx]['title'] = new_title
            ch_tree.set(item, 'title', new_title)
            _ch_dismiss_edit()

        def _cancel_edit(event=None):
            _ch_dismiss_edit()

        entry.bind('<Return>', _save_edit)
        entry.bind('<Escape>', _cancel_edit)
        entry.bind('<FocusOut>', _save_edit)

    def _ch_dismiss_edit():
        """Remove the floating edit widget if it exists."""
        if _ch_edit_widget[0]:
            widget, _ = _ch_edit_widget[0]
            widget.destroy()
            _ch_edit_widget[0] = None

    ch_tree.bind('<Double-1>', _ch_on_double_click)

    def _ch_add():
        if chapter_list:
            last_end = chapter_list[-1]['end']
        else:
            last_end = 0.0
        new_end = min(last_end + 300, _file_duration) if _file_duration else last_end + 300
        chapter_list.append({
            'start': last_end,
            'end': new_end,
            'title': f'Chapter {len(chapter_list) + 1}',
        })
        _ch_rebuild()
        items = ch_tree.get_children()
        if items:
            ch_tree.selection_set(items[-1])
            ch_tree.see(items[-1])

    def _ch_remove():
        _ch_dismiss_edit()
        sel = ch_tree.selection()
        if not sel:
            return
        items = ch_tree.get_children()
        idx = list(items).index(sel[0])
        if idx < len(chapter_list):
            del chapter_list[idx]
            _ch_rebuild()

    def _ch_clear():
        _ch_dismiss_edit()
        chapter_list.clear()
        _ch_rebuild()

    def _ch_auto_generate():
        interval = _ch_interval_var.get()
        if not _file_duration:
            messagebox.showwarning('Chapters', 'File duration unknown — cannot auto-generate.')
            return
        new_chapters = generate_auto_chapters(_file_duration, interval)
        if new_chapters:
            chapter_list.clear()
            chapter_list.extend(new_chapters)
            _ch_rebuild()

    # Toolbar buttons
    ttk.Button(ch_toolbar, text='Add', command=_ch_add, width=6).pack(side='left', padx=2)
    ttk.Button(ch_toolbar, text='Remove', command=_ch_remove, width=7).pack(side='left', padx=2)
    ttk.Button(ch_toolbar, text='Clear All', command=_ch_clear, width=8).pack(side='left', padx=2)
    ttk.Separator(ch_toolbar, orient='vertical').pack(side='left', fill='y', padx=8)
    ttk.Label(ch_toolbar, text='Generate every').pack(side='left', padx=(0, 2))
    _ch_interval_var = tk.IntVar(value=5)
    tk.Spinbox(ch_toolbar, textvariable=_ch_interval_var, from_=1, to=60,
               width=3).pack(side='left', padx=(0, 2))
    ttk.Label(ch_toolbar, text='min').pack(side='left', padx=(0, 4))
    ttk.Button(ch_toolbar, text='Generate', command=_ch_auto_generate,
               width=8).pack(side='left', padx=2)

    # ── Mode switching ──
    def _ch_switch_to_edit():
        """Switch from view mode to edit mode."""
        ch_view_frame.pack_forget()
        ch_edit_outer.pack(fill='both', expand=True)
        _ch_rebuild()

    def _ch_switch_to_view():
        """Switch from edit mode to view mode."""
        ch_edit_outer.pack_forget()
        ch_view_frame.pack(fill='both', expand=True)
        _ch_populate_view()

    # Show the appropriate initial mode
    if chapter_list:
        _ch_populate_view()
        ch_view_frame.pack(fill='both', expand=True)
    else:
        ch_edit_outer.pack(fill='both', expand=True)

    tab_widgets['Chapters'] = None  # editable tab

    # ══════════════════════════════════════════════════════════════
    # Read-only tabs: Attachments, Metadata, Full Report
    # ══════════════════════════════════════════════════════════════
    if 'Attachments' in sections:
        _add_text_tab('Attachments', sections['Attachments'])
    _add_text_tab('Full Report', sections.get('full', ''))

    # ══════════════════════════════════════════════════════════════
    # Change detection
    # ══════════════════════════════════════════════════════════════
    def _chapters_changed():
        """Check if chapter list differs from original."""
        if len(chapter_list) != len(_orig_chapters):
            return True
        for a, b in zip(chapter_list, _orig_chapters):
            if (abs(a['start'] - b['start']) > 0.001
                    or abs(a['end'] - b['end']) > 0.001
                    or a['title'] != b['title']):
                return True
        return False

    def _has_changes():
        """Check if any editable field has been modified."""
        # Container title
        if container_title_var.get() != originals.get(('container', 'title'), ''):
            return True
        # Chapters
        if _chapters_changed():
            return True
        # Stream fields
        for (idx, field), var in edit_vars.items():
            orig = originals.get((idx, field))
            if field == 'language':
                current = _lang_code_from_display(var.get())
                if current != orig:
                    return True
            elif isinstance(var, tk.BooleanVar):
                if var.get() != orig:
                    return True
            else:
                if var.get() != orig:
                    return True
        return False

    # ══════════════════════════════════════════════════════════════
    # Save / Remux
    # ══════════════════════════════════════════════════════════════
    def _apply_changes():
        """Build ffmpeg remux command and apply metadata/disposition changes."""
        if not _has_changes():
            return

        save_btn.configure(state='disabled')
        dlg.after(0, _show_progress)

        base, ext = os.path.splitext(filepath)
        tmp_out = f'{base}_tagfix_tmp{ext}'

        cmd = ['ffmpeg', '-y', '-i', filepath, '-map', '0', '-c', 'copy']

        streams = data['streams']

        # Container title
        new_title = container_title_var.get()
        orig_title = originals.get(('container', 'title'), '')
        if new_title != orig_title:
            cmd.extend(['-metadata', f'title={new_title}'])

        # Collect which stream types had disposition changes
        # (we must emit disposition for ALL streams of that type)
        disp_changed_types = set()
        for (idx, field), var in edit_vars.items():
            if field.startswith('disp_'):
                orig = originals.get((idx, field))
                if isinstance(var, tk.BooleanVar) and var.get() != orig:
                    # Find the codec_type for this stream
                    for s in streams:
                        if int(s.get('index', -1)) == idx:
                            disp_changed_types.add(s.get('codec_type'))
                            break

        # Per-stream metadata and disposition
        for stream in streams:
            abs_idx = int(stream.get('index', 0))
            codec_type = stream.get('codec_type', '')
            type_idx = _compute_type_index(streams, abs_idx, codec_type)
            spec = f's:{codec_type[0]}:{type_idx}' if codec_type else f's:{abs_idx}'

            # Title
            title_var = edit_vars.get((abs_idx, 'title'))
            if title_var:
                new_val = title_var.get()
                if new_val != originals.get((abs_idx, 'title'), ''):
                    cmd.extend([f'-metadata:{spec}', f'title={new_val}'])

            # Language
            lang_var = edit_vars.get((abs_idx, 'language'))
            if lang_var:
                new_lang = _lang_code_from_display(lang_var.get())
                if new_lang != originals.get((abs_idx, 'language'), 'und'):
                    cmd.extend([f'-metadata:{spec}', f'language={new_lang}'])

            # Disposition: emit for ALL streams of types that had changes
            if codec_type in disp_changed_types:
                disp_flags = []
                all_disp_keys = [k for k in originals if k[0] == abs_idx and k[1].startswith('disp_')]
                for _, field in all_disp_keys:
                    flag_name = field[5:]  # strip 'disp_' prefix
                    var = edit_vars.get((abs_idx, field))
                    if var and var.get():
                        disp_flags.append(flag_name)
                disp_str = '+'.join(disp_flags) if disp_flags else '0'
                cmd.extend([f'-disposition:{abs_idx}', disp_str])

        # Chapters
        ch_meta_file = None
        if _chapters_changed():
            if chapter_list:
                ch_meta_file = chapters_to_ffmetadata(chapter_list)
                if ch_meta_file:
                    # Insert chapter metadata file as an additional input
                    # Must go before -map, so rebuild command with extra input
                    idx_insert = cmd.index('-map')
                    cmd.insert(idx_insert, ch_meta_file)
                    cmd.insert(idx_insert, '-i')
                    # Map chapters from the new input (input index 1)
                    cmd.extend(['-map_chapters', '1'])
            else:
                # All chapters removed
                cmd.extend(['-map_chapters', '-1'])

        cmd.append(tmp_out)

        # Get total duration for progress calculation
        total_dur = _file_duration or 0.0

        def _run_remux():
            import re as _re
            stderr_output = []
            try:
                # Use Popen so we can parse progress from stderr
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    universal_newlines=False)

                # Read stderr character by character to handle \r-terminated progress
                buf = ''
                while True:
                    ch_byte = proc.stderr.read(1)
                    if not ch_byte:
                        break
                    try:
                        c = ch_byte.decode('utf-8', errors='replace')
                    except Exception:
                        c = '?'
                    if c in ('\r', '\n'):
                        line = buf.strip()
                        buf = ''
                        if not line:
                            continue
                        stderr_output.append(line)
                        # Parse time= from ffmpeg progress
                        if total_dur > 0 and 'time=' in line:
                            m = _re.search(r'time=(\d+):(\d+):(\d+\.\d+)', line)
                            if m:
                                t = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
                                pct = min(100.0, (t / total_dur) * 100)
                                dlg.after(0, lambda p=pct: _progress_var.set(p))
                    else:
                        buf += c

                proc.wait(timeout=600)

                if proc.returncode != 0:
                    err = '\n'.join(stderr_output[-10:]) if stderr_output else 'Unknown error'
                    dlg.after(0, lambda: messagebox.showerror(
                        'Save Failed', f'ffmpeg returned an error:\n\n{err}'))
                    if os.path.exists(tmp_out):
                        os.unlink(tmp_out)
                    dlg.after(0, _hide_progress)
                    dlg.after(0, lambda: save_btn.configure(text='Save Changes', state='normal'))
                    return

                dlg.after(0, lambda: _progress_var.set(100))

                # Atomic replace
                os.replace(tmp_out, filepath)

                # Re-probe and refresh
                dlg.after(0, _on_save_success)

            except Exception as e:
                dlg.after(0, lambda: messagebox.showerror(
                    'Save Failed', f'Error during remux:\n\n{e}'))
                if os.path.exists(tmp_out):
                    try:
                        os.unlink(tmp_out)
                    except OSError:
                        pass
                dlg.after(0, _hide_progress)
                dlg.after(0, lambda: save_btn.configure(text='Save Changes', state='normal'))
            finally:
                # Clean up temp chapter metadata file
                if ch_meta_file and os.path.exists(ch_meta_file):
                    try:
                        os.unlink(ch_meta_file)
                    except OSError:
                        pass

        threading.Thread(target=_run_remux, daemon=True).start()

    def _on_save_success():
        """Called after a successful save — update originals and show confirmation."""
        # Update originals from current widget values so _has_changes() returns False
        for (idx, field), var in edit_vars.items():
            if field == 'language':
                originals[(idx, field)] = _lang_code_from_display(var.get())
            elif isinstance(var, tk.BooleanVar):
                originals[(idx, field)] = var.get()
            else:
                originals[(idx, field)] = var.get()
        originals[('container', 'title')] = container_title_var.get()

        # Update chapter originals
        _orig_chapters.clear()
        _orig_chapters.extend([dict(c) for c in chapter_list])

        # Re-probe and refresh chapter list + text-based tabs
        new_data = probe_file(filepath)
        if new_data['format']:
            # Refresh chapter list from new probe data
            chapter_list.clear()
            for ch in new_data.get('chapters', []):
                chapter_list.append({
                    'start': float(ch.get('start_time', 0)),
                    'end': float(ch.get('end_time', 0)),
                    'title': ch.get('tags', {}).get('title', ''),
                })
            _orig_chapters.clear()
            _orig_chapters.extend([dict(c) for c in chapter_list])
            _ch_rebuild()

            new_sections = build_full_report(filepath, new_data)
            for tab_name in ('Attachments', 'Metadata', 'Full Report'):
                widget = tab_widgets.get(tab_name)
                key = 'full' if tab_name == 'Full Report' else tab_name
                if widget and key in new_sections:
                    widget.configure(state='normal')
                    widget.delete('1.0', 'end')
                    widget.insert('1.0', new_sections[key])
                    widget.configure(state='disabled')

        _progress_label.configure(text='Saved!')

        if _close_after_save[0]:
            # User chose "Yes" on the close warning — close after brief confirmation
            dlg.after(500, dlg.destroy)
            return

        dlg.after(1500, _hide_progress)
        save_btn.configure(state='disabled')
        dlg.after(2000, lambda: save_btn.configure(text='Save Changes',
                                                     state='normal' if _has_changes() else 'disabled'))

    # ══════════════════════════════════════════════════════════════
    # Progress bar (hidden until save)
    # ══════════════════════════════════════════════════════════════
    progress_frame = ttk.Frame(main_frame)
    # row 1 — shown/hidden dynamically
    progress_frame.columnconfigure(1, weight=1)
    _progress_var = tk.DoubleVar(value=0)
    _progress_label = ttk.Label(progress_frame, text='Saving...')
    _progress_label.grid(row=0, column=0, sticky='w', padx=(0, 8))
    _progress_bar = ttk.Progressbar(progress_frame, variable=_progress_var,
                                     maximum=100, mode='determinate')
    _progress_bar.grid(row=0, column=1, sticky='ew')

    def _show_progress():
        progress_frame.grid(row=1, column=0, sticky='ew', pady=(4, 0))
        _progress_var.set(0)
        _progress_label.configure(text='Saving...')

    def _hide_progress():
        progress_frame.grid_forget()

    # ══════════════════════════════════════════════════════════════
    # Button bar
    # ══════════════════════════════════════════════════════════════
    btn_frame = ttk.Frame(main_frame)
    btn_frame.grid(row=2, column=0, sticky='ew', pady=(6, 0))

    def _copy_current():
        """Copy the current tab's content to clipboard."""
        current_tab = notebook.tab(notebook.select(), 'text').strip()
        widget = tab_widgets.get(current_tab)
        if widget:
            content = widget.get('1.0', 'end-1c')
            dlg.clipboard_clear()
            dlg.clipboard_append(content)
            copy_btn.configure(text='Copied!')
            dlg.after(1500, lambda: copy_btn.configure(text='Copy to Clipboard'))

    def _copy_all():
        """Copy the full report to clipboard."""
        dlg.clipboard_clear()
        dlg.clipboard_append(sections.get('full', ''))
        copy_all_btn.configure(text='Copied!')
        dlg.after(1500, lambda: copy_all_btn.configure(text='Copy Full Report'))

    copy_btn = ttk.Button(btn_frame, text='Copy to Clipboard', command=_copy_current)
    copy_btn.pack(side='left', padx=(0, 4))

    copy_all_btn = ttk.Button(btn_frame, text='Copy Full Report', command=_copy_all)
    copy_all_btn.pack(side='left', padx=(0, 4))

    save_btn = ttk.Button(btn_frame, text='Save Changes', command=_apply_changes)
    save_btn.pack(side='right', padx=(4, 0))

    _close_after_save = [False]

    def _on_close():
        if _has_changes():
            answer = messagebox.askyesnocancel(
                'Unsaved Changes',
                'You have unsaved changes.\n\nSave before closing?',
                parent=dlg)
            if answer is True:
                _close_after_save[0] = True
                _apply_changes()
                return  # dialog stays open; _on_save_success will close it
            elif answer is None:
                return  # Cancel — don't close
            # answer is False — discard changes and close
        dlg.destroy()

    _on_close_fn[0] = _on_close  # register with the WM_DELETE_WINDOW handler
    ttk.Button(btn_frame, text='Close', command=_on_close).pack(side='right')
