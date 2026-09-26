"""Server-side login throttling.

This replaces a lockout that lived in the (client-held) session cookie. That
version was bypassed simply by discarding the cookie: in a repro, 30 of 30
password guesses reached Jellyfin. Here the counters live in the process, keyed
on things the client can't shed by clearing cookies:

  * per client IP:  3 failures within 5 minutes lock that IP for 1 hour
    (the same numbers the cookie lockout used);
  * per username:   6 failures within 15 minutes, from ANY mix of IPs, refuse
    that username for 15 minutes. This is what stops a guesser who rotates
    IPs, and it keeps this public page from being the thing that grinds a
    Jellyfin account toward Jellyfin's own lockout.

An IPv6 client is counted per /64, not per address: one client holds at
least a /64 and can pick any address in it, so per-address counting would hand
it 2^64 fresh sets of attempts (30 of 30 spray guesses got through in a repro).

Everything is in memory and per process (gunicorn must run ONE worker, see
app.py), thread-safe, and pruned, so an attacker spraying random usernames or
addresses can't grow it without bound.
"""

import collections
import ipaddress
import math
import secrets
import threading
import time

# Keys are attacker-controlled (a username can be megabytes of form data).
MAX_KEY_LEN = 128


class Throttle:
    """`limit` failures for one key inside `window` seconds lock that key for
    `lockout` seconds. Not thread-safe on its own: LoginLimiter holds the lock.
    """

    def __init__(self, limit, window, lockout, max_keys):
        self.limit = limit
        self.window = window
        self.lockout = lockout
        self.max_keys = max_keys
        # key -> [(time, tag), ...] inside the window, oldest first. The tag
        # says which account a failure was against, so a success can forgive
        # just that account's share. Dict order is least-recently-failed first
        # (a key is re-inserted on every failure), which is what the hard cap
        # evicts.
        self._failures = {}
        # key -> epoch the lock ends. Every lock lasts `lockout`, so insertion
        # order is also expiry order.
        self._locked = {}

    def remaining(self, key, now):
        """Seconds left on this key's lock, 0.0 if it isn't locked."""
        until = self._locked.get(key)
        if until is None:
            return 0.0
        if until <= now:
            del self._locked[key]
            return 0.0
        return until - now

    def fail(self, key, now, tag=None):
        """Record a failure. Returns the attempts left (0 = the key is locked)."""
        if self.remaining(key, now):
            return 0
        recent = [(t, g) for t, g in self._failures.pop(key, ()) if now - t < self.window]
        recent.append((now, tag))
        if len(recent) >= self.limit:
            # Locking starts a clean slate: when the lock ends the key gets a
            # full set of attempts, not an instant re-lock.
            self._locked[key] = now + self.lockout
            self._cap(self._locked)
            return 0
        self._failures[key] = recent
        self._cap(self._failures)
        return self.limit - len(recent)

    def recent(self, key, now):
        """Failures for this key inside the current window."""
        return sum(1 for t, _tag in self._failures.get(key, ()) if now - t < self.window)

    def forgive(self, key, tag):
        """Drop this key's failures that carry `tag`; the rest still count."""
        kept = [(t, g) for t, g in self._failures.get(key, ()) if g != tag]
        if kept:
            self._failures[key] = kept
        else:
            self._failures.pop(key, None)

    def prune(self, now):
        self._failures = {k: v for k, v in self._failures.items() if now - v[-1][0] < self.window}
        self._locked = {k: until for k, until in self._locked.items() if until > now}

    def _cap(self, table):
        # Only reachable under a flood from thousands of addresses; evicting the
        # stalest entry beats letting memory grow without bound.
        while len(table) > self.max_keys:
            del table[next(iter(table))]

    def keys(self):
        return list(self._failures) + list(self._locked)

    def __len__(self):
        return len(self._failures) + len(self._locked)


def _ip_key(ip):
    """The counter an address belongs to: IPv4 as itself, IPv6 by its /64.

    An IPv4-mapped address (::ffff:a.b.c.d, how a dual-stack socket reports an
    IPv4 client) is the IPv4 client it wraps. Anything unparseable is keyed by
    its (clipped) text, which still limits a client that repeats it.
    """
    text = str(ip or "")[:MAX_KEY_LEN].strip()
    try:
        addr = ipaddress.ip_address(text)
    except ValueError:
        return text
    if addr.version == 6:
        if addr.ipv4_mapped is not None:
            return str(addr.ipv4_mapped)
        # int() ignores a zone id (fe80::1%eth0): same /64 on any interface.
        return str(ipaddress.IPv6Network((int(addr) >> 64 << 64, 64)))
    return str(addr)


def _user_key(username):
    # Jellyfin matches user names case-insensitively, so "Alice" and "alice"
    # are the same account and must share one counter. Clip BEFORE casefold so
    # a megabyte username isn't casefolded in full.
    return str(username or "")[:MAX_KEY_LEN].casefold()[:MAX_KEY_LEN]


class LoginLimiter:
    def __init__(
        self,
        clock=None,
        ip_limit=3,
        ip_window=5 * 60,
        ip_lockout=60 * 60,
        user_limit=6,
        user_window=15 * 60,
        user_lockout=15 * 60,
        max_keys=10_000,
        prune_every=60,
    ):
        self._clock = clock or time.time
        self.ip = Throttle(ip_limit, ip_window, ip_lockout, max_keys)
        self.user = Throttle(user_limit, user_window, user_lockout, max_keys)
        self._prune_every = prune_every
        self._last_prune = None
        self._lock = threading.Lock()

    def _now(self):
        """The current time; prunes expired entries now and then. Hold the lock."""
        now = self._clock()
        if self._last_prune is None or abs(now - self._last_prune) >= self._prune_every:
            self.ip.prune(now)
            self.user.prune(now)
            self._last_prune = now
        return now

    def ip_locked(self, ip):
        """Whole seconds (rounded UP) until this IP may try again; 0 = it may."""
        with self._lock:
            return math.ceil(self.ip.remaining(_ip_key(ip), self._now()))

    def user_locked(self, username):
        """Whole seconds (rounded UP) until this username may try again; 0 = it may."""
        with self._lock:
            return math.ceil(self.user.remaining(_user_key(username), self._now()))

    def failed(self, ip, username):
        """Record one failed sign-in. Returns the attempts this IP has left
        (0 = the IP is now locked)."""
        with self._lock:
            now = self._now()
            user = _user_key(username)
            self.user.fail(user, now)
            return self.ip.fail(_ip_key(ip), now, tag=user)

    def user_failed(self, username):
        """Count a failure against the USERNAME only. For a sign-in whose
        answer never arrived: Jellyfin probably counted it against the
        account, but the client learned nothing, so the IP isn't charged."""
        with self._lock:
            self.user.fail(_user_key(username), self._now())

    def ip_failures(self, ip):
        """Failures this IP has inside the current window (0 once it is locked:
        the lock consumed the streak)."""
        with self._lock:
            return self.ip.recent(_ip_key(ip), self._now())

    def succeeded(self, ip, username):
        """A successful sign-in forgives this IP's failures against THAT
        account only (the owner mistyping, then getting it right — what
        clearing the session used to do). Failures against other accounts
        stay: otherwise anyone with an account of their own could reset their
        IP's count at will and never hit the 3-strike lock.

        The USERNAME streak is deliberately kept: otherwise a distributed
        guesser is reset every time the real owner signs in."""
        with self._lock:
            self.ip.forgive(_ip_key(ip), _user_key(username))

    def tracked(self):
        """How many keys are held in memory (for tests / sanity)."""
        with self._lock:
            return len(self.ip) + len(self.user)


class PendingSignIns:
    """Sign-ins whose answer never arrived (read timeout, connection dropped
    after sending), per Jellyfin account.

    The password did reach Jellyfin, and Jellyfin counts a wrong one when it
    finishes — possibly after the next sign-in has already read the account's
    InvalidLoginAttemptCount and found room. Until `hold` seconds have passed,
    each such sign-in is assumed to have counted, on top of whatever Jellyfin
    reports. (Conservative on purpose: one that did land is briefly counted
    twice, which costs a wait, never the account.)

    Monotonic clock. Keys are Jellyfin account ids (only accounts that exist
    get this far), and expired entries are dropped on every call.
    """

    def __init__(self, hold=120, clock=None):
        self.hold = hold
        self._clock = clock or time.monotonic
        self._until = {}
        self._lock = threading.Lock()

    def _prune(self, now):
        for key in list(self._until):
            live = [t for t in self._until[key] if t > now]
            if live:
                self._until[key] = live
            else:
                del self._until[key]

    def add(self, key):
        with self._lock:
            now = self._clock()
            self._prune(now)
            self._until.setdefault(key, []).append(now + self.hold)

    def count(self, key):
        with self._lock:
            self._prune(self._clock())
            return len(self._until.get(key, ()))

    def __len__(self):
        with self._lock:
            self._prune(self._clock())
            return len(self._until)


class PasswordCheckTimer:
    """How long Jellyfin takes to check a password, so that sign-ins refused
    WITHOUT asking it (no such user, a disabled or LAN-only account, one
    failure from lockout) answer after about the same time.

    Otherwise the answer time alone says whether a name exists: in a repro a
    made-up name failed in ~10 ms and a real one in ~265 ms, with the same
    message. The pad is drawn from recently measured checks, so it varies the
    way real ones do.
    """

    KEEP = 16
    MAX_PAD = 5.0   # never hold the (serialised) sign-in queue longer than this

    def __init__(self, default=0.3, clock=None, sleep=None):
        self.default = default
        self._clock = clock or time.monotonic
        self._sleep = sleep or time.sleep
        self._samples = collections.deque(maxlen=self.KEEP)
        self._lock = threading.Lock()

    def now(self):
        """A reading of this timer's clock, for record() and pad()."""
        return self._clock()

    def record(self, seconds):
        """One AuthenticateByName call that came back with a verdict."""
        if isinstance(seconds, (int, float)) and math.isfinite(seconds) and seconds >= 0:
            with self._lock:
                self._samples.append(float(seconds))

    def samples(self):
        with self._lock:
            return list(self._samples)

    def pad(self, started):
        """Sleep until about one password check has passed since `started`
        (a reading of this timer's clock)."""
        with self._lock:
            target = secrets.choice(self._samples) if self._samples else self.default
        delay = min(target, self.MAX_PAD) - (self._clock() - started)
        if delay > 0:
            self._sleep(delay)
