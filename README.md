# Gamerr

> **Warning: Alpha Software**
> This application is in the early stages of development. It is functional but should be considered alpha software. Expect bugs, breaking changes, and a rapidly evolving feature set.

(Will add screenshot)

## Core Concept

Gamerr is a Python-based `*arr`-style application for managing and automating the acquisition of PC games. The application allows a user to monitor for game releases from various sources. Once a release is found, it integrates with download clients to handle the download and subsequent post-processing.

The project is built with Flask and SQLite on the backend and uses a clean, functional web interface.

## Key Features

*   **IGDB Integration:** Add new games by searching the IGDB database for official metadata, cover art, and release dates.
*   **Multi-Tiered Release Finding:** Automatically scans for new releases using a sophisticated strategy:
    *   **Tier 1:** Checks predb.net for scene releases.
    *   **Tier 2:** Checks Reddit and public RSS feeds for new P2P releases.
    *   **Tier 3:** Performs a deep search of Reddit's history for older releases.
*   **Download Client Integration:** Sends found releases to qBittorrent via its Web API and monitors download progress.
*   **Library Management:** Includes features to import existing game libraries and discover new games from curated lists.
*   **Modern UI:** A clean, responsive interface with a card-based layout for easy browsing.

## Getting Started

You can run Gamerr either locally for development or as a Docker container for production.

### Running Locally (for Development)

This method is ideal for contributing to the code or running on a local Windows/macOS/Linux machine.

1.  **Clone the repository:**
    ```bash
    git clone https://github.com/datsun69/gamerr.git
    cd gamerr
    ```

2.  **Create and activate a virtual environment:**
    ```bash
    # For Windows
    python -m venv venv
    venv\Scripts\activate

    # For macOS/Linux
    python3 -m venv venv
    source venv/bin/activate
    ```

3.  **Install the required packages:**
    ```bash
    pip install -r requirements.txt
    ```

4.  **Set required environment variables:**
    Gamerr needs to know where your game library is located.
    ```bash
    # For Windows CMD (use your actual network/local path)
    set LIBRARY_PATH=\\YOUR_SERVER\path\to\games
    set DOWNLOADS_PATH=\\YOUR_SERVER\path\to\games\_downloads

    # For PowerShell
    $env:LIBRARY_PATH = '\\YOUR_SERVER\path\to\games'
    $env:DOWNLOADS_PATH = '\\YOUR_SERVER\path\to\games\_downloads'
    ```

5.  **Initialize the database:**
    The first time you run the app, you need to create the database schema.
    ```bash
    flask db init
    ```
    *Note: For the current setup, simply running the app will create the database automatically.*

6.  **Run the application:**
    ```bash
    python run.py
    ```
    The application will be available at `http://127.0.0.1:5000`.

### Running with Docker (Recommended for Production)

This is the easiest and most reliable way to run Gamerr on a server like Unraid. This example runs the Gamerr container by itself. For a full stack with qBittorrent, please see the `docker-compose.yml` file in the repository.

```bash
docker run -d \
  --name=Gamerr \
  -p 5000:5000 \
  -v /path/on/your/server/appdata/Gamerr:/app/instance \
  -v /path/on/your/server/games:/games \
  -e LIBRARY_PATH=/games \
  -e DOWNLOADS_PATH=/games/_downloads \
  -e TZ="Europe/London" \
  --restart unless-stopped \
  datsun69/gamerr:latest
```

**Parameter Breakdown:**
*   `-p 5000:5000`: Maps the container's port 5000 to your host's port 5000.
*   `-v /path/on/your/server/appdata/gamearr:/app/instance`: **CRITICAL.** Maps a folder on your server to the container's `instance` folder to persist the database and configuration. **You must change the left side.**
*   `-v /path/on/your/server/games:/games`: **CRITICAL.** Maps your game library on your server to the `/games` folder inside the container. **You must change the left side.**
*   `-e LIBRARY_PATH=/games`: Tells the application to use the container path for the library.
*   `-e DOWNLOADS_PATH=/games/_downloads`: Tells the application to use the container path for the downloads folder.

### Roadmap & To-Do List

This project is actively being developed. The current roadmap includes:

-   [x] Core refactor from a single file to a structured Flask application.
-   [x] Feature: Import existing games from a folder structure.
-   [x] Feature: Discover new and popular games from within the app.
-   [ ] **Settings Page Overhaul:**
    -   [x] Add collapsible sections for better organization.
    -   [ ] Implement dynamic CRUD (Create, Read, Update, Delete) for Jackett indexers.
    -   [ ] Redact secrets (passwords, API keys) in the UI.
    -   [ ] Add robust input validation using Flask-WTF.
-   [ ] **Advanced Release Management:**
    -   [x] "Related Releases" engine to track and manage updates, patches, and DLCs.
    -   [ ] "Alternative Base Game Releases"
    -   [ ] "Upscale" feature to find better-quality releases for already-imported games.

### Limitations

*   **qBittorrent Only:** Currently, qBittorrent is the only supported download client.
*   **Jackett Only:** Currently, it only works by configuring indexers from Jackett.
*   **Release Parsing:** The release name parsing is robust but may not correctly identify every possible format.
*   **Windows Development:** Running in the Flask development server on Windows can cause some non-critical logging errors due to file locking. The Docker container on Linux does not have this issue.
