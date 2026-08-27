"""Compatibilité avec l'ancien projet.

Le pipeline actuel ne ré-encode volontairement ni la vidéo ni l'audio : il
utilise FFmpeg en `-c copy`. Les encodeurs GPU ne sont donc pas utilisés.
"""
from dataclasses import dataclass, field
from typing import List, Optional

@dataclass
class EncoderProfile:
    name: str
    encoder: str
    pre_input_args: List[str] = field(default_factory=list)
    filter_args: List[str] = field(default_factory=list)
    encode_args: List[str] = field(default_factory=list)
    decode_hwaccel: Optional[str] = None

SOFTWARE = EncoderProfile('software', 'libx264', encode_args=['-preset', 'veryfast', '-crf', '20'])

def list_available_encoders(vaapi_device='/dev/dri/renderD128'):
    del vaapi_device
    return []

def get_encoder_profile(prefer='auto', vaapi_device='/dev/dri/renderD128'):
    del prefer, vaapi_device
    return SOFTWARE

def describe(profile):
    return 'Aucun ré-encodage : vidéo + audio copiés avec -c copy'
