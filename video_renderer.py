"""
Video Renderer — beat-synced music Short with color grading + text overlay
Composites Pexels footage clips cut to beat intervals, applies phonk/edit
aesthetic color grading via ffmpeg, adds track name overlay, and muxes
with the audio segment.

All heavy lifting is done by ffmpeg subprocess calls (fast, low RAM).
"""

import os
import gc
import time
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


def crop_to_vertical(clip_path: str, output_path: str) -> str:
    """
    Crop + color-grade a video clip to 9:16 (1080x1920) in one ffmpeg pass.
    Handles both vertical and horizontal source footage.
    """
    filter_chain = (
        f"scale=-2:{HEIGHT},"
        f"crop={WIDTH}:{HEIGHT},"
        f"{COLOR_GRADE_FILTERS}"
    )
    cmd = [
        "ffmpeg", "-y",
        "-i", clip_path,
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
    crop/grade it, and trim to the interval duration.
    Returns list of paths to trimmed segments.
    """
    segments = []
    n_clips = len(footage_paths)
    if n_clips == 0:
        logger.error("No footage clips available")
        return []

    for i, (start, end) in enumerate(beat_intervals):
        duration = end - start
        if duration <= 0:
            continue

        src_clip = footage_paths[i % n_clips]
        graded_path = os.path.join(output_dir, f"graded_{i}.mp4")
        segment_path = os.path.join(output_dir, f"beat_seg_{i}.mp4")

        try:
            crop_to_vertical(src_clip, graded_path)

            # Trim to beat duration; loop if clip is shorter than interval
            cmd = [
                "ffmpeg", "-y",
                "-stream_loop", "-1",
                "-i", graded_path,
                "-t", f"{duration:.3f}",
                "-c:v", "libx264",
                "-preset", "ultrafast",
                "-crf", "23",
                "-pix_fmt", "yuv420p",
                "-an",
                segment_path,
            ]
            subprocess.run(cmd, check=True, capture_output=True, timeout=60)
            segments.append(segment_path)

        except subprocess.CalledProcessError as e:
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
                      output_path: str) -> str:
    """
    Burn track name + artist text onto the video using ffmpeg drawtext.
    Positioned at bottom-center with semi-transparent background.
    """
    safe_track = track_name.replace("'", "\\'").replace(":", "\\:").replace('"', '\\"')
    safe_artist = artist.replace("'", "\\'").replace(":", "\\:").replace('"', '\\"')

    filter_text = (
        f"drawtext=text='{safe_track}':"
        f"fontsize=48:fontcolor=white:"
        f"borderw=2:bordercolor=black:"
        f"x=(w-text_w)/2:y=h-220:"
        f"box=1:boxcolor=black@0.4:boxborderw=12,"
        f"drawtext=text='{safe_artist}':"
        f"fontsize=32:fontcolor=#CCCCCC:"
        f"borderw=1:bordercolor=black:"
        f"x=(w-text_w)/2:y=h-155"
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
                  artist: str = "Star Drift",
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
        add_text_overlay(concat_path, track_name, artist, text_path)
    except subprocess.CalledProcessError as e:
        logger.error(f"Text overlay failed: {e}")
        text_path = concat_path  # fall back to no-text version

    # Step 4: Mux with audio
    final_name = f"stardrift_short_{ts}.mp4"
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
