import argparse
import glob
import os
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed

from audio_match import find_matches
from remove_segments import remove_segments

VIDEO_EXTENSIONS = {'.mp4', '.mov', '.avi', '.mkv', '.webm', '.m4v', '.flv', '.wmv', '.mpeg', '.mpg'}


def _expand_video_inputs(inputs, out_root):
    """Return [(video_path, out_dir), ...] with folder inputs mirrored as <name>_cleaned."""
    jobs = []
    seen = set()
    for path in inputs:
        if os.path.isdir(path):
            folder_name = os.path.basename(os.path.abspath(path.rstrip(os.sep)))
            out_dir = os.path.join(out_root, f'{folder_name}_cleaned')
            for f in sorted(os.listdir(path)):
                video_path = os.path.join(path, f)
                if (os.path.isfile(video_path)
                        and os.path.splitext(f)[1].lower() in VIDEO_EXTENSIONS
                        and video_path not in seen):
                    seen.add(video_path)
                    jobs.append((video_path, out_dir))
        else:
            if path not in seen:
                seen.add(path)
                jobs.append((path, out_root))
    return jobs


def process_video(video_path, ref_paths, out_dir, args):
    all_segments = []
    for ref in ref_paths:
        matches = find_matches(ref, video_path, threshold=args.threshold)
        for m in matches:
            print(f'[{os.path.basename(video_path)}] [{os.path.basename(ref)}] match '
                  f'{m.start:.2f}s -> {m.end:.2f}s (score={m.score:.2f})')
            all_segments.append((m.start, m.end))

    os.makedirs(out_dir, exist_ok=True)
    base = os.path.splitext(os.path.basename(video_path))[0]
    out_path = os.path.join(out_dir, f'{base}_cleaned.mp4')

    if all_segments:
        remove_segments(video_path, all_segments, out_path)
    else:
        print(f'[{os.path.basename(video_path)}] No matches found, copying as-is.')
        shutil.copy2(video_path, out_path)
    print(f'[{os.path.basename(video_path)}] -> written: {out_path}')
    return out_path


def main():
    parser = argparse.ArgumentParser(description='Remove passages without re-encoding video or audio.')
    parser.add_argument('--refs', required=True, help='Folder containing reference .wav files')
    parser.add_argument('--videos', nargs='+', required=True,
                        help='Videos and/or folders containing videos')
    parser.add_argument('--out', required=True, help='Output folder')
    parser.add_argument('--threshold', type=float, default=0.82, help='Similarity threshold (0-1)')
    parser.add_argument('--workers', type=int, default=2, help='Number of videos in parallel')
    args = parser.parse_args()

    ref_paths = sorted(glob.glob(os.path.join(args.refs, '*.wav')))
    if not ref_paths:
        raise SystemExit(f'No .wav found in {args.refs}')
    os.makedirs(args.out, exist_ok=True)

    jobs = _expand_video_inputs(args.videos, args.out)
    if not jobs:
        print('No videos found.')
        return

    print('Mode: STREAM COPY — no video/audio re-encoding.')
    print('Cuts are aligned to keyframes to preserve quality and sync.')
    print(f'{len(jobs)} video(s) to process.\n')

    errors = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {
            executor.submit(process_video, video, ref_paths, out_dir, args): video
            for video, out_dir in jobs
        }
        for future in as_completed(futures):
            video = futures[future]
            try:
                future.result()
            except Exception as exc:
                errors.append((video, str(exc)))
                print(f'[{os.path.basename(video)}] ERROR: {exc}')

    if errors:
        print(f'\n{len(errors)} video(s) failed out of {len(jobs)}.')
    else:
        print(f'\nDone: {len(jobs)} video(s) processed with no errors.')


if __name__ == '__main__':
    main()
