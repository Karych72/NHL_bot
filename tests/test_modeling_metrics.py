"""Tests for modeling metrics and report assembly (stage 5)."""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest

from modeling.metrics import (
    DEFAULT_EPSILON,
    MetricsInputError,
    brier,
    ece,
    log_loss,
    reliability_table,
    team_breakdown,
    trivial_baseline,
)
from modeling.report import (
    compose_metrics_json,
    compose_summary_md,
    configure_run_logger,
    logger as report_logger,
    plot_reliability,
    write_report,
)

Y_MINI = np.array([0, 1, 1, 0])
P_MINI = np.array([0.1, 0.9, 0.7, 0.2])

EXPECTED_LOG_LOSS_MINI = float(
    np.mean(
        [
            -math.log(0.9),
            -math.log(0.9),
            -math.log(0.7),
            -math.log(0.8),
        ]
    )
)
EXPECTED_BRIER_MINI = 0.0375
EXPECTED_ECE_MINI = 0.175

VALID_RUN_ID = "home_win_lgbm_b334df68_20260530T143022Z"


def _metric_block(
    *,
    k: int | None = None,
    n_test: int = 100,
    raw: dict | None = None,
    trivial: dict | None = None,
) -> dict:
    payload = {
        "k": k,
        "train_range": {"start": "2018-10-01", "end": "2020-03-31"},
        "test_range": {"start": "2020-04-01", "end": "2020-04-30"},
        "n_train": 1000,
        "n_test": n_test,
        "raw": raw
        or {"log_loss": 0.62, "brier": 0.21, "ece": 0.04},
        "calibrated": None,
        "trivial_base_rate": trivial
        or {"log_loss": 0.69, "brier": 0.25, "p": 0.54},
    }
    if k is None:
        payload.pop("k")
    return payload


def _sample_metrics_json() -> dict:
    fold = _metric_block(k=1)
    holdout = _metric_block(n_test=200)
    holdout["reliability_path"] = "reliability_home_win.png"
    return compose_metrics_json(
        run_id=VALID_RUN_ID,
        task="home_win",
        model="lgbm",
        features_hash="b334df68cab14a12056b7a41b324face3cc9cd835c30b738caffdef1b72f81a1",
        folds=[fold],
        holdout=holdout,
        team_breakdown={
            "home_team_id": [
                {"team_id": 1, "n_games": 10, "log_loss": 0.8, "log_loss_minus_overall": 0.2},
                {"team_id": 2, "n_games": 12, "log_loss": 0.5, "log_loss_minus_overall": -0.1},
            ],
            "away_team_id": [],
        },
    )


class TestReferenceMetrics:
    def test_log_loss_brier_ece_on_mini_vectors(self) -> None:
        assert log_loss(Y_MINI, P_MINI) == pytest.approx(EXPECTED_LOG_LOSS_MINI, rel=1e-12)
        assert brier(Y_MINI, P_MINI) == pytest.approx(EXPECTED_BRIER_MINI, rel=1e-12)
        assert ece(Y_MINI, P_MINI, n_bins=10) == pytest.approx(EXPECTED_ECE_MINI, rel=1e-12)

    def test_log_loss_clips_extreme_probabilities(self) -> None:
        y = np.array([0, 1, 0, 1])
        p = np.array([0.0, 1.0, 1.0, 0.0])
        value = log_loss(y, p, epsilon=DEFAULT_EPSILON)
        assert math.isfinite(value)
        p_clip = np.clip(p, DEFAULT_EPSILON, 1.0 - DEFAULT_EPSILON)
        expected = float(np.mean(-(y * np.log(p_clip) + (1.0 - y) * np.log(1.0 - p_clip))))
        assert value == pytest.approx(expected, rel=1e-12)


class TestCalibrationMetrics:
    def test_ece_near_zero_for_perfectly_calibrated_synthetic_slice(self) -> None:
        rng = np.random.default_rng(12345)
        n = 50_000
        p = rng.uniform(0.0, 1.0, size=n)
        y = rng.binomial(1, p).astype(float)
        assert ece(y, p, n_bins=10) < 0.01

    def test_reliability_table_weights_and_empty_bins(self) -> None:
        table = reliability_table(Y_MINI, P_MINI, n_bins=10)
        assert len(table) == 10
        assert table["weight"].sum() == pytest.approx(1.0, rel=1e-12)
        empty = table[table["count"] == 0]
        assert not empty.empty
        assert empty["mean_pred"].isna().all()
        assert empty["frac_positive"].isna().all()


class TestTrivialBaseline:
    def test_base_rate_comes_from_train_not_test(self) -> None:
        y_train = np.zeros(100, dtype=float)
        y_test = np.ones(10, dtype=float)
        result = trivial_baseline(y_train, y_test, epsilon=DEFAULT_EPSILON)
        assert result["p"] == pytest.approx(0.0, abs=1e-15)
        assert result["log_loss"] == pytest.approx(-math.log(DEFAULT_EPSILON), rel=1e-12)

        swapped = trivial_baseline(y_test, y_train, epsilon=DEFAULT_EPSILON)
        assert swapped["p"] == pytest.approx(1.0, abs=1e-15)
        assert swapped["log_loss"] != pytest.approx(result["log_loss"], rel=1e-6)


class TestTeamBreakdown:
    def test_bad_team_has_higher_log_loss_than_overall(self) -> None:
        y = np.array([1, 1, 1, 0, 0, 0])
        p = np.array([0.9, 0.85, 0.8, 0.2, 0.15, 0.95])
        teams = np.array([1, 1, 1, 2, 2, 2])
        table = team_breakdown(y, p, team_ids=teams, by="home_team_id")
        assert list(table.columns) == ["team_id", "n_games", "log_loss", "log_loss_minus_overall"]
        bad = table.loc[table["team_id"] == 2].iloc[0]
        assert bad["log_loss_minus_overall"] > 0.0

    def test_invalid_by_raises(self) -> None:
        with pytest.raises(MetricsInputError, match="by must be"):
            team_breakdown(
                [0, 1],
                [0.5, 0.5],
                team_ids=[1, 2],
                by="invalid_column",  # type: ignore[arg-type]
            )


class TestReliabilityPlot:
    def test_plot_reliability_writes_non_empty_png(
        self,
        tmp_path: Path,
        matplotlib_agg_backend: None,
    ) -> None:
        table = reliability_table(Y_MINI, P_MINI, n_bins=10)
        out = tmp_path / "reliability_home_win.png"
        plot_reliability(table, title="test", out_path=out)
        assert out.exists()
        assert out.stat().st_size > 0


class TestComposeMetricsJson:
    def test_valid_run_id_passes(self) -> None:
        payload = _sample_metrics_json()
        assert payload["run_id"] == VALID_RUN_ID
        assert payload["folds"][0]["trivial_base_rate"]["log_loss"] == 0.69
        assert payload["holdout"]["trivial_base_rate"]["brier"] == 0.25

    def test_invalid_run_id_raises(self) -> None:
        with pytest.raises(ValueError, match="run_id must match"):
            compose_metrics_json(
                run_id="weird-id",
                task="home_win",
                model="logreg",
                features_hash="abc",
                folds=[],
                holdout=_metric_block(),
                team_breakdown={},
            )

    def test_fold_missing_trivial_base_rate_raises(self) -> None:
        bad_fold = _metric_block(k=1)
        del bad_fold["trivial_base_rate"]
        holdout = _metric_block(n_test=200)
        holdout["reliability_path"] = "reliability_home_win.png"
        with pytest.raises(ValueError, match="trivial_base_rate"):
            compose_metrics_json(
                run_id=VALID_RUN_ID,
                task="home_win",
                model="lgbm",
                features_hash="b334df68cab14a12056b7a41b324face3cc9cd835c30b738caffdef1b72f81a1",
                folds=[bad_fold],
                holdout=holdout,
                team_breakdown={},
            )


class TestWriteReport:
    def test_writes_metrics_summary_and_reliability_png(
        self,
        tmp_path: Path,
        matplotlib_agg_backend: None,
    ) -> None:
        metrics_json = _sample_metrics_json()
        rel_df = reliability_table(Y_MINI, P_MINI, n_bins=10)
        summary = compose_summary_md(metrics_json)
        out_dir = tmp_path / VALID_RUN_ID
        configure_run_logger(out_dir, level="INFO")
        write_report(
            out_dir,
            metrics_json=metrics_json,
            reliability_pngs={"home_win": rel_df},
            summary_md=summary,
        )
        write_report(
            out_dir,
            metrics_json=metrics_json,
            reliability_pngs={"home_win": rel_df},
            summary_md=summary,
        )

        metrics_path = out_dir / "metrics.json"
        summary_path = out_dir / "summary.md"
        png_path = out_dir / "reliability_home_win.png"
        log_path = out_dir / "run.log"
        assert metrics_path.exists()
        assert summary_path.exists() and summary_path.read_text(encoding="utf-8")
        assert png_path.exists() and png_path.stat().st_size > 0

        loaded = json.loads(metrics_path.read_text(encoding="utf-8"))
        raw_text = metrics_path.read_text(encoding="utf-8")
        assert list(loaded.keys()) == sorted(loaded.keys())
        assert raw_text.index('"evaluation"') < raw_text.index('"features_hash"')

        log_text = log_path.read_text(encoding="utf-8")
        assert "Overwriting existing file" in log_text


class TestConfigureRunLogger:
    def test_creates_run_log_and_is_idempotent(self, tmp_path: Path) -> None:
        out_dir = tmp_path / "report_dir"
        logger_a = configure_run_logger(out_dir, level="INFO")
        assert logger_a is report_logger
        logger_a.info("first line")
        handler_count = len(logger_a.handlers)

        logger_b = configure_run_logger(out_dir, level="INFO")
        assert logger_b is logger_a
        assert len(logger_b.handlers) == handler_count

        log_path = out_dir / "run.log"
        assert log_path.exists()
        for handler in logger_a.handlers:
            handler.flush()
        assert "first line" in log_path.read_text(encoding="utf-8")
