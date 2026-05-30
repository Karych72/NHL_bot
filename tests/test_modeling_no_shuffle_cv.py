"""AST guard against shuffled CV / random match splits in modeling/ (stage 4 base)."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path


MODELING_ROOT = Path(__file__).resolve().parent.parent / "modeling"


class _ShuffleCVVisitor(ast.NodeVisitor):
    def __init__(self, path: Path) -> None:
        self.path = path
        self.violations: list[str] = []

    def visit_Call(self, node: ast.Call) -> None:
        func_name = _call_name(node.func)
        if func_name in {"KFold", "StratifiedKFold"}:
            shuffle = _keyword_bool(node, "shuffle")
            if shuffle is True:
                self.violations.append(
                    f"{self.path}:{node.lineno}: {func_name}(shuffle=True) is forbidden"
                )
        elif func_name == "ShuffleSplit":
            self.violations.append(f"{self.path}:{node.lineno}: ShuffleSplit is forbidden")
        elif func_name == "train_test_split":
            shuffle = _keyword_bool(node, "shuffle")
            if shuffle is not False:
                self.violations.append(
                    f"{self.path}:{node.lineno}: train_test_split requires shuffle=False"
                )
        self.generic_visit(node)


def _call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _keyword_bool(node: ast.Call, name: str) -> bool | None:
    for keyword in node.keywords:
        if keyword.arg != name:
            continue
        if isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, bool):
            return keyword.value.value
    return None


def find_shuffle_cv_violations(root: Path = MODELING_ROOT) -> list[str]:
    violations: list[str] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        visitor = _ShuffleCVVisitor(path)
        visitor.visit(tree)
        violations.extend(visitor.violations)
    return violations


class TestNoShuffleCV(unittest.TestCase):
    def test_modeling_tree_has_no_forbidden_splitters(self) -> None:
        violations = find_shuffle_cv_violations()
        self.assertEqual(violations, [])

    def test_detector_catches_kfold_shuffle_true(self) -> None:
        snippet = "from sklearn.model_selection import KFold\nKFold(shuffle=True)\n"
        tree = ast.parse(snippet)
        visitor = _ShuffleCVVisitor(Path("fake.py"))
        visitor.visit(tree)
        self.assertTrue(any("KFold" in item for item in visitor.violations))


if __name__ == "__main__":
    unittest.main()
