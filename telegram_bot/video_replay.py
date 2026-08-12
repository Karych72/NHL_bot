"""
NHL Goal Video Replay service.

Chain: game_id + event_id → NHL Replay API → Brightcove → MP4 file.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from typing import List, Optional

import requests

logger = logging.getLogger(__name__)

NHL_BASE = "https://api-web.nhle.com"
BC_ACCOUNT = "6415718365001"
BC_POLICY_KEY = (
    "BCpkADawqM3l37Vq8trLJ95vVwxubXYZXYglAopEZXQTHTWX3YdalyF9xmkuknxjBgiMYwt8VZ_"
    "OZ1jAjYxz_yzuNh_cjC3uOaMspVTD-hZfNUHtNnBnhVD0Gmsih8TBF8QlQFXiCQM3W_u4ydJ1qK"
    "2Rx8ZutCUg3PHb7Q"
)

_REQUEST_TIMEOUT = 10
_DOWNLOAD_TIMEOUT = 60
_FFMPEG_TIMEOUT_SEC = 120
_THUMB_MAX_BYTES = 190_000


@dataclass
class GoalVideoDelivery:
    """Local MP4 path and Telegram send_video hints (caller deletes path / thumb_path)."""

    path: str
    width: Optional[int] = None
    height: Optional[int] = None
    duration: Optional[int] = None
    thumb_path: Optional[str] = None


def _get_brightcove_clip_id(game_id: int, event_id: int) -> Optional[int]:
    """Fetch highlightClip (Brightcove video ID) from NHL Replay API."""
    url = f"{NHL_BASE}/v1/ppt-replay/{game_id}/{event_id}"
    try:
        resp = requests.get(url, timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException:
        logger.exception("NHL Replay API failed for game=%s event=%s", game_id, event_id)
        return None

    goal = resp.json().get("goal", {})
    clip_id = goal.get("highlightClip")
    if not clip_id:
        logger.warning("No highlightClip for game=%s event=%s", game_id, event_id)
    return clip_id


def _mp4_sources(sources: List[dict]) -> List[dict]:
    return [
        s
        for s in sources
        if s.get("container") == "MP4" and str(s.get("src", "")).startswith("https")
    ]


def _pick_best_mp4(mp4s: List[dict]) -> Optional[dict]:
    """Prefer H.264 over HEVC, then highest resolution."""

    if not mp4s:
        return None

    def sort_key(s: dict) -> tuple:
        codec = (s.get("codec") or "").upper()
        if "H265" in codec or "HEVC" in codec:
            tier = 1
        else:
            tier = 0
        w = int(s.get("width") or 0)
        h = int(s.get("height") or 0)
        return (tier, -(w * h))

    return min(mp4s, key=sort_key)


def _brightcove_rendition(clip_id: int) -> Optional[tuple]:
    """
    Resolve clip to (mp4_url, width, height, duration_seconds).

    duration_seconds from Brightcove video.duration (milliseconds).
    """
    url = f"https://edge.api.brightcove.com/playback/v1/accounts/{BC_ACCOUNT}/videos/{clip_id}"
    headers = {"Accept": f"application/json;pk={BC_POLICY_KEY}"}
    try:
        resp = requests.get(url, headers=headers, timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException:
        logger.exception("Brightcove API failed for clip=%s", clip_id)
        return None

    data = resp.json()
    mp4s = _mp4_sources(data.get("sources", []))
    best = _pick_best_mp4(mp4s)
    if not best:
        logger.warning("No MP4 sources for clip=%s", clip_id)
        return None

    duration_ms = data.get("duration")
    if duration_ms is not None:
        try:
            duration_sec = max(1, int(round(float(duration_ms) / 1000.0)))
        except (TypeError, ValueError):
            duration_sec = None
    else:
        duration_sec = None

    w = best.get("width")
    h = best.get("height")
    width = int(w) if w is not None else None
    height = int(h) if h is not None else None

    return (best["src"], width, height, duration_sec)


def _get_mp4_url(clip_id: int) -> Optional[str]:
    """Resolve Brightcove clip ID to a direct HTTPS MP4 URL."""
    r = _brightcove_rendition(clip_id)
    return r[0] if r else None


def get_goal_video_url(game_id: int, event_id: int) -> Optional[str]:
    """Resolve goal replay to a direct MP4 URL (no download)."""
    clip_id = _get_brightcove_clip_id(game_id, event_id)
    if clip_id is None:
        return None
    return _get_mp4_url(clip_id)


def _ffmpeg_bin() -> Optional[str]:
    return shutil.which("ffmpeg")


def _remux_faststart_inplace(path: str) -> None:
    """Move moov atom to file start (streaming-friendly); no re-encode."""
    ffmpeg = _ffmpeg_bin()
    if not ffmpeg:
        return
    fd, out = tempfile.mkstemp(suffix=".mp4")
    os.close(fd)
    try:
        proc = subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                path,
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                out,
            ],
            capture_output=True,
            timeout=_FFMPEG_TIMEOUT_SEC,
        )
        if proc.returncode != 0:
            logger.warning(
                "ffmpeg faststart failed: %s", (proc.stderr or b"").decode()[:200]
            )
            return
        os.replace(out, path)
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning("ffmpeg faststart error: %s", e)
    finally:
        if os.path.isfile(out):
            try:
                os.unlink(out)
            except OSError:
                pass


def _make_telegram_thumb(video_path: str) -> Optional[str]:
    """
    JPEG thumbnail ≤ ~200 KB, max side 320 (Telegram limits).
    """
    ffmpeg = _ffmpeg_bin()
    if not ffmpeg:
        return None
    thumb = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False).name
    for q in (4, 6, 8, 10):
        try:
            proc = subprocess.run(
                [
                    ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-ss",
                    "0.5",
                    "-i",
                    video_path,
                    "-frames:v",
                    "1",
                    "-vf",
                    "scale=320:-2",
                    "-q:v",
                    str(q),
                    thumb,
                ],
                capture_output=True,
                timeout=60,
            )
            if proc.returncode != 0:
                continue
            if os.path.isfile(thumb) and os.path.getsize(thumb) <= _THUMB_MAX_BYTES:
                return thumb
        except (OSError, subprocess.SubprocessError) as e:
            logger.warning("ffmpeg thumb error: %s", e)
            break
    try:
        os.unlink(thumb)
    except OSError:
        pass
    return None


def download_goal_video(game_id: int, event_id: int) -> Optional[GoalVideoDelivery]:
    """
    Download goal replay MP4 to a temp file.

    Applies faststart remux and optional JPEG thumb when ffmpeg is available —
    fixes Telegram inline preview (white bubble) for MP4 with moov at EOF.

    Returns GoalVideoDelivery on success; caller must delete path and thumb_path.

    Blocking (HTTP download + two ffmpeg passes, up to ~2 minutes): call it from
    async code via ``asyncio.to_thread`` so the bot's event loop keeps polling.
    """
    clip_id = _get_brightcove_clip_id(game_id, event_id)
    if clip_id is None:
        return None

    rend = _brightcove_rendition(clip_id)
    if rend is None:
        return None
    mp4_url, width, height, duration_sec = rend

    try:
        resp = requests.get(mp4_url, stream=True, timeout=_DOWNLOAD_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException:
        logger.exception("MP4 download failed for game=%s event=%s", game_id, event_id)
        return None

    tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
    try:
        for chunk in resp.iter_content(chunk_size=65536):
            tmp.write(chunk)
        tmp.close()
        path = tmp.name
    except Exception:
        logger.exception("Error writing video to temp file")
        tmp.close()
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        return None

    _remux_faststart_inplace(path)
    thumb_path = _make_telegram_thumb(path)

    return GoalVideoDelivery(
        path=path,
        width=width,
        height=height,
        duration=duration_sec,
        thumb_path=thumb_path,
    )
