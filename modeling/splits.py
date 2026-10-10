"""Season-by-season temporal splits for prematch classifiers (Задача 66).

For every checked season ``s`` the model trains on all rows of earlier seasons
(their tail is calibration and hyper-parameter selection) and is checked on every
row of ``s``. Embargo is **not** used: rolling features are built with ``shift(1)``
and never consume in-match signals, so nothing leaks across block boundaries.

Block-order validation (``_assert_strictly_before``) compares **positions** in the
sorted timeline, not calendar days. Seasons do not overlap in time, so a boundary
between two seasons never falls inside a day; the train / inner_val / calibration
boundaries are cut by row count and may land inside a day. This is safe because every
feature is either scoped to ``(team_id, season_id)`` and never looks across teams
(``modeling/dataset_builder/features.py``), or — Elo, the one exception
(``modeling/elo.py``) — is frozen per calendar day, so it never sees a same-day game's
result either.

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

from modeling.config import ConfigError, SplitConfig

METADATA_PARITY_KEYS: tuple[str, ...] = (
    "feature_set_version",
    "features_hash",
    "rolling_windows",
    "feature_manifest",
)


class SplitError(ValueError):
    """Invalid split geometry or insufficient history for the requested seasons."""


@dataclass(frozen=True)
class DayRange:
    min_day: pd.Timestamp
    max_day: pd.Timestamp

    def to_pair(self) -> tuple[pd.Timestamp, pd.Timestamp]:
        return (self.min_day, self.max_day)


@dataclass(frozen=True)
class SeasonWindow:
    """One checked season: what the model learns from and the season it is checked on."""

    season_id: int
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
            "season_id": self.season_id,
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
class SeasonSplits:
    """All per-season windows of a run, in ``split.test_seasons`` order."""

    windows: tuple[SeasonWindow, ...]

    def to_log_dict(self) -> dict[str, Any]:
        return {
            "n_windows": len(self.windows),
            "windows": [window.to_log_dict() for window in self.windows],
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


def build_season_splits(
    keys: pd.DataFrame,
    config: SplitConfig,
    *,
    yaml_reference: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> SeasonSplits:
    """Build one window per checked season (``config.test_seasons``).

    Parameters
    ----------
    keys:
        DataFrame with required columns ``day`` (datetime64), ``game_id`` and ``season_id``.
    config:
        Split section from ``modeling.config`` (``SplitConfig``).
    yaml_reference:
        Optional YAML reference mapping for metadata parity checks.
    metadata:
        Optional ``metadata_train.json`` payload for parity checks.

    Returns
    -------
    SeasonSplits
        One :class:`SeasonWindow` per ``test_seasons`` entry, in that order. Rows after
        the last checked season (a season in progress) belong to no window.

    Guarantees
    ----------
    - Stable sort by ``(day, game_id)``; input row order is ignored.
    - ``test`` = all rows of the season; ``train`` + ``inner_val`` + ``calibration`` =
      all rows of earlier seasons, split as consecutive non-overlapping blocks by position
      (the last ``calibration_games`` rows calibrate, the ``inner_val_games`` before them
      pick hyper-parameters, the rest train).
    - Deterministic for fixed ``keys`` + ``config`` (no randomness, no clock).

    Raises
    ------
    SplitError
        A checked season is absent from the data, or fewer than
        ``inner_val_games + calibration_games + 1`` rows precede it.
    """
    validate_metadata_parity(yaml_reference, metadata)

    sorted_keys = _prepare_keys(keys)
    seasons = sorted_keys["season_id"].to_numpy()
    windows: list[SeasonWindow] = []
    for season_id in config.test_seasons:
        test = np.flatnonzero(seasons == season_id)
        if len(test) == 0:
            raise SplitError(f"checked season {season_id} is not in the data")
        before = np.flatnonzero(seasons < season_id)
        need = config.inner_val_games + config.calibration_games
        if len(before) < need + 1:
            raise SplitError(
                f"not enough history before season {season_id}: need >= {need + 1} rows "
                f"(inner_val_games={config.inner_val_games} + calibration_games="
                f"{config.calibration_games} + train>=1), have {len(before)}"
            )
        calibration = before[-config.calibration_games :]
        inner_val = before[-need : -config.calibration_games]
        train = before[:-need]
        windows.append(_make_window(sorted_keys, season_id, train, inner_val, calibration, test))

    result = SeasonSplits(windows=tuple(windows))
    _validate_splits(sorted_keys, result)
    return result


def _prepare_keys(keys: pd.DataFrame) -> pd.DataFrame:
    required = {"day", "game_id", "season_id"}
    missing = required - set(keys.columns)
    if missing:
        raise SplitError(f"keys missing required columns: {sorted(missing)}")
    frame = keys.copy()
    frame["day"] = pd.to_datetime(frame["day"])
    frame = frame.sort_values(["day", "game_id"], kind="mergesort").reset_index(drop=True)
    return frame


def _make_window(
    sorted_keys: pd.DataFrame,
    season_id: int,
    train_idx: np.ndarray,
    inner_val_idx: np.ndarray,
    calibration_idx: np.ndarray,
    test_idx: np.ndarray,
) -> SeasonWindow:
    days = sorted_keys["day"]
    return SeasonWindow(
        season_id=int(season_id),
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


def _validate_splits(sorted_keys: pd.DataFrame, splits: SeasonSplits) -> None:
    days = sorted_keys["day"]
    for window in splits.windows:
        label = f"season {window.season_id}"
        _assert_strictly_before(
            days, window.train_idx, window.inner_val_idx, f"{label}: train must end before inner_val"
        )
        _assert_strictly_before(
            days,
            window.inner_val_idx,
            window.calibration_idx,
            f"{label}: inner_val must end before calibration",
        )
        _assert_strictly_before(
            days, window.calibration_idx, window.test_idx, f"{label}: calibration must end before test"
        )
    _assert_no_cross_window_overlap(
        sorted_keys["game_id"], [window.test_idx for window in splits.windows], "test"
    )


def _assert_strictly_before(
    days: pd.Series,
    earlier_idx: np.ndarray,
    later_idx: np.ndarray,
    message: str,
) -> None:
    """Assert every row of ``earlier_idx`` precedes every row of ``later_idx``.

    Compares **positions** in the ``(day, game_id)``-sorted timeline (see the
    module docstring, "Index convention"), not calendar days. On a real NHL
    calendar several games share a day and blocks are cut by row count, so a
    block boundary can legitimately fall inside a day — the old day-strict
    check rejected that and could not build a split on any calendar with
    more than one game per day (Задача 32).

    Most rolling/as-of features are safe here because they are scoped to
    ``(team_id, season_id)`` and never look across teams
    (``modeling/dataset_builder/features.py``): two games on the same day
    are four different teams with disjoint histories. Elo
    (``modeling/elo.py::compute_pregame_elo``) is the one exception — cross-team
    and cross-season — but is safe for a different reason: it freezes every
    rating at the start of each calendar day, so no game sees a same-day
    game's result regardless of block. A *future* cross-team/league-wide
    feature must make the same guarantee, or this check stops being
    sufficient on its own.

    Parameters
    ----------
    days:
        Full sorted ``day`` series; used only to render dates in the raised
        error message, not for the comparison itself.
    earlier_idx, later_idx:
        Positional indices, into the same sorted timeline, of the two blocks
        being compared. An empty block is not a violation and is skipped.
    message:
        Prefix for the raised ``SplitError``.
    """
    if len(earlier_idx) == 0 or len(later_idx) == 0:
        return
    earlier_max_pos = int(np.max(earlier_idx))
    later_min_pos = int(np.min(later_idx))
    if earlier_max_pos >= later_min_pos:
        raise SplitError(
            f"{message} (max earlier position {earlier_max_pos} "
            f"[day {days.iloc[earlier_max_pos].date()}] >= min later position "
            f"{later_min_pos} [day {days.iloc[later_min_pos].date()}])"
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
    "SeasonSplits",
    "SeasonWindow",
    "SplitError",
    "build_season_splits",
    "validate_metadata_parity",
]
