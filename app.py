import sqlite3
import json
import urllib.parse
import atexit
from datetime import datetime
import uuid
import os
import shutil
from datetime import timedelta
import httpx
import time
from pathlib import Path

# --- Third-party Library Imports ---
from flask import Flask, render_template, request, redirect, url_for, send_from_directory
from qbittorrentapi import Client, exceptions
from apscheduler.schedulers.background import BackgroundScheduler
import feedparser
import requests
import praw
import re

# --- Flask App Initialization ---
app = Flask(__name__)

@app.template_filter('timestamp_to_date') # <--- FIX for 'timestamp_to_date_filter is not defined'
def timestamp_to_date_filter(s):
    """A custom filter for Jinja2 to use in HTML templates."""
    if s is None: 
        return "N/A"
    try:
        # The timestamp can come as a string or int, so we convert it
        return datetime.utcfromtimestamp(int(s)).strftime('%Y-%m-%d')
    except (ValueError, TypeError):
        return "Invalid Date"

# --- Database Setup ---
def get_db_connection():
    conn = sqlite3.connect('database.db')
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    return conn

@app.cli.command('init-db')
def init_db_command():
    """Clears the existing data and creates new tables."""
    conn = get_db_connection()
    with app.open_resource('schema.sql') as f:
        conn.executescript(f.read().decode('utf8'))
    conn.commit()
    conn.close()
    print("Database initialized.")


def search_igdb(search_term, settings): # Note: we now pass 'settings' into the function
    """Searches the IGDB API using credentials from the settings."""
    
    # Get the credentials from the settings dictionary
    twitch_client_id = settings.get('twitch_client_id')
    twitch_client_secret = settings.get('twitch_client_secret')

    if not twitch_client_id or not twitch_client_secret:
        print("Twitch Client ID or Secret is not configured in settings.")
        return []

    # Step 1: Get an Access Token from Twitch
    auth_url = 'https://id.twitch.tv/oauth2/token'
    auth_params = {
        'client_id': twitch_client_id,
        'client_secret': twitch_client_secret,
        'grant_type': 'client_credentials'
    }
    try:
        auth_response = requests.post(auth_url, params=auth_params)
        auth_response.raise_for_status()
        access_token = auth_response.json()['access_token']
    except requests.exceptions.RequestException as e:
        print(f"Error getting Twitch access token: {e}")
        return []

    # Step 2: Query the IGDB API (this part remains the same)
    igdb_url = 'https://api.igdb.com/v4/games'
    headers = {
        'Client-ID': twitch_client_id,
        'Authorization': f'Bearer {access_token}'
    }
    query_body = f'search "{search_term}"; fields name, cover.url, first_release_date; limit 20;'

    try:
        response = requests.post(igdb_url, headers=headers, data=query_body)
        response.raise_for_status()
        # ... (the rest of the function remains the same) ...
        results = response.json()
        
        cleaned_results = []
        for game in results:
            cover_data = game.get('cover')
            cover_url = cover_data.get('url', '') if cover_data else ''
            
            cleaned_results.append({
                'id': game.get('id'),
                'name': game.get('name'),
                'cover_url': cover_url.replace('t_thumb', 't_cover_big'),
                'release_timestamp': game.get('first_release_date')
            })
        return cleaned_results
    except requests.exceptions.RequestException as e:
        print(f"Error searching IGDB: {e}")
        return []

# --- Jackett/qBittorrent ---

def search_jackett(game_title, settings):
    """
    Searches Jackett and robustly parses the Torznab feed to use the 'grabs' count as the popularity metric.
    """
    if not all([settings.get('jackett_url'), settings.get('jackett_api_key')]):
        print("Jackett settings are incomplete.")
        return []

    # Use 'all' if no specific indexers are provided, otherwise join them
    indexer_list = settings.get('jackett_indexers', 'all')
    if indexer_list:
        indexer_list = indexer_list.replace(',', ';')
    else:
        indexer_list = 'all'

    url = (
        f"{settings['jackett_url']}/api/v2.0/indexers/{indexer_list}/results/torznab/"
        f"?apikey={settings['jackett_api_key']}&t=search&cat=4000&q={urllib.parse.quote_plus(game_title)}"
    )

    print(f"Searching Jackett: {url}")
    
    try:
        feed = feedparser.parse(url)
        results = []

        for item in feed.entries:
            try:
                # --- THIS IS THE FIX ---
                # We will now parse the 'grabs' count as our primary metric.
                popularity_metric = int(item.get('grabs', 0))
                # --- END OF FIX ---

                results.append({
                    'title': item.title,
                    'link': next(link.href for link in item.links if 'magnet:' in link.href or link.type == 'application/x-bittorrent'),
                    'seeders': popularity_metric # We'll still call it 'seeders' in our template for consistency
                })
            except (StopIteration, AttributeError, ValueError) as e:
                print(f"Skipping a malformed Jackett result. Error: {e}")
                continue
        
        # Return results sorted by the number of grabs (most popular first)
        return sorted(results, key=lambda x: x['seeders'], reverse=True)
    
    except Exception as e:
        print(f"An error occurred while fetching or parsing the Jackett feed: {e}")
        return []

def add_to_qbittorrent(magnet_link, settings):
    """
    Adds a magnet link to qBittorrent and returns a tuple: (success_boolean, torrent_hash_string).
    """
    if not all([settings.get('qbittorrent_host'), settings.get('qbittorrent_port')]):
        print("qBittorrent settings are incomplete.")
        return False, None
        
    try:
        # --- THIS IS THE FIX ---
        # We use the full, correct constructor with values from the settings dictionary.
        # No more placeholders.
        client = Client(
            host=settings['qbittorrent_host'],
            port=settings['qbittorrent_port'],
            username=settings.get('qbittorrent_user'),
            password=settings.get('qbittorrent_pass')
        )
        # ------------------------
        client.auth_log_in()
        
        category_to_set = settings.get('qbittorrent_category')
        
        result = client.torrents_add(
            urls=magnet_link,
            category=category_to_set,
            tags="gamearr"
        )

        if result != "Ok.":
            print(f"Failed to add torrent: {result}")
            return False, None

        # Give the torrent a moment to register in the client
        import time
        time.sleep(2)

        # Find the hash of the torrent we just added
        added_torrents = client.torrents_info(tag="gamearr", category=category_to_set, sort='added_on', reverse=True)
        
        if not added_torrents:
            print("Successfully sent torrent to client, but could not find its hash.")
            return True, None

        new_torrent_hash = added_torrents[0].hash
        print(f"Torrent added successfully. Hash: {new_torrent_hash}")
        return True, new_torrent_hash

    except Exception as e:
        print(f"An unexpected qBittorrent error occurred: {e}")
        return False, None

def find_release_for_game(game, conn):
    """
    The unified search engine, with the correctly implemented client-side filtering logic.
    """
    print(f"--> Processing '{game['official_title']}'")
    settings = {row['key']: row['value'] for row in conn.execute('SELECT * FROM settings').fetchall()}

    # --- TIER 1: SCENE CHECK (api.predb.net) ---
    try:
        sanitized_title = ''.join(e for e in game['official_title'] if e.isalnum() or e.isspace()).strip()
        url = "https://api.predb.net/"
        params = {'type': 'search', 'q': sanitized_title, 'sort': 'DESC'}
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        results = response.json().get('data', [])

        if not results:
             print(f"    -> api.predb.net returned no results for '{sanitized_title}'.")
        else:
            print(f"    -> api.predb.net returned {len(results)} results.")

            normalized_title = game['official_title'].replace(':', '').lower()
            title_keywords = set(normalized_title.split())
            
            BLOCKED_KEYWORDS = {'TRAINER', 'UPDATE', 'DLC', 'PATCH', 'CRACKFIX', 'MACOS', 'LINUX', 'NSW', 'PS5', 'PS4', 'XBOX'}

            for release in results:
                # Prepare the release name's keywords for comparison
                cleaned_release_for_keywords = release.get('release', '').upper().replace('.', ' ').replace('_', ' ').replace('-', ' ')
                release_keyword_set = set(cleaned_release_for_keywords.split())
                
                # --- FIXED: Normalize case for comparison ---
                # Convert title_keywords to uppercase to match release_keyword_set
                title_keywords_upper = {keyword.upper() for keyword in title_keywords}
                
                # 1. Relevance Check: Is this the right game?
                if not title_keywords_upper.issubset(release_keyword_set):
                    continue # No, skip to the next release.

                # 2. Quality Check: Does this release contain ANY blocked keywords?
                if any(blocked_word in release_keyword_set for blocked_word in BLOCKED_KEYWORDS):
                    continue # Yes, it's a trainer/mac/console release. Skip it.
                # --- END OF FIX ---
                
                # If we have reached this point, it means we have passed BOTH checks.
                # This MUST be a valid, full PC game release for the game we want.
                found_release_name = release['release']
                found_release_group = release['group']
                nfo_path, nfo_img_path = fetch_and_save_nfo(found_release_name)
                
                conn.execute(
                    "UPDATE games SET status = 'Definitely Cracked', release_name = ?, release_group = ?, nfo_path = ?, nfo_img_path = ? WHERE id = ?",
                    (found_release_name, found_release_group, nfo_path, nfo_img_path, game['id'])
                )
                conn.commit()
                print(f"    SUCCESS! Found Scene release: {found_release_name}")
                return True
    
    except Exception as e:
        print(f"    ERROR checking predb.net: {e}")

    # --- TIER 2 and TIER 3 remain the same and are correct ---
    print("    No scene release found. Checking P2P sources...")
    
    # Source 2.1: Reddit Recents
    recent_release_name, recent_group_name = check_source_reddit(game['official_title'], settings)
    if recent_release_name:
        print(f"    SUCCESS! Found recent P2P release via Reddit: {recent_release_name}")
        conn.execute(
            "UPDATE games SET status = ?, release_name = ?, release_group = ? WHERE id = ?",
            ('Probably Cracked (P2P)', recent_release_name, recent_group_name, game['id'])
        )
        conn.commit()
        return True
    
    # Source 2.2: RSS Feeds
    p2p_rss_sources = { "FitGirl RSS": "http://fitgirl-repacks.site/feed/", "Repack.info RSS": "https://repack.info/en/rss.xml" }
    for source_name, feed_url in p2p_rss_sources.items():
        p2p_release_name = check_source_rss(feed_url, game['official_title'])
        if p2p_release_name:
            print(f"    SUCCESS! Found P2P release via {source_name}: {p2p_release_name}")
            group_name = "FitGirl" if "FitGirl" in source_name else "P2P Repack"
            conn.execute(
                "UPDATE games SET status = ?, release_name = ?, release_group = ? WHERE id = ?",
                ('Probably Cracked (P2P)', p2p_release_name, group_name, game['id'])
            )
            conn.commit()
            return True

    # --- TIER 3: P2P HISTORICAL DEEP SEARCH (Reddit) ---
    print("    No recent releases found. Performing deep search on Reddit...")
    deep_release_name, deep_group_name = check_reddit_deep_search(game['official_title'], settings)
    if deep_release_name:
        print(f"    SUCCESS! Found historical P2P release via Reddit deep search: {deep_release_name}")
        conn.execute(
            "UPDATE games SET status = ?, release_name = ?, release_group = ? WHERE id = ?",
            ('Probably Cracked (P2P)', deep_release_name, deep_group_name, game['id'])
        )
        conn.commit()
        return True

    print(f"    No release found for '{game['official_title']}' after all automated checks.")
    return False


def check_source_reddit(game_title, settings):
    """
    Checks Reddit for a new release using a stricter, keyword-based matching algorithm.
    """
    print(f"    -> Checking Reddit for '{game_title}'...")
    try:
        # Check if all required Reddit settings are present
        if not all([settings.get('reddit_client_id'), settings.get('reddit_client_secret'),
                    settings.get('reddit_username'), settings.get('reddit_password')]):
            print("    -> Reddit credentials are not fully configured in settings. Skipping check.")
            return None, None

        reddit = praw.Reddit(
            client_id=settings.get('reddit_client_id'),
            client_secret=settings.get('reddit_client_secret'),
            user_agent="Gamearr v1.0 (by u/YourUsername)", # A descriptive user agent is required
            username=settings.get('reddit_username'),
            password=settings.get('reddit_password')
        )

        redditor = reddit.redditor('EssenseOfMagic')
        
        for submission in redditor.submissions.new(limit=100):
            if "Daily Releases" in submission.title:
                pattern = re.compile(r"^\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|", re.MULTILINE)
                matches = pattern.findall(submission.selftext)
                
                # --- THIS IS THE NEW, STRICTER LOGIC ---
                # Prepare a set of keywords from our official game title
                normalized_title = game_title.replace(':', '').lower()
                title_keywords = set(normalized_title.split())
                # --- END OF NEW LOGIC ---

                for game_name, group_name in matches:
                    # --- APPLY THE NEW LOGIC HERE ---
                    # Prepare a set of keywords from the release found on Reddit
                    cleaned_reddit_title_set = set(game_name.strip().replace('.', ' ').replace('_', ' ').lower().split())
                    
                    # Check if all of our game's keywords are present in the Reddit title
                    if title_keywords.issubset(cleaned_reddit_title_set):
                        print(f"       --> Found Reddit match in post '{submission.title}': {game_name.strip()} by {group_name.strip()}")
                        return game_name.strip(), group_name.strip()

    except Exception as e:
        print(f"    -> ERROR: Reddit check failed: {e}")
    
    return None, None

def check_reddit_deep_search(game_title, settings):
    """
    Performs a deep, historical search on Reddit for older releases.
    This is a slow, 'last resort' check.
    """
    print(f"    -> Performing Reddit deep search for '{game_title}'...")
    try:
        if not all([settings.get('reddit_client_id'), settings.get('reddit_client_secret'),
                    settings.get('reddit_username'), settings.get('reddit_password')]):
            print("    -> Reddit credentials not configured. Skipping deep search.")
            return None, None

        reddit = praw.Reddit(
            client_id=settings.get('reddit_client_id'),
            client_secret=settings.get('reddit_client_secret'),
            user_agent="Gamearr v1.0 Deep Search",
            username=settings.get('reddit_username'),
            password=settings.get('reddit_password')
        )

        # The subreddit where EssenseOfMagic posts
        target_subreddit = "CrackWatch"
        # This query is the key: it searches for posts by the author that contain our game title
        search_query = f'author:EssenseOfMagic "{game_title}"'
        
        print(f"       -> Searching 'r/{target_subreddit}' with query: '{search_query}'")
        subreddit = reddit.subreddit(target_subreddit)
        
        # Search a deep backlog of posts
        search_results = subreddit.search(search_query, sort='new', limit=100)

        for submission in search_results:
            # We can add a quick filter to ensure it's a release post
            if "daily releases" not in submission.title.lower():
                continue

            # Parse the markdown table in the post body
            pattern = re.compile(r"^\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|", re.MULTILINE)
            matches = pattern.findall(submission.selftext)

            for game_name, group_name in matches:
                # If Reddit's search found the post, a simple 'in' check is a good final validation
                if game_title.lower() in game_name.strip().lower():
                    print(f"       --> Found historical match in post '{submission.title}': {game_name.strip()} by {group_name.strip()}")
                    return game_name.strip(), group_name.strip()

    except Exception as e:
        print(f"    -> ERROR: Reddit deep search failed: {e}")
    
    return None, None # Return None if no match is found

def check_for_releases():
    """
    The main background job. Loops through all monitored games and calls the
    unified search engine for each one.
    """
    print("Scheduler: Running multi-tier release check.")
    with app.app_context():
        conn = get_db_connection()
        games_to_monitor = conn.execute("SELECT * FROM games WHERE status = 'Monitoring'").fetchall()

        if not games_to_monitor:
            conn.close()
            return
        
        print(f"Scheduler: Monitoring {len(games_to_monitor)} games.")

        for game in games_to_monitor:
            find_release_for_game(game, conn)
            time.sleep(1)
        
        conn.close()

def check_single_game_release(game_id):
    """
    The instant check. Gets a single game from the DB and calls the
    unified search engine for it.
    """
    print(f"Performing instant check for newly added game (ID: {game_id}).")
    with app.app_context():
        conn = get_db_connection()
        game = conn.execute('SELECT * FROM games WHERE id = ?', (game_id,)).fetchone()

        if game:
            find_release_for_game(game, conn)
        
        conn.close()

def check_source_rss(feed_url, game_title):
    """Checks a generic RSS feed with a 'fuzzy' keyword matching logic."""
    print(f"    -> Checking RSS Feed '{feed_url}' for '{game_title}'...")
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
                    print(f"        --> Fuzzy match found! Matched {len(matching_keywords)}/{len(title_keywords)} keywords.")
                    return entry.title
    except Exception as e:
        print(f"    ERROR: RSS feed check for '{feed_url}' failed: {e}")
    return None


def fetch_and_save_nfo(release_name):
    """
    Fetches NFO data and image, correctly parsing the dictionary response from the API.
    """
    print(f"NFO Fetcher: Attempting to fetch NFO for '{release_name}'")
    
    try:
        url = "https://api.predb.net/"
        params = {'type': 'nfo', 'release': release_name}
        
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        
        json_data = response.json()

        # --- THIS IS THE FINAL, CORRECT FIX ---
        # 1. The top-level response is a dictionary. We get the 'data' dictionary from it.
        nfo_data_dict = json_data.get('data')

        # 2. Check if the 'data' dictionary exists and is not empty.
        if not nfo_data_dict or not isinstance(nfo_data_dict, dict):
            print("NFO Fetcher: No 'data' object found in the API response.")
            return None, None
        
        # 3. Get the URLs directly from the dictionary keys.
        nfo_url = nfo_data_dict.get('nfo')
        nfo_img_url = nfo_data_dict.get('nfo_img')
        # --- END OF FIX ---

        # The rest of the function remains the same, it will now work correctly.
        nfo_storage_path = os.path.join(app.root_path, 'nfo_storage')
        os.makedirs(nfo_storage_path, exist_ok=True)
        
        local_nfo_path = None
        local_nfo_img_path = None

        if nfo_url:
            nfo_response = requests.get(nfo_url, timeout=10)
            if nfo_response.status_code == 200:
                safe_filename = "".join(c for c in release_name if c.isalnum() or c in ('_','-')).rstrip()
                local_nfo_path = os.path.join(nfo_storage_path, f"{safe_filename}.nfo")
                with open(local_nfo_path, 'w', encoding='utf-8', errors='ignore') as f:
                    f.write(nfo_response.text)
                print(f"NFO Fetcher: Saved .nfo file to {local_nfo_path}")

        if nfo_img_url:
            nfo_img_response = requests.get(nfo_img_url, timeout=10)
            if nfo_img_response.status_code == 200:
                safe_filename = "".join(c for c in release_name if c.isalnum() or c in ('_','-')).rstrip()
                local_nfo_img_path = os.path.join(nfo_storage_path, f"{safe_filename}.png")
                with open(local_nfo_img_path, 'wb') as f:
                    f.write(nfo_img_response.content)
                print(f"NFO Fetcher: Saved .png image to {local_nfo_img_path}")
        
        db_nfo_path = os.path.relpath(local_nfo_path, nfo_storage_path) if local_nfo_path else None
        db_nfo_img_path = os.path.relpath(local_nfo_img_path, nfo_storage_path) if local_nfo_img_path else None
        
        return db_nfo_path, db_nfo_img_path

    except Exception as e:
        print(f"NFO Fetcher: An error occurred: {e}")
        return None, None

def update_download_statuses():
    """Scheduled job to check qBittorrent for download progress and handle deletions."""
    print("\n--- [START] Running job 'update_download_statuses' ---")
    with app.app_context():
        conn = get_db_connection()
        settings = {row['key']: row['value'] for row in conn.execute('SELECT * FROM settings').fetchall()}

        # We need to track 'Stalledup' etc. to fix them, so we broaden the search for now.
        # A better long-term solution might be to have a separate "is_active" flag.
        games_to_track = conn.execute(
            "SELECT * FROM games WHERE status NOT IN ('Monitoring', 'Cracked', 'Imported') AND torrent_hash IS NOT NULL"
        ).fetchall()

        if not games_to_track:
            print("[INFO] No active downloads to track. Job finished.")
            print("--- [END] Job 'update_download_statuses' ---\n")
            conn.close()
            return

        print(f"[INFO] Found {len(games_to_track)} game(s) to track.")
        db_hashes = {game['torrent_hash'] for game in games_to_track}
        hash_to_game_id = {game['torrent_hash']: game['id'] for game in games_to_track}
        
        try:
            client = Client(
                host=settings.get('qbittorrent_host'),
                port=settings.get('qbittorrent_port'),
                username=settings.get('qbittorrent_user'),
                password=settings.get('qbittorrent_pass')
            )
            client.auth_log_in()
            
            torrents_info = client.torrents_info(torrent_hashes=list(db_hashes))
            active_hashes = {t.hash for t in torrents_info}

            for torrent in torrents_info:
                game_id = hash_to_game_id[torrent.hash]
                progress = torrent.progress * 100
                
                # --- VERBOSE LOGGING ---
                print(f"    [TRACKING] Hash: {torrent.hash}")
                print(f"        -> qBit State: '{torrent.state}'")
                print(f"        -> qBit Progress: {torrent.progress}")
                
                new_status = ""

                # --- REVISED LOGIC ---
                # This is the most important check. If progress is 100%, the status
                # MUST be 'Downloaded' for the next job to pick it up.
                # Using >= 1 is safer for floating point numbers.
                if torrent.progress >= 1:
                    new_status = "Downloaded"
                    print(f"        -> LOGIC: Progress is >= 1. Setting status to '{new_status}'.")
                elif torrent.state == 'metaDL':
                    new_status = "Downloading Metadata"
                elif torrent.state in ['downloading', 'pausedDL']:
                    new_status = f"Downloading {progress:.0f}%"
                elif torrent.state == 'error':
                    new_status = "Error"
                else: # Covers other states like 'stalledDL', 'queuedDL', etc.
                    # This now correctly ignores seeding states like 'stalledUP'
                    new_status = f"{torrent.state.capitalize()}"
                    print(f"        -> LOGIC: Unhandled state. Setting status to '{new_status}'.")

                if new_status:
                    conn.execute("UPDATE games SET status = ? WHERE id = ?", (new_status, game_id))
                    conn.commit()
                    print(f"        -> DB UPDATE: Set status to '{new_status}' for game ID {game_id}.")

            # Handle torrents that were deleted from the client
            deleted_hashes = db_hashes - active_hashes
            if deleted_hashes:
                print(f"    [INFO] Detected {len(deleted_hashes)} torrent(s) missing from client.")
                for dead_hash in deleted_hashes:
                    game_id = hash_to_game_id[dead_hash]
                    conn.execute(
                        "UPDATE games SET status = 'Cracked', torrent_hash = NULL WHERE id = ?", 
                        (game_id,)
                    )
                    conn.commit()
                    print(f"        -> Reset status for game ID {game_id} (hash: {dead_hash}).")

        except Exception as e:
            print(f"[ERROR] An error occurred in update_download_statuses: {e}")
        finally:
            conn.close()
    
    print("--- [END] Job 'update_download_statuses' ---\n")


def process_search_tasks():
    """Scheduled job that finds PENDING search tasks and executes them."""
    with app.app_context():
        conn = get_db_connection()
        # Find the oldest pending task
        task = conn.execute("SELECT * FROM search_tasks WHERE status = 'PENDING' ORDER BY created_at ASC LIMIT 1").fetchone()

        if task:
            print(f"Background search: Found pending task {task['id']} for term '{task['search_term']}'")
            settings = {row['key']: row['value'] for row in conn.execute('SELECT * FROM settings').fetchall()}
            
            # Perform the slow API call
            igdb_results = search_igdb(task['search_term'], settings)

            # Save the results back to the database
            conn.execute(
                "UPDATE search_tasks SET status = ?, results = ? WHERE id = ?",
                ("COMPLETE", json.dumps(igdb_results), task['id'])
            )
            conn.commit()
            print(f"Background search: Task {task['id']} complete.")
        
        conn.close()

def get_qbit_client():
    """Helper function to get an authenticated qBittorrent client."""
    conn = get_db_connection()
    settings = {row['key']: row['value'] for row in conn.execute('SELECT * FROM settings').fetchall()}
    conn.close()
    
    client = Client(
        host=settings.get('qbittorrent_host'),
        port=settings.get('qbittorrent_port'),
        username=settings.get('qbittorrent_user'),
        password=settings.get('qbittorrent_pass')
    )
    client.auth_log_in()
    return client

def process_completed_downloads():
    """
    Scheduled job to perform post-processing on downloaded games.
    - Copies specified asset files (.nfo, etc.) from the source download folder
      to the final, extracted game directory.
    - Updates the game status to 'Imported'.
    """
    print("Scheduler: Running job 'process_completed_downloads'.")
    
    with app.app_context():
        conn = get_db_connection()
        games_to_process = conn.execute(
            "SELECT * FROM games WHERE status = 'Downloaded' AND release_name IS NOT NULL"
        ).fetchall()

        if not games_to_process:
            conn.close()
            return # Exit silently if there's nothing to do
        
        print(f"Post-processor: Found {len(games_to_process)} downloaded game(s) to process.")
        
        # Read paths from environment variables with Docker-friendly defaults
        downloads_path_str = os.getenv('DOWNLOADS_PATH', '/games/_downloads')
        library_path_str = os.getenv('LIBRARY_PATH', '/games')

        downloads_base = Path(downloads_path_str)
        library_base = Path(library_path_str)
        ASSET_EXTENSIONS = {'.nfo', '.sfv', '.jpg', '.png'}
        
        for game in games_to_process:
            release_name = game['release_name'].strip()
            print(f"  -> Processing '{game['official_title']}' ({release_name})")

            source_folder = downloads_base / release_name
            dest_folder = library_base / release_name
            
            # --- Key validation checks ---
            if not source_folder.is_dir():
                print(f"     [ERROR] Source folder not found: {source_folder}. Skipping.")
                continue
                
            if not dest_folder.is_dir():
                print(f"     [INFO] Destination folder not found. Unpackerr may not be finished. Will retry later.")
                continue
                
            try:
                files_copied_count = 0
                for item in source_folder.iterdir():
                    if item.is_file() and item.suffix.lower() in ASSET_EXTENSIONS:
                        dest_file = dest_folder / item.name
                        shutil.copy2(item, dest_file)
                        files_copied_count += 1
                
                if files_copied_count > 0:
                    print(f"     -> Copied {files_copied_count} asset file(s).")
                
                # --- Final Status Update ---
                conn.execute("UPDATE games SET status = 'Imported' WHERE id = ?", (game['id'],))
                conn.commit()
                print(f"     -> Status updated to 'Imported'.")

            except Exception as e:
                print(f"     [ERROR] An unexpected error occurred while processing '{release_name}': {e}")
        
        conn.close()
    print("Scheduler: Job 'process_completed_downloads' finished.")

# --- Routes ---
@app.route('/')
def index():
    conn = get_db_connection()
    games = conn.execute('SELECT * FROM games ORDER BY id DESC').fetchall()
    conn.close()
    return render_template('index.html', games=games)

@app.route('/add_game', methods=['POST'])
def add_game():
    game_title = request.form['game_title']
    if not game_title:
        return redirect(url_for('index')) # Or show an error

    conn = get_db_connection()
    # Just add the game with a "Monitoring" status. The background job will do the rest.
    conn.execute(
        'INSERT INTO games (title, status) VALUES (?, ?)',
        (game_title, 'Monitoring')
    )
    conn.commit()
    conn.close()
    return redirect(url_for('index'))

@app.route('/add', methods=['GET', 'POST'])
def add_game_search():
    """Creates a search task and redirects to the results page."""
    if request.method == 'POST':
        search_term = request.form['game_title']
        
        # Create a new task in the database
        task_id = str(uuid.uuid4())
        conn = get_db_connection()
        conn.execute(
            "INSERT INTO search_tasks (id, search_term, status) VALUES (?, ?, ?)",
            (task_id, search_term, 'PENDING')
        )
        conn.commit()
        conn.close()
        
        # Immediately redirect the user to the results page for this new task
        return redirect(url_for('show_search_results', task_id=task_id))
    
    # If it's a GET request, just show the initial search form
    return render_template('add_game_form.html') # We'll rename the template

@app.route('/settings', methods=['GET'])
def settings():
    """Displays the settings page."""
    conn = get_db_connection()
    # Fetch all settings from the DB and turn them into a dictionary for easy use
    settings_data = {row['key']: row['value'] for row in conn.execute('SELECT * FROM settings').fetchall()}
    conn.close()
    return render_template('settings.html', settings=settings_data)

@app.route('/activity')
def activity_page():
    """Serves the main HTML page for the Activity dashboard."""
    return render_template('activity.html')

@app.route('/activity/data')
def activity_data():
    """
    This JSON API endpoint polls qBittorrent for live data,
    now correctly filtering by the configured category.
    """
    conn = get_db_connection()
    settings = {row['key']: row['value'] for row in conn.execute('SELECT * FROM settings').fetchall()}
    conn.close()

    # Get the category from settings to use as our filter
    gamearr_category = settings.get('qbittorrent_category')
    if not gamearr_category:
        print("Activity Page: qBittorrent category is not set in settings.")
        return {'torrents': []}

    torrents_data = []
    try:
        # We can reuse our helper function to get an authenticated client
        client = get_qbit_client()
        
        # --- THIS IS THE FIX ---
        # Instead of getting hashes from our DB, we directly ask qBittorrent
        # for all torrents that match our specific category.
        torrents_info = client.torrents_info(category=gamearr_category)
        # --- END OF FIX ---

        # We can also fetch our local game data to match friendly names
        conn = get_db_connection()
        hash_to_title = {game['torrent_hash']: game['official_title'] 
                         for game in conn.execute("SELECT torrent_hash, official_title FROM games").fetchall()}
        conn.close()

        for t in torrents_info:
            torrents_data.append({
                'hash': t.hash,
                'name': t.name,
                # Use the friendly name from our DB if we have it, otherwise default to the torrent name
                'friendly_name': hash_to_title.get(t.hash, t.name),
                'state': t.state.upper(),
                'progress': f"{t.progress * 100:.1f}%",
                'size': f"{t.size / (1024**3):.2f} GB",

                # ... (the rest of the data fields are the same) ...
                'downloaded': f"{t.downloaded / (1024**3):.2f} GB",
                'uploaded': f"{t.uploaded / (1024**3):.2f} GB",
                'ratio': f"{t.ratio:.2f}",
                'dlspeed': f"{t.dlspeed / (1024**2):.2f} MB/s",
                'upspeed': f"{t.upspeed / (1024**2):.2f} MB/s",
                'time_active': str(timedelta(seconds=t.time_active)),
                'seeding_time': str(timedelta(seconds=t.seeding_time))
            })

    except Exception as e:
        print(f"Error fetching activity data from qBittorrent: {e}")
        return {'error': str(e)}, 500
        
    # Sort the data by progress for a cleaner UI
    sorted_torrents = sorted(torrents_data, key=lambda x: x['progress'], reverse=True)
    return {'torrents': sorted_torrents}



@app.route('/activity/pause', methods=['POST'])
def pause_torrent():
    tor_hash = request.form.get('hash')
    client = get_qbit_client()
    client.torrents_pause(torrent_hashes=tor_hash)
    print(f"Paused torrent: {tor_hash}")
    return redirect(url_for('activity_page'))

@app.route('/activity/resume', methods=['POST'])
def resume_torrent():
    tor_hash = request.form.get('hash')
    client = get_qbit_client()
    client.torrents_resume(torrent_hashes=tor_hash)
    print(f"Resumed torrent: {tor_hash}")
    return redirect(url_for('activity_page'))

@app.route('/activity/delete', methods=['POST'])
def delete_torrent():
    tor_hash = request.form.get('hash')
    delete_files = request.form.get('delete_files') == 'true'
    
    client = get_qbit_client()
    client.torrents_delete(delete_files=delete_files, torrent_hashes=tor_hash)
    print(f"Deleted torrent: {tor_hash} (Delete files: {delete_files})")
    
    # Also remove the hash from our local DB so we stop tracking it
    conn = get_db_connection()
    conn.execute("UPDATE games SET torrent_hash = NULL, status = 'Cracked' WHERE torrent_hash = ?", (tor_hash,))
    conn.commit()
    conn.close()

    return redirect(url_for('activity_page'))

@app.route('/add/results/<task_id>')
def show_search_results(task_id):
    """This is the page the user waits on. It will use JS to fetch results."""
    # We pass the task_id to the template so the JavaScript knows which task to poll
    return render_template('add_game_results.html', task_id=task_id)

@app.route('/add/results/data/<task_id>')
def get_search_results_data(task_id):
    """This route serves the raw JSON data that the JS will fetch."""
    conn = get_db_connection()
    task = conn.execute("SELECT status, results FROM search_tasks WHERE id = ?", (task_id,)).fetchone()
    conn.close()
    
    if task:
        return {'status': task['status'], 'results': json.loads(task['results'] or '[]')}
    else:
        return {'status': 'NOT_FOUND', 'results': []}

@app.route('/settings/save', methods=['POST'])
def save_settings():
    """Saves the submitted settings form to the database."""
    conn = get_db_connection()
    # Loop through all the items in the form
    for key, value in request.form.items():
        # Use INSERT OR REPLACE to either update the existing setting or create it
        conn.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)', (key, value))
    conn.commit()
    conn.close()
    print("Settings saved successfully.")
    # Redirect back to the settings page to show the changes
    return redirect(url_for('settings'))

@app.route('/search/<int:game_id>', methods=['GET', 'POST'])
def interactive_search(game_id):
    conn = get_db_connection()
    game = conn.execute('SELECT * FROM games WHERE id = ?', (game_id,)).fetchone()
    settings = {row['key']: row['value'] for row in conn.execute('SELECT * FROM settings').fetchall()}
    conn.close()

    if game is None:
        return "Game not found", 404

    default_search_term = game['release_name'] if game['release_name'] else game['title']
    search_term = request.form.get('search_term', default_search_term)
    
    jackett_results = search_jackett(search_term, settings)

    return render_template('search.html', game=game, results=jackett_results, search_term=search_term)

@app.route('/download', methods=['POST'])
def download():
    magnet_link = request.form['magnet_link']
    game_id = request.form['game_id']

    conn = get_db_connection()
    settings = {row['key']: row['value'] for row in conn.execute('SELECT * FROM settings').fetchall()}
    
    # The function now returns two values
    success, torrent_hash = add_to_qbittorrent(magnet_link, settings)
    
    if success:
        # We now also save the torrent_hash to the database
        conn.execute("UPDATE games SET status = 'Snatched', torrent_hash = ? WHERE id = ?", (torrent_hash, game_id))
        conn.commit()
    
    conn.close()
    return redirect(url_for('index'))

@app.route('/add/confirm', methods=['POST'])
def add_game_confirm():
    igdb_id = request.form.get('igdb_id')
    official_title = request.form.get('official_title')
    cover_url = request.form.get('cover_url')
    # Handle the timestamp safely
    release_timestamp = request.form.get('release_timestamp')
    release_date = timestamp_to_date_filter(int(release_timestamp)) if release_timestamp and release_timestamp != 'None' else None

    if not igdb_id or not official_title:
        # Handle cases where form data might be missing
        print("Error: Missing igdb_id or official_title in form submission.")
        return redirect(url_for('add_game_search'))

    conn = get_db_connection()
    # Check if a game with this IGDB ID is already being monitored
    exists = conn.execute('SELECT id FROM games WHERE igdb_id = ?', (igdb_id,)).fetchone()
    
    if exists:
        print(f"Game '{official_title}' (IGDB ID: {igdb_id}) is already being monitored.")
    else:
        # We need to get the ID of the new row we are about to insert.
        # We use a cursor to do this.
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO games (igdb_id, official_title, release_date, cover_url, status) 
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                igdb_id,
                official_title,
                release_date,
                cover_url,
                'Monitoring'
            )
        )
        # Get the ID of the row we just created
        new_game_id = cursor.lastrowid
        conn.commit()
        print(f"Added '{official_title}' to the monitor list with ID: {new_game_id}.")
        
        # --- THIS IS THE NEW PART ---
        # Immediately run our targeted check for the new game.
        check_single_game_release(new_game_id)
        # ----------------------------
    
    conn.close()
    return redirect(url_for('index'))

@app.route('/game/delete/<int:game_id>', methods=['POST'])
def delete_game(game_id):
    """Deletes a game from the database."""
    conn = get_db_connection()
    # Execute the DELETE SQL statement for the specific game ID
    conn.execute('DELETE FROM games WHERE id = ?', (game_id,))
    conn.commit()
    conn.close()
    
    print(f"Deleted game with ID: {game_id}")
    
    # Redirect back to the main page to show the updated list
    return redirect(url_for('index'))

@app.route('/nfo/<path:filename>')
def serve_nfo_file(filename):
    """Serves a file from the nfo_storage directory."""
    nfo_storage_path = os.path.join(app.root_path, 'nfo_storage')
    return send_from_directory(nfo_storage_path, filename)

# --- Scheduler Setup ---

# The 'daemon=True' flag ensures that background threads exit when the main app exits.
scheduler = BackgroundScheduler(daemon=True)

# Add the jobs. The 'replace_existing=True' option is the key to preventing duplicates.
scheduler.add_job(
    func=check_for_releases,
    trigger="interval",
    minutes=30,
    id="release_check_job",
    replace_existing=True
)
scheduler.add_job(
    func=update_download_statuses,
    trigger="interval",
    minutes=1,
    id="download_update_job",
    replace_existing=True
)
scheduler.add_job(
    func=process_search_tasks,
    trigger="interval",
    seconds=5, # Run this job very frequently
    id="search_task_job",
    replace_existing=True
)
scheduler.add_job(
    func=process_completed_downloads, # <-- The new job
    trigger="interval",
    minutes=5, # Run every 5 minutes
    id="post_process_job",
    replace_existing=True
)

scheduler.start()

# Shut down the scheduler when the app exits gracefully.
atexit.register(lambda: scheduler.shutdown())