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
OVERLAY_MAX_LINES = int(os.getenv("OVERLAY_MAX_LINES", "5"))
MIN_VISUAL_SEGMENT_SECONDS = float(os.getenv("MIN_VISUAL_SEGMENT_SECONDS", "0.4"))

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


def _ken_burns_filter(duration: float, direction: str = "in",
                      intensity: float = 0.08) -> str:
    """
    Slow zoom (Ken Burns) via zoompan filter.
    intensity: how much to zoom (0.05 = subtle, 0.12 = noticeable).
    """
    fps = FPS
    total_frames = max(1, round(duration * fps))
    if direction == "in":
        zoom_expr = f"1+{intensity}*on/{total_frames}"
    else:
        zoom_expr = f"{1 + intensity}-{intensity}*on/{total_frames}"
    return (
        f"zoompan=z='{zoom_expr}'"
        f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
        f":d=1:s={WIDTH}x{HEIGHT}:fps={fps}"
    )


# Genre → visual energy style.
# "fast" = snappy cuts, stronger zoom. "slow" = longer holds, gentle zoom.
GENRE_ENERGY = {
    "phonk": "fast", "hype": "fast", "trap": "fast", "electronic": "fast",
    "rock": "fast", "dark": "fast",
    "chill": "slow", "lofi": "slow", "ambient": "slow", "rnb": "slow",
    "orchestral": "slow", "psychedelic": "slow",
    "default": "medium",
}


def crop_to_vertical(clip_path: str, output_path: str,
                     seek_offset: float = 0.0, max_duration: float = 10.0,
                     extra_vf: str = "", grade_override: str = "",
                     ken_burns: str = "", kb_intensity: float = 0.08) -> str:
    """
    Crop + color-grade a video clip to 9:16 (1080x1920) in one ffmpeg pass.
    Handles both vertical and horizontal source footage.
    seek_offset: start this many seconds into the clip for variety.
    max_duration: only process this many seconds (avoids processing full-length archive clips).
    extra_vf: optional additional filter (beat FX).
    grade_override: replaces default color grade with genre-specific theme.
    ken_burns: "in", "out", or "" for slow zoom direction.
    kb_intensity: zoom amount (0.05=subtle, 0.12=punchy).
    """
    grade = grade_override or COLOR_GRADE_FILTERS

    if ken_burns:
        filters = [
            f"scale=-2:{HEIGHT + 200}",
            f"crop={WIDTH + 100}:{HEIGHT + 200}",
            _ken_burns_filter(max_duration, direction=ken_burns,
                              intensity=kb_intensity),
            grade,
        ]
    else:
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


def _merge_short_intervals(beat_intervals: List[Tuple[float, float]],
                           min_duration: float = MIN_VISUAL_SEGMENT_SECONDS) -> List[Tuple[float, float]]:
    """Merge tiny beat cuts so no source clip flashes for a split second."""
    merged: List[Tuple[float, float]] = []
    for start, end in beat_intervals:
        if end <= start:
            continue
        if merged and end - start < min_duration:
            prev_start, _ = merged[-1]
            merged[-1] = (prev_start, end)
        else:
            merged.append((start, end))

    if len(merged) > 1 and merged[0][1] - merged[0][0] < min_duration:
        first_start, _ = merged[0]
        _, second_end = merged[1]
        merged[1] = (first_start, second_end)
        merged.pop(0)

    return merged


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

    stable_intervals = _merge_short_intervals(beat_intervals)
    if len(stable_intervals) != len(beat_intervals):
        logger.info("Merged %d short visual cuts", len(beat_intervals) - len(stable_intervals))

    for i, (start, end) in enumerate(stable_intervals):
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

        # Alternate zoom in/out between clips for visual variety
        kb_direction = "in" if i % 2 == 0 else "out"
        energy_style = GENRE_ENERGY.get(genre, "medium")
        if energy_style == "fast":
            kb_intensity = 0.12
        elif energy_style == "slow":
            kb_intensity = 0.05
        else:
            kb_intensity = 0.08

        try:
            # Only process enough of the source clip for this beat + small buffer
            crop_to_vertical(src_clip, graded_path,
                             seek_offset=seek, max_duration=duration + 2.0,
                             extra_vf=fx, grade_override=theme["grade"],
                             ken_burns=kb_direction, kb_intensity=kb_intensity)

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
    """Return a heavy/black weight font for ffmpeg drawtext.
    Prioritizes Montserrat Black or similar thick geometric sans —
    these pop on video like modern Shorts/TikTok text.
    """
    candidates = [
        # Montserrat Black — the go-to for modern short-form video text.
        # Install on CI: fonts-montserrat or download from Google Fonts.
        "/usr/share/fonts/truetype/montserrat/Montserrat-Black.ttf",
        "/usr/share/fonts/truetype/montserrat/Montserrat-ExtraBold.ttf",
        "/usr/local/share/fonts/Montserrat-Black.ttf",
        # Bebas Neue — tall condensed, looks great for overlays.
        "/usr/share/fonts/truetype/bebas-neue/BebasNeue-Regular.ttf",
        "/usr/local/share/fonts/BebasNeue-Regular.ttf",
        # Inter Black — clean, modern, widely available.
        "/usr/share/fonts/truetype/inter/Inter-Black.ttf",
        "/usr/local/share/fonts/Inter-Black.ttf",
        # Poppins Bold/Black.
        "/usr/share/fonts/truetype/poppins/Poppins-Black.ttf",
        "/usr/local/share/fonts/Poppins-Black.ttf",
        # Fallbacks that still look decent (bold weight).
        "/usr/share/fonts/truetype/noto/NotoSans-Black.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-ExtraBold.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        # macOS — SF Pro Heavy or Helvetica Neue Bold.
        "/System/Library/Fonts/SFCompact-Heavy.otf",
        "/Library/Fonts/SF-Pro-Display-Heavy.otf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return ""


def _wrap_overlay_text(text: str, max_chars: int = 28) -> list:
    """
    Word-wrap a line of overlay text so no single line overflows the video frame.
    Returns a list of wrapped lines (usually 1–3).
    """
    words = text.split()
    lines = []
    current = []
    current_len = 0
    for word in words:
        needed = len(word) + (1 if current else 0)
        if current and current_len + needed > max_chars:
            lines.append(" ".join(current))
            current = [word]
            current_len = len(word)
        else:
            current.append(word)
            current_len += needed
    if current:
        lines.append(" ".join(current))
    return lines or [text]


def _fit_font_size(text: str, base_size: int, min_size: int,
                   max_chars: int) -> int:
    """Crude drawtext fit guard for long titles in a 1080px Shorts frame."""
    clean_len = max(1, len(text.strip()))
    if clean_len <= max_chars:
        return base_size
    return max(min_size, int(base_size * max_chars / clean_len))


def add_text_overlay(video_path: str, track_name: str, artist: str,
                      output_path: str, total_duration: float = 30.0,
                      poem_lines: list = None, bpm: float = 0,
                      poem_sets: list = None,
                      overlay_max_lines: int = None,
                      overlay_mode: str = "") -> str:
    """
    Burn text overlays onto the video using ffmpeg drawtext.
    - Interesting text appears early and stays readable.
    - Bottom label shows the current song and artist.

    poem_sets: list of poem_lines lists — used for long-form content (livestream).
               Each set is shown for CYCLE_SECS seconds, one after another.
               If provided, overrides poem_lines.
    poem_lines: single list of text lines — used for Shorts (< 60s).
    """
    font = _find_font()
    font_param = f"fontfile={font}:" if font else ""
    overlay_max_lines = overlay_max_lines or OVERLAY_MAX_LINES

    def _escape(text: str) -> str:
        return (text
                .replace("\\", "\\\\")
                .replace("'", "\u2019")
                .replace(":", "\\:")
                .replace('"', '\\"'))

    safe_artist = _escape(artist)
    has_center_overlay = bool(poem_lines) and not poem_sets
    if total_duration < 14.0:
        hook_end = max(7.0, total_duration - 1.5)
    else:
        hook_end = min(max(12.0, total_duration * 0.70),
                       total_duration - 2.0)

    filters = []

    # Resolve which text sets to show
    # poem_sets = multiple blocks for long videos; poem_lines = single block for Shorts
    CYCLE_SECS = 35.0  # how long each text block stays visible in long videos

    if poem_sets and len(poem_sets) > 0:
        all_sets = poem_sets
    elif poem_lines:
        all_sets = [poem_lines]
    else:
        all_sets = []

    is_short_mode = has_center_overlay
    if bpm > 0:
        bar_dur = 4 * (60.0 / bpm)
    else:
        bar_dur = 4.0

    poem_y_start = 300  # high enough to hit before the thumb scrolls
    line_spacing = 105  # enough room for five lines without cutting off

    for set_idx, lines in enumerate(all_sets):
        # Time window this set is visible
        slot_start = set_idx * CYCLE_SECS
        if is_short_mode:
            slot_end = hook_end
        else:
            slot_end = min(slot_start + CYCLE_SECS, total_duration - bar_dur)

        if slot_start >= total_duration - 2.0:
            break  # past end of video

        # Expand each source line into word-wrapped sub-lines
        # Use same max_chars as generator (22) to avoid overflow
        wrapped_lines = []
        for line in lines:
            wrapped_lines.extend(_wrap_overlay_text(line, max_chars=22))
        if is_short_mode:
            wrapped_lines = wrapped_lines[:overlay_max_lines]

        n = len(wrapped_lines)
        for i, sub_line in enumerate(wrapped_lines):
            safe_line = _escape(sub_line)
            # Shorts: stagger lines so viewer can read each one as it appears.
            # ~2.2s between lines — slow reveal that matches the music vibe.
            if is_short_mode:
                appear_at = slot_start + 0.8 + i * 2.2
            else:
                appear_at = slot_start + (1 + i) * bar_dur
                if appear_at > slot_end - 1.0:
                    appear_at = slot_end - (n - i) * 1.2
                appear_at = max(slot_start + 0.5, appear_at)
            disappear_at = slot_end
            y_pos = poem_y_start + i * line_spacing
            base_size = 64
            min_size = 44
            font_size = _fit_font_size(sub_line, base_size=base_size,
                                       min_size=min_size, max_chars=24)
            filters.append(
                f"drawtext={font_param}text='{safe_line}':"
                f"fontsize={font_size}:fontcolor=white:"
                f"borderw=6:bordercolor=black:"
                f"x=(w-text_w)/2:y={y_pos}:"
                f"enable='between(t\\,{appear_at:.2f}\\,{disappear_at:.2f})'"
            )

    # Song title — persistent bottom label (yellow accent like YouTube captions)
    now_playing = f"Now Playing: {track_name}"
    safe_now_playing = _escape(now_playing)
    track_font = _fit_font_size(f"Now Playing: {track_name}", base_size=44, min_size=30, max_chars=34)
    filters.append(
        f"drawtext={font_param}text='{safe_now_playing}':"
        f"fontsize={track_font}:fontcolor=#FFDD00:"
        f"borderw=5:bordercolor=black:"
        f"x=(w-text_w)/2:y=h-360"
    )

    # Artist name
    artist_font = _fit_font_size(artist, base_size=40, min_size=30, max_chars=30)
    filters.append(
        f"drawtext={font_param}text='{safe_artist}':"
        f"fontsize={artist_font}:fontcolor=white:"
        f"borderw=4:bordercolor=black:"
        f"x=(w-text_w)/2:y=h-305"
    )

    stream_text = "Stream below"
    safe_stream = _escape(stream_text)
    filters.append(
        f"drawtext={font_param}text='{safe_stream}':"
        f"fontsize=34:fontcolor=#AAAAAA:"
        f"borderw=3:bordercolor=black:"
        f"x=(w-text_w)/2:y=h-255"
    )

    # End-card CTA — fades in for the last 3 seconds, matches overlay content
    import hashlib
    _cta_by_mode = {
        "fact": [
            "subscribe for more facts like this",
            "follow for daily facts & beats",
            "more facts every day",
        ],
        "reddit": [
            "subscribe for more like this",
            "follow for daily posts & beats",
            "more every day — subscribe",
        ],
        "protip": [
            "subscribe for more tips like this",
            "follow for daily tips & beats",
            "more tips every day",
        ],
        "none": [
            "subscribe for more beats",
            "new beats every day",
            "follow for daily drops",
        ],
    }
    cta_options = _cta_by_mode.get(overlay_mode, _cta_by_mode["none"])
    cta_seed = int(hashlib.md5(track_name.encode()).hexdigest()[:8], 16)
    cta_text = _escape(cta_options[cta_seed % len(cta_options)])
    cta_appear = max(0, total_duration - 3.5)
    cta_end = total_duration
    cta_alpha = f"if(lt(t-{cta_appear:.2f}\\,0.6)\\,(t-{cta_appear:.2f})/0.6\\,1)"
    filters.append(
        f"drawtext={font_param}text='{cta_text}':"
        f"fontsize=56:fontcolor=white:"
        f"alpha='{cta_alpha}':"
        f"borderw=6:bordercolor=black:"
        f"x=(w-text_w)/2:y=220:"
        f"enable='between(t\\,{cta_appear:.2f}\\,{cta_end:.2f})'"
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


def _burn_cta_only(video_path: str, track_name: str, output_path: str, total_duration: float, overlay_mode: str = "") -> str:
    """Burn just the subscribe CTA at the end — used for no-text-overlay Shorts."""
    import hashlib
    font = _find_font()
    font_param = f"fontfile={font}:" if font else ""

    _cta_by_mode = {
        "fact": [
            "subscribe for more facts like this",
            "follow for daily facts & beats",
            "more facts every day",
        ],
        "reddit": [
            "subscribe for more like this",
            "follow for daily posts & beats",
            "more every day — subscribe",
        ],
        "protip": [
            "subscribe for more tips like this",
            "follow for daily tips & beats",
            "more tips every day",
        ],
        "none": [
            "subscribe for more beats",
            "new beats every day",
            "follow for daily drops",
        ],
    }
    cta_options = _cta_by_mode.get(overlay_mode, _cta_by_mode["none"])
    cta_seed = int(hashlib.md5(track_name.encode()).hexdigest()[:8], 16)
    cta_text = cta_options[cta_seed % len(cta_options)].replace("'", "'\\''")
    cta_appear = max(0, total_duration - 3.5)
    cta_end = total_duration
    cta_alpha = f"if(lt(t-{cta_appear:.2f}\\,0.6)\\,(t-{cta_appear:.2f})/0.6\\,1)"

    vf = (
        f"drawtext={font_param}text='{cta_text}':"
        f"fontsize=56:fontcolor=white:"
        f"alpha='{cta_alpha}':"
        f"borderw=6:bordercolor=black:"
        f"x=(w-text_w)/2:y=220:"
        f"enable='between(t\\,{cta_appear:.2f}\\,{cta_end:.2f})'"
    )
    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-vf", vf,
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
        "-pix_fmt", "yuv420p", "-c:a", "copy",
        output_path,
    ]
    subprocess.run(cmd, check=True, timeout=120)
    return output_path


def render_short(audio_segment_path: str,
                  footage_paths: List[str],
                  beat_intervals: List[Tuple[float, float]],
                  track_name: str,
                  artist: str = "Unknown Artist",
                  genre: str = "default",
                  poem_lines: list = None,
                  bpm: float = 0,
                  output_dir: str = "/tmp",
                  poem_sets: list = None,
                  overlay_max_lines: int = None,
                  skip_text_overlay: bool = False,
                  skip_cta: bool = False,
                  overlay_mode: str = "") -> Optional[str]:
    """
    Top-level render function:
    1. Cut footage to beat intervals with color grading
    2. Concatenate via stream-copy
    3. Burn text overlay (unless skip_text_overlay=True)
    4. Mux with audio
    5. Cleanup temp files
    Returns path to final MP4, or None on failure.
    skip_text_overlay: skip content text (poem/fact lines + now playing label)
    skip_cta: skip the subscribe CTA at end (True for livestream only)
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

    # Step 3: Mux with audio FIRST (loop raw footage if shorter than audio,
    # before text is burned in — prevents text from appearing twice on loop)
    total_dur = beat_intervals[-1][1] if beat_intervals else 30.0
    muxed_path = os.path.join(output_dir, f"muxed_{ts}.mp4")
    try:
        mux_audio_video(concat_path, audio_segment_path, muxed_path,
                        total_duration=total_dur)
    except subprocess.CalledProcessError as e:
        logger.error(f"Audio mux failed: {e}")
        return None

    # Step 4: Text overlay on the full-duration muxed video
    final_name = f"music_short_{ts}.mp4"
    final_path = os.path.join(output_dir, final_name)
    if skip_text_overlay:
        if not skip_cta:
            try:
                _burn_cta_only(muxed_path, track_name, final_path, total_dur, overlay_mode=overlay_mode)
            except subprocess.CalledProcessError as e:
                logger.error(f"CTA overlay failed: {e}")
                os.rename(muxed_path, final_path)
        else:
            os.rename(muxed_path, final_path)
    else:
        try:
            add_text_overlay(muxed_path, track_name, artist, final_path,
                             total_duration=total_dur, poem_lines=poem_lines, bpm=bpm,
                             poem_sets=poem_sets, overlay_max_lines=overlay_max_lines,
                             overlay_mode=overlay_mode)
        except subprocess.CalledProcessError as e:
            logger.error(f"Text overlay failed: {e}")
            os.rename(muxed_path, final_path)

    # Step 5: Cleanup intermediates
    for path in segments + [concat_path]:
        try:
            os.unlink(path)
        except OSError:
            pass
    try:
        os.unlink(muxed_path)
    except OSError:
        pass

    gc.collect()
    logger.info(f"Final Short rendered: {final_path}")
    return final_path
