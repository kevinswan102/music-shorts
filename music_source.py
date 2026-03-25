"""
Music Source — yt-dlp channel scanning + audio download
Scans the @official_stardrift YouTube channel, picks the next unprocessed
track, and downloads the audio as MP3.
"""

import os
import json
import subprocess
import glob
import logging
from typing import Optional, Dict, List

logger = logging.getLogger(__name__)

ARCHIVE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "archive.txt")
CHANNEL_URL = os.getenv(
    "STARDRIFT_CHANNEL_URL",
    "https://www.youtube.com/@official_stardrift",
)
COOKIES_FILE = os.getenv("YTDLP_COOKIES", "")


def _cookies_args() -> List[str]:
    """Return ['--cookies', path] if a cookies file is set and exists."""
    if COOKIES_FILE and os.path.exists(COOKIES_FILE):
        return ["--cookies", COOKIES_FILE]
    return []


def load_archive() -> set:
    """Load set of already-processed YouTube video IDs from archive.txt."""
    if not os.path.exists(ARCHIVE_FILE):
        return set()
    with open(ARCHIVE_FILE, "r") as f:
        return {line.strip() for line in f if line.strip()}


def save_to_archive(video_id: str) -> None:
    """Append a video ID to archive.txt."""
    with open(ARCHIVE_FILE, "a") as f:
        f.write(f"{video_id}\n")


def list_channel_videos() -> List[Dict]:
    """
    Use yt-dlp --flat-playlist to get all video metadata from the channel.
    Returns list of dicts with keys: id, title, url, duration.
    Ordered newest-first (default yt-dlp order for channel/videos).
    """
    cmd = [
        "yt-dlp",
        "--flat-playlist",
        "--dump-json",
        "--no-warnings",
    ] + _cookies_args() + [
        f"{CHANNEL_URL}/videos",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        logger.error("yt-dlp channel scan timed out")
        return []

    if result.returncode != 0:
        logger.error(f"yt-dlp channel scan failed: {result.stderr[:500]}")
        return []

    videos = []
    for line in result.stdout.strip().split("\n"):
        if not line.strip():
            continue
        try:
            info = json.loads(line)
            vid_id = info.get("id")
            if not vid_id:
                continue
            videos.append({
                "id": vid_id,
                "title": info.get("title", "Untitled"),
                "url": info.get("url") or f"https://www.youtube.com/watch?v={vid_id}",
                "duration": info.get("duration"),
            })
        except json.JSONDecodeError:
            continue

    logger.info(f"Found {len(videos)} videos on channel")
    return videos


def pick_next_track(videos: List[Dict]) -> Optional[Dict]:
    """
    Pick the next unprocessed track. Iterates newest-first,
    skips anything already in archive.txt. Returns None if all processed.
    """
    archive = load_archive()
    for video in videos:
        if video["id"] not in archive:
            return video
    return None


def download_audio(video_url: str, output_dir: str = "/tmp") -> Optional[str]:
    """
    Download audio from a YouTube video as MP3 using yt-dlp.
    Returns path to downloaded MP3, or None on failure.
    """
    output_template = os.path.join(output_dir, "%(id)s.%(ext)s")
    cmd = [
        "yt-dlp",
        "-x",
        "--audio-format", "mp3",
        "--audio-quality", "192K",
        "-o", output_template,
        "--no-playlist",
    ] + _cookies_args() + [
        video_url,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        logger.error("Audio download timed out")
        return None

    if result.returncode != 0:
        logger.error(f"Audio download failed: {result.stderr[:500]}")
        return None

    # Find the downloaded MP3 (yt-dlp names it by video ID)
    matches = glob.glob(os.path.join(output_dir, "*.mp3"))
    if matches:
        return max(matches, key=os.path.getmtime)
    return None
