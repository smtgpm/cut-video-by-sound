import argparse
import glob
import os
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed

from audio_match import find_matches
from remove_segments import remove_segments

VIDEO_EXTENSIONS = {'.mp4', '.mov', '.avi', '.mkv', '.webm', '.m4v', '.flv', '.wmv', '.mpeg', '.mpg'}


def _expand_video_inputs(inputs):
    videos = []
    for path in inputs:
        if os.path.isdir(path):
            videos.extend(sorted(
                os.path.join(path, f) for f in os.listdir(path)
                if os.path.isfile(os.path.join(path, f))
                and os.path.splitext(f)[1].lower() in VIDEO_EXTENSIONS
            ))
        else:
            videos.append(path)
    return list(dict.fromkeys(videos))


def process_video(video_path, ref_paths, out_dir, args):
    all_segments = []
    for ref in ref_paths:
        matches = find_matches(ref, video_path, threshold=args.threshold)
        for m in matches:
            print(f'[{os.path.basename(video_path)}] [{os.path.basename(ref)}] match '
                  f'{m.start:.2f}s -> {m.end:.2f}s (score={m.score:.2f})')
            all_segments.append((m.start, m.end))

    base = os.path.splitext(os.path.basename(video_path))[0]
    out_path = os.path.join(out_dir, f'{base}_nettoye.mp4')

    if all_segments:
        remove_segments(video_path, all_segments, out_path)
    else:
        print(f'[{os.path.basename(video_path)}] Aucune occurrence trouvée, copie telle quelle.')
        shutil.copy2(video_path, out_path)
    print(f'[{os.path.basename(video_path)}] -> écrit : {out_path}')
    return out_path


def main():
    parser = argparse.ArgumentParser(description='Supprime des passages sans ré-encoder vidéo ni audio.')
    parser.add_argument('--refs', required=True, help='Dossier contenant les .wav de référence')
    parser.add_argument('--videos', nargs='+', required=True,
                        help='Vidéos et/ou dossiers contenant les vidéos')
    parser.add_argument('--out', required=True, help='Dossier de sortie')
    parser.add_argument('--threshold', type=float, default=0.82, help='Seuil de similarité (0-1)')
    parser.add_argument('--workers', type=int, default=2, help='Nombre de vidéos en parallèle')
    args = parser.parse_args()

    ref_paths = sorted(glob.glob(os.path.join(args.refs, '*.wav')))
    if not ref_paths:
        raise SystemExit(f'Aucun .wav trouvé dans {args.refs}')
    os.makedirs(args.out, exist_ok=True)

    videos = _expand_video_inputs(args.videos)
    if not videos:
        print('Aucune vidéo trouvée.')
        return

    print('Mode : COPIE DE FLUX — aucun ré-encodage vidéo/audio.')
    print('Les coupes sont alignées sur les keyframes pour préserver la qualité et la synchronisation.')
    print(f'{len(videos)} vidéo(s) à traiter.\n')

    errors = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {executor.submit(process_video, v, ref_paths, args.out, args): v for v in videos}
        for future in as_completed(futures):
            video = futures[future]
            try:
                future.result()
            except Exception as exc:
                errors.append((video, str(exc)))
                print(f'[{os.path.basename(video)}] ERREUR : {exc}')

    if errors:
        print(f'\n{len(errors)} vidéo(s) en erreur sur {len(videos)}.')
    else:
        print(f'\nTerminé : {len(videos)} vidéo(s) traitée(s) sans erreur.')


if __name__ == '__main__':
    main()
