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
from .models import Game, SearchTask, AdditionalRelease, Setting
from datetime import datetime, timedelta
import time

from .services import (
    get_settings_dict,
    search_igdb,
    get_qbit_client,
    update_discover_lists,
    process_all_releases_for_game,
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
    """
    with app.app_context():
        game_to_check = Game.query.filter_by(needs_release_check=True).first()
        if game_to_check:
            app.logger.info(f"Task Queue: Claiming '{game_to_check.official_title}' for an immediate release check.")
            game_to_check.needs_release_check = False
            db.session.commit()
            
            # Call the single, powerful, unified engine
            process_all_releases_for_game(game_to_check.id)

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

                # SCENARIO 1: The download was already complete. The job is done.
                if game.status == 'Downloaded':
                    app.logger.info(f"Torrent for '{game.official_title}' removed post-download. Final status remains 'Downloaded'.")
                    game.torrent_hash = None
                    continue

                # SCENARIO 2: The download was aborted before completion. Revert to 'Cracked'.
                app.logger.warning(f"Torrent for '{game.official_title}' (Status: {game.status}) removed before completion. Reverting to available state.")
                
                # Use our stored release_type to revert to the correct status
                if game.release_type == 'Scene':
                    game.status = 'Cracked (Scene)'
                else: # 'P2P' and 'Repack' both revert to P2P status
                    game.status = 'Cracked (P2P)'
                
                game.torrent_hash = None
            
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            app.logger.error(f"Error in update_download_statuses: {e}")

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