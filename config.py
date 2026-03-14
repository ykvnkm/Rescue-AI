from __future__ import annotations

import os

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

if load_dotenv is not None:
    load_dotenv()


class Config:
    """Single interface for reading environment variables across services."""

    def get(self, name: str, default: str | None = None) -> str | None:
        return os.getenv(name, default)

    def get_non_empty(self, *names: str, default: str | None = None) -> str:
        for name in names:
            value = os.getenv(name)
            if value:
                return value
        return "" if default is None else default

    def get_bool(self, name: str, default: bool) -> bool:
        raw = os.getenv(name)
        if raw is None:
            return default
        return raw.strip().lower() in {"1", "true", "yes", "on"}

    def get_float(self, name: str, default: float) -> float:
        raw = os.getenv(name)
        if raw is None:
            return default
        return float(raw)

    def service_version(self) -> str:
        return self.get_non_empty("SERVICE_VERSION", default="dev")

    def detection_http_timeout_sec(self) -> float:
        return self.get_float("DETECTION_HTTP_TIMEOUT_SEC", default=1.0)


config = Config()
