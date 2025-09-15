/* This statement will delete the old tables every time you initialize the DB */
DROP TABLE IF EXISTS games;
DROP TABLE IF EXISTS settings;

/* Create the main 'games' table with all the columns we need for future features */
CREATE TABLE games (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    
    /* IGDB Metadata (The source of truth for what a game IS) */
    igdb_id INTEGER UNIQUE NOT NULL,
    official_title TEXT NOT NULL,
    release_date TEXT,
    cover_url TEXT,
    
    /* Monitoring & Release Info (What we DISCOVER about the game) */
    status TEXT NOT NULL,
    release_name TEXT,
    release_group TEXT,
    nfo_path TEXT,
    nfo_img_path TEXT,
    
    /* Download & Filesystem Info (The state of the game on our system) */
    torrent_hash TEXT,
    local_path TEXT
);

/* Create the 'settings' table to hold all application configuration */
CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE search_tasks (
    id TEXT PRIMARY KEY,        -- A unique ID we generate for the task
    search_term TEXT NOT NULL,
    status TEXT NOT NULL,       -- PENDING, COMPLETE, FAILED
    results TEXT,               -- The JSON results from IGDB will be stored here as text
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

/* Insert all the default settings keys so they exist on first run */
/* This makes the settings page easier to manage */
INSERT INTO settings (key, value) VALUES
    /* Jackett Settings */
    ('jackett_url', ''),
    ('jackett_api_key', ''),
    ('jackett_indexers', 'all'),

    /* qBittorrent Settings */
    ('qbittorrent_host', 'localhost'),
    ('qbittorrent_port', '8080'),
    ('qbittorrent_user', ''),
    ('qbittorrent_pass', ''),
    ('qbittorrent_category', 'gamearr'),

    /* IGDB/Twitch API Settings */
    ('twitch_client_id', ''),
    ('twitch_client_secret', ''),

    /* NEW: Reddit API Settings */
    ('reddit_client_id', ''),
    ('reddit_client_secret', ''),
    ('reddit_username', ''),
    ('reddit_password', '');