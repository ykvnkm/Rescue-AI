"""Simple local UI for pilot API operations."""

from pathlib import Path

_TEMPLATE_PATH = Path(__file__).with_name("templates") / "pilot_ui.html"


def build_ui_html() -> str:
    return _TEMPLATE_PATH.read_text(encoding="utf-8")
