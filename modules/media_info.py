"""
Docflix Media Suite — Enhanced Media Details

Comprehensive media file analysis tool using ffprobe.
Displays container, video, audio, subtitle, chapter, and
metadata information in a tabbed dialog with copy-to-clipboard.
"""

import json
import os
import subprocess
import tkinter as tk
from tkinter import ttk, messagebox

from .utils import scaled_geometry, scaled_minsize


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
        # Show common nice values
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


# ═══════════════════════════════════════════════════════════════════
# Report builder — produces the text for each section
# ═══════════════════════════════════════════════════════════════════

def _build_general_section(filepath, fmt):
    """Build the General / Container section."""
    lines = []
    lines.append(f'  File Name:      {os.path.basename(filepath)}')
    lines.append(f'  Directory:      {os.path.dirname(filepath)}')

    # Edition / container title
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

    # Header
    lang = tags.get('language', '')
    title = tags.get('title', '')
    hdr_label = _derive_hdr_format(stream, hdr)
    header_parts = [f'STREAM #{stream.get("index", "?")} — VIDEO']
    if lang and lang != 'und':
        header_parts.append(f'[{lang.upper()}]')
    lines.append(f'  {"  ".join(header_parts)}')
    lines.append(f'  {"─" * 52}')

    # Codec
    codec_name = stream.get('codec_long_name', stream.get('codec_name', '?'))
    lines.append(f'    Codec:            {codec_name}')
    lines.append(f'    Codec Tag:        {_safe(stream.get("codec_tag_string"))}')
    lines.append(f'    Profile:          {_safe(stream.get("profile"))}')

    level = stream.get('level')
    if level and level != -99:
        # H.264/H.265 levels are stored as integers (41 = Level 4.1)
        if level > 9:
            lines.append(f'    Level:            {level / 10:.1f}')
        else:
            lines.append(f'    Level:            {level}')

    if title:
        lines.append(f'    Title:            {title}')

    # Dimensions
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

    # Timing
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

    # Color
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

    # HDR
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

    # Bitrate
    sbr = stream.get('bit_rate')
    if sbr:
        lines.append(f'    Bitrate:          {_fmt_bitrate(sbr)}')
    max_br = stream.get('max_bit_rate')
    if max_br:
        lines.append(f'    Max Bitrate:      {_fmt_bitrate(max_br)}')

    # Misc
    refs = stream.get('refs')
    if refs:
        lines.append(f'    Reference Frames: {refs}')

    cc = stream.get('closed_captions')
    if cc and int(cc) == 1:
        lines.append(f'    Closed Captions:  Yes')

    # Disposition
    flags = []
    if disp.get('default'):       flags.append('Default')
    if disp.get('attached_pic'):  flags.append('Cover Art')
    if flags:
        lines.append(f'    Disposition:      {", ".join(flags)}')

    # Stream duration
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

    # Disposition flags
    flags = []
    if disp.get('default'):           flags.append('Default')
    if disp.get('forced'):            flags.append('Forced')
    if disp.get('visual_impaired'):   flags.append('Visual Impaired')
    if disp.get('comment'):           flags.append('Commentary')
    if disp.get('original'):          flags.append('Original')
    if flags:
        lines.append(f'    Disposition:      {", ".join(flags)}')

    # Stream duration
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

    # Event count
    nb = tags.get('NUMBER_OF_FRAMES') or stream.get('nb_frames')
    if nb and nb != 'N/A' and nb != '0':
        lines.append(f'    Events:           {nb}')

    # Bitmap subtitle resolution
    w = stream.get('width')
    h = stream.get('height')
    if w and h and int(w) > 0:
        lines.append(f'    Resolution:       {w}x{h}')

    # Flags
    flags = []
    if disp.get('default'):            flags.append('Default')
    if disp.get('forced'):             flags.append('Forced')
    if disp.get('hearing_impaired'):   flags.append('SDH')
    if disp.get('comment'):            flags.append('Commentary')
    if flags:
        lines.append(f'    Disposition:      {", ".join(flags)}')

    # Duration
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

    # Display tags in a friendly order, then any remaining
    priority_keys = [
        ('title', 'Title'),
        ('artist', 'Artist'),
        ('album', 'Album'),
        ('album_artist', 'Album Artist'),
        ('date', 'Date'),
        ('creation_time', 'Creation Time'),
        ('year', 'Year'),
        ('genre', 'Genre'),
        ('show', 'Show'),
        ('season_number', 'Season'),
        ('episode_sort', 'Episode'),
        ('episode_id', 'Episode ID'),
        ('network', 'Network'),
        ('description', 'Description'),
        ('synopsis', 'Synopsis'),
        ('comment', 'Comment'),
        ('copyright', 'Copyright'),
        ('encoder', 'Encoder'),
        ('encoding_tool', 'Encoding Tool'),
        ('handler_name', 'Handler'),
        ('ENCODER', 'Encoder'),
        ('COMPATIBLE_BRANDS', 'Compatible Brands'),
    ]

    shown = set()
    for key, label in priority_keys:
        # Case-insensitive tag lookup
        val = tags.get(key) or tags.get(key.upper()) or tags.get(key.lower())
        if val and key.lower() not in shown:
            lines.append(f'    {label + ":":<22}{val}')
            shown.add(key.lower())

    # Show remaining tags not in priority list
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
    # Cover art video streams
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
# Tkinter dialog
# ═══════════════════════════════════════════════════════════════════

def show_enhanced_media_info(app, filepath):
    """Show the enhanced media info dialog for a file.

    Args:
        app: The VideoConverterApp or StandaloneContext instance.
        filepath: Path to the video file.
    """
    # Probe in foreground (fast — typically <1s)
    data = probe_file(filepath)
    if not data['format']:
        messagebox.showerror('Media Details', f'ffprobe failed to read:\n{filepath}')
        return

    sections = build_full_report(filepath, data)

    # ── Window ──
    dlg = tk.Toplevel(app.root)
    dlg.title(f'Enhanced Media Details — {os.path.basename(filepath)}')
    dlg.geometry(scaled_geometry(dlg, 780, 620))
    dlg.minsize(*scaled_minsize(dlg, 600, 400))
    dlg.resizable(True, True)
    try:
        app._center_on_main(dlg)
    except Exception:
        pass

    main_frame = ttk.Frame(dlg, padding=6)
    main_frame.pack(fill='both', expand=True)
    main_frame.columnconfigure(0, weight=1)
    main_frame.rowconfigure(0, weight=1)

    # ── Notebook (tabs) ──
    notebook = ttk.Notebook(main_frame)
    notebook.grid(row=0, column=0, sticky='nsew')

    tab_widgets = {}  # name → ScrolledText widget

    tab_order = ['General', 'Video', 'Audio', 'Subtitles', 'Chapters',
                 'Attachments', 'Metadata', 'Full Report']

    for tab_name in tab_order:
        key = 'full' if tab_name == 'Full Report' else tab_name
        if key not in sections:
            continue

        frame = ttk.Frame(notebook, padding=4)
        notebook.add(frame, text=f' {tab_name} ')

        # Use a Text widget with scrollbar
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

        # Insert content
        text.insert('1.0', sections[key])
        text.configure(state='disabled')

        tab_widgets[tab_name] = text

    # ── Button bar ──
    btn_frame = ttk.Frame(main_frame)
    btn_frame.grid(row=1, column=0, sticky='ew', pady=(6, 0))

    def _copy_current():
        """Copy the current tab's content to clipboard."""
        current_tab = notebook.tab(notebook.select(), 'text').strip()
        widget = tab_widgets.get(current_tab)
        if widget:
            content = widget.get('1.0', 'end-1c')
            dlg.clipboard_clear()
            dlg.clipboard_append(content)
            # Flash the button text briefly
            copy_btn.configure(text='Copied!')
            dlg.after(1500, lambda: copy_btn.configure(text='📋 Copy to Clipboard'))

    def _copy_all():
        """Copy the full report to clipboard."""
        dlg.clipboard_clear()
        dlg.clipboard_append(sections.get('full', ''))
        copy_all_btn.configure(text='Copied!')
        dlg.after(1500, lambda: copy_all_btn.configure(text='📋 Copy Full Report'))

    copy_btn = ttk.Button(btn_frame, text='📋 Copy to Clipboard', command=_copy_current)
    copy_btn.pack(side='left', padx=(0, 4))

    copy_all_btn = ttk.Button(btn_frame, text='📋 Copy Full Report', command=_copy_all)
    copy_all_btn.pack(side='left', padx=(0, 4))

    ttk.Button(btn_frame, text='Close', command=dlg.destroy).pack(side='right')
