from flask import (
    Blueprint, render_template, request, redirect, url_for, 
    send_from_directory, current_app, jsonify, flash
)
import uuid
import json
import os
from datetime import timedelta
import time
import re

from . import db
from .models import Game, Setting, SearchTask, DiscoverCache, Indexer, Profile
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

def _parse_indexer_id_from_url(url):
    """
    Parses 'http://host/api/v2.0/indexers/THE_ID/results/torznab/'
    and returns 'THE_ID'.
    """
    if not url:
        return None
    match = re.search(r'/indexers/([^/]+)/results/torznab', url)
    return match.group(1) if match else None

@main.route('/add/results/<task_id>')
def show_search_results(task_id):
    # --- THIS IS THE FIX ---

    # 1. Query the database to get the Profile objects
    profiles_query = Profile.query.order_by(Profile.is_default.desc(), Profile.name).all()
    
    # 2. Convert the list of objects into a list of dictionaries
    profiles_data = [
        {
            'id': profile.id, 
            'name': profile.name, 
            'is_default': profile.is_default
        } 
        for profile in profiles_query
    ]
    
    # 3. Pass the JSON-friendly list of dictionaries to the template
    return render_template('add_game_results.html', task_id=task_id, profiles=profiles_data)

@main.route('/add/results/data/<task_id>')
def get_search_results_data(task_id):
    task = SearchTask.query.get(task_id)
    if task:
        return jsonify({'status': task.status, 'results': json.loads(task.results or '[]')})
    return jsonify({'status': 'NOT_FOUND', 'results': []}), 404

@main.route('/add/confirm', methods=['POST'])
def add_game_confirm():
    igdb_id = request.form.get('igdb_id')

    profile_id = request.form.get('profile_id')

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
    
    # --- MODIFIED: Assign the profile selected by the user ---
    # The old logic to find the default profile is no longer needed here.
    if profile_id:
        new_game.profile_id = profile_id
    
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

@main.route('/game/scan/<int:game_id>', methods=['POST'])
def manual_scan_game(game_id):
    """
    API endpoint to trigger a manual release scan for a single game.
    """
    game = Game.query.get_or_404(game_id)
    if not game:
        return jsonify({'error': 'Game not found'}), 404
        
    # --- THE CRITICAL FIX ---
    # Set the status in the database IMMEDIATELY.
    game.status = 'Processing'
    
    # Set the flag for the background processor to pick up
    game.needs_release_check = True
    db.session.commit()
    
    flash(f"A manual release scan for '{game.official_title}' has been queued.", "success")
    return jsonify({'success': True, 'message': f"Scan queued for {game.official_title}"})

@main.route('/game/status/<int:game_id>')
def get_game_status_api(game_id):
    """
    API endpoint for polling the current status of a game.
    """
    game = Game.query.get_or_404(game_id)
    return jsonify({'status': game.status})

# Settings Routes
@main.route('/settings', methods=['GET'])
def settings():
    settings_data = {s.key: s.value for s in Setting.query.all()}
    # Redact secret fields before sending to the template
    for key in ['twitch_client_secret', 'qbittrent_pass', 'jackett_api_key']:
        if settings_data.get(key):
            settings_data[key] = '••••••••'
            
    indexers = Indexer.query.order_by(Indexer.name).all()
    
    # --- NEW: Fetch profiles to display in the UI ---
    profiles = Profile.query.order_by(Profile.name).all()
    
    return render_template('settings.html', settings=settings_data, indexers=indexers, profiles=profiles)

@main.route('/settings/save', methods=['POST'])
def save_settings():
    new_default_id = request.form.get('default_profile')
    
    if new_default_id:
        # First, unset the current default
        current_default = Profile.query.filter_by(is_default=True).first()
        if current_default:
            current_default.is_default = False
        
        # Then, set the new default
        new_default = Profile.query.get(new_default_id)
        if new_default:
            new_default.is_default = True
    auto_download_enabled = 'true' if 'auto_download_enabled' in request.form else 'false'
    setting = Setting.query.get('auto_download_enabled')
    if setting:
        setting.value = auto_download_enabled
    else:
        db.session.add(Setting(key='auto_download_enabled', value=auto_download_enabled))

    # This function now only saves non-indexer settings
    for key, value in request.form.items():
        # Skip the checkbox we just handled
        if key == 'auto_download_enabled':
            continue
        # Do not save placeholder values for secrets
        if value == '••••••••':
            continue
            
        setting = Setting.query.get(key)
        if setting:
            setting.value = value
        else:
            db.session.add(Setting(key=key, value=value))
            
    db.session.commit()
    flash("Global settings saved successfully!", "success")
    return redirect(url_for('main.settings'))

@main.route('/settings/indexer/add', methods=['POST'])
def add_indexer():
    data = request.get_json()
    name = data.get('name')
    url = data.get('url') # This will be the full Torznab URL

    if not all([name, url]):
        return jsonify({'error': 'Name and URL Path are required.'}), 400

    indexer_id = _parse_indexer_id_from_url(url)
    if not indexer_id:
        return jsonify({'error': 'Invalid Torznab URL format. Could not extract indexer ID.'}), 400

    if Indexer.query.filter_by(name=name).first():
        return jsonify({'error': f"An indexer with the name '{name}' already exists."}), 409

    new_indexer = Indexer(
        name=name,
        indexer_id=indexer_id,
        enabled=data.get('enabled', True),
        categories_override=data.get('categories'),
        extra_parameters=data.get('extra_params')
    )
    db.session.add(new_indexer)
    db.session.commit()
    flash(f"Indexer '{name}' added successfully.", "success")
    return jsonify({'success': True}), 201

@main.route('/settings/indexer/delete/<int:indexer_id>', methods=['POST'])
def delete_indexer(indexer_id):
    indexer = Indexer.query.get_or_404(indexer_id)
    db.session.delete(indexer)
    db.session.commit()
    flash(f"Indexer '{indexer.name}' deleted.", "success")
    return jsonify({'success': True})


# NEW PROFILE MANAGEMENT ROUTES
@main.route('/settings/profile/add', methods=['POST'])
def add_profile():
    data = request.get_json()
    name = data.get('name')

    if not name:
        return jsonify({'error': 'Profile Name is required.'}), 400
    if Profile.query.filter_by(name=name).first():
        return jsonify({'error': f"A profile named '{name}' already exists."}), 409

    # Helper to clean and split comma-separated strings
    def parse_groups(group_string):
        return [g.strip() for g in group_string.split(',') if g.strip()]

    is_first_profile = Profile.query.first() is None
    new_profile = Profile(
        name=name,
        release_types=json.dumps(data.get('release_types', [])),
        preferred_groups=json.dumps(parse_groups(data.get('preferred_groups', ''))),
        avoided_groups=json.dumps(parse_groups(data.get('avoided_groups', ''))),
        delay_hours=int(data.get('delay_hours', 0)),
        is_default=is_first_profile
    )
    db.session.add(new_profile)
    db.session.commit()
    flash(f"Profile '{name}' added successfully.", "success")
    return jsonify({'success': True}), 201

@main.route('/settings/profile/edit/<int:profile_id>', methods=['POST'])
def edit_profile(profile_id):
    profile = Profile.query.get_or_404(profile_id)
    data = request.get_json()
    new_name = data.get('name')

    if not new_name:
        return jsonify({'error': 'Profile Name is required.'}), 400
    
    # Check if another profile already has the new name
    existing = Profile.query.filter(Profile.name == new_name, Profile.id != profile_id).first()
    if existing:
        return jsonify({'error': f"A profile named '{new_name}' already exists."}), 409

    def parse_groups(group_string):
        return [g.strip() for g in group_string.split(',') if g.strip()]

    profile.name = new_name
    profile.release_types = json.dumps(data.get('release_types', []))
    profile.preferred_groups = json.dumps(parse_groups(data.get('preferred_groups', '')))
    profile.avoided_groups = json.dumps(parse_groups(data.get('avoided_groups', '')))
    profile.delay_hours = int(data.get('delay_hours', 0))
    
    db.session.commit()
    flash(f"Profile '{profile.name}' updated successfully.", "success")
    return jsonify({'success': True})

@main.route('/settings/profile/delete/<int:profile_id>', methods=['POST'])
def delete_profile(profile_id):
    profile = Profile.query.get_or_404(profile_id)
    profile_name = profile.name

    # --- Important: Unassign this profile from any games using it ---
    games_to_update = Game.query.filter_by(profile_id=profile_id).all()
    for game in games_to_update:
        game.profile_id = None
        
    db.session.delete(profile)
    db.session.commit()
    flash(f"Profile '{profile_name}' deleted.", "success")
    return jsonify({'success': True})

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
    
    # --- NEW: Fetch all profiles to populate the dropdown ---
    profiles = Profile.query.order_by(Profile.name).all()
    
    return render_template('game_detail.html', game=game, profiles=profiles)

@main.route('/game/update/<int:game_id>', methods=['POST'])
def update_game_settings(game_id):
    game = Game.query.get_or_404(game_id)
    
    profile_id = request.form.get('profile_id')
    
    # The value "0" or "" means "None" (unassign profile)
    if profile_id and int(profile_id) > 0:
        game.profile_id = profile_id
    else:
        game.profile_id = None
        
    db.session.commit()
    flash(f"'{game.official_title}' profile has been updated.", "success")
    return redirect(url_for('main.game_detail', game_id=game_id))

@main.route('/nfo/<path:filename>')
def serve_nfo_file(filename):
    nfo_storage_path = os.path.join(current_app.root_path, 'nfo_storage')
    return send_from_directory(nfo_storage_path, filename)

