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
from datetime import datetime, timedelta, timezone
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

# Publishing slots in UTC hours — 10am / 1pm / 6pm / 9pm ET
_PUBLISH_SLOTS_UTC = [14, 17, 22, 1]


def _get_publish_schedule(n: int) -> list:
    """Return n UTC ISO8601 publish times spread across today's schedule.
    Slots: 14:00, 17:00, 22:00, 01:00 UTC (= 10am, 1pm, 6pm, 9pm ET).
    Any slot already in the past is bumped to tomorrow at the same hour.
    Returns empty list if SCHEDULE_UPLOADS env var is not 'true'.
    """
    if os.getenv("SCHEDULE_UPLOADS", "true").lower() != "true":
        return []
    now = datetime.now(timezone.utc)
    times = []
    for h in _PUBLISH_SLOTS_UTC[:n]:
        # Slot 01 is after midnight UTC — always the next calendar day
        offset_days = 1 if h < 6 else 0
        candidate = now.replace(hour=h, minute=0, second=0, microsecond=0) + timedelta(days=offset_days)
        if candidate <= now:
            candidate += timedelta(days=1)
        times.append(candidate.strftime("%Y-%m-%dT%H:%M:%SZ"))
    return times


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


_BLOCKED = {
    "fuck", "shit", "ass", "damn", "hell", "dick", "bitch",
    "sex", "porn", "kill", "die", "dead", "nsfw", "rape", "suicide",
    "murder", "drug", "heroin", "cocaine", "meth",
}

# Max chars per overlay line — longer lines = more readable on a big stream screen
_OVERLAY_MAX_CHARS = 32


def generate_overlay_text() -> list:
    """
    Fetch an interesting/funny Reddit post for the video overlay.
    Rotates across multiple subreddits for vibes variety.
    Goal: make viewer pause and read → stays longer → hears more of the song.
    """
    sources = [
        _showerthoughts,      # r/Showerthoughts — thought-provoking one-liners, perfect vibe
        _til_reddit,          # r/todayilearned — surprising facts
        _mildly_interesting,  # r/mildlyinteresting — curious observations
        _interesting_asfuck,  # r/interestingasfuck — wild facts
        _life_pro_tips,       # r/LifeProTips — useful insight that sparks engagement
        _useless_fact_api,    # uselessfacts.jsph.pl — free API fallback
        _numbers_fact_api,    # numbersapi.com — trivia fallback
    ]
    import random
    # Shuffle the top sources for variety — don't always start with Showerthoughts
    random.shuffle(sources[:4])

    for source in sources:
        try:
            lines = source()
            if lines:
                return lines
        except Exception as e:
            logger.warning(f"Overlay source {source.__name__} failed: {e}")

    return _fallback_facts()


def generate_multiple_overlay_texts(n: int) -> list:
    """
    Generate n distinct overlay texts for long-form content (livestream).
    Each call tries a different source for variety. Returns list of line-lists.
    """
    import random
    texts = []
    seen: set = set()
    attempts = 0
    while len(texts) < n and attempts < n * 3:
        attempts += 1
        lines = generate_overlay_text()
        key = " ".join(lines)
        if key not in seen:
            seen.add(key)
            texts.append(lines)
    # Pad with fallbacks if needed
    while len(texts) < n:
        texts.append(_fallback_facts())
    return texts


def _reddit_top_facts(subreddit: str, strip_prefix: str = "") -> list:
    """
    Shared helper: fetch top posts from a subreddit, return one as split lines.
    strip_prefix: e.g. "TIL " to remove from the start of post titles.
    """
    import requests, random
    resp = requests.get(
        f"https://www.reddit.com/r/{subreddit}/top.json?t=week&limit=50",
        headers={"User-Agent": "music-shorts-bot/1.0"},
        timeout=8,
    )
    if resp.status_code != 200:
        return []

    posts = resp.json().get("data", {}).get("children", [])
    candidates = []
    for post in posts:
        d = post.get("data", {})
        title = d.get("title", "")
        if d.get("over_18"):
            continue
        # Strip common prefixes
        for pfx in ["TIL that ", "TIL ", strip_prefix]:
            if title.lower().startswith(pfx.lower()):
                title = title[len(pfx):]
                break
        # Clean up — take first sentence only (facts can be very long)
        for sep in [". ", "! ", "? "]:
            if sep in title:
                title = title.split(sep)[0] + sep.strip()
                break
        title = title.strip()
        if len(title) < 20 or len(title) > 130:
            continue
        words = set(title.lower().split())
        if words & _BLOCKED:
            continue
        candidates.append(title)

    if not candidates:
        return []

    fact = random.choice(candidates[:15])
    return _split_text(fact, max_chars=_OVERLAY_MAX_CHARS)


def _til_reddit() -> list:
    """r/todayilearned top of week — proven interesting facts."""
    return _reddit_top_facts("todayilearned")


def _mildly_interesting() -> list:
    """r/mildlyinteresting top of week — curious observations."""
    return _reddit_top_facts("mildlyinteresting")


def _showerthoughts() -> list:
    """r/Showerthoughts top of week — thought-provoking one-liners, great for music vibes."""
    return _reddit_top_facts("Showerthoughts")


def _interesting_asfuck() -> list:
    """r/interestingasfuck top of week — wild, mind-blowing facts."""
    return _reddit_top_facts("interestingasfuck")


def _life_pro_tips() -> list:
    """r/LifeProTips top of week — useful tips that make people think."""
    return _reddit_top_facts("LifeProTips", strip_prefix="LPT: ")


def _useless_fact_api() -> list:
    """uselessfacts.jsph.pl — free fun facts API, no key needed."""
    import requests
    resp = requests.get(
        "https://uselessfacts.jsph.pl/api/v2/facts/random",
        params={"language": "en"},
        timeout=8,
    )
    resp.raise_for_status()
    text = resp.json().get("text", "").strip()
    if not text or len(text) < 20:
        return []
    words = set(text.lower().split())
    if words & _BLOCKED:
        return []
    # Trim to first sentence if long
    for sep in [". ", "! ", "? "]:
        if sep in text[20:]:
            text = text.split(sep)[0] + sep.strip()
            break
    return _split_text(text[:160], max_chars=_OVERLAY_MAX_CHARS)


def _numbers_fact_api() -> list:
    """numbersapi.com — free trivia facts about random numbers, no key needed."""
    import requests
    resp = requests.get(
        "http://numbersapi.com/random/trivia",
        params={"json": True},
        timeout=8,
    )
    resp.raise_for_status()
    text = resp.json().get("text", "").strip()
    if not text or len(text) < 20:
        return []
    words = set(text.lower().split())
    if words & _BLOCKED:
        return []
    return _split_text(text[:160], max_chars=_OVERLAY_MAX_CHARS)


def _fallback_facts() -> list:
    """Hardcoded fun facts — last resort when all APIs are down."""
    import random
    facts = [
        "Honey never expires. 3000-year-old honey found in Egyptian tombs was still edible.",
        "Cleopatra lived closer in time to the Moon landing than to the pyramids being built.",
        "A day on Venus is longer than a year on Venus.",
        "Sharks are older than trees. They've existed for over 400 million years.",
        "Oxford University is older than the Aztec Empire.",
        "The average cloud weighs about 1.1 million pounds.",
        "Wombat poop is cube-shaped. It's the only animal that does this.",
        "There are more possible chess games than atoms in the observable universe.",
        "Bananas are berries. Strawberries are not.",
        "A group of flamingos is called a flamboyance.",
        "Nintendo was founded in 1889. It started as a playing card company.",
        "The moon is slowly drifting away from Earth — about 3.8cm per year.",
        "Crows can recognize human faces and hold grudges.",
        "Octopuses have three hearts and blue blood.",
        "The longest English word you can type with one hand is 'stewardesses'.",
    ]
    return _split_text(random.choice(facts), max_chars=_OVERLAY_MAX_CHARS)


def _split_text(text: str, max_chars: int = 22) -> list:
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
    return lines[:5]  # max 5 lines


# -- Original poem generator (disabled — kept for reference) --
# def generate_poem(track_name: str, genre: str) -> list:
#     """Generate a 4-line poem/quote for engagement overlay."""
#     ... (see git history for full implementation)


def render_and_upload_short(audio_path: str, analysis: dict,
                             window_start: float, window_end: float,
                             song_title: str, genre: str,
                             bpm: float, energy: str, brightness: str,
                             texture: str, short_num: int,
                             publish_at: str = None) -> bool:
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
    upload_payload = {
        "video_path": final_video,
        "track_name": song_title,
        "artist": ARTIST_NAME,
        "genre": genre,
        "description_text": description_text,
    }
    if publish_at:
        upload_payload["publish_at"] = publish_at
        logger.info(f"Short {short_num} scheduled for: {publish_at}")
    result = uploader.upload_video(upload_payload)

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

    # Calculate staggered publish times (10am / 1pm / 6pm / 9pm ET)
    publish_schedule = _get_publish_schedule(len(windows))
    if publish_schedule:
        logger.info(f"Publish schedule: {publish_schedule}")
    else:
        logger.info("Uploading immediately (SCHEDULE_UPLOADS not enabled)")

    successes = 0
    for i, (ws, we) in enumerate(windows):
        publish_at = publish_schedule[i] if i < len(publish_schedule) else None
        ok = render_and_upload_short(
            audio_path, analysis, ws, we,
            song_title, genre, bpm, energy, brightness, texture,
            short_num=i + 1,
            publish_at=publish_at,
        )
        if ok:
            successes += 1

    # Step 7: Update archive — only if at least one short uploaded successfully.
    # Previously this archived the track even on upload failure, burning through
    # the track list while posting nothing to YouTube.
    logger.info("Step 7: Updating archive...")
    if successes > 0:
        save_to_archive(track["id"])
        logger.info(f"Archived: {track['id']} ({successes}/{len(windows)} shorts uploaded)")
    else:
        logger.warning(f"NOT archiving {track['id']} — 0/{len(windows)} shorts uploaded. Will retry next run.")

    # Final cleanup
    try:
        os.unlink(audio_path)
    except OSError:
        pass

    gc.collect()
    logger.info(f"DONE. {successes}/{len(windows)} shorts generated and uploaded.")


if __name__ == "__main__":
    main()
