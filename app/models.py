from . import db  # We will create this 'db' object in __init__.py
from datetime import datetime

class Game(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    igdb_id = db.Column(db.String, unique=True, nullable=True)
    official_title = db.Column(db.String, nullable=False)
    slug = db.Column(db.String, nullable=True)
    release_date = db.Column(db.String, nullable=True)
    cover_url = db.Column(db.String, nullable=True)
    status = db.Column(db.String, default='Monitoring', nullable=False)
    release_name = db.Column(db.String, nullable=True)
    release_group = db.Column(db.String, nullable=True)
    nfo_path = db.Column(db.String, nullable=True)
    nfo_img_path = db.Column(db.String, nullable=True)
    torrent_hash = db.Column(db.String, nullable=True)
    local_path = db.Column(db.String, nullable=True)

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