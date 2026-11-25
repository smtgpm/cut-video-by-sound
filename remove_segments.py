"""
Fast exact-timestamp removal.

Strategy:
- Removal timestamps are exact and are never expanded.
- Untouched middle portions of H.264/HEVC are stream-copied. Only the frames
  between a cut and the nearest usable keyframe are re-encoded. Other video
  codecs cannot be spliced, so their kept frames are re-encoded.
- Audio is never re-encoded. Whole packets are selected, so a junction is
  within half a packet (about 10-16 ms) of the cut, without accumulating.
- Subtitle streams, chapters and attachments (fonts) are kept and retimed.

Packets are timestamped individually from one Timeline. FFmpeg's -ss with
-c copy and its concat demuxer are deliberately not used: the first seeks to
earlier keyframes (repeating footage), the second accumulates drift.
"""

from __future__ import annotations

import bisect
import heapq
import json
import os
import shutil
import statistics
import struct
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Callable, List, Set, Tuple

import av

from subtitle_bitmap import retime_pgs, spu_duration, spu_retime
from timeline import FOREVER, Timeline

MILLISECONDS = Fraction(1, 1000)
ENCODES_AT_ONCE = max(1, min(4, (os.cpu_count() or 1) // 4))  # boundary pieces of one video
PLANAR_FORMATS = {
    "yuv420p", "yuvj420p", "yuv422p", "yuv444p",
    "yuv420p10le", "yuv422p10le", "yuv444p10le",
}
TEXT_SUBTITLES = {"mov_text", "text", "subviewer", "microdvd", "sami"}
COPY_SUBTITLES = {
    "subrip", "ass", "webvtt", "hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle",
}


class Cancelled(Exception):
    pass


@dataclass
class CutResult:
    timeline: Timeline
    fps: float | None
    warnings: List[str] = field(default_factory=list)


@dataclass
class _Copy:
    first: int  # decode-order packet range
    stop: int
    min_pts: int  # older packets are leading pictures of an open GOP
    pts: List[int]


@dataclass
class _Encoded:
    path: str
    pts: List[int]  # decode order


def _run_checked(command, label: str) -> str:
    result = subprocess.run(command, capture_output=True, text=True, errors="replace")
    if result.returncode:
        raise RuntimeError(f"{label} failed:\n{result.stderr.strip()[-5000:]}")
    return result.stdout


def _probe(path: str) -> dict:
    return json.loads(_run_checked([
        "ffprobe", "-v", "error",
        "-show_streams", "-show_format", "-show_chapters",
        "-of", "json", path,
    ], "ffprobe"))


def _ms(value, time_base) -> int:
    """
    value * time_base in milliseconds, rounded half to even like round() of a
    Fraction, but with plain integers: this runs for every packet.
    """
    top = getattr(time_base, "numerator", None)
    bottom = getattr(time_base, "denominator", None)
    if type(value) is not int or type(top) is not int or type(bottom) is not int or bottom <= 0:
        return round(value * time_base * 1000)
    whole, rest = divmod(value * top * 1000, bottom)
    twice = 2 * rest
    if twice > bottom or (twice == bottom and whole & 1):
        whole += 1
    return whole


def _packets(container, stream):
    for packet in container.demux(stream):
        if packet.size:
            yield packet


# ---------- Input ----------

def _as_matroska(
    source: str, probe: dict, origin: int, work: str, warnings: List[str],
) -> str:
    """
    Losslessly remux to Matroska starting at zero, unless already so. This
    gives a single packet layout, timebase and zero point to the pipeline.
    """
    if "matroska" in probe["format"]["format_name"] and not origin:
        return source

    command = [
        "ffmpeg", "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-fflags", "+genpts", "-i", source,
    ]
    codecs, count = [], 0
    for stream in probe["streams"]:
        kind, name = stream.get("codec_type"), stream.get("codec_name", "unknown")
        codec = "copy"
        if stream.get("disposition", {}).get("attached_pic"):
            continue
        if kind == "audio" and name in ("pcm_bluray", "pcm_dvd"):
            codec = "pcm_s24le"
        elif kind == "subtitle":
            if name in TEXT_SUBTITLES:
                codec = "srt"
            elif name not in COPY_SUBTITLES:
                warnings.append(f"subtitle stream #{stream['index']} ({name}) is not supported")
                continue
        elif kind not in ("video", "audio", "attachment"):
            continue
        command += ["-map", f"0:{stream['index']}"]
        codecs += [f"-c:{count}", codec]
        count += 1

    output = os.path.join(work, "source.mkv")
    _run_checked(
        command + codecs + [
            "-max_interleave_delta", "0",
            # Do not shift every track by the audio encoder's priming delay.
            "-avoid_negative_ts", "disabled",
            output,
        ],
        "FFmpeg remux to Matroska",
    )
    return output


# ---------- Video ----------

def _parameter_sets(codec: str, extradata: bytes) -> List[bytes] | None:
    """SPS/PPS/VPS from avcC/hvcC, or None if the stream cannot be spliced."""
    sets: List[bytes] = []
    try:
        if not extradata or extradata[0] != 1:
            return None
        if codec == "h264":
            if extradata[4] & 3 != 3:  # only 4-byte NAL lengths
                return None
            pos = 5
            for mask in (31, 255):
                count, pos = extradata[pos] & mask, pos + 1
                for _ in range(count):
                    size = struct.unpack_from(">H", extradata, pos)[0]
                    sets.append(extradata[pos + 2:pos + 2 + size])
                    pos += 2 + size
        elif codec == "hevc":
            if extradata[21] & 3 != 3:
                return None
            pos = 23
            for _ in range(extradata[22]):
                kind, count = struct.unpack_from(">BH", extradata, pos)
                pos += 3
                for _ in range(count):
                    size = struct.unpack_from(">H", extradata, pos)[0]
                    if kind & 63 in (32, 33, 34):
                        sets.append(extradata[pos + 2:pos + 2 + size])
                    pos += 2 + size
        else:
            return None
    except (IndexError, struct.error):
        return None
    return sets


def _nal_types(payload: bytes, codec: str) -> List[int]:
    types, pos = [], 0
    while pos + 4 < len(payload):
        header = payload[pos + 4]
        types.append(header & 31 if codec == "h264" else (header >> 1) & 63)
        pos += 4 + struct.unpack_from(">I", payload, pos)[0]
    return types


def _with_parameter_sets(payload: bytes, codec: str, sets: List[bytes]) -> bytes:
    """
    Copied and re-encoded parts use different parameter sets with the same
    ids, so every keyframe must carry its own, or seeking shows garbage.
    """
    at = 0
    types = _nal_types(payload, codec)
    if types and types[0] == (9 if codec == "h264" else 35):
        # An access unit delimiter has to stay first.
        at = 4 + struct.unpack_from(">I", payload)[0]
    extra = b"".join(struct.pack(">I", len(s)) + s for s in sets)
    return payload[:at] + extra + payload[at:]


def _as_broken_link(payload: bytes) -> bytes:
    """
    Turn a CRA/BLA keyframe into BLA_N_LP, HEVC's marker for a splice point:
    picture order restarts, and frames the decoder still holds are displayed.
    (An end-of-sequence marker before a CRA would make it discard them.)
    """
    patched, pos = bytearray(payload), 0
    while pos + 4 < len(payload):
        if (payload[pos + 4] >> 1) & 63 in (16, 17, 18, 21):
            patched[pos + 4] = (18 << 1) | (payload[pos + 4] & 0x81)
        pos += 4 + struct.unpack_from(">I", payload, pos)[0]
    return bytes(patched)


def _reorder_depth(pts: List[int]) -> int:
    rank = {value: i for i, value in enumerate(sorted(pts))}
    return max((i - rank[value] for i, value in enumerate(pts)), default=0)


class _Video:
    def __init__(self, path: str, info: dict, crf: int, preset: str, log, progress, cancelled):
        self.path, self.info, self.crf, self.preset = path, info, crf, preset
        self.log, self.progress, self.cancelled = log, progress, cancelled
        self.pts: List[int] = []
        self.keys: List[int] = []  # decode indexes of keyframes
        self.entries: Set[int] = set()  # keyframes a copy may start at
        self.spliceable = True

        with av.open(path) as container:
            stream = container.streams.video[0]
            self.codec = stream.codec_context.name
            self.fps = stream.average_rate
            self.sets = _parameter_sets(self.codec, stream.codec_context.extradata)
            if self.sets is None:
                self.spliceable = False

            for packet in _packets(container, stream):
                if cancelled():
                    raise Cancelled()
                if packet.pts is None:
                    self.spliceable = False
                    continue
                index = len(self.pts)
                self.pts.append(_ms(packet.pts, stream.time_base))
                if not packet.is_keyframe:
                    continue
                self.keys.append(index)
                if self.sets is not None:
                    types = _nal_types(bytes(packet), self.codec)
                    if self.codec == "h264":
                        usable = 5 in types  # IDR
                    else:
                        usable = any(16 <= kind <= 21 for kind in types)
                    if usable or index == 0:
                        self.entries.add(index)

        if not self.pts:
            raise RuntimeError("No video frames found")
        if len(set(self.pts)) != len(self.pts) or info.get("field_order") in ("tt", "bb", "tb", "bt"):
            self.spliceable = False

        self.key_end = max(self.pts)
        self.key_times = sorted(self.pts[i] for i in self.keys)
        self.reorder = _reorder_depth(self.pts)
        self.lowest_after = list(self.pts) + [FOREVER]
        for i in range(len(self.pts) - 1, -1, -1):
            self.lowest_after[i] = min(self.pts[i], self.lowest_after[i + 1])

    def plan(self, timeline: Timeline, work: str) -> List[_Copy | _Encoded]:
        keeps = timeline.keeps()
        if not self.spliceable:
            self.log(f"  {self.codec}: cannot splice this stream, re-encoding the kept video")
            return [self._encode(keeps, os.path.join(work, "video.mkv"))]

        pieces: List[_Copy | _Encoded] = []
        encodes = []  # (index in pieces, frames), run side by side below
        for start, end in keeps:
            frames = sorted(p for p in self.pts if start <= p < end)
            if not frames:
                continue

            copy = self._copy_range(frames, end == FOREVER)
            if copy:
                low, high = min(copy.pts), max(copy.pts)
                ranges = [[p for p in frames if p < low], copy, [p for p in frames if p > high]]
            else:
                ranges = [frames]

            for part in ranges:
                if isinstance(part, _Copy):
                    self.log(f"  Copy: {low / 1000:.3f}s -> {high / 1000:.3f}s")
                    pieces.append(part)
                elif part:
                    self.log(f"  Re-encode: {part[0] / 1000:.3f}s -> {part[-1] / 1000:.3f}s")
                    encodes.append((len(pieces), part))
                    pieces.append(None)

        # Each boundary piece is decoded and encoded on its own, so they run in
        # parallel. Same frames, same settings: the output is the same.
        def encode(job):
            index, part = job
            return index, self._encode([(part[0], part[-1] + 1)], os.path.join(work, f"{index:04d}.mkv"))

        with ThreadPoolExecutor(max(1, min(ENCODES_AT_ONCE, len(encodes)))) as pool:
            for index, piece in pool.map(encode, encodes):
                pieces[index] = piece
        return pieces

    def _copy_range(self, frames: List[int], last: bool) -> _Copy | None:
        first = next(
            (i for i in self.keys
             if i in self.entries and frames[0] <= self.pts[i] <= frames[-1]),
            None,
        )
        if first is None:
            return None

        if last:
            stop = len(self.pts)
        else:
            # Stop before a keyframe *and* its leading pictures. At least one
            # re-encoded frame always follows, which closes the copied GOP.
            stop = next(
                (i for i in reversed(self.keys)
                 if i > first and self.lowest_after[i] <= frames[-1]),
                None,
            )
            if stop is None:
                return None

        start = self.pts[first]
        copied = [p for p in self.pts[first:stop] if p >= start]
        if not copied:
            return None
        expected = bisect.bisect_right(frames, max(copied)) - bisect.bisect_left(frames, start)
        if expected != len(copied):
            return None  # irregular GOP
        return _Copy(first, stop, start, copied)

    def _encoder_command(self, pix_fmt: str, width: int, height: int, output: str):
        rate = str(self.fps or 25)
        command = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "rawvideo", "-pix_fmt", pix_fmt, "-s", f"{width}x{height}", "-r", rate,
        ]
        # Declared on the input too, so that nothing gets colour-converted.
        colours = []
        for key, option in (
            ("color_range", "-color_range"),
            ("color_space", "-colorspace"),
            ("color_transfer", "-color_trc"),
            ("color_primaries", "-color_primaries"),
        ):
            value = self.info.get(key)
            # "gbr" is meaningless for the YUV that is encoded here.
            if value and value not in ("unknown", "reserved", "gbr"):
                colours += [option, value]
        command += colours + ["-i", "pipe:0"]

        aspect = self.info.get("sample_aspect_ratio", "0:1")
        if not aspect.startswith("0:"):
            command += ["-vf", "setsar=" + aspect.replace(":", "/")]

        if self.codec == "hevc":
            # Boundary pieces are a few seconds at most: no B-frames and closed
            # GOPs keep the joins with copied pieces simple.
            command += [
                "-c:v", "libx265",
                "-x265-params", "log-level=error:bframes=0:open-gop=0",
            ]
        else:
            command += ["-c:v", "libx264"]
            # Same reorder depth as the source, or decoders drop frames at seams.
            if self.spliceable and self.reorder == 0:
                command += ["-bf", "0"]
            elif self.spliceable and self.reorder == 1:
                command += ["-x264-params", "b-pyramid=none"]

        return command + colours + [
            "-preset", self.preset, "-crf", str(self.crf),
            "-pix_fmt", pix_fmt, "-f", "matroska", output,
        ]

    def _encode(self, ranges: List[Tuple[int, int]], output: str) -> _Encoded:
        """Re-encode exactly the frames whose timestamp lies in ranges."""
        low, high = ranges[0][0], ranges[-1][1]
        before = bisect.bisect_right(self.key_times, low) - 1
        seek = self.key_times[before] if before > 0 else None

        encoder, pix_fmt, size, shown = None, None, None, []
        with tempfile.TemporaryFile() as log:
            try:
                while True:
                    retry = False
                    with av.open(self.path) as container:
                        stream = container.streams.video[0]
                        stream.thread_type = "AUTO"
                        if seek is not None:
                            container.seek(int(Fraction(seek, 1000) / stream.time_base), stream=stream)
                        first = True
                        for frame in container.decode(stream):
                            if self.cancelled():
                                raise Cancelled()
                            if frame.pts is None:
                                continue
                            t = _ms(frame.pts, stream.time_base)
                            if first and seek is not None and t > low:
                                retry = True  # the seek landed too late
                                break
                            first = False
                            if t >= high:
                                break
                            if not any(a <= t < b for a, b in ranges) or (shown and t <= shown[-1]):
                                continue

                            if encoder is None:
                                name = frame.format.name
                                if name in PLANAR_FORMATS:
                                    pix_fmt = name
                                elif "10" in name:
                                    pix_fmt = "yuv420p10le"
                                else:
                                    pix_fmt = "yuv420p"
                                size = (frame.width, frame.height)
                                encoder = subprocess.Popen(
                                    self._encoder_command(pix_fmt, *size, output),
                                    stdin=subprocess.PIPE, stderr=log,
                                )
                            if frame.format.name != pix_fmt or (frame.width, frame.height) != size:
                                frame = frame.reformat(*size, format=pix_fmt)
                            sample_bytes = 2 if pix_fmt.endswith("le") else 1
                            for plane in frame.planes:
                                row, stride = plane.width * sample_bytes, plane.line_size
                                data = memoryview(plane)
                                if row == stride:
                                    encoder.stdin.write(data[:row * plane.height])
                                else:
                                    for y in range(plane.height):
                                        encoder.stdin.write(data[y * stride:y * stride + row])
                            shown.append(t)
                            if not self.spliceable:  # the only slow encode
                                self.progress(0.9 * t / max(1, self.key_end))
                    if not retry:
                        break
                    seek = None

                if encoder is None:
                    raise RuntimeError("No video left to keep")
                encoder.stdin.close()
                if encoder.wait():
                    raise BrokenPipeError
            except BrokenPipeError:
                log.seek(0)
                raise RuntimeError(
                    "FFmpeg boundary encode failed:\n" + log.read().decode(errors="replace")[-5000:]
                ) from None
            finally:
                if encoder and encoder.poll() is None:
                    encoder.kill()
                    encoder.wait()

        with av.open(output) as container:
            encoded = [p.pts for p in _packets(container, container.streams.video[0])]
        if len(encoded) != len(shown):
            raise RuntimeError("The encoder returned a different number of frames")
        rank = {value: i for i, value in enumerate(sorted(encoded))}
        return _Encoded(output, [shown[rank[value]] for value in encoded])

    def packets(self, pieces, timeline: Timeline, target):
        times = [timeline.map(p) for piece in pieces for p in piece.pts]
        ordered, depth = sorted(times), _reorder_depth(times)
        step = max(1, round(1000 / (self.fps or 25)))
        index = 0

        def stamp(packet):
            nonlocal index
            packet.time_base = MILLISECONDS
            packet.pts = times[index]
            packet.dts = (
                ordered[index - depth] if index >= depth
                else ordered[0] - (depth - index) * step
            )
            packet.stream = target
            index += 1
            return packet.dts, packet

        def replace(packet, payload):
            new = av.Packet(payload)
            new.is_keyframe = packet.is_keyframe
            new.time_base = MILLISECONDS
            new.duration = _ms(packet.duration or 0, packet.time_base)
            return new

        with av.open(self.path) as container:
            source = enumerate(_packets(container, container.streams.video[0]))
            for number, piece in enumerate(pieces):
                if isinstance(piece, _Copy):
                    for i, packet in source:
                        if i >= piece.first and self.pts[i] >= piece.min_pts:
                            if packet.is_keyframe:
                                payload = bytes(packet)
                                if self.codec == "hevc" and i == piece.first and number > 0:
                                    payload = _as_broken_link(payload)
                                packet = replace(
                                    packet, _with_parameter_sets(payload, self.codec, self.sets))
                            else:
                                packet.duration = _ms(packet.duration or 0, packet.time_base)
                            yield stamp(packet)
                        if i + 1 >= piece.stop:
                            break
                    continue

                with av.open(piece.path) as encoded:
                    stream = encoded.streams.video[0]
                    extradata = stream.codec_context.extradata
                    sets = _parameter_sets(self.codec, extradata) if self.spliceable else None
                    for packet in _packets(encoded, stream):
                        payload = bytes(packet)
                        if sets and packet.is_keyframe:
                            payload = _with_parameter_sets(payload, self.codec, sets)
                        yield stamp(replace(packet, payload))


# ---------- Audio ----------

def _plan_audio(
    path: str, index: int, timeline: Timeline, drop_priming: bool = False,
) -> List[Tuple[int, int, int]]:
    """
    (first packet, stop packet, time shift) for each kept interval.

    drop_priming is for AAC from MP4: its encoder delay lives in an edit list
    that Matroska has no place for, and would push the other tracks late.
    """
    starts, keys = [], []
    with av.open(path) as container:
        stream = container.streams[index]
        for packet in _packets(container, stream):
            starts.append(_ms(packet.pts, stream.time_base) if packet.pts is not None
                          else (starts[-1] if starts else 0))
            keys.append(packet.is_keyframe)
    if not starts:
        return []

    # Robust to millisecond rounding and to the odd gap.
    sample = [b - a for a, b in zip(starts[:2000], starts[1:2001])]
    length = statistics.mean(sample) if sample else 20
    if sample and length > 2 * statistics.median(sample) + 1:
        length = statistics.median(sample)

    lead = bisect.bisect_left(starts, -length / 2) if drop_priming else 0
    if lead >= len(starts):
        return []
    plan, cursor, used = [], None, lead
    for start, end in timeline.keeps():
        if start == -FOREVER:
            first, shift = lead, 0
        else:
            junction = timeline.map(start)
            if cursor is None:
                cursor = min(starts[lead], 0) + junction  # keep the encoder priming offset
            # Pick the packet that continues the output closest to the ideal map.
            ideal = start + (cursor - junction if abs(cursor - junction) <= 2 * length else 0)
            at = max(used, bisect.bisect_left(starts, ideal - length / 2))
            while at < len(starts) and not keys[at]:
                at += 1
            if at >= len(starts):
                break
            first = at
            if abs(starts[at] - ideal) <= length and abs(cursor - junction) <= 2 * length:
                shift = starts[at] - cursor
            else:
                shift = start - junction  # a real gap in the audio: preserve it

        stop = bisect.bisect_left(starts, end - length / 2, first)
        if stop > first:
            plan.append((first, stop, shift))
            following = starts[stop] if stop < len(starts) else starts[-1] + round(length)
            cursor, used = following - shift, stop
    return plan


def _audio_packets(path: str, index: int, plan, target):
    ranges, previous = iter(plan), None
    current = next(ranges, None)
    with av.open(path) as container:
        stream = container.streams[index]
        time_base = stream.time_base
        for i, packet in enumerate(_packets(container, stream)):
            while current and i >= current[1]:
                current = next(ranges, None)
            if not current:
                break
            if i < current[0] or packet.pts is None:
                continue
            when = _ms(packet.pts, time_base) - current[2]
            if previous is not None:
                when = max(when, previous)
            previous = when
            packet.duration = _ms(packet.duration or 0, time_base)
            packet.time_base = MILLISECONDS
            packet.pts = packet.dts = when
            packet.stream = target
            yield when, packet


# ---------- Subtitles and chapters ----------

def _subtitle_packets(path: str, index: int, timeline: Timeline, target):
    # Subtitle streams are tiny, and some need look-ahead: load them whole.
    with av.open(path) as container:
        stream = container.streams[index]
        codec, time_base = stream.codec_context.name, stream.time_base
        source = [
            (_ms(p.pts, time_base), _ms(p.duration or 0, time_base), p)
            for p in _packets(container, stream) if p.pts is not None
        ]

    result = []
    if codec in ("pgssub", "hdmv_pgs_subtitle"):
        for when, payload in retime_pgs([(t, bytes(p)) for t, _, p in source], timeline):
            result.append((when, 0, av.Packet(payload)))
    elif codec in ("dvbsub", "dvb_subtitle"):
        last_in_cut = {}
        for i, (t, _, _) in enumerate(source):
            for a, b in timeline.cuts:
                if a <= t < b:
                    last_in_cut[a] = i
        for i, (t, _, packet) in enumerate(source):
            if not timeline.removed(t) or i in last_in_cut.values():
                result.append((timeline.map(t), 0, packet))
    else:
        for start, duration, packet in source:
            bitmap = codec in ("dvdsub", "dvd_subtitle")
            if bitmap:
                duration = spu_duration(bytes(packet)) or duration
            span = timeline.span(start, start + duration)
            if not span:
                continue
            length = span[1] - span[0]
            if bitmap and length != duration:
                trim = next((b - start for a, b in timeline.cuts if a <= start < b), 0)
                packet = av.Packet(spu_retime(bytes(packet), trim, length))
            result.append((span[0], length, packet))
        result.sort(key=lambda item: item[0])

    for when, length, packet in result:
        packet.time_base = MILLISECONDS
        packet.pts = packet.dts = when
        packet.duration = length
        packet.is_keyframe = True
        packet.stream = target
        yield when, packet


def _chapters(probe: dict, origin: int, timeline: Timeline) -> List[dict]:
    chapters = []
    for number, chapter in enumerate(probe.get("chapters", [])):
        start = timeline.map(max(0, round(float(chapter["start_time"]) * 1000) - origin))
        end = timeline.map(max(0, round(float(chapter["end_time"]) * 1000) - origin))
        if end - start >= 1000:
            chapters.append({
                "id": number + 1, "start": start, "end": end,
                "time_base": MILLISECONDS, "metadata": chapter.get("tags", {}),
            })
    return chapters


# ---------- Public entry point ----------

def remove_segments(
    video_path: str,
    segments: List[Tuple[float, float]],
    output_path: str,
    *,
    crf: int = 20,
    preset: str = "veryfast",
    log: Callable[[str], None] = print,
    progress: Callable[[float], None] = lambda fraction: None,
    cancelled: Callable[[], bool] = lambda: False,
) -> CutResult:
    """
    Remove [start, end) intervals, given in seconds of playback time.

    Returns the Timeline that was applied, for retiming external files.
    Raises Cancelled, leaving no partial output, when cancelled() turns true.
    """
    video_path, output_path = os.path.abspath(video_path), os.path.abspath(output_path)
    if video_path == output_path:
        raise ValueError("Output would overwrite the source video")

    probe = _probe(video_path)
    origin = max(0, round(float(probe["format"].get("start_time", 0)) * 1000))
    duration = float(probe["format"].get("duration", FOREVER))
    timeline = Timeline((start, min(end, duration)) for start, end in segments)
    if not timeline.cuts:
        raise ValueError("No valid removal segments")

    info = next(
        (s for s in probe["streams"]
         if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")),
        None,
    )
    if info is None:
        raise RuntimeError("No video stream found")

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    work = tempfile.mkdtemp(prefix=".cut-video-", dir=os.path.dirname(output_path))
    warnings: List[str] = []
    try:
        source = _as_matroska(video_path, probe, origin, work, warnings)
        progress(0.05)

        video = _Video(source, info, crf, preset, log, progress, cancelled)
        pieces = video.plan(timeline, work)
        if not pieces:
            raise RuntimeError("No video left to keep")

        temporary = os.path.join(work, "output.mkv")
        with av.open(source) as container, av.open(temporary, "w", format="matroska") as output:
            output.metadata.update(
                {k: v for k, v in container.metadata.items() if k.lower() != "encoder"})
            main_video, tracks = container.streams.video[0], []

            for stream in container.streams:
                if stream.type == "attachment":
                    output.add_attachment(
                        stream.name or f"attachment{stream.index}",
                        stream.mimetype or "application/octet-stream",
                        stream.data,
                    )
                    continue
                if stream.type not in ("video", "audio", "subtitle"):
                    continue
                if stream.type == "video" and stream.index != main_video.index:
                    continue  # cover art

                if stream.type == "video" and not video.spliceable:
                    with av.open(pieces[0].path) as encoded:
                        target = output.add_stream_from_template(encoded.streams.video[0], opaque=True)
                else:
                    target = output.add_stream_from_template(stream, opaque=True)
                # Per-track statistics (DURATION, BPS...) are wrong after a cut.
                target.metadata.update({
                    k: v for k, v in stream.metadata.items()
                    if k.lower() in ("language", "title")
                })
                target.disposition = stream.disposition

                if stream.type == "video":
                    tracks.append(video.packets(pieces, timeline, target))
                elif stream.type == "audio":
                    drop_priming = source != video_path and stream.codec_context.name == "aac"
                    plan = _plan_audio(source, stream.index, timeline, drop_priming)
                    tracks.append(_audio_packets(source, stream.index, plan, target))
                else:
                    tracks.append(_subtitle_packets(source, stream.index, timeline, target))

            chapters = _chapters(probe, origin, timeline)
            if chapters:
                output.set_chapters(chapters)

            length = max(1, timeline.map(round(min(duration, FOREVER) * 1000)))
            before = 0.2 if video.spliceable else 0.9
            progress(before)
            for count, (when, packet) in enumerate(heapq.merge(*tracks, key=lambda item: item[0])):
                if cancelled():
                    raise Cancelled()
                output.mux(packet)
                if count % 500 == 0:
                    progress(before + (1 - before) * min(1.0, max(0, when) / length))

        os.replace(temporary, output_path)
        return CutResult(timeline, float(video.fps) if video.fps else None, warnings)
    finally:
        shutil.rmtree(work, ignore_errors=True)
