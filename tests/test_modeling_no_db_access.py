"""Ensure modeling train modules do not import PostgreSQL / dataset builder (stage 4 base)."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path


FORBIDDEN_IMPORT_ROOTS = frozenset({"psycopg2", "modeling.dataset_builder"})


def forbidden_imports_in_module(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_forbidden(alias.name):
                    hits.append(f"{path}:{node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom) and node.module:
            if _is_forbidden(node.module):
                hits.append(f"{path}:{node.lineno}: from {node.module} import ...")
    return hits


def _is_forbidden(module: str) -> bool:
    return any(
        module == root or module.startswith(f"{root}.")
        for root in FORBIDDEN_IMPORT_ROOTS
    )


class TestNoDbAccessInModelingModules(unittest.TestCase):
    def test_modeling_modules_have_no_db_imports(self) -> None:
        root = Path(__file__).resolve().parent.parent / "modeling"
        for rel in ("splits.py", "metrics.py", "report.py", "bootstrap.py"):
            hits = forbidden_imports_in_module(root / rel)
            self.assertEqual(hits, [], msg=f"forbidden imports in modeling/{rel}: {hits}")


if __name__ == "__main__":
    unittest.main()
