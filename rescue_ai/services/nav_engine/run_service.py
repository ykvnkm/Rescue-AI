"""Entry point for rescue-ai-nav-engine pod.

Запускается как:

    python -m rescue_ai.services.nav_engine.run_service

Порт берётся из ``NAV_ENGINE_PORT`` (default 8001), хост — из
``NAV_ENGINE_HOST`` (default 0.0.0.0). Тот же образ, что у API:
просто другой entry point.
"""

from __future__ import annotations

import logging
import os

import uvicorn

from rescue_ai.services.nav_engine.app import build_app


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("APP_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    host = os.environ.get("NAV_ENGINE_HOST", "0.0.0.0")
    port = int(os.environ.get("NAV_ENGINE_PORT", "8001"))
    uvicorn.run(build_app(), host=host, port=port)


if __name__ == "__main__":  # pragma: no cover
    main()
