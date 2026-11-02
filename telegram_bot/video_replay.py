"""
NHL Goal Video Replay service.

Chain: game_id + event_id → NHL Replay API → Brightcove → MP4 file.
Подпись к видео (автор, передачи, номер гола в сезоне, время) — из того же ответа Replay API.
Кэш загруженных клипов (Задача 87): ``file_id`` Telegram и подпись по голу в ``bot_goal_clips`` —
клип скачивается и грузится один раз, дальше рассылка и кнопка видео шлют его по ``file_id``.
"""

from __future__ import annotations

import html
import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from typing import List, Optional, Tuple

import requests

from bot_messages import team_name_for_abbrev
from database import fetch_all, get_connection

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
    """Local MP4 path, Telegram send_video hints and HTML caption (``goal_caption``);
    caller deletes path / thumb_path."""

    path: str
    caption: str
    width: Optional[int] = None
    height: Optional[int] = None
    duration: Optional[int] = None
    thumb_path: Optional[str] = None


# Пометки гола в подписи: ``strength`` и ``goalModifier`` Replay API.
_STRENGTH_RU = {"pp": "в большинстве", "sh": "в меньшинстве"}
_MODIFIER_RU = {"empty-net": "в пустые ворота"}


def _get_replay(game_id: int, event_id: int) -> Optional[dict]:
    """Ответ NHL Replay API по голу; ``None`` — запрос не удался или в ответе нет клипа."""
    url = f"{NHL_BASE}/v1/ppt-replay/{game_id}/{event_id}"
    try:
        resp = requests.get(url, timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException:
        logger.exception("NHL Replay API failed for game=%s event=%s", game_id, event_id)
        return None

    replay = resp.json()
    if not replay.get("goal", {}).get("highlightClip"):
        logger.warning("No highlightClip for game=%s event=%s", game_id, event_id)
        return None
    return replay


def _period_ru(descriptor: dict) -> str:
    """Период гола по-русски: «2-й период» или «овертайм»."""
    if descriptor["periodType"] == "OT":
        return "овертайм"
    return f"{descriptor['number']}-й период"


def goal_caption(replay: dict) -> str:
    """HTML-подпись к видео гола: автор и его номер гола в сезоне, передачи
    с числом передач в сезоне, период, время, счёт после гола и пометки
    (большинство, меньшинство, пустые ворота). Гол в серии буллитов — одна строка:
    у него в API нет номера гола в сезоне, передач и времени.

    Зачем: видео из кнопки карточки приходило без подписи — по клипу не понять,
    чей гол.

    Команды — полным названием из ``teams`` по коду из ответа (Задача 98).

    Аргументы: ``replay`` — ответ ``/v1/ppt-replay/{game_id}/{event_id}``.
    """
    def team(abbrev: str) -> str:
        return html.escape(team_name_for_abbrev(abbrev))

    goal = replay["goal"]
    away, home = team(replay["awayTeam"]["abbrev"]), team(replay["homeTeam"]["abbrev"])
    scorer = (
        f"🚨 <b>{html.escape(goal['name']['default'])}</b> ({team(goal['teamAbbrev']['default'])})"
    )
    if goal["periodDescriptor"]["periodType"] == "SO":
        return f"{scorer} — гол в серии буллитов, {away} – {home}"
    lines = [f"{scorer} — {goal['goalsToDate']}-й гол в сезоне"]
    assists = [
        f"{html.escape(a['name']['default'])} ({a['assistsToDate']})" for a in goal["assists"]
    ]
    lines.append("Передачи: " + ", ".join(assists) if assists else "Без передач")
    details = [
        f"{_period_ru(goal['periodDescriptor'])}, {goal['timeInPeriod']}",
        f"{away} {goal['awayScore']}:{goal['homeScore']} {home}",
    ]
    for mark in (_STRENGTH_RU.get(goal["strength"]), _MODIFIER_RU.get(goal["goalModifier"])):
        if mark:
            details.append(mark)
    lines.append(" · ".join(details))
    return "\n".join(lines)


def goal_clip_published(game_id: int, event_id: int) -> bool:
    """Выложила ли NHL клип гола (`highlightClip` в Replay API) — без скачивания.

    Зачем: подписка на страну ждёт клипы матча перед отправкой (Задача 76); через
    полчаса после матча их может ещё не быть.

    Аргументы: ``game_id``, ``event_id`` — гол.
    """
    return _get_replay(game_id, event_id) is not None


def cached_goal_clip(game_id: int, event_id: int) -> Optional[Tuple[str, str]]:
    """Загруженный ранее клип гола: (``file_id`` Telegram, HTML-подпись) или ``None``.

    Зачем: рассылка и кнопка видео шлют клип по ``file_id`` — без скачивания с NHL,
    ffmpeg и повторной загрузки (Задача 87).

    Аргументы: ``game_id``, ``event_id`` — гол.
    """
    row = fetch_all(
        "SELECT file_id, caption FROM bot_goal_clips WHERE game_id = %s AND event_id = %s",
        (game_id, event_id),
        columns=["file_id", "caption"],
    )
    if not row["count_rows"]:
        return None
    return row["file_id"][0], row["caption"][0]


def save_goal_clip(game_id: int, event_id: int, file_id: str, caption: str) -> None:
    """Запоминает ``file_id`` клипа, только что загруженного в Telegram, и его подпись.

    Зачем: см. ``cached_goal_clip``. Повторная загрузка (Telegram отверг протухший
    ``file_id``) перезаписывает строку.

    Аргументы: ``game_id``, ``event_id`` — гол; ``file_id`` — из ответа на отправку видео;
    ``caption`` — ``GoalVideoDelivery.caption``.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO bot_goal_clips (game_id, event_id, file_id, caption) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (game_id, event_id) DO UPDATE "
                "SET file_id = EXCLUDED.file_id, caption = EXCLUDED.caption, uploaded_at = now()",
                (game_id, event_id, file_id, caption),
            )
        conn.commit()


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


def _ffmpeg_bin() -> str:
    """Path to the ffmpeg binary; raises RuntimeError when it is not in PATH."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError(
            "ffmpeg not found in PATH: it is required for the faststart remux "
            "and thumbnail of goal videos (installed in the Docker image)"
        )
    return ffmpeg


def _remux_faststart_inplace(path: str, ffmpeg: str) -> None:
    """Move moov atom to file start (streaming-friendly); no re-encode."""
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


def _make_telegram_thumb(video_path: str, ffmpeg: str) -> Optional[str]:
    """
    JPEG thumbnail ≤ ~200 KB, max side 320 (Telegram limits).
    """
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

    Applies faststart remux and a JPEG thumb (ffmpeg) — fixes Telegram inline
    preview (white bubble) for MP4 with moov at EOF.

    Raises RuntimeError, before any network request, if ffmpeg is not in PATH.
    Returns GoalVideoDelivery on success; caller must delete path and thumb_path.

    Blocking (HTTP download + two ffmpeg passes, up to ~2 minutes): call it from
    async code via ``asyncio.to_thread`` so the bot's event loop keeps polling.
    """
    ffmpeg = _ffmpeg_bin()

    replay = _get_replay(game_id, event_id)
    if replay is None:
        return None
    # До записи файлов: ошибка в подписи не должна оставлять временные mp4.
    caption = goal_caption(replay)

    rend = _brightcove_rendition(replay["goal"]["highlightClip"])
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

    _remux_faststart_inplace(path, ffmpeg)
    thumb_path = _make_telegram_thumb(path, ffmpeg)

    return GoalVideoDelivery(
        path=path,
        width=width,
        height=height,
        duration=duration_sec,
        thumb_path=thumb_path,
        caption=caption,
    )
