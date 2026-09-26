import ipaddress
import math
import os
import re
import secrets
import threading
import time
from datetime import timedelta

import requests
from flask import Flask, flash, get_flashed_messages, jsonify, redirect, render_template, request, session, url_for

import downloads
import jellyfin
from history import (
    FUTURE_SKEW_SECONDS,
    MAX_ENTRIES,
    OUTCOME_BUSY,
    OUTCOME_COOLDOWN,
    OUTCOME_ERROR,
    OUTCOME_STARTED,
    ScanHistory,
)
from jellyfin import JfError
from limiter import LoginLimiter, PasswordCheckTimer, PendingSignIns

app = Flask(__name__)

# SECRET_KEY signs the session cookie, and the cookie is the whole login: a key
# anyone knows lets anyone mint {"auth": true}. The shipped docker-compose.yml
# and .env.example both carry a placeholder, and the README's setup example
# ("some_long_random_string") is long enough to pass the length check, so a
# deployment that copied either was wide open. Such a key is refused — but the
# app keeps running on a random key instead of crashing, because existing
# deployments auto-pull the image and a hard failure would just take them down.
# "random_string" catches both published examples; a key that really is random
# (hex, base64, token_urlsafe) never spells it out.
SECRET_KEY_PLACEHOLDERS = ("change_this", "changeme", "your_secret", "random_string")
MIN_SECRET_KEY_LEN = 16


def resolve_secret_key(raw, log):
    """The key to sign sessions with. Never logs the configured value."""
    if not raw or not raw.strip():
        log.warning(
            "SECRET_KEY is not set: using a random per-process key, so sessions will "
            "not survive a restart. Set SECRET_KEY to a long random string."
        )
        return os.urandom(32)
    lowered = raw.lower()
    if len(raw) < MIN_SECRET_KEY_LEN or any(marker in lowered for marker in SECRET_KEY_PLACEHOLDERS):
        log.error(
            "SECRET_KEY is a placeholder or shorter than %d characters, so anyone could "
            "forge a login with it. IGNORING it and using a random per-process key "
            "instead (sessions will not survive a restart). Set SECRET_KEY to a long "
            "random string, e.g. the output of: "
            "python3 -c 'import secrets; print(secrets.token_hex(32))'",
            MIN_SECRET_KEY_LEN,
        )
        return os.urandom(32)
    return raw


app.secret_key = resolve_secret_key(os.environ.get("SECRET_KEY", ""), app.logger)

# The session cookie. Lax keeps it off cross-site POSTs (so another site can't
# press the scan button or sign you out); HttpOnly keeps it away from page
# scripts. Secure is opt-in because the LAN port is plain HTTP and a Secure
# cookie would never come back over it — set SECURE_COOKIES=1 when the app is
# only reached over HTTPS. Flask enforces the lifetime as the maximum age of
# the cookie's signature on EVERY session (permanent or not), which also bounds
# how long a logged-out session id must stay on the revoked list.
app.config.update(
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=os.environ.get("SECURE_COOKIES") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(days=7),
)

JELLYFIN_URL = os.environ.get("JELLYFIN_URL", "").rstrip("/")
JELLYFIN_API_KEY = os.environ.get("JELLYFIN_API_KEY", "")

COOLDOWN_SECONDS = 60 * 60  # 1 hour

# A signed-in session is re-checked against Jellyfin this often, so deleting or
# disabling the Jellyfin account ends access here too (not just on Jellyfin).
REVALIDATE_SECONDS = 10 * 60
# ...and when Jellyfin can't answer, the next try waits this long instead of
# making every request wait on a dead server.
REVALIDATE_RETRY_SECONDS = 60

# Typed usernames are kept (pre-fill, logs, throttle keys) only this long.
MAX_USERNAME_LEN = 128

MSG_UNREACHABLE = "Can't reach the Jellyfin server. Try again in a moment."
MSG_BUSY = "The server is busy with other sign-ins. Try again in a moment."
MSG_NO_PASSWORD = "Enter your password. Jellyfin accounts without a password can't sign in here."
# There is deliberately no "disabled", "LAN only" or "about to lock" message:
# those told anyone which accounts exist (for free, before any password was
# checked). Such refusals read like a wrong password; the log says why.

# ---------------------------------------------------------------------------
# PROCESS-LOCAL STATE. Every lock below, the cooldown fallbacks, the login
# limiter and the revoked-session list live in this process's memory. Two
# gunicorn workers would each have their own copies: two presses could both
# pass "the" scan lock, and a guesser would get a fresh set of attempts per
# worker. Run exactly ONE worker (gunicorn -w 1; threads are fine).
# ---------------------------------------------------------------------------

# Scan history. Lives on a bind mount (docker-compose: ./data:/data) so it — and
# therefore the cooldown, which is derived from it — survives a container
# restart or an image pull. There is deliberately no in-memory `scan_until`:
# that global reset on every restart and let anyone scan again immediately.
DATA_DIR = os.environ.get("DATA_DIR", "/data")
HISTORY_PATH = os.path.join(DATA_DIR, "scan_history.json")
# warn: the clock-jumped-back notice and quarantined-file notices land in the
# app's log, next to everything else an operator reads.
history = ScanHistory(HISTORY_PATH, warn=app.logger.warning)


def check_history_storage():
    """Probe DATA_DIR once, at startup. True if the history can be saved.

    An unwritable /data used to be silent: presses "worked", nothing was kept,
    and the cooldown (derived from the file) was gone after the next restart.
    One ERROR here says where, as whom, and why — ownership of a bind mount
    is the usual culprit, and the uid is what to chown it to.
    """
    problem = history.storage_problem()
    if problem is None:
        return True
    app.logger.error(
        "scan history can't be saved to %r (the app runs as uid %d, gid %d): %s. "
        "Presses won't be recorded and the cooldown won't survive a restart; check the /data volume.",
        HISTORY_PATH, os.getuid(), os.getgid(), problem,
    )
    return False


# Whether presses are being saved, for the page ("writable" in /api/history).
# The startup probe sets it; after that the most recent save attempt decides,
# so a volume fixed while the app runs clears the warning at the next press,
# and a volume that breaks later raises it. (Per process — see above.)
_history_writable = check_history_storage()

# Serialises the cooldown check -> Jellyfin call -> "started" record sequence.
# The dev server is threaded, so without this two simultaneous presses both pass
# the cooldown check and both kick off a scan. (Per process — see above.)
scan_lock = threading.Lock()

# In-process cooldown safety net. The cooldown's source of truth is the last
# OUTCOME_STARTED row on disk, but disk can fail three ways that must NOT
# silently disable rate limiting (fail-open):
#   * a STARTED record can't be persisted (read-only /data, disk full)  -> _last_started_fallback
#   * the history file can't be read at all (EMFILE/EACCES/EIO)         -> fall back to last good read
#   * the file is quarantined as corrupt and reads back empty           -> _last_started_cache
# All let the cooldown survive for the life of the process instead of vanishing.
# (Per process — see above.)
_state_lock = threading.Lock()
_last_started_fallback = 0.0   # set when a STARTED record fails to persist
_last_started_cache = 0.0      # most recent last_started_at successfully read from disk

# Failed sign-ins, counted per client IP and per username on the SERVER. The
# old count lived in the session cookie, and discarding the cookie reset it.
login_limiter = LoginLimiter()

# Sign-ins that got no answer but probably reached Jellyfin (see limiter.py):
# counted as failures toward the account-disable guard until they've surely
# landed. And how long a real password check takes, so refusals that never ask
# Jellyfin answer no faster. (Per process — see above.)
pending_sign_ins = PendingSignIns()
password_check_timer = PasswordCheckTimer()

# Sign-ins are decided ONE AT A TIME: the limiter check, the Policy read, the
# Jellyfin call and recording the verdict form one atomic step. Otherwise N
# parallel wrong-password posts all pass the checks before the first failure
# is counted — past the per-IP limit, and straight past the account-disable
# guard (Jellyfin counts all N and disables the account). Sign-ins are rare on
# a family server, so a queue costs nothing; a request that waits too long is
# turned away without consuming an attempt. (Per process — see above.)
_login_lock = threading.Lock()
LOGIN_QUEUE_SECONDS = 30

# Session ids signed out via /logout: sid -> epoch after which no copy of that
# cookie can verify anyway (see PERMANENT_SESSION_LIFETIME). A signed cookie
# can't be recalled, so a copy taken before logout would otherwise stay valid.
# In memory: a restart forgets it (the cookie's own max age still applies).
_revoked_lock = threading.Lock()
_revoked_sids = {}


# The "Incoming" panel (qBittorrent downloads + finished-but-not-in-Jellyfin).
# Off unless QBITTORRENT_INSTANCES is set. Read-only: never touches a torrent.
incoming = downloads.Incoming(
    downloads.parse_instances(os.environ.get("QBITTORRENT_INSTANCES", ""), warn=app.logger.warning),
    JELLYFIN_URL,
    JELLYFIN_API_KEY,
)

# Whether the library scan was running at the last successful progress read
# (None = not read yet). When that flips, the panel's cached payload says the
# opposite ("ready" vs "adding now…"), so it is dropped. (Per process.)
_scan_seen_lock = threading.Lock()
_scan_seen_running = None


def note_scan_running(running):
    global _scan_seen_running
    with _scan_seen_lock:
        changed = running != _scan_seen_running
        _scan_seen_running = running
    if changed:
        incoming.invalidate()   # never waits (see downloads.Incoming)


# ---------------------------------------------------------------- security headers

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    # The pages are single-file templates with inline <script>/<style>.
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src https://fonts.gstatic.com; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'none'; "
    "form-action 'self'; "
    "object-src 'none'"
)

SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",  # no clickjacking the scan button in a frame
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
}


@app.after_request
def add_security_headers(response):
    # after_request also runs for error responses (404/405/500), so every
    # response gets them.
    for name, value in SECURITY_HEADERS.items():
        response.headers[name] = value
    return response


# ---------------------------------------------------------------- Jellyfin sign-in


class JellyfinUnreachable(Exception):
    """Jellyfin could not be reached (or answered with a non-auth failure)."""


class JellyfinAmbiguous(JellyfinUnreachable):
    """The password reached Jellyfin but its answer didn't reach us (read
    timeout, connection dropped after sending, a gateway in front of Jellyfin
    timing out). Jellyfin still counts a wrong one when it finishes, so this is
    not "nothing happened" — see pending_sign_ins."""


class JellyfinRefused(Exception):
    """Jellyfin answered 403: the account exists but may not sign in (disabled,
    remote access off, parental schedule), whatever the password."""


def authenticate_jellyfin(username: str, password: str) -> "dict | None":
    """Verify a Jellyfin user's credentials against the Jellyfin server.

    Returns the response's "User" dict (e.g. {"Name": ..., "Id": ...}) on
    success, None on a genuine credential rejection (401/400), raises
    JellyfinRefused on a 403, JellyfinAmbiguous when the request probably
    arrived but the answer was lost, and JellyfinUnreachable for anything else
    that is NOT a verdict on the credentials (connection refused, 3xx, other
    5xx, unparseable body) — the caller must not count those as failed attempts.

    The password travels ONLY in the request body, and is never logged or
    stored. Jellyfin creates a session for us on success; we immediately
    best-effort revoke it (we only wanted the yes/no, never the token).
    """
    started = password_check_timer.now()
    try:
        resp = requests.post(
            f"{JELLYFIN_URL}/Users/AuthenticateByName",
            json={"Username": username, "Pw": password},
            # Identifies this app; AuthenticateByName rejects anonymous clients.
            # No Token: this call is how a client obtains one.
            headers={"Authorization": jellyfin.client_header()},
            timeout=10,
            # Following a redirect would re-send the password to wherever the
            # 3xx points. A redirect is a misconfigured JELLYFIN_URL, not a
            # verdict on the credentials: it lands in "unreachable" below.
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        if jellyfin.maybe_delivered(exc):
            raise JellyfinAmbiguous("no answer from Jellyfin, which probably got the password") from exc
        raise JellyfinUnreachable("could not reach Jellyfin") from exc

    if resp.status_code in (502, 504):
        # A proxy in front of Jellyfin gave up waiting (504) or lost the
        # upstream mid-request (502): the same doubt as our own read timeout.
        raise JellyfinAmbiguous(f"a proxy in front of Jellyfin answered HTTP {resp.status_code}")
    if resp.status_code in (200, 400, 401, 403):
        # A verdict: this is how long a password check takes (see _fail_quietly).
        password_check_timer.record(password_check_timer.now() - started)
    if resp.status_code in (400, 401):
        return None  # a real verdict: bad credentials
    if resp.status_code == 403:
        raise JellyfinRefused()
    if resp.status_code != 200:
        raise JellyfinUnreachable(f"Jellyfin returned HTTP {resp.status_code}")

    try:
        body = resp.json()
        user = body.get("User")
    except Exception as exc:
        raise JellyfinUnreachable("Jellyfin returned an unparseable response") from exc
    if not isinstance(user, dict):
        raise JellyfinUnreachable("Jellyfin response had no User object")

    # Best-effort revoke of the session Jellyfin just created — we never keep
    # (or log) that token, and a failure here must not break the login. The
    # token goes in the MediaBrowser header: newer Jellyfin ignores the legacy
    # X-Emby-Token header, which made this revoke a silent no-op.
    token = body.get("AccessToken")
    if isinstance(token, str) and token:
        try:
            requests.post(
                f"{JELLYFIN_URL}/Sessions/Logout",
                headers={"Authorization": jellyfin.client_header(token)},
                timeout=10,
                allow_redirects=False,
            )
        except Exception:
            pass

    return user


def _policy(account):
    policy = account.get("Policy") if isinstance(account, dict) else None
    return policy if isinstance(policy, dict) else {}


def _policy_int(policy, key):
    """An integer policy value, or None if it's missing or not a number."""
    value = policy.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return int(value)


# The address ranges a home LAN actually uses: RFC 1918, loopback, link-local,
# and IPv6 unique-local. Deliberately an explicit list, not ipaddress's
# is_private: that also covers Teredo (2001::/23) and 6to4 (2002::/16), which
# are real internet clients. 100.64.0.0/10 (carrier-grade NAT, Tailscale) is
# not listed either: such clients count as remote.
LAN_NETWORKS = tuple(ipaddress.ip_network(net) for net in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "169.254.0.0/16",
    "::1/128", "fc00::/7", "fe80::/10",
))


def is_local_address(ip):
    """True for an address on the local network (LAN_NETWORKS) — what Jellyfin
    treats as "in the local network" by default. Unparseable -> False."""
    try:
        addr = ipaddress.ip_address(str(ip).strip())
    except ValueError:
        return False
    if addr.version == 6 and addr.ipv4_mapped is not None:
        # How a dual-stack socket reports an IPv4 client (::ffff:a.b.c.d).
        addr = addr.ipv4_mapped
    return any(addr in net for net in LAN_NETWORKS)


def remote_access_denied(account, ip):
    """Jellyfin refuses a user without remote access from outside the LAN, but
    it only ever sees THIS container's LAN address. So the check is ours: the
    client's address is the one that matters (TRUST_PROXY behind a tunnel)."""
    return _policy(account).get("EnableRemoteAccess") is False and not is_local_address(ip)


def _lockout(policy):
    """(failures Jellyfin has counted, the limit that disables the account),
    or None when the account has no lockout (-1, missing, junk)."""
    limit = _policy_int(policy, "LoginAttemptsBeforeLockout")
    if limit is None or limit < 0:
        return None
    return max(0, _policy_int(policy, "InvalidLoginAttemptCount") or 0), limit


def account_refusal(account, ip):
    """Why this account must NOT be sent to AuthenticateByName from here (a
    reason for the log), or None to go ahead.

    Jellyfin DISABLES an account once Policy.InvalidLoginAttemptCount reaches
    Policy.LoginAttemptsBeforeLockout, and the counter only resets on a
    successful login. A disabled account needs an admin to revive it, so this
    public page must never be the thing that trips it.
    """
    policy = _policy(account)
    if policy.get("IsDisabled") is True:
        return "Jellyfin account is disabled"

    lockout = _lockout(policy)
    if lockout is not None:
        count, limit = lockout
        if limit <= 1:
            # Jellyfin disables once count+1 >= limit, so 1 means the FIRST
            # failure; a stored 0 does too (the policy editor maps 0 to 3 only
            # when a policy is saved, and the DTO reports the stored value).
            # Signing in elsewhere to reset the count can't help here: the
            # admin has to raise the limit.
            return f"account locks on its first failure (limit {limit}); raise its lockout limit"
        if count >= limit - 1:
            # The next failure would be the one that disables it. Signing in
            # to Jellyfin directly once resets the count.
            return f"account is {count}/{limit} failures from Jellyfin's lockout"

    if remote_access_denied(account, ip):
        return "account has no remote access and the client is outside the LAN"
    return None


def held_for_pending(account, pending):
    """A log reason if earlier sign-ins that got no answer (and that Jellyfin
    may still count) would leave this one able to disable the account."""
    lockout = _lockout(_policy(account))
    if not pending or lockout is None:
        return None
    count, limit = lockout
    if count + pending >= limit - 1:
        return (f"{pending} earlier sign-in(s) got no answer and may still be counted "
                f"({count}+{pending} of {limit}); waiting for them")
    return None


def _account_key(account, username):
    uid = account.get("Id")
    return uid if isinstance(uid, str) and uid else "name:" + username[:MAX_USERNAME_LEN].casefold()


def _log_login(verdict, username, ip, reason):
    # %r: the username (and, with TRUST_PROXY, the IP) are client-supplied;
    # repr() escapes CR/LF so they can't forge log lines. Never the password.
    app.logger.warning(
        "login %s for user %r from %r: %s", verdict, username[:MAX_USERNAME_LEN], ip, reason
    )


def _refuse_login(message, username, ip, reason):
    """Turn a sign-in away WITHOUT consuming an attempt."""
    _log_login("refused", username, ip, reason)
    flash(message)
    return redirect(url_for("login"))


def _fail_login(username, ip, reason):
    """A failed sign-in: consumes an attempt from the IP and username, and
    reads "wrong username or password" whatever the reason (the log has it)."""
    left = login_limiter.failed(ip, username)
    _log_login("failed", username, ip, f"{reason} ({left} attempts left for this IP)")
    if left > 0:
        flash(f"Wrong username or password. {left} attempt{'s' if left != 1 else ''} remaining.")
    # left == 0: this IP is now locked, and the redirect lands on the lockout page.
    return redirect(url_for("login"))


def _fail_quietly(username, ip, started, reason):
    """A failure decided WITHOUT asking Jellyfin: first wait about as long as
    a password check would have taken, so the answer time doesn't give away
    that the name doesn't exist or that the account is refused. Waits inside
    the sign-in lock, exactly where the real check would have."""
    password_check_timer.pad(started)
    return _fail_login(username, ip, reason)


def _minutes(seconds):
    minutes = max(1, math.ceil(seconds / 60))
    return f"{minutes} minute{'s' if minutes != 1 else ''}"


# ---------------------------------------------------------------- sessions


def revoke_sid(sid):
    now = time.time()
    with _revoked_lock:
        for old in [s for s, until in _revoked_sids.items() if until <= now]:
            del _revoked_sids[old]
        _revoked_sids[sid] = now + app.permanent_session_lifetime.total_seconds()


def sid_revoked(sid):
    with _revoked_lock:
        until = _revoked_sids.get(sid)
    return until is not None and until > time.time()


@app.before_request
def drop_revoked_session():
    # Clear a revoked session BEFORE any view can modify it: a modified session
    # is re-signed with a fresh timestamp, and a re-signed copy of a logged-out
    # cookie would outlive its entry on the revoked list.
    sid = session.get("sid")
    if isinstance(sid, str) and sid_revoked(sid):
        session.clear()


def authenticated():
    if session.get("auth") is not True:
        return False
    sid = session.get("sid")
    uid = session.get("uid")
    if not (isinstance(sid, str) and sid and isinstance(uid, str) and uid):
        # A session from before this upgrade: no sid means it could never be
        # revoked, no uid means it can't be re-checked against Jellyfin.
        session.clear()
        return False
    if sid_revoked(sid):
        session.clear()
        return False

    now = time.time()
    recheck_at = session.get("recheck_at")
    due = (
        not isinstance(recheck_at, (int, float))
        or isinstance(recheck_at, bool)
        or now >= recheck_at
        # Further out than we ever schedule: the clock jumped backwards.
        or recheck_at > now + REVALIDATE_SECONDS
    )
    if due:
        try:
            account = jellyfin.get_user(JELLYFIN_URL, JELLYFIN_API_KEY, uid)
        except JfError:
            # A Jellyfin outage must not sign everyone out. Keep the session,
            # and try again soon rather than on every request.
            session["recheck_at"] = now + REVALIDATE_RETRY_SECONDS
            return True
        if account is None:
            reason = "Jellyfin account was deleted"
        elif _policy(account).get("IsDisabled") is True:
            reason = "Jellyfin account is disabled"
        elif remote_access_denied(account, client_ip()):
            reason = "Jellyfin account lost remote access and the client is outside the LAN"
        else:
            reason = None
        if reason:
            app.logger.warning(
                "signed out user %r from %r: %s", str(session.get("user", "")), client_ip(), reason
            )
            session.clear()
            return False
        session["recheck_at"] = now + REVALIDATE_SECONDS
    return True


# TRUST_PROXY is read per request (so tests can flip it), but a bad value is
# warned about once per value, not once per request.
_trust_proxy_lock = threading.Lock()
_trust_proxy_warned = None


def trusted_proxies():
    """How many proxies TRUST_PROXY says sit in front of the app; 0 = none.

    Only a plain positive integer counts. Unset, empty and "0" mean no proxy.
    Anything else ("true", "yes", "-1", "1.5") also means no proxy — the safe
    reading, since believing X-Forwarded-For without a proxy lets anyone pick
    their own address — and is logged ONCE, because the operator clearly
    meant something by it and is otherwise left wondering why every visitor
    still shows up as the proxy.
    """
    global _trust_proxy_warned
    raw = os.environ.get("TRUST_PROXY", "").strip()
    # [0-9] rather than int(): int() also takes "+2", "1_0" and non-ASCII
    # digits, none of which anyone writes on purpose.
    if re.fullmatch(r"[0-9]{1,3}", raw):
        return int(raw)
    if raw:
        with _trust_proxy_lock:
            if _trust_proxy_warned == raw:
                return 0
            _trust_proxy_warned = raw
        app.logger.warning(
            "TRUST_PROXY=%r is not a number of proxies (1 for one proxy or tunnel, 2 for two in "
            "a row, ...); ignoring X-Forwarded-For and using the connecting address instead.",
            raw,
        )
    return 0


def client_ip():
    """Best-effort client IP. The login throttle, the remote-access check and
    the history all use this one answer.

    X-Forwarded-For is trivially spoofed by anyone who can reach the app, and
    this app is reachable on the LAN — so only believe it when the operator has
    explicitly said how many proxies are in front (TRUST_PROXY=<n>).
    """
    hops = trusted_proxies()
    if hops:
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded.strip():
            # Each trusted proxy appends the address IT observed to the RIGHT
            # of whatever it received. So the rightmost n entries were written
            # by our n proxies, and the n-th from the right is what the
            # outermost one saw: the client. Everything left of that is
            # client-supplied (a client can prepend a forged or a victim's IP)
            # and must never be trusted, even behind legitimate proxies.
            entries = forwarded.split(",")
            if len(entries) >= hops:
                return entries[-hops].strip()
            # Fewer entries than proxies: the chain isn't what the operator
            # described — the client went straight to an inner proxy (split
            # DNS, or around the tunnel), or TRUST_PROXY counts one too many.
            # The rightmost entry is still what the proxy next to the app saw,
            # and no client can pick it; anything left of it may be forged.
            # NOT the connecting address: that is the proxy itself, a private
            # address, and it would pass every such client off as LAN (past
            # the remote-access check) and share one throttle among them all.
            return entries[-1].strip()
    return request.remote_addr or ""


# Say so at startup, where the operator is looking, if TRUST_PROXY is unusable.
trusted_proxies()


def record_press(outcome, error="", user=""):
    """Record a button press. Never lets a history problem break the button."""
    global _last_started_cache, _last_started_fallback, _history_writable
    try:
        entry = history.record(
            outcome,
            ip=client_ip(),
            user_agent=request.headers.get("User-Agent", ""),
            error=error,
            user=user,
        )
        _history_writable = True
        if outcome == OUTCOME_STARTED:
            # This process just started a scan; remember it without waiting
            # for a re-read, so a file that is corrupted or quarantined right
            # after can't make it forget.
            with _state_lock:
                _last_started_cache = max(_last_started_cache, float(entry["ts"]))
        return entry
    except Exception:  # e.g. read-only /data — log it, but still serve the user
        app.logger.exception("failed to record scan history entry")
        # The page says so: otherwise nobody learns until the history (and,
        # after a restart, the cooldown) is simply gone.
        _history_writable = False
        if outcome == OUTCOME_STARTED:
            # The scan really started but we couldn't persist the row the
            # cooldown is derived from. Without this the cooldown would silently
            # cease to exist and the button could be held down to hammer
            # Jellyfin. Keep it alive in-process for the life of this process.
            with _state_lock:
                _last_started_fallback = time.time()
        return None


def cooldown_remaining():
    """Seconds left on the cooldown, derived from the last successful scan.

    Fails CLOSED: if the history file can't be read (or a STARTED record
    couldn't be written, or the file was quarantined as corrupt), we do NOT
    return 0 and hand out a free scan — the baseline is the newest start this
    process knows about from ANY source, or a full cooldown if we have none
    and can't read the file.
    """
    global _last_started_cache
    now = time.time()
    try:
        last_started = history.last_started_at()
        readable = True
    except Exception:
        app.logger.exception("failed to read scan history")
        last_started = 0.0
        readable = False

    with _state_lock:
        if last_started > _last_started_cache:
            _last_started_cache = last_started
        # A start further in the future than the skew can't be real: the clock
        # jumped back after it was noted. The disk rows get the same rule in
        # history.last_started_at(); honouring these would freeze the button.
        candidates = (last_started, _last_started_cache, _last_started_fallback)
        baseline = max((t for t in candidates if t <= now + FUTURE_SKEW_SECONDS), default=0.0)

    if baseline <= 0 and not readable:
        # Never observed a successful scan, and now we can't read the file.
        # Assume a scan just happened rather than risk hammering Jellyfin.
        return float(COOLDOWN_SECONDS)
    return min(float(COOLDOWN_SECONDS), max(0.0, baseline + COOLDOWN_SECONDS - now))


def _percent(value):
    """Jellyfin's CurrentProgressPercentage as a JSON-safe 0..100 float.
    (NaN/Infinity would make jsonify emit invalid JSON.)"""
    try:
        pct = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(pct):
        return 0.0
    return min(100.0, max(0.0, pct))


# ---------------------------------------------------------------- routes


@app.route("/healthz")
def healthz():
    # For the Docker HEALTHCHECK: no auth, no Jellyfin call, no information.
    return "ok", 200, {"Content-Type": "text/plain; charset=utf-8"}


@app.route("/login", methods=["GET", "POST"])
def login():
    ip = client_ip()

    if request.method == "GET":
        error = get_flashed_messages()  # drained either way, so none go stale
        # Shown once, right after a failure. Never the password.
        username = session.pop("login_username", "")
        remaining = login_limiter.ip_locked(ip)
        if remaining:
            return render_template("login.html", locked=True, locked_seconds=remaining, error=None, username="")
        return render_template(
            "login.html",
            locked=False,
            locked_seconds=0,
            error=error[0] if error else None,
            username=username if isinstance(username, str) else "",
        )

    username = request.form.get("username") or ""
    password = request.form.get("password") or ""

    # Cheap rejection before queueing for the sign-in lock (re-checked inside).
    if login_limiter.ip_locked(ip):
        _log_login("refused", username, ip, "client IP is locked out")
        return redirect(url_for("login"))

    if username:
        session["login_username"] = username[:MAX_USERNAME_LEN]
    # None of these is a credential verdict, so none consumes an attempt; all
    # are still logged like every other refused sign-in.
    if not username:
        return _refuse_login(
            "Enter your Jellyfin username and password.", username, ip, "missing username"
        )
    if not password:
        # An empty password is NEVER forwarded. Jellyfin signs an account
        # that has no password in with an empty one, so anyone who knows (or
        # guesses) the name would be in. Such accounts are usually meant for
        # the LAN only, and nothing here can hold that line: Jellyfin only
        # ever sees this container's address, and without TRUST_PROXY our own
        # remote-access check sees every tunnel visitor as the tunnel's
        # private address. So a passwordless account can't sign in here at
        # all, and the message says why instead of a bare "enter a password".
        return _refuse_login(MSG_NO_PASSWORD, username, ip, "empty password")

    if not JELLYFIN_URL:
        return _refuse_login("JELLYFIN_URL is not configured.", username, ip, "JELLYFIN_URL is not set")
    if not JELLYFIN_API_KEY:
        # The account guard below reads the user's Policy with the API key.
        return _refuse_login(
            "JELLYFIN_API_KEY is not configured.", username, ip, "JELLYFIN_API_KEY is not set"
        )

    if not _login_lock.acquire(timeout=LOGIN_QUEUE_SECONDS):
        return _refuse_login(MSG_BUSY, username, ip, "timed out queueing behind other sign-ins")
    try:
        return _decide_login(username, password, ip)
    finally:
        _login_lock.release()


def _decide_login(username, password, ip):
    """The atomic part of a sign-in. Caller holds _login_lock."""
    if login_limiter.ip_locked(ip):
        # Locked by a request that was ahead of this one in the queue.
        _log_login("refused", username, ip, "client IP is locked out")
        return redirect(url_for("login"))

    wait = login_limiter.user_locked(username)
    if wait:
        return _refuse_login(
            f"Too many failed sign-ins for this username. Try again in {_minutes(wait)}.",
            username, ip, "username is locked out",
        )

    # Look the account up with the API key BEFORE sending the password, so a
    # sign-in that Jellyfin would turn away — or that would disable the account
    # — is never attempted.
    try:
        account = jellyfin.find_user(jellyfin.list_users(JELLYFIN_URL, JELLYFIN_API_KEY), username)
    except JfError as exc:
        # A connection failure is NOT a failed attempt: it says nothing about
        # the credentials, so it must not consume one.
        return _refuse_login(MSG_UNREACHABLE, username, ip, f"user lookup failed: {exc}")

    # From here on, every failure that depends on the account — no such
    # user, disabled, LAN-only, about to lock, a 403 — has the same message,
    # the same attempt cost and about the same answer time as a wrong
    # password: the page must not reveal which usernames exist.
    started = password_check_timer.now()
    if account is None:
        return _fail_quietly(username, ip, started, "no such Jellyfin user")

    refusal = account_refusal(account, ip)
    if refusal:
        return _fail_quietly(username, ip, started, f"refused without asking Jellyfin: {refusal}")

    key = _account_key(account, username)
    held = held_for_pending(account, pending_sign_ins.count(key))
    if held:
        # Only ever follows an unanswered sign-in, which already said "can't
        # reach": saying so again reveals nothing new, and costs no attempt.
        return _refuse_login(MSG_UNREACHABLE, username, ip, held)

    try:
        user = authenticate_jellyfin(username, password)
    except JellyfinAmbiguous as exc:
        # Probably counted by Jellyfin: hold it against the account (the
        # disable guard and the username streak), not against the IP — the
        # client learned nothing from it.
        pending_sign_ins.add(key)
        login_limiter.user_failed(username)
        return _refuse_login(MSG_UNREACHABLE, username, ip,
                             f"{exc}; counted as a probable failure for {pending_sign_ins.hold}s")
    except JellyfinRefused:
        return _fail_login(username, ip, "Jellyfin refused the sign-in (HTTP 403)")
    except JellyfinUnreachable as exc:
        return _refuse_login(MSG_UNREACHABLE, username, ip, str(exc))

    if user is None:
        return _fail_login(username, ip, "wrong password")

    uid = user.get("Id") or account.get("Id")
    if not isinstance(uid, str) or not uid:
        # Without an Id the session could never be re-validated.
        return _refuse_login(MSG_UNREACHABLE, username, ip, "Jellyfin returned no user Id")

    # Forgives this IP's failures against THIS account only; see succeeded().
    login_limiter.succeeded(ip, username)
    name = user.get("Name", "")
    # clear() first: drops pre-login state AND rotates the session so a
    # pre-login session value can never survive (session fixation).
    session.clear()
    session["auth"] = True
    session["user"] = name if isinstance(name, str) else ""
    session["uid"] = uid
    session["sid"] = secrets.token_urlsafe(24)
    session["recheck_at"] = time.time() + REVALIDATE_SECONDS
    return redirect(url_for("index"))


@app.route("/logout", methods=["POST"])
def logout():
    # POST only: a GET logout can be fired cross-site by an <img>, while the
    # SameSite=Lax cookie never rides along on a cross-site POST.
    sid = session.get("sid")
    if isinstance(sid, str) and sid:
        revoke_sid(sid)
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
def index():
    if not authenticated():
        return redirect(url_for("login"))
    return render_template("index.html", incoming_enabled=incoming.enabled, user=session.get("user", ""))


@app.route("/api/scan/state")
def scan_state():
    if not authenticated():
        return jsonify({"error": "Unauthorized"}), 401
    remaining = cooldown_remaining()
    return jsonify({"active": remaining > 0, "remaining_ms": int(remaining * 1000)})


@app.route("/api/history")
def scan_history():
    if not authenticated():
        return jsonify({"error": "Unauthorized"}), 401

    def as_int(name, default):
        try:
            return max(0, int(request.args.get(name, default)))
        except (TypeError, ValueError):
            return default

    limit = min(as_int("limit", 50), MAX_ENTRIES)
    offset = as_int("offset", 0)
    try:
        entries = history.entries(limit=limit, offset=offset)
        total = history.count()
    except Exception:
        # An unreadable history must not pass for "no presses yet": the page
        # shows its error state on a non-OK response. "writable" rides along
        # because a broken /data usually breaks reads too: this 503 is often
        # the only answer the page gets while its warning matters most.
        app.logger.exception("failed to read scan history")
        return jsonify({"error": "Couldn't read scan history", "writable": _history_writable}), 503
    return jsonify({"entries": entries, "total": total, "writable": _history_writable})


@app.route("/api/scan/progress")
def scan_progress():
    if not authenticated():
        return jsonify({"error": "Unauthorized"}), 401
    try:
        # Found by Key: task names are localized, and the old name-keyword
        # match ("refresh", "media", ...) also caught unrelated tasks.
        task = jellyfin.library_task(JELLYFIN_URL, JELLYFIN_API_KEY)
    except JfError as exc:
        # str(exc) is a short fixed reason; raw requests text names the
        # internal Jellyfin host:port.
        return jsonify({"error": str(exc)}), 502
    note_scan_running(jellyfin.task_busy(task))
    if jellyfin.task_busy(task):
        name = task.get("Name")
        return jsonify({
            "state": "running",
            "percent": round(_percent(task.get("CurrentProgressPercentage")), 1),
            "name": name if isinstance(name, str) and name else "Scan Media Library",
        })
    # How the last run ended (Completed/Failed/Cancelled/Aborted), or null. A
    # page that watched a scan disappear would otherwise have to guess, and it
    # used to guess "complete 100%" for a scan that was cancelled or failed.
    try:
        last = jellyfin.last_result(task)
    except Exception as exc:
        # Only extra detail, read from whatever Jellyfin sent (an offset of
        # +24:00 made it raise). The idle answer could not fail before "last"
        # existed and must not start now; the page falls back to its old
        # wording. %r: the text may come from Jellyfin, so escape it.
        app.logger.warning("couldn't read how the last library scan ended: %r", exc)
        last = None
    return jsonify({"state": "idle", "percent": 0, "last": last})


@app.route("/api/downloads")
def api_downloads():
    if not authenticated():
        return jsonify({"error": "Unauthorized"}), 401
    try:
        return jsonify(incoming.payload())
    except downloads.Busy:
        # Another viewer's check is still running and there's nothing older to
        # show. The page keeps its last render and tries again.
        return jsonify({"error": "Still checking downloads"}), 503
    except Exception:  # never let a raw exception (URLs, credentials) reach the page
        app.logger.exception("failed to build the downloads panel")
        return jsonify({"error": "Couldn't check downloads"}), 500


@app.route("/api/scan", methods=["POST"])
def scan():
    if not authenticated():
        return jsonify({"error": "Unauthorized"}), 401

    user = session.get("user", "")
    with scan_lock:
        remaining = cooldown_remaining()
        if remaining > 0:
            record_press(OUTCOME_COOLDOWN, user=user)
            return jsonify({"error": "Scan cooldown active", "remaining_ms": int(remaining * 1000)}), 429

        if not JELLYFIN_URL or not JELLYFIN_API_KEY:
            msg = "JELLYFIN_URL or JELLYFIN_API_KEY not configured"
            record_press(OUTCOME_ERROR, error=msg, user=user)
            return jsonify({"error": msg}), 500

        # A second /Library/Refresh doesn't queue: it CANCELS the running scan
        # and restarts it at 0%. So while one runs (or is winding down after a
        # cancel), refuse — and don't start the cooldown, since nothing started.
        try:
            task = jellyfin.library_task(JELLYFIN_URL, JELLYFIN_API_KEY)
        except JfError:
            # Not knowing is no reason to refuse: the POST below is the real
            # test of whether Jellyfin is there.
            task = None
        if jellyfin.task_busy(task):
            record_press(OUTCOME_BUSY, user=user)
            return jsonify({"error": "A library scan is already running.", "running": True}), 409

        note = ""
        try:
            jellyfin.post(JELLYFIN_URL, JELLYFIN_API_KEY, "/Library/Refresh")
        except jellyfin.JfAmbiguous as exc:
            # The Refresh left and Jellyfin most likely queued it; only the
            # answer was lost (read timeout, dropped connection). Calling that
            # a failure would start no cooldown and invite a retry — and a
            # second Refresh restarts the running scan at 0%. So it is a start,
            # with the doubt noted on the row. (Must precede the JfError
            # clause: JfAmbiguous is a subclass.)
            note = str(exc)
        except JfError as exc:
            # The short reason only: it is shown to every signed-in user and
            # kept in the shared history, and raw requests text contains the
            # internal Jellyfin host:port.
            record_press(OUTCOME_ERROR, error=str(exc), user=user)
            return jsonify({"error": str(exc)}), 502

        # Recorded only on a (probable) start: this entry IS the cooldown.
        record_press(OUTCOME_STARTED, error=note, user=user)

    # The panel's cached "ready to add" rows are about to become "adding now".
    # Outside scan_lock: nothing about the panel may hold up other presses.
    incoming.invalidate()
    return jsonify({"status": "started"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
