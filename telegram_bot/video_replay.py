"""
NHL Goal Video Replay service.

Chain: game_id + event_id → NHL Replay API → Brightcove → MP4 file.
"""

import logging
import tempfile
from typing import Optional

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


def _get_mp4_url(clip_id: int) -> Optional[str]:
    """Resolve Brightcove clip ID to a direct HTTPS MP4 URL."""
    url = f"https://edge.api.brightcove.com/playback/v1/accounts/{BC_ACCOUNT}/videos/{clip_id}"
    headers = {"Accept": f"application/json;pk={BC_POLICY_KEY}"}
    try:
        resp = requests.get(url, headers=headers, timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException:
        logger.exception("Brightcove API failed for clip=%s", clip_id)
        return None

    sources = resp.json().get("sources", [])
    mp4s = [
        s for s in sources
        if s.get("container") == "MP4" and s.get("src", "").startswith("https")
    ]
    if not mp4s:
        logger.warning("No MP4 sources for clip=%s", clip_id)
        return None

    return max(mp4s, key=lambda s: s.get("width", 0))["src"]


def get_goal_video_url(game_id: int, event_id: int) -> Optional[str]:
    """Resolve goal replay to a direct MP4 URL (no download)."""
    clip_id = _get_brightcove_clip_id(game_id, event_id)
    if clip_id is None:
        return None
    return _get_mp4_url(clip_id)


def download_goal_video(game_id: int, event_id: int) -> Optional[str]:
    """
    Download goal replay MP4 to a temp file.

    Returns the file path on success, None on failure.
    Caller is responsible for deleting the file after use.
    """
    clip_id = _get_brightcove_clip_id(game_id, event_id)
    if clip_id is None:
        return None

    mp4_url = _get_mp4_url(clip_id)
    if mp4_url is None:
        return None

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
        return tmp.name
    except Exception:
        logger.exception("Error writing video to temp file")
        tmp.close()
        return None
