"""Precise reference-audio matching.

The reference WAV defines the maximum amount that can be removed.  Matching
uses both normalized waveform correlation and a mel-spectral fingerprint so
that a re-encoded copy of the same passage can still be found.  Candidate
locations are refined against the waveform to obtain the most precise start
available from the decoded audio.  No keyframe logic or padding exists here.
"""
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import List, Tuple

import librosa
import numpy as np
from scipy import fft as sp_fft
from scipy.signal import fftconvolve, oaconvolve

# The FFTs split their work over this many threads. The result is identical.
FFT_WORKERS = min(8, os.cpu_count() or 1)


@dataclass
class Match:
    """Where a sample is heard in a video, in seconds on the playback clock."""
    start: float
    end: float
    score: float  # best of the waveform and spectral scores, at least the threshold
    waveform_score: float = 0.0  # how exact a copy it is: what the app shows as "match %"
    rank: float = 0.0  # used to choose between overlapping candidates of one sample


@dataclass
class TargetAudio:
    """A video's decoded audio, reusable for any number of samples."""
    samples: np.ndarray
    fingerprint: np.ndarray
    sr: int


def playback_origin(path: str) -> float:
    """Container time that a player shows as 0:00."""
    probe = subprocess.run([
        'ffprobe', '-v', 'error', '-show_entries', 'format=start_time',
        '-of', 'default=nw=1:nk=1', path,
    ], capture_output=True, text=True)
    try:
        return max(0.0, float(probe.stdout.strip()))
    except ValueError:
        return 0.0


def extract_audio(video_path: str, sr: int = 16000) -> np.ndarray:
    """Mono audio on the playback clock, normalised (zero mean, unit RMS)."""
    origin = playback_origin(video_path)
    # Sample 0 must be playback time 0, not the first audio sample, otherwise
    # delayed audio or encoder priming shifts every cut and subtitle.
    to_playback_time = (
        f'asetpts=PTS-{origin}/TB,aresample={sr}:async=1:first_pts=0')
    cmd = [
        'ffmpeg', '-y', '-v', 'error', '-copyts', '-i', video_path,
        '-vn', '-af', to_playback_time,
        '-ac', '1', '-ar', str(sr), '-c:a', 'pcm_s16le', '-f', 's16le', 'pipe:1',
    ]
    # Piped instead of written to a temporary WAV and read back. Same 16-bit
    # samples, scaled exactly as librosa (soundfile) reads a 16-bit WAV.
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode:
        raise RuntimeError(
            f'ffmpeg audio extraction failed:\n{r.stderr.decode(errors="replace")[-1600:]}')
    pcm = np.frombuffer(r.stdout, dtype='<i2')
    del r
    # float32: an hour of audio is 230 MB instead of 460 MB, and every array
    # computed from it is halved too. Sums that need more precision are done
    # in float64 (see _normalise and _corr).
    return _normalise(pcm * np.float32(1 / 32768))


def _load(path: str, sr: int) -> np.ndarray:
    y, _ = librosa.load(path, sr=sr, mono=True)
    return _normalise(np.array(y, dtype=np.float32))


def _normalise(y: np.ndarray) -> np.ndarray:
    if y.size:
        y -= y.dtype.type(np.mean(y, dtype=np.float64))
        # Squares summed in float64, a few million at a time.
        squares = sum(float(np.dot(part, part)) for part in
                      (y[i:i + (1 << 22)].astype(np.float64) for i in range(0, y.size, 1 << 22)))
        scale = np.sqrt(squares / y.size)
        if scale > 1e-10:
            y /= y.dtype.type(scale)
    return y


def _corr(target: np.ndarray, ref: np.ndarray) -> np.ndarray:
    """
    Normalised correlation of ref at every position in target, in float32.
    Computed in float64, a few million positions at a time, so an hour-long
    video never needs its whole score array in float64 (460 MB) at once.
    """
    n = len(ref)
    if n < 2 or len(target) < n:
        return np.empty(0, dtype=np.float32)
    ref = ref.astype(np.float64)
    ref -= np.mean(ref)
    rn = float(np.linalg.norm(ref))
    count = len(target) - n + 1
    if rn < 1e-12:
        return np.zeros(count, dtype=np.float32)
    reversed_ref = ref[::-1].copy()
    out = np.empty(count, dtype=np.float32)
    step = max(1 << 22, n)
    with sp_fft.set_workers(FFT_WORKERS):
        for first in range(0, count, step):
            last = min(count, first + step)
            part = target[first:last + n - 1].astype(np.float64)
            # Overlap-add: same result as one huge FFT, in a fraction of the memory.
            dot = oaconvolve(part, reversed_ref, mode='valid')
            part *= part
            sums = np.empty(len(part) + 1)
            sums[0] = 0.0
            np.cumsum(part, out=sums[1:])
            energy = sums[n:] - sums[:-n]
            del part, sums
            # Silence scores 0: rounding noise divided by nothing is not a match.
            quiet = energy < n * 1e-6
            np.sqrt(np.maximum(energy, 1e-12, out=energy), out=energy)
            dot /= energy * rn
            dot[quiet] = 0.0
            out[first:last] = dot
    return out


def _spectral_fingerprint(y: np.ndarray, sr: int) -> np.ndarray:
    n_fft, hop, block = 2048, 256, 4096
    # In blocks: one STFT of a whole episode takes about 2 GB.
    padded = np.pad(y, n_fft // 2)
    frames = 1 + (len(padded) - n_fft) // hop

    def one(first):
        return librosa.feature.melspectrogram(
            y=padded[first * hop:(min(first + block, frames) - 1) * hop + n_fft],
            sr=sr, n_fft=n_fft, hop_length=hop, n_mels=64, center=False,
            fmin=40, fmax=min(7600, sr // 2), power=2.0,
        )

    # The blocks are independent, so they are computed side by side.
    firsts = range(0, frames, block)
    if len(firsts) > 1 and FFT_WORKERS > 1:
        with ThreadPoolExecutor(FFT_WORKERS) as pool:
            mel = np.concatenate(list(pool.map(one, firsts)), axis=1)
    else:
        mel = np.concatenate([one(first) for first in firsts], axis=1)
    mel = librosa.power_to_db(mel, ref=np.max).T.astype(np.float64)
    mel -= mel.mean(axis=1, keepdims=True)
    return mel / np.maximum(np.linalg.norm(mel, axis=1, keepdims=True), 1e-9)


def _spectral_scores(target_feat: np.ndarray, ref_feat: np.ndarray) -> np.ndarray:
    n = len(ref_feat)
    if n < 2 or len(target_feat) < n:
        return np.empty(0)
    dot = np.zeros(len(target_feat) - n + 1, dtype=np.float64)
    with sp_fft.set_workers(FFT_WORKERS):
        for c in range(ref_feat.shape[1]):
            dot += fftconvolve(target_feat[:, c], ref_feat[::-1, c], mode='valid')
    cs = np.concatenate(
        ([0.0], np.cumsum(np.sum(target_feat * target_feat, axis=1))))
    energy = cs[n:] - cs[:-n]
    rn = np.linalg.norm(ref_feat)
    return dot / (np.sqrt(np.maximum(energy, 1e-12)) * max(rn, 1e-12))


def _only_between(scores: np.ndarray, starts: Tuple[float, float], rate: float) -> np.ndarray:
    """The scores, with every start outside [low, high] seconds made hopeless."""
    low, high = max(0, int(starts[0] * rate)), max(0, int(np.ceil(starts[1] * rate)) + 1)
    scores[:low] = -1.0  # in place: the scores are this search's own
    scores[high:] = -1.0
    return scores


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


def _refine(scores: np.ndarray, start: int, radius: int) -> tuple[int, float] | None:
    """
    Best waveform score near start. The scores already hold the correlation at
    every sample, so they are only searched, not computed again.
    """
    lo = max(0, start - radius)
    hi = min(len(scores) - 1, start + radius)
    if hi < lo:
        return None
    i = int(np.argmax(scores[lo:hi + 1]))
    return lo + i, float(scores[lo + i])


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


def load_target(video_path: str, sr: int = 16000) -> TargetAudio:
    samples = extract_audio(video_path, sr)
    return TargetAudio(samples, _spectral_fingerprint(samples, sr), sr)


def find_matches(ref_wav_path: str, target_video_path: str, threshold: float = 0.72,
                 sr: int = 16000, intro_max_seconds: float = 180.0, early_bonus: float = 0.02,
                 target: TargetAudio | None = None,
                 starts: Tuple[float, float] | None = None) -> List[Match]:
    """
    Every distinct place where the sample is heard in the video.

    Pass `target` (from load_target) to search one video for several samples
    without decoding its audio again. With `starts` (low, high), in seconds,
    only matches starting in that range are looked for.
    """
    ref = _load(ref_wav_path, sr)
    ref_duration = len(ref) / sr
    if len(ref) < int(0.25 * sr):
        return []

    if target is None or target.sr != sr:
        target = load_target(target_video_path, sr)
    target, tf = target.samples, target.fingerprint

    if len(target) < len(ref):
        return []

    # Both detectors propose candidates: the waveform finds exact copies, the
    # spectral fingerprint finds copies that were re-encoded differently.
    wave_scores = _corr(target, ref)
    if starts is not None:
        # Masked before picking peaks: a stronger copy just outside the range
        # must not hide the one inside it.
        wave_scores = _only_between(wave_scores, starts, sr)
    wave_threshold = max(0.55, threshold)
    wave_idx = _local_peaks(
        wave_scores, wave_threshold,
        max(1, int(ref_duration * 0.5 * sr)),
    )

    rf = _spectral_fingerprint(ref, sr)
    spec_scores = _spectral_scores(tf, rf)
    frame_rate = sr / 256.0
    if starts is not None:
        spec_scores = _only_between(spec_scores, starts, frame_rate)
    spec_threshold = max(0.45, threshold - 0.12)
    spec_idx = _local_peaks(
        spec_scores, spec_threshold,
        max(1, int(ref_duration * 0.5 * frame_rate)),
    )
    spec_samples = [int(round(i * sr / frame_rate)) for i in spec_idx]

    # Union candidate locations from both detectors, then do precise waveform
    # refinement around each one.
    raw_candidates = sorted([(i, 'wave') for i in wave_idx] + [(i, 'spec') for i in spec_samples])

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
        refined = _refine(wave_scores, idx, int(1.5 * sr))
        if not refined:
            continue
        start, wave_score = refined
        if starts is not None and not starts[0] - 0.05 <= start / sr <= starts[1] + 0.05:
            continue  # refinement moved it out of the range

        # Spectral score at the refined location, for a detector-independent
        # measure of similarity after re-encoding.
        frame_idx = int(round(start * frame_rate / sr))
        if 0 <= frame_idx < len(spec_scores):
            spec_score = float(spec_scores[frame_idx])
        else:
            spec_score = -1.0

        # Candidates may be found permissively, but the threshold always decides.
        score = max(wave_score, spec_score)
        if score < threshold:
            continue

        context = _context(target, ref, start, sr)
        pos = max(0.0, 1.0 - start / max(intro_max_seconds * sr, 1))
        rank = (
            0.68 * wave_score
            + 0.22 * max(spec_score, 0.0)
            + 0.08 * max(context, 0.0)
            + early_bonus * pos
        )

        candidates.append(Match(
            start=start / sr,
            # A removal is never longer than the sample itself.
            end=(start + len(ref)) / sr,
            score=score,
            waveform_score=wave_score,
            rank=rank,
        ))

    if not candidates:
        return []

    # Every distinct occurrence is kept: which ones become cuts is decided
    # later, over all the samples together (pipeline.settle_cuts).
    candidates.sort(key=lambda m: m.start)
    selected = []
    for m in candidates:
        if not selected or m.start >= selected[-1].end:
            selected.append(m)
        elif m.rank > selected[-1].rank:
            selected[-1] = m

    return selected
