import argparse
import glob
import os
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed

from audio_match import find_matches
from remove_segments import remove_segments

VIDEO_EXTENSIONS = {'.mp4', '.mov', '.avi', '.mkv',
                    '.webm', '.m4v', '.flv', '.wmv', '.mpeg', '.mpg'}


def _expand_video_inputs(inputs, out_root):
    jobs, seen = [], set()
    for path in inputs:
        if os.path.isdir(path):
            folder_name = os.path.basename(
                os.path.abspath(path.rstrip(os.sep)))
            out_dir = os.path.join(out_root, f'{folder_name}_cleaned')
            for f in sorted(os.listdir(path)):
                p = os.path.join(path, f)
                if os.path.isfile(p) and os.path.splitext(f)[1].lower() in VIDEO_EXTENSIONS and p not in seen:
                    seen.add(p)
                    jobs.append((p, out_dir))
        elif path not in seen:
            seen.add(path)
            jobs.append((path, out_root))
    return jobs


def process_video(video_path, ref_paths, out_dir, args):
    segments = []
    for ref in ref_paths:
        matches = find_matches(ref, video_path, threshold=args.threshold,
                               intro_max_seconds=args.intro_max)
        for m in matches:
            # CRITICAL: the removal interval is exactly the reference WAV's
            # duration. No padding, no keyframe adjustment, no extension.
            print(f'[{os.path.basename(video_path)}] [{os.path.basename(ref)}] '
                  f'match {m.start:.3f}s -> {m.end:.3f}s '
                  f'(duration={m.end-m.start:.3f}s, score={m.score:.3f})')
            segments.append((m.start, m.end))

    os.makedirs(out_dir, exist_ok=True)
    base = os.path.splitext(os.path.basename(video_path))[0]
    out_path = os.path.join(out_dir, f'{base}_cleaned.mkv')

    if not segments:
        print(f'[{os.path.basename(video_path)}] No matches found, copying as-is.')
        shutil.copy2(video_path, out_path)
        return out_path

    remove_segments(video_path, segments, out_path)
    print(f'[{os.path.basename(video_path)}] -> written: {out_path}')
    return out_path


def main():
    parser = argparse.ArgumentParser(
        description='Frame-accurate removal of reference-audio passages.')
    parser.add_argument('--refs', required=True,
                        help='Folder containing reference .wav files')
    parser.add_argument('--videos', nargs='+', required=True,
                        help='Videos and/or folders containing videos')
    parser.add_argument('--out', required=True, help='Output folder')
    parser.add_argument('--threshold', type=float, default=0.85,
                        help='Audio correlation threshold (default: 0.85)')
    parser.add_argument('--intro-max', type=float, default=180.0,
                        help='Soft preference region for early matches (default: 180s)')
    parser.add_argument('--workers', type=int, default=1,
                        help='Videos processed in parallel (default: 1)')
    args = parser.parse_args()

    refs = sorted(glob.glob(os.path.join(args.refs, '*.wav')))
    if not refs:
        raise SystemExit(f'No .wav files found in {args.refs}')
    os.makedirs(args.out, exist_ok=True)

    jobs = _expand_video_inputs(args.videos, args.out)
    if not jobs:
        print('No videos found.')
        return

    print('Mode: SMART HYBRID — exact removal timestamps; only boundary GOPs re-encoded, rest video copied.')
    print(
        f'Threshold: {args.threshold:.2f}; reference duration is the maximum removal duration.')
    print(f'{len(jobs)} video(s) to process. Workers: {max(1, args.workers)}\n')

    errors = []
    executor = ThreadPoolExecutor(max_workers=max(1, args.workers))
    futures = {executor.submit(process_video, video, refs, out_dir, args): video
               for video, out_dir in jobs}
    try:
        for future in as_completed(futures):
            video = futures[future]
            try:
                future.result()
            except Exception as exc:
                errors.append((video, str(exc)))
                print(f'[{os.path.basename(video)}] ERROR: {exc}')
    except KeyboardInterrupt:
        print('\nInterrupted: cancelling queued jobs. Active FFmpeg jobs may finish.')
        for future in futures:
            future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)
        return
    else:
        executor.shutdown(wait=True)

    if errors:
        print(f'\n{len(errors)} video(s) failed out of {len(jobs)}.')
    else:
        print(f'\nDone: {len(jobs)} video(s) processed with no errors.')


if __name__ == '__main__':
    main()
