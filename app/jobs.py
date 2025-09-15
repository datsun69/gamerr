# /gamearr/app/jobs.py

from flask import current_app
import json
import shutil
from pathlib import Path

from . import db
from .models import Game, SearchTask
from .services import find_release_for_game, search_igdb, get_qbit_client, update_discover_lists
from datetime import datetime, timedelta

def check_for_releases(app):
    """
    Scheduled job with INTELLIGENT MONITORING to find releases.
    - Skips future games.
    - Checks recent games frequently.
    - Checks old games infrequently.
    """
    with app.app_context():
        app.logger.info("Scheduler: Running INTELLIGENT release check.")
        
        games_to_monitor = Game.query.filter_by(status='Monitoring').all()
        if not games_to_monitor:
            return

        today = datetime.utcnow().date()
        hot_release_window_days = 30 # Games released in the last 30 days are "hot"
        
        # We only want to run the slow "backlog" check once per day.
        # A simple way is to only run it during a specific hour (e.g., 3 AM).
        run_backlog_check = (datetime.utcnow().hour == 3)
        if run_backlog_check:
            app.logger.info("    -> It's 3 AM. Running the daily backlog check as well.")

        for game in games_to_monitor:
            # Skip games that don't have a release date from IGDB yet.
            if not game.release_date:
                continue

            try:
                # Convert the database string date into a real date object
                game_release_date = datetime.strptime(game.release_date, '%Y-%m-%d').date()
            except ValueError:
                app.logger.warning(f"    -> Skipping '{game.official_title}' due to invalid date format: {game.release_date}")
                continue

            # --- THE LOGIC ---

            # 1. Skip Future Games
            if game_release_date > today:
                continue # Do nothing, move to the next game

            # 2. Check "Hot" Releases
            elif (today - game_release_date).days <= hot_release_window_days:
                app.logger.info(f"    -> Checking 'hot' release: '{game.official_title}'")
                find_release_for_game(game.id)
            
            # 3. Check Backlog Games (but only once a day)
            elif run_backlog_check:
                app.logger.info(f"    -> Checking 'backlog' release: '{game.official_title}'")
                find_release_for_game(game.id)

def check_single_game_release(app, game_id): # Also needs the app context
    with app.app_context():
        app.logger.info(f"Scheduler: Performing instant check for game ID: {game_id}")
        find_release_for_game(game_id)

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