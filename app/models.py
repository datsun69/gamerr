# /gamearr/app/models.py

from . import db  # We will create this 'db' object in __init__.py
from datetime import datetime

class Game(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    igdb_id = db.Column(db.String, unique=True, nullable=True)
    
    # --- Core Info (Already Have) ---
    official_title = db.Column(db.String, nullable=False)
    slug = db.Column(db.String, nullable=True)
    cover_url = db.Column(db.String, nullable=True)
    release_date = db.Column(db.String, nullable=True)
    
    # --- NEW: Rich Metadata Fields ---
    summary = db.Column(db.Text, nullable=True) # For the game description
    genres = db.Column(db.String, nullable=True) # Store as a comma-separated string
    critic_score = db.Column(db.Integer, nullable=True)
    user_score = db.Column(db.Integer, nullable=True)
    
    # Store media URLs as comma-separated strings
    screenshots_urls = db.Column(db.Text, nullable=True)
    videos_urls = db.Column(db.Text, nullable=True) # For trailers, etc.

    # --- Status and Release Info (Already Have) ---
    status = db.Column(db.String, default='Monitoring', nullable=False)
    release_name = db.Column(db.String, nullable=True)
    release_group = db.Column(db.String, nullable=True)
    nfo_path = db.Column(db.String, nullable=True)
    nfo_img_path = db.Column(db.String, nullable=True)
    torrent_hash = db.Column(db.String, nullable=True)
    local_path = db.Column(db.String, nullable=True)

    # --- NEW: Relationship to Additional Releases ---
    # This links a Game to all of its DLCs, Updates, etc.
    needs_content_scan = db.Column(db.Boolean, default=False, nullable=False)
    needs_release_check = db.Column(db.Boolean, default=False, nullable=False)
    additional_releases = db.relationship('AdditionalRelease', backref='game', lazy=True, cascade="all, delete-orphan")
    alternative_releases = db.relationship('AlternativeRelease', backref='game', lazy=True, cascade="all, delete-orphan")
    
class AlternativeRelease(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    release_name = db.Column(db.String(300), nullable=False)
    source = db.Column(db.String(50), nullable=False)
    game_id = db.Column(db.Integer, db.ForeignKey('game.id'), nullable=False)
    
    # --- NEW NFO Fields ---
    nfo_path = db.Column(db.String, nullable=True)
    nfo_img_path = db.Column(db.String, nullable=True)

    def __repr__(self):
        return f'<AlternativeRelease {self.release_name}>'

class AdditionalRelease(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    release_name = db.Column(db.String, nullable=False, unique=True)
    release_type = db.Column(db.String, nullable=False) # 'Update', 'DLC', 'Fix', 'Trainer'
    status = db.Column(db.String, default='Not Snatched', nullable=False)
    source = db.Column(db.String(50), nullable=True)
    
    # This is the foreign key that links it back to the base game
    game_id = db.Column(db.Integer, db.ForeignKey('game.id'), nullable=False)

class Setting(db.Model):
    key = db.Column(db.String, primary_key=True)
    value = db.Column(db.String, nullable=True)

class SearchTask(db.Model):
    id = db.Column(db.String, primary_key=True)
    search_term = db.Column(db.String, nullable=False)
    status = db.Column(db.String, default='PENDING', nullable=False)
    results = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class DiscoverCache(db.Model):
    # This table will store the raw JSON content of a discover list
    list_name = db.Column(db.String, primary_key=True) # e.g., 'anticipated', 'coming_soon'
    content = db.Column(db.Text, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)