"""
Beat Analyzer — librosa beat detection + best-section finder
Loads an MP3, detects BPM and beat timestamps, finds the most energetic
30-second window (the "drop"), and returns beat-aligned cut points.
"""

import os
import librosa
import numpy as np
import soundfile as sf
import logging
from typing import Tuple, List

logger = logging.getLogger(__name__)

MAX_DURATION = 30.0   # max Short length
MIN_DURATION = 15.0   # min Short length
WINDOW_HOP = 1.0      # sliding window hop (seconds)


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

    # Choose Short duration based on BPM — faster tracks get shorter Shorts
    # Minimum 25s to give text overlay enough screen time
    if bpm >= 160:
        target_dur = 25.0
    elif bpm >= 140:
        target_dur = 28.0
    elif bpm >= 120:
        target_dur = 30.0
    else:
        target_dur = 35.0
    target_dur = min(target_dur, duration)  # can't exceed track length
    logger.info(f"Target Short duration: {target_dur:.0f}s (BPM: {bpm:.0f})")

    # Find multiple non-overlapping energetic windows
    num_shorts = int(os.environ.get("NUM_SHORTS", "2"))
    all_windows = _find_top_windows(onset_env, sr, duration, target_dur, n=num_shorts)

    # Snap each to bar boundaries for clean looping
    bar_duration = 4 * (60.0 / bpm)  # 4 beats per bar
    snapped_windows = []
    for ws, we in all_windows:
        ws, we = _snap_to_bars(ws, we, beat_times, bar_duration, duration)
        snapped_windows.append((ws, we))
    logger.info(f"Found {len(snapped_windows)} bar-aligned windows (bar={bar_duration:.2f}s)")

    # Primary window (best energy)
    best_start, best_end = snapped_windows[0]
    logger.info(f"Primary window: {best_start:.2f}s - {best_end:.2f}s "
                f"({(best_end - best_start):.1f}s)")

    # Filter beats to only those within the primary window
    window_beats = [b for b in beat_times if best_start <= b <= best_end]

    # Audio features for genre classification
    rms = float(np.mean(librosa.feature.rms(y=y)))
    spectral_centroid = float(np.mean(librosa.feature.spectral_centroid(y=y, sr=sr)))
    spectral_rolloff = float(np.mean(librosa.feature.spectral_rolloff(y=y, sr=sr)))
    zcr = float(np.mean(librosa.feature.zero_crossing_rate(y)))

    energy_tag = "aggressive" if rms > 0.08 else ("moderate" if rms > 0.03 else "calm")
    brightness_tag = "bright" if spectral_centroid > 3000 else ("mid" if spectral_centroid > 1500 else "dark")
    noise_tag = "distorted" if zcr > 0.15 else ("textured" if zcr > 0.08 else "clean")

    logger.info(f"Audio features: RMS={rms:.4f}({energy_tag}), "
                f"centroid={spectral_centroid:.0f}({brightness_tag}), "
                f"ZCR={zcr:.4f}({noise_tag})")

    return {
        "bpm": bpm,
        "beat_times": window_beats,
        "all_beat_times": beat_times,
        "best_start": best_start,
        "best_end": best_end,
        "all_windows": snapped_windows,
        "duration": duration,
        "sr": sr,
        "energy": energy_tag,
        "brightness": brightness_tag,
        "texture": noise_tag,
        "rms": rms,
        "spectral_centroid": spectral_centroid,
    }


def _find_best_window(onset_env: np.ndarray, sr: int,
                       track_duration: float,
                       target_duration: float = 30.0) -> Tuple[float, float]:
    """
    Slide a window over the onset envelope and find the position
    with the highest average onset strength (= most energetic section).
    """
    hop_length = 512  # librosa default
    frame_duration = hop_length / sr
    window_frames = int(target_duration / frame_duration)

    if len(onset_env) <= window_frames:
        return 0.0, min(track_duration, target_duration)

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
    best_end = best_start + target_duration

    if best_end > track_duration:
        best_end = track_duration
        best_start = max(0.0, best_end - target_duration)

    return best_start, best_end


def _find_top_windows(onset_env: np.ndarray, sr: int,
                       track_duration: float,
                       target_duration: float = 30.0,
                       n: int = 3) -> List[Tuple[float, float]]:
    """
    Find top N non-overlapping windows ranked by energy.
    Returns list of (start, end) tuples.
    """
    hop_length = 512
    frame_duration = hop_length / sr
    window_frames = int(target_duration / frame_duration)

    if len(onset_env) <= window_frames:
        return [(0.0, min(track_duration, target_duration))]

    hop_frames = max(1, int(WINDOW_HOP / frame_duration))

    # Score all windows
    scored = []
    for start_frame in range(0, len(onset_env) - window_frames, hop_frames):
        window = onset_env[start_frame: start_frame + window_frames]
        score = float(np.mean(window))
        start_time = start_frame * frame_duration
        scored.append((score, start_time))

    scored.sort(reverse=True)

    # Greedily pick non-overlapping windows
    results = []
    for score, start_time in scored:
        end_time = start_time + target_duration
        if end_time > track_duration:
            end_time = track_duration
            start_time = max(0.0, end_time - target_duration)
        # Check overlap with already picked windows
        overlaps = False
        for rs, re_ in results:
            if start_time < re_ and end_time > rs:
                overlaps = True
                break
        if not overlaps:
            results.append((start_time, end_time))
            if len(results) >= n:
                break

    # Fallback: if not enough windows found, divide track into n equal zones
    # and pick the best window from each zone (guarantees distinct segments)
    if len(results) < n and track_duration >= target_duration * 1.5:
        results = []
        zone_len = track_duration / n
        for z in range(n):
            zone_start = z * zone_len
            zone_end = min((z + 1) * zone_len, track_duration)
            best_score = -1
            best_time = zone_start
            for sc, st in scored:
                if zone_start <= st < zone_end and st + target_duration <= track_duration:
                    if sc > best_score:
                        best_score = sc
                        best_time = st
            results.append((best_time, min(best_time + target_duration, track_duration)))

    return results


def _snap_to_bars(best_start: float, best_end: float,
                   beat_times: List[float], bar_duration: float,
                   track_duration: float) -> Tuple[float, float]:
    """
    Snap the segment start to the nearest beat and the end to a full bar
    boundary so the Short loops seamlessly (no fade needed).
    Tries 8, 4, 2, then 1 bar multiples for best fit near target duration.
    """
    target_dur = best_end - best_start

    # Snap start to the nearest beat at or before best_start
    candidates = [b for b in beat_times if b <= best_start + 0.1]
    if candidates:
        snap_start = candidates[-1]
    else:
        snap_start = beat_times[0] if beat_times else best_start

    # Try to fit the most bars that stay within reasonable Short length
    best_fit_end = None
    best_fit_diff = float('inf')

    for n_bars in range(1, 32):
        candidate_end = snap_start + n_bars * bar_duration
        if candidate_end > track_duration:
            break
        diff = abs((candidate_end - snap_start) - target_dur)
        if diff < best_fit_diff:
            best_fit_diff = diff
            best_fit_end = candidate_end
        # Once we've passed target by more than 2 bars, stop searching
        if candidate_end - snap_start > target_dur + 2 * bar_duration:
            break

    if best_fit_end and best_fit_end > snap_start:
        return snap_start, best_fit_end

    return best_start, best_end


def extract_audio_segment(audio_path: str, start: float, end: float,
                           output_path: str) -> str:
    """
    Extract a segment of audio. Saves as WAV for lossless compositing.
    Applies a tiny (10ms) fade at start/end to eliminate loop click.
    """
    y, sr = librosa.load(audio_path, sr=22050, mono=False,
                          offset=start, duration=end - start)
    # 10ms micro-fade to prevent loop click (imperceptible)
    fade_samples = int(sr * 0.01)
    if y.ndim == 1:
        if len(y) > fade_samples * 2:
            y[:fade_samples] *= np.linspace(0, 1, fade_samples)
            y[-fade_samples:] *= np.linspace(1, 0, fade_samples)
    else:
        if y.shape[1] > fade_samples * 2:
            y[:, :fade_samples] *= np.linspace(0, 1, fade_samples)
            y[:, -fade_samples:] *= np.linspace(1, 0, fade_samples)
    sf.write(output_path, y.T if y.ndim > 1 else y, sr)
    logger.info(f"Extracted audio segment: {start:.1f}s-{end:.1f}s -> {output_path}")
    return output_path


def _snap_to_frame(t: float, fps: int = 30) -> float:
    """Snap a time value to the nearest frame boundary to prevent drift."""
    return round(t * fps) / fps


def get_beat_intervals(beat_times: List[float], start_offset: float = 0.0,
                        segment_duration: float = 30.0,
                        min_interval: float = 1.5,
                        max_interval: float = 4.0,
                        fps: int = 30,
                        skip_ratio: float = 0.6) -> List[Tuple[float, float]]:
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
        return [(0.0, segment_duration)]

    relative = [b - start_offset for b in beat_times if b >= start_offset]
    if not relative:
        return [(0.0, segment_duration)]

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
    final_end = _snap_to_frame(segment_duration, fps)
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
