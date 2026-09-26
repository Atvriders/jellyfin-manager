# Incoming — qBittorrent downloads panel

**Date:** 2026-09-26 · **Status:** implemented 2026-09-26 (see the plan's Execution notes)

## Purpose

People who open the scan page should be able to see two things without asking the owner:

1. **What is downloading right now**, on either qBittorrent box.
2. **What finished downloading but is not in Jellyfin yet.** That is exactly the case where pressing
   **Scan media library** helps, so the page should say so.

The feature is read-only: it never pauses, deletes, or changes a torrent.

## Decisions made during brainstorming

| Question | Decision |
|---|---|
| Where do finished torrents go? | qBittorrent saves **straight into the library folders** Jellyfin watches (no *arr import step). So names on disk match names in qBittorrent. |
| Which instances? | **Several**, configured as a labelled list (the owner runs two boxes). |
| How do we decide "not in Jellyfin yet"? | **Finished after the last library scan started, minus anything Jellyfin already has by name.** Details below. |
| Who sees it? | Everyone who can log in (same audience as the dive log). |

## Configuration

One environment variable turns the feature on:

```
QBITTORRENT_INSTANCES=Box 1=http://192.0.2.10:8080, Box 2=http://192.0.2.11:8080
```

- Comma-separated `label=url` pairs. Whitespace around labels, URLs, and commas is trimmed.
- A URL may carry credentials, `http://user:pass@host:8080` (percent-encode special characters). They
  are removed from the URL before any request, used only for `POST /api/v2/auth/login`, and never
  logged or returned. With no credentials, the client relies on qBittorrent's "bypass authentication
  for clients on localhost / whitelisted subnets" setting.
- Unset or empty means the feature is off: `/api/downloads` returns `{"enabled": false}` and the page
  does not render the panel.
- A malformed entry (no `=`, no scheme, empty label, duplicate label) is logged once at startup and
  skipped. The remaining valid entries still work.

## Backend — `app/downloads.py` (new)

### qBittorrent client (one per instance)

- `requests.Session` per instance. Every request sends `Referer: <base url>`, which qBittorrent's
  CSRF check wants.
- Fetch: `GET /api/v2/torrents/info`, with a 5 s timeout.
- **Auth:** if credentials are configured and there is no session yet, log in first. A `403` (or a
  `401`, from qBittorrent 5.2 on) on any call means the session expired: log in again **once** and
  retry. A login succeeds on `200` with the body `Ok.` (qBittorrent up to 5.1) or on `204 No
  Content` (5.2 and later, WebAPI 2.14). Anything else (`200 Fails.`, `401`, `403`), or a second
  `401`/`403` on a call, is the error `login failed`.
- **Errors reported to the client are fixed categories only:** `unreachable`, `login failed`,
  `unexpected response`. Raw exception text is never returned, because it can contain URLs or
  credentials.
- Instances are fetched **concurrently** (a small thread pool), so one dead box costs 5 s at most
  and never delays the other.

### Classifying torrents

Fields used: `name`, `state`, `progress`, `dlspeed`, `eta`, `size`, `amount_left`, `added_on`,
`completion_on`, `content_path`, `save_path`.

**Complete** means a state in the upload family (`uploading`, `stalledUP`, `queuedUP`, `forcedUP`,
`checkingUP`, `pausedUP`, `stoppedUP`), or otherwise `progress >= 1` with `amount_left` not negative.
A negative `amount_left` is never complete: qBittorrent computes `amount_left = wanted - done` and an
unclamped `progress = done / wanted`, so the ENOSPC self-lock reports `progress > 1` together with
`amount_left < 0` (its "Unexpected data detected" warning), and that torrent must show as `stuck`.

`completion_on` values `<= 0` or absurd values (above year 2100) mean "unknown". A complete torrent
with no known completion time is never a *ready* candidate.

**Downloading** covers every incomplete torrent. Each one gets a display state:

| qBittorrent state(s) | display state |
|---|---|
| `downloading`, `forcedDL` | `downloading` |
| `metaDL`, `forcedMetaDL` | `metadata` |
| `stalledDL` | `stalled` |
| `queuedDL` | `queued` |
| `checkingDL`, `checkingResumeData`, `allocating`, `moving` | `checking` |
| `pausedDL`, `stoppedDL` | `paused` |
| `error`, `missingFiles` | `error` |
| any incomplete torrent with `amount_left < 0` (overrides the rows above) | `stuck` — the ENOSPC self-lock; the UI hint says "recheck in qBittorrent" |
| anything else | `downloading` if `dlspeed > 0`, otherwise `stalled` |

`eta` of `8640000` (qBittorrent's "infinity") or `<= 0` becomes `null`.

Order: `downloading` and `metadata` first, then `checking`, `stalled`, `stuck`, `error`, `queued`,
and `paused` last. Within a group, by progress, highest first.

### "Ready to add" — finished but not in Jellyfin yet

Inputs from Jellyfin, using the existing `JELLYFIN_API_KEY`:

1. `GET /ScheduledTasks`: pick the task whose `Key == "RefreshLibrary"`. From it read `State` (a
   `Running` or `Cancelling` state means a scan is in progress), `CurrentProgressPercentage`, and
   `LastExecutionResult.{StartTimeUtc, Status}`.
2. Recently added items: `GET /Items?Recursive=true&IsFolder=false&SortBy=DateCreated&SortOrder=Descending&Fields=Path,DateCreated&EnableImages=false&EnableUserData=false&EnableTotalRecordCount=false&StartIndex=…&Limit=200`.
   Page through until an item's `DateCreated` is older than the *match window start* (below), or
   until 2,000 items. This is only called when there is at least one candidate. The paging has a
   12 s deadline from the start of the build; past it, `ready_status` is `"unknown"`.

**Candidate:** a complete torrent whose `completion_on` is later than the **coverage point** minus
120 s. The slack covers clock skew between the boxes; it errs toward showing, and the name match
removes false positives.

**Coverage point:**
- If `LastExecutionResult.Status == "Completed"`, the coverage point is its `StartTimeUtc`. That run
  started after the torrent finished, so it saw the file.
- Any other status (`Failed`, `Cancelled`, `Aborted`), or no result at all: the last run cannot be
  trusted to have seen anything. Fall back to **"finished in the last 48 hours"**.
- Jellyfin timestamps carry 7 fractional digits and a `Z` suffix. The parser is our own (truncate the
  fraction to 6 digits, accept `Z` and `±hh:mm`) because Python 3.10's `fromisoformat` rejects this
  format. Tests run on 3.10 locally; the image runs 3.12.

**Name match — drop the candidate if Jellyfin already has it.**
- The torrent's *match keys*: the basename of `content_path`, plus `name` when it differs.
- If `content_path` equals `save_path` (a multi-file torrent created without a root folder), the
  basename would be the library folder itself, which matches everything. In that case fetch
  `GET /api/v2/torrents/files?hash=…` and use the file basenames as keys instead. This only happens
  for candidates, so it is rare and cheap. Past the 12 s build deadline it is skipped, and the
  torrent keeps no keys (it stays visible: errs toward showing).
- A candidate matches when any key equals any **path component** of a Jellyfin item's `Path` (split
  on `/` and `\`, both sides NFC-normalized, exact case). This covers a single file (the key is the
  last component) and a folder (the key is a middle component).
- **Match window start** = the earliest candidate's `added_on` minus 1 day. The anchor is `added_on`,
  not `completion_on`, because a library set to "use file creation date" gives the item a
  `DateCreated` near when the download *started*.

**Scan running:** if the `RefreshLibrary` task is `Running` or `Cancelling`, unmatched candidates are returned with
`adding: true`. The UI then says they are being added now. When the scan finishes,
`LastExecutionResult` moves forward and the normal rule takes over again. A torrent that finished
part-way through a scan correctly stays a candidate until a later scan or a name match.

**Unknown is not zero:** if Jellyfin cannot be reached or answers with garbage, `ready_status` is
`"unknown"` and `ready` is empty. The UI says "Can't check Jellyfin right now". It never says
"nothing waiting".

### Endpoint — `GET /api/downloads`

Login required (`401` JSON otherwise, like every other `/api` route). Response:

```json
{
  "enabled": true,
  "checked_at": 1790000000,
  "sources": [{"label": "Box 1", "ok": true}, {"label": "Box 2", "ok": false, "error": "unreachable"}],
  "downloading": [{"source": "Box 1", "name": "…", "progress": 0.42, "state": "downloading",
                   "dlspeed": 5242880, "eta": 1800, "size": 4294967296}],
  "downloading_total": 3,
  "ready": [{"source": "Box 1", "name": "…", "completed_at": 1789999000, "size": 1073741824, "adding": false}],
  "ready_status": "ok",
  "scan": {"running": false, "percent": 0}
}
```

- **Field allow-list.** Only the fields above leave the server. No hash, no path, no tracker, no
  category, no instance URL.
- `downloading` is capped at 25 rows; `downloading_total` is the true count. `ready` is sorted
  most recently finished first and capped at 25.
- **Cache:** the whole payload is cached for **10 s**, counted from when its build *finished*, on the
  monotonic clock (a wall-clock step back must not freeze it). One build runs at a time, and viewers
  that arrive during it get that build's result however long it took, so any number of viewers
  costs one upstream round per 10 s. Waiting on another viewer's build is capped at 15 s; after that
  the viewer gets the last payload, or `503` when there is none (the page keeps its last render).
- A successful `POST /api/scan` invalidates the cache after the scan lock is released. Invalidation
  never waits for a build in progress; a build that started before it is not reused as fresh.

### Existing `/api/scan/progress`

It currently keyword-matches task *names* (`scan`, `refresh`, `media`, `library`), which also matches
unrelated tasks. It changes to select `Key == "RefreshLibrary"` through the same helper the downloads
code uses, and it stops returning raw exception text.

## Frontend — `app/templates/index.html`

- **New station "Incoming"** between *Scan console* and *Dive log*. It reuses the existing
  `.station` / `.panel` / `.panel-head` structure, fonts, and restrained gold. No new ornament, and the
  jellyfish is untouched. It does not render at all when `enabled` is false.
- **Downloading group.** One row per torrent:
  - name, ellipsized, with the full name in `title`
  - a small source tag (`Box 1` / `Box 2`)
  - a thin progress bar and percent
  - speed and ETA
  - a state word when the state isn't plain `downloading`; `stuck` also shows "recheck in qBittorrent"

  Paused rows are dimmed and come last. Eight rows are visible, then "+N more". When nothing is
  downloading, one quiet line says so. While some box can't be reached, the "nothing" lines of both
  groups speak only for the boxes that answered ("Nothing downloading on Box 1."); with no box
  reachable they say "Can't reach qBittorrent right now." A box that didn't answer is unknown, not
  zero.
- **Ready to add group.** One row each: name, source tag, "finished 12 min ago". Rows with
  `adding: true` read "adding now…".
- **Per-source problems** appear as one line, e.g. "Box 2: unreachable", and
  `ready_status: "unknown"` shows "Can't check Jellyfin right now". Neither hides the healthy data.
- **Nudge on the Scan console**, under the button, when `ready` has non-`adding` rows:
  - "2 finished downloads aren't in Jellyfin yet — scan to add them."
  - during the cooldown it adds: "Scan available in 23:10"
  - while a scan runs: "Adding 2 downloads now…"

  The button itself does not flash.
- **Polling:** `/api/downloads` every 15 s while the tab is visible; paused while it is hidden; an
  immediate refresh on becoming visible, one 3 s after a scan press, and one 11.5 s after the console
  sees a scan end (once the server's 10 s cache has turned over). A `401` sends the browser to
  `/login`. Requests never overlap.
- **Rendering:** every string from the server is set with `textContent`, never `innerHTML`.
  Progress widths are clamped numbers.
- **Accessibility and layout:** the section is an `aria-labelledby` region. The nudge is announced
  through its own polite live region (`#incoming-announce`, so it never clobbers the cooldown
  announcement in `#sr-announce`), only when the count changes.
- **Coupling to the scan console:** none through globals. The existing inline script dispatches
  `scan:cooldown` (`detail.until`, epoch ms, `0` when over), `scan:started`, and `scan:activity`
  (`detail.running`, whether the console sees a scan under way) DOM events; `app/static/incoming.js`
  listens for them. "Adding now" follows `scan:activity`, which the console checks every 2 s,
  rather than the panel's own answer, which can be 25 s old. Just after a scan ends, until the
  server's cache has turned over, the rows claim neither "adding now" nor "scan to add them". The panel works at 390 px wide
  and respects `prefers-reduced-motion`, so the progress bars do not animate.

## Files

- `app/downloads.py` — new: config parsing, qBittorrent client, classification, cache, payload
  builder. Jellyfin calls go through the shared `app/jellyfin.py` (new; also used by `app.py`).
- `app/app.py` — `/api/downloads`, the `RefreshLibrary` fix, and cache invalidation on scan.
- `app/templates/index.html` — the Incoming station markup/CSS, the nudge slot, and three DOM events.
- `app/static/incoming.js` — new: polling, rendering, and the nudge.
- `app/tests/test_downloads.py` — new. `app/tests/test_jellyfin.py` — new (the shared helpers and the
  timestamp parser). `app/tests/test_incoming_e2e.py` — new (playwright; skips when no browser is
  available, e.g. in CI).
- `README.md`, `docker-compose.yml`, `.env.example` — document `QBITTORRENT_INSTANCES` using RFC 5737
  example addresses and placeholder labels, never real LAN IPs or hostnames.

## Testing

**pytest,** with fake qBittorrent and Jellyfin via monkeypatched `requests`:
- config parsing: valid, malformed, duplicate, credentials stripped
- the state table, one row per state, including `stuck`
- the ready rule: before/after the coverage point, the slack, a non-`Completed` last run → 48 h
  fallback, a scan running → `adding`
- name matching: file, folder, no-root-folder via `files`, the NFC case, the window anchored on
  `added_on`
- an unreachable instance next to a healthy one
- 403 → re-login once, then success; 403 twice → `login failed`; a 5.2-style login (`204`, a bad
  password `401`)
- a real-shaped ENOSPC torrent (`progress > 1`, negative `amount_left`) lists as `stuck`
- the cache hit and invalidation after a scan; viewers queued behind a build slower than the TTL all
  get its one result; the bounded wait (last payload, or `503`); a scan press never waits on a build;
  a backward wall-clock step doesn't freeze the cache
- the field allow-list: no hash, path, or URL appears anywhere in the JSON
- `401` when logged out; `{"enabled": false}` when unset
- the Jellyfin timestamp parser

**Browser:** python-playwright against the app with fake qBittorrent and Jellyfin HTTP servers.
Screenshots at 390×844 and 1440×900 in these states: downloading plus ready; ready during the
cooldown; one source down; Jellyfin unknown; feature disabled. The owner reviews them before commit.

## Out of scope

- Any control of torrents (pause, resume, delete, recheck).
- Radarr/Sonarr import tracking. There is no import step in this setup.
- Per-user visibility rules.

## Delivery

This ships together with the fixes from the 2026-09-26 audit, as **one commit** after the full
verification: pytest, the browser checks, and the owner's screenshot review.
