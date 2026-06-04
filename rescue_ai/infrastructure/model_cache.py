"""Local model-asset cache used before mission processing starts."""

from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlretrieve

MODEL_CACHE_DIR = Path("runtime/models")


class ModelCache:
    """Resolve model assets into a local cache and verify checksums."""

    def __init__(self, cache_dir: Path = MODEL_CACHE_DIR) -> None:
        self._cache_dir = cache_dir

    def resolve_file(self, source: str, sha256: str | None = None) -> Path:
        """Return a local file path for a model asset."""
        path = self._resolve_path(source)
        if path.exists() and path.is_file():
            _verify_sha256(path, sha256)
            return path

        self._cache_dir.mkdir(parents=True, exist_ok=True)
        target = self._cache_dir / _filename_from_source(source, fallback="model.pt")
        if not target.exists():
            urlretrieve(source, target)
        _verify_sha256(target, sha256)
        return target

    def resolve_directory(self, source: str, sha256: str | None = None) -> Path:
        """Return a local directory path for an exported runtime package.

        Local directories are used as-is. Remote sources are expected to be
        zip archives; the archive is cached, verified, and extracted once.
        """
        path = self._resolve_path(source)
        if path.exists() and path.is_dir():
            return path

        archive = self.resolve_file(source, sha256)
        target_dir = self._cache_dir / archive.stem
        if not target_dir.exists():
            target_dir.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(archive) as payload:
                payload.extractall(target_dir)
        return _single_child_dir(target_dir)

    def _resolve_path(self, source: str) -> Path:
        parsed = urlparse(source)
        if parsed.scheme in ("", "file"):
            return Path(parsed.path if parsed.scheme == "file" else source)
        return Path("__remote__")


def _filename_from_source(source: str, *, fallback: str) -> str:
    parsed = urlparse(source)
    return Path(parsed.path).name or fallback


def _single_child_dir(path: Path) -> Path:
    children = [item for item in path.iterdir() if item.is_dir()]
    if len(children) == 1:
        return children[0]
    return path


def _verify_sha256(path: Path, expected_sha256: str | None) -> None:
    if not expected_sha256:
        return
    normalized = expected_sha256.strip().lower()
    if len(normalized) != 64 or not all(ch in "0123456789abcdef" for ch in normalized):
        raise RuntimeError("Invalid model sha256 format in runtime config")
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != normalized:
        raise RuntimeError(
            f"Model checksum mismatch for {path.name}: "
            f"expected {normalized}, got {actual}"
        )
