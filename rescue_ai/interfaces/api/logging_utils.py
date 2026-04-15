"""Logging helpers for API route handlers."""

from __future__ import annotations

import re
from hashlib import sha256
from pathlib import PurePosixPath
from urllib.parse import urlsplit

_URI_RE = re.compile(r"\b[a-z][a-z0-9+.\-]*://[^\s\"')]+", re.IGNORECASE)
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def sanitize_log_text(value: object) -> str:
    text = str(value)
    text = _URI_RE.sub("<redacted-url>", text)
    return _IPV4_RE.sub("<redacted-ip>", text)


def build_source_log_fields(image_uri: str) -> tuple[str, str, str]:
    """Build safe diagnostic source fields for logs without full URI leakage."""
    uri = image_uri.strip()
    source_id_hash = sha256(uri.encode("utf-8")).hexdigest()[:12] if uri else "empty"
    parsed = urlsplit(uri)
    source_scheme = parsed.scheme.lower() if parsed.scheme else "file"
    path_raw = parsed.path if parsed.scheme else uri
    source_path_tail = _path_tail(path_raw)
    return source_scheme, source_path_tail, source_id_hash


def _path_tail(path: str, keep_parts: int = 3) -> str:
    normalized = path.strip().strip("/")
    if not normalized:
        return "-"
    parts = [part for part in PurePosixPath(normalized).parts if part not in {"/", "."}]
    if not parts:
        return "-"
    if len(parts) <= keep_parts:
        return "/".join(parts)
    return "/".join(parts[-keep_parts:])
