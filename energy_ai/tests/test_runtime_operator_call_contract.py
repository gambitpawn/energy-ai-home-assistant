from __future__ import annotations

import ast
from pathlib import Path

from app.release_version import RELEASE_VERSION

ROOT = Path(__file__).resolve().parents[1]


def _function_keywords(path: Path, function_name: str) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name:
            return {arg.arg for arg in node.args.kwonlyargs}
    raise AssertionError(f"function {function_name!r} not found in {path}")


def _call_keywords(path: Path, function_name: str) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        if isinstance(target, ast.Name) and target.id == function_name:
            return {kw.arg for kw in node.keywords if kw.arg is not None}
    raise AssertionError(f"call to {function_name!r} not found in {path}")


def test_runtime_operator_persistent_mode_call_matches_function_signature():
    implementation = ROOT / "app" / "persistent_operating_mode.py"
    caller = ROOT / "app" / "runtime_operator.py"
    accepted = _function_keywords(implementation, "install_persistent_operating_mode")
    supplied = _call_keywords(caller, "install_persistent_operating_mode")
    assert supplied <= accepted
    assert supplied == {"app", "actuator", "ha", "startup_state"}


def test_runtime_and_addon_versions_match_without_hardcoding_release():
    runtime = (ROOT / "app" / "runtime_operator.py").read_text(encoding="utf-8")
    config = (ROOT / "config.yaml").read_text(encoding="utf-8")
    version_line = next(line for line in config.splitlines() if line.startswith("version: "))
    version = version_line.split(":", 1)[1].strip().strip('"')

    assert RELEASE_VERSION == version
    assert "from .release_version import RELEASE_VERSION" in runtime
    assert "RELEASE_BUILD = RELEASE_VERSION" in runtime
    assert 'base.core.cfg["runtime_build"] = RELEASE_BUILD' in runtime
    assert 'RELEASE_BUILD = "' not in runtime
