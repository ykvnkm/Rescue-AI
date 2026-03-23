"""Pytest configuration helpers."""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# The default test suite exercises the in-process API wiring and should not
# inherit a developer's local postgres backend from .env.
os.environ["APP_REPOSITORY_BACKEND"] = "memory"
