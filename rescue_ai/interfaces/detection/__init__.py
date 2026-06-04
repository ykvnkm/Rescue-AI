"""rescue-ai-detection FastAPI service (ADR-0008 §1)."""

from rescue_ai.interfaces.detection.app import build_app

__all__ = ["build_app"]
