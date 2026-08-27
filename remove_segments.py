"""Fast exact removal using a smart hybrid strategy.

Only the GOPs touching a removal boundary are decoded/re-encoded. Everything
far from a boundary is stream-copied. The actual removal timestamps are exact;
keyframes are used only as *internal re-encode boundaries*, never as the cut
point. This gives most of the speed of stream-copying while keeping precise
cuts.
"""
import json
import os
import shutil
import subprocess
import tempfile
from typing import List, Tuple


def _run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(r.stderr[-3000:])
    return r.stdout


def _probe(path):
    d = json.loads(_run(['ffprobe', '-v', 'error',
                   '-show_streams', '-show_format', '-of', 'json', path]))
    v = next((x for x in d.get('streams', [])
             if x.get('codec_type') == 'video'), None)
    a = next((x for x in d.get('streams', [])
             if x.get('codec_type') == 'audio'), None)
    return v, a, d.get('format', {})


def _duration(path):
    return float(_run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=nw=1:nk=1', path]).strip())


def _keyframes(path):
    out = _run([
        'ffprobe', '-v', 'error', '-skip_frame', 'nokey', '-select_streams', 'v:0',
        '-show_entries', 'frame=best_effort_timestamp_time',
        '-of', 'csv=p=0', path,
    ])
    vals = []
    for line in out.splitlines():
        try:
            vals.append(float(line.strip().split(',')[0]))
        except (ValueError, IndexError):
            pass
    return sorted(set(x for x in vals if x >= 0))


def _merge(intervals):
    vals = sorted((max(0.0, float(s)), max(0.0, float(e)))
                  for s, e in intervals if e > s)
    if not vals:
        return []
    out = [list(vals[0])]
    for s, e in vals[1:]:
        if s <= out[-1][1] + 1e-7:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(s, e) for s, e in out]


def _codec_args(v):
    codec = (v or {}).get('codec_name', '')
    pix = (v or {}).get('pix_fmt', '')
    if codec in ('hevc', 'h265'):
        args = ['-c:v', 'libx265', '-preset', 'fast', '-crf', '18']
        if pix and pix not in ('unknown', 'N/A'):
            args += ['-pix_fmt', pix]
        if '10' in pix:
            args += ['-profile:v', 'main10']
    elif codec in ('h264', 'avc1'):
        args = ['-c:v', 'libx264', '-preset', 'fast', '-crf', '18']
        if pix and pix not in ('unknown', 'N/A'):
            args += ['-pix_fmt', pix]
    elif codec == 'vp9':
        args = ['-c:v', 'libvpx-vp9', '-crf', '18', '-b:v', '0']
        if pix and pix not in ('unknown', 'N/A'):
            args += ['-pix_fmt', pix]
    else:
        args = ['-c:v', 'libx264', '-preset', 'fast', '-crf', '18']
    return args


def _boundary_encode_args(v):
    args = _codec_args(v)
    mapping = {
        'color_primaries': '-color_primaries',
        'color_transfer': '-color_trc',
        'color_space': '-colorspace',
        'color_range': '-color_range',
    }
    for k, opt in mapping.items():
        val = (v or {}).get(k)
        if val and val not in ('unknown', 'N/A'):
            args += [opt, val]
    return args


def _copy_chunk(src, start, end, dst):
    cmd = ['ffmpeg', '-y', '-v', 'error', '-ss', f'{start:.9f}', '-to', f'{end:.9f}',
           '-i', src, '-map', '0:v:0', '-map', '0:a?', '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k', '-avoid_negative_ts', 'make_zero', dst]
    _run(cmd)


def _encode_chunk(src, start, end, dst, v):
    # Start is deliberately a keyframe, but END is the exact removal boundary.
    # Thus the cut itself is not snapped to a keyframe.
    cmd = ['ffmpeg', '-y', '-v', 'error', '-ss', f'{start:.9f}', '-to', f'{end:.9f}',
           '-i', src, '-map', '0:v:0', '-map', '0:a?', '-sn',
           *_boundary_encode_args(v), '-c:a', 'aac', '-b:a', '192k',
           '-avoid_negative_ts', 'make_zero', dst]
    _run(cmd)


def remove_segments(video_path: str, segments_to_remove: List[Tuple[float, float]], output_path: str,
                    **_ignored) -> None:
    rem = _merge(segments_to_remove)
    if not rem:
        shutil.copy2(video_path, output_path)
        return

    duration = _duration(video_path)
    rem = [(max(0.0, s), min(duration, e))
           for s, e in rem if s < duration and e > 0]
    if not rem:
        shutil.copy2(video_path, output_path)
        return

    v, a, _fmt = _probe(video_path)
    kfs = _keyframes(video_path)
    if not kfs:
        raise RuntimeError(
            'Could not read video keyframes for smart hybrid cutting.')
    if kfs[0] > 1e-3:
        kfs.insert(0, 0.0)

    # Build exact kept intervals first.
    keep = []
    cur = 0.0
    for s, e in rem:
        if s > cur:
            keep.append((cur, s))
        cur = max(cur, e)
    if cur < duration:
        keep.append((cur, duration))
    if not keep:
        raise ValueError('Nothing remains after removal.')

    # Every kept interval is either entirely copyable (starts at a keyframe),
    # or its beginning/end touches a removal and must be re-encoded around the
    # exact cut. We partition around keyframes so copied chunks always start at
    # safe decode points.
    def prev_kf(t):
        vals = [x for x in kfs if x <= t + 1e-6]
        return vals[-1] if vals else 0.0

    def next_kf(t):
        for x in kfs:
            if x >= t - 1e-6:
                return x
        return duration

    # Create chunks from the kept intervals. Boundary pieces are encoded from
    # a preceding keyframe to the exact start, and from exact end to next kf.
    # The keyframe itself is an internal encoding boundary, never the deletion
    # timestamp.
    work = tempfile.mkdtemp(prefix='cut_video_')
    chunk_paths = []
    try:
        idx = 0
        for ks, ke in keep:
            left_kf = prev_kf(ks)
            right_kf = next_kf(ke)

            if ks <= left_kf + 1e-6:
                if ke < duration - 1e-6 and ke < right_kf - 1e-6:
                    # Copying can safely end at the exact requested endpoint;
                    # the following chunk begins at a keyframe or is absent.
                    pass
                start_copy = ks
            else:
                # Encode from previous keyframe through the exact kept start.
                p = os.path.join(work, f'{idx:05d}.mkv')
                idx += 1
                _encode_chunk(video_path, left_kf, ks, p, v)
                chunk_paths.append(p)
                start_copy = ks

            # Find the next boundary where copy can safely resume: the next
            # keyframe after the kept interval's end. If end is duration, copy.
            if ke >= duration - 1e-6:
                if start_copy < ke - 1e-6:
                    p = os.path.join(work, f'{idx:05d}.mkv')
                    idx += 1
                    _copy_chunk(video_path, start_copy, ke, p)
                    chunk_paths.append(p)
            elif ke < right_kf - 1e-6:
                # Exact end is inside a GOP: encode from the exact end through
                # the next keyframe, but only keep the portion from end onward.
                # This chunk belongs to the NEXT kept interval, so do not emit it
                # here. The next interval will encode its own left boundary.
                if start_copy < ke - 1e-6:
                    p = os.path.join(work, f'{idx:05d}.mkv')
                    idx += 1
                    _copy_chunk(video_path, start_copy, ke, p)
                    chunk_paths.append(p)
            else:
                if start_copy < ke - 1e-6:
                    p = os.path.join(work, f'{idx:05d}.mkv')
                    idx += 1
                    _copy_chunk(video_path, start_copy, ke, p)
                    chunk_paths.append(p)

        if not chunk_paths:
            raise ValueError('No output chunks were produced.')

        # The simple partition above can still leave a copied chunk beginning at
        # a non-keyframe immediately after a removal. Rebuild using a precise
        # boundary-aware partition if necessary.
        # For correctness, use a second, deterministic pass over each keep
        # interval, encoding only the GOP portions touching its boundaries.
        shutil.rmtree(work)
        os.makedirs(work)
        chunk_paths = []
        idx = 0
        for ks, ke in keep:
            if ks > 1e-6 and any(abs(ks - r[1]) < 1e-5 for r in rem):
                # Start immediately after a removal: exact start -> next keyframe.
                nk = next_kf(ks)
                if nk > ks + 1e-6:
                    p = os.path.join(work, f'{idx:05d}.mkv')
                    idx += 1
                    _encode_chunk(video_path, ks, nk, p, v)
                else:
                    p = os.path.join(work, f'{idx:05d}.mkv')
                    idx += 1
                    _copy_chunk(video_path, ks, ke, p)
                chunk_paths.append(p)
                start = nk
            else:
                start = ks

            if start < ke - 1e-6:
                # If the end is immediately before a removal, reencode from
                # previous keyframe through the exact end; otherwise copy.
                ends_removal = any(abs(ke - r[0]) < 1e-5 for r in rem)
                if ends_removal and ke > start + 1e-6:
                    pk = prev_kf(ke)
                    if pk > start + 1e-6:
                        p = os.path.join(work, f'{idx:05d}.mkv')
                        idx += 1
                        _copy_chunk(video_path, start, pk, p)
                        chunk_paths.append(p)
                        start = pk
                    p = os.path.join(work, f'{idx:05d}.mkv')
                    idx += 1
                    _encode_chunk(video_path, start, ke, p, v)
                    chunk_paths.append(p)
                else:
                    p = os.path.join(work, f'{idx:05d}.mkv')
                    idx += 1
                    _copy_chunk(video_path, start, ke, p)
                    chunk_paths.append(p)

        concat_list = os.path.join(work, 'concat.txt')
        with open(concat_list, 'w', encoding='utf-8') as f:
            for p in chunk_paths:
                f.write("file '" + p.replace("'", "'\\''") + "'\n")

        # Video is copied for ordinary chunks; audio is normalized to AAC in
        # every chunk so exact boundary chunks and copied-video chunks have the
        # same stream layout and can be concatenated safely. This is much faster
        # than re-encoding the whole x265 video.
        _run(['ffmpeg', '-y', '-v', 'error', '-f', 'concat', '-safe', '0',
              '-i', concat_list, '-map', '0', '-c', 'copy', output_path])
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _exact_full_reencode(video_path, rem, output_path, v, a):
    duration = _duration(video_path)
    keep = []
    cur = 0.0
    for s, e in rem:
        if s > cur:
            keep.append((cur, s))
        cur = max(cur, e)
    if cur < duration:
        keep.append((cur, duration))
    filters, labels = [], []
    for i, (s, e) in enumerate(keep):
        filters.append(
            f'[0:v]trim=start={s:.9f}:end={e:.9f},setpts=PTS-STARTPTS[v{i}]')
        if a:
            filters.append(
                f'[0:a]atrim=start={s:.9f}:end={e:.9f},asetpts=PTS-STARTPTS[a{i}]')
            labels.append(f'[v{i}][a{i}]')
        else:
            labels.append(f'[v{i}]')
    filters.append(''.join(
        labels) + f'concat=n={len(keep)}:v=1:a={1 if a else 0}[outv]' + ('[outa]' if a else ''))
    cmd = ['ffmpeg', '-y', '-v', 'error', '-i', video_path,
           '-filter_complex', ';'.join(filters), '-map', '[outv]']
    if a:
        cmd += ['-map', '[outa]']
    cmd += _codec_args(v)
    if a:
        cmd += ['-c:a', 'aac', '-b:a', '192k']
    cmd += ['-map_metadata', '0', '-avoid_negative_ts', 'make_zero', output_path]
    _run(cmd)
