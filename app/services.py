# /gamearr/app/services.py

# --- Standard Library Imports ---
import os
import PTN
import re
import json
import time
from datetime import datetime
import urllib.parse
import subprocess
import sys
import html

# --- Third-Party Library Imports ---
import requests
import praw
import feedparser
from qbittorrentapi import Client, exceptions
from flask import current_app
import xml.etree.ElementTree as ET
import unicodedata
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter

# --- Local Application Imports ---
from . import db
from .models import Game, Setting, DiscoverCache, AlternativeRelease, AdditionalRelease

# --- NEW: Global cache for the IGDB token ---
_igdb_access_token = None
_igdb_token_expires = 0

def search_igdb(search_term):
    """
    Performs a LIGHTWEIGHT search on IGDB, fetching only the data
    needed for the search results page.
    """
    try:
        # --- REFACTORED ---
        headers = _get_igdb_headers()
        
        igdb_url = 'https://api.igdb.com/v4/games'
        query_body = f'search "{search_term}"; fields name, cover.url, first_release_date, slug; limit 20;'
        
        response = requests.post(igdb_url, headers=headers, data=query_body, timeout=10)
        response.raise_for_status()
        
        results = response.json()
        cleaned_results = [
            {
                'id': game.get('id'),
                'name': game.get('name'),
                'slug': game.get('slug'),
                'cover_url': game.get('cover', {}).get('url', '').replace('t_thumb', 't_cover_big'),
                'release_timestamp': game.get('first_release_date')
            } for game in results
        ]
        return cleaned_results
    except Exception as e:
        current_app.logger.error(f"Error searching IGDB for '{search_term}': {e}")
        return []

def get_igdb_game_details(igdb_id):
    """
    Fetches the full, rich dataset for a SINGLE game from IGDB using its ID.
    """
    try:
        headers = _get_igdb_headers()
        
        igdb_url = 'https://api.igdb.com/v4/games'
        
        query_body = (
            f'fields name, slug, cover.url, first_release_date, summary, genres.name, '
            f'aggregated_rating, rating, screenshots.url, videos.video_id; '
            f'where id = {igdb_id};'
        )
        
        response = requests.post(igdb_url, headers=headers, data=query_body, timeout=10)
        response.raise_for_status()
        
        results = response.json()
        if not results:
            return None 

        game = results[0]
        
        game_details = {
            'igdb_id': game.get('id'),
            'official_title': game.get('name'),
            'slug': game.get('slug'),
            'summary': game.get('summary'),
            'genres': ", ".join([g['name'] for g in game.get('genres', [])]),
            'critic_score': int(round(game.get('aggregated_rating', 0))),
            'user_score': int(round(game.get('rating', 0))),
            'screenshots_urls': ",".join([s['url'].replace('t_thumb', 't_screenshot_med') for s in game.get('screenshots', [])]),
            'videos_urls': ",".join([f"https://www.youtube.com/watch?v={v['video_id']}" for v in game.get('videos', [])]),
            'cover_url': game.get('cover', {}).get('url', '').replace('t_thumb', 't_cover_big'),
            'release_date': current_app.jinja_env.filters['timestamp_to_date'](game.get('first_release_date'))
        }
        return game_details
    except Exception as e:
        current_app.logger.error(f"Error getting IGDB details for ID {igdb_id}: {e}")
        return None

def _format_bytes(size_in_bytes):
    """Formats bytes into a human-readable string (KB, MB, GB)."""
    if size_in_bytes is None:
        return "N/A"
    size = float(size_in_bytes)
    power = 1024
    n = 0
    power_labels = {0: '', 1: 'KB', 2: 'MB', 3: 'GB', 4: 'TB'}
    while size > power and n < len(power_labels) -1 :
        size /= power
        n += 1
    return f"{size:.2f} {power_labels[n]}"

def _safe_timestamp_convert(ts):
    """Safely converts a value to an integer timestamp."""
    if not ts:
        return None
    try:
        return int(ts)
    except (ValueError, TypeError):
        return None

def _search_predb_net(search_term):
    """Helper to search the predb.net API."""
    current_app.logger.info(f"    -> Checking predb.net for '{search_term}'...")
    try:
        safe_search = search_term.replace(':', '')
        params = {'type': 'search', 'q': safe_search, 'section': 'GAMES', 'sort': 'DESC'}
        response = requests.get("https://api.predb.net/", params=params, timeout=10)
        response.raise_for_status()
        results = response.json().get('data', [])
        current_app.logger.info(f"       --> Found {len(results)} releases on predb.net.")
        return [
            {'release': r.get('release'), 'group': r.get('group'), 'timestamp': _safe_timestamp_convert(r.get('pretime'))}
            for r in results if r.get('release')
        ]
    except Exception as e:
        current_app.logger.error(f"    -> ERROR checking predb.net: {e}")
        return []

def _search_predb_club(search_term):
    """Helper to search the predb.club API."""
    current_app.logger.info(f"    -> Checking predb.club for '{search_term}'...")
    try:
        filtered_search = f"{search_term} @cat GAMES"
        url = f"https://predb.club/api/v1/?q={urllib.parse.quote_plus(filtered_search)}"
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        json_data = response.json()
        if json_data.get('status') != 'success': return []
        results = json_data.get('data', {}).get('rows', [])
        current_app.logger.info(f"       --> Found {len(results)} releases on predb.club.")
        return [
            {'release': r.get('name'), 'group': r.get('team'), 'timestamp': _safe_timestamp_convert(r.get('preAt'))}
            for r in results if r.get('name')
        ]
    except Exception as e:
        current_app.logger.error(f"    -> ERROR checking predb.club: {e}")
        return []

def _calculate_title_similarity(game_title, release_name):
    """Calculate similarity score between game title and release name."""
    
    # First replace dots, underscores, and dashes with spaces, THEN remove other punctuation
    normalized_game = re.sub(r'[._-]', ' ', game_title)
    normalized_release = re.sub(r'[._-]', ' ', release_name)
    
    # Now remove remaining punctuation and split into words
    game_words = set(re.sub(r'[^\w\s]', '', normalized_game.lower()).split())
    release_words = set(re.sub(r'[^\w\s]', '', normalized_release.lower()).split())
    
    # Remove common gaming words that don't affect matching
    common_words = {'repack', 'multi', 'multilingual', 'campaign', 'kampagne', 
                   'complete', 'edition', 'goty', 'deluxe', 'ultimate', 'directors',
                   'cut', 'enhanced', 'definitive', 'special', 'collectors', 
                   '2022', '2023', '2024', '2025', 'multi2', 'multi8', 'multi4',
                   'xx', 'x'}
    release_words -= common_words
    
    # Calculate overlap score
    if not game_words or not release_words:
        return 0
    
    overlap = len(game_words.intersection(release_words))
    return overlap / len(game_words)

def _refine_search_term(raw_release_name):
    """
    Cleans a raw release name into a standardized search term by replacing
    common separators with spaces.
    Example: 'METAL_GEAR_SOLID_DELTA_SNAKE_EATER-FLT' -> 'METAL GEAR SOLID DELTA SNAKE EATER-FLT'
    """
    if not raw_release_name:
        return ""
    
    # Replace common separators (underscores, periods) with spaces
    cleaned = raw_release_name.replace('.', ' ').replace('_', ' ')
    
    # Collapse multiple spaces into one and trim whitespace from the ends
    return re.sub(r'\s+', ' ', cleaned).strip()

def _is_valid_game_match(game_title, release_name, min_similarity=0.7):
    """Check if release name is a valid match for the game title."""
    similarity = _calculate_title_similarity(game_title, release_name)
    
    # Special handling for numbered sequels (II, III, IV, etc.)
    game_roman = re.search(r'\b(II|III|IV|V|VI|VII|VIII|IX|X)\b', game_title)
    release_roman = re.search(r'\b(II|III|IV|V|VI|VII|VIII|IX|X)\b', release_name)
    
    if game_roman and release_roman:
        # For numbered sequels, roman numerals must match exactly
        if game_roman.group(1) != release_roman.group(1):
            return False
    elif game_roman and not release_roman:
        # Game has roman numeral but release doesn't - likely not a match
        return False
        
    return similarity >= min_similarity

def check_source_xrel(search_term):
    """Checks the xrel.to API for releases."""
    current_app.logger.info(f"    -> Checking xrel.to for '{search_term}'...")
    found_releases = []
    try:
        safe_search = search_term.replace(':', '')
        url = f"https://api.xrel.to/v2/search/releases.xml?q={urllib.parse.quote_plus(safe_search)}&scene=1&p2p=1"
        response = requests.get(url, timeout=15)
        response.raise_for_status()
        root = ET.fromstring(response.content)
        for rls in root.findall('.//rls') + root.findall('.//p2p_rls'):
            dirname = rls.find('dirname')
            group = rls.find('.//group/name')
            pub_time = rls.find('pub_time')
            ts = _safe_timestamp_convert(pub_time.text if pub_time is not None else None)
            if dirname is not None and group is not None and ts:
                found_releases.append({'release': dirname.text, 'group': group.text, 'timestamp': ts})
        current_app.logger.info(f"       --> Found {len(found_releases)} releases on xrel.to.")
        return found_releases
    except Exception as e:
        current_app.logger.error(f"    -> ERROR xrel.to: {e}")
        return []
        
def check_source_fitgirl(game_title):
    """Checks the FitGirl site for a repack of a specific game."""
    try:
        current_app.logger.info(f"    -> Checking FitGirl for '{game_title}'...")
        base_url = "https://fitgirl-repacks.site/"
        encoded_search = urllib.parse.quote_plus(game_title)
        
        response = requests.get(f"{base_url}?s={encoded_search}", timeout=15)
        response.raise_for_status()

        if "Sorry, but nothing matched your search terms." in response.text:
            return None
        
        matches = re.findall(r'<h1 class="entry-title"><a href=".+?" rel="bookmark">(.+?)</a></h1>', response.text)
        
        for found_title in matches:
            # Use the improved matching logic instead of simple substring check
            if _is_valid_game_match(game_title, found_title, min_similarity=0.8):
                current_app.logger.info(f"       --> Found FitGirl match: {found_title}")
                return found_title
        
        current_app.logger.info(f"       --> No valid FitGirl matches found after similarity check")
        return None
    except Exception as e:
        current_app.logger.error(f"    -> ERROR: FitGirl check failed: {e}")
        return None

def search_jackett(game_title):
    """
    Searches Jackett and parses the Torznab feed.
    """
    settings = get_settings_dict()
    if not all([settings.get('jackett_url'), settings.get('jackett_api_key')]):
        current_app.logger.error("Jackett search: Settings are incomplete.")
        return []

    indexers = settings.get('jackett_indexers', 'all').replace(',', ';') or 'all'
    url = (
        f"{settings['jackett_url']}/api/v2.0/indexers/{indexers}/results/torznab/"
        f"?apikey={settings['jackett_api_key']}&t=search&cat=4000&q={urllib.parse.quote_plus(game_title)}"
    )
    
    try:
        feed = feedparser.parse(url)
        results = []
        if not feed.entries:
            return []

        for item in feed.entries:
            try:
                link_obj = next((link for link in item.links if 'magnet:' in link.href or link.type == 'application/x-bittorrent'), None)
                if not link_obj:
                    continue
                
                download_link = link_obj.href
                size_in_bytes = item.get('size')
                grabs = int(item.get('grabs', 0))
                torznab_attr = item.get('torznab_attr', [])
                attributes = {}
                
                if isinstance(torznab_attr, dict):
                    if 'name' in torznab_attr and 'value' in torznab_attr:
                        attributes[torznab_attr['name']] = torznab_attr['value']
                elif isinstance(torznab_attr, list):
                    for attr in torznab_attr:
                        if isinstance(attr, dict):
                            name_key = '@name' if '@name' in attr else 'name'
                            value_key = '@value' if '@value' in attr else 'value'
                            if name_key in attr and value_key in attr:
                                attributes[attr[name_key]] = attr[value_key]

                def safe_int(value, default=0):
                    try: return int(value) if value is not None else default
                    except (ValueError, TypeError): return default

                seeders = 0
                for seed_key in ['seeders', 'seeds', 'seeder']:
                    if seed_key in attributes:
                        seeders = safe_int(attributes[seed_key]); break

                leechers, peers = 0, 0
                for leech_key in ['leechers', 'leeches', 'leech']:
                    if leech_key in attributes:
                        leechers = safe_int(attributes[leech_key]); break
                
                if leechers == 0:
                    for peer_key in ['peers', 'peer']:
                        if peer_key in attributes:
                            peers = safe_int(attributes[peer_key])
                            leechers = max(0, peers - seeders); break

                tracker_type = item.get('type', 'unknown')
                indexer_raw = item.get('jackettindexer', 'Unknown')
                indexer = indexer_raw.get('id', indexer_raw) if isinstance(indexer_raw, dict) else indexer_raw

                results.append({
                    'title': item.title, 'link': download_link, 'indexer': indexer,
                    'grabs': grabs,
                    'seeders': seeders if seeders > 0 or tracker_type != 'private' else -1,
                    'leechers': leechers if leechers > 0 or tracker_type != 'private' else -1,
                    'size': _format_bytes(size_in_bytes)
                })
            except (AttributeError, ValueError, TypeError, StopIteration, KeyError) as e:
                current_app.logger.error(f"Failed to parse a specific Jackett result for '{item.title}'. Error: {e}")
                continue
        
        return sorted(results, key=lambda x: (x['grabs'], x['seeders'] if x['seeders'] != -1 else 0), reverse=True)
    
    except Exception as e:
        current_app.logger.error(f"A critical error occurred while fetching the Jackett feed: {e}")
        return []

def get_settings_dict():
    """Helper function to get all settings as a dictionary."""
    try:
        settings = Setting.query.all()
        return {setting.key: setting.value for setting in settings}
    except Exception as e:
        current_app.logger.error(f"Error fetching settings: {e}")
        return {}

def get_qbit_client():
    """Helper function to get an authenticated qBittorrent client."""
    settings = get_settings_dict()
    if not all([settings.get(k) for k in ['qbittorrent_host', 'qbittorrent_port', 'qbittorrent_user', 'qbittorrent_pass']]):
        current_app.logger.error("qBittorrent credentials not configured.")
        return None
    try:
        client = Client(
            host=settings.get('qbittorrent_host'), port=settings.get('qbittorrent_port'),
            username=settings.get('qbittorrent_user'), password=settings.get('qbittorrent_pass')
        )
        client.auth_log_in()
        return client
    except Exception as e:
        current_app.logger.error(f"Error connecting to qBittorrent: {e}")
        return None

def add_to_qbittorrent(magnet_link):
    """Adds a magnet link to qBittorrent and returns the torrent hash."""
    settings = get_settings_dict()
    try:
        client = get_qbit_client()
        category = settings.get('qbittorrent_category')
        result = client.torrents_add(urls=magnet_link, category=category, tags="gamearr")

        if result != "Ok.":
            current_app.logger.error(f"Failed to add torrent to qBittorrent: {result}")
            return None
        time.sleep(2)
        added_torrents = client.torrents_info(tag="gamearr", category=category, sort='added_on', reverse=True)
        return added_torrents[0].hash if added_torrents else None
    except Exception as e:
        current_app.logger.error(f"An unexpected qBittorrent error occurred: {e}")
        return None

def check_source_rss(feed_url, game_title):
    try:
        feed = feedparser.parse(feed_url)
        if feed.entries:
            normalized_title = game_title.replace(':', '').lower()
            title_keywords = set(normalized_title.split())
            required_matches = max(2, int(len(title_keywords) * 0.75))
            for entry in feed.entries:
                cleaned_entry_title_set = set(entry.title.replace('.', ' ').replace('_', ' ').lower().split())
                matching_keywords = title_keywords.intersection(cleaned_entry_title_set)
                if len(matching_keywords) >= required_matches:
                    current_app.logger.info(f"        --> Fuzzy match found! Matched {len(matching_keywords)}/{len(title_keywords)} keywords.")
                    return entry.title
    except Exception as e:
        current_app.logger.info(f"    ERROR: RSS feed check for '{feed_url}' failed: {e}")
    return None

def process_all_releases_for_game(game_id):
    """
    The single, unified engine to find, categorize, and process ALL releases 
    (base games, add-ons, etc.) for a given game.
    """
    game = Game.query.get(game_id)
    if not game:
        current_app.logger.error(f"Unified engine called with invalid game_id: {game_id}")
        return

    current_app.logger.info(f"--> Starting UNIFIED release scan for '{game.official_title}' (Current Status: {game.status})")

    # --- ENGINE CONSTANTS ---
    MOVIE_KEYWORDS = {'BDRIP', 'BLURAY', 'HDTV', 'X264', 'X265', 'DTSHD', 'DVDRIP'}
    PLATFORM_KEYWORDS = {'MACOS', 'LINUX', 'NSW', 'PS4', 'PS5', 'XBOX', 'WII', 'NGC', 'PS2DVD'}
    TYPE_BLOCKED_KEYWORDS = {'UPDATE', 'DLC', 'PATCH', 'CRACKFIX', 'TRAINER'}
    REPACK_GROUPS = {'FITGIRL', 'DODI', 'ELAMIGOS', 'KAOSKREW', 'MASQUERADE'}
    TIER_ORDER = {'Scene': 2, 'Repack': 1, 'P2P': 0}

    # --- HELPER FUNCTIONS ---
    def _calculate_relevancy_score(game_release_date, release_timestamp):
        if not game_release_date or not release_timestamp: return 0
        try:
            game_date = datetime.strptime(game_release_date, '%Y-%m-%d')
            release_date = datetime.fromtimestamp(int(release_timestamp))
            delta_days = (release_date - game_date).days
            
            if delta_days < -30: return -1000
            if delta_days <= 90:    return 200
            if delta_days <= 365:   return 150
            if delta_days <= 730:   return 100
            if delta_days <= 1095:  return 50
            if delta_days <= 1460:  return 25
            return -1000
        except Exception:
            return 0
    
    def _detect_type(release_name, original_type):
        if any(rg in release_name.upper() for rg in REPACK_GROUPS):
            return 'Repack'
        return original_type

    # --- NEW, MORE ROBUST MATCHING FUNCTION ---
    def _is_valid_game_match(official_title, candidate_name):
        """
        Compares two strings by simplifying them to a common ASCII format.
        This correctly handles special characters like 'ö' vs 'o'.
        """
        def _simplify_text(text):
            # Convert to lowercase
            text = text.lower()
            # Replace common separators with spaces
            text = re.sub(r'[_.:-]', ' ', text)
            # Normalize Unicode characters to their base ASCII form (e.g., ö -> o)
            text = ''.join(c for c in unicodedata.normalize('NFD', text) if unicodedata.category(c) != 'Mn')
            # Remove any remaining non-alphanumeric characters (except spaces)
            text = re.sub(r'[^\w\s]', '', text)
            # Collapse multiple spaces
            return re.sub(r'\s+', ' ', text).strip()

        simplified_title = _simplify_text(official_title)
        simplified_candidate = _simplify_text(candidate_name)

        return simplified_title in simplified_candidate

    # --- STEP 1: GATHER EVERYTHING FROM ALL SOURCES ---
    all_releases = {}
    for r in _search_predb_club(game.official_title):
        if r['release']: all_releases[r['release']] = {'source': r['group'] or 'Scene', 'type': 'Scene', 'timestamp': r['timestamp']}
    for r in _search_predb_net(game.official_title):
        if r['release']: all_releases.setdefault(r['release'], {'source': r['group'] or 'Scene', 'type': 'Scene', 'timestamp': r['timestamp']})
    for r in check_source_xrel(game.official_title):
        if r['release']: all_releases.setdefault(r['release'], {'source': r['group'] or 'P2P', 'type': 'P2P', 'timestamp': r['timestamp']})
    
    fitgirl_release = check_source_fitgirl(game.official_title)
    if fitgirl_release:
        all_releases.setdefault(fitgirl_release, {'source': 'FitGirl', 'type': 'Repack', 'timestamp': int(time.time())})
    
    if not all_releases:
        current_app.logger.info(f"    -> No potential releases found for '{game.official_title}'.")
        if game.status == 'Processing': game.status = 'Monitoring'
        db.session.commit()
        return

    # --- STEP 2: CATEGORIZE & FILTER ---
    current_app.logger.info(f"    -> Found {len(all_releases)} total releases. Categorizing and filtering...")
    base_game_candidates = []
    add_ons = []

    for name, data in all_releases.items():
        clean_name = html.unescape(name)
        upper_words = set(clean_name.upper().replace('.', ' ').replace('_', ' ').replace('-', ' ').split())
        
        if not TYPE_BLOCKED_KEYWORDS.isdisjoint(upper_words):
            add_ons.append({'release_name': clean_name, **data})
            continue

        if not MOVIE_KEYWORDS.isdisjoint(upper_words):
            continue
        
        # --- USE THE NEW, ROBUST MATCHER ---
        if not _is_valid_game_match(game.official_title, clean_name):
            continue
            
        data['score'] = _calculate_relevancy_score(game.release_date, data.get('timestamp'))
        if data['score'] < 0:
            continue
            
        data['release_name'] = clean_name
        data['type'] = _detect_type(clean_name, data.get('type', 'P2P'))
        data['is_pc'] = PLATFORM_KEYWORDS.isdisjoint(upper_words)
        base_game_candidates.append(data)

    # --- STEP 3: PROCESS BASE GAMES (with imported game fix) ---
    if not base_game_candidates:
        current_app.logger.info(f"    -> No valid BASE GAME releases found after filtering.")
    else:
        sorted_games = sorted(
            base_game_candidates,
            key=lambda item: (item['is_pc'], item['score'], TIER_ORDER.get(item['type'], 0)),
            reverse=True
        )
        
        if game.status == 'Imported':
            current_app.logger.info(f"    -> Game is 'Imported'. Preserving status, scanning for new alternative releases.")
            existing_alternatives = {r.release_name for r in game.alternative_releases}
            new_alt_count = 0
            for alt_data in sorted_games:
                if alt_data['release_name'] not in existing_alternatives:
                    nfo_path, nfo_img_path = fetch_and_save_nfo(alt_data['release_name'])
                    alt_release = AlternativeRelease(
                        release_name=alt_data['release_name'], source=alt_data['source'], game_id=game.id,
                        nfo_path=nfo_path, nfo_img_path=nfo_img_path
                    )
                    db.session.add(alt_release)
                    new_alt_count += 1
                    time.sleep(1)
            if new_alt_count > 0:
                 current_app.logger.info(f"    -> Saved {new_alt_count} new alternative releases.")
        else:
            primary_release = sorted_games[0]
            alternative_releases = sorted_games[1:]

            game.release_name = primary_release['release_name']
            game.release_group = primary_release['source']
            game.release_type = primary_release['type']
            
            if primary_release['type'] == 'Scene':
                game.status = 'Cracked (Scene)'
            else: # Covers 'Repack' and 'P2P'
                game.status = 'Cracked (P2P)'
            
            game.nfo_path, game.nfo_img_path = fetch_and_save_nfo(primary_release['release_name'])
            current_app.logger.info(f"    SUCCESS! Assigning primary release: '{game.release_name}'")

            AlternativeRelease.query.filter_by(game_id=game.id).delete()
            for alt_data in alternative_releases:
                nfo_path, nfo_img_path = fetch_and_save_nfo(alt_data['release_name'])
                alt_release = AlternativeRelease(
                    release_name=alt_data['release_name'], source=alt_data['source'], game_id=game.id,
                    nfo_path=nfo_path, nfo_img_path=nfo_img_path
                )
                db.session.add(alt_release)
                time.sleep(1) # <--- AND ADD POLITE DELAY HERE
            current_app.logger.info(f"    -> Saved {len(alternative_releases)} alternative releases.")

    # --- STEP 4: PROCESS ADD-ONS ---
    if add_ons:
        existing_add_ons = {r.release_name for r in AdditionalRelease.query.filter_by(game_id=game.id).all()}
        new_add_on_count = 0
        for item in add_ons:
            if item['release_name'] not in existing_add_ons:
                release_type = parse_additional_release_info(item['release_name'])
                if release_type:
                    new_add_on = AdditionalRelease(
                        release_name=item['release_name'], release_type=release_type, 
                        source=item.get('source'), game_id=game.id
                    )
                    db.session.add(new_add_on)
                    new_add_on_count += 1
        if new_add_on_count > 0:
            current_app.logger.info(f"    -> Saved {new_add_on_count} new add-on releases.")
    
    game.needs_release_check = False
    db.session.commit()
    current_app.logger.info(f"--> Finished UNIFIED release scan for '{game.official_title}'")

def fetch_and_save_nfo(release_name):
    """
    Fetches NFO data and image, now with retry logic for network resilience.
    """
    current_app.logger.info(f"NFO Fetcher: Attempting to fetch NFO for '{release_name}'")

    # Setup a requests session with a retry strategy
    retry_strategy = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    http = requests.Session()
    http.mount("https://", adapter)

    try:
        url = "https://api.predb.net/"
        params = {'type': 'nfo', 'release': release_name}
        
        response = http.get(url, params=params, timeout=15)
        response.raise_for_status()
        json_data = response.json()
        nfo_data_dict = json_data.get('data')

        if not nfo_data_dict or not isinstance(nfo_data_dict, dict):
            current_app.logger.info(f"NFO Fetcher: No 'data' object found for '{release_name}'.")
            return None, None
        
        nfo_url = nfo_data_dict.get('nfo')
        nfo_img_url = nfo_data_dict.get('nfo_img')
        nfo_storage_path = os.path.join(current_app.root_path, 'nfo_storage')
        os.makedirs(nfo_storage_path, exist_ok=True)
        
        local_nfo_path, local_nfo_img_path = None, None

        if nfo_url:
            try:
                nfo_response = http.get(nfo_url, timeout=15)
                if nfo_response.status_code == 200:
                    safe_filename = "".join(c for c in release_name if c.isalnum() or c in ('_', '-')).rstrip()
                    local_nfo_path = os.path.join(nfo_storage_path, f"{safe_filename}.nfo")
                    with open(local_nfo_path, 'w', encoding='utf-8', errors='ignore') as f:
                        f.write(nfo_response.text)
            except Exception as e:
                current_app.logger.warning(f"NFO Fetcher: Could not download NFO file for '{release_name}': {e}")

        if nfo_img_url:
            try:
                nfo_img_response = http.get(nfo_img_url, timeout=15)
                if nfo_img_response.status_code == 200:
                    safe_filename = "".join(c for c in release_name if c.isalnum() or c in ('_', '-')).rstrip()
                    local_nfo_img_path = os.path.join(nfo_storage_path, f"{safe_filename}.png")
                    with open(local_nfo_img_path, 'wb') as f:
                        f.write(nfo_img_response.content)
            except Exception as e:
                current_app.logger.warning(f"NFO Fetcher: Could not download NFO image for '{release_name}': {e}")
        
        db_nfo_path = os.path.relpath(local_nfo_path, nfo_storage_path) if local_nfo_path else None
        db_nfo_img_path = os.path.relpath(local_nfo_img_path, nfo_storage_path) if local_nfo_img_path else None
        
        return db_nfo_path, db_nfo_img_path
    except Exception as e:
        current_app.logger.warning(f"NFO Fetcher: A non-retryable error occurred for '{release_name}': {e}")
        return None, None

def parse_additional_release_info(release_name):
    """
    Takes a raw release name and identifies if it is a VALID additional release.
    """
    name_upper = release_name.upper().replace('.', ' ').replace('_', ' ').replace('-', ' ')
    PLATFORM_EXCLUSIONS = {'NSW', 'LINUX', 'MACOS', 'PS5', 'PS4', 'XBOX'}
    release_keyword_set = set(name_upper.split())
    if any(platform in release_keyword_set for platform in PLATFORM_EXCLUSIONS):
        return None

    type_map = {'CRACKFIX': 'Fix', 'DLC': 'DLC', 'UPDATE': 'Update', 'PATCH': 'Update', 'FIX': 'Fix', 'TRAINER': 'Trainer'}
    for keyword, type_name in type_map.items():
        if f' {keyword} ' in name_upper or name_upper.endswith(f' {keyword}'):
            return type_name
    return None

def scan_library_folder():
    """
    Scans the library path for folders that aren't already in the database.
    """
    library_path = current_app.config['LIBRARY_PATH']
    current_app.logger.info(f"--- Starting Library Scan on Path: '{library_path}' ---")
    if not os.path.isdir(library_path):
        current_app.logger.error(f"Library path not found or is not a directory.")
        return []

    IGNORE_FOLDERS = {'_downloads', '@eaDir'}
    existing_folders = {game.local_path for game in Game.query.all() if game.local_path}
    found_folders = []
    with os.scandir(library_path) as it:
        for entry in it:
            if entry.is_dir() and entry.name not in existing_folders and entry.name not in IGNORE_FOLDERS:
                found_folders.append(entry.name)
    return found_folders

def process_library_scan():
    """
    The main service function for the library import feature.
    """
    untracked_folders = scan_library_folder()
    if not untracked_folders:
        return []

    all_games_in_db = Game.query.all()
    existing_title_keyword_sets = [set(_clean_release_name(game.official_title).split()) for game in all_games_in_db]
    results = []
    current_app.logger.info("--- Matching folders to IGDB (and checking for duplicates) ---")

    for folder in untracked_folders:
        guessed_title = _clean_release_name(folder)
        guessed_keywords = set(_clean_release_name(guessed_title).split())

        is_already_in_library = False
        for existing_keywords in existing_title_keyword_sets:
            if guessed_keywords.issubset(existing_keywords):
                is_already_in_library = True; break

        if is_already_in_library:
            current_app.logger.info(f"Folder: '{folder}'  --> Guessed Title: '{guessed_title}' [SKIPPING - Already in library]")
            continue

        current_app.logger.info(f"Folder: '{folder}'  --> Guessed Title: '{guessed_title}' [NEW - Searching IGDB]")
        igdb_matches = search_igdb(guessed_title)
        results.append({
            'folder_name': folder,
            'guessed_title': guessed_title,
            'igdb_matches': igdb_matches
        })
    return results

def _clean_release_name(release_name):
    """
    The single, definitive function for cleaning any release string.
    """
    if not isinstance(release_name, str): return ""
    cleaned = release_name.lower().replace('.', ' ').replace('_', ' ').replace('-', ' ')
    info = PTN.parse(cleaned)
    cleaned = info.get('title', cleaned)
    patterns_to_remove = [
        r'v\d+(\.\d+)*', r'\d{3,4}p', r'repack', r'multi\d*',
        r'flt', 'rune', 'codex', 'elamigos', 'p2p', 'tenoke',
    ]
    for pattern in patterns_to_remove:
        cleaned = re.sub(pattern, '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"[:'!,\[\]]", "", cleaned)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned

def _get_igdb_headers():
    """
    A private helper that gets and CACHES the required auth headers for any IGDB API call.
    """
    global _igdb_access_token, _igdb_token_expires

    if not _igdb_access_token or time.time() > (_igdb_token_expires - 60):
        current_app.logger.info("IGDB token is missing or expired. Requesting a new one...")
        settings = get_settings_dict()
        twitch_client_id = settings.get('twitch_client_id')
        twitch_client_secret = settings.get('twitch_client_secret')
        
        if not all([twitch_client_id, twitch_client_secret]):
            raise Exception("Twitch API credentials are not configured.")

        auth_url = 'https://id.twitch.tv/oauth2/token'
        auth_params = {'client_id': twitch_client_id, 'client_secret': twitch_client_secret, 'grant_type': 'client_credentials'}
        auth_response = requests.post(auth_url, params=auth_params, timeout=10)
        auth_response.raise_for_status()
        
        token_data = auth_response.json()
        _igdb_access_token = token_data['access_token']
        _igdb_token_expires = time.time() + token_data['expires_in']
        current_app.logger.info("Successfully obtained new IGDB token.")

    return {
        'Client-ID': get_settings_dict().get('twitch_client_id'),
        'Authorization': f'Bearer {_igdb_access_token}',
        'User-Agent': 'Gamearr/1.0 (Python/Requests)'
    }

def update_discover_lists():
    """
    Fetches and caches high-quality IGDB discover lists using a smarter filter for anticipated games.
    """
    current_app.logger.info("--- Starting Discover list update (using API v4) ---")
    try:
        headers = _get_igdb_headers()
        now_timestamp = int(time.time())

        popularity_api_url = "https://api.igdb.com/v4/popularity_primitives"
        games_api_url = "https://api.igdb.com/v4/games"
        
        CANDIDATE_LIMIT = 200 

        # --- "MOST ANTICIPATED" LOGIC (Final Version) ---
        anticipated_games = []
        try:
            current_app.logger.info("    -> Fetching 'Most Anticipated' list...")
            pop_query = f'fields game_id, value; where popularity_type = 2; sort value desc; limit {CANDIDATE_LIMIT};'
            pop_response = requests.post(popularity_api_url, headers=headers, data=pop_query, timeout=20)
            pop_response.raise_for_status()
            
            ids = [item['game_id'] for item in pop_response.json() if 'game_id' in item]
            current_app.logger.info(f"       - Found {len(ids)} potential anticipated game IDs.")

            if ids:
                ids_string = ",".join(map(str, ids))
                # --- THE FINAL FIX: Include games where platforms are unannounced ---
                details_query = (
                    f'fields name, cover.url, first_release_date, slug; '
                    f'where id = ({ids_string}) & first_release_date > {now_timestamp} & (platforms = (6) | platforms = null); limit {CANDIDATE_LIMIT};'
                )
                details_response = requests.post(games_api_url, headers=headers, data=details_query, timeout=20)
                details_response.raise_for_status()
                
                game_map = {game['id']: game for game in details_response.json()}
                anticipated_games = [game_map[gid] for gid in ids if gid in game_map][:12]
                current_app.logger.info(f"       - Built list with {len(anticipated_games)} final games.")
        except Exception as e:
            current_app.logger.error(f"    -> Failed to build 'Most Anticipated' list: {e}")

        # --- "POPULAR RIGHT NOW" LOGIC (No changes needed here) ---
        popular_now_games = []
        try:
            current_app.logger.info("    -> Fetching 'Popular Right Now' list...")
            pop_query = f'fields game_id, value; where popularity_type = 3; sort value desc; limit {CANDIDATE_LIMIT};'
            pop_response = requests.post(popularity_api_url, headers=headers, data=pop_query, timeout=20)
            pop_response.raise_for_status()
            
            ids = [item['game_id'] for item in pop_response.json() if 'game_id' in item]
            current_app.logger.info(f"       - Found {len(ids)} potential popular game IDs.")
            
            if ids:
                ids_string = ",".join(map(str, ids))
                details_query = (
                    f'fields name, cover.url, slug, aggregated_rating; '
                    f'where id = ({ids_string}) & first_release_date < {now_timestamp} & platforms = (6); limit {CANDIDATE_LIMIT};'
                )
                details_response = requests.post(games_api_url, headers=headers, data=details_query, timeout=20)
                details_response.raise_for_status()
                
                game_map = {game['id']: game for game in details_response.json()}
                popular_now_games = [game_map[gid] for gid in ids if gid in game_map][:12]
                current_app.logger.info(f"       - Built list with {len(popular_now_games)} final games.")
        except Exception as e:
            current_app.logger.error(f"    -> Failed to build 'Popular Right Now' list: {e}")

        # --- (The rest of the queries remain the same) ---
        ninety_days_from_now = now_timestamp + (90 * 24 * 60 * 60)
        coming_soon_query = f'fields name, cover.url, first_release_date, slug; where first_release_date > {now_timestamp} & first_release_date < {ninety_days_from_now} & platforms = (6); sort first_release_date asc; limit 12;'
        response_coming_soon = requests.post(games_api_url, headers=headers, data=coming_soon_query, timeout=20)
        coming_soon_games = response_coming_soon.json()

        one_year_ago = now_timestamp - (365 * 24 * 60 * 60)
        top_reviewed_query = (
            f'fields name, cover.url, first_release_date, slug, total_rating, total_rating_count; ' # Use total_rating
            f'where first_release_date < {now_timestamp} & first_release_date > {one_year_ago} '    # Look back one year
            f'& platforms = (6) & total_rating > 75 & total_rating_count > 5; '                   # Add rating count filter
            f'sort total_rating desc; limit 12;'                                                     # Sort by total_rating
        )
        response_top_reviewed = requests.post(games_api_url, headers=headers, data=top_reviewed_query, timeout=20)
        top_reviewed_games = response_top_reviewed.json()
        
        # Cache all four lists in the database
        lists_to_cache = {
            'anticipated': anticipated_games,
            'popular_now': popular_now_games,
            'coming_soon': coming_soon_games,
            'top_reviewed': top_reviewed_games
        }

        for name, content in lists_to_cache.items():
            cache_item = DiscoverCache.query.get(name)
            if cache_item: cache_item.content = json.dumps(content)
            else: db.session.add(DiscoverCache(list_name=name, content=json.dumps(content)))
        
        db.session.commit()
        current_app.logger.info("--- Finished Discover list update ---")
        return True

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Failed to update Discover lists using API v4: {e}")
        return False