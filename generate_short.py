#!/usr/bin/env python3
"""
generate_short.py — Main entry point for music-shorts pipeline.

1. Scan source channel for tracks
2. Pick next unprocessed track
3. Download audio, analyze beats, find multiple best sections
4. For each section: fetch footage, render beat-synced video, upload
5. Update archive.txt
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
NUM_SHORTS = int(os.getenv("NUM_SHORTS", "2"))


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
    """Generate a short personal-sounding blurb about the track."""
    try:
        from llm_client import get_llm_client, llm_available
        if not llm_available():
            return ""

        client, model = get_llm_client()
        resp = client.chat.completions.create(
            model=model,
            messages=[{
                "role": "user",
                "content": (
                    f"Write 1-2 SHORT sentences as if you're the artist casually telling "
                    f"people about your track \"{track_name}\". Sound natural and human, "
                    f"like a real person typing a quick note — NOT like AI or marketing copy. "
                    f"No hashtags, no emojis, no \"subscribe\", no exclamation marks. "
                    f"Keep it chill and genuine. Examples of the tone:\n"
                    f"- \"made this one late at night, just vibes\"\n"
                    f"- \"been sitting on this beat for a minute, felt right to drop it\"\n"
                    f"- \"this one hits different with headphones on\"\n"
                    f"Reply with ONLY the 1-2 sentences, nothing else."
                ),
            }],
            max_tokens=80,
            temperature=0.9,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        logger.warning(f"LLM description generation failed: {e}")
        return ""


def generate_overlay_text() -> list:
    """Generate 2-3 line overlay text: either a Reddit shower thought or a motivational plot twist.
    Returns list of short lines for the text overlay."""
    import random

    # 50/50 chance: shower thought vs motivational plot twist
    use_shower_thought = random.random() < 0.5

    if use_shower_thought:
        lines = _fetch_shower_thought()
        if lines:
            return lines

    # Motivational plot twist (or fallback if shower thought failed)
    return _generate_motivational_twist()


def _fetch_shower_thought() -> list:
    """Fetch a trending shower thought from Reddit. Returns split lines or empty list."""
    import requests
    try:
        resp = requests.get(
            "https://www.reddit.com/r/Showerthoughts/hot.json?limit=25",
            headers={"User-Agent": "music-shorts-bot/1.0"},
            timeout=8,
        )
        if resp.status_code != 200:
            return []

        posts = resp.json().get("data", {}).get("children", [])
        import random
        # Filter: no profanity, not too long, not too short
        BLOCKED = {"fuck", "shit", "ass", "damn", "hell", "dick", "bitch", "sex", "porn", "kill", "die", "dead", "nsfw"}
        candidates = []
        for post in posts:
            title = post.get("data", {}).get("title", "")
            if not title or len(title) < 15 or len(title) > 120:
                continue
            if post.get("data", {}).get("over_18"):
                continue
            words = set(title.lower().split())
            if words & BLOCKED:
                continue
            candidates.append(title)

        if not candidates:
            return []

        thought = random.choice(candidates[:10])
        # Split into 2-3 lines (~35 chars each)
        return _split_text(thought, max_chars=35)

    except Exception as e:
        logger.warning(f"Reddit shower thought fetch failed: {e}")
        return []


def _generate_motivational_twist() -> list:
    """Generate a motivational quote with a funny unexpected twist via LLM."""
    try:
        from llm_client import get_llm_client, llm_available
        if not llm_available():
            return _fallback_overlay_lines()

        client, model = get_llm_client()
        resp = client.chat.completions.create(
            model=model,
            messages=[{
                "role": "user",
                "content": (
                    "Write a motivational quote in exactly 3 short lines.\n"
                    "Line 1: Inspirational setup (under 30 chars)\n"
                    "Line 2: Continuation that sounds motivational (under 35 chars)\n"
                    "Line 3: Funny unexpected twist that subverts it (under 35 chars)\n\n"
                    "Examples:\n"
                    "  Never give up\n"
                    "  on finishing what you started\n"
                    "  Finish that Big Mac.\n"
                    "\n"
                    "  Believe in yourself\n"
                    "  because no one else will\n"
                    "  They saw your browser history.\n"
                    "\n"
                    "  The early bird\n"
                    "  gets the worm\n"
                    "  The late bird gets Uber Eats.\n"
                    "\n"
                    "  Dream big\n"
                    "  work hard\n"
                    "  Nap harder.\n"
                    "\n"
                    "Rules:\n"
                    "- No profanity, keep it YouTube-safe\n"
                    "- The twist should make people laugh or smirk\n"
                    "- Lowercase for lines 1-2, capitalize the twist (line 3) for emphasis\n"
                    "- Reply with ONLY the 3 lines, nothing else"
                ),
            }],
            max_tokens=80,
            temperature=1.0,
        )
        lines = [l.strip() for l in resp.choices[0].message.content.strip().split('\n') if l.strip()]
        return lines[:3] if lines else _fallback_overlay_lines()
    except Exception as e:
        logger.warning(f"LLM motivational twist generation failed: {e}")
        return _fallback_overlay_lines()


def _fallback_overlay_lines() -> list:
    """Hardcoded fallback overlay texts when both Reddit and LLM fail."""
    import random
    options = [
        ["Never give up", "on finishing what you started", "Finish that Big Mac."],
        ["Dream big", "work hard", "Nap harder."],
        ["Believe in yourself", "because at this point", "Who else is going to?"],
        ["Stay humble", "stay hungry", "Order the large fries."],
        ["Good things come", "to those who wait", "Great things come to those who don't."],
    ]
    return random.choice(options)


def _split_text(text: str, max_chars: int = 35) -> list:
    """Split text into lines of max_chars, breaking at word boundaries."""
    words = text.split()
    lines = []
    current = ""
    for word in words:
        test = f"{current} {word}".strip()
        if len(test) <= max_chars:
            current = test
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines[:3]  # max 3 lines


# -- Original poem generator (disabled — kept for reference) --
# def generate_poem(track_name: str, genre: str) -> list:
#     """Generate a 4-line poem/quote for engagement overlay."""
#     ... (see git history for full implementation)


def render_and_upload_short(audio_path: str, analysis: dict,
                             window_start: float, window_end: float,
                             song_title: str, genre: str,
                             bpm: float, energy: str, brightness: str,
                             texture: str, short_num: int) -> bool:
    """Render and upload a single short from a specific audio window.
    Returns True on success."""
    logger.info(f"--- Short {short_num}: {window_start:.1f}s - {window_end:.1f}s ---")

    segment_path = f"/tmp/audio_segment_{short_num}.wav"
    extract_audio_segment(audio_path, window_start, window_end, segment_path)

    segment_duration = window_end - window_start
    beat_intervals = get_beat_intervals(
        [b for b in analysis["all_beat_times"] if window_start <= b <= window_end],
        start_offset=window_start,
        segment_duration=segment_duration,
    )
    logger.info(f"Beat intervals: {len(beat_intervals)} cuts")

    # Fetch fresh footage for each short (different clips)
    logger.info(f"Fetching footage for short {short_num}...")
    footage_paths = fetch_footage(song_title, bpm=bpm, energy=energy,
                                   brightness=brightness, texture=texture)
    if not footage_paths:
        logger.error(f"No footage for short {short_num}. Skipping.")
        return False

    # Generate unique overlay text for each short (shower thought or motivational twist)
    poem_lines = generate_overlay_text()
    if poem_lines:
        logger.info(f"Overlay text {short_num}: {poem_lines}")

    # Render
    final_video = render_short(
        audio_segment_path=segment_path,
        footage_paths=footage_paths,
        beat_intervals=beat_intervals,
        track_name=song_title,
        artist=ARTIST_NAME,
        genre=genre,
        poem_lines=poem_lines,
        bpm=bpm,
    )
    if not final_video:
        logger.error(f"Render failed for short {short_num}. Skipping.")
        return False
    logger.info(f"Short {short_num} rendered: {final_video}")

    # Generate description + upload
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

    success = False
    if result.get("success"):
        logger.info(f"Short {short_num} uploaded: {result.get('video_url')}")
        success = True
    elif result.get("mock_upload"):
        logger.info(f"Short {short_num} mock upload: {result.get('would_upload', {}).get('title', '')}")
        success = True
    else:
        logger.error(f"Short {short_num} upload failed: {result.get('error')}")

    # Cleanup
    for path in [segment_path, final_video] + footage_paths:
        try:
            os.unlink(path)
        except OSError:
            pass
    gc.collect()

    return success


def main():
    logger.info("=" * 60)
    logger.info(f"MUSIC SHORTS GENERATOR (NUM_SHORTS={NUM_SHORTS})")
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

    # Step 4: Analyze beats — finds multiple windows
    logger.info("Step 4: Analyzing beats and finding best sections...")
    analysis = analyze_track(audio_path)
    bpm = analysis["bpm"]
    energy = analysis.get("energy", "")
    brightness = analysis.get("brightness", "")
    texture = analysis.get("texture", "")

    # Step 5: Classify genre
    from footage_fetcher import classify_genre_llm
    genre = classify_genre_llm(song_title, bpm=bpm, energy=energy,
                                brightness=brightness, texture=texture)
    logger.info(f"Genre: {genre} (BPM: {bpm:.0f}, {energy}/{brightness}/{texture})")

    # Step 6: Render + upload each short from different windows
    windows = analysis.get("all_windows", [(analysis["best_start"], analysis["best_end"])])
    logger.info(f"Generating {len(windows)} shorts from different sections")

    successes = 0
    for i, (ws, we) in enumerate(windows):
        ok = render_and_upload_short(
            audio_path, analysis, ws, we,
            song_title, genre, bpm, energy, brightness, texture,
            short_num=i + 1,
        )
        if ok:
            successes += 1

    # Step 7: Update archive
    logger.info("Step 7: Updating archive...")
    save_to_archive(track["id"])
    logger.info(f"Archived: {track['id']}")

    # Final cleanup
    try:
        os.unlink(audio_path)
    except OSError:
        pass

    gc.collect()
    logger.info(f"DONE. {successes}/{len(windows)} shorts generated and uploaded.")


if __name__ == "__main__":
    main()
