"""Discovery and retiming of subtitle files stored next to a video.

Files are edited in place in memory: only timestamps change, so styling,
encoding, line endings and formatting survive. Source files are never written.

Rules, for every format:
- a cue entirely inside a removed part is dropped;
- a cue overlapping a removed part is shortened by the removed amount;
- everything later moves back by the total removed before it.

Streams inside the container are handled by remove_segments.py.
"""

from __future__ import annotations

import codecs
import os
import re
import struct
from typing import Iterable, List, Tuple

from subtitle_bitmap import retime_pgs, spu_duration, spu_retime
from timeline import Timeline

SUBTITLE_DIRS = {"subs", "sub", "subtitles", "subtitle"}
UNSUPPORTED = {".ttml", ".dfxp", ".sbv", ".lrc", ".usf", ".stl", ".mks"}

SRT_TIMING = re.compile(
    r"^[ \t]*(\d+):(\d\d):(\d\d)[,.](\d{1,3})[ \t]*-->[ \t]*"
    r"(\d+):(\d\d):(\d\d)[,.](\d{1,3})(.*)$"
)
VTT_TIME = r"(?:\d+:)?\d\d:\d\d\.\d{3}"
VTT_TIMING = re.compile(rf"^({VTT_TIME})[ \t]+-->[ \t]+({VTT_TIME})(.*)$")


def _clock(h, m, s, fraction="0") -> int:
    return (int(h) * 3600 + int(m) * 60 + int(s)) * 1000 + int(fraction.ljust(3, "0")[:3])


def _parts(ms: int) -> Tuple[int, int, int, int]:
    seconds, ms = divmod(ms, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return hours, minutes, seconds, ms


def _split_eol(line: str) -> Tuple[str, str]:
    body = line.rstrip("\r\n")
    return body, line[len(body):]


def retime_srt(text: str, timeline: Timeline) -> str:
    lines = text.splitlines(True)
    newline = "\r\n" if "\r\n" in text else "\n"

    # Split on timing lines, not blank lines: blank lines inside cues and
    # missing separators are both common in the wild.
    timings = [i for i, line in enumerate(lines) if SRT_TIMING.match(_split_eol(line)[0])]
    if not timings:
        if text.strip():
            raise ValueError("no SRT timestamps found")
        return text

    blocks = []
    for k, i in enumerate(timings):
        stop = len(lines)
        if k + 1 < len(timings):
            stop = timings[k + 1]
            if stop > i + 1 and lines[stop - 1].strip().isdigit():
                stop -= 1
        body = [_split_eol(line)[0] for line in lines[i + 1:stop]]
        while body and not body[-1].strip():
            body.pop()

        match = SRT_TIMING.match(_split_eol(lines[i])[0])
        span = timeline.span(_clock(*match.groups()[:4]), _clock(*match.groups()[4:8]))
        if span:
            start, end = ("%02d:%02d:%02d,%03d" % _parts(t) for t in span)
            timing = f"{start} --> {end}{match.group(9)}"
            blocks.append(newline.join([str(len(blocks) + 1), timing, *body]))

    return "".join(block + newline * 2 for block in blocks)


def retime_vtt(text: str, timeline: Timeline) -> str:
    def parse(value: str) -> int:
        clock, fraction = value.split(".")
        return _clock(*([0, *clock.split(":")][-3:]), fraction)

    def fmt(ms: int) -> str:
        return "%02d:%02d:%02d.%03d" % _parts(ms)

    newline = "\r\n" if "\r\n" in text else "\n"
    result = []
    for block in re.split(r"\n[ \t]*\n+", text.replace("\r\n", "\n").strip("\n")):
        lines = block.split("\n")
        for i, line in enumerate(lines[:2]):  # an optional cue id, then the timing
            match = VTT_TIMING.match(line)
            if not match:
                continue
            span = timeline.span(parse(match.group(1)), parse(match.group(2)))
            if span:
                lines[i] = f"{fmt(span[0])} --> {fmt(span[1])}{match.group(3)}"
                for j in range(i + 1, len(lines)):
                    lines[j] = re.sub(
                        rf"<({VTT_TIME})>",
                        lambda m: f"<{fmt(timeline.map(parse(m.group(1))))}>",
                        lines[j],
                    )
            else:
                lines = []
            break
        if lines:
            result.append(newline.join(lines))
    return (newline * 2).join(result) + newline


def retime_ass(text: str, timeline: Timeline) -> str:
    def parse(value: str) -> int:
        h, m, s = value.strip().split(":")
        seconds, _, fraction = s.partition(".")
        return _clock(h, m, seconds, fraction)

    def fmt(ms: int) -> str:
        h, m, s, rest = _parts((ms + 5) // 10 * 10)
        return f"{h}:{m:02d}:{s:02d}.{rest // 10:02d}"

    result, section = [], ""
    start_col, end_col, columns = 1, 2, 10

    for line in text.splitlines(True):
        body, eol = _split_eol(line)
        key, colon, value = body.partition(":")
        key = key.strip().lower()

        if body.lstrip().startswith("["):
            section = body.strip().lower()
        elif section == "[events]" and colon:
            if key == "format":
                names = [name.strip().lower() for name in value.split(",")]
                start_col, end_col = names.index("start"), names.index("end")
                columns = len(names)
            elif key in ("dialogue", "comment"):
                fields = value.split(",", columns - 1)
                span = timeline.span(parse(fields[start_col]), parse(fields[end_col]))
                if not span:
                    continue
                fields[start_col], fields[end_col] = map(fmt, span)
                line = f"{body.partition(':')[0]}:{','.join(fields)}{eol}"
        result.append(line)

    if "[events]" not in text.lower():
        raise ValueError("no [Events] section found")
    return "".join(result)


def retime_microdvd(text: str, timeline: Timeline, fps: float) -> str:
    result, found = [], False
    for line in text.splitlines(True):
        body, eol = _split_eol(line)
        match = re.match(r"^\{(\d+)\}\{(\d+)\}(.*)$", body)
        if not match:
            result.append(line)
            continue

        first, last, content = int(match.group(1)), int(match.group(2)), match.group(3)
        if not found and first == last and first <= 1 and re.fullmatch(r"\d\d(\.\d+)?", content):
            fps = float(content)  # header declaring the frame rate
            result.append(line)
            continue

        found = True
        span = timeline.span(round(first * 1000 / fps), round(last * 1000 / fps))
        if span:
            first, last = (round(t * fps / 1000) for t in span)
            result.append(f"{{{first}}}{{{last}}}{content}{eol}")

    if not found:
        raise ValueError("only MicroDVD and VobSub .sub files are supported")
    return "".join(result)


def retime_smi(text: str, timeline: Timeline) -> str:
    head, *blocks = re.split(r"(?i)(?=<sync\b)", text)
    if not blocks:
        raise ValueError("no SAMI <SYNC> tags found")

    tail = ""
    closing = re.search(r"(?i)</body>", blocks[-1])
    if closing:
        tail = blocks[-1][closing.start():]
        blocks[-1] = blocks[-1][:closing.start()]

    # Each SYNC is a state change per language: inside a cut, only the last
    # state of each language matters, and it takes effect at the junction.
    events, last_in_cut = [], {}
    for i, block in enumerate(blocks):
        match = re.search(r"(?i)\bstart\s*=\s*[\"']?(\d+)", block)
        if not match:
            raise ValueError("SAMI <SYNC> without a Start time")
        t = int(match.group(1))
        language = re.search(r"(?i)\bclass\s*=\s*[\"']?([\w-]+)", block)
        for a, b in timeline.cuts:
            if a <= t < b:
                last_in_cut[(a, language and language.group(1).lower())] = i
        events.append((t, block, match.span(1)))

    result = [head]
    for i, (t, block, (lo, hi)) in enumerate(events):
        if not timeline.removed(t) or i in last_in_cut.values():
            result.append(f"{block[:lo]}{timeline.map(t)}{block[hi:]}")
    return "".join(result) + tail


def retime_sup(data: bytes, timeline: Timeline) -> bytes:
    sets, segments, when, pos = [], b"", None, 0
    while pos + 13 <= len(data):
        if data[pos:pos + 2] != b"PG":
            raise ValueError("invalid .sup file")
        pts, _, kind, size = struct.unpack_from(">IIBH", data, pos + 2)
        if when is None:
            when = round(pts / 90)
        segments += data[pos + 10:pos + 13 + size]
        pos += 13 + size
        if kind == 0x80:  # end of display set
            sets.append((when, segments))
            segments, when = b"", None

    result = bytearray()
    for t, payload in retime_pgs(sets, timeline):
        i = 0
        while i < len(payload):
            size = 3 + struct.unpack_from(">H", payload, i + 1)[0]
            result += b"PG" + struct.pack(">II", t * 90, 0) + payload[i:i + size]
            i += size
    return bytes(result)


def _spu_positions(data: bytes, pos: int) -> List[int]:
    """Byte positions of one subtitle picture inside a VobSub program stream."""
    positions: List[int] = []
    size, substream = None, None
    while pos + 6 <= len(data) and data[pos:pos + 3] == b"\x00\x00\x01":
        kind = data[pos + 3]
        if kind == 0xBA:
            pos += 14 + (data[pos + 13] & 7)
            continue
        end = pos + 6 + struct.unpack_from(">H", data, pos + 4)[0]
        if kind == 0xBD:
            payload = pos + 9 + data[pos + 8]
            if substream is None:
                substream = data[payload]
            if data[payload] == substream:
                positions.extend(range(payload + 1, min(end, len(data))))
                if size is None and len(positions) >= 2:
                    size = data[positions[0]] << 8 | data[positions[1]]
                if size and len(positions) >= size:
                    return positions[:size]
        pos = end
    raise ValueError("invalid VobSub .sub file")


def retime_vobsub(index: str, data: bytes, timeline: Timeline) -> Tuple[str, bytes]:
    patched, result = bytearray(data), []
    delay = 0
    for line in index.splitlines(True):
        body, eol = _split_eol(line)
        if body.startswith("id:"):
            delay = 0
        shift = re.match(r"delay:\s*([+-]?)(\d+):(\d\d):(\d\d):(\d+)", body)
        if shift:
            sign, *value = shift.groups()
            delay += (-1 if sign == "-" else 1) * _clock(*value)
            continue  # folded into the timestamps below

        cue = re.match(r"timestamp:\s*(\d+):(\d\d):(\d\d):(\d{3}),\s*filepos:\s*([0-9a-fA-F]+)", body)
        if not cue:
            result.append(line)
            continue

        *value, filepos = cue.groups()
        start = max(0, _clock(*value) + delay)
        where = _spu_positions(data, int(filepos, 16))
        spu = bytes(data[i] for i in where)
        duration = spu_duration(spu)
        span = timeline.span(start, start + duration)
        if not span:
            continue
        if duration and span[1] - span[0] != duration:
            trim = next((b - start for a, b in timeline.cuts if a <= start < b), 0)
            for i, byte in zip(where, spu_retime(spu, trim, span[1] - span[0])):
                patched[i] = byte
        stamp = "%02d:%02d:%02d:%03d" % _parts(span[0])
        result.append(f"timestamp: {stamp}, filepos: {filepos}{eol}")
    return "".join(result), bytes(patched)


def _decode(data: bytes) -> Tuple[str, str, bytes]:
    for bom, encoding in (
        (codecs.BOM_UTF8, "utf-8"),
        (codecs.BOM_UTF16_LE, "utf-16-le"),
        (codecs.BOM_UTF16_BE, "utf-16-be"),
    ):
        if data.startswith(bom):
            return data[len(bom):].decode(encoding), encoding, bom
    try:
        return data.decode("utf-8"), "utf-8", b""
    except UnicodeDecodeError:
        # Unknown legacy code page (cp1252, cp1251, GBK...). Latin-1 maps every
        # byte to itself, and only ASCII timestamps are edited, so the original
        # bytes are written back untouched. str.splitlines() would also split
        # on byte 0x85, which is harmless because the pieces are rejoined.
        return data.decode("latin-1"), "latin-1", b""


def find_sidecars(video: str, video_extensions: Iterable[str]) -> List[Tuple[str, str]]:
    """
    Return (path, name suffix) for the subtitle files belonging to a video.

    Recognised layouts: <video>.srt, <video>.en.forced.srt, the same inside a
    Subs/ folder, and Subs/<video>/<anything>.srt. When several video names
    match, the longest wins, so "Show.srt" and "Show.Part2.srt" stay apart.
    """
    folder = os.path.dirname(os.path.abspath(video))
    stem = os.path.splitext(os.path.basename(video))[0]
    entries = sorted(os.listdir(folder))
    stems = {
        os.path.splitext(name)[0] for name in entries
        if os.path.splitext(name)[1].lower() in set(video_extensions)
    } | {stem}

    places = [(folder, False)]
    for name in entries:
        path = os.path.join(folder, name)
        if name.lower() in SUBTITLE_DIRS and os.path.isdir(path):
            places.append((path, False))
            if os.path.isdir(os.path.join(path, stem)):
                places.append((os.path.join(path, stem), True))

    known = set(RETIMERS) | UNSUPPORTED | {".sub", ".idx", ".sup"}
    found = []
    for place, owned in places:
        names = sorted(os.listdir(place))
        indexes = {os.path.splitext(n)[0] for n in names if n.lower().endswith(".idx")}
        for name in names:
            base, ext = os.path.splitext(name)
            path = os.path.join(place, name)
            if ext.lower() not in known or not os.path.isfile(path):
                continue
            if ext.lower() == ".sub" and base in indexes:
                continue  # written together with its .idx
            if owned:
                found.append((path, f".{base}{ext}"))
                continue
            owners = [
                s for s in stems
                if base == s or (base.startswith(s) and base[len(s)] in "._- [(")
            ]
            if owners and max(owners, key=len) == stem:
                found.append((path, name[len(stem):]))
    return found


RETIMERS = {
    ".srt": retime_srt,
    ".vtt": retime_vtt,
    ".ass": retime_ass,
    ".ssa": retime_ass,
    ".smi": retime_smi,
    ".sami": retime_smi,
}


def _retime_file(path: str, timeline: Timeline, fps: float | None) -> List[Tuple[str, bytes]]:
    """Return the (extension, content) files to write for one sidecar."""
    base, ext = os.path.splitext(path)
    kind = ext.lower()
    with open(path, "rb") as handle:
        data = handle.read()

    if not timeline.cuts:
        files = [(ext, data)]
        if kind == ".idx":
            files.append(_read_vobsub_data(base))
        return files
    if kind in UNSUPPORTED:
        raise ValueError(f"{kind} is not supported, convert it to .srt")
    if kind == ".sup":
        return [(ext, retime_sup(data, timeline))]

    text, encoding, bom = _decode(data)
    if kind == ".idx":
        sub_ext, sub_data = _read_vobsub_data(base)
        text, sub_data = retime_vobsub(text, sub_data, timeline)
        return [(ext, bom + text.encode(encoding)), (sub_ext, sub_data)]
    if kind == ".sub":
        text = retime_microdvd(text, timeline, fps or 25.0)
    else:
        text = RETIMERS[kind](text, timeline)
    return [(ext, bom + text.encode(encoding))]


def _read_vobsub_data(base: str) -> Tuple[str, bytes]:
    for ext in (".sub", ".SUB"):
        if os.path.isfile(base + ext):
            with open(base + ext, "rb") as handle:
                return ext, handle.read()
    raise ValueError("the .idx has no matching .sub")


def retime_sidecars(
    video: str,
    timeline: Timeline,
    video_extensions: Iterable[str],
    fps: float | None = None,
) -> Tuple[List[Tuple[str, bytes]], List[str]]:
    """
    Retimed content for each subtitle file of a video, to be written over the
    file itself. Nothing is written here. Returns ((path, content), warnings).
    """
    files, warnings = [], []
    for path, _suffix in find_sidecars(video, video_extensions):
        try:
            base = os.path.splitext(path)[0]
            files += [(base + ext, content) for ext, content in _retime_file(path, timeline, fps)]
        except (ValueError, IndexError, OSError, struct.error) as exc:
            warnings.append(f"subtitle NOT retimed, now out of sync: {os.path.basename(path)}: {exc}")
    return files, warnings


def write_sidecars(
    video: str,
    output_video: str,
    timeline: Timeline,
    video_extensions: Iterable[str],
    fps: float | None = None,
) -> Tuple[List[str], List[str]]:
    """Write retimed copies next to the output. Returns (written, warnings)."""
    output_base = os.path.splitext(output_video)[0]
    written, warnings = [], []

    for path, suffix in find_sidecars(video, video_extensions):
        try:
            files = _retime_file(path, timeline, fps)
            # Not splitext(suffix): it reads ".srt" as a name without extension.
            extension = os.path.splitext(path)[1]
            base = original = output_base + suffix[:-len(extension)]
            number = 1
            while any(base + ext in written for ext, _ in files):
                number += 1
                base = f"{original}.{number}"
            for ext, content in files:
                if os.path.abspath(base + ext) == os.path.abspath(path):
                    raise ValueError("output would overwrite the source")
                with open(base + ext, "wb") as handle:
                    handle.write(content)
                written.append(base + ext)
        except (ValueError, IndexError, OSError, struct.error) as exc:
            warnings.append(f"subtitle NOT retimed: {os.path.basename(path)}: {exc}")
    return written, warnings
