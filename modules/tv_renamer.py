"""
Docflix Media Suite — Media Renamer

Batch rename TV show and movie files using episode data
from TVDB or TMDB. Can run as standalone tool or as part
of the main converter app.
"""

import json
import os
import re
import subprocess
import threading
import tkinter as tk
from tkinter import ttk, messagebox

from .constants import (
    APP_NAME, APP_VERSION, VIDEO_EXTENSIONS, SUBTITLE_EXTENSIONS,
)
from .utils import create_tooltip, scaled_geometry, scaled_minsize, ask_open_files, ask_directory

try:
    from tkinterdnd2 import DND_FILES
    HAS_DND = True
except ImportError:
    HAS_DND = False


# ── Video file probing for template variables ──

_VCODEC_MAP = {
    'hevc': 'x265', 'h265': 'x265', 'h264': 'x264', 'avc': 'x264',
    'av1': 'AV1', 'vp9': 'VP9', 'vp8': 'VP8', 'mpeg4': 'MPEG4',
    'mpeg2video': 'MPEG2', 'mpeg1video': 'MPEG1', 'prores': 'ProRes',
    'theora': 'Theora', 'wmv3': 'WMV', 'vc1': 'VC1',
}

_ACODEC_MAP = {
    'aac': 'AAC', 'ac3': 'AC3', 'eac3': 'EAC3', 'dts': 'DTS',
    'truehd': 'TrueHD', 'flac': 'FLAC', 'opus': 'Opus',
    'vorbis': 'Vorbis', 'mp3': 'MP3', 'mp2': 'MP2',
    'pcm_s16le': 'PCM', 'pcm_s24le': 'PCM', 'pcm_s32le': 'PCM',
    'wmav2': 'WMA', 'alac': 'ALAC',
}

# Keywords to detect source from filename
_SOURCE_PATTERNS = [
    ('REMUX', 'REMUX'), ('BluRay', 'BluRay'), ('BDRip', 'BDRip'),
    ('WEB-DL', 'WEB-DL'), ('WEBRip', 'WEBRip'), ('WEB', 'WEB'),
    ('HDTV', 'HDTV'), ('DVDRip', 'DVDRip'), ('DVD', 'DVD'),
    ('BRRip', 'BRRip'), ('CAM', 'CAM'), ('TS', 'TS'),
]


def _probe_media_tags(filepath):
    """Probe a video file for resolution, codecs, and HDR info.
    Returns a dict with keys: resolution, vcodec, acodec, hdr, source."""
    tags = {'resolution': '', 'vcodec': '', 'acodec': '', 'hdr': '', 'source': ''}
    try:
        cmd = [
            'ffprobe', '-v', 'quiet', '-print_format', 'json',
            '-show_streams', '-show_entries',
            'stream=codec_type,codec_name,width,height,color_transfer,color_primaries',
            filepath,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if result.returncode != 0:
            return tags
        import json as _j
        data = _j.loads(result.stdout)
        for s in data.get('streams', []):
            if s.get('codec_type') == 'video' and not tags['vcodec']:
                # Resolution
                w = s.get('width', 0)
                h = s.get('height', 0)
                if h >= 2000 or w >= 3800:
                    tags['resolution'] = '2160p'
                elif h >= 1000 or w >= 1900:
                    tags['resolution'] = '1080p'
                elif h >= 700 or w >= 1200:
                    tags['resolution'] = '720p'
                elif h > 0:
                    tags['resolution'] = '480p'
                # Video codec
                codec = s.get('codec_name', '').lower()
                tags['vcodec'] = _VCODEC_MAP.get(codec, codec.upper() if codec else '')
                # HDR detection
                ct = s.get('color_transfer', '')
                cp = s.get('color_primaries', '')
                if 'smpte2084' in ct or 'arib-std-b67' in ct:
                    if 'bt2020' in cp:
                        tags['hdr'] = 'HDR10'
                    else:
                        tags['hdr'] = 'HDR'
                elif ct and 'bt709' not in ct and 'unknown' not in ct:
                    tags['hdr'] = 'HDR'
                else:
                    tags['hdr'] = 'SDR'
            elif s.get('codec_type') == 'audio' and not tags['acodec']:
                codec = s.get('codec_name', '').lower()
                tags['acodec'] = _ACODEC_MAP.get(codec, codec.upper() if codec else '')
                # Check for Atmos (TrueHD with object audio)
                if codec == 'truehd':
                    profile = s.get('profile', '')
                    if 'atmos' in profile.lower():
                        tags['acodec'] = 'Atmos'
                # Check for DTS-HD
                elif codec == 'dts':
                    profile = s.get('profile', '')
                    if 'hd ma' in profile.lower() or 'hd-ma' in profile.lower():
                        tags['acodec'] = 'DTS-HD'
                    elif 'hd hra' in profile.lower():
                        tags['acodec'] = 'DTS-HD HRA'
    except Exception:
        pass

    # Source detection from filename
    fname_upper = os.path.basename(filepath).upper()
    for pattern, label in _SOURCE_PATTERNS:
        if pattern.upper() in fname_upper:
            tags['source'] = label
            break

    return tags


def open_tv_renamer(app):
        import urllib.request
        import urllib.parse
        import json as _json

        TVDB_BASE = 'https://api4.thetvdb.com/v4'
        TMDB_BASE = 'https://api.themoviedb.org/3'
        TMDB_IMG_BASE = 'https://image.tmdb.org/t/p'

        win = tk.Toplevel(app.root)
        win.withdraw()
        win.title("📺 Docflix Media Renamer")
        geom_str = scaled_geometry(win, 960, 650)
        win.geometry(geom_str)
        win.minsize(*scaled_minsize(win, 800, 550))
        win.resizable(True, True)
        win.update_idletasks()
        try:
            import re as _re
            gm = _re.match(r'(\d+)x(\d+)', geom_str)
            dw = int(gm.group(1)) if gm else win.winfo_reqwidth()
            dh = int(gm.group(2)) if gm else win.winfo_reqheight()
            pw = app.root.winfo_width()
            ph = app.root.winfo_height()
            px = app.root.winfo_x()
            py = app.root.winfo_y()
            x = px + (pw - dw) // 2
            y = py + (ph - dh) // 2
            win.geometry(f'{dw}x{dh}+{max(0, x)}+{max(0, y)}')
        except Exception:
            pass
        win.deiconify()

        # ── State ──
        _tvdb_token = [None]
        _all_shows = {}      # {show_name: {(season, ep): ep_data, ...}}
        _file_items = []     # list of {'path': ..., 'season': N, 'episode': N, 'ext': ...}
        _rename_history = []  # list of [(old_path, new_path), ...] for undo

        # Load API keys and preferences
        _saved_key = getattr(app, '_tvdb_api_key', '')
        _saved_tmdb_key = getattr(app, '_tmdb_api_key', '')
        _saved_provider = getattr(app, '_tv_rename_provider', 'TVDB')
        _saved_template = getattr(app, '_tv_rename_template',
                                  '{show} S{season}E{episode} {title}')
        _saved_movie_template = getattr(app, '_movie_rename_template',
                                        '{show} ({year})')

        # ── TVDB API helpers ──
        def _tvdb_request(method, path, body=None, token=None):
            """Make a TVDB v4 API request."""
            url = TVDB_BASE + path
            headers = {'Content-Type': 'application/json'}
            if token:
                headers['Authorization'] = f'Bearer {token}'
            data = _json.dumps(body).encode() if body else None
            req = urllib.request.Request(url, data=data, headers=headers,
                                         method=method)
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    return _json.loads(resp.read().decode())
            except urllib.error.HTTPError as e:
                try:
                    err_body = e.read().decode()
                    return _json.loads(err_body)
                except Exception:
                    return {'status': 'error', 'message': str(e)}
            except Exception as e:
                return {'status': 'error', 'message': str(e)}

        def _tvdb_login():
            """Authenticate with TVDB and store token."""
            key = api_key_var.get().strip()
            if not key:
                _log("Enter your TVDB API key", 'WARNING')
                return False
            _log(f"Logging in to TVDB (key: {key[:8]}...)")
            result = _tvdb_request('POST', '/login', {'apikey': key})
            _log(f"Login response: {result.get('status') if result else 'None'}")
            if result and result.get('status') == 'success':
                _tvdb_token[0] = result['data']['token']
                _log("TVDB login successful")
                app._tvdb_api_key = key
                app.save_preferences()
                return True
            else:
                msg = result.get('message', 'Login failed') if result else 'No response'
                _log(f"TVDB login failed: {msg}", 'ERROR')
                return False

        def _tvdb_search(query):
            """Search TVDB for TV series and movies."""
            if not _tvdb_token[0]:
                _log("No token — logging in...")
                if not _tvdb_login():
                    _log("Login failed — cannot search", 'ERROR')
                    return []
            encoded_q = urllib.parse.quote(query)
            # Search without type filter to get both series and movies
            url = f'/search?query={encoded_q}'
            result = _tvdb_request('GET', url, token=_tvdb_token[0])
            if result:
                if result.get('status') == 'success':
                    data = result.get('data', [])
                    # Filter to series and movies only
                    data = [r for r in data
                            if r.get('type') in ('series', 'movie', None)]
                    _log(f"TVDB search returned {len(data)} results")
                    return data
                else:
                    _log(f"Search error: {result.get('message', 'unknown')}", 'ERROR')
            else:
                _log("Search returned no response", 'ERROR')
            return []

        def _tvdb_get_episodes(series_id):
            """Get all episodes for a series."""
            if not _tvdb_token[0]:
                _log("No token — cannot fetch episodes", 'ERROR')
                return []
            all_eps = []
            page = 0
            while True:
                url = f'/series/{series_id}/episodes/default?page={page}'
                _log(f"Fetching: {TVDB_BASE}{url}")
                result = _tvdb_request('GET', url, token=_tvdb_token[0])
                if not result:
                    _log("No response from episodes endpoint", 'ERROR')
                    break
                if result.get('status') != 'success':
                    _log(f"Episodes error: {result.get('message', 'unknown')}", 'ERROR')
                    break
                eps = result.get('data', {}).get('episodes', [])
                if not eps:
                    _log(f"No episodes on page {page}")
                    break
                all_eps.extend(eps)
                # Check if there are more pages
                links = result.get('links', {})
                if links.get('next'):
                    page += 1
                else:
                    break
            return all_eps

        # ── TMDB API helpers ──
        def _tmdb_request(path):
            """Make a TMDB v3 API GET request."""
            key = tmdb_key_var.get().strip()
            if not key:
                return None
            sep = '&' if '?' in path else '?'
            url = f'{TMDB_BASE}{path}{sep}api_key={key}'
            req = urllib.request.Request(url, headers={
                'Accept': 'application/json',
                'User-Agent': 'DocflixVideoConverter/1.9'})
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    return _json.loads(resp.read().decode())
            except urllib.error.HTTPError as e:
                try:
                    err_body = e.read().decode()
                    return _json.loads(err_body)
                except Exception:
                    return {'status_code': e.code, 'status_message': str(e)}
            except Exception as e:
                return {'status_code': 0, 'status_message': str(e)}

        def _tmdb_search(query):
            """Search TMDB for TV series and movies. Returns results
            normalized to the same dict format as TVDB for the
            disambiguation dialog."""
            encoded_q = urllib.parse.quote(query)
            normalized = []
            seen_ids = set()

            # Search TV series
            tv_result = _tmdb_request(f'/search/tv?query={encoded_q}')
            if tv_result and 'results' in tv_result:
                for r in tv_result['results']:
                    rid = ('tv', r.get('id', ''))
                    if rid in seen_ids:
                        continue
                    seen_ids.add(rid)
                    year = ''
                    fad = r.get('first_air_date', '')
                    if fad and len(fad) >= 4:
                        year = fad[:4]
                    countries = r.get('origin_country', [])
                    country = countries[0] if countries else ''
                    poster = r.get('poster_path', '')
                    normalized.append({
                        'name': r.get('name', ''),
                        'year': year,
                        'country': country,
                        'network': 'TV Series',
                        'overview': r.get('overview', ''),
                        'thumbnail': (f'{TMDB_IMG_BASE}/w92{poster}'
                                      if poster else ''),
                        'image_url': (f'{TMDB_IMG_BASE}/w300{poster}'
                                      if poster else ''),
                        'id': r.get('id', ''),
                        'tvdb_id': '',
                        '_provider': 'tmdb',
                        '_media_type': 'tv',
                    })

            # Search movies
            movie_result = _tmdb_request(f'/search/movie?query={encoded_q}')
            if movie_result and 'results' in movie_result:
                for r in movie_result['results']:
                    rid = ('movie', r.get('id', ''))
                    if rid in seen_ids:
                        continue
                    seen_ids.add(rid)
                    year = ''
                    rd = r.get('release_date', '')
                    if rd and len(rd) >= 4:
                        year = rd[:4]
                    poster = r.get('poster_path', '')
                    normalized.append({
                        'name': r.get('title', ''),
                        'year': year,
                        'country': '',
                        'network': 'Movie',
                        'overview': r.get('overview', ''),
                        'thumbnail': (f'{TMDB_IMG_BASE}/w92{poster}'
                                      if poster else ''),
                        'image_url': (f'{TMDB_IMG_BASE}/w300{poster}'
                                      if poster else ''),
                        'id': r.get('id', ''),
                        'tvdb_id': '',
                        '_provider': 'tmdb',
                        '_media_type': 'movie',
                    })

            if not normalized:
                _log("TMDB search: no results", 'WARNING')
            else:
                _log(f"TMDB search returned {len(normalized)} results")
            return normalized

        def _tmdb_get_episodes(series_id):
            """Get all episodes for a TMDB series. Fetches show details first
            to get the number of seasons, then fetches each season.
            Returns episodes normalized to TVDB episode dict format."""
            # Get show details for season count
            details = _tmdb_request(f'/tv/{series_id}')
            if not details or 'number_of_seasons' not in details:
                msg = (details.get('status_message', 'unknown')
                       if details else 'No response')
                _log(f"TMDB show details error: {msg}", 'ERROR')
                return []
            num_seasons = details['number_of_seasons']
            all_eps = []
            for sn in range(1, num_seasons + 1):
                season_data = _tmdb_request(f'/tv/{series_id}/season/{sn}')
                if not season_data or 'episodes' not in season_data:
                    _log(f"  Season {sn}: no data")
                    continue
                for ep in season_data['episodes']:
                    all_eps.append({
                        'seasonNumber': ep.get('season_number', sn),
                        'number': ep.get('episode_number'),
                        'name': ep.get('name', ''),
                        'aired': ep.get('air_date', ''),
                        'year': (ep.get('air_date', '')[:4]
                                 if ep.get('air_date') else ''),
                    })
                _log(f"  Season {sn}: {len(season_data['episodes'])} episodes")
            return all_eps

        # ── Provider-agnostic search & episode fetch ──
        def _provider_search(query):
            """Search the active provider for a TV series."""
            prov = provider_var.get()
            if prov == 'TMDB':
                return _tmdb_search(query)
            else:
                return _tvdb_search(query)

        def _provider_get_episodes(series_id, provider=None):
            """Fetch episodes from the active (or specified) provider."""
            prov = provider or provider_var.get()
            if prov == 'TMDB':
                return _tmdb_get_episodes(series_id)
            else:
                return _tvdb_get_episodes(series_id)

        def _provider_get_series_id(result):
            """Extract the series ID from a search result dict."""
            prov = result.get('_provider', provider_var.get().lower())
            if prov == 'tmdb':
                return result.get('id', '')
            else:
                sid = result.get('tvdb_id', result.get('id', ''))
                if isinstance(sid, str) and sid.startswith('series-'):
                    sid = sid[7:]
                return sid

        # ── Episode number parser ──
        def _parse_episode_info(filename):
            """Extract season and episode numbers from a filename.
            Returns (season, episode) for single-episode files,
            (season, [ep1, ep2, ...]) for multi-episode files,
            or sets item['air_date'] = 'YYYY-MM-DD' for date-based episodes."""
            name = os.path.basename(filename)
            # S01E01E02, S01E01-E03, S01E01E02E03 (multi-episode)
            m = re.search(r'[Ss](\d{1,2})\s*[Ee](\d{1,3})(?:\s*-?\s*[Ee](\d{1,3}))+', name)
            if m:
                season = int(m.group(1))
                # Extract all episode numbers from the full match
                eps = [int(x) for x in re.findall(r'[Ee](\d{1,3})', m.group(0))]
                if len(eps) > 1:
                    # Check for range pattern like S01E01-E03 (fill in gaps)
                    if len(eps) == 2 and eps[1] > eps[0] + 1:
                        eps = list(range(eps[0], eps[1] + 1))
                    return season, eps
                return season, eps[0]
            # S01E01, s1e1 (single episode)
            m = re.search(r'[Ss](\d{1,2})\s*[Ee](\d{1,3})', name)
            if m:
                return int(m.group(1)), int(m.group(2))
            # 1x01, 01x01
            m = re.search(r'(\d{1,2})[xX](\d{1,3})', name)
            if m:
                return int(m.group(1)), int(m.group(2))
            # Season 1 Episode 1
            m = re.search(r'[Ss]eason\s*(\d+).*?[Ee]pisode\s*(\d+)', name)
            if m:
                return int(m.group(1)), int(m.group(2))
            # Date-based: 2026.04.22, 2026-04-22, 2026 04 22
            m = re.search(r'((?:19|20)\d{2})[.\-\s](0[1-9]|1[0-2])[.\-\s](0[1-9]|[12]\d|3[01])', name)
            if m:
                # Return a special marker — date stored in item dict later
                return 'date', f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
            # E01 or Ep01 (season assumed from folder or default 1)
            m = re.search(r'[Ee](?:p|pisode)?\s*(\d{1,3})', name)
            if m:
                return None, int(m.group(1))
            return None, None

        def _sanitize_filename(name):
            """Remove characters not allowed in filenames."""
            # Replace : with - (common in episode titles), strip others
            name = name.replace(':', ' ').replace('/', '-').replace('\\', '-')
            name = re.sub(r'[<>"|?*]', '', name)
            # Collapse multiple spaces
            name = re.sub(r'\s+', ' ', name).strip()
            # Remove trailing dots/spaces (Windows compatibility)
            name = name.rstrip('. ')
            return name

        def _sanitize_path(name):
            """Sanitize a filename that may contain path separators (/).
            Each path component is sanitized individually, preserving the
            folder structure defined in the template."""
            if '/' not in name:
                return _sanitize_filename(name)
            parts = name.split('/')
            sanitized = []
            for part in parts:
                part = part.replace(':', ' ').replace('\\', '-')
                part = re.sub(r'[<>"|?*]', '', part)
                part = re.sub(r'\s+', ' ', part).strip()
                part = part.rstrip('. ')
                if part:  # skip empty components
                    sanitized.append(part)
            return os.path.join(*sanitized) if sanitized else ''

        def _match_file_to_show(item):
            """Match a file to one of the loaded shows by filename."""
            if not _all_shows:
                return None
            fname = os.path.splitext(os.path.basename(item['path']))[0]
            cleaned = _normalize_for_match(_clean_show_name(fname))
            if not cleaned:
                return None

            best_match = None
            best_score = 0.0
            for show_name in _all_shows:
                show_norm = _normalize_for_match(show_name)
                # Exact match
                if show_norm == cleaned:
                    return show_name
                # Show name contained in filename
                if show_norm in cleaned:
                    score = len(show_norm) / max(len(cleaned), 1)
                    if score > best_score:
                        best_score = score
                        best_match = show_name
                # Filename contained in show name
                elif cleaned in show_norm:
                    score = len(cleaned) / max(len(show_norm), 1) * 0.8
                    if score > best_score:
                        best_score = score
                        best_match = show_name

            # Word-level overlap fallback
            if best_score < 0.4:
                cleaned_words = set(cleaned.split())
                for show_name in _all_shows:
                    show_words = set(_normalize_for_match(show_name).split())
                    if show_words and cleaned_words:
                        overlap = len(cleaned_words & show_words) / len(show_words)
                        if overlap > best_score and overlap >= 0.5:
                            best_score = overlap
                            best_match = show_name

            return best_match if best_score >= 0.3 else None

        # ISO 639-1 → ISO 639-2/B (3-letter) mapping for subtitle language codes
        _LANG_2TO3 = {
            'en': 'eng', 'es': 'spa', 'fr': 'fre', 'de': 'ger', 'it': 'ita',
            'pt': 'por', 'ja': 'jpn', 'ko': 'kor', 'zh': 'chi', 'ru': 'rus',
            'ar': 'ara', 'hi': 'hin', 'nl': 'dut', 'sv': 'swe', 'da': 'dan',
            'no': 'nor', 'fi': 'fin', 'pl': 'pol', 'cs': 'cze', 'el': 'gre',
            'he': 'heb', 'tr': 'tur', 'th': 'tha', 'vi': 'vie', 'uk': 'ukr',
            'ro': 'rum', 'hu': 'hun', 'bg': 'bul', 'hr': 'hrv', 'sk': 'slo',
            'sl': 'slv', 'ms': 'may', 'id': 'ind', 'tl': 'fil', 'af': 'afr',
            'ca': 'cat', 'cy': 'wel', 'et': 'est', 'ga': 'gle', 'lv': 'lav',
            'lt': 'lit', 'mk': 'mac', 'mt': 'mlt', 'sq': 'alb', 'sr': 'srp',
            'sw': 'swa', 'ta': 'tam', 'te': 'tel', 'ur': 'urd', 'bn': 'ben',
        }

        def _detect_language_from_content(filepath):
            """Detect language from subtitle file content using langdetect.
            Returns a 3-letter ISO 639-2 code, or None on failure."""
            try:
                from langdetect import detect
                ext = os.path.splitext(filepath)[1].lower()
                if ext not in ('.srt', '.ass', '.ssa', '.vtt', '.sub'):
                    return None
                # Read file, try common encodings
                text = None
                for enc in ('utf-8', 'latin-1', 'cp1252'):
                    try:
                        with open(filepath, 'r', encoding=enc) as f:
                            text = f.read(8192)  # first 8KB is enough
                        break
                    except (UnicodeDecodeError, UnicodeError):
                        continue
                if not text:
                    return None
                # Strip SRT formatting (timestamps, tags, numbers)
                cleaned = re.sub(r'\d+\s*\n\d{2}:\d{2}:\d{2}[.,]\d+ --> '
                                 r'\d{2}:\d{2}:\d{2}[.,]\d+\s*\n', '', text)
                cleaned = re.sub(r'<[^>]+>', '', cleaned)
                cleaned = re.sub(r'\{[^}]+\}', '', cleaned)
                cleaned = re.sub(r'♪[^\n]*', '', cleaned)
                # Strip ASS header/style sections
                cleaned = re.sub(r'\[Script Info\].*?\[Events\]',
                                 '', cleaned, flags=re.DOTALL)
                cleaned = re.sub(r'Dialogue:\s*\d+,\d[^,]*,\d[^,]*,[^,]*,'
                                 r'[^,]*,\d+,\d+,\d+,[^,]*,', '', cleaned)
                # Collapse whitespace
                cleaned = re.sub(r'\s+', ' ', cleaned).strip()
                if len(cleaned) < 20:
                    return None
                lang_2 = detect(cleaned)
                return _LANG_2TO3.get(lang_2, lang_2)
            except Exception:
                return None

        def _detect_sub_tags(filename):
            """Detect language, forced, and SDH tags from a subtitle filename.
            Returns a string like '.eng.forced' or '.eng.sdh' to insert
            before the extension. Language is detected from filename first,
            then verified/detected from file content via langdetect."""
            stem = os.path.splitext(os.path.basename(filename))[0].lower()
            parts = re.split(r'[\.\s_\-]+', stem)
            tags = []
            # Walk trailing dot-separated tokens for known tags
            # Common patterns: .eng.forced.srt, .en.sdh.srt, .forced.srt
            _LANG_CODES = {
                'en', 'eng', 'es', 'spa', 'fr', 'fra', 'fre', 'de', 'deu',
                'ger', 'it', 'ita', 'pt', 'por', 'ja', 'jpn', 'ko', 'kor',
                'zh', 'zho', 'chi', 'ru', 'rus', 'ar', 'ara', 'hi', 'hin',
                'nl', 'nld', 'dut', 'sv', 'swe', 'da', 'dan', 'no', 'nor',
                'fi', 'fin', 'pl', 'pol', 'cs', 'ces', 'cze', 'el', 'ell',
                'gre', 'he', 'heb', 'tr', 'tur', 'th', 'tha', 'vi', 'vie',
                'uk', 'ukr', 'ro', 'ron', 'rum', 'hu', 'hun', 'bg', 'bul',
                'hr', 'hrv', 'sk', 'slk', 'slo', 'sl', 'slv', 'ms', 'msa',
                'may', 'id', 'ind', 'tl', 'fil', 'und',
            }
            _TAG_WORDS = {'forced', 'sdh', 'cc', 'hi'}
            filename_lang = None
            found_tags = []
            # Scan from the end of the parts list
            for part in reversed(parts):
                p = part.strip().lower()
                if p in _TAG_WORDS:
                    found_tags.insert(0, p)
                elif p in _LANG_CODES and filename_lang is None:
                    filename_lang = p
                else:
                    break  # stop at first non-tag token

            # Normalize 2-letter filename codes to 3-letter
            if filename_lang and len(filename_lang) == 2:
                filename_lang = _LANG_2TO3.get(filename_lang, filename_lang)

            # Detect language from file content
            content_lang = _detect_language_from_content(filename)

            # Use content detection, fall back to filename, then default 'eng'
            if content_lang:
                lang = content_lang
                if filename_lang and filename_lang != content_lang:
                    _log(f"  Language: filename says '{filename_lang}', "
                         f"content detected '{content_lang}' — "
                         f"using '{content_lang}'")
            else:
                lang = filename_lang if filename_lang else 'eng'

            tags.append(lang)
            seen = set()
            for t in found_tags:
                if t not in seen:
                    tags.append(t)
                    seen.add(t)
            return '.' + '.'.join(tags)

        def _build_new_name(item, template, show_name, movie_template=None):
            """Build a new filename (or relative path) from template and
            episode data.  When the template contains '/' separators the
            result is a relative path whose parent directories will be
            created at rename time."""
            if not show_name:
                return None
            show_data = _all_shows.get(show_name, {})

            # Build provider ID variables for template
            # Both {tvdb} and {tmdb} resolve to the active provider's ID
            # so either template variable works regardless of provider
            sid = show_data.get('_series_id', '') if isinstance(show_data, dict) else ''
            prov = show_data.get('_provider', '') if isinstance(show_data, dict) else ''
            provider_id = f'{prov}-{sid}' if prov and sid else ''
            tvdb_id = provider_id
            tmdb_id = provider_id

            # Media tags — from probed video or matched video for subtitles
            mt = item.get('media_tags', {})
            if not mt and item.get('ext') in SUBTITLE_EXTENSIONS:
                # Try to get tags from a matching video file
                for other in _file_items:
                    if (other.get('ext') in VIDEO_EXTENSIONS
                            and other.get('matched_show') == show_name
                            and other.get('media_tags')):
                        mt = other['media_tags']
                        break
            media_vars = {
                'resolution': mt.get('resolution', ''),
                'vcodec': mt.get('vcodec', ''),
                'acodec': mt.get('acodec', ''),
                'hdr': mt.get('hdr', ''),
                'source': mt.get('source', ''),
            }

            # ── Movie mode — uses movie template ──
            if isinstance(show_data, dict) and show_data.get('_is_movie'):
                year = show_data.get('_year', '')
                m_tmpl = movie_template or '{show} ({year})'
                # Choose sanitizer: path-aware when template contains '/'
                sanitize = (_sanitize_path if '/' in m_tmpl
                            else _sanitize_filename)
                try:
                    name = m_tmpl.format(
                        show=show_name,
                        year=year,
                        tvdb=tvdb_id,
                        tmdb=tmdb_id,
                        # Provide TV vars as empty so shared templates don't crash
                        season='', episode='', title='',
                        **media_vars,
                    )
                except (KeyError, IndexError):
                    name = f"{show_name} ({year})" if year else show_name
                ext = item['ext']
                sub_tags = ''
                if ext in SUBTITLE_EXTENSIONS:
                    sub_tags = _detect_sub_tags(item['path'])
                return sanitize(name) + sub_tags + ext

            # Choose sanitizer for TV template
            sanitize = (_sanitize_path if '/' in template
                        else _sanitize_filename)

            # ── Date-based episode mode ──
            air_date = item.get('air_date')
            if air_date:
                ep_data = show_data.get(('date', air_date))
                if ep_data:
                    title = ep_data.get('name', '')
                    s = ep_data.get('seasonNumber', 1)
                    e = ep_data.get('number', 0)
                    name = template.format(
                        show=show_name,
                        season=str(s).zfill(2),
                        episode=str(e).zfill(2),
                        title=title,
                        year=ep_data.get('year', air_date[:4]),
                        tvdb=tvdb_id,
                        tmdb=tmdb_id,
                        **media_vars,
                    )
                else:
                    # No episode data found — use date as title
                    name = f"{show_name} - {air_date}"
                ext = item['ext']
                sub_tags = ''
                if ext in SUBTITLE_EXTENSIONS:
                    sub_tags = _detect_sub_tags(item['path'])
                return sanitize(name) + sub_tags + ext

            # ── TV series mode — need season/episode ──
            s = item.get('season')
            e = item.get('episode')
            if s is None or e is None:
                return None

            # ── Multi-episode support ──
            if isinstance(e, list) and len(e) > 1:
                # Build combined episode tag: E01-E02 or E01E02E03
                first_ep, last_ep = e[0], e[-1]
                if e == list(range(first_ep, last_ep + 1)):
                    ep_tag = f"E{str(first_ep).zfill(2)}-E{str(last_ep).zfill(2)}"
                else:
                    ep_tag = ''.join(f"E{str(x).zfill(2)}" for x in e)
                # Collect titles from each episode
                titles = []
                year = ''
                for ep_num in e:
                    ep_data = show_data.get((s, ep_num))
                    if ep_data:
                        t = ep_data.get('name', '')
                        if t:
                            titles.append(t)
                        if not year:
                            year = ep_data.get('year', '')
                title = ' & '.join(titles) if titles else ''
                name = template.format(
                    show=show_name,
                    season=str(s).zfill(2),
                    episode=ep_tag,
                    title=title,
                    year=year,
                    tvdb=tvdb_id,
                    tmdb=tmdb_id,
                    **media_vars,
                )
            else:
                # Single episode
                ep_num = e[0] if isinstance(e, list) else e
                ep_data = show_data.get((s, ep_num))
                title = ep_data.get('name', '') if ep_data else ''
                name = template.format(
                    show=show_name,
                    season=str(s).zfill(2),
                    episode=str(ep_num).zfill(2),
                    title=title,
                    year=ep_data.get('year', '') if ep_data else '',
                    tvdb=tvdb_id,
                    tmdb=tmdb_id,
                    **media_vars,
                )
            ext = item['ext']
            # For subtitle files, preserve language/forced/SDH tags
            sub_tags = ''
            if ext in SUBTITLE_EXTENSIONS:
                sub_tags = _detect_sub_tags(item['path'])
            return sanitize(name) + sub_tags + ext

        # ── Logging ──
        def _log(msg, level='INFO'):
            log_text.configure(state='normal')
            log_text.insert('end', msg + '\n')
            log_text.see('end')
            log_text.configure(state='disabled')

        # ══════════════════════════════════════════════════════════════
        # UI Layout
        # ══════════════════════════════════════════════════════════════

        main_f = ttk.Frame(win, padding=8)
        main_f.pack(fill='both', expand=True)
        main_f.columnconfigure(1, weight=1)

        # ── API keys ──
        api_key_var = tk.StringVar(value='8903a14b-8b71-436e-a48a-d553884f2991')
        tmdb_key_var = tk.StringVar(value='9375eb1401938b7615afd69988611a74')
        provider_var = tk.StringVar(value=_saved_provider)

        def _on_provider_change(*_args):
            # Save preference
            app._tv_rename_provider = provider_var.get()
            app.save_preferences()
            # Clear loaded shows and re-search with the new provider
            _all_shows.clear()
            for item in _file_items:
                item.pop('matched_show', None)
            if _file_items:
                _auto_load_shows()
            else:
                _refresh_preview()

        def _file_items_refresh_matches():
            """Re-run matching and refresh preview after provider change."""
            for item in _file_items:
                item.pop('matched_show', None)
            _refresh_preview()

        provider_var.trace_add('write', _on_provider_change)

        # Save TMDB key on change
        def _save_tmdb_key(*_args):
            app._tmdb_api_key = tmdb_key_var.get().strip()
            app.save_preferences()

        # ── Row 0: Loaded Shows ──

        def _normalize_for_match(text):
            """Normalize a show name for comparison: lowercase, collapse
            '&' / 'and' / ':' differences, and squash extra whitespace."""
            t = text.lower()
            t = t.replace('&', ' and ')
            t = t.replace(':', ' ')
            t = re.sub(r'\s+', ' ', t).strip()
            return t

        def _clean_show_name(raw):
            """Strip episode info, quality tags, and release group from a show name."""
            # Replace dots and underscores with spaces, but preserve hyphens
            # that are part of the show name (e.g. 9-1-1, S.W.A.T., X-Men)
            name = re.sub(r'[._]', ' ', raw).strip()
            # Replace hyphens that act as word separators (surrounded by spaces
            # or at the boundary of a release group like "h264-GRACE") but keep
            # hyphens between non-space characters (e.g. "9-1-1", "X-Men")
            name = re.sub(r'(?<=\s)-|-(?=\s)', ' ', name)
            # Truncate at episode markers (including multi-episode S01E01E02)
            name = re.sub(r'\s*[Ss]\d{1,2}\s*[Ee]\d.*', '', name)
            name = re.sub(r'\s*\d{1,2}[xX]\d.*', '', name)
            # Truncate at date-based episode markers (2026 04 22)
            name = re.sub(r'\s*(?:19|20)\d{2}\s+(?:0[1-9]|1[0-2])\s+(?:0[1-9]|[12]\d|3[01]).*', '', name)
            # Truncate at quality/resolution tags
            name = re.sub(r'\s*(?:720|1080|2160|480)[pPiI].*', '', name)
            # Truncate at common release tags
            name = re.sub(r'\s*(?:WEB|HDTV|BluRay|BDRip|DVDRip|REMUX|PROPER).*',
                          '', name, flags=re.IGNORECASE)
            # Strip trailing year (e.g. "Rise Of The Conqueror 2026" or "Movie (2026)")
            name = re.sub(r'\s+\(?(?:19|20)\d{2}\)?\s*$', '', name)
            return name.strip()

        def _remove_show_for_selected():
            """Remove the loaded show and all its matched files from the queue."""
            sel = tree.selection()
            if not sel:
                return
            removed = set()
            for iid in sel:
                idx = tree.index(iid)
                if idx < len(_file_items):
                    show = _file_items[idx].get('matched_show')
                    if show and show not in removed:
                        removed.add(show)
            if removed:
                # Remove files whose matched_show is in the removed set
                before = len(_file_items)
                _file_items[:] = [f for f in _file_items
                                  if f.get('matched_show') not in removed]
                count = before - len(_file_items)
                # Now remove the show data
                for name in removed:
                    _all_shows.pop(name, None)
                    _log(f"Removed \"{name}\" — {count} file(s) removed")
                _refresh_preview()

        def _clear_all_shows():
            """Remove all loaded shows."""
            _all_shows.clear()
            _refresh_preview()
            _log("All shows cleared")

        def _ask_user_pick_show(query, candidates):
            """Show a dialog for the user to pick from multiple show matches.
            candidates: list of dicts from TVDB search results.
            Returns the chosen dict, or None if cancelled."""
            dlg = tk.Toplevel(win)
            dlg.title("Multiple Matches")
            dlg.geometry("700x500")
            dlg.minsize(500, 350)
            dlg.resizable(True, True)

            ttk.Label(dlg, text=f"Multiple shows found for \"{query}\":",
                      font=('Helvetica', 11, 'bold'),
                      padding=(10, 10, 10, 4)).pack(anchor='w')

            # ── Scrollable list area ──
            outer_f = ttk.Frame(dlg)
            outer_f.pack(fill='both', expand=True, padx=10, pady=4)

            canvas = tk.Canvas(outer_f, highlightthickness=0)
            scrollbar = ttk.Scrollbar(outer_f, orient='vertical',
                                       command=canvas.yview)
            scroll_frame = ttk.Frame(canvas)

            scroll_frame.bind('<Configure>',
                              lambda e: canvas.configure(
                                  scrollregion=canvas.bbox('all')))
            canvas_win = canvas.create_window((0, 0), window=scroll_frame,
                                               anchor='nw')
            canvas.configure(yscrollcommand=scrollbar.set)

            # Make scroll_frame fill canvas width on resize
            _resize_after_id = [None]
            def _on_canvas_resize(event):
                canvas.itemconfig(canvas_win, width=event.width)
                # Debounced redraw to fix thumbnail dropout at high DPI
                if _resize_after_id[0]:
                    canvas.after_cancel(_resize_after_id[0])
                _resize_after_id[0] = canvas.after(
                    100, lambda: canvas.configure(
                        scrollregion=canvas.bbox('all')))
            canvas.bind('<Configure>', _on_canvas_resize)

            canvas.pack(side='left', fill='both', expand=True)
            scrollbar.pack(side='right', fill='y')

            # Mousewheel scrolling
            def _on_mousewheel(event):
                canvas.yview_scroll(int(-1 * (event.delta / 120)), 'units')
            def _on_button4(event):
                canvas.yview_scroll(-3, 'units')
            def _on_button5(event):
                canvas.yview_scroll(3, 'units')
            canvas.bind_all('<MouseWheel>', _on_mousewheel)
            canvas.bind_all('<Button-4>', _on_button4)
            canvas.bind_all('<Button-5>', _on_button5)

            chosen = [None]
            selected_idx = [0]
            row_frames = []
            _thumb_refs = []  # prevent GC of PhotoImages

            def _select_row(idx):
                """Highlight the selected row."""
                selected_idx[0] = idx
                for i, rf in enumerate(row_frames):
                    if i == idx:
                        rf.configure(style='Selected.TFrame')
                        for child in rf.winfo_children():
                            try:
                                child.configure(style='Selected.TLabel')
                            except Exception:
                                pass
                    else:
                        rf.configure(style='TFrame')
                        for child in rf.winfo_children():
                            try:
                                child.configure(style='TLabel')
                            except Exception:
                                pass

            # Style for selected row
            style = ttk.Style()
            style.configure('Selected.TFrame', background='#3a6ea5')
            style.configure('Selected.TLabel', background='#3a6ea5',
                            foreground='white')

            def _ok():
                chosen[0] = candidates[selected_idx[0]]
                dlg.destroy()

            # ── Build show cards ──
            for i, r in enumerate(candidates):
                name = r.get('name', r.get('objectName', ''))
                year = r.get('year', '')
                country = r.get('country', '').upper()
                network = r.get('network', '')
                overview = r.get('overview', '')

                title = f"{name} ({year})" if year else name
                meta_parts = []
                if country:
                    meta_parts.append(country)
                if network:
                    meta_parts.append(network)
                meta_line = '  |  '.join(meta_parts)

                row_f = ttk.Frame(scroll_frame, padding=(8, 6),
                                  relief='flat')
                row_f.pack(fill='x', padx=2, pady=2)
                row_f.columnconfigure(1, weight=1)
                row_frames.append(row_f)

                # Click to select
                def _click(event, idx=i):
                    _select_row(idx)
                def _dblclick(event, idx=i):
                    _select_row(idx)
                    _ok()
                row_f.bind('<Button-1>', _click)
                row_f.bind('<Double-1>', _dblclick)

                # Thumbnail placeholder (load async later)
                thumb_label = ttk.Label(row_f, text='', width=10)
                thumb_label.grid(row=0, column=0, rowspan=3, sticky='n',
                                 padx=(0, 10), pady=2)
                thumb_label.bind('<Button-1>', _click)
                thumb_label.bind('<Double-1>', _dblclick)

                # Title
                title_lbl = ttk.Label(row_f, text=title,
                                      font=('Helvetica', 11, 'bold'))
                title_lbl.grid(row=0, column=1, sticky='w')
                title_lbl.bind('<Button-1>', _click)
                title_lbl.bind('<Double-1>', _dblclick)

                # Meta line (country | network)
                if meta_line:
                    meta_lbl = ttk.Label(row_f, text=meta_line,
                                         font=('Helvetica', 9),
                                         foreground='#888')
                    meta_lbl.grid(row=1, column=1, sticky='w')
                    meta_lbl.bind('<Button-1>', _click)
                    meta_lbl.bind('<Double-1>', _dblclick)

                # Overview (show synopsis)
                if overview:
                    ov_lbl = ttk.Label(row_f, text=overview,
                                       wraplength=500,
                                       font=('Helvetica', 9),
                                       justify='left')
                    ov_lbl.grid(row=2, column=1, sticky='w', pady=(2, 0))
                    ov_lbl.bind('<Button-1>', _click)
                    ov_lbl.bind('<Double-1>', _dblclick)

                # Separator between cards
                if i < len(candidates) - 1:
                    ttk.Separator(scroll_frame, orient='horizontal').pack(
                        fill='x', padx=8, pady=0)

            # Select first row
            if row_frames:
                _select_row(0)

            # ── Load thumbnails in background ──
            def _load_thumbs():
                import io
                for i, r in enumerate(candidates):
                    thumb_url = r.get('thumbnail', '')
                    if not thumb_url:
                        continue
                    try:
                        req = urllib.request.Request(thumb_url, headers={
                            'User-Agent': 'DocflixVideoConverter/1.8'})
                        resp = urllib.request.urlopen(req, timeout=5)
                        img_data = resp.read()
                        # Schedule PhotoImage creation on the main thread
                        dlg.after(0, _apply_thumb, i, img_data)
                    except Exception:
                        pass

            def _apply_thumb(idx, img_data):
                """Create PhotoImage and apply to widget (must run on main thread)."""
                try:
                    import io
                    from PIL import Image, ImageTk
                    img = Image.open(io.BytesIO(img_data))
                    img.thumbnail((60, 90), Image.LANCZOS)
                    photo = ImageTk.PhotoImage(img)
                    _thumb_refs.append(photo)
                    rf = row_frames[idx]
                    for child in rf.grid_slaves(row=0, column=0):
                        child.configure(image=photo, width=0)
                        child._photo = photo  # prevent GC on resize
                        # Re-bind click events after image loads
                        child.bind('<Button-1>',
                                   lambda e, i=idx: _select_row(i))
                        child.bind('<Double-1>',
                                   lambda e, i=idx: (_select_row(i), _ok()))
                        break
                except Exception:
                    pass

            thumb_thread = threading.Thread(target=_load_thumbs, daemon=True)
            thumb_thread.start()

            # ── Buttons ──
            btn_f = ttk.Frame(dlg, padding=(10, 6))
            btn_f.pack(fill='x')
            ttk.Button(btn_f, text="Load", command=_ok,
                       width=10).pack(side='left', padx=4)

            # Unbind mousewheel on close to prevent leaking into parent
            def _on_close():
                canvas.unbind_all('<MouseWheel>')
                canvas.unbind_all('<Button-4>')
                canvas.unbind_all('<Button-5>')
                dlg.destroy()
            dlg.protocol('WM_DELETE_WINDOW', _on_close)

            _center_on_parent(dlg, win)
            win.wait_window(dlg)
            return chosen[0]

        def _load_show_by_name(query):
            """Search the active provider for a show name and auto-load the
            best match. Prompts the user if multiple shows share the same name.
            Returns the loaded show name, or None on failure."""
            if not query:
                return None
            prov = provider_var.get()
            results = _provider_search(query)
            # Retry with And↔& swap if initial search found nothing
            if not results:
                alt = None
                if re.search(r'\bAnd\b', query, re.IGNORECASE):
                    alt = re.sub(r'\bAnd\b', '&', query, flags=re.IGNORECASE)
                elif '&' in query:
                    alt = query.replace('&', 'And')
                if alt:
                    _log(f"  Retrying search as \"{alt}\"...")
                    results = _provider_search(alt)
            if not results:
                _log(f"  No {prov} results for \"{query}\"", 'WARNING')
                return None

            # Check if there are multiple results with the same/similar name
            # First collect both exact matches AND close matches (name contains
            # query or vice versa), then decide whether to prompt the user.
            # This catches cases like "Ghosts" returning "Ghosts", "Ghosts (US)",
            # "Ghosts (2019)", "Ghosts (DE)" — all should be presented.
            # Normalize both sides so "And" matches "&" and colons are ignored.
            query_norm = _normalize_for_match(query)
            close_matches = []
            seen_ids = set()
            for r in results[:15]:  # limit to top 15
                rname = r.get('name', r.get('objectName', ''))
                rname_norm = _normalize_for_match(rname)
                rid = r.get('tvdb_id', r.get('id', ''))
                if rid in seen_ids:
                    continue
                if (rname_norm == query_norm
                        or query_norm in rname_norm
                        or rname_norm in query_norm):
                    close_matches.append(r)
                    seen_ids.add(rid)

            if len(close_matches) > 1:
                # Multiple shows match — ask the user to pick
                _log(f"  Found {len(close_matches)} matches for \"{query}\" — asking...")
                win.update_idletasks()
                best = _ask_user_pick_show(query, close_matches)
                if not best:
                    _log(f"  Skipped \"{query}\"")
                    return None
            elif len(close_matches) == 1:
                best = close_matches[0]
            else:
                best = results[0]

            show_name = best.get('name', best.get('objectName', ''))
            if show_name in _all_shows:
                _log(f"  \"{show_name}\" already loaded")
                return show_name

            series_id = _provider_get_series_id(best)
            media_type = best.get('_media_type', best.get('type', 'series'))

            prov = best.get('_provider', provider_var.get().lower())

            if media_type == 'movie':
                # Movies have no episodes — store a single entry
                year = best.get('year', '')
                _all_shows[show_name] = {
                    '_is_movie': True,
                    '_year': year,
                    '_name': show_name,
                    '_series_id': str(series_id),
                    '_provider': prov,
                }
                _log(f"  Loaded movie \"{show_name}\" ({year})")
                return show_name

            eps = _provider_get_episodes(series_id)
            if not eps:
                _log(f"  No episodes found for \"{show_name}\"", 'WARNING')
                return None

            show_eps = {}
            seasons = set()
            for ep in eps:
                s = ep.get('seasonNumber')
                e = ep.get('number')
                if s is not None and e is not None:
                    show_eps[(s, e)] = ep
                    seasons.add(s)
                # Also index by air date for date-based episodes
                aired = ep.get('aired') or ep.get('air_date') or ''
                if aired and len(aired) >= 10:
                    show_eps[('date', aired[:10])] = ep

            show_eps['_series_id'] = str(series_id)
            show_eps['_provider'] = prov
            _all_shows[show_name] = show_eps
            real_seasons = {s for s in seasons if s > 0} or seasons
            _log(f"  Loaded \"{show_name}\" — {len(eps)} eps, "
                 f"{len(real_seasons)} seasons")
            return show_name

        def _auto_load_shows():
            """Detect unique show names from file list and load them all
            in a background thread with progress indication."""
            if not _file_items:
                _log("No files loaded — add files first", 'WARNING')
                return

            # Extract unique show names from video filenames only —
            # subtitle files contain language/forced/sdh tags that pollute
            # the show name and cause failed API searches
            show_names = set()
            for item in _file_items:
                if item.get('ext') in SUBTITLE_EXTENSIONS:
                    continue
                fname = os.path.splitext(os.path.basename(item['path']))[0]
                cleaned = _clean_show_name(fname).strip()
                if cleaned:
                    show_names.add(cleaned)

            if not show_names:
                _log("Could not detect any show names from filenames", 'WARNING')
                return

            # Filter out names that are already matched by a loaded show
            to_search = set()
            for name in show_names:
                already = False
                name_lower = name.lower()
                for loaded in _all_shows:
                    if loaded.lower() in name_lower or name_lower in loaded.lower():
                        already = True
                        break
                if not already:
                    to_search.add(name)

            if not to_search:
                _log(f"All {len(show_names)} detected shows are already loaded")
                _refresh_preview()
                return

            total = len(to_search)
            _log(f"Auto-loading {total} show(s) from {provider_var.get()}...")

            # ── Progress bar ──
            prog_f = ttk.Frame(main_f)
            prog_f.grid(row=6, column=0, columnspan=3, sticky='ew',
                        padx=4, pady=(2, 0))
            prog_lbl = ttk.Label(prog_f, text="Loading shows...",
                                 font=('Helvetica', 9))
            prog_lbl.pack(side='left', padx=(0, 8))
            prog_bar = ttk.Progressbar(prog_f, maximum=total, mode='determinate')
            prog_bar.pack(side='left', fill='x', expand=True)

            _api_cancel = [False]

            def _cancel_load():
                _api_cancel[0] = True
                cancel_btn_api.configure(state='disabled')

            cancel_btn_api = ttk.Button(prog_f, text="Cancel",
                                        command=_cancel_load, width=7)
            cancel_btn_api.pack(side='right', padx=(4, 0))

            def _worker():
                loaded_count = 0
                for i, name in enumerate(sorted(to_search)):
                    if _api_cancel[0]:
                        win.after(0, lambda: _log("Auto-load cancelled", 'WARNING'))
                        break
                    win.after(0, lambda n=name: _log(f"Searching: \"{n}\"..."))
                    win.after(0, lambda n=name, idx=i:
                              (prog_lbl.configure(
                                  text=f"Loading {idx + 1}/{total}: {n}"),
                               prog_bar.configure(value=idx)))
                    try:
                        # _load_show_by_name may open a picker dialog,
                        # which needs to run on the main thread
                        import queue
                        result_q = queue.Queue()

                        def _do_load(q=name):
                            try:
                                r = _load_show_by_name(q)
                                result_q.put(('ok', r))
                            except Exception as ex:
                                result_q.put(('error', ex))

                        win.after(0, _do_load)
                        # Wait for result (check periodically)
                        result = None
                        while True:
                            try:
                                status, val = result_q.get(timeout=0.1)
                                if status == 'ok':
                                    result = val
                                else:
                                    raise val
                                break
                            except queue.Empty:
                                if _api_cancel[0]:
                                    break
                                continue
                        if _api_cancel[0]:
                            break
                        if result:
                            loaded_count += 1
                    except Exception as e:
                        win.after(0, lambda n=name, err=e:
                                  _log(f"  Error loading \"{n}\": {err}", 'ERROR'))

                def _finish(cnt=loaded_count, tot=total):
                    prog_f.destroy()
                    _log(f"Auto-load complete: {cnt}/{tot} shows loaded",
                         'SUCCESS')
                    _refresh_preview()
                win.after(0, _finish)

            t = threading.Thread(target=_worker, daemon=True)
            t.start()

        template_var = tk.StringVar(value=_saved_template)
        movie_template_var = tk.StringVar(value=_saved_movie_template)

        # Save templates on change
        def _on_template_change(*_):
            app._tv_rename_template = template_var.get()
            app.save_preferences()
            _refresh_preview()
        template_var.trace_add('write', _on_template_change)

        def _on_movie_template_change(*_):
            app._movie_rename_template = movie_template_var.get()
            app.save_preferences()
            _refresh_preview()
        movie_template_var.trace_add('write', _on_movie_template_change)

        # ── Row 1: Template display ──
        tmpl_display = ttk.Frame(main_f)
        tmpl_display.grid(row=1, column=0, columnspan=3, sticky='ew', padx=6, pady=(2, 0))
        tmpl_display.columnconfigure(1, weight=1)
        tmpl_display.columnconfigure(3, weight=1)

        ttk.Label(tmpl_display, text="TV:", font=('Helvetica', 9, 'bold')).grid(
            row=0, column=0, sticky='w', padx=(0, 4))
        _tv_tmpl_lbl = ttk.Label(tmpl_display, text=template_var.get(),
                                  font=('Helvetica', 9), foreground='#336')
        _tv_tmpl_lbl.grid(row=0, column=1, sticky='w')

        ttk.Label(tmpl_display, text="Movie:", font=('Helvetica', 9, 'bold')).grid(
            row=0, column=2, sticky='w', padx=(16, 4))
        _mv_tmpl_lbl = ttk.Label(tmpl_display, text=movie_template_var.get(),
                                  font=('Helvetica', 9), foreground='#633')
        _mv_tmpl_lbl.grid(row=0, column=3, sticky='w')

        def _update_tmpl_labels(*_):
            _tv_tmpl_lbl.configure(text=template_var.get())
            _mv_tmpl_lbl.configure(text=movie_template_var.get())
        template_var.trace_add('write', _update_tmpl_labels)
        movie_template_var.trace_add('write', _update_tmpl_labels)

        # ── Row 2: File list (treeview) ──
        tree_f = ttk.Frame(main_f)
        tree_f.grid(row=2, column=0, columnspan=3, sticky='nsew', padx=4, pady=4)
        main_f.rowconfigure(2, weight=1)

        columns = ('current', 'type', 'new_name')
        tree = ttk.Treeview(tree_f, columns=columns, show='headings',
                            selectmode='extended')
        tree.heading('current', text='Current Filename')
        tree.heading('type', text='Type')
        tree.heading('new_name', text='New Filename')
        tree.column('current', width=320, minwidth=150)
        tree.column('type', width=55, minwidth=45, anchor='center')
        tree.column('new_name', width=380, minwidth=150)

        tree_scroll = ttk.Scrollbar(tree_f, orient='vertical', command=tree.yview)
        tree.configure(yscrollcommand=tree_scroll.set)
        tree.pack(side='left', fill='both', expand=True)
        tree_scroll.pack(side='right', fill='y')

        def _refresh_preview():
            """Update the treeview with current/new filenames."""
            tree.delete(*tree.get_children())
            template = template_var.get().strip()
            m_template = movie_template_var.get().strip()

            for item in _file_items:
                cur_name = os.path.basename(item['path'])
                s = item.get('season')
                e = item.get('episode')

                # Match file to a loaded show
                matched = _match_file_to_show(item)
                item['matched_show'] = matched

                new_name = ''
                is_movie = (isinstance(_all_shows.get(matched), dict)
                            and _all_shows.get(matched, {}).get('_is_movie'))
                has_ep = (s is not None and e is not None)
                has_date = item.get('air_date') is not None
                type_label = '—'
                if matched and (is_movie or has_ep or has_date):
                    type_label = 'Movie' if is_movie else 'TV'
                    try:
                        new_name = _build_new_name(item, template, matched,
                                                   movie_template=m_template) or ''
                    except (KeyError, ValueError):
                        new_name = '(template error)'

                iid = tree.insert('', 'end',
                                  values=(cur_name, type_label, new_name))
                # Color rows without matches
                if not new_name or new_name == '(template error)':
                    tree.item(iid, tags=('nomatch',))

            tree.tag_configure('nomatch', foreground='#999')
            # Update undo button state
            try:
                undo_btn.configure(
                    state='normal' if _rename_history else 'disabled')
            except Exception:
                pass

        # ── Right-click context menu ──
        _tree_ctx = tk.Menu(tree, tearoff=0)

        def _open_containing_folder():
            """Open the folder containing the selected file."""
            sel = tree.selection()
            if not sel:
                return
            idx = tree.index(sel[0])
            if idx < len(_file_items):
                folder = os.path.dirname(_file_items[idx]['path'])
                try:
                    subprocess.Popen(['xdg-open', folder])
                except Exception:
                    pass

        def _copy_new_name():
            """Copy the new filename of the selected file to clipboard."""
            sel = tree.selection()
            if not sel:
                return
            vals = tree.item(sel[0], 'values')
            if vals and len(vals) > 2 and vals[2]:
                win.clipboard_clear()
                win.clipboard_append(vals[2])

        def _on_tree_right_click(event):
            iid = tree.identify_row(event.y)
            if iid:
                if iid not in tree.selection():
                    tree.selection_set(iid)
            _tree_ctx.delete(0, 'end')
            sel = tree.selection()
            if sel:
                idx = tree.index(sel[0])
                # ── Per-file actions ──
                _tree_ctx.add_command(
                    label="Set Episode...",
                    command=_set_episode_for_selected)
                # Copy new name
                vals = tree.item(sel[0], 'values')
                if vals and len(vals) > 2 and vals[2]:
                    _tree_ctx.add_command(
                        label="Copy New Name",
                        command=_copy_new_name)
                _tree_ctx.add_command(
                    label="Open Folder",
                    command=_open_containing_folder)
                _tree_ctx.add_separator()
                _tree_ctx.add_command(
                    label=f"Remove Selected ({len(sel)} file{'s' if len(sel) > 1 else ''})",
                    command=_remove_selected_files)
                # "Remove show" — unload the matched show for the selected file
                if idx < len(_file_items):
                    show = _file_items[idx].get('matched_show')
                    if show:
                        _tree_ctx.add_command(
                            label=f"Remove show \"{show}\"",
                            command=_remove_show_for_selected)
                _tree_ctx.add_separator()
            _tree_ctx.add_command(label="Clear all files",
                                 command=_clear_files)
            _tree_ctx.tk_popup(event.x_root, event.y_root)

        def _remove_selected_files():
            """Remove selected files from the queue."""
            sel = tree.selection()
            if not sel:
                return
            # Get indices in reverse order to avoid shifting
            indices = sorted([tree.index(iid) for iid in sel], reverse=True)
            for idx in indices:
                if idx < len(_file_items):
                    _file_items.pop(idx)
            _log(f"Removed {len(indices)} file(s)")
            # Remove shows that no longer have any files matched
            remaining_shows = {f.get('matched_show') for f in _file_items
                               if f.get('matched_show')}
            orphaned = [s for s in _all_shows if s not in remaining_shows]
            for s in orphaned:
                _all_shows.pop(s, None)
            _refresh_preview()

        tree.bind('<Button-3>', _on_tree_right_click)

        # ── Shift+Arrow multi-select ──
        def _shift_arrow(evt, direction):
            items = tree.get_children()
            if not items:
                return 'break'
            focus = tree.focus()
            if not focus:
                return 'break'
            idx = list(items).index(focus)
            new_idx = idx + direction
            if new_idx < 0 or new_idx >= len(items):
                return 'break'
            new_item = items[new_idx]
            tree.focus(new_item)
            tree.see(new_item)
            tree.selection_add(new_item)
            return 'break'

        tree.bind('<Shift-Up>',   lambda e: _shift_arrow(e, -1))
        tree.bind('<Shift-Down>', lambda e: _shift_arrow(e, 1))

        # ── Drag and drop ──
        _RENAME_EXTENSIONS = VIDEO_EXTENSIONS | SUBTITLE_EXTENSIONS

        def _add_paths(paths):
            """Add files/folders to the file list. Recursively scans folders."""
            added = 0
            for p in paths:
                if os.path.isdir(p):
                    for root_dir, _dirs, files in os.walk(p):
                        _dirs[:] = sorted(d for d in _dirs if not d.startswith('.'))
                        for f in sorted(files):
                            if f.startswith('.'):
                                continue
                            fp = os.path.join(root_dir, f)
                            ext = os.path.splitext(f)[1].lower()
                            if ext in _RENAME_EXTENSIONS:
                                s, e = _parse_episode_info(f)
                                item = {'path': fp, 'season': s,
                                        'episode': e, 'ext': ext}
                                if s == 'date':
                                    item['air_date'] = e
                                    item['season'] = None
                                    item['episode'] = None
                                # Probe video files for media tags
                                if ext in VIDEO_EXTENSIONS:
                                    item['media_tags'] = _probe_media_tags(fp)
                                _file_items.append(item)
                                added += 1
                elif os.path.isfile(p):
                    ext = os.path.splitext(p)[1].lower()
                    if ext in _RENAME_EXTENSIONS:
                        s, e = _parse_episode_info(p)
                        item = {'path': p, 'season': s,
                                'episode': e, 'ext': ext}
                        if s == 'date':
                            item['air_date'] = e
                            item['season'] = None
                            item['episode'] = None
                        if ext in VIDEO_EXTENSIONS:
                            item['media_tags'] = _probe_media_tags(p)
                        _file_items.append(item)
                        added += 1
            _v = sum(1 for i in _file_items if i['ext'] in VIDEO_EXTENSIONS)
            _s = sum(1 for i in _file_items if i['ext'] in SUBTITLE_EXTENSIONS)
            _log(f"Added {added} files ({_v} video, {_s} subtitle)")
            # Auto-load any new shows detected from the added files
            has_key = (api_key_var.get().strip()
                       if provider_var.get() == 'TVDB'
                       else tmdb_key_var.get().strip())
            if added > 0 and has_key:
                _auto_load_shows()
            elif added > 0 and provider_var.get() == 'TMDB' and not tmdb_key_var.get().strip():
                _log("TMDB selected but no API key entered. "
                     "Get a free key at themoviedb.org", 'WARNING')
                _refresh_preview()
            else:
                _refresh_preview()

        def _on_drop(event):
            raw = event.data
            paths = []
            if 'file://' in raw:
                from urllib.parse import unquote, urlparse
                for line in raw.splitlines():
                    line = line.strip()
                    if line.startswith('file://'):
                        decoded = unquote(urlparse(line).path)
                        if decoded:
                            paths.append(decoded)
            else:
                i = 0
                while i < len(raw):
                    if raw[i] == '{':
                        end = raw.find('}', i)
                        paths.append(raw[i + 1:end])
                        i = end + 2
                    elif raw[i] == ' ':
                        i += 1
                    else:
                        end = raw.find(' ', i)
                        if end == -1:
                            end = len(raw)
                        paths.append(raw[i:end])
                        i = end + 1
            if paths:
                _add_paths(paths)

        try:
            win.drop_target_register(DND_FILES)
            win.dnd_bind('<<Drop>>', _on_drop)
        except Exception:
            pass

        # ── Selection preview label ──
        _sel_preview = ttk.Label(main_f, text="", font=('Courier', 9),
                                  foreground='#336', anchor='w')
        _sel_preview.grid(row=3, column=0, columnspan=3, sticky='ew',
                          padx=6, pady=(2, 0))

        def _on_tree_select(event=None):
            sel = tree.selection()
            if sel:
                vals = tree.item(sel[0], 'values')
                if vals and len(vals) > 2 and vals[2]:
                    _sel_preview.configure(text=f"New: {vals[2]}")
                else:
                    _sel_preview.configure(text=f"Current: {vals[0]}" if vals else "")
            else:
                _sel_preview.configure(text="")
        tree.bind('<<TreeviewSelect>>', _on_tree_select)

        # ── Row 4: Buttons ──
        btn_f = ttk.Frame(main_f)
        btn_f.grid(row=4, column=0, columnspan=3, sticky='ew', padx=4, pady=(4, 0))

        def _do_rename():
            """Rename all files with valid new names."""
            template = template_var.get().strip()
            m_template = movie_template_var.get().strip()
            if not template:
                messagebox.showwarning("No Template", "Enter a filename template.",
                                       parent=win)
                return
            renamed = 0
            skipped = 0
            errors = 0
            batch_history = []  # [(old_path, new_path), ...]
            created_dirs = []   # track dirs created for undo cleanup
            # Track parent folder renames so we only rename each once
            _renamed_parents = {}  # old_parent → new_parent
            for item in _file_items:
                try:
                    matched = item.get('matched_show')
                    if not matched:
                        skipped += 1
                        continue
                    new_name = _build_new_name(item, template, matched,
                                              movie_template=m_template)
                    if not new_name:
                        skipped += 1
                        continue
                    old_path = item['path']

                    # If parent was already renamed by a previous file, update path
                    orig_parent = os.path.dirname(old_path)
                    if orig_parent in _renamed_parents:
                        old_path = os.path.join(
                            _renamed_parents[orig_parent],
                            os.path.basename(old_path))

                    if '/' in new_name:
                        # Folder template: first component renames the parent dir,
                        # remaining components are subfolders within it
                        parts = new_name.split('/')
                        show_folder = parts[0]
                        remaining = os.path.join(*parts[1:]) if len(parts) > 1 else parts[0]

                        current_parent = os.path.dirname(old_path)
                        current_parent_name = os.path.basename(current_parent)
                        grandparent = os.path.dirname(current_parent)

                        # Rename parent folder if needed (only once per folder)
                        if current_parent_name != show_folder:
                            new_parent = os.path.join(grandparent, show_folder)
                            if orig_parent not in _renamed_parents:
                                if not os.path.exists(new_parent):
                                    os.rename(current_parent, new_parent)
                                    _renamed_parents[orig_parent] = new_parent
                                    batch_history.append((current_parent, new_parent))
                                    _log(f"  Renamed folder: {current_parent_name} → {show_folder}")
                                elif new_parent == current_parent:
                                    pass  # same folder, no rename needed
                                else:
                                    _log(f"  Folder already exists: {show_folder}", 'WARNING')
                            current_parent = _renamed_parents.get(orig_parent, current_parent)
                            old_path = os.path.join(current_parent, os.path.basename(old_path))

                        # Build new path within the (renamed) parent
                        new_path = os.path.join(current_parent, remaining)
                    else:
                        # Flat template: rename within the same directory
                        new_path = os.path.join(os.path.dirname(old_path), new_name)

                    if old_path == new_path:
                        item['_renamed'] = True
                        renamed += 1
                        continue
                    if os.path.exists(new_path):
                        _log(f"Skipped (exists): {new_name}", 'WARNING')
                        skipped += 1
                        continue
                    # Create subdirectories if needed (e.g. Season 01)
                    new_dir = os.path.dirname(new_path)
                    if new_dir and not os.path.exists(new_dir):
                        os.makedirs(new_dir, exist_ok=True)
                        created_dirs.append(new_dir)
                    os.rename(old_path, new_path)
                    batch_history.append((old_path, new_path))
                    item['path'] = new_path
                    item['_renamed'] = True
                    renamed += 1
                except Exception as e:
                    _log(f"Error renaming: {e}", 'ERROR')
                    errors += 1
            # Save undo history (include created dirs and removed items for restore)
            renamed_items = [i.copy() for i in _file_items if i.get('_renamed')]
            if batch_history:
                _rename_history.append({
                    'renames': batch_history,
                    'created_dirs': created_dirs,
                    'items': renamed_items,
                })
            # Remove successfully renamed files from the list
            _file_items[:] = [i for i in _file_items if not i.get('_renamed')]
            parts = [f"Renamed {renamed} files"]
            if skipped:
                parts.append(f"{skipped} skipped (no match)")
            if errors:
                parts.append(f"{errors} errors")
            msg = " — ".join(parts)
            _log(msg, 'SUCCESS')
            _refresh_preview()
            if errors:
                messagebox.showwarning("Rename Complete", msg, parent=win)
            else:
                messagebox.showinfo("Rename Complete", msg, parent=win)

        def _do_undo():
            """Undo the last rename batch."""
            if not _rename_history:
                _log("Nothing to undo", 'WARNING')
                return
            entry = _rename_history.pop()
            # Support both old format (list) and new format (dict)
            if isinstance(entry, dict):
                batch = entry['renames']
                created_dirs = entry.get('created_dirs', [])
                saved_items = entry.get('items', [])
            else:
                batch = entry
                created_dirs = []
                saved_items = []
            undone = 0
            errors = 0

            # Separate folder renames from file renames —
            # folder renames are entries where new_path is a directory
            file_renames = []
            folder_renames = []
            for old_path, new_path in batch:
                if os.path.isdir(new_path) or (
                        not os.path.exists(new_path) and not os.path.splitext(new_path)[1]):
                    folder_renames.append((old_path, new_path))
                else:
                    file_renames.append((old_path, new_path))

            # Phase 1: Undo file renames (reverse order)
            restored_old_paths = set()
            for old_path, new_path in reversed(file_renames):
                try:
                    if os.path.exists(new_path) and not os.path.exists(old_path):
                        os.rename(new_path, old_path)
                        for item in _file_items:
                            if item['path'] == new_path:
                                item['path'] = old_path
                                break
                        restored_old_paths.add(old_path)
                        undone += 1
                    else:
                        _log(f"Cannot undo: {os.path.basename(new_path)}", 'WARNING')
                        errors += 1
                except Exception as e:
                    _log(f"Undo error: {e}", 'ERROR')
                    errors += 1

            # Phase 2: Clean up empty subdirectories (Season folders etc.)
            for d in sorted(created_dirs, key=len, reverse=True):
                try:
                    if os.path.isdir(d) and not os.listdir(d):
                        os.rmdir(d)
                except OSError:
                    pass

            # Phase 3: Undo folder renames (reverse order — after files moved out)
            for old_path, new_path in reversed(folder_renames):
                try:
                    if os.path.exists(new_path) and not os.path.exists(old_path):
                        os.rename(new_path, old_path)
                        # Update restored file paths to reflect parent rename
                        for item in _file_items:
                            if item['path'].startswith(new_path + os.sep):
                                item['path'] = item['path'].replace(
                                    new_path, old_path, 1)
                        undone += 1
                    else:
                        _log(f"Cannot undo folder: {os.path.basename(new_path)}", 'WARNING')
                        errors += 1
                except Exception as e:
                    _log(f"Undo folder error: {e}", 'ERROR')
                    errors += 1

            # Phase 4: Clean up any remaining empty directories
            # (parent folder after rename-back may have empty subdirs)
            for d in sorted(created_dirs, key=len, reverse=True):
                try:
                    if os.path.isdir(d) and not os.listdir(d):
                        os.rmdir(d)
                except OSError:
                    pass
            # Also walk up from created_dirs to clean empty parents
            for d in sorted(created_dirs, key=len, reverse=True):
                parent = os.path.dirname(d)
                while parent and parent != os.path.dirname(parent):
                    try:
                        if os.path.isdir(parent) and not os.listdir(parent):
                            os.rmdir(parent)
                            parent = os.path.dirname(parent)
                        else:
                            break
                    except OSError:
                        break

            # Restore items that were removed from the list after rename
            if saved_items and (restored_old_paths or folder_renames):
                existing_paths = {f['path'] for f in _file_items}
                for item in saved_items:
                    orig_path = None
                    for old_path, new_path in file_renames:
                        if new_path == item['path'] and old_path in restored_old_paths:
                            orig_path = old_path
                            break
                    # Also check if the path needs parent folder adjustment
                    if orig_path:
                        for fold_old, fold_new in folder_renames:
                            if orig_path.startswith(fold_new + os.sep):
                                orig_path = orig_path.replace(fold_new, fold_old, 1)
                                break
                    if orig_path and orig_path not in existing_paths:
                        item['path'] = orig_path
                        item.pop('_renamed', None)
                        _file_items.append(item)
            msg = f"Undone {undone} rename(s)"
            if errors:
                msg += f" ({errors} errors)"
            _log(msg, 'SUCCESS' if not errors else 'WARNING')
            _refresh_preview()

        def _set_episode_for_selected():
            """Open a dialog to manually set season/episode for selected files."""
            sel = tree.selection()
            if not sel:
                return
            idx = tree.index(sel[0])
            if idx >= len(_file_items):
                return
            item = _file_items[idx]

            dlg = tk.Toplevel(win)
            dlg.title("Set Episode")
            dlg.geometry("320x180")
            dlg.resizable(False, False)
            dlg.transient(win)
            dlg.grab_set()
            _center_on_parent(dlg, win)

            f = ttk.Frame(dlg, padding=16)
            f.pack(fill='both', expand=True)

            ttk.Label(f, text=os.path.basename(item['path']),
                      font=('Helvetica', 9), wraplength=280).grid(
                          row=0, column=0, columnspan=2, sticky='w', pady=(0, 10))

            cur_s = item.get('season')
            cur_e = item.get('episode')
            # For multi-episode, show the first episode
            if isinstance(cur_e, list):
                cur_e = cur_e[0] if cur_e else ''

            ttk.Label(f, text="Season:").grid(row=1, column=0, sticky='w', pady=4)
            s_var = tk.StringVar(value=str(cur_s) if cur_s is not None else '')
            s_entry = ttk.Entry(f, textvariable=s_var, width=8)
            s_entry.grid(row=1, column=1, sticky='w', padx=(8, 0), pady=4)

            ttk.Label(f, text="Episode:").grid(row=2, column=0, sticky='w', pady=4)
            e_var = tk.StringVar(value=str(cur_e) if cur_e is not None else '')
            e_entry = ttk.Entry(f, textvariable=e_var, width=8)
            e_entry.grid(row=2, column=1, sticky='w', padx=(8, 0), pady=4)

            def _apply():
                try:
                    sv = s_var.get().strip()
                    ev = e_var.get().strip()
                    new_s = int(sv) if sv else None
                    new_e = int(ev) if ev else None
                except ValueError:
                    messagebox.showwarning("Invalid", "Enter valid numbers.",
                                           parent=dlg)
                    return
                # Apply to all selected files
                for iid in sel:
                    i = tree.index(iid)
                    if i < len(_file_items):
                        _file_items[i]['season'] = new_s
                        _file_items[i]['episode'] = new_e
                dlg.destroy()
                _refresh_preview()

            btn_f = ttk.Frame(f)
            btn_f.grid(row=3, column=0, columnspan=2, sticky='e', pady=(12, 0))
            ttk.Button(btn_f, text="Apply", command=_apply,
                       width=8).pack(side='right', padx=(4, 0))
            ttk.Button(btn_f, text="Cancel", command=dlg.destroy,
                       width=8).pack(side='right')

            s_entry.focus_set()
            s_entry.select_range(0, 'end')
            dlg.wait_window()

        rename_btn = ttk.Button(btn_f, text="✏ Rename All", command=_do_rename,
                                width=12)
        rename_btn.pack(side='left', padx=2)
        create_tooltip(rename_btn, "Rename all files to their new names")

        undo_btn = ttk.Button(btn_f, text="↩ Undo", command=_do_undo,
                              width=8, state='disabled')
        undo_btn.pack(side='left', padx=2)
        create_tooltip(undo_btn, "Undo the last rename operation")


        def _refresh_shows():
            """Re-query the provider for all shows and refresh previews."""
            if not _file_items:
                _log("No files loaded — add files first", 'WARNING')
                return
            _all_shows.clear()
            for item in _file_items:
                item.pop('matched_show', None)
            _log(f"Refreshing from {provider_var.get()}...")
            _auto_load_shows()

        refresh_btn = ttk.Button(btn_f, text="🔄 Refresh", command=_refresh_shows,
                                 width=10)
        refresh_btn.pack(side='left', padx=2)
        create_tooltip(refresh_btn, "Re-query the provider for all shows")

        def _clear_files():
            _file_items.clear()
            _all_shows.clear()
            _rename_history.clear()
            tree.delete(*tree.get_children())
            _log("File list cleared")
            undo_btn.configure(state='disabled')

        clear_btn = ttk.Button(btn_f, text="Clear", command=_clear_files, width=8)
        clear_btn.pack(side='left', padx=2)
        create_tooltip(clear_btn, "Remove all files from the list")

        def _browse_files():
            paths = ask_open_files(
                parent=win, title="Select Video Files",
                filetypes=[("Video files", "*.mkv *.mp4 *.avi *.mov *.ts *.m2ts"),
                           ("All files", "*.*")])
            if paths:
                _add_paths(list(paths))

        def _browse_folder():
            path = ask_directory(parent=win, title="Select Folder")
            if path:
                _add_paths([path])

        # ── Row 4: Log ──
        log_f = ttk.LabelFrame(main_f, text="Log", padding=4)
        log_f.grid(row=5, column=0, columnspan=3, sticky='nsew', padx=4, pady=(4, 0))
        main_f.rowconfigure(5, weight=1)
        log_text = tk.Text(log_f, height=4, wrap='word', font=('Courier', 9),
                           state='disabled', bg='#1e1e1e', fg='#d4d4d4')
        log_scroll = ttk.Scrollbar(log_f, orient='vertical', command=log_text.yview)
        log_text.configure(yscrollcommand=log_scroll.set)
        log_text.pack(side='left', fill='both', expand=True)
        log_scroll.pack(side='right', fill='y')

        def _clear_log():
            log_text.configure(state='normal')
            log_text.delete('1.0', 'end')
            log_text.configure(state='disabled')

        clear_log_btn = ttk.Button(log_f, text="Clear Log", command=_clear_log, width=8)
        clear_log_btn.pack(side='bottom', anchor='e', pady=(4, 0))

        # ══════════════════════════════════════════════════════════════
        # Menu Bar
        # ══════════════════════════════════════════════════════════════

        menubar = tk.Menu(win)
        win.configure(menu=menubar)

        # ── File menu ──
        file_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="File", menu=file_menu)
        file_menu.add_command(label="Add Files...", command=_browse_files,
                              accelerator="Ctrl+O")
        file_menu.add_command(label="Add Folder...", command=_browse_folder,
                              accelerator="Ctrl+Shift+O")
        file_menu.add_separator()
        file_menu.add_command(label="Rename All", command=_do_rename,
                              accelerator="Ctrl+R")
        file_menu.add_separator()
        file_menu.add_command(label="Clear All", command=_clear_files)
        file_menu.add_command(label="Clear Log", command=_clear_log)
        file_menu.add_separator()
        file_menu.add_command(label="Close",
                              command=lambda: _close_window(),
                              accelerator="Ctrl+W")

        # ── Edit menu ──
        edit_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Edit", menu=edit_menu)
        edit_menu.add_command(label="Undo Rename", command=_do_undo,
                              accelerator="Ctrl+Z")
        edit_menu.add_separator()
        edit_menu.add_command(label="Set Episode...",
                              command=_set_episode_for_selected)
        edit_menu.add_separator()
        edit_menu.add_command(label="Select All",
                              command=lambda: tree.selection_set(
                                  tree.get_children()),
                              accelerator="Ctrl+A")
        edit_menu.add_command(label="Remove Selected",
                              command=_remove_selected_files,
                              accelerator="Delete")

        # ── Settings menu ──
        settings_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Settings", menu=settings_menu)

        # Provider submenu
        provider_menu = tk.Menu(settings_menu, tearoff=0)
        settings_menu.add_cascade(label="Provider", menu=provider_menu)
        provider_menu.add_radiobutton(label="TVDB", variable=provider_var,
                                       value='TVDB')
        provider_menu.add_radiobutton(label="TMDB", variable=provider_var,
                                       value='TMDB')

        # Template dialog
        def _center_on_parent(dlg, parent):
            """Center a dialog over its parent window."""
            parent.update_idletasks()
            dlg.update_idletasks()
            px, py = parent.winfo_x(), parent.winfo_y()
            pw, ph = parent.winfo_width(), parent.winfo_height()
            dw, dh = dlg.winfo_width(), dlg.winfo_height()
            if dw <= 1 or dh <= 1:
                geo = dlg.geometry()
                try:
                    size_part = geo.split('+')[0]
                    if 'x' in size_part:
                        dw, dh = map(int, size_part.split('x'))
                except (ValueError, IndexError):
                    dw = dlg.winfo_reqwidth()
                    dh = dlg.winfo_reqheight()
            x = max(0, px + (pw - dw) // 2)
            y = max(0, py + (ph - dh) // 2)
            dlg.geometry(f"{dw}x{dh}+{x}+{y}")

        def _open_template_settings():
            dlg = tk.Toplevel(win)
            dlg.title("Filename Template")
            dlg.geometry(scaled_geometry(dlg, 860, 750))
            dlg.minsize(*scaled_minsize(dlg, 780, 650))
            dlg.resizable(True, True)
            dlg.grab_set()
            _center_on_parent(dlg, win)

            # Close button packed from bottom first so it's never clipped
            close_frame = ttk.Frame(dlg, padding=(20, 4, 20, 12))
            close_frame.pack(fill='x', side='bottom')
            ttk.Button(close_frame, text="Close",
                       command=lambda: _on_close(),
                       width=8).pack(side='right')
            ttk.Button(close_frame, text="Template Wizard...",
                       command=lambda: (_on_close(), _open_template_wizard()),
                       width=16).pack(side='left')

            # Scrollable content area
            _canvas = tk.Canvas(dlg, highlightthickness=0)
            _vscroll = ttk.Scrollbar(dlg, orient='vertical', command=_canvas.yview)
            _canvas.configure(yscrollcommand=_vscroll.set)
            _vscroll.pack(side='right', fill='y')
            _canvas.pack(side='left', fill='both', expand=True)

            f = ttk.Frame(_canvas, padding=(20, 20, 20, 0))
            _canvas_win = _canvas.create_window((0, 0), window=f, anchor='nw')

            def _on_frame_configure(event):
                _canvas.configure(scrollregion=_canvas.bbox('all'))
            f.bind('<Configure>', _on_frame_configure)

            def _on_canvas_configure(event):
                _canvas.itemconfig(_canvas_win, width=event.width)
            _canvas.bind('<Configure>', _on_canvas_configure)

            def _on_mousewheel(event):
                _canvas.yview_scroll(event.delta // 120 or (
                    -1 if event.num == 4 else 1), 'units')
            # Use bind_all on the dialog so mousewheel works on any widget
            dlg.bind_all('<Button-4>', _on_mousewheel)
            dlg.bind_all('<Button-5>', _on_mousewheel)
            dlg.bind_all('<MouseWheel>', _on_mousewheel)
            # Clean up bind_all when dialog closes to prevent bleed-through
            def _on_close():
                dlg.unbind_all('<Button-4>')
                dlg.unbind_all('<Button-5>')
                dlg.unbind_all('<MouseWheel>')
                dlg.destroy()
            dlg.protocol('WM_DELETE_WINDOW', _on_close)

            # ── Active template entries ──
            ttk.Label(f, text="TV template:",
                      font=('Helvetica', 11)).grid(
                          row=0, column=0, sticky='w', padx=(0, 8), pady=(0, 6))
            t_entry = ttk.Entry(f, textvariable=template_var, width=50,
                                font=('Helvetica', 10))
            t_entry.grid(row=0, column=1, sticky='ew', pady=(0, 6))
            f.columnconfigure(1, weight=1)

            ttk.Label(f, text="Movie template:",
                      font=('Helvetica', 11)).grid(
                          row=1, column=0, sticky='w', padx=(0, 8), pady=(0, 10))
            m_entry = ttk.Entry(f, textvariable=movie_template_var, width=50,
                                font=('Helvetica', 10))
            m_entry.grid(row=1, column=1, sticky='ew', pady=(0, 10))

            # ── Variables reference ──
            ttk.Label(f, text="Available variables:",
                      font=('Helvetica', 10, 'bold')).grid(
                          row=2, column=0, columnspan=2, sticky='w',
                          pady=(6, 4))
            vars_text = (
                "{show}       — Show / movie name\n"
                "{season}     — Season number (zero-padded)\n"
                "{episode}    — Episode number (zero-padded)\n"
                "{title}      — Episode title\n"
                "{year}       — Air / release year\n"
                "{tvdb}       — TVDB ID (e.g. tvdb-475560)\n"
                "{tmdb}       — TMDB ID (e.g. tmdb-12345)\n"
                "{resolution} — Auto-detected (e.g. 1080p)\n"
                "{vcodec}     — Auto-detected (e.g. x265)\n"
                "{acodec}     — Auto-detected (e.g. AAC)\n"
                "{source}     — From filename (e.g. BluRay)\n"
                "{hdr}        — Auto-detected (e.g. HDR10)\n"
                "\n"
                "Use / to create folders automatically."
            )
            vars_box = tk.Text(f, font=('Courier', 10), height=14, width=50,
                               wrap='none', relief='flat',
                               bg=f.winfo_toplevel().cget('bg'),
                               cursor='arrow')
            vars_box.insert('1.0', vars_text)
            vars_box.configure(state='disabled')
            vars_box.grid(row=3, column=0, columnspan=2, sticky='w',
                          padx=(15, 0))
            def _vars_copy(event=None):
                try:
                    sel = vars_box.get('sel.first', 'sel.last')
                    vars_box.clipboard_clear()
                    vars_box.clipboard_append(sel)
                except tk.TclError:
                    pass
            vars_ctx = tk.Menu(vars_box, tearoff=0)
            vars_ctx.add_command(label="Copy", command=_vars_copy)
            vars_box.bind('<Button-1>', lambda e: vars_box.focus_set())
            vars_box.bind('<ButtonPress-3>',
                          lambda e: vars_ctx.tk_popup(e.x_root, e.y_root))
            vars_box.bind('<Control-c>', _vars_copy)

            # ── Presets: TV (left) and Movie (right) side by side ──
            presets_frame = ttk.Frame(f)
            presets_frame.grid(row=4, column=0, columnspan=2, sticky='ew',
                               pady=(12, 0))
            presets_frame.columnconfigure(0, weight=1)
            presets_frame.columnconfigure(1, weight=1)

            # ── Custom template data (separate lists for TV and Movie) ──
            _custom_tv = list(getattr(app, '_custom_tv_templates', []))
            _custom_mv = list(getattr(app, '_custom_movie_templates', []))
            # One-time migration: move old shared list to TV, then clear it
            _old_shared = list(getattr(app, '_custom_rename_templates', []))
            if _old_shared:
                if not _custom_tv:
                    _custom_tv = list(_old_shared)
                    app._custom_tv_templates = _custom_tv
                app._custom_rename_templates = []
                app.save_preferences()

            def _save_prefs():
                app._custom_tv_templates = _custom_tv
                app._custom_movie_templates = _custom_mv
                app.save_preferences()

            # ── Helper: build a preset column with custom section ──
            def _build_preset_column(parent, label, flat_presets, folder_presets,
                                     target_var, custom_list):
                col = ttk.LabelFrame(parent, text=label, padding=6)

                def _refresh_custom():
                    # Clear old custom buttons
                    for w in col.winfo_children():
                        if getattr(w, '_is_custom', False):
                            w.destroy()
                    # Add custom template buttons
                    for tmpl in custom_list:
                        desc = tmpl
                        # Truncate long templates for display
                        if len(desc) > 42:
                            desc = desc[:39] + '...'
                        btn_f = ttk.Frame(col)
                        btn_f.pack(anchor='w', pady=1)
                        btn_f._is_custom = True
                        def _use(t=tmpl):
                            target_var.set(t)
                        ttk.Button(btn_f, text=desc, command=_use,
                                   width=34).pack(side='left')
                        def _del(t=tmpl):
                            if t in custom_list:
                                custom_list.remove(t)
                                _save_prefs()
                                _refresh_custom()
                        ttk.Button(btn_f, text="✕", width=2,
                                   command=_del).pack(side='left', padx=(2, 0))
                    # "+ Save Current" button at the end
                    save_f = ttk.Frame(col)
                    save_f.pack(anchor='w', pady=(4, 0))
                    save_f._is_custom = True
                    def _save():
                        tmpl = target_var.get().strip()
                        if not tmpl:
                            return
                        if tmpl not in custom_list:
                            custom_list.append(tmpl)
                            _save_prefs()
                            _refresh_custom()
                    ttk.Button(save_f, text="+ Save Current",
                               command=_save).pack(side='left')

                # Built-in Flat presets
                ttk.Label(col, text="Flat:",
                          font=('Helvetica', 9, 'bold')).pack(anchor='w', pady=(0, 2))
                for tmpl, desc in flat_presets:
                    def _set(t=tmpl):
                        target_var.set(t)
                    ttk.Button(col, text=desc, command=_set,
                               width=38).pack(anchor='w', pady=1)

                # Built-in Folder presets
                ttk.Label(col, text="Folder:",
                          font=('Helvetica', 9, 'bold')).pack(anchor='w', pady=(8, 2))
                for tmpl, desc in folder_presets:
                    def _set(t=tmpl):
                        target_var.set(t)
                    ttk.Button(col, text=desc, command=_set,
                               width=38).pack(anchor='w', pady=1)

                # Custom section
                ttk.Label(col, text="Custom:",
                          font=('Helvetica', 9, 'bold')).pack(anchor='w', pady=(8, 2))
                _refresh_custom()

                return col

            # ── TV presets (left column) ──
            tv_flat_presets = [
                ('{show} S{season}E{episode} {title}',
                 'Show S01E01 Title'),
                ('{show} - S{season}E{episode} - {title}',
                 'Show - S01E01 - Title'),
                ('{show} {season}x{episode} {title}',
                 'Show 01x01 Title'),
                ('{show} - {season}x{episode} - {title}',
                 'Show - 01x01 - Title'),
            ]
            tv_folder_presets = [
                ('{show}/Season {season}/{show} S{season}E{episode} {title}',
                 'Show/Season 01/Show S01E01 Title'),
                ('{show}/Season {season}/{show} - S{season}E{episode} - {title}',
                 'Show/Season 01/Show - S01E01 - Title'),
                ('{show}/S{season}/{show} S{season}E{episode} {title}',
                 'Show/S01/Show S01E01 Title'),
                ('{show} {{{tvdb}}}/Season {season}/{show} S{season}E{episode} {title}',
                 'Show {tvdb-ID}/Season 01/Show S01E01 Title'),
                ('{show} {{{tmdb}}}/Season {season}/{show} S{season}E{episode} {title}',
                 'Show {tmdb-ID}/Season 01/Show S01E01 Title'),
            ]
            tv_col = _build_preset_column(
                presets_frame, "TV Presets",
                tv_flat_presets, tv_folder_presets,
                template_var, _custom_tv)
            tv_col.grid(row=0, column=0, sticky='nsew', padx=(0, 4))

            # ── Movie presets (right column) ──
            movie_flat_presets = [
                ('{show} ({year})',
                 'Movie (2026)'),
                ('{show} ({year}) {{{tmdb}}}',
                 'Movie (2026) {tmdb-12345}'),
                ('{show} ({year}) {{{tvdb}}}',
                 'Movie (2026) {tvdb-475560}'),
            ]
            movie_folder_presets = [
                ('{show} ({year})/{show} ({year})',
                 'Movie (2026)/Movie (2026)'),
                ('{show} {year}/{show} {year}',
                 'Movie 2026/Movie 2026'),
                ('{show} ({year}) {{{tmdb}}}/{show} ({year})',
                 'Movie (2026) {tmdb-ID}/Movie (2026)'),
                ('{show} ({year}) {{{tvdb}}}/{show} ({year})',
                 'Movie (2026) {tvdb-ID}/Movie (2026)'),
            ]
            mv_col = _build_preset_column(
                presets_frame, "Movie Presets",
                movie_flat_presets, movie_folder_presets,
                movie_template_var, _custom_mv)
            mv_col.grid(row=0, column=1, sticky='nsew', padx=(4, 0))

            dlg.update_idletasks()
            dlg.wait_window()

        settings_menu.add_command(label="Filename Template...",
                                  command=_open_template_settings)

        # ── Template Wizard ──
        def _open_template_wizard():
            wiz = tk.Toplevel(win)
            wiz.withdraw()
            wiz.title("Template Wizard")
            wiz.geometry(scaled_geometry(wiz, 560, 420))
            wiz.minsize(*scaled_minsize(wiz, 480, 380))
            wiz.resizable(True, True)

            # Wizard state
            _step = [0]
            _type = tk.StringVar(value='tv')           # tv or movie
            _style = tk.StringVar(value='compact')     # compact, dashes, classic, classic_dashes
            _mv_style = tk.StringVar(value='year_paren')  # year_paren, year_bare
            _folders = tk.StringVar(value='none')      # none, season, s_short
            _provider = tk.StringVar(value='none')     # none, tvdb, tmdb
            _prov_location = tk.StringVar(value='folder')  # folder, filename, both
            # Extras — checkboxes for auto-probed tags
            _extra_resolution = tk.BooleanVar(value=False)
            _extra_vcodec = tk.BooleanVar(value=False)
            _extra_acodec = tk.BooleanVar(value=False)
            _extra_source = tk.BooleanVar(value=False)
            _extra_hdr = tk.BooleanVar(value=False)
            _extra_custom = tk.StringVar(value='')

            # Navigation buttons — packed at bottom first
            nav_frame = ttk.Frame(wiz, padding=(16, 8, 16, 12))
            nav_frame.pack(side='bottom', fill='x')

            back_btn = ttk.Button(nav_frame, text="< Back", width=8)
            back_btn.pack(side='left')
            cancel_btn = ttk.Button(nav_frame, text="Cancel", width=8,
                                     command=wiz.destroy)
            cancel_btn.pack(side='right', padx=(4, 0))
            apply_btn = ttk.Button(nav_frame, text="Apply", width=8)
            apply_btn.pack(side='right', padx=(4, 0))
            save_btn = ttk.Button(nav_frame, text="Save as Custom", width=14)
            save_btn.pack(side='right', padx=(4, 0))
            next_btn = ttk.Button(nav_frame, text="Next >", width=8)
            next_btn.pack(side='right')

            # Content area
            content = ttk.Frame(wiz, padding=(24, 16, 24, 0))
            content.pack(fill='both', expand=True)

            # Step indicator
            step_label = ttk.Label(content, text="",
                                    font=('Helvetica', 9), foreground='gray')
            step_label.pack(anchor='w', pady=(0, 4))

            # Title for each step
            title_label = ttk.Label(content, text="",
                                     font=('Helvetica', 13, 'bold'))
            title_label.pack(anchor='w', pady=(0, 10))

            # Frame for step-specific widgets (cleared each step)
            step_frame = ttk.Frame(content)
            step_frame.pack(fill='both', expand=True)

            # Preview at bottom of content
            ttk.Separator(content).pack(fill='x', pady=(8, 6))
            preview_label = ttk.Label(content, text="Template:",
                                       font=('Helvetica', 9, 'bold'))
            preview_label.pack(anchor='w')
            preview_tmpl = ttk.Label(content, text="",
                                      font=('Courier', 10), foreground='#336')
            preview_tmpl.pack(anchor='w', pady=(2, 0))
            preview_ex_label = ttk.Label(content, text="Example:",
                                          font=('Helvetica', 9, 'bold'))
            preview_ex_label.pack(anchor='w', pady=(4, 0))
            preview_example = ttk.Label(content, text="",
                                         font=('Courier', 10), foreground='#363')
            preview_example.pack(anchor='w', pady=(2, 0))

            def _build_template():
                """Build the template string from wizard choices."""
                is_movie = _type.get() == 'movie'
                prov = _provider.get()
                prov_loc = _prov_location.get()

                # Build the filename part
                if is_movie:
                    if _mv_style.get() == 'year_paren':
                        name_part = '{show} ({year})'
                    else:
                        name_part = '{show} {year}'
                else:
                    style = _style.get()
                    if style == 'compact':
                        name_part = '{show} S{season}E{episode} {title}'
                    elif style == 'dashes':
                        name_part = '{show} - S{season}E{episode} - {title}'
                    elif style == 'classic':
                        name_part = '{show} {season}x{episode} {title}'
                    else:  # classic_dashes
                        name_part = '{show} - {season}x{episode} - {title}'

                # Append auto-probed media tag variables to filename
                extras = []
                if _extra_resolution.get():
                    extras.append('{resolution}')
                if _extra_vcodec.get():
                    extras.append('{vcodec}')
                if _extra_acodec.get():
                    extras.append('{acodec}')
                if _extra_source.get():
                    extras.append('{source}')
                if _extra_hdr.get():
                    extras.append('{hdr}')
                custom = _extra_custom.get().strip()
                if custom:
                    extras.append(custom)
                if extras:
                    name_part = name_part + ' ' + ' '.join(extras)

                # Append provider ID to filename if requested
                if prov != 'none' and prov_loc in ('filename', 'both'):
                    id_tag = '{{{tvdb}}}' if prov == 'tvdb' else '{{{tmdb}}}'
                    name_part = f'{name_part} {id_tag}'

                folder = _folders.get()
                if folder == 'none':
                    return name_part

                # Build folder prefix
                if is_movie:
                    if folder == 'movie_year':
                        base_folder = '{show} {year}'
                    else:
                        base_folder = '{show} ({year})'
                    if prov != 'none' and prov_loc in ('folder', 'both'):
                        id_tag = '{{{tvdb}}}' if prov == 'tvdb' else '{{{tmdb}}}'
                        folder_name = base_folder + ' ' + id_tag
                    else:
                        folder_name = base_folder
                    return f'{folder_name}/{name_part}'
                else:
                    if prov != 'none' and prov_loc in ('folder', 'both'):
                        id_tag = '{{{tvdb}}}' if prov == 'tvdb' else '{{{tmdb}}}'
                        show_dir = '{show} ' + id_tag
                    else:
                        show_dir = '{show}'

                    if folder == 'season':
                        return f'{show_dir}/Season {{season}}/{name_part}'
                    else:  # s_short
                        return f'{show_dir}/S{{season}}/{name_part}'

            def _build_example(tmpl):
                """Generate an example filename from the template."""
                try:
                    return tmpl.format(
                        show='Breaking Bad', season='01', episode='01',
                        title='Pilot', year='2008',
                        tvdb='tvdb-81189', tmdb='tmdb-1396',
                        resolution='1080p', vcodec='x265', acodec='AAC',
                        source='BluRay', hdr='HDR10')
                except (KeyError, IndexError):
                    return '(preview unavailable)'

            def _update_preview(*_):
                tmpl = _build_template()
                preview_tmpl.configure(text=tmpl)
                preview_example.configure(text=_build_example(tmpl))

            # Attach trace to all vars for live preview
            for var in (_type, _style, _mv_style, _folders, _provider,
                        _prov_location, _extra_resolution, _extra_vcodec,
                        _extra_acodec, _extra_source, _extra_hdr, _extra_custom):
                var.trace_add('write', _update_preview)

            # ── Step definitions ──
            steps_tv = ['type', 'style', 'folders', 'provider', 'extras', 'confirm']
            steps_movie = ['type', 'mv_style', 'folders', 'provider', 'extras', 'confirm']

            def _get_steps():
                return steps_movie if _type.get() == 'movie' else steps_tv

            def _show_step():
                # Clear step frame
                for w in step_frame.winfo_children():
                    w.destroy()

                steps = _get_steps()
                idx = _step[0]
                step_name = steps[idx]
                total = len(steps)
                step_label.configure(text=f"Step {idx + 1} of {total}")

                # Nav button state
                back_btn.configure(state='normal' if idx > 0 else 'disabled')
                is_last = idx == total - 1
                next_btn.pack_forget()
                apply_btn.pack_forget()
                save_btn.pack_forget()
                if is_last:
                    save_btn.pack(side='right', padx=(4, 0))
                    apply_btn.pack(side='right', padx=(4, 0))
                else:
                    next_btn.pack(side='right')

                if step_name == 'type':
                    title_label.configure(text="What are you renaming?")
                    ttk.Radiobutton(step_frame, text="TV Shows",
                                     variable=_type, value='tv').pack(
                                         anchor='w', pady=4, padx=10)
                    ttk.Radiobutton(step_frame, text="Movies",
                                     variable=_type, value='movie').pack(
                                         anchor='w', pady=4, padx=10)

                elif step_name == 'style':
                    title_label.configure(text="How should the filename look?")
                    styles = [
                        ('compact', 'Show S01E01 Title'),
                        ('dashes', 'Show - S01E01 - Title'),
                        ('classic', 'Show 01x01 Title'),
                        ('classic_dashes', 'Show - 01x01 - Title'),
                    ]
                    for val, desc in styles:
                        ttk.Radiobutton(step_frame, text=desc,
                                         variable=_style, value=val).pack(
                                             anchor='w', pady=4, padx=10)

                elif step_name == 'mv_style':
                    title_label.configure(text="How should the filename look?")
                    styles = [
                        ('year_paren', 'Movie (2008)'),
                        ('year_bare', 'Movie 2008'),
                    ]
                    for val, desc in styles:
                        ttk.Radiobutton(step_frame, text=desc,
                                         variable=_mv_style, value=val).pack(
                                             anchor='w', pady=4, padx=10)

                elif step_name == 'folders':
                    title_label.configure(text="Organize into folders?")
                    is_movie = _type.get() == 'movie'
                    ttk.Radiobutton(step_frame,
                                     text="No — all files stay in current folder",
                                     variable=_folders, value='none').pack(
                                         anchor='w', pady=4, padx=10)
                    if is_movie:
                        ttk.Radiobutton(step_frame,
                                         text="Yes — Movie (Year)/filename",
                                         variable=_folders, value='season').pack(
                                             anchor='w', pady=4, padx=10)
                        ttk.Radiobutton(step_frame,
                                         text="Yes — Movie Year/filename",
                                         variable=_folders, value='movie_year').pack(
                                             anchor='w', pady=4, padx=10)
                    else:
                        ttk.Radiobutton(step_frame,
                                         text="Yes — Show/Season 01/filename",
                                         variable=_folders, value='season').pack(
                                             anchor='w', pady=4, padx=10)
                        ttk.Radiobutton(step_frame,
                                         text="Yes — Show/S01/filename",
                                         variable=_folders, value='s_short').pack(
                                             anchor='w', pady=4, padx=10)

                elif step_name == 'provider':
                    title_label.configure(
                        text="Include a database ID?")
                    # Provider selection
                    current_prov = provider_var.get()  # TVDB or TMDB
                    ttk.Radiobutton(step_frame, text="No ID",
                                     variable=_provider, value='none').pack(
                                         anchor='w', pady=4, padx=10)
                    ttk.Radiobutton(step_frame,
                                     text=f"TVDB ID  (e.g. tvdb-81189)"
                                     + ("  — current provider" if current_prov == 'TVDB' else ""),
                                     variable=_provider, value='tvdb').pack(
                                         anchor='w', pady=4, padx=10)
                    ttk.Radiobutton(step_frame,
                                     text=f"TMDB ID  (e.g. tmdb-1396)"
                                     + ("  — current provider" if current_prov == 'TMDB' else ""),
                                     variable=_provider, value='tmdb').pack(
                                         anchor='w', pady=4, padx=10)

                    # Location sub-options (only shown when a provider is selected)
                    loc_frame = ttk.LabelFrame(step_frame, text="Where to put the ID",
                                                padding=6)
                    loc_frame.pack(anchor='w', fill='x', padx=10, pady=(10, 4))
                    has_folders = _folders.get() != 'none'
                    ttk.Radiobutton(loc_frame,
                                     text="In the filename  (e.g. ...Title {tmdb-1396}.mkv)",
                                     variable=_prov_location, value='filename').pack(
                                         anchor='w', pady=2)
                    folder_rb = ttk.Radiobutton(loc_frame,
                                     text="In the folder name  (e.g. Show {tmdb-1396}/...)",
                                     variable=_prov_location, value='folder')
                    folder_rb.pack(anchor='w', pady=2)
                    both_rb = ttk.Radiobutton(loc_frame,
                                     text="Both  (folder and filename)",
                                     variable=_prov_location, value='both')
                    both_rb.pack(anchor='w', pady=2)
                    if not has_folders:
                        folder_rb.configure(state='disabled')
                        both_rb.configure(state='disabled')
                        if _prov_location.get() in ('folder', 'both'):
                            _prov_location.set('filename')
                        ttk.Label(loc_frame,
                                   text="(Enable folder structure in the previous step "
                                   "to use folder placement)",
                                   foreground='gray',
                                   font=('Helvetica', 8)).pack(anchor='w', padx=20)

                    def _toggle_loc_frame(*_):
                        if _provider.get() == 'none':
                            for child in loc_frame.winfo_children():
                                if isinstance(child, ttk.Radiobutton):
                                    child.configure(state='disabled')
                        else:
                            for child in loc_frame.winfo_children():
                                if isinstance(child, ttk.Radiobutton):
                                    if child in (folder_rb, both_rb) and not has_folders:
                                        child.configure(state='disabled')
                                    else:
                                        child.configure(state='normal')
                    _provider.trace_add('write', _toggle_loc_frame)
                    _toggle_loc_frame()

                elif step_name == 'extras':
                    title_label.configure(text="Add media tags?  (optional)")
                    ttk.Label(step_frame,
                               text="Check the tags to include. Values are auto-detected\n"
                               "from each video file using ffprobe.",
                               foreground='gray',
                               font=('Helvetica', 9)).pack(anchor='w', padx=10, pady=(0, 8))

                    grid_f = ttk.Frame(step_frame)
                    grid_f.pack(fill='x', padx=10)

                    tags = [
                        (_extra_resolution, 'Resolution',   '{resolution}', 'e.g. 1080p, 2160p'),
                        (_extra_vcodec,     'Video codec',  '{vcodec}',     'e.g. x265, x264, AV1'),
                        (_extra_acodec,     'Audio codec',  '{acodec}',     'e.g. AAC, DTS, TrueHD'),
                        (_extra_source,     'Source',        '{source}',     'e.g. BluRay, WEB-DL (from filename)'),
                        (_extra_hdr,        'HDR',           '{hdr}',        'e.g. HDR10, SDR'),
                    ]
                    for var, label, var_name, hint in tags:
                        row_f = ttk.Frame(grid_f)
                        row_f.pack(fill='x', pady=3)
                        ttk.Checkbutton(row_f, text=label,
                                         variable=var).pack(side='left')
                        ttk.Label(row_f, text=f"  {var_name}  — {hint}",
                                   foreground='gray',
                                   font=('Helvetica', 8)).pack(side='left')

                    ttk.Separator(grid_f).pack(fill='x', pady=(8, 6))
                    custom_f = ttk.Frame(grid_f)
                    custom_f.pack(fill='x')
                    ttk.Label(custom_f, text="Custom text:").pack(side='left', padx=(0, 8))
                    ttk.Entry(custom_f, textvariable=_extra_custom,
                              width=20).pack(side='left', fill='x', expand=True)

                elif step_name == 'confirm':
                    title_label.configure(text="Your template is ready!")
                    ttk.Label(step_frame,
                               text="Click Apply to use this template now,\n"
                               "or Save as Custom to keep it for later.",
                               font=('Helvetica', 10)).pack(
                                   anchor='w', pady=4, padx=10)

                _update_preview()

            def _next():
                steps = _get_steps()
                if _step[0] < len(steps) - 1:
                    _step[0] += 1
                    _show_step()

            def _back():
                if _step[0] > 0:
                    _step[0] -= 1
                    _show_step()

            def _switch_provider_if_needed():
                """Switch the active provider to match the wizard's choice."""
                prov = _provider.get()
                if prov == 'none':
                    return
                target = 'TVDB' if prov == 'tvdb' else 'TMDB'
                if provider_var.get() != target:
                    provider_var.set(target)
                    _log(f"Wizard: switched provider to {target}")

            def _apply():
                tmpl = _build_template()
                _switch_provider_if_needed()
                if _type.get() == 'movie':
                    movie_template_var.set(tmpl)
                else:
                    template_var.set(tmpl)
                _log(f"Wizard: applied {'movie' if _type.get() == 'movie' else 'TV'} "
                     f"template: {tmpl}")
                wiz.destroy()

            def _save_and_apply():
                tmpl = _build_template()
                _switch_provider_if_needed()
                is_movie = _type.get() == 'movie'
                if is_movie:
                    movie_template_var.set(tmpl)
                    customs = list(getattr(app, '_custom_movie_templates', []))
                    if tmpl not in customs:
                        customs.append(tmpl)
                        app._custom_movie_templates = customs
                        app.save_preferences()
                else:
                    template_var.set(tmpl)
                    customs = list(getattr(app, '_custom_tv_templates', []))
                    if tmpl not in customs:
                        customs.append(tmpl)
                        app._custom_tv_templates = customs
                        app.save_preferences()
                _log(f"Wizard: saved and applied {'movie' if is_movie else 'TV'} "
                     f"template: {tmpl}")
                wiz.destroy()

            next_btn.configure(command=_next)
            back_btn.configure(command=_back)
            apply_btn.configure(command=_apply)
            save_btn.configure(command=_save_and_apply)

            # Show first step
            _show_step()

            # Center and show
            wiz.update_idletasks()
            _center_on_parent(wiz, win)
            wiz.deiconify()

        settings_menu.add_command(label="Template Wizard...",
                                  command=_open_template_wizard)

        # TMDB Key dialog
        def _open_api_key_settings():
            dlg = tk.Toplevel(win)
            dlg.title("API Keys")
            dlg.geometry("520x320")
            dlg.minsize(450, 280)
            dlg.resizable(True, True)
            dlg.transient(win)
            dlg.grab_set()
            _center_on_parent(dlg, win)

            f = ttk.Frame(dlg, padding=20)
            f.pack(fill='both', expand=True)
            f.columnconfigure(1, weight=1)

            # ── TVDB ──
            ttk.Label(f, text="TVDB API Key:",
                      font=('Helvetica', 10, 'bold')).grid(
                          row=0, column=0, columnspan=2, sticky='w',
                          pady=(0, 4))
            tvdb_entry = ttk.Entry(f, textvariable=api_key_var, width=45)
            tvdb_entry.grid(row=1, column=0, columnspan=2, sticky='ew',
                            pady=(0, 2))

            tvdb_link = ttk.Label(
                f, text="Get a free key at thetvdb.com/dashboard/account/apikey",
                foreground='#3a6ea5', font=('Helvetica', 9, 'underline'),
                cursor='hand2')
            tvdb_link.grid(row=2, column=0, columnspan=2, sticky='w',
                           pady=(0, 16))
            tvdb_link.bind('<Button-1>', lambda e: subprocess.Popen(
                ['xdg-open', 'https://thetvdb.com/dashboard/account/apikey']))

            # ── TMDB ──
            ttk.Label(f, text="TMDB API Key (v3):",
                      font=('Helvetica', 10, 'bold')).grid(
                          row=3, column=0, columnspan=2, sticky='w',
                          pady=(0, 4))
            tmdb_entry = ttk.Entry(f, textvariable=tmdb_key_var, width=45)
            tmdb_entry.grid(row=4, column=0, columnspan=2, sticky='ew',
                            pady=(0, 2))

            tmdb_link = ttk.Label(
                f, text="Get a free key at themoviedb.org/settings/api",
                foreground='#3a6ea5', font=('Helvetica', 9, 'underline'),
                cursor='hand2')
            tmdb_link.grid(row=5, column=0, columnspan=2, sticky='w',
                           pady=(0, 16))
            tmdb_link.bind('<Button-1>', lambda e: subprocess.Popen(
                ['xdg-open', 'https://www.themoviedb.org/settings/api']))

            def _save_and_close():
                app._tvdb_api_key = api_key_var.get().strip()
                app._tmdb_api_key = tmdb_key_var.get().strip()
                app.save_preferences()
                _log("API keys saved")
                dlg.destroy()

            btn_f = ttk.Frame(f)
            btn_f.grid(row=6, column=0, columnspan=2, sticky='e',
                       pady=(8, 0))
            ttk.Button(btn_f, text="Save", command=_save_and_close,
                       width=8).pack(side='right', padx=(4, 0))
            ttk.Button(btn_f, text="Cancel", command=dlg.destroy,
                       width=8).pack(side='right')

            dlg.wait_window()

        settings_menu.add_command(label="API Keys...",
                                  command=_open_api_key_settings)

        # ── Help menu ──
        help_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Help", menu=help_menu)

        def _show_about():
            messagebox.showinfo("About Docflix Media Renamer",
                f"Docflix Media Renamer\n"
                f"Part of {APP_NAME} v{APP_VERSION}\n\n"
                f"Rename video and subtitle files using\n"
                f"episode data from TVDB or TMDB.\n\n"
                f"Drag and drop files or folders to begin.",
                parent=win)

        help_menu.add_command(label="Template Variables...",
                              command=_open_template_settings)
        help_menu.add_separator()
        help_menu.add_command(label="About...", command=_show_about)

        # ── Window close ──
        def _close_window():
            win.destroy()
            if getattr(app, '_standalone_mode', False):
                app.root.destroy()

        win.protocol('WM_DELETE_WINDOW', _close_window)

        # ── Keyboard shortcuts ──
        win.bind('<Control-o>', lambda e: _browse_files())
        win.bind('<Control-O>', lambda e: _browse_folder())
        win.bind('<Control-r>', lambda e: _do_rename())
        win.bind('<Control-R>', lambda e: _do_rename())
        win.bind('<Control-z>', lambda e: _do_undo())
        win.bind('<Control-Z>', lambda e: _do_undo())
        win.bind('<Control-w>', lambda e: _close_window())
        win.bind('<Control-W>', lambda e: _close_window())
        win.bind('<Control-a>', lambda e: tree.selection_set(
            tree.get_children()))
        win.bind('<Delete>', lambda e: _remove_selected_files())

        _log(f"Docflix Media Renamer ready — provider: {provider_var.get()}")
        _log("Drag and drop video files or folders to begin")



def main():
    """Launch File Renamer as a standalone application."""
    from .standalone import create_standalone_root

    root, app = create_standalone_root(
        title="\U0001f4fa Docflix Media Renamer",
        geometry="960x650",
        minsize=(800, 550),
    )

    app._standalone_mode = True
    root.withdraw()
    open_tv_renamer(app)

    root.mainloop()


if __name__ == '__main__':
    main()
