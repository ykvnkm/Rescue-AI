"""Tests for UI page loader."""

from rescue_ai.interfaces.api.ui_page import build_ui_html


def test_build_ui_html_returns_template() -> None:
    html = build_ui_html()
    assert isinstance(html, str)
    assert "<html" in html.lower()
