# Jellyfin Manager

A lightweight web page with one button: **Scan Media Library**. Sign in with any Jellyfin account, press the button, and it triggers a full library refresh on your Jellyfin server, shows live scan progress, and then enforces a 1-hour cooldown so the server doesn't get hammered. Every press is recorded to an on-disk history you can review from the page.

## Features

- One-click full library scan (calls Jellyfin's `/Library/Refresh`)
- Live scan progress, polled from Jellyfin's scheduled tasks
- 1-hour cooldown after each scan, shared across all clients and **persisted to disk** (restarting the container no longer resets it)
- **Scan history** — every button press is logged with its outcome (`started`, `cooldown`, `error`), timestamp, the Jellyfin user who pressed it, client IP and User-Agent
- **Jellyfin account login** — sign in with your own Jellyfin username and password, no shared app password to configure
- Rate-limited login attempts (lockout after repeated failures)
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
     - SECRET_KEY=some_long_random_string
     - DATA_DIR=/data
   ```
   > Get your API key from Jellyfin: **Dashboard → Advanced → API Keys → + New Key**

   There is no app password to pick — you sign in with a Jellyfin account (see
   [Login](#login)). `JELLYFIN_URL` is used both to verify logins and to
   trigger scans, so make sure it's reachable from inside the container.

   Set `SECRET_KEY` to a long random value, otherwise it is regenerated on every
   restart and everyone gets logged out. Generate one with:
   ```bash
   python3 -c "import secrets; print(secrets.token_hex(32))"
   ```

3. **Check the volume (required — see [Data & persistence](#data--persistence))**

   `docker-compose.yml` ships with the bind mount already in place:
   ```yaml
   volumes:
     - ./data:/data
   ```
   Don't remove it. Without it, your scan history and cooldown are wiped every time you update the image.

4. **Pull the image**
   ```bash
   docker pull ghcr.io/atvriders/jellyfin-manager:latest
   ```

5. **Start the app**
   ```bash
   docker compose up -d
   ```

6. **Open in browser and sign in with your Jellyfin account**
   ```
   http://localhost:5455
   ```

## Login

You sign in with **any Jellyfin account** — the same username and password you
use for Jellyfin itself. There is no separate app password and nothing extra to
create or configure.

How it works:

- Your credentials are verified against `JELLYFIN_URL` using Jellyfin's own
  login API (`/Users/AuthenticateByName`) and are **never stored** by this app.
  The temporary Jellyfin session created by the check is revoked immediately.
- The app keeps only your Jellyfin **username** — in its login session and in
  the scan history, so the dive log shows who pressed the button.
- Any valid Jellyfin user can sign in; there is no admin requirement.

**Lockout:** 3 failed login attempts within a 5-minute window lock the login
page for 1 hour. Only genuine credential rejections count — if the Jellyfin
server is unreachable, the attempt is not counted and you get a distinct
"can't reach the server" message instead.

> Note: repeated wrong passwords are real failed logins against your Jellyfin
> server, so they can **also** trip Jellyfin's own brute-force protection and
> temporarily lock that Jellyfin account.

## Data & persistence

The app writes its scan history to `$DATA_DIR/scan_history.json`, and the shared
1-hour cooldown is **derived from that file** (from the timestamp of the last
`started` entry).

The bind mount in `docker-compose.yml` is therefore **required**:

```yaml
volumes:
  - ./data:/data
environment:
  - DATA_DIR=/data
```

Anything written inside the container that isn't on a volume lives in the
container's writable layer, which Docker **destroys** when the container is
recreated — which happens on every `docker compose pull` / `docker compose up -d`
after a new image is published. Without the bind mount you would silently lose
your entire scan history, and the cooldown would reset, on every single update.

The app still starts without a volume (it falls back to `/data` inside the
container) — the history is simply ephemeral, which is almost certainly not what
you want.

Retention: the most recent **500** entries are kept; older ones are trimmed
automatically.

### Privacy

`data/scan_history.json` records the **Jellyfin username**, **client IP
address** and **User-Agent** of every button press. Passwords are never
written anywhere — only the username of whoever was signed in.

`data/` is in `.gitignore` — keep it that way. This is a public repo, and
committing that file would publish the usernames and IP addresses of everyone
who uses your instance.

### Behind a reverse proxy

If you run this behind nginx, Traefik, Caddy, a Cloudflare Tunnel, etc., the
request appears to come from the proxy, so **every history entry will show the
proxy's IP** instead of the real client's.

To fix that, set:

```yaml
environment:
  - TRUST_PROXY=1
```

This makes the app read the client IP from the `X-Forwarded-For` header.

> Only enable `TRUST_PROXY=1` if the app really is behind a proxy that sets
> `X-Forwarded-For`. If the app is exposed directly, a client can forge that
> header and spoof the IP recorded in your history.

## Configuration

| Variable | Default | Description |
|---|---|---|
| `JELLYFIN_URL` | — | Full URL to your Jellyfin server (include port if needed). Used both to verify logins and to trigger scans |
| `JELLYFIN_API_KEY` | — | API key generated from the Jellyfin dashboard (used for scan triggering) |
| `SECRET_KEY` | random per restart | Flask session signing key. Set it, or logins won't survive a restart |
| `DATA_DIR` | `/data` | Directory the scan history is written to |
| `TRUST_PROXY` | unset | Set to `1` to read the client IP from `X-Forwarded-For` (see above) |

## Development

Tests run against a plain Python 3 install with `flask`, `requests` and `pytest`:

```bash
python3 -m pytest app/tests -q
```

CI runs this same suite on every push, **before** the Docker image is built and
pushed, so a failing test never reaches ghcr.io.

## Stack

- **Backend:** Python 3.12, Flask
- **Frontend:** Vanilla JS (no build step)
- **Container:** Docker / Docker Compose, image published to GitHub Container Registry
