"""
hwaccel.py
Détecte les encodeurs vidéo matériels disponibles dans l'installation ffmpeg
locale (NVIDIA NVENC, Intel QuickSync, Apple VideoToolbox, VAAPI Linux) et
fournit les arguments ffmpeg correspondants. Se dégrade proprement vers
libx264 (CPU) si rien n'est détecté, si l'utilisateur force le logiciel, ou
si l'encodage matériel échoue à l'exécution (voir remove_segments.py, qui
retente automatiquement en logiciel en cas d'échec).
"""

import functools
import subprocess
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class EncoderProfile:
    name: str
    encoder: str
    # avant -i (ex: -vaapi_device ...)
    pre_input_args: List[str] = field(default_factory=list)
    # ex: -vf format=nv12,hwupload
    filter_args: List[str] = field(default_factory=list)
    encode_args: List[str] = field(default_factory=list)       # preset/qualité
    # valeur pour -hwaccel (décodage GPU)
    decode_hwaccel: Optional[str] = None


def _candidates(vaapi_device: str) -> List[EncoderProfile]:
    # Ordre de préférence : matériel le plus courant/robuste d'abord.
    return [
        EncoderProfile(
            name="nvenc",
            encoder="h264_nvenc",
            encode_args=[
                "-preset", "p4",
                "-rc", "vbr",
                "-cq", "19",
                "-pix_fmt", "yuv420p",
            ],
            decode_hwaccel=None,
        ),
        EncoderProfile(
            name="qsv", encoder="h264_qsv",
            encode_args=["-preset", "medium", "-global_quality", "20"],
            decode_hwaccel="qsv",
        ),
        EncoderProfile(
            name="videotoolbox", encoder="h264_videotoolbox",
            encode_args=["-q:v", "60"],
            decode_hwaccel="videotoolbox",
        ),
        EncoderProfile(
            name="vaapi", encoder="h264_vaapi",
            pre_input_args=["-vaapi_device", vaapi_device],
            filter_args=["-vf", "format=nv12,hwupload"],
            encode_args=["-qp", "20"],
            # le décodage vaapi complique le filter-chain ; on décode en CPU par défaut
            decode_hwaccel=None,
        ),
    ]


SOFTWARE = EncoderProfile(
    name="software", encoder="libx264",
    encode_args=["-preset", "veryfast", "-crf", "20"],
)


@functools.lru_cache(maxsize=1)
def _ffmpeg_encoders_output() -> str:
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True)
    return result.stdout


def list_available_encoders(vaapi_device: str = "/dev/dri/renderD128") -> List[str]:
    """Retourne les noms internes ('nvenc', 'qsv', ...) détectés comme disponibles dans ffmpeg."""
    out = _ffmpeg_encoders_output()
    return [c.name for c in _candidates(vaapi_device) if c.encoder in out]


def get_encoder_profile(prefer: str = "auto", vaapi_device: str = "/dev/dri/renderD128") -> EncoderProfile:
    """
    prefer : "auto" (détection automatique, GPU en priorité), "none"/"software"/"cpu"
             (force libx264), ou un nom explicite ("nvenc", "qsv", "videotoolbox", "vaapi").
    """
    if prefer in ("none", "software", "cpu"):
        return SOFTWARE

    out = _ffmpeg_encoders_output()
    candidates = _candidates(vaapi_device)

    if prefer and prefer != "auto":
        for c in candidates:
            if c.name == prefer:
                if c.encoder not in out:
                    raise RuntimeError(
                        f"L'encodeur '{c.encoder}' ({prefer}) n'est pas disponible dans cette "
                        f"build de ffmpeg. Lance 'ffmpeg -encoders' pour vérifier."
                    )
                return c
        raise ValueError(f"Choix --hw inconnu : {prefer}")

    for c in candidates:
        if c.encoder in out:
            return c
    return SOFTWARE


def describe(profile: EncoderProfile) -> str:
    if profile.name == "software":
        return "CPU (libx264 — aucun GPU détecté, ou mode logiciel forcé)"
    return f"GPU via {profile.encoder} ({profile.name})"
