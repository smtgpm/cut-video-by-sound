"""
remove_segments.py
Découpe une vidéo en supprimant des plages temporelles données (par exemple
les génériques détectés par audio_match.py), en gardant tout le reste, et en
recollant les morceaux restants dans un nouveau fichier.

Optimisations :
  - Encodage matériel (GPU) auto-détecté via hwaccel.py, avec repli
    automatique sur libx264 (CPU) si l'encodage GPU échoue à l'exécution.
  - Mode "smart cut" (optionnel, expérimental) : au lieu de ré-encoder
    l'intégralité de chaque partie gardée, on ne ré-encode que quelques
    secondes autour de chaque point de coupe (calées sur les images clé) et
    on copie le flux (sans ré-encodage, quasi instantané) pour le reste.
    Ce mode nécessite une vidéo source en H.264 ; sinon il se désactive tout
    seul et retombe sur un ré-encodage complet.

Dépendance externe : ffmpeg + ffprobe
"""

import bisect
import os
import subprocess
import tempfile
from typing import List, Tuple

from hwaccel import EncoderProfile, SOFTWARE, get_encoder_profile


def _merge_intervals(intervals: List[Tuple[float, float]], gap: float = 0.5) -> List[Tuple[float, float]]:
    """Fusionne les intervalles qui se chevauchent ou sont très proches (marge en secondes)."""
    if not intervals:
        return []
    intervals = sorted(intervals)
    merged = [list(intervals[0])]
    for s, e in intervals[1:]:
        if s <= merged[-1][1] + gap:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged]


def _get_duration(video_path: str) -> float:
    cmd = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", video_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError(
            f"ffprobe n'a pas pu lire la durée de {video_path}:\n{result.stderr[-500:]}")
    return float(result.stdout.strip())


def _get_video_codec(video_path: str) -> str:
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=codec_name", "-of", "default=noprint_wrappers=1:nokey=1", video_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.stdout.strip()


def _keyframe_times(video_path: str) -> List[float]:
    """Liste les timestamps (secondes) des images clé de la vidéo (rapide : ne décode que les I-frames)."""
    cmd = [
        "ffprobe", "-v", "error", "-skip_frame", "nokey",
        "-select_streams", "v:0", "-show_entries", "frame=pts_time",
        "-of", "csv=p=0", video_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    times = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if line:
            try:
                times.append(float(line))
            except ValueError:
                pass
    return sorted(times)


def _build_reencode_cmd(profile: EncoderProfile, video_path: str, s: float, e: float, out_path: str) -> List[str]:
    cmd = ["ffmpeg", "-y"]
    if profile.decode_hwaccel:
        cmd += ["-hwaccel", profile.decode_hwaccel]
    cmd += profile.pre_input_args
    cmd += ["-ss", f"{s:.3f}", "-i", video_path, "-t", f"{e - s:.3f}"]
    cmd += profile.filter_args
    cmd += ["-c:v", profile.encoder]
    cmd += profile.encode_args
    cmd += ["-c:a", "aac", out_path]
    return cmd


def _reencode_with_fallback(profile: EncoderProfile, video_path: str, s: float, e: float, out_path: str, warned: dict):
    """Ré-encode [s, e]. Essaie le profil demandé (GPU ou CPU), retombe sur libx264 si ça échoue."""
    if profile.name != "software":
        cmd = _build_reencode_cmd(profile, video_path, s, e, out_path)
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            return
        if not warned.get("hw_fallback"):
            last_line = result.stderr.strip().splitlines(
            )[-1] if result.stderr.strip() else ""
            print(
                f"  [!] Encodage GPU ({profile.name}) indisponible/échoué, repli sur le CPU (libx264).")
            print(f"      Détail ffmpeg : {last_line}")
            warned["hw_fallback"] = True

    cmd = _build_reencode_cmd(SOFTWARE, video_path, s, e, out_path)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg (CPU) a échoué sur [{s:.2f},{e:.2f}]:\n{result.stderr[-800:]}")


def _stream_copy(video_path: str, s: float, e: float, out_path: str):
    cmd = ["ffmpeg", "-y", "-ss", f"{s:.3f}", "-i",
           video_path, "-t", f"{e - s:.3f}", "-c", "copy", out_path]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg (copie de flux) a échoué sur [{s:.2f},{e:.2f}]:\n{result.stderr[-800:]}")


def _concat(part_paths: List[str], output_path: str):
    with tempfile.TemporaryDirectory() as tmp:
        concat_list_path = os.path.join(tmp, "concat.txt")
        with open(concat_list_path, "w", encoding="utf-8") as f:
            for p in part_paths:
                f.write(f"file '{p}'\n")
        cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
               "-i", concat_list_path, "-c", "copy", output_path]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(
                f"ffmpeg a échoué à la concaténation:\n{result.stderr[-800:]}")


def _keep_intervals(video_path: str, segments_to_remove: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
    duration = _get_duration(video_path)
    removals = _merge_intervals(segments_to_remove)
    keep = []
    cursor = 0.0
    for s, e in removals:
        s = max(0.0, min(s, duration))
        e = max(0.0, min(e, duration))
        if s > cursor:
            keep.append((cursor, s))
        cursor = max(cursor, e)
    if cursor < duration:
        keep.append((cursor, duration))
    return keep


def remove_segments(
    video_path: str,
    segments_to_remove: List[Tuple[float, float]],
    output_path: str,
    hw_prefer: str = "auto",
    vaapi_device: str = "/dev/dri/renderD128",
    smart_cut: bool = False,
    min_gop_for_copy: float = 1.0,
) -> None:
    """
    Supprime segments_to_remove (liste de (début, fin) en secondes, vidéo + son)
    de video_path, et écrit le résultat dans output_path.

    hw_prefer : "auto" (détecte le GPU disponible), "none" (force le CPU), ou un
                nom explicite ("nvenc", "qsv", "videotoolbox", "vaapi").
    smart_cut : si True, ne ré-encode que de petits segments autour de chaque
                coupe et copie le flux pour le gros du contenu (beaucoup plus
                rapide). Nécessite une source H.264 ; sinon repli automatique
                sur un ré-encodage complet.
    """
    profile = get_encoder_profile(hw_prefer, vaapi_device)
    keep = _keep_intervals(video_path, segments_to_remove)
    if not keep:
        raise ValueError(
            "Rien à garder : les segments à supprimer couvrent toute la vidéo.")

    codec = _get_video_codec(video_path)
    use_smart_cut = smart_cut and codec == "h264"
    if smart_cut and not use_smart_cut:
        print(
            f"  [!] Smart-cut demandé mais codec source '{codec}' != h264 : repli sur ré-encodage complet.")

    warned = {}
    with tempfile.TemporaryDirectory() as tmp:
        part_paths = []
        part_idx = 0

        kf_times = _keyframe_times(video_path) if use_smart_cut else []

        for s, e in keep:
            if not use_smart_cut:
                part_path = os.path.join(tmp, f"part_{part_idx:04d}.mp4")
                _reencode_with_fallback(
                    profile, video_path, s, e, part_path, warned)
                part_paths.append(part_path)
                part_idx += 1
                continue

            # Première image clé >= s, dernière image clé <= e.
            i = bisect.bisect_left(kf_times, s)
            kf_after_s = kf_times[i] if i < len(kf_times) else None
            j = bisect.bisect_right(kf_times, e) - 1
            kf_before_e = kf_times[j] if j >= 0 else None

            bulk_possible = (
                kf_after_s is not None and kf_before_e is not None
                and kf_before_e - kf_after_s >= min_gop_for_copy
            )

            if not bulk_possible:
                part_path = os.path.join(tmp, f"part_{part_idx:04d}.mp4")
                _reencode_with_fallback(
                    profile, video_path, s, e, part_path, warned)
                part_paths.append(part_path)
                part_idx += 1
                continue

            if kf_after_s - s > 0.05:
                p = os.path.join(tmp, f"part_{part_idx:04d}.mp4")
                _reencode_with_fallback(
                    profile, video_path, s, kf_after_s, p, warned)
                part_paths.append(p)
                part_idx += 1

            p = os.path.join(tmp, f"part_{part_idx:04d}.mp4")
            _stream_copy(video_path, kf_after_s, kf_before_e, p)
            part_paths.append(p)
            part_idx += 1

            if e - kf_before_e > 0.05:
                p = os.path.join(tmp, f"part_{part_idx:04d}.mp4")
                _reencode_with_fallback(
                    profile, video_path, kf_before_e, e, p, warned)
                part_paths.append(p)
                part_idx += 1

        _concat(part_paths, output_path)
