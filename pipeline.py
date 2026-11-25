"""The steps shared by the command line and the app: samples, scan, export."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
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
    """Videos processed at once: about 1.3 GB of RAM and a few cores each."""
    try:
        memory = os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES') / 1e9
    except (AttributeError, ValueError, OSError):
        memory = 8.0  # Windows has no sysconf: assume a modest machine
    return max(1, min(8, (os.cpu_count() or 1) // 4, int(memory // 3)))


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
    Ties go to the set covering more time. Unless one_per_sample, further
    matches are then added, best first, wherever they overlap nothing.
    """
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
    intro_max: float = 180.0,
    several_per_sample: bool = False,
    min_waveform: float = 0.0,
    progress: Callable[[float], None] = lambda fraction: None,
    cancelled: Callable[[], bool] = lambda: False,
) -> List[Cut]:
    """
    Every match, for review. The ticked ones are chosen together, over all
    samples: the plausible matches (not doubtful) with the highest total
    waveform score that do not overlap, one per sample unless
    several_per_sample is set (see choose_cuts). Unticked matches overlapping
    a ticked one are left out, as are matches whose waveform score is below
    min_waveform.
    """
    if cancelled():
        raise Cancelled()
    target = load_target(video)
    progress(0.3)

    cuts: List[Cut] = []
    for number, sample in enumerate(samples):
        if cancelled():
            raise Cancelled()
        cuts += [
            Cut(m.start, m.end, sample, m.score, m.waveform_score)
            for m in find_matches(sample, video, threshold=threshold,
                                  intro_max_seconds=intro_max, target=target)
            if m.waveform_score >= min_waveform
        ]
        progress(0.3 + 0.7 * (number + 1) / len(samples))

    chosen, others = settle_cuts(cuts, lambda cut: cut.sample, lambda cut: cut.waveform,
                                 one_per_sample=not several_per_sample,
                                 eligible=lambda cut: not cut.doubtful)
    for cut in chosen:
        cut.enabled = True
    for cut in others:
        cut.enabled = False
    return sorted(chosen + others, key=lambda cut: cut.start)


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
