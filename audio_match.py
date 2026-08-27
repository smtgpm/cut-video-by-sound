"""
audio_match.py
Recherche, dans une vidéo cible, les passages dont l'audio correspond à un
extrait de référence (ex: le générique d'une série), par similarité de
caractéristiques audio (chroma) glissée sur toute la piste.

Optimisation : la corrélation glissée est calculée via FFT (scipy), pas par
force brute. Complexité O(T log T) au lieu de O(T x Tr), et surtout aucune
grosse matrice de fenêtres à garder en mémoire (utile pour de longues vidéos
ou une référence longue). Le GPU n'apporte pas grand chose ici : les données
audio sont minuscules comparées à la vidéo, le vrai gain GPU est côté
ré-encodage (voir hwaccel.py / remove_segments.py).

Dépendances Python : numpy, scipy, librosa
Dépendance externe : ffmpeg (pour extraire l'audio des vidéos)
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
    start: float   # secondes, dans la vidéo cible
    end: float
    score: float   # similarité cosinus, 0..1


def extract_audio_to_wav(video_path: str, sr: int = 22050) -> str:
    """Extrait la piste audio complète d'une vidéo vers un .wav mono temporaire."""
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
            f"ffmpeg a échoué sur {video_path}:\n{result.stderr[-800:]}")
    return out_path


def _chroma_features(y: np.ndarray, sr: int, hop_length: int) -> np.ndarray:
    """Retourne une matrice (T, 12) de vecteurs chroma, un par frame temporelle."""
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
    Cherche toutes les occurrences de l'audio de ref_wav_path dans la piste audio
    de target_video_path, par corrélation glissée (FFT) sur des empreintes chroma.

    threshold : seuil de similarité cosinus (0 à 1). ~0.8-0.9 pour un générique
                quasi identique bit à bit ; baisser si le volume/l'encodage varie.
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

    # --- Produit scalaire glissé, par FFT, un canal chroma à la fois ---
    # correlate(target[:,c], ref[:,c], mode='valid') donne, pour chaque position i,
    # sum_k target[i+k, c] * ref[k, c] -- exactement ce qu'il faut sommer sur les
    # 12 canaux pour obtenir le produit scalaire glissé complet.
    dot = np.zeros(n_windows, dtype=np.float64)
    for c in range(ref_feat.shape[1]):
        dot += correlate(target_feat[:, c],
                         ref_feat[:, c], mode="valid", method="fft")

    # --- Norme de chaque fenêtre cible, via somme cumulée (O(T), pas de boucle par fenêtre) ---
    energy_per_frame = np.sum(target_feat**2, axis=1)  # (Tt,)
    cumsum = np.concatenate([[0.0], np.cumsum(energy_per_frame)])  # (Tt+1,)
    window_energy = cumsum[Tr:Tr + n_windows] - cumsum[0:n_windows]
    window_norms = np.sqrt(np.maximum(window_energy, 1e-12))

    scores = dot / (window_norms * ref_norm)

    # Peak picking : garde le meilleur score par voisinage (évite les doublons
    # autour d'un même vrai match).
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
