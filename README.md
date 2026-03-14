# Jellyfin Manager

A lightweight web UI for managing your Jellyfin media server — view libraries, monitor scan tasks, and trigger library refreshes from a single dashboard.

## Features

- Live connection status to your Jellyfin server
- Browse all media libraries with type icons
- View and run/stop scheduled scan tasks
- Real-time progress bars via Server-Sent Events (SSE)
- Scan all libraries with one click
- Dark UI themed to match Jellyfin

## Requirements

- Docker & Docker Compose
- A running Jellyfin server with an API key

## Setup

1. **Clone the repo**
   ```bash
   git clone https://github.com/Atvriders/jellyfin-manager.git
   cd jellyfin-manager
   ```

2. **Configure environment**

   Edit `docker-compose.yml` and set your values:
   ```yaml
   environment:
     - JELLYFIN_URL=http://192.168.1.100:8096
     - JELLYFIN_API_KEY=your_api_key_here
   ```
   > Get your API key from Jellyfin: **Dashboard → Advanced → API Keys → + New Key**

3. **Pull the image**
   ```bash
   docker pull ghcr.io/atvriders/jellyfin-manager:latest
   ```

4. **Start the app**
   ```bash
   docker compose up -d
   ```

4. **Open in browser**
   ```
   http://localhost:5455
   ```

## Configuration

| Variable | Description |
|---|---|
| `JELLYFIN_URL` | Full URL to your Jellyfin server (include port if needed) |
| `JELLYFIN_API_KEY` | API key generated from the Jellyfin dashboard |

## Stack

- **Backend:** Python 3.12, Flask, Gunicorn
- **Frontend:** Vanilla JS, SSE for live updates
- **Container:** Docker / Docker Compose
