"""
Remove sample passages from videos without the app.

    python cli_remove.py --refs samples/ --videos Season1/ --out cleaned/
    python cli_remove.py --refs samples/ --videos Season1/ --in-place

Every .wav file in --refs is a sample (an intro, an outro...). Each video gets
at most one cut per sample (see --unlimited-extracts), chosen over all samples
together so that cuts never overlap.
"""

import argparse
import glob
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

from audio_match import find_matches, load_target
from pipeline import (DOUBTFUL_WAVEFORM, default_workers, expand_video_inputs, export_video,
                      plan_outputs, replace_video, replaced_path, settle_cuts)


def process_video(video_path, ref_paths, out_base, args):
    name = os.path.basename(video_path)

    def say(text):
        print(f'[{name}] {text}')

    target = load_target(video_path)
    found = []
    for ref in ref_paths:
        for match in find_matches(ref, video_path, threshold=args.threshold,
                                  intro_max_seconds=args.intro_max, target=target):
            match.ref = ref
            found.append(match)
    # Chosen over all samples together: the non-overlapping matches with the
    # highest total score. So an intro also heard in the outro cannot take the
    # copy inside the outro.
    chosen, _ = settle_cuts(found, lambda m: m.ref, lambda m: m.waveform_score,
                            one_per_sample=not args.unlimited_extracts)

    segments = []
    for match in sorted(chosen, key=lambda m: m.start):
        say(f'{os.path.basename(match.ref)}: cut {match.start:.3f}s -> {match.end:.3f}s '
            f'({match.end - match.start:.3f}s, match {match.waveform_score:.0%})')
        if match.waveform_score < DOUBTFUL_WAVEFORM:
            say('  doubtful match, check it in the app: python app_extract.py')
        segments.append((match.start, match.end))
    unused = len(found) - len(chosen)
    if unused:
        say(f'{unused} other candidate(s) left out (overlapping, or a second match of the same sample)')

    if args.in_place:
        if not segments:
            say('no match, left as it is')
            return video_path
        result = replace_video(video_path, segments)
    else:
        out_path = out_base + ('.mkv' if segments else os.path.splitext(video_path)[1])
        if not segments:
            say('no match, copied as it is')
        result = export_video(video_path, segments, out_path)
    for path in result.subtitles:
        say(f'subtitles -> {path}')
    for warning in result.warnings:
        say(f'WARNING: {warning}')
    say(f'{"replaced by" if args.in_place else "written"}: {result.output}')
    return result.output


def main():
    parser = argparse.ArgumentParser(
        description='Remove the passages where samples (intro, outro...) are heard from videos.')
    parser.add_argument('--refs', required=True,
                        help='Folder containing the samples, as .wav files')
    parser.add_argument('--videos', nargs='+', required=True,
                        help='Videos and/or folders containing videos')
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument('--out', help='Output folder')
    target.add_argument('--in-place', action='store_true',
                        help='Replace each video by its cut version (Episode.mp4 becomes Episode.mkv, '
                             'subtitle files are overwritten). Cannot be undone.')
    parser.add_argument('--yes', action='store_true', help='With --in-place: do not ask for confirmation')
    parser.add_argument('--threshold', type=float, default=0.72,
                        help='Match threshold, higher is stricter (default: %(default)s)')
    parser.add_argument('--intro-max', type=float, default=180.0,
                        help='Seconds in which an intro is expected, only used to rank close candidates (default: %(default)s)')
    parser.add_argument('--workers', type=int, default=default_workers(),
                        help='Videos processed in parallel (default: %(default)s on this machine, '
                             'about 1.3 GB of RAM each)')
    parser.add_argument('--unlimited-extracts', action='store_true', default=False,
                        help='Allow several cuts per sample in a video')
    args = parser.parse_args()

    refs = sorted(glob.glob(os.path.join(args.refs, '*.wav')))
    if not refs:
        raise SystemExit(f'No .wav files found in {args.refs}')
    if args.in_place:
        jobs = [(video, None) for video, _ in expand_video_inputs(args.videos)]
        blocked = [replaced_path(v) for v, _ in jobs
                   if os.path.exists(replaced_path(v)) and not os.path.samefile(replaced_path(v), v)]
        if blocked:
            raise SystemExit('These files would be overwritten by a cut video, rename them first:\n  '
                             + '\n  '.join(blocked))
    else:
        os.makedirs(args.out, exist_ok=True)
        planned = plan_outputs(
            [(video, sub, False) for video, sub in expand_video_inputs(args.videos)],
            args.out)
        jobs = [(video, os.path.splitext(out)[0]) for video, out in planned.items()]
    if not jobs:
        print('No videos found.')
        return
    if args.in_place and not args.yes:
        answer = input(f'Replace {len(jobs)} video(s) and their subtitle files in place? '
                       'This cannot be undone. [y/N] ')
        if answer.strip().lower() not in ('y', 'yes'):
            print('Nothing changed.')
            return

    limit = 'every match that fits' if args.unlimited_extracts else 'one cut per sample'
    print(f'{len(jobs)} video(s), {len(refs)} sample(s), threshold {args.threshold:.2f}, '
          f'{limit}, {max(1, args.workers)} at once.\n')

    errors = []
    executor = ThreadPoolExecutor(max_workers=max(1, args.workers))
    futures = {executor.submit(process_video, video, refs, out_base, args): video
               for video, out_base in jobs}
    try:
        for future in as_completed(futures):
            video = futures[future]
            try:
                future.result()
            except Exception as exc:
                errors.append((video, str(exc)))
                print(f'[{os.path.basename(video)}] ERROR: {exc}')
    except KeyboardInterrupt:
        print('\nInterrupted: queued videos are cancelled, the ones in progress finish.')
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
