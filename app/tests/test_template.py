"""Regression tests for the dive-log template.

The history "user" value is attacker-influenced in width (a Jellyfin account
name, clipped server-side at 64 chars). The .history-user span must be able
to shrink and ellipsize like .history-ua, or long names overflow the glass
panel on narrow viewports.
"""

import re
from pathlib import Path

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
