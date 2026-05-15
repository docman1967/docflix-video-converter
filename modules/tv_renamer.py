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
from .utils import create_tooltip, get_dpi_scale, scaled_geometry, scaled_minsize, ask_open_files, ask_directory

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
        _query_to_show = {}  # search query → loaded show name (remembers user picks)

        # Load preferences
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
            _log(f"Logging in to TVDB (key: {key[:8]}...)")
            result = _tvdb_request('POST', '/login', {'apikey': key})
            _log(f"Login response: {result.get('status') if result else 'None'}")
            if result and result.get('status') == 'success':
                _tvdb_token[0] = result['data']['token']
                _log("TVDB login successful")
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
                        'original_name': r.get('original_name', ''),
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
                        'original_name': r.get('original_title', ''),
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
            # Check if the show has a Specials season (Season 0)
            has_specials = any(
                s.get('season_number') == 0
                for s in details.get('seasons', []))
            start_season = 0 if has_specials else 1
            for sn in range(start_season, num_seasons + 1):
                season_data = _tmdb_request(f'/tv/{series_id}/season/{sn}')
                if not season_data or 'episodes' not in season_data:
                    _log(f"  Season {sn}: no data")
                    continue
                for ep in season_data['episodes']:
                    all_eps.append({
                        'id': ep.get('id', ''),
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
            # Date-based: 2026.04.22, 2026-04-22, 2026_04_22, 2026 04 22
            m = re.search(r'((?:19|20)\d{2})[.\-_\s](0[1-9]|1[0-2])[.\-_\s](0[1-9]|[12]\d|3[01])', name)
            if m:
                # Return a special marker — date stored in item dict later
                return 'date', f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
            # SP01, SP 01 — special episode (Season 0)
            m = re.search(r'\bSP\s*(\d{1,3})\b', name, re.IGNORECASE)
            if m:
                return 0, int(m.group(1))
            # E01 or Ep01 (season assumed from folder or default 1)
            m = re.search(r'[Ee](?:p|pisode)?\s*(\d{1,3})', name)
            if m:
                return None, int(m.group(1))
            # Keyword-based specials: "Special", "Bonus", "Extra"
            m = re.search(
                r'\b(?:Special|Bonus|Extra|Behind[.\s]+the[.\s]+Scenes)\b',
                name, re.IGNORECASE)
            if m:
                after = name[m.end():]
                num_m = re.search(r'\s*(\d{1,3})', after)
                return 0, int(num_m.group(1)) if num_m else 1
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

        # Regex to detect Season-style folder names that should be skipped
        # when looking for the show folder (e.g. "Season 1", "Season 02",
        # "Series 3", "S01", "S1")
        _SEASON_FOLDER_RE = re.compile(
            r'^(?:Season|Series|S)\s*\d+$', re.IGNORECASE)

        def _get_show_folder(filepath):
            """Return the show-level parent folder name for a file path,
            skipping past Season subfolders.  For a file at
            .../Ghosts UK/Season 1/episode.mkv, returns "Ghosts UK"
            instead of "Season 1"."""
            parent = os.path.dirname(filepath)
            parent_name = os.path.basename(parent)
            if parent_name and _SEASON_FOLDER_RE.match(parent_name):
                # Parent is a Season folder — use grandparent instead
                grandparent = os.path.dirname(parent)
                grandparent_name = os.path.basename(grandparent)
                if grandparent_name:
                    return grandparent_name
            return parent_name

        def _match_file_to_show(item):
            """Match a file to one of the loaded shows by filename and
            parent folder name.  The folder name is used to disambiguate
            when multiple loaded shows match the filename (e.g. two shows
            named "Ghosts" in folders "Ghosts (US)" and "Ghosts (2019)")."""
            if not _all_shows:
                return None
            fname = os.path.splitext(os.path.basename(item['path']))[0]
            cleaned = _normalize_for_match(_clean_show_name(fname))

            # Also extract the show folder name for disambiguation,
            # skipping past Season subfolders
            parent_dir = _get_show_folder(item['path'])
            folder_cleaned = _normalize_for_match(
                _clean_show_name(parent_dir)) if parent_dir else ''

            # When filename has no show name (e.g. "S01E03.mkv"),
            # use the parent folder name as the search key instead
            if not cleaned:
                if not folder_cleaned:
                    return None
                cleaned = folder_cleaned

            # Check if the user already picked a show for this folder
            # (via the Multiple Matches dialog during auto-load)
            if folder_cleaned and folder_cleaned in _query_to_show:
                mapped = _query_to_show[folder_cleaned]
                if mapped in _all_shows:
                    return mapped

            best_match = None
            best_score = 0.0
            candidates = []  # collect all matches above threshold
            for show_name in _all_shows:
                show_norm = _normalize_for_match(show_name)
                score = 0.0
                # Exact match on filename
                if show_norm == cleaned:
                    candidates.append((show_name, 1.0))
                    continue
                # Show name contained in filename
                if show_norm in cleaned:
                    score = len(show_norm) / max(len(cleaned), 1)
                # Filename contained in show name
                elif cleaned in show_norm:
                    score = len(cleaned) / max(len(show_norm), 1) * 0.8
                if score >= 0.3:
                    candidates.append((show_name, score))

            # Word-level overlap fallback (only if no good matches yet)
            if not candidates or max(s for _, s in candidates) < 0.4:
                cleaned_words = set(cleaned.split())
                for show_name in _all_shows:
                    if any(sn == show_name for sn, _ in candidates):
                        continue
                    show_words = set(_normalize_for_match(show_name).split())
                    if show_words and cleaned_words:
                        overlap = (len(cleaned_words & show_words)
                                   / len(show_words))
                        if overlap >= 0.5:
                            candidates.append((show_name, overlap))

            if not candidates:
                return None

            # If only one candidate, return it
            if len(candidates) == 1:
                return candidates[0][0]

            # Multiple candidates — sort by filename score
            candidates.sort(key=lambda x: -x[1])

            # If the top candidate has a clear filename advantage over the
            # runner-up, trust the filename match.  This prevents folder
            # disambiguation from overriding an unambiguous filename —
            # e.g. filename "Ghosts (US) S01E01" matches "Ghosts (US)" at
            # 1.0 vs "Ghosts" at 0.6; the filename is definitive.
            if candidates[0][1] - candidates[1][1] >= 0.3:
                return candidates[0][0]

            # Close filename scores — use parent folder to disambiguate
            if folder_cleaned:
                for show_name, score in candidates:
                    show_norm = _normalize_for_match(show_name)
                    # Folder matches show name exactly
                    if show_norm == folder_cleaned:
                        return show_name
                    # Show name contained in folder (e.g. show "Ghosts"
                    # folder "ghosts 2019")
                    if show_norm in folder_cleaned:
                        folder_score = len(show_norm) / max(
                            len(folder_cleaned), 1)
                        if folder_score > best_score:
                            best_score = folder_score
                            best_match = show_name
                    # Folder contained in show name
                    elif folder_cleaned in show_norm:
                        folder_score = (len(folder_cleaned)
                                        / max(len(show_norm), 1) * 0.8)
                        if folder_score > best_score:
                            best_score = folder_score
                            best_match = show_name

                if best_match:
                    return best_match

            # No folder disambiguation — return the highest-scoring candidate
            return candidates[0][0]

        def _match_episode_by_title(item, show_name):
            """When a file has no SxxExx info, try to identify the episode
            by matching the part of the filename after the show name against
            all episode titles in the loaded show data.
            Returns (season, episode) if a match is found, or (None, None)."""
            show_data = _all_shows.get(show_name)
            if not show_data or not isinstance(show_data, dict):
                return None, None
            if show_data.get('_is_movie'):
                return None, None

            # Roman numeral → digit mapping for title normalization
            _ROMAN_MAP = {
                'i': '1', 'ii': '2', 'iii': '3', 'iv': '4',
                'v': '5', 'vi': '6', 'vii': '7', 'viii': '8',
                'ix': '9', 'x': '10', 'xi': '11', 'xii': '12',
                'xiii': '13', 'xiv': '14', 'xv': '15',
                'xvi': '16', 'xvii': '17', 'xviii': '18',
                'xix': '19', 'xx': '20',
            }

            def _norm_title(text):
                """Extra normalization for title matching — strips periods
                and commas that differ between filenames and API names
                (e.g. 'vs.' in API vs 'vs' in filename), and converts
                Roman numerals to digits (e.g. 'II' → '2') so
                'World War II' matches 'World War 2'."""
                n = _normalize_for_match(text)
                n = re.sub(r'[.,!?]', '', n)
                n = re.sub(r'\s+', ' ', n).strip()
                # Replace Roman numerals with digits — only when they
                # appear as whole words (not part of a longer word)
                words = n.split()
                words = [_ROMAN_MAP.get(w, w) for w in words]
                return ' '.join(words)

            # Build cleaned filename without extension
            fname = os.path.splitext(os.path.basename(item['path']))[0]
            # Strip subtitle language/tag suffixes that splitext leaves
            # behind (e.g. "Movie.eng" from "Movie.eng.srt",
            # "Movie.eng.forced" from "Movie.eng.forced.srt")
            if item.get('ext') in SUBTITLE_EXTENSIONS:
                fname = re.sub(
                    r'[\.\s](?:forced|sdh|hi|cc|'
                    r'[a-z]{2,3})$', '', fname, flags=re.IGNORECASE)
                fname = re.sub(
                    r'[\.\s](?:forced|sdh|hi|cc|'
                    r'[a-z]{2,3})$', '', fname, flags=re.IGNORECASE)
            fname_clean = re.sub(r'[._]', ' ', fname).strip()
            fname_clean = re.sub(r'\[[^\]]*\]', '', fname_clean)
            # Strip quality/release/codec tags to get just the
            # meaningful part
            fname_clean = re.sub(
                r'\s*(?:720|1080|2160|480)[pPiI].*', '', fname_clean)
            fname_clean = re.sub(
                r'\s*(?:WEB|HDTV|BluRay|BDRip|DVDRip|REMUX|PROPER).*',
                '', fname_clean, flags=re.IGNORECASE)
            fname_clean = re.sub(
                r'\s*(?:x264|x265|h264|h265|HEVC|AVC|AAC|DDP|'
                r'FLAC|10bit|ATMOS|TrueHD|DTS)\b.*',
                '', fname_clean, flags=re.IGNORECASE)
            fname_clean = re.sub(
                r'\s*-\s*[A-Za-z][A-Za-z0-9]*\s*$', '', fname_clean)
            fname_norm = _norm_title(fname_clean)
            show_norm = _norm_title(show_name)

            # Extract the part of the filename after the show name
            # e.g. "america facts vs fiction world war ii" minus
            #      "america facts vs fiction" = "world war ii"
            remainder = ''
            if show_norm and show_norm in fname_norm:
                idx = fname_norm.index(show_norm) + len(show_norm)
                remainder = fname_norm[idx:].strip(' -')
            if not remainder:
                # Try word-by-word prefix removal
                show_words = show_norm.split()
                fname_words = fname_norm.split()
                if show_words and fname_words:
                    # Find the longest prefix of fname_words that matches
                    # show_words (allowing for partial coverage)
                    best_end = 0
                    for i in range(min(len(show_words), len(fname_words))):
                        if fname_words[i] == show_words[i]:
                            best_end = i + 1
                        else:
                            break
                    if best_end > 0 and best_end < len(fname_words):
                        remainder = ' '.join(fname_words[best_end:])

            if not remainder:
                return None, None

            # Score each episode title against the remainder
            best_s, best_e, best_score = None, None, 0.0
            for key, ep_data in show_data.items():
                if not isinstance(key, tuple) or len(key) != 2:
                    continue
                sk, ek = key
                if sk == 'date':
                    continue
                if not isinstance(ep_data, dict):
                    continue
                ep_title = ep_data.get('name', '')
                if not ep_title:
                    continue
                title_norm = _norm_title(ep_title)
                if not title_norm:
                    continue

                # Exact match
                if title_norm == remainder:
                    return sk, ek

                # Title contained in remainder or remainder in title
                score = 0.0
                if title_norm in remainder:
                    score = len(title_norm) / max(len(remainder), 1)
                elif remainder in title_norm:
                    score = len(remainder) / max(len(title_norm), 1) * 0.9

                # Word-overlap fallback — handles typos in filenames
                # (e.g. "villians" vs "villains") by checking how many
                # words match between the remainder and the title.
                # Uses starts-with matching so truncated words still
                # count (e.g. "villia" matches "villain").
                if score < 0.6:
                    r_words = remainder.split()
                    t_words = title_norm.split()
                    if r_words and t_words:
                        matched = 0
                        for rw in r_words:
                            for tw in t_words:
                                # Exact word match or one starts with
                                # the other (handles typos/truncation
                                # where most of the word matches)
                                if (rw == tw
                                        or (len(rw) >= 4
                                            and len(tw) >= 4
                                            and (rw[:4] == tw[:4]))):
                                    matched += 1
                                    break
                        denom = max(len(r_words), len(t_words))
                        word_score = matched / denom if denom else 0
                        # Require high overlap — at least 80% of words
                        # must match to avoid false positives
                        if word_score >= 0.8 and word_score > score:
                            score = word_score

                if score > best_score and score >= 0.6:
                    best_score = score
                    best_s, best_e = sk, ek

            if best_s is not None:
                return best_s, best_e
            return None, None

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
            # Episode ID — resolved later per-episode; default empty
            tvdb_ep_id = ''
            tmdb_ep_id = ''

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
                        tvdb_ep='', tmdb_ep='',
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
                    ep_id = str(ep_data.get('id', ''))
                    ep_id_tag = f'{prov}-{ep_id}' if prov and ep_id else ''
                    name = template.format(
                        show=show_name,
                        season=str(s).zfill(2),
                        episode=str(e).zfill(2),
                        title=title,
                        year=show_data.get('_year', ''),
                        tvdb=tvdb_id,
                        tmdb=tmdb_id,
                        tvdb_ep=ep_id_tag,
                        tmdb_ep=ep_id_tag,
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
                first_ep_id = ''
                for ep_num in e:
                    ep_data = show_data.get((s, ep_num))
                    if ep_data:
                        t = ep_data.get('name', '')
                        if t:
                            titles.append(t)
                        if not first_ep_id:
                            first_ep_id = str(ep_data.get('id', ''))
                title = ' & '.join(titles) if titles else ''
                ep_id_tag = f'{prov}-{first_ep_id}' if prov and first_ep_id else ''
                name = template.format(
                    show=show_name,
                    season=str(s).zfill(2),
                    episode=ep_tag,
                    title=title,
                    year=show_data.get('_year', ''),
                    tvdb=tvdb_id,
                    tmdb=tmdb_id,
                    tvdb_ep=ep_id_tag,
                    tmdb_ep=ep_id_tag,
                    **media_vars,
                )
            else:
                # Single episode
                ep_num = e[0] if isinstance(e, list) else e
                ep_data = show_data.get((s, ep_num))
                title = ep_data.get('name', '') if ep_data else ''
                ep_id = str(ep_data.get('id', '')) if ep_data else ''
                ep_id_tag = f'{prov}-{ep_id}' if prov and ep_id else ''
                name = template.format(
                    show=show_name,
                    season=str(s).zfill(2),
                    episode=str(ep_num).zfill(2),
                    title=title,
                    year=show_data.get('_year', ''),
                    tvdb=tvdb_id,
                    tmdb=tmdb_id,
                    tvdb_ep=ep_id_tag,
                    tmdb_ep=ep_id_tag,
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

        # ── Row 0: Loaded Shows ──

        def _normalize_for_match(text):
            """Normalize a show name for comparison: lowercase, strip
            apostrophes and periods, collapse '&' / 'and' / ':'
            differences, and squash extra whitespace.  Apostrophe
            removal ensures "Greys Anatomy" matches "Grey's Anatomy".
            Period removal ensures "vs." matches "vs", "Dr." matches
            "Dr", etc. — filenames never have periods (dots are
            separators converted to spaces by _clean_show_name)."""
            t = text.lower()
            t = t.replace("'", '')
            t = t.replace('\u2019', '')   # right single quote (curly)
            t = t.replace('\u2018', '')   # left single quote (curly)
            t = t.replace('&', ' and ')
            t = t.replace(':', ' ')
            t = t.replace('.', ' ')
            t = re.sub(r'\s+', ' ', t).strip()
            return t

        def _clean_show_name(raw):
            """Strip episode info, quality tags, release groups, codec tags,
            streaming service tags, and special markers from a show name."""
            # Replace dots and underscores with spaces, but preserve hyphens
            # that are part of the show name (e.g. 9-1-1, S.W.A.T., X-Men)
            name = re.sub(r'[._]', ' ', raw).strip()
            # Strip bracketed tags early: [YTS], [RARBG], [YTS.MX], etc.
            name = re.sub(r'\[[^\]]*\]', '', name)
            # Replace hyphens that act as word separators (surrounded by spaces
            # or at the boundary of a release group like "h264-GRACE") but keep
            # hyphens between non-space characters (e.g. "9-1-1", "X-Men")
            name = re.sub(r'(?<=\s)-|-(?=\s)', ' ', name)
            # Truncate at episode markers (including multi-episode S01E01E02)
            name = re.sub(r'\s*[Ss]\d{1,2}\s*[Ee]\d.*', '', name)
            name = re.sub(r'\s*\d{1,2}[xX]\d.*', '', name)
            # Truncate at date-based episode markers (2026 04 22)
            name = re.sub(r'\s*(?:19|20)\d{2}\s+(?:0[1-9]|1[0-2])\s+(?:0[1-9]|[12]\d|3[01]).*', '', name)
            # Truncate at special episode markers (SP01, OVA, Special, etc.)
            name = re.sub(r'\s+SP\s*\d+\b.*', '', name, flags=re.IGNORECASE)
            name = re.sub(r'\s+OVA\s*\d*\b.*', '', name, flags=re.IGNORECASE)
            # "Special"/"Bonus"/"Extra" — only truncate if preceded by at
            # least 2 words (avoids stripping from show names like
            # "Special Agent Oso").  Uses a non-capturing check instead
            # of a variable-width lookbehind.
            name = re.sub(
                r'(\S+\s+\S+\s+)(?:Special|Bonus|Extra|'
                r'Behind the Scenes)\b.*',
                r'\1', name, flags=re.IGNORECASE)
            # Truncate at quality/resolution tags
            name = re.sub(r'\s*(?:720|1080|2160|480)[pPiI].*', '', name)
            # Truncate at common source/release tags
            name = re.sub(r'\s*(?:WEB|HDTV|BluRay|BDRip|DVDRip|REMUX|PROPER).*',
                          '', name, flags=re.IGNORECASE)
            # Truncate at codec tags
            name = re.sub(
                r'\s*(?:x264|x265|h264|h265|HEVC|AVC|AAC|'
                r'DDP\s*5\s*1|DDP|FLAC|10bit|ATMOS|TrueHD|'
                r'DTS(?:\s*-?\s*HD)?)\b.*',
                '', name, flags=re.IGNORECASE)
            # Truncate at streaming service tags (appear after show name
            # in scene releases, e.g. "Show Name AMZN WEB-DL")
            name = re.sub(
                r'\s+(?:AMZN|NF|HULU|DSNP|ATVP|PCOK|PMTP|STAN|'
                r'CRAV|MAX|HBO|APTV)\s.*',
                '', name, flags=re.IGNORECASE)
            # Strip trailing release group: "-GRACE", "-DHD", "-FLUX"
            # (only after all other tags have been stripped).
            # Require at least one letter to avoid stripping numeric
            # suffixes from show names like "9-1-1".
            name = re.sub(r'\s*-\s*[A-Za-z][A-Za-z0-9]*\s*$', '', name)
            # Strip trailing year (e.g. "Rise Of The Conqueror 2026" or "Movie (2026)")
            name = re.sub(r'\s+\(?(?:19|20)\d{2}\)?\s*$', '', name)
            return name.strip()

        def _rematch_selected():
            """Re-search TVDB/TMDB for the shows matched to the selected files.
            Clears the old show data and triggers a fresh API search with the
            disambiguation dialog so the user can pick the correct show."""
            sel = tree.selection()
            if not sel:
                return
            # Collect unique show names (and their search queries) from
            # the selected files
            shows_to_rematch = {}  # show_name → search_query
            for iid in sel:
                idx = tree.index(iid)
                if idx < len(_file_items):
                    item = _file_items[idx]
                    show = item.get('matched_show')
                    if show and show not in shows_to_rematch:
                        # Derive the search query: prefer folder name,
                        # fall back to cleaned filename
                        folder = _get_show_folder(item['path'])
                        stem = os.path.splitext(os.path.basename(item['path']))[0]
                        cleaned_stem = _clean_show_name(stem)
                        # Use folder name if it looks related to the show
                        if folder and _normalize_for_match(folder) != _normalize_for_match(cleaned_stem):
                            # Folder might be a better query if it's a
                            # dedicated show folder
                            query = folder
                        else:
                            query = cleaned_stem or folder or show
                        shows_to_rematch[show] = query

            if not shows_to_rematch:
                return

            count = len(shows_to_rematch)
            _log(f"Re-matching {count} show{'s' if count > 1 else ''}...")

            for old_name, query in shows_to_rematch.items():
                # 1. Remove old show data
                _all_shows.pop(old_name, None)

                # 2. Clear query→show mappings for this show
                stale = [q for q, s in _query_to_show.items()
                         if s == old_name]
                for q in stale:
                    del _query_to_show[q]

                # 3. Clear matched_show on ALL files that had this show
                #    (not just selected ones — other files from the same
                #    show need re-matching too)
                for item in _file_items:
                    if item.get('matched_show') == old_name:
                        item['matched_show'] = None

                # 4. Re-search via API — shows disambiguation dialog
                #    if multiple matches found
                new_name = _load_show_by_name(query)
                if new_name:
                    _log(f"  Re-matched \"{old_name}\" → \"{new_name}\"")
                else:
                    _log(f"  No match found for \"{query}\"", 'WARNING')

            # 5. Refresh all filenames
            _refresh_preview()

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
                # Now remove the show data and query mappings
                for name in removed:
                    _all_shows.pop(name, None)
                    # Remove any query→show mappings pointing to this show
                    stale = [q for q, s in _query_to_show.items()
                             if s == name]
                    for q in stale:
                        del _query_to_show[q]
                    _log(f"Removed \"{name}\" — {count} file(s) removed")
                _refresh_preview()

        def _clear_all_shows():
            """Remove all loaded shows."""
            _all_shows.clear()
            _query_to_show.clear()
            _refresh_preview()
            _log("All shows cleared")

        def _ask_user_pick_show(query, candidates):
            """Show a dialog for the user to pick from multiple show matches.
            candidates: list of dicts from TVDB search results.
            Returns the chosen dict, or None if cancelled."""
            dlg = tk.Toplevel(win)
            dlg.title("Multiple Matches")
            try:
                return _build_pick_show_dialog(dlg, query, candidates)
            except Exception as e:
                # Ensure dialog is destroyed if setup fails — prevents
                # orphaned windows when PIL/ImageTk is missing, etc.
                try:
                    dlg.destroy()
                except Exception:
                    pass
                raise

        def _build_pick_show_dialog(dlg, query, candidates):
            """Build and display the Multiple Matches picker dialog.
            Separated from _ask_user_pick_show so that exceptions during
            dialog setup are caught and the dialog is cleaned up."""
            dlg.geometry(scaled_geometry(dlg, 700, 500))
            dlg.minsize(*scaled_minsize(dlg, 500, 350))
            dlg.resizable(True, True)
            _dpi = get_dpi_scale(dlg)

            # Find the source folder path for this query so the user knows
            # which directory is being matched (helpful when two shows have
            # similar names, e.g. "Ghosts (US)" vs "Ghosts (2019)").
            query_norm = _normalize_for_match(query)
            source_folder = ''
            _has_video = any(i.get('ext') in VIDEO_EXTENSIONS
                            for i in _file_items)
            for item in _file_items:
                if item.get('ext') in SUBTITLE_EXTENSIONS and _has_video:
                    continue
                folder = _get_show_folder(item['path'])
                if folder:
                    folder_cleaned = _normalize_for_match(
                        _clean_show_name(folder))
                    fname = os.path.splitext(
                        os.path.basename(item['path']))[0]
                    fname_cleaned = _normalize_for_match(
                        _clean_show_name(fname))
                    if folder_cleaned == query_norm or fname_cleaned == query_norm:
                        # Show the full parent directory path (up to the
                        # show folder level) for clarity
                        parent = os.path.dirname(item['path'])
                        parent_name = os.path.basename(parent)
                        if parent_name and _SEASON_FOLDER_RE.match(parent_name):
                            source_folder = os.path.dirname(parent)
                        else:
                            source_folder = parent
                        break

            if source_folder:
                ttk.Label(dlg, text=source_folder,
                          font=('Helvetica', 9),
                          foreground='gray',
                          padding=(10, 8, 10, 0)).pack(anchor='w')

            ttk.Label(dlg, text=f"Multiple shows found for \"{query}\":",
                      font=('Helvetica', 11, 'bold'),
                      padding=(10, 4 if source_folder else 10, 10, 4)).pack(anchor='w')

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

            # Mousewheel scrolling — guard against destroyed canvas
            def _on_mousewheel(event):
                try:
                    canvas.yview_scroll(int(-1 * (event.delta / 120)), 'units')
                except tk.TclError:
                    pass
            def _on_button4(event):
                try:
                    canvas.yview_scroll(-3, 'units')
                except tk.TclError:
                    pass
            def _on_button5(event):
                try:
                    canvas.yview_scroll(3, 'units')
                except tk.TclError:
                    pass
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
                _on_close()

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

                # Thumbnail placeholder (load async later).
                # Use tk.Label with a PIL-created opaque placeholder —
                # avoids tk.PhotoImage transparency (renders as black on
                # dark themes) and avoids DPI double-scaling issues where
                # Tk's own scaling conflicts with manual pixel scaling.
                # Do NOT set explicit width/height — let the label
                # auto-size from the image content.
                try:
                    from PIL import Image as _PILImage, ImageTk as _PILImageTk
                    _blank_pil = _PILImage.new('RGB', (60, 90), '#3b3b3b')
                    _blank_img = _PILImageTk.PhotoImage(_blank_pil)
                    thumb_label = tk.Label(row_f, image=_blank_img,
                                           relief='flat', bd=0)
                    thumb_label._blank = _blank_img  # prevent GC
                except Exception:
                    # PIL/ImageTk not available — use a plain empty label
                    thumb_label = tk.Label(row_f, width=8, height=4)
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
                    # Stop downloading if the dialog was closed
                    try:
                        if not dlg.winfo_exists():
                            return
                    except Exception:
                        return
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
                    # Dialog may have been closed while thumbnails were
                    # downloading — skip if the window no longer exists
                    if not dlg.winfo_exists():
                        return
                    import io
                    from PIL import Image, ImageTk
                    img = Image.open(io.BytesIO(img_data))
                    # Scale to logical pixel size — Tk handles DPI
                    # scaling automatically.  Do NOT multiply by _dpi
                    # here (that causes double-scaling on high-DPI
                    # displays where Tk already scales the widget).
                    img.thumbnail((60, 90), Image.LANCZOS)
                    photo = ImageTk.PhotoImage(img)
                    _thumb_refs.append(photo)
                    rf = row_frames[idx]
                    for child in rf.grid_slaves(row=0, column=0):
                        # Let the label auto-size from the image —
                        # do NOT set explicit width/height (avoids
                        # DPI mismatch between image and label size)
                        child.configure(image=photo)
                        child._photo = photo  # prevent GC
                        # Re-bind click events after image loads
                        child.bind('<Button-1>',
                                   lambda e, i=idx: _select_row(i))
                        child.bind('<Double-1>',
                                   lambda e, i=idx: (_select_row(i), _ok()))
                        break
                except Exception as ex:
                    _log(f"  Thumbnail error: {ex}", 'WARNING')

            thumb_thread = threading.Thread(target=_load_thumbs, daemon=True)
            thumb_thread.start()

            # ── Buttons ──
            btn_f = ttk.Frame(dlg, padding=(10, 6))
            btn_f.pack(fill='x')

            def _no_match():
                chosen[0] = '__filename_fallback__'
                _on_close()
            ttk.Button(btn_f, text="No Match Found", command=_no_match,
                       width=16).pack(side='left', padx=4)
            ttk.Button(btn_f, text="Load", command=_ok,
                       width=10).pack(side='right', padx=4)

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

        def _fallback_from_filename(query):
            """Create a show entry derived from the filename when no provider
            match is found.  Scans _file_items to determine whether the
            query is a movie (no episode info) or TV, and extracts the year
            from the original filename if present."""
            if not query or query in _all_shows:
                return query if query in _all_shows else None

            # Scan files to find year and decide movie vs TV
            year = ''
            is_tv = False
            for item in _file_items:
                if item.get('ext') in SUBTITLE_EXTENSIONS:
                    continue
                fname = os.path.splitext(os.path.basename(item['path']))[0]
                cleaned = _clean_show_name(fname).strip()
                if _normalize_for_match(cleaned) != _normalize_for_match(query):
                    continue
                # Check for episode markers → TV
                s = item.get('season')
                e = item.get('episode')
                if s is not None and e is not None:
                    is_tv = True
                # Extract year from original filename (before cleaning strips it)
                if not year:
                    raw = re.sub(r'[._]', ' ', fname)
                    m = re.search(r'(?:^|\s)\(?((?:19|20)\d{2})\)?(?:\s|$)', raw)
                    if m:
                        year = m.group(1)

            show_name = query
            if is_tv:
                # TV show with no provider data — episodes won't have titles
                # but the template can still fill {show}, {season}, {episode}
                _all_shows[show_name] = {
                    '_series_id': '',
                    '_provider': '',
                    '_year': year,
                }
                _log(f"  No provider match — using \"{show_name}\" from filename")
            else:
                # Assume movie
                _all_shows[show_name] = {
                    '_is_movie': True,
                    '_year': year,
                    '_name': show_name,
                    '_series_id': '',
                    '_provider': '',
                }
                yr_str = f" ({year})" if year else ''
                _log(f"  No provider match — using \"{show_name}{yr_str}\" "
                     f"from filename")
            _query_to_show[_normalize_for_match(query)] = show_name
            return show_name

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
            # If still no results and the query has many words (likely
            # contains an episode title baked into the filename because
            # there was no SxxExx separator), progressively strip trailing
            # words until we get results.  e.g. "America Facts vs Fiction
            # World War II" → try without "II", then without "War II", etc.
            if not results:
                words = query.split()
                if len(words) > 2:
                    for trim in range(1, len(words) - 1):
                        shorter = ' '.join(words[:-trim])
                        if len(shorter.split()) < 2:
                            break
                        _log(f"  Retrying search as \"{shorter}\"...")
                        results = _provider_search(shorter)
                        if results:
                            # Use the shorter query for subsequent matching
                            query = shorter
                            break

            # Retry with transliteration (strip diacritics/accents) for
            # foreign titles like "Château" → "Chateau", "Señor" → "Senor"
            if not results:
                import unicodedata
                nfkd = unicodedata.normalize('NFKD', query)
                ascii_q = ''.join(
                    c for c in nfkd if not unicodedata.combining(c))
                if ascii_q != query:
                    _log(f"  Retrying search as \"{ascii_q}\"...")
                    results = _provider_search(ascii_q)

            if not results:
                _log(f"  No {prov} results for \"{query}\"", 'WARNING')
                return _fallback_from_filename(query)

            # Check if there are multiple results with the same/similar name
            # First collect both exact matches AND close matches (name contains
            # query or vice versa), then decide whether to prompt the user.
            # This catches cases like "Ghosts" returning "Ghosts", "Ghosts (US)",
            # "Ghosts (2019)", "Ghosts (DE)" — all should be presented.
            # Normalize both sides so "And" matches "&" and colons are ignored.
            #
            # Sort results so exact name matches come first — prevents movies
            # from being pushed past the scan limit by TV substring matches
            # (e.g. searching "Mars" shouldn't let 15 TV shows containing
            # "mars" push the actual movie "Mars" out of the top 15).
            query_norm = _normalize_for_match(query)

            def _match_rank(r):
                rn = _normalize_for_match(
                    r.get('name', r.get('objectName', '')))
                if rn == query_norm:
                    return 0  # exact match first
                return 1     # substring match second
            ranked = sorted(results, key=_match_rank)

            close_matches = []
            seen_ids = set()
            for r in ranked[:15]:  # limit to top 15
                rname = r.get('name', r.get('objectName', ''))
                rname_norm = _normalize_for_match(rname)
                rid = (r.get('_media_type', ''), r.get('id', ''))
                if rid in seen_ids:
                    continue
                # Check primary name
                if (rname_norm == query_norm
                        or query_norm in rname_norm
                        or rname_norm in query_norm):
                    close_matches.append(r)
                    seen_ids.add(rid)
                    continue
                # Check original/foreign title and aliases
                for alt_field in ('original_name', 'aliases'):
                    val = r.get(alt_field, '')
                    names_to_check = (
                        val if isinstance(val, list)
                        else [val] if val else [])
                    for alt_name in names_to_check:
                        alt_norm = _normalize_for_match(alt_name)
                        if alt_norm and (
                                alt_norm == query_norm
                                or query_norm in alt_norm
                                or alt_norm in query_norm):
                            close_matches.append(r)
                            seen_ids.add(rid)
                            break
                    else:
                        continue
                    break

            # If only 0–1 close matches and the query has multiple words,
            # the trailing word may be a qualifier the provider doesn't use
            # (e.g. user folder "Ghosts UK" but TVDB calls it just "Ghosts").
            # Retry with the qualifier stripped so the broader search can
            # trigger the Multiple Matches dialog.
            if len(close_matches) <= 1 and ' ' in query:
                base_query = query.rsplit(' ', 1)[0].strip()
                if base_query:
                    _log(f"  Retrying search as \"{base_query}\"...")
                    retry_results = _provider_search(base_query)
                    if retry_results:
                        base_norm = _normalize_for_match(base_query)

                        def _retry_rank(r):
                            rn = _normalize_for_match(
                                r.get('name', r.get('objectName', '')))
                            return 0 if rn == base_norm else 1

                        retry_close = []
                        retry_seen = set(seen_ids)
                        for r in sorted(retry_results,
                                        key=_retry_rank)[:15]:
                            rname = r.get('name', r.get('objectName', ''))
                            rn = _normalize_for_match(rname)
                            rid = (r.get('_media_type', ''),
                                   r.get('id', ''))
                            if rid in retry_seen:
                                continue
                            if (rn == base_norm
                                    or base_norm in rn
                                    or rn in base_norm):
                                retry_close.append(r)
                                retry_seen.add(rid)
                        # Merge: combine original close matches with retry
                        if retry_close:
                            # Deduplicate by id — keep originals first
                            merged = list(close_matches)
                            merged_ids = {
                                (r.get('_media_type', ''), r.get('id', ''))
                                for r in merged}
                            for r in retry_close:
                                rid = (r.get('_media_type', ''),
                                       r.get('id', ''))
                                if rid not in merged_ids:
                                    merged.append(r)
                                    merged_ids.add(rid)
                            if len(merged) > len(close_matches):
                                close_matches = merged
                                _log(f"  {prov} retry returned "
                                     f"{len(retry_results)} results, "
                                     f"{len(close_matches)} close matches")

            if len(close_matches) > 1:
                # Try year-based auto-disambiguation before prompting
                # the user — extract the year from the original filename
                # or folder (e.g. "Ghosts (2019)" or "Battlestar
                # Galactica 2003") and match against the show's premiere
                best = None
                context_year = ''
                for item in _file_items:
                    raw_fname = os.path.splitext(
                        os.path.basename(item['path']))[0]
                    raw_fname = re.sub(r'[._]', ' ', raw_fname)
                    folder = _get_show_folder(item['path']) or ''
                    for source in (raw_fname, folder):
                        m_yr = re.search(
                            r'\(?((?:19|20)\d{2})\)?', source)
                        if m_yr:
                            yr = m_yr.group(1)
                            # Verify this year is near the query text
                            stripped = re.sub(
                                r'\s*\(?' + yr + r'\)?\s*', ' ',
                                source).strip()
                            stripped_n = _normalize_for_match(
                                _clean_show_name(stripped))
                            if (stripped_n == query_norm
                                    or query_norm in stripped_n
                                    or stripped_n in query_norm):
                                context_year = yr
                                break
                    if context_year:
                        break
                if context_year:
                    year_matches = [
                        r for r in close_matches
                        if r.get('year', '') == context_year]
                    if len(year_matches) == 1:
                        best = year_matches[0]
                        _log(f"  Auto-selected \"{best.get('name', '')}\" "
                             f"by year ({context_year})")
                if best is None:
                    # No year match — ask the user to pick
                    _log(f"  Found {len(close_matches)} matches for "
                         f"\"{query}\" — asking...")
                    win.update_idletasks()
                    best = _ask_user_pick_show(query, close_matches)
                    if best == '__filename_fallback__':
                        return _fallback_from_filename(query)
                    if not best:
                        _log(f"  Skipped \"{query}\"")
                        return None
            elif len(close_matches) == 1:
                best = close_matches[0]
            else:
                best = results[0]

            show_name = best.get('name', best.get('objectName', ''))
            # Remember this query → show association so folder-based
            # matching and the already-loaded filter can use it later
            _query_to_show[_normalize_for_match(query)] = show_name
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
            show_eps['_year'] = best.get('year', '')
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

            # Extract unique show names from video filenames first —
            # subtitle files contain language/forced/sdh tags that pollute
            # the show name and cause failed API searches.
            # When ONLY subtitle files are loaded (no video files), fall
            # back to subtitle filenames after stripping known sub tags.
            # Also check parent folder names to disambiguate shows with the
            # same filename-derived name (e.g. two "Ghosts" shows in
            # folders "Ghosts (US)" and "Ghosts (2019)").
            _SUB_TAG_TOKENS = {
                'en', 'eng', 'es', 'spa', 'fr', 'fra', 'fre', 'de', 'deu',
                'ger', 'it', 'ita', 'pt', 'por', 'ja', 'jpn', 'ko', 'kor',
                'zh', 'zho', 'chi', 'ru', 'rus', 'ar', 'ara', 'hi', 'hin',
                'nl', 'nld', 'dut', 'sv', 'swe', 'da', 'dan', 'no', 'nor',
                'fi', 'fin', 'pl', 'pol', 'cs', 'ces', 'cze', 'el', 'ell',
                'gre', 'he', 'heb', 'tr', 'tur', 'th', 'tha', 'vi', 'vie',
                'uk', 'ukr', 'ro', 'ron', 'rum', 'hu', 'hun', 'bg', 'bul',
                'hr', 'hrv', 'sk', 'slk', 'slo', 'sl', 'slv', 'ms', 'msa',
                'may', 'id', 'ind', 'tl', 'fil', 'und',
                'forced', 'sdh', 'cc', 'hi',
            }
            has_video = any(i.get('ext') in VIDEO_EXTENSIONS
                           for i in _file_items)
            show_names = set()
            fname_to_folders = {}  # track which folders share a filename name
            for item in _file_items:
                is_sub = item.get('ext') in SUBTITLE_EXTENSIONS
                if is_sub and has_video:
                    continue  # prefer video filenames when available
                fname = os.path.splitext(os.path.basename(item['path']))[0]
                if is_sub:
                    # Strip trailing subtitle tags (lang codes, forced, sdh)
                    # e.g. "Show.Name.S01E01.eng.forced" → "Show.Name.S01E01"
                    parts = re.split(r'[\.]', fname)
                    while (parts
                           and parts[-1].lower() in _SUB_TAG_TOKENS):
                        parts.pop()
                    fname = '.'.join(parts) if parts else fname
                cleaned = _clean_show_name(fname).strip()
                # Track show folder for this filename-derived name,
                # skipping past Season subfolders
                parent = _get_show_folder(item['path'])
                folder_name = _clean_show_name(parent).strip() if parent else ''
                # When filename has no show name (e.g. "S01E03.mkv"),
                # use the parent folder name as the show name source
                if not cleaned:
                    if folder_name:
                        show_names.add(folder_name)
                    continue
                fname_to_folders.setdefault(cleaned, set())
                if folder_name:
                    fname_to_folders[cleaned].add(folder_name)

            # For each filename-derived name, check if files come from
            # multiple distinct folders — if so, use folder names instead
            # to produce separate search queries
            for fname_name, folders in fname_to_folders.items():
                if len(folders) > 1:
                    # Multiple folders share the same filename name —
                    # use folder names for disambiguation
                    for folder_name in folders:
                        show_names.add(folder_name)
                elif len(folders) == 1:
                    # Single folder — prefer the folder name when it's
                    # related to the filename (either contains it or is
                    # contained by it).  The folder is the user's chosen
                    # label: a broader folder like "Ghosts" produces a
                    # wider TMDB search than a filename like "Ghosts (US)",
                    # and a more specific folder like "Ghosts (2019)"
                    # carries extra context the filename may lack.
                    folder_name = next(iter(folders))
                    folder_norm = _normalize_for_match(folder_name)
                    fname_norm = _normalize_for_match(fname_name)
                    if folder_norm and (fname_norm in folder_norm
                                        or folder_norm in fname_norm):
                        show_names.add(folder_name)
                    else:
                        # Folder unrelated (e.g. "Downloads") — use filename
                        show_names.add(fname_name)
                else:
                    # No folder info available
                    show_names.add(fname_name)

            if not show_names:
                _log("Could not detect any show names from filenames", 'WARNING')
                return

            # Filter out names that are already matched by a loaded show
            # or previously searched (user already picked a show for this query)
            to_search = set()
            for name in show_names:
                already = False
                name_norm = _normalize_for_match(name)
                # Check if this query was already searched and resolved
                if name_norm in _query_to_show:
                    mapped = _query_to_show[name_norm]
                    if mapped in _all_shows:
                        already = True
                if not already:
                    name_lower = name.lower()
                    for loaded in _all_shows:
                        if (loaded.lower() in name_lower
                                or name_lower in loaded.lower()):
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

                # If matched to a TV show but no episode info parsed from
                # the filename, try to identify the episode by matching
                # the filename against episode titles from the API
                if (matched and not is_movie and not has_ep
                        and not has_date):
                    ts, te = _match_episode_by_title(item, matched)
                    if ts is not None and te is not None:
                        item['season'] = ts
                        item['episode'] = te
                        s, e = ts, te
                        has_ep = True
                        _log(f"Title match: \"{cur_name}\" → "
                             f"S{str(ts).zfill(2)}E{str(te).zfill(2)}")

                # Custom name override — user manually edited the name
                if item.get('custom_name'):
                    ext = item['ext']
                    sub_tags = ''
                    if ext in SUBTITLE_EXTENSIONS:
                        sub_tags = _detect_sub_tags(item['path'])
                    new_name = item['custom_name'] + sub_tags + ext
                    type_label = 'Edit'
                else:
                    type_label = '—'
                    if matched and (is_movie or has_ep or has_date):
                        type_label = 'Movie' if is_movie else 'TV'
                        try:
                            new_name = _build_new_name(
                                item, template, matched,
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
                _tree_ctx.add_command(
                    label="Edit Name...",
                    command=_edit_name_for_selected)
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
                # Collect unique shows from all selected files
                sel_shows = set()
                for iid in sel:
                    si = tree.index(iid)
                    if si < len(_file_items):
                        s = _file_items[si].get('matched_show')
                        if s:
                            sel_shows.add(s)
                if sel_shows:
                    # "Re-match" — re-search API for selected shows
                    if len(sel_shows) == 1:
                        re_label = f"Re-match \"{list(sel_shows)[0]}\""
                    else:
                        re_label = f"Re-match {len(sel_shows)} shows"
                    _tree_ctx.add_command(
                        label=re_label,
                        command=_rematch_selected)
                    # "Remove show"
                    if len(sel_shows) == 1:
                        _tree_ctx.add_command(
                            label=f"Remove show \"{list(sel_shows)[0]}\"",
                            command=_remove_show_for_selected)
                    else:
                        _tree_ctx.add_command(
                            label=f"Remove {len(sel_shows)} shows",
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

        _scanning_files = [False]  # guard against overlapping scans

        def _add_paths(paths):
            """Add files/folders to the file list in a background thread.

            Phase 1 (instant): collect file paths via os.walk.
            Phase 2 (threaded): probe each video file for media tags
            with a progress bar, then auto-load shows.
            """
            if _scanning_files[0]:
                _log("File scan already in progress — please wait", 'WARNING')
                return

            # Phase 1: collect all candidate file paths (fast, main thread)
            collected = []  # list of (filepath, ext)
            for p in paths:
                if os.path.isdir(p):
                    for root_dir, _dirs, files in os.walk(p):
                        _dirs[:] = sorted(d for d in _dirs if not d.startswith('.'))
                        for f in sorted(files):
                            if f.startswith('.'):
                                continue
                            ext = os.path.splitext(f)[1].lower()
                            if ext in _RENAME_EXTENSIONS:
                                collected.append((os.path.join(root_dir, f), ext))
                elif os.path.isfile(p):
                    ext = os.path.splitext(p)[1].lower()
                    if ext in _RENAME_EXTENSIONS:
                        collected.append((p, ext))

            if not collected:
                _log("No supported files found")
                return

            total = len(collected)
            video_count = sum(1 for _, ext in collected if ext in VIDEO_EXTENSIONS)

            # For small batches with few videos, run synchronously (fast enough)
            if video_count <= 3:
                added = 0
                for fp, ext in collected:
                    s, e = _parse_episode_info(fp)
                    item = {'path': fp, 'season': s,
                            'episode': e, 'ext': ext}
                    if s == 'date':
                        item['air_date'] = e
                        item['season'] = None
                        item['episode'] = None
                    if ext in VIDEO_EXTENSIONS:
                        item['media_tags'] = _probe_media_tags(fp)
                    _file_items.append(item)
                    added += 1
                _v = sum(1 for i in _file_items if i['ext'] in VIDEO_EXTENSIONS)
                _s = sum(1 for i in _file_items if i['ext'] in SUBTITLE_EXTENSIONS)
                _log(f"Added {added} files ({_v} video, {_s} subtitle)")
                if added > 0:
                    _auto_load_shows()
                else:
                    _refresh_preview()
                return

            # Phase 2: probe in background with progress bar
            _scanning_files[0] = True

            scan_prog_f = ttk.Frame(main_f)
            scan_prog_f.grid(row=6, column=0, columnspan=3, sticky='ew',
                             padx=4, pady=(2, 0))
            scan_prog_lbl = ttk.Label(scan_prog_f,
                                      text=f"Scanning 0/{total} files...",
                                      font=('Helvetica', 9))
            scan_prog_lbl.pack(side='left', padx=(0, 8))
            scan_prog_bar = ttk.Progressbar(scan_prog_f, maximum=total,
                                            mode='determinate')
            scan_prog_bar.pack(side='left', fill='x', expand=True)

            _scan_cancel = [False]

            def _cancel_scan():
                _scan_cancel[0] = True
                scan_cancel_btn.configure(state='disabled')

            scan_cancel_btn = ttk.Button(scan_prog_f, text="Cancel",
                                         command=_cancel_scan, width=7)
            scan_cancel_btn.pack(side='right', padx=(4, 0))

            def _scan_worker():
                import time as _time
                t0 = _time.monotonic()
                added = 0
                for i, (fp, ext) in enumerate(collected):
                    if _scan_cancel[0]:
                        break

                    s, e = _parse_episode_info(fp)
                    item = {'path': fp, 'season': s,
                            'episode': e, 'ext': ext}
                    if s == 'date':
                        item['air_date'] = e
                        item['season'] = None
                        item['episode'] = None
                    if ext in VIDEO_EXTENSIONS:
                        item['media_tags'] = _probe_media_tags(fp)
                    _file_items.append(item)
                    added += 1

                    # Update progress bar (throttled)
                    elapsed = _time.monotonic() - t0
                    if i > 0:
                        per_file = elapsed / (i + 1)
                        eta = per_file * (total - i - 1)
                        if eta >= 60:
                            eta_str = f" — {int(eta // 60)}m {int(eta % 60)}s left"
                        else:
                            eta_str = f" — {int(eta)}s left"
                    else:
                        eta_str = ""
                    win.after(0, lambda idx=i, es=eta_str: (
                        scan_prog_bar.configure(value=idx + 1),
                        scan_prog_lbl.configure(
                            text=f"Scanning {idx + 1}/{total} files{es}")))

                def _finish(cnt=added):
                    scan_prog_f.destroy()
                    _scanning_files[0] = False
                    if _scan_cancel[0]:
                        _log(f"Scan cancelled — added {cnt} of {total} files",
                             'WARNING')
                    else:
                        _v = sum(1 for i in _file_items
                                 if i['ext'] in VIDEO_EXTENSIONS)
                        _s = sum(1 for i in _file_items
                                 if i['ext'] in SUBTITLE_EXTENSIONS)
                        elapsed = _time.monotonic() - t0
                        _log(f"Added {cnt} files ({_v} video, {_s} subtitle)"
                             f" in {elapsed:.1f}s")
                    if cnt > 0:
                        _auto_load_shows()
                    else:
                        _refresh_preview()
                win.after(0, _finish)

            t = threading.Thread(target=_scan_worker, daemon=True)
            t.start()

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
                    # Custom name override from Edit Name dialog
                    if item.get('custom_name'):
                        ext = item['ext']
                        sub_tags = ''
                        if ext in SUBTITLE_EXTENSIONS:
                            sub_tags = _detect_sub_tags(item['path'])
                        new_name = item['custom_name'] + sub_tags + ext
                    elif not matched:
                        skipped += 1
                        continue
                    else:
                        new_name = _build_new_name(item, template, matched,
                                                   movie_template=m_template)
                    if not new_name:
                        skipped += 1
                        continue
                    old_path = item['path']

                    if '/' in new_name:
                        # Folder template: first component is the show dir name,
                        # remaining components are subfolders/filename within it
                        parts = new_name.split('/')
                        show_folder = parts[0]
                        remaining = os.path.join(*parts[1:]) if len(parts) > 1 else parts[0]

                        file_parent = os.path.dirname(old_path)
                        file_parent_name = os.path.basename(file_parent)

                        # Determine the show-level folder. If the file is
                        # inside a Season subfolder, go up one level so we
                        # rename the show folder, not the season folder.
                        in_season = _SEASON_FOLDER_RE.match(file_parent_name)
                        if in_season:
                            current_parent = os.path.dirname(file_parent)
                        else:
                            current_parent = file_parent
                        current_parent_name = os.path.basename(current_parent)
                        grandparent = os.path.dirname(current_parent)

                        # Detect "loose" files — files whose parent folder
                        # contains files from multiple different shows.
                        # A loose file's parent is a shared root (like "TV/"),
                        # not a dedicated show folder.  For loose files we
                        # CREATE a new show folder instead of RENAMING the
                        # parent (which would rename the entire root dir).
                        is_loose = False
                        if not in_season:
                            # Check if other files in the batch come from
                            # the same parent but match different shows
                            this_show = item.get('matched_show', '')
                            for other in _file_items:
                                if other is item:
                                    continue
                                other_parent = os.path.dirname(other['path'])
                                if other_parent == file_parent:
                                    other_show = other.get('matched_show', '')
                                    if other_show and other_show != this_show:
                                        is_loose = True
                                        break
                                # Also loose if siblings are in subfolders
                                # of the same parent (e.g. TV/Show1/ and
                                # loose file TV/Show4.S01E01.mkv)
                                if os.path.dirname(other_parent) == file_parent:
                                    is_loose = True
                                    break

                        if is_loose:
                            # Loose file: create a new show folder under
                            # the file's parent directory
                            new_parent = os.path.join(file_parent, show_folder)
                            if not os.path.exists(new_parent):
                                os.makedirs(new_parent, exist_ok=True)
                                created_dirs.append(new_parent)
                                _log(f"  Created folder: {show_folder}")
                            new_path = os.path.join(new_parent, remaining)
                        else:
                            # File is in a dedicated show folder — rename it

                            # Track by show folder path (not season folder) so
                            # files from Season 1/ and Season 2/ share the same
                            # rename record
                            orig_show_parent = current_parent

                            # If this show folder was already renamed, update
                            if orig_show_parent in _renamed_parents:
                                current_parent = _renamed_parents[orig_show_parent]

                            # Rename show folder if needed (only once per folder)
                            if current_parent_name != show_folder:
                                new_parent = os.path.join(grandparent, show_folder)
                                if orig_show_parent not in _renamed_parents:
                                    if not os.path.exists(new_parent):
                                        os.rename(current_parent, new_parent)
                                        _renamed_parents[orig_show_parent] = new_parent
                                        batch_history.append((current_parent, new_parent))
                                        _log(f"  Renamed folder: {current_parent_name} → {show_folder}")
                                    elif new_parent == current_parent:
                                        pass  # same folder, no rename needed
                                    else:
                                        _log(f"  Folder already exists: {show_folder}", 'WARNING')
                                current_parent = _renamed_parents.get(
                                    orig_show_parent, current_parent)

                            # Update old_path to reflect the renamed show folder.
                            # For season subfolder files the actual file is now at
                            # renamed_show/Season X/filename.ext
                            if in_season:
                                old_path = os.path.join(
                                    current_parent, file_parent_name,
                                    os.path.basename(old_path))
                            else:
                                old_path = os.path.join(
                                    current_parent,
                                    os.path.basename(old_path))

                            # Build new path within the (renamed) show folder
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
            # Clean up empty Season subdirectories left behind after
            # files were moved out of them into the new structure
            _removed_season_dirs = []
            for new_show_dir in _renamed_parents.values():
                if not os.path.isdir(new_show_dir):
                    continue
                for entry in sorted(os.listdir(new_show_dir)):
                    sub = os.path.join(new_show_dir, entry)
                    if (os.path.isdir(sub)
                            and _SEASON_FOLDER_RE.match(entry)
                            and not os.listdir(sub)):
                        os.rmdir(sub)
                        _removed_season_dirs.append(sub)
                        _log(f"  Removed empty folder: {entry}")

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

        def _edit_name_for_selected():
            """Open a dialog to manually edit the output filename."""
            sel = tree.selection()
            if not sel:
                return
            idx = tree.index(sel[0])
            if idx >= len(_file_items):
                return
            item = _file_items[idx]

            dlg = tk.Toplevel(win)
            dlg.title("Edit Name")
            dlg.geometry("500x150")
            dlg.resizable(True, False)
            dlg.transient(win)
            dlg.grab_set()
            _center_on_parent(dlg, win)

            f = ttk.Frame(dlg, padding=16)
            f.pack(fill='both', expand=True)
            f.columnconfigure(1, weight=1)

            ttk.Label(f, text=os.path.basename(item['path']),
                      font=('Helvetica', 9), wraplength=460).grid(
                          row=0, column=0, columnspan=2, sticky='w',
                          pady=(0, 10))

            ttk.Label(f, text="New name:").grid(
                row=1, column=0, sticky='w', pady=4)
            # Pre-fill with the current new name (without extension),
            # or the template-generated name if available
            cur_vals = tree.item(sel[0], 'values')
            cur_new = ''
            if cur_vals and len(cur_vals) > 2 and cur_vals[2]:
                # Strip extension from the displayed new name
                cur_new = os.path.splitext(cur_vals[2])[0]
            elif item.get('custom_name'):
                cur_new = item['custom_name']
            name_var = tk.StringVar(value=cur_new)
            name_entry = ttk.Entry(f, textvariable=name_var)
            name_entry.grid(row=1, column=1, sticky='ew',
                            padx=(8, 0), pady=4)

            def _apply():
                new_name = name_var.get().strip()
                if not new_name:
                    # Clear custom name — revert to template
                    item.pop('custom_name', None)
                else:
                    item['custom_name'] = new_name
                dlg.destroy()
                _refresh_preview()

            btn_f2 = ttk.Frame(f)
            btn_f2.grid(row=2, column=0, columnspan=2, sticky='e',
                        pady=(12, 0))
            ttk.Button(btn_f2, text="Apply", command=_apply,
                       width=8).pack(side='right', padx=(4, 0))
            ttk.Button(btn_f2, text="Cancel", command=dlg.destroy,
                       width=8).pack(side='right')

            name_entry.focus_set()
            name_entry.select_range(0, 'end')
            # Enter key applies
            dlg.bind('<Return>', lambda e: _apply())
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
            _query_to_show.clear()
            _rename_history.clear()
            tree.delete(*tree.get_children())
            _log("File list cleared")
            undo_btn.configure(state='disabled')

        clear_btn = ttk.Button(btn_f, text="Clear", command=_clear_files, width=8)
        clear_btn.pack(side='left', padx=2)
        create_tooltip(clear_btn, "Remove all files from the list")

        _last_browse_dir = [None]  # last directory used by Add Files/Folder

        def _get_browse_dir():
            """Return a valid initial directory for file/folder dialogs.
            Falls back to parent or home if the last dir was renamed."""
            d = _last_browse_dir[0]
            if d and os.path.isdir(d):
                return d
            # Last dir was renamed/deleted — try its parent
            if d:
                parent = os.path.dirname(d)
                if os.path.isdir(parent):
                    return parent
            return os.path.expanduser('~')

        def _browse_files():
            paths = ask_open_files(
                initialdir=_get_browse_dir(),
                parent=win, title="Select Video Files",
                filetypes=[("Video files", "*.mkv *.mp4 *.avi *.mov *.ts *.m2ts"),
                           ("All files", "*.*")])
            if paths:
                _last_browse_dir[0] = os.path.dirname(paths[0])
                _add_paths(list(paths))

        def _browse_folder():
            paths = ask_directory(initialdir=_get_browse_dir(),
                                 parent=win,
                                 title="Select Folder(s)",
                                 multiple=True)
            if paths:
                _last_browse_dir[0] = os.path.dirname(paths[-1])
                _add_paths(paths)

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
        edit_menu.add_command(label="Edit Name...",
                              command=_edit_name_for_selected)
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
            _vscroll = ttk.Scrollbar(dlg, orient='vertical')
            _canvas = tk.Canvas(dlg, highlightthickness=0,
                                yscrollcommand=_vscroll.set)
            _vscroll.configure(command=_canvas.yview)
            # Pack canvas first — scrollbar visibility managed by _update_scrollbar
            _canvas.pack(side='left', fill='both', expand=True)

            f = ttk.Frame(_canvas, padding=(20, 20, 20, 0))
            _canvas_win = _canvas.create_window((0, 0), window=f, anchor='nw')

            def _update_scrollbar():
                """Show scrollbar only when content exceeds viewport."""
                _canvas.update_idletasks()
                _canvas.configure(scrollregion=_canvas.bbox('all'))
                if f.winfo_reqheight() > _canvas.winfo_height():
                    _vscroll.pack(side='right', fill='y')
                else:
                    _vscroll.pack_forget()

            def _on_frame_configure(event):
                _update_scrollbar()
            f.bind('<Configure>', _on_frame_configure)

            def _on_canvas_configure(event):
                _canvas.itemconfig(_canvas_win, width=event.width)
                _update_scrollbar()
            _canvas.bind('<Configure>', _on_canvas_configure)

            def _on_mousewheel(event):
                try:
                    _canvas.yview_scroll(event.delta // 120 or (
                        -1 if event.num == 4 else 1), 'units')
                except tk.TclError:
                    pass
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
            vars_col1 = (
                "{show}       — Show / movie name\n"
                "{season}     — Season number (zero-padded)\n"
                "{episode}    — Episode number (zero-padded)\n"
                "{title}      — Episode title\n"
                "{year}       — Show premiere / release year\n"
                "{tvdb}       — TVDB ID (e.g. tvdb-475560)\n"
                "{tmdb}       — TMDB ID (e.g. tmdb-12345)"
            )
            vars_col2 = (
                "{tvdb_ep}    — Episode ID (e.g. tvdb-127396)\n"
                "{tmdb_ep}    — Episode ID (e.g. tmdb-62085)\n"
                "{resolution} — Auto-detected (e.g. 1080p)\n"
                "{vcodec}     — Auto-detected (e.g. x265)\n"
                "{acodec}     — Auto-detected (e.g. AAC)\n"
                "{source}     — From filename (e.g. BluRay)\n"
                "{hdr}        — Auto-detected (e.g. HDR10)"
            )
            vars_frame = ttk.Frame(f)
            vars_frame.grid(row=3, column=0, columnspan=2, sticky='ew',
                            padx=(15, 0))
            _dlg_bg = f.winfo_toplevel().cget('bg')
            vars_box_l = tk.Text(vars_frame, font=('Courier', 10), height=7,
                                  width=42, wrap='none', relief='flat',
                                  bg=_dlg_bg, cursor='arrow')
            vars_box_l.insert('1.0', vars_col1)
            vars_box_l.configure(state='disabled')
            vars_box_l.pack(side='left', anchor='nw', padx=(0, 12))
            vars_box_r = tk.Text(vars_frame, font=('Courier', 10), height=7,
                                  width=42, wrap='none', relief='flat',
                                  bg=_dlg_bg, cursor='arrow')
            vars_box_r.insert('1.0', vars_col2)
            vars_box_r.configure(state='disabled')
            vars_box_r.pack(side='left', anchor='nw')

            ttk.Label(f, text="Use / to create folders automatically.",
                      foreground='gray',
                      font=('Helvetica', 9)).grid(
                          row=4, column=0, columnspan=2, sticky='w',
                          padx=(15, 0), pady=(2, 0))

            def _vars_copy(event=None):
                widget = event.widget if event else None
                for vb in (vars_box_l, vars_box_r):
                    try:
                        sel = vb.get('sel.first', 'sel.last')
                        if sel:
                            vb.clipboard_clear()
                            vb.clipboard_append(sel)
                            return
                    except tk.TclError:
                        pass
            vars_ctx = tk.Menu(vars_frame, tearoff=0)
            vars_ctx.add_command(label="Copy", command=_vars_copy)
            for vars_box in (vars_box_l, vars_box_r):
                vars_box.bind('<Button-1>', lambda e: e.widget.focus_set())
                vars_box.bind('<ButtonPress-3>',
                              lambda e: vars_ctx.tk_popup(e.x_root, e.y_root))
                vars_box.bind('<Control-c>', _vars_copy)

            # ── Presets: TV (left) and Movie (right) side by side ──
            presets_frame = ttk.Frame(f)
            presets_frame.grid(row=5, column=0, columnspan=2, sticky='ew',
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

            # Force initial layout and scrollbar state after content is built
            dlg.update_idletasks()
            _update_scrollbar()
            dlg.wait_window()

        settings_menu.add_command(label="Filename Template...",
                                  command=_open_template_settings)

        # ── Template Wizard ──
        def _open_template_wizard():
            wiz = tk.Toplevel(win)
            wiz.withdraw()
            wiz.title("Template Wizard")
            # Restore saved size if available, otherwise use default
            saved_geo = getattr(app, '_wizard_geometry', None)
            if saved_geo:
                wiz.geometry(saved_geo)
            else:
                wiz.geometry(scaled_geometry(wiz, 620, 540))
            wiz.minsize(*scaled_minsize(wiz, 540, 480))
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
            _tv_year = tk.StringVar(value='none')        # none, filename, folder, both
            _tv_year_style = tk.StringVar(value='paren') # paren=(2008), bare=2008
            _episode_id = tk.BooleanVar(value=False)     # include episode ID in filename

            # Navigation buttons — packed at bottom first
            nav_frame = ttk.Frame(wiz, padding=(16, 8, 16, 12))
            nav_frame.pack(side='bottom', fill='x')

            back_btn = ttk.Button(nav_frame, text="< Back", width=8)
            back_btn.pack(side='left')
            cancel_btn = ttk.Button(nav_frame, text="Cancel", width=8)
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
                    tv_yr = _tv_year.get()
                    yr_in_name = tv_yr in ('filename', 'both')
                    if _tv_year_style.get() == 'paren':
                        yr_tag = ' ({year})'
                    else:
                        yr_tag = ' {year}'
                    show_with_yr = '{show}' + yr_tag if yr_in_name else '{show}'
                    if style == 'compact':
                        name_part = f'{show_with_yr} S{{season}}E{{episode}} {{title}}'
                    elif style == 'dashes':
                        name_part = f'{show_with_yr} - S{{season}}E{{episode}} - {{title}}'
                    elif style == 'classic':
                        name_part = f'{show_with_yr} {{season}}x{{episode}} {{title}}'
                    else:  # classic_dashes
                        name_part = f'{show_with_yr} - {{season}}x{{episode}} - {{title}}'

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

                # Append episode ID to filename if requested (TV only)
                if prov != 'none' and _episode_id.get() and not is_movie:
                    ep_id_tag = '{{{tvdb_ep}}}' if prov == 'tvdb' else '{{{tmdb_ep}}}'
                    name_part = f'{name_part} {ep_id_tag}'

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
                    tv_yr = _tv_year.get()
                    yr_in_folder = tv_yr in ('folder', 'both')
                    if yr_in_folder:
                        if _tv_year_style.get() == 'paren':
                            show_base = '{show} ({year})'
                        else:
                            show_base = '{show} {year}'
                    else:
                        show_base = '{show}'

                    if prov != 'none' and prov_loc in ('folder', 'both'):
                        id_tag = '{{{tvdb}}}' if prov == 'tvdb' else '{{{tmdb}}}'
                        show_dir = show_base + ' ' + id_tag
                    else:
                        show_dir = show_base

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
                        tvdb_ep='tvdb-349232', tmdb_ep='tmdb-62085',
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
                        _prov_location, _episode_id,
                        _extra_resolution, _extra_vcodec,
                        _extra_acodec, _extra_source, _extra_hdr, _extra_custom,
                        _tv_year, _tv_year_style):
                var.trace_add('write', _update_preview)

            # ── Step definitions ──
            steps_tv = ['type', 'style', 'year', 'folders', 'provider', 'extras', 'confirm']
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
                another_btn.pack_forget()
                cancel_btn.configure(text="Cancel")
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

                elif step_name == 'year':
                    title_label.configure(text="Include the year?")
                    ttk.Label(step_frame,
                               text="Add the show's premiere year to filenames\n"
                               "and/or folder names (e.g. 2008 from TVDB/TMDB).",
                               foreground='gray',
                               font=('Helvetica', 9)).pack(anchor='w', padx=10, pady=(0, 8))

                    ttk.Radiobutton(step_frame, text="No year",
                                     variable=_tv_year, value='none').pack(
                                         anchor='w', pady=4, padx=10)
                    ttk.Radiobutton(step_frame,
                                     text="In the filename only  (e.g. Show (2008) S01E01 Title)",
                                     variable=_tv_year, value='filename').pack(
                                         anchor='w', pady=4, padx=10)
                    ttk.Radiobutton(step_frame,
                                     text="In the folder name only  (e.g. Show (2008)/Season 01/...)",
                                     variable=_tv_year, value='folder').pack(
                                         anchor='w', pady=4, padx=10)
                    ttk.Radiobutton(step_frame,
                                     text="Both  (folder and filename)",
                                     variable=_tv_year, value='both').pack(
                                         anchor='w', pady=4, padx=10)

                    # Year format sub-option
                    fmt_frame = ttk.LabelFrame(step_frame, text="Year format",
                                                padding=6)
                    fmt_frame.pack(anchor='w', fill='x', padx=10, pady=(10, 4))
                    ttk.Radiobutton(fmt_frame, text="Parenthesized — Show (2008)",
                                     variable=_tv_year_style, value='paren').pack(
                                         anchor='w', pady=2)
                    ttk.Radiobutton(fmt_frame, text="Plain — Show 2008",
                                     variable=_tv_year_style, value='bare').pack(
                                         anchor='w', pady=2)

                    def _toggle_year_fmt(*_):
                        state = 'disabled' if _tv_year.get() == 'none' else 'normal'
                        for child in fmt_frame.winfo_children():
                            if isinstance(child, ttk.Radiobutton):
                                child.configure(state=state)
                    _tv_year.trace_add('write', _toggle_year_fmt)
                    _toggle_year_fmt()

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

                    # Episode ID checkbox (TV shows only)
                    is_tv = _type.get() != 'movie'
                    ep_id_cb = ttk.Checkbutton(
                        step_frame,
                        text="Also include episode ID in filename",
                        variable=_episode_id)
                    if is_tv:
                        ep_id_cb.pack(anchor='w', padx=10, pady=(10, 0))
                        ttk.Label(step_frame,
                                   text="e.g. ...Pilot {tvdb-349232}.mkv  — unique per episode",
                                   foreground='gray',
                                   font=('Helvetica', 8)).pack(
                                       anchor='w', padx=30, pady=(0, 4))

                    def _toggle_loc_frame(*_):
                        no_prov = _provider.get() == 'none'
                        if no_prov:
                            for child in loc_frame.winfo_children():
                                if isinstance(child, ttk.Radiobutton):
                                    child.configure(state='disabled')
                            ep_id_cb.configure(state='disabled')
                        else:
                            for child in loc_frame.winfo_children():
                                if isinstance(child, ttk.Radiobutton):
                                    if child in (folder_rb, both_rb) and not has_folders:
                                        child.configure(state='disabled')
                                    else:
                                        child.configure(state='normal')
                            ep_id_cb.configure(state='normal')
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

                    _tag_vars = [_extra_resolution, _extra_vcodec,
                                  _extra_acodec, _extra_source, _extra_hdr]

                    def _check_all_tags():
                        for v in _tag_vars:
                            v.set(True)
                    def _uncheck_all_tags():
                        for v in _tag_vars:
                            v.set(False)

                    btn_row = ttk.Frame(grid_f)
                    btn_row.pack(fill='x', pady=(0, 6))
                    ttk.Button(btn_row, text="Check All",
                               command=_check_all_tags).pack(side='left', padx=(0, 4))
                    ttk.Button(btn_row, text="Uncheck All",
                               command=_uncheck_all_tags).pack(side='left')

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

            # Message for the done step (set by _apply / _save_and_apply)
            _done_message = ['']

            # "Build Another" button — shown only on the done step
            another_btn = ttk.Button(nav_frame, text="Build Another", width=14)

            def _close_wizard():
                """Save wizard window geometry and destroy."""
                try:
                    geo = wiz.geometry()
                    if geo != getattr(app, '_wizard_geometry', ''):
                        app._wizard_geometry = geo
                        app.save_preferences()
                except Exception:
                    pass
                wiz.destroy()

            def _show_done(message):
                """Show the done step with a success message and
                Build Another / Done buttons."""
                _done_message[0] = message
                # Clear step content
                for w in step_frame.winfo_children():
                    w.destroy()
                # Hide normal nav buttons, show done buttons
                back_btn.configure(state='disabled')
                next_btn.pack_forget()
                apply_btn.pack_forget()
                save_btn.pack_forget()
                another_btn.pack_forget()
                cancel_btn.pack_forget()
                # Pack done-step buttons: Done on far right, Build Another next
                cancel_btn.configure(text="Done")
                cancel_btn.pack(side='right', padx=(4, 0))
                another_btn.pack(side='right', padx=(4, 0))

                step_label.configure(text="")
                title_label.configure(text="Template applied!")
                ttk.Label(step_frame,
                           text=message,
                           font=('Helvetica', 10)).pack(
                               anchor='w', pady=4, padx=10)

            def _restart_wizard():
                """Reset the wizard to step 1 to build another template."""
                # Reset state variables to defaults
                _step[0] = 0
                _type.set('tv')
                _style.set('compact')
                _mv_style.set('year_paren')
                _folders.set('flat')
                _provider.set('none')
                _prov_location.set('folder')
                _extra_resolution.set(False)
                _extra_vcodec.set(False)
                _extra_acodec.set(False)
                _extra_source.set(False)
                _extra_hdr.set(False)
                _extra_custom.set('')
                _tv_year.set('none')
                _tv_year_style.set('paren')
                _episode_id.set(False)
                # Restore nav button labels and layout
                cancel_btn.configure(text="Cancel")
                another_btn.pack_forget()
                _show_step()

            def _apply():
                tmpl = _build_template()
                _switch_provider_if_needed()
                kind = 'Movie' if _type.get() == 'movie' else 'TV'
                if _type.get() == 'movie':
                    movie_template_var.set(tmpl)
                else:
                    template_var.set(tmpl)
                _log(f"Wizard: applied {kind} template: {tmpl}")
                _show_done(f"{kind} template applied:\n{tmpl}\n\n"
                           f"Build another template or click Done to close.")

            def _save_and_apply():
                tmpl = _build_template()
                _switch_provider_if_needed()
                is_movie = _type.get() == 'movie'
                kind = 'Movie' if is_movie else 'TV'
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
                _log(f"Wizard: saved and applied {kind} template: {tmpl}")
                _show_done(f"{kind} template saved and applied:\n{tmpl}\n\n"
                           f"Build another template or click Done to close.")

            next_btn.configure(command=_next)
            back_btn.configure(command=_back)
            cancel_btn.configure(command=_close_wizard)
            apply_btn.configure(command=_apply)
            save_btn.configure(command=_save_and_apply)
            another_btn.configure(command=_restart_wizard)
            wiz.protocol('WM_DELETE_WINDOW', _close_wizard)

            # Show first step
            _show_step()

            # Center and show — skip centering if restoring saved geometry
            wiz.update_idletasks()
            if not saved_geo:
                _center_on_parent(wiz, win)
            wiz.deiconify()

        settings_menu.add_command(label="Template Wizard...",
                                  command=_open_template_wizard)

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
