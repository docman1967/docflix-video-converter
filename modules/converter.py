"""
Docflix Video Converter — Conversion Engine

The VideoConverter class builds and runs ffmpeg commands
for video transcoding. Supports CPU and GPU encoding,
two-pass, subtitle handling, metadata cleanup, and
closed caption passthrough.
"""

import json
import os
import re
import shutil
import subprocess
import threading
from datetime import datetime

from .constants import (
    GPU_BACKENDS, VIDEO_CODEC_MAP,
    get_gpu_encoder,
)
from .utils import (
    get_video_duration, get_subtitle_streams,
)
from .gpu import (
    get_video_pix_fmt, detect_closed_captions,
    extract_closed_captions_to_srt, _A53CC_ENCODER_FLAGS,
)


class VideoConverter:
    """Handles video conversion using ffmpeg"""
    
    def __init__(self, log_callback=None, progress_callback=None):
        self.log_callback = log_callback
        self.progress_callback = progress_callback
        self.current_process = None
        self.is_paused = False
        self.is_stopped = False
    
    def log(self, message, level='INFO'):
        """Send log message to callback"""
        if self.log_callback:
            timestamp = datetime.now().strftime('%H:%M:%S')
            self.log_callback(f"[{timestamp}] [{level}] {message}")
    
    def convert_file(self, input_path, output_path, settings):
        """
        Convert a single video file

        settings dict:
            - transcode_mode: 'video', 'audio', or 'both'
            - encoder: 'cpu' or GPU backend id (e.g. 'nvenc', 'qsv', 'vaapi')
            - mode: 'bitrate' or 'crf'
            - bitrate: e.g., '2M'
            - crf: int 0-51
            - preset: CPU preset or GPU preset
            - audio_codec: 'aac', 'mp3', 'opus', etc.
            - audio_bitrate: e.g., '128k'
        """
        self.is_paused = False
        self.is_stopped = False

        try:
            encoder       = settings.get('encoder', 'cpu')
            hw_decode     = settings.get('hw_decode', False)
            transcode_mode = settings.get('transcode_mode', 'both')
            codec_info    = settings.get('codec_info', VIDEO_CODEC_MAP['H.265 / HEVC'])
            codec_name    = settings.get('codec_name', 'H.265 / HEVC')
            mode          = settings.get('mode', 'bitrate')
            two_pass      = settings.get('two_pass', False)
            is_gpu        = encoder != 'cpu'
            backend       = GPU_BACKENDS.get(encoder) if is_gpu else None

            # Resolve the actual ffmpeg encoder name
            if is_gpu:
                video_enc_name = get_gpu_encoder(codec_name, encoder) or codec_info['cpu_encoder']
            else:
                video_enc_name = codec_info['cpu_encoder']

            # Two-pass only makes sense for CPU bitrate mode on supported codecs
            cpu_encoder = codec_info.get('cpu_encoder', '')
            TWO_PASS_SUPPORTED = {'libx265', 'libx264', 'libvpx-vp9', 'mpeg4'}
            use_two_pass = (
                two_pass and
                encoder == 'cpu' and
                mode == 'bitrate' and
                cpu_encoder in TWO_PASS_SUPPORTED and
                transcode_mode in ('video', 'both')
            )

            # GPU multipass (separate concept from CPU two-pass)
            use_gpu_multipass = (
                two_pass and
                is_gpu and
                backend is not None and
                mode == 'bitrate' and
                video_enc_name in backend.get('multipass_encoders', set())
            )

            # ── External subtitle handling ──
            ext_subs = settings.get('external_subs', [])
            embed_subs = [s for s in ext_subs if s['mode'] == 'embed']
            # Forced subtitles first so they appear as the first subtitle track(s)
            embed_subs.sort(key=lambda s: (not s.get('forced', False)))
            burn_in_subs = [s for s in ext_subs if s['mode'] == 'burn_in']
            has_burn_in = bool(burn_in_subs)

            # Burn-in is incompatible with hardware decode
            effective_hw = hw_decode and not has_burn_in

            if has_burn_in and hw_decode:
                self.log("Hardware decode disabled: burn-in subtitles require CPU filter pipeline", 'WARNING')
            if len(burn_in_subs) > 1:
                self.log("Warning: only the first burn-in subtitle will be rendered", 'WARNING')

            # ── Pixel format compatibility check ──
            # When using GPU hwaccel, frames stay on the device in the source
            # pixel format.  If the source is 10-bit (e.g. yuv420p10le) but the
            # target encoder only supports 8-bit (e.g. h264_nvenc), we need a
            # scale filter to convert the pixel format.
            # NOTE: We no longer use -hwaccel_output_format cuda because it
            # fails on sources with mid-stream resolution changes (the scale_cuda
            # filter doesn't support filter graph reinit).  Without it, frames
            # pass through system memory where format conversion is automatic.
            needs_pix_fmt_convert = False
            if effective_hw and is_gpu and backend and video_enc_name not in (None, 'copy'):
                src_pix_fmt = get_video_pix_fmt(input_path) or ''
                is_10bit_src = '10' in src_pix_fmt  # yuv420p10le, p010le, etc.
                _8bit_only_encoders = {'h264_nvenc', 'h264_qsv', 'h264_vaapi'}
                if is_10bit_src and video_enc_name in _8bit_only_encoders:
                    needs_pix_fmt_convert = True
                    self.log(f"Source is 10-bit ({src_pix_fmt}) — adding pixel format "
                             f"conversion for {video_enc_name}", 'INFO')

            # Edited subtitles — maps stream_index → (temp_srt_path, input_index)
            edited_subs = settings.get('edited_subs', {})
            # Build ordered list of edited sub inputs for consistent input indexing
            _edited_sub_inputs = sorted(edited_subs.items())  # [(stream_idx, path), ...]

            # ── Closed caption handling (ATSC A53 / EIA-608 / CEA-708) ──
            cc_srt_path = None
            has_cc = settings.get('has_closed_captions', False)
            if has_cc and settings.get('extract_cc', True):
                import tempfile
                import shutil as _shutil
                if _shutil.which('ccextractor'):
                    cc_tmp = tempfile.NamedTemporaryFile(suffix='_cc.srt', delete=False, dir=os.path.dirname(output_path))
                    cc_tmp.close()
                    self.log("Extracting ATSC A53 closed captions with ccextractor…", 'INFO')
                    if extract_closed_captions_to_srt(input_path, cc_tmp.name):
                        cc_srt_path = cc_tmp.name
                        self.log("Closed captions extracted to SRT successfully", 'SUCCESS')
                    else:
                        self.log("ccextractor could not extract caption data", 'WARNING')
                        try:
                            os.remove(cc_tmp.name)
                        except OSError:
                            pass
                else:
                    self.log("ccextractor not found — CC will be preserved via A53 passthrough only", 'INFO')

            # A53 CC passthrough: embed CC data in the output video bitstream
            # This preserves CC for players that support it (VLC, mpv, etc.)
            cc_passthrough_flags = []
            if has_cc and video_enc_name and video_enc_name != 'copy':
                flags = _A53CC_ENCODER_FLAGS.get(video_enc_name)
                if flags is not None:
                    cc_passthrough_flags = flags
                    self.log(f"A53 CC passthrough enabled for {video_enc_name}", 'INFO')

            # ── Chapter injection ──
            chapters_metadata_path = None
            chapters = settings.get('chapters', [])
            if chapters and not settings.get('strip_chapters', False):
                from .chapters import chapters_to_ffmetadata
                chapters_metadata_path = chapters_to_ffmetadata(chapters)
                if chapters_metadata_path:
                    self.log(f"Adding {len(chapters)} chapters to output", 'INFO')

            def _build_base_cmd():
                """Build the common part of the ffmpeg command."""
                c = ['ffmpeg', '-y']
                if effective_hw and is_gpu and backend and transcode_mode in ['video', 'both'] and video_enc_name not in (None, 'copy'):
                    c.extend(backend['hwaccel'])
                c.extend(['-i', input_path])
                # Add external embed subtitle inputs
                for es in embed_subs:
                    c.extend(['-i', es['path']])
                # Add edited subtitle inputs
                for _si, _path in _edited_sub_inputs:
                    c.extend(['-i', _path])
                # Add extracted closed caption SRT as input
                if cc_srt_path:
                    c.extend(['-i', cc_srt_path])
                # Add chapter metadata file as input
                if chapters_metadata_path:
                    c.extend(['-i', chapters_metadata_path])
                return c

            def _edited_sub_input_idx(stream_index):
                """Return the ffmpeg input index for an edited subtitle, or None."""
                for i, (si, _path) in enumerate(_edited_sub_inputs):
                    if si == stream_index:
                        # Input 0 = main file, 1..N = embed_subs, N+1.. = edited subs
                        return 1 + len(embed_subs) + i
                return None

            def _add_video_args(c, pass_num=None):
                """Add video encoding arguments. pass_num: None=single, 1=first, 2=second."""
                if transcode_mode in ['video', 'both']:

                    if video_enc_name != 'copy':
                        # Video filters MUST come before -c:v for proper filter
                        # chain initialization with hwaccel_output_format surfaces
                        if has_burn_in:
                            sub_path = burn_in_subs[0]['path']
                            ext = Path(sub_path).suffix.lower()
                            # Escape special chars for ffmpeg filter syntax
                            escaped = sub_path.replace('\\', '\\\\\\\\').replace(':', '\\:').replace("'", "\\'")
                            if ext in ('.ass', '.ssa'):
                                c.extend(['-vf', f"ass='{escaped}'"])
                            else:
                                c.extend(['-vf', f"subtitles='{escaped}'"])
                        elif needs_pix_fmt_convert and backend:
                            # GPU hwaccel: convert pixel format on-device
                            c.extend(['-vf', backend['scale_filter']])
                        elif codec_info['cpu_encoder'] == 'prores_ks':
                            # ProRes requires 4:2:2 or 4:4:4
                            prores_profile = settings.get('preset', '') or 'hq'
                            if prores_profile in ('4444', '4444xq'):
                                pix = 'yuva444p10le'
                            else:
                                pix = 'yuv422p10le'
                            c.extend(['-vf', f'format={pix}'])

                    c.extend(['-c:v', video_enc_name])

                    # A53 CC passthrough flags
                    if cc_passthrough_flags:
                        c.extend(cc_passthrough_flags)

                    if video_enc_name != 'copy':
                        preset = settings.get('preset', '')
                        if preset:
                            if codec_info['cpu_encoder'] == 'libvpx-vp9' and encoder == 'cpu':
                                c.extend(['-cpu-used', preset])
                            elif codec_info['cpu_encoder'] == 'prores_ks' and encoder == 'cpu':
                                c.extend(['-profile:v', preset])
                            elif is_gpu and backend and backend.get('preset_flag'):
                                c.extend([backend['preset_flag'], preset])
                            elif not is_gpu:
                                c.extend(['-preset', preset])

                        if mode == 'crf':
                            crf_val = str(settings.get('crf', codec_info['crf_default']))
                            if is_gpu and backend:
                                cq_flag = backend.get('cq_flag')
                                if cq_flag:
                                    c.extend([cq_flag, crf_val])
                            elif codec_info['crf_flag']:
                                c.extend([codec_info['crf_flag'], crf_val])
                                if codec_info['cpu_encoder'] == 'libvpx-vp9':
                                    c.extend(['-b:v', '0'])
                        else:
                            bitrate = settings.get('bitrate', DEFAULT_BITRATE)
                            c.extend(['-b:v', bitrate])
                            if encoder == 'cpu' and codec_info['cpu_encoder'] not in ('libsvtav1', 'libvpx-vp9', 'prores_ks', 'mpeg4'):
                                c.extend(['-minrate', bitrate, '-maxrate', bitrate, '-bufsize', bitrate])
                            if use_gpu_multipass and backend:
                                c.extend(backend['multipass_args'])

                        if pass_num is not None:
                            c.extend(['-pass', str(pass_num)])
                            c.extend(['-passlogfile', passlog])

                elif transcode_mode == 'audio':
                    c.extend(['-c:v', 'copy'])

            def _add_audio_args(c):
                """Add audio encoding arguments."""
                EXPERIMENTAL_CODECS = {'opus', 'vorbis'}
                LOSSLESS_CODECS = {'flac', 'alac', 'pcm_s16le', 'pcm_s24le', 'wavpack', 'tta'}
                audio_codec = settings.get('audio_codec', 'aac')
                audio_bitrate = settings.get('audio_bitrate', '128k')
                if audio_codec == 'copy':
                    c.extend(['-c:a', 'copy'])
                else:
                    c.extend(['-c:a', audio_codec])
                    if audio_codec in EXPERIMENTAL_CODECS:
                        c.extend(['-strict', '-2'])
                    if audio_codec not in LOSSLESS_CODECS:
                        c.extend(['-b:a', audio_bitrate])

            def _add_subtitle_args(c):
                """Add subtitle stream arguments (internal + external embed).

                Output order: forced external subs first, then internal/configured
                subs, then remaining external subs.  This ensures forced tracks
                appear as the first subtitle stream(s) in the output.
                """
                container = settings.get('container', '.mkv')
                # Helper to compute the ffmpeg input index for the extracted CC SRT
                def _cc_input_idx():
                    return 1 + len(embed_subs) + len(_edited_sub_inputs)

                # AVI does not support embedded subtitle streams
                if container == '.avi':
                    c.extend(['-map', '0:v:0?', '-map', '0:a?'])
                    self.log("Subtitles skipped: AVI container does not support embedded subtitles", 'WARNING')
                    if cc_srt_path:
                        self.log("Closed captions also skipped: AVI does not support subtitles", 'WARNING')
                    return

                # MPEG-TS only supports DVB subtitles — drop text-based subs
                if container == '.ts':
                    c.extend(['-map', '0:v:0?', '-map', '0:a?'])
                    # Map through any DVB subtitle streams from the source
                    try:
                        int_streams = get_subtitle_streams(input_path)
                        for si, ist in enumerate(int_streams):
                            if ist.get('codec_name') == 'dvb_subtitle':
                                c.extend(['-map', f'0:s:{si}', f'-c:s:{si}', 'copy'])
                    except Exception:
                        pass
                    self.log("Text subtitles skipped: MPEG-TS container only supports DVB subtitles", 'WARNING')
                    if cc_srt_path:
                        self.log("Closed captions also skipped: MPEG-TS output does not support SRT", 'WARNING')
                    return

                sub_settings = settings.get('subtitle_settings', {})
                strip_internal = settings.get('strip_internal_subs', False)

                if not sub_settings and not embed_subs and not strip_internal and not edited_subs:
                    # Simple case: no per-file config, no external subs, no edits, keep internals
                    c.extend(['-map', '0:v:0?', '-map', '0:a?', '-map', '0:s?'])
                    # Handle subtitle codec compatibility between containers
                    BITMAP_CODECS = {'hdmv_pgs_subtitle', 'dvd_subtitle', 'dvb_subtitle'}
                    try:
                        int_streams = get_subtitle_streams(input_path)
                    except Exception:
                        int_streams = []

                    out_sub_idx = len(int_streams)

                    if container in ('.mp4', '.mov'):
                        # MP4/MOV only support mov_text — convert text subs, drop bitmap subs
                        if int_streams:
                            for si, ist in enumerate(int_streams):
                                if ist['codec_name'] in BITMAP_CODECS:
                                    c.extend([f'-c:s:{si}', 'copy'])
                                    self.log(f"Subtitle stream {ist['index']} ({ist['codec_name']}): "
                                             f"bitmap format, may not be supported in {container}", 'WARNING')
                                else:
                                    c.extend([f'-c:s:{si}', 'mov_text'])
                        else:
                            c.extend(['-c:s', 'mov_text'])
                    else:
                        # MKV/other containers: copy most subs, but convert mov_text to srt
                        # (mov_text is MP4-only and not supported in MKV)
                        if int_streams and any(s['codec_name'] == 'mov_text' for s in int_streams):
                            for si, ist in enumerate(int_streams):
                                if ist['codec_name'] == 'mov_text':
                                    c.extend([f'-c:s:{si}', 'srt'])
                                else:
                                    c.extend([f'-c:s:{si}', 'copy'])
                        else:
                            c.extend(['-c:s', 'copy'])

                    # Map extracted closed captions as an additional subtitle track
                    if cc_srt_path:
                        cc_idx = _cc_input_idx()
                        c.extend(['-map', f'{cc_idx}:s:0'])
                        if container in ('.mp4', '.mov'):
                            c.extend([f'-c:s:{out_sub_idx}', 'mov_text'])
                        else:
                            c.extend([f'-c:s:{out_sub_idx}', 'srt'])
                        c.extend([f'-metadata:s:s:{out_sub_idx}', 'language=eng'])
                        c.extend([f'-metadata:s:s:{out_sub_idx}', 'title=Closed Captions (CC)'])
                        self.log("Mapping extracted closed captions as subtitle track", 'INFO')
                    return

                # We need explicit mapping when we have external subs, per-file config,
                # or are stripping internal tracks
                c.extend(['-map', '0:v:0?', '-map', '0:a?'])
                out_sub_idx = 0
                container = settings.get('container', '.mkv')

                # Bitmap subtitle codecs that cannot be converted to mov_text
                _BITMAP_SUB_CODECS = {'hdmv_pgs_subtitle', 'dvd_subtitle', 'dvb_subtitle'}

                def _internal_sub_codec(ist):
                    """Return the codec to use when copying an internal subtitle stream."""
                    if container in ('.mp4', '.mov') and ist.get('codec_name') not in _BITMAP_SUB_CODECS:
                        return 'mov_text'
                    return 'copy'

                def _map_embed_sub(i, es):
                    """Map a single external embed subtitle input.
                    i is the index in embed_subs (input_idx = 1 + i)."""
                    nonlocal out_sub_idx
                    input_idx = 1 + i
                    c.extend(['-map', f'{input_idx}:s:0'])
                    if container in ('.mp4', '.mov'):
                        codec = 'mov_text'
                    elif container == '.ts':
                        codec = 'dvb_subtitle'
                    else:
                        codec = es.get('format', 'srt')
                    c.extend([f'-c:s:{out_sub_idx}', codec])
                    # Language metadata
                    lang = es.get('language', 'und')
                    if lang and lang != 'und':
                        c.extend([f'-metadata:s:s:{out_sub_idx}', f'language={lang}'])
                    # Disposition flags (default / forced / hearing_impaired)
                    disp_parts = []
                    if es.get('default'):
                        disp_parts.append('default')
                    if es.get('sdh'):
                        disp_parts.append('hearing_impaired')
                    if es.get('forced'):
                        disp_parts.append('forced')
                    if disp_parts:
                        c.extend([f'-disposition:s:{out_sub_idx}', '+'.join(disp_parts)])
                    # Track title — makes flags visible in MediaInfo and players
                    title_parts = []
                    if lang and lang != 'und':
                        lang_name = lang
                        for lc, ln in SUBTITLE_LANGUAGES:
                            if lc == lang:
                                lang_name = ln
                                break
                        title_parts.append(lang_name)
                    if es.get('sdh'):
                        title_parts.append('SDH')
                    if es.get('forced'):
                        title_parts.append('Forced')
                    if title_parts:
                        c.extend([f'-metadata:s:s:{out_sub_idx}', f'title={" - ".join(title_parts)}'])
                    out_sub_idx += 1

                # ── Phase 1: Map forced external subs first ──
                for i, es in enumerate(embed_subs):
                    if es.get('forced'):
                        _map_embed_sub(i, es)

                # ── Phase 2: Map internal / per-file-configured subs ──
                if strip_internal:
                    self.log("Stripping internal subtitle tracks (replaced by external subs)", 'INFO')
                elif not sub_settings:
                    # Map internal subtitle streams, auto-replacing any that
                    # conflict with external subs of the same language + type.
                    try:
                        internal_streams = get_subtitle_streams(input_path)
                    except Exception:
                        internal_streams = []

                    if embed_subs and internal_streams:
                        replaced = []
                        for ist in internal_streams:
                            ist_lang = ist.get('language', 'und')
                            ist_forced = ist.get('forced', False)
                            # Check if any external sub replaces this internal one
                            conflict = False
                            for es in embed_subs:
                                es_lang = es.get('language', 'und')
                                es_forced = es.get('forced', False)
                                # Match: same language (or either is 'und') and same type
                                lang_match = (ist_lang == es_lang
                                              or ist_lang == 'und'
                                              or es_lang == 'und')
                                type_match = (ist_forced == es_forced)
                                if lang_match and type_match:
                                    conflict = True
                                    break
                            if conflict:
                                replaced.append(ist)
                            else:
                                c.extend(['-map', f"0:{ist['index']}"])
                                c.extend([f'-c:s:{out_sub_idx}', _internal_sub_codec(ist)])
                                out_sub_idx += 1
                        if replaced:
                            labels = []
                            for r in replaced:
                                rl = r.get('language', 'und')
                                rt = ' (forced)' if r.get('forced') else ''
                                labels.append(f"{rl}{rt}")
                            self.log(f"Auto-replaced {len(replaced)} internal subtitle(s) "
                                     f"matching external subs: {', '.join(labels)}", 'INFO')
                    elif internal_streams:
                        # No external subs — keep all internal streams
                        if not edited_subs:
                            c.extend(['-map', '0:s?'])
                            for ist in internal_streams:
                                c.extend([f'-c:s:{out_sub_idx}', _internal_sub_codec(ist)])
                                out_sub_idx += 1
                        else:
                            # Some streams edited — map individually
                            for ist in internal_streams:
                                ed_input = _edited_sub_input_idx(ist['index'])
                                if ed_input is not None:
                                    c.extend(['-map', f'{ed_input}:s:0'])
                                    c.extend([f'-c:s:{out_sub_idx}', 'srt'])
                                    self.log(f"Using edited subtitle for stream #{ist['index']}", 'INFO')
                                else:
                                    c.extend(['-map', f"0:{ist['index']}"])
                                    c.extend([f'-c:s:{out_sub_idx}', _internal_sub_codec(ist)])
                                out_sub_idx += 1
                else:
                    for stream_index, ss in sub_settings.items():
                        if not ss.get('keep', True):
                            continue
                        fmt = ss.get('format', 'copy')
                        if fmt == 'drop':
                            continue
                        if fmt == 'extract only':
                            fmt = 'copy'
                        # Check if this stream has been edited
                        ed_input = _edited_sub_input_idx(stream_index)
                        if ed_input is not None:
                            c.extend(['-map', f'{ed_input}:s:0'])
                            c.extend([f'-c:s:{out_sub_idx}', 'srt'])
                            self.log(f"Using edited subtitle for stream #{stream_index}", 'INFO')
                        else:
                            c.extend(['-map', f"0:{stream_index}"])
                            c.extend([f'-c:s:{out_sub_idx}', fmt])
                        out_sub_idx += 1

                # ── Phase 3: Map remaining (non-forced) external subs ──
                for i, es in enumerate(embed_subs):
                    if not es.get('forced'):
                        _map_embed_sub(i, es)

                # ── Phase 4: Map extracted closed captions ──
                if cc_srt_path:
                    cc_idx = _cc_input_idx()
                    c.extend(['-map', f'{cc_idx}:s:0'])
                    if container in ('.mp4', '.mov'):
                        c.extend([f'-c:s:{out_sub_idx}', 'mov_text'])
                    else:
                        c.extend([f'-c:s:{out_sub_idx}', 'srt'])
                    c.extend([f'-metadata:s:s:{out_sub_idx}', 'language=eng'])
                    c.extend([f'-metadata:s:s:{out_sub_idx}', 'title=Closed Captions (CC)'])
                    out_sub_idx += 1
                    self.log("Mapping extracted closed captions as subtitle track", 'INFO')

            def _add_metadata_args(c):
                """Add metadata cleanup and track metadata flags."""
                # Add chapters or strip chapters (mutually exclusive)
                if chapters_metadata_path:
                    ch_idx = 1 + len(embed_subs) + len(_edited_sub_inputs) + (1 if cc_srt_path else 0)
                    c.extend(['-map_chapters', str(ch_idx)])
                elif settings.get('strip_chapters', False):
                    c.extend(['-map_chapters', '-1'])
                    self.log("Stripping chapters from output", 'INFO')

                # Strip global tags/metadata
                if settings.get('strip_metadata_tags', False):
                    c.extend(['-map_metadata', '-1'])
                    self.log("Stripping global tags/metadata from output", 'INFO')

                # Set track metadata (language, clear names/title)
                if settings.get('set_track_metadata', False):
                    video_lang = settings.get('meta_video_lang', 'und')
                    audio_lang = settings.get('meta_audio_lang', 'eng')
                    sub_lang   = settings.get('meta_sub_lang', 'eng')
                    # Container title
                    c.extend(['-metadata', 'title='])
                    # Video track
                    c.extend(['-metadata:s:v:0', f'language={video_lang}',
                              '-metadata:s:v:0', 'title='])
                    # Audio track
                    c.extend(['-metadata:s:a:0', f'language={audio_lang}',
                              '-metadata:s:a:0', 'title='])
                    # First subtitle track
                    c.extend(['-metadata:s:s:0', f'language={sub_lang}',
                              '-metadata:s:s:0', f'title={sub_lang.upper() if len(sub_lang) <= 3 else sub_lang}'])
                    self.log(f"Setting track metadata: video={video_lang}, audio={audio_lang}, sub={sub_lang}", 'INFO')

                # Edition tag — write to container title
                # Placed after set_track_metadata so it overrides the title= clear
                # Works independently — doesn't require set_track_metadata to be on
                edition = settings.get('edition_tag', '')
                if edition:
                    c.extend(['-metadata', f'title={edition}'])
                    self.log(f"Setting edition tag: {edition}", 'INFO')

            # ── Log what we're about to do ──
            self.log(f"Video codec: {video_enc_name}", 'INFO')
            self.log(f"Mode: {mode}" + (" (two-pass)" if use_two_pass else " (GPU multipass)" if use_gpu_multipass else ""), 'INFO')
            if hw_decode and is_gpu and backend:
                self.log(f"Hardware decode: {backend['hwaccel'][1]} enabled", 'INFO')

            import tempfile
            passlog = None

            if use_two_pass:
                # Create a temp passlog file prefix
                tmp = tempfile.NamedTemporaryFile(delete=False, suffix='_passlog')
                passlog = tmp.name
                tmp.close()

                # ── Pass 1 ──
                self.log("Two-pass encoding: starting pass 1 of 2...", 'INFO')
                cmd1 = _build_base_cmd()
                _add_video_args(cmd1, pass_num=1)
                cmd1.extend(['-an'])          # no audio in pass 1
                cmd1.extend(['-f', 'null', '/dev/null'])
                self.log(f"Pass 1 command: {' '.join(cmd1)}", 'INFO')

                if not self._run_process(cmd1, input_path, pass_label="Pass 1/2"):
                    return False
                if self.is_stopped:
                    return False

                # ── Pass 2 ──
                self.log("Two-pass encoding: starting pass 2 of 2...", 'INFO')
                cmd2 = _build_base_cmd()
                _add_video_args(cmd2, pass_num=2)
                _add_audio_args(cmd2)
                _add_subtitle_args(cmd2)
                _add_metadata_args(cmd2)
                cmd2.append(output_path)
                self.log(f"Pass 2 command: {' '.join(cmd2)}", 'INFO')

                success = self._run_process(cmd2, input_path, pass_label="Pass 2/2")

                # Clean up passlog files
                for ext in ('', '.log', '.log.mbtree', '-0.log', '-0.log.mbtree'):
                    try:
                        os.remove(passlog + ext)
                    except FileNotFoundError:
                        pass

                if success:
                    self.log(f"Two-pass complete: {os.path.basename(output_path)}", "SUCCESS")
                return success

            else:
                # ── Single pass ──
                cmd = _build_base_cmd()
                _add_video_args(cmd)
                _add_audio_args(cmd)
                _add_subtitle_args(cmd)
                _add_metadata_args(cmd)
                cmd.append(output_path)
                self.log(f"Command: {' '.join(cmd)}", 'INFO')
                return self._run_process(cmd, input_path)
            
            # Audio encoding
            audio_codec = settings.get('audio_codec', 'aac')
            audio_bitrate = settings.get('audio_bitrate', '128k')
            
            # Codecs that require -strict -2 (experimental in ffmpeg)
            EXPERIMENTAL_CODECS = {'opus', 'vorbis'}
            # Lossless codecs — don't set a bitrate target
            LOSSLESS_CODECS = {'flac', 'alac', 'pcm_s16le', 'pcm_s24le', 'wavpack', 'tta'}

            if audio_codec == 'copy':
                cmd.extend(['-c:a', 'copy'])
                self.log("Audio stream: copying (no re-encode)", 'INFO')
            else:
                cmd.extend(['-c:a', audio_codec])
                if audio_codec in EXPERIMENTAL_CODECS:
                    cmd.extend(['-strict', '-2'])
                    self.log(f"Audio codec {audio_codec}: enabling experimental mode", 'INFO')
                if audio_codec not in LOSSLESS_CODECS:
                    cmd.extend(['-b:a', audio_bitrate])
                    self.log(f"Audio encoding: {audio_codec} at {audio_bitrate}", 'INFO')
                else:
                    self.log(f"Audio encoding: {audio_codec} (lossless)", 'INFO')
            
            # Subtitle handling
            sub_settings = settings.get('subtitle_settings', {})
            if not sub_settings:
                # No per-file settings — copy all subtitle streams
                cmd.extend(['-c:s', 'copy'])
                self.log("Subtitle streams: copying all (no re-encode)", 'INFO')
            else:
                # Explicit per-stream mapping — must also map video and audio
                # otherwise ffmpeg disables default stream selection
                cmd.extend(['-map', '0:v:0?', '-map', '0:a?'])
        except Exception as e:
            self.log(f"Conversion error: {str(e)}", "ERROR")
            return False
        finally:
            self.current_process = None
            # Clean up extracted CC temp file
            if cc_srt_path:
                try:
                    os.remove(cc_srt_path)
                except OSError:
                    pass
            # Clean up chapter metadata temp file
            if chapters_metadata_path:
                try:
                    os.remove(chapters_metadata_path)
                except OSError:
                    pass

    def _run_process(self, cmd, input_path, pass_label=None):
        """Run an ffmpeg subprocess, parse progress, handle pause/stop. Returns True on success."""
        import time
        try:
            self.current_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1
            )

            duration = get_video_duration(input_path)
            label = f"[{pass_label}] " if pass_label else ""

            # ── ETA calculation ──
            # Blended approach: FPS-based ETA (responsive, good early) mixed
            # with wall-clock average ETA (stable, good over time).  The blend
            # shifts from 100% FPS-based → 100% wall-clock over BLEND_SECS.
            process_start = time.monotonic()
            paused_total = 0.0
            pause_began = None
            BLEND_SECS = 30             # seconds to fully transition to wall-clock ETA

            # Get total frame count for FPS-based ETA
            total_frames = None
            try:
                probe_cmd = [
                    'ffprobe', '-v', 'error', '-select_streams', 'v:0',
                    '-show_entries', 'stream=nb_frames,r_frame_rate',
                    '-of', 'default=noprint_wrappers=1', input_path
                ]
                probe_result = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=10)
                for pline in probe_result.stdout.strip().split('\n'):
                    if pline.startswith('nb_frames=') and pline.split('=')[1].strip().isdigit():
                        total_frames = int(pline.split('=')[1].strip())
                    elif pline.startswith('r_frame_rate='):
                        num, den = pline.split('=')[1].strip().split('/')
                        source_fps = float(num) / float(den)
                # If nb_frames not available, estimate from duration × fps
                if total_frames is None and duration and source_fps:
                    total_frames = int(duration * source_fps)
            except Exception:
                pass

            for line in self.current_process.stdout:
                if self.is_stopped:
                    self.current_process.terminate()
                    try:
                        self.current_process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        self.current_process.kill()
                    self.log("Conversion stopped by user", "WARNING")
                    return False

                while self.is_paused:
                    if pause_began is None:
                        pause_began = time.monotonic()
                    if self.is_stopped:
                        return False
                    time.sleep(0.5)
                if pause_began is not None:
                    paused_total += time.monotonic() - pause_began
                    pause_began = None

                line = line.strip()

                if 'time=' in line:
                    match = re.search(r'time=(\d+):(\d+):(\d+)', line)
                    if match and duration:
                        h, m, s = map(int, match.groups())
                        current_time = h * 3600 + m * 60 + s
                        progress = (current_time / duration) * 100

                        # Parse frame, fps, and speed from the same line
                        frame_match = re.search(r'frame=\s*(\d+)', line)
                        fps_match   = re.search(r'fps=\s*([\d.]+)', line)
                        speed_match = re.search(r'speed=\s*([\d.]+)x', line)
                        cur_frame = int(frame_match.group(1))   if frame_match else None
                        fps       = float(fps_match.group(1))   if fps_match   else None
                        speed     = float(speed_match.group(1)) if speed_match else None

                        # Calculate blended ETA
                        eta = None
                        wall_elapsed = time.monotonic() - process_start - paused_total

                        # FPS-based ETA: remaining_frames / current_fps
                        fps_eta = None
                        if fps and fps > 0 and total_frames and cur_frame is not None:
                            remaining_frames = total_frames - cur_frame
                            if remaining_frames > 0:
                                fps_eta = remaining_frames / fps

                        # Wall-clock ETA: remaining_video / avg_speed
                        wall_eta = None
                        if current_time > 0 and wall_elapsed > 0:
                            avg_speed = current_time / wall_elapsed
                            remaining_video_secs = duration - current_time
                            wall_eta = remaining_video_secs / avg_speed

                        # Blend: start with FPS-based, shift to wall-clock over time
                        if fps_eta is not None and wall_eta is not None:
                            w = min(wall_elapsed / BLEND_SECS, 1.0)
                            eta = w * wall_eta + (1 - w) * fps_eta
                        elif fps_eta is not None:
                            eta = fps_eta
                        elif wall_eta is not None:
                            eta = wall_eta

                        if self.progress_callback:
                            self.progress_callback(progress, f"{label}{line}",
                                                   fps=fps, eta=eta, pass_label=pass_label)

                if any(kw in line.lower() for kw in ['error', 'warning', 'failed']):
                    self.log(f"{label}{line}", "ERROR" if 'error' in line.lower() else "WARNING")

            return_code = self.current_process.wait()
            if return_code == 0:
                if not pass_label:
                    self.log(f"Conversion complete: {os.path.basename(cmd[-1])}", "SUCCESS")
                return True
            else:
                self.log(f"{label}Conversion failed with code {return_code}", "ERROR")
                return False

        except Exception as e:
            self.log(f"Process error: {e}", "ERROR")
            return False
        finally:
            self.current_process = None
    
    def pause(self):
        """Pause conversion"""
        self.is_paused = True
        self.log("Conversion paused", "WARNING")
    
    def resume(self):
        """Resume conversion"""
        self.is_paused = False
        self.log("Conversion resumed", "INFO")
    
    def stop(self):
        """Stop conversion — terminate then force-kill if needed."""
        self.is_stopped = True
        if self.current_process:
            self.current_process.terminate()
            try:
                self.current_process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.current_process.kill()
                self.current_process.wait(timeout=5)

# ============================================================================
# Main Application Class
# ============================================================================

