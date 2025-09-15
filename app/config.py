import os

class Config:
    """Base configuration."""
    SECRET_KEY = os.getenv('SECRET_KEY', 'your-default-secret-key-for-dev')
    
    # --- Database Configuration ---
    # This specifies the path to the database file within the instance folder.
    SQLALCHEMY_DATABASE_URI = os.getenv('DATABASE_URL', 'sqlite:///database.db')
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    
    # --- Custom App Configuration ---
    # These are the paths the app will use. They are read from environment variables,
    # with the Docker paths as defaults.
    DOWNLOADS_PATH = os.getenv('DOWNLOADS_PATH', '/games/_downloads')
    LIBRARY_PATH = os.getenv('LIBRARY_PATH', '/games')