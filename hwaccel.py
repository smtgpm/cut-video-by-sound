"""Compatibility with the old project.

The current pipeline intentionally re-encodes neither video nor audio: it
uses FFmpeg with `-c copy`. GPU encoders are therefore not used.
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
    return 'No re-encoding: video + audio copied with -c copy'
