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
_OVERLAY_MAX_LINES = int(os.getenv("OVERLAY_MAX_LINES", "5"))


def generate_overlay_text(track_name: str = "", genre: str = "",
                          bpm: float = 0, energy: str = "",
                          brightness: str = "", texture: str = "",
                          short_num: int = 1,
                          max_lines: int = None) -> list:
    """
    Generate varied overlay text for the video.
    Four uploads/day cannot all use the same CTA pattern, so this rotates between
    song-first hooks, Reddit/fact hooks, quick questions, low-text visual cuts,
    and occasional artist CTAs.
    """
    max_lines = max_lines or _OVERLAY_MAX_LINES
    mode = _pick_overlay_mode(short_num=short_num)
    if mode == "visual":
        return []
    if mode == "reddit":
        reddit_lines = _random_reddit_overlay(max_lines=max_lines)
        if reddit_lines:
            return reddit_lines
    if mode == "question":
        return _question_overlay(track_name=track_name, genre=genre,
                                 short_num=short_num, max_lines=max_lines)
    if mode == "artist":
        return _artist_overlay(track_name=track_name, short_num=short_num,
                               max_lines=max_lines)

    return _music_value_overlay(
        track_name=track_name,
        genre=genre,
        bpm=bpm,
        energy=energy,
        brightness=brightness,
        texture=texture,
        short_num=short_num,
        max_lines=max_lines,
    )


def generate_multiple_overlay_texts(n: int) -> list:
    """
    Generate n distinct overlay text blocks for long-form content (livestream).
    Each block can be a song hook, question, artist CTA, Reddit/fact hook, or
    a low-text visual cut.
    """
    import random
    texts = []
    seen: set = set()
    attempts = 0
    while len(texts) < n and attempts < n * 3:
        attempts += 1
        lines = generate_overlay_text(short_num=attempts)
        key = " ".join(lines)
        if key not in seen:
            seen.add(key)
            texts.append(lines)
    # Pad with fallbacks if needed
    while len(texts) < n:
        texts.append(_fallback_music_tips())
    return texts


def _pick_overlay_mode(short_num: int = 1) -> str:
    """Rotate formats so a 4x/day channel does not feel templated."""
    import random

    env_modes = os.getenv("OVERLAY_MODE_ROTATION", "").strip()
    if env_modes:
        modes = [m.strip().lower() for m in env_modes.split(",") if m.strip()]
    else:
        modes = ["song", "visual", "reddit", "question", "artist", "visual", "song", "reddit"]

    day_offset = datetime.now(timezone.utc).timetuple().tm_yday
    idx = (day_offset + max(0, short_num - 1)) % len(modes)
    mode = modes[idx]
    if mode == "random":
        mode = random.choice(["song", "visual", "reddit", "question", "artist"])
    return mode if mode in {"song", "visual", "reddit", "question", "artist"} else "song"


def _overlay_max_lines_for_duration(duration: float) -> int:
    """Let longer/slower Shorts carry more text without crowding fast clips."""
    if duration < 18.0:
        return min(3, _OVERLAY_MAX_LINES)
    if duration < 21.0:
        return min(4, _OVERLAY_MAX_LINES)
    return _OVERLAY_MAX_LINES


def _random_reddit_overlay(max_lines: int = _OVERLAY_MAX_LINES) -> list:
    """Original Reddit/fact overlay, kept as one recurring content format."""
    sources = [
        _showerthoughts,
        _til_reddit,
        _mildly_interesting,
        _interesting_facts,
        _life_pro_tips,
        _useless_fact_api,
        _numbers_fact_api,
    ]
    import random
    random.shuffle(sources)

    for source in sources:
        try:
            lines = source()
            if lines:
                fitted = _fit_overlay_text(" ".join(lines), max_lines=max_lines)
                if fitted:
                    return fitted
        except Exception as e:
            logger.warning(f"Overlay source {source.__name__} failed: {e}")

    return _fallback_facts(max_lines=max_lines)


def _question_overlay(track_name: str = "", genre: str = "",
                      short_num: int = 1,
                      max_lines: int = _OVERLAY_MAX_LINES) -> list:
    """Comment-bait without making every upload a direct ad."""
    import random

    song_name = (track_name or "this song").strip()
    genre_l = (genre or "").lower()
    prompts = [
        ["VIBE CHECK", "night drive", "or gym playlist?", "comment one"],
        ["WOULD YOU SAVE THIS?", "yes or skip?", "be honest", "stream link in bio"],
        ["THIS PART", "headphones or car?", "where does it hit?", "comment the mood"],
        ["PLAYLIST TEST", song_name[:32], "add or skip?", "stream link in bio"],
        ["RATE THE DROP", "1 to 10", "no overthinking", "Star Drift"],
    ]
    if any(key in genre_l for key in ("ambient", "chill", "lofi")):
        prompts.append(["MOOD CHECK", "study", "sleep", "or late drive?"])
    return _normalize_overlay_lines(random.choice(prompts), max_lines=max_lines)


def _artist_overlay(track_name: str = "", short_num: int = 1,
                    max_lines: int = _OVERLAY_MAX_LINES) -> list:
    """Occasional direct Star Drift CTA."""
    import random

    song_name = (track_name or "this song").strip()
    prompts = [
        ["STAR DRIFT", song_name[:32], "full song in bio", "save if it hits"],
        ["NEW STAR DRIFT", "listen to this part", "then stream the song", "link in bio"],
        ["IF THIS LOOPS", "save the song", "Star Drift", "stream link in bio"],
    ]
    if os.getenv("BEATSTARS_URL", "").strip() and short_num % 4 == 0:
        prompts.append(["ARTISTS", "stream Star Drift", "beats also in bio", "use one for a hook"])
    return _normalize_overlay_lines(random.choice(prompts), max_lines=max_lines)


def _music_value_overlay(track_name: str = "", genre: str = "",
                         bpm: float = 0, energy: str = "",
                         brightness: str = "", texture: str = "",
                         short_num: int = 1,
                         max_lines: int = _OVERLAY_MAX_LINES) -> list:
    """Pick a compact, song-first text hook for Shorts."""
    import random

    genre_l = (genre or "").lower()
    energy_l = (energy or "").lower()
    brightness_l = (brightness or "").lower()
    texture_l = (texture or "").lower()
    has_beat_link = bool(os.getenv("BEATSTARS_URL", "").strip())
    song_name = (track_name or "this song").strip()

    candidates = []

    if bpm >= 145:
        candidates.extend([
            [
                "FAST PART",
                "wait for the switch",
                "save it if it hits",
                "stream link in bio",
            ],
            [
                "PLAYLIST TEST",
                "gym or night drive?",
                "let this part decide",
                "stream link in bio",
            ],
        ])
    elif 0 < bpm <= 95:
        candidates.extend([
            [
                "SLOW PART",
                "headphones make it hit",
                "save for later",
                "stream link in bio",
            ],
            [
                "LATE NIGHT TEST",
                "window down",
                "volume up",
                "stream link in bio",
            ],
        ])

    if any(key in genre_l for key in ("trap", "hip", "rap", "phonk")):
        candidates.extend([
            [
                "THIS PART",
                "is for the late drive",
                "save if you feel it",
                "stream link in bio",
            ],
            [
                "STAR DRIFT",
                song_name[:32],
                "does this hit?",
                "stream link in bio",
            ],
        ])

    if "high" in energy_l or "bright" in brightness_l:
        candidates.append([
            "TURN THIS UP",
            "best part starts early",
            "save for the playlist",
            "stream link in bio",
        ])

    if "dark" in brightness_l or "distorted" in texture_l:
        candidates.append([
            "DARK MODE SONG",
            "late night headphones",
            "let the drop breathe",
            "stream link in bio",
        ])

    if has_beat_link and short_num % 3 == 0:
        candidates.append([
            "ARTISTS",
            "stream Star Drift",
            "beats also in bio",
            "use one for a hook",
        ])

    candidates.extend(_fallback_music_tips(pool=True))
    return _normalize_overlay_lines(random.choice(candidates), max_lines=max_lines)


def _fallback_music_tips(pool: bool = False):
    tips = [
        [
            "NEW SONG PREVIEW",
            "listen for the hook",
            "save if it hits",
            "stream link in bio",
        ],
        [
            "MOOD CHECK",
            "night drive",
            "or headphones?",
            "stream link in bio",
        ],
        [
            "STAR DRIFT",
            "if this part loops",
            "save the song",
            "stream link in bio",
        ],
        [
            "PLAYLIST CHECK",
            "would you add this?",
            "let the drop answer",
            "stream link in bio",
        ],
        [
            "DO NOT SKIP",
            "the hook comes fast",
            "wait for it",
            "stream link in bio",
        ],
        [
            "VIBE TEST",
            "late night or sunrise?",
            "comment the mood",
            "stream link in bio",
        ],
    ]
    if pool:
        return tips
    import random
    return _normalize_overlay_lines(random.choice(tips))


def _normalize_overlay_lines(lines: list, max_chars: int = _OVERLAY_MAX_CHARS,
                             max_lines: int = _OVERLAY_MAX_LINES) -> list:
    wrapped = []
    for line in lines:
        wrapped.extend(_split_text(str(line), max_chars=max_chars))
    return wrapped[:max_lines]


def _fit_overlay_text(text: str, max_chars: int = _OVERLAY_MAX_CHARS,
                      max_lines: int = _OVERLAY_MAX_LINES) -> list:
    """Return wrapped lines only when the complete text fits on screen."""
    lines = _split_text(text, max_chars=max_chars, max_lines=None)
    return lines if 0 < len(lines) <= max_lines else []


def _reddit_top_facts(subreddit: str, strip_prefix: str = "") -> list:
    """
    Shared helper: fetch top posts from a subreddit, return one as split lines.
    Tries JSON API first (browser UA), then RSS feed as fallback — both are
    public endpoints that don't need auth.
    strip_prefix: e.g. "LPT: " to remove from the start of post titles.
    """
    import requests, random

    # Reddit blocks generic bot UAs. Use a browser-like UA to get through.
    _HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/html, */*",
        "Accept-Language": "en-US,en;q=0.9",
    }

    posts = []

    # ── Try 1: JSON API ───────────────────────────────────────────────────────
    try:
        resp = requests.get(
            f"https://www.reddit.com/r/{subreddit}/top.json?t=week&limit=50",
            headers=_HEADERS,
            timeout=10,
        )
        if resp.status_code == 200:
            try:
                posts = resp.json().get("data", {}).get("children", [])
            except Exception:
                posts = []
        else:
            logger.debug(f"Reddit JSON {subreddit}: HTTP {resp.status_code}, trying RSS")
    except Exception as e:
        logger.debug(f"Reddit JSON {subreddit} request failed: {e}, trying RSS")

    # ── Try 2: RSS feed (much less restricted than JSON API) ──────────────────
    if not posts:
        try:
            import xml.etree.ElementTree as ET
            rss_resp = requests.get(
                f"https://www.reddit.com/r/{subreddit}/top/.rss?t=week&limit=50",
                headers={**_HEADERS, "Accept": "application/rss+xml, application/xml, */*"},
                timeout=10,
            )
            if rss_resp.status_code == 200:
                root = ET.fromstring(rss_resp.text)
                ns = {"atom": "http://www.w3.org/2005/Atom"}
                titles = []
                for entry in root.findall(".//atom:entry", ns):
                    title_el = entry.find("atom:title", ns)
                    if title_el is not None and title_el.text:
                        titles.append(title_el.text.strip())
                if titles:
                    # Convert to fake "post children" format so rest of code works
                    posts = [{"data": {"title": t, "over_18": False}} for t in titles]
                    logger.debug(f"Reddit RSS {subreddit}: got {len(posts)} posts")
        except Exception as e:
            logger.debug(f"Reddit RSS {subreddit} failed: {e}")

    if not posts:
        return []

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
        if len(title) < 20 or len(title) > 115:
            continue
        words = set(title.lower().split())
        if words & _BLOCKED:
            continue
        lines = _fit_overlay_text(title)
        if not lines:
            continue
        candidates.append(lines)

    if not candidates:
        return []

    return random.choice(candidates[:15])


def _til_reddit() -> list:
    """r/todayilearned top of week — proven interesting facts."""
    return _reddit_top_facts("todayilearned")


def _mildly_interesting() -> list:
    """r/mildlyinteresting top of week — curious observations."""
    return _reddit_top_facts("mildlyinteresting")


def _showerthoughts() -> list:
    """r/Showerthoughts top of week — thought-provoking one-liners, great for music vibes."""
    return _reddit_top_facts("Showerthoughts")


def _interesting_facts() -> list:
    """r/Damnthatsinteresting top of week — wild, mind-blowing facts."""
    return _reddit_top_facts("Damnthatsinteresting")


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
    return _fit_overlay_text(text[:120])


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
    return _fit_overlay_text(text[:120])


def _fallback_facts(max_lines: int = _OVERLAY_MAX_LINES) -> list:
    """Hardcoded fun facts — last resort when all APIs are down."""
    import random
    facts = [
        "Honey basically never expires.",
        "A day on Venus is longer than its year.",
        "Sharks are older than trees.",
        "Oxford University is older than the Aztec Empire.",
        "The average cloud weighs over a million pounds.",
        "Bananas are berries. Strawberries are not.",
        "Nintendo started as a playing card company.",
        "The moon drifts away about 3.8cm each year.",
        "Octopuses have three hearts and blue blood.",
        "The word stewardesses can be typed with one hand.",
    ]
    return _fit_overlay_text(random.choice(facts), max_lines=max_lines)


def _split_text(text: str, max_chars: int = 22, max_lines: int = 5) -> list:
    """Split text into lines of max_chars, breaking at word boundaries."""
    words = text.split()
    lines = []
    current = ""
    for word in words:
        if len(word) > max_chars:
            if current:
                lines.append(current)
                current = ""
            lines.append(word[:max_chars])
            continue
        test = f"{current} {word}".strip()
        if len(test) <= max_chars:
            current = test
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines if max_lines is None else lines[:max_lines]


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

    # Pick the overlay before cut pacing. Text-heavy Shorts need calmer visuals;
    # low-text Shorts can let the background move more.
    overlay_max_lines = _overlay_max_lines_for_duration(segment_duration)
    poem_lines = generate_overlay_text(
        track_name=song_title,
        genre=genre,
        bpm=bpm,
        energy=energy,
        brightness=brightness,
        texture=texture,
        short_num=short_num,
        max_lines=overlay_max_lines,
    )
    if poem_lines:
        logger.info(f"Overlay text {short_num}: {poem_lines}")

    if poem_lines:
        cut_kwargs = {"min_interval": 2.0, "max_interval": 4.8, "skip_ratio": 0.78}
    else:
        cut_kwargs = {"min_interval": 1.4, "max_interval": 3.4, "skip_ratio": 0.45}

    beat_intervals = get_beat_intervals(
        [b for b in analysis["all_beat_times"] if window_start <= b <= window_end],
        start_offset=window_start,
        segment_duration=segment_duration,
        **cut_kwargs,
    )
    logger.info(f"Beat intervals: {len(beat_intervals)} cuts")

    # Fetch fresh footage for each short (different clips)
    logger.info(f"Fetching footage for short {short_num}...")
    footage_paths = fetch_footage(song_title, bpm=bpm, energy=energy,
                                   brightness=brightness, texture=texture)
    if not footage_paths:
        logger.error(f"No footage for short {short_num}. Skipping.")
        return False

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
        overlay_max_lines=overlay_max_lines,
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
