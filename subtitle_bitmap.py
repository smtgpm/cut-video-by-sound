"""Retiming of bitmap subtitles (PGS and VobSub).

These cannot be treated as independent cues:
- PGS is a stream of screen updates that depend on earlier ones;
- VobSub stores show/hide delays inside each image packet.
"""

from __future__ import annotations

import struct
from typing import List, Tuple

from timeline import Timeline

PGS_COMPOSITION = 0x16
# Argument sizes of the VobSub display-control commands.
SPU_COMMAND_SIZES = {0: 0, 1: 0, 2: 0, 3: 2, 4: 2, 5: 6, 6: 4}


def _pgs_state(payload: bytes) -> Tuple[bool, bool] | None:
    """(is a decoder refresh, shows something) for a PGS display set."""
    pos = 0
    while pos + 3 <= len(payload):
        kind, size = struct.unpack_from(">BH", payload, pos)
        body = payload[pos + 3:pos + 3 + size]
        if kind == PGS_COMPOSITION and len(body) >= 11:
            return body[7] != 0, body[10] > 0
        pos += 3 + size
    return None


def retime_pgs(
    sets: List[Tuple[int, bytes]], timeline: Timeline,
) -> List[Tuple[int, bytes]]:
    """
    Retime (time, display set) pairs.

    If a subtitle is on screen when a removed part ends, the updates needed to
    draw it are moved to the junction. If one was cleared inside the removed
    part, so is the clear.
    """
    states, previous = [], (True, False)
    for _, payload in sets:
        previous = _pgs_state(payload) or (False, previous[1])
        states.append(previous)

    selected = {i for i, (t, _) in enumerate(sets) if not timeline.removed(t)}

    for a, b in timeline.cuts:
        inside = [i for i, (t, _) in enumerate(sets) if a <= t < b]
        if not inside:
            continue

        first, last = inside[0], inside[-1]
        shown_before = first > 0 and states[first - 1][1]
        shown_after = states[last][1]
        next_needs_history = last + 1 < len(sets) and not states[last + 1][0]

        if shown_after or next_needs_history:
            refresh = last
            while refresh > 0 and not states[refresh][0]:
                refresh -= 1
            selected.update(i for i in inside if i >= refresh)
        elif shown_before:
            selected.add(last)

    result: List[Tuple[int, bytes]] = []
    for i in sorted(selected):
        when = timeline.map(sets[i][0])
        if result:
            when = max(when, result[-1][0] + 1)
        result.append((when, sets[i][1]))
    return result


def _spu_controls(spu: bytes) -> List[Tuple[int, int, bool]]:
    """(offset, delay in ms, hides the image) for each control sequence."""
    if len(spu) < 4:
        return []

    pos, found = struct.unpack_from(">H", spu, 2)[0], []
    while pos + 4 <= len(spu):
        date, following = struct.unpack_from(">HH", spu, pos)
        hides, i = False, pos + 4
        while i < len(spu) and spu[i] in SPU_COMMAND_SIZES:
            hides |= spu[i] == 2
            i += 1 + SPU_COMMAND_SIZES[spu[i]]
        found.append((pos, round(date * 1024 / 90), hides))
        if following <= pos:
            break
        pos = following
    return found


def spu_duration(spu: bytes) -> int:
    return max((ms for _, ms, hides in _spu_controls(spu) if hides), default=0)


def spu_retime(spu: bytes, head_trim: int, length: int) -> bytes:
    """Shift and clip the delays stored inside a VobSub packet."""
    patched = bytearray(spu)
    for offset, ms, _ in _spu_controls(spu):
        delay = min(max(0, ms - head_trim), length)
        struct.pack_into(">H", patched, offset, min(65535, round(delay * 90 / 1024)))
    return bytes(patched)
