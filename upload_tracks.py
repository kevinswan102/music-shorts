#!/usr/bin/env python3
"""
upload_tracks.py — Local-only script to download tracks and upload to GitHub Releases.

Run this locally whenever you add new tracks to the source channel.
Downloads audio via yt-dlp (works from home IP) and uploads as GitHub Release assets.

Usage:
    python3 upload_tracks.py

Requires: yt-dlp, gh (GitHub CLI), both installed locally.
"""

import os
import sys
import json
import subprocess
import glob
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("upload_tracks")

CHANNEL_URL = os.getenv("SOURCE_CHANNEL_URL", "")
REPO = os.getenv("GITHUB_REPOSITORY", "kevinswan102/music-shorts")
RELEASE_TAG = "audio-tracks"
DOWNLOAD_DIR = "/tmp/music_tracks"


def list_channel_videos():
    """Get all video IDs and titles from the channel (Releases + Videos tabs)."""
    seen_ids = set()
    videos = []

    def _parse_lines(stdout):
        results = []
        for line in stdout.strip().split("\n"):
            if not line.strip():
                continue
            try:
                info = json.loads(line)
                vid_id = info.get("id")
                if vid_id:
                    results.append({"id": vid_id, "title": info.get("title", "Untitled")})
            except json.JSONDecodeError:
                continue
        return results

    # 1. Scan Releases tab (albums) and expand each into individual tracks
    releases_cmd = ["yt-dlp", "--flat-playlist", "--dump-json", "--no-warnings", f"{CHANNEL_URL}/releases"]
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
                logger.info(f"  Expanding album: {album.get('title', '?')}")
                try:
                    album_result = subprocess.run(
                        ["yt-dlp", "--flat-playlist", "--dump-json", "--no-warnings", album_url],
                        capture_output=True, text=True, timeout=60,
                    )
                    if album_result.returncode == 0:
                        for track in _parse_lines(album_result.stdout):
                            if track["id"] not in seen_ids:
                                seen_ids.add(track["id"])
                                videos.append(track)
                except subprocess.TimeoutExpired:
                    logger.warning(f"  Timeout expanding album: {album.get('title', '?')}")
    except subprocess.TimeoutExpired:
        logger.warning("Releases tab scan timed out")

    return videos


def get_existing_assets():
    """Get list of already-uploaded asset filenames from the GitHub Release."""
    try:
        result = subprocess.run(
            ["gh", "release", "view", RELEASE_TAG, "--repo", REPO, "--json", "assets", "-q", ".assets[].name"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return set()
        return {name.strip() for name in result.stdout.strip().split("\n") if name.strip()}
    except Exception:
        return set()


def ensure_release_exists():
    """Create the GitHub Release if it doesn't exist."""
    result = subprocess.run(
        ["gh", "release", "view", RELEASE_TAG, "--repo", REPO],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        logger.info(f"Creating release '{RELEASE_TAG}'...")
        subprocess.run(
            ["gh", "release", "create", RELEASE_TAG, "--repo", REPO,
             "--title", "Audio Tracks", "--notes", "MP3 audio tracks for video generation"],
            check=True,
        )
        logger.info("Release created.")


def download_track(video_id: str) -> str:
    """Download audio for a single video. Returns MP3 path."""
    output_path = os.path.join(DOWNLOAD_DIR, f"{video_id}.mp3")
    if os.path.exists(output_path):
        return output_path

    cmd = [
        "yt-dlp", "-x",
        "--audio-format", "mp3",
        "--audio-quality", "192K",
        "-o", os.path.join(DOWNLOAD_DIR, "%(id)s.%(ext)s"),
        "--no-playlist",
        f"https://www.youtube.com/watch?v={video_id}",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        logger.error(f"Download failed for {video_id}: {result.stderr[:300]}")
        return ""
    return output_path if os.path.exists(output_path) else ""


def upload_asset(filepath: str):
    """Upload a file as a release asset."""
    filename = os.path.basename(filepath)
    logger.info(f"Uploading {filename}...")
    subprocess.run(
        ["gh", "release", "upload", RELEASE_TAG, filepath, "--repo", REPO, "--clobber"],
        check=True, capture_output=True,
    )
    logger.info(f"Uploaded: {filename}")


def main():
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    logger.info("Scanning channel...")
    videos = list_channel_videos()
    if not videos:
        logger.error("No videos found")
        sys.exit(1)
    logger.info(f"Found {len(videos)} videos")

    ensure_release_exists()
    existing = get_existing_assets()
    logger.info(f"Already uploaded: {len(existing)} tracks")

    new_count = 0
    for video in videos:
        filename = f"{video['id']}.mp3"
        if filename in existing:
            logger.info(f"  Skip (exists): {video['title']}")
            continue

        logger.info(f"  Downloading: {video['title']} ({video['id']})")
        mp3_path = download_track(video["id"])
        if not mp3_path:
            logger.warning(f"  Failed to download {video['id']}, skipping")
            continue

        upload_asset(mp3_path)
        new_count += 1

    logger.info(f"\nDone! Uploaded {new_count} new tracks.")
    if new_count == 0:
        logger.info("All tracks already uploaded.")


if __name__ == "__main__":
    main()
