"""
Beat Analyzer — librosa beat detection + best-section finder
Loads an MP3, detects BPM and beat timestamps, finds the most energetic
30-second window (the "drop"), and returns beat-aligned cut points.
"""

import librosa
import numpy as np
import soundfile as sf
import logging
from typing import Tuple, List

logger = logging.getLogger(__name__)

TARGET_DURATION = 30.0  # seconds
WINDOW_HOP = 1.0  # sliding window hop (seconds)


def analyze_track(audio_path: str) -> dict:
    """
    Full analysis of a track.

    Returns dict with:
        bpm: float
        beat_times: list[float] — beat timestamps within the best window
        all_beat_times: list[float] — all beats in the full track
        best_start: float — start of most energetic 30s window
        best_end: float
        duration: float — full track duration
        sr: int
    """
    logger.info(f"Loading audio: {audio_path}")
    y, sr = librosa.load(audio_path, sr=22050, mono=True)
    duration = librosa.get_duration(y=y, sr=sr)
    logger.info(f"Track duration: {duration:.1f}s, SR: {sr}")

    # Beat tracking
    tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr)
    beat_times = librosa.frames_to_time(beat_frames, sr=sr).tolist()
    bpm = float(np.atleast_1d(tempo)[0])
    logger.info(f"BPM: {bpm:.1f}, {len(beat_times)} beats detected")

    # Onset strength envelope (used to find most energetic section)
    onset_env = librosa.onset.onset_strength(y=y, sr=sr)

    # Find the most energetic 30-second window
    best_start, best_end = _find_best_window(onset_env, sr, duration)
    logger.info(f"Best 30s window: {best_start:.1f}s - {best_end:.1f}s")

    # Filter beats to only those within the chosen window
    window_beats = [b for b in beat_times if best_start <= b <= best_end]

    return {
        "bpm": bpm,
        "beat_times": window_beats,
        "all_beat_times": beat_times,
        "best_start": best_start,
        "best_end": best_end,
        "duration": duration,
        "sr": sr,
    }


def _find_best_window(onset_env: np.ndarray, sr: int,
                       track_duration: float) -> Tuple[float, float]:
    """
    Slide a TARGET_DURATION window over the onset envelope and find the
    position with the highest average onset strength (= most energetic section).
    """
    hop_length = 512  # librosa default
    frame_duration = hop_length / sr
    window_frames = int(TARGET_DURATION / frame_duration)

    if len(onset_env) <= window_frames:
        return 0.0, min(track_duration, TARGET_DURATION)

    hop_frames = max(1, int(WINDOW_HOP / frame_duration))
    best_score = -1.0
    best_frame = 0

    for start_frame in range(0, len(onset_env) - window_frames, hop_frames):
        window = onset_env[start_frame: start_frame + window_frames]
        score = float(np.mean(window))
        if score > best_score:
            best_score = score
            best_frame = start_frame

    best_start = best_frame * frame_duration
    best_end = best_start + TARGET_DURATION

    if best_end > track_duration:
        best_end = track_duration
        best_start = max(0.0, best_end - TARGET_DURATION)

    return best_start, best_end


def extract_audio_segment(audio_path: str, start: float, end: float,
                           output_path: str) -> str:
    """
    Extract a segment of audio. Saves as WAV for lossless moviepy compositing.
    Returns output_path.
    """
    y, sr = librosa.load(audio_path, sr=22050, mono=False,
                          offset=start, duration=end - start)
    sf.write(output_path, y.T if y.ndim > 1 else y, sr)
    logger.info(f"Extracted audio segment: {start:.1f}s-{end:.1f}s -> {output_path}")
    return output_path


def _snap_to_frame(t: float, fps: int = 30) -> float:
    """Snap a time value to the nearest frame boundary to prevent drift."""
    return round(t * fps) / fps


def get_beat_intervals(beat_times: List[float], start_offset: float = 0.0,
                        min_interval: float = 0.3,
                        max_interval: float = 4.0,
                        fps: int = 30,
                        skip_ratio: float = 0.35) -> List[Tuple[float, float]]:
    """
    Convert beat timestamps into (start, end) intervals for video cuts.
    Merges beats that are too close (< min_interval).
    Splits intervals that are too long (> max_interval).
    Randomly skips ~skip_ratio of beats so some clips hold longer (visual variety).
    All times are relative to the clip (offset by start_offset).
    Times are snapped to frame boundaries (1/fps) to prevent cumulative drift.
    """
    import random

    if not beat_times:
        return [(0.0, TARGET_DURATION)]

    relative = [b - start_offset for b in beat_times if b >= start_offset]
    if not relative:
        return [(0.0, TARGET_DURATION)]

    # Randomly skip some beats so not every beat is a cut.
    # Keep first and last few beats, skip randomly in between.
    if len(relative) > 6 and skip_ratio > 0:
        keep = [relative[0]]  # always cut on first beat
        for beat in relative[1:-1]:
            if random.random() > skip_ratio:
                keep.append(beat)
        keep.append(relative[-1])  # always cut near end
        relative = keep

    intervals = []
    current_start = 0.0

    for beat in relative:
        snapped = _snap_to_frame(beat, fps)
        if snapped - current_start >= min_interval:
            intervals.append((current_start, snapped))
            current_start = snapped

    # Final interval to the end
    final_end = _snap_to_frame(TARGET_DURATION, fps)
    if current_start < final_end:
        intervals.append((current_start, final_end))

    # Split long intervals
    final = []
    for s, e in intervals:
        while e - s > max_interval:
            mid = _snap_to_frame(s + max_interval, fps)
            final.append((s, mid))
            s = mid
        final.append((s, e))

    return final
