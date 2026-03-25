#!/usr/bin/env python3
"""
generate_short.py — Main entry point for music-shorts pipeline.

1. Scan source channel for tracks
2. Pick next unprocessed track
3. Download audio, analyze beats, find best 30s section
4. Fetch Pexels stock footage matching the track vibe
5. Render beat-synced video with color grading + text overlay
6. Generate LLM description + tags
7. Upload to YouTube
8. Update archive.txt
"""

import os
import sys
import logging
import gc
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("generate_short")

from music_source import list_channel_videos, pick_next_track, download_audio, save_to_archive
from beat_analyzer import analyze_track, extract_audio_segment, get_beat_intervals
from footage_fetcher import fetch_footage
from video_renderer import render_short


import re

ARTIST_NAME = os.getenv("ARTIST_NAME", "Unknown Artist")


def clean_song_title(raw_title: str) -> str:
    """
    Strip artist prefix, suffixes like '(Official Visualizer)', and common clutter.
    'Star Drift - Star Gazing (Synthwave Visualizer)' → 'Star Gazing'
    '[FREE] Mac Miller x Jaden Type Beat | "Summer Love"' → 'Summer Love'
    """
    title = raw_title.strip()
    # Strip "Artist - " prefix (anything before first " - ")
    if " - " in title:
        title = title.split(" - ", 1)[1].strip()
    # Strip parenthetical suffixes like (Official Visualizer), (Slowed), (Sped Up)
    title = re.sub(r'\s*\((?:Official\s+)?(?:Visualizer|Audio|Video|Lyric(?:s)?|Music\s+Video)(?:\s+\w+)?\)\s*$', '', title, flags=re.IGNORECASE)
    # Strip [FREE], [FREE FOR PROFIT], [FREE DOWNLOAD] prefixes
    title = re.sub(r'^\[(?:FREE(?:\s+(?:FOR\s+PROFIT|DOWNLOAD))?)\]\s*', '', title, flags=re.IGNORECASE)
    # Strip "No Copyright Song:" type prefixes
    title = re.sub(r'^(?:No\s+Copyright\s+Song:\s*)', '', title, flags=re.IGNORECASE)
    # Strip "Type Beat" suffixes and everything before the pipe/dash
    if " Type Beat" in title:
        # "[FREE] Future Type Beat - Royal Payne" → "Royal Payne"
        for sep in [' - ', ' | ']:
            if sep in title:
                title = title.split(sep)[-1].strip()
                break
        title = re.sub(r'\s*Type\s+Beat.*$', '', title, flags=re.IGNORECASE)
    # Strip surrounding quotes
    title = title.strip('"\'')
    return title.strip() or raw_title.strip()


def generate_description(track_name: str, genre: str) -> str:
    """Use Groq LLM to generate a 2-3 sentence description of the track vibe."""
    try:
        from llm_client import get_llm_client, llm_available
        if not llm_available():
            return f'"{track_name}" by {ARTIST_NAME}.'

        client, model = get_llm_client()
        resp = client.chat.completions.create(
            model=model,
            messages=[{
                "role": "user",
                "content": (
                    f"Write a 2-3 sentence YouTube Shorts description for a music "
                    f"visualizer video. Track: \"{track_name}\", Genre: {genre}, "
                    f"Artist: {ARTIST_NAME}. Keep it hype and engaging. "
                    f"Do NOT include hashtags. Do NOT say 'subscribe'."
                ),
            }],
            max_tokens=150,
            temperature=0.7,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        logger.warning(f"LLM description generation failed: {e}")
        return f'"{track_name}" by {ARTIST_NAME}.'


def main():
    logger.info("=" * 60)
    logger.info("MUSIC SHORTS GENERATOR")
    logger.info("=" * 60)

    # Step 1: Scan channel
    logger.info("Step 1: Scanning source channel...")
    videos = list_channel_videos()
    if not videos:
        logger.error("No videos found on channel. Exiting.")
        sys.exit(0)
    logger.info(f"Found {len(videos)} videos on channel")

    # Step 2: Pick next track
    logger.info("Step 2: Picking next unprocessed track...")
    track = pick_next_track(videos)
    if not track:
        logger.info("All tracks have been processed. Nothing to do.")
        sys.exit(0)
    raw_title = track["title"]
    song_title = clean_song_title(raw_title)
    logger.info(f"Selected: {raw_title} → display as: {song_title} (ID: {track['id']})")

    # Step 3: Download audio
    logger.info("Step 3: Downloading audio...")
    audio_path = download_audio(track["url"])
    if not audio_path:
        logger.error("Failed to download audio. Exiting.")
        sys.exit(1)
    logger.info(f"Audio downloaded: {audio_path}")

    # Step 4: Analyze beats
    logger.info("Step 4: Analyzing beats and finding best section...")
    analysis = analyze_track(audio_path)
    logger.info(
        f"BPM: {analysis['bpm']:.1f}, "
        f"Best window: {analysis['best_start']:.1f}s - {analysis['best_end']:.1f}s"
    )

    segment_path = "/tmp/audio_segment.wav"
    extract_audio_segment(
        audio_path, analysis["best_start"], analysis["best_end"], segment_path
    )

    segment_duration = analysis["best_end"] - analysis["best_start"]
    beat_intervals = get_beat_intervals(
        analysis["beat_times"],
        start_offset=analysis["best_start"],
        segment_duration=segment_duration,
    )
    logger.info(f"Beat intervals: {len(beat_intervals)} cuts")

    # Step 5: Classify genre (with audio analysis) + fetch footage
    bpm = analysis["bpm"]
    energy = analysis.get("energy", "")
    brightness = analysis.get("brightness", "")
    texture = analysis.get("texture", "")
    from footage_fetcher import classify_genre_llm
    genre = classify_genre_llm(song_title, bpm=bpm, energy=energy,
                                brightness=brightness, texture=texture)
    logger.info(f"Genre: {genre} (BPM: {bpm:.0f}, {energy}/{brightness}/{texture})")

    logger.info("Step 5: Fetching footage...")
    footage_paths = fetch_footage(song_title, bpm=bpm, energy=energy,
                                   brightness=brightness, texture=texture)
    if not footage_paths:
        logger.error("No footage fetched. Exiting.")
        sys.exit(1)
    logger.info(f"Fetched {len(footage_paths)} footage clips")

    # Step 6: Render video
    logger.info("Step 6: Rendering beat-synced video...")
    final_video = render_short(
        audio_segment_path=segment_path,
        footage_paths=footage_paths,
        beat_intervals=beat_intervals,
        track_name=song_title,
        artist=ARTIST_NAME,
        genre=genre,
    )
    if not final_video:
        logger.error("Video rendering failed. Exiting.")
        sys.exit(1)
    logger.info(f"Video rendered: {final_video}")

    # Step 7: Generate description + upload
    logger.info("Step 7: Generating description and uploading...")
    description_text = generate_description(song_title, genre)

    from youtube_uploader import YouTubeUploader
    uploader = YouTubeUploader()
    result = uploader.upload_video({
        "video_path": final_video,
        "track_name": song_title,
        "artist": ARTIST_NAME,
        "genre": genre,
        "description_text": description_text,
    })

    if result.get("success"):
        logger.info(f"Upload successful: {result.get('video_url')}")
    elif result.get("mock_upload"):
        logger.info(f"Mock upload (dev mode): {result.get('would_upload', {}).get('title', '')}")
    else:
        logger.error(f"Upload failed: {result.get('error')}")
        sys.exit(1)

    # Step 8: Update archive
    logger.info("Step 8: Updating archive...")
    save_to_archive(track["id"])
    logger.info(f"Archived: {track['id']}")

    # Cleanup temp files
    for path in [audio_path, segment_path, final_video] + footage_paths:
        try:
            os.unlink(path)
        except OSError:
            pass

    gc.collect()
    logger.info("DONE. Short generated and uploaded successfully.")


if __name__ == "__main__":
    main()
