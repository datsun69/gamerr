# /gamearr/run.py

from app import create_app
from app.config import Config  # <-- THE FIX: Import from the 'app' package

app = create_app(Config)

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)