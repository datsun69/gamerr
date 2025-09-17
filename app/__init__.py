# /gamearr/app/__init__.py

import os
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from apscheduler.schedulers.background import BackgroundScheduler
from .config import Config

import logging
import sys
from concurrent_log_handler import ConcurrentRotatingFileHandler

# Create extension instances without an app
db = SQLAlchemy()
scheduler = BackgroundScheduler(daemon=True)

def create_app(config_class=Config):
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(config_class)
    
    app.logger.setLevel(logging.INFO)

    if not app.debug:
        log_dir = os.path.join(app.instance_path, 'logs')
        os.makedirs(log_dir, exist_ok=True)
        
        # --- FIX: Use the concurrent-safe handler ---
        # This will safely handle log rotations from multiple threads.
        file_handler = ConcurrentRotatingFileHandler(
            os.path.join(log_dir, 'gamearr.log'), 
            maxBytes=10240, 
            backupCount=10
        )
        
        file_handler.setFormatter(logging.Formatter(
            '%(asctime)s %(levelname)s: %(message)s [in %(pathname)s:%(lineno)d]'))
        
        app.logger.addHandler(file_handler)

    app.logger.info('Gamearr startup')

    try:
        os.makedirs(app.instance_path)
    except OSError:
        pass

    db.init_app(app)
    
    from . import routes
    app.register_blueprint(routes.main)
    
    from .util import timestamp_to_date_filter
    app.jinja_env.filters['timestamp_to_date'] = timestamp_to_date_filter

    with app.app_context():
        from . import models
        db.create_all()

        from . import jobs
        jobs.register_cli_commands(app)
        if not app.debug or os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
            if not scheduler.running:
                app.logger.info("Starting scheduler...")
                scheduler.add_job(func=jobs.check_for_releases, trigger="interval", minutes=30, id="release_check_job", replace_existing=True, args=[app])
                scheduler.add_job(func=jobs.update_download_statuses, trigger="interval", minutes=1, id="download_update_job", replace_existing=True, args=[app])
                scheduler.add_job(func=jobs.process_search_tasks, trigger="interval", seconds=5, id="search_task_job", replace_existing=True, args=[app])
                
                scheduler.add_job(func=jobs.process_release_check_queue, trigger="interval", seconds=5, id="release_queue_job", replace_existing=True, args=[app], max_instances=3)
                
                scheduler.add_job(func=jobs.process_completed_downloads, trigger="interval", minutes=5, id="post_process_job", replace_existing=True, args=[app])
                scheduler.add_job(func=jobs.refresh_discover_cache, trigger="interval", hours=24, id="discover_refresh_job", replace_existing=True, args=[app])
                scheduler.add_job(func=jobs.process_content_scan_queue, trigger="interval", seconds=15, id="content_queue_job", replace_existing=True, args=[app])
                scheduler.add_job(func=jobs.scan_all_library_games, trigger="interval", hours=12, id="additional_content_job", replace_existing=True, args=[app])
                scheduler.start()

    return app