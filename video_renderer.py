"""
Video Renderer — beat-synced music Short with color grading + text overlay
Composites Pexels footage clips cut to beat intervals, applies phonk/edit
aesthetic color grading via ffmpeg, adds track name overlay, and muxes
with the audio segment.

All heavy lifting is done by ffmpeg subprocess calls (fast, low RAM).
"""

import os
import gc
import math
import time
import random
import subprocess
import logging
from typing import List, Tuple, Optional

logger = logging.getLogger(__name__)

# Output dimensions (YouTube Shorts = 9:16)
WIDTH = 1080
HEIGHT = 1920
FPS = 30

# Color grading filter chain (phonk/edit aesthetic)
COLOR_GRADE_FILTERS = (
    "eq=saturation=0.6:contrast=1.3,"
    "colorbalance=rs=0.12:gs=-0.08:bs=0.22,"
    "vignette=PI/4"
)

# Occasional FX filters applied to random segments for variety
BEAT_FX = [
    None,  # no extra FX (most common)
    None,
    None,
    "negate",  # brief color inversion
    "hue=h=180",  # hue shift
    "eq=brightness=0.15:contrast=1.5",  # flash-bright
    "hflip",  # mirror
    "eq=saturation=0:contrast=1.4",  # B&W high contrast
]


def _get_clip_duration(clip_path: str) -> float:
    """Get duration of a video clip using ffprobe."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", clip_path],
            capture_output=True, text=True, timeout=10,
        )
        return float(result.stdout.strip())
    except Exception:
        return 0.0


def crop_to_vertical(clip_path: str, output_path: str,
                     seek_offset: float = 0.0, max_duration: float = 10.0,
                     extra_vf: str = "") -> str:
    """
    Crop + color-grade a video clip to 9:16 (1080x1920) in one ffmpeg pass.
    Handles both vertical and horizontal source footage.
    seek_offset: start this many seconds into the clip for variety.
    max_duration: only process this many seconds (avoids processing full-length archive clips).
    extra_vf: optional additional filter (beat FX).
    """
    filters = [
        f"scale=-2:{HEIGHT}",
        f"crop={WIDTH}:{HEIGHT}",
        COLOR_GRADE_FILTERS,
    ]
    if extra_vf:
        filters.append(extra_vf)
    filter_chain = ",".join(filters)

    cmd = [
        "ffmpeg", "-y",
    ]
    if seek_offset > 0:
        cmd += ["-ss", f"{seek_offset:.2f}"]
    cmd += [
        "-i", clip_path,
        "-t", f"{max_duration:.2f}",
        "-vf", filter_chain,
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "23",
        "-pix_fmt", "yuv420p",
        "-r", str(FPS),
        "-an",
        output_path,
    ]
    subprocess.run(cmd, check=True, capture_output=True, timeout=120)
    return output_path


def cut_footage_to_beats(footage_paths: List[str],
                          beat_intervals: List[Tuple[float, float]],
                          output_dir: str = "/tmp") -> List[str]:
    """
    For each beat interval, take the next footage clip (cycling),
    crop/grade it, and trim to EXACT frame count (prevents beat drift).
    Applies random seek offset into clips + occasional FX for variety.
    Returns list of paths to trimmed segments.
    """
    segments = []
    n_clips = len(footage_paths)
    if n_clips == 0:
        logger.error("No footage clips available")
        return []

    # Pre-compute clip durations for random seek offsets
    clip_durations = {}

    for i, (start, end) in enumerate(beat_intervals):
        duration = end - start
        if duration <= 0:
            continue

        # Exact frame count — prevents cumulative drift
        n_frames = round(duration * FPS)
        if n_frames <= 0:
            continue

        src_clip = footage_paths[i % n_clips]
        graded_path = os.path.join(output_dir, f"graded_{i}.mp4")
        segment_path = os.path.join(output_dir, f"beat_seg_{i}.mp4")

        # Random seek offset into the source clip for variety
        if src_clip not in clip_durations:
            clip_durations[src_clip] = _get_clip_duration(src_clip)
        src_dur = clip_durations[src_clip]
        max_seek = max(0, src_dur - duration - 1.0)
        seek = random.uniform(0, max_seek) if max_seek > 1.0 else 0.0

        # Occasional FX on some segments
        fx = random.choice(BEAT_FX)

        try:
            # Only process enough of the source clip for this beat + small buffer
            crop_to_vertical(src_clip, graded_path,
                             seek_offset=seek, max_duration=duration + 2.0,
                             extra_vf=fx or "")

            # Trim to EXACT frame count (not float duration)
            cmd = [
                "ffmpeg", "-y",
                "-stream_loop", "-1",
                "-i", graded_path,
                "-frames:v", str(n_frames),
                "-c:v", "libx264",
                "-preset", "ultrafast",
                "-crf", "23",
                "-pix_fmt", "yuv420p",
                "-r", str(FPS),
                "-an",
                segment_path,
            ]
            subprocess.run(cmd, check=True, capture_output=True, timeout=60)
            segments.append(segment_path)

        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            logger.warning(f"Failed to process segment {i}: {e}")
            continue
        finally:
            try:
                os.unlink(graded_path)
            except OSError:
                pass

    return segments


def concat_segments(segment_paths: List[str], output_path: str) -> str:
    """
    Concatenate video segments using ffmpeg concat demuxer + stream-copy.
    Same pattern as the stock/crypto projects (near-zero RAM).
    """
    concat_list = output_path + ".concat.txt"
    with open(concat_list, "w") as f:
        for seg in segment_paths:
            f.write(f"file '{seg}'\n")

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", concat_list,
        "-c", "copy",
        output_path,
    ]
    subprocess.run(cmd, check=True, capture_output=True, timeout=120)

    try:
        os.unlink(concat_list)
    except OSError:
        pass

    return output_path


def add_text_overlay(video_path: str, track_name: str, artist: str,
                      output_path: str, total_duration: float = 30.0) -> str:
    """
    Burn track name + end CTA onto the video using ffmpeg drawtext.
    - Song title: subtle, always visible, centered near bottom
    - CTA: "Stream now - link below" fades in during last 4 seconds
    """
    safe_track = track_name.replace("'", "\\'").replace(":", "\\:").replace('"', '\\"')

    cta_start = max(0, total_duration - 4.0)

    filter_text = (
        # Song title — always visible, subtle
        f"drawtext=text='{safe_track}':"
        f"fontsize=36:fontcolor=white@0.7:"
        f"borderw=1:bordercolor=black@0.5:"
        f"x=(w-text_w)/2:y=h-180,"
        # CTA — fades in last 4 seconds
        f"drawtext=text='Stream now \\- link below':"
        f"fontsize=42:fontcolor=white:"
        f"borderw=2:bordercolor=black@0.6:"
        f"x=(w-text_w)/2:y=h/2-21:"
        f"enable='gte(t,{cta_start:.1f})':"
        f"alpha='min(1,(t-{cta_start:.1f})/0.5)'"
    )

    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-vf", filter_text,
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "23",
        "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        output_path,
    ]
    subprocess.run(cmd, check=True, capture_output=True, timeout=120)
    return output_path


def mux_audio_video(video_path: str, audio_path: str,
                     output_path: str) -> str:
    """Mux concatenated video with the audio segment."""
    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-i", audio_path,
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "192k",
        "-shortest",
        output_path,
    ]
    subprocess.run(cmd, check=True, capture_output=True, timeout=120)
    return output_path


def render_short(audio_segment_path: str,
                  footage_paths: List[str],
                  beat_intervals: List[Tuple[float, float]],
                  track_name: str,
                  artist: str = "Unknown Artist",
                  output_dir: str = "/tmp") -> Optional[str]:
    """
    Top-level render function:
    1. Cut footage to beat intervals with color grading
    2. Concatenate via stream-copy
    3. Burn text overlay
    4. Mux with audio
    5. Cleanup temp files
    Returns path to final MP4, or None on failure.
    """
    ts = int(time.time())

    logger.info(f"Rendering Short: {track_name} by {artist}")
    logger.info(f"  Footage clips: {len(footage_paths)}")
    logger.info(f"  Beat intervals: {len(beat_intervals)}")

    # Step 1: Cut footage to beat intervals
    segments = cut_footage_to_beats(footage_paths, beat_intervals, output_dir)
    if not segments:
        logger.error("No segments rendered")
        return None

    # Step 2: Concatenate
    concat_path = os.path.join(output_dir, f"concat_{ts}.mp4")
    try:
        concat_segments(segments, concat_path)
    except subprocess.CalledProcessError as e:
        logger.error(f"Concat failed: {e}")
        return None

    # Step 3: Text overlay
    text_path = os.path.join(output_dir, f"text_{ts}.mp4")
    try:
        # Calculate total duration from beat intervals for CTA timing
        total_dur = beat_intervals[-1][1] if beat_intervals else 30.0
        add_text_overlay(concat_path, track_name, artist, text_path,
                         total_duration=total_dur)
    except subprocess.CalledProcessError as e:
        logger.error(f"Text overlay failed: {e}")
        text_path = concat_path  # fall back to no-text version

    # Step 4: Mux with audio
    final_name = f"music_short_{ts}.mp4"
    final_path = os.path.join(output_dir, final_name)
    try:
        mux_audio_video(text_path, audio_segment_path, final_path)
    except subprocess.CalledProcessError as e:
        logger.error(f"Audio mux failed: {e}")
        return None

    # Step 5: Cleanup intermediates
    for path in segments + [concat_path]:
        try:
            os.unlink(path)
        except OSError:
            pass
    if text_path != concat_path:
        try:
            os.unlink(text_path)
        except OSError:
            pass

    gc.collect()
    logger.info(f"Final Short rendered: {final_path}")
    return final_path
