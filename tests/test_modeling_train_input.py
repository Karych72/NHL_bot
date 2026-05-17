"""Tests for modeling.train_input train artifact contract."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from modeling.dataset_builder.schema import LABEL_COLUMNS, features_hash
from modeling.train_input import (
    TrainMetadataError,
    TrainSchemaError,
    load_training_table_split,
    split_training_frame,
)


def _minimal_manifest() -> list[dict[str, str]]:
    return [
        {"name": "f_a", "dtype": "float64", "position": "0"},
        {"name": "f_b", "dtype": "float64", "position": "1"},
    ]


def _minimal_metadata_rows(*, manifest: list[dict[str, str]], rows: int = 2) -> dict:
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
        "mode": "train",
        "feature_set_version": fsv,
        "features_hash": fh,
        "feature_manifest": manifest,
        "rolling_windows": rolling,
        "cold_start_policy_predict": cold_predict,
        "min_prior_games": 5,
        "dataset_rows": rows,
        "code_version": "test",
        "data_snapshot_id": "test-snap",
        "dataset_built_at": "2020-01-01T00:00:00Z",
    }


def _write_pair(tmp: Path, csv_text: str, metadata: dict) -> tuple[Path, Path]:
    csv_path = tmp / "dataset_train.csv"
    meta_path = tmp / "metadata_train.json"
    csv_path.write_text(csv_text, encoding="utf-8")
    meta_path.write_text(json.dumps(metadata, ensure_ascii=True, indent=2), encoding="utf-8")
    return csv_path, meta_path


class TestTrainInputHappyPath(unittest.TestCase):
    def test_split_matches_manifest_order_and_groups(self) -> None:
        manifest = _minimal_manifest()
        meta = _minimal_metadata_rows(manifest=manifest, rows=2)
        rows = (
            "game_id,day,f_a,f_b,y_home_win,y_over_5_5,feature_set_version,dataset_built_at,source_snapshot_id\n"
            "1,2024-01-01,0.1,0.2,1,0,v1,2024-01-01Z,snap\n"
            "2,2024-01-02,0.3,0.4,0,1,v1,2024-01-01Z,snap\n"
        )
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            csv_path, meta_path = _write_pair(tmp, rows, meta)
            X, keys, labels, service, md = load_training_table_split(csv_path, meta_path)

            self.assertEqual(list(X.columns), ["f_a", "f_b"])
            self.assertEqual(list(keys.columns), ["game_id", "day"])
            self.assertEqual(list(labels.columns), list(LABEL_COLUMNS))
            self.assertIn("feature_set_version", service.columns)
            self.assertEqual(md["features_hash"], meta["features_hash"])
            self.assertEqual(X.iloc[0]["f_a"], 0.1)
            self.assertEqual(labels.iloc[1]["y_over_5_5"], 1)


class TestTrainInputFailures(unittest.TestCase):
    def test_missing_feature_manifest_key(self) -> None:
        meta = {"features_hash": "x", "feature_set_version": "v1"}
        rows = (
            "game_id,day,f_a,f_b,y_home_win,y_over_5_5,feature_set_version,dataset_built_at,source_snapshot_id\n"
            "1,2024-01-01,0.1,0.2,1,0,v1,2024-01-01Z,snap\n"
        )
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            csv_path, meta_path = _write_pair(tmp, rows, meta)
            with self.assertRaises(TrainMetadataError):
                load_training_table_split(csv_path, meta_path)

    def test_missing_feature_set_version(self) -> None:
        manifest = _minimal_manifest()
        meta = _minimal_metadata_rows(manifest=manifest)
        del meta["feature_set_version"]
        rows = (
            "game_id,day,f_a,f_b,y_home_win,y_over_5_5,feature_set_version,dataset_built_at,source_snapshot_id\n"
            "1,2024-01-01,0.1,0.2,1,0,v1,2024-01-01Z,snap\n"
        )
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            csv_path, meta_path = _write_pair(tmp, rows, meta)
            with self.assertRaises(TrainMetadataError):
                load_training_table_split(csv_path, meta_path)

    def test_manifest_entry_missing_dtype(self) -> None:
        manifest = [{"name": "f_a", "position": "0"}]
        meta = _minimal_metadata_rows(manifest=manifest)
        rows = (
            "game_id,day,f_a,f_b,y_home_win,y_over_5_5,feature_set_version,dataset_built_at,source_snapshot_id\n"
            "1,2024-01-01,0.1,0.2,1,0,v1,2024-01-01Z,snap\n"
        )
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            csv_path, meta_path = _write_pair(tmp, rows, meta)
            with self.assertRaises(TrainMetadataError) as ctx:
                load_training_table_split(csv_path, meta_path)
            self.assertIn("dtype", str(ctx.exception))

    def test_invalid_metadata_json(self) -> None:
        manifest = _minimal_manifest()
        meta = _minimal_metadata_rows(manifest=manifest)
        rows = (
            "game_id,day,f_a,f_b,y_home_win,y_over_5_5,feature_set_version,dataset_built_at,source_snapshot_id\n"
            "1,2024-01-01,0.1,0.2,1,0,v1,2024-01-01Z,snap\n"
        )
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            csv_path = tmp / "dataset_train.csv"
            meta_path = tmp / "metadata_train.json"
            csv_path.write_text(rows, encoding="utf-8")
            meta_path.write_text("{not valid json", encoding="utf-8")
            with self.assertRaises(TrainMetadataError) as ctx:
                load_training_table_split(csv_path, meta_path)
            self.assertIn("invalid JSON", str(ctx.exception))

    def test_missing_features_hash_in_metadata(self) -> None:
        manifest = _minimal_manifest()
        meta = _minimal_metadata_rows(manifest=manifest)
        del meta["features_hash"]
        rows = (
            "game_id,day,f_a,f_b,y_home_win,y_over_5_5,feature_set_version,dataset_built_at,source_snapshot_id\n"
            "1,2024-01-01,0.1,0.2,1,0,v1,2024-01-01Z,snap\n"
        )
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            csv_path, meta_path = _write_pair(tmp, rows, meta)
            with self.assertRaises(TrainMetadataError):
                load_training_table_split(csv_path, meta_path)

    def test_features_hash_recompute_mismatch(self) -> None:
        manifest = _minimal_manifest()
        meta = _minimal_metadata_rows(manifest=manifest)
        meta["features_hash"] = "deadbeef"
        rows = (
            "game_id,day,f_a,f_b,y_home_win,y_over_5_5,feature_set_version,dataset_built_at,source_snapshot_id\n"
            "1,2024-01-01,0.1,0.2,1,0,v1,2024-01-01Z,snap\n"
        )
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            csv_path, meta_path = _write_pair(tmp, rows, meta)
            with self.assertRaises(TrainMetadataError) as ctx:
                load_training_table_split(csv_path, meta_path)
            self.assertIn("features_hash mismatch", str(ctx.exception))

    def test_feature_column_order_mismatch_vs_manifest(self) -> None:
        manifest = _minimal_manifest()
        meta = _minimal_metadata_rows(manifest=manifest)
        # Swap f_a / f_b in CSV relative to manifest order → parity fails
        rows = (
            "game_id,day,f_b,f_a,y_home_win,y_over_5_5,feature_set_version,dataset_built_at,source_snapshot_id\n"
            "1,2024-01-01,0.2,0.1,1,0,v1,2024-01-01Z,snap\n"
        )
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            csv_path, meta_path = _write_pair(tmp, rows, meta)
            with self.assertRaises(TrainSchemaError):
                load_training_table_split(csv_path, meta_path)

    def test_unknown_extra_column_fail_fast(self) -> None:
        manifest = _minimal_manifest()
        meta = _minimal_metadata_rows(manifest=manifest)
        rows = (
            "game_id,day,f_a,f_b,mystery_col,y_home_win,y_over_5_5,"
            "feature_set_version,dataset_built_at,source_snapshot_id\n"
            "1,2024-01-01,0.1,0.2,9.0,1,0,v1,2024-01-01Z,snap\n"
        )
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            csv_path, meta_path = _write_pair(tmp, rows, meta)
            with self.assertRaises(TrainSchemaError) as ctx:
                load_training_table_split(csv_path, meta_path)
            self.assertIn("unexpected columns", str(ctx.exception))

    def test_csv_missing_column_listed_in_feature_manifest(self) -> None:
        manifest = _minimal_manifest()
        meta = _minimal_metadata_rows(manifest=manifest)
        # Manifest requires f_a and f_b; drop f_b from CSV header
        rows = (
            "game_id,day,f_a,y_home_win,y_over_5_5,feature_set_version,dataset_built_at,source_snapshot_id\n"
            "1,2024-01-01,0.1,1,0,v1,2024-01-01Z,snap\n"
        )
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            csv_path, meta_path = _write_pair(tmp, rows, meta)
            with self.assertRaises(TrainSchemaError) as ctx:
                load_training_table_split(csv_path, meta_path)
            self.assertIn("missing manifest feature columns", str(ctx.exception))
            self.assertIn("f_b", str(ctx.exception))


class TestSplitTrainingFrameGuards(unittest.TestCase):
    def test_split_rejects_unknown_columns(self) -> None:
        import pandas as pd

        manifest = _minimal_manifest()
        meta = _minimal_metadata_rows(manifest=manifest)
        df = pd.DataFrame(
            {
                "game_id": [1],
                "f_a": [1.0],
                "f_b": [2.0],
                "ghost": [3.0],
            }
        )
        with self.assertRaises(TrainSchemaError):
            split_training_frame(df, meta)


if __name__ == "__main__":
    unittest.main()
