from flask import (
    Blueprint, render_template, request, redirect, url_for, 
    send_from_directory, current_app, jsonify, flash
)
import uuid
import json
import os
from datetime import timedelta
import time

from . import db
from .models import Game, Setting, SearchTask, DiscoverCache
from .services import search_jackett, add_to_qbittorrent, get_qbit_client, process_library_scan, get_igdb_game_details, _refine_search_term

main = Blueprint('main', __name__)

def format_bytes(size):
    """Converts bytes to a human-readable string (KB, MB, GB)."""
    if size == 0:
        return "0 B"
    power = 1024
    n = 0
    power_labels = {0: '', 1: 'K', 2: 'M', 3: 'G', 4: 'T'}
    while size > power and n < len(power_labels):
        size /= power
        n += 1
    return f"{size:.2f} {power_labels[n]}B"

def format_seconds(seconds):
    """Formats a duration in seconds into a human-readable string."""
    if seconds < 0:
        return ""
    days = int(seconds // (24 * 3600))
    hours = int((seconds % (24 * 3600)) // 3600)
    minutes = int((seconds % 3600) // 60)
    
    if days > 0:
        return f"{days}d {hours}h"
    elif hours > 0:
        return f"{hours}h {minutes}m"
    else:
        return f"{minutes}m"

# Main Routes
@main.route('/')
def index():
    query = Game.query

    filter_status = request.args.get('filter_status', 'all')
    sort_by = request.args.get('sort_by', 'id_desc')
    page = request.args.get('page', 1, type=int)

    if filter_status and filter_status != 'all':
        query = query.filter_by(status=filter_status)

    if sort_by == 'title_asc':
        query = query.order_by(Game.official_title.asc())
    elif sort_by == 'release_date_desc':
        query = query.order_by(Game.release_date.desc())
    else: # Default case
        query = query.order_by(Game.id.desc())
        
    all_statuses = [s[0] for s in db.session.query(Game.status).distinct()]

    pagination = query.paginate(page=page, per_page=25, error_out=False) # Using 25 per page is a good number
    games = pagination.items
    
    return render_template(
        'index.html', 
        games=games, 
        pagination=pagination,
        all_statuses=all_statuses,
        current_filter=filter_status,
        current_sort=sort_by
    )

# Add/Search Game Routes
@main.route('/add', methods=['GET', 'POST'])
def add_game_search():
    if request.method == 'POST':
        search_term = request.form['game_title']
        if not search_term:
            flash("Please enter a game title to search.", "warning")
            return render_template('add_game_form.html')
        
        task_id = str(uuid.uuid4())
        new_task = SearchTask(id=task_id, search_term=search_term)
        db.session.add(new_task)
        db.session.commit()
        return redirect(url_for('main.show_search_results', task_id=task_id))
    discover_lists = {}
    cached_items = DiscoverCache.query.all()
    for item in cached_items:
        discover_lists[item.list_name] = json.loads(item.content)
    
    return render_template('add_game_form.html', discover=discover_lists)    

@main.route('/add/results/<task_id>')
def show_search_results(task_id):
    return render_template('add_game_results.html', task_id=task_id)

@main.route('/add/results/data/<task_id>')
def get_search_results_data(task_id):
    task = SearchTask.query.get(task_id)
    if task:
        return jsonify({'status': task.status, 'results': json.loads(task.results or '[]')})
    return jsonify({'status': 'NOT_FOUND', 'results': []}), 404

@main.route('/add/confirm', methods=['POST'])
def add_game_confirm():
    igdb_id = request.form.get('igdb_id')
    if not igdb_id:
        flash("Invalid request. No game ID provided.", "error")
        return redirect(url_for('main.add_game_search'))

    # --- Duplicate Check ---
    exists = Game.query.filter_by(igdb_id=igdb_id).first()
    if exists:
        flash(f"'{exists.official_title}' is already in your library.", "info")
        return redirect(url_for('main.index'))

    # --- Fetch Full Details from API ---
    game_details = get_igdb_game_details(igdb_id)
    if not game_details:
        flash("Could not fetch full details for that game from IGDB.", "error")
        return redirect(url_for('main.add_game_search'))

    # --- Create the New Game ---
    new_game = Game(**game_details, status='Processing')
    
    # --- THIS IS THE CORRECT LOGIC ---
    # Instead of starting a thread, we set our flag to True.
    # This officially adds the task to our reliable database "queue".
    new_game.needs_release_check = True
    
    db.session.add(new_game)
    db.session.commit()

    flash(f"'{new_game.official_title}' has been added and a release check has been queued.", "success")
    
    return redirect(url_for('main.index'))

@main.route('/library/scan', methods=['POST'])
def library_scan():
    """
    Scans the library, parses folder names, and searches IGDB for matches.
    Returns the results as JSON for the frontend to display.
    """
    scan_results = process_library_scan()
    return jsonify(scan_results)

@main.route('/library/import/confirm', methods=['POST'])
def library_import_confirm():
    """Receives a confirmed match from the UI and adds it to the database."""
    data = request.get_json()
    igdb_id = str(data.get('igdb_id'))

    # Check if this game is already in the database
    exists = Game.query.filter(
        (Game.igdb_id == igdb_id) | 
        (Game.local_path == data.get('folder_name'))
    ).first()

    if exists:
        return jsonify({'error': 'Game already exists in the database.'}), 409

    # --- NEW: Fetch the full, rich details from IGDB ---
    game_details = get_igdb_game_details(igdb_id)
    if not game_details:
        return jsonify({'error': 'Could not fetch full game details from IGDB.'}), 500

    # Create the new game using the complete dataset
    new_game = Game(**game_details)
    
    # Set the status and local path specific to an imported game
    new_game.status = 'Imported'
    new_game.local_path = data.get('folder_name')
    
    db.session.add(new_game)
    db.session.commit()
    
    return jsonify({'success': True, 'game_id': new_game.id}), 201


# Manual Search & Download Routes
@main.route('/search/<int:game_id>', methods=['GET', 'POST'])
def interactive_search(game_id):
    game = Game.query.get_or_404(game_id)
    
    # 1. Determine the initial, potentially "raw" term from the request
    initial_term = ""
    if request.method == 'POST':
        # If the form was submitted, its value is the highest priority.
        initial_term = request.form.get('search_term', '').strip()
    else: 
        # For a GET request, use the URL param or fall back to the game's release name.
        initial_term = (
            request.args.get('q') or 
            game.release_name or 
            game.official_title
        )
    
    # 2. Refine the term ONCE. This is the single source of truth now.
    search_term = _refine_search_term(initial_term)
    
    # 3. Use the single refined term for both the Jackett search and for populating the template.
    jackett_results = search_jackett(search_term)
    return render_template('search.html', game=game, results=jackett_results, search_term=search_term)


@main.route('/download', methods=['POST'])
def download():
    game_id = request.form['game_id']
    magnet_link = request.form['magnet_link']
    game = Game.query.get_or_404(game_id)
    
    torrent_hash = add_to_qbittorrent(magnet_link)
    if torrent_hash:
        game.status = 'Snatched'
        game.torrent_hash = torrent_hash
        db.session.commit()
    return redirect(url_for('main.index'))

@main.route('/game/status/<int:game_id>')
def get_game_status(game_id):
    game = Game.query.get_or_404(game_id)
    # Also return the release_name, which can be null
    return jsonify({
        'status': game.status,
        'release_name': game.release_name,
        'slug': game.slug 
    })

# Settings Routes
@main.route('/settings', methods=['GET'])
def settings():
    settings_data = {s.key: s.value for s in Setting.query.all()}
    return render_template('settings.html', settings=settings_data)

@main.route('/settings/save', methods=['POST'])
def save_settings():
    for key, value in request.form.items():
        setting = Setting.query.get(key)
        if setting:
            setting.value = value
        else:
            db.session.add(Setting(key=key, value=value))
    db.session.commit()
    return redirect(url_for('main.settings'))

# Activity Page & API
@main.route('/activity')
def activity_page():
    return render_template('activity.html')

@main.route('/activity/data')
def activity_data():
    try:
        client = get_qbit_client()
        settings = {s.key: s.value for s in Setting.query.all()}
        category = settings.get('qbittorrent_category')
        torrents_info = client.torrents_info(category=category) if category else []
        
        hash_to_title = {g.torrent_hash: g.official_title for g in Game.query.filter(Game.torrent_hash.isnot(None)).all()}
        
        torrents_data = []
        for t in torrents_info:
            seeding_time_str = ""
            # Check if the torrent is seeding and has a completion date
            if t.state in ['uploading', 'stalledUP', 'checkingUP', 'forcedUP'] and t.completion_on > 0:
                seeding_duration = time.time() - t.completion_on
                seeding_time_str = format_seconds(seeding_duration)

            torrents_data.append({
                'hash': t.hash,
                'name': t.name,
                'friendly_name': hash_to_title.get(t.hash, t.name),
                'torrent_name': t.name,
                'state': t.state.upper(),
                'progress': f"{t.progress * 100:.1f}%",
                'size': format_bytes(t.size),
                'ratio': f"{t.ratio:.2f}",
                'dlspeed': f"{format_bytes(t.dlspeed)}/s",
                'upspeed': f"{format_bytes(t.upspeed)}/s",
                # --- NEW FIELD FOR SEEDING TIME ---
                'seeding_time': seeding_time_str
            })

        return jsonify({'torrents': sorted(torrents_data, key=lambda x: x['progress'], reverse=True)})
    except Exception as e:
        current_app.logger.error(f"Error fetching activity data: {e}")
        return jsonify({'error': str(e)}), 500

@main.route('/activity/action', methods=['POST'])
def activity_action():
    tor_hash = request.form.get('hash')
    action = request.form.get('action')
    client = get_qbit_client()
    
    if action == 'pause': client.torrents_pause(torrent_hashes=tor_hash)
    elif action == 'resume': client.torrents_resume(torrent_hashes=tor_hash)
    elif action == 'delete':
        delete_files = request.form.get('delete_files') == 'true'
        client.torrents_delete(delete_files=delete_files, torrent_hashes=tor_hash)
        game = Game.query.filter_by(torrent_hash=tor_hash).first()
        if game:
            game.status = 'Cracked'
            game.torrent_hash = None
            db.session.commit()
            
    return redirect(url_for('main.activity_page'))

# Game & File Routes
@main.route('/game/delete/<int:game_id>', methods=['POST'])
def delete_game(game_id):
    game = Game.query.get_or_404(game_id)
    db.session.delete(game)
    db.session.commit()
    return redirect(url_for('main.index'))

@main.route('/game/<int:game_id>')
def game_detail(game_id):
    """Displays the detailed page for a single game."""
    game = Game.query.get_or_404(game_id)
    # The 'additional_releases' relationship is automatically available here
    return render_template('game_detail.html', game=game)

@main.route('/nfo/<path:filename>')
def serve_nfo_file(filename):
    nfo_storage_path = os.path.join(current_app.root_path, 'nfo_storage')
    return send_from_directory(nfo_storage_path, filename)

