"""
Footage Fetcher — Pexels + Archive.org public domain content
Mixes stock footage (Pexels) with public domain cartoons/vintage clips
(Archive.org) based on genre classification.
"""

import os
import time
import random
import logging
import requests
from typing import List, Dict, Optional
from urllib.parse import quote_plus

logger = logging.getLogger(__name__)

PEXELS_API_KEY = os.getenv("PEXELS_API_KEY", "")
PEXELS_BASE_URL = "https://api.pexels.com/videos"
ARCHIVE_SEARCH_URL = "https://archive.org/advancedsearch.php"
ARCHIVE_HEADERS = {"User-Agent": "MusicShortsBot/1.0 (Python; automated-shorts)"}

# Visual mood -> Pexels search keywords (shuffled each run for variety)
# These are VISUAL MOODS, not strict music genres — matches what looks right
PEXELS_KEYWORDS = {
    "electronic": [
        "neon city night", "futuristic tunnel", "laser lights club",
        "cyberpunk city street", "led lights party", "night drive city",
        "DJ turntable", "crowd concert", "drone city night",
        "electric sparks", "hologram", "arcade game",
    ],
    "hype": [
        "skateboard trick", "parkour urban", "crowd cheering stadium",
        "boxing knockout", "basketball dunk", "lightning storm",
        "fire explosion slow motion", "motorcycle stunt", "confetti celebration",
        "race car speed", "skydiving", "surfing wave",
    ],
    "chill": [
        "ocean waves beach", "sunset clouds timelapse", "rain on window",
        "cat sleeping cozy", "goldfish aquarium", "jellyfish underwater",
        "flower blooming timelapse", "butterfly garden", "puppy playing",
        "waterfall forest", "snow cabin", "candle flame dark",
    ],
    "lofi": [
        "rain window cozy", "coffee steam morning", "vinyl record player",
        "train window countryside", "autumn leaves falling", "city rooftop sunset",
        "cat on windowsill", "bookshelf cozy", "rainy street night",
        "hand writing journal", "piano keys", "bicycle ride city",
    ],
    "phonk": [
        "car drift smoke", "dark urban alley", "boxing gym training",
        "motorcycle night ride", "smoke slow motion dark", "gym deadlift",
        "bull riding rodeo", "wolf howling", "eagle flying",
        "thunderstorm dark", "street racing", "dark tunnel",
    ],
    "trap": [
        "money cash counting", "city skyline night", "gold jewelry",
        "luxury car driving", "helicopter aerial city", "lion roaring",
        "smoke hookah", "crowd mosh pit", "graffiti art",
        "chain necklace", "nightclub vip", "fireworks night",
    ],
    "psychedelic": [
        "kaleidoscope colorful", "ink drop water colorful", "fractal zoom",
        "lava lamp close up", "oil water colors", "paint mixing swirl",
        "soap bubble rainbow", "prism light rainbow", "aurora sky timelapse",
        "mushroom forest macro", "deep ocean bioluminescence", "northern lights",
    ],
    "dark": [
        "thunderstorm clouds", "dark forest fog", "storm ocean night",
        "fire burning dark", "lightning strike night", "dark canyon",
        "mountain storm clouds", "dark river night", "smoke dark background",
        "storm waves ocean", "dark road rain", "night sky dramatic",
    ],
    "rock": [
        "concert crowd lights", "guitar close up", "drum sticks playing",
        "stadium concert lights", "fire pyrotechnics stage", "crowd surfing",
        "electric guitar sparks", "smoke stage lights", "headbanging crowd",
        "motorcycle highway", "desert road driving", "mountain storm",
    ],
    "ambient": [
        "underwater coral reef", "aurora borealis sky", "space earth orbit",
        "deep ocean jellyfish", "fog mountain sunrise", "crystal cave light",
        "northern lights timelapse", "bioluminescence ocean", "desert sand dunes",
        "milky way stars", "ice glacier melting", "light rays forest",
    ],
    "orchestral": [
        "eagle soaring mountain", "castle medieval", "storm ocean waves",
        "volcano eruption", "horse galloping field", "sword fight sparks",
        "waterfall aerial", "sunrise mountain peak", "army marching",
        "lion walking savanna", "dragon fire", "glacier landscape",
    ],
    "default": [
        "abstract motion colorful", "particle effects dark", "light streaks motion",
        "smoke dark background", "water surface ripple", "slow motion liquid",
        "dog running field", "cat funny", "bird flying sky",
        "neon bokeh lights", "ink drop water", "city timelapse night",
    ],
}

# Genre -> Archive.org search queries for public domain content
# These pull cartoons, vintage footage, retro clips
ARCHIVE_QUERIES = {
    "hype": [
        ("superman", "fleischer_studios"),
        ("popeye", "classic_cartoons"),
        ("boxing", "prelinger"),
        ("racing car", "prelinger"),
        ("action cartoon", "classic_cartoons"),
    ],
    "phonk": [
        ("noir detective", None),
        ("dark city night", "prelinger"),
        ("superman villain", "fleischer_studios"),
        ("thunder lightning", "prelinger"),
        ("gangster film noir", "film_noir"),
    ],
    "trap": [
        ("gangster film noir", "film_noir"),
        ("city night detective", None),
        ("popeye fight", "classic_cartoons"),
        ("gold rush", None),
        ("explosion military", "prelinger"),
    ],
    "electronic": [
        ("rocket space", "prelinger"),
        ("atomic energy", "prelinger"),
        ("futuristic city", "prelinger"),
        ("robot mechanical", "prelinger"),
        ("neon lights city", "prelinger"),
    ],
    "orchestral": [
        ("superman flying", "fleischer_studios"),
        ("castle knight", None),
        ("war battle", "prelinger"),
        ("eagle soaring", "prelinger"),
        ("ancient ruins", "prelinger"),
    ],
    "default": [
        ("betty boop", "classic_cartoons"),
        ("felix cat", "classic_cartoons"),
        ("funny cartoon", "classic_cartoons"),
        ("color cartoon", "classic_cartoons"),
        ("newsreel", "prelinger"),
    ],
}

# Which source to use per mood — "archive" or "pexels" (never mixed)
# Keeps visual cohesion: all vintage OR all modern stock footage
SOURCE_STYLE = {
    "hype":        "archive",   # cartoons, action clips
    "phonk":       "archive",   # noir, dark vintage
    "trap":        "archive",   # film noir, vintage
    "orchestral":  "archive",   # epic vintage
    "electronic":  "pexels",    # modern neon/city footage
    "chill":       "pexels",    # modern nature/cozy
    "lofi":        "pexels",    # modern cozy vibes
    "ambient":     "pexels",    # modern nature/space
    "psychedelic": "pexels",    # colorful abstract modern
    "dark":        "pexels",    # modern dark/moody
    "rock":        "pexels",    # modern concert/road footage
    "default":     "pexels",    # modern stock
}

TARGET_CLIPS = 12

# ─── Genre Classification ───────────────────────────────────

def classify_genre(track_title: str) -> str:
    """Simple keyword-based visual mood classification from track title."""
    title_lower = track_title.lower()
    # Order matters — more specific matches first
    hints = {
        "psychedelic": ["psychedelic", "trippy", "acid", "fractal", "kaleidoscope", "experimental", "weird"],
        "dark": ["ghost", "nightmare", "demon", "horror", "scream", "doom", "grave", "cursed", "haunted"],
        "rock": ["rock", "guitar", "punk", "grunge", "metal", "shred"],
        "phonk": ["phonk", "drift", "cowbell", "memphis"],
        "trap": ["trap", "drill", "gang", "menace", "opp", "concealed"],
        "hype": ["hype", "knockout", "fight", "insanity", "beast", "turbo"],
        "chill": ["chill", "relax", "calm", "peaceful", "dreamy", "soft", "breeze",
                  "summer", "bright", "sunny", "love", "beautiful", "gentle", "sweet"],
        "lofi": ["lofi", "lo-fi", "lo fi", "study", "cozy", "campfire", "late night"],
        "ambient": ["ambient", "space", "ethereal", "atmospheric", "cosmic", "nebula", "float"],
        "orchestral": ["orchestral", "epic", "cinematic", "battle", "inbound", "war", "kingdom"],
        "electronic": ["electronic", "synth", "edm", "techno", "house", "trance",
                       "dubstep", "neon", "digital", "cyber"],
    }
    for genre, keywords in hints.items():
        if any(kw in title_lower for kw in keywords):
            return genre
    return "default"


def classify_genre_llm(track_title: str, bpm: float = 0.0,
                       energy: str = "", brightness: str = "",
                       texture: str = "") -> str:
    """Use LLM to classify into a visual mood using title + audio features."""
    try:
        from llm_client import get_llm_client, llm_available
        if not llm_available():
            return classify_genre(track_title)

        client, model = get_llm_client()
        moods = list(PEXELS_KEYWORDS.keys())

        audio_hint = ""
        if bpm > 0:
            audio_hint = (
                f"\nAudio: BPM={bpm:.0f}, energy={energy}, "
                f"brightness={brightness}, texture={texture}."
            )

        resp = client.chat.completions.create(
            model=model,
            messages=[{
                "role": "user",
                "content": (
                    f"Pick the VISUAL MOOD that best matches this music track. "
                    f"This determines what VIDEO FOOTAGE to show.\n"
                    f"Options: {moods}\n"
                    f"Track title: \"{track_title}\"{audio_hint}\n\n"
                    f"IMPORTANT: The track TITLE is your strongest signal. "
                    f"Words like Bright, Summer, Love, Chill = chill or lofi. "
                    f"Audio features can be misleading — a layered RnB song may "
                    f"measure as high energy even though it feels light.\n\n"
                    f"Guide:\n"
                    f"- chill: bright, summer, love, day, sun, breeze, relax, soft, peaceful\n"
                    f"- lofi: cozy, study, warm, nostalgic, rainy, coffee, late night\n"
                    f"- psychedelic: trippy, experimental, weird, acid, colorful, dream\n"
                    f"- dark: ghost, haunted, cursed, demon, horror, eerie, nightmare, doom\n"
                    f"- rock: guitars, punk, grunge, alt rock, concert energy\n"
                    f"- phonk: memphis, drift, cowbell, dark bass, skrt\n"
                    f"- hype: fight, knockout, fire, insane, turbo, beast\n"
                    f"- trap: hip-hop, drill, street, flex, luxury, gang\n"
                    f"- electronic: EDM, synth, techno, futuristic, cyber, neon\n"
                    f"- ambient: space, cosmos, ethereal, atmospheric, void, float\n"
                    f"- orchestral: epic, cinematic, battle, war, rise, kingdom\n"
                    f"- default: if nothing fits well\n\n"
                    f"Reply with ONLY one word from the list."
                ),
            }],
            max_tokens=20,
            temperature=0.0,
        )
        mood = resp.choices[0].message.content.strip().lower()
        if mood in PEXELS_KEYWORDS:
            return mood
        return classify_genre(track_title)
    except Exception as e:
        logger.warning(f"LLM genre classification failed: {e}")
        return classify_genre(track_title)


# ─── Pexels Source ───────────────────────────────────────────

def _pexels_search(query: str, per_page: int = 10,
                    orientation: str = "portrait") -> List[Dict]:
    """Search Pexels for videos matching query."""
    if not PEXELS_API_KEY:
        logger.error("PEXELS_API_KEY not set")
        return []

    headers = {"Authorization": PEXELS_API_KEY}
    params = {"query": query, "per_page": per_page, "orientation": orientation}
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


def _pexels_download(video_info: Dict, output_dir: str = "/tmp",
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


def _fetch_pexels(genre: str, num_clips: int, output_dir: str) -> List[str]:
    """Fetch clips from Pexels for a genre."""
    keywords = list(PEXELS_KEYWORDS.get(genre, PEXELS_KEYWORDS["default"]))
    random.shuffle(keywords)

    downloaded = []
    used_ids = set()

    for keyword in keywords:
        if len(downloaded) >= num_clips:
            break
        time.sleep(0.5)
        videos = _pexels_search(keyword, per_page=10, orientation="portrait")
        random.shuffle(videos)
        for video in videos:
            if len(downloaded) >= num_clips:
                break
            vid_id = video.get("id")
            if vid_id in used_ids:
                continue
            used_ids.add(vid_id)
            path = _pexels_download(video, output_dir=output_dir)
            if path:
                downloaded.append(path)

    return downloaded


# ─── Archive.org Source ──────────────────────────────────────

def _archive_search(query: str, collection: Optional[str] = None,
                     rows: int = 15) -> List[Dict]:
    """Search archive.org for public domain video content."""
    parts = [query, "mediatype:movies"]
    if collection:
        parts.append(f"collection:{collection}")
    # Prefer items with known PD license or pre-1928
    parts.append("(year:[1800 TO 1960] OR licenseurl:*publicdomain*)")

    q = " AND ".join(parts)
    params = {
        "q": q,
        "fl[]": ["identifier", "title", "year", "downloads"],
        "sort[]": "downloads desc",
        "rows": rows,
        "output": "json",
    }
    try:
        resp = requests.get(ARCHIVE_SEARCH_URL, params=params,
                            headers=ARCHIVE_HEADERS, timeout=30)
        resp.raise_for_status()
        return resp.json().get("response", {}).get("docs", [])
    except Exception as e:
        logger.error(f"Archive.org search failed for '{query}': {e}")
        return []


def _archive_get_video_url(identifier: str, max_size_mb: float = 50) -> Optional[str]:
    """Get the best video download URL for an archive.org item."""
    try:
        resp = requests.get(
            f"https://archive.org/metadata/{identifier}/files",
            headers=ARCHIVE_HEADERS, timeout=20,
        )
        resp.raise_for_status()
        files = resp.json().get("result", [])
    except Exception as e:
        logger.error(f"Archive.org metadata failed for {identifier}: {e}")
        return None

    video_exts = {".mp4", ".ogv", ".avi", ".mpeg", ".mpg"}
    candidates = []
    for f in files:
        name = f.get("name", "")
        ext = os.path.splitext(name)[1].lower()
        if ext not in video_exts:
            continue
        size_mb = int(f.get("size", 0)) / (1024 * 1024)
        if size_mb > max_size_mb or size_mb < 0.1:
            continue
        # Prefer mp4, smaller files
        priority = 0 if ext == ".mp4" else 1
        candidates.append((priority, size_mb, name))

    if not candidates:
        return None

    candidates.sort(key=lambda x: (x[0], x[1]))
    best_name = candidates[0][2]
    return f"https://archive.org/download/{identifier}/{quote_plus(best_name)}"


def _archive_download(identifier: str, output_dir: str = "/tmp") -> Optional[str]:
    """Download a video from archive.org."""
    output_path = os.path.join(output_dir, f"archive_{identifier}.mp4")
    if os.path.exists(output_path):
        return output_path

    url = _archive_get_video_url(identifier)
    if not url:
        return None

    try:
        logger.info(f"Downloading archive.org clip: {identifier}")
        resp = requests.get(url, stream=True, headers=ARCHIVE_HEADERS, timeout=120)
        resp.raise_for_status()
        with open(output_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
        size_mb = os.path.getsize(output_path) / (1024 * 1024)
        logger.info(f"Downloaded archive.org clip: {output_path} ({size_mb:.1f}MB)")
        return output_path
    except Exception as e:
        logger.error(f"Archive.org download failed for {identifier}: {e}")
        try:
            os.unlink(output_path)
        except OSError:
            pass
        return None


def _fetch_archive(genre: str, num_clips: int, output_dir: str) -> List[str]:
    """Fetch clips from archive.org for a genre."""
    queries = list(ARCHIVE_QUERIES.get(genre, ARCHIVE_QUERIES["default"]))
    random.shuffle(queries)

    downloaded = []
    used_ids = set()

    for query, collection in queries:
        if len(downloaded) >= num_clips:
            break
        time.sleep(1)  # be respectful to archive.org
        results = _archive_search(query, collection=collection, rows=8)
        random.shuffle(results)

        for item in results:
            if len(downloaded) >= num_clips:
                break
            identifier = item.get("identifier", "")
            if not identifier or identifier in used_ids:
                continue
            used_ids.add(identifier)

            path = _archive_download(identifier, output_dir=output_dir)
            if path:
                downloaded.append(path)

    return downloaded


# ─── Main Orchestrator ───────────────────────────────────────

def fetch_footage(track_title: str, num_clips: int = TARGET_CLIPS,
                   output_dir: str = "/tmp", bpm: float = 0.0,
                   energy: str = "", brightness: str = "",
                   texture: str = "") -> List[str]:
    """
    High-level: classify genre, fetch from both Pexels + Archive.org,
    shuffle together for variety.
    """
    genre = classify_genre_llm(track_title, bpm=bpm, energy=energy,
                                brightness=brightness, texture=texture)
    style = SOURCE_STYLE.get(genre, "pexels")
    logger.info(f"Genre: {genre} | Style: {style}")

    # Use ONE source for visual cohesion— all vintage OR all modern
    if style == "archive":
        total = _fetch_archive(genre, num_clips, output_dir)
        # Fall back to pexels if archive doesn't deliver enough
        if len(total) < num_clips:
            extra = _fetch_pexels(genre, num_clips - len(total), output_dir)
            total.extend(extra)
    else:
        total = _fetch_pexels(genre, num_clips, output_dir)

    random.shuffle(total)

    if not total:
        logger.warning("No footage from any source, trying fallback")
        videos = _pexels_search("abstract motion", per_page=10)
        for video in videos[:num_clips]:
            path = _pexels_download(video, output_dir=output_dir)
            if path:
                total.append(path)

    logger.info(f"Total footage: {len(total)} clips ({style})")
    return total
