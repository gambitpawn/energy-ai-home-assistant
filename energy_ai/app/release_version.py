from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path

_VERSION_RE = re.compile(r'^version:\s*["\']?([^"\'\s#]+)', re.MULTILINE)
_ENV_KEY = "ENERGY_AI_VERSION"


@lru_cache(maxsize=1)
def release_version() -> str:
    """Return the canonical add-on release version.

    Production images receive BUILD_VERSION from the Home Assistant add-on
    builder and expose it as ENERGY_AI_VERSION in the Dockerfile. Source-tree
    execution and tests fall back to the add-on manifest, which is the release
    source of truth.
    """
    env_version = os.environ.get(_ENV_KEY, "").strip()
    if env_version:
        return env_version

    manifest = Path(__file__).resolve().parents[1] / "config.yaml"
    try:
        text = manifest.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(
            f"Cannot determine Energy AI release version: {_ENV_KEY} is unset "
            f"and {manifest} is unavailable"
        ) from exc

    match = _VERSION_RE.search(text)
    if not match:
        raise RuntimeError(f"Cannot determine Energy AI release version from {manifest}")
    return match.group(1)


RELEASE_VERSION = release_version()
