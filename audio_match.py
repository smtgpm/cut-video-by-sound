"""Precise reference-audio matching.

The reference WAV defines the maximum amount that can be removed.  Matching
uses both normalized waveform correlation and a mel-spectral fingerprint so
that a re-encoded copy of the same passage can still be found.  Candidate
locations are refined against the waveform to obtain the most precise start
available from the decoded audio.  No keyframe logic or padding exists here.
"""
import os
import subprocess
import tempfile
from dataclasses import dataclass
from typing import List

import librosa
import numpy as np
from scipy.signal import fftconvolve


@dataclass
class Match:
    start: float
    end: float
    score: float
    context_score: float = 0.0
    position_score: float = 0.0
    final_score: float = 0.0


def extract_audio_to_wav(video_path: str, sr: int = 16000) -> str:
    fd, out_path = tempfile.mkstemp(suffix='.wav')
    os.close(fd)
    cmd = [
        'ffmpeg', '-y', '-v', 'error', '-i', video_path,
        '-vn', '-ac', '1', '-ar', str(sr), '-c:a', 'pcm_s16le', out_path,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(
            f'ffmpeg audio extraction failed:\n{r.stderr[-1600:]}')
    return out_path


def _load(path: str, sr: int) -> np.ndarray:
    y, _ = librosa.load(path, sr=sr, mono=True)
    y = np.asarray(y, dtype=np.float64)
    if y.size:
        y -= np.mean(y)
        scale = np.sqrt(np.mean(y * y))
        if scale > 1e-10:
            y /= scale
    return y


def _corr(target: np.ndarray, ref: np.ndarray) -> np.ndarray:
    n = len(ref)
    if n < 2 or len(target) < n:
        return np.empty(0, dtype=np.float64)
    ref = ref - np.mean(ref)
    rn = np.linalg.norm(ref)
    if rn < 1e-12:
        return np.zeros(len(target) - n + 1)
    dot = fftconvolve(target, ref[::-1], mode='valid')
    cs = np.concatenate(([0.0], np.cumsum(target * target)))
    energy = cs[n:] - cs[:-n]
    return dot / (np.sqrt(np.maximum(energy, 1e-12)) * rn)


def _spectral_fingerprint(y: np.ndarray, sr: int) -> np.ndarray:
    mel = librosa.feature.melspectrogram(
        y=y, sr=sr, n_fft=2048, hop_length=256, n_mels=64,
        fmin=40, fmax=min(7600, sr // 2), power=2.0,
    )
    mel = librosa.power_to_db(mel, ref=np.max).T.astype(np.float64)
    mel -= mel.mean(axis=1, keepdims=True)
    return mel / np.maximum(np.linalg.norm(mel, axis=1, keepdims=True), 1e-9)


def _spectral_scores(target_feat: np.ndarray, ref_feat: np.ndarray) -> np.ndarray:
    n = len(ref_feat)
    if n < 2 or len(target_feat) < n:
        return np.empty(0)
    dot = np.zeros(len(target_feat) - n + 1, dtype=np.float64)
    for c in range(ref_feat.shape[1]):
        dot += fftconvolve(target_feat[:, c], ref_feat[::-1, c], mode='valid')
    cs = np.concatenate(
        ([0.0], np.cumsum(np.sum(target_feat * target_feat, axis=1))))
    energy = cs[n:] - cs[:-n]
    rn = np.linalg.norm(ref_feat)
    return dot / (np.sqrt(np.maximum(energy, 1e-12)) * max(rn, 1e-12))


def _local_peaks(scores: np.ndarray, threshold: float, min_gap: int) -> List[int]:
    if not len(scores):
        return []
    candidates = []
    radius = max(1, min_gap // 4)
    for i in np.flatnonzero(scores >= threshold):
        lo, hi = max(0, i - radius), min(len(scores), i + radius + 1)
        if scores[i] >= np.max(scores[lo:hi]):
            candidates.append(int(i))
    candidates.sort(key=lambda i: scores[i], reverse=True)
    selected = []
    for i in candidates:
        if all(abs(i - j) >= min_gap for j in selected):
            selected.append(i)
    return selected


def _refine(target: np.ndarray, ref: np.ndarray, start: int, radius: int) -> tuple[int, float] | None:
    lo = max(0, start - radius)
    hi = min(len(target) - len(ref), start + radius)
    if hi < lo:
        return None
    scores = _corr(target[lo:hi + len(ref)], ref)
    if not len(scores):
        return None
    i = int(np.argmax(scores))
    return lo + i, float(scores[i])


def _context(target: np.ndarray, ref: np.ndarray, start: int, sr: int) -> float:
    # Context is only a ranking signal. It never changes the reference duration.
    n = min(int(1.0 * sr), len(ref) // 4)
    vals = []
    if n and start >= n:
        a, b = target[start-n:start], ref[:n]
        vals.append(
            float(np.dot(a, b) / max(np.linalg.norm(a) * np.linalg.norm(b), 1e-12)))
    end = start + len(ref)
    if n and end + n <= len(target):
        a, b = target[end:end+n], ref[-n:]
        vals.append(
            float(np.dot(a, b) / max(np.linalg.norm(a) * np.linalg.norm(b), 1e-12)))
    return float(np.mean(vals)) if vals else 0.0


def find_matches(ref_wav_path: str, target_video_path: str, threshold: float = 0.72,
                 hop_length: int = 256, sr: int = 16000,
                 intro_max_seconds: float = 180.0, early_bonus: float = 0.02,
                 **_ignored) -> List[Match]:
    del hop_length
    ref = _load(ref_wav_path, sr)
    ref_duration = len(ref) / sr
    if len(ref) < int(0.25 * sr):
        return []

    wav = extract_audio_to_wav(target_video_path, sr)
    try:
        target = _load(wav, sr)
    finally:
        try:
            os.unlink(wav)
        except OSError:
            pass

    if len(target) < len(ref):
        return []

    # Run BOTH detectors.  Previously the spectral detector was only used when
    # waveform correlation found nothing, which could miss the real occurrence
    # when a different occurrence happened to have the strongest waveform score.
    wave_scores = _corr(target, ref)
    wave_threshold = max(0.55, threshold)
    wave_idx = _local_peaks(
        wave_scores, wave_threshold,
        max(1, int(ref_duration * 0.5 * sr)),
    )

    rf = _spectral_fingerprint(ref, sr)
    tf = _spectral_fingerprint(target, sr)
    spec_scores = _spectral_scores(tf, rf)
    frame_rate = sr / 256.0
    spec_threshold = max(0.45, threshold - 0.12)
    spec_idx = _local_peaks(
        spec_scores, spec_threshold,
        max(1, int(ref_duration * 0.5 * frame_rate)),
    )
    spec_samples = [int(round(i * sr / frame_rate)) for i in spec_idx]

    # Union candidate locations from both detectors, then do precise waveform
    # refinement around each one.
    raw_candidates = []
    for i in wave_idx:
        raw_candidates.append((i, 'wave'))
    for i in spec_samples:
        raw_candidates.append((i, 'spec'))
    raw_candidates.sort(key=lambda x: x[0])

    # Collapse detector candidates that refer to the same occurrence.
    merged = []
    merge_gap = max(int(0.75 * sr), int(ref_duration * 0.08 * sr))
    for idx, source in raw_candidates:
        if merged and abs(idx - merged[-1][0]) <= merge_gap:
            # Prefer the waveform candidate as the initial refinement point.
            if source == 'wave' and merged[-1][1] == 'spec':
                merged[-1] = (idx, source)
        else:
            merged.append((idx, source))

    candidates = []
    for idx, source in merged:
        refined = _refine(target, ref, idx, int(1.5 * sr))
        if not refined:
            continue
        start, wave_score = refined

        # Spectral score at the refined location, for a detector-independent
        # measure of similarity after re-encoding.
        frame_idx = int(round(start * frame_rate / sr))
        if 0 <= frame_idx < len(spec_scores):
            spec_score = float(spec_scores[frame_idx])
        else:
            spec_score = -1.0

        # Accept a candidate if either detector supports it. Waveform score is
        # preferred for timestamp precision; spectral score supplies tolerance.
        if wave_score < threshold and spec_score < spec_threshold:
            continue

        context = _context(target, ref, start, sr)
        pos = max(0.0, 1.0 - start / max(intro_max_seconds * sr, 1))
        final = (
            0.68 * wave_score
            + 0.22 * max(spec_score, 0.0)
            + 0.08 * max(context, 0.0)
            + early_bonus * pos
        )

        candidates.append(Match(
            start=start / sr,
            # HARD LIMIT: removal can never exceed the actual reference WAV.
            end=(start + len(ref)) / sr,
            score=max(wave_score, spec_score),
            context_score=context,
            position_score=pos,
            final_score=final,
        ))

    if not candidates:
        return []

    # Keep every distinct occurrence. Do not choose only the globally strongest
    # occurrence: the user's intent is to remove every occurrence of this sound.
    candidates.sort(key=lambda m: m.start)
    selected = []
    for m in candidates:
        if not selected or m.start >= selected[-1].end:
            selected.append(m)
        elif m.final_score > selected[-1].final_score:
            selected[-1] = m

    return selected
