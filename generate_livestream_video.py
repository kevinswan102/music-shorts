#!/usr/bin/env python3
"""
generate_livestream_video.py — Build a long-form livestream video from all stardrift tracks.

Renders full-length beat-synced videos for every track in the GitHub Release,
concatenates them into a single MP4 suitable for looping as a YouTube livestream.

Usage (locally or via GitHub Actions build-livestream.yml):
    python generate_livestream_video.py
"""

import os
import sys
import gc
import json
import logging
import subprocess
import requests
from typing import List, Optional

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("generate_livestream_video")

from music_source import list_channel_videos, download_audio, _get_release_assets
from beat_analyzer import analyze_track, get_beat_intervals
from footage_fetcher import fetch_footage, classify_genre_llm
from video_renderer import render_short
from generate_short import clean_song_title

ARTIST_NAME = os.getenv("ARTIST_NAME", "Unknown Artist")
REPO = os.getenv("GITHUB_REPOSITORY", "kevinswan102/music-shorts")
RELEASE_TAG = "audio-tracks"
OUTPUT_PATH = os.getenv("LIVESTREAM_OUTPUT", "/tmp/livestream.mp4")
UPLOAD_TO_RELEASE = os.getenv("UPLOAD_TO_RELEASE", "true").lower() == "true"
# More clips for full-length tracks (cycles with random seek so variety holds up)
FOOTAGE_CLIPS = int(os.getenv("FOOTAGE_CLIPS", "20"))
# Cap tracks per build run — 150+ tracks would exceed GitHub Actions 6h limit
MAX_TRACKS = int(os.getenv("MAX_TRACKS", "15"))


def get_all_available_ids() -> set:
    """Return set of track IDs that have audio uploaded to the GitHub Release."""
    assets = _get_release_assets()
    return {name.replace(".mp3", "") for name in assets if name.endswith(".mp3")}


def render_full_track(audio_path: str, song_title: str) -> Optional[str]:
    """
    Render a full-length beat-synced video for one track.
    Same pipeline as the shorts but window = full track (0 → duration).
    Returns path to final MP4, or None on failure.
    """
    logger.info(f"Analyzing: {song_title}")
    analysis = analyze_track(audio_path)
    bpm = analysis["bpm"]
    duration = analysis["duration"]
    energy = analysis.get("energy", "")
    brightness = analysis.get("brightness", "")
    texture = analysis.get("texture", "")

    genre = classify_genre_llm(song_title, bpm=bpm, energy=energy,
                                brightness=brightness, texture=texture)
    logger.info(f"Genre: {genre} | BPM: {bpm:.0f} | Duration: {duration:.1f}s")

    # Beat intervals for the FULL track (not a 30s window).
    # Livestream uses slower cuts (4-10s) vs Shorts (1.5-4s) — ambient background
    # listening needs clips to breathe, not rapid-fire edits.
    beat_intervals = get_beat_intervals(
        analysis["all_beat_times"],
        start_offset=0.0,
        segment_duration=duration,
        min_interval=4.0,
        max_interval=10.0,
        skip_ratio=0.75,  # skip more beats so clips hold even longer
    )
    logger.info(f"Beat intervals: {len(beat_intervals)} cuts over {duration:.0f}s")

    footage_paths = fetch_footage(
        song_title,
        num_clips=FOOTAGE_CLIPS,
        bpm=bpm,
        energy=energy,
        brightness=brightness,
        texture=texture,
    )
    if not footage_paths:
        logger.error(f"No footage fetched for '{song_title}', skipping.")
        return None

    # Generate cycling text sets — one fresh Reddit fact every 35s of the track.
    # Keeps viewers reading/engaged throughout the full track instead of just the first 30s.
    CYCLE_SECS = 35.0
    n_sets = max(1, int(duration / CYCLE_SECS))
    from generate_short import generate_multiple_overlay_texts
    poem_sets = generate_multiple_overlay_texts(n_sets)
    logger.info(f"Generated {len(poem_sets)} text blocks for {duration:.0f}s track")
    for idx, s in enumerate(poem_sets):
        logger.info(f"  Block {idx+1}: {' / '.join(s)}")

    # render_short works for any duration — same function used by the Shorts pipeline
    final_video = render_short(
        audio_segment_path=audio_path,
        footage_paths=footage_paths,
        beat_intervals=beat_intervals,
        track_name=song_title,
        artist=ARTIST_NAME,
        genre=genre,
        bpm=bpm,
        output_dir="/tmp",
        poem_sets=poem_sets,
    )

    for path in footage_paths:
        try:
            os.unlink(path)
        except OSError:
            pass
    gc.collect()

    return final_video


def concat_all(track_videos: List[str], output_path: str) -> str:
    """Concatenate rendered track videos into one master file, re-encoding at lower bitrate.

    Re-encoding at 2500kbps video + 128kbps audio keeps quality solid for a livestream
    while shrinking a 40-min 9Mbps original (~2.5GB) down to ~800MB — small enough to
    upload to a GitHub Release reliably.
    """
    concat_list = "/tmp/ls_concat.txt"
    with open(concat_list, "w") as f:
        for v in track_videos:
            f.write(f"file '{v}'\n")

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", concat_list,
        # Re-encode at ~2500kbps video + 128kbps audio instead of stream-copying at 9+Mbps
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-b:v", "2500k",
        "-maxrate", "2500k",
        "-bufsize", "5000k",
        "-pix_fmt", "yuv420p",
        "-g", "60",
        "-c:a", "aac",
        "-b:a", "128k",
        "-ar", "48000",
        output_path,
    ]
    subprocess.run(cmd, check=True, timeout=7200)  # up to 2h for long files
    try:
        os.unlink(concat_list)
    except OSError:
        pass
    return output_path


def upload_to_release(file_path: str) -> bool:
    """Upload livestream.mp4 to the GitHub Release using gh CLI.

    gh handles large-file uploads far more reliably than raw requests.post() —
    it uses chunked transfer and built-in retries.
    --clobber replaces any existing asset with the same name.
    """
    github_token = os.getenv("GITHUB_TOKEN", "")
    if not github_token:
        logger.error("GITHUB_TOKEN not set — skipping release upload")
        return False

    file_size = os.path.getsize(file_path)
    logger.info(f"Uploading livestream.mp4 ({file_size / (1024*1024):.0f} MB) via gh CLI ...")

    env = os.environ.copy()
    env["GH_TOKEN"] = github_token  # gh CLI reads GH_TOKEN

    result = subprocess.run(
        [
            "gh", "release", "upload", RELEASE_TAG,
            file_path,
            "--repo", REPO,
            "--clobber",          # replace existing asset with same name
        ],
        env=env,
        timeout=3600,             # 1h upload timeout
        capture_output=True,
        text=True,
    )

    if result.returncode == 0:
        logger.info(f"Upload complete.")
        if result.stdout.strip():
            logger.info(result.stdout.strip())
        return True

    logger.error(f"Upload failed (exit {result.returncode}):")
    if result.stderr:
        logger.error(result.stderr[-500:])
    return False


def generate_stream_meta(track_titles: List[str]) -> dict:
    """Generate a YouTube title + description for the livestream using the LLM."""
    artist = ARTIST_NAME
    track_list = "\n".join(f"• {t}" for t in track_titles) if track_titles else "• (tracks loading)"

    # Try LLM first
    try:
        from llm_client import get_llm_client, llm_available
        if llm_available():
            client, model = get_llm_client()
            resp = client.chat.completions.create(
                model=model,
                messages=[{
                    "role": "user",
                    "content": (
                        f"Write a YouTube LIVE stream title and description for {artist}'s music.\n"
                        f"Tracks in this stream: {', '.join(track_titles[:8])}.\n\n"
                        f"Rules:\n"
                        f"- Title: max 80 chars, include artist name, '24/7 LIVE', mood (chill/phonk/lofi), "
                        f"and 1-2 emojis. No year.\n"
                        f"- Description: 3-4 lines. Mention it loops 24/7, describe the vibe, "
                        f"say 'subscribe for more'. Sound human, not corporate.\n\n"
                        f"Reply in this exact format:\n"
                        f"TITLE: <title here>\n"
                        f"DESC: <description here>"
                    ),
                }],
                max_tokens=200,
                temperature=0.7,
            )
            raw = resp.choices[0].message.content.strip()
            title, desc = "", ""
            for line in raw.split("\n"):
                if line.startswith("TITLE:"):
                    title = line[6:].strip()[:100]
                elif line.startswith("DESC:"):
                    desc = line[5:].strip()
            if title:
                logger.info(f"LLM title: {title}")
                return {"title": title, "description": desc, "track_list": track_list}
    except Exception as e:
        logger.warning(f"LLM meta generation failed: {e}")

    # Fallback
    title = f"🎵 {artist} — 24/7 Chill Music LIVE | Beat-Synced Visuals"[:100]
    desc = (
        f"Looping 24/7 — {artist} beats with beat-synced visuals.\n"
        f"New tracks added regularly. Subscribe so you never miss a drop.\n"
        f"Best with headphones on."
    )
    return {"title": title, "description": desc, "track_list": track_list}


def upload_meta_to_release(meta: dict) -> bool:
    """Upload livestream_meta.json to the GitHub Release so stream-live.yml can read it."""
    github_token = os.getenv("GITHUB_TOKEN", "")
    if not github_token:
        return False

    meta_path = "/tmp/livestream_meta.json"
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)

    env = os.environ.copy()
    env["GH_TOKEN"] = github_token
    result = subprocess.run(
        ["gh", "release", "upload", RELEASE_TAG, meta_path,
         "--repo", REPO, "--clobber"],
        env=env, timeout=60, capture_output=True, text=True,
    )
    if result.returncode == 0:
        logger.info("Uploaded livestream_meta.json to release")
        return True
    logger.warning(f"Meta upload failed: {result.stderr[-200:]}")
    return False


def main():
    logger.info("=" * 60)
    logger.info("STARDRIFT LIVESTREAM VIDEO BUILDER")
    logger.info("=" * 60)

    # Get channel metadata (titles) for all tracks
    logger.info("Scanning channel for track metadata...")
    videos = list_channel_videos()
    video_meta = {v["id"]: v for v in videos}
    logger.info(f"Channel metadata: {len(video_meta)} tracks")

    # Only process tracks that have audio uploaded to the release
    available_ids = get_all_available_ids()
    logger.info(f"Tracks with uploaded audio: {len(available_ids)}")

    if not available_ids:
        logger.error("No audio files found in GitHub Release. Run upload_tracks.py first.")
        sys.exit(1)

    track_videos = []
    rendered_titles = []
    processed = 0

    ids_to_process = sorted(available_ids)[:MAX_TRACKS]
    logger.info(f"Processing {len(ids_to_process)} tracks (MAX_TRACKS={MAX_TRACKS})")

    for i, track_id in enumerate(ids_to_process):
        meta = video_meta.get(track_id)
        raw_title = meta["title"] if meta else track_id
        song_title = clean_song_title(raw_title)
        track_url = (meta["url"] if meta
                     else f"https://www.youtube.com/watch?v={track_id}")

        logger.info(f"\n[{i+1}/{len(available_ids)}] {song_title} (ID: {track_id})")

        audio_path = download_audio(track_url, output_dir="/tmp")
        if not audio_path:
            logger.warning(f"  Audio download failed for {track_id}, skipping.")
            continue

        video_path = render_full_track(audio_path, song_title)

        try:
            os.unlink(audio_path)
        except OSError:
            pass

        if video_path:
            track_videos.append(video_path)
            rendered_titles.append(song_title)
            processed += 1
            logger.info(f"  Rendered: {video_path}")
        else:
            logger.warning(f"  Render failed for {song_title}, skipping.")

    if not track_videos:
        logger.error("No tracks rendered successfully. Exiting.")
        sys.exit(1)

    logger.info(f"\nConcatenating {len(track_videos)} tracks into {OUTPUT_PATH} ...")
    concat_all(track_videos, OUTPUT_PATH)

    for v in track_videos:
        try:
            os.unlink(v)
        except OSError:
            pass
    gc.collect()

    size_mb = os.path.getsize(OUTPUT_PATH) / (1024 * 1024)
    logger.info(f"Livestream video ready: {OUTPUT_PATH} ({size_mb:.0f} MB)")
    logger.info(f"Tracks rendered: {processed}/{len(available_ids)}")

    # Generate title + description from track list
    logger.info("Generating stream title and description...")
    stream_meta = generate_stream_meta(rendered_titles)

    # Append affiliate links + music links to description
    spotify_url   = os.getenv("SPOTIFY_URL", "").strip()
    apple_url     = os.getenv("APPLE_MUSIC_URL", "").strip()
    beatstars_url = os.getenv("BEATSTARS_URL", "").strip()
    hyperfollow   = os.getenv("HYPERFOLLOW_URL", "").strip()
    freecash_url  = os.getenv("FREECASH_URL", "").strip()
    coinbase_url  = os.getenv("AFFILIATE_COINBASE", "").strip()
    cryptocom_url = os.getenv("AFFILIATE_CRYPTOCOM", "https://crypto.com/app/3d2tscf727").strip()
    kalshi_url    = os.getenv("AFFILIATE_KALSHI", "").strip()
    paypal_url    = os.getenv("AFFILIATE_PAYPAL", "").strip()

    extra_lines = ["\n━━━━━━━━━━━━━━━━━━━━━━━"]

    # Music / artist links
    music_links = []
    if spotify_url:
        music_links.append(f"🎧 Spotify: {spotify_url}")
    if apple_url:
        music_links.append(f"🍎 Apple Music: {apple_url}")
    if beatstars_url:
        music_links.append(f"🎹 BeatStars: {beatstars_url}")
    if hyperfollow:
        music_links.append(f"🔗 All platforms: {hyperfollow}")
    if music_links:
        extra_lines += ["🎵 Stream the music:"] + music_links

    # Track listing
    extra_lines += [
        "\n🎶 Tracks in this stream:",
        stream_meta.get("track_list", ""),
    ]

    # Affiliate links
    aff_lines = []
    if coinbase_url:
        aff_lines.append(f"💰 Coinbase — $10 free Bitcoin on signup: {coinbase_url}")
    aff_lines.append(f"🪙 Crypto.com — up to $100 welcome bonus: {cryptocom_url}")
    if kalshi_url:
        aff_lines.append(f"🎯 Kalshi — up to $25 trade bonus: {kalshi_url}")
    if freecash_url:
        aff_lines.append(f"🎁 Freecash — $10 free on signup: {freecash_url}")
    if paypal_url:
        aff_lines.append(f"💸 PayPal — spend $5 get $10 back (new users): {paypal_url}")
    if aff_lines:
        extra_lines += ["\n📌 Support the channel (free to sign up):"] + aff_lines
    stream_meta["description"] = stream_meta.get("description", "") + "\n" + "\n".join(extra_lines)

    logger.info("=" * 60)
    logger.info(f"STREAM TITLE:  {stream_meta['title']}")
    logger.info(f"STREAM DESC:\n{stream_meta['description']}")
    logger.info("=" * 60)

    if UPLOAD_TO_RELEASE:
        ok = upload_to_release(OUTPUT_PATH)
        if not ok:
            logger.error("Release upload failed.")
            sys.exit(1)
        upload_meta_to_release(stream_meta)
    else:
        logger.info("UPLOAD_TO_RELEASE=false — video saved locally only.")

    logger.info("Done.")


if __name__ == "__main__":
    main()
