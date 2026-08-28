from __future__ import annotations

import ast
from pathlib import Path


RUNTIME_PATH = Path(__file__).resolve().parents[1] / "app" / "runtime_routes.py"


def _tree() -> ast.Module:
    return ast.parse(RUNTIME_PATH.read_text(encoding="utf-8"))


def _attribute_chain(node: ast.AST) -> str | None:
    parts: list[str] = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        return ".".join(reversed(parts))
    return None


def _control_mode_function(tree: ast.Module):
    installer = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "install_runtime_routes"
    )
    return next(
        node for node in installer.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "control_mode"
    )


def test_active_preflight_references_injected_actual_actuator_instance():
    tree = _tree()
    function = _control_mode_function(tree)
    chains = {
        chain
        for node in ast.walk(function)
        if isinstance(node, ast.Attribute)
        for chain in [_attribute_chain(node)]
        if chain is not None
    }

    # v1.0.91 regression invariant, now expressed without historical runtime
    # aliases: ACTIVE preflight must use the actual actuator instance injected by
    # app.runtime into the consolidated route installer.
    assert "actuator.preflight" in chains
    assert not any(chain.startswith("v187.") or chain.startswith("actuator_runtime.") for chain in chains)


def test_consolidated_active_route_performs_hardened_transition_itself():
    tree = _tree()
    function = _control_mode_function(tree)
    chains = {
        chain
        for node in ast.walk(function)
        if isinstance(node, ast.Attribute)
        for chain in [_attribute_chain(node)]
        if chain is not None
    }
    referenced_names = {
        node.id
        for node in ast.walk(function)
        if isinstance(node, ast.Name)
    }

    assert "actuator.process_candidate" in chains
    assert "actuator.fail_safe" in chains
    assert "adapter.safe_release" in chains
    # set_mode is intentionally passed to asyncio.to_thread rather than called
    # directly on the event loop.
    assert "set_mode" in referenced_names
