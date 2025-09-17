# /gamearr/app/jobs.py

from flask import current_app
import json
import shutil
from pathlib import Path
import requests
import praw
import re

from . import db
from .models import Game, SearchTask, AdditionalRelease, Setting
from datetime import datetime, timedelta
import time

from .services import (
    get_settings_dict,
    find_release_for_game,
    search_igdb,
    get_qbit_client,
    update_discover_lists,
    _clean_release_name,
    parse_additional_release_info,
    _get_reddit_instance,
    _parse_reddit_section
)

def check_for_releases(app):
    """
    Scheduled job with INTELLGENT MONITORING to find releases.
    - Skips future games.
    - Checks recent games frequently.
    - Checks old games infrequently based on a persistent timestamp.
    """
    with app.app_context():
        app.logger.info("Scheduler: Running INTELLIGENT release check.")
        
        games_to_monitor = Game.query.filter_by(status='Monitoring').all()
        if not games_to_monitor:
            return

        today = datetime.utcnow().date()
        hot_release_window_days = 30 # Games released in the last 30 days are "hot"
        
        run_backlog_check = False
        backlog_check_interval_hours = 23 # How often the backlog should be checked
        
        # 1. Get the timestamp of the last backlog check from the database.
        last_check_setting = Setting.query.get('last_backlog_check_timestamp')
        
        # If the setting doesn't exist, we should run the check now.
        if not last_check_setting:
            app.logger.info("    -> No last backlog check time found. Will run the backlog check now.")
            run_backlog_check = True
            # Create the setting for the next time.
            last_check_setting = Setting(key='last_backlog_check_timestamp', value='0')
            db.session.add(last_check_setting)
        else:
            # 2. Compare the stored timestamp to the current time.
            last_check_time = float(last_check_setting.value)
            hours_since_last_check = (time.time() - last_check_time) / 3600
            
            if hours_since_last_check > backlog_check_interval_hours:
                app.logger.info(f"    -> It has been {hours_since_last_check:.1f} hours since the last backlog check. Running it now.")
                run_backlog_check = True
            else:
                app.logger.info(f"    -> It has only been {hours_since_last_check:.1f} hours. Skipping daily backlog check.")

        # --- END OF NEW LOGIC ---

        for game in games_to_monitor:
            if not game.release_date:
                continue

            try:
                game_release_date = datetime.strptime(game.release_date, '%Y-%m-%d').date()
            except ValueError:
                app.logger.warning(f"    -> Skipping '{game.official_title}' due to invalid date format: {game.release_date}")
                continue

            # --- THE LOGIC (Now using our new 'run_backlog_check' variable) ---

            # 1. Skip Future Games
            if game_release_date > today:
                continue

            # 2. Check "Hot" Releases (This always runs)
            elif (today - game_release_date).days <= hot_release_window_days:
                app.logger.info(f"    -> Checking 'hot' release: '{game.official_title}'")
                find_release_for_game(game.id)
            
            # 3. Check Backlog Games (This now only runs if our logic above sets it to True)
            elif run_backlog_check:
                app.logger.info(f"    -> Checking 'backlog' release: '{game.official_title}'")
                find_release_for_game(game.id)
        
        # --- THIS IS THE FINAL STEP ---
        # 4. If we ran the backlog check, update the timestamp in the database to the current time.
        if run_backlog_check:
            last_check_setting.value = str(time.time())
            app.logger.info("    -> Updating last backlog check timestamp to now.")
        
        # 5. Commit any changes (either creating the setting or updating its value).
        db.session.commit()

def check_single_game_release(app, game_id): # Also needs the app context
    with app.app_context():
        app.logger.info(f"Scheduler: Performing instant check for game ID: {game_id}")
        find_release_for_game(game_id)

def _scan_single_game_for_additional_content(app, game_id, deep_scan=False):
    """
    The definitive, comprehensive scanner for additional content. This version uses an
    efficient two-stage filter for both speed and precision.
    """
    game = Game.query.get(game_id)
    if not game:
        app.logger.warning(f"Content scanner called with invalid game_id: {game_id}")
        return

    with app.app_context():
        app.logger.info(f"  -> Scanning for additional content for '{game.official_title}'...")
        existing_releases = {release.release_name for release in AdditionalRelease.query.all()}
        
        # --- STEP 1: GATHER POTENTIAL RELEASES WITH A FAST PRE-FILTER ---
        potential_releases = set()
        sanitized_title = ''.join(e for e in game.official_title if e.isalnum() or e.isspace()).strip()
        game_title_keywords = set(sanitized_title.lower().split())

        # Source 1: predb.net (already pre-filtered by its API)
        try:
            target_sections = "GAMES,GAMES-DOX,GAMES-DLC,GAMES-UPDATES,UPDATES"
            params = {'type': 'search', 'q': sanitized_title, 'section': target_sections, 'sort': 'DESC'}
            response = requests.get("https://api.predb.net/", params=params, timeout=10)
            response.raise_for_status()
            results = response.json().get('data', [])
            for release in results:
                potential_releases.add(release.get('release'))
        except Exception as e:
            app.logger.error(f"    -> Error scanning predb.net for '{game.official_title}': {e}")
        
        # Source 2: Reddit (with a new, efficient pre-filter)
        try:
            app.logger.info(f"    -> Scanning Reddit for P2P releases...")
            reddit = _get_reddit_instance()
            if reddit:
                redditor = reddit.redditor('EssenseOfMagic')
                for submission in redditor.submissions.new(limit=300):
                     if "Daily Releases" in submission.title:
                        pattern = re.compile(r"^\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|", re.MULTILINE)
                        matches = pattern.findall(submission.selftext)
                        for game_name, group_name in matches:
                            # --- THIS IS THE EFFICIENT PRE-FILTER ---
                            # Quickly check if the release is even plausible before adding it.
                            # This reduces 6000+ candidates down to a handful.
                            cleaned_release_set = set(game_name.lower().replace('.', ' ').replace('_', ' ').split())
                            if game_title_keywords.issubset(cleaned_release_set):
                                potential_releases.add(game_name.strip())
            app.logger.info(f"    -> Finished scanning all sources.")
        except Exception as e:
            app.logger.error(f"    -> Error scanning Reddit for '{game.official_title}': {e}")


        # --- STEP 2: APPLY THE SINGLE, STRICT FILTER TO THE PRE-FILTERED LIST ---
        app.logger.info(f"    -> Found {len(potential_releases)} plausible releases. Applying precision filter...")
        
        search_phrase = re.sub(r'[:\'!,\[\]]', '', game.official_title.lower()).strip()
        search_phrase = re.sub(r'\s+', ' ', search_phrase)
        ADDITIONAL_CONTENT_KEYWORDS = {'update', 'dlc', 'patch', 'fix', 'hotfix', 'trainer'}
        
        final_releases = set()
        for release_name in potential_releases:
            if not release_name: continue
            normalized_release = release_name.lower().replace('.', ' ').replace('_', ' ').replace('-', ' ')
            normalized_release = re.sub(r'\s+', ' ', normalized_release).strip()

            if search_phrase not in normalized_release:
                continue
            
            release_words = set(normalized_release.split())
            if not any(keyword in release_words for keyword in ADDITIONAL_CONTENT_KEYWORDS):
                continue
            
            final_releases.add(release_name)
        
        # --- STEP 3: PROCESS THE FINAL, FILTERED RELEASES ---
        if not final_releases:
            app.logger.info(f"    -> No new potential releases found for '{game.official_title}' after final filtering.")
            return

        app.logger.info(f"    -> Found {len(final_releases)} valid releases after filtering. Parsing now...")
        new_releases_found_for_this_game = 0
        cleaned_main_release = _clean_release_name(game.release_name)
        
        for release_name in final_releases:
            if release_name in existing_releases: continue
            
            cleaned_found_release = _clean_release_name(release_name)
            if cleaned_found_release == cleaned_main_release and cleaned_main_release != "": continue
            
            release_type = parse_additional_release_info(release_name)
            
            if release_type:
                new_add_release = AdditionalRelease(release_name=release_name, release_type=release_type, game_id=game.id)
                db.session.add(new_add_release)
                existing_releases.add(release_name)
                new_releases_found_for_this_game += 1
                app.logger.info(f"    [FOUND] New {release_type}: {release_name}")
        
        if new_releases_found_for_this_game > 0:
            db.session.commit()

def scan_all_library_games(app):
    """Scheduled job that scans ALL library games in efficient batches."""
    with app.app_context():
        app.logger.info("Scheduler: Running full library scan for additional content.")
        library_statuses = ['Imported', 'Downloaded', 'Definitely Cracked', 'Probably Cracked (P2P)']
        
        offset = 0
        batch_size = 50 # Process 50 games at a time. A good, safe number.
        
        while True:
            # 1. Fetch only one 'batch' of games from the database
            games_in_batch = Game.query.filter(Game.status.in_(library_statuses)).limit(batch_size).offset(offset).all()
            
            # 2. If the batch is empty, we've processed all games. Exit the loop.
            if not games_in_batch:
                app.logger.info("Scheduler: Finished full library scan.")
                break
            
            app.logger.info(f"    -> Processing batch of {len(games_in_batch)} games (offset: {offset}).")
            
            # 3. Process the games in the current batch
            for game in games_in_batch:
                _scan_single_game_for_additional_content(app, game.id)
            
            # 4. Increase the offset to fetch the *next* batch in the next loop iteration
            offset += batch_size
        # --- END OF NEW LOGIC ---

def process_release_check_queue(app):
    """
    Looks for games that have been flagged for an immediate release check,
    processes one, and then un-flags it.
    """
    with app.app_context():
        # Find the first available game that needs a check
        game_to_check = Game.query.filter_by(needs_release_check=True).first()
        
        if game_to_check:
            app.logger.info(f"Task Queue: Claiming '{game_to_check.official_title}' for an immediate release check.")
            
            # --- IMPORTANT ---
            # Un-flag it *immediately* to "claim" it. This prevents other
            # workers from picking up the same job.
            game_to_check.needs_release_check = False
            db.session.commit()
            
            # Now, run the actual (potentially long) task
            find_release_for_game(game_to_check.id)

def process_content_scan_queue(app):
    with app.app_context():
        game_to_scan = Game.query.filter_by(needs_content_scan=True).first()
        if game_to_scan:
            app.logger.info(f"Task Queue: Claiming '{game_to_scan.official_title}' for a ONE-TIME DEEP SCAN.")
            game_to_scan.needs_content_scan = False
            db.session.commit()
            
            # We call the scanner with deep_scan=True
            _scan_single_game_for_additional_content(app, game_to_scan.id, deep_scan=True)

def process_search_tasks(app):
    # THE FIX: We use the passed-in 'app' to create the context,
    # not the problematic 'current_app'.
    with app.app_context():
        task = SearchTask.query.filter_by(status='PENDING').order_by(SearchTask.created_at).first()
        if task:
            app.logger.info(f"Background search: Processing task {task.id} for '{task.search_term}'")
            igdb_results = search_igdb(task.search_term)
            task.results = json.dumps(igdb_results)
            task.status = 'COMPLETE'
            db.session.commit()

def update_download_statuses(app):
    """Scheduled job to check qBittorrent for download progress."""
    with app.app_context():
        games_to_track = Game.query.filter(
            Game.status.notin_(['Monitoring', 'Cracked', 'Imported']),
            Game.torrent_hash.isnot(None)
        ).all()
        
        if not games_to_track:
            return

        games_by_hash = {g.torrent_hash: g for g in games_to_track}
        
        try:
            client = get_qbit_client()
            torrents_info = client.torrents_info(torrent_hashes=list(games_by_hash.keys()))
            active_hashes = {t.hash for t in torrents_info}

            for torrent in torrents_info:
                game = games_by_hash.get(torrent.hash)
                if not game: continue

                new_status = game.status
                if torrent.progress >= 1:
                    new_status = "Downloaded"
                elif torrent.state in ['downloading', 'pausedDL', 'metaDL', 'stalledDL']:
                    new_status = f"Downloading {torrent.progress * 100:.0f}%"
                elif torrent.state == 'error':
                    new_status = "Error"
                else: # Covers seeding, stalledUP, queued, etc.
                    new_status = torrent.state.capitalize()

                if game.status != new_status:
                    game.status = new_status
            
            # Handle deleted torrents
            deleted_hashes = set(games_by_hash.keys()) - active_hashes
            for dead_hash in deleted_hashes:
                game = games_by_hash.get(dead_hash)
                game.status = 'Cracked'
                game.torrent_hash = None
            
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            app.logger.error(f"Error in update_download_statuses: {e}")

def process_completed_downloads(app):
    """Scheduled job to perform post-processing on downloaded games."""
    with app.app_context():
        games_to_process = Game.query.filter_by(status='Downloaded').filter(Game.release_name.isnot(None)).all()
        if not games_to_process:
            return

        current_app.logger.info(f"Post-processor: Found {len(games_to_process)} game(s) to process.")
        
        downloads_base = Path(current_app.config['DOWNLOADS_PATH'])
        library_base = Path(current_app.config['LIBRARY_PATH'])
        ASSET_EXTENSIONS = {'.nfo', '.sfv', '.jpg', '.png'}
        
        for game in games_to_process:
            try:
                source_folder = downloads_base / game.release_name.strip()
                dest_folder = library_base / game.release_name.strip()
                
                if not source_folder.is_dir() or not dest_folder.is_dir():
                    continue
                
                # ... (File copy logic remains the same) ...
                
                game.status = 'Imported'
                db.session.commit()
                current_app.logger.info(f"Successfully processed and imported '{game.release_name}'.")

            except Exception as e:
                db.session.rollback()
                current_app.logger.error(f"Error post-processing '{game.release_name}': {e}")

def refresh_discover_cache(app):
    """Scheduled job to refresh the IGDB discover lists."""
    with app.app_context():
        update_discover_lists()                

def register_cli_commands(app):
    """A function to register our custom commands with Flask."""

    @app.cli.command('update-discover')
    def update_discover_command():
        """Fetches and caches the IGDB Discover lists."""
        current_app.logger.info("--- Manually running Discover list update ---")
        with app.app_context():
            success = update_discover_lists() # Call our existing service function
        if success:
            current_app.logger.info("--- Discover lists updated successfully! ---")
            
        else:
            current_app.logger.info("--- An error occurred during the update. Check logs for details. ---")       

    @app.cli.command('scan-content')
    def scan_content_command():
        """Scans for additional content (DLCs, updates) for ALL library games."""
        print("--- Manually running FULL Additional Content scan ---")
        from flask import current_app
        # It now correctly calls the wrapper function that scans ALL games.
        scan_all_library_games(current_app._get_current_object())
        print("--- Additional Content scan finished. Check logs for details. ---")        