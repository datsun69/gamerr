# /gamearr/app/util.py

from datetime import datetime

def timestamp_to_date_filter(s):
    """A custom filter for Jinja2 to use in HTML templates."""
    if s is None: 
        return "N/A"
    try:
        # The timestamp can come as a string or int, so we convert it
        # Handle potential float values from some APIs
        return datetime.fromtimestamp(int(float(s))).strftime('%Y-%m-%d')
    except (ValueError, TypeError, OSError):
        # OSError can happen for out-of-range timestamps
        return "Invalid Date"