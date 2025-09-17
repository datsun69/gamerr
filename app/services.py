# /gamearr/app/services.py

# --- Standard Library Imports ---
import os
import PTN
import re
import json
import time
from datetime import datetime
import urllib.parse

# --- Third-Party Library Imports ---
import requests
import praw
import feedparser
from qbittorrentapi import Client, exceptions
from flask import current_app

# --- Local Application Imports ---
from . import db
from .models import Game, Setting, DiscoverCache

# --- NEW: Global cache for the IGDB token ---
_igdb_access_token = None
_igdb_token_expires = 0
    
# =============================================
# External API Services (Jackett, IGDB, qBit)
# =============================================

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
        # --- REFACTORED ---
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

def _get_reddit_instance():
    """
    Creates and returns an authenticated PRAW Reddit instance.
    Returns None if credentials are not configured.
    """
    settings = get_settings_dict()
    if not all([settings.get('reddit_client_id'), settings.get('reddit_client_secret'), settings.get('reddit_username'), settings.get('reddit_password')]):
        current_app.logger.warning("Reddit credentials are not fully configured in settings.")
        return None
        
    try:
        reddit = praw.Reddit(
            client_id=settings.get('reddit_client_id'),
            client_secret=settings.get('reddit_client_secret'),
            user_agent="Gamearr v1.2 by datsun69",
            username=settings.get('reddit_username'),
            password=settings.get('reddit_password')
        )
        if reddit.user.me():
            return reddit
    except Exception as e:
        current_app.logger.error(f"Failed to create Reddit instance: {e}")
    
    return None

def _parse_reddit_section(submission_text, section_title):
    """
    Parses a specific section of a Reddit submission's markdown text to find release names and groups.
    """
    releases = []
    try:
        sections = re.split(r'\*\*([^*]+)\*\*', submission_text)
        normalized_section_title = section_title.strip().lower()
        
        found_index = -1
        for i, header in enumerate(sections):
            if i % 2 == 1 and header.strip().lower() == normalized_section_title:
                found_index = i
                break

        if found_index != -1 and (found_index + 1) < len(sections):
            section_content = sections[found_index + 1]
            pattern = re.compile(r"^\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|", re.MULTILINE)
            matches = pattern.findall(section_content)
            for game_name, group_name in matches:
                releases.append((game_name.strip(), group_name.strip()))
    except Exception as e:
        current_app.logger.error(f"Failed during _parse_reddit_section for title '{section_title}': {e}")
    return releases

def check_source_reddit(game_title):
    """
    Checks recent Reddit posts for a new, full game release by parsing ONLY the 'Daily release' section.
    """
    current_app.logger.info(f"    -> Checking Reddit for '{game_title}'...")
    reddit = _get_reddit_instance()
    if not reddit:
        current_app.logger.warning("    -> Reddit credentials not configured. Skipping check.")
        return None, None
        
    try:
        redditor = reddit.redditor('EssenseOfMagic')
        normalized_title = game_title.replace(':', '').lower()
        title_keywords = set(normalized_title.split())
        BLOCKED_KEYWORDS = {'TRAINER', 'UPDATE', 'DLC', 'PATCH', 'CRACKFIX', 'MACOS', 'LINUX'}

        for submission in redditor.submissions.new(limit=100):
            if "Daily Releases" not in submission.title:
                continue

            daily_releases = _parse_reddit_section(submission.selftext, "Daily release")

            for game_name, group_name in daily_releases:
                cleaned_reddit_title_set = set(game_name.strip().replace('.', ' ').replace('_', ' ').lower().split())
                
                if not title_keywords.issubset(cleaned_reddit_title_set):
                    continue
                if any(blocked_word in {word.upper() for word in cleaned_reddit_title_set} for blocked_word in BLOCKED_KEYWORDS):
                    continue

                current_app.logger.info(f"       --> Found Reddit match: {game_name} by {group_name}")
                return game_name, group_name
    except Exception as e:
        current_app.logger.error(f"    -> ERROR: Reddit check failed: {e}")
    
    return None, None


def check_reddit_deep_search(game_title):
    """
    Performs a deep search of Reddit for a historical, full game release.
    This version uses wide-net parsing (like the original) and robust keyword matching.
    """
    current_app.logger.info(f"    -> Performing Reddit deep search for '{game_title}'...")
    reddit = _get_reddit_instance()
    if not reddit:
        current_app.logger.warning("    -> Reddit credentials not configured. Skipping deep search.")
        return None, None

    try:
        # Normalize the title for the search query to improve finding the post
        normalized_search_term = re.sub(r"[:']", "", game_title)
        
        target_subreddit = "CrackWatch"
        search_query = f'author:EssenseOfMagic "{normalized_search_term}"'
        subreddit = reddit.subreddit(target_subreddit)
        search_results = subreddit.search(search_query, sort='new', limit=100)

        # Use the original title to create a precise set of keywords for matching
        normalized_title_for_keywords = game_title.replace(':', '').lower()
        title_keywords = set(normalized_title_for_keywords.split())
        BLOCKED_KEYWORDS = {'TRAINER', 'UPDATE', 'DLC', 'PATCH', 'CRACKFIX', 'MACOS', 'LINUX'}
        
        for submission in search_results:
            if "daily releases" not in submission.title.lower():
                continue

            # --- THIS IS THE FIX ---
            # We revert to the original, more effective method of scanning the ENTIRE post body.
            # We do NOT use the strict _parse_reddit_section helper here.
            pattern = re.compile(r"^\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|", re.MULTILINE)
            matches = pattern.findall(submission.selftext)

            for game_name, group_name in matches:
                # Use the robust keyword subset check for accurate matching
                cleaned_reddit_title_set = set(game_name.strip().replace('.', ' ').replace('_', ' ').lower().split())
                if not title_keywords.issubset(cleaned_reddit_title_set):
                    continue

                # Still perform the critical check for blocked words
                if any(blocked_word in {word.upper() for word in cleaned_reddit_title_set} for blocked_word in BLOCKED_KEYWORDS):
                    continue

                current_app.logger.info(f"       --> Found historical match: {game_name.strip()} by {group_name.strip()}")
                return game_name.strip(), group_name.strip()

    except Exception as e:
        current_app.logger.error(f"    -> ERROR: Reddit deep search failed: {e}")
    
    return None, None

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

# =============================================
# P2P Source Checkers
# =============================================
# (The primary check_source_reddit is already defined above, the duplicate has been removed)

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


# =============================================
# The Main Release Finding Engine
# =============================================

def find_release_for_game(game_id):
    """
    The definitive, unified search engine for finding a BASE GAME release.
    """
    game = Game.query.get(game_id)
    if not game:
        current_app.logger.error(f"find_release_for_game called with invalid game_id: {game_id}")
        return False
        
    current_app.logger.info(f"--> Processing '{game.official_title}'")
    
    # --- TIER 1: SCENE CHECK (predb.net) ---
    try:
        sanitized_title = ''.join(e for e in game.official_title if e.isalnum() or e.isspace()).strip()
        params = {'type': 'search', 'q': sanitized_title, 'section': 'GAMES', 'sort': 'DESC'}
        BLOCKED_KEYWORDS = {'TRAINER', 'UPDATE', 'DLC', 'PATCH', 'CRACKFIX', 'MACOS', 'LINUX', 'NSW', 'PS5', 'PS4', 'XBOX'}
        response = requests.get("https://api.predb.net/", params=params, timeout=10)
        response.raise_for_status()
        results = response.json().get('data', [])

        if results:
            current_app.logger.info(f"    -> api.predb.net (GAMES section) returned {len(results)} results.")
            normalized_title = game.official_title.replace(':', '').lower()
            title_keywords = set(normalized_title.split())
            
            for release in results:
                release_name = release.get('release', '')
                release_keyword_set = set(release_name.upper().replace('.', ' ').replace('_', ' ').replace('-', ' ').split())
                
                if title_keywords.issubset({word.lower() for word in release_keyword_set}) and not any(word in release_keyword_set for word in BLOCKED_KEYWORDS):
                    db_nfo_path, db_nfo_img_path = fetch_and_save_nfo(release_name)
                    game.status = 'Definitely Cracked'
                    game.release_name = release_name
                    game.release_group = release.get('group')
                    game.nfo_path = db_nfo_path
                    game.nfo_img_path = db_nfo_img_path
                    game.needs_content_scan = True
                    db.session.commit()
                    current_app.logger.info(f"    SUCCESS! Found Scene release: {release_name}")
                    return True
    
    except Exception as e:
        current_app.logger.error(f"    ERROR checking predb.net: {e}")

    # --- TIER 2: P2P RECENT CHECKS ---
    current_app.logger.info("    No scene release found. Checking P2P sources...")
    
    recent_release_name, recent_group_name = check_source_reddit(game.official_title)
    if recent_release_name:
        current_app.logger.info(f"    SUCCESS! Found recent P2P release via Reddit: {recent_release_name}")
        game.status = 'Probably Cracked (P2P)'
        game.release_name = recent_release_name
        game.release_group = recent_group_name
        game.needs_content_scan = True
        db.session.commit()
        return True
    
    # --- TIER 3: P2P HISTORICAL DEEP SEARCH ---
    current_app.logger.info("    No recent releases found. Performing deep search on Reddit...")
    deep_release_name, deep_group_name = check_reddit_deep_search(game.official_title)
    if deep_release_name:
        current_app.logger.info(f"    SUCCESS! Found historical P2P release: {deep_release_name}")
        game.status = 'Probably Cracked (P2P)'
        game.release_name = deep_release_name
        game.release_group = deep_group_name
        game.needs_content_scan = True
        db.session.commit()
        return True

    # --- Final Step ---
    current_app.logger.info(f"    No release found for '{game.official_title}' after all checks.")
    if game.status == 'Processing':
        game.status = 'Monitoring'
        db.session.commit()
        
    return False

def fetch_and_save_nfo(release_name):
    """
    Fetches NFO data and image.
    """
    current_app.logger.info(f"NFO Fetcher: Attempting to fetch NFO for '{release_name}'")
    try:
        url = "https://api.predb.net/"
        params = {'type': 'nfo', 'release': release_name}
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        json_data = response.json()
        nfo_data_dict = json_data.get('data')

        if not nfo_data_dict or not isinstance(nfo_data_dict, dict):
            current_app.logger.info("NFO Fetcher: No 'data' object found.")
            return None, None
        
        nfo_url = nfo_data_dict.get('nfo')
        nfo_img_url = nfo_data_dict.get('nfo_img')
        nfo_storage_path = os.path.join(current_app.root_path, 'nfo_storage')
        os.makedirs(nfo_storage_path, exist_ok=True)
        
        local_nfo_path, local_nfo_img_path = None, None

        if nfo_url:
            try:
                nfo_response = requests.get(nfo_url, timeout=10)
                if nfo_response.status_code == 200:
                    safe_filename = "".join(c for c in release_name if c.isalnum() or c in ('_', '-')).rstrip()
                    local_nfo_path = os.path.join(nfo_storage_path, f"{safe_filename}.nfo")
                    with open(local_nfo_path, 'w', encoding='utf-8', errors='ignore') as f:
                        f.write(nfo_response.text)
            except Exception as e:
                current_app.logger.error(f"NFO Fetcher: Error downloading NFO file: {e}")

        if nfo_img_url:
            try:
                nfo_img_response = requests.get(nfo_img_url, timeout=10)
                if nfo_img_response.status_code == 200:
                    safe_filename = "".join(c for c in release_name if c.isalnum() or c in ('_', '-')).rstrip()
                    local_nfo_img_path = os.path.join(nfo_storage_path, f"{safe_filename}.png")
                    with open(local_nfo_img_path, 'wb') as f:
                        f.write(nfo_img_response.content)
            except Exception as e:
                current_app.logger.error(f"NFO Fetcher: Error downloading NFO image: {e}")
        
        db_nfo_path = os.path.relpath(local_nfo_path, nfo_storage_path) if local_nfo_path else None
        db_nfo_img_path = os.path.relpath(local_nfo_img_path, nfo_storage_path) if local_nfo_img_path else None
        
        return db_nfo_path, db_nfo_img_path
    except Exception as e:
        current_app.logger.error(f"NFO Fetcher: An error occurred: {e}")
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

# =============================================
# Library scan
# =============================================

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

# =============================================
# Discover
# =============================================

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
    Fetches and caches IGDB discover lists.
    """
    current_app.logger.info("--- Starting daily Discover list update (using API v4) ---")
    try:
        headers = _get_igdb_headers()
        api_url = "https://api.igdb.com/v4/games"
        now_timestamp = int(time.time())

        anticipated_query = f'fields name, cover.url, first_release_date, slug; where first_release_date > {now_timestamp} & platforms = 6 & hypes > 0; sort hypes desc; limit 12;'
        response_anticipated = requests.post(api_url, headers=headers, data=anticipated_query, timeout=20)
        response_anticipated.raise_for_status()
        anticipated_games = response_anticipated.json()

        ninety_days_from_now = now_timestamp + (90 * 24 * 60 * 60)
        coming_soon_query = f'fields name, cover.url, first_release_date, slug; where first_release_date > {now_timestamp} & first_release_date < {ninety_days_from_now} & platforms = 6; sort first_release_date asc; limit 12;'
        response_coming_soon = requests.post(api_url, headers=headers, data=coming_soon_query, timeout=20)
        response_coming_soon.raise_for_status()
        coming_soon_games = response_coming_soon.json()

        ninety_days_ago = now_timestamp - (90 * 24 * 60 * 60)
        top_reviewed_query = f'fields name, cover.url, first_release_date, slug, aggregated_rating, aggregated_rating_count; where first_release_date < {now_timestamp} & first_release_date > {ninety_days_ago} & platforms = 6 & aggregated_rating > 70; sort aggregated_rating desc; limit 12;'
        response_top_reviewed = requests.post(api_url, headers=headers, data=top_reviewed_query, timeout=20)
        response_top_reviewed.raise_for_status()
        top_reviewed_games = response_top_reviewed.json()
        
        lists_to_cache = {
            'anticipated': anticipated_games, 'coming_soon': coming_soon_games, 'top_reviewed': top_reviewed_games
        }

        for name, content in lists_to_cache.items():
            cache_item = DiscoverCache.query.get(name)
            if cache_item:
                cache_item.content = json.dumps(content)
            else:
                cache_item = DiscoverCache(list_name=name, content=json.dumps(content))
                db.session.add(cache_item)
        
        db.session.commit()
        current_app.logger.info("--- Finished Discover list update ---")
        return True

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Failed to update Discover lists using API v4: {e}")
        return False