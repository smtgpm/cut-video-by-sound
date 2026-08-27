"""
audio_match.py
Search a target video for passages whose audio matches a reference clip
(e.g. a series intro), using sliding chroma feature similarity over the
full track.

Optimization: the sliding correlation is computed via FFT (scipy), not by
brute force. Complexity O(T log T) instead of O(T x Tr), and especially no
large window matrix kept in memory (useful for long videos or a long
reference). GPU does not help much here: audio data is tiny compared to
video; the real GPU win is on the re-encoding side
(see hwaccel.py / remove_segments.py).

Python dependencies: numpy, scipy, librosa
External dependency: ffmpeg (to extract audio from videos)
"""

import os
import subprocess
import tempfile
from dataclasses import dataclass
from typing import List

import numpy as np
from scipy.signal import correlate
import librosa


@dataclass
class Match:
    start: float   # seconds, in the target video
    end: float
    score: float   # cosine similarity, 0..1


def extract_audio_to_wav(video_path: str, sr: int = 22050) -> str:
    """Extract the full audio track from a video to a temporary mono .wav."""
    fd, out_path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    cmd = [
        "ffmpeg", "-y", "-i", video_path,
        "-vn", "-ac", "1", "-ar", str(sr),
        "-acodec", "pcm_s16le", out_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed on {video_path}:\n{result.stderr[-800:]}")
    return out_path


def _chroma_features(y: np.ndarray, sr: int, hop_length: int) -> np.ndarray:
    """Return a (T, 12) matrix of chroma vectors, one per time frame."""
    chroma = librosa.feature.chroma_stft(
        y=y, sr=sr, hop_length=hop_length)  # (12, T)
    return chroma.T.astype(np.float64)  # (T, 12)


def find_matches(
    ref_wav_path: str,
    target_video_path: str,
    threshold: float = 0.82,
    hop_length: int = 2048,
    sr: int = 22050,
) -> List[Match]:
    """
    Find all occurrences of ref_wav_path's audio in the audio track of
    target_video_path, using sliding FFT correlation on chroma fingerprints.

    threshold: cosine similarity cutoff (0 to 1). ~0.8-0.9 for an intro that
               is nearly bit-identical; lower if volume/encoding varies.
    """
    y_ref, _ = librosa.load(ref_wav_path, sr=sr, mono=True)
    ref_duration = len(y_ref) / sr

    target_wav = extract_audio_to_wav(target_video_path, sr=sr)
    try:
        y_target, _ = librosa.load(target_wav, sr=sr, mono=True)
    finally:
        os.remove(target_wav)

    ref_feat = _chroma_features(y_ref, sr, hop_length)         # (Tr, 12)
    target_feat = _chroma_features(y_target, sr, hop_length)   # (Tt, 12)

    Tr = ref_feat.shape[0]
    Tt = target_feat.shape[0]
    if Tr < 2 or Tt <= Tr:
        return []

    ref_norm = np.linalg.norm(ref_feat)
    if ref_norm == 0:
        return []

    n_windows = Tt - Tr + 1

    # --- Sliding dot product, via FFT, one chroma channel at a time ---
    # correlate(target[:,c], ref[:,c], mode='valid') gives, for each position i,
    # sum_k target[i+k, c] * ref[k, c] -- exactly what to sum over the
    # 12 channels to get the full sliding dot product.
    dot = np.zeros(n_windows, dtype=np.float64)
    for c in range(ref_feat.shape[1]):
        dot += correlate(target_feat[:, c],
                         ref_feat[:, c], mode="valid", method="fft")

    # --- Norm of each target window, via cumulative sum (O(T), no per-window loop) ---
    energy_per_frame = np.sum(target_feat**2, axis=1)  # (Tt,)
    cumsum = np.concatenate([[0.0], np.cumsum(energy_per_frame)])  # (Tt+1,)
    window_energy = cumsum[Tr:Tr + n_windows] - cumsum[0:n_windows]
    window_norms = np.sqrt(np.maximum(window_energy, 1e-12))

    scores = dot / (window_norms * ref_norm)

    # Peak picking: keep the best score per neighborhood (avoids duplicates
    # around the same true match).
    matches = []
    i = 0
    min_gap = max(1, int(Tr * 0.5))
    while i < len(scores):
        if scores[i] >= threshold:
            j = min(i + min_gap, len(scores))
            local_best = i + int(np.argmax(scores[i:j]))
            best_score = float(scores[local_best])
            start_time = local_best * hop_length / sr
            matches.append(
                Match(start=start_time, end=start_time + ref_duration, score=best_score))
            i = local_best + min_gap
        else:
            i += 1

    return matches
