# Jellyfin Manager

A lightweight web page with one button: **Scan Media Library**. Sign in with any Jellyfin account, press the button, and it triggers a full library refresh on your Jellyfin server, shows live scan progress, and then enforces a 1-hour cooldown so the server doesn't get hammered. Every press is recorded to an on-disk history you can review from the page.

## Features

- One-click full library scan (calls Jellyfin's `/Library/Refresh`). A press while a scan is **already running** is refused instead of restarting that scan from 0%
- Live scan progress, read from Jellyfin's "Scan Media Library" scheduled task
- 1-hour cooldown after each scan, shared across all clients and **persisted to disk** (restarting the container no longer resets it)
- **Scan history** — every button press is logged with its outcome (`started`, `cooldown`, `busy`, `error`), timestamp, the Jellyfin user who pressed it, client IP and User-Agent. Repeated rejected presses within a minute are folded into one row
- **Jellyfin account login** — sign in with your own Jellyfin username and password, no shared app password to configure
- Server-side login throttling, plus a guard that never lets this page be the thing that trips Jellyfin's own account lockout (see [Login](#login))
- **Downloads panel (optional)** — shows what qBittorrent is downloading on one or more boxes, and which finished downloads aren't in Jellyfin yet, with a hint that a scan will add them
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
     - JELLYFIN_URL=http://192.0.2.10:8096
     - JELLYFIN_API_KEY=your_api_key_here
     - SECRET_KEY=change_this_to_a_random_string
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
   The placeholder shipped in `docker-compose.yml`, or any value shorter than 16
   characters, is **ignored** (it is public, so anyone could forge a login with
   it): the app logs an error and uses a random key instead, which means logins
   don't survive a restart until you set a real one.

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
- Any Jellyfin user with a password can sign in; there is no admin requirement.
  Accounts without a password can't sign in here: an empty password is never
  passed to Jellyfin, which would accept it from anyone who knows the username.

**Throttling:** failed sign-ins are counted **on the server**, not in the
browser, so clearing cookies doesn't reset them:

- 3 failures from one client IP within 5 minutes lock that IP out for 1 hour.
- 6 failures for one username within 15 minutes (from any IPs) pause sign-ins
  for that username for 15 minutes.
- A wrong password, a username that doesn't exist, and an account this page
  turns away (disabled, LAN-only from outside the LAN, or close to Jellyfin's
  lockout, below) all get the same "Wrong username or password"
  answer after about the same time, and each uses up an attempt. That way the
  page doesn't reveal which usernames exist; the log records the real reason.
- If the Jellyfin server can't be reached, you get a distinct message and no
  attempt is used up. If Jellyfin probably got the password but never answered
  (a timeout), you get the same message, and the sign-in counts against the
  username but not your IP.

Behind a proxy or tunnel, set `TRUST_PROXY` to the number of proxies in front of the app, usually `1` (see
[Behind a reverse proxy](#behind-a-reverse-proxy)). Otherwise every visitor
appears to come from the proxy's address and one person's typos lock everyone
out.

**Jellyfin's own lockout:** if an admin sets *Maximum number of failed login
attempts* on a Jellyfin user, exceeding it **disables that Jellyfin account**
until an admin re-enables it, and Jellyfin only resets the counter on a
successful login. So that this public page can never be used to disable
someone's account, the app checks the account's counter (with the API key)
before passing a sign-in to Jellyfin. When one more failure would disable the
account, the app doesn't pass the sign-in on, and the person sees the usual
wrong-password answer. Signing in to Jellyfin directly once resets the counter.
An account whose limit is 1 can't sign in here at all, since its first failure
would disable it; an admin has to raise the limit. A sign-in that Jellyfin
never answered is treated as a failure it may still count, for 2 minutes, so a
slow server can't be used to slip past this check.

Users whose Jellyfin policy doesn't allow remote access can only sign in from a
private (LAN) address, the same as in Jellyfin itself.

Signing out (the **Sign out** button on the page) ends the session right away.
Otherwise a session ends after 7 days without a visit. It has no fixed maximum
age: a visit more than 10 minutes after the last check renews it, so a session
that keeps being used stays signed in. It is dropped within 10 minutes if the
Jellyfin account is deleted or disabled, or loses remote access while the
visitor is outside the LAN.

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

Mount the **directory**, not the file: a single-file mount such as
`./scan_history.json:/data/scan_history.json` can't work, because every save
replaces the file by renaming a new one over it. At startup the app checks that
it can write to `$DATA_DIR`. If it can't (read-only mount, wrong owner, full
disk, a single-file mount, or a directory where the file should be) it logs one
ERROR naming the path, the uid it runs as and the reason, and the page shows
"History isn't being saved — check the /data volume" until a press is saved
again.

The app still starts without a volume (it falls back to `/data` inside the
container) — the history is simply ephemeral, which is almost certainly not what
you want.

Retention: the most recent **500** entries are kept; older ones are trimmed
automatically.

The running app also remembers the last scan time in memory, so a damaged or
deleted history file can't hand out a free scan. To reset the cooldown on
purpose, delete the file **and** restart the container. A damaged history file
is moved aside as `scan_history.json.corrupt-<time>-<id>` (the newest 5 copies
are kept) and a warning is logged.

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

This makes the app read the client IP from the `X-Forwarded-For` header. The
number is how many proxies sit in a row in front of the app: `1` for a single
nginx/Traefik/Caddy or a Cloudflare Tunnel, `2` when one forwards to another
(e.g. Cloudflare in front of Traefik). Each proxy appends the address it saw on
the right, so the app uses the n-th address from the right; anything further
left was written by the client and is not trusted. If the header holds fewer
addresses than that, the app uses the rightmost one, which the nearest proxy
wrote. The login throttle and the remote-access check use the same address.
Anything other than a whole number (e.g. `true`) is ignored with a warning in
the log; `0` or unset means no proxy.

> Only set `TRUST_PROXY` (and never higher than the real number of proxies,
> which would let a client pick its own address) if the app really is behind a
> proxy that sets
> `X-Forwarded-For`. Anything that can reach the app's port **directly** can
> forge that header and spoof the recorded IP, so with a proxy on the same host
> publish the port on `127.0.0.1` only (`"127.0.0.1:5455:5000"`), or drop the
> `ports:` mapping entirely when the proxy or tunnel container shares the app's
> Docker network.

If every visit is over HTTPS, also set `SECURE_COOKIES=1` so the session cookie
is never sent over plain HTTP. Leave it unset if you also use the plain-HTTP
LAN port, or you won't be able to sign in there.

## Downloads panel (qBittorrent)

Set `QBITTORRENT_INSTANCES` to show an **Incoming** panel between the scan
console and the dive log:

```yaml
    environment:
      - QBITTORRENT_INSTANCES=Box 1=http://192.0.2.20:8080, Box 2=http://192.0.2.21:8080
```

- Comma-separated `label=url` pairs, one per qBittorrent WebUI. If a WebUI needs
  a login, put it in the URL: `http://user:pass@192.0.2.20:8080`.
  Percent-encode the user name and password: at least `%`→`%25`, `,`→`%2C`,
  `#`→`%23`, `/`→`%2F`, `?`→`%3F`, `@`→`%40` and `:`→`%3A`.
  Unencoded, `#`, `/` or `?` end the address early and `,` splits the entry,
  so the box just shows as unreachable. To encode one:
  `python3 -c "import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=''))" 'your password'`.
  Without a login in the URL, the app relies on qBittorrent's *Bypass
  authentication for clients in whitelisted IP subnets*.
- **Downloading** lists every unfinished torrent with progress, speed and ETA.
  A torrent whose remaining amount went negative (the out-of-disk-space
  self-lock) is shown as **stuck — recheck in qBittorrent**.
- **Ready to add** lists torrents that finished *after the last completed
  library scan started* and that Jellyfin doesn't already have (matched by the
  torrent's folder or file name against recently added items). This assumes
  qBittorrent saves straight into the folders Jellyfin watches. If the last scan
  failed or was cancelled, anything finished in the last 48 hours counts.
  When something is waiting, the scan console says so under the button.
- It is **read-only**: it never pauses, deletes or changes a torrent. Torrent
  names are visible to everyone who can sign in; hashes, paths, trackers and the
  qBittorrent addresses never leave the server.
- If a box or Jellyfin can't be reached, the panel says so. It never shows
  "nothing downloading" when it simply couldn't look: with one box down, it
  says "Nothing downloading on Box 1." for the boxes that did answer.

## Configuration

| Variable | Default | Description |
|---|---|---|
| `JELLYFIN_URL` | — | Full URL to your Jellyfin server (include port if needed). Used both to verify logins and to trigger scans |
| `JELLYFIN_API_KEY` | — | API key generated from the Jellyfin dashboard (used for scan triggering) |
| `SECRET_KEY` | random per restart | Flask session signing key. Set it (16+ characters, not the placeholder), or logins won't survive a restart |
| `DATA_DIR` | `/data` | Directory the scan history is written to |
| `TRUST_PROXY` | unset | Number of proxies in front of the app (`1` for one proxy or tunnel); the client IP is then read from `X-Forwarded-For` (see above) |
| `SECURE_COOKIES` | unset | Set to `1` to mark the session cookie HTTPS-only (only if every visit is over HTTPS) |
| `QBITTORRENT_INSTANCES` | unset | Optional. `label=url` pairs for the [Downloads panel](#downloads-panel-qbittorrent). Unset hides the panel |

## Development

Tests run against a plain Python 3.10+ install with the packages in
`app/requirements.txt` plus `pytest`:

```bash
python3 -m pytest app/tests -q
```

The browser tests in `app/tests/test_frontend_e2e.py` and
`app/tests/test_incoming_e2e.py` also need `pip install playwright` and a
Chrome/Chromium (set `CHROME_PATH` if it isn't `/usr/bin/google-chrome` or
`/usr/bin/chromium`); without them they are skipped. Set
`FRONTEND_SHOTS=/some/dir` / `INCOMING_SHOTS=/some/dir` to save screenshots of
each state.

CI runs the suite (and `node --check` on the static scripts and the templates'
inline scripts) on every push and pull request. The browser tests are skipped
there. Only pushes to `master`, releases and a weekly rebuild publish an
image: it is smoke-tested (`/healthz` and `/login` must answer) before the
multi-arch (`amd64` + `arm64`) push, so a broken build never reaches ghcr.io.
Every image is also tagged with its commit SHA, so you can roll back to one.

## Stack

- **Backend:** Python 3.12, Flask, served by gunicorn (one worker: the scan lock, cooldown and login throttle live in that process)
- **Frontend:** Vanilla JS (no build step)
- **Container:** Docker / Docker Compose, image published to GitHub Container Registry
