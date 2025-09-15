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
from .services import search_jackett, add_to_qbittorrent, get_qbit_client, process_library_scan
from .jobs import check_single_game_release

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
    games = Game.query.order_by(Game.id.desc()).all()
    return render_template('index.html', games=games)

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
    """
    Handles adding a new game to the library from various sources.
    This is the endpoint for the 'Add' buttons from both Search and Discover.
    """
    igdb_id = request.form.get('igdb_id')
    official_title = request.form.get('official_title')
    slug = request.form.get('slug') # You already have this line, which is great!
    exists = Game.query.filter_by(igdb_id=igdb_id).first()

    # --- Input Validation ---
    if not igdb_id or not official_title:
        flash("Could not add game: Missing required information.", "error")
        return redirect(url_for('main.add_game_search'))

    # --- Duplicate Check ---
    exists = Game.query.filter_by(igdb_id=igdb_id).first()
    if exists:
        flash(f"'{official_title}' is already in your library.", "info")
        return redirect(url_for('main.index'))

    # --- Date Handling ---
    release_date_str = request.form.get('release_date_str')
    release_timestamp = request.form.get('release_timestamp')

    release_date = None
    if release_date_str:
        release_date = release_date_str
    elif release_timestamp and release_timestamp != 'None':
        release_date = current_app.jinja_env.filters['timestamp_to_date'](release_timestamp)

    # --- Create and Save the New Game ---
        new_game = Game(
            igdb_id=igdb_id,
            official_title=official_title,
            slug=slug, # <-- We are now saving the slug to the database model
            cover_url=request.form.get('cover_url'),
            release_date=release_date, # Your date logic variable
            status='Monitoring'
        )
        db.session.add(new_game)
        db.session.commit()

    flash(f"'{official_title}' has been added and is being monitored.", "success")
    
    # --- Trigger an Immediate Release Check ---
    import threading
    thread = threading.Thread(target=check_single_game_release, args=(current_app._get_current_object(), new_game.id))
    thread.start()
    
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

    # Check if this game is already in the database (by igdb_id or path)
    exists = Game.query.filter(
        (Game.igdb_id == str(data.get('igdb_id'))) | 
        (Game.local_path == data.get('folder_name'))
    ).first()

    if exists:
        # Return an error if the game already exists to prevent duplicates
        return jsonify({'error': 'Game already exists in the database.'}), 409

    new_game = Game(
        igdb_id=str(data.get('igdb_id')),
        official_title=data.get('official_title'),
        cover_url=data.get('cover_url'),
        release_date=current_app.jinja_env.filters['timestamp_to_date'](data.get('release_timestamp')),
        status='Imported',
        local_path=data.get('folder_name')
    )
    db.session.add(new_game)
    db.session.commit()
    
    return jsonify({'success': True, 'game_id': new_game.id}), 201

# Manual Search & Download Routes
@main.route('/search/<int:game_id>', methods=['GET', 'POST'])
def interactive_search(game_id):
    game = Game.query.get_or_404(game_id)
    search_term = request.form.get('search_term', game.release_name or game.official_title)
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

@main.route('/nfo/<path:filename>')
def serve_nfo_file(filename):
    nfo_storage_path = os.path.join(current_app.root_path, 'nfo_storage')
    return send_from_directory(nfo_storage_path, filename)

