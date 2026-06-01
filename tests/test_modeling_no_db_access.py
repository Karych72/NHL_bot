"""Ensure modeling train modules do not import PostgreSQL / dataset builder (stage 11)."""

from __future__ import annotations

import ast
import tempfile
import unittest
from pathlib import Path


FORBIDDEN_IMPORT_ROOTS = frozenset({"psycopg2", "modeling.dataset_builder"})
MODELING_ROOT = Path(__file__).resolve().parent.parent / "modeling"


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


def collect_no_db_access_targets(root: Path = MODELING_ROOT) -> list[Path]:
    """Files covered by UPDATE plan §11 AST guard (glob for new train_*.py)."""
    paths: set[Path] = set()
    for pattern in ("train_*.py",):
        paths.update(root.glob(pattern))
    for name in ("acceptance.py", "calibrate.py", "splits.py", "metrics.py"):
        paths.add(root / name)
    return sorted(paths)


class TestNoDbAccessInModelingModules(unittest.TestCase):
    def test_modeling_modules_have_no_db_imports(self) -> None:
        targets = collect_no_db_access_targets()
        self.assertGreater(len(targets), 0, msg="expected at least one modeling module")
        for path in targets:
            hits = forbidden_imports_in_module(path)
            self.assertEqual(hits, [], msg=f"forbidden imports in {path.relative_to(MODELING_ROOT.parent)}: {hits}")

    def test_detector_catches_psycopg2_import(self) -> None:
        snippet = "import psycopg2\nfrom modeling.dataset_builder import build\n"
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(snippet)
            path = Path(handle.name)
        try:
            hits = forbidden_imports_in_module(path)
            self.assertEqual(len(hits), 2)
        finally:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
