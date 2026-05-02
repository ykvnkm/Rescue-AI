"""rescue-ai-nav-engine FastAPI service (ADR-0008 §1)."""

from rescue_ai.services.nav_engine.app import build_app

__all__ = ["build_app"]
