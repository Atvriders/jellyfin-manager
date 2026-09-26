/* Incoming panel: what qBittorrent is downloading, and what finished but is
   not in Jellyfin yet (so people know pressing Scan will add it).

   SECURITY: torrent names come from whoever made the torrent. Every value from
   /api/downloads is inserted with createElement + textContent ONLY. Never
   innerHTML.

   Coupling to the scan console is by DOM events only (dispatched by the inline
   script in index.html):
     scan:cooldown  detail.until = epoch ms the cooldown ends (0 = no cooldown)
     scan:started   a scan was just kicked off: refresh soon
     scan:activity  detail.running = whether the console sees a scan under way.
                    It looks every 2 s while it follows one; this panel's own
                    answer can be 10 s (server cache) + 15 s (poll) old, so
                    "adding now" follows the console, not the payload. */
(function () {
  'use strict';

  const root = document.getElementById('incoming');
  if (!root) return;                        // feature off: the template rendered nothing

  const POLL_MS = 15000;
  const TIMEOUT_MS = 20000;                 // the server may wait 5 s on qBittorrent, then on Jellyfin
  const VISIBLE_ROWS = 8;
  const SETTLE_MS = 11500;                  // the server reuses one answer for 10 s (CACHE_TTL)
  const NBSP = '\u00a0';

  const $ = (id) => document.getElementById(id);
  const sourcesEl    = $('incoming-sources');
  const countEl      = $('incoming-count');
  const dlList       = $('incoming-downloading');
  const dlEmpty      = $('incoming-downloading-empty');
  const dlUnknown    = $('incoming-downloading-unknown');
  const moreEl       = $('incoming-more');
  const readyList    = $('incoming-ready');
  const readyEmpty   = $('incoming-ready-empty');
  const readyUnknown = $('incoming-ready-unknown');
  const errorEl      = $('incoming-error');
  const loadingEl    = $('incoming-loading');
  const nudgeEl      = $('incoming-nudge');
  const announceEl   = $('incoming-announce');

  let pollTimer = null;
  let nudgeTimer = null;
  let inflight = false;
  let again = false;                        // a refresh was requested mid-flight
  let cooldownUntil = 0;
  let readyCount = 0;
  let addingCount = 0;
  let scanRunning = false;
  let lastAnnounced = null;
  let lastData = null;                      // the last good answer, redrawn when the console reports
  let consoleRunning = false;
  let settleUntil = 0;                      // a scan just ended: until then no answer can say what it added
  let settleTimer = null;

  const STATE_WORDS = {
    metadata: 'fetching metadata',
    checking: 'checking',
    stalled:  'stalled',
    queued:   'queued',
    paused:   'paused',
    error:    'error',
    stuck:    'stuck — recheck in qBittorrent',
  };

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function num(value) {
    const n = Number(value);
    return Number.isFinite(n) ? n : 0;
  }

  function plural(n, word) {
    return n + ' ' + word + (n === 1 ? '' : 's');
  }

  function fmtBytes(bytes) {
    let n = Math.max(0, num(bytes));
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let i = 0;
    while (n >= 1000 && i < units.length - 1) { n /= 1000; i++; }
    return (i === 0 ? String(Math.round(n)) : n.toFixed(n < 10 ? 1 : 0)) + ' ' + units[i];
  }

  function fmtEta(seconds) {
    const s = num(seconds);
    if (s <= 0) return '';
    if (s < 60) return '<1 min left';
    const min = Math.round(s / 60);
    if (min < 60) return min + ' min left';
    const h = Math.floor(min / 60);
    if (h < 48) return h + ' h ' + (min % 60) + ' min left';
    return Math.round(h / 24) + ' days left';
  }

  function fmtAgo(epochSeconds) {
    const sec = Math.round(Date.now() / 1000 - num(epochSeconds));
    if (sec < 60) return 'finished just now';
    const min = Math.round(sec / 60);
    if (min < 60) return 'finished ' + min + ' min ago';
    const h = Math.round(min / 60);
    if (h < 48) return 'finished ' + h + ' h ago';
    return 'finished ' + Math.round(h / 24) + ' days ago';
  }

  function fmtClock(ms) {
    const total = Math.max(0, Math.ceil(ms / 1000));
    const m = Math.floor(total / 60);
    const s = total % 60;
    return String(m).padStart(2, '0') + ':' + String(s).padStart(2, '0');
  }

  function pctText(progress) {
    // An unfinished torrent never reads "100.0%" (0.9996 would round up).
    const pct = Math.min(99.9, Math.max(0, num(progress) * 100));
    return pct.toFixed(1) + '%';
  }

  function nameEl(className, name) {
    const span = el('span', className, name);
    span.title = String(name ?? '');        // property assignment, not markup
    return span;
  }

  function downloadRow(d) {
    const state = typeof d.state === 'string' ? d.state : 'downloading';
    let cls = 'dl-row';
    if (state === 'paused') cls += ' is-paused';
    if (state === 'stuck' || state === 'error') cls += ' is-bad';
    const row = el('div', cls);

    const head = el('div', 'dl-head');
    head.appendChild(nameEl('dl-name', d.name));
    head.appendChild(el('span', 'src-tag', d.source));
    row.appendChild(head);

    const track = el('div', 'dl-track');
    track.setAttribute('aria-hidden', 'true');   // the percent is in the text below
    const fill = el('div', 'dl-fill');
    fill.style.width = (Math.min(1, Math.max(0, num(d.progress))) * 100).toFixed(1) + '%';
    track.appendChild(fill);
    row.appendChild(track);

    const parts = [pctText(d.progress)];
    if (state === 'downloading' && num(d.dlspeed) > 0) parts.push(fmtBytes(d.dlspeed) + '/s');
    const eta = state === 'downloading' ? fmtEta(d.eta) : '';
    if (eta) parts.push(eta);
    const meta = el('div', 'dl-meta', parts.join(' · '));
    if (STATE_WORDS[state]) {
      meta.appendChild(el('span', 'dl-state', STATE_WORDS[state]));
    }
    row.appendChild(meta);
    return row;
  }

  function readyRow(r, adding) {
    const row = el('div', 'ready-row' + (adding ? ' is-adding' : ''));
    const head = el('div', 'dl-head');
    head.appendChild(nameEl('dl-name', r.name));
    head.appendChild(el('span', 'src-tag', r.source));
    row.appendChild(head);
    row.appendChild(el('div', 'dl-meta', adding ? 'adding now…' : fmtAgo(r.completed_at)));
    return row;
  }

  function render(d) {
    errorEl.hidden = true;
    loadingEl.hidden = true;
    lastData = d;
    draw();
  }

  function draw() {
    const d = lastData;
    if (!d) return;
    const sources = Array.isArray(d.sources) ? d.sources : [];
    const bad = sources.filter((s) => s && !s.ok);
    const okLabels = sources.filter((s) => s && s.ok).map((s) => String(s.label));
    const anyOk = okLabels.length > 0;
    clear(sourcesEl);
    for (const s of bad) {
      sourcesEl.appendChild(el('div', 'src-problem', String(s.label) + ': ' + String(s.error || 'unavailable')));
    }
    sourcesEl.hidden = bad.length === 0;

    // Downloading. With no reachable box, "nothing downloading" would be a lie;
    // with some boxes down it speaks only for the ones that answered.
    const rows = Array.isArray(d.downloading) ? d.downloading : [];
    const total = Math.max(rows.length, num(d.downloading_total));
    clear(dlList);
    for (const r of rows.slice(0, VISIBLE_ROWS)) {
      if (r && typeof r === 'object') dlList.appendChild(downloadRow(r));
    }
    const more = total - Math.min(rows.length, VISIBLE_ROWS);
    moreEl.textContent = more > 0 ? '+' + more + ' more' : '';
    moreEl.hidden = more <= 0;
    dlEmpty.textContent = bad.length
      ? 'Nothing downloading on ' + okLabels.join(', ') + '.'
      : 'Nothing downloading right now.';
    dlEmpty.hidden = total > 0 || !anyOk;
    dlUnknown.hidden = anyOk;

    // Ready to add. "unknown" is shown as unknown, never as "nothing waiting".
    const unknown = d.ready_status !== 'ok';
    const ready = unknown || !Array.isArray(d.ready) ? [] : d.ready.filter((r) => r && typeof r === 'object');
    // A scan the console just saw end: an answer that may predate the end
    // can't say what that scan added, so until the server's cache has turned
    // over the rows claim neither "adding now" nor "scan to add them".
    const settling = settleUntil > Date.now();
    const running = consoleRunning || (!settling && !!(d.scan && d.scan.running));
    clear(readyList);
    for (const r of ready) readyList.appendChild(readyRow(r, running));
    readyUnknown.textContent = unknown ? 'Can’t check Jellyfin right now.' : 'Can’t tell until qBittorrent answers.';
    readyUnknown.hidden = !(unknown || !anyOk);
    readyEmpty.textContent = bad.length
      ? 'Nothing from ' + okLabels.join(', ') + ' waiting for a scan.'
      : 'Nothing waiting for a scan.';
    readyEmpty.hidden = unknown || ready.length > 0 || !anyOk;

    readyCount = running || settling ? 0 : ready.length;
    addingCount = running ? ready.length : 0;
    scanRunning = running;

    // Non-breaking inside each part: on a narrow panel it wraps between them.
    const counts = [];
    if (total) counts.push(total + NBSP + 'downloading');
    if (ready.length) counts.push(ready.length + NBSP + 'ready');
    countEl.textContent = counts.join(NBSP + '· ');

    renderNudge();
  }

  function renderNudge() {
    let text = '';
    if (addingCount > 0 && scanRunning) {
      text = 'Adding ' + plural(addingCount, 'download') + ' now…';
    } else if (readyCount > 0) {
      text = plural(readyCount, 'finished download') + ' '
        + (readyCount === 1 ? "isn't" : "aren't") + ' in Jellyfin yet — scan to add '
        + (readyCount === 1 ? 'it' : 'them') + '.';
      const left = cooldownUntil - Date.now();
      if (left > 0) text += ' Scan available in ' + fmtClock(left) + '.';
    }
    nudgeEl.textContent = text;
    nudgeEl.hidden = !text;

    // Keep the countdown in the nudge ticking only while it is on screen.
    const ticking = readyCount > 0 && cooldownUntil > Date.now();
    if (ticking && !nudgeTimer) nudgeTimer = setInterval(renderNudge, 1000);
    if (!ticking && nudgeTimer) { clearInterval(nudgeTimer); nudgeTimer = null; }

    // Screen readers hear it once per change of count, not every second.
    if (readyCount !== lastAnnounced) {
      lastAnnounced = readyCount;
      announceEl.textContent = readyCount > 0
        ? plural(readyCount, 'finished download') + ' ready to add to Jellyfin.'
        : '';
    }
  }

  async function refresh() {
    if (inflight) { again = true; return; }
    inflight = true;
    // Requests never overlap, so one that hangs must not freeze the panel.
    const ctrl = new AbortController();
    const timeout = setTimeout(() => ctrl.abort(), TIMEOUT_MS);
    try {
      const r = await fetch('/api/downloads', {
        headers: { Accept: 'application/json' }, cache: 'no-store', signal: ctrl.signal,
      });
      if (r.status === 401) { window.location.assign('/login'); return; }
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const d = await r.json();
      if (!d || d.enabled === false) { root.hidden = true; nudgeEl.hidden = true; stop(); return; }
      render(d);
    } catch (_) {
      loadingEl.hidden = true;
      errorEl.hidden = false;               // keep the last good render, just flag it
    } finally {
      clearTimeout(timeout);
      inflight = false;
      if (again) { again = false; refresh(); }
    }
  }

  function stop() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }

  function start() {
    stop();
    if (!document.hidden) pollTimer = setInterval(refresh, POLL_MS);
  }

  document.addEventListener('visibilitychange', () => {
    if (document.hidden) { stop(); return; }
    refresh();
    start();
  });

  document.addEventListener('scan:cooldown', (e) => {
    cooldownUntil = num(e.detail && e.detail.until);
    renderNudge();
  });

  document.addEventListener('scan:started', () => {
    // The press emptied the server's cache. Look once Jellyfin has had a
    // moment to flip the task to Running (that answer is then shared with
    // every viewer for 10 s); scan:activity has already marked the rows.
    setTimeout(refresh, 3000);
  });

  document.addEventListener('scan:activity', (e) => {
    const running = !!(e.detail && e.detail.running);
    if (running === consoleRunning) return;
    consoleRunning = running;
    clearTimeout(settleTimer);
    settleTimer = null;
    settleUntil = 0;
    if (!running) {
      settleUntil = Date.now() + SETTLE_MS;
      settleTimer = setTimeout(() => {
        settleTimer = null;
        if (!document.hidden) refresh();    // coming back refreshes anyway
      }, SETTLE_MS);
    }
    draw();
  });

  refresh();
  start();
})();
