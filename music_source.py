"""
Music Source — channel scanning + audio download from GitHub Releases.

The flat-playlist scan (metadata only) works from anywhere via yt-dlp.
Audio files are pre-uploaded to a GitHub Release by running upload_tracks.py locally.
The workflow downloads the MP3 from the release asset URL.
"""

import os
import json
import subprocess
import logging
import requests
from typing import Optional, Dict, List

logger = logging.getLogger(__name__)

ARCHIVE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "archive.txt")
CHANNEL_URL = os.getenv("SOURCE_CHANNEL_URL", "")
REPO = os.getenv("GITHUB_REPOSITORY", "kevinswan102/music-shorts")
RELEASE_TAG = "audio-tracks"


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


def _parse_flat_playlist(stdout: str) -> List[Dict]:
    """Parse yt-dlp --flat-playlist --dump-json output into video dicts."""
    videos = []
    for line in stdout.strip().split("\n"):
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
    return videos


def list_channel_videos() -> List[Dict]:
    """
    Scan the channel's Releases tab (albums only, no Videos/Shorts).
    Expands each album playlist into individual tracks.
    Flat-playlist only fetches IDs/titles (no actual download), works from CI.
    """
    seen_ids: set = set()
    all_videos: List[Dict] = []

    # --- 1. Scan Releases tab (albums) and expand each into tracks ---
    releases_cmd = [
        "yt-dlp", "--flat-playlist", "--dump-json", "--no-warnings",
        f"{CHANNEL_URL}/releases",
    ]
    try:
        result = subprocess.run(releases_cmd, capture_output=True, text=True, timeout=120)
        if result.returncode == 0:
            for line in result.stdout.strip().split("\n"):
                if not line.strip():
                    continue
                try:
                    album = json.loads(line)
                except json.JSONDecodeError:
                    continue
                album_url = album.get("url") or album.get("webpage_url", "")
                if not album_url:
                    continue
                # Expand album playlist into individual tracks
                try:
                    album_result = subprocess.run(
                        ["yt-dlp", "--flat-playlist", "--dump-json", "--no-warnings", album_url],
                        capture_output=True, text=True, timeout=60,
                    )
                    if album_result.returncode == 0:
                        for track in _parse_flat_playlist(album_result.stdout):
                            if track["id"] not in seen_ids:
                                seen_ids.add(track["id"])
                                all_videos.append(track)
                except subprocess.TimeoutExpired:
                    logger.warning(f"Timeout expanding album: {album.get('title', '?')}")
        else:
            logger.warning(f"Releases tab scan failed: {result.stderr[:300]}")
    except subprocess.TimeoutExpired:
        logger.warning("Releases tab scan timed out")

    logger.info(f"Found {len(all_videos)} unique tracks from Releases")
    return all_videos


def _get_release_assets() -> Dict[str, str]:
    """
    Fetch the list of assets from the GitHub Release.
    Returns dict of {filename: download_url}.
    """
    api_url = f"https://api.github.com/repos/{REPO}/releases/tags/{RELEASE_TAG}"
    try:
        resp = requests.get(api_url, timeout=15, headers={"Accept": "application/vnd.github+json"})
        if resp.status_code != 200:
            logger.warning(f"GitHub release API returned {resp.status_code}")
            return {}
        assets = resp.json().get("assets", [])
        return {a["name"]: a["browser_download_url"] for a in assets}
    except Exception as e:
        logger.error(f"Failed to fetch release assets: {e}")
        return {}


def pick_next_track(videos: List[Dict]) -> Optional[Dict]:
    """
    Pick the next unprocessed track that also has an uploaded audio file.
    Iterates newest-first, skips archived, skips tracks without audio.
    """
    archive = load_archive()
    assets = _get_release_assets()
    available_ids = {name.replace(".mp3", "") for name in assets.keys()}

    for video in videos:
        if video["id"] not in archive and video["id"] in available_ids:
            return video

    # Log why nothing was found
    unprocessed = [v for v in videos if v["id"] not in archive]
    if not unprocessed:
        logger.info("All tracks have been processed.")
    elif not available_ids:
        logger.warning("No audio files in GitHub Release. Run upload_tracks.py locally first.")
    else:
        missing = [v["id"] for v in unprocessed if v["id"] not in available_ids]
        if missing:
            logger.warning(f"Tracks missing audio upload: {missing[:5]}")

    return None


def download_audio(video_url: str, output_dir: str = "/tmp") -> Optional[str]:
    """
    Download audio MP3 from GitHub Release assets (not YouTube).
    Falls back to yt-dlp if release asset not found.
    """
    video_id = video_url.split("v=")[-1].split("&")[0] if "v=" in video_url else ""
    if not video_id:
        logger.error("Could not extract video ID from URL")
        return None

    # Primary: download from GitHub Release
    assets = _get_release_assets()
    filename = f"{video_id}.mp3"
    download_url = assets.get(filename)

    if download_url:
        output_path = os.path.join(output_dir, filename)
        try:
            logger.info(f"Downloading audio from GitHub Release: {filename}")
            resp = requests.get(download_url, stream=True, timeout=120)
            resp.raise_for_status()
            with open(output_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
            logger.info(f"Audio downloaded: {output_path} ({os.path.getsize(output_path) / 1024:.0f} KB)")
            return output_path
        except Exception as e:
            logger.error(f"GitHub Release download failed: {e}")

    # Fallback: try yt-dlp directly (works locally, fails on CI)
    logger.warning(f"No release asset for {video_id}, trying yt-dlp directly...")
    output_template = os.path.join(output_dir, "%(id)s.%(ext)s")
    cmd = [
        "yt-dlp", "-x",
        "--audio-format", "mp3",
        "--audio-quality", "192K",
        "-o", output_template,
        "--no-playlist",
        video_url,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode == 0:
            import glob
            matches = glob.glob(os.path.join(output_dir, "*.mp3"))
            if matches:
                return max(matches, key=os.path.getmtime)
    except subprocess.TimeoutExpired:
        pass

    logger.error(f"All download methods failed for {video_id}")
    return None
