FROM python:3.12-slim

# PYTHONUNBUFFERED: log lines reach `docker logs` as they happen instead of
# sitting in a stdio buffer (and vanishing with it if the container is killed).
# PYTHONDONTWRITEBYTECODE: don't litter the container's writable layer with .pyc.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Scan history lives here. docker-compose bind-mounts ./data over /data so the
# history survives image updates. Creating the directory here (and defaulting
# DATA_DIR to it) means the app still starts and works when run WITHOUT a
# volume — the history is just ephemeral in that case, not a crash.
ENV DATA_DIR=/data
RUN mkdir -p /data

COPY . .

# Deliberately runs as root (no USER). Existing installs already have a
# root-owned ./data bind mount written by earlier images; an unprivileged user
# couldn't write to it, every history write would fail with EACCES, and the app
# would carry on serving while silently dropping rows (record_press swallows
# write errors on purpose so the button keeps working).
EXPOSE 5000

# slim ships neither curl nor wget, so probe with the interpreter we already
# have. The opener raises on anything that isn't a 2xx, and an uncaught
# exception exits 1 = unhealthy. ProxyHandler({}) ignores HTTP_PROXY & co:
# Docker injects those into every container when its client config sets
# "proxies", and unless NO_PROXY happens to list 127.0.0.1 the probe would go
# to the proxy instead of this container, fail, and mark a healthy app unhealthy.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request as u; u.build_opener(u.ProxyHandler({})).open('http://127.0.0.1:5000/healthz', timeout=4)"]

# gunicorn, not `python app.py`: as PID 1 the Flask dev server ignores SIGTERM,
# so every `docker stop` / `compose pull` sat out the full 10 s grace period and
# then got SIGKILLed, in-flight requests and all. gunicorn stops accepting on
# SIGTERM and gives in-flight requests up to --graceful-timeout to finish.
#
# -w MUST stay 1. The scan lock, the history lock, the cooldown fallbacks and
# the login limiter all live in this one process's memory; a second worker would
# have its own copies, so two presses could both start a scan and the fail-closed
# cooldown / lockout would only hold within whichever worker served the request.
# Concurrency comes from --threads instead (all threads share that state).
#
# --threads 64, not the usual handful, because a thread can be tied up for a
# long time doing nothing: a sign-in waits up to LOGIN_QUEUE_SECONDS (30 s)
# behind the others while Jellyfin is slow, and although gunicorn waits for a
# request's first bytes without using a thread, once they arrive a thread
# blocks until the rest of the headers do, with no timeout at all. With 8
# threads, 8 such requests froze every page AND this HEALTHCHECK (the old dev
# server, one thread per connection, never did). Threads are only started when
# needed and spend their time waiting on sockets, so a high cap costs nothing
# while idle. It raises the bar rather than removing it, which is one more
# reason the port should only be reachable by the LAN or your proxy.
#
# --keep-alive stays at gunicorn's default (2 s). The price: a stop right after
# a response went out (say a page just polled) waits out the whole
# --graceful-timeout, because gunicorn 26 doesn't expire that idle connection
# during shutdown. docker-compose.yml allows 15 s for that, and since the
# connection is idle, a plain `docker stop` SIGKILLing at 10 s loses nothing.
# --keep-alive 0 would make every stop instant but is worse:
# every response then ends with gunicorn waiting up to 2 s, on its single
# event-loop thread, for the client to hang up, so a few clients slow to close
# stall every request, the HEALTHCHECK included.
CMD ["gunicorn", "-w", "1", "--threads", "64", "-b", "0.0.0.0:5000", "--access-logfile", "-", "--graceful-timeout", "10", "app:app"]
