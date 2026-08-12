"""Walk-forward temporal splits for prematch classifiers (UPDATE plan stage 4).

Embargo is **not** used: rolling features in the training dataset are built with
``shift(1)`` and never consume in-match / same-row signals, so there is no
information leak across block boundaries. See
``plan/classifier/nhl_classifier_modeling_plan_UPDATE.md``, section
"### 4. Временные сплиты без утечки".

Index convention
----------------
All ``*_idx`` fields are **positional integer indices** into the input keys
DataFrame after stable sort by ``(day, game_id)`` and ``reset_index(drop=True)``.
Use ``keys.iloc[idx]`` to materialize rows. ``game_id`` is not stored in split
structures to avoid duplicate identity columns.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from modeling.config import ConfigError, HoldoutConfig, SplitConfig, SplitMethod

METADATA_PARITY_KEYS: tuple[str, ...] = (
    "feature_set_version",
    "features_hash",
    "rolling_windows",
    "feature_manifest",
)


class SplitError(ValueError):
    """Invalid split geometry or insufficient history for the requested windows."""


@dataclass(frozen=True)
class DayRange:
    min_day: pd.Timestamp
    max_day: pd.Timestamp

    def to_pair(self) -> tuple[pd.Timestamp, pd.Timestamp]:
        return (self.min_day, self.max_day)


@dataclass(frozen=True)
class OuterWindow:
    """One walk-forward outer window ``k`` (1-indexed, chronological)."""

    k: int
    train_idx: np.ndarray
    inner_val_idx: np.ndarray
    calibration_idx: np.ndarray
    test_idx: np.ndarray
    train_days: DayRange
    inner_val_days: DayRange
    calibration_days: DayRange
    test_days: DayRange
    train_size: int
    inner_val_size: int
    calibration_size: int
    test_size: int

    def to_log_dict(self) -> dict[str, Any]:
        """Structured metadata suitable for ``metrics.json`` / ``run.log``."""
        return {
            "k": self.k,
            "train_size": self.train_size,
            "inner_val_size": self.inner_val_size,
            "calibration_size": self.calibration_size,
            "test_size": self.test_size,
            "train_days": [str(self.train_days.min_day.date()), str(self.train_days.max_day.date())],
            "inner_val_days": [
                str(self.inner_val_days.min_day.date()),
                str(self.inner_val_days.max_day.date()),
            ],
            "calibration_days": [
                str(self.calibration_days.min_day.date()),
                str(self.calibration_days.max_day.date()),
            ],
            "test_days": [str(self.test_days.min_day.date()), str(self.test_days.max_day.date())],
            "train_idx_range": _positional_range(self.train_idx),
            "test_idx_range": _positional_range(self.test_idx),
        }


@dataclass(frozen=True)
class HoldoutSplit:
    holdout_idx: np.ndarray
    holdout_days: DayRange
    holdout_size: int

    def to_log_dict(self) -> dict[str, Any]:
        return {
            "holdout_size": self.holdout_size,
            "holdout_days": [
                str(self.holdout_days.min_day.date()),
                str(self.holdout_days.max_day.date()),
            ],
            "holdout_idx_range": _positional_range(self.holdout_idx),
        }


@dataclass(frozen=True)
class WalkForwardSplits:
    windows: tuple[OuterWindow, ...]
    holdout: HoldoutSplit

    def to_log_dict(self) -> dict[str, Any]:
        return {
            "n_windows": len(self.windows),
            "windows": [window.to_log_dict() for window in self.windows],
            "holdout": self.holdout.to_log_dict(),
        }


def validate_metadata_parity(
    yaml_reference: Mapping[str, Any] | None,
    metadata: Mapping[str, Any] | None,
) -> None:
    """Fail-fast when YAML reference copies disagree with ``metadata_train.json``."""
    if yaml_reference is None or metadata is None:
        return
    diffs: list[dict[str, Any]] = []
    for key in METADATA_PARITY_KEYS:
        yaml_value = yaml_reference.get(key)
        if yaml_value is None:
            continue
        meta_value = metadata.get(key)
        if meta_value is None:
            continue
        if yaml_value != meta_value:
            diffs.append(
                {
                    "field": key,
                    "value_yaml": yaml_value,
                    "value_metadata": meta_value,
                }
            )
    if diffs:
        lines = ["Config metadata conflict (YAML vs metadata_train.json):"]
        for item in diffs:
            lines.append(
                f"  {item['field']}: value_yaml={item['value_yaml']!r} "
                f"value_metadata={item['value_metadata']!r}"
            )
        raise ConfigError("\n".join(lines))


def build_walk_forward_splits(
    keys: pd.DataFrame,
    config: SplitConfig,
    *,
    game_ids: pd.Series | None = None,
    yaml_reference: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> WalkForwardSplits:
    """Build ordered walk-forward outer windows and a single final holdout.

    Parameters
    ----------
    keys:
        DataFrame with required columns ``day`` (datetime64) and ``game_id``.
    config:
        Split section from ``modeling.config`` (``SplitConfig``).
    game_ids:
        Optional pre-extracted ``game_id`` series aligned with ``keys``; when
        omitted, ``keys['game_id']`` is used after sorting.
    yaml_reference:
        Optional YAML reference mapping for metadata parity checks.
    metadata:
        Optional ``metadata_train.json`` payload for parity checks.

    Returns
    -------
    WalkForwardSplits
        Chronological outer windows (``k=1..n_test_windows``) plus holdout cut
        from the timeline tail **before** walk-forward windows are carved.

    Guarantees
    ----------
    - Stable sort by ``(day, game_id)``; input row order is ignored.
    - Expanding train: ``train_k`` is strictly before ``inner_val_k``.
    - ``inner_val_k``, ``calibration_k``, ``test_k`` are consecutive,
      non-overlapping blocks within each window; holdout is after all ``test_k``.
    - ``test_k`` blocks do not overlap across windows (by ``game_id``).
    - For ``method=month``, ``inner_val_k`` and ``calibration_k`` may overlap
      across windows: each window takes the tail of games immediately before its
      test month, so neighbouring windows share validation/calibration rows.
      This is intentional (no test leakage — each ``test_k`` is isolated).
      For ``method=fixed_games``, outer blocks are disjoint and inner_val/cal
      do not overlap across windows.
    - Deterministic for fixed ``keys`` + ``config`` (no randomness, no clock).
    """
    validate_metadata_parity(yaml_reference, metadata)

    sorted_keys = _prepare_keys(keys)
    days = sorted_keys["day"]
    ids = game_ids if game_ids is not None else sorted_keys["game_id"]
    if len(ids) != len(sorted_keys):
        raise SplitError("game_ids length must match keys after sorting")

    holdout_idx = _holdout_indices(days, config.holdout)
    wf_mask = np.ones(len(sorted_keys), dtype=bool)
    wf_mask[list(holdout_idx)] = False
    wf_positions = np.flatnonzero(wf_mask)

    if len(wf_positions) == 0:
        raise SplitError("no rows remain for walk-forward after holdout cut")

    if config.method == SplitMethod.month:
        windows = _windows_calendar_month(sorted_keys, wf_positions, config)
    elif config.method == SplitMethod.fixed_games:
        windows = _windows_fixed_games(sorted_keys, wf_positions, config)
    else:
        raise SplitError(f"unsupported split method: {config.method!r}")

    if len(windows) != config.n_test_windows:
        raise SplitError(
            f"expected {config.n_test_windows} outer windows, built {len(windows)}"
        )

    holdout_block = HoldoutSplit(
        holdout_idx=holdout_idx,
        holdout_days=_day_range(days, holdout_idx),
        holdout_size=int(len(holdout_idx)),
    )

    result = WalkForwardSplits(windows=tuple(windows), holdout=holdout_block)
    _validate_splits(sorted_keys, ids, result, config)
    return result


def _prepare_keys(keys: pd.DataFrame) -> pd.DataFrame:
    required = {"day", "game_id"}
    missing = required - set(keys.columns)
    if missing:
        raise SplitError(f"keys missing required columns: {sorted(missing)}")
    frame = keys.copy()
    frame["day"] = pd.to_datetime(frame["day"])
    frame = frame.sort_values(["day", "game_id"], kind="mergesort").reset_index(drop=True)
    return frame


def _holdout_indices(days: pd.Series, holdout: HoldoutConfig) -> np.ndarray:
    if holdout.fraction is not None:
        unique_days = pd.Series(days.unique()).sort_values()
        n_holdout_days = max(1, int(np.ceil(len(unique_days) * holdout.fraction)))
        cutoff_days = set(unique_days.iloc[-n_holdout_days:].tolist())
        return np.flatnonzero(days.isin(cutoff_days).to_numpy())
    date_range = holdout.date_range
    mask = pd.Series(True, index=days.index)
    if date_range.from_ is not None:
        mask &= days >= pd.Timestamp(date_range.from_)
    if date_range.to is not None:
        mask &= days <= pd.Timestamp(date_range.to)
    holdout_idx = np.flatnonzero(mask.to_numpy())
    if len(holdout_idx) == 0:
        raise SplitError("holdout date_range matched zero rows")
    return holdout_idx


def _windows_calendar_month(
    sorted_keys: pd.DataFrame,
    wf_positions: np.ndarray,
    config: SplitConfig,
) -> list[OuterWindow]:
    wf_days = sorted_keys.iloc[wf_positions]["day"]
    month_codes = wf_days.dt.to_period("M")
    unique_months = month_codes.drop_duplicates().tolist()
    if len(unique_months) < config.n_test_windows:
        raise SplitError(
            "not enough history for "
            f"{config.n_test_windows} outer windows with current split parameters "
            f"(only {len(unique_months)} calendar months in walk-forward region)"
        )

    test_months = unique_months[-config.n_test_windows :]
    windows: list[OuterWindow] = []
    for k, month in enumerate(test_months, start=1):
        month_positions = wf_positions[month_codes.to_numpy() == month]
        if len(month_positions) == 0:
            raise SplitError(f"calendar month {month} has zero games in walk-forward region")
        test_start = int(month_positions[0])
        before_test = wf_positions[wf_positions < test_start]
        window = _window_from_tail(sorted_keys, before_test, month_positions, config, k)
        windows.append(window)
    return windows


def _windows_fixed_games(
    sorted_keys: pd.DataFrame,
    wf_positions: np.ndarray,
    config: SplitConfig,
) -> list[OuterWindow]:
    assert config.outer_block_games is not None
    test_games = config.outer_block_games - config.inner_val_games - config.calibration_games
    if test_games <= 0:
        raise SplitError(
            "fixed_games test block size must be > 0; increase split.outer_block_games"
        )
    block_size = config.outer_block_games
    total_block_span = block_size * config.n_test_windows
    if len(wf_positions) < total_block_span:
        raise SplitError(
            "not enough history for "
            f"{config.n_test_windows} outer windows with current split parameters "
            f"(need at least {total_block_span} walk-forward rows, have {len(wf_positions)})"
        )

    windows: list[OuterWindow] = []
    base = len(wf_positions) - total_block_span
    for k in range(1, config.n_test_windows + 1):
        start = base + (k - 1) * block_size
        block = wf_positions[start : start + block_size]
        test = block[-test_games:]
        cal = block[-(test_games + config.calibration_games) : -test_games]
        inner_val = block[
            -(test_games + config.calibration_games + config.inner_val_games) : -(
                test_games + config.calibration_games
            )
        ]
        train = wf_positions[wf_positions < int(block[0])]
        windows.append(
            _make_window(sorted_keys, k, train, inner_val, cal, test, config)
        )
    return windows


def _window_from_tail(
    sorted_keys: pd.DataFrame,
    before_test: np.ndarray,
    test_positions: np.ndarray,
    config: SplitConfig,
    k: int,
) -> OuterWindow:
    need = config.inner_val_games + config.calibration_games
    if len(before_test) < need:
        raise SplitError(
            "not enough history for "
            f"{config.n_test_windows} outer windows with current split parameters "
            f"(window k={k}: need {need} rows before test, have {len(before_test)})"
        )
    cal = before_test[-config.calibration_games :]
    inner_val = before_test[-(config.inner_val_games + config.calibration_games) : -config.calibration_games]
    train = before_test[: -need]
    return _make_window(sorted_keys, k, train, inner_val, cal, test_positions, config)


def _make_window(
    sorted_keys: pd.DataFrame,
    k: int,
    train_idx: np.ndarray,
    inner_val_idx: np.ndarray,
    calibration_idx: np.ndarray,
    test_idx: np.ndarray,
    config: SplitConfig,
) -> OuterWindow:
    days = sorted_keys["day"]
    if len(train_idx) == 0:
        raise SplitError(f"window k={k}: train block is empty")
    if len(test_idx) == 0:
        raise SplitError(f"window k={k}: test block is empty")
    if len(inner_val_idx) < config.inner_val_games:
        raise SplitError(
            f"window k={k}: inner_val has {len(inner_val_idx)} rows, "
            f"need >= {config.inner_val_games}"
        )
    if len(calibration_idx) < config.calibration_games:
        raise SplitError(
            f"window k={k}: calibration has {len(calibration_idx)} rows, "
            f"need >= {config.calibration_games}"
        )
    return OuterWindow(
        k=k,
        train_idx=np.asarray(train_idx, dtype=np.int64),
        inner_val_idx=np.asarray(inner_val_idx, dtype=np.int64),
        calibration_idx=np.asarray(calibration_idx, dtype=np.int64),
        test_idx=np.asarray(test_idx, dtype=np.int64),
        train_days=_day_range(days, train_idx),
        inner_val_days=_day_range(days, inner_val_idx),
        calibration_days=_day_range(days, calibration_idx),
        test_days=_day_range(days, test_idx),
        train_size=int(len(train_idx)),
        inner_val_size=int(len(inner_val_idx)),
        calibration_size=int(len(calibration_idx)),
        test_size=int(len(test_idx)),
    )


def _day_range(days: pd.Series, indices: np.ndarray) -> DayRange:
    subset = days.iloc[indices]
    return DayRange(min_day=subset.min(), max_day=subset.max())


def _positional_range(indices: np.ndarray) -> list[int | None]:
    """Return ``[min, max]`` positional index bounds (not ``game_id`` values)."""
    if len(indices) == 0:
        return [None, None]
    return [int(indices.min()), int(indices.max())]


def _validate_splits(
    sorted_keys: pd.DataFrame,
    game_ids: pd.Series,
    splits: WalkForwardSplits,
    config: SplitConfig,
) -> None:
    days = sorted_keys["day"]
    holdout_ids = set(game_ids.iloc[splits.holdout.holdout_idx].tolist())

    seen_test: list[np.ndarray] = []

    for window in splits.windows:
        _assert_strictly_before(
            days,
            window.train_idx,
            window.inner_val_idx,
            f"window k={window.k}: train must end before inner_val",
        )
        _assert_strictly_before(
            days,
            window.inner_val_idx,
            window.calibration_idx,
            f"window k={window.k}: inner_val must end before calibration",
        )
        _assert_strictly_before(
            days,
            window.calibration_idx,
            window.test_idx,
            f"window k={window.k}: calibration must end before test",
        )
        _assert_strictly_before(
            days,
            window.test_idx,
            splits.holdout.holdout_idx,
            f"window k={window.k}: test must end before holdout",
        )

        for label, idx in (
            ("train", window.train_idx),
            ("inner_val", window.inner_val_idx),
            ("calibration", window.calibration_idx),
            ("test", window.test_idx),
        ):
            overlap = holdout_ids.intersection(game_ids.iloc[idx].tolist())
            if overlap:
                raise SplitError(
                    f"window k={window.k}: {label} overlaps holdout on "
                    f"{len(overlap)} game_id(s)"
                )

        seen_test.append(window.test_idx)

    _assert_no_cross_window_overlap(game_ids, seen_test, "test")
    # ``fixed_games`` uses disjoint outer blocks; ``month`` reuses tail prefixes
    # before each test month, so inner_val/cal may overlap across windows by design.
    if config.method == SplitMethod.fixed_games:
        seen_inner = [window.inner_val_idx for window in splits.windows]
        seen_cal = [window.calibration_idx for window in splits.windows]
        _assert_no_cross_window_overlap(game_ids, seen_inner, "inner_val")
        _assert_no_cross_window_overlap(game_ids, seen_cal, "calibration")


def _assert_strictly_before(
    days: pd.Series,
    earlier_idx: np.ndarray,
    later_idx: np.ndarray,
    message: str,
) -> None:
    if len(earlier_idx) == 0 or len(later_idx) == 0:
        return
    if days.iloc[earlier_idx].max() >= days.iloc[later_idx].min():
        raise SplitError(
            f"{message} (max earlier day {days.iloc[earlier_idx].max()} "
            f">= min later day {days.iloc[later_idx].min()})"
        )


def _assert_no_cross_window_overlap(
    game_ids: pd.Series,
    blocks: Sequence[np.ndarray],
    block_name: str,
) -> None:
    for i in range(len(blocks)):
        ids_i = set(game_ids.iloc[blocks[i]].tolist())
        for j in range(i + 1, len(blocks)):
            overlap = ids_i.intersection(game_ids.iloc[blocks[j]].tolist())
            if overlap:
                raise SplitError(
                    f"{block_name} blocks for windows {i + 1} and {j + 1} "
                    f"overlap on {len(overlap)} game_id(s)"
                )


__all__ = [
    "DayRange",
    "HoldoutSplit",
    "OuterWindow",
    "SplitError",
    "WalkForwardSplits",
    "build_walk_forward_splits",
    "validate_metadata_parity",
]
