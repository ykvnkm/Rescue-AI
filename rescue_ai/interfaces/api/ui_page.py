"""Simple local UI for pilot API operations."""

from pathlib import Path

_TEMPLATE_PATH = Path(__file__).with_name("templates") / "pilot_ui.html"


def build_ui_html() -> str:
    """Render the operator UI, injecting the deployment profile.

    The frames source is profile-aware (cloud → re-run from S3, offline →
    local ZIP upload), so the template needs to know the mode. We inject it
    by replacing a placeholder token — no extra endpoint required.
    """
    from rescue_ai.config import get_settings  # noqa: PLC0415

    mode = str(getattr(get_settings().deployment, "mode", "cloud"))
    html = _TEMPLATE_PATH.read_text(encoding="utf-8")
    return html.replace("__DEPLOYMENT_MODE__", mode)
