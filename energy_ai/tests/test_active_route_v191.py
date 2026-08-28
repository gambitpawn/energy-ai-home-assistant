from __future__ import annotations

import ast
from pathlib import Path


RUNTIME_PATH = Path(__file__).resolve().parents[1] / "app" / "runtime_entry_v188.py"


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


def test_active_preflight_references_actual_v187_actuator_instance():
    tree = _tree()
    chains = {
        chain
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        for chain in [_attribute_chain(node)]
        if chain is not None
    }

    # Regression for v1.0.90 production failure. The actual actuator instance is
    # defined in runtime_entry_v187, imported as actuator_runtime. The final
    # wrapper module does not expose ACTUATOR and must never be dereferenced as
    # v187.ACTUATOR by the ACTIVE configuration gate.
    assert "actuator_runtime.ACTUATOR.preflight" in chains
    assert "v187.ACTUATOR.preflight" not in chains
    assert "v187.ACTUATOR" not in chains


def test_active_wrapper_still_delegates_to_hardened_transition():
    tree = _tree()
    function = next(
        node
        for node in tree.body
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
        and node.name == "production_control_mode_v188"
    )

    called_names = {
        node.func.id
        for node in ast.walk(function)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "_previous_control_endpoint" in called_names
