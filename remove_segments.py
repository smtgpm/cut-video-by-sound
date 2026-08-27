import bisect
import os
import subprocess
import tempfile
from typing import List, Tuple


def _merge_intervals(intervals, gap=0.5):
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


def _get_duration(video_path):
    cmd = ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
           '-of', 'default=noprint_wrappers=1:nokey=1', video_path]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip():
        raise RuntimeError(f'ffprobe could not read duration of {video_path}:\n{r.stderr[-1000:]}')
    return float(r.stdout.strip())


def _keyframe_times(video_path):
    cmd = ['ffprobe', '-v', 'error', '-skip_frame', 'nokey', '-select_streams', 'v:0',
           '-show_entries', 'frame=pts_time', '-of', 'csv=p=0', video_path]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f'ffprobe could not read keyframes:\n{r.stderr[-1000:]}')
    times = []
    for line in r.stdout.splitlines():
        try:
            times.append(float(line.strip()))
        except ValueError:
            pass
    return sorted(set(times))


def _keep_intervals(video_path, segments_to_remove):
    duration = _get_duration(video_path)
    removals = _merge_intervals(segments_to_remove)
    keep, cursor = [], 0.0
    for s, e in removals:
        s = max(0.0, min(s, duration))
        e = max(0.0, min(e, duration))
        if e <= s:
            continue
        if s > cursor:
            keep.append((cursor, s))
        cursor = max(cursor, e)
    if cursor < duration:
        keep.append((cursor, duration))
    return keep, duration


def _align_to_keyframes(keep, keyframes, duration):
    if not keyframes:
        raise RuntimeError('No keyframes found: cannot cut without re-encoding.')
    result = []
    for s, e in keep:
        # Start: first keyframe >= s. For 0, keep the beginning of the file.
        if s <= 0.001:
            a = 0.0
        else:
            i = bisect.bisect_left(keyframes, s)
            a = keyframes[i] if i < len(keyframes) else None

        # End: last keyframe <= e. For the end, keep the end of the file.
        if e >= duration - 0.001:
            b = duration
        else:
            j = bisect.bisect_right(keyframes, e) - 1
            b = keyframes[j] if j >= 0 else None

        if a is not None and b is not None and b > a + 0.001:
            result.append((a, b))
    return result


def _stream_copy(video_path, start, end, out_path):
    if end <= start:
        return
    cmd = ['ffmpeg', '-y', '-ss', f'{start:.6f}', '-i', video_path,
           '-t', f'{end-start:.6f}', '-map', '0:v?', '-map', '0:a?',
           '-c', 'copy', '-avoid_negative_ts', 'make_zero', out_path]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f'ffmpeg (stream copy) failed on [{start:.3f},{end:.3f}]:\n{r.stderr[-1500:]}')


def _concat(parts, output_path):
    with tempfile.TemporaryDirectory() as tmp:
        list_path = os.path.join(tmp, 'concat.txt')
        with open(list_path, 'w', encoding='utf-8') as f:
            for p in parts:
                p = p.replace('\\', '\\\\').replace("'", "'\\''")
                f.write(f"file '{p}'\n")
        cmd = ['ffmpeg', '-y', '-f', 'concat', '-safe', '0', '-i', list_path,
               '-c', 'copy', '-avoid_negative_ts', 'make_zero', output_path]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f'ffmpeg failed during concatenation:\n{r.stderr[-1500:]}')


def remove_segments(video_path: str, segments_to_remove: List[Tuple[float, float]], output_path: str,
                    hw_prefer='auto', vaapi_device='/dev/dri/renderD128', smart_cut=False,
                    min_gop_for_copy=1.0) -> None:
    # Parameters kept for compatibility with the old CLI.
    del hw_prefer, vaapi_device, smart_cut, min_gop_for_copy
    keep, duration = _keep_intervals(video_path, segments_to_remove)
    if not keep:
        raise ValueError('Nothing to keep: segments to remove cover the entire video.')
    aligned = _align_to_keyframes(keep, _keyframe_times(video_path), duration)
    if not aligned:
        raise ValueError('Could not build valid pieces from keyframes.')

    with tempfile.TemporaryDirectory() as tmp:
        parts = []
        for i, (s, e) in enumerate(aligned):
            p = os.path.join(tmp, f'part_{i:04d}.mp4')
            print(f'  Stream-copying: {s:.3f}s -> {e:.3f}s')
            _stream_copy(video_path, s, e, p)
            parts.append(p)
        _concat(parts, output_path)
