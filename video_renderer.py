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

# Visual themes — one is picked per video for cohesion
# Each theme has a color grade override and optional per-segment accent
VISUAL_THEMES = {
    "phonk": {
        "grade": "eq=saturation=0.4:contrast=1.5,colorbalance=rs=0.15:gs=-0.10:bs=0.25,vignette=PI/3",
        "accent": "eq=brightness=0.12:contrast=1.6",  # occasional flash
        "accent_chance": 0.15,
    },
    "hype": {
        "grade": "eq=saturation=1.2:contrast=1.4,colorbalance=rs=0.08:gs=0.05:bs=-0.05,vignette=PI/5",
        "accent": "eq=brightness=0.15:contrast=1.5",
        "accent_chance": 0.2,
    },
    "chill": {
        "grade": "eq=saturation=0.7:contrast=1.1,colorbalance=rs=-0.03:gs=0.04:bs=0.10,vignette=PI/4",
        "accent": None,
        "accent_chance": 0.0,
    },
    "lofi": {
        "grade": "eq=saturation=0.5:contrast=1.2,colorbalance=rs=0.06:gs=0.02:bs=0.08,vignette=PI/3,noise=alls=12:allf=t",
        "accent": None,
        "accent_chance": 0.0,
    },
    "trap": {
        "grade": "eq=saturation=0.8:contrast=1.3,colorbalance=rs=0.05:gs=-0.05:bs=0.15,vignette=PI/4",
        "accent": "hflip",
        "accent_chance": 0.1,
    },
    "ambient": {
        "grade": "eq=saturation=0.6:contrast=1.0,colorbalance=rs=-0.05:gs=0.0:bs=0.12,vignette=PI/5",
        "accent": None,
        "accent_chance": 0.0,
    },
    "electronic": {
        "grade": "eq=saturation=1.3:contrast=1.3,colorbalance=rs=-0.05:gs=0.08:bs=0.20,vignette=PI/4",
        "accent": "hue=h=30",
        "accent_chance": 0.12,
    },
    "orchestral": {
        "grade": "eq=saturation=0.5:contrast=1.4,colorbalance=rs=0.10:gs=0.02:bs=-0.03,vignette=PI/3",
        "accent": None,
        "accent_chance": 0.0,
    },
    "psychedelic": {
        "grade": "eq=saturation=1.8:contrast=1.2,colorbalance=rs=0.10:gs=0.10:bs=0.15,vignette=PI/5",
        "accent": "hue=h=60",
        "accent_chance": 0.25,
    },
    "dark": {
        "grade": "eq=saturation=0.3:contrast=1.6:brightness=-0.05,colorbalance=rs=0.05:gs=-0.08:bs=0.02,vignette=PI/3",
        "accent": "eq=brightness=-0.1:contrast=1.7",
        "accent_chance": 0.1,
    },
    "rock": {
        "grade": "eq=saturation=0.9:contrast=1.4,colorbalance=rs=0.08:gs=0.0:bs=-0.05,vignette=PI/4",
        "accent": "eq=brightness=0.1:contrast=1.5",
        "accent_chance": 0.15,
    },
    "default": {
        "grade": COLOR_GRADE_FILTERS,
        "accent": None,
        "accent_chance": 0.0,
    },
}


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
                     extra_vf: str = "", grade_override: str = "") -> str:
    """
    Crop + color-grade a video clip to 9:16 (1080x1920) in one ffmpeg pass.
    Handles both vertical and horizontal source footage.
    seek_offset: start this many seconds into the clip for variety.
    max_duration: only process this many seconds (avoids processing full-length archive clips).
    extra_vf: optional additional filter (beat FX).
    grade_override: replaces default color grade with genre-specific theme.
    """
    grade = grade_override or COLOR_GRADE_FILTERS
    filters = [
        f"scale=-2:{HEIGHT}",
        f"crop={WIDTH}:{HEIGHT}",
        grade,
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
                          output_dir: str = "/tmp",
                          genre: str = "default") -> List[str]:
    """
    For each beat interval, assign a unique footage clip (no reuse),
    crop/grade it with genre-specific theme, and trim to EXACT frame count.
    If more intervals than clips, caps at available clips.
    Returns list of paths to trimmed segments.
    """
    segments = []
    n_clips = len(footage_paths)
    if n_clips == 0:
        logger.error("No footage clips available")
        return []

    # Pick cohesive visual theme for this video based on genre
    theme = VISUAL_THEMES.get(genre, VISUAL_THEMES["default"])
    logger.info(f"Visual theme: {genre}")

    # Shuffle clip assignment so it's not always the same order
    # Clips cycle via i % n_clips — all beat intervals are kept, clips repeat as needed
    clip_order = list(range(n_clips))
    random.shuffle(clip_order)

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

        src_clip = footage_paths[clip_order[i % n_clips]]
        graded_path = os.path.join(output_dir, f"graded_{i}.mp4")
        segment_path = os.path.join(output_dir, f"beat_seg_{i}.mp4")

        # Random seek offset into the source clip for variety
        if src_clip not in clip_durations:
            clip_durations[src_clip] = _get_clip_duration(src_clip)
        src_dur = clip_durations[src_clip]
        max_seek = max(0, src_dur - duration - 1.0)
        seek = random.uniform(0, max_seek) if max_seek > 1.0 else 0.0

        # Occasional themed accent (not random jarring FX)
        fx = ""
        if theme["accent"] and random.random() < theme["accent_chance"]:
            fx = theme["accent"]

        try:
            # Only process enough of the source clip for this beat + small buffer
            crop_to_vertical(src_clip, graded_path,
                             seek_offset=seek, max_duration=duration + 2.0,
                             extra_vf=fx, grade_override=theme["grade"])

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


def _find_font() -> str:
    """Return the best available font path for ffmpeg drawtext."""
    candidates = [
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",  # macOS fallback
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return ""  # let ffmpeg use its built-in (last resort)


def add_text_overlay(video_path: str, track_name: str, artist: str,
                      output_path: str, total_duration: float = 30.0,
                      poem_lines: list = None, bpm: float = 0) -> str:
    """
    Burn text overlays onto the video using ffmpeg drawtext.
    - Subscribe badge: top-left corner, always visible
    - Poem/fun fact lines: appear one at a time mid-screen
    - Song title: visible from start, bottom
    - Artist name: fades in later, bottom
    - CTA: last 4 seconds, centered
    """
    font = _find_font()
    font_param = f"fontfile={font}:" if font else ""

    def _escape(text: str) -> str:
        return (text
                .replace("\\", "\\\\")
                .replace("'", "\u2019")
                .replace(":", "\\:")
                .replace('"', '\\"'))

    safe_track = _escape(track_name)
    safe_artist = _escape(artist)
    cta_start = max(0, total_duration - 4.0)

    # Artist fade: invisible until artist_in, then fades in over 1.5s
    artist_in = min(6.0, total_duration * 0.3)
    artist_fade_end = artist_in + 1.5

    filters = []

    # Subscribe badge — top-left corner, discreet but always visible
    # Red pill with white text: "▶ SUBSCRIBE"
    filters.append(
        f"drawtext={font_param}text='SUBSCRIBE':"
        f"fontsize=34:fontcolor=white:"
        f"box=1:boxcolor=0xff0000@0.80:boxborderw=14:"
        f"x=28:y=58"
    )
    # Small up-arrow above it hinting to tap
    filters.append(
        f"drawtext={font_param}text='▶  tap to subscribe':"
        f"fontsize=22:fontcolor=white@0.65:"
        f"borderw=2:bordercolor=black@0.5:"
        f"x=28:y=118"
    )

    # Fun fact / poem lines — appear one per bar, synced to musical structure
    if poem_lines:
        n = len(poem_lines)
        if bpm > 0:
            bar_dur = 4 * (60.0 / bpm)  # 4 beats per bar
        else:
            bar_dur = 4.0

        poem_y_start = 520  # center of screen
        line_spacing = 90

        for i, line in enumerate(poem_lines):
            safe_line = _escape(line)
            appear_at = (2 + i) * bar_dur
            max_start = total_duration * 0.6
            if appear_at > max_start:
                appear_at = max_start - (n - 1 - i) * 1.5
                appear_at = max(1.0, appear_at)
            disappear_at = total_duration - bar_dur
            y_pos = poem_y_start + i * line_spacing
            filters.append(
                f"drawtext={font_param}text='{safe_line}':"
                f"fontsize=68:fontcolor=white:"
                f"borderw=5:bordercolor=black@0.8:"
                f"box=1:boxcolor=black@0.30:boxborderw=14:"
                f"x=(w-text_w)/2:y={y_pos}:"
                f"enable='between(t\\,{appear_at:.2f}\\,{disappear_at:.2f})'"
            )

    # Song title — visible from start, bottom of screen
    filters.append(
        f"drawtext={font_param}text='{safe_track}':"
        f"fontsize=60:fontcolor=white@0.95:"
        f"borderw=3:bordercolor=black@0.7:"
        f"box=1:boxcolor=black@0.40:boxborderw=12:"
        f"x=(w-text_w)/2:y=h-240"
    )

    # Artist name — fades in at ~30% through
    filters.append(
        f"drawtext={font_param}text='{safe_artist}':"
        f"fontsize=44:fontcolor=white:"
        f"borderw=2:bordercolor=black@0.5:"
        f"x=(w-text_w)/2:y=h-168:"
        f"enable='gte(t\\,{artist_in:.1f})':"
        f"alpha='if(gte(t\\,{artist_fade_end:.1f})\\,0.65\\,(t-{artist_in:.1f})/{artist_fade_end - artist_in:.1f}*0.65)'"
    )

    # CTA — appears last 4 seconds, centered
    filters.append(
        f"drawtext={font_param}text='Stream now - link in bio':"
        f"fontsize=54:fontcolor=white@0.9:"
        f"borderw=3:bordercolor=black@0.7:"
        f"box=1:boxcolor=black@0.45:boxborderw=14:"
        f"x=(w-text_w)/2:y=h/2-30:"
        f"enable='gte(t\\,{cta_start:.1f})'"
    )

    filter_text = ",".join(filters)

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
                     output_path: str, total_duration: float = 30.0) -> str:
    """Mux video with audio.
    We loop the video if it's shorter than the audio (can happen when beat segments
    don't perfectly cover the full track), then trim the whole output to the exact
    audio duration so the song always plays completely.
    """
    cmd = [
        "ffmpeg", "-y",
        "-stream_loop", "-1",   # loop video indefinitely if shorter than audio
        "-i", video_path,
        "-i", audio_path,
        "-map", "0:v:0",        # video from first input
        "-map", "1:a:0",        # audio from second input
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "23",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "192k",
        "-t", str(total_duration),   # end exactly when song ends
        output_path,
    ]
    subprocess.run(cmd, check=True, capture_output=True, timeout=300)
    return output_path


def render_short(audio_segment_path: str,
                  footage_paths: List[str],
                  beat_intervals: List[Tuple[float, float]],
                  track_name: str,
                  artist: str = "Unknown Artist",
                  genre: str = "default",
                  poem_lines: list = None,
                  bpm: float = 0,
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

    # Step 1: Cut footage to beat intervals with genre-specific theme
    segments = cut_footage_to_beats(footage_paths, beat_intervals, output_dir,
                                     genre=genre)
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
                         total_duration=total_dur, poem_lines=poem_lines, bpm=bpm)
    except subprocess.CalledProcessError as e:
        logger.error(f"Text overlay failed: {e}")
        text_path = concat_path  # fall back to no-text version

    # Step 4: Mux with audio
    final_name = f"music_short_{ts}.mp4"
    final_path = os.path.join(output_dir, final_name)
    try:
        mux_audio_video(text_path, audio_segment_path, final_path,
                        total_duration=total_dur)
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
