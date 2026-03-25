"""
Footage Fetcher — Pexels API search + download
Searches for vertical stock footage clips matching the track's vibe,
downloads them, and returns local file paths.
"""

import os
import time
import random
import logging
import requests
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)

PEXELS_API_KEY = os.getenv("PEXELS_API_KEY", "")
PEXELS_BASE_URL = "https://api.pexels.com/videos"

# Genre -> Pexels search keywords
GENRE_KEYWORDS = {
    "electronic": [
        "neon city night", "futuristic tunnel", "laser lights",
        "abstract lights", "cyberpunk city", "led lights",
        "neon signs", "night drive city",
    ],
    "chill": [
        "rain window", "ocean sunset", "forest fog",
        "clouds timelapse", "water reflection", "candle flame",
        "snow falling", "starry sky",
    ],
    "lofi": [
        "rain window", "cozy room", "coffee steam",
        "vinyl record", "sunset city", "book reading",
        "train window", "autumn leaves",
    ],
    "phonk": [
        "dark urban", "smoke particles", "car drift",
        "motorcycle night", "boxing training", "dark alley",
        "gym workout", "race car",
    ],
    "ambient": [
        "underwater", "aurora borealis", "space nebula",
        "deep ocean", "crystal cave", "fog forest",
        "ice landscape", "light rays",
    ],
    "default": [
        "abstract motion", "particle effects", "light streaks",
        "smoke dark", "water surface", "geometric shapes",
        "neon abstract", "bokeh lights",
    ],
}

TARGET_CLIPS = 12


def classify_genre(track_title: str) -> str:
    """Simple keyword-based genre classification from track title."""
    title_lower = track_title.lower()
    hints = {
        "phonk": ["phonk", "drift", "cowbell", "memphis", "dark"],
        "chill": ["chill", "relax", "calm", "peaceful", "dreamy", "soft"],
        "lofi": ["lofi", "lo-fi", "lo fi", "study", "beats"],
        "ambient": ["ambient", "space", "ethereal", "atmospheric", "cosmic"],
        "electronic": [
            "electronic", "synth", "bass", "drop", "edm",
            "techno", "house", "trance", "dubstep", "pulse",
            "neon", "digital", "cyber",
        ],
    }
    for genre, keywords in hints.items():
        if any(kw in title_lower for kw in keywords):
            return genre
    return "default"


def classify_genre_llm(track_title: str) -> str:
    """Use LLM to classify genre. Falls back to keyword-based."""
    try:
        from llm_client import get_llm_client, llm_available
        if not llm_available():
            return classify_genre(track_title)

        client, model = get_llm_client()
        genres = list(GENRE_KEYWORDS.keys())
        resp = client.chat.completions.create(
            model=model,
            messages=[{
                "role": "user",
                "content": (
                    f"Classify this music track title into exactly one genre "
                    f"from this list: {genres}. "
                    f"Track title: \"{track_title}\"\n"
                    f"Reply with ONLY the genre name, nothing else."
                ),
            }],
            max_tokens=20,
            temperature=0.0,
        )
        genre = resp.choices[0].message.content.strip().lower()
        if genre in GENRE_KEYWORDS:
            return genre
        return classify_genre(track_title)
    except Exception as e:
        logger.warning(f"LLM genre classification failed: {e}")
        return classify_genre(track_title)


def search_videos(query: str, per_page: int = 10,
                   orientation: str = "portrait") -> List[Dict]:
    """Search Pexels for videos matching query."""
    if not PEXELS_API_KEY:
        logger.error("PEXELS_API_KEY not set")
        return []

    headers = {"Authorization": PEXELS_API_KEY}
    params = {
        "query": query,
        "per_page": per_page,
        "orientation": orientation,
    }
    try:
        resp = requests.get(
            f"{PEXELS_BASE_URL}/search",
            headers=headers, params=params, timeout=30,
        )
        resp.raise_for_status()
        return resp.json().get("videos", [])
    except requests.RequestException as e:
        logger.error(f"Pexels search failed for '{query}': {e}")
        return []


def download_video(video_info: Dict, output_dir: str = "/tmp",
                    prefer_height: int = 1920) -> Optional[str]:
    """Download a Pexels video file. Prefers file closest to prefer_height."""
    files = video_info.get("video_files", [])
    if not files:
        return None

    best = None
    best_score = float("inf")
    for f in files:
        h = f.get("height", 0)
        w = f.get("width", 0)
        orient_bonus = 0 if h > w else 1000
        score = abs(h - prefer_height) + orient_bonus
        if score < best_score:
            best_score = score
            best = f

    if not best or not best.get("link"):
        return None

    url = best["link"]
    file_id = video_info.get("id", "unknown")
    ext = url.split("?")[0].split(".")[-1] or "mp4"
    output_path = os.path.join(output_dir, f"pexels_{file_id}.{ext}")

    try:
        resp = requests.get(url, stream=True, timeout=60)
        resp.raise_for_status()
        with open(output_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
        logger.info(f"Downloaded Pexels clip: {output_path}")
        return output_path
    except Exception as e:
        logger.error(f"Download failed for Pexels clip {file_id}: {e}")
        return None


def fetch_footage(track_title: str, num_clips: int = TARGET_CLIPS,
                   output_dir: str = "/tmp") -> List[str]:
    """
    High-level: classify genre, search Pexels, download clips.
    Returns list of local file paths.
    """
    genre = classify_genre_llm(track_title)
    keywords = GENRE_KEYWORDS.get(genre, GENRE_KEYWORDS["default"])
    logger.info(f"Genre: {genre}, keywords: {keywords[:3]}")

    downloaded = []
    used_ids = set()

    for keyword in keywords:
        if len(downloaded) >= num_clips:
            break

        time.sleep(0.5)  # respect rate limits

        videos = search_videos(keyword, per_page=10, orientation="portrait")
        random.shuffle(videos)

        for video in videos:
            if len(downloaded) >= num_clips:
                break
            vid_id = video.get("id")
            if vid_id in used_ids:
                continue
            used_ids.add(vid_id)

            path = download_video(video, output_dir=output_dir)
            if path:
                downloaded.append(path)

    if not downloaded:
        logger.warning("No footage found, trying fallback search")
        videos = search_videos("abstract motion", per_page=10)
        for video in videos[:num_clips]:
            path = download_video(video, output_dir=output_dir)
            if path:
                downloaded.append(path)

    logger.info(f"Fetched {len(downloaded)} footage clips")
    return downloaded
