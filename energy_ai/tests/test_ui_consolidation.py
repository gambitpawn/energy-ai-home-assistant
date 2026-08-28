from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"


def test_no_versioned_ui_modules_remain():
    assert list(APP.glob("ui_v*.py")) == []


def test_runtime_has_no_versioned_ui_dependency():
    text = (APP / "runtime.py").read_text(encoding="utf-8")
    assert "ui_v" not in text
    assert "model_compare_v" not in text


def test_runtime_ui_owns_one_current_ui_route_and_no_ui_middleware():
    path = APP / "runtime_ui.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    ui_routes = 0
    middleware = 0
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for deco in node.decorator_list:
            if not isinstance(deco, ast.Call) or not isinstance(deco.func, ast.Attribute):
                continue
            if deco.func.attr == "middleware":
                middleware += 1
            if deco.func.attr == "get" and deco.args:
                arg = deco.args[0]
                if isinstance(arg, ast.Constant) and arg.value == "/ui":
                    ui_routes += 1
    assert middleware == 0
    assert ui_routes == 1


def test_semantic_ui_modules_do_not_register_http_middleware():
    for name in ("ui_evaluation.py", "ui_live.py", "ui_models.py", "ui_parameters.py", "ui_charts.py"):
        text = (APP / name).read_text(encoding="utf-8")
        assert '@app.middleware("http")' not in text
