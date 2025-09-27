# /gamearr/app/jobs.py

from flask import current_app
import json
import shutil
from pathlib import Path
import requests
import praw
import re
import html

from . import db
from .models import Game, SearchTask, AdditionalRelease, Setting, Profile
from datetime import datetime, timedelta
import time

from .services import (
    get_settings_dict,
    search_igdb,
    get_qbit_client,
    update_discover_lists,
    process_all_releases_for_game,
    search_jackett,
    add_to_qbittorrent,
    process_and_import_game,
    process_and_import_addon
)

def check_for_releases(app):
    """
    Scheduled job with INTELLIGENT MONITORING to find releases.
    This now correctly calls the new, unified release processing engine.
    """
    with app.app_context():
        app.logger.info("Scheduler: Running INTELLIGENT release check.")
        
        games_to_monitor = Game.query.filter_by(status='Monitoring').all()
        if not games_to_monitor:
            return

        today = datetime.utcnow().date()
        hot_release_window_days = 30
        
        run_backlog_check = False
        backlog_check_interval_hours = 23
        last_check_setting = Setting.query.get('last_backlog_check_timestamp')
        
        if not last_check_setting:
            app.logger.info("    -> No last backlog check time found. Will run the backlog check now.")
            run_backlog_check = True
            last_check_setting = Setting(key='last_backlog_check_timestamp', value='0')
            db.session.add(last_check_setting)
        else:
            last_check_time = float(last_check_setting.value)
            hours_since_last_check = (time.time() - last_check_time) / 3600
            if hours_since_last_check > backlog_check_interval_hours:
                app.logger.info(f"    -> It has been {hours_since_last_check:.1f} hours since the last backlog check. Running it now.")
                run_backlog_check = True
            else:
                app.logger.info(f"    -> It has only been {hours_since_last_check:.1f} hours. Skipping daily backlog check.")

        for game in games_to_monitor:
            if not game.release_date:
                continue
            try:
                game_release_date = datetime.strptime(game.release_date, '%Y-%m-%d').date()
            except ValueError:
                app.logger.warning(f"    -> Skipping '{game.official_title}' due to invalid date format: {game.release_date}")
                continue

            if game_release_date > today:
                continue
            elif (today - game_release_date).days <= hot_release_window_days:
                app.logger.info(f"    -> Checking 'hot' release: '{game.official_title}'")
                process_all_releases_for_game(game.id)
            elif run_backlog_check:
                app.logger.info(f"    -> Checking 'backlog' release: '{game.official_title}'")
                process_all_releases_for_game(game.id)

            time.sleep(2)    

        if run_backlog_check:
            last_check_setting.value = str(time.time())
            app.logger.info("    -> Updating last backlog check timestamp to now.")
        
        db.session.commit()

def process_release_check_queue(app):
    """
    Looks for games that have been flagged for an immediate release check,
    processes one, and then un-flags it.

    FIXED: Now includes proper session management to avoid stale reads.
    """
    with app.app_context():
        try:
            # Query for a game that needs a check
            game_to_check = Game.query.filter_by(needs_release_check=True).order_by(Game.id).first()
            
            if game_to_check:
                app.logger.info(f"Task Queue: Claiming '{game_to_check.official_title}' for an immediate release check.")
                
                # Immediately flip the flag and commit so other workers don't grab the same job.
                game_to_check.needs_release_check = False
                db.session.commit()
                
                # Now, run the potentially long-running process.
                process_all_releases_for_game(game_to_check.id)
            # Optional: Add an else block for debugging to confirm the job is running
            # else:
            #     app.logger.info("Task Queue: No games found needing an immediate release check.")

        finally:
            # THIS IS THE CRITICAL FIX:
            # This ensures the database session is closed and removed at the end of the
            # job's execution. The next run will get a fresh session.
            db.session.remove()

def scan_all_library_games(app):
    """
    Scheduled job that runs the unified scanner on ALL library games
    to find new base games and add-ons.
    """
    with app.app_context():
        app.logger.info("Scheduler: Running full library scan for ALL releases.")
        library_statuses = ['Imported', 'Downloaded', 'Cracked (Scene)', 'Cracked (P2P)']
        
        offset = 0
        batch_size = 50
        
        while True:
            games_in_batch = Game.query.filter(Game.status.in_(library_statuses)).limit(batch_size).offset(offset).all()
            if not games_in_batch:
                app.logger.info("Scheduler: Finished full library scan.")
                break
            
            app.logger.info(f"    -> Processing batch of {len(games_in_batch)} games (offset: {offset}).")
            
            for game in games_in_batch:
                # Call the single, powerful, unified engine for each game
                process_all_releases_for_game(game.id)
                time.sleep(2)
            
            offset += batch_size

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
    """Scheduled job to check qBittorrent for download progress for BOTH games and addons."""
    with app.app_context():
        # --- Find items to track ---
        games_to_track = Game.query.filter(
            Game.status.notin_(['Monitoring', 'Cracked', 'Imported']),
            Game.torrent_hash.isnot(None)
        ).all()
        
        addons_to_track = AdditionalRelease.query.filter(
            AdditionalRelease.status.notin_(['Not Snatched', 'Imported']),
            AdditionalRelease.torrent_hash.isnot(None)
        ).all()
        
        if not games_to_track and not addons_to_track:
            return

        # --- Create lookup dictionaries ---
        items_by_hash = {}
        for g in games_to_track:
            items_by_hash[g.torrent_hash] = g
        for a in addons_to_track:
            items_by_hash[a.torrent_hash] = a

        try:
            client = get_qbit_client()
            torrents_info = client.torrents_info(torrent_hashes=list(items_by_hash.keys()))
            active_hashes = {t.hash for t in torrents_info}

            for torrent in torrents_info:
                item = items_by_hash.get(torrent.hash)
                if not item: continue
                
                # Skip items that are already fully processed
                if item.status in ['Imported']:
                    continue

                new_status = item.status
                if torrent.progress >= 1:
                    new_status = "Downloaded"
                elif torrent.state in ['downloading', 'pausedDL', 'metaDL', 'stalledDL']:
                    new_status = f"Downloading {torrent.progress * 100:.0f}%"
                elif torrent.state == 'error':
                    new_status = "Error"
                
                if item.status != new_status:
                    item.status = new_status
            
            # --- Handle deleted torrents for both types ---
            deleted_hashes = set(items_by_hash.keys()) - active_hashes
            for dead_hash in deleted_hashes:
                item = items_by_hash.get(dead_hash)
                if not item: continue

                if isinstance(item, Game):
                    # Revert a Game to its "Cracked" status
                    if item.release_type == 'Scene':
                        item.status = 'Cracked (Scene)'
                    else:
                        item.status = 'Cracked (P2P)'
                elif isinstance(item, AdditionalRelease):
                    # Revert an Addon to "Not Snatched"
                    item.status = 'Not Snatched'
                
                item.torrent_hash = None
            
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            app.logger.error(f"Error in update_download_statuses: {e}")

def auto_download_snatcher(app):
    """
    Scheduled job to automatically find and download releases based on user profiles.
    This version includes a "claiming" mechanism to prevent race conditions.
    """
    with app.app_context():
        settings = get_settings_dict()
        if settings.get('auto_download_enabled') != 'true':
            return

        games_to_snatch = Game.query.join(Profile).filter(
            Game.status.in_(['Cracked (Scene)', 'Cracked (P2P)'])
        ).all()
        
        if not games_to_snatch:
            return

        app.logger.info(f"Auto-Snatcher: Found {len(games_to_snatch)} candidate(s) for automatic download.")

        for game in games_to_snatch:
            # --- THIS IS THE FIX: Claim the game immediately ---
            original_status = game.status
            game.status = 'Snatching'
            db.session.commit()
            app.logger.info(f"    -> Claiming '{game.official_title}' for snatching.")
            # --- END OF FIX ---
            
            profile = game.profile
            now = int(time.time())
            
            # 1. Check Delay
            if game.release_found_timestamp:
                elapsed_seconds = now - game.release_found_timestamp
                if elapsed_seconds < (profile.delay_hours * 3600):
                    app.logger.info(f"    -> '{game.official_title}' is waiting for delay. Reverting status.")
                    game.status = original_status # Revert status if it's too early
                    db.session.commit()
                    continue
            
            # ... (Candidate building logic is unchanged) ...
            candidates = []
            if game.release_name:
                 candidates.append({'name': game.release_name, 'group': game.release_group, 'type': game.release_type})
            for alt in game.alternative_releases:
                release_type = 'Repack' if alt.source.upper() in {'FITGIRL', 'DODI', 'ELAMIGOS'} else 'P2P'
                if alt.source.upper() not in {'FITGIRL', 'DODI', 'ELAMIGOS'}:
                    if '-' in alt.release_name and ' ' not in alt.release_name.split('-')[-1]:
                         release_type = 'Scene'
                candidates.append({'name': alt.release_name, 'group': alt.source, 'type': release_type})
            
            # ... (Filtering and scoring logic is unchanged) ...
            best_candidate = None
            highest_score = -1
            profile_types = json.loads(profile.release_types)
            profile_preferred = [g.upper() for g in json.loads(profile.preferred_groups)]
            profile_avoided = [g.upper() for g in json.loads(profile.avoided_groups)]
            for cand in candidates:
                cand_group_upper = cand['group'].upper()
                if cand['type'] not in profile_types: continue
                if cand_group_upper in profile_avoided: continue
                score = 0
                if cand_group_upper in profile_preferred: score += 100
                if score > highest_score:
                    highest_score = score
                    best_candidate = cand

            # 4. Snatch the Best Match
            if best_candidate:
                app.logger.info(f"    -> Match found for '{game.official_title}': '{best_candidate['name']}' based on profile '{profile.name}'")
                jackett_results = search_jackett(best_candidate['name'])
                
                if jackett_results:
                    valid_torrents = [t for t in jackett_results if t.get('seeders', 0) != 0]
                    if not valid_torrents:
                         app.logger.warning(f"    -> No results with seeders found for '{best_candidate['name']}'. Reverting status.")
                         game.status = original_status # Revert status
                         db.session.commit()
                         continue

                    best_torrent = sorted(valid_torrents, key=lambda t: 9999 if t.get('seeders') == -1 else t.get('seeders', 0), reverse=True)[0]
                    magnet_link = best_torrent['link']
                    
                    torrent_hash = add_to_qbittorrent(magnet_link)
                    if torrent_hash:
                        game.status = 'Snatched'
                        game.torrent_hash = torrent_hash
                        db.session.commit()
                        app.logger.info(f"    SUCCESS: Snatched '{game.official_title}' and sent to qBittorrent.")
                        time.sleep(1)
                    else:
                        app.logger.error(f"    ERROR: Failed to send '{game.official_title}' to qBittorrent. Reverting status.")
                        game.status = original_status # Revert status
                        db.session.commit()
                else:
                    app.logger.warning(f"    -> No results from Jackett for '{best_candidate['name']}'. Reverting status.")
                    game.status = original_status # Revert status
                    db.session.commit()
            else:
                app.logger.info(f"    -> No releases for '{game.official_title}' matched profile. Reverting status.")
                game.status = original_status # Revert status
                db.session.commit()

def process_completed_downloads(app):
    """
    Scheduled job to find 'Downloaded' games OR addons and hand them off to the correct importer.
    Processes one item per run to avoid overload.
    """
    with app.app_context():
        # --- THIS IS THE NEW LOGIC ---
        settings = get_settings_dict()
        import_mode = settings.get('import_mode', 'Move') # Default to 'Move' if not set

        if import_mode == 'None':
            # If the importer is disabled, do nothing.
            return 
        # --- END OF NEW LOGIC ---

        # --- Priority 1: Process a completed base game ---
        game_to_process = Game.query.filter_by(status='Downloaded').first()
        if game_to_process:
            app.logger.info(f"Post-Processor: Found downloaded game '{game_to_process.official_title}'. Claiming...")
            game_to_process.status = 'Importing'
            db.session.commit()
            try:
                process_and_import_game(game_to_process.id)
            except Exception as e:
                app.logger.error(f"A critical error occurred during game import for '{game_to_process.official_title}'. Reverting status. Error: {e}")
                game = Game.query.get(game_to_process.id)
                if game and game.status == 'Importing': # Check status to avoid race conditions
                    game.status = 'Downloaded'
                    db.session.commit()
            return # Exit after processing one item

        # --- Priority 2: Process a completed addon ---
        addon_to_process = AdditionalRelease.query.filter_by(status='Downloaded').first()
        if addon_to_process:
            app.logger.info(f"Post-Processor: Found downloaded addon '{addon_to_process.release_name}'. Claiming...")
            addon_to_process.status = 'Installing'
            db.session.commit()
            try:
                process_and_import_addon(addon_to_process.id)
            except Exception as e:
                # The addon engine handles its own status reversion
                app.logger.error(f"A critical error occurred during addon import for '{addon_to_process.release_name}'.")
            return # Exit after processing one item

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