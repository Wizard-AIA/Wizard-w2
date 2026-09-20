"""The application version: one value, read from the repository's ``VERSION`` file.

``VERSION`` at the repository root is the single source of truth for a release.
The release build stamps the CLI from it, the release workflow refuses a tag that
disagrees with it, and ``scripts/release.py check`` fails CI if the copies that
package managers require (``frontend/package.json``, ``CITATION.cff``) drift.
Release archives ship ``VERSION`` at their root, so an installed backend reports
the version it was released as.
"""

from __future__ import annotations

import os
from pathlib import Path


_UNKNOWN = "0.0.0+unknown"


def _read_version() -> str:
    # backend/src/version.py -> repository (or unpacked archive) root
    version_file = Path(__file__).resolve().parents[2] / "VERSION"
    try:
        value = version_file.read_text(encoding="utf-8").strip()
    except OSError:
        # A deployment that ships the backend without the repository root
        # (a bare container image) can still say what it is.
        value = os.environ.get("WIZARD_VERSION", "").strip().lstrip("v")
    return value or _UNKNOWN


APP_VERSION: str = _read_version()
