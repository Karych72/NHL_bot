"""Tests for modeling.config training configuration (stage 2)."""

from __future__ import annotations

import ast
import io
import json
import logging
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from datetime import datetime, timezone
from pathlib import Path

import yaml

from modeling.config import (
    ConfigError,
    ModelingConfig,
    ResolvedConfig,
    apply_overrides,
    build_run_id,
    configure_run_logger,
    derive_seed,
    load_config,
    parse_override,
    resolve_config,
    resolved_config_to_yaml,
)
from modeling.dataset_builder.schema import features_hash


def _minimal_manifest() -> list[dict[str, str]]:
    return [
        {"name": "f_a", "dtype": "float64", "position": "0"},
        {"name": "f_b", "dtype": "float64", "position": "1"},
    ]


def _minimal_metadata(*, manifest: list[dict[str, str]] | None = None) -> dict:
    manifest = manifest or _minimal_manifest()
    rolling = [5, 10]
    cold_predict = "allow_with_flag"
    fsv = "v1"
    fh = features_hash(
        feature_manifest=manifest,
        rolling_windows=rolling,
        cold_start_policy=f"train:drop|predict:{cold_predict}",
        feature_set_version=fsv,
    )
    return {
        "feature_set_version": fsv,
        "features_hash": fh,
        "feature_manifest": manifest,
        "rolling_windows": rolling,
        "cold_start_policy_predict": cold_predict,
    }


def _minimal_yaml_text(**overrides: object) -> str:
    base = textwrap.dedent(
        """
        random_seed: 7
        compute:
          num_threads: 2
          log_level: INFO
        tasks:
          home_win:
            enabled: true
          over_5_5:
            enabled: true
        split:
          method: month
          n_test_windows: 5
          inner_val_games: 300
          calibration_games: 300
          holdout:
            fraction: 0.15
            date_range:
              from: null
              to: null
        models:
          logreg:
            grids:
              C: [0.1, 1.0]
          lgbm:
            grids:
              num_leaves: [31]
              min_data_in_leaf: [20]
              feature_fraction: [0.9]
              bagging_fraction: [0.9]
              lambda_l1: [0.0]
              lambda_l2: [0.0]
              learning_rate: [0.05]
            monotone:
              home_win:
                f_a: 1
              over_5_5:
                f_b: 1
        calibration:
          method: isotonic
          min_samples: 500
        evaluation:
          ece_bins: 10
          bootstrap_samples: 1000
          bootstrap_block_by_day: true
          epsilon_clip: 1.0e-15
        """
    ).strip()
    data = yaml.safe_load(base)
    data.update(overrides)
    return yaml.safe_dump(data, sort_keys=False)


class TestModelingConfigHappyPath(unittest.TestCase):
    def test_minimal_yaml_parses_and_resolves(self) -> None:
        metadata = _minimal_metadata()
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            cfg_path = tmp / "cfg.yaml"
            cfg_path.write_text(_minimal_yaml_text(), encoding="utf-8")
            resolved = resolve_config(cfg_path, metadata)

            self.assertIsInstance(resolved, ResolvedConfig)
            self.assertEqual(resolved.random_seed, 7)
            self.assertEqual(resolved.compute.num_threads, 2)
            self.assertEqual(resolved.features_hash, metadata["features_hash"])
            self.assertEqual(resolved.feature_manifest, metadata["feature_manifest"])

    def test_default_yaml_valid_with_metadata(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        default_cfg = repo_root / "configs" / "modeling_default.yaml"
        metadata = _minimal_metadata()
        resolved = resolve_config(default_cfg, metadata)
        self.assertEqual(resolved.calibration.method.value, "isotonic")
        self.assertEqual(resolved.evaluation.bootstrap_samples, 1000)


class TestModelingConfigValidationErrors(unittest.TestCase):
    def _resolve_expect_error(self, yaml_text: str, metadata: dict | None = None) -> str:
        metadata = metadata or _minimal_metadata()
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            cfg_path = tmp / "cfg.yaml"
            cfg_path.write_text(yaml_text, encoding="utf-8")
            with self.assertRaises(ConfigError) as ctx:
                resolve_config(cfg_path, metadata)
            return str(ctx.exception)

    def test_missing_random_seed(self) -> None:
        text = _minimal_yaml_text()
        data = yaml.safe_load(text)
        del data["random_seed"]
        msg = self._resolve_expect_error(yaml.safe_dump(data, sort_keys=False))
        self.assertIn("random_seed", msg)

    def test_missing_split_n_test_windows(self) -> None:
        data = yaml.safe_load(_minimal_yaml_text())
        del data["split"]["n_test_windows"]
        msg = self._resolve_expect_error(yaml.safe_dump(data, sort_keys=False))
        self.assertIn("n_test_windows", msg)

    def test_missing_evaluation_epsilon_clip(self) -> None:
        data = yaml.safe_load(_minimal_yaml_text())
        del data["evaluation"]["epsilon_clip"]
        msg = self._resolve_expect_error(yaml.safe_dump(data, sort_keys=False))
        self.assertIn("epsilon_clip", msg)

    def test_extra_top_level_key_forbidden(self) -> None:
        msg = self._resolve_expect_error(_minimal_yaml_text(unknown_field=True))
        self.assertIn("unknown_field", msg)
        self.assertIn("extra", msg.lower())

    def test_invalid_random_seed_type(self) -> None:
        msg = self._resolve_expect_error(_minimal_yaml_text(random_seed="abc"))
        self.assertIn("random_seed", msg)

    def test_n_test_windows_below_minimum(self) -> None:
        data = yaml.safe_load(_minimal_yaml_text())
        data["split"]["n_test_windows"] = 4
        msg = self._resolve_expect_error(yaml.safe_dump(data, sort_keys=False))
        self.assertIn("n_test_windows", msg)

    def test_inner_val_games_below_minimum(self) -> None:
        data = yaml.safe_load(_minimal_yaml_text())
        data["split"]["inner_val_games"] = 299
        msg = self._resolve_expect_error(yaml.safe_dump(data, sort_keys=False))
        self.assertIn("inner_val_games", msg)

    def test_calibration_games_below_minimum(self) -> None:
        data = yaml.safe_load(_minimal_yaml_text())
        data["split"]["calibration_games"] = 100
        msg = self._resolve_expect_error(yaml.safe_dump(data, sort_keys=False))
        self.assertIn("calibration_games", msg)

    def test_both_tasks_disabled(self) -> None:
        data = yaml.safe_load(_minimal_yaml_text())
        data["tasks"]["home_win"]["enabled"] = False
        data["tasks"]["over_5_5"]["enabled"] = False
        msg = self._resolve_expect_error(yaml.safe_dump(data, sort_keys=False))
        self.assertIn("enabled", msg.lower())

    def test_invalid_split_method(self) -> None:
        data = yaml.safe_load(_minimal_yaml_text())
        data["split"]["method"] = "shuffle"
        msg = self._resolve_expect_error(yaml.safe_dump(data, sort_keys=False))
        self.assertIn("method", msg)

    def test_invalid_calibration_method(self) -> None:
        data = yaml.safe_load(_minimal_yaml_text())
        data["calibration"]["method"] = "sigmoid"
        msg = self._resolve_expect_error(yaml.safe_dump(data, sort_keys=False))
        self.assertIn("method", msg)

    def test_invalid_monotone_sign(self) -> None:
        data = yaml.safe_load(_minimal_yaml_text())
        data["models"]["lgbm"]["monotone"]["home_win"]["f_a"] = 2
        msg = self._resolve_expect_error(yaml.safe_dump(data, sort_keys=False))
        self.assertIn("monotone", msg)

    def test_holdout_fraction_and_date_range_mutually_exclusive(self) -> None:
        data = yaml.safe_load(_minimal_yaml_text())
        data["split"]["holdout"]["date_range"]["from"] = "2024-01-01"
        msg = self._resolve_expect_error(yaml.safe_dump(data, sort_keys=False))
        self.assertIn("exactly one", msg.lower())


class TestMetadataMerge(unittest.TestCase):
    def test_features_hash_mismatch_raises_diff(self) -> None:
        metadata = _minimal_metadata()
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            cfg_path = tmp / "cfg.yaml"
            cfg_path.write_text(_minimal_yaml_text(features_hash="deadbeef"), encoding="utf-8")
            with self.assertRaises(ConfigError) as ctx:
                resolve_config(cfg_path, metadata)
            msg = str(ctx.exception)
            self.assertIn("features_hash", msg)
            self.assertIn("value_yaml", msg)
            self.assertIn("value_metadata", msg)

    def test_feature_set_version_mismatch_raises_diff(self) -> None:
        metadata = _minimal_metadata()
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            cfg_path = tmp / "cfg.yaml"
            cfg_path.write_text(_minimal_yaml_text(feature_set_version="v2"), encoding="utf-8")
            with self.assertRaises(ConfigError) as ctx:
                resolve_config(cfg_path, metadata)
            self.assertIn("feature_set_version", str(ctx.exception))

    def test_missing_yaml_reference_fields_filled_from_metadata(self) -> None:
        metadata = _minimal_metadata()
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            cfg_path = tmp / "cfg.yaml"
            cfg_path.write_text(_minimal_yaml_text(), encoding="utf-8")
            resolved = resolve_config(cfg_path, metadata)
            self.assertEqual(resolved.rolling_windows, metadata["rolling_windows"])
            self.assertEqual(resolved.cold_start_policy_predict, metadata["cold_start_policy_predict"])


class TestOverrides(unittest.TestCase):
    def test_set_num_threads_override(self) -> None:
        metadata = _minimal_metadata()
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            cfg_path = tmp / "cfg.yaml"
            cfg_path.write_text(_minimal_yaml_text(), encoding="utf-8")
            resolved = resolve_config(cfg_path, metadata, overrides=[("compute.num_threads", 8)])
            self.assertEqual(resolved.compute.num_threads, 8)

    def test_set_learning_rate_list_override(self) -> None:
        metadata = _minimal_metadata()
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            cfg_path = tmp / "cfg.yaml"
            cfg_path.write_text(_minimal_yaml_text(), encoding="utf-8")
            overrides = [("models.lgbm.grids.learning_rate", [0.05, 0.1])]
            resolved = resolve_config(cfg_path, metadata, overrides=overrides)
            self.assertEqual(resolved.models.lgbm.grids.learning_rate, [0.05, 0.1])

    def test_parse_override_helper(self) -> None:
        key, value = parse_override("compute.num_threads=8")
        self.assertEqual(key, "compute.num_threads")
        self.assertEqual(value, 8)
        key, value = parse_override("models.lgbm.grids.learning_rate=[0.05,0.1]")
        self.assertEqual(value, [0.05, 0.1])


class TestHelpers(unittest.TestCase):
    def test_build_run_id_format(self) -> None:
        run_id = build_run_id(
            "home_win",
            "lgbm",
            "b334df68cab14a12056b7a41b324face3cc9cd835c30b738caffdef1b72f81a1",
            datetime(2026, 5, 30, 12, 0, 0, tzinfo=timezone.utc),
        )
        pattern = r"^home_win_lgbm_b334df68_20260530T120000Z$"
        self.assertRegex(run_id, pattern)

    def test_derive_seed_deterministic(self) -> None:
        self.assertEqual(derive_seed("bootstrap", 42), derive_seed("bootstrap", 42))
        self.assertNotEqual(derive_seed("bootstrap", 42), derive_seed("metrics", 42))

    def test_resolved_config_derive_seed(self) -> None:
        metadata = _minimal_metadata()
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            cfg_path = tmp / "cfg.yaml"
            cfg_path.write_text(_minimal_yaml_text(), encoding="utf-8")
            resolved = resolve_config(cfg_path, metadata)
            self.assertEqual(resolved.derive_seed("bootstrap"), derive_seed("bootstrap", resolved.random_seed))

    def test_configure_run_logger_creates_log_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            log_path = configure_run_logger("test_run", "INFO", reports_root=tmp / "reports")
            self.assertTrue(log_path.exists())
            logging.getLogger().info("stage 2 logger smoke")
            for handler in logging.getLogger().handlers:
                handler.flush()
            content = log_path.read_text(encoding="utf-8")
            self.assertIn("stage 2 logger smoke", content)


class TestPrintResolvedConfigCli(unittest.TestCase):
    def test_cli_print_resolved_config_roundtrip(self) -> None:
        metadata = _minimal_metadata()
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            cfg_path = tmp / "cfg.yaml"
            meta_path = tmp / "metadata_train.json"
            cfg_path.write_text(_minimal_yaml_text(), encoding="utf-8")
            meta_path.write_text(json.dumps(metadata, ensure_ascii=True), encoding="utf-8")

            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "modeling.cli",
                    "train",
                    "--config",
                    str(cfg_path),
                    "--metadata",
                    str(meta_path),
                    "--print-resolved-config",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            roundtrip = yaml.safe_load(proc.stdout)
            self.assertEqual(roundtrip["random_seed"], 7)
            self.assertEqual(roundtrip["features_hash"], metadata["features_hash"])
            reparsed = ResolvedConfig.model_validate(roundtrip)
            self.assertEqual(reparsed.compute.num_threads, 2)


class TestConfigModuleImports(unittest.TestCase):
    def test_config_module_has_no_forbidden_imports(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        source = (repo_root / "modeling" / "config.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        forbidden_roots = {"psycopg2", "modeling.dataset_builder"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    self.assertNotIn(root, forbidden_roots, msg=f"forbidden import {alias.name}")
            elif isinstance(node, ast.ImportFrom) and node.module:
                root = node.module.split(".")[0]
                if node.module.startswith("modeling.dataset_builder"):
                    self.fail(f"forbidden import from {node.module}")
                self.assertNotIn(root, forbidden_roots, msg=f"forbidden import from {node.module}")


class TestCliLazyImports(unittest.TestCase):
    def test_cli_has_no_top_level_dataset_builder_import(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        source = (repo_root / "modeling" / "cli.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in tree.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn("dataset_builder", alias.name)
            elif isinstance(node, ast.ImportFrom) and node.module:
                self.assertFalse(
                    node.module.startswith("modeling.dataset_builder"),
                    msg="dataset_builder import must not be at module level in cli.py",
                )


class TestApplyOverrides(unittest.TestCase):
    def test_apply_overrides_on_mapping(self) -> None:
        base = {"compute": {"num_threads": 2}, "random_seed": 1}
        merged = apply_overrides(base, {"compute.num_threads": 4})
        self.assertEqual(merged["compute"]["num_threads"], 4)


if __name__ == "__main__":
    unittest.main()
