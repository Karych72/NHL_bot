"""
NHL Goal Replay Downloader — proof of concept.

Chain:
  1. /v1/ppt-replay/goal/{gameId}/{eventId} → JSON with highlightClip (Brightcove ID)
  2. Brightcove Playback API → list of sources (HLS, DASH, MP4)
  3. Pick HTTPS MP4 source → download

Usage:
  python3 test_goal_replay.py                     # auto-find today's game
  python3 test_goal_replay.py 2025021094 318      # specific game + event
"""

import requests
import json
import sys
import os
from pathlib import Path

NHL_BASE = "https://api-web.nhle.com"
BC_ACCOUNT = "6415718365001"
BC_POLICY_KEY = (
    "BCpkADawqM3l37Vq8trLJ95vVwxubXYZXYglAopEZXQTHTWX3YdalyF9xmkuknxjBgiMYwt8VZ_"
    "OZ1jAjYxz_yzuNh_cjC3uOaMspVTD-hZfNUHtNnBnhVD0Gmsih8TBF8QlQFXiCQM3W_u4ydJ1qK"
    "2Rx8ZutCUg3PHb7Q"
)


def get_recent_game_with_goals():
    """Find a recent completed game that has goals."""
    for endpoint in ["/v1/score/now", "/v1/score/2026-03-20"]:
        resp = requests.get(f"{NHL_BASE}{endpoint}")
        if resp.status_code != 200:
            continue
        for game in resp.json().get("games", []):
            if game.get("gameState") in ("FINAL", "OFF", "LIVE"):
                return game["id"], f"{game['awayTeam']['abbrev']}@{game['homeTeam']['abbrev']}"
    return None, None


def get_goals_from_game(game_id):
    """Extract goal events from play-by-play."""
    resp = requests.get(f"{NHL_BASE}/v1/gamecenter/{game_id}/play-by-play")
    resp.raise_for_status()
    return [
        play for play in resp.json().get("plays", [])
        if play.get("typeDescKey") == "goal"
    ]


def get_replay_info(game_id, event_id):
    """Get goal replay metadata including Brightcove clip ID."""
    resp = requests.get(f"{NHL_BASE}/v1/ppt-replay/{game_id}/{event_id}")
    resp.raise_for_status()
    return resp.json().get("goal", {})


def get_brightcove_mp4(clip_id):
    """
    Resolve Brightcove video ID → direct MP4 download URL.
    Returns (url, size_bytes) or (None, 0).
    """
    url = f"https://edge.api.brightcove.com/playback/v1/accounts/{BC_ACCOUNT}/videos/{clip_id}"
    headers = {"Accept": f"application/json;pk={BC_POLICY_KEY}"}
    resp = requests.get(url, headers=headers)
    if resp.status_code != 200:
        print(f"Brightcove API error {resp.status_code}: {resp.text[:200]}")
        return None, 0

    data = resp.json()
    sources = data.get("sources", [])

    mp4s = [
        s for s in sources
        if s.get("container") == "MP4" and s.get("src", "").startswith("https")
    ]
    if not mp4s:
        return None, 0

    best = max(mp4s, key=lambda s: s.get("width", 0))
    return best["src"], best.get("size", 0)


def download_video(url, filepath):
    """Stream-download video to file. Returns bytes downloaded."""
    resp = requests.get(url, stream=True)
    resp.raise_for_status()
    downloaded = 0
    with open(filepath, "wb") as f:
        for chunk in resp.iter_content(chunk_size=65536):
            f.write(chunk)
            downloaded += len(chunk)
    return downloaded


def main():
    if len(sys.argv) >= 3:
        game_id, event_id = int(sys.argv[1]), int(sys.argv[2])
        label = f"game {game_id}"
    else:
        print("Looking for a recent game...")
        game_id, label = get_recent_game_with_goals()
        if not game_id:
            print("No recent games found.")
            sys.exit(1)
        print(f"Found: {game_id} ({label})")

        goals = get_goals_from_game(game_id)
        if not goals:
            print("No goals in this game.")
            sys.exit(1)
        print(f"Goals in game: {len(goals)}")

        event_id = goals[0]["eventId"]

    # Step 1: Get replay metadata
    print(f"\n--- Step 1: Replay metadata (game={game_id}, event={event_id}) ---")
    goal = get_replay_info(game_id, event_id)
    clip_id = goal.get("highlightClip")
    scorer = goal.get("name", {}).get("default", "unknown")
    sharing_url = goal.get("highlightClipSharingUrl", "N/A")

    print(f"Scorer: {scorer}")
    print(f"Brightcove clip ID: {clip_id}")
    print(f"Sharing URL: {sharing_url}")

    if not clip_id:
        print("No highlightClip — video not yet available for this goal.")
        sys.exit(0)

    # Step 2: Resolve MP4 via Brightcove
    print(f"\n--- Step 2: Brightcove → MP4 URL ---")
    mp4_url, size = get_brightcove_mp4(clip_id)
    if not mp4_url:
        print("No MP4 source available.")
        sys.exit(1)
    print(f"MP4 URL: {mp4_url[:120]}...")
    print(f"Size: {size / (1024*1024):.1f} MB")

    # Step 3: Download
    out_dir = Path("downloads")
    out_dir.mkdir(exist_ok=True)
    filename = out_dir / f"goal_{game_id}_{event_id}_{scorer.replace(' ', '_')}.mp4"

    print(f"\n--- Step 3: Downloading → {filename} ---")
    downloaded = download_video(mp4_url, filename)
    print(f"Done: {downloaded / (1024*1024):.1f} MB")

    # Summary for Telegram integration
    print(f"\n{'='*60}")
    print(f"SUMMARY — what Telegram bot needs:")
    print(f"  1. Call get_replay_info(game_id, event_id)")
    print(f"  2. Call get_brightcove_mp4(highlightClip)")
    print(f"  3. Send MP4 via bot.send_video(url) or download + send")
    print(f"  4. Video size: ~{size/(1024*1024):.0f} MB (Telegram limit: 50 MB)")
    print(f"  5. Telegram also supports send_video(url) — direct URL may work")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
