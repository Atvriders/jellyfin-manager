"""Regression tests for the dive-log template.

The history "user" value is attacker-influenced in width (a Jellyfin account
name, clipped server-side at 64 chars). The .history-user span must be able
to shrink and ellipsize like .history-ua, or long names overflow the glass
panel on narrow viewports.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

TEMPLATE = Path(__file__).resolve().parents[1] / "templates" / "index.html"


def _css_block(selector):
    text = TEMPLATE.read_text(encoding="utf-8")
    match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", text)
    assert match, f"CSS block {selector} not found in template"
    return match.group(1)


def test_history_user_can_shrink_and_ellipsize():
    block = _css_block(".history-user")
    assert "overflow: hidden" in block
    assert "text-overflow: ellipsis" in block
    assert "min-width: 0" in block


def test_history_user_is_not_flex_none():
    # flex: none made the span unshrinkable, so a 64-char username pushed
    # the whole meta line past the panel border on phones.
    block = _css_block(".history-user")
    assert "flex: none" not in block


def test_history_user_has_a_max_width_cap():
    # A cap keeps "ip · browser" visible even against a full-width name.
    block = _css_block(".history-user")
    assert "max-width" in block


def test_user_span_gets_title_via_property_assignment():
    # The truncated name stays recoverable on hover/long-press. Must be a
    # property assignment (like the ua span) -- innerHTML is forbidden.
    text = TEMPLATE.read_text(encoding="utf-8")
    assert re.search(r"userEl\.title\s*=", text)


def test_history_renderer_never_uses_innerhtml():
    text = TEMPLATE.read_text(encoding="utf-8")
    # A comment may say "never innerHTML"; forbid actual assignments/reads.
    assert not re.search(r"\.\s*innerHTML", text)


# --- Incoming panel ---------------------------------------------------------

INCOMING_JS = Path(__file__).resolve().parents[1] / "static" / "incoming.js"
LOGIN = Path(__file__).resolve().parents[1] / "templates" / "login.html"


def test_incoming_js_never_uses_innerhtml():
    # Torrent names are attacker-chosen; rows must be built with textContent.
    assert not re.search(r"\.\s*innerHTML", INCOMING_JS.read_text(encoding="utf-8"))


def test_hidden_attribute_beats_class_display_rules():
    text = TEMPLATE.read_text(encoding="utf-8")
    assert re.search(r"\[hidden\]\s*\{\s*display:\s*none\s*!important;?\s*\}", text)


def test_scan_console_announces_cooldown_and_start_events():
    text = TEMPLATE.read_text(encoding="utf-8")
    assert text.count("new CustomEvent('scan:cooldown'") == 2      # start/re-sync + end of cooldown
    assert "new CustomEvent('scan:started')" in text
    # Whether the console sees a scan under way: the panel's own answer can
    # be 25 s old, so its "adding now" follows the console, not the payload.
    assert text.count("new CustomEvent('scan:activity'") == 1
    js = INCOMING_JS.read_text(encoding="utf-8")
    assert "addEventListener('scan:cooldown'" in js
    assert "addEventListener('scan:started'" in js
    assert "addEventListener('scan:activity'" in js


def test_incoming_js_loads_before_the_inline_script():
    # It must be listening before the inline script dispatches scan:cooldown.
    text = TEMPLATE.read_text(encoding="utf-8")
    assert text.index("filename='incoming.js'") < text.index("new CustomEvent('scan:cooldown'")


def test_incoming_fetch_has_a_timeout():
    # Requests never overlap, so a hung one would freeze the panel for good.
    js = INCOMING_JS.read_text(encoding="utf-8")
    assert "AbortController" in js and "signal:" in js


# --- Page script: one fetch helper, focus-safe button, events ---------------

def _inline_script():
    text = TEMPLATE.read_text(encoding="utf-8")
    scripts = re.findall(r"<script>(.*?)</script>", text, re.S)
    assert scripts, "inline script not found"
    return max(scripts, key=len)


def test_every_page_call_goes_through_one_fetch_helper():
    js = _inline_script()
    # Exactly one raw fetch(...): inside api(), which handles 401, non-JSON
    # bodies and the timeout for every caller.
    assert len(re.findall(r"\bfetch\(", js)) == 1
    assert "async function api(" in js
    assert "r.status === 401" in js and "location.assign('/login')" in js
    assert "AbortController" in js
    assert "Couldn't reach Jellyfin Manager" in js


def test_scan_button_uses_aria_disabled_not_the_disabled_property():
    # btn.disabled = true drops keyboard focus to <body> on press.
    text = TEMPLATE.read_text(encoding="utf-8")
    js = _inline_script()
    assert not re.search(r"btn\.disabled\s*=", js)
    assert "getAttribute('aria-disabled') === 'true'" in js       # the click guard
    assert re.search(r'#scan-btn\[aria-disabled="true"\](:not\(\[data-booting\]\))?\s*\{', text)
    assert re.search(r'<button id="scan-btn"[^>]*aria-describedby="[^"]*scan-when', text)


def test_server_errors_are_shown_as_sent():
    # No "Error: " prefix or other rewriting of the server's short reason.
    js = _inline_script()
    assert "'Error: '" not in js


def test_progress_poll_is_chained_not_an_interval():
    js = _inline_script()
    assert "setInterval(async" not in js
    assert "Scan progress unavailable" in js


# --- Dive log layout ---------------------------------------------------------

def test_history_meta_has_its_own_grid_row_at_every_width():
    # Outside any media query: a 39-char IPv6 address can't squeeze the name out.
    block = _css_block(".history-meta")
    assert "grid-column: 1 / -1" in block


def test_history_ip_can_ellipsize():
    block = _css_block(":where(.history-meta-line) > span")
    for rule in ("min-width: 0", "overflow: hidden", "text-overflow: ellipsis", "max-width: 100%"):
        assert rule in block


def test_busy_outcome_has_its_own_badge():
    assert _css_block(".badge.busy")
    assert "busy: 'busy'" in _inline_script()


# --- Surface: who is signed in, and sign-out --------------------------------

def test_sign_out_is_a_post_form():
    text = TEMPLATE.read_text(encoding="utf-8")
    assert re.search(r'<form[^>]*method="post"[^>]*action="/logout"', text)
    assert 'href="/logout"' not in text


def test_user_name_is_never_marked_safe():
    for path in (TEMPLATE, LOGIN):
        assert "|safe" not in path.read_text(encoding="utf-8").replace(" ", "")


# --- Contrast outside the glass ----------------------------------------------

def test_labels_on_bare_water_get_a_halo():
    assert "text-shadow: var(--halo)" in _css_block(".tick-label")
    assert "text-shadow: var(--halo)" in _css_block(".brand-text .eyebrow")
    login = LOGIN.read_text(encoding="utf-8")
    assert re.search(r"\.brand-eyebrow\s*\{[^}]*text-shadow:\s*var\(--halo\)", login)


# --- Login page --------------------------------------------------------------

def test_login_never_uses_innerhtml():
    assert not re.search(r"\.\s*innerHTML", LOGIN.read_text(encoding="utf-8"))


def test_login_error_is_announced_and_described():
    login = LOGIN.read_text(encoding="utf-8")
    assert 'id="login-error" role="alert"' in login
    assert login.count('aria-invalid="true" aria-describedby="login-error"') == 2


def test_login_keeps_the_username_but_never_the_password():
    login = LOGIN.read_text(encoding="utf-8")
    assert "value=\"{{ username or '' }}\"" in login
    password_input = re.search(r'<input type="password"[^>]*>', login, re.S).group(0)
    assert "value=" not in password_input


def test_lockout_countdown_uses_a_deadline():
    login = LOGIN.read_text(encoding="utf-8")
    assert "remaining--" not in login
    assert "const deadline = Date.now() +" in login
    assert "visibilitychange" in login


# --- How a scan ended, unsaved history, notes on rows ------------------------

def test_history_note_is_neutral_and_errors_stay_coral():
    # A note on a "started" row ("Jellyfin was slow to answer…") is not a
    # failure; painting it coral would cry wolf next to real errors.
    text = TEMPLATE.read_text(encoding="utf-8")
    # Its own rule (it may share the layout rule with .history-err), later
    # than the shared one so its colour wins.
    note = re.search(r"(?m)^\s*\.history-note\s*\{([^}]*)\}", text)
    assert note and "color: var(--benthos)" in note.group(1) and "coral" not in note.group(1)
    assert note.start() > text.index(".history-err")
    assert re.search(r"\.history-err\s*[,{][^}]*color:\s*var\(--coral\)", text)


def test_unsaved_history_warning_is_static_and_styled_like_the_history_error():
    text = TEMPLATE.read_text(encoding="utf-8")
    # Static markup (no server text), inside the dive log next to #history-error.
    warn = re.search(r'<div id="history-unsaved">([^<]*)</div>', text)
    assert warn and warn.group(1) == "History isn’t being saved — check the /data volume."
    assert text.index('id="history-unsaved"') < text.index('id="history-error"')
    for rule in (r"#history-empty,\s*#history-error,\s*#history-unsaved\s*\{",
                 r"#history-error\.show,\s*#history-unsaved\.show\s*\{",
                 r"#history-error,\s*#history-unsaved\s*\{\s*color:\s*var\(--coral\)"):
        assert re.search(rule, text), rule


def test_the_page_reads_writable_on_history_errors_too():
    # api() resolves non-OK JSON bodies; the 503 carries "writable" as well.
    js = _inline_script()
    assert re.search(r"\.writable\s*===\s*false", js)
    assert re.search(r"\.writable\s*===\s*true", js)


def test_only_a_completed_run_is_called_complete():
    js = _inline_script()
    assert "'Completed'" in js and "'Cancelled'" in js
    assert "'Scan cancelled.'" in js and "'Scan ended: '" in js


# --- Incoming panel layout ----------------------------------------------------

def test_paused_rows_are_not_dimmed_as_a_whole():
    # Row-wide opacity took the tag and percent text to 2.64:1 (WCAG 1.4.3
    # wants 4.5:1). Only the bar steps back.
    text = TEMPLATE.read_text(encoding="utf-8")
    assert not re.search(r"(?m)^\s*\.is-paused\s*\{[^}]*opacity", text)
    assert re.search(r"\.is-paused \.dl-track\s*\{[^}]*opacity", text)


def test_incoming_count_can_shrink_and_wrap():
    # "11 downloading · 2 ready" spilled past the panel at 320 px.
    text = TEMPLATE.read_text(encoding="utf-8")
    rule = re.search(r"(?m)^\s*#incoming-count\s*\{([^}]*)\}", text)
    assert rule and "min-width: 0" in rule.group(1) and "flex: 0 1 auto" in rule.group(1)


# --- Every inline script parses ---------------------------------------------

def _inline_scripts(path):
    """(name, source) for each inline <script>, padded so node's line numbers
    are the template's. Jinja expressions become a number literal."""
    text = path.read_text(encoding="utf-8")
    for i, m in enumerate(re.finditer(r"<script>(.*?)</script>", text, re.S)):
        body = re.sub(r"\{\{.*?\}\}", "0", m.group(1))
        assert "{%" not in body, f"Jinja statement inside {path.name} script {i}"
        yield f"{path.name}.{i}.js", "\n" * text.count("\n", 0, m.start(1)) + body


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
@pytest.mark.parametrize("path", [TEMPLATE, LOGIN], ids=lambda p: p.name)
def test_inline_scripts_are_valid_javascript(path, tmp_path):
    # The page's whole state machine is inline: a syntax error there leaves a
    # dead button, and the browser tests that would notice are skipped in CI.
    scripts = list(_inline_scripts(path))
    assert scripts
    for name, source in scripts:
        target = tmp_path / name
        target.write_text(source, encoding="utf-8")
        run = subprocess.run(["node", "--check", str(target)], capture_output=True, text=True)
        assert run.returncode == 0, run.stderr
