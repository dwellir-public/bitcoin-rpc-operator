import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]


@pytest.mark.parametrize("relative_path", ["src/charm.py", "src/interface_prometheus.py"])
def test_handlers_start_with_debug_log(relative_path):
    """Every hook and action handler must log before any other executable statement."""
    tree = ast.parse((ROOT / relative_path).read_text(encoding="utf-8"))
    failures = []

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or not node.name.startswith("_on_"):
            continue
        body = (
            node.body[1:]
            if node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
            else node.body
        )
        first = body[0] if body else None
        valid = (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Call)
            and isinstance(first.value.func, ast.Attribute)
            and isinstance(first.value.func.value, ast.Name)
            and first.value.func.value.id == "logger"
            and first.value.func.attr == "debug"
        )
        if not valid:
            failures.append(f"{node.name}:{node.lineno}")

    assert not failures, f"handlers without a first debug log in {relative_path}: {', '.join(failures)}"
