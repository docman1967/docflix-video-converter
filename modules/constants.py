"""
Docflix Media Suite — Constants and Configuration

All shared constants, codec maps, extension sets, and GPU backend definitions.
"""

# ── App identity ──
APP_NAME = "Docflix Media Suite"
APP_VERSION = "3.0.0"

# ── Defaults ──
DEFAULT_BITRATE = "2M"
DEFAULT_CRF = 23
DEFAULT_PRESET = "ultrafast"
DEFAULT_GPU_PRESET = "p4"
MAX_CHARS_PER_LINE = 42

# ── Preferences path ──
PREFS_DIR = "~/.local/share/docflix"
PREFS_FILENAME = "preferences.json"

# ── Edition presets for container title tagging ──
EDITION_PRESETS = [
    '',                     # (no edition)
    'Theatrical',
    "Director's Cut",
    'Extended',
    'Extended Director\'s Cut',
    'Unrated',
    'Special Edition',
    'IMAX',
    'Criterion',
    'Remastered',
    'Anniversary Edition',
    'Ultimate Edition',
    'Custom...',
]

# ── Bitmap subtitle codecs (cannot convert to text without OCR) ──
BITMAP_SUB_CODECS = frozenset({
    'hdmv_pgs_subtitle', 'dvd_subtitle', 'dvb_subtitle',
    'dvb_teletext', 'xsub',
})

# ── GPU Backend Definitions ──
# Each backend defines its hwaccel flags, per-codec encoders, presets, quality
# flags, and how to detect whether the hardware is present.
GPU_BACKENDS = {
    'nvenc': {
        'label':        'NVIDIA (NVENC)',
        'short':        'NVENC',
        'hwaccel':      ['-hwaccel', 'cuda', '-hwaccel_output_format', 'cuda'],
        'scale_filter': 'scale_cuda=format=yuv420p',
        'detect_encoders': ['hevc_nvenc'],
        'detect_cmd':   ['nvidia-smi', '--query-gpu=name',
                         '--format=csv,noheader'],
        'encoders': {
            'H.265 / HEVC': 'hevc_nvenc',
            'H.264 / AVC':  'h264_nvenc',
            'AV1':          'av1_nvenc',
            'VP9':          None,
            'MPEG-4':       None,
            'ProRes (QuickTime)': None,
            'Copy (no re-encode)': 'copy',
        },
        'presets':        ('p1', 'p2', 'p3', 'p4', 'p5', 'p6', 'p7'),
        'preset_default': 'p4',
        'preset_flag':    '-preset',
        'cq_flag':        '-cq',
        'multipass_encoders': {'hevc_nvenc', 'h264_nvenc', 'av1_nvenc'},
        'multipass_args':     ['-multipass', 'fullres'],
    },
    'qsv': {
        'label':        'Intel (QSV)',
        'short':        'QSV',
        'hwaccel':      ['-hwaccel', 'qsv',
                         '-hwaccel_output_format', 'qsv'],
        'scale_filter': 'scale_qsv=format=nv12',
        'detect_encoders': ['hevc_qsv'],
        'detect_cmd':   None,
        'encoders': {
            'H.265 / HEVC': 'hevc_qsv',
            'H.264 / AVC':  'h264_qsv',
            'AV1':          'av1_qsv',
            'VP9':          'vp9_qsv',
            'MPEG-4':       None,
            'ProRes (QuickTime)': None,
            'Copy (no re-encode)': 'copy',
        },
        'presets':        ('veryfast', 'faster', 'fast', 'medium',
                           'slow', 'slower', 'veryslow'),
        'preset_default': 'medium',
        'preset_flag':    '-preset',
        'cq_flag':        '-global_quality',
        'multipass_encoders': set(),
        'multipass_args':     [],
    },
    'vaapi': {
        'label':        'AMD / VAAPI',
        'short':        'VAAPI',
        'hwaccel':      ['-hwaccel', 'vaapi',
                         '-hwaccel_output_format', 'vaapi',
                         '-vaapi_device', '/dev/dri/renderD128'],
        'scale_filter': 'scale_vaapi=format=nv12',
        'detect_encoders': ['hevc_vaapi'],
        'detect_cmd':   None,
        'encoders': {
            'H.265 / HEVC': 'hevc_vaapi',
            'H.264 / AVC':  'h264_vaapi',
            'AV1':          'av1_vaapi',
            'VP9':          'vp9_vaapi',
            'MPEG-4':       None,
            'ProRes (QuickTime)': None,
            'Copy (no re-encode)': 'copy',
        },
        'presets':        (),
        'preset_default': None,
        'preset_flag':    None,
        'cq_flag':        '-qp',
        'multipass_encoders': set(),
        'multipass_args':     [],
    },
}

# ── Video codec definitions ──
VIDEO_CODEC_MAP = {
    'H.265 / HEVC': {
        'cpu_encoder': 'libx265',
        'cpu_presets': ('ultrafast', 'superfast', 'veryfast', 'faster',
                        'fast', 'medium', 'slow', 'slower', 'veryslow'),
        'cpu_preset_default': 'ultrafast',
        'crf_min': 0, 'crf_max': 51, 'crf_default': 23,
        'crf_flag': '-crf',
        'short_name': 'H265',
    },
    'H.264 / AVC': {
        'cpu_encoder': 'libx264',
        'cpu_presets': ('ultrafast', 'superfast', 'veryfast', 'faster',
                        'fast', 'medium', 'slow', 'slower', 'veryslow'),
        'cpu_preset_default': 'ultrafast',
        'crf_min': 0, 'crf_max': 51, 'crf_default': 23,
        'crf_flag': '-crf',
        'short_name': 'H264',
    },
    'AV1': {
        'cpu_encoder': 'libsvtav1',
        'cpu_presets': ('0', '1', '2', '3', '4', '5', '6', '7',
                        '8', '9', '10', '11', '12', '13'),
        'cpu_preset_default': '8',
        'crf_min': 0, 'crf_max': 63, 'crf_default': 35,
        'crf_flag': '-crf',
        'short_name': 'AV1',
    },
    'VP9': {
        'cpu_encoder': 'libvpx-vp9',
        'cpu_presets': ('0', '1', '2', '3', '4', '5'),
        'cpu_preset_default': '2',
        'crf_min': 0, 'crf_max': 63, 'crf_default': 33,
        'crf_flag': '-crf',
        'short_name': 'VP9',
    },
    'MPEG-4': {
        'cpu_encoder': 'mpeg4',
        'cpu_presets': (),
        'cpu_preset_default': None,
        'crf_min': 1, 'crf_max': 31, 'crf_default': 4,
        'crf_flag': '-q:v',
        'short_name': 'MPEG4',
    },
    'ProRes (QuickTime)': {
        'cpu_encoder': 'prores_ks',
        'cpu_presets': ('proxy', 'lt', 'standard', 'hq', '4444',
                        '4444xq'),
        'cpu_preset_default': 'hq',
        'crf_min': 0, 'crf_max': 64, 'crf_default': 10,
        'crf_flag': '-q:v',
        'short_name': 'ProRes',
    },
    'Copy (no re-encode)': {
        'cpu_encoder': 'copy',
        'cpu_presets': (),
        'cpu_preset_default': None,
        'crf_min': 0, 'crf_max': 51, 'crf_default': 23,
        'crf_flag': None,
        'short_name': 'copy',
    },
}

# ── Supported file extensions ──
VIDEO_EXTENSIONS = {
    '.mkv', '.mp4', '.avi', '.mov', '.wmv', '.flv',
    '.webm', '.ts', '.m2ts', '.mts',
}

SUBTITLE_EXTENSIONS = {
    '.srt', '.ass', '.ssa', '.vtt', '.sub', '.idx', '.sup',
}

# ── Subtitle codec mapping ──
SUBTITLE_EXT_TO_CODEC = {
    '.srt': 'srt',
    '.ass': 'ass',
    '.ssa': 'ass',
    '.vtt': 'webvtt',
    '.sub': 'dvd_subtitle',
    '.idx': 'dvd_subtitle',
    '.sup': 'hdmv_pgs_subtitle',
}

# ── Common subtitle languages ──
SUBTITLE_LANGUAGES = [
    ('und', 'Undetermined'),
    ('eng', 'English'),
    ('spa', 'Spanish'),
    ('fra', 'French'),
    ('deu', 'German'),
    ('ita', 'Italian'),
    ('por', 'Portuguese'),
    ('rus', 'Russian'),
    ('jpn', 'Japanese'),
    ('kor', 'Korean'),
    ('zho', 'Chinese'),
    ('ara', 'Arabic'),
    ('hin', 'Hindi'),
    ('nld', 'Dutch'),
    ('pol', 'Polish'),
    ('swe', 'Swedish'),
    ('tur', 'Turkish'),
    ('vie', 'Vietnamese'),
]

# Lookup dict: 3-letter code → human-readable name
LANG_CODE_TO_NAME = {code: name for code, name in SUBTITLE_LANGUAGES}


# ── GPU helper functions ──

def get_gpu_encoder(codec_name, backend_id):
    """Return the GPU encoder name for a codec + backend, or None."""
    backend = GPU_BACKENDS.get(backend_id)
    if not backend:
        return None
    return backend['encoders'].get(codec_name)


def get_gpu_presets(backend_id):
    """Return (presets_tuple, default) for a GPU backend."""
    backend = GPU_BACKENDS.get(backend_id)
    if not backend:
        return (), None
    return backend['presets'], backend['preset_default']


def get_cq_flag(backend_id):
    """Return the constant-quality flag for a GPU backend."""
    backend = GPU_BACKENDS.get(backend_id)
    if not backend:
        return None
    return backend.get('cq_flag')
