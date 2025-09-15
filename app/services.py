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

    
# =============================================
# External API Services (Jackett, IGDB, qBit)
# =============================================

def search_igdb(search_term):
    """Searches the IGDB API using credentials from the settings."""
    settings = get_settings_dict()
    twitch_client_id = settings.get('twitch_client_id')
    twitch_client_secret = settings.get('twitch_client_secret')

    if not all([twitch_client_id, twitch_client_secret]):
        current_app.logger.error("IGDB Search: Twitch credentials not configured.")
        return []

    try:
        auth_url = 'https://id.twitch.tv/oauth2/token'
        auth_params = {'client_id': twitch_client_id, 'client_secret': twitch_client_secret, 'grant_type': 'client_credentials'}
        auth_response = requests.post(auth_url, params=auth_params, timeout=10)
        auth_response.raise_for_status()
        access_token = auth_response.json()['access_token']

        igdb_url = 'https://api.igdb.com/v4/games'
        headers = {'Client-ID': twitch_client_id, 'Authorization': f'Bearer {access_token}'}
        
        # --- THIS IS THE FIX ---
        # We've added 'slug' to the list of fields we're requesting.
        query_body = f'search "{search_term}"; fields name, cover.url, first_release_date, slug; limit 20;'
        
        response = requests.post(igdb_url, headers=headers, data=query_body, timeout=10)
        response.raise_for_status()
        
        results = response.json()
        
        # --- THIS IS THE OTHER FIX ---
        # We now include the 'slug' in the data we send back to the frontend.
        cleaned_results = [
            {
                'id': game.get('id'),
                'name': game.get('name'),
                'slug': game.get('slug'), # <-- Add the slug here
                'cover_url': game.get('cover', {}).get('url', '').replace('t_thumb', 't_cover_big'),
                'release_timestamp': game.get('first_release_date')
            } for game in results
        ]
        return cleaned_results
    except requests.exceptions.RequestException as e:
        current_app.logger.error(f"Error searching IGDB: {e}")
        return []

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

def search_jackett(game_title):
    """
    Searches Jackett and parses the Torznab feed.
    This is the definitive version, corrected for typos and to handle both
    magnet links (public trackers) and .torrent files (private trackers).
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
                # --- FIX: Robust link finding for both Private and Public trackers ---
                link_obj = next((link for link in item.links if 'magnet:' in link.href or link.type == 'application/x-bittorrent'), None)
                if not link_obj:
                    continue
                
                download_link = link_obj.href

                # Get size directly from the top-level item
                size_in_bytes = item.get('size')
                grabs = int(item.get('grabs', 0))
                
                # FIX: Handle both dictionary and list formats for torznab_attr
                torznab_attr = item.get('torznab_attr', [])
                attributes = {}
                
                if isinstance(torznab_attr, dict):
                    # Single attribute as dict with 'name' and 'value' keys
                    if 'name' in torznab_attr and 'value' in torznab_attr:
                        attributes[torznab_attr['name']] = torznab_attr['value']
                elif isinstance(torznab_attr, list):
                    # Multiple attributes as list of dicts
                    for attr in torznab_attr:
                        if isinstance(attr, dict):
                            # Handle both @name/@value and name/value formats
                            name_key = '@name' if '@name' in attr else 'name'
                            value_key = '@value' if '@value' in attr else 'value'
                            
                            if name_key in attr and value_key in attr:
                                attributes[attr[name_key]] = attr[value_key]

                # Try multiple possible attribute names and handle string conversion
                def safe_int(value, default=0):
                    try:
                        return int(value) if value is not None else default
                    except (ValueError, TypeError):
                        return default

                # Check various possible attribute names for seeders
                seeders = 0
                for seed_key in ['seeders', 'seeds', 'seeder']:
                    if seed_key in attributes:
                        seeders = safe_int(attributes[seed_key])
                        break

                # Check various possible attribute names for leechers/peers
                leechers = 0
                peers = 0
                
                # First try to get leechers directly
                for leech_key in ['leechers', 'leeches', 'leech']:
                    if leech_key in attributes:
                        leechers = safe_int(attributes[leech_key])
                        break
                
                # If no direct leechers, try peers and calculate
                if leechers == 0:
                    for peer_key in ['peers', 'peer']:
                        if peer_key in attributes:
                            peers = safe_int(attributes[peer_key])
                            leechers = max(0, peers - seeders)
                            break

                # For private trackers that don't provide seeder/leecher data,
                # we might want to indicate this differently (e.g., show "N/A" or use grabs as indicator)
                tracker_type = item.get('type', 'unknown')
                
                indexer_raw = item.get('jackettindexer', 'Unknown')
                indexer = indexer_raw.get('id', indexer_raw) if isinstance(indexer_raw, dict) else indexer_raw

                results.append({
                    'title': item.title,
                    'link': download_link,
                    'indexer': indexer,
                    'grabs': grabs,
                    'seeders': seeders if seeders > 0 or tracker_type != 'private' else -1,  # -1 indicates "N/A" for private trackers
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
    
    if not all([
        settings.get('qbittorrent_host'),
        settings.get('qbittorrent_port'),
        settings.get('qbittorrent_user'),
        settings.get('qbittorrent_pass')
    ]):
        current_app.logger.error("qBittorrent credentials not configured.")
        return None
    
    try:
        client = Client(
            host=settings.get('qbittorrent_host'),
            port=settings.get('qbittorrent_port'),
            username=settings.get('qbittorrent_user'),
            password=settings.get('qbittorrent_pass')
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

        # Give client a moment to register the torrent
        import time
        time.sleep(2)
        
        # Find the hash of the torrent we just added
        added_torrents = client.torrents_info(tag="gamearr", category=category, sort='added_on', reverse=True)
        return added_torrents[0].hash if added_torrents else None
    except Exception as e:
        current_app.logger.error(f"An unexpected qBittorrent error occurred: {e}")
        return None

# =============================================
# P2P Source Checkers
# =============================================

def check_source_reddit(game_title):
    """
    Checks recent Reddit posts for a new, full game release, ignoring updates and DLCs.
    """
    current_app.logger.info(f"    -> Checking Reddit for '{game_title}'...")
    settings = get_settings_dict()
    
    if not all([settings.get('reddit_client_id'), settings.get('reddit_client_secret'), settings.get('reddit_username'), settings.get('reddit_password')]):
        current_app.logger.warning("    -> Reddit credentials not configured. Skipping check.")
        return None, None
        
    try:
        reddit = praw.Reddit(
            client_id=settings.get('reddit_client_id'),
            client_secret=settings.get('reddit_client_secret'),
            user_agent="Gamearr v1.1",
            username=settings.get('reddit_username'),
            password=settings.get('reddit_password')
        )

        redditor = reddit.redditor('EssenseOfMagic')
        normalized_title = game_title.replace(':', '').lower()
        title_keywords = set(normalized_title.split())
        BLOCKED_KEYWORDS = {'TRAINER', 'UPDATE', 'DLC', 'PATCH', 'CRACKFIX', 'MACOS', 'LINUX'}

        for submission in redditor.submissions.new(limit=100):
            if "Daily Releases" not in submission.title:
                continue

            pattern = re.compile(r"^\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|", re.MULTILINE)
            matches = pattern.findall(submission.selftext)

            for game_name, group_name in matches:
                cleaned_reddit_title_set = set(game_name.strip().replace('.', ' ').replace('_', ' ').lower().split())
                
                # Check 1: Is it the right game?
                if not title_keywords.issubset(cleaned_reddit_title_set):
                    continue
                    
                # Check 2: Does it contain blocked words?
                if any(blocked_word in {word.upper() for word in cleaned_reddit_title_set} for blocked_word in BLOCKED_KEYWORDS):
                    continue

                # If both checks pass, it's a valid release.
                current_app.logger.info(f"       --> Found Reddit match: {game_name.strip()} by {group_name.strip()}")
                return game_name.strip(), group_name.strip()

    except Exception as e:
        current_app.logger.error(f"    -> ERROR: Reddit check failed: {e}")
    
    return None, None

def check_reddit_deep_search(game_title):
    """
    Performs a deep search of Reddit for a historical, full game release.
    """
    current_app.logger.info(f"    -> Performing Reddit deep search for '{game_title}'...")
    settings = get_settings_dict()

    if not all([settings.get('reddit_client_id'), settings.get('reddit_client_secret'), settings.get('reddit_username'), settings.get('reddit_password')]):
        current_app.logger.warning("    -> Reddit credentials not configured. Skipping deep search.")
        return None, None

    try:
        reddit = praw.Reddit(
            client_id=settings.get('reddit_client_id'),
            client_secret=settings.get('reddit_client_secret'),
            user_agent="Gamearr v1.1 Deep Search",
            username=settings.get('reddit_username'),
            password=settings.get('reddit_password')
        )

        target_subreddit = "CrackWatch"
        search_query = f'author:EssenseOfMagic "{game_title}"'
        subreddit = reddit.subreddit(target_subreddit)
        search_results = subreddit.search(search_query, sort='new', limit=100)

        # We must define these here to use in the loop
        normalized_title = game_title.replace(':', '').lower()
        title_keywords = set(normalized_title.split())
        BLOCKED_KEYWORDS = {'TRAINER', 'UPDATE', 'DLC', 'PATCH', 'CRACKFIX', 'MACOS', 'LINUX'}
        
        for submission in search_results:
            if "daily releases" not in submission.title.lower():
                continue

            pattern = re.compile(r"^\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|", re.MULTILINE)
            matches = pattern.findall(submission.selftext)

            for game_name, group_name in matches:
                # Use a simple 'in' check for deep search, as the Reddit search is already targeted
                if game_title.lower() not in game_name.strip().lower():
                    continue

                # Still perform the critical check for blocked words
                cleaned_reddit_title_set = set(game_name.strip().replace('.', ' ').replace('_', ' ').lower().split())
                if any(blocked_word in {word.upper() for word in cleaned_reddit_title_set} for blocked_word in BLOCKED_KEYWORDS):
                    continue

                current_app.logger.info(f"       --> Found historical match: {game_name.strip()} by {group_name.strip()}")
                return game_name.strip(), group_name.strip()

    except Exception as e:
        current_app.logger.error(f"    -> ERROR: Reddit deep search failed: {e}")
    
    return None, None

def check_source_rss(feed_url, game_title):
    try:
        feed = feedparser.parse(feed_url)
        if feed.entries:
            normalized_title = game_title.replace(':', '').lower()
            title_keywords = set(normalized_title.split())
            
            # --- THIS IS THE NEW FUZZY LOGIC ---
            # We will consider it a match if at least 75% of our keywords are found.
            # We also require at least 2 keywords to match to avoid single-word false positives.
            required_matches = max(2, int(len(title_keywords) * 0.75))
            # ------------------------------------

            for entry in feed.entries:
                cleaned_entry_title_set = set(entry.title.replace('.', ' ').replace('_', ' ').lower().split())
                
                # Find how many of our keywords are in the release title
                matching_keywords = title_keywords.intersection(cleaned_entry_title_set)
                
                if len(matching_keywords) >= required_matches:
                    current_app.logger.info(f"        --> Fuzzy match found! Matched {len(matching_keywords)}/{len(title_keywords)} keywords.")
                    return entry.title
    except Exception as e:
        current_app.logger.info(f"    ERROR: RSS feed check for '{feed_url}' failed: {e}")
    return None # Return None on failure

# =============================================
# The Main Release Finding Engine
# =============================================

def find_release_for_game(game_id):
    """
    The unified search engine, now using the correct server-side 'section'
    filter for the predb.net API call.
    """
    game = Game.query.get(game_id)
    if not game: return False
        
    current_app.logger.info(f"--> Processing '{game.official_title}'")
    
    # --- TIER 1: SCENE CHECK (with the correct API parameter) ---
    try:
        sanitized_title = ''.join(e for e in game.official_title if e.isalnum() or e.isspace()).strip()
        url = "https://api.predb.net/"
        
        # --- THIS IS THE FIX ---
        # We now tell the API to only search in sections that contain the word "GAMES".
        # This is far more efficient than fetching everything and filtering it ourselves.
        params = {
            'type': 'search',
            'q': sanitized_title,
            'section': 'GAMES', # This is the key parameter
            'sort': 'DESC'
        }
        # --- END OF FIX ---

        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        results = response.json().get('data', [])

        if results:
            current_app.logger.info(f"    -> api.predb.net (GAMES section) returned {len(results)} results for '{sanitized_title}'.")
            
            # These keyword checks are still valuable for filtering out trainers, DLCs, etc.
            normalized_title = game.official_title.replace(':', '').lower()
            title_keywords = set(normalized_title.split())
            BLOCKED_KEYWORDS = {'TRAINER', 'UPDATE', 'DLC', 'PATCH', 'CRACKFIX', 'MACOS', 'LINUX', 'NSW', 'PS5', 'PS4', 'XBOX'}

            for release in results:
                release_name = release.get('release', '')
                
                # We no longer need the 'Category Gatekeeper' check here,
                # as the API has already done it for us.
                
                cleaned_release_for_keywords = release_name.upper().replace('.', ' ').replace('_', ' ').replace('-', ' ')
                release_keyword_set = set(cleaned_release_for_keywords.split())
                title_keywords_upper = {keyword.upper() for keyword in title_keywords}
                
                if not title_keywords_upper.issubset(release_keyword_set):
                    continue
                
                if any(blocked_word in release_keyword_set for blocked_word in BLOCKED_KEYWORDS):
                    continue
                
                # If we get here, it's a valid release.
                db_nfo_path, db_nfo_img_path = fetch_and_save_nfo(release_name)
                
                game.status = 'Definitely Cracked'
                game.release_name = release_name
                game.release_group = release.get('group')
                game.nfo_path = db_nfo_path
                game.nfo_img_path = db_nfo_img_path
                
                db.session.commit()
                current_app.logger.info(f"    SUCCESS! Found Scene release: {release_name}")
                return True
    
    except Exception as e:
        current_app.logger.error(f"    ERROR checking predb.net: {e}")

    # --- TIER 2: P2P RECENT CHECKS ---
    current_app.logger.info("    No scene release found. Checking P2P sources...")
    
    # Source 2.1: Reddit Recents
    # (Assuming check_source_reddit is also refactored to not need 'settings')
    recent_release_name, recent_group_name = check_source_reddit(game.official_title)
    if recent_release_name:
        current_app.logger.info(f"    SUCCESS! Found recent P2P release via Reddit: {recent_release_name}")
        game.status = 'Probably Cracked (P2P)'
        game.release_name = recent_release_name
        game.release_group = recent_group_name
        db.session.commit()
        return True
    
    # Source 2.2: RSS Feeds
    p2p_rss_sources = { "FitGirl RSS": "http://fitgirl-repacks.site/feed/", "Repack.info RSS": "https://repack.info/en/rss.xml" }
    for source_name, feed_url in p2p_rss_sources.items():
        p2p_release_name = check_source_rss(feed_url, game.official_title)
        if p2p_release_name:
            current_app.logger.info(f"    SUCCESS! Found P2P release via {source_name}: {p2p_release_name}")
            game.status = 'Probably Cracked (P2P)'
            game.release_name = p2p_release_name
            game.release_group = "FitGirl" if "FitGirl" in source_name else "P2P Repack"
            db.session.commit()
            return True

    # --- TIER 3: P2P HISTORICAL DEEP SEARCH (Reddit) ---
    current_app.logger.info("    No recent releases found. Performing deep search on Reddit...")
    deep_release_name, deep_group_name = check_reddit_deep_search(game.official_title)
    if deep_release_name:
        current_app.logger.info(f"    SUCCESS! Found historical P2P release via Reddit deep search: {deep_release_name}")
        game.status = 'Probably Cracked (P2P)'
        game.release_name = deep_release_name
        game.release_group = deep_group_name
        db.session.commit()
        return True

    current_app.logger.info(f"    No release found for '{game.official_title}' after all automated checks.")
    return False


def fetch_and_save_nfo(release_name):
    """
    Fetches NFO data and image, correctly parsing the dictionary response from the API.
    """
    current_app.logger.info(f"NFO Fetcher: Attempting to fetch NFO for '{release_name}'")
    
    try:
        url = "https://api.predb.net/"
        params = {'type': 'nfo', 'release': release_name}
        
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        
        json_data = response.json()

        # Get the 'data' dictionary from the API response
        nfo_data_dict = json_data.get('data')

        # Check if the 'data' dictionary exists and is not empty
        if not nfo_data_dict or not isinstance(nfo_data_dict, dict):
            current_app.logger.info("NFO Fetcher: No 'data' object found in the API response.")
            return None, None
        
        # Get the URLs directly from the dictionary keys
        nfo_url = nfo_data_dict.get('nfo')
        nfo_img_url = nfo_data_dict.get('nfo_img')

        # Create NFO storage directory
        nfo_storage_path = os.path.join(current_app.root_path, 'nfo_storage')
        os.makedirs(nfo_storage_path, exist_ok=True)
        
        local_nfo_path = None
        local_nfo_img_path = None

        # Download and save NFO file
        if nfo_url:
            try:
                nfo_response = requests.get(nfo_url, timeout=10)
                if nfo_response.status_code == 200:
                    safe_filename = "".join(c for c in release_name if c.isalnum() or c in ('_', '-')).rstrip()
                    local_nfo_path = os.path.join(nfo_storage_path, f"{safe_filename}.nfo")
                    with open(local_nfo_path, 'w', encoding='utf-8', errors='ignore') as f:
                        f.write(nfo_response.text)
                    current_app.logger.info(f"NFO Fetcher: Saved .nfo file to {local_nfo_path}")
            except Exception as e:
                current_app.logger.error(f"NFO Fetcher: Error downloading NFO file: {e}")

        # Download and save NFO image
        if nfo_img_url:
            try:
                nfo_img_response = requests.get(nfo_img_url, timeout=10)
                if nfo_img_response.status_code == 200:
                    safe_filename = "".join(c for c in release_name if c.isalnum() or c in ('_', '-')).rstrip()
                    local_nfo_img_path = os.path.join(nfo_storage_path, f"{safe_filename}.png")
                    with open(local_nfo_img_path, 'wb') as f:
                        f.write(nfo_img_response.content)
                    current_app.logger.info(f"NFO Fetcher: Saved .png image to {local_nfo_img_path}")
            except Exception as e:
                current_app.logger.error(f"NFO Fetcher: Error downloading NFO image: {e}")
        
        # Convert absolute paths to relative paths for database storage
        db_nfo_path = os.path.relpath(local_nfo_path, nfo_storage_path) if local_nfo_path else None
        db_nfo_img_path = os.path.relpath(local_nfo_img_path, nfo_storage_path) if local_nfo_img_path else None
        
        return db_nfo_path, db_nfo_img_path

    except Exception as e:
        current_app.logger.error(f"NFO Fetcher: An error occurred: {e}")
        return None, None
    
# =============================================
# Library scan
# =============================================

def _normalize_for_comparison(title):
    """A helper to clean titles consistently for accurate comparison."""
    # Lowercase, remove common separators and specific punctuation
    cleaned = title.lower().replace('.', ' ').replace('_', ' ').replace('-', ' ')
    # Remove characters like colons, apostrophes, etc.
    cleaned = re.sub(r"[:'!,]", "", cleaned)
    # Replace multiple spaces with a single space
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned

def scan_library_folder():
    """
    Scans the library path for folders that aren't already in the database,
    and ignores common unwanted folders.
    """
    library_path = current_app.config['LIBRARY_PATH']
    current_app.logger.info(f"--- Starting Library Scan on Path: '{library_path}' ---")

    if not os.path.isdir(library_path):
        current_app.logger.error(f"Library path not found or is not a directory.")
        return []

    # --- FIX: Add a set of folders to always ignore ---
    IGNORE_FOLDERS = {'_downloads', '@eaDir'} # Add any other folder names you want to skip

    # Get a list of all folder names already tracked by the app's 'local_path' column
    existing_folders = {game.local_path for game in Game.query.all() if game.local_path}
    current_app.logger.info(f"Found {len(existing_folders)} folders already in the database.")

    found_folders = []
    with os.scandir(library_path) as it:
        for entry in it:
            # --- FIX: Add checks for the ignore list ---
            if entry.is_dir() and entry.name not in existing_folders and entry.name not in IGNORE_FOLDERS:
                found_folders.append(entry.name)
    
    current_app.logger.info(f"Scan complete. Found {len(found_folders)} new folders to process.")
    return found_folders

def parse_folder_name(folder_name):
    """
    Uses a combination of string manipulation, PTN, and regex to
    accurately guess the game title from a folder name.
    """
    # Step 1: Pre-processing. Replace common separators with spaces to help the parser.
    clean_name = folder_name.replace('.', ' ').replace('_', ' ')

    # Step 2: Let PTN do its best to parse the pre-cleaned name.
    # It's still good at finding things like 'v1.2' or 'Repack'.
    info = PTN.parse(clean_name)
    title = info.get('title', clean_name) # Use the cleaned name as a fallback

    # Step 3: Post-processing. THIS IS THE FIX.
    # Use regex to strip common release group tags from the end of the string.
    # This pattern looks for a hyphen followed by letters/numbers at the very end.
    # Examples: -FLT, -TENOKE, -CODEX, -P2P, -SKIDROW
    title = re.sub(r'-[A-Za-z0-9]+$', '', title).strip()

    # Step 4: Final cleanup.
    return title.strip()

def process_library_scan():
    """
    The main service function for the library import feature.
    - Scans for new folders.
    - Guesses titles.
    - Filters out games that already exist in the library by title.
    - Searches IGDB for the remaining new games.
    """
    untracked_folders = scan_library_folder()
    if not untracked_folders:
        return []

    # --- THE FIX: Get all existing game titles and normalize them for comparison ---
    all_games_in_db = Game.query.all()
    # Create a list of keyword sets for every game in the library
    existing_title_keyword_sets = [
        set(_normalize_for_comparison(game.official_title).split()) 
        for game in all_games_in_db
    ]

    results = []
    current_app.logger.info("--- Matching folders to IGDB (and checking for duplicates) ---")

    for folder in untracked_folders:
        guessed_title = parse_folder_name(folder)
        normalized_guessed_title = _normalize_for_comparison(guessed_title)
        guessed_keywords = set(normalized_guessed_title.split())

        # --- THE FIX: Check if the guessed title matches any existing game ---
        is_already_in_library = False
        for existing_keywords in existing_title_keyword_sets:
            # If the keywords from the folder are a subset of an existing game's keywords,
            # it's considered a match. (e.g., {'7', 'days'} is a subset of {'7', 'days', 'to', 'die'})
            if guessed_keywords.issubset(existing_keywords):
                is_already_in_library = True
                break # Found a match, no need to check further

        if is_already_in_library:
            current_app.logger.info(f"Folder: '{folder}'  --> Guessed Title: '{guessed_title}' [SKIPPING - Already in library]")
            continue # Skip this folder and move to the next one

        # If we get here, it's a new game. Proceed with IGDB search.
        current_app.logger.info(f"Folder: '{folder}'  --> Guessed Title: '{guessed_title}' [NEW - Searching IGDB]")
        igdb_matches = search_igdb(guessed_title)
        match_count = len(igdb_matches)
        current_app.logger.info(f"    └─> IGDB search found {match_count} match(es).")
        
        results.append({
            'folder_name': folder,
            'guessed_title': guessed_title,
            'igdb_matches': igdb_matches
        })
    
    current_app.logger.info("--- Finished matching process ---")
    return results

# =============================================
# Discover
# =============================================

def _get_igdb_headers():
    """A private helper to get the required auth headers for any IGDB API call."""
    settings = get_settings_dict()
    twitch_client_id = settings.get('twitch_client_id')
    twitch_client_secret = settings.get('twitch_client_secret')
    if not all([twitch_client_id, twitch_client_secret]):
        raise Exception("Twitch API credentials are not configured.")

    auth_url = 'https://id.twitch.tv/oauth2/token'
    auth_params = {'client_id': twitch_client_id, 'client_secret': twitch_client_secret, 'grant_type': 'client_credentials'}
    auth_response = requests.post(auth_url, params=auth_params, timeout=10)
    auth_response.raise_for_status()
    access_token = auth_response.json()['access_token']
    
    # --- THIS IS THE FIX ---
    # We add a standard User-Agent header to make our request look more legitimate.
    return {
        'Client-ID': twitch_client_id,
        'Authorization': f'Bearer {access_token}',
        'User-Agent': 'Gamearr/1.0 (Python/Requests)'
    }

def update_discover_lists():
    """
    Fetches 'Anticipated', 'Coming Soon', and a NEW 'Top Reviewed' list
    using the official, reliable IGDB API v4 and caches them in the database.
    """
    current_app.logger.info("--- Starting daily Discover list update (using API v4) ---")
    try:
        headers = _get_igdb_headers()
        api_url = "https://api.igdb.com/v4/games"
        now_timestamp = int(time.time())

        # --- Query 1: Most Anticipated (Unchanged) ---
        anticipated_query = (
            f'fields name, cover.url, first_release_date, slug; '
            f'where first_release_date > {now_timestamp} & platforms = 6 & hypes > 0; '
            f'sort hypes desc; limit 12;'
        )
        response_anticipated = requests.post(api_url, headers=headers, data=anticipated_query, timeout=20)
        response_anticipated.raise_for_status()
        anticipated_games = response_anticipated.json()

        # --- Query 2: Coming Soon (Unchanged) ---
        ninety_days_from_now = now_timestamp + (90 * 24 * 60 * 60)
        coming_soon_query = (
            f'fields name, cover.url, first_release_date, slug; '
            f'where first_release_date > {now_timestamp} & first_release_date < {ninety_days_from_now} & platforms = 6; '
            f'sort first_release_date asc; limit 12;'
        )
        response_coming_soon = requests.post(api_url, headers=headers, data=coming_soon_query, timeout=20)
        response_coming_soon.raise_for_status()
        coming_soon_games = response_coming_soon.json()

        # --- THIS IS THE NEW QUERY: Top Reviewed (Replaces Recently Released) ---
        ninety_days_ago = now_timestamp - (90 * 24 * 60 * 60) # Look at the last 3 months
        top_reviewed_query = (
            # We still request the rating_count, but we don't filter by it anymore
            f'fields name, cover.url, first_release_date, slug, aggregated_rating, aggregated_rating_count; '
            
            # --- THIS IS THE FIX ---
            # We have removed the 'aggregated_rating_count > 5' condition.
            f'where first_release_date < {now_timestamp} & first_release_date > {ninety_days_ago} & platforms = 6 & aggregated_rating > 70; '
            
            f'sort aggregated_rating desc; limit 12;'
        )
        response_top_reviewed = requests.post(api_url, headers=headers, data=top_reviewed_query, timeout=20)
        response_top_reviewed.raise_for_status()
        top_reviewed_games = response_top_reviewed.json()
        
        # --- Cache the results with the new list name ---
        lists_to_cache = {
            'anticipated': anticipated_games,
            'coming_soon': coming_soon_games,
            'top_reviewed': top_reviewed_games # <-- New key name
        }

        # The rest of the caching logic remains the same...
        for name, content in lists_to_cache.items():
            cache_item = DiscoverCache.query.get(name)
            if cache_item:
                cache_item.content = json.dumps(content)
            else:
                cache_item = DiscoverCache(list_name=name, content=json.dumps(content))
                db.session.add(cache_item)
            current_app.logger.info(f"Successfully cached Discover list: {name} with {len(content)} games.")
        
        db.session.commit()
        current_app.logger.info("--- Finished Discover list update ---")
        return True

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Failed to update Discover lists using API v4: {e}")
        return False
    
