"""Tests for modeling.train_common shared helpers."""

from __future__ import annotations

import unittest

import lightgbm as lgb
import numpy as np
import pandas as pd

from modeling.train_common import predict_raw_proba
from modeling.train_lgbm import predict_lgbm_proba
from modeling.train_logreg import build_logreg_pipeline


class TestPredictRawProba(unittest.TestCase):
    def test_logreg_matches_pipeline_predict_proba(self) -> None:
        rng = np.random.default_rng(0)
        X = pd.DataFrame({"f_a": rng.uniform(size=30), "f_b": rng.uniform(size=30)})
        y = (X["f_a"] > X["f_b"]).astype(int).to_numpy()
        pipeline = build_logreg_pipeline(C=1.0, random_seed=42)
        pipeline.fit(X, y)

        result = predict_raw_proba("logreg", pipeline, X)

        np.testing.assert_array_equal(result, pipeline.predict_proba(X)[:, 1])

    def test_lgbm_matches_predict_lgbm_proba(self) -> None:
        rng = np.random.default_rng(0)
        X = pd.DataFrame({"f_a": rng.uniform(size=30), "f_b": rng.uniform(size=30)})
        y = (X["f_a"] > X["f_b"]).astype(int).to_numpy()
        booster = lgb.train(
            {"objective": "binary", "verbosity": -1, "seed": 42},
            lgb.Dataset(X, label=y),
            num_boost_round=5,
        )

        result = predict_raw_proba("lgbm", booster, X)

        np.testing.assert_array_equal(result, predict_lgbm_proba(booster, X))


if __name__ == "__main__":
    unittest.main()
