from __future__ import annotations

import re
from pathlib import Path

from app import release_version as release_version_module

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "config.yaml"


def _manifest_version() -> str:
    text = MANIFEST.read_text(encoding="utf-8")
    match = re.search(r'^version:\s*["\']?([^"\'\s#]+)', text, re.MULTILINE)
    assert match is not None
    return match.group(1)


def test_release_version_matches_addon_manifest(monkeypatch):
    monkeypatch.delenv("ENERGY_AI_VERSION", raising=False)
    release_version_module.release_version.cache_clear()
    try:
        assert release_version_module.release_version() == _manifest_version()
        assert release_version_module.RELEASE_VERSION == _manifest_version()
    finally:
        release_version_module.release_version.cache_clear()


def test_production_build_version_takes_precedence(monkeypatch):
    monkeypatch.setenv("ENERGY_AI_VERSION", "9.8.7-test")
    release_version_module.release_version.cache_clear()
    try:
        assert release_version_module.release_version() == "9.8.7-test"
    finally:
        release_version_module.release_version.cache_clear()


def test_operator_runtime_and_ui_use_canonical_release():
    source = (ROOT / "app" / "runtime_operator.py").read_text(encoding="utf-8")
    assert "from .release_version import RELEASE_VERSION" in source
    assert "RELEASE_BUILD = RELEASE_VERSION" in source
    assert 'base.core.cfg["runtime_build"] = RELEASE_BUILD' in source
    assert 'RELEASE_BUILD = "' not in source


def test_docker_exposes_home_assistant_build_version():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert 'ENV ENERGY_AI_VERSION="${BUILD_VERSION}"' in dockerfile
