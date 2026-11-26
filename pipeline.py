"""The steps shared by the command line and the app: samples, scan, export."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import Callable, Dict, Iterable, List, Sequence, Tuple

from audio_match import find_matches, load_target, playback_origin
from remove_segments import Cancelled, remove_segments
from subtitles import retime_sidecars, write_sidecars
from timeline import Timeline

VIDEO_EXTENSIONS = {'.mp4', '.mov', '.avi', '.mkv',
                    '.webm', '.m4v', '.flv', '.wmv', '.mpeg', '.mpg'}

# The spectral detector alone also fires on audio that merely sounds similar;
# real copies of a sample correlate well above this on the waveform.
DOUBTFUL_WAVEFORM = 0.5

# A match lying this much (of its own length) inside a longer sample's match is
# a copy of the same passage, e.g. an intro tune that is repeated in the outro.
CONTAINED = 0.5
MIN_MATCH = 0.25  # seconds left after trimming an edge overlap
# Cuts may touch by this much (seconds) before they count as overlapping; the
# shorter one is then trimmed so that they only meet.
EDGE_OVERLAP = 1.0
MAX_CANDIDATES = 8  # best matches per sample considered when choosing the cuts
# Scores at or above this all count as a perfect copy when choosing cuts, so
# between overlapping samples that both match this well, the longest one wins.
PERFECT_MATCH = 0.98
# Close candidates of one sample are ranked a little higher the earlier they
# start in this many seconds (audio_match.find_matches). Not a setting.
INTRO_MAX = 180.0


@dataclass
class Cut:
    start: float
    end: float
    sample: str = ""  # empty for a cut added by hand
    score: float = 0.0
    waveform: float = 0.0
    enabled: bool = True
    edited: bool = False

    @property
    def doubtful(self) -> bool:
        return bool(self.sample) and self.waveform < DOUBTFUL_WAVEFORM


@dataclass
class ExportResult:
    output: str
    timeline: Timeline
    subtitles: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


def default_workers() -> int:
    """
    Videos searched at once, at most: a few cores each. Memory may allow fewer
    at a time (see MemoryBudget), which then start as others finish.
    """
    return max(1, min(8, (os.cpu_count() or 1) // 4))


def available_memory() -> float | None:
    """Bytes of memory free for new work, None if unknown."""
    try:
        with open('/proc/meminfo') as handle:
            for line in handle:
                if line.startswith('MemAvailable:'):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    try:
        # No /proc/meminfo (macOS): half of the memory, to be safe.
        return os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES') / 2
    except (AttributeError, ValueError, OSError):
        return None  # Windows: no limit


def search_memory(duration: float) -> float:
    """
    Bytes needed at most to search a video of this many seconds: its audio,
    fingerprint and scores, about 1 GB for an hour (measured), plus margin.
    """
    return 0.3e9 + max(duration, 60.0) * 0.3e6


class MemoryBudget:
    """
    Lets videos be searched side by side only while their memory fits in what
    was free when the first one started. One always runs, however long it is.
    Without this, eight hour-long episodes at once fill the memory and the
    system slows to a crawl swapping to disk.
    """
    SHARE = 0.8  # of the free memory, the rest is left to the system and other programs

    def __init__(self):
        self._lock = threading.Condition()
        self._reserved = 0.0
        self._limit = None

    @contextmanager
    def reserve(self, amount: float, cancelled: Callable[[], bool] = lambda: False):
        with self._lock:
            while self._reserved:
                if self._limit is None or self._reserved + amount <= self._limit:
                    break
                if cancelled():
                    raise Cancelled()
                self._lock.wait(0.25)
            else:
                free = available_memory()
                self._limit = free * self.SHARE if free else None
            self._reserved += amount
        try:
            yield
        finally:
            with self._lock:
                self._reserved -= amount
                if self._reserved < 1:
                    self._reserved = 0.0
                self._lock.notify_all()


MEMORY = MemoryBudget()


def is_video(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in VIDEO_EXTENSIONS


def expand_video_inputs(inputs: Iterable[str]) -> List[Tuple[str, str]]:
    """
    (video, output subfolder) pairs. A folder is searched with all its
    subfolders, and its videos go to <folder>_cleaned, keeping the same
    subfolders. A file given directly goes to the output root. Hidden files
    and folders are skipped.
    """
    found, seen = [], set()
    for given in inputs:
        path = os.path.abspath(given)
        if os.path.isdir(path):
            videos = []
            root_name = f'{os.path.basename(path)}_cleaned'
            for folder, folders, names in os.walk(path):
                folders[:] = sorted(name for name in folders if not name.startswith('.'))
                relative = os.path.relpath(folder, path)
                subfolder = root_name if relative == '.' else os.path.join(root_name, relative)
                videos += [(os.path.join(folder, name), subfolder) for name in sorted(names)
                           if not name.startswith('.') and is_video(name)
                           and os.path.isfile(os.path.join(folder, name))]
        else:
            videos = [(path, '')]
        for video, subfolder in videos:
            if video not in seen:
                seen.add(video)
                found.append((video, subfolder))
    return found


def plan_outputs(
    items: Iterable[Tuple[str, str, bool]], out_root: str,
) -> Dict[str, str]:
    """
    Output path for each (video, subfolder, has cuts).

    Cut videos are Matroska; untouched copies keep their own extension. Two
    inputs never share an output, and an output is never one of the inputs.
    """
    items = list(items)
    taken = {os.path.abspath(video) for video, _, _ in items}
    taken_bases = {os.path.splitext(path)[0].lower() for path in taken}

    planned = {}
    for video, subfolder, has_cuts in items:
        stem, ext = os.path.splitext(os.path.basename(video))
        folder = os.path.abspath(os.path.join(out_root, subfolder))
        base, number = os.path.join(folder, f'{stem}_cleaned'), 1
        # Compared without extension, because subtitle files share the name.
        while base.lower() in taken_bases:
            number += 1
            base = os.path.join(folder, f'{stem} ({number})_cleaned')
        taken_bases.add(base.lower())
        planned[video] = base + ('.mkv' if has_cuts else ext)
    return planned


def media_info(path: str) -> Tuple[float, float]:
    """(duration in seconds, frames per second)."""
    result = subprocess.run([
        'ffprobe', '-v', 'error',
        '-show_entries', 'format=duration:stream=codec_type,avg_frame_rate',
        '-of', 'json', path,
    ], capture_output=True, text=True, errors='replace')
    try:
        info = json.loads(result.stdout)
        duration = float(info['format']['duration'])
    except (ValueError, KeyError):
        raise RuntimeError(f'Cannot read {os.path.basename(path)}. {result.stderr.strip()[-500:]}') from None

    fps = 25.0
    for stream in info.get('streams', []):
        if stream.get('codec_type') == 'video':
            top, _, bottom = stream.get('avg_frame_rate', '0/0').partition('/')
            if float(top) and float(bottom or 0):
                fps = float(top) / float(bottom)
            break
    return duration, fps


def list_samples(folder: str) -> List[str]:
    if not os.path.isdir(folder):
        return []
    return [os.path.join(folder, name) for name in sorted(os.listdir(folder))
            if name.lower().endswith('.wav')]


# ---------- Per-sample options, kept next to the samples ----------
# A sample set may say, per sample (by file name):
# - where in a video its match must start (search-windows.json):
#     ('first', a, b)  between a and b seconds after the start
#     ('last', x)      in the last x seconds
#   Samples not listed are looked for anywhere.
# - what to cut beyond the match (cut-extensions.json):
#     ('before', x)    also everything before it, up to x seconds
#     ('after', x)     also everything after it, up to x seconds
WINDOWS_FILE = 'search-windows.json'
EXTENSIONS_FILE = 'cut-extensions.json'


def _read_options(folder: str, file_name: str, check) -> Dict[str, tuple]:
    try:
        with open(os.path.join(folder, file_name), encoding='utf-8') as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    options = {}
    for name, value in (data.items() if isinstance(data, dict) else ()):
        try:
            options[name] = check(value)
        except (TypeError, ValueError):
            continue
    return options


def _write_option(folder: str, file_name: str, check, name: str, value) -> None:
    options = _read_options(folder, file_name, check)
    if value is None:
        options.pop(name, None)
    else:
        options[name] = check(value)
    path = os.path.join(folder, file_name)
    if not options:
        if os.path.exists(path):
            os.unlink(path)
        return
    handle, temporary = tempfile.mkstemp(prefix='.options-', dir=folder)
    try:
        with os.fdopen(handle, 'w', encoding='utf-8') as target:
            json.dump({k: list(v) for k, v in sorted(options.items())}, target, indent=1)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_windows(folder: str) -> Dict[str, tuple]:
    """{sample file name: window} for a sample set."""
    return _read_options(folder, WINDOWS_FILE, check_window)


def write_window(folder: str, name: str, window: tuple | None) -> None:
    """Set (or with None, remove) the window of one sample of a set."""
    _write_option(folder, WINDOWS_FILE, check_window, name, window)


def window_of(sample: str) -> tuple | None:
    return read_windows(os.path.dirname(sample)).get(os.path.basename(sample))


def read_extensions(folder: str) -> Dict[str, tuple]:
    """{sample file name: extension} for a sample set."""
    return _read_options(folder, EXTENSIONS_FILE, check_extension)


def write_extension(folder: str, name: str, extension: tuple | None) -> None:
    """Set (or with None, remove) what is cut beyond one sample's match."""
    _write_option(folder, EXTENSIONS_FILE, check_extension, name, extension)


def extension_of(sample: str) -> tuple | None:
    return read_extensions(os.path.dirname(sample)).get(os.path.basename(sample))


def check_extension(extension) -> tuple:
    kind, *values = extension
    if kind in ('before', 'after') and len(values) == 1 and float(values[0]) > 0:
        return (kind, float(values[0]))
    raise ValueError(f'Not a cut extension: {extension!r}')


def describe_extension(extension: tuple | None) -> str:
    """'–', 'before ≤60 s' or 'after ≤120 s'."""
    if not extension:
        return '–'
    return f'{extension[0]} ≤{extension[1]:g} s'


def extend_cuts(cuts: list, extension: Callable[[object], tuple | None], duration: float,
                barriers: Sequence = ()) -> None:
    """
    Stretch each cut (anything with start and end) by its sample's extension:
    everything before it, or after it, up to the extension's seconds. A cut
    stops at the ends of the video, and at any barrier (the other cuts kept),
    so it never swallows another cut.
    """
    for cut in cuts:
        wanted = extension(cut)
        if not wanted:
            continue
        others = [other for other in barriers if other is not cut]
        if wanted[0] == 'before':
            limit = max([0.0] + [o.end for o in others if o.end <= cut.start + 1e-6])
            cut.start = max(cut.start - wanted[1], limit)
        else:
            limit = min([duration] + [o.start for o in others if o.start >= cut.end - 1e-6])
            cut.end = min(cut.end + wanted[1], limit)


def check_window(window) -> tuple:
    kind, *values = window
    values = [float(v) for v in values]
    if kind == 'first' and len(values) == 2 and 0 <= values[0] < values[1]:
        return ('first', *values)
    if kind == 'last' and len(values) == 1 and values[0] > 0:
        return ('last', *values)
    raise ValueError(f'Not a search window: {window!r}')


def describe_window(window: tuple | None) -> str:
    """Where a match may start: 'anywhere', '0–5 s' or 'last 60 s'."""
    if not window:
        return 'anywhere'
    if window[0] == 'first':
        return f'{window[1]:g}–{window[2]:g} s'
    return f'last {window[1]:g} s'


def window_starts(window: tuple | None, duration: float) -> Tuple[float, float] | None:
    """The range a match may start in, in seconds, for a video this long."""
    if not window:
        return None
    if window[0] == 'first':
        return window[1], min(window[2], duration)
    return max(0.0, duration - window[1]), duration


def safe_name(text: str) -> str:
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]+', '_', text).strip(' .')


def save_sample(video: str, start: float, end: float, folder: str, name: str) -> str:
    """Extract [start, end) as a mono WAV, without replacing an existing one."""
    if end - start < 0.25:
        raise ValueError('A sample must be at least 0.25 seconds long.')
    name = safe_name(name) or 'sample'
    os.makedirs(folder, exist_ok=True)
    path, number = os.path.join(folder, f'{name}.wav'), 1
    while os.path.exists(path):
        number += 1
        path = os.path.join(folder, f'{name}_{number}.wav')

    # On the matcher's clock. A plain -ss counts from the container's first
    # packet, so encoder priming would shift every sample, and with it every cut.
    rate, seek = 22050, int(max(0.0, start - 5.0))
    on_playback_clock = (
        f'asetpts=PTS-{playback_origin(video)}/TB,'
        f'aresample={rate}:async=1:first_pts={seek * rate},'
        f'atrim=start={start:.6f}:end={end:.6f},asetpts=PTS-STARTPTS')
    result = subprocess.run([
        'ffmpeg', '-nostdin', '-n', '-v', 'error',
        '-copyts', '-ss', str(seek), '-i', video,
        '-vn', '-af', on_playback_clock,
        '-ac', '1', '-ar', str(rate), '-c:a', 'pcm_s16le', path,
    ], capture_output=True, text=True, errors='replace')
    # A bare WAV header (44 bytes, or 78 with metadata) means there was no audio.
    if result.returncode or not os.path.exists(path) or os.path.getsize(path) < 1000:
        if os.path.exists(path):
            os.unlink(path)
        raise RuntimeError(f'No audio could be extracted there. {result.stderr.strip()[-500:]}')
    return path


def claim(found: list, taken: list) -> list:
    """
    Matches (anything with start and end) that do not collide with the ones
    already taken. A match mostly inside a taken one is dropped. One that only
    touches a taken match at an edge is trimmed to end where the other starts
    (or start where it ends), so cuts never overlap and none is lost.
    """
    kept = []
    for match in found:
        for other in taken:
            overlap = min(match.end, other.end) - max(match.start, other.start)
            if overlap <= 0:
                continue
            if overlap >= CONTAINED * (match.end - match.start):
                match = None
                break
            if match.start < other.start:
                match.end = other.start
            else:
                match.start = other.end
            if match.end - match.start < MIN_MATCH:
                match = None
                break
        if match is not None:
            kept.append(match)
    return kept


def _overlap(a, b) -> float:
    return min(a.end, b.end) - max(a.start, b.start)


def choose_cuts(matches: list, sample: Callable[[object], str], score: Callable[[object], float],
                one_per_sample: bool = True) -> list:
    """
    The matches to cut: the set with the highest total score in which no two
    overlap (by more than EDGE_OVERLAP, trimmed afterwards).

    Each sample first gets at most one match, and the samples are assigned
    together. That is what keeps an intro sample off the copy of
    its tune inside the outro, even when that copy scores higher than the real
    intro: the outro sample then has nowhere good left to go, so the right
    pairing (intro on the intro, outro on the outro) has the higher total.
    Ties go to the set covering more time. Scores of PERFECT_MATCH or more
    count as equal, so when two samples overlap and both match that well, the
    longer one is cut. Unless one_per_sample, further matches are then added,
    best first, wherever they overlap nothing.
    """
    raw_score = score

    def score(match):
        return min(raw_score(match), PERFECT_MATCH)

    def better(a, b):
        return (round(a[0], 9), a[1]) > (round(b[0], 9), b[1])

    def value(chosen):
        return sum(score(m) for m in chosen), sum(m.end - m.start for m in chosen)

    def free(match, chosen):
        return all(_overlap(match, other) <= EDGE_OVERLAP for other in chosen)

    groups: Dict[str, list] = {}
    for match in matches:
        groups.setdefault(sample(match), []).append(match)
    groups = [sorted(group, key=score, reverse=True)[:MAX_CANDIDATES] for group in groups.values()]
    groups.sort(key=lambda group: score(group[0]), reverse=True)
    # The most each remaining sample could still add, to skip hopeless branches.
    ceiling = [sum(score(group[0]) for group in groups[i:]) for i in range(len(groups) + 1)]
    best = {'value': (-1.0, 0.0), 'chosen': []}

    def search(index, chosen, total):
        if total + ceiling[index] < best['value'][0] - 1e-9:
            return
        if index == len(groups):
            if better(value(chosen), best['value']):
                best['value'], best['chosen'] = value(chosen), list(chosen)
            return
        for match in groups[index]:
            if free(match, chosen):
                chosen.append(match)
                search(index + 1, chosen, total + score(match))
                chosen.pop()
        search(index + 1, chosen, total)  # this sample left out

    search(0, [], 0.0)
    chosen = best['chosen']
    if not one_per_sample:
        # Further occurrences are added only where they overlap nothing. A
        # plain best total would rather cut the intro tune twice than intro
        # and outro, leaving the rest of the outro in.
        for match in sorted(matches, key=score, reverse=True):
            if all(match is not other for other in chosen) and free(match, chosen):
                chosen.append(match)
    return chosen


def settle_cuts(matches: list, sample: Callable[[object], str], score: Callable[[object], float],
                one_per_sample: bool = True, eligible: Callable[[object], bool] = lambda m: True):
    """
    (chosen, others): the eligible matches that should be cut (see choose_cuts),
    trimmed where they touch, and the matches left over that do not overlap
    any chosen one. Leftovers that do are dropped: they are the same passage.
    """
    chosen = choose_cuts([m for m in matches if eligible(m)], sample, score, one_per_sample)
    picked = {id(m) for m in chosen}
    others = [m for m in matches
              if id(m) not in picked and all(_overlap(m, c) <= 0 for c in chosen)]
    trimmed = []
    for match in sorted(chosen, key=lambda m: m.start - m.end):  # longest first
        trimmed += claim([match], trimmed)
    return trimmed, others


def scan_video(
    video: str,
    samples: Sequence[str],
    *,
    threshold: float = 0.72,
    intro_max: float = INTRO_MAX,
    several_per_sample: bool = False,
    min_waveform: float = 0.0,
    windows: Dict[str, tuple] | None = None,
    extensions: Dict[str, tuple] | None = None,
    keep: Sequence[Cut] = (),
    progress: Callable[[float], None] = lambda fraction: None,
    cancelled: Callable[[], bool] = lambda: False,
) -> Tuple[List[Cut], List[Cut]]:
    """
    (cuts, candidates). cuts: every match, for review. The ticked ones are chosen together, over all
    samples: the plausible matches (not doubtful) with the highest total
    waveform score that do not overlap, one per sample unless
    several_per_sample is set (see choose_cuts). Unticked matches overlapping
    a ticked one are left out, as are matches whose waveform score is below
    min_waveform. Matches over a cut in `keep` (cuts already decided on) are
    dropped, or trimmed where they only touch it.

    windows: {sample path: window}, where each sample's match must start
    (see read_windows); other samples are looked for anywhere.
    extensions: {sample path: extension}, what to cut beyond a sample's match
    (see read_extensions). Applied once the cuts are chosen, and never over
    another chosen or kept cut.

    candidates: every place each sample was heard, untrimmed, so the review
    can move a cut to its sample's next best match.
    """
    if cancelled():
        raise Cancelled()
    try:
        expected = media_info(video)[0]
    except RuntimeError:
        expected = 3600.0  # ffmpeg will say what is wrong
    # Waits here while the videos already being searched leave too little memory.
    with MEMORY.reserve(search_memory(expected), cancelled):
        if cancelled():
            raise Cancelled()
        target = load_target(video)
        duration = len(target.samples) / target.sr
        progress(0.3)

        cuts: List[Cut] = []
        for number, sample in enumerate(samples):
            if cancelled():
                raise Cancelled()
            starts = window_starts((windows or {}).get(sample), duration)
            cuts += [
                Cut(m.start, m.end, sample, m.score, m.waveform_score)
                for m in find_matches(sample, video, threshold=threshold,
                                      intro_max_seconds=intro_max, target=target, starts=starts)
                if m.waveform_score >= min_waveform
            ]
            progress(0.3 + 0.7 * (number + 1) / len(samples))
        del target

    candidates = [replace(cut, enabled=False) for cut in cuts]
    cuts = claim(cuts, list(keep))
    chosen, others = settle_cuts(cuts, lambda cut: cut.sample, lambda cut: cut.waveform,
                                 one_per_sample=not several_per_sample,
                                 eligible=lambda cut: not cut.doubtful)
    for cut in chosen:
        cut.enabled = True
    for cut in others:
        cut.enabled = False
    if extensions:
        # The decoded audio can run a little past the video: stop at the shorter.
        try:
            duration = min(duration, media_info(video)[0] or duration)
        except RuntimeError:
            pass
        extend_cuts(chosen + others, lambda cut: extensions.get(cut.sample), duration,
                    barriers=chosen + list(keep))
    return sorted(chosen + others, key=lambda cut: cut.start), candidates


def export_video(
    video: str,
    segments: List[Tuple[float, float]],
    output: str,
    *,
    crf: int = 20,
    preset: str = 'veryfast',
    log: Callable[[str], None] = print,
    progress: Callable[[float], None] = lambda fraction: None,
    cancelled: Callable[[], bool] = lambda: False,
) -> ExportResult:
    """Write the cleaned video and its subtitle files."""
    os.makedirs(os.path.dirname(os.path.abspath(output)), exist_ok=True)
    if segments:
        cut = remove_segments(video, segments, output, crf=crf, preset=preset,
                              log=log, progress=progress, cancelled=cancelled)
        timeline, fps, warnings = cut.timeline, cut.fps, list(cut.warnings)
    else:
        _copy_file(video, output, progress, cancelled)
        timeline, fps, warnings = Timeline(), None, []

    written, problems = write_sidecars(video, output, timeline, VIDEO_EXTENSIONS, fps)
    progress(1.0)
    return ExportResult(output, timeline, written, warnings + problems)


def replaced_path(video: str) -> str:
    """Where replace_video puts the result: same name, as Matroska."""
    return os.path.splitext(os.path.abspath(video))[0] + '.mkv'


def replace_video(
    video: str,
    segments: List[Tuple[float, float]],
    *,
    crf: int = 20,
    preset: str = 'veryfast',
    log: Callable[[str], None] = print,
    progress: Callable[[float], None] = lambda fraction: None,
    cancelled: Callable[[], bool] = lambda: False,
) -> ExportResult:
    """
    Cut the video in place. The result is Matroska: Episode.mp4 becomes
    Episode.mkv and the .mp4 is deleted. Its subtitle files are retimed where
    they are. The source is only touched once the cut video is complete.
    """
    video = os.path.abspath(video)
    if not segments:
        raise ValueError('Nothing to cut')
    final = replaced_path(video)
    same = os.path.exists(final) and os.path.samefile(final, video)
    if os.path.exists(final) and not same:
        raise ValueError(f'{os.path.basename(final)} already exists next to it. '
                         'Rename one of them, or export to a folder instead.')

    folder = os.path.dirname(video)
    handle, temporary = tempfile.mkstemp(prefix='.cut-video-', suffix='.mkv', dir=folder)
    os.close(handle)
    try:
        cut = remove_segments(video, segments, temporary, crf=crf, preset=preset,
                              log=log, progress=progress, cancelled=cancelled)
        sidecars, problems = retime_sidecars(video, cut.timeline, VIDEO_EXTENSIONS, cut.fps)
        if cancelled():
            raise Cancelled()
        os.chmod(temporary, os.stat(video).st_mode & 0o777)
        os.replace(temporary, final)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    if not same:
        os.unlink(video)

    written = []
    for path, content in sidecars:
        try:
            _write_atomically(path, content)
            written.append(path)
        except OSError as exc:
            problems.append(f'subtitle NOT replaced, now out of sync: {os.path.basename(path)}: {exc}')
    progress(1.0)
    return ExportResult(final, cut.timeline, written, list(cut.warnings) + problems)


def _write_atomically(path: str, content: bytes) -> None:
    handle, temporary = tempfile.mkstemp(prefix='.cut-video-', dir=os.path.dirname(path))
    try:
        with os.fdopen(handle, 'wb') as target:
            target.write(content)
        if os.path.exists(path):
            os.chmod(temporary, os.stat(path).st_mode & 0o777)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _copy_file(source: str, output: str, progress, cancelled) -> None:
    if os.path.abspath(source) == os.path.abspath(output):
        raise ValueError('Output would overwrite the source video')
    size, done = max(1, os.path.getsize(source)), 0
    handle, temporary = tempfile.mkstemp(
        prefix='.cut-video-', dir=os.path.dirname(os.path.abspath(output)))
    try:
        with os.fdopen(handle, 'wb') as target, open(source, 'rb') as origin:
            while block := origin.read(8 * 1024 * 1024):
                if cancelled():
                    raise Cancelled()
                target.write(block)
                done += len(block)
                progress(done / size)
        os.chmod(temporary, os.stat(source).st_mode & 0o777)
        os.replace(temporary, output)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
